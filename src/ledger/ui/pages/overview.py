"""Overview page."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from ledger.analysis.health_scorer import MIN_ACTIVE_DAYS, SCORING_WINDOW_DAYS
from ledger.evidence import EvidenceService
from ledger.insights import InsightEngine
from ledger.planning import BudgetService, PlanningReadinessService
from ledger.query import LedgerQueryService
from ledger.reporting import FinancialBriefService, ReportSnapshotService
from ledger.rules import RuleApplicationService
from ledger.ui.cache import cache_database_read
from ledger.ui.components import (
    CURRENCY_FORMAT,
    NEGATIVE,
    POSITIVE,
    bar_chart,
    build_hierarchical_spending,
    format_currency,
    format_currency_precise,
    kpi_row,
    page_header,
    period_status_strip,
    status_grid,
    style_fig,
)
from ledger.ui.context import AppContext
from ledger.ui.financial_brief import render_financial_brief
from ledger.ui.metric_explanations import render_metric_explanations
from ledger.utils.dates import next_month_start as _next_month_start
from ledger.utils.dates import year_month

# ---------------------------------------------------------------------------
# Cached read-only helpers
# Keys include main/WAL/journal revisions as well as path and dates.
# st.cache_data.clear() (called by mutation pages) clears ALL of these.
# ---------------------------------------------------------------------------


@cache_database_read
def _cached_true_cost_kpis(db_path_str: str, start_date: date, end_date: date) -> dict:
    """Return compute_true_cost_kpis result; cached per (db_path, period)."""
    return LedgerQueryService(Path(db_path_str)).compute_true_cost_kpis(start_date, end_date)


@cache_database_read
def _cached_financial_brief(db_path_str: str, start_date: date, end_date: date) -> dict:
    return FinancialBriefService(Path(db_path_str)).build(start_date, end_date)


@cache_database_read
def _cached_true_cost_by_category(db_path_str: str, start_date: date, end_date: date) -> dict:
    """Return get_true_cost_by_category result; cached per (db_path, period)."""
    return LedgerQueryService(Path(db_path_str)).get_true_cost_by_category(start_date, end_date)


@cache_database_read
def _cached_spending_by_merchant(
    db_path_str: str, start_date: date, end_date: date, top_n: int = 10
) -> dict:
    """Return get_spending_by_merchant result; cached per (db_path, period, top_n)."""
    return LedgerQueryService(Path(db_path_str)).get_spending_by_merchant(
        start_date, end_date, top_n=top_n
    )


@cache_database_read
def _cached_income_sources(
    db_path_str: str, start_date: date, end_date: date, top_n: int = 8
) -> dict:
    """Return get_income_sources result; cached per (db_path, period, top_n)."""
    return LedgerQueryService(Path(db_path_str)).get_income_sources(
        start_date, end_date, top_n=top_n
    )


@cache_database_read
def _cached_monthly_kpis(db_path_str: str, month_start: date, month_end: date) -> dict:
    """Return compute_true_cost_kpis for a single month; cached per (db_path, month)."""
    return LedgerQueryService(Path(db_path_str)).compute_true_cost_kpis(month_start, month_end)


def _status_text(value: object) -> str:
    return "Never" if value is None else str(value)


def _monthly_cashflow(db_path_str: str, start: date, end: date) -> pd.DataFrame:
    rows = []
    current = start.replace(day=1)
    while current <= end.replace(day=1):
        month_end = min(_next_month_start(current) - timedelta(days=1), end)
        month_start = max(current, start)
        kpis = _cached_monthly_kpis(db_path_str, month_start, month_end)
        rows.append(
            {
                "Month": year_month(current),
                "Income": float(kpis["income"]),
                "Expenses": float(kpis["expenses"]),
                "Net": float(kpis["net"]),
            }
        )
        current = _next_month_start(current)
    return pd.DataFrame(rows)


def _cashflow_chart(df: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    fig.add_trace(go.Bar(x=df["Month"], y=df["Income"], name="Income", marker_color=POSITIVE))
    fig.add_trace(go.Bar(x=df["Month"], y=df["Expenses"], name="Expenses", marker_color=NEGATIVE))
    fig.add_trace(go.Scatter(x=df["Month"], y=df["Net"], name="Net", mode="lines+markers"))
    fig.update_layout(
        height=380,
        barmode="group",
        margin={"l": 12, "r": 12, "t": 20, "b": 28},
        hovermode="x unified",
        xaxis_title="",
        yaxis_title="",
        legend={"orientation": "h", "y": 1.08},
        # A year-month string is a period label, not a timestamp. Without this Plotly built a
        # datetime axis and, for the default single-month period, rendered
        # microsecond ticks around midnight with two absurdly wide bars.
        xaxis={"type": "category"},
    )
    return style_fig(fig)


def _render_status_strip(ctx: AppContext, snapshot: dict | None) -> None:
    latest_import = ctx.service.get_latest_import_run()
    latest_rules = RuleApplicationService(ctx.db_path).latest_run()
    status_grid(
        [
            {"label": "Data Through", "value": str(ctx.latest), "detail": None},
            {"label": "Pending Review", "value": f"{ctx.pending_reviews:,}", "detail": None},
            {
                "label": "Last Import",
                "value": _status_text(latest_import["run_at"].date() if latest_import else None),
                "detail": None,
            },
            {
                "label": "Last Rule Apply",
                "value": _status_text(latest_rules["applied_at"].date() if latest_rules else None),
                "detail": None,
            },
            # A transient snapshot (id None) is computed at render time, so its
            # created_at is always "today" — dating it here made the card
            # incapable of ever showing staleness while the Evidence strip on the
            # same page warned the stored snapshot was old. Say what it is.
            {
                "label": "Report Snapshot",
                "value": "Live"
                if snapshot and snapshot.get("id") is None
                else _status_text(snapshot["created_at"].date() if snapshot else None),
                "detail": "computed for this view; not stored"
                if snapshot and snapshot.get("id") is None
                else None,
            },
        ]
    )


def _render_baseline_notice(snapshot: dict | None) -> None:
    """Disclose a comparison baseline that is shorter than the selected period.

    The baseline is clamped to the earliest transaction date, so a period starting
    near the beginning of the data compares against a much shorter window.
    Calling the windows comparable would turn their different lengths into a
    misleading percentage change.
    """
    payload = (snapshot or {}).get("payload") or {}
    period = payload.get("period") or {}
    if period.get("baseline_comparable", True):
        return
    baseline_days = period.get("baseline_days")
    days = period.get("days")
    if not baseline_days or not days:
        st.info("No comparable prior period, so percentage changes are not shown.")
        return
    st.warning(
        f"The comparison baseline covers only {baseline_days} day(s) against this "
        f"period's {days} — there is not enough earlier data for an equal-length "
        "window. Percentage changes are suppressed; the amounts below are still exact.",
        icon="⚠️",
    )


def _render_reporting_signals(snapshot: dict | None) -> None:
    if not snapshot:
        return
    payload = snapshot.get("payload") or {}
    recurring = payload.get("recurring_costs") or []
    top_categories = payload.get("top_categories") or []
    movement = payload.get("income_spend_net_movement") or {}
    cost_candidates = payload.get("cost_cutting_candidates") or []
    category_drift = payload.get("category_drift") or []

    st.subheader("Reporting Signals")
    cols = st.columns(3)
    cols[0].metric("Recurring Costs", f"{len(recurring):,}")
    cols[1].metric("Anomalies", f"{payload.get('anomaly_count', 0):,}")
    top_category = top_categories[0]["category"] if top_categories else "None"
    # The snapshot's top_categories are LEAF categories, while the Top Categories
    # chart below rolls up to parents — the two can disagree (a leaf can lead
    # while its parent trails another family). Name the basis.
    cols[2].metric(
        "Largest Category",
        top_category,
        help=(
            "Leaf (unrolled) category with the highest spending in the period. "
            "The Top Categories chart rolls subcategories up to parents, so its "
            "largest bar can differ."
        ),
    )

    if recurring:
        recurring_df = pd.DataFrame(recurring[:6])
        recurring_df["expected_amount"] = recurring_df["expected_amount"].map(
            lambda value: f"${float(value):,.2f}"
        )
        st.dataframe(
            recurring_df.rename(
                columns={
                    "merchant": "Merchant",
                    "category": "Category",
                    "expected_amount": "Expected",
                    "frequency": "Frequency",
                    "confidence": "Confidence",
                }
            ),
            width="stretch",
            hide_index=True,
            height=250,
        )

    st.subheader("Movement")
    _render_baseline_notice(snapshot)
    move_cols = st.columns(3)
    for col, key, label in zip(
        move_cols,
        ["income", "spending", "net"],
        ["Income", "Spending", "Net"],
        strict=False,
    ):
        item = movement.get(key, {})
        # Spending is a COST: more of it is bad. st.metric's default colouring paints
        # a positive delta green, so a spending increase rendered green and a decrease
        # red. "inverse" flips that for the cost tile only.
        delta_color: Literal["normal", "inverse"] = "inverse" if key == "spending" else "normal"
        delta = (
            None
            if item.get("comparable") is False
            else f"{float(item.get('change_pct', 0)):.1f}% vs previous"
        )
        col.metric(
            label,
            format_currency(item.get("current", 0)),
            delta,
            delta_color=delta_color,
        )

    c1, c2 = st.columns(2, gap="large")
    with c1:
        st.subheader("Savings Opportunities")
        if cost_candidates:
            st.dataframe(
                pd.DataFrame(cost_candidates).rename(
                    columns={"merchant": "Merchant", "amount": "Amount", "reason": "Reason"}
                ),
                width="stretch",
                hide_index=True,
                column_config={
                    "Amount": st.column_config.NumberColumn("Amount", format=CURRENCY_FORMAT)
                },
                height=280,
            )
        else:
            st.caption("No savings candidates found in the current snapshot.")
    with c2:
        st.subheader("Category Drift")
        if category_drift:
            st.dataframe(
                pd.DataFrame(category_drift).rename(
                    columns={
                        "category": "Category",
                        "current": "Current",
                        "previous": "Previous",
                        "change": "Change",
                        # change_pct was absent from this map AND from column_config,
                        # so it rendered as a raw snake_case header with unformatted
                        # floats with excess decimal places beside $-formatted siblings.
                        "change_pct": "Change %",
                        "is_new": "New",
                        "direction": "Direction",
                    }
                ),
                width="stretch",
                hide_index=True,
                column_config={
                    "Current": st.column_config.NumberColumn("Current", format=CURRENCY_FORMAT),
                    "Previous": st.column_config.NumberColumn("Previous", format=CURRENCY_FORMAT),
                    "Change": st.column_config.NumberColumn("Change", format=CURRENCY_FORMAT),
                    "Change %": st.column_config.NumberColumn("Change %", format="%.1f%%"),
                },
                height=280,
            )
        else:
            st.caption("No category drift found in the current snapshot.")


def _render_metric_definitions() -> None:
    """Define the adjusted metrics in the UI.

    "True Cost", "offsets", and "adjusted income" appeared as bare labels and were
    defined nowhere in the app, while the Transactions page rendered raw totals under
    near-identical names -- so the same period showed two different Income and Net
    figures with nothing to reconcile them.
    """
    with st.expander("How these numbers are calculated"):
        st.markdown(
            "**Offsets** pair a refund, reimbursement, or reward with the expense it "
            "cancels, so a $140 counselling fee reimbursed later nets to $0 rather "
            "than counting as both spending and income.\n\n"
            "- **Adjusted Income** — income minus the credits that were matched to an "
            "expense as an offset (those are not new money).\n"
            "- **True Cost** — spending minus the offsets applied to it. Excludes "
            "transfers and other categories flagged as not-spending.\n"
            "- **Adjusted Net** — Adjusted Income minus True Cost.\n\n"
            "The **Transactions** page deliberately reports the RAW totals for the "
            "same period instead: every row it lists, with no offsets applied and no "
            "categories excluded. The two will differ, and both are correct for their "
            "purpose — use Overview to judge spending, Transactions to reconcile "
            "against a statement."
        )


def _health_staleness_notice(
    *,
    score_date: date | None,
    data_through: date | None,
    active_days: int | None,
    coverage_clause: str | None = None,
    scoring_anchor: date | None = None,
) -> str | None:
    """Explain a score_date that trails the newest transaction, or None if it doesn't.

    The gap has THREE causes and they need different advice. Analytics that were
    never re-run are fixed by `ledger analysis refresh`. A tail too sparse to clear
    the scoring gate is not: the refresh recomputes every day and still cannot
    produce a newer score, so the old unconditional advice sent users to a command
    that could not work, with no way to find out why. `active_days=None` means the
    count could not be established -- fall back to the refresh advice rather than
    assert sparsity.

    THE THIRD CAUSE IS THE ANCHOR ITSELF. `default_anchor_date()` is
    `min(today - 1, newest posting day)` and `compute_recent_scores` prunes
    anything past it, so a ledger holding a transaction posted TODAY has a gap of
    at least one day that no number of refreshes can close. That is checked first,
    because it is structural: when the score has already reached the anchor,
    nothing about density is being blocked and the refresh advice is futile in
    exactly the way this banner exists to stop.

    TWO BASES, NAMED SEPARATELY. `coverage_clause` is built by
    `planning.readiness._coverage_clause` and rendered here verbatim, so this
    banner and the two tile captions say the coverage fact in the SAME words
    rather than in three spellings that can drift. It states the oldest of the
    non-dormant accounts' newest imported statement months as "a statement for
    that month or later" -- never "covered through", which an account on a
    two-month cadence does not satisfy for the month in between -- and it names
    how many accounts it quantifies over. It is a different fact from the scored
    day and neither implies the other: the density collapse can start INSIDE a
    fully covered month, which is what makes a frozen score beside current
    statements look like a contradiction when only one of them is stated.

    THE COUNT BELONGS TO A WINDOW, NOT TO A DAY. `count_active_days(D)` is the
    count for the 30-day window ENDING D. Describe the window explicitly so the
    count cannot be read as a number of active days occurring on one date.

    WHAT IS NOT PROMISED. Not that the next import unfreezes the score. The count
    is over active DAYS, so a statement that adds rows on days already counted
    does not increase that count. Timely statement arrivals alone therefore do
    not establish that the scoring window has enough active days.
    """
    if score_date is None or data_through is None or score_date >= data_through:
        return None

    counted_through = scoring_anchor or data_through
    coverage = "" if not coverage_clause else f" {coverage_clause[0].upper()}{coverage_clause[1:]}."
    preamble = (
        f"This score was computed through {score_date}, but transactions run to "
        f"{data_through}.{coverage}"
    )

    if scoring_anchor is not None and score_date >= scoring_anchor:
        return (
            f"{preamble} The scorer anchors no later than {scoring_anchor} — the day before "
            "today, or the newest imported day, whichever is earlier — so this score has "
            "already reached the newest day it can be computed for, and `ledger analysis "
            "refresh` cannot close the remaining gap."
        )

    if active_days is not None and active_days < MIN_ACTIVE_DAYS:
        return (
            f"{preamble} Scoring a day needs {MIN_ACTIVE_DAYS} days carrying income or "
            f"spending inside the {SCORING_WINDOW_DAYS}-day window ending on it, and the "
            f"{SCORING_WINDOW_DAYS}-day window ending {counted_through} holds only "
            f"{active_days} — so `ledger analysis refresh` cannot move the score. Only "
            "newer imported statements can, and only if they raise that count."
        )

    return f"{preamble} Run `ledger analysis refresh` to recompute it."


def _render_health_score(health: dict, *, data_through: date, basis: dict | None = None) -> None:
    """Render the health score with its date, scale, and the risk factors behind it.

    The score used to render as a bare integer with no scale and no date, while the
    stored row also carries five sub-scores and the named risk factors that produced
    it. Showing the date matters most: scores are only rewritten when analytics are
    refreshed, so a number computed weeks ago used to be indistinguishable from a
    current one.

    ``basis`` is ``PlanningReadinessService.summary()["health_basis"]``: the two
    disclosed bases and the gate input behind them, built once per render and
    shared with the tiles below, so the caption, the banner and both tiles cannot
    disagree. ``ledger health`` is the fourth surface and reaches the identical
    sentences through ``planning.readiness.health_basis``.
    """
    basis = basis or {}
    score_date = health.get("score_date")
    trend = health.get("score_trend_7d", "stable")
    change = health.get("score_change_7d", 0)
    st.caption(
        f"Financial health score **{health['overall_score']}/100** "
        f"as of {score_date} ({trend}, {change:+} over 7d)"
    )

    notice = _health_staleness_notice(
        score_date=score_date,
        data_through=data_through,
        active_days=basis.get("active_days"),
        coverage_clause=basis.get("coverage_clause"),
        scoring_anchor=basis.get("anchor_date"),
    )
    if notice is not None:
        st.warning(notice, icon="⚠️")

    with st.expander("What drives this score"):
        components = [
            ("Cashflow health", health.get("cashflow_health"), "30%"),
            ("Savings rate", health.get("savings_rate"), "25%"),
            ("Emergency fund", health.get("emergency_fund_months"), "20%"),
            ("Debt ratio", health.get("debt_ratio"), "15%"),
            ("Spending consistency", health.get("spending_consistency"), "10%"),
        ]
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Component": label,
                        "Value": "Not assessable" if value is None else f"{value}",
                        "Weight": weight,
                    }
                    for label, value, weight in components
                ]
            ),
            width="stretch",
            hide_index=True,
        )
        st.caption(
            "'Not assessable' means the inputs are unavailable — emergency fund and "
            "debt ratio need a statement-backed (absolute) balance for every active "
            "account. Unassessable components are dropped and the remaining weights "
            "are renormalized rather than scored as zero."
        )
        st.caption(
            f"A day is only scored when the {SCORING_WINDOW_DAYS}-day window ending on "
            f"it — that day included — holds at least {MIN_ACTIVE_DAYS} days carrying "
            "income or spending, so the newest score can sit behind the newest "
            "transaction when recent activity is sparse. Recomputing cannot move it; "
            "only newer imported statements can, and only when they raise that count."
        )
        risk_factors = health.get("risk_factors") or []
        if risk_factors:
            st.markdown("**Risk factors**")
            for factor in risk_factors:
                st.markdown(f"- {factor}")


def _render_planning_status(ctx: AppContext) -> None:
    budget_rows = BudgetService(ctx.db_path, prepare_schema=False).budget_variance(
        ctx.start_date, ctx.end_date
    )
    over_budget = [row for row in budget_rows if row["status"] == "over"]
    engine = InsightEngine(ctx.db_path)
    findings = engine.list_findings(ctx.start_date, ctx.end_date)
    served = engine.served_period(ctx.start_date, ctx.end_date)
    high_findings = [row for row in findings if row["severity"] in {"critical", "high"}]
    if not budget_rows and not findings:
        return

    st.subheader("Needs Attention")
    cols = st.columns(3)
    cols[0].metric("Open Findings", f"{len(findings):,}")
    cols[1].metric("High Priority", f"{len(high_findings):,}")
    cols[2].metric("Over Budget", f"{len(over_budget):,}")
    if served is not None and served != (ctx.start_date, ctx.end_date):
        st.caption(
            f"Findings come from the newest stored analysis ({served[0]} to {served[1]}); "
            "no stored analysis matches the selected period exactly."
        )
    st.caption(
        "Over Budget compares plans with gross spending on the ledger.aggregate basis; "
        "Overview true cost additionally applies income treatments."
    )

    if findings:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Severity": row["severity"],
                        "Type": row["type"],
                        "Finding": row["title"],
                        "Action": row["suggested_action"],
                    }
                    for row in findings[:8]
                ]
            ),
            width="stretch",
            hide_index=True,
            height=260,
        )


def _goal_remaining_detail(remaining: Decimal | None) -> str:
    return format_currency(remaining) if remaining is not None else "Needs absolute value"


def _health_tile_detail(health: dict, basis: dict) -> str | None:
    """The score's own as-of fact first, then what it says about the buffer.

    This tile used to show only the emergency-fund months, which made a
    weeks-old score read as a current one -- the Overview caption above carried
    the staleness and this tile did not. The as-of fact now comes from the same
    `health_basis` block the caption uses, so the two cannot drift.
    """
    if not health:
        return None
    # emergency_fund_months is None when no explicit absolute row exists.
    buffer_detail = (
        f"{float(health['emergency_fund_months']):.1f} months emergency"
        if health.get("emergency_fund_months") is not None
        else "Emergency fund N/A (needs an absolute statement value)"
    )
    score_detail = basis.get("score_detail")
    return buffer_detail if not score_detail else f"{score_detail} · {buffer_detail}"


def _render_planning_readiness(summary: dict) -> None:
    health = summary["health"] or {}
    basis = summary.get("health_basis") or {}

    st.subheader("Planning Readiness")
    status_grid(
        [
            {
                "label": "Budgets",
                "value": f"{summary['budgets']['active']:,}",
                "detail": f"{summary['budgets']['over_budget']:,} over",
            },
            {
                "label": "Active Goals",
                "value": f"{summary['goals']['active']:,}",
                "detail": _goal_remaining_detail(summary["goals"]["remaining"]),
            },
            {
                "label": "Recurring Monthly",
                "value": format_currency(summary["recurring_monthly"]),
                "detail": None,
            },
            {
                "label": "Health Score",
                "value": str(health.get("overall_score", "n/a")),
                "detail": _health_tile_detail(health, basis),
            },
        ]
    )
    if basis.get("statement"):
        st.caption(basis["statement"])

    next_actions = summary["next_actions"]
    if next_actions:
        st.caption("Readiness gaps")
        st.dataframe(
            pd.DataFrame({"Next Action": next_actions}),
            width="stretch",
            hide_index=True,
            height=min(180, 48 + len(next_actions) * 35),
        )
    else:
        st.caption("Planning data is current for this selected period.")


def _evidence_status_items(evidence: dict) -> list[dict[str, str | None]]:
    return [
        {
            "label": "Audit",
            "value": str((evidence.get("audit") or {}).get("status", "unknown")),
            "detail": (evidence.get("audit") or {}).get("detail"),
        },
        {
            "label": "Review",
            "value": str((evidence.get("review") or {}).get("status", "unknown")),
            "detail": (evidence.get("review") or {}).get("detail"),
        },
        {
            "label": "Quality",
            "value": str((evidence.get("quality") or {}).get("status", "unknown")),
            "detail": (evidence.get("quality") or {}).get("detail"),
        },
        {
            "label": "Snapshot",
            "value": str((evidence.get("snapshots") or {}).get("status", "unknown")),
            "detail": (evidence.get("snapshots") or {}).get("detail"),
        },
    ]


def _render_evidence_status(ctx: AppContext) -> None:
    evidence = EvidenceService(ctx.db_path).summary(pending_reviews=ctx.pending_reviews)
    st.subheader("Evidence")
    status_grid(_evidence_status_items(evidence))


def render(ctx: AppContext) -> None:
    page_header(
        "Overview",
        "A compact status view for the selected period.",
        status=ctx.period_label,
    )

    db_path_str = str(ctx.db_path)
    service = ctx.service
    with st.spinner("Loading overview…"):
        snapshot = ReportSnapshotService(ctx.db_path).snapshot_for_period(
            ctx.start_date,
            ctx.end_date,
            source="streamlit_overview",
            persist=False,
        )
        kpis = _cached_true_cost_kpis(db_path_str, ctx.start_date, ctx.end_date)
        financial_brief = _cached_financial_brief(
            db_path_str,
            ctx.start_date,
            ctx.end_date,
        )
        latest_health = service.get_latest_health_score()
        # Built ONCE and shared by the caption, the warning, and the tile below.
        # Three surfaces render this score and only the caption used to explain
        # it; computing the basis per surface would have let them drift again.
        readiness = PlanningReadinessService(ctx.db_path, prepare_schema=False).summary(
            ctx.start_date,
            ctx.end_date,
            pending_reviews=ctx.pending_reviews,
        )
    expenses = kpis["expenses"]
    income = kpis["income"]
    net = kpis["net"]
    savings_rate = (net / income * 100) if income else Decimal("0")

    kpi_row(
        [
            ("Adjusted Income", format_currency(income), "net of offsets", "pos"),
            (
                "True Cost",
                format_currency(expenses),
                f"{format_currency(kpis['offsets'])} offsets",
                "neg",
            ),
            (
                "Adjusted Net",
                format_currency(net),
                f"{float(savings_rate):.1f}% savings rate",
                "pos" if net >= 0 else "neg",
            ),
            ("Transactions", f"{kpis['transaction_count']:,}", None),
        ]
    )
    _render_metric_definitions()
    render_metric_explanations(
        ctx.db_path,
        ctx.start_date,
        ctx.end_date,
        {
            "Adjusted Income": {"metric": "income"},
            "Gross Spending": {"metric": "gross_spending"},
            "True Cost": {"metric": "true_cost"},
            "Adjusted Net": {"metric": "net"},
            "Offsets": {"metric": "offsets"},
        },
        key="overview",
    )
    period_status_strip(
        start_date=ctx.start_date,
        end_date=ctx.end_date,
        row_count=int(kpis["transaction_count"]),
        snapshot=snapshot,
        source="Snapshot + live queries",
    )
    render_financial_brief(financial_brief)

    if latest_health:
        _render_health_score(
            latest_health,
            data_through=ctx.latest,
            basis=readiness["health_basis"],
        )

    _render_status_strip(ctx, snapshot)
    _render_evidence_status(ctx)
    _render_planning_readiness(readiness)
    _render_planning_status(ctx)

    st.divider()

    spending = _cached_true_cost_by_category(db_path_str, ctx.start_date, ctx.end_date)
    hierarchy = build_hierarchical_spending(spending, ctx.category_config)
    parent_totals = sorted(
        hierarchy["parent_totals"].items(), key=lambda item: item[1], reverse=True
    )
    merchants = _cached_spending_by_merchant(db_path_str, ctx.start_date, ctx.end_date, top_n=10)
    income_sources = _cached_income_sources(db_path_str, ctx.start_date, ctx.end_date, top_n=8)

    col1, col2 = st.columns(2, gap="large")
    with col1:
        st.subheader("Top Categories")
        if parent_totals:
            labels = [item[0] for item in parent_totals[:8]]
            values = [float(item[1]) for item in parent_totals[:8]]
            st.plotly_chart(bar_chart(labels, values, orientation="h"), width="stretch")
        else:
            st.info("No spending found for this period.")

    with col2:
        st.subheader("Top Merchants")
        if merchants:
            labels = list(merchants.keys())[:10]
            values = [float(value) for value in merchants.values()][:10]
            st.plotly_chart(bar_chart(labels, values, orientation="h"), width="stretch")
        else:
            st.info("No merchant spending found for this period.")

    col3, col4 = st.columns(2, gap="large")
    with col3:
        st.subheader("Cashflow Trend")
        with st.spinner("Building cashflow trend…"):
            cashflow = _monthly_cashflow(db_path_str, ctx.start_date, ctx.end_date)
        if not cashflow.empty:
            st.plotly_chart(_cashflow_chart(cashflow), width="stretch")
        else:
            st.info("Not enough monthly cashflow data for the selected range.")

    with col4:
        st.subheader("Income Sources")
        if income_sources:
            income_df = pd.DataFrame(
                {
                    "Source": list(income_sources.keys()),
                    "Amount": [format_currency_precise(value) for value in income_sources.values()],
                }
            )
            st.dataframe(income_df, width="stretch", hide_index=True, height=260)
        else:
            st.info("No income transactions found for this period.")

        # Reuse anomalies already computed inside snapshot_for_period/_compute_payload
        # (payload['anomalies'] = list of dicts with keys: txn_id, date, merchant,
        # category, amount).  This avoids a redundant second detect_anomalies call.
        snapshot_payload = snapshot.get("payload") or {}
        anomalies = list(snapshot_payload.get("anomalies") or [])
        st.subheader("Unusual Activity")
        if anomalies:
            anomaly_df = pd.DataFrame(
                [
                    {
                        "Date": date.fromisoformat(item["date"]),
                        "Merchant": item.get("merchant") or "-",
                        "Category": item.get("category"),
                        "Amount": float(item["amount"]),
                    }
                    for item in anomalies[:8]
                ]
            )
            st.dataframe(
                anomaly_df,
                width="stretch",
                hide_index=True,
                column_config={
                    "Date": st.column_config.DateColumn("Date", format="YYYY-MM-DD"),
                    "Amount": st.column_config.NumberColumn("Amount", format=CURRENCY_FORMAT),
                },
                height=260,
            )
        else:
            st.caption("No unusual transactions detected for this period.")

    _render_reporting_signals(snapshot)
