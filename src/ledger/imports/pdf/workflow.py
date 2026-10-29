#!/usr/bin/env python3
# ruff: noqa: E402,I001
"""
Incrementally import statement PDFs into an existing Ledger SQLite database.

This is the stateful PDF workflow used by the ledger-pdf-import skill. It treats
PDFs and generated CSVs as staging artifacts, and treats the SQLite database as
the working ledger source of truth.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from ledger.runtime import default_database_path, runtime_path, seed_config_path
from typing import Any

from sqlalchemy import text

SCRIPT_DIR = Path(__file__).resolve().parent


def _find_repo_root(start: Path) -> Path:
    """Resolve the repo root (the directory holding ``pyproject.toml``), which the
    default ``--pdf-dir``/``--db``/etc. paths hang off of.

    Walk up from this module and return the first ancestor containing
    ``pyproject.toml``. Falls back to the known source nesting depth
    ``src/ledger/imports/pdf`` → repo root (``parents[3]``) if the marker is absent
    (e.g. an installed wheel), never one-above-root as a hardcoded index once did.
    """
    for parent in (start, *start.parents):
        if (parent / "pyproject.toml").is_file():
            return parent
    return start.parents[3]


REPO_ROOT = _find_repo_root(SCRIPT_DIR)

from ledger.core.results import RunResult
from ledger.imports.pdf import statement_pdf_to_csv
from ledger.imports.pdf.files import (
    FinalizeLockError,
    _promote_database_locked,
    clear_latest_run_file,
    clone_database,
    db_counts,
    finalize_run_lock,
    known_pdf_hashes,
    make_run_dir,
    promote_database,  # noqa: F401 - compatibility re-export for workflow callers/tests
    read_json,
    resolve_run_dir,
    sha256_file,
    write_json,
    write_run_state,
)
from ledger.imports.pdf.manifest import next_actions_for, read_manifest
from ledger.imports.pdf.paths import Paths
from ledger.imports.pdf.post_processing import (
    persist_printed_credit_limits,
    persist_statement_coverage,
    run_post_processing,
)
from ledger.imports.pdf.processing import (
    is_no_activity_statement,
    load_config,
    no_activity_only_import,
    record_processed_files,
    result_counts,
    validate_processing_result,
    write_result_to_db,
)
from ledger.imports.pdf.readiness import (
    FINALIZABLE_STATUSES,
    PUBLICATION_PROTOCOL_VERSION,
    PublicationProofError,
    build_import_readiness,
    clear_publication_protocol,
    database_fingerprint,
    import_owned_fingerprint,
    import_owned_match,
    live_mutation_boundary,
    publication_candidate_fingerprint,
    publication_protocol_is_tokenized,
    recover_publication_completed,
    run_publication_completed,
    target_db_unchanged,
    validate_publication_protocol_state,
    verify_published_marker,
)
from ledger.imports.pdf.review import (
    build_merchant_review,
    print_merchant_summary,
    summarize_final_merchants,
    write_merchant_review_markdown,
)
from ledger.import_review import (
    REVIEW_STATUSES,
    apply_review_decisions,
    ensure_review_schema,
    get_review_rows,
    pending_review_count,
    review_needs_decision,
    save_review_rows,
    seed_review_candidates,
)
from ledger.processing import process
from ledger.processing.pipeline import validate_source_files
from ledger.reporting import FinancialBriefService
from ledger.storage.database import (
    AccountDB,
    DBOperationAuditDB,
    ReportSnapshotDB,
    TransactionDB,
    TransferDB,
    session_scope,
)
from ledger.storage.migrations import run_migrations
from ledger.storage.snapshots import database_target_lock
from ledger.storage.operations import record_db_operation, utc_now_dt as operation_utc_now
from ledger.utils.hashing import stable_id
from ledger.utils.json_io import utc_now
from ledger.utils.private_files import tighten_private_file
from ledger.utils.private_files import copy_private_file, ensure_private_dir
from ledger.adapters.provenance import (
    bind_import_inputs,
    configuration_bindings,
    file_binding,
    verify_import_inputs,
)
from ledger.adapters.registry import snapshot_bindings
from ledger.adapters.contracts import AdapterError


def remove_skipped_csvs(pdf_items: list[dict[str, Any]]) -> list[Path]:
    """Best-effort deletion of staged CSVs for skipped PDFs.

    The explicit approved CSV list is the safety boundary. This cleanup only reduces
    confusing leftovers; an unlink failure cannot make a skipped CSV eligible for
    processing. Returns the paths that were removed successfully.
    """
    removed: list[Path] = []
    for item in pdf_items:
        if not item.get("skipped_reason"):
            continue
        csv_path = item.get("csv_path")
        if not csv_path:
            continue
        path = Path(csv_path)
        with contextlib.suppress(OSError):
            path.unlink()
            removed.append(path)
        # A skipped statement never imports, so drop the now-stale CSV pointer from the
        # manifest item too (keeps run.json honest: no csv_path for a skipped PDF).
        item["csv_path"] = ""
    return removed


def _csv_paths_from_pdf_items(
    pdf_items: list[dict[str, Any]],
    *,
    require_path: bool,
) -> list[Path]:
    """Return CSV paths for non-skipped PDF items.

    New stage state requires a path for every importable PDF. Legacy run states may
    omit it; those items approve nothing rather than falling back to discovery.
    """
    paths: list[Path] = []
    for item in pdf_items:
        if item.get("skipped_reason"):
            continue
        raw_path = item.get("csv_path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            if require_path:
                raise ValueError("Importable PDF is missing its staged CSV path")
            return []
        paths.append(Path(raw_path))
    return paths


def _approved_csv_paths_for_stage(
    pdf_items: list[dict[str, Any]],
    csv_root: Path,
) -> list[Path]:
    """Build the validated stage allowlist from non-skipped PDF items."""
    return validate_source_files(
        csv_root,
        _csv_paths_from_pdf_items(pdf_items, require_path=True),
    )


def _approved_csv_paths_for_finalize(
    run_state: dict[str, Any],
    csv_root: Path,
) -> list[Path]:
    """Load a persisted allowlist or safely reconstruct one for a legacy run."""
    if "approved_csv_paths" not in run_state:
        candidates = _csv_paths_from_pdf_items(
            list(run_state.get("pdfs") or []),
            require_path=False,
        )
    else:
        persisted = run_state["approved_csv_paths"]
        if not isinstance(persisted, list) or any(
            not isinstance(path, str) or not path.strip() for path in persisted
        ):
            raise ValueError("Run state has an invalid approved CSV path list")
        candidates = [Path(path) for path in persisted]
    return validate_source_files(csv_root, candidates)


def _relative_csv_path(csv_root: Path, csv_path: Path) -> str:
    """Return the parser ``source_path`` key for a contained staged CSV."""
    return str(csv_path.expanduser().resolve().relative_to(csv_root.expanduser().resolve()))


def _account_slugs_for_stage(
    pdf_items: list[dict[str, Any]],
    csv_root: Path,
    approved_csv_paths: list[Path],
) -> dict[str, str]:
    """Build the approved relative-path to canonical-account mapping for stage."""
    approved = {path.expanduser().resolve() for path in approved_csv_paths}
    approved_relative = {_relative_csv_path(csv_root, path) for path in approved_csv_paths}
    account_slugs: dict[str, str] = {}
    for item in pdf_items:
        if item.get("skipped_reason"):
            continue
        raw_path = item.get("csv_path")
        raw_slug = item.get("account_slug")
        if not isinstance(raw_path, str) or not raw_path.strip():
            continue
        if not isinstance(raw_slug, str) or not raw_slug.strip():
            continue
        csv_path = Path(raw_path).expanduser().resolve()
        if csv_path not in approved:
            continue
        relative_path = _relative_csv_path(csv_root, csv_path)
        if relative_path in account_slugs:
            raise ValueError("account slug mapping has a duplicate account slug path")
        account_slugs[relative_path] = raw_slug.strip()
    if set(account_slugs) != approved_relative:
        raise ValueError("account slug mapping does not cover approved CSV paths")
    return account_slugs


def _account_slugs_for_finalize(
    run_state: dict[str, Any],
    csv_root: Path,
    approved_csv_paths: list[Path],
) -> dict[str, str]:
    """Reload a persisted account map; legacy states intentionally return empty."""
    if "account_slugs" not in run_state:
        return {}
    persisted = run_state["account_slugs"]
    if not isinstance(persisted, dict) or any(
        not isinstance(path, str)
        or not path.strip()
        or not isinstance(slug, str)
        or not slug.strip()
        for path, slug in persisted.items()
    ):
        raise ValueError("Run state has an invalid account slug mapping")

    approved = {_relative_csv_path(csv_root, path) for path in approved_csv_paths}
    account_slugs: dict[str, str] = {}
    for raw_path, raw_slug in persisted.items():
        relative_path = _relative_csv_path(csv_root, csv_root / raw_path)
        if relative_path not in approved:
            raise ValueError("Run state account slug path is not an approved CSV")
        if relative_path in account_slugs:
            raise ValueError("account slug mapping has a duplicate account slug path")
        account_slugs[relative_path] = raw_slug.strip()
    if set(account_slugs) != approved:
        raise ValueError("account slug mapping does not cover approved CSV paths")
    return account_slugs


def _base_run_state(
    *,
    paths: Paths,
    staging_db: Path,
    base_db: Path,
    csv_root: Path,
    target_db_fingerprint_before: dict,
    target_db_import_owned_before: dict,
    target_db_before: dict,
    staging_db_before: dict,
) -> dict[str, Any]:
    """Return the keys common to every run_state dict written by stage().

    Two complementary fingerprints are captured at stage time:
      - target_db_fingerprint_before: whole-file fingerprint (used by the UI
        readiness gate / target_db_unchanged; NOT changed by this feature).
      - target_db_import_owned_before: per-table fingerprint of import-owned
        tables only (used by the drift-recheck inside promote_database so that
        runtime-table edits do NOT trigger a spurious abort).
    """
    return {
        "version": 1,
        "created_at": utc_now(),
        "target_db_path": str(paths.db_path),
        "staging_db_path": str(staging_db),
        "base_db_path": str(base_db),
        "pdf_dir": str(paths.pdf_dir),
        "processed_dir": str(paths.processed_dir),
        "csv_root": str(csv_root),
        "approved_csv_paths": [],
        "account_slugs": {},
        "target_db_fingerprint_before": target_db_fingerprint_before,
        "target_db_import_owned_before": target_db_import_owned_before,
        "target_db_before": target_db_before,
        "staging_db_before": staging_db_before,
    }


def stage(args: argparse.Namespace, paths: Paths) -> None:
    # Retain the per-run lock across extraction and every terminal state write,
    # while the target lock below remains limited to baseline+registration.
    with contextlib.ExitStack() as stage_lifetime:
        _stage_with_lifetime(args, paths, stage_lifetime)


def _stage_with_lifetime(
    args: argparse.Namespace,
    paths: Paths,
    stage_lifetime: contextlib.ExitStack,
) -> None:
    mapped_source = getattr(args, "csv_source", None)
    source_kind = "mapped_csv" if mapped_source is not None else "pdf"
    pdfs = (
        [Path(mapped_source)]
        if mapped_source is not None
        else sorted(path for path in paths.pdf_dir.glob("*.pdf") if path.is_file())
    )
    if not pdfs:
        print(f"No PDFs found in {paths.pdf_dir}")
        return

    # Capture interpretation before any parser executes. It must still match after
    # staging and immediately before publication, including the original sources.
    interpretation = snapshot_bindings()
    config_paths = [paths.ledger_config, paths.merchant_rules, paths.categories]
    configuration = configuration_bindings(config_paths)
    artifacts = {str(path): file_binding(path) for path in pdfs}
    if mapped_source is not None:
        artifacts[str(args.csv_profile)] = file_binding(Path(args.csv_profile))

    with live_mutation_boundary(paths.db_path, operation="pdf_stage"):
        target_db_fingerprint_before = database_fingerprint(paths.db_path)
        # Capture the import-owned baseline and clone while holding the same target
        # exclusion boundary as every conflicting live writer. Register a provisional
        # promotable run before release so no writer can pass between snapshot and
        # durable coordination publication.
        target_db_import_owned_before = import_owned_fingerprint(paths.db_path)
        run_dir = make_run_dir(paths.runs_dir)
        stage_lifetime.enter_context(finalize_run_lock(paths.runs_dir, run_dir))
        staging_db = run_dir / "staging.db"
        base_db = paths.base_db_path or paths.db_path
        clone_database(base_db, staging_db)
        run_migrations(staging_db)
        ensure_review_schema(staging_db)

        target_db_before = db_counts(paths.db_path) if paths.db_path.exists() else {}
        staging_db_before = db_counts(staging_db)
        known_hashes = known_pdf_hashes(staging_db)
        provisional_csv_root = run_dir / "csv"
        write_run_state(
            run_dir,
            _base_run_state(
                paths=paths,
                staging_db=staging_db,
                base_db=base_db,
                csv_root=provisional_csv_root,
                target_db_fingerprint_before=target_db_fingerprint_before,
                target_db_import_owned_before=target_db_import_owned_before,
                target_db_before=target_db_before,
                staging_db_before=staging_db_before,
            )
            | {"status": "staging"},
        )
    new_pdfs: list[dict[str, Any]] = []
    duplicate_pdfs: list[dict[str, Any]] = []

    for pdf_path in pdfs:
        file_hash = artifacts[str(pdf_path)]["sha256"]
        item: dict[str, Any] = {
            "original_pdf": str(pdf_path.resolve()),
            "file_hash": file_hash,
        }
        # Raw CSV bytes do not identify their explicit account/profile binding.
        # Mapped exports deduplicate by canonical financial row identity instead.
        if source_kind == "pdf" and file_hash in known_hashes:
            item["skipped_reason"] = "duplicate_pdf_hash"
            duplicate_pdfs.append(item)
        else:
            new_pdfs.append(item)

    staging_inbox = run_dir / "inbox"
    csv_root = run_dir / "csv"
    extraction_manifest = run_dir / "extraction-manifest.json"
    parse_exceptions = run_dir / "parse-exceptions.json"
    validation_report = run_dir / "statement-validation.json"
    merchant_review_json = run_dir / "merchant-review.json"
    merchant_review_md = run_dir / "merchant-review.md"

    staged_by_name: dict[str, dict[str, Any]] = {}
    for item in new_pdfs:
        original = Path(item["original_pdf"])
        ensure_private_dir(staging_inbox)
        staged_pdf = staging_inbox / original.name
        copy_private_file(original, staged_pdf)
        if file_binding(staged_pdf) != artifacts[str(original)]:
            raise ValueError("import_binding_changed")
        artifacts[str(staged_pdf)] = file_binding(staged_pdf)
        item["staged_pdf"] = str(staged_pdf)
        staged_by_name[staged_pdf.name] = item

    if not new_pdfs:
        run_state = _base_run_state(
            paths=paths,
            staging_db=staging_db,
            base_db=base_db,
            csv_root=csv_root,
            target_db_fingerprint_before=target_db_fingerprint_before,
            target_db_import_owned_before=target_db_import_owned_before,
            target_db_before=target_db_before,
            staging_db_before=staging_db_before,
        ) | {
            "status": "cancelled",
            "cancel_reason": "no_new_pdfs",
            "duplicate_pdfs": duplicate_pdfs,
            "staging_db_after": db_counts(staging_db),
        }
        write_run_state(run_dir, run_state)
        print(f"No new PDFs to import. Run state: {run_dir / 'run.json'}")
        return

    extraction_results = []
    for item in new_pdfs:
        staged_pdf = Path(item["staged_pdf"])
        try:
            if mapped_source is not None:
                from ledger.adapters.mapped_export import extract_mapped_export

                extracted = extract_mapped_export(
                    staged_pdf,
                    Path(args.csv_profile),
                    csv_root,
                    paths.processed_dir,
                    institution=args.institution,
                    account=args.account,
                    account_type=args.account_type,
                )
            else:
                extracted = statement_pdf_to_csv.process_pdf(
                    staged_pdf,
                    csv_root,
                    paths.processed_dir,
                    dry_run=False,
                    archive_pdf=False,
                )
        except Exception as exc:
            extracted = statement_pdf_to_csv.ExtractedCsv(
                pdf=staged_pdf,
                csv_path=None,
                processed_pdf=None,
                statement_type="parser_exception",
                transaction_count=0,
                skipped_reason="parser_exception",
                validation_errors=(f"parser_exception:{type(exc).__name__}",),
            )
        extraction_results.append(extracted)
    statement_pdf_to_csv.write_manifest(extraction_manifest, extraction_results, dry_run=False)
    extraction_by_pdf = {
        entry["pdf"]: entry
        for entry in read_json(extraction_manifest)
        if isinstance(entry, dict) and isinstance(entry.get("pdf"), str)
    }

    for extraction in extraction_results:
        item = staged_by_name[extraction.pdf.name]
        no_activity = is_no_activity_statement(extraction_by_pdf.get(str(extraction.pdf), {}))
        item.update(
            {
                "csv_path": str(extraction.csv_path) if extraction.csv_path else "",
                "processed_pdf": str(extraction.processed_pdf) if extraction.processed_pdf else "",
                "statement_type": extraction.statement_type,
                "transaction_count": extraction.transaction_count,
                "skipped_reason": "" if no_activity else extraction.skipped_reason,
                "no_activity": no_activity,
                "validation_errors": list(extraction.validation_errors),
                "validation_warnings": list(extraction.validation_warnings),
                "account_slug": getattr(extraction, "account_slug", ""),
            }
        )

    validation_items = [
        {
            "pdf": str(result.pdf),
            "statement_type": result.statement_type,
            "errors": list(result.validation_errors),
            "warnings": list(result.validation_warnings),
        }
        for result in extraction_results
        if result.validation_errors or result.validation_warnings
    ]
    write_json(validation_report, validation_items)
    blocking_validation_errors = [item for item in validation_items if item["errors"]]

    supported_pdfs = [item for item in new_pdfs if not item.get("skipped_reason")]
    if not supported_pdfs:
        run_state = _base_run_state(
            paths=paths,
            staging_db=staging_db,
            base_db=base_db,
            csv_root=csv_root,
            target_db_fingerprint_before=target_db_fingerprint_before,
            target_db_import_owned_before=target_db_import_owned_before,
            target_db_before=target_db_before,
            staging_db_before=staging_db_before,
        ) | {
            "status": "cancelled",
            "cancel_reason": "no_supported_pdfs",
            "pdfs": new_pdfs,
            "duplicate_pdfs": duplicate_pdfs,
            "extraction_manifest": str(extraction_manifest),
            "statement_validation": str(validation_report),
            "staging_db_after": db_counts(staging_db),
        }
        write_run_state(run_dir, run_state)
        print(f"No supported PDFs imported. Run state: {run_dir / 'run.json'}")
        if mapped_source is not None:
            raise SystemExit("mapped_csv_validation_failed")
        return

    # Partition: a PDF with blocking validation errors becomes a per-PDF skip
    # (mirrors the unsupported_statement skip) instead of aborting the batch.
    # We only fail the whole run if NOTHING remains importable.
    failed_pdf_names = {Path(str(item["pdf"])).name for item in blocking_validation_errors}
    for item in new_pdfs:
        staged_name = Path(item.get("staged_pdf", "")).name
        if staged_name in failed_pdf_names and not item.get("skipped_reason"):
            item["skipped_reason"] = "statement_validation_failed"
            item["next_actions"] = next_actions_for("failed")

    # process_pdf writes a CSV for any statement that parsed rows, including one that
    # later fails reconciliation. Removing those skipped artifacts keeps the run tidy,
    # but the explicit approved_csv_paths list below remains the correctness boundary
    # if an unlink fails.
    remove_skipped_csvs(new_pdfs)

    importable_pdfs = [item for item in new_pdfs if not item.get("skipped_reason")]
    if not importable_pdfs:
        run_state = _base_run_state(
            paths=paths,
            staging_db=staging_db,
            base_db=base_db,
            csv_root=csv_root,
            target_db_fingerprint_before=target_db_fingerprint_before,
            target_db_import_owned_before=target_db_import_owned_before,
            target_db_before=target_db_before,
            staging_db_before=staging_db_before,
        ) | {
            "status": "failed",
            "failure_reason": "statement_validation_failed",
            "staging_inbox": str(staging_inbox),
            "pdfs": new_pdfs,
            "duplicate_pdfs": duplicate_pdfs,
            "extraction_manifest": str(extraction_manifest),
            "statement_validation": str(validation_report),
        }
        write_run_state(run_dir, run_state)
        raise SystemExit(
            f"Statement validation failed for all {len(blocking_validation_errors)} PDFs: {validation_report}"
        )

    approved_csv_paths = _approved_csv_paths_for_stage(importable_pdfs, csv_root)
    account_slugs = _account_slugs_for_stage(importable_pdfs, csv_root, approved_csv_paths)
    artifacts[str(extraction_manifest)] = file_binding(extraction_manifest)
    import_binding = bind_import_inputs(
        csv_root=csv_root,
        source_files=approved_csv_paths,
        config_paths=config_paths,
        account_slugs=account_slugs,
        interpretation=interpretation,
        configuration=configuration,
        artifacts=artifacts,
    )
    verify_import_inputs(
        import_binding,
        csv_root=csv_root,
        source_files=approved_csv_paths,
        account_slugs=account_slugs,
        config_paths=config_paths,
    )
    result = None
    try:
        config = load_config(paths, staging_db)
        result = process(
            config=config,
            source_root=csv_root,
            include_raw=False,
            source_files=approved_csv_paths,
            account_slugs=account_slugs,
            defer_identity_check_to_writer=True,
        )
        if source_kind == "mapped_csv":
            # Banking joins use one globally unique account slug. An explicit
            # mapping cannot reassign an existing slug to another institution.
            with session_scope(staging_db) as session:
                for institution, account in {
                    (row.institution, row.account)
                    for row in (*result.transactions, *result.transfers)
                }:
                    for model, account_column in (
                        (AccountDB, AccountDB.account_number),
                        (TransactionDB, TransactionDB.account),
                        (TransferDB, TransferDB.account),
                    ):
                        if (
                            session.query(model.institution)
                            .filter(account_column == account, model.institution != institution)
                            .first()
                            is not None
                        ):
                            raise AdapterError("import_account_conflict")
        if result.total_scanned == 0 and no_activity_only_import(
            extraction_manifest, approved_csv_paths
        ):
            validate_processing_result(result, parse_exceptions, allow_no_activity=True)
        else:
            validate_processing_result(result, parse_exceptions)
        verify_import_inputs(
            import_binding,
            csv_root=csv_root,
            source_files=approved_csv_paths,
            account_slugs=account_slugs,
            config_paths=config_paths,
        )
        write_result_to_db(staging_db, csv_root, result)
    except SystemExit:
        # Persist state before re-raising so status/cancel and latest-run.txt can
        # resolve and describe the failed run (mirrors finalize()'s rationale).
        failed_state: dict[str, Any] = _base_run_state(
            paths=paths,
            staging_db=staging_db,
            base_db=base_db,
            csv_root=csv_root,
            target_db_fingerprint_before=target_db_fingerprint_before,
            target_db_import_owned_before=target_db_import_owned_before,
            target_db_before=target_db_before,
            staging_db_before=staging_db_before,
        ) | {
            "status": "failed",
            "failure_reason": "processing_validation_failed",
            "staging_inbox": str(staging_inbox),
            "pdfs": new_pdfs,
            "duplicate_pdfs": duplicate_pdfs,
            "extraction_manifest": str(extraction_manifest),
            "statement_validation": str(validation_report),
            "approved_csv_paths": [str(path) for path in approved_csv_paths],
            "account_slugs": account_slugs,
        }
        if result is not None and result.exceptions:
            failed_state["parse_exceptions"] = str(parse_exceptions)
        write_run_state(run_dir, failed_state)
        raise
    except Exception as exc:
        failed_state = _base_run_state(
            paths=paths,
            staging_db=staging_db,
            base_db=base_db,
            csv_root=csv_root,
            target_db_fingerprint_before=target_db_fingerprint_before,
            target_db_import_owned_before=target_db_import_owned_before,
            target_db_before=target_db_before,
            staging_db_before=staging_db_before,
        ) | {
            "status": "failed",
            "failure_reason": "processing_exception",
            "failure_exception_type": type(exc).__name__,
            "staging_inbox": str(staging_inbox),
            "pdfs": new_pdfs,
            "duplicate_pdfs": duplicate_pdfs,
            "extraction_manifest": str(extraction_manifest),
            "statement_validation": str(validation_report),
            "approved_csv_paths": [str(path) for path in approved_csv_paths],
            "account_slugs": account_slugs,
        }
        if result is not None and result.exceptions:
            failed_state["parse_exceptions"] = str(parse_exceptions)
        write_run_state(run_dir, failed_state)
        raise
    candidates = build_merchant_review(result)
    seed_review_candidates(staging_db, candidates)
    write_json(merchant_review_json, candidates)
    write_merchant_review_markdown(merchant_review_md, candidates)

    pending_after_stage = pending_review_count(staging_db)
    run_status = "needs_review" if pending_after_stage else "ready_to_finalize"
    run_state = _base_run_state(
        paths=paths,
        staging_db=staging_db,
        base_db=base_db,
        csv_root=csv_root,
        target_db_fingerprint_before=target_db_fingerprint_before,
        target_db_import_owned_before=target_db_import_owned_before,
        target_db_before=target_db_before,
        staging_db_before=staging_db_before,
    ) | {
        "status": run_status,
        "source_kind": source_kind,
        "import_binding": import_binding,
        "staging_inbox": str(staging_inbox),
        "pdfs": new_pdfs,
        "duplicate_pdfs": duplicate_pdfs,
        "extraction_manifest": str(extraction_manifest),
        "merchant_review_json": str(merchant_review_json),
        "merchant_review_markdown": str(merchant_review_md),
        "parse_exceptions": str(parse_exceptions),
        "statement_validation": str(validation_report),
        "approved_csv_paths": [str(path) for path in approved_csv_paths],
        "account_slugs": account_slugs,
        "staging_db_after": db_counts(staging_db),
        "stage_counts": result_counts(result),
        "pending_review_count": pending_after_stage,
    }
    write_run_state(run_dir, run_state)
    record_db_operation(
        staging_db,
        operation="import_stage",
        result=RunResult(
            status="success",
            started_at=operation_utc_now(),
            finished_at=operation_utc_now(),
            counts=result_counts(result),
            artifacts={"run_dir": run_dir, "manifest": run_dir / "manifest.json"},
            details={"run_status": run_status},
        ),
    )

    print(f"Staged {len(supported_pdfs)} PDFs into {staging_db}")
    print(f"Target DB is unchanged: {paths.db_path}")
    print(f"Rows scanned: {result.total_scanned}")
    print(f"Transactions: {len(result.transactions)}; transfers: {len(result.transfers)}")
    print(f"Merchant review candidates: {len(candidates)}")
    print(f"Statement validation: {validation_report}")
    print(f"Run dir: {run_dir}")
    print(f"Merchant review: {merchant_review_md}")
    print("After review is resolved, run:")
    print(f"  ./scripts/import-pdf-inbox finalize --run-dir {run_dir}")


def _import_owned_unchanged(run_state: dict[str, Any], target_db: Path) -> bool:
    """Return True iff the import-owned tables on *target_db* match the stage-time snapshot.

    Uses ``target_db_import_owned_before`` from run_state when present (new runs).
    Falls back to the whole-file ``target_db_unchanged`` for runs staged before this
    feature was deployed (backward-compat: those runs only have the whole-file key).
    """
    import_owned_before = run_state.get("target_db_import_owned_before")
    if import_owned_before is not None:
        current = import_owned_fingerprint(target_db)
        return import_owned_match(import_owned_before, current)
    # Fallback: no import-owned snapshot → use whole-file fingerprint.
    return target_db_unchanged(run_state)


def _fail_if_target_db_changed(run_dir: Path, run_state: dict[str, Any], target_db: Path) -> None:
    """Abort finalize if the live target DB has drifted on IMPORT-OWNED tables.

    User-owned runtime-table edits (budgets, savings goals, spending preferences,
    planned schedules, config metadata, and account lifecycle) are explicitly
    excluded from this check. They are merged forward during promote and must not
    cause a spurious abort.

    The whole-file fingerprint is KEPT in run_state for the UI readiness gate
    (build_import_readiness / target_db_unchanged) and is not touched here.
    """
    if _import_owned_unchanged(run_state, target_db):
        return

    clear_publication_protocol(run_state)
    run_state.update(
        {
            "status": "failed",
            "failure_reason": "target_db_changed_since_stage",
            "last_finalize_attempt_at": utc_now(),
            "target_db_fingerprint_current": database_fingerprint(target_db),
        }
    )
    write_run_state(run_dir, run_state)
    raise SystemExit("Target database changed since stage; cancel and restage this import.")


class _FinalizeRetryNeededError(Exception):
    """Private sentinel raised by _finalize_recover_from_finalizing when the run
    was at 'finalizing' but promote had not yet happened.  The caller (finalize())
    catches this and re-runs the full finalize logic with the updated on-disk state."""


def _finalize_idempotency_key(run_dir: Path) -> str:
    """Return the stable audit identity for one import run."""
    return stable_id("import_finalize", str(run_dir.resolve()))


def _read_final_report(path: Path) -> dict[str, Any]:
    try:
        payload = read_json(path)
    except FileNotFoundError, json.JSONDecodeError, OSError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _brief_since(run_state: dict[str, Any]) -> datetime | None:
    """Return the run start as a naive UTC timestamp for change queries."""
    raw = run_state.get("created_at")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


def _finalize_tail_from_state(run_dir: Path, run_state: dict[str, Any]) -> dict[str, Any]:
    """Return durable tail inputs, reconstructing legacy state where possible."""
    stored = run_state.get("finalize_tail")
    if isinstance(stored, dict):
        tail = dict(stored)
    else:
        final_report = Path(run_state.get("final_report") or run_dir / "final-report.json")
        previous_report = _read_final_report(final_report)
        tail = {
            "idempotency_key": _finalize_idempotency_key(run_dir),
            "final_report": str(final_report),
            "target_db_backup": run_state.get("target_db_backup", ""),
            "counts": run_state.get("final_counts")
            or previous_report.get("counts")
            or run_state.get("stage_counts")
            or {},
            "post_processing": run_state.get("post_processing")
            or previous_report.get("post_processing")
            or {},
            "merchant_summary": run_state.get("merchant_summary")
            or previous_report.get("merchant_summary")
            or {},
            "review_rows": run_state.get("review_rows") or previous_report.get("review_rows") or [],
            "processed_pdfs": list(run_state.get("pdfs") or []),
            "financial_brief": run_state.get("financial_brief")
            or previous_report.get("financial_brief"),
        }

    tail.setdefault("idempotency_key", _finalize_idempotency_key(run_dir))
    tail.setdefault("final_report", str(run_dir / "final-report.json"))
    tail.setdefault("target_db_backup", run_state.get("target_db_backup", ""))
    tail.setdefault("counts", run_state.get("final_counts") or run_state.get("stage_counts") or {})
    tail.setdefault("post_processing", run_state.get("post_processing") or {})
    tail.setdefault("merchant_summary", run_state.get("merchant_summary") or {})
    tail.setdefault("review_rows", run_state.get("review_rows") or [])
    tail.setdefault("processed_pdfs", list(run_state.get("pdfs") or []))
    tail.setdefault("financial_brief", run_state.get("financial_brief"))
    return tail


def _managed_archive_path(
    value: object,
    root: Path,
    *,
    outside_warning: str,
    symlink_warning: str,
) -> tuple[Path | None, str | None]:
    """Return one lexical child of *root* without following symlink components."""
    root = root.expanduser().resolve()
    candidate = Path(os.path.abspath(Path(str(value)).expanduser()))
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return None, outside_warning
    if relative == Path("."):
        return None, outside_warning

    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            return None, symlink_warning
    return candidate, None


def _matching_archived_pdf(
    target: Path,
    expected_hash: str,
    processed_root: Path,
) -> tuple[Path | None, str | None]:
    """Find a verified prior publication without trusting archive candidates."""
    if len(expected_hash) != 64 or any(char not in "0123456789abcdef" for char in expected_hash):
        return None, None
    candidates = [target]
    candidates.extend(
        target.with_name(f"{target.stem}-{index}{target.suffix}") for index in range(2, 1000)
    )
    for raw_candidate in candidates:
        candidate, warning = _managed_archive_path(
            raw_candidate,
            processed_root,
            outside_warning="archive_target_outside_processed",
            symlink_warning="archive_candidate_symlink",
        )
        if warning:
            return None, warning
        assert candidate is not None
        if not candidate.is_file():
            continue
        try:
            candidate_hash = sha256_file(candidate)
        except OSError:
            return None, "archive_candidate_unreadable"
        if candidate_hash == expected_hash:
            return candidate, None
    return None, None


def _archive_finalize_pdfs(
    pdf_items: list[dict[str, Any]],
    *,
    inbox_root: Path,
    processed_root: Path,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Best-effort, idempotently archive PDFs and return a redacted outcome."""
    finalized = [dict(item) for item in pdf_items]
    attempted = 0
    archived = 0
    warnings: list[str] = []
    for item in finalized:
        if item.get("skipped_reason"):
            continue
        original_value = item.get("original_pdf")
        target_value = item.get("processed_pdf")
        if not original_value or not target_value:
            item["finalize_warning"] = "archive_paths_missing"
            warnings.append("archive_paths_missing")
            continue
        original, path_warning = _managed_archive_path(
            original_value,
            inbox_root,
            outside_warning="archive_source_outside_inbox",
            symlink_warning="archive_source_symlink",
        )
        if path_warning:
            item["finalize_warning"] = path_warning
            warnings.append(path_warning)
            continue
        target, path_warning = _managed_archive_path(
            target_value,
            processed_root,
            outside_warning="archive_target_outside_processed",
            symlink_warning="archive_candidate_symlink",
        )
        if path_warning:
            item["finalize_warning"] = path_warning
            warnings.append(path_warning)
            continue
        assert original is not None
        assert target is not None

        expected_hash = str(item.get("file_hash") or "").lower()
        recovered, candidate_warning = _matching_archived_pdf(
            target,
            expected_hash,
            processed_root,
        )
        if candidate_warning:
            item["finalize_warning"] = candidate_warning
            warnings.append(candidate_warning)
            continue
        if original.exists():
            attempted += 1
            try:
                if expected_hash and sha256_file(original) != expected_hash:
                    item["finalize_warning"] = "archive_source_hash_changed"
                    warnings.append("archive_source_hash_changed")
                    continue

                if recovered is not None:
                    tighten_private_file(recovered)
                    if original.resolve() != recovered.resolve():
                        original.unlink()
                    final_path = recovered
                else:
                    final_path = statement_pdf_to_csv.move_pdf(
                        original,
                        target,
                        dry_run=False,
                    )
                item["processed_pdf"] = str(final_path.resolve())
                item.pop("finalize_warning", None)
                archived += 1
            except Exception:
                item["finalize_warning"] = "archive_move_failed"
                warnings.append("archive_move_failed")
                sys.stderr.write(
                    f"WARNING: could not archive {original} -> {target_value}:"
                    " PDF remains in inbox (hash already promoted; archive it manually).\n"
                )
            continue

        if recovered is not None:
            item["processed_pdf"] = str(recovered.resolve())
            item.pop("finalize_warning", None)
            archived += 1
        else:
            item["finalize_warning"] = "archive_source_missing"
            warnings.append("archive_source_missing")

    outcome = {
        "status": "warning" if warnings else "success",
        "attempted": attempted,
        "archived": archived,
        "warnings": warnings,
    }
    return finalized, outcome


