"""Remove unused transaction fields while preserving transfer pairing.

The four pairing fields still belong to TransferDB and its in-memory model.
The retired days_since_payday field has neither a producer nor a consumer."""

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime
from decimal import Decimal

from ledger.core import Transaction
from ledger.storage.database import TransactionDB, TransferDB, init_database
from ledger.storage.mappers import transfer_from_db
from ledger.storage.migrations import run_migrations, validate_database

DEAD_COLUMNS = (
    "paired_transfer_id",
    "transfer_confidence",
    "counterparty_account",
    "counterparty_institution",
    "days_since_payday",
)

# The four that stay on transfers. days_since_payday was never a transfer concept.
TRANSFER_PAIRING_COLUMNS = DEAD_COLUMNS[:4]

_MIGRATION = "20260810_0023_drop_dead_transaction_columns"


def _column_names(db_path, table: str) -> set[str]:
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def test_orm_no_longer_declares_the_dead_transaction_columns():
    declared = set(TransactionDB.__table__.columns.keys())
    assert declared.isdisjoint(DEAD_COLUMNS), (
        "TransactionDB still declares never-populated columns: "
        f"{sorted(declared & set(DEAD_COLUMNS))}"
    )


def test_transfer_orm_still_declares_every_pairing_column():
    """Transfer orm still declares every pairing column."""
    declared = set(TransferDB.__table__.columns.keys())
    assert declared >= set(TRANSFER_PAIRING_COLUMNS), (
        f"TransferDB lost pairing columns: {sorted(set(TRANSFER_PAIRING_COLUMNS) - declared)}"
    )


def test_transaction_model_drops_days_since_payday_only():
    """Pydantic keeps the pairing fields because transfers reuse this model.

    ``transfer_from_db`` builds a ``Transaction`` from a ``TransferDB`` row and
    ``TransferMatcher.apply_matches`` ``model_copy``-updates exactly these four
    fields, so removing them from the model would break transfer matching even
    though no ``transactions`` row will ever carry them again.
    """
    fields = set(Transaction.model_fields)
    assert "days_since_payday" not in fields, (
        "Transaction still carries days_since_payday, which nothing computes"
    )
    assert fields >= set(TRANSFER_PAIRING_COLUMNS), (
        "Transaction lost transfer-pairing fields the matcher writes: "
        f"{sorted(set(TRANSFER_PAIRING_COLUMNS) - fields)}"
    )


def test_fresh_database_has_no_dead_transaction_columns(tmp_path):
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    assert _column_names(db_path, "transactions").isdisjoint(DEAD_COLUMNS)
    assert _column_names(db_path, "transfers") >= set(TRANSFER_PAIRING_COLUMNS)


