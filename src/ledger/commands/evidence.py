"""CLI commands for local evidence summaries."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from ledger.commands.common import console, resolve_db_path
from ledger.evidence import EvidenceService
from ledger.evidence.service import default_audit_runs_dir
from ledger.runtime import resolve_runtime_path

evidence_app = typer.Typer(help="Inspect read-only ledger evidence status.")


@evidence_app.command("status")
def evidence_status(
    db_path: Annotated[Path | None, typer.Option("--db", help="SQLite database path")] = None,
    audit_runs_dir: Annotated[
        Path | None,
        typer.Option("--audit-runs-dir", help="Statement audit evidence directory"),
    ] = None,
    import_runs_dir: Annotated[
        Path | None,
        typer.Option("--import-runs-dir", help="PDF import run manifest directory"),
    ] = None,
    json_output: Annotated[bool, typer.Option("--json", help="Print JSON output")] = False,
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="CLI defaults config file (overrides default locations)"),
    ] = None,
) -> None:
    """Print the current read-only local evidence summary."""
    import_runs_dir = resolve_runtime_path(import_runs_dir, "import-runs")
    db_path = resolve_db_path(db_path, config_file)
    summary = EvidenceService(
        db_path,
        audit_runs_dir=audit_runs_dir or default_audit_runs_dir(),
        import_runs_dir=import_runs_dir,
    ).summary()
    if json_output:
        console.print_json(json.dumps(summary, default=str))
        return

    table = Table(title="Ledger Evidence")
    for column in ["Signal", "Status", "Detail"]:
        table.add_column(column)
    for label, key in [
        ("Statement Audit", "audit"),
        ("Import Freshness", "imports"),
        ("Review Readiness", "review"),
        ("Data Quality", "quality"),
        ("Report Snapshot", "snapshots"),
        ("Backup History", "backups"),
        ("Schema", "database"),
    ]:
        section = summary[key]
        table.add_row(label, str(section.get("status", "unknown")), str(section.get("detail", "")))
    console.print(table)
    if summary["next_actions"]:
        console.print("\nNext actions:")
        for action in summary["next_actions"]:
            console.print(f"  • {action}")
