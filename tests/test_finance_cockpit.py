"""Tests for production-grade local finance cockpit services."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from account_fixtures import seed_account

from ledger.insights import InsightEngine
from ledger.insights.service import GeneratedFinding
from ledger.planning import BudgetService, PlanningReadinessService
from ledger.quality import DataQualityService
from ledger.reporting import ReportSnapshotService
from ledger.storage.database import (
    AccountBalanceDB,
    AccountDB,
    BudgetDB,
    FinancialHealthScoreDB,
    InsightFindingDB,
    RecurringPatternDB,
    SavingsGoalDB,
    TransactionDB,
    TransferDB,
    init_database,
    session_scope,
)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _temporal_fields(posting_date: date) -> dict:
    return {
        "day_of_week": posting_date.weekday(),
        "day_of_month": posting_date.day,
        "week_of_year": posting_date.isocalendar()[1],
        "is_weekend": posting_date.weekday() >= 5,
        "is_month_start": posting_date.day <= 3,
        "is_month_end": posting_date.day >= 28,
    }


def _insert_txn(
    db_path: Path,
    txn_id: str,
    posted: date,
    amount: str,
    merchant: str,
    category: str,
    *,
    account_type: str = "visa",
) -> None:
    with session_scope(db_path) as session:
        session.add(
            TransactionDB(
                txn_id=txn_id,
                posting_date=posted,
                transaction_date=posted,
                institution="test-bank",
                account="4111111111111111",
                account_type=account_type,
                flow="expense" if Decimal(amount) < 0 else "income",
                cashflow_type="expense" if Decimal(amount) < 0 else "income",
                include_in_cashflow=True,
                currency="CAD",
                amount_raw=Decimal(amount).copy_abs(),
                amount_signed=Decimal(amount),
                description=merchant,
                merchant=merchant,
                category=category,
                source_file="statement.csv",
                source_path="/private/statement.csv",
                source_period=posted.strftime("%Y-%m"),
                source_row=1,
                imported_at=_now(),
                parser_version="test",
                txn_fingerprint=f"{txn_id}-fingerprint",
                **_temporal_fields(posted),
            )
        )


def test_insight_engine_persists_period_findings(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_txn(db_path, "prev-food", date(2026, 4, 10), "-20.00", "MARKET", "Groceries")
    _insert_txn(db_path, "cur-food", date(2026, 5, 10), "-600.00", "MARKET", "Groceries")
    _insert_txn(db_path, "cur-card", date(2026, 5, 11), "-300.00", "APPLE", "Software & Digital")
    # Income belongs on a deposit account, not the card -- otherwise the card's period
    # net goes positive and (correctly) no longer reads as material card spend.
    _insert_txn(
        db_path,
        "cur-income",
        date(2026, 5, 12),
        "1000.00",
        "PAYROLL",
        "Income",
        account_type="bank",
    )

    snapshot = ReportSnapshotService(db_path).snapshot_for_period(
        date(2026, 5, 1),
        date(2026, 5, 31),
        source="test",
    )
    result = InsightEngine(db_path).refresh_period(date(2026, 5, 1), date(2026, 5, 31))

    assert snapshot["payload"]["cost_cutting_candidates"]
    assert result.ok
    with session_scope(db_path) as session:
        rows = session.query(InsightFindingDB).all()
        finding_types = {row.finding_type for row in rows}
    assert {"cost_cutting", "category_drift", "credit_cards"} <= finding_types


def test_budget_variance_calculates_selected_period(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_txn(db_path, "groceries", date(2026, 5, 5), "-125.00", "MARKET", "Groceries")

    service = BudgetService(db_path)
    result = service.replace_budgets(
        [{"month": "*", "category": "Groceries", "amount": "100.00", "notes": ""}]
    )
    rows = service.budget_variance(date(2026, 5, 1), date(2026, 5, 31))

    assert result.ok
    groceries = next(row for row in rows if row["category"] == "Groceries")
    # Updated from float to Decimal (was: 125.0, 100.0) after P3-B fix
    assert groceries["actual"] == Decimal("125.00")
    assert groceries["budget"] == Decimal("100.00")
    assert groceries["status"] == "over"


def test_budget_variance_returns_decimal_not_float(tmp_path: Path) -> None:
    """budget/actual/variance/pct_used must be Decimal — no float drift."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    # Use an amount that would drift as float (1/3 * 3 != 1 in IEEE754)
    _insert_txn(db_path, "t1", date(2026, 5, 5), "-33.33", "SHOP", "Clothing")
    _insert_txn(db_path, "t2", date(2026, 5, 6), "-33.33", "SHOP", "Clothing")
    _insert_txn(db_path, "t3", date(2026, 5, 7), "-33.34", "SHOP", "Clothing")

    service = BudgetService(db_path)
    service.replace_budgets(
        [{"month": "*", "category": "Clothing", "amount": "100.00", "notes": ""}]
    )
    rows = service.budget_variance(date(2026, 5, 1), date(2026, 5, 31))
    row = next(r for r in rows if r["category"] == "Clothing")

    # All money fields must be Decimal, not float
    assert isinstance(row["budget"], Decimal), f"budget is {type(row['budget'])}, expected Decimal"
    assert isinstance(row["actual"], Decimal), f"actual is {type(row['actual'])}, expected Decimal"
    assert isinstance(row["variance"], Decimal), (
        f"variance is {type(row['variance'])}, expected Decimal"
    )
    assert isinstance(row["pct_used"], Decimal), (
        f"pct_used is {type(row['pct_used'])}, expected Decimal"
    )

    # Exact arithmetic: 33.33+33.33+33.34 = 100.00 exactly in Decimal
    assert row["actual"] == Decimal("100.00")
    assert row["budget"] == Decimal("100.00")
    assert row["variance"] == Decimal("0.00")  # budget - actual
    assert row["status"] == "ok"

    # Variance sign: positive means under budget, negative means over
    service.replace_budgets(
        [{"month": "*", "category": "Clothing", "amount": "90.00", "notes": ""}]
    )
    rows2 = service.budget_variance(date(2026, 5, 1), date(2026, 5, 31))
    row2 = next(r for r in rows2 if r["category"] == "Clothing")
    assert row2["variance"] < 0  # over budget → negative variance
    assert row2["status"] == "over"


