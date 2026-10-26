import contextlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import textwrap
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from import_binding_fixtures import bound_import_state

from ledger.config.repository import SQLiteConfigRepository
from ledger.config.schemas import CategoriesConfig, CategoryRule, MerchantRulesConfig
from ledger.imports.pdf import workflow
from ledger.imports.pdf.paths import Paths
from ledger.imports.pdf.readiness import (
    PublicationProofError,
    database_fingerprint,
    import_owned_fingerprint,
    publication_candidate_fingerprint,
    validate_publication_protocol_state,
)
from ledger.imports.pdf.statement_pdf_to_csv import ExtractedCsv
from ledger.processing import ProcessingResult
from ledger.rules.categorization import CategorizationService
from ledger.storage.database import (
    DBOperationAuditDB,
    ProcessedFileDB,
    init_database,
    session_scope,
)


def _paths(tmp_path: Path) -> Paths:
    pdf_dir = tmp_path / "inbox"
    processed_dir = tmp_path / "processed"
    runs_dir = tmp_path / "runs"
    pdf_dir.mkdir()
    processed_dir.mkdir()
    init_database(tmp_path / "ledger.db")
    return Paths(
        pdf_dir=pdf_dir,
        processed_dir=processed_dir,
        db_path=tmp_path / "ledger.db",
        base_db_path=None,
        backup_dir=tmp_path / "backups",
        ledger_config=tmp_path / "ledger.toml",
        merchant_rules=tmp_path / "merchants.toml",
        categories=tmp_path / "categories.toml",
        runs_dir=runs_dir,
    )


def _extract_one(pdf: Path, csv_root: Path, processed_dir: Path) -> ExtractedCsv:
    csv_path = csv_root / "2026-01" / "harbor-chequing-1001.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text(
        "Ledger Format,Date,Description,Amount,Currency,Reference,Institution,Account Type\nledger-mapped-v1,2026-01-02,SYNTHETIC,1.00,CAD,,harbor,Chequing\n",
        encoding="utf-8",
    )
    return ExtractedCsv(
        pdf=pdf,
        csv_path=csv_path,
        processed_pdf=processed_dir / "2026-01-31-harbor-deposit-chequing-1001.pdf",
        statement_type="harbor_deposit",
        transaction_count=1,
        account_slug="harbor-chequing-1001",
    )


@pytest.mark.parametrize(
    ("version", "tokenized"),
    [(1, False), (1, True), (2, True)],
)
@pytest.mark.parametrize("phase", ["preparing", "candidate_ready", "published"])
def test_publication_protocol_accepts_both_committed_v1_encodings_and_canonical_v2(
    version: int,
    tokenized: bool,
    phase: str,
) -> None:
    state: dict[str, object] = {
        "status": "finalizing",
        "finalize_protocol_version": version,
        "publication_phase": phase,
    }
    if tokenized:
        state["publication_token"] = "a" * 32
    if phase != "preparing":
        state["publication_candidate_import_owned"] = {"tables": {}}
        state["finalize_tail"] = {"idempotency_key": "synthetic"}
    if phase == "published":
        state["publication_completed"] = True
        if tokenized:
            state["publication_target_identity"] = {"dev": 1, "ino": 2}

    validate_publication_protocol_state(state)


@pytest.mark.parametrize(
    "state",
    [
        {
            "status": "finalizing",
            "finalize_protocol_version": 1,
            "publication_phase": "candidate_ready",
            "publication_token": "a" * 32,
            "publication_candidate_import_owned": {"tables": {}},
            "finalize_tail": {"idempotency_key": "synthetic"},
            "publication_target_identity": {"dev": 1, "ino": 2},
        },
        {
            "status": "finalizing",
            "finalize_protocol_version": 1,
            "publication_phase": "published",
            "publication_candidate_import_owned": {"tables": {}},
            "finalize_tail": {"idempotency_key": "synthetic"},
            "publication_completed": True,
            "publication_target_identity": {"dev": 1, "ino": 2},
        },
        {
            "status": "finalizing",
            "finalize_protocol_version": 2,
            "publication_phase": "published",
            "publication_candidate_import_owned": {"tables": {}},
            "finalize_tail": {"idempotency_key": "synthetic"},
            "publication_completed": True,
            "publication_target_identity": {"dev": 1, "ino": 2},
        },
    ],
)
def test_publication_protocol_rejects_mixed_or_incomplete_encodings(
    state: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="publication state"):
        validate_publication_protocol_state(state)


def test_stage_persists_redacted_ordinary_processing_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    (paths.pdf_dir / "statement.pdf").write_bytes(b"%PDF synthetic")
    monkeypatch.setattr(
        workflow.statement_pdf_to_csv,
        "process_pdf",
        lambda pdf, csv_root, processed_dir, **_kwargs: _extract_one(pdf, csv_root, processed_dir),
    )
    monkeypatch.setattr(workflow, "load_config", lambda *_args: object())
    monkeypatch.setattr(
        workflow,
        "process",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("TOP SECRET statement content")),
    )

    with pytest.raises(RuntimeError, match="TOP SECRET"):
        workflow.stage(SimpleNamespace(), paths)

    [run_dir] = [path for path in paths.runs_dir.iterdir() if path.is_dir()]
    state_text = (run_dir / "run.json").read_text(encoding="utf-8")
    state = json.loads(state_text)
    assert state["status"] == "failed"
    assert state["failure_reason"] == "processing_exception"
    assert state["failure_exception_type"] == "RuntimeError"
    assert "TOP SECRET" not in state_text


def test_stage_redacts_per_pdf_parser_exception(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    (paths.pdf_dir / "statement.pdf").write_bytes(b"%PDF synthetic")
    monkeypatch.setattr(
        workflow.statement_pdf_to_csv,
        "process_pdf",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("TOP SECRET parser bytes")),
    )

    workflow.stage(SimpleNamespace(), paths)

    [run_dir] = [path for path in paths.runs_dir.iterdir() if path.is_dir()]
    serialized = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (
            run_dir / "run.json",
            run_dir / "extraction-manifest.json",
            run_dir / "statement-validation.json",
        )
    )
    assert "TOP SECRET" not in serialized
    assert "parser_exception" in serialized
    assert "RuntimeError" in serialized


def test_cancel_cannot_complete_while_stage_owns_the_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    pdf = paths.pdf_dir / "statement.pdf"
    pdf.write_bytes(b"%PDF synthetic")
    cancel_refused = False

    def extract_while_cancel_probes(
        pdf_path: Path,
        csv_root: Path,
        processed_dir: Path,
        **_kwargs: object,
    ) -> ExtractedCsv:
        nonlocal cancel_refused
        [run_dir] = [path for path in paths.runs_dir.iterdir() if path.is_dir()]
        with pytest.raises(SystemExit, match="already in progress"):
            workflow.cancel(SimpleNamespace(run_dir=run_dir, reason="probe"), paths)
        cancel_refused = True
        return _extract_one(pdf_path, csv_root, processed_dir)

    monkeypatch.setattr(workflow.statement_pdf_to_csv, "process_pdf", extract_while_cancel_probes)
    monkeypatch.setattr(workflow, "load_config", lambda *_args: object())
    monkeypatch.setattr(workflow, "process", lambda **_kwargs: ProcessingResult(total_scanned=1))
    monkeypatch.setattr(workflow, "validate_processing_result", lambda *_args: None)
    monkeypatch.setattr(workflow, "write_result_to_db", lambda *_args: None)
    monkeypatch.setattr(workflow, "build_merchant_review", lambda _result: [])

    workflow.stage(SimpleNamespace(), paths)

    assert cancel_refused
    [run_dir] = [path for path in paths.runs_dir.iterdir() if path.is_dir()]
    assert json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["status"] == (
        "ready_to_finalize"
    )


def test_cancel_is_possible_after_a_crashed_stage_releases_its_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    (paths.pdf_dir / "statement.pdf").write_bytes(b"%PDF synthetic")
    monkeypatch.setattr(
        workflow.statement_pdf_to_csv,
        "process_pdf",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt("synthetic crash")),
    )

    with pytest.raises(KeyboardInterrupt, match="synthetic crash"):
        workflow.stage(SimpleNamespace(), paths)

    [run_dir] = [path for path in paths.runs_dir.iterdir() if path.is_dir()]
    workflow.cancel(SimpleNamespace(run_dir=run_dir, reason="recovery"), paths)

    assert json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["status"] == "cancelled"


def _ready_finalize_run(
    tmp_path: Path,
    *,
    with_pdf: bool = False,
    source_in_processed: bool = False,
) -> tuple[Paths, Path, Path, Path]:
    paths = _paths(tmp_path)
    run_dir = paths.runs_dir / "run-1"
    csv_root = run_dir / "csv"
    staging_db = run_dir / "staging.db"
    csv_root.mkdir(parents=True)
    (run_dir / "inbox").mkdir()
    init_database(staging_db)
    now = datetime.now(UTC).replace(tzinfo=None)
    with session_scope(staging_db) as session:
        session.add(
            ProcessedFileDB(
                file_path="synthetic-staged-marker.pdf",
                file_hash="0" * 64,
                processed_at=now,
                transaction_count=1,
            )
        )
    (run_dir / "merchant-review.json").write_text("[]", encoding="utf-8")

    pdfs: list[dict[str, object]] = []
    source_root = paths.processed_dir if source_in_processed else paths.pdf_dir
    if with_pdf:
        original_pdf = source_root / "synthetic-statement.pdf"
        original_pdf.write_bytes(b"%PDF synthetic archive")
        pdfs.append(
            {
                "original_pdf": str(original_pdf),
                "processed_pdf": str(paths.processed_dir / "synthetic-statement.pdf"),
                "file_hash": workflow.sha256_file(original_pdf),
                "transaction_count": 1,
                "skipped_reason": "",
            }
        )

    workflow.write_run_state(
        run_dir,
        bound_import_state(
            {
                "version": 1,
                "status": "ready_to_finalize",
                "target_db_path": str(paths.db_path),
                "staging_db_path": str(staging_db),
                "base_db_path": str(paths.db_path),
                "pdf_dir": str(source_root),
                "processed_dir": str(paths.processed_dir),
                "csv_root": str(csv_root),
                "staging_inbox": str(run_dir / "inbox"),
                "merchant_review_json": str(run_dir / "merchant-review.json"),
                "pending_review_count": 0,
                "approved_csv_paths": [],
                "account_slugs": {},
                "target_db_fingerprint_before": database_fingerprint(paths.db_path),
                "target_db_import_owned_before": import_owned_fingerprint(paths.db_path),
                "stage_counts": {"transactions": 1, "transfers": 0},
                "pdfs": pdfs,
            },
            paths,
        ),
    )
    (paths.runs_dir / "latest-run.txt").write_text(str(run_dir), encoding="utf-8")
    return paths, run_dir, staging_db, csv_root


def _patch_finalize_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workflow, "load_config", lambda *_args: object())
    monkeypatch.setattr(workflow, "run_migrations", lambda _db: None)
    monkeypatch.setattr(
        workflow,
        "process",
        lambda **_kwargs: ProcessingResult(total_scanned=2),
    )
    monkeypatch.setattr(workflow, "validate_processing_result", lambda *_args: None)
    monkeypatch.setattr(workflow, "write_result_to_db", lambda *_args: None)
    monkeypatch.setattr(workflow, "build_merchant_review", lambda _result: [])
    monkeypatch.setattr(workflow, "seed_review_candidates", lambda *_args: None)
    monkeypatch.setattr(workflow, "get_review_rows", lambda _db: [{"review_key": "synthetic"}])
    monkeypatch.setattr(workflow, "apply_review_decisions", lambda _db: 0)
    monkeypatch.setattr(workflow, "pending_review_count", lambda _db: 0)
    monkeypatch.setattr(
        workflow,
        "run_post_processing",
        lambda _db, _config: {"accounts": {"saved": 1}},
    )
    monkeypatch.setattr(workflow, "record_processed_files", lambda *_args: None)
    monkeypatch.setattr(
        workflow,
        "result_counts",
        lambda _result: {"transactions": 7, "transfers": 2},
    )
    monkeypatch.setattr(
        workflow,
        "summarize_final_merchants",
        lambda *_args: {
            "resolved_merchants": ["SYNTHETIC"],
            "manual_review_merchants": [],
        },
    )
    monkeypatch.setattr(workflow, "print_merchant_summary", lambda _summary: None)

    class SyntheticFinancialBriefService:
        def __init__(self, db_path: Path) -> None:
            assert db_path.is_file()

        def build_default(self, *, since: datetime | None = None) -> dict[str, object]:
            return {
                "schema_version": 1,
                "status": "reported",
                "since": None if since is None else since.isoformat(),
                "observations": [
                    {
                        "rank": 1,
                        "label": "change",
                        "title": "Synthetic import completed",
                    }
                ],
            }

    monkeypatch.setattr(workflow, "FinancialBriefService", SyntheticFinancialBriefService)


