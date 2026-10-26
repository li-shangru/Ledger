"""Statement-backed database repair workflow."""

from __future__ import annotations

import contextlib
import csv
import io
import os
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from ledger.adapters.contracts import BalanceSpec as StatementBalanceSpec
from ledger.adapters.registry import metadata_for_statement_type
from ledger.analysis import AnalysisRunner
from ledger.audit import StatementAuditOptions, StatementAuditService
from ledger.config import load_categories, load_ledger_config, load_merchant_rules
from ledger.config.repository import SQLiteConfigRepository, load_runtime_app_config
from ledger.core.account_identity import account_identity, institution_from_statement_type
from ledger.core.results import RunResult
from ledger.imports.pdf import statement_pdf_to_csv
from ledger.imports.pdf.post_processing import (
    persist_printed_credit_limits,
    persist_statement_coverage,
    refresh_transfer_matches,
)
from ledger.imports.pdf.readiness import live_mutation_boundary
from ledger.imports.pdf.workflow import (
    result_counts,
    sha256_file,
    validate_processing_result,
    write_json,
    write_result_to_db,
)
from ledger.insights import InsightEngine
from ledger.processing import process
from ledger.quality import DataQualityService
from ledger.reporting import ReportSnapshotService
from ledger.runtime import runtime_path, seed_config_path
from ledger.storage.database import (
    AccountBalanceDB,
    AccountDB,
    ProcessedFileDB,
    init_database,
    session_scope,
)
from ledger.storage.migrations import invalidate_db_caches, run_migrations, validate_database
from ledger.storage.operations import backup_database, record_db_operation
from ledger.storage.operations import utc_now_dt as utc_now
from ledger.storage.overrides import collect_durable_overrides, reconcile_durable_overrides
from ledger.storage.snapshots import (
    cleanup_database_artifacts,
    publish_database,
)
from ledger.utils import parse_money
from ledger.utils.private_files import (
    copy_private_file,
    ensure_private_dir,
    tighten_private_file,
    write_private_text_atomic,
)
from ledger.utils.text import clean_account_name, neutralize_csv_row

CENT = Decimal("0.01")

# The card Account-summary movement components, in the roles the extractor's own
# printed-equation check uses: previous + charges - payments == new (see
# validate_card_statement in imports/pdf/statement_pdf_to_csv/_dispatch.py). They
# are magnitudes, not signed figures.
_CARD_CHARGE_KEYS = ("purchases", "fees", "interest", "cash_advances")
_CARD_PAYMENT_KEY = "payments"


def _balance_spec(result):
    value = getattr(result, "balance_spec", None)
    if value is None:
        metadata = metadata_for_statement_type(result.statement_type)
        value = metadata.get("balance_spec") if metadata else None
    return StatementBalanceSpec(**value) if value else None


def carry_forward_overrides(live_db: Path, candidate_db: Path) -> int:
    """Carry durable per-transaction overrides from the live DB into a freshly
    built repair candidate.

    A statement repair rebuilds the candidate from scratch (``init_database`` +
    re-parse), so the candidate only ever holds IMPORT-confidence categorizations.
    Without this step, ``promote_candidate`` replaces the live transactions
    wholesale and silently discards every MANUAL user correction (and any RULE
    categorization not reproduced by re-parsing). Uses the same changed-ID
    reconciliation as incremental statement replacement.

    Returns the number of candidate rows updated.
    """
    with session_scope(live_db) as live:
        overrides = collect_durable_overrides(live)
    if not overrides:
        return 0

    with session_scope(candidate_db) as cand:
        return reconcile_durable_overrides(cand, overrides)


def carry_forward_import_review(live_db: Path, candidate_db: Path) -> int:
    """Carry user-authored ``import_review`` rows from the live DB into the repair
    candidate before promote.

    ``import_review`` is import-owned (not in MERGE_FORWARD_TABLES), so the
    candidate's empty copy would otherwise replace the live review queue at promote,
    erasing the user's review decisions (status / merchant / category / note). These
    rows are user-authored and not re-derivable, so copy them forward verbatim.

    Returns the number of review rows carried.
    """
    import sqlite3

    with contextlib.closing(sqlite3.connect(live_db)) as live:
        live.row_factory = sqlite3.Row
        rows = live.execute("SELECT * FROM import_review").fetchall()
    if not rows:
        return 0

    cols = list(rows[0].keys())  # schema-derived column names (not user input)
    col_list = ", ".join(cols)
    placeholders = ", ".join("?" for _ in cols)
    with contextlib.closing(sqlite3.connect(candidate_db)) as cand, cand:
        cand.executemany(
            f"INSERT OR REPLACE INTO import_review ({col_list}) VALUES ({placeholders})",
            [tuple(row[col] for col in cols) for row in rows],
        )
    return len(rows)


