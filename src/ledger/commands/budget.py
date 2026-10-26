"""CLI commands for budget."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from ledger.commands.common import (
    print_run_result,
    resolve_db_path,
)

budget_app = typer.Typer(help="Import, export, suggest, and validate local budgets.")


@budget_app.command("import")
def budget_import(
    source: Annotated[Path, typer.Option("--source", help="Budget CSV path")],
    db_path: Annotated[Path | None, typer.Option("--db", help="SQLite database path")] = None,
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="CLI defaults config file (overrides default locations)"),
    ] = None,
    execute: Annotated[
        bool,
        typer.Option("--execute", help="Replace stored budgets after a pre-write backup"),
    ] = False,
) -> None:
    """Preview a category-budget CSV; pass --execute to replace stored budgets."""
    from ledger.planning import BudgetService

    db_path = resolve_db_path(db_path, config_file)
    result = BudgetService(db_path, prepare_schema=False).import_csv(
        source.expanduser().resolve(), execute=execute
    )
    print_run_result("Budget import", result)
    if not result.ok:
        raise typer.Exit(1)


@budget_app.command("export")
def budget_export(
    dest: Annotated[
        Path,
        typer.Option("--dest", help="Absent Budget CSV destination (must not exist)"),
    ],
    db_path: Annotated[Path | None, typer.Option("--db", help="SQLite database path")] = None,
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="CLI defaults config file (overrides default locations)"),
    ] = None,
) -> None:
    """Export category budgets to a destination that must not exist."""
    from ledger.planning import BudgetService

    db_path = resolve_db_path(db_path, config_file)
    result = BudgetService(db_path, prepare_schema=False).export_csv(dest.expanduser())
    print_run_result("Budget export", result)
    if not result.ok:
        raise typer.Exit(1)


@budget_app.command("suggest")
def budget_suggest(
    start_date: Annotated[
        str, typer.Option("--start-date", help="Inclusive first date (YYYY-MM-DD)")
    ],
    end_date: Annotated[str, typer.Option("--end-date", help="Inclusive last date (YYYY-MM-DD)")],
    dest: Annotated[
        Path,
        typer.Option("--dest", help="Absent suggestion CSV destination (must not exist)"),
    ],
    db_path: Annotated[Path | None, typer.Option("--db", help="SQLite database path")] = None,
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="CLI defaults config file (overrides default locations)"),
    ] = None,
) -> None:
    """Write represented nonzero-month import rows; destination must not exist."""
    from ledger.planning import BudgetService
    from ledger.utils import parse_date

    db_path = resolve_db_path(db_path, config_file)
    parsed_start = parse_date(start_date)
    parsed_end = parse_date(end_date)
    if parsed_start is None or parsed_end is None:
        raise typer.BadParameter("--start-date and --end-date must be valid dates")
    if parsed_end < parsed_start:
        raise typer.BadParameter("--end-date must be on or after --start-date")
    result = BudgetService(db_path, prepare_schema=False).export_suggestions(
        dest.expanduser(), parsed_start, parsed_end
    )
    print_run_result("Budget suggestions", result)
    if not result.ok:
        raise typer.Exit(1)


@budget_app.command("validate")
def budget_validate(
    db_path: Annotated[Path | None, typer.Option("--db", help="SQLite database path")] = None,
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="CLI defaults config file (overrides default locations)"),
    ] = None,
) -> None:
    """Validate local budget rows."""
    from ledger.planning import BudgetService

    db_path = resolve_db_path(db_path, config_file)
    result = BudgetService(db_path, prepare_schema=False).validate()
    print_run_result("Budget validation", result)
    if not result.ok:
        raise typer.Exit(1)