def _record_finalize_audit_once(
    target_db: Path,
    run_dir: Path,
    tail: dict[str, Any],
    archive_outcome: dict[str, Any],
) -> None:
    """Insert one finalize audit per stable run key in a serialized transaction."""
    key = str(tail["idempotency_key"])
    backup = str(tail.get("target_db_backup") or "")
    with session_scope(target_db) as session:
        # Serialize the check/insert pair. A state-write retry after commit will see
        # the existing key and cannot append a duplicate audit row.
        session.execute(text("BEGIN IMMEDIATE"))
        existing = (
            session.query(DBOperationAuditDB)
            .filter(DBOperationAuditDB.operation == "import_finalize")
            .all()
        )
        for row in existing:
            details = row.details or {}
            if details.get("idempotency_key") == key:
                return
            # Legacy finalize audits predate the explicit key. Their unique rollback
            # backup is the strongest available run identity.
            if backup and details.get("backup") == backup:
                return

        now = operation_utc_now()
        session.add(
            DBOperationAuditDB(
                operation="import_finalize",
                status="success",
                started_at=now,
                finished_at=now,
                db_path=str(target_db),
                artifact_path=str(run_dir / "manifest.json"),
                counts=dict(tail.get("counts") or {}),
                warnings=list(archive_outcome.get("warnings") or []),
                errors=[],
                details={
                    "idempotency_key": key,
                    "run_dir": str(run_dir),
                    "backup": backup,
                },
            )
        )
        # The finalize's own report snapshot was written during post-processing,
        # BEFORE this audit row — so the freshness gate in snapshot_for_period
        # (created_at >= newest mutating op) judged the snapshot that this very
        # finalize produced as stale, forever, and every later page render
        # recomputed transiently. The snapshot is by construction post-mutation:
        # stamp it with the same instant as the audit row so it passes the gate
        # it was always meant to pass.
        finalize_snapshot = (
            session.query(ReportSnapshotDB)
            .filter(ReportSnapshotDB.source == "pdf_finalize")
            .order_by(ReportSnapshotDB.created_at.desc())
            .first()
        )
        if finalize_snapshot is not None:
            finalize_snapshot.created_at = now


