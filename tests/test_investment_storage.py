"""Investment-domain models and additive storage schema tests."""

from __future__ import annotations

import contextlib
import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from ledger.investments.models import InvestmentAccount, InvestmentPerformance, InvestmentPosition
from ledger.storage.database import (
    InvestmentAccountDB,
    InvestmentActivityDB,
    InvestmentAssetBalanceDB,
    InvestmentPerformanceDB,
    InvestmentPositionDB,
    InvestmentStatementDB,
    TransactionDB,
    get_engine,
    get_session,
    init_database,
    session_scope,
)
from ledger.storage.migrations import _ensure_investment_provenance_and_performance, run_migrations


def test_investment_models_are_frozen_and_keep_source_precision() -> None:
    position = InvestmentPosition(
        asset_category="Stocks",
        currency="USD",
        symbol="AAA",
        discriminator="Summary",
        quantity=Decimal("1.123456789"),
        multiplier=Decimal("1"),
        cost_price=Decimal("12.345678901"),
        cost_basis=Decimal("13.872427984"),
        close_price=Decimal("14.25"),
        market_value=Decimal("16.010259246"),
        unrealized_pl=Decimal("2.137831262"),
        source_row=12,
    )

    assert position.quantity == Decimal("1.123456789")
    with pytest.raises(ValidationError):
        position.quantity = Decimal("2")

    performance = InvestmentPerformance(
        asset_category="Stocks",
        symbol="AAA",
        cost_adjustment=Decimal("0.000000000001"),
        realized_short_profit=Decimal("4.123456789012"),
        realized_short_loss=Decimal("-1.000000000000"),
        realized_long_profit=Decimal("0"),
        realized_long_loss=Decimal("0"),
        realized_total=Decimal("3.123456789012"),
        unrealized_short_profit=Decimal("5"),
        unrealized_short_loss=Decimal("-2"),
        unrealized_long_profit=Decimal("0"),
        unrealized_long_loss=Decimal("0"),
        unrealized_total=Decimal("3"),
        total=Decimal("6.123456789012"),
        code="A",
        source_row=14,
    )
    assert performance.total == Decimal("6.123456789012")
    with pytest.raises(ValidationError):
        performance.total = Decimal("7")


@pytest.mark.parametrize(
    "invalid",
    (
        Decimal("0.0000000000001"),
        Decimal("10000000000000000"),
        Decimal("-10000000000000000"),
        Decimal("NaN"),
        Decimal("Infinity"),
    ),
)
def test_exact_investment_decimal_rejects_out_of_contract_values(invalid) -> None:
    """Persistence rejects excess scale/range instead of rounding or coercing."""
    from ledger.storage.exact_decimal import canonical_fixed_point_text

    with pytest.raises(ValueError, match="exact fixed-point"):
        canonical_fixed_point_text(invalid)


def test_exact_investment_decimal_preserves_maximum_value_and_scale() -> None:
    """The canonical representation retains all 28 permitted source digits."""
    from ledger.storage.exact_decimal import canonical_fixed_point_text

    maximum = Decimal("9999999999999999.999999999999")

    assert canonical_fixed_point_text(maximum) == "9999999999999999.999999999999"


def test_investment_account_rejects_transferred_status_as_separate_lifecycle_state() -> None:
    with pytest.raises(ValidationError, match="status"):
        InvestmentAccount(
            account_id="account-invalid-lifecycle",
            institution="summit",
            source_account_id="U-invalid",
            account_alias="Margin",
            account_type="margin",
            base_currency="USD",
            status="transferred",
            transfer_destination="Lumen",
            status_confirmed_on=date(2026, 8, 1),
        )


def test_fresh_investment_account_table_rejects_unsupported_lifecycle_status(tmp_path) -> None:
    db_path = tmp_path / "fresh-lifecycle.db"
    init_database(db_path)

    with (
        contextlib.closing(sqlite3.connect(db_path)) as connection,
        connection,
        pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"),
    ):
        connection.execute(
            "INSERT INTO investment_accounts ("
            "account_id, institution, source_account_id, account_alias, account_type, "
            "base_currency, status, created_at, updated_at) "
            "VALUES ('invalid', 'summit', 'opaque', 'Invalid', 'registered', 'USD', "
            "'transferred', '2026-08-01 00:00:00', '2026-08-01 00:00:00')"
        )


def test_investment_account_persists_closed_transfer_lifecycle_facts(tmp_path) -> None:
    account = InvestmentAccount(
        account_id="account-lifecycle",
        institution="summit",
        source_account_id="U123",
        account_alias="Margin",
        account_type="margin",
        base_currency="USD",
        status="closed",
        transfer_destination="Lumen",
        status_confirmed_on=date(2026, 8, 1),
        closed_on=None,
    )
    assert account.status == "closed"
    assert account.transfer_destination == "Lumen"
    assert account.status_confirmed_on == date(2026, 8, 1)
    assert account.closed_on is None

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    with session_scope(db_path) as session:
        session.add(
            InvestmentAccountDB(
                account_id=account.account_id,
                institution=account.institution,
                source_account_id=account.source_account_id,
                account_alias=account.account_alias,
                account_type=account.account_type,
                base_currency=account.base_currency,
                status=account.status,
                transfer_destination=account.transfer_destination,
                status_confirmed_on=account.status_confirmed_on,
                closed_on=account.closed_on,
                first_statement_date=None,
                last_statement_date=None,
                created_at=datetime(2026, 8, 1),
                updated_at=datetime(2026, 8, 1),
            )
        )

    with session_scope(db_path) as session:
        persisted = session.get(InvestmentAccountDB, account.account_id)
        assert persisted is not None
        assert persisted.status == "closed"
        assert persisted.transfer_destination == "Lumen"
        assert persisted.status_confirmed_on == date(2026, 8, 1)
        assert persisted.closed_on is None


