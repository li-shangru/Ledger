"""Deterministic, source-backed explanations for displayed financial metrics."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from account_fixtures import seed_account

from ledger.adapters.registry import CompatibilityPolicySnapshot
from ledger.adjustments import AdjustmentDetectionService
from ledger.reporting.explanations import MetricExplanationService
from ledger.storage.database import (
    AccountBalanceDB,
    BudgetDB,
    PlannedScheduleDB,
    RecurringPatternDB,
    TransactionDB,
    TransactionOffsetDB,
    TransactionTreatmentDB,
    init_database,
    session_scope,
)
from ledger.storage.read_only import ReadOnlyDatabase


@pytest.fixture
def invented_reimbursement_policy(monkeypatch):
    policy = CompatibilityPolicySnapshot(
        digest="invented-reimbursement-policy",
        credit_institutions=frozenset(),
        credit_account_tokens=(),
        payment_tokens=(),
        reimbursement_merchants=frozenset({"INVENTED BENEFITS"}),
    )
    monkeypatch.setattr("ledger.adjustments.service.compatibility_policy_snapshot", lambda: policy)


def _temporal_fields(posted: date) -> dict[str, object]:
    return {
        "day_of_week": posted.weekday(),
        "day_of_month": posted.day,
        "week_of_year": posted.isocalendar()[1],
        "is_weekend": posted.weekday() >= 5,
        "is_month_start": posted.day <= 3,
        "is_month_end": posted.day >= 28,
    }


def _transaction(
    txn_id: str,
    *,
    posted: date,
    amount: str,
    merchant: str,
    category: str,
    currency: str = "CAD",
    include_in_cashflow: bool = True,
    exclude_from_spending: bool = False,
    account: str = "fixture-chequing",
    description: str | None = None,
) -> TransactionDB:
    value = Decimal(amount)
    cashflow_type = "expense" if value < 0 else "income"
    return TransactionDB(
        txn_id=txn_id,
        posting_date=posted,
        transaction_date=posted,
        institution="fixture-bank",
        account=account,
        account_type="chequing",
        flow=cashflow_type,
        cashflow_type=cashflow_type,
        include_in_cashflow=include_in_cashflow,
        currency=currency,
        amount_raw=abs(value),
        amount_signed=value,
        description=description or merchant,
        merchant=merchant,
        category=category,
        exclude_from_spending=exclude_from_spending,
        source_file="synthetic.csv",
        source_path="/synthetic/source.csv",
        source_period=posted.strftime("%Y-%m"),
        source_row=1,
        imported_at=datetime(2026, 1, 31, 12, 0),
        parser_version="synthetic",
        txn_fingerprint=txn_id[:32],
        **_temporal_fields(posted),
    )


def _seed_true_cost_period(db_path: Path) -> None:
    init_database(db_path)
    with session_scope(db_path) as session:
        session.add_all(
            [
                _transaction(
                    "1" * 40,
                    posted=date(2026, 1, 2),
                    amount="1000.00",
                    merchant="PAYROLL",
                    category="Income",
                ),
                _transaction(
                    "2" * 40,
                    posted=date(2026, 1, 3),
                    amount="-100.00",
                    merchant="ELECTRONICS SHOP",
                    category="Electronics",
                ),
                _transaction(
                    "3" * 40,
                    posted=date(2026, 1, 4),
                    amount="40.00",
                    merchant="ELECTRONICS SHOP",
                    category="Electronics",
                    description="ELECTRONICS SHOP (Credit)",
                ),
                _transaction(
                    "4" * 40,
                    posted=date(2026, 1, 5),
                    amount="10.00",
                    merchant="CARD REWARD",
                    category="Rewards",
                    description="Cashback reward",
                ),
                _transaction(
                    "5" * 40,
                    posted=date(2026, 1, 6),
                    amount="-25.00",
                    merchant="EXCLUDED",
                    category="Transfers",
                    exclude_from_spending=True,
                ),
                _transaction(
                    "6" * 40,
                    posted=date(2026, 1, 7),
                    amount="-50.00",
                    merchant="USD MERCHANT",
                    category="Electronics",
                    currency="USD",
                ),
            ]
        )
    result = AdjustmentDetectionService(db_path).detect(execute=True)
    assert result.ok


def _seed_account_anchor(db_path: Path, *, with_anchor: bool = True) -> str:
    init_database(db_path)
    account_id = seed_account(
        db_path,
        "fixture-bank",
        "fixture-chequing",
        account_type="chequing",
    )
    if with_anchor:
        with session_scope(db_path) as session:
            session.add(
                AccountBalanceDB(
                    account_id=account_id,
                    balance_date=date(2026, 1, 20),
                    balance=Decimal("500.00"),
                    balance_basis="absolute",
                    transaction_count=3,
                    balance_7d_avg=Decimal("500.00"),
                    balance_30d_avg=Decimal("500.00"),
                    balance_7d_min=Decimal("500.00"),
                    balance_7d_max=Decimal("500.00"),
                    credit_utilization=None,
                    computed_at=datetime(2026, 1, 20, 12, 0),
                )
            )
    return account_id


def test_income_explanation_reconciles_adjusted_income_and_is_json_safe(tmp_path: Path) -> None:
    """Dropping either income adjustment would make the explanation disagree with the KPI."""
    db_path = tmp_path / "ledger.db"
    _seed_true_cost_period(db_path)

    result = MetricExplanationService(db_path).explain(
        "income",
        date(2026, 1, 1),
        date(2026, 1, 31),
    )

    assert result["status"] == "reported"
    assert result["metric"] == "income"
    assert result["formula"] == (
        "max(0, gross_income - refunds - reimbursements - other_adjustments)"
    )
    assert result["period"] == {
        "start": "2026-01-01",
        "end": "2026-01-31",
        "inclusive": True,
    }
    assert result["currency"] == "CAD"
    assert result["total"] == "1000.00"
    assert result["components"] == [
        {"name": "gross_income", "operation": "add", "amount": "1050.00"},
        {"name": "refunds", "operation": "subtract", "amount": "40.00"},
        {"name": "reimbursements", "operation": "subtract", "amount": "0.00"},
        {"name": "other_adjustments", "operation": "subtract", "amount": "10.00"},
    ]
    assert result["adjustments"] == {
        "refunds": "40.00",
        "reimbursements": "0.00",
        "other_adjustments": "10.00",
        "total": "50.00",
        "applied_to_total": True,
    }
    assert result["exclusions"]["counts"] == {
        "non_cad": 0,
        "not_in_cashflow": 0,
        "excluded_from_spending": 0,
    }
    assert result["source_date_bounds"] == {
        "earliest": "2026-01-02",
        "latest": "2026-01-05",
    }
    assert result["reconciliation"] == {
        "status": "exact",
        "component_total": "1000.00",
        "displayed_total": "1000.00",
        "difference": "0.00",
    }
    assert {row["evidence_id"] for row in result["relevant_transactions"]} == {
        "1" * 40,
        "3" * 40,
        "4" * 40,
    }
    json.dumps(result)


@pytest.mark.parametrize(
    ("metric", "formula", "total", "components"),
    [
        (
            "gross_spending",
            "reportable_spending",
            "100.00",
            [{"name": "reportable_spending", "operation": "add", "amount": "100.00"}],
        ),
        (
            "offsets",
            "refunds + reimbursements + other_adjustments",
            "40.00",
            [
                {"name": "refunds", "operation": "add", "amount": "40.00"},
                {"name": "reimbursements", "operation": "add", "amount": "0.00"},
                {"name": "other_adjustments", "operation": "add", "amount": "0.00"},
            ],
        ),
        (
            "true_cost",
            "max(0, gross_spending - refunds - reimbursements - other_adjustments)",
            "60.00",
            [
                {"name": "gross_spending", "operation": "add", "amount": "100.00"},
                {"name": "refunds", "operation": "subtract", "amount": "40.00"},
                {"name": "reimbursements", "operation": "subtract", "amount": "0.00"},
                {"name": "other_adjustments", "operation": "subtract", "amount": "0.00"},
            ],
        ),
        (
            "net",
            "adjusted_income - true_cost",
            "940.00",
            [
                {"name": "adjusted_income", "operation": "add", "amount": "1000.00"},
                {"name": "true_cost", "operation": "subtract", "amount": "60.00"},
            ],
        ),
    ],
)
def test_period_metric_explanations_reconcile_to_the_canonical_true_cost_summary(
    tmp_path: Path,
    metric: str,
    formula: str,
    total: str,
    components: list[dict[str, str]],
) -> None:
    """Changing any period metric to a gross/raw total would break exact reconciliation."""
    db_path = tmp_path / "ledger.db"
    _seed_true_cost_period(db_path)

    result = MetricExplanationService(db_path).explain(
        metric,
        date(2026, 1, 1),
        date(2026, 1, 31),
    )

    assert result["status"] == "reported"
    assert result["formula"] == formula
    assert result["total"] == total
    assert result["components"] == components
    assert result["reconciliation"] == {
        "status": "exact",
        "component_total": total,
        "displayed_total": total,
        "difference": "0.00",
    }
    assert result["exclusions"]["counts"] == {
        "non_cad": 1,
        "not_in_cashflow": 0,
        "excluded_from_spending": 1,
    }
    assert result["exclusions"]["amounts"] == {
        "non_cad": "50.00",
        "not_in_cashflow": "0.00",
        "excluded_from_spending": "25.00",
    }


@pytest.mark.parametrize("metric", ["income", "true_cost"])
def test_floored_metrics_disclose_the_floor_needed_for_exact_reconciliation(
    tmp_path: Path,
    metric: str,
) -> None:
    """Even inconsistent retained offsets must not produce a displayed mismatch."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    debit_id = "d" * 40
    credit_id = "e" * 40
    detected_at = datetime(2026, 1, 31, 12, 0)
    with session_scope(db_path) as session:
        session.add_all(
            [
                _transaction(
                    debit_id,
                    posted=date(2026, 1, 2),
                    amount="-10.00",
                    merchant="SYNTHETIC SHOP",
                    category="Shopping",
                ),
                _transaction(
                    credit_id,
                    posted=date(2026, 1, 3),
                    amount="20.00",
                    merchant="SYNTHETIC SHOP",
                    category="Shopping",
                ),
                TransactionTreatmentDB(
                    txn_id=credit_id,
                    treatment_type="product_return",
                    status="auto_applied",
                    confidence=Decimal("1.00"),
                    netting_policy="reduce_category_cost",
                    target_category="Shopping",
                    evidence={},
                    detected_at=detected_at,
                    source="deterministic_v1",
                ),
                TransactionOffsetDB(
                    credit_txn_id=credit_id,
                    debit_txn_id=debit_id,
                    offset_amount=Decimal("30.00"),
                    confidence=Decimal("1.00"),
                    status="auto_applied",
                    evidence={},
                    detected_at=detected_at,
                    source="deterministic_v1",
                ),
            ]
        )

    result = MetricExplanationService(db_path).explain(
        metric,
        date(2026, 1, 1),
        date(2026, 1, 31),
    )

    assert result["total"] == "0.00"
    assert result["formula"].startswith("max(0, ")
    assert result["components"][-1] == {
        "name": "floor_at_zero",
        "operation": "add",
        "amount": "10.00" if metric == "income" else "20.00",
    }
    assert result["reconciliation"] == {
        "status": "exact",
        "component_total": "0.00",
        "displayed_total": "0.00",
        "difference": "0.00",
    }