def _finalize_audits(db_path: Path) -> list[DBOperationAuditDB]:
    with session_scope(db_path) as session:
        return (
            session.query(DBOperationAuditDB)
            .filter(DBOperationAuditDB.operation == "import_finalize")
            .order_by(DBOperationAuditDB.id)
            .all()
        )


def test_report_failure_after_promote_retries_tail_without_repromoting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, run_dir, _staging_db, _csv_root = _ready_finalize_run(tmp_path)
    _patch_finalize_dependencies(monkeypatch)
    real_promote = workflow._promote_database_locked
    promote_calls = 0

    def count_promote(*args, **kwargs):
        nonlocal promote_calls
        promote_calls += 1
        return real_promote(*args, **kwargs)

    monkeypatch.setattr(workflow, "_promote_database_locked", count_promote)
    real_write_json = workflow.write_json
    failed_once = False

    def fail_first_report(path: Path, payload: object) -> None:
        nonlocal failed_once
        if path.name == "final-report.json" and not failed_once:
            failed_once = True
            raise OSError("synthetic final report fault")
        real_write_json(path, payload)

    monkeypatch.setattr(workflow, "write_json", fail_first_report)

    with pytest.raises(OSError, match="synthetic final report fault"):
        workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    failed_state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert failed_state["status"] != "finalized"

    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    report = json.loads((run_dir / "final-report.json").read_text(encoding="utf-8"))
    assert promote_calls == 1
    assert state["status"] == "finalized"
    assert state["archive_outcome"] == {
        "status": "success",
        "attempted": 0,
        "archived": 0,
        "warnings": [],
    }
    assert report["counts"] == {"transactions": 7, "transfers": 2}
    assert report["merchant_summary"]["resolved_merchants"] == ["SYNTHETIC"]
    assert len(_finalize_audits(paths.db_path)) == 1


def test_failure_after_audit_insert_retries_to_exactly_one_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, run_dir, _staging_db, _csv_root = _ready_finalize_run(tmp_path)
    _patch_finalize_dependencies(monkeypatch)
    real_write_run_state = workflow.write_run_state
    failed_once = False

    def fail_final_state_after_audit(path: Path, state: dict) -> None:
        nonlocal failed_once
        if (
            state.get("status") == "finalized"
            and len(_finalize_audits(paths.db_path)) == 1
            and not failed_once
        ):
            failed_once = True
            raise OSError("synthetic state fault after audit")
        real_write_run_state(path, state)

    monkeypatch.setattr(workflow, "write_run_state", fail_final_state_after_audit)

    with pytest.raises(OSError, match="synthetic state fault after audit"):
        workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    assert len(_finalize_audits(paths.db_path)) == 1
    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)
    assert len(_finalize_audits(paths.db_path)) == 1
    assert json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["status"] == "finalized"


def test_post_promote_state_write_crash_recovers_from_prepersisted_tail_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, run_dir, _staging_db, _csv_root = _ready_finalize_run(tmp_path)
    _patch_finalize_dependencies(monkeypatch)
    real_promote = workflow._promote_database_locked
    promoted = False
    promote_calls = 0

    def mark_promoted(*args, **kwargs):
        nonlocal promoted, promote_calls
        promote_calls += 1
        result = real_promote(*args, **kwargs)
        promoted = True
        return result

    monkeypatch.setattr(workflow, "_promote_database_locked", mark_promoted)
    real_write_run_state = workflow.write_run_state
    crashed = False

    def crash_first_post_promote_state(path: Path, state: dict) -> None:
        nonlocal crashed
        if promoted and not crashed:
            crashed = True
            raise KeyboardInterrupt("synthetic process death after promote")
        real_write_run_state(path, state)

    monkeypatch.setattr(workflow, "write_run_state", crash_first_post_promote_state)

    with pytest.raises(KeyboardInterrupt, match="synthetic process death"):
        workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    wedged = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert wedged["status"] == "finalizing"
    assert wedged["publication_candidate_import_owned"] == import_owned_fingerprint(_staging_db)
    assert wedged["finalize_tail"]["counts"] == {"transactions": 7, "transfers": 2}
    assert wedged["finalize_tail"]["merchant_summary"]["resolved_merchants"] == ["SYNTHETIC"]
    rollback = Path(wedged["finalize_tail"]["target_db_backup"])
    assert rollback.is_file()

    monkeypatch.setattr(workflow, "write_run_state", real_write_run_state)
    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    report = json.loads((run_dir / "final-report.json").read_text(encoding="utf-8"))
    assert promote_calls == 1
    assert report["counts"] == {"transactions": 7, "transfers": 2}
    assert report["merchant_summary"]["resolved_merchants"] == ["SYNTHETIC"]


def test_markerless_recovery_reads_a_real_long_lived_wal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opening a pinned main file through /dev/fd loses its sibling WAL and must fail this."""
    paths, run_dir, staging_db, _csv_root = _ready_finalize_run(tmp_path)
    _patch_finalize_dependencies(monkeypatch)
    connection = sqlite3.connect(paths.db_path)
    try:
        assert connection.execute("pragma journal_mode=wal").fetchone() == ("wal",)
        connection.execute("pragma wal_autocheckpoint=0")
        # Retain a read snapshot so publication cannot checkpoint its replacement
        # entirely into the target's main file.
        connection.execute("begin")
        connection.execute("select count(*) from processed_files").fetchone()

        state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        state.update(
            {
                "status": "finalizing",
                "publication_candidate_import_owned": import_owned_fingerprint(staging_db),
            }
        )
        workflow.write_run_state(run_dir, state)
        workflow.promote_database(staging_db, paths.db_path, paths.backup_dir)
        wal_path = Path(f"{paths.db_path}-wal")
        assert wal_path.is_file() and wal_path.stat().st_size > 0
        assert (
            import_owned_fingerprint(paths.db_path) == state["publication_candidate_import_owned"]
        )

        monkeypatch.setattr(
            workflow,
            "_promote_database_locked",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("post-publish WAL recovery must not re-promote")
            ),
        )
        workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)
    finally:
        connection.close()

    recovered = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert recovered["status"] == "finalized"
    assert recovered["publication_completed"] is True


def test_candidate_fingerprint_is_bound_against_connect_time_lexical_aba(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Restoring the pathname before post-connect validation must not fool the proof."""
    from ledger.imports.pdf import readiness
    from ledger.storage.snapshots import snapshot_database

    paths, run_dir, staging_db, _csv_root = _ready_finalize_run(tmp_path)
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    expected = import_owned_fingerprint(staging_db)
    replacement = run_dir / "replacement.db"
    snapshot_database(paths.db_path, replacement)
    displaced = run_dir / "displaced.db"
    real_connect = sqlite3.connect
    swapped = False

    def swap_only_during_connect(database: object, *args: object, **kwargs: object):
        nonlocal swapped
        if not swapped:
            swapped = True
            os.replace(staging_db, displaced)
            os.replace(replacement, staging_db)
            try:
                return real_connect(database, *args, **kwargs)
            finally:
                os.replace(staging_db, replacement)
                os.replace(displaced, staging_db)
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(readiness.sqlite3, "connect", swap_only_during_connect)

    observed = publication_candidate_fingerprint(run_dir, state)

    assert swapped
    assert observed == expected


def test_bound_alias_connect_time_aba_is_refused_even_after_name_restoration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replacing the alias only while SQLite opens must not redirect its real main fd."""
    from ledger.storage import snapshots
    from ledger.storage.snapshots import snapshot_database

    paths, run_dir, staging_db, _csv_root = _ready_finalize_run(tmp_path)
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    replacement = run_dir / "replacement.db"
    snapshot_database(paths.db_path, replacement)
    real_connect = snapshots.sqlite3.connect
    attacked = False

    def replace_alias_only_during_connect(
        database: object,
        *args: object,
        **kwargs: object,
    ) -> sqlite3.Connection:
        nonlocal attacked
        text = str(database)
        if not attacked and ".ledger-sqlite-alias-" in text and "source.db" in text:
            alias = Path(text.split("?", 1)[0].removeprefix("file:"))
            saved = alias.with_name("source-original.db")
            attacked = True
            alias.parent.chmod(0o700)
            alias.rename(saved)
            os.link(replacement, alias)
            try:
                connection = real_connect(database, *args, **kwargs)
            finally:
                alias.unlink()
                saved.rename(alias)
                alias.parent.chmod(0o700)
            return connection
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(snapshots.sqlite3, "connect", replace_alias_only_during_connect)

    with pytest.raises(PublicationProofError, match="Publication recovery proof"):
        publication_candidate_fingerprint(run_dir, state)

    assert attacked
    assert import_owned_fingerprint(staging_db) != import_owned_fingerprint(replacement)


def test_sqlite_opened_file_audit_treats_a_reused_descriptor_as_new(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A descriptor number is not identity: close+reuse must audit the new tuple."""
    from ledger.storage import snapshots

    before = {
        7: (10, 11, "/synthetic/sacrificial"),
        8: (20, 21, "/synthetic/pinned-main"),
    }
    after = {
        7: (30, 31, "/synthetic/replacement-main"),
        8: (20, 21, "/synthetic/pinned-main"),
        9: (40, 41, "/synthetic/expected-wal"),
    }
    monkeypatch.setattr(snapshots, "_open_fd_snapshot", lambda: after)

    def header_for(descriptor: int, _length: int, _offset: int) -> bytes:
        if descriptor in {7, 8}:
            return b"SQLite format 3\x00"
        return b"\x37\x7f\x06\x82"

    monkeypatch.setattr(snapshots.os, "pread", header_for)

    with pytest.raises(OSError, match="unbound database inode"):
        snapshots._audit_sqlite_opened_files(
            before,
            expected={"source.db": (20, 21), "source.db-wal": (40, 41)},
        )