def test_investment_lifecycle_migration_normalizes_legacy_open_and_transferred(tmp_path) -> None:
    db_path = tmp_path / "legacy-lifecycle.db"
    run_migrations(db_path)
    with contextlib.closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute(
            "DELETE FROM schema_migrations "
            "WHERE version = '20260801_0017_investment_lifecycle_values'"
        )
        connection.executescript(
            """
            DROP TABLE investment_accounts;
            CREATE TABLE investment_accounts (
                account_id VARCHAR(128) NOT NULL PRIMARY KEY,
                institution VARCHAR(80) NOT NULL,
                source_account_id VARCHAR(128) NOT NULL,
                account_alias VARCHAR(200) NOT NULL DEFAULT '',
                account_type VARCHAR(100) NOT NULL,
                base_currency VARCHAR(3) NOT NULL,
                status VARCHAR(30) NOT NULL DEFAULT 'open',
                transfer_destination VARCHAR(200),
                status_confirmed_on DATE,
                closed_on DATE,
                first_statement_date DATE,
                last_statement_date DATE,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            );
            """
        )
        connection.executemany(
            "INSERT INTO investment_accounts ("
            "account_id, institution, source_account_id, account_alias, account_type, "
            "base_currency, status, created_at, updated_at) "
            "VALUES (?, 'summit', ?, ?, 'registered', 'USD', ?, ?, ?)",
            [
                (
                    "legacy-open",
                    "opaque-open",
                    "OPEN",
                    "open",
                    "2026-08-01 00:00:00",
                    "2026-08-01 00:00:00",
                ),
                (
                    "legacy-transferred",
                    "opaque-transferred",
                    "TRANSFERRED",
                    "transferred",
                    "2026-08-01 00:00:00",
                    "2026-08-01 00:00:00",
                ),
            ],
        )

    result = run_migrations(db_path)

    assert result.details["applied_versions"] == ["20260801_0017_investment_lifecycle_values"]
    with contextlib.closing(sqlite3.connect(db_path)) as connection, connection:
        assert connection.execute(
            "SELECT account_id, status FROM investment_accounts ORDER BY account_id"
        ).fetchall() == [
            ("legacy-open", "active"),
            ("legacy-transferred", "closed"),
        ]
        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint"):
            connection.execute(
                "INSERT INTO investment_accounts ("
                "account_id, institution, source_account_id, account_alias, account_type, "
                "base_currency, status, created_at, updated_at) "
                "VALUES ('invalid', 'summit', 'opaque', 'Invalid', 'registered', 'USD', "
                "'transferred', '2026-08-01 00:00:00', '2026-08-01 00:00:00')"
            )


def test_investment_account_lifecycle_migration_upgrades_pre_0012_schema(tmp_path) -> None:
    db_path = seed_pre_investment_database(tmp_path)
    init_database(db_path)
    initial_result = run_migrations(db_path)
    assert "20260801_0011_investment_tables" in initial_result.details["applied_versions"]

    with contextlib.closing(sqlite3.connect(db_path)) as connection, connection:
        assert connection.execute(
            "SELECT version FROM schema_migrations WHERE version = '20260801_0011_investment_tables'"
        ).fetchone() == ("20260801_0011_investment_tables",)
        connection.execute(
            "DELETE FROM schema_migrations "
            "WHERE version = '20260801_0012_investment_account_lifecycle'"
        )
        connection.execute("DROP TABLE investment_accounts")
        connection.execute(
            "CREATE TABLE investment_accounts ("
            "account_id VARCHAR(128) NOT NULL PRIMARY KEY,"
            "institution VARCHAR(80) NOT NULL,"
            "source_account_id VARCHAR(128) NOT NULL,"
            "account_alias VARCHAR(200) NOT NULL DEFAULT '',"
            "account_type VARCHAR(100) NOT NULL,"
            "base_currency VARCHAR(3) NOT NULL,"
            "first_statement_date DATE,"
            "last_statement_date DATE,"
            "created_at DATETIME NOT NULL,"
            "updated_at DATETIME NOT NULL"
            ")"
        )
        connection.execute(
            "INSERT INTO investment_accounts "
            "(account_id, institution, source_account_id, account_alias, account_type, "
            "base_currency, created_at, updated_at) "
            "VALUES ('legacy-account', 'summit', 'U123', 'Legacy', 'margin', 'USD', "
            "'2026-07-31 00:00:00', '2026-07-31 00:00:00')"
        )
        connection.execute(
            "INSERT INTO investment_statements "
            "(statement_id, account_id, period_start, period_end, generated_at, "
            "base_currency, total_nav, parsed_fingerprint, imported_at) "
            "VALUES ('legacy-statement', 'legacy-account', '2026-07-01', '2026-07-31', "
            "'2026-07-31 00:00:00', 'USD', 1, 'f', '2026-07-31 00:00:00')"
        )

        pre_upgrade_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(investment_accounts)")
        }
        assert {
            "status",
            "transfer_destination",
            "status_confirmed_on",
            "closed_on",
        }.isdisjoint(pre_upgrade_columns)

    result = run_migrations(db_path)

    assert "20260801_0012_investment_account_lifecycle" in result.details["applied_versions"]
    assert transaction_ids(db_path) == ["existing-transaction"]
    with contextlib.closing(sqlite3.connect(db_path)) as connection, connection:
        post_upgrade_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(investment_accounts)")
        }
        assert {
            "status",
            "transfer_destination",
            "status_confirmed_on",
            "closed_on",
        } <= post_upgrade_columns

        legacy_account = connection.execute(
            "SELECT account_id, status, transfer_destination, status_confirmed_on, closed_on "
            "FROM investment_accounts WHERE account_id = 'legacy-account'"
        ).fetchone()
        assert legacy_account == ("legacy-account", "open", None, None, None)
        assert connection.execute(
            "SELECT statement_id FROM investment_statements WHERE statement_id = 'legacy-statement'"
        ).fetchone() == ("legacy-statement",)

        connection.execute(
            "UPDATE investment_accounts SET status = 'transferred', "
            "transfer_destination = 'Lumen', status_confirmed_on = '2026-08-01', "
            "closed_on = NULL WHERE account_id = 'legacy-account'"
        )
        assert connection.execute(
            "SELECT status, transfer_destination, status_confirmed_on, closed_on "
            "FROM investment_accounts WHERE account_id = 'legacy-account'"
        ).fetchone() == ("transferred", "Lumen", "2026-08-01", None)


