"""
Test pattern detection functionality.

Tests recurring pattern detection and matching.
"""

import calendar
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from account_fixtures import seed_account, seed_accounts_for_existing_rows

from ledger.analysis import PatternMatcher, RecurringPatternDetector
from ledger.analysis.runner import AnalysisRunner
from ledger.core import Transaction
from ledger.core.account_identity import account_identity
from ledger.processing import ProcessingResult
from ledger.storage.database import RecurringPatternDB, TransactionDB, init_database, session_scope
from ledger.storage.writer import DatabaseWriter


def add_months(base_date: date, months: int, day_of_month: int | None = None) -> date:
    """Return base_date shifted by whole months, clamping the day when needed."""
    target_month = base_date.month + months
    target_year = base_date.year + (target_month - 1) // 12
    target_month = ((target_month - 1) % 12) + 1
    target_day = day_of_month if day_of_month is not None else base_date.day
    last_day = calendar.monthrange(target_year, target_month)[1]
    return date(target_year, target_month, min(target_day, last_day))


def recent_monthly_start(day_of_month: int, months: int) -> date:
    """Return a start date whose final occurrence is recent enough to be active."""
    today = datetime.now(UTC).date()
    latest_offset = 0 if today.day >= day_of_month else -1
    latest_occurrence = add_months(today.replace(day=1), latest_offset, day_of_month)
    return add_months(latest_occurrence, -(months - 1), day_of_month)


def recent_weekly_start(weekday: int, weeks: int) -> date:
    """Return a start date whose final weekly occurrence is the latest weekday."""
    today = datetime.now(UTC).date()
    days_since_weekday = (today.weekday() - weekday) % 7
    latest_occurrence = today - timedelta(days=days_since_weekday)
    return latest_occurrence - timedelta(weeks=weeks - 1)


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    """Create a temporary database for testing."""
    db_path = tmp_path / "test_patterns.db"
    init_database(db_path)
    # Every fixture transaction below is TestBank/1234; recurring_patterns.account_id
    # references accounts.account_id, so the parent row has to exist.
    seed_account(db_path, "TestBank", "1234", account_type="credit_card", is_credit_card=True)
    return db_path


def create_monthly_subscription(
    merchant: str,
    amount: Decimal,
    start_date: date,
    months: int = 6,
    day_of_month: int | None = None,
) -> list[Transaction]:
    """Create a monthly subscription pattern."""
    transactions = []

    # Use start_date's day if not specified
    if day_of_month is None:
        day_of_month = start_date.day

    for i in range(months):
        # Calculate posting date (same day each month)
        current_month = start_date.month + i
        current_year = start_date.year + (current_month - 1) // 12
        current_month = ((current_month - 1) % 12) + 1

        posting_date = date(current_year, current_month, day_of_month)

        # Compute temporal features
        day_of_week = posting_date.weekday()
        week_of_year = posting_date.isocalendar()[1]

        txn = Transaction(
            txn_id=f"sub_{merchant}_{i}",
            posting_date=posting_date,
            transaction_date=posting_date,
            institution="TestBank",
            account="1234",
            account_type="credit_card",
            flow="expense",
            cashflow_type="expense",
            include_in_cashflow=True,
            currency="CAD",
            amount_raw=amount,
            amount_signed=-amount,
            description=f"{merchant} Subscription",
            merchant=merchant,
            category="Subscription",
            merchant_confidence="high",
            category_confidence="high",
            exclude_from_spending=False,
            merchant_raw=merchant,
            category_raw="Subscription",
            source_file="test.csv",
            source_path="/test",
            source_period="2025-01",
            source_row=i,
            imported_at=datetime.now(UTC).replace(tzinfo=None),
            parser_version="1.0.0",
            txn_fingerprint=f"fp_{merchant}_{i}",
            # Temporal features
            day_of_week=day_of_week,
            day_of_month=posting_date.day,
            week_of_year=week_of_year,
            is_weekend=day_of_week >= 5,
            is_month_start=posting_date.day <= 3,
            is_month_end=posting_date.day >= 28,
        )
        transactions.append(txn)

    return transactions


def create_weekly_subscription(
    merchant: str,
    amount: Decimal,
    start_date: date,
    weeks: int = 12,
) -> list[Transaction]:
    """Create a weekly subscription pattern."""
    transactions = []

    for i in range(weeks):
        posting_date = start_date + timedelta(weeks=i)

        # Compute temporal features
        day_of_week = posting_date.weekday()
        week_of_year = posting_date.isocalendar()[1]

        txn = Transaction(
            txn_id=f"weekly_{merchant}_{i}",
            posting_date=posting_date,
            transaction_date=posting_date,
            institution="TestBank",
            account="1234",
            account_type="credit_card",
            flow="expense",
            cashflow_type="expense",
            include_in_cashflow=True,
            currency="CAD",
            amount_raw=amount,
            amount_signed=-amount,
            description=f"{merchant} Weekly",
            merchant=merchant,
            category="Service",
            merchant_confidence="high",
            category_confidence="high",
            exclude_from_spending=False,
            merchant_raw=merchant,
            category_raw="Service",
            source_file="test.csv",
            source_path="/test",
            source_period="2025-01",
            source_row=i,
            imported_at=datetime.now(UTC).replace(tzinfo=None),
            parser_version="1.0.0",
            txn_fingerprint=f"fp_weekly_{merchant}_{i}",
            # Temporal features
            day_of_week=day_of_week,
            day_of_month=posting_date.day,
            week_of_year=week_of_year,
            is_weekend=day_of_week >= 5,
            is_month_start=posting_date.day <= 3,
            is_month_end=posting_date.day >= 28,
        )
        transactions.append(txn)

    return transactions