def _cleanup_finalize_staging(run_dir: Path, run_state: dict[str, Any]) -> None:
    for key in ("csv_root", "staging_inbox"):
        value = run_state.get(key)
        if not value:
            continue
        path, warning = _managed_archive_path(
            value,
            run_dir,
            outside_warning="cleanup_path_outside_run",
            symlink_warning="cleanup_path_symlink",
        )
        if warning or path is None:
            raise ValueError(
                f"Finalize cleanup path for {key} must be a contained child of its import run."
            )
        if not path.exists():
            continue
        if not path.is_dir():
            raise ValueError(f"Finalize cleanup path for {key} must be a directory.")

        # Revalidate immediately before the destructive call so a persisted path
        # cannot escape through a symlink introduced after its first validation.
        revalidated, warning = _managed_archive_path(
            path,
            run_dir,
            outside_warning="cleanup_path_outside_run",
            symlink_warning="cleanup_path_symlink",
        )
        if warning or revalidated != path:
            raise ValueError(
                f"Finalize cleanup path for {key} must be a contained child of its import run."
            )
        shutil.rmtree(path)


def _complete_finalize_tail(
    run_dir: Path,
    run_state: dict[str, Any],
    paths: Paths,
) -> None:
    """Idempotently complete every post-promote artifact before final status."""
    target_db = Path(run_state["target_db_path"])
    staging_db = Path(run_state.get("staging_db_path", ""))
    tail = _finalize_tail_from_state(run_dir, run_state)
    run_state["finalize_tail"] = tail
    if run_state.get("status") == "finalized":
        # Entering the idempotent tail demotes the terminal status so a crash
        # mid-tail is visibly incomplete — but a run only ever reads "finalized"
        # because its promote completed, and pre-protocol legacy runs carry no
        # durable publication proof. Without the marker, one interruption in the
        # tail (a Ctrl-C during PDF re-hashing, a BUSY on the audit insert) left
        # status "finalizing" with no proof, and _run_can_still_promote then
        # refused every live write until manual run.json surgery. Stamp the
        # proof the run's own terminal status already establishes, in the same
        # durable write as the demotion.
        run_state["publication_completed"] = True
        run_state["status"] = "finalizing"
        write_run_state(run_dir, run_state)

    # Finalize is commonly invoked later with only --run-dir. In particular, a
    # fresh rerun stages from and retains PDFs in the processed directory, while
    # the later command's Paths object falls back to the default inbox. Preserve
    # the roots selected at stage time; current CLI roots are only for legacy run
    # states created before these fields were persisted.
    archive_inbox_root = Path(str(run_state.get("pdf_dir") or paths.pdf_dir))
    archive_processed_root = Path(str(run_state.get("processed_dir") or paths.processed_dir))
    finalized_pdfs, archive_outcome = _archive_finalize_pdfs(
        list(tail.get("processed_pdfs") or []),
        inbox_root=archive_inbox_root,
        processed_root=archive_processed_root,
    )
    tail["processed_pdfs"] = finalized_pdfs
    run_state.update(
        {
            "status": "finalizing",
            "pdfs": finalized_pdfs,
            "archive_outcome": archive_outcome,
            "finalize_tail": tail,
        }
    )
    # This state write deliberately precedes the report/audit/final marker. A crash
    # after a PDF move can therefore be recovered without another promotion.
    write_run_state(run_dir, run_state)

    _cleanup_finalize_staging(run_dir, run_state)
    financial_brief = tail.get("financial_brief")
    if not isinstance(financial_brief, dict):
        financial_brief = FinancialBriefService(target_db).build_default(
            since=_brief_since(run_state)
        )
        tail["financial_brief"] = financial_brief
        run_state["financial_brief"] = financial_brief
        run_state["finalize_tail"] = tail
        # Persist the deterministic brief before the presentation report and
        # terminal marker. Recovery reuses it and therefore cannot drift or
        # publish twice after an interruption later in the tail.
        write_run_state(run_dir, run_state)
    final_report = Path(str(tail["final_report"]))
    write_json(
        final_report,
        {
            "db_path": str(target_db),
            "staging_db_path": str(staging_db),
            "target_db_backup": str(tail.get("target_db_backup") or ""),
            "counts": dict(tail.get("counts") or {}),
            "post_processing": dict(tail.get("post_processing") or {}),
            "merchant_summary": dict(tail.get("merchant_summary") or {}),
            "review_rows": list(tail.get("review_rows") or []),
            "processed_pdfs": finalized_pdfs,
            "financial_brief": financial_brief,
        },
    )
    _record_finalize_audit_once(target_db, run_dir, tail, archive_outcome)

    run_state.update(
        {
            "status": "finalized",
            "finalized_at": run_state.get("finalized_at") or utc_now(),
            "pdfs": finalized_pdfs,
            "final_counts": dict(tail.get("counts") or {}),
            "post_processing": dict(tail.get("post_processing") or {}),
            "merchant_summary": dict(tail.get("merchant_summary") or {}),
            "target_db_backup": str(tail.get("target_db_backup") or ""),
            "target_db_after_promote": db_counts(target_db),
            "review_rows": list(tail.get("review_rows") or []),
            "final_report": str(final_report),
            "archive_outcome": archive_outcome,
            "financial_brief": financial_brief,
            "finalize_tail": tail,
        }
    )
    write_run_state(run_dir, run_state)
    clear_latest_run_file(paths.runs_dir, run_dir)


