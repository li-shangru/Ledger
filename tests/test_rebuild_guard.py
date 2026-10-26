"""Format-independent safety gate for ``scripts/rebuild-db``.

The rebuild script must refuse to promote a freshly-built DB that either has no
rows or recorded any parse exceptions. The check reads ``import_runs.errors``
(persisted regardless of output FORMATS) rather than an ``exceptions_all.csv``
that is only emitted when CSV output is enabled.
"""

import contextlib
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from ledger.maintenance.rebuild_guard import check_rebuild_safe
from ledger.storage.database import (
    ImportRunDB,
    TransactionDB,
    TransferDB,
    init_database,
    session_scope,
)


def _temporal_fields(posting_date: date) -> dict:
    return {
        "day_of_week": posting_date.weekday(),
        "day_of_month": posting_date.day,
        "week_of_year": posting_date.isocalendar()[1],
        "is_weekend": posting_date.weekday() >= 5,
        "is_month_start": posting_date.day <= 3,
        "is_month_end": posting_date.day >= 28,
    }


def _add_transaction(session, txn_id: str) -> None:
    posting_date = date(2025, 1, 15)
    session.add(
        TransactionDB(
            txn_id=txn_id,
            posting_date=posting_date,
            transaction_date=posting_date,
            institution="harbor",
            account="chequing",
            flow="expense",
            cashflow_type="expense",
            include_in_cashflow=True,
            amount_raw=Decimal("100.00"),
            amount_signed=Decimal("-100.00"),
            description="TEST TRANSACTION",
            merchant="TEST MERCHANT",
            category="Test",
            source_file="test.csv",
            source_path="test/test.csv",
            source_row=1,
            imported_at=datetime.now(UTC).replace(tzinfo=None),
            parser_version="test",
            txn_fingerprint=f"fp-{txn_id}",
            **_temporal_fields(posting_date),
        )
    )


def _add_import_run(session, *, errors: int, transactions: int = 1) -> None:
    session.add(
        ImportRunDB(
            run_at=datetime.now(UTC).replace(tzinfo=None),
            source_path="data/csv",
            files_processed=1,
            transactions_imported=transactions,
            transfers_imported=0,
            duplicates_skipped=0,
            errors=errors,
            parser_version="test",
        )
    )


def test_rebuild_guard_unsafe_when_import_run_has_errors(tmp_path: Path) -> None:
    """Case A (regression): errors>0 with rows present -> UNSAFE.

    This is exactly the partial-loss build the old CSV-based gate missed when
    CSV output was not emitted.
    """
    db_path = tmp_path / "rebuilt.db"
    init_database(db_path)
    with session_scope(db_path) as session:
        _add_transaction(session, "txn-a")
        _add_import_run(session, errors=3)

    result = check_rebuild_safe(db_path)

    assert result.ok is False
    assert result.exceptions == 3
    assert "3 exceptions" in result.message


def test_rebuild_guard_safe_when_no_errors_and_rows_present(tmp_path: Path) -> None:
    """Case B: errors==0 and rows>0 -> SAFE."""
    db_path = tmp_path / "rebuilt.db"
    init_database(db_path)
    with session_scope(db_path) as session:
        _add_transaction(session, "txn-b")
        _add_import_run(session, errors=0)

    result = check_rebuild_safe(db_path)

    assert result.ok is True
    assert result.exceptions == 0
    assert result.transactions == 1
    assert result.transfers == 0
    assert "1 transactions" in result.message


def test_rebuild_guard_safe_counts_transfers(tmp_path: Path) -> None:
    """Case B variant: transfers alone (no transactions) still count as rows."""
    db_path = tmp_path / "rebuilt.db"
    init_database(db_path)
    posting_date = date(2025, 1, 15)
    with session_scope(db_path) as session:
        session.add(
            TransferDB(
                txn_id="xfer-1",
                posting_date=posting_date,
                institution="harbor",
                account="chequing",
                flow="transfer",
                amount_raw=Decimal("50.00"),
                description="TRANSFER",
                source_file="test.csv",
                imported_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )
        _add_import_run(session, errors=0, transactions=0)

    result = check_rebuild_safe(db_path)

    assert result.ok is True
    assert result.transactions == 0
    assert result.transfers == 1


def test_rebuild_guard_unsafe_when_zero_rows(tmp_path: Path) -> None:
    """Case C: zero transactions and transfers -> UNSAFE (zero-rows)."""
    db_path = tmp_path / "rebuilt.db"
    init_database(db_path)
    with session_scope(db_path) as session:
        _add_import_run(session, errors=0, transactions=0)

    result = check_rebuild_safe(db_path)

    assert result.ok is False
    assert result.transactions == 0
    assert result.transfers == 0
    assert "zero transactions" in result.message


def test_rebuild_guard_fails_closed_without_import_runs_table(tmp_path: Path) -> None:
    """Case D: import_runs table absent -> UNSAFE (fail closed)."""
    import sqlite3

    db_path = tmp_path / "bare.db"
    with contextlib.closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("create table transactions (txn_id text)")
        conn.execute("create table transfers (txn_id text)")
        conn.execute("insert into transactions (txn_id) values ('x')")

    result = check_rebuild_safe(db_path)

    assert result.ok is False
