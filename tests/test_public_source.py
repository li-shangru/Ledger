"""Synthetic Git-index source export checks; no owner runtime or history is read."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/public_source.py"
SPEC = importlib.util.spec_from_file_location("ledger_public_source", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
public_source = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = public_source
SPEC.loader.exec_module(public_source)


def _git(repo: Path, *arguments: str) -> None:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    subprocess.run(
        ["git", "-C", str(repo), *arguments], env=environment, check=True, capture_output=True
    )


def _put(repo: Path, name: str, value: str | bytes = "fictional source\n") -> Path:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value.encode() if isinstance(value, str) else value)
    return path


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    repo = tmp_path.resolve() / "source"
    repo.mkdir()
    _git(repo, "init", "--quiet")
    _put(repo, "README.md", "# Fictional source\n")
    _put(repo, "LICENSE", "Synthetic test license text\n")
    _put(repo, "SECURITY.md", "Use a private synthetic report.\n")
    _put(repo, ".github/workflows/ci.yml", "name: Synthetic workflow\n")
    _put(repo, "src/example/__init__.py", 'VALUE = "fixture"\n')
    _put(repo, "tests/test_example.py", "def test_example():\n    assert True\n")
    script = _put(repo, "scripts/example-check", "#!/bin/sh\nexit 0\n")
    script.chmod(0o755)
    _put(repo, "docs/example.md", "Independently authored documentation.\n")
    _put(repo, "configs/example.toml", 'name = "MERCHANT_001"\n')
    _put(repo, "vendor/example/LICENSE-APACHE-2.0", "Synthetic license fixture\n")
    _put(repo, ".streamlit/config.toml", "[server]\nheadless = true\n")
    _git(repo, "add", "--all")
    return repo


def _export(repo: Path, name: str = "public") -> tuple[Path, dict[str, object]]:
    checked = public_source.check_source(repo)
    dest = repo.parent / name
    result = public_source.export_source(repo, dest, expected_sha256=checked.manifest_sha256)
    return dest, result


def test_export_uses_exact_current_tracked_bytes_and_no_history(source_repo: Path):
    _put(source_repo, "README.md", "# Reviewed current working bytes\n")
    index_before = (source_repo / ".git/index").read_bytes()
    checked = public_source.check_source(source_repo)
    dest, result = _export(source_repo)

    assert (source_repo / ".git/index").read_bytes() == index_before
    assert not (dest / ".git").exists()
    assert result["verified"] is True
    assert result["manifest_sha256"] == checked.manifest_sha256
    manifest_bytes = (dest / public_source.MANIFEST_NAME).read_bytes()
    assert hashlib.sha256(manifest_bytes).hexdigest() == checked.manifest_sha256
    manifest = json.loads(manifest_bytes)
    assert manifest == checked.manifest
    assert str(source_repo) not in manifest_bytes.decode()
    assert {path.relative_to(dest).as_posix() for path in dest.rglob("*") if path.is_file()} == {
        public_source.MANIFEST_NAME,
        *(entry["path"] for entry in manifest["files"]),
    }
    for entry in manifest["files"]:
        content = (dest / entry["path"]).read_bytes()
        assert content == (source_repo / entry["path"]).read_bytes()
        assert entry["size"] == len(content)
        assert entry["sha256"] == hashlib.sha256(content).hexdigest()
    assert (dest / "scripts/example-check").stat().st_mode & 0o111
    assert not (dest / "src/example/__init__.py").stat().st_mode & 0o111


def test_untracked_and_ignored_private_files_are_never_copied(source_repo: Path):
    _put(source_repo, ".gitignore", "data/\n.env\n")
    _git(source_repo, "add", ".gitignore")
    _put(source_repo, "data/private.db", b"\x00private fixture")
    _put(source_repo, ".env", "secret fixture")
    _put(source_repo, "src/unreviewed.py", "UNREVIEWED = True\n")
    dest, result = _export(source_repo)
    assert result["untracked_files_ignored"] == 1
    assert not (dest / "data").exists()
    assert not (dest / ".env").exists()
    assert not (dest / "src/unreviewed.py").exists()


@pytest.mark.parametrize(
    "relative",
    [
        "data/statement.txt",
        "docs/statements/one.txt",
        "docs/statement-2042.txt",
        "configs/secrets.toml",
        "src/.env",
        "vendor/history/log.txt",
        "docs/transactions.json",
        "exports/report.json",
        "docs/statement.pdf",
        "vendor/library.whl",
        "unexpected.txt",
        ".github/workflows/private.yml",
    ],
)
def test_tracked_private_or_non_source_paths_refuse_export(source_repo: Path, relative: str):
    _put(source_repo, relative)
    _git(source_repo, "add", "--all")
    with pytest.raises(public_source.SourceError):
        public_source.check_source(source_repo)
    assert not (source_repo.parent / "public").exists()


@pytest.mark.parametrize(
    "content", [b"\x00text", b"\xff\xfe", b"text\x01", "text\u202ehidden".encode()]
)
def test_binary_and_directional_control_content_is_rejected(source_repo: Path, content: bytes):
    _put(source_repo, "src/binary.py", content)
    _git(source_repo, "add", "--all")
    with pytest.raises(public_source.SourceError):
        public_source.check_source(source_repo)


@pytest.mark.parametrize(
    "content",
    [
        "/" + "Users/fictional-person/private/file",
        "/" + "Volumes/fictional-drive/private/file",
        "ghp_" + "A" * 35,
        "-----BEGIN " + "PRIVATE KEY-----",
    ],
)
def test_private_path_and_credential_signatures_are_rejected_without_echoing_content(
    source_repo: Path, content: str
):
    _put(source_repo, "docs/unsafe.md", content)
    _git(source_repo, "add", "--all")
    with pytest.raises(public_source.SourceError) as error:
        public_source.check_source(source_repo)
    assert content not in str(error.value)


def test_reserved_example_home_path_is_supported(source_repo: Path):
    _put(source_repo, "docs/example.md", "/" + "Users/example/private/example.csv\n")
    checked = public_source.check_source(source_repo)
    assert checked.contents["docs/example.md"].endswith(b"example.csv\n")


def test_tracked_symlink_is_refused(source_repo: Path):
    (source_repo / "src/link.py").symlink_to("example/__init__.py")
    _git(source_repo, "add", "--all")
    with pytest.raises(public_source.SourceError, match="links"):
        public_source.check_source(source_repo)


def test_unstaged_symlink_replacement_is_refused(source_repo: Path):
    target = source_repo / "README.md"
    target.unlink()
    target.symlink_to("docs/example.md")
    with pytest.raises(OSError):
        public_source.check_source(source_repo)


def test_symlink_parent_and_symlink_repository_are_refused(source_repo: Path):
    original = source_repo / "src/example"
    moved = source_repo.parent / "moved-source"
    original.rename(moved)
    original.symlink_to(moved, target_is_directory=True)
    with pytest.raises(OSError):
        public_source.check_source(source_repo)
    alias = source_repo.parent / "source-alias"
    alias.symlink_to(source_repo, target_is_directory=True)
    with pytest.raises(OSError):
        public_source.check_source(alias)


def test_hardlinked_source_file_is_refused(source_repo: Path):
    os.link(source_repo / "README.md", source_repo.parent / "linked-readme")
    with pytest.raises(public_source.SourceError, match="single-link"):
        public_source.check_source(source_repo)


def test_unmerged_index_and_mode_drift_are_refused(source_repo: Path, monkeypatch):
    actual = public_source._index(source_repo)
    monkeypatch.setattr(public_source, "_index", lambda _repo: actual.replace(b" 0\t", b" 1\t", 1))
    with pytest.raises(public_source.SourceError, match="unmerged"):
        public_source.check_source(source_repo)
    monkeypatch.undo()
    (source_repo / "README.md").chmod(0o755)
    with pytest.raises(public_source.SourceError, match="mode differs"):
        public_source.check_source(source_repo)


def test_case_colliding_parent_paths_are_refused_before_files_are_read(
    source_repo: Path, monkeypatch
):
    blob = b"0" * 40
    index = (
        b"100644 "
        + blob
        + b" 0\tsrc/Case/first.py\0"
        + b"100644 "
        + blob
        + b" 0\tsrc/case/second.py\0"
    )
    monkeypatch.setattr(public_source, "_index", lambda _repo: index)
    monkeypatch.setattr(public_source, "_read_regular", lambda *_args: (b"fictional\n", 0o644))
    with pytest.raises(public_source.SourceError, match="case-colliding"):
        public_source.check_source(source_repo)


def test_index_drift_during_inspection_is_refused(source_repo: Path, monkeypatch):
    indexed = public_source._index(source_repo)
    values = iter((indexed, indexed + b"changed"))
    monkeypatch.setattr(public_source, "_index", lambda _repo: next(values))
    with pytest.raises(public_source.SourceError, match="index changed"):
        public_source.check_source(source_repo)


def test_changed_reviewed_bytes_refuse_before_creating_destination(source_repo: Path):
    digest = public_source.check_source(source_repo).manifest_sha256
    _put(source_repo, "README.md", "changed after review\n")
    destination = source_repo.parent / "public"
    with pytest.raises(public_source.SourceError, match="changed after review"):
        public_source.export_source(source_repo, destination, expected_sha256=digest)
    assert not destination.exists()


def test_existing_destination_and_repository_destinations_are_untouched(source_repo: Path):
    digest = public_source.check_source(source_repo).manifest_sha256
    existing = source_repo.parent / "existing"
    existing.mkdir()
    (existing / "keep.txt").write_text("keep")
    with pytest.raises(FileExistsError):
        public_source.export_source(source_repo, existing, expected_sha256=digest)
    assert (existing / "keep.txt").read_text() == "keep"
    for destination in (source_repo, source_repo / "public", source_repo.parent):
        with pytest.raises(public_source.SourceError, match="outside"):
            public_source.export_source(source_repo, destination, expected_sha256=digest)
    assert not (source_repo / "public").exists()


def test_output_symlink_parent_and_traversal_are_refused(source_repo: Path):
    digest = public_source.check_source(source_repo).manifest_sha256
    real = source_repo.parent / "actual-output-parent"
    real.mkdir()
    alias = source_repo.parent / "output-alias"
    alias.symlink_to(real, target_is_directory=True)
    with pytest.raises(OSError):
        public_source.export_source(source_repo, alias / "public", expected_sha256=digest)
    assert not (real / "public").exists()
    with pytest.raises(public_source.SourceError, match="traversal"):
        public_source.export_source(
            source_repo, source_repo / ".." / "public", expected_sha256=digest
        )


def test_verification_rejects_changed_or_extra_exported_files(source_repo: Path):
    dest, _result = _export(source_repo)
    readme = dest / "README.md"
    original = readme.read_bytes()
    readme.write_text("tampered\n")
    with pytest.raises(public_source.SourceError, match="differ"):
        public_source.verify_export(dest)
    readme.write_bytes(original)
    (dest / "extra.txt").write_text("not declared")
    with pytest.raises(public_source.SourceError, match="undeclared"):
        public_source.verify_export(dest)


def test_verification_rejects_hardlinks_and_symlinks(source_repo: Path):
    dest, _result = _export(source_repo)
    os.link(dest / "README.md", source_repo.parent / "linked-output")
    with pytest.raises(public_source.SourceError, match="single-link"):
        public_source.verify_export(dest)
    (source_repo.parent / "linked-output").unlink()
    (dest / "unexpected-link").symlink_to("README.md")
    with pytest.raises(public_source.SourceError, match="unexpected link"):
        public_source.verify_export(dest)


def test_verification_refuses_invalid_manifest_metadata(source_repo: Path):
    dest, _result = _export(source_repo)
    manifest = json.loads((dest / public_source.MANIFEST_NAME).read_bytes())
    manifest["files"][0]["mode"] = []
    (dest / public_source.MANIFEST_NAME).write_bytes(public_source._manifest_bytes(manifest))
    with pytest.raises(public_source.SourceError, match="metadata"):
        public_source.verify_export(dest)


def test_cli_check_export_and_verify_are_explicit_and_content_free_on_errors(
    source_repo: Path, capsys
):
    assert public_source.main(["check", "--repo", str(source_repo)]) == 0
    checked = json.loads(capsys.readouterr().out)
    dest = source_repo.parent / "public"
    assert (
        public_source.main(
            [
                "export",
                "--repo",
                str(source_repo),
                "--dest",
                str(dest),
                "--expect-sha256",
                checked["manifest_sha256"],
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["verified"] is True
    assert (
        public_source.main(
            ["verify", "--dest", str(dest), "--expect-sha256", checked["manifest_sha256"]]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["verified"] is True
    assert (
        public_source.main(
            [
                "export",
                "--repo",
                str(source_repo),
                "--dest",
                str(dest),
                "--expect-sha256",
                "invalid",
            ]
        )
        == 1
    )
    assert "public source refused" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("relative", "mode"),
    [
        ("README.md", 0o600),
        ("README.md", 0o666),
        ("scripts/example-check", 0o700),
        ("scripts/example-check", 0o777),
        ("SOURCE_MANIFEST.json", 0o600),
        ("SOURCE_MANIFEST.json", 0o666),
    ],
)
def test_export_verification_rejects_permission_drift(source_repo, relative, mode):
    dest, result = _export(source_repo)
    (dest / relative).chmod(mode)
    with pytest.raises(public_source.SourceError):
        public_source.verify_export(dest, expected_sha256=result["manifest_sha256"])


PNG_PATH = "docs/images/synthetic-preview.png"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _png_chunk(kind: bytes, payload: bytes = b"") -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload))
    )


def _png_header(
    width=1, height=1, depth=8, color=2, compression=0, filtering=0, interlace=0
) -> bytes:
    return _png_chunk(
        b"IHDR",
        struct.pack(">IIBBBBB", width, height, depth, color, compression, filtering, interlace),
    )


def _png(compressed: bytes | None = None, *, header: bytes | None = None) -> bytes:
    return (
        PNG_SIGNATURE
        + (_png_header() if header is None else header)
        + _png_chunk(
            b"IDAT", zlib.compress(b"\0\x14\x28\x3c") if compressed is None else compressed
        )
        + _png_chunk(b"IEND")
    )


def _backend():
    spec = importlib.util.spec_from_file_location(
        "ledger_png_build_backend_test", SCRIPT.with_name("build_backend.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_png_export_and_backend_manifest_ingestion_preserve_exact_bytes(source_repo):
    # All five PNG filters and split IDAT chunks are valid. Pixels are authored here.
    rows = b"".join(bytes([value, 20, 40, 60]) for value in range(5))
    compressed = zlib.compress(rows)
    content = (
        PNG_SIGNATURE
        + _png_header(height=5)
        + _png_chunk(b"IDAT", compressed[:3])
        + _png_chunk(b"IDAT")
        + _png_chunk(b"IDAT", compressed[3:])
        + _png_chunk(b"IEND")
    )
    _put(source_repo, PNG_PATH, content)
    _git(source_repo, "add", "--all")
    dest, result = _export(source_repo)
    checked = public_source.check_source(source_repo)
    assert checked.contents[PNG_PATH] == (dest / PNG_PATH).read_bytes() == content
    entry = next(item for item in checked.manifest["files"] if item["path"] == PNG_PATH)
    assert entry == {
        "path": PNG_PATH,
        "sha256": hashlib.sha256(content).hexdigest(),
        "size": len(content),
        "mode": "0644",
    }
    assert public_source.verify_export(dest, expected_sha256=result["manifest_sha256"])["verified"]
    snapshot = _backend().source_snapshot(dest)
    assert snapshot.contents == checked.contents
    assert snapshot.manifest_sha256 == checked.manifest_sha256


@pytest.mark.parametrize(
    "relative",
    [
        "docs/preview.png",
        "src/preview.png",
        "tests/preview.png",
        "docs/images/subdir/preview.png",
        "docs/images/Preview.png",
        "docs/images/preview.PNG",
        "docs/images/.preview.png",
        "docs/images/preview.extra.png",
        "docs/images/preview.svg",
        "docs/images/preview.jpg",
        "docs/images/preview.pdf",
        "docs/images/secrets.png",
        "docs/images/transactions.png",
        "docs/images/statement-2042.png",
        "docs/images/llm_export.png",
        "docs/images/" + "a" * 97 + ".png",
    ],
)
def test_png_cannot_bypass_source_path_restrictions(source_repo, relative):
    _put(source_repo, relative, _png())
    _git(source_repo, "add", "--all")
    with pytest.raises(public_source.SourceError):
        public_source.check_source(source_repo)


@pytest.mark.parametrize(
    "header",
    [
        _png_header(width=0),
        _png_header(height=0),
        _png_header(width=4097),
        _png_header(height=4097),
        _png_header(width=4000, height=2001),
        _png_header(depth=16),
        *(_png_header(color=color) for color in (0, 1, 3, 4, 5, 6)),
        _png_header(compression=1),
        _png_header(filtering=1),
        _png_header(interlace=1),
    ],
)
def test_png_header_constraints_are_enforced_before_decompression(header, monkeypatch):
    def unexpected_decompression(*args, **kwargs):
        pytest.fail("unsupported PNG header reached decompression")

    monkeypatch.setattr(zlib, "decompressobj", unexpected_decompression)
    with pytest.raises(public_source.SourceError):
        public_source._admit_content(PNG_PATH, _png(header=header))


@pytest.mark.parametrize(
    "kind", [b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"iCCP", b"PLTE", b"acTL", b"abcd"]
)
def test_png_metadata_and_other_chunks_are_refused(kind):
    content = PNG_SIGNATURE + _png_header() + _png_chunk(kind, b"synthetic") + _png()[33:]
    with pytest.raises(public_source.SourceError):
        public_source._admit_content(PNG_PATH, content)


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"not a PNG", id="signature"),
        pytest.param(PNG_SIGNATURE, id="empty"),
        pytest.param(_png()[:-1], id="truncated-chunk"),
        pytest.param(PNG_SIGNATURE + struct.pack(">I", 0xFFFFFFFF) + b"IHDR", id="chunk-size"),
        pytest.param(_png(header=_png_chunk(b"IHDR", b"\0" * 12)), id="header-size"),
        pytest.param(PNG_SIGNATURE + _png()[33:], id="missing-header"),
        pytest.param(_png(header=_png_header() * 2), id="repeated-header"),
        pytest.param(
            PNG_SIGNATURE + _png_header() + _png_chunk(b"IDAT") + _png()[8:],
            id="header-between-data-chunks",
        ),
        pytest.param(PNG_SIGNATURE + _png_header() + _png_chunk(b"IEND"), id="missing-data"),
        pytest.param(_png()[:-12], id="missing-end"),
        pytest.param(_png()[:-12] + _png_chunk(b"IEND", b"x"), id="end-payload"),
        pytest.param(_png() + b"trailing", id="trailing-bytes"),
        pytest.param(_png() + _png_chunk(b"IEND"), id="repeated-end"),
        pytest.param(_png() + _png_chunk(b"IDAT"), id="data-after-end"),
        pytest.param(_png()[:32] + bytes([_png()[32] ^ 1]) + _png()[33:], id="header-crc"),
        pytest.param(_png()[:-13] + bytes([_png()[-13] ^ 1]) + _png()[-12:], id="data-crc"),
        pytest.param(_png()[:-1] + bytes([_png()[-1] ^ 1]), id="end-crc"),
        pytest.param(_png(b"invalid"), id="invalid-zlib"),
        pytest.param(_png(zlib.compress(b"\0" * 4)[:-1]), id="truncated-zlib"),
        pytest.param(
            _png(zlib.compress(b"\0" * 4) + zlib.compress(b"")), id="multiple-zlib-members"
        ),
        pytest.param(_png(zlib.compress(b"\0" * 4) + b"x"), id="zlib-trailing-bytes"),
        pytest.param(_png(zlib.compress(b"\0" * 3)), id="short-decoded-data"),
        pytest.param(_png(zlib.compress(b"\0" * 5)), id="extra-decoded-data"),
        pytest.param(_png(zlib.compress(b"\0" * 200_000)), id="oversized-decoded-data"),
        pytest.param(_png(zlib.compress(b"\5\x14\x28\x3c")), id="invalid-row-filter"),
    ],
)
def test_malformed_png_structure_and_streams_are_refused(content):
    with pytest.raises(public_source.SourceError):
        public_source._admit_content(PNG_PATH, content)


def test_png_file_and_chunk_count_bounds_are_enforced_before_decompression(monkeypatch):
    def unexpected_decompression(*args, **kwargs):
        pytest.fail("oversized PNG reached decompression")

    monkeypatch.setattr(zlib, "decompressobj", unexpected_decompression)
    cases = [
        _png() + b"\0" * (2 * 1024 * 1024),
        PNG_SIGNATURE + _png_header() + _png_chunk(b"IDAT") * 1024 + _png_chunk(b"IEND"),
    ]
    for content in cases:
        with pytest.raises(public_source.SourceError):
            public_source._admit_content(PNG_PATH, content)


def test_png_decompression_never_exceeds_the_declared_scanline_budget(monkeypatch):
    create_decoder = zlib.decompressobj
    limits = []

    class BoundedDecoder:
        def __init__(self):
            self.decoder = create_decoder()

        def decompress(self, content, max_length):
            limits.append(max_length)
            return self.decoder.decompress(content, max_length)

        def __getattr__(self, name):
            return getattr(self.decoder, name)

    monkeypatch.setattr(zlib, "decompressobj", BoundedDecoder)
    with pytest.raises(public_source.SourceError, match="decoded size"):
        public_source._admit_content(PNG_PATH, _png(zlib.compress(b"\0" * 200_000)))
    assert limits == [5]  # One RGB scanline, one filter byte, one overflow sentinel.


@pytest.mark.parametrize("reader", ["export", "backend"])
def test_png_manifest_readers_revalidate_even_when_hashes_match(source_repo, reader):
    _put(source_repo, PNG_PATH, _png())
    _git(source_repo, "add", "--all")
    dest, _result = _export(source_repo)
    malformed = _png() + b"synthetic hidden payload"
    (dest / PNG_PATH).write_bytes(malformed)
    manifest_path = dest / public_source.MANIFEST_NAME
    manifest = json.loads(manifest_path.read_bytes())
    entry = next(item for item in manifest["files"] if item["path"] == PNG_PATH)
    entry.update(sha256=hashlib.sha256(malformed).hexdigest(), size=len(malformed))
    manifest_path.write_bytes(public_source._manifest_bytes(manifest))
    backend = _backend()
    action = public_source.verify_export if reader == "export" else backend.source_snapshot
    error = public_source.SourceError if reader == "export" else backend.source.SourceError
    with pytest.raises(error):
        action(dest)


def test_png_bytes_disguised_as_text_still_fail_utf8_validation(source_repo):
    _put(source_repo, "docs/images/preview.txt", _png())
    _git(source_repo, "add", "--all")
    with pytest.raises(public_source.SourceError, match="not UTF-8"):
        public_source.check_source(source_repo)
