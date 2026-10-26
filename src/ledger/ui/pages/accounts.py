"""Accounts page."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from ledger.core.account_identity import account_identity
from ledger.reporting import ReportSnapshotService
from ledger.ui.components import (
    download_dataframe,
    format_currency,
    kpi_row,
    page_header,
    period_status_strip,
    render_transaction_table,
    style_fig,
    truncation_notice,
)
from ledger.ui.context import AppContext
from ledger.ui.metric_explanations import render_metric_explanations


def _normalize_account_text(value: object) -> str:
    return "".join(char for char in str(value or "").lower() if char.isalnum())


def _account_family_token(account: dict) -> str:
    from ledger.adapters.registry import compatibility_policy

    text = _normalize_account_text(
        " ".join(
            str(account.get(field) or "")
            for field in ("account_number", "account_type", "account_subtype")
        )
    )
    if "chequ" in text or "checking" in text:
        return "chequing"
    if "saving" in text:
        return "savings"
    for token, family in compatibility_policy()["account_product_tokens"]:
        normalized_token = _normalize_account_text(token)
        if normalized_token and normalized_token in text:
            return _normalize_account_text(family)
    return _normalize_account_text(
        account.get("account_subtype")
        or account.get("account_type")
        or account.get("account_number")
    )


def _canonical_account_key(account: dict) -> str:
    return account_identity(
        str(account.get("institution") or ""),
        str(account.get("account_number") or ""),
    )


def _month_span(start: date | None, end: date | None) -> int:
    if not start or not end:
        return 1
    return max(1, ((end.year - start.year) * 12) + end.month - start.month + 1)


def _logical_account_groups(accounts: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for account in accounts:
        grouped.setdefault(_canonical_account_key(account), []).append(account)

    logical_accounts = []
    for canonical_account_id, account_group in grouped.items():
        ordered = sorted(
            account_group,
            key=lambda row: (
                row.get("first_transaction_date") or date.min,
                row.get("account_number") or "",
            ),
        )
        latest = max(ordered, key=lambda row: row.get("last_transaction_date") or date.min)
        first_dates = [d for row in ordered if (d := row.get("first_transaction_date")) is not None]
        first_transaction_date = min(first_dates, default=None)
        last_dates = [d for row in ordered if (d := row.get("last_transaction_date")) is not None]
        last_transaction_date = max(last_dates, default=None)
        transaction_count = sum(int(row.get("transaction_count") or 0) for row in ordered)
        months = _month_span(first_transaction_date, last_transaction_date)

        logical = dict(latest)
        logical["account_id"] = canonical_account_id
        logical["account_ids"] = list(dict.fromkeys(row["account_id"] for row in ordered))
        logical["account_numbers"] = list(dict.fromkeys(row["account_number"] for row in ordered))
        logical["first_transaction_date"] = first_transaction_date
        logical["last_transaction_date"] = last_transaction_date
        logical["transaction_count"] = transaction_count
        logical["avg_monthly_transactions"] = Decimal(transaction_count) / Decimal(months)
        logical["is_active"] = any(bool(row.get("is_active")) for row in ordered)
        logical_accounts.append(logical)

    return sorted(logical_accounts, key=_account_label)


def _account_label(account: dict) -> str:
    number = account.get("account_number") or account.get("account_id", "")
    subtype = account.get("account_subtype") or account.get("account_type")
    linked_count = len(account.get("account_numbers") or [])
    if linked_count > 1:
        label = f"{account['institution']} - {subtype} ({linked_count} linked)"
    else:
        suffix = number[-4:] if number else "----"
        label = f"{account['institution']} - {subtype} (...{suffix})"
    if not account.get("is_active", True):
        label = f"{label} - inactive"
    return label


def _account_balances(
    service,
    account_ids: list[str],
    *,
    balance_basis: Literal["relative", "absolute"],
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[dict]:
    balances = []
    for account_id in account_ids:
        balances.extend(
            service.get_account_balances(
                account_id=account_id,
                start_date=start_date,
                end_date=end_date,
                balance_basis=balance_basis,
            )
        )
    return sorted(balances, key=lambda row: row["balance_date"])


def _account_balance_history(
    service,
    account_ids: list[str],
    *,
    end_date: date | None = None,
) -> tuple[Literal["relative", "absolute"], list[dict]]:
    """Choose one explicit basis, preferring statement-backed absolute history."""
    absolute = _account_balances(
        service,
        account_ids,
        balance_basis="absolute",
        end_date=end_date,
    )
    if absolute:
        return "absolute", absolute
    return (
        "relative",
        _account_balances(
            service,
            account_ids,
            balance_basis="relative",
            end_date=end_date,
        ),
    )


def _period_balance_history(
    service,
    account_ids: list[str],
    *,
    start_date: date,
    end_date: date,
) -> tuple[Literal["relative", "absolute"] | None, list[dict]]:
    """Basis for the IN-PERIOD history chart, chosen from what the period holds.

    The all-history chooser above serves the anchor KPI, but a period can hold
    only relative rows while absolute anchors exist earlier: choosing a basis on
    all history and then filtering the chart to the period hid a fully populated
    in-period relative series behind a "no snapshots" empty state. Statement
    bases are still never mixed inside one chart."""
    absolute = _account_balances(
        service,
        account_ids,
        balance_basis="absolute",
        start_date=start_date,
        end_date=end_date,
    )
    if absolute:
        return "absolute", absolute
    relative = _account_balances(
        service,
        account_ids,
        balance_basis="relative",
        start_date=start_date,
        end_date=end_date,
    )
    if relative:
        return "relative", relative
    return None, []


def _default_account_index(accounts: list[dict], start_date: date, end_date: date) -> int:
    """Index of the first account with transactions overlapping the period.

    The picker defaulted to the alphabetically-first account, which on the
    default period could be one whose data ended before the period opened —
    the page then rendered zero activity and an empty history while another
    account carried every in-period transaction."""

    def _as_date(value: object) -> date | None:
        if isinstance(value, date):
            return value
        if isinstance(value, str) and value:
            try:
                return date.fromisoformat(value)
            except ValueError:
                return None
        return None

    for index, account in enumerate(accounts):
        first = _as_date(account.get("first_transaction_date"))
        last = _as_date(account.get("last_transaction_date"))
        if first is None or last is None:
            continue
        if first <= end_date and last >= start_date:
            return index
    return 0


def _period_end_detail(latest_balance: dict | None, end_date: date) -> str | None:
    """Disclose the anchor date whenever it is not the period end it stands in for.

    "Period-End Balance" used to render the newest snapshot at or before the
    period end with no date at all, so a month-stale anchor read as the balance
    on the period's last day while the same view counted newer transactions."""
    if not latest_balance:
        return None
    balance_date = latest_balance.get("balance_date")
    if balance_date is None or balance_date == end_date:
        return None
    return f"as of {balance_date}"


