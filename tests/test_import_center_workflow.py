from __future__ import annotations

import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
from import_binding_fixtures import bound_import_state

from ledger.import_review import seed_review_candidates
from ledger.imports.pdf import controller
from ledger.imports.pdf.controller import accept_uploaded_pdf
from ledger.imports.pdf.paths import Paths
from ledger.imports.pdf.readiness import database_fingerprint, import_owned_fingerprint
from ledger.imports.pdf.statement_pdf_to_csv import ExtractedCsv
from ledger.processing import ProcessingResult
from ledger.storage.database import init_database
from ledger.utils.private_files import ensure_private_dir, write_private_text_atomic


def _paths(tmp_path: Path) -> Paths:
    private_root = ensure_private_dir(tmp_path / "private")
    inbox = ensure_private_dir(private_root / "inbox")
    processed = ensure_private_dir(private_root / "processed")
    target = private_root / "ledger.db"
    init_database(target)
    return Paths(
        pdf_dir=inbox,
        processed_dir=processed,
        db_path=target,
        base_db_path=None,
        backup_dir=private_root / "backups",
        ledger_config=tmp_path / "ledger.toml",
        merchant_rules=tmp_path / "merchants.toml",
        categories=tmp_path / "categories.toml",
        runs_dir=private_root / "runs",
    )


def _write_run(
    paths: Paths,
    *,
    status: str,
    pending_reviews: int = 0,
    failure_reason: str | None = None,
) -> Path:
    from ledger.imports.pdf import workflow

    run_dir = ensure_private_dir(paths.runs_dir / "run-1")
    csv_root = ensure_private_dir(run_dir / "csv")
    staging_db = run_dir / "staging.db"
    init_database(staging_db)
    if pending_reviews:
        seed_review_candidates(
            staging_db,
            [
                {
                    "key": "merchant:synthetic",
                    "merchant": "SYNTHETIC",
                    "current_category": "Other",
                    "txn_ids": ["txn-1"],
                    "examples": [],
                    "count": 1,
                    "total_abs_amount": "12.34",
                }
            ],
        )
    validation = run_dir / "statement-validation.json"
    validation.write_text("[]", encoding="utf-8")
    merchant_review = run_dir / "merchant-review.json"
    merchant_review.write_text("[]", encoding="utf-8")
    staging_inbox = ensure_private_dir(run_dir / "inbox")
    state = {
        "version": 1,
        "status": status,
        "target_db_path": str(paths.db_path),
        "staging_db_path": str(staging_db),
        "base_db_path": str(paths.db_path),
        "pdf_dir": str(paths.pdf_dir),
        "processed_dir": str(paths.processed_dir),
        "csv_root": str(csv_root),
        "staging_inbox": str(staging_inbox),
        "statement_validation": str(validation),
        "merchant_review_json": str(merchant_review),
        "pending_review_count": pending_reviews,
        "approved_csv_paths": [],
        "account_slugs": {},
        "target_db_fingerprint_before": database_fingerprint(paths.db_path),
        "target_db_import_owned_before": import_owned_fingerprint(paths.db_path),
        "pdfs": [],
    }
    if failure_reason is not None:
        state["failure_reason"] = failure_reason
    workflow.write_run_state(run_dir, bound_import_state(state, paths))
    write_private_text_atomic(paths.runs_dir / "latest-run.txt", str(run_dir))
    return run_dir


def _extract_one(pdf: Path, csv_root: Path, processed_dir: Path) -> ExtractedCsv:
    csv_path = csv_root / "2026-08" / "harbor-chequing-1001.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text(
        "Ledger Format,Date,Description,Amount,Currency,Reference,Institution,Account Type\nledger-mapped-v1,2026-08-02,SYNTHETIC,1.00,CAD,,harbor,Chequing\n",
        encoding="utf-8",
    )
    return ExtractedCsv(
        pdf=pdf,
        csv_path=csv_path,
        processed_pdf=processed_dir / "2026-08-31-harbor-deposit-chequing-1001.pdf",
        statement_type="harbor_deposit",
        transaction_count=1,
        account_slug="harbor-chequing-1001",
    )


