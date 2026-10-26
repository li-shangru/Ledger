"""Plan expectations and imported evidence use only disposable synthetic fixtures."""

from __future__ import annotations

import importlib
import json
import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal

import pytest
from test_cash_planning import (
    _account,
    _add_pattern,
    _add_same_sign_card_payment_pair,
    _add_transaction,
    _add_transfer_pair,
    _draft,
)

from ledger.core.account_identity import account_identity
from ledger.planning.cash_plan import CashPlanService
from ledger.planning.schedules import ScheduleService
from ledger.storage.database import session_scope
from ledger.storage.migrations import ensure_schema


@pytest.fixture
def plan_db(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.db"
    ensure_schema(path)
    ids = {"chequing": account_identity("test", "daily-chequing")}
    with session_scope(path) as session:
        session.add(_account("test", "daily-chequing"))
    monkeypatch.setattr("ledger.planning.schedules.naive_utc_now", lambda: datetime(2026, 9, 1, 12))
    return path, ids


def _schedule(path, ids, schedule_id="internet", **changes):
    values = {
        "name": "Synthetic Internet",
        "amount": Decimal("100.00"),
        "next_date": date(2026, 9, 10),
        "frequency": "monthly",
        "provenance": "confirmed",
    }
    values.update(changes)
    draft = _draft(ids, **values)
    return ScheduleService(path, prepare_schema=False).create(draft, schedule_id=schedule_id)


def _service(path):
    assert importlib.util.find_spec("ledger.planning.reconciliation") is not None
    return importlib.import_module("ledger.planning.reconciliation").PlanReconciliationService(path)


def _build(path, start=date(2026, 9, 1), end=date(2026, 9, 30), as_of=date(2026, 10, 1)):
    return _service(path).build(start, end, as_of=as_of)


def _coverage(path, ids, start=date(2026, 9, 1), end=date(2026, 9, 30)):
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "INSERT INTO statement_coverage_intervals "
            "(interval_id,institution,account_slug,start_date,end_date,evidence_basis,imported_at) "
            "VALUES (?,?,?,?,?,'printed_statement_period',?)",
            (
                f"interval-{start}-{end}",
                "test",
                "daily-chequing",
                start.isoformat(),
                end.isoformat(),
                "2026-10-01 00:00:00",
            ),
        )


def test_future_edit_and_remove_preserve_elapsed_expectations_and_cash_plan(plan_db):
    path, ids = plan_db
    record = _schedule(path, ids)
    service = ScheduleService(path, prepare_schema=False)
    assert hasattr(service, "write_on_connection")
    service.update(
        record.schedule_id,
        replace(
            _draft(ids),
            name=record.name,
            amount=Decimal("125"),
            next_date=record.next_date,
            provenance="confirmed",
        ),
        effective_date=date(2026, 10, 1),
    )
    service.delete(record.schedule_id, effective_date=date(2026, 11, 1))
    assert service.list() == ()
    findings = _build(path, end=date(2026, 11, 30), as_of=date(2026, 12, 1))["findings"]
    assert [(f["expected_date"], f["expected_amount"]) for f in findings] == [
        ("2026-09-10", "100.00"),
        ("2026-10-10", "125.00"),
    ]
    plan = CashPlanService(path, prepare_schema=False).build(
        start_date=date(2026, 9, 1), as_of=date(2026, 9, 1), end_date=date(2026, 11, 30)
    )
    assert [(leg.occurrence_date, leg.amount) for leg in plan.planning_legs] == [
        (date(2026, 9, 10), Decimal("-100.00")),
        (date(2026, 10, 10), Decimal("-125.00")),
    ]