def test_budget_variance_zero_budget_guard(tmp_path: Path) -> None:
    """A category with budget == 0 must return pct_used=None (no ZeroDivisionError)."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_txn(db_path, "u1", date(2026, 5, 5), "-50.00", "RANDOM", "Other")
    # No budget row → unbudgeted, budget=0
    service = BudgetService(db_path)
    rows = service.budget_variance(date(2026, 5, 1), date(2026, 5, 31))
    row = next(r for r in rows if r["category"] == "Other")

    assert row["budget"] == Decimal("0")
    assert row["pct_used"] is None  # no ZeroDivisionError; not 0% or inf
    assert row["status"] == "unbudgeted"


def test_list_budgets_returns_decimal(tmp_path: Path) -> None:
    """list_budgets amount must be Decimal, not float."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    service = BudgetService(db_path)
    service.replace_budgets(
        [{"month": "2026-05", "category": "Groceries", "amount": "200.00", "notes": ""}]
    )
    budgets = service.list_budgets()
    assert budgets, "Expected at least one budget row"
    assert isinstance(budgets[0]["amount"], Decimal), (
        f"amount is {type(budgets[0]['amount'])}, expected Decimal"
    )


def test_budget_export_csv_refuses_unsafe_semantic_cells(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    service = BudgetService(db_path)
    result = service.replace_budgets(
        [
            {
                "month": "*",
                "category": "=CATEGORY",
                "amount": "+50.00",
                "notes": "\nnotes",
            }
        ]
    )
    assert result.ok
    path = tmp_path / "budgets.csv"

    export = service.export_csv(path)

    assert not export.ok
    assert not path.exists()


def _make_account_with_balances(
    db_path: Path,
    institution: str,
    number: str,
    balances: list[tuple[date, Decimal]],
) -> str:
    """Helper to insert an AccountDB and its balance rows.

    The account row is committed first (account_balances.account_id references it),
    and its derived id is returned so callers link goals to the same account.
    """
    now = _now()
    account_id = seed_account(
        db_path,
        institution,
        number,
        account_type="Savings",
        first_transaction_date=balances[0][0],
        last_transaction_date=balances[-1][0],
    )
    with session_scope(db_path) as session:
        for bal_date, bal_amount in balances:
            session.add(
                AccountBalanceDB(
                    account_id=account_id,
                    balance_date=bal_date,
                    balance=bal_amount,
                    balance_basis="absolute",
                    transaction_count=1,
                    balance_7d_avg=bal_amount,
                    balance_30d_avg=bal_amount,
                    balance_7d_min=bal_amount,
                    balance_7d_max=bal_amount,
                    credit_utilization=None,
                    computed_at=now,
                )
            )
    return account_id


def test_goals_use_latest_linked_account_balance(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    account_id = _make_account_with_balances(
        db_path,
        "test-bank",
        "savings-1234",
        [(date(2026, 5, 1), Decimal("900.00")), (date(2026, 5, 10), Decimal("1250.00"))],
    )

    service = BudgetService(db_path)
    result = service.replace_goals(
        [
            {
                "name": "Restore savings",
                "account_id": account_id,
                "target_amount": "5000.00",
                "current_amount": "1.00",
                "target_date": "2026-10-01",
                "status": "active",
                "notes": "",
            }
        ]
    )

    goals = service.list_goals()

    assert result.ok
    # Updated from float to Decimal (was: 5000.0, 1250.0) after P3-B fix
    assert goals == [
        {
            "id": goals[0]["id"],
            "name": "Restore savings",
            "account_id": account_id,
            "account": "test-bank - Savings (...1234)",
            "target_amount": Decimal("5000.00"),
            "current_amount": Decimal("1250.00"),
            "current_balance_date": date(2026, 5, 10),
            "target_date": date(2026, 10, 1),
            "status": "active",
            "notes": "",
        }
    ]


def test_list_goals_amounts_are_decimal(tmp_path: Path) -> None:
    """list_goals target_amount/current_amount must be Decimal for linked goals."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    account_id = _make_account_with_balances(
        db_path,
        "mybank",
        "acct-0001",
        [(date(2026, 5, 1), Decimal("3000.00"))],
    )
    service = BudgetService(db_path)
    service.replace_goals(
        [
            {
                "name": "Car fund",
                "account_id": account_id,
                "target_amount": "10000.00",
                "target_date": None,
                "status": "active",
                "notes": "",
            }
        ]
    )
    goals = service.list_goals()
    assert len(goals) == 1
    g = goals[0]
    assert isinstance(g["target_amount"], Decimal), (
        f"target_amount is {type(g['target_amount'])}, expected Decimal"
    )
    assert isinstance(g["current_amount"], Decimal), (
        f"current_amount is {type(g['current_amount'])}, expected Decimal"
    )
    assert g["target_amount"] == Decimal("10000.00")
    assert g["current_amount"] == Decimal("3000.00")

    # Progress % should not produce float drift: 3000/10000*100 = 30.0 exactly
    target = g["target_amount"]
    current = g["current_amount"]
    pct = current / target * Decimal("100")
    assert pct == Decimal("30.0")


def _insert_null_account_goal(
    db_path: Path, name: str, target_amount: str, status: str = "active"
) -> None:
    """Insert a savings_goal with NULL account_id, simulating a legacy pre-migration row.

    In production, rows with NULL account_id exist in databases that had goals before
    migration 0004 added the account_id column (nullable, no NOT NULL).  Here we
    replicate that by dropping and recreating savings_goals without the NOT NULL
    constraint on account_id before inserting, then restoring the index.
    """
    now = _now().isoformat()
    with closing(sqlite3.connect(db_path)) as conn, conn:
        # Recreate the table with account_id nullable (mirrors the ALTER TABLE ADD COLUMN path)
        conn.execute("DROP TABLE IF EXISTS savings_goals_bak")
        conn.execute("ALTER TABLE savings_goals RENAME TO savings_goals_bak")
        conn.execute(
            "CREATE TABLE savings_goals ("
            "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "  name VARCHAR(160) NOT NULL,"
            "  account_id VARCHAR(64),"  # nullable — migration 0004 path
            "  target_amount NUMERIC(12,2) NOT NULL,"
            "  target_date DATE,"
            "  status VARCHAR(30) NOT NULL DEFAULT 'active',"
            "  notes TEXT NOT NULL DEFAULT '',"
            "  created_at DATETIME NOT NULL,"
            "  updated_at DATETIME NOT NULL"
            ")"
        )
        conn.execute(
            "INSERT INTO savings_goals SELECT id,name,account_id,target_amount,target_date,"
            "status,notes,created_at,updated_at FROM savings_goals_bak"
        )
        conn.execute("DROP TABLE savings_goals_bak")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_savings_goal_account_status "
            "ON savings_goals(account_id, status)"
        )
        # Now insert the NULL-account row
        conn.execute(
            "INSERT INTO savings_goals (name, account_id, target_amount, status, notes, created_at, updated_at)"
            " VALUES (?, NULL, ?, ?, '', ?, ?)",
            (name, target_amount, status, now, now),
        )


def test_null_account_id_goal_returns_none_not_zero(tmp_path: Path) -> None:
    """A goal row with account_id NULL must return current_amount=None (not 0.0)
    and account='Not linked' (not '')."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    # Insert goal with NULL account_id via raw SQL (legacy migration path — ORM enforces NOT NULL)
    _insert_null_account_goal(db_path, "Legacy Goal", "5000.00")

    service = BudgetService(db_path)
    goals = service.list_goals()

    assert len(goals) == 1
    g = goals[0]
    assert g["account_id"] is None, f"account_id should be None, got {g['account_id']!r}"
    assert g["account"] == "Not linked", (
        f"account should be 'Not linked' for unlinked goal, got {g['account']!r}"
    )
    assert g["current_amount"] is None, (
        f"current_amount should be None for unlinked goal, got {g['current_amount']!r}"
    )
    assert g["current_balance_date"] is None


def test_list_goal_accounts_current_amount_is_decimal(tmp_path: Path) -> None:
    """list_goal_accounts must return Decimal (not float) for current_amount."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    account_id = _make_account_with_balances(
        db_path,
        "mybank",
        "ga-0001",
        [(date(2026, 5, 1), Decimal("7500.00"))],
    )
    service = BudgetService(db_path)
    accounts = service.list_goal_accounts()

    assert accounts, "Expected at least one goal account row"
    row = next(r for r in accounts if str(r["account_id"]) == account_id)
    assert row["current_amount"] is not None, (
        "current_amount should not be None for account with balance"
    )
    assert isinstance(row["current_amount"], Decimal), (
        f"current_amount is {type(row['current_amount'])}, expected Decimal"
    )
    assert row["current_amount"] == Decimal("7500.00")


def test_list_goal_accounts_no_balance_returns_none(tmp_path: Path) -> None:
    """list_goal_accounts must return None (not 0 or float) for an account with no balance rows."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    # Insert account but no AccountBalanceDB rows
    now = _now()
    with session_scope(db_path) as session:
        session.add(
            AccountDB(
                account_id="acct-nobal",
                institution="nobal-bank",
                account_number="nobal-0001",
                account_type="bank",
                account_subtype="Savings",
                first_transaction_date=date(2026, 5, 1),
                last_transaction_date=date(2026, 5, 1),
                is_active=True,
                transaction_count=0,
                avg_monthly_transactions=Decimal("0.00"),
                is_credit_card=False,
                detected_credit_limit=None,
                created_at=now,
                updated_at=now,
            )
        )
    service = BudgetService(db_path)
    accounts = service.list_goal_accounts()
    row = next(r for r in accounts if str(r["account_id"]) == "acct-nobal")
    assert row["current_amount"] is None, (
        f"current_amount should be None when no balance exists, got {row['current_amount']!r}"
    )


def test_all_unlinked_goals_sum_is_decimal_zero(tmp_path: Path) -> None:
    """When all active goals are unlinked (current_amount=None), current sum must be Decimal('0')."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_null_account_goal(db_path, "Unlinked A", "1000.00")
    _insert_null_account_goal(db_path, "Unlinked B", "2000.00")

    service = BudgetService(db_path)
    goals = service.list_goals()
    active_goals = [g for g in goals if g["status"] == "active"]

    goal_current = sum(
        (g["current_amount"] for g in active_goals if g["current_amount"] is not None),
        Decimal("0"),
    )
    assert isinstance(goal_current, Decimal), (
        f"sum should be Decimal when all goals are unlinked, got {type(goal_current)}"
    )
    assert goal_current == Decimal("0")


def test_readiness_handles_null_account_id_goals(tmp_path: Path) -> None:
    """PlanningReadinessService must not crash on or zero-count NULL-account goals."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)

    # One linked goal with real balance
    account_id = _make_account_with_balances(
        db_path,
        "testbank",
        "1111",
        [(date(2026, 5, 1), Decimal("2000.00"))],
    )
    now = _now()
    with session_scope(db_path) as session:
        # Linked goal (uses ORM — has a valid account_id)
        session.add(
            SavingsGoalDB(
                name="Linked Goal",
                account_id=account_id,
                target_amount=Decimal("4000.00"),
                target_date=date(2026, 12, 31),
                status="active",
                notes="",
                created_at=now,
                updated_at=now,
            )
        )
    # Unlinked (NULL) goal — legacy row, must insert via raw SQL
    _insert_null_account_goal(db_path, "Unlinked Goal", "1000.00")

    # Must not raise; must not include unlinked goal's "current" as $0 in sums
    summary = PlanningReadinessService(db_path).summary(
        date(2026, 5, 1),
        date(2026, 5, 31),
        pending_reviews=0,
    )

    goals_summary = summary["goals"]
    assert goals_summary["active"] == 2  # both goals count as active
    # target includes both goals: 4000 + 1000 = 5000
    assert goals_summary["target"] == 5000.0
    # One unlinked goal makes aggregate progress unassessable; treating its unknown
    # current value as zero would understate progress and overstate remaining need.
    assert goals_summary["current"] is None
    assert goals_summary["remaining"] is None


def test_planning_readiness_summary_is_derived_without_writing_plan_rows(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    now = _now()
    _insert_txn(db_path, "income", date(2026, 5, 1), "2500.00", "PAYROLL", "Income")
    _insert_txn(db_path, "expense", date(2026, 5, 2), "-500.00", "RENT", "Housing")
    # The balance and recurring rows below reference this account, so it must exist first.
    account_id = seed_account(
        db_path,
        "test-bank",
        "chequing-1000",
        account_type="Chequing",
        first_transaction_date=date(2026, 5, 1),
        last_transaction_date=date(2026, 5, 2),
    )
    with session_scope(db_path) as session:
        session.add(
            AccountBalanceDB(
                account_id=account_id,
                balance_date=date(2026, 5, 2),
                balance=Decimal("4000.00"),
                balance_basis="absolute",
                transaction_count=2,
                balance_7d_avg=Decimal("4000.00"),
                balance_30d_avg=Decimal("4000.00"),
                balance_7d_min=Decimal("4000.00"),
                balance_7d_max=Decimal("4000.00"),
                credit_utilization=None,
                computed_at=now,
            )
        )
        session.add(
            RecurringPatternDB(
                pattern_id="subscription",
                pattern_type="subscription",
                merchant="STREAMING",
                category="Entertainment",
                institution="test-bank",
                account_id=account_id,
                frequency="monthly",
                expected_day_of_period=1,
                is_fixed_amount=True,
                expected_amount=Decimal("45.00"),
                amount_std_dev=Decimal("0.00"),
                confidence=0.9,
                detection_method="test",
                occurrence_count=3,
                first_occurrence=date(2026, 3, 1),
                last_occurrence=date(2026, 5, 1),
                next_expected_date=date(2026, 6, 1),
                is_active=True,
                missed_count=0,
                detected_at=now,
                updated_at=now,
            )
        )
        session.add(
            FinancialHealthScoreDB(
                score_date=date(2026, 5, 2),
                overall_score=82,
                cashflow_health=75,
                spending_consistency=80,
                savings_rate=60,
                emergency_fund_months=Decimal("8.00"),
                debt_ratio=Decimal("0.10"),
                score_trend_7d="stable",
                score_change_7d=0,
                risk_factors="",
                computed_at=now,
            )
        )

    summary = PlanningReadinessService(db_path).summary(
        date(2026, 5, 1),
        date(2026, 5, 31),
        pending_reviews=2,
    )

    assert summary["budgets"]["active"] == 0
    assert summary["goals"]["active"] == 0
    assert summary["recurring_monthly"] == 45.0
    assert summary["health"]["overall_score"] == 82
    assert summary["health"]["emergency_fund_months"] == 8.0
    assert summary["data_freshness"]["latest_transaction"] == date(2026, 5, 2)
    assert "Resolve 2 review row(s)." in summary["next_actions"]
    assert "Create monthly category budgets." in summary["next_actions"]
    assert "Create account-linked savings goals." in summary["next_actions"]

    with session_scope(db_path) as session:
        assert session.query(BudgetDB).count() == 0
        assert session.query(SavingsGoalDB).count() == 0


def test_quality_uses_unmatched_transfer_stat_key(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    with session_scope(db_path) as session:
        session.add(
            TransferDB(
                txn_id="transfer-1",
                posting_date=date(2026, 5, 3),
                institution="test-bank",
                account="1111",
                account_type="chequing",
                flow="transfer",
                amount_raw=Decimal("50.00"),
                description="TRANSFER OUT",
                source_file="statement.csv",
                imported_at=_now(),
            )
        )

    issues = DataQualityService(db_path).run_checks()
    transfer_issue = next(issue for issue in issues if issue.check_name == "unmatched_transfers")

    assert transfer_issue.issue_count == 1
    assert transfer_issue.status == "warn"


def test_insight_fingerprint_distinguishes_recurring_patterns() -> None:
    first = GeneratedFinding(
        finding_type="recurring",
        severity="medium",
        title="Recurring cost: Condo",
        explanation="First recurring pattern.",
        evidence={"merchant": "Condo", "pattern_id": "pattern-1"},
        suggested_action="Review.",
    )
    second = GeneratedFinding(
        finding_type="recurring",
        severity="medium",
        title="Recurring cost: Condo",
        explanation="Second recurring pattern.",
        evidence={"merchant": "Condo", "pattern_id": "pattern-2"},
        suggested_action="Review.",
    )

    assert first.fingerprint(date(2026, 5, 1), date(2026, 5, 31)) != second.fingerprint(
        date(2026, 5, 1),
        date(2026, 5, 31),
    )
