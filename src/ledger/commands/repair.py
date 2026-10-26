"""CLI commands for repair."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from ledger.commands.common import (
    console,
    resolve_db_path,
)
from ledger.runtime import resolve_runtime_path, runtime_path, seed_config_path

repair_app = typer.Typer(help="Repair local ledger state from source evidence.")


@repair_app.command("statements")
def repair_statements(
    db_path: Annotated[Path | None, typer.Option("--db", help="SQLite database path")] = None,
    pdf_dirs: Annotated[
        list[Path] | None,
        typer.Option("--pdf-dir", help="Statement PDF directory; may be repeated"),
    ] = None,
    processed_dir: Annotated[
        Path | None,
        typer.Option("--processed-dir", help="Canonical processed PDF directory"),
    ] = None,
    runs_dir: Annotated[
        Path | None,
        typer.Option("--runs-dir", help="Repair run output directory"),
    ] = None,
    backup_dir: Annotated[
        Path | None,
        typer.Option("--backup-dir", help="Database backup directory"),
    ] = None,
    execute: Annotated[
        bool,
        typer.Option(
            "--execute", help="Back up and promote the candidate DB (default: validate-only)"
        ),
    ] = False,
    ledger_config: Annotated[
        Path | None,
        typer.Option("--ledger-config", help="Ledger config seed path"),
    ] = None,
    merchant_rules: Annotated[
        Path | None,
        typer.Option("--merchant-rules", help="Merchant config seed/export path"),
    ] = None,
    categories: Annotated[
        Path | None,
        typer.Option("--categories", help="Category config seed/export path"),
    ] = None,
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="CLI defaults config file (overrides default locations)"),
    ] = None,
) -> None:
    """Rebuild and validate the ledger DB from source statement PDFs. Defaults to validate-only."""
    ledger_config = ledger_config if ledger_config is not None else seed_config_path("ledger.toml")
    merchant_rules = (
        merchant_rules if merchant_rules is not None else seed_config_path("merchants.toml")
    )
    categories = categories if categories is not None else seed_config_path("categories.toml")
    processed_dir = resolve_runtime_path(processed_dir, "processed")
    runs_dir = resolve_runtime_path(runs_dir, "repair-runs")
    backup_dir = resolve_runtime_path(backup_dir, "db-backups")
    from ledger.repair import StatementRepairOptions, StatementRepairService

    db_path = resolve_db_path(db_path, config_file)
    directories = pdf_dirs or [runtime_path("inbox"), runtime_path("processed")]
    result = StatementRepairService().run(
        StatementRepairOptions(
            db_path=db_path,
            pdf_dirs=[path.expanduser().resolve() for path in directories],
            processed_dir=processed_dir.expanduser().resolve(),
            runs_dir=runs_dir.expanduser().resolve(),
            backup_dir=backup_dir.expanduser().resolve(),
            ledger_config=ledger_config,
            merchant_rules=merchant_rules,
            categories=categories,
            dry_run=not execute,
        )
    )
    style = "green" if result.ok else "red"
    console.print(f"[{style}]Statement repair: {result.status}[/{style}]")
    console.print(f"  • Run dir: {result.run_dir}")
    console.print(f"  • Candidate DB: {result.candidate_db}")
    counts = result.summary.get("candidate_counts") or {}
    for key, value in counts.items():
        console.print(f"  • {key}: {value}")
    if not result.ok:
        console.print(f"  • [red]error:[/red] {result.summary.get('error', 'unknown')}")
        raise typer.Exit(1)
    if not execute:
        console.print("  • Pass --execute to back up and promote the candidate DB.")