def test_amount_and_date_differences_are_independent_and_review_binds_material_evidence(plan_db):
    path, ids = plan_db
    _schedule(path, ids)
    _add_transaction(
        path,
        txn_id="internet-paid",
        on=date(2026, 9, 12),
        amount="-105.00",
        cashflow_type="expense",
        merchant="Synthetic Internet",
    )
    [finding] = _build(path)["findings"]
    assert finding["status"] == "amount_and_date_difference"
    assert finding["amount_changed"] is True and finding["date_changed"] is True
    assert finding["amount_difference"] == "5.00"
    assert finding["date_difference"] == 2
    assert finding["evidence_ids"] == ["internet-paid"]
    _service(path).mark_reviewed(
        date(2026, 9, 1),
        date(2026, 9, 30),
        as_of=date(2026, 10, 1),
        occurrence_id=finding["occurrence_id"],
        material_revision=finding["material_revision"],
    )
    assert _build(path, as_of=date(2026, 10, 2))["findings"][0]["reviewed"] is True
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE transactions SET amount_signed=-110 WHERE txn_id='internet-paid'")
    changed = _build(path)["findings"][0]
    assert changed["reviewed"] is False
    assert changed["material_revision"] != finding["material_revision"]


def test_matching_requires_identity_and_never_reuses_competing_event_even_across_period_edge(
    plan_db,
):
    path, ids = plan_db
    _schedule(path, ids, next_date=date(2026, 9, 1), frequency="once")
    _schedule(path, ids, "competing", next_date=date(2026, 9, 3), frequency="once")
    _add_transaction(
        path,
        txn_id="one-event",
        on=date(2026, 9, 2),
        amount="-100.00",
        cashflow_type="expense",
        merchant="Synthetic Internet",
    )
    findings = _build(path)["findings"]
    assert [f["status"] for f in findings] == ["ambiguous", "ambiguous"]
    assert _build(path, end=date(2026, 9, 1))["findings"][0]["status"] == "ambiguous"
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE transactions SET merchant='Other synthetic merchant'")
    assert all(f["actual_amount"] is None for f in _build(path)["findings"])


def test_missing_requires_exact_account_full_window_and_elapsed_window(plan_db):
    path, ids = plan_db
    _schedule(path, ids)
    assert _build(path)["findings"][0]["status"] == "coverage_gap"
    assert (
        _service(path).build(date(2026, 9, 1), date(2026, 9, 30), as_of=date(2026, 9, 12))[
            "findings"
        ][0]["status"]
        == "pending"
    )
    _coverage(path, ids, end=date(2026, 9, 12))
    assert _build(path)["findings"][0]["status"] == "coverage_gap"
    _coverage(path, ids, start=date(2026, 9, 13))
    finding = _build(path)["findings"][0]
    assert finding["status"] == "missing"
    assert finding["coverage"]["complete"] is True
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE statement_coverage_intervals SET institution='other-bank'")
    assert _build(path)["findings"][0]["status"] == "coverage_gap"


def test_multiple_candidates_are_ambiguous_and_approximate_match_never_suppresses_cash_plan(
    plan_db,
):
    path, ids = plan_db
    _schedule(path, ids)
    _add_transaction(
        path,
        txn_id="nearby",
        on=date(2026, 9, 10),
        amount="-101.00",
        cashflow_type="expense",
        merchant="Synthetic Internet",
    )
    assert _build(path)["findings"][0]["status"] == "amount_difference"
    cash_plan = CashPlanService(path, prepare_schema=False).build(
        start_date=date(2026, 9, 1), as_of=date(2026, 9, 10), end_date=date(2026, 9, 30)
    )
    assert cash_plan.suppressions == ()
    assert len(cash_plan.planning_legs) == 1
    _add_transaction(
        path,
        txn_id="exact",
        on=date(2026, 9, 10),
        amount="-100.00",
        cashflow_type="expense",
        merchant="Synthetic Internet",
    )
    assert _build(path)["findings"][0]["status"] == "ambiguous"