def test_sqlite_opened_file_audit_ignores_path_drift_on_an_existing_inode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F_GETPATH drift on an already-open fd must not impersonate a new open."""
    from ledger.storage import snapshots

    before = {
        7: (10, 11, ""),
        8: (20, 21, "/synthetic/pinned-main"),
    }
    after = {
        7: (10, 11, "/synthetic/canonical-existing"),
        8: (20, 21, "/synthetic/pinned-main"),
        9: (20, 21, "/synthetic/alias/source.db"),
    }
    monkeypatch.setattr(snapshots, "_open_fd_snapshot", lambda: after)
    monkeypatch.setattr(
        snapshots.os,
        "pread",
        lambda _descriptor, _length, _offset: b"SQLite format 3\x00",
    )

    snapshots._audit_sqlite_opened_files(before, expected={"source.db": (20, 21)})


def test_audited_writer_owns_the_commit_after_the_caller_returns(
    tmp_path: Path,
) -> None:
    from ledger.storage.snapshots import audited_database_write_connection

    root = (tmp_path / "audited-writer-commit").resolve()
    root.mkdir(mode=0o700)
    database = root / "ledger.db"
    init_database(database)
    database.chmod(0o600)

    with audited_database_write_connection(database, label="audited writer") as connection:
        connection.execute(
            "insert into config_metadata (key, value) values (?, ?)",
            ("synthetic_atomic_commit", "yes"),
        )

    with contextlib.closing(sqlite3.connect(database)) as connection, connection:
        assert connection.execute(
            "select value from config_metadata where key = ?",
            ("synthetic_atomic_commit",),
        ).fetchone() == ("yes",)


def test_audited_writer_refuses_namespace_drift_before_commit(
    tmp_path: Path,
) -> None:
    from ledger.storage.snapshots import audited_database_write_connection, snapshot_database

    root = (tmp_path / "audited-writer-drift").resolve()
    root.mkdir(mode=0o700)
    database = root / "ledger.db"
    init_database(database)
    database.chmod(0o600)
    decoy = root / "decoy.db"
    snapshot_database(database, decoy)
    displaced = root / "displaced.db"

    with (
        pytest.raises(OSError),
        audited_database_write_connection(database, label="audited writer") as connection,
    ):
        connection.execute(
            "insert into config_metadata (key, value) values (?, ?)",
            ("must_rollback", "yes"),
        )
        database.rename(displaced)
        decoy.rename(database)

    database.rename(decoy)
    displaced.rename(database)
    with contextlib.closing(sqlite3.connect(database)) as connection, connection:
        assert connection.execute(
            "select count(*) from config_metadata where key = ?",
            ("must_rollback",),
        ).fetchone() == (0,)


def test_candidate_fingerprint_independently_requires_owner_only_main_database(
    tmp_path: Path,
) -> None:
    from ledger.storage.snapshots import bound_database_read_connection

    _paths_value, _run_dir, staging_db, _csv_root = _ready_finalize_run(tmp_path)
    staging_db.chmod(0o644)

    with (
        pytest.raises(PermissionError, match="source file is not owner-only"),
        bound_database_read_connection(
            staging_db,
            label="Main-mode mutation guard",
            require_main_owner_only=True,
            require_parent_owner_only=True,
        ),
    ):
        pass


@pytest.mark.parametrize("widened_component", ["parent", "main", "alias"])
def test_bound_reader_revalidates_parent_main_and_alias_modes_after_connect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    widened_component: str,
) -> None:
    from ledger.imports.pdf import readiness
    from ledger.storage import snapshots

    _paths_value, run_dir, staging_db, _csv_root = _ready_finalize_run(tmp_path)
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    real_connect = snapshots.sqlite3.connect
    real_fingerprint = readiness.import_owned_fingerprint
    widened_alias: Path | None = None
    fingerprinted = False

    def observe_fingerprint(database: object) -> dict:
        nonlocal fingerprinted
        fingerprinted = True
        return real_fingerprint(database)

    def widen_through_next_validation(
        database: object,
        *args: object,
        **kwargs: object,
    ) -> sqlite3.Connection:
        nonlocal widened_alias
        text = str(database)
        if widened_alias is None and ".ledger-sqlite-alias-" in text and "source.db" in text:
            alias = Path(text.split("?", 1)[0].removeprefix("file:"))
            if widened_component == "main":
                staging_db.chmod(0o644)
            elif widened_component == "parent":
                run_dir.chmod(0o755)
            else:
                alias.parent.chmod(0o755)
            widened_alias = alias.parent
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(snapshots.sqlite3, "connect", widen_through_next_validation)
    monkeypatch.setattr(readiness, "import_owned_fingerprint", observe_fingerprint)
    try:
        with pytest.raises(PublicationProofError, match="Publication recovery proof"):
            publication_candidate_fingerprint(run_dir, state)
    finally:
        staging_db.chmod(0o600)
        run_dir.chmod(0o700)
        if widened_alias is not None and widened_alias.exists():
            widened_alias.chmod(0o700)

    assert widened_alias is not None
    assert fingerprinted is False, "unsafe modes must refuse before any caller query"


def test_one_shot_alias_unlink_failure_leaves_no_sensitive_hardlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.storage import snapshots

    _paths_value, run_dir, _staging_db, _csv_root = _ready_finalize_run(tmp_path)
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    real_unlink = snapshots.os.unlink
    faulted = False

    def fail_first_sensitive_unlink(path: object, *args: object, **kwargs: object) -> None:
        nonlocal faulted
        if not faulted and str(path) == "source.db" and kwargs.get("dir_fd") is not None:
            faulted = True
            raise OSError("synthetic one-shot alias unlink failure")
        real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(snapshots.os, "unlink", fail_first_sensitive_unlink)
    with pytest.raises(PublicationProofError, match="Publication recovery proof"):
        publication_candidate_fingerprint(run_dir, state)

    assert faulted
    assert list(run_dir.glob(".ledger-sqlite-alias-*")) == []


def test_alias_cleanup_attempts_main_after_an_earlier_persistent_wal_unlink_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.storage import snapshots
    from ledger.storage.snapshots import bound_database_read_connection

    root = (tmp_path / "cleanup-continues").resolve()
    root.mkdir(mode=0o700)
    source = root / "source.db"
    init_database(source)
    source.chmod(0o600)
    writer = sqlite3.connect(source)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("create table cleanup_probe (value text)")
        writer.execute("insert into cleanup_probe values ('synthetic')")
        writer.commit()
        Path(f"{source}-wal").chmod(0o600)
        Path(f"{source}-shm").chmod(0o600)

        real_unlink = snapshots.os.unlink
        attempted_main = False

        def refuse_wal_but_observe_main(
            path: object,
            *args: object,
            **kwargs: object,
        ) -> None:
            nonlocal attempted_main
            if kwargs.get("dir_fd") is not None and str(path) == "source.db-wal":
                raise OSError("synthetic persistent WAL unlink failure")
            if kwargs.get("dir_fd") is not None and str(path) == "source.db":
                attempted_main = True
            real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(snapshots.os, "unlink", refuse_wal_but_observe_main)
        with (
            pytest.raises(OSError),
            bound_database_read_connection(
                source,
                label="cleanup continuation guard",
                require_main_owner_only=True,
                require_parent_owner_only=True,
            ),
        ):
            pass
        assert attempted_main, "cleanup must attempt every sensitive alias after one failure"
    finally:
        writer.close()


def test_persistent_alias_unlink_failure_retains_control_evidence_for_later_reap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.storage import snapshots
    from ledger.storage.snapshots import bound_database_read_connection

    root = (tmp_path / "persistent-cleanup-evidence").resolve()
    root.mkdir(mode=0o700)
    source = root / "source.db"
    init_database(source)
    source.chmod(0o600)
    keeper = sqlite3.connect(source)
    try:
        assert keeper.execute("pragma journal_mode=wal").fetchone() == ("wal",)
        keeper.execute("create table residue_probe (value text)")
        keeper.execute("insert into residue_probe values ('synthetic')")
        keeper.commit()
        Path(f"{source}-wal").chmod(0o600)
        Path(f"{source}-shm").chmod(0o600)
        real_unlink = snapshots.os.unlink

        def persistently_refuse_wal(path: object, *args: object, **kwargs: object) -> None:
            if kwargs.get("dir_fd") is not None and str(path) == "source.db-wal":
                raise OSError("synthetic persistent WAL unlink failure")
            real_unlink(path, *args, **kwargs)

        monkeypatch.setattr(snapshots.os, "unlink", persistently_refuse_wal)
        with (
            pytest.raises(OSError, match="alias cleanup failed"),
            bound_database_read_connection(
                source,
                label="persistent cleanup evidence",
                require_main_owner_only=True,
                require_parent_owner_only=True,
            ),
        ):
            pass

        [alias] = list(root.glob(".ledger-sqlite-alias-*"))
        assert (alias / "source.db-wal").is_file()
        assert (alias / ".identity.json").is_file()
        assert (alias / ".lease").is_file()

        monkeypatch.setattr(snapshots.os, "unlink", real_unlink)
        with bound_database_read_connection(
            source,
            label="later cleanup retry",
            require_main_owner_only=True,
            require_parent_owner_only=True,
        ) as connection:
            assert connection.execute("select count(*) from residue_probe").fetchone() == (1,)
        assert list(root.glob(".ledger-sqlite-alias-*")) == []
    finally:
        keeper.close()


def test_alias_setup_and_stale_reaping_are_serialized_between_readers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.storage import snapshots
    from ledger.storage.snapshots import bound_database_read_connection

    root = (tmp_path / "concurrent-alias-readers").resolve()
    root.mkdir(mode=0o700)
    source = root / "source.db"
    init_database(source)
    source.chmod(0o600)
    first_at_lease = threading.Event()
    release_first = threading.Event()
    second_reap_started = threading.Event()
    real_write_alias = snapshots._write_alias_file
    real_reap = snapshots._reap_stale_aliases
    results: dict[str, object] = {}

    def pause_first_setup(directory_descriptor: int, name: str, content: bytes) -> None:
        if threading.current_thread().name == "alias-reader-a" and name == ".lease":
            first_at_lease.set()
            assert release_first.wait(timeout=3)
        real_write_alias(directory_descriptor, name, content)

    def observe_second_reap(pinned_main: object) -> None:
        if threading.current_thread().name == "alias-reader-b":
            second_reap_started.set()
        real_reap(pinned_main)

    monkeypatch.setattr(snapshots, "_write_alias_file", pause_first_setup)
    monkeypatch.setattr(snapshots, "_reap_stale_aliases", observe_second_reap)

    def read(label: str) -> None:
        try:
            with bound_database_read_connection(
                source,
                label=label,
                require_main_owner_only=True,
                require_parent_owner_only=True,
            ) as connection:
                results[label] = connection.execute("select count(*) from sqlite_master").fetchone()
        except BaseException as exc:  # captured for deterministic thread assertion
            results[label] = exc

    first = threading.Thread(target=read, name="alias-reader-a", args=("a",))
    second = threading.Thread(target=read, name="alias-reader-b", args=("b",))
    first.start()
    assert first_at_lease.wait(timeout=3)
    second.start()
    second_reap_started.wait(timeout=0.25)
    release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive() and not second.is_alive()
    assert all(isinstance(result, tuple) for result in results.values()), results
    assert results["a"] == results["b"]


def test_alias_reaper_caps_matching_entries_before_processing(
    tmp_path: Path,
) -> None:
    from ledger.storage.snapshots import bound_database_read_connection

    root = (tmp_path / "bounded-alias-scan").resolve()
    root.mkdir(mode=0o700)
    source = root / "source.db"
    init_database(source)
    source.chmod(0o600)
    for index in range(257):
        (root / f".ledger-sqlite-alias-stale-{index:03d}").mkdir(mode=0o700)

    with (
        pytest.raises(OSError, match="too many SQLite alias"),
        bound_database_read_connection(
            source,
            label="bounded alias scan",
            require_main_owner_only=True,
            require_parent_owner_only=True,
        ),
    ):
        pass


def test_next_bound_read_reaps_a_sigkill_stale_alias(
    tmp_path: Path,
) -> None:
    from ledger.storage.snapshots import bound_database_read_connection

    root = (tmp_path / "stale-alias").resolve()
    root.mkdir(mode=0o700)
    source = root / "source.db"
    init_database(source)
    source.chmod(0o600)
    script = textwrap.dedent(
        """
        import sqlite3
        import sys
        import time
        from pathlib import Path
        from ledger.storage import snapshots

        source = Path(sys.argv[1])
        real_connect = snapshots.sqlite3.connect
        def stop_after_alias_setup(database, *args, **kwargs):
            if '.ledger-sqlite-alias-' in str(database):
                print('ALIAS_READY', flush=True)
                time.sleep(60)
            return real_connect(database, *args, **kwargs)
        snapshots.sqlite3.connect = stop_after_alias_setup
        with snapshots.bound_database_read_connection(
            source,
            label='SIGKILL setup',
            require_main_owner_only=True,
            require_parent_owner_only=True,
        ):
            pass
        """
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(source)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    try:
        assert child.stdout.readline().strip() == "ALIAS_READY"
        child.kill()
        child.wait(timeout=3)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=3)
        if child.stdout is not None:
            child.stdout.close()
        if child.stderr is not None:
            child.stderr.close()

    assert list(root.glob(".ledger-sqlite-alias-*"))
    with bound_database_read_connection(
        source,
        label="stale alias recovery",
        require_main_owner_only=True,
        require_parent_owner_only=True,
    ) as connection:
        connection.execute("select count(*) from sqlite_master").fetchone()

    assert list(root.glob(".ledger-sqlite-alias-*")) == []


def test_next_bound_read_recovers_when_setup_dies_after_the_first_sensitive_link(
    tmp_path: Path,
) -> None:
    from ledger.storage.snapshots import bound_database_read_connection

    root = (tmp_path / "pre-metadata-alias").resolve()
    root.mkdir(mode=0o700)
    source = root / "source.db"
    init_database(source)
    source.chmod(0o600)
    script = textwrap.dedent(
        """
        import os
        import sys
        import time
        from pathlib import Path
        from ledger.storage import snapshots

        source = Path(sys.argv[1])
        real_link = snapshots.os.link
        def stop_after_first_sensitive_link(source_name, alias_name, **kwargs):
            result = real_link(source_name, alias_name, **kwargs)
            if str(alias_name) == 'source.db':
                print('LINK_READY', flush=True)
                time.sleep(60)
            return result
        snapshots.os.link = stop_after_first_sensitive_link
        with snapshots.bound_database_read_connection(
            source,
            label='pre-metadata SIGKILL setup',
            require_main_owner_only=True,
            require_parent_owner_only=True,
        ):
            pass
        """
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(source)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    try:
        assert child.stdout.readline().strip() == "LINK_READY"
        child.kill()
        child.wait(timeout=3)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=3)
        if child.stdout is not None:
            child.stdout.close()
        if child.stderr is not None:
            child.stderr.close()

    [stale_alias] = list(root.glob(".ledger-sqlite-alias-*"))
    assert (stale_alias / "source.db").is_file()
    with bound_database_read_connection(
        source,
        label="pre-metadata SIGKILL recovery",
        require_main_owner_only=True,
        require_parent_owner_only=True,
    ) as connection:
        assert connection.execute("select count(*) from sqlite_master").fetchone() is not None

    assert list(root.glob(".ledger-sqlite-alias-*")) == []


def test_stale_alias_reaper_rejects_empty_identity_metadata_for_sensitive_artifacts(
    tmp_path: Path,
) -> None:
    from ledger.storage.snapshots import bound_database_read_connection

    root = (tmp_path / "empty-alias-provenance").resolve()
    root.mkdir(mode=0o700)
    source = root / "source.db"
    foreign = root / "foreign.db"
    init_database(source)
    init_database(foreign)
    source.chmod(0o600)
    foreign.chmod(0o600)
    foreign_identity = (foreign.stat().st_dev, foreign.stat().st_ino)

    alias = root / ".ledger-sqlite-alias-stale"
    alias.mkdir(mode=0o700)
    (alias / ".lease").write_bytes(b"")
    (alias / ".lease").chmod(0o600)
    (alias / ".identity.json").write_text(
        json.dumps(
            {
                "version": 1,
                "pid": 0,
                "nonce": "stale",
                "identities": {},
            }
        ),
        encoding="utf-8",
    )
    (alias / ".identity.json").chmod(0o600)
    os.link(foreign, alias / "source.db")

    with (
        pytest.raises(OSError, match="provenance is unavailable"),
        bound_database_read_connection(
            source,
            label="empty alias provenance guard",
            require_main_owner_only=True,
            require_parent_owner_only=True,
        ),
    ):
        pass

    assert (alias / "source.db").is_file()
    assert (alias / "source.db").stat().st_dev == foreign_identity[0]
    assert (alias / "source.db").stat().st_ino == foreign_identity[1]


def test_stale_alias_reaper_rejects_empty_identity_metadata_without_artifacts(
    tmp_path: Path,
) -> None:
    from ledger.storage.snapshots import bound_database_read_connection

    root = (tmp_path / "empty-control-provenance").resolve()
    root.mkdir(mode=0o700)
    source = root / "source.db"
    init_database(source)
    source.chmod(0o600)

    alias = root / ".ledger-sqlite-alias-stale"
    alias.mkdir(mode=0o700)
    (alias / ".lease").write_bytes(b"")
    (alias / ".lease").chmod(0o600)
    (alias / ".identity.json").write_text(
        json.dumps(
            {
                "version": 1,
                "pid": 0,
                "nonce": "stale",
                "identities": {},
            }
        ),
        encoding="utf-8",
    )
    (alias / ".identity.json").chmod(0o600)

    with (
        pytest.raises(OSError, match="provenance is unavailable"),
        bound_database_read_connection(
            source,
            label="empty control provenance guard",
            require_main_owner_only=True,
            require_parent_owner_only=True,
        ),
    ):
        pass

    assert (alias / ".identity.json").is_file()
    assert (alias / ".lease").is_file()


def test_stale_alias_reaper_requires_provenance_for_every_sensitive_artifact(
    tmp_path: Path,
) -> None:
    from ledger.storage.snapshots import bound_database_read_connection

    root = (tmp_path / "incomplete-alias-provenance").resolve()
    root.mkdir(mode=0o700)
    source = root / "source.db"
    foreign = root / "foreign.db"
    init_database(source)
    init_database(foreign)
    source.chmod(0o600)
    foreign.chmod(0o600)
    foreign_identity = (foreign.stat().st_dev, foreign.stat().st_ino)

    alias = root / ".ledger-sqlite-alias-stale"
    alias.mkdir(mode=0o700)
    (alias / ".lease").write_bytes(b"")
    (alias / ".lease").chmod(0o600)
    (alias / ".identity.json").write_text(
        json.dumps(
            {
                "version": 1,
                "pid": 0,
                "nonce": "stale",
                "identities": {"source.db-wal": [foreign_identity[0], foreign_identity[1]]},
            }
        ),
        encoding="utf-8",
    )
    (alias / ".identity.json").chmod(0o600)
    os.link(foreign, alias / "source.db")

    with (
        pytest.raises(OSError, match="cleanup failed"),
        bound_database_read_connection(
            source,
            label="incomplete alias provenance guard",
            require_main_owner_only=True,
            require_parent_owner_only=True,
        ),
    ):
        pass

    assert (alias / "source.db").is_file()
    assert (alias / "source.db").stat().st_dev == foreign_identity[0]
    assert (alias / "source.db").stat().st_ino == foreign_identity[1]


def test_stale_alias_reaper_refuses_an_artifact_whose_inode_breaks_provenance(
    tmp_path: Path,
) -> None:
    from ledger.storage.snapshots import bound_database_read_connection

    root = (tmp_path / "stale-alias-identity").resolve()
    root.mkdir(mode=0o700)
    source = root / "source.db"
    foreign = root / "foreign.db"
    init_database(source)
    init_database(foreign)
    source.chmod(0o600)
    foreign.chmod(0o600)

    alias = root / ".ledger-sqlite-alias-stale"
    alias.mkdir(mode=0o700)
    (alias / ".lease").write_bytes(b"")
    (alias / ".lease").chmod(0o600)
    source_metadata = source.stat()
    (alias / ".identity.json").write_text(
        json.dumps(
            {
                "version": 1,
                "pid": 0,
                "nonce": "stale",
                "identities": {"source.db": [source_metadata.st_dev, source_metadata.st_ino]},
            }
        ),
        encoding="utf-8",
    )
    (alias / ".identity.json").chmod(0o600)
    os.link(foreign, alias / "source.db")
    foreign_identity = (foreign.stat().st_dev, foreign.stat().st_ino)

    with (
        pytest.raises(OSError, match="alias cleanup failed"),
        bound_database_read_connection(
            source,
            label="stale alias identity guard",
            require_main_owner_only=True,
            require_parent_owner_only=True,
        ),
    ):
        pass

    alias_metadata = (alias / "source.db").stat()
    assert (alias_metadata.st_dev, alias_metadata.st_ino) == foreign_identity


@pytest.mark.parametrize("sidecar_suffix", ["-wal", "-shm"])
def test_candidate_fingerprint_refuses_broadly_readable_sqlite_sidecars(
    tmp_path: Path,
    sidecar_suffix: str,
) -> None:
    _paths_value, run_dir, staging_db, _csv_root = _ready_finalize_run(tmp_path)
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    keeper = sqlite3.connect(staging_db)
    try:
        assert keeper.execute("pragma journal_mode=wal").fetchone() == ("wal",)
        keeper.execute("pragma wal_autocheckpoint=0")
        keeper.execute(
            "insert into processed_files values (?, ?, ?, ?)",
            ("retained-sidecar.pdf", "1" * 64, "2026-08-11 00:00:00", 1),
        )
        keeper.commit()
        sidecar = Path(f"{staging_db}{sidecar_suffix}")
        assert sidecar.is_file()
        sidecar.chmod(0o644)

        with pytest.raises(PublicationProofError, match="Publication recovery proof"):
            publication_candidate_fingerprint(run_dir, state)
    finally:
        keeper.close()


@pytest.mark.parametrize(
    ("sidecar_suffix", "kind"),
    [(suffix, kind) for suffix in ("-wal", "-shm") for kind in ("symlink", "fifo")],
)
def test_candidate_fingerprint_promptly_refuses_unsafe_sidecar_types(
    tmp_path: Path,
    sidecar_suffix: str,
    kind: str,
) -> None:
    _paths_value, run_dir, staging_db, _csv_root = _ready_finalize_run(tmp_path)
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    sidecar = Path(f"{staging_db}{sidecar_suffix}")
    if kind == "symlink":
        decoy = run_dir / f"decoy{sidecar_suffix}"
        decoy.write_bytes(b"synthetic")
        decoy.chmod(0o600)
        sidecar.symlink_to(decoy)
    else:
        os.mkfifo(sidecar, 0o600)

    with pytest.raises(PublicationProofError, match="Publication recovery proof"):
        publication_candidate_fingerprint(run_dir, state)


def test_sidecar_regular_file_swap_to_fifo_cannot_block_the_proof_reader(
    tmp_path: Path,
) -> None:
    """The second validation is useful only if the open itself cannot block forever."""
    script = textwrap.dedent(
        """
        import os
        import sys
        from pathlib import Path

        from ledger.storage.database import init_database
        from ledger.storage import snapshots

        root = Path(sys.argv[1]).resolve()
        root.mkdir(mode=0o700)
        source = root / "source.db"
        init_database(source)
        wal = Path(f"{source}-wal")
        wal.write_bytes(b"regular-before-race")
        wal.chmod(0o600)
        real_metadata = snapshots._sidecar_metadata
        swapped = False

        def race_to_fifo(parent_descriptor, name):
            global swapped
            metadata = real_metadata(parent_descriptor, name)
            if name.endswith("-wal") and metadata is not None and not swapped:
                swapped = True
                os.unlink(name, dir_fd=parent_descriptor)
                os.mkfifo(wal, 0o600)
            return metadata

        snapshots._sidecar_metadata = race_to_fifo
        try:
            with snapshots.bound_database_read_connection(
                source,
                label="bounded FIFO regression",
                require_main_owner_only=True,
                require_parent_owner_only=True,
            ):
                pass
        except OSError:
            print("REFUSED_PROMPTLY")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "fifo-race")],
        check=True,
        capture_output=True,
        text=True,
        timeout=3,
    )

    assert result.stdout.strip() == "REFUSED_PROMPTLY"


