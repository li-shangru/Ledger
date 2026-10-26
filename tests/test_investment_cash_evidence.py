"""Synthetic source cash evidence, exact persistence and publication contracts."""

import hashlib
import sqlite3
from contextlib import closing
from decimal import Decimal
from pathlib import Path

import pytest
from test_investment_pdf_storage import pdf_statement

from ledger.investments import workflow
from ledger.investments.models import InvestmentCashBalance
from ledger.investments.repository import InvestmentRepository
from ledger.repair.statements import carry_forward_investments
from ledger.storage.database import (
    Base,
    InvestmentCashBalanceDB,
    InvestmentStatementDB,
    init_database,
    session_scope,
)
from ledger.storage.exact_decimal import INVESTMENT_EXACT_DECIMAL_COLUMNS
from ledger.storage.migrations import MIGRATIONS, run_migrations

MIGRATION = "20260913_0030_investment_cash_balances"
TABLE = "investment_cash_balances"


def _store(repo, statement, *, source="one", **kwargs):
    return repo.store_statement(
        statement,
        source_pdf_path=Path(f"/synthetic/{source}.pdf"),
        source_pdf_sha256=hashlib.sha256(source.encode()).hexdigest(),
        source_pdf_role="statement_pdf",
        **kwargs,
    )


def _investment_rows(db_path, *, exclude=()):
    with closing(sqlite3.connect(db_path)) as connection:
        return {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in Base.metadata.tables
            if table.startswith("investment_") and table not in exclude
        }


def test_cash_persistence_keeps_all_source_precision_and_null_fx(tmp_path):
    value = pdf_statement().model_copy(
        update={
            "cash_balances": (
                InvestmentCashBalance(
                    currency="USD",
                    opening_balance=Decimal("123.123456789012"),
                    debits=Decimal("2.000000000001"),
                    credits=Decimal("3.111111111111"),
                    closing_balance=Decimal("124.234567900122"),
                    opening_fx_rate=Decimal("1.123456789012"),
                    closing_fx_rate=Decimal("1.234567890123"),
                ),
                InvestmentCashBalance(
                    currency="CAD",
                    opening_balance=Decimal("0.00"),
                    debits=Decimal("0.00"),
                    credits=Decimal("0.00"),
                    closing_balance=Decimal("0.00"),
                ),
            )
        }
    )
    repo = InvestmentRepository(tmp_path / "synthetic.db")
    assert _store(repo, value) == "inserted"
    assert _store(repo, value) == "unchanged"
    with session_scope(repo.db_path) as session:
        for expected in value.cash_balances:
            stored = session.get(InvestmentCashBalanceDB, (value.statement_id, expected.currency))
            for field, amount in expected.model_dump().items():
                actual = getattr(stored, field)
                if isinstance(amount, Decimal):
                    assert actual.as_tuple() == amount.as_tuple()
                else:
                    assert actual == amount
    with closing(sqlite3.connect(repo.db_path)) as connection:
        columns = INVESTMENT_EXACT_DECIMAL_COLUMNS[TABLE]
        raw = connection.execute(
            f"SELECT {', '.join(columns)} FROM {TABLE} WHERE currency = 'USD'"
        ).fetchone()
        assert raw == tuple(str(getattr(value.cash_balances[0], field)) for field in columns)
        assert all(isinstance(item, str) for item in raw)


def test_0029_upgrade_preserves_old_facts_and_unavailable_cash(tmp_path):
    db_path = tmp_path / "synthetic.db"
    run_migrations(db_path)
    repo = InvestmentRepository(db_path)
    value = pdf_statement()
    _store(repo, value)
    before = _investment_rows(db_path, exclude=(TABLE,))
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute(f"DROP TABLE {TABLE}")
        connection.execute("DELETE FROM schema_migrations WHERE version = ?", (MIGRATION,))
    result = run_migrations(db_path)
    assert result.details["applied_versions"] == [MIGRATION]
    assert _investment_rows(db_path, exclude=(TABLE,)) == before
    with session_scope(db_path) as session:
        assert session.query(InvestmentCashBalanceDB).count() == 0
        assert (
            session.query(InvestmentStatementDB).one().parsed_fingerprint
            == value.parsed_fingerprint
        )
    assert run_migrations(db_path).counts["applied"] == 0
    assert _store(repo, value) == "unchanged"


def test_cash_migration_can_create_missing_table_and_repeat_without_other_migrations(tmp_path):
    db_path = tmp_path / "synthetic.db"
    init_database(db_path)
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute(f"DROP TABLE {TABLE}")
    migration = next(migration for migration in MIGRATIONS if migration.version == MIGRATION)
    migration.apply(db_path)
    migration.apply(db_path)
    with closing(sqlite3.connect(db_path)) as connection:
        assert connection.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone() == (0,)