def test_raw_legacy_schedule_history_is_disclosed_and_never_claims_retrospective_missing(plan_db):
    path, ids = plan_db
    _schedule(path, ids)
    assert importlib.util.find_spec("ledger.planning.reconciliation") is not None
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("DELETE FROM planned_schedule_revisions")
    _coverage(path, ids)
    result = _build(path)
    assert result["disclosures"]["incomplete_history_schedule_count"] == 1
    assert result["findings"][0]["history_complete"] is False
    assert result["findings"][0]["status"] == "coverage_gap"
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize("removed", [False, True])
def test_migrated_legacy_history_before_future_baseline_is_disclosed(plan_db, removed):
    from ledger.storage.migrations import run_migrations

    path, ids = plan_db
    record = _schedule(path, ids, next_date=date(2026, 10, 10))
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("DELETE FROM planned_schedule_revisions")
        conn.execute(
            "DELETE FROM schema_migrations WHERE version='20260904_0027_plan_reconciliation'"
        )
    run_migrations(path)
    if removed:
        ScheduleService(path, prepare_schema=False).delete(
            record.schedule_id, effective_date=date(2026, 11, 1)
        )
    result = _build(path)
    assert result["total_count"] == 0
    assert result["findings"] == []
    assert result["disclosures"]["incomplete_history_schedule_count"] == 1
    before_creation = _build(path, start=date(2026, 8, 1), end=date(2026, 8, 31))
    assert before_creation["disclosures"]["incomplete_history_schedule_count"] == 0


def test_known_new_future_schedule_does_not_imply_unknown_earlier_history(plan_db):
    path, ids = plan_db
    _schedule(path, ids, next_date=date(2026, 10, 10))
    result = _build(path)
    assert result["total_count"] == 0
    assert result["disclosures"]["incomplete_history_schedule_count"] == 0


def test_past_creation_is_disclosed_but_new_future_revision_has_complete_expectations(
    plan_db, monkeypatch
):
    path, ids = plan_db
    monkeypatch.setattr("ledger.planning.schedules.naive_utc_now", lambda: datetime(2026, 9, 20))
    record = _schedule(path, ids)
    _coverage(path, ids)
    earlier = _build(path)["findings"][0]
    assert earlier["history_complete"] is False
    assert earlier["status"] == "coverage_gap"
    service = ScheduleService(path, prepare_schema=False)
    service.update(
        record.schedule_id, service.draft_from_record(record), effective_date=date(2026, 10, 1)
    )
    _coverage(path, ids, start=date(2026, 10, 1), end=date(2026, 10, 31))
    future = _build(path, start=date(2026, 10, 1), end=date(2026, 10, 31), as_of=date(2026, 11, 1))[
        "findings"
    ][0]
    assert future["history_complete"] is True
    assert future["status"] == "missing"


def test_migrated_legacy_confirmation_establishes_future_known_history(plan_db):
    from ledger.storage.migrations import run_migrations

    path, ids = plan_db
    _schedule(path, ids, provenance="hypothetical")
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("DELETE FROM planned_schedule_revisions")
        conn.execute(
            "DELETE FROM schema_migrations WHERE version='20260904_0027_plan_reconciliation'"
        )
    run_migrations(path)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute(
            "SELECT history_complete FROM planned_schedule_revisions"
        ).fetchall() == [(0,)]
    ScheduleService(path, prepare_schema=False).confirm(
        "internet", effective_date=date(2026, 10, 1)
    )
    future = _build(path, end=date(2026, 10, 31), as_of=date(2026, 11, 1))
    assert future["disclosures"]["hypothetical_occurrence_count"] == 1
    assert future["findings"][0]["expected_date"] == "2026-10-10"
    assert future["findings"][0]["history_complete"] is True


@pytest.mark.parametrize(
    ("on", "amount", "status"),
    [
        (date(2026, 9, 10), "-100.00", "matched"),
        (date(2026, 9, 13), "-100.00", "date_difference"),
        (date(2026, 9, 10), "-120.00", "amount_difference"),
        (date(2026, 9, 14), "-100.00", "coverage_gap"),
        (date(2026, 9, 10), "-120.01", "coverage_gap"),
    ],
)
def test_exact_and_approximate_matching_respect_both_inclusive_bounds(plan_db, on, amount, status):
    path, ids = plan_db
    _schedule(path, ids)
    _add_transaction(
        path,
        txn_id="actual",
        on=on,
        amount=amount,
        cashflow_type="expense",
        merchant="Synthetic Internet",
    )
    assert _build(path)["findings"][0]["status"] == status


