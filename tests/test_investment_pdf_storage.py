"""Synthetic PDF-only investment persistence and old-schema upgrade contracts."""

from __future__ import annotations

import hashlib
import sqlite3
from contextlib import closing
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect

from ledger.investments.models import InvestmentAccount, InvestmentAssetBalance, InvestmentStatement
from ledger.investments.repository import InvestmentConflictError, InvestmentRepository
from ledger.storage.database import (
    InvestmentAccountDB,
    InvestmentActivityDB,
    InvestmentAssetBalanceDB,
    InvestmentSourceAssociationDB,
    InvestmentSourceFileDB,
    InvestmentStatementDB,
    get_engine,
    init_database,
    session_scope,
)
from ledger.storage.migrations import run_migrations

MIGRATION = "20260912_0029_investment_pdf_statement_fields"
REBUILT_TABLES = (
    "investment_accounts",
    "investment_statements",
    "investment_asset_balances",
)


def pdf_statement(*, revision: str = "one") -> InvestmentStatement:
    return InvestmentStatement.model_validate(
        {
            "statement_id": "synthetic-pdf-period",
            "account": {
                "account_id": "synthetic-pdf-account",
                "institution": "lumen",
                "source_account_id": "synthetic-source-account",
                "account_alias": "Synthetic TFSA",
                "account_type": "TFSA",
                "base_currency": "CAD",
                "status": "unknown",
            },
            "period_start": date(2026, 7, 1),
            "period_end": date(2026, 7, 31),
            "base_currency": "CAD",
            "total_nav": Decimal("123.123456789012"),
            "asset_balances": [
                {"asset_class": "Stocks", "current_total": Decimal("123.123456789012")}
            ],
            "positions": [],
            "activities": [
                {
                    "activity_id": f"synthetic-{kind}",
                    "activity_type": kind,
                    "activity_date": date(2026, 7, 15),
                    "symbol": "SYN",
                    "currency": "CAD",
                    "quantity": Decimal("1.123456789012"),
                    "source_row": row,
                }
                for row, kind in enumerate(("stock_loan", "stock_recall"), start=1)
            ],
            "parsed_fingerprint": hashlib.sha256(revision.encode()).hexdigest(),
        }
    )


def store_pdf(repo: InvestmentRepository, value: InvestmentStatement, *, token="one", **kwargs):
    return repo.store_statement(
        value,
        source_csv_path=None,
        source_csv_sha256=None,
        source_pdf_path=Path("/synthetic/statement.pdf"),
        source_pdf_sha256=hashlib.sha256(token.encode()).hexdigest(),
        source_pdf_role="statement_pdf",
        **kwargs,
    )


def test_pdf_statement_models_represent_missing_fields_without_inventing_values() -> None:
    value = pdf_statement()
    assert value.generated_at is None
    assert value.account.status_confirmed_on is None
    assert value.account.status == "unknown"
    balance = value.asset_balances[0]
    assert balance.prior_total is None
    assert balance.current_long is None
    assert balance.current_short is None
    assert balance.change is None
    assert balance.current_total == Decimal("123.123456789012")
    with pytest.raises(ValidationError, match="current_total"):
        InvestmentAssetBalance(asset_class="Stocks")


def test_pdf_only_store_is_idempotent_with_one_structured_source(tmp_path) -> None:
    repo = InvestmentRepository(tmp_path / "ledger.db")
    value = pdf_statement()
    assert store_pdf(repo, value) == "inserted"
    assert store_pdf(repo, value) == "unchanged"
    # Repository initialization can precede first migration on a fresh database.
    # The legacy normalization step must retain this newly legitimate status.
    run_migrations(repo.db_path)
    with session_scope(repo.db_path) as session:
        assert session.query(InvestmentStatementDB).one().generated_at is None
        account = session.query(InvestmentAccountDB).one()
        assert account.status == "unknown"
        assert account.status_confirmed_on is None
        balance = session.query(InvestmentAssetBalanceDB).one()
        assert balance.prior_total is None
        assert balance.current_long is None
        assert balance.current_short is None
        assert balance.change is None
        assert balance.current_total == Decimal("123.123456789012")
        source = session.query(InvestmentSourceFileDB).one()
        association = session.query(InvestmentSourceAssociationDB).one()
        for row in (source, association):
            assert row.source_role == "statement_pdf"
            assert row.structured_import is True
            assert row.statement_id == value.statement_id
            assert row.source_path == "/synthetic/statement.pdf"
        activities = session.query(InvestmentActivityDB).all()
        assert {row.activity_type for row in activities} == {"stock_loan", "stock_recall"}
        assert all(row.amount is None for row in activities)
        assert all(row.quantity == Decimal("1.123456789012") for row in activities)


