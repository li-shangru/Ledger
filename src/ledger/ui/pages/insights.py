"""Insights, recurring patterns, forecast, and ratios."""

from __future__ import annotations

import math
import statistics
from datetime import date
from decimal import Decimal

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy.exc import SQLAlchemyError

from ledger.analysis.pattern_detector import (
    RECURRING_COST_TYPES,
    monthly_equivalent,
    next_occurrence,
)
from ledger.insights import InsightEngine
from ledger.insights.preferences import (
    PREFERENCE_DISPOSITIONS,
    PREFERENCE_NOTE_MAX_LENGTH,
)
from ledger.insights.service import FindingActionTarget, StaleFindingError
from ledger.reporting import ReportSnapshotService
from ledger.storage.snapshots import DatabaseTargetLockError
from ledger.ui.components import (
    CURRENCY_FORMAT,
    format_currency,
    format_currency_precise,
    kpi_row,
    line_chart,
    page_header,
    period_status_strip,
    render_transaction_table,
    style_fig,
)
from ledger.ui.context import AppContext
from ledger.ui.navigation import supporting_findings, take_brief_context
from ledger.utils.dates import (
    month_end as _month_end,
)
from ledger.utils.dates import (
    next_month_start as _next_month_start,
)
from ledger.utils.dates import (
    year_month,
)

PRIORITY_REVIEW_FINDING_TYPES = (
    "possible_duplicates",
    "recurring_price_increase",
    "unresolved_reimbursements",
)


def _available_months(ctx: AppContext) -> list[str]:
    months = []
    current = ctx.start_date.replace(day=1)
    end = ctx.end_date.replace(day=1)
    while current <= end:
        months.append(year_month(current))
        current = _next_month_start(current)
    return months


def _whole_data_months(ctx: AppContext, months_back: int) -> list[str]:
    """The trailing ``months_back`` WHOLE calendar months of imported data.

    Derived from data availability (``ctx.earliest``..``ctx.latest``), never from
    the selected view period: the Ratios tiles and the Forecast advertise a
    12-whole-month basis, and deriving the list from the view collapsed both to
    the default single partial month — Forecast then claimed there was "not
    enough historical data" against a multi-year database. A month qualifies only
    when the data range covers its first and last day, so the in-progress partial
    month never dilutes an average that is captioned "whole calendar months"."""
    months: list[str] = []
    current = ctx.earliest.replace(day=1)
    last_month_start = ctx.latest.replace(day=1)
    while current <= last_month_start:
        month_end = _month_end(current)
        if current >= ctx.earliest and month_end <= ctx.latest:
            months.append(year_month(current))
        current = _next_month_start(current)
    return months[-months_back:]


def _monthly_cashflow(ctx: AppContext, months_back: int = 12) -> list[dict]:
    months = _whole_data_months(ctx, months_back)
    rows = []
    with st.spinner("Calculating monthly cashflow…"):
        for month in months:
            start = date.fromisoformat(f"{month}-01")
            end = _month_end(start)
            kpis = ctx.service.compute_true_cost_kpis(start, end)
            rows.append(
                {
                    "month": month,
                    "income": float(kpis["income"]),
                    "expenses": float(kpis["expenses"]),
                    "net": float(kpis["net"]),
                }
            )
    return rows


def _current_positive_balance(ctx: AppContext) -> Decimal | None:
    """Sum of the newest positive statement-anchored balances across accounts.

    Deliberately NOT gated on ``is_active``: that flag decays against wall-clock
    at every import finalize, so importing one institution's statement silently
    re-ages every other account still waiting on its own — and partial statement
    arrival is this system's documented normal state. Money must not vanish from
    the forecast because a different account's statement arrived first; the
    anchor's own date already carries the staleness."""
    total = Decimal("0")
    accounts = ctx.service.get_accounts()
    if not accounts:
        return None
    anchored = 0
    for account in accounts:
        # Only the latest balance is needed; fetch just that row instead of the whole
        # per-day series for every account.
        row = ctx.service.get_latest_account_balance(
            account["account_id"],
            end_date=ctx.end_date,
            balance_basis="absolute",
        )
        if not row:
            # An account with no statement anchor contributes no level — it is
            # unknown, not zero, and must not veto the accounts that do anchor.
            continue
        anchored += 1
        balance = Decimal(str(row["balance"]))
        if balance > 0:
            total += balance
    if anchored == 0:
        # No statement-backed level anywhere: the forecast has no starting value,
        # which is the message the caller renders. Relative history cannot fill in.
        return None
    return total


