"""Separate private financial state from source code and distributed artifacts.

Resolving defaults never creates a directory or opens a database. The filesystem
publication helpers still enforce their existing identity and permission checks.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path, PurePosixPath


class RuntimePathError(ValueError):
    """A runtime location violates the source/data separation contract."""


def _source_ancestor(path: Path) -> bool:
    for parent in (path, *path.parents):
        marker = parent / ".git"
        if marker.exists() or marker.is_symlink():
            return True
        # A downloaded source archive has no .git directory.
        if (parent / "pyproject.toml").is_file() and (
            parent / "src" / "ledger" / "__init__.py"
        ).is_file():
            return True
    return False


def require_external_runtime_path(path: Path) -> Path:
    """Reject a runtime path in a Git checkout or Ledger source archive.

    Inspect both lexical and resolved paths so relative paths, parent traversal,
    linked worktrees and symlink aliases cannot hide an in-repository destination.
    Return the lexical absolute path: callers retain their stricter no-symlink
    rules rather than accidentally approving an arbitrary resolved alias.
    """
    expanded = Path(path).expanduser()
    absolute = Path(os.path.abspath(os.fspath(expanded)))
    if _source_ancestor(absolute) or _source_ancestor(absolute.resolve()):
        raise RuntimePathError(
            "runtime_in_source_repository: keep financial state outside source repositories"
        )
    return absolute


def runtime_home() -> Path:
    """Return the private runtime root, with an optional absolute LEDGER_HOME."""
    override = os.environ.get("LEDGER_HOME")
    if override is not None:
        if not override.strip():
            raise RuntimePathError("LEDGER_HOME must be an absolute directory")
        root = Path(override).expanduser()
        if not root.is_absolute():
            raise RuntimePathError("LEDGER_HOME must be an absolute directory")
    elif sys.platform == "darwin":
        root = Path.home() / "Library" / "Application Support" / "Ledger"
    else:
        data_home = os.environ.get("XDG_DATA_HOME")
        root = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
        if not root.is_absolute():
            raise RuntimePathError("XDG_DATA_HOME must be an absolute directory")
        root /= "ledger"
    return require_external_runtime_path(root)


def runtime_path(child: str) -> Path:
    """Resolve one named runtime child without creating it."""
    relative = PurePosixPath(child)
    if not child or relative.is_absolute() or ".." in relative.parts or "\\" in child:
        raise RuntimePathError("runtime child must be a relative path without parent traversal")
    return require_external_runtime_path(runtime_home().joinpath(*relative.parts))


def default_database_path(config_file: Path | None = None) -> Path:
    """Use LEDGER_DB, an explicit/runtime defaults file, then the private home."""
    override = os.environ.get("LEDGER_DB")
    if override:
        return require_external_runtime_path(Path(override))
    from ledger.config.loader import load_cli_defaults

    defaults = load_cli_defaults(config_file)
    if defaults.db:
        return require_external_runtime_path(Path(defaults.db))
    return runtime_path("ledger.db")


def resolve_runtime_path(path: Path | None, child: str) -> Path:
    """Validate an explicit location or select a named private default."""
    if path is not None:
        return require_external_runtime_path(path)
    return default_database_path() if child == "ledger.db" else runtime_path(child)


def seed_config_path(name: str) -> Path:
    """Locate public configuration seeds in an installed wheel or source tree."""
    if name not in {"ledger.toml", "merchants.toml", "categories.toml"}:
        raise ValueError("Unknown configuration seed")
    package = Path(__file__).resolve().parent
    bundled = package / "seeds" / name
    if bundled.is_file():
        return bundled
    source = package.parent.parent / "configs" / name
    if source.is_file():
        return source
    raise FileNotFoundError("Ledger configuration seeds are not installed")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Print a private Ledger runtime path.")
    parser.add_argument("child", nargs="?", default="ledger.db")
    arguments = parser.parse_args()
    try:
        print(resolve_runtime_path(None, arguments.child))
    except RuntimePathError as exc:
        parser.exit(2, f"{exc}\n")
