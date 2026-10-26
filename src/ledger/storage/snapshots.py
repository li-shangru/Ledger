"""SQLite-consistent owner-only snapshot publication."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from ledger.utils.private_files import (
    PinnedPrivateDirectory,
    PinnedPrivateFile,
    ensure_private_dir,
    pin_existing_directory,
    pin_private_directory,
    private_atomic_binary,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX host
    fcntl = None  # type: ignore[assignment]

_SQLITE_HEADER = b"SQLite format 3\x00"
_SQLITE_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")
_ALIAS_PREFIX = ".ledger-sqlite-alias-"
_ALIAS_METADATA = ".identity.json"
_ALIAS_LEASE = ".lease"
_ALIAS_PENDING_WAL = ".local-wal"
_ALIAS_PENDING_SHM = ".local-shm"
_ALIAS_PARENT_LOCK = ".ledger-sqlite-alias.lock"
_ALIAS_MAX_METADATA = 16 * 1024
_ALIAS_MAX_ENTRIES = 256


class DatabaseTargetLockError(RuntimeError):
    """Raised when another operation already owns a database publication lock."""


class DatabaseWriteCommittedError(OSError):
    """A SQLite write committed, but its final namespace validation failed."""


@dataclass
class DatabaseWriteOutcome:
    """Track when a semantic write has committed below surrounding boundaries."""

    committed: bool = False

    def mark_committed(self) -> None:
        """Record the irreversible boundary immediately after SQLite commit returns."""
        self.committed = True


@contextmanager
def database_write_outcome() -> Iterator[DatabaseWriteOutcome]:
    """Preserve committed truth through later validation or cleanup failures."""
    outcome = DatabaseWriteOutcome()
    try:
        yield outcome
    except BaseException as exc:
        if outcome.committed and not isinstance(exc, DatabaseWriteCommittedError):
            raise DatabaseWriteCommittedError(
                "SQLite write commit succeeded, but final validation or cleanup did not complete"
            ) from exc
        raise


class DatabasePublicationDurabilityError(OSError):
    """Raised when a fresh database name cannot be made durably recoverable."""

    def __init__(
        self,
        message: str,
        *,
        target_durable: bool,
        staged_recovery: Path | None,
        rollback_error: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.target_durable = target_durable
        self.staged_recovery = staged_recovery
        self.rollback_error = rollback_error

    @property
    def preserve_staged(self) -> bool:
        """Return whether the staged pathname is still a required recovery name."""
        return self.staged_recovery is not None


def cleanup_database_artifacts(path: Path) -> None:
    """Remove a database path and any SQLite sidecars without following links."""
    for candidate in (path, *(Path(f"{path}{suffix}") for suffix in _SQLITE_SIDECAR_SUFFIXES)):
        with contextlib.suppress(OSError):
            candidate.unlink()


def has_sqlite_header(path: Path) -> bool:
    """Return whether *path* is an initialized SQLite database file."""
    try:
        if not path.is_file():
            return False
        with path.open("rb") as handle:
            return handle.read(len(_SQLITE_HEADER)) == _SQLITE_HEADER
    except OSError:
        return False


def _require_sqlite_header(path: Path, *, label: str) -> None:
    if not has_sqlite_header(path):
        raise sqlite3.DatabaseError(f"{label} does not have a valid SQLite database header: {path}")


def _require_integrity(connection: sqlite3.Connection, *, label: str) -> None:
    integrity_rows = connection.execute("PRAGMA integrity_check").fetchall()
    if not integrity_rows or any(str(row[0]).strip().lower() != "ok" for row in integrity_rows):
        raise sqlite3.DatabaseError(f"{label} failed integrity_check")


def _create_private_empty_file(path: Path) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
    except BaseException:
        with contextlib.suppress(BaseException):
            os.close(descriptor)
        with contextlib.suppress(BaseException):
            path.unlink()
        raise
    else:
        os.close(descriptor)


def _absolute_lexical(path: Path) -> Path:
    path = path.expanduser()
    return path if path.is_absolute() else Path.cwd() / path


@contextmanager
def database_target_lock(target: Path) -> Iterator[Path]:
    """Acquire the private nonblocking lock shared by database publishers."""
    target = _absolute_lexical(Path(target))
    _reject_path_symlinks(target)
    ensure_private_dir(target.parent)
    # Keep the established filename so upgraded import/repair publishers also
    # coordinate with an older rebuild process that is still running.
    lock_path = target.parent / f".{target.name}.rebuild.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    if os.name == "posix":
        os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "r+") as handle:
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DatabaseTargetLockError(
                    f"A database publication is already in progress for {target.name}"
                )
        try:
            yield lock_path
        finally:
            if fcntl is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _reject_path_symlinks(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise OSError(f"Database path contains a symlink: {current}")


@dataclass
class _PinnedDatabaseFile:
    path: Path
    parent_descriptor: int
    parent_identity: tuple[int, int]
    descriptor: int
    file_identity: tuple[int, int]

    def validate(self) -> None:
        _reject_path_symlinks(self.path)
        current_parent = self.path.parent.stat(follow_symlinks=False)
        if (current_parent.st_dev, current_parent.st_ino) != self.parent_identity:
            raise OSError(
                f"Database destination parent changed during operation: {self.path.parent}"
            )
        current_file = os.stat(
            self.path.name,
            dir_fd=self.parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(current_file.st_mode)
            or (
                current_file.st_dev,
                current_file.st_ino,
            )
            != self.file_identity
        ):
            raise OSError(f"Database destination changed during operation: {self.path}")

    def require_sqlite_header(self, *, label: str) -> None:
        if os.pread(self.descriptor, len(_SQLITE_HEADER), 0) != _SQLITE_HEADER:
            raise sqlite3.DatabaseError(
                f"{label} does not have a valid SQLite database header: {self.path}"
            )


@contextmanager
def _borrow_or_pin_database_file(
    source: Path | PinnedPrivateFile,
) -> Iterator[_PinnedDatabaseFile]:
    """Borrow an existing private pin or create one exactly once.

    Recovery already owns descriptor-pinned run/target files. Turning those pins back
    into pathnames and repinning creates a second race boundary, so callers may pass the
    live pin all the way through to the SQLite alias reader.
    """
    if isinstance(source, PinnedPrivateFile):
        source.validate()
        metadata = os.fstat(source.descriptor)
        borrowed = _PinnedDatabaseFile(
            path=source.path,
            parent_descriptor=source.parent.descriptor,
            parent_identity=source.parent.identity,
            descriptor=source.descriptor,
            file_identity=(metadata.st_dev, metadata.st_ino),
        )
        borrowed.validate()
        yield borrowed
        borrowed.validate()
        return
    with _pin_database_file(Path(source), writable=False, private_parent=False) as pinned:
        yield pinned


@dataclass
class _PinnedSQLiteSidecar:
    """A regular owner-only SQLite sidecar pinned below a database parent."""

    name: str
    descriptor: int
    identity: tuple[int, int]

    def validate(self, parent_descriptor: int) -> None:
        current = os.stat(self.name, dir_fd=parent_descriptor, follow_symlinks=False)
        pinned = os.fstat(self.descriptor)
        if (
            not stat.S_ISREG(current.st_mode)
            or not stat.S_ISREG(pinned.st_mode)
            or stat.S_IMODE(current.st_mode) != 0o600
            or stat.S_IMODE(pinned.st_mode) != 0o600
            or (current.st_dev, current.st_ino) != self.identity
            or (pinned.st_dev, pinned.st_ino) != self.identity
        ):
            raise OSError("SQLite sidecar changed during the bound read")


@contextmanager
def _pin_database_file(
    path: Path,
    *,
    writable: bool,
    private_parent: bool,
) -> Iterator[_PinnedDatabaseFile]:
    path = _absolute_lexical(Path(path))
    _reject_path_symlinks(path)
    if path.is_symlink():
        raise OSError(f"Database path is a symlink: {path}")
    expected_parent = path.parent.stat(follow_symlinks=False)
    try:
        expected_file = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        raise FileNotFoundError(f"Database not found: {path}") from None
    if not stat.S_ISREG(expected_file.st_mode):
        raise ValueError(f"Database path is not a regular file: {path}")
    parent = ensure_private_dir(path.parent) if private_parent else path.parent
    path = parent / path.name
    secured_parent = parent.stat(follow_symlinks=False)
    if (secured_parent.st_dev, secured_parent.st_ino) != (
        expected_parent.st_dev,
        expected_parent.st_ino,
    ):
        raise OSError(f"Database destination parent changed during operation: {parent}")
    parent_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        parent_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        parent_flags |= os.O_NOFOLLOW
    parent_descriptor = os.open(parent, parent_flags)
    descriptor = -1
    try:
        pinned_parent = os.fstat(parent_descriptor)
        parent_identity = (expected_parent.st_dev, expected_parent.st_ino)
        if (pinned_parent.st_dev, pinned_parent.st_ino) != parent_identity:
            raise OSError(f"Database destination parent changed during operation: {parent}")
        file_flags = os.O_RDWR if writable else os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            file_flags |= os.O_NOFOLLOW
        if hasattr(os, "O_NONBLOCK"):
            file_flags |= os.O_NONBLOCK
        descriptor = os.open(path.name, file_flags, dir_fd=parent_descriptor)
        file_metadata = os.fstat(descriptor)
        if not stat.S_ISREG(file_metadata.st_mode) or (
            file_metadata.st_dev,
            file_metadata.st_ino,
        ) != (expected_file.st_dev, expected_file.st_ino):
            raise OSError(f"Database destination changed during operation: {path}")
        pinned = _PinnedDatabaseFile(
            path=path,
            parent_descriptor=parent_descriptor,
            parent_identity=parent_identity,
            descriptor=descriptor,
            file_identity=(file_metadata.st_dev, file_metadata.st_ino),
        )
        pinned.validate()
        yield pinned
    finally:
        if descriptor >= 0:
            with contextlib.suppress(BaseException):
                os.close(descriptor)
        with contextlib.suppress(BaseException):
            os.close(parent_descriptor)


def _sidecar_metadata(parent_descriptor: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _pin_sqlite_sidecar(
    parent_descriptor: int,
    name: str,
) -> _PinnedSQLiteSidecar | None:
    expected = _sidecar_metadata(parent_descriptor, name)
    if expected is None:
        return None
    if not stat.S_ISREG(expected.st_mode) or stat.S_IMODE(expected.st_mode) != 0o600:
        raise OSError("SQLite sidecar is not a regular owner-only file")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    descriptor = os.open(name, flags, dir_fd=parent_descriptor)
    try:
        metadata = os.fstat(descriptor)
        identity = (expected.st_dev, expected.st_ino)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or (metadata.st_dev, metadata.st_ino) != identity
        ):
            raise OSError("SQLite sidecar changed while it was pinned")
        return _PinnedSQLiteSidecar(name=name, descriptor=descriptor, identity=identity)
    except BaseException:
        os.close(descriptor)
        raise


def _validate_bound_source_namespace(
    pinned_main: _PinnedDatabaseFile,
    sidecars: dict[str, _PinnedSQLiteSidecar | None],
) -> None:
    pinned_main.validate()
    journal_name = f"{pinned_main.path.name}-journal"
    if _sidecar_metadata(pinned_main.parent_descriptor, journal_name) is not None:
        raise OSError("Rollback-journal SQLite state cannot be read as a bound WAL snapshot")
    for suffix in ("-wal", "-shm"):
        name = f"{pinned_main.path.name}{suffix}"
        pinned = sidecars[suffix]
        current = _sidecar_metadata(pinned_main.parent_descriptor, name)
        if pinned is None:
            if current is not None:
                raise OSError("SQLite sidecar presence changed during the bound read")
        else:
            pinned.validate(pinned_main.parent_descriptor)
    if sidecars["-shm"] is not None and sidecars["-wal"] is None:
        raise OSError("SQLite shared-memory state exists without its WAL")


def _validate_bound_writer_namespace(
    pinned_main: _PinnedDatabaseFile,
    sidecars: dict[str, _PinnedSQLiteSidecar | None],
) -> None:
    """Validate a writer namespace while allowing its active rollback journal."""
    pinned_main.validate()
    journal = _sidecar_metadata(
        pinned_main.parent_descriptor,
        f"{pinned_main.path.name}-journal",
    )
    if journal is not None and (
        not stat.S_ISREG(journal.st_mode) or stat.S_IMODE(journal.st_mode) != 0o600
    ):
        raise OSError("SQLite writer journal is not an owner-only regular file")
    for suffix in ("-wal", "-shm"):
        name = f"{pinned_main.path.name}{suffix}"
        pinned = sidecars[suffix]
        current = _sidecar_metadata(pinned_main.parent_descriptor, name)
        if pinned is None:
            if current is not None:
                raise OSError("SQLite sidecar presence changed during the write")
        else:
            pinned.validate(pinned_main.parent_descriptor)
    if sidecars["-shm"] is not None and sidecars["-wal"] is None:
        raise OSError("SQLite shared-memory state exists without its WAL")


def _require_bound_source_modes(
    pinned_main: _PinnedDatabaseFile,
    *,
    require_main_owner_only: bool,
    require_parent_owner_only: bool,
) -> None:
    """Validate descriptor and namespace modes without changing the source."""
    pinned_main.validate()
    parent_descriptor_mode = stat.S_IMODE(os.fstat(pinned_main.parent_descriptor).st_mode)
    parent_namespace_mode = stat.S_IMODE(
        pinned_main.path.parent.stat(follow_symlinks=False).st_mode
    )
    main_descriptor_mode = stat.S_IMODE(os.fstat(pinned_main.descriptor).st_mode)
    main_namespace_mode = stat.S_IMODE(
        os.stat(
            pinned_main.path.name,
            dir_fd=pinned_main.parent_descriptor,
            follow_symlinks=False,
        ).st_mode
    )
    if require_parent_owner_only and (
        parent_descriptor_mode != 0o700 or parent_namespace_mode != 0o700
    ):
        raise PermissionError("SQLite source directory is not owner-only")
    if require_main_owner_only and (main_descriptor_mode != 0o600 or main_namespace_mode != 0o600):
        raise PermissionError("SQLite source file is not owner-only")


def _require_alias_mode(
    pinned_main: _PinnedDatabaseFile,
    alias_name: str,
    alias_descriptor: int,
    *,
    expected_mode: int,
) -> None:
    descriptor_metadata = os.fstat(alias_descriptor)
    namespace_metadata = os.stat(
        alias_name,
        dir_fd=pinned_main.parent_descriptor,
        follow_symlinks=False,
    )
    if (
        not stat.S_ISDIR(descriptor_metadata.st_mode)
        or not stat.S_ISDIR(namespace_metadata.st_mode)
        or (descriptor_metadata.st_dev, descriptor_metadata.st_ino)
        != (namespace_metadata.st_dev, namespace_metadata.st_ino)
        or stat.S_IMODE(descriptor_metadata.st_mode) != expected_mode
        or stat.S_IMODE(namespace_metadata.st_mode) != expected_mode
    ):
        raise PermissionError("SQLite alias directory permissions changed")


def _open_fd_snapshot() -> dict[int, tuple[int, int, str]]:
    """Return open regular-file identities for connect-time descriptor auditing."""
    fd_root = Path("/dev/fd") if sys.platform == "darwin" else Path("/proc/self/fd")
    if not fd_root.is_dir():
        raise OSError("SQLite descriptor auditing is unsupported on this platform")
    result: dict[int, tuple[int, int, str]] = {}
    for raw_fd in os.listdir(fd_root):
        try:
            descriptor = int(raw_fd)
            metadata = os.fstat(descriptor)
        except OSError, ValueError:
            continue
        if stat.S_ISREG(metadata.st_mode):
            try:
                path = _fd_path(descriptor)
            except OSError:
                path = ""
            result[descriptor] = (metadata.st_dev, metadata.st_ino, path)
    return result


def _fd_path(descriptor: int) -> str:
    if sys.platform == "darwin":
        if fcntl is None:
            raise OSError("SQLite descriptor auditing is unsupported on this platform")
        raw = fcntl.fcntl(descriptor, 50, b"\0" * 1024)
        return raw.split(b"\0", 1)[0].decode("utf-8")
    if sys.platform.startswith("linux"):
        return os.readlink(f"/proc/self/fd/{descriptor}")
    raise OSError("SQLite descriptor auditing is unsupported on this platform")


def _audit_sqlite_opened_files(
    before: dict[int, tuple[int, int, str]],
    *,
    expected: dict[str, tuple[int, int]],
) -> None:
    """Prove SQLite itself opened the intended main, WAL, and SHM pinned inodes."""
    after = _open_fd_snapshot()
    expected_main = expected["source.db"]
    expected_wal = expected.get("source.db-wal")
    for descriptor, observed in after.items():
        previous = before.get(descriptor)
        if previous is not None and previous[:2] == observed[:2]:
            # Darwin F_GETPATH may change from empty to a surviving canonical
            # hard-link name after an alias is unlinked. The still-open fd has
            # not been reused unless its device/inode identity changed.
            continue
        identity = observed[:2]
        try:
            header = os.pread(descriptor, len(_SQLITE_HEADER), 0)
        except OSError:
            continue
        if header == _SQLITE_HEADER and identity != expected_main:
            raise OSError("SQLite opened an unbound database inode")
        if header[:4] in {b"\x37\x7f\x06\x82", b"\x37\x7f\x06\x83"} and identity != expected_wal:
            raise OSError("SQLite opened an unbound WAL inode")
    for filename, identity in expected.items():
        matches: list[tuple[int, str]] = []
        new_matches: list[tuple[int, str]] = []
        for descriptor, observed in after.items():
            observed_identity = observed[:2]
            path = observed[2]
            if observed_identity != identity:
                continue
            matches.append((descriptor, path))
            previous = before.get(descriptor)
            if previous is None or previous[:2] != observed[:2]:
                new_matches.append((descriptor, path))
        if filename == "source.db" and expected_wal is not None and not new_matches:
            # Darwin's unix VFS may reuse an existing main handle when another
            # connection holds the same WAL database. In that case the newly opened,
            # alias-named WAL descriptor below binds the shared main view to this
            # exact inode. Empty paths remain the observed connect-time ABA signal.
            if not matches or any(not path for _, path in matches):
                raise OSError("SQLite did not open the exact bound source inode")
            continue
        if not new_matches or any(not path for _, path in new_matches):
            raise OSError("SQLite did not open the exact bound source inode")


def _require_exact_changed_regular_file(
    before: dict[int, tuple[int, int, str]],
    *,
    expected_identity: tuple[int, int],
    label: str,
) -> None:
    """Require one connect-time regular fd with an attributable pathname."""
    after = _open_fd_snapshot()
    changed = [
        observed
        for descriptor, observed in after.items()
        if descriptor not in before or before[descriptor][:2] != observed[:2]
    ]
    if len(changed) != 1:
        raise OSError(f"SQLite did not open exactly one {label} file")
    observed = changed[0]
    if observed[:2] != expected_identity:
        raise OSError(f"SQLite opened an unbound {label} inode")
    if not observed[2]:
        raise OSError(f"SQLite opened an unattributed {label} inode")


def _require_rollback_journal_header(
    pinned_main: _PinnedDatabaseFile,
    *,
    label: str,
) -> None:
    """Require SQLite header read/write versions 1/1 before a DELETE-mode write."""
    if os.pread(pinned_main.descriptor, 2, 18) != b"\x01\x01":
        raise OSError(f"{label} is not in rollback-journal format")


def _create_pinned_writer_journal(
    pinned_main: _PinnedDatabaseFile,
) -> _PinnedSQLiteSidecar:
    """Create and durably pin the one canonical owner-only journal name."""
    name = f"{pinned_main.path.name}-journal"
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    descriptor = os.open(name, flags, 0o600, dir_fd=pinned_main.parent_descriptor)
    try:
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        identity = (metadata.st_dev, metadata.st_ino)
        pinned = _PinnedSQLiteSidecar(
            name=name,
            descriptor=descriptor,
            identity=identity,
        )
        pinned.validate(pinned_main.parent_descriptor)
        os.fsync(pinned_main.parent_descriptor)
        return pinned
    except BaseException:
        with contextlib.suppress(OSError):
            current = os.stat(
                name,
                dir_fd=pinned_main.parent_descriptor,
                follow_symlinks=False,
            )
            pinned_metadata = os.fstat(descriptor)
            if (current.st_dev, current.st_ino) == (
                pinned_metadata.st_dev,
                pinned_metadata.st_ino,
            ):
                os.unlink(name, dir_fd=pinned_main.parent_descriptor)
                os.fsync(pinned_main.parent_descriptor)
        os.close(descriptor)
        raise


def _unlink_unused_writer_journal(
    pinned_main: _PinnedDatabaseFile,
    journal: _PinnedSQLiteSidecar,
) -> None:
    """Remove only a known empty journal before any transaction was attempted."""
    try:
        journal.validate(pinned_main.parent_descriptor)
    except FileNotFoundError:
        metadata = os.fstat(journal.descriptor)
        if metadata.st_size == 0 and metadata.st_nlink == 0:
            return
        raise
    metadata = os.fstat(journal.descriptor)
    if metadata.st_size != 0:
        raise OSError("SQLite unused writer journal unexpectedly contains data")
    if metadata.st_nlink != 1:
        raise OSError("SQLite unused writer journal link count changed")
    os.unlink(journal.name, dir_fd=pinned_main.parent_descriptor)
    os.fsync(pinned_main.parent_descriptor)
    if os.fstat(journal.descriptor).st_nlink != 0:
        raise OSError("SQLite unused writer journal remained linked")


def _require_single_writer_journal_link(
    pinned_main: _PinnedDatabaseFile,
    journal: _PinnedSQLiteSidecar,
) -> None:
    """Require the active canonical journal to have exactly one namespace link."""
    journal.validate(pinned_main.parent_descriptor)
    current = os.stat(
        journal.name,
        dir_fd=pinned_main.parent_descriptor,
        follow_symlinks=False,
    )
    pinned = os.fstat(journal.descriptor)
    if current.st_nlink != 1 or pinned.st_nlink != 1:
        raise OSError("SQLite writer journal link count changed")


def _validate_detached_writer_journal(journal: _PinnedSQLiteSidecar) -> None:
    """Validate the pinned journal fd after DELETE-mode commit removed its name."""
    metadata = os.fstat(journal.descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or (metadata.st_dev, metadata.st_ino) != journal.identity
        or metadata.st_nlink != 0
    ):
        raise OSError("SQLite writer journal descriptor changed")


def _recover_existing_writer_journal(
    pinned_main: _PinnedDatabaseFile,
    journal: _PinnedSQLiteSidecar,
    connection: sqlite3.Connection,
    *,
    label: str,
) -> bool:
    """Recover a hot journal; return whether SQLite left an inactive one to reuse."""
    _require_single_writer_journal_link(pinned_main, journal)
    if os.pread(journal.descriptor, 8, 0) not in {
        bytes(8),
        bytes.fromhex("d9d505f920a163d7"),
    }:
        raise OSError("SQLite recovery journal has an unrecognized header")
    connection.execute("PRAGMA schema_version").fetchone()
    _require_integrity(connection, label=label)
    current = _sidecar_metadata(pinned_main.parent_descriptor, journal.name)
    if current is not None:
        if (current.st_dev, current.st_ino) != journal.identity:
            raise OSError("SQLite recovery journal identity changed")
        _require_single_writer_journal_link(pinned_main, journal)
        if os.pread(journal.descriptor, 8, 0) == bytes(8):
            # A crash before the first journal sync leaves pages behind an
            # inactive header. SQLite correctly leaves it on a read. Retain the
            # exact pin so the next SQLite transaction can reuse and consume it;
            # never unlink it ourselves or guess from its size alone.
            return True
        raise OSError(
            "SQLite canonical rollback recovery did not complete; validate the database "
            "before retrying"
        )
    _validate_detached_writer_journal(journal)
    pinned_main.validate()
    return False


def _write_alias_file(
    directory_descriptor: int,
    name: str,
    content: bytes,
) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(name, flags, 0o600, dir_fd=directory_descriptor)
    try:
        os.write(descriptor, content)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _create_pinned_alias_file(
    directory_descriptor: int,
    name: str,
) -> _PinnedSQLiteSidecar:
    """Create, fsync, and retain one owner-only alias-local regular file."""
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    descriptor = os.open(name, flags, 0o600, dir_fd=directory_descriptor)
    try:
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        pinned = _PinnedSQLiteSidecar(
            name=name,
            descriptor=descriptor,
            identity=(metadata.st_dev, metadata.st_ino),
        )
        pinned.validate(directory_descriptor)
        os.fsync(directory_descriptor)
        return pinned
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(descriptor)
        raise


def _validate_alias_namespace(
    directory_descriptor: int,
    expected: dict[str, tuple[int, int]],
    *,
    expected_modes: dict[str, int] | None = None,
) -> None:
    """Revalidate every fully materialized alias identity and owner-only mode."""
    expected_modes = expected_modes or {}
    for name, identity in expected.items():
        metadata = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != expected_modes.get(name, 0o600)
            or (metadata.st_dev, metadata.st_ino) != identity
        ):
            raise OSError("SQLite alias artifact identity changed")


def _read_alias_metadata(directory_descriptor: int) -> dict[str, object] | None:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    try:
        descriptor = os.open(_ALIAS_METADATA, flags, dir_fd=directory_descriptor)
    except FileNotFoundError:
        return None
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size > _ALIAS_MAX_METADATA
        ):
            raise OSError("SQLite alias metadata is invalid")
        raw = os.read(descriptor, _ALIAS_MAX_METADATA + 1)
        if len(raw) > _ALIAS_MAX_METADATA:
            raise OSError("SQLite alias metadata is invalid")
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise OSError("SQLite alias metadata is invalid")
        return parsed
    finally:
        os.close(descriptor)


def _alias_artifact_names() -> tuple[str, ...]:
    return (
        "source.db-journal",
        "source.db-wal",
        "source.db-shm",
        "source.db",
        _ALIAS_PENDING_WAL,
        _ALIAS_PENDING_SHM,
    )


def _remove_alias_artifacts(
    directory_descriptor: int,
    expected: dict[str, tuple[int, int]],
    *,
    expected_modes: dict[str, int] | None = None,
) -> list[BaseException]:
    """Attempt every sensitive unlink and report errors after the full pass."""
    expected_modes = expected_modes or {}
    errors: list[BaseException] = []
    for name in _alias_artifact_names():
        try:
            metadata = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            continue
        try:
            if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(
                metadata.st_mode
            ) != expected_modes.get(name, 0o600):
                raise OSError("SQLite alias artifact is not an owner-only regular file")
            expected_identity = expected.get(name)
            if expected_identity is None:
                raise OSError("SQLite alias provenance is unavailable")
            if (metadata.st_dev, metadata.st_ino) != expected_identity:
                raise OSError("SQLite alias artifact identity changed")
            os.unlink(name, dir_fd=directory_descriptor)
        except BaseException as exc:
            errors.append(exc)
    with contextlib.suppress(OSError):
        os.fsync(directory_descriptor)
    return errors


def _remove_alias_control_files(directory_descriptor: int) -> None:
    for name in (_ALIAS_METADATA, _ALIAS_LEASE):
        try:
            metadata = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise OSError("SQLite alias control file is invalid")
        os.unlink(name, dir_fd=directory_descriptor)
    os.fsync(directory_descriptor)


def _alias_artifacts_remain(directory_descriptor: int) -> bool:
    return any(
        _sidecar_metadata(directory_descriptor, name) is not None
        for name in _alias_artifact_names()
    )


def _remove_metadata_less_alias_setup(directory_descriptor: int) -> None:
    """Remove only bounded empty setup controls created before provenance exists."""
    entries = set(os.listdir(directory_descriptor))
    allowed = {_ALIAS_LEASE, _ALIAS_PENDING_WAL, _ALIAS_PENDING_SHM}
    if not entries <= allowed:
        raise OSError("SQLite alias provenance is unavailable")
    for name in (_ALIAS_PENDING_WAL, _ALIAS_PENDING_SHM):
        metadata = _sidecar_metadata(directory_descriptor, name)
        if metadata is None:
            continue
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size != 0
            or metadata.st_nlink != 1
        ):
            raise OSError("SQLite alias provenance is unavailable")
        os.unlink(name, dir_fd=directory_descriptor)
    _remove_alias_control_files(directory_descriptor)


def _acquire_alias_parent_lock(pinned_main: _PinnedDatabaseFile) -> int:
    """Serialize alias census/setup/audit/cleanup within one pinned DB parent."""
    if fcntl is None:
        raise OSError("SQLite alias locking is unsupported on this platform")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if hasattr(os, "O_NONBLOCK"):
        flags |= os.O_NONBLOCK
    descriptor = os.open(
        _ALIAS_PARENT_LOCK,
        flags,
        0o600,
        dir_fd=pinned_main.parent_descriptor,
    )
    try:
        pinned = os.fstat(descriptor)
        current = os.stat(
            _ALIAS_PARENT_LOCK,
            dir_fd=pinned_main.parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(pinned.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or stat.S_IMODE(pinned.st_mode) != 0o600
            or stat.S_IMODE(current.st_mode) != 0o600
            or (pinned.st_dev, pinned.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise OSError("SQLite alias lock is invalid")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        pinned_main.validate()
        current = os.stat(
            _ALIAS_PARENT_LOCK,
            dir_fd=pinned_main.parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(current.st_mode)
            or stat.S_IMODE(current.st_mode) != 0o600
            or (pinned.st_dev, pinned.st_ino) != (current.st_dev, current.st_ino)
        ):
            raise OSError("SQLite alias lock changed")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _release_alias_parent_lock(descriptor: int) -> None:
    if descriptor < 0:
        return
    if fcntl is not None:
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    os.close(descriptor)


def _reap_stale_aliases(pinned_main: _PinnedDatabaseFile) -> None:
    """Identity-check and remove aliases whose setup lease owner no longer exists."""
    if fcntl is None:
        raise OSError("SQLite alias recovery is unsupported on this platform")
    alias_names = sorted(
        name for name in os.listdir(pinned_main.parent_descriptor) if name.startswith(_ALIAS_PREFIX)
    )
    if len(alias_names) > _ALIAS_MAX_ENTRIES:
        raise OSError("SQLite source has too many SQLite alias entries")
    for alias_name in alias_names:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        alias_descriptor = os.open(alias_name, flags, dir_fd=pinned_main.parent_descriptor)
        lease_descriptor = -1
        try:
            alias_metadata = os.fstat(alias_descriptor)
            if not stat.S_ISDIR(alias_metadata.st_mode) or stat.S_IMODE(
                alias_metadata.st_mode
            ) not in {0o500, 0o700}:
                raise OSError("SQLite alias directory is invalid")
            parsed = _read_alias_metadata(alias_descriptor)
            if parsed is None:
                _remove_metadata_less_alias_setup(alias_descriptor)
            else:
                identities = parsed.get("identities")
                modes = parsed.get("modes", {})
                if parsed.get("version") != 1 or not isinstance(identities, dict) or not identities:
                    raise OSError("SQLite alias provenance is unavailable")
                if not isinstance(modes, dict):
                    raise OSError("SQLite alias provenance is unavailable")
                lease_flags = os.O_RDWR
                if hasattr(os, "O_NOFOLLOW"):
                    lease_flags |= os.O_NOFOLLOW
                if hasattr(os, "O_NONBLOCK"):
                    lease_flags |= os.O_NONBLOCK
                lease_descriptor = os.open(_ALIAS_LEASE, lease_flags, dir_fd=alias_descriptor)
                lease_metadata = os.fstat(lease_descriptor)
                if (
                    not stat.S_ISREG(lease_metadata.st_mode)
                    or stat.S_IMODE(lease_metadata.st_mode) != 0o600
                ):
                    raise OSError("SQLite alias lease is invalid")
                try:
                    fcntl.flock(lease_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                os.fchmod(alias_descriptor, 0o700)
                expected: dict[str, tuple[int, int]] = {}
                expected_modes: dict[str, int] = {}
                for name, value in identities.items():
                    if (
                        not isinstance(name, str)
                        or name not in _alias_artifact_names()
                        or not isinstance(value, list)
                        or len(value) != 2
                        or not all(isinstance(item, int) for item in value)
                    ):
                        raise OSError("SQLite alias provenance is unavailable")
                    expected[name] = (value[0], value[1])
                for name, value in modes.items():
                    if (
                        not isinstance(name, str)
                        or name not in expected
                        or not isinstance(value, int)
                        or value < 0
                        or value > 0o777
                    ):
                        raise OSError("SQLite alias provenance is unavailable")
                    expected_modes[name] = value
                errors = _remove_alias_artifacts(
                    alias_descriptor,
                    expected,
                    expected_modes=expected_modes,
                )
                if errors or _alias_artifacts_remain(alias_descriptor):
                    raise OSError("SQLite alias cleanup failed")
                _remove_alias_control_files(alias_descriptor)
            if os.listdir(alias_descriptor):
                raise OSError("SQLite alias directory contains unexpected files")
        finally:
            if lease_descriptor >= 0:
                with contextlib.suppress(OSError):
                    os.close(lease_descriptor)
            os.close(alias_descriptor)
        os.rmdir(alias_name, dir_fd=pinned_main.parent_descriptor)
        os.fsync(pinned_main.parent_descriptor)


@contextmanager
def bound_database_read_connection(
    source: Path | PinnedPrivateFile,
    *,
    label: str,
    require_main_owner_only: bool,
    require_parent_owner_only: bool,
) -> Iterator[sqlite3.Connection]:
    """Open SQLite on hard links to the exact pinned main/WAL/SHM inodes.

    SQLite derives sidecar names from the pathname it opens. A descriptor pin followed
    by a lexical reopen is therefore not an identity boundary: a rename ABA during
    ``sqlite3.connect`` can bind the connection to a different inode and restore the
    checked name before validation. This helper links the validated inodes into a 0700
    alias directory on the same filesystem, verifies each alias identity, and lets
    SQLite open only that private namespace. Existing SHM is linked too so readers share
    the source WAL-index locks; absent WAL-format sidecars are pre-created and pinned only
    beside the alias. Source sidecar presence and identities are checked throughout.
    """
    with _borrow_or_pin_database_file(source) as pinned_main:
        pinned_main.require_sqlite_header(label=label)
        _require_bound_source_modes(
            pinned_main,
            require_main_owner_only=require_main_owner_only,
            require_parent_owner_only=require_parent_owner_only,
        )

        sidecars: dict[str, _PinnedSQLiteSidecar | None] = {}
        alias_lock_descriptor = -1
        alias_parent_descriptor = -1
        lease_descriptor = -1
        alias_local_sidecars: dict[str, _PinnedSQLiteSidecar] = {}
        connection: sqlite3.Connection | None = None
        alias_dir: Path | None = None
        expected_aliases: dict[str, tuple[int, int]] = {}
        expected_alias_modes: dict[str, int] = {}
        try:
            for suffix in ("-wal", "-shm"):
                sidecars[suffix] = _pin_sqlite_sidecar(
                    pinned_main.parent_descriptor,
                    f"{pinned_main.path.name}{suffix}",
                )
            _validate_bound_source_namespace(pinned_main, sidecars)
            alias_lock_descriptor = _acquire_alias_parent_lock(pinned_main)
            _reap_stale_aliases(pinned_main)

            alias_name = f"{_ALIAS_PREFIX}{os.getpid()}-{uuid4().hex}"
            os.mkdir(alias_name, 0o700, dir_fd=pinned_main.parent_descriptor)
            alias_parent_flags = os.O_RDONLY
            if hasattr(os, "O_DIRECTORY"):
                alias_parent_flags |= os.O_DIRECTORY
            if hasattr(os, "O_NOFOLLOW"):
                alias_parent_flags |= os.O_NOFOLLOW
            alias_parent_descriptor = os.open(
                alias_name,
                alias_parent_flags,
                dir_fd=pinned_main.parent_descriptor,
            )
            alias_dir = pinned_main.path.parent / alias_name
            alias_main = alias_dir / "source.db"
            if fcntl is None:
                raise OSError("SQLite alias binding is unsupported on this platform")
            _write_alias_file(alias_parent_descriptor, _ALIAS_LEASE, b"")
            lease_flags = os.O_RDWR
            if hasattr(os, "O_NOFOLLOW"):
                lease_flags |= os.O_NOFOLLOW
            lease_descriptor = os.open(
                _ALIAS_LEASE,
                lease_flags,
                dir_fd=alias_parent_descriptor,
            )
            fcntl.flock(lease_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

            source_links: list[tuple[str, str, tuple[int, int]]] = [
                (pinned_main.path.name, alias_main.name, pinned_main.file_identity)
            ]
            for suffix in ("-wal", "-shm"):
                pinned_sidecar = sidecars[suffix]
                if pinned_sidecar is not None:
                    source_links.append(
                        (
                            pinned_sidecar.name,
                            f"{alias_main.name}{suffix}",
                            pinned_sidecar.identity,
                        )
                    )
            for _source_name, alias_file, identity in source_links:
                expected_aliases[alias_file] = identity
            expected_alias_modes[alias_main.name] = stat.S_IMODE(
                os.fstat(pinned_main.descriptor).st_mode
            )

            wal_format = os.pread(pinned_main.descriptor, 2, 18) == b"\x02\x02"
            if wal_format and sidecars["-wal"] is None:
                alias_local_sidecars["-wal"] = _create_pinned_alias_file(
                    alias_parent_descriptor,
                    _ALIAS_PENDING_WAL,
                )
                expected_aliases[_ALIAS_PENDING_WAL] = alias_local_sidecars["-wal"].identity
                expected_aliases["source.db-wal"] = alias_local_sidecars["-wal"].identity
            if (wal_format or sidecars["-wal"] is not None) and sidecars["-shm"] is None:
                alias_local_sidecars["-shm"] = _create_pinned_alias_file(
                    alias_parent_descriptor,
                    _ALIAS_PENDING_SHM,
                )
                expected_aliases[_ALIAS_PENDING_SHM] = alias_local_sidecars["-shm"].identity
                expected_aliases["source.db-shm"] = alias_local_sidecars["-shm"].identity

            metadata_payload = json.dumps(
                {
                    "version": 1,
                    "pid": os.getpid(),
                    "nonce": alias_name.removeprefix(_ALIAS_PREFIX),
                    "identities": {
                        name: [identity[0], identity[1]]
                        for name, identity in expected_aliases.items()
                    },
                    "modes": expected_alias_modes,
                },
                sort_keys=True,
            ).encode("utf-8")
            _write_alias_file(alias_parent_descriptor, _ALIAS_METADATA, metadata_payload)
            os.fsync(alias_parent_descriptor)
            for source_name, alias_file, _identity in source_links:
                os.link(
                    source_name,
                    alias_file,
                    src_dir_fd=pinned_main.parent_descriptor,
                    dst_dir_fd=alias_parent_descriptor,
                    follow_symlinks=False,
                )
            for suffix, pending_name in (
                ("-wal", _ALIAS_PENDING_WAL),
                ("-shm", _ALIAS_PENDING_SHM),
            ):
                if suffix in alias_local_sidecars:
                    os.link(
                        pending_name,
                        f"source.db{suffix}",
                        src_dir_fd=alias_parent_descriptor,
                        dst_dir_fd=alias_parent_descriptor,
                        follow_symlinks=False,
                    )
            os.fsync(alias_parent_descriptor)
            _validate_alias_namespace(
                alias_parent_descriptor,
                expected_aliases,
                expected_modes=expected_alias_modes,
            )
            _validate_bound_source_namespace(pinned_main, sidecars)
            _require_bound_source_modes(
                pinned_main,
                require_main_owner_only=require_main_owner_only,
                require_parent_owner_only=require_parent_owner_only,
            )
            _require_alias_mode(
                pinned_main,
                alias_dir.name,
                alias_parent_descriptor,
                expected_mode=0o700,
            )
            os.fchmod(alias_parent_descriptor, 0o500)
            _require_alias_mode(
                pinned_main,
                alias_dir.name,
                alias_parent_descriptor,
                expected_mode=0o500,
            )
            _validate_alias_namespace(
                alias_parent_descriptor,
                expected_aliases,
                expected_modes=expected_alias_modes,
            )
            for local_sidecar in alias_local_sidecars.values():
                local_sidecar.validate(alias_parent_descriptor)

            before_fds = _open_fd_snapshot()
            uri = f"{alias_main.as_uri()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True)
            _validate_alias_namespace(
                alias_parent_descriptor,
                expected_aliases,
                expected_modes=expected_alias_modes,
            )
            _require_alias_mode(
                pinned_main,
                alias_dir.name,
                alias_parent_descriptor,
                expected_mode=0o500,
            )
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            _validate_alias_namespace(
                alias_parent_descriptor,
                expected_aliases,
                expected_modes=expected_alias_modes,
            )
            _require_alias_mode(
                pinned_main,
                alias_dir.name,
                alias_parent_descriptor,
                expected_mode=0o500,
            )
            connection.execute("PRAGMA schema_version").fetchone()
            _validate_alias_namespace(
                alias_parent_descriptor,
                expected_aliases,
                expected_modes=expected_alias_modes,
            )
            _validate_bound_source_namespace(pinned_main, sidecars)
            _require_bound_source_modes(
                pinned_main,
                require_main_owner_only=require_main_owner_only,
                require_parent_owner_only=require_parent_owner_only,
            )
            _require_alias_mode(
                pinned_main,
                alias_dir.name,
                alias_parent_descriptor,
                expected_mode=0o500,
            )
            for local_sidecar in alias_local_sidecars.values():
                local_sidecar.validate(alias_parent_descriptor)
            _audit_sqlite_opened_files(
                before_fds,
                expected={
                    "source.db": pinned_main.file_identity,
                    **(
                        {
                            "source.db-wal": (
                                sidecars["-wal"].identity
                                if sidecars["-wal"] is not None
                                else alias_local_sidecars["-wal"].identity
                            )
                        }
                        if sidecars["-wal"] is not None or "-wal" in alias_local_sidecars
                        else {}
                    ),
                },
            )

            os.fchmod(alias_parent_descriptor, 0o700)
            _require_alias_mode(
                pinned_main,
                alias_dir.name,
                alias_parent_descriptor,
                expected_mode=0o700,
            )
            cleanup_errors = _remove_alias_artifacts(
                alias_parent_descriptor,
                expected_aliases,
                expected_modes=expected_alias_modes,
            )
            cleanup_error = cleanup_errors[0] if cleanup_errors else None
            if cleanup_error is not None:
                connection.close()
                connection = None
                _remove_alias_artifacts(
                    alias_parent_descriptor,
                    expected_aliases,
                    expected_modes=expected_alias_modes,
                )
            if _alias_artifacts_remain(alias_parent_descriptor):
                raise OSError("SQLite alias cleanup failed")
            _remove_alias_control_files(alias_parent_descriptor)
            for local_sidecar in alias_local_sidecars.values():
                os.close(local_sidecar.descriptor)
            alias_local_sidecars.clear()
            os.close(lease_descriptor)
            lease_descriptor = -1
            os.close(alias_parent_descriptor)
            alias_parent_descriptor = -1
            os.rmdir(alias_dir.name, dir_fd=pinned_main.parent_descriptor)
            os.fsync(pinned_main.parent_descriptor)
            _release_alias_parent_lock(alias_lock_descriptor)
            alias_lock_descriptor = -1
            if cleanup_error is not None:
                raise cleanup_error
            if connection is None:
                raise OSError("SQLite bound connection closed during cleanup")

            yield connection
            _validate_bound_source_namespace(pinned_main, sidecars)
            _require_bound_source_modes(
                pinned_main,
                require_main_owner_only=require_main_owner_only,
                require_parent_owner_only=require_parent_owner_only,
            )
        finally:
            if connection is not None:
                connection.close()
            if alias_parent_descriptor >= 0:
                with contextlib.suppress(OSError):
                    os.fchmod(alias_parent_descriptor, 0o700)
                first_errors = _remove_alias_artifacts(
                    alias_parent_descriptor,
                    expected_aliases,
                    expected_modes=expected_alias_modes,
                )
                if first_errors:
                    _remove_alias_artifacts(
                        alias_parent_descriptor,
                        expected_aliases,
                        expected_modes=expected_alias_modes,
                    )
                residue = _alias_artifacts_remain(alias_parent_descriptor)
                if not residue:
                    with contextlib.suppress(OSError):
                        _remove_alias_control_files(alias_parent_descriptor)
                for local_sidecar in alias_local_sidecars.values():
                    with contextlib.suppress(OSError):
                        os.close(local_sidecar.descriptor)
                alias_local_sidecars.clear()
                if lease_descriptor >= 0:
                    with contextlib.suppress(OSError):
                        os.close(lease_descriptor)
                    lease_descriptor = -1
                os.close(alias_parent_descriptor)
                alias_parent_descriptor = -1
                if not residue and alias_dir is not None:
                    with contextlib.suppress(OSError):
                        os.rmdir(alias_dir.name, dir_fd=pinned_main.parent_descriptor)
                    with contextlib.suppress(OSError):
                        os.fsync(pinned_main.parent_descriptor)
            if alias_lock_descriptor >= 0:
                with contextlib.suppress(BaseException):
                    _release_alias_parent_lock(alias_lock_descriptor)
            for pinned_sidecar in sidecars.values():
                if pinned_sidecar is not None:
                    with contextlib.suppress(BaseException):
                        os.close(pinned_sidecar.descriptor)


@contextmanager
def audited_database_write_connection(
    source: Path | PinnedPrivateFile,
    *,
    label: str,
) -> Iterator[sqlite3.Connection]:
    """Yield an exact canonical DELETE-journal writer in ``BEGIN IMMEDIATE``.

    The caller retains the shared high-level target lock and the exact canonical
    parent/main pins.  This helper audits SQLite's connect-time main descriptor, then
    securely creates and pins the canonical rollback journal before materializing it.
    A possibly hot journal is never removed by cleanup; ordinary rollback or commit
    owns its lifecycle so abrupt process death remains recoverable by canonical reopen.
    """
    committed = False
    try:
        with _borrow_or_pin_database_file(source) as pinned_main:
            pinned_main.require_sqlite_header(label=label)
            _require_rollback_journal_header(pinned_main, label=label)
            _require_bound_source_modes(
                pinned_main,
                require_main_owner_only=True,
                require_parent_owner_only=True,
            )
            sidecars: dict[str, _PinnedSQLiteSidecar | None] = {
                "-wal": None,
                "-shm": None,
            }
            connection: sqlite3.Connection | None = None
            journal: _PinnedSQLiteSidecar | None = None
            recovery_journal = _pin_sqlite_sidecar(
                pinned_main.parent_descriptor,
                f"{pinned_main.path.name}-journal",
            )
            journal_may_be_hot = False
            reused_inactive_journal = False
            try:
                if recovery_journal is not None:
                    _require_single_writer_journal_link(pinned_main, recovery_journal)
                    _validate_bound_writer_namespace(pinned_main, sidecars)
                    if os.fstat(recovery_journal.descriptor).st_size == 0:
                        _unlink_unused_writer_journal(pinned_main, recovery_journal)
                        os.close(recovery_journal.descriptor)
                        recovery_journal = None
                if recovery_journal is None:
                    _validate_bound_source_namespace(pinned_main, sidecars)

                before_main_fds = _open_fd_snapshot()
                uri = f"{pinned_main.path.as_uri()}?mode=rw"
                connection = sqlite3.connect(uri, uri=True, isolation_level=None)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA busy_timeout=5000")
                _require_exact_changed_regular_file(
                    before_main_fds,
                    expected_identity=pinned_main.file_identity,
                    label="database",
                )
                pinned_main.validate()
                _require_bound_source_modes(
                    pinned_main,
                    require_main_owner_only=True,
                    require_parent_owner_only=True,
                )
                if recovery_journal is not None:
                    reused_inactive_journal = _recover_existing_writer_journal(
                        pinned_main,
                        recovery_journal,
                        connection,
                        label=label,
                    )
                    if reused_inactive_journal:
                        journal = recovery_journal
                        # Even on failure this journal belongs to SQLite, not
                        # the unused-empty-journal cleanup path.
                        journal_may_be_hot = True
                    else:
                        os.close(recovery_journal.descriptor)
                    recovery_journal = None
                if journal is None:
                    _validate_bound_source_namespace(pinned_main, sidecars)
                else:
                    _validate_bound_writer_namespace(pinned_main, sidecars)
                mode = connection.execute("PRAGMA journal_mode").fetchone()
                if mode is None or str(mode[0]).casefold() != "delete":
                    raise OSError("SQLite writer requires DELETE rollback-journal mode")
                connection.execute("PRAGMA synchronous=FULL")
                synchronous = connection.execute("PRAGMA synchronous").fetchone()
                if synchronous is None or int(synchronous[0]) != 2:
                    raise OSError("SQLite writer requires synchronous=FULL")

                if journal is None:
                    journal = _create_pinned_writer_journal(pinned_main)
                _require_single_writer_journal_link(pinned_main, journal)
                before_journal_fds = _open_fd_snapshot()
                connection.execute("BEGIN IMMEDIATE")
                try:
                    journal.validate(pinned_main.parent_descriptor)
                except FileNotFoundError:
                    # An ordinary canonical writer that already held SQLite's
                    # RESERVED lock may have used and removed the empty journal
                    # while BEGIN IMMEDIATE waited.  Now that this connection owns
                    # the lock, replace only the vanished pin with a fresh journal.
                    os.close(journal.descriptor)
                    journal = _create_pinned_writer_journal(pinned_main)
                    reused_inactive_journal = False
                    journal_may_be_hot = False
                    _require_single_writer_journal_link(pinned_main, journal)
                    before_journal_fds = _open_fd_snapshot()
                if reused_inactive_journal:
                    _require_single_writer_journal_link(pinned_main, journal)
                    if os.pread(journal.descriptor, 8, 0) != bytes(8):
                        raise OSError("SQLite recovery journal changed before materialization")
                elif os.fstat(journal.descriptor).st_size != 0:
                    raise OSError("SQLite writer journal was used before materialization")
                user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                journal_may_be_hot = True
                connection.execute(f"PRAGMA user_version={user_version}")
                _require_exact_changed_regular_file(
                    before_journal_fds,
                    expected_identity=journal.identity,
                    label="journal",
                )
                _validate_bound_writer_namespace(pinned_main, sidecars)
                journal.validate(pinned_main.parent_descriptor)
                _require_single_writer_journal_link(pinned_main, journal)
                _require_bound_source_modes(
                    pinned_main,
                    require_main_owner_only=True,
                    require_parent_owner_only=True,
                )
                connection.set_authorizer(_deny_validation_transaction_control)
                yield connection
                if not connection.in_transaction:
                    raise RuntimeError(f"{label} write transaction was released")
                _validate_bound_writer_namespace(pinned_main, sidecars)
                journal.validate(pinned_main.parent_descriptor)
                _require_single_writer_journal_link(pinned_main, journal)
                _require_bound_source_modes(
                    pinned_main,
                    require_main_owner_only=True,
                    require_parent_owner_only=True,
                )
                connection.set_authorizer(None)
                connection.commit()
                committed = True
                _validate_detached_writer_journal(journal)
                _validate_bound_source_namespace(pinned_main, sidecars)
                _require_bound_source_modes(
                    pinned_main,
                    require_main_owner_only=True,
                    require_parent_owner_only=True,
                )
            except BaseException:
                if connection is not None:
                    with contextlib.suppress(sqlite3.Error):
                        connection.set_authorizer(None)
                if connection is not None and connection.in_transaction:
                    with contextlib.suppress(sqlite3.Error):
                        connection.rollback()
                raise
            finally:
                if connection is not None:
                    connection.close()
                if recovery_journal is not None:
                    os.close(recovery_journal.descriptor)
                if journal is not None:
                    try:
                        if not journal_may_be_hot:
                            _unlink_unused_writer_journal(pinned_main, journal)
                    finally:
                        os.close(journal.descriptor)
    except BaseException as exc:
        if committed and not isinstance(exc, DatabaseWriteCommittedError):
            raise DatabaseWriteCommittedError(
                "SQLite write committed before final validation or cleanup failed"
            ) from exc
        raise


def recover_database_journal(source: Path | PinnedPrivateFile, *, label: str) -> bool:
    """Recover interrupted SQLite storage without changing logical records.

    The caller must hold its existing publication/proposal-store lock. Only a
    local write operation may call this helper: read-only services must continue
    to refuse journal state. A clean database is left byte-for-byte untouched.
    """
    if not isinstance(source, PinnedPrivateFile):
        absolute = Path(source).expanduser().absolute()
        with (
            pin_existing_directory(absolute.parent) as parent,
            parent.pin_private_file(absolute.name, mutable=True) as pinned,
        ):
            return recover_database_journal(pinned, label=label)
    with _borrow_or_pin_database_file(source) as pinned_main:
        pinned_main.require_sqlite_header(label=label)
        if (
            _sidecar_metadata(pinned_main.parent_descriptor, f"{pinned_main.path.name}-journal")
            is None
        ):
            return False
        with audited_database_write_connection(source, label=label) as connection:
            _require_integrity(connection, label=label)
        return True


def _tighten_private_database_modes(pinned: _PinnedDatabaseFile) -> None:
    if os.name == "posix":
        os.fchmod(pinned.parent_descriptor, 0o700)
        os.fchmod(pinned.descriptor, 0o600)


def _require_private_database_modes(
    pinned: _PinnedDatabaseFile,
    *,
    label: str,
    restore_on_failure: bool,
) -> None:
    parent_mode = stat.S_IMODE(os.fstat(pinned.parent_descriptor).st_mode)
    file_mode = stat.S_IMODE(os.fstat(pinned.descriptor).st_mode)
    if parent_mode == 0o700 and file_mode == 0o600:
        return
    if restore_on_failure:
        _tighten_private_database_modes(pinned)
        parent_mode = stat.S_IMODE(os.fstat(pinned.parent_descriptor).st_mode)
        file_mode = stat.S_IMODE(os.fstat(pinned.descriptor).st_mode)
        if parent_mode != 0o700 or file_mode != 0o600:
            raise PermissionError(f"{label} private permissions could not be restored")
    raise PermissionError(f"{label} permissions changed during validation")


def _deny_validation_transaction_control(
    action_code: int,
    _argument_one: str | None,
    _argument_two: str | None,
    _database_name: str | None,
    _trigger_name: str | None,
) -> int:
    if action_code in {sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT}:
        return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


@contextmanager
def validated_database_target(
    path: Path,
    *,
    label: str,
    guard: Callable[[sqlite3.Connection], None],
) -> Iterator[sqlite3.Connection]:
    """Hold a validated live SQLite target across a caller-controlled transition.

    The caller must already hold :func:`database_target_lock` for *path*. This
    inner boundary pins the parent and database identities, tightens their modes
    through the pinned descriptors, and holds an exclusive SQLite transaction
    from integrity/guard validation through the yielded transition. The guard
    and caller borrow one read-only connection; this helper owns transaction
    control and rolls the validation transaction back on exit.
    """
    with _pin_database_file(path, writable=True, private_parent=True) as pinned:
        _tighten_private_database_modes(pinned)
        _require_private_database_modes(
            pinned,
            label=label,
            restore_on_failure=False,
        )
        pinned.require_sqlite_header(label=label)

        pinned.validate()
        target_uri = f"{pinned.path.as_uri()}?mode=rw"
        with closing(sqlite3.connect(target_uri, uri=True, isolation_level=None)) as connection:
            pinned.validate()
            connection.execute("PRAGMA busy_timeout=5000")
            locking_mode = connection.execute("PRAGMA locking_mode=EXCLUSIVE").fetchone()
            if locking_mode != ("exclusive",):
                raise sqlite3.OperationalError(
                    f"Could not request exclusive SQLite validation locking for {label}"
                )
            connection.execute("BEGIN EXCLUSIVE")
            connection.execute("PRAGMA query_only=ON")
            connection.set_authorizer(_deny_validation_transaction_control)
            try:
                _require_integrity(connection, label=label)
                guard(connection)
                if not connection.in_transaction:
                    raise RuntimeError(f"{label} validation transaction was released")
                pinned.validate()
                _require_private_database_modes(
                    pinned,
                    label=label,
                    restore_on_failure=True,
                )
                yield connection
                if not connection.in_transaction:
                    raise RuntimeError(f"{label} validation transaction was released")
                pinned.validate()
                _require_private_database_modes(
                    pinned,
                    label=label,
                    restore_on_failure=True,
                )
                connection.set_authorizer(None)
                connection.execute("ROLLBACK")
            except BaseException as primary_error:
                cleanup_errors: list[tuple[str, BaseException]] = []

                def attempt_cleanup(operation: str, action: Callable[[], object]) -> None:
                    try:
                        action()
                    except BaseException as cleanup_error:
                        cleanup_errors.append((operation, cleanup_error))

                attempt_cleanup("authorizer reset", lambda: connection.set_authorizer(None))
                if connection.in_transaction:
                    attempt_cleanup("transaction rollback", lambda: connection.execute("ROLLBACK"))
                attempt_cleanup(
                    "permission restoration", lambda: _tighten_private_database_modes(pinned)
                )
                attempt_cleanup(
                    "permission verification",
                    lambda: _require_private_database_modes(
                        pinned,
                        label=label,
                        restore_on_failure=False,
                    ),
                )
                for operation, cleanup_error in cleanup_errors:
                    primary_error.add_note(
                        f"Secondary {operation} failure: "
                        f"{type(cleanup_error).__name__}: {cleanup_error}"
                    )
                raise
            pinned.validate()
            _require_private_database_modes(
                pinned,
                label=label,
                restore_on_failure=True,
            )


def _publish_database_no_clobber(
    staged: Path,
    target: Path,
    *,
    prepublication_check: Callable[[], None] | None = None,
) -> None:
    """Atomically link a complete private file into an absent destination."""
    staged = _absolute_lexical(staged)
    target = _absolute_lexical(target)
    if staged.parent != target.parent:
        raise ValueError("No-clobber publication requires files in the same directory")

    with pin_private_directory(target.parent) as parent:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(staged.name, flags, dir_fd=parent.descriptor)
        file_identity: tuple[int, int] | None = None
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError(f"Database publication candidate is not a regular file: {staged}")
            file_identity = (metadata.st_dev, metadata.st_ino)
            if os.pread(descriptor, len(_SQLITE_HEADER), 0) != _SQLITE_HEADER:
                raise sqlite3.DatabaseError(
                    f"Database publication candidate does not have a valid SQLite header: {staged}"
                )
            try:
                _fsync_pinned_directory(parent)
            except Exception as exc:
                raise DatabasePublicationDurabilityError(
                    "Database publication durability barrier failed for the staged recovery name",
                    target_durable=False,
                    staged_recovery=staged,
                ) from exc
            if prepublication_check is not None:
                prepublication_check()
                parent.validate()
                current = os.stat(staged.name, dir_fd=parent.descriptor, follow_symlinks=False)
                if (
                    not stat.S_ISREG(current.st_mode)
                    or (current.st_dev, current.st_ino) != file_identity
                ):
                    raise OSError(f"Database candidate changed before publication: {staged}")
            os.link(
                staged.name,
                target.name,
                src_dir_fd=parent.descriptor,
                dst_dir_fd=parent.descriptor,
                follow_symlinks=False,
            )
            try:
                target_metadata = os.stat(
                    target.name,
                    dir_fd=parent.descriptor,
                    follow_symlinks=False,
                )
                if (
                    not stat.S_ISREG(target_metadata.st_mode)
                    or (target_metadata.st_dev, target_metadata.st_ino) != file_identity
                ):
                    raise OSError(f"Database destination changed during publication: {target}")
                _fsync_pinned_directory(parent)
            except BaseException as exc:
                rollback_error = _rollback_uncommitted_database_link(
                    parent,
                    target_name=target.name,
                    file_identity=file_identity,
                )
                if not isinstance(exc, Exception):
                    raise
                raise DatabasePublicationDurabilityError(
                    "Database publication durability barrier failed before target commit",
                    target_durable=False,
                    staged_recovery=staged,
                    rollback_error=rollback_error,
                ) from exc

            staged_unlinked = False
            try:
                parent.validate()
                os.unlink(staged.name, dir_fd=parent.descriptor)
                staged_unlinked = True
                _fsync_pinned_directory(parent)
            except Exception as exc:
                raise DatabasePublicationDurabilityError(
                    "Database publication durability barrier failed during staged-name cleanup",
                    target_durable=True,
                    staged_recovery=None if staged_unlinked else staged,
                ) from exc
        finally:
            with contextlib.suppress(BaseException):
                os.close(descriptor)


def _fsync_pinned_directory(parent: PinnedPrivateDirectory) -> None:
    """Validate and sync one already-pinned database parent directory."""
    parent.validate()
    os.fsync(parent.descriptor)
    parent.validate()


def _rollback_uncommitted_database_link(
    parent: PinnedPrivateDirectory,
    *,
    target_name: str,
    file_identity: tuple[int, int],
) -> BaseException | None:
    """Remove only this publication's uncommitted target link and sync the mutation."""
    try:
        parent.validate()
        try:
            current = os.stat(
                target_name,
                dir_fd=parent.descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return None
        if (
            not stat.S_ISREG(current.st_mode)
            or (
                current.st_dev,
                current.st_ino,
            )
            != file_identity
        ):
            return None
        os.unlink(target_name, dir_fd=parent.descriptor)
        _fsync_pinned_directory(parent)
    except BaseException as exc:
        return exc
    return None


def snapshot_database(source: Path, target: Path) -> Path:
    """Publish an integrity-checked online SQLite backup of *source* at *target*.

    The source is opened read-only so a missing path cannot be created as a side
    effect. The backup is built in a unique owner-only work directory and is
    atomically published only after every ``PRAGMA integrity_check`` row is ``ok``.
    """
    return _snapshot_database(source, target, no_clobber=False)


def _write_snapshot_connection(
    source_connection: sqlite3.Connection,
    published: BinaryIO,
) -> None:
    """Write an integrity-checked snapshot into an already-pinned publication stream."""
    with tempfile.TemporaryDirectory(prefix="ledger-snapshot-") as raw_work_dir:
        work_dir = ensure_private_dir(Path(raw_work_dir).resolve())
        temp_path = work_dir / "snapshot.db"
        _create_private_empty_file(temp_path)
        with closing(sqlite3.connect(temp_path)) as target_connection:
            source_connection.backup(target_connection)
            _require_integrity(target_connection, label="SQLite snapshot")
        with temp_path.open("rb") as staged:
            shutil.copyfileobj(staged, published)


def snapshot_bound_database(
    source: Path | PinnedPrivateFile,
    target: Path,
    *,
    label: str,
    require_main_owner_only: bool,
    require_parent_owner_only: bool,
) -> Path:
    """Snapshot the exact pinned main/WAL/SHM logical view into a private file."""
    source_absolute = (
        source.path if isinstance(source, PinnedPrivateFile) else _absolute_lexical(Path(source))
    )
    target_absolute = _absolute_lexical(Path(target))
    if source_absolute == target_absolute:
        raise ValueError("Snapshot source and target must be different files")
    with (
        private_atomic_binary(Path(target)) as published,
        bound_database_read_connection(
            source,
            label=label,
            require_main_owner_only=require_main_owner_only,
            require_parent_owner_only=require_parent_owner_only,
        ) as source_connection,
    ):
        _write_snapshot_connection(source_connection, published)
    return Path(target)


def _snapshot_database(
    source: Path,
    target: Path,
    *,
    no_clobber: bool,
    prepublication_check: Callable[[], None] | None = None,
) -> Path:
    source_input = Path(source)
    target_input = Path(target)
    publication_target = target_input
    if no_clobber:
        target_absolute = _absolute_lexical(target_input)
        if target_absolute.exists():
            raise FileExistsError(f"Database destination already exists: {target_input}")
        publication_target = target_input.with_name(
            f".{target_input.name}.no-clobber-{os.getpid()}-{uuid4().hex}.tmp"
        )
    with _pin_database_file(
        source_input,
        writable=False,
        private_parent=False,
    ) as pinned_source:
        pinned_source.require_sqlite_header(label="Snapshot source")
        target_absolute = _absolute_lexical(target_input)
        if pinned_source.path == target_absolute:
            raise ValueError("Snapshot source and target must be different files")
        if target_absolute.exists() and target_absolute.is_file():
            target_metadata = target_absolute.stat(follow_symlinks=False)
            if (target_metadata.st_dev, target_metadata.st_ino) == pinned_source.file_identity:
                raise ValueError("Snapshot source and target must be different files")

        with private_atomic_binary(publication_target) as published:
            source_uri = f"{pinned_source.path.as_uri()}?mode=ro"
            with closing(sqlite3.connect(source_uri, uri=True)) as source_connection:
                pinned_source.validate()
                _write_snapshot_connection(source_connection, published)
                pinned_source.validate()
    if no_clobber:
        try:
            _publish_database_no_clobber(
                publication_target,
                target_input,
                prepublication_check=prepublication_check,
            )
        except DatabasePublicationDurabilityError as exc:
            if not exc.preserve_staged:
                cleanup_database_artifacts(publication_target)
            raise
        except BaseException:
            cleanup_database_artifacts(publication_target)
            raise
        else:
            cleanup_database_artifacts(publication_target)
    return target_input


def restore_database(
    snapshot: Path,
    live_target: Path,
    *,
    guard: Callable[[sqlite3.Connection], None] | None = None,
) -> Path:
    """Restore a validated snapshot into an existing live SQLite target.

    SQLite's online backup API writes through the destination database connection,
    so WAL state is replaced transactionally instead of replaying over a swapped
    main file. The source is fully validated before the live target is opened.
    """
    snapshot_input = Path(snapshot)
    live_target_input = Path(live_target)
    with (
        _pin_database_file(
            snapshot_input,
            writable=False,
            private_parent=False,
        ) as pinned_source,
        _pin_database_file(
            live_target_input,
            writable=True,
            private_parent=True,
        ) as pinned_target,
    ):
        pinned_source.require_sqlite_header(label="Restore snapshot")
        pinned_target.require_sqlite_header(label="Restore destination")
        if pinned_source.file_identity == pinned_target.file_identity:
            raise ValueError("Restore snapshot and destination must be different files")
        if os.name == "posix":
            os.fchmod(pinned_target.descriptor, 0o600)

        source_uri = f"{pinned_source.path.as_uri()}?mode=ro"
        target_uri = f"{pinned_target.path.as_uri()}?mode=rw"
        with closing(sqlite3.connect(source_uri, uri=True)) as source_connection:
            pinned_source.validate()
            _require_integrity(source_connection, label="Restore snapshot")
            target_connection = (
                sqlite3.connect(target_uri, uri=True)
                if guard is None
                else sqlite3.connect(target_uri, uri=True, isolation_level=None)
            )
            with closing(target_connection):
                pinned_target.validate()
                target_connection.execute("PRAGMA busy_timeout=5000")
                if guard is not None:
                    locking_mode = target_connection.execute(
                        "PRAGMA locking_mode=EXCLUSIVE"
                    ).fetchone()
                    if locking_mode != ("exclusive",):
                        raise sqlite3.OperationalError(
                            "Could not request exclusive SQLite publication locking"
                        )
                    target_connection.execute("BEGIN EXCLUSIVE")
                    try:
                        guard(target_connection)
                        pinned_source.validate()
                        pinned_target.validate()
                        target_connection.execute("PRAGMA locking_mode=NORMAL")
                        target_connection.execute("COMMIT")
                    except BaseException:
                        if target_connection.in_transaction:
                            target_connection.execute("ROLLBACK")
                        raise
                pinned_source.validate()
                pinned_target.validate()
                source_connection.backup(target_connection)
                _require_integrity(target_connection, label="Restored database")
                pinned_source.validate()
                pinned_target.validate()
    return live_target_input


def publish_database(
    candidate: Path,
    live_target: Path,
    *,
    guard: Callable[[sqlite3.Connection], None] | None = None,
    prepublication_check: Callable[[], None] | None = None,
) -> Path:
    """Publish a complete SQLite candidate without replacing a live WAL main file."""
    candidate = Path(candidate)
    live_target = Path(live_target)
    if live_target.is_symlink():
        raise OSError(f"Database path is a symlink: {live_target}")
    if live_target.exists():
        live_guard = guard
        if prepublication_check is not None:

            def checked_guard(connection: sqlite3.Connection) -> None:
                if guard is not None:
                    guard(connection)
                prepublication_check()

            live_guard = checked_guard
        restore_database(candidate, live_target, guard=live_guard)
    else:
        _snapshot_database(
            candidate,
            live_target,
            no_clobber=guard is not None or prepublication_check is not None,
            prepublication_check=prepublication_check,
        )
    return live_target
