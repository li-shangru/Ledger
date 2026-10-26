"""Genuine old-schema upgrade-path tests for every guarded ALTER branch.

Each test:
  1. Calls init_database() to create the fully-current schema.
  2. Tears the target table down to its PRE-migration shape (renames, recreates
     WITHOUT the new column, copies rows back, drops backup).
  3. Inserts a representative row into the old-shaped table.
  4. Deletes the migration's row from schema_migrations so run_migrations()
     considers it PENDING.
  5. Calls run_migrations().
  6. Asserts:
     (a) the pre-existing row still exists  (data preserved)
     (b) PRAGMA table_info now includes the added column  (ALTER ran)

If the ALTER branch were deleted from migrations.py, step 6-b would fail
because the column would remain absent after run_migrations() returns.

Migrations under test
---------------------
  20260513_0002_finance_cockpit     → report_snapshots ADD COLUMN source_kind,
                                                        ADD COLUMN schema_version
  20260514_0004_account_linked_goals → savings_goals ADD COLUMN account_id
  20260603_0005_transfer_source_path → transfers ADD COLUMN source_path
"""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest

from ledger.storage.database import init_database
from ledger.storage.migrations import run_migrations

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _column_names(db_path, table: str) -> set[str]:
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def _mark_pending(db_path, migration_version: str) -> None:
    """Remove a migration record so run_migrations() treats it as pending."""
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            "DELETE FROM schema_migrations WHERE version = ?",
            (migration_version,),
        )


# ---------------------------------------------------------------------------
# 1. report_snapshots: ADD COLUMN source_kind + schema_version
#    migration id: 20260513_0002_finance_cockpit
# ---------------------------------------------------------------------------


def test_phase_four_alters_add_columns_to_old_report_snapshots(tmp_path):
    """Migration 0002 must ADD source_kind and schema_version to a pre-migration
    report_snapshots table and preserve pre-existing rows."""
    db_path = tmp_path / "ledger.db"

    # 1. Build current schema so all OTHER tables exist and have their
    #    migration rows recorded.
    init_database(db_path)

    now_iso = datetime.now(UTC).replace(tzinfo=None).isoformat()

    # 2. Tear report_snapshots down to its pre-0002 shape (no source_kind, no schema_version).
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("DROP TABLE IF EXISTS report_snapshots_bak")
        conn.execute("ALTER TABLE report_snapshots RENAME TO report_snapshots_bak")
        conn.execute(
            "CREATE TABLE report_snapshots ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  created_at DATETIME NOT NULL,"
            "  source VARCHAR(80) NOT NULL DEFAULT 'manual',"
            "  period_start DATE,"
            "  period_end DATE,"
            "  status VARCHAR(30) NOT NULL DEFAULT 'success',"
            "  payload JSON NOT NULL DEFAULT '{}'"
            ")"
        )
        # Copy rows that exist in the narrower schema
        conn.execute(
            "INSERT INTO report_snapshots (id, created_at, source, period_start, period_end, status, payload) "
            "SELECT id, created_at, source, period_start, period_end, status, payload "
            "FROM report_snapshots_bak"
        )
        conn.execute("DROP TABLE report_snapshots_bak")

    # 3. Insert a representative row into the old-shaped table.
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO report_snapshots (created_at, source, status, payload) "
            "VALUES (?, 'manual', 'success', '{}')",
            (now_iso,),
        )
        inserted_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Sanity: the new columns must NOT exist yet.
    assert "source_kind" not in _column_names(db_path, "report_snapshots")
    assert "schema_version" not in _column_names(db_path, "report_snapshots")

    # 4. Mark the migration as pending (delete its schema_migrations record).
    _mark_pending(db_path, "20260513_0002_finance_cockpit")

    # 5. Run migrations.
    result = run_migrations(db_path)

    # 6a. Pre-existing row still present.
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT id, source FROM report_snapshots WHERE id = ?", (inserted_id,)
        ).fetchone()
    assert row is not None, "Pre-existing report_snapshots row was lost during migration"
    assert row[1] == "manual"

    # 6b. Both new columns are now present.
    cols = _column_names(db_path, "report_snapshots")
    assert "source_kind" in cols, (
        "migration 0002 ALTER branch did not add source_kind to report_snapshots"
    )
    assert "schema_version" in cols, (
        "migration 0002 ALTER branch did not add schema_version to report_snapshots"
    )

    # Migration must have been recorded as applied.
    assert "20260513_0002_finance_cockpit" in result.details["applied_versions"]


# ---------------------------------------------------------------------------
# 2. savings_goals: ADD COLUMN account_id
#    migration id: 20260514_0004_account_linked_goals
# ---------------------------------------------------------------------------