def _period_end_balance_kpi(
    balance_basis: Literal["relative", "absolute"],
    latest_balance: dict | None,
    end_date: date,
) -> tuple[str, str, str | None]:
    """Build the exact headline label/value that its explanation will inspect."""
    if latest_balance is None:
        return (
            "Period-End Balance",
            "Unavailable",
            "No balance snapshot is available on or before the selected period end.",
        )
    label = _balance_presentation(balance_basis)["period_end"]
    return (
        label,
        format_currency(Decimal(str(latest_balance["balance"]))),
        _period_end_detail(latest_balance, end_date),
    )


def _balance_presentation(balance_basis: Literal["relative", "absolute"]) -> dict[str, str]:
    if balance_basis == "absolute":
        return {
            "period_end": "Period-End Balance",
            "history": "Balance History",
            "empty": "No balance snapshots found for this account in the selected period.",
            "low": "Period Low",
            "high": "Period High",
            "trace": "Balance",
            "avg_7d": "7-day avg",
            "avg_30d": "30-day avg",
        }
    return {
        "period_end": "Period-End Relative Change",
        "history": "Relative Change History",
        "empty": "No relative change snapshots found for this account in the selected period.",
        "low": "Period Low Change",
        "high": "Period High Change",
        "trace": "Relative change",
        "avg_7d": "7-day avg change",
        "avg_30d": "30-day avg change",
    }


def _account_period_kpis(
    service,
    *,
    start_date: date,
    end_date: date,
    institution: str,
    account_numbers: list[str],
) -> dict:
    """Return uncapped SQL KPIs for one logical account."""
    return service.compute_kpis(
        start_date,
        end_date,
        institutions=[institution],
        accounts=account_numbers,
    )


