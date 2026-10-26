"""
Trend aggregation for pre-computing spending statistics.

Aggregates transactions by time period and category for fast dashboard queries.
"""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from ledger.core import Transaction
from ledger.core.analysis_models import SpendingTrend
from ledger.finance_policy import spend_sql_filters
from ledger.storage.database import SpendingTrendDB, TransactionDB, session_scope
from ledger.utils.dates import naive_utc_now
from ledger.utils.money import quantize_cents

# vs_previous_period_pct and vs_avg_6m_pct are stored in Numeric(8,2) columns: at most 6
# integer digits, so the representable magnitude is +/- 999999.99. A sparse category (a
# tiny 6-month average vs a large current spend) can compute a percentage far beyond that
# and overflow the column. Clamp the magnitude to this bound and quantize to cents so the
# value always fits its column and satisfies the cents invariant in memory.
_PCT_COLUMN_MAX = Decimal("999999.99")


def _clamp_pct(pct: Decimal) -> Decimal:
    """Clamp a percentage to the Numeric(8,2) column range and quantize to cents."""
    clamped = max(-_PCT_COLUMN_MAX, min(pct, _PCT_COLUMN_MAX))
    return quantize_cents(clamped)


class TrendAggregator:
    """
    Aggregate spending trends for efficient dashboard queries.

    Pre-computes daily, weekly, monthly, and quarterly trends by category.
    """

    def __init__(self, db_path: Path):
        """
        Initialize trend aggregator.

        Args:
            db_path: Path to SQLite database
        """
        self.db_path = db_path

    def compute_daily_trends(self, target_date: date) -> list[SpendingTrend]:
        """
        Compute spending trends for a specific day.

        Args:
            target_date: Date to compute trends for

        Returns:
            List of SpendingTrend objects, one per category
        """
        from ledger.storage.mappers import transaction_from_db

        # Fetch transactions for this day
        with session_scope(self.db_path) as session:
            db_txns = (
                session.query(TransactionDB)
                .filter(TransactionDB.posting_date == target_date)
                # Single source of truth for the spend universe (expense-only,
                # include_in_cashflow, exclude_from_spending, AND CAD-only) so the
                # persisted trend totals never diverge from get_spending_by_category
                # / get_category_spending_trend. Previously the CAD-only predicate
                # was missing here, letting a USD row inflate total_spent at par.
                .filter(*spend_sql_filters())
                .all()
            )
            day_txns = [transaction_from_db(db_txn) for db_txn in db_txns]

        # Group by category
        category_txns = self._group_by_category(day_txns)

        # Compute trends
        trends = []
        for category, txns in category_txns.items():
            trend = self._build_daily_trend(
                time_period=target_date.isoformat(),
                category=category,
                transactions=txns,
                target_date=target_date,
            )
            trends.append(trend)

        return trends

    def compute_monthly_trends(self, year: int, month: int) -> list[SpendingTrend]:
        """
        Compute spending trends for a specific month.

        Args:
            year: Year (e.g., 2025)
            month: Month (1-12)

        Returns:
            List of SpendingTrend objects, one per category
        """
        from ledger.storage.mappers import transaction_from_db

        # Determine month start and end dates
        start_date = date(year, month, 1)
        if month == 12:
            end_date = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            end_date = date(year, month + 1, 1) - timedelta(days=1)

        # Fetch transactions for this month
        with session_scope(self.db_path) as session:
            db_txns = (
                session.query(TransactionDB)
                .filter(TransactionDB.posting_date >= start_date)
                .filter(TransactionDB.posting_date <= end_date)
                # Canonical spend universe (incl. CAD-only); see compute_daily_trends.
                .filter(*spend_sql_filters())
                .all()
            )
            month_txns = [transaction_from_db(db_txn) for db_txn in db_txns]

        # Group by category
        category_txns = self._group_by_category(month_txns)

        # Compute trends
        trends = []
        for category, txns in category_txns.items():
            trend = self._build_monthly_trend(
                year=year,
                month=month,
                category=category,
                transactions=txns,
            )
            trends.append(trend)

        return trends

    def _group_by_category(self, transactions: list[Transaction]) -> dict[str, list[Transaction]]:
        """Group transactions by category."""
        grouped = defaultdict(list)
        for txn in transactions:
            grouped[txn.category].append(txn)
        return dict(grouped)

    def _build_daily_trend(
        self,
        time_period: str,
        category: str,
        transactions: list[Transaction],
        target_date: date,
    ) -> SpendingTrend:
        """
        Build daily trend from transactions.

        Args:
            time_period: ISO date string (e.g., "2025-12-15")
            category: Category name
            transactions: Transactions for this category on this day
            target_date: The date being aggregated

        Returns:
            SpendingTrend object
        """
        # Basic stats
        transaction_count = len(transactions)
        amounts = [abs(txn.amount_signed) for txn in transactions]
        total_spent = sum(amounts, Decimal("0"))
        # Quantize derived stats at the boundary they are persisted from: an odd
        # transaction count divides into repeating sub-cent decimals, and an even
        # median averages two cents into a half-cent — both violated the module's
        # own cents invariant once stored.
        avg_transaction = (
            quantize_cents(total_spent / transaction_count)
            if transaction_count > 0
            else Decimal("0")
        )
        median_transaction = quantize_cents(self._compute_median(amounts))

        # Find top merchant
        merchant_totals: defaultdict[str, Decimal] = defaultdict(Decimal)
        for txn in transactions:
            merchant_totals[txn.merchant] += abs(txn.amount_signed)

        if merchant_totals:
            top_merchant = max(merchant_totals.items(), key=lambda x: x[1])
            top_merchant_name = top_merchant[0]
            top_merchant_amount = top_merchant[1]
        else:
            top_merchant_name = None
            top_merchant_amount = None

        # Compute comparisons
        vs_previous_period_abs, vs_previous_period_pct = self._compare_to_previous_period(
            category=category,
            current_date=target_date,
            current_amount=total_spent,
            period_type="daily",
        )

        vs_avg_6m_pct = self._compare_to_6m_average(
            category=category,
            current_date=target_date,
            current_amount=total_spent,
            period_type="daily",
        )

        # Determine trend direction
        if vs_previous_period_pct > Decimal("10"):
            trend_direction = "increasing"
        elif vs_previous_period_pct < Decimal("-10"):
            trend_direction = "decreasing"
        elif vs_previous_period_pct == 0 and vs_previous_period_abs > 0:
            # New / previously-zero category that now spends: up from nothing.
            trend_direction = "increasing"
        else:
            trend_direction = "stable"

        return SpendingTrend(
            time_period=time_period,
            period_type="daily",
            category=category,
            transaction_count=transaction_count,
            total_spent=total_spent,
            avg_transaction=avg_transaction,
            median_transaction=median_transaction,
            vs_previous_period_pct=vs_previous_period_pct,
            vs_previous_period_abs=vs_previous_period_abs,
            vs_avg_6m_pct=vs_avg_6m_pct,
            top_merchant=top_merchant_name,
            top_merchant_amount=top_merchant_amount,
            trend_direction=trend_direction,
            computed_at=naive_utc_now(),
        )

    def _build_monthly_trend(
        self,
        year: int,
        month: int,
        category: str,
        transactions: list[Transaction],
    ) -> SpendingTrend:
        """
        Build monthly trend from transactions.

        Args:
            year: Year
            month: Month (1-12)
            category: Category name
            transactions: Transactions for this category in this month

        Returns:
            SpendingTrend object
        """
        # Time period format: "2025-12"
        time_period = f"{year:04d}-{month:02d}"

        # Basic stats
        transaction_count = len(transactions)
        amounts = [abs(txn.amount_signed) for txn in transactions]
        total_spent = sum(amounts, Decimal("0"))
        # Quantize derived stats at the boundary they are persisted from: an odd
        # transaction count divides into repeating sub-cent decimals, and an even
        # median averages two cents into a half-cent — both violated the module's
        # own cents invariant once stored.
        avg_transaction = (
            quantize_cents(total_spent / transaction_count)
            if transaction_count > 0
            else Decimal("0")
        )
        median_transaction = quantize_cents(self._compute_median(amounts))

        # Find top merchant
        merchant_totals: defaultdict[str, Decimal] = defaultdict(Decimal)
        for txn in transactions:
            merchant_totals[txn.merchant] += abs(txn.amount_signed)

        if merchant_totals:
            top_merchant = max(merchant_totals.items(), key=lambda x: x[1])
            top_merchant_name = top_merchant[0]
            top_merchant_amount = top_merchant[1]
        else:
            top_merchant_name = None
            top_merchant_amount = None

        # Compute comparisons
        current_date = date(year, month, 15)  # Use mid-month as reference
        vs_previous_period_abs, vs_previous_period_pct = self._compare_to_previous_period(
            category=category,
            current_date=current_date,
            current_amount=total_spent,
            period_type="monthly",
        )

        vs_avg_6m_pct = self._compare_to_6m_average(
            category=category,
            current_date=current_date,
            current_amount=total_spent,
            period_type="monthly",
        )

        # Determine trend direction
        if vs_previous_period_pct > Decimal("10"):
            trend_direction = "increasing"
        elif vs_previous_period_pct < Decimal("-10"):
            trend_direction = "decreasing"
        elif vs_previous_period_pct == 0 and vs_previous_period_abs > 0:
            # New / previously-zero category that now spends: up from nothing.
            trend_direction = "increasing"
        else:
            trend_direction = "stable"

        return SpendingTrend(
            time_period=time_period,
            period_type="monthly",
            category=category,
            transaction_count=transaction_count,
            total_spent=total_spent,
            avg_transaction=avg_transaction,
            median_transaction=median_transaction,
            vs_previous_period_pct=vs_previous_period_pct,
            vs_previous_period_abs=vs_previous_period_abs,
            vs_avg_6m_pct=vs_avg_6m_pct,
            top_merchant=top_merchant_name,
            top_merchant_amount=top_merchant_amount,
            trend_direction=trend_direction,
            computed_at=naive_utc_now(),
        )

    def _compare_to_previous_period(
        self,
        category: str,
        current_date: date,
        current_amount: Decimal,
        period_type: str,
    ) -> tuple[Decimal, Decimal]:
        """
        Compare current amount to previous period.

        Args:
            category: Category name
            current_date: Current period date
            current_amount: Current period total
            period_type: "daily" or "monthly"

        Returns:
            Tuple of (absolute_change, percentage_change)
        """
        # Calculate previous period
        if period_type == "daily":
            previous_date = current_date - timedelta(days=1)
            previous_period = previous_date.isoformat()
        elif period_type == "monthly":
            # Previous month
            if current_date.month == 1:
                previous_year = current_date.year - 1
                previous_month = 12
            else:
                previous_year = current_date.year
                previous_month = current_date.month - 1
            previous_period = f"{previous_year:04d}-{previous_month:02d}"
        else:
            return Decimal("0"), Decimal("0")

        # Fetch previous period data
        with session_scope(self.db_path) as session:
            previous_trend = (
                session.query(SpendingTrendDB)
                .filter_by(
                    time_period=previous_period,
                    period_type=period_type,
                    category=category,
                )
                .first()
            )

            if previous_trend and previous_trend.total_spent > 0:
                # Extract value while still in session
                previous_amount = previous_trend.total_spent
                absolute_change = current_amount - previous_amount
                percentage_change = (absolute_change / previous_amount) * 100
                # Clamp/quantize the pct so it never overflows its Numeric(8,2) column.
                # absolute_change uses the wider Numeric(12,2) column and is left as-is.
                return absolute_change, _clamp_pct(percentage_change)

        # No previous (or previously-zero) period: a category that spent this period is
        # an increase from nothing, so report the full amount as the change (the % is
        # undefined, left at 0) -- not (0, 0), which read as a misleading "stable".
        if current_amount > 0:
            return current_amount, Decimal("0")
        return Decimal("0"), Decimal("0")

    def _compare_to_6m_average(
        self,
        category: str,
        current_date: date,
        current_amount: Decimal,
        period_type: str,
    ) -> Decimal:
        """
        Compare current amount to 6-month average.

        Args:
            category: Category name
            current_date: Current period date
            current_amount: Current period total
            period_type: "daily" or "monthly"

        Returns:
            Percentage change from 6-month average
        """
        # Calculate 6 months ago
        if period_type == "monthly":
            # Get previous 6 months (excluding current)
            months_ago = []
            for i in range(1, 7):
                month_offset = current_date.month - i
                year_offset = current_date.year
                while month_offset <= 0:
                    month_offset += 12
                    year_offset -= 1
                months_ago.append(f"{year_offset:04d}-{month_offset:02d}")

            # Fetch trends for these months
            with session_scope(self.db_path) as session:
                trends = (
                    session.query(SpendingTrendDB)
                    .filter(SpendingTrendDB.time_period.in_(months_ago))
                    .filter_by(period_type="monthly", category=category)
                    .all()
                )

                if trends:
                    # Extract values while in session. Divide by the FULL window (missing
                    # months count as $0), not just the months that had spend -- otherwise a
                    # sparse category's baseline is overstated and a real spike reads as
                    # "in line with average".
                    total = sum((t.total_spent for t in trends), Decimal("0"))
                    avg_6m = total / len(months_ago)

                    if avg_6m > 0:
                        percentage_change = ((current_amount - avg_6m) / avg_6m) * 100
                        # Clamp/quantize so a sparse category's spike can't overflow the
                        # Numeric(8,2) column.
                        return _clamp_pct(percentage_change)

        # For daily or if no data, return 0
        return Decimal("0")

    def _compute_median(self, values: list[Decimal]) -> Decimal:
        """Compute median of values."""
        if not values:
            return Decimal("0")

        sorted_values = sorted(values)
        n = len(sorted_values)

        if n % 2 == 0:
            # Even number of values
            mid1 = sorted_values[n // 2 - 1]
            mid2 = sorted_values[n // 2]
            return (mid1 + mid2) / 2
        else:
            # Odd number of values
            return sorted_values[n // 2]

    def save_trends(
        self,
        trends: list[SpendingTrend],
        *,
        replacement_scope: tuple[str, str],
    ) -> int:
        """
        Save trends to database.

        Replace one explicit ``(period_type, time_period)`` scope. The scope is
        independent from ``trends`` so an empty result still removes stale categories.

        Args:
            trends: Complete replacement rows for the period
            replacement_scope: ``(period_type, time_period)`` to replace

        Returns:
            Number of trends saved
        """
        period_type, time_period = replacement_scope
        mismatched = [
            (trend.period_type, trend.time_period)
            for trend in trends
            if (trend.period_type, trend.time_period) != replacement_scope
        ]
        if mismatched:
            raise ValueError(f"Trend rows fall outside replacement scope: {mismatched}")
        with session_scope(self.db_path) as session:
            session.query(SpendingTrendDB).filter_by(
                period_type=period_type,
                time_period=time_period,
            ).delete()
            for trend in trends:
                session.add(
                    SpendingTrendDB(
                        time_period=trend.time_period,
                        period_type=trend.period_type,
                        category=trend.category,
                        transaction_count=trend.transaction_count,
                        total_spent=trend.total_spent,
                        avg_transaction=trend.avg_transaction,
                        median_transaction=trend.median_transaction,
                        vs_previous_period_pct=trend.vs_previous_period_pct,
                        vs_previous_period_abs=trend.vs_previous_period_abs,
                        vs_avg_6m_pct=trend.vs_avg_6m_pct,
                        top_merchant=trend.top_merchant,
                        top_merchant_amount=trend.top_merchant_amount,
                        trend_direction=trend.trend_direction,
                        computed_at=trend.computed_at,
                    )
                )

        return len(trends)

    def aggregate_recent_days(self, days: int = 7, *, today: date | None = None) -> int:
        """
        Aggregate trends for the last N days.

        Args:
            days: Number of recent days to aggregate
            today: Reference date to treat as "today" (defaults to date.today()). Pass an
                explicit value in tests to freeze the clock and avoid flakiness near midnight
                or calendar boundaries.

        Returns:
            Total number of trends computed
        """
        total_trends = 0
        today = today or date.today()

        # Oldest day first, so each day's prev-period comparison can read the prior
        # day's freshly-saved row (otherwise the current period is computed before its
        # predecessor exists and every comparison comes back 0/"stable").
        for i in reversed(range(days)):
            target_date = today - timedelta(days=i)
            trends = self.compute_daily_trends(target_date)
            saved = self.save_trends(
                trends,
                replacement_scope=("daily", target_date.isoformat()),
            )
            total_trends += saved

        return total_trends

    def aggregate_recent_months(self, months: int = 6) -> int:
        """
        Aggregate trends for the last N months.

        Args:
            months: Number of recent months to aggregate

        Returns:
            Total number of trends computed
        """
        total_trends = 0
        today = date.today()

        # Oldest month first, so each month's prev-period comparison can read the prior
        # month's freshly-saved row.
        for i in reversed(range(months)):
            month_offset = today.month - i
            year_offset = today.year
            while month_offset <= 0:
                month_offset += 12
                year_offset -= 1

            trends = self.compute_monthly_trends(year_offset, month_offset)
            saved = self.save_trends(
                trends,
                replacement_scope=("monthly", f"{year_offset:04d}-{month_offset:02d}"),
            )
            total_trends += saved

        return total_trends
