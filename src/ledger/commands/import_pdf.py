"""CLI commands for import pdf."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from ledger.commands.common import (
    pdf_args,
)
from ledger.runtime import resolve_runtime_path

import_pdf_app = typer.Typer(help="Stage, inspect, finalize, and cancel PDF imports.")

# The agent-facing Review Queue. Every subcommand operates on the run's staging
# database only; none of them accept a --db, because none of them may touch the live
# ledger. Mirrored on the argparse tree in ledger.imports.pdf.workflow so that both
# ./scripts/import-pdf-inbox and `ledger import pdf` expose the same surface.
import_pdf_review_app = typer.Typer(help="Inspect and decide a staged run's review queue.")
import_pdf_app.add_typer(import_pdf_review_app, name="review")


@import_pdf_app.command("stage")
def import_pdf_stage(
    pdf_dir: Annotated[Path | None, typer.Option("--pdf-dir", help="Inbox PDF directory")] = None,
    processed_dir: Annotated[Path | None, typer.Option("--processed-dir")] = None,
    db_path: Annotated[Path | None, typer.Option("--db", help="Target SQLite database")] = None,
    base_db: Annotated[Path | None, typer.Option("--base-db")] = None,
    backup_dir: Annotated[Path | None, typer.Option("--backup-dir")] = None,
    runs_dir: Annotated[Path | None, typer.Option("--runs-dir")] = None,
) -> None:
    """Stage PDFs into an assisted-safe import run."""
    pdf_dir = resolve_runtime_path(pdf_dir, "inbox")
    processed_dir = resolve_runtime_path(processed_dir, "processed")
    db_path = resolve_runtime_path(db_path, "ledger.db")
    backup_dir = resolve_runtime_path(backup_dir, "db-backups")
    runs_dir = resolve_runtime_path(runs_dir, "import-runs")
    from ledger.imports.pdf.workflow import build_paths, stage

    args = pdf_args(
        pdf_dir=pdf_dir,
        processed_dir=processed_dir,
        db_path=db_path,
        base_db=base_db,
        backup_dir=backup_dir,
        runs_dir=runs_dir,
    )
    stage(args, build_paths(args))


@import_pdf_app.command("status")
def import_pdf_status(
    run_dir: Annotated[Path | None, typer.Option("--run-dir")] = None,
    pdf_dir: Annotated[Path | None, typer.Option("--pdf-dir")] = None,
    processed_dir: Annotated[Path | None, typer.Option("--processed-dir")] = None,
    db_path: Annotated[Path | None, typer.Option("--db")] = None,
    runs_dir: Annotated[Path | None, typer.Option("--runs-dir")] = None,
) -> None:
    """Print the import run manifest."""
    pdf_dir = resolve_runtime_path(pdf_dir, "inbox")
    processed_dir = resolve_runtime_path(processed_dir, "processed")
    db_path = resolve_runtime_path(db_path, "ledger.db")
    runs_dir = resolve_runtime_path(runs_dir, "import-runs")
    from ledger.imports.pdf.workflow import build_paths, status

    args = pdf_args(
        pdf_dir=pdf_dir,
        processed_dir=processed_dir,
        db_path=db_path,
        runs_dir=runs_dir,
        run_dir=run_dir,
    )
    status(args, build_paths(args))


@import_pdf_app.command("finalize")
def import_pdf_finalize(
    run_dir: Annotated[Path | None, typer.Option("--run-dir")] = None,
    pdf_dir: Annotated[Path | None, typer.Option("--pdf-dir")] = None,
    processed_dir: Annotated[Path | None, typer.Option("--processed-dir")] = None,
    db_path: Annotated[Path | None, typer.Option("--db")] = None,
    backup_dir: Annotated[Path | None, typer.Option("--backup-dir")] = None,
    runs_dir: Annotated[Path | None, typer.Option("--runs-dir")] = None,
) -> None:
    """Finalize a staged import run after review gates pass."""
    pdf_dir = resolve_runtime_path(pdf_dir, "inbox")
    processed_dir = resolve_runtime_path(processed_dir, "processed")
    db_path = resolve_runtime_path(db_path, "ledger.db")
    backup_dir = resolve_runtime_path(backup_dir, "db-backups")
    runs_dir = resolve_runtime_path(runs_dir, "import-runs")
    from ledger.imports.pdf.workflow import build_paths, finalize

    args = pdf_args(
        pdf_dir=pdf_dir,
        processed_dir=processed_dir,
        db_path=db_path,
        backup_dir=backup_dir,
        runs_dir=runs_dir,
        run_dir=run_dir,
    )
    finalize(args, build_paths(args))


@import_pdf_app.command("cancel")
def import_pdf_cancel(
    run_dir: Annotated[Path | None, typer.Option("--run-dir")] = None,
    reason: Annotated[str, typer.Option("--reason")] = "manual_cancel",
    pdf_dir: Annotated[Path | None, typer.Option("--pdf-dir")] = None,
    processed_dir: Annotated[Path | None, typer.Option("--processed-dir")] = None,
    db_path: Annotated[Path | None, typer.Option("--db")] = None,
    runs_dir: Annotated[Path | None, typer.Option("--runs-dir")] = None,
) -> None:
    """Cancel a non-finalized import run."""
    pdf_dir = resolve_runtime_path(pdf_dir, "inbox")
    processed_dir = resolve_runtime_path(processed_dir, "processed")
    db_path = resolve_runtime_path(db_path, "ledger.db")
    runs_dir = resolve_runtime_path(runs_dir, "import-runs")
    from ledger.imports.pdf.workflow import build_paths, cancel

    args = pdf_args(
        pdf_dir=pdf_dir,
        processed_dir=processed_dir,
        db_path=db_path,
        runs_dir=runs_dir,
        run_dir=run_dir,
        reason=reason,
    )
    cancel(args, build_paths(args))


@import_pdf_app.command("repair-coordination")
def import_pdf_repair_coordination(
    run_dir: Annotated[Path, typer.Option("--run-dir")],
) -> None:
    """Register a bounded coordination record for an already-terminal run.

    Use when a live write refuses with "import-run coordination is unreadable"
    because a finalized or cancelled run's `run.json` outgrew the coordination cap.
    """
    from ledger.imports.pdf.readiness import repair_run_coordination

    try:
        record = repair_run_coordination(run_dir)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--run-dir") from None
    typer.echo(f"repaired_coordination\trun_dir={Path(run_dir).resolve()}\trecord={record}")


@import_pdf_review_app.command("list")
def import_pdf_review_list(
    run_dir: Annotated[Path, typer.Option("--run-dir", help="Import run directory")],
    status: Annotated[
        str,
        typer.Option("--status", help="Filter: all, pending, resolved, or reviewed_as_is"),
    ] = "all",
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON output")] = False,
) -> None:
    """List a staged run's review rows with its live review gate."""
    from ledger.imports.pdf.workflow import review_list

    review_list(run_dir, status=status, json_output=json_output)


