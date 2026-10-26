"""Lightweight local SQLite migration runner."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import MetaData, inspect
from sqlalchemy.dialects.sqlite import dialect as sqlite_dialect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.schema import CreateIndex, CreateTable

from ledger.core.results import RunResult
from ledger.storage.database import (
    Base,
    SchemaMigrationDB,
    clear_engine_cache,
    get_engine,
    init_database,
    session_scope,
)
from ledger.storage.exact_decimal import (
    INVESTMENT_EXACT_DECIMAL_COLUMNS,
    canonical_fixed_point_text,
    decimal_from_canonical_text,
)
from ledger.storage.operations import record_db_operation
from ledger.utils.dates import naive_utc_now


@dataclass(frozen=True)
class Migration:
    version: str
    description: str
    apply: Callable[[Path], None]


def _create_phase_three_tables(db_path: Path) -> None:
    init_database(db_path)


def _column_names(db_path: Path, table: str) -> set[str]:
    with closing(sqlite3.connect(db_path)) as conn:
        # table is a trusted internal constant; PRAGMA can't be parameter-bound.
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {str(row[1]) for row in rows}


def _ensure_phase_four_tables(db_path: Path) -> None:
    init_database(db_path)
    snapshot_columns = _column_names(db_path, "report_snapshots")
    with closing(sqlite3.connect(db_path)) as conn, conn:
        if "source_kind" not in snapshot_columns:
            conn.execute(
                "ALTER TABLE report_snapshots "
                "ADD COLUMN source_kind VARCHAR(40) NOT NULL DEFAULT 'reporting'"
            )
        if "schema_version" not in snapshot_columns:
            conn.execute(
                "ALTER TABLE report_snapshots ADD COLUMN schema_version INTEGER NOT NULL DEFAULT 3"
            )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_report_snapshot_period_schema_source "
            "ON report_snapshots(period_start, period_end, schema_version, source_kind)"
        )
        # Single-column indexes the ORM declares (create_all does NOT add indexes to an
        # already-existing table, so an in-place-migrated report_snapshots would
        # otherwise silently lack these vs a fresh DB).
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_report_snapshot_date ON report_snapshots(created_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_report_snapshots_source_kind "
            "ON report_snapshots(source_kind)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_report_snapshots_schema_version "
            "ON report_snapshots(schema_version)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_insight_period_type_status "
            "ON insight_findings(period_start, period_end, finding_type, status)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_budget_month_category "
            "ON budgets(budget_month, category)"
        )


def _ensure_true_cost_adjustment_tables(db_path: Path) -> None:
    init_database(db_path)


def _ensure_account_linked_goals(db_path: Path) -> None:
    init_database(db_path)
    columns = _column_names(db_path, "savings_goals")
    with closing(sqlite3.connect(db_path)) as conn, conn:
        if "account_id" not in columns:
            # Legacy rows keep account_id NULL; preserve them rather than wiping
            # user-entered goals (which are not reconstructable from statements).
            conn.execute("ALTER TABLE savings_goals ADD COLUMN account_id VARCHAR(64)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_savings_goal_account_status "
            "ON savings_goals(account_id, status)"
        )


def _ensure_financial_workflow_tables(db_path: Path) -> None:
    """Create durable user-authored preference and schedule records."""
    init_database(db_path)


def _ensure_plan_reconciliation_tables(db_path: Path) -> None:
    """Preserve legacy definitions while disclosing their unknown earlier edits."""
    from ledger.planning.schedules import ScheduleService

    init_database(db_path)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM planned_schedules WHERE schedule_id NOT IN "
            "(SELECT schedule_id FROM planned_schedule_revisions)"
        ).fetchall()
        for row in rows:
            record = ScheduleService._from_sql_row(row)
            conn.execute(
                "INSERT INTO planned_schedule_revisions "
                "(schedule_id,effective_date,recorded_at,definition_json,is_removed,history_complete) "
                "VALUES (?,?,?,?,0,0)",
                (
                    record.schedule_id,
                    record.next_date.isoformat(),
                    naive_utc_now().isoformat(),
                    json.dumps(record.to_dict(), sort_keys=True),
                ),
            )


def _ensure_unique_confirmed_schedule_pattern(db_path: Path) -> None:
    """Enforce one confirmed schedule override for each detected pattern."""
    init_database(db_path)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        duplicate = conn.execute(
            "SELECT 1 FROM planned_schedules "
            "WHERE pattern_id IS NOT NULL AND provenance = 'confirmed' "
            "GROUP BY pattern_id HAVING COUNT(*) > 1 LIMIT 1"
        ).fetchone()
        if duplicate is not None:
            raise ValueError(
                "Confirmed schedule pattern uniqueness cannot be enforced until existing "
                "duplicates are resolved."
            )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_planned_schedule_confirmed_pattern "
            "ON planned_schedules(pattern_id) "
            "WHERE pattern_id IS NOT NULL AND provenance = 'confirmed'"
        )


def _ensure_planning_definition_generations(db_path: Path) -> None:
    """Detect logical-key remove/create/remove changes across every write surface."""
    init_database(db_path)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        for table, kind, first, second in (
            ("budgets", "budget", "budget_month", "category"),
            ("spending_preferences", "preference", "scope", "name"),
        ):
            # All interpolated identifiers and enum values are internal constants.
            conn.execute(
                "INSERT OR IGNORE INTO planning_definition_generations "
                f"(target_kind,key_a,key_b,generation) SELECT '{kind}',{first},{second},1 FROM {table}"
            )

            def bump(alias: str, kind=kind, first=first, second=second) -> str:
                return (
                    "INSERT INTO planning_definition_generations (target_kind,key_a,key_b,generation) "
                    f"VALUES ('{kind}',{alias}.{first},{alias}.{second},1) "
                    "ON CONFLICT(target_kind,key_a,key_b) DO UPDATE SET generation=generation+1;"
                )

            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS planning_generation_{table}_insert "
                f"AFTER INSERT ON {table} BEGIN {bump('NEW')} END"
            )
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS planning_generation_{table}_delete "
                f"AFTER DELETE ON {table} BEGIN {bump('OLD')} END"
            )
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS planning_generation_{table}_update "
                f"AFTER UPDATE ON {table} BEGIN {bump('NEW')} END"
            )
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS planning_generation_{table}_rename "
                f"AFTER UPDATE ON {table} WHEN OLD.{first} != NEW.{first} OR OLD.{second} != NEW.{second} "
                f"BEGIN {bump('OLD')} END"
            )


def _ensure_transfer_source_path(db_path: Path) -> None:
    init_database(db_path)
    columns = _column_names(db_path, "transfers")
    with closing(sqlite3.connect(db_path)) as conn, conn:
        if "source_path" not in columns:
            conn.execute("ALTER TABLE transfers ADD COLUMN source_path VARCHAR(500)")
        conn.execute("CREATE INDEX IF NOT EXISTS ix_transfer_source_path ON transfers(source_path)")


def _ensure_transfer_institution_account_index(db_path: Path) -> None:
    init_database(db_path)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_transfer_institution_account "
            "ON transfers(institution, account)"
        )


def _ensure_transfer_currency(db_path: Path) -> None:
    init_database(db_path)
    columns = _column_names(db_path, "transfers")
    with closing(sqlite3.connect(db_path)) as conn, conn:
        if "currency" not in columns:
            # NOT NULL DEFAULT 'CAD' backfills every existing (pre-currency) leg to
            # CAD, matching is_cad's legacy semantics, so the new balance/credit-limit
            # guards leave historical all-CAD data unchanged.
            conn.execute(
                "ALTER TABLE transfers ADD COLUMN currency VARCHAR(3) NOT NULL DEFAULT 'CAD'"
            )


def _ensure_account_statement_credit_limits(db_path: Path) -> None:
    # New table declared on Base.metadata; create_all is additive + idempotent,
    # so init_database creates it if absent and leaves an existing one untouched.
    init_database(db_path)


def _ensure_investment_tables(db_path: Path) -> None:
    init_database(db_path)


def _ensure_investment_account_lifecycle(db_path: Path) -> None:
    """Add lifecycle fields without inventing facts for existing accounts."""
    init_database(db_path)
    columns = _column_names(db_path, "investment_accounts")
    with closing(sqlite3.connect(db_path)) as conn, conn:
        if "status" not in columns:
            conn.execute(
                "ALTER TABLE investment_accounts "
                "ADD COLUMN status VARCHAR(30) NOT NULL DEFAULT 'open'"
            )
        if "transfer_destination" not in columns:
            conn.execute(
                "ALTER TABLE investment_accounts ADD COLUMN transfer_destination VARCHAR(200)"
            )
        if "status_confirmed_on" not in columns:
            conn.execute("ALTER TABLE investment_accounts ADD COLUMN status_confirmed_on DATE")
        if "closed_on" not in columns:
            conn.execute("ALTER TABLE investment_accounts ADD COLUMN closed_on DATE")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_investment_accounts_status "
            "ON investment_accounts(status)"
        )


def _enforce_investment_account_lifecycle_values(db_path: Path) -> None:
    """Normalize legacy spellings and install the canonical lifecycle constraint."""
    init_database(db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN IMMEDIATE")
            invalid = conn.execute(
                "SELECT DISTINCT status FROM investment_accounts "
                "WHERE status NOT IN ('active', 'closed', 'unknown', 'open', 'transferred')"
            ).fetchall()
            if invalid:
                raise ValueError("Investment account lifecycle contains an unsupported status.")
            conn.execute("UPDATE investment_accounts SET status = 'active' WHERE status = 'open'")
            conn.execute(
                "UPDATE investment_accounts SET status = 'closed' WHERE status = 'transferred'"
            )

            table_name = "investment_accounts"
            temporary_name = f"{table_name}__lifecycle_new"
            table = Base.metadata.tables[table_name]
            columns = tuple(column.name for column in table.columns)
            conn.execute(_canonical_table_sql(table_name, temporary_name))
            placeholders = ", ".join("?" for _ in columns)
            column_list = ", ".join(columns)
            rows = conn.execute(f"SELECT {column_list} FROM {table_name}").fetchall()
            if rows:
                conn.executemany(
                    f"INSERT INTO {temporary_name} ({column_list}) VALUES ({placeholders})",
                    rows,
                )
            conn.execute(f"DROP TABLE {table_name}")
            conn.execute(f"ALTER TABLE {temporary_name} RENAME TO {table_name}")
            for schema_index in sorted(table.indexes, key=lambda value: str(value.name)):
                conn.execute(str(CreateIndex(schema_index).compile(dialect=sqlite_dialect())))
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError(
                    "investment lifecycle migration cannot preserve investment relationships"
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")


def _ensure_investment_statement_period_uniqueness(db_path: Path) -> None:
    """Enforce one statement per account-period without exposing duplicates."""
    with closing(sqlite3.connect(db_path)) as conn, conn:
        duplicate = conn.execute(
            "SELECT 1 FROM investment_statements "
            "GROUP BY account_id, period_start, period_end HAVING COUNT(*) > 1 LIMIT 1"
        ).fetchone()
        if duplicate is not None:
            raise ValueError(
                "Investment statement period uniqueness cannot be enforced until existing "
                "duplicates are resolved."
            )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_investment_statement_account_period "
            "ON investment_statements(account_id, period_start, period_end)"
        )


def _ensure_investment_pdf_statement_fields(db_path: Path) -> None:
    """Widen statement fields and lifecycle values without manufacturing facts.

    SQLite needs rebuilt tables to relax NOT NULL and CHECK constraints. Keep
    their exact stored values, identifiers and foreign keys in one transaction;
    a failed copy or relationship check leaves the old schema and rows intact.
    """
    tables = (
        "investment_accounts",
        "investment_statements",
        "investment_asset_balances",
    )
    with closing(sqlite3.connect(db_path)) as conn:
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN IMMEDIATE")
            for table_name in tables:
                table = Base.metadata.tables[table_name]
                columns = tuple(column.name for column in table.columns)
                actual_columns = {
                    str(row[1]) for row in conn.execute(f"PRAGMA table_info({table_name})")
                }
                if actual_columns != set(columns):
                    raise ValueError(
                        "Investment PDF field migration cannot preserve an unexpected schema."
                    )
                temporary_name = f"{table_name}__pdf_fields_new"
                column_list = ", ".join(columns)
                conn.execute(_canonical_table_sql(table_name, temporary_name))
                conn.execute(
                    f"INSERT INTO {temporary_name} ({column_list}) "
                    f"SELECT {column_list} FROM {table_name}"
                )
                conn.execute(f"DROP TABLE {table_name}")
                conn.execute(f"ALTER TABLE {temporary_name} RENAME TO {table_name}")
                for schema_index in sorted(table.indexes, key=lambda value: str(value.name)):
                    conn.execute(str(CreateIndex(schema_index).compile(dialect=sqlite_dialect())))
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError(
                    "Investment PDF field migration cannot preserve investment relationships."
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")


def _investment_source_file_foreign_keys(db_path: Path) -> set[tuple[str, str, str]]:
    """Return (column, parent table, parent column) source-file relationships."""
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute("PRAGMA foreign_key_list(investment_source_files)").fetchall()
    return {(str(row[3]), str(row[2]), str(row[4])) for row in rows}


def validate_investment_cash_balance_schema(conn: sqlite3.Connection) -> None:
    """Require the additive cash-evidence table's exact shape without changing it."""
    table_name = "investment_cash_balances"
    table = Base.metadata.tables[table_name]
    primary_key = tuple(column.name for column in table.primary_key.columns)
    expected = [
        (
            column.name,
            str(column.type.compile(dialect=sqlite_dialect())).upper(),
            int(not column.nullable),
            None,
            primary_key.index(column.name) + 1 if column.name in primary_key else 0,
        )
        for column in table.columns
    ]
    actual = [tuple(row[1:]) for row in conn.execute(f"PRAGMA table_info({table_name})")]
    foreign_keys = {
        tuple(row[2:]) for row in conn.execute(f"PRAGMA foreign_key_list({table_name})")
    }
    expected_foreign_keys = {
        ("investment_statements", "statement_id", "statement_id", "NO ACTION", "NO ACTION", "NONE")
    }
    if actual != expected or foreign_keys != expected_foreign_keys:
        raise ValueError("Investment cash evidence has an unexpected schema.")