def test_next_occurrence_clamps_to_real_month_end():
    """A day-31 biller must land on the target month's real last day, not always
    the 28th (regression: monthly prediction did replace(day=min(expected_day, 28)))."""
    from ledger.analysis.pattern_detector import next_occurrence

    assert next_occurrence(date(2025, 8, 31), "monthly", 31) == date(2025, 9, 30)
    assert next_occurrence(date(2024, 1, 31), "monthly", 31) == date(2024, 2, 29)  # leap
    assert next_occurrence(date(2025, 1, 31), "monthly", 31) == date(2025, 2, 28)  # non-leap
    assert next_occurrence(date(2025, 1, 15), "monthly", 15) == date(2025, 2, 15)
    assert next_occurrence(date(2025, 12, 15), "monthly", 15) == date(2026, 1, 15)  # year roll
    assert next_occurrence(date(2025, 1, 10), "weekly", 10) == date(2025, 1, 17)


def test_detect_monthly_pattern(temp_db: Path):
    """Test detection of monthly subscription pattern."""
    # Create Netflix-like subscription: $15.99/month on the 15th
    # Use recent dates so pattern is considered active
    transactions = create_monthly_subscription(
        merchant="Netflix",
        amount=Decimal("15.99"),
        start_date=recent_monthly_start(day_of_month=15, months=6),
        months=6,
        day_of_month=15,
    )

    detector = RecurringPatternDetector(temp_db, min_occurrences=3)
    patterns = detector.detect_patterns(transactions)

    # Should detect exactly 1 pattern
    assert len(patterns) == 1

    pattern = patterns[0]

    # Verify pattern properties
    assert pattern.merchant == "Netflix"
    assert pattern.frequency == "monthly"
    assert pattern.expected_day_of_period == 15
    assert pattern.is_fixed_amount is True  # Amount should be consistent
    assert abs(pattern.expected_amount - Decimal("15.99")) < Decimal("0.01")
    assert pattern.confidence > 0.6  # Require >60% confidence
    assert pattern.occurrence_count == 6
    assert pattern.pattern_type == "subscription"
    assert pattern.is_active is True


def test_detect_weekly_pattern(temp_db: Path):
    """Test detection of weekly pattern."""
    # Create weekly coffee subscription: $5.00/week
    transactions = create_weekly_subscription(
        merchant="Coffee Club",
        amount=Decimal("5.00"),
        start_date=recent_weekly_start(weekday=0, weeks=8),  # Monday
        weeks=8,
    )

    detector = RecurringPatternDetector(temp_db, min_occurrences=3)
    patterns = detector.detect_patterns(transactions)

    # Should detect exactly 1 pattern
    assert len(patterns) == 1

    pattern = patterns[0]

    # Verify pattern properties
    assert pattern.merchant == "Coffee Club"
    assert pattern.frequency == "weekly"
    assert pattern.expected_day_of_period == 0  # Monday
    assert abs(pattern.expected_amount - Decimal("5.00")) < Decimal("0.01")
    assert pattern.confidence > 0.6
    assert pattern.occurrence_count == 8


def test_no_pattern_with_insufficient_occurrences(temp_db: Path):
    """Test that patterns are not detected with too few occurrences."""
    # Create only 2 transactions (below min_occurrences=3)
    transactions = create_monthly_subscription(
        merchant="NewService",
        amount=Decimal("9.99"),
        start_date=recent_monthly_start(day_of_month=1, months=2),
        months=2,
    )

    detector = RecurringPatternDetector(temp_db, min_occurrences=3)
    patterns = detector.detect_patterns(transactions)

    # Should detect no patterns
    assert len(patterns) == 0


def test_detect_multiple_patterns(temp_db: Path):
    """Test detection of multiple different patterns."""
    # Create Netflix subscription (15th of each month)
    netflix = create_monthly_subscription(
        merchant="Netflix",
        amount=Decimal("15.99"),
        start_date=recent_monthly_start(day_of_month=15, months=6),
        months=6,
    )

    # Create Spotify subscription (1st of each month)
    spotify = create_monthly_subscription(
        merchant="Spotify",
        amount=Decimal("10.99"),
        start_date=recent_monthly_start(day_of_month=1, months=6),
        months=6,
    )

    all_transactions = netflix + spotify

    detector = RecurringPatternDetector(temp_db, min_occurrences=3)
    patterns = detector.detect_patterns(all_transactions)

    # Should detect 2 patterns
    assert len(patterns) == 2

    # Find each pattern
    netflix_pattern = next(p for p in patterns if p.merchant == "Netflix")
    spotify_pattern = next(p for p in patterns if p.merchant == "Spotify")

    assert netflix_pattern.expected_day_of_period == 15
    assert spotify_pattern.expected_day_of_period == 1


def test_save_patterns_to_database(temp_db: Path):
    """Test saving patterns to database."""
    # Create pattern
    transactions = create_monthly_subscription(
        merchant="Disney Plus",
        amount=Decimal("11.99"),
        start_date=recent_monthly_start(day_of_month=1, months=5),
        months=5,
    )

    detector = RecurringPatternDetector(temp_db, min_occurrences=3)
    patterns = detector.detect_patterns(transactions)

    # Save to database
    saved_count = detector.save_patterns(patterns)
    assert saved_count == 1

    # Verify in database
    with session_scope(temp_db) as session:
        db_patterns = session.query(RecurringPatternDB).all()
        assert len(db_patterns) == 1

        db_pattern = db_patterns[0]
        assert db_pattern.merchant == "Disney Plus"
        assert db_pattern.frequency == "monthly"
        assert db_pattern.occurrence_count == 5


def test_save_patterns_replaces_stale_rows_on_recompute(temp_db: Path):
    """Re-running detection must REPLACE the prior set, not accumulate. The pattern_id is
    derived from the amount/frequency/day, so a price change mints a new id; a plain
    upsert-by-id left the old row orphaned and duplicates piled up every recompute."""
    detector = RecurringPatternDetector(temp_db, min_occurrences=3)

    first = create_monthly_subscription(
        "NETFLIX",
        Decimal("15.99"),
        recent_monthly_start(day_of_month=15, months=6),
        months=6,
        day_of_month=15,
    )
    detector.save_patterns(detector.detect_patterns(first))

    # A price change shifts the average amount -> a DIFFERENT pattern_id for the same biller.
    second = create_monthly_subscription(
        "NETFLIX",
        Decimal("17.99"),
        recent_monthly_start(day_of_month=15, months=6),
        months=6,
        day_of_month=15,
    )
    detector.save_patterns(detector.detect_patterns(second))

    with session_scope(temp_db) as session:
        rows = session.query(RecurringPatternDB).filter_by(merchant="NETFLIX").all()
        assert len(rows) == 1  # the stale $15.99 row must be gone, not orphaned alongside
        assert abs(rows[0].expected_amount - Decimal("17.99")) < Decimal("0.01")


