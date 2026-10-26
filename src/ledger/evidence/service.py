"""Read-only ledger evidence summaries."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from ledger.import_review import pending_review_count_if_present
from ledger.imports.pdf.readiness import FINALIZABLE_STATUSES
from ledger.runtime import resolve_runtime_path
from ledger.storage.database import (
    DataQualityCheckDB,
    DBOperationAuditDB,
    ImportRunDB,
    ReportSnapshotDB,
    session_scope,
)
from ledger.storage.migrations import REQUIRED_TABLES
from ledger.utils.dates import naive_utc_now


def default_audit_runs_dir() -> Path:
    override = os.environ.get("LEDGER_AUDIT_RUNS_DIR")
    return resolve_runtime_path(Path(override) if override else None, "audit-runs")


def _iso(value: object) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    return str(value)


def _when(value: object) -> str | None:
    """Human-facing timestamp for detail strings: minute precision.

    ``_iso`` stays microsecond-exact for the machine-read ``latest_at`` fields;
    display strings omit seconds and fractional seconds for readability."""
    if value is None:
        return None
    if hasattr(value, "strftime"):
        return str(value.strftime("%Y-%m-%d %H:%M"))
    return str(value)


def latest_audit_summary(audit_runs_dir: Path | None = None) -> dict[str, Any]:
    """Return the newest retained statement audit summary without running an audit."""
    audit_runs_dir = resolve_runtime_path(audit_runs_dir or default_audit_runs_dir(), "audit-runs")
    if not audit_runs_dir.exists():
        return {
            "status": "unknown",
            "run_dir": None,
            "counts": {},
            "detail": "No statement audit evidence found.",
        }

    summaries = sorted(audit_runs_dir.glob("*/summary.json"), reverse=True)
    if not summaries:
        return {
            "status": "unknown",
            "run_dir": None,
            "counts": {},
            "detail": "No statement audit evidence found.",
        }

    path = summaries[0]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "status": "unknown",
            "run_dir": str(path.parent),
            "counts": {},
            "detail": f"Could not read statement audit evidence: {exc}",
        }

    counts = payload.get("counts") or {}
    status = str(payload.get("status", "unknown"))
    by_type = payload.get("mismatches_by_type") or {}
    row_mismatches = int(counts.get("row_mismatches", 0))
    balance_mismatches = int(counts.get("balance_mismatches", 0))

    # Row and balance mismatches need completely different responses -- a row
    # mismatch means transactions are missing or extra relative to the source PDF,
    # while a balance mismatch usually just means no closing balance was captured.
    # Summing them into one "mismatches" number hid that, and mismatches_by_type
    # (already computed by the audit) had no consumer anywhere.
    detail_parts = [
        f"{int(counts.get('pdfs', 0)):,} PDFs",
        f"{int(counts.get('source_rows', 0)):,} rows",
    ]
    if row_mismatches:
        missing = int(counts.get("source_rows", 0)) - int(counts.get("db_rows", 0))
        suffix = f" ({missing:,} row(s) missing)" if missing > 0 else ""
        detail_parts.append(f"{row_mismatches:,} row mismatches{suffix}")
    if balance_mismatches:
        detail_parts.append(f"{balance_mismatches:,} balance mismatches")
    if not row_mismatches and not balance_mismatches:
        detail_parts.append("no mismatches")

    return {
        "status": status,
        "run_dir": str(path.parent),
        "counts": counts,
        "mismatches_by_type": by_type,
        "row_mismatches": row_mismatches,
        "balance_mismatches": balance_mismatches,
        "detail": ", ".join(detail_parts),
    }


class EvidenceService:
    """Build a compact read-only confidence summary for the local ledger."""

    def __init__(
        self,
        db_path: Path,
        *,
        audit_runs_dir: Path | None = None,
        import_runs_dir: Path | None = None,
    ) -> None:
        import_runs_dir = resolve_runtime_path(import_runs_dir, "import-runs")
        self.db_path = db_path
        self.audit_runs_dir = audit_runs_dir or default_audit_runs_dir()
        self.import_runs_dir = import_runs_dir

    def summary(
        self,
        *,
        pending_reviews: int | None = None,
        reference_time: datetime | None = None,
    ) -> dict[str, Any]:
        """Build the summary.

        ``pending_reviews`` defaults to None meaning "count it": it used to default to
        zero, so a CLI-only caller (which passes nothing) was told review was clear in
        exactly the window where it is not — mid-import, with the decisions still
        sitting in the run's staging database. The UI keeps passing its own count.
        """
        reference_time = reference_time or naive_utc_now()
        audit = latest_audit_summary(self.audit_runs_dir)
        imports = self._imports()
        review = self._review(
            self._pending_reviews() if pending_reviews is None else pending_reviews
        )
        quality = self._quality()
        snapshots = self._snapshots(reference_time)
        backups = self._backups()
        database = self._database()
        next_actions = self._next_actions(
            audit=audit,
            imports=imports,
            review=review,
            quality=quality,
            snapshots=snapshots,
            backups=backups,
            database=database,
        )
        return {
            "audit": audit,
            "imports": imports,
            "review": review,
            "quality": quality,
            "snapshots": snapshots,
            "database": database,
            "backups": backups,
            "next_actions": next_actions,
        }

    def _imports(self) -> dict[str, Any]:
        latest_manifest = self._latest_import_manifest()
        latest_db_import = self._latest_db_import()
        if latest_manifest:
            status = str(latest_manifest.get("status", "unknown"))
            run_dir = latest_manifest.get("run_dir")
            row_count = sum(
                int(item.get("transaction_count", 0)) for item in latest_manifest.get("pdfs", [])
            )
            evidence_status = "fail" if status == "failed" else "pass"
            return {
                "status": evidence_status,
                "manifest_status": status,
                "run_dir": run_dir,
                "latest_at": None,
                "detail": f"Latest manifest {status}; {row_count:,} staged row(s)",
            }
        if latest_db_import:
            status = "fail" if int(latest_db_import["errors"]) else "pass"
            return {
                "status": status,
                "manifest_status": None,
                "run_dir": None,
                "latest_at": _iso(latest_db_import["run_at"]),
                "detail": (
                    f"Latest import {_when(latest_db_import['run_at'])}; "
                    f"{int(latest_db_import['transactions_imported']):,} transaction(s)"
                ),
            }
        return {
            "status": "unknown",
            "manifest_status": None,
            "run_dir": None,
            "latest_at": None,
            "detail": "No import evidence found.",
        }

    def _pending_reviews(self) -> int:
        """Count review rows that still need a decision, wherever they live.

        Two queues can hold them and only their sum is the truth an agent needs:

        * the live database's own ``import_review`` table (promoted by the last
          finalize, so normally fully decided), and
        * the newest import run's STAGING database, but only while that run can still
          be finalized. A finalized or cancelled run keeps its staging.db on disk
          (finalize cleanup removes only the CSVs and staged inbox), and counting those
          leftovers would report review work that no promotion is waiting on.

        Both reads are non-mutating: ``pending_review_count`` would create the schema.
        """
        return pending_review_count_if_present(self.db_path) + self._active_run_pending()

    def _active_run_pending(self) -> int:
        if not self.import_runs_dir.exists():
            return 0
        # Run directories are UTC-timestamp names, so reverse order is newest first —
        # the same "latest run" the imports section reports on.
        run_states = sorted(self.import_runs_dir.glob("*/run.json"), reverse=True)
        if not run_states:
            return 0
        try:
            run_state = json.loads(run_states[0].read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
            return 0
        if str(run_state.get("status", "")) not in FINALIZABLE_STATUSES:
            return 0
        staging_db = str(run_state.get("staging_db_path") or "")
        if not staging_db:
            return 0
        return pending_review_count_if_present(Path(staging_db))

    def _review(self, pending_reviews: int) -> dict[str, Any]:
        if pending_reviews:
            return {
                "status": "warn",
                "pending": pending_reviews,
                "detail": f"{pending_reviews:,} pending review row(s)",
            }
        return {"status": "pass", "pending": 0, "detail": "No pending review rows"}

    def _quality(self) -> dict[str, Any]:
        try:
            with session_scope(self.db_path) as session:
                latest_run = (
                    session.query(DataQualityCheckDB.run_at)
                    .order_by(DataQualityCheckDB.run_at.desc())
                    .first()
                )
                if latest_run is None:
                    return {
                        "status": "unknown",
                        "latest_at": None,
                        "issue_count": 0,
                        "detail": "No data quality evidence found.",
                    }
                rows = (
                    session.query(DataQualityCheckDB)
                    .filter(DataQualityCheckDB.run_at == latest_run.run_at)
                    .all()
                )
                latest_at = latest_run.run_at
                statuses = {str(row.status) for row in rows}
                issue_count = sum(int(row.issue_count or 0) for row in rows)
        except SQLAlchemyError as exc:
            return {
                "status": "unknown",
                "latest_at": None,
                "issue_count": 0,
                "detail": f"Could not read data quality evidence: {exc}",
            }

        if "fail" in statuses:
            status = "fail"
        elif "warn" in statuses:
            status = "warn"
        else:
            status = "pass"
        return {
            "status": status,
            "latest_at": _iso(latest_at),
            "issue_count": issue_count,
            "detail": f"Latest checks {status}; {issue_count:,} issue(s)",
        }

    def _snapshots(self, reference_time: datetime) -> dict[str, Any]:
        try:
            with session_scope(self.db_path) as session:
                row = (
                    session.query(ReportSnapshotDB)
                    .order_by(ReportSnapshotDB.created_at.desc())
                    .first()
                )
                latest_at = row.created_at if row else None
        except SQLAlchemyError as exc:
            return {
                "status": "unknown",
                "latest_at": None,
                "detail": f"Could not read report snapshot evidence: {exc}",
            }
        if latest_at is None:
            return {
                "status": "unknown",
                "latest_at": None,
                "detail": "No report snapshot evidence found.",
            }
        stale = latest_at < reference_time - timedelta(days=2)
        status = "warn" if stale else "pass"
        return {
            "status": status,
            "latest_at": _iso(latest_at),
            "detail": f"Latest snapshot {_when(latest_at)}",
        }

    def _backups(self) -> dict[str, Any]:
        try:
            with session_scope(self.db_path) as session:
                row = (
                    session.query(DBOperationAuditDB)
                    .filter(DBOperationAuditDB.operation == "backup")
                    .filter(DBOperationAuditDB.status == "success")
                    .order_by(DBOperationAuditDB.started_at.desc())
                    .first()
                )
                latest_at = row.started_at if row else None
                artifact = row.artifact_path if row else None
        except SQLAlchemyError as exc:
            return {
                "status": "unknown",
                "latest_at": None,
                "artifact": None,
                "detail": f"Could not read backup evidence: {exc}",
            }
        if latest_at is None:
            return {
                "status": "unknown",
                "latest_at": None,
                "artifact": None,
                "detail": "No backup evidence found.",
            }
        return {
            "status": "pass",
            "latest_at": _iso(latest_at),
            "artifact": artifact,
            "detail": f"Latest backup {_when(latest_at)}",
        }

    def _database(self) -> dict[str, Any]:
        if not self.db_path.exists():
            return {
                "status": "fail",
                "missing_tables": sorted(REQUIRED_TABLES),
                "detail": f"Database not found: {self.db_path}",
            }
        try:
            with closing(sqlite3.connect(self.db_path)) as conn:
                rows = conn.execute(
                    "select name from sqlite_master where type = 'table'"
                ).fetchall()
        except sqlite3.Error as exc:
            return {
                "status": "fail",
                "missing_tables": [],
                "detail": f"Could not inspect database: {exc}",
            }
        existing = {str(row[0]) for row in rows}
        missing = sorted(REQUIRED_TABLES - existing)
        if missing:
            return {
                "status": "fail",
                "missing_tables": missing,
                "detail": f"{len(missing):,} required table(s) missing",
            }
        return {
            "status": "pass",
            "missing_tables": [],
            "detail": "Schema valid",
        }

    def _latest_db_import(self) -> dict[str, Any] | None:
        try:
            with session_scope(self.db_path) as session:
                row = session.query(ImportRunDB).order_by(ImportRunDB.run_at.desc()).first()
                if row is None:
                    return None
                return {
                    "run_at": row.run_at,
                    "transactions_imported": row.transactions_imported,
                    "errors": row.errors,
                }
        except SQLAlchemyError:
            return None

    def _latest_import_manifest(self) -> dict[str, Any] | None:
        if not self.import_runs_dir.exists():
            return None
        manifests = sorted(self.import_runs_dir.glob("*/manifest.json"), reverse=True)
        if not manifests:
            return None
        try:
            return json.loads(manifests[0].read_text(encoding="utf-8"))
        except OSError, json.JSONDecodeError:
            return None

    @staticmethod
    def _next_actions(**sections: dict[str, Any]) -> list[str]:
        actions: list[str] = []
        if sections["audit"]["status"] != "pass":
            actions.append("Run ./scripts/audit-statements.")
        if sections["imports"]["status"] == "fail":
            actions.append("Inspect the latest import manifest.")
        if sections["review"]["status"] != "pass":
            actions.append("Resolve Review Queue decisions.")
        if sections["quality"]["status"] in {"fail", "warn", "unknown"}:
            actions.append("Run ledger quality run.")
        if sections["snapshots"]["status"] in {"warn", "unknown"}:
            actions.append("Run ledger reports refresh.")
        if sections["database"]["status"] != "pass":
            actions.append("Run ledger db validate.")
        if sections["backups"]["status"] == "unknown":
            actions.append("Create a local DB backup.")
        return actions
