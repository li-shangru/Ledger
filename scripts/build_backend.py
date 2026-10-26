"""PEP 517 builds from admitted source bytes, never a recursive working-tree copy.

Git checkouts use the source checker's stage-zero inventory. Exported trees and
sdists use their canonical SOURCE_MANIFEST.json. Undeclared files are ignored;
declared file drift is fatal. Hatch receives an immutable temporary snapshot,
and every resulting archive member is checked before the artifact is returned.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import importlib.util
import io
import json
import os
import re
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "_ledger_public_source_backend", Path(__file__).with_name("public_source.py")
)
assert _SPEC is not None and _SPEC.loader is not None
source = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = source
_SPEC.loader.exec_module(source)

SEEDS = ("ledger.toml", "merchants.toml", "categories.toml")


def source_snapshot(root: Path):
    """Read admitted Git files or manifest files without traversing other content."""
    root = source._absolute(root)
    if os.path.lexists(root / ".git"):
        return source.check_source(root)
    with source._directory(root) as descriptor:
        raw, mode = source._read_regular(descriptor, source.MANIFEST_NAME)
        try:
            manifest = json.loads(raw)
        except (ValueError, UnicodeError) as error:
            raise source.SourceError("invalid source manifest") from error
        if (
            not isinstance(manifest, dict)
            or set(manifest) != {"format", "files"}
            or manifest["format"] != source.FORMAT
            or mode != 0o644
            or raw != source._manifest_bytes(manifest)
        ):
            raise source.SourceError("unsupported or noncanonical source manifest")
        entries = manifest["files"]
        if not isinstance(entries, list) or not 0 < len(entries) <= source.MAX_FILES:
            raise source.SourceError("invalid source manifest file list")
        contents = {}
        folded = {}
        total = 0
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"path", "sha256", "size", "mode"}:
                raise source.SourceError("invalid source manifest entry")
            relative = entry["path"]
            if not isinstance(relative, str):
                raise source.SourceError("invalid source manifest path")
            source._admit_path(relative)
            source._admit_unique_path(relative, folded)
            if (
                type(entry["size"]) is not int
                or not 0 <= entry["size"] <= source.MAX_FILE_BYTES
                or entry["mode"] not in ("0644", "0755")
                or not isinstance(entry["sha256"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
            ):
                raise source.SourceError("invalid source manifest metadata")
            content, file_mode = source._read_regular(descriptor, relative)
            source._admit_content(relative, content)
            if (
                len(content) != entry["size"]
                or source._sha256(content) != entry["sha256"]
                or f"{file_mode:04o}" != entry["mode"]
            ):
                raise source.SourceError(f"{relative}: bytes or mode differ from the manifest")
            total += len(content)
            if total > source.MAX_TOTAL_BYTES:
                raise source.SourceError("source exceeds the source size bound")
            contents[relative] = content
        if list(contents) != sorted(contents):
            raise source.SourceError("noncanonical source manifest order")
    return source.SourceSnapshot(manifest, contents, 0)


def _project(snapshot) -> dict:
    required = {
        "pyproject.toml",
        "README.md",
        "LICENSE",
        "scripts/build_backend.py",
        "scripts/public_source.py",
        "src/ledger/__init__.py",
        *(f"configs/{name}" for name in SEEDS),
    }
    if not required <= snapshot.contents.keys():
        raise source.SourceError("distribution requires admitted build helpers, package and seeds")
    project = tomllib.loads(snapshot.contents["pyproject.toml"].decode())["project"]
    if project["name"] != "ledger" or not re.fullmatch(r"\d+\.\d+\.\d+", project["version"]):
        raise source.SourceError("unsupported distribution name or version")
    return project


def wheel_payload(snapshot, *, editable_root: Path | None = None) -> dict[str, bytes]:
    result = {
        path.removeprefix("src/"): content
        for path, content in snapshot.contents.items()
        if path.startswith("src/ledger/")
    }
    if editable_root is not None:
        result = {"_ledger.pth": str(editable_root / "src").encode()}
    for name in SEEDS:
        destination = f"ledger/seeds/{name}"
        if destination in result:
            raise source.SourceError("package source conflicts with bundled seeds")
        result[destination] = snapshot.contents[f"configs/{name}"]
    return result


def validate_wheel(path: Path, snapshot, *, editable_root: Path | None = None) -> None:
    """Validate the complete wheel inventory, source bytes, and RECORD hashes."""
    project = _project(snapshot)
    metadata = f"ledger-{project['version']}.dist-info"
    payload = wheel_payload(snapshot, editable_root=editable_root)
    payload[f"{metadata}/licenses/LICENSE"] = snapshot.contents["LICENSE"]
    generated = {f"{metadata}/{name}" for name in ("METADATA", "WHEEL", "RECORD")}
    if project.get("scripts") or project.get("gui-scripts") or project.get("entry-points"):
        generated.add(f"{metadata}/entry_points.txt")
    expected = set(payload) | generated
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)) or set(names) != expected:
            raise source.SourceError("wheel contains missing, duplicate or undeclared members")
        for name, content in payload.items():
            if archive.read(name) != content:
                raise source.SourceError("wheel source bytes differ from the admitted snapshot")
        records = list(csv.reader(io.StringIO(archive.read(f"{metadata}/RECORD").decode())))
        if (
            any(len(row) != 3 for row in records)
            or len(records) != len(expected)
            or {row[0] for row in records} != expected
        ):
            raise source.SourceError("wheel RECORD inventory differs from the admitted inventory")
        for name, digest, size in records:
            if name == f"{metadata}/RECORD":
                if digest or size:
                    raise source.SourceError("invalid wheel RECORD self entry")
                continue
            content = archive.read(name)
            encoded = (
                base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")
            )
            if digest != f"sha256={encoded}" or size != str(len(content)):
                raise source.SourceError("wheel RECORD does not match archive bytes")


def validate_sdist(path: Path, snapshot) -> None:
    """Validate all sdist members, including the source manifest and generated metadata."""
    project = _project(snapshot)
    prefix = f"ledger-{project['version']}/"
    payload = dict(snapshot.contents)
    payload[source.MANIFEST_NAME] = source._manifest_bytes(snapshot.manifest)
    expected = {prefix + name for name in payload} | {prefix + "PKG-INFO"}
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        if (
            len(members) != len(expected)
            or {member.name for member in members} != expected
            or any(not member.isfile() or member.mode & 0o7000 for member in members)
        ):
            raise source.SourceError("sdist contains missing, duplicate or undeclared members")
        modes = {entry["path"]: int(entry["mode"], 8) for entry in snapshot.manifest["files"]}
        modes[source.MANIFEST_NAME] = 0o644
        for name, content in payload.items():
            member = archive.getmember(prefix + name)
            handle = archive.extractfile(member)
            if handle is None or handle.read() != content or member.mode != modes[name]:
                raise source.SourceError("sdist bytes or modes differ from the admitted snapshot")


def _build(directory: str, target: str, *, editable: bool = False) -> str:
    from hatchling.builders.sdist import SdistBuilder
    from hatchling.builders.wheel import WheelBuilder

    root = Path.cwd().resolve()
    output = Path(directory).resolve()
    output.mkdir(parents=True, exist_ok=True)
    snapshot = source_snapshot(root)
    _project(snapshot)
    # The build output selects scratch placement. Nothing is staged in the checkout.
    with tempfile.TemporaryDirectory(prefix=".ledger-build-", dir=output) as temporary:
        stage = Path(temporary) / "source"
        stage.mkdir(mode=0o700)
        with source._directory(stage) as descriptor:
            for entry in snapshot.manifest["files"]:
                source._write_new(
                    descriptor,
                    entry["path"],
                    snapshot.contents[entry["path"]],
                    int(entry["mode"], 8),
                )
            source._write_new(
                descriptor, source.MANIFEST_NAME, source._manifest_bytes(snapshot.manifest), 0o644
            )
        source.verify_export(stage, expected_sha256=snapshot.manifest_sha256)
        config = tomllib.loads(snapshot.contents["pyproject.toml"].decode())
        if editable:
            config["tool"]["hatch"]["build"]["dev-mode-dirs"] = [str(root / "src")]
        builder_type = WheelBuilder if target == "wheel" else SdistBuilder
        builder = builder_type(str(stage), config=config)
        artifact = Path(
            next(
                builder.build(
                    directory=temporary, versions=["editable" if editable else "standard"]
                )
            )
        )
        if target == "wheel":
            validate_wheel(artifact, snapshot, editable_root=root if editable else None)
        else:
            validate_sdist(artifact, snapshot)
        os.replace(artifact, output / artifact.name)
        return artifact.name


def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    return _build(wheel_directory, "wheel")


def build_sdist(sdist_directory, config_settings=None):
    return _build(sdist_directory, "sdist")


def build_editable(wheel_directory, config_settings=None, metadata_directory=None):
    return _build(wheel_directory, "wheel", editable=True)