def _patterns_for_period(ctx: AppContext, pattern_type: str | None = None) -> list[dict]:
    patterns = ctx.service.get_recurring_patterns(
        pattern_type=pattern_type,
        is_active=True,
        limit=200,
    )
    # first_occurrence alone gates the window: every pattern here is is_active, so
    # the detector says the commitment is live now, and requiring last_occurrence
    # to fall inside the window dropped every active bill that had not recharged
    # yet in a young period (the default mid-month view lost the mortgage).
    return [
        pattern
        for pattern in patterns
        if (not pattern["first_occurrence"] or pattern["first_occurrence"] <= ctx.end_date)
    ]


def _upcoming_expected(patterns: list[dict], data_edge: date) -> list[dict]:
    """Expected occurrences strictly after the newest imported day, soonest first.

    The old filter kept ``next_expected_date`` INSIDE the selected period — but
    every reachable period is clamped to the newest transaction, so a genuinely
    upcoming charge (one expected after the data edge) was structurally
    unreachable, and what rendered instead was already-elapsed expectations.
    Stored next-expected dates are retrospective (one period after the last
    accepted occurrence), so each is rolled forward to its first date past the
    edge with the same cadence arithmetic the detector uses."""
    rows: list[dict] = []
    for p in patterns:
        expected = p.get("next_expected_date")
        if not expected:
            continue
        expected_day = int(p.get("expected_day_of_period") or expected.day)
        steps = 0
        while expected <= data_edge and steps < 400:
            advanced = next_occurrence(expected, p["frequency"], expected_day)
            if advanced <= expected:
                break
            expected = advanced
            steps += 1
        if expected <= data_edge:
            continue
        rows.append({**p, "expected_on": expected})
    return sorted(rows, key=lambda p: p["expected_on"])


def _render_patterns(ctx: AppContext) -> None:
    pattern_filter = st.segmented_control(
        "Pattern type",
        ["All", "Subscriptions", "Bills", "Income", "Transfers"],
        default="All",
    )
    pattern_type_map = {
        "Subscriptions": "subscription",
        "Bills": "bill",
        "Income": "income",
        "Transfers": "transfer",
    }
    patterns = _patterns_for_period(ctx, pattern_type_map.get(pattern_filter or ""))
    if not patterns:
        st.info("No active recurring patterns detected for this period.")
        return

    # Costs only, and normalized to a monthly equivalent. This tile is rendered with
    # the "neg" (cost) style but had NO pattern-type filter, so income patterns were
    # summed in as though they were expenses; and filtering to frequency == "monthly"
    # silently dropped every weekly/biweekly/quarterly/annual obligation.
    monthly = sum(
        (
            monthly_equivalent(p["expected_amount"], p["frequency"])
            for p in patterns
            if p["pattern_type"] in RECURRING_COST_TYPES
        ),
        Decimal("0"),
    )
    subscriptions = sum(1 for p in patterns if p["pattern_type"] == "subscription")
    bills = sum(1 for p in patterns if p["pattern_type"] == "bill")
    avg_confidence = sum(p["confidence"] for p in patterns) / len(patterns)
    kpi_row(
        [
            ("Recurring Costs / mo", format_currency(monthly), "subscriptions + bills", "neg"),
            ("Patterns", f"{len(patterns):,}", None),
            ("Subscriptions", f"{subscriptions:,}", f"{bills:,} bills"),
            ("Avg Confidence", f"{avg_confidence:.0%}", None),
        ]
    )

    df = pd.DataFrame(
        [
            {
                "Merchant": p["merchant"],
                "Category": p["category"],
                "Type": p["pattern_type"].title(),
                "Frequency": p["frequency"].title(),
                "Amount": float(p["expected_amount"]),
                "Confidence": float(p["confidence"]) * 100,
                "Last Seen": p["last_occurrence"],
                "Next Expected": p["next_expected_date"],
                "Count": p["occurrence_count"],
                "Missed": p["missed_count"],
            }
            for p in patterns
        ]
    )
    st.dataframe(
        df,
        width="stretch",
        hide_index=True,
        column_config={
            "Amount": st.column_config.NumberColumn("Amount", format=CURRENCY_FORMAT),
            "Confidence": st.column_config.NumberColumn("Confidence", format="%.0f%%"),
            "Last Seen": st.column_config.DateColumn("Last Seen", format="YYYY-MM-DD"),
            "Next Expected": st.column_config.DateColumn("Next Expected", format="YYYY-MM-DD"),
        },
        height=520,
    )

    upcoming = _upcoming_expected(patterns, ctx.latest)[:12]
    if upcoming:
        st.subheader("Upcoming")
        st.caption(
            f"Expected occurrences after the newest imported day ({ctx.latest}). Stored "
            "next-expected dates are retrospective — one period after the last accepted "
            "charge — so each is rolled forward to its first date past the data edge."
        )
        upcoming_df = pd.DataFrame(
            [
                {
                    "Date": p["expected_on"],
                    "Merchant": p["merchant"],
                    "Amount": float(p["expected_amount"]),
                    "Type": p["pattern_type"].title(),
                }
                for p in upcoming
            ]
        )
        st.dataframe(
            upcoming_df,
            width="stretch",
            hide_index=True,
            column_config={
                "Date": st.column_config.DateColumn("Date", format="YYYY-MM-DD"),
                "Amount": st.column_config.NumberColumn("Amount", format=CURRENCY_FORMAT),
            },
            height=300,
        )


