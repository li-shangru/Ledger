"""Apply runtime merchant/category rules to historical ledger rows."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ledger.analysis import AnalysisRunner
from ledger.config.repository import SQLiteConfigRepository
from ledger.config.schemas import merchant_match_texts
from ledger.core.enums import Confidence
from ledger.core.results import RunResult
from ledger.storage.database import (
    RuleApplicationRunDB,
    TransactionDB,
    init_database,
    session_scope,
)
from ledger.storage.operations import record_db_operation
from ledger.utils.dates import naive_utc_now


@dataclass(frozen=True)
class RuleApplicationResult:
    """Result of one runtime rule application pass."""

    status: str
    source: str
    transactions_scanned: int
    transactions_updated: int
    changed_merchants: int
    changed_categories: int
    analytics_refreshed: bool
    validation_issues: list[str]
    details: dict[str, Any]

    @property
    def ok(self) -> bool:
        return self.status == "success" and not self.validation_issues


class RuleApplicationService:
    """Propagate canonical merchant profiles into denormalized transaction rows."""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        init_database(self.db_path)

    def apply_all(
        self,
        *,
        source: str = "manual",
        refresh_analytics: bool = True,
    ) -> RuleApplicationResult:
        """Apply current runtime rules and record an audit row."""
        started_at = naive_utc_now()
        repo = SQLiteConfigRepository(self.db_path)
        validation = repo.validate()
        if not validation.ok:
            result = RuleApplicationResult(
                status="validation_failed",
                source=source,
                transactions_scanned=0,
                transactions_updated=0,
                changed_merchants=0,
                changed_categories=0,
                analytics_refreshed=False,
                validation_issues=validation.issues,
                details={"validation_issues": validation.issues},
            )
            self._record_run(result)
            return result

        merchants = repo.load_merchant_rules()
        categories = repo.load_categories()
        category_exclusions = {
            category.name: category.exclude_from_spending for category in categories.categories
        }

        transactions_scanned = 0
        transactions_updated = 0
        changed_merchants: set[str] = set()
        changed_categories: set[str] = set()

        with session_scope(self.db_path) as session:
            rows = session.query(TransactionDB).all()
            transactions_scanned = len(rows)
            for row in rows:
                # row.merchant is a RESOLVED display name, not statement text, so it
                # goes in as display_name (identity match against merchant_norm) rather
                # than as a third raw text. Passed positionally it was pattern-matched,
                # and any name containing a broad rule's pattern inherited that rule's
                # exclude_from_spending — flipping what counts as spending on a row the
                # raw text never matched.
                merchant, category, merchant_exclude = merchants.find_merchant(
                    *merchant_match_texts(
                        str(row.merchant_raw or ""),
                        str(row.description or ""),
                    ),
                    display_name=str(row.merchant or ""),
                )
                if not merchant:
                    continue

                if (
                    row.category_confidence == Confidence.MANUAL
                    or row.merchant_confidence == Confidence.MANUAL
                ):
                    continue  # manual overrides win permanently

                exclude = bool(merchant_exclude or category_exclusions.get(category, False))
                changed = (
                    row.merchant != merchant
                    or row.category != category
                    or bool(row.exclude_from_spending) != exclude
                    or row.merchant_confidence != Confidence.RULE
                    or row.category_confidence != Confidence.RULE
                )
                if not changed:
                    continue

                row.merchant = merchant
                row.category = category
                row.exclude_from_spending = exclude
                row.merchant_confidence = Confidence.RULE
                row.category_confidence = Confidence.RULE
                changed_merchants.add(merchant)
                changed_categories.add(category)
                transactions_updated += 1

        analytics_refreshed = False
        analysis_details: dict[str, Any] = {}
        if refresh_analytics:
            analysis = AnalysisRunner(self.db_path)
            analysis_details = {
                "patterns": analysis.run_pattern_detection(),
                "trends": analysis.run_trend_aggregation(days=30, months=12),
                "health": analysis.run_health_scoring(days=30),
            }
            analytics_refreshed = True

        snapshot_details: dict[str, Any] = {}
        if analytics_refreshed:
            from ledger.insights import InsightEngine
            from ledger.quality import DataQualityService
            from ledger.reporting import ReportSnapshotService

            snapshot = ReportSnapshotService(self.db_path).create_snapshot(source=source)
            insights = InsightEngine(self.db_path).refresh_period(
                snapshot["period_start"],
                snapshot["period_end"],
            )
            quality = DataQualityService(self.db_path).run_checks()
            snapshot_details = {
                "report_snapshot_id": snapshot.get("id"),
                "insight_findings": insights.counts,
                "quality_checks": len(quality),
                "quality_issues": sum(issue.issue_count for issue in quality),
            }

        result = RuleApplicationResult(
            status="success",
            source=source,
            transactions_scanned=transactions_scanned,
            transactions_updated=transactions_updated,
            changed_merchants=len(changed_merchants),
            changed_categories=len(changed_categories),
            analytics_refreshed=analytics_refreshed,
            validation_issues=[],
            details={
                "changed_merchants": sorted(changed_merchants),
                "changed_categories": sorted(changed_categories),
                "analysis": analysis_details,
                **snapshot_details,
            },
        )
        self._record_run(result)
        # A bulk rules-apply rewrites merchant/category/exclude_from_spending on existing
        # rows, so a snapshot written before it is stale. Record a 'rules_apply' DB-op
        # audit row (in the freshness allowlist in reporting/service.py) so a post-apply
        # dashboard view recomputes instead of serving pre-apply KPIs. Done even when
        # refresh_analytics=False (the rows are mutated regardless).
        record_db_operation(
            self.db_path,
            operation="rules_apply",
            result=RunResult(
                status="success",
                started_at=started_at,
                finished_at=naive_utc_now(),
                counts={
                    "transactions_scanned": transactions_scanned,
                    "transactions_updated": transactions_updated,
                },
                details={
                    "source": source,
                    "changed_merchants": len(changed_merchants),
                    "changed_categories": len(changed_categories),
                },
            ),
        )
        return result

    def latest_run(self) -> dict[str, Any] | None:
        """Return the most recent rule-application audit row."""
        with session_scope(self.db_path) as session:
            row = (
                session.query(RuleApplicationRunDB)
                .order_by(RuleApplicationRunDB.applied_at.desc())
                .first()
            )
            if row is None:
                return None
            return {
                "id": row.id,
                "applied_at": row.applied_at,
                "source": row.source,
                "status": row.status,
                "transactions_updated": row.transactions_updated,
                "merchant_profiles_changed": row.merchant_profiles_changed,
                "categories_changed": row.categories_changed,
                "analytics_refreshed": bool(row.analytics_refreshed),
                "validation_issues": row.validation_issues,
                "details": row.details or {},
            }

    def history(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return recent rule-application runs."""
        with session_scope(self.db_path) as session:
            rows = (
                session.query(RuleApplicationRunDB)
                .order_by(RuleApplicationRunDB.applied_at.desc())
                .limit(limit)
                .all()
            )
            return [
                {
                    "Applied At": row.applied_at,
                    "Source": row.source,
                    "Status": row.status,
                    "Transactions Updated": row.transactions_updated,
                    "Changed Merchants": row.merchant_profiles_changed,
                    "Changed Categories": row.categories_changed,
                    "Analytics": "yes" if row.analytics_refreshed else "no",
                }
                for row in rows
            ]

    def _record_run(self, result: RuleApplicationResult) -> None:
        with session_scope(self.db_path) as session:
            session.add(
                RuleApplicationRunDB(
                    applied_at=naive_utc_now(),
                    source=result.source,
                    status=result.status,
                    merchant_profiles_changed=result.changed_merchants,
                    categories_changed=result.changed_categories,
                    transactions_scanned=result.transactions_scanned,
                    transactions_updated=result.transactions_updated,
                    analytics_refreshed=result.analytics_refreshed,
                    validation_issues=len(result.validation_issues),
                    details=result.details,
                )
            )
