"""Focused insights query module."""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal

from ledger.core import Transaction
from ledger.finance_policy import spend_sql_filters
from ledger.query.aggregates import AggregateQueries
from ledger.query.base import QueryModule
from ledger.storage.database import (
    TransactionDB,
)
from ledger.storage.mappers import transaction_from_db


class InsightQueries(QueryModule):
    """Focused query methods owned by the insights namespace."""

    def get_period_comparison(
        self,
        current_start: date,
        current_end: date,
        previous_start: date,
        previous_end: date,
    ) -> dict:
        """
        Compare two time periods with comprehensive metrics.

        Args:
            current_start: Current period start date
            current_end: Current period end date
            previous_start: Previous period start date
            previous_end: Previous period end date

        Returns:
            Dictionary with current, previous, and changes for all KPIs
        """
        # Get KPIs for both periods
        aggregates = AggregateQueries(
            self.db_path,
            session_scope_factory=self._session_scope_factory,
        )
        current_kpis = aggregates.compute_kpis(current_start, current_end)
        previous_kpis = aggregates.compute_kpis(previous_start, previous_end)

        # Calculate changes
        changes = {}
        for key in ["income", "expenses", "net"]:
            curr = current_kpis[key]
            prev = previous_kpis[key]

            absolute = curr - prev
            percent = (absolute / prev * 100) if prev != 0 else Decimal("0")

            changes[key] = {
                "absolute": absolute,
                "percent": percent,
                "direction": "up" if absolute > 0 else ("down" if absolute < 0 else "stable"),
            }

        # Transaction count change
        curr_count = current_kpis["transaction_count"]
        prev_count = previous_kpis["transaction_count"]
        count_change = curr_count - prev_count
        count_pct = (count_change / prev_count * 100) if prev_count > 0 else 0

        changes["transaction_count"] = {
            "absolute": count_change,
            "percent": Decimal(str(count_pct)),
            "direction": "up" if count_change > 0 else ("down" if count_change < 0 else "stable"),
        }

        return {"current": current_kpis, "previous": previous_kpis, "changes": changes}

    def get_spending_by_category_comparison(
        self,
        current_start: date,
        current_end: date,
        previous_start: date,
        previous_end: date,
        exclude_from_spending: bool = True,
    ) -> dict:
        """
        Compare category spending across two periods.

        Args:
            current_start: Current period start
            current_end: Current period end
            previous_start: Previous period start
            previous_end: Previous period end
            exclude_from_spending: Exclude non-spending categories

        Returns:
            Dictionary mapping category to comparison metrics:
            {
                'category_name': {
                    'current': Decimal,
                    'previous': Decimal,
                    'change_abs': Decimal,
                    'change_pct': Decimal,
                    'direction': str,  # 'up', 'down', 'stable'
                },
                ...
            }
        """
        aggregates = AggregateQueries(
            self.db_path,
            session_scope_factory=self._session_scope_factory,
        )
        current = aggregates.get_spending_by_category(
            current_start,
            current_end,
            exclude_from_spending,
        )
        previous = aggregates.get_spending_by_category(
            previous_start,
            previous_end,
            exclude_from_spending,
        )

        # Get all unique categories
        all_categories = set(current.keys()) | set(previous.keys())

        result = {}
        for category in all_categories:
            curr_amt = current.get(category, Decimal("0"))
            prev_amt = previous.get(category, Decimal("0"))

            change_abs = curr_amt - prev_amt
            change_pct = (change_abs / prev_amt * 100) if prev_amt != 0 else Decimal("0")

            result[category] = {
                "current": curr_amt,
                "previous": prev_amt,
                "change_abs": change_abs,
                "change_pct": change_pct,
                "direction": "up" if change_abs > 0 else ("down" if change_abs < 0 else "stable"),
            }

        return result

    # ========================================================================
    # New Query Methods for Dashboard Enhancements
    # ========================================================================

    def detect_anomalies(
        self,
        start_date: date,
        end_date: date,
        threshold: float = 2.0,
    ) -> list[Transaction]:
        """
        Find outlier transactions using statistical threshold.

        Finds transactions where amount > mean + (threshold * std_dev)
        by category.

        Args:
            start_date: Start date
            end_date: End date
            threshold: Number of standard deviations for outlier detection

        Returns:
            List of anomalous Transaction objects
        """
        with self._open_session() as session:
            # Get all expense transactions for the period, matching the same
            # universe as get_spending_by_category so that intentionally-excluded
            # transactions (e.g. large reimbursables) never surface as anomalies.
            all_txns = (
                session.query(TransactionDB)
                .filter(TransactionDB.posting_date >= start_date)
                .filter(TransactionDB.posting_date <= end_date)
                .filter(*spend_sql_filters())
                .all()
            )

            # Per-category running sums, for O(1) LEAVE-ONE-OUT statistics: a category's
            # single biggest spend must not inflate its OWN threshold and hide itself, so
            # each candidate is judged against the mean/std of the OTHER transactions.
            cat_sum: dict[str, float] = defaultdict(float)
            cat_sumsq: dict[str, float] = defaultdict(float)
            cat_count: dict[str, int] = defaultdict(int)
            for txn in all_txns:
                amt = abs(float(txn.amount_signed))
                category = str(txn.category)
                cat_sum[category] += amt
                cat_sumsq[category] += amt * amt
                cat_count[category] += 1

            scored: list[tuple[float, TransactionDB]] = []
            for txn_db in all_txns:
                category = str(txn_db.category)
                n = cat_count[category]
                if n < 4:  # need >= 3 OTHERS once the candidate is left out
                    continue
                amt = abs(float(txn_db.amount_signed))
                others = n - 1
                mean = (cat_sum[category] - amt) / others
                variance = max((cat_sumsq[category] - amt * amt) / others - mean * mean, 0.0)
                std = variance**0.5
                if std <= 0:
                    # Other transactions are identical; flag only if the candidate exceeds
                    # that uniform baseline.
                    if amt > mean:
                        scored.append((float("inf"), txn_db))
                    continue
                if amt > mean + threshold * std:
                    scored.append(((amt - mean) / std, txn_db))  # severity = sigma above

            # Rank by SURPRISE (sigma above the others), not raw dollar amount, and keep a
            # generous cap so genuinely-anomalous small categories aren't silently dropped.
            scored.sort(key=lambda item: item[0], reverse=True)
            return [transaction_from_db(txn_db) for _, txn_db in scored[:50]]
