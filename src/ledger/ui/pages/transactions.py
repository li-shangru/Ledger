"""Transactions page."""

from __future__ import annotations

from decimal import Decimal

import pandas as pd
import plotly.express as px
import streamlit as st

from ledger.config.repository import SQLiteConfigRepository
from ledger.finance_policy import is_cad_row
from ledger.rules import CategorizationService
from ledger.rules.categorization import AutomaticRuleDerivationUnavailable
from ledger.ui.components import (
    CURRENCY_FORMAT,
    download_dataframe,
    format_currency,
    kpi_row,
    metric_row,
    page_header,
    period_status_strip,
    render_transaction_table,
    style_fig,
    truncate,
    truncation_notice,
)
from ledger.ui.context import AppContext
from ledger.ui.navigation import BriefContext, take_brief_context


def _render_linked_evidence(ctx: AppContext) -> bool:
    """Resolve exact imported IDs, including both legs stored as transfers."""
    from ledger.planning.reconciliation import PlanReconciliationService

    navigation = take_brief_context("transactions")
    if navigation and navigation.evidence_ids:
        st.session_state["planning_evidence_context"] = navigation
    context = st.session_state.get("planning_evidence_context")
    if not isinstance(context, BriefContext) or (context.start_date, context.end_date) != (
        ctx.start_date,
        ctx.end_date,
    ):
        st.session_state.pop("planning_evidence_context", None)
        return False
    st.subheader("Imported evidence")
    st.caption(
        f"Supporting records for {ctx.start_date} through {ctx.end_date}; posting-window evidence may fall just outside that period."
    )
    try:
        rows = PlanReconciliationService(ctx.db_path).evidence(list(context.evidence_ids))
    except Exception:
        st.error("Supporting evidence could not be read. Reopen the comparison and retry.")
        return True
    if len(rows) != len(context.evidence_ids):
        st.warning(
            "Some linked evidence is no longer available; reopen the comparison before deciding."
        )
    if rows:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Date": row["date"],
                        "Amount": row["amount"],
                        "Currency": row["currency"],
                        "Event": row["event_type"],
                        "Account": row["account_id"],
                        "Merchant": row["merchant"],
                        "Description": row["description"],
                        "Evidence": row["evidence_id"],
                    }
                    for row in rows
                ]
            ),
            width="stretch",
            hide_index=True,
        )
    if st.button("Show all transactions", key="planning_evidence_clear"):
        st.session_state.pop("planning_evidence_context", None)
        st.rerun()
    return True


def _period_kpis(transactions) -> tuple[Decimal, Decimal, Decimal, int]:
    """Income/Expenses/Net over CAD-only rows, plus the non-CAD row count.

    ``get_transactions`` applies no currency filter, so a non-CAD row stored
    without currency conversion would otherwise be summed at
    par into these CAD-labelled totals -- making the headline Net silently
    disagree with the CAD-only Overview/Insights/Categories KPIs for the same
    window. We sum CAD rows only (mirroring the public CAD policy on the Categories
    page) and return how many non-CAD rows were excluded so the caller can note
    the discrepancy and the raw table reconciles with the KPI row.

    This is the raw transaction explorer, so we filter on CURRENCY only -- an
    ``exclude_from_spending`` CAD expense still counts here (the explorer
    intentionally lists excluded rows), unlike the spend-universe category page.
    """
    income = sum(
        (
            txn.amount_signed
            for txn in transactions
            if is_cad_row(currency=txn.currency) and txn.amount_signed > 0
        ),
        Decimal("0"),
    )
    expenses = sum(
        (
            abs(txn.amount_signed)
            for txn in transactions
            if is_cad_row(currency=txn.currency) and txn.amount_signed < 0
        ),
        Decimal("0"),
    )
    non_cad_count = sum(1 for txn in transactions if not is_cad_row(currency=txn.currency))
    return income, expenses, income - expenses, non_cad_count


def _resolve_amount_bounds(low: int, high: int, cap: int) -> tuple[Decimal | None, Decimal | None]:
    """Translate the abs-amount slider selection into query bounds.

    The slider's extremes mean "no bound": at ``low == 0`` there is no minimum,
    and at ``high == cap`` there is no maximum. This is what keeps the default
    (full-range) selection from silently excluding any row above the cap — the
    bug where a hardcoded $10k ceiling dropped paycheques/rent from the page
    totals and CSV export.
    """
    min_amount = Decimal(str(low)) if low > 0 else None
    max_amount = Decimal(str(high)) if high < cap else None
    return min_amount, max_amount