def test_investment_tables_exist_and_round_trip_high_precision(tmp_path) -> None:
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    run_migrations(db_path)

    tables = set(inspect(get_engine(db_path)).get_table_names())
    assert {
        "investment_accounts",
        "investment_statements",
        "investment_asset_balances",
        "investment_positions",
        "investment_activities",
        "investment_performance",
        "investment_source_files",
        "investment_import_runs",
    } <= tables

    expected_high = Decimal("9999999999999999.123456789")
    expected_12 = Decimal("0.123456789012")
    now = datetime(2026, 8, 1)
    with session_scope(db_path) as session:
        session.add(
            InvestmentAccountDB(
                account_id="account-1",
                institution="summit",
                source_account_id="U123",
                account_alias="Test",
                account_type="margin",
                base_currency="USD",
                status="active",
                transfer_destination=None,
                status_confirmed_on=date(2026, 8, 1),
                closed_on=None,
                first_statement_date=None,
                last_statement_date=None,
                created_at=now,
                updated_at=now,
            )
        )
        session.flush()
        session.add(
            InvestmentStatementDB(
                statement_id="statement-1",
                account_id="account-1",
                period_start=date(2026, 7, 1),
                period_end=date(2026, 7, 31),
                generated_at=now,
                base_currency="USD",
                total_nav=expected_high,
                time_weighted_return=expected_12,
                parsed_fingerprint="f" * 64,
                imported_at=now,
            )
        )
        session.flush()
        session.add(
            InvestmentAssetBalanceDB(
                statement_id="statement-1",
                asset_class="Stocks",
                prior_total=expected_high,
                current_long=expected_12,
                current_short=-expected_12,
                current_total=expected_high,
                change=expected_12,
            )
        )
        session.add(
            InvestmentPositionDB(
                statement_id="statement-1",
                source_row=12,
                asset_category="Stocks",
                currency="USD",
                symbol="AAA",
                discriminator="Summary",
                quantity=expected_high,
                multiplier=expected_12,
                cost_price=expected_high,
                cost_basis=expected_12,
                close_price=expected_high,
                market_value=expected_high,
                unrealized_pl=expected_12,
            )
        )
        session.add(
            InvestmentPerformanceDB(
                statement_id="statement-1",
                source_row=14,
                asset_category="Total",
                symbol="",
                cost_adjustment=expected_12,
                realized_short_profit=expected_high,
                realized_short_loss=-expected_12,
                realized_long_profit=expected_12,
                realized_long_loss=-expected_12,
                realized_total=expected_high,
                unrealized_short_profit=expected_high,
                unrealized_short_loss=-expected_12,
                unrealized_long_profit=expected_12,
                unrealized_long_loss=-expected_12,
                unrealized_total=expected_high,
                total=expected_high,
                code="",
            )
        )
        session.add(
            InvestmentActivityDB(
                statement_id="statement-1",
                activity_id="activity-1",
                activity_type="trade",
                activity_date=date(2026, 7, 15),
                activity_time=now,
                symbol="AAA",
                currency="USD",
                quantity=expected_high,
                trade_price=expected_12,
                close_price=expected_high,
                amount=expected_high,
                commission_fee=-expected_12,
                basis=expected_high,
                realized_pl=expected_12,
                mtm_pl=-expected_12,
                description="synthetic",
                direction="buy",
                source_row=20,
            )
        )

    with session_scope(db_path) as session:
        statement = session.get(InvestmentStatementDB, "statement-1")
        assert statement is not None
        assert statement.total_nav == expected_high
        assert statement.time_weighted_return == expected_12
        balance = session.get(InvestmentAssetBalanceDB, ("statement-1", "Stocks"))
        assert balance is not None
        assert balance.prior_total == expected_high
        assert balance.current_long == expected_12
        position = session.get(InvestmentPositionDB, ("statement-1", 12))
        assert position is not None
        assert position.quantity == expected_high
        assert position.market_value == expected_high
        assert position.multiplier == expected_12
        performance = session.get(InvestmentPerformanceDB, ("statement-1", 14))
        assert performance is not None
        assert performance.cost_adjustment == expected_12
        assert performance.total == expected_high
        activity = session.get(InvestmentActivityDB, ("statement-1", "activity-1"))
        assert activity is not None
        assert activity.quantity == expected_high
        assert activity.trade_price == expected_12

    with contextlib.closing(sqlite3.connect(db_path)) as connection, connection:
        exact_columns = (
            ("investment_statements", "total_nav"),
            ("investment_asset_balances", "prior_total"),
            ("investment_positions", "quantity"),
            ("investment_performance", "realized_short_profit"),
            ("investment_activities", "amount"),
        )
        for table, column in exact_columns:
            storage_type, value = connection.execute(
                f"SELECT typeof({column}), {column} FROM {table}"
            ).fetchone()
            assert storage_type == "text"
            assert value == format(expected_high, "f")