def is_legacy_investment_cash_schema(conn: sqlite3.Connection) -> bool:
    """Recognize the complete 0029 layout without migrating reviewed evidence."""
    optional_table = "investment_cash_balances"
    existing = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    required = set(Base.metadata.tables) - {optional_table}
    if (
        required - existing
        or conn.execute("SELECT 1 FROM sqlite_master WHERE name = ?", (optional_table,)).fetchone()
        is not None
    ):
        return False
    expected_versions = {
        migration.version
        for migration in MIGRATIONS
        if migration.version <= "20260912_0029_investment_pdf_statement_fields"
    }
    versions = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
    if versions != expected_versions:
        return False
    return all(
        {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        == set(Base.metadata.tables[table].columns.keys())
        for table in required
    )


def _ensure_investment_cash_balances(db_path: Path) -> None:
    """Add optional source evidence without deriving or backfilling historical facts."""
    table_name = "investment_cash_balances"
    with closing(sqlite3.connect(db_path)) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = ?", (table_name,)
            ).fetchone()
            if exists is None:
                conn.execute(
                    str(
                        CreateTable(Base.metadata.tables[table_name]).compile(
                            dialect=sqlite_dialect()
                        )
                    )
                )
            validate_investment_cash_balance_schema(conn)
            columns = ", ".join(INVESTMENT_EXACT_DECIMAL_COLUMNS[table_name])
            for row in conn.execute(f"SELECT {columns} FROM {table_name}"):
                for value in row:
                    if value is not None:
                        decimal_from_canonical_text(value)
            if conn.execute(f"PRAGMA foreign_key_check({table_name})").fetchone() is not None:
                raise ValueError("Investment cash evidence has invalid statement relationships.")
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _ensure_investment_source_file_foreign_keys(db_path: Path) -> None:
    """Rebuild legacy provenance storage when either canonical FK is absent."""
    canonical_foreign_keys = {
        ("import_run_id", "investment_import_runs", "run_id"),
        ("statement_id", "investment_statements", "statement_id"),
    }
    if canonical_foreign_keys <= _investment_source_file_foreign_keys(db_path):
        return

    columns = (
        "file_hash",
        "source_path",
        "source_role",
        "original_path",
        "archived_path",
        "structured_import",
        "byte_size",
        "archive_status",
        "import_run_id",
        "statement_id",
        "discovered_at",
        "staged_at",
        "archived_at",
        "finalized_at",
        "updated_at",
        "status",
    )
    column_list = ", ".join(columns)
    with closing(sqlite3.connect(db_path)) as conn:
        try:
            # SQLite only honors this pragma outside a transaction.
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                CREATE TABLE investment_source_files__new (
                    file_hash VARCHAR(64) NOT NULL PRIMARY KEY,
                    source_path VARCHAR(500) NOT NULL UNIQUE,
                    source_role VARCHAR(30),
                    original_path VARCHAR(500),
                    archived_path VARCHAR(500),
                    structured_import BOOLEAN,
                    byte_size INTEGER,
                    archive_status VARCHAR(30),
                    import_run_id VARCHAR(128),
                    statement_id VARCHAR(128),
                    discovered_at DATETIME NOT NULL,
                    staged_at DATETIME,
                    archived_at DATETIME,
                    finalized_at DATETIME,
                    updated_at DATETIME,
                    status VARCHAR(30) NOT NULL,
                    FOREIGN KEY(import_run_id) REFERENCES investment_import_runs(run_id),
                    FOREIGN KEY(statement_id) REFERENCES investment_statements(statement_id)
                )
                """
            )
            conn.execute(
                f"INSERT INTO investment_source_files__new ({column_list}) "
                f"SELECT {column_list} FROM investment_source_files"
            )
            conn.execute("DROP TABLE investment_source_files")
            conn.execute(
                "ALTER TABLE investment_source_files__new RENAME TO investment_source_files"
            )
            # Four provenance lookup indexes plus the canonical lifecycle-status index.
            for name, column in (
                ("ix_investment_source_files_source_role", "source_role"),
                ("ix_investment_source_files_archive_status", "archive_status"),
                ("ix_investment_source_files_import_run_id", "import_run_id"),
                ("ix_investment_source_files_statement_id", "statement_id"),
                ("ix_investment_source_files_status", "status"),
            ):
                conn.execute(f"CREATE INDEX {name} ON investment_source_files({column})")
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError(
                    "Investment source-file foreign keys cannot be restored while invalid "
                    "references exist."
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")


def _ensure_investment_provenance_and_performance(db_path: Path) -> None:
    """Add source/run provenance and source-backed performance with row-preserving repair."""
    source_columns = _column_names(db_path, "investment_source_files")
    run_columns = _column_names(db_path, "investment_import_runs")
    source_additions = {
        "source_role": "VARCHAR(30)",
        "original_path": "VARCHAR(500)",
        "archived_path": "VARCHAR(500)",
        "structured_import": "BOOLEAN",
        "byte_size": "INTEGER",
        "archive_status": "VARCHAR(30)",
        "import_run_id": "VARCHAR(128)",
        "archived_at": "DATETIME",
        "updated_at": "DATETIME",
    }
    run_additions = {
        "updated_at": "DATETIME",
        "staged_at": "DATETIME",
        "finalizing_at": "DATETIME",
        "archive_pending_at": "DATETIME",
        "cancelled_at": "DATETIME",
        "failed_at": "DATETIME",
        "private_manifest_path": "VARCHAR(500)",
        "baseline_fingerprint": "JSON",
        "staging_fingerprint": "JSON",
        "target_fingerprint": "JSON",
        "source_count": "INTEGER",
        "structured_source_count": "INTEGER",
        "evidence_source_count": "INTEGER",
        "inserted_count": "INTEGER",
        "replaced_count": "INTEGER",
        "unchanged_count": "INTEGER",
        "lifecycle_inputs": "JSON",
        "conflict_review": "JSON",
        "conflict_authorization_fingerprint": "VARCHAR(64)",
        "redacted_failure": "VARCHAR(120)",
    }
    with closing(sqlite3.connect(db_path)) as conn, conn:
        for name, sql_type in source_additions.items():
            if name not in source_columns:
                conn.execute(f"ALTER TABLE investment_source_files ADD COLUMN {name} {sql_type}")
        for name, sql_type in run_additions.items():
            if name not in run_columns:
                conn.execute(f"ALTER TABLE investment_import_runs ADD COLUMN {name} {sql_type}")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS investment_performance (
                statement_id VARCHAR(128) NOT NULL,
                source_row INTEGER NOT NULL,
                asset_category VARCHAR(100) NOT NULL,
                symbol VARCHAR(100) NOT NULL DEFAULT '',
                cost_adjustment NUMERIC(28, 12),
                realized_short_profit NUMERIC(28, 12),
                realized_short_loss NUMERIC(28, 12),
                realized_long_profit NUMERIC(28, 12),
                realized_long_loss NUMERIC(28, 12),
                realized_total NUMERIC(28, 12),
                unrealized_short_profit NUMERIC(28, 12),
                unrealized_short_loss NUMERIC(28, 12),
                unrealized_long_profit NUMERIC(28, 12),
                unrealized_long_loss NUMERIC(28, 12),
                unrealized_total NUMERIC(28, 12),
                total NUMERIC(28, 12),
                code VARCHAR(80) NOT NULL DEFAULT '',
                PRIMARY KEY (statement_id, source_row),
                FOREIGN KEY(statement_id) REFERENCES investment_statements (statement_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_investment_performance_statement_asset "
            "ON investment_performance(statement_id, asset_category)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_investment_source_files_source_role "
            "ON investment_source_files(source_role)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_investment_source_files_archive_status "
            "ON investment_source_files(archive_status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_investment_source_files_import_run_id "
            "ON investment_source_files(import_run_id)"
        )
    _ensure_investment_source_file_foreign_keys(db_path)


def _legacy_fixed_point_text(value: object) -> str:
    """Validate one legacy SQLite scalar and return canonical exact text."""
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("exact fixed-point migration found invalid legacy content") from exc
    try:
        return canonical_fixed_point_text(decimal_value)
    except ValueError as exc:
        raise ValueError("exact fixed-point migration found invalid legacy content") from exc


def _canonical_table_sql(table_name: str, temporary_name: str) -> str:
    metadata = MetaData()
    for table in Base.metadata.sorted_tables:
        table.to_metadata(metadata)
    temporary = Base.metadata.tables[table_name].to_metadata(metadata, name=temporary_name)
    return str(CreateTable(temporary).compile(dialect=sqlite_dialect()))


def _ensure_investment_exact_decimals(db_path: Path) -> None:
    """Transactionally rebuild legacy investment NUMERIC columns as exact TEXT."""
    # Migration 0016 owns only the original NUMERIC tables. Later additive
    # evidence tables start exact and have their own guarded migrations.
    legacy_decimal_columns = {
        table: columns
        for table, columns in INVESTMENT_EXACT_DECIMAL_COLUMNS.items()
        if table != "investment_cash_balances"
    }
    with closing(sqlite3.connect(db_path)) as conn:
        try:
            # SQLite permits toggling this only outside a transaction. The full
            # rebuild remains protected by one IMMEDIATE transaction and ends with
            # an explicit relationship check before commit.
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN IMMEDIATE")

            converted_rows: dict[str, tuple[tuple[object, ...], ...]] = {}
            needs_rebuild = False
            for table_name, decimal_columns in legacy_decimal_columns.items():
                table_info = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
                if not table_info:
                    raise ValueError("exact fixed-point migration is missing an investment table")
                columns = tuple(str(row[1]) for row in table_info)
                declared_types = {str(row[1]): str(row[2]).upper() for row in table_info}
                if any(declared_types.get(column) != "TEXT" for column in decimal_columns):
                    needs_rebuild = True
                decimal_indexes = {columns.index(column) for column in decimal_columns}
                converted_table_rows: list[tuple[object, ...]] = []
                for raw_row in conn.execute(f"SELECT * FROM {table_name}").fetchall():
                    converted: list[object] = []
                    for column_index, value in enumerate(raw_row):
                        if column_index in decimal_indexes and value is not None:
                            converted.append(_legacy_fixed_point_text(value))
                        else:
                            converted.append(value)
                    converted_table_rows.append(tuple(converted))
                converted_rows[table_name] = tuple(converted_table_rows)

            if not needs_rebuild:
                conn.commit()
                return

            for table_name in legacy_decimal_columns:
                table = Base.metadata.tables[table_name]
                columns = tuple(column.name for column in table.columns)
                temporary_name = f"{table_name}__exact_new"
                conn.execute(_canonical_table_sql(table_name, temporary_name))
                stored_rows = converted_rows[table_name]
                if stored_rows:
                    placeholders = ", ".join("?" for _ in columns)
                    column_list = ", ".join(columns)
                    conn.executemany(
                        f"INSERT INTO {temporary_name} ({column_list}) VALUES ({placeholders})",
                        stored_rows,
                    )
                conn.execute(f"DROP TABLE {table_name}")
                conn.execute(f"ALTER TABLE {temporary_name} RENAME TO {table_name}")

            for table_name in legacy_decimal_columns:
                for schema_index in sorted(
                    Base.metadata.tables[table_name].indexes,
                    key=lambda value: str(value.name),
                ):
                    conn.execute(str(CreateIndex(schema_index).compile(dialect=sqlite_dialect())))

            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError(
                    "exact fixed-point migration cannot preserve investment relationships"
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")


def _ensure_investment_source_associations(db_path: Path) -> None:
    """Normalize reusable source paths and backfill durable occurrence associations."""
    init_database(db_path)
    source_columns = tuple(
        column.name for column in Base.metadata.tables["investment_source_files"].columns
    )
    with closing(sqlite3.connect(db_path)) as conn:
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN IMMEDIATE")
            source_path_is_unique = False
            for index in conn.execute("PRAGMA index_list(investment_source_files)"):
                if not int(index[2]):
                    continue
                index_columns = tuple(
                    str(row[2]) for row in conn.execute(f"PRAGMA index_info({index[1]!s})")
                )
                if index_columns == ("source_path",):
                    source_path_is_unique = True
                    break
            if source_path_is_unique:
                temporary_name = "investment_source_files__association_new"
                conn.execute(_canonical_table_sql("investment_source_files", temporary_name))
                column_list = ", ".join(source_columns)
                conn.execute(
                    f"INSERT INTO {temporary_name} ({column_list}) "
                    f"SELECT {column_list} FROM investment_source_files"
                )
                conn.execute("DROP TABLE investment_source_files")
                conn.execute(f"ALTER TABLE {temporary_name} RENAME TO investment_source_files")
                for schema_index in sorted(
                    Base.metadata.tables["investment_source_files"].indexes,
                    key=lambda value: str(value.name),
                ):
                    conn.execute(str(CreateIndex(schema_index).compile(dialect=sqlite_dialect())))

            rows = conn.execute(
                "SELECT source.file_hash, source.source_path, source.source_role, "
                "source.original_path, source.archived_path, source.structured_import, "
                "source.byte_size, source.archive_status, source.import_run_id, "
                "source.statement_id, source.discovered_at, source.staged_at, "
                "source.archived_at, source.finalized_at, source.updated_at, source.status, "
                "statement.parsed_fingerprint "
                "FROM investment_source_files AS source "
                "LEFT JOIN investment_statements AS statement "
                "ON statement.statement_id = source.statement_id"
            ).fetchall()
            for row in rows:
                (
                    file_hash,
                    source_path,
                    source_role,
                    original_path,
                    archived_path,
                    structured_import,
                    byte_size,
                    archive_status,
                    import_run_id,
                    statement_id,
                    discovered_at,
                    staged_at,
                    archived_at,
                    finalized_at,
                    updated_at,
                    status,
                    statement_fingerprint,
                ) = row
                normalized_role = str(source_role or "unknown")
                association_payload = [
                    str(import_run_id or "direct"),
                    str(file_hash),
                    str(statement_id or "evidence"),
                    str(source_path),
                    normalized_role,
                ]
                association_id = hashlib.sha256(
                    json.dumps(
                        association_payload,
                        separators=(",", ":"),
                        ensure_ascii=True,
                    ).encode()
                ).hexdigest()
                conn.execute(
                    "INSERT OR IGNORE INTO investment_source_associations ("
                    "association_id, file_hash, import_run_id, statement_id, "
                    "statement_fingerprint, source_path, source_role, original_path, "
                    "archived_path, structured_import, byte_size, archive_status, "
                    "discovered_at, staged_at, archived_at, finalized_at, updated_at, status, "
                    "superseded_by_statement_id, superseded_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
                    (
                        association_id,
                        file_hash,
                        import_run_id,
                        statement_id,
                        statement_fingerprint,
                        source_path,
                        normalized_role,
                        original_path or source_path,
                        archived_path,
                        bool(structured_import)
                        if structured_import is not None
                        else normalized_role != "evidence_pdf",
                        byte_size,
                        archive_status or "pending",
                        discovered_at,
                        staged_at or discovered_at,
                        archived_at,
                        finalized_at,
                        updated_at,
                        status,
                    ),
                )
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError(
                    "Investment source associations cannot preserve foreign-key integrity."
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")


def _notnull_columns(db_path: Path, table: str) -> set[str]:
    """Column names declared NOT NULL in the live SQLite table (PRAGMA notnull flag)."""
    with closing(sqlite3.connect(db_path)) as conn:
        # table is a trusted internal constant; PRAGMA can't be parameter-bound.
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    # row layout: (cid, name, type, notnull, dflt_value, pk)
    return {str(row[1]) for row in rows if int(row[3]) == 1}


def _relax_health_score_nullability(db_path: Path) -> None:
    """Make financial_health_scores.emergency_fund_months / debt_ratio nullable.

    These two metrics are now reported as NULL when "not assessable" (no absolute
    balance anchor exists — see FinancialHealthScorer). A legacy table created them
    NOT NULL, which would reject the NULL write. SQLite cannot drop a NOT NULL
    constraint in place, so rebuild the table via the canonical
    create-copy-drop-rename dance. The table is a purely derived daily cache
    (re-computed by compute_recent_scores), so copying existing rows across is
    safe and loses nothing; NULL is a widening, so existing non-null values remain.
    """
    init_database(db_path)
    notnull = _notnull_columns(db_path, "financial_health_scores")
    if not ({"emergency_fund_months", "debt_ratio"} & notnull):
        # Already nullable (fresh DB created by current create_all) — nothing to do.
        return
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.executescript(
            """
            CREATE TABLE financial_health_scores__new (
                score_date DATE NOT NULL PRIMARY KEY,
                overall_score INTEGER NOT NULL,
                cashflow_health INTEGER NOT NULL,
                spending_consistency INTEGER NOT NULL,
                savings_rate NUMERIC(5, 2) NOT NULL,
                emergency_fund_months NUMERIC(5, 2),
                debt_ratio NUMERIC(5, 2),
                score_trend_7d VARCHAR(20) NOT NULL,
                score_change_7d INTEGER NOT NULL,
                risk_factors TEXT NOT NULL,
                computed_at DATETIME NOT NULL
            );
            INSERT INTO financial_health_scores__new
                SELECT score_date, overall_score, cashflow_health, spending_consistency,
                       savings_rate, emergency_fund_months, debt_ratio, score_trend_7d,
                       score_change_7d, risk_factors, computed_at
                FROM financial_health_scores;
            DROP TABLE financial_health_scores;
            ALTER TABLE financial_health_scores__new RENAME TO financial_health_scores;
            CREATE INDEX IF NOT EXISTS ix_financial_health_scores_overall_score
                ON financial_health_scores(overall_score);
            """
        )


def _unmapped_columns(db_path: Path, table: str) -> set[str]:
    """Live columns the ORM no longer declares for ``table``."""
    mapped = set(Base.metadata.tables[table].columns.keys())
    return _column_names(db_path, table) - mapped


def _drop_unmapped_columns(db_path: Path, table: str) -> None:
    """Rebuild ``table`` so it matches the ORM exactly, dropping stale columns.

    A column the ORM has stopped declaring is harmless while it is nullable, but a
    legacy NOT NULL column with no SQLite DEFAULT makes every ORM INSERT fail --
    the ORM omits the column, SQLite rejects the row. That is invisible to
    validate_database, which only ever checked for MISSING columns.

    Rebuilt via the canonical create-copy-drop-rename dance against the ORM's own
    DDL (_canonical_table_sql), so it stays correct as the model evolves and does
    not depend on SQLite's version-gated ALTER TABLE DROP COLUMN. Only the mapped
    columns are copied; data in the dropped columns is intentionally discarded.
    """
    init_database(db_path)
    if not _unmapped_columns(db_path, table):
        return

    columns = tuple(column.name for column in Base.metadata.tables[table].columns)
    column_list = ", ".join(columns)
    temporary_name = f"{table}__rebuild_new"
    with closing(sqlite3.connect(db_path)) as conn:
        try:
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(f"DROP TABLE IF EXISTS {temporary_name}")
            conn.execute(_canonical_table_sql(table, temporary_name))
            conn.execute(
                f"INSERT INTO {temporary_name} ({column_list}) SELECT {column_list} FROM {table}"
            )
            conn.execute(f"DROP TABLE {table}")
            conn.execute(f"ALTER TABLE {temporary_name} RENAME TO {table}")
            for schema_index in sorted(
                Base.metadata.tables[table].indexes, key=lambda value: str(value.name)
            ):
                conn.execute(str(CreateIndex(schema_index).compile(dialect=sqlite_dialect())))
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError(f"rebuilding {table} cannot preserve relationships")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")


def _rebuild_for_account_foreign_keys(db_path: Path) -> None:
    """Rebuild the derived account tables so they carry the ORM's foreign keys.

    SQLite cannot ALTER a table to add a constraint, so this uses the same
    create-copy-drop-rename dance as _drop_unmapped_columns, against the ORM's own
    DDL -- which now declares account_balances.account_id and
    recurring_patterns.account_id as references to accounts.account_id.

    A pre-existing orphan makes the post-rebuild foreign_key_check fail and rolls
    the whole migration back rather than publishing a database the MCP cannot
    read: its preflight maps any foreign_key_check hit to `corrupt_data`, which is
    content-free and would identify nothing.

    The unique index on accounts.account_number is created here too. accounts is
    not itself rebuilt, so create_all would not add an index to the already-existing
    table.
    """
    init_database(db_path)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_accounts_account_number "
            "ON accounts(account_number)"
        )

    for table in ("account_balances", "recurring_patterns"):
        columns = tuple(column.name for column in Base.metadata.tables[table].columns)
        column_list = ", ".join(columns)
        temporary_name = f"{table}__fk_new"
        with closing(sqlite3.connect(db_path)) as conn:
            try:
                conn.execute("PRAGMA foreign_keys = OFF")
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(f"DROP TABLE IF EXISTS {temporary_name}")
                conn.execute(_canonical_table_sql(table, temporary_name))
                conn.execute(
                    f"INSERT INTO {temporary_name} ({column_list}) "
                    f"SELECT {column_list} FROM {table}"
                )
                conn.execute(f"DROP TABLE {table}")
                conn.execute(f"ALTER TABLE {temporary_name} RENAME TO {table}")
                for schema_index in sorted(
                    Base.metadata.tables[table].indexes, key=lambda value: str(value.name)
                ):
                    conn.execute(str(CreateIndex(schema_index).compile(dialect=sqlite_dialect())))
                if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                    raise ValueError(f"{table} has rows referencing an account that does not exist")
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.execute("PRAGMA foreign_keys = ON")


def _add_statement_coverage_and_drop_dead_columns(db_path: Path) -> None:
    """Create statement_coverage and retire the never-populated embedding columns.

    ``init_database`` creates the new table (create_all is additive and
    idempotent). The transactions rebuild then drops ``embedding`` and
    ``embedding_version``, which no producer ever set to anything but None -- both
    assignment sites hardcoded it -- so no data is lost.
    """
    init_database(db_path)
    _drop_unmapped_columns(db_path, "transactions")


def _drop_dead_transaction_columns(db_path: Path) -> None:
    """Retire the five never-populated ``transactions`` columns.

    ``paired_transfer_id``, ``transfer_confidence``, ``counterparty_account`` and
    ``counterparty_institution`` were declared for a transfer-pairing pass that only
    ever ran against ``transfers``; ``days_since_payday`` deferred to an analysis
    pipeline nobody wrote. Every write site assigned None to these columns;
    there was no producer of transaction-level values to preserve.

    The SAME FOUR pairing columns on ``transfers`` are populated by the matcher
    and are untouched: this rebuilds ``transactions`` only.
    """
    init_database(db_path)
    _drop_unmapped_columns(db_path, "transactions")


def _ensure_account_lifecycle(db_path: Path) -> None:
    """Create the operator-authored account_lifecycle table.

    ``create_all`` is additive and idempotent, and the table has no foreign key to
    rebuild against, so creating it is the whole migration. There is nothing to
    backfill: an absent row means "the operator has said nothing", which is exactly
    the pre-migration state and is a meaningful answer, not a gap.
    """
    init_database(db_path)


def _backfill_statement_coverage(db_path: Path) -> None:
    """Seed statement_coverage from transactions already imported.

    The table is written at finalize and at repair, so on a database whose
    statements were all imported before it existed it stays empty -- and an empty
    coverage table cannot be told apart from a ledger nobody ever imported into,
    which is the very distinction the table exists to make.

    Backfill recovers the "imported, with activity" rows exactly: transactions
    carry both the account slug and the YYYY-MM source_period, and the distinct
    source_file count per account-month gives statement_count.

    It CANNOT recover a statement that imported with zero rows -- nothing was
    written to recover it from. Those become visible only for statements imported
    from here on, and the tool reports absence honestly rather than claiming
    coverage it does not have. Existing rows are left alone, so a re-run and a
    subsequent real import never fight.
    """
    init_database(db_path)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO statement_coverage (
                account_slug, period, institution, statement_type,
                row_count, statement_count, skipped_reason, imported_at
            )
            SELECT
                account,
                source_period,
                MIN(institution),
                '',
                COUNT(DISTINCT txn_id),
                COUNT(DISTINCT source_file),
                '',
                MAX(imported_at)
            FROM transactions
            WHERE source_period != '' AND account != ''
            GROUP BY account, source_period
            """
        )


