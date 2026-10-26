"""
Test financial health scoring functionality.

Tests daily 0-100 health metrics.

Clock discipline: all tests that anchor data to a relative window use a frozen
reference date (FROZEN_TODAY) instead of date.today() so they are stable across
calendar boundaries and can exercise specific date arithmetic.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from account_fixtures import seed_account, seed_accounts_for_existing_rows

from ledger.analysis import AnalysisRunner, FinancialHealthScorer
from ledger.core import Transaction
from ledger.processing import ProcessingResult
from ledger.storage.database import FinancialHealthScoreDB, init_database, session_scope
from ledger.storage.writer import DatabaseWriter

# Frozen reference date used in place of date.today() throughout this module.
# Any test that computes "yesterday" or "30 days ago" should use this constant
# rather than date.today() so results are deterministic.
FROZEN_TODAY = date(2026, 1, 15)
FROZEN_YESTERDAY = FROZEN_TODAY - timedelta(days=1)


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    """Create a temporary database for testing."""
    db_path = tmp_path / "test_health.db"
    init_database(db_path)
    return db_path


def create_test_transactions(
    start_date: date,
    days: int,
    daily_income: Decimal,
    daily_expenses: Decimal,
) -> list[Transaction]:
    """Helper to create transactions for testing."""
    transactions = []

    for i in range(days):
        txn_date = start_date + timedelta(days=i)
        day_of_week = txn_date.weekday()
        week_of_year = txn_date.isocalendar()[1]

        # Create income transaction
        if daily_income > 0:
            income_txn = Transaction(
                txn_id=f"income_{txn_date.isoformat()}",
                posting_date=txn_date,
                transaction_date=txn_date,
                institution="TestBank",
                account="1234",
                account_type="checking",
                flow="income",
                cashflow_type="income",
                include_in_cashflow=True,
                currency="CAD",
                amount_raw=daily_income,
                amount_signed=daily_income,
                description="Salary",
                merchant="Employer",
                category="Income",
                merchant_confidence="high",
                category_confidence="high",
                exclude_from_spending=True,
                merchant_raw="Employer",
                category_raw="Income",
                source_file="test.csv",
                source_path="/test",
                source_period=txn_date.strftime("%Y-%m"),
                source_row=i * 2,
                imported_at=datetime.now(UTC).replace(tzinfo=None),
                parser_version="1.0.0",
                txn_fingerprint=f"fp_income_{i}",
                day_of_week=day_of_week,
                day_of_month=txn_date.day,
                week_of_year=week_of_year,
                is_weekend=day_of_week >= 5,
                is_month_start=txn_date.day <= 3,
                is_month_end=txn_date.day >= 28,
            )
            transactions.append(income_txn)

        # Create expense transaction
        if daily_expenses > 0:
            expense_txn = Transaction(
                txn_id=f"expense_{txn_date.isoformat()}",
                posting_date=txn_date,
                transaction_date=txn_date,
                institution="TestBank",
                account="5678",
                account_type="credit_card",
                flow="expense",
                cashflow_type="expense",
                include_in_cashflow=True,
                currency="CAD",
                amount_raw=daily_expenses,
                amount_signed=-daily_expenses,
                description="Purchase",
                merchant="Store",
                category="Shopping",
                merchant_confidence="high",
                category_confidence="high",
                exclude_from_spending=False,
                merchant_raw="Store",
                category_raw="Shopping",
                source_file="test.csv",
                source_path="/test",
                source_period=txn_date.strftime("%Y-%m"),
                source_row=i * 2 + 1,
                imported_at=datetime.now(UTC).replace(tzinfo=None),
                parser_version="1.0.0",
                txn_fingerprint=f"fp_expense_{i}",
                day_of_week=day_of_week,
                day_of_month=txn_date.day,
                week_of_year=week_of_year,
                is_weekend=day_of_week >= 5,
                is_month_start=txn_date.day <= 3,
                is_month_end=txn_date.day >= 28,
            )
            transactions.append(expense_txn)

    return transactions


def test_savings_rate_stored_as_real_percentage(temp_db: Path):
    """savings_rate must be the actual % of income saved (40%), not the 0-100 bucket
    sub-score (which stored 80 for a 40% saver)."""
    start_date = FROZEN_TODAY - timedelta(days=30)
    transactions = create_test_transactions(
        start_date=start_date,
        days=30,
        daily_income=Decimal("100.00"),
        daily_expenses=Decimal("60.00"),
    )
    writer = DatabaseWriter(temp_db)
    writer.write_result(
        ProcessingResult(
            transactions=transactions,
            transfers=[],
            exceptions=[],
            duplicates_removed=0,
            total_scanned=1,
        ),
        Path("/test"),
        "1.0.0",
    )

    scorer = FinancialHealthScorer(temp_db)
    score = scorer.compute_daily_score(FROZEN_YESTERDAY)

    assert score is not None
    assert score.savings_rate == Decimal("40.00")


def _expense_txn(
    txn_date: date,
    amount: Decimal,
    *,
    exclude_from_spending: bool = False,
    currency: str = "CAD",
    key: str = "e",
) -> Transaction:
    """Build a single expense Transaction for spend-universe filter tests."""
    dow = txn_date.weekday()
    return Transaction(
        txn_id=f"expense_{key}_{txn_date.isoformat()}",
        posting_date=txn_date,
        transaction_date=txn_date,
        institution="TestBank",
        account="5678",
        account_type="credit_card",
        flow="expense",
        cashflow_type="expense",
        include_in_cashflow=True,
        currency=currency,
        amount_raw=amount,
        amount_signed=-amount,
        description="Purchase",
        merchant="Store",
        category="Shopping",
        merchant_confidence="high",
        category_confidence="high",
        exclude_from_spending=exclude_from_spending,
        merchant_raw="Store",
        category_raw="Shopping",
        source_file="test.csv",
        source_path="/test",
        source_period=txn_date.strftime("%Y-%m"),
        source_row=1,
        imported_at=datetime.now(UTC).replace(tzinfo=None),
        parser_version="1.0.0",
        txn_fingerprint=f"fp_{key}_{txn_date.isoformat()}",
        day_of_week=dow,
        day_of_month=txn_date.day,
        week_of_year=txn_date.isocalendar()[1],
        is_weekend=dow >= 5,
        is_month_start=txn_date.day <= 3,
        is_month_end=txn_date.day >= 28,
    )


def test_cashflow_period_excludes_exclude_from_spending_expenses(temp_db: Path):
    """_get_cashflow_period expense total must honor exclude_from_spending, matching
    the canonical spend universe (a reimbursable expense must not depress the ratio)."""
    day = FROZEN_TODAY
    scorer = FinancialHealthScorer(temp_db)
    txns = [
        _expense_txn(day, Decimal("60.00"), exclude_from_spending=False, key="counted"),
        _expense_txn(day, Decimal("40.00"), exclude_from_spending=True, key="excluded"),
    ]
    result = scorer._get_cashflow_period(txns)
    assert result["expenses"] == Decimal("60.00")


def test_spending_consistency_excludes_exclude_from_spending_expenses(temp_db: Path):
    """Daily-spend buckets for the consistency score must drop exclude_from_spending rows."""
    scorer = FinancialHealthScorer(temp_db)
    txns = [
        _expense_txn(FROZEN_TODAY, Decimal("10.00"), exclude_from_spending=False, key="a"),
        # A single large reimbursable on its own day would wreck the consistency
        # variance if counted; it must be excluded from the spend buckets.
        _expense_txn(
            FROZEN_TODAY - timedelta(days=1),
            Decimal("5000.00"),
            exclude_from_spending=True,
            key="b",
        ),
    ]
    # With the reimbursable excluded there is only one spend-day, which is
    # perfectly consistent (100). If it were counted, the $10 vs $5000 spread
    # would drop the score to 50.
    assert scorer._compute_spending_consistency(txns) == 100


def _policy_row(
    txn_date: date,
    amount: str,
    *,
    cashflow_type: str,
    include_in_cashflow: bool = True,
    exclude_from_spending: bool = False,
    currency: str = "CAD",
) -> SimpleNamespace:
    return SimpleNamespace(
        posting_date=txn_date,
        amount_signed=Decimal(amount),
        cashflow_type=cashflow_type,
        include_in_cashflow=include_in_cashflow,
        exclude_from_spending=exclude_from_spending,
        currency=currency,
    )


def test_cashflow_period_uses_strict_policy_for_counts_and_totals(temp_db: Path):
    scorer = FinancialHealthScorer(temp_db)
    day = FROZEN_YESTERDAY
    transactions = [
        _policy_row(day, "100.00", cashflow_type="income"),
        _policy_row(day, "-40.00", cashflow_type="expense"),
        _policy_row(day, "0.00", cashflow_type="income"),
        _policy_row(day, "-900.00", cashflow_type="income"),
        _policy_row(day, "800.00", cashflow_type="expense"),
        _policy_row(day, "0.00", cashflow_type="expense"),
        _policy_row(day, "-500.00", cashflow_type="expense", exclude_from_spending=True),
        _policy_row(day, "-600.00", cashflow_type="expense", include_in_cashflow=False),
    ]

    assert scorer._get_cashflow_period(transactions) == {
        "income": Decimal("100.00"),
        "expenses": Decimal("40.00"),
        "net": Decimal("60.00"),
        "transaction_count": 2,
    }


def test_spending_consistency_ignores_zero_and_invalid_sign_expenses(temp_db: Path):
    scorer = FinancialHealthScorer(temp_db)
    transactions = [
        _policy_row(FROZEN_YESTERDAY, "-10.00", cashflow_type="expense"),
        _policy_row(
            FROZEN_YESTERDAY - timedelta(days=1),
            "5000.00",
            cashflow_type="expense",
        ),
        _policy_row(
            FROZEN_YESTERDAY - timedelta(days=2),
            "0.00",
            cashflow_type="expense",
        ),
    ]

    assert scorer._compute_spending_consistency(transactions) == 100


def test_health_score_requires_twenty_canonical_activity_dates(temp_db: Path):
    target_date = FROZEN_YESTERDAY
    transactions = [
        _policy_row(target_date - timedelta(days=offset), "1.00", cashflow_type="income")
        for offset in range(19)
    ]
    transactions.extend(
        _policy_row(target_date - timedelta(days=offset), "0.00", cashflow_type="income")
        for offset in range(19, 30)
    )

    class FakeQueryService:
        def get_transactions(self, **_kwargs):
            return transactions

        def get_account_balances(self, **_kwargs):
            return []

        def get_accounts(self):
            return []

    scorer = FinancialHealthScorer(temp_db)
    scorer.query_service = FakeQueryService()

    assert scorer.compute_daily_score(target_date) is None


def test_count_active_days_reports_the_gate_input_for_a_target_date(temp_db: Path):
    """The UI needs the same number the gate uses, without re-deriving the rule.

    compute_daily_score silently returns None when the trailing window holds fewer
    than MIN_ACTIVE_DAYS active days, so the dashboard could not tell "analytics not
    re-run" from "too sparse to score" and told users to run a refresh that cannot help.
    """
    from ledger.analysis.health_scorer import MIN_ACTIVE_DAYS, SCORING_WINDOW_DAYS

    target_date = FROZEN_YESTERDAY
    transactions = [
        _policy_row(target_date - timedelta(days=offset), "1.00", cashflow_type="income")
        for offset in range(17)
    ]
    # Same day twice: distinct DAYS is the gate, not row count.
    transactions.append(_policy_row(target_date, "-5.00", cashflow_type="expense"))
    # Rows that carry no cashflow weight must not inflate the count.
    transactions.extend(
        _policy_row(target_date - timedelta(days=offset), "0.00", cashflow_type="income")
        for offset in range(17, SCORING_WINDOW_DAYS)
    )

    class FakeQueryService:
        def get_transactions(self, **_kwargs):
            return transactions

    scorer = FinancialHealthScorer(temp_db)
    scorer.query_service = FakeQueryService()

    assert scorer.count_active_days(target_date) == 17
    assert MIN_ACTIVE_DAYS == 20
    assert SCORING_WINDOW_DAYS == 30


def test_count_active_days_window_excludes_days_before_the_trailing_window(temp_db: Path):
    """Only the trailing SCORING_WINDOW_DAYS may count toward the gate."""
    from ledger.analysis.health_scorer import SCORING_WINDOW_DAYS

    target_date = FROZEN_YESTERDAY
    captured: dict[str, date] = {}
    oldest_in_window = target_date - timedelta(days=SCORING_WINDOW_DAYS - 1)

    class FakeQueryService:
        def get_transactions(self, **kwargs):
            captured.update(start_date=kwargs["start_date"], end_date=kwargs["end_date"])
            return [_policy_row(oldest_in_window, "1.00", cashflow_type="income")]

    scorer = FinancialHealthScorer(temp_db)
    scorer.query_service = FakeQueryService()

    assert scorer.count_active_days(target_date) == 1
    assert captured["start_date"] == oldest_in_window
    assert captured["end_date"] == target_date


def test_health_subscore_mappings(tmp_path: Path):
    """Smooth interpolation + the new emergency-fund and debt sub-score mappings."""
    from ledger.analysis.health_scorer import FinancialHealthScorer, _interpolate

    assert _interpolate(1.0, [(0.0, 0.0), (2.0, 100.0)]) == 50.0
    assert _interpolate(-1.0, [(0.0, 0.0), (2.0, 100.0)]) == 0.0  # clamp low
    assert _interpolate(9.0, [(0.0, 0.0), (2.0, 100.0)]) == 100.0  # clamp high

    db = tmp_path / "x.db"
    init_database(db)
    scorer = FinancialHealthScorer(db)
    assert scorer._emergency_fund_score(Decimal("0")) == 0
    assert scorer._emergency_fund_score(Decimal("3")) == 60
    assert scorer._emergency_fund_score(Decimal("6")) == 100
    assert scorer._emergency_fund_score(Decimal("12")) == 100  # clamp
    assert scorer._debt_score(Decimal("0")) == 100
    assert scorer._debt_score(Decimal("0.5")) == 50
    assert scorer._debt_score(Decimal("1.0")) == 0
    assert scorer._debt_score(Decimal("2.0")) == 0  # clamp


def test_debt_only_absolute_snapshot_cannot_score_as_debt_free(temp_db: Path):
    """Debt only absolute snapshot cannot score as debt free."""
    scorer = FinancialHealthScorer(temp_db)
    accounts = [{"account_id": "card", "is_credit_card": True}]
    balances = [
        {
            "account_id": "card",
            "balance_date": FROZEN_YESTERDAY,
            "balance": Decimal("-500.00"),
            "balance_basis": "absolute",
        }
    ]

    debt_ratio = scorer._compute_debt_ratio(accounts, balances)

    assert debt_ratio == Decimal("999.99")
    assert scorer._debt_score(debt_ratio) == 0


def test_compute_health_score_breaking_even(temp_db: Path):
    """Test health score when income equals expenses (breaking even)."""
    # Create 30 days of transactions: $100 income, $100 expenses per day
    start_date = FROZEN_TODAY - timedelta(days=30)
    transactions = create_test_transactions(
        start_date=start_date,
        days=30,
        daily_income=Decimal("100.00"),
        daily_expenses=Decimal("100.00"),
    )

    # Write to database
    writer = DatabaseWriter(temp_db)
    result = ProcessingResult(
        transactions=transactions,
        transfers=[],
        exceptions=[],
        duplicates_removed=0,
        total_scanned=1,
    )
    writer.write_result(result, Path("/test"), "1.0.0")

    # Compute health score for yesterday
    scorer = FinancialHealthScorer(temp_db)
    target_date = FROZEN_YESTERDAY
    score = scorer.compute_daily_score(target_date)

    assert score is not None
    assert score.score_date == target_date

    # Breaking even: cashflow health should be ~50 (ratio = 1.0)
    assert score.cashflow_health == 50

    # Consistent daily spending should give high consistency score
    assert score.spending_consistency >= 75

    # Breaking even is a 0% savings rate (the field stores the real percentage).
    assert score.savings_rate == Decimal("0.00")

    # Overall score should be moderate
    assert 30 <= score.overall_score <= 60


def test_compute_health_score_high_savings(temp_db: Path):
    """Test health score with high savings rate."""
    # Create 30 days: $200 income, $50 expenses per day (75% savings)
    start_date = FROZEN_TODAY - timedelta(days=30)
    transactions = create_test_transactions(
        start_date=start_date,
        days=30,
        daily_income=Decimal("200.00"),
        daily_expenses=Decimal("50.00"),
    )

    writer = DatabaseWriter(temp_db)
    result = ProcessingResult(
        transactions=transactions,
        transfers=[],
        exceptions=[],
        duplicates_removed=0,
        total_scanned=1,
    )
    writer.write_result(result, Path("/test"), "1.0.0")

    scorer = FinancialHealthScorer(temp_db)
    target_date = FROZEN_YESTERDAY
    score = scorer.compute_daily_score(target_date)

    assert score is not None

    # High income/expense ratio (4.0) should give high cashflow health
    assert score.cashflow_health >= 75

    # Consistent spending
    assert score.spending_consistency >= 75

    # 75% savings rate (income 200, expenses 50) -- the field stores the real percentage.
    assert score.savings_rate == Decimal("75.00")

    # Overall score should be high
    assert score.overall_score >= 80


def test_compute_health_score_deficit(temp_db: Path):
    """Test health score when running a deficit."""
    # Create 30 days: $100 income, $150 expenses per day (50% deficit)
    start_date = FROZEN_TODAY - timedelta(days=30)
    transactions = create_test_transactions(
        start_date=start_date,
        days=30,
        daily_income=Decimal("100.00"),
        daily_expenses=Decimal("150.00"),
    )

    writer = DatabaseWriter(temp_db)
    result = ProcessingResult(
        transactions=transactions,
        transfers=[],
        exceptions=[],
        duplicates_removed=0,
        total_scanned=1,
    )
    writer.write_result(result, Path("/test"), "1.0.0")

    scorer = FinancialHealthScorer(temp_db)
    target_date = FROZEN_YESTERDAY
    score = scorer.compute_daily_score(target_date)

    assert score is not None

    # Deficit (ratio 0.67) should give low cashflow health
    assert score.cashflow_health <= 50

    # Deficit is a negative savings rate (income 100, expenses 150 = -50%).
    assert score.savings_rate == Decimal("-50.00")

    # Should detect deficit risk
    assert any("deficit" in risk.lower() for risk in score.risk_factors)

    # Overall score should be low
    assert score.overall_score <= 40


def test_risk_factor_detection(temp_db: Path):
    """Test that risk factors are detected correctly."""
    # Create deficit scenario
    start_date = FROZEN_TODAY - timedelta(days=30)
    transactions = create_test_transactions(
        start_date=start_date,
        days=30,
        daily_income=Decimal("100.00"),
        daily_expenses=Decimal("95.00"),  # 95% expense ratio
    )

    writer = DatabaseWriter(temp_db)
    result = ProcessingResult(
        transactions=transactions,
        transfers=[],
        exceptions=[],
        duplicates_removed=0,
        total_scanned=1,
    )
    writer.write_result(result, Path("/test"), "1.0.0")

    scorer = FinancialHealthScorer(temp_db)
    target_date = FROZEN_YESTERDAY
    score = scorer.compute_daily_score(target_date)

    assert score is not None

    # Should detect high expense ratio
    assert any("expense ratio" in risk.lower() for risk in score.risk_factors)


def test_score_persistence(temp_db: Path):
    """Test saving and retrieving health scores."""
    # Create transactions
    start_date = FROZEN_TODAY - timedelta(days=30)
    transactions = create_test_transactions(
        start_date=start_date,
        days=30,
        daily_income=Decimal("100.00"),
        daily_expenses=Decimal("80.00"),
    )

    writer = DatabaseWriter(temp_db)
    result = ProcessingResult(
        transactions=transactions,
        transfers=[],
        exceptions=[],
        duplicates_removed=0,
        total_scanned=1,
    )
    writer.write_result(result, Path("/test"), "1.0.0")

    # Compute and save score
    scorer = FinancialHealthScorer(temp_db)
    target_date = FROZEN_YESTERDAY
    score = scorer.compute_daily_score(target_date)

    assert score is not None
    saved = scorer.save_score(score)
    assert saved is True

    # Retrieve from database
    with session_scope(temp_db) as session:
        db_score = session.query(FinancialHealthScoreDB).filter_by(score_date=target_date).first()
        assert db_score is not None
        assert db_score.overall_score == score.overall_score
        assert db_score.cashflow_health == score.cashflow_health


def test_score_trend_calculation(temp_db: Path):
    """Test that score trends are calculated correctly."""
    # Create transactions for 40 days
    start_date = FROZEN_TODAY - timedelta(days=40)
    transactions = create_test_transactions(
        start_date=start_date,
        days=40,
        daily_income=Decimal("100.00"),
        daily_expenses=Decimal("80.00"),
    )

    writer = DatabaseWriter(temp_db)
    result = ProcessingResult(
        transactions=transactions,
        transfers=[],
        exceptions=[],
        duplicates_removed=0,
        total_scanned=1,
    )
    writer.write_result(result, Path("/test"), "1.0.0")

    scorer = FinancialHealthScorer(temp_db)

    # Compute score for 8 days ago
    date_8d_ago = FROZEN_TODAY - timedelta(days=8)
    score_old = scorer.compute_daily_score(date_8d_ago)
    assert score_old is not None
    scorer.save_score(score_old)

    # Compute score for yesterday
    target_date = FROZEN_YESTERDAY
    score_new = scorer.compute_daily_score(target_date)

    assert score_new is not None

    # Should have trend comparison
    assert score_new.score_trend_7d in ["improving", "declining", "stable"]
    assert score_new.score_change_7d == score_new.overall_score - score_old.overall_score


def test_compute_recent_scores(temp_db: Path):
    """Test computing scores for multiple recent days."""
    # Create 40 days of transactions
    start_date = FROZEN_TODAY - timedelta(days=40)
    transactions = create_test_transactions(
        start_date=start_date,
        days=40,
        daily_income=Decimal("100.00"),
        daily_expenses=Decimal("70.00"),
    )

    writer = DatabaseWriter(temp_db)
    result = ProcessingResult(
        transactions=transactions,
        transfers=[],
        exceptions=[],
        duplicates_removed=0,
        total_scanned=1,
    )
    writer.write_result(result, Path("/test"), "1.0.0")

    # Compute scores for last 7 days relative to the frozen reference date.
    scorer = FinancialHealthScorer(temp_db)
    scores_computed = scorer.compute_recent_scores(days=7, today=FROZEN_TODAY)

    assert scores_computed == 7

    # Verify scores in database
    with session_scope(temp_db) as session:
        db_scores = session.query(FinancialHealthScoreDB).all()
        assert len(db_scores) == 7


def test_writer_integration(temp_db: Path):
    """Test health scoring via DatabaseWriter integration."""
    # Create transactions
    start_date = FROZEN_TODAY - timedelta(days=40)
    transactions = create_test_transactions(
        start_date=start_date,
        days=40,
        daily_income=Decimal("100.00"),
        daily_expenses=Decimal("75.00"),
    )

    writer = DatabaseWriter(temp_db)
    result = ProcessingResult(
        transactions=transactions,
        transfers=[],
        exceptions=[],
        duplicates_removed=0,
        total_scanned=1,
    )
    writer.write_result(result, Path("/test"), "1.0.0")

    # Run health scoring via AnalysisRunner directly, frozen to the test reference date.
    results = AnalysisRunner(temp_db).run_health_scoring(days=7, today=FROZEN_TODAY)

    assert results["scores_computed"] == 7


def test_insufficient_data(temp_db: Path):
    """Test that None is returned when insufficient data."""
    # Create only 10 days of data (need 30)
    start_date = FROZEN_TODAY - timedelta(days=10)
    transactions = create_test_transactions(
        start_date=start_date,
        days=10,
        daily_income=Decimal("100.00"),
        daily_expenses=Decimal("80.00"),
    )

    writer = DatabaseWriter(temp_db)
    result = ProcessingResult(
        transactions=transactions,
        transfers=[],
        exceptions=[],
        duplicates_removed=0,
        total_scanned=1,
    )
    writer.write_result(result, Path("/test"), "1.0.0")

    scorer = FinancialHealthScorer(temp_db)
    target_date = FROZEN_YESTERDAY
    score = scorer.compute_daily_score(target_date)

    # Should return None due to insufficient history
    assert score is None


def test_spending_consistency_score(temp_db: Path):
    """Test spending consistency scoring with varying amounts."""
    # Create 30 days with variable spending
    start_date = FROZEN_TODAY - timedelta(days=30)
    transactions = []

    for i in range(30):
        txn_date = start_date + timedelta(days=i)
        day_of_week = txn_date.weekday()
        week_of_year = txn_date.isocalendar()[1]

        # Highly variable expenses: $10 to $100
        daily_expense = Decimal(str(10 + (i % 10) * 10))

        expense_txn = Transaction(
            txn_id=f"expense_{txn_date.isoformat()}",
            posting_date=txn_date,
            transaction_date=txn_date,
            institution="TestBank",
            account="1234",
            account_type="credit_card",
            flow="expense",
            cashflow_type="expense",
            include_in_cashflow=True,
            currency="CAD",
            amount_raw=daily_expense,
            amount_signed=-daily_expense,
            description="Purchase",
            merchant="Store",
            category="Shopping",
            merchant_confidence="high",
            category_confidence="high",
            exclude_from_spending=False,
            merchant_raw="Store",
            category_raw="Shopping",
            source_file="test.csv",
            source_path="/test",
            source_period=txn_date.strftime("%Y-%m"),
            source_row=i,
            imported_at=datetime.now(UTC).replace(tzinfo=None),
            parser_version="1.0.0",
            txn_fingerprint=f"fp_{i}",
            day_of_week=day_of_week,
            day_of_month=txn_date.day,
            week_of_year=week_of_year,
            is_weekend=day_of_week >= 5,
            is_month_start=txn_date.day <= 3,
            is_month_end=txn_date.day >= 28,
        )
        transactions.append(expense_txn)

        # Fixed income
        income_txn = Transaction(
            txn_id=f"income_{txn_date.isoformat()}",
            posting_date=txn_date,
            transaction_date=txn_date,
            institution="TestBank",
            account="5678",
            account_type="checking",
            flow="income",
            cashflow_type="income",
            include_in_cashflow=True,
            currency="CAD",
            amount_raw=Decimal("100.00"),
            amount_signed=Decimal("100.00"),
            description="Salary",
            merchant="Employer",
            category="Income",
            merchant_confidence="high",
            category_confidence="high",
            exclude_from_spending=True,
            merchant_raw="Employer",
            category_raw="Income",
            source_file="test.csv",
            source_path="/test",
            source_period=txn_date.strftime("%Y-%m"),
            source_row=i + 1000,
            imported_at=datetime.now(UTC).replace(tzinfo=None),
            parser_version="1.0.0",
            txn_fingerprint=f"fp_income_{i}",
            day_of_week=day_of_week,
            day_of_month=txn_date.day,
            week_of_year=week_of_year,
            is_weekend=day_of_week >= 5,
            is_month_start=txn_date.day <= 3,
            is_month_end=txn_date.day >= 28,
        )
        transactions.append(income_txn)

    writer = DatabaseWriter(temp_db)
    result = ProcessingResult(
        transactions=transactions,
        transfers=[],
        exceptions=[],
        duplicates_removed=0,
        total_scanned=1,
    )
    writer.write_result(result, Path("/test"), "1.0.0")

    scorer = FinancialHealthScorer(temp_db)
    target_date = FROZEN_YESTERDAY
    score = scorer.compute_daily_score(target_date)

    assert score is not None

    # Variable spending should result in lower consistency score
    assert score.spending_consistency < 100


# ---------------------------------------------------------------------------
# Relative-only balance series must not drive emergency-fund / debt-ratio (M2)
# ---------------------------------------------------------------------------
#
# AccountBalance.balance is a RELATIVE running series seeded at 0 on the first
# observed transaction (balance_calculator.py) — change-since-first-transaction,
# not the account's true balance. No absolute/opening balance is imported, so
# emergency_fund_months and debt_ratio derived from it are meaningless and must
# be reported as "not assessable" (None) and excluded from overall_score.


def _write_balance_series(
    db_path: Path,
    rows: list[tuple[str, date, Decimal]],
    *,
    balance_basis: str = "relative",
) -> None:
    """Insert AccountBalanceDB rows (account_id, balance_date, balance).

    ``balance_basis`` defaults to the calculator's own basis so existing callers keep
    seeding relative series; pass "absolute" to stand in for statement-repair anchors.
    """
    from ledger.storage.database import AccountBalanceDB, session_scope

    now = datetime.now(UTC).replace(tzinfo=None)
    with session_scope(db_path) as session:
        for account_id, balance_date, balance in rows:
            session.merge(
                AccountBalanceDB(
                    account_id=account_id,
                    balance_date=balance_date,
                    balance=balance,
                    balance_basis=balance_basis,
                    transaction_count=1,
                    balance_7d_avg=balance,
                    balance_30d_avg=balance,
                    balance_7d_min=balance,
                    balance_7d_max=balance,
                    credit_utilization=None,
                    computed_at=now,
                )
            )


def test_emergency_fund_not_assessable_from_relative_series(temp_db: Path):
    """A solvent, high-savings profile whose relative balance series ends near its
    zero anchor must NOT be reported as 0 months of emergency fund. The metric is
    not assessable without an absolute (statement/opening) balance, so it is None
    and its 0 sub-score must not be blended into overall_score."""
    start_date = FROZEN_TODAY - timedelta(days=30)
    # 75% savings rate: genuinely solvent profile.
    transactions = create_test_transactions(
        start_date=start_date,
        days=30,
        daily_income=Decimal("200.00"),
        daily_expenses=Decimal("50.00"),
    )
    writer = DatabaseWriter(temp_db)
    writer.write_result(
        ProcessingResult(
            transactions=transactions,
            transfers=[],
            exceptions=[],
            duplicates_removed=0,
            total_scanned=1,
        ),
        Path("/test"),
        "1.0.0",
    )
    seed_accounts_for_existing_rows(temp_db)
    chequing_id = seed_account(temp_db, "TestBank", "1234")
    # A relative balance series that ends right back near its zero anchor — exactly
    # what a solvent chequing account looks like when the window ends where it began.
    _write_balance_series(
        temp_db,
        [(chequing_id, start_date + timedelta(days=i), Decimal("0.00")) for i in range(31)],
    )

    scorer = FinancialHealthScorer(temp_db)
    score = scorer.compute_daily_score(FROZEN_YESTERDAY)

    assert score is not None
    # Not assessable, not a misleading 0.
    assert score.emergency_fund_months is None
    assert score.debt_ratio is None
    # A 75%-savings solvent profile must not be dragged down by a spurious 0.
    assert score.overall_score >= 80


def test_overall_score_composed_only_of_assessable_components(temp_db: Path):
    """When emergency/debt are not assessable, overall_score is the weighted mean of
    ONLY cashflow (0.30), savings (0.25) and consistency (0.10) — renormalized to
    those weights — never blending a spurious 0 emergency/debt sub-score."""
    start_date = FROZEN_TODAY - timedelta(days=30)
    transactions = create_test_transactions(
        start_date=start_date,
        days=30,
        daily_income=Decimal("200.00"),
        daily_expenses=Decimal("50.00"),
    )
    writer = DatabaseWriter(temp_db)
    writer.write_result(
        ProcessingResult(
            transactions=transactions,
            transfers=[],
            exceptions=[],
            duplicates_removed=0,
            total_scanned=1,
        ),
        Path("/test"),
        "1.0.0",
    )
    seed_accounts_for_existing_rows(temp_db)
    chequing_id = seed_account(temp_db, "TestBank", "1234")
    _write_balance_series(
        temp_db,
        [(chequing_id, start_date + timedelta(days=i), Decimal("0.00")) for i in range(31)],
    )

    scorer = FinancialHealthScorer(temp_db)
    score = scorer.compute_daily_score(FROZEN_YESTERDAY)
    assert score is not None

    # Expected renormalized composite from the three assessable sub-scores only.
    # The scorer exposes the two 0-100 inputs it did NOT renormalize away
    # (cashflow_health, spending_consistency) directly on the score; the savings
    # sub-score is derived from the same cashflow window the scorer used.
    cashflow_used = {"income": Decimal("200.00") * 30, "expenses": Decimal("50.00") * 30}
    savings_sub = scorer._compute_savings_rate(cashflow_used)
    weights = [
        (Decimal("0.30"), score.cashflow_health),
        (Decimal("0.25"), savings_sub),
        (Decimal("0.10"), score.spending_consistency),
    ]
    total_w = sum((w for w, _ in weights), Decimal("0"))
    expected = int(sum((w * s for w, s in weights), Decimal("0")) / total_w)
    assert score.overall_score == expected


def test_debt_ratio_from_relative_series_does_not_force_debt_score_zero(temp_db: Path):
    """A normal mid-window dip that ends the relative series negative must NOT be
    miscounted as debt (which would push debt_ratio up and the debt sub-score to 0
    and tank overall_score). debt_ratio is not assessable -> None, and the debt
    sub-score is excluded entirely rather than dragging the score down."""
    start_date = FROZEN_TODAY - timedelta(days=30)
    transactions = create_test_transactions(
        start_date=start_date,
        days=30,
        daily_income=Decimal("200.00"),
        daily_expenses=Decimal("50.00"),
    )
    writer = DatabaseWriter(temp_db)
    writer.write_result(
        ProcessingResult(
            transactions=transactions,
            transfers=[],
            exceptions=[],
            duplicates_removed=0,
            total_scanned=1,
        ),
        Path("/test"),
        "1.0.0",
    )
    seed_accounts_for_existing_rows(temp_db)
    chequing_id = seed_account(temp_db, "TestBank", "1234")
    # Relative series that dips negative and ends negative — looks like "debt" to the
    # naive bucketer even though it is only a mid-window dip below the zero anchor.
    _write_balance_series(
        temp_db,
        [(chequing_id, start_date + timedelta(days=i), Decimal("-500.00")) for i in range(31)],
    )

    scorer = FinancialHealthScorer(temp_db)
    score = scorer.compute_daily_score(FROZEN_YESTERDAY)
    assert score is not None
    assert score.debt_ratio is None
    # Solvent 75%-savings profile is not dragged toward 0 by a spurious debt sub-score.
    assert score.overall_score >= 80


def test_not_assessable_metrics_round_trip_as_null(temp_db: Path):
    """A score with not-assessable (None) emergency/debt metrics must save and reload
    as NULL — the DB column is nullable and get_health_scores keeps it None, never
    coercing to a misleading 0.0."""
    from ledger.query import LedgerQueryService

    start_date = FROZEN_TODAY - timedelta(days=30)
    transactions = create_test_transactions(
        start_date=start_date,
        days=30,
        daily_income=Decimal("200.00"),
        daily_expenses=Decimal("50.00"),
    )
    writer = DatabaseWriter(temp_db)
    writer.write_result(
        ProcessingResult(
            transactions=transactions,
            transfers=[],
            exceptions=[],
            duplicates_removed=0,
            total_scanned=1,
        ),
        Path("/test"),
        "1.0.0",
    )

    scorer = FinancialHealthScorer(temp_db)
    score = scorer.compute_daily_score(FROZEN_YESTERDAY)
    assert score is not None
    assert score.emergency_fund_months is None
    assert scorer.save_score(score) is True

    reloaded = LedgerQueryService(temp_db).get_health_scores(limit=1)
    assert reloaded
    assert reloaded[0]["emergency_fund_months"] is None
    assert reloaded[0]["debt_ratio"] is None


# ---------------------------------------------------------------------------
# The other half of the rule: once statement repair anchors EVERY account with an
# ABSOLUTE balance, emergency_fund_months and debt_ratio must actually compute.
# The refusal above is correct and must survive; a permanent refusal is not.
# ---------------------------------------------------------------------------


def _seed_thirty_day_profile(temp_db: Path) -> tuple[str, str, date]:
    """A 30-day window with a chequing income account and a credit-card spend account.

    Returns (chequing_id, card_id, start_date). Expenses total 1500 and income 6000
    across the window ending FROZEN_YESTERDAY.
    """
    start_date = FROZEN_TODAY - timedelta(days=30)
    transactions = create_test_transactions(
        start_date=start_date,
        days=30,
        daily_income=Decimal("200.00"),
        daily_expenses=Decimal("50.00"),
    )
    writer = DatabaseWriter(temp_db)
    writer.write_result(
        ProcessingResult(
            transactions=transactions,
            transfers=[],
            exceptions=[],
            duplicates_removed=0,
            total_scanned=1,
        ),
        Path("/test"),
        "1.0.0",
    )
    seed_accounts_for_existing_rows(temp_db)
    chequing_id = seed_account(temp_db, "TestBank", "1234")
    card_id = seed_account(temp_db, "TestBank", "5678", account_type="credit_card")
    return chequing_id, card_id, start_date


def _anchor_rows(
    account_id: str,
    start_date: date,
    balance: Decimal,
    days: int = 31,
) -> list[tuple[str, date, Decimal]]:
    return [(account_id, start_date + timedelta(days=i), balance) for i in range(days)]


def test_metrics_compute_once_every_account_carries_an_absolute_anchor(temp_db: Path):
    """The unblock. With a statement-backed absolute row for every active account,
    emergency_fund_months and debt_ratio stop being None and report real figures --
    including the credit card's debt, which no metric could see before anchors existed.

    Signs are Unit H's stored convention: +9000.00 held in chequing, -1500.00 owed on
    the card. 9000/1500 = 6 months of buffer; 1500/9000 = 0.17 debt-to-assets."""
    chequing_id, card_id, start_date = _seed_thirty_day_profile(temp_db)
    _write_balance_series(
        temp_db,
        _anchor_rows(chequing_id, start_date, Decimal("9000.00"))
        + _anchor_rows(card_id, start_date, Decimal("-1500.00")),
        balance_basis="absolute",
    )

    score = FinancialHealthScorer(temp_db).compute_daily_score(FROZEN_YESTERDAY)

    assert score is not None
    assert score.emergency_fund_months == Decimal("6.00")
    assert score.debt_ratio == Decimal("0.17")
    # Both sub-scores now enter the composite; a solvent, low-debt profile scores high.
    assert score.overall_score >= 90


def test_card_debt_reaches_debt_ratio_only_through_an_absolute_anchor(temp_db: Path):
    """The same card, anchored only by the RELATIVE series, must stay invisible: that
    series is seeded at 0 on the first transaction, so its level is a change, not a
    balance. Identical numbers, opposite outcome -- the basis is the whole difference."""
    chequing_id, card_id, start_date = _seed_thirty_day_profile(temp_db)
    _write_balance_series(
        temp_db,
        _anchor_rows(chequing_id, start_date, Decimal("9000.00"))
        + _anchor_rows(card_id, start_date, Decimal("-1500.00")),
        balance_basis="relative",
    )

    score = FinancialHealthScorer(temp_db).compute_daily_score(FROZEN_YESTERDAY)

    assert score is not None
    assert score.emergency_fund_months is None
    assert score.debt_ratio is None


def test_partial_absolute_coverage_still_refuses_to_compute(temp_db: Path):
    """Fail closed on partial coverage: an unanchored account could hold debt the
    aggregate would otherwise silently treat as zero."""
    chequing_id, card_id, start_date = _seed_thirty_day_profile(temp_db)
    _write_balance_series(
        temp_db,
        _anchor_rows(chequing_id, start_date, Decimal("9000.00")),
        balance_basis="absolute",
    )
    _write_balance_series(
        temp_db,
        _anchor_rows(card_id, start_date, Decimal("-1500.00")),
        balance_basis="relative",
    )

    score = FinancialHealthScorer(temp_db).compute_daily_score(FROZEN_YESTERDAY)

    assert score is not None
    assert score.emergency_fund_months is None
    assert score.debt_ratio is None


def test_stored_scores_carry_the_metrics_after_a_full_run(temp_db: Path):
    """compute_recent_scores pre-loads balances once and filters them per day; that
    path must reach the same conclusion as the per-day query, or the stored rows stay
    NULL no matter how many anchors exist."""
    from ledger.query import LedgerQueryService

    chequing_id, card_id, start_date = _seed_thirty_day_profile(temp_db)
    _write_balance_series(
        temp_db,
        _anchor_rows(chequing_id, start_date, Decimal("9000.00"))
        + _anchor_rows(card_id, start_date, Decimal("-1500.00")),
        balance_basis="absolute",
    )

    scorer = FinancialHealthScorer(temp_db)
    computed = scorer.compute_recent_scores(days=3, today=FROZEN_TODAY)

    assert computed == 3
    stored = LedgerQueryService(temp_db).get_health_scores(limit=3)
    assert len(stored) == 3
    assert all(row["emergency_fund_months"] is not None for row in stored)
    assert all(row["debt_ratio"] is not None for row in stored)


def test_a_credit_card_in_credit_is_not_counted_as_debt(temp_db: Path):
    """A positive card balance is a prepayment, not a liability.

    Keying debt on is_credit_card discarded the sign, so an overpaid or refunded
    card scored identically to one owing the same amount.
    """
    scorer = FinancialHealthScorer(temp_db)
    accounts = [
        {"account_id": "chequing", "is_credit_card": False},
        {"account_id": "card", "is_credit_card": True},
    ]

    def _snapshot(card_balance: Decimal) -> list[dict]:
        return [
            {
                "account_id": "chequing",
                "balance_date": FROZEN_YESTERDAY,
                "balance": Decimal("2000.00"),
                "balance_basis": "absolute",
            },
            {
                "account_id": "card",
                "balance_date": FROZEN_YESTERDAY,
                "balance": card_balance,
                "balance_basis": "absolute",
            },
        ]

    owing = scorer._compute_debt_ratio(accounts, _snapshot(Decimal("-800.00")))
    in_credit = scorer._compute_debt_ratio(accounts, _snapshot(Decimal("800.00")))

    assert owing == Decimal("0.40")
    assert in_credit == Decimal("0.00")
    assert owing != in_credit, "the sign of a card balance must change the answer"