def test_fresh_investment_schema_uses_text_for_every_decimal_column(tmp_path) -> None:
    """Every persisted investment Decimal opts into the one exact SQLAlchemy type."""
    expected = {
        "investment_statements": {"total_nav", "time_weighted_return"},
        "investment_asset_balances": {
            "prior_total",
            "current_long",
            "current_short",
            "current_total",
            "change",
        },
        "investment_positions": {
            "quantity",
            "multiplier",
            "cost_price",
            "cost_basis",
            "close_price",
            "market_value",
            "unrealized_pl",
        },
        "investment_performance": {
            "cost_adjustment",
            "realized_short_profit",
            "realized_short_loss",
            "realized_long_profit",
            "realized_long_loss",
            "realized_total",
            "unrealized_short_profit",
            "unrealized_short_loss",
            "unrealized_long_profit",
            "unrealized_long_loss",
            "unrealized_total",
            "total",
        },
        "investment_activities": {
            "quantity",
            "trade_price",
            "close_price",
            "amount",
            "commission_fee",
            "basis",
            "realized_pl",
            "mtm_pl",
        },
    }
    db_path = tmp_path / "ledger.db"
    init_database(db_path)

    with contextlib.closing(sqlite3.connect(db_path)) as connection, connection:
        for table, columns in expected.items():
            declared = {
                str(row[1]): str(row[2]).upper()
                for row in connection.execute(f"PRAGMA table_info({table})")
            }
            assert {column: declared[column] for column in columns} == dict.fromkeys(
                columns, "TEXT"
            )


_INVESTMENT_DECIMAL_TABLES = (
    "investment_statements",
    "investment_asset_balances",
    "investment_positions",
    "investment_performance",
    "investment_activities",
)


def _replace_exact_investment_tables_with_legacy_numeric_schema(db_path) -> None:
    with contextlib.closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.executescript(
            """
            DROP TABLE investment_activities;
            DROP TABLE investment_performance;
            DROP TABLE investment_positions;
            DROP TABLE investment_asset_balances;
            DROP TABLE investment_statements;

            CREATE TABLE investment_statements (
                statement_id VARCHAR(128) NOT NULL PRIMARY KEY,
                account_id VARCHAR(128) NOT NULL,
                period_start DATE NOT NULL,
                period_end DATE NOT NULL,
                generated_at DATETIME NOT NULL,
                base_currency VARCHAR(3) NOT NULL,
                total_nav NUMERIC(28, 12) NOT NULL,
                time_weighted_return NUMERIC(28, 12),
                parsed_fingerprint VARCHAR(64) NOT NULL UNIQUE,
                imported_at DATETIME NOT NULL,
                CONSTRAINT uq_investment_statement_account_period
                    UNIQUE (account_id, period_start, period_end),
                FOREIGN KEY(account_id) REFERENCES investment_accounts(account_id)
            );
            CREATE INDEX ix_investment_statements_account_id
                ON investment_statements(account_id);
            CREATE INDEX ix_investment_statements_period_start
                ON investment_statements(period_start);
            CREATE INDEX ix_investment_statements_period_end
                ON investment_statements(period_end);
            CREATE INDEX ix_investment_statement_account_period
                ON investment_statements(account_id, period_start, period_end);

            CREATE TABLE investment_asset_balances (
                statement_id VARCHAR(128) NOT NULL,
                asset_class VARCHAR(100) NOT NULL,
                prior_total NUMERIC(28, 12) NOT NULL,
                current_long NUMERIC(28, 12) NOT NULL,
                current_short NUMERIC(28, 12) NOT NULL,
                current_total NUMERIC(28, 12) NOT NULL,
                change NUMERIC(28, 12) NOT NULL,
                PRIMARY KEY (statement_id, asset_class),
                FOREIGN KEY(statement_id) REFERENCES investment_statements(statement_id)
            );

            CREATE TABLE investment_positions (
                statement_id VARCHAR(128) NOT NULL,
                source_row INTEGER NOT NULL,
                asset_category VARCHAR(100) NOT NULL,
                currency VARCHAR(3) NOT NULL,
                symbol VARCHAR(100) NOT NULL,
                discriminator VARCHAR(100) NOT NULL,
                quantity NUMERIC(28, 12) NOT NULL,
                multiplier NUMERIC(28, 12),
                cost_price NUMERIC(28, 12),
                cost_basis NUMERIC(28, 12),
                close_price NUMERIC(28, 12),
                market_value NUMERIC(28, 12),
                unrealized_pl NUMERIC(28, 12),
                PRIMARY KEY (statement_id, source_row),
                FOREIGN KEY(statement_id) REFERENCES investment_statements(statement_id)
            );
            CREATE INDEX ix_investment_position_statement_symbol
                ON investment_positions(statement_id, symbol);

            CREATE TABLE investment_performance (
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
                FOREIGN KEY(statement_id) REFERENCES investment_statements(statement_id)
            );
            CREATE INDEX ix_investment_performance_statement_asset
                ON investment_performance(statement_id, asset_category);

            CREATE TABLE investment_activities (
                statement_id VARCHAR(128) NOT NULL,
                activity_id VARCHAR(128) NOT NULL,
                activity_type VARCHAR(32) NOT NULL,
                activity_date DATE NOT NULL,
                activity_time DATETIME,
                symbol VARCHAR(100),
                currency VARCHAR(3) NOT NULL,
                quantity NUMERIC(28, 12),
                trade_price NUMERIC(28, 12),
                close_price NUMERIC(28, 12),
                amount NUMERIC(28, 12),
                commission_fee NUMERIC(28, 12),
                basis NUMERIC(28, 12),
                realized_pl NUMERIC(28, 12),
                mtm_pl NUMERIC(28, 12),
                description TEXT NOT NULL DEFAULT '',
                direction VARCHAR(32),
                source_row INTEGER NOT NULL,
                PRIMARY KEY (statement_id, activity_id),
                FOREIGN KEY(statement_id) REFERENCES investment_statements(statement_id)
            );
            CREATE INDEX ix_investment_activities_activity_type
                ON investment_activities(activity_type);
            CREATE INDEX ix_investment_activities_activity_date
                ON investment_activities(activity_date);
            CREATE INDEX ix_investment_activities_symbol
                ON investment_activities(symbol);
            CREATE INDEX ix_investment_activity_statement_date
                ON investment_activities(statement_id, activity_date);
            """
        )
        connection.execute("PRAGMA foreign_keys = ON")