def _legacy_repair_source(db_path):
    run_migrations(db_path)
    _store(InvestmentRepository(db_path), pdf_statement())
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute(f"DROP TABLE {TABLE}")
        connection.execute("DELETE FROM schema_migrations WHERE version = ?", (MIGRATION,))


def test_repair_carries_exact_0029_source_to_current_candidate_without_touching_source(tmp_path):
    live, candidate = tmp_path / "synthetic-0029.db", tmp_path / "synthetic-current.db"
    _legacy_repair_source(live)
    run_migrations(candidate)
    before = live.read_bytes()
    expected = _investment_rows(live, exclude=(TABLE,))
    counts = carry_forward_investments(live, candidate)
    assert counts[TABLE] == 0
    assert _investment_rows(candidate, exclude=(TABLE,)) == expected
    with closing(sqlite3.connect(candidate)) as connection:
        assert connection.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone() == (0,)
    assert live.read_bytes() == before


def test_legacy_cash_layout_stays_bound_to_0029_after_a_later_migration_is_added(
    tmp_path, monkeypatch
):
    from ledger.storage import migrations

    live = tmp_path / "synthetic-0029.db"
    _legacy_repair_source(live)
    later_version = "20400101_0031_synthetic_future"
    monkeypatch.setattr(
        migrations,
        "MIGRATIONS",
        (
            *migrations.MIGRATIONS,
            migrations.Migration(later_version, "synthetic", lambda path: None),
        ),
    )
    with closing(sqlite3.connect(live)) as connection, connection:
        assert migrations.is_legacy_investment_cash_schema(connection)
        connection.execute(
            "INSERT INTO schema_migrations VALUES (?, 'synthetic', '2040-01-01')", (later_version,)
        )
        assert not migrations.is_legacy_investment_cash_schema(connection)


def test_legacy_layout_refuses_a_view_at_the_optional_cash_table_name(tmp_path):
    from ledger.storage.migrations import is_legacy_investment_cash_schema

    live, candidate = tmp_path / "synthetic-0029.db", tmp_path / "synthetic-current.db"
    _legacy_repair_source(live)
    run_migrations(candidate)
    with closing(sqlite3.connect(live)) as connection, connection:
        connection.execute(f"CREATE VIEW {TABLE} AS SELECT 1 AS forged")
        assert not is_legacy_investment_cash_schema(connection)
    before = live.read_bytes()
    with pytest.raises(ValueError, match="incompatible schema"):
        carry_forward_investments(live, candidate)
    with pytest.raises(RuntimeError, match="invalid cash evidence storage"):
        workflow._validate_staging_database(live, expected_statements=1)
    assert live.read_bytes() == before


def test_current_table_with_partial_legacy_migration_history_is_rejected(tmp_path):
    live, candidate = tmp_path / "synthetic-partial.db", tmp_path / "synthetic-current.db"
    run_migrations(live)
    run_migrations(candidate)
    with closing(sqlite3.connect(live)) as connection, connection:
        connection.execute("DELETE FROM schema_migrations WHERE version = ?", (MIGRATION,))
    before = live.read_bytes()
    with pytest.raises(ValueError, match="incompatible schema"):
        carry_forward_investments(live, candidate)
    with pytest.raises(RuntimeError, match="invalid cash evidence storage"):
        workflow._validate_staging_database(live, expected_statements=0)
    assert live.read_bytes() == before


@pytest.mark.parametrize(
    "malformation", ["other_missing", "cash_marker_present", "older_marker_missing", "partial"]
)
def test_repair_rejects_unrecognized_optional_absence_without_touching_either_database(
    tmp_path, malformation
):
    live, candidate = tmp_path / "synthetic-source.db", tmp_path / "synthetic-candidate.db"
    _legacy_repair_source(live)
    run_migrations(candidate)
    with closing(sqlite3.connect(live)) as connection, connection:
        if malformation == "other_missing":
            connection.execute("DROP TABLE investment_performance")
        elif malformation == "cash_marker_present":
            connection.execute(
                "INSERT INTO schema_migrations VALUES (?, 'synthetic', '2040-01-01')", (MIGRATION,)
            )
        elif malformation == "older_marker_missing":
            connection.execute(
                "DELETE FROM schema_migrations WHERE version = "
                "'20260912_0029_investment_pdf_statement_fields'"
            )
        else:
            connection.execute(f"CREATE TABLE {TABLE}(statement_id TEXT, currency TEXT)")
    source_before, candidate_before = live.read_bytes(), candidate.read_bytes()
    with pytest.raises(ValueError, match=r"investment.*schema|Investment.*schema"):
        carry_forward_investments(live, candidate)
    assert live.read_bytes() == source_before
    assert candidate.read_bytes() == candidate_before


def test_staging_validation_requires_new_cash_table(tmp_path):
    db_path = tmp_path / "synthetic.db"
    init_database(db_path)
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute(f"DROP TABLE {TABLE}")
    with pytest.raises(RuntimeError, match="invalid cash evidence storage"):
        workflow._validate_staging_database(db_path, expected_statements=0)