def _forecast_series(
    current_balance: float,
    avg_net: float,
    net_std: float,
    start: date,
    horizon: int,
    k: float = 1.0,
) -> list[dict]:
    """Project monthly balances from the recent average net cashflow.

    ``Base`` walks the balance forward by ``avg_net`` each month. Optimistic / Pessimistic
    widen as ``k * net_std * sqrt(month)`` -- a random-walk cone reflecting the user's
    ACTUAL month-to-month net volatility. The previous fixed +/-20%-of-avg-net band was
    disconnected from real uncertainty (typically ~7-26x too narrow) and collapsed to
    nothing when ``avg_net`` was ~0; Optimistic stays the better-off line for either sign.
    """
    rows: list[dict] = []
    balance = current_balance
    current = start
    for idx in range(horizon + 1):
        if idx > 0:
            balance += avg_net
        spread = k * net_std * math.sqrt(idx)
        rows.append(
            {
                "Month": year_month(current),
                "Base": balance,
                "Optimistic": balance + spread,
                "Pessimistic": balance - spread,
            }
        )
        current = _next_month_start(current)
    return rows


def _render_forecast(ctx: AppContext) -> None:
    horizon = st.selectbox(
        "Forecast horizon", [3, 6, 12, 18, 24], index=1, format_func=lambda x: f"{x} months"
    )
    rows = _monthly_cashflow(ctx, months_back=12)
    if len(rows) < 2:
        st.warning("Need at least two months of historical data for a forecast.")
        return

    # _monthly_cashflow serves whole calendar months only (the in-progress partial
    # month is already excluded), so every row belongs in the averages/volatility.
    stat_rows = rows
    avg_income = sum(row["income"] for row in stat_rows) / len(stat_rows)
    avg_expenses = sum(row["expenses"] for row in stat_rows) / len(stat_rows)
    avg_net = avg_income - avg_expenses
    nets = [row["net"] for row in stat_rows]
    net_std = statistics.stdev(nets) if len(nets) > 1 else 0.0
    absolute_balance = _current_positive_balance(ctx)
    if absolute_balance is None:
        st.warning(
            "Forecast needs an absolute statement-backed account balance; "
            "relative change history cannot establish a starting value."
        )
        return
    current_balance = float(absolute_balance)

    kpi_row(
        [
            ("Current Positive Balance", format_currency(current_balance), None, "pos"),
            ("Avg Income", format_currency(avg_income), None, "pos"),
            ("Avg Expenses", format_currency(avg_expenses), None, "neg"),
            ("Avg Net", format_currency(avg_net), None, "pos" if avg_net >= 0 else "neg"),
        ]
    )

    # Project forward from the newest imported month, not the view period's end:
    # the averages above are the trailing whole months of DATA, and forecasting a
    # historical view period from current averages would pin the wrong start.
    forecast_rows = _forecast_series(
        current_balance,
        avg_net,
        net_std,
        _next_month_start(ctx.latest.replace(day=1)),
        horizon,
    )

    df = pd.DataFrame(forecast_rows)
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=df["Month"], y=df["Optimistic"], name="Optimistic", mode="lines"))
    fig.add_trace(go.Scatter(x=df["Month"], y=df["Base"], name="Base", mode="lines+markers"))
    fig.add_trace(go.Scatter(x=df["Month"], y=df["Pessimistic"], name="Pessimistic", mode="lines"))
    fig.update_layout(
        height=430,
        margin={"l": 12, "r": 12, "t": 20, "b": 28},
        hovermode="x unified",
        legend={"orientation": "h", "y": 1.08},
        xaxis_title="",
        yaxis_title="",
    )
    st.plotly_chart(style_fig(fig), width="stretch")

    if avg_net < 0:
        burn = abs(avg_net)
        runway = int(current_balance / burn) if burn else 999
        st.warning(f"Runway is about {runway} months at the current average burn rate.")
    else:
        st.success(f"Positive average cashflow: {format_currency_precise(avg_net)} per month.")


