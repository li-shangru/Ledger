"""CLI commands for quality."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from ledger.commands.common import (
    console,
    resolve_db_path,
)

quality_app = typer.Typer(help="Run local data quality checks.")


@quality_app.command("run")
def quality_run(
    db_path: Annotated[Path | None, typer.Option("--db", help="SQLite database path")] = None,
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="CLI defaults config file (overrides default locations)"),
    ] = None,
) -> None:
    """Run and record local data quality checks."""
    from ledger.quality import DataQualityService

    db_path = resolve_db_path(db_path, config_file)
    issues = DataQualityService(db_path).run_checks()
    table = Table(title="Data Quality")
    for column in ["Check", "Severity", "Status", "Issues"]:
        table.add_column(column)
    for issue in issues:
        table.add_row(issue.check_name, issue.severity, issue.status, str(issue.issue_count))
    console.print(table)
    if any(issue.status == "fail" for issue in issues):
        raise typer.Exit(1)
