"""Budgets and savings goals page."""

from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any, TypedDict

import pandas as pd
import streamlit as st

from ledger.planning import BudgetService, PlanningReadinessService
from ledger.planning.cash_plan import (
    CashPlan,
    CashPlanError,
    CashPlanService,
    schedule_occurrence_dates,
)
from ledger.planning.schedules import (
    ScheduleConflictError,
    ScheduleDraft,
    ScheduleNotFoundError,
    ScheduleRecord,
    ScheduleService,
    ScheduleValidationError,
)
from ledger.storage.snapshots import DatabaseTargetLockError, DatabaseWriteCommittedError
from ledger.ui.components import (
    CURRENCY_FORMAT,
    confirmation_ready,
    empty_state,
    format_currency,
    kpi_row,
    page_header,
    period_status_strip,
    status_grid,
)
from ledger.ui.context import AppContext
from ledger.ui.metric_explanations import render_metric_explanations

GOAL_COLUMNS = [
    "name",
    "account",
    "current_amount",
    "current_balance_date",
    "target_amount",
    "target_date",
    "status",
    "notes",
]
BUDGET_SAVE_CONFIRM = "save budget replacements"
GOAL_SAVE_CONFIRM = "save goal replacements"
PLANNING_SAVE_REFUSAL = (
    "Planning changes could not be saved; retry after other database work completes."
)
PLANNING_SAVE_COMMITTED_WARNING = (
    "Planning change commit succeeded, but final validation or cleanup did not complete; "
    "reload and inspect before retrying."
)
SCHEDULE_FREQUENCY_HELP = (
    "Once occurs only on the chosen date. Weekly repeats every 7 days; biweekly every 14 days. "
    "Semimonthly repeats twice a month on two days 15 apart, based on the starting date; "
    "a start on the 15th or month-end uses the 15th and month-end. Monthly, quarterly, and "
    "yearly keep the starting day, using month-end only when that day is unavailable."
)


class _GoalProgressSummary(TypedDict):
    target: Decimal
    current: Decimal | None
    remaining: Decimal | None
    progress_pct: Decimal | None


def _account_label(account_labels: dict[str, str], account_id: str) -> str:
    """Return presentation metadata without leaking a canonical internal ID."""
    return account_labels.get(account_id) or "Unknown account"


def _budget_dataframe(rows: list[dict]) -> pd.DataFrame:
    if rows:
        return pd.DataFrame(
            [
                {
                    "month": row["month"],
                    "category": row["category"],
                    "amount": row["amount"],
                    "notes": row.get("notes", ""),
                }
                for row in rows
            ]
        )
    return pd.DataFrame(
        [
            {"month": "*", "category": "", "amount": 0.0, "notes": ""},
        ]
    )


def _goal_dataframe(
    rows: list[dict],
    account_labels: dict[str, str],
    default_account: str,
) -> pd.DataFrame:
    if rows:
        return pd.DataFrame(
            [
                {
                    "name": row["name"],
                    "account": account_labels.get(
                        row.get("account_id", ""), row.get("account", "")
                    ),
                    "current_amount": row["current_amount"],
                    "current_balance_date": row.get("current_balance_date"),
                    "target_amount": row["target_amount"],
                    "target_date": row["target_date"],
                    "status": row["status"],
                    "notes": row.get("notes", ""),
                }
                for row in rows
            ]
        )
    return pd.DataFrame(
        [
            {
                "name": "",
                "account": default_account,
                "current_amount": None,
                "current_balance_date": None,
                "target_amount": 0.0,
                "target_date": None,
                "status": "active",
                "notes": "",
            }
        ]
    )


def _schedule_dataframe(
    rows: tuple[ScheduleRecord, ...] | list[ScheduleRecord],
    account_labels: dict[str, str],
) -> pd.DataFrame:
    """UI-safe schedule rows with provenance and account direction visible."""
    return pd.DataFrame(
        [
            {
                "Name": row.name,
                "Event": row.event_type,
                "Amount": row.amount,
                "Currency": row.currency,
                "Account": _account_label(account_labels, row.account_id),
                "Destination": (
                    _account_label(account_labels, row.counterparty_account_id)
                    if row.counterparty_account_id
                    else ""
                ),
                "Next date": row.next_date,
                "Frequency": row.frequency,
                "Provenance": row.provenance,
                "Active": row.is_active,
                "Detected link": row.pattern_id or "",
                "Note": row.note,
            }
            for row in rows
        ]
    )


def _cash_plan_leg_rows(plan: CashPlan, account_labels: dict[str, str]) -> list[dict]:
    return [
        {
            "Date": leg.occurrence_date,
            "Account": _account_label(account_labels, leg.account_id),
            "Event": leg.event_type,
            "Name": leg.name,
            "Amount": leg.amount,
            "Provenance": leg.provenance,
            "Source": leg.source_kind,
            "Spending": "yes" if leg.is_spending else "no",
        }
        for leg in plan.planning_legs
        if leg.in_selected_period
    ]


def _cash_plan_fact_rows(plan: CashPlan, account_labels: dict[str, str]) -> list[dict]:
    """Aggregate imported facts for presentation without hiding their exact net effect."""
    groups: dict[tuple[date, str, str, bool], dict[str, Any]] = {}
    for leg in plan.fact_legs:
        label = _account_label(account_labels, leg.account_id)
        key = (leg.occurrence_date, label, leg.event_type, leg.is_spending)
        group = groups.setdefault(
            key,
            {
                "Date": leg.occurrence_date,
                "Account": label,
                "Event": leg.event_type,
                "Imported rows": 0,
                "Net movement": Decimal("0.00"),
                "Spending": "yes" if leg.is_spending else "no",
            },
        )
        group["Imported rows"] += 1
        group["Net movement"] += leg.amount
    return [groups[key] for key in sorted(groups)]


