"""Regression tests for staged PDF import safety helpers."""

from __future__ import annotations

import contextlib
import importlib.util
import json
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from import_binding_fixtures import bound_import_state
from sqlalchemy import inspect

from ledger.import_review import (
    apply_review_decisions,
    get_review_rows,
    pending_review_count,
    save_review_rows,
    seed_review_candidates,
)
from ledger.imports.pdf import workflow
from ledger.imports.pdf.files import promote_database, write_run_state
from ledger.imports.pdf.manifest import read_manifest
from ledger.imports.pdf.paths import Paths
from ledger.imports.pdf.readiness import (
    PUBLICATION_PROTOCOL_VERSION,
    build_import_readiness,
    database_fingerprint,
    target_db_unchanged,
)
from ledger.processing import ProcessingResult
from ledger.storage.database import get_engine, init_database
from ledger.storage.migrations import ensure_schema

REPO_ROOT = Path(__file__).resolve().parents[1]
PDF_SCRIPT = REPO_ROOT / "docs/agent/skills/ledger-pdf-import/scripts/statement_pdf_to_csv.py"


def load_pdf_script():
    spec = importlib.util.spec_from_file_location("statement_pdf_to_csv", PDF_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_generic_activity_statement_is_not_misrouted_to_investments() -> None:
    """An unrecognized activity label never selects an investment adapter."""
    from ledger.imports.pdf.statement_pdf_to_csv import detect_statement_type

    assert detect_statement_type("Generic Activity Statement") == "unsupported"


def test_transaction_pdf_stage_manifest_and_readiness_delegate_managed_investment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The transaction stage leaves managed investment evidence for its dedicated importer."""
    runs_dir = tmp_path / "runs"
    paths = _make_stage_paths(tmp_path, runs_dir)
    inbox_pdf = paths.pdf_dir / "statement.pdf"

    def managed_result(pdf: Path, *_args, **_kwargs):
        return workflow.statement_pdf_to_csv.ExtractedCsv(
            pdf=pdf,
            csv_path=None,
            processed_pdf=None,
            statement_type="managed_by_investment_importer",
            transaction_count=0,
            skipped_reason="managed_by_investment_importer",
        )

    monkeypatch.setattr(workflow.statement_pdf_to_csv, "process_pdf", managed_result)

    workflow.stage(SimpleNamespace(), paths)

    run_dir = workflow.resolve_run_dir(runs_dir, None)
    manifest = read_manifest(run_dir)
    readiness = build_import_readiness(run_dir)
    assert inbox_pdf.exists()
    assert manifest["status"] == "cancelled"
    assert manifest["left_in_inbox"]["counts"] == {
        "unsupported": 0,
        "managed": 1,
        "duplicates": 0,
        "failed_move": 0,
    }
    assert manifest["left_in_inbox"]["items"] == []
    assert all("remain in data/inbox" not in action for action in manifest["next_actions"])
    assert any(
        "investment adapter" in action
        and "required source layout" in action
        and "investment import" in action
        for action in manifest["next_actions"]
    )
    assert readiness["can_finalize"] is False


def insert_minimal_transaction(db_path: Path, txn_id: str) -> None:
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            """
            insert into transactions (
                txn_id, posting_date, institution, account, account_type, flow,
                cashflow_type, include_in_cashflow, currency, amount_raw, amount_signed,
                description, merchant, category, merchant_confidence, category_confidence,
                exclude_from_spending, merchant_raw, category_raw, source_file, source_path,
                source_period, source_row, imported_at, parser_version, txn_fingerprint,
                day_of_week, day_of_month, week_of_year, is_weekend, is_month_start,
                is_month_end
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                txn_id,
                "2044-02-10",
                "harbor",
                "harbor-chequing",
                "Chequing",
                "expense",
                "expense",
                1,
                "CAD",
                "18.60",
                "-18.60",
                "Card purchase - 7001 SYNTHETIC ACORN CAFE",
                "SYNTHETIC ACORN CAFE",
                "Other",
                "low",
                "low",
                0,
                "SYNTHETIC ACORN CAFE",
                "",
                "statement.csv",
                "2044-02/statement.csv",
                "2044-02",
                1,
                now,
                "test",
                f"fingerprint-{txn_id}",
                2,
                10,
                6,
                0,
                0,
                0,
            ),
        )


def seed_runtime_categories(db_path: Path, *category_names: str) -> None:
    """Seed the authoritative category choices carried by a valid staged database."""
    from ledger.config.repository import SQLiteConfigRepository
    from ledger.config.schemas import CategoriesConfig, CategoryRule, MerchantRulesConfig

    SQLiteConfigRepository(db_path).replace_from_configs(
        MerchantRulesConfig(version="test"),
        CategoriesConfig(
            version="test",
            default_category="Other",
            categories=[
                CategoryRule(name="Other"),
                *(CategoryRule(name=name) for name in category_names),
            ],
        ),
    )


def write_marker_database(path: Path, value: str) -> None:
    path.unlink(missing_ok=True)
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("create table marker (value text not null)")
        connection.execute("insert into marker values (?)", (value,))


def read_marker_database(path: Path) -> str:
    with closing(sqlite3.connect(path)) as connection:
        return str(connection.execute("select value from marker").fetchone()[0])


def test_review_decision_updates_staging_transaction(tmp_path: Path):
    db_path = tmp_path / "staging.db"
    init_database(db_path)
    insert_minimal_transaction(db_path, "txn-review-1")
    seed_runtime_categories(db_path, "Restaurants & Dining")

    seed_review_candidates(
        db_path,
        [
            {
                "key": "merchant:SYNTHETIC ACORN CAFE",
                "merchant": "SYNTHETIC ACORN CAFE",
                "current_category": "Other",
                "txn_ids": ["txn-review-1"],
                "examples": [{"description": "SYNTHETIC ACORN CAFE"}],
                "count": 1,
                "total_abs_amount": Decimal("18.60"),
            }
        ],
    )
    rows = get_review_rows(db_path)
    assert pending_review_count(db_path) == 1

    rows[0]["status"] = "resolved"
    rows[0]["category"] = "Restaurants & Dining"
    save_review_rows(db_path, rows)

    assert pending_review_count(db_path) == 0
    assert apply_review_decisions(db_path) == 1

    with closing(sqlite3.connect(db_path)) as conn:
        merchant, category, merchant_confidence, category_confidence = conn.execute(
            """
            select merchant, category, merchant_confidence, category_confidence
            from transactions
            where txn_id = 'txn-review-1'
            """
        ).fetchone()

    assert merchant == "SYNTHETIC ACORN CAFE"
    assert category == "Restaurants & Dining"
    assert merchant_confidence == "manual"
    assert category_confidence == "manual"


def test_database_fingerprint_detects_target_db_changes(tmp_path: Path):
    db_path = tmp_path / "ledger.db"
    init_database(db_path)

    before = database_fingerprint(db_path)
    same = database_fingerprint(db_path)

    assert before["exists"] is True
    assert before["sha256"] == same["sha256"]
    assert target_db_unchanged(
        {
            "target_db_path": str(db_path),
            "target_db_fingerprint_before": before,
        }
    )

    insert_minimal_transaction(db_path, "txn-live-change")

    after = database_fingerprint(db_path)
    assert after["sha256"] != before["sha256"]
    assert not target_db_unchanged(
        {
            "target_db_path": str(db_path),
            "target_db_fingerprint_before": before,
        }
    )


def test_import_readiness_reports_stale_target_db_blocker(tmp_path: Path):
    run_dir = tmp_path / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    csv_root = run_dir / "csv"
    csv_root.mkdir()
    target_db = tmp_path / "ledger.db"
    staging_db = run_dir / "staging.db"
    init_database(target_db)
    init_database(staging_db)
    before = database_fingerprint(target_db)
    write_run_state(
        run_dir,
        bound_import_state(
            {
                "status": "ready_to_finalize",
                "target_db_path": str(target_db),
                "staging_db_path": str(staging_db),
                "csv_root": str(csv_root),
                "statement_validation": str(run_dir / "statement-validation.json"),
                "pending_review_count": 0,
                "target_db_fingerprint_before": before,
            },
            _make_finalize_paths(tmp_path, run_dir.parent),
        ),
    )

    ready = build_import_readiness(run_dir)
    assert ready["gates"]["target_db_unchanged"] == "pass"
    assert ready["gates"]["staging_db"] == "pass"
    assert ready["gates"]["csv_root"] == "pass"
    assert ready["blockers"] == []

    insert_minimal_transaction(target_db, "txn-live-change")

    stale = build_import_readiness(run_dir)
    assert stale["gates"]["target_db_unchanged"] == "fail"
    assert (
        "Target database changed since stage; cancel and restage this import." in stale["blockers"]
    )


def test_import_readiness_uses_live_review_count_over_stale_run_state(tmp_path: Path):
    run_dir = tmp_path / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    csv_root = run_dir / "csv"
    csv_root.mkdir()
    target_db = tmp_path / "ledger.db"
    staging_db = run_dir / "staging.db"
    init_database(target_db)
    init_database(staging_db)
    write_run_state(
        run_dir,
        bound_import_state(
            {
                "status": "needs_review",
                "target_db_path": str(target_db),
                "staging_db_path": str(staging_db),
                "csv_root": str(csv_root),
                "pending_review_count": 3,
                "target_db_fingerprint_before": database_fingerprint(target_db),
            },
            _make_finalize_paths(tmp_path, run_dir.parent),
        ),
    )

    readiness = build_import_readiness(run_dir)

    assert readiness["counts"]["pending_review"] == 0
    assert readiness["gates"]["review"] == "pass"
    assert readiness["can_finalize"] is True


def test_stage_records_target_db_fingerprint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pdf_dir = tmp_path / "inbox"
    processed_dir = tmp_path / "processed"
    runs_dir = tmp_path / "runs"
    backup_dir = tmp_path / "backups"
    target_db = tmp_path / "ledger.db"
    pdf_dir.mkdir()
    processed_dir.mkdir()
    (pdf_dir / "statement.pdf").write_bytes(b"%PDF fake")
    init_database(target_db)

    paths = Paths(
        pdf_dir=pdf_dir,
        processed_dir=processed_dir,
        db_path=target_db,
        base_db_path=None,
        backup_dir=backup_dir,
        ledger_config=tmp_path / "ledger.toml",
        merchant_rules=tmp_path / "merchants.toml",
        categories=tmp_path / "categories.toml",
        runs_dir=runs_dir,
    )

    def fake_process_pdf(pdf: Path, csv_root: Path, _processed_dir: Path, **_kwargs):
        csv_root.mkdir(parents=True, exist_ok=True)
        csv_path = csv_root / "statement.csv"
        csv_path.write_text("Date,Description,Amount\n", encoding="utf-8")
        return SimpleNamespace(
            pdf=pdf,
            csv_path=csv_path,
            processed_pdf=processed_dir / "2026-05-01-test.pdf",
            statement_type="test",
            transaction_count=1,
            skipped_reason="",
            validation_errors=[],
            validation_warnings=[],
            account_slug="statement",
        )

    monkeypatch.setattr(workflow.statement_pdf_to_csv, "process_pdf", fake_process_pdf)
    monkeypatch.setattr(
        workflow.statement_pdf_to_csv,
        "write_manifest",
        lambda path, _results, dry_run: path.write_text("[]", encoding="utf-8"),
    )
    monkeypatch.setattr(workflow, "load_config", lambda _paths, _runtime_db: object())
    monkeypatch.setattr(
        workflow,
        "process",
        lambda **_kwargs: ProcessingResult(total_scanned=1),
    )
    monkeypatch.setattr(workflow, "validate_processing_result", lambda _result, _path: None)
    monkeypatch.setattr(workflow, "write_result_to_db", lambda *_args: None)
    monkeypatch.setattr(workflow, "build_merchant_review", lambda _result: [])
    monkeypatch.setattr(workflow, "seed_review_candidates", lambda *_args: None)
    monkeypatch.setattr(workflow, "record_db_operation", lambda *_args, **_kwargs: None)

    workflow.stage(SimpleNamespace(), paths)

    [run_dir] = [path for path in runs_dir.iterdir() if path.is_dir()]
    run_state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

    assert run_state["target_db_fingerprint_before"]["exists"] is True
    assert target_db_unchanged(run_state)
    assert read_manifest(run_dir)["source"]["target_db_fingerprint_before"]["exists"] is True


def test_finalize_blocks_when_target_db_changed_since_stage(tmp_path: Path):
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "run-1"
    csv_root = run_dir / "csv"
    target_db = tmp_path / "ledger.db"
    staging_db = run_dir / "staging.db"
    csv_root.mkdir(parents=True)
    init_database(target_db)
    init_database(staging_db)
    before = database_fingerprint(target_db)
    insert_minimal_transaction(target_db, "txn-live-change")
    write_run_state(
        run_dir,
        bound_import_state(
            {
                "status": "ready_to_finalize",
                "target_db_path": str(target_db),
                "staging_db_path": str(staging_db),
                "csv_root": str(csv_root),
                "pending_review_count": 0,
                "target_db_fingerprint_before": before,
            },
            _make_finalize_paths(tmp_path, run_dir.parent),
        ),
    )
    paths = Paths(
        pdf_dir=tmp_path / "inbox",
        processed_dir=tmp_path / "processed",
        db_path=target_db,
        base_db_path=None,
        backup_dir=tmp_path / "backups",
        ledger_config=tmp_path / "ledger.toml",
        merchant_rules=tmp_path / "merchants.toml",
        categories=tmp_path / "categories.toml",
        runs_dir=runs_dir,
    )

    with pytest.raises(SystemExit, match="Target database changed since stage"):
        workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    run_state_after = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert run_state_after["status"] == "failed"
    assert run_state_after["failure_reason"] == "target_db_changed_since_stage"
    with closing(sqlite3.connect(target_db)) as conn:
        count = conn.execute(
            "select count(*) from transactions where txn_id = 'txn-live-change'"
        ).fetchone()[0]
    assert count == 1