@pytest.mark.parametrize("reuse_hash", [False, True])
def test_pdf_replacement_preserves_historical_and_current_associations(
    tmp_path, reuse_hash
) -> None:
    repo = InvestmentRepository(tmp_path / "ledger.db")
    original, replacement = pdf_statement(), pdf_statement(revision="two")
    assert store_pdf(repo, original) == "inserted"
    token = "one" if reuse_hash else "two"
    with pytest.raises(InvestmentConflictError, match="explicit replacement"):
        store_pdf(repo, replacement, token=token)
    assert store_pdf(repo, replacement, token=token, replace_conflict=True) == "replaced"
    assert store_pdf(repo, replacement, token=token) == "unchanged"
    with session_scope(repo.db_path) as session:
        associations = session.query(InvestmentSourceAssociationDB).all()
        assert len(associations) == 2
        historical = next(row for row in associations if row.superseded_at is not None)
        current = next(row for row in associations if row.superseded_at is None)
        assert historical.statement_fingerprint == original.parsed_fingerprint
        assert historical.superseded_by_statement_id == replacement.statement_id
        assert current.statement_fingerprint == replacement.parsed_fingerprint
        assert session.query(InvestmentSourceFileDB).count() == (1 if reuse_hash else 2)
        assert session.query(InvestmentActivityDB).count() == 2
    with closing(sqlite3.connect(repo.db_path)) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_pdf_source_cannot_be_reused_by_another_account_or_period(tmp_path) -> None:
    repo = InvestmentRepository(tmp_path / "ledger.db")
    value = pdf_statement()
    assert store_pdf(repo, value) == "inserted"
    other = value.model_copy(
        update={
            "statement_id": "synthetic-other-period",
            "period_start": date(2026, 8, 1),
            "period_end": date(2026, 8, 31),
            "parsed_fingerprint": "b" * 64,
        }
    )
    with pytest.raises(InvestmentConflictError, match="source provenance"):
        store_pdf(repo, other)
    with session_scope(repo.db_path) as session:
        assert session.query(InvestmentStatementDB).count() == 1
        assert session.query(InvestmentSourceAssociationDB).count() == 1


def test_pdf_repeated_same_source_replacements_keep_every_revision(tmp_path) -> None:
    repo = InvestmentRepository(tmp_path / "ledger.db")
    assert store_pdf(repo, pdf_statement()) == "inserted"
    for revision in ("two", "one", "two"):
        value = pdf_statement(revision=revision)
        assert store_pdf(repo, value, replace_conflict=True) == "replaced"
        assert store_pdf(repo, value) == "unchanged"
    with session_scope(repo.db_path) as session:
        rows = session.query(InvestmentSourceAssociationDB).all()
        assert len(rows) == 4
        assert sum(row.superseded_at is None for row in rows) == 1
        assert session.query(InvestmentSourceFileDB).count() == 1


@pytest.mark.parametrize(
    "csv_path,csv_hash,pdf_role",
    [(Path("/synthetic/source.csv"), None, "pdf"), (None, "a" * 64, "pdf"), (None, None, "pdf")],
)
def test_partial_or_ambiguous_pdf_source_contract_is_rejected(
    tmp_path, csv_path, csv_hash, pdf_role
):
    repo = InvestmentRepository(tmp_path / "ledger.db")
    with pytest.raises(ValueError, match="source"):
        repo.store_statement(
            pdf_statement(),
            source_csv_path=csv_path,
            source_csv_sha256=csv_hash,
            source_pdf_path=Path("/synthetic/source.pdf"),
            source_pdf_sha256="b" * 64,
            source_pdf_role=pdf_role,
        )
    with session_scope(repo.db_path) as session:
        assert session.query(InvestmentStatementDB).count() == 0