def test_semimonthly_calendar_and_cross_month_posting_evidence(plan_db):
    path, ids = plan_db
    _schedule(path, ids, next_date=date(2026, 9, 15), frequency="semimonthly")
    _add_transaction(
        path,
        txn_id="month-end-paid",
        on=date(2026, 10, 2),
        amount="-100.00",
        cashflow_type="expense",
        merchant="Synthetic Internet",
    )
    findings = _build(path, end=date(2026, 10, 31), as_of=date(2026, 11, 5))["findings"]
    assert [f["expected_date"] for f in findings] == [
        "2026-09-15",
        "2026-09-30",
        "2026-10-15",
        "2026-10-31",
    ]
    assert findings[1]["status"] == "date_difference"
    assert findings[1]["actual_date"] == "2026-10-02"


@pytest.mark.parametrize("card", [False, True])
def test_paired_transfers_are_one_event_with_opposite_account_effects(plan_db, card):
    path, ids = plan_db
    institution, slug = ("card-bank", "rewards-card") if card else ("test", "rainy-day")
    destination = account_identity(institution, slug)
    with session_scope(path) as session:
        session.add(
            _account(
                institution,
                slug,
                account_type="credit_card" if card else "savings",
                is_credit_card=card,
            )
        )
    _schedule(
        path,
        ids,
        event_type="card_payment" if card else "transfer",
        counterparty_account_id=destination,
    )
    if card:
        _add_same_sign_card_payment_pair(path, on=date(2026, 9, 10), amount="100.00")
    else:
        _add_transfer_pair(
            path, on=date(2026, 9, 10), amount="100.00", destination_on=date(2026, 9, 11)
        )
    finding = _build(path)["findings"][0]
    assert finding["status"] == ("matched" if card else "date_difference")
    assert len(finding["evidence_ids"]) == 2
    evidence = _service(path).evidence(finding["evidence_ids"])
    assert sorted(row["amount"] for row in evidence) == ["-100.00", "100.00"]
    assert {row["event_type"] for row in evidence} == {"card_payment" if card else "transfer"}
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("UPDATE transfers SET currency='USD'")
    assert _build(path)["findings"][0]["status"] == "coverage_gap"


def test_partial_transfer_is_ambiguous_even_when_both_statement_windows_are_covered(plan_db):
    path, ids = plan_db
    destination = account_identity("test", "rainy-day")
    with session_scope(path) as session:
        session.add(_account("test", "rainy-day", account_type="savings"))
    _schedule(path, ids, event_type="transfer", counterparty_account_id=destination)
    _add_transfer_pair(path, on=date(2026, 9, 10), amount="100.00")
    _coverage(path, ids)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("DELETE FROM transfers WHERE txn_id='transfer-in'")
        conn.execute(
            "INSERT INTO statement_coverage_intervals SELECT 'destination',institution,'rainy-day',start_date,end_date,evidence_basis,imported_at FROM statement_coverage_intervals"
        )
    finding = _build(path)["findings"][0]
    assert finding["coverage"]["complete"] is True
    assert finding["status"] == "ambiguous"
    assert finding["actual_amount"] is None
    assert finding["evidence_ids"] == ["transfer-out"]


def test_confirm_deactivate_and_reactivate_preserve_their_effective_regions(plan_db):
    path, ids = plan_db
    _schedule(path, ids, provenance="hypothetical")
    service = ScheduleService(path, prepare_schema=False)
    record = service.confirm("internet", effective_date=date(2026, 10, 1))
    draft = service.draft_from_record(record)
    service.update("internet", replace(draft, is_active=False), effective_date=date(2026, 11, 1))
    service.update("internet", draft, effective_date=date(2026, 12, 1))
    result = _build(path, end=date(2026, 12, 31), as_of=date(2027, 1, 1))
    assert [row["expected_date"] for row in result["findings"]] == ["2026-10-10", "2026-12-10"]
    assert result["disclosures"]["hypothetical_occurrence_count"] == 1