def test_promote_database_backs_up_then_atomically_swaps(tmp_path: Path):
    from ledger.imports.pdf.files import promote_database

    target = tmp_path / "ledger.db"
    staging = tmp_path / "staging.db"
    write_marker_database(target, "OLD-DB")
    write_marker_database(staging, "NEW-DB")

    backup = promote_database(staging, target, tmp_path / "backups")

    assert read_marker_database(target) == "NEW-DB"  # staging promoted onto target
    assert backup is not None and read_marker_database(backup) == "OLD-DB"
    # No leftover promote temp files (unique-name scheme — glob to catch any PID/uuid variant).
    assert not list(target.parent.glob(f".{target.name}.promote-*.tmp"))


def test_promote_database_skips_backup_when_target_absent(tmp_path: Path):
    from ledger.imports.pdf.files import promote_database

    target = tmp_path / "ledger.db"  # does not exist yet
    staging = tmp_path / "staging.db"
    write_marker_database(staging, "NEW-DB")

    backup = promote_database(staging, target, tmp_path / "backups")

    assert read_marker_database(target) == "NEW-DB"
    assert backup is None


def test_finalize_promotes_staging_and_marks_finalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "run-1"
    csv_root = run_dir / "csv"
    target_db = tmp_path / "ledger.db"
    staging_db = run_dir / "staging.db"
    csv_root.mkdir(parents=True)
    init_database(target_db)
    init_database(staging_db)
    insert_minimal_transaction(staging_db, "txn-staged")  # present only in staging
    before = database_fingerprint(target_db)  # target unchanged -> fingerprint will match
    (run_dir / "merchant-review.json").write_text("[]", encoding="utf-8")
    write_run_state(
        run_dir,
        bound_import_state(
            {
                "status": "ready_to_finalize",
                "target_db_path": str(target_db),
                "staging_db_path": str(staging_db),
                "csv_root": str(csv_root),
                "staging_inbox": str(run_dir / "inbox"),
                "merchant_review_json": str(run_dir / "merchant-review.json"),
                "pending_review_count": 0,
                "target_db_fingerprint_before": before,
                "pdfs": [],
            },
            _make_finalize_paths(tmp_path, run_dir.parent),
        ),
    )
    paths = Paths(
        pdf_dir=tmp_path / "inbox",
        processed_dir=tmp_path / "processed",
        db_path=target_db,
        base_db_path=None,
        backup_dir=tmp_path / "backups",
        ledger_config=tmp_path / "ledger.toml",
        merchant_rules=tmp_path / "merchants.toml",
        categories=tmp_path / "categories.toml",
        runs_dir=runs_dir,
    )

    # Mock the heavy import/post-processing steps; keep promote_database real.
    monkeypatch.setattr(workflow, "load_config", lambda _paths, _db: object())
    monkeypatch.setattr(workflow, "run_migrations", lambda _db: None)
    monkeypatch.setattr(workflow, "process", lambda **_kwargs: ProcessingResult(total_scanned=1))
    monkeypatch.setattr(workflow, "validate_processing_result", lambda *_args: None)
    monkeypatch.setattr(workflow, "write_result_to_db", lambda *_args: None)
    monkeypatch.setattr(workflow, "build_merchant_review", lambda _result: [])
    monkeypatch.setattr(workflow, "seed_review_candidates", lambda *_args: None)
    monkeypatch.setattr(workflow, "get_review_rows", lambda _db: [])
    monkeypatch.setattr(workflow, "apply_review_decisions", lambda _db: 0)
    monkeypatch.setattr(workflow, "pending_review_count", lambda _db: 0)
    monkeypatch.setattr(workflow, "run_post_processing", lambda _db, _config: {})
    monkeypatch.setattr(workflow, "record_processed_files", lambda *_args: None)
    monkeypatch.setattr(workflow, "record_db_operation", lambda *_args, **_kwargs: None)

    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    # The staged row was atomically promoted onto the live target DB.
    with closing(sqlite3.connect(target_db)) as conn:
        promoted = conn.execute(
            "select count(*) from transactions where txn_id = 'txn-staged'"
        ).fetchone()[0]
    assert promoted == 1
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state["status"] == "finalized"
    assert state["target_db_backup"]  # a pre-promote backup was taken


def test_import_status_includes_readiness_payload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "run-1"
    csv_root = run_dir / "csv"
    target_db = tmp_path / "ledger.db"
    staging_db = run_dir / "staging.db"
    csv_root.mkdir(parents=True)
    init_database(target_db)
    init_database(staging_db)
    write_run_state(
        run_dir,
        bound_import_state(
            {
                "status": "ready_to_finalize",
                "target_db_path": str(target_db),
                "staging_db_path": str(staging_db),
                "csv_root": str(csv_root),
                "pending_review_count": 0,
                "target_db_fingerprint_before": database_fingerprint(target_db),
            },
            _make_finalize_paths(tmp_path, run_dir.parent),
        ),
    )
    paths = Paths(
        pdf_dir=tmp_path / "inbox",
        processed_dir=tmp_path / "processed",
        db_path=target_db,
        base_db_path=None,
        backup_dir=tmp_path / "backups",
        ledger_config=tmp_path / "ledger.toml",
        merchant_rules=tmp_path / "merchants.toml",
        categories=tmp_path / "categories.toml",
        runs_dir=runs_dir,
    )

    workflow.status(SimpleNamespace(run_dir=run_dir), paths)

    payload = json.loads(capsys.readouterr().out)
    assert payload["readiness"]["can_finalize"] is True
    assert payload["readiness"]["gates"]["target_db_unchanged"] == "pass"


def _make_finalize_paths(tmp_path: Path, runs_dir: Path) -> Paths:
    """Return a Paths fixture pointing at tmp_path assets."""
    return Paths(
        pdf_dir=tmp_path / "inbox",
        processed_dir=tmp_path / "processed",
        db_path=tmp_path / "ledger.db",
        base_db_path=None,
        backup_dir=tmp_path / "backups",
        ledger_config=tmp_path / "ledger.toml",
        merchant_rules=tmp_path / "merchants.toml",
        categories=tmp_path / "categories.toml",
        runs_dir=runs_dir,
    )


def _setup_staged_run(
    tmp_path: Path,
    runs_dir: Path,
    run_dir: Path,
) -> tuple[Path, Path, Path]:
    """Create a minimal staged run directory ready for finalize().

    Returns (target_db, staging_db, csv_root).
    """
    from ledger.imports.pdf.readiness import import_owned_fingerprint

    csv_root = run_dir / "csv"
    target_db = tmp_path / "ledger.db"
    staging_db = run_dir / "staging.db"
    csv_root.mkdir(parents=True)
    init_database(target_db)
    init_database(staging_db)
    before = database_fingerprint(target_db)
    import_owned_before = import_owned_fingerprint(target_db)
    (run_dir / "merchant-review.json").write_text("[]", encoding="utf-8")
    write_run_state(
        run_dir,
        bound_import_state(
            {
                "status": "ready_to_finalize",
                "target_db_path": str(target_db),
                "staging_db_path": str(staging_db),
                "csv_root": str(csv_root),
                "staging_inbox": str(run_dir / "inbox"),
                "merchant_review_json": str(run_dir / "merchant-review.json"),
                "pending_review_count": 0,
                "target_db_fingerprint_before": before,
                "target_db_import_owned_before": import_owned_before,
                "pdfs": [],
            },
            _make_finalize_paths(tmp_path, run_dir.parent),
        ),
    )
    return target_db, staging_db, csv_root


def _apply_finalize_mocks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch all heavy finalize steps with no-ops (keeping promote_database real)."""
    monkeypatch.setattr(workflow, "load_config", lambda _paths, _db: object())
    monkeypatch.setattr(workflow, "run_migrations", lambda _db: None)
    monkeypatch.setattr(workflow, "process", lambda **_kwargs: ProcessingResult(total_scanned=1))
    monkeypatch.setattr(workflow, "validate_processing_result", lambda *_args: None)
    monkeypatch.setattr(workflow, "write_result_to_db", lambda *_args: None)
    monkeypatch.setattr(workflow, "build_merchant_review", lambda _result: [])
    monkeypatch.setattr(workflow, "seed_review_candidates", lambda *_args: None)
    monkeypatch.setattr(workflow, "get_review_rows", lambda _db: [])
    monkeypatch.setattr(workflow, "apply_review_decisions", lambda _db: 0)
    monkeypatch.setattr(workflow, "pending_review_count", lambda _db: 0)
    monkeypatch.setattr(workflow, "run_post_processing", lambda _db, _config: {})
    monkeypatch.setattr(workflow, "record_processed_files", lambda *_args: None)
    monkeypatch.setattr(workflow, "record_db_operation", lambda *_args, **_kwargs: None)


def test_finalize_crash_before_candidate_provenance_retries_under_current_protocol(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A current protocol phase distinguishes a safely retryable pre-publish crash.

    Legacy markerless state remains fail-closed, but a run that durably entered the
    current protocol before fallible preparation can prove publication never started.
    """
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "run-1"
    _target_db, _staging_db, _csv_root = _setup_staged_run(tmp_path, runs_dir, run_dir)
    paths = _make_finalize_paths(tmp_path, runs_dir)

    _apply_finalize_mocks(monkeypatch)

    # Inject a fault into run_post_processing on the first call only.
    call_count = {"n": 0}

    def flaky_run_post_processing(_db, _config):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("Simulated disk-full during post-processing")
        return {}

    monkeypatch.setattr(workflow, "run_post_processing", flaky_run_post_processing)

    # ── First finalize: should raise and persist failed/finalize_failed ──────
    with pytest.raises(RuntimeError, match="Simulated disk-full"):
        workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    state_after_crash = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state_after_crash["status"] == "failed", (
        "Expected status='failed' after crash, got: "
        + repr(state_after_crash.get("status"))
        + " — without the try/except fix the run is wedged at 'finalizing'"
    )
    assert state_after_crash["failure_reason"] == "finalize_failed", (
        "Expected failure_reason='finalize_failed' after crash, got: "
        + repr(state_after_crash.get("failure_reason"))
    )

    assert state_after_crash["finalize_protocol_version"] == PUBLICATION_PROTOCOL_VERSION
    assert state_after_crash["publication_phase"] == "preparing"
    assert "publication_candidate_import_owned" not in state_after_crash

    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    state_after_retry = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert call_count["n"] == 2
    assert state_after_retry["status"] == "finalized"
    assert state_after_retry["publication_phase"] == "published"
    assert state_after_retry["publication_completed"] is True


def test_finalize_needs_review_systemexit_not_marked_finalize_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """H3 regression: the needs_review SystemExit must NOT be converted to finalize_failed.

    When pending_review_count > 0 inside finalize(), the function sets status='needs_review'
    then raises SystemExit. The except SystemExit: raise branch must let this propagate
    without overwriting state with failed/finalize_failed.
    """
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "run-1"
    _target_db, _staging_db, _csv_root = _setup_staged_run(tmp_path, runs_dir, run_dir)
    paths = _make_finalize_paths(tmp_path, runs_dir)

    _apply_finalize_mocks(monkeypatch)
    # Override pending_review_count to simulate unresolved reviews.
    monkeypatch.setattr(workflow, "pending_review_count", lambda _db: 2)

    with pytest.raises(SystemExit, match="Manual review is not complete"):
        workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state["status"] == "needs_review", (
        "needs_review SystemExit must leave status='needs_review', not overwrite with 'failed'; "
        "got: " + repr(state.get("status"))
    )
    assert state.get("failure_reason") != "finalize_failed", (
        "failure_reason must NOT be 'finalize_failed' after needs_review path"
    )


def test_promote_database_invalidates_schema_memo_for_replaced_db(tmp_path: Path):
    """Publishing complete DB content in-process must drop the ensure_schema memo.

    ensure_schema() skips init+migrate for a path it has already initialized this
    process. promote_database() rewrites the database at that path, so it must invalidate
    the memo: otherwise a later ensure_schema() trusts the stale memo and skips the
    rebuild, leaving callers to hit "no such table" in the published database.
    """
    target_db = tmp_path / "ledger.db"
    ensure_schema(target_db)  # initialize + memoize "schema ready" for this path

    # Stand in for a future path that promotes a schema-incomplete replacement.
    staging_db = tmp_path / "staging.db"
    with closing(sqlite3.connect(staging_db)) as conn, conn:
        conn.execute("CREATE TABLE placeholder (id INTEGER)")

    promote_database(staging_db, target_db, tmp_path / "backups")

    # The memo must be gone so ensure_schema rebuilds the schema on the new file.
    ensure_schema(target_db)
    tables = set(inspect(get_engine(target_db)).get_table_names())
    assert "transactions" in tables


# ── H4: inbox PDFs must move only AFTER the atomic promote succeeds ───────────


