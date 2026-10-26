"""
Recurring pattern detection using a gap/median heuristic.

Detects subscriptions, bills, income, and other recurring transactions.

Algorithm Enhancements (v2):
- Separate tolerances for income (±25%) vs expenses (±10%)
- Weekend/holiday shift normalization for better timing detection
- More lenient confidence thresholds for income patterns (50% vs 60%)
- Wider frequency ranges for income to handle payroll variations
"""

import calendar
import statistics
from collections import defaultdict
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

from ledger.core import Transaction
from ledger.core.account_identity import account_identity
from ledger.core.analysis_models import RecurringPattern
from ledger.storage.database import RecurringPatternDB, TransactionDB, session_scope
from ledger.utils.dates import naive_utc_now
from ledger.utils.hashing import stable_id
from ledger.utils.money import quantize_cents, to_cents


def _add_calendar_months(base: date, months: int, day: int) -> date:
    """Add ``months`` calendar months to ``base`` and land on ``day``, clamped to the
    target month's real last day (leap-year aware)."""
    month_index = base.month - 1 + months
    year = base.year + month_index // 12
    month = month_index % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day, last_day))


# Occurrences per month for each frequency _classify_frequency can emit. Weekly and
# biweekly use 52/12 and 26/12 rather than 4 and 2: a biweekly bill lands 26 times a
# year, not 24, and treating it as 2/month understates it by 8%.
_MONTHLY_MULTIPLIER: dict[str, Decimal] = {
    "weekly": Decimal(52) / Decimal(12),
    "biweekly": Decimal(26) / Decimal(12),
    "monthly": Decimal(1),
    "quarterly": Decimal(1) / Decimal(3),
    "annual": Decimal(1) / Decimal(12),
}

RECURRING_FREQUENCIES = frozenset(_MONTHLY_MULTIPLIER)

# Pattern types that represent an ongoing COST. Income and transfer patterns are
# detected too, and summing them into a "recurring monthly cost" figure overstates it.
RECURRING_COST_TYPES = frozenset({"subscription", "bill"})


def monthly_equivalent(expected_amount: Decimal | float | str, frequency: str) -> Decimal:
    """Normalize a recurring pattern's amount to a per-month figure.

    Every "Recurring Monthly" KPI used to filter on ``frequency == "monthly"`` and
    drop the rest, so a biweekly mortgage contributed exactly $0 to the user's stated
    fixed costs while a $15 subscription contributed $15. Unknown frequencies return 0
    rather than guessing — ``_classify_frequency`` only persists the five above.
    """
    multiplier = _MONTHLY_MULTIPLIER.get(frequency)
    if multiplier is None:
        return Decimal("0")
    return to_cents(Decimal(str(expected_amount)) * multiplier)


def next_occurrence(last_date: date, frequency: str, expected_day: int) -> date:
    """Predict the next occurrence date for a recurring frequency.

    Monthly/quarterly/annual advance by calendar months and clamp the day to the
    target month's actual last day, so an end-of-month biller lands on 30/29/28 (and
    Feb-29 in leap years) instead of always the 28th. Shared by the detector and the
    matcher so the month-end handling can't drift between them."""
    if frequency == "weekly":
        return last_date + timedelta(days=7)
    if frequency == "biweekly":
        return last_date + timedelta(days=14)
    if frequency == "monthly":
        return _add_calendar_months(last_date, 1, expected_day)
    if frequency == "quarterly":
        return _add_calendar_months(last_date, 3, expected_day)
    if frequency == "annual":
        return _add_calendar_months(last_date, 12, expected_day)
    return last_date + timedelta(days=30)