def _seed_legacy_investment_decimal_rows(db_path, *, activity_amount: str) -> None:
    now = "2026-08-01 12:00:00"
    with contextlib.closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute(
            "INSERT INTO investment_accounts "
            "(account_id, institution, source_account_id, account_alias, account_type, "
            "base_currency, status, status_confirmed_on, created_at, updated_at) "
            "VALUES ('legacy-account', 'summit', 'U123', 'Legacy', 'margin', 'USD', "
            "'closed', '2026-08-01', ?, ?)",
            (now, now),
        )
        connection.execute(
            "INSERT INTO investment_statements "
            "(statement_id, account_id, period_start, period_end, generated_at, "
            "base_currency, total_nav, time_weighted_return, parsed_fingerprint, imported_at) "
            "VALUES ('legacy-statement', 'legacy-account', '2026-07-01', '2026-07-31', "
            "?, 'USD', '123.123456789', '0.123456789012', ?, ?)",
            (now, "1" * 64, now),
        )
        connection.execute(
            "INSERT INTO investment_asset_balances VALUES "
            "('legacy-statement', 'Stocks', '123.123456789', '0.123456789012', "
            "'-0.123456789012', '123.123456789', '0.123456789012')"
        )
        connection.execute(
            "INSERT INTO investment_positions VALUES "
            "('legacy-statement', 10, 'Stocks', 'USD', 'AAA', 'Summary', "
            "'123.123456789', '0.123456789012', '123.123456789', '0.123456789012', "
            "'123.123456789', '123.123456789', '0.123456789012')"
        )
        connection.execute(
            "INSERT INTO investment_performance VALUES "
            "('legacy-statement', 15, 'Total', '', '0.123456789012', "
            "'123.123456789', '-0.123456789012', '0.123456789012', "
            "'-0.123456789012', '123.123456789', '123.123456789', "
            "'-0.123456789012', '0.123456789012', '-0.123456789012', "
            "'123.123456789', '123.123456789', '')"
        )
        connection.execute(
            "INSERT INTO investment_activities "
            "(statement_id, activity_id, activity_type, activity_date, currency, quantity, "
            "trade_price, close_price, amount, commission_fee, basis, realized_pl, mtm_pl, "
            "description, source_row) VALUES "
            "('legacy-statement', 'legacy-activity', 'deposit', '2026-07-15', 'USD', "
            "'123.123456789', '0.123456789012', '123.123456789', ?, "
            "'-0.123456789012', '123.123456789', '0.123456789012', "
            "'-0.123456789012', 'synthetic', 20)",
            (activity_amount,),
        )
        connection.execute(
            "INSERT INTO investment_source_files "
            "(file_hash, source_path, source_role, structured_import, byte_size, "
            "archive_status, statement_id, discovered_at, status) VALUES "
            "(?, '/synthetic/legacy.csv', 'csv', 1, 1, 'pending', "
            "'legacy-statement', ?, 'staged')",
            ("2" * 64, now),
        )


def _investment_schema_contract(db_path) -> dict:
    contract = {}
    with contextlib.closing(sqlite3.connect(db_path)) as connection, connection:
        for table in _INVESTMENT_DECIMAL_TABLES:
            indexes = []
            for index in connection.execute(f"PRAGMA index_list({table})"):
                name = str(index[1])
                contract_name = (
                    "<sqlite-autoindex>" if name.startswith("sqlite_autoindex_") else name
                )
                columns = tuple(
                    str(row[2]) for row in connection.execute(f"PRAGMA index_info({name})")
                )
                indexes.append((contract_name, int(index[2]), str(index[3]), columns))
            contract[table] = {
                "columns": tuple(
                    (str(row[1]), str(row[2]).upper(), int(row[3]), int(row[5]))
                    for row in connection.execute(f"PRAGMA table_info({table})")
                ),
                "indexes": tuple(sorted(indexes)),
                "foreign_keys": tuple(
                    sorted(
                        (str(row[3]), str(row[2]), str(row[4]))
                        for row in connection.execute(f"PRAGMA foreign_key_list({table})")
                    )
                ),
            }
    return contract