def _cash_plan_account_rows(plan: CashPlan, account_labels: dict[str, str]) -> list[dict]:
    rows = []
    for account in plan.accounts:
        if account.anchor.balance_date is not None:
            anchor = f"{account.anchor.balance_date.isoformat()} statement"
        elif account.anchor.status == "no_anchor_on_or_before_period":
            anchor = "No statement anchor by period start"
        else:
            anchor = "No statement anchor"
        rows.append(
            {
                "Account": _account_label(account_labels, account.account_id),
                "Anchor": anchor,
                "Anchor balance": account.anchor.balance,
                "Imported roll-forward": account.actual_roll_forward_movement,
                "Opening at as-of": account.opening_projected_balance,
                "Planned movement": account.planned_movement,
                "Projected balance": account.projected_balance,
                "Lowest balance": account.lowest_projected_balance,
                "Lowest date": account.lowest_projected_date,
            }
        )
    return rows


def _apply_goal_editor_state(df: pd.DataFrame, editor_state: object) -> pd.DataFrame:
    if not isinstance(editor_state, dict):
        return df.reindex(columns=GOAL_COLUMNS)

    hydrated = df.reindex(columns=GOAL_COLUMNS).copy()
    for row_index, changes in (editor_state.get("edited_rows") or {}).items():
        index = int(row_index)
        if index not in hydrated.index:
            continue
        for column, value in changes.items():
            if column in hydrated.columns:
                hydrated.at[index, column] = value

    added_rows = editor_state.get("added_rows") or []
    if added_rows:
        # Append only added rows not already present, so applying the same editor
        # state twice is idempotent (guards against a double-apply duplicating rows
        # if this helper is ever re-fed an already-applied frame). Compare on each
        # added row's own specified fields (the editor only carries the cells the
        # user touched), against the existing frame rows restricted to those same
        # columns — so a previously-appended row (now carrying NaN fillers in the
        # untouched columns) still matches.
        existing_records = hydrated.to_dict("records")
        fresh = []
        for candidate in added_rows:
            keys = [column for column in candidate if column in GOAL_COLUMNS]
            signature = tuple(candidate[column] for column in keys)
            already = any(
                tuple(record.get(column) for column in keys) == signature
                for record in existing_records
            )
            if not already:
                fresh.append(candidate)
                existing_records.append(candidate)
        if fresh:
            hydrated = pd.concat(
                [hydrated, pd.DataFrame(fresh).reindex(columns=GOAL_COLUMNS)],
                ignore_index=True,
            )

    deleted_rows = {int(index) for index in editor_state.get("deleted_rows") or []}
    if deleted_rows:
        hydrated = hydrated.drop(
            index=[index for index in deleted_rows if index in hydrated.index]
        ).reset_index(drop=True)

    return hydrated.reindex(columns=GOAL_COLUMNS)


def _with_goal_account_balances(
    df: pd.DataFrame,
    account_balances: dict[str, dict],
) -> pd.DataFrame:
    hydrated = df.reindex(columns=GOAL_COLUMNS).copy()
    hydrated["current_amount"] = hydrated["account"].map(
        lambda account: account_balances.get(str(account or ""), {}).get("current_amount")
    )
    hydrated["current_balance_date"] = hydrated["account"].map(
        lambda account: account_balances.get(str(account or ""), {}).get("current_balance_date")
    )
    return hydrated[GOAL_COLUMNS]


def _goal_progress_summary(active_goals: list[dict]) -> _GoalProgressSummary:
    """Summarize progress only when every active goal has an absolute balance."""
    target = sum((goal["target_amount"] for goal in active_goals), Decimal("0"))
    if active_goals and any(goal.get("current_amount") is None for goal in active_goals):
        return {
            "target": target,
            "current": None,
            "remaining": None,
            "progress_pct": None,
        }
    current = sum((goal["current_amount"] for goal in active_goals), Decimal("0"))
    return {
        "target": target,
        "current": current,
        "remaining": target - current,
        "progress_pct": (current / target * Decimal("100")) if target else Decimal("0"),
    }


def _planning_records(rows: list[dict] | pd.DataFrame) -> list[dict]:
    if isinstance(rows, pd.DataFrame):
        return rows.to_dict("records")
    return rows


def _meaningful_planning_rows(rows: list[dict] | pd.DataFrame, kind: str) -> list[dict]:
    records = _planning_records(rows)
    if kind == "budget":
        return [row for row in records if str(row.get("category") or "").strip()]
    if kind == "goal":
        return [row for row in records if str(row.get("name") or "").strip()]
    raise ValueError(f"Unsupported planning save kind: {kind}")


def _planning_save_preview(
    original_rows: list[dict] | pd.DataFrame,
    edited_rows: list[dict] | pd.DataFrame,
    kind: str,
) -> dict:
    original = _meaningful_planning_rows(original_rows, kind)
    edited = _meaningful_planning_rows(edited_rows, kind)
    errors = []
    if kind == "goal":
        for row in edited:
            if not str(row.get("account_id") or row.get("account") or "").strip():
                errors.append(f"Goal account is required: {row.get('name')}")

    # Identity-based diff, not length arithmetic: deleting one row and adding
    # another previewed as "0 creates / 0 deletes", so the confirmation the user
    # typed described a change the save was not going to make.
    def _identity(row: dict) -> tuple:
        if kind == "goal":
            return (str(row.get("name") or "").strip(),)
        return (
            str(row.get("month") or "").strip(),
            str(row.get("category") or "").strip(),
        )

    original_keys = Counter(_identity(row) for row in original)
    edited_keys = Counter(_identity(row) for row in edited)
    creates = sum((edited_keys - original_keys).values())
    deletes = sum((original_keys - edited_keys).values())
    return {
        "rows": len(edited),
        "creates": creates,
        "deletes": deletes,
        "requires_confirmation": len(original) > 0,
        "errors": errors,
    }