def _patch_stage_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    from ledger.imports.pdf import workflow

    monkeypatch.setattr(
        workflow.statement_pdf_to_csv,
        "process_pdf",
        lambda pdf, csv_root, processed_dir, **_kwargs: _extract_one(pdf, csv_root, processed_dir),
    )
    monkeypatch.setattr(workflow, "load_config", lambda *_args: object())
    monkeypatch.setattr(
        workflow,
        "process",
        lambda **_kwargs: ProcessingResult(total_scanned=1),
    )
    monkeypatch.setattr(workflow, "validate_processing_result", lambda *_args: None)
    monkeypatch.setattr(workflow, "write_result_to_db", lambda *_args: None)
    monkeypatch.setattr(workflow, "build_merchant_review", lambda _result: [])
    monkeypatch.setattr(workflow, "record_db_operation", lambda *_args, **_kwargs: None)


def _patch_finalize_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    from ledger.imports.pdf import workflow

    monkeypatch.setattr(workflow, "load_config", lambda *_args: object())
    monkeypatch.setattr(workflow, "run_migrations", lambda _db: None)
    monkeypatch.setattr(
        workflow,
        "process",
        lambda **_kwargs: ProcessingResult(total_scanned=1),
    )
    monkeypatch.setattr(workflow, "validate_processing_result", lambda *_args: None)
    monkeypatch.setattr(workflow, "write_result_to_db", lambda *_args: None)
    monkeypatch.setattr(workflow, "build_merchant_review", lambda _result: [])
    monkeypatch.setattr(workflow, "seed_review_candidates", lambda *_args: None)
    monkeypatch.setattr(workflow, "get_review_rows", lambda _db: [])
    monkeypatch.setattr(workflow, "apply_review_decisions", lambda _db: 0)
    monkeypatch.setattr(workflow, "pending_review_count", lambda _db: 0)
    monkeypatch.setattr(workflow, "run_post_processing", lambda *_args: {})
    monkeypatch.setattr(workflow, "record_processed_files", lambda *_args: None)
    monkeypatch.setattr(
        workflow,
        "result_counts",
        lambda _result: {"transactions": 1, "transfers": 0},
    )
    monkeypatch.setattr(
        workflow,
        "summarize_final_merchants",
        lambda *_args: {"resolved_merchants": [], "manual_review_merchants": []},
    )
    monkeypatch.setattr(workflow, "print_merchant_summary", lambda _summary: None)


def test_upload_writes_one_owner_only_pdf_into_the_passed_inbox(tmp_path: Path) -> None:
    inbox = tmp_path / "private-inbox"
    content = b"%PDF-1.7\nsynthetic statement\n%%EOF\n"

    receipt = accept_uploaded_pdf(inbox, "August Statement.pdf", content)

    assert receipt.status == "stored"
    assert receipt.path == inbox / "August Statement.pdf"
    assert receipt.path.read_bytes() == content
    assert stat.S_IMODE(inbox.stat().st_mode) == 0o700
    assert stat.S_IMODE(receipt.path.stat().st_mode) == 0o600


def test_upload_normalizes_the_pdf_suffix_to_the_workflow_allowlist(tmp_path: Path) -> None:
    inbox = tmp_path / "private-inbox"

    receipt = accept_uploaded_pdf(inbox, "August Statement.PDF", b"%PDF-1.7\n%%EOF\n")

    assert receipt.path.name == "August Statement.pdf"
    assert [path.name for path in inbox.glob("*.pdf")] == ["August Statement.pdf"]