def _setup_staged_run_with_pdf(
    tmp_path: Path,
    runs_dir: Path,
    run_dir: Path,
) -> tuple[Path, Path, Path, Path, Path]:
    """Create a staged run whose pdfs list references a real inbox PDF.

    Returns (target_db, staging_db, csv_root, inbox_pdf, processed_target).

    inbox_pdf physically exists in an inbox dir; processed_target is the intended
    archive destination (where finalize WOULD move it after a successful promote).
    """
    from ledger.imports.pdf.readiness import import_owned_fingerprint

    csv_root = run_dir / "csv"
    target_db = tmp_path / "ledger.db"
    staging_db = run_dir / "staging.db"
    csv_root.mkdir(parents=True)
    init_database(target_db)
    init_database(staging_db)
    before = database_fingerprint(target_db)
    import_owned_before = import_owned_fingerprint(target_db)
    (run_dir / "merchant-review.json").write_text("[]", encoding="utf-8")

    inbox_dir = tmp_path / "inbox"
    processed_dir = tmp_path / "processed"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)
    inbox_pdf = inbox_dir / "2044-03-07-harbor-deposit-chequing-1001.pdf"
    inbox_pdf.write_bytes(b"%PDF-1.4 fake inbox statement")
    processed_target = processed_dir / "2044-03-07-harbor-deposit-chequing-1001.pdf"
    file_hash = workflow.sha256_file(inbox_pdf)

    write_run_state(
        run_dir,
        bound_import_state(
            {
                "status": "ready_to_finalize",
                "target_db_path": str(target_db),
                "staging_db_path": str(staging_db),
                "csv_root": str(csv_root),
                "staging_inbox": str(run_dir / "inbox"),
                "merchant_review_json": str(run_dir / "merchant-review.json"),
                "pending_review_count": 0,
                "target_db_fingerprint_before": before,
                "target_db_import_owned_before": import_owned_before,
                "pdfs": [
                    {
                        "original_pdf": str(inbox_pdf),
                        "file_hash": file_hash,
                        "processed_pdf": str(processed_target),
                        "transaction_count": 2,
                        "skipped_reason": "",
                    }
                ],
            },
            _make_finalize_paths(tmp_path, run_dir.parent),
        ),
    )
    return target_db, staging_db, csv_root, inbox_pdf, processed_target


def _apply_finalize_mocks_keep_record(monkeypatch: pytest.MonkeyPatch) -> None:
    """Like _apply_finalize_mocks but keeps record_processed_files real.

    record_processed_files reads item['file_hash'] + item['processed_pdf'] from the
    run state (no filesystem access), so we can assert the processed-PDF hash was
    actually recorded into the staging DB and promoted with the data.
    """
    monkeypatch.setattr(workflow, "load_config", lambda _paths, _db: object())
    monkeypatch.setattr(workflow, "run_migrations", lambda _db: None)
    monkeypatch.setattr(workflow, "process", lambda **_kwargs: ProcessingResult(total_scanned=1))
    monkeypatch.setattr(workflow, "validate_processing_result", lambda *_args: None)
    monkeypatch.setattr(workflow, "write_result_to_db", lambda *_args: None)
    monkeypatch.setattr(workflow, "build_merchant_review", lambda _result: [])
    monkeypatch.setattr(workflow, "seed_review_candidates", lambda *_args: None)
    monkeypatch.setattr(workflow, "get_review_rows", lambda _db: [])
    monkeypatch.setattr(workflow, "apply_review_decisions", lambda _db: 0)
    monkeypatch.setattr(workflow, "pending_review_count", lambda _db: 0)
    monkeypatch.setattr(workflow, "run_post_processing", lambda _db, _config: {})
    monkeypatch.setattr(workflow, "record_db_operation", lambda *_args, **_kwargs: None)


def _recorded_processed_hashes(db_path: Path) -> set[str]:
    with closing(sqlite3.connect(db_path)) as conn:
        return {row[0] for row in conn.execute("select file_hash from processed_files").fetchall()}


def test_finalize_late_failure_does_not_orphan_inbox_pdf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """H4 regression: a late finalize failure must NOT move the inbox PDF.

    The irreversible inbox->processed move must happen only AFTER the atomic
    promote succeeds. If the LAST safety gate (_fail_if_target_db_changed right
    before promote) trips, the original inbox PDF must still exist on disk so the
    data is not silently lost (next stage can re-glob it).

    Drift is injected LATE — after the early post-processing gate has already
    passed — so that only the final pre-promote gate trips. Pre-fix this test
    FAILS: the move loop ran before that final gate, so the PDF is already gone.
    """
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "run-1"
    target_db, _staging_db, _csv_root, inbox_pdf, processed_target = _setup_staged_run_with_pdf(
        tmp_path, runs_dir, run_dir
    )
    paths = _make_finalize_paths(tmp_path, runs_dir)
    _apply_finalize_mocks_keep_record(monkeypatch)

    # Inject target-DB drift LATE: summarize_final_merchants runs after the early
    # post-processing gate but before the final pre-promote gate, so this trips
    # ONLY the last gate (the one guarding the atomic promote). Pre-fix the move
    # loop precedes that gate, so the PDF would already be gone when it fires.
    def drift_then_summarize(stage_candidates, result):
        insert_minimal_transaction(target_db, "txn-live-drift")
        return {"resolved_merchants": [], "manual_review_merchants": []}

    monkeypatch.setattr(workflow, "summarize_final_merchants", drift_then_summarize)

    with pytest.raises(SystemExit, match="Target database changed since stage"):
        workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    # The inbox PDF must NOT have been moved out.
    assert inbox_pdf.exists(), "inbox PDF was moved/orphaned before the promote gate"
    assert not processed_target.exists(), "PDF was archived despite the promote never running"


def test_finalize_promote_failure_does_not_orphan_inbox_pdf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """H4 regression: if promote_database itself raises, the inbox PDF must survive."""
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "run-1"
    _target_db, _staging_db, _csv_root, inbox_pdf, processed_target = _setup_staged_run_with_pdf(
        tmp_path, runs_dir, run_dir
    )
    paths = _make_finalize_paths(tmp_path, runs_dir)
    _apply_finalize_mocks_keep_record(monkeypatch)

    def boom_promote(*_args, **_kwargs):
        raise RuntimeError("Simulated promote failure")

    monkeypatch.setattr(workflow, "_promote_database_locked", boom_promote)

    with pytest.raises(RuntimeError, match="Simulated promote failure"):
        workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    assert inbox_pdf.exists(), "inbox PDF was moved before promote succeeded"
    assert not processed_target.exists()
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    # A genuine pre-finalized crash still marks the run resumable (H3 behavior).
    assert state["status"] == "failed"
    assert state["failure_reason"] == "finalize_failed"


def test_finalize_happy_path_archives_pdf_after_promote(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """H4: a normal finalize moves the PDF to processed AFTER promote and records the hash."""
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "run-1"
    target_db, _staging_db, _csv_root, inbox_pdf, processed_target = _setup_staged_run_with_pdf(
        tmp_path, runs_dir, run_dir
    )
    expected_hash = workflow.sha256_file(inbox_pdf)
    paths = _make_finalize_paths(tmp_path, runs_dir)
    _apply_finalize_mocks_keep_record(monkeypatch)

    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state["status"] == "finalized"
    # PDF physically archived after the promote.
    assert not inbox_pdf.exists(), "inbox PDF should have been moved out after promote"
    assert processed_target.exists(), "PDF should have been archived to processed/"
    # The persisted manifest points at the actual archived location.
    assert state["pdfs"][0]["processed_pdf"] == str(processed_target.resolve())
    # The processed-PDF hash was recorded into the (now-promoted) target DB.
    assert expected_hash in _recorded_processed_hashes(target_db), (
        "processed-PDF hash must be recorded+promoted to prevent double-import"
    )


def test_finalize_best_effort_move_failure_does_not_wedge_promoted_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """H4 / Invariant 3: a post-promote move failure must NOT fail the finalized run.

    Once promote succeeds the run is complete: data is in the target DB and the
    processed-PDF hash is recorded+promoted. If the subsequent physical move fails,
    the PDF simply stays in inbox (the next stage skips it by hash) and the run must
    still be marked 'finalized' (NOT 'finalize_failed'), with a warning on the item.
    """
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "run-1"
    target_db, _staging_db, _csv_root, inbox_pdf, processed_target = _setup_staged_run_with_pdf(
        tmp_path, runs_dir, run_dir
    )
    expected_hash = workflow.sha256_file(inbox_pdf)
    paths = _make_finalize_paths(tmp_path, runs_dir)
    _apply_finalize_mocks_keep_record(monkeypatch)

    # Force the post-promote physical archive move to fail.
    def boom_move(*_args, **_kwargs):
        raise OSError("Simulated archive move failure (e.g. processed/ read-only)")

    monkeypatch.setattr(workflow.statement_pdf_to_csv, "move_pdf", boom_move)

    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    # The operator-visible warning must appear on stderr for the failed move.
    err = capsys.readouterr().err
    assert "could not archive" in err, (
        "expected 'could not archive' warning on stderr for archive-move failure; got: " + repr(err)
    )

    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    # Run is finalized, NOT finalize_failed — the move failure is best-effort.
    assert state["status"] == "finalized", (
        "a post-promote move failure must not wedge an already-promoted run; got: "
        + repr(state.get("status"))
    )
    assert state.get("failure_reason") != "finalize_failed"
    # Data was promoted and the processed-PDF hash recorded (double-import safety).
    assert expected_hash in _recorded_processed_hashes(target_db)
    # The inbox PDF remains (so it can be archived manually or skipped by hash later).
    assert inbox_pdf.exists(), "inbox PDF should remain after a failed best-effort move"
    assert not processed_target.exists()
    # A warning is recorded on the item so the manifest reflects what happened.
    assert state["pdfs"][0].get("finalize_warning") == "archive_move_failed"


def test_finalize_partial_move_failure_two_pdfs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
):
    """H4: partial multi-PDF move failure — first succeeds, second raises.

    Asserts:
    - run status == "finalized" (the promoted run is never wedged)
    - first PDF's processed_pdf reflects the actual moved location and inbox PDF is gone
    - second item has finalize_warning == "archive_move_failed" and inbox PDF remains
    - both hashes are recorded in the promoted DB (double-import prevention still holds)
    - a WARNING is written to stderr for the failed move
    """
    from ledger.imports.pdf.readiness import import_owned_fingerprint as _io_fp

    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "run-1"
    csv_root = run_dir / "csv"
    target_db = tmp_path / "ledger.db"
    staging_db = run_dir / "staging.db"
    csv_root.mkdir(parents=True)
    init_database(target_db)
    init_database(staging_db)
    before = database_fingerprint(target_db)
    import_owned_before = _io_fp(target_db)
    (run_dir / "merchant-review.json").write_text("[]", encoding="utf-8")

    inbox_dir = tmp_path / "inbox"
    processed_dir = tmp_path / "processed"
    inbox_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)

    inbox_pdf_1 = inbox_dir / "2026-04-harbor-chequing-0001.pdf"
    inbox_pdf_2 = inbox_dir / "2026-05-harbor-chequing-0002.pdf"
    inbox_pdf_1.write_bytes(b"%PDF-1.4 fake statement one")
    inbox_pdf_2.write_bytes(b"%PDF-1.4 fake statement two")
    processed_target_1 = processed_dir / "2026-04-harbor-chequing-0001.pdf"
    processed_target_2 = processed_dir / "2026-05-harbor-chequing-0002.pdf"

    hash_1 = workflow.sha256_file(inbox_pdf_1)
    hash_2 = workflow.sha256_file(inbox_pdf_2)

    write_run_state(
        run_dir,
        bound_import_state(
            {
                "status": "ready_to_finalize",
                "target_db_path": str(target_db),
                "staging_db_path": str(staging_db),
                "csv_root": str(csv_root),
                "staging_inbox": str(run_dir / "inbox"),
                "merchant_review_json": str(run_dir / "merchant-review.json"),
                "pending_review_count": 0,
                "target_db_fingerprint_before": before,
                "target_db_import_owned_before": import_owned_before,
                "pdfs": [
                    {
                        "original_pdf": str(inbox_pdf_1),
                        "file_hash": hash_1,
                        "processed_pdf": str(processed_target_1),
                        "transaction_count": 2,
                        "skipped_reason": "",
                    },
                    {
                        "original_pdf": str(inbox_pdf_2),
                        "file_hash": hash_2,
                        "processed_pdf": str(processed_target_2),
                        "transaction_count": 3,
                        "skipped_reason": "",
                    },
                ],
            },
            _make_finalize_paths(tmp_path, run_dir.parent),
        ),
    )

    paths = _make_finalize_paths(tmp_path, runs_dir)
    _apply_finalize_mocks_keep_record(monkeypatch)

    # move_pdf succeeds on the first call, raises on the second.
    call_count = {"n": 0}
    real_move_pdf = workflow.statement_pdf_to_csv.move_pdf

    def partial_move(src, dst, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return real_move_pdf(src, dst, **kwargs)
        raise OSError("Simulated archive move failure on second PDF")

    monkeypatch.setattr(workflow.statement_pdf_to_csv, "move_pdf", partial_move)

    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    # The operator-visible warning must appear on stderr for the failed move.
    err = capsys.readouterr().err
    assert "could not archive" in err, (
        "expected 'could not archive' warning on stderr for archive-move failure; got: " + repr(err)
    )

    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

    # Run must be finalized (partial move failure cannot wedge a promoted run).
    assert state["status"] == "finalized", (
        "partial move failure must not wedge finalized run; got: " + repr(state.get("status"))
    )

    pdfs = state["pdfs"]
    assert len(pdfs) == 2

    # First PDF: successfully archived — inbox PDF gone, processed_pdf updated.
    assert not inbox_pdf_1.exists(), "first inbox PDF should have been moved out"
    assert processed_target_1.exists(), "first PDF should be at its processed target"
    assert pdfs[0]["processed_pdf"] == str(processed_target_1.resolve())
    assert pdfs[0].get("finalize_warning") is None

    # Second PDF: move failed — inbox PDF remains, warning recorded.
    assert inbox_pdf_2.exists(), "second inbox PDF should remain after failed move"
    assert not processed_target_2.exists()
    assert pdfs[1].get("finalize_warning") == "archive_move_failed"

    # Both hashes must be in the promoted DB (double-import prevention).
    recorded = _recorded_processed_hashes(target_db)
    assert hash_1 in recorded, "hash of first PDF must be in promoted DB"
    assert hash_2 in recorded, "hash of second PDF must be in promoted DB"


# ── M16: failed stage must persist a run.json so status/cancel don't crash ────


def _make_stage_paths(tmp_path: Path, runs_dir: Path) -> Paths:
    """Return a Paths fixture for stage() tests."""
    pdf_dir = tmp_path / "inbox"
    processed_dir = tmp_path / "processed"
    target_db = tmp_path / "ledger.db"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)
    (pdf_dir / "statement.pdf").write_bytes(b"%PDF fake")
    init_database(target_db)
    return Paths(
        pdf_dir=pdf_dir,
        processed_dir=processed_dir,
        db_path=target_db,
        base_db_path=None,
        backup_dir=tmp_path / "backups",
        ledger_config=tmp_path / "ledger.toml",
        merchant_rules=tmp_path / "merchants.toml",
        categories=tmp_path / "categories.toml",
        runs_dir=runs_dir,
    )


