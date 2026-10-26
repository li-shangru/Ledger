#!/usr/bin/env python3
"""Validate and export reviewed tracked source without Git history or runtime files.

This is a conservative structural gate, not a semantic privacy certification.
Review fixture provenance, public text and image pixels separately. A check returns the digest
that export requires, binding the copy to the exact reviewed file manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import struct
import subprocess
import sys
import zlib
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

FORMAT = "ledger-public-source-v1"
MANIFEST_NAME = "SOURCE_MANIFEST.json"
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 256 * 1024 * 1024
MAX_FILES = 10_000
MAX_PNG_BYTES = 2 * 1024 * 1024
MAX_PNG_DIMENSION = 4096
MAX_PNG_PIXELS = 8_000_000
MAX_PNG_CHUNKS = 1024
DOCUMENTATION_PNG = re.compile(r"docs/images/[a-z0-9][a-z0-9_-]{0,95}\.png")
SOURCE_ROOTS = frozenset({"src", "tests", "scripts", "docs", "configs", "vendor"})
ROOT_FILES = frozenset(
    {
        "LICENSE",
        "SECURITY.md",
        ".github/workflows/ci.yml",
        "README.md",
        "AGENTS.md",
        "pyproject.toml",
        "requirements.lock",
        "streamlit_app.py",
        ".gitignore",
        ".python-version",
        ".editorconfig",
        ".ledgerrc.example",
        ".streamlit/config.toml",
    }
)
TEXT_SUFFIXES = frozenset(
    {
        ".py",
        ".pyi",
        ".md",
        ".toml",
        ".json",
        ".yaml",
        ".yml",
        ".txt",
        ".lock",
        ".sh",
        ".mmd",
        ".patch",
        ".diff",
    }
)
PRIVATE_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "__pycache__",
        ".cache",
        "data",
        "runtime",
        "runtimes",
        "statements",
        "credentials",
        "secrets",
        "history",
        "git-history",
        "inputs",
        "outputs",
        "exports",
        "backups",
        "db-backups",
        "import-runs",
        "pdf-inbox",
        "processed-pdfs",
        "dist",
        "build",
        "node_modules",
        "logs",
        "audit-runs",
    }
)
PRIVATE_BASENAMES = re.compile(
    r"^(?:\.env(?:\..*)?|\.netrc|\.npmrc|\.pypirc|\.ledgerrc|"
    r"credentials(?:\..*)?|secrets(?:\..*)?|id_(?:rsa|dsa|ecdsa|ed25519)(?:\..*)?)$",
    re.IGNORECASE,
)
PRIVATE_PATH = re.compile(r"/(?:Users|home)/([^/\s\"'`<>]+)/|/" + r"Volumes/[^/\s\"'`<>]+/")
EXAMPLE_USERS = frozenset(
    {"example", "example-user", "user", "username", "alice", "bob", "synthetic"}
)
CREDENTIAL_SIGNATURES = (
    re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{24,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
)


class SourceError(ValueError):
    """An input cannot be admitted as reviewed public source."""


@dataclass(frozen=True)
class SourceSnapshot:
    manifest: dict[str, object]
    contents: dict[str, bytes]
    untracked_count: int

    @property
    def manifest_sha256(self) -> str:
        return _sha256(_manifest_bytes(self.manifest))

    def summary(self) -> dict[str, object]:
        return {
            "manifest": self.manifest,
            "manifest_sha256": self.manifest_sha256,
            "untracked_files_ignored": self.untracked_count,
            "scope": "Git index entries only; all untracked and ignored files are excluded",
            "limitation": "Content provenance and semantic privacy require separate review",
        }


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _manifest_bytes(manifest: dict[str, object]) -> bytes:
    return (json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode()


def _absolute(path: str | Path) -> Path:
    value = Path(path).expanduser()
    if ".." in value.parts:
        raise SourceError("parent traversal is not allowed")
    return Path(os.path.abspath(value))


@contextmanager
def _directory(path: Path) -> Iterator[int]:
    """Open every absolute path component without following a symlink."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:]:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _parent(root_fd: int, relative: str, *, create: bool = False) -> Iterator[tuple[int, str]]:
    parts = PurePosixPath(relative).parts
    descriptor = os.dup(root_fd)
    try:
        for component in parts[:-1]:
            if create:
                with suppress(FileExistsError):
                    os.mkdir(component, 0o755, dir_fd=descriptor)
            child = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = child
        yield descriptor, parts[-1]
    finally:
        os.close(descriptor)


