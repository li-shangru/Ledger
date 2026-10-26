"""Compare preserved schedule expectations with imported evidence for one period."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from ledger.planning.cash_plan import schedule_occurrence_dates
from ledger.planning.discovery import local_reference
from ledger.planning.reconciliation import PlanReconciliationService
from ledger.proposals import ProposalService
from ledger.ui.components import page_header
from ledger.ui.context import AppContext
from ledger.ui.navigation import BriefContext, register_brief_link, take_brief_context
from ledger.ui.pages.proposals import _failure


def _finding_label(finding: dict[str, Any]) -> str:
    return f"{finding['expected_date']} · {finding['name']} · {finding['status'].replace('_', ' ')}"


def _review_displayed_finding(
    db_path: Path, start: date, end: date, as_of: date, occurrence_id: str, material_revision: str
) -> None:
    """Run with the previous rendered finding before Streamlit rebuilds widgets."""
    try:
        PlanReconciliationService(db_path).mark_reviewed(
            start,
            end,
            as_of=as_of,
            occurrence_id=occurrence_id,
            material_revision=material_revision,
        )
    except Exception as exc:
        st.session_state["plan_review_outcome"] = (False, str(exc))
    else:
        st.session_state["plan_review_outcome"] = (
            True,
            "Exception reviewed. Changed material evidence will reopen it.",
        )


def _future_correction(
    ctx: AppContext, service: PlanReconciliationService, finding: dict[str, Any], as_of: date
) -> None:
    if finding["status"] not in {
        "amount_difference",
        "date_difference",
        "amount_and_date_difference",
    }:
        return
    try:
        current = service.current_schedule_for_finding(
            ctx.start_date,
            ctx.end_date,
            as_of=as_of,
            occurrence_id=finding["occurrence_id"],
            material_revision=finding["material_revision"],
        )
        target = local_reference(ctx.db_path, "schedule", current.schedule_id)
    except ValueError, KeyError:
        st.info(
            "A newer schedule definition or incomplete history prevents a direct correction from this finding. Review the current schedule before proposing an update."
        )
        return
    if current.frequency == "once":
        st.caption(
            "This was a one-time expectation. Create a separate future schedule proposal if the activity will recur."
        )
        return
    tomorrow = date.today() + timedelta(days=1)
    through = tomorrow + timedelta(days=min(2192, (date.max - tomorrow).days))
    next_dates = schedule_occurrence_dates(current.next_date, current.frequency, tomorrow, through)
    suggested_date = next_dates[0] if next_dates else tomorrow
    shift = int(finding["date_difference"] or 0)
    for candidate in next_dates:
        shifted = candidate + timedelta(days=shift)
        if shifted >= tomorrow:
            suggested_date = shifted
            break
    st.subheader("Propose a future correction")
    st.caption(
        "Filing saves a proposal. Apply happens separately after reviewing its exact local preview. A posting-date difference does not by itself change the schedule's due date."
    )
    store = Path(
        st.text_input(
            "Proposal store",
            value=str(ctx.db_path.parent / "proposals.db"),
            key="planning_proposal_store",
        )
    ).expanduser()
    key = f"plan_future_{finding['occurrence_id']}_{finding['material_revision']}"
    with st.form(key):
        use_amount = st.checkbox("Change future amount", value=bool(finding["amount_changed"]))
        amount = st.text_input("Future amount (CAD)", value=str(finding["actual_amount"]))
        use_date = st.checkbox("Change next occurrence date", value=False)
        next_date = st.date_input("Next occurrence date", value=suggested_date, min_value=tomorrow)
        effective = st.date_input("Effective from", value=tomorrow, min_value=tomorrow)
        submitted = st.form_submit_button("Update future schedule")
    if submitted:
        changes = {}
        if use_amount:
            changes["amount"] = amount.strip()
        if use_date:
            changes["next_date"] = next_date.isoformat()
        if not changes:
            st.error("Select the amount or next occurrence date to change.")
            return
        try:
            fresh = service.current_schedule_for_finding(
                ctx.start_date,
                ctx.end_date,
                as_of=as_of,
                occurrence_id=finding["occurrence_id"],
                material_revision=finding["material_revision"],
            )
            if (
                fresh != current
                or local_reference(ctx.db_path, "schedule", current.schedule_id) != target
            ):
                raise ValueError(
                    "The schedule changed. Refresh this finding before filing a correction."
                )
            proposal = ProposalService(store).file_planning_change(
                {
                    "operation": "schedule_update",
                    "target": target,
                    "effective_date": effective.isoformat(),
                    "changes": changes,
                }
            )
        except Exception as exc:
            _failure(exc)
            return
        st.session_state[key + "_filed"] = proposal.proposal_id
        st.success(
            "Proposal filed. The schedule is unchanged until you apply its reviewed preview."
        )
    filed = st.session_state.get(key + "_filed")
    pages = st.session_state.get("ledger_pages", {})
    if filed and "proposals" in pages:
        context = BriefContext(
            "proposals", ctx.start_date, ctx.end_date, "proposal", filed, (), as_of
        )
        st.page_link(
            pages["proposals"],
            label="Open filed proposal",
            query_params=register_brief_link(context),
        )


def render(ctx: AppContext) -> None:
    page_header(
        "Plan versus actual",
        "Compare confirmed expectations with imported activity.",
        status=ctx.period_label,
    )
    navigation = take_brief_context("plan-reconciliation")
    period_key = (str(ctx.db_path.resolve()), ctx.start_date, ctx.end_date)
    if st.session_state.get("plan_reconciliation_period") != period_key:
        st.session_state["plan_reconciliation_period"] = period_key
        for key in (
            "plan_reconciliation_as_of",
            "plan_reconciliation_page",
            "plan_reconciliation_selected",
        ):
            st.session_state.pop(key, None)
    if navigation and navigation.as_of:
        st.session_state["plan_reconciliation_as_of"] = navigation.as_of
    as_of = st.date_input("As of", value=ctx.end_date, key="plan_reconciliation_as_of")
    st.caption(
        f"Reporting period: {ctx.start_date.isoformat()} through {ctx.end_date.isoformat()}."
    )
    service = PlanReconciliationService(ctx.db_path)
    outcome = st.session_state.pop("plan_review_outcome", None)
    if outcome:
        (st.success if outcome[0] else st.error)(outcome[1])
    if (ctx.end_date - ctx.start_date).days >= 366:
        st.info("Choose a period of at most 366 days to compare plans and imported activity.")
        with st.expander("Coverage and matching assumptions"):
            st.write("The selected reporting period is wider than the supported comparison window.")
        return
    try:
        requested_page = int(st.session_state.get("plan_reconciliation_page", 1))
        report = service.build(
            ctx.start_date, ctx.end_date, as_of=as_of, limit=100, offset=(requested_page - 1) * 100
        )
    except Exception as exc:
        _failure(exc)
        return
    with st.expander("Coverage and matching assumptions"):
        st.json(
            {
                key: report.get(key)
                for key in ("period", "as_of", "posting_window_days", "disclosures", "assumptions")
            }
        )
    if navigation and navigation.subject_kind == "occurrence":
        # Follow a brief's exact occurrence even when it is not in the first page.
        report = service.build(ctx.start_date, ctx.end_date, as_of=as_of, limit=100)
        while (
            not any(row["occurrence_id"] == navigation.subject for row in report["findings"])
            and report["truncated"]
        ):
            report = service.build(
                ctx.start_date,
                ctx.end_date,
                as_of=as_of,
                limit=100,
                offset=report["offset"] + report["returned_count"],
            )
        st.session_state["plan_reconciliation_selected"] = navigation.subject
        st.session_state["plan_reconciliation_page"] = report["offset"] // 100 + 1
        st.session_state["plan_include_reviewed"] = True
    page_count = max(1, (report["total_count"] + 99) // 100)
    if st.session_state.get("plan_reconciliation_page", 1) > page_count:
        st.session_state["plan_reconciliation_page"] = 1
        report = service.build(ctx.start_date, ctx.end_date, as_of=as_of, limit=100)
    page = int(
        st.number_input(
            "Occurrence page",
            min_value=1,
            max_value=page_count,
            value=report["offset"] // 100 + 1,
            step=1,
            key="plan_reconciliation_page",
        )
    )
    if (page - 1) * 100 != report["offset"]:
        report = service.build(
            ctx.start_date, ctx.end_date, as_of=as_of, limit=100, offset=(page - 1) * 100
        )
    include_reviewed = st.toggle("Include reviewed and matched", key="plan_include_reviewed")
    findings = [
        row
        for row in report["findings"]
        if include_reviewed or (row["actionable"] and not row["reviewed"])
    ]
    if page_count > 1:
        st.caption(
            f"Page {page} of {page_count}; {report['total_count']} occurrences in the reporting period."
        )
    if not findings:
        if report["total_count"]:
            st.info("No unreviewed exceptions on this page.")
        elif report["disclosures"]["incomplete_history_schedule_count"]:
            st.info(
                "No preserved confirmed expectations in this period. "
                "Incomplete schedule history prevents determining what was expected."
            )
        else:
            st.info("No confirmed expectations in this period.")
        return
    by_id = {row["occurrence_id"]: row for row in findings}
    if st.session_state.get("plan_reconciliation_selected") not in by_id:
        if navigation:
            st.info("The linked occurrence is no longer available for this reporting period.")
            return
        st.session_state.pop("plan_reconciliation_selected", None)
    identifier = st.selectbox(
        "Expected occurrence",
        list(by_id),
        format_func=lambda key: _finding_label(by_id[key]),
        key="plan_reconciliation_selected",
    )
    finding = by_id[identifier]
    st.subheader(finding["name"])
    st.caption(
        "Status: "
        + finding["status"].replace("_", " ")
        + (" · Reviewed" if finding["reviewed"] else "")
    )
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Field": "Amount",
                    "Expected": finding["expected_amount"],
                    "Actual": finding["actual_amount"] or "Unavailable",
                },
                {
                    "Field": "Date",
                    "Expected": finding["expected_date"],
                    "Actual": finding["actual_date"] or "Unavailable",
                },
            ]
        ),
        width="stretch",
        hide_index=True,
    )
    if finding["amount_changed"]:
        st.caption(f"Amount changed · {finding['currency']} {finding['amount_difference']}")
    if finding["date_changed"]:
        st.caption(f"Date changed · {finding['date_difference']} day(s)")
    st.write("Matching basis: " + finding["matching_basis"].replace("_", " "))
    if not finding["history_complete"]:
        st.info("This expectation starts from an incomplete historical baseline.")
    with st.expander("Statement coverage and expectation revision"):
        st.json(
            {
                "coverage": finding["coverage"],
                "schedule_revision": finding["schedule_revision"],
                "material_revision": finding["material_revision"],
            }
        )
    pages = st.session_state.get("ledger_pages", {})
    if finding["evidence_ids"] and "transactions" in pages:
        link = BriefContext(
            "transactions",
            ctx.start_date,
            ctx.end_date,
            "occurrence",
            identifier,
            tuple(finding["evidence_ids"][:20]),
            as_of,
        )
        st.page_link(
            pages["transactions"],
            label="Open imported evidence",
            query_params=register_brief_link(link),
        )
    if finding["actionable"] and not finding["reviewed"]:
        st.button(
            "Review unchanged exception",
            key="plan_review_exception",
            on_click=_review_displayed_finding,
            args=(
                ctx.db_path,
                ctx.start_date,
                ctx.end_date,
                as_of,
                identifier,
                finding["material_revision"],
            ),
        )
    _future_correction(ctx, service, finding, as_of)


__all__ = ["render"]