def _amount_slider_cap(period_max: Decimal) -> int:
    """Pick a slider upper bound that always covers the period's largest row.

    Rounds the period maximum up to the next $500 and never goes below $10k so
    the control stays usable on sparse data.
    """
    import math

    floor_cap = 10_000
    if period_max <= 0:
        return floor_cap
    rounded = int(math.ceil(float(period_max) / 500.0) * 500)
    return max(floor_cap, rounded)


def _filter_transactions(
    transactions, cashflow_filter, day_filter, day_start, day_end, flow_filter
):
    result = transactions
    if cashflow_filter == "Expenses":
        result = [txn for txn in result if txn.amount_signed < 0]
    elif cashflow_filter == "Income":
        result = [txn for txn in result if txn.amount_signed > 0]
    elif cashflow_filter == "Cashflow only":
        result = [txn for txn in result if txn.include_in_cashflow]

    if day_filter:
        day_map = {
            "Monday": 0,
            "Tuesday": 1,
            "Wednesday": 2,
            "Thursday": 3,
            "Friday": 4,
            "Saturday": 5,
            "Sunday": 6,
        }
        allowed = {day_map[day] for day in day_filter}
        result = [txn for txn in result if txn.posting_date.weekday() in allowed]

    result = [txn for txn in result if day_start <= txn.posting_date.day <= day_end]

    if flow_filter:
        result = [txn for txn in result if txn.flow in flow_filter]

    return result


def _render_quality_panels(ctx: AppContext) -> None:
    duplicate_candidates = ctx.service.get_duplicate_candidates(
        threshold_days=2,
        start_date=ctx.start_date,
        end_date=ctx.end_date,
    )
    transfer_stats = ctx.service.get_transfer_stats(ctx.start_date, ctx.end_date)

    col1, col2 = st.columns(2, gap="large")
    with col1:
        st.subheader("Duplicate Candidates")
        if duplicate_candidates:
            st.warning(f"{len(duplicate_candidates)} potential duplicate pairs")
            with st.expander("Inspect duplicate candidates"):
                rows = []
                for idx, (txn1, txn2) in enumerate(duplicate_candidates[:30], 1):
                    rows.append(
                        {
                            "Pair": idx,
                            "Merchant": txn1.merchant or "Unknown",
                            "Amount": float(abs(txn1.amount_signed)),
                            "First Date": txn1.posting_date,
                            "Second Date": txn2.posting_date,
                            "First": truncate(txn1.description, 56),
                            "Second": truncate(txn2.description, 56),
                        }
                    )
                st.dataframe(
                    pd.DataFrame(rows),
                    width="stretch",
                    hide_index=True,
                    column_config={
                        "Amount": st.column_config.NumberColumn("Amount", format=CURRENCY_FORMAT)
                    },
                    height=320,
                )
        else:
            st.caption("No duplicate candidates detected.")

    with col2:
        st.subheader("Transfer Matching")
        if transfer_stats["total_transfers"] > 0:
            # Compare like with like: match_rate is computed on the LEG basis, so
            # rendering it beside matched_PAIRS read as "2 transfers, 1 matched,
            # 100%". Show matched legs next to the rate, and pairs separately.
            metric_row(
                [
                    ("Transfer legs", f"{transfer_stats['total_transfers']:,}", None),
                    (
                        "Matched legs",
                        f"{transfer_stats['matched_transfers']:,}",
                        f"{transfer_stats['matched_pairs']:,} pair(s)",
                    ),
                    ("Match rate", f"{transfer_stats['match_rate']:.1%}", "of legs"),
                ]
            )
            with st.expander("Inspect transfers"):
                view = st.segmented_control(
                    "Transfer view", ["Matched", "Unmatched"], default="Matched"
                )
                if view == "Matched":
                    pairs = ctx.service.get_transfer_matches(ctx.start_date, ctx.end_date)
                    if pairs:
                        rows = []
                        for source, dest in pairs[:50]:
                            rows.append(
                                {
                                    "Date": source.posting_date,
                                    "Amount": float(abs(source.amount_signed)),
                                    "From": f"{source.institution} ...{source.account[-4:]}",
                                    "To": f"{dest.institution} ...{dest.account[-4:]}",
                                    "Confidence": float(source.transfer_confidence or 0) * 100,
                                }
                            )
                        st.dataframe(
                            pd.DataFrame(rows),
                            width="stretch",
                            hide_index=True,
                            column_config={
                                "Date": st.column_config.DateColumn("Date", format="YYYY-MM-DD"),
                                "Amount": st.column_config.NumberColumn(
                                    "Amount", format=CURRENCY_FORMAT
                                ),
                                "Confidence": st.column_config.NumberColumn(
                                    "Confidence", format="%.0f%%"
                                ),
                            },
                            height=320,
                        )
                    else:
                        st.info("No matched transfers in this period.")
                else:
                    unmatched = ctx.service.get_unmatched_transfers(ctx.start_date, ctx.end_date)
                    if unmatched:
                        df = pd.DataFrame(
                            [
                                {
                                    "Date": txn.posting_date,
                                    "Institution": txn.institution,
                                    "Account": f"...{txn.account[-4:]}",
                                    "Amount": float(txn.amount_signed),
                                    "Description": truncate(txn.description, 64),
                                }
                                for txn in unmatched
                            ]
                        )
                        st.dataframe(
                            df,
                            width="stretch",
                            hide_index=True,
                            column_config={
                                "Date": st.column_config.DateColumn("Date", format="YYYY-MM-DD"),
                                "Amount": st.column_config.NumberColumn(
                                    "Amount", format=CURRENCY_FORMAT
                                ),
                            },
                            height=320,
                        )
                    else:
                        st.success("All transfers are matched for this period.")
        else:
            st.caption("No transfers detected.")


