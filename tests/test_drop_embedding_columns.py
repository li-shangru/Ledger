"""The dead transaction embedding columns are gone -- and dropping them is lossless.

``transactions.embedding`` / ``embedding_version`` were declared for a similarity
search that was never built. Both assignment sites hardcoded None and no consumer
ever read them back, so retiring them costs nothing. These tests pin the two halves
of that removal: the ORM/model no longer declare the columns, and migration
20260809_0021 rebuilds a legacy table without them while preserving every row.
"""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime

from ledger.core import Transaction
from ledger.storage.database import TransactionDB, init_database
from ledger.storage.migrations import run_migrations, validate_database

DEAD_COLUMNS = ("embedding", "embedding_version")

_MIGRATION = "20260809_0021_statement_coverage_and_dead_columns"


def _column_names(db_path, table: str) -> set[str]:
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def test_orm_no_longer_declares_embedding_columns():
    declared = set(TransactionDB.__table__.columns.keys())
    assert declared.isdisjoint(DEAD_COLUMNS), (
        "TransactionDB still declares the never-populated embedding columns"
    )


def test_transaction_model_no_longer_declares_embedding_fields():
    assert set(Transaction.model_fields).isdisjoint(DEAD_COLUMNS), (
        "Transaction still carries the never-populated embedding fields"
    )


def test_fresh_database_has_no_embedding_columns(tmp_path):
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    assert _column_names(db_path, "transactions").isdisjoint(DEAD_COLUMNS)


def test_migration_drops_embedding_columns_and_preserves_rows(tmp_path):
    """Migration 0021 must drop both columns from a legacy table without losing rows.

    The pre-migration shape is exactly the current shape plus the two nullable
    embedding columns, so re-adding them to a fresh database reproduces a legacy
    schema faithfully. The row carries values in every other column so a botched
    create-copy-drop-rename shows up as changed data, not just a missing row.
    """
    db_path = tmp_path / "ledger.db"
    init_database(db_path)

    imported_at = datetime(2026, 8, 1, 12, 0, tzinfo=UTC).replace(tzinfo=None).isoformat()
    with closing(sqlite3.connect(db_path)) as conn, conn:
        # Rebuild the pre-0021 shape.
        conn.execute("ALTER TABLE transactions ADD COLUMN embedding_version VARCHAR(20)")
        conn.execute("ALTER TABLE transactions ADD COLUMN embedding TEXT")
        conn.execute(
            "INSERT INTO transactions ("
            "  txn_id, posting_date, transaction_date, institution, account, account_type,"
            "  flow, cashflow_type, include_in_cashflow, currency, amount_raw, amount_signed,"
            "  description, merchant, category, merchant_confidence, category_confidence,"
            "  exclude_from_spending, merchant_raw, category_raw, source_file, source_path,"
            "  source_period, source_row, imported_at, parser_version, txn_fingerprint,"
            "  recurring_pattern_id, day_of_week, day_of_month,"
            "  week_of_year, is_weekend, is_month_start, is_month_end,"
            "  embedding_version, embedding"
            ") VALUES ("
            "  'txn-legacy-1', '2026-07-15', '2026-07-14', 'harbor', 'chequing', 'chequing',"
            "  'expense', 'expense', 1, 'CAD', 42.5, -42.5,"
            "  'LEGACY COFFEE', 'Coffee Shop', 'Dining', 'high', 'high',"
            "  0, 'LEGACY COFFEE', 'Dining', 'stmt.pdf', '/statements/stmt.pdf',"
            "  '2026-07', 7, ?, 'harbor-1.0.0', 'f0000000000000000000000000000001',"
            "  NULL, 1, 15,"
            "  29, 0, 0, 0,"
            "  'v1', '[0.1, 0.2]'"
            ")",
            (imported_at,),
        )

    assert _column_names(db_path, "transactions") >= set(DEAD_COLUMNS)

    # Make the migration pending again so it re-runs against the legacy shape.
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("DELETE FROM schema_migrations WHERE version = ?", (_MIGRATION,))

    result = run_migrations(db_path)
    assert result.ok, result.errors
    assert _MIGRATION in result.details["applied_versions"]

    assert _column_names(db_path, "transactions").isdisjoint(DEAD_COLUMNS), (
        "migration 0021 left the dead embedding columns in place"
    )

    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT txn_id, posting_date, transaction_date, institution, account, flow,"
            "       currency, amount_raw, amount_signed, description, merchant, category,"
            "       source_file, source_period, source_row, parser_version, day_of_month,"
            "       week_of_year "
            "FROM transactions WHERE txn_id = 'txn-legacy-1'"
        ).fetchone()
    assert row is not None, "migration 0021 lost the pre-existing transaction row"
    assert row == (
        "txn-legacy-1",
        "2026-07-15",
        "2026-07-14",
        "harbor",
        "chequing",
        "expense",
        "CAD",
        42.5,
        -42.5,
        "LEGACY COFFEE",
        "Coffee Shop",
        "Dining",
        "stmt.pdf",
        "2026-07",
        7,
        "harbor-1.0.0",
        15,
        29,
    )

    # The rebuilt table is the ORM's own DDL, so validation must be clean afterwards.
    assert validate_database(db_path, record=False).status == "success"
