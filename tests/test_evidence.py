from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from typer.testing import CliRunner

from ledger.cli import app
from ledger.evidence import EvidenceService, latest_audit_summary
from ledger.storage.database import (
    DataQualityCheckDB,
    DBOperationAuditDB,
    ReportSnapshotDB,
    init_database,
    session_scope,
)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _write_audit_summary(run_dir: Path, *, status: str = "pass") -> None:
    run_dir.mkdir(parents=True)
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "status": status,
                "counts": {
                    "pdfs": 2,
                    "source_rows": 10,
                    "db_rows": 10,
                    "row_mismatches": 0 if status == "pass" else 1,
                    "balance_mismatches": 0,
                    "processing_errors": 0,
                    "statement_validation_errors": 0,
                    "processed_file_hash_matches": 2,
                },
            }
        ),
        encoding="utf-8",
    )


def test_latest_audit_summary_uses_newest_summary_file(tmp_path: Path) -> None:
    _write_audit_summary(tmp_path / "20260515T120000Z", status="fail")
    _write_audit_summary(tmp_path / "20260516T120000Z", status="pass")

    summary = latest_audit_summary(tmp_path)

    assert summary["status"] == "pass"
    assert summary["run_dir"].endswith("20260516T120000Z")
    assert summary["counts"]["pdfs"] == 2


def test_latest_audit_summary_reports_unknown_when_missing(tmp_path: Path) -> None:
    summary = latest_audit_summary(tmp_path / "missing")

    assert summary == {
        "status": "unknown",
        "run_dir": None,
        "counts": {},
        "detail": "No statement audit evidence found.",
    }


def test_evidence_summary_flags_failing_audit_pending_review_and_stale_snapshot(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ledger.db"
    audit_runs_dir = tmp_path / "audit-runs"
    init_database(db_path)
    _write_audit_summary(audit_runs_dir / "20260516T120000Z", status="fail")
    now = _now()
    with session_scope(db_path) as session:
        session.add(
            ReportSnapshotDB(
                created_at=now - timedelta(days=3),
                source="test",
                source_kind="reporting",
                schema_version=2,
                period_start=None,
                period_end=None,
                status="success",
                payload={},
            )
        )
        session.add(
            DataQualityCheckDB(
                run_at=now,
                check_name="schema",
                severity="error",
                status="fail",
                issue_count=1,
                details={},
            )
        )

    summary = EvidenceService(db_path, audit_runs_dir=audit_runs_dir).summary(
        pending_reviews=2,
        reference_time=now,
    )

    assert summary["audit"]["status"] == "fail"
    assert summary["review"]["status"] == "warn"
    assert summary["snapshots"]["status"] == "warn"
    assert summary["quality"]["status"] == "fail"
    assert summary["database"]["status"] == "pass"
    assert any("Review Queue" in action for action in summary["next_actions"])


def test_evidence_summary_reports_backup_history(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    now = _now()
    with session_scope(db_path) as session:
        session.add(
            DBOperationAuditDB(
                operation="backup",
                status="success",
                started_at=now,
                finished_at=now,
                db_path=str(db_path),
                artifact_path=str(tmp_path / "backup.db"),
                counts={"bytes": 100},
                warnings=[],
                errors=[],
                details={},
            )
        )

    summary = EvidenceService(db_path, audit_runs_dir=tmp_path / "missing").summary(
        pending_reviews=0,
        reference_time=now,
    )

    assert summary["backups"]["status"] == "pass"
    assert summary["backups"]["latest_at"] == now.isoformat()


def test_evidence_cli_json_returns_stable_keys(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    audit_runs_dir = tmp_path / "audit-runs"
    init_database(db_path)
    _write_audit_summary(audit_runs_dir / "20260516T120000Z", status="pass")

    result = CliRunner().invoke(
        app,
        [
            "evidence",
            "status",
            "--db",
            str(db_path),
            "--audit-runs-dir",
            str(audit_runs_dir),
            "--json",
        ],
    )

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert {
        "audit",
        "imports",
        "review",
        "quality",
        "snapshots",
        "database",
        "backups",
        "next_actions",
    } <= payload.keys()
    assert payload["audit"]["status"] == "pass"


def test_evidence_cli_text_prints_compact_summary(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    init_database(db_path)

    result = CliRunner().invoke(app, ["evidence", "status", "--db", str(db_path)])

    assert result.exit_code == 0
    assert "Ledger Evidence" in result.stdout
    assert "Statement Audit" in result.stdout