def test_transfer_round_trip_still_carries_pairing_values(tmp_path):
    """A stored transfer pairing survives the ORM → model hop unchanged."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    row = TransferDB(
        txn_id="tr-1",
        posting_date=date(2026, 7, 15),
        institution="harbor",
        account="harbor-chequing",
        account_type="Chequing",
        flow="transfer",
        amount_raw=Decimal("-500.00"),
        currency="CAD",
        description="E-TRANSFER OUT",
        source_file="harbor.csv",
        source_path="harbor/harbor.csv",
        imported_at=datetime(2026, 8, 1, tzinfo=UTC).replace(tzinfo=None),
        paired_transfer_id="tr-2",
        transfer_confidence=Decimal("0.95"),
        counterparty_account="harbor-savings",
        counterparty_institution="harbor",
    )
    txn = transfer_from_db(row)
    assert txn.paired_transfer_id == "tr-2"
    assert txn.transfer_confidence == 0.95
    assert txn.counterparty_account == "harbor-savings"
    assert txn.counterparty_institution == "harbor"


def test_migration_drops_dead_columns_and_preserves_rows(tmp_path):
    """Migration 0023 must drop all five from a legacy table without losing rows.

    The pre-migration shape is the current shape plus the five nullable columns, so
    re-adding them to a fully migrated database reproduces the legacy schema
    faithfully. Only 0023 is un-recorded before the second run, so the drop is
    attributable to it and not to 0021's earlier transactions rebuild.
    """
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    assert run_migrations(db_path).ok

    imported_at = datetime(2026, 8, 1, 12, 0, tzinfo=UTC).replace(tzinfo=None).isoformat()
    with closing(sqlite3.connect(db_path)) as conn, conn:
        # Rebuild the pre-0023 shape.
        conn.execute("ALTER TABLE transactions ADD COLUMN paired_transfer_id VARCHAR(40)")
        conn.execute("ALTER TABLE transactions ADD COLUMN transfer_confidence NUMERIC(3, 2)")
        conn.execute("ALTER TABLE transactions ADD COLUMN counterparty_account VARCHAR(100)")
        conn.execute("ALTER TABLE transactions ADD COLUMN counterparty_institution VARCHAR(50)")
        conn.execute("ALTER TABLE transactions ADD COLUMN days_since_payday INTEGER")
        conn.execute(
            "INSERT INTO transactions ("
            "  txn_id, posting_date, transaction_date, institution, account, account_type,"
            "  flow, cashflow_type, include_in_cashflow, currency, amount_raw, amount_signed,"
            "  description, merchant, category, merchant_confidence, category_confidence,"
            "  exclude_from_spending, merchant_raw, category_raw, source_file, source_path,"
            "  source_period, source_row, imported_at, parser_version, txn_fingerprint,"
            "  recurring_pattern_id, day_of_week, day_of_month,"
            "  week_of_year, is_weekend, is_month_start, is_month_end,"
            "  paired_transfer_id, transfer_confidence, counterparty_account,"
            "  counterparty_institution, days_since_payday"
            ") VALUES ("
            "  'txn-legacy-1', '2026-07-15', '2026-07-14', 'harbor', 'chequing', 'chequing',"
            "  'expense', 'expense', 1, 'CAD', 42.5, -42.5,"
            "  'LEGACY COFFEE', 'Coffee Shop', 'Dining', 'high', 'high',"
            "  0, 'LEGACY COFFEE', 'Dining', 'stmt.pdf', '/statements/stmt.pdf',"
            "  '2026-07', 7, ?, 'harbor-1.0.0', 'f0000000000000000000000000000001',"
            "  NULL, 1, 15,"
            "  29, 0, 0, 0,"
            "  'txn-legacy-2', 0.95, 'harbor-savings',"
            "  'harbor', 3"
            ")",
            (imported_at,),
        )

    assert _column_names(db_path, "transactions") >= set(DEAD_COLUMNS)

    # Make only 0023 pending again so it re-runs against the legacy shape.
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("DELETE FROM schema_migrations WHERE version = ?", (_MIGRATION,))

    result = run_migrations(db_path)
    assert result.ok, result.errors
    assert result.details["applied_versions"] == [_MIGRATION], result.details

    assert _column_names(db_path, "transactions").isdisjoint(DEAD_COLUMNS), (
        "migration 0023 left dead columns on transactions"
    )
    assert _column_names(db_path, "transfers") >= set(TRANSFER_PAIRING_COLUMNS), (
        "migration 0023 must not touch the transfers table"
    )

    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT txn_id, posting_date, transaction_date, institution, account, flow,"
            "       currency, amount_raw, amount_signed, description, merchant, category,"
            "       source_file, source_period, source_row, parser_version, day_of_month,"
            "       is_month_end "
            "FROM transactions WHERE txn_id = 'txn-legacy-1'"
        ).fetchone()
    assert row is not None, "migration 0023 lost the pre-existing transaction row"
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
        0,
    )

    # The rebuilt table is the ORM's own DDL, so validation must be clean afterwards.
    assert validate_database(db_path, record=False).status == "success"