def _admit_path(relative: str) -> None:
    if not relative or len(relative) > 512 or "\\" in relative or relative.startswith("/"):
        raise SourceError("invalid repository-relative path")
    parts = relative.split("/")
    if len(parts) > 32 or any(
        part in {"", ".", ".."} or not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts
    ):
        raise SourceError("noncanonical or unsupported source path")
    if relative not in ROOT_FILES and (len(parts) < 2 or parts[0] not in SOURCE_ROOTS):
        raise SourceError(f"{relative}: outside the public source allowlist")
    if any(part.casefold() in PRIVATE_DIRECTORIES for part in parts):
        raise SourceError(f"{relative}: runtime, history or private directory")
    name = parts[-1]
    if PRIVATE_BASENAMES.fullmatch(name) and relative != ".ledgerrc.example":
        raise SourceError(f"{relative}: credential or local configuration file")
    if re.fullmatch(
        r"(?:transactions|transfers|statement|statement[-_].*|llm_export.*)\.(?:json|jsonl|txt|xml|png)",
        name,
        re.IGNORECASE,
    ):
        raise SourceError(f"{relative}: statement or financial export artifact")
    if relative in ROOT_FILES:
        return
    if any(part.startswith(".") for part in parts):
        raise SourceError(f"{relative}: hidden source entries are not admitted")
    if DOCUMENTATION_PNG.fullmatch(relative):
        return
    suffix = PurePosixPath(name).suffix.lower()
    extensionless_script = parts[0] == "scripts" and not suffix
    license_text = name == "LICENSE" or name.startswith("LICENSE-")
    if suffix not in TEXT_SUFFIXES and not extensionless_script and not license_text:
        raise SourceError(f"{relative}: unsupported or artifact file type")


def _admit_unique_path(relative: str, folded: dict[str, str]) -> None:
    parts = relative.split("/")
    for length in range(1, len(parts) + 1):
        prefix = "/".join(parts[:length])
        key = prefix.casefold()
        previous = folded.get(key)
        if previous is not None and (previous != prefix or length == len(parts)):
            raise SourceError("duplicate or case-colliding source paths")
        folded[key] = prefix


def _admit_text(relative: str, content: bytes) -> None:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise SourceError(f"{relative}: not UTF-8 text") from error
    if any((ord(char) < 32 and char not in "\n\r\t") or ord(char) == 127 for char in text):
        raise SourceError(f"{relative}: binary or control-byte content")
    if any(char in text for char in "\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"):
        raise SourceError(f"{relative}: directional control characters")
    for match in PRIVATE_PATH.finditer(text):
        if match.group(1) not in EXAMPLE_USERS:
            raise SourceError(f"{relative}: suspicious private absolute path")
    if any(pattern.search(text) for pattern in CREDENTIAL_SIGNATURES):
        raise SourceError(f"{relative}: possible credential material")