def test_pattern_matcher(temp_db: Path):
    """Test matching new transactions to existing patterns."""
    # Create initial pattern
    transactions = create_monthly_subscription(
        merchant="Netflix",
        amount=Decimal("15.99"),
        start_date=recent_monthly_start(day_of_month=15, months=5),
        months=5,
    )

    # Detect and save pattern
    detector = RecurringPatternDetector(temp_db, min_occurrences=3)
    patterns = detector.detect_patterns(transactions)
    detector.save_patterns(patterns)

    # Create a new transaction that should match
    new_date = add_months(transactions[-1].posting_date, 1, 15)
    day_of_week = new_date.weekday()
    week_of_year = new_date.isocalendar()[1]

    new_txn = Transaction(
        txn_id="new_netflix_txn",
        posting_date=new_date,
        transaction_date=new_date,
        institution="TestBank",
        account="1234",
        account_type="credit_card",
        flow="expense",
        cashflow_type="expense",
        include_in_cashflow=True,
        currency="CAD",
        amount_raw=Decimal("15.99"),
        amount_signed=Decimal("-15.99"),
        description="Netflix Subscription",
        merchant="Netflix",
        category="Subscription",
        merchant_confidence="high",
        category_confidence="high",
        exclude_from_spending=False,
        merchant_raw="Netflix",
        category_raw="Subscription",
        source_file="test.csv",
        source_path="/test",
        source_period="2024-12",
        source_row=999,
        imported_at=datetime.now(UTC).replace(tzinfo=None),
        parser_version="1.0.0",
        txn_fingerprint="fp_new_netflix",
        # Temporal features
        day_of_week=day_of_week,
        day_of_month=new_date.day,
        week_of_year=week_of_year,
        is_weekend=day_of_week >= 5,
        is_month_start=new_date.day <= 3,
        is_month_end=new_date.day >= 28,
    )

    # Try to match
    matcher = PatternMatcher(temp_db)
    pattern_id = matcher.match_transaction_to_pattern(new_txn)

    # Should find a match
    assert pattern_id is not None
    assert pattern_id == patterns[0].pattern_id


def test_pattern_matcher_no_match_wrong_amount(temp_db: Path):
    """Test that matcher rejects transactions with wrong amount."""
    # Create initial pattern
    transactions = create_monthly_subscription(
        merchant="Netflix",
        amount=Decimal("15.99"),
        start_date=recent_monthly_start(day_of_month=15, months=5),
        months=5,
    )

    detector = RecurringPatternDetector(temp_db, min_occurrences=3)
    patterns = detector.detect_patterns(transactions)
    detector.save_patterns(patterns)

    # Create transaction with very different amount
    new_date = add_months(transactions[-1].posting_date, 1, 15)
    day_of_week = new_date.weekday()
    week_of_year = new_date.isocalendar()[1]

    new_txn = Transaction(
        txn_id="wrong_amount_txn",
        posting_date=new_date,
        transaction_date=new_date,
        institution="TestBank",
        account="1234",
        account_type="credit_card",
        flow="expense",
        cashflow_type="expense",
        include_in_cashflow=True,
        currency="CAD",
        amount_raw=Decimal("99.99"),  # Very different amount
        amount_signed=Decimal("-99.99"),
        description="Netflix Subscription",
        merchant="Netflix",
        category="Subscription",
        merchant_confidence="high",
        category_confidence="high",
        exclude_from_spending=False,
        merchant_raw="Netflix",
        category_raw="Subscription",
        source_file="test.csv",
        source_path="/test",
        source_period="2024-12",
        source_row=999,
        imported_at=datetime.now(UTC).replace(tzinfo=None),
        parser_version="1.0.0",
        txn_fingerprint="fp_wrong_amount",
        # Temporal features
        day_of_week=day_of_week,
        day_of_month=new_date.day,
        week_of_year=week_of_year,
        is_weekend=day_of_week >= 5,
        is_month_start=new_date.day <= 3,
        is_month_end=new_date.day >= 28,
    )

    # Try to match
    matcher = PatternMatcher(temp_db)
    pattern_id = matcher.match_transaction_to_pattern(new_txn)

    # Should NOT find a match (amount too different)
    assert pattern_id is None


def test_micro_amount_stream_is_not_a_pattern(temp_db: Path):
    """Sub-dollar rounding noise (e.g. $0.01-$0.03 deposit-interest corrections) must not
    be certified as a recurring pattern -- it pollutes the bills/subscriptions list."""
    transactions = []
    for i in range(5):
        transactions.extend(
            create_monthly_subscription(
                merchant="HARBOR DEPOSIT INTEREST",
                amount=Decimal("0.02"),
                start_date=add_months(recent_monthly_start(day_of_month=1, months=5), i, 1),
                months=1,
                day_of_month=1,
            )
        )

    detector = RecurringPatternDetector(temp_db, min_occurrences=3)
    patterns = detector.detect_patterns(transactions)

    assert all(p.merchant != "HARBOR DEPOSIT INTEREST" for p in patterns)


def test_weekly_pattern_needs_more_than_three_occurrences(temp_db: Path):
    """Three sporadic same-merchant charges that happen to land ~7 days apart (gas
    fill-ups) must not be certified as a 'weekly' bill; a weekly cadence needs >=4
    points. Four IS enough, so the boundary is pinned at 4 (not over-suppressed)."""
    three = create_weekly_subscription(
        merchant="MEADOW MARKET GAS BAR",
        amount=Decimal("45.00"),
        start_date=recent_weekly_start(weekday=2, weeks=3),
        weeks=3,
    )
    detector = RecurringPatternDetector(temp_db, min_occurrences=3)
    assert detector.detect_patterns(three) == []

    four = create_weekly_subscription(
        merchant="CLEANER",
        amount=Decimal("60.00"),
        start_date=recent_weekly_start(weekday=2, weeks=4),
        weeks=4,
    )
    patterns = RecurringPatternDetector(temp_db, min_occurrences=3).detect_patterns(four)
    assert any(p.merchant == "CLEANER" and p.frequency == "weekly" for p in patterns)