def _finalize_recover_from_finalizing(
    run_dir: Path,
    run_state: dict[str, Any],
    paths: Paths,
) -> None:
    """Recovery handler for runs wedged at status='finalizing' after a process kill.

    Normal publication persists an import-owned candidate fingerprint before promote
    and a completion marker immediately afterward under the same target lock. If the
    marker write was interrupted, recovery pins the exact staging and target files,
    validates staging against that provenance, and uses WAL-aware logical fingerprints
    to decide whether publication happened. A positive proof is converted to the durable
    marker before the lock is released. A negative proof retries normal finalize; missing,
    corrupt, or unsafe proof fails closed without exposing private paths.
    """
    if (
        run_state.get("finalize_protocol_version") in {1, PUBLICATION_PROTOCOL_VERSION}
        and run_state.get("publication_phase") == "preparing"
        and "publication_candidate_import_owned" not in run_state
        and not run_publication_completed(run_state)
    ):
        clear_publication_protocol(run_state)
        run_state.update(
            {
                "status": "ready_to_finalize",
                "last_finalize_attempt_at": utc_now(),
                "recovery_note": "retry_current_protocol_before_candidate",
            }
        )
        run_state.pop("failure_reason", None)
        write_run_state(run_dir, run_state)
        raise _FinalizeRetryNeededError()

    target_db = Path(str(run_state.get("target_db_path", "")))
    try:
        with database_target_lock(target_db):
            if run_publication_completed(run_state):
                if run_state.get("finalize_protocol_version") in {
                    1,
                    PUBLICATION_PROTOCOL_VERSION,
                }:
                    verify_published_marker(run_state)
                promote_happened = True
            else:

                def persist_recovered_publication(target_identity: dict[str, int]) -> None:
                    run_state["publication_completed"] = True
                    if run_state.get("finalize_protocol_version") in {
                        1,
                        PUBLICATION_PROTOCOL_VERSION,
                    }:
                        run_state["publication_phase"] = "published"
                    if publication_protocol_is_tokenized(run_state):
                        run_state["publication_target_identity"] = target_identity
                    write_run_state(run_dir, run_state)

                promote_happened = recover_publication_completed(
                    run_dir,
                    run_state,
                    on_published=persist_recovered_publication,
                )
    except Exception:
        raise SystemExit("Publication recovery proof is unavailable.") from None

    if not promote_happened:
        # Promote has NOT happened yet (import-owned content of target does not match
        # staging), OR staging_db is missing so we cannot confirm the promote.
        # Reset to 'ready_to_finalize' and raise a sentinel so finalize() re-dispatches
        # cleanly through the normal full finalize path (which includes
        # _fail_if_target_db_changed to guard against out-of-band drift).
        print(
            "RECOVERY: run needs post-promote recovery but promote has NOT happened yet "
            "(import-owned fingerprint of live target does not match staging). "
            "Resetting to 'ready_to_finalize' and retrying the full finalize path."
        )
        run_state.update(
            {
                "status": "ready_to_finalize",
                "last_finalize_attempt_at": utc_now(),
                "recovery_note": "reset_from_finalizing_pre_promote",
            }
        )
        clear_publication_protocol(run_state)
        write_run_state(run_dir, run_state)
        raise _FinalizeRetryNeededError()

    # ── Promote already happened — replay the idempotent tail only ──────────────
    print(
        "RECOVERY: promote has ALREADY happened "
        "(import-owned fingerprint of live target matches staging — promote confirmed). "
        "Resuming from post-promote tail (no re-promote)."
    )

    _complete_finalize_tail(run_dir, run_state, paths)

    print(f"Finalized run (recovered): {run_dir}")
    print(f"Kept import run manifest: {run_dir / 'manifest.json'}")