def _admit_png(relative: str, content: bytes) -> None:
    """Admit bounded RGB8 PNG structure; pixel privacy still requires human review."""
    if len(content) > MAX_PNG_BYTES or not content.startswith(b"\x89PNG\r\n\x1a\n"):
        raise SourceError(f"{relative}: invalid or oversized PNG")
    offset = 8
    chunks = 0
    width = height = 0
    compressed: list[bytes] = []
    ended = False
    while offset < len(content):
        chunks += 1
        if chunks > MAX_PNG_CHUNKS or len(content) - offset < 12:
            raise SourceError(f"{relative}: truncated PNG or too many chunks")
        size = struct.unpack_from(">I", content, offset)[0]
        if size > len(content) - offset - 12:
            raise SourceError(f"{relative}: truncated PNG chunk")
        kind = content[offset + 4 : offset + 8]
        payload_end = offset + 8 + size
        payload = content[offset + 8 : payload_end]
        crc = struct.unpack_from(">I", content, payload_end)[0]
        if crc != zlib.crc32(content[offset + 4 : payload_end]):
            raise SourceError(f"{relative}: PNG checksum mismatch")
        offset = payload_end + 4
        if kind == b"IHDR":
            if chunks != 1 or size != 13:
                raise SourceError(f"{relative}: invalid PNG header order or size")
            width, height, depth, color, compression, filtering, interlace = struct.unpack(
                ">IIBBBBB", payload
            )
            if (
                not 0 < width <= MAX_PNG_DIMENSION
                or not 0 < height <= MAX_PNG_DIMENSION
                or width * height > MAX_PNG_PIXELS
                or (depth, color, compression, filtering, interlace) != (8, 2, 0, 0, 0)
            ):
                raise SourceError(f"{relative}: unsupported or oversized PNG header")
        elif kind == b"IDAT":
            if not width:
                raise SourceError(f"{relative}: PNG data precedes its header")
            compressed.append(payload)
        elif kind == b"IEND":
            if not compressed or size or offset != len(content):
                raise SourceError(f"{relative}: invalid PNG end or trailing content")
            ended = True
            break
        else:
            raise SourceError(f"{relative}: PNG metadata or unsupported chunk")
    if not ended:
        raise SourceError(f"{relative}: incomplete PNG")

    # The admitted sequence is one IHDR, consecutive IDAT chunks and one IEND.
    # Decode at most one byte beyond the exact RGB scanline size, without flush.
    stride = 1 + width * 3
    expected = height * stride
    decoder = zlib.decompressobj()
    try:
        decoded = decoder.decompress(b"".join(compressed), expected + 1)
    except zlib.error as error:
        raise SourceError(f"{relative}: invalid PNG compressed data") from error
    if (
        len(decoded) != expected
        or not decoder.eof
        or decoder.unused_data
        or decoder.unconsumed_tail
    ):
        raise SourceError(f"{relative}: invalid PNG decoded size or compressed stream boundary")
    if any(value > 4 for value in decoded[::stride]):
        raise SourceError(f"{relative}: unsupported PNG row filter")


def _admit_content(relative: str, content: bytes) -> None:
    if DOCUMENTATION_PNG.fullmatch(relative):
        _admit_png(relative, content)
    else:
        _admit_text(relative, content)


def _read_regular(root_fd: int, relative: str) -> tuple[bytes, int]:
    with _parent(root_fd, relative) as (parent_fd, name):
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise SourceError(f"{relative}: expected a regular single-link file")
        if before.st_mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
            raise SourceError(f"{relative}: special permission bits are not allowed")
        if before.st_size > MAX_FILE_BYTES:
            raise SourceError(f"{relative}: file exceeds the source size bound")
        chunks: list[bytes] = []
        remaining = MAX_FILE_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 65536))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        content = b"".join(chunks)
        after = os.fstat(descriptor)

        def identity(value: os.stat_result) -> tuple[int, ...]:
            return (
                value.st_dev,
                value.st_ino,
                value.st_size,
                value.st_mtime_ns,
                value.st_ctime_ns,
                value.st_nlink,
                value.st_mode,
            )

        if identity(before) != identity(after) or len(content) != before.st_size:
            raise SourceError(f"{relative}: file changed while being read")
        if len(content) > MAX_FILE_BYTES:
            raise SourceError(f"{relative}: file exceeds the source size bound")
        return content, stat.S_IMODE(before.st_mode)
    finally:
        os.close(descriptor)


