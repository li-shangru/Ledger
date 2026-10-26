"""PDF import center page."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from ledger.imports.pdf.controller import (
    ActionUnavailableError,
    ImportCenterController,
    ImportCenterOperationError,
    ImportCenterSnapshot,
    ImportOperationResult,
    UploadConflictError,
    UploadRejectedError,
)
from ledger.imports.pdf.manifest import read_manifest
from ledger.imports.pdf.paths import Paths
from ledger.imports.pdf.readiness import FINALIZABLE_STATUSES, build_import_readiness
from ledger.runtime import resolve_runtime_path, seed_config_path
from ledger.ui.components import empty_state, page_header
from ledger.ui.context import AppContext, get_active_import_run
from ledger.ui.financial_brief import render_financial_brief

READINESS_LABELS = {
    "target_db_unchanged": "Target DB unchanged",
    "staging_db": "Staging DB present",
    "csv_root": "Staged CSV root present",
    "statement_validation": "Statement validation",
    "review": "Review decisions",
}
_BUSY_KEY = "import_center_busy"
_FLASH_KEY = "import_center_flash"
_UPLOAD_KEY_VERSION = "import_center_upload_key_version"


def _controller_paths(ctx: AppContext) -> Paths:
    """Build UI import paths from the context's already-selected database."""
    db_path = Path(ctx.db_path).expanduser().resolve()
    data_root = db_path.parent
    return Paths(
        pdf_dir=data_root / "inbox",
        processed_dir=data_root / "processed",
        db_path=db_path,
        base_db_path=None,
        backup_dir=data_root / "db-backups",
        ledger_config=seed_config_path("ledger.toml"),
        merchant_rules=seed_config_path("merchants.toml"),
        categories=seed_config_path("categories.toml"),
        runs_dir=data_root / "import-runs",
    )


def _with_readiness(manifest: dict) -> dict:
    run_dir = manifest.get("run_dir")
    if not run_dir:
        return manifest
    enriched = dict(manifest)
    try:
        enriched["readiness"] = build_import_readiness(Path(run_dir))
    except (FileNotFoundError, OSError, KeyError, ValueError) as exc:
        enriched["readiness"] = {
            "gates": {},
            "blockers": [f"Could not read import readiness: {exc}"],
            "counts": {},
        }
    return enriched


def _readiness_gate_rows(readiness: dict | None) -> list[dict[str, str]]:
    if not readiness:
        return []
    blockers = readiness.get("blockers") or []
    rows = []
    for key, label in READINESS_LABELS.items():
        status = str((readiness.get("gates") or {}).get(key, "unknown"))
        detail = ""
        if key == "target_db_unchanged" and blockers:
            detail = "; ".join(str(blocker) for blocker in blockers)
        rows.append({"Gate": label, "Status": status, "Detail": detail})
    return rows


def _before_after_rows(manifest: dict[str, Any]) -> list[dict[str, int | str]]:
    """Presentation rows for the exact database census persisted around promotion."""
    counts = manifest.get("database_counts")
    if not isinstance(counts, dict):
        return []
    before = counts.get("before")
    after = counts.get("after")
    change = counts.get("change")
    if (
        not isinstance(before, dict)
        or not before
        or not isinstance(after, dict)
        or not after
        or not isinstance(change, dict)
        or not change
    ):
        return []
    labels = (
        ("transactions", "Transactions"),
        ("transfers", "Transfers"),
        ("import_runs", "Import runs"),
    )
    return [
        {
            "Record": label,
            "Before": int(before.get(key) or 0),
            "After": int(after.get(key) or 0),
            "Change": int(change.get(key) or 0),
        }
        for key, label in labels
    ]


def _latest_manifest(runs_dir: Path | None = None) -> dict | None:
    runs_dir = resolve_runtime_path(runs_dir, "import-runs")
    active = get_active_import_run(runs_dir)
    if active:
        return _with_readiness(read_manifest(active["run_dir"]))

    if not runs_dir.exists():
        return None
    candidates = sorted(
        (
            path
            for path in runs_dir.iterdir()
            if path.is_dir() and (path / "manifest.json").exists()
        ),
        reverse=True,
    )
    if not candidates:
        return None
    return _with_readiness(read_manifest(candidates[0]))