def test_account_linked_goals_alter_adds_account_id_to_old_savings_goals(tmp_path):
    """Migration 0004 must ADD account_id to a pre-migration savings_goals table
    and preserve pre-existing goal rows (legacy rows keep account_id NULL)."""
    db_path = tmp_path / "ledger.db"

    # 1. Build current schema.
    init_database(db_path)

    now_iso = datetime.now(UTC).replace(tzinfo=None).isoformat()

    # 2. Tear savings_goals down to its pre-0004 shape (no account_id column).
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("DROP TABLE IF EXISTS savings_goals_bak")
        conn.execute("ALTER TABLE savings_goals RENAME TO savings_goals_bak")
        conn.execute(
            "CREATE TABLE savings_goals ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  name VARCHAR(160) NOT NULL,"
            "  target_amount NUMERIC(12,2) NOT NULL,"
            "  target_date DATE,"
            "  status VARCHAR(30) NOT NULL DEFAULT 'active',"
            "  notes TEXT NOT NULL DEFAULT '',"
            "  created_at DATETIME NOT NULL,"
            "  updated_at DATETIME NOT NULL"
            ")"
        )
        # Copy only columns that existed in the old schema.
        conn.execute(
            "INSERT INTO savings_goals (id, name, target_amount, target_date, status, notes, created_at, updated_at) "
            "SELECT id, name, target_amount, target_date, status, notes, created_at, updated_at "
            "FROM savings_goals_bak"
        )
        conn.execute("DROP TABLE savings_goals_bak")

    # 3. Insert a representative old-schema row.
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO savings_goals (name, target_amount, status, notes, created_at, updated_at) "
            "VALUES ('Emergency Fund', '10000.00', 'active', '', ?, ?)",
            (now_iso, now_iso),
        )
        inserted_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]

    # Sanity: column must NOT exist yet.
    assert "account_id" not in _column_names(db_path, "savings_goals")

    # 4. Mark migration 0004 as pending.
    _mark_pending(db_path, "20260514_0004_account_linked_goals")

    # 5. Run migrations.
    result = run_migrations(db_path)

    # 6a. Pre-existing row still present.
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT id, name FROM savings_goals WHERE id = ?", (inserted_id,)
        ).fetchone()
    assert row is not None, "Pre-existing savings_goals row was lost during migration"
    assert row[1] == "Emergency Fund"

    # 6b. account_id column is now present.
    cols = _column_names(db_path, "savings_goals")
    assert "account_id" in cols, (
        "migration 0004 ALTER branch did not add account_id to savings_goals"
    )

    # 6c. The migrated row has account_id NULL (legacy rows are preserved, not wiped).
    with closing(sqlite3.connect(db_path)) as conn:
        account_id_val = conn.execute(
            "SELECT account_id FROM savings_goals WHERE id = ?", (inserted_id,)
        ).fetchone()[0]
    assert account_id_val is None, (
        f"Legacy row should have account_id=NULL after migration, got {account_id_val!r}"
    )

    assert "20260514_0004_account_linked_goals" in result.details["applied_versions"]


# ---------------------------------------------------------------------------
# 3. transfers: ADD COLUMN source_path
#    migration id: 20260603_0005_transfer_source_path
# ---------------------------------------------------------------------------


