"""Explicit local trust and lifecycle controls for installed adapter packages."""

import json
from pathlib import Path
from typing import Annotated

import typer

from ledger.adapters import registry
from ledger.adapters.contracts import AdapterError

adapters_app = typer.Typer(help="Manage explicitly trusted installed import adapters.")


@adapters_app.command("list")
def list_adapters(config: Annotated[Path | None, typer.Option("--config")] = None) -> None:
    """Read inert enabled/disabled metadata without importing extension code."""
    try:
        records = registry.read_configuration(config)["adapters"]
        for record in records:
            typer.echo(
                json.dumps(
                    {
                        key: record[key]
                        for key in ("distribution", "entry_point", "version", "enabled", "sha256")
                    },
                    ensure_ascii=True,
                )
            )
    except AdapterError as error:
        typer.echo(error.code, err=True)
        raise typer.Exit(2) from None


@adapters_app.command("enable")
def enable_adapter(
    distribution: Annotated[str, typer.Option("--distribution")],
    entry_point: Annotated[str, typer.Option("--entry-point")],
    config: Annotated[Path, typer.Option("--config")],
    trust_code: Annotated[
        bool,
        typer.Option(
            "--trust-code", help="Acknowledge this installed package runs trusted Python code."
        ),
    ] = False,
) -> None:
    """Inspect and pin an installed wheel; this executes its trusted entry point."""
    if not trust_code:
        typer.echo(
            "Enabling executes trusted Python code. Supply --trust-code after reviewing the package.",
            err=True,
        )
        raise typer.Exit(2)
    try:
        record = registry.enable_distribution(distribution, entry_point, path=config)
        typer.echo(f"Adapter enabled; artifact SHA-256: {record['sha256']}")
    except AdapterError as error:
        typer.echo(error.code, err=True)
        raise typer.Exit(2) from None


@adapters_app.command("disable")
def disable_adapter(
    distribution: Annotated[str, typer.Option("--distribution")],
    entry_point: Annotated[str, typer.Option("--entry-point")],
    config: Annotated[Path, typer.Option("--config")],
) -> None:
    """Disable parsing while retaining inert metadata for historical interpretation."""
    try:
        registry.disable_distribution(distribution, entry_point, path=config)
        typer.echo("Adapter disabled; historical metadata retained.")
    except AdapterError as error:
        typer.echo(error.code, err=True)
        raise typer.Exit(2) from None
