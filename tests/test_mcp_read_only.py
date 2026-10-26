"""Synthetic invariants for the Ledger MCP's SQLite read-only boundary."""

from __future__ import annotations

import contextlib
import os
import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import func, text
from sqlalchemy.exc import SQLAlchemyError

from ledger.storage.database import TransactionDB, session_scope
from ledger.storage.migrations import run_migrations
from ledger.storage.read_only import (
    CorruptDatabaseError,
    DatabaseChangedError,
    DatabaseUnavailableError,
    IncompatibleSchemaError,
    ReadOnlyDatabase,
    ReadOnlyUnavailableError,
)


def _temporal_fields(posting_date: date) -> dict[str, object]:
    return {
        "day_of_week": posting_date.weekday(),
        "day_of_month": posting_date.day,
        "week_of_year": posting_date.isocalendar()[1],
        "is_weekend": posting_date.weekday() >= 5,
        "is_month_start": posting_date.day <= 3,
        "is_month_end": posting_date.day >= 28,
    }


def _build_database(path: Path) -> Path:
    run_migrations(path)
    posting_date = date(2025, 1, 15)
    with session_scope(path) as session:
        session.add(
            TransactionDB(
                txn_id="0" * 40,
                posting_date=posting_date,
                transaction_date=posting_date,
                institution="synthetic-bank",
                account="synthetic-account",
                account_type="chequing",
                flow="income",
                cashflow_type="income",
                include_in_cashflow=True,
                currency="CAD",
                amount_raw=Decimal("10.01"),
                amount_signed=Decimal("10.01"),
                description="SYNTHETIC INCOME",
                merchant="SYNTHETIC SOURCE",
                category="Income",
                source_file="synthetic.csv",
                source_path="synthetic/synthetic.csv",
                source_period="2025-01",
                source_row=1,
                imported_at=datetime.now(UTC).replace(tzinfo=None),
                parser_version="synthetic",
                txn_fingerprint="1" * 32,
                **_temporal_fields(posting_date),
            )
        )
    return path


def _artifacts(path: Path) -> dict[str, tuple[bytes, int]]:
    result: dict[str, tuple[bytes, int]] = {}
    for suffix in ("", "-journal", "-wal", "-shm"):
        candidate = Path(f"{path}{suffix}")
        if candidate.exists():
            result[suffix] = (candidate.read_bytes(), candidate.stat().st_mtime_ns)
    return result


def test_preflight_reports_only_aggregate_safe_database_state(tmp_path: Path) -> None:
    db_path = _build_database(tmp_path / "ledger.db")

    snapshot = ReadOnlyDatabase(db_path).preflight()

    assert snapshot.transaction_count == 1
    assert snapshot.earliest_date == date(2025, 1, 15)
    assert snapshot.latest_date == date(2025, 1, 15)
    assert snapshot.schema_compatible is True
    assert snapshot.integrity_ok is True


def test_read_only_session_runs_queries_without_mutating_any_sqlite_artifact(
    tmp_path: Path,
) -> None:
    db_path = _build_database(tmp_path / "ledger.db")
    before = _artifacts(db_path)
    database = ReadOnlyDatabase(db_path)
    database.preflight()

    with database.session_scope(db_path) as session:
        assert session.query(func.count(TransactionDB.txn_id)).scalar() == 1

    assert _artifacts(db_path) == before


def test_read_only_database_close_is_idempotent(tmp_path: Path) -> None:
    db_path = _build_database(tmp_path / "ledger.db")
    database = ReadOnlyDatabase(db_path)
    database.preflight()

    database.close()
    database.close()

    assert database._pinned.descriptor == -1
    assert database._pinned.parent_descriptor == -1


@pytest.mark.parametrize(
    "statement",
    [
        "delete from transactions",
        "update transactions set merchant = 'CHANGED'",
        "create table forbidden(value text)",
        "pragma user_version = 77",
        "pragma wal_checkpoint",
        "attach database ':memory:' as attached",
        "vacuum",
        "reindex",
        "analyze",
    ],
)
def test_read_only_session_denies_mutation_and_transaction_control(
    tmp_path: Path,
    statement: str,
) -> None:
    db_path = _build_database(tmp_path / "ledger.db")
    before = _artifacts(db_path)
    database = ReadOnlyDatabase(db_path)
    database.preflight()

    with pytest.raises(SQLAlchemyError), database.session_scope(db_path) as session:
        session.execute(text(statement))

    assert _artifacts(db_path) == before
    with contextlib.closing(sqlite3.connect(db_path)) as connection, connection:
        assert connection.execute("select merchant from transactions").fetchone() == (
            "SYNTHETIC SOURCE",
        )
        assert connection.execute("pragma user_version").fetchone() == (0,)