def test_transfer_source_path_alter_adds_column_to_old_transfers(tmp_path):
    """Migration 0005 must ADD source_path to a pre-migration transfers table
    and preserve pre-existing transfer rows."""
    db_path = tmp_path / "ledger.db"

    # 1. Build current schema.
    init_database(db_path)

    now_iso = datetime.now(UTC).replace(tzinfo=None).isoformat()

    # 2. Tear transfers down to its pre-0005 shape (no source_path column).
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("DROP TABLE IF EXISTS transfers_bak")
        conn.execute("ALTER TABLE transfers RENAME TO transfers_bak")
        conn.execute(
            "CREATE TABLE transfers ("
            "  txn_id VARCHAR(16) PRIMARY KEY,"
            "  posting_date DATE NOT NULL,"
            "  institution VARCHAR(50) NOT NULL,"
            "  account VARCHAR(100) NOT NULL,"
            "  account_type VARCHAR(100) NOT NULL DEFAULT '',"
            "  flow VARCHAR(20) NOT NULL,"
            "  amount_raw NUMERIC(12,2) NOT NULL,"
            "  description TEXT NOT NULL,"
            "  source_file VARCHAR(255) NOT NULL,"
            "  imported_at DATETIME NOT NULL,"
            "  paired_transfer_id VARCHAR(16),"
            "  transfer_confidence NUMERIC(3,2),"
            "  counterparty_account VARCHAR(100),"
            "  counterparty_institution VARCHAR(50)"
            ")"
        )
        # Copy only the columns present in the old schema.
        conn.execute(
            "INSERT INTO transfers "
            "  (txn_id, posting_date, institution, account, account_type, flow, "
            "   amount_raw, description, source_file, imported_at, "
            "   paired_transfer_id, transfer_confidence, counterparty_account, counterparty_institution) "
            "SELECT "
            "  txn_id, posting_date, institution, account, account_type, flow, "
            "  amount_raw, description, source_file, imported_at, "
            "  paired_transfer_id, transfer_confidence, counterparty_account, counterparty_institution "
            "FROM transfers_bak"
        )
        conn.execute("DROP TABLE transfers_bak")

    # 3. Insert a representative old-schema row.
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO transfers "
            "  (txn_id, posting_date, institution, account, account_type, flow, "
            "   amount_raw, description, source_file, imported_at) "
            "VALUES (?, '2026-01-15', 'HARBOR', 'Chequing', 'chequing', 'debit', "
            "        '-500.00', 'Transfer to savings', 'harbor-2026-01.csv', ?)",
            ("txn-test-001", now_iso),
        )

    # Sanity: column must NOT exist yet.
    assert "source_path" not in _column_names(db_path, "transfers")

    # 4. Mark migration 0005 as pending.
    _mark_pending(db_path, "20260603_0005_transfer_source_path")

    # 5. Run migrations.
    result = run_migrations(db_path)

    # 6a. Pre-existing row still present.
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT txn_id, description FROM transfers WHERE txn_id = ?",
            ("txn-test-001",),
        ).fetchone()
    assert row is not None, "Pre-existing transfers row was lost during migration"
    assert row[1] == "Transfer to savings"

    # 6b. source_path column is now present.
    cols = _column_names(db_path, "transfers")
    assert "source_path" in cols, "migration 0005 ALTER branch did not add source_path to transfers"

    # 6c. The migrated row has source_path NULL (nullable column, no backfill required).
    with closing(sqlite3.connect(db_path)) as conn:
        sp_val = conn.execute(
            "SELECT source_path FROM transfers WHERE txn_id = ?", ("txn-test-001",)
        ).fetchone()[0]
    assert sp_val is None, (
        f"source_path should be NULL on pre-existing row after migration, got {sp_val!r}"
    )

    assert "20260603_0005_transfer_source_path" in result.details["applied_versions"]


# ---------------------------------------------------------------------------
# 4. transfers: ADD COLUMN currency (NOT NULL DEFAULT 'CAD')
#    migration id: 20260703_0008_transfer_currency
# ---------------------------------------------------------------------------