def test_upload_does_not_dedupe_against_a_file_the_workflow_cannot_discover(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "private-inbox"
    inbox.mkdir(mode=0o700)
    content = b"%PDF-1.7\n%%EOF\n"
    (inbox / "ignored.PDF").write_bytes(content)

    receipt = accept_uploaded_pdf(inbox, "statement.pdf", content)

    assert receipt.status == "stored"
    assert receipt.path.name == "statement.pdf"
    assert [path.name for path in inbox.glob("*.pdf")] == ["statement.pdf"]


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("../escape.pdf", b"%PDF-1.7\n%%EOF\n"),
        ("nested/statement.pdf", b"%PDF-1.7\n%%EOF\n"),
        (r"nested\statement.pdf", b"%PDF-1.7\n%%EOF\n"),
        (".hidden.pdf", b"%PDF-1.7\n%%EOF\n"),
        ("statement.txt", b"%PDF-1.7\n%%EOF\n"),
        ("statement.pdf.exe", b"%PDF-1.7\n%%EOF\n"),
        ("statement.pdf", b"not a pdf"),
        ("statement.pdf", b""),
    ],
)
def test_upload_rejects_names_and_bytes_outside_the_pdf_allowlist(
    tmp_path: Path,
    filename: str,
    content: bytes,
) -> None:
    inbox = tmp_path / "private-inbox"

    with pytest.raises(controller.UploadRejectedError):
        accept_uploaded_pdf(inbox, filename, content)

    assert not list(inbox.glob("*.pdf")) if inbox.exists() else True
    assert not (tmp_path / "escape.pdf").exists()


