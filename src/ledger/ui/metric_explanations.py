"""Shared Streamlit rendering for source-backed metric explanations."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from ledger.reporting.explanations import MetricExplanationService


def _display_name(value: object) -> str:
    return str(value).replace("_", " ").strip().title()


def _component_rows(explanation: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {
            "Component": _display_name(row.get("name", "")),
            "Operation": "+" if row.get("operation") == "add" else "-",
            "Amount": f"${row.get('amount', '0.00')}",
        }
        for row in explanation.get("components", [])
    ]


def _render_explanation(explanation: dict[str, Any]) -> None:
    if explanation.get("status") != "reported":
        unavailable = explanation.get("unavailable") or {}
        st.info(str(unavailable.get("detail") or "This value is unavailable for this period."))
        return

    period = explanation.get("period") or {}
    source_dates = explanation.get("source_date_bounds") or {}
    st.caption(
        f"{period.get('start')} through {period.get('end')} · "
        f"{explanation.get('currency', 'CAD')} · source dates "
        f"{source_dates.get('earliest') or 'unavailable'} through "
        f"{source_dates.get('latest') or 'unavailable'}"
    )
    st.markdown(f"**Formula:** `{explanation.get('formula', '')}`")
    rows = _component_rows(explanation)
    if rows:
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    reconciliation = explanation.get("reconciliation") or {}
    if reconciliation.get("status") == "exact":
        st.success(
            f"Components reconcile exactly to ${reconciliation.get('displayed_total', '0.00')}."
        )
    else:
        st.warning("The available components do not reconcile to the displayed value.")

    details = explanation.get("details") or {}
    for key, value in details.items():
        st.markdown(f"**{_display_name(key)}**")
        st.json(value)

    adjustments = explanation.get("adjustments") or {}
    exclusions = explanation.get("exclusions") or {}
    evidence = explanation.get("relevant_transactions") or []
    with st.expander("Adjustments, exclusions, and supporting transactions"):
        st.markdown("**Adjustments**")
        st.json(adjustments)
        st.markdown("**Exclusions**")
        st.json(exclusions)
        if evidence:
            st.markdown("**Supporting transactions**")
            st.dataframe(pd.DataFrame(evidence), width="stretch", hide_index=True)
        else:
            st.caption("No transaction rows contribute directly to this value.")


def render_metric_explanations(
    db_path: Path,
    start_date: date,
    end_date: date,
    options: dict[str, dict[str, Any]],
    *,
    key: str,
) -> None:
    """Render one consistent explanation selector for headline metrics."""
    if not options:
        return
    with st.expander("Explain this number"):
        label = st.selectbox(
            "Metric",
            list(options),
            key=f"{key}_metric_explanation",
        )
        request = options[label]
        explanation = MetricExplanationService(db_path).explain(
            str(request["metric"]),
            start_date,
            end_date,
            account_id=request.get("account_id"),
            category=request.get("category"),
            balance_basis=request.get("balance_basis"),
            balance_date=request.get("balance_date"),
        )
        _render_explanation(explanation)


__all__ = ["render_metric_explanations"]