def test_pattern_with_variable_amounts(temp_db: Path):
    """Test detection of patterns with slightly variable amounts (e.g., utility bills)."""
    # Create utility bill pattern with ±7% variation (within ±10% clustering tolerance)
    amounts = [
        Decimal("95.00"),  # -5%
        Decimal("105.00"),  # +5%
        Decimal("97.00"),  # -3%
        Decimal("103.00"),  # +3%
        Decimal("93.00"),  # -7%
        Decimal("107.00"),  # +7%
    ]

    transactions = []
    for i, amount in enumerate(amounts):
        txns = create_monthly_subscription(
            merchant="Electric Company",
            amount=amount,
            start_date=add_months(recent_monthly_start(day_of_month=5, months=6), i, 5),
            months=1,
            day_of_month=5,
        )
        transactions.extend(txns)

    detector = RecurringPatternDetector(temp_db, min_occurrences=3)
    patterns = detector.detect_patterns(transactions)

    # Should still detect pattern despite variation
    assert len(patterns) == 1

    pattern = patterns[0]
    assert pattern.merchant == "Electric Company"
    assert pattern.frequency == "monthly"
    # With ±7% variation (CV ~5-6%), should be detected as variable
    assert pattern.is_fixed_amount is False  # Should detect variable amount
    assert pattern.amount_std_dev > Decimal("0")  # Has some variation


def test_variable_amount_bill_is_detected(temp_db: Path):
    """A utility-style monthly bill with WIDELY varying amounts ($189-$380) must be
    detected -- amount variation must not shatter it before the cadence is even checked."""
    amounts = [Decimal(a) for a in ("189", "230", "275", "189", "312", "355", "380")]
    transactions = []
    for i, amount in enumerate(amounts):
        transactions.extend(
            create_monthly_subscription(
                merchant="POWERSTREAM HYDRO",
                amount=amount,
                start_date=add_months(recent_monthly_start(day_of_month=15, months=7), i, 15),
                months=1,
                day_of_month=15,
            )
        )

    detector = RecurringPatternDetector(temp_db, min_occurrences=3)
    patterns = detector.detect_patterns(transactions)

    assert any(p.merchant == "POWERSTREAM HYDRO" and p.frequency == "monthly" for p in patterns)


def test_mixed_amount_stream_isolates_recurring(temp_db: Path):
    """A small monthly subscription mixed with larger one-off purchases at the same
    merchant must still surface the subscription (distinct price points are separated)."""
    transactions = []
    for i in range(6):
        transactions.extend(
            create_monthly_subscription(
                merchant="APPLE",
                amount=Decimal("14.68"),
                start_date=add_months(recent_monthly_start(day_of_month=10, months=6), i, 10),
                months=1,
                day_of_month=10,
            )
        )
    base = recent_monthly_start(day_of_month=10, months=6)
    for amt, off in (("80.00", 3), ("120.00", 51), ("95.00", 77)):
        d = base + timedelta(days=off)
        transactions.extend(
            create_monthly_subscription(
                merchant="APPLE", amount=Decimal(amt), start_date=d, months=1, day_of_month=d.day
            )
        )

    detector = RecurringPatternDetector(temp_db, min_occurrences=3)
    patterns = detector.detect_patterns(transactions)

    assert any(
        p.merchant == "APPLE" and abs(p.expected_amount - Decimal("14.68")) < Decimal("1.00")
        for p in patterns
    )


# ---------------------------------------------------------------------------
# M15 — pattern occurrence_count double-counted on a fresh import
# ---------------------------------------------------------------------------