def test_upload_is_idempotent_by_hash_even_when_the_browser_name_changes(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "private-inbox"
    content = b"%PDF-1.7\nsame statement\n%%EOF\n"

    first = accept_uploaded_pdf(inbox, "statement.pdf", content)
    same_name = accept_uploaded_pdf(inbox, "statement.pdf", content)
    renamed = accept_uploaded_pdf(inbox, "download (1).pdf", content)

    assert first.status == "stored"
    assert same_name.status == "already_present"
    assert renamed.status == "already_present"
    assert same_name.path == first.path
    assert renamed.path == first.path
    assert [path.name for path in inbox.glob("*.pdf")] == ["statement.pdf"]


def test_upload_never_clobbers_a_different_pdf_with_the_same_name(tmp_path: Path) -> None:
    inbox = tmp_path / "private-inbox"
    original = b"%PDF-1.7\nfirst statement\n%%EOF\n"
    replacement = b"%PDF-1.7\ndifferent statement\n%%EOF\n"
    target = accept_uploaded_pdf(inbox, "statement.pdf", original).path

    with pytest.raises(controller.UploadConflictError, match="already exists"):
        accept_uploaded_pdf(inbox, "statement.pdf", replacement)

    assert target.read_bytes() == original


def test_upload_refuses_a_symlink_destination_without_touching_its_target(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "private-inbox"
    inbox.mkdir(mode=0o700)
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"outside")
    (inbox / "statement.pdf").symlink_to(outside)

    with pytest.raises(controller.UploadConflictError):
        accept_uploaded_pdf(inbox, "statement.pdf", b"%PDF-1.7\nnew\n%%EOF\n")

    assert outside.read_bytes() == b"outside"


def test_snapshot_offers_stage_only_when_the_passed_inbox_has_a_pdf(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    importer = controller.ImportCenterController(paths)

    empty = importer.snapshot()
    accept_uploaded_pdf(paths.pdf_dir, "statement.pdf", b"%PDF-1.7\n%%EOF\n")
    ready = importer.snapshot()

    assert empty.status == "idle"
    assert empty.actions == ("upload",)
    assert ready.status == "idle"
    assert ready.inbox_pdf_count == 1
    assert ready.actions == ("upload", "stage")


def test_snapshot_uses_live_review_readiness_instead_of_the_manifest_snapshot(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    run_dir = _write_run(paths, status="needs_review", pending_reviews=1)
    # Make the persisted manifest lie the way a stage-time snapshot naturally can.
    manifest_path = run_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["gates"]["review"] = "pass"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    state = controller.ImportCenterController(paths).snapshot()

    assert state.status == "needs_review"
    assert state.readiness is not None
    assert state.readiness["counts"]["pending_review"] == 1
    assert state.actions == ("upload", "review", "cancel")


def test_snapshot_allows_finalize_only_when_every_live_gate_is_clear(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    run_dir = _write_run(paths, status="ready_to_finalize")
    importer = controller.ImportCenterController(paths)

    ready = importer.snapshot()
    (run_dir / "csv").rmdir()
    blocked = importer.snapshot()

    assert ready.actions == ("upload", "finalize", "cancel")
    assert blocked.actions == ("upload", "cancel")
    assert "Staged CSV root is missing." in blocked.blockers


def test_snapshot_never_offers_finalize_while_stage_is_incomplete(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _write_run(paths, status="staging")

    snapshot = controller.ImportCenterController(paths).snapshot()

    assert snapshot.readiness is not None
    assert snapshot.actions == ("upload", "cancel")


@pytest.mark.parametrize("status", ["finalizing", "finalize_failed"])
def test_snapshot_routes_finalize_protocol_states_to_resume_only(
    tmp_path: Path,
    status: str,
) -> None:
    paths = _paths(tmp_path)
    run_dir = _write_run(paths, status="ready_to_finalize")
    state_path = run_dir / "run.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update(
        {
            "status": status,
            "finalize_protocol_version": 2,
            "publication_phase": "preparing",
            "publication_token": "a" * 32,
        }
    )
    from ledger.imports.pdf import workflow

    workflow.write_run_state(run_dir, state)

    snapshot = controller.ImportCenterController(paths).snapshot()

    assert snapshot.status == status
    assert snapshot.protocol_valid
    assert snapshot.actions == ("upload", "resume_finalize")


def test_snapshot_fails_closed_when_publication_protocol_state_is_invalid(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    run_dir = _write_run(paths, status="ready_to_finalize")
    state_path = run_dir / "run.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update(
        {
            "status": "finalizing",
            "finalize_protocol_version": 2,
            "publication_phase": "published",
            "publication_completed": True,
        }
    )
    state_path.write_text(json.dumps(state), encoding="utf-8")

    snapshot = controller.ImportCenterController(paths).snapshot(run_dir=run_dir)

    assert not snapshot.protocol_valid
    assert snapshot.actions == ("upload",)
    assert any("publication protocol" in item.lower() for item in snapshot.diagnostics)


def test_snapshot_fails_closed_when_staging_database_escapes_the_run(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    run_dir = _write_run(paths, status="ready_to_finalize")
    state_path = run_dir / "run.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["staging_db_path"] = str(paths.db_path)
    state_path.write_text(json.dumps(state), encoding="utf-8")

    snapshot = controller.ImportCenterController(paths).snapshot(run_dir=run_dir)

    assert not snapshot.protocol_valid
    assert snapshot.actions == ("upload",)
    assert any("staging database" in item.lower() for item in snapshot.diagnostics)


def test_snapshot_refuses_a_symlink_alias_for_an_import_run(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    run_dir = _write_run(paths, status="ready_to_finalize")
    alias = paths.runs_dir / "run-alias"
    alias.symlink_to(run_dir, target_is_directory=True)

    snapshot = controller.ImportCenterController(paths).snapshot(run_dir=alias)

    assert snapshot.run_dir is None
    assert snapshot.actions == ("upload",)
    assert any("outside its configured root" in item for item in snapshot.diagnostics)


def test_stage_calls_the_existing_workflow_with_explicit_paths_and_is_double_click_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    accept_uploaded_pdf(paths.pdf_dir, "statement.pdf", b"%PDF-1.7\n%%EOF\n")
    _patch_stage_dependencies(monkeypatch)
    importer = controller.ImportCenterController(paths)

    first = importer.stage()
    second = importer.stage()

    run_dirs = [path for path in paths.runs_dir.iterdir() if path.is_dir()]
    assert len(run_dirs) == 1
    assert first.changed
    assert first.snapshot.status == "ready_to_finalize"
    assert not second.changed
    assert second.snapshot.run_dir == first.snapshot.run_dir
    persisted = json.loads((run_dirs[0] / "run.json").read_text(encoding="utf-8"))
    assert persisted["target_db_path"] == str(paths.db_path)
    assert persisted["pdf_dir"] == str(paths.pdf_dir)


def test_review_summary_uses_the_staging_only_review_service(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    run_dir = _write_run(paths, status="needs_review", pending_reviews=1)

    result = controller.ImportCenterController(paths).review_summary(run_dir=run_dir)

    assert result["run_status"] == "needs_review"
    assert result["pending_review"] == 1
    assert result["rows"] == [
        {
            "key": "merchant:synthetic",
            "status": "pending",
            "needs_decision": True,
            "merchant": "SYNTHETIC",
            "category": "Other",
            "note": "",
            "transactions": 1,
            "amount": "12.34",
        }
    ]


def test_cancel_calls_the_existing_workflow_and_a_repeat_is_a_noop(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    run_dir = _write_run(paths, status="needs_review", pending_reviews=1)
    importer = controller.ImportCenterController(paths)

    first = importer.cancel(run_dir=run_dir, reason="ui_cancel")
    second = importer.cancel(run_dir=run_dir, reason="ui_cancel")

    persisted = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert first.changed
    assert first.snapshot.status == "cancelled"
    assert not second.changed
    assert persisted["status"] == "cancelled"
    assert persisted["cancel_reason"] == "ui_cancel"


def test_finalize_double_click_publishes_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ledger.imports.pdf import workflow

    paths = _paths(tmp_path)
    run_dir = _write_run(paths, status="ready_to_finalize")
    _patch_finalize_dependencies(monkeypatch)
    real_promote = workflow._promote_database_locked
    promote_calls = 0

    def count_promote(*args, **kwargs):
        nonlocal promote_calls
        promote_calls += 1
        return real_promote(*args, **kwargs)

    monkeypatch.setattr(workflow, "_promote_database_locked", count_promote)
    importer = controller.ImportCenterController(paths)

    first = importer.finalize(run_dir=run_dir)
    second = importer.finalize(run_dir=run_dir)

    assert first.changed
    assert first.snapshot.status == "finalized"
    assert not second.changed
    assert second.snapshot.status == "finalized"
    assert promote_calls == 1


def test_finalize_retry_resumes_the_published_tail_without_publishing_twice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.imports.pdf import workflow

    paths = _paths(tmp_path)
    run_dir = _write_run(paths, status="ready_to_finalize")
    _patch_finalize_dependencies(monkeypatch)
    real_promote = workflow._promote_database_locked
    real_archive = workflow._archive_finalize_pdfs
    promote_calls = 0
    fail_once = True

    def count_promote(*args, **kwargs):
        nonlocal promote_calls
        promote_calls += 1
        return real_promote(*args, **kwargs)

    def interrupt_tail(*args, **kwargs):
        nonlocal fail_once
        if fail_once:
            fail_once = False
            raise OSError("synthetic archive interruption")
        return real_archive(*args, **kwargs)

    monkeypatch.setattr(workflow, "_promote_database_locked", count_promote)
    monkeypatch.setattr(workflow, "_archive_finalize_pdfs", interrupt_tail)
    importer = controller.ImportCenterController(paths)

    with pytest.raises(controller.ImportCenterOperationError, match="archive interruption"):
        importer.finalize(run_dir=run_dir)
    interrupted = importer.snapshot(run_dir=run_dir)
    retried = importer.finalize(run_dir=run_dir)

    assert interrupted.status == "finalizing"
    assert interrupted.publication_completed
    assert interrupted.actions == ("upload", "resume_finalize")
    assert retried.snapshot.status == "finalized"
    assert promote_calls == 1


def test_import_center_page_paths_follow_the_explicit_context_database(tmp_path: Path) -> None:
    from ledger.ui.pages import import_center

    db_path = (tmp_path / "isolated" / "ledger.db").resolve()

    paths = import_center._controller_paths(SimpleNamespace(db_path=db_path))

    assert paths.db_path == db_path
    assert paths.pdf_dir == db_path.parent / "inbox"
    assert paths.processed_dir == db_path.parent / "processed"
    assert paths.backup_dir == db_path.parent / "db-backups"
    assert paths.runs_dir == db_path.parent / "import-runs"


def test_import_center_next_action_names_finalize_recovery(tmp_path: Path) -> None:
    from ledger.imports.pdf import workflow
    from ledger.ui.pages import import_center

    paths = _paths(tmp_path)
    run_dir = _write_run(paths, status="ready_to_finalize")
    state_path = run_dir / "run.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.update(
        {
            "status": "finalize_failed",
            "finalize_protocol_version": 2,
            "publication_phase": "preparing",
            "publication_token": "a" * 32,
        }
    )
    workflow.write_run_state(run_dir, state)
    snapshot = controller.ImportCenterController(paths).snapshot(run_dir=run_dir)

    action, detail = import_center._snapshot_next_action(snapshot)

    assert action == "Resume finalize"
    assert "recovery" in detail.lower()


def test_completed_import_action_invalidates_cached_financial_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A finalized import must not leave Overview's period brief/KPIs cached."""
    from ledger.ui.pages import import_center

    cleared: list[bool] = []
    monkeypatch.setattr(import_center.st.cache_data, "clear", lambda: cleared.append(True))

    import_center._clear_read_caches_after_change(changed=False)
    import_center._clear_read_caches_after_change(changed=True)

    assert cleared == [True]


def test_finalized_manifest_drives_an_exact_before_after_database_summary(
    tmp_path: Path,
) -> None:
    from ledger.imports.pdf.manifest import build_manifest
    from ledger.ui.pages import import_center

    run_dir = tmp_path / "run-1"
    run_dir.mkdir()
    manifest = build_manifest(
        run_dir,
        {
            "status": "finalized",
            "target_db_before": {
                "transactions": 10,
                "transfers": 2,
                "import_runs": 1,
            },
            "target_db_after_promote": {
                "transactions": 13,
                "transfers": 4,
                "import_runs": 2,
            },
            "pdfs": [],
        },
    )

    assert manifest["database_counts"] == {
        "before": {"transactions": 10, "transfers": 2, "import_runs": 1},
        "after": {"transactions": 13, "transfers": 4, "import_runs": 2},
        "change": {"transactions": 3, "transfers": 2, "import_runs": 1},
    }
    assert import_center._before_after_rows(manifest) == [
        {"Record": "Transactions", "Before": 10, "After": 13, "Change": 3},
        {"Record": "Transfers", "Before": 2, "After": 4, "Change": 2},
        {"Record": "Import runs", "Before": 1, "After": 2, "Change": 1},
    ]


def _import_center_page_script(db_str: str) -> None:  # pragma: no cover - AppTest subprocess
    from pathlib import Path
    from types import SimpleNamespace

    import streamlit as st

    from ledger.ui.pages import import_center

    ctx = SimpleNamespace(
        db_path=Path(db_str),
        pending_reviews=0,
        review_source={"source_kind": "Current DB", "status": "live_db"},
    )
    pages = [
        st.Page(lambda: import_center.render(ctx), title="Import Center", default=True),
        st.Page(
            lambda: st.write("Transaction details"), title="Transactions", url_path="transactions"
        ),
    ]
    st.session_state["ledger_pages"] = {page.url_path: page for page in pages}
    st.navigation(pages).run()


def test_import_center_idle_page_renders_upload_and_disabled_mutations(tmp_path: Path) -> None:
    from streamlit.testing.v1 import AppTest

    paths = _paths(tmp_path)
    at = AppTest.from_function(
        _import_center_page_script,
        args=(str(paths.db_path),),
        default_timeout=30,
    )

    at.run()

    assert not at.exception, at.exception
    assert len(at.get("file_uploader")) == 1
    buttons = {button.label: button for button in at.button}
    assert buttons["Add PDFs to inbox"].disabled
    assert buttons["Stage inbox PDFs"].disabled
    assert any("No import run" in info.value for info in at.info)


def test_import_center_ready_page_enables_finalize_and_guards_cancel(tmp_path: Path) -> None:
    from streamlit.testing.v1 import AppTest

    from ledger.ui.pages import import_center

    paths = _paths(tmp_path)
    ui_paths = import_center._controller_paths(SimpleNamespace(db_path=paths.db_path))
    _write_run(ui_paths, status="ready_to_finalize")
    at = AppTest.from_function(
        _import_center_page_script,
        args=(str(paths.db_path),),
        default_timeout=30,
    )

    at.run()

    assert not at.exception, at.exception
    buttons = {button.label: button for button in at.button}
    assert not buttons["Finalize import"].disabled
    assert buttons["Cancel import run"].disabled
    assert any("ready_to_finalize" in markdown.value for markdown in at.markdown)


def test_disposable_import_walkthrough_reaches_before_after_and_brief_ui(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise upload → stage → finalize → ordinary result UI on one disposable tree."""
    from streamlit.testing.v1 import AppTest

    from ledger.imports.pdf import workflow
    from ledger.ui.pages import import_center

    class SyntheticBriefService:
        def __init__(self, _db_path: Path):
            pass

        def build_default(self, *, since=None) -> dict:
            return {
                "schema_version": 1,
                "status": "ready",
                "period": {"start": "2026-08-01", "end": "2026-08-31"},
                "comparison": {"start": "2026-07-01", "end": "2026-07-31"},
                "observations": [
                    {
                        "rank": index,
                        "label": "arithmetic",
                        "title": f"Synthetic observation {index}",
                        "statement": "Derived from disposable imported evidence.",
                        "dashboard_path": "/transactions",
                        "evidence_ids": [],
                    }
                    for index in range(1, 4)
                ],
                "coverage": {"completeness": {"status": "not_asserted"}},
            }

    target = _paths(tmp_path).db_path
    paths = import_center._controller_paths(SimpleNamespace(db_path=target))
    importer = controller.ImportCenterController(paths)
    importer.upload("statement.pdf", b"%PDF-1.7\nsynthetic\n%%EOF\n")
    _patch_stage_dependencies(monkeypatch)
    staged = importer.stage()
    assert staged.snapshot.status == "ready_to_finalize"

    _patch_finalize_dependencies(monkeypatch)
    monkeypatch.setattr(workflow, "FinancialBriefService", SyntheticBriefService)
    finalized = importer.finalize(run_dir=staged.snapshot.run_dir)
    assert finalized.snapshot.status == "finalized"

    at = AppTest.from_function(
        _import_center_page_script,
        args=(str(paths.db_path),),
        default_timeout=30,
    )
    at.run()

    assert not at.exception, at.exception
    rendered = "\n".join(
        str(element.value)
        for kind in (at.subheader, at.markdown, at.caption, at.info)
        for element in kind
    )
    assert "Before / after" in rendered
    assert "Financial Brief" in rendered
    assert "Synthetic observation 1" in rendered
    links = at.get("page_link")
    assert len(links) == 3
    transactions_page = at.session_state["ledger_pages"]["transactions"]
    assert all(link.proto.label == "Open supporting details" for link in links)
    assert all(not link.proto.external for link in links)
    assert all(link.proto.page_script_hash == transactions_page._script_hash for link in links)
    at._page_hash = links[0].proto.page_script_hash
    at.run()
    assert not at.exception
    assert any(element.value == "Transaction details" for element in at.markdown)


def test_read_only_ui_smoke_refuses_every_import_center_mutation() -> None:
    from ledger.ui.smoke import ensure_read_only_click

    for label in (
        "Add PDFs to inbox",
        "Stage inbox PDFs",
        "Finalize import",
        "Resume finalize",
        "Cancel import run",
    ):
        with pytest.raises(ValueError, match="mutating control"):
            ensure_read_only_click(label)


pytestmark = pytest.mark.usefixtures("fictional_adapter_metadata")