def _drop_legacy_savings_goal_amount(db_path: Path) -> None:
    """Drop the legacy savings_goals.current_amount column.

    The ORM stopped declaring current_amount when goals became account-linked, but
    the pre-existing DDL had emitted it as ``NUMERIC(12, 2) NOT NULL`` with no
    server-side default (the old default was Python-side). On any database created
    before that change, every savings-goal insert therefore fails with
    ``IntegrityError: NOT NULL constraint failed: savings_goals.current_amount``
    while validate_database reports SUCCESS. Migration 0004 is already recorded as
    applied so it cannot be edited in place; this is its follow-up.
    """
    _drop_unmapped_columns(db_path, "savings_goals")


def _ensure_account_balance_basis(db_path: Path) -> None:
    """Persist balance basis and make it part of the snapshot identity.

    The basis belongs in the primary key so statement-backed absolute and calculator
    relative rows can coexist for the same account/date. Existing rows predate
    statement anchoring and are therefore backfilled as ``relative``.
    """
    init_database(db_path)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        columns = conn.execute("PRAGMA table_info(account_balances)").fetchall()
        by_name = {str(row[1]): row for row in columns}
        basis_column = by_name.get("balance_basis")
        if basis_column is not None and int(basis_column[5]) > 0:
            return

        basis_expression = (
            "COALESCE(balance_basis, 'relative')" if basis_column is not None else "'relative'"
        )
        conn.executescript(
            f"""
            BEGIN IMMEDIATE;
            DROP TABLE IF EXISTS account_balances__new;
            CREATE TABLE account_balances__new (
                account_id VARCHAR(64) NOT NULL,
                balance_date DATE NOT NULL,
                balance NUMERIC(12, 2) NOT NULL,
                balance_basis VARCHAR(16) NOT NULL DEFAULT 'relative',
                transaction_count INTEGER NOT NULL,
                balance_7d_avg NUMERIC(12, 2) NOT NULL,
                balance_30d_avg NUMERIC(12, 2) NOT NULL,
                balance_7d_min NUMERIC(12, 2) NOT NULL,
                balance_7d_max NUMERIC(12, 2) NOT NULL,
                credit_utilization NUMERIC(5, 2),
                computed_at DATETIME NOT NULL,
                PRIMARY KEY (account_id, balance_date, balance_basis)
            );
            INSERT INTO account_balances__new (
                account_id, balance_date, balance, balance_basis, transaction_count,
                balance_7d_avg, balance_30d_avg, balance_7d_min, balance_7d_max,
                credit_utilization, computed_at
            )
            SELECT account_id, balance_date, balance, {basis_expression}, transaction_count,
                   balance_7d_avg, balance_30d_avg, balance_7d_min, balance_7d_max,
                   credit_utilization, computed_at
            FROM account_balances;
            DROP TABLE account_balances;
            ALTER TABLE account_balances__new RENAME TO account_balances;
            CREATE INDEX IF NOT EXISTS ix_balance_date ON account_balances(balance_date);
            CREATE INDEX IF NOT EXISTS ix_balance_account_date
                ON account_balances(account_id, balance_date);
            COMMIT;
            """
        )


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version="20260511_0001_phase_three_local_ops",
        description="Create phase three migration, audit, quality, export, and report tables",
        apply=_create_phase_three_tables,
    ),
    Migration(
        version="20260513_0002_finance_cockpit",
        description="Create insight findings, budgets, goals, and period snapshot indexes",
        apply=_ensure_phase_four_tables,
    ),
    Migration(
        version="20260513_0003_true_cost_adjustments",
        description="Create transaction treatment and offset tables for true-cost reporting",
        apply=_ensure_true_cost_adjustment_tables,
    ),
    Migration(
        version="20260514_0004_account_linked_goals",
        description="Link savings goals to accounts (preserve existing goals)",
        apply=_ensure_account_linked_goals,
    ),
    Migration(
        version="20260603_0005_transfer_source_path",
        description="Add source_path to transfers for per-statement re-import scoping",
        apply=_ensure_transfer_source_path,
    ),
    Migration(
        version="20260621_0006_transfer_institution_account_index",
        description="Add (institution, account) composite index to transfers for O(1) account lookups",
        apply=_ensure_transfer_institution_account_index,
    ),
    Migration(
        version="20260703_0007_account_statement_credit_limits",
        description="Add account_statement_credit_limits table for persisted printed credit limits",
        apply=_ensure_account_statement_credit_limits,
    ),
    Migration(
        version="20260703_0008_transfer_currency",
        description="Add currency to transfers so non-CAD legs are excluded from CAD balance math",
        apply=_ensure_transfer_currency,
    ),
    Migration(
        version="20260704_0009_health_score_nullable_metrics",
        description=(
            "Make financial_health_scores.emergency_fund_months / debt_ratio nullable "
            "(NULL = not assessable without an absolute balance anchor)"
        ),
        apply=_relax_health_score_nullability,
    ),
    Migration(
        version="20260709_0010_account_balance_basis",
        description="Mark account balance snapshots as relative or absolute",
        apply=_ensure_account_balance_basis,
    ),
    Migration(
        version="20260801_0011_investment_tables",
        description="Create historical investment accounts, statements, positions, and activities",
        apply=_ensure_investment_tables,
    ),
    Migration(
        version="20260801_0012_investment_account_lifecycle",
        description="Add status and closure lifecycle fields to investment accounts",
        apply=_ensure_investment_account_lifecycle,
    ),
    Migration(
        version="20260801_0013_investment_statement_period_uniqueness",
        description="Enforce one historical investment statement per account-period",
        apply=_ensure_investment_statement_period_uniqueness,
    ),
    Migration(
        version="20260801_0014_investment_provenance_and_performance",
        description="Add investment source/run provenance and statement performance rows",
        apply=_ensure_investment_provenance_and_performance,
    ),
    Migration(
        version="20260801_0015_investment_provenance_foreign_keys",
        description="Restore canonical investment source-file provenance foreign keys",
        apply=_ensure_investment_source_file_foreign_keys,
    ),
    Migration(
        version="20260801_0016_investment_exact_decimals",
        description="Persist every investment Decimal as validated canonical text",
        apply=_ensure_investment_exact_decimals,
    ),
    Migration(
        version="20260801_0017_investment_lifecycle_values",
        description="Enforce active or closed investment account lifecycle values",
        apply=_enforce_investment_account_lifecycle_values,
    ),
    Migration(
        version="20260801_0018_investment_source_associations",
        description="Preserve append-only investment source occurrence provenance",
        apply=_ensure_investment_source_associations,
    ),
    Migration(
        version="20260802_0019_drop_legacy_savings_goal_amount",
        description="Drop the legacy NOT NULL savings_goals.current_amount column",
        apply=_drop_legacy_savings_goal_amount,
    ),
    Migration(
        version="20260809_0020_account_referential_integrity",
        description=(
            "Make accounts authoritative: unique slug index and foreign keys "
            "from the derived account tables"
        ),
        apply=_rebuild_for_account_foreign_keys,
    ),
    Migration(
        version="20260809_0021_statement_coverage_and_dead_columns",
        description=(
            "Add statement_coverage; drop the never-populated transaction embedding columns"
        ),
        apply=_add_statement_coverage_and_drop_dead_columns,
    ),
    Migration(
        version="20260809_0022_backfill_statement_coverage",
        description="Seed statement_coverage from already-imported transactions",
        apply=_backfill_statement_coverage,
    ),
    Migration(
        version="20260810_0023_drop_dead_transaction_columns",
        description=(
            "Drop five never-populated transactions columns: the four transfer-pairing "
            "columns, whose live twins on transfers are untouched, and days_since_payday, "
            "which exists on no other table"
        ),
        apply=_drop_dead_transaction_columns,
    ),
    Migration(
        version="20260810_0024_account_lifecycle",
        description="Add the operator-authored account_lifecycle closure table",
        apply=_ensure_account_lifecycle,
    ),
    Migration(
        version="20260903_0025_financial_workflow_records",
        description="Add durable spending preferences and planned cash schedules",
        apply=_ensure_financial_workflow_tables,
    ),
    Migration(
        version="20260903_0026_unique_confirmed_schedule_pattern",
        description="Enforce one confirmed cash schedule per detected pattern",
        apply=_ensure_unique_confirmed_schedule_pattern,
    ),
    Migration(
        version="20260904_0027_plan_reconciliation",
        description="Preserve schedule history, precise statement coverage, reviews and proposal receipts",
        apply=_ensure_plan_reconciliation_tables,
    ),
    Migration(
        version="20260904_0028_planning_definition_generations",
        description="Track removed and recreated budget/preference logical keys for conditional undo",
        apply=_ensure_planning_definition_generations,
    ),
    Migration(
        version="20260912_0029_investment_pdf_statement_fields",
        description="Allow unknown investment lifecycle and unavailable PDF statement fields",
        apply=_ensure_investment_pdf_statement_fields,
    ),
    Migration(
        version="20260913_0030_investment_cash_balances",
        description="Retain optional source-printed native investment cash balances and FX rates",
        apply=_ensure_investment_cash_balances,
    ),
)


