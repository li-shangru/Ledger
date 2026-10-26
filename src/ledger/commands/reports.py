"""CLI commands for reports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from ledger.commands.common import (
    console,
    resolve_db_path,
)

reports_app = typer.Typer(help="Refresh and inspect reporting snapshots.")


@reports_app.command("refresh")
def reports_refresh(
    db_path: Annotated[Path | None, typer.Option("--db", help="SQLite database path")] = None,
    start_date: Annotated[
        str | None,
        typer.Option("--start", help="Start date for the snapshot period (YYYY-MM-DD)"),
    ] = None,
    end_date: Annotated[
        str | None,
        typer.Option("--end", help="End date for the snapshot period (YYYY-MM-DD)"),
    ] = None,
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="CLI defaults config file (overrides default locations)"),
    ] = None,
) -> None:
    """Refresh the latest reporting snapshot."""
    from ledger.reporting import ReportSnapshotService
    from ledger.utils import parse_date

    db_path = resolve_db_path(db_path, config_file)

    def _required_date(value: str, name: str):
        parsed = parse_date(value)
        if parsed is None:
            # Distinguish an invalid date from the "only one provided" case below, so a
            # typo isn't silently reported as a missing pair (or, if both are invalid,
            # silently ignored — snapshotting the full unfiltered range).
            console.print(f"[red]Error:[/red] {name} is not a valid date (YYYY-MM-DD)")
            raise typer.Exit(1)
        return parsed

    if bool(start_date) != bool(end_date):
        console.print("[red]Error:[/red] --start and --end must be provided together")
        raise typer.Exit(1)
    start = _required_date(start_date, "--start") if start_date else None
    end = _required_date(end_date, "--end") if end_date else None
    snapshot = ReportSnapshotService(db_path).create_snapshot(
        source="cli_reports_refresh",
        start_date=start,
        end_date=end,
    )
    console.print("[green]✓[/green] Refreshed report snapshot")
    console.print(f"  • Snapshot: {snapshot['id']}")
    console.print(f"  • Period: {snapshot['period_start']} to {snapshot['period_end']}")


@reports_app.command("list")
def reports_list(
    db_path: Annotated[Path | None, typer.Option("--db", help="SQLite database path")] = None,
    limit: Annotated[int, typer.Option("--limit", help="Maximum snapshots to show")] = 20,
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="CLI defaults config file (overrides default locations)"),
    ] = None,
) -> None:
    """List recent reporting snapshots."""
    from ledger.reporting import ReportSnapshotService

    db_path = resolve_db_path(db_path, config_file)
    rows = ReportSnapshotService(db_path).list_snapshots(limit=limit)
    table = Table(title="Report Snapshots")
    for column in ["ID", "Created", "Period", "Source", "Kind", "Schema", "Status"]:
        table.add_column(column)
    for row in rows:
        table.add_row(
            str(row["id"]),
            str(row["created_at"]),
            f"{row['period_start']} to {row['period_end']}",
            str(row["source"]),
            str(row["source_kind"]),
            str(row["schema_version"]),
            str(row["status"]),
        )
    console.print(table)


@reports_app.command("show")
def reports_show(
    snapshot_id: Annotated[int, typer.Option("--id", help="Snapshot ID")],
    db_path: Annotated[Path | None, typer.Option("--db", help="SQLite database path")] = None,
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="CLI defaults config file (overrides default locations)"),
    ] = None,
) -> None:
    """Print one report snapshot as JSON."""
    from ledger.reporting import ReportSnapshotService

    db_path = resolve_db_path(db_path, config_file)
    snapshot = ReportSnapshotService(db_path).get_snapshot(snapshot_id)
    if not snapshot:
        console.print(f"[red]Error:[/red] Snapshot not found: {snapshot_id}")
        raise typer.Exit(1)
    console.print_json(json.dumps(snapshot, default=str))
