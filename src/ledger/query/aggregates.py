"""Focused aggregates query module."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import and_, case, desc, func, or_

from ledger.core.types import CategorySpending, DatabaseStats, MerchantSpending
from ledger.finance_policy import (
    cad_sql_filters,
    expense_sql_filters,
    income_sql_filters,
    income_treatment_sql_filter,
    spend_sql_filters,
)
from ledger.finance_policy import (
    is_spend_row as _is_spend_row,
)
from ledger.query.base import QueryModule
from ledger.storage.database import (
    TransactionDB,
    TransactionTreatmentDB,
    TransferDB,
)
from ledger.utils.money import to_cents


def _cents(value) -> Decimal:
    """Coerce a SQL aggregate result to an exact cent-quantized Decimal.

    ``func.abs(func.sum(...))`` loses SQLAlchemy's Numeric typing, so SQLite can
    return a Python float; quantizing recovers the exact cent value.
    """
    return to_cents(value)


# Compatibility exports for callers outside this package that imported the
# historical private helpers. Production code imports the public policy module.
_cad_only = cad_sql_filters
_spend_universe_filters = spend_sql_filters


def is_spend_row(
    *,
    cashflow_type: str,
    include_in_cashflow: bool,
    exclude_from_spending: bool,
    currency: str | None,
    amount_signed: Decimal | None = None,
) -> bool:
    """Preserve the historical aggregate predicate call shape.

    Older callers did not provide an amount. When one is supplied, apply the
    stricter canonical sign check; otherwise retain the former expense-row
    semantics while delegating every other predicate to the public policy.
    """
    return _is_spend_row(
        amount_signed=amount_signed if amount_signed is not None else Decimal("-1"),
        cashflow_type=cashflow_type,
        include_in_cashflow=include_in_cashflow,
        exclude_from_spending=exclude_from_spending,
        currency=currency,
    )


class AggregateQueries(QueryModule):
    """Focused query methods owned by the aggregates namespace."""

    def get_spending_by_category(
        self,
        start_date: date,
        end_date: date,
        exclude_from_spending: bool = True,
    ) -> CategorySpending:
        """
        Get total spending by category for a date range.

        Returns: {"Dining": Decimal("500.00"), "Groceries": Decimal("800.00"), ...}
        """
        with self._open_session() as session:
            # Default path uses the canonical spend universe so category totals
            # never diverge from get_spending_by_merchant / the trend aggregates
            # (expense-only, include_in_cashflow, exclude_from_spending, and the
            # transfer exclusion baked into cashflow_type == "expense").
            # Legacy opt-in: when the caller wants exclude_from_spending rows,
            # retain the rest of the universe and drop only that predicate.
            spend_filters = spend_sql_filters() if exclude_from_spending else expense_sql_filters()

            query = session.query(
                TransactionDB.category,
                func.sum(func.abs(TransactionDB.amount_signed)).label("total"),
            ).filter(
                TransactionDB.posting_date >= start_date,
                TransactionDB.posting_date <= end_date,
                *spend_filters,
            )

            results = query.group_by(TransactionDB.category).all()

            return {row.category: _cents(row.total) for row in results}

    def get_spending_by_merchant(
        self,
        start_date: date,
        end_date: date,
        top_n: int = 20,
    ) -> MerchantSpending:
        """Get top N merchants by spending."""
        with self._open_session() as session:
            results = (
                session.query(
                    TransactionDB.merchant,
                    func.sum(func.abs(TransactionDB.amount_signed)).label("total"),
                )
                .filter(
                    TransactionDB.posting_date >= start_date,
                    TransactionDB.posting_date <= end_date,
                    *spend_sql_filters(),
                    TransactionDB.merchant != "",
                )
                .group_by(TransactionDB.merchant)
                .order_by(desc("total"))
                .limit(top_n)
                .all()
            )

            return {row.merchant: _cents(row.total) for row in results}

    def get_monthly_cashflow(
        self,
        year: int,
    ) -> list[dict]:
        """
        Get monthly cashflow for a year.

        Returns: [
            {"month": "2025-01", "income": 5000, "expense": -3000, "net": 2000},
            ...
        ]
        """
        with self._open_session() as session:
            income_condition = and_(*income_sql_filters())
            spend_condition = and_(*spend_sql_filters())
            # Use SQLite's strftime to group by month
            results = (
                session.query(
                    func.strftime("%Y-%m", TransactionDB.posting_date).label("month"),
                    func.sum(case((income_condition, TransactionDB.amount_signed), else_=0)).label(
                        "income"
                    ),
                    func.sum(case((spend_condition, TransactionDB.amount_signed), else_=0)).label(
                        "expense"
                    ),
                )
                .filter(
                    func.strftime("%Y", TransactionDB.posting_date) == str(year),
                    or_(income_condition, spend_condition),
                )
                .group_by("month")
                .order_by("month")
                .all()
            )

            return [
                {
                    "month": row.month,
                    # case()/sum() over a Numeric column loses typing, so SQLite can
                    # return a float (e.g. 2999.9999999999995). Quantize like every
                    # other aggregate in this module so totals stay exact cents.
                    "income": _cents(row.income),
                    "expense": _cents(row.expense),
                    # Net is derived so it always equals income + expense; a bare
                    # sum(amount_signed) would silently re-include excluded rows.
                    "net": _cents(row.income) + _cents(row.expense),
                }
                for row in results
            ]

    def get_max_abs_amount(self, start_date: date, end_date: date) -> Decimal:
        """Largest ``abs(amount_signed)`` over the date range, as cents.

        Returns ``Decimal("0.00")`` when the range has no rows. Used by the
        Transactions page to make the amount-filter slider's upper bound
        data-driven, so large rows (paycheques, rent, tuition, tax payments) are
        never silently excluded by a hardcoded cap.
        """
        with self._open_session() as session:
            value = (
                session.query(func.max(func.abs(TransactionDB.amount_signed)))
                .filter(
                    TransactionDB.posting_date >= start_date,
                    TransactionDB.posting_date <= end_date,
                )
                .scalar()
            )
        return _cents(value) if value is not None else Decimal("0.00")

    def get_stats(self) -> DatabaseStats:
        """Get database statistics."""
        with self._open_session() as session:
            stats = {
                "total_transactions": session.query(func.count(TransactionDB.txn_id)).scalar(),
                "total_transfers": session.query(func.count(TransferDB.txn_id)).scalar(),
                "earliest_date": session.query(func.min(TransactionDB.posting_date)).scalar(),
                "latest_date": session.query(func.max(TransactionDB.posting_date)).scalar(),
            }
            return stats

    def compute_kpis(
        self,
        start_date: date,
        end_date: date,
        categories: list[str] | None = None,
        merchants: list[str] | None = None,
        institutions: list[str] | None = None,
        accounts: list[str] | None = None,
        account_types: list[str] | None = None,
    ) -> dict:
        """
        Compute KPIs in database (not Python).

        ``activity_count`` counts every row in the filtered scope. The legacy
        ``transaction_count`` remains CAD + include-in-cashflow for compatibility.
        """
        with self._open_session() as session:
            query = session.query(TransactionDB).filter(
                TransactionDB.posting_date >= start_date,
                TransactionDB.posting_date <= end_date,
            )

            # Apply filters
            if categories:
                query = query.filter(TransactionDB.category.in_(categories))
            if merchants:
                query = query.filter(TransactionDB.merchant.in_(merchants))
            if institutions:
                query = query.filter(TransactionDB.institution.in_(institutions))
            if accounts:
                query = query.filter(TransactionDB.account.in_(accounts))
            if account_types:
                query = query.filter(TransactionDB.account_type.in_(account_types))

            # SQL aggregation
            income_condition = and_(*income_sql_filters())
            spend_condition = and_(*spend_sql_filters())
            expense_condition = and_(*expense_sql_filters())
            legacy_count_condition = and_(
                TransactionDB.include_in_cashflow == True,  # noqa: E712
                *cad_sql_filters(),
            )
            result = query.with_entities(
                func.sum(case((income_condition, TransactionDB.amount_signed), else_=0)).label(
                    "income"
                ),
                func.sum(
                    case(
                        (spend_condition, func.abs(TransactionDB.amount_signed)),
                        else_=0,
                    )
                ).label("expenses"),
                func.sum(
                    case(
                        (expense_condition, func.abs(TransactionDB.amount_signed)),
                        else_=0,
                    )
                ).label("gross_expenses"),
                func.sum(case((legacy_count_condition, 1), else_=0)).label("transaction_count"),
                func.count(TransactionDB.txn_id).label("activity_count"),
            ).first()

            income = _cents(result.income if result is not None else 0)
            expenses = _cents(result.expenses if result is not None else 0)
            gross_expenses = _cents(result.gross_expenses if result is not None else 0)

            # `net` stays on the SPEND universe (income - expenses), a decision locked
            # by the third-pass audit -- see test_compute_kpis_excludes_exclude_from_
            # spending_expenses.
            #
            # Note the asymmetry it carries: `expenses` applies exclude_from_spending
            # but `income` cannot, because that flag marks the Income category itself
            # as excluded-from-SPEND-metrics (payroll would vanish). So an excluded
            # transfer IN counts as income while the matching transfer OUT does not.
            # `gross_expenses` is exposed so callers can present a like-for-like pair
            # instead of two tiles that look subtractable but are not.
            return {
                "income": income,
                "expenses": expenses,
                "gross_expenses": gross_expenses,
                "net": income - expenses,
                "transaction_count": int(result.transaction_count or 0)
                if result is not None
                else 0,
                "activity_count": int(result.activity_count or 0) if result is not None else 0,
            }

    def get_daily_trends(
        self,
        start_date: date,
        end_date: date,
        category: str | None = None,
    ) -> list:
        """
        Get pre-computed daily trends.

        Args:
            start_date: Start date (inclusive)
            end_date: End date (inclusive)
            category: Optional category filter

        Returns:
            List of SpendingTrend dictionaries
        """
        from ledger.storage.database import SpendingTrendDB

        with self._open_session() as session:
            query = session.query(SpendingTrendDB).filter_by(period_type="daily")

            # Filter by date range
            query = query.filter(SpendingTrendDB.time_period >= start_date.isoformat()).filter(
                SpendingTrendDB.time_period <= end_date.isoformat()
            )

            if category:
                query = query.filter_by(category=category)

            trends = query.order_by(SpendingTrendDB.time_period).all()

            return [self._trend_db_to_dict(t) for t in trends]

    def get_monthly_trends(
        self,
        start_year: int,
        start_month: int,
        end_year: int,
        end_month: int,
        category: str | None = None,
    ) -> list:
        """
        Get pre-computed monthly trends.

        Args:
            start_year: Start year
            start_month: Start month (1-12)
            end_year: End year
            end_month: End month (1-12)
            category: Optional category filter

        Returns:
            List of SpendingTrend dictionaries
        """
        from ledger.storage.database import SpendingTrendDB

        start_period = f"{start_year:04d}-{start_month:02d}"
        end_period = f"{end_year:04d}-{end_month:02d}"

        with self._open_session() as session:
            query = session.query(SpendingTrendDB).filter_by(period_type="monthly")

            # Filter by period range
            query = query.filter(SpendingTrendDB.time_period >= start_period).filter(
                SpendingTrendDB.time_period <= end_period
            )

            if category:
                query = query.filter_by(category=category)

            trends = query.order_by(SpendingTrendDB.time_period).all()

            return [self._trend_db_to_dict(t) for t in trends]

    def get_trending_categories(self, period_type: str = "monthly", limit: int = 10) -> list:
        """
        Get categories with the most significant trends.

        Args:
            period_type: "daily" or "monthly"
            limit: Maximum number of categories to return

        Returns:
            List of categories with trend information, sorted by trend strength
        """
        from ledger.storage.database import SpendingTrendDB

        with self._open_session() as session:
            # Get most recent trends for each category
            trends = (
                session.query(SpendingTrendDB)
                .filter_by(period_type=period_type)
                .order_by(SpendingTrendDB.time_period.desc())
                .limit(100)  # Get recent trends
                .all()
            )

            # Group by category and find the most recent
            category_trends = {}
            for trend in trends:
                if trend.category not in category_trends:
                    category_trends[trend.category] = trend

            # Sort by absolute percentage change
            sorted_trends = sorted(
                category_trends.values(),
                # vs_previous_period_pct is nullable (a brand-new category has no
                # prior period); treat a missing value as 0 rather than crashing on
                # float(None).
                key=lambda t: abs(float(t.vs_previous_period_pct or 0)),
                reverse=True,
            )

            return [self._trend_db_to_dict(t) for t in sorted_trends[:limit]]

    def _trend_db_to_dict(self, db_row) -> dict:
        """Convert database trend row to dictionary.

        Money/ratio fields are kept as cent-quantized Decimal (the columns are
        Numeric) rather than downcast to float, so downstream money math never
        drifts -- mirroring every other ``_*_db_to_dict`` helper.
        """
        return {
            "time_period": db_row.time_period,
            "period_type": db_row.period_type,
            "category": db_row.category,
            "transaction_count": db_row.transaction_count,
            "total_spent": _cents(db_row.total_spent),
            "avg_transaction": _cents(db_row.avg_transaction),
            "median_transaction": _cents(db_row.median_transaction),
            "vs_previous_period_pct": _cents(db_row.vs_previous_period_pct),
            "vs_previous_period_abs": _cents(db_row.vs_previous_period_abs),
            "vs_avg_6m_pct": _cents(db_row.vs_avg_6m_pct),
            "top_merchant": db_row.top_merchant,
            "top_merchant_amount": _cents(db_row.top_merchant_amount)
            if db_row.top_merchant_amount is not None
            else None,
            "trend_direction": db_row.trend_direction,
            "computed_at": db_row.computed_at.isoformat() if db_row.computed_at else None,
        }

    def get_category_spending_trend(
        self,
        category: str,
        start_date: date,
        end_date: date,
        frequency: str = "monthly",
    ) -> list[dict]:
        """
        Get spending trend for a specific category over time.

        Args:
            category: Category name
            start_date: Start date
            end_date: End date
            frequency: 'monthly' or 'daily'

        Returns:
            List of dicts with keys: period, amount, transaction_count
        """
        with self._open_session() as session:
            period_format = "%Y-%m" if frequency == "monthly" else "%Y-%m-%d"

            query = (
                session.query(
                    func.strftime(period_format, TransactionDB.posting_date).label("period"),
                    func.sum(func.abs(TransactionDB.amount_signed)).label("amount"),
                    func.count(TransactionDB.txn_id).label("transaction_count"),
                )
                .filter(TransactionDB.category == category)
                .filter(TransactionDB.posting_date >= start_date)
                .filter(TransactionDB.posting_date <= end_date)
                .filter(*spend_sql_filters())
                .group_by("period")
                .order_by("period")
            )

            results = []
            for row in query:
                results.append(
                    {
                        "period": row.period,
                        "amount": _cents(row.amount),
                        "transaction_count": row.transaction_count,
                    }
                )

            return results

    def get_merchant_spending_trend(
        self,
        merchant: str,
        start_date: date,
        end_date: date,
        frequency: str = "monthly",
    ) -> list[dict]:
        """
        Get spending trend for a specific merchant over time.

        Args:
            merchant: Merchant name
            start_date: Start date
            end_date: End date
            frequency: 'monthly' or 'daily'

        Returns:
            List of dicts with keys: period, amount, transaction_count
        """
        with self._open_session() as session:
            period_format = "%Y-%m" if frequency == "monthly" else "%Y-%m-%d"

            query = (
                session.query(
                    func.strftime(period_format, TransactionDB.posting_date).label("period"),
                    func.sum(func.abs(TransactionDB.amount_signed)).label("amount"),
                    func.count(TransactionDB.txn_id).label("transaction_count"),
                )
                .filter(TransactionDB.merchant == merchant)
                .filter(TransactionDB.posting_date >= start_date)
                .filter(TransactionDB.posting_date <= end_date)
                .filter(*spend_sql_filters())
                .group_by("period")
                .order_by("period")
            )

            results = []
            for row in query:
                results.append(
                    {
                        "period": row.period,
                        "amount": _cents(row.amount),
                        "transaction_count": row.transaction_count,
                    }
                )

            return results

    def get_income_sources(
        self,
        start_date: date,
        end_date: date,
        top_n: int = 10,
    ) -> dict[str, Decimal]:
        """
        Get income breakdown by merchant/source.

        Args:
            start_date: Start date
            end_date: End date
            top_n: Number of top income sources to return

        Returns:
            Dict mapping merchant/source to income amount
        """
        with self._open_session() as session:
            query = (
                session.query(
                    TransactionDB.merchant, func.sum(TransactionDB.amount_signed).label("amount")
                )
                .filter(TransactionDB.posting_date >= start_date)
                .filter(TransactionDB.posting_date <= end_date)
                .filter(*income_sql_filters())
                # merchant is nullable=False default="" so isnot(None) is a no-op that
                # lets unresolved-merchant income collapse into one blank "" bucket.
                # Match get_spending_by_merchant and exclude the empty bucket.
                .filter(TransactionDB.merchant != "")
                .outerjoin(
                    TransactionTreatmentDB,
                    TransactionTreatmentDB.txn_id == TransactionDB.txn_id,
                )
                .filter(income_treatment_sql_filter())
                .group_by(TransactionDB.merchant)
                .order_by(desc("amount"))
                .limit(top_n)
            )

            result = {}
            for row in query:
                # Quantize like the other aggregates (func.sum over Numeric can come
                # back as a float); keeps income totals exact cents.
                result[row.merchant] = _cents(row.amount)

            return result