def test_migration_0016_upgrades_legacy_numeric_rows_with_fresh_schema_parity(
    tmp_path,
) -> None:
    """0016 preserves rows/FKs/indexes and becomes a no-op after exact upgrade."""
    from ledger.storage.migrations import _ensure_investment_exact_decimals

    db_path = tmp_path / "legacy.db"
    fresh_path = tmp_path / "fresh.db"
    run_migrations(db_path)
    _replace_exact_investment_tables_with_legacy_numeric_schema(db_path)
    _seed_legacy_investment_decimal_rows(
        db_path,
        activity_amount="123.123456789",
    )
    with contextlib.closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute(
            "DELETE FROM schema_migrations "
            "WHERE version = '20260801_0016_investment_exact_decimals'"
        )

    result = run_migrations(db_path)

    assert result.details["applied_versions"] == ["20260801_0016_investment_exact_decimals"]
    init_database(fresh_path)
    assert _investment_schema_contract(db_path) == _investment_schema_contract(fresh_path)
    with contextlib.closing(sqlite3.connect(db_path)) as connection, connection:
        assert connection.execute(
            "SELECT typeof(total_nav), total_nav, typeof(time_weighted_return), "
            "time_weighted_return FROM investment_statements"
        ).fetchone() == ("text", "123.123456789", "text", "0.123456789012")
        assert connection.execute(
            "SELECT typeof(amount), amount FROM investment_activities"
        ).fetchone() == ("text", "123.123456789")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute(
            "SELECT statement_id FROM investment_source_files"
        ).fetchone() == ("legacy-statement",)
        schema_version = int(connection.execute("PRAGMA schema_version").fetchone()[0])

    _ensure_investment_exact_decimals(db_path)

    with contextlib.closing(sqlite3.connect(db_path)) as connection, connection:
        assert int(connection.execute("PRAGMA schema_version").fetchone()[0]) == schema_version


@pytest.mark.parametrize(
    "invalid_value",
    ("not-a-decimal", "0.0000000000001", "10000000000000000"),
)
def test_migration_0016_rejects_invalid_legacy_content_transactionally(
    tmp_path,
    invalid_value,
) -> None:
    """One invalid legacy numeric leaves every original table and row untouched."""
    from ledger.storage.migrations import _ensure_investment_exact_decimals

    db_path = tmp_path / "invalid-legacy.db"
    run_migrations(db_path)
    _replace_exact_investment_tables_with_legacy_numeric_schema(db_path)
    _seed_legacy_investment_decimal_rows(db_path, activity_amount=invalid_value)
    before_contract = _investment_schema_contract(db_path)
    with contextlib.closing(sqlite3.connect(db_path)) as connection, connection:
        schema_version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
        stored_before = connection.execute("SELECT amount FROM investment_activities").fetchone()

    with pytest.raises(ValueError, match="exact fixed-point"):
        _ensure_investment_exact_decimals(db_path)

    assert _investment_schema_contract(db_path) == before_contract
    with contextlib.closing(sqlite3.connect(db_path)) as connection, connection:
        assert int(connection.execute("PRAGMA schema_version").fetchone()[0]) == schema_version
        assert (
            connection.execute("SELECT amount FROM investment_activities").fetchone()
            == stored_before
        )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE 'investment_%__exact_new'"
            ).fetchall()
            == []
        )


def test_direct_investment_provenance_migration_restores_canonical_foreign_keys(
    tmp_path,
) -> None:
    """The direct migration preserves legacy provenance while restoring both FKs."""
    db_path = tmp_path / "legacy.db"
    expected_source_row = (
        "legacy-hash",
        "/synthetic/legacy.csv",
        "structured",
        "/synthetic/original.csv",
        "/synthetic/archive.csv",
        1,
        1234,
        "archived",
        "legacy-run",
        "legacy-statement",
        "2026-07-31 00:00:00",
        "2026-07-31 00:00:01",
        "2026-07-31 00:01:00",
        "2026-07-31 00:02:00",
        "2026-07-31 00:03:00",
        "finalized",
    )
    with contextlib.closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript(
            """
            CREATE TABLE investment_statements (
                statement_id VARCHAR(128) PRIMARY KEY
            );
            INSERT INTO investment_statements VALUES ('legacy-statement');
            CREATE TABLE investment_source_files (
                file_hash VARCHAR(64) PRIMARY KEY,
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
                FOREIGN KEY(statement_id) REFERENCES investment_statements(statement_id)
            );
            CREATE TABLE investment_import_runs (
                run_id VARCHAR(128) PRIMARY KEY,
                started_at DATETIME NOT NULL,
                finalized_at DATETIME,
                status VARCHAR(30) NOT NULL,
                source_path VARCHAR(500) NOT NULL,
                statements_imported INTEGER NOT NULL DEFAULT 0,
                errors INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO investment_import_runs VALUES (
                'legacy-run', '2026-07-31 00:00:00', '2026-07-31 00:01:00',
                'finalized', '/synthetic', 1, 0
            );
            """
        )
        connection.execute(
            "INSERT INTO investment_source_files VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            expected_source_row,
        )

    _ensure_investment_provenance_and_performance(db_path)

    with contextlib.closing(sqlite3.connect(db_path)) as connection, connection:
        schema_version_after_first_upgrade = int(
            connection.execute("PRAGMA schema_version").fetchone()[0]
        )

    _ensure_investment_provenance_and_performance(db_path)

    with contextlib.closing(sqlite3.connect(db_path)) as connection, connection:
        assert int(connection.execute("PRAGMA schema_version").fetchone()[0]) == (
            schema_version_after_first_upgrade
        )
        source_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(investment_source_files)")
        }
        run_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(investment_import_runs)")
        }
        assert {
            "source_role",
            "original_path",
            "archived_path",
            "structured_import",
            "byte_size",
            "archive_status",
            "import_run_id",
            "archived_at",
            "updated_at",
        } <= source_columns
        assert {
            "updated_at",
            "staged_at",
            "finalizing_at",
            "archive_pending_at",
            "cancelled_at",
            "failed_at",
            "private_manifest_path",
            "baseline_fingerprint",
            "staging_fingerprint",
            "target_fingerprint",
            "source_count",
            "structured_source_count",
            "evidence_source_count",
            "lifecycle_inputs",
            "conflict_review",
            "conflict_authorization_fingerprint",
            "redacted_failure",
        } <= run_columns
        foreign_keys = {
            (str(row[3]), str(row[2]), str(row[4]))
            for row in connection.execute("PRAGMA foreign_key_list(investment_source_files)")
        }
        assert ("import_run_id", "investment_import_runs", "run_id") in foreign_keys
        assert ("statement_id", "investment_statements", "statement_id") in foreign_keys
        assert (
            connection.execute(
                "SELECT file_hash, source_path, source_role, original_path, archived_path, "
                "structured_import, byte_size, archive_status, import_run_id, statement_id, "
                "discovered_at, staged_at, archived_at, finalized_at, updated_at, status "
                "FROM investment_source_files"
            ).fetchone()
            == expected_source_row
        )
        assert connection.execute(
            "SELECT run_id, status, statements_imported FROM investment_import_runs"
        ).fetchone() == ("legacy-run", "finalized", 1)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='investment_performance'"
        ).fetchone() == ("investment_performance",)


