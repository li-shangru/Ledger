"""Invalidate financial reads when an external writer changes SQLite artifacts."""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Protocol, cast

import streamlit as st


class CachedDatabaseRead[**P, R](Protocol):
    def __call__(self, *args: P.args, **kwargs: P.kwargs) -> R: ...

    clear: Callable[[], None]


def _database_revision(path: Path) -> tuple:
    # The main file alone misses committed WAL writes. Inode/ctime also catch
    # atomic publication and same-size replacements with preserved mtime.
    revisions: list[tuple[int, int, int, int, int] | None] = []
    for suffix in ("", "-wal", "-journal"):
        try:
            metadata = Path(f"{path}{suffix}").stat(follow_symlinks=False)
        except FileNotFoundError:
            revisions.append(None)
        else:
            revisions.append(
                (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                    metadata.st_ctime_ns,
                )
            )
    return tuple(revisions)


def cache_database_read[**P, R](function: Callable[P, R]) -> CachedDatabaseRead[P, R]:
    """Cache a read whose first argument is its database path, including WAL state."""

    @wraps(function)
    def read_at_revision(revision: tuple, *args, **kwargs) -> R:
        return function(*args, **kwargs)

    cached = st.cache_data(show_spinner=False, max_entries=128)(read_at_revision)

    @wraps(function)
    def read(*args: P.args, **kwargs: P.kwargs) -> R:
        path = args[0] if args else kwargs.get("db_path_str", kwargs.get("db_path"))
        revision = _database_revision(Path(str(path)).expanduser().absolute())
        return cached(revision, *args, **kwargs)

    result = cast(CachedDatabaseRead[P, R], read)
    result.clear = cached.clear
    return result
