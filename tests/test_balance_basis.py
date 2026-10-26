"""Balance-basis persistence and interpretation regressions."""

from __future__ import annotations

import contextlib
import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from account_fixtures import seed_account
from pydantic import ValidationError

from ledger.analysis import AnalysisRunner
from ledger.analysis.balance_calculator import BalanceCalculator
from ledger.analysis.health_scorer import FinancialHealthScorer
from ledger.analysis.trend_aggregator import TrendAggregator
from ledger.core import Transaction
from ledger.core.account_identity import account_identity
from ledger.core.analysis_models import AccountBalance
from ledger.planning import BudgetService, PlanningReadinessService
from ledger.processing import ProcessingResult
from ledger.query import LedgerQueryService
from ledger.storage.database import (
    AccountBalanceDB,
    AccountDB,
    SavingsGoalDB,
    SpendingTrendDB,
    init_database,
    session_scope,
)
from ledger.storage.migrations import run_migrations
from ledger.storage.writer import DatabaseWriter
from ledger.utils.dates import naive_utc_now
from ledger.utils.hashing import stable_id

BASIS_INSTITUTION = "test-bank"
BASIS_SLUG = "test-chequing"
BASIS_ACCOUNT_ID = account_identity(BASIS_INSTITUTION, BASIS_SLUG)


def _balance(**overrides: object) -> AccountBalance:
    values = {
        "account_id": BASIS_ACCOUNT_ID,
        "balance_date": date(2026, 7, 1),
        "balance": Decimal("125.00"),
        "transaction_count": 1,
        "balance_7d_avg": Decimal("125.00"),
        "balance_30d_avg": Decimal("125.00"),
        "balance_7d_min": Decimal("125.00"),
        "balance_7d_max": Decimal("125.00"),
        "credit_utilization": None,
        "computed_at": naive_utc_now(),
    }
    values.update(overrides)
    return AccountBalance.model_validate(values)


def _balance_column(db_path: Path) -> tuple | None:
    with contextlib.closing(sqlite3.connect(db_path)) as conn, conn:
        return next(
            (
                row
                for row in conn.execute("PRAGMA table_info(account_balances)").fetchall()
                if row[1] == "balance_basis"
            ),
            None,
        )