def _txn_label(txn: object) -> str:
    merchant = getattr(txn, "merchant", "") or getattr(txn, "description", "") or ""
    amount = abs(getattr(txn, "amount_signed", 0))
    return f"{getattr(txn, 'posting_date', '')} · {merchant[:36]} · {getattr(txn, 'category', '')} · ${amount:,.2f}"


def _render_recategorize(ctx: AppContext, transactions: list) -> None:
    """Fix a category, optionally remembering it as a reusable merchant rule."""
    if not transactions:
        return

    with st.expander("✏️ Fix a category"):
        success_message = st.session_state.pop("recat_success_message", None)
        if isinstance(success_message, str):
            st.success(success_message)
        repo = SQLiteConfigRepository(ctx.db_path)
        all_categories = repo.load_categories()
        parent_names = all_categories.referenced_parent_names()
        category_names = [c.name for c in all_categories.categories if c.name not in parent_names]
        if not category_names:
            st.info("No categories configured yet — set them up on the Rules / Settings page.")
            return

        options = transactions[:300]
        if len(transactions) > 300:
            st.caption(
                f"Showing the first 300 of {len(transactions):,}. Narrow with the filters "
                "above to reach a specific transaction."
            )
        by_id = {txn.txn_id: txn for txn in options}
        selected_id = st.selectbox(
            "Transaction",
            list(by_id.keys()),
            format_func=lambda tid: _txn_label(by_id[tid]),
            key="recat_txn",
        )
        txn = by_id[selected_id]
        st.caption(
            f"Currently **{txn.category}**"
            + (f" · merchant **{txn.merchant}**" if txn.merchant else " · merchant not recognized")
        )

        default_idx = category_names.index(txn.category) if txn.category in category_names else 0
        new_category = st.selectbox(
            "Correct category", category_names, index=default_idx, key="recat_cat"
        )
        svc = CategorizationService(ctx.db_path)
        mode = st.radio(
            "Scope",
            ["Just this transaction", "Remember for this merchant"],
            horizontal=True,
            key="recat_mode",
            help="'Remember' creates a merchant rule and re-applies it to every matching "
            "transaction, so you don't re-fix this merchant on the next import.",
        )

        if mode == "Just this transaction":
            if st.button("Apply", type="primary", key="recat_apply_one"):
                try:
                    updated = svc.set_transaction_category(txn.txn_id, new_category)
                except ValueError as exc:
                    st.error(f"Could not set category: {exc}")
                    return
                if updated:
                    st.cache_data.clear()
                    st.session_state["recat_success_message"] = f"Set to {new_category}."
                    st.rerun()
                else:
                    st.error("Transaction not found.")
            return

        try:
            suggestion = svc.suggest_for_transaction(txn.txn_id)
        except AutomaticRuleDerivationUnavailable as exc:
            suggestion = None
            if exc.reason == "missing_raw":
                st.info(
                    "Automatic pattern derivation is unavailable because this row has no raw "
                    "statement evidence. Enter a Merchant and a regex derived from raw statement "
                    "evidence before saving."
                )
            else:
                st.info(
                    "Automatic pattern derivation is unavailable because the raw e-transfer "
                    "evidence does not identify a counterparty. Enter a Merchant and a regex "
                    "derived from raw statement evidence before saving."
                )
        col_m, col_p = st.columns(2)
        with col_m:
            merchant_norm = st.text_input(
                "Merchant",
                value=suggestion.merchant_norm if suggestion else "",
                key="recat_merchant",
            )
        with col_p:
            pattern = st.text_input(
                "Match pattern (regex)",
                value=suggestion.pattern if suggestion else "",
                key="recat_pattern",
                help=(
                    "At import and during a rules pass, the regex is searched in merchant_raw, "
                    "description, and their concatenation. The resolved merchant is compared "
                    "by exact identity with the Merchant field."
                ),
            )

        preview_error: str | None = None
        match_count: int | None = None
        preview_stored_excluded: bool | None = None
        preview_effective_excluded: bool | None = None
        if pattern.strip():
            try:
                preview = svc.preview_rule_update(
                    pattern,
                    merchant_norm=merchant_norm.strip(),
                    category=new_category,
                )
                match_count = preview.transactions_updated
                preview_stored_excluded = preview.stored_merchant_exclude_from_spending
                preview_effective_excluded = preview.effective_exclude_from_spending
            except ValueError as exc:
                preview_error = str(exc)

        if preview_error:
            st.warning(f"Invalid pattern: {preview_error}")
        elif match_count is not None:
            st.caption(
                f"This taught rule would update **{match_count}** non-manual transaction(s) "
                f"to **{new_category}**. "
                + (
                    "The merchant rule stores excluded from spending; "
                    if preview_stored_excluded
                    else "The merchant rule stores included in spending; "
                )
                + (
                    "the merchant rule makes the taught rows effectively excluded from "
                    "spending totals. "
                    if preview_stored_excluded
                    else (
                        "the target category makes them effectively excluded from spending totals. "
                        if preview_effective_excluded
                        else "the taught rows are effectively included in spending totals. "
                    )
                )
                + "Saving also re-applies the full ruleset; unrelated outdated rows may "
                "change separately."
            )

        disabled = not (merchant_norm.strip() and pattern.strip()) or preview_error is not None
        if st.button("Save rule & re-apply", type="primary", disabled=disabled, key="recat_save"):
            try:
                with st.spinner("Saving rule and re-applying across all transactions…"):
                    result = svc.remember_category(
                        merchant_norm=merchant_norm.strip(),
                        pattern=pattern.strip(),
                        category=new_category,
                    )
            except ValueError as exc:
                st.error(f"Could not save rule: {exc}")
                return
            if not result.ok:
                st.error(
                    result.validation_issues[0]
                    if result.validation_issues
                    else "Could not save rule."
                )
                return
            st.cache_data.clear()
            taught_updated = int(result.details.get("taught_transactions_updated", 0))
            taught_stored_excluded = bool(
                result.details.get("taught_stored_merchant_exclude_from_spending", False)
            )
            taught_effective_excluded = bool(
                result.details.get("taught_effective_exclude_from_spending", False)
            )
            success_message = (
                f"Saved. This rule updated {taught_updated} transaction(s) to {new_category}; "
                + (
                    "the merchant rule stores excluded from spending, "
                    if taught_stored_excluded
                    else "the merchant rule stores included in spending, "
                )
                + (
                    "and the merchant rule makes the taught rows effectively excluded from "
                    "spending totals. "
                    if taught_stored_excluded
                    else (
                        "and the target category makes them effectively excluded from spending "
                        "totals. "
                        if taught_effective_excluded
                        else "so the taught rows are effectively included in spending totals. "
                    )
                )
                + f"The full rules pass updated {result.transactions_updated} transaction(s) total."
            )
            st.session_state["recat_success_message"] = success_message
            st.rerun()