def test_candidate_fingerprint_does_not_create_shm_beside_the_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.imports.pdf import readiness

    _paths_value, run_dir, staging_db, _csv_root = _ready_finalize_run(tmp_path)
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    keeper = sqlite3.connect(staging_db)
    try:
        assert keeper.execute("pragma journal_mode=wal").fetchone() == ("wal",)
        keeper.execute("pragma wal_autocheckpoint=0")
        keeper.execute(
            "insert into processed_files values (?, ?, ?, ?)",
            ("retained-without-shm.pdf", "2" * 64, "2026-08-11 00:00:00", 1),
        )
        keeper.commit()
        wal = Path(f"{staging_db}-wal")
        shm = Path(f"{staging_db}-shm")
        assert wal.is_file()
        shm.unlink()
        assert not shm.exists()
        created_beside_source = False
        real_fingerprint = readiness.import_owned_fingerprint

        def observe_source_namespace(database: object) -> dict:
            nonlocal created_beside_source
            created_beside_source = shm.exists()
            return real_fingerprint(database)

        monkeypatch.setattr(readiness, "import_owned_fingerprint", observe_source_namespace)

        publication_candidate_fingerprint(run_dir, state)

        assert not created_beside_source
        assert not shm.exists()
    finally:
        keeper.close()


