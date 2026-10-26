"""Local data quality checks for reporting confidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from ledger.config.repository import SQLiteConfigRepository
from ledger.core.enums import Confidence
from ledger.query import LedgerQueryService
from ledger.reporting import ReportSnapshotService
from ledger.storage.database import (
    DataQualityCheckDB,
    ImportRunDB,
    InsightFindingDB,
    TransactionDB,
    session_scope,
)
from ledger.storage.migrations import ensure_schema, validate_database
from ledger.utils.dates import naive_utc_now


@dataclass(frozen=True)
class DataQualityIssue:
    check_name: str
    severity: str
    status: str
    issue_count: int
    details: dict[str, Any]


class DataQualityService:
    """Run and record local data quality checks."""

    def __init__(self, db_path: Path, *, prepare_schema: bool = True):
        self.db_path = db_path
        if prepare_schema:
            ensure_schema(db_path)

    def run_checks(self) -> list[DataQualityIssue]:
        service = LedgerQueryService(self.db_path)
        issues = [
            self._schema_check(),
            self._config_check(),
            self._missing_enrichment_check(),
            self._low_confidence_check(),
            self._transfer_check(service),
            self._duplicate_check(service),
            self._report_freshness_check(service),
            self._insight_freshness_check(service),
            self._failed_imports_check(),
        ]
        self._record(issues)
        return issues

    def latest(self, limit: int = 50) -> list[dict[str, Any]]:
        with session_scope(self.db_path) as session:
            rows = (
                session.query(DataQualityCheckDB)
                .order_by(DataQualityCheckDB.run_at.desc(), DataQualityCheckDB.severity)
                .limit(limit)
                .all()
            )
            return [
                {
                    "Run At": row.run_at,
                    "Check": row.check_name,
                    "Severity": row.severity,
                    "Status": row.status,
                    "Issues": row.issue_count,
                    "Details": row.details or {},
                }
                for row in rows
            ]

    def _schema_check(self) -> DataQualityIssue:
        result = validate_database(self.db_path, record=False)
        return DataQualityIssue(
            check_name="schema",
            severity="error" if not result.ok else "info",
            status="pass" if result.ok else "fail",
            issue_count=result.counts.get("missing", 0),
            details=result.details,
        )

    def _config_check(self) -> DataQualityIssue:
        result = SQLiteConfigRepository(self.db_path).validate()
        return DataQualityIssue(
            check_name="runtime_config",
            severity="error" if not result.ok else "info",
            status="pass" if result.ok else "fail",
            issue_count=len(result.issues),
            details={"issues": result.issues},
        )

    def _missing_enrichment_check(self) -> DataQualityIssue:
        with session_scope(self.db_path) as session:
            missing = (
                session.query(TransactionDB)
                .filter((TransactionDB.merchant == "") | (TransactionDB.category == "Other"))
                .count()
            )
        return DataQualityIssue(
            check_name="missing_enrichment",
            severity="warning" if missing else "info",
            status="warn" if missing else "pass",
            issue_count=missing,
            details={},
        )

    def _low_confidence_check(self) -> DataQualityIssue:
        with session_scope(self.db_path) as session:
            low_confidence = (
                session.query(TransactionDB)
                .filter(
                    (
                        TransactionDB.merchant_confidence.notin_(
                            [Confidence.RULE, Confidence.MANUAL, Confidence.HIGH]
                        )
                    )
                    | (
                        TransactionDB.category_confidence.notin_(
                            [Confidence.RULE, Confidence.MANUAL, Confidence.HIGH]
                        )
                    )
                )
                .count()
            )
        return DataQualityIssue(
            check_name="low_confidence_rules",
            severity="warning" if low_confidence else "info",
            status="warn" if low_confidence else "pass",
            issue_count=low_confidence,
            details={},
        )

    def _transfer_check(self, service: LedgerQueryService) -> DataQualityIssue:
        stats = service.get_transfer_stats()
        unmatched = int(stats.get("unmatched_transfers", 0))
        return DataQualityIssue(
            check_name="unmatched_transfers",
            severity="warning" if unmatched else "info",
            status="warn" if unmatched else "pass",
            issue_count=unmatched,
            details=stats,
        )

    def _duplicate_check(self, service: LedgerQueryService) -> DataQualityIssue:
        count = len(service.get_duplicate_candidates(threshold_days=2))
        return DataQualityIssue(
            check_name="duplicate_candidates",
            severity="warning" if count else "info",
            status="warn" if count else "pass",
            issue_count=count,
            details={},
        )

    def _report_freshness_check(self, service: LedgerQueryService) -> DataQualityIssue:
        snapshot = ReportSnapshotService(self.db_path).latest_snapshot()
        _earliest, latest = service.get_date_range()
        stale = False
        if latest and snapshot:
            stale = snapshot["created_at"].date() < latest - timedelta(days=1)
        elif latest and not snapshot:
            stale = True
        return DataQualityIssue(
            check_name="report_freshness",
            severity="warning" if stale else "info",
            status="warn" if stale else "pass",
            issue_count=1 if stale else 0,
            details={
                "latest_transaction": str(latest),
                "snapshot": str(snapshot["created_at"]) if snapshot else None,
            },
        )

    def _insight_freshness_check(self, service: LedgerQueryService) -> DataQualityIssue:
        _earliest, latest = service.get_date_range()
        if latest is None:
            issue_count = 0
            details: dict[str, Any] = {"latest_transaction": None, "latest_finding": None}
        else:
            with session_scope(self.db_path) as session:
                latest_finding = (
                    session.query(InsightFindingDB)
                    .order_by(InsightFindingDB.created_at.desc())
                    .first()
                )
                latest_finding_at = latest_finding.created_at if latest_finding else None
            stale = latest_finding_at is None or latest_finding_at.date() < latest - timedelta(
                days=1
            )
            issue_count = 1 if stale else 0
            details = {
                "latest_transaction": str(latest),
                "latest_finding": str(latest_finding_at) if latest_finding_at else None,
            }
        return DataQualityIssue(
            check_name="insight_freshness",
            severity="warning" if issue_count else "info",
            status="warn" if issue_count else "pass",
            issue_count=issue_count,
            details=details,
        )

    def _failed_imports_check(self) -> DataQualityIssue:
        with session_scope(self.db_path) as session:
            failed = session.query(ImportRunDB).filter(ImportRunDB.errors > 0).count()
        return DataQualityIssue(
            check_name="failed_imports",
            severity="warning" if failed else "info",
            status="warn" if failed else "pass",
            issue_count=failed,
            details={},
        )

    def _record(self, issues: list[DataQualityIssue]) -> None:
        run_at = naive_utc_now()
        with session_scope(self.db_path) as session:
            for issue in issues:
                session.add(
                    DataQualityCheckDB(
                        run_at=run_at,
                        check_name=issue.check_name,
                        severity=issue.severity,
                        status=issue.status,
                        issue_count=issue.issue_count,
                        details=issue.details,
                    )
                )