def _balance_chart(
    balances: list[dict],
    *,
    balance_basis: Literal["relative", "absolute"],
) -> go.Figure:
    presentation = _balance_presentation(balance_basis)
    df = pd.DataFrame(balances).sort_values("balance_date")
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=df["balance_date"],
            y=df["balance"],
            name=presentation["trace"],
            mode="lines",
            line={"width": 3},
        )
    )
    if "balance_7d_avg" in df:
        fig.add_trace(
            go.Scatter(
                x=df["balance_date"],
                y=df["balance_7d_avg"],
                name=presentation["avg_7d"],
                mode="lines",
                line={"width": 2, "dash": "dash"},
            )
        )
    if "balance_30d_avg" in df:
        fig.add_trace(
            go.Scatter(
                x=df["balance_date"],
                y=df["balance_30d_avg"],
                name=presentation["avg_30d"],
                mode="lines",
                line={"width": 2, "dash": "dot"},
            )
        )
    fig.update_layout(
        height=420,
        margin={"l": 12, "r": 12, "t": 20, "b": 28},
        hovermode="x unified",
        legend={"orientation": "h", "y": 1.08},
        xaxis_title="",
        yaxis_title="",
    )
    return style_fig(fig)


def render(ctx: AppContext) -> None:
    page_header(
        "Accounts",
        "Statement-backed balances or relative change, plus activity by account.",
        status=ctx.period_label,
    )

    accounts = _logical_account_groups(ctx.service.get_accounts())
    if not accounts:
        st.warning("No accounts found in the database.")
        return

    snapshot = ReportSnapshotService(ctx.db_path).snapshot_for_period(
        ctx.start_date,
        ctx.end_date,
        source="streamlit_accounts",
        persist=False,
    )
    kpis = ctx.service.compute_kpis(ctx.start_date, ctx.end_date)
    period_status_strip(
        start_date=ctx.start_date,
        end_date=ctx.end_date,
        row_count=int(kpis["transaction_count"]),
        snapshot=snapshot,
        source="Snapshot + live queries",
    )
    card_summary = (snapshot or {}).get("payload", {}).get("credit_card_optimization", {})
    if card_summary:
        card_cols = st.columns(2)
        card_cols[0].metric("Cards Detected", f"{card_summary.get('card_count', 0):,}")
        card_cols[1].metric("Card Spend", format_currency(card_summary.get("total_spend", 0)))

    options = {_account_label(account): account for account in accounts}
    selected_label = st.selectbox(
        "Account",
        options=list(options.keys()),
        # Alphabetical-first used to land on an account with no activity in the
        # default period, rendering an all-zero page while another account held
        # every in-period transaction.
        index=_default_account_index(list(options.values()), ctx.start_date, ctx.end_date),
        key="selected_account",
    )
    account = options[selected_label]
    account_id = account["account_id"]
    account_number = account["account_number"]
    account_ids = account.get("account_ids") or [account_id]
    account_numbers = account.get("account_numbers") or [account_number]
    account_kpis = _account_period_kpis(
        ctx.service,
        start_date=ctx.start_date,
        end_date=ctx.end_date,
        institution=account["institution"],
        account_numbers=account_numbers,
    )

    balance_basis, balances = _account_balance_history(
        ctx.service,
        account_ids,
        end_date=ctx.end_date,
    )
    presentation = _balance_presentation(balance_basis)
    period_basis, period_balances = _period_balance_history(
        ctx.service,
        account_ids,
        start_date=ctx.start_date,
        end_date=ctx.end_date,
    )
    _account_row_cap = 5000
    txns = ctx.service.get_transactions(
        start_date=ctx.start_date,
        end_date=ctx.end_date,
        institutions=[account["institution"]],
        accounts=account_numbers,
        limit=_account_row_cap,
        order_by="date_desc",
    )
    _txns_truncation = truncation_notice(
        len(txns),
        _account_row_cap,
        kpis_limited=False,
    )

    latest_balance = balances[-1] if balances else None
    period_end_metric = _period_end_balance_kpi(balance_basis, latest_balance, ctx.end_date)
    current_balance = (
        Decimal(str(latest_balance["balance"])) if latest_balance is not None else None
    )
    utilization = None
    if latest_balance and latest_balance.get("credit_utilization") is not None:
        utilization = Decimal(str(latest_balance["credit_utilization"]))

    kpi_row(
        [
            (
                *period_end_metric,
                None if current_balance is None else ("pos" if current_balance >= 0 else "neg"),
            ),
            ("Period Activity", f"{account_kpis['activity_count']:,}", None),
            ("Money in", format_currency(account_kpis["income"]), "all inflows", "pos"),
            (
                "Money out",
                format_currency(account_kpis["gross_expenses"]),
                f"{format_currency(account_kpis['expenses'])} counted as spending",
                "neg",
            ),
        ]
    )
    render_metric_explanations(
        ctx.db_path,
        ctx.start_date,
        ctx.end_date,
        {
            period_end_metric[0]: {
                "metric": "balance",
                "account_id": (
                    str(latest_balance.get("account_id") or account_id)
                    if latest_balance is not None
                    else account_id
                ),
                "balance_basis": balance_basis,
                "balance_date": (
                    latest_balance.get("balance_date") if latest_balance is not None else None
                ),
            }
        },
        key="accounts",
    )
    # "Income"/"Expenses" implied a matched pair, but income is gross while the old
    # Expenses tile applied the spend filter -- so an excluded transfer IN counted
    # while the matching transfer OUT did not, and the two tiles could not be
    # subtracted. Both tiles are now gross; the spend figure is shown as a sub-label.
    if _txns_truncation:
        st.caption(_txns_truncation)

    st.caption(
        f"{account['institution']} | {account['account_type']} | "
        f"{account['first_transaction_date']} to {account['last_transaction_date']}"
    )
    if len(account_numbers) > 1:
        st.caption(
            f"Activity combines {len(account_numbers)} linked account identities for this logical account."
        )

    col1, col2 = st.columns([0.64, 0.36], gap="large")
    with col1:
        period_presentation = (
            _balance_presentation(period_basis) if period_basis is not None else presentation
        )
        st.subheader(period_presentation["history"])
        if period_balances and period_basis is not None:
            st.plotly_chart(
                _balance_chart(period_balances, balance_basis=period_basis),
                width="stretch",
            )
        else:
            empty = period_presentation["empty"]
            if latest_balance and latest_balance.get("balance_date"):
                empty += (
                    f" The newest {presentation['trace'].lower()} snapshot is from "
                    f"{latest_balance['balance_date']}."
                )
            st.info(empty)

    with col2:
        st.subheader("Account Details")
        detail_rows = [
            ("Subtype", account.get("account_subtype") or "-"),
            ("Linked Identities", f"{len(account_numbers):,}"),
            ("Transactions", f"{account['transaction_count']:,}"),
            ("Avg Monthly Txns", f"{float(account['avg_monthly_transactions']):.1f}"),
            ("Active", "Yes" if account["is_active"] else "No"),
        ]
        if account.get("detected_credit_limit"):
            detail_rows.append(
                ("Detected Limit", format_currency(account["detected_credit_limit"]))
            )
        if utilization is not None:
            detail_rows.append(("Credit Utilization", f"{float(utilization):.1f}%"))
        st.dataframe(
            pd.DataFrame(detail_rows, columns=["Field", "Value"]),
            width="stretch",
            hide_index=True,
            height=245,
        )

        if period_balances and period_basis is not None:
            # Label the low/high with the basis the period series actually uses —
            # a relative-basis period under an absolute anchor KPI must not call
            # its extremes "Period Low/High Balance".
            extremes_presentation = _balance_presentation(period_basis)
            min_balance = min(Decimal(str(row["balance"])) for row in period_balances)
            max_balance = max(Decimal(str(row["balance"])) for row in period_balances)
            st.metric(extremes_presentation["low"], format_currency(min_balance))
            st.metric(extremes_presentation["high"], format_currency(max_balance))

    st.subheader("Account Activity")
    df = render_transaction_table(txns, detailed=False, height=560)
    download_dataframe(
        "Download account CSV",
        df,
        f"{account['institution']}_{_account_family_token(account)}_{ctx.start_date}_{ctx.end_date}.csv",
    )