def finalize(args: argparse.Namespace, paths: Paths) -> None:
    run_dir = resolve_run_dir(paths.runs_dir, args.run_dir)
    try:
        with finalize_run_lock(paths.runs_dir, run_dir):
            _finalize_locked(args, paths, run_dir)
    except FinalizeLockError as exc:
        raise SystemExit(str(exc)) from None


def _finalize_locked(args: argparse.Namespace, paths: Paths, run_dir: Path) -> None:
    run_path = run_dir / "run.json"
    if not run_path.exists():
        raise SystemExit(f"Run state not found: {run_path}")

    run_state = read_json(run_path)
    try:
        validate_publication_protocol_state(run_state)
    except ValueError:
        raise SystemExit("Publication recovery state is invalid.") from None
    if run_state.get("status") not in {
        "staged",
        "needs_review",
        "ready_to_finalize",
        "failed",
        "finalize_failed",
        "finalizing",
        "finalized",
    }:
        raise SystemExit(f"Run is not staged: {run_state.get('status')}")
    if run_state.get("status") == "failed" and run_state.get("failure_reason") != "finalize_failed":
        raise SystemExit(
            f"Run cannot be finalized after failure: {run_state.get('failure_reason')}"
        )

    # ── 'finalizing' recovery: process-killed between promote and status='finalized' ──
    # If the process was killed (SIGKILL / power loss) AFTER promote_database returned
    # but BEFORE status='finalized' was written, run.json is stuck at 'finalizing'.
    # Python's except clauses never fire for a killed process, so P2-6's 'finalize_failed'
    # path did NOT run.  We must detect whether the promote already happened, then either:
    #   - replay ONLY the idempotent tail (final-report, P2-7 PDF moves, clear latest-run,
    #     status='finalized') WITHOUT re-promoting, or
    #   - fall through to the normal path if promote did not happen yet.
    # 'finalize_failed' (an except-caught post-promote error) needs the SAME
    # promote-already-happened detection: a failure in the tail AFTER a successful
    # promote leaves csv_root deleted, so the normal path below would SystemExit
    # "Staged CSV root not found" and wedge the run forever. Route it through the same
    # recovery: if promote happened, replay the idempotent tail (no re-promote, no
    # csv_root needed); if not, reset to ready_to_finalize and re-run the full path
    # (csv_root still exists in the pre-promote failure case).
    _status = run_state.get("status")
    if _status == "finalized":
        # Current protocol states retain proof of the exact published target.
        # Check it before the tail can demote status or write any artifacts.
        # Pre-protocol terminal states retain their existing legacy recovery path.
        if run_state.get("finalize_protocol_version") in {1, PUBLICATION_PROTOCOL_VERSION}:
            try:
                with database_target_lock(Path(run_state["target_db_path"])):
                    verify_published_marker(run_state)
            except Exception:
                raise SystemExit("Publication recovery proof is unavailable.") from None
        _complete_finalize_tail(run_dir, run_state, paths)
        print(f"Finalized run (verified/repaired): {run_dir}")
        print(f"Kept import run manifest: {run_dir / 'manifest.json'}")
        return
    if _status in {"finalizing", "finalize_failed"} or (
        _status == "failed" and run_state.get("failure_reason") == "finalize_failed"
    ):
        try:
            _finalize_recover_from_finalizing(run_dir, run_state, paths)
            return
        except _FinalizeRetryNeededError:
            # promote had NOT happened yet; _finalize_recover_from_finalizing reset status
            # to 'ready_to_finalize' and persisted run.json.  Re-invoke finalize() to run
            # the full path from the updated on-disk state.
            _finalize_locked(args, paths, run_dir)
            return

    csv_root = Path(run_state["csv_root"])
    if not csv_root.exists():
        raise SystemExit(f"Staged CSV root not found: {csv_root}")
    staging_db = Path(run_state["staging_db_path"])
    target_db = Path(run_state["target_db_path"])
    if not staging_db.exists():
        raise SystemExit(f"Staging DB not found: {staging_db}")
    approved_csv_paths = _approved_csv_paths_for_finalize(run_state, csv_root)
    run_state["approved_csv_paths"] = [str(path) for path in approved_csv_paths]
    has_persisted_account_slugs = "account_slugs" in run_state
    account_slugs = _account_slugs_for_finalize(run_state, csv_root, approved_csv_paths)
    if has_persisted_account_slugs:
        run_state["account_slugs"] = account_slugs
    verify_import_inputs(
        run_state.get("import_binding"),
        csv_root=csv_root,
        source_files=approved_csv_paths,
        account_slugs=account_slugs,
        config_paths=[paths.ledger_config, paths.merchant_rules, paths.categories],
    )
    _fail_if_target_db_changed(run_dir, run_state, target_db)

    clear_publication_protocol(run_state)
    run_state.pop("archive_outcome", None)
    run_state.pop("failure_reason", None)
    run_state.update(
        {
            "status": "finalizing",
            "last_finalize_attempt_at": utc_now(),
            "finalize_protocol_version": PUBLICATION_PROTOCOL_VERSION,
            "publication_phase": "preparing",
            "publication_token": secrets.token_hex(16),
        }
    )
    write_run_state(run_dir, run_state)
    try:
        config = load_config(paths, staging_db)
        parse_exceptions = run_dir / "finalize-parse-exceptions.json"
        final_report = run_dir / "final-report.json"
        result = process(
            config=config,
            source_root=csv_root,
            include_raw=False,
            source_files=approved_csv_paths,
            account_slugs=account_slugs,
            defer_identity_check_to_writer=True,
        )
        if result.total_scanned == 0 and no_activity_only_import(
            Path(run_state.get("extraction_manifest", "")), approved_csv_paths
        ):
            validate_processing_result(result, parse_exceptions, allow_no_activity=True)
        else:
            validate_processing_result(result, parse_exceptions)
        run_migrations(staging_db)
        write_result_to_db(staging_db, csv_root, result)

        stage_candidates = read_json(Path(run_state["merchant_review_json"]))
        latest_candidates = build_merchant_review(result)
        seed_review_candidates(staging_db, latest_candidates)
        reviewed_rows = get_review_rows(staging_db)
        apply_review_decisions(staging_db)

        pending = pending_review_count(staging_db)
        if pending:
            clear_publication_protocol(run_state)
            run_state.update(
                {
                    "status": "needs_review",
                    "last_finalize_attempt_at": utc_now(),
                    "pending_review_count": pending,
                }
            )
            write_run_state(run_dir, run_state)
            raise SystemExit(
                f"Manual review is not complete: {pending} pending review rows in staging DB {staging_db}"
            )

        # Persist each statement's PRINTED credit limit (from the extraction manifest)
        # into the staging DB BEFORE analysis so AccountDetector prefers the printed
        # figure over the deepest-balance estimate. Best-effort: a missing/partial
        # manifest just leaves the estimate behaviour unchanged.
        extraction_manifest_value = run_state.get("extraction_manifest", "")
        if extraction_manifest_value:
            # Normalize the persist key with the SAME account_aliases ingestion applies
            # to TransactionDB.account, so the stored slug matches the key
            # AccountDetector reads back with.  getattr chain: a stub/config without a
            # general.account_aliases legitimately means "no aliases".
            general = getattr(getattr(config, "ledger", None), "general", None)
            account_aliases = getattr(general, "account_aliases", None)
            persist_printed_credit_limits(
                staging_db,
                Path(extraction_manifest_value),
                account_aliases=account_aliases,
                source_files=approved_csv_paths,
            )
            # Record which account-months this run covered, INCLUDING the statements
            # finalize is about to drop for having produced no rows. Nothing else in
            # the staging DB remembers them, so without this a month with no activity
            # reads back as a month never imported. Must stay paired with the repair
            # call site in repair/statements.py: a table only finalize writes is
            # emptied by the next `repair statements` promote.
            persist_statement_coverage(
                staging_db,
                Path(extraction_manifest_value),
                account_aliases=account_aliases,
                source_files=approved_csv_paths,
            )

        post_processing = run_post_processing(staging_db, config)
        _fail_if_target_db_changed(run_dir, run_state, target_db)

        # Build the processed-PDF manifest WITHOUT moving anything yet. The PDFs
        # stay in the inbox until the atomic promote succeeds (H4): an irreversible
        # inbox->processed move before the last gate / promote would orphan the file
        # if the gate trips or the promote fails. Each item already carries
        # file_hash (computed at stage time from the original inbox file) and
        # processed_pdf (the INTENDED archive target); record_processed_files reads
        # those two fields directly and never touches the filesystem, so recording
        # the intended target now keeps double-import prevention promoting atomically
        # with the data even though the physical move happens later.
        finalized_pdfs: list[dict[str, Any]] = []
        for item in run_state.get("pdfs", []):
            if item.get("skipped_reason"):
                finalized_pdfs.append(item)
                continue
            original = Path(item["original_pdf"])
            target = Path(item["processed_pdf"])
            if not original.exists() and not target.exists():
                item["finalize_warning"] = "original_pdf_missing_and_processed_pdf_not_found"
            finalized_pdfs.append(item)

        record_processed_files(staging_db, finalized_pdfs)
        merchant_summary = summarize_final_merchants(stage_candidates, result)
        _fail_if_target_db_changed(run_dir, run_state, target_db)
        backup_label = (
            "pre-fresh-rerun"
            if Path(run_state.get("base_db_path", str(target_db))).resolve() != target_db.resolve()
            else "pre-finalize"
        )
        predeclared_backup = (
            paths.backup_dir / f"{target_db.stem}-{run_dir.name}-{backup_label}{target_db.suffix}"
            if target_db.exists()
            else None
        )
        tail = {
            "idempotency_key": _finalize_idempotency_key(run_dir),
            "final_report": str(final_report),
            "target_db_backup": str(predeclared_backup) if predeclared_backup else "",
            "counts": result_counts(result),
            "post_processing": post_processing,
            "merchant_summary": merchant_summary,
            "review_rows": reviewed_rows,
            "processed_pdfs": [dict(item) for item in finalized_pdfs],
        }
        candidate_fingerprint = publication_candidate_fingerprint(run_dir, run_state)
        run_state["finalize_tail"] = tail
        run_state["publication_candidate_import_owned"] = candidate_fingerprint
        run_state["publication_phase"] = "candidate_ready"
        write_run_state(run_dir, run_state)
        # Publication and its durable completion marker share one target lock.
        # `promote_database` cannot be called here because it acquires this
        # non-reentrant lock itself.
        with database_target_lock(target_db):

            def verify_interpretation_before_publication() -> None:
                verify_import_inputs(
                    run_state.get("import_binding"),
                    csv_root=csv_root,
                    source_files=approved_csv_paths,
                    account_slugs=account_slugs,
                    config_paths=[paths.ledger_config, paths.merchant_rules, paths.categories],
                )

            verify_interpretation_before_publication()
            current_candidate = publication_candidate_fingerprint(run_dir, run_state)
            if not import_owned_match(current_candidate, candidate_fingerprint):
                raise PublicationProofError("Publication recovery proof is unavailable") from None

            def persist_publication_proof(target_identity: dict[str, int]) -> None:
                run_state["publication_target_identity"] = target_identity
                run_state["publication_completed"] = True
                run_state["publication_phase"] = "published"
                write_run_state(run_dir, run_state)

            backup_path = _promote_database_locked(
                staging_db,
                target_db,
                paths.backup_dir,
                # Preferred: import-owned fingerprint — runtime-table edits
                # (budgets/goals/salt) are invisible to this check.
                expected_import_owned=run_state.get("target_db_import_owned_before"),
                expected_candidate_import_owned=candidate_fingerprint,
                expected_fingerprint=run_state.get("target_db_fingerprint_before"),
                expected_preserved_tables=None,
                label=backup_label,
                rollback_path=predeclared_backup,
                publication_token=str(run_state["publication_token"]),
                on_published=persist_publication_proof,
                prepublication_check=verify_interpretation_before_publication,
            )
            tail["target_db_backup"] = str(backup_path) if backup_path else ""
            run_state["finalize_tail"] = tail
    except SystemExit:
        # needs_review and _fail_if_target_db_changed paths already persisted their own
        # state; let them propagate as-is so the run remains recoverable via cancel/restage.
        # NOTE: SystemExit is a BaseException (not Exception) in Python 3, so the
        # `except Exception` below would NOT catch it — this explicit branch exists to
        # document that these intentional gate exits must never be marked finalize_failed.
        raise
    except Exception:
        run_state.update(
            {
                "status": "failed",
                "failure_reason": "finalize_failed",
                "last_finalize_attempt_at": utc_now(),
            }
        )
        write_run_state(run_dir, run_state)
        raise

    _complete_finalize_tail(run_dir, run_state, paths)

    print(f"Finalized run: {run_dir}")
    print(f"Rows scanned: {result.total_scanned}")
    print(f"Transactions: {len(result.transactions)}; transfers: {len(result.transfers)}")
    print(f"Resolved merchants: {len(merchant_summary['resolved_merchants'])}")
    print(f"Manual-review merchants: {len(merchant_summary['manual_review_merchants'])}")
    print_merchant_summary(merchant_summary)
    if backup_path:
        print(f"Backed up previous target DB: {backup_path}")
    print(f"Promoted staging DB to: {target_db}")

    print(f"Kept import run manifest: {run_dir / 'manifest.json'}")


