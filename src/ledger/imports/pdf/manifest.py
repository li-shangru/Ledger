"""Strict JSON manifest helpers for assisted-safe PDF import runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ledger.utils.json_io import utc_now, write_json_atomic

IMPORT_STATES = {
    "staged",
    "needs_review",
    "ready_to_finalize",
    "finalizing",
    "finalized",
    "failed",
    "cancelled",
}


_LEFT_IN_INBOX_ACTION = (
    "Some PDFs remain in the private inbox (unsupported, duplicate, or failed to move) — "
    "review manifest.json 'left_in_inbox' and re-run or remove them."
)
_INVESTMENT_DELEGATION_ACTION = (
    "Select the enabled investment adapter's required source layout in its private "
    "inbox account subfolder, then run the investment import workflow."
)


def next_actions_for(
    status: str,
    pending_review_count: int = 0,
    left_in_inbox_count: int = 0,
    managed_investment_count: int = 0,
) -> list[str]:
    if status == "needs_review" or pending_review_count:
        actions = [
            "Open Review Queue against the staging DB.",
            "Resolve pending merchant/category decisions.",
            "Run ledger import pdf status before finalizing.",
        ]
    elif status == "ready_to_finalize":
        actions = ["Run ledger import pdf finalize --run-dir <run-dir> when ready to promote."]
    elif status == "finalized":
        actions = ["Open the dashboard and inspect the refreshed report snapshot."]
    elif status == "failed":
        actions = ["Inspect errors in manifest.json and fix the failed gate before retrying."]
    elif status == "cancelled":
        actions = ["No promotion is pending for this run."]
    else:
        actions = ["Wait for the current operation to finish."]
    if left_in_inbox_count:
        actions.append(_LEFT_IN_INBOX_ACTION)
    if managed_investment_count:
        actions.append(_INVESTMENT_DELEGATION_ACTION)
    return actions


def left_in_inbox(run_state: dict[str, Any]) -> dict[str, Any]:
    """Aggregate PDFs that stay in the private inbox and need action.

    Actionable buckets are unsupported, duplicates, and failed moves. Statements
    delegated to the investment importer remain untouched in a non-actionable
    ``managed`` count and never trigger the ordinary inbox-leftover warning.
    """
    pdfs = run_state.get("pdfs", [])
    managed = [p for p in pdfs if p.get("skipped_reason") == "managed_by_investment_importer"]
    unsupported = [
        p
        for p in pdfs
        if p.get("skipped_reason")
        and p.get("skipped_reason") not in {"duplicate_pdf_hash", "managed_by_investment_importer"}
    ]
    failed_move = [p for p in pdfs if p.get("finalize_warning")]
    duplicates = list(run_state.get("duplicate_pdfs", []))
    items = [
        {
            "file_hash": p.get("file_hash", ""),
            "pdf": p.get("original_pdf", ""),
            "reason": p.get("skipped_reason") or p.get("finalize_warning") or "duplicate_pdf_hash",
        }
        for p in (*unsupported, *failed_move, *duplicates)
    ]
    counts = {
        "unsupported": len(unsupported),
        "duplicates": len(duplicates),
        "failed_move": len(failed_move),
    }
    if managed:
        counts["managed"] = len(managed)
    return {
        "counts": counts,
        "items": items,
    }


def normalize_status(run_state: dict[str, Any]) -> str:
    status = str(run_state.get("status", "staged"))
    if status in IMPORT_STATES:
        return status
    if status in {"no_new_pdfs", "no_supported_pdfs"}:
        return "cancelled"
    if status in {"validation_failed", "finalize_failed"}:
        return "failed"
    return "staged"


def _database_counts(run_state: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Return the small, exact before/after publication census for UI summaries."""
    keys = ("transactions", "transfers", "import_runs")
    before_raw = run_state.get("target_db_before")
    after_raw = run_state.get("target_db_after_promote")
    if not isinstance(before_raw, dict) or not isinstance(after_raw, dict):
        return {"before": {}, "after": {}, "change": {}}
    before = {key: int(before_raw.get(key) or 0) for key in keys}
    after = {key: int(after_raw.get(key) or 0) for key in keys}
    return {
        "before": before,
        "after": after,
        "change": {key: after[key] - before[key] for key in keys},
    }


def build_manifest(run_dir: Path, run_state: dict[str, Any]) -> dict[str, Any]:
    status = normalize_status(run_state)
    pending = int(run_state.get("pending_review_count") or 0)
    artifacts = {
        "run_state": str(run_dir / "run.json"),
        "staging_db": run_state.get("staging_db_path", ""),
        "target_db": run_state.get("target_db_path", ""),
        "csv_root": run_state.get("csv_root", ""),
        "extraction_manifest": run_state.get("extraction_manifest", ""),
        "statement_validation": run_state.get("statement_validation", ""),
        "merchant_review": run_state.get("merchant_review_json", ""),
        "final_report": run_state.get("final_report", ""),
    }
    errors = []
    for item in run_state.get("pdfs", []):
        errors.extend(item.get("validation_errors", []))
    inbox_leftovers = left_in_inbox(run_state)
    left_in_inbox_count = sum(
        int(inbox_leftovers["counts"].get(key, 0))
        for key in ("unsupported", "duplicates", "failed_move")
    )
    return {
        "schema_version": 1,
        "run_id": run_dir.name,
        "run_dir": str(run_dir),
        "status": status,
        "updated_at": utc_now(),
        "source": {
            "pdf_dir": run_state.get("pdf_dir", ""),
            "processed_dir": run_state.get("processed_dir", ""),
            "base_db": run_state.get("base_db_path", ""),
            "target_db_fingerprint_before": run_state.get("target_db_fingerprint_before", {}),
        },
        "counts": {
            "pdfs": len(run_state.get("pdfs", [])),
            "duplicates": len(run_state.get("duplicate_pdfs", [])),
            "pending_review": pending,
            **(run_state.get("stage_counts") or run_state.get("final_counts") or {}),
        },
        "gates": {
            "statement_validation": "fail" if errors else "pass",
            "review": "fail" if pending else "pass",
            "promotion": "pass" if status == "finalized" else "not_run",
        },
        "artifacts": artifacts,
        "pdfs": run_state.get("pdfs", []),
        "warnings": [
            warning
            for item in run_state.get("pdfs", [])
            for warning in item.get("validation_warnings", [])
        ],
        "errors": errors,
        "left_in_inbox": inbox_leftovers,
        "database_counts": _database_counts(run_state),
        "financial_brief": run_state.get("financial_brief"),
        "next_actions": next_actions_for(
            status,
            pending,
            left_in_inbox_count,
            int(inbox_leftovers["counts"].get("managed", 0)),
        ),
    }


def write_manifest(run_dir: Path, run_state: dict[str, Any]) -> dict[str, Any]:
    manifest = build_manifest(run_dir, run_state)
    write_json_atomic(run_dir / "manifest.json", manifest)
    return manifest


def read_manifest(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "manifest.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    run_path = run_dir / "run.json"
    if not run_path.exists():
        raise FileNotFoundError(f"Import run manifest not found: {path}")
    run_state = json.loads(run_path.read_text(encoding="utf-8"))
    return write_manifest(run_dir, run_state)
