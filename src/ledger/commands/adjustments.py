"""CLI commands for adjustments."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from ledger.commands.common import (
    console,
    print_run_result,
    resolve_db_path,
)

adjustments_app = typer.Typer(help="Detect and validate true-cost transaction adjustments.")


@adjustments_app.command("detect")
def adjustments_detect(
    db_path: Annotated[Path | None, typer.Option("--db", help="SQLite database path")] = None,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview detected adjustments without saving"),
    ] = False,
    execute: Annotated[
        bool,
        typer.Option("--execute", help="Persist deterministic adjustment treatments"),
    ] = False,
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="CLI defaults config file (overrides default locations)"),
    ] = None,
) -> None:
    """Detect refunds, reimbursements, rewards, and insurance treatment rows."""
    from ledger.adjustments import AdjustmentDetectionService
    from ledger.reporting import ReportSnapshotService

    if dry_run and execute:
        console.print("[red]Error:[/red] choose either --dry-run or --execute, not both")
        raise typer.Exit(1)
    db_path = resolve_db_path(db_path, config_file)
    apply_changes = execute
    result = AdjustmentDetectionService(db_path).detect(execute=apply_changes)
    title = "Adjustment detection" if apply_changes else "Adjustment detection dry-run"
    status_style = "green" if result.ok else "red"
    console.print(f"[{status_style}]{title}: {result.status}[/{status_style}]")
    for key, value in result.counts.items():
        console.print(f"  • {key}: {value}")
    for warning in result.warnings:
        console.print(f"  • [yellow]warning:[/yellow] {warning}")
    for error in result.errors:
        console.print(f"  • [red]error:[/red] {error}")
    if result.ok and apply_changes:
        snapshot = ReportSnapshotService(db_path).create_snapshot(source="adjustments_detect")
        console.print(f"  • refreshed_snapshot: {snapshot['id']}")
    if not apply_changes:
        console.print("  • Pass --execute to save these treatments.")
    if not result.ok:
        raise typer.Exit(1)


@adjustments_app.command("validate")
def adjustments_validate(
    db_path: Annotated[Path | None, typer.Option("--db", help="SQLite database path")] = None,
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="CLI defaults config file (overrides default locations)"),
    ] = None,
) -> None:
    """Validate persisted true-cost adjustment rows."""
    from ledger.adjustments import AdjustmentDetectionService

    db_path = resolve_db_path(db_path, config_file)
    result = AdjustmentDetectionService(db_path).validate()
    print_run_result("Adjustment validation", result)
    if not result.ok:
        raise typer.Exit(1)