def _render_monthly_comparison(ctx: AppContext) -> None:
    months = _available_months(ctx)
    if not months:
        st.warning("No monthly data available.")
        return

    selected = st.multiselect(
        "Months",
        options=months[-12:],
        default=months[-3:] if len(months) >= 3 else months,
        max_selections=6,
    )
    if not selected:
        st.info("Select at least one month.")
        return

    month_rows = []
    category_rows = []
    for month in selected:
        start = date.fromisoformat(f"{month}-01")
        end = _month_end(start)
        kpis = ctx.service.compute_true_cost_kpis(start, end)
        spending = ctx.service.get_true_cost_by_category(start, end)
        month_rows.append(
            {
                "Month": month,
                "Income": float(kpis["income"]),
                "Expenses": float(kpis["expenses"]),
                "Net": float(kpis["net"]),
                "Transactions": kpis["transaction_count"],
            }
        )
        for category, amount in spending.items():
            category_rows.append({"Month": month, "Category": category, "Amount": float(amount)})

    st.dataframe(
        pd.DataFrame(month_rows),
        width="stretch",
        hide_index=True,
        column_config={
            "Income": st.column_config.NumberColumn("Income", format=CURRENCY_FORMAT),
            "Expenses": st.column_config.NumberColumn("Expenses", format=CURRENCY_FORMAT),
            "Net": st.column_config.NumberColumn("Net", format=CURRENCY_FORMAT),
        },
        height=240,
    )

    if category_rows:
        fig = px.bar(
            pd.DataFrame(category_rows),
            x="Category",
            y="Amount",
            color="Month",
            barmode="group",
            height=520,
        )
        fig.update_layout(margin={"l": 12, "r": 12, "t": 20, "b": 28})
        st.plotly_chart(style_fig(fig), width="stretch")


def _render_anomalies(ctx: AppContext) -> None:
    anomalies = ctx.service.detect_anomalies(ctx.start_date, ctx.end_date, threshold=2.0)
    render_transaction_table(anomalies, detailed=True, height=360)


def _safe_ratio_pct(numerator: float, denominator: float) -> float | None:
    """A percentage ratio, or ``None`` when its denominator is zero.

    A ratio with no denominator is unassessable, not zero: the old ``else 0``
    fallback rendered "Period Spend / Income 0.0%" — the best possible reading —
    for a period with real spending and no income at all."""
    if not denominator:
        return None
    return numerator / denominator * 100