def test_later_pdf_import_preserves_operator_confirmed_lifecycle(tmp_path) -> None:
    repo = InvestmentRepository(tmp_path / "ledger.db")
    value = pdf_statement()
    assert store_pdf(repo, value) == "inserted"
    repo.set_account_lifecycle(
        value.account.account_alias,
        "closed",
        date(2026, 8, 5),
        "Synthetic destination",
        date(2026, 8, 3),
    )
    later = value.model_copy(
        update={
            "statement_id": "synthetic-next-period",
            "period_start": date(2026, 8, 1),
            "period_end": date(2026, 8, 31),
            "parsed_fingerprint": "c" * 64,
        }
    )
    repo.store_statement(
        later,
        source_pdf_path=Path("/synthetic/next.pdf"),
        source_pdf_sha256="d" * 64,
        source_pdf_role="statement_pdf",
    )
    with session_scope(repo.db_path) as session:
        account = session.query(InvestmentAccountDB).one()
        assert account.status == "closed"
        assert account.status_confirmed_on == date(2026, 8, 5)
        assert account.closed_on == date(2026, 8, 3)
        assert account.transfer_destination == "Synthetic destination"
        assert account.last_statement_date == date(2026, 8, 31)


def _legacy_schema(db_path: Path) -> None:
    """Recreate the exact old constraints while retaining every synthetic row."""
    with closing(sqlite3.connect(db_path)) as conn, conn:
        for table in REBUILT_TABLES:
            sql = conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[
                0
            ]
            sql = sql.replace("'active', 'closed', 'unknown'", "'active', 'closed'")
            columns = {
                "investment_statements": ("generated_at",),
                "investment_asset_balances": (
                    "prior_total",
                    "current_long",
                    "current_short",
                    "change",
                ),
            }.get(table, ())
            for column in columns:
                column_type = "DATETIME" if column == "generated_at" else "TEXT"
                sql = sql.replace(f"{column} {column_type},", f"{column} {column_type} NOT NULL,")
            indexes = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL",
                (table,),
            ).fetchall()
            sql = sql.replace(f'CREATE TABLE "{table}"', f"CREATE TABLE {table}", 1)
            conn.execute(sql.replace(f"CREATE TABLE {table}", f"CREATE TABLE {table}__legacy", 1))
            conn.execute(f"INSERT INTO {table}__legacy SELECT * FROM {table}")
            conn.execute(f"DROP TABLE {table}")
            conn.execute(f"ALTER TABLE {table}__legacy RENAME TO {table}")
            for (index_sql,) in indexes:
                conn.execute(index_sql)
        conn.execute("DELETE FROM schema_migrations WHERE version = ?", (MIGRATION,))