# Single source of truth: every table the ORM declares is required. Deriving this
# from Base.metadata (rather than a hand-maintained literal) means a newly-added
# table is validated automatically — a previous hardcoded set silently omitted
# several live tables, so validate_database reported SUCCESS on a DB missing one
# of them. The guard test in the suite asserts this stays in lockstep.
REQUIRED_TABLES: frozenset[str] = frozenset(Base.metadata.tables.keys())


def run_migrations(db_path: Path) -> RunResult:
    """Apply pending local migrations."""
    started_at = naive_utc_now()
    init_database(db_path)

    applied_versions: list[str] = []
    with session_scope(db_path) as session:
        applied = {row.version for row in session.query(SchemaMigrationDB).all()}

    for migration in MIGRATIONS:
        if migration.version in applied:
            continue
        migration.apply(db_path)
        with session_scope(db_path) as session:
            session.add(
                SchemaMigrationDB(
                    version=migration.version,
                    description=migration.description,
                    applied_at=naive_utc_now(),
                )
            )
        applied_versions.append(migration.version)

    result = RunResult(
        status="success",
        started_at=started_at,
        finished_at=naive_utc_now(),
        counts={"applied": len(applied_versions)},
        artifacts={},
        details={"applied_versions": applied_versions},
    )
    if applied_versions:
        record_db_operation(db_path, operation="migrate", result=result)
    return result


