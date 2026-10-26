"""Synthetic behavior tests for reviewed, record-scoped planning proposals."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

import pytest
from test_cash_planning import _account
from test_review_teach import _seed_config

from ledger.proposals import ProposalService
from ledger.storage.database import init_database, session_scope
from ledger.storage.migrations import ensure_schema
from ledger.storage.operations import ensure_account_ref_salt


def _setup(tmp_path: Path) -> tuple[Path, ProposalService]:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    db = directory / "ledger.db"
    init_database(db)
    ensure_schema(db)
    _seed_config(db)
    with session_scope(db) as session:
        session.add(_account("test", "test-chequing"))
        session.add(_account("test", "test-card", is_credit_card=True))
    ensure_account_ref_salt(db)
    return db, ProposalService(directory / "proposals.db")


def _discovery(db: Path, kind: str = "all") -> dict:
    from ledger.planning.discovery import discover_planning_definitions, read_reference_salt

    with closing(sqlite3.connect(db)) as connection:
        connection.row_factory = sqlite3.Row
        return discover_planning_definitions(
            connection, salt=read_reference_salt(connection), kind=kind
        )


def _ref(entry: dict) -> dict:
    return {"ref": entry["ref"], "revision": entry["revision"]}


def _budget_intent(db: Path) -> dict:
    category = _discovery(db, "category")["entries"][0]
    return {
        "operation": "budget_create",
        "category": _ref(category),
        "budget_month": "*",
        "amount": "123.45",
        "currency": "CAD",
    }


def test_typed_filing_writes_only_the_isolated_store(tmp_path: Path) -> None:
    service = ProposalService(tmp_path / "private" / "proposals.db")
    assert hasattr(service, "file_planning_change"), "typed proposal filing is missing"
    filed = service.file_planning_change(
        {
            "operation": "budget_create",
            "category": {"ref": "category-" + "a" * 40, "revision": "b" * 64},
            "budget_month": "*",
            "amount": "123.45",
            "currency": "CAD",
        }
    )
    assert filed.status == "pending"
    assert service.show_planning_proposal(filed.proposal_id) == filed
    assert not (service.proposals_db.parent / "ledger.db").exists()
    assert service.list_planning_proposals() == [filed]


@pytest.mark.parametrize(
    "mutation",
    [
        {"amount": 12.30},
        {"amount": "1.234"},
        {"amount": "NaN"},
        {"currency": None},
        {"currency": "USD"},
        {"budget_month": "2026-99"},
        {"unknown": True},
    ],
)
def test_filing_rejects_ambiguous_or_unsafe_money_without_creating_store(
    tmp_path: Path, mutation: dict
) -> None:
    service = ProposalService(tmp_path / "private" / "proposals.db")
    assert hasattr(service, "file_planning_change"), "typed proposal filing is missing"
    intent = {
        "operation": "budget_create",
        "category": {"ref": "category-" + "a" * 40, "revision": "b" * 64},
        "budget_month": "*",
        "amount": "100.00",
        "currency": "CAD",
    }
    intent.update(mutation)
    with pytest.raises(ValueError):
        service.file_planning_change(intent)
    assert not service.proposals_db.exists()


def test_discovery_is_private_stable_bounded_and_revision_sensitive(tmp_path: Path) -> None:
    db, _service = _setup(tmp_path)
    from ledger.planning.discovery import discover_planning_definitions, read_reference_salt

    with closing(sqlite3.connect(db)) as connection:
        connection.row_factory = sqlite3.Row
        salt = read_reference_salt(connection)
        first = discover_planning_definitions(connection, salt=salt, limit=1)
        second = discover_planning_definitions(connection, salt=salt, limit=1, offset=1)
    assert first["has_more"]
    assert len(first["entries"]) == 1
    assert first["entries"][0]["ref"] != second["entries"][0]["ref"]
    wire = json.dumps(_discovery(db))
    for private in ("test-chequing", "test-card", "chequing", "account_id", "name", "note"):
        assert private not in wire
    before = _discovery(db, "category")["entries"][0]
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute("UPDATE category_config SET exclude_from_spending = 1")
    after = _discovery(db, "category")["entries"][0]
    assert before["ref"] == after["ref"]
    assert before["revision"] != after["revision"]


def test_budget_preview_apply_retry_and_conditional_undo(tmp_path: Path) -> None:
    db, service = _setup(tmp_path)
    assert hasattr(service, "file_planning_change"), "typed proposal filing is missing"
    proposal = service.file_planning_change(_budget_intent(db))
    preview = service.preview_planning_proposal(proposal.proposal_id, ledger_db=db)
    assert preview.can_apply
    assert preview.before is None
    assert preview.after["amount"] == "123.45"
    result = service.apply_planning_proposal(
        proposal.proposal_id,
        ledger_db=db,
        expected_proposal_revision=proposal.proposal_revision,
        expected_preview_revision=preview.preview_revision,
    )
    assert result.ok, result.errors
    retry = service.apply_planning_proposal(
        proposal.proposal_id,
        ledger_db=db,
        expected_proposal_revision=proposal.proposal_revision,
        expected_preview_revision=preview.preview_revision,
    )
    assert retry.ok
    with closing(sqlite3.connect(db)) as connection:
        assert connection.execute("SELECT count(*) FROM budgets").fetchone()[0] == 1
        assert (
            connection.execute("SELECT count(*) FROM planning_proposal_receipts").fetchone()[0] == 1
        )
    undone = service.undo_planning_proposal(
        proposal.proposal_id,
        ledger_db=db,
        expected_after_revision=result.details["after_revision"],
    )
    assert undone.ok, undone.errors
    with closing(sqlite3.connect(db)) as connection:
        assert connection.execute("SELECT count(*) FROM budgets").fetchone()[0] == 0


def _apply(service: ProposalService, db: Path, intent: dict):
    proposal = service.file_planning_change(intent)
    preview = service.preview_planning_proposal(proposal.proposal_id, ledger_db=db)
    assert preview.can_apply, preview.validation_errors
    result = service.apply_planning_proposal(
        proposal.proposal_id,
        ledger_db=db,
        expected_proposal_revision=proposal.proposal_revision,
        expected_preview_revision=preview.preview_revision,
    )
    assert result.ok, result.errors
    return proposal, preview, result


def _schedule_intent(db: Path, *, provenance: str = "hypothetical") -> dict:
    account = next(row for row in _discovery(db, "account")["entries"] if not row["is_credit_card"])
    return {
        "operation": "schedule_create",
        "name": "Private expected rent",
        "event_type": "bill",
        "amount": "100.00",
        "currency": "CAD",
        "account": _ref(account),
        "counterparty_account": None,
        "next_date": (date.today() + timedelta(days=30)).isoformat(),
        "frequency": "monthly",
        "provenance": provenance,
        "is_active": True,
        "note": "Private source note",
        "effective_date": (date.today() + timedelta(days=1)).isoformat(),
    }


@pytest.mark.parametrize("operation", ["budget_update", "budget_remove"])
def test_budget_changes_restore_only_the_affected_row(tmp_path: Path, operation: str) -> None:
    db, service = _setup(tmp_path)
    _apply(service, db, _budget_intent(db))
    entry = _discovery(db, "budget")["entries"][0]
    intent = {"operation": operation, "target": _ref(entry)}
    if operation == "budget_update":
        intent.update({**_budget_intent(db), "operation": operation, "amount": "250.00"})
    proposal, preview, result = _apply(service, db, intent)
    assert preview.before["amount"] == "123.45"
    assert (
        preview.after is None
        if operation.endswith("remove")
        else preview.after["amount"] == "250.00"
    )
    # A later unrelated record must survive targeted inverse application.
    unrelated = _budget_intent(db)
    unrelated.update(budget_month="2030-01", amount="17.00")
    _apply(service, db, unrelated)
    undone = service.undo_planning_proposal(
        proposal.proposal_id, ledger_db=db, expected_after_revision=result.details["after_revision"]
    )
    assert undone.ok, undone.errors
    amounts = {(row["budget_month"], row["amount"]) for row in _discovery(db, "budget")["entries"]}
    assert amounts == {("*", "123.45"), ("2030-01", "17.00")}


@pytest.mark.parametrize(
    "operation",
    [
        "schedule_update",
        "schedule_confirm",
        "schedule_deactivate",
        "schedule_reactivate",
        "schedule_remove",
    ],
)
def test_every_schedule_operation_has_conditional_history_preserving_undo(
    tmp_path: Path, operation: str
) -> None:
    db, service = _setup(tmp_path)
    original = _schedule_intent(db)
    if operation == "schedule_reactivate":
        original["is_active"] = False
    _apply(service, db, original)
    before = _discovery(db, "schedule")["entries"][0]
    intent = {
        "operation": operation,
        "target": _ref(before),
        "effective_date": original["effective_date"],
    }
    if operation == "schedule_update":
        intent["changes"] = {
            "amount": "125.55",
            "next_date": (date.today() + timedelta(days=45)).isoformat(),
        }
    proposal, preview, result = _apply(service, db, intent)
    if operation == "schedule_update":
        assert preview.next_occurrences[0] == (date.today() + timedelta(days=45)).isoformat()
        assert preview.after["amount"] == "125.55"
    elif operation == "schedule_confirm":
        assert preview.after["provenance"] == "confirmed"
    elif operation == "schedule_remove":
        assert preview.after is None
    else:
        assert preview.after["is_active"] == (operation == "schedule_reactivate")
    undo = service.undo_planning_proposal(
        proposal.proposal_id, ledger_db=db, expected_after_revision=result.details["after_revision"]
    )
    assert undo.ok, undo.errors
    after = _discovery(db, "schedule")["entries"][0]
    assert {k: v for k, v in before.items() if k != "revision"} == {
        k: v for k, v in after.items() if k != "revision"
    }
    with closing(sqlite3.connect(db)) as connection:
        assert (
            connection.execute("SELECT count(*) FROM planned_schedule_revisions").fetchone()[0] == 3
        )
        assert connection.execute("SELECT count(*) FROM transactions").fetchone()[0] == 0


@pytest.mark.parametrize("scope", ["merchant", "category"])
@pytest.mark.parametrize("operation", ["preference_set", "preference_change", "preference_remove"])
def test_preference_operations_preserve_facts_and_undo_only_target(
    tmp_path: Path, scope: str, operation: str
) -> None:
    db, service = _setup(tmp_path)
    subject = _discovery(db, scope)["entries"][0]
    intent = {
        "operation": "preference_set",
        "scope": scope,
        "subject": _ref(subject),
        "disposition": "worthwhile",
        "note": "Private note",
    }
    if operation != "preference_set":
        _apply(service, db, intent)
        target = _discovery(db, "preference")["entries"][0]
        intent = {"operation": operation, "target": _ref(target)}
        if operation == "preference_change":
            intent["disposition"] = "necessary"
    proposal, preview, result = _apply(service, db, intent)
    if operation == "preference_change":
        assert preview.after["note"] == "Private note"
        assert preview.after["disposition"] == "necessary"
    assert service.undo_planning_proposal(
        proposal.proposal_id, ledger_db=db, expected_after_revision=result.details["after_revision"]
    ).ok
    restored = _discovery(db, "preference")["entries"]
    if operation == "preference_set":
        assert restored == []
    else:
        assert restored[0]["disposition"] == "worthwhile"


def test_stale_preview_and_later_edit_undo_are_refused(tmp_path: Path) -> None:
    db, service = _setup(tmp_path)
    initial, _preview, result = _apply(service, db, _budget_intent(db))
    target = _discovery(db, "budget")["entries"][0]
    update = {
        **_budget_intent(db),
        "operation": "budget_update",
        "target": _ref(target),
        "amount": "222.00",
    }
    stale = service.file_planning_change(update)
    reviewed = service.preview_planning_proposal(stale.proposal_id, ledger_db=db)
    _apply(service, db, {**update, "amount": "333.00"})
    rejected = service.apply_planning_proposal(
        stale.proposal_id,
        ledger_db=db,
        expected_proposal_revision=stale.proposal_revision,
        expected_preview_revision=reviewed.preview_revision,
    )
    assert not rejected.ok
    undo = service.undo_planning_proposal(
        initial.proposal_id, ledger_db=db, expected_after_revision=result.details["after_revision"]
    )
    assert undo.status == "conflict"
    assert _discovery(db, "budget")["entries"][0]["amount"] == "333.00"


def test_apply_binds_proposal_and_preview_and_preserves_unrelated_edits(tmp_path: Path) -> None:
    db, service = _setup(tmp_path)
    proposal = service.file_planning_change(_budget_intent(db))
    preview = service.preview_planning_proposal(proposal.proposal_id, ledger_db=db)
    bad = service.apply_planning_proposal(
        proposal.proposal_id,
        ledger_db=db,
        expected_proposal_revision="a" * 64,
        expected_preview_revision=preview.preview_revision,
    )
    assert bad.status == "conflict"
    bad_preview = service.apply_planning_proposal(
        proposal.proposal_id,
        ledger_db=db,
        expected_proposal_revision=proposal.proposal_revision,
        expected_preview_revision="a" * 64,
    )
    assert bad_preview.status == "conflict"
    _apply(service, db, {**_budget_intent(db), "budget_month": "2030-02"})
    valid = service.apply_planning_proposal(
        proposal.proposal_id,
        ledger_db=db,
        expected_proposal_revision=proposal.proposal_revision,
        expected_preview_revision=preview.preview_revision,
    )
    assert valid.ok, valid.errors
    assert len(_discovery(db, "budget")["entries"]) == 2


def test_interrupted_apply_recovers_from_receipt_without_applying_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, service = _setup(tmp_path)
    proposal = service.file_planning_change(_budget_intent(db))
    preview = service.preview_planning_proposal(proposal.proposal_id, ledger_db=db)
    original = service._mark_planning_status

    def interrupted(*_args):
        raise OSError("Synthetic proposal-store tail interruption")

    monkeypatch.setattr(service, "_mark_planning_status", interrupted)
    args = {
        "ledger_db": db,
        "expected_proposal_revision": proposal.proposal_revision,
        "expected_preview_revision": preview.preview_revision,
    }
    result = service.apply_planning_proposal(proposal.proposal_id, **args)
    assert result.status == "committed_unconfirmed"
    assert service.show_planning_proposal(proposal.proposal_id).status == "pending"
    monkeypatch.setattr(service, "_mark_planning_status", original)
    recovered = service.apply_planning_proposal(proposal.proposal_id, **args)
    assert recovered.ok and recovered.details["recovered_from_receipt"]
    with closing(sqlite3.connect(db)) as connection:
        assert connection.execute("SELECT count(*) FROM budgets").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT count(*) FROM db_operation_audit WHERE operation='planning_proposal_apply'"
            ).fetchone()[0]
            == 1
        )


def test_financial_failure_rolls_back_mutation_and_receipt_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, service = _setup(tmp_path)
    proposal = service.file_planning_change(_budget_intent(db))
    preview = service.preview_planning_proposal(proposal.proposal_id, ledger_db=db)

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("Synthetic pre-commit failure")

    monkeypatch.setattr("ledger.proposals.planning_apply.record_db_operation", fail_audit)
    with pytest.raises(RuntimeError, match="Synthetic"):
        service.apply_planning_proposal(
            proposal.proposal_id,
            ledger_db=db,
            expected_proposal_revision=proposal.proposal_revision,
            expected_preview_revision=preview.preview_revision,
        )
    with closing(sqlite3.connect(db)) as connection:
        assert connection.execute("SELECT count(*) FROM budgets").fetchone()[0] == 0
        assert (
            connection.execute("SELECT count(*) FROM planning_proposal_receipts").fetchone()[0] == 0
        )
    assert service.show_planning_proposal(proposal.proposal_id).status == "pending"


def test_interrupted_undo_recovers_without_repeating_inverse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, service = _setup(tmp_path)
    proposal, _preview, applied = _apply(service, db, _budget_intent(db))
    original = service._mark_planning_status

    def interrupted(*_args):
        raise OSError("Synthetic undo tail interruption")

    monkeypatch.setattr(service, "_mark_planning_status", interrupted)
    result = service.undo_planning_proposal(
        proposal.proposal_id,
        ledger_db=db,
        expected_after_revision=applied.details["after_revision"],
    )
    assert result.status == "committed_unconfirmed"
    monkeypatch.setattr(service, "_mark_planning_status", original)
    retry = service.undo_planning_proposal(
        proposal.proposal_id,
        ledger_db=db,
        expected_after_revision=applied.details["after_revision"],
    )
    assert retry.ok and retry.details["already_undone"]
    with closing(sqlite3.connect(db)) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM db_operation_audit WHERE operation='planning_proposal_undo'"
            ).fetchone()[0]
            == 1
        )


def test_exact_lookup_discloses_matches_without_returning_private_labels(tmp_path: Path) -> None:
    db, service = _setup(tmp_path)
    _apply(service, db, _schedule_intent(db))
    from ledger.planning.discovery import discover_local_planning_definitions

    result = discover_local_planning_definitions(
        db, kind="schedule", exact_name=" PRIVATE   expected rent "
    )
    assert result["matched_count"] == 1
    assert "Private expected rent" not in json.dumps(result)
    assert "Private source note" not in json.dumps(result)
    assert (
        discover_local_planning_definitions(db, kind="schedule", exact_name="missing")["status"]
        == "no_match"
    )


def test_schedule_revision_binds_history_even_when_definition_is_identical(tmp_path: Path) -> None:
    db, service = _setup(tmp_path)
    _apply(service, db, _schedule_intent(db))
    before = _discovery(db, "schedule")["entries"][0]
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute("UPDATE planned_schedule_revisions SET effective_date='2099-01-01'")
    after = _discovery(db, "schedule")["entries"][0]
    assert after["ref"] == before["ref"]
    assert after["revision"] != before["revision"]


def test_discovery_no_match_scan_is_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, _service = _setup(tmp_path)
    from ledger.planning.discovery import discover_local_planning_definitions

    monkeypatch.setattr("ledger.planning.discovery.MAX_DISCOVERY_SOURCE_ROWS", 1)
    with pytest.raises(ValueError, match="too broad"):
        discover_local_planning_definitions(db, kind="category", exact_name="missing")


def test_budget_preview_quantifies_limit_delta_and_immediate_effect(tmp_path: Path) -> None:
    db, service = _setup(tmp_path)
    _apply(service, db, _budget_intent(db))
    target = _discovery(db, "budget")["entries"][0]
    proposal = service.file_planning_change(
        {
            **_budget_intent(db),
            "operation": "budget_update",
            "target": _ref(target),
            "amount": "200.00",
        }
    )
    preview = service.preview_planning_proposal(proposal.proposal_id, ledger_db=db)
    assert preview.effective_date == date.today().isoformat()
    [change] = preview.impact["budget_limit_changes"]
    assert (
        change["budget_month"],
        change["before_limit"],
        change["after_limit"],
        change["delta"],
    ) == ("*", "123.45", "200.00", "76.55")


def test_payment_preview_quantifies_both_cash_legs_without_new_spending(tmp_path: Path) -> None:
    db, service = _setup(tmp_path)
    intent = _schedule_intent(db)
    card = next(row for row in _discovery(db, "account")["entries"] if row["is_credit_card"])
    intent.update(
        event_type="card_payment",
        counterparty_account=_ref(card),
        frequency="once",
        amount="456.78",
    )
    proposal = service.file_planning_change(intent)
    preview = service.preview_planning_proposal(proposal.proposal_id, ledger_db=db)
    assert sorted(row["delta"] for row in preview.impact["account_movements"]) == [
        "-456.78",
        "456.78",
    ]
    assert preview.impact["planned_spending_delta"] == "0.00"
    assert preview.impact["after_next_occurrences"] == [intent["next_date"]]


@pytest.mark.parametrize("kind", ["budget", "preference"])
@pytest.mark.parametrize("via_editor", [False, True])
def test_removed_logical_target_aba_blocks_stale_undo(
    tmp_path: Path, kind: str, via_editor: bool
) -> None:
    db, service = _setup(tmp_path)
    if kind == "budget":
        create = _budget_intent(db)
    else:
        create = {
            "operation": "preference_set",
            "scope": "category",
            "subject": _ref(_discovery(db, "category")["entries"][0]),
            "disposition": "worthwhile",
        }
    _apply(service, db, create)
    first_target = _discovery(db, kind)["entries"][0]
    removed, _preview, removal = _apply(
        service, db, {"operation": kind + "_remove", "target": _ref(first_target)}
    )
    if via_editor:
        if kind == "budget":
            from decimal import Decimal

            from ledger.planning.service import BudgetService

            editor = BudgetService(db, prepare_schema=False)
            assert editor.replace_budgets(
                [{"month": "*", "category": "Dining", "amount": Decimal("999.00"), "notes": ""}]
            ).ok
            assert editor.replace_budgets([]).ok
        else:
            from ledger.insights.preferences import SpendingPreferenceService

            editor = SpendingPreferenceService(db, prepare_schema=False)
            editor.upsert("category", "Dining", "necessary")
            assert editor.delete("category", "Dining")
    else:
        create.update({"amount": "999.00"} if kind == "budget" else {"disposition": "necessary"})
        _apply(service, db, create)
        later_target = _discovery(db, kind)["entries"][0]
        _apply(service, db, {"operation": kind + "_remove", "target": _ref(later_target)})
    undo = service.undo_planning_proposal(
        removed.proposal_id, ledger_db=db, expected_after_revision=removal.details["after_revision"]
    )
    assert undo.status == "conflict", "An earlier removal must not undo a later deliberate absence."
    assert _discovery(db, kind)["entries"] == []


def test_interrupted_financial_commit_cannot_be_rejected_before_receipt_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db, service = _setup(tmp_path)
    proposal = service.file_planning_change(_budget_intent(db))
    preview = service.preview_planning_proposal(proposal.proposal_id, ledger_db=db)
    original = service._mark_planning_status

    def interrupted(*_args):
        raise OSError("Synthetic committed finance interrupted store tail")

    monkeypatch.setattr(service, "_mark_planning_status", interrupted)
    args = {
        "ledger_db": db,
        "expected_proposal_revision": proposal.proposal_revision,
        "expected_preview_revision": preview.preview_revision,
    }
    result = service.apply_planning_proposal(proposal.proposal_id, **args)
    assert result.status == "committed_unconfirmed"
    with pytest.raises(ValueError, match=r"application|recover|receipt"):
        service.reject_planning_proposal(
            proposal.proposal_id, expected_proposal_revision=proposal.proposal_revision
        )
    monkeypatch.setattr(service, "_mark_planning_status", original)
    recovery = service.apply_planning_proposal(proposal.proposal_id, **args)
    assert recovery.ok
    assert service.show_planning_proposal(proposal.proposal_id).status == "applied"
    assert len(_discovery(db, "budget")["entries"]) == 1


@pytest.mark.parametrize("source", ["membership", "transaction"])
def test_known_merchant_without_rule_can_receive_a_preference(tmp_path: Path, source: str) -> None:
    db, service = _setup(tmp_path)
    from ledger.planning.discovery import discover_local_planning_definitions

    with closing(sqlite3.connect(db)) as connection, connection:
        if source == "membership":
            connection.execute(
                "INSERT INTO category_merchants (category_name,merchant,sort_order) VALUES ('Dining','SYNTHETIC STREAMING',9)"
            )
        else:
            from test_review_teach import _insert_txn

            _insert_txn(db, "known-merchant")
            connection.execute(
                "UPDATE transactions SET merchant='SYNTHETIC STREAMING' WHERE txn_id='known-merchant'"
            )
    found = discover_local_planning_definitions(
        db, kind="merchant", exact_name="Synthetic Streaming"
    )
    assert found["matched_count"] == 1
    entry = found["entries"][0]
    assert "SYNTHETIC STREAMING" not in json.dumps(found)
    proposal, preview, _result = _apply(
        service,
        db,
        {
            "operation": "preference_set",
            "scope": "merchant",
            "subject": _ref(entry),
            "disposition": "worthwhile",
        },
    )
    assert preview.after["name"] == "SYNTHETIC STREAMING"
    assert service.show_planning_proposal(proposal.proposal_id).status == "applied"


def _open_run(db: Path, directory: Path) -> None:
    from ledger.imports.pdf.files import write_run_state

    directory.mkdir(parents=True, mode=0o700)
    write_run_state(
        directory,
        {
            "version": 1,
            "status": "needs_review",
            "target_db_path": str(db),
            "staging_db_path": str(directory / "staging.db"),
        },
    )


@pytest.mark.parametrize("custom_path", [False, True])
@pytest.mark.parametrize("action", ["apply", "undo"])
def test_planning_financial_writes_refuse_promotable_imports(
    tmp_path: Path, custom_path: bool, action: str
) -> None:
    from ledger.imports.pdf.readiness import LiveMutationDuringOpenImportRefused

    db, service = _setup(tmp_path)
    if action == "undo":
        proposal, preview, result = _apply(service, db, _budget_intent(db))
    else:
        proposal = service.file_planning_change(_budget_intent(db))
        preview = service.preview_planning_proposal(proposal.proposal_id, ledger_db=db)
    run = (tmp_path / "custom" / "runs" if custom_path else db.parent / "import-runs") / "open"
    _open_run(db, run)
    before = db.read_bytes()
    with pytest.raises(LiveMutationDuringOpenImportRefused):
        if action == "undo":
            service.undo_planning_proposal(
                proposal.proposal_id,
                ledger_db=db,
                expected_after_revision=result.details["after_revision"],
            )
        else:
            service.apply_planning_proposal(
                proposal.proposal_id,
                ledger_db=db,
                expected_proposal_revision=proposal.proposal_revision,
                expected_preview_revision=preview.preview_revision,
            )
    assert db.read_bytes() == before
    if action == "apply":
        assert (
            service.reject_planning_proposal(
                proposal.proposal_id, expected_proposal_revision=proposal.proposal_revision
            ).status
            == "rejected"
        )


def test_failed_recovery_gate_preserves_committed_application_reservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ledger.imports.pdf.readiness import LiveMutationDuringOpenImportRefused

    db, service = _setup(tmp_path)
    proposal = service.file_planning_change(_budget_intent(db))
    preview = service.preview_planning_proposal(proposal.proposal_id, ledger_db=db)
    original = service._mark_planning_status

    def interrupted(*_args):
        raise OSError("Synthetic tail interruption")

    monkeypatch.setattr(service, "_mark_planning_status", interrupted)
    args = {
        "ledger_db": db,
        "expected_proposal_revision": proposal.proposal_revision,
        "expected_preview_revision": preview.preview_revision,
    }
    assert (
        service.apply_planning_proposal(proposal.proposal_id, **args).status
        == "committed_unconfirmed"
    )
    monkeypatch.setattr(service, "_mark_planning_status", original)
    recovery = service.preview_planning_proposal(proposal.proposal_id, ledger_db=db)
    assert recovery.recovery_required
    assert recovery.application_preview_revision == preview.preview_revision
    _open_run(db, tmp_path / "custom" / "open")
    with pytest.raises(LiveMutationDuringOpenImportRefused):
        service.apply_planning_proposal(proposal.proposal_id, **args)
    with pytest.raises(ValueError, match="recovery"):
        service.reject_planning_proposal(
            proposal.proposal_id, expected_proposal_revision=proposal.proposal_revision
        )


@pytest.mark.parametrize("effective_offset", [2, 59, 60, 61])
def test_confirmation_preview_respects_future_pattern_removal_boundary(
    tmp_path: Path, effective_offset: int
) -> None:
    from dataclasses import replace

    from test_cash_planning import _draft
    from test_review_teach import _insert_txn

    from ledger.core.account_identity import account_identity
    from ledger.imports.pdf.readiness import _selected_table_fingerprint
    from ledger.planning.discovery import local_reference
    from ledger.planning.schedules import ScheduleService

    db, service = _setup(tmp_path)
    schedules = ScheduleService(db, prepare_schema=False)
    tomorrow = date.today() + timedelta(days=1)
    removal_date = date.today() + timedelta(days=60)
    draft = _draft(
        {"chequing": account_identity("test", "test-chequing")},
        next_date=tomorrow,
        provenance="confirmed",
        pattern_id="synthetic-reserved-pattern",
    )
    original = schedules.create(draft, schedule_id="original", effective_date=tomorrow)
    schedules.delete("original", expected_record=original, effective_date=removal_date)
    schedules.create(
        replace(draft, name="Replacement", provenance="hypothetical"),
        schedule_id="replacement",
        effective_date=tomorrow,
    )
    unrelated = schedules.create(
        replace(draft, name="Unrelated", pattern_id=None),
        schedule_id="unrelated",
        effective_date=tomorrow,
    )
    _insert_txn(db, "unrelated-imported-row")
    _apply(service, db, _budget_intent(db))
    unaffected_tables = {"transactions", "budgets", "spending_preferences", "category_config"}
    unaffected_before = _selected_table_fingerprint(db, unaffected_tables)
    with closing(sqlite3.connect(db)) as connection:
        old_history = connection.execute(
            "SELECT * FROM planned_schedule_revisions WHERE schedule_id='original' ORDER BY revision_id"
        ).fetchall()
    proposal = service.file_planning_change(
        {
            "operation": "schedule_confirm",
            "target": local_reference(db, "schedule", "replacement"),
            "effective_date": (date.today() + timedelta(days=effective_offset)).isoformat(),
        }
    )
    financial_before = db.read_bytes()
    guarded_tables = unaffected_tables | {
        "planned_schedules",
        "planned_schedule_revisions",
        "planning_proposal_receipts",
        "db_operation_audit",
    }
    guarded_before = _selected_table_fingerprint(db, guarded_tables)
    preview = service.preview_planning_proposal(proposal.proposal_id, ledger_db=db)
    assert preview.can_apply is (effective_offset >= 60)
    assert db.read_bytes() == financial_before
    result = service.apply_planning_proposal(
        proposal.proposal_id,
        ledger_db=db,
        expected_proposal_revision=proposal.proposal_revision,
        expected_preview_revision=preview.preview_revision,
    )
    if effective_offset < 60:
        assert "overlapping confirmed schedule" in " ".join(preview.validation_errors)
        assert result.status == "validation_failed"
        assert _selected_table_fingerprint(db, guarded_tables) == guarded_before
        assert service.show_planning_proposal(proposal.proposal_id).status == "pending"
    else:
        assert preview.validation_errors == ()
        assert result.ok, result.errors
        replacement = next(row for row in schedules.list() if row.schedule_id == "replacement")
        assert replacement.provenance == "confirmed"
    assert next(row for row in schedules.list() if row.schedule_id == "unrelated") == unrelated
    assert _selected_table_fingerprint(db, unaffected_tables) == unaffected_before
    with closing(sqlite3.connect(db)) as connection:
        assert (
            connection.execute(
                "SELECT * FROM planned_schedule_revisions WHERE schedule_id='original' ORDER BY revision_id"
            ).fetchall()
            == old_history
        )


@pytest.mark.parametrize(
    "wrong_target", ["independent", "clone", "replaced_original", "legacy", "legacy_clone"]
)
def test_interrupted_application_reservation_stays_bound_to_original_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wrong_target: str
) -> None:
    import shutil

    original_root = tmp_path / "original"
    original_root.mkdir(mode=0o700)
    db, service = _setup(original_root)
    proposal = service.file_planning_change(_budget_intent(db))
    preview = service.preview_planning_proposal(proposal.proposal_id, ledger_db=db)
    original_tail = service._mark_planning_status

    def interrupted(*_args):
        raise OSError("Synthetic committed financial change interrupted before status tail")

    monkeypatch.setattr(service, "_mark_planning_status", interrupted)
    approvals = {
        "expected_proposal_revision": proposal.proposal_revision,
        "expected_preview_revision": preview.preview_revision,
    }
    assert (
        service.apply_planning_proposal(proposal.proposal_id, ledger_db=db, **approvals).status
        == "committed_unconfirmed"
    )
    monkeypatch.setattr(service, "_mark_planning_status", original_tail)
    if wrong_target.startswith("legacy"):
        with closing(sqlite3.connect(service.proposals_db)) as connection, connection:
            connection.execute(
                "ALTER TABLE planning_proposal_applications DROP COLUMN ledger_target"
            )
    source_before = db.read_bytes()
    other_root = tmp_path / "other"
    other_root.mkdir(mode=0o700)
    if wrong_target in {"clone", "legacy_clone"}:
        other_db = other_root / "ledger.db"
        shutil.copy2(db, other_db)
    else:
        other_db, _other_service = _setup(other_root)
    if wrong_target == "replaced_original":
        backup = original_root / "original-ledger.db"
        shutil.copy2(db, backup)
        shutil.copy2(other_db, db)
        other_db = db
    wrong_before = other_db.read_bytes()
    retry = service.apply_planning_proposal(proposal.proposal_id, ledger_db=other_db, **approvals)
    assert retry.status == "conflict"
    assert other_db.read_bytes() == wrong_before
    assert service.show_planning_proposal(proposal.proposal_id).status == "pending"
    with pytest.raises(ValueError, match="recovery"):
        service.reject_planning_proposal(
            proposal.proposal_id, expected_proposal_revision=proposal.proposal_revision
        )
    if wrong_target == "replaced_original":
        shutil.copy2(backup, db)
    assert db.read_bytes() == source_before
    if wrong_target == "clone":
        # Normal publication changes the inode at the original pathname while
        # preserving its financial lineage and must not strand safe recovery.
        publication = db.with_name("publication.db")
        shutil.copy2(db, publication)
        publication.replace(db)
    recovered = service.apply_planning_proposal(proposal.proposal_id, ledger_db=db, **approvals)
    if wrong_target.startswith("legacy"):
        # With no retained target identity, even an exact receipt cannot prove
        # this is the original database rather than a clone. Keep the claim
        # instead of guessing a target during an additive store upgrade.
        assert recovered.status == "conflict"
        assert "binding" in " ".join(recovered.errors).lower()
        assert service.show_planning_proposal(proposal.proposal_id).status == "pending"
        with pytest.raises(ValueError, match="recovery"):
            service.reject_planning_proposal(
                proposal.proposal_id, expected_proposal_revision=proposal.proposal_revision
            )
        assert db.read_bytes() == source_before
        return
    assert recovered.ok, recovered.errors
    assert recovered.details["recovered_from_receipt"]
    assert service.show_planning_proposal(proposal.proposal_id).status == "applied"
    with closing(sqlite3.connect(db)) as connection:
        assert connection.execute("SELECT count(*) FROM budgets").fetchone()[0] == 1
        assert (
            connection.execute("SELECT count(*) FROM planning_proposal_receipts").fetchone()[0] == 1
        )


def test_interrupted_application_reservation_refuses_undo_against_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    db, service = _setup(tmp_path)
    proposal = service.file_planning_change(_budget_intent(db))
    preview = service.preview_planning_proposal(proposal.proposal_id, ledger_db=db)
    original_tail = service._mark_planning_status

    def interrupted(*_args):
        raise OSError("Synthetic financial commit interrupted before proposal status")

    monkeypatch.setattr(service, "_mark_planning_status", interrupted)
    approvals = {
        "expected_proposal_revision": proposal.proposal_revision,
        "expected_preview_revision": preview.preview_revision,
    }
    assert (
        service.apply_planning_proposal(proposal.proposal_id, ledger_db=db, **approvals).status
        == "committed_unconfirmed"
    )
    monkeypatch.setattr(service, "_mark_planning_status", original_tail)
    receipt_revision = service.preview_planning_proposal(
        proposal.proposal_id, ledger_db=db
    ).receipt_after_revision
    clone = db.with_name("clone.db")
    shutil.copy2(db, clone)
    before = db.read_bytes()
    result = service.undo_planning_proposal(
        proposal.proposal_id, ledger_db=clone, expected_after_revision=receipt_revision
    )
    assert result.status == "conflict"
    assert clone.read_bytes() == db.read_bytes() == before
    with pytest.raises(ValueError, match="recovery"):
        service.reject_planning_proposal(
            proposal.proposal_id, expected_proposal_revision=proposal.proposal_revision
        )
    assert service.apply_planning_proposal(proposal.proposal_id, ledger_db=db, **approvals).ok


@pytest.mark.parametrize("action", ["apply", "undo"])
def test_completed_application_target_binding_survives_replay_and_undo(
    tmp_path: Path, action: str
) -> None:
    import shutil

    db, service = _setup(tmp_path)
    proposal, preview, result = _apply(service, db, _budget_intent(db))
    approvals = {
        "expected_proposal_revision": proposal.proposal_revision,
        "expected_preview_revision": preview.preview_revision,
    }
    after_revision = result.details["after_revision"]

    def replay(target: Path):
        if action == "apply":
            return service.apply_planning_proposal(
                proposal.proposal_id, ledger_db=target, **approvals
            )
        return service.undo_planning_proposal(
            proposal.proposal_id, ledger_db=target, expected_after_revision=after_revision
        )

    for status in ("applied", "undone"):
        clone = db.with_name("clone.db")
        shutil.copy2(db, clone)
        original_before = db.read_bytes()
        refused = replay(clone)
        assert refused.status == "conflict"
        assert "original" in " ".join(refused.errors).lower()
        assert clone.read_bytes() == db.read_bytes() == original_before
        assert service.show_planning_proposal(proposal.proposal_id).status == status
        assert not service.preview_planning_proposal(
            proposal.proposal_id, ledger_db=db
        ).recovery_required
        with closing(sqlite3.connect(service.proposals_db)) as connection:
            assert connection.execute(
                "SELECT count(*) FROM planning_proposal_applications"
            ).fetchone() == (0,)
            binding = connection.execute(
                "SELECT * FROM planning_proposal_targets WHERE proposal_id=?",
                (proposal.proposal_id,),
            ).fetchone()
        assert binding is not None
        if status == "applied":
            original_binding = binding
        else:
            assert binding == original_binding

        # A normal publication may replace the original inode while retaining
        # its lineage, pathname, receipt, and immutable application binding.
        publication = db.with_name("publication.db")
        shutil.copy2(db, publication)
        publication.replace(db)
        recovered = service.apply_planning_proposal(proposal.proposal_id, ledger_db=db, **approvals)
        assert recovered.ok, recovered.errors
        assert recovered.details["applied_state"] == status
        undone = service.undo_planning_proposal(
            proposal.proposal_id, ledger_db=db, expected_after_revision=after_revision
        )
        assert undone.ok, undone.errors
        assert undone.details["already_undone"] is (status == "undone")
    with closing(sqlite3.connect(db)) as connection:
        assert connection.execute("SELECT count(*) FROM budgets").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM planning_proposal_receipts").fetchone() == (
            1,
        )


@pytest.mark.parametrize("status", ["applied", "undone"])
@pytest.mark.parametrize("action", ["apply", "undo"])
def test_legacy_completed_application_without_origin_binding_fails_closed(
    tmp_path: Path, status: str, action: str
) -> None:
    db, service = _setup(tmp_path)
    proposal, preview, applied = _apply(service, db, _budget_intent(db))
    if status == "undone":
        assert service.undo_planning_proposal(
            proposal.proposal_id,
            ledger_db=db,
            expected_after_revision=applied.details["after_revision"],
        ).ok
    with closing(sqlite3.connect(service.proposals_db)) as connection, connection:
        connection.execute("DROP TABLE IF EXISTS planning_proposal_targets")
    before = db.read_bytes()
    if action == "apply":
        result = service.apply_planning_proposal(
            proposal.proposal_id,
            ledger_db=db,
            expected_proposal_revision=proposal.proposal_revision,
            expected_preview_revision=preview.preview_revision,
        )
    else:
        result = service.undo_planning_proposal(
            proposal.proposal_id,
            ledger_db=db,
            expected_after_revision=applied.details["after_revision"],
        )
    assert result.status == "conflict"
    assert "binding" in " ".join(result.errors).lower()
    assert db.read_bytes() == before
    assert service.show_planning_proposal(proposal.proposal_id).status == status
