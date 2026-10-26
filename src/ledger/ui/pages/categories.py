"""Category and merchant analysis page."""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import plotly.express as px
import streamlit as st

from ledger.finance_policy import is_spend_row
from ledger.planning import BudgetService
from ledger.reporting import ReportSnapshotService
from ledger.ui.components import (
    CURRENCY_FORMAT,
    bar_chart,
    build_hierarchical_spending,
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
from ledger.ui.navigation import take_brief_context


def positive_true_cost_spending(
    category_details: dict[str, dict[str, Decimal]],
) -> dict[str, Decimal]:
    """Return the per-category true-cost spend, keeping only net-positive categories.

    Matches get_true_cost_by_category (query/exports.py): a fully-offset or net-credit
    category (true_cost <= 0) is not spending and must not appear in the spend picker.
    """
    return {
        category: values["true_cost"]
        for category, values in category_details.items()
        if values["true_cost"] > 0
    }


def _spend_universe_expenses(transactions) -> list:
    """Keep only expense rows that belong to the canonical spend universe.

    The True Cost KPI numerator derives from ``true_cost_by_category`` (the full
    spend universe), so the breakdown/Avg/Top-Merchants that share the KPI row
    must divide and sum over the SAME universe. Otherwise a non-CAD or
    ``exclude_from_spending`` merchant miscategorized into the selected category
    would appear in Top Merchants with a dollar value and inflate the Avg
    denominator while contributing $0 to the True Cost shown directly above.

    Uses ``is_spend_row`` (the in-memory mirror of the SQL spend universe) so
    the page reconciles with the aggregate layer -- mirroring ``_spending_by_dow``.
    """
    return [
        txn
        for txn in transactions
        if txn.amount_signed < 0
        and is_spend_row(
            amount_signed=txn.amount_signed,
            cashflow_type=txn.cashflow_type,
            include_in_cashflow=txn.include_in_cashflow,
            exclude_from_spending=txn.exclude_from_spending,
            currency=txn.currency,
        )
    ]


def _category_members(category: str, ctx: AppContext, hierarchy: dict) -> list[str]:
    children = hierarchy["hierarchy"].get(category, [])
    if children:
        return children
    return [category]


def _category_trend(ctx: AppContext, categories: list[str]) -> pd.DataFrame:
    rows = []
    for category in categories:
        for item in ctx.service.get_category_spending_trend(
            category,
            ctx.start_date,
            ctx.end_date,
            frequency="monthly",
        ):
            rows.append(
                {
                    "Period": item["period"],
                    "Category": category,
                    "Amount": float(item["amount"]),
                    "Transactions": item["transaction_count"],
                }
            )
    return pd.DataFrame(rows)


def _spending_by_dow(transactions) -> pd.DataFrame:
    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    totals = {day: Decimal("0") for day in day_names}
    for txn in transactions:
        # Match the canonical spend universe (finance_policy.is_spend_row) so the chart
        # never disagrees with the True Cost totals on the same page: expense-side,
        # in cashflow, not excluded from spending, and CAD-only (non-CAD rows are
        # un-FX-converted and must not be summed at par).
        if is_spend_row(
            amount_signed=txn.amount_signed,
            cashflow_type=txn.cashflow_type,
            include_in_cashflow=txn.include_in_cashflow,
            exclude_from_spending=txn.exclude_from_spending,
            currency=txn.currency,
        ):
            totals[day_names[txn.posting_date.weekday()]] += abs(txn.amount_signed)
    return pd.DataFrame(
        {"Day": list(totals.keys()), "Amount": [float(value) for value in totals.values()]}
    )


def _render_category(ctx: AppContext, subject: str | None = None) -> None:
    # Derive the per-category spend from the details rather than issuing a second
    # true-cost aggregation over the same period (get_true_cost_by_category recomputes
    # what category_details already carries under "true_cost").
    category_details = ctx.service.get_true_cost_category_details(ctx.start_date, ctx.end_date)
    # Only surface categories with net-positive true cost as spend options (a
    # fully-offset or net-credit category is not spending); see
    # positive_true_cost_spending.
    spending = positive_true_cost_spending(category_details)
    hierarchy = build_hierarchical_spending(spending, ctx.category_config)
    totals = hierarchy["parent_totals"] or spending
    # Brief observations name individual categories, including children and a
    # category whose prior spending has fallen to zero in this window.
    choices = dict(spending) | totals
    known = set(category_details) | {rule.name for rule in ctx.category_config.categories}
    if subject is not None and subject not in known:
        st.info("The category in this brief is no longer available in this database.")
        return
    for category in (subject, st.session_state.get("selected_category")):
        if category in known:
            choices.setdefault(category, Decimal("0"))
    options = sorted(choices, key=lambda key: choices[key], reverse=True)
    if not options:
        st.info("No category spending found for this period.")
        return

    if subject in options:
        st.session_state["selected_category"] = subject
    selected = st.selectbox(
        "Category",
        options=options,
        key="selected_category",
    )
    members = _category_members(selected, ctx, hierarchy)
    transactions = ctx.service.get_transactions(
        start_date=ctx.start_date,
        end_date=ctx.end_date,
        categories=members,
        limit=5000,
    )
    expense_transactions = _spend_universe_expenses(transactions)
    total = sum(
        category_details.get(member, {}).get("true_cost", Decimal("0")) for member in members
    )
    offsets = sum(
        category_details.get(member, {}).get("offsets", Decimal("0")) for member in members
    )
    avg = total / len(expense_transactions) if expense_transactions else Decimal("0")
    merchants: dict[str, Decimal] = {}
    for txn in expense_transactions:
        merchant = txn.merchant or "Unknown"
        merchants[merchant] = merchants.get(merchant, Decimal("0")) + abs(txn.amount_signed)

    kpi_row(
        [
            (
                "True Cost",
                format_currency(total),
                f"{format_currency(offsets)} offsets",
                "neg",
            ),
            ("Transactions", f"{len(expense_transactions):,}", None),
            ("Avg Transaction", format_currency_precise(avg), None),
            ("Merchants", f"{len(merchants):,}", None),
        ]
    )
    budget_rows = {
        row["category"]: row
        for row in BudgetService(ctx.db_path, prepare_schema=False).budget_variance(
            ctx.start_date, ctx.end_date
        )
    }
    budget = budget_rows.get(selected)
    if budget:
        st.caption(
            "Budget: "
            f"{format_currency(budget['budget'])}; "
            f"remaining {format_currency(budget['variance'])}; "
            f"status {budget['status']}. Actual uses gross ledger.aggregate spending, "
            "not the true-cost amount above."
        )

    col1, col2 = st.columns(2, gap="large")
    with col1:
        st.subheader("Trend")
        trend = _category_trend(ctx, members)
        if not trend.empty:
            if len(members) > 1:
                fig = px.line(
                    trend, x="Period", y="Amount", color="Category", markers=True, height=360
                )
                # Period labels, not timestamps -- see line_chart / _cashflow_chart.
                fig.update_layout(
                    margin={"l": 12, "r": 12, "t": 20, "b": 28},
                    xaxis={"type": "category"},
                )
                st.plotly_chart(style_fig(fig), width="stretch")
            else:
                st.plotly_chart(
                    line_chart(
                        trend["Period"].tolist(),
                        trend["Amount"].tolist(),
                        name=selected,
                    ),
                    width="stretch",
                )
        else:
            st.info("No monthly trend data for this selection.")

    with col2:
        st.subheader("Breakdown")
        if len(members) > 1:
            child_values = [
                float(spending.get(member, Decimal("0")))
                for member in members
                if spending.get(member, Decimal("0")) > 0
            ]
            child_labels = [member for member in members if spending.get(member, Decimal("0")) > 0]
            st.plotly_chart(bar_chart(child_labels, child_values, orientation="h"), width="stretch")
        elif merchants:
            merchant_rows = sorted(merchants.items(), key=lambda item: item[1], reverse=True)[:10]
            st.plotly_chart(
                bar_chart(
                    [row[0] for row in merchant_rows],
                    [float(row[1]) for row in merchant_rows],
                    orientation="h",
                ),
                width="stretch",
            )
        else:
            st.info("No breakdown available.")

    col3, col4 = st.columns(2, gap="large")
    with col3:
        st.subheader("Day of Week")
        dow = _spending_by_dow(expense_transactions)
        st.plotly_chart(bar_chart(dow["Day"].tolist(), dow["Amount"].tolist()), width="stretch")
    with col4:
        st.subheader("Top Merchants")
        if merchants:
            merchant_df = pd.DataFrame(
                [
                    {"Merchant": name, "Amount": float(amount)}
                    for name, amount in sorted(
                        merchants.items(), key=lambda item: item[1], reverse=True
                    )[:12]
                ]
            )
            st.dataframe(
                merchant_df,
                width="stretch",
                hide_index=True,
                column_config={
                    "Amount": st.column_config.NumberColumn("Amount", format=CURRENCY_FORMAT)
                },
                height=340,
            )
        else:
            st.caption("No merchants found.")

    st.subheader("Transactions")
    render_transaction_table(transactions, detailed=False, height=500)


def _render_merchant(ctx: AppContext, subject: str | None = None) -> None:
    merchant_spending = ctx.service.get_spending_by_merchant(
        ctx.start_date, ctx.end_date, top_n=200
    )
    options = list(merchant_spending.keys()) or ctx.service.get_merchants()
    requested = [subject, st.session_state.get("selected_merchant")]
    missing = {merchant for merchant in requested if merchant and merchant not in options}
    available = set(ctx.service.get_merchants()) if missing else set(options)
    if subject is not None and subject not in available:
        st.info("The merchant in this brief is no longer available in this database.")
        return
    options.extend(sorted(missing & available))
    if not options:
        st.info("No merchants found.")
        return

    if subject in options:
        st.session_state["selected_merchant"] = subject
    selected = st.selectbox("Merchant", options=options, key="selected_merchant")
    txns = ctx.service.get_merchant_transactions(selected, ctx.start_date, ctx.end_date, limit=5000)
    expenses = _spend_universe_expenses(txns)
    total = sum(abs(txn.amount_signed) for txn in expenses)
    avg = total / len(expenses) if expenses else Decimal("0")
    categories = sorted({txn.category for txn in expenses if txn.category})

    kpi_row(
        [
            ("Spending", format_currency(total), None, "neg"),
            ("Visits", f"{len(expenses):,}", None),
            ("Avg Visit", format_currency_precise(avg), None),
            ("Categories", f"{len(categories):,}", None),
        ]
    )

    col1, col2 = st.columns(2, gap="large")
    with col1:
        st.subheader("Trend")
        trend = ctx.service.get_merchant_spending_trend(selected, ctx.start_date, ctx.end_date)
        if trend:
            st.plotly_chart(
                line_chart(
                    [row["period"] for row in trend],
                    [float(row["amount"]) for row in trend],
                    name=selected,
                ),
                width="stretch",
            )
        else:
            st.info("No monthly trend data for this merchant.")

    with col2:
        st.subheader("Category Mix")
        category_totals: dict[str, Decimal] = {}
        for txn in expenses:
            category_totals[txn.category] = category_totals.get(txn.category, Decimal("0")) + abs(
                txn.amount_signed
            )
        if category_totals:
            labels = list(category_totals.keys())
            values = [float(value) for value in category_totals.values()]
            fig = px.pie(values=values, names=labels, height=360)
            fig.update_layout(margin={"l": 12, "r": 12, "t": 20, "b": 28})
            st.plotly_chart(style_fig(fig), width="stretch")
        else:
            st.info("No expense category mix available.")

    st.subheader("Transactions")
    render_transaction_table(txns, detailed=False, height=560)


def render(ctx: AppContext) -> None:
    page_header(
        "Categories",
        "Analyze spending by category or merchant without leaving the selected period.",
        status=ctx.period_label,
    )

    navigation = take_brief_context("categories")
    if navigation and navigation.subject_kind in {"category", "merchant"}:
        st.session_state["categories_analysis"] = navigation.subject_kind.title()
    mode = st.segmented_control(
        "Analysis", ["Category", "Merchant"], default="Category", key="categories_analysis"
    )
    snapshot = ReportSnapshotService(ctx.db_path).snapshot_for_period(
        ctx.start_date,
        ctx.end_date,
        source="streamlit_categories",
        persist=False,
    )
    kpis = ctx.service.compute_true_cost_kpis(ctx.start_date, ctx.end_date)
    period_status_strip(
        start_date=ctx.start_date,
        end_date=ctx.end_date,
        row_count=int(kpis["transaction_count"]),
        snapshot=snapshot,
        source="Snapshot + live queries",
    )
    drift = (snapshot or {}).get("payload", {}).get("category_drift", [])
    if drift:
        with st.expander("Category Drift Snapshot"):
            # Same payload, same presentation as Overview's Category Drift table:
            # the raw frame rendered machine headers (change_pct, is_new) and
            # unformatted floats beside every other formatted table on the page.
            st.dataframe(
                pd.DataFrame(drift).rename(
                    columns={
                        "category": "Category",
                        "current": "Current",
                        "previous": "Previous",
                        "change": "Change",
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
                height=240,
            )
    st.divider()

    if mode == "Category":
        _render_category(
            ctx,
            navigation.subject if navigation and navigation.subject_kind == "category" else None,
        )
    else:
        _render_merchant(
            ctx,
            navigation.subject if navigation and navigation.subject_kind == "merchant" else None,
        )