def _write_transactions(db_path: Path, transactions: list[Transaction]) -> None:
    """Insert Transaction objects into the DB via DatabaseWriter, then seed their accounts."""
    DatabaseWriter(db_path).write_result(
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
    # Detection writes recurring_patterns rows keyed by account; give them their parent.
    seed_accounts_for_existing_rows(db_path)


def test_occurrence_count_not_doubled_on_fresh_import(tmp_path: Path) -> None:
    """Fixed-amount subscription must not be double-counted on a fresh import.

    Bug: save_patterns sets occurrence_count = len(source_txns), then
    match_all_transactions re-matches those same txns (recurring_pattern_id IS NULL)
    and increments the count again. A 6-occurrence pattern shows up as 12.

    Fix: save_patterns marks the source txns' recurring_pattern_id so
    match_all_transactions skips them.
    """
    db = tmp_path / "test.db"
    init_database(db)

    txns = create_monthly_subscription(
        merchant="Netflix",
        amount=Decimal("15.99"),
        start_date=recent_monthly_start(day_of_month=5, months=6),
        months=6,
        day_of_month=5,
    )
    _write_transactions(db, txns)

    results = AnalysisRunner(db).run_pattern_detection()

    assert results["patterns_detected"] == 1

    with session_scope(db) as session:
        pattern = session.query(RecurringPatternDB).filter_by(merchant="Netflix").first()
        assert pattern is not None
        # Must be exactly 6, NOT 12 (the pre-fix double-count value).
        assert pattern.occurrence_count == 6, (
            f"Expected 6 but got {pattern.occurrence_count} — "
            "source txns are being re-matched and double-counted"
        )


def test_non_cad_transaction_cannot_extend_cad_recurring_pattern(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    init_database(db)
    transactions = create_monthly_subscription(
        merchant="CROSSCURRENCY",
        amount=Decimal("20.00"),
        start_date=recent_monthly_start(day_of_month=5, months=4),
        months=4,
        day_of_month=5,
    )
    usd_transaction = transactions[-1].model_copy(update={"currency": "USD"})
    _write_transactions(db, [*transactions[:-1], usd_transaction])

    results = AnalysisRunner(db).run_pattern_detection()

    assert results == {
        "patterns_detected": 1,
        "transactions_matched": 0,
        "patterns_updated": 0,
    }
    with session_scope(db) as session:
        usd_row = session.get(TransactionDB, usd_transaction.txn_id)
        pattern = session.query(RecurringPatternDB).filter_by(merchant="CROSSCURRENCY").one()
        assert usd_row is not None and usd_row.recurring_pattern_id is None
        assert pattern.occurrence_count == 3

    assert PatternMatcher(db).match_transaction_to_pattern(usd_transaction) is None


def test_occurrence_count_idempotent_on_rerun(tmp_path: Path) -> None:
    """Running run_pattern_detection a second time must NOT increase occurrence_count.

    After the first run the source txns carry recurring_pattern_id. The second run
    deletes and re-inserts the pattern (save_patterns rebuilds), then the matcher
    sees the txns still have recurring_pattern_id set and skips them. Count stays 6.
    """
    db = tmp_path / "test.db"
    init_database(db)

    txns = create_monthly_subscription(
        merchant="Spotify",
        amount=Decimal("10.99"),
        start_date=recent_monthly_start(day_of_month=1, months=6),
        months=6,
        day_of_month=1,
    )
    _write_transactions(db, txns)

    runner = AnalysisRunner(db)
    runner.run_pattern_detection()
    runner.run_pattern_detection()  # second run

    with session_scope(db) as session:
        pattern = session.query(RecurringPatternDB).filter_by(merchant="Spotify").first()
        assert pattern is not None
        assert pattern.occurrence_count == 6, (
            f"Expected 6 after two runs but got {pattern.occurrence_count}"
        )
        # All source txns must carry the pattern id
        unlinked = (
            session.query(TransactionDB)
            .filter(
                TransactionDB.merchant == "Spotify",
                TransactionDB.recurring_pattern_id.is_(None),
            )
            .count()
        )
        assert unlinked == 0, f"{unlinked} Spotify txns still have no recurring_pattern_id"


def test_new_transaction_still_matched_after_detection(tmp_path: Path) -> None:
    """A genuinely NEW transaction imported AFTER detection must still be matched.

    Detection marks the 6 source txns. A 7th txn (imported later, recurring_pattern_id
    NULL) must be matched by match_all_transactions and bring the count to 7.
    """
    db = tmp_path / "test.db"
    init_database(db)

    # 6 source txns — same subscription
    start = recent_monthly_start(day_of_month=5, months=6)
    txns = create_monthly_subscription(
        merchant="Disney Plus",
        amount=Decimal("11.99"),
        start_date=start,
        months=6,
        day_of_month=5,
    )
    _write_transactions(db, txns)

    # First detection run — marks the 6 source txns.
    AnalysisRunner(db).run_pattern_detection()

    # Simulate a 7th charge arriving next month.
    seventh_date = add_months(txns[-1].posting_date, 1, 5)
    day_of_week = seventh_date.weekday()
    seventh = Transaction(
        txn_id="disney_7th",
        posting_date=seventh_date,
        transaction_date=seventh_date,
        institution="TestBank",
        account="1234",
        account_type="credit_card",
        flow="expense",
        cashflow_type="expense",
        include_in_cashflow=True,
        currency="CAD",
        amount_raw=Decimal("11.99"),
        amount_signed=Decimal("-11.99"),
        description="Disney Plus Subscription",
        merchant="Disney Plus",
        category="Subscription",
        merchant_confidence="high",
        category_confidence="high",
        exclude_from_spending=False,
        merchant_raw="Disney Plus",
        category_raw="Subscription",
        source_file="test.csv",
        source_path="/test",
        source_period=f"{seventh_date.year}-{seventh_date.month:02d}",
        source_row=6,
        imported_at=datetime.now(UTC).replace(tzinfo=None),
        parser_version="1.0.0",
        txn_fingerprint="fp_disney_7th",
        day_of_week=day_of_week,
        day_of_month=seventh_date.day,
        week_of_year=seventh_date.isocalendar()[1],
        is_weekend=day_of_week >= 5,
        is_month_start=seventh_date.day <= 3,
        is_month_end=seventh_date.day >= 28,
    )
    _write_transactions(db, [seventh])

    # Second detection run picks up all 7 txns and updates the pattern.
    AnalysisRunner(db).run_pattern_detection()

    with session_scope(db) as session:
        pattern = session.query(RecurringPatternDB).filter_by(merchant="Disney Plus").first()
        assert pattern is not None
        assert pattern.occurrence_count == 7, (
            f"Expected 7 after 7th txn imported but got {pattern.occurrence_count}"
        )


def test_no_dangling_pattern_references_after_rebuild(tmp_path: Path) -> None:
    """No transaction may hold a recurring_pattern_id that points at a deleted pattern.

    Scenario: first detection run builds a $15.99/month Netflix pattern (id_A) and stamps
    the 6 source txns with id_A.  Then 4 of those TransactionDB rows are deleted DIRECTLY
    via session_scope (NOT re-imported through the writer, which would reset
    recurring_pattern_id to NULL).  The 2 surviving rows still carry id_A.  A second
    detection run sees only 2 txns for this merchant — below min_occurrences=3 — so no
    pattern forms, and id_A is deleted from recurring_patterns.  Unless save_patterns
    NULLs recurring_pattern_id on the surviving rows BEFORE deleting the pattern, those
    2 rows become dangling references.

    This test FAILS without the clear-before-delete step and PASSES with it.
    """
    db = tmp_path / "test.db"
    init_database(db)

    # --- first run: 6 txns at $15.99, detect+save pattern id_A ---------------
    first_txns = create_monthly_subscription(
        merchant="Netflix",
        amount=Decimal("15.99"),
        start_date=recent_monthly_start(day_of_month=5, months=6),
        months=6,
        day_of_month=5,
    )
    _write_transactions(db, first_txns)
    AnalysisRunner(db).run_pattern_detection()

    # Confirm all 6 source txns carry id_A.
    with session_scope(db) as session:
        first_pattern = session.query(RecurringPatternDB).filter_by(merchant="Netflix").first()
        assert first_pattern is not None, "First run must detect a pattern"
        first_pattern_id = first_pattern.pattern_id
        linked = (
            session.query(TransactionDB)
            .filter(TransactionDB.recurring_pattern_id == first_pattern_id)
            .count()
        )
        assert linked == 6, f"Expected 6 txns linked to id_A, got {linked}"

    # --- destroy 4 of the 6 txn rows directly in the DB (NOT through the writer) ---
    # This preserves recurring_pattern_id=id_A on the 2 survivors.  Re-importing via
    # the writer would reset recurring_pattern_id to NULL and hide the dangling condition.
    with session_scope(db) as session:
        txn_ids_to_delete = [t.txn_id for t in first_txns[:4]]
        session.query(TransactionDB).filter(TransactionDB.txn_id.in_(txn_ids_to_delete)).delete(
            synchronize_session=False
        )

    # Confirm the 2 survivors still carry id_A (the dangling condition is set up).
    with session_scope(db) as session:
        survivors = (
            session.query(TransactionDB)
            .filter(TransactionDB.recurring_pattern_id == first_pattern_id)
            .count()
        )
        assert survivors == 2, f"Expected 2 surviving txns with id_A, got {survivors}"

    # --- second run: only 2 txns remain — below min_occurrences=3 — so no pattern forms,
    # and id_A is deleted.  The clear step must NULL the 2 survivors before deleting id_A.
    AnalysisRunner(db).run_pattern_detection()

    # --- invariant: every non-NULL recurring_pattern_id must resolve ----------
    with session_scope(db) as session:
        existing_pattern_ids = {row.pattern_id for row in session.query(RecurringPatternDB).all()}
        dangling = (
            session.query(TransactionDB)
            .filter(
                TransactionDB.recurring_pattern_id.isnot(None),
                TransactionDB.recurring_pattern_id.notin_(existing_pattern_ids),
            )
            .count()
        )
        assert dangling == 0, (
            f"{dangling} transaction(s) still reference a deleted recurring_pattern_id "
            "(dangling reference left by a rebuild that did not clear txn->pattern links)"
        )


# ---------------------------------------------------------------------------
# N+1 session fix — perf refactor: match_all_transactions must reuse one session
# ---------------------------------------------------------------------------


def test_match_all_session_count_is_constant(tmp_path: Path) -> None:
    """match_all_transactions must open O(1) sessions regardless of unmatched-txn count.

    Pre-fix: match_transaction_to_pattern opens one session_scope per txn + one per
    match (two sessions per matched row, one per unmatched row).  With N=10 unmatched
    txns (all matching a single pattern) that is >=10 extra session opens.

    Post-fix: the bulk path reuses the already-open session, so total session_scope
    calls from match_all_transactions must be exactly 1 (the outer session only).
    """
    import unittest.mock as mock

    from ledger.storage import database as db_module

    db = tmp_path / "session_count.db"
    init_database(db)

    # Build 5 source txns, detect+save pattern (marks them → match_all skips them).
    start = recent_monthly_start(day_of_month=10, months=5)
    source = create_monthly_subscription(
        merchant="Netflix",
        amount=Decimal("15.99"),
        start_date=start,
        months=5,
        day_of_month=10,
    )
    _write_transactions(db, source)
    detector = RecurringPatternDetector(db, min_occurrences=3)
    detector.save_patterns(detector.detect_patterns(source))

    # Now import 10 NEW unmatched txns — none have recurring_pattern_id yet.
    new_txns = []
    last_date = source[-1].posting_date
    for i in range(10):
        d = add_months(last_date, i + 1, 10)
        dow = d.weekday()
        new_txns.append(
            Transaction(
                txn_id=f"new_{i}",
                posting_date=d,
                transaction_date=d,
                institution="TestBank",
                account="1234",
                account_type="credit_card",
                flow="expense",
                cashflow_type="expense",
                include_in_cashflow=True,
                currency="CAD",
                amount_raw=Decimal("15.99"),
                amount_signed=Decimal("-15.99"),
                description="Netflix Subscription",
                merchant="Netflix",
                category="Subscription",
                merchant_confidence="high",
                category_confidence="high",
                exclude_from_spending=False,
                merchant_raw="Netflix",
                category_raw="Subscription",
                source_file="test.csv",
                source_path="/test",
                source_period=f"{d.year}-{d.month:02d}",
                source_row=i,
                imported_at=datetime.now(UTC).replace(tzinfo=None),
                parser_version="1.0.0",
                txn_fingerprint=f"fp_new_{i}",
                day_of_week=dow,
                day_of_month=d.day,
                week_of_year=d.isocalendar()[1],
                is_weekend=dow >= 5,
                is_month_start=d.day <= 3,
                is_month_end=d.day >= 28,
            )
        )
    _write_transactions(db, new_txns)

    # Count session_scope calls during match_all_transactions.
    real_session_scope = db_module.session_scope
    call_count = 0

    def counting_session_scope(path):
        nonlocal call_count
        call_count += 1
        return real_session_scope(path)

    with mock.patch.object(db_module, "session_scope", side_effect=counting_session_scope):
        # Also patch the import inside pattern_matcher so the same counter fires.
        import ledger.analysis.pattern_matcher as pm_module

        with mock.patch.object(pm_module, "session_scope", side_effect=counting_session_scope):
            matcher = PatternMatcher(db)
            result = matcher.match_all_transactions()

    # All 10 new txns should still match (equivalence check).
    assert result["transactions_matched"] == 10, (
        f"Expected 10 matches but got {result['transactions_matched']}"
    )

    # O(1) sessions: exactly 1 (the outer session in match_all_transactions).
    assert call_count == 1, (
        f"Expected 1 session_scope call but got {call_count} — "
        "N+1 session-per-row pattern still present"
    )


def test_match_all_equivalence_multi_pattern(tmp_path: Path) -> None:
    """match_all_transactions refactor must produce IDENTICAL matches + occurrence counts.

    Scenario:
    - Two patterns: Netflix (monthly, $15.99) and Spotify (monthly, $10.99).
    - 4 new unmatched txns for Netflix, 3 for Spotify, 2 that should NOT match.
    - After match_all_transactions:
        * Every txn that should match carries the correct recurring_pattern_id.
        * Unmatched txns have recurring_pattern_id IS NULL.
        * Each pattern's occurrence_count equals base_count + matched_count (no
          double-counting, no under-counting).
        * Amount stats (expected_amount, amount_std_dev) are consistent with
          the running-Welford update applied exactly once per matched txn.
    """
    db = tmp_path / "equiv.db"
    init_database(db)

    # --- Build and save patterns (source txns get marked, skipped by match_all). ---
    n_source = 5
    netflix_start = recent_monthly_start(day_of_month=10, months=n_source)
    netflix_source = create_monthly_subscription(
        merchant="Netflix",
        amount=Decimal("15.99"),
        start_date=netflix_start,
        months=n_source,
        day_of_month=10,
    )
    spotify_start = recent_monthly_start(day_of_month=1, months=n_source)
    spotify_source = create_monthly_subscription(
        merchant="Spotify",
        amount=Decimal("10.99"),
        start_date=spotify_start,
        months=n_source,
        day_of_month=1,
    )

    _write_transactions(db, netflix_source + spotify_source)
    detector = RecurringPatternDetector(db, min_occurrences=3)
    detector.save_patterns(detector.detect_patterns(netflix_source + spotify_source))

    # Record base occurrence counts (should be n_source each).
    with session_scope(db) as s:
        np_row = s.query(RecurringPatternDB).filter_by(merchant="Netflix").first()
        sp_row = s.query(RecurringPatternDB).filter_by(merchant="Spotify").first()
        assert np_row is not None and sp_row is not None
        netflix_pattern_id = np_row.pattern_id
        spotify_pattern_id = sp_row.pattern_id
        netflix_base_count = np_row.occurrence_count
        spotify_base_count = sp_row.occurrence_count

    # --- Import new txns: 4 Netflix, 3 Spotify, 2 non-matching (wrong amount). ---
    n_netflix_new = 4
    n_spotify_new = 3

    new_txns: list[Transaction] = []

    netflix_last = netflix_source[-1].posting_date
    for i in range(n_netflix_new):
        d = add_months(netflix_last, i + 1, 10)
        dow = d.weekday()
        new_txns.append(
            Transaction(
                txn_id=f"nf_{i}",
                posting_date=d,
                transaction_date=d,
                institution="TestBank",
                account="1234",
                account_type="credit_card",
                flow="expense",
                cashflow_type="expense",
                include_in_cashflow=True,
                currency="CAD",
                amount_raw=Decimal("15.99"),
                amount_signed=Decimal("-15.99"),
                description="Netflix Subscription",
                merchant="Netflix",
                category="Subscription",
                merchant_confidence="high",
                category_confidence="high",
                exclude_from_spending=False,
                merchant_raw="Netflix",
                category_raw="Subscription",
                source_file="test.csv",
                source_path="/test",
                source_period=f"{d.year}-{d.month:02d}",
                source_row=100 + i,
                imported_at=datetime.now(UTC).replace(tzinfo=None),
                parser_version="1.0.0",
                txn_fingerprint=f"fp_nf_{i}",
                day_of_week=dow,
                day_of_month=d.day,
                week_of_year=d.isocalendar()[1],
                is_weekend=dow >= 5,
                is_month_start=d.day <= 3,
                is_month_end=d.day >= 28,
            )
        )

    spotify_last = spotify_source[-1].posting_date
    for i in range(n_spotify_new):
        d = add_months(spotify_last, i + 1, 1)
        dow = d.weekday()
        new_txns.append(
            Transaction(
                txn_id=f"sp_{i}",
                posting_date=d,
                transaction_date=d,
                institution="TestBank",
                account="1234",
                account_type="credit_card",
                flow="expense",
                cashflow_type="expense",
                include_in_cashflow=True,
                currency="CAD",
                amount_raw=Decimal("10.99"),
                amount_signed=Decimal("-10.99"),
                description="Spotify Subscription",
                merchant="Spotify",
                category="Subscription",
                merchant_confidence="high",
                category_confidence="high",
                exclude_from_spending=False,
                merchant_raw="Spotify",
                category_raw="Subscription",
                source_file="test.csv",
                source_path="/test",
                source_period=f"{d.year}-{d.month:02d}",
                source_row=200 + i,
                imported_at=datetime.now(UTC).replace(tzinfo=None),
                parser_version="1.0.0",
                txn_fingerprint=f"fp_sp_{i}",
                day_of_week=dow,
                day_of_month=d.day,
                week_of_year=d.isocalendar()[1],
                is_weekend=dow >= 5,
                is_month_start=d.day <= 3,
                is_month_end=d.day >= 28,
            )
        )

    # Two non-matching transactions (wildly wrong amount).
    for i in range(2):
        d = add_months(netflix_last, i + 1, 10)
        dow = d.weekday()
        new_txns.append(
            Transaction(
                txn_id=f"nomatch_{i}",
                posting_date=d,
                transaction_date=d,
                institution="TestBank",
                account="1234",
                account_type="credit_card",
                flow="expense",
                cashflow_type="expense",
                include_in_cashflow=True,
                currency="CAD",
                amount_raw=Decimal("999.00"),
                amount_signed=Decimal("-999.00"),
                description="Random Charge",
                merchant="Netflix",  # same merchant but vastly different amount
                category="Other",
                merchant_confidence="high",
                category_confidence="high",
                exclude_from_spending=False,
                merchant_raw="Netflix",
                category_raw="Other",
                source_file="test.csv",
                source_path="/test",
                source_period=f"{d.year}-{d.month:02d}",
                source_row=300 + i,
                imported_at=datetime.now(UTC).replace(tzinfo=None),
                parser_version="1.0.0",
                txn_fingerprint=f"fp_nomatch_{i}",
                day_of_week=dow,
                day_of_month=d.day,
                week_of_year=d.isocalendar()[1],
                is_weekend=dow >= 5,
                is_month_start=d.day <= 3,
                is_month_end=d.day >= 28,
            )
        )

    _write_transactions(db, new_txns)

    # --- Run match_all_transactions. ---
    matcher = PatternMatcher(db)
    result = matcher.match_all_transactions()

    assert result["transactions_matched"] == n_netflix_new + n_spotify_new, (
        f"Expected {n_netflix_new + n_spotify_new} matched but got {result['transactions_matched']}"
    )

    # --- Verify DB state. ---
    with session_scope(db) as s:
        # Occurrence counts: base + newly matched.
        nf = s.query(RecurringPatternDB).filter_by(pattern_id=netflix_pattern_id).first()
        sp = s.query(RecurringPatternDB).filter_by(pattern_id=spotify_pattern_id).first()
        assert nf is not None and sp is not None

        assert nf.occurrence_count == netflix_base_count + n_netflix_new, (
            f"Netflix count: expected {netflix_base_count + n_netflix_new} "
            f"got {nf.occurrence_count}"
        )
        assert sp.occurrence_count == spotify_base_count + n_spotify_new, (
            f"Spotify count: expected {spotify_base_count + n_spotify_new} "
            f"got {sp.occurrence_count}"
        )

        # Matched txns carry the correct pattern_id.
        netflix_linked = (
            s.query(TransactionDB)
            .filter(
                TransactionDB.merchant == "Netflix",
                TransactionDB.txn_id.like("nf_%"),
                TransactionDB.recurring_pattern_id == netflix_pattern_id,
            )
            .count()
        )
        assert netflix_linked == n_netflix_new, (
            f"Expected {n_netflix_new} Netflix txns linked but got {netflix_linked}"
        )

        spotify_linked = (
            s.query(TransactionDB)
            .filter(
                TransactionDB.merchant == "Spotify",
                TransactionDB.txn_id.like("sp_%"),
                TransactionDB.recurring_pattern_id == spotify_pattern_id,
            )
            .count()
        )
        assert spotify_linked == n_spotify_new, (
            f"Expected {n_spotify_new} Spotify txns linked but got {spotify_linked}"
        )

        # Non-matching txns remain unlinked.
        nomatch_linked = (
            s.query(TransactionDB)
            .filter(
                TransactionDB.txn_id.like("nomatch_%"),
                TransactionDB.recurring_pattern_id.isnot(None),
            )
            .count()
        )
        assert nomatch_linked == 0, f"Expected 0 non-matching txns linked but got {nomatch_linked}"


def _occurrence(
    merchant: str,
    *,
    account: str,
    posting_date: date,
    amount: Decimal,
    index: int,
) -> Transaction:
    """One expense occurrence of `merchant` on `account`, detector-ready."""
    return Transaction(
        txn_id=f"{account}_{merchant}_{index}",
        posting_date=posting_date,
        transaction_date=posting_date,
        institution="TestBank",
        account=account,
        account_type="credit_card",
        flow="expense",
        cashflow_type="expense",
        include_in_cashflow=True,
        currency="CAD",
        amount_raw=amount,
        amount_signed=-amount,
        description=f"{merchant} charge",
        merchant=merchant,
        category="Subscription",
        merchant_confidence="high",
        category_confidence="high",
        exclude_from_spending=False,
        merchant_raw=merchant,
        category_raw="Subscription",
        source_file="test.csv",
        source_path="/test",
        source_period="2025-01",
        source_row=index,
        imported_at=datetime.now(UTC).replace(tzinfo=None),
        parser_version="1.0.0",
        txn_fingerprint=f"fp_{account}_{merchant}_{index}",
        day_of_week=posting_date.weekday(),
        day_of_month=posting_date.day,
        week_of_year=posting_date.isocalendar()[1],
        is_weekend=posting_date.weekday() >= 5,
        is_month_start=posting_date.day <= 3,
        is_month_end=posting_date.day >= 28,
    )


def test_a_merchant_billing_two_accounts_is_two_patterns_one_per_account(temp_db: Path):
    """A merchant billing two accounts is two patterns one per account."""
    start = recent_monthly_start(day_of_month=12, months=8)
    old_card = [
        _occurrence(
            "Netflix",
            account="1111",
            posting_date=add_months(start, offset, 12),
            amount=Decimal("10.00"),
            index=offset,
        )
        for offset in range(5)
    ]
    new_card = [
        _occurrence(
            "Netflix",
            account="2222",
            posting_date=add_months(start, offset, 12),
            amount=Decimal("30.00"),
            index=offset,
        )
        for offset in range(5, 8)
    ]

    patterns = RecurringPatternDetector(temp_db, min_occurrences=3).detect_patterns(
        old_card + new_card
    )

    by_account = {pattern.account_id: pattern for pattern in patterns}
    assert set(by_account) == {
        account_identity("TestBank", "1111"),
        account_identity("TestBank", "2222"),
    }, patterns
    assert by_account[account_identity("TestBank", "1111")].expected_amount == Decimal("10.00")
    assert by_account[account_identity("TestBank", "2222")].expected_amount == Decimal("30.00")


def test_the_account_a_pattern_names_is_the_account_every_occurrence_posted_to(temp_db: Path):
    """`account_id` is taken from the pattern's first transaction, which is only
    a fact about the pattern if every transaction in it shares that account."""
    start = recent_monthly_start(day_of_month=9, months=6)
    rows = [
        _occurrence(
            "Spotify",
            account="1111" if offset < 3 else "2222",
            posting_date=add_months(start, offset, 9),
            amount=Decimal("12.00"),
            index=offset,
        )
        for offset in range(6)
    ]

    patterns = RecurringPatternDetector(temp_db, min_occurrences=3).detect_patterns(rows)

    assert {pattern.account_id for pattern in patterns} == {
        account_identity("TestBank", "1111"),
        account_identity("TestBank", "2222"),
    }
