"""Shared rendering for deterministic financial briefs."""

from __future__ import annotations

from typing import Any

import streamlit as st

from ledger.ui.navigation import brief_context, register_brief_link


def render_financial_brief(brief: dict[str, Any] | None, *, heading: bool = True) -> None:
    """Render ordinary brief content while keeping diagnostics compact."""
    if not brief:
        return
    if heading:
        st.subheader("Financial Brief")

    period = brief.get("period") or {}
    comparison = brief.get("comparison") or {}
    if period:
        comparison_text = (
            f" compared with {comparison.get('start')} through {comparison.get('end')}"
            if comparison
            else ""
        )
        st.caption(
            f"{period.get('start')} through {period.get('end')}{comparison_text}. "
            "Associations are evidence signals, not proven causes."
        )

    for observation in brief.get("observations") or []:
        label = str(observation.get("label", "observation")).replace("_", " ").title()
        st.markdown(f"**{observation.get('rank', '')}. {observation.get('title', '')}**")
        st.write(str(observation.get("statement", "")))
        evidence_count = len(observation.get("evidence_ids") or [])
        details = []
        if evidence_count:
            details.append(f"{evidence_count} supporting transaction(s)")
        st.caption(" · ".join([label, *details]))
        context = brief_context(period, observation)
        pages = st.session_state.get("ledger_pages", {})
        if context and context.page in pages:
            st.page_link(
                pages[context.page],
                label="Open supporting details",
                query_params=register_brief_link(context),
            )

    coverage = brief.get("coverage") or {}
    with st.expander("Evidence freshness and coverage"):
        st.json(coverage)


__all__ = ["render_financial_brief"]