def _render_ratios(ctx: AppContext) -> None:
    rows = _monthly_cashflow(ctx, months_back=12)
    if not rows:
        st.info("No monthly cashflow data available.")
        return

    avg_income = sum(row["income"] for row in rows) / len(rows)
    avg_expenses = sum(row["expenses"] for row in rows) / len(rows)
    avg_net = avg_income - avg_expenses
    savings_rate = _safe_ratio_pct(avg_net, avg_income)
    recurring = _patterns_for_period(ctx)
    fixed_monthly = sum(
        (
            monthly_equivalent(p["expected_amount"], p["frequency"])
            for p in recurring
            if p["pattern_type"] in RECURRING_COST_TYPES
        ),
        Decimal("0"),
    )
    fixed_ratio = _safe_ratio_pct(float(fixed_monthly), avg_expenses)
    current_kpis = ctx.service.compute_true_cost_kpis(ctx.start_date, ctx.end_date)
    period_ratio = _safe_ratio_pct(float(current_kpis["expenses"]), float(current_kpis["income"]))

    kpi_row(
        [
            (
                "Savings Rate",
                f"{savings_rate:.1f}%" if savings_rate is not None else "n/a",
                "avg of last 12 whole months"
                if savings_rate is not None
                else "no income in the 12-month basis",
                "pos" if (savings_rate or 0) >= 0 else "neg",
            ),
            (
                "Fixed Expense Ratio",
                f"{fixed_ratio:.1f}%" if fixed_ratio is not None else "n/a",
                "of avg monthly spend"
                if fixed_ratio is not None
                else "no spending in the 12-month basis",
            ),
            (
                "Period Spend / Income",
                f"{period_ratio:.1f}%" if period_ratio is not None else "n/a",
                ctx.period_label
                if period_ratio is not None
                else f"no income in {ctx.period_label}",
            ),
            (
                "Recurring Costs / mo",
                format_currency(fixed_monthly),
                "subscriptions + bills",
                "neg",
            ),
        ]
    )
    st.caption(
        "Savings Rate and Fixed Expense Ratio are averaged over the last 12 whole "
        f"calendar months, not {ctx.period_label} — only Period Spend / Income uses "
        "the selected period."
    )

    df = pd.DataFrame(rows)
    st.plotly_chart(
        line_chart(
            df["month"].tolist(), df["net"].tolist(), name="Net cashflow", title="Net Cashflow"
        ),
        width="stretch",
    )

    ratio_df = pd.DataFrame(
        [
            {"Metric": "Average monthly income", "Value": format_currency_precise(avg_income)},
            {"Metric": "Average monthly expenses", "Value": format_currency_precise(avg_expenses)},
            {"Metric": "Average monthly net", "Value": format_currency_precise(avg_net)},
            {
                "Metric": "Estimated fixed recurring",
                "Value": format_currency_precise(fixed_monthly),
            },
        ]
    )
    st.dataframe(ratio_df, width="stretch", hide_index=True, height=220)


def _snapshot_payload(ctx: AppContext) -> dict:
    # Lazy-fallback: called from widget helpers that need payload data without
    # re-using the snapshot fetched in render().  Keep both calls separate —
    # consolidating them into one shared result would couple render() ordering
    # to widget call order and break the lazy-fallback pattern.
    snapshot = ReportSnapshotService(ctx.db_path).snapshot_for_period(
        ctx.start_date,
        ctx.end_date,
        source="streamlit_insights",
        persist=False,
    )
    return snapshot.get("payload") or {}


def _ensure_findings(ctx: AppContext) -> InsightEngine:
    return InsightEngine(ctx.db_path)


def _update_displayed_finding(
    engine: InsightEngine, finding_id: int, status: str, expected: FindingActionTarget
) -> None:
    try:
        engine.update_status(finding_id, status, expected=expected)
    except StaleFindingError as exc:
        st.session_state["insight_action_notice"] = str(exc)