def test_balance_explanation_names_the_statement_anchor_and_as_of_date(tmp_path: Path) -> None:
    """Using a relative row or hiding anchor age would make a stale balance look current."""
    db_path = tmp_path / "ledger.db"
    account_id = _seed_account_anchor(db_path)

    result = MetricExplanationService(db_path).explain(
        "balance",
        date(2026, 1, 1),
        date(2026, 1, 31),
        account_id=account_id,
    )

    assert result["status"] == "reported"
    assert result["formula"] == "statement_anchor_as_of"
    assert result["total"] == "500.00"
    assert result["components"] == [
        {"name": "statement_anchor", "operation": "add", "amount": "500.00"}
    ]
    assert result["statement_anchor"] == {
        "basis": "absolute",
        "balance_date": "2026-01-20",
        "as_of": "2026-01-31",
        "lag_days": 11,
        "is_historical": True,
    }
    assert result["source_date_bounds"] == {
        "earliest": "2026-01-20",
        "latest": "2026-01-20",
    }
    assert result["reconciliation"]["status"] == "exact"


def test_balance_without_an_absolute_statement_anchor_is_unavailable_not_zero(
    tmp_path: Path,
) -> None:
    """A missing statement anchor must not be rendered as a reassuring zero balance."""
    db_path = tmp_path / "ledger.db"
    account_id = _seed_account_anchor(db_path, with_anchor=False)

    result = MetricExplanationService(db_path).explain(
        "balance",
        date(2026, 1, 1),
        date(2026, 1, 31),
        account_id=account_id,
    )

    assert result["status"] == "unavailable"
    assert result["total"] is None
    assert result["unavailable"]["reason"] == "no_absolute_anchor"
    assert result["reconciliation"]["status"] == "unavailable"