def _apply_stage_mocks_up_to_process(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Patch the PDF-extraction steps so stage() reaches process()/validate_processing_result().

    The fake process_pdf writes an empty CSV; load_config and write_manifest are no-ops
    so the test doesn't need real TOML files.
    """
    processed_dir = tmp_path / "processed"

    def fake_process_pdf(pdf: Path, csv_root: Path, _processed_dir: Path, **_kwargs):
        csv_root.mkdir(parents=True, exist_ok=True)
        csv_path = csv_root / "statement.csv"
        csv_path.write_text("Date,Description,Amount\n", encoding="utf-8")
        return SimpleNamespace(
            pdf=pdf,
            csv_path=csv_path,
            processed_pdf=processed_dir / "2026-05-01-test.pdf",
            statement_type="test",
            transaction_count=0,
            skipped_reason="",
            validation_errors=[],
            validation_warnings=[],
            account_slug="statement",
        )

    monkeypatch.setattr(workflow.statement_pdf_to_csv, "process_pdf", fake_process_pdf)
    monkeypatch.setattr(
        workflow.statement_pdf_to_csv,
        "write_manifest",
        lambda path, _results, dry_run: path.write_text("[]", encoding="utf-8"),
    )
    monkeypatch.setattr(workflow, "load_config", lambda _paths, _runtime_db: object())


def test_stage_parse_exceptions_writes_failed_run_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """M16: stage() with parse exceptions must persist run.json with status='failed'.

    Pre-fix this test FAILS (run.json is absent when validate_processing_result raises).
    Post-fix: run.json exists with status='failed' and failure_reason='processing_validation_failed'.
    """
    runs_dir = tmp_path / "runs"
    paths = _make_stage_paths(tmp_path, runs_dir)
    _apply_stage_mocks_up_to_process(monkeypatch, tmp_path)

    from ledger.core.models import ExceptionRecord
    from ledger.processing import ProcessingResult

    fake_exception = ExceptionRecord(
        source_file="statement.csv",
        source_path="2026-04/statement.csv",
        source_row=0,
        reason="cannot parse amount",
        raw_data={"Date": "bad", "Description": "x", "Amount": "oops"},
    )

    monkeypatch.setattr(
        workflow,
        "process",
        lambda **_kwargs: ProcessingResult(
            total_scanned=1,
            exceptions=[fake_exception],
        ),
    )

    with pytest.raises(SystemExit, match="parse exceptions"):
        workflow.stage(SimpleNamespace(), paths)

    # Exactly one run dir must have been created.
    run_dirs = [p for p in runs_dir.iterdir() if p.is_dir()]
    assert len(run_dirs) == 1, f"expected 1 run dir, got {run_dirs}"
    run_dir = run_dirs[0]

    run_json = run_dir / "run.json"
    assert run_json.exists(), (
        "run.json must be written before validate_processing_result raises; "
        "pre-fix this file is absent (M16 bug)"
    )

    state = json.loads(run_json.read_text(encoding="utf-8"))
    assert state["status"] == "failed", f"expected status='failed', got {state.get('status')!r}"
    assert state["failure_reason"] == "processing_validation_failed", (
        f"expected failure_reason='processing_validation_failed', got {state.get('failure_reason')!r}"
    )


def test_stage_zero_rows_writes_failed_run_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """M16: stage() with zero scanned rows must also persist run.json with status='failed'."""
    runs_dir = tmp_path / "runs"
    paths = _make_stage_paths(tmp_path, runs_dir)
    _apply_stage_mocks_up_to_process(monkeypatch, tmp_path)

    from ledger.processing import ProcessingResult

    monkeypatch.setattr(
        workflow,
        "process",
        lambda **_kwargs: ProcessingResult(total_scanned=0),
    )

    with pytest.raises(SystemExit, match="zero scanned rows"):
        workflow.stage(SimpleNamespace(), paths)

    run_dirs = [p for p in runs_dir.iterdir() if p.is_dir()]
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]

    run_json = run_dir / "run.json"
    assert run_json.exists(), "run.json must be written even when zero rows are scanned"

    state = json.loads(run_json.read_text(encoding="utf-8"))
    assert state["status"] == "failed"
    assert state["failure_reason"] == "processing_validation_failed"


def test_stage_failed_run_status_and_cancel_dont_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """M16: status() and cancel() against a failed-stage run must not raise.

    This drives stage() to produce a parse-exception failure, then calls status()
    and cancel() against the resulting run dir to confirm neither raises
    FileNotFoundError or 'Run state not found'.
    """
    runs_dir = tmp_path / "runs"
    paths = _make_stage_paths(tmp_path, runs_dir)
    _apply_stage_mocks_up_to_process(monkeypatch, tmp_path)

    from ledger.core.models import ExceptionRecord
    from ledger.processing import ProcessingResult

    fake_exception = ExceptionRecord(
        source_file="statement.csv",
        source_path="2026-04/statement.csv",
        source_row=0,
        reason="cannot parse amount",
        raw_data={"Date": "bad", "Description": "x", "Amount": "oops"},
    )

    monkeypatch.setattr(
        workflow,
        "process",
        lambda **_kwargs: ProcessingResult(
            total_scanned=1,
            exceptions=[fake_exception],
        ),
    )

    with pytest.raises(SystemExit):
        workflow.stage(SimpleNamespace(), paths)

    run_dirs = [p for p in runs_dir.iterdir() if p.is_dir()]
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]

    # status() must not raise — it should return a JSON payload describing the failure.
    workflow.status(SimpleNamespace(run_dir=run_dir), paths)
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["status"] == "failed", (
        f"status payload has wrong status: {payload.get('status')!r}"
    )

    # cancel() must not raise.
    workflow.cancel(SimpleNamespace(run_dir=run_dir, reason="cleanup_after_failed_stage"), paths)

    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state["status"] == "cancelled", (
        f"cancel() should set status='cancelled', got {state.get('status')!r}"
    )


# --- audit trail for apply_review_decisions (M7) ---


def test_apply_review_decisions_records_audit_row(tmp_path: Path):
    """apply_review_decisions must write a db_operations audit row after applying."""
    from ledger.storage.operations import recent_db_operations

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    insert_minimal_transaction(db_path, "txn-audit-1")
    insert_minimal_transaction(db_path, "txn-audit-2")
    seed_runtime_categories(db_path, "Dining")

    seed_review_candidates(
        db_path,
        [
            {
                "key": "merchant:AUDIT CAFE",
                "merchant": "AUDIT CAFE",
                "current_category": "Other",
                "txn_ids": ["txn-audit-1", "txn-audit-2"],
                "examples": [{"description": "AUDIT CAFE"}],
                "count": 2,
                "total_abs_amount": Decimal("55.00"),
            }
        ],
    )
    rows = get_review_rows(db_path)
    rows[0]["status"] = "resolved"
    rows[0]["category"] = "Dining"
    save_review_rows(db_path, rows)

    updated = apply_review_decisions(db_path)
    assert updated == 2

    ops = recent_db_operations(db_path)
    review_ops = [o for o in ops if o["Operation"] == "review_apply"]
    assert len(review_ops) == 1
    assert review_ops[0]["Status"] == "success"


def test_apply_review_decisions_no_rows_still_records_audit(tmp_path: Path):
    """apply_review_decisions records an audit row even when zero transactions are updated."""
    from ledger.storage.operations import recent_db_operations

    db_path = tmp_path / "ledger.db"
    init_database(db_path)

    updated = apply_review_decisions(db_path)
    assert updated == 0

    ops = recent_db_operations(db_path)
    review_ops = [o for o in ops if o["Operation"] == "review_apply"]
    assert len(review_ops) == 1
    assert review_ops[0]["Status"] == "success"


def test_apply_review_decisions_audit_failure_does_not_propagate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If record_db_operation raises, apply_review_decisions must still return the
    correct count and must NOT propagate the exception — the mutation already committed."""
    import ledger.import_review as review_mod

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    insert_minimal_transaction(db_path, "txn-be-1")
    seed_runtime_categories(db_path, "Dining")

    seed_review_candidates(
        db_path,
        [
            {
                "key": "merchant:BE CAFE",
                "merchant": "BE CAFE",
                "current_category": "Other",
                "txn_ids": ["txn-be-1"],
                "examples": [{"description": "BE CAFE"}],
                "count": 1,
                "total_abs_amount": Decimal("10.00"),
            }
        ],
    )
    rows = get_review_rows(db_path)
    rows[0]["status"] = "resolved"
    rows[0]["category"] = "Dining"
    save_review_rows(db_path, rows)

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("simulated DB locked")

    monkeypatch.setattr(review_mod, "record_db_operation", _raise)

    # Must not raise, must return the normal mutation count
    updated = apply_review_decisions(db_path)
    assert updated == 1


# ── Fix 2: pdftotext subprocess timeout (H-timeout) ─────────────────────────


def test_run_pdftotext_raises_runtime_error_on_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """run_pdftotext must raise RuntimeError('...timed out') when the subprocess
    times out, instead of hanging indefinitely.

    Fix 2 adds timeout=120 to subprocess.run and catches TimeoutExpired,
    re-raising as RuntimeError so the per-PDF audit path gets a clean error.
    """
    from ledger.imports.pdf import statement_pdf_to_csv

    fake_pdf = tmp_path / "fake.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 fake")

    def fake_subprocess_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["pdftotext"], timeout=120)

    monkeypatch.setattr(statement_pdf_to_csv.subprocess, "run", fake_subprocess_run)
    # Also mock shutil.which so the "pdftotext not found" guard passes.
    monkeypatch.setattr(statement_pdf_to_csv.shutil, "which", lambda _cmd: "/usr/bin/pdftotext")

    with pytest.raises(RuntimeError, match="timed out"):
        statement_pdf_to_csv.run_pdftotext(fake_pdf)