def status(args: argparse.Namespace, paths: Paths) -> None:
    from ledger.imports.pdf.readiness import build_import_readiness

    run_dir = resolve_run_dir(paths.runs_dir, args.run_dir)
    try:
        manifest = read_manifest(run_dir)
    except FileNotFoundError, json.JSONDecodeError:
        # run.json missing or corrupt (e.g. stage crashed before first write).
        manifest = {
            "status": "unknown",
            "run_dir": str(run_dir),
            "note": "run.json is missing or corrupt; the run may have crashed before writing state",
        }
        print(json.dumps(manifest, indent=2))
        return
    with contextlib.suppress(FileNotFoundError, json.JSONDecodeError):
        manifest["readiness"] = build_import_readiness(run_dir)
    print(json.dumps(manifest, indent=2))


def cancel(args: argparse.Namespace, paths: Paths) -> None:
    run_dir = resolve_run_dir(paths.runs_dir, args.run_dir)
    try:
        with finalize_run_lock(paths.runs_dir, run_dir):
            _cancel_locked(args, paths, run_dir)
    except FinalizeLockError as exc:
        raise SystemExit(str(exc)) from None


def _cancel_locked(args: argparse.Namespace, paths: Paths, run_dir: Path) -> None:
    run_path = run_dir / "run.json"
    if not run_path.exists():
        raise SystemExit(f"Run state not found: {run_path}")
    run_state = read_json(run_path)
    status = run_state.get("status")
    if status == "finalized":
        raise SystemExit("Finalized import runs cannot be cancelled.")
    if status in {"finalizing", "finalize_failed"} or (
        status == "failed" and run_state.get("failure_reason") == "finalize_failed"
    ):
        raise SystemExit(
            "Finalize recovery states must be resumed with finalize and cannot be cancelled."
        )
    run_state.update(
        {
            "status": "cancelled",
            "cancelled_at": utc_now(),
            "cancel_reason": args.reason,
        }
    )
    clear_publication_protocol(run_state)
    write_run_state(run_dir, run_state)
    clear_latest_run_file(paths.runs_dir, run_dir)
    print(f"Cancelled import run: {run_dir}")
    print(f"Manifest: {run_dir / 'manifest.json'}")


# ── Review queue (staging-only) ───────────────────────────────────────────────
#
# The agent-facing counterpart of the Streamlit Review Queue page. Everything here
# operates on ONE import run's staging database and never on the live target: that is
# the surface's main advantage over the GUI, which retargets live without saying so.

REVIEW_STATUS_FILTERS: tuple[str, ...] = ("all", *REVIEW_STATUSES)


def _review_run_dir(run_dir: Path) -> Path:
    return Path(run_dir).expanduser().resolve()


def _review_runs_dir(runs_dir: Path | None) -> Path:
    return Path(runs_dir or runtime_path("import-runs")).expanduser().resolve()


def _validated_status_filter(value: str | None) -> str:
    status_filter = (value or "all").strip()
    if status_filter not in REVIEW_STATUS_FILTERS:
        raise SystemExit(
            f"Unknown review status filter: {status_filter!r}. "
            f"Valid filters: {', '.join(REVIEW_STATUS_FILTERS)}"
        )
    return status_filter