def _git(repo: Path, *arguments: str) -> bytes:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update(
        {"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_OPTIONAL_LOCKS": "0"}
    )
    result = subprocess.run(
        ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", "-C", str(repo), *arguments],
        env=environment,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if result.returncode:
        raise SourceError("unable to read the repository's Git index")
    return result.stdout


def _index(repo: Path) -> bytes:
    return _git(repo, "ls-files", "--stage", "--full-name", "-z")


def check_source(repo: str | Path) -> SourceSnapshot:
    """Validate current bytes for every stage-zero tracked file, without writing Git."""
    root = _absolute(repo)
    with _directory(root) as root_fd:
        top = _git(root, "rev-parse", "--show-toplevel").decode("utf-8").strip()
        if _absolute(top) != root:
            raise SourceError("--repo must name the repository root")
        indexed = _index(root)
        if not indexed:
            raise SourceError("the Git index contains no source files")
        entries: list[dict[str, object]] = []
        contents: dict[str, bytes] = {}
        folded: dict[str, str] = {}
        total = 0
        records = indexed.rstrip(b"\0").split(b"\0")
        if len(records) > MAX_FILES:
            raise SourceError("too many tracked files")
        for record in records:
            try:
                header, raw_path = record.split(b"\t", 1)
                mode, _blob, stage = header.decode("ascii").split()
                relative = raw_path.decode("utf-8")
            except (ValueError, UnicodeError) as error:
                raise SourceError("malformed Git index entry") from error
            if stage != "0":
                raise SourceError("unmerged Git index entries must be resolved first")
            _admit_path(relative)
            if mode not in {"100644", "100755"}:
                raise SourceError(f"{relative}: Git links or non-file entries are forbidden")
            _admit_unique_path(relative, folded)
            content, disk_mode = _read_regular(root_fd, relative)
            expected_mode = 0o755 if mode == "100755" else 0o644
            git_mode = 0o755 if disk_mode & 0o111 else 0o644
            if git_mode != expected_mode:
                raise SourceError(f"{relative}: executable mode differs from the Git index")
            _admit_content(relative, content)
            total += len(content)
            if total > MAX_TOTAL_BYTES:
                raise SourceError("tracked content exceeds the source size bound")
            contents[relative] = content
            entries.append(
                {
                    "path": relative,
                    "sha256": _sha256(content),
                    "size": len(content),
                    "mode": f"{expected_mode:04o}",
                }
            )
        if _index(root) != indexed:
            raise SourceError("the Git index changed during source inspection")
        untracked = _git(root, "ls-files", "--others", "--exclude-standard", "-z")
    entries.sort(key=lambda entry: str(entry["path"]))
    return SourceSnapshot({"format": FORMAT, "files": entries}, contents, untracked.count(b"\0"))


def _write_new(root_fd: int, relative: str, content: bytes, mode: int) -> None:
    with _parent(root_fd, relative, create=True) as (parent_fd, name):
        descriptor = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent_fd
        )
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(descriptor)
        os.fchmod(descriptor, mode)
    finally:
        os.close(descriptor)


def _actual_files(root_fd: int, prefix: str = "") -> set[str]:
    found: set[str] = set()
    for name in os.listdir(root_fd):
        relative = f"{prefix}/{name}" if prefix else name
        info = os.stat(name, dir_fd=root_fd, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode):
            descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            try:
                children = _actual_files(descriptor, relative)
            finally:
                os.close(descriptor)
            if not children:
                raise SourceError(f"{relative}: unexpected empty directory")
            found.update(children)
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            found.add(relative)
        else:
            raise SourceError(f"{relative}: unexpected link or special entry")
    return found