def test_run_pdftotext_bytes_uses_stdin_without_source_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pinned PDF bytes are passed through stdin with no reopenable pathname."""
    from ledger.imports.pdf import statement_pdf_to_csv

    observed: dict[str, object] = {}

    def fake_subprocess_run(command, **kwargs):
        observed["command"] = command
        observed.update(kwargs)
        return subprocess.CompletedProcess(command, 0, stdout=b"pinned text\n", stderr=b"")

    monkeypatch.setattr(statement_pdf_to_csv.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(statement_pdf_to_csv.shutil, "which", lambda _cmd: "/usr/bin/pdftotext")

    assert statement_pdf_to_csv.run_pdftotext_bytes(b"%PDF-pinned%") == "pinned text\n"
    assert observed["command"] == ["pdftotext", "-layout", "-", "-"]
    assert observed["input"] == b"%PDF-pinned%"
    assert "text" not in observed


# ── Promote abort: re-check fingerprint immediately before replace ─────────────


def test_promote_database_aborts_when_target_changed_concurrently(tmp_path: Path):
    """promote_database must raise RuntimeError if the target DB changed after the
    expected_fingerprint was captured, and the live DB must remain INTACT (not replaced).

    Pre-fix: promote_database has no recheck — it silently overwrites the live DB,
    losing the concurrent write. This test fails pre-fix (promote succeeds, new data gone).
    Post-fix: RuntimeError is raised BEFORE replace, live DB still has the concurrent write.
    """
    from ledger.imports.pdf.files import promote_database
    from ledger.imports.pdf.readiness import database_fingerprint

    target = tmp_path / "ledger.db"
    init_database(target)
    # Capture fingerprint of clean target.
    expected_fp = database_fingerprint(target)

    # Simulate a concurrent write AFTER staging (fingerprint capture) but BEFORE promote.
    insert_minimal_transaction(target, "txn-concurrent-edit")
    concurrent_fp = database_fingerprint(target)
    assert concurrent_fp["sha256"] != expected_fp["sha256"]  # sanity check

    staging = tmp_path / "staging.db"
    init_database(staging)

    with pytest.raises(RuntimeError, match="target DB changed during finalize"):
        promote_database(staging, target, tmp_path / "backups", expected_fingerprint=expected_fp)

    # Live DB must still contain the concurrent write — NOT replaced by staging clone.
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(target)) as conn:
        count = conn.execute(
            "select count(*) from transactions where txn_id = 'txn-concurrent-edit'"
        ).fetchone()[0]
    assert count == 1, "live DB must be intact; concurrent write must not be lost"

    # Temp file must not have been left behind (unique-name scheme — glob for any variant).
    assert not list(target.parent.glob(f".{target.name}.promote-*.tmp"))


def test_promote_database_happy_path_unchanged_target(tmp_path: Path):
    """promote_database must publish staging when the target is unchanged."""
    from ledger.imports.pdf.files import promote_database
    from ledger.imports.pdf.readiness import database_fingerprint

    target = tmp_path / "ledger.db"
    init_database(target)
    expected_fp = database_fingerprint(target)

    staging = tmp_path / "staging.db"
    init_database(staging)
    insert_minimal_transaction(staging, "txn-staged")

    backup = promote_database(
        staging, target, tmp_path / "backups", expected_fingerprint=expected_fp
    )

    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(target)) as conn:
        count = conn.execute(
            "select count(*) from transactions where txn_id = 'txn-staged'"
        ).fetchone()[0]
    assert count == 1, "staging data must be promoted onto target"
    assert backup is not None


def test_promote_database_serializes_competing_target_publications(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two runs may not both publish after validating the same live baseline."""
    from ledger.imports.pdf.readiness import import_owned_fingerprint

    target = tmp_path / "ledger.db"
    init_database(target)
    expected = import_owned_fingerprint(target)
    staging_a = tmp_path / "staging-a.db"
    staging_b = tmp_path / "staging-b.db"
    init_database(staging_a)
    init_database(staging_b)
    insert_minimal_transaction(staging_a, "txn-run-a")
    insert_minimal_transaction(staging_b, "txn-run-b")

    replace_barrier = threading.Barrier(2)
    real_replace = Path.replace

    def synchronized_replace(source: Path, destination: Path) -> Path:
        if ".promote-" in source.name and Path(destination) == target:
            replace_barrier.wait(timeout=5)
        return real_replace(source, destination)

    monkeypatch.setattr(Path, "replace", synchronized_replace)

    def publish(staging: Path) -> Path | None:
        return promote_database(
            staging,
            target,
            tmp_path / "backups",
            expected_import_owned=expected,
        )

    outcomes: list[Path | BaseException | None] = []
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(publish, staging) for staging in (staging_a, staging_b)]
        for future in futures:
            try:
                outcomes.append(future.result(timeout=10))
            except BaseException as exc:  # capture both competing outcomes for assertion
                outcomes.append(exc)

    errors = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    assert len(errors) == 1, "exactly one competing publication must fail closed"
    assert isinstance(errors[0], RuntimeError)

    with closing(sqlite3.connect(target)) as connection:
        published_ids = {
            row[0]
            for row in connection.execute(
                "select txn_id from transactions where txn_id in ('txn-run-a', 'txn-run-b')"
            )
        }
    assert len(published_ids) == 1


def test_promote_database_never_loses_budget_committed_at_publication_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runtime-table commit after merge-forward must block or follow publish."""
    from ledger.imports.pdf import files as pdf_files
    from ledger.imports.pdf.readiness import import_owned_fingerprint

    target = tmp_path / "ledger.db"
    staging = tmp_path / "staging.db"
    init_database(target)
    init_database(staging)
    insert_minimal_transaction(staging, "txn-staged-late-budget")
    with closing(sqlite3.connect(target)) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        connection.execute("PRAGMA wal_autocheckpoint=0")
    expected = import_owned_fingerprint(target)

    real_publish = pdf_files.publish_database
    publish_entered = threading.Event()
    budget_committed = threading.Event()
    writer_errors: list[BaseException] = []

    def publish_after_budget(candidate: Path, live_target: Path, **kwargs) -> Path:
        publish_entered.set()
        assert budget_committed.wait(timeout=10)
        assert writer_errors == []
        with closing(sqlite3.connect(live_target)) as connection:
            assert connection.execute(
                "SELECT count(*) FROM budgets WHERE category = 'PRIVATE-LIVE-BUDGET'"
            ).fetchone() == (1,)
        return real_publish(candidate, live_target, **kwargs)

    def commit_late_budget() -> None:
        try:
            assert publish_entered.wait(timeout=10)
            observed_at = "2026-08-01 12:00:00"
            with closing(sqlite3.connect(target)) as connection, connection:
                connection.execute(
                    "INSERT INTO budgets "
                    "(budget_month, category, amount, notes, created_at, updated_at) "
                    "VALUES (?, ?, ?, '', ?, ?)",
                    (
                        "2026-08",
                        "PRIVATE-LIVE-BUDGET",
                        "123.45",
                        observed_at,
                        observed_at,
                    ),
                )
        except BaseException as exc:
            writer_errors.append(exc)
        finally:
            budget_committed.set()

    monkeypatch.setattr(pdf_files, "publish_database", publish_after_budget)
    writer = threading.Thread(target=commit_late_budget)
    writer.start()
    failure: RuntimeError | None = None
    try:
        promote_database(
            staging,
            target,
            tmp_path / "backups",
            expected_import_owned=expected,
        )
    except RuntimeError as exc:
        failure = exc
    finally:
        publish_entered.set()
        writer.join(timeout=10)

    assert not writer.is_alive()
    assert writer_errors == []
    with closing(sqlite3.connect(target)) as connection:
        assert connection.execute(
            "SELECT count(*) FROM budgets WHERE category = 'PRIVATE-LIVE-BUDGET'"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM transactions WHERE txn_id = 'txn-staged-late-budget'"
        ).fetchone() == (0,)
    assert failure is not None
    assert "target DB changed" in str(failure)


def test_promote_database_preserves_unchanged_older_schema_runtime_row(
    tmp_path: Path,
) -> None:
    """An older runtime-table schema must migrate forward without false drift."""
    from ledger.imports.pdf.readiness import import_owned_fingerprint

    target = tmp_path / "ledger.db"
    staging = tmp_path / "staging.db"
    init_database(target)
    init_database(staging)
    insert_minimal_transaction(staging, "txn-staged-older-runtime-schema")

    observed_at = "2026-08-01 12:00:00"
    with closing(sqlite3.connect(target)) as connection, connection:
        connection.execute("DROP TABLE savings_goals")
        connection.execute(
            "CREATE TABLE savings_goals ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "name VARCHAR(160) NOT NULL, "
            "target_amount NUMERIC(12, 2) NOT NULL, "
            "target_date DATE, "
            "status VARCHAR(30) NOT NULL DEFAULT 'active', "
            "notes TEXT NOT NULL DEFAULT '', "
            "created_at DATETIME NOT NULL, "
            "updated_at DATETIME NOT NULL"
            ")"
        )
        connection.execute(
            "INSERT INTO savings_goals "
            "(name, target_amount, status, notes, created_at, updated_at) "
            "VALUES ('LEGACY-GOAL', '4321.00', 'active', '', ?, ?)",
            (observed_at, observed_at),
        )

    with closing(sqlite3.connect(target)) as connection:
        assert "account_id" not in {
            row[1] for row in connection.execute("PRAGMA table_info(savings_goals)")
        }
    expected = import_owned_fingerprint(target)

    backup = promote_database(
        staging,
        target,
        tmp_path / "backups",
        expected_import_owned=expected,
    )

    assert backup is not None
    with closing(sqlite3.connect(target)) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(savings_goals)")}
        goal = connection.execute(
            "SELECT name, account_id, target_amount FROM savings_goals"
        ).fetchone()
        staged_count = connection.execute(
            "SELECT count(*) FROM transactions WHERE txn_id = 'txn-staged-older-runtime-schema'"
        ).fetchone()
    assert "account_id" in columns
    assert goal == ("LEGACY-GOAL", None, 4321)
    assert staged_count == (1,)


def test_promote_database_no_fingerprint_arg_behaves_as_before(tmp_path: Path):
    """promote_database with no expected_fingerprint must behave exactly as before
    (no recheck, always promotes). Backward-compat for callers that don't supply it."""
    from ledger.imports.pdf.files import promote_database

    target = tmp_path / "ledger.db"
    staging = tmp_path / "staging.db"
    write_marker_database(target, "OLD-DB")
    write_marker_database(staging, "NEW-DB")

    backup = promote_database(staging, target, tmp_path / "backups")

    assert read_marker_database(target) == "NEW-DB"
    assert backup is not None and read_marker_database(backup) == "OLD-DB"


def test_promote_candidate_aborts_when_target_changed_concurrently(tmp_path: Path):
    """promote_candidate must raise RuntimeError if the target changed since the
    expected_fingerprint was captured, and the live DB must be INTACT.

    Pre-fix: no recheck — concurrent write is silently lost.
    Post-fix: RuntimeError before replace, live DB unchanged.
    """
    from ledger.imports.pdf.readiness import database_fingerprint
    from ledger.repair.statements import promote_candidate

    target = tmp_path / "ledger.db"
    init_database(target)
    expected_fp = database_fingerprint(target)

    # Concurrent write after fingerprint captured.
    insert_minimal_transaction(target, "txn-repair-concurrent")

    candidate = tmp_path / "candidate.db"
    init_database(candidate)

    with pytest.raises(RuntimeError, match="target DB changed during finalize"):
        promote_candidate(candidate, target, expected_fingerprint=expected_fp)

    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(target)) as conn:
        count = conn.execute(
            "select count(*) from transactions where txn_id = 'txn-repair-concurrent'"
        ).fetchone()[0]
    assert count == 1, "live DB must be intact after aborted repair promote"

    # Temp file must not have been left behind (unique-name scheme).
    assert not list(target.parent.glob(f".{target.name}.statement-repair-*.tmp"))


def test_promote_candidate_publishes_through_open_target_wal(tmp_path: Path) -> None:
    from ledger.repair.statements import promote_candidate

    target = tmp_path / "ledger.db"
    candidate = tmp_path / "candidate.db"
    write_marker_database(target, "base")
    write_marker_database(candidate, "candidate")

    keeper = sqlite3.connect(target)
    try:
        assert keeper.execute("pragma journal_mode=wal").fetchone()[0] == "wal"
        keeper.execute("pragma wal_autocheckpoint=0")
        keeper.execute("pragma wal_checkpoint(truncate)")
        keeper.execute("update marker set value = 'committed-in-old-wal'")
        keeper.commit()

        promote_candidate(candidate, target)

        assert read_marker_database(target) == "candidate"
        assert keeper.execute("select value from marker").fetchone()[0] == "candidate"
    finally:
        keeper.close()


# ── concurrency-fs: unique per-promote temp filename ─────────────────────────
#
# Two concurrent promotes to the same target must use different temp filenames
# so they cannot clobber each other's staging copy.  The unique name is formed
# as  .{target.name}.promote-{pid}-{uuid4hex}.tmp  (promote_database) and
# .{target.name}.statement-repair-{pid}-{uuid4hex}.tmp  (promote_candidate).
# A failed promote (fingerprint mismatch) must also clean up its unique temp.


def test_promote_database_temp_name_is_unique_across_calls(tmp_path: Path):
    """Two calls to promote_database must use distinct temp paths — not the same
    fixed name — so concurrent promotes to the same DB cannot race on the staging file.
    """
    import re

    from ledger.imports.pdf.files import promote_database

    target = tmp_path / "ledger.db"
    staging = tmp_path / "staging.db"

    seen_temps: list[str] = []

    from ledger.imports.pdf.files import snapshot_database as original_snapshot

    def spy_snapshot(src, dst):
        dst_path = str(dst)
        if ".promote-" in dst_path and dst_path.endswith(".tmp"):
            seen_temps.append(dst_path)
        return original_snapshot(src, dst)

    import unittest.mock as mock

    with mock.patch("ledger.imports.pdf.files.snapshot_database", side_effect=spy_snapshot):
        write_marker_database(target, "V0")
        write_marker_database(staging, "V1")
        promote_database(staging, target, tmp_path / "backups")

        write_marker_database(staging, "V2")
        promote_database(staging, target, tmp_path / "backups")

    assert len(seen_temps) == 2, f"expected 2 spied copies, got {seen_temps}"
    assert seen_temps[0] != seen_temps[1], (
        f"promote_database reused the same temp path: {seen_temps[0]}"
    )
    # Validate the naming scheme: .ledger.db.promote-<pid>-<hex32>.tmp
    pattern = re.compile(r"\.ledger\.db\.promote-\d+-[0-9a-f]{32}\.tmp$")
    for path in seen_temps:
        assert pattern.search(path), f"temp name does not match expected scheme: {path}"


