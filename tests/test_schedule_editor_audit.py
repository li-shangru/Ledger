"""Behavior regressions for schedule form identity and recurrence previews."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from ledger.core.account_identity import account_identity
from ledger.planning.schedules import ScheduleDraft, ScheduleService
from ledger.storage.database import AccountDB, session_scope
from ledger.storage.migrations import ensure_schema
from ledger.utils.dates import naive_utc_now


def _seed_db(db_path: Path, *, name: str = "Internet", amount: str = "50") -> ScheduleService:
    ensure_schema(db_path)
    now = naive_utc_now()
    with session_scope(db_path) as session:
        for slug in ("chequing", "savings"):
            session.add(
                AccountDB(
                    account_id=account_identity("test", slug),
                    institution="test",
                    account_number=slug,
                    account_type=slug,
                    account_subtype=slug,
                    first_transaction_date=date(2026, 1, 1),
                    last_transaction_date=date(2026, 9, 1),
                    is_active=True,
                    transaction_count=1,
                    avg_monthly_transactions=Decimal("1"),
                    is_credit_card=False,
                    created_at=now,
                    updated_at=now,
                )
            )
    service = ScheduleService(db_path, prepare_schema=False)
    service.create(_draft(name=name, amount=Decimal(amount)), schedule_id="internet")
    service.create(
        _draft(
            name="Rent",
            amount=Decimal("1200"),
            next_date=date(2026, 10, 1),
            frequency="monthly",
            provenance="confirmed",
            note="Confirmed rent amount",
        ),
        schedule_id="rent",
    )
    return service


def _draft(**changes) -> ScheduleDraft:
    return replace(
        ScheduleDraft(
            name="Internet",
            event_type="bill",
            amount=Decimal("50"),
            currency="CAD",
            account_id=account_identity("test", "chequing"),
            counterparty_account_id=None,
            next_date=date(2026, 9, 5),
            frequency="monthly",
            provenance="hypothetical",
            note="Draft amount",
        ),
        **changes,
    )


def _app(db_paths: tuple[str, ...]) -> None:
    from datetime import date
    from pathlib import Path

    import streamlit as st

    from ledger.config import CategoriesConfig
    from ledger.planning import BudgetService
    from ledger.query import LedgerQueryService
    from ledger.ui.context import AppContext
    from ledger.ui.pages.budgets_goals import _render_cash_planning

    db_path = Path(st.selectbox("Test database", db_paths, key="test_db"))
    ctx = AppContext(
        db_path=db_path,
        service=LedgerQueryService(db_path),
        category_config=CategoriesConfig(),
        stats={},
        earliest=date(2026, 1, 1),
        latest=date(2026, 9, 1),
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 30),
        period_label="September 2026",
        review_source={},
        review_db=db_path,
        pending_reviews=0,
    )
    _render_cash_planning(ctx, BudgetService(db_path, prepare_schema=False))


def _start(*paths: Path) -> AppTest:
    app = AppTest.from_function(
        _app, args=(tuple(str(path) for path in paths),), default_timeout=30
    ).run()
    assert not app.exception
    return app


def _field(app: AppTest, kind: str, label: str, *, edit: bool = True):
    return [item for item in getattr(app, kind) if item.label == label][int(edit)]


def _button(app: AppTest, label: str):
    return next(item for item in app.button if item.label == label)


def test_switching_records_reseeds_every_field_before_save(tmp_path: Path) -> None:
    service = _seed_db(tmp_path / "ledger.db")
    before = service.list()
    app = _start(service.db_path)

    _field(app, "text_input", "Name").input("Unsaved Internet draft")
    _field(app, "number_input", "Amount (CAD)").set_value(75)
    _field(app, "selectbox", "Event type").select("transfer")
    _field(app, "selectbox", "Account").select_index(1)
    _field(app, "selectbox", "Destination account").select_index(1)
    _field(app, "date_input", "Next date").set_value(date(2026, 9, 8))
    _field(app, "selectbox", "Frequency").select("weekly")
    _field(app, "text_input", "Note").input("Unsaved note")
    app.checkbox[0].uncheck()
    [button for button in app.button if button.label == "Preview dates"][1].click().run()
    assert not app.exception
    assert _field(app, "text_input", "Name").value == "Unsaved Internet draft"
    assert _field(app, "number_input", "Amount (CAD)").value == 75
    assert _field(app, "selectbox", "Event type").value == "transfer"
    account = _field(app, "selectbox", "Account")
    assert account.value == account.options[1]
    destination = _field(app, "selectbox", "Destination account")
    assert destination.value == destination.options[1]
    assert _field(app, "date_input", "Next date").value == date(2026, 9, 8)
    assert _field(app, "selectbox", "Frequency").value == "weekly"
    assert _field(app, "text_input", "Note").value == "Unsaved note"
    assert app.checkbox[0].value is False
    assert service.list() == before
    next(item for item in app.selectbox if item.label == "Schedule to edit").select_index(1).run()

    assert not app.exception
    assert _field(app, "text_input", "Name").value == "Rent"
    assert _field(app, "number_input", "Amount (CAD)").value == 1200
    assert _field(app, "selectbox", "Event type").value == "bill"
    account = _field(app, "selectbox", "Account")
    assert account.value == account.options[0]
    assert _field(app, "selectbox", "Destination account").value == "No destination"
    assert _field(app, "date_input", "Next date").value == date(2026, 10, 1)
    assert _field(app, "selectbox", "Frequency").value == "monthly"
    assert _field(app, "selectbox", "Provenance").value == "confirmed"
    assert app.checkbox[0].value is True
    assert _field(app, "text_input", "Note").value == "Confirmed rent amount"

    _field(app, "number_input", "Amount (CAD)").set_value(1250)
    _button(app, "Save Schedule").click().run()

    assert not app.exception
    rows = {row.schedule_id: row for row in service.list()}
    assert (rows["rent"].name, rows["rent"].amount, rows["rent"].next_date) == (
        "Rent",
        Decimal("1250"),
        date(2026, 10, 1),
    )
    assert (rows["internet"].name, rows["internet"].amount) == ("Internet", Decimal("50"))


def test_database_switch_cannot_save_another_databases_draft(tmp_path: Path) -> None:
    first = _seed_db(tmp_path / "first.db")
    second = _seed_db(tmp_path / "second.db", name="Mobile", amount="90")
    before = (first.list(), second.list())
    app = _start(first.db_path, second.db_path)
    _field(app, "text_input", "Name").input("Private first draft")
    [button for button in app.button if button.label == "Preview dates"][1].click().run()
    assert not app.exception
    assert _field(app, "text_input", "Name").value == "Private first draft"
    assert (first.list(), second.list()) == before
    app.selectbox(key="test_db").select_index(1).run()

    assert not app.exception
    assert _field(app, "text_input", "Name").value == "Mobile"
    assert _field(app, "number_input", "Amount (CAD)").value == 90
    _button(app, "Save Schedule").click().run()
    assert not app.exception
    assert second.list()[0].name == "Mobile"
    assert first.list()[0].name == "Internet"


def test_external_saved_change_reloads_the_selected_record(tmp_path: Path) -> None:
    service = _seed_db(tmp_path / "ledger.db")
    app = _start(service.db_path)
    next(item for item in app.selectbox if item.label == "Schedule to edit").select_index(1).run()
    service.update(
        "rent", _draft(name="Updated rent", amount=Decimal("1300"), next_date=date(2026, 10, 2))
    )
    app.run()

    assert not app.exception
    assert _field(app, "text_input", "Name").value == "Updated rent"
    assert _field(app, "number_input", "Amount (CAD)").value == 1300
    assert _field(app, "date_input", "Next date").value == date(2026, 10, 2)
    _button(app, "Save Schedule").click().run()
    assert not app.exception
    assert next(row for row in service.list() if row.schedule_id == "rent").name == "Updated rent"


def test_ordinary_rerun_preserves_intentional_unsaved_edits(tmp_path: Path) -> None:
    service = _seed_db(tmp_path / "ledger.db")
    app = _start(service.db_path)
    _field(app, "text_input", "Name").input("Deliberate draft")
    _field(app, "number_input", "Amount (CAD)").set_value(60)
    # AppTest batches form edits until submit, matching Streamlit 1.66. Preview
    # sends the draft to session state without persisting the schedule.
    [button for button in app.button if button.label == "Preview dates"][1].click().run()
    app.run().run()

    assert not app.exception
    assert _field(app, "text_input", "Name").value == "Deliberate draft"
    assert _field(app, "number_input", "Amount (CAD)").value == 60
    assert service.list()[0].name == "Internet"


def test_stale_submission_does_not_overwrite_an_external_change(tmp_path: Path) -> None:
    service = _seed_db(tmp_path / "ledger.db")
    app = _start(service.db_path)
    _field(app, "number_input", "Amount (CAD)").set_value(60)
    saved = service.update("internet", _draft(name="Updated Internet", amount=Decimal("65")))
    _button(app, "Save Schedule").click().run()

    assert not app.exception
    assert service.list()[0] == saved
    assert _field(app, "text_input", "Name").value == "Updated Internet"
    assert _field(app, "number_input", "Amount (CAD)").value == 65


@pytest.mark.parametrize(
    "action", ["Save Schedule", "Confirm Selected Schedule", "Delete Selected Schedule"]
)
@pytest.mark.parametrize("external_change", ["update", "delete"])
def test_schedule_action_refuses_a_change_after_its_page_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    external_change: str,
) -> None:
    service = _seed_db(tmp_path / "ledger.db")
    app = _start(service.db_path)
    _field(app, "number_input", "Amount (CAD)").set_value(60)
    original_list = ScheduleService.list
    change_pending = True

    def list_then_change(reader, **kwargs):
        nonlocal change_pending
        snapshot = original_list(reader, **kwargs)
        if change_pending:
            change_pending = False
            if external_change == "delete":
                service.delete("internet")
            else:
                service.update("internet", _draft(name="External update", amount=Decimal("65")))
        return snapshot

    with monkeypatch.context() as patch:
        patch.setattr(ScheduleService, "list", list_then_change)
        _button(app, action).click().run()

    assert not app.exception
    rows = {row.schedule_id: row for row in service.list()}
    if external_change == "delete":
        assert "internet" not in rows
        assert _field(app, "text_input", "Name").value == "Rent"
    else:
        assert "internet" in rows
        assert rows["internet"].name == "External update"
        assert rows["internet"].amount == Decimal("65")
        assert rows["internet"].provenance == "hypothetical"
        assert _field(app, "text_input", "Name").value == "External update"
        assert _field(app, "number_input", "Amount (CAD)").value == 65
    assert any(
        "changed" in str(item.value).lower() and "reload" in str(item.value).lower()
        for item in app.warning
    )
    if external_change == "update":
        if action == "Save Schedule":
            _field(app, "number_input", "Amount (CAD)").set_value(70)
        _button(app, action).click().run()
        assert not app.exception
        retried_rows = {row.schedule_id: row for row in service.list()}
        if action == "Delete Selected Schedule":
            assert "internet" not in retried_rows
        else:
            assert retried_rows["internet"].name == "External update"
            assert retried_rows["internet"].amount == Decimal(
                "70" if action == "Save Schedule" else "65"
            )
            assert retried_rows["internet"].provenance == (
                "confirmed" if action == "Confirm Selected Schedule" else "hypothetical"
            )


@pytest.mark.parametrize("edit", [False, True], ids=["add", "edit"])
def test_preview_refreshes_unsaved_dates_without_writing(tmp_path: Path, edit: bool) -> None:
    service = _seed_db(tmp_path / "ledger.db")
    before = service.list()
    app = _start(service.db_path)
    _field(app, "date_input", "Next date", edit=edit).set_value(date(2026, 9, 10))
    _field(app, "selectbox", "Frequency", edit=edit).select("semimonthly")
    preview_buttons = [item for item in app.button if item.label == "Preview dates"]
    assert len(preview_buttons) == 2
    preview_buttons[int(edit)].click().run()

    assert not app.exception
    rendered = " ".join(str(item.value) for item in app.markdown)
    for expected in (
        "2026-09-10",
        "2026-09-25",
        "2026-10-10",
        "2026-10-25",
        "2026-11-10",
        "2026-11-25",
    ):
        assert expected in rendered
    assert service.list() == before


def test_add_draft_is_reset_when_switching_databases(tmp_path: Path) -> None:
    first = _seed_db(tmp_path / "first.db")
    second = _seed_db(tmp_path / "second.db")
    before = (first.list(), second.list())
    app = _start(first.db_path, second.db_path)
    _field(app, "text_input", "Name", edit=False).input("First database draft")
    _field(app, "number_input", "Amount (CAD)", edit=False).set_value(75)
    _button(app, "Preview dates").click().run()
    assert not app.exception
    assert _field(app, "text_input", "Name", edit=False).value == "First database draft"
    assert _field(app, "number_input", "Amount (CAD)", edit=False).value == 75
    assert (first.list(), second.list()) == before
    app.selectbox(key="test_db").select_index(1).run()

    assert not app.exception
    assert _field(app, "text_input", "Name", edit=False).value == ""
    assert _field(app, "number_input", "Amount (CAD)", edit=False).value == 0
    assert (first.list(), second.list()) == before


def test_preview_handles_the_last_representable_schedule_date(tmp_path: Path) -> None:
    service = _seed_db(tmp_path / "ledger.db")
    service.update("internet", _draft(next_date=date.max, frequency="yearly"))
    before = service.list()
    app = _start(service.db_path)
    next(item for item in app.selectbox if item.label == "Schedule to edit").select_index(1).run()

    assert not app.exception
    edit_expander = next(item for item in app.expander if item.label == "Edit selected schedule")
    assert "9999-12-31" in " ".join(str(item.value) for item in edit_expander.markdown)
    assert service.list() == before
