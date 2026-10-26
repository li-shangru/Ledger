"""Synthetic regressions for cash-plan identity, cadence, and payday boundaries."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from test_cash_planning import (
    _account,
    _add_absolute_balance,
    _add_pattern,
    _add_transaction,
    _create_schedule,
)

from ledger.core.account_identity import account_identity
from ledger.planning.cash_plan import CashPlanService, schedule_occurrence_dates
from ledger.storage.database import session_scope
from ledger.storage.migrations import ensure_schema

TODAY = date(2026, 9, 4)


@pytest.fixture
def cash_plan_db(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    db_path = tmp_path / "ledger.db"
    ensure_schema(db_path)
    accounts = {"chequing": account_identity("test", "daily-chequing")}
    with session_scope(db_path) as session:
        session.add(_account("test", "daily-chequing"))
    _add_absolute_balance(db_path, accounts["chequing"], "1000.00")
    return db_path, accounts


@pytest.mark.parametrize(
    ("event_type", "cashflow_type", "amount", "expected_balance"),
    [
        ("bill", "expense", "-100.00", "800.00"),
        ("income", "income", "100.00", "1200.00"),
    ],
)
@pytest.mark.parametrize("merchant", ["SYNTHETIC UNRELATED MERCHANT", "", "   "])
def test_same_date_amount_and_type_do_not_prove_a_bill_or_income_was_paid(
    cash_plan_db: tuple[Path, dict[str, str]],
    event_type: str,
    cashflow_type: str,
    amount: str,
    expected_balance: str,
    merchant: str,
) -> None:
    db_path, accounts = cash_plan_db
    _add_transaction(
        db_path,
        txn_id="unrelated-fact",
        on=TODAY,
        amount=amount,
        cashflow_type=cashflow_type,
        merchant=merchant,
    )
    _create_schedule(
        db_path,
        accounts,
        "still-due",
        name="SYNTHETIC SCHEDULED MERCHANT",
        event_type=event_type,
        amount=Decimal("100.00"),
        next_date=TODAY,
        frequency="once",
        provenance="confirmed",
    )

    plan = CashPlanService(db_path, prepare_schema=False).build(
        start_date=TODAY, as_of=TODAY, end_date=date(2026, 9, 30)
    )

    assert plan.suppressions == ()
    assert [leg.source_id for leg in plan.planning_legs] == ["still-due"]
    assert plan.accounts[0].projected_balance == Decimal(expected_balance)


@pytest.mark.parametrize("source_kind", ["schedule", "detected_pattern"])
@pytest.mark.parametrize("identity_evidence", ["merchant", "linked_pattern"])
def test_paid_occurrence_uses_merchant_or_linked_pattern_identity(
    cash_plan_db: tuple[Path, dict[str, str]],
    source_kind: str,
    identity_evidence: str,
) -> None:
    db_path, accounts = cash_plan_db
    _add_pattern(
        db_path,
        accounts,
        pattern_id="internet-pattern",
        name="Synthetic Internet",
        pattern_type="bill",
        amount="100.00",
        next_date=TODAY,
    )
    if source_kind == "schedule":
        _create_schedule(
            db_path,
            accounts,
            "internet-schedule",
            name="Synthetic Internet",
            amount=Decimal("100.00"),
            next_date=TODAY,
            frequency="once",
            provenance="confirmed",
            pattern_id="internet-pattern",
        )
    _add_transaction(
        db_path,
        txn_id="paid-fact",
        on=TODAY,
        amount="-100.00",
        cashflow_type="expense",
        merchant=(
            "  SYNTHETIC   INTERNET "
            if identity_evidence == "merchant"
            else "Different imported display name"
        ),
        recurring_pattern_id=(
            "internet-pattern" if identity_evidence == "linked_pattern" else None
        ),
    )

    plan = CashPlanService(db_path, prepare_schema=False).build(
        start_date=TODAY, as_of=TODAY, end_date=date(2026, 9, 30)
    )

    assert plan.planning_legs == ()
    assert len(plan.suppressions) == 1
    assert plan.suppressions[0].source_kind == source_kind
    assert plan.suppressions[0].matched_evidence_ids == ("paid-fact",)
    assert plan.accounts[0].projected_balance == Decimal("900.00")


def test_one_paid_transaction_cannot_suppress_two_identical_schedules(
    cash_plan_db: tuple[Path, dict[str, str]],
) -> None:
    db_path, accounts = cash_plan_db
    for schedule_id in ("first", "second"):
        _create_schedule(
            db_path,
            accounts,
            schedule_id,
            name="Synthetic Internet",
            amount=Decimal("100.00"),
            next_date=TODAY,
            frequency="once",
            provenance="confirmed",
        )
    _add_transaction(
        db_path,
        txn_id="paid-fact",
        on=TODAY,
        amount="-100.00",
        cashflow_type="expense",
        merchant="Synthetic Internet",
    )

    plan = CashPlanService(db_path, prepare_schema=False).build(
        start_date=TODAY, as_of=TODAY, end_date=date(2026, 9, 30)
    )

    assert plan.suppressions == ()
    assert {leg.source_id for leg in plan.planning_legs} == {"first", "second"}
    assert plan.coverage.ambiguous_suppression_count == 2
    assert plan.accounts[0].projected_balance == Decimal("700.00")


@pytest.mark.parametrize("source_kind", ["schedule", "detected_pattern"])
def test_unpaid_income_due_today_is_the_next_payday(
    cash_plan_db: tuple[Path, dict[str, str]], source_kind: str
) -> None:
    db_path, accounts = cash_plan_db
    if source_kind == "schedule":
        _create_schedule(
            db_path,
            accounts,
            "salary",
            name="Synthetic Salary",
            event_type="income",
            amount=Decimal("2000.00"),
            next_date=TODAY,
            frequency="monthly",
            provenance="confirmed",
        )
    else:
        _add_pattern(
            db_path,
            accounts,
            pattern_id="salary-pattern",
            name="Synthetic Salary",
            pattern_type="income",
            amount="2000.00",
            next_date=TODAY,
        )
    _add_transaction(
        db_path,
        txn_id="today-purchase",
        on=TODAY,
        amount="-100.00",
        cashflow_type="expense",
    )

    plan = CashPlanService(db_path, prepare_schema=False).build(
        start_date=TODAY, as_of=TODAY, end_date=date(2026, 10, 5)
    )

    payday = plan.before_next_payday
    assert payday is not None
    assert payday.payday_date == TODAY
    assert payday.income_amount == Decimal("2000.00")
    assert payday.accounts[0].projected_movement == Decimal("0.00")
    assert payday.accounts[0].projected_balance_before_payday == Decimal("900.00")


def test_paid_income_today_does_not_replace_the_next_unpaid_payday(
    cash_plan_db: tuple[Path, dict[str, str]],
) -> None:
    db_path, accounts = cash_plan_db
    _create_schedule(
        db_path,
        accounts,
        "salary",
        name="Synthetic Salary",
        event_type="income",
        amount=Decimal("2000.00"),
        next_date=TODAY,
        frequency="monthly",
        provenance="confirmed",
    )
    _add_transaction(
        db_path,
        txn_id="today-salary",
        on=TODAY,
        amount="2000.00",
        cashflow_type="income",
        merchant="Synthetic Salary",
    )

    plan = CashPlanService(db_path, prepare_schema=False).build(
        start_date=TODAY, as_of=TODAY, end_date=date(2026, 10, 5)
    )

    payday = plan.before_next_payday
    assert payday is not None
    assert payday.payday_date == date(2026, 10, 4)
    assert payday.accounts[0].projected_movement == Decimal("0.00")
    assert payday.accounts[0].projected_balance_before_payday == Decimal("3000.00")


@pytest.mark.parametrize(
    ("next_date", "start_date", "end_date", "expected"),
    [
        (
            date(2026, 9, 1),
            date(2026, 9, 1),
            date(2026, 10, 31),
            (date(2026, 9, 1), date(2026, 9, 16), date(2026, 10, 1), date(2026, 10, 16)),
        ),
        (
            date(2026, 1, 14),
            date(2026, 1, 1),
            date(2026, 3, 31),
            (
                date(2026, 1, 14),
                date(2026, 1, 29),
                date(2026, 2, 14),
                date(2026, 2, 28),
                date(2026, 3, 14),
                date(2026, 3, 29),
            ),
        ),
        (
            date(2026, 1, 29),
            date(2026, 2, 1),
            date(2026, 3, 31),
            (date(2026, 2, 14), date(2026, 2, 28), date(2026, 3, 14), date(2026, 3, 29)),
        ),
        (
            date(2026, 1, 30),
            date(2026, 1, 1),
            date(2026, 3, 31),
            (
                date(2026, 1, 30),
                date(2026, 2, 15),
                date(2026, 2, 28),
                date(2026, 3, 15),
                date(2026, 3, 30),
            ),
        ),
        (
            date(2026, 2, 28),
            date(2026, 2, 1),
            date(2026, 4, 30),
            (
                date(2026, 2, 28),
                date(2026, 3, 15),
                date(2026, 3, 31),
                date(2026, 4, 15),
                date(2026, 4, 30),
            ),
        ),
        (
            date(2028, 1, 14),
            date(2028, 2, 1),
            date(2028, 3, 31),
            (date(2028, 2, 14), date(2028, 2, 29), date(2028, 3, 14), date(2028, 3, 29)),
        ),
        (
            date(2028, 2, 29),
            date(2028, 2, 1),
            date(2028, 3, 31),
            (date(2028, 2, 29), date(2028, 3, 15), date(2028, 3, 31)),
        ),
        (
            date(2026, 12, 16),
            date(2026, 12, 1),
            date(2027, 1, 31),
            (date(2026, 12, 16), date(2027, 1, 1), date(2027, 1, 16)),
        ),
    ],
)
def test_semimonthly_keeps_its_original_pair_after_clamping_and_year_changes(
    next_date: date, start_date: date, end_date: date, expected: tuple[date, ...]
) -> None:
    assert schedule_occurrence_dates(next_date, "semimonthly", start_date, end_date) == expected


@pytest.mark.parametrize(
    ("frequency", "expected"),
    [
        ("weekly", (date(9999, 12, 15), date(9999, 12, 22), date(9999, 12, 29))),
        ("biweekly", (date(9999, 12, 15), date(9999, 12, 29))),
        ("semimonthly", (date(9999, 12, 15), date.max)),
        ("monthly", (date(9999, 12, 15),)),
        ("quarterly", (date(9999, 12, 15),)),
        ("yearly", (date(9999, 12, 15),)),
    ],
)
def test_schedule_expansion_returns_available_dates_at_the_supported_year_limit(
    frequency: str, expected: tuple[date, ...]
) -> None:
    assert (
        schedule_occurrence_dates(date(9999, 12, 15), frequency, date(9999, 12, 1), date.max)
        == expected
    )
