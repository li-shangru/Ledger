"""CLI commands for export."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from ledger.commands.common import (
    console,
    resolve_db_path,
)
from ledger.runtime import resolve_runtime_path

export_app = typer.Typer(help="Create privacy-safe local analysis exports.")


@export_app.command("package")
def export_package(
    db_path: Annotated[Path | None, typer.Option("--db", help="SQLite database path")] = None,
    preset: Annotated[
        str,
        typer.Option("--preset", help="Export preset name"),
    ] = "chatgpt-safe",
    export_format: Annotated[
        str,
        typer.Option("--format", help="Output format: zip, csv, jsonl, or parquet"),
    ] = "zip",
    start_date: Annotated[
        str | None,
        typer.Option("--start", help="Start date for export period (YYYY-MM-DD)"),
    ] = None,
    end_date: Annotated[
        str | None,
        typer.Option("--end", help="End date for export period (YYYY-MM-DD)"),
    ] = None,
    dest: Annotated[Path | None, typer.Option("--dest", help="Destination directory")] = None,
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="CLI defaults config file (overrides default locations)"),
    ] = None,
) -> None:
    """Create a privacy-safe export for external analysis."""
    dest = resolve_runtime_path(dest, "exports")
    from ledger.exporting import ExportPackageService
    from ledger.exporting.service import EXPORT_PRESETS
    from ledger.utils import parse_date

    valid_formats = ("zip", "csv", "jsonl", "parquet")
    if preset not in EXPORT_PRESETS:
        raise typer.BadParameter(
            f"unknown preset {preset!r}; valid presets: {', '.join(sorted(EXPORT_PRESETS))}",
            param_hint="--preset",
        )
    if export_format not in valid_formats:
        raise typer.BadParameter(
            f"unknown format {export_format!r}; valid formats: {', '.join(valid_formats)}",
            param_hint="--format",
        )

    db_path = resolve_db_path(db_path, config_file)
    start = parse_date(start_date) if start_date else None
    end = parse_date(end_date) if end_date else None
    if start_date is not None and start is None:
        console.print("[red]Error:[/red] --start must be a valid date (YYYY-MM-DD)")
        raise typer.Exit(1)
    if end_date is not None and end is None:
        console.print("[red]Error:[/red] --end must be a valid date (YYYY-MM-DD)")
        raise typer.Exit(1)
    if bool(start) != bool(end):
        console.print("[red]Error:[/red] --start and --end must be provided together")
        raise typer.Exit(1)
    try:
        result = ExportPackageService(db_path).create_package(
            dest_dir=dest.expanduser().resolve(),
            preset=preset,
            export_format=export_format,
            start_date=start,
            end_date=end,
        )
    except ValueError as exc:
        # Belt-and-suspenders: the service also rejects a share-safe preset asked
        # for as per-transaction csv/jsonl/parquet. Surface that as a clean message,
        # not a raw traceback.
        raise typer.BadParameter(str(exc)) from exc
    console.print("[green]✓[/green] Created export package")
    console.print(f"  • Path: {result.path}")
    console.print(f"  • Rows: {result.rows_exported:,}")
