"""Reusable Streamlit display helpers."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from datetime import date
from decimal import Decimal
from html import escape
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from ledger.core import Transaction
from ledger.utils.text import neutralize_csv_cell

# Shared visual accent + money tones (kept in sync with the CSS in streamlit_app.py).
ACCENT = "#4f46e5"
POSITIVE = "#16a34a"
NEGATIVE = "#dc2626"

# Money format for st.column_config.NumberColumn. The printf form "$%.2f" places the
# minus AFTER the symbol ("$-71.45"), which reads as a positive amount at a glance and
# disagreed with format_currency's "-$71" used in every KPI tile. Streamlit's named
# "dollar" format puts the sign first, matching the rest of the UI.
CURRENCY_FORMAT = "dollar"


def format_currency(amount: Decimal | float | int) -> str:
    val = float(amount)
    sign = "-" if val < 0 else ""
    return f"{sign}${abs(val):,.0f}"


def format_currency_precise(amount: Decimal | float | int) -> str:
    val = float(amount)
    sign = "-" if val < 0 else ""
    return f"{sign}${abs(val):,.2f}"


def truncation_notice(count: int, limit: int, *, kpis_limited: bool = True) -> str | None:
    """A caption when a fetched row list hit its SQL LIMIT, else None.

    Pages that sum KPIs / build a CSV from a capped get_transactions() list would
    otherwise silently under-count on a window that exceeds the cap and present the
    clipped totals as authoritative. (At this single-user DB's volume the caps are
    effectively unreachable; this is honesty insurance if the data ever grows.)
    """
    if count >= limit:
        limited_surfaces = "KPIs and CSV export" if kpis_limited else "the table and CSV export"
        return (
            f"Showing the {limit:,} most recent matching rows — {limited_surfaces} are limited "
            "to this window."
        )
    return None


def page_header(title: str, caption: str | None = None, status: str | None = None) -> None:
    caption_html = f'<div class="ledger-page-caption">{escape(caption)}</div>' if caption else ""
    status_html = f'<span class="ledger-status">{escape(status)}</span>' if status else ""
    st.markdown(
        f"""
        <div class="ledger-page-header">
            <div>
                <h1>{escape(title)}</h1>
                {caption_html}
            </div>
            {status_html}
        </div>
        """,
        unsafe_allow_html=True,
    )


def metric_row(metrics: Iterable[tuple[str, str, str | None]]) -> None:
    metrics = list(metrics)
    if not metrics:
        return
    cols = st.columns(len(metrics))
    for col, (label, value, delta) in zip(cols, metrics, strict=False):
        with col:
            st.metric(label, value, delta=delta)


def _date_text(value: object) -> str:
    if value is None:
        return "None"
    if hasattr(value, "date"):
        value = value.date()
    return str(value)


def status_strip_items(
    *,
    start_date: date,
    end_date: date,
    row_count: int | None = None,
    snapshot: dict | None = None,
    source: str = "Live queries",
) -> list[dict[str, str | None]]:
    snapshot_period = "None"
    freshness = "Live"
    if snapshot:
        snapshot_period = f"{snapshot.get('period_start')} to {snapshot.get('period_end')}"
        freshness = _date_text(snapshot.get("created_at"))

    return [
        {"label": "Period", "value": f"{start_date} to {end_date}", "detail": None},
        {
            "label": "Rows",
            "value": f"{row_count:,}" if row_count is not None else "n/a",
            "detail": None,
        },
        {"label": "Snapshot", "value": snapshot_period, "detail": None},
        {"label": "Source", "value": source, "detail": freshness},
    ]


def confirmation_ready(state: Any, key: str, expected: str) -> bool:
    # `state` is usually Streamlit's SessionStateProxy (dict-like, .get) but a plain
    # dict in tests; typed Any so both are accepted without a false dict-mismatch.
    return str(state.get(key, "")).strip() == expected


def _status_strip_markup(items: list[dict[str, str | None]], primary: bool = False) -> str:
    cells = []
    for item in items:
        detail = (
            f'<div class="ledger-status-detail">{escape(item["detail"])}</div>'
            if item["detail"]
            else ""
        )
        tone = item.get("tone")
        value_class = "ledger-status-value" + (f" {tone}" if tone in ("pos", "neg") else "")
        cells.append(
            "".join(
                [
                    '<div class="ledger-status-cell">',
                    f'<div class="ledger-status-label">{escape(item["label"] or "")}</div>',
                    f'<div class="{value_class}">{escape(item["value"] or "")}</div>',
                    detail,
                    "</div>",
                ]
            )
        )
    grid_class = "ledger-status-grid is-primary" if primary else "ledger-status-grid"
    return f'<div class="{grid_class}">{"".join(cells)}</div>'


def status_grid(items: list[dict[str, str | None]], primary: bool = False) -> None:
    """Render a compact status grid. ``primary=True`` gives the row KPI emphasis;
    each item may carry an optional ``"tone"`` of ``"pos"``/``"neg"`` to color its value."""
    if not items:
        return
    st.markdown(_status_strip_markup(items, primary=primary), unsafe_allow_html=True)


def kpi_row(
    metrics: Iterable[tuple[str, str, str | None] | tuple[str, str, str | None, str | None]],
) -> None:
    """Render a page's headline KPI row with primary emphasis (accent border, larger
    values). Each metric is ``(label, value, detail)`` with an optional 4th ``tone`` of
    ``"pos"``/``"neg"`` to color money green/red. Canonical KPI renderer so the hero row
    looks consistent on every page."""
    items: list[dict[str, str | None]] = []
    for metric in metrics:
        label, value, detail = metric[0], metric[1], metric[2]
        tone = metric[3] if len(metric) > 3 else None
        item: dict[str, str | None] = {"label": label, "value": value, "detail": detail}
        if tone:
            item["tone"] = tone
        items.append(item)
    status_grid(items, primary=True)


def period_status_strip(
    *,
    start_date: date,
    end_date: date,
    row_count: int | None = None,
    snapshot: dict | None = None,
    source: str = "Live queries",
) -> None:
    """Render a compact period contract strip for reporting pages."""
    items = status_strip_items(
        start_date=start_date,
        end_date=end_date,
        row_count=row_count,
        snapshot=snapshot,
        source=source,
    )
    status_grid(items)


def empty_state(message: str, detail: str | None = None) -> None:
    st.info(message)
    if detail:
        st.caption(detail)


def transaction_dataframe(transactions: list[Transaction], detailed: bool = False) -> pd.DataFrame:
    rows = []
    for txn in transactions:
        row = {
            "Date": txn.posting_date,
            # Full text always: this frame is also what the CSV download writes,
            # and a 74-char display truncation exported "...  "-terminated
            # descriptions users then tried to reconcile against statements.
            # The grid clips long cells visually on its own.
            "Description": txn.description,
            "Merchant": txn.merchant or "-",
            "Category": txn.category,
            "Amount": float(txn.amount_signed),
        }
        if detailed:
            row.update(
                {
                    "Institution": txn.institution,
                    "Account": txn.account,
                    "Account Type": txn.account_type,
                    "Flow": txn.flow,
                    "Cashflow": txn.cashflow_type,
                    "Txn ID": txn.txn_id,
                }
            )
        rows.append(row)
    return pd.DataFrame(rows)


def render_transaction_table(
    transactions: list[Transaction],
    *,
    detailed: bool = False,
    height: int = 520,
) -> pd.DataFrame:
    df = transaction_dataframe(transactions, detailed=detailed)
    if df.empty:
        empty_state("No transactions match the current filters.")
        return df

    st.dataframe(
        df,
        width="stretch",
        hide_index=True,
        column_config={
            "Date": st.column_config.DateColumn("Date", format="YYYY-MM-DD"),
            "Amount": st.column_config.NumberColumn("Amount", format=CURRENCY_FORMAT),
        },
        height=height,
    )
    return df


def download_dataframe(label: str, df: pd.DataFrame, filename: str) -> None:
    if df.empty:
        return
    # Defuse CSV formula injection: PDF-derived merchant/description text can begin
    # with = + - @ and execute when the download is opened in a spreadsheet app.
    # neutralize_csv_cell leaves numbers/non-strings intact.
    safe = df.map(neutralize_csv_cell)
    st.download_button(
        label,
        data=safe.to_csv(index=False).encode("utf-8"),
        file_name=filename,
        mime="text/csv",
    )


def truncate(value: str, length: int = 80) -> str:
    if len(value) <= length:
        return value
    return value[: length - 3] + "..."


def build_hierarchical_spending(spending_by_category: dict, category_config) -> dict:
    parent_totals: defaultdict[str, Decimal] = defaultdict(Decimal)
    child_details: defaultdict[str, dict] = defaultdict(dict)

    for category, amount in spending_by_category.items():
        parent = category_config.get_category_with_parent(category)
        if parent:
            parent_totals[parent] += amount
            child_details[parent][category] = amount
        else:
            parent_totals[category] += amount

    return {
        "parent_totals": dict(parent_totals),
        "child_details": dict(child_details),
        "hierarchy": category_config.get_category_hierarchy(),
    }


def _theme_is_dark() -> bool:
    """Best-effort read of the active Streamlit theme (inferred from background color)."""
    try:
        return st.context.theme.type == "dark"
    except Exception:
        return False


def style_fig(fig: go.Figure) -> go.Figure:
    """Make a Plotly figure transparent and theme-aware.

    ``plotly_white`` bakes in dark text on a white canvas, which is unreadable on a dark
    Streamlit theme. Instead the canvas goes transparent and the font/grid colors follow
    the active theme, so one chart definition reads well in both light and dark."""
    dark = _theme_is_dark()
    font_color = "#e5e7eb" if dark else "#374151"
    grid_color = "rgba(148, 163, 184, 0.20)" if dark else "rgba(15, 23, 42, 0.08)"
    fig.update_layout(
        paper_bgcolor="rgba(0, 0, 0, 0)",
        plot_bgcolor="rgba(0, 0, 0, 0)",
        font={"color": font_color},
    )
    fig.update_xaxes(gridcolor=grid_color, zerolinecolor=grid_color)
    fig.update_yaxes(gridcolor=grid_color, zerolinecolor=grid_color)
    return fig


def bar_chart(
    labels: list[str],
    values: list[float],
    *,
    title: str | None = None,
    orientation: str = "v",
    height: int = 360,
) -> go.Figure:
    if orientation == "h":
        chart_height = max(height, 110 + len(labels) * 28)
        fig = go.Figure(go.Bar(x=values, y=labels, orientation="h", marker_color=ACCENT))
        fig.update_layout(
            yaxis={"autorange": "reversed", "automargin": True, "tickfont": {"size": 11}},
            xaxis={"automargin": True},
        )
    else:
        chart_height = height
        fig = go.Figure(go.Bar(x=labels, y=values, marker_color=ACCENT))
    layout = {
        "height": chart_height,
        "margin": {
            "l": 170 if orientation == "h" else 36,
            "r": 18,
            "t": 42 if title else 10,
            "b": 32,
        },
        "xaxis_title": "",
        "yaxis_title": "",
    }
    if title:
        layout["title"] = {"text": title}
    fig.update_layout(
        **layout,
    )
    return style_fig(fig)


def line_chart(
    x: list,
    y: list[float],
    *,
    name: str,
    title: str | None = None,
    height: int = 360,
) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(x=x, y=y, mode="lines+markers", name=name, line={"width": 3, "color": ACCENT})
    )
    layout = {
        "height": height,
        "margin": {"l": 36, "r": 18, "t": 42 if title else 10, "b": 32},
        "hovermode": "x unified",
        "xaxis_title": "",
        "yaxis_title": "",
        # Every caller passes period labels ("2026-06"), which Plotly otherwise
        # auto-detects as dates. With a single point in range -- the DEFAULT period --
        # it then auto-ranged to microseconds and drew ticks like
        # "23:59:59.9996 May 31, 2026". Categorical keeps one tick per period.
        "xaxis": {"type": "category"},
    }
    if title:
        layout["title"] = {"text": title}
    fig.update_layout(**layout)
    return style_fig(fig)


def month_bounds(month: str) -> tuple[date, date]:
    year, month_num = [int(part) for part in month.split("-")]
    start = date(year, month_num, 1)
    end = date(year + 1, 1, 1) if month_num == 12 else date(year, month_num + 1, 1)
    return start, end