def test_investment_provenance_migration_0015_repairs_previously_applied_0014(
    tmp_path,
) -> None:
    """A database already stamped at 0014 gets the missing provenance FKs via 0015."""
    db_path = tmp_path / "legacy-0014.db"
    run_migrations(db_path)

    with contextlib.closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript(
            """
            DELETE FROM schema_migrations
            WHERE version = '20260801_0015_investment_provenance_foreign_keys';
            DROP TABLE investment_source_files;
            CREATE TABLE investment_source_files (
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
                status VARCHAR(30) NOT NULL
            );
            INSERT INTO investment_import_runs (
                run_id, started_at, status, source_path, statements_imported, errors
            ) VALUES (
                'legacy-run', '2026-07-31 00:00:00', 'finalized', '/synthetic', 1, 0
            );
            INSERT INTO investment_source_files (
                file_hash, source_path, import_run_id, discovered_at, status
            ) VALUES (
                'legacy-hash', '/synthetic/legacy.csv', 'legacy-run',
                '2026-07-31 00:00:00', 'finalized'
            );
            """
        )
        assert connection.execute(
            "SELECT version FROM schema_migrations "
            "WHERE version = '20260801_0014_investment_provenance_and_performance'"
        ).fetchone() == ("20260801_0014_investment_provenance_and_performance",)
        assert (
            connection.execute("PRAGMA foreign_key_list(investment_source_files)").fetchall() == []
        )

    result = run_migrations(db_path)

    assert result.details["applied_versions"] == [
        "20260801_0015_investment_provenance_foreign_keys"
    ]
    with contextlib.closing(sqlite3.connect(db_path)) as connection, connection:
        foreign_keys = {
            (str(row[3]), str(row[2]), str(row[4]))
            for row in connection.execute("PRAGMA foreign_key_list(investment_source_files)")
        }
        assert foreign_keys == {
            ("import_run_id", "investment_import_runs", "run_id"),
            ("statement_id", "investment_statements", "statement_id"),
        }
        assert connection.execute(
            "SELECT file_hash, source_path, import_run_id, status FROM investment_source_files"
        ).fetchone() == (
            "legacy-hash",
            "/synthetic/legacy.csv",
            "legacy-run",
            "finalized",
        )


def test_investment_provenance_fk_rebuild_preserves_orphaned_legacy_row_on_failure(
    tmp_path,
) -> None:
    """An invalid run reference aborts the table swap without changing its source row."""
    db_path = tmp_path / "orphaned-provenance.db"
    with contextlib.closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript(
            """
            CREATE TABLE investment_statements (
                statement_id VARCHAR(128) PRIMARY KEY
            );
            CREATE TABLE investment_import_runs (
                run_id VARCHAR(128) PRIMARY KEY,
                started_at DATETIME NOT NULL,
                finalized_at DATETIME,
                status VARCHAR(30) NOT NULL,
                source_path VARCHAR(500) NOT NULL,
                statements_imported INTEGER NOT NULL DEFAULT 0,
                errors INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE investment_source_files (
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
                status VARCHAR(30) NOT NULL
            );
            INSERT INTO investment_source_files (
                file_hash, source_path, import_run_id, discovered_at, status
            ) VALUES (
                'orphaned-hash', '/synthetic/orphaned.csv', 'missing-run',
                '2026-07-31 00:00:00', 'finalized'
            );
            """
        )

    with pytest.raises(ValueError, match="invalid references"):
        _ensure_investment_provenance_and_performance(db_path)

    with contextlib.closing(sqlite3.connect(db_path)) as connection, connection:
        assert connection.execute(
            "SELECT file_hash, source_path, import_run_id, status FROM investment_source_files"
        ).fetchone() == (
            "orphaned-hash",
            "/synthetic/orphaned.csv",
            "missing-run",
            "finalized",
        )
        assert (
            connection.execute("PRAGMA foreign_key_list(investment_source_files)").fetchall() == []
        )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name = 'investment_source_files__new'"
            ).fetchone()
            is None
        )