def _seed_db_balance(
    db_path: Path,
    *,
    balance_date: date,
    amount: str,
    balance_basis: str,
    account_id: str = BASIS_ACCOUNT_ID,
) -> None:
    value = Decimal(amount)
    with session_scope(db_path) as session:
        session.add(
            AccountBalanceDB(
                account_id=account_id,
                balance_date=balance_date,
                balance=value,
                balance_basis=balance_basis,
                transaction_count=1,
                balance_7d_avg=value,
                balance_30d_avg=value,
                balance_7d_min=value,
                balance_7d_max=value,
                credit_utilization=None,
                computed_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )


def test_account_balance_model_defaults_to_relative_and_rejects_unknown_basis() -> None:
    assert _balance().balance_basis == "relative"
    assert _balance(balance_basis="absolute").balance_basis == "absolute"

    with pytest.raises(ValidationError):
        _balance(balance_basis="estimated")


def test_fresh_account_balance_schema_has_non_null_relative_default(tmp_path: Path) -> None:
    db_path = tmp_path / "fresh.db"
    init_database(db_path)

    column = _balance_column(db_path)

    assert column is not None
    assert column[3] == 1
    assert str(column[4]).strip("'\"") == "relative"
    assert column[5] > 0


def test_migration_backfills_existing_account_balances_as_relative(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    with contextlib.closing(sqlite3.connect(db_path)) as conn, conn:
        conn.executescript(
            """
            CREATE TABLE account_balances (
                account_id VARCHAR(64) NOT NULL,
                balance_date DATE NOT NULL,
                balance NUMERIC(12, 2) NOT NULL,
                transaction_count INTEGER NOT NULL,
                balance_7d_avg NUMERIC(12, 2) NOT NULL,
                balance_30d_avg NUMERIC(12, 2) NOT NULL,
                balance_7d_min NUMERIC(12, 2) NOT NULL,
                balance_7d_max NUMERIC(12, 2) NOT NULL,
                credit_utilization NUMERIC(5, 2),
                computed_at DATETIME NOT NULL,
                PRIMARY KEY (account_id, balance_date)
            );
            """
        )
    init_database(db_path)
    legacy_account_id = seed_account(db_path, BASIS_INSTITUTION, "legacy-chequing")
    with contextlib.closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO account_balances VALUES ("
            "?, '2026-06-30', 42.00, 1, 42.00, 42.00, 42.00, 42.00, NULL, '2026-07-01 00:00:00')",
            (legacy_account_id,),
        )

    result = run_migrations(db_path)

    assert "20260709_0010_account_balance_basis" in result.details["applied_versions"]
    column = _balance_column(db_path)
    assert column is not None
    assert column[3] == 1
    assert str(column[4]).strip("'\"") == "relative"
    assert column[5] > 0
    with contextlib.closing(sqlite3.connect(db_path)) as conn, conn:
        basis = conn.execute(
            "SELECT balance_basis FROM account_balances WHERE account_id = ?",
            (legacy_account_id,),
        ).fetchone()[0]
    assert basis == "relative"


def test_balance_basis_migration_rolls_back_failed_table_rebuild(tmp_path: Path) -> None:
    db_path = tmp_path / "corrupt-legacy.db"
    with contextlib.closing(sqlite3.connect(db_path)) as conn, conn:
        conn.executescript(
            """
            CREATE TABLE account_balances (
                account_id VARCHAR(64) NOT NULL,
                balance_date DATE NOT NULL,
                balance NUMERIC(12, 2),
                transaction_count INTEGER NOT NULL,
                balance_7d_avg NUMERIC(12, 2) NOT NULL,
                balance_30d_avg NUMERIC(12, 2) NOT NULL,
                balance_7d_min NUMERIC(12, 2) NOT NULL,
                balance_7d_max NUMERIC(12, 2) NOT NULL,
                credit_utilization NUMERIC(5, 2),
                computed_at DATETIME NOT NULL,
                PRIMARY KEY (account_id, balance_date)
            );
            INSERT INTO account_balances VALUES (
                'corrupt-account', '2026-06-30', NULL, 1,
                0.00, 0.00, 0.00, 0.00, NULL, '2026-07-01 00:00:00'
            );
            """
        )

    with pytest.raises(sqlite3.IntegrityError):
        run_migrations(db_path)

    with contextlib.closing(sqlite3.connect(db_path)) as conn, conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        columns = {row[1] for row in conn.execute("PRAGMA table_info(account_balances)").fetchall()}
        row = conn.execute("SELECT account_id, balance FROM account_balances").fetchone()
    assert "account_balances" in tables
    assert "account_balances__new" not in tables
    assert "balance_basis" not in columns
    assert row == ("corrupt-account", None)


def test_balance_reconstruction_preserves_absolute_basis(tmp_path: Path) -> None:
    calculator = BalanceCalculator(tmp_path / "reconstruction.db")

    rebuilt = calculator.compute_rolling_stats([_balance(balance_basis="absolute")])

    assert rebuilt[0].balance_basis == "absolute"


def test_balance_save_and_queries_round_trip_an_explicit_basis(tmp_path: Path) -> None:
    db_path = tmp_path / "round-trip.db"
    init_database(db_path)
    seed_account(db_path, BASIS_INSTITUTION, BASIS_SLUG)
    calculator = BalanceCalculator(db_path)
    calculator.save_balances(
        [
            _balance(
                balance_date=date(2026, 6, 30),
                balance=Decimal("500.00"),
                balance_basis="absolute",
            )
        ],
        replacement_account_ids={BASIS_ACCOUNT_ID},
        replacement_basis="absolute",
    )
    calculator.save_balances(
        [
            _balance(
                balance_date=date(2026, 7, 1),
                balance=Decimal("25.00"),
                balance_basis="relative",
            )
        ],
        replacement_account_ids={BASIS_ACCOUNT_ID},
        replacement_basis="relative",
    )
    service = LedgerQueryService(db_path)

    absolute_rows = service.get_account_balances(
        BASIS_ACCOUNT_ID,
        balance_basis="absolute",
    )
    latest_absolute = service.get_latest_account_balance(
        BASIS_ACCOUNT_ID,
        balance_basis="absolute",
    )

    assert [row["balance_basis"] for row in absolute_rows] == ["absolute"]
    assert latest_absolute is not None
    assert latest_absolute["balance_date"] == date(2026, 6, 30)
    assert latest_absolute["balance"] == Decimal("500.00")
    assert latest_absolute["balance_basis"] == "absolute"


def test_same_day_relative_and_absolute_balances_coexist(tmp_path: Path) -> None:
    db_path = tmp_path / "same-day-bases.db"
    init_database(db_path)
    seed_account(db_path, BASIS_INSTITUTION, BASIS_SLUG)
    calculator = BalanceCalculator(db_path)
    calculator.save_balances(
        [_balance(balance=Decimal("500.00"), balance_basis="absolute")],
        replacement_account_ids={BASIS_ACCOUNT_ID},
        replacement_basis="absolute",
    )
    calculator.save_balances(
        [_balance(balance=Decimal("25.00"), balance_basis="relative")],
        replacement_account_ids={BASIS_ACCOUNT_ID},
        replacement_basis="relative",
    )

    rows = LedgerQueryService(db_path).get_account_balances(BASIS_ACCOUNT_ID)

    assert {(row["balance_basis"], row["balance"]) for row in rows} == {
        ("absolute", Decimal("500.00")),
        ("relative", Decimal("25.00")),
    }
    with pytest.raises(TypeError):
        LedgerQueryService(db_path).get_latest_account_balance(BASIS_ACCOUNT_ID)


def test_relative_only_goal_balance_is_unassessable_not_zero(tmp_path: Path) -> None:
    db_path = tmp_path / "relative-goal.db"
    init_database(db_path)
    seed_account(db_path, BASIS_INSTITUTION, BASIS_SLUG)
    _seed_db_balance(
        db_path,
        balance_date=date(2026, 7, 1),
        amount="125.00",
        balance_basis="relative",
    )
    service = BudgetService(db_path)
    result = service.replace_goals(
        [
            {
                "name": "Emergency fund",
                "account_id": BASIS_ACCOUNT_ID,
                "target_amount": "1000.00",
                "status": "active",
            }
        ]
    )

    goal = service.list_goals()[0]
    account = service.list_goal_accounts()[0]

    assert result.ok
    assert goal["current_amount"] is None
    assert goal["current_balance_date"] is None
    assert account["current_amount"] is None
    assert account["current_balance_date"] is None


def test_goal_balance_prefers_older_absolute_row_over_newer_relative_row(tmp_path: Path) -> None:
    db_path = tmp_path / "mixed-goal.db"
    init_database(db_path)
    seed_account(db_path, BASIS_INSTITUTION, BASIS_SLUG)
    _seed_db_balance(
        db_path,
        balance_date=date(2026, 6, 30),
        amount="500.00",
        balance_basis="absolute",
    )
    _seed_db_balance(
        db_path,
        balance_date=date(2026, 7, 1),
        amount="25.00",
        balance_basis="relative",
    )
    service = BudgetService(db_path)
    service.replace_goals(
        [
            {
                "name": "Emergency fund",
                "account_id": BASIS_ACCOUNT_ID,
                "target_amount": "1000.00",
                "status": "active",
            }
        ]
    )

    goal = service.list_goals()[0]
    account = service.list_goal_accounts()[0]

    assert goal["current_amount"] == Decimal("500.00")
    assert goal["current_balance_date"] == date(2026, 6, 30)
    assert account["current_amount"] == Decimal("500.00")
    assert account["current_balance_date"] == date(2026, 6, 30)


def test_health_metrics_select_only_explicit_absolute_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "health-basis.db"
    init_database(db_path)
    scorer = FinancialHealthScorer(db_path)
    accounts = [{"account_id": "acct-basis", "is_credit_card": False}]
    balances = [
        {
            "account_id": "acct-basis",
            "balance_date": date(2026, 6, 30),
            "balance": Decimal("500.00"),
            "balance_basis": "absolute",
        },
        {
            "account_id": "acct-basis",
            "balance_date": date(2026, 7, 1),
            "balance": Decimal("-999.00"),
            "balance_basis": "relative",
        },
    ]

    assert scorer._has_absolute_balance_anchor(accounts, balances)
    assert scorer._compute_emergency_fund_months(
        {"expenses": Decimal("100.00")}, accounts, balances
    ) == Decimal("5.00")
    assert scorer._compute_debt_ratio(accounts, balances) == Decimal("0.00")


def test_unrelated_absolute_balance_does_not_open_health_metric_gate(tmp_path: Path) -> None:
    db_path = tmp_path / "unrelated-health-basis.db"
    init_database(db_path)
    scorer = FinancialHealthScorer(db_path)
    accounts = [{"account_id": "in-scope", "is_credit_card": False}]
    unrelated = [
        {
            "account_id": "not-in-scope",
            "balance_date": date(2026, 7, 1),
            "balance": Decimal("5000.00"),
            "balance_basis": "absolute",
        }
    ]

    assert not scorer._has_absolute_balance_anchor(accounts, unrelated)
    assert scorer._compute_emergency_fund_months(
        {"expenses": Decimal("100.00")}, accounts, unrelated
    ) == Decimal("0.00")


def test_health_metrics_require_absolute_balance_coverage_for_every_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "partial-health-basis.db"
    init_database(db_path)
    scorer = FinancialHealthScorer(db_path)
    target_date = date(2026, 7, 1)
    transactions = [
        SimpleNamespace(
            posting_date=target_date - timedelta(days=offset),
            include_in_cashflow=True,
            currency="CAD",
            cashflow_type="expense",
            exclude_from_spending=False,
            amount_signed=Decimal("-10.00"),
        )
        for offset in range(30)
    ]
    monkeypatch.setattr(
        scorer.query_service,
        "get_transactions",
        lambda **_kwargs: transactions,
    )
    accounts = [
        {"account_id": "cash", "is_credit_card": False},
        {"account_id": "credit-card", "is_credit_card": True},
    ]
    balances = [
        {
            "account_id": "cash",
            "balance_date": target_date,
            "balance": Decimal("5000.00"),
            "balance_basis": "absolute",
        },
        {
            "account_id": "credit-card",
            "balance_date": target_date,
            "balance": Decimal("-250.00"),
            "balance_basis": "relative",
        },
    ]

    score = scorer.compute_daily_score(
        target_date,
        _preloaded_accounts=accounts,
        _preloaded_balances=balances,
    )

    assert score is not None
    assert score.emergency_fund_months is None
    assert score.debt_ratio is None


def test_planning_readiness_does_not_treat_relative_goal_as_zero_progress(tmp_path: Path) -> None:
    db_path = tmp_path / "relative-readiness.db"
    init_database(db_path)
    seed_account(db_path, BASIS_INSTITUTION, BASIS_SLUG)
    _seed_db_balance(
        db_path,
        balance_date=date(2026, 7, 1),
        amount="125.00",
        balance_basis="relative",
    )
    BudgetService(db_path).replace_goals(
        [
            {
                "name": "Emergency fund",
                "account_id": BASIS_ACCOUNT_ID,
                "target_amount": "1000.00",
                "status": "active",
            }
        ]
    )

    summary = PlanningReadinessService(db_path).summary(
        date(2026, 7, 1),
        date(2026, 7, 31),
    )

    assert summary["goals"]["target"] == Decimal("1000.00")
    assert summary["goals"]["current"] is None
    assert summary["goals"]["remaining"] is None


def test_empty_scoped_balance_save_deletes_only_target_account(tmp_path: Path) -> None:
    db_path = tmp_path / "balance-replacement.db"
    init_database(db_path)
    replaced_account_id = seed_account(db_path, BASIS_INSTITUTION, "replace-me-chequing")
    kept_account_id = seed_account(db_path, BASIS_INSTITUTION, "keep-me-chequing")
    _seed_db_balance(
        db_path,
        account_id=replaced_account_id,
        balance_date=date(2026, 7, 1),
        amount="100.00",
        balance_basis="absolute",
    )
    _seed_db_balance(
        db_path,
        account_id=kept_account_id,
        balance_date=date(2026, 7, 1),
        amount="200.00",
        balance_basis="absolute",
    )

    saved = BalanceCalculator(db_path).save_balances(
        [],
        replacement_account_ids={replaced_account_id},
        replacement_basis="absolute",
    )

    assert saved == 0
    with session_scope(db_path) as session:
        assert (
            session.query(AccountBalanceDB).filter_by(account_id=replaced_account_id).count() == 0
        )
        assert session.query(AccountBalanceDB).filter_by(account_id=kept_account_id).count() == 1


def test_empty_scoped_trend_save_deletes_only_target_period(tmp_path: Path) -> None:
    db_path = tmp_path / "trend-replacement.db"
    init_database(db_path)
    now = datetime.now(UTC).replace(tzinfo=None)
    with session_scope(db_path) as session:
        for time_period in ("2026-07-01", "2026-07-02"):
            session.add(
                SpendingTrendDB(
                    time_period=time_period,
                    period_type="daily",
                    category="Dining",
                    transaction_count=1,
                    total_spent=Decimal("10.00"),
                    avg_transaction=Decimal("10.00"),
                    median_transaction=Decimal("10.00"),
                    vs_previous_period_pct=Decimal("0.00"),
                    vs_previous_period_abs=Decimal("0.00"),
                    vs_avg_6m_pct=Decimal("0.00"),
                    top_merchant="CAFE",
                    top_merchant_amount=Decimal("10.00"),
                    trend_direction="stable",
                    computed_at=now,
                )
            )

    saved = TrendAggregator(db_path).save_trends(
        [],
        replacement_scope=("daily", "2026-07-01"),
    )

    assert saved == 0
    with session_scope(db_path) as session:
        assert (
            session.query(SpendingTrendDB)
            .filter_by(period_type="daily", time_period="2026-07-01")
            .count()
            == 0
        )
        assert (
            session.query(SpendingTrendDB)
            .filter_by(period_type="daily", time_period="2026-07-02")
            .count()
            == 1
        )


def test_empty_recent_period_prunes_stale_trend(tmp_path: Path) -> None:
    db_path = tmp_path / "empty-aggregate.db"
    init_database(db_path)
    target = date(2026, 7, 1)
    now = datetime.now(UTC).replace(tzinfo=None)
    with session_scope(db_path) as session:
        session.add(
            SpendingTrendDB(
                time_period=target.isoformat(),
                period_type="daily",
                category="Dining",
                transaction_count=1,
                total_spent=Decimal("10.00"),
                avg_transaction=Decimal("10.00"),
                median_transaction=Decimal("10.00"),
                vs_previous_period_pct=Decimal("0.00"),
                vs_previous_period_abs=Decimal("0.00"),
                vs_avg_6m_pct=Decimal("0.00"),
                top_merchant="CAFE",
                top_merchant_amount=Decimal("10.00"),
                trend_direction="stable",
                computed_at=now,
            )
        )

    assert TrendAggregator(db_path).aggregate_recent_days(1, today=target) == 0
    with session_scope(db_path) as session:
        assert session.query(SpendingTrendDB).count() == 0


def _new_account_transaction() -> Transaction:
    posted = date(2026, 7, 1)
    return Transaction(
        txn_id="replacement-new-account",
        txn_fingerprint="replacement-new-account-fp",
        posting_date=posted,
        transaction_date=posted,
        institution="new-bank",
        account="new-chequing",
        account_type="checking",
        flow="expense",
        cashflow_type="expense",
        include_in_cashflow=True,
        currency="CAD",
        amount_raw=Decimal("25.00"),
        amount_signed=Decimal("-25.00"),
        description="SYNTHETIC GROCERY",
        merchant="GROCERY",
        category="Groceries",
        source_file="synthetic.csv",
        source_path="/synthetic/synthetic.csv",
        source_period="2026-07",
        source_row=1,
        imported_at=datetime.now(UTC).replace(tzinfo=None),
        parser_version="test",
    )


def test_full_account_refresh_prunes_stale_derived_rows_but_preserves_goal(tmp_path: Path) -> None:
    db_path = tmp_path / "account-replacement.db"
    init_database(db_path)
    stale_account_id = seed_account(db_path, BASIS_INSTITUTION, "stale-chequing")
    dropped_account_id = seed_account(db_path, BASIS_INSTITUTION, "dropped-chequing")
    _seed_db_balance(
        db_path,
        account_id=stale_account_id,
        balance_date=date(2026, 6, 30),
        amount="999.00",
        balance_basis="absolute",
    )
    _seed_db_balance(
        db_path,
        account_id=dropped_account_id,
        balance_date=date(2026, 6, 30),
        amount="111.00",
        balance_basis="relative",
    )
    BudgetService(db_path).replace_goals(
        [
            {
                "name": "Preserve me",
                "account_id": stale_account_id,
                "target_amount": "1000.00",
                "status": "active",
            }
        ]
    )
    DatabaseWriter(db_path).write_result(
        ProcessingResult(transactions=[_new_account_transaction()], total_scanned=1),
        Path("/synthetic"),
        "test",
    )

    result = AnalysisRunner(db_path).run_account_analysis()
    new_account_id = stable_id("new-bank", "new-chequing")

    assert result["accounts_detected"] == 1
    with session_scope(db_path) as session:
        assert session.get(AccountDB, stale_account_id) is None
        assert session.query(AccountBalanceDB).filter_by(account_id=stale_account_id).count() == 0
        assert session.query(AccountBalanceDB).filter_by(account_id=dropped_account_id).count() == 0
        assert session.get(AccountDB, new_account_id) is not None
        new_balances = session.query(AccountBalanceDB).filter_by(account_id=new_account_id).all()
        assert new_balances
        assert {row.balance_basis for row in new_balances} == {"relative"}
        goals = session.query(SavingsGoalDB).all()
        assert len(goals) == 1
        assert goals[0].account_id == stale_account_id


def test_retained_account_absolute_anchor_survives_relative_refresh(tmp_path: Path) -> None:
    db_path = tmp_path / "retained-anchor.db"
    init_database(db_path)
    DatabaseWriter(db_path).write_result(
        ProcessingResult(transactions=[_new_account_transaction()], total_scanned=1),
        Path("/synthetic"),
        "test",
    )
    runner = AnalysisRunner(db_path)
    runner.run_account_analysis()
    account_id = stable_id("new-bank", "new-chequing")
    _seed_db_balance(
        db_path,
        account_id=account_id,
        balance_date=date(2026, 6, 30),
        amount="500.00",
        balance_basis="absolute",
    )

    runner.run_account_analysis()

    with session_scope(db_path) as session:
        absolute = (
            session.query(AccountBalanceDB)
            .filter_by(account_id=account_id, balance_basis="absolute")
            .one()
        )
        relative = (
            session.query(AccountBalanceDB)
            .filter_by(account_id=account_id, balance_basis="relative")
            .all()
        )
        assert absolute.balance == Decimal("500.00")
        assert relative
