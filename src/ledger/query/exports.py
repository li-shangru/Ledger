"""Focused exports query module."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from ledger.core.types import CategorySpending
from ledger.query.base import QueryModule


class ExportQueries(QueryModule):
    """Focused query methods owned by the exports namespace."""

    def compute_true_cost_kpis(
        self,
        start_date: date,
        end_date: date,
    ) -> dict:
        """Compute reporting KPIs using true-cost adjustment treatments."""
        from ledger.adjustments import AdjustmentDetectionService

        summary = AdjustmentDetectionService(
            self.db_path,
            session_scope_factory=self._session_scope_factory,
            prepare_schema=False,
        ).true_cost_summary(start_date, end_date)
        return {
            "income": summary.adjusted_income,
            "expenses": summary.true_cost,
            "net": summary.net,
            "transaction_count": summary.transaction_count,
            "gross_income": summary.gross_income,
            "gross_spend": summary.gross_spend,
            "offsets": summary.offsets,
            "true_cost": summary.true_cost,
        }

    def get_true_cost_by_category(
        self,
        start_date: date,
        end_date: date,
    ) -> CategorySpending:
        """Get true-cost spending by category for a date range."""
        from ledger.adjustments import AdjustmentDetectionService

        rows = AdjustmentDetectionService(
            self.db_path,
            session_scope_factory=self._session_scope_factory,
            prepare_schema=False,
        ).true_cost_by_category(start_date, end_date)
        return {
            category: values["true_cost"]
            for category, values in rows.items()
            if values["true_cost"] > 0
        }

    def get_true_cost_category_details(
        self,
        start_date: date,
        end_date: date,
    ) -> dict[str, dict[str, Decimal]]:
        """Get gross spend, offsets, and true cost by category."""
        from ledger.adjustments import AdjustmentDetectionService

        return AdjustmentDetectionService(
            self.db_path,
            session_scope_factory=self._session_scope_factory,
            prepare_schema=False,
        ).true_cost_by_category(start_date, end_date)
