"""CLI commands for optimize."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from ledger.commands.common import (
    console,
    resolve_db_path,
)
from ledger.runtime import resolve_runtime_path

optimize_app = typer.Typer(help="Optimize local ledger rules and taxonomy.")


@optimize_app.command("categories")
def optimize_categories(
    db_path: Annotated[Path | None, typer.Option("--db", help="SQLite database path")] = None,
    runs_dir: Annotated[
        Path | None,
        typer.Option("--runs-dir", help="Optimization run output directory"),
    ] = None,
    execute_safe: Annotated[
        bool,
        typer.Option(
            "--execute-safe",
            help="Takes a pre-mutation backup, applies the full taxonomy rewrite and all auto-rules to the live DB, and auto-restores the backup on failure",
        ),
    ] = False,
    exported_config_dir: Annotated[
        Path | None,
        typer.Option("--exported-config-dir", help="Destination for exported config snapshots"),
    ] = None,
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="CLI defaults config file (overrides default locations)"),
    ] = None,
) -> None:
    """Optimize merchant profiles and category taxonomy after statement repair."""
    runs_dir = resolve_runtime_path(runs_dir, "optimization-runs")
    exported_config_dir = resolve_runtime_path(exported_config_dir, "exported")
    from ledger.optimize import CategoryOptimizationOptions, CategoryOptimizer

    db_path = resolve_db_path(db_path, config_file)
    result = CategoryOptimizer().run(
        CategoryOptimizationOptions(
            db_path=db_path,
            runs_dir=runs_dir.expanduser().resolve(),
            execute_safe=execute_safe,
            exported_config_dir=exported_config_dir.expanduser().resolve(),
        )
    )
    style = "green" if result.ok else "red"
    console.print(f"[{style}]Category optimization: {result.status}[/{style}]")
    console.print(f"  • Run dir: {result.run_dir}")
    for key, value in result.counts.items():
        console.print(f"  • {key}: {value}")
    for warning in result.warnings:
        console.print(f"  • [yellow]warning:[/yellow] {warning}")
    for error in result.errors:
        console.print(f"  • [red]error:[/red] {error}")
    if not result.ok:
        raise typer.Exit(1)