def test_review_survives_unrelated_import_but_stale_review_is_rejected(plan_db):
    from ledger.planning.reconciliation import ReconciliationConflictError

    path, ids = plan_db
    _schedule(path, ids)
    _coverage(path, ids)
    finding = _build(path)["findings"][0]
    service = _service(path)
    service.mark_reviewed(
        date(2026, 9, 1),
        date(2026, 9, 30),
        as_of=date(2026, 10, 1),
        occurrence_id=finding["occurrence_id"],
        material_revision=finding["material_revision"],
    )
    _add_transaction(
        path,
        txn_id="unrelated",
        on=date(2026, 9, 10),
        amount="-100",
        cashflow_type="expense",
        merchant="Other synthetic merchant",
    )
    assert _build(path)["findings"][0]["reviewed"] is True
    _add_transaction(
        path,
        txn_id="material",
        on=date(2026, 9, 10),
        amount="-100",
        cashflow_type="expense",
        merchant="Synthetic Internet",
    )
    with pytest.raises(ReconciliationConflictError):
        service.mark_reviewed(
            date(2026, 9, 1),
            date(2026, 9, 30),
            as_of=date(2026, 10, 1),
            occurrence_id=finding["occurrence_id"],
            material_revision=finding["material_revision"],
        )
    assert _build(path)["findings"][0]["reviewed"] is False


@pytest.mark.parametrize("publication", ["import", "repair"])
def test_real_publication_preserves_late_history_reviews_and_receipts(
    plan_db, tmp_path, publication
):
    from ledger.imports.pdf.files import promote_database
    from ledger.imports.pdf.readiness import import_owned_fingerprint, import_owned_match
    from ledger.repair.statements import promote_candidate
    from ledger.storage.database import PlanningProposalReceiptDB

    path, ids = plan_db
    baseline = import_owned_fingerprint(path)
    candidate = tmp_path / "candidate.db"
    ensure_schema(candidate)
    with session_scope(candidate) as session:
        session.add(_account("test", "daily-chequing"))
    _add_transaction(
        candidate,
        txn_id="new-imported-fact",
        on=date(2026, 9, 12),
        amount="-10",
        cashflow_type="expense",
        merchant="Other synthetic merchant",
    )
    _schedule(path, ids)
    finding = _build(path)["findings"][0]
    _service(path).mark_reviewed(
        date(2026, 9, 1),
        date(2026, 9, 30),
        as_of=date(2026, 10, 1),
        occurrence_id=finding["occurrence_id"],
        material_revision=finding["material_revision"],
    )
    with session_scope(path) as session:
        session.add(
            PlanningProposalReceiptDB(
                proposal_id="synthetic-receipt",
                proposal_revision="a" * 64,
                preview_revision="b" * 64,
                target_kind="schedule",
                target_id="internet",
                before_json=None,
                after_json='{"synthetic":true}',
                after_revision="c" * 64,
                applied_at=datetime(2026, 9, 1),
            )
        )
    tables = (
        "planned_schedules",
        "planned_schedule_revisions",
        "plan_reconciliation_reviews",
        "planning_proposal_receipts",
    )
    with closing(sqlite3.connect(path)) as conn:
        before = {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() for table in tables
        }
    assert import_owned_match(baseline, import_owned_fingerprint(path))
    if publication == "import":
        assert (
            promote_database(candidate, path, tmp_path / "backups", expected_import_owned=baseline)
            is not None
        )
    else:
        promote_candidate(candidate, path, expected_import_owned=baseline)
    with closing(sqlite3.connect(path)) as conn:
        after = {
            table: conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall() for table in tables
        }
        assert conn.execute("SELECT txn_id FROM transactions").fetchall() == [
            ("new-imported-fact",)
        ]
    assert before == after
    assert _build(path)["findings"][0]["reviewed"] is True