def verify_export(dest: str | Path, *, expected_sha256: str | None = None) -> dict[str, object]:
    """Check the exported manifest and every file; reject undeclared extra content."""
    target = _absolute(dest)
    with _directory(target) as root_fd:
        raw, mode = _read_regular(root_fd, MANIFEST_NAME)
        try:
            manifest = json.loads(raw)
        except (ValueError, UnicodeError) as error:
            raise SourceError("invalid source manifest") from error
        if (
            not isinstance(manifest, dict)
            or set(manifest) != {"format", "files"}
            or manifest["format"] != FORMAT
        ):
            raise SourceError("unsupported source manifest")
        if mode != 0o644 or raw != _manifest_bytes(manifest):
            raise SourceError("source manifest is not canonical")
        digest = _sha256(raw)
        if expected_sha256 is not None and digest != expected_sha256:
            raise SourceError("source manifest does not match the reviewed digest")
        entries = manifest["files"]
        if not isinstance(entries, list) or not 0 < len(entries) <= MAX_FILES:
            raise SourceError("invalid source manifest file list")
        expected = {MANIFEST_NAME}
        ordered: list[str] = []
        folded: dict[str, str] = {}
        total = 0
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "size", "mode"}:
                raise SourceError("invalid source manifest entry")
            relative = entry["path"]
            if not isinstance(relative, str):
                raise SourceError("invalid source manifest path")
            _admit_path(relative)
            _admit_unique_path(relative, folded)
            if (
                type(entry["size"]) is not int
                or not 0 <= entry["size"] <= MAX_FILE_BYTES
                or not isinstance(entry["mode"], str)
                or entry["mode"] not in {"0644", "0755"}
                or not isinstance(entry["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
            ):
                raise SourceError("invalid source manifest metadata")
            content, file_mode = _read_regular(root_fd, relative)
            _admit_content(relative, content)
            if (
                len(content) != entry["size"]
                or _sha256(content) != entry["sha256"]
                or f"{file_mode:04o}" != entry["mode"]
            ):
                raise SourceError(f"{relative}: exported bytes or mode differ from the manifest")
            expected.add(relative)
            ordered.append(relative)
            total += len(content)
        if ordered != sorted(ordered) or total > MAX_TOTAL_BYTES:
            raise SourceError("noncanonical or oversized source manifest")
        if _actual_files(root_fd) != expected:
            raise SourceError("export contains undeclared files")
    return {
        "manifest_sha256": digest,
        "file_count": len(entries),
        "total_bytes": total,
        "verified": True,
    }


def export_source(repo: str | Path, dest: str | Path, *, expected_sha256: str) -> dict[str, object]:
    """Export an exact checked snapshot to a new directory outside the repository.

    An I/O failure can leave a partial new destination. It is never reported as
    verified; retain it for inspection or remove it explicitly before retrying.
    """
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise SourceError("export requires a check's manifest SHA-256")
    root, target = _absolute(repo), _absolute(dest)
    if target == root or root in target.parents or target in root.parents:
        raise SourceError("destination must be outside the repository and its ancestors")
    snapshot = check_source(root)
    if snapshot.manifest_sha256 != expected_sha256:
        raise SourceError("tracked bytes changed after review; run check again")
    with _directory(target.parent) as parent_fd:
        os.mkdir(target.name, 0o700, dir_fd=parent_fd)
        descriptor = os.open(
            target.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
        )
        try:
            for entry in snapshot.manifest["files"]:
                _write_new(
                    descriptor,
                    entry["path"],
                    snapshot.contents[entry["path"]],
                    int(entry["mode"], 8),
                )
            _write_new(descriptor, MANIFEST_NAME, _manifest_bytes(snapshot.manifest), 0o644)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    result = verify_export(target, expected_sha256=expected_sha256)
    result["untracked_files_ignored"] = snapshot.untracked_count
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="validate tracked source and print its review digest")
    check.add_argument("--repo", required=True, type=Path)
    export = commands.add_parser(
        "export", help="copy the reviewed snapshot into a new external directory"
    )
    export.add_argument("--repo", required=True, type=Path)
    export.add_argument(
        "--dest",
        required=True,
        type=Path,
        help="new directory outside the repository; parent must exist",
    )
    export.add_argument("--expect-sha256", required=True)
    verify = commands.add_parser("verify", help="verify a completed source export")
    verify.add_argument("--dest", required=True, type=Path)
    verify.add_argument("--expect-sha256")
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "check":
            result = check_source(arguments.repo).summary()
        elif arguments.command == "export":
            result = export_source(
                arguments.repo, arguments.dest, expected_sha256=arguments.expect_sha256
            )
        else:
            result = verify_export(arguments.dest, expected_sha256=arguments.expect_sha256)
    except (SourceError, OSError, UnicodeError, subprocess.TimeoutExpired, RecursionError) as error:
        print(f"public source refused: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