def _render_finding_actions(engine: InsightEngine, finding: dict) -> None:
    cols = st.columns([0.18, 0.18, 0.22, 0.42])
    finding_id = int(finding["id"])
    target = finding.get("_action_target")
    bound = isinstance(target, FindingActionTarget)
    # Callbacks run before the next script render consumes the one-shot brief
    # context. Handling button return values would lose these actions when that
    # render returns to the ordinary Insights view.
    cols[0].button(
        "Watch",
        key=f"finding_watch_{finding_id}",
        on_click=_update_displayed_finding,
        args=(engine, finding_id, "watch", target),
        disabled=not bound,
    )
    cols[1].button(
        "Ignore",
        key=f"finding_ignore_{finding_id}",
        on_click=_update_displayed_finding,
        args=(engine, finding_id, "ignored", target),
        disabled=not bound,
    )
    cols[2].button(
        "Mark reviewed",
        key=f"finding_reviewed_{finding_id}",
        on_click=_update_displayed_finding,
        args=(engine, finding_id, "reviewed", target),
        disabled=not bound,
    )
    cols[3].caption(f"Status: {finding['status']}")


def _render_findings(
    ctx: AppContext,
    engine: InsightEngine,
    finding_type: str | tuple[str, ...],
    empty: str,
) -> None:
    finding_types = (finding_type,) if isinstance(finding_type, str) else finding_type
    key = "_".join(finding_types)
    show_closed = st.toggle("Show ignored/reviewed", value=False, key=f"{key}_closed")
    findings = _findings_for_types(
        engine,
        start_date=ctx.start_date,
        end_date=ctx.end_date,
        finding_types=finding_types,
        include_closed=show_closed,
    )
    if not findings:
        st.info(empty)
        return
    _render_finding_rows(engine, findings)


def _render_finding_rows(
    engine: InsightEngine, findings: list[dict], *, expanded: bool = False
) -> None:
    kpi_row(
        [
            ("Findings", f"{len(findings):,}", None),
            (
                "High Severity",
                f"{sum(1 for row in findings if row['severity'] in {'critical', 'high'}):,}",
                None,
            ),
            ("Watching", f"{sum(1 for row in findings if row['status'] == 'watch'):,}", None),
        ]
    )
    for finding in findings:
        with st.expander(
            f"{finding['severity'].title()} - {finding['title']}",
            expanded=expanded
            or (
                finding["severity"] in {"critical", "high"}
                and finding.get("action_priority", "normal") == "normal"
            ),
        ):
            st.write(finding["explanation"])
            preference = finding.get("preference_context")
            if preference:
                label = (
                    f"{str(preference['disposition']).title()} "
                    f"{preference['scope']}: {preference['name']}"
                )
                note = str(preference.get("note") or "")
                st.caption(f"Spending preference: {label}" + (f" — {note}" if note else ""))
                if preference.get("effect") == "material_evidence_retained":
                    st.info(
                        "The preference changes recommendation context only. This new evidence "
                        "still needs review and remains a detected possibility."
                    )
            effective_action = finding.get(
                "effective_suggested_action", finding["suggested_action"]
            )
            if effective_action:
                st.caption(f"Suggested action: {effective_action}")
            else:
                st.caption(
                    "Suggested action suppressed: spend size alone does not override this "
                    "recorded preference."
                )
            evidence = finding.get("evidence") or {}
            if evidence:
                st.dataframe(
                    pd.DataFrame([evidence]),
                    width="stretch",
                    hide_index=True,
                    height=90,
                )
            _render_finding_actions(engine, finding)


def _findings_for_types(
    engine: InsightEngine,
    *,
    start_date: date,
    end_date: date,
    finding_types: tuple[str, ...],
    include_closed: bool,
) -> list[dict]:
    """Collect one review queue without hiding a supported material finding type."""
    findings = [
        finding
        for finding_type in finding_types
        for finding in engine.list_findings(
            start_date,
            end_date,
            finding_type=finding_type,
            include_closed=include_closed,
            include_action_targets=True,
        )
    ]
    priority_rank = {"normal": 0, "low": 1, "suppressed": 2}
    severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    return sorted(
        findings,
        key=lambda row: (
            priority_rank.get(str(row.get("action_priority", "normal")), 99),
            severity_rank.get(str(row.get("severity", "low")), 99),
            finding_types.index(str(row.get("type"))),
            int(row.get("id", 0)),
        ),
    )