def test_transfer_currency_alter_adds_column_and_backfills_cad(tmp_path):
    """Migration 0008 must ADD currency to a pre-migration transfers table, preserve
    pre-existing rows, and backfill them to 'CAD' (the is_cad legacy default) so the
    new CAD-only balance/credit-limit guards leave historical all-CAD data unchanged."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    now_iso = datetime.now(UTC).replace(tzinfo=None).isoformat()

    # Tear transfers down to its pre-0008 shape (has source_path, no currency).
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("DROP TABLE IF EXISTS transfers_bak")
        conn.execute("ALTER TABLE transfers RENAME TO transfers_bak")
        conn.execute(
            "CREATE TABLE transfers ("
            "  txn_id VARCHAR(40) PRIMARY KEY,"
            "  posting_date DATE NOT NULL,"
            "  institution VARCHAR(50) NOT NULL,"
            "  account VARCHAR(100) NOT NULL,"
            "  account_type VARCHAR(100) NOT NULL DEFAULT '',"
            "  flow VARCHAR(20) NOT NULL,"
            "  amount_raw NUMERIC(12,2) NOT NULL,"
            "  description TEXT NOT NULL,"
            "  source_file VARCHAR(255) NOT NULL,"
            "  source_path VARCHAR(500),"
            "  imported_at DATETIME NOT NULL,"
            "  paired_transfer_id VARCHAR(40),"
            "  transfer_confidence NUMERIC(3,2),"
            "  counterparty_account VARCHAR(100),"
            "  counterparty_institution VARCHAR(50)"
            ")"
        )
        conn.execute("DROP TABLE transfers_bak")
        conn.execute(
            "INSERT INTO transfers "
            "  (txn_id, posting_date, institution, account, account_type, flow, "
            "   amount_raw, description, source_file, imported_at) "
            "VALUES (?, '2026-01-15', 'HARBOR', 'Chequing', 'chequing', 'debit', "
            "        '-500.00', 'Transfer to savings', 'harbor-2026-01.csv', ?)",
            ("txn-cur-001", now_iso),
        )

    assert "currency" not in _column_names(db_path, "transfers")

    _mark_pending(db_path, "20260703_0008_transfer_currency")
    result = run_migrations(db_path)

    cols = _column_names(db_path, "transfers")
    assert "currency" in cols, "migration 0008 did not add currency to transfers"

    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT description, currency FROM transfers WHERE txn_id = ?", ("txn-cur-001",)
        ).fetchone()
    assert row is not None, "Pre-existing transfers row was lost during migration"
    assert row[0] == "Transfer to savings"
    assert row[1] == "CAD", f"legacy leg should backfill to CAD, got {row[1]!r}"

    assert "20260703_0008_transfer_currency" in result.details["applied_versions"]


def _index_names(db_path, table: str) -> set[str]:
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
    return {str(row[1]) for row in rows}


def test_confirmed_pattern_link_uniqueness_migration_preserves_valid_rows(tmp_path):
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    migration = "20260903_0026_unique_confirmed_schedule_pattern"
    index = "ux_planned_schedule_confirmed_pattern"
    now_iso = datetime.now(UTC).replace(tzinfo=None).isoformat()
    statement = (
        "INSERT INTO planned_schedules "
        "(schedule_id, name, event_type, amount, currency, account_id, next_date, "
        "frequency, provenance, pattern_id, is_active, note, created_at, updated_at) "
        "VALUES (?, 'Utility', 'bill', 100, 'CAD', 'account-a', '2026-10-01', "
        "'monthly', ?, 'detected-utility', 1, '', ?, ?)"
    )
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(f"DROP INDEX IF EXISTS {index}")
        conn.execute(statement, ("confirmed", "confirmed", now_iso, now_iso))
        conn.execute(statement, ("hypothetical", "hypothetical", now_iso, now_iso))
    _mark_pending(db_path, migration)

    result = run_migrations(db_path)

    assert migration in result.details["applied_versions"]
    assert index in _index_names(db_path, "planned_schedules")
    with closing(sqlite3.connect(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM planned_schedules").fetchone() == (2,)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(statement, ("duplicate", "confirmed", now_iso, now_iso))


def test_confirmed_pattern_link_uniqueness_migration_fails_closed_on_duplicates(tmp_path):
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    migration = "20260903_0026_unique_confirmed_schedule_pattern"
    index = "ux_planned_schedule_confirmed_pattern"
    now_iso = datetime.now(UTC).replace(tzinfo=None).isoformat()
    statement = (
        "INSERT INTO planned_schedules "
        "(schedule_id, name, event_type, amount, currency, account_id, next_date, "
        "frequency, provenance, pattern_id, is_active, note, created_at, updated_at) "
        "VALUES (?, 'Utility', 'bill', 100, 'CAD', 'account-a', '2026-10-01', "
        "'monthly', 'confirmed', 'detected-utility', 1, '', ?, ?)"
    )
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(f"DROP INDEX IF EXISTS {index}")
        conn.execute(statement, ("first", now_iso, now_iso))
        conn.execute(statement, ("second", now_iso, now_iso))
    _mark_pending(db_path, migration)

    with pytest.raises(ValueError, match="duplicates are resolved"):
        run_migrations(db_path)

    with closing(sqlite3.connect(db_path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM planned_schedules").fetchone() == (2,)
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = ?", (migration,)
        ).fetchone() == (0,)
    assert index not in _index_names(db_path, "planned_schedules")


def test_phase_four_creates_report_snapshot_single_column_indexes_on_migrated_db(tmp_path):
    """F19: create_all does not add indexes to an EXISTING table, so a legacy-migrated
    report_snapshots silently diverges from a fresh DB. The migration must CREATE the
    ORM's single-column indexes (date / source_kind / schema_version)."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    now_iso = datetime.now(UTC).replace(tzinfo=None).isoformat()

    # Tear report_snapshots to a bare pre-phase-four shape: no source_kind/schema_version
    # columns and NO single-column indexes.
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("DROP TABLE IF EXISTS report_snapshots_bak")
        conn.execute("ALTER TABLE report_snapshots RENAME TO report_snapshots_bak")
        conn.execute(
            "CREATE TABLE report_snapshots ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  created_at DATETIME NOT NULL,"
            "  source VARCHAR(80) NOT NULL DEFAULT 'manual',"
            "  period_start DATE, period_end DATE,"
            "  status VARCHAR(30) NOT NULL DEFAULT 'success',"
            "  payload JSON NOT NULL DEFAULT '{}'"
            ")"
        )
        conn.execute("DROP TABLE report_snapshots_bak")
        conn.execute(
            "INSERT INTO report_snapshots (created_at, source, status, payload) "
            "VALUES (?, 'manual', 'success', '{}')",
            (now_iso,),
        )

    fresh = tmp_path / "fresh.db"
    init_database(fresh)
    expected = {
        "ix_report_snapshot_date",
        "ix_report_snapshots_source_kind",
        "ix_report_snapshots_schema_version",
    }
    assert expected <= _index_names(fresh, "report_snapshots"), "fresh DB must declare these"
    assert not (expected & _index_names(db_path, "report_snapshots")), "legacy table has none yet"

    _mark_pending(db_path, "20260513_0002_finance_cockpit")
    run_migrations(db_path)

    migrated = _index_names(db_path, "report_snapshots")
    assert expected <= migrated, f"migration must add {expected - migrated}"