def carry_forward_operation_audit(live_db: Path, candidate_db: Path) -> int:
    """Preserve the live operation trail across whole-database repair promotion.

    This is deliberately repair-specific rather than a ``MERGE_FORWARD_TABLES``
    member: audit writes stay drift-exempt for import publication, while a repair
    candidate still inherits the stable proposal idempotency keys needed for a
    ledger-committed/proposal-pending retry.
    """
    import sqlite3

    with contextlib.closing(sqlite3.connect(live_db)) as live:
        live.row_factory = sqlite3.Row
        rows = live.execute("SELECT * FROM db_operation_audit ORDER BY id").fetchall()

    with contextlib.closing(sqlite3.connect(candidate_db)) as candidate, candidate:
        # Candidate-local pre-publication bookkeeping is not live history. Start
        # with the exact live trail; repair's promote audit is appended afterward.
        candidate.execute("DELETE FROM db_operation_audit")
        if rows:
            columns = list(rows[0].keys())
            column_list = ", ".join(columns)
            placeholders = ", ".join("?" for _ in columns)
            candidate.executemany(
                f"INSERT INTO db_operation_audit ({column_list}) VALUES ({placeholders})",
                [tuple(row[column] for column in columns) for row in rows],
            )
    return len(rows)


def investment_table_names() -> tuple[str, ...]:
    """Every investment-domain table, derived from the ORM (never hand-maintained).

    Deriving from Base.metadata means a newly-added investment table is carried
    forward automatically, the same way REQUIRED_TABLES is derived.
    """
    from ledger.storage.database import Base

    return tuple(sorted(name for name in Base.metadata.tables if name.startswith("investment_")))