def _planning_save_ready(
    state: Any,  # Streamlit SessionStateProxy (dict-like) or a plain dict in tests
    key: str,
    expected: str,
    preview: dict,
) -> bool:
    if int(preview.get("rows", 0)) == 0 or preview.get("errors"):
        return False
    if not preview.get("requires_confirmation"):
        return True
    return confirmation_ready(state, key, expected)


def _planning_empty_messages(summary: dict) -> list[str]:
    messages = []
    if summary["budgets"]["active"] == 0:
        messages.append("No budgets are configured yet.")
    if summary["goals"]["active"] == 0:
        messages.append("No savings goals are configured yet.")
    return messages


def _emergency_fund_detail(health: dict, basis: dict) -> str | None:
    """Why the buffer figure is what it is, and how old the score behind it is.

    This tile reads the same stored score the Overview does, and used to present
    it with no date at all -- so a score frozen weeks back by sparse recent data
    rendered here as a current statement about the buffer. The as-of fact comes
    from `health_basis`, which is built once per summary and shared with every
    other consumer.
    """
    # emergency_fund_months is None when no explicit absolute row exists.
    unassessable = "Needs an absolute statement value" if health else None
    reason = unassessable if not health or health.get("emergency_fund_months") is None else None
    score_detail = basis.get("score_detail") if health else None
    parts = [part for part in (score_detail, reason) if part]
    return " · ".join(parts) if parts else None


def _render_planning_readiness(summary: dict) -> None:
    health = summary["health"] or {}
    basis = summary.get("health_basis") or {}
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
                "detail": (
                    format_currency(summary["goals"]["remaining"])
                    if summary["goals"]["remaining"] is not None
                    else "Needs absolute value"
                ),
            },
            {
                "label": "Recurring Monthly",
                "value": format_currency(summary["recurring_monthly"]),
                "detail": None,
            },
            {
                # emergency_fund_months is None when no explicit absolute row exists.
                "label": "Emergency Fund",
                "value": (
                    (
                        f"{float(health['emergency_fund_months']):.1f} months"
                        if health.get("emergency_fund_months") is not None
                        else "N/A"
                    )
                    if health
                    else "n/a"
                ),
                "detail": _emergency_fund_detail(health, basis),
            },
        ]
    )
    if basis.get("statement"):
        st.caption(basis["statement"])
    empty_messages = _planning_empty_messages(summary)
    if empty_messages:
        st.info(" ".join(empty_messages))
    if summary["next_actions"]:
        st.dataframe(
            pd.DataFrame({"Readiness Gap": summary["next_actions"]}),
            width="stretch",
            hide_index=True,
            height=min(180, 48 + len(summary["next_actions"]) * 35),
        )


def _render_schedule_date_preview(next_date: date, frequency: str) -> None:
    """Show a bounded preview of the latest submitted form values without a write."""
    preview_days = {
        "once": 0,
        "weekly": 35,
        "biweekly": 70,
        "semimonthly": 93,
        "monthly": 155,
        "quarterly": 465,
        "yearly": 1830,
    }
    try:
        end_date = date.fromordinal(
            min(date.max.toordinal(), next_date.toordinal() + preview_days[frequency])
        )
        occurrences = schedule_occurrence_dates(
            next_date, frequency, next_date, end_date, max_occurrences=12
        )[:6]
    except CashPlanError, OverflowError, ValueError:
        st.warning("Dates could not be previewed. Check the next date and frequency.")
        return
    st.markdown("**Upcoming dates**\n\n" + " · ".join(day.isoformat() for day in occurrences))
    st.caption(
        f"Preview: {frequency} from {next_date.isoformat()}. "
        "Select Preview dates after changing the date or frequency."
    )