def test_investment_period_unique_index_blocks_duplicate_rows_across_sessions(tmp_path) -> None:
    """The database, not a repository pre-read, owns the account-period invariant."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    run_migrations(db_path)
    now = datetime(2026, 8, 1)
    with session_scope(db_path) as session:
        session.add(
            InvestmentAccountDB(
                account_id="account-race",
                institution="broker",
                source_account_id="opaque",
                account_alias="Race",
                account_type="registered",
                base_currency="USD",
                status="active",
                transfer_destination=None,
                status_confirmed_on=now.date(),
                closed_on=None,
                first_statement_date=None,
                last_statement_date=None,
                created_at=now,
                updated_at=now,
            )
        )

    first_session = get_session(db_path)
    second_session = get_session(db_path)
    try:
        first_session.add(_race_statement("statement-first", "a" * 64, now))
        first_session.commit()
        second_session.add(_race_statement("statement-second", "b" * 64, now))
        with pytest.raises(IntegrityError):
            second_session.commit()
    finally:
        first_session.close()
        second_session.rollback()
        second_session.close()


def test_investment_period_uniqueness_migration_fails_safely_on_legacy_duplicates(tmp_path) -> None:
    """The additive migration leaves an inconsistent legacy table untouched."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    with contextlib.closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute("DROP TABLE investment_statements")
        connection.execute(
            "CREATE TABLE investment_statements ("
            "statement_id VARCHAR(128) NOT NULL PRIMARY KEY,"
            "account_id VARCHAR(128) NOT NULL,"
            "period_start DATE NOT NULL, period_end DATE NOT NULL,"
            "generated_at DATETIME NOT NULL, base_currency VARCHAR(3) NOT NULL,"
            "total_nav NUMERIC(28, 12) NOT NULL,"
            "time_weighted_return NUMERIC(28, 12),"
            "parsed_fingerprint VARCHAR(64) NOT NULL UNIQUE, imported_at DATETIME NOT NULL"
            ")"
        )
        for statement_id, fingerprint in (("first", "a" * 64), ("second", "b" * 64)):
            connection.execute(
                "INSERT INTO investment_statements VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    statement_id,
                    "opaque-account",
                    "2026-07-01",
                    "2026-07-31",
                    "2026-08-01",
                    "USD",
                    "1",
                    None,
                    fingerprint,
                    "2026-08-01",
                ),
            )

    with pytest.raises(ValueError) as raised:
        run_migrations(db_path)

    assert "opaque-account" not in str(raised.value)
    with contextlib.closing(sqlite3.connect(db_path)) as connection, connection:
        assert connection.execute("SELECT COUNT(*) FROM investment_statements").fetchone() == (2,)


def _race_statement(statement_id: str, fingerprint: str, now: datetime) -> InvestmentStatementDB:
    return InvestmentStatementDB(
        statement_id=statement_id,
        account_id="account-race",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        generated_at=now,
        base_currency="USD",
        total_nav=Decimal("1.000000000000"),
        time_weighted_return=None,
        parsed_fingerprint=fingerprint,
        imported_at=now,
    )


def seed_pre_investment_database(tmp_path):
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    with session_scope(db_path) as session:
        session.add(
            TransactionDB(
                txn_id="existing-transaction",
                posting_date=date(2026, 8, 1),
                transaction_date=None,
                institution="test",
                account="test-account",
                account_type="checking",
                flow="expense",
                cashflow_type="expense",
                include_in_cashflow=True,
                currency="CAD",
                amount_raw=Decimal("1.00"),
                amount_signed=Decimal("-1.00"),
                description="Synthetic migration row",
                merchant="",
                category="Other",
                merchant_confidence="none",
                category_confidence="low",
                exclude_from_spending=False,
                merchant_raw="",
                category_raw="",
                source_file="synthetic.csv",
                source_path="/synthetic.csv",
                source_period="2026-08",
                source_row=1,
                imported_at=datetime(2026, 8, 1, tzinfo=UTC).replace(tzinfo=None),
                parser_version="test",
                txn_fingerprint="a" * 32,
                recurring_pattern_id=None,
                day_of_week=5,
                day_of_month=1,
                week_of_year=31,
                is_weekend=True,
                is_month_start=True,
                is_month_end=False,
            )
        )
    with get_engine(db_path).begin() as connection:
        for table_name in (
            "investment_activities",
            "investment_positions",
            "investment_asset_balances",
            "investment_source_files",
            "investment_statements",
            "investment_import_runs",
            "investment_accounts",
        ):
            connection.execute(text(f"DROP TABLE {table_name}"))
    return db_path


def transaction_ids(db_path) -> list[str]:
    with session_scope(db_path) as session:
        return [row.txn_id for row in session.query(TransactionDB).order_by(TransactionDB.txn_id)]


def test_investment_migration_preserves_existing_transactions(tmp_path) -> None:
    db_path = seed_pre_investment_database(tmp_path)

    result = run_migrations(db_path)

    assert transaction_ids(db_path) == ["existing-transaction"]
    assert "20260801_0011_investment_tables" in result.details["applied_versions"]
