"""Focused reports query module."""

from __future__ import annotations

from datetime import date

from ledger.query.base import QueryModule
from ledger.storage.database import (
    RecurringPatternDB,
)


class ReportQueries(QueryModule):
    """Focused query methods owned by the reports namespace."""

    def get_health_scores(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
        limit: int = 30,
    ) -> list[dict]:
        """
        Get financial health scores.

        Args:
            start_date: Filter scores on or after this date
            end_date: Filter scores on or before this date
            limit: Maximum number of scores to return

        Returns:
            List of health score dictionaries
        """
        from ledger.storage.database import FinancialHealthScoreDB

        with self._open_session() as session:
            query = session.query(FinancialHealthScoreDB)

            if start_date:
                query = query.filter(FinancialHealthScoreDB.score_date >= start_date)
            if end_date:
                query = query.filter(FinancialHealthScoreDB.score_date <= end_date)

            query = query.order_by(FinancialHealthScoreDB.score_date.desc()).limit(limit)
            scores = query.all()

            return [
                {
                    "score_date": score.score_date,
                    "overall_score": score.overall_score,
                    "cashflow_health": score.cashflow_health,
                    "spending_consistency": score.spending_consistency,
                    "savings_rate": float(score.savings_rate),
                    # None = not assessable (relative-only balance series); keep it None
                    # rather than coercing to 0.0, which would read as a real "0 months".
                    "emergency_fund_months": (
                        float(score.emergency_fund_months)
                        if score.emergency_fund_months is not None
                        else None
                    ),
                    "debt_ratio": (
                        float(score.debt_ratio) if score.debt_ratio is not None else None
                    ),
                    "score_trend_7d": score.score_trend_7d,
                    "score_change_7d": score.score_change_7d,
                    "risk_factors": score.risk_factors.split(",") if score.risk_factors else [],
                }
                for score in scores
            ]

    def get_latest_health_score(self) -> dict | None:
        """Get the most recent health score."""
        scores = self.get_health_scores(limit=1)
        return scores[0] if scores else None

    def get_recurring_patterns(
        self,
        pattern_type: str | None = None,
        is_active: bool | None = None,
        limit: int = 100,
    ) -> list[dict]:
        """
        Get detected recurring patterns.

        Args:
            pattern_type: Filter by pattern type (subscription, bill, income, transfer)
            is_active: Filter by active status
            limit: Maximum patterns to return

        Returns:
            List of pattern dictionaries
        """

        with self._open_session() as session:
            query = session.query(RecurringPatternDB)

            if pattern_type:
                query = query.filter(RecurringPatternDB.pattern_type == pattern_type)
            if is_active is not None:
                query = query.filter(RecurringPatternDB.is_active == is_active)

            query = query.order_by(RecurringPatternDB.last_occurrence.desc()).limit(limit)
            patterns = query.all()

            return [
                {
                    "pattern_id": p.pattern_id,
                    "pattern_type": p.pattern_type,
                    "merchant": p.merchant,
                    "category": p.category,
                    "institution": p.institution,
                    "account_id": p.account_id,
                    "frequency": p.frequency,
                    "expected_day_of_period": p.expected_day_of_period,
                    "is_fixed_amount": p.is_fixed_amount,
                    "expected_amount": float(p.expected_amount),
                    "amount_std_dev": float(p.amount_std_dev),
                    "confidence": float(p.confidence),
                    "detection_method": p.detection_method,
                    "occurrence_count": p.occurrence_count,
                    "first_occurrence": p.first_occurrence,
                    "last_occurrence": p.last_occurrence,
                    "next_expected_date": p.next_expected_date,
                    "is_active": p.is_active,
                    "missed_count": p.missed_count,
                }
                for p in patterns
            ]

    def get_latest_import_run(self) -> dict | None:
        """Get the most recent import run summary (for the overview page)."""
        from ledger.storage.database import ImportRunDB

        with self._open_session() as session:
            row = session.query(ImportRunDB).order_by(ImportRunDB.run_at.desc()).first()
            if row is None:
                return None
            return {
                "run_at": row.run_at,
                "files_processed": row.files_processed,
                "transactions_imported": row.transactions_imported,
                "transfers_imported": row.transfers_imported,
                "errors": row.errors,
            }