def test_missing_and_non_regular_database_paths_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(DatabaseUnavailableError):
        ReadOnlyDatabase(tmp_path / "missing.db")

    directory = tmp_path / "directory.db"
    directory.mkdir()
    with pytest.raises(DatabaseUnavailableError):
        ReadOnlyDatabase(directory)


def test_symlinked_database_or_parent_fails_closed(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    db_path = _build_database(real / "ledger.db")
    file_link = tmp_path / "file-link.db"
    file_link.symlink_to(db_path)
    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(real, target_is_directory=True)

    with pytest.raises(ReadOnlyUnavailableError):
        ReadOnlyDatabase(file_link)
    with pytest.raises(ReadOnlyUnavailableError):
        ReadOnlyDatabase(parent_link / "ledger.db")


def test_wal_mode_and_existing_sidecars_fail_before_read_connection(tmp_path: Path) -> None:
    db_path = _build_database(tmp_path / "ledger.db")
    with contextlib.closing(sqlite3.connect(db_path)) as connection, connection:
        assert connection.execute("pragma journal_mode=wal").fetchone() == ("wal",)
        connection.execute(
            "insert into import_runs values (1, current_timestamp, '', 0, 0, 0, 0, 0, '')"
        )
        connection.commit()
        with pytest.raises(ReadOnlyUnavailableError):
            ReadOnlyDatabase(db_path)

    rollback_path = _build_database(tmp_path / "rollback.db")
    Path(f"{rollback_path}-journal").write_bytes(b"synthetic-sidecar")
    with pytest.raises(ReadOnlyUnavailableError):
        ReadOnlyDatabase(rollback_path)


def test_incompatible_and_corrupt_databases_fail_with_typed_errors(tmp_path: Path) -> None:
    incompatible = tmp_path / "incompatible.db"
    with contextlib.closing(sqlite3.connect(incompatible)) as connection, connection:
        connection.execute("create table unrelated(value text)")

    with pytest.raises(IncompatibleSchemaError):
        ReadOnlyDatabase(incompatible).preflight()

    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"not a sqlite database")
    with pytest.raises(CorruptDatabaseError):
        ReadOnlyDatabase(corrupt)

    malformed_row = _build_database(tmp_path / "malformed-row.db")
    with contextlib.closing(sqlite3.connect(malformed_row)) as connection, connection:
        connection.execute("update transactions set posting_date = 'not-a-date'")
    with pytest.raises(CorruptDatabaseError):
        ReadOnlyDatabase(malformed_row).preflight()


def test_unknown_future_migration_fails_closed(tmp_path: Path) -> None:
    db_path = _build_database(tmp_path / "future-migration.db")
    with contextlib.closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute(
            "insert into schema_migrations(version, description, applied_at) values (?, ?, ?)",
            (
                "99991231_9999_synthetic_future",
                "Synthetic future migration unknown to this Ledger build",
                "9999-12-31 00:00:00",
            ),
        )

    with pytest.raises(IncompatibleSchemaError):
        ReadOnlyDatabase(db_path).preflight()


def test_database_replacement_is_detected_before_a_query(tmp_path: Path) -> None:
    db_path = _build_database(tmp_path / "ledger.db")
    database = ReadOnlyDatabase(db_path)
    database.preflight()
    replacement = _build_database(tmp_path / "replacement.db")
    os.replace(replacement, db_path)

    with pytest.raises(DatabaseChangedError), database.session_scope(db_path):
        pass


def test_parent_directory_replacement_is_detected_before_a_query(tmp_path: Path) -> None:
    live_parent = tmp_path / "live"
    live_parent.mkdir()
    db_path = _build_database(live_parent / "ledger.db")
    database = ReadOnlyDatabase(db_path)
    database.preflight()

    detached_parent = tmp_path / "detached"
    live_parent.rename(detached_parent)
    live_parent.mkdir()
    _build_database(live_parent / "ledger.db")

    with pytest.raises(DatabaseChangedError), database.session_scope(db_path):
        pass
