"""Synthetic operator regressions for reconciliation and reviewed planning changes."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from urllib.parse import parse_qs

import pytest
from streamlit.testing.v1 import AppTest
from test_cash_planning import _account, _add_same_sign_card_payment_pair, _add_transfer_pair
from test_financial_brief import _insert_transaction, _seed_database
from typer.testing import CliRunner

from ledger.cli import app
from ledger.core.account_identity import account_identity
from ledger.planning.discovery import local_reference
from ledger.planning.reconciliation import PlanReconciliationService
from ledger.planning.schedules import ScheduleDraft, ScheduleService
from ledger.proposals import ProposalService
from ledger.reporting.brief import FinancialBriefService
from ledger.storage.database import (
    PlannedScheduleDB,
    StatementCoverageIntervalDB,
    TransactionDB,
    session_scope,
)
from ledger.ui.navigation import brief_context


def test_reconciliation_link_keeps_exact_occurrence_period_and_as_of() -> None:
    context = brief_context(
        {"start": "2026-08-01", "end": "2026-08-31"},
        {
            "dashboard_path": "/plan-reconciliation",
            "subject_kind": "occurrence",
            "subject": "a" * 40,
            "evidence_ids": ["b" * 40],
            "as_of": "2026-09-04",
        },
    )

    assert context is not None
    assert context.page == "plan-reconciliation"
    assert (context.start_date, context.end_date) == (date(2026, 8, 1), date(2026, 8, 31))
    assert (context.subject_kind, context.subject) == ("occurrence", "a" * 40)
    assert context.evidence_ids == ("b" * 40,)
    assert context.as_of == date(2026, 9, 4)


def _income_expectation(db: Path) -> str:
    with pytest.MonkeyPatch.context() as clock:
        clock.setattr("ledger.planning.schedules.naive_utc_now", lambda: datetime(2026, 8, 1))
        record = ScheduleService(db).create(
            ScheduleDraft(
                name="PAYROLL",
                event_type="income",
                amount=Decimal("2000.00"),
                currency="CAD",
                account_id=account_identity("synthetic-bank", "synthetic-chequing-1234"),
                counterparty_account_id=None,
                next_date=date(2026, 8, 3),
                frequency="monthly",
                provenance="confirmed",
            ),
            schedule_id="payroll",
        )
    return record.schedule_id


def test_brief_prioritizes_material_plan_exception_and_keeps_period(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    _seed_database(db)
    _income_expectation(db)

    brief = FinancialBriefService(db).build(
        date(2026, 8, 1), date(2026, 8, 31), as_of=date(2026, 9, 4)
    )

    findings = [
        item for item in brief["observations"] if item["dashboard_path"] == "/plan-reconciliation"
    ]
    assert len(findings) == 1
    assert 3 <= len(brief["observations"]) <= 5
    assert findings[0]["amount"] == 100.0
    assert "2026-08-03" in findings[0]["statement"]
    assert "2026-08-05" in findings[0]["statement"]
    assert len(findings[0]["evidence_ids"]) == 1
    context = brief_context(brief["period"], findings[0])
    assert context is not None and context.subject_kind == "occurrence"
    assert (context.start_date, context.end_date, context.as_of) == (
        date(2026, 8, 1),
        date(2026, 8, 31),
        date(2026, 9, 4),
    )


def test_cli_lists_and_rejects_exact_planning_proposal_without_ledger(tmp_path: Path) -> None:
    store = tmp_path / "proposals.db"
    proposal = ProposalService(store).file_planning_change(
        {
            "operation": "schedule_update",
            "target": {"ref": "schedule-" + "a" * 40, "revision": "b" * 64},
            "effective_date": "2027-01-01",
            "changes": {"amount": "2100.00"},
        }
    )
    runner = CliRunner()
    result = runner.invoke(app, ["proposals", "list", "--proposals-db", str(store)])
    assert result.exit_code == 0, result.output
    [listed] = json.loads(result.output)
    assert listed["proposal_id"] == proposal.proposal_id
    assert listed["intent"]["changes"] == {"amount": "2100.00"}

    args = [
        "proposals",
        "reject",
        proposal.proposal_id,
        "--kind",
        "planning",
        "--proposals-db",
        str(store),
    ]
    refused = runner.invoke(app, args)
    assert refused.exit_code != 0
    assert ProposalService(store).show_planning_proposal(proposal.proposal_id).status == "pending"
    rejected = runner.invoke(app, [*args, "--proposal-revision", proposal.proposal_revision])
    assert rejected.exit_code == 0, rejected.output
    assert json.loads(rejected.output)["status"] == "rejected"


def test_cli_preview_apply_and_targeted_undo_require_reviewed_revisions(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    store = tmp_path / "proposals.db"
    _seed_database(db)
    _income_expectation(db)
    proposal = ProposalService(store).file_planning_change(
        {
            "operation": "schedule_update",
            "target": local_reference(db, "schedule", "payroll"),
            "effective_date": (date.today() + timedelta(days=1)).isoformat(),
            "changes": {
                "amount": "2100.00",
                "next_date": (date.today() + timedelta(days=30)).isoformat(),
            },
        }
    )
    runner = CliRunner()
    common = [proposal.proposal_id, "--db", str(db), "--proposals-db", str(store)]
    preview = runner.invoke(app, ["proposals", "preview", *common])
    assert preview.exit_code == 0, preview.output
    reviewed = json.loads(preview.output)
    assert reviewed["before"]["amount"] == "2000.00"
    assert reviewed["after"]["amount"] == "2100.00"
    apply_args = ["proposals", "apply", *common, "--kind", "planning"]
    assert runner.invoke(app, apply_args).exit_code != 0
    assert ScheduleService(db).list()[0].amount == Decimal("2000.00")
    applied = runner.invoke(
        app,
        [
            *apply_args,
            "--proposal-revision",
            reviewed["proposal_revision"],
            "--preview-revision",
            reviewed["preview_revision"],
        ],
    )
    assert applied.exit_code == 0, applied.output
    assert ScheduleService(db).list()[0].amount == Decimal("2100.00")
    receipt = json.loads(applied.output)
    undone = runner.invoke(
        app,
        [
            "proposals",
            "undo",
            *common,
            "--after-revision",
            receipt["details"]["expected_after_revision"],
        ],
    )
    assert undone.exit_code == 0, undone.output
    assert ScheduleService(db).list()[0].amount == Decimal("2000.00")
    replay = runner.invoke(
        app,
        [
            *apply_args,
            "--proposal-revision",
            reviewed["proposal_revision"],
            "--preview-revision",
            reviewed["preview_revision"],
        ],
    )
    assert replay.exit_code == 0, replay.output
    assert json.loads(replay.output)["status"] == "undone"
    assert ScheduleService(db).list()[0].amount == Decimal("2000.00")


def test_empty_period_brief_includes_missing_plan_and_reopens_changed_evidence(
    tmp_path: Path,
) -> None:
    db = tmp_path / "ledger.db"
    _seed_database(db)
    _income_expectation(db)
    with session_scope(db) as session:
        session.add(
            StatementCoverageIntervalDB(
                interval_id="coverage",
                institution="synthetic-bank",
                account_slug="synthetic-chequing-1234",
                start_date=date(2026, 8, 1),
                end_date=date(2026, 9, 30),
                evidence_basis="printed_statement_period",
                imported_at=datetime(2026, 10, 1),
            )
        )
    service = FinancialBriefService(db)
    brief = service.build(date(2026, 9, 1), date(2026, 9, 30))
    assert brief["status"] == "empty"
    assert brief["metrics"] is None
    exception = next(
        item for item in brief["observations"] if item["dashboard_path"] == "/plan-reconciliation"
    )
    assert "fully covered" in exception["statement"]
    reconciliation = PlanReconciliationService(db)
    [finding] = reconciliation.build(date(2026, 9, 1), date(2026, 9, 30), as_of=date(2026, 9, 30))[
        "findings"
    ]
    reconciliation.mark_reviewed(
        date(2026, 9, 1),
        date(2026, 9, 30),
        as_of=date(2026, 9, 30),
        occurrence_id=finding["occurrence_id"],
        material_revision=finding["material_revision"],
    )
    reviewed = service.build(date(2026, 9, 1), date(2026, 9, 30))
    assert all(
        item["dashboard_path"] != "/plan-reconciliation" for item in reviewed["observations"]
    )
    _insert_transaction(db, "new-payroll", date(2026, 9, 4), "2100.00", "PAYROLL", "Income")
    reopened = service.build(date(2026, 9, 1), date(2026, 9, 30))
    assert any(
        item["dashboard_path"] == "/plan-reconciliation" for item in reopened["observations"]
    )
    default = service.build_default()
    assert default["period"]["end"] == "2026-09-30"


def test_brief_discloses_reconciliation_window_and_missing_coverage(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    _seed_database(db)
    _income_expectation(db)
    service = FinancialBriefService(db)
    no_coverage = service.build(date(2026, 9, 1), date(2026, 9, 30))
    [finding] = [
        row
        for row in no_coverage["observations"]
        if row["dashboard_path"] == "/plan-reconciliation"
    ]
    assert finding["label"] == "coverage"
    assert "coverage" in finding["statement"].lower()
    assert "Multiple plausible" not in finding["statement"]
    wide = service.build(date(2025, 1, 1), date(2026, 9, 30))
    assert wide["status"] == "ready"
    assert 3 <= len(wide["observations"]) <= 5
    assert any(row["title"] == "Plan comparison period boundary" for row in wide["observations"])


def _operator_app() -> None:
    from datetime import date

    import streamlit as st

    from ledger.ui.context import build_app_context
    from ledger.ui.pages import plan_reconciliation, proposals, transactions

    st.session_state.setdefault("period_preset", "Custom Range")
    st.session_state.setdefault("custom_start_date", date(2026, 8, 1))
    st.session_state.setdefault("custom_end_date", date(2026, 8, 31))
    ctx = build_app_context()
    pages = [
        st.Page(
            lambda: plan_reconciliation.render(ctx),
            title="Plan versus actual",
            url_path="plan-reconciliation",
            default=True,
        ),
        st.Page(lambda: transactions.render(ctx), title="Transactions", url_path="transactions"),
        st.Page(lambda: proposals.render(ctx), title="Proposals", url_path="proposals"),
    ]
    st.session_state["ledger_pages"] = {
        "plan-reconciliation": pages[0],
        "transactions": pages[1],
        "proposals": pages[2],
    }
    st.navigation(pages).run()


def _follow_operator_link(at: AppTest, label: str) -> None:
    [link] = [item.proto for item in at.get("page_link") if item.proto.label == label]
    assert not link.external
    at._page_hash = link.page_script_hash
    at.query_params = parse_qs(link.query_string)
    at.run()
    assert not at.exception


def test_reconciliation_ui_shows_independent_differences_and_imported_evidence(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "ledger.db"
    _seed_database(db)
    _income_expectation(db)
    monkeypatch.setenv("LEDGER_DB", str(db))
    at = AppTest.from_function(_operator_app, default_timeout=30).run()
    assert not at.exception
    captions = [row.value for row in at.caption]
    assert any("Amount changed" in text and "100.00" in text for text in captions)
    assert any("Date changed" in text and "2" in text for text in captions)
    _follow_operator_link(at, "Open imported evidence")
    assert at.date_input(key="custom_start_date").value == date(2026, 8, 1)
    assert at.date_input(key="custom_end_date").value == date(2026, 8, 31)
    [table] = at.dataframe
    assert len(table.value) == 1
    assert table.value.iloc[0]["Amount"] == "2100.00"
    assert table.value.iloc[0]["Date"] == "2026-08-05"


def _proposal_app() -> None:
    from ledger.ui.context import build_app_context
    from ledger.ui.pages import proposals

    proposals.render(build_app_context())


def test_proposal_ui_requires_preview_then_applies_and_undoes_exact_change(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "ledger.db"
    store = tmp_path / "proposals.db"
    _seed_database(db)
    _income_expectation(db)
    filed = ProposalService(store).file_planning_change(
        {
            "operation": "schedule_update",
            "target": local_reference(db, "schedule", "payroll"),
            "effective_date": (date.today() + timedelta(days=1)).isoformat(),
            "changes": {
                "amount": "2100.00",
                "next_date": (date.today() + timedelta(days=30)).isoformat(),
            },
        }
    )
    monkeypatch.setenv("LEDGER_DB", str(db))
    at = AppTest.from_function(_proposal_app, default_timeout=30).run()
    assert not at.exception
    assert not any(button.label == "Apply reviewed proposal" for button in at.button)
    at.button(key="planning_preview_button").click().run()
    assert not at.exception
    [amount] = [row for row in at.dataframe[0].value.to_dict("records") if row["Field"] == "amount"]
    assert (amount["Before"], amount["After"]) == ("2000.00", "2100.00")
    at.button(key="planning_apply").click().run()
    assert not at.exception
    assert ScheduleService(db).list()[0].amount == Decimal("2100.00")
    assert ProposalService(store).show_planning_proposal(filed.proposal_id).status == "applied"
    at.button(key="planning_preview_button").click().run()
    assert not at.exception
    at.button(key="planning_undo").click().run()
    assert not at.exception
    assert ScheduleService(db).list()[0].amount == Decimal("2000.00")
    assert ProposalService(store).show_planning_proposal(filed.proposal_id).status == "undone"


def test_reconciliation_future_update_files_amount_proposal_without_changing_schedule(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "ledger.db"
    _seed_database(db)
    _income_expectation(db)
    monkeypatch.setenv("LEDGER_DB", str(db))
    at = AppTest.from_function(_operator_app, default_timeout=30).run()
    assert not at.exception
    [button] = [row for row in at.button if row.label == "Update future schedule"]
    button.click().run()
    assert not at.exception
    assert ScheduleService(db).list()[0].amount == Decimal("2000.00")
    [filed] = ProposalService(tmp_path / "proposals.db").list_planning_proposals()
    intent = filed.to_dict()["intent"]
    assert intent["changes"] == {"amount": "2100.00"}
    assert date.fromisoformat(intent["effective_date"]) > date.today()
    _follow_operator_link(at, "Open filed proposal")
    at.button(key="planning_preview_button").click().run()
    assert not at.exception
    assert not at.button(key="planning_apply").disabled


def test_reconciliation_ui_refuses_correction_after_newer_future_edit(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "ledger.db"
    _seed_database(db)
    _income_expectation(db)
    [record] = ScheduleService(db).list()
    ScheduleService(db).update(
        "payroll",
        ScheduleDraft(
            name=record.name,
            event_type=record.event_type,
            amount=Decimal("2300.00"),
            currency=record.currency,
            account_id=record.account_id,
            counterparty_account_id=None,
            next_date=record.next_date,
            frequency=record.frequency,
            provenance=record.provenance,
        ),
        effective_date=date.today() + timedelta(days=10),
    )
    monkeypatch.setenv("LEDGER_DB", str(db))
    at = AppTest.from_function(_operator_app, default_timeout=30).run()
    assert not at.exception
    assert not any(button.label == "Update future schedule" for button in at.button)
    assert any("Review the current schedule" in row.value for row in at.info)
    assert ScheduleService(db).list()[0].amount == Decimal("2300.00")


@pytest.mark.parametrize("card", [False, True])
def test_reconciliation_ui_transfer_link_renders_both_account_legs(
    tmp_path: Path, monkeypatch, card: bool
) -> None:
    db = tmp_path / "ledger.db"
    _seed_database(db)
    destination = ("card-bank", "rewards-card") if card else ("test", "rainy-day")
    with session_scope(db) as session:
        session.add(_account("test", "daily-chequing"))
        session.add(
            _account(
                *destination, account_type="credit_card" if card else "savings", is_credit_card=card
            )
        )
    ScheduleService(db).create(
        ScheduleDraft(
            name="Synthetic payment",
            event_type="card_payment" if card else "transfer",
            amount=Decimal("100.00"),
            currency="CAD",
            account_id=account_identity("test", "daily-chequing"),
            counterparty_account_id=account_identity(*destination),
            next_date=date(2026, 8, 10),
            frequency="monthly",
            provenance="confirmed",
        )
    )
    if card:
        _add_same_sign_card_payment_pair(db, on=date(2026, 8, 11), amount="100.00")
    else:
        _add_transfer_pair(db, on=date(2026, 8, 11), amount="100.00")
    monkeypatch.setenv("LEDGER_DB", str(db))
    at = AppTest.from_function(_operator_app, default_timeout=30).run()
    assert not at.exception
    _follow_operator_link(at, "Open imported evidence")
    [table] = at.dataframe
    assert sorted(table.value["Amount"].tolist()) == ["-100.00", "100.00"]
    assert set(table.value["Event"]) == {"card_payment" if card else "transfer"}
    assert len(set(table.value["Account"])) == 2


def test_cli_files_typed_intent_without_financial_database(tmp_path: Path) -> None:
    intent = tmp_path / "intent.json"
    store = tmp_path / "proposals.db"
    intent.write_text(
        json.dumps(
            {
                "operation": "schedule_remove",
                "target": {"ref": "schedule-" + "a" * 40, "revision": "b" * 64},
                "effective_date": "2027-01-01",
            }
        )
    )
    result = CliRunner().invoke(
        app, ["proposals", "file", "--intent-file", str(intent), "--proposals-db", str(store)]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["intent"]["operation"] == "schedule_remove"
    assert len(ProposalService(store).list_planning_proposals()) == 1
    assert not (tmp_path / "ledger.db").exists()


def _linked_reconciliation_app(observation: dict) -> None:
    import streamlit as st

    from ledger.ui.context import build_app_context
    from ledger.ui.financial_brief import render_financial_brief
    from ledger.ui.pages import plan_reconciliation, transactions

    ctx = build_app_context()
    brief = {"period": {"start": "2026-08-01", "end": "2026-08-31"}, "observations": [observation]}
    pages = [
        st.Page(lambda: render_financial_brief(brief), title="Brief", default=True),
        st.Page(
            lambda: plan_reconciliation.render(ctx),
            title="Plan versus actual",
            url_path="plan-reconciliation",
        ),
        st.Page(lambda: transactions.render(ctx), title="Transactions", url_path="transactions"),
    ]
    st.session_state["ledger_pages"] = {
        "plan-reconciliation": pages[1],
        "transactions": pages[2],
    }
    st.navigation(pages).run()


@pytest.mark.parametrize("transfer_only", [False, True])
def test_reconciliation_brief_link_without_transactions_keeps_historical_context(
    tmp_path: Path, monkeypatch, transfer_only: bool
) -> None:
    db = tmp_path / "ledger.db"
    _seed_database(db)
    with session_scope(db) as session:
        session.query(TransactionDB).delete()
    target_schedule = _income_expectation(db)
    if transfer_only:
        with session_scope(db) as session:
            session.add_all([_account("test", "daily-chequing"), _account("test", "rainy-day")])
        target_schedule = (
            ScheduleService(db)
            .create(
                ScheduleDraft(
                    name="Synthetic transfer",
                    event_type="transfer",
                    amount=Decimal("100.00"),
                    currency="CAD",
                    account_id=account_identity("test", "daily-chequing"),
                    counterparty_account_id=account_identity("test", "rainy-day"),
                    next_date=date(2026, 8, 10),
                    frequency="once",
                    provenance="confirmed",
                )
            )
            .schedule_id
        )
        _add_transfer_pair(db, on=date(2026, 8, 11), amount="100.00")
    report = PlanReconciliationService(db).build(
        date(2026, 8, 1), date(2026, 8, 31), as_of=date(2026, 8, 31)
    )
    target = next(row for row in report["findings"] if row["schedule_id"] == target_schedule)
    brief = FinancialBriefService(db).build(date(2026, 8, 1), date(2026, 8, 31))
    observation = next(
        row for row in brief["observations"] if row.get("subject") == target["occurrence_id"]
    )
    monkeypatch.setenv("LEDGER_DB", str(db))
    at = AppTest.from_function(
        _linked_reconciliation_app, args=(observation,), default_timeout=30
    ).run()
    assert not at.exception
    _follow_operator_link(at, "Open supporting details")
    assert not at.exception
    assert at.selectbox(key="period_preset").value == "Custom Range"
    assert at.date_input(key="custom_start_date").value == date(2026, 8, 1)
    assert at.date_input(key="custom_end_date").value == date(2026, 8, 31)
    assert at.selectbox(key="plan_reconciliation_selected").value == target["occurrence_id"]
    assert at.date_input(key="plan_reconciliation_as_of").value == date(2026, 8, 31)
    at.run()
    assert at.selectbox(key="plan_reconciliation_selected").value == target["occurrence_id"]
    if transfer_only:
        _follow_operator_link(at, "Open imported evidence")
        assert not at.exception
        assert sorted(at.dataframe[0].value["Amount"]) == ["-100.00", "100.00"]
        assert any("2026-08-01 through 2026-08-31" in row.value for row in at.caption)


@pytest.mark.parametrize("offset", [-90, 90])
def test_empty_ledger_allows_manual_past_and_future_planning_periods(
    tmp_path: Path, monkeypatch, offset: int
) -> None:
    db = tmp_path / "ledger.db"
    _seed_database(db)
    with session_scope(db) as session:
        session.query(TransactionDB).delete()
    monkeypatch.setenv("LEDGER_DB", str(db))
    at = AppTest.from_function(_linked_reconciliation_app, args=({},), default_timeout=30).run()
    assert not at.exception
    at.selectbox(key="period_preset").select("Custom Range").run()
    start = date.today() + timedelta(days=offset)
    end = start + timedelta(days=20)
    at.date_input(key="custom_start_date").set_value(start)
    at.date_input(key="custom_end_date").set_value(end).run()
    assert not at.exception
    assert at.date_input(key="custom_start_date").value == start
    assert at.date_input(key="custom_end_date").value == end
    assert at.date_input(key="custom_start_date").min <= start
    assert at.date_input(key="custom_end_date").max >= end


def test_linked_later_page_review_marks_only_the_displayed_occurrence(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "ledger.db"
    _seed_database(db)
    with session_scope(db) as session:
        session.add_all(
            [
                PlannedScheduleDB(
                    schedule_id=f"schedule-{index:03d}",
                    name=f"Synthetic bill {index:03d}",
                    event_type="bill",
                    amount=Decimal("100.00"),
                    currency="CAD",
                    account_id=account_identity("synthetic-bank", "synthetic-chequing-1234"),
                    counterparty_account_id=None,
                    next_date=date(2026, 8, 20),
                    frequency="once",
                    provenance="confirmed",
                    is_active=True,
                    note="",
                    created_at=datetime(2026, 8, 1),
                    updated_at=datetime(2026, 8, 1),
                )
                for index in range(101)
            ]
        )
    service = PlanReconciliationService(db)
    [target] = service.build(
        date(2026, 8, 1), date(2026, 8, 31), as_of=date(2026, 8, 31), offset=100
    )["findings"]
    observation = {
        "rank": 1,
        "label": "coverage",
        "title": "Plan versus actual exception",
        "statement": "Synthetic missing coverage",
        "dashboard_path": "/plan-reconciliation",
        "subject_kind": "occurrence",
        "subject": target["occurrence_id"],
        "as_of": "2026-08-31",
    }
    monkeypatch.setenv("LEDGER_DB", str(db))
    at = AppTest.from_function(
        _linked_reconciliation_app, args=(observation,), default_timeout=30
    ).run()
    assert not at.exception
    _follow_operator_link(at, "Open supporting details")
    assert at.selectbox(key="plan_reconciliation_selected").value == target["occurrence_id"]
    at.button(key="plan_review_exception").click().run()
    assert not at.exception
    first_page = service.build(date(2026, 8, 1), date(2026, 8, 31), as_of=date(2026, 8, 31))
    [last] = service.build(
        date(2026, 8, 1), date(2026, 8, 31), as_of=date(2026, 8, 31), offset=100
    )["findings"]
    assert last["reviewed"]
    assert not any(row["reviewed"] for row in first_page["findings"])


def test_fresh_dashboard_recovers_interrupted_application_from_shown_preview(
    tmp_path: Path, monkeypatch
) -> None:
    db = tmp_path / "ledger.db"
    store = tmp_path / "proposals.db"
    _seed_database(db)
    _income_expectation(db)
    service = ProposalService(store)
    proposal = service.file_planning_change(
        {
            "operation": "schedule_update",
            "target": local_reference(db, "schedule", "payroll"),
            "effective_date": (date.today() + timedelta(days=1)).isoformat(),
            "changes": {"amount": "2100.00"},
        }
    )
    preview = service.preview_planning_proposal(proposal.proposal_id, ledger_db=db)

    def interrupted(*_args):
        raise OSError("Synthetic proposal-store interruption")

    with monkeypatch.context() as fault:
        fault.setattr(service, "_mark_planning_status", interrupted)
        failed = service.apply_planning_proposal(
            proposal.proposal_id,
            ledger_db=db,
            expected_proposal_revision=preview.proposal_revision,
            expected_preview_revision=preview.preview_revision,
        )
    assert failed.status == "committed_unconfirmed"
    assert ScheduleService(db).list()[0].amount == Decimal("2100.00")
    assert service.show_planning_proposal(proposal.proposal_id).status == "pending"
    monkeypatch.setenv("LEDGER_DB", str(db))
    at = AppTest.from_function(_proposal_app, default_timeout=30).run()
    assert not at.exception
    at.button(key="planning_preview_button").click().run()
    assert not at.exception
    assert not any(button.label == "Reject proposal" for button in at.button)
    at.button(key="planning_recover").click().run()
    assert not at.exception
    assert service.show_planning_proposal(proposal.proposal_id).status == "applied"
    assert ScheduleService(db).list()[0].amount == Decimal("2100.00")
    assert any("status recovered" in row.value for row in at.success)
