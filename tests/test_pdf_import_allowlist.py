"""Regression tests for explicit staged-CSV selection in PDF imports."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from import_binding_fixtures import bound_import_state

from ledger.adapters.contracts import AdapterError
from ledger.config import AppConfig
from ledger.imports.pdf import workflow
from ledger.imports.pdf.files import write_run_state
from ledger.imports.pdf.paths import Paths
from ledger.imports.pdf.statement_pdf_to_csv import ExtractedCsv
from ledger.processing import ProcessingResult, pipeline
from ledger.storage.database import init_database

HARBOR_HEADER = (
    "Ledger Format,Date,Description,Amount,Currency,Reference,Institution,Account Type\n"
)


class _StopFinalizeError(RuntimeError):
    """Stop finalize immediately after observing its process() arguments."""


def _write_harbor_csv(path: Path, description: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        HARBOR_HEADER + f"ledger-mapped-v1,2026-01-05,{description},-5.00,CAD,,harbor,Chequing\n",
        encoding="utf-8",
    )


def test_process_uses_only_explicit_source_files(
    tmp_path: Path,
    sample_app_config: AppConfig,
) -> None:
    csv_root = tmp_path / "csv"
    approved = csv_root / "approved.csv"
    rogue = csv_root / "rogue.csv"
    _write_harbor_csv(approved, "APPROVED ROW")
    _write_harbor_csv(rogue, "ROGUE ROW")

    result = pipeline.process(
        sample_app_config,
        csv_root,
        source_files=[approved],
    )

    descriptions = {transaction.description for transaction in result.transactions}
    assert result.total_scanned == 1
    assert any("APPROVED ROW" in description for description in descriptions)
    assert all("ROGUE ROW" not in description for description in descriptions)


def test_process_accepts_explicit_files_with_a_relative_source_root(
    tmp_path: Path,
    sample_app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    csv_root = tmp_path / "csv"
    approved = csv_root / "approved.csv"
    _write_harbor_csv(approved, "APPROVED ROW")
    monkeypatch.chdir(tmp_path)

    result = pipeline.process(
        sample_app_config,
        Path("csv"),
        source_files=[Path("csv/approved.csv")],
    )

    assert result.total_scanned == 1
    assert result.transactions[0].source_path == "approved.csv"


@pytest.mark.parametrize("invalid_kind", ["missing", "outside", "symlink_escape"])
def test_process_rejects_invalid_explicit_files_before_parsing(
    tmp_path: Path,
    sample_app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
    invalid_kind: str,
) -> None:
    csv_root = tmp_path / "csv"
    approved = csv_root / "approved.csv"
    _write_harbor_csv(approved, "APPROVED ROW")

    if invalid_kind == "missing":
        invalid = csv_root / "missing.csv"
    else:
        outside = tmp_path / "outside.csv"
        _write_harbor_csv(outside, "OUTSIDE ROW")
        if invalid_kind == "outside":
            invalid = outside
        else:
            invalid = csv_root / "escaped.csv"
            invalid.symlink_to(outside)

    parse_calls: list[Path] = []

    def record_parse(*args, **kwargs):
        parse_calls.append(Path(args[2]))
        return []

    monkeypatch.setattr(pipeline, "parse_file", record_parse)

    with pytest.raises((FileNotFoundError, ValueError)):
        pipeline.process(
            sample_app_config,
            csv_root,
            source_files=[approved, invalid],
        )

    assert parse_calls == []


def test_process_rejects_explicit_non_csv_file(
    tmp_path: Path,
    sample_app_config: AppConfig,
) -> None:
    csv_root = tmp_path / "csv"
    csv_root.mkdir()
    not_csv = csv_root / "statement.txt"
    not_csv.write_text("not a CSV", encoding="utf-8")

    with pytest.raises(ValueError, match="CSV"):
        pipeline.process(sample_app_config, csv_root, source_files=[not_csv])


def _stage_paths(tmp_path: Path) -> Paths:
    pdf_dir = tmp_path / "inbox"
    processed_dir = tmp_path / "processed"
    runs_dir = tmp_path / "runs"
    target_db = tmp_path / "ledger.db"
    pdf_dir.mkdir()
    processed_dir.mkdir()
    (pdf_dir / "good.pdf").write_bytes(b"%PDF good")
    (pdf_dir / "bad.pdf").write_bytes(b"%PDF bad")
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


def test_stage_allowlist_excludes_failed_statement_when_unlink_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _stage_paths(tmp_path)
    captured_source_files: list[Path] = []
    captured_account_slugs: dict[str, str] = {}

    def fake_process_pdf(
        pdf: Path,
        csv_root: Path,
        _processed_dir: Path,
        **_kwargs,
    ) -> ExtractedCsv:
        csv_path = csv_root / f"{pdf.stem}.csv"
        _write_harbor_csv(csv_path, f"{pdf.stem.upper()} ROW")
        errors = ("statement reconciliation failed",) if pdf.name == "bad.pdf" else ()
        return ExtractedCsv(
            pdf=pdf,
            csv_path=csv_path,
            processed_pdf=paths.processed_dir / pdf.name,
            statement_type="harbor_deposit",
            transaction_count=1,
            validation_errors=errors,
            account_slug="harbor-chequing-1001",
        )

    monkeypatch.setattr(workflow.statement_pdf_to_csv, "process_pdf", fake_process_pdf)
    monkeypatch.setattr(
        workflow.statement_pdf_to_csv,
        "write_manifest",
        lambda path, _results, dry_run: path.write_text("[]", encoding="utf-8"),
    )
    monkeypatch.setattr(workflow, "load_config", lambda _paths, _runtime_db: object())

    original_unlink = Path.unlink

    def fail_bad_unlink(path: Path, *args, **kwargs) -> None:
        if path.name == "bad.csv":
            raise OSError("forced unlink failure")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_bad_unlink)

    def capture_process(**kwargs) -> ProcessingResult:
        captured_source_files.extend(Path(path) for path in kwargs["source_files"])
        captured_account_slugs.update(kwargs["account_slugs"])
        return ProcessingResult(total_scanned=1)

    monkeypatch.setattr(workflow, "process", capture_process)
    monkeypatch.setattr(workflow, "validate_processing_result", lambda *_args: None)
    monkeypatch.setattr(workflow, "write_result_to_db", lambda *_args: None)
    monkeypatch.setattr(workflow, "build_merchant_review", lambda _result: [])
    monkeypatch.setattr(workflow, "seed_review_candidates", lambda *_args: None)
    monkeypatch.setattr(workflow, "write_merchant_review_markdown", lambda *_args: None)
    monkeypatch.setattr(workflow, "pending_review_count", lambda _db: 0)
    monkeypatch.setattr(workflow, "record_db_operation", lambda *_args, **_kwargs: None)

    workflow.stage(SimpleNamespace(), paths)

    [run_dir] = [path for path in paths.runs_dir.iterdir() if path.is_dir()]
    csv_root = run_dir / "csv"
    run_state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    approved = (csv_root / "good.csv").resolve()

    assert (csv_root / "bad.csv").exists(), "the forced cleanup failure did not occur"
    assert captured_source_files == [approved]
    assert captured_account_slugs == {"good.csv": "harbor-chequing-1001"}
    assert run_state["approved_csv_paths"] == [str(approved)]
    assert run_state["account_slugs"] == {"good.csv": "harbor-chequing-1001"}


def test_stage_rejects_approved_csv_without_canonical_account_slug(tmp_path: Path) -> None:
    csv_root = tmp_path / "csv"
    first = csv_root / "first.csv"
    second = csv_root / "second.csv"
    _write_harbor_csv(first, "FIRST ROW")
    _write_harbor_csv(second, "SECOND ROW")
    pdf_items = [
        {"csv_path": str(first), "account_slug": "harbor-chequing-1001"},
        {"csv_path": str(second)},
    ]

    with pytest.raises(ValueError, match="account slug mapping does not cover approved CSV paths"):
        workflow._account_slugs_for_stage(pdf_items, csv_root, [first, second])


def test_stage_rejects_conflicting_slugs_for_same_approved_csv(tmp_path: Path) -> None:
    csv_root = tmp_path / "csv"
    approved = csv_root / "approved.csv"
    _write_harbor_csv(approved, "APPROVED ROW")
    pdf_items = [
        {"csv_path": str(approved), "account_slug": "harbor-chequing-1001"},
        {"csv_path": str(approved), "account_slug": "harbor-chequing-2002"},
    ]

    with pytest.raises(ValueError, match="duplicate account slug path"):
        workflow._account_slugs_for_stage(pdf_items, csv_root, [approved])


def _finalize_paths(tmp_path: Path, runs_dir: Path) -> Paths:
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


def _write_minimal_finalize_state(
    tmp_path: Path,
    *,
    approved_csv_paths: list[str] | None,
    pdfs: list[dict[str, object]],
    account_slugs: dict[str, str] | None = None,
) -> tuple[Paths, Path, Path]:
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "run-1"
    csv_root = run_dir / "csv"
    csv_root.mkdir(parents=True)
    target_db = tmp_path / "ledger.db"
    staging_db = run_dir / "staging.db"
    target_db.write_bytes(b"target")
    staging_db.write_bytes(b"staging")
    merchant_review = run_dir / "merchant-review.json"
    merchant_review.write_text("[]", encoding="utf-8")
    state: dict[str, object] = {
        "status": "ready_to_finalize",
        "target_db_path": str(target_db),
        "staging_db_path": str(staging_db),
        "csv_root": str(csv_root),
        "merchant_review_json": str(merchant_review),
        "pdfs": pdfs,
    }
    if approved_csv_paths is not None:
        state["approved_csv_paths"] = approved_csv_paths
    if account_slugs is not None:
        state["account_slugs"] = account_slugs
    write_run_state(run_dir, state)
    return _finalize_paths(tmp_path, runs_dir), run_dir, csv_root


@pytest.mark.parametrize("legacy", [False, True])
def test_finalize_reparses_only_a_bound_allowlist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    legacy: bool,
) -> None:
    provisional_root = tmp_path / "runs" / "run-1" / "csv"
    approved = provisional_root / "approved.csv"
    skipped = provisional_root / "skipped.csv"
    rogue = provisional_root / "rogue.csv"
    pdfs = [
        {"csv_path": str(approved), "skipped_reason": ""},
        {"csv_path": str(skipped), "skipped_reason": "statement_validation_failed"},
    ]
    persisted = None if legacy else [str(approved)]
    paths, run_dir, _csv_root = _write_minimal_finalize_state(
        tmp_path,
        approved_csv_paths=persisted,
        pdfs=pdfs,
        account_slugs=None if legacy else {"approved.csv": "harbor-chequing-1001"},
    )
    _write_harbor_csv(approved, "APPROVED ROW")
    _write_harbor_csv(skipped, "SKIPPED ROW")
    _write_harbor_csv(rogue, "ROGUE ROW")
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert workflow._approved_csv_paths_for_finalize(state, _csv_root) == [approved.resolve()]
    if not legacy:
        write_run_state(run_dir, bound_import_state(state, paths))

    captured_source_files: list[Path] = []
    captured_account_slugs: dict[str, str] = {}
    monkeypatch.setattr(workflow, "_fail_if_target_db_changed", lambda *_args: None)
    monkeypatch.setattr(workflow, "load_config", lambda *_args: object())
    monkeypatch.setattr(workflow, "run_migrations", lambda _db: None)

    def capture_process(**kwargs):
        captured_source_files.extend(Path(path) for path in kwargs["source_files"])
        captured_account_slugs.update(kwargs["account_slugs"])
        raise _StopFinalizeError()

    monkeypatch.setattr(workflow, "process", capture_process)

    if legacy:
        saved_state = (run_dir / "run.json").read_bytes()
        with pytest.raises(AdapterError, match="import_binding_missing"):
            workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)
        assert captured_source_files == []
        assert captured_account_slugs == {}
        assert (run_dir / "run.json").read_bytes() == saved_state
        return

    with pytest.raises(_StopFinalizeError):
        workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    assert captured_source_files == [approved.resolve()]
    assert captured_account_slugs == {"approved.csv": "harbor-chequing-1001"}
    final_state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert final_state["approved_csv_paths"] == [str(approved.resolve())]
    assert final_state["account_slugs"] == captured_account_slugs


def test_finalize_rejects_partial_account_slug_mapping(tmp_path: Path) -> None:
    csv_root = tmp_path / "csv"
    first = csv_root / "first.csv"
    second = csv_root / "second.csv"
    _write_harbor_csv(first, "FIRST ROW")
    _write_harbor_csv(second, "SECOND ROW")
    run_state = {"account_slugs": {"first.csv": "harbor-chequing-1001"}}

    with pytest.raises(ValueError, match="account slug mapping does not cover approved CSV paths"):
        workflow._account_slugs_for_finalize(run_state, csv_root, [first, second])


def test_finalize_rejects_canonicalized_duplicate_account_slug_paths(tmp_path: Path) -> None:
    csv_root = tmp_path / "csv"
    approved = csv_root / "approved.csv"
    _write_harbor_csv(approved, "APPROVED ROW")
    run_state = {
        "account_slugs": {
            "approved.csv": "harbor-chequing-1001",
            "./approved.csv": "harbor-chequing-2002",
        }
    }

    with pytest.raises(ValueError, match="duplicate account slug path"):
        workflow._account_slugs_for_finalize(run_state, csv_root, [approved])


def test_finalize_rejects_explicit_null_allowlist_before_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provisional_root = tmp_path / "runs" / "run-1" / "csv"
    approved = provisional_root / "approved.csv"
    paths, run_dir, _csv_root = _write_minimal_finalize_state(
        tmp_path,
        approved_csv_paths=None,
        pdfs=[{"csv_path": str(approved), "skipped_reason": ""}],
    )
    _write_harbor_csv(approved, "APPROVED ROW")
    run_state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    run_state["approved_csv_paths"] = None
    write_run_state(run_dir, run_state)

    side_effects: list[str] = []
    monkeypatch.setattr(workflow, "_fail_if_target_db_changed", lambda *_args: None)
    monkeypatch.setattr(
        workflow,
        "load_config",
        lambda *_args: side_effects.append("config") or object(),
    )

    def unexpected_process(**_kwargs):
        side_effects.append("process")
        raise _StopFinalizeError()

    monkeypatch.setattr(workflow, "process", unexpected_process)
    monkeypatch.setattr(
        workflow,
        "run_migrations",
        lambda _db: side_effects.append("migration"),
    )

    with pytest.raises(ValueError, match="invalid approved CSV path list"):
        workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    assert side_effects == []


def test_legacy_finalize_approves_nothing_when_an_importable_item_has_no_csv_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provisional_root = tmp_path / "runs" / "run-1" / "csv"
    approved = provisional_root / "approved.csv"
    paths, run_dir, _csv_root = _write_minimal_finalize_state(
        tmp_path,
        approved_csv_paths=None,
        pdfs=[
            {"csv_path": str(approved), "skipped_reason": ""},
            {"skipped_reason": ""},
        ],
    )
    _write_harbor_csv(approved, "APPROVED ROW")

    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert workflow._approved_csv_paths_for_finalize(state, _csv_root) == []
    captured_source_files: list[Path] = []
    monkeypatch.setattr(workflow, "_fail_if_target_db_changed", lambda *_args: None)
    monkeypatch.setattr(workflow, "load_config", lambda *_args: object())
    monkeypatch.setattr(workflow, "run_migrations", lambda _db: None)

    def capture_process(**kwargs):
        captured_source_files.extend(Path(path) for path in kwargs["source_files"])
        raise _StopFinalizeError()

    monkeypatch.setattr(workflow, "process", capture_process)

    with pytest.raises(AdapterError, match="import_binding_missing"):
        workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    assert captured_source_files == []


@pytest.mark.parametrize("invalid_kind", ["missing", "outside"])
def test_finalize_rejects_invalid_approved_path_before_database_mutation(
    tmp_path: Path,
    sample_app_config: AppConfig,
    monkeypatch: pytest.MonkeyPatch,
    invalid_kind: str,
) -> None:
    provisional_root = tmp_path / "runs" / "run-1" / "csv"
    if invalid_kind == "missing":
        invalid = provisional_root / "missing.csv"
    else:
        invalid = tmp_path / "outside.csv"
        _write_harbor_csv(invalid, "OUTSIDE ROW")
    paths, run_dir, _csv_root = _write_minimal_finalize_state(
        tmp_path,
        approved_csv_paths=[str(invalid)],
        pdfs=[],
    )

    database_mutations: list[str] = []
    monkeypatch.setattr(workflow, "_fail_if_target_db_changed", lambda *_args: None)

    def load_config(*_args):
        database_mutations.append("config")
        return sample_app_config

    monkeypatch.setattr(workflow, "load_config", load_config)
    monkeypatch.setattr(
        workflow,
        "run_migrations",
        lambda _db: database_mutations.append("migration"),
    )
    monkeypatch.setattr(
        workflow,
        "write_result_to_db",
        lambda *_args: database_mutations.append("write"),
    )

    with pytest.raises((FileNotFoundError, ValueError)):
        workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    assert database_mutations == []


pytestmark = pytest.mark.usefixtures("fictional_adapter_metadata")