def test_relative_change_explanation_reconciles_the_exact_displayed_snapshot(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ledger.db"
    account_id = _seed_account_anchor(db_path, with_anchor=False)
    with session_scope(db_path) as session:
        session.add(
            AccountBalanceDB(
                account_id=account_id,
                balance_date=date(2026, 1, 20),
                balance=Decimal("25.00"),
                balance_basis="relative",
                transaction_count=3,
                balance_7d_avg=Decimal("25.00"),
                balance_30d_avg=Decimal("25.00"),
                balance_7d_min=Decimal("25.00"),
                balance_7d_max=Decimal("25.00"),
                credit_utilization=None,
                computed_at=datetime(2026, 1, 20, 12, 0),
            )
        )

    result = MetricExplanationService(db_path).explain(
        "balance",
        date(2026, 1, 1),
        date(2026, 1, 31),
        account_id=account_id,
        balance_basis="relative",
        balance_date=date(2026, 1, 20),
    )

    assert result["status"] == "reported"
    assert result["formula"] == "relative_change_as_of"
    assert result["total"] == "25.00"
    assert result["components"] == [
        {"name": "relative_account_change", "operation": "add", "amount": "25.00"}
    ]
    assert result["balance_snapshot"] == {
        "basis": "relative",
        "balance_date": "2026-01-20",
        "as_of": "2026-01-31",
        "lag_days": 11,
        "is_historical": True,
    }
    assert result["reconciliation"]["status"] == "exact"


def test_budget_explanation_reconciles_prorated_definitions_and_gross_actual(
    tmp_path: Path,
) -> None:
    """Ignoring one matching definition or using true cost as actual would misstate the budget."""
    db_path = tmp_path / "ledger.db"
    _seed_true_cost_period(db_path)
    with session_scope(db_path) as session:
        session.add_all(
            [
                BudgetDB(
                    budget_month="*",
                    category="Electronics",
                    amount=Decimal("200.00"),
                    notes="recurring",
                    created_at=datetime(2026, 1, 1, 12, 0),
                    updated_at=datetime(2026, 1, 1, 12, 0),
                ),
                BudgetDB(
                    budget_month="2026-01",
                    category="Electronics",
                    amount=Decimal("50.00"),
                    notes="period top-up",
                    created_at=datetime(2026, 1, 1, 12, 0),
                    updated_at=datetime(2026, 1, 1, 12, 0),
                ),
            ]
        )

    result = MetricExplanationService(db_path).explain(
        "budget",
        date(2026, 1, 1),
        date(2026, 1, 31),
        category="Electronics",
    )

    assert result["status"] == "reported"
    assert result["formula"] == "sum(prorated_matching_budget_definitions)"
    assert result["total"] == "250.00"
    assert result["components"] == [
        {
            "name": "prorated_matching_budget_definitions",
            "operation": "add",
            "amount": "250.00",
        }
    ]
    assert result["budget_context"] == {
        "category": "Electronics",
        "actual_gross_spending": "100.00",
        "variance": "150.00",
        "status": "ok",
        "definition_count": 2,
        "actual_basis": "gross CAD reportable spending",
    }
    assert result["source_date_bounds"] == {
        "earliest": "2026-01-03",
        "latest": "2026-01-03",
    }
    assert result["reconciliation"]["status"] == "exact"

    actual = MetricExplanationService(db_path).explain(
        "budget_actual",
        date(2026, 1, 1),
        date(2026, 1, 31),
        category="Electronics",
    )
    assert actual["status"] == "reported"
    assert actual["formula"] == "sum(gross_CAD_spending_in_budget_scope)"
    assert actual["total"] == "100.00"
    assert actual["components"] == [
        {
            "name": "gross_CAD_spending_in_budget_scope",
            "operation": "add",
            "amount": "100.00",
        }
    ]
    assert actual["reconciliation"]["status"] == "exact"

    variance = MetricExplanationService(db_path).explain(
        "budget_variance",
        date(2026, 1, 1),
        date(2026, 1, 31),
        category="Electronics",
    )
    assert variance["status"] == "reported"
    assert variance["formula"] == "budgeted - actual_gross_spending"
    assert variance["total"] == "150.00"
    assert variance["components"] == [
        {
            "name": "budgeted",
            "operation": "add",
            "amount": "250.00",
        },
        {
            "name": "actual_gross_spending",
            "operation": "subtract",
            "amount": "100.00",
        },
    ]
    assert variance["reconciliation"]["status"] == "exact"


def test_budget_without_a_matching_definition_is_unavailable_not_zero(tmp_path: Path) -> None:
    """An absent category budget must remain unknown instead of becoming a zero target."""
    db_path = tmp_path / "ledger.db"
    _seed_true_cost_period(db_path)

    result = MetricExplanationService(db_path).explain(
        "budget",
        date(2026, 1, 1),
        date(2026, 1, 31),
        category="Electronics",
    )

    assert result["status"] == "unavailable"
    assert result["total"] is None
    assert result["unavailable"]["reason"] == "no_budget_defined"


def test_unbudgeted_headlines_reconcile_to_the_same_visible_ui_arithmetic(
    tmp_path: Path,
) -> None:
    """The page displays 0 budget, gross actual, and 0-minus-actual variance."""
    db_path = tmp_path / "ledger.db"
    _seed_true_cost_period(db_path)
    service = MetricExplanationService(db_path)

    explanations = {
        metric: service.explain(metric, date(2026, 1, 1), date(2026, 1, 31))
        for metric in ("budget", "budget_actual", "budget_variance")
    }

    assert explanations["budget"]["total"] == "0.00"
    assert explanations["budget_actual"]["total"] == "100.00"
    assert explanations["budget_variance"]["total"] == "-100.00"
    assert all(row["status"] == "reported" for row in explanations.values())
    assert all(row["reconciliation"]["status"] == "exact" for row in explanations.values())
    assert all(row["budget_context"]["status"] == "unbudgeted" for row in explanations.values())
    assert all(row["budget_context"]["definition_count"] == 0 for row in explanations.values())


def test_projection_explanation_is_anchor_plus_schedule_aware_cash_plan_arithmetic(
    tmp_path: Path,
) -> None:
    """The explanation must match Planning's schedules plus detected-pattern cash plan."""
    db_path = tmp_path / "ledger.db"
    account_id = _seed_account_anchor(db_path)
    with session_scope(db_path) as session:
        session.add(
            RecurringPatternDB(
                pattern_id="a" * 40,
                pattern_type="income",
                merchant="SYNTHETIC PAYROLL",
                category="Income",
                institution="fixture-bank",
                account_id=account_id,
                frequency="monthly",
                expected_day_of_period=1,
                is_fixed_amount=True,
                expected_amount=Decimal("100.00"),
                amount_std_dev=Decimal("0.00"),
                confidence=1.0,
                detection_method="synthetic",
                occurrence_count=3,
                first_occurrence=date(2025, 11, 1),
                last_occurrence=date(2026, 1, 1),
                next_expected_date=date(2026, 2, 1),
                is_active=True,
                missed_count=0,
                detected_at=datetime(2026, 1, 20, 12, 0),
                updated_at=datetime(2026, 1, 20, 12, 0),
            )
        )
        session.add(
            PlannedScheduleDB(
                schedule_id="planned-utility",
                name="Confirmed utility",
                event_type="bill",
                amount=Decimal("30.00"),
                currency="CAD",
                account_id=account_id,
                counterparty_account_id=None,
                next_date=date(2026, 2, 10),
                frequency="once",
                provenance="confirmed",
                pattern_id=None,
                is_active=True,
                note="Synthetic fixture",
                created_at=datetime(2026, 1, 20, 12, 0),
                updated_at=datetime(2026, 1, 20, 12, 0),
            )
        )

    result = MetricExplanationService(db_path).explain(
        "projection",
        date(2026, 1, 31),
        date(2026, 2, 20),
        account_id=account_id,
    )

    assert result["status"] == "reported"
    assert result["formula"] == "statement_anchor + cash_plan_movement_since_anchor"
    assert result["total"] == "570.00"
    assert result["components"] == [
        {"name": "statement_anchor", "operation": "add", "amount": "500.00"},
        {
            "name": "cash_plan_movement_since_anchor",
            "operation": "add",
            "amount": "70.00",
        },
    ]
    assert result["projection_context"]["anchor"] == {
        "basis": "absolute",
        "balance": "500.00",
        "balance_date": "2026-01-20",
        "as_of": "2026-01-31",
        "lag_days": 11,
    }
    assert result["projection_context"]["target_date"] == "2026-02-20"
    assert result["projection_context"]["net_projected_flow"] == "70.00"
    assert [row["source_kind"] for row in result["projection_context"]["line_items"]] == [
        "detected_pattern",
        "schedule",
    ]
    assert [row["amount"] for row in result["projection_context"]["line_items"]] == [
        "100.00",
        "-30.00",
    ]
    assert [row["provenance"] for row in result["projection_context"]["line_items"]] == [
        "detected",
        "confirmed",
    ]
    assert result["projection_context"]["source_coverage"] == {
        "stored_schedule_count": 1,
        "active_schedule_count": 1,
        "confirmed_schedule_count": 1,
        "hypothetical_schedule_count": 0,
        "detected_pattern_count": 1,
        "overridden_detected_pattern_count": 0,
        "suppressed_occurrence_count": 0,
        "ambiguous_suppression_count": 0,
    }
    assert result["projection_context"]["assumptions"]
    assert result["source_date_bounds"] == {
        "earliest": "2026-01-20",
        "latest": "2026-02-10",
    }
    assert result["reconciliation"]["status"] == "exact"


def test_period_explanation_scopes_transactions_to_canonical_account_identity(
    tmp_path: Path,
) -> None:
    """Filtering only by date/category would leak a second account into the explained total."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    first_id = seed_account(db_path, "fixture-bank", "first-chequing")
    seed_account(db_path, "fixture-bank", "second-chequing")
    with session_scope(db_path) as session:
        session.add_all(
            [
                _transaction(
                    "7" * 40,
                    posted=date(2026, 1, 2),
                    amount="100.00",
                    merchant="FIRST PAY",
                    category="Income",
                    account="first-chequing",
                ),
                _transaction(
                    "8" * 40,
                    posted=date(2026, 1, 3),
                    amount="-40.00",
                    merchant="FIRST SHOP",
                    category="Dining",
                    account="first-chequing",
                ),
                _transaction(
                    "9" * 40,
                    posted=date(2026, 1, 2),
                    amount="200.00",
                    merchant="SECOND PAY",
                    category="Income",
                    account="second-chequing",
                ),
                _transaction(
                    "a" * 40,
                    posted=date(2026, 1, 3),
                    amount="-50.00",
                    merchant="SECOND SHOP",
                    category="Dining",
                    account="second-chequing",
                ),
            ]
        )

    result = MetricExplanationService(db_path).explain(
        "net",
        date(2026, 1, 1),
        date(2026, 1, 31),
        account_id=first_id,
    )

    assert result["status"] == "reported"
    assert result["total"] == "60.00"
    assert result["components"] == [
        {"name": "adjusted_income", "operation": "add", "amount": "100.00"},
        {"name": "true_cost", "operation": "subtract", "amount": "40.00"},
    ]
    assert {row["evidence_id"] for row in result["relevant_transactions"]} == {
        "7" * 40,
        "8" * 40,
    }


def test_relevant_transaction_evidence_is_deterministically_bounded(tmp_path: Path) -> None:
    """A large period must keep the response bounded without hiding the full source count."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    with session_scope(db_path) as session:
        session.add_all(
            _transaction(
                f"{index:040x}",
                posted=date(2026, 1, 2),
                amount="1.00",
                merchant=f"SOURCE {index}",
                category="Income",
            )
            for index in range(25)
        )

    result = MetricExplanationService(db_path).explain(
        "income",
        date(2026, 1, 1),
        date(2026, 1, 31),
    )

    assert result["total"] == "25.00"
    assert result["evidence"] == {
        "limit": 20,
        "total_count": 25,
        "returned_count": 20,
        "truncated": True,
    }
    assert [row["evidence_id"] for row in result["relevant_transactions"]] == [
        f"{index:040x}" for index in range(20)
    ]
    assert [row["merchant"] for row in result["relevant_transactions"]] == [
        f"SOURCE {index}" for index in range(20)
    ]
    for row in result["relevant_transactions"]:
        assert set(row) == {
            "evidence_id",
            "posting_date",
            "amount",
            "currency",
            "category",
            "merchant",
            "role",
        }


def test_explanation_uses_a_caller_supplied_pinned_read_session(tmp_path: Path) -> None:
    """Opening an independent writable session would break MCP snapshot pinning."""
    db_path = tmp_path / "ledger.db"
    _seed_true_cost_period(db_path)
    database = ReadOnlyDatabase(db_path)
    try:
        database.preflight()
        result = MetricExplanationService(
            db_path,
            session_scope_factory=database.session_scope,
        ).explain(
            "true_cost",
            date(2026, 1, 1),
            date(2026, 1, 31),
        )
    finally:
        database.close()

    assert result["status"] == "reported"
    assert result["total"] == "60.00"
    assert result["reconciliation"]["status"] == "exact"


def test_true_cost_explanation_separates_reimbursements_from_refunds(
    tmp_path: Path, invented_reimbursement_policy
) -> None:
    """Collapsing insurer reimbursements into generic refunds would hide their treatment."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    with session_scope(db_path) as session:
        session.add_all(
            [
                _transaction(
                    "b" * 40,
                    posted=date(2026, 1, 3),
                    amount="-200.00",
                    merchant="DENTIST",
                    category="Health & Wellness",
                ),
                _transaction(
                    "c" * 40,
                    posted=date(2026, 1, 20),
                    amount="80.00",
                    merchant="INVENTED BENEFITS",
                    category="Insurance",
                    description="Misc Payment INVENTED BENEFITS",
                ),
            ]
        )
    assert AdjustmentDetectionService(db_path).detect(execute=True).ok

    result = MetricExplanationService(db_path).explain(
        "true_cost",
        date(2026, 1, 1),
        date(2026, 1, 31),
    )

    assert result["total"] == "120.00"
    assert result["adjustments"] == {
        "refunds": "0.00",
        "reimbursements": "80.00",
        "other_adjustments": "0.00",
        "total": "80.00",
        "applied_to_total": True,
    }
    assert result["components"] == [
        {"name": "gross_spending", "operation": "add", "amount": "200.00"},
        {"name": "refunds", "operation": "subtract", "amount": "0.00"},
        {"name": "reimbursements", "operation": "subtract", "amount": "80.00"},
        {"name": "other_adjustments", "operation": "subtract", "amount": "0.00"},
    ]