def _render_preferences(engine: InsightEngine) -> None:
    service = engine.preferences
    st.subheader("Spending Preferences")
    st.caption(
        "Record an intentional, necessary, or worthwhile merchant/category. Preferences change "
        "recommendation priority only; transaction facts and finding status stay unchanged."
    )

    with st.form("new_spending_preference", clear_on_submit=True):
        scope_col, disposition_col = st.columns(2)
        scope = scope_col.selectbox("Scope", ["merchant", "category"])
        disposition = disposition_col.selectbox("Disposition", sorted(PREFERENCE_DISPOSITIONS))
        name = st.text_input(
            "Exact merchant or category name",
            help="Matching ignores case and repeated whitespace, but does not use substrings.",
        )
        note = st.text_area(
            "Note",
            max_chars=PREFERENCE_NOTE_MAX_LENGTH,
            help="Optional context kept with this local preference.",
        )
        create = st.form_submit_button("Add or update preference", type="primary")
    if create:
        try:
            service.upsert(scope, name, disposition, note=note)
        except (ValueError, DatabaseTargetLockError, OSError, SQLAlchemyError) as exc:
            st.error(f"Preference could not be saved: {exc}")
        else:
            st.success("Preference saved.")
            st.rerun()

    rows = service.list_preferences()
    if not rows:
        st.info("No spending preferences are recorded yet.")
        return

    st.caption(
        "Save edits on one row at a time. Removing a preference restores ordinary recommendation "
        "ranking and can be reversed by adding it again."
    )
    dispositions = sorted(PREFERENCE_DISPOSITIONS)
    for row in rows:
        with st.expander(f"{row['scope'].title()} — {row['name']}"):
            with st.form(f"spending_preference_{row['id']}"):
                disposition = st.selectbox(
                    "Disposition",
                    dispositions,
                    index=dispositions.index(row["disposition"]),
                    key=f"preference_disposition_{row['id']}",
                )
                note = st.text_area(
                    "Note",
                    value=row.get("note", ""),
                    max_chars=PREFERENCE_NOTE_MAX_LENGTH,
                    key=f"preference_note_{row['id']}",
                )
                save_col, remove_col = st.columns(2)
                save = save_col.form_submit_button("Save changes", type="primary")
                remove = remove_col.form_submit_button("Remove preference")
            if save or remove:
                try:
                    if save:
                        service.upsert(
                            row["scope"],
                            row["name"],
                            disposition,
                            note=note,
                        )
                    else:
                        service.delete(row["scope"], row["name"])
                except (ValueError, DatabaseTargetLockError, OSError, SQLAlchemyError) as exc:
                    st.error(f"Preference could not be changed: {exc}")
                else:
                    st.success("Preference saved." if save else "Preference removed.")
                    st.rerun()


def _render_snapshot_rows(rows: list[dict], empty: str) -> None:
    if not rows:
        st.info(empty)
        return
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True, height=520)


