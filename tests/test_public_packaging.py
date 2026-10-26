"""Distribution checks use tiny authored packages and disposable local Git indexes."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "ledger_build_backend_test", ROOT / "scripts/build_backend.py"
)
assert SPEC is not None and SPEC.loader is not None
backend = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backend)


def _put(root: Path, name: str, content: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _environment(root: Path) -> dict[str, str]:
    home = root / "home"
    temporary = root / "temporary"
    home.mkdir(exist_ok=True)
    temporary.mkdir(exist_ok=True)
    environment = {
        key: os.environ[key] for key in ("PATH", "LANG", "LC_CTYPE") if key in os.environ
    }
    environment.update(
        HOME=str(home),
        TMPDIR=str(temporary),
        PYTHONDONTWRITEBYTECODE="1",
        PIP_DISABLE_PIP_VERSION_CHECK="1",
        PIP_CONFIG_FILE=os.devnull,
        PIP_CACHE_DIR=str(root / "pip-cache"),
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
    )
    return environment


def _run(root: Path, command: list[str]) -> str:
    result = subprocess.run(
        command,
        cwd=root,
        env=_environment(root),
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    repo = tmp_path.resolve() / "source"
    repo.mkdir()
    _run(repo, ["git", "init", "--quiet"])
    _put(repo, ".gitignore", "home/\ntemporary/\n__pycache__/\n")
    _put(repo, "README.md", "# Independently authored packaging fixture\n")
    _put(repo, "LICENSE", "Synthetic license text\n")
    _put(repo, "src/ledger/__init__.py", 'VALUE = "synthetic package"\n')
    _put(repo, "docs/guide.md", "Independently authored source documentation.\n")
    _put(repo, "scripts/fixture-check", "#!/bin/sh\nexit 0\n").chmod(0o755)
    for name in backend.SEEDS:
        _put(repo, f"configs/{name}", f'name = "synthetic-{name}"\n')
    for name in ("build_backend.py", "public_source.py"):
        shutil.copyfile(ROOT / "scripts" / name, repo / "scripts" / name)
    project = re.sub(
        r'(?m)^version = "[^"]+"$',
        'version = "0.0.1"',
        (ROOT / "pyproject.toml").read_text(),
        count=1,
    )
    _put(repo, "pyproject.toml", project)
    _run(repo, ["git", "add", "--all"])
    return repo


def _pip_wheel(repo: Path, output: Path) -> Path:
    output.mkdir()
    _run(
        repo,
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-index",
            "--no-deps",
            "--no-build-isolation",
            "--no-cache-dir",
            "--wheel-dir",
            str(output),
            str(repo),
        ],
    )
    wheels = list(output.glob("*.whl"))
    assert len(wheels) == 1
    return wheels[0]


def _sdist(repo: Path, output: Path, monkeypatch) -> Path:
    monkeypatch.chdir(repo)
    output.mkdir()
    return output / backend.build_sdist(str(output))


def _install_probe(repo: Path, prefix: Path, *, editable: bool = False) -> dict:
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--ignore-installed",
        "--no-index",
        "--no-deps",
        "--no-build-isolation",
        "--no-cache-dir",
        "--prefix",
        str(prefix),
    ]
    command += ["-e", str(repo)] if editable else [str(repo)]
    _run(repo, command)
    runtime = prefix.parent / (prefix.name + "-python")
    _run(repo, [sys.executable, "-m", "venv", "--without-pip", str(runtime)])
    site = (
        prefix
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    code = (
        "import json, site; site.addsitedir(" + repr(str(site)) + "); "
        "import ledger; from importlib.resources import files; "
        "print(json.dumps({'origin': ledger.__file__, 'value': ledger.VALUE, "
        "'seed': files('ledger').joinpath('seeds/ledger.toml').read_text()}))"
    )
    # Editable modules use the source's own config fallbacks, so only probe origin.
    if editable:
        code = (
            "import json, site; site.addsitedir(" + repr(str(site)) + "); "
            "import ledger; print(json.dumps({'origin': ledger.__file__, 'value': ledger.VALUE}))"
        )
    return json.loads(_run(repo, [str(runtime / "bin/python"), "-I", "-c", code]))


def test_direct_pip_wheel_excludes_untracked_package_and_docs_files(source_repo: Path):
    _put(source_repo, "src/ledger/local_notes.txt", "Unreviewed local fixture\n")
    _put(source_repo, "docs/local_notes.md", "Unreviewed local fixture\n")
    snapshot = backend.source_snapshot(source_repo)
    wheel = _pip_wheel(source_repo, source_repo.parent / "wheels")
    backend.validate_wheel(wheel, snapshot)
    with zipfile.ZipFile(wheel) as archive:
        assert not any("local_notes" in name for name in archive.namelist())
        assert "ledger/seeds/ledger.toml" in archive.namelist()


def test_sdist_is_complete_and_no_git_source_installs_with_seeds(source_repo: Path, monkeypatch):
    _put(source_repo, "src/ledger/local_notes.txt", "Unreviewed local fixture\n")
    _put(source_repo, "docs/local_notes.md", "Unreviewed local fixture\n")
    snapshot = backend.source_snapshot(source_repo)
    artifact = _sdist(source_repo, source_repo.parent / "sdists", monkeypatch)
    backend.validate_sdist(artifact, snapshot)
    unpacked = source_repo.parent / "unpacked"
    with tarfile.open(artifact) as archive:
        assert not any("local_notes" in name for name in archive.getnames())
        archive.extractall(unpacked, filter="data")
    archive_source = unpacked / "ledger-0.0.1"
    assert not (archive_source / ".git").exists()
    _put(archive_source, "src/ledger/local_notes.txt", "Unreviewed archive-local fixture\n")
    _put(archive_source, "docs/local_notes.md", "Unreviewed archive-local fixture\n")
    wheel = _pip_wheel(archive_source, source_repo.parent / "archive-wheels")
    backend.validate_wheel(wheel, snapshot)
    installed = _install_probe(archive_source, source_repo.parent / "installed")
    assert installed["value"] == "synthetic package"
    assert installed["seed"] == 'name = "synthetic-ledger.toml"\n'
    assert "site-packages/ledger/__init__.py" in installed["origin"]


def test_export_without_git_builds_and_declared_drift_is_rejected(source_repo: Path):
    snapshot = backend.source_snapshot(source_repo)
    exported = source_repo.parent / "export"
    backend.source.export_source(source_repo, exported, expected_sha256=snapshot.manifest_sha256)
    wheel = _pip_wheel(exported, source_repo.parent / "export-wheels")
    backend.validate_wheel(wheel, snapshot)
    _put(exported, "src/ledger/__init__.py", 'VALUE = "unreviewed change"\n')
    with pytest.raises(backend.source.SourceError, match="differ from the manifest"):
        backend.source_snapshot(exported)


def test_pip_editable_retains_source_origin(source_repo: Path):
    installed = _install_probe(source_repo, source_repo.parent / "editable", editable=True)
    assert Path(installed["origin"]) == source_repo / "src/ledger/__init__.py"
    assert installed["value"] == "synthetic package"


def test_no_git_or_manifest_fails_closed(source_repo: Path):
    shutil.rmtree(source_repo / ".git")
    with pytest.raises(FileNotFoundError):
        backend.source_snapshot(source_repo)


def test_untracked_backend_cannot_produce_an_unbuildable_sdist(source_repo: Path, monkeypatch):
    _run(source_repo, ["git", "rm", "--cached", "scripts/build_backend.py"])
    monkeypatch.chdir(source_repo)
    output = source_repo.parent / "sdists"
    with pytest.raises(backend.source.SourceError, match="requires admitted build helpers"):
        backend.build_sdist(str(output))
    assert not list(output.glob("*.tar.gz"))


def test_complete_wheel_inventory_rejects_nonpackage_member(source_repo: Path):
    snapshot = backend.source_snapshot(source_repo)
    wheel = _pip_wheel(source_repo, source_repo.parent / "wheels")
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr("local_notes.txt", "Unreviewed local fixture")
    with pytest.raises(backend.source.SourceError, match="undeclared members"):
        backend.validate_wheel(wheel, snapshot)


def test_complete_sdist_inventory_rejects_extra_member(source_repo: Path, monkeypatch):
    snapshot = backend.source_snapshot(source_repo)
    artifact = _sdist(source_repo, source_repo.parent / "sdists", monkeypatch)
    changed = artifact.with_name("changed.tar.gz")
    with tarfile.open(artifact) as original, tarfile.open(changed, "w:gz") as destination:
        for member in original.getmembers():
            destination.addfile(member, original.extractfile(member))
        extra = tarfile.TarInfo("ledger-0.0.1/docs/local_notes.md")
        destination.addfile(extra)
    with pytest.raises(backend.source.SourceError, match="undeclared members"):
        backend.validate_sdist(changed, snapshot)