def _render_cash_planning(ctx: AppContext, budget_service: BudgetService) -> None:
    schedule_service = ScheduleService(ctx.db_path, prepare_schema=False)
    account_rows = budget_service.list_goal_accounts()
    account_labels = {str(row["account_id"]): str(row["label"]) for row in account_rows}
    account_ids_by_label = {label: account_id for account_id, label in account_labels.items()}
    labels = list(account_ids_by_label)
    schedules = schedule_service.list()
    database_scope = sha256(str(ctx.db_path.expanduser().resolve()).encode()).hexdigest()
    add_form_key = f"cash_schedule_add_{database_scope}"
    conflict_notice_key = f"cash_schedule_conflict_{database_scope}"

    st.subheader("Schedules")
    if st.session_state.pop(conflict_notice_key, False):
        st.warning(
            "That schedule changed or was removed before your action could be applied. "
            "The latest saved schedules have been reloaded. Review them before retrying."
        )
    st.caption(
        "Schedules are planning inputs. Confirmed, hypothetical, and detected sources stay "
        "labelled separately; only confirmed schedules replace a linked detected pattern."
    )
    schedule_df = _schedule_dataframe(schedules, account_labels)
    if schedule_df.empty:
        empty_state(
            "No cash schedules are configured yet.",
            "Add a confirmed or hypothetical income, bill, card payment, or transfer below.",
        )
    else:
        st.dataframe(
            schedule_df,
            width="stretch",
            hide_index=True,
            column_config={
                "Amount": st.column_config.NumberColumn("Amount", format=CURRENCY_FORMAT),
                "Next date": st.column_config.DateColumn("Next date", format="YYYY-MM-DD"),
            },
        )

    with st.expander("Add schedule", expanded=not schedules):
        with st.form(add_form_key):
            add_name = st.text_input("Name", key=f"{add_form_key}_name")
            add_event = st.selectbox(
                "Event type",
                ["bill", "income", "card_payment", "transfer"],
                key=f"{add_form_key}_event",
            )
            add_amount = st.number_input(
                "Amount (CAD)",
                min_value=0.0,
                step=0.01,
                format="%.2f",
                key=f"{add_form_key}_amount",
            )
            add_account = st.selectbox(
                "Account",
                labels,
                key=f"{add_form_key}_account",
                disabled=not labels,
            )
            add_destination = st.selectbox(
                "Destination account",
                ["No destination", *labels],
                key=f"{add_form_key}_destination",
                help="Required for card payments and transfers; card payments must end at a card.",
            )
            add_date = st.date_input(
                "Next date",
                value=max(date.today(), ctx.end_date),
                min_value=date.min,
                max_value=date.max,
                key=f"{add_form_key}_date",
            )
            add_frequency = st.selectbox(
                "Frequency",
                ["once", "weekly", "biweekly", "semimonthly", "monthly", "quarterly", "yearly"],
                key=f"{add_form_key}_frequency",
                help=SCHEDULE_FREQUENCY_HELP,
            )
            add_provenance = st.selectbox(
                "Provenance",
                ["hypothetical", "confirmed"],
                key=f"{add_form_key}_provenance",
            )
            add_note = st.text_input("Note", key=f"{add_form_key}_note")
            st.form_submit_button(
                "Preview dates", help="Refresh the upcoming dates without saving a schedule."
            )
            add_submitted = st.form_submit_button(
                "Add Schedule", type="primary", disabled=not labels
            )
        _render_schedule_date_preview(add_date, add_frequency)
        if add_submitted:
            try:
                schedule_service.create(
                    ScheduleDraft(
                        name=add_name,
                        event_type=add_event,
                        amount=Decimal(str(add_amount)),
                        currency="CAD",
                        account_id=account_ids_by_label.get(str(add_account), ""),
                        counterparty_account_id=(
                            account_ids_by_label.get(str(add_destination))
                            if add_destination != "No destination"
                            else None
                        ),
                        next_date=add_date,
                        frequency=add_frequency,
                        provenance=add_provenance,
                        note=add_note,
                    )
                )
            except ScheduleValidationError as exc:
                st.error(str(exc))
            except DatabaseWriteCommittedError:
                st.error(PLANNING_SAVE_COMMITTED_WARNING)
            except DatabaseTargetLockError, OSError, sqlite3.Error:
                st.error(PLANNING_SAVE_REFUSAL)
            else:
                st.success("Schedule added.")
                st.rerun()

    if schedules:
        schedules_by_id = {row.schedule_id: row for row in schedules}
        schedule_labels = {
            row.schedule_id: f"{row.name} · {row.next_date.isoformat()} · {row.provenance}"
            for row in schedules
        }
        selected_schedule_id = st.selectbox(
            "Schedule to edit",
            list(schedules_by_id),
            format_func=schedule_labels.__getitem__,
            key=f"cash_schedule_selected_{database_scope}",
        )
        selected_schedule = schedules_by_id[selected_schedule_id]
        # All fields and submit controls belong to one database, record, and saved
        # revision. Ordinary reruns keep drafts; selecting or reloading a changed
        # record creates fresh widgets instead of retargeting the previous draft.
        edit_scope = sha256(repr((database_scope, selected_schedule)).encode()).hexdigest()
        edit_form_key = f"cash_schedule_edit_{edit_scope}"
        with st.expander("Edit selected schedule"):
            selected_account_label = _account_label(account_labels, selected_schedule.account_id)
            destination_label = (
                _account_label(account_labels, selected_schedule.counterparty_account_id)
                if selected_schedule.counterparty_account_id
                else "No destination"
            )
            with st.form(edit_form_key):
                edit_name = st.text_input(
                    "Name", value=selected_schedule.name, key=f"{edit_form_key}_name"
                )
                event_options = ["bill", "income", "card_payment", "transfer"]
                edit_event = st.selectbox(
                    "Event type",
                    event_options,
                    index=event_options.index(selected_schedule.event_type),
                    key=f"{edit_form_key}_event",
                )
                edit_amount = st.number_input(
                    "Amount (CAD)",
                    min_value=0.0,
                    value=float(selected_schedule.amount),
                    step=0.01,
                    format="%.2f",
                    key=f"{edit_form_key}_amount",
                )
                edit_account = st.selectbox(
                    "Account",
                    labels,
                    index=labels.index(selected_account_label)
                    if selected_account_label in labels
                    else 0,
                    key=f"{edit_form_key}_account",
                )
                destination_options = ["No destination", *labels]
                edit_destination = st.selectbox(
                    "Destination account",
                    destination_options,
                    index=(
                        destination_options.index(destination_label)
                        if destination_label in destination_options
                        else 0
                    ),
                    key=f"{edit_form_key}_destination",
                )
                edit_date = st.date_input(
                    "Next date",
                    value=selected_schedule.next_date,
                    min_value=date.min,
                    max_value=date.max,
                    key=f"{edit_form_key}_date",
                )
                frequency_options = [
                    "once",
                    "weekly",
                    "biweekly",
                    "semimonthly",
                    "monthly",
                    "quarterly",
                    "yearly",
                ]
                edit_frequency = st.selectbox(
                    "Frequency",
                    frequency_options,
                    index=frequency_options.index(selected_schedule.frequency),
                    key=f"{edit_form_key}_frequency",
                    help=SCHEDULE_FREQUENCY_HELP,
                )
                provenance_options = ["hypothetical", "confirmed"]
                edit_provenance = st.selectbox(
                    "Provenance",
                    provenance_options,
                    index=provenance_options.index(selected_schedule.provenance),
                    key=f"{edit_form_key}_provenance",
                )
                edit_active = st.checkbox(
                    "Active", value=selected_schedule.is_active, key=f"{edit_form_key}_active"
                )
                edit_note = st.text_input(
                    "Note", value=selected_schedule.note, key=f"{edit_form_key}_note"
                )
                st.form_submit_button(
                    "Preview dates", help="Refresh the upcoming dates without saving changes."
                )
                edit_submitted = st.form_submit_button("Save Schedule")
            _render_schedule_date_preview(edit_date, edit_frequency)
            if edit_submitted:
                try:
                    schedule_service.update(
                        selected_schedule.schedule_id,
                        ScheduleDraft(
                            name=edit_name,
                            event_type=edit_event,
                            amount=Decimal(str(edit_amount)),
                            currency="CAD",
                            account_id=account_ids_by_label.get(str(edit_account), ""),
                            counterparty_account_id=(
                                account_ids_by_label.get(str(edit_destination))
                                if edit_destination != "No destination"
                                else None
                            ),
                            next_date=edit_date,
                            frequency=edit_frequency,
                            provenance=edit_provenance,
                            pattern_id=selected_schedule.pattern_id,
                            is_active=edit_active,
                            note=edit_note,
                        ),
                        expected_record=selected_schedule,
                    )
                except ScheduleConflictError:
                    st.session_state[conflict_notice_key] = True
                    st.rerun()
                except ScheduleValidationError as exc:
                    st.error(str(exc))
                except ScheduleNotFoundError:
                    st.error("That schedule no longer exists; reload before retrying.")
                except DatabaseWriteCommittedError:
                    st.error(PLANNING_SAVE_COMMITTED_WARNING)
                except DatabaseTargetLockError, OSError, sqlite3.Error:
                    st.error(PLANNING_SAVE_REFUSAL)
                else:
                    st.success("Schedule saved.")
                    st.rerun()

        confirm_col, delete_col = st.columns(2)
        with confirm_col:
            if st.button(
                "Confirm Selected Schedule",
                disabled=selected_schedule.provenance == "confirmed",
                key=f"{edit_form_key}_confirm",
            ):
                try:
                    schedule_service.confirm(
                        selected_schedule.schedule_id, expected_record=selected_schedule
                    )
                except ScheduleConflictError:
                    st.session_state[conflict_notice_key] = True
                    st.rerun()
                except ScheduleValidationError as exc:
                    st.error(str(exc))
                except ScheduleNotFoundError:
                    st.error("That schedule no longer exists; reload before retrying.")
                except DatabaseWriteCommittedError:
                    st.error(PLANNING_SAVE_COMMITTED_WARNING)
                except DatabaseTargetLockError, OSError, sqlite3.Error:
                    st.error(PLANNING_SAVE_REFUSAL)
                else:
                    st.success("Schedule confirmed.")
                    st.rerun()
        with delete_col:
            if st.button("Delete Selected Schedule", key=f"{edit_form_key}_delete"):
                try:
                    schedule_service.delete(
                        selected_schedule.schedule_id, expected_record=selected_schedule
                    )
                except ScheduleConflictError:
                    st.session_state[conflict_notice_key] = True
                    st.rerun()
                except ScheduleNotFoundError:
                    st.error("That schedule no longer exists; reload before retrying.")
                except DatabaseWriteCommittedError:
                    st.error(PLANNING_SAVE_COMMITTED_WARNING)
                except DatabaseTargetLockError, OSError, sqlite3.Error:
                    st.error(PLANNING_SAVE_REFUSAL)
                else:
                    st.success("Schedule deleted.")
                    st.rerun()

    candidates = schedule_service.detected_candidates()
    st.subheader("Detected possibilities")
    if candidates:
        candidate = st.selectbox(
            "Detected pattern to confirm",
            candidates,
            format_func=lambda row: (
                f"{row.name} · {format_currency(row.amount)} · "
                f"{row.next_date.isoformat()} · "
                f"{_account_label(account_labels, row.account_id)} · "
                f"{row.confidence:.0%} confidence"
            ),
            key="cash_pattern_selected",
        )
        if st.button("Confirm Detected Pattern", key="cash_pattern_confirm"):
            try:
                schedule_service.confirm_pattern(candidate.pattern_id)
            except ScheduleValidationError as exc:
                st.error(str(exc))
            except ScheduleNotFoundError:
                st.error(
                    "That detected pattern no longer exists; refresh analysis before retrying."
                )
            except DatabaseWriteCommittedError:
                st.error(PLANNING_SAVE_COMMITTED_WARNING)
            except DatabaseTargetLockError, OSError, sqlite3.Error:
                st.error(PLANNING_SAVE_REFUSAL)
            else:
                st.success("Detected pattern confirmed as a schedule.")
                st.rerun()
    else:
        st.caption("No unconfirmed detected income or bill patterns are available.")

    st.subheader("Cash plan")
    default_start = max(date.today(), ctx.end_date)
    plan_start = st.date_input("Plan from", value=default_start, key="cash_plan_start")
    plan_end = st.date_input(
        "Plan through",
        value=default_start + timedelta(days=60),
        min_value=plan_start,
        max_value=plan_start + timedelta(days=366),
        key="cash_plan_end",
    )
    try:
        plan = CashPlanService(ctx.db_path, prepare_schema=False).build(
            start_date=plan_start,
            end_date=plan_end,
            as_of=plan_start,
        )
    except CashPlanError as exc:
        st.error(str(exc))
        return
    except OSError, sqlite3.Error:
        st.error(
            "Cash plan could not read a stable database snapshot; retry after other work completes."
        )
        return

    net_movement = sum((account.planned_movement for account in plan.accounts), Decimal("0.00"))
    anchored = plan.coverage.anchored_account_count
    kpi_row(
        [
            ("Planned Movement", format_currency(net_movement), f"from {plan.as_of.isoformat()}"),
            ("Anchored Accounts", f"{anchored:,} / {plan.coverage.account_count:,}", None),
            (
                "Planned Occurrences",
                f"{plan.coverage.applied_planning_occurrence_count:,}",
                f"{plan.facts.fact_leg_count:,} imported fact leg(s)",
            ),
            ("Suppressed as Imported", f"{plan.coverage.suppressed_occurrence_count:,}", None),
        ]
    )
    st.caption(
        f"Projected levels start from the dated statement anchors shown below, apply imported "
        f"account activity through {plan.facts.actual_through.isoformat()}, then apply the labelled "
        "planning inputs. They are arithmetic planning values, not live account balances."
    )
    cash_account_rows = _cash_plan_account_rows(plan, account_labels)
    st.dataframe(
        pd.DataFrame(cash_account_rows),
        width="stretch",
        hide_index=True,
        column_config={
            "Anchor balance": st.column_config.NumberColumn(
                "Anchor balance", format=CURRENCY_FORMAT
            ),
            "Imported roll-forward": st.column_config.NumberColumn(
                "Imported roll-forward", format=CURRENCY_FORMAT
            ),
            "Opening at as-of": st.column_config.NumberColumn(
                "Opening at as-of", format=CURRENCY_FORMAT
            ),
            "Planned movement": st.column_config.NumberColumn(
                "Planned movement", format=CURRENCY_FORMAT
            ),
            "Projected balance": st.column_config.NumberColumn(
                "Projected balance", format=CURRENCY_FORMAT
            ),
            "Lowest balance": st.column_config.NumberColumn(
                "Lowest balance", format=CURRENCY_FORMAT
            ),
            "Lowest date": st.column_config.DateColumn("Lowest date", format="YYYY-MM-DD"),
        },
    )
    projection_options: dict[str, dict[str, str | None]] = {
        f"Projected balance · {account_labels[account.account_id]}": {
            "metric": "projection",
            "account_id": account.account_id,
        }
        for account in plan.accounts
        if account.account_id in account_labels
    }
    render_metric_explanations(
        ctx.db_path,
        plan_start,
        plan_end,
        projection_options,
        key="budgets_goals_projection",
    )

    fact_rows = _cash_plan_fact_rows(plan, account_labels)
    st.markdown("**Imported actual roll-forward**")
    st.caption(
        "These are imported facts after each account's usable statement anchor through the "
        "as-of date, aggregated by account, date, and event for display."
    )
    if fact_rows:
        st.dataframe(
            pd.DataFrame(fact_rows),
            width="stretch",
            hide_index=True,
            column_config={
                "Date": st.column_config.DateColumn("Date", format="YYYY-MM-DD"),
                "Net movement": st.column_config.NumberColumn(
                    "Net movement", format=CURRENCY_FORMAT
                ),
            },
        )
    else:
        st.caption("No imported CAD activity is needed to roll these anchors to the as-of date.")

    st.markdown("**Planned and detected occurrences**")
    leg_rows = _cash_plan_leg_rows(plan, account_labels)
    if leg_rows:
        st.dataframe(
            pd.DataFrame(leg_rows),
            width="stretch",
            hide_index=True,
            column_config={
                "Date": st.column_config.DateColumn("Date", format="YYYY-MM-DD"),
                "Amount": st.column_config.NumberColumn("Amount", format=CURRENCY_FORMAT),
            },
        )
    else:
        st.info("No unsuppressed schedule or detected-pattern occurrences fall in this period.")

    if plan.before_next_payday is not None:
        payday = plan.before_next_payday
        st.markdown(f"**Before next payday: {payday.payday_date.isoformat()}**")
        st.caption(
            f"Expected income {format_currency(payday.income_amount)} from "
            f"{', '.join(payday.income_provenances)} source(s)."
        )
        before_rows = [
            {
                "Account": _account_label(account_labels, row.account_id),
                "Movement before payday": row.projected_movement,
                "Projected balance before payday": row.projected_balance_before_payday,
            }
            for row in payday.accounts
        ]
        st.dataframe(
            pd.DataFrame(before_rows),
            width="stretch",
            hide_index=True,
            column_config={
                "Movement before payday": st.column_config.NumberColumn(
                    "Movement before payday", format=CURRENCY_FORMAT
                ),
                "Projected balance before payday": st.column_config.NumberColumn(
                    "Projected balance before payday", format=CURRENCY_FORMAT
                ),
            },
        )
    else:
        st.info(
            "A before-next-payday view needs a confirmed or detected income occurrence in the period."
        )

    if plan.suppressions:
        with st.expander(f"Imported-activity suppressions ({len(plan.suppressions)})"):
            st.caption(
                "Each row was suppressed by one unique exact account, date, amount, and type "
                "match in imported activity."
            )
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Date": row.occurrence_date,
                            "Name": row.name,
                            "Event": row.event_type,
                            "Amount": row.amount,
                            "Provenance": row.provenance,
                            "Matched as": row.matched_kind.replace("_", " "),
                            "Matched imported evidence": ", ".join(row.matched_evidence_ids),
                        }
                        for row in plan.suppressions
                    ]
                ),
                width="stretch",
                hide_index=True,
                column_config={
                    "Date": st.column_config.DateColumn("Date", format="YYYY-MM-DD"),
                    "Amount": st.column_config.NumberColumn("Amount", format=CURRENCY_FORMAT),
                },
            )
    with st.expander("Coverage and assumptions"):
        st.caption(
            f"Imported facts applied: {plan.facts.fact_leg_count:,} "
            f"({plan.facts.transaction_leg_count:,} transaction, "
            f"{plan.facts.transfer_leg_count:,} transfer). "
            f"Duplicate table representations removed: "
            f"{plan.facts.deduplicated_representation_count:,}; non-CAD rows excluded: "
            f"{plan.facts.excluded_non_cad_count:,}; rows without a canonical account: "
            f"{plan.facts.unmapped_account_count:,}."
        )
        st.caption(
            f"Schedules: {plan.coverage.active_schedule_count:,} active of "
            f"{plan.coverage.stored_schedule_count:,} stored "
            f"({plan.coverage.confirmed_schedule_count:,} confirmed, "
            f"{plan.coverage.hypothetical_schedule_count:,} hypothetical). "
            f"Detected sources used: {plan.coverage.detected_pattern_count:,}; "
            f"confirmed-link overrides: {plan.coverage.overridden_detected_pattern_count:,}."
        )
        if plan.coverage.excluded_pre_as_of_planning_occurrence_count:
            st.caption(
                f"{plan.coverage.excluded_pre_as_of_planning_occurrence_count:,} unmatched "
                "planning occurrence(s) before the as-of date were excluded rather than assumed."
            )
        if plan.coverage.missing_anchor_account_ids:
            missing_labels = [
                _account_label(account_labels, account_id)
                for account_id in plan.coverage.missing_anchor_account_ids
            ]
            st.caption("Accounts without a usable statement anchor: " + ", ".join(missing_labels))
        if plan.coverage.omitted_detected_patterns:
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Omitted detected-source reason": row.reason.replace("_", " "),
                            "Count": row.count,
                        }
                        for row in plan.coverage.omitted_detected_patterns
                    ]
                ),
                width="stretch",
                hide_index=True,
            )
        if plan.coverage.ambiguous_suppression_count:
            st.caption(
                f"{plan.coverage.ambiguous_suppression_count:,} occurrence(s) had ambiguous "
                "imported-activity candidates and remained in the plan."
            )
        st.markdown("**Planning assumptions**")
        for assumption in plan.assumptions:
            st.markdown(f"- {assumption}")


