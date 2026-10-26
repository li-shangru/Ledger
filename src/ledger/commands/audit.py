"""CLI commands for audit."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Annotated

import typer

from ledger.commands.common import (
    console,
    resolve_db_path,
)
from ledger.runtime import resolve_runtime_path, runtime_path, seed_config_path

audit_app = typer.Typer(help="Run read-only source-to-ledger audits.")


def _resolve_end(end_date: str | None) -> date | None:
    """Resolve the audit end date: today when omitted, else the parsed date
    (None if the provided value is not a valid date)."""
    from ledger.utils import parse_date

    if end_date is None:
        return date.today()
    return parse_date(end_date)


@audit_app.command("statements")
def audit_statements(
    db_path: Annotated[Path | None, typer.Option("--db", help="SQLite database path")] = None,
    pdf_dirs: Annotated[
        list[Path] | None,
        typer.Option("--pdf-dir", help="Statement PDF directory; may be repeated"),
    ] = None,
    start_date: Annotated[
        str,
        typer.Option("--start", help="Audit start date (YYYY-MM-DD)"),
    ] = "2025-01-01",
    end_date: Annotated[
        str | None,
        typer.Option("--end", help="Audit end date (YYYY-MM-DD); defaults to today"),
    ] = None,
    audit_runs_dir: Annotated[
        Path | None,
        typer.Option("--audit-runs-dir", help="Directory for compact audit reports"),
    ] = None,
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
    fail_on_issues: Annotated[
        bool,
        typer.Option(
            "--fail-on-issues",
            help="Exit nonzero when the audit finds mismatches or processing issues",
        ),
    ] = False,
) -> None:
    """Audit source statement PDFs against the current database without mutation."""
    ledger_config = ledger_config if ledger_config is not None else seed_config_path("ledger.toml")
    merchant_rules = (
        merchant_rules if merchant_rules is not None else seed_config_path("merchants.toml")
    )
    categories = categories if categories is not None else seed_config_path("categories.toml")
    audit_runs_dir = resolve_runtime_path(audit_runs_dir, "audit-runs")
    from ledger.audit import StatementAuditOptions, StatementAuditService
    from ledger.utils import parse_date

    db_path = resolve_db_path(db_path, config_file)
    start = parse_date(start_date)
    end = _resolve_end(end_date)
    if start is None or end is None:
        console.print("[red]Error:[/red] --start and --end must be valid dates (YYYY-MM-DD)")
        raise typer.Exit(1)
    if start > end:
        console.print("[red]Error:[/red] --start must be on or before --end")
        raise typer.Exit(1)

    directories = pdf_dirs or [runtime_path("inbox"), runtime_path("processed")]
    result = StatementAuditService().run(
        StatementAuditOptions(
            db_path=db_path,
            pdf_dirs=[path.expanduser().resolve() for path in directories],
            start_date=start,
            end_date=end,
            audit_runs_dir=audit_runs_dir.expanduser().resolve(),
            ledger_config=ledger_config,
            merchant_rules=merchant_rules,
            categories=categories,
        )
    )
    status_style = "green" if result.status == "pass" else "yellow"
    console.print(f"[{status_style}]Statement audit: {result.status}[/{status_style}]")
    console.print(f"  • Run dir: {result.run_dir}")
    for key, value in result.summary.get("counts", {}).items():
        console.print(f"  • {key}: {value}")
    if fail_on_issues and result.status != "pass":
        raise typer.Exit(1)