def render(ctx: AppContext) -> None:
    page_header(
        "Transactions",
        "Search, inspect, export, and audit transaction quality.",
        status=ctx.period_label,
    )
    if _render_linked_evidence(ctx):
        return

    c1, c2, c3 = st.columns([0.48, 0.22, 0.30])
    with c1:
        search = st.text_input("Search", placeholder="Description or merchant")
    with c2:
        abs_cap = _amount_slider_cap(ctx.service.get_max_abs_amount(ctx.start_date, ctx.end_date))
        amount_range = st.slider("Abs amount", 0, abs_cap, (0, abs_cap), step=25)
    with c3:
        cashflow_filter = st.segmented_control(
            "Type",
            ["All", "Expenses", "Income", "Cashflow only"],
            default="All",
            wrap=True,
        )

    with st.expander("Advanced filters"):
        a1, a2, a3 = st.columns(3)
        with a1:
            day_filter = st.multiselect(
                "Day of week",
                ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
                placeholder="All days",
            )
        with a2:
            day_start, day_end = st.slider("Day of month", 1, 31, (1, 31))
        with a3:
            flow_filter = st.multiselect(
                "Flow",
                [
                    "expense",
                    "income",
                    "fee",
                    "interest",
                    "reward",
                    "payment",
                    "transfer",
                    "etransfer",
                ],
                placeholder="All flows",
            )

    min_amount, max_amount = _resolve_amount_bounds(amount_range[0], amount_range[1], abs_cap)
    _txn_row_cap = 50000
    transactions = ctx.service.get_transactions(
        start_date=ctx.start_date,
        end_date=ctx.end_date,
        description_search=search or None,
        min_amount=min_amount,
        max_amount=max_amount,
        limit=_txn_row_cap,
        order_by="date_desc",
    )
    _truncation = truncation_notice(len(transactions), _txn_row_cap)
    transactions = _filter_transactions(
        transactions,
        cashflow_filter,
        day_filter,
        day_start,
        day_end,
        flow_filter,
    )

    income, expenses, net, non_cad_count = _period_kpis(transactions)
    # Deliberately labelled "Raw": these are the totals of the rows LISTED below --
    # no offsets applied, no categories excluded -- so they reconcile against a bank
    # statement. Overview reports the adjusted (spending) view of the same period and
    # will differ; identical labels on both pages made that look like a bug.
    kpi_row(
        [
            ("Results", f"{len(transactions):,}", None),
            ("Raw Inflows", format_currency(income), "as listed", "pos"),
            ("Raw Outflows", format_currency(expenses), "as listed", "neg"),
            (
                "Raw Net",
                format_currency(net),
                "no offsets applied",
                "pos" if net >= 0 else "neg",
            ),
        ]
    )
    st.caption(
        "Totals of every row listed below, with no offsets applied and no categories "
        "excluded — use these to reconcile against a statement. Overview reports the "
        "adjusted spending view of the same period, so the two will differ."
    )
    if non_cad_count:
        st.caption(
            f"{non_cad_count:,} non-CAD transaction(s) are listed below but excluded "
            "from these totals (amounts are stored un-FX-converted)."
        )
    if _truncation:
        st.caption(_truncation)
    period_status_strip(
        start_date=ctx.start_date,
        end_date=ctx.end_date,
        row_count=len(transactions),
        source="Live queries",
    )

    st.divider()

    view = st.segmented_control("View", ["Table", "Timeline", "Quality"], default="Table")
    if view == "Quality":
        _render_quality_panels(ctx)
        return

    if view == "Timeline":
        if not transactions:
            st.info("No transactions match the current filters.")
            return
        df = pd.DataFrame(
            [
                {
                    "Date": txn.posting_date,
                    "Amount": float(abs(txn.amount_signed)),
                    "Category": txn.category,
                    "Merchant": txn.merchant or "Unknown",
                    "Type": "Income" if txn.amount_signed > 0 else "Expense",
                }
                for txn in transactions
            ]
        )
        fig = px.scatter(
            df,
            x="Date",
            y="Amount",
            color="Category",
            size="Amount",
            hover_data=["Merchant", "Type"],
            height=620,
        )
        fig.update_layout(hovermode="closest")
        st.plotly_chart(style_fig(fig), width="stretch")
        return

    detailed = st.toggle("Show detailed columns", value=False)
    df = render_transaction_table(transactions, detailed=detailed, height=620)

    export_col1, export_col2 = st.columns([0.55, 0.45])
    with export_col1:
        categories = sorted({txn.category for txn in transactions if txn.category})
        selected_categories = st.multiselect(
            "Export categories",
            options=categories,
            placeholder="Optional category subset",
        )
    export_df = df
    if selected_categories and not df.empty:
        export_df = df[df["Category"].isin(selected_categories)]
    with export_col2:
        download_dataframe(
            f"Download CSV ({len(export_df):,})",
            export_df,
            f"transactions_{ctx.start_date}_{ctx.end_date}.csv",
        )

    st.divider()
    _render_recategorize(ctx, transactions)
