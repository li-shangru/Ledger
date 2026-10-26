"""CLI commands for db."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from ledger.commands.common import (
    console,
    print_run_result,
    resolve_db_path,
)

db_app = typer.Typer(help="Local database migration, validation, and backup operations.")


@db_app.command("migrate")
def db_migrate(
    db_path: Annotated[Path | None, typer.Option("--db", help="SQLite database path")] = None,
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="CLI defaults config file (overrides default locations)"),
    ] = None,
) -> None:
    """Apply pending local SQLite migrations."""
    from ledger.storage.migrations import run_migrations

    db_path = resolve_db_path(db_path, config_file)
    result = run_migrations(db_path)
    print_run_result("Database migration", result)
    if not result.ok:
        raise typer.Exit(1)


@db_app.command("validate")
def db_validate(
    db_path: Annotated[Path | None, typer.Option("--db", help="SQLite database path")] = None,
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="CLI defaults config file (overrides default locations)"),
    ] = None,
) -> None:
    """Validate local SQLite schema and required operational tables."""
    from ledger.storage.migrations import validate_database

    db_path = resolve_db_path(db_path, config_file)
    result = validate_database(db_path)
    print_run_result("Database validation", result)
    if not result.ok:
        raise typer.Exit(1)


@db_app.command("backup")
def db_backup(
    db_path: Annotated[Path | None, typer.Option("--db", help="SQLite database path")] = None,
    dest: Annotated[Path | None, typer.Option("--dest", help="Backup directory")] = None,
    reason: Annotated[str, typer.Option("--reason", help="Backup reason suffix")] = "manual",
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="CLI defaults config file (overrides default locations)"),
    ] = None,
) -> None:
    """Create a timestamped local SQLite backup."""
    from ledger.storage.operations import backup_database

    db_path = resolve_db_path(db_path, config_file)
    result = backup_database(db_path, backup_dir=dest, reason=reason)
    print_run_result("Database backup", result)
    if not result.ok:
        raise typer.Exit(1)


@db_app.command("doctor")
def db_doctor(
    db_path: Annotated[Path | None, typer.Option("--db", help="SQLite database path")] = None,
    execute: Annotated[
        bool,
        typer.Option("--execute", help="Apply the repairs that have an unambiguous fix"),
    ] = False,
    audit_runs_dir: Annotated[
        Path | None,
        typer.Option("--audit-runs-dir", help="Directory holding statement-audit runs"),
    ] = None,
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="CLI defaults config file (overrides default locations)"),
    ] = None,
) -> None:
    """Diagnose local database drift. Defaults to a read-only report.

    Tests cannot catch a database written by a superseded version of the code, which
    is how a stale health score, a config that cannot be saved, and silently missing
    transactions all coexisted with a green suite. This is the standing check for that
    class of problem.

    With --execute, repairs only what has one correct answer (de-duplicating config
    rows, clearing dangling references, recomputing derived tables) after taking a
    pre-mutation backup. Anything needing judgement is reported, never guessed.
    """
    from ledger.maintenance.doctor import DatabaseDoctor
    from ledger.storage.operations import backup_database

    db_path = resolve_db_path(db_path, config_file)
    doctor = DatabaseDoctor(db_path, audit_runs_dir=audit_runs_dir)

    backup_path = None
    if execute:
        backup = backup_database(db_path, reason="pre-doctor")
        backup_path = backup.artifacts.get("backup")
        if not backup.ok:
            for error in backup.errors:
                console.print(f"[red]Backup failed:[/red] {error}")
            console.print("[red]Aborting[/red] — refusing to mutate without a backup.")
            raise typer.Exit(1)
        console.print(f"[dim]Pre-mutation backup:[/dim] {backup_path}")

    result = doctor.run(execute=execute)
    findings = result.details.get("findings", [])

    if not findings:
        console.print("[green]✓[/green] No database drift detected.")
        return

    severity_colour = {"high": "red", "medium": "yellow", "low": "cyan"}
    for finding in findings:
        colour = severity_colour.get(finding["severity"], "white")
        marker = " [dim](repairable)[/dim]" if finding["repairable"] else ""
        console.print(
            f"\n[{colour}]{finding['severity'].upper()}[/{colour}] {finding['summary']}{marker}"
        )
        for detail in finding["details"][:10]:
            console.print(f"    • {detail}")
        if len(finding["details"]) > 10:
            console.print(f"    • … and {len(finding['details']) - 10} more")
        console.print(f"    [dim]{finding['remedy']}[/dim]")

    repaired = result.details.get("repaired", {})
    if execute:
        console.print("")
        if repaired:
            for check, count in repaired.items():
                console.print(f"[green]✓[/green] Repaired {check}: {count:,}")
            console.print(f"[dim]Roll back with the backup at:[/dim] {backup_path}")
        else:
            console.print("No repairable findings — nothing was changed.")
        console.print(f"Remaining findings: {result.counts['remaining']:,}")
    else:
        repairable = sum(1 for finding in findings if finding["repairable"])
        console.print("")
        if repairable:
            console.print(
                f"Pass [bold]--execute[/bold] to repair {repairable} of these "
                "(a pre-mutation backup is taken automatically)."
            )
        else:
            console.print("None of these can be repaired automatically — see each remedy above.")