def test_future_correction_cannot_overwrite_a_newer_scheduled_revision(plan_db):
    from ledger.planning.reconciliation import ReconciliationConflictError

    path, ids = plan_db
    record = _schedule(path, ids)
    _add_transaction(
        path,
        txn_id="actual",
        on=date(2026, 9, 12),
        amount="-105",
        cashflow_type="expense",
        merchant="Synthetic Internet",
    )
    service = _service(path)
    finding = _build(path)["findings"][0]
    assert hasattr(service, "current_schedule_for_finding")
    parameters = {
        "as_of": date(2026, 10, 1),
        "occurrence_id": finding["occurrence_id"],
        "material_revision": finding["material_revision"],
    }
    assert (
        service.current_schedule_for_finding(date(2026, 9, 1), date(2026, 9, 30), **parameters)
        == record
    )
    schedules = ScheduleService(path, prepare_schema=False)
    schedules.update(
        "internet",
        replace(schedules.draft_from_record(record), amount=Decimal("115")),
        effective_date=date(2026, 10, 1),
    )
    assert _build(path)["findings"][0]["material_revision"] == finding["material_revision"]
    with pytest.raises(ReconciliationConflictError):
        service.current_schedule_for_finding(date(2026, 9, 1), date(2026, 9, 30), **parameters)


@pytest.mark.parametrize("transition", ["remove", "deactivate"])
def test_detected_pattern_override_follows_effective_history_without_double_counting(
    plan_db, transition
):
    path, ids = plan_db
    _add_pattern(
        path,
        ids,
        pattern_id="linked",
        name="Synthetic Internet",
        pattern_type="bill",
        amount="100",
        next_date=date(2026, 9, 10),
    )
    schedules = ScheduleService(path, prepare_schema=False)
    record = schedules.confirm_pattern("linked", schedule_id="confirmed")
    if transition == "remove":
        schedules.delete("confirmed", effective_date=date(2026, 10, 1))
    else:
        schedules.update(
            "confirmed",
            replace(schedules.draft_from_record(record), is_active=False),
            effective_date=date(2026, 10, 1),
        )
    plan = CashPlanService(path, prepare_schema=False).build(
        start_date=date(2026, 9, 1), as_of=date(2026, 9, 4), end_date=date(2026, 10, 31)
    )
    assert [(leg.occurrence_date, leg.provenance) for leg in plan.planning_legs] == (
        [(date(2026, 9, 10), "confirmed"), (date(2026, 10, 10), "detected")]
        if transition == "remove"
        else [(date(2026, 9, 10), "confirmed")]
    )


def test_pattern_confirmation_cannot_overlap_a_future_removed_confirmed_schedule(plan_db):
    from ledger.planning.schedules import ScheduleValidationError

    path, ids = plan_db
    _add_pattern(
        path,
        ids,
        pattern_id="linked",
        name="Synthetic Internet",
        pattern_type="bill",
        amount="100",
        next_date=date(2026, 9, 10),
    )
    schedules = ScheduleService(path, prepare_schema=False)
    schedules.confirm_pattern("linked", schedule_id="old")
    schedules.delete("old", effective_date=date(2026, 10, 1))
    with pytest.raises(ScheduleValidationError, match="overlap"):
        schedules.confirm_pattern("linked", schedule_id="new")
    schedules.confirm_pattern("linked", schedule_id="new", effective_date=date(2026, 10, 1))
    plan = CashPlanService(path, prepare_schema=False).build(
        start_date=date(2026, 9, 1), as_of=date(2026, 9, 4), end_date=date(2026, 10, 31)
    )
    assert [(leg.occurrence_date, leg.source_id) for leg in plan.planning_legs] == [
        (date(2026, 9, 10), "old"),
        (date(2026, 10, 10), "new"),
    ]


def test_pattern_confirmation_refuses_an_existing_unrelated_schedule_id(plan_db):
    from ledger.planning.schedules import ScheduleValidationError

    path, ids = plan_db
    record = _schedule(path, ids)
    _add_pattern(
        path,
        ids,
        pattern_id="new-pattern",
        name="Synthetic Different Bill",
        pattern_type="bill",
        amount="50",
        next_date=date(2026, 9, 10),
    )
    service = ScheduleService(path, prepare_schema=False)
    with pytest.raises(ScheduleValidationError):
        service.confirm_pattern("new-pattern", schedule_id="internet")
    assert service.list() == (record,)