@import_pdf_review_app.command("resolve")
def import_pdf_review_resolve(
    run_dir: Annotated[Path, typer.Option("--run-dir", help="Import run directory")],
    key: Annotated[str, typer.Option("--key", help="Review key to resolve")],
    category: Annotated[str, typer.Option("--category", help="Category to assign")],
    merchant: Annotated[
        str | None,
        typer.Option("--merchant", help="Merchant to assign (defaults to the row's own)"),
    ] = None,
    note: Annotated[str, typer.Option("--note", help="Note to record with the decision")] = "",
    teach: Annotated[
        bool,
        typer.Option(
            "--teach",
            help=(
                "Also create the reusable merchant rule, in THIS RUN's staging config "
                "(promoted by finalize). Teaching the live database while a run is open "
                "is refused: it would abort the run and lose its review decisions."
            ),
        ),
    ] = False,
    pattern: Annotated[
        str | None,
        typer.Option(
            "--pattern",
            help="Override the pattern --teach derives from the staged raw statement text",
        ),
    ] = None,
    runs_dir: Annotated[Path | None, typer.Option("--runs-dir")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON output")] = False,
) -> None:
    """Set one staged review row's merchant and category."""
    runs_dir = resolve_runtime_path(runs_dir, "import-runs")
    from ledger.imports.pdf.workflow import review_resolve

    review_resolve(
        run_dir,
        runs_dir=runs_dir,
        key=key,
        category=category,
        merchant=merchant,
        note=note,
        teach=teach,
        pattern=pattern,
        json_output=json_output,
    )


@import_pdf_review_app.command("reviewed-as-is")
def import_pdf_review_reviewed_as_is(
    run_dir: Annotated[Path, typer.Option("--run-dir", help="Import run directory")],
    key: Annotated[str, typer.Option("--key", help="Review key to accept as is")],
    note: Annotated[str, typer.Option("--note", help="Note to record with the decision")] = "",
    runs_dir: Annotated[Path | None, typer.Option("--runs-dir")] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON output")] = False,
) -> None:
    """Accept one staged review row's auto-derived merchant and category."""
    runs_dir = resolve_runtime_path(runs_dir, "import-runs")
    from ledger.imports.pdf.workflow import review_reviewed_as_is

    review_reviewed_as_is(
        run_dir,
        runs_dir=runs_dir,
        key=key,
        note=note,
        json_output=json_output,
    )