# ---------------------------------------------------------------------------
# 5. financial_health_scores: relax emergency_fund_months / debt_ratio to nullable
#    migration id: 20260704_0009_health_score_nullable_metrics
# ---------------------------------------------------------------------------


def _notnull_names(db_path, table: str) -> set[str]:
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows if int(row[3]) == 1}


def test_health_score_metrics_relaxed_to_nullable_on_old_db(tmp_path):
    """Migration 0009 must relax financial_health_scores.emergency_fund_months and
    debt_ratio from NOT NULL to nullable on a legacy DB, preserve existing rows, and
    allow a NULL write afterwards (the new "not assessable" sentinel)."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    now_iso = datetime.now(UTC).replace(tzinfo=None).isoformat()

    # Tear financial_health_scores down to its pre-0009 shape: the two metrics NOT NULL.
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("DROP TABLE IF EXISTS financial_health_scores")
        conn.execute(
            "CREATE TABLE financial_health_scores ("
            "  score_date DATE NOT NULL PRIMARY KEY,"
            "  overall_score INTEGER NOT NULL,"
            "  cashflow_health INTEGER NOT NULL,"
            "  spending_consistency INTEGER NOT NULL,"
            "  savings_rate NUMERIC(5,2) NOT NULL,"
            "  emergency_fund_months NUMERIC(5,2) NOT NULL,"
            "  debt_ratio NUMERIC(5,2) NOT NULL,"
            "  score_trend_7d VARCHAR(20) NOT NULL,"
            "  score_change_7d INTEGER NOT NULL,"
            "  risk_factors TEXT NOT NULL,"
            "  computed_at DATETIME NOT NULL"
            ")"
        )
        conn.execute(
            "INSERT INTO financial_health_scores VALUES "
            "('2026-01-14', 82, 75, 80, '60.00', '8.00', '0.10', 'stable', 0, '', ?)",
            (now_iso,),
        )

    # Sanity: the two metrics are NOT NULL in the legacy table.
    assert {"emergency_fund_months", "debt_ratio"} <= _notnull_names(
        db_path, "financial_health_scores"
    )

    _mark_pending(db_path, "20260704_0009_health_score_nullable_metrics")
    result = run_migrations(db_path)
    assert "20260704_0009_health_score_nullable_metrics" in result.details["applied_versions"]

    # 6a. The two metrics are now nullable.
    notnull = _notnull_names(db_path, "financial_health_scores")
    assert "emergency_fund_months" not in notnull
    assert "debt_ratio" not in notnull

    # 6b. Pre-existing row preserved with its original values.
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT overall_score, emergency_fund_months FROM financial_health_scores "
            "WHERE score_date = '2026-01-14'"
        ).fetchone()
    assert row is not None, "Pre-existing health-score row was lost during migration"
    assert row[0] == 82
    # SQLite NUMERIC affinity collapses 8.00 -> 8; compare numerically, not textually.
    assert float(row[1]) == 8.0

    # 6c. A NULL write now succeeds (the "not assessable" sentinel).
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO financial_health_scores VALUES "
            "('2026-01-15', 90, 100, 90, '75.00', NULL, NULL, 'stable', 0, '', ?)",
            (now_iso,),
        )
        got = conn.execute(
            "SELECT emergency_fund_months, debt_ratio FROM financial_health_scores "
            "WHERE score_date = '2026-01-15'"
        ).fetchone()
    assert got == (None, None)

    # 6d. The overall_score index the ORM declares is present on the rebuilt table.
    assert "ix_financial_health_scores_overall_score" in _index_names(
        db_path, "financial_health_scores"
    )