def test_promote_candidate_temp_name_is_unique_across_calls(tmp_path: Path):
    """Two calls to promote_candidate must use distinct temp paths."""
    import re

    from ledger.repair.statements import promote_candidate

    target = tmp_path / "ledger.db"
    candidate = tmp_path / "candidate.db"

    seen_temps: list[str] = []

    from ledger.repair.statements import copy_private_file as original_copy

    def spy_copy(src, dst):
        dst_path = str(dst)
        if ".statement-repair-" in dst_path and dst_path.endswith(".tmp"):
            seen_temps.append(dst_path)
        return original_copy(src, dst)

    import unittest.mock as mock

    with mock.patch("ledger.repair.statements.copy_private_file", side_effect=spy_copy):
        write_marker_database(target, "V0")
        write_marker_database(candidate, "V1")
        promote_candidate(candidate, target)

        write_marker_database(candidate, "V2")
        promote_candidate(candidate, target)

    assert len(seen_temps) == 2, f"expected 2 spied copies, got {seen_temps}"
    assert seen_temps[0] != seen_temps[1], (
        f"promote_candidate reused the same temp path: {seen_temps[0]}"
    )
    pattern = re.compile(r"\.ledger\.db\.statement-repair-\d+-[0-9a-f]{32}\.tmp$")
    for path in seen_temps:
        assert pattern.search(path), f"temp name does not match expected scheme: {path}"


def test_promote_database_aborted_promote_cleans_up_unique_temp(tmp_path: Path):
    """When promote_database aborts (fingerprint mismatch), the unique temp file
    must be deleted — no leftover staging copy.
    """
    from ledger.imports.pdf.files import promote_database
    from ledger.imports.pdf.readiness import database_fingerprint

    target = tmp_path / "ledger.db"
    init_database(target)
    expected_fp = database_fingerprint(target)

    # Simulate concurrent write after fingerprint capture.
    insert_minimal_transaction(target, "txn-abort-cleanup-test")

    staging = tmp_path / "staging.db"
    init_database(staging)

    with pytest.raises(RuntimeError, match="target DB changed during finalize"):
        promote_database(staging, target, tmp_path / "backups", expected_fingerprint=expected_fp)

    # No leftover unique temp files.
    leftovers = list(target.parent.glob(f".{target.name}.promote-*.tmp"))
    assert not leftovers, f"Aborted promote left temp files behind: {leftovers}"


def test_promote_candidate_aborted_promote_cleans_up_unique_temp(tmp_path: Path):
    """When promote_candidate aborts (fingerprint mismatch), the unique temp file
    must be deleted — no leftover staging copy.
    """
    from ledger.imports.pdf.readiness import database_fingerprint
    from ledger.repair.statements import promote_candidate

    target = tmp_path / "ledger.db"
    init_database(target)
    expected_fp = database_fingerprint(target)

    insert_minimal_transaction(target, "txn-repair-abort-cleanup")

    candidate = tmp_path / "candidate.db"
    init_database(candidate)

    with pytest.raises(RuntimeError, match="target DB changed during finalize"):
        promote_candidate(candidate, target, expected_fingerprint=expected_fp)

    leftovers = list(target.parent.glob(f".{target.name}.statement-repair-*.tmp"))
    assert not leftovers, f"Aborted repair promote left temp files behind: {leftovers}"


# ── PRAGMA listener: foreign_keys and busy_timeout ────────────────────────────


def test_get_engine_sets_pragma_foreign_keys_and_busy_timeout(tmp_path: Path):
    """A fresh connection from get_engine must report PRAGMA foreign_keys=1
    and PRAGMA busy_timeout=5000.

    Pre-fix: no listener — foreign_keys defaults to 0, busy_timeout may not be 5000.
    Post-fix: listener fires on every connection and sets both PRAGMAs.
    """
    from ledger.storage.database import clear_engine_cache, get_engine

    db_path = tmp_path / "pragma_test.db"
    init_database(db_path)
    clear_engine_cache()  # ensure a fresh engine (no cached one from other tests)

    engine = get_engine(db_path)

    # Use the engine's underlying raw connection to check the PRAGMAs.
    with engine.connect() as conn:
        fk = conn.execute(__import__("sqlalchemy").text("PRAGMA foreign_keys")).fetchone()[0]
        bt = conn.execute(__import__("sqlalchemy").text("PRAGMA busy_timeout")).fetchone()[0]

    assert fk == 1, f"PRAGMA foreign_keys must be 1 (got {fk!r})"
    assert bt == 5000, f"PRAGMA busy_timeout must be 5000 (got {bt!r})"

    clear_engine_cache()  # clean up


# ── Defect 1: finalize() must PASS expected_fingerprint into promote_database ─


def test_finalize_passes_expected_fingerprint_into_promote_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """finalize() must wire the stage-time import-owned fingerprint into promote_database
    so the drift-recheck guard fires on a genuine concurrent write to import-owned tables.

    Post merge-forward: finalize passes expected_import_owned (not expected_fingerprint)
    so that runtime-table edits do NOT trigger a spurious abort.  The spy asserts that
    expected_import_owned is non-None and equals the stage-time import-owned snapshot.
    """
    from ledger.imports.pdf.files import _promote_database_locked as real_promote_database

    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "run-1"
    _target_db, _staging_db, _csv_root = _setup_staged_run(tmp_path, runs_dir, run_dir)
    paths = _make_finalize_paths(tmp_path, runs_dir)

    # Read back the stage-time import-owned fingerprint so we can assert it is forwarded.
    import json as _json

    run_json = _json.loads((run_dir / "run.json").read_text())
    stage_import_owned = run_json["target_db_import_owned_before"]
    assert stage_import_owned is not None and "tables" in stage_import_owned

    captured: dict[str, Any] = {}

    def spy_promote(
        staging: Path,
        target: Path,
        backup_dir: Path,
        *,
        expected_fingerprint=None,
        expected_import_owned=None,
        expected_candidate_import_owned=None,
        expected_preserved_tables=None,
        label="pre-finalize",
        rollback_path=None,
        publication_token=None,
        on_published=None,
        prepublication_check=None,
    ):
        captured["expected_import_owned"] = expected_import_owned
        captured["expected_candidate_import_owned"] = expected_candidate_import_owned
        captured["expected_fingerprint"] = expected_fingerprint
        captured["label"] = label
        return real_promote_database(
            staging,
            target,
            backup_dir,
            expected_fingerprint=expected_fingerprint,
            expected_import_owned=expected_import_owned,
            expected_candidate_import_owned=expected_candidate_import_owned,
            expected_preserved_tables=expected_preserved_tables,
            label=label,
            rollback_path=rollback_path,
            publication_token=publication_token,
            on_published=on_published,
            prepublication_check=prepublication_check,
        )

    monkeypatch.setattr(workflow, "_promote_database_locked", spy_promote)
    _apply_finalize_mocks(monkeypatch)

    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)  # type: ignore[arg-type]

    assert "expected_import_owned" in captured, (
        "promote_database was never called — finalize() did not reach the promote step"
    )
    passed_io = captured["expected_import_owned"]
    assert passed_io is not None, (
        "finalize() called promote_database with expected_import_owned=None; "
        "the drift-recheck guard is dormant"
    )
    assert passed_io.get("tables") == stage_import_owned["tables"], (
        "finalize() must pass the stage-time import-owned fingerprint to promote_database; "
        f"got tables keys {list(passed_io.get('tables', {}).keys())!r}"
    )
    assert captured["expected_candidate_import_owned"] is not None


def test_finalize_concurrent_write_aborts_via_promote_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """finalize() must abort when the live DB receives a concurrent import-owned write
    in the narrow window between the last pre-promote gate check and the atomic swap.

    The concurrent write (a new transaction row — import-owned table) is injected
    inside promote_database itself so it arrives AFTER all _fail_if_target_db_changed
    gates have passed.  Only the recheck inside promote_database can catch it.

    Post merge-forward: the recheck uses expected_import_owned (not expected_fingerprint).
    A transaction insert changes the import-owned hash → abort → RuntimeError.
    Post-fix: promote_database raises RuntimeError and finalize marks the run 'failed'.
    """
    from ledger.imports.pdf.files import _promote_database_locked as real_promote_database

    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "run-1"
    target_db, _staging_db, _csv_root = _setup_staged_run(tmp_path, runs_dir, run_dir)
    paths = _make_finalize_paths(tmp_path, runs_dir)

    def concurrent_write_inside_promote(
        staging: Path,
        target: Path,
        backup_dir: Path,
        *,
        expected_fingerprint=None,
        expected_import_owned=None,
        expected_candidate_import_owned=None,
        expected_preserved_tables=None,
        label="pre-finalize",
        rollback_path=None,
        publication_token=None,
        on_published=None,
        prepublication_check=None,
    ):
        # Simulate a concurrent import-owned write that lands AFTER all gates but during promote.
        insert_minimal_transaction(target, "txn-concurrent-finalize")
        # Now call the real promote_database — which will see the import-owned mismatch
        # (if expected_import_owned is wired) and raise RuntimeError.
        return real_promote_database(
            staging,
            target,
            backup_dir,
            expected_fingerprint=expected_fingerprint,
            expected_import_owned=expected_import_owned,
            expected_candidate_import_owned=expected_candidate_import_owned,
            expected_preserved_tables=expected_preserved_tables,
            label=label,
            rollback_path=rollback_path,
            publication_token=publication_token,
            on_published=on_published,
            prepublication_check=prepublication_check,
        )

    _apply_finalize_mocks(monkeypatch)
    monkeypatch.setattr(workflow, "_promote_database_locked", concurrent_write_inside_promote)

    with pytest.raises(RuntimeError, match="target DB changed during finalize"):
        workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)  # type: ignore[arg-type]

    # The run must be marked 'failed'.
    import json as _json

    state = _json.loads((run_dir / "run.json").read_text())
    assert state["status"] == "failed", (
        f"expected status='failed' after concurrent-write abort, got {state.get('status')!r}"
    )

    # The live DB must be INTACT — concurrent write must not have been lost.
    with closing(sqlite3.connect(target_db)) as conn:
        count = conn.execute(
            "select count(*) from transactions where txn_id = 'txn-concurrent-finalize'"
        ).fetchone()[0]
    assert count == 1, (
        "concurrent write was lost — promote_database ran without the import-owned recheck guard"
    )


# ── concurrency-fs: atomic JSON writes + guarded readers ──────────────────────


def test_write_json_atomic_temp_in_same_dir(tmp_path: Path):
    """write_json_atomic must write the temp file in the same directory as the target.

    A same-directory temp+os.replace is atomic on a single filesystem. If the temp
    is in a different directory (e.g. /tmp), the rename may cross a mount point and
    lose atomicity. Assert the temp sibling is under the same parent before replace.
    """
    import os as _os
    import unittest.mock as _mock

    from ledger.imports.pdf.files import write_json_atomic

    target = tmp_path / "state.json"
    target.write_text('{"old": 1}', encoding="utf-8")

    captured_replace: list[tuple[str, int | None, int | None, tuple[int, int] | None]] = []
    real_replace = _os.replace

    def spy_replace(src, dst, **kwargs):
        src_dir_fd = kwargs.get("src_dir_fd")
        dst_dir_fd = kwargs.get("dst_dir_fd")
        parent_identity = None
        if src_dir_fd is not None:
            metadata = _os.fstat(src_dir_fd)
            parent_identity = (metadata.st_dev, metadata.st_ino)
        captured_replace.append((str(src), src_dir_fd, dst_dir_fd, parent_identity))
        return real_replace(src, dst, **kwargs)

    with _mock.patch("ledger.utils.json_io.os.replace", side_effect=spy_replace):
        write_json_atomic(target, {"new": 2})

    assert captured_replace, "os.replace was never called"
    src, src_dir_fd, dst_dir_fd, parent_identity = captured_replace[0]
    target_parent = target.parent.stat()
    assert Path(src).parent == Path(".")
    assert src_dir_fd is not None and src_dir_fd == dst_dir_fd
    assert parent_identity == (target_parent.st_dev, target_parent.st_ino)


def test_write_json_atomic_replace_failure_leaves_original_intact(tmp_path: Path):
    """When os.replace raises (e.g. simulated disk failure), the original file
    must remain fully intact — write_json_atomic must not leave a half-written
    or empty target.

    This is the core atomicity guarantee: reader sees OLD or NEW, never partial.
    """
    import unittest.mock as _mock

    from ledger.imports.pdf.files import write_json_atomic

    original_content = '{"status": "ready_to_finalize"}'
    target = tmp_path / "run.json"
    target.write_text(original_content, encoding="utf-8")

    def boom_replace(src, dst, **kwargs):
        raise OSError("Simulated disk failure during rename")

    with (
        _mock.patch("ledger.utils.json_io.os.replace", side_effect=boom_replace),
        pytest.raises(OSError, match="Simulated disk failure"),
    ):
        write_json_atomic(target, {"status": "corrupted"})

    # Original must be fully intact.
    assert target.read_text(encoding="utf-8") == original_content, (
        "Original file was modified or truncated; write_json_atomic is non-atomic"
    )
    # No temp file should remain (it may have been cleaned up or never committed).
    # The original file must be the only .json file here.
    json_files = [f for f in tmp_path.iterdir() if f.suffix == ".json"]
    assert json_files == [target], (
        f"Unexpected files after failed write: {list(tmp_path.iterdir())}"
    )