def _next_action(manifest: dict | None, inbox_count: int, pending_reviews: int) -> tuple[str, str]:
    if manifest is None:
        if inbox_count:
            return "Stage inbox PDFs", "Run `./scripts/import-pdf-inbox stage`."
        return (
            "No import action",
            "Drop statement PDFs into the private runtime inbox when you are ready.",
        )

    status = str(manifest.get("status", "unknown"))
    if status == "staged":
        return "Review staged import", "Inspect reconciliation and review blockers before finalize."
    if status == "needs_review" or pending_reviews:
        return "Resolve Review Queue", "Finish pending merchant/category decisions."
    if status == "ready_to_finalize":
        run_dir = manifest.get("run_dir") or "<private-runtime>/import-runs/<run-id>"
        return "Finalize import", f"Run `./scripts/import-pdf-inbox finalize --run-dir {run_dir}`."
    if status == "finalizing":
        return "Wait for finalize", "The run is currently promoting the staged database."
    if status == "finalized":
        return "Import complete", "Review the dashboard and archive any external copies manually."
    if status == "failed":
        return "Inspect failure", "Open the manifest errors, then cancel or rerun stage."
    if status == "cancelled":
        return "Start a new run", "Stage inbox PDFs when ready."
    return "Inspect manifest", "Use `./scripts/import-pdf-inbox status --run-dir ...` for details."


def _snapshot_next_action(snapshot: ImportCenterSnapshot) -> tuple[str, str]:
    """Describe the action admitted by live readiness and publication state."""
    actions = set(snapshot.actions)
    if "resume_finalize" in actions:
        return (
            "Resume finalize",
            "Continue the existing recovery-safe finalize operation; publication will not repeat.",
        )
    if "review" in actions:
        return "Resolve Review Queue", "Finish the staged merchant/category decisions."
    if "finalize" in actions:
        return "Finalize import", "All live readiness gates are clear."
    if "stage" in actions:
        return "Stage inbox PDFs", "Build an isolated staging database from the private inbox."
    if snapshot.status == "finalized":
        return "Import complete", "Review the financial brief and dashboard results."
    if snapshot.status == "cancelled":
        return "Run cancelled", "Upload or retain another PDF before starting a new run."
    if snapshot.status == "failed":
        return "Inspect failure", "Open diagnostics, then cancel the run after fixing the cause."
    if snapshot.status in FINALIZABLE_STATUSES:
        return "Finalize blocked", "Open the live readiness gates below for the required remedy."
    return "No import action", "Drop PDF statements above when you are ready."


def _store_flash(
    severity: str,
    message: str,
    *,
    output: str = "",
) -> None:
    st.session_state[_FLASH_KEY] = {
        "severity": severity,
        "message": message,
        "output": output,
    }


def _render_flash() -> None:
    flash = st.session_state.pop(_FLASH_KEY, None)
    if not isinstance(flash, dict):
        return
    message = str(flash.get("message") or "")
    severity = str(flash.get("severity") or "info")
    renderer = {
        "success": st.success,
        "warning": st.warning,
        "error": st.error,
    }.get(severity, st.info)
    renderer(message)
    output = str(flash.get("output") or "").strip()
    if output:
        with st.expander("Operation details"):
            st.code(output, language="text")


def _clear_read_caches_after_change(*, changed: bool) -> None:
    """Invalidate period reads after a database or import-run mutation."""
    if changed:
        st.cache_data.clear()


def _run_action(
    label: str,
    callback: Callable[[], ImportOperationResult],
) -> None:
    if st.session_state.get(_BUSY_KEY):
        st.warning("Another import action is already running.")
        return
    st.session_state[_BUSY_KEY] = True
    progress = st.status(label, expanded=True)
    try:
        result = callback()
    except ImportCenterOperationError as exc:
        progress.update(label=f"{label} failed", state="error")
        _store_flash("error", str(exc), output=exc.output)
    except (ActionUnavailableError, OSError, ValueError) as exc:
        progress.update(label=f"{label} blocked", state="error")
        _store_flash("error", str(exc))
    else:
        progress.update(label=result.message, state="complete", expanded=False)
        severity = "success" if result.changed else "info"
        _store_flash(severity, result.message, output=result.output)
        _clear_read_caches_after_change(changed=result.changed)
    finally:
        st.session_state[_BUSY_KEY] = False
    st.rerun()


def _render_uploads(importer: ImportCenterController) -> None:
    st.subheader("Add statements")
    st.caption(
        "Drop supported PDF statements here. Files are copied into the private inbox "
        "with owner-only permissions and content-hash duplicate protection."
    )
    version = int(st.session_state.get(_UPLOAD_KEY_VERSION, 0))
    uploads = st.file_uploader(
        "PDF statements",
        type=["pdf"],
        accept_multiple_files=True,
        key=f"import_center_uploads_{version}",
    )
    if st.button(
        "Add PDFs to inbox",
        disabled=not uploads or bool(st.session_state.get(_BUSY_KEY)),
        key="import_center_accept_uploads",
    ):
        stored = 0
        existing = 0
        errors: list[str] = []
        for upload in uploads or []:
            try:
                receipt = importer.upload(upload.name, upload.getvalue())
            except (UploadRejectedError, UploadConflictError, OSError) as exc:
                errors.append(f"{upload.name}: {exc}")
                continue
            stored += int(receipt.status == "stored")
            existing += int(receipt.status == "already_present")
        if errors:
            _store_flash(
                "error",
                "Some PDFs were not accepted. " + " ".join(errors),
            )
        else:
            _store_flash(
                "success",
                f"Added {stored} PDF(s); {existing} duplicate upload(s) were already present.",
            )
            st.session_state[_UPLOAD_KEY_VERSION] = version + 1
        st.rerun()