def test_bound_reader_links_an_existing_shm_into_the_alias_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.storage import snapshots

    _paths_value, run_dir, staging_db, _csv_root = _ready_finalize_run(tmp_path)
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    keeper = sqlite3.connect(staging_db)
    linked_names: list[tuple[str, str]] = []
    real_link = snapshots.os.link

    def record_link(source: object, target: object, *args: object, **kwargs: object) -> None:
        linked_names.append((str(source), str(target)))
        real_link(source, target, *args, **kwargs)

    try:
        assert keeper.execute("pragma journal_mode=wal").fetchone() == ("wal",)
        keeper.execute(
            "insert into processed_files values (?, ?, ?, ?)",
            (
                "existing-shm.pdf",
                "5" * 64,
                "2026-08-11 00:00:00",
                1,
            ),
        )
        keeper.commit()
        assert Path(f"{staging_db}-shm").is_file()
        monkeypatch.setattr(snapshots.os, "link", record_link)

        publication_candidate_fingerprint(run_dir, state)
    finally:
        keeper.close()

    assert ("staging.db-shm", "source.db-shm") in linked_names


def test_bound_reader_materializes_every_alias_before_connect_and_makes_it_immutable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Main/WAL/SHM identity and 0500 namespace are fixed before SQLite opens."""
    from ledger.storage import snapshots

    root = (tmp_path / "materialized-bound-reader").resolve()
    root.mkdir(mode=0o700)
    source = root / "source.db"
    init_database(source)
    source.chmod(0o600)
    keeper = sqlite3.connect(source)
    observations: list[dict[str, object]] = []
    real_connect = snapshots.sqlite3.connect
    try:
        assert keeper.execute("pragma journal_mode=wal").fetchone() == ("wal",)
        keeper.execute("pragma wal_autocheckpoint=0")
        keeper.execute("create table materialized_probe (value text)")
        keeper.execute("insert into materialized_probe values ('synthetic')")
        keeper.commit()
        wal = Path(f"{source}-wal")
        shm = Path(f"{source}-shm")
        assert wal.is_file() and shm.is_file()
        wal.chmod(0o600)
        shm.chmod(0o600)

        def observe_connect(database: object, *args: object, **kwargs: object):
            alias_main = Path(str(database).removeprefix("file:").split("?", 1)[0])
            alias_wal = Path(f"{alias_main}-wal")
            alias_shm = Path(f"{alias_main}-shm")
            observations.append(
                {
                    "mode": stat.S_IMODE(alias_main.parent.stat().st_mode),
                    "main": (alias_main.stat().st_dev, alias_main.stat().st_ino),
                    "wal": (alias_wal.stat().st_dev, alias_wal.stat().st_ino),
                    "shm": (alias_shm.stat().st_dev, alias_shm.stat().st_ino),
                }
            )
            return real_connect(database, *args, **kwargs)

        monkeypatch.setattr(snapshots.sqlite3, "connect", observe_connect)

        with snapshots.bound_database_read_connection(
            source,
            label="materialized reader",
            require_main_owner_only=True,
            require_parent_owner_only=True,
        ) as connection:
            assert connection.execute("select value from materialized_probe").fetchone() == (
                "synthetic",
            )

        source_identity = (source.stat().st_dev, source.stat().st_ino)
        wal_identity = (wal.stat().st_dev, wal.stat().st_ino)
        shm_identity = (shm.stat().st_dev, shm.stat().st_ino)
        assert observations == [
            {
                "mode": 0o500,
                "main": source_identity,
                "wal": wal_identity,
                "shm": shm_identity,
            }
        ]
    finally:
        keeper.close()


def test_bound_reader_precreates_and_pins_alias_shm_before_connect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing source SHM has a pinned 0600 alias identity before connect."""
    from ledger.storage import snapshots

    root = (tmp_path / "precreated-shm-reader").resolve()
    root.mkdir(mode=0o700)
    source = root / "source.db"
    init_database(source)
    source.chmod(0o600)
    keeper = sqlite3.connect(source)
    observed: list[tuple[int, tuple[int, int], int, tuple[int, int]]] = []
    real_connect = snapshots.sqlite3.connect
    try:
        assert keeper.execute("pragma journal_mode=wal").fetchone() == ("wal",)
        keeper.execute("pragma wal_autocheckpoint=0")
        keeper.execute("create table precreated_shm_probe (value text)")
        keeper.execute("insert into precreated_shm_probe values ('synthetic')")
        keeper.commit()
        wal = Path(f"{source}-wal")
        shm = Path(f"{source}-shm")
        assert wal.is_file()
        wal.chmod(0o600)
        shm.unlink()
        assert not shm.exists()

        def observe_connect(database: object, *args: object, **kwargs: object):
            alias_main = Path(str(database).removeprefix("file:").split("?", 1)[0])
            alias_shm = Path(f"{alias_main}-shm")
            metadata = alias_shm.stat(follow_symlinks=False)
            matching_fds = [
                descriptor
                for descriptor, identity in snapshots._open_fd_snapshot().items()
                if identity[:2] == (metadata.st_dev, metadata.st_ino)
            ]
            alias_metadata = json.loads(
                (alias_main.parent / snapshots._ALIAS_METADATA).read_text(encoding="utf-8")
            )
            observed.append(
                (
                    stat.S_IMODE(alias_main.parent.stat().st_mode),
                    (metadata.st_dev, metadata.st_ino),
                    len(matching_fds),
                    tuple(alias_metadata["identities"]["source.db-shm"]),
                )
            )
            return real_connect(database, *args, **kwargs)

        monkeypatch.setattr(snapshots.sqlite3, "connect", observe_connect)

        with snapshots.bound_database_read_connection(
            source,
            label="precreated SHM reader",
            require_main_owner_only=True,
            require_parent_owner_only=True,
        ) as connection:
            assert connection.execute("select value from precreated_shm_probe").fetchone() == (
                "synthetic",
            )

        assert len(observed) == 1
        mode, identity, pinned_fd_count, recorded_identity = observed[0]
        assert mode == 0o500
        assert pinned_fd_count >= 1
        assert recorded_identity == identity
        assert not shm.exists()
    finally:
        keeper.close()


def test_finalize_publishes_the_candidate_state_retained_only_in_staging_wal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, run_dir, staging_db, _csv_root = _ready_finalize_run(tmp_path)
    _patch_finalize_dependencies(monkeypatch)
    reader = sqlite3.connect(staging_db)
    writer = sqlite3.connect(staging_db)
    try:
        assert writer.execute("pragma journal_mode=wal").fetchone() == ("wal",)
        writer.execute("pragma wal_autocheckpoint=0")
        reader.execute("begin")
        reader.execute("select count(*) from processed_files").fetchone()
        writer.execute(
            "insert into processed_files values (?, ?, ?, ?)",
            ("decision-retained-in-wal.pdf", "3" * 64, "2026-08-11 00:00:00", 1),
        )
        writer.commit()
        candidate = publication_candidate_fingerprint(
            run_dir,
            json.loads((run_dir / "run.json").read_text(encoding="utf-8")),
        )
        assert Path(f"{staging_db}-wal").stat().st_size > 0

        workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)
    finally:
        reader.close()
        writer.close()

    with contextlib.closing(sqlite3.connect(paths.db_path)) as published, published:
        assert published.execute(
            "select count(*) from processed_files where file_path = ?",
            ("decision-retained-in-wal.pdf",),
        ).fetchone() == (1,)
    assert import_owned_fingerprint(paths.db_path) == candidate
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state["publication_completed"] is True


def test_finalize_rechecks_candidate_inside_the_publication_primitive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.storage.snapshots import snapshot_database

    paths, run_dir, staging_db, _csv_root = _ready_finalize_run(tmp_path)
    _patch_finalize_dependencies(monkeypatch)
    replacement = run_dir / "replacement.db"
    snapshot_database(paths.db_path, replacement)
    displaced = run_dir / "displaced.db"
    real_promote = workflow._promote_database_locked

    def swap_after_outer_provenance(*args: object, **kwargs: object):
        os.replace(staging_db, displaced)
        os.replace(replacement, staging_db)
        return real_promote(*args, **kwargs)

    monkeypatch.setattr(workflow, "_promote_database_locked", swap_after_outer_provenance)

    with pytest.raises(PublicationProofError, match="Publication recovery proof"):
        workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state.get("publication_completed") is not True
    assert import_owned_fingerprint(paths.db_path) == state["target_db_import_owned_before"]


def test_finalize_verifies_the_published_target_before_writing_completion_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.imports.pdf import files as pdf_files

    paths, run_dir, _staging_db, _csv_root = _ready_finalize_run(tmp_path)
    _patch_finalize_dependencies(monkeypatch)
    real_publish = pdf_files.publish_database

    def corrupt_after_publication(*args: object, **kwargs: object) -> Path:
        target = real_publish(*args, **kwargs)
        with contextlib.closing(sqlite3.connect(paths.db_path)) as connection, connection:
            connection.execute(
                "insert into processed_files values (?, ?, ?, ?)",
                ("post-publish-corruption.pdf", "4" * 64, "2026-08-11 00:00:00", 1),
            )
        return target

    monkeypatch.setattr(pdf_files, "publish_database", corrupt_after_publication)

    with pytest.raises(PublicationProofError, match="Publication recovery proof"):
        workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state.get("publication_completed") is not True


def test_negative_candidate_proof_retry_clears_stale_provenance_before_new_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, run_dir, _staging_db, _csv_root = _ready_finalize_run(tmp_path)
    _patch_finalize_dependencies(monkeypatch)
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    state.update(
        {
            "status": "failed",
            "failure_reason": "finalize_failed",
            "finalize_protocol_version": 1,
            "publication_phase": "candidate_ready",
            "publication_token": "e" * 32,
            "publication_candidate_import_owned": publication_candidate_fingerprint(run_dir, state),
            "finalize_tail": {"stale": True},
        }
    )
    workflow.write_run_state(run_dir, state)
    calls = 0

    def fail_first_new_preparation(_db: Path, _config: object) -> dict:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("synthetic second preparation failure")
        return {}

    monkeypatch.setattr(workflow, "run_post_processing", fail_first_new_preparation)

    with pytest.raises(RuntimeError, match="second preparation failure"):
        workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    failed = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert failed["status"] == "failed"
    assert failed["publication_phase"] == "preparing"
    assert "publication_candidate_import_owned" not in failed
    assert "finalize_tail" not in failed

    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)
    completed = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert completed["status"] == "finalized"
    assert completed["publication_phase"] == "published"


