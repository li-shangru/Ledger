"""Private SQLite storage for deferred merchant-rule proposals.

This module intentionally owns a declarative base, engine, and schema lifecycle
that are unrelated to ``ledger.storage.database``.  A proposal is operator intent,
not statement/import data; keeping it in a sibling file prevents finalize and
statement repair from replacing it wholesale.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import stat
import threading
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import DateTime, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import StaticPool

from ledger.storage.snapshots import (
    audited_database_write_connection,
    bound_database_read_connection,
    recover_database_journal,
)
from ledger.utils.private_files import PinnedPrivateDirectory, pin_private_directory

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback is process-local
    fcntl = None  # type: ignore[assignment]


_INITIALIZATION_LOCK = threading.Lock()
_SQLITE_HEADER = b"SQLite format 3\x00"
_LOCK_OPEN_ATTEMPTS = 100


class ProposalBase(DeclarativeBase):
    """Declarative base used only by ``proposals.db``."""


class ProposalDB(ProposalBase):
    """One narrow, deferred merchant-rule teaching action."""

    __tablename__ = "proposals"

    proposal_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    proposal_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    merchant_norm: Mapped[str] = mapped_column(String(200), nullable=False)
    pattern: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    evidence_id: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PlanningProposalDB(ProposalBase):
    """A typed planning intent; deliberately separate from financial tables."""

    __tablename__ = "planning_proposals"
    proposal_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    proposal_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    intent_json: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PlanningProposalApplicationDB(ProposalBase):
    """Durable application reservation, before the separate financial commit."""

    __tablename__ = "planning_proposal_applications"
    proposal_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    proposal_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    preview_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    ledger_target: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class PlanningProposalTargetDB(ProposalBase):
    """Original financial target retained after application and undo complete."""

    __tablename__ = "planning_proposal_targets"
    proposal_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    ledger_target: Mapped[str] = mapped_column(String(64), nullable=False)
    bound_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


def _absolute(path: Path) -> Path:
    expanded = Path(path).expanduser()
    return Path(os.path.abspath(os.fspath(expanded)))


def _new_schema_bytes() -> bytes:
    """Build a complete store without opening the eventual proposal pathname."""
    with closing(sqlite3.connect(":memory:")) as connection:
        engine = create_engine(
            "sqlite://",
            creator=lambda: connection,
            poolclass=StaticPool,
        )
        try:
            ProposalBase.metadata.create_all(engine)
            connection.commit()
            return connection.serialize()
        finally:
            engine.dispose()


def _publish_new_store(
    parent: PinnedPrivateDirectory,
    name: str,
    payload: bytes,
) -> None:
    """Durably link a complete staged database only when the final name is absent."""
    candidate = f".{name}.initialize-{uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(candidate, flags, 0o600, dir_fd=parent.descriptor)
    published = False
    try:
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:  # pragma: no cover - regular-file write invariant
                raise OSError("Proposal store staging write made no progress")
            view = view[written:]
        os.fsync(descriptor)
        parent.validate()
        os.link(
            candidate,
            name,
            src_dir_fd=parent.descriptor,
            dst_dir_fd=parent.descriptor,
            follow_symlinks=False,
        )
        published = True
        os.fsync(parent.descriptor)
    finally:
        os.close(descriptor)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(candidate, dir_fd=parent.descriptor)
        if published:
            os.fsync(parent.descriptor)


@contextmanager
def _initialization_lock(path: Path) -> Iterator[PinnedPrivateDirectory]:
    """Serialize schema/writes across MCP tasks and proposal-server processes."""

    with _INITIALIZATION_LOCK, pin_private_directory(path.parent) as parent:
        flags = os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        lock_name = f".{path.name}.initialize.lock"
        descriptor = -1
        for _ in range(_LOCK_OPEN_ATTEMPTS):
            try:
                descriptor = os.open(lock_name, flags, dir_fd=parent.descriptor)
            except FileNotFoundError:
                try:
                    descriptor = os.open(
                        lock_name,
                        flags | os.O_CREAT | os.O_EXCL,
                        0o600,
                        dir_fd=parent.descriptor,
                    )
                except FileExistsError:
                    # Another process published the permanent lock first. Reopen
                    # that exact directory entry on the next iteration.
                    continue
            break
        if descriptor < 0:  # pragma: no cover - pathological namespace churn
            raise OSError("Could not pin proposal initialization lock")
        try:
            if os.name == "posix":
                os.fchmod(descriptor, 0o600)
            if fcntl is not None:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            current = os.stat(lock_name, dir_fd=parent.descriptor, follow_symlinks=False)
            pinned = os.fstat(descriptor)
            if (
                not stat.S_ISREG(current.st_mode)
                or not stat.S_ISREG(pinned.st_mode)
                or (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino)
            ):
                raise OSError("Proposal initialization lock changed during acquisition")
            parent.validate()
            yield parent
            parent.validate()
        finally:
            if fcntl is not None:
                with contextlib.suppress(OSError):
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _initialize_locked(parent: PinnedPrivateDirectory, absolute: Path) -> None:
    """Create and validate the store while its permanent process lock is held."""
    parent.require_owner_only()
    try:
        metadata = os.stat(absolute.name, dir_fd=parent.descriptor, follow_symlinks=False)
    except FileNotFoundError:
        _publish_new_store(parent, absolute.name, _new_schema_bytes())
    else:
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("Proposal store is not a regular file")
    with parent.pin_private_file(absolute.name, mutable=True) as pinned:
        if os.pread(pinned.descriptor, len(_SQLITE_HEADER), 0) != _SQLITE_HEADER:
            raise sqlite3.DatabaseError("Proposal store does not have a valid SQLite header")


def init_proposal_database(path: Path) -> Path:
    """Create the proposal schema in a ``0700`` directory and ``0600`` file."""
    absolute = _absolute(path)
    with _initialization_lock(absolute) as parent:
        _initialize_locked(parent, absolute)
    return absolute


def recover_proposal_database(path: Path) -> bool:
    """Consume an interrupted store journal under the normal store lock."""
    absolute = _absolute(path)
    with (
        _initialization_lock(absolute) as parent,
        parent.pin_private_file(absolute.name, mutable=True) as pinned,
    ):
        return recover_database_journal(pinned, label="Proposal store recovery")


@contextmanager
def _pinned_proposal_connection(
    parent: PinnedPrivateDirectory,
    absolute: Path,
    *,
    writable: bool,
) -> Iterator[sqlite3.Connection]:
    parent.require_owner_only()
    with parent.pin_private_file(absolute.name, mutable=writable) as pinned:
        if writable:
            boundary = audited_database_write_connection(pinned, label="Proposal store")
        else:
            boundary = bound_database_read_connection(
                pinned,
                label="Proposal store",
                require_main_owner_only=True,
                require_parent_owner_only=True,
            )
        with boundary as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=5000")
            if writable:
                # Additive upgrade for pre-existing merchant-rule stores. No
                # financial schema initializer or pathname is ever consulted.
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS planning_proposals ("
                    "proposal_id VARCHAR(36) PRIMARY KEY, "
                    "proposal_revision VARCHAR(64) NOT NULL, intent_json TEXT NOT NULL, "
                    "status VARCHAR(20) NOT NULL, created_at DATETIME NOT NULL, "
                    "applied_at DATETIME, rejected_at DATETIME, undone_at DATETIME)"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS planning_proposal_applications ("
                    "proposal_id VARCHAR(36) PRIMARY KEY, proposal_revision VARCHAR(64) NOT NULL, "
                    "preview_revision VARCHAR(64) NOT NULL, ledger_target VARCHAR(64), "
                    "started_at DATETIME NOT NULL)"
                )
                application_columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(planning_proposal_applications)"
                    )
                }
                if "ledger_target" not in application_columns:
                    connection.execute(
                        "ALTER TABLE planning_proposal_applications ADD COLUMN ledger_target VARCHAR(64)"
                    )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS planning_proposal_targets ("
                    "proposal_id VARCHAR(36) PRIMARY KEY, ledger_target VARCHAR(64) NOT NULL, "
                    "bound_at DATETIME NOT NULL)"
                )
            yield connection


@contextmanager
def proposal_session(
    path: Path,
    *,
    create: bool,
    writable: bool,
) -> Iterator[sqlite3.Connection]:
    """Open one descriptor-audited proposal-store connection on a pinned inode."""
    absolute = _absolute(path)
    # The descriptor audits snapshot process-wide open files, while DELETE-journal
    # recovery has one canonical filename. Hold one process/flock boundary through
    # every read or write so those audits cannot be confused by another proposal
    # connection and a reader never observes an in-flight rollback journal.
    with _initialization_lock(absolute) as parent:
        if create:
            # Keep the first write inside the same process lock that published and
            # validated the inode. No pathname is released/reopened between them.
            _initialize_locked(parent, absolute)
        with _pinned_proposal_connection(parent, absolute, writable=writable) as connection:
            yield connection


__all__ = [
    "ProposalBase",
    "ProposalDB",
    "init_proposal_database",
    "proposal_session",
]