def _render_brief(brief: dict[str, Any]) -> None:
    render_financial_brief(brief)


def _render_diagnostics(
    snapshot: ImportCenterSnapshot,
    manifest: dict[str, Any],
) -> None:
    with st.expander("Diagnostics and recovery details"):
        st.markdown(
            f"**Protocol:** {snapshot.publication_phase or 'not started'} · "
            f"publication completed: {str(snapshot.publication_completed).lower()}"
        )
        for diagnostic in snapshot.diagnostics:
            st.error(diagnostic)
        for warning in manifest.get("warnings") or []:
            st.warning(str(warning))
        for error in manifest.get("errors") or []:
            st.error(str(error))

        next_actions = manifest.get("next_actions", [])
        if next_actions:
            st.markdown("**Recorded next actions**")
            for action in next_actions:
                st.caption(str(action))

        artifacts = manifest.get("artifacts", {})
        artifact_rows = [
            {"Artifact": key, "Path": value} for key, value in artifacts.items() if value
        ]
        if artifact_rows:
            st.markdown("**Artifacts**")
            st.dataframe(
                pd.DataFrame(artifact_rows),
                width="stretch",
                hide_index=True,
                height=240,
            )

        pdf_rows = [
            {
                "PDF": Path(item.get("original_pdf", item.get("staged_pdf", ""))).name,
                "Statement": item.get("statement_type", ""),
                "Rows": item.get("transaction_count", 0),
                "Skipped": item.get("skipped_reason", ""),
                "Errors": len(item.get("validation_errors", [])),
                "Warnings": len(item.get("validation_warnings", [])),
            }
            for item in manifest.get("pdfs", [])
        ]
        if pdf_rows:
            st.markdown("**PDF details**")
            st.dataframe(
                pd.DataFrame(pdf_rows),
                width="stretch",
                hide_index=True,
                height=280,
            )

        st.markdown("**CLI recovery commands**")
        st.code(
            "\n".join(
                [
                    "./scripts/import-pdf-inbox stage",
                    "./scripts/import-pdf-inbox status --run-dir <private-runtime>/import-runs/<run-id>",
                    "./scripts/import-pdf-inbox finalize --run-dir <private-runtime>/import-runs/<run-id>",
                    "./scripts/import-pdf-inbox cancel --run-dir <private-runtime>/import-runs/<run-id>",
                ]
            ),
            language="bash",
        )


