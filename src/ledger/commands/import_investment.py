"""CLI commands for staged historical investment imports."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from ledger.investments.workflow import (
    cancel_investment_import,
    finalize_investment_import,
    investment_import_status,
    stage_investment_import,
)
from ledger.runtime import resolve_runtime_path

import_investment_app = typer.Typer(
    help="Stage, inspect, finalize, and cancel historical investment imports."
)


_PUBLIC_COUNT_KEYS = {
    "evidence_pdfs",
    "pairs",
    "statements",
    "inserted",
    "replaced",
    "unchanged",
    "archived",
    "pending_archives",
}
_PUBLIC_GATE_KEYS = {
    "staging_integrity",
    "source_identity",
    "target_unchanged",
    "conflict_authorization",
    "publication",
    "archive",
}
_PUBLIC_STRUCTURE_KEYS = {"asset_balances", "positions", "performance", "activities"}
_PUBLIC_VALUE_KEYS = {"total_nav", "statement_return", "realized_pl", "unrealized_pl"}
_PUBLIC_DIRECTIONS = {"unchanged", "added", "removed", "increased", "decreased"}
_PUBLIC_STATES = {
    "staging",
    "staged",
    "finalizing",
    "archive_pending",
    "finalized",
    "cancelled",
    "failed",
}


def public_investment_import_result(payload: dict[str, Any]) -> dict[str, Any]:
    """Return the strict aggregate-only CLI view of a private run manifest."""
    counts_value = payload.get("counts")
    raw_counts = counts_value if isinstance(counts_value, dict) else {}
    counts = {
        key: int(value)
        for key, value in raw_counts.items()
        if key in _PUBLIC_COUNT_KEYS and isinstance(value, int) and not isinstance(value, bool)
    }
    lifecycle_value = payload.get("lifecycle")
    raw_lifecycle = lifecycle_value if isinstance(lifecycle_value, dict) else {}
    aliases = raw_lifecycle.get("closed_aliases")
    closed_count = len(aliases) if isinstance(aliases, list) else 0
    gates_value = payload.get("gates")
    raw_gates = gates_value if isinstance(gates_value, dict) else {}
    gates = {
        key: value
        for key, value in raw_gates.items()
        if key in _PUBLIC_GATE_KEYS
        and isinstance(value, str)
        and value in {"pass", "fail", "pending", "not_run"}
    }
    state_value = payload.get("status")
    state = (
        state_value if isinstance(state_value, str) and state_value in _PUBLIC_STATES else "unknown"
    )
    gates.setdefault("publication", "pass" if payload.get("published") else "not_run")
    gates.setdefault(
        "archive",
        "pass" if state == "finalized" else "pending" if state == "archive_pending" else "not_run",
    )
    review_value = payload.get("conflict_review")
    raw_review = review_value if isinstance(review_value, dict) else {}
    raw_periods = raw_review.get("summary")
    periods: list[dict] = []
    if isinstance(raw_periods, list):
        for index, raw in enumerate(raw_periods, start=1):
            if not isinstance(raw, dict):
                continue
            structure: dict[str, dict[str, int]] = {}
            raw_structure = raw.get("structure")
            if isinstance(raw_structure, dict):
                for name, delta in raw_structure.items():
                    if name not in _PUBLIC_STRUCTURE_KEYS or not isinstance(delta, dict):
                        continue
                    safe_delta = {
                        key: int(value)
                        for key, value in delta.items()
                        if key in {"before", "after", "delta"}
                        and isinstance(value, int)
                        and not isinstance(value, bool)
                    }
                    if safe_delta:
                        structure[name] = safe_delta
            value_changes: dict[str, str] = {}
            raw_values = raw.get("value_changes")
            if isinstance(raw_values, dict):
                value_changes = {
                    key: value
                    for key, value in raw_values.items()
                    if key in _PUBLIC_VALUE_KEYS
                    and isinstance(value, str)
                    and value in _PUBLIC_DIRECTIONS
                }
            periods.append(
                {
                    "review_index": index,
                    "structure": structure,
                    "value_changes": value_changes,
                }
            )
    last_error = payload.get("last_error")
    errors = (
        [last_error]
        if last_error
        in {
            "archive_failed",
            "publication_durability_failed",
            "publication_failed",
            "source_identity_failed",
            "conflict_authorization_failed",
            "staging_failed",
            "status_failed",
            "finalize_failed",
            "cancel_failed",
        }
        else []
    )
    return {
        "run_id": str(payload.get("run_id") or "unknown"),
        "state": state,
        "counts": counts,
        "lifecycle": {
            "closed_accounts": closed_count,
            "transfer_destination_recorded": bool(raw_lifecycle.get("transfer_destination")),
            "status_confirmation_recorded": bool(raw_lifecycle.get("status_confirmed_on")),
        },
        "gates": gates,
        "conflict_review": {"count": len(periods), "periods": periods},
        "errors": errors,
    }


def _print_payload(payload: dict) -> None:
    typer.echo(json.dumps(public_investment_import_result(payload), indent=2))


def _run_public_operation(
    operation: Callable[[], dict[str, Any]],
    *,
    error_code: str,
    required_success_state: str | None = None,
) -> None:
    """Keep private workflow exception content behind the CLI output boundary."""
    try:
        payload = operation()
    except Exception, SystemExit:
        _print_payload(
            {
                "run_id": "unknown",
                "status": "failed",
                "last_error": error_code,
            }
        )
        raise typer.Exit(code=1) from None
    _print_payload(payload)
    if required_success_state is not None and payload.get("status") != required_success_state:
        raise typer.Exit(code=1)


@import_investment_app.command("stage")
def import_investment_stage(
    institution: Annotated[
        str | None,
        typer.Option("--institution", help="Enabled investment format; omit to use its default."),
    ] = None,
    inbox_dir: Annotated[Path | None, typer.Option("--inbox-dir")] = None,
    db_path: Annotated[Path | None, typer.Option("--db")] = None,
    runs_dir: Annotated[Path | None, typer.Option("--runs-dir")] = None,
    processed_dir: Annotated[Path | None, typer.Option("--processed-dir")] = None,
    backup_dir: Annotated[Path | None, typer.Option("--backup-dir")] = None,
    mark_closed: Annotated[
        list[str] | None,
        typer.Option("--mark-closed", help="Account alias to mark closed; repeat per account."),
    ] = None,
    transfer_destination: Annotated[str | None, typer.Option("--transfer-destination")] = None,
    status_confirmed_on: Annotated[datetime | None, typer.Option("--status-confirmed-on")] = None,
    replace_conflicts: Annotated[
        bool,
        typer.Option("--replace-conflicts", help="Explicitly replace changed account-periods."),
    ] = False,
) -> None:
    """Stage complete statements through an enabled investment format."""
    inbox_dir = resolve_runtime_path(inbox_dir, "inbox")
    db_path = resolve_runtime_path(db_path, "ledger.db")
    runs_dir = resolve_runtime_path(runs_dir, "investment-import-runs")
    processed_dir = resolve_runtime_path(processed_dir, "processed")
    backup_dir = resolve_runtime_path(backup_dir, "db-backups")

    def operation() -> dict[str, Any]:
        run_dir = stage_investment_import(
            inbox_dir=inbox_dir,
            db_path=db_path,
            runs_dir=runs_dir,
            processed_dir=processed_dir,
            backup_dir=backup_dir,
            institution=institution,
            closed_aliases=tuple(mark_closed or ()),
            transfer_destination=transfer_destination,
            status_confirmed_on=status_confirmed_on.date() if status_confirmed_on else None,
            replace_conflicts=replace_conflicts,
        )
        return investment_import_status(run_dir)

    _run_public_operation(operation, error_code="staging_failed")


@import_investment_app.command("status")
def import_investment_status_command(
    run_dir: Annotated[Path | None, typer.Option("--run-dir")] = None,
    runs_dir: Annotated[Path | None, typer.Option("--runs-dir")] = None,
) -> None:
    """Print one privacy-safe investment import manifest."""
    runs_dir = resolve_runtime_path(runs_dir, "investment-import-runs")
    _run_public_operation(
        lambda: investment_import_status(run_dir, runs_dir=runs_dir),
        error_code="status_failed",
    )


@import_investment_app.command("finalize")
def import_investment_finalize_command(
    run_dir: Annotated[Path | None, typer.Option("--run-dir")] = None,
    runs_dir: Annotated[Path | None, typer.Option("--runs-dir")] = None,
) -> None:
    """Publish staging, then archive verified sources."""
    runs_dir = resolve_runtime_path(runs_dir, "investment-import-runs")
    _run_public_operation(
        lambda: finalize_investment_import(run_dir, runs_dir=runs_dir),
        error_code="finalize_failed",
        required_success_state="finalized",
    )


@import_investment_app.command("cancel")
def import_investment_cancel_command(
    run_dir: Annotated[Path | None, typer.Option("--run-dir")] = None,
    runs_dir: Annotated[Path | None, typer.Option("--runs-dir")] = None,
    reason: Annotated[str, typer.Option("--reason")] = "manual_cancel",
) -> None:
    """Cancel an unpublished staged run."""
    runs_dir = resolve_runtime_path(runs_dir, "investment-import-runs")
    _run_public_operation(
        lambda: cancel_investment_import(run_dir, runs_dir=runs_dir, reason=reason),
        error_code="cancel_failed",
    )