def previous_occurrence(next_date: date, frequency: str, expected_day: int) -> date:
    """Step one period BACK, exactly inverting :func:`next_occurrence`.

    Needed because ``next_expected_date`` is stored one period after the last
    accepted occurrence: a consumer whose window opens before that stored date
    has to walk backwards to find the occurrences inside it, and doing that by
    subtracting a nominal 30 days would put a monthly biller on a different day
    of the month each step. Kept beside its inverse so the month-end clamp cannot
    drift between the two directions -- both clamp against the pattern's OWN
    ``expected_day``, so ``previous_occurrence`` followed by ``next_occurrence``
    returns the date it started from.
    """
    if frequency == "weekly":
        return next_date - timedelta(days=7)
    if frequency == "biweekly":
        return next_date - timedelta(days=14)
    if frequency == "monthly":
        return _add_calendar_months(next_date, -1, expected_day)
    if frequency == "quarterly":
        return _add_calendar_months(next_date, -3, expected_day)
    if frequency == "annual":
        return _add_calendar_months(next_date, -12, expected_day)
    return next_date - timedelta(days=30)


class RecurringPatternDetector:
    """
    Detect recurring transaction patterns using ML techniques.

    Uses merchant grouping, amount streaming, and periodicity detection.
    """

    # Split a merchant's transactions into separate amount streams only when two amounts
    # differ by more than this ratio (a clear distinct price point), so a variable-but-
    # unimodal biller stays together instead of being shattered by a fixed tolerance.
    AMOUNT_SPLIT_RATIO = Decimal("3.0")

    # Ignore sub-dollar "patterns": $0.01-$0.03 interest-rounding corrections are noise,
    # not a recurring bill or subscription, and only pollute the bills list.
    MIN_PATTERN_AMOUNT = Decimal("1.00")

    # A weekly cadence is only credible with enough points. Three sporadic same-merchant
    # charges that happen to fall ~7 days apart (e.g. gas fill-ups) are a coincidental
    # cluster, not a standing weekly commitment; require >=4 occurrences to call it weekly.
    MIN_WEEKLY_OCCURRENCES = 4

    def __init__(
        self,
        db_path: Path,
        min_occurrences: int = 3,
    ):
        """
        Initialize pattern detector.

        Args:
            db_path: Path to SQLite database
            min_occurrences: Minimum occurrences to consider a pattern (default 3)
        """
        self.db_path = db_path
        self.min_occurrences = min_occurrences

    def detect_patterns(self, transactions: list[Transaction]) -> list[RecurringPattern]:
        """
        Detect recurring patterns from transaction list.

        Algorithm:
        1. Group transactions by ACCOUNT and merchant
        2. Split each group into amount streams (only at a clear price-point gap)
        3. For each stream, detect periodicity (weekly, monthly, etc.)
        4. Assign confidence based on consistency

        Args:
            transactions: List of Transaction objects

        Returns:
            List of RecurringPattern objects
        """
        patterns = []

        # Step 1: Group by (account, merchant)
        by_merchant = self._group_by_account_and_merchant(transactions)

        # Step 2: For each account-merchant, detect patterns
        for _key, txns in by_merchant.items():
            # Skip if too few transactions
            if len(txns) < self.min_occurrences:
                continue

            # Split into amount streams ONLY at a clear price-point gap (a variable but
            # unimodal biller stays together; a small subscription is separated from
            # large one-off purchases at the same merchant), then detect cadence per stream.
            for stream_txns in self._amount_streams(txns):
                if len(stream_txns) < self.min_occurrences:
                    continue

                pattern = self._detect_periodicity(stream_txns)
                if pattern:
                    patterns.append(pattern)

        return patterns

    def _group_by_account_and_merchant(
        self, transactions: list[Transaction]
    ) -> dict[tuple[str, str, str], list[Transaction]]:
        """Group transactions by the ACCOUNT they posted to and the merchant.

        The account is in the key because every field the resulting pattern
        carries is a fact about one account or about none of them.
        ``RecurringPattern.account_id`` is taken from the group's first
        transaction, ``expected_amount`` is the mean over the group, and
        ``next_expected_date`` is one period after its last member; grouping by
        merchant alone made those three describe different populations the moment
        a merchant appeared on two accounts.

        A merchant can move a subscription to another card, or split payroll
        between two bank accounts. Pooling those rows would combine one account's
        identity with another account's dates and a mean that belongs to neither.
        Each account therefore needs its own cadence and amount estimate.

        The cost is honest and visible: a merchant that migrates accounts needs
        ``min_occurrences`` occurrences on the new one before a pattern is claimed
        there, and the old account's pattern lapses through ``missed_count``. That
        is the detector saying it does not yet know, which is what it does not
        know.
        """
        grouped = defaultdict(list)

        for txn in transactions:
            # Use merchant if available, otherwise use description
            merchant = txn.merchant if txn.merchant else txn.description[:50]
            grouped[(txn.institution, txn.account, merchant)].append(txn)

        return dict(grouped)

    def _is_income(self, txn: Transaction) -> bool:
        """Check if transaction is income."""
        return txn.flow in ["income"] or txn.cashflow_type == "income" or txn.amount_signed > 0

    def _amount_streams(self, transactions: list[Transaction]) -> list[list[Transaction]]:
        """Split a merchant's transactions into amount streams, but ONLY at a clear
        multi-modal price-point gap.

        Unlike a fixed +/- tolerance cluster (which shatters a variable-amount biller like
        a utility before its cadence is ever examined), this keeps a unimodal-but-variable
        stream together and starts a new stream only when consecutive sorted amounts differ
        by more than AMOUNT_SPLIT_RATIO -- e.g. a $14.68 subscription vs $80 one-off
        purchases at the same merchant. Cadence is then detected per stream.
        """
        if not transactions:
            return []

        sorted_txns = sorted(transactions, key=lambda t: abs(t.amount_signed))
        streams: list[list[Transaction]] = [[sorted_txns[0]]]
        for prev, txn in pairwise(sorted_txns):
            prev_amount = abs(prev.amount_signed)
            amount = abs(txn.amount_signed)
            if prev_amount > 0 and amount / prev_amount > self.AMOUNT_SPLIT_RATIO:
                streams.append([txn])  # clear separate price point
            else:
                streams[-1].append(txn)
        return streams

    def _normalize_date_for_pattern(self, txn_date: date) -> date:
        """
        Normalize date to account for weekend/holiday shifts.

        If a transaction posts on Saturday/Sunday, it likely was scheduled
        for Friday or Monday. This helps detect patterns that shift due to weekends.
        """
        # If weekend, shift to Friday
        if txn_date.weekday() == 5:  # Saturday
            return txn_date - timedelta(days=1)
        elif txn_date.weekday() == 6:  # Sunday
            return txn_date - timedelta(days=2)
        return txn_date

    def _detect_periodicity(self, transactions: list[Transaction]) -> RecurringPattern | None:
        """
        Detect periodicity from transaction dates.

        Returns RecurringPattern if a consistent pattern is found.
        Accounts for weekend/holiday shifts in timing.
        """
        # Sort by date
        sorted_txns = sorted(transactions, key=lambda t: t.posting_date)

        # Compute gaps between consecutive DISTINCT (normalized) dates. Coalescing same-day
        # postings removes spurious 0-day gaps from a duplicate or fee line, which would
        # otherwise drag the cadence estimate into a dead zone.
        normalized_dates = sorted(
            {self._normalize_date_for_pattern(t.posting_date) for t in sorted_txns}
        )
        gaps = [
            (normalized_dates[i + 1] - normalized_dates[i]).days
            for i in range(len(normalized_dates) - 1)
        ]

        if not gaps:
            return None

        # Classify on the MEDIAN gap (robust to a single missed/extra occurrence); keep the
        # mean for the consistency/confidence calculation below.
        avg_gap = sum(gaps) / len(gaps)
        frequency, expected_day = self._classify_frequency(statistics.median(gaps), sorted_txns)

        if not frequency:
            return None

        # A weekly claim from only a few points is usually a coincidental cluster, not a
        # recurring commitment -- require more evidence before trusting the tightest cadence.
        if frequency == "weekly" and len(sorted_txns) < self.MIN_WEEKLY_OCCURRENCES:
            return None

        # Compute confidence based on gap consistency
        gap_std = self._compute_std(gaps)

        # For income, be more lenient with timing variations
        is_income = self._is_income(sorted_txns[0])
        confidence_threshold = 0.5 if is_income else 0.6

        consistency = 1.0 - min(gap_std / avg_gap, 1.0) if avg_gap > 0 else 0.0
        confidence = max(0.0, min(1.0, consistency))

        # Accept patterns above threshold
        if confidence < confidence_threshold:
            return None

        # Pattern classification
        pattern_type = self._classify_pattern_type(sorted_txns[0])

        # Compute statistics
        amounts = [abs(t.amount_signed) for t in sorted_txns]
        avg_amount = sum(amounts) / len(amounts)

        # Drop sub-dollar noise (interest-rounding corrections) before it becomes a pattern.
        if avg_amount < self.MIN_PATTERN_AMOUNT:
            return None

        # The average of cent-quantized amounts is generally not itself a cent-multiple
        # (e.g. (10.00 + 10.00 + 11.00) / 3 = 10.3333...). Quantize the value that becomes
        # expected_amount so the in-memory RecurringPattern already satisfies the cents
        # invariant instead of relying on the Numeric(12,2) column to round on write.
        # NOTE: pattern_id is intentionally still derived from the FULL-precision avg
        # string below, preserving the existing id derivation (and its dedup behavior).
        expected_amount = quantize_cents(Decimal(str(avg_amount)))

        amount_std = self._compute_std(amounts)

        # Convert to float for comparison (amount_std is float, avg_amount is Decimal)
        is_fixed_amount = (amount_std / float(avg_amount)) < 0.05 if avg_amount > 0 else False

        # Predict next occurrence
        last_date = sorted_txns[-1].posting_date
        next_date = self._predict_next_occurrence(last_date, frequency, expected_day)

        # Check if pattern is still active (no missed occurrences)
        now = naive_utc_now()
        is_active, missed_count = self._check_active(last_date, next_date, now.date())

        # Account info from the first transaction. Safe because the grouping key
        # includes (institution, account): every transaction in this stream posted
        # to the same account, so "the first one's account" and "the pattern's
        # account" are the same fact rather than two.
        txn = sorted_txns[0]

        # Generate pattern ID
        pattern_id = stable_id(
            txn.institution,
            txn.account,
            txn.merchant or txn.description[:50],
            txn.category,
            frequency,
            str(expected_day),
            str(avg_amount),
        )

        return RecurringPattern(
            pattern_id=pattern_id,
            pattern_type=pattern_type,
            merchant=txn.merchant,
            category=txn.category,
            institution=txn.institution,
            account_id=account_identity(txn.institution, txn.account),
            frequency=frequency,
            expected_day_of_period=expected_day,
            is_fixed_amount=is_fixed_amount,
            expected_amount=expected_amount,
            amount_std_dev=Decimal(str(amount_std)),
            confidence=confidence,
            detection_method="rule_based",
            occurrence_count=len(sorted_txns),
            first_occurrence=sorted_txns[0].posting_date,
            last_occurrence=sorted_txns[-1].posting_date,
            next_expected_date=next_date,
            is_active=is_active,
            missed_count=missed_count,
            detected_at=now,
            updated_at=now,
            source_txn_ids=tuple(t.txn_id for t in sorted_txns),
        )

    def _classify_frequency(
        self, gap_days: float, transactions: list[Transaction]
    ) -> tuple[str, int]:
        """
        Classify frequency from average gap in days.

        Returns: (frequency, expected_day_of_period)

        Uses wider tolerances to account for:
        - Weekend/holiday shifts
        - Variations in payroll processing
        - Month length variations (28-31 days)
        """
        # Determine if income (more lenient thresholds)
        is_income = self._is_income(transactions[0])

        # Weekly: 6-9 days (wider for income)
        weekly_min = 5 if is_income else 6
        weekly_max = 9 if is_income else 8
        if weekly_min <= gap_days <= weekly_max:
            # Expected day of week (0=Monday)
            day_of_week = transactions[0].posting_date.weekday()
            return "weekly", day_of_week

        # Biweekly: 12-16 days (wider for income)
        biweekly_min = 12 if is_income else 13
        biweekly_max = 16 if is_income else 15
        if biweekly_min <= gap_days <= biweekly_max:
            day_of_week = transactions[0].posting_date.weekday()
            return "biweekly", day_of_week

        # Monthly: 27-33 days (wider for income)
        monthly_min = 27 if is_income else 28
        monthly_max = 33 if is_income else 32
        if monthly_min <= gap_days <= monthly_max:
            # Expected day of month
            day_of_month = transactions[0].posting_date.day
            return "monthly", day_of_month

        # Quarterly: 85-98 days (wider for income)
        quarterly_min = 85 if is_income else 88
        quarterly_max = 98 if is_income else 95
        if quarterly_min <= gap_days <= quarterly_max:
            day_of_month = transactions[0].posting_date.day
            return "quarterly", day_of_month

        # Annual: 355-375 days (wider for income)
        annual_min = 355 if is_income else 360
        annual_max = 375 if is_income else 370
        if annual_min <= gap_days <= annual_max:
            day_of_month = transactions[0].posting_date.day
            return "annual", day_of_month

        return "", 0

    def _classify_pattern_type(self, txn: Transaction) -> str:
        """
        Classify pattern type from transaction characteristics.

        Returns: "subscription", "bill", "income", "transfer"
        """
        # Income patterns
        if txn.flow in ["income"] or txn.cashflow_type == "income":
            return "income"

        # Transfer patterns
        if txn.flow in ["transfer", "payment"] or txn.cashflow_type == "transfer":
            return "transfer"

        from ledger.adapters.registry import compatibility_policy

        merchant_lower = txn.merchant.lower()
        category_lower = (txn.category or "").lower()

        # Bills: utilities, insurance, telecom, housing. Check known providers AND the
        # category, so utility and telecom billers are not mislabeled
        # "subscription". Checked first because some providers also sell subscriptions.
        bill_keywords = [
            *compatibility_policy().get("bill_tokens", []),
            "insurance",
            "phone",
            "utility",
            "utilities",
            "electric",
            "hydro",
            "gas",
            "water",
            "internet",
            "telecom",
            "mobile",
            "mobility",
            "wireless",
            "cable",
            "rent",
            "mortgage",
            "bell",
            "telus",
            "fido",
            "shaw",
            "fortis",
            "enmax",
            "epcor",
            "koodo",
        ]
        bill_categories = ("utilit", "insurance", "rent", "mortgage", "bill", "telecom", "phone")
        if any(kw in merchant_lower for kw in bill_keywords) or any(
            c in category_lower for c in bill_categories
        ):
            return "bill"

        # Subscriptions: streaming / software / memberships.
        subscription_keywords = [
            "netflix",
            "spotify",
            "apple",
            "amazon prime",
            "prime video",
            "youtube",
            "disney",
            "hbo",
            "hulu",
            "crave",
            "audible",
            "patreon",
            "membership",
            "microsoft",
            "adobe",
            "dropbox",
            "icloud",
        ]
        subscription_categories = ("subscription", "streaming", "software", "entertainment")
        if any(kw in merchant_lower for kw in subscription_keywords) or any(
            c in category_lower for c in subscription_categories
        ):
            return "subscription"

        # Default: unknown recurring expense -> subscription.
        return "subscription"

    def _predict_next_occurrence(self, last_date: date, frequency: str, expected_day: int) -> date:
        """Predict next occurrence date (delegates to the shared month-end-safe helper)."""
        return next_occurrence(last_date, frequency, expected_day)

    def _check_active(
        self, last_date: date, next_expected_date: date, today: date
    ) -> tuple[bool, int]:
        """
        Check if pattern is still active.

        A pattern is inactive if we're past the next expected date by >50% of the period.

        Returns: (is_active, missed_count)
        """
        if today < next_expected_date:
            # Not yet due
            return True, 0

        # How many days past expected?
        days_overdue = (today - next_expected_date).days

        # Compute period length
        period_days = (next_expected_date - last_date).days
        if period_days <= 0:
            return True, 0

        # Allow 50% grace period
        grace_days = period_days * 0.5

        if days_overdue > grace_days:
            # Missed occurrence
            missed_count = int(days_overdue / period_days)
            is_active = missed_count < 2  # Inactive after 2 missed

            return is_active, missed_count

        return True, 0

    def _compute_std(self, values: Sequence[float | Decimal]) -> float:
        """Compute standard deviation."""
        if not values:
            return 0.0

        values_float = [float(v) for v in values]
        mean = sum(values_float) / len(values_float)
        variance = sum((v - mean) ** 2 for v in values_float) / len(values_float)
        return variance**0.5

    def save_patterns(self, patterns: list[RecurringPattern]) -> int:
        """
        Save patterns to database, REPLACING the prior detected set.

        The pattern_id is derived from mutable inputs (amount, frequency, expected day),
        so a re-run whose inputs shifted mints a NEW id; a plain upsert-by-id would leave
        the old row orphaned and duplicates would pile up on every recompute. The detector
        always runs over the full transaction set, so rebuild: clear the existing
        rule-based rows, then insert the freshly-detected set (mirrors save_trends).

        Args:
            patterns: List of RecurringPattern objects

        Returns:
            Number of patterns saved
        """
        with session_scope(self.db_path) as session:
            # A full rebuild of rule-based patterns must also reset the txn->pattern links
            # it owns, so no transaction is left pointing at a pattern we are about to
            # delete.  If a pattern's id changes between runs (amount/frequency drifted) or
            # a pattern disappears entirely, the old txns would otherwise keep a
            # recurring_pattern_id that points at a now-deleted row (dangling reference),
            # causing the matcher to skip them and leaving them permanently mislinked.
            # Scoped to rule_based so links owned by other detection methods are untouched.
            session.query(TransactionDB).filter(
                TransactionDB.recurring_pattern_id.in_(
                    session.query(RecurringPatternDB.pattern_id).filter(
                        RecurringPatternDB.detection_method == "rule_based"
                    )
                )
            ).update({TransactionDB.recurring_pattern_id: None}, synchronize_session=False)

            # Bulk DELETE executes immediately, so the subsequent merge/insert sees a clean
            # slate -- no stale rows survive from a previous run's different pattern_ids.
            session.query(RecurringPatternDB).filter(
                RecurringPatternDB.detection_method == "rule_based"
            ).delete(synchronize_session=False)
            for pattern in patterns:
                db_pattern = RecurringPatternDB(
                    pattern_id=pattern.pattern_id,
                    pattern_type=pattern.pattern_type,
                    merchant=pattern.merchant,
                    category=pattern.category,
                    institution=pattern.institution,
                    account_id=pattern.account_id,
                    frequency=pattern.frequency,
                    expected_day_of_period=pattern.expected_day_of_period,
                    is_fixed_amount=pattern.is_fixed_amount,
                    expected_amount=pattern.expected_amount,
                    amount_std_dev=pattern.amount_std_dev,
                    confidence=pattern.confidence,
                    detection_method=pattern.detection_method,
                    occurrence_count=pattern.occurrence_count,
                    first_occurrence=pattern.first_occurrence,
                    last_occurrence=pattern.last_occurrence,
                    next_expected_date=pattern.next_expected_date,
                    is_active=pattern.is_active,
                    missed_count=pattern.missed_count,
                    detected_at=pattern.detected_at,
                    updated_at=pattern.updated_at,
                )
                session.merge(db_pattern)  # Upsert

                # Mark the source transactions that formed this pattern so that
                # match_all_transactions (which skips rows with recurring_pattern_id
                # set) won't re-match them and double-count the occurrence_count.
                if pattern.source_txn_ids:
                    (
                        session.query(TransactionDB)
                        .filter(TransactionDB.txn_id.in_(pattern.source_txn_ids))
                        .update(
                            {TransactionDB.recurring_pattern_id: pattern.pattern_id},
                            synchronize_session=False,
                        )
                    )

        return len(patterns)