def render(ctx: AppContext) -> None:
    page_header(
        "Import Center",
        "Monitor assisted-safe PDF import runs before explicitly finalizing promotion.",
        "Assisted safe",
    )

    importer = ImportCenterController(_controller_paths(ctx))
    snapshot = importer.snapshot()
    manifest = snapshot.manifest

    _render_flash()
    _render_uploads(importer)

    cols = st.columns(4)
    pending_reviews = int(
        ((snapshot.readiness or {}).get("counts") or {}).get("pending_review", ctx.pending_reviews)
    )
    cols[0].metric("Inbox PDFs", f"{snapshot.inbox_pdf_count:,}")
    cols[1].metric("Pending Review", f"{pending_reviews:,}")
    cols[2].metric("Active Source", ctx.review_source["source_kind"])
    cols[3].metric("Run Status", snapshot.status)
    st.markdown(f"**Current state:** `{snapshot.status}`")

    action, detail = _snapshot_next_action(snapshot)
    st.info(f"Next action: {action}. {detail}")

    busy = bool(st.session_state.get(_BUSY_KEY))
    action_cols = st.columns(4)
    if action_cols[0].button(
        "Stage inbox PDFs",
        type="primary",
        disabled="stage" not in snapshot.actions or busy,
        key="import_center_stage",
    ):
        _run_action("Staging inbox PDFs", importer.stage)

    if "review" in snapshot.actions:
        action_cols[1].markdown("[Open Review Queue](/review)")
    else:
        action_cols[1].caption("Review Queue is clear")

    resume = "resume_finalize" in snapshot.actions
    finalize_label = "Resume finalize" if resume else "Finalize import"
    if action_cols[2].button(
        finalize_label,
        type="primary" if resume else "secondary",
        disabled=("finalize" not in snapshot.actions and not resume) or busy,
        key="import_center_finalize",
    ):
        _run_action(
            "Resuming finalize" if resume else "Finalizing import",
            lambda: importer.finalize(run_dir=snapshot.run_dir),
        )

    confirm_cancel = action_cols[3].checkbox(
        "Confirm cancellation",
        disabled="cancel" not in snapshot.actions or busy,
        key="import_center_confirm_cancel",
        help="Cancellation discards this staged run and never publishes its database.",
    )
    if action_cols[3].button(
        "Cancel import run",
        disabled="cancel" not in snapshot.actions or not confirm_cancel or busy,
        key="import_center_cancel",
    ):
        _run_action(
            "Cancelling import run",
            lambda: importer.cancel(run_dir=snapshot.run_dir),
        )

    st.subheader("Current Run")
    if not manifest:
        empty_state(
            "No import run manifest found.", "Add PDF statements above, then stage the inbox."
        )
    else:
        status = snapshot.status
        st.metric("Run Status", status)

        # A manifest records the gate values as they stood when the run was staged and
        # is not rewritten when a gate is later satisfied. For a FINALIZED run those
        # values are history, not instructions -- rendering them live made a completed
        # import permanently display "Review Gate: fail" alongside "Import complete",
        # plus three obsolete next actions telling the user to resolve reviews that
        # were resolved before the promotion happened.
        is_complete = status in {"finalized", "cancelled"}
        gates = manifest.get("gates", {})
        if is_complete:
            with st.expander(f"Gate values recorded when this run was staged ({status})"):
                st.dataframe(
                    pd.DataFrame(
                        [
                            {"Gate": "Statement validation", "At stage time": v}
                            for v in [gates.get("statement_validation", "unknown")]
                        ]
                        + [{"Gate": "Review", "At stage time": gates.get("review", "unknown")}]
                        + [
                            {
                                "Gate": "Promotion",
                                "At stage time": gates.get("promotion", "unknown"),
                            }
                        ],
                    ),
                    width="stretch",
                    hide_index=True,
                )
        else:
            gate_cols = st.columns(3)
            gate_cols[0].metric(
                "Statement Validation", gates.get("statement_validation", "unknown")
            )
            gate_cols[1].metric("Review Gate", gates.get("review", "unknown"))
            gate_cols[2].metric("Promotion", gates.get("promotion", "unknown"))

        next_actions = manifest.get("next_actions", []) if not is_complete else []
        if next_actions:
            st.caption(f"Recorded next step: {next_actions[0]}")

        readiness = snapshot.readiness or manifest.get("readiness")
        readiness_rows = _readiness_gate_rows(readiness)
        if readiness_rows:
            # The gate table is always shown -- it is the page's record of what the run
            # checked, and it is what tells a user the run was verified. Only the
            # call-to-action beneath it is conditional: "Finalize blocked" / "gates are
            # clear" describe pending work, and a completed run has none.
            st.subheader("Finalize Readiness")
            # Rendered as text, not only as a dataframe: st.dataframe draws to a
            # canvas, so the gate names and statuses were absent from the DOM --
            # unreadable by a screen reader, un-searchable, and invisible to the UI
            # smoke gate that is supposed to verify them.
            st.markdown(
                "\n".join(
                    f"- **{row['Gate']}** — {row['Status']}"
                    + (f" ({row['Detail']})" if row["Detail"] else "")
                    for row in readiness_rows
                )
            )
            blockers = readiness.get("blockers", []) if readiness else []
            if is_complete:
                st.caption(f"Gates as recorded for this {status} run. No action is pending.")
            elif blockers:
                st.warning("Finalize blocked: " + " ".join(str(blocker) for blocker in blockers))
            else:
                st.success("Finalize readiness gates are clear.")

        counts = manifest.get("counts") or {}
        summary_keys = [
            ("Transactions", "transactions"),
            ("Transfers", "transfers"),
            ("PDFs", "pdfs"),
            ("Duplicates", "duplicates"),
        ]
        summary_cols = st.columns(4)
        for column, (label, key) in zip(summary_cols, summary_keys, strict=True):
            column.metric(label, f"{int(counts.get(key) or 0):,}")

        before_after_rows = _before_after_rows(manifest)
        if is_complete and before_after_rows:
            st.subheader("Before / after")
            st.caption(
                "Exact database record counts immediately before staging and after publication."
            )
            st.dataframe(
                pd.DataFrame(before_after_rows),
                width="stretch",
                hide_index=True,
            )

        brief = manifest.get("financial_brief")
        if is_complete and isinstance(brief, dict):
            _render_brief(brief)

        _render_diagnostics(snapshot, manifest)

    if snapshot.diagnostics and not manifest:
        with st.expander("Diagnostics and recovery details"):
            for diagnostic in snapshot.diagnostics:
                st.error(diagnostic)