_schema_ready: set[str] = set()


def ensure_schema(db_path: Path) -> None:
    """Initialize + migrate a database once per process.

    Services call this from their constructors; memoizing avoids re-running the
    init/migration check on every construction (several happen per Streamlit render).
    """
    key = str(db_path)
    if key in _schema_ready:
        return
    init_database(db_path)
    run_migrations(db_path)
    _schema_ready.add(key)


def clear_schema_cache() -> None:
    """Reset the ensure_schema memo (used by tests for isolation)."""
    _schema_ready.clear()


def invalidate_db_caches() -> None:
    """Drop per-process caches after complete database content is published.

    Promotion rewrites an existing live database through SQLite's backup API or
    publishes a snapshot when the target is absent. Both caches key off the path,
    so clearing them symmetrically ensures later access observes and validates the
    published schema instead of trusting stale engine or schema state.
    """
    clear_engine_cache()
    clear_schema_cache()


def validate_database(db_path: Path, *, record: bool = True) -> RunResult:
    """Validate required local schema tables, structural integrity, and FK consistency.

    Runs three checks in order:
    1. Table-presence check against REQUIRED_TABLES.
    2. PRAGMA integrity_check — detects corrupted pages, malformed records, etc.
    3. PRAGMA foreign_key_check — detects orphaned FK references (schema declares
       none today, so this always returns empty; it future-proofs the check).
    """
    started_at = naive_utc_now()
    init_database(db_path)
    schema_errors: list[str] = []
    try:
        inspector = inspect(get_engine(db_path))
        existing = set(inspector.get_table_names())
    except SQLAlchemyError as exc:
        # A corrupted/unreadable DB can make schema reflection raise; report it as a
        # failure (the PRAGMA below also flags it) instead of crashing validation.
        existing = set()
        schema_errors.append(f"schema_inspect: {exc}")
    missing = sorted(REQUIRED_TABLES - existing)
    errors = [f"Missing table: {table}" for table in missing] + schema_errors

    # Column-presence check: a legacy DB that HAS a required table but is missing a
    # migration-added column (e.g. transactions.currency) passes the table check while
    # being functionally broken. Compare each present required table's columns against
    # the ORM model (the same Base.metadata REQUIRED_TABLES is derived from).
    column_errors: list[str] = []
    stale_column_errors: list[str] = []
    try:
        col_inspector = inspect(get_engine(db_path))
        for table_name in sorted(REQUIRED_TABLES & existing):
            expected = set(Base.metadata.tables[table_name].columns.keys())
            live_columns = col_inspector.get_columns(table_name)
            actual = {col["name"] for col in live_columns}
            column_errors.extend(
                f"Missing column: {table_name}.{col}" for col in sorted(expected - actual)
            )
            # The mirror check. A column the ORM no longer maps is only benign while
            # it is nullable; a legacy NOT NULL column with no server default makes
            # every ORM INSERT fail, because the ORM omits it and SQLite rejects the
            # row. Checking only (expected - actual) reported SUCCESS on exactly such
            # a database -- see savings_goals.current_amount.
            for col in live_columns:
                name = str(col["name"])
                if name in expected:
                    continue
                if not col.get("nullable", True) and col.get("default") is None:
                    stale_column_errors.append(
                        f"Stale non-nullable column: {table_name}.{name} "
                        "(not mapped by the ORM; inserts will fail)"
                    )
    except SQLAlchemyError as exc:
        column_errors.append(f"column_inspect: {exc}")
    errors = errors + column_errors + stale_column_errors

    # PRAGMA integrity_check — read-only, returns "ok" on a clean DB.
    integrity_errors: list[str] = []
    fk_errors: list[str] = []
    try:
        with closing(sqlite3.connect(db_path)) as conn:
            integrity_rows = conn.execute("PRAGMA integrity_check").fetchall()
            for row in integrity_rows:
                if str(row[0]).lower() != "ok":
                    integrity_errors.append(f"integrity_check: {row[0]}")

            fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
            for row in fk_rows:
                fk_errors.append(
                    f"foreign_key_check: table={row[0]} rowid={row[1]} parent={row[2]} fkid={row[3]}"
                )
    except sqlite3.DatabaseError as exc:
        # A corrupted/malformed file can make the PRAGMAs raise rather than return
        # error rows; report it as an integrity failure instead of crashing validation.
        integrity_errors.append(f"integrity_check: {exc}")

    errors = errors + integrity_errors + fk_errors
    status = "success" if not errors else "failed"
    result = RunResult(
        status=status,
        started_at=started_at,
        finished_at=naive_utc_now(),
        counts={"tables": len(existing), "missing": len(missing)},
        errors=errors,
        details={
            "missing_tables": missing,
            "missing_columns": len(column_errors),
            "stale_columns": len(stale_column_errors),
            "integrity_errors": len(integrity_errors),
            "fk_errors": len(fk_errors),
        },
    )
    if record:
        record_db_operation(db_path, operation="validate", result=result)
    return result


def migration_status(db_path: Path) -> dict[str, Any]:
    init_database(db_path)
    with session_scope(db_path) as session:
        applied = {row.version: row.applied_at for row in session.query(SchemaMigrationDB).all()}
    return {
        "total": len(MIGRATIONS),
        "applied": len(applied),
        "pending": [
            migration.version for migration in MIGRATIONS if migration.version not in applied
        ],
        "applied_versions": applied,
    }