def test_pdf_field_migration_preserves_old_values_relationships_and_schema_parity(tmp_path) -> None:
    db_path, fresh_path = tmp_path / "legacy.db", tmp_path / "fresh.db"
    run_migrations(db_path)
    repo = InvestmentRepository(db_path)
    value = pdf_statement()
    value = value.model_copy(
        update={
            "account": InvestmentAccount(
                **{
                    **value.account.model_dump(),
                    "institution": "summit",
                    "status": "active",
                    "status_confirmed_on": date(2026, 8, 1),
                }
            ),
            "generated_at": datetime(2026, 8, 1),
            "asset_balances": (
                InvestmentAssetBalance(
                    asset_class="Stocks",
                    current_total=value.total_nav,
                    prior_total=Decimal("100"),
                    current_long=value.total_nav,
                    current_short=Decimal("0"),
                    change=Decimal("23.123456789012"),
                ),
            ),
        }
    )
    assert store_pdf(repo, value) == "inserted"
    with session_scope(db_path) as session:
        account = session.query(InvestmentAccountDB).one()
        session.add(
            InvestmentAccountDB(
                **{
                    column.name: (
                        "synthetic-closed-account"
                        if column.name == "account_id"
                        else "closed"
                        if column.name == "status"
                        else getattr(account, column.name)
                    )
                    for column in InvestmentAccountDB.__table__.columns
                }
            )
        )
    _legacy_schema(db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        before = {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in (
                *REBUILT_TABLES,
                "investment_activities",
                "investment_source_files",
                "investment_source_associations",
            )
        }
        assert (
            "'unknown'"
            not in conn.execute(
                "SELECT sql FROM sqlite_master WHERE name='investment_accounts'"
            ).fetchone()[0]
        )
        assert (
            next(
                row[3]
                for row in conn.execute("PRAGMA table_info(investment_statements)")
                if row[1] == "generated_at"
            )
            == 1
        )
        balance_columns = {
            row[1]: row[3] for row in conn.execute("PRAGMA table_info(investment_asset_balances)")
        }
        assert all(
            balance_columns[column] == 1
            for column in (
                "prior_total",
                "current_long",
                "current_short",
                "change",
                "current_total",
            )
        )
    result = run_migrations(db_path)
    assert result.details["applied_versions"] == [MIGRATION]
    assert run_migrations(db_path).counts["applied"] == 0
    init_database(fresh_path)
    with closing(sqlite3.connect(db_path)) as conn:
        for table, rows in before.items():
            assert conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() == rows
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    legacy, fresh = inspect(get_engine(db_path)), inspect(get_engine(fresh_path))
    for table in REBUILT_TABLES:
        assert [(c["name"], str(c["type"]), c["nullable"]) for c in legacy.get_columns(table)] == [
            (c["name"], str(c["type"]), c["nullable"]) for c in fresh.get_columns(table)
        ]
        assert legacy.get_indexes(table) == fresh.get_indexes(table)
        assert legacy.get_foreign_keys(table) == fresh.get_foreign_keys(table)
        assert legacy.get_check_constraints(table) == fresh.get_check_constraints(table)
    with session_scope(db_path) as session:
        account = session.get(InvestmentAccountDB, value.account.account_id)
        account.status = "unknown"
        account.status_confirmed_on = None
        session.get(InvestmentStatementDB, value.statement_id).generated_at = None
        balance = session.query(InvestmentAssetBalanceDB).one()
        balance.prior_total = balance.current_long = balance.current_short = balance.change = None
    with (
        closing(sqlite3.connect(db_path)) as conn,
        conn,
        pytest.raises(sqlite3.IntegrityError, match="NOT NULL"),
    ):
        conn.execute("UPDATE investment_asset_balances SET current_total = NULL")


@pytest.mark.parametrize("failure", ["foreign_key", "unexpected_column"])
def test_pdf_field_migration_failure_rolls_back_all_table_rebuilds(tmp_path, failure) -> None:
    db_path = tmp_path / "legacy.db"
    run_migrations(db_path)
    _legacy_schema(db_path)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        if failure == "foreign_key":
            conn.execute(
                "INSERT INTO investment_statements "
                "(statement_id, account_id, period_start, period_end, generated_at, base_currency, "
                "total_nav, parsed_fingerprint, imported_at) VALUES "
                "('synthetic-orphan', 'synthetic-missing-account', '2026-07-01', '2026-07-31', "
                "'2026-08-01', 'CAD', '123.123456789012', ?, '2026-08-01')",
                ("a" * 64,),
            )
        else:
            conn.execute("ALTER TABLE investment_asset_balances ADD COLUMN synthetic_extra TEXT")
        schema_before = conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"
        ).fetchall()
        rows_before = conn.execute("SELECT * FROM investment_statements").fetchall()
    with pytest.raises(ValueError, match="cannot preserve"):
        run_migrations(db_path)
    with closing(sqlite3.connect(db_path)) as conn:
        assert (
            conn.execute(
                "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"
            ).fetchall()
            == schema_before
        )
        assert conn.execute("SELECT * FROM investment_statements").fetchall() == rows_before
        assert (
            conn.execute(
                "SELECT 1 FROM schema_migrations WHERE version = ?", (MIGRATION,)
            ).fetchone()
            is None
        )
