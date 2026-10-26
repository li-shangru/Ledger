"""Historical, statement-backed investment dashboard."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext
from typing import Any, cast

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from ledger.ui.components import (
    format_currency_precise,
    kpi_row,
    page_header,
    status_grid,
    style_fig,
)
from ledger.ui.context import AppContext

_HOLDINGS_LIMIT = 200
_ACTIVITY_LIMIT = 200
_CURRENT_LABEL = "Live investment value unavailable"


@dataclass(frozen=True)
class _HoldingMetric:
    total: Decimal | None
    reported_count: int
    position_count: int


@dataclass(frozen=True)
class _AllocationView:
    frame: pd.DataFrame
    title: str
    detail: str
    complete: bool


def _as_float(value: object) -> float | None:
    if value is None:
        return None
    return float(cast(Decimal | float | int | str, value))


def _status_summary(accounts: list[dict[str, Any]]) -> dict[str, Any]:
    """Summarize account lifecycle and statement freshness without current-value claims."""
    dates = [
        value for row in accounts if isinstance((value := row.get("last_statement_date")), date)
    ]
    return {
        "historical_accounts": len(accounts),
        "closed_accounts": sum(row.get("status") == "closed" for row in accounts),
        "unknown_accounts": sum(row.get("status") not in {"active", "closed"} for row in accounts),
        "transferred_accounts": sum(
            row.get("status") == "closed"
            and bool(row.get("transfer_destination"))
            and row.get("status_confirmed_on") is not None
            for row in accounts
        ),
        "lifecycle_confirmed_on": _unanimous_closed_confirmation(accounts),
        "latest_statement_date": max(dates, default=None),
        "current_value": None,
        "current_label": _CURRENT_LABEL,
    }


def _lifecycle_caption(accounts: list[dict[str, Any]]) -> str | None:
    """Render only unanimous, source-backed lifecycle facts for historical accounts."""
    historical = [row for row in accounts if row.get("status") == "closed"]
    if not historical:
        return None
    aliases = sorted(str(row["account_alias"]) for row in historical)
    alias_text = aliases[0] if len(aliases) == 1 else f"{', '.join(aliases[:-1])} and {aliases[-1]}"
    destinations = {row.get("transfer_destination") for row in historical}
    destination = next(iter(destinations)) if len(destinations) == 1 else None
    confirmed_on = _unanimous_closed_confirmation(historical)
    caption = f"{alias_text} {'is' if len(aliases) == 1 else 'are'} closed"
    if destination:
        caption += f" and transferred to {destination}"
    if confirmed_on:
        caption += f"; status confirmed {confirmed_on:%b %-d, %Y}"
    if any(row.get("closed_on") is None for row in historical):
        caption += "; exact closure dates are unavailable"
    return f"{caption}."


def _unanimous_closed_confirmation(accounts: list[dict[str, Any]]) -> date | None:
    closed = [row for row in accounts if row.get("status") == "closed"]
    confirmations = [row.get("status_confirmed_on") for row in closed]
    if not closed or not all(isinstance(value, date) for value in confirmations):
        return None
    unique = set(confirmations)
    return next(iter(unique)) if len(unique) == 1 else None


def _combined_nav_frame(rows: list[dict[str, Any]], currency: str = "USD") -> pd.DataFrame:
    """Return exact-date NAV totals; coverage is retained rather than filled."""
    rows = [row for row in rows if row.get("base_currency", "USD") == currency]
    return pd.DataFrame(
        [
            {
                "period_end": row["period_end"],
                "total_nav": _as_float(row.get("total_nav")),
                "contributing_accounts": int(row.get("contributing_accounts") or 0),
            }
            for row in rows
        ],
        columns=["period_end", "total_nav", "contributing_accounts"],
    ).sort_values("period_end", ignore_index=True)


def _account_nav_frame(rows: list[dict[str, Any]], currency: str = "USD") -> pd.DataFrame:
    """Return exact per-account NAV observations with no imputation."""
    rows = [row for row in rows if row.get("base_currency", "USD") == currency]
    return pd.DataFrame(
        [
            {
                "period_end": row["period_end"],
                "account_alias": row["account_alias"],
                "total_nav": _as_float(row.get("total_nav")),
            }
            for row in rows
        ],
        columns=["period_end", "account_alias", "total_nav"],
    ).sort_values(["account_alias", "period_end"], ignore_index=True)


def _holding_frame(rows: list[dict[str, Any]], currency: str = "USD") -> pd.DataFrame:
    """Convert one or more historical statement position snapshots into a safe table."""
    rows = [row for row in rows if row.get("currency", "USD") == currency]
    rendered: list[dict[str, Any]] = []
    for row in rows:
        cost_basis = row.get("cost_basis")
        unrealized = row.get("unrealized_pl")
        percentage_return = None
        if cost_basis not in {None, Decimal("0"), 0} and unrealized is not None:
            percentage_return = _as_float(
                (Decimal(str(unrealized)) / Decimal(str(cost_basis))) * Decimal("100")
            )
        rendered.append(
            {
                "Account": row.get("account_alias") or "—",
                "Symbol": row.get("symbol") or "—",
                "Asset class": row.get("asset_category") or "Unclassified",
                "Quantity": _as_float(row.get("quantity")),
                f"Close price ({currency})": _as_float(row.get("close_price")),
                f"Cost basis ({currency})": _as_float(row.get("cost_basis")),
                f"Market value ({currency})": _as_float(row.get("market_value")),
                f"Unrealized P&L ({currency})": _as_float(row.get("unrealized_pl")),
                "Return (%)": percentage_return,
            }
        )
    return pd.DataFrame(
        rendered,
        columns=[
            "Account",
            "Symbol",
            "Asset class",
            "Quantity",
            f"Close price ({currency})",
            f"Cost basis ({currency})",
            f"Market value ({currency})",
            f"Unrealized P&L ({currency})",
            "Return (%)",
        ],
    )


def _exact_decimal_sum(values: list[Decimal]) -> Decimal:
    with localcontext() as context:
        context.prec = max(64, sum(len(value.as_tuple().digits) for value in values) + 2)
        return sum(values, start=Decimal("0"))


def _holding_coverage(
    rows: list[dict[str, Any]], currency: str = "USD"
) -> dict[str, _HoldingMetric]:
    """Aggregate each nullable source metric exactly and retain independent coverage."""
    rows = [row for row in rows if row.get("currency", "USD") == currency]
    result: dict[str, _HoldingMetric] = {}
    for field in ("cost_basis", "market_value", "unrealized_pl"):
        values = [value for row in rows if isinstance((value := row.get(field)), Decimal)]
        result[field] = _HoldingMetric(
            total=_exact_decimal_sum(values) if values else None,
            reported_count=len(values),
            position_count=len(rows),
        )
    return result


def _holding_kpi_rows(
    coverage: dict[str, _HoldingMetric],
    currency: str = "USD",
) -> list[tuple[str, str, str | None] | tuple[str, str, str | None, str | None]]:
    definitions = (
        ("cost_basis", f"Cost basis ({currency})", "Selected statement month"),
        ("market_value", f"Market value ({currency})", "Reported position value"),
        ("unrealized_pl", f"Unrealized P&L ({currency})", "Statement-reported"),
    )
    rows: list[tuple[str, str, str | None] | tuple[str, str, str | None, str | None]] = []
    for field, label, complete_detail in definitions:
        metric = coverage[field]
        if metric.reported_count == 0 or metric.total is None:
            rows.append(
                (
                    label,
                    "Unavailable",
                    f"0/{metric.position_count} positions reported",
                )
            )
            continue
        if metric.reported_count < metric.position_count:
            rows.append(
                (
                    f"{label} — partial ({metric.reported_count}/{metric.position_count})",
                    _money(metric.total, currency),
                    "Reported positions only",
                )
            )
            continue
        if field == "unrealized_pl":
            rows.append(
                (
                    label,
                    _money(metric.total, currency),
                    complete_detail,
                    "pos" if metric.total >= 0 else "neg",
                )
            )
        else:
            rows.append((label, _money(metric.total, currency), complete_detail))
    return rows


def _holding_allocation_frame(rows: list[dict[str, Any]], currency: str = "USD") -> pd.DataFrame:
    """Build reported allocation from exact Decimal source values before plotting."""
    rows = [row for row in rows if row.get("currency", "USD") == currency]
    values: dict[str, list[Decimal]] = {}
    for row in rows:
        market_value = row.get("market_value")
        if not isinstance(market_value, Decimal):
            continue
        asset_class = str(row.get("asset_category") or "Unclassified")
        values.setdefault(asset_class, []).append(market_value)
    return pd.DataFrame(
        [
            {
                "Asset class": asset_class,
                f"Market value ({currency})": float(_exact_decimal_sum(amounts)),
            }
            for asset_class, amounts in sorted(values.items())
        ],
        columns=["Asset class", f"Market value ({currency})"],
    )


def _reported_allocation(
    statements: list[dict[str, Any]],
    positions: list[dict[str, Any]],
    currency: str = "USD",
) -> _AllocationView:
    """Prefer complete statement NAV classes; label position-only fallback explicitly."""
    statements = [row for row in statements if row.get("base_currency", "USD") == currency]
    reported: dict[str, list[Decimal]] = {}
    complete = bool(statements)
    for statement in statements:
        balances = statement.get("asset_balances")
        total_nav = statement.get("total_nav")
        if not isinstance(balances, list) or not balances or not isinstance(total_nav, Decimal):
            complete = False
            break
        values: list[Decimal] = []
        for balance in balances:
            value = balance.get("current_total") if isinstance(balance, dict) else None
            asset_class = balance.get("asset_class") if isinstance(balance, dict) else None
            if (
                not isinstance(value, Decimal)
                or not isinstance(asset_class, str)
                or not asset_class
            ):
                complete = False
                break
            values.append(value)
            reported.setdefault(asset_class, []).append(value)
        if not complete or _exact_decimal_sum(values) != total_nav:
            complete = False
            break
    if complete:
        frame = pd.DataFrame(
            [
                {
                    "Asset class": asset_class,
                    f"Market value ({currency})": float(_exact_decimal_sum(values)),
                }
                for asset_class, values in sorted(reported.items())
            ],
            columns=["Asset class", f"Market value ({currency})"],
        )
        return _AllocationView(
            frame=frame,
            title="Statement-reported allocation",
            detail=(
                "Uses every reported statement NAV class, including cash and other reported "
                "NAV classes."
            ),
            complete=True,
        )
    return _AllocationView(
        frame=_holding_allocation_frame(positions, currency),
        title="Open-position-only mix (partial)",
        detail=(
            "Cash and other NAV classes excluded because statement balance coverage is partial. "
            f"Only reported {currency} positions are included; no FX conversion is applied."
        ),
        complete=False,
    )


def _holding_account_coverage(
    accounts: list[dict[str, Any]],
    selected_statements: list[dict[str, Any]],
    account_alias: str | None,
) -> str:
    expected = (
        {account_alias}
        if account_alias is not None
        else {str(row.get("account_alias")) for row in accounts if row.get("account_alias")}
    )
    contributing = {
        str(row.get("account_alias"))
        for row in selected_statements
        if row.get("account_alias") in expected
    }
    missing = expected - contributing
    contributing_text = ", ".join(sorted(contributing)) or "none"
    missing_text = ", ".join(sorted(missing)) or "none"
    return f"Contributing accounts: {contributing_text}; missing accounts: {missing_text}."


def _activity_frame(rows: list[dict[str, Any]]) -> pd.DataFrame:
    """Convert statement-owned activity rows into a safe, bounded-detail table."""
    return pd.DataFrame(
        [
            {
                "Date": row.get("activity_date"),
                "Account": row.get("account_alias") or "—",
                "Type": str(row.get("activity_type") or "—").replace("_", " ").title(),
                "Symbol": row.get("symbol") or "—",
                "Amount": _as_float(row.get("amount")),
                "Currency": row.get("currency") or "—",
                "Description": row.get("description") or "—",
            }
            for row in rows
        ],
        columns=["Date", "Account", "Type", "Symbol", "Amount", "Currency", "Description"],
    )


def _money(value: Decimal | float | int | None, currency: str = "USD") -> str:
    return f"{format_currency_precise(value)} {currency}" if value is not None else "—"


def _reported_twr(value: Decimal | float | int | None) -> str:
    """Format a statement-reported fractional return without inventing a missing value."""
    return f"{float(value) * 100:.2f}%" if value is not None else "—"


def _nav_figure(combined: pd.DataFrame, accounts: pd.DataFrame, currency: str = "USD") -> go.Figure:
    fig = go.Figure()
    if not accounts.empty:
        for alias in sorted(accounts["account_alias"].unique()):
            rows = accounts[accounts["account_alias"] == alias]
            fig.add_trace(
                go.Scatter(
                    x=rows["period_end"],
                    y=rows["total_nav"],
                    mode="lines+markers",
                    line={"width": 2},
                    hovertemplate=(
                        "%{x|%b %-d, %Y}<br>Reported NAV: $%{y:,.2f} "
                        + currency
                        + "<extra></extra>"
                    ),
                    name=str(alias),
                )
            )
    fig.add_trace(
        go.Scatter(
            x=combined["period_end"],
            y=combined["total_nav"],
            customdata=combined[["contributing_accounts"]],
            mode="lines+markers",
            line={"width": 3},
            hovertemplate=(
                "%{x|%b %-d, %Y}<br>Reported NAV: $%{y:,.2f} "
                + currency
                + "<br>Source accounts: %{customdata[0]}<extra></extra>"
            ),
            name="Combined reported NAV",
        )
    )
    fig.update_layout(
        height=390,
        margin={"l": 14, "r": 14, "t": 24, "b": 30},
        hovermode="x unified",
        xaxis_title="",
        yaxis={"title": currency, "tickprefix": "$", "tickformat": ",.0f"},
    )
    return style_fig(fig)


def _performance_frame(rows: list[dict[str, Any]], currency: str = "USD") -> pd.DataFrame:
    rows = [row for row in rows if row.get("base_currency", "USD") == currency]
    latest_by_account: dict[str, dict[str, Any]] = {}
    for row in rows:
        alias = str(row.get("account_alias") or "—")
        if alias not in latest_by_account:
            latest_by_account[alias] = row
    return pd.DataFrame(
        [
            {
                "Account": alias,
                "Statement date": row.get("period_end"),
                f"Reported NAV ({currency})": _as_float(row.get("total_nav")),
                "Reported TWR": _reported_twr(row.get("time_weighted_return")),
                f"Realized P&L ({currency})": _as_float(row.get("realized_pl")),
                f"Unrealized P&L ({currency})": _as_float(row.get("unrealized_pl")),
            }
            for alias, row in sorted(latest_by_account.items())
        ],
        columns=[
            "Account",
            "Statement date",
            f"Reported NAV ({currency})",
            "Reported TWR",
            f"Realized P&L ({currency})",
            f"Unrealized P&L ({currency})",
        ],
    )


def _historical_kpis(
    performance_rows: list[dict[str, Any]],
    activity_summary: Mapping[str, Decimal],
    currency: str = "USD",
) -> dict[str, Any]:
    """Compute selected-account metrics only from reported values in the chosen window.

    Scope discipline: deposits/dividends aggregate every statement in the window,
    and realized P&L is a period flow, so it is summed over the same statements —
    taking only the newest statement's month silently misstated it against the
    sibling tiles. Unrealized P&L is a point-in-time stock and the statement
    return is per-statement (chaining is deliberately refused), so both stay
    latest-statement values and their labels say so."""
    performance_rows = [
        row for row in performance_rows if row.get("base_currency", "USD") == currency
    ]
    if not performance_rows:
        return {
            "latest_nav": None,
            "latest_period_end": None,
            "net_deposits_withdrawals": None,
            "dividends_interest": None,
            "realized_pl": None,
            "unrealized_pl": None,
            "reported_return": None,
        }
    latest = performance_rows[0]
    realized_values = [
        row["realized_pl"] for row in performance_rows if row.get("realized_pl") is not None
    ]
    return {
        "latest_nav": latest.get("total_nav"),
        "latest_period_end": latest.get("period_end"),
        "net_deposits_withdrawals": activity_summary["net_deposits_withdrawals"],
        "dividends_interest": activity_summary["dividends_interest"],
        "realized_pl": _exact_decimal_sum(realized_values) if realized_values else None,
        "unrealized_pl": latest.get("unrealized_pl"),
        "reported_return": latest.get("time_weighted_return"),
    }


def _provenance_caption(provenance: dict[str, Any]) -> str:
    """Format allowlisted provenance facts while ignoring private fields."""
    alias = str(provenance.get("account_alias") or "Historical account")
    start = provenance.get("period_start")
    end = provenance.get("period_end")
    period = (
        f"{start:%b %-d, %Y} to {end:%b %-d, %Y}"
        if isinstance(start, date) and isinstance(end, date)
        else "period unavailable"
    )
    import_state = str(provenance.get("import_state") or "unavailable").replace("_", " ")
    archive_state = "archive verified" if provenance.get("archive_verified") else "archive pending"
    return f"{alias} statement {period} · import {import_state} · {archive_state}"


def _latest_reported_nav(frame: pd.DataFrame) -> float | None:
    return None if frame.empty else frame.iloc[-1]["total_nav"]


def _render_holdings(position_rows: list[dict[str, Any]], currency: str) -> None:
    """Render each native currency separately, including positions outside the NAV currency."""
    holdings = _holding_frame(position_rows, currency)
    st.caption(f"Reported positions in {currency}")
    kpi_row(_holding_kpi_rows(_holding_coverage(position_rows, currency), currency))
    st.dataframe(
        holdings.head(_HOLDINGS_LIMIT),
        width="stretch",
        hide_index=True,
        column_config={
            "Quantity": st.column_config.NumberColumn(format="%.6f"),
            **{
                f"{label} ({currency})": st.column_config.NumberColumn(format=f"$%.2f {currency}")
                for label in ("Close price", "Cost basis", "Market value", "Unrealized P&L")
            },
            "Return (%)": st.column_config.NumberColumn(format="%.2f%%"),
        },
    )
    if len(holdings) > _HOLDINGS_LIMIT:
        st.caption(
            f"Showing the first {_HOLDINGS_LIMIT} of {len(holdings)} reported {currency} positions."
        )


def render(ctx: AppContext) -> None:
    page_header(
        "Investments",
        "Statement-backed investment snapshots. Values retain their reported currencies and dates.",
    )
    investments = ctx.service.investments
    accounts = investments.get_investment_accounts()
    summary = _status_summary(accounts)
    coverage_start, coverage_end = investments.get_investment_date_range()
    if coverage_start is None or coverage_end is None:
        st.info("No historical investment statements are available yet.")
        st.caption(
            "Import a reviewed investment statement to see source-backed NAV, holdings, performance, and activity."
        )
        return

    status_grid(
        [
            {
                "label": "Statement coverage",
                "value": f"{coverage_start:%b %-d, %Y} to {coverage_end:%b %-d, %Y}",
                "detail": "Imported investment statements",
            },
            {
                "label": "Investment accounts",
                "value": str(summary["historical_accounts"]),
                "detail": (
                    f"{summary['closed_accounts']} closed or transferred; "
                    f"{summary['unknown_accounts']} status unconfirmed"
                ),
            },
            {
                "label": "Latest source statement",
                "value": (
                    f"{summary['latest_statement_date']:%b %-d, %Y}"
                    if summary["latest_statement_date"]
                    else "—"
                ),
                "detail": "Statement-reported date",
            },
        ]
    )
    st.info(
        "Values describe their source statement dates. Live holdings, prices, and FX are unavailable; "
        "CAD and USD totals are kept separate. Account status is shown only when confirmed."
    )
    if lifecycle_caption := _lifecycle_caption(accounts):
        st.caption(lifecycle_caption)

    currencies = sorted({str(row["base_currency"]) for row in accounts})
    currency = (
        st.selectbox("Reporting currency", currencies) if len(currencies) > 1 else currencies[0]
    )
    accounts = [row for row in accounts if row["base_currency"] == currency]
    st.caption(f"Reporting currency: {currency}. No currency conversion is applied.")
    aliases = [str(row["account_alias"]) for row in accounts]
    account_label = st.selectbox("Investment account", ["All investment accounts", *aliases])
    account_alias = None if account_label == "All investment accounts" else account_label
    selected_dates = st.date_input(
        "Statement date range",
        value=(coverage_start, coverage_end),
        min_value=coverage_start,
        max_value=coverage_end,
    )
    if isinstance(selected_dates, date):
        start_date = end_date = selected_dates
    elif selected_dates:
        start_date, end_date = selected_dates[0], selected_dates[-1]
    else:
        start_date, end_date = coverage_start, coverage_end

    nav_rows = investments.get_investment_nav_history(
        account_alias=account_alias,
        base_currency=currency,
        start_date=start_date,
        end_date=end_date,
    )
    account_nav_rows = investments.get_investment_nav_series(
        account_alias=account_alias,
        base_currency=currency,
        start_date=start_date,
        end_date=end_date,
    )
    nav_frame = _combined_nav_frame(nav_rows, currency)
    account_nav_frame = _account_nav_frame(account_nav_rows, currency)
    latest_nav = _latest_reported_nav(nav_frame)
    performance_rows = investments.get_investment_performance(
        account_alias=account_alias,
        base_currency=currency,
        start_date=start_date,
        end_date=end_date,
    )
    activity_start = min(
        (row["period_start"] for row in performance_rows if row.get("period_start")),
        default=start_date,
    )
    activity_end = max(
        (row["period_end"] for row in performance_rows if row.get("period_end")),
        default=end_date,
    )
    if account_alias is None:
        kpi_row(
            [
                (
                    f"Latest reported NAV ({currency})",
                    _money(latest_nav, currency),
                    "Exact statement-date total",
                ),
                (
                    "Source accounts at latest NAV",
                    str(int(nav_frame.iloc[-1]["contributing_accounts"]))
                    if not nav_frame.empty
                    else "—",
                    "No forward fill",
                ),
                (summary["current_label"], "Not reported", "Statement snapshots only"),
            ]
        )
    else:
        activity_summary = investments.get_investment_activity_summary(
            account_alias=account_alias,
            base_currency=currency,
            start_date=activity_start,
            end_date=activity_end,
            currency=currency,
        )
        metrics = _historical_kpis(performance_rows, activity_summary, currency)
        latest_statement_label = (
            f"Latest statement ({metrics['latest_period_end']:%b %Y})"
            if metrics["latest_period_end"]
            else "Latest statement"
        )
        kpi_row(
            [
                (
                    f"Latest reported NAV ({currency})",
                    _money(metrics["latest_nav"], currency),
                    "Historical",
                ),
                (
                    f"Net deposits / withdrawals ({currency})",
                    _money(metrics["net_deposits_withdrawals"], currency),
                    f"{currency} activity only",
                ),
                (
                    f"Dividends + interest ({currency})",
                    _money(metrics["dividends_interest"], currency),
                    f"{currency} activity only",
                ),
                (
                    f"Realized P&L ({currency})",
                    _money(metrics["realized_pl"], currency),
                    "Statement-reported, summed over the range",
                ),
                (
                    f"Unrealized P&L ({currency})",
                    _money(metrics["unrealized_pl"], currency),
                    latest_statement_label,
                ),
                (
                    "Statement-reported return",
                    _reported_twr(metrics["reported_return"]),
                    f"{latest_statement_label}; no combined return",
                ),
            ]
        )

    st.subheader("Historical NAV")
    if nav_frame.empty:
        st.info("No source-backed NAV snapshots match this account and date range.")
    else:
        st.plotly_chart(_nav_figure(nav_frame, account_nav_frame, currency), width="stretch")
        st.caption(
            f"Combined NAV sums only {currency} accounts that reported on each exact statement date. "
            "Missing account-periods are not forward-filled, and no combined time-weighted return is calculated."
        )

    statement_dates = sorted(
        {row["period_end"] for row in performance_rows if row.get("period_end")}, reverse=True
    )
    st.subheader("Holdings and allocation")
    if not statement_dates:
        st.info("No statement snapshots are available for holdings in this selection.")
    else:
        selected_statement_date = st.selectbox(
            "Holdings statement month",
            statement_dates,
            format_func=lambda value: value.strftime("%B %Y"),
        )
        selected_statements = [
            row for row in performance_rows if row.get("period_end") == selected_statement_date
        ]
        statement_details = [
            detail
            for statement in selected_statements
            if (detail := investments.get_investment_statement(statement["statement_id"]))
            is not None
        ]
        st.caption(_holding_account_coverage(accounts, selected_statements, account_alias))
        position_rows: list[dict[str, Any]] = []
        for statement in selected_statements:
            position_rows.extend(investments.get_investment_positions(statement["statement_id"]))
        allocation = _reported_allocation(statement_details, position_rows, currency)
        left, right = st.columns((3, 2))
        with left:
            if not position_rows:
                st.info("The selected source statements contain no position rows.")
            else:
                for native_currency in sorted(
                    {str(row.get("currency", "USD")) for row in position_rows}
                ):
                    _render_holdings(position_rows, native_currency)
        with right:
            if allocation.frame.empty:
                st.info("No source-backed allocation values are available.")
            else:
                allocation_fig = go.Figure(
                    go.Pie(
                        labels=allocation.frame["Asset class"],
                        values=allocation.frame[f"Market value ({currency})"],
                        hole=0.48,
                        hovertemplate="%{label}<br>$%{value:,.2f} " + currency + "<extra></extra>",
                    )
                )
                allocation_fig.update_layout(
                    height=360,
                    margin={"l": 12, "r": 12, "t": 34, "b": 16},
                    title=allocation.title,
                )
                st.plotly_chart(style_fig(allocation_fig), width="stretch")
                st.caption(allocation.detail)
        provenance = [
            investments.get_investment_provenance(statement["statement_id"])
            for statement in selected_statements
        ]
        captions = [_provenance_caption(row) for row in provenance if row is not None]
        if captions:
            st.caption(" | ".join(captions))

    st.subheader("Account performance")
    performance = _performance_frame(performance_rows, currency)
    if performance.empty:
        st.info("No account-level statement performance matches this selection.")
    else:
        st.dataframe(
            performance,
            width="stretch",
            hide_index=True,
            column_config={
                f"Reported NAV ({currency})": st.column_config.NumberColumn(
                    format=f"$%.2f {currency}"
                ),
                "Reported TWR": st.column_config.TextColumn(),
                f"Realized P&L ({currency})": st.column_config.NumberColumn(
                    format=f"$%.2f {currency}"
                ),
                f"Unrealized P&L ({currency})": st.column_config.NumberColumn(
                    format=f"$%.2f {currency}"
                ),
            },
        )
        st.caption(
            "Returns are statement-reported per account; Ledger does not calculate a combined return."
        )

    st.subheader("Statement activity")
    st.caption(
        "Activity amounts retain their source-reported currency. Only native amounts in the "
        f"selected currency ({currency}) contribute to its KPI totals; no FX conversion is applied."
    )
    activity_types = investments.get_investment_activity_types(
        account_alias=account_alias,
        base_currency=currency,
        start_date=activity_start,
        end_date=activity_end,
    )
    symbols = investments.get_investment_activity_symbols(
        account_alias=account_alias,
        base_currency=currency,
        start_date=activity_start,
        end_date=activity_end,
    )
    activity_cols = st.columns(2)
    with activity_cols[0]:
        selected_types = st.multiselect("Activity type", activity_types, default=activity_types)
    with activity_cols[1]:
        selected_symbol = st.selectbox("Symbol", ["All symbols", *symbols])
    activities = investments.get_investment_activities(
        account_alias=account_alias,
        base_currency=currency,
        start_date=activity_start,
        end_date=activity_end,
        activity_types=selected_types,
        symbol=None if selected_symbol == "All symbols" else selected_symbol,
        limit=_ACTIVITY_LIMIT + 1,
    )
    activity_frame = _activity_frame(activities[:_ACTIVITY_LIMIT])
    if activity_frame.empty:
        st.info("No source-backed activities match these filters.")
    else:
        st.dataframe(
            activity_frame,
            width="stretch",
            hide_index=True,
            column_config={"Amount": st.column_config.NumberColumn(format="%.2f")},
        )
        if len(activities) > _ACTIVITY_LIMIT:
            st.caption(f"Showing the most recent {_ACTIVITY_LIMIT} matching activities.")
