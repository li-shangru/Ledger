"""Fail-closed SQLite access for the local read-only Ledger MCP."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from ledger.storage.database import Base
from ledger.storage.migrations import MIGRATIONS, REQUIRED_TABLES

_SQLITE_HEADER = b"SQLite format 3\x00"
_SQLITE_SIDECARS = ("-journal", "-wal", "-shm")
_OPTIONAL_CASH_TABLE = "investment_cash_balances"
_LEGACY_INVESTMENT_VERSION = "20260912_0029_investment_pdf_statement_fields"


class ReadOnlyDatabaseError(RuntimeError):
    """A content-free failure at the MCP database boundary."""

    code = "read_only_unavailable"

    def __init__(self) -> None:
        super().__init__(self.code)


class DatabaseUnavailableError(ReadOnlyDatabaseError):
    code = "database_unavailable"


class IncompatibleSchemaError(ReadOnlyDatabaseError):
    code = "incompatible_schema"


class CorruptDatabaseError(ReadOnlyDatabaseError):
    code = "corrupt_data"


class ReadOnlyUnavailableError(ReadOnlyDatabaseError):
    code = "read_only_unavailable"


class DatabaseChangedError(ReadOnlyUnavailableError):
    code = "database_changed"


@dataclass(frozen=True)
class DatabaseSnapshot:
    """Aggregate-safe readiness facts; never contains paths or row content."""

    transaction_count: int
    earliest_date: date | None
    latest_date: date | None
    migration_version: str
    generation: str = ""
    schema_compatible: bool = True
    integrity_ok: bool = True
    # A naive DATETIME, unlike the posting dates, and the age of THIS PIN: it
    # says when the snapshot being served was last imported into, not what the
    # clock reads now.
    #
    # Taken over `transactions` AND `statement_coverage`, because those two
    # stamps are written at different moments of ONE import: `imported_at` on a
    # transaction is stamped per parsed FILE by `ParseContext`, while
    # `statement_coverage.imported_at` is a single `utc_now_dt()` taken at
    # finalize, which always runs after every file has been parsed. The coverage
    # stamp is therefore structurally the newer of the two, and a maximum taken
    # over transactions alone names an instant the import had not finished at.
    # This is the value `ledger.changes` takes as its boundary, so a lagging one
    # made the documented loop re-report the same statements forever.
    latest_imported_at: datetime | None = None


def _snapshot_generation(
    file_identity: tuple[int, int, int, int],
    transaction_count: int,
) -> str:
    """Derive an opaque id for one pinned database state.

    Hashed from the pin's own (device, inode, size, mtime_ns) plus the row count,
    so it costs no extra I/O and changes whenever the pinned file changes -- by
    promote (new inode) or by an in-place write (new size/mtime).

    Deliberately NOT `migration_version`: a migration version does not identify
    the imported contents of a database.

    Hashed rather than emitted raw because device and inode numbers are host
    filesystem details that the wire contract has no business carrying.
    """
    document = "|".join(str(part) for part in (*file_identity, transaction_count))
    return hashlib.sha256(document.encode("utf-8")).hexdigest()[:16]


def _dates(*values: object) -> list[date]:
    """Parse the present date bounds, ignoring the tables that hold no rows."""
    return [date.fromisoformat(str(value)) for value in values if value is not None]


def _stamps(*values: object) -> list[datetime]:
    """Parse the present import stamps, ignoring the tables that hold no rows."""
    return [datetime.fromisoformat(str(value)) for value in values if value is not None]


def _lexical_absolute(path: Path) -> Path:
    expanded = path.expanduser()
    return Path(os.path.abspath(os.fspath(expanded)))


def _reject_symlink_components(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            if current == path:
                raise DatabaseUnavailableError from None
            raise DatabaseUnavailableError from None
        except OSError:
            raise ReadOnlyUnavailableError from None
        if stat.S_ISLNK(metadata.st_mode):
            raise ReadOnlyUnavailableError


def _directory_open_flags() -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


@dataclass
class _PinnedReadOnlyFile:
    path: Path
    parent_descriptor: int
    descriptor: int
    parent_identity: tuple[int, int]
    file_identity: tuple[int, int, int, int]

    def validate_unchanged(self) -> None:
        live_parent_descriptor = -1
        try:
            _reject_symlink_components(self.path)
            live_parent_descriptor = os.open(
                self.path.parent,
                _directory_open_flags(),
            )
            pinned_parent = os.fstat(self.parent_descriptor)
            live_parent = os.fstat(live_parent_descriptor)
            if (pinned_parent.st_dev, pinned_parent.st_ino) != self.parent_identity or (
                live_parent.st_dev,
                live_parent.st_ino,
            ) != self.parent_identity:
                raise DatabaseChangedError
            current = os.stat(
                self.path.name,
                dir_fd=live_parent_descriptor,
                follow_symlinks=False,
            )
            pinned = os.fstat(self.descriptor)
        except ReadOnlyDatabaseError:
            raise
        except OSError:
            raise DatabaseChangedError from None
        finally:
            if live_parent_descriptor >= 0:
                with suppress(OSError):
                    os.close(live_parent_descriptor)
        current_identity = (
            current.st_dev,
            current.st_ino,
            current.st_size,
            current.st_mtime_ns,
        )
        pinned_identity = (
            pinned.st_dev,
            pinned.st_ino,
            pinned.st_size,
            pinned.st_mtime_ns,
        )
        if (
            not stat.S_ISREG(current.st_mode)
            or not stat.S_ISREG(pinned.st_mode)
            or current_identity != self.file_identity
            or pinned_identity != self.file_identity
        ):
            raise DatabaseChangedError

    def close(self) -> None:
        descriptor = self.descriptor
        parent_descriptor = self.parent_descriptor
        self.descriptor = -1
        self.parent_descriptor = -1
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
        if parent_descriptor >= 0:
            with suppress(OSError):
                os.close(parent_descriptor)


def _pin_database(path: Path) -> _PinnedReadOnlyFile:
    from ledger.runtime import RuntimePathError, require_external_runtime_path

    try:
        require_external_runtime_path(path)
    except RuntimePathError:
        raise ReadOnlyUnavailableError from None
    _reject_symlink_components(path)
    try:
        parent_descriptor = os.open(path.parent, _directory_open_flags())
    except OSError:
        raise ReadOnlyUnavailableError from None

    descriptor = -1
    try:
        file_flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            file_flags |= os.O_NOFOLLOW
        descriptor = os.open(path.name, file_flags, dir_fd=parent_descriptor)
        parent = os.fstat(parent_descriptor)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise DatabaseUnavailableError
        pinned = _PinnedReadOnlyFile(
            path=path,
            parent_descriptor=parent_descriptor,
            descriptor=descriptor,
            parent_identity=(parent.st_dev, parent.st_ino),
            file_identity=(
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
            ),
        )
        pinned.validate_unchanged()
        return pinned
    except ReadOnlyDatabaseError:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_descriptor)
        raise
    except OSError:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
        with suppress(OSError):
            os.close(parent_descriptor)
        raise DatabaseUnavailableError from None


def _reject_sidecars(path: Path) -> None:
    for suffix in _SQLITE_SIDECARS:
        try:
            os.lstat(f"{path}{suffix}")
        except FileNotFoundError:
            continue
        except OSError:
            raise ReadOnlyUnavailableError from None
        raise ReadOnlyUnavailableError


def _validate_header(pinned: _PinnedReadOnlyFile) -> None:
    try:
        header = os.pread(pinned.descriptor, 100, 0)
    except OSError:
        raise DatabaseUnavailableError from None
    if len(header) < 100 or not header.startswith(_SQLITE_HEADER):
        raise CorruptDatabaseError
    if header[18] == 2 or header[19] == 2:
        raise ReadOnlyUnavailableError
    if header[18] != 1 or header[19] != 1:
        raise CorruptDatabaseError


_DENIED_ACTIONS = frozenset(
    action
    for action in (
        getattr(sqlite3, "SQLITE_CREATE_INDEX", None),
        getattr(sqlite3, "SQLITE_CREATE_TABLE", None),
        getattr(sqlite3, "SQLITE_CREATE_TEMP_INDEX", None),
        getattr(sqlite3, "SQLITE_CREATE_TEMP_TABLE", None),
        getattr(sqlite3, "SQLITE_CREATE_TEMP_TRIGGER", None),
        getattr(sqlite3, "SQLITE_CREATE_TEMP_VIEW", None),
        getattr(sqlite3, "SQLITE_CREATE_TRIGGER", None),
        getattr(sqlite3, "SQLITE_CREATE_VIEW", None),
        getattr(sqlite3, "SQLITE_DELETE", None),
        getattr(sqlite3, "SQLITE_DROP_INDEX", None),
        getattr(sqlite3, "SQLITE_DROP_TABLE", None),
        getattr(sqlite3, "SQLITE_DROP_TEMP_INDEX", None),
        getattr(sqlite3, "SQLITE_DROP_TEMP_TABLE", None),
        getattr(sqlite3, "SQLITE_DROP_TEMP_TRIGGER", None),
        getattr(sqlite3, "SQLITE_DROP_TEMP_VIEW", None),
        getattr(sqlite3, "SQLITE_DROP_TRIGGER", None),
        getattr(sqlite3, "SQLITE_DROP_VIEW", None),
        getattr(sqlite3, "SQLITE_INSERT", None),
        getattr(sqlite3, "SQLITE_UPDATE", None),
        getattr(sqlite3, "SQLITE_ATTACH", None),
        getattr(sqlite3, "SQLITE_DETACH", None),
        getattr(sqlite3, "SQLITE_ALTER_TABLE", None),
        getattr(sqlite3, "SQLITE_REINDEX", None),
        getattr(sqlite3, "SQLITE_ANALYZE", None),
        getattr(sqlite3, "SQLITE_CREATE_VTABLE", None),
        getattr(sqlite3, "SQLITE_DROP_VTABLE", None),
        getattr(sqlite3, "SQLITE_TRANSACTION", None),
        getattr(sqlite3, "SQLITE_SAVEPOINT", None),
    )
    if action is not None
)

_READ_ONLY_PRAGMAS = frozenset(
    {
        "collation_list",
        "compile_options",
        "database_list",
        "foreign_key_check",
        "foreign_key_list",
        "index_info",
        "index_list",
        "index_xinfo",
        "integrity_check",
        "journal_mode",
        "quick_check",
        "read_uncommitted",
        "schema_version",
        "table_info",
        "table_list",
        "table_xinfo",
        "query_only",
    }
)


def _authorizer(
    action: int,
    argument_one: str | None,
    argument_two: str | None,
    _database_name: str | None,
    _trigger_name: str | None,
) -> int:
    if action in _DENIED_ACTIONS:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_PRAGMA:
        pragma = (argument_one or "").casefold()
        if pragma not in _READ_ONLY_PRAGMAS:
            return sqlite3.SQLITE_DENY
        if pragma in {"journal_mode", "query_only", "read_uncommitted"} and argument_two:
            return sqlite3.SQLITE_DENY
    return sqlite3.SQLITE_OK


class ReadOnlyDatabase:
    """A static, pinned Ledger snapshot that can only execute read queries."""

    def __init__(self, path: Path) -> None:
        self.path = _lexical_absolute(Path(path))
        self._pinned = _pin_database(self.path)
        try:
            _reject_sidecars(self.path)
            _validate_header(self._pinned)
            self._engine = create_engine(
                "sqlite+pysqlite://",
                creator=self._connect,
                poolclass=NullPool,
            )
        except BaseException:
            self._pinned.close()
            raise
        self._snapshot: DatabaseSnapshot | None = None

    def _connect(self) -> sqlite3.Connection:
        self.validate_current()
        uri = f"file:/dev/fd/{self._pinned.descriptor}?mode=ro"
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(uri, uri=True)
            connection.execute("PRAGMA query_only=ON")
            if connection.execute("PRAGMA query_only").fetchone() != (1,):
                raise ReadOnlyUnavailableError
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.set_authorizer(_authorizer)
            self.validate_current()
            return connection
        except ReadOnlyDatabaseError:
            if connection is not None:
                connection.close()
            raise
        except sqlite3.Error:
            if connection is not None:
                connection.close()
            raise ReadOnlyUnavailableError from None

    def validate_current(self) -> None:
        """Revalidate the pinned file, its live path, header, and sidecar state."""
        self._pinned.validate_unchanged()
        _reject_sidecars(self.path)
        _validate_header(self._pinned)

    def preflight(self) -> DatabaseSnapshot:
        """Validate compatibility/integrity once and return safe readiness facts."""
        self.validate_current()
        try:
            inspector = inspect(self._engine)
            existing = set(inspector.get_table_names())
            # 0030 only adds optional source evidence. The fully validated 0029
            # layout remains readable without letting MCP migrate owner data.
            # Both table and marker must be absent; partial upgrades still fail.
            legacy_cash_layout = _OPTIONAL_CASH_TABLE not in existing
            if legacy_cash_layout:
                with self._engine.connect() as connection:
                    if (
                        connection.exec_driver_sql(
                            "SELECT 1 FROM sqlite_master WHERE name = ?",
                            (_OPTIONAL_CASH_TABLE,),
                        ).fetchone()
                        is not None
                    ):
                        raise IncompatibleSchemaError
            required_tables = REQUIRED_TABLES - (
                {_OPTIONAL_CASH_TABLE} if legacy_cash_layout else set()
            )
            if required_tables - existing:
                raise IncompatibleSchemaError
            for table_name in required_tables:
                expected = set(Base.metadata.tables[table_name].columns.keys())
                actual = {str(column["name"]) for column in inspector.get_columns(table_name)}
                if expected - actual:
                    raise IncompatibleSchemaError

            required_versions = {migration.version for migration in MIGRATIONS}
            if legacy_cash_layout:
                required_versions = {
                    version
                    for version in required_versions
                    if version <= _LEGACY_INVESTMENT_VERSION
                }
            with self._engine.connect() as connection:
                applied_versions = {
                    str(row[0])
                    for row in connection.exec_driver_sql(
                        "SELECT version FROM schema_migrations"
                    ).fetchall()
                }
                if applied_versions != required_versions:
                    raise IncompatibleSchemaError

                integrity = connection.exec_driver_sql("PRAGMA integrity_check").fetchall()
                if not integrity or any(str(row[0]).casefold() != "ok" for row in integrity):
                    raise CorruptDatabaseError
                if connection.exec_driver_sql("PRAGMA foreign_key_check").fetchone() is not None:
                    raise CorruptDatabaseError

                row = connection.exec_driver_sql(
                    "SELECT count(txn_id), min(posting_date), max(posting_date), "
                    "max(imported_at) FROM transactions"
                ).one()
                # Transfers carry their own posting_date and are a separate
                # table, so a range taken from `transactions` alone under-reports
                # how current the database is. `transaction_count` stays as it
                # reads: the number of transaction rows.
                transfer_row = connection.exec_driver_sql(
                    "SELECT min(posting_date), max(posting_date) FROM transfers"
                ).one()
                # The other half of the import-stamp range. A statement that
                # produced no rows leaves nothing in `transactions` at all, and
                # finalize's single stamp is written after every per-file parse
                # stamp, so the newest import is only ever visible here.
                coverage_row = connection.exec_driver_sql(
                    "SELECT max(imported_at) FROM statement_coverage"
                ).one()
        except ReadOnlyDatabaseError:
            raise
        except SQLAlchemyError, sqlite3.DatabaseError:
            raise CorruptDatabaseError from None

        self.validate_current()
        try:
            starts = _dates(row[1], transfer_row[0])
            ends = _dates(row[2], transfer_row[1])
            snapshot = DatabaseSnapshot(
                transaction_count=int(row[0]),
                earliest_date=min(starts, default=None),
                latest_date=max(ends, default=None),
                migration_version=max(required_versions, default="base"),
                generation=_snapshot_generation(
                    self._pinned.file_identity,
                    int(row[0]),
                ),
                latest_imported_at=max(_stamps(row[3], coverage_row[0]), default=None),
            )
        except TypeError, ValueError:
            raise CorruptDatabaseError from None
        self._snapshot = snapshot
        return snapshot

    @contextmanager
    def session_scope(self, _path: Path) -> Iterator[Session]:
        """Yield one non-committing ORM session over a fresh read-only connection."""
        self.validate_current()
        session = Session(self._engine)
        try:
            yield session
        finally:
            session.rollback()
            session.close()
            self.validate_current()

    def close(self) -> None:
        self._engine.dispose()
        self._pinned.close()

    def __del__(self) -> None:
        if hasattr(self, "_engine"):
            with suppress(BaseException):
                self.close()


__all__ = [
    "CorruptDatabaseError",
    "DatabaseChangedError",
    "DatabaseSnapshot",
    "DatabaseUnavailableError",
    "IncompatibleSchemaError",
    "ReadOnlyDatabase",
    "ReadOnlyDatabaseError",
    "ReadOnlyUnavailableError",
]
