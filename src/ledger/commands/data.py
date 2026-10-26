"""CLI commands for data."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from ledger.commands.common import (
    console,
    print_run_result,
    resolve_db_path,
)

data_app = typer.Typer(help="Clean local generated data artifacts.")


@data_app.command("clean")
def data_clean(
    db_path: Annotated[Path | None, typer.Option("--db", help="SQLite database path")] = None,
    data_dir: Annotated[
        Path | None,
        typer.Option("--data-dir", help="Ledger data directory; defaults to the DB parent"),
    ] = None,
    execute: Annotated[
        bool,
        typer.Option("--execute", help="Actually delete artifacts and prune stale metadata"),
    ] = False,
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="CLI defaults config file (overrides default locations)"),
    ] = None,
) -> None:
    """Clean stale local data artifacts. Defaults to dry-run."""
    from ledger.maintenance import DataCleanupService

    db_path = resolve_db_path(db_path, config_file)
    result = DataCleanupService(db_path, data_dir=data_dir).clean(execute=execute)
    title = "Data cleanup" if execute else "Data cleanup dry-run"
    print_run_result(title, result)
    for key in ["delete_roots", "stale_import_run_ids", "stale_export_run_ids"]:
        values = result.details.get(key, [])
        if values:
            console.print(f"  • {key}:")
            for value in values:
                console.print(f"    - {value}")
    if not execute:
        console.print("  • Pass --execute to apply this cleanup.")
    if not result.ok:
        raise typer.Exit(1)