def _review_staging_db(run_dir: Path) -> tuple[Path, dict[str, Any]]:
    """Resolve one run to its STAGING database, refusing runs nothing can promote.

    Two guards, both load-bearing:

    * Completed runs are refused. ``_cleanup_finalize_staging`` removes only csv_root
      and staging_inbox, so a finalized or cancelled run's staging.db survives and would
      happily accept writes — reporting success while changing nothing, because there is
      no promote left to carry them into the live database.
    * The staging database must be a contained child of the run directory. A run.json
      whose ``staging_db_path`` points elsewhere (at the live database, say) is refused
      rather than followed.
    """
    run_path = run_dir / "run.json"
    if not run_path.exists():
        raise SystemExit(f"Run state not found: {run_path}")
    run_state = read_json(run_path)

    status_value = str(run_state.get("status", "unknown"))
    if status_value not in FINALIZABLE_STATUSES:
        raise SystemExit(
            f"Review decisions are unavailable for a {status_value} run: it has no "
            "pending promotion, so any decision saved now would never reach the ledger."
        )

    staging_value = run_state.get("staging_db_path") or ""
    if not staging_value:
        raise SystemExit("Run state has no staging database path.")
    staging_db, warning = _managed_archive_path(
        staging_value,
        run_dir,
        outside_warning="staging_db_outside_run",
        symlink_warning="staging_db_symlink",
    )
    if warning or staging_db is None:
        raise SystemExit(
            "Refusing to operate: this run's staging database is not a contained child "
            "of its run directory. Review is staging-only and must never write to the "
            "live database."
        )
    if not staging_db.is_file():
        raise SystemExit("Staging database not found for this import run.")
    return staging_db, run_state


def _review_row(staging_db: Path, key: str) -> dict[str, Any]:
    rows = {str(row["review_key"]): row for row in get_review_rows(staging_db)}
    row = rows.get(key)
    if row is None:
        raise SystemExit(
            f"Unknown review key: {key!r}. This run has {len(rows)} review key(s); "
            "run `review list --run-dir <run-dir>` to see them."
        )
    return row


