"""Operator CLI for inspecting and deciding isolated deferred proposals."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from ledger.imports.pdf.readiness import LiveMutationDuringOpenImportRefused
from ledger.proposals import Proposal, ProposalService
from ledger.runtime import resolve_runtime_path
from ledger.storage.snapshots import DatabaseTargetLockError

proposals_app = typer.Typer(help="Inspect, apply, or reject isolated deferred proposals.")


def _fail(code: str) -> NoReturn:
    typer.echo(f"proposal_command_failed:{code}")
    raise typer.Exit(1)


def _proposal_payload(proposal: Proposal) -> dict[str, Any]:
    def timestamp(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    return {
        "proposal_id": proposal.proposal_id,
        "proposal_type": proposal.proposal_type,
        "status": proposal.status,
        "merchant_norm": proposal.merchant_norm,
        "pattern": proposal.pattern,
        "category": proposal.category,
        "evidence_id": proposal.evidence_id,
        "created_at": timestamp(proposal.created_at),
        "applied_at": timestamp(proposal.applied_at),
        "rejected_at": timestamp(proposal.rejected_at),
    }


def _print_json(value: object) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


@proposals_app.command("list")
def list_proposals(
    proposals_db: Annotated[
        Path | None,
        typer.Option("--proposals-db", help="Isolated proposal-store path"),
    ] = None,
    status: Annotated[
        str,
        typer.Option("--status", help="pending, applied, rejected, or all"),
    ] = "pending",
    kind: Annotated[str, typer.Option("--kind", help="all, merchant_rule, or planning")] = "all",
) -> None:
    """List deferred proposals, pending by default."""
    proposals_db = resolve_runtime_path(proposals_db, "proposals.db")
    try:
        if kind not in {"all", "merchant_rule", "planning"}:
            raise ValueError("Invalid proposal kind")
        service = ProposalService(proposals_db)
        rows = (
            [_proposal_payload(proposal) for proposal in service.list_proposals(status=status)]
            if kind != "planning" and status != "undone"
            else []
        )
        if kind != "merchant_rule":
            rows.extend(
                {"proposal_type": "planning", **proposal.to_dict()}
                for proposal in service.list_planning_proposals(status=status)
            )
    except ValueError:
        _fail("invalid_argument")
    except FileNotFoundError, OSError:
        _fail("store_unavailable")
    except Exception:
        _fail("internal_error")
    _print_json(rows)


@proposals_app.command("show")
def show_proposal(
    proposal_id: str,
    proposals_db: Annotated[
        Path | None,
        typer.Option("--proposals-db", help="Isolated proposal-store path"),
    ] = None,
    kind: Annotated[str, typer.Option("--kind", help="all, merchant_rule, or planning")] = "all",
) -> None:
    """Show one proposal."""
    proposals_db = resolve_runtime_path(proposals_db, "proposals.db")
    try:
        if kind not in {"all", "merchant_rule", "planning"}:
            raise ValueError("Invalid proposal kind")
        service = ProposalService(proposals_db)
        try:
            if kind == "planning":
                raise KeyError("Planning proposal selected")
            payload = _proposal_payload(service.show_proposal(proposal_id))
        except KeyError:
            if kind == "merchant_rule":
                raise
            payload = {
                "proposal_type": "planning",
                **service.show_planning_proposal(proposal_id).to_dict(),
            }
    except KeyError:
        _fail("not_found")
    except ValueError:
        _fail("invalid_argument")
    except FileNotFoundError, OSError:
        _fail("store_unavailable")
    except Exception:
        _fail("internal_error")
    _print_json(payload)


@proposals_app.command("reject")
def reject_proposal(
    proposal_id: str,
    proposals_db: Annotated[
        Path | None,
        typer.Option("--proposals-db", help="Isolated proposal-store path"),
    ] = None,
    kind: Annotated[
        str, typer.Option("--kind", help="merchant_rule or planning")
    ] = "merchant_rule",
    proposal_revision: Annotated[
        str | None,
        typer.Option("--proposal-revision", help="Exact reviewed planning proposal revision"),
    ] = None,
) -> None:
    """Reject one pending proposal without opening the ledger."""
    proposals_db = resolve_runtime_path(proposals_db, "proposals.db")
    if kind not in {"merchant_rule", "planning"}:
        _fail("invalid_argument")
    if kind == "planning" and not proposal_revision:
        _fail("reviewed_revision_required")
    try:
        service = ProposalService(proposals_db)
        if kind == "planning":
            assert proposal_revision is not None
            result = service.reject_planning_proposal(
                proposal_id, expected_proposal_revision=proposal_revision
            )
            payload = {"proposal_id": result.proposal_id, "status": result.status}
        else:
            proposal = service.reject_proposal(proposal_id)
            payload = {"proposal_id": proposal.proposal_id, "status": proposal.status}
    except KeyError:
        _fail("not_found")
    except ValueError:
        _fail("invalid_status")
    except FileNotFoundError, OSError:
        _fail("store_unavailable")
    except Exception:
        _fail("internal_error")
    _print_json(payload)


@proposals_app.command("apply")
def apply_proposal(
    proposal_id: str,
    db_path: Annotated[Path, typer.Option("--db", help="SQLite ledger database path")],
    proposals_db: Annotated[
        Path | None,
        typer.Option("--proposals-db", help="Isolated proposal-store path"),
    ] = None,
    backup_dir: Annotated[
        Path | None,
        typer.Option("--backup-dir", help="Pre-apply ledger backup directory"),
    ] = None,
    kind: Annotated[
        str, typer.Option("--kind", help="merchant_rule or planning")
    ] = "merchant_rule",
    proposal_revision: Annotated[
        str | None,
        typer.Option("--proposal-revision", help="Exact reviewed planning proposal revision"),
    ] = None,
    preview_revision: Annotated[
        str | None, typer.Option("--preview-revision", help="Exact reviewed local preview revision")
    ] = None,
) -> None:
    """Apply one pending proposal through the guarded ledger write boundary."""
    proposals_db = resolve_runtime_path(proposals_db, "proposals.db")
    if not db_path.is_file():
        _fail("ledger_unavailable")
    if kind not in {"merchant_rule", "planning"}:
        _fail("invalid_argument")
    if kind == "planning":
        if not proposal_revision or not preview_revision:
            _fail("reviewed_revisions_required")
        result = _planning_call(
            lambda: ProposalService(proposals_db).apply_planning_proposal(
                proposal_id,
                ledger_db=db_path,
                expected_proposal_revision=proposal_revision,
                expected_preview_revision=preview_revision,
            )
        )
        if not result.ok:
            _fail(result.status)
        _print_json(
            {
                "proposal_id": proposal_id,
                "status": result.details["applied_state"],
                "counts": result.counts,
                "details": result.details,
            }
        )
        return
    try:
        result = ProposalService(proposals_db).apply_proposal(
            proposal_id,
            ledger_db=db_path,
            backup_dir=backup_dir,
        )
    except KeyError:
        _fail("not_found")
    except LiveMutationDuringOpenImportRefused:
        # A ValueError subclass, but the arguments are fine — the LIVE STATE
        # refuses: an import run can still promote. "invalid_argument" sent the
        # operator hunting a typo instead of the open run.
        _fail("open_import_run")
    except DatabaseTargetLockError:
        # Writer-lock contention is a retry-after-the-other-writer condition,
        # not an internal error.
        _fail("database_busy")
    except ValueError:
        _fail("invalid_argument")
    except FileNotFoundError, OSError:
        _fail("operation_unavailable")
    except Exception:
        _fail("internal_error")
    if not result.ok:
        _fail(result.status)
    _print_json(
        {
            "proposal_id": proposal_id,
            "status": "applied",
            "recovered_from_audit": bool(result.details.get("recovered_from_audit")),
            "counts": result.counts,
        }
    )


def _planning_call(operation: Callable[[], Any]) -> Any:
    """Keep operational failures readable without printing private exception data."""
    try:
        return operation()
    except KeyError:
        _fail("not_found")
    except LiveMutationDuringOpenImportRefused:
        _fail("open_import_run")
    except DatabaseTargetLockError:
        _fail("database_busy")
    except ValueError:
        _fail("review_refresh_required")
    except FileNotFoundError, OSError:
        _fail("operation_unavailable")
    except Exception:
        _fail("internal_error")


@proposals_app.command("recover")
def recover_storage(
    db_path: Annotated[Path, typer.Option("--db", help="SQLite ledger database path")],
    proposals_db: Annotated[
        Path | None, typer.Option("--proposals-db", help="Isolated proposal-store path")
    ] = None,
) -> None:
    """Recover interrupted local storage so previews can reopen; keep proposal decisions."""
    proposals_db = resolve_runtime_path(proposals_db, "proposals.db")
    if not db_path.is_file():
        _fail("ledger_unavailable")
    result = _planning_call(
        lambda: ProposalService(proposals_db).recover_planning_storage(ledger_db=db_path)
    )
    if not result.ok:
        _fail(result.status)
    _print_json({"status": result.status, **result.details})


@proposals_app.command("file")
def file_proposal(
    intent_file: Annotated[
        Path,
        typer.Option(
            "--intent-file", help="JSON file containing one explicit typed planning intent"
        ),
    ],
    proposals_db: Annotated[
        Path | None, typer.Option("--proposals-db", help="Isolated proposal-store path")
    ] = None,
) -> None:
    """Validate and file a planning intent without opening a financial database."""
    proposals_db = resolve_runtime_path(proposals_db, "proposals.db")
    proposal = _planning_call(
        lambda: ProposalService(proposals_db).file_planning_change(
            json.loads(intent_file.read_text(encoding="utf-8"))
        )
    )
    _print_json(proposal.to_dict())


@proposals_app.command("preview")
def preview_proposal(
    proposal_id: str,
    db_path: Annotated[Path, typer.Option("--db", help="SQLite ledger database path")],
    proposals_db: Annotated[
        Path | None, typer.Option("--proposals-db", help="Isolated proposal-store path")
    ] = None,
) -> None:
    """Show exact planning target, before/after, impact, and reviewed revisions."""
    proposals_db = resolve_runtime_path(proposals_db, "proposals.db")
    if not db_path.is_file():
        _fail("ledger_unavailable")
    preview = _planning_call(
        lambda: ProposalService(proposals_db).preview_planning_proposal(
            proposal_id,
            ledger_db=db_path,
        )
    )
    _print_json(preview.to_dict())


@proposals_app.command("undo")
def undo_proposal(
    proposal_id: str,
    db_path: Annotated[Path, typer.Option("--db", help="SQLite ledger database path")],
    after_revision: Annotated[
        str,
        typer.Option(
            "--after-revision",
            help="Receipt after revision reviewed in the applied proposal preview",
        ),
    ],
    proposals_db: Annotated[
        Path | None, typer.Option("--proposals-db", help="Isolated proposal-store path")
    ] = None,
) -> None:
    """Undo only the applied planning record when it has not since changed."""
    proposals_db = resolve_runtime_path(proposals_db, "proposals.db")
    if not db_path.is_file():
        _fail("ledger_unavailable")
    result = _planning_call(
        lambda: ProposalService(proposals_db).undo_planning_proposal(
            proposal_id,
            ledger_db=db_path,
            expected_after_revision=after_revision,
        )
    )
    if not result.ok:
        _fail(result.status)
    _print_json(
        {
            "proposal_id": proposal_id,
            "status": "undone",
            "counts": result.counts,
            "details": result.details,
        }
    )


__all__ = ["proposals_app"]