def test_write_json_atomic_produces_valid_json(tmp_path: Path):
    """write_json_atomic must produce exactly what json.dumps would for the payload,
    readable by json.loads, with the schema unchanged.
    """
    from ledger.imports.pdf.files import write_json_atomic

    payload = {"status": "finalized", "run_id": "2026-06-20T120000Z", "count": 42}
    target = tmp_path / "run.json"
    write_json_atomic(target, payload)

    result = json.loads(target.read_text(encoding="utf-8"))
    assert result == payload


def test_build_import_readiness_returns_sentinel_on_corrupt_run_json(tmp_path: Path):
    """build_import_readiness must NOT propagate JSONDecodeError when run.json is corrupt.

    A partial write (crash mid-write) can produce a truncated file. The reader must
    catch JSONDecodeError and raise FileNotFoundError (clean sentinel) rather than
    letting the raw JSONDecodeError propagate to callers.

    Pre-fix: json.loads raises JSONDecodeError unguarded → this test FAILS.
    Post-fix: function catches the error and raises FileNotFoundError.
    """
    from ledger.imports.pdf.readiness import build_import_readiness

    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    # Write deliberately corrupt/truncated JSON.
    (run_dir / "run.json").write_text('{"status": "ready_to_finali', encoding="utf-8")

    # Must NOT raise JSONDecodeError; must raise FileNotFoundError (clean sentinel).
    with pytest.raises((FileNotFoundError, json.JSONDecodeError)) as exc_info:
        build_import_readiness(run_dir)

    assert not isinstance(exc_info.value, json.JSONDecodeError), (
        "build_import_readiness propagated raw JSONDecodeError for corrupt run.json; "
        "must catch it and raise FileNotFoundError instead"
    )


def test_get_active_import_run_returns_none_on_corrupt_run_json(tmp_path: Path):
    """get_active_import_run must return None (not raise JSONDecodeError) when run.json
    is corrupt/partial.

    Pre-fix: json.loads is unguarded → JSONDecodeError propagates to the UI.
    Post-fix: function catches and returns None (the no-run sentinel).
    """
    from ledger.ui.context import get_active_import_run

    runs_dir = tmp_path / "import-runs"
    runs_dir.mkdir()
    run_dir = runs_dir / "20260620T120000Z"
    run_dir.mkdir()
    (runs_dir / "latest-run.txt").write_text(str(run_dir), encoding="utf-8")
    # Write deliberately corrupt/truncated JSON.
    (run_dir / "run.json").write_text('{"status": "ready_to_fin', encoding="utf-8")

    # Pre-fix: raises JSONDecodeError. Post-fix: returns None.
    result = get_active_import_run(runs_dir)
    assert result is None, (
        f"Expected None for corrupt run.json, got {result!r}; "
        "JSONDecodeError must not propagate from get_active_import_run"
    )