def _restore_review_row(staging_db: Path, row: dict[str, Any]) -> None:
    """Restore one pre-command review row without calling a possibly failing writer."""
    import sqlite3
    from contextlib import closing

    with closing(sqlite3.connect(staging_db)) as conn, conn:
        cursor = conn.execute(
            """
            update import_review
            set status = ?, merchant = ?, category = ?, note = ?, scope = ?, updated_at = ?
            where review_key = ?
            """,
            (
                row.get("status") or "pending",
                row.get("merchant") or "",
                row.get("category") or "Other",
                row.get("note") or "",
                row.get("scope") or "same_raw_merchant_in_run",
                row.get("updated_at") or row.get("created_at") or utc_now(),
                row["review_key"],
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("The original staged review decision could not be restored.")


def _require_known_category(staging_db: Path, category: str) -> None:
    """Reject a category the applier will not recognise.

    ``apply_review_decisions`` resolves the exclusion flag with
    ``category_exc.get(row["category"], False)`` against exactly this config. An unknown
    name therefore evaluates to False and FLIPS ``exclude_from_spending`` from True to
    False, while the same write stamps 'manual' confidence — which every later rule pass
    skips, so the flag never self-heals. Fail loudly instead of silently.
    """
    from ledger.config.repository import SQLiteConfigRepository

    try:
        categories = SQLiteConfigRepository(staging_db).load_categories()
    except Exception as exc:  # config load failure must not become a silent False
        raise SystemExit(
            "Could not load the staged category config, so the category cannot be "
            f"validated ({type(exc).__name__}); refusing to resolve."
        ) from None
    known = sorted(rule.name for rule in categories.categories)
    if category not in known:
        raise SystemExit(
            f"Unknown category: {category!r}. Resolving to a category this run's config "
            "does not define silently clears exclude_from_spending and freezes it behind "
            "'manual' confidence. Valid categories: " + ", ".join(known)
        )


def _review_row_raw_text(staging_db: Path, row: dict[str, Any]) -> tuple[str, str]:
    """Return (merchant_raw, description) for one of this review row's transactions.

    A taught pattern must be derived from RAW statement text, so it is read from the
    staged transactions themselves. The review key is deliberately not a fallback: it
    may itself be a previously resolved merchant label, which import enrichment never
    pattern-matches as raw input.
    """
    import sqlite3
    from contextlib import closing

    txn_ids = json.loads(row.get("txn_ids") or "[]")
    if txn_ids:
        placeholders = ",".join("?" for _ in txn_ids)
        with closing(sqlite3.connect(staging_db)) as conn:
            conn.row_factory = sqlite3.Row
            found = conn.execute(
                f"""
                select merchant_raw, description from transactions
                where txn_id in ({placeholders})
                order by txn_id
                limit 1
                """,
                txn_ids,
            ).fetchone()
        if found is not None:
            return str(found["merchant_raw"] or ""), str(found["description"] or "")
    return "", ""


def _teach_from_review_row(
    staging_db: Path,
    *,
    row: dict[str, Any],
    merchant_norm: str,
    category: str,
    pattern: str | None,
) -> tuple[str, bool, bool]:
    """Persist a rule into the run's staging config and return pattern + spend semantics.

    Staging-only, like every other review write. The staging database is what finalize
    promotes, so the rule reaches the live ledger through the ordinary promote (with its
    drift guard intact) instead of through a live write that would abort this very run —
    see ``ledger.rules.categorization.OPEN_IMPORT_RUN_REMEDY``.

    The rule is written BEFORE the review decision, so a refused rule leaves the queue
    exactly as it was rather than half-applying what the operator asked for.
    """
    from ledger.config.repository import SQLiteConfigRepository
    from ledger.rules.categorization import derive_rule_suggestion, upsert_merchant_rule

    chosen = str(pattern or "").strip()
    if not chosen:
        merchant_raw, description = _review_row_raw_text(staging_db, row)
        try:
            chosen = derive_rule_suggestion(
                merchant_norm, merchant_raw, description, category
            ).pattern
        except ValueError as exc:
            raise SystemExit(str(exc)) from None

    repo = SQLiteConfigRepository(staging_db)
    taxonomy = repo.load_taxonomy()
    category_exclusions = {
        rule.name: bool(rule.exclude_from_spending) for rule in taxonomy.categories
    }
    if category in taxonomy.referenced_parent_names():
        raise SystemExit(
            f"Refusing to teach {merchant_norm!r}: {category!r} is a parent category and "
            "a merchant rule must target a leaf category."
        )
    try:
        updated = upsert_merchant_rule(
            repo.load_merchant_rules(),
            merchant_norm,
            chosen,
            category,
            category_exclusions=category_exclusions,
        )
    except ValueError as exc:
        raise SystemExit(f"Refusing to teach {merchant_norm!r}: {exc}") from None
    try:
        repo.replace_from_configs(updated, taxonomy)
    except Exception as exc:
        raise SystemExit(
            f"Could not save the merchant rule for {merchant_norm!r} ({type(exc).__name__}): "
            f"{exc}. No review decision was recorded."
        ) from None
    taught_rule = next(
        rule
        for rule in updated.rules
        if rule.merchant_norm.strip().upper() == merchant_norm.strip().upper()
    )
    stored_exclusion = bool(taught_rule.exclude_from_spending)
    effective_exclusion = bool(
        taught_rule.exclude_from_spending or category_exclusions.get(category, False)
    )
    return chosen, stored_exclusion, effective_exclusion


def _review_field(value: Any) -> str:
    """Collapse whitespace so one record always occupies exactly one output line."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return " ".join(str(value).split())


def _review_line(prefix: str, fields: dict[str, Any]) -> str:
    return "\t".join([prefix, *(f"{name}={_review_field(v)}" for name, v in fields.items())])


def _review_row_payload(row: dict[str, Any]) -> dict[str, Any]:
    """Project one review row onto the stable CLI surface.

    Deliberately omits ``scope`` (written by every writer, read by none) and ``examples``
    (verbatim statement descriptions); the run's own merchant-review.md already carries
    the examples for an agent that needs more context to decide.
    """
    return {
        "key": str(row["review_key"]),
        "status": str(row["status"]),
        "needs_decision": review_needs_decision(row),
        "merchant": str(row["merchant"] or ""),
        "category": str(row["category"] or ""),
        "note": str(row["note"] or ""),
        "transactions": int(row["transaction_count"] or 0),
        "amount": str(row["total_abs_amount"] or "0"),
    }


def _review_gate(run_dir: Path) -> dict[str, Any]:
    """Return the LIVE review gate for *run_dir*.

    ``manifest.json``'s top-level ``gates.review`` is a stage-time snapshot: it is
    written once and never refreshed, so it still reads "pass" while decisions are
    outstanding (and "fail" after they are all made). The nested readiness block counts
    the staging queue on every call, so that is the only honest gate.
    """
    readiness = build_import_readiness(run_dir)
    return {
        "run_status": readiness["status"],
        "review_gate": readiness["gates"]["review"],
        "pending_review": readiness["counts"]["pending_review"],
    }


def _print_review_result(
    prefix: str,
    run_dir: Path,
    fields: dict[str, Any],
    *,
    json_output: bool,
) -> None:
    payload = {"run_dir": str(run_dir), **_review_gate(run_dir), **fields}
    if json_output:
        print(json.dumps({"command": prefix, **payload}, indent=2))
        return
    print(_review_line(prefix, payload))


def review_list(
    run_dir: Path,
    *,
    status: str = "all",
    json_output: bool = False,
) -> None:
    """Print one import run's review queue and its live review gate."""
    resolved_run_dir = _review_run_dir(run_dir)
    status_filter = _validated_status_filter(status)
    staging_db, _run_state = _review_staging_db(resolved_run_dir)
    rows = [
        _review_row_payload(row)
        for row in get_review_rows(staging_db)
        if status_filter == "all" or row["status"] == status_filter
    ]
    header = {
        "run_dir": str(resolved_run_dir),
        **_review_gate(resolved_run_dir),
        "status_filter": status_filter,
    }
    if json_output:
        print(json.dumps({**header, "rows": rows}, indent=2))
        return
    print(_review_line("review_run", {**header, "rows": len(rows)}))
    for row in rows:
        print(_review_line("review_row", row))


def review_resolve(
    run_dir: Path,
    *,
    runs_dir: Path | None = None,
    key: str,
    category: str,
    merchant: str | None = None,
    note: str = "",
    teach: bool = False,
    pattern: str | None = None,
    json_output: bool = False,
) -> None:
    """Record a merchant/category decision for one staged review row.

    With ``teach``, the same decision also creates the reusable merchant rule, in the
    run's STAGING config, so the next import classifies this merchant without a review
    row. The rule's pattern is derived from the staged transaction's RAW text unless
    ``pattern`` overrides it. It does NOT re-apply across the staged rows: this row's own
    transactions are already set by the decision, and finalize re-runs enrichment with
    the staged config in place.
    """
    resolved_run_dir = _review_run_dir(run_dir)
    review_key = str(key or "").strip()
    if not review_key:
        raise SystemExit("--key is required: name the review row to resolve.")
    resolved_category = str(category or "").strip()
    if not resolved_category:
        raise SystemExit("--category is required: resolving writes a category.")
    if pattern is not None and not teach:
        raise SystemExit(
            "--pattern requires --teach; refusing to discard a supplied reusable-rule pattern."
        )

    try:
        # Serialize against finalize, which reads the queue and applies it in one pass.
        with finalize_run_lock(_review_runs_dir(runs_dir), resolved_run_dir):
            staging_db, _run_state = _review_staging_db(resolved_run_dir)
            row = _review_row(staging_db, review_key)
            _require_known_category(staging_db, resolved_category)
            # Omitting --merchant keeps the row's own merchant; passing it empty is an
            # explicit request to clear it, which the guard below then rejects.
            row_merchant = str(row["merchant"] or "")
            resolved_merchant = (row_merchant if merchant is None else str(merchant)).strip()
            if not resolved_merchant:
                # apply_review_decisions skips a resolved row missing either field, and
                # pending_review_count keeps counting it, so finalize would still block.
                raise SystemExit(
                    "Resolving needs a merchant name and this row has none; pass --merchant."
                )
            taught_pattern = ""
            taught_stored_exclude: bool | str = ""
            taught_effective_exclude: bool | str = ""
            prior_merchants = None
            prior_taxonomy = None
            if teach:
                from ledger.config.repository import SQLiteConfigRepository

                staging_config = SQLiteConfigRepository(staging_db)
                prior_merchants = staging_config.load_merchant_rules()
                prior_taxonomy = staging_config.load_taxonomy()
                taught_pattern, taught_stored_exclude, taught_effective_exclude = (
                    _teach_from_review_row(
                        staging_db,
                        row=row,
                        merchant_norm=resolved_merchant,
                        category=resolved_category,
                        pattern=pattern,
                    )
                )
            try:
                save_review_rows(
                    staging_db,
                    [
                        {
                            **row,
                            "status": "resolved",
                            "merchant": resolved_merchant,
                            "category": resolved_category,
                            "note": note or str(row["note"] or ""),
                        }
                    ],
                )
            except Exception:
                if teach and prior_merchants is not None and prior_taxonomy is not None:
                    rollback_errors: list[Exception] = []
                    try:
                        staging_config.replace_from_configs(prior_merchants, prior_taxonomy)
                    except Exception as rollback_exc:
                        rollback_errors.append(rollback_exc)
                    try:
                        _restore_review_row(staging_db, row)
                    except Exception as rollback_exc:
                        rollback_errors.append(rollback_exc)
                    if rollback_errors:
                        raise RuntimeError(
                            "Could not fully roll back staged teaching after the review "
                            "decision failed; cancel and restage this import."
                        ) from rollback_errors[0]
                raise
    except FinalizeLockError as exc:
        raise SystemExit(str(exc)) from None

    _print_review_result(
        "review_resolved",
        resolved_run_dir,
        {
            "key": review_key,
            "merchant": resolved_merchant,
            "category": resolved_category,
            "taught": teach,
            "taught_pattern": taught_pattern,
            "taught_stored_merchant_exclude_from_spending": taught_stored_exclude,
            "taught_effective_exclude_from_spending": taught_effective_exclude,
        },
        json_output=json_output,
    )


def review_reviewed_as_is(
    run_dir: Path,
    *,
    runs_dir: Path | None = None,
    key: str,
    note: str = "",
    json_output: bool = False,
) -> None:
    """Accept one staged review row's auto-derived values and stop prompting for it."""
    resolved_run_dir = _review_run_dir(run_dir)
    review_key = str(key or "").strip()
    if not review_key:
        raise SystemExit("--key is required: name the review row to accept as is.")

    try:
        with finalize_run_lock(_review_runs_dir(runs_dir), resolved_run_dir):
            staging_db, _run_state = _review_staging_db(resolved_run_dir)
            row = _review_row(staging_db, review_key)
            # Only the status changes: merchant/category stay auto-derived so a later
            # rule pass can still improve them (apply_review_decisions never freezes
            # confidence for this status).
            save_review_rows(
                staging_db,
                [{**row, "status": "reviewed_as_is", "note": note or str(row["note"] or "")}],
            )
    except FinalizeLockError as exc:
        raise SystemExit(str(exc)) from None

    _print_review_result(
        "review_reviewed_as_is",
        resolved_run_dir,
        {"key": review_key},
        json_output=json_output,
    )


def review(args: argparse.Namespace) -> None:
    """Dispatch the argparse ``review`` subcommands."""
    command = str(getattr(args, "review_command", "") or "")
    if command == "list":
        review_list(args.run_dir, status=args.status, json_output=args.json_output)
    elif command == "resolve":
        review_resolve(
            args.run_dir,
            runs_dir=args.runs_dir,
            key=args.key,
            category=args.category,
            merchant=args.merchant,
            note=args.note,
            teach=args.teach,
            pattern=args.pattern,
            json_output=args.json_output,
        )
    elif command == "reviewed-as-is":
        review_reviewed_as_is(
            args.run_dir,
            runs_dir=args.runs_dir,
            key=args.key,
            note=args.note,
            json_output=args.json_output,
        )
    else:
        raise SystemExit(f"Unknown review command: {command}")


def build_paths(args: argparse.Namespace) -> Paths:
    from ledger.runtime import require_external_runtime_path

    for value in (args.pdf_dir, args.processed_dir, args.db, args.backup_dir, args.runs_dir):
        require_external_runtime_path(value)
    if args.base_db is not None:
        require_external_runtime_path(args.base_db)
    return Paths(
        pdf_dir=args.pdf_dir.expanduser().resolve(),
        processed_dir=args.processed_dir.expanduser().resolve(),
        db_path=args.db.expanduser().resolve(),
        base_db_path=args.base_db.expanduser().resolve() if args.base_db else None,
        backup_dir=args.backup_dir.expanduser().resolve(),
        ledger_config=args.ledger_config.expanduser().resolve(),
        merchant_rules=args.merchant_rules.expanduser().resolve(),
        categories=args.categories.expanduser().resolve(),
        runs_dir=args.runs_dir.expanduser().resolve(),
    )


def add_review_args(parser: argparse.ArgumentParser) -> None:
    """Add the arguments every review subcommand shares.

    Review is STAGING-ONLY, so it deliberately omits every argument in
    ``add_common_args`` that names the target database or the inbox: there is nothing
    for the caller to point at but the run directory itself.
    """
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--json", action="store_true", dest="json_output")


def add_review_write_args(parser: argparse.ArgumentParser) -> None:
    """Writers additionally need --runs-dir to take the per-run finalize lock."""
    add_review_args(parser)
    parser.add_argument("--runs-dir", type=Path, default=runtime_path("import-runs"))
    parser.add_argument("--key", required=True)
    parser.add_argument("--note", default="")


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--pdf-dir", type=Path, default=runtime_path("inbox"))
    parser.add_argument("--processed-dir", type=Path, default=runtime_path("processed"))
    parser.add_argument("--db", type=Path, default=default_database_path())
    parser.add_argument("--base-db", type=Path, default=None)
    parser.add_argument("--backup-dir", type=Path, default=runtime_path("db-backups"))
    parser.add_argument("--ledger-config", type=Path, default=seed_config_path("ledger.toml"))
    parser.add_argument("--merchant-rules", type=Path, default=seed_config_path("merchants.toml"))
    parser.add_argument("--categories", type=Path, default=seed_config_path("categories.toml"))
    parser.add_argument("--runs-dir", type=Path, default=runtime_path("import-runs"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    stage_parser = subparsers.add_parser("stage", help="Stage PDFs and insert parsed rows")
    add_common_args(stage_parser)

    finalize_parser = subparsers.add_parser("finalize", help="Finalize staged import")
    add_common_args(finalize_parser)
    finalize_parser.add_argument("--run-dir", type=Path, default=None)

    status_parser = subparsers.add_parser("status", help="Print import run manifest")
    add_common_args(status_parser)
    status_parser.add_argument("--run-dir", type=Path, default=None)

    cancel_parser = subparsers.add_parser("cancel", help="Cancel a staged import run")
    add_common_args(cancel_parser)
    cancel_parser.add_argument("--run-dir", type=Path, default=None)
    cancel_parser.add_argument("--reason", default="manual_cancel")

    repair_parser = subparsers.add_parser(
        "repair-coordination",
        help="Register a bounded coordination record for an already-terminal run",
    )
    repair_parser.add_argument("--run-dir", type=Path, required=True)

    review_parser = subparsers.add_parser(
        "review", help="Inspect and decide a staged run's review queue"
    )
    review_subparsers = review_parser.add_subparsers(dest="review_command", required=True)

    review_list_parser = review_subparsers.add_parser(
        "list", help="List review rows and the live review gate"
    )
    add_review_args(review_list_parser)
    review_list_parser.add_argument("--status", default="all")

    review_resolve_parser = review_subparsers.add_parser(
        "resolve", help="Set a review row's merchant and category"
    )
    add_review_write_args(review_resolve_parser)
    review_resolve_parser.add_argument("--category", required=True)
    review_resolve_parser.add_argument("--merchant", default=None)
    review_resolve_parser.add_argument(
        "--teach",
        action="store_true",
        help="Also create the merchant rule, in this run's staging config",
    )
    review_resolve_parser.add_argument(
        "--pattern",
        default=None,
        help="Override the pattern --teach derives from the staged raw text",
    )

    review_as_is_parser = review_subparsers.add_parser(
        "reviewed-as-is", help="Accept a review row's auto-derived values"
    )
    add_review_write_args(review_as_is_parser)

    args = parser.parse_args()

    # Review never reads or writes the target database, so it does not take (or build)
    # the target-DB Paths bundle the other subcommands depend on.
    if args.command == "review":
        review(args)
        return 0

    # Repairing coordination names one run explicitly and never touches the target
    # database, so it must not build (or lock) the target Paths bundle — the whole
    # point is that it stays usable while live writes are refusing.
    if args.command == "repair-coordination":
        from ledger.imports.pdf.readiness import repair_run_coordination

        try:
            record = repair_run_coordination(args.run_dir)
        except ValueError as exc:
            raise SystemExit(str(exc)) from None
        print(f"repaired_coordination\trun_dir={args.run_dir.resolve()}\trecord={record}")
        return 0

    paths = build_paths(args)

    if args.command == "stage":
        stage(args, paths)
    elif args.command == "finalize":
        finalize(args, paths)
    elif args.command == "status":
        status(args, paths)
    elif args.command == "cancel":
        cancel(args, paths)
    else:
        parser.error(f"Unknown command: {args.command}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