def render(ctx: AppContext) -> None:
    page_header(
        "Budgets / Goals",
        "Set local budgets, savings goals, confirmed schedules, and dated cash plans.",
        status=ctx.period_label,
    )

    service = BudgetService(ctx.db_path, prepare_schema=False)
    readiness = PlanningReadinessService(ctx.db_path, prepare_schema=False).summary(
        ctx.start_date,
        ctx.end_date,
        pending_reviews=ctx.pending_reviews,
    )
    kpis = ctx.service.compute_kpis(ctx.start_date, ctx.end_date)
    period_status_strip(
        start_date=ctx.start_date,
        end_date=ctx.end_date,
        row_count=int(kpis["transaction_count"]),
        source="Budgets + imported actuals",
    )
    _render_planning_readiness(readiness)

    budget_rows = service.list_budgets()
    variance = service.budget_variance(ctx.start_date, ctx.end_date)
    budgeted = sum(row["budget"] for row in variance)
    actual = sum(row["actual"] for row in variance)
    over_count = sum(1 for row in variance if row["status"] == "over")
    kpi_row(
        [
            ("Budgeted", format_currency(budgeted), None),
            ("Actual", format_currency(actual), None),
            (
                "Variance",
                format_currency(budgeted - actual),
                None,
                "pos" if budgeted - actual >= 0 else "neg",
            ),
            ("Over Budget", f"{over_count:,}", None),
        ]
    )
    render_metric_explanations(
        ctx.db_path,
        ctx.start_date,
        ctx.end_date,
        {
            "Budgeted": {"metric": "budget"},
            "Actual": {"metric": "budget_actual"},
            "Variance": {"metric": "budget_variance"},
        },
        key="budgets_goals_budget",
    )

    tab_budget, tab_goals, tab_cash = st.tabs(["Budgets", "Goals", "Cash Plan"])
    with tab_budget:
        st.subheader("Actual vs Budget")
        st.caption(
            "Actual is gross CAD spending on the same basis as ledger.aggregate: "
            "cashflow expenses excluding spend-excluded rows. It differs from Overview "
            "true cost, which also applies separate income treatments."
        )
        if budget_rows:
            st.dataframe(
                pd.DataFrame(variance).rename(
                    columns={
                        "category": "Category",
                        "budget": "Budget",
                        "actual": "Actual",
                        "variance": "Remaining",
                        "pct_used": "Used %",
                        "status": "Status",
                    }
                ),
                width="stretch",
                hide_index=True,
                column_config={
                    "Budget": st.column_config.NumberColumn("Budget", format=CURRENCY_FORMAT),
                    "Actual": st.column_config.NumberColumn("Actual", format=CURRENCY_FORMAT),
                    "Remaining": st.column_config.NumberColumn("Remaining", format=CURRENCY_FORMAT),
                    "Used %": st.column_config.NumberColumn("Used %", format="%.0f%%"),
                },
                height=300,
            )
        else:
            empty_state(
                "No budgets are configured yet.",
                "Add category rows below when you are ready; this page will not create defaults automatically.",
            )

        st.subheader("Edit Budgets")
        category_options = sorted(category.name for category in ctx.category_config.categories)
        budget_df = _budget_dataframe(budget_rows)
        edited = st.data_editor(
            budget_df,
            width="stretch",
            hide_index=True,
            num_rows="dynamic",
            height=420,
            column_config={
                "month": st.column_config.TextColumn(
                    "Month",
                    help="Use YYYY-MM for one month or * for a recurring monthly budget.",
                ),
                "category": st.column_config.SelectboxColumn("Category", options=category_options),
                "amount": st.column_config.NumberColumn(
                    "Amount", format=CURRENCY_FORMAT, min_value=0
                ),
            },
            key="budget_editor",
        )
        budget_preview = _planning_save_preview(
            budget_rows,
            edited,
            kind="budget",
        )
        st.caption(
            "Save preview: "
            f"{budget_preview['rows']:,} row(s), "
            f"{budget_preview['creates']:,} create(s), "
            f"{budget_preview['deletes']:,} delete(s)."
        )
        if budget_preview["requires_confirmation"]:
            st.text_input(
                "Confirm budget save",
                key="budget_confirm_save",
                help=f"Type `{BUDGET_SAVE_CONFIRM}` before replacing budget rows.",
            )
        for error in budget_preview["errors"]:
            st.error(error)
        if st.button(
            "Save Budgets",
            type="primary",
            disabled=not _planning_save_ready(
                st.session_state,
                "budget_confirm_save",
                BUDGET_SAVE_CONFIRM,
                budget_preview,
            ),
        ):
            try:
                result = service.replace_budgets(edited.to_dict("records"))
            except DatabaseWriteCommittedError:
                st.error(PLANNING_SAVE_COMMITTED_WARNING)
            except DatabaseTargetLockError, OSError, sqlite3.Error:
                st.error(PLANNING_SAVE_REFUSAL)
            else:
                if result.ok:
                    st.success(f"Saved {result.counts.get('budgets', 0)} budget row(s).")
                    st.rerun()
                else:
                    st.error("; ".join(result.errors))

    with tab_cash:
        _render_cash_planning(ctx, service)

    with tab_goals:
        goals = service.list_goals()
        goal_accounts = service.list_goal_accounts()
        account_labels = {row["account_id"]: row["label"] for row in goal_accounts}
        account_ids_by_label = {row["label"]: row["account_id"] for row in goal_accounts}
        account_balances_by_label = {
            row["label"]: {
                "current_amount": row.get("current_amount"),
                "current_balance_date": row.get("current_balance_date"),
            }
            for row in goal_accounts
        }
        default_account = goal_accounts[0]["label"] if goal_accounts else ""
        active_goals = [goal for goal in goals if goal["status"] == "active"]
        goal_summary = _goal_progress_summary(active_goals)
        goal_current = goal_summary["current"]
        goal_remaining = goal_summary["remaining"]
        goal_progress = goal_summary["progress_pct"]
        kpi_row(
            [
                ("Active Goals", f"{len(active_goals):,}", None),
                ("Target", format_currency(goal_summary["target"]), None),
                (
                    "Current",
                    format_currency(goal_current) if goal_current is not None else "N/A",
                    f"{goal_progress:.1f}%"
                    if goal_progress is not None
                    else "Needs absolute value",
                    "pos" if goal_current is not None else None,
                ),
                (
                    "Remaining",
                    format_currency(goal_remaining) if goal_remaining is not None else "N/A",
                    None,
                ),
            ]
        )
        if active_goals and goal_current is None:
            st.info(
                "Goal progress needs an absolute statement-backed account value; "
                "relative change history is not treated as zero progress."
            )
        if not goal_accounts:
            st.info(
                "No accounts are available yet. Refresh reports after importing transactions to create account balances."
            )
        if not goals:
            empty_state(
                "No savings goals are configured yet.",
                "Choose an account-backed goal below when you are ready; balances are read from account snapshots.",
            )
        # Feed st.data_editor the un-mutated base frame (DB rows + initial
        # balances) under the "goal_editor" key. Streamlit owns the edit delta in
        # st.session_state["goal_editor"] and re-applies it to this input exactly
        # once per run. Pre-applying that delta ourselves AND re-feeding the
        # mutated frame under the same key double-applied it on rerun (added rows
        # duplicated, deletes re-resolved against stale positions). The disabled
        # current_amount/current_balance_date columns are recomputed from the
        # *output* (edited_goals) below — the values that actually get saved — so
        # the editor still tracks the selected account, without the double-apply.
        goal_df = _with_goal_account_balances(
            _goal_dataframe(goals, account_labels, default_account),
            account_balances_by_label,
        )
        edited_goals = st.data_editor(
            goal_df,
            width="stretch",
            hide_index=True,
            num_rows="dynamic",
            height=460,
            column_order=GOAL_COLUMNS,
            column_config={
                "name": st.column_config.TextColumn("Name"),
                "account": st.column_config.SelectboxColumn(
                    "Account", options=list(account_ids_by_label)
                ),
                "target_amount": st.column_config.NumberColumn(
                    "Target", format=CURRENCY_FORMAT, min_value=0
                ),
                "current_amount": st.column_config.NumberColumn(
                    "Current", format=CURRENCY_FORMAT, disabled=True
                ),
                "current_balance_date": st.column_config.DateColumn("Balance Date", disabled=True),
                "target_date": st.column_config.DateColumn("Target Date", format="YYYY-MM-DD"),
                "status": st.column_config.SelectboxColumn(
                    "Status",
                    options=["active", "paused", "completed", "archived"],
                ),
                "notes": st.column_config.TextColumn("Notes"),
            },
            disabled=["current_amount", "current_balance_date"],
            key="goal_editor",
        )
        goal_records = []
        for row in edited_goals.to_dict("records"):
            account = str(row.get("account") or "")
            balance = account_balances_by_label.get(account, {})
            row["account_id"] = account_ids_by_label.get(account, "")
            row["current_amount"] = balance.get("current_amount")
            row["current_balance_date"] = balance.get("current_balance_date")
            goal_records.append(row)
        goal_preview = _planning_save_preview(goals, goal_records, kind="goal")
        st.caption(
            "Save preview: "
            f"{goal_preview['rows']:,} row(s), "
            f"{goal_preview['creates']:,} create(s), "
            f"{goal_preview['deletes']:,} delete(s)."
        )
        if goal_preview["requires_confirmation"]:
            st.text_input(
                "Confirm goal save",
                key="goal_confirm_save",
                help=f"Type `{GOAL_SAVE_CONFIRM}` before replacing goal rows.",
            )
        for error in goal_preview["errors"]:
            st.error(error)
        if st.button(
            "Save Goals",
            type="primary",
            disabled=not _planning_save_ready(
                st.session_state,
                "goal_confirm_save",
                GOAL_SAVE_CONFIRM,
                goal_preview,
            ),
        ):
            try:
                result = service.replace_goals(goal_records)
            except DatabaseWriteCommittedError:
                st.error(PLANNING_SAVE_COMMITTED_WARNING)
            except DatabaseTargetLockError, OSError, sqlite3.Error:
                st.error(PLANNING_SAVE_REFUSAL)
            except ValueError as exc:
                st.error(str(exc))
            else:
                if result.ok:
                    st.success(f"Saved {result.counts.get('goals', 0)} goal row(s).")
                    st.rerun()
                else:
                    st.error("; ".join(result.errors))