def test_status_on_run_dir_with_missing_run_json_returns_clean_result(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    """status() must return a clean result when latest-run.txt points to a run dir
    that has no run.json (e.g. stage crashed before any writes).

    Pre-fix: read_manifest raises FileNotFoundError → traceback. This test FAILS pre-fix.
    Post-fix: status() prints JSON describing the missing/blank run (no traceback).
    """
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    run_dir = runs_dir / "20260620T000000Z"
    run_dir.mkdir()  # dir exists but NO run.json inside

    paths = Paths(
        pdf_dir=tmp_path / "inbox",
        processed_dir=tmp_path / "processed",
        db_path=tmp_path / "ledger.db",
        base_db_path=None,
        backup_dir=tmp_path / "backups",
        ledger_config=tmp_path / "ledger.toml",
        merchant_rules=tmp_path / "merchants.toml",
        categories=tmp_path / "categories.toml",
        runs_dir=runs_dir,
    )

    # Must NOT raise FileNotFoundError.
    workflow.status(SimpleNamespace(run_dir=run_dir), paths)

    output = capsys.readouterr().out
    # The output must be parseable JSON describing the blank/missing state.
    payload = json.loads(output)
    assert payload.get("status") in {"unknown", "missing", "no_run", None, ""}, (
        f"Expected a blank-run sentinel status, got {payload.get('status')!r}"
    )


# ── concurrency-fs: finalize recovery from promoted-but-not-finalized wedge ───
#
# If a process is KILLED (SIGKILL / power loss) AFTER promote_database returns
# but BEFORE status='finalized' is persisted, the live target DB already holds
# the new data but run.json still says status='finalizing'.  Python's except
# clauses never fire for a killed process, so the P2-6 'finalize_failed' path
# does NOT run.  On retry, the old gate rejected 'finalizing' entirely.
#
# Fix: detect whether promote already happened via the target DB's sha256 (not
# db_counts).  Why sha256?  finalize() calls write_result_to_db() a SECOND time
# before promote, adding an extra import_run row — so staging_db_after (captured
# at stage time, import_runs=N+1) is always one less than the post-promote target
# (import_runs=N+2).  The db_counts equality therefore NEVER holds for a real
# promoted-wedged run: it is a structural false negative in production.
# A sha256 of the live target file is immune: promote_database does a file-level
# replace (shutil.copy2 + os.replace), so the hash changes iff the file changed.


def _apply_stage_mocks_no_write_result(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Patch PDF-extraction and config steps for stage(), but leave write_result_to_db REAL.

    This is critical: write_result_to_db must run for real so the staging DB
    gains a genuine import_run row at stage time.  finalize() will call it again
    (inserting a second import_run row), exactly reproducing the production double-write
    that makes db_counts an unreliable promote-detection signal.
    """
    processed_dir = tmp_path / "processed"

    def fake_process_pdf(pdf: Path, csv_root: Path, _processed_dir: Path, **_kwargs):
        csv_root.mkdir(parents=True, exist_ok=True)
        csv_path = csv_root / "statement.csv"
        csv_path.write_text("Date,Description,Amount\n", encoding="utf-8")
        return SimpleNamespace(
            pdf=pdf,
            csv_path=csv_path,
            processed_pdf=processed_dir / "2026-05-01-test.pdf",
            statement_type="test",
            transaction_count=0,
            skipped_reason="",
            validation_errors=[],
            validation_warnings=[],
            account_slug="statement",
        )

    monkeypatch.setattr(workflow.statement_pdf_to_csv, "process_pdf", fake_process_pdf)
    monkeypatch.setattr(
        workflow.statement_pdf_to_csv,
        "write_manifest",
        lambda path, _results, dry_run: path.write_text("[]", encoding="utf-8"),
    )
    monkeypatch.setattr(workflow, "load_config", lambda _paths, _runtime_db: object())
    monkeypatch.setattr(
        workflow,
        "process",
        lambda **_kwargs: ProcessingResult(total_scanned=1),
    )
    monkeypatch.setattr(workflow, "validate_processing_result", lambda _result, _path: None)
    monkeypatch.setattr(workflow, "build_merchant_review", lambda _result: [])
    monkeypatch.setattr(workflow, "seed_review_candidates", lambda *_args: None)
    monkeypatch.setattr(workflow, "record_db_operation", lambda *_args, **_kwargs: None)


def _apply_finalize_mocks_no_write_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """Like _apply_finalize_mocks but leaves write_result_to_db REAL.

    This ensures the second write_result_to_db call in finalize() inserts a real
    import_run row into staging, reproducing the double-write that causes
    db_counts(target) != staging_db_after in production.
    """
    monkeypatch.setattr(workflow, "load_config", lambda _paths, _db: object())
    monkeypatch.setattr(workflow, "run_migrations", lambda _db: None)
    monkeypatch.setattr(
        workflow,
        "process",
        lambda **_kwargs: ProcessingResult(total_scanned=1),
    )
    monkeypatch.setattr(workflow, "validate_processing_result", lambda *_args: None)
    # write_result_to_db intentionally NOT mocked — must run for real
    monkeypatch.setattr(workflow, "build_merchant_review", lambda _result: [])
    monkeypatch.setattr(workflow, "seed_review_candidates", lambda *_args: None)
    monkeypatch.setattr(workflow, "get_review_rows", lambda _db: [])
    monkeypatch.setattr(workflow, "apply_review_decisions", lambda _db: 0)
    monkeypatch.setattr(workflow, "pending_review_count", lambda _db: 0)
    monkeypatch.setattr(workflow, "run_post_processing", lambda _db, _config: {})
    monkeypatch.setattr(workflow, "record_processed_files", lambda *_args: None)
    monkeypatch.setattr(workflow, "record_db_operation", lambda *_args, **_kwargs: None)


def _run_stage_then_kill_during_finalize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path, Path, Path, Any]:
    """Run a REAL stage() followed by a finalize() killed via KeyboardInterrupt after promote.

    Returns (target_db, staging_db, csv_root, run_dir, real_rmtree).

    The caller MUST restore the real rmtree before calling finalize() a second time:
        monkeypatch.setattr(workflow.shutil, "rmtree", real_rmtree)

    Mechanism:
    - stage() runs with real write_result_to_db → staging DB gets import_runs=1.
    - finalize() runs with real write_result_to_db → staging DB gets import_runs=2
      (the production double-write).
    - promote_database() is called for real → target DB now has the staging content
      (import_runs=2).  The live target sha256 changes.
    - Immediately after promote (shutil.rmtree of csv_root) we raise KeyboardInterrupt.
      This is a BaseException — it bypasses 'except Exception' (P2-6) and leaves
      run.json persisted at status='finalizing'.
    - The caller catches KeyboardInterrupt and inspects the wedged state.

    WHY this test would FAIL with the old db_counts signal:
      After stage, staging_db_after = {import_runs: 1, ...}.
      After finalize's second write_result_to_db, staging has import_runs=2.
      After promote, target also has import_runs=2.
      db_counts(target) == staging_db_after would require import_runs=1 == 2 → FALSE.
      So promote_happened=False → recovery tries to re-promote → hits the fingerprint
      recheck gate → SystemExit('Target database changed since stage') — wrong outcome.
    """
    import shutil as _real_shutil

    real_rmtree = _real_shutil.rmtree  # capture the real function BEFORE any monkeypatching

    pdf_dir = tmp_path / "inbox"
    processed_dir = tmp_path / "processed"
    runs_dir = tmp_path / "runs"
    backup_dir = tmp_path / "backups"
    target_db = tmp_path / "ledger.db"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)
    (pdf_dir / "statement.pdf").write_bytes(b"%PDF fake")
    init_database(target_db)

    paths = Paths(
        pdf_dir=pdf_dir,
        processed_dir=processed_dir,
        db_path=target_db,
        base_db_path=None,
        backup_dir=backup_dir,
        ledger_config=tmp_path / "ledger.toml",
        merchant_rules=tmp_path / "merchants.toml",
        categories=tmp_path / "categories.toml",
        runs_dir=runs_dir,
    )

    # Stage: write_result_to_db runs for real → import_runs row 1
    _apply_stage_mocks_no_write_result(monkeypatch, tmp_path)
    workflow.stage(SimpleNamespace(), paths)

    [run_dir] = [p for p in runs_dir.iterdir() if p.is_dir()]
    staging_db = run_dir / "staging.db"

    # Write a latest-run.txt so we can verify it is cleared on recovery.
    (runs_dir / "latest-run.txt").write_text(str(run_dir), encoding="utf-8")
    (run_dir / "merchant-review.json").write_text("[]", encoding="utf-8")

    # Patch the run state to 'ready_to_finalize' (stage left it as 'needs_review'
    # or 'ready_to_finalize'; ensure it's the latter so finalize() proceeds).
    run_state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    run_state["status"] = "ready_to_finalize"
    write_run_state(run_dir, run_state)

    csv_root_path = Path(run_state["csv_root"])

    # Finalize: write_result_to_db runs for real → import_runs row 2.
    # Then promote_database runs for real → target gets import_runs=2.
    # Then KeyboardInterrupt is raised (bypasses except Exception) → run.json stays
    # at status='finalizing'.
    _apply_finalize_mocks_no_write_result(monkeypatch)

    ki_raised = {"done": False}

    def rmtree_then_ki(path, *args, **kwargs):
        # We raise BEFORE the rmtree so csv_root still exists on recovery retry,
        # but AFTER promote has already succeeded (promote comes just before rmtree).
        if not ki_raised["done"] and str(path) == str(csv_root_path):
            ki_raised["done"] = True
            raise KeyboardInterrupt("simulated SIGKILL after promote")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(workflow.shutil, "rmtree", rmtree_then_ki)

    with contextlib.suppress(KeyboardInterrupt):
        workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    return target_db, staging_db, csv_root_path, run_dir, real_rmtree


def _setup_wedged_pre_promote(
    tmp_path: Path,
    runs_dir: Path,
    run_dir: Path,
) -> tuple[Path, Path, Path]:
    """Directly set up a run wedged at 'finalizing' before promote happened.

    Simulates a kill between the status='finalizing' write and promote_database.
    The target DB sha256 is unchanged from the pre-stage snapshot.

    Returns (target_db, staging_db, csv_root).
    """
    csv_root = run_dir / "csv"
    target_db = tmp_path / "ledger.db"
    staging_db = run_dir / "staging.db"
    csv_root.mkdir(parents=True)
    init_database(target_db)
    init_database(staging_db)

    # staging DB has one transaction (the "staged result").
    insert_minimal_transaction(staging_db, "txn-staged")

    from ledger.imports.pdf.readiness import import_owned_fingerprint

    target_db_fp_before = database_fingerprint(target_db)

    # No promote_database call — target is still at pre-promote state.
    (run_dir / "merchant-review.json").write_text("[]", encoding="utf-8")
    write_run_state(
        run_dir,
        bound_import_state(
            {
                "status": "finalizing",  # ← wedged here (before promote)
                "last_finalize_attempt_at": "2026-06-21T00:00:00Z",
                "target_db_path": str(target_db),
                "staging_db_path": str(staging_db),
                "base_db_path": str(staging_db),
                "csv_root": str(csv_root),
                "staging_inbox": str(run_dir / "inbox"),
                "merchant_review_json": str(run_dir / "merchant-review.json"),
                "pending_review_count": 0,
                "target_db_fingerprint_before": target_db_fp_before,
                "publication_candidate_import_owned": import_owned_fingerprint(staging_db),
                "pdfs": [],
            },
            _make_finalize_paths(tmp_path, run_dir.parent),
        ),
    )
    return target_db, staging_db, csv_root


def test_finalize_wedged_finalizing_promoted_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """concurrency-fs recovery: a run wedged at status='finalizing' after promote
    (SIGKILL scenario) must be detectable and recoverable WITHOUT re-promoting.

    This test exercises the REAL production path to validate the sha256 signal:
    - stage() + finalize() run for real, with write_result_to_db NOT mocked.
    - finalize() calls write_result_to_db a SECOND time (double-write), so the
      staging DB has import_runs=2 at promote time (staging_db_after from stage = 1).
    - The old db_counts signal (staging_db_after == target counts) would therefore
      NEVER match (1 ≠ 2) → false negative → recovery branch unreachable in production.
    - The sha256 signal detects the file-level replace → promote_happened=True. ✓

    Scenario:
    - stage() completes normally (real write_result_to_db → import_runs=1).
    - finalize() runs write_result_to_db again (import_runs=2 in staging).
    - promote_database() succeeds → target has the staged content (import_runs=2).
    - KeyboardInterrupt is raised BEFORE status='finalized' (bypasses except Exception).
    - run.json is stuck at status='finalizing'.
    - On retry, finalize() is called again.

    Expected behaviour (post-fix):
    (a) promote_database is NOT called again.
    (b) The tail is replayed: status='finalized', final-report.json present,
        latest-run.txt cleared.

    FAILS with old db_counts signal: staging_db_after(1) != target counts(2) →
    promote_happened=False → re-promote → fingerprint gate fires → SystemExit.
    PASSES with sha256 signal: target sha256 ≠ pre-stage sha256 → promote_happened=True.
    """
    target_db, _staging_db, _csv_root, run_dir, real_rmtree = _run_stage_then_kill_during_finalize(
        tmp_path, monkeypatch
    )
    runs_dir = tmp_path / "runs"

    # Verify the wedged state: run.json must be at 'finalizing', target must be promoted.
    wedged_state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert wedged_state["status"] == "finalizing", (
        f"Expected run to be wedged at 'finalizing', got {wedged_state.get('status')!r}"
    )
    # Target DB should have been promoted (its sha256 differs from pre-stage snapshot).
    current_fp = database_fingerprint(target_db)
    assert current_fp["sha256"] != wedged_state["target_db_fingerprint_before"]["sha256"], (
        "Target sha256 must differ from pre-stage snapshot after real promote"
    )

    # ── Now retry finalize() — this is the recovery path ──
    paths = _make_finalize_paths(tmp_path, runs_dir)
    promote_call_count = {"n": 0}
    real_promote = workflow._promote_database_locked

    def spy_promote(*args, **kwargs):
        promote_call_count["n"] += 1
        return real_promote(*args, **kwargs)

    monkeypatch.setattr(workflow, "_promote_database_locked", spy_promote)
    # Restore the real rmtree so the recovery finalize() can proceed normally.
    monkeypatch.setattr(workflow.shutil, "rmtree", real_rmtree)

    # Pre-fix this raises or produces a wrong outcome (SystemExit or re-promote).
    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    # (a) promote must NOT have been called again.
    assert promote_call_count["n"] == 0, (
        f"promote_database was called {promote_call_count['n']} time(s); "
        "recovery path must skip re-promote on an already-promoted run"
    )

    # (b) status='finalized', final-report.json present, latest-run cleared.
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state["status"] == "finalized", (
        f"Expected status='finalized' after recovery, got {state.get('status')!r}"
    )
    final_report = run_dir / "final-report.json"
    assert final_report.exists(), "final-report.json must be written on recovery"
    assert not (runs_dir / "latest-run.txt").exists(), (
        "latest-run.txt must be cleared after recovery"
    )


def test_finalize_wedged_finalizing_not_promoted_falls_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """concurrency-fs: a run at 'finalizing' where the live DB has NOT been promoted
    yet must fall through to the normal finalize path (promote runs normally).

    This is the rarer 'killed between status-write and promote_database' scenario.
    The target DB sha256 is unchanged from the pre-stage snapshot → promote_happened=False.
    Expected: finalize() resets to 'ready_to_finalize' and runs the full finalize path.

    Pre-fix: SystemExit('Run is not staged: finalizing') — fails before detection.
    """
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "run-1"
    target_db, _staging_db, _csv_root = _setup_wedged_pre_promote(tmp_path, runs_dir, run_dir)
    # Confirm the target sha256 is unchanged (not promoted yet).
    run_state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    fp_before = run_state["target_db_fingerprint_before"]
    assert database_fingerprint(target_db)["sha256"] == fp_before["sha256"], (
        "Fixture: target sha256 must match pre-stage snapshot when not yet promoted"
    )

    paths = _make_finalize_paths(tmp_path, runs_dir)
    _apply_finalize_mocks(monkeypatch)

    # Pre-fix: raises SystemExit('Run is not staged: finalizing').
    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state["status"] == "finalized", (
        f"Expected status='finalized' after fall-through, got {state.get('status')!r}"
    )
    # The staging data must be promoted now.
    with closing(sqlite3.connect(target_db)) as conn:
        count = conn.execute(
            "select count(*) from transactions where txn_id = 'txn-staged'"
        ).fetchone()[0]
    assert count == 1, "staged data must be promoted on the fall-through path"


def test_finalize_wedged_finalizing_recovery_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery on an already-promoted run must be safe to call multiple times.

    Calling finalize() twice on a 'finalizing' (promoted) run must succeed on the
    first call, and the second call sees status='finalized' and exits cleanly.
    """
    target_db, _staging_db, _csv_root, run_dir, real_rmtree = _run_stage_then_kill_during_finalize(
        tmp_path, monkeypatch
    )
    runs_dir = tmp_path / "runs"
    paths = _make_finalize_paths(tmp_path, runs_dir)

    # Restore the real rmtree so subsequent finalize() calls work normally.
    monkeypatch.setattr(workflow.shutil, "rmtree", real_rmtree)

    # First recovery call succeeds.
    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state["status"] == "finalized"

    with closing(sqlite3.connect(target_db)) as conn:
        audits_before = conn.execute(
            "select count(*) from db_operation_audit where operation = 'import_finalize'"
        ).fetchone()[0]

    # A completed run is now an idempotent verification/repair entrypoint. Re-entry
    # must neither promote again nor duplicate its operation audit.
    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state["status"] == "finalized"
    with closing(sqlite3.connect(target_db)) as conn:
        audits_after = conn.execute(
            "select count(*) from db_operation_audit where operation = 'import_finalize'"
        ).fetchone()[0]
    assert audits_after == audits_before == 1


def test_finalize_wedged_finalizing_operator_message_distinguishes_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Recovery must emit a clear operator message distinguishing promoted-but-not-finalized
    from a true mid-stage failure.
    """
    _target_db2, _staging_db, _csv_root, run_dir, real_rmtree = (
        _run_stage_then_kill_during_finalize(tmp_path, monkeypatch)
    )
    runs_dir = tmp_path / "runs"
    paths = _make_finalize_paths(tmp_path, runs_dir)

    # Restore the real rmtree so the recovery finalize() can complete normally.
    monkeypatch.setattr(workflow.shutil, "rmtree", real_rmtree)

    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    out = capsys.readouterr().out
    # The operator message must indicate recovery from a promoted wedge.
    assert any(
        keyword in out.lower()
        for keyword in ("recover", "promoted", "finaliz", "wedge", "kill", "resuming")
    ), "Expected an operator-visible recovery message on stdout; got: " + repr(out)


def test_finalize_wedged_finalizing_oob_edit_not_silently_finalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A6 drift-case: out-of-band edit to live target during the pre-promote kill window.

    Defect being tested (pre-fix behaviour):
        promote_happened = current_sha256 != sha256_before
        When an out-of-band edit changes the live target BEFORE promote ran, the sha256
        differs from the pre-stage snapshot (sha256_before), so promote_happened wrongly
        evaluates to True.  The recovery tail then sets status='finalized' without ever
        calling promote_database — staging data is silently dropped.

    Fix: promote_happened = target_db.exists() and sha256_file(target_db) == sha256_file(staging_db)
        The live target only equals the staging DB byte-for-byte when promote_database
        did a file-level copy (shutil.copy2 + os.replace).  An out-of-band edit
        produces a different sha256 → promote_happened=False → falls through to the
        normal finalize path.

    Scenario:
        - Run wedged at status='finalizing' BEFORE promote happened.
        - AFTER the wedge but BEFORE recovery, an out-of-band edit changes the live
          target DB (target sha256 now differs from sha256_before, but is NOT equal
          to staging sha256 either — the file is a third, unrelated state).
        - Recovery must NOT take the "promoted" branch.
        - Expected: _fail_if_target_db_changed fires → SystemExit (safe abort, no data
          loss, run still in 'failed' or 'finalizing' state, not 'finalized') OR the
          run falls through to re-promote and the staged data ends up in the live target.
        - In either case: status must NOT be 'finalized' via the silent-replay path,
          and the staged transaction must NOT be silently discarded.

    Confirms this test FAILS pre-fix (status=finalized, staged data dropped) and
    passes post-fix (SystemExit from _fail_if_target_db_changed, or re-promote succeeds).
    """
    from ledger.imports.pdf.files import sha256_file

    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "run-1"
    target_db, staging_db, _csv_root = _setup_wedged_pre_promote(tmp_path, runs_dir, run_dir)

    # Verify initial state: target sha256 matches pre-stage snapshot (promote NOT done).
    run_state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    fp_before = run_state["target_db_fingerprint_before"]
    assert database_fingerprint(target_db)["sha256"] == fp_before["sha256"], (
        "Fixture: target sha256 must match pre-stage snapshot (not yet promoted)"
    )

    # Simulate an out-of-band edit: add a transaction directly to the live target.
    # This changes target sha256 so it differs from sha256_before AND from staging sha256.
    insert_minimal_transaction(target_db, "txn-oob-edit")
    current_target_sha256 = sha256_file(target_db)
    staging_sha256 = sha256_file(staging_db)
    assert current_target_sha256 != fp_before["sha256"], (
        "After OOB edit, target sha256 must differ from pre-stage snapshot"
    )
    assert current_target_sha256 != staging_sha256, (
        "After OOB edit, target sha256 must differ from staging sha256 "
        "(otherwise this wouldn't exercise the defect)"
    )

    paths = _make_finalize_paths(tmp_path, runs_dir)
    _apply_finalize_mocks(monkeypatch)

    # The defect: pre-fix recovery sees sha256 changed → wrongly treats it as promoted
    # → replays tail → status='finalized', staged data dropped.
    # The fix: recovery checks target sha256 == staging sha256 → they differ → NOT promoted
    # → falls through to normal finalize → _fail_if_target_db_changed aborts (SystemExit)
    # because target was edited out-of-band.
    # Either outcome (SystemExit abort OR re-promote) is acceptable; what is NOT acceptable
    # is status='finalized' via the silent replay path.

    promote_call_count = {"n": 0}
    real_promote = workflow._promote_database_locked

    def spy_promote(*args, **kwargs):
        promote_call_count["n"] += 1
        return real_promote(*args, **kwargs)

    monkeypatch.setattr(workflow, "_promote_database_locked", spy_promote)

    # Recovery must NOT silently finalize with dropped staged data.
    # Acceptable outcomes: SystemExit (safe abort) or re-promote (staged data promoted).
    try:
        workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)
        # If finalize() returned without raising, a re-promote must have happened.
        assert promote_call_count["n"] >= 1, (
            "finalize() returned without raising — expected at least one promote_database call "
            "since the live target was edited out-of-band (not the staging result)"
        )
        # And the staged transaction must be visible in the live target.
        with closing(sqlite3.connect(target_db)) as conn:
            count = conn.execute(
                "select count(*) from transactions where txn_id = 'txn-staged'"
            ).fetchone()[0]
        assert count == 1, "staged transaction must be present in target after re-promote"
    except SystemExit:
        # Safe abort from _fail_if_target_db_changed: acceptable outcome.
        # The run must NOT be finalized — either 'failed' or still 'finalizing'.
        final_state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        assert final_state["status"] != "finalized", (
            "status must not be 'finalized' when recovery aborted due to OOB edit"
        )
        # The staged transaction must NOT be silently discarded (staging DB is untouched).
        with closing(sqlite3.connect(staging_db)) as conn:
            count = conn.execute(
                "select count(*) from transactions where txn_id = 'txn-staged'"
            ).fetchone()[0]
        assert count == 1, "staged transaction must remain in staging DB after safe abort"