def render(ctx: AppContext) -> None:
    page_header(
        "Insights",
        "Recurring patterns, forecast, month comparisons, anomalies, and ratios.",
        status=ctx.period_label,
    )
    notice = st.session_state.pop("insight_action_notice", None)
    if notice:
        st.warning(notice)
    # render() snapshot: used for period_status_strip; _snapshot_payload() below
    # makes a separate read-only call for widget helpers (lazy-fallback pattern).
    snapshot = ReportSnapshotService(ctx.db_path).snapshot_for_period(
        ctx.start_date,
        ctx.end_date,
        source="streamlit_insights",
        persist=False,
    )
    kpis = ctx.service.compute_true_cost_kpis(ctx.start_date, ctx.end_date)
    period_status_strip(
        start_date=ctx.start_date,
        end_date=ctx.end_date,
        row_count=int(kpis["transaction_count"]),
        snapshot=snapshot,
        source="Insight findings + snapshot",
    )
    engine = _ensure_findings(ctx)
    refresh_col, summary_col = st.columns([0.22, 0.78])
    if refresh_col.button("Refresh Insights"):
        # This refreshes the OPERATOR's selected sidebar period, not the standing
        # trailing window `analysis refresh` uses, and reconciliation is keyed on
        # the exact date pair -- so a preset whose end date ties the standing
        # window's mints a SECOND stored period beside it, permanently. That is
        # correct for this page, which reads back the same period it refreshed.
        # `ledger.insights` therefore serves the widest window sharing the newest
        # end date and publishes `computed_at`, rather than claiming the pair it
        # serves is the trailing one; see INSIGHT_PERIOD_STATEMENT.
        result = engine.refresh_period(ctx.start_date, ctx.end_date)
        st.success(f"Generated {result.counts.get('generated', 0)} finding(s).")
        st.rerun()
    summary = engine.summary(ctx.start_date, ctx.end_date)
    served = engine.served_period(ctx.start_date, ctx.end_date)
    summary_col.caption(
        f"{summary['open']} open finding(s), {summary['high']} high priority, "
        f"{summary['watch']} on watch for this period."
    )
    if served is not None and served != (ctx.start_date, ctx.end_date):
        st.caption(
            f"Serving findings from the newest stored analysis ({served[0]} to {served[1]}) — "
            "no stored analysis matches the selected period exactly. Refresh Insights "
            "recomputes them for the current view."
        )

    navigation = take_brief_context("insights")
    if navigation:
        st.subheader(
            "Supporting findings from the brief"
            if navigation.subject or navigation.evidence_ids
            else "Findings for the brief's period"
        )
        if served != (ctx.start_date, ctx.end_date):
            st.info("No stored findings are available for this brief's exact period.")
        else:
            findings = supporting_findings(
                navigation,
                engine.list_findings(
                    ctx.start_date, ctx.end_date, include_closed=True, include_action_targets=True
                ),
            )
            if findings:
                _render_finding_rows(engine, findings, expanded=True)
            else:
                st.info(
                    "The supporting finding is no longer available for this brief's exact period."
                )
        if st.button("Browse all insights"):
            st.rerun()
        return

    view = st.segmented_control(
        "Insight",
        [
            "Priority Review",
            "Cost Cutting",
            "Category Drift",
            "Credit Cards",
            "Recurring",
            "Forecast",
            "Monthly",
            "Anomalies",
            "Ratios",
            "Preferences",
        ],
        default="Priority Review",
    )
    st.divider()

    if view == "Priority Review":
        _render_findings(
            ctx,
            engine,
            PRIORITY_REVIEW_FINDING_TYPES,
            "No duplicate, recurring-price, or unresolved-reimbursement findings are available yet.",
        )
    elif view == "Cost Cutting":
        _render_findings(
            ctx,
            engine,
            "cost_cutting",
            "No cost-cutting candidates are available yet.",
        )
    elif view == "Category Drift":
        _render_findings(
            ctx,
            engine,
            "category_drift",
            "No category drift data is available yet.",
        )
    elif view == "Credit Cards":
        payload = snapshot.get("payload") or _snapshot_payload(ctx)
        summary = payload.get("credit_card_optimization", {})
        kpi_row(
            [
                ("Cards", f"{summary.get('card_count', 0):,}", None),
                ("Card Spend", format_currency(summary.get("total_spend", 0)), None, "neg"),
            ]
        )
        _render_snapshot_rows(
            summary.get("cards", []),
            "No card/account optimization data is available yet.",
        )
        _render_findings(
            ctx,
            engine,
            "credit_cards",
            "No credit-card optimization findings are available yet.",
        )
    elif view == "Recurring":
        _render_findings(
            ctx,
            engine,
            "recurring",
            "No recurring-spend findings are available yet.",
        )
        st.divider()
        _render_patterns(ctx)
    elif view == "Forecast":
        _render_forecast(ctx)
    elif view == "Monthly":
        _render_monthly_comparison(ctx)
    elif view == "Anomalies":
        _render_findings(
            ctx,
            engine,
            "anomalies",
            "No anomaly findings are available yet.",
        )
        st.divider()
        _render_anomalies(ctx)
    elif view == "Preferences":
        _render_preferences(engine)
    else:
        _render_ratios(ctx)
