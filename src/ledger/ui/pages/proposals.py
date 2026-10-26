"""Local review of exact planning proposals and conditional per-record undo."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from ledger.imports.pdf.readiness import LiveMutationDuringOpenImportRefused
from ledger.proposals import ProposalService
from ledger.storage.snapshots import DatabaseTargetLockError, DatabaseWriteCommittedError
from ledger.ui.components import page_header
from ledger.ui.context import AppContext
from ledger.ui.navigation import take_brief_context


def _value(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _render_preview(preview: dict[str, Any]) -> None:
    st.subheader("Reviewed preview")
    st.write(str(preview["operation"]).replace("_", " ").title())
    before, after = preview.get("before") or {}, preview.get("after") or {}
    st.dataframe(
        pd.DataFrame(
            [
                {"Field": key, "Before": _value(before.get(key)), "After": _value(after.get(key))}
                for key in sorted(set(before) | set(after))
            ]
        ),
        width="stretch",
        hide_index=True,
    )
    if preview.get("effective_date"):
        st.write(f"Effective from: {preview['effective_date']}")
    if preview.get("next_occurrences"):
        st.write("Next occurrences: " + ", ".join(preview["next_occurrences"]))
    st.markdown("**Impact and assumptions**")
    if preview.get("impact"):
        st.json(preview["impact"])
    for assumption in preview.get("assumptions", []):
        st.write(assumption)
    for error in preview.get("validation_errors", []):
        st.warning(error)
    with st.expander("Exact target and reviewed revisions"):
        st.json(
            {
                key: preview.get(key)
                for key in (
                    "proposal_id",
                    "proposal_revision",
                    "preview_revision",
                    "target_revision",
                    "receipt_after_revision",
                    "application_preview_revision",
                )
            }
        )


def _failure(exc: Exception) -> None:
    if isinstance(exc, LiveMutationDuringOpenImportRefused):
        st.error(
            "An import can still promote. Finalize or cancel that run, then refresh this preview."
        )
    elif isinstance(exc, DatabaseTargetLockError):
        st.error("The database is busy. Wait for the other operation, then refresh this preview.")
    elif isinstance(exc, DatabaseWriteCommittedError):
        st.error(
            "The financial write committed but its completion tail needs recovery. Retry this same proposal before filing another."
        )
    elif isinstance(exc, ValueError):
        st.error(str(exc))
    elif isinstance(exc, KeyError):
        st.error("This proposal or target no longer exists. Refresh the proposal list.")
    else:
        st.error(
            "The proposal operation could not complete. Reopen the local store and refresh the preview."
        )


def render(ctx: AppContext) -> None:
    page_header("Proposals", "Inspect the exact change, then apply or reject its reviewed preview.")
    navigation = take_brief_context("proposals")
    store = Path(
        st.text_input(
            "Proposal store",
            value=str(ctx.db_path.parent / "proposals.db"),
            key="planning_proposal_store",
        )
    ).expanduser()
    notice = st.session_state.pop("planning_proposal_notice", None)
    if notice:
        st.success(notice)
    if not store.is_file():
        st.info("No proposals have been filed in this store yet.")
        return
    service = ProposalService(store)
    with st.expander("Recover after an interruption"):
        st.caption(
            "If Ledger stopped during a change and previews no longer open, restore access here. "
            "Your proposals and saved approvals are preserved. Review the recovered preview "
            "to finish the earlier application."
        )
        if st.button("Recover interrupted storage", key="planning_storage_recover"):
            try:
                result = service.recover_planning_storage(ledger_db=ctx.db_path)
            except Exception as exc:
                _failure(exc)
                return
            if not result.ok:
                st.error("Storage recovery could not complete. Retry after resolving the blocker.")
                return
            st.session_state.pop("planning_preview", None)
            st.session_state["planning_proposal_notice"] = (
                "Storage is ready. Open the proposal preview to review its recovery status."
            )
            st.rerun()
    try:
        proposals = service.list_planning_proposals(status="all")
    except Exception as exc:
        _failure(exc)
        return
    if not proposals:
        st.info(
            "No planning proposals are available. Merchant-rule proposals remain available through ledger proposals list."
        )
        return
    by_id = {proposal.proposal_id: proposal for proposal in proposals}
    if navigation and navigation.subject_kind == "proposal":
        if navigation.subject not in by_id:
            st.info("The linked proposal is not available in this store.")
            return
        st.session_state["planning_proposal_selected"] = navigation.subject
    if st.session_state.get("planning_proposal_selected") not in by_id:
        st.session_state.pop("planning_proposal_selected", None)
    selected_id = st.selectbox(
        "Planning proposal",
        list(by_id),
        format_func=lambda identifier: (
            f"{by_id[identifier].intent.operation.replace('_', ' ')} · "
            f"{by_id[identifier].status} · {identifier}"
        ),
        key="planning_proposal_selected",
    )
    selected = by_id[selected_id]
    st.caption(f"Status: {selected.status}")
    with st.expander("Filed intent"):
        st.json(selected.to_dict())
    binding = (str(ctx.db_path.resolve()), str(store.resolve()), selected_id)
    stored = st.session_state.get("planning_preview")
    if stored is not None and stored["binding"] != binding:
        st.session_state.pop("planning_preview", None)
        stored = None
    if st.button("Preview selected proposal", key="planning_preview_button"):
        try:
            preview = service.preview_planning_proposal(
                selected_id, ledger_db=ctx.db_path
            ).to_dict()
        except Exception as exc:
            _failure(exc)
            st.session_state.pop("planning_preview", None)
            return
        stored = {"binding": binding, "preview": preview}
        st.session_state["planning_preview"] = stored
    if stored is None:
        st.info("Open a local preview to inspect concrete before and after values before deciding.")
        return
    preview = stored["preview"]
    _render_preview(preview)
    if preview.get("recovery_required"):
        st.info(
            "An earlier reviewed application needs its saved status recovered. Recovery uses the original approval and receipt."
        )
        if st.button("Recover reviewed application", key="planning_recover"):
            try:
                result = service.apply_planning_proposal(
                    selected_id,
                    ledger_db=ctx.db_path,
                    expected_proposal_revision=preview["proposal_revision"],
                    expected_preview_revision=preview["application_preview_revision"],
                )
                if not result.ok:
                    st.error("; ".join(result.errors) or result.status)
                    return
            except Exception as exc:
                _failure(exc)
                return
            status = result.details["applied_state"]
            st.session_state.pop("planning_preview", None)
            st.session_state["planning_proposal_notice"] = (
                f"Application status recovered: {status}."
            )
            st.rerun()
        return
    if selected.status == "pending":
        apply_col, reject_col = st.columns(2)
        with apply_col:
            if st.button(
                "Apply reviewed proposal",
                key="planning_apply",
                type="primary",
                disabled=not preview["can_apply"],
            ):
                try:
                    result = service.apply_planning_proposal(
                        selected_id,
                        ledger_db=ctx.db_path,
                        expected_proposal_revision=preview["proposal_revision"],
                        expected_preview_revision=preview["preview_revision"],
                    )
                    if not result.ok:
                        st.error("; ".join(result.errors) or result.status)
                        return
                except Exception as exc:
                    _failure(exc)
                    return
                st.session_state.pop("planning_preview", None)
                st.session_state["planning_proposal_notice"] = (
                    "The earlier application was already undone. Its status was recovered."
                    if result.details["applied_state"] == "undone"
                    else "Planning change applied and its receipt saved. Preview the applied proposal to inspect or undo it."
                )
                st.rerun()
        with reject_col:
            if st.button("Reject proposal", key="planning_reject"):
                try:
                    service.reject_planning_proposal(
                        selected_id, expected_proposal_revision=preview["proposal_revision"]
                    )
                except Exception as exc:
                    _failure(exc)
                    return
                st.session_state.pop("planning_preview", None)
                st.session_state["planning_proposal_notice"] = "Proposal rejected."
                st.rerun()
    elif selected.status == "applied" and preview.get("receipt_after_revision"):
        st.caption(
            "Undo restores only this record when no later edit conflicts. Past effective schedule expectations stay preserved."
        )
        if st.button("Undo applied change", key="planning_undo"):
            try:
                result = service.undo_planning_proposal(
                    selected_id,
                    ledger_db=ctx.db_path,
                    expected_after_revision=preview["receipt_after_revision"],
                )
                if not result.ok:
                    st.error("; ".join(result.errors) or result.status)
                    return
            except Exception as exc:
                _failure(exc)
                return
            st.session_state.pop("planning_preview", None)
            st.session_state["planning_proposal_notice"] = (
                "The applied record was restored and the undo was audited."
            )
            st.rerun()


__all__ = ["render"]
