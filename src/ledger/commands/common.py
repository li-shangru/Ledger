"""Shared helpers for Ledger CLI command modules."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from types import SimpleNamespace

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from ledger.config.repository import ConfigValidationResult
from ledger.runtime import (
    default_database_path,
    require_external_runtime_path,
    resolve_runtime_path,
    seed_config_path,
)

console = Console()


def setup_logging(level: str) -> None:
    """Configure logging with Rich handler.

    Resolves the level name safely: an unknown string raises a clean
    ``typer.BadParameter`` (exit code 2) instead of crashing with an
    ``AttributeError`` from ``getattr(logging, ...)``.
    """
    resolved = logging.getLevelNamesMapping().get((level or "").upper())
    if resolved is None:
        raise typer.BadParameter("--log-level must be one of DEBUG/INFO/WARNING/ERROR/CRITICAL")
    logging.basicConfig(
        level=resolved,
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
    )


def validate_period(value: str | None, pattern: str, name: str) -> str | None:
    """Validate period argument format."""
    if value and not re.fullmatch(pattern, value):
        raise typer.BadParameter(f"{name} must match {pattern}")
    return value


def resolve_db_path(
    db_path: Path | None,
    config_file: Path | None = None,
    *,
    must_exist: bool = True,
) -> Path:
    """Resolve a database path from --db or CLI defaults.

    ``must_exist=False`` is for the commands that legitimately CREATE the database
    (seeding a fresh runtime config), which would otherwise be rejected here.
    """
    selected = default_database_path(config_file) if db_path is None else db_path
    resolved = require_external_runtime_path(selected).resolve()
    if must_exist and not resolved.exists():
        console.print(f"[red]Error:[/red] Database not found: {resolved}")
        raise typer.Exit(1)

    return resolved


def print_config_validation(result: ConfigValidationResult) -> None:
    table = Table(title="Runtime Config")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right")
    table.add_row("Merchant rules", str(result.merchant_rules))
    table.add_row("Merchant patterns", str(result.merchant_patterns))
    table.add_row("Categories", str(result.categories))
    table.add_row("Category merchants", str(result.category_merchants))
    table.add_row("Issues", str(len(result.issues)))
    console.print(table)

    if result.issues:
        console.print("\n[red]Config issues:[/red]")
        for issue in result.issues:
            console.print(f"  • {issue}")


def print_run_result(title: str, result) -> None:
    status_style = "green" if result.ok else "red"
    console.print(f"[{status_style}]{title}: {result.status}[/{status_style}]")
    if result.counts:
        for key, value in result.counts.items():
            console.print(f"  • {key}: {value}")
    for warning in result.warnings:
        console.print(f"  • [yellow]warning:[/yellow] {warning}")
    for error in result.errors:
        console.print(f"  • [red]error:[/red] {error}")
    for key, value in result.artifacts.items():
        console.print(f"  • {key}: {value}")


def pdf_args(
    *,
    pdf_dir: Path,
    processed_dir: Path,
    db_path: Path,
    base_db: Path | None = None,
    backup_dir: Path | None = None,
    ledger_config: Path | None = None,
    merchant_rules: Path | None = None,
    categories: Path | None = None,
    runs_dir: Path | None = None,
    run_dir: Path | None = None,
    reason: str = "manual_cancel",
):
    ledger_config = ledger_config if ledger_config is not None else seed_config_path("ledger.toml")
    merchant_rules = (
        merchant_rules if merchant_rules is not None else seed_config_path("merchants.toml")
    )
    categories = categories if categories is not None else seed_config_path("categories.toml")
    backup_dir = resolve_runtime_path(backup_dir, "db-backups")
    runs_dir = resolve_runtime_path(runs_dir, "import-runs")
    return SimpleNamespace(
        pdf_dir=pdf_dir,
        processed_dir=processed_dir,
        db=db_path,
        base_db=base_db,
        backup_dir=backup_dir,
        ledger_config=ledger_config,
        merchant_rules=merchant_rules,
        categories=categories,
        runs_dir=runs_dir,
        run_dir=run_dir,
        reason=reason,
    )