def test_markerless_recovery_rejects_regular_staging_replacement_before_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Current equality is not proof when staging no longer matches its persisted candidate."""
    from ledger.storage.snapshots import snapshot_database

    paths, run_dir, staging_db, _csv_root = _ready_finalize_run(tmp_path)
    _patch_finalize_dependencies(monkeypatch)
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    state.update(
        {
            "status": "finalizing",
            "publication_candidate_import_owned": import_owned_fingerprint(staging_db),
        }
    )
    workflow.write_run_state(run_dir, state)
    replacement = run_dir / "replacement.db"
    snapshot_database(paths.db_path, replacement)
    os.replace(replacement, staging_db)

    with pytest.raises(SystemExit, match="Publication recovery proof is unavailable"):
        workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    refused = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert refused["status"] == "finalizing"


def test_current_protocol_first_import_absence_is_safe_negative_publication_proof(
    tmp_path: Path,
) -> None:
    from ledger.imports.pdf.readiness import recover_publication_completed

    root = (tmp_path / "first-import").resolve()
    runs_dir = root / "runs"
    run_dir = runs_dir / "run-1"
    run_dir.mkdir(parents=True)
    root.chmod(0o700)
    runs_dir.chmod(0o700)
    run_dir.chmod(0o700)
    staging_db = run_dir / "staging.db"
    init_database(staging_db)
    staging_db.chmod(0o600)
    target_db = root / "ledger.db"
    state = {
        "version": 1,
        "status": "finalizing",
        "finalize_protocol_version": 1,
        "publication_phase": "candidate_ready",
        "publication_token": "a" * 32,
        "target_db_path": str(target_db),
        "staging_db_path": str(staging_db),
        "target_db_fingerprint_before": database_fingerprint(target_db),
        "target_db_import_owned_before": import_owned_fingerprint(target_db),
        "finalize_tail": {"idempotency_key": "synthetic"},
    }
    state["publication_candidate_import_owned"] = publication_candidate_fingerprint(run_dir, state)

    assert recover_publication_completed(run_dir, state) is False
    assert not target_db.exists()
    assert all(not Path(f"{target_db}{suffix}").exists() for suffix in ("-journal", "-wal", "-shm"))


def test_recovery_bound_reader_uses_outer_pinned_files_not_reopened_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import contextmanager

    from ledger.imports.pdf import readiness
    from ledger.storage.snapshots import snapshot_database
    from ledger.utils.private_files import PinnedPrivateFile

    paths, run_dir, staging_db, _csv_root = _ready_finalize_run(tmp_path)
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    state.update(
        {
            "status": "finalizing",
            "publication_candidate_import_owned": publication_candidate_fingerprint(run_dir, state),
        }
    )
    replacement = run_dir / "target-replacement.db"
    snapshot_database(staging_db, replacement)
    displaced = paths.db_path.with_name("target-original.db")
    real_bound = readiness.bound_database_read_connection
    attacked = False
    received_live_pin = False

    @contextmanager
    def replace_target_only_for_inner_read(source: object, **kwargs: object):
        nonlocal attacked, received_live_pin
        if not attacked and kwargs.get("label") == "Publication target":
            received_live_pin = isinstance(source, PinnedPrivateFile)
            attacked = True
            paths.db_path.rename(displaced)
            replacement.rename(paths.db_path)
            try:
                with real_bound(source, **kwargs) as connection:
                    yield connection
            finally:
                paths.db_path.rename(replacement)
                displaced.rename(paths.db_path)
            return
        with real_bound(source, **kwargs) as connection:
            yield connection

    monkeypatch.setattr(
        readiness, "bound_database_read_connection", replace_target_only_for_inner_read
    )

    with pytest.raises(PublicationProofError, match="Publication recovery proof"):
        readiness.recover_publication_completed(run_dir, state)
    assert attacked
    assert received_live_pin, "recovery must hand its exact outer pin to the bound reader"
    assert import_owned_fingerprint(paths.db_path) != state["publication_candidate_import_owned"]


def test_state_write_rejects_v1_candidate_ready_with_completion_marker(
    tmp_path: Path,
) -> None:
    _paths_value, run_dir, _staging_db, _csv_root = _ready_finalize_run(tmp_path)
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    state.update(
        {
            "status": "finalizing",
            "finalize_protocol_version": 1,
            "publication_phase": "candidate_ready",
            "publication_token": "b" * 32,
            "publication_candidate_import_owned": {"tables": {}},
            "finalize_tail": {"idempotency_key": "synthetic"},
            "publication_completed": True,
        }
    )

    with pytest.raises(ValueError, match="publication state"):
        workflow.write_run_state(run_dir, state)

    persisted = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert persisted["status"] == "ready_to_finalize"


def test_finalize_recovery_rejects_v1_phase_marker_contradiction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, run_dir, _staging_db, _csv_root = _ready_finalize_run(tmp_path)
    _patch_finalize_dependencies(monkeypatch)
    state_path = run_dir / "run.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update(
        {
            "status": "finalizing",
            "finalize_protocol_version": 1,
            "publication_phase": "candidate_ready",
            "publication_token": "c" * 32,
            "publication_candidate_import_owned": {"tables": {}},
            "finalize_tail": {"idempotency_key": "synthetic"},
            "publication_completed": True,
        }
    )
    state_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(SystemExit, match="Publication recovery state is invalid"):
        workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)


def test_finalize_persists_one_bounded_publication_token_that_config_replace_preserves(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, run_dir, _staging_db, _csv_root = _ready_finalize_run(tmp_path)
    _patch_finalize_dependencies(monkeypatch)

    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    with contextlib.closing(sqlite3.connect(paths.db_path)) as connection, connection:
        rows = connection.execute(
            "select value from config_metadata where key = ?",
            ("__ledger_pdf_publication_token",),
        ).fetchall()
    assert len(rows) == 1
    token = str(rows[0][0])
    assert token
    assert run_dir.name not in token
    assert str(run_dir) not in token

    repo = SQLiteConfigRepository(paths.db_path)
    repo.replace_from_configs(repo.load_merchant_rules(), repo.load_categories())
    with contextlib.closing(sqlite3.connect(paths.db_path)) as connection, connection:
        preserved = connection.execute(
            "select value from config_metadata where key = ?",
            ("__ledger_pdf_publication_token",),
        ).fetchone()
    assert preserved == (token,)
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state["finalize_protocol_version"] == 2


def test_tokenless_v1_published_marker_tails_without_requiring_volatile_equality(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, run_dir, _staging_db, _csv_root = _ready_finalize_run(tmp_path)
    _patch_finalize_dependencies(monkeypatch)
    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)
    state_path = run_dir / "run.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update(
        {
            "status": "finalize_failed",
            "failure_reason": "finalize_failed",
            "finalize_protocol_version": 1,
            "publication_phase": "published",
            "publication_completed": True,
        }
    )
    state.pop("publication_token", None)
    state.pop("publication_target_identity", None)
    state_path.write_text(json.dumps(state), encoding="utf-8")
    with contextlib.closing(sqlite3.connect(paths.db_path)) as connection, connection:
        connection.execute(
            "insert into processed_files values (?, ?, ?, ?)",
            ("post-legacy-marker.pdf", "9" * 64, "2026-08-11 00:00:00", 1),
        )

    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    completed = json.loads(state_path.read_text(encoding="utf-8"))
    assert completed["status"] == "finalized"
    with contextlib.closing(sqlite3.connect(paths.db_path)) as connection, connection:
        assert connection.execute(
            "select count(*) from processed_files where file_path = ?",
            ("post-legacy-marker.pdf",),
        ).fetchone() == (1,)


@pytest.mark.parametrize("phase", ["preparing", "candidate_ready"])
def test_tokenized_v1_retry_migrates_new_publication_work_to_v2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    paths, run_dir, staging_db, _csv_root = _ready_finalize_run(tmp_path)
    _patch_finalize_dependencies(monkeypatch)
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    state.update(
        {
            "status": "finalize_failed",
            "failure_reason": "finalize_failed",
            "finalize_protocol_version": 1,
            "publication_phase": phase,
            "publication_token": "e" * 32,
        }
    )
    if phase == "candidate_ready":
        state["publication_candidate_import_owned"] = publication_candidate_fingerprint(
            run_dir, state
        )
        state["finalize_tail"] = {"idempotency_key": "synthetic"}
        assert import_owned_fingerprint(paths.db_path) != import_owned_fingerprint(staging_db)
    workflow.write_run_state(run_dir, state)

    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    completed = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert completed["status"] == "finalized"
    assert completed["finalize_protocol_version"] == 2


def test_tokenized_v1_published_state_uses_inode_and_token_proof_before_tail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, run_dir, _staging_db, _csv_root = _ready_finalize_run(tmp_path)
    _patch_finalize_dependencies(monkeypatch)
    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)
    state_path = run_dir / "run.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update(
        {
            "status": "finalize_failed",
            "failure_reason": "finalize_failed",
            "finalize_protocol_version": 1,
        }
    )
    state_path.write_text(json.dumps(state), encoding="utf-8")

    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    assert json.loads(state_path.read_text(encoding="utf-8"))["status"] == "finalized"


def test_tokenized_v1_finalized_state_remains_compatible(tmp_path: Path) -> None:
    _paths_value, run_dir, _staging_db, _csv_root = _ready_finalize_run(tmp_path)
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    state.update(
        {
            "status": "finalized",
            "finalize_protocol_version": 1,
            "publication_phase": "published",
            "publication_token": "f" * 32,
            "publication_candidate_import_owned": {"tables": {}},
            "finalize_tail": {"idempotency_key": "synthetic"},
            "publication_completed": True,
            "publication_target_identity": {"dev": 1, "ino": 2},
        }
    )

    validate_publication_protocol_state(state)


def test_published_marker_requires_the_persisted_target_inode_even_with_matching_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.imports.pdf.readiness import verify_published_marker
    from ledger.storage.snapshots import snapshot_database

    paths, run_dir, _staging_db, _csv_root = _ready_finalize_run(tmp_path)
    _patch_finalize_dependencies(monkeypatch)
    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

    same_content_new_inode = tmp_path / "same-published-content.db"
    snapshot_database(paths.db_path, same_content_new_inode)
    os.replace(same_content_new_inode, paths.db_path)

    with pytest.raises(PublicationProofError, match="Publication recovery proof"):
        verify_published_marker(state)


def test_published_marker_requires_the_persisted_token_on_the_same_target_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.imports.pdf.readiness import verify_published_marker

    paths, run_dir, _staging_db, _csv_root = _ready_finalize_run(tmp_path)
    _patch_finalize_dependencies(monkeypatch)
    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

    with contextlib.closing(sqlite3.connect(paths.db_path)) as connection, connection:
        connection.execute(
            "update config_metadata set value = ? where key = ?",
            ("d" * 32, "__ledger_pdf_publication_token"),
        )

    with pytest.raises(PublicationProofError, match="Publication recovery proof"):
        verify_published_marker(state)


def test_target_replacement_during_completion_marker_cannot_become_trusted_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.storage.snapshots import snapshot_database

    paths, run_dir, _staging_db, _csv_root = _ready_finalize_run(tmp_path)
    _patch_finalize_dependencies(monkeypatch)
    old_target = tmp_path / "old-target.db"
    snapshot_database(paths.db_path, old_target)
    real_write = workflow.write_run_state
    attacked = False

    def replace_target_during_marker(run: Path, state: dict[str, object]) -> None:
        nonlocal attacked
        if state.get("publication_completed") is True and not attacked:
            os.replace(old_target, paths.db_path)
            attacked = True
        real_write(run, state)

    monkeypatch.setattr(workflow, "write_run_state", replace_target_during_marker)

    with pytest.raises(PublicationProofError, match="Publication recovery proof"):
        workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    assert attacked
    persisted = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert persisted["publication_completed"] is True
    with pytest.raises(SystemExit, match="Publication recovery proof is unavailable"):
        workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)


def test_pending_review_reset_clears_current_protocol_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, run_dir, _staging_db, _csv_root = _ready_finalize_run(tmp_path)
    _patch_finalize_dependencies(monkeypatch)
    monkeypatch.setattr(workflow, "pending_review_count", lambda _db: 1)

    with pytest.raises(SystemExit, match="Manual review is not complete"):
        workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    persisted = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert persisted["status"] == "needs_review"
    for key in (
        "finalize_protocol_version",
        "publication_phase",
        "publication_token",
        "publication_candidate_import_owned",
        "publication_completed",
        "publication_target_identity",
    ):
        assert key not in persisted


def test_markerless_recovery_without_candidate_provenance_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, run_dir, staging_db, _csv_root = _ready_finalize_run(tmp_path)
    _patch_finalize_dependencies(monkeypatch)
    workflow.promote_database(staging_db, paths.db_path, paths.backup_dir)
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    state["status"] = "finalizing"
    assert "publication_candidate_import_owned" not in state
    workflow.write_run_state(run_dir, state)

    with pytest.raises(SystemExit, match="Publication recovery proof is unavailable"):
        workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)


def test_markerless_recovery_sanitizes_target_lock_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import traceback
    from contextlib import contextmanager

    paths, run_dir, staging_db, _csv_root = _ready_finalize_run(tmp_path)
    _patch_finalize_dependencies(monkeypatch)
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    state.update(
        {
            "status": "finalizing",
            "publication_candidate_import_owned": import_owned_fingerprint(staging_db),
        }
    )
    workflow.write_run_state(run_dir, state)

    @contextmanager
    def fail_private_lock(_target: Path):
        raise OSError(f"synthetic private lock path: {tmp_path}")
        yield  # pragma: no cover

    monkeypatch.setattr(workflow, "database_target_lock", fail_private_lock)

    with pytest.raises(SystemExit) as excinfo:
        workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    formatted = "".join(traceback.format_exception(excinfo.value))
    assert str(excinfo.value) == "Publication recovery proof is unavailable."
    assert str(tmp_path) not in formatted


@pytest.mark.parametrize("legacy", [False, True])
def test_partial_finalized_run_repairs_report_and_audit_without_repromoting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy: bool,
) -> None:
    paths, run_dir, staging_db, _csv_root = _ready_finalize_run(tmp_path)
    _patch_finalize_dependencies(monkeypatch)
    backup = workflow.promote_database(staging_db, paths.db_path, paths.backup_dir)
    final_report = run_dir / "final-report.json"
    final_report.write_text("{}", encoding="utf-8")
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    tail = {
        "idempotency_key": "synthetic-finalize-key",
        "final_report": str(final_report),
        "target_db_backup": str(backup or ""),
        "counts": {"transactions": 7, "transfers": 2},
        "post_processing": {"accounts": {"saved": 1}},
        "merchant_summary": {
            "resolved_merchants": ["SYNTHETIC"],
            "manual_review_merchants": [],
        },
        "review_rows": [{"review_key": "synthetic"}],
        "processed_pdfs": [],
    }
    state.update(
        {
            "status": "finalized",
            "finalized_at": "2026-07-10T00:00:00Z",
            "final_report": str(final_report),
            "final_counts": tail["counts"],
            "post_processing": tail["post_processing"],
            "target_db_backup": tail["target_db_backup"],
            "review_rows": tail["review_rows"],
            "pdfs": [],
        }
    )
    if not legacy:
        state["finalize_tail"] = tail
    workflow.write_run_state(run_dir, state)
    promote_calls = 0

    def unexpected_promote(*_args, **_kwargs):
        nonlocal promote_calls
        promote_calls += 1
        raise AssertionError("partial finalized recovery must not promote")

    monkeypatch.setattr(workflow, "promote_database", unexpected_promote)

    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    repaired = json.loads(final_report.read_text(encoding="utf-8"))
    assert promote_calls == 0
    assert repaired["counts"] == {"transactions": 7, "transfers": 2}
    if legacy:
        # The old state format finalized before persisting either the report or
        # merchant summary, so a crash in that exact window cannot reconstruct
        # unavailable presentation metadata. Recovery must stay honest rather than
        # inventing it; the financial counts and audit remain repairable.
        assert repaired["merchant_summary"] == {}
    else:
        assert repaired["merchant_summary"]["resolved_merchants"] == ["SYNTHETIC"]
    assert len(_finalize_audits(paths.db_path)) == 1


def test_finalize_persists_one_recoverable_financial_brief(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, run_dir, _staging_db, _csv_root = _ready_finalize_run(tmp_path)
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    state["created_at"] = "2026-09-03T14:15:16Z"
    workflow.write_run_state(run_dir, state)
    _patch_finalize_dependencies(monkeypatch)

    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)
    first_state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    first_report = json.loads((run_dir / "final-report.json").read_text(encoding="utf-8"))
    first_manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))

    assert first_state["financial_brief"] == first_report["financial_brief"]
    assert first_state["financial_brief"]["since"] == "2026-09-03T14:15:16"
    assert first_manifest["financial_brief"] == first_state["financial_brief"]

    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)
    recovered_state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    recovered_report = json.loads((run_dir / "final-report.json").read_text(encoding="utf-8"))

    assert recovered_state["financial_brief"] == first_state["financial_brief"]
    assert recovered_report["financial_brief"] == first_report["financial_brief"]
    assert len(_finalize_audits(paths.db_path)) == 1


def test_legacy_finalize_failed_status_without_provenance_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, run_dir, staging_db, _csv_root = _ready_finalize_run(tmp_path)
    _patch_finalize_dependencies(monkeypatch)
    workflow.promote_database(staging_db, paths.db_path, paths.backup_dir)
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    state.update(
        {
            "status": "finalize_failed",
            "final_counts": {"transactions": 7, "transfers": 2},
            "post_processing": {"accounts": {"saved": 1}},
            "review_rows": [],
            "pdfs": [],
            "final_report": str(run_dir / "final-report.json"),
        }
    )
    workflow.write_run_state(run_dir, state)
    with pytest.raises(SystemExit, match="Publication recovery proof is unavailable"):
        workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    assert json.loads((run_dir / "run.json").read_text(encoding="utf-8"))["status"] == (
        "finalize_failed"
    )
    assert len(_finalize_audits(paths.db_path)) == 0


@pytest.mark.parametrize("archive_fails", [False, True])
def test_archive_outcome_is_persisted_before_finalized_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    archive_fails: bool,
) -> None:
    paths, run_dir, _staging_db, _csv_root = _ready_finalize_run(tmp_path, with_pdf=True)
    _patch_finalize_dependencies(monkeypatch)
    if archive_fails:
        monkeypatch.setattr(
            workflow.statement_pdf_to_csv,
            "move_pdf",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                OSError("secret archive implementation detail")
            ),
        )
    real_write_run_state = workflow.write_run_state
    persisted_states: list[dict] = []

    def capture_states(path: Path, state: dict) -> None:
        persisted_states.append(json.loads(json.dumps(state)))
        real_write_run_state(path, state)

    monkeypatch.setattr(workflow, "write_run_state", capture_states)

    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    final_index = next(
        index for index, state in enumerate(persisted_states) if state.get("status") == "finalized"
    )
    marker_index = next(
        index
        for index, state in enumerate(persisted_states)
        if state.get("publication_completed") is True
    )
    assert marker_index < final_index
    prior_outcomes = [
        state["archive_outcome"]
        for state in persisted_states[:final_index]
        if "archive_outcome" in state
    ]
    assert prior_outcomes
    outcome = prior_outcomes[-1]
    assert outcome["status"] == ("warning" if archive_fails else "success")
    assert outcome["warnings"] == (["archive_move_failed"] if archive_fails else [])
    serialized = json.dumps(persisted_states)
    assert "secret archive implementation detail" not in serialized
    report = json.loads((run_dir / "final-report.json").read_text(encoding="utf-8"))
    final_state = persisted_states[final_index]
    assert report["processed_pdfs"] == final_state["pdfs"]


def test_post_publish_live_teach_survives_tail_recovery_without_repromote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, run_dir, staging_db, _csv_root = _ready_finalize_run(tmp_path)
    _patch_finalize_dependencies(monkeypatch)
    SQLiteConfigRepository(staging_db).replace_from_configs(
        MerchantRulesConfig(version="test", rules=[]),
        CategoriesConfig(
            version="test",
            default_category="Other",
            categories=[
                CategoryRule(name="Other"),
                CategoryRule(name="Dining"),
            ],
        ),
    )
    real_tail = workflow._complete_finalize_tail
    tail_calls = 0

    def fail_first_tail(*args: object, **kwargs: object) -> None:
        nonlocal tail_calls
        tail_calls += 1
        if tail_calls == 1:
            raise RuntimeError("synthetic tail failure")
        real_tail(*args, **kwargs)

    monkeypatch.setattr(workflow, "_complete_finalize_tail", fail_first_tail)
    with pytest.raises(RuntimeError, match="synthetic tail failure"):
        workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    wedged = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert wedged["status"] == "finalizing"
    assert wedged["publication_completed"] is True
    taught = CategorizationService(paths.db_path).remember_category(
        merchant_norm="SYNTHETIC MERCHANT",
        pattern="SYNTHETIC",
        category="Dining",
        refresh_analytics=False,
    )
    assert taught.ok

    monkeypatch.setattr(
        workflow,
        "_promote_database_locked",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("durably published recovery must not re-promote")
        ),
    )
    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    rules = SQLiteConfigRepository(paths.db_path).load_merchant_rules().rules
    assert any(rule.merchant_norm == "SYNTHETIC MERCHANT" for rule in rules)


@pytest.mark.parametrize("link_target", ["target", "copy"])
def test_markerless_recovery_rejects_staging_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    link_target: str,
) -> None:
    paths, run_dir, staging_db, _csv_root = _ready_finalize_run(tmp_path)
    _patch_finalize_dependencies(monkeypatch)
    workflow.promote_database(staging_db, paths.db_path, paths.backup_dir)
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    state["status"] = "finalizing"
    state["publication_candidate_import_owned"] = import_owned_fingerprint(staging_db)
    workflow.write_run_state(run_dir, state)
    staging_db.unlink()
    if link_target == "copy":
        from ledger.storage.snapshots import snapshot_database

        copied = run_dir / "copied-staging.db"
        snapshot_database(paths.db_path, copied)
        staging_db.symlink_to(copied)
    else:
        staging_db.symlink_to(paths.db_path)

    with pytest.raises(SystemExit, match="Publication recovery proof is unavailable"):
        workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)


def test_markerless_recovery_rejects_staging_swap_during_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.imports.pdf import readiness
    from ledger.storage.snapshots import snapshot_database

    paths, run_dir, staging_db, _csv_root = _ready_finalize_run(tmp_path)
    _patch_finalize_dependencies(monkeypatch)
    workflow.promote_database(staging_db, paths.db_path, paths.backup_dir)
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    state["status"] = "finalizing"
    state["publication_candidate_import_owned"] = import_owned_fingerprint(staging_db)
    workflow.write_run_state(run_dir, state)
    real_fingerprint = readiness.import_owned_fingerprint
    calls = 0

    def swap_after_first_fingerprint(database: object) -> dict:
        nonlocal calls
        result = real_fingerprint(database)
        calls += 1
        if calls == 1:
            replacement = run_dir / "replacement.db"
            snapshot_database(paths.db_path, replacement)
            os.replace(replacement, staging_db)
        return result

    monkeypatch.setattr(readiness, "import_owned_fingerprint", swap_after_first_fingerprint)

    with pytest.raises(SystemExit, match="Publication recovery proof is unavailable"):
        workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    assert calls == 1


def test_finalize_serializes_concurrent_callers_to_one_promote_and_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, run_dir, _staging_db, _csv_root = _ready_finalize_run(tmp_path)
    _patch_finalize_dependencies(monkeypatch)
    first_entered = threading.Event()
    release_first = threading.Event()
    first_errors: list[BaseException] = []

    def block_first_caller(*_args: object) -> object:
        if not first_entered.is_set():
            first_entered.set()
            if not release_first.wait(timeout=10):
                raise AssertionError("timed out waiting to release first finalize caller")
        return object()

    monkeypatch.setattr(workflow, "load_config", block_first_caller)
    real_promote = workflow._promote_database_locked
    promote_calls = 0
    promote_calls_lock = threading.Lock()

    def count_promote(*args: object, **kwargs: object):
        nonlocal promote_calls
        with promote_calls_lock:
            promote_calls += 1
        return real_promote(*args, **kwargs)

    monkeypatch.setattr(workflow, "_promote_database_locked", count_promote)

    def run_first_finalize() -> None:
        try:
            workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)
        except BaseException as exc:  # pragma: no cover - asserted below
            first_errors.append(exc)

    first = threading.Thread(target=run_first_finalize)
    first.start()
    assert first_entered.wait(timeout=10), "first finalize caller did not reach the held gate"

    try:
        with pytest.raises(SystemExit, match="already in progress"):
            workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)
    finally:
        release_first.set()
        first.join(timeout=10)

    assert not first.is_alive(), "first finalize caller did not finish"
    assert first_errors == []
    assert promote_calls == 1
    assert len(_finalize_audits(paths.db_path)) == 1
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state["status"] == "finalized"
    assert stat.S_IMODE(run_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((run_dir / ".finalize.lock").stat().st_mode) == 0o600


def test_latest_run_clear_serializes_with_newer_pointer_publication(tmp_path: Path) -> None:
    from ledger.imports.pdf import files as pdf_files

    runs_dir = tmp_path / "runs"
    run_a = runs_dir / "run-a"
    run_b = runs_dir / "run-b"
    run_a.mkdir(parents=True)
    run_b.mkdir()
    latest = pdf_files.latest_run_file(runs_dir)
    latest.write_text(str(run_a), encoding="utf-8")
    started = threading.Event()
    finished = threading.Event()

    def clear_old_pointer() -> None:
        started.set()
        pdf_files.clear_latest_run_file(runs_dir, run_a)
        finished.set()

    with pdf_files._runs_lock(runs_dir):
        clear_thread = threading.Thread(target=clear_old_pointer)
        clear_thread.start()
        assert started.wait(timeout=5)
        assert not finished.wait(timeout=0.2), "pointer clear did not join the runs lock"
        latest.write_text(str(run_b), encoding="utf-8")

    clear_thread.join(timeout=5)
    assert not clear_thread.is_alive()
    assert latest.read_text(encoding="utf-8") == str(run_b)


def test_finalize_rejects_run_outside_configured_runs_before_state_read(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    outside_run = tmp_path / "outside-run"
    outside_run.mkdir()
    (outside_run / "run.json").write_text("not-json", encoding="utf-8")

    with pytest.raises(SystemExit, match="outside the configured import-runs directory"):
        workflow.finalize(SimpleNamespace(run_dir=outside_run), paths)

    assert not (outside_run / ".finalize.lock").exists()


def test_finalize_rejects_symlinked_run_lock(tmp_path: Path) -> None:
    paths, run_dir, _staging_db, _csv_root = _ready_finalize_run(tmp_path)
    outside_file = tmp_path / "outside-lock-target"
    outside_file.write_text("must remain unchanged", encoding="utf-8")
    (run_dir / ".finalize.lock").symlink_to(outside_file)

    with pytest.raises(SystemExit, match="lock is a symlink"):
        workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    assert outside_file.read_text(encoding="utf-8") == "must remain unchanged"


def test_cancel_cannot_overwrite_an_active_finalize_state(tmp_path: Path) -> None:
    paths, run_dir, _staging_db, _csv_root = _ready_finalize_run(tmp_path)
    before = (run_dir / "run.json").read_bytes()

    with (
        workflow.finalize_run_lock(paths.runs_dir, run_dir),
        pytest.raises(SystemExit, match="already in progress"),
    ):
        workflow.cancel(
            SimpleNamespace(run_dir=run_dir, reason="synthetic cancellation"),
            paths,
        )

    assert (run_dir / "run.json").read_bytes() == before


@pytest.mark.parametrize(
    ("status", "failure_reason"),
    [
        ("finalizing", None),
        ("finalize_failed", None),
        ("failed", "finalize_failed"),
    ],
)
def test_cancel_rejects_finalize_recovery_states_after_lock_release(
    tmp_path: Path,
    status: str,
    failure_reason: str | None,
) -> None:
    paths, run_dir, staging_db, _csv_root = _ready_finalize_run(tmp_path)
    workflow.promote_database(staging_db, paths.db_path, paths.backup_dir)
    run_state = workflow.read_json(run_dir / "run.json")
    run_state["status"] = status
    if failure_reason is not None:
        run_state["failure_reason"] = failure_reason
    workflow.write_run_state(run_dir, run_state)
    before = (run_dir / "run.json").read_bytes()

    with pytest.raises(SystemExit, match="must be resumed with finalize"):
        workflow.cancel(
            SimpleNamespace(run_dir=run_dir, reason="must not suppress recovery"),
            paths,
        )

    assert (run_dir / "run.json").read_bytes() == before


def test_archive_retry_after_death_reuses_published_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "inbox" / "statement.pdf"
    target = tmp_path / "processed" / "statement.pdf"
    source.parent.mkdir()
    source.write_bytes(b"%PDF synthetic archive")
    item = {
        "original_pdf": str(source),
        "processed_pdf": str(target),
        "file_hash": workflow.sha256_file(source),
        "skipped_reason": "",
    }
    real_unlink = Path.unlink
    killed = False

    def die_after_destination_publish(
        path: Path,
        *args: object,
        **kwargs: object,
    ) -> None:
        nonlocal killed
        if path == source and not killed:
            killed = True
            raise KeyboardInterrupt("synthetic death after archive publication")
        real_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "unlink", die_after_destination_publish)
    with pytest.raises(KeyboardInterrupt, match="synthetic death"):
        workflow._archive_finalize_pdfs(
            [item],
            inbox_root=source.parent,
            processed_root=target.parent,
        )

    assert source.is_file()
    assert target.is_file()
    monkeypatch.setattr(Path, "unlink", real_unlink)

    finalized, outcome = workflow._archive_finalize_pdfs(
        [item],
        inbox_root=source.parent,
        processed_root=target.parent,
    )

    assert not source.exists()
    assert target.is_file()
    assert not target.with_name("statement-2.pdf").exists()
    assert finalized[0]["processed_pdf"] == str(target.resolve())
    assert finalized[0].get("finalize_warning") is None
    assert outcome == {
        "status": "success",
        "attempted": 1,
        "archived": 1,
        "warnings": [],
    }


def test_archive_recovery_preserves_source_when_hash_changed(tmp_path: Path) -> None:
    source = tmp_path / "inbox" / "statement.pdf"
    target = tmp_path / "processed" / "statement.pdf"
    source.parent.mkdir()
    target.parent.mkdir()
    original_bytes = b"%PDF originally staged"
    source.write_bytes(original_bytes)
    expected_hash = workflow.sha256_file(source)
    target.write_bytes(original_bytes)
    source.write_bytes(b"%PDF changed after staging")
    item = {
        "original_pdf": str(source),
        "processed_pdf": str(target),
        "file_hash": expected_hash,
        "skipped_reason": "",
    }

    finalized, outcome = workflow._archive_finalize_pdfs(
        [item],
        inbox_root=source.parent,
        processed_root=target.parent,
    )

    assert source.read_bytes() == b"%PDF changed after staging"
    assert target.read_bytes() == original_bytes
    assert not target.with_name("statement-2.pdf").exists()
    assert finalized[0]["finalize_warning"] == "archive_source_hash_changed"
    assert outcome == {
        "status": "warning",
        "attempted": 1,
        "archived": 0,
        "warnings": ["archive_source_hash_changed"],
    }


def test_unreadable_archive_candidate_is_a_durable_finalize_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, run_dir, _staging_db, _csv_root = _ready_finalize_run(tmp_path, with_pdf=True)
    _patch_finalize_dependencies(monkeypatch)
    staged = workflow.read_json(run_dir / "run.json")
    original = Path(staged["pdfs"][0]["original_pdf"])
    candidate = Path(staged["pdfs"][0]["processed_pdf"])
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_bytes(b"%PDF pre-existing archive candidate")
    real_hash = workflow.sha256_file

    def fail_candidate_read(path: Path) -> str:
        if path == candidate:
            raise PermissionError("synthetic unreadable archive candidate")
        return real_hash(path)

    monkeypatch.setattr(workflow, "sha256_file", fail_candidate_read)

    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    finalized = workflow.read_json(run_dir / "run.json")
    assert finalized["status"] == "finalized"
    assert finalized["archive_outcome"] == {
        "status": "warning",
        "attempted": 0,
        "archived": 0,
        "warnings": ["archive_candidate_unreadable"],
    }
    assert finalized["pdfs"][0]["finalize_warning"] == "archive_candidate_unreadable"
    assert original.is_file()
    assert candidate.is_file()


@pytest.mark.parametrize("case", ["outside", "symlink_parent", "run_root"])
def test_finalize_staging_cleanup_rejects_uncontained_paths(
    tmp_path: Path,
    case: str,
) -> None:
    run_dir = tmp_path / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "retain.txt"
    sentinel.write_text("retain", encoding="utf-8")

    if case == "outside":
        cleanup_path = outside
    elif case == "symlink_parent":
        linked_parent = run_dir / "linked"
        linked_parent.symlink_to(outside, target_is_directory=True)
        cleanup_path = linked_parent
    else:
        cleanup_path = run_dir

    with pytest.raises(ValueError, match="contained child of its import run"):
        workflow._cleanup_finalize_staging(
            run_dir,
            {"csv_root": str(cleanup_path)},
        )

    assert sentinel.read_text(encoding="utf-8") == "retain"
    assert run_dir.is_dir()


def test_archive_rejects_outside_source_before_read_or_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inbox_root = tmp_path / "inbox"
    processed_root = tmp_path / "processed"
    outside_source = tmp_path / "outside" / "private-statement.pdf"
    target = processed_root / "statement.pdf"
    inbox_root.mkdir()
    processed_root.mkdir()
    outside_source.parent.mkdir()
    outside_source.write_bytes(b"%PDF must not be read or removed")
    item = {
        "original_pdf": str(outside_source),
        "processed_pdf": str(target),
        "file_hash": "0" * 64,
        "skipped_reason": "",
    }
    real_hash = workflow.sha256_file
    real_unlink = Path.unlink

    def guard_hash(path: Path) -> str:
        if path == outside_source:
            raise AssertionError("outside source was read")
        return real_hash(path)

    def guard_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if path == outside_source:
            raise AssertionError("outside source was unlinked")
        real_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(workflow, "sha256_file", guard_hash)
    monkeypatch.setattr(Path, "unlink", guard_unlink)

    finalized, outcome = workflow._archive_finalize_pdfs(
        [item],
        inbox_root=inbox_root,
        processed_root=processed_root,
    )

    assert outside_source.read_bytes() == b"%PDF must not be read or removed"
    assert not target.exists()
    assert finalized[0]["finalize_warning"] == "archive_source_outside_inbox"
    assert outcome["warnings"] == ["archive_source_outside_inbox"]


def test_archive_rejects_symlink_candidate_before_hashing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inbox_root = tmp_path / "inbox"
    processed_root = tmp_path / "processed"
    source = inbox_root / "statement.pdf"
    target = processed_root / "statement.pdf"
    outside_target = tmp_path / "outside-target.pdf"
    inbox_root.mkdir()
    processed_root.mkdir()
    source.write_bytes(b"%PDF staged statement")
    outside_target.write_bytes(b"%PDF staged statement")
    target.symlink_to(outside_target)
    item = {
        "original_pdf": str(source),
        "processed_pdf": str(target),
        "file_hash": workflow.sha256_file(source),
        "skipped_reason": "",
    }
    real_hash = workflow.sha256_file

    def reject_symlink_hash(path: Path) -> str:
        if path.is_symlink() or path == outside_target:
            raise AssertionError("symlink candidate was hashed")
        return real_hash(path)

    monkeypatch.setattr(workflow, "sha256_file", reject_symlink_hash)

    finalized, outcome = workflow._archive_finalize_pdfs(
        [item],
        inbox_root=inbox_root,
        processed_root=processed_root,
    )

    assert source.is_file()
    assert target.is_symlink()
    assert outside_target.read_bytes() == b"%PDF staged statement"
    assert finalized[0]["finalize_warning"] == "archive_candidate_symlink"
    assert outcome["warnings"] == ["archive_candidate_symlink"]


def test_archive_allows_fresh_rerun_when_inbox_and_processed_roots_match(tmp_path: Path) -> None:
    shared_root = tmp_path / "processed"
    source_and_target = shared_root / "statement.pdf"
    shared_root.mkdir()
    source_and_target.write_bytes(b"%PDF retained processed statement")
    item = {
        "original_pdf": str(source_and_target),
        "processed_pdf": str(source_and_target),
        "file_hash": workflow.sha256_file(source_and_target),
        "skipped_reason": "",
    }

    finalized, outcome = workflow._archive_finalize_pdfs(
        [item],
        inbox_root=shared_root,
        processed_root=shared_root,
    )

    assert source_and_target.read_bytes() == b"%PDF retained processed statement"
    assert finalized[0]["processed_pdf"] == str(source_and_target.resolve())
    assert outcome["status"] == "success"


def test_finalize_uses_staged_archive_roots_for_fresh_rerun(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, run_dir, _staging_db, _csv_root = _ready_finalize_run(
        tmp_path, with_pdf=True, source_in_processed=True
    )
    _patch_finalize_dependencies(monkeypatch)
    run_state = workflow.read_json(run_dir / "run.json")
    retained_pdf = Path(run_state["pdfs"][0]["original_pdf"])

    # The documented finalize command supplies only --run-dir, so its current
    # Paths object still has the default inbox. Archive containment must use the
    # roots captured by stage instead.
    assert paths.pdf_dir != paths.processed_dir
    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    finalized = workflow.read_json(run_dir / "run.json")
    assert retained_pdf.read_bytes() == b"%PDF synthetic archive"
    assert finalized["archive_outcome"] == {
        "status": "success",
        "attempted": 1,
        "archived": 1,
        "warnings": [],
    }
    assert finalized["pdfs"][0]["processed_pdf"] == str(retained_pdf.resolve())


pytestmark = pytest.mark.usefixtures("fictional_adapter_metadata")