def carry_forward_investments(live_db: Path, candidate_db: Path) -> dict[str, int]:
    """Carry the whole investment domain from the live DB into the repair candidate.

    A statement repair rebuilds the candidate from TRANSACTION PDFs only, so the
    candidate's investment tables are empty. Promote then replaced the live ones with
    that emptiness, destroying every investment statement, activity, position, performance
    row, and archived-source provenance association -- a domain that CANNOT be
    reconstructed from the sources this command reads. AGENTS.md states the same
    hazard for `rebuild-db` ("never use rebuild-db to recreate historical
    investments"); `repair statements` is the same rebuild-and-promote shape and
    carries it too.

    The investment tables are deliberately NOT in MERGE_FORWARD_TABLES -- that list is
    excluded from the import drift fingerprint, and investment tables must stay in
    that gate (see the FAIL-SAFE BOUNDARY note in imports/pdf/readiness.py). So they
    are carried here instead, in the repair path that actually needs it.

    Insertion order follows Base.metadata.sorted_tables so parents land before the
    rows that reference them. Returns per-table copied counts.
    """
    import sqlite3

    from ledger.storage.database import Base
    from ledger.storage.migrations import (
        is_legacy_investment_cash_schema,
        validate_investment_cash_balance_schema,
    )

    ordered = [
        table.name for table in Base.metadata.sorted_tables if table.name.startswith("investment_")
    ]
    copied: dict[str, int] = {}
    with contextlib.closing(
        sqlite3.connect(f"{live_db.resolve().as_uri()}?mode=ro", uri=True)
    ) as live:
        live.row_factory = sqlite3.Row
        existing = {
            row[0] for row in live.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        optional_table = "investment_cash_balances"
        missing = set(ordered) - existing
        if missing:
            if missing != {optional_table} or not is_legacy_investment_cash_schema(live):
                raise ValueError("Investment repair source has an incompatible schema.")
        else:
            validate_investment_cash_balance_schema(live)
            versions = {row[0] for row in live.execute("SELECT version FROM schema_migrations")}
            # Fresh, current init_database-only sources have no migration history.
            # A nonempty history must record the new table's completed migration.
            if versions and "20260913_0030_investment_cash_balances" not in versions:
                raise ValueError("Investment repair source has an incompatible schema.")
        # Only the recognized 0029 layout lacks cash evidence. Its absence means
        # unavailable; every other source table must retain the complete columns.
        for table in set(ordered) - missing:
            actual = {row[1] for row in live.execute(f"PRAGMA table_info({table})")}
            if actual != set(Base.metadata.tables[table].columns.keys()):
                raise ValueError("Investment repair source has an incompatible schema.")
        payload = {
            table: [] if table in missing else live.execute(f"SELECT * FROM {table}").fetchall()
            for table in ordered
        }

    with contextlib.closing(sqlite3.connect(candidate_db)) as cand, cand:
        cand.execute("PRAGMA foreign_keys = OFF")
        for table in ordered:
            rows = payload[table]
            if not rows:
                copied[table] = 0
                continue
            cols = list(rows[0].keys())  # schema-derived, not user input
            col_list = ", ".join(cols)
            placeholders = ", ".join("?" for _ in cols)
            cand.executemany(
                f"INSERT OR REPLACE INTO {table} ({col_list}) VALUES ({placeholders})",
                [tuple(row[col] for col in cols) for row in rows],
            )
            copied[table] = len(rows)
        if cand.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("carrying investments forward would break referential integrity")
        cand.execute("PRAGMA foreign_keys = ON")
    return copied


@dataclass(frozen=True)
class StatementRepairOptions:
    db_path: Path
    pdf_dirs: list[Path]
    processed_dir: Path
    runs_dir: Path = field(default_factory=lambda: runtime_path("repair-runs"))
    backup_dir: Path | None = None  # resolved at call-site as db_path.parent / "db-backups"
    ledger_config: Path = field(default_factory=lambda: seed_config_path("ledger.toml"))
    merchant_rules: Path = field(default_factory=lambda: seed_config_path("merchants.toml"))
    categories: Path = field(default_factory=lambda: seed_config_path("categories.toml"))
    dry_run: bool = False


@dataclass(frozen=True)
class StatementRepairResult:
    status: str
    run_dir: Path
    candidate_db: Path
    summary: dict[str, Any]

    @property
    def ok(self) -> bool:
        return self.status in {"success", "validated"}


class StatementRepairService:
    """Rebuild the local ledger DB from supported statement PDFs."""

    def run(self, options: StatementRepairOptions) -> StatementRepairResult:
        run_dir = make_run_dir(options.runs_dir)
        candidate_db = run_dir / "candidate.db"
        csv_root = run_dir / "csv"
        extraction_manifest = run_dir / "extraction-manifest.json"
        parse_exceptions = run_dir / "parse-exceptions.json"
        inventory_path = run_dir / "statement_inventory.csv"
        move_plan_path = run_dir / "pdf-move-plan.json"
        final_summary_path = run_dir / "repair-summary.json"

        started_at = utc_now()
        pdfs = discover_pdfs(options.pdf_dirs)
        summary: dict[str, Any] = {
            "status": "started",
            "started_at": started_at.isoformat(),
            "pdfs": len(pdfs),
            "dry_run": options.dry_run,
            "candidate_db": str(candidate_db),
        }
        # Tracks whether promote_candidate has published candidate content to the live DB.
        # Used in the except block to decide whether a failed audit row is warranted.
        promoted = False
        backup = None  # set after backup_database succeeds, used for failed audit artifacts
        try:
            if not pdfs:
                raise RuntimeError("No PDFs found for statement repair.")

            init_database(candidate_db)
            run_migrations(candidate_db)
            copy_runtime_config(options.db_path, candidate_db, options)

            extraction_results = [
                statement_pdf_to_csv.process_pdf(
                    pdf,
                    csv_root,
                    options.processed_dir,
                    dry_run=False,
                    archive_pdf=False,
                )
                for pdf in pdfs
            ]
            statement_pdf_to_csv.write_manifest(
                extraction_manifest,
                extraction_results,
                dry_run=False,
            )
            inventory = build_inventory(extraction_results)
            write_csv(inventory_path, inventory)

            statement_errors = [
                {
                    "pdf": str(result.pdf),
                    "errors": list(result.validation_errors),
                    "warnings": list(result.validation_warnings),
                }
                for result in extraction_results
                if result.validation_errors
            ]
            if statement_errors:
                write_json(run_dir / "statement-validation-errors.json", statement_errors)
                raise RuntimeError(f"Statement validation failed for {len(statement_errors)} PDFs.")

            config = load_runtime_config(candidate_db, options)
            csv_root_resolved = csv_root.resolve()
            account_slugs = {
                str(result.csv_path.resolve().relative_to(csv_root_resolved)): result.account_slug
                for result in extraction_results
                if result.csv_path is not None and result.account_slug
            }
            processing = process(
                config=config,
                source_root=csv_root,
                include_raw=False,
                account_slugs=account_slugs,
            )
            validate_processing_result(processing, parse_exceptions)
            write_result_to_db(candidate_db, csv_root, processing)

            # Preserve durable user/rule categorizations across the rebuild BEFORE
            # analytics run (so trend/health/snapshot reflect the corrected rows)
            # and before promote replaces the live transactions wholesale.
            carried_overrides = carry_forward_overrides(options.db_path, candidate_db)
            summary["carried_overrides"] = carried_overrides
            # Carry user-authored review-queue rows forward too (import-owned, so
            # promote would otherwise erase them).
            summary["carried_import_review"] = carry_forward_import_review(
                options.db_path, candidate_db
            )
            # Keep the operation trail — especially proposal idempotency keys —
            # across the candidate promotion that replaces the database wholesale.
            summary["carried_operation_audit"] = carry_forward_operation_audit(
                options.db_path, candidate_db
            )
            # Carry the investment domain forward. It is built from a SEPARATE private
            # workflow, never from transaction PDFs, so without this the promote below
            # replaces every investment table with the candidate's empty copy.
            carried_investments = carry_forward_investments(options.db_path, candidate_db)
            summary["carried_investments"] = sum(carried_investments.values())
            summary["carried_investments_by_table"] = carried_investments

            printed_credit_limits = persist_printed_credit_limits(
                candidate_db,
                extraction_manifest,
                account_aliases=config.ledger.general.account_aliases,
            )
            # Rebuild statement coverage into the candidate from this run's extraction
            # manifest. The promote below replaces the live DB wholesale, so a table
            # written only at PDF finalize would be emptied by every repair — the exact
            # failure that destroyed the investment domain before 1.2.0. Coverage is
            # rebuildable from the transaction PDFs, so repair regenerates it rather
            # than carrying it forward.
            statement_coverage = persist_statement_coverage(
                candidate_db,
                extraction_manifest,
                account_aliases=config.ledger.general.account_aliases,
            )
            post_processing = refresh_repair_analytics(candidate_db, config, extraction_results)
            post_processing["printed_credit_limits"] = printed_credit_limits
            post_processing["statement_coverage"] = statement_coverage
            move_plan = build_move_plan(extraction_results, options.processed_dir)
            write_json(move_plan_path, move_plan)
            validate_move_plan(move_plan)
            record_processed_files(candidate_db, move_plan)

            candidate_audit = StatementAuditService().run(
                StatementAuditOptions(
                    db_path=candidate_db,
                    pdf_dirs=options.pdf_dirs,
                    start_date=date(1900, 1, 1),
                    end_date=date(2999, 12, 31),
                    audit_runs_dir=run_dir / "candidate-audit",
                    ledger_config=options.ledger_config,
                    merchant_rules=options.merchant_rules,
                    categories=options.categories,
                )
            )
            validate_candidate_audit(candidate_audit.summary, expected_pdfs=len(pdfs))
            db_validation = validate_database(candidate_db, record=False)
            if db_validation.status != "success":
                raise RuntimeError("; ".join(db_validation.errors))

            summary.update(
                {
                    "status": "validated",
                    "candidate_counts": result_counts(processing),
                    "post_processing": post_processing,
                    "candidate_audit": {
                        "run_dir": str(candidate_audit.run_dir),
                        "summary": candidate_audit.summary,
                    },
                    "move_plan": move_plan,
                }
            )
            if options.dry_run:
                summary["finished_at"] = utc_now().isoformat()
                write_json(final_summary_path, summary)
                return StatementRepairResult("validated", run_dir, candidate_db, summary)

            resolved_backup_dir = (
                options.backup_dir
                if options.backup_dir is not None
                else options.db_path.parent / "db-backups"
            )

            backup = backup_database(
                options.db_path, backup_dir=resolved_backup_dir, reason="pre-statement-repair"
            )
            if not backup.ok:
                raise RuntimeError("; ".join(backup.errors))

            prepare_pdf_targets(move_plan)

            # Capture the pre-promote fingerprint AFTER backup_database and
            # prepare_pdf_targets (neither of which touch the live DB's data).
            # backup_database writes a db_operation_audit row to the live DB, so
            # capturing before it would see a stale hash/mtime and cause a false
            # abort on every repair run. promote_candidate itself only privately copies
            # the candidate onto a temp file before the recheck, so no self-inflicted
            # change occurs between this capture and publication.
            #
            # We capture the IMPORT-OWNED fingerprint (not the whole-file fingerprint)
            # so that a concurrent budget/goal/salt edit does NOT trigger a spurious
            # abort.  Only import-owned table changes (transactions, import_runs, etc.)
            # will fire the abort guard.
            from ledger.imports.pdf.readiness import import_owned_fingerprint as _io_fingerprint

            with live_mutation_boundary(options.db_path, operation="statement_repair"):
                pre_promote_import_owned = _io_fingerprint(options.db_path)

                _promote_candidate_locked(
                    candidate_db,
                    options.db_path,
                    expected_fingerprint=None,
                    expected_import_owned=pre_promote_import_owned,
                )
                promoted = True  # Candidate content is live from this point on.

                # Re-apply runtime config AFTER promote so that the candidate's intended
                # config metadata survives. Keep it inside the publication boundary so
                # staging cannot snapshot between these two live writes.
                copy_runtime_config(options.db_path, options.db_path, options)

            remove_replaced_sources(move_plan)
            final_audit = StatementAuditService().run(
                StatementAuditOptions(
                    db_path=options.db_path,
                    pdf_dirs=[options.processed_dir],
                    start_date=date(1900, 1, 1),
                    end_date=date(2999, 12, 31),
                    audit_runs_dir=run_dir / "final-audit",
                    ledger_config=options.ledger_config,
                    merchant_rules=options.merchant_rules,
                    categories=options.categories,
                )
            )
            validate_candidate_audit(final_audit.summary, expected_pdfs=len(pdfs))
            record_db_operation(
                options.db_path,
                operation="statement_repair_promote",
                result=RunResult(
                    status="success",
                    started_at=started_at,
                    finished_at=utc_now(),
                    counts={
                        "pdfs": len(pdfs),
                        **result_counts(processing),
                    },
                    artifacts={
                        "run_dir": run_dir,
                        "candidate_db": candidate_db,
                        "backup": backup.artifacts.get("backup", ""),
                        "final_audit": final_audit.run_dir,
                    },
                    details={
                        "final_audit_status": final_audit.status,
                        "processed_file_hash_matches": final_audit.summary["counts"][
                            "processed_file_hash_matches"
                        ],
                    },
                ),
            )
            summary.update(
                {
                    "status": "success",
                    "finished_at": utc_now().isoformat(),
                    "backup": str(backup.artifacts.get("backup", "")),
                    "final_audit": {
                        "run_dir": str(final_audit.run_dir),
                        "summary": final_audit.summary,
                    },
                }
            )
            write_json(final_summary_path, summary)
            return StatementRepairResult("success", run_dir, candidate_db, summary)
        except Exception as exc:
            # If live DB publication already happened but a post-promote step failed,
            # record a FAILED statement_repair_promote audit row best-effort.  This ensures
            # the destructive mutation is always audited regardless of post-promote outcome.
            # If promote_candidate never ran (promoted=False), no audit row is written — the
            # publication did not happen and the audit must truthfully reflect that.
            if promoted:
                _backup_artifact: Path | None = (
                    Path(str(backup.artifacts["backup"]))
                    if backup and backup.artifacts.get("backup")
                    else None
                )
                with contextlib.suppress(Exception):
                    record_db_operation(
                        options.db_path,
                        operation="statement_repair_promote",
                        result=RunResult(
                            status="failed",
                            started_at=started_at,
                            finished_at=utc_now(),
                            counts={"pdfs": len(pdfs)},
                            artifacts={
                                "run_dir": run_dir,
                                "candidate_db": candidate_db,
                                **({"backup": _backup_artifact} if _backup_artifact else {}),
                            },
                            errors=[str(exc)],
                            details={"candidate_db": str(candidate_db)},
                        ),
                        artifact_path=_backup_artifact,
                    )
            # When promote already happened but a post-promote step failed, use a
            # distinct status so callers can see that DB publication DID occur. A bare
            # 'failed' hides the irreversible mutation; 'promoted_with_post_step_errors'
            # makes it explicit that the candidate is now live and post-steps need attention.
            result_status = "promoted_with_post_step_errors" if promoted else "failed"
            summary.update(
                {
                    "status": result_status,
                    "finished_at": utc_now().isoformat(),
                    "error": str(exc),
                }
            )
            write_json(final_summary_path, summary)
            return StatementRepairResult(result_status, run_dir, candidate_db, summary)


def make_run_dir(runs_dir: Path) -> Path:
    ensure_private_dir(runs_dir)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = runs_dir / stamp
    suffix = 2
    while run_dir.exists():
        run_dir = runs_dir / f"{stamp}-{suffix}"
        suffix += 1
    ensure_private_dir(run_dir)
    return run_dir


def discover_pdfs(pdf_dirs: list[Path]) -> list[Path]:
    # Case-insensitive on the suffix so a .PDF file is discovered too. This keeps
    # repair's file set aligned with the audit it validates against, which is also
    # case-insensitive (see audit.statements.StatementAuditService._discover_pdfs).
    pdfs: list[Path] = []
    seen: set[Path] = set()
    for pdf_dir in pdf_dirs:
        for path in sorted(pdf_dir.expanduser().resolve().glob("*")):
            if path.suffix.lower() != ".pdf" or not path.is_file():
                continue
            if path not in seen:
                pdfs.append(path)
                seen.add(path)
    return pdfs


def copy_runtime_config(
    source_db: Path, candidate_db: Path, options: StatementRepairOptions
) -> None:
    source_repo = SQLiteConfigRepository(source_db, prepare_schema=False)
    target_repo = SQLiteConfigRepository(candidate_db)
    if source_repo.has_runtime_config():
        merchants = source_repo.load_merchant_rules()
        categories = source_repo.load_categories()
    else:
        merchants = load_merchant_rules(options.merchant_rules)
        categories = load_categories(options.categories)
    target_repo.replace_from_configs(merchants, categories)


def load_runtime_config(db_path: Path, options: StatementRepairOptions):
    ledger = load_ledger_config(options.ledger_config)
    return load_runtime_app_config(db_path, ledger, options.merchant_rules, options.categories)


def build_inventory(
    extraction_results: list[statement_pdf_to_csv.ExtractedCsv],
) -> list[dict[str, Any]]:
    rows = []
    for result in extraction_results:
        rows.append(
            {
                "pdf_path": str(result.pdf),
                "processed_pdf": str(result.processed_pdf or ""),
                "statement_type": result.statement_type,
                "transaction_count": result.transaction_count,
                "validation_errors": " | ".join(result.validation_errors),
                "validation_warnings": " | ".join(result.validation_warnings),
                **(result.validation_summary or {}),
            }
        )
    return rows


def refresh_repair_analytics(
    db_path: Path,
    config,
    extraction_results: list[statement_pdf_to_csv.ExtractedCsv],
) -> dict[str, Any]:
    from ledger.adjustments import AdjustmentDetectionService

    analysis = AnalysisRunner(db_path)
    transfer_matching = refresh_transfer_matches(db_path, config)
    accounts = analysis.run_account_analysis()
    statement_balances = apply_statement_balances(
        db_path,
        extraction_results,
        account_aliases=config.ledger.general.account_aliases,
    )
    # AFTER the anchors land. Account analysis computes utilization from the
    # absolute rows, so running it only before this wrote NULL to every card and
    # left the column empty until the next `ledger analysis refresh` -- which is
    # how the column stayed empty in the first place.
    analysis.apply_credit_utilization()
    patterns = analysis.run_pattern_detection()
    trends = analysis.run_trend_aggregation(days=30, months=18)
    health = analysis.run_health_scoring(days=30)
    # Re-derive true-cost treatments/offsets on the rebuilt candidate so a repair
    # does not leave true-cost reporting empty until a manual detect (mirrors the
    # import post-processing path; also records an adjustments_detect audit row).
    adjustments = AdjustmentDetectionService(db_path).detect(execute=True)
    snapshot = ReportSnapshotService(db_path).create_snapshot(source="statement_repair")
    insights = InsightEngine(db_path).refresh_period(
        snapshot["period_start"], snapshot["period_end"]
    )
    quality = DataQualityService(db_path).run_checks()
    return {
        "transfer_matching": transfer_matching,
        "accounts": accounts,
        "statement_balances": statement_balances,
        "patterns": patterns,
        "trends": trends,
        "health": health,
        "adjustments": {
            "offsets": len(adjustments.offsets),
            "treatments": len(adjustments.treatments),
        },
        "report_snapshot": snapshot,
        "insights": insights.counts,
        "quality": {"checks": len(quality), "issues": sum(issue.issue_count for issue in quality)},
    }


def apply_statement_balances(
    db_path: Path,
    extraction_results: list[statement_pdf_to_csv.ExtractedCsv],
    account_aliases: dict[str, str] | None = None,
) -> dict[str, int]:
    """Write the ABSOLUTE daily balance series every statement type can anchor.

    For each statement, walk the parsed rows day by day from its printed opening
    to its printed closing figure and store the running level as
    ``balance_basis="absolute"``. The walk asserts it lands exactly on the printed
    closing: that assertion is the only thing standing between a sign-convention
    slip and a stored balance that looks entirely plausible while being the
    negative of the truth, so it applies to cards as well as deposits.

    Every figure is converted to ONE stored sign convention -- positive is money
    held, negative is money owed (see ``AccountBalanceDB.balance``). ``spec``
    carries the per-issuer conversion.
    """
    records_by_account: dict[str, dict[date, dict[str, Any]]] = defaultdict(dict)
    required_accounts: set[str] = set()
    for result in extraction_results:
        spec = _balance_spec(result)
        if spec is None or result.csv_path is None:
            continue
        anchors = statement_anchor_balances(result, spec)
        if anchors is None:
            # Not fatal (see statement_anchor_balances), but it costs the account
            # its anchor for this period, so say so rather than skip in silence.
            print(
                f"{result.pdf.name}: no printed balance to anchor on; "
                f"{result.statement_type} period left without an absolute balance",
                file=sys.stderr,
            )
            continue
        opening, closing = anchors
        rows = read_statement_rows(result.csv_path, spec)
        if not rows:
            continue
        account = clean_account_name(result.account_slug or result.csv_path.stem, account_aliases)
        account_id = account_identity(
            result.institution or institution_from_statement_type(result.statement_type), account
        )
        required_accounts.add(account_id)
        running = opening
        current = min(row["date"] for row in rows)
        end = statement_end_date_from_result(result)
        rows_by_date: dict[date, list[Decimal]] = defaultdict(list)
        for row in rows:
            rows_by_date[row["date"]].append(row["amount"])
        while current <= end:
            amounts = rows_by_date.get(current, [])
            running += sum(amounts, Decimal("0"))
            records_by_account[account_id][current] = {
                "balance": running.quantize(CENT),
                "transaction_count": len(amounts),
            }
            current += timedelta(days=1)
        if running.quantize(CENT) != closing:
            raise RuntimeError(
                f"{result.pdf}: statement closing balance {closing} does not match computed {running.quantize(CENT)}"
            )

    now = utc_now()
    saved = 0
    with session_scope(db_path) as session:
        existing_accounts = {row.account_id for row in session.query(AccountDB).all()}
        missing = sorted(required_accounts - existing_accounts)
        if missing:
            raise RuntimeError(f"Missing account rows for statement balances: {missing}")
        if required_accounts:
            session.query(AccountBalanceDB).filter(
                AccountBalanceDB.account_id.in_(required_accounts),
                AccountBalanceDB.balance_basis == "absolute",
            ).delete(synchronize_session=False)
        for account_id, records in records_by_account.items():
            sorted_items = sorted(records.items())
            balances = [Decimal(str(item["balance"])) for _day, item in sorted_items]
            for index, (balance_date, item) in enumerate(sorted_items):
                window_7 = balances[max(0, index - 6) : index + 1]
                window_30 = balances[max(0, index - 29) : index + 1]
                session.merge(
                    AccountBalanceDB(
                        account_id=account_id,
                        balance_date=balance_date,
                        balance=Decimal(str(item["balance"])),
                        balance_basis="absolute",
                        transaction_count=int(item["transaction_count"]),
                        balance_7d_avg=(sum(window_7, Decimal("0")) / len(window_7)).quantize(CENT),
                        balance_30d_avg=(sum(window_30, Decimal("0")) / len(window_30)).quantize(
                            CENT
                        ),
                        balance_7d_min=min(window_7).quantize(CENT),
                        balance_7d_max=max(window_7).quantize(CENT),
                        credit_utilization=None,
                        computed_at=now,
                    )
                )
                saved += 1
    return {"accounts": len(records_by_account), "balances": saved}


def decimal_or_error(value: object, pdf: Path, label: str) -> Decimal:
    if value in (None, ""):
        raise RuntimeError(f"{pdf}: missing {label}")
    parsed = parse_money(str(value))
    if parsed is None:
        raise RuntimeError(f"{pdf}: invalid {label}: {value}")
    return parsed.quantize(CENT)


def statement_anchor_balances(
    result: statement_pdf_to_csv.ExtractedCsv,
    spec: StatementBalanceSpec,
) -> tuple[Decimal, Decimal] | None:
    """Return this statement's (opening, closing) in the stored sign convention.

    ``None`` means the statement carries no figure to anchor on, and the caller
    must skip it rather than invent one. Some card layouts omit an opening balance,
    and a card whose account summary
    did not parse has no movement components to recover it from either. Anchoring
    on a guess would store a plausible wrong number for every day but the last.

    A DEPOSIT statement missing either figure still raises. Its validator
    reconciles the row table against exactly those two labels, so a deposit
    statement that reached this point without them is a parse regression, not a
    layout the issuer is entitled to.
    """
    summary = result.validation_summary or {}
    closing_printed = summary.get(spec.closing_key)
    if spec.movement_backed and closing_printed in (None, ""):
        return None
    closing = (
        decimal_or_error(closing_printed, result.pdf, spec.closing_key.replace("_", " "))
        * spec.printed_sign
    )

    opening_printed = summary.get(spec.opening_key)
    if opening_printed not in (None, "") or not spec.movement_backed:
        opening = (
            decimal_or_error(opening_printed, result.pdf, spec.opening_key.replace("_", " "))
            * spec.printed_sign
        )
        return opening, closing

    movement = card_movement(summary)
    if movement is None:
        return None
    # The issuer's own printed equation is previous + movement == new, where
    # movement is charges minus payments. Negated into the stored convention that
    # reads closing == opening - movement, so opening == closing + movement.
    return closing + movement, closing


def card_movement(summary: dict[str, str]) -> Decimal | None:
    """Net debt movement a card statement printed for its period, or ``None``.

    ``None`` when the Account summary printed none of the movement components --
    the extractor warns about exactly that case ("Could not parse card
    Account-summary movement for reconciliation") and there is nothing to derive.

    Once at least one component printed, a missing sibling is read as a printed
    zero. That is the same reading the extractor's own movement reconciliation
    takes, and that reconciliation is what already checked this figure against the
    parsed rows -- so the two cannot disagree about which components exist.
    """
    if not any(
        summary.get(key) not in (None, "") for key in (*_CARD_CHARGE_KEYS, _CARD_PAYMENT_KEY)
    ):
        return None

    def component(key: str) -> Decimal:
        raw = summary.get(key)
        if raw in (None, ""):
            return Decimal("0")
        parsed = parse_money(str(raw))
        if parsed is None:
            raise RuntimeError(f"invalid card Account-summary component: {key}")
        return parsed.quantize(CENT)

    charges = sum((component(key) for key in _CARD_CHARGE_KEYS), Decimal("0"))
    return charges - component(_CARD_PAYMENT_KEY)


def read_statement_rows(csv_path: Path, spec: StatementBalanceSpec) -> list[dict[str, Any]]:
    """Parse one extracted statement CSV into dated, holder-signed amounts.

    ``spec.row_sign`` is applied here so the walk upstream never has to think
    about which issuer prints a purchase as a positive charge.
    """
    rows: list[dict[str, Any]] = []
    with csv_path.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            amount = parse_money(row.get(spec.amount_column, ""))
            if amount is None:
                continue
            posted = next(
                (
                    value
                    for value in ((row.get(column) or "").strip() for column in spec.date_columns)
                    if value
                ),
                "",
            )
            if not posted:
                raise RuntimeError(f"{csv_path}: statement row carries no posting date")
            rows.append(
                {
                    "date": date.fromisoformat(posted),
                    "amount": amount.quantize(CENT) * spec.row_sign,
                }
            )
    return rows


def statement_end_date_from_result(result: statement_pdf_to_csv.ExtractedCsv) -> date:
    if result.processed_pdf:
        prefix = result.processed_pdf.name[:10]
        return date.fromisoformat(prefix)
    raise RuntimeError(f"{result.pdf}: missing processed statement date")


def build_move_plan(
    extraction_results: list[statement_pdf_to_csv.ExtractedCsv],
    processed_dir: Path,
) -> list[dict[str, Any]]:
    plan = []
    for result in extraction_results:
        source = result.pdf.resolve()
        target = (result.processed_pdf or processed_dir / result.pdf.name).resolve()
        plan.append(
            {
                "source_pdf": str(source),
                "target_pdf": str(target),
                "source_hash": sha256_file(source),
                "target_exists": target.exists(),
                "target_hash": sha256_file(target) if target.exists() else "",
                "transaction_count": result.transaction_count,
                "statement_type": result.statement_type,
            }
        )
    return plan


def validate_move_plan(move_plan: list[dict[str, Any]]) -> None:
    targets: dict[str, str] = {}
    for item in move_plan:
        target = item["target_pdf"]
        source_hash = item["source_hash"]
        previous_hash = targets.get(target)
        if previous_hash and previous_hash != source_hash:
            raise RuntimeError(f"Multiple different PDFs map to processed target: {target}")
        targets[target] = source_hash
        if item["target_exists"] and item["target_hash"] != source_hash:
            raise RuntimeError(f"Processed target exists with a different hash: {target}")


def prepare_pdf_targets(move_plan: list[dict[str, Any]]) -> None:
    for item in move_plan:
        source = Path(item["source_pdf"])
        target = Path(item["target_pdf"])
        if source.resolve() == target.resolve():
            continue
        ensure_private_dir(target.parent)
        if target.exists():
            tighten_private_file(target)
            continue
        copy_private_file(source, target)
        if sha256_file(target) != item["source_hash"]:
            raise RuntimeError(f"Copied PDF hash mismatch: {target}")


def remove_replaced_sources(move_plan: list[dict[str, Any]]) -> None:
    for item in move_plan:
        source = Path(item["source_pdf"])
        target = Path(item["target_pdf"])
        if source.resolve() == target.resolve():
            continue
        if target.exists() and source.exists() and sha256_file(target) == item["source_hash"]:
            source.unlink()


def record_processed_files(db_path: Path, move_plan: list[dict[str, Any]]) -> None:
    now = utc_now()
    with session_scope(db_path) as session:
        session.query(ProcessedFileDB).delete()
        for item in move_plan:
            session.merge(
                ProcessedFileDB(
                    file_path=item["target_pdf"],
                    file_hash=item["source_hash"],
                    processed_at=now,
                    transaction_count=int(item["transaction_count"]),
                )
            )


def validate_candidate_audit(summary: dict[str, Any], *, expected_pdfs: int) -> None:
    counts = summary.get("counts", {})
    errors = []
    for key in [
        "row_mismatches",
        "balance_mismatches",
        "processing_errors",
        "statement_validation_errors",
    ]:
        if int(counts.get(key, 0)) != 0:
            errors.append(f"{key}={counts.get(key)}")
    if int(counts.get("processed_file_hash_matches", 0)) != expected_pdfs:
        errors.append(
            f"processed_file_hash_matches={counts.get('processed_file_hash_matches')} expected={expected_pdfs}"
        )
    if errors:
        raise RuntimeError("Statement audit failed hard gates: " + ", ".join(errors))


def promote_candidate(
    candidate_db: Path,
    target_db: Path,
    *,
    expected_fingerprint: dict | None = None,
    expected_import_owned: dict | None = None,
) -> None:
    """Promote *candidate_db* onto *target_db* atomically.

    Sequence:
      1. privately copy candidate_db → temp_target
      2. merge_forward(live=target_db, into=temp_target)   ← merges runtime tables
      3. Drift recheck via *expected_import_owned* (import-owned tables only).
         Runtime-table edits are ignored; import-owned changes abort.
      4. publish temp_target through SQLite's WAL-aware backup API

    Legacy *expected_fingerprint* (whole-file) is still accepted for backward-compat.
    If BOTH are supplied, *expected_import_owned* takes precedence.
    """
    with live_mutation_boundary(target_db, operation="statement_repair"):
        _promote_candidate_locked(
            candidate_db,
            target_db,
            expected_fingerprint=expected_fingerprint,
            expected_import_owned=expected_import_owned,
        )


def _promote_candidate_locked(
    candidate_db: Path,
    target_db: Path,
    *,
    expected_fingerprint: dict | None,
    expected_import_owned: dict | None,
) -> None:
    from ledger.imports.pdf.readiness import (
        database_fingerprint,
        fingerprints_match,
        import_owned_fingerprint,
        import_owned_match,
        is_sqlite_db,
        merge_forward,
    )

    if target_db.is_symlink():
        raise OSError(f"Database destination is a symlink: {target_db}")
    ensure_private_dir(target_db.parent)
    temp_target = target_db.with_name(
        f".{target_db.name}.statement-repair-{os.getpid()}-{uuid4().hex}.tmp"
    )
    try:
        copy_private_file(candidate_db, temp_target)

        # Merge live runtime tables forward into the temp copy.
        # Guard: only merge when the target is a real SQLite DB.
        if target_db.exists() and is_sqlite_db(target_db):
            merge_forward(live_db=target_db, into_db=temp_target)

        # Re-check drift at the shared publication boundary.
        if expected_import_owned is not None:
            current_owned = import_owned_fingerprint(target_db)
            if not import_owned_match(expected_import_owned, current_owned):
                raise RuntimeError(
                    "target DB changed during finalize; aborting promote to avoid losing concurrent edits"
                )
        elif expected_fingerprint is not None:
            current = database_fingerprint(target_db)
            if not fingerprints_match(expected_fingerprint, current):
                raise RuntimeError(
                    "target DB changed during finalize; aborting promote to avoid losing concurrent edits"
                )

        publish_database(temp_target, target_db)
    except Exception:
        cleanup_database_artifacts(temp_target)
        raise
    cleanup_database_artifacts(temp_target)
    invalidate_db_caches()


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        write_private_text_atomic(path, "", encoding="utf-8", newline="")
        return
    fieldnames = list(dict.fromkeys(key for row in rows for key in row))
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(neutralize_csv_row(row) for row in rows)
    write_private_text_atomic(path, buffer.getvalue(), encoding="utf-8", newline="")
