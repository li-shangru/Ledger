"""Staged, local-only import workflow for historical investment statements."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any, Literal

from ledger.adapters.contracts import AdapterError, InvestmentFormat
from ledger.adapters.registry import get_investment_format, snapshot_bindings, verify_bindings
from ledger.imports.pdf.files import (
    clear_latest_run_file,
    clone_database,
    finalize_run_lock,
    make_run_dir,
    promote_database,
    resolve_run_dir,
)
from ledger.imports.pdf.readiness import (
    import_owned_fingerprint,
    import_owned_match,
    investment_preserved_fingerprint,
    investment_preserved_match,
)
from ledger.investments.contracts import validate_statement_contract
from ledger.investments.models import InvestmentStatement
from ledger.investments.repository import InvestmentRepository
from ledger.runtime import resolve_runtime_path
from ledger.storage.database import (
    InvestmentImportRunDB,
    InvestmentStatementDB,
    session_scope,
)
from ledger.storage.migrations import run_migrations
from ledger.storage.snapshots import (
    DatabasePublicationDurabilityError,
    database_target_lock,
    snapshot_database,
    validated_database_target,
)
from ledger.utils.json_io import utc_now, write_json_atomic
from ledger.utils.private_files import (
    PinnedPrivateDirectory,
    pin_existing_directory,
    pin_private_directory,
)

_MANIFEST_VERSION = 1
_MANIFEST_NAME = "manifest.json"
_STAGING_DB_NAME = "staging.db"
_CANCELLATION_AUDIT_NAME = "cancellation-audit.db"
_SOURCE_SUFFIXES = {".csv", ".pdf"}
_MANAGED_ARCHIVE_PREFIXES = (
    ".investment-source-recovery-",
    ".investment-archive-candidate-",
    ".investment-archive-guard-",
)
_CANCELLATION_AUDIT_COLUMNS = (
    "run_id",
    "status",
    "cancelled_at",
    "cancel_reason",
    "source_count",
    "structured_source_count",
    "evidence_source_count",
    "statements_imported",
)


@dataclass(frozen=True)
class _PinnedSource:
    path: Path
    alias: str
    role: Literal["csv", "pdf", "evidence_pdf", "statement_pdf"]
    sha256: str
    size_bytes: int
    device: int
    inode: int
    modified_ns: int
    content: bytes
    institution: str = ""
    source_layout: str = ""
    period_start: date | None = None
    period_end: date | None = None
    descriptor: int = -1
    directory: PinnedPrivateDirectory | None = None


@dataclass(frozen=True)
class _LocatedSource:
    """A finalize-time source bound to captured parent and child identities."""

    path: Path
    parent_identity: tuple[int, int]
    device: int
    inode: int
    size_bytes: int
    modified_ns: int
    sha256: str
    content: bytes


@dataclass(frozen=True)
class _ArchiveArtifactNames:
    source_recovery: str
    archive_candidate: str
    archive_guard: str


@dataclass(frozen=True)
class _StatementPair:
    alias: str
    csv: _PinnedSource | None
    pdf: _PinnedSource
    statement: InvestmentStatement


def stage_investment_import(
    *,
    inbox_dir: Path,
    db_path: Path,
    runs_dir: Path,
    processed_dir: Path,
    backup_dir: Path,
    closed_aliases: tuple[str, ...] = (),
    transfer_destination: str | None = None,
    status_confirmed_on: date | None = None,
    replace_conflicts: bool = False,
    institution: str | None = None,
) -> Path:
    """Validate sources into a private staging clone without touching live state."""
    inbox_dir = _absolute(inbox_dir)
    db_path = _absolute(db_path)
    runs_dir = _absolute(runs_dir)
    processed_dir = _absolute(processed_dir)
    backup_dir = _absolute(backup_dir)
    adapter_binding = snapshot_bindings()
    investment_format = get_investment_format(institution)
    verify_bindings(adapter_binding)
    institution = _safe_source_token(investment_format.institution)
    source_layout = investment_format.source_layout
    if source_layout not in {"flat", "account_folders"}:
        raise AdapterError("adapter_invalid_contract")
    if source_layout == "flat":
        if closed_aliases or transfer_destination is not None or status_confirmed_on is not None:
            raise ValueError("Flat statement imports do not accept lifecycle inputs.")
        normalized_aliases: tuple[str, ...] = ()
    else:
        normalized_aliases = (
            _normalize_closed_aliases(closed_aliases, investment_format) if closed_aliases else ()
        )
        if normalized_aliases:
            if transfer_destination is None or not transfer_destination.strip():
                raise ValueError("Closed investment accounts require a transfer destination.")
            if status_confirmed_on is None:
                raise ValueError("Closed investment accounts require a confirmation date.")
            transfer_destination = transfer_destination.strip()
        elif transfer_destination is not None or status_confirmed_on is not None:
            raise ValueError("Lifecycle confirmation requires explicitly selected closed aliases.")
    _require_safe_directory(inbox_dir, label="Investment inbox")
    _reject_symlink_components(db_path)
    _reject_symlink_components(runs_dir)
    _reject_symlink_components(processed_dir)
    _reject_symlink_components(backup_dir)

    pairs, evidence = (
        _discover_flat_sources(inbox_dir, investment_format)
        if source_layout == "flat"
        else _discover_sources(inbox_dir, normalized_aliases, investment_format)
    )
    if not pairs:
        raise ValueError("Investment inbox contains no supported complete investment statements.")
    observed_aliases = {pair.alias for pair in pairs}
    missing_aliases = set(normalized_aliases) - observed_aliases
    if missing_aliases:
        raise ValueError("Every explicitly closed investment alias requires a statement pair.")

    verify_bindings(adapter_binding)
    baseline = import_owned_fingerprint(db_path)
    preserved_baseline = investment_preserved_fingerprint(db_path)
    run_dir = make_run_dir(runs_dir)
    staging_db = run_dir / _STAGING_DB_NAME
    sources = [
        source for pair in pairs for source in (pair.csv, pair.pdf) if source is not None
    ] + evidence
    lifecycle: dict[str, Any] = {
        "closed_aliases": list(normalized_aliases),
        "transfer_destination": transfer_destination,
        "status_confirmed_on": status_confirmed_on.isoformat() if status_confirmed_on else None,
    }
    manifest_paths = {
        "inbox_dir": str(inbox_dir),
        "db_path": str(db_path),
        "processed_dir": str(processed_dir),
        "backup_dir": str(backup_dir),
    }
    manifest_sources = [_manifest_source(source, run_id=run_dir.name) for source in sources]
    interpretation = {
        "adapter_binding": adapter_binding,
        "format_id": investment_format.format_id,
        "institution": institution,
        "source_layout": source_layout,
    }
    lifecycle["adapter_binding_fingerprint"] = _interpretation_binding_fingerprint(interpretation)
    lifecycle["source_binding_fingerprint"] = _flat_source_binding_fingerprint(
        {**interpretation, "paths": manifest_paths, "sources": manifest_sources}
    )
    result_counts = {"inserted": 0, "replaced": 0, "unchanged": 0}
    conflict_bindings: list[dict[str, Any]] = []
    conflict_summaries: list[dict[str, Any]] = []
    repository: InvestmentRepository | None = None
    run_record_created = False
    try:
        clone_database(db_path if db_path.exists() else None, staging_db)
        run_migrations(staging_db)
        repository = InvestmentRepository(staging_db, initialize=False)
        repository.create_import_run(
            run_id=run_dir.name,
            source_path=inbox_dir,
            private_manifest_path=run_dir / _MANIFEST_NAME,
            baseline_fingerprint=baseline,
            lifecycle_inputs=lifecycle,
            source_count=len(sources),
            structured_source_count=len(sources) - len(evidence),
            evidence_source_count=len(evidence),
        )
        run_record_created = True
        for pair in pairs:
            conflict = repository.conflict_delta(pair.statement)
            if conflict is not None:
                private, summary = conflict
                conflict_bindings.append(private)
                conflict_summaries.append(summary)
            result = repository.store_statement(
                pair.statement,
                source_csv_path=pair.csv.path if pair.csv else None,
                source_csv_sha256=pair.csv.sha256 if pair.csv else None,
                source_pdf_path=pair.pdf.path,
                source_pdf_sha256=pair.pdf.sha256,
                source_csv_size=pair.csv.size_bytes if pair.csv else None,
                source_pdf_size=pair.pdf.size_bytes,
                import_run_id=run_dir.name,
                replace_conflict=replace_conflicts,
                source_pdf_role="statement_pdf" if pair.csv is None else "pdf",
            )
            result_counts[result] += 1
        for source in evidence:
            repository.record_evidence_source(
                source_path=source.path,
                file_hash=source.sha256,
                byte_size=source.size_bytes,
                import_run_id=run_dir.name,
            )
        for alias in normalized_aliases:
            assert status_confirmed_on is not None
            repository.set_account_lifecycle(
                alias,
                "closed",
                status_confirmed_on,
                transfer_destination,
            )
        verify_bindings(adapter_binding)
        _validate_staging_database(staging_db, expected_statements=len(pairs))
        conflict_authorization = _conflict_authorization_fingerprint(conflict_bindings)
        pre_audit_staging = import_owned_fingerprint(staging_db)
        repository.update_import_run(
            run_dir.name,
            "staged",
            staging_fingerprint=pre_audit_staging,
            target_fingerprint=baseline,
            statements_imported=len(pairs),
            inserted_count=result_counts["inserted"],
            replaced_count=result_counts["replaced"],
            unchanged_count=result_counts["unchanged"],
            conflict_review={"summary": conflict_summaries},
            conflict_authorization_fingerprint=conflict_authorization,
            redacted_failure=None,
        )
        manifest: dict[str, Any] = {
            "version": _MANIFEST_VERSION,
            "run_id": run_dir.name,
            "manifest_path": str(run_dir / _MANIFEST_NAME),
            "status": "staged",
            "created_at": utc_now(),
            "updated_at": utc_now(),
            "published": False,
            "paths": manifest_paths,
            "lifecycle": lifecycle,
            **interpretation,
            "replace_conflicts": replace_conflicts,
            "target_import_owned": baseline,
            "target_non_investment": preserved_baseline,
            "staging_import_owned": import_owned_fingerprint(staging_db),
            "staging_non_investment": investment_preserved_fingerprint(staging_db),
            "conflict_review": {
                "bindings": conflict_bindings,
                "summary": conflict_summaries,
                "authorization_fingerprint": conflict_authorization,
            },
            "gates": {
                "staging_integrity": "pass",
                "source_identity": "pass",
                "target_unchanged": "pass",
                "conflict_authorization": "pass",
                "publication": "not_run",
                "archive": "not_run",
            },
            "counts": {
                "evidence_pdfs": len(evidence),
                "pairs": sum(pair.csv is not None for pair in pairs),
                "statements": len(pairs),
                **result_counts,
            },
            "sources": manifest_sources,
            "next_actions": ["finalize", "cancel"],
        }
        verify_bindings(adapter_binding)
        _write_manifest(run_dir, manifest)
    except BaseException as primary_error:
        if not run_record_created:
            try:
                clear_latest_run_file(runs_dir, run_dir)
            except BaseException as cleanup_error:
                primary_error.add_note(
                    "Secondary latest-run cleanup failure: "
                    f"{type(cleanup_error).__name__}: {cleanup_error}"
                )
        if run_record_created and repository is not None:
            try:
                conflict_authorization = _conflict_authorization_fingerprint(conflict_bindings)
                repository.update_run_sources(run_dir.name, "failed")
                repository.update_import_run(
                    run_dir.name,
                    "failed",
                    staging_fingerprint=import_owned_fingerprint(staging_db),
                    target_fingerprint=baseline,
                    statements_imported=sum(result_counts.values()),
                    inserted_count=result_counts["inserted"],
                    replaced_count=result_counts["replaced"],
                    unchanged_count=result_counts["unchanged"],
                    conflict_review={"summary": conflict_summaries},
                    conflict_authorization_fingerprint=conflict_authorization,
                    redacted_failure="staging_failed",
                    errors=1,
                )
                failed_manifest: dict[str, Any] = {
                    "version": _MANIFEST_VERSION,
                    "run_id": run_dir.name,
                    "manifest_path": str(run_dir / _MANIFEST_NAME),
                    "status": "failed",
                    "created_at": utc_now(),
                    "updated_at": utc_now(),
                    "published": False,
                    "paths": manifest_paths,
                    "lifecycle": lifecycle,
                    **interpretation,
                    "replace_conflicts": replace_conflicts,
                    "target_import_owned": baseline,
                    "target_non_investment": preserved_baseline,
                    "staging_import_owned": import_owned_fingerprint(staging_db),
                    "conflict_review": {
                        "bindings": conflict_bindings,
                        "summary": conflict_summaries,
                        "authorization_fingerprint": conflict_authorization,
                    },
                    "gates": {
                        "staging_integrity": "fail",
                        "source_identity": "pass",
                        "target_unchanged": "pass",
                        "conflict_authorization": "not_run",
                        "publication": "not_run",
                        "archive": "not_run",
                    },
                    "counts": {
                        "evidence_pdfs": len(evidence),
                        "pairs": sum(pair.csv is not None for pair in pairs),
                        "statements": sum(result_counts.values()),
                        **result_counts,
                    },
                    "sources": manifest_sources,
                    "last_error": "staging_failed",
                    "next_actions": ["cancel"],
                }
                _write_manifest(run_dir, failed_manifest)
            except BaseException:
                _cleanup_staging_database(staging_db)
            else:
                raise
        _cleanup_staging_database(staging_db)
        raise
    return run_dir


def investment_import_status(
    run_dir: Path | None = None,
    *,
    runs_dir: Path | None = None,
) -> dict[str, Any]:
    """Return the private, content-free manifest for one investment run."""
    runs_dir = resolve_runtime_path(runs_dir, "investment-import-runs")
    resolved = _resolve_and_validate_run(runs_dir, run_dir)
    return _read_manifest(resolved)


def finalize_investment_import(
    run_dir: Path | None = None,
    *,
    runs_dir: Path | None = None,
) -> dict[str, Any]:
    """Publish a staged investment database and then archive verified sources."""
    runs_dir = resolve_runtime_path(runs_dir, "investment-import-runs")
    resolved = _resolve_and_validate_run(runs_dir, run_dir)
    with finalize_run_lock(resolved.parent, resolved):
        manifest = _read_manifest(resolved)
        _reconcile_staging_transition(resolved, manifest)
        manifest.setdefault("gates", {})
        status = manifest.get("status")
        entry_status = status
        if status == "finalized":
            return manifest
        if status == "cancelled":
            raise RuntimeError("Cancelled investment import cannot be finalized.")
        if status not in {"staged", "finalizing", "archive_pending"}:
            raise RuntimeError("Investment import is not in a finalizable state.")

        paths = _manifest_paths(manifest)
        if status == "finalizing":
            expected_staging_owned = _require_manifest_fingerprint(manifest, "staging_import_owned")
            expected_staging_preserved = _require_manifest_fingerprint(
                manifest, "staging_non_investment"
            )
            expected_target_owned = _require_manifest_fingerprint(manifest, "target_import_owned")
            expected_target_preserved = _require_manifest_fingerprint(
                manifest, "target_non_investment"
            )
            staging_db = resolved / _STAGING_DB_NAME
            if not staging_db.is_file() or staging_db.is_symlink():
                raise RuntimeError("Investment staging database is missing or unsafe.")
            _validate_staging_database(
                staging_db,
                expected_statements=_require_manifest_statement_count(manifest),
            )
            current_staging_owned = import_owned_fingerprint(staging_db)
            current_staging_preserved = investment_preserved_fingerprint(staging_db)
            if not import_owned_match(
                expected_staging_owned, current_staging_owned
            ) or not investment_preserved_match(
                expected_staging_preserved, current_staging_preserved
            ):
                raise RuntimeError("Investment staging database has recovery fingerprint drift.")
            target_was_absent = (
                expected_target_owned["tables"] == {} and expected_target_preserved["tables"] == {}
            )
            if not paths["db_path"].exists():
                if not target_was_absent:
                    raise RuntimeError(
                        "Finalizing investment import has ambiguous target DB recovery drift."
                    )
                _transition_staging_run(
                    resolved,
                    manifest,
                    InvestmentRepository(staging_db, initialize=False),
                    from_status="finalizing",
                    to_status="staged",
                    last_error="publication_failed",
                    redacted_failure="publication_failed",
                    errors=1,
                )
                status = "staged"
            else:
                recovery_state: str | None = None

                def classify_recovery(connection: sqlite3.Connection) -> None:
                    nonlocal recovery_state
                    current_target = import_owned_fingerprint(connection)
                    current_preserved = investment_preserved_fingerprint(connection)
                    published_match = import_owned_match(
                        expected_staging_owned, current_target
                    ) and investment_preserved_match(expected_staging_preserved, current_preserved)
                    baseline_match = import_owned_match(
                        expected_target_owned, current_target
                    ) and investment_preserved_match(expected_target_preserved, current_preserved)
                    if published_match == baseline_match:
                        raise RuntimeError(
                            "Finalizing investment import has ambiguous target DB recovery drift."
                        )
                    recovery_state = "published" if published_match else "not_published"

                recovery_manifest = deepcopy(manifest)
                transition_attempted = False
                with database_target_lock(paths["db_path"]):
                    try:
                        with validated_database_target(
                            paths["db_path"],
                            label="Investment recovery target",
                            guard=classify_recovery,
                        ):
                            if recovery_state == "published":
                                _restore_legacy_published_metadata(manifest, staging_db)
                                manifest["published"] = True
                                manifest["published_at"] = manifest.get("published_at") or utc_now()
                                manifest["publication_recovered"] = True
                                manifest["status"] = "archive_pending"
                                manifest["last_error"] = None
                                manifest["gates"]["publication"] = "pass"
                                manifest["gates"]["archive"] = "pending"
                                transition_attempted = True
                                _write_manifest(resolved, manifest)
                                status = "archive_pending"
                            elif recovery_state == "not_published":
                                _transition_staging_run(
                                    resolved,
                                    manifest,
                                    InvestmentRepository(staging_db, initialize=False),
                                    from_status="finalizing",
                                    to_status="staged",
                                    last_error="publication_failed",
                                    redacted_failure="publication_failed",
                                    errors=1,
                                )
                                status = "staged"
                            else:  # pragma: no cover - guard must classify or raise
                                raise RuntimeError(
                                    "Finalizing investment import recovery state is unavailable."
                                )
                    except BaseException as primary_error:
                        if transition_attempted:
                            manifest.clear()
                            manifest.update(recovery_manifest)
                            try:
                                _write_manifest(resolved, manifest)
                            except BaseException as restore_error:
                                primary_error.add_note(
                                    "Secondary recovery manifest restore failure: "
                                    f"{type(restore_error).__name__}: {restore_error}"
                                )
                        raise

        if status == "staged":
            _verify_manifest_adapter(manifest)
            staging_db = resolved / _STAGING_DB_NAME
            if not staging_db.is_file() or staging_db.is_symlink():
                raise RuntimeError("Investment staging database is missing or unsafe.")
            _validate_prepublication_archive_collisions(
                manifest,
                paths["processed_dir"],
            )
            pending_sources = _validate_prepublication_sources(manifest, paths["inbox_dir"])
            _validate_staging_database(
                staging_db,
                expected_statements=_require_manifest_statement_count(manifest),
            )
            expected_staging_owned = _require_manifest_fingerprint(manifest, "staging_import_owned")
            current_staging = import_owned_fingerprint(staging_db)
            if not import_owned_match(expected_staging_owned, current_staging):
                raise RuntimeError("Investment staging database changed after validation.")
            current_staging_preserved = investment_preserved_fingerprint(staging_db)
            if "staging_non_investment" in manifest:
                expected_staging_preserved = _require_manifest_fingerprint(
                    manifest, "staging_non_investment"
                )
                if not investment_preserved_match(
                    expected_staging_preserved, current_staging_preserved
                ):
                    raise RuntimeError("Investment staging database changed after validation.")
            else:
                manifest["staging_non_investment"] = current_staging_preserved
            current_target = import_owned_fingerprint(paths["db_path"])
            if not import_owned_match(manifest["target_import_owned"], current_target):
                raise RuntimeError(
                    "target DB changed during finalize; aborting promote to avoid losing edits"
                )
            current_preserved = investment_preserved_fingerprint(paths["db_path"])
            if not investment_preserved_match(manifest["target_non_investment"], current_preserved):
                raise RuntimeError(
                    "target DB changed during finalize; aborting promote to preserve "
                    "non-investment state"
                )
            _verify_conflict_authorization(
                manifest,
                pending_sources,
                paths["db_path"],
                staging_db,
            )
            publication_manifest = deepcopy(manifest)
            publication_sources = tuple(pending_sources.values())

            def verify_publication_inputs() -> None:
                _verify_manifest_adapter(publication_manifest)
                for source in publication_sources:
                    _require_located_source_unchanged(source)
                _validate_prepublication_sources(publication_manifest, paths["inbox_dir"])
                _verify_manifest_adapter(publication_manifest)

            staging_repository = InvestmentRepository(staging_db, initialize=False)
            with tempfile.TemporaryDirectory(
                prefix="ledger-investment-promotion-"
            ) as raw_candidate_dir:
                promotion_candidate = Path(raw_candidate_dir).resolve() / "candidate.db"
                _verify_manifest_adapter(manifest)
                _transition_staging_run(
                    resolved,
                    manifest,
                    staging_repository,
                    from_status="staged",
                    to_status="finalizing",
                    last_error=None,
                    redacted_failure=None,
                    promotion_candidate=promotion_candidate,
                )
                try:
                    _verify_manifest_adapter(manifest)
                    backup = promote_database(
                        promotion_candidate,
                        paths["db_path"],
                        paths["backup_dir"],
                        expected_import_owned=manifest["target_import_owned"],
                        expected_preserved_tables=manifest["target_non_investment"],
                        prepublication_check=verify_publication_inputs,
                        label="pre-investment-import",
                    )
                except DatabasePublicationDurabilityError as exc:
                    if exc.target_durable:
                        manifest["last_error"] = "publication_durability_failed"
                        manifest["gates"]["publication"] = "pending"
                        _write_manifest(resolved, manifest)
                        raise
                    _transition_staging_run(
                        resolved,
                        manifest,
                        staging_repository,
                        from_status="finalizing",
                        to_status="staged",
                        last_error="publication_failed",
                        redacted_failure="publication_failed",
                        errors=1,
                    )
                    raise
                except Exception:
                    _transition_staging_run(
                        resolved,
                        manifest,
                        staging_repository,
                        from_status="finalizing",
                        to_status="staged",
                        last_error="publication_failed",
                        redacted_failure="publication_failed",
                        errors=1,
                    )
                    raise
            manifest["published"] = True
            manifest["published_at"] = utc_now()
            manifest["status"] = "archive_pending"
            manifest["backup"] = str(backup) if backup is not None else None
            manifest["last_error"] = None
            manifest["gates"]["publication"] = "pass"
            manifest["gates"]["archive"] = "pending"
            _write_manifest(resolved, manifest)
            status = "archive_pending"

        if entry_status == "archive_pending":
            _validate_archive_pending_target(
                manifest,
                paths,
                staging_db=resolved / _STAGING_DB_NAME,
            )
        _preflight_archives(manifest, paths["processed_dir"])
        pending_sources, recovered_archives = _locate_pending_sources(
            manifest, paths["inbox_dir"], paths["processed_dir"]
        )
        if recovered_archives:
            _write_manifest(resolved, manifest)

        live_repository = InvestmentRepository(paths["db_path"], initialize=False)
        live_repository.update_import_run(
            str(manifest["run_id"]),
            "archive_pending",
            redacted_failure=None,
        )
        for source in manifest["sources"]:
            if source.get("archive_status") != "archived":
                continue
            live_repository.update_source_archive(
                file_hash=str(source["sha256"]),
                archived_path=_archive_target(paths["processed_dir"], source),
                finalized=True,
                import_run_id=str(manifest["run_id"]),
            )

        for source in manifest["sources"]:
            if source.get("archive_status") == "archived":
                continue
            source_path = pending_sources[source["source_key"]]
            try:
                archived_path = _archive_source(source, source_path, paths["processed_dir"])
            except Exception:
                manifest["status"] = "archive_pending"
                manifest["last_error"] = "archive_failed"
                manifest["gates"]["archive"] = "pending"
                _write_manifest(resolved, manifest)
                live_repository.update_import_run(
                    str(manifest["run_id"]),
                    "archive_pending",
                    redacted_failure="archive_failed",
                    errors=1,
                )
                return manifest
            live_repository.update_source_archive(
                file_hash=str(source["sha256"]),
                archived_path=archived_path,
                finalized=True,
                import_run_id=str(manifest["run_id"]),
            )
            source["archive_status"] = "archived"
            source["archived_at"] = utc_now()
            manifest["last_error"] = None
            _write_manifest(resolved, manifest)

        live_repository.update_import_run(
            str(manifest["run_id"]),
            "finalized",
            target_fingerprint=import_owned_fingerprint(paths["db_path"]),
            redacted_failure=None,
            errors=0,
        )
        manifest["status"] = "finalized"
        manifest["finalized_at"] = utc_now()
        manifest["next_actions"] = []
        manifest["last_error"] = None
        manifest["gates"]["archive"] = "pass"
        _write_manifest(resolved, manifest)
        return manifest


def cancel_investment_import(
    run_dir: Path | None = None,
    *,
    runs_dir: Path | None = None,
    reason: str = "manual_cancel",
) -> dict[str, Any]:
    """Cancel an unpublished run without changing source or live data."""
    runs_dir = resolve_runtime_path(runs_dir, "investment-import-runs")
    resolved = _resolve_and_validate_run(runs_dir, run_dir)
    safe_reason = _safe_reason(reason)
    with finalize_run_lock(resolved.parent, resolved):
        manifest = _read_manifest(resolved)
        if manifest.get("published") or manifest.get("status") in {
            "archive_pending",
            "finalizing",
            "finalized",
        }:
            raise RuntimeError("A published investment import cannot be cancelled.")
        if manifest.get("status") == "cancelled":
            cancelled_at = manifest.get("cancelled_at")
            cancel_reason = manifest.get("cancel_reason")
            if not isinstance(cancelled_at, str) or not isinstance(cancel_reason, str):
                raise RuntimeError("Cancelled investment import audit metadata is invalid.")
            _publish_cancellation_audit(
                resolved,
                manifest,
                cancel_reason=_safe_reason(cancel_reason),
                cancelled_at=cancelled_at,
            )
            return manifest
        if manifest.get("status") not in {"staged", "failed"}:
            raise RuntimeError("Investment import is not in a cancellable state.")
        staging_db = resolved / _STAGING_DB_NAME
        if staging_db.is_file() and not staging_db.is_symlink():
            repository = InvestmentRepository(staging_db, initialize=False)
            repository.update_run_sources(str(manifest["run_id"]), "cancelled")
            repository.update_import_run(
                str(manifest["run_id"]),
                "cancelled",
                redacted_failure=None,
            )
        cancelled_at = utc_now()
        _publish_cancellation_audit(
            resolved,
            manifest,
            cancel_reason=safe_reason,
            cancelled_at=cancelled_at,
        )
        _cleanup_staging_database(staging_db)
        manifest["status"] = "cancelled"
        manifest["cancel_reason"] = safe_reason
        manifest["cancelled_at"] = cancelled_at
        manifest["next_actions"] = []
        _write_manifest(resolved, manifest)
        return manifest


def _absolute(path: Path) -> Path:
    expanded = Path(path).expanduser()
    return expanded if expanded.is_absolute() else Path.cwd() / expanded


def _normalize_closed_aliases(
    aliases: tuple[str, ...], investment_format: InvestmentFormat
) -> tuple[str, ...]:
    normalized = tuple(
        _safe_source_token(investment_format.normalize_alias(alias).strip().upper())
        for alias in aliases
    )
    if not normalized or any(not alias for alias in normalized):
        raise ValueError("At least one non-empty closed investment alias is required.")
    if len(set(normalized)) != len(normalized):
        raise ValueError("Closed investment aliases are ambiguous after normalization.")
    return normalized


def _reject_symlink_components(path: Path) -> None:
    lexical = _absolute(path)
    current = Path(lexical.anchor)
    for part in lexical.parts[1:]:
        current /= part
        if current.is_symlink():
            raise OSError(f"Investment workflow path contains a symlink: {current}")
        if not current.exists():
            break


def _require_safe_directory(path: Path, *, label: str) -> None:
    _reject_symlink_components(path)
    metadata = path.stat(follow_symlinks=False)
    if not stat.S_ISDIR(metadata.st_mode):
        raise NotADirectoryError(f"{label} is not a directory.")


@contextlib.contextmanager
def _pin_child_directory(
    parent: PinnedPrivateDirectory,
    name: str,
) -> Iterator[PinnedPrivateDirectory]:
    """Open one directory child relative to an already pinned parent."""
    child = Path(name)
    if not name or child.name != name or name in {".", ".."}:
        raise ValueError("Investment account folder name is invalid.")
    parent.validate()
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = -1
    try:
        descriptor = os.open(name, flags, dir_fd=parent.descriptor)
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise NotADirectoryError("Investment account source is not a directory.")
        pinned = PinnedPrivateDirectory(
            path=parent.path / name,
            descriptor=descriptor,
            identity=(metadata.st_dev, metadata.st_ino),
        )
        pinned.validate()
        yield pinned
        pinned.validate()
        parent.validate()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _directory_names(directory: PinnedPrivateDirectory) -> list[str]:
    """List one pinned directory without resolving its pathname again."""
    directory.validate()
    names = os.listdir(directory.descriptor)
    directory.validate()
    if not all(isinstance(name, str) for name in names):
        raise OSError("Investment source directory contains an invalid child name.")
    return sorted(names, key=str.casefold)


def _relative_metadata(directory: PinnedPrivateDirectory, name: str) -> os.stat_result:
    directory.validate()
    try:
        metadata = os.stat(name, dir_fd=directory.descriptor, follow_symlinks=False)
    except OSError as exc:
        raise OSError("Investment statement source is missing or unsafe.") from exc
    directory.validate()
    return metadata


def _safe_source_token(value: object) -> str:
    """Validate inert path components without loading a format adapter."""
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", value) is None
    ):
        raise ValueError("Investment source identifier is invalid.")
    return value


def _parse_statement(content: bytes, investment_format: InvestmentFormat) -> InvestmentStatement:
    try:
        statement = investment_format.parse(content)
    except Exception:
        raise AdapterError() from None
    if not isinstance(statement, InvestmentStatement):
        raise AdapterError("adapter_invalid_contract")
    validate_statement_contract(statement, investment_format.capabilities)
    if statement.account.institution.casefold() != investment_format.institution.casefold():
        raise AdapterError("adapter_invalid_contract")
    if (
        statement.account.status != "unknown"
        or statement.account.status_confirmed_on is not None
        or statement.account.closed_on is not None
        or statement.account.transfer_destination is not None
    ):
        raise ValueError("Investment statement contains unsupported lifecycle facts.")
    return statement


def _is_format_source(content: bytes, investment_format: InvestmentFormat) -> bool:
    return investment_format.recognizes_text(_extract_pinned_pdf_text(content))


def _discover_flat_sources(
    inbox_dir: Path,
    investment_format: InvestmentFormat,
) -> tuple[list[_StatementPair], list[_PinnedSource]]:
    """Read flat PDF sources through pinned bytes, leaving other statements alone."""
    statements: list[_StatementPair] = []
    opened: list[_PinnedSource] = []
    seen_hashes: set[str] = set()
    seen_periods: set[tuple[str, date, date]] = set()
    with pin_private_directory(inbox_dir) as inbox:
        try:
            for name in _directory_names(inbox):
                metadata = _relative_metadata(inbox, name)
                if stat.S_ISLNK(metadata.st_mode):
                    raise OSError("Investment inbox contains a symlink.")
                if Path(name).suffix.casefold() != ".pdf" or stat.S_ISDIR(metadata.st_mode):
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError("Investment statement source is not a regular file.")
                source = _pin_source(inbox, name, "", "statement_pdf")
                opened.append(source)
                relevant = _is_format_source(source.content, investment_format)
                _require_source_unchanged(source)
                if not relevant:
                    continue
                statement = _parse_statement(source.content, investment_format)
                alias = _safe_source_token(statement.account.account_alias)
                period_key = (
                    statement.account.account_id,
                    statement.period_start,
                    statement.period_end,
                )
                if source.sha256 in seen_hashes or period_key in seen_periods:
                    raise ValueError(
                        "Investment statement source set contains duplicate statements."
                    )
                seen_hashes.add(source.sha256)
                seen_periods.add(period_key)
                source = replace(
                    source,
                    alias=alias,
                    institution=investment_format.institution,
                    source_layout="flat",
                    period_start=statement.period_start,
                    period_end=statement.period_end,
                )
                _require_source_unchanged(source)
                statements.append(_StatementPair(alias, None, source, statement))
            for source in opened:
                _require_source_unchanged(source)
            inbox.validate()
        finally:
            for source in opened:
                os.close(source.descriptor)
    return [
        replace(pair, pdf=replace(pair.pdf, descriptor=-1, directory=None)) for pair in statements
    ], []


def _discover_sources(
    inbox_dir: Path,
    closed_aliases: tuple[str, ...],
    investment_format: InvestmentFormat,
) -> tuple[list[_StatementPair], list[_PinnedSource]]:
    pairs: list[_StatementPair] = []
    unmatched_pdfs: list[_PinnedSource] = []
    with pin_private_directory(inbox_dir) as inbox:
        account_dirs: dict[str, str] = {}
        for name in _directory_names(inbox):
            metadata = _relative_metadata(inbox, name)
            if stat.S_ISLNK(metadata.st_mode):
                raise OSError("Investment inbox contains a symlink.")
            if not stat.S_ISDIR(metadata.st_mode):
                if Path(name).suffix.casefold() in _SOURCE_SUFFIXES:
                    raise ValueError("Investment statement sources must be inside account folders.")
                continue
            alias = investment_format.normalize_alias(name).strip().upper()
            if closed_aliases and alias not in closed_aliases:
                continue
            if alias in account_dirs:
                raise ValueError("Multiple investment folders normalize to the same account alias.")
            account_dirs[alias] = name

        missing_dirs = set(closed_aliases) - set(account_dirs)
        if missing_dirs:
            raise ValueError("A configured investment account folder is missing.")

        for alias in closed_aliases or tuple(sorted(account_dirs)):
            with _pin_child_directory(inbox, account_dirs[alias]) as folder:
                opened_sources: list[_PinnedSource] = []
                try:
                    sources_by_stem: dict[str, dict[str, str]] = {}
                    for name in _directory_names(folder):
                        metadata = _relative_metadata(folder, name)
                        if stat.S_ISLNK(metadata.st_mode):
                            raise OSError("Investment statement source is a symlink.")
                        if stat.S_ISDIR(metadata.st_mode):
                            raise ValueError(
                                "Nested investment statement directories are unsupported."
                            )
                        source_name = Path(name)
                        if source_name.suffix.casefold() not in _SOURCE_SUFFIXES:
                            continue
                        if not stat.S_ISREG(metadata.st_mode):
                            raise ValueError("Investment statement source is not a regular file.")
                        suffix = source_name.suffix.casefold()
                        bucket = sources_by_stem.setdefault(source_name.stem, {})
                        if suffix in bucket:
                            raise ValueError("Investment statement pair is ambiguous.")
                        bucket[suffix] = name

                    for members in sources_by_stem.values():
                        csv_name = members.get(".csv")
                        pdf_name = members.get(".pdf")
                        if csv_name is None:
                            assert pdf_name is not None
                            evidence = _pin_source(folder, pdf_name, alias, "evidence_pdf")
                            opened_sources.append(evidence)
                            evidence = replace(
                                evidence,
                                institution=investment_format.institution,
                                source_layout="account_folders",
                            )
                            evidence_start, evidence_end = _evidence_period(
                                evidence, investment_format
                            )
                            unmatched_pdfs.append(
                                _with_period(evidence, evidence_start, evidence_end)
                            )
                            continue
                        if pdf_name is None:
                            raise ValueError("Investment CSV is missing its same-stem PDF pair.")
                        csv_source = _pin_source(folder, csv_name, alias, "csv")
                        opened_sources.append(csv_source)
                        pdf_source = _pin_source(folder, pdf_name, alias, "pdf")
                        opened_sources.append(pdf_source)
                        csv_source = replace(
                            csv_source,
                            institution=investment_format.institution,
                            source_layout="account_folders",
                        )
                        pdf_source = replace(
                            pdf_source,
                            institution=investment_format.institution,
                            source_layout="account_folders",
                        )
                        statement = _parse_statement(csv_source.content, investment_format)
                        if statement.account.account_alias.strip().upper() != alias:
                            raise ValueError(
                                "Investment statement alias does not match its account folder."
                            )
                        _require_paired_pdf_matches_statement(
                            pdf_source, statement, investment_format
                        )
                        csv_source = _with_period(
                            csv_source, statement.period_start, statement.period_end
                        )
                        pdf_source = _with_period(
                            pdf_source, statement.period_start, statement.period_end
                        )
                        _require_source_unchanged(csv_source)
                        _require_source_unchanged(pdf_source)
                        pairs.append(_StatementPair(alias, csv_source, pdf_source, statement))
                    for source in opened_sources:
                        _require_source_unchanged(source)
                finally:
                    for source in opened_sources:
                        if source.descriptor >= 0:
                            os.close(source.descriptor)
        inbox.validate()

    if len(unmatched_pdfs) > 1:
        raise ValueError("Investment inbox has ambiguous unmatched PDF evidence.")
    if unmatched_pdfs:
        evidence = unmatched_pdfs[0]
        if evidence.alias not in investment_format.evidence_only_aliases:
            raise ValueError("Unmatched PDF is not permitted overlapping evidence.")
        overlaps = [
            pair.statement
            for pair in pairs
            if pair.alias == evidence.alias
            and evidence.period_start is not None
            and evidence.period_end is not None
            and evidence.period_start <= pair.statement.period_end
            and pair.statement.period_start <= evidence.period_end
        ]
        if not overlaps:
            raise ValueError("Unmatched evidence PDF does not overlap a statement period.")
        accounts = {
            (statement.account.account_id, statement.account.source_account_id)
            for statement in overlaps
        }
        if len(accounts) != 1:
            raise ValueError("Unmatched evidence PDF has ambiguous overlapping accounts.")
        _, source_account_id = next(iter(accounts))
        _require_evidence_account_match(evidence, source_account_id, investment_format)
    sanitized_pairs = [
        _StatementPair(
            pair.alias,
            replace(pair.csv, descriptor=-1, directory=None) if pair.csv else None,
            replace(pair.pdf, descriptor=-1, directory=None),
            pair.statement,
        )
        for pair in pairs
    ]
    sanitized_evidence = [
        replace(source, descriptor=-1, directory=None) for source in unmatched_pdfs
    ]
    return sanitized_pairs, sanitized_evidence


def _pin_source(
    directory: PinnedPrivateDirectory,
    name: str,
    alias: str,
    role: Literal["csv", "pdf", "evidence_pdf", "statement_pdf"],
) -> _PinnedSource:
    path = directory.path / name
    descriptor = -1
    try:
        directory.validate()
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(name, flags, dir_fd=directory.descriptor)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("Investment source is not a regular file.")
        content = _read_descriptor_bytes(descriptor)
        after = os.fstat(descriptor)
        directory.validate()
        current = os.stat(name, dir_fd=directory.descriptor, follow_symlinks=False)
        before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        current_identity = (
            current.st_dev,
            current.st_ino,
            current.st_size,
            current.st_mtime_ns,
        )
        if before_identity != after_identity or after_identity != current_identity:
            raise RuntimeError("Investment statement source changed during staging.")
        return _PinnedSource(
            path=path,
            alias=alias,
            role=role,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=after.st_size,
            device=after.st_dev,
            inode=after.st_ino,
            modified_ns=after.st_mtime_ns,
            content=content,
            descriptor=descriptor,
            directory=directory,
        )
    except BaseException as exc:
        if descriptor >= 0:
            os.close(descriptor)
        if isinstance(exc, OSError):
            raise OSError("Investment statement source is missing or unsafe.") from exc
        raise


def _read_descriptor_bytes(descriptor: int) -> bytes:
    chunks: list[bytes] = []
    offset = 0
    while True:
        chunk = os.pread(descriptor, 1024 * 1024, offset)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)
        offset += len(chunk)


def _with_period(source: _PinnedSource, period_start: date, period_end: date) -> _PinnedSource:
    return replace(
        source,
        period_start=period_start,
        period_end=period_end,
    )


def _evidence_period(
    source: _PinnedSource, investment_format: InvestmentFormat
) -> tuple[date, date]:
    """Extract a source-owned period without retaining statement text."""
    text = source.content.decode("latin-1", errors="ignore")
    period = (
        investment_format.period_from_text(text)
        if investment_format.recognizes_text(text)
        else None
    )
    if period is None:
        text = _extract_pinned_pdf_text(source.content)
        period = (
            investment_format.period_from_text(text)
            if investment_format.recognizes_text(text)
            else None
        )
    _require_source_unchanged(source)
    if period is None or period[0] > period[1]:
        raise ValueError("Unable to verify the unmatched investment evidence PDF period.")
    return period


def _extract_pinned_pdf_text(content: bytes) -> str:
    """Extract text by feeding immutable source bytes through pdftotext stdin."""
    from ledger.imports.pdf.statement_pdf_to_csv import run_pdftotext_bytes

    return run_pdftotext_bytes(content)


def _require_evidence_account_match(
    source: _PinnedSource, source_account_id: str, investment_format: InvestmentFormat
) -> None:
    """Match the immutable evidence bytes to the sole overlapping account."""

    def matches_text(text: str) -> bool:
        return (
            investment_format.recognizes_text(text)
            and investment_format.period_from_text(text) == (source.period_start, source.period_end)
            and investment_format.matches_account(text, source_account_id)
        )

    try:
        matches = matches_text(source.content.decode("latin-1", errors="ignore"))
        if not matches:
            matches = matches_text(_extract_pinned_pdf_text(source.content))
    except Exception:
        raise ValueError("Unmatched evidence PDF does not match the overlapping account.") from None
    if not matches:
        raise ValueError("Unmatched evidence PDF does not match the overlapping account.")


def _require_paired_pdf_matches_statement(
    source: _PinnedSource,
    statement: InvestmentStatement,
    investment_format: InvestmentFormat,
) -> None:
    """Bind same-stem PDF evidence to the parsed institution, account, and period."""

    def matches_text(text: str) -> bool:
        return (
            investment_format.recognizes_text(text)
            and investment_format.period_from_text(text)
            == (statement.period_start, statement.period_end)
            and investment_format.matches_account(text, statement.account.source_account_id)
        )

    try:
        matches = matches_text(source.content.decode("latin-1", errors="ignore"))
        if not matches:
            matches = matches_text(_extract_pinned_pdf_text(source.content))
    except Exception:
        raise ValueError("Investment paired PDF does not match its structured CSV.") from None
    _require_source_unchanged(source)
    if (
        statement.account.institution.casefold() != investment_format.institution.casefold()
        or not matches
    ):
        raise ValueError("Investment paired PDF does not match its structured CSV.")


def _require_source_unchanged(source: _PinnedSource) -> None:
    descriptor = source.descriptor
    directory = source.directory
    if descriptor < 0 or directory is None:
        raise RuntimeError("Investment statement source binding is no longer available.")
    try:
        directory.validate()
        metadata = os.fstat(descriptor)
        identity = (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
        expected = (source.device, source.inode, source.size_bytes, source.modified_ns)
        digest, size = _hash_descriptor(descriptor)
        current = os.stat(
            source.path.name,
            dir_fd=directory.descriptor,
            follow_symlinks=False,
        )
        current_identity = (
            current.st_dev,
            current.st_ino,
            current.st_size,
            current.st_mtime_ns,
        )
        if (
            identity != expected
            or current_identity != expected
            or size != source.size_bytes
            or digest != source.sha256
        ):
            raise RuntimeError("Investment statement source changed during staging.")
    except OSError as exc:
        raise OSError("Investment statement source became missing or unsafe.") from exc


def _manifest_source(source: _PinnedSource, *, run_id: str) -> dict[str, Any]:
    archive_relative = _archive_relative_path(source)
    source_key = hashlib.sha256(
        f"{source.alias}:{source.role}:{source.sha256}".encode()
    ).hexdigest()
    artifact_token = hashlib.sha256(f"{run_id}:{source_key}".encode()).hexdigest()[:32]
    return {
        "source_key": source_key,
        "archive_artifact_token": artifact_token,
        "alias": source.alias,
        "institution": source.institution,
        "source_layout": source.source_layout,
        "source_name_hash": hashlib.sha256(source.path.name.encode("utf-8")).hexdigest(),
        "source_folder": source.path.parent.name
        if source.source_layout == "account_folders"
        else None,
        "role": source.role,
        "structured_import": source.role != "evidence_pdf",
        "sha256": source.sha256,
        "size_bytes": source.size_bytes,
        "period_start": source.period_start.isoformat() if source.period_start else None,
        "period_end": source.period_end.isoformat() if source.period_end else None,
        "archive_relative": str(archive_relative),
        "archive_status": "pending",
    }


def _archive_artifact_names(source: dict[str, Any]) -> _ArchiveArtifactNames:
    token = source.get("archive_artifact_token")
    if not isinstance(token, str) or re.fullmatch(r"[0-9a-f]{32}", token) is None:
        raise ValueError("Investment archive artifact identity is invalid.")
    return _ArchiveArtifactNames(
        source_recovery=f".investment-source-recovery-{token}",
        archive_candidate=f".investment-archive-candidate-{token}",
        archive_guard=f".investment-archive-guard-{token}",
    )


def _conflict_authorization_fingerprint(bindings: list[dict[str, Any]]) -> str:
    ordered = sorted(
        bindings,
        key=lambda item: (
            str(item.get("account_id")),
            str(item.get("period_start")),
            str(item.get("period_end")),
        ),
    )
    return hashlib.sha256(
        json.dumps(ordered, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _verify_conflict_authorization(
    manifest: dict[str, Any],
    pending_sources: dict[str, _LocatedSource],
    target_db: Path,
    staging_db: Path,
) -> None:
    review = manifest.get("conflict_review")
    if not isinstance(review, dict):
        raise RuntimeError("Investment conflict authorization changed after review.")
    expected = review.get("authorization_fingerprint")
    bindings = review.get("bindings")
    if not isinstance(expected, str) or not isinstance(bindings, list):
        raise RuntimeError("Investment conflict authorization changed after review.")
    if _conflict_authorization_fingerprint(bindings) != expected:
        raise RuntimeError("Investment conflict authorization changed after review.")
    with session_scope(staging_db) as session:
        run = session.get(InvestmentImportRunDB, str(manifest.get("run_id")))
        if run is None or run.conflict_authorization_fingerprint != expected:
            raise RuntimeError("Investment conflict authorization changed after review.")
    if not bindings:
        return
    with contextlib.closing(sqlite3.connect(staging_db)) as connection:
        from ledger.storage.migrations import is_legacy_investment_cash_schema

        legacy_review = is_legacy_investment_cash_schema(connection)
    if legacy_review:
        legacy_bindings = _legacy_reviewed_conflicts(
            manifest, pending_sources, target_db, staging_db
        )
        if _conflict_authorization_fingerprint(legacy_bindings) != expected:
            raise RuntimeError("Investment conflict authorization changed after review.")
        return
    investment_format = _verified_investment_format(manifest)
    repository = InvestmentRepository(target_db, initialize=False)
    recomputed: list[dict[str, Any]] = []
    for source in manifest.get("sources", []):
        if not isinstance(source, dict) or source.get("role") not in {"csv", "statement_pdf"}:
            continue
        source_key = str(source.get("source_key"))
        located = pending_sources.get(source_key)
        if located is None:
            raise RuntimeError("Investment conflict authorization changed after review.")
        content = _require_located_source_unchanged(located)
        statement = _parse_statement(content, investment_format)
        conflict = repository.conflict_delta(statement)
        if conflict is not None:
            recomputed.append(conflict[0])
    if _conflict_authorization_fingerprint(recomputed) != expected:
        raise RuntimeError("Investment conflict authorization changed after review.")


def _legacy_reviewed_conflicts(
    manifest: dict[str, Any],
    pending_sources: dict[str, _LocatedSource],
    target_db: Path,
    staging_db: Path,
) -> list[dict[str, Any]]:
    """Verify 0029 review bindings from its pinned aggregate and source association.

    The caller already verified the entire staging/target fingerprints and source
    bytes. A newer parser would produce a different aggregate fingerprint by adding
    cash evidence; retain the reviewed 0029 aggregate and its original source hash.
    """
    from ledger.storage.database import InvestmentSourceAssociationDB

    recomputed: list[dict[str, Any]] = []
    with session_scope(staging_db) as staging, session_scope(target_db) as target:
        for source in manifest.get("sources", []):
            if not isinstance(source, dict) or source.get("role") not in {"csv", "statement_pdf"}:
                continue
            located = pending_sources.get(str(source.get("source_key")))
            if located is None:
                raise RuntimeError("Investment conflict authorization changed after review.")
            _require_located_source_unchanged(located)
            association = (
                staging.query(InvestmentSourceAssociationDB)
                .filter(
                    InvestmentSourceAssociationDB.import_run_id == str(manifest["run_id"]),
                    InvestmentSourceAssociationDB.file_hash == source.get("sha256"),
                    InvestmentSourceAssociationDB.source_role == source.get("role"),
                    InvestmentSourceAssociationDB.superseded_at.is_(None),
                )
                .one_or_none()
            )
            statement = (
                staging.get(InvestmentStatementDB, association.statement_id)
                if association is not None
                else None
            )
            if (
                association is None
                or statement is None
                or statement.parsed_fingerprint != association.statement_fingerprint
            ):
                raise RuntimeError("Investment conflict authorization changed after review.")
            existing = (
                target.query(InvestmentStatementDB)
                .filter(
                    InvestmentStatementDB.account_id == statement.account_id,
                    InvestmentStatementDB.period_start == statement.period_start,
                    InvestmentStatementDB.period_end == statement.period_end,
                )
                .one_or_none()
            )
            if existing is not None and existing.parsed_fingerprint != statement.parsed_fingerprint:
                recomputed.append(
                    {
                        "account_id": statement.account_id,
                        "period_start": statement.period_start.isoformat(),
                        "period_end": statement.period_end.isoformat(),
                        "existing_fingerprint": existing.parsed_fingerprint,
                        "incoming_fingerprint": statement.parsed_fingerprint,
                    }
                )
    return recomputed


def _archive_relative_path(source: _PinnedSource) -> Path:
    institution = _safe_source_token(source.institution)
    alias = _safe_source_token(source.alias)
    assert source.period_start is not None and source.period_end is not None
    if source.source_layout == "flat":
        if source.role != "statement_pdf":
            raise ValueError("Investment archive source role is invalid.")
        filename = (
            f"{source.period_end.isoformat()}-{institution}-{alias.lower()}-"
            f"{source.period_start.isoformat()}-{source.sha256[:12]}.pdf"
        )
    elif source.source_layout == "account_folders":
        if source.role == "evidence_pdf":
            filename = f"{institution}-{alias.lower()}-evidence-only-{source.sha256[:12]}.pdf"
        elif source.role in {"csv", "pdf"}:
            suffix = ".csv" if source.role == "csv" else ".pdf"
            filename = (
                f"{institution}-{alias.lower()}-{source.period_start.isoformat()}_"
                f"{source.period_end.isoformat()}-{source.sha256[:12]}{suffix}"
            )
        else:
            raise ValueError("Investment archive source role is invalid.")
    else:
        raise ValueError("Investment archive source layout is invalid.")
    return Path("investments") / institution / alias / str(source.period_end.year) / filename


def _validate_staging_database(staging_db: Path, *, expected_statements: int) -> None:
    from ledger.storage.migrations import (
        is_legacy_investment_cash_schema,
        validate_investment_cash_balance_schema,
    )

    with contextlib.closing(sqlite3.connect(staging_db)) as connection:
        try:
            if not is_legacy_investment_cash_schema(connection):
                validate_investment_cash_balance_schema(connection)
                if (
                    connection.execute(
                        "SELECT 1 FROM schema_migrations WHERE version = ?",
                        ("20260913_0030_investment_cash_balances",),
                    ).fetchone()
                    is None
                ):
                    raise ValueError("Investment cash evidence migration is incomplete.")
        except ValueError:
            raise RuntimeError(
                "Investment staging database has invalid cash evidence storage."
            ) from None
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            raise RuntimeError("Investment staging database failed integrity validation.")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("Investment staging database failed relationship validation.")
    with session_scope(staging_db) as session:
        if session.query(InvestmentStatementDB).count() < expected_statements:
            raise RuntimeError("Investment staging database is missing parsed statements.")


def _require_fingerprint(
    value: Any,
    *,
    error: str,
) -> dict[str, Any]:
    fingerprint = value
    if not isinstance(fingerprint, dict):
        raise RuntimeError(error)
    tables = fingerprint.get("tables")
    if not isinstance(tables, dict):
        raise RuntimeError(error)
    for table, row_hash in tables.items():
        if (
            not isinstance(table, str)
            or not isinstance(row_hash, list)
            or len(row_hash) != 2
            or not isinstance(row_hash[0], int)
            or isinstance(row_hash[0], bool)
            or row_hash[0] < 0
            or not isinstance(row_hash[1], str)
            or re.fullmatch(r"[0-9a-f]{64}", row_hash[1]) is None
        ):
            raise RuntimeError(error)
    return fingerprint


def _require_manifest_fingerprint(
    manifest: dict[str, Any],
    field: str,
) -> dict[str, Any]:
    return _require_fingerprint(
        manifest.get(field),
        error="Investment recovery manifest fingerprint is missing or malformed.",
    )


def _require_manifest_statement_count(manifest: dict[str, Any]) -> int:
    counts = manifest.get("counts")
    statements = counts.get("statements") if isinstance(counts, dict) else None
    if not isinstance(statements, int) or isinstance(statements, bool) or statements < 0:
        raise RuntimeError("Investment recovery manifest counts are missing or malformed.")
    return statements


_ARCHIVE_AUDIT_TABLES = frozenset(
    {
        "investment_import_runs",
        "investment_source_files",
        "investment_source_associations",
    }
)
_RUN_ARCHIVE_MUTABLE_COLUMNS = frozenset(
    {
        "status",
        "updated_at",
        "archive_pending_at",
        "finalized_at",
        "target_fingerprint",
        "redacted_failure",
        "errors",
    }
)
_SOURCE_ARCHIVE_MUTABLE_COLUMNS = frozenset(
    {
        "archived_path",
        "archive_status",
        "archived_at",
        "finalized_at",
        "updated_at",
        "status",
    }
)


def _fingerprint_without_archive_audit_tables(
    fingerprint: dict[str, Any],
) -> dict[str, Any]:
    tables = fingerprint.get("tables")
    if not isinstance(tables, dict) or not set(tables) >= _ARCHIVE_AUDIT_TABLES:
        raise RuntimeError("Investment archive recovery fingerprint is malformed.")
    return {
        "tables": {
            table: row_hash
            for table, row_hash in tables.items()
            if table not in _ARCHIVE_AUDIT_TABLES
        }
    }


_STAGING_TRANSITIONS = frozenset(
    {
        ("staged", "finalizing"),
        ("finalizing", "staged"),
    }
)
_TRANSITION_RUN_MUTABLE_COLUMNS = {
    ("staged", "finalizing"): frozenset(
        {"status", "updated_at", "finalizing_at", "redacted_failure", "errors"}
    ),
    ("finalizing", "staged"): frozenset(
        {"status", "updated_at", "staged_at", "redacted_failure", "errors"}
    ),
}
_TRANSITION_AUDIT_CONTRACT_SCHEMA = "investment-staging-transition-audit"
_TRANSITION_AUDIT_CONTRACT_VERSION = 1
_TRANSITION_AUDIT_DIGEST_FIELDS = frozenset(
    {
        "source_files_sha256",
        "source_associations_sha256",
        "unrelated_runs_sha256",
        "run_before_sha256",
        "run_immutable_sha256",
    }
)


def _canonical_contract_digest(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _transition_audit_rows(
    connection: sqlite3.Connection,
    run_id: str,
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[str, Any],
]:
    audit_rows = _archive_audit_contract(connection)
    runs = dict(audit_rows["investment_import_runs"])
    run = runs.pop(run_id, None)
    if run is None:
        raise RuntimeError("Investment staging transition audit row is unavailable.")
    return (
        audit_rows["investment_source_files"],
        audit_rows["investment_source_associations"],
        runs,
        run,
    )


def _transition_run_immutable_row(
    run: dict[str, Any],
    transition: tuple[str, str],
) -> dict[str, Any]:
    mutable_columns = _TRANSITION_RUN_MUTABLE_COLUMNS.get(transition)
    if mutable_columns is None:
        raise RuntimeError("Investment staging transition marker is invalid.")
    return {column: value for column, value in run.items() if column not in mutable_columns}


def _build_transition_audit_contract(
    connection: sqlite3.Connection,
    run_id: str,
    transition: tuple[str, str],
) -> dict[str, Any]:
    source_files, source_associations, unrelated_runs, run = _transition_audit_rows(
        connection, run_id
    )
    if run.get("status") != transition[0]:
        raise RuntimeError("Investment staging transition audit state is invalid.")
    try:
        return {
            "schema": _TRANSITION_AUDIT_CONTRACT_SCHEMA,
            "version": _TRANSITION_AUDIT_CONTRACT_VERSION,
            "source_files_sha256": _canonical_contract_digest(source_files),
            "source_associations_sha256": _canonical_contract_digest(source_associations),
            "unrelated_runs_sha256": _canonical_contract_digest(unrelated_runs),
            "run_before_sha256": _canonical_contract_digest(run),
            "run_immutable_sha256": _canonical_contract_digest(
                _transition_run_immutable_row(run, transition)
            ),
        }
    except TypeError, ValueError:
        raise RuntimeError("Investment staging transition audit rows are invalid.") from None


def _require_transition_audit_contract(value: object) -> dict[str, Any]:
    expected_fields = {"schema", "version", *_TRANSITION_AUDIT_DIGEST_FIELDS}
    if (
        not isinstance(value, dict)
        or set(value) != expected_fields
        or value.get("schema") != _TRANSITION_AUDIT_CONTRACT_SCHEMA
        or type(value.get("version")) is not int
        or value.get("version") != _TRANSITION_AUDIT_CONTRACT_VERSION
        or any(
            not isinstance(value.get(field), str)
            or re.fullmatch(r"[0-9a-f]{64}", value[field]) is None
            for field in _TRANSITION_AUDIT_DIGEST_FIELDS
        )
    ):
        raise RuntimeError("Investment staging transition audit contract is missing or malformed.")
    return value


def _require_transition_run_lifecycle(
    run: dict[str, Any],
    transition: tuple[str, str],
) -> None:
    if transition == ("staged", "finalizing"):
        valid = (
            run.get("status") == "finalizing"
            and run.get("finalizing_at") is not None
            and run.get("updated_at") is not None
            and run.get("redacted_failure") is None
            and run.get("errors") == 0
        )
    elif transition == ("finalizing", "staged"):
        valid = (
            run.get("status") == "staged"
            and run.get("staged_at") is not None
            and run.get("updated_at") is not None
            and run.get("redacted_failure") == "publication_failed"
            and run.get("errors") == 1
        )
    else:
        valid = False
    if not valid:
        raise RuntimeError("Investment staging transition audit state is invalid.")


def _require_transition_audit_unchanged(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    transition: tuple[str, str],
    expected: dict[str, Any],
) -> str:
    source_files, source_associations, unrelated_runs, run = _transition_audit_rows(
        connection, run_id
    )
    try:
        current_digests = {
            "source_files_sha256": _canonical_contract_digest(source_files),
            "source_associations_sha256": _canonical_contract_digest(source_associations),
            "unrelated_runs_sha256": _canonical_contract_digest(unrelated_runs),
            "run_before_sha256": _canonical_contract_digest(run),
            "run_immutable_sha256": _canonical_contract_digest(
                _transition_run_immutable_row(run, transition)
            ),
        }
    except TypeError, ValueError:
        raise RuntimeError("Investment staging transition audit has recovery drift.") from None
    if any(
        current_digests[field] != expected[field]
        for field in (
            "source_files_sha256",
            "source_associations_sha256",
            "unrelated_runs_sha256",
            "run_immutable_sha256",
        )
    ):
        raise RuntimeError("Investment staging transition audit has recovery drift.")

    status = run.get("status")
    if status == transition[0]:
        if current_digests["run_before_sha256"] != expected["run_before_sha256"]:
            raise RuntimeError("Investment staging transition audit has recovery drift.")
    elif status == transition[1]:
        _require_transition_run_lifecycle(run, transition)
    else:
        raise RuntimeError("Investment staging transition audit state is invalid.")
    return status


@contextlib.contextmanager
def _staging_transition_snapshot(
    staging_db: Path,
) -> Iterator[sqlite3.Connection]:
    if not staging_db.is_file() or staging_db.is_symlink():
        raise RuntimeError("Investment staging database is missing or unsafe.")
    with contextlib.closing(sqlite3.connect(staging_db)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()


def _require_transition_staging_unchanged(
    connection: sqlite3.Connection,
    manifest: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
        raise RuntimeError("Investment staging database failed integrity validation.")
    if connection.execute("PRAGMA foreign_key_check").fetchall():
        raise RuntimeError("Investment staging database failed relationship validation.")
    statement_count = connection.execute("SELECT COUNT(*) FROM investment_statements").fetchone()
    if statement_count is None or int(statement_count[0]) < _require_manifest_statement_count(
        manifest
    ):
        raise RuntimeError("Investment staging database is missing parsed statements.")
    expected_owned = _fingerprint_without_archive_audit_tables(
        _require_manifest_fingerprint(manifest, "staging_import_owned")
    )
    current_owned = import_owned_fingerprint(connection)
    current_immutable_owned = _fingerprint_without_archive_audit_tables(current_owned)
    expected_preserved = _require_manifest_fingerprint(manifest, "staging_non_investment")
    current_preserved = investment_preserved_fingerprint(connection)
    if not import_owned_match(
        expected_owned, current_immutable_owned
    ) or not investment_preserved_match(expected_preserved, current_preserved):
        raise RuntimeError("Investment staging transition has immutable fingerprint drift.")
    return current_owned, current_preserved


def _require_resolved_staging_unchanged(
    connection: sqlite3.Connection,
    manifest: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    current_owned, current_preserved = _require_transition_staging_unchanged(connection, manifest)
    expected_owned = _require_manifest_fingerprint(manifest, "staging_import_owned")
    expected_preserved = _require_manifest_fingerprint(manifest, "staging_non_investment")
    if not import_owned_match(expected_owned, current_owned) or not investment_preserved_match(
        expected_preserved, current_preserved
    ):
        raise RuntimeError("Investment staging database has resolved fingerprint drift.")
    return current_owned, current_preserved


def _snapshot_resolved_staging(
    connection: sqlite3.Connection,
    staging_db: Path,
    candidate: Path,
    manifest: dict[str, Any],
) -> None:
    expected_owned, expected_preserved = _require_resolved_staging_unchanged(connection, manifest)
    snapshot_database(staging_db, candidate)
    if not candidate.is_file() or candidate.is_symlink():
        raise RuntimeError("Investment promotion candidate is missing or unsafe.")
    candidate_uri = f"{candidate.as_uri()}?mode=ro"
    with contextlib.closing(sqlite3.connect(candidate_uri, uri=True)) as candidate_connection:
        candidate_owned, candidate_preserved = _require_resolved_staging_unchanged(
            candidate_connection, manifest
        )
    if not import_owned_match(expected_owned, candidate_owned) or not investment_preserved_match(
        expected_preserved, candidate_preserved
    ):
        raise RuntimeError("Investment promotion candidate has snapshot drift.")


def _reconcile_staging_transition(run_dir: Path, manifest: dict[str, Any]) -> None:
    transition = manifest.get("staging_transition")
    if transition is None:
        return
    if not isinstance(transition, dict):
        raise RuntimeError("Investment staging transition marker is invalid.")
    from_status = transition.get("from")
    to_status = transition.get("to")
    transition_pair = (from_status, to_status)
    if (
        not isinstance(from_status, str)
        or not isinstance(to_status, str)
        or transition_pair not in _STAGING_TRANSITIONS
        or manifest.get("status") != from_status
    ):
        raise RuntimeError("Investment staging transition marker is invalid.")
    last_error = transition.get("last_error")
    expected_last_error = (
        None if transition_pair == ("staged", "finalizing") else "publication_failed"
    )
    if last_error != expected_last_error:
        raise RuntimeError("Investment staging transition marker is invalid.")
    expected_audit = _require_transition_audit_contract(transition.get("audit_contract"))

    staging_db = run_dir / _STAGING_DB_NAME
    with _staging_transition_snapshot(staging_db) as connection:
        database_status = _require_transition_audit_unchanged(
            connection,
            run_id=str(manifest["run_id"]),
            transition=transition_pair,
            expected=expected_audit,
        )
        current_owned, current_preserved = _require_transition_staging_unchanged(
            connection, manifest
        )
        if database_status == to_status:
            manifest["status"] = to_status
            manifest["last_error"] = last_error
            manifest["staging_import_owned"] = current_owned
            manifest["staging_non_investment"] = current_preserved
        manifest.pop("staging_transition", None)
        _write_manifest(run_dir, manifest)


def _transition_staging_run(
    run_dir: Path,
    manifest: dict[str, Any],
    repository: InvestmentRepository,
    *,
    from_status: str,
    to_status: str,
    last_error: str | None,
    redacted_failure: str | None,
    errors: int = 0,
    promotion_candidate: Path | None = None,
) -> None:
    transition_pair = (from_status, to_status)
    if transition_pair not in _STAGING_TRANSITIONS:
        raise RuntimeError("Investment staging transition is unsupported.")
    if promotion_candidate is not None and transition_pair != ("staged", "finalizing"):
        raise RuntimeError("Investment promotion candidate transition is invalid.")
    if manifest.get("status") != from_status or "staging_transition" in manifest:
        raise RuntimeError("Investment staging transition state is invalid.")
    staging_db = run_dir / _STAGING_DB_NAME
    with _staging_transition_snapshot(staging_db) as connection:
        _require_resolved_staging_unchanged(connection, manifest)
        audit_contract = _build_transition_audit_contract(
            connection,
            str(manifest["run_id"]),
            transition_pair,
        )
        manifest["staging_transition"] = {
            "from": from_status,
            "to": to_status,
            "last_error": last_error,
            "audit_contract": audit_contract,
        }
        _write_manifest(run_dir, manifest)
    repository.update_import_run(
        str(manifest["run_id"]),
        to_status,
        redacted_failure=redacted_failure,
        errors=errors,
    )
    with _staging_transition_snapshot(staging_db) as connection:
        database_status = _require_transition_audit_unchanged(
            connection,
            run_id=str(manifest["run_id"]),
            transition=transition_pair,
            expected=audit_contract,
        )
        current_owned, current_preserved = _require_transition_staging_unchanged(
            connection, manifest
        )
        if database_status != to_status:
            raise RuntimeError("Investment staging transition audit state is invalid.")
        manifest["status"] = to_status
        manifest["last_error"] = last_error
        manifest["staging_import_owned"] = current_owned
        manifest["staging_non_investment"] = current_preserved
        manifest.pop("staging_transition", None)
        _write_manifest(run_dir, manifest)
        if promotion_candidate is not None:
            _snapshot_resolved_staging(
                connection,
                staging_db,
                promotion_candidate,
                manifest,
            )


def _table_rows_by_key(
    connection: sqlite3.Connection,
    table: str,
    key_column: str,
) -> dict[str, dict[str, Any]]:
    columns = [str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")]
    if key_column not in columns:
        raise RuntimeError("Investment archive audit schema is invalid.")
    rows: dict[str, dict[str, Any]] = {}
    for values in connection.execute(f"SELECT * FROM {table}").fetchall():
        row = dict(zip(columns, values, strict=True))
        key = row.get(key_column)
        if not isinstance(key, str) or key in rows:
            raise RuntimeError("Investment archive audit rows are invalid.")
        rows[key] = row
    return rows


def _archive_audit_contract(
    connection: sqlite3.Connection,
) -> dict[str, dict[str, dict[str, Any]]]:
    return {
        "investment_import_runs": _table_rows_by_key(
            connection, "investment_import_runs", "run_id"
        ),
        "investment_source_files": _table_rows_by_key(
            connection, "investment_source_files", "file_hash"
        ),
        "investment_source_associations": _table_rows_by_key(
            connection, "investment_source_associations", "association_id"
        ),
    }


def _require_immutable_row_columns(
    expected: dict[str, Any],
    current: dict[str, Any],
    *,
    mutable_columns: frozenset[str],
) -> None:
    if set(expected) != set(current) or any(
        current[column] != value
        for column, value in expected.items()
        if column not in mutable_columns
    ):
        raise RuntimeError("Investment archive audit rows have recovery drift.")


def _require_run_archive_state(
    expected: dict[str, Any],
    current: dict[str, Any],
    *,
    all_sources_archived: bool,
    current_immutable_owned: dict[str, Any],
) -> None:
    _require_immutable_row_columns(
        expected,
        current,
        mutable_columns=_RUN_ARCHIVE_MUTABLE_COLUMNS,
    )
    status = current.get("status")
    if status not in {"finalizing", "archive_pending", "finalized"}:
        raise RuntimeError("Investment archive run audit state is invalid.")
    if current.get("updated_at") is None:
        raise RuntimeError("Investment archive run audit state is invalid.")
    if status == "finalizing":
        if (
            all_sources_archived
            or current.get("archive_pending_at") is not None
            or current.get("finalized_at") is not None
            or current.get("target_fingerprint") != expected.get("target_fingerprint")
            or current.get("redacted_failure") is not None
            or current.get("errors") != 0
        ):
            raise RuntimeError("Investment archive run audit state is invalid.")
        return
    if current.get("archive_pending_at") is None:
        raise RuntimeError("Investment archive run audit state is invalid.")
    if status == "archive_pending":
        if (
            current.get("finalized_at") is not None
            or current.get("target_fingerprint") != expected.get("target_fingerprint")
            or current.get("redacted_failure") not in {None, "archive_failed"}
            or current.get("errors") not in {0, 1}
        ):
            raise RuntimeError("Investment archive run audit state is invalid.")
        return
    raw_target_fingerprint = current.get("target_fingerprint")
    try:
        target_fingerprint = _require_fingerprint(
            json.loads(raw_target_fingerprint)
            if isinstance(raw_target_fingerprint, str)
            else raw_target_fingerprint,
            error="Investment archive run audit state is invalid.",
        )
        target_immutable_owned = _fingerprint_without_archive_audit_tables(target_fingerprint)
    except json.JSONDecodeError, TypeError:
        raise RuntimeError("Investment archive run audit state is invalid.") from None
    if (
        not all_sources_archived
        or current.get("finalized_at") is None
        or not import_owned_match(target_immutable_owned, current_immutable_owned)
        or current.get("redacted_failure") is not None
        or current.get("errors") != 0
    ):
        raise RuntimeError("Investment archive run audit state is invalid.")


def _require_source_archive_state(
    expected: dict[str, Any],
    current: dict[str, Any],
    source: dict[str, Any],
    *,
    processed_dir: Path,
) -> bool:
    _require_immutable_row_columns(
        expected,
        current,
        mutable_columns=_SOURCE_ARCHIVE_MUTABLE_COLUMNS,
    )
    expected_hash = str(source.get("sha256"))
    expected_size = int(source.get("size_bytes", -1))
    source_path = Path(str(expected.get("source_path", "")))
    archive_path = _archive_target(processed_dir, source)
    source_recovery_path = source_path.parent / _archive_artifact_names(source).source_recovery
    if source.get("source_layout") == "flat":
        # A content-bound flat source may have been renamed before publication.
        # The historical path may now contain an unrelated statement; resolve the
        # reviewed bytes first without changing the immutable source audit.
        source_exists = False
        with pin_existing_directory(source_path.parent) as directory:
            matches = _matching_source_names(directory, source)
            if len(matches) > 1:
                raise RuntimeError("Investment archive source identity is ambiguous.")
            if matches:
                source_exists = _verify_existing_file_readonly(
                    directory.path / matches[0][0],
                    expected_hash=expected_hash,
                    expected_size=expected_size,
                    label="Investment archive source state",
                )
            directory.validate()
    else:
        source_exists = _verify_existing_file_readonly(
            source_path,
            expected_hash=expected_hash,
            expected_size=expected_size,
            label="Investment archive source state",
        )
    archive_exists = _verify_existing_file_readonly(
        archive_path,
        expected_hash=expected_hash,
        expected_size=expected_size,
        label="Investment archive state",
    )
    source_recovery_exists = _verify_existing_file_readonly(
        source_recovery_path,
        expected_hash=expected_hash,
        expected_size=expected_size,
        label="Investment archive source recovery state",
    )
    if not source_exists and not archive_exists and not source_recovery_exists:
        raise RuntimeError("Investment archive source state has recovery drift.")

    database_archived = current.get("archive_status") == "archived"
    if database_archived:
        if (
            current.get("status") != "finalized"
            or current.get("archived_path") != str(archive_path)
            or current.get("archived_at") is None
            or current.get("finalized_at") is None
            or current.get("updated_at") is None
            or not archive_exists
            or source_exists
            or source_recovery_exists
        ):
            raise RuntimeError("Investment archive source audit state is invalid.")
    elif (
        current.get("archive_status") != "pending"
        or current.get("status") != "staged"
        or current.get("archived_path") is not None
        or current.get("archived_at") is not None
        or current.get("finalized_at") is not None
    ):
        raise RuntimeError("Investment archive source audit state is invalid.")

    manifest_archived = source.get("archive_status") == "archived"
    if source.get("archive_status") not in {"pending", "archived"}:
        raise RuntimeError("Investment archive manifest source state is invalid.")
    if (database_archived or manifest_archived) and (
        not archive_exists or source_exists or source_recovery_exists
    ):
        raise RuntimeError("Investment archive source state has recovery drift.")
    return database_archived


def _require_archive_audit_consistency(
    staging_contract: dict[str, dict[str, dict[str, Any]]],
    current_contract: dict[str, dict[str, dict[str, Any]]],
    manifest: dict[str, Any],
    *,
    processed_dir: Path,
    current_immutable_owned: dict[str, Any],
) -> None:
    expected_runs = staging_contract["investment_import_runs"]
    current_runs = current_contract["investment_import_runs"]
    expected_sources = staging_contract["investment_source_files"]
    current_sources = current_contract["investment_source_files"]
    expected_associations = staging_contract["investment_source_associations"]
    current_associations = current_contract["investment_source_associations"]
    if (
        set(expected_runs) != set(current_runs)
        or set(expected_sources) != set(current_sources)
        or set(expected_associations) != set(current_associations)
    ):
        raise RuntimeError("Investment archive audit rows have recovery drift.")

    run_id = manifest.get("run_id")
    sources = manifest.get("sources")
    if not isinstance(run_id, str) or not isinstance(sources, list):
        raise RuntimeError("Investment archive manifest source state is invalid.")
    source_by_hash: dict[str, dict[str, Any]] = {}
    for source in sources:
        if not isinstance(source, dict):
            raise RuntimeError("Investment archive manifest source state is invalid.")
        file_hash = source.get("sha256")
        if not isinstance(file_hash, str) or file_hash in source_by_hash:
            raise RuntimeError("Investment archive manifest source state is invalid.")
        source_by_hash[file_hash] = source
    expected_run = expected_runs.get(run_id)
    current_run = current_runs.get(run_id)
    if expected_run is None or current_run is None:
        raise RuntimeError("Investment archive run audit state is invalid.")

    for other_run_id, expected_row in expected_runs.items():
        if other_run_id != run_id and current_runs[other_run_id] != expected_row:
            raise RuntimeError("Investment archive audit rows have recovery drift.")
    current_run_source_hashes = {
        file_hash
        for file_hash, row in current_sources.items()
        if row.get("import_run_id") == run_id
    }
    if current_run_source_hashes != set(source_by_hash):
        raise RuntimeError("Investment archive source audit rows have recovery drift.")
    current_run_associations = {
        str(row.get("file_hash")): association_id
        for association_id, row in current_associations.items()
        if row.get("import_run_id") == run_id
    }
    if set(current_run_associations) != set(source_by_hash):
        raise RuntimeError("Investment archive source association rows have recovery drift.")

    database_archived: list[bool] = []
    for file_hash, expected_row in expected_sources.items():
        current_row = current_sources[file_hash]
        source = source_by_hash.get(file_hash)
        if source is None:
            if current_row != expected_row:
                raise RuntimeError("Investment archive audit rows have recovery drift.")
            continue
        database_archived.append(
            _require_source_archive_state(
                expected_row,
                current_row,
                source,
                processed_dir=processed_dir,
            )
        )
    for association_id, expected_row in expected_associations.items():
        current_row = current_associations[association_id]
        source = source_by_hash.get(str(expected_row.get("file_hash")))
        if expected_row.get("import_run_id") != run_id:
            if current_row != expected_row:
                raise RuntimeError("Investment archive audit rows have recovery drift.")
            continue
        if source is None:
            raise RuntimeError("Investment archive source association rows have recovery drift.")
        database_archived.append(
            _require_source_archive_state(
                expected_row,
                current_row,
                source,
                processed_dir=processed_dir,
            )
        )
    all_sources_archived = bool(database_archived) and all(database_archived)
    _require_run_archive_state(
        expected_run,
        current_run,
        all_sources_archived=all_sources_archived,
        current_immutable_owned=current_immutable_owned,
    )


def _validate_archive_pending_target(
    manifest: dict[str, Any],
    paths: dict[str, Path],
    *,
    staging_db: Path,
) -> None:
    """Require complete immutable publication plus bounded archive audit progress."""
    expected_owned = _require_manifest_fingerprint(manifest, "staging_import_owned")
    expected_preserved = _require_manifest_fingerprint(manifest, "staging_non_investment")
    _validate_staging_database(
        staging_db,
        expected_statements=_require_manifest_statement_count(manifest),
    )
    current_staging_owned = import_owned_fingerprint(staging_db)
    current_staging_preserved = investment_preserved_fingerprint(staging_db)
    if not import_owned_match(
        expected_owned, current_staging_owned
    ) or not investment_preserved_match(expected_preserved, current_staging_preserved):
        raise RuntimeError("Investment staging database has recovery fingerprint drift.")
    with contextlib.closing(sqlite3.connect(staging_db)) as staging_connection:
        staging_contract = _archive_audit_contract(staging_connection)
    expected_immutable_owned = _fingerprint_without_archive_audit_tables(expected_owned)

    def require_published_state(connection: sqlite3.Connection) -> None:
        current_owned = import_owned_fingerprint(connection)
        current_preserved = investment_preserved_fingerprint(connection)
        current_immutable_owned = _fingerprint_without_archive_audit_tables(current_owned)
        if (
            not import_owned_match(expected_immutable_owned, current_immutable_owned)
            or not investment_preserved_match(expected_preserved, current_preserved)
            or connection.execute("PRAGMA foreign_key_check").fetchall()
        ):
            raise RuntimeError("Investment archive target has recovery fingerprint drift.")
        _restore_legacy_published_metadata(manifest, staging_db)
        _require_archive_audit_consistency(
            staging_contract,
            _archive_audit_contract(connection),
            manifest,
            processed_dir=paths["processed_dir"],
            current_immutable_owned=current_immutable_owned,
        )

    with (
        database_target_lock(paths["db_path"]),
        validated_database_target(
            paths["db_path"],
            label="Investment archive target",
            guard=require_published_state,
        ),
    ):
        pass


def _write_manifest(run_dir: Path, payload: dict[str, Any]) -> None:
    payload["updated_at"] = utc_now()
    write_json_atomic(run_dir / _MANIFEST_NAME, payload)


def _read_manifest(run_dir: Path) -> dict[str, Any]:
    descriptor = -1
    try:
        with pin_private_directory(run_dir) as pinned:
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(_MANIFEST_NAME, flags, dir_fd=pinned.descriptor)
            except OSError as exc:
                raise OSError("Investment import manifest is missing or unsafe.") from exc
            metadata = os.fstat(descriptor)
            identity = (metadata.st_dev, metadata.st_ino)
            if not stat.S_ISREG(metadata.st_mode):
                raise OSError("Investment import manifest is not a regular file.")
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                raise OSError("Investment import manifest permissions must be 0600.")
            with os.fdopen(os.dup(descriptor), "r", encoding="utf-8") as handle:
                raw_payload = handle.read()
            pinned.validate()
            current = os.stat(
                _MANIFEST_NAME,
                dir_fd=pinned.descriptor,
                follow_symlinks=False,
            )
            final = os.fstat(descriptor)
            if (
                not stat.S_ISREG(current.st_mode)
                or not stat.S_ISREG(final.st_mode)
                or (current.st_dev, current.st_ino) != identity
                or (final.st_dev, final.st_ino) != identity
                or stat.S_IMODE(current.st_mode) != 0o600
                or stat.S_IMODE(final.st_mode) != 0o600
            ):
                raise OSError("Investment import manifest changed during read.")
        payload = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        raise ValueError("Investment import manifest is missing or invalid.") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(payload, dict) or payload.get("version") != _MANIFEST_VERSION:
        raise ValueError("Investment import manifest version is unsupported.")
    run_id = payload.get("run_id")
    if not isinstance(run_id, str) or run_id != run_dir.name:
        raise ValueError("Investment import run directory binding is invalid.")
    expected_manifest = (run_dir / _MANIFEST_NAME).resolve()
    raw_manifest_path = payload.get("manifest_path")
    if not isinstance(raw_manifest_path, str) or not raw_manifest_path:
        raise ValueError("Investment import manifest path binding is invalid.")
    stored_manifest = _absolute(Path(raw_manifest_path))
    _reject_symlink_components(stored_manifest)
    if stored_manifest.resolve() != expected_manifest:
        raise ValueError("Investment import manifest path binding is invalid.")

    staging_db = run_dir / _STAGING_DB_NAME
    if staging_db.exists():
        if staging_db.is_symlink() or not staging_db.is_file():
            raise ValueError("Investment import manifest path binding is invalid.")
        with session_scope(staging_db) as session:
            run = session.get(InvestmentImportRunDB, run_id)
            if run is None or not run.private_manifest_path:
                raise ValueError("Investment import manifest path binding is invalid.")
            database_manifest = _absolute(Path(run.private_manifest_path))
            _reject_symlink_components(database_manifest)
            if database_manifest.resolve() != expected_manifest:
                raise ValueError("Investment import manifest path binding is invalid.")
            lifecycle = run.lifecycle_inputs or {}
            adapter_binding = lifecycle.get("adapter_binding_fingerprint")
            if (adapter_binding is not None or "adapter_binding" in payload) and (
                adapter_binding is None
                or adapter_binding != _interpretation_binding_fingerprint(payload)
                or payload.get("lifecycle", {}).get("adapter_binding_fingerprint")
                != adapter_binding
            ):
                raise AdapterError("import_binding_changed")
            source_binding = lifecycle.get("source_binding_fingerprint")
            if source_binding is not None and source_binding != _flat_source_binding_fingerprint(
                payload
            ):
                raise ValueError("Investment import source binding changed after staging.")
    return payload


def _resolve_and_validate_run(runs_dir: Path, run_dir: Path | None) -> Path:
    if run_dir is not None:
        lexical = _absolute(run_dir)
        _reject_symlink_components(lexical)
        resolved = lexical.resolve()
        if not resolved.is_dir():
            raise ValueError("Investment import run directory was not found.")
        return resolved
    runs_root = _absolute(runs_dir).resolve()
    resolved = resolve_run_dir(runs_root, None)
    try:
        relative = resolved.relative_to(runs_root)
    except ValueError:
        raise ValueError(
            "Investment import run is outside the configured runs directory."
        ) from None
    if relative == Path(".") or not resolved.is_dir():
        raise ValueError("Investment import run directory was not found.")
    _reject_symlink_components(resolved)
    return resolved


def _manifest_paths(manifest: dict[str, Any]) -> dict[str, Path]:
    raw_paths = manifest.get("paths")
    if not isinstance(raw_paths, dict):
        raise ValueError("Investment import manifest paths are invalid.")
    required = ("inbox_dir", "db_path", "processed_dir", "backup_dir")
    paths: dict[str, Path] = {}
    for key in required:
        raw = raw_paths.get(key)
        if not isinstance(raw, str) or not raw:
            raise ValueError("Investment import manifest paths are invalid.")
        path = _absolute(Path(raw))
        _reject_symlink_components(path)
        paths[key] = path
    return paths


def _pending_source_state(
    manifest: dict[str, Any],
) -> tuple[
    tuple[str, ...],
    list[dict[str, Any]],
    dict[str, dict[str, Any]],
    dict[tuple[str, str, str], list[dict[str, Any]]],
]:
    sources = manifest.get("sources")
    lifecycle = manifest.get("lifecycle")
    if not isinstance(sources, list) or not isinstance(lifecycle, dict):
        raise ValueError("Investment import manifest source metadata is invalid.")
    aliases_raw = lifecycle.get("closed_aliases")
    if not isinstance(aliases_raw, list) or not all(isinstance(item, str) for item in aliases_raw):
        raise ValueError("Investment import manifest lifecycle metadata is invalid.")
    _manifest_source_layout(manifest)
    # Source selection and lifecycle confirmation are independent. Open/unknown
    # account imports still bind and revalidate every selected source alias.
    aliases = tuple(sorted({_safe_source_token(str(source.get("alias"))) for source in sources}))

    pending = {
        source["source_key"]: source
        for source in sources
        if source.get("archive_status") != "archived"
    }
    by_identity: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for source in pending.values():
        role = source.get("role")
        suffix = ".csv" if role == "csv" else ".pdf"
        identity_key = (str(source.get("alias")), suffix, str(source.get("sha256")))
        by_identity.setdefault(identity_key, []).append(source)
    return aliases, sources, pending, by_identity


def _flat_source_binding_fingerprint(manifest: dict[str, Any]) -> str:
    """Bind paths and immutable PDF metadata to the fingerprinted staging run."""
    sources = manifest.get("sources")
    if not isinstance(sources, list) or not all(isinstance(source, dict) for source in sources):
        raise ValueError("Investment import flat source binding is invalid.")
    keys: tuple[str, ...] = (
        "source_key",
        "archive_artifact_token",
        "institution",
        "source_layout",
        "alias",
        "role",
        "structured_import",
        "sha256",
        "size_bytes",
        "period_start",
        "period_end",
        "archive_relative",
    )
    if any("source_folder" in source for source in sources):
        keys += ("source_folder",)
    if any("source_name_hash" in source for source in sources):
        keys += ("source_name_hash",)
    binding = {
        "institution": manifest.get("institution"),
        "source_layout": manifest.get("source_layout"),
        "paths": manifest.get("paths"),
        "sources": [{key: source.get(key) for key in keys} for source in sources],
    }
    return hashlib.sha256(
        json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _interpretation_binding_fingerprint(manifest: dict[str, Any]) -> str:
    return _canonical_contract_digest(
        {
            key: manifest.get(key)
            for key in ("adapter_binding", "format_id", "institution", "source_layout")
        }
    )


def _verify_manifest_adapter(manifest: dict[str, Any]) -> None:
    if "adapter_binding" not in manifest:
        raise RuntimeError(
            "Investment import binding is missing; use a compatible original executable "
            "or cancel and restage."
        )
    verify_bindings(manifest["adapter_binding"])
    lifecycle = manifest.get("lifecycle")
    if not isinstance(lifecycle, dict) or lifecycle.get(
        "adapter_binding_fingerprint"
    ) != _interpretation_binding_fingerprint(manifest):
        raise AdapterError("import_binding_changed")
    _manifest_source_layout(manifest)


def _verified_investment_format(manifest: dict[str, Any]) -> InvestmentFormat:
    _verify_manifest_adapter(manifest)
    investment_format = get_investment_format(manifest.get("format_id"))
    if investment_format.institution != manifest.get(
        "institution"
    ) or investment_format.source_layout != manifest.get("source_layout"):
        raise AdapterError("import_binding_changed")
    verify_bindings(manifest["adapter_binding"])
    return investment_format


def _restore_legacy_published_metadata(manifest: dict[str, Any], staging_db: Path) -> None:
    """Recover inert legacy routing only after the caller proves publication.

    Historical manifests may omit layout metadata. Published source associations
    and account rows supply the missing values; current adapter configuration is
    never consulted. Bound modern manifests may not use this compatibility path.
    """
    if (
        "adapter_binding" in manifest
        or manifest.get("lifecycle", {}).get("source_binding_fingerprint") is not None
    ):
        return
    sources = manifest.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("Investment import manifest source metadata is invalid.")
    by_folder: dict[str, tuple[str, str]] = {}
    recovered: list[tuple[dict[str, Any], dict[str, Any]]] = []
    with contextlib.closing(sqlite3.connect(staging_db)) as connection:
        connection.row_factory = sqlite3.Row
        for source in sources:
            rows = connection.execute(
                "SELECT s.source_path, s.source_role, s.byte_size, a.institution, "
                "a.account_alias, st.period_start, st.period_end "
                "FROM investment_source_associations s "
                "LEFT JOIN investment_statements st ON st.statement_id=s.statement_id "
                "LEFT JOIN investment_accounts a ON a.account_id=st.account_id "
                "WHERE s.import_run_id=? AND s.file_hash=? AND s.superseded_at IS NULL",
                (manifest["run_id"], source.get("sha256")),
            ).fetchall()
            if len(rows) != 1:
                raise ValueError("Legacy investment source binding is unavailable.")
            row = dict(rows[0])
            if row["source_role"] != source.get("role") or row["byte_size"] != source.get(
                "size_bytes"
            ):
                raise ValueError("Legacy investment source binding is inconsistent.")
            if row["institution"] is not None:
                if (
                    row["account_alias"] != source.get("alias")
                    or row["period_start"] != source.get("period_start")
                    or row["period_end"] != source.get("period_end")
                ):
                    raise ValueError("Legacy investment source binding is inconsistent.")
                folder = Path(row["source_path"]).parent.name
                identity = (
                    _safe_source_token(row["institution"]),
                    _safe_source_token(row["account_alias"]),
                )
                if folder in by_folder and by_folder[folder] != identity:
                    raise ValueError("Legacy investment source folder is ambiguous.")
                by_folder[folder] = identity
            recovered.append((source, row))
    institutions: set[str] = set()
    layouts: set[str] = set()
    for source, row in recovered:
        layout = "flat" if row["source_role"] == "statement_pdf" else "account_folders"
        folder = Path(row["source_path"]).parent.name
        institution = row["institution"]
        if institution is None:
            folder_identity = by_folder.get(folder)
            if folder_identity is None or folder_identity[1] != source.get("alias"):
                raise ValueError("Legacy investment evidence binding is unavailable.")
            institution = folder_identity[0]
        for key, expected in (("institution", institution), ("source_layout", layout)):
            if key in source and source[key] != expected:
                raise ValueError("Legacy investment source binding is inconsistent.")
            source[key] = expected
        if layout == "account_folders":
            if "source_folder" in source and source["source_folder"] != folder:
                raise ValueError("Legacy investment source folder is inconsistent.")
            source["source_folder"] = folder
        institutions.add(institution)
        layouts.add(layout)
    if len(institutions) != 1 or len(layouts) != 1:
        raise ValueError("Legacy investment source layout is ambiguous.")
    for key, expected in (("institution", institutions.pop()), ("source_layout", layouts.pop())):
        if key in manifest and manifest[key] != expected:
            raise ValueError("Legacy investment source binding is inconsistent.")
        manifest[key] = expected


def _manifest_source_layout(manifest: dict[str, Any]) -> str:
    institution = _safe_source_token(manifest.get("institution"))
    layout = manifest.get("source_layout")
    if layout not in {"account_folders", "flat"}:
        raise ValueError("Investment import source layout is invalid.")
    sources = manifest.get("sources")
    if not isinstance(sources, list) or not sources:
        raise ValueError("Investment import manifest source metadata is invalid.")
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError("Investment import manifest source metadata is invalid.")
        if source.get("institution") != institution or source.get("source_layout") != layout:
            raise ValueError("Investment import source binding is inconsistent.")
        if "source_name_hash" in source:
            name_hash = source["source_name_hash"]
            if not isinstance(name_hash, str) or re.fullmatch(r"[0-9a-f]{64}", name_hash) is None:
                raise ValueError("Investment import source name hash is invalid.")
        if layout == "flat" and source.get("role") != "statement_pdf":
            raise ValueError("Investment import structured PDF binding is invalid.")
    return str(layout)


def _manifest_source_folders(manifest: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    if _manifest_source_layout(manifest) == "flat":
        return result
    for source in manifest["sources"]:
        name = source.get("source_folder", source.get("alias"))
        if not isinstance(name, str) or name in {"", ".", ".."} or Path(name).name != name:
            raise ValueError("Investment source folder binding is invalid.")
        alias = _safe_source_token(source.get("alias"))
        if name in result and result[name] != alias:
            raise ValueError("Investment source folder binding is ambiguous.")
        result[name] = alias
    return result


def _locate_flat_source_files(
    inbox_dir: Path,
    *,
    pending: dict[str, dict[str, Any]],
    archive_durable: dict[str, bool] | None,
    investment_format: InvestmentFormat | None = None,
) -> dict[str, _LocatedSource]:
    """Require the same relevant PDF bytes, accepting harmless source renames."""
    by_hash: dict[str, list[dict[str, Any]]] = {}
    for source in pending.values():
        by_hash.setdefault(str(source.get("sha256")), []).append(source)
    located: dict[str, _LocatedSource] = {}
    with pin_private_directory(inbox_dir) as inbox:
        allowed = (
            {_archive_artifact_names(source).source_recovery for source in pending.values()}
            if archive_durable is not None
            else set()
        )
        _reject_unexpected_archive_artifacts(inbox, allowed)
        if archive_durable is not None:
            for source_key, source in pending.items():
                _reconcile_source_recovery_artifact(
                    inbox, source, archive_durable=archive_durable[source_key]
                )
        for name in _directory_names(inbox):
            metadata = _relative_metadata(inbox, name)
            if stat.S_ISLNK(metadata.st_mode):
                raise OSError("Investment inbox contains a symlink.")
            if Path(name).suffix.casefold() != ".pdf" or stat.S_ISDIR(metadata.st_mode):
                continue
            if not stat.S_ISREG(metadata.st_mode):
                raise RuntimeError("Investment source is not a regular file.")
            pinned = _pin_source(inbox, name, "", "statement_pdf")
            try:
                candidates = by_hash.get(pinned.sha256, [])
                if not candidates:
                    name_hash = hashlib.sha256(name.encode("utf-8")).hexdigest()
                    if any(
                        source.get("source_name_hash") == name_hash for source in pending.values()
                    ):
                        raise RuntimeError("Investment source name was reused with changed bytes.")
                    relevant = (
                        _is_format_source(pinned.content, investment_format)
                        if investment_format is not None
                        else False
                    )
                    _require_source_unchanged(pinned)
                    if relevant:
                        raise RuntimeError(
                            "Investment source changed or source set drifted after staging."
                        )
                    continue
                if len(candidates) != 1:
                    raise RuntimeError("Investment source identity is ambiguous after staging.")
                source = candidates[0]
                source_key = str(source["source_key"])
                if int(source.get("size_bytes", -1)) != pinned.size_bytes or source_key in located:
                    raise RuntimeError("Investment source identity is ambiguous after staging.")
                _require_source_unchanged(pinned)
                located[source_key] = _LocatedSource(
                    path=pinned.path,
                    parent_identity=inbox.identity,
                    device=pinned.device,
                    inode=pinned.inode,
                    size_bytes=pinned.size_bytes,
                    modified_ns=pinned.modified_ns,
                    sha256=pinned.sha256,
                    content=pinned.content,
                )
            finally:
                os.close(pinned.descriptor)
        inbox.validate()
    return located


def _locate_source_files(
    inbox_dir: Path,
    *,
    aliases: tuple[str, ...],
    pending: dict[str, dict[str, Any]],
    by_identity: dict[tuple[str, str, str], list[dict[str, Any]]],
    archive_durable: dict[str, bool] | None,
    source_layout: str = "account_folders",
    investment_format: InvestmentFormat | None = None,
    source_folders: dict[str, str] | None = None,
) -> dict[str, _LocatedSource]:
    """Validate current inbox files, optionally reconciling published archive state."""
    _require_safe_directory(inbox_dir, label="Investment inbox")
    if source_layout == "flat":
        return _locate_flat_source_files(
            inbox_dir,
            pending=pending,
            archive_durable=archive_durable,
            investment_format=investment_format,
        )

    located: dict[str, _LocatedSource] = {}
    current_count = 0
    with pin_private_directory(inbox_dir) as inbox:
        account_dirs: dict[str, str] = {}
        for name in _directory_names(inbox):
            metadata = _relative_metadata(inbox, name)
            if stat.S_ISLNK(metadata.st_mode):
                raise OSError("Investment inbox contains a symlink.")
            if not stat.S_ISDIR(metadata.st_mode):
                if Path(name).suffix.casefold() in _SOURCE_SUFFIXES:
                    raise RuntimeError("Investment source set changed after staging.")
                continue
            alias = (source_folders or {}).get(name, "")
            if alias not in aliases:
                continue
            if alias in account_dirs:
                raise ValueError("Multiple investment folders normalize to the same account alias.")
            account_dirs[alias] = name
        if set(account_dirs) != set(aliases):
            raise RuntimeError("Investment source folders changed after staging.")

        for alias, folder_name in account_dirs.items():
            with _pin_child_directory(inbox, folder_name) as folder:
                alias_sources = [
                    source for source in pending.values() if str(source.get("alias")) == alias
                ]
                allowed_source_names = (
                    {_archive_artifact_names(source).source_recovery for source in alias_sources}
                    if archive_durable is not None
                    else set()
                )
                _reject_unexpected_archive_artifacts(folder, allowed_source_names)
                if archive_durable is not None:
                    for source in alias_sources:
                        source_key = str(source.get("source_key"))
                        _reconcile_source_recovery_artifact(
                            folder,
                            source,
                            archive_durable=archive_durable[source_key],
                        )
                for name in _directory_names(folder):
                    metadata = _relative_metadata(folder, name)
                    if stat.S_ISLNK(metadata.st_mode):
                        raise OSError("Investment statement source is a symlink.")
                    if stat.S_ISDIR(metadata.st_mode):
                        raise RuntimeError("Investment source layout changed after staging.")
                    suffix = Path(name).suffix.casefold()
                    if suffix not in _SOURCE_SUFFIXES:
                        continue
                    if not stat.S_ISREG(metadata.st_mode):
                        raise RuntimeError("Investment source is not a regular file.")
                    current_count += 1
                    source_role: Literal["csv", "pdf", "evidence_pdf"] = (
                        "csv" if suffix == ".csv" else "pdf"
                    )
                    pinned = _pin_source(folder, name, alias, source_role)
                    try:
                        candidates = by_identity.get((alias, suffix, pinned.sha256), [])
                        if len(candidates) != 1:
                            raise RuntimeError(
                                "Investment source changed or source set drifted after staging."
                            )
                        source = candidates[0]
                        if int(source.get("size_bytes", -1)) != pinned.size_bytes:
                            raise RuntimeError("Investment source changed after staging.")
                        source_key = str(source["source_key"])
                        if source_key in located:
                            raise RuntimeError(
                                "Investment source identity is ambiguous after staging."
                            )
                        _require_source_unchanged(pinned)
                        located[source_key] = _LocatedSource(
                            path=pinned.path,
                            parent_identity=folder.identity,
                            device=pinned.device,
                            inode=pinned.inode,
                            size_bytes=pinned.size_bytes,
                            modified_ns=pinned.modified_ns,
                            sha256=pinned.sha256,
                            content=pinned.content,
                        )
                    finally:
                        os.close(pinned.descriptor)
        inbox.validate()

    if current_count != len(located):
        raise RuntimeError("Investment source changed or source set drifted after staging.")
    return located


def _validate_prepublication_sources(
    manifest: dict[str, Any], inbox_dir: Path
) -> dict[str, _LocatedSource]:
    """Validate every staged source without touching archive state or artifacts."""
    aliases, sources, pending, by_identity = _pending_source_state(manifest)
    if len(pending) != len(sources):
        raise RuntimeError("Investment pre-publication source state is invalid.")
    located = _locate_source_files(
        inbox_dir,
        aliases=aliases,
        pending=pending,
        by_identity=by_identity,
        archive_durable=None,
        source_layout=_manifest_source_layout(manifest),
        investment_format=_verified_investment_format(manifest),
        source_folders=_manifest_source_folders(manifest),
    )
    if set(located) != set(pending):
        raise RuntimeError("Investment source changed or source set drifted after staging.")
    return located


def _locate_pending_sources(
    manifest: dict[str, Any], inbox_dir: Path, processed_dir: Path
) -> tuple[dict[str, _LocatedSource], bool]:
    """Reconcile archive artifacts, then locate or recover every pending source."""
    aliases, _sources, pending, by_identity = _pending_source_state(manifest)
    targets: dict[str, Path] = {}
    target_allowed_names: dict[Path, set[str]] = {}
    for source_key, source in pending.items():
        target = _archive_target(processed_dir, source)
        targets[str(source_key)] = target
        names = _archive_artifact_names(source)
        target_allowed_names.setdefault(target.parent, set()).update(
            {names.archive_candidate, names.archive_guard}
        )
    archive_durable: dict[str, bool] = {}
    for source_key, source in pending.items():
        target = targets[str(source_key)]
        archive_durable[str(source_key)] = _reconcile_archive_directory_artifacts(
            source,
            target,
            allowed_names=target_allowed_names[target.parent],
        )

    located = _locate_source_files(
        inbox_dir,
        aliases=aliases,
        pending=pending,
        by_identity=by_identity,
        archive_durable=archive_durable,
        source_layout=_manifest_source_layout(manifest),
        source_folders=_manifest_source_folders(manifest),
    )

    recovered = False
    for source_key, source in pending.items():
        if source_key in located:
            continue
        target = _archive_target(processed_dir, source)
        if _verify_existing_archive(
            target,
            expected_hash=str(source.get("sha256")),
            expected_size=int(source.get("size_bytes", -1)),
        ):
            source["archive_status"] = "archived"
            source["archived_at"] = utc_now()
            source["archive_recovered"] = True
            recovered = True
            continue
        raise RuntimeError("Investment source changed or source set drifted after staging.")
    return located, recovered


def _require_located_source_unchanged(source: _LocatedSource) -> bytes:
    """Revalidate a finalize-time source through its captured parent/child identity."""
    with pin_private_directory(source.path.parent) as directory:
        if directory.identity != source.parent_identity:
            raise RuntimeError("Investment source parent changed after staging.")
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(source.path.name, flags, dir_fd=directory.descriptor)
        try:
            metadata = os.fstat(descriptor)
            expected = (
                source.device,
                source.inode,
                source.size_bytes,
                source.modified_ns,
            )
            identity = (
                metadata.st_dev,
                metadata.st_ino,
                metadata.st_size,
                metadata.st_mtime_ns,
            )
            content = _read_descriptor_bytes(descriptor)
            digest = hashlib.sha256(content).hexdigest()
            current = os.stat(
                source.path.name,
                dir_fd=directory.descriptor,
                follow_symlinks=False,
            )
            current_identity = (
                current.st_dev,
                current.st_ino,
                current.st_size,
                current.st_mtime_ns,
            )
            directory.validate()
            if (
                not stat.S_ISREG(metadata.st_mode)
                or identity != expected
                or current_identity != expected
                or digest != source.sha256
                or content != source.content
            ):
                raise RuntimeError("Investment source changed after staging.")
            return content
        finally:
            os.close(descriptor)


def _archive_target(processed_dir: Path, source: dict[str, Any]) -> Path:
    relative = Path(str(source.get("archive_relative", "")))
    if not relative.parts or relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Investment archive destination is invalid.")
    digest = str(source.get("sha256"))
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise ValueError("Investment archive source hash is invalid.")
    expected = _archive_relative_path(
        _PinnedSource(
            path=Path("."),
            alias=_safe_source_token(source.get("alias")),
            role=source["role"],
            sha256=digest,
            size_bytes=0,
            device=0,
            inode=0,
            modified_ns=0,
            content=b"",
            institution=_safe_source_token(source.get("institution")),
            source_layout=str(source.get("source_layout")),
            period_start=date.fromisoformat(str(source.get("period_start"))),
            period_end=date.fromisoformat(str(source.get("period_end"))),
        )
    )
    if relative != expected:
        raise ValueError("Investment archive destination binding is invalid.")
    target = processed_dir / relative
    _reject_symlink_components(target)
    return target


def _preflight_archives(manifest: dict[str, Any], processed_dir: Path) -> None:
    for source in manifest["sources"]:
        if source.get("archive_status") == "archived":
            continue
        target = _archive_target(processed_dir, source)
        _verify_existing_archive(
            target,
            expected_hash=str(source.get("sha256")),
            expected_size=int(source.get("size_bytes", -1)),
        )


def _verify_existing_file_readonly(
    target: Path,
    *,
    expected_hash: str,
    expected_size: int,
    label: str,
) -> bool:
    """Verify optional file identity and bytes without changing filesystem metadata."""
    try:
        with pin_existing_directory(target.parent) as directory:
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(target.name, flags, dir_fd=directory.descriptor)
            except FileNotFoundError:
                return False
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    raise RuntimeError(f"{label} is not a regular file.")
                digest, size = _hash_descriptor(descriptor)
                current = os.stat(
                    target.name,
                    dir_fd=directory.descriptor,
                    follow_symlinks=False,
                )
                directory.validate()
                if (
                    not stat.S_ISREG(current.st_mode)
                    or (current.st_dev, current.st_ino) != (metadata.st_dev, metadata.st_ino)
                    or digest != expected_hash
                    or size != expected_size
                ):
                    raise RuntimeError(f"{label} has different bytes.")
                return True
            finally:
                os.close(descriptor)
    except FileNotFoundError:
        return False


def _validate_prepublication_archive_collisions(
    manifest: dict[str, Any],
    processed_dir: Path,
) -> None:
    """Read-only collision validation before changing publication state."""
    for source in manifest["sources"]:
        if source.get("archive_status") == "archived":
            continue
        _verify_existing_file_readonly(
            _archive_target(processed_dir, source),
            expected_hash=str(source.get("sha256")),
            expected_size=int(source.get("size_bytes", -1)),
            label="Investment archive collision",
        )


def _hash_descriptor(descriptor: int) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    offset = 0
    while True:
        chunk = os.pread(descriptor, 1024 * 1024, offset)
        if not chunk:
            return digest.hexdigest(), size
        digest.update(chunk)
        size += len(chunk)
        offset += len(chunk)


def _fsync_directory(directory: PinnedPrivateDirectory) -> None:
    """Durably order directory metadata or fail before the source can be lost."""
    directory.validate()
    os.fsync(directory.descriptor)
    directory.validate()


def _validate_archive_binding(
    directory: PinnedPrivateDirectory,
    name: str,
    descriptor: int,
    identity: tuple[int, int],
) -> None:
    directory.validate()
    try:
        current = os.stat(name, dir_fd=directory.descriptor, follow_symlinks=False)
    except OSError as exc:
        raise RuntimeError("Investment archive destination changed during archival.") from exc
    pinned = os.fstat(descriptor)
    if (
        not stat.S_ISREG(current.st_mode)
        or not stat.S_ISREG(pinned.st_mode)
        or (current.st_dev, current.st_ino) != identity
        or (pinned.st_dev, pinned.st_ino) != identity
        or stat.S_IMODE(current.st_mode) != 0o600
        or stat.S_IMODE(pinned.st_mode) != 0o600
    ):
        raise RuntimeError("Investment archive destination changed during archival.")


def _open_verified_archive(
    directory: PinnedPrivateDirectory,
    name: str,
    *,
    expected_hash: str,
    expected_size: int,
) -> tuple[int, tuple[int, int]] | None:
    directory.validate()
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(name, flags, dir_fd=directory.descriptor)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise OSError("Investment archive destination is missing or unsafe.") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("Investment archive collision is not a regular file.")
        digest, size = _hash_descriptor(descriptor)
        if digest != expected_hash or size != expected_size:
            raise RuntimeError("Investment archive collision has different bytes.")
        os.fchmod(descriptor, 0o600)
        identity = (metadata.st_dev, metadata.st_ino)
        _validate_archive_binding(directory, name, descriptor, identity)
        return descriptor, identity
    except BaseException:
        os.close(descriptor)
        raise


def _verify_existing_archive(
    target: Path,
    *,
    expected_hash: str,
    expected_size: int,
) -> bool:
    try:
        parent_metadata = target.parent.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(parent_metadata.st_mode):
        raise NotADirectoryError("Investment archive parent is not a directory.")
    with pin_private_directory(target.parent) as directory:
        verified = _open_verified_archive(
            directory,
            target.name,
            expected_hash=expected_hash,
            expected_size=expected_size,
        )
        if verified is None:
            return False
        descriptor, _identity = verified
        os.close(descriptor)
        return True


def _reject_unexpected_archive_artifacts(
    directory: PinnedPrivateDirectory,
    allowed_names: set[str],
) -> None:
    for name in _directory_names(directory):
        if name.startswith(_MANAGED_ARCHIVE_PREFIXES) and name not in allowed_names:
            raise RuntimeError("Unexpected investment archive artifact requires review.")


def _open_verified_artifact(
    directory: PinnedPrivateDirectory,
    name: str,
    *,
    expected_hash: str,
    expected_size: int,
) -> tuple[int, tuple[int, int]] | None:
    try:
        return _open_verified_archive(
            directory,
            name,
            expected_hash=expected_hash,
            expected_size=expected_size,
        )
    except (OSError, RuntimeError) as exc:
        raise RuntimeError("Investment archive artifact is invalid or mismatched.") from exc


def _reconcile_archive_directory_artifacts(
    source: dict[str, Any],
    target: Path,
    *,
    allowed_names: set[str],
) -> bool:
    """Publish or clean deterministic candidate/guard names before retry recovery."""
    try:
        parent = target.parent.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISDIR(parent.st_mode):
        raise NotADirectoryError("Investment archive parent is not a directory.")

    names = _archive_artifact_names(source)
    expected_hash = str(source.get("sha256"))
    expected_size = int(source.get("size_bytes", -1))
    with pin_private_directory(target.parent) as directory:
        _reject_unexpected_archive_artifacts(directory, allowed_names)
        candidate = _open_verified_artifact(
            directory,
            names.archive_candidate,
            expected_hash=expected_hash,
            expected_size=expected_size,
        )
        guard = _open_verified_artifact(
            directory,
            names.archive_guard,
            expected_hash=expected_hash,
            expected_size=expected_size,
        )
        canonical = _open_verified_archive(
            directory,
            target.name,
            expected_hash=expected_hash,
            expected_size=expected_size,
        )
        descriptors = [
            verified[0] for verified in (candidate, guard, canonical) if verified is not None
        ]
        changed = False
        try:
            if guard is not None and canonical is not None and guard[1] != canonical[1]:
                raise RuntimeError("Investment archive guard artifact has ambiguous identity.")

            recovery = guard if guard is not None else candidate
            if canonical is None and recovery is not None:
                try:
                    os.link(
                        names.archive_guard if guard is not None else names.archive_candidate,
                        target.name,
                        src_dir_fd=directory.descriptor,
                        dst_dir_fd=directory.descriptor,
                        follow_symlinks=False,
                    )
                except FileExistsError as exc:
                    raise RuntimeError(
                        "Investment archive artifact recovery collided with canonical state."
                    ) from exc
                canonical = _open_verified_archive(
                    directory,
                    target.name,
                    expected_hash=expected_hash,
                    expected_size=expected_size,
                )
                if canonical is None or canonical[1] != recovery[1]:
                    raise RuntimeError("Investment archive artifact recovery changed identity.")
                descriptors.append(canonical[0])
                os.fsync(canonical[0])
                _fsync_directory(directory)
                changed = True

            if canonical is not None:
                os.fsync(canonical[0])
                _fsync_directory(directory)

            for artifact_name, verified in (
                (names.archive_candidate, candidate),
                (names.archive_guard, guard),
            ):
                if verified is not None:
                    os.unlink(artifact_name, dir_fd=directory.descriptor)
                    changed = True
            if changed:
                _fsync_directory(directory)
            return canonical is not None
        finally:
            for descriptor in descriptors:
                os.close(descriptor)


def _matching_source_names(
    directory: PinnedPrivateDirectory,
    source: dict[str, Any],
) -> list[tuple[str, tuple[int, int]]]:
    suffix = ".csv" if source.get("role") == "csv" else ".pdf"
    expected_hash = str(source.get("sha256"))
    expected_size = int(source.get("size_bytes", -1))
    matches: list[tuple[str, tuple[int, int]]] = []
    flags = os.O_RDONLY | os.O_NONBLOCK
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    for name in _directory_names(directory):
        if name.startswith(_MANAGED_ARCHIVE_PREFIXES) or Path(name).suffix.casefold() != suffix:
            continue
        descriptor = -1
        try:
            if not stat.S_ISREG(_relative_metadata(directory, name).st_mode):
                continue
            descriptor = os.open(name, flags, dir_fd=directory.descriptor)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                continue
            digest, size = _hash_descriptor(descriptor)
            if digest == expected_hash and size == expected_size:
                matches.append((name, (metadata.st_dev, metadata.st_ino)))
        except OSError:
            continue
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    return matches


def _reconcile_source_recovery_artifact(
    directory: PinnedPrivateDirectory,
    source: dict[str, Any],
    *,
    archive_durable: bool,
) -> None:
    """Restore or remove one verified deterministic source recovery link."""
    names = _archive_artifact_names(source)
    expected_hash = str(source.get("sha256"))
    expected_size = int(source.get("size_bytes", -1))
    recovery = _open_verified_artifact(
        directory,
        names.source_recovery,
        expected_hash=expected_hash,
        expected_size=expected_size,
    )
    if recovery is None:
        if archive_durable:
            _fsync_directory(directory)
        return
    descriptor, recovery_identity = recovery
    try:
        matches = _matching_source_names(directory, source)
        if len(matches) > 1:
            raise RuntimeError("Investment source recovery artifact is ambiguous.")
        if matches:
            if matches[0][1] != recovery_identity:
                raise RuntimeError("Investment source recovery artifact changed identity.")
        elif not archive_durable:
            suffix = ".csv" if source.get("role") == "csv" else ".pdf"
            token = str(source.get("archive_artifact_token"))
            institution = _safe_source_token(source.get("institution"))
            recovered_name = f"{institution}-recovered-{token}{suffix}"
            try:
                os.link(
                    names.source_recovery,
                    recovered_name,
                    src_dir_fd=directory.descriptor,
                    dst_dir_fd=directory.descriptor,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                raise RuntimeError(
                    "Investment source recovery artifact collided with inbox state."
                ) from exc
            restored = os.stat(
                recovered_name,
                dir_fd=directory.descriptor,
                follow_symlinks=False,
            )
            if (restored.st_dev, restored.st_ino) != recovery_identity:
                raise RuntimeError("Investment source recovery artifact changed identity.")
            _fsync_directory(directory)
        os.unlink(names.source_recovery, dir_fd=directory.descriptor)
        _fsync_directory(directory)
    finally:
        os.close(descriptor)


def _private_temp_file(directory: PinnedPrivateDirectory, name: str) -> tuple[str, int]:
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(name, flags, 0o600, dir_fd=directory.descriptor)
    os.fchmod(descriptor, 0o600)
    return name, descriptor


def _private_hardlink(
    directory: PinnedPrivateDirectory,
    source_name: str,
    name: str,
) -> str:
    os.link(
        source_name,
        name,
        src_dir_fd=directory.descriptor,
        dst_dir_fd=directory.descriptor,
        follow_symlinks=False,
    )
    return name


def _unlink_private_name(directory: PinnedPrivateDirectory, name: str | None) -> bool:
    if name is None:
        return False
    try:
        os.unlink(name, dir_fd=directory.descriptor)
    except FileNotFoundError:
        return False
    return True


def _restore_source(
    directory: PinnedPrivateDirectory,
    source_name: str,
    recovery_name: str,
    identity: tuple[int, int],
) -> None:
    with contextlib.suppress(FileExistsError):
        os.link(
            recovery_name,
            source_name,
            src_dir_fd=directory.descriptor,
            dst_dir_fd=directory.descriptor,
            follow_symlinks=False,
        )
    current = os.stat(source_name, dir_fd=directory.descriptor, follow_symlinks=False)
    if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != identity:
        raise RuntimeError("Investment source could not be restored after archive drift.")
    _fsync_directory(directory)


def _archive_source(
    source: dict[str, Any],
    source_path: Path | _LocatedSource,
    processed_dir: Path,
) -> Path:
    """Publish without replacement, then unlink through pinned recovery links."""
    located: _LocatedSource | None
    lexical_source: Path
    if isinstance(source_path, _LocatedSource):
        located = source_path
        lexical_source = source_path.path
    else:
        located = None
        lexical_source = source_path
    target = _archive_target(processed_dir, source)
    artifact_names = _archive_artifact_names(source)
    expected_hash = str(source.get("sha256"))
    expected_size = int(source.get("size_bytes", -1))
    with (
        pin_private_directory(lexical_source.parent) as source_directory,
        pin_private_directory(target.parent) as target_directory,
    ):
        source_descriptor = -1
        target_descriptor = -1
        candidate_descriptor = -1
        source_guard_descriptor = -1
        candidate_name: str | None = None
        archive_guard: str | None = None
        source_guard: str | None = None
        source_removed = False
        preserve_source_guard = False
        failure: BaseException | None = None

        def retain_first_failure(error: BaseException) -> None:
            nonlocal failure
            if failure is None:
                failure = error

        try:
            if located is not None and source_directory.identity != located.parent_identity:
                raise RuntimeError("Investment source parent changed after staging.")
            source_flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                source_flags |= os.O_NOFOLLOW
            source_descriptor = os.open(
                lexical_source.name,
                source_flags,
                dir_fd=source_directory.descriptor,
            )
            source_metadata = os.fstat(source_descriptor)
            if not stat.S_ISREG(source_metadata.st_mode):
                raise RuntimeError("Investment source is not a regular file.")
            source_identity = (source_metadata.st_dev, source_metadata.st_ino)
            if located is not None and (
                source_metadata.st_dev,
                source_metadata.st_ino,
                source_metadata.st_size,
                source_metadata.st_mtime_ns,
            ) != (
                located.device,
                located.inode,
                located.size_bytes,
                located.modified_ns,
            ):
                raise RuntimeError("Investment source changed after staging.")
            digest, size = _hash_descriptor(source_descriptor)
            if digest != expected_hash or size != expected_size:
                raise RuntimeError("Investment source changed before archival.")

            verified = _open_verified_archive(
                target_directory,
                target.name,
                expected_hash=expected_hash,
                expected_size=expected_size,
            )
            if verified is None:
                candidate_name, candidate_descriptor = _private_temp_file(
                    target_directory, artifact_names.archive_candidate
                )
                os.lseek(source_descriptor, 0, os.SEEK_SET)
                copied_hash = hashlib.sha256()
                copied_size = 0
                while True:
                    chunk = os.read(source_descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    copied_hash.update(chunk)
                    copied_size += len(chunk)
                    view = memoryview(chunk)
                    while view:
                        written = os.write(candidate_descriptor, view)
                        view = view[written:]
                os.fsync(candidate_descriptor)
                if copied_hash.hexdigest() != expected_hash or copied_size != expected_size:
                    raise RuntimeError("Investment archive hash verification failed.")
                candidate_metadata = os.fstat(candidate_descriptor)
                candidate_identity = (candidate_metadata.st_dev, candidate_metadata.st_ino)
                try:
                    os.link(
                        candidate_name,
                        target.name,
                        src_dir_fd=target_directory.descriptor,
                        dst_dir_fd=target_directory.descriptor,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    verified = _open_verified_archive(
                        target_directory,
                        target.name,
                        expected_hash=expected_hash,
                        expected_size=expected_size,
                    )
                    if verified is None:
                        raise RuntimeError("Investment archive destination changed during publish.")
                    target_descriptor, target_identity = verified
                    archive_guard = _private_hardlink(
                        target_directory,
                        target.name,
                        artifact_names.archive_guard,
                    )
                else:
                    verified = _open_verified_archive(
                        target_directory,
                        target.name,
                        expected_hash=expected_hash,
                        expected_size=expected_size,
                    )
                    if verified is None:
                        raise RuntimeError("Investment archive publication disappeared.")
                    target_descriptor, target_identity = verified
                    if target_identity != candidate_identity:
                        raise RuntimeError("Investment archive destination changed during publish.")
                    archive_guard = candidate_name
            else:
                target_descriptor, target_identity = verified
                archive_guard = _private_hardlink(
                    target_directory,
                    target.name,
                    artifact_names.archive_guard,
                )

            guard_metadata = os.stat(
                archive_guard,
                dir_fd=target_directory.descriptor,
                follow_symlinks=False,
            )
            if (guard_metadata.st_dev, guard_metadata.st_ino) != target_identity:
                raise RuntimeError("Investment archive destination changed during archival.")
            _validate_archive_binding(
                target_directory, target.name, target_descriptor, target_identity
            )
            os.fsync(target_descriptor)
            _fsync_directory(target_directory)

            source_guard = _private_hardlink(
                source_directory,
                lexical_source.name,
                artifact_names.source_recovery,
            )
            _fsync_directory(source_directory)
            source_current = os.stat(
                lexical_source.name,
                dir_fd=source_directory.descriptor,
                follow_symlinks=False,
            )
            guard_current = os.stat(
                source_guard,
                dir_fd=source_directory.descriptor,
                follow_symlinks=False,
            )
            if (source_current.st_dev, source_current.st_ino) != source_identity or (
                guard_current.st_dev,
                guard_current.st_ino,
            ) != source_identity:
                raise RuntimeError("Investment source changed before archival unlink.")
            _validate_archive_binding(
                target_directory, target.name, target_descriptor, target_identity
            )
            try:
                recovery_flags = os.O_RDONLY
                if hasattr(os, "O_NOFOLLOW"):
                    recovery_flags |= os.O_NOFOLLOW
                source_guard_descriptor = os.open(
                    source_guard,
                    recovery_flags,
                    dir_fd=source_directory.descriptor,
                )
                final_source = os.stat(
                    lexical_source.name,
                    dir_fd=source_directory.descriptor,
                    follow_symlinks=False,
                )
                final_recovery = os.fstat(source_guard_descriptor)
                source_digest, source_size = _hash_descriptor(source_descriptor)
                recovery_digest, recovery_size = _hash_descriptor(source_guard_descriptor)
                if (
                    not stat.S_ISREG(final_source.st_mode)
                    or not stat.S_ISREG(final_recovery.st_mode)
                    or (final_source.st_dev, final_source.st_ino) != source_identity
                    or (final_recovery.st_dev, final_recovery.st_ino) != source_identity
                    or source_digest != expected_hash
                    or recovery_digest != expected_hash
                    or source_size != expected_size
                    or recovery_size != expected_size
                ):
                    raise RuntimeError("Investment source bytes changed before archival unlink.")
            except BaseException:
                preserve_source_guard = True
                raise
            os.unlink(lexical_source.name, dir_fd=source_directory.descriptor)
            source_removed = True
            _fsync_directory(source_directory)
            _validate_archive_binding(
                target_directory, target.name, target_descriptor, target_identity
            )
            try:
                final_recovery = os.fstat(source_guard_descriptor)
                recovery_digest, recovery_size = _hash_descriptor(source_guard_descriptor)
                if (
                    not stat.S_ISREG(final_recovery.st_mode)
                    or (final_recovery.st_dev, final_recovery.st_ino) != source_identity
                    or recovery_digest != expected_hash
                    or recovery_size != expected_size
                ):
                    raise RuntimeError("Investment source bytes changed after archival unlink.")
            except BaseException:
                preserve_source_guard = True
                raise
        except BaseException as exc:
            failure = exc
            if source_removed and source_guard is not None:
                try:
                    _restore_source(
                        source_directory,
                        lexical_source.name,
                        source_guard,
                        source_identity,
                    )
                    source_removed = False
                except BaseException as restore_exc:
                    preserve_source_guard = True
                    failure = restore_exc
        finally:
            source_cleanup_changed = False
            target_cleanup_changed = False
            if source_guard is not None and not preserve_source_guard:
                try:
                    source_cleanup_changed = _unlink_private_name(source_directory, source_guard)
                except BaseException as exc:
                    retain_first_failure(exc)
            if archive_guard is not None and archive_guard != candidate_name:
                try:
                    target_cleanup_changed = _unlink_private_name(target_directory, archive_guard)
                except BaseException as exc:
                    retain_first_failure(exc)
            if candidate_name is not None:
                try:
                    target_cleanup_changed = (
                        _unlink_private_name(target_directory, candidate_name)
                        or target_cleanup_changed
                    )
                except BaseException as exc:
                    retain_first_failure(exc)
            for descriptor in (
                candidate_descriptor,
                target_descriptor,
                source_guard_descriptor,
                source_descriptor,
            ):
                if descriptor < 0:
                    continue
                try:
                    os.close(descriptor)
                except BaseException as exc:
                    retain_first_failure(exc)
            if source_cleanup_changed:
                try:
                    _fsync_directory(source_directory)
                except BaseException as exc:
                    retain_first_failure(exc)
            if target_cleanup_changed:
                try:
                    _fsync_directory(target_directory)
                except BaseException as exc:
                    retain_first_failure(exc)
        if failure is not None:
            raise failure.with_traceback(failure.__traceback__)
    return target


def _safe_reason(reason: str) -> str:
    value = reason.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        raise ValueError("Cancellation reason must be a short privacy-safe label.")
    return value


def _cancellation_audit_record(
    manifest: dict[str, Any],
    *,
    cancel_reason: str,
    cancelled_at: str,
) -> tuple[str, str, str, str, int, int, int, int]:
    run_id = manifest.get("run_id")
    counts = manifest.get("counts")
    if not isinstance(run_id, str) or not run_id or not isinstance(counts, dict):
        raise RuntimeError("Investment cancellation audit counts are invalid.")

    def count(name: str) -> int:
        value = counts.get(name)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise RuntimeError("Investment cancellation audit counts are invalid.")
        return value

    pairs = count("pairs")
    evidence_sources = count("evidence_pdfs")
    statements = count("statements")
    structured_sources = pairs * 2
    return (
        run_id,
        "cancelled",
        cancelled_at,
        cancel_reason,
        structured_sources + evidence_sources,
        structured_sources,
        evidence_sources,
        statements,
    )


def _verify_cancellation_audit_bytes(
    audit_bytes: bytes,
    expected: tuple[str, str, str, str, int, int, int, int],
) -> None:
    with contextlib.closing(sqlite3.connect(":memory:")) as connection:
        connection.deserialize(audit_bytes)
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise RuntimeError("Investment cancellation audit failed integrity validation.")
        columns = tuple(
            row[1] for row in connection.execute("PRAGMA table_info(investment_cancel_audit)")
        )
        if columns != _CANCELLATION_AUDIT_COLUMNS:
            raise RuntimeError("Investment cancellation audit schema is invalid.")
        rows = connection.execute(
            "SELECT run_id, status, cancelled_at, cancel_reason, source_count, "
            "structured_source_count, evidence_source_count, statements_imported "
            "FROM investment_cancel_audit"
        ).fetchall()
        if rows != [expected]:
            raise RuntimeError("Investment cancellation audit record is invalid.")


def _read_pinned_private_bytes(directory: PinnedPrivateDirectory, name: str) -> bytes:
    directory.validate()
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(name, flags, dir_fd=directory.descriptor)
    try:
        metadata = os.fstat(descriptor)
        identity = (metadata.st_dev, metadata.st_ino)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise OSError("Investment cancellation audit is missing or unsafe.")
        content = _read_descriptor_bytes(descriptor)
        directory.validate()
        current = os.stat(name, dir_fd=directory.descriptor, follow_symlinks=False)
        final = os.fstat(descriptor)
        if (
            not stat.S_ISREG(current.st_mode)
            or not stat.S_ISREG(final.st_mode)
            or (current.st_dev, current.st_ino) != identity
            or (final.st_dev, final.st_ino) != identity
            or stat.S_IMODE(current.st_mode) != 0o600
            or stat.S_IMODE(final.st_mode) != 0o600
        ):
            raise OSError("Investment cancellation audit changed during verification.")
        return content
    finally:
        os.close(descriptor)


def _restore_or_remove_rejected_cancellation_audit(
    directory: PinnedPrivateDirectory,
    audit_bytes: bytes,
    expected: tuple[str, str, str, str, int, int, int, int],
) -> None:
    """Leave a rejected audit canonical and valid, or durably remove it."""
    try:
        with directory.atomic_binary(_CANCELLATION_AUDIT_NAME) as handle:
            handle.write(audit_bytes)
        restored = _read_pinned_private_bytes(directory, _CANCELLATION_AUDIT_NAME)
        if restored != audit_bytes:
            raise RuntimeError("Investment cancellation audit restoration was rejected.")
        _verify_cancellation_audit_bytes(restored, expected)
        _fsync_directory(directory)
        return
    except BaseException:
        try:
            directory.unlink(_CANCELLATION_AUDIT_NAME, missing_ok=True)
            _fsync_directory(directory)
        except BaseException as cleanup_error:
            raise RuntimeError(
                "Investment cancellation audit rejection could not be cleaned safely."
            ) from cleanup_error


def _publish_cancellation_audit(
    run_dir: Path,
    manifest: dict[str, Any],
    *,
    cancel_reason: str,
    cancelled_at: str,
) -> None:
    expected = _cancellation_audit_record(
        manifest,
        cancel_reason=cancel_reason,
        cancelled_at=cancelled_at,
    )
    with contextlib.closing(sqlite3.connect(":memory:")) as connection:
        connection.execute(
            "CREATE TABLE investment_cancel_audit ("
            "run_id TEXT PRIMARY KEY, "
            "status TEXT NOT NULL CHECK(status = 'cancelled'), "
            "cancelled_at TEXT NOT NULL, "
            "cancel_reason TEXT NOT NULL, "
            "source_count INTEGER NOT NULL, "
            "structured_source_count INTEGER NOT NULL, "
            "evidence_source_count INTEGER NOT NULL, "
            "statements_imported INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO investment_cancel_audit "
            "(run_id, status, cancelled_at, cancel_reason, source_count, "
            "structured_source_count, evidence_source_count, statements_imported) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            expected,
        )
        connection.commit()
        audit_bytes = connection.serialize()

    _verify_cancellation_audit_bytes(audit_bytes, expected)
    with pin_private_directory(run_dir) as directory:
        with directory.atomic_binary(_CANCELLATION_AUDIT_NAME) as handle:
            handle.write(audit_bytes)
        try:
            published = _read_pinned_private_bytes(directory, _CANCELLATION_AUDIT_NAME)
        except BaseException:
            _restore_or_remove_rejected_cancellation_audit(directory, audit_bytes, expected)
            raise
        if published != audit_bytes:
            _restore_or_remove_rejected_cancellation_audit(directory, audit_bytes, expected)
            raise RuntimeError("Investment cancellation audit changed during publication.")
    _verify_cancellation_audit_bytes(published, expected)


def _cleanup_staging_database(staging_db: Path) -> None:
    removed = False
    failure: BaseException | None = None

    def retain_first_failure(error: BaseException) -> None:
        nonlocal failure
        if failure is None:
            failure = error

    with pin_private_directory(staging_db.parent) as directory:
        for suffix in ("", "-journal", "-wal", "-shm"):
            name = f"{staging_db.name}{suffix}"
            try:
                metadata = os.stat(name, dir_fd=directory.descriptor, follow_symlinks=False)
            except FileNotFoundError:
                continue
            except BaseException as exc:
                retain_first_failure(exc)
                continue
            if stat.S_ISREG(metadata.st_mode):
                try:
                    directory.unlink(name)
                except BaseException as exc:
                    retain_first_failure(exc)
                else:
                    removed = True
        if removed:
            try:
                _fsync_directory(directory)
            except BaseException as exc:
                retain_first_failure(exc)
    if failure is not None:
        raise failure.with_traceback(failure.__traceback__)
