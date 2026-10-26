"""
Test trend aggregation functionality.

Tests pre-computed spending trends for dashboard queries.

Clock discipline: the one test that anchors data relative to "today" uses a
frozen reference date (FROZEN_TODAY) instead of date.today() so it is
deterministic across calendar boundaries.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ledger.analysis import TrendAggregator
from ledger.core import Transaction
from ledger.query.service import LedgerQueryService
from ledger.storage.database import init_database
from ledger.storage.writer import DatabaseWriter

# Frozen reference date used in test_aggregate_recent_days instead of
# date.today() so the test is stable across calendar boundaries.
FROZEN_TODAY = date(2026, 1, 15)


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    """Create a temporary database for testing."""
    db_path = tmp_path / "test_trends.db"
    init_database(db_path)
    return db_path


def create_transactions_for_period(
    start_date: date,
    days: int,
    category: str,
    daily_amount: Decimal,
    merchant: str = "Test Merchant",
) -> list[Transaction]:
    """Helper to create transactions for a date range."""
    transactions = []

    for i in range(days):
        txn_date = start_date + timedelta(days=i)
        day_of_week = txn_date.weekday()
        week_of_year = txn_date.isocalendar()[1]

        txn = Transaction(
            txn_id=f"txn_{category}_{txn_date.isoformat()}_{i}_{merchant}",
            posting_date=txn_date,
            transaction_date=txn_date,
            institution="TestBank",
            account="1234",
            account_type="credit_card",
            flow="expense",
            cashflow_type="expense",
            include_in_cashflow=True,
            currency="CAD",
            amount_raw=daily_amount,
            amount_signed=-daily_amount,
            description=f"{merchant} purchase",
            merchant=merchant,
            category=category,
            merchant_confidence="high",
            category_confidence="high",
            exclude_from_spending=False,
            merchant_raw=merchant,
            category_raw=category,
            source_file="test.csv",
            source_path="/test",
            source_period=txn_date.strftime("%Y-%m"),
            source_row=i,
            imported_at=datetime.now(UTC).replace(tzinfo=None),
            parser_version="1.0.0",
            txn_fingerprint=f"fp_{category}_{i}",
            # Temporal features
            day_of_week=day_of_week,
            day_of_month=txn_date.day,
            week_of_year=week_of_year,
            is_weekend=day_of_week >= 5,
            is_month_start=txn_date.day <= 3,
            is_month_end=txn_date.day >= 28,
        )
        transactions.append(txn)

    return transactions


def _write(temp_db: Path, transactions: list) -> None:
    from ledger.processing import ProcessingResult

    DatabaseWriter(temp_db).write_result(
        ProcessingResult(
            transactions=transactions,
            transfers=[],
            exceptions=[],
            duplicates_removed=0,
            total_scanned=len(transactions),
        ),
        Path("/test"),
        "1.0.0",
    )


def test_new_category_trend_reads_increasing_not_stable(temp_db: Path):
    """A brand-new category (no prior period) that spent is an increase from nothing:
    the absolute change is the full amount and the direction is 'increasing' -- not
    'stable' at 0% (which is what (0, 0) used to produce)."""
    feb = create_transactions_for_period(date(2025, 2, 6), 1, "Travel", Decimal("500"))
    _write(temp_db, feb)

    trends = TrendAggregator(temp_db).compute_monthly_trends(2025, 2)
    travel = next(t for t in trends if t.category == "Travel")

    assert travel.vs_previous_period_abs == Decimal("500")
    assert travel.trend_direction == "increasing"


def test_6m_average_divides_by_full_window(temp_db: Path):
    """The 6-month average divides by the full 6-month window (missing months = $0), so a
    sparse category that spends once reads as a spike, not 'in line with average'."""
    from ledger.storage.database import SpendingTrendDB, session_scope

    with session_scope(temp_db) as session:
        session.add(
            SpendingTrendDB(
                time_period="2026-01",
                period_type="monthly",
                category="Dining",
                transaction_count=1,
                total_spent=Decimal("600.00"),
                avg_transaction=Decimal("600.00"),
                median_transaction=Decimal("600.00"),
                vs_previous_period_pct=Decimal("0"),
                vs_previous_period_abs=Decimal("0"),
                vs_avg_6m_pct=Decimal("0"),
                top_merchant="X",
                top_merchant_amount=Decimal("600.00"),
                trend_direction="stable",
                computed_at=datetime(2026, 1, 31),
            )
        )

    pct = TrendAggregator(temp_db)._compare_to_6m_average(
        "Dining", date(2026, 7, 15), Decimal("300"), "monthly"
    )

    # avg over 6 months = 600/6 = 100; current 300 -> +200% (was 300 vs 600 -> -50%)
    assert pct == Decimal("200")


def test_compute_daily_trend(temp_db: Path):
    """Test computing a daily trend for a single day."""
    # Create transactions for one day
    target_date = date(2025, 12, 15)
    transactions = create_transactions_for_period(
        start_date=target_date,
        days=1,
        category="Dining",
        daily_amount=Decimal("25.00"),
        merchant="Restaurant",
    )

    # Write to database
    writer = DatabaseWriter(temp_db)
    from ledger.processing import ProcessingResult

    result = ProcessingResult(
        transactions=transactions,
        transfers=[],
        exceptions=[],
        duplicates_removed=0,
        total_scanned=1,
    )
    writer.write_result(result, Path("/test"), "1.0.0")

    # Compute trends
    aggregator = TrendAggregator(temp_db)
    trends = aggregator.compute_daily_trends(target_date)

    # Should have one trend for Dining category
    assert len(trends) == 1

    trend = trends[0]
    assert trend.time_period == "2025-12-15"
    assert trend.period_type == "daily"
    assert trend.category == "Dining"
    assert trend.transaction_count == 1
    assert trend.total_spent == Decimal("25.00")
    assert trend.avg_transaction == Decimal("25.00")
    assert trend.median_transaction == Decimal("25.00")
    assert trend.top_merchant == "Restaurant"
    assert trend.top_merchant_amount == Decimal("25.00")


def test_daily_trends_exclude_income(temp_db: Path):
    """Income transactions must not be counted as spending in trends."""
    from ledger.processing import ProcessingResult

    target_date = date(2025, 12, 15)
    expense = create_transactions_for_period(
        start_date=target_date,
        days=1,
        category="Dining",
        daily_amount=Decimal("25.00"),
        merchant="Restaurant",
    )
    income = Transaction(
        txn_id="txn_income_1",
        txn_fingerprint="fp_income_1",
        posting_date=target_date,
        transaction_date=target_date,
        institution="TestBank",
        account="1234",
        account_type="chequing",
        flow="income",
        cashflow_type="income",
        include_in_cashflow=True,
        currency="CAD",
        amount_raw=Decimal("5000.00"),
        amount_signed=Decimal("5000.00"),
        description="PAYROLL DEPOSIT",
        merchant="EMPLOYER",
        category="Salary",
        exclude_from_spending=False,
        merchant_raw="",
        category_raw="Salary",
        source_file="test.csv",
        source_path="/test",
        source_period=target_date.strftime("%Y-%m"),
        source_row=99,
        imported_at=datetime.now(UTC).replace(tzinfo=None),
        parser_version="1.0.0",
    )

    writer = DatabaseWriter(temp_db)
    writer.write_result(
        ProcessingResult(
            transactions=[*expense, income],
            transfers=[],
            exceptions=[],
            duplicates_removed=0,
            total_scanned=2,
        ),
        Path("/test"),
        "1.0.0",
    )

    trends = TrendAggregator(temp_db).compute_daily_trends(target_date)
    categories = {t.category for t in trends}

    assert "Salary" not in categories  # income excluded from spending trends
    assert "Dining" in categories


def test_compute_monthly_trend(temp_db: Path):
    """Test computing a monthly trend."""
    # Create transactions for December 2025
    start_date = date(2025, 12, 1)
    transactions = []

    # Add 10 dining transactions
    for i in range(10):
        txns = create_transactions_for_period(
            start_date=start_date + timedelta(days=i * 3),
            days=1,
            category="Dining",
            daily_amount=Decimal("30.00"),
            merchant=f"Restaurant {i % 3}",  # 3 different restaurants
        )
        transactions.extend(txns)

    # Write to database
    writer = DatabaseWriter(temp_db)
    from ledger.processing import ProcessingResult

    result = ProcessingResult(
        transactions=transactions,
        transfers=[],
        exceptions=[],
        duplicates_removed=0,
        total_scanned=1,
    )
    writer.write_result(result, Path("/test"), "1.0.0")

    # Compute monthly trends
    aggregator = TrendAggregator(temp_db)
    trends = aggregator.compute_monthly_trends(2025, 12)

    assert len(trends) == 1

    trend = trends[0]
    assert trend.time_period == "2025-12"
    assert trend.period_type == "monthly"
    assert trend.category == "Dining"
    assert trend.transaction_count == 10
    assert trend.total_spent == Decimal("300.00")  # 10 * $30
    assert trend.avg_transaction == Decimal("30.00")


def test_multiple_categories(temp_db: Path):
    """Test trends with multiple categories."""
    target_date = date(2025, 12, 15)

    # Create transactions in 3 categories
    dining_txns = create_transactions_for_period(
        start_date=target_date,
        days=1,
        category="Dining",
        daily_amount=Decimal("25.00"),
    )

    groceries_txns = create_transactions_for_period(
        start_date=target_date,
        days=1,
        category="Groceries",
        daily_amount=Decimal("50.00"),
    )

    entertainment_txns = create_transactions_for_period(
        start_date=target_date,
        days=1,
        category="Entertainment",
        daily_amount=Decimal("15.00"),
    )

    all_txns = dining_txns + groceries_txns + entertainment_txns

    # Write to database
    writer = DatabaseWriter(temp_db)
    from ledger.processing import ProcessingResult

    result = ProcessingResult(
        transactions=all_txns,
        transfers=[],
        exceptions=[],
        duplicates_removed=0,
        total_scanned=1,
    )
    writer.write_result(result, Path("/test"), "1.0.0")

    # Compute trends
    aggregator = TrendAggregator(temp_db)
    trends = aggregator.compute_daily_trends(target_date)

    # Should have 3 trends
    assert len(trends) == 3

    categories = {t.category for t in trends}
    assert categories == {"Dining", "Groceries", "Entertainment"}


def test_trend_persistence(temp_db: Path):
    """Test saving and retrieving trends from database."""
    target_date = date(2025, 12, 15)
    transactions = create_transactions_for_period(
        start_date=target_date,
        days=1,
        category="Dining",
        daily_amount=Decimal("25.00"),
    )

    # Write transactions
    writer = DatabaseWriter(temp_db)
    from ledger.processing import ProcessingResult

    result = ProcessingResult(
        transactions=transactions,
        transfers=[],
        exceptions=[],
        duplicates_removed=0,
        total_scanned=1,
    )
    writer.write_result(result, Path("/test"), "1.0.0")

    # Compute and save trends
    aggregator = TrendAggregator(temp_db)
    trends = aggregator.compute_daily_trends(target_date)
    saved_count = aggregator.save_trends(
        trends,
        replacement_scope=("daily", target_date.isoformat()),
    )

    assert saved_count == 1

    # Retrieve from database using QueryService
    query_service = LedgerQueryService(temp_db)
    retrieved_trends = query_service.get_daily_trends(target_date, target_date)

    assert len(retrieved_trends) == 1
    retrieved = retrieved_trends[0]
    assert retrieved["time_period"] == "2025-12-15"
    assert retrieved["category"] == "Dining"
    assert retrieved["total_spent"] == 25.00


def test_trend_comparison_to_previous_period(temp_db: Path):
    """Test trend comparison to previous period."""
    # Create transactions for two consecutive months
    # December: $300
    dec_start = date(2025, 12, 1)
    dec_txns = []
    for i in range(10):
        txns = create_transactions_for_period(
            start_date=dec_start + timedelta(days=i * 3),
            days=1,
            category="Dining",
            daily_amount=Decimal("30.00"),
        )
        dec_txns.extend(txns)

    # January: $450 (50% increase)
    jan_start = date(2026, 1, 1)
    jan_txns = []
    for i in range(10):
        txns = create_transactions_for_period(
            start_date=jan_start + timedelta(days=i * 3),
            days=1,
            category="Dining",
            daily_amount=Decimal("45.00"),
        )
        jan_txns.extend(txns)

    all_txns = dec_txns + jan_txns

    # Write to database
    writer = DatabaseWriter(temp_db)
    from ledger.processing import ProcessingResult

    result = ProcessingResult(
        transactions=all_txns,
        transfers=[],
        exceptions=[],
        duplicates_removed=0,
        total_scanned=1,
    )
    writer.write_result(result, Path("/test"), "1.0.0")

    # Compute and save December trend first
    aggregator = TrendAggregator(temp_db)
    dec_trends = aggregator.compute_monthly_trends(2025, 12)
    aggregator.save_trends(
        dec_trends,
        replacement_scope=("monthly", "2025-12"),
    )

    # Then compute January trend (should compare to December)
    jan_trends = aggregator.compute_monthly_trends(2026, 1)

    jan_trend = jan_trends[0]
    assert jan_trend.category == "Dining"
    assert jan_trend.total_spent == Decimal("450.00")

    # Should show 50% increase
    assert jan_trend.vs_previous_period_abs == Decimal("150.00")
    assert jan_trend.vs_previous_period_pct == Decimal("50.00")
    assert jan_trend.trend_direction == "increasing"


def test_aggregate_recent_days(temp_db: Path):
    """Test aggregating trends for recent days."""
    # Create transactions for last 7 days relative to frozen reference date.
    transactions = []

    for i in range(7):
        txn_date = FROZEN_TODAY - timedelta(days=i)
        txns = create_transactions_for_period(
            start_date=txn_date,
            days=1,
            category="Dining",
            daily_amount=Decimal("25.00"),
        )
        transactions.extend(txns)

    # Write to database
    writer = DatabaseWriter(temp_db)
    from ledger.processing import ProcessingResult

    result = ProcessingResult(
        transactions=transactions,
        transfers=[],
        exceptions=[],
        duplicates_removed=0,
        total_scanned=1,
    )
    writer.write_result(result, Path("/test"), "1.0.0")

    # Aggregate recent 7 days relative to the frozen reference date.
    aggregator = TrendAggregator(temp_db)
    total_trends = aggregator.aggregate_recent_days(days=7, today=FROZEN_TODAY)

    # Should have computed 7 days of trends (1 category per day)
    assert total_trends == 7


def test_top_merchant_calculation(temp_db: Path):
    """Test that top merchant is correctly identified."""
    target_date = date(2025, 12, 15)
    transactions = []

    # 3 transactions at Restaurant A ($10 each = $30 total)
    for _i in range(3):
        txns = create_transactions_for_period(
            start_date=target_date,
            days=1,
            category="Dining",
            daily_amount=Decimal("10.00"),
            merchant="Restaurant A",
        )
        transactions.extend(txns)

    # 1 transaction at Restaurant B ($50 total)
    txns = create_transactions_for_period(
        start_date=target_date,
        days=1,
        category="Dining",
        daily_amount=Decimal("50.00"),
        merchant="Restaurant B",
    )
    transactions.extend(txns)

    # Write to database
    writer = DatabaseWriter(temp_db)
    from ledger.processing import ProcessingResult

    result = ProcessingResult(
        transactions=transactions,
        transfers=[],
        exceptions=[],
        duplicates_removed=0,
        total_scanned=1,
    )
    writer.write_result(result, Path("/test"), "1.0.0")

    # Compute trends
    aggregator = TrendAggregator(temp_db)
    trends = aggregator.compute_daily_trends(target_date)

    trend = trends[0]
    # Restaurant B should be top merchant (highest total)
    assert trend.top_merchant == "Restaurant B"
    assert trend.top_merchant_amount == Decimal("50.00")


def test_median_calculation(temp_db: Path):
    """Test median transaction calculation."""
    target_date = date(2025, 12, 15)
    transactions = []

    # Create transactions with different amounts
    amounts = [
        Decimal("10.00"),
        Decimal("20.00"),
        Decimal("30.00"),
        Decimal("40.00"),
        Decimal("50.00"),
    ]
    for i, amount in enumerate(amounts):
        txns = create_transactions_for_period(
            start_date=target_date,
            days=1,
            category="Shopping",
            daily_amount=amount,
            merchant=f"Store {i}",
        )
        transactions.extend(txns)

    # Write to database
    writer = DatabaseWriter(temp_db)
    from ledger.processing import ProcessingResult

    result = ProcessingResult(
        transactions=transactions,
        transfers=[],
        exceptions=[],
        duplicates_removed=0,
        total_scanned=1,
    )
    writer.write_result(result, Path("/test"), "1.0.0")

    # Compute trends
    aggregator = TrendAggregator(temp_db)
    trends = aggregator.compute_daily_trends(target_date)

    trend = trends[0]
    # Median of [10, 20, 30, 40, 50] = 30
    assert trend.median_transaction == Decimal("30.00")


def test_query_service_get_trending_categories(temp_db: Path):
    """Test QueryService method to get trending categories."""
    # Create transactions with different growth rates
    # Dining: Growing quickly (+50%)
    dining_txns = []
    for month, multiplier in [(11, 1.0), (12, 1.5)]:
        for i in range(10):
            txns = create_transactions_for_period(
                start_date=date(2025, month, 1) + timedelta(days=i * 3),
                days=1,
                category="Dining",
                daily_amount=Decimal(f"{30.00 * multiplier:.2f}"),
            )
            dining_txns.extend(txns)

    # Groceries: Stable (0%)
    groceries_txns = []
    for month in [11, 12]:
        for i in range(10):
            txns = create_transactions_for_period(
                start_date=date(2025, month, 1) + timedelta(days=i * 3),
                days=1,
                category="Groceries",
                daily_amount=Decimal("100.00"),
            )
            groceries_txns.extend(txns)

    all_txns = dining_txns + groceries_txns

    # Write to database
    writer = DatabaseWriter(temp_db)
    from ledger.processing import ProcessingResult

    result = ProcessingResult(
        transactions=all_txns,
        transfers=[],
        exceptions=[],
        duplicates_removed=0,
        total_scanned=1,
    )
    writer.write_result(result, Path("/test"), "1.0.0")

    # Compute trends
    aggregator = TrendAggregator(temp_db)
    for month in [11, 12]:
        trends = aggregator.compute_monthly_trends(2025, month)
        aggregator.save_trends(
            trends,
            replacement_scope=("monthly", f"2025-{month:02d}"),
        )

    # Get trending categories
    query_service = LedgerQueryService(temp_db)
    trending = query_service.get_trending_categories(period_type="monthly", limit=5)

    # Dining should be first (higher % change)
    assert len(trending) >= 2
    assert trending[0]["category"] == "Dining"
    assert abs(trending[0]["vs_previous_period_pct"]) > 0


def test_empty_day_no_trends(temp_db: Path):
    """Test that empty days produce no trends."""
    target_date = date(2025, 12, 15)

    # No transactions for this day
    aggregator = TrendAggregator(temp_db)
    trends = aggregator.compute_daily_trends(target_date)

    # Should return empty list
    assert trends == []
