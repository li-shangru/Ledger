"""Immutable interpretation receipts kept separate from financial identities."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

from ledger.adapters.contracts import AdapterError


def core_digest() -> str:
    """Conservatively bind every core Python source byte; never use unknown hashes."""
    root = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    files = sorted(root.rglob("*.py"))
    if not files:
        raise AdapterError("import_binding_invalid")
    for path in files:
        if path.is_symlink() or path.stat().st_nlink != 1:
            raise AdapterError("import_binding_invalid")
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def digest_value(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def file_binding(path: Path) -> dict[str, Any]:
    """Bind regular-file bytes without accepting a linked or changing source."""
    try:
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise AdapterError("import_binding_invalid")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise AdapterError("import_binding_invalid")
            digest = hashlib.sha256()
            size = 0
            while block := stream.read(1024 * 1024):
                digest.update(block)
                size += len(block)
            after = os.fstat(stream.fileno())
        current = path.stat(follow_symlinks=False)

        def identity(value):
            return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)

        if identity(before) != identity(after) or identity(after) != identity(current):
            raise AdapterError("import_binding_changed")
        return {"sha256": digest.hexdigest(), "size_bytes": size}
    except AdapterError:
        raise
    except OSError, ValueError:
        raise AdapterError("import_binding_invalid") from None


def configuration_bindings(paths: list[Path]) -> dict[str, Any]:
    """Missing optional configuration is an explicit input, never an omitted pin."""
    return {
        str(path): file_binding(path) if path.exists() or path.is_symlink() else None
        for path in paths
    }


def bind_import_inputs(
    *,
    csv_root: Path,
    source_files: list[Path],
    config_paths: list[Path],
    account_slugs: dict[str, str],
    interpretation: dict[str, Any] | None = None,
    configuration: dict[str, Any] | None = None,
    artifacts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from ledger.adapters.registry import snapshot_bindings

    return {
        "schema_version": 1,
        "interpretation": interpretation if interpretation is not None else snapshot_bindings(),
        "sources": {str(path.relative_to(csv_root)): file_binding(path) for path in source_files},
        "configuration": configuration
        if configuration is not None
        else configuration_bindings(config_paths),
        "account_slugs": dict(account_slugs),
        "artifacts": artifacts or {},
    }


def verify_import_inputs(
    binding: object,
    *,
    csv_root: Path,
    source_files: list[Path],
    account_slugs: dict[str, str],
    config_paths: list[Path] | None = None,
) -> None:
    from ledger.adapters.registry import verify_bindings

    if not isinstance(binding, dict) or set(binding) != {
        "schema_version",
        "interpretation",
        "sources",
        "configuration",
        "account_slugs",
        "artifacts",
    }:
        raise AdapterError("import_binding_missing")
    if binding["schema_version"] != 1 or binding["account_slugs"] != account_slugs:
        raise AdapterError("import_binding_changed")
    verify_bindings(binding["interpretation"])
    current = {str(path.relative_to(csv_root)): file_binding(path) for path in source_files}
    if current != binding["sources"]:
        raise AdapterError("import_binding_changed")
    if not isinstance(binding["configuration"], dict):
        raise AdapterError("import_binding_invalid")
    if config_paths is None:
        config_paths = [Path(filename) for filename in binding["configuration"]]
    if configuration_bindings(config_paths) != binding["configuration"]:
        raise AdapterError("import_binding_changed")
    if not isinstance(binding["artifacts"], dict):
        raise AdapterError("import_binding_invalid")
    for filename, expected in binding["artifacts"].items():
        if file_binding(Path(filename)) != expected:
            raise AdapterError("import_binding_changed")
