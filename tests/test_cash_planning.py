from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from ledger.core.account_identity import account_identity
from ledger.planning.cash_plan import CashPlanService, schedule_occurrence_dates
from ledger.planning.schedules import (
    ScheduleDraft,
    ScheduleNotFoundError,
    ScheduleService,
    ScheduleValidationError,
)
from ledger.storage.database import (
    AccountBalanceDB,
    AccountDB,
    PlannedScheduleDB,
    RecurringPatternDB,
    TransactionDB,
    TransferDB,
    session_scope,
)
from ledger.storage.migrations import ensure_schema
from ledger.ui.pages.budgets_goals import (
    _cash_plan_account_rows,
    _cash_plan_fact_rows,
    _cash_plan_leg_rows,
    _schedule_dataframe,
)
from ledger.utils.dates import naive_utc_now


def _account(
    institution: str,
    slug: str,
    *,
    account_type: str = "chequing",
    is_credit_card: bool = False,
) -> AccountDB:
    now = naive_utc_now()
    return AccountDB(
        account_id=account_identity(institution, slug),
        institution=institution,
        account_number=slug,
        account_type=account_type,
        account_subtype=account_type,
        first_transaction_date=date(2026, 1, 1),
        last_transaction_date=date(2026, 9, 1),
        is_active=True,
        transaction_count=1,
        avg_monthly_transactions=Decimal("1.00"),
        is_credit_card=is_credit_card,
        detected_credit_limit=Decimal("5000.00") if is_credit_card else None,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def planning_db(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    db_path = tmp_path / "ledger.db"
    ensure_schema(db_path)
    accounts = {
        "chequing": account_identity("test", "daily-chequing"),
        "savings": account_identity("test", "rainy-day"),
        "card": account_identity("card-bank", "rewards-card"),
        "unanchored": account_identity("test", "unanchored"),
    }
    with session_scope(db_path) as session:
        session.add_all(
            [
                _account("test", "daily-chequing"),
                _account("test", "rainy-day", account_type="savings"),
                _account(
                    "card-bank",
                    "rewards-card",
                    account_type="credit_card",
                    is_credit_card=True,
                ),
                _account("test", "unanchored", account_type="savings"),
            ]
        )
    return db_path, accounts


def _draft(accounts: dict[str, str], **changes: object) -> ScheduleDraft:
    values: dict[str, object] = {
        "name": "Rent",
        "event_type": "bill",
        "amount": Decimal("1200.00"),
        "currency": "CAD",
        "account_id": accounts["chequing"],
        "counterparty_account_id": None,
        "next_date": date(2026, 10, 1),
        "frequency": "monthly",
        "provenance": "hypothetical",
        "pattern_id": None,
        "is_active": True,
        "note": "Draft amount",
    }
    values.update(changes)
    return ScheduleDraft(**values)  # type: ignore[arg-type]


def test_schedule_crud_is_explicit_and_confirmation_is_reversible(
    planning_db: tuple[Path, dict[str, str]],
) -> None:
    db_path, accounts = planning_db
    service = ScheduleService(db_path, prepare_schema=False)

    created = service.create(_draft(accounts), schedule_id="rent")
    assert created.schedule_id == "rent"
    assert created.amount == Decimal("1200.00")
    assert created.provenance == "hypothetical"

    edited = service.update(
        "rent",
        _draft(
            accounts,
            name="Confirmed rent",
            amount=Decimal("1210.00"),
            provenance="hypothetical",
        ),
    )
    confirmed = service.confirm("rent")

    assert edited.created_at == created.created_at
    assert confirmed.provenance == "confirmed"
    assert confirmed.amount == Decimal("1210.00")
    assert [row.schedule_id for row in service.list()] == ["rent"]

    assert service.delete("rent") is True
    assert service.list() == ()
    with pytest.raises(ScheduleNotFoundError):
        service.confirm("rent")


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"currency": "USD"}, "unsupported_currency"),
        ({"amount": Decimal("0.00")}, "invalid_amount"),
        ({"amount": Decimal("1.001")}, "invalid_amount"),
        ({"event_type": "expense"}, "invalid_event_type"),
        ({"frequency": "daily"}, "invalid_frequency"),
        ({"provenance": "assumed"}, "invalid_provenance"),
        ({"is_active": 1}, "invalid_active_state"),
        ({"account_id": "missing"}, "unknown_account"),
        (
            {"event_type": "card_payment", "counterparty_account_id": None},
            "counterparty_required",
        ),
    ],
)
def test_schedule_validation_refuses_ambiguous_or_unrepresentable_rows(
    planning_db: tuple[Path, dict[str, str]],
    changes: dict[str, object],
    code: str,
) -> None:
    db_path, accounts = planning_db

    with pytest.raises(ScheduleValidationError) as caught:
        ScheduleService(db_path, prepare_schema=False).create(_draft(accounts, **changes))

    assert caught.value.code == code


def test_schedule_note_is_bounded_in_service_and_database_constraint(
    planning_db: tuple[Path, dict[str, str]],
) -> None:
    db_path, accounts = planning_db
    service = ScheduleService(db_path, prepare_schema=False)

    with pytest.raises(ScheduleValidationError) as caught:
        service.create(_draft(accounts, note="x" * 501))
    assert caught.value.code == "invalid_note"
    assert "x" * 501 not in str(caught.value)

    now = naive_utc_now()
    with pytest.raises(IntegrityError), session_scope(db_path) as session:
        session.add(
            PlannedScheduleDB(
                schedule_id="raw-long-note",
                name="Raw schedule",
                event_type="bill",
                amount=Decimal("10.00"),
                currency="CAD",
                account_id=accounts["chequing"],
                counterparty_account_id=None,
                next_date=date(2026, 10, 1),
                frequency="once",
                provenance="confirmed",
                pattern_id=None,
                is_active=True,
                note="x" * 501,
                created_at=now,
                updated_at=now,
            )
        )


def test_transfer_requires_two_different_existing_accounts(
    planning_db: tuple[Path, dict[str, str]],
) -> None:
    db_path, accounts = planning_db

    with pytest.raises(ScheduleValidationError) as caught:
        ScheduleService(db_path, prepare_schema=False).create(
            _draft(
                accounts,
                event_type="transfer",
                account_id=accounts["chequing"],
                counterparty_account_id=accounts["chequing"],
            )
        )

    assert caught.value.code == "same_account_transfer"


def test_card_payment_requires_a_credit_card_destination(
    planning_db: tuple[Path, dict[str, str]],
) -> None:
    db_path, accounts = planning_db
    service = ScheduleService(db_path, prepare_schema=False)

    with pytest.raises(ScheduleValidationError) as caught:
        service.create(
            _draft(
                accounts,
                event_type="card_payment",
                counterparty_account_id=accounts["savings"],
            )
        )

    assert caught.value.code == "card_destination_required"


def test_confirm_detected_pattern_copies_its_disclosed_amount_and_cadence(
    planning_db: tuple[Path, dict[str, str]],
) -> None:
    db_path, accounts = planning_db
    now = naive_utc_now()
    with session_scope(db_path) as session:
        session.add(
            RecurringPatternDB(
                pattern_id="annual-insurance",
                pattern_type="subscription",
                merchant="INSURER",
                category="Insurance",
                institution="test",
                account_id=accounts["chequing"],
                frequency="annual",
                expected_day_of_period=30,
                is_fixed_amount=True,
                expected_amount=Decimal("400.00"),
                amount_std_dev=Decimal("0.00"),
                confidence=0.9,
                detection_method="gap_analysis",
                occurrence_count=3,
                first_occurrence=date(2024, 6, 30),
                last_occurrence=date(2026, 6, 30),
                next_expected_date=date(2027, 6, 30),
                is_active=True,
                missed_count=0,
                detected_at=now,
                updated_at=now,
            )
        )

    service = ScheduleService(db_path, prepare_schema=False)
    candidates = service.detected_candidates()
    confirmed = service.confirm_pattern("annual-insurance", schedule_id="insurance")

    assert [candidate.pattern_id for candidate in candidates] == ["annual-insurance"]
    assert confirmed.name == "INSURER"
    assert confirmed.event_type == "bill"
    assert confirmed.frequency == "yearly"
    assert confirmed.amount == Decimal("400.00")
    assert confirmed.provenance == "confirmed"
    assert confirmed.pattern_id == "annual-insurance"
    assert service.detected_candidates() == ()


def test_schedule_to_dict_is_json_safe(planning_db: tuple[Path, dict[str, str]]) -> None:
    db_path, accounts = planning_db
    created = ScheduleService(db_path, prepare_schema=False).create(
        _draft(accounts), schedule_id="rent"
    )

    payload = created.to_dict()

    assert payload["amount"] == "1200.00"
    assert payload["next_date"] == "2026-10-01"
    json.dumps(payload)


def _add_absolute_balance(
    db_path: Path,
    account_id: str,
    amount: str,
    on: date = date(2026, 9, 1),
) -> None:
    now = naive_utc_now()
    balance = Decimal(amount)
    with session_scope(db_path) as session:
        session.add(
            AccountBalanceDB(
                account_id=account_id,
                balance_date=on,
                balance=balance,
                balance_basis="absolute",
                transaction_count=1,
                balance_7d_avg=balance,
                balance_30d_avg=balance,
                balance_7d_min=balance,
                balance_7d_max=balance,
                credit_utilization=None,
                computed_at=now,
            )
        )


def _add_pattern(
    db_path: Path,
    accounts: dict[str, str],
    *,
    pattern_id: str,
    name: str,
    pattern_type: str,
    amount: str,
    next_date: date,
    frequency: str = "monthly",
) -> None:
    now = naive_utc_now()
    with session_scope(db_path) as session:
        session.add(
            RecurringPatternDB(
                pattern_id=pattern_id,
                pattern_type=pattern_type,
                merchant=name,
                category="Employment Income" if pattern_type == "income" else "Bills",
                institution="test",
                account_id=accounts["chequing"],
                frequency=frequency,
                expected_day_of_period=next_date.day,
                is_fixed_amount=True,
                expected_amount=Decimal(amount),
                amount_std_dev=Decimal("0.00"),
                confidence=0.91,
                detection_method="gap_analysis",
                occurrence_count=4,
                first_occurrence=date(2025, 1, next_date.day),
                last_occurrence=date(2026, 8, min(next_date.day, 28)),
                next_expected_date=next_date,
                is_active=True,
                missed_count=0,
                detected_at=now,
                updated_at=now,
            )
        )


def _add_transaction(
    db_path: Path,
    *,
    txn_id: str,
    on: date,
    amount: str,
    cashflow_type: str,
    account: str = "daily-chequing",
    merchant: str = "Imported activity",
    recurring_pattern_id: str | None = None,
) -> None:
    value = Decimal(amount)
    now = naive_utc_now()
    with session_scope(db_path) as session:
        session.add(
            TransactionDB(
                txn_id=txn_id,
                posting_date=on,
                transaction_date=on,
                institution="test",
                account=account,
                account_type="chequing",
                flow=cashflow_type,
                cashflow_type=cashflow_type,
                include_in_cashflow=True,
                currency="CAD",
                amount_raw=value,
                amount_signed=value,
                description="Imported activity",
                merchant=merchant,
                category="Income" if cashflow_type == "income" else "Bills",
                merchant_confidence="manual",
                category_confidence="manual",
                exclude_from_spending=False,
                merchant_raw="IMPORTED ACTIVITY",
                category_raw="",
                source_file="synthetic.csv",
                source_path="synthetic/synthetic.csv",
                source_period=on.strftime("%Y-%m"),
                source_row=1,
                imported_at=now,
                parser_version="test",
                txn_fingerprint=f"fp-{txn_id}",
                recurring_pattern_id=recurring_pattern_id,
                day_of_week=on.weekday(),
                day_of_month=on.day,
                week_of_year=on.isocalendar().week,
                is_weekend=on.weekday() >= 5,
                is_month_start=on.day <= 3,
                is_month_end=on.day >= 28,
            )
        )


def _create_schedule(
    db_path: Path,
    accounts: dict[str, str],
    schedule_id: str,
    **changes: object,
) -> None:
    ScheduleService(db_path, prepare_schema=False).create(
        _draft(accounts, **changes), schedule_id=schedule_id
    )


def test_schedule_confirmation_rejects_a_relinked_detected_pattern(
    planning_db: tuple[Path, dict[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Keep the October schedule prospective for this relinking scenario.
    scenario_now = datetime(2026, 9, 30, 12)
    monkeypatch.setattr("ledger.planning.schedules.naive_utc_now", lambda: scenario_now)
    db_path, accounts = planning_db
    _add_pattern(
        db_path,
        accounts,
        pattern_id="detected-utility",
        name="Detected utility",
        pattern_type="bill",
        amount="100.00",
        next_date=date(2026, 10, 1),
    )
    service = ScheduleService(db_path, prepare_schema=False)

    service.confirm_pattern("detected-utility", schedule_id="original")
    service.update(
        "original",
        _draft(
            accounts,
            name="Detected utility",
            amount=Decimal("100.00"),
            provenance="hypothetical",
            pattern_id="detected-utility",
        ),
    )
    service.confirm_pattern("detected-utility", schedule_id="replacement")

    with pytest.raises(ScheduleValidationError) as caught:
        service.confirm("original")

    assert caught.value.code == "pattern_already_confirmed"
    assert [
        row.schedule_id
        for row in service.list()
        if row.pattern_id == "detected-utility" and row.provenance == "confirmed"
    ] == ["replacement"]


def test_schedule_create_and_update_reject_duplicate_confirmed_pattern_links(
    planning_db: tuple[Path, dict[str, str]],
) -> None:
    db_path, accounts = planning_db
    service = ScheduleService(db_path, prepare_schema=False)
    confirmed = _draft(
        accounts,
        provenance="confirmed",
        pattern_id="shared-pattern",
    )
    service.create(confirmed, schedule_id="first")

    with pytest.raises(ScheduleValidationError) as create_error:
        service.create(confirmed, schedule_id="second")
    assert create_error.value.code == "pattern_already_confirmed"

    service.create(
        _draft(accounts, pattern_id="shared-pattern"),
        schedule_id="hypothetical",
    )
    with pytest.raises(ScheduleValidationError) as update_error:
        service.update("hypothetical", confirmed)
    assert update_error.value.code == "pattern_already_confirmed"


def test_schedule_occurrences_use_calendar_cadences_without_month_end_drift() -> None:
    assert schedule_occurrence_dates(
        date(2026, 1, 31), "monthly", date(2026, 1, 1), date(2026, 4, 30)
    ) == (
        date(2026, 1, 31),
        date(2026, 2, 28),
        date(2026, 3, 31),
        date(2026, 4, 30),
    )
    assert schedule_occurrence_dates(
        date(2026, 1, 15), "semimonthly", date(2026, 1, 1), date(2026, 3, 1)
    ) == (
        date(2026, 1, 15),
        date(2026, 1, 31),
        date(2026, 2, 15),
        date(2026, 2, 28),
    )
    assert schedule_occurrence_dates(
        date(2026, 2, 5), "once", date(2026, 1, 1), date(2026, 12, 31)
    ) == (date(2026, 2, 5),)


def test_cash_plan_combines_sources_with_exact_internal_legs_and_link_override(
    planning_db: tuple[Path, dict[str, str]],
) -> None:
    db_path, accounts = planning_db
    _add_absolute_balance(db_path, accounts["chequing"], "1000.00")
    _add_absolute_balance(db_path, accounts["savings"], "500.00")
    _add_absolute_balance(db_path, accounts["card"], "-200.00")
    _add_pattern(
        db_path,
        accounts,
        pattern_id="detected-bill",
        name="Detected utility",
        pattern_type="bill",
        amount="100.00",
        next_date=date(2026, 9, 10),
    )
    _add_pattern(
        db_path,
        accounts,
        pattern_id="detected-payroll",
        name="Detected payroll",
        pattern_type="income",
        amount="2000.00",
        next_date=date(2026, 9, 15),
        frequency="biweekly",
    )
    _add_pattern(
        db_path,
        accounts,
        pattern_id="linked-pattern",
        name="Old detected amount",
        pattern_type="bill",
        amount="50.00",
        next_date=date(2026, 9, 12),
    )
    _create_schedule(
        db_path,
        accounts,
        "linked-confirmed",
        name="Confirmed amount",
        amount=Decimal("60.00"),
        next_date=date(2026, 9, 12),
        provenance="confirmed",
        pattern_id="linked-pattern",
    )
    _create_schedule(
        db_path,
        accounts,
        "hypothetical-bill",
        name="Possible fee",
        amount=Decimal("25.00"),
        next_date=date(2026, 9, 5),
        frequency="once",
        provenance="hypothetical",
    )
    _create_schedule(
        db_path,
        accounts,
        "savings-transfer",
        name="Savings sweep",
        event_type="transfer",
        amount=Decimal("300.00"),
        next_date=date(2026, 9, 8),
        frequency="once",
        provenance="confirmed",
        counterparty_account_id=accounts["savings"],
    )
    _create_schedule(
        db_path,
        accounts,
        "card-payment",
        name="Card payment",
        event_type="card_payment",
        amount=Decimal("200.00"),
        next_date=date(2026, 9, 9),
        frequency="once",
        provenance="confirmed",
        counterparty_account_id=accounts["card"],
    )

    plan = CashPlanService(db_path, prepare_schema=False).build(
        start_date=date(2026, 9, 2),
        end_date=date(2026, 9, 20),
        as_of=date(2026, 9, 2),
    )
    by_account = {account.account_id: account for account in plan.accounts}

    chequing = by_account[accounts["chequing"]]
    assert chequing.anchor.balance == Decimal("1000.00")
    assert chequing.anchor.balance_date == date(2026, 9, 1)
    assert chequing.anchor.basis == "statement_absolute"
    assert chequing.projected_movement == Decimal("1315.00")
    assert chequing.projected_balance == Decimal("2315.00")
    assert chequing.lowest_projected_balance == Decimal("315.00")
    assert chequing.lowest_projected_date == date(2026, 9, 12)

    transfer_legs = [leg for leg in plan.legs if leg.source_id == "savings-transfer"]
    payment_legs = [leg for leg in plan.legs if leg.source_id == "card-payment"]
    assert [leg.amount for leg in transfer_legs] == [Decimal("-300.00"), Decimal("300.00")]
    assert [leg.amount for leg in payment_legs] == [Decimal("-200.00"), Decimal("200.00")]
    assert [leg.account_id for leg in transfer_legs] == [
        accounts["chequing"],
        accounts["savings"],
    ]
    assert [leg.counterparty_account_id for leg in transfer_legs] == [
        accounts["savings"],
        accounts["chequing"],
    ]
    assert [leg.account_id for leg in payment_legs] == [
        accounts["chequing"],
        accounts["card"],
    ]
    assert [leg.counterparty_account_id for leg in payment_legs] == [
        accounts["card"],
        accounts["chequing"],
    ]
    assert all(not leg.is_spending for leg in transfer_legs + payment_legs)
    assert sum((leg.amount for leg in transfer_legs), Decimal("0.00")) == Decimal("0.00")
    assert sum((leg.amount for leg in payment_legs), Decimal("0.00")) == Decimal("0.00")
    assert "linked-pattern" not in {leg.source_id for leg in plan.legs}
    assert {leg.provenance for leg in plan.legs} == {"confirmed", "hypothetical", "detected"}
    income_legs = [leg for leg in plan.legs if leg.event_type == "income"]
    bill_legs = [leg for leg in plan.legs if leg.event_type == "bill"]
    assert income_legs and all(leg.amount > 0 and not leg.is_spending for leg in income_legs)
    assert bill_legs and all(leg.amount < 0 and leg.is_spending for leg in bill_legs)
    assert plan.coverage.overridden_detected_pattern_count == 1

    unanchored = by_account[accounts["unanchored"]]
    assert unanchored.anchor.status == "no_absolute_anchor"
    assert unanchored.projected_balance is None
    assert unanchored.lowest_projected_balance is None
    assert accounts["unanchored"] in plan.coverage.missing_anchor_account_ids

    before_payday = plan.before_next_payday
    assert before_payday is not None
    assert before_payday.payday_date == date(2026, 9, 15)
    assert before_payday.income_amount == Decimal("2000.00")
    assert before_payday.income_provenances == ("detected",)
    before_by_account = {row.account_id: row for row in before_payday.accounts}
    assert before_by_account[accounts["chequing"]].projected_movement == Decimal("-685.00")
    assert before_payday.includes_hypothetical is True


def test_imported_activity_suppression_requires_exact_account_date_amount_type_and_identity(
    planning_db: tuple[Path, dict[str, str]],
) -> None:
    db_path, accounts = planning_db
    _add_absolute_balance(db_path, accounts["chequing"], "1000.00")
    _create_schedule(
        db_path,
        accounts,
        "exact",
        name="Exact",
        amount=Decimal("100.00"),
        next_date=date(2026, 9, 5),
        frequency="once",
        provenance="confirmed",
    )
    _create_schedule(
        db_path,
        accounts,
        "wrong-date",
        name="Wrong date",
        amount=Decimal("100.00"),
        next_date=date(2026, 9, 11),
        frequency="once",
        provenance="confirmed",
    )
    _create_schedule(
        db_path,
        accounts,
        "wrong-amount",
        name="Wrong amount",
        amount=Decimal("99.00"),
        next_date=date(2026, 9, 5),
        frequency="once",
        provenance="confirmed",
    )
    _add_transaction(
        db_path,
        txn_id="imported-exact",
        on=date(2026, 9, 5),
        amount="-100.00",
        cashflow_type="expense",
        merchant="Exact",
    )

    plan = CashPlanService(db_path, prepare_schema=False).build(
        start_date=date(2026, 9, 2),
        end_date=date(2026, 9, 12),
        as_of=date(2026, 9, 10),
    )

    assert {leg.source_id for leg in plan.planning_legs} == {"wrong-date"}
    assert [leg.source_id for leg in plan.fact_legs] == ["imported-exact"]
    assert plan.coverage.excluded_pre_as_of_planning_occurrence_count == 1
    assert len(plan.suppressions) == 1
    suppression = plan.suppressions[0]
    assert suppression.source_id == "exact"
    assert suppression.occurrence_date == date(2026, 9, 5)
    assert suppression.amount == Decimal("100.00")
    assert suppression.matched_kind == "transaction"
    assert suppression.matched_evidence_ids == ("imported-exact",)


def test_ambiguous_imported_activity_does_not_suppress_an_occurrence(
    planning_db: tuple[Path, dict[str, str]],
) -> None:
    db_path, accounts = planning_db
    _add_absolute_balance(db_path, accounts["chequing"], "1000.00")
    _create_schedule(
        db_path,
        accounts,
        "ambiguous",
        name="Ambiguous bill",
        amount=Decimal("100.00"),
        next_date=date(2026, 9, 5),
        frequency="once",
        provenance="confirmed",
    )
    for txn_id in ("ambiguous-one", "ambiguous-two"):
        _add_transaction(
            db_path,
            txn_id=txn_id,
            on=date(2026, 9, 5),
            amount="-100.00",
            cashflow_type="expense",
            merchant="Ambiguous bill",
        )

    plan = CashPlanService(db_path, prepare_schema=False).build(
        start_date=date(2026, 9, 2),
        end_date=date(2026, 9, 10),
        as_of=date(2026, 9, 5),
    )

    assert [leg.source_id for leg in plan.planning_legs] == ["ambiguous"]
    assert {leg.source_id for leg in plan.fact_legs} == {"ambiguous-one", "ambiguous-two"}
    assert plan.suppressions == ()
    assert plan.coverage.ambiguous_suppression_count == 1
    assert plan.to_dict()["coverage"]["ambiguous_suppression_count"] == 1


def test_imported_match_replaces_schedule_and_rolls_anchor_forward_as_fact(
    planning_db: tuple[Path, dict[str, str]],
) -> None:
    db_path, accounts = planning_db
    _add_absolute_balance(db_path, accounts["chequing"], "1000.00")
    _create_schedule(
        db_path,
        accounts,
        "already-paid",
        name="Already paid",
        amount=Decimal("100.00"),
        next_date=date(2026, 9, 5),
        frequency="once",
        provenance="confirmed",
    )
    _create_schedule(
        db_path,
        accounts,
        "future-bill",
        name="Future bill",
        amount=Decimal("50.00"),
        next_date=date(2026, 9, 12),
        frequency="once",
        provenance="confirmed",
    )
    _add_transaction(
        db_path,
        txn_id="paid-fact",
        on=date(2026, 9, 5),
        amount="-100.00",
        cashflow_type="expense",
        merchant="Already paid",
    )

    plan = CashPlanService(db_path, prepare_schema=False).build(
        start_date=date(2026, 9, 10),
        end_date=date(2026, 9, 15),
        as_of=date(2026, 9, 10),
    )
    chequing = next(row for row in plan.accounts if row.account_id == accounts["chequing"])

    assert chequing.opening_projected_balance == Decimal("900.00")
    assert chequing.projected_movement == Decimal("-50.00")
    assert chequing.projected_balance == Decimal("850.00")
    assert chequing.movement_since_anchor == Decimal("-150.00")
    assert [row.source_id for row in plan.suppressions] == ["already-paid"]
    assert [(row.source_id, row.amount) for row in plan.fact_legs] == [
        ("paid-fact", Decimal("-100.00"))
    ]
    assert [(row.source_id, row.amount) for row in plan.planning_legs] == [
        ("future-bill", Decimal("-50.00"))
    ]
    assert plan.to_dict()["facts"]["actual_through"] == "2026-09-10"
    fact_rows = _cash_plan_fact_rows(plan, {accounts["chequing"]: "Chequing"})
    assert fact_rows == [
        {
            "Date": date(2026, 9, 5),
            "Account": "Chequing",
            "Event": "bill",
            "Imported rows": 1,
            "Net movement": Decimal("-100.00"),
            "Spending": "yes",
        }
    ]


def test_actual_roll_forward_deduplicates_transaction_and_transfer_representations(
    planning_db: tuple[Path, dict[str, str]],
) -> None:
    db_path, accounts = planning_db
    _add_absolute_balance(db_path, accounts["chequing"], "1000.00")
    _add_absolute_balance(db_path, accounts["savings"], "500.00")
    _add_transfer_pair(db_path, on=date(2026, 9, 5), amount="300.00")
    _add_transaction(
        db_path,
        txn_id="transfer-out",
        on=date(2026, 9, 5),
        amount="-300.00",
        cashflow_type="expense",
    )

    plan = CashPlanService(db_path, prepare_schema=False).build(
        start_date=date(2026, 9, 10),
        end_date=date(2026, 9, 15),
        as_of=date(2026, 9, 10),
    )
    by_account = {row.account_id: row for row in plan.accounts}

    assert by_account[accounts["chequing"]].opening_projected_balance == Decimal("700.00")
    assert by_account[accounts["savings"]].opening_projected_balance == Decimal("800.00")
    assert len(plan.fact_legs) == 2
    assert sum((row.amount for row in plan.fact_legs), Decimal("0.00")) == Decimal("0.00")
    assert all(row.event_type == "transfer" and not row.is_spending for row in plan.fact_legs)
    assert plan.facts.deduplicated_representation_count == 1
    rendered_facts = json.dumps(_cash_plan_fact_rows(plan, {}), default=str)
    assert "Unknown account" in rendered_facts
    assert all(account_id not in rendered_facts for account_id in accounts.values())


def test_as_of_day_fact_rolls_opening_and_suppresses_matching_upcoming_plan(
    planning_db: tuple[Path, dict[str, str]],
) -> None:
    db_path, accounts = planning_db
    _add_absolute_balance(db_path, accounts["chequing"], "1000.00")
    _create_schedule(
        db_path,
        accounts,
        "today-bill",
        name="Today bill",
        amount=Decimal("100.00"),
        next_date=date(2026, 9, 10),
        frequency="once",
        provenance="confirmed",
    )
    _add_transaction(
        db_path,
        txn_id="today-fact",
        on=date(2026, 9, 10),
        amount="-100.00",
        cashflow_type="expense",
        merchant="Today bill",
    )

    plan = CashPlanService(db_path, prepare_schema=False).build(
        start_date=date(2026, 9, 2),
        end_date=date(2026, 9, 15),
        as_of=date(2026, 9, 10),
    )
    account = next(row for row in plan.accounts if row.account_id == accounts["chequing"])

    assert account.actual_roll_forward_movement == Decimal("-100.00")
    assert account.opening_projected_balance == Decimal("900.00")
    assert account.planned_movement == Decimal("0.00")
    assert account.projected_balance == Decimal("900.00")
    assert [row.source_id for row in plan.fact_legs] == ["today-fact"]
    assert plan.planning_legs == ()
    assert [row.source_id for row in plan.suppressions] == ["today-bill"]


def test_unmatched_plan_before_as_of_is_excluded_but_as_of_day_plan_is_upcoming(
    planning_db: tuple[Path, dict[str, str]],
) -> None:
    db_path, accounts = planning_db
    _add_absolute_balance(db_path, accounts["chequing"], "1000.00")
    _create_schedule(
        db_path,
        accounts,
        "historical-unmatched",
        name="Historical unmatched",
        amount=Decimal("100.00"),
        next_date=date(2026, 9, 5),
        frequency="once",
        provenance="confirmed",
    )
    _create_schedule(
        db_path,
        accounts,
        "today-upcoming",
        name="Today upcoming",
        amount=Decimal("50.00"),
        next_date=date(2026, 9, 10),
        frequency="once",
        provenance="confirmed",
    )

    plan = CashPlanService(db_path, prepare_schema=False).build(
        start_date=date(2026, 9, 2),
        end_date=date(2026, 9, 15),
        as_of=date(2026, 9, 10),
    )
    account = next(row for row in plan.accounts if row.account_id == accounts["chequing"])

    assert account.actual_roll_forward_movement == Decimal("0.00")
    assert account.opening_projected_balance == Decimal("1000.00")
    assert account.planned_movement == Decimal("-50.00")
    assert account.projected_movement == Decimal("-50.00")
    assert account.projected_balance == Decimal("950.00")
    assert [row.source_id for row in plan.planning_legs] == ["today-upcoming"]
    assert plan.coverage.applied_planning_occurrence_count == 1
    assert plan.coverage.excluded_pre_as_of_planning_occurrence_count == 1


def _add_transfer_pair(
    db_path: Path,
    *,
    on: date,
    amount: str,
    paired: bool = True,
    destination_on: date | None = None,
) -> None:
    now = naive_utc_now()
    rows = (
        ("transfer-out", "test", "daily-chequing", "chequing", Decimal(f"-{amount}")),
        ("transfer-in", "test", "rainy-day", "savings", Decimal(amount)),
    )
    destination_on = destination_on or on
    with session_scope(db_path) as session:
        for index, (txn_id, institution, account, account_type, value) in enumerate(rows):
            session.add(
                TransferDB(
                    txn_id=txn_id,
                    posting_date=on if index == 0 else destination_on,
                    institution=institution,
                    account=account,
                    account_type=account_type,
                    flow="transfer",
                    amount_raw=value,
                    currency="CAD",
                    description="Synthetic transfer",
                    source_file="synthetic.csv",
                    source_path="synthetic/synthetic.csv",
                    imported_at=now,
                    paired_transfer_id=rows[1 - index][0] if paired else None,
                    transfer_confidence=Decimal("1.00") if paired else None,
                    counterparty_account=rows[1 - index][2] if paired else None,
                    counterparty_institution=rows[1 - index][1] if paired else None,
                )
            )


def _add_same_sign_card_payment_pair(db_path: Path, *, on: date, amount: str) -> None:
    """Persist the cross-issuer shape whose two native statement signs are negative."""
    now = naive_utc_now()
    rows = (
        ("card-payment-bank", "test", "daily-chequing", "chequing"),
        ("card-payment-card", "card-bank", "rewards-card", "credit_card"),
    )
    with session_scope(db_path) as session:
        for index, (txn_id, institution, account, account_type) in enumerate(rows):
            session.add(
                TransferDB(
                    txn_id=txn_id,
                    posting_date=on,
                    institution=institution,
                    account=account,
                    account_type=account_type,
                    flow="payment",
                    amount_raw=Decimal(f"-{amount}"),
                    currency="CAD",
                    description="Synthetic card payment",
                    source_file="synthetic.csv",
                    source_path="synthetic/synthetic.csv",
                    imported_at=now,
                    paired_transfer_id=rows[1 - index][0],
                    transfer_confidence=Decimal("1.00"),
                    counterparty_account=rows[1 - index][2],
                    counterparty_institution=rows[1 - index][1],
                )
            )


def test_same_sign_imported_card_payment_uses_role_based_account_effects(
    planning_db: tuple[Path, dict[str, str]],
) -> None:
    db_path, accounts = planning_db
    _add_absolute_balance(db_path, accounts["chequing"], "1000.00")
    _add_absolute_balance(db_path, accounts["card"], "-500.00")
    _add_same_sign_card_payment_pair(db_path, on=date(2026, 9, 5), amount="300.00")

    plan = CashPlanService(db_path, prepare_schema=False).build(
        start_date=date(2026, 9, 10),
        end_date=date(2026, 9, 15),
        as_of=date(2026, 9, 10),
    )
    by_account = {row.account_id: row for row in plan.accounts}
    legs = [row for row in plan.fact_legs if row.event_type == "card_payment"]

    assert by_account[accounts["chequing"]].opening_projected_balance == Decimal("700.00")
    assert by_account[accounts["card"]].opening_projected_balance == Decimal("-200.00")
    assert [row.amount for row in legs] == [Decimal("-300.00"), Decimal("300.00")]
    assert {row.occurrence_id for row in legs} == {
        "fact:transfer-pair:card-payment-bank:card-payment-card"
    }
    assert [row.direction for row in legs] == ["source", "destination"]
    assert [row.counterparty_account_id for row in legs] == [
        accounts["card"],
        accounts["chequing"],
    ]
    assert all(not row.is_spending for row in legs)


def test_two_account_occurrence_is_suppressed_only_by_an_exact_imported_pair(
    planning_db: tuple[Path, dict[str, str]],
) -> None:
    db_path, accounts = planning_db
    _add_absolute_balance(db_path, accounts["chequing"], "1000.00")
    _add_absolute_balance(db_path, accounts["savings"], "500.00")
    _create_schedule(
        db_path,
        accounts,
        "transfer",
        name="Transfer",
        event_type="transfer",
        amount=Decimal("300.00"),
        next_date=date(2026, 9, 5),
        frequency="once",
        provenance="confirmed",
        counterparty_account_id=accounts["savings"],
    )
    _add_transfer_pair(db_path, on=date(2026, 9, 5), amount="300.00")

    plan = CashPlanService(db_path, prepare_schema=False).build(
        start_date=date(2026, 9, 2),
        end_date=date(2026, 9, 10),
        as_of=date(2026, 9, 10),
    )

    assert plan.planning_legs == ()
    assert {leg.source_id for leg in plan.fact_legs} == {"transfer-out", "transfer-in"}
    assert len(plan.suppressions) == 1
    assert plan.suppressions[0].matched_kind == "paired_transfer"
    assert set(plan.suppressions[0].matched_evidence_ids) == {"transfer-out", "transfer-in"}


def test_linked_transfer_on_adjacent_posting_dates_suppresses_one_scheduled_occurrence(
    planning_db: tuple[Path, dict[str, str]],
) -> None:
    db_path, accounts = planning_db
    _add_absolute_balance(db_path, accounts["chequing"], "1000.00")
    _add_absolute_balance(db_path, accounts["savings"], "500.00")
    _create_schedule(
        db_path,
        accounts,
        "delayed-transfer",
        name="Delayed transfer",
        event_type="transfer",
        amount=Decimal("300.00"),
        next_date=date(2026, 9, 5),
        frequency="once",
        provenance="confirmed",
        counterparty_account_id=accounts["savings"],
    )
    _add_transfer_pair(
        db_path,
        on=date(2026, 9, 5),
        destination_on=date(2026, 9, 6),
        amount="300.00",
    )

    plan = CashPlanService(db_path, prepare_schema=False).build(
        start_date=date(2026, 9, 2),
        end_date=date(2026, 9, 10),
        as_of=date(2026, 9, 6),
    )

    assert plan.planning_legs == ()
    assert {leg.source_id for leg in plan.fact_legs} == {"transfer-out", "transfer-in"}
    assert [row.source_id for row in plan.suppressions] == ["delayed-transfer"]


def test_stored_reciprocal_pair_from_wider_match_window_remains_authoritative(
    planning_db: tuple[Path, dict[str, str]],
) -> None:
    db_path, accounts = planning_db
    _add_absolute_balance(db_path, accounts["chequing"], "1000.00")
    _add_absolute_balance(db_path, accounts["savings"], "500.00")
    _create_schedule(
        db_path,
        accounts,
        "wide-window-transfer",
        name="Wide-window transfer",
        event_type="transfer",
        amount=Decimal("300.00"),
        next_date=date(2026, 9, 5),
        frequency="once",
        provenance="confirmed",
        counterparty_account_id=accounts["savings"],
    )
    # Import matching may validly use a configured window wider than five days.
    # Once reciprocal ids are persisted, cash planning must not re-adjudicate it
    # with an unrelated default window.
    _add_transfer_pair(
        db_path,
        on=date(2026, 9, 5),
        destination_on=date(2026, 9, 11),
        amount="300.00",
    )

    plan = CashPlanService(db_path, prepare_schema=False).build(
        start_date=date(2026, 9, 2),
        end_date=date(2026, 9, 15),
        as_of=date(2026, 9, 5),
    )

    assert plan.planning_legs == ()
    assert [leg.source_id for leg in plan.fact_legs] == ["transfer-out"]
    assert [row.source_id for row in plan.suppressions] == ["wide-window-transfer"]


def test_posted_source_leg_suppresses_schedule_when_linked_destination_posts_next_day(
    planning_db: tuple[Path, dict[str, str]],
) -> None:
    db_path, accounts = planning_db
    _add_absolute_balance(db_path, accounts["chequing"], "1000.00")
    _add_absolute_balance(db_path, accounts["savings"], "500.00")
    _create_schedule(
        db_path,
        accounts,
        "pending-destination",
        name="Pending destination",
        event_type="transfer",
        amount=Decimal("300.00"),
        next_date=date(2026, 9, 5),
        frequency="once",
        provenance="confirmed",
        counterparty_account_id=accounts["savings"],
    )
    _add_transfer_pair(
        db_path,
        on=date(2026, 9, 5),
        destination_on=date(2026, 9, 6),
        amount="300.00",
    )

    plan = CashPlanService(db_path, prepare_schema=False).build(
        start_date=date(2026, 9, 2),
        end_date=date(2026, 9, 10),
        as_of=date(2026, 9, 5),
    )
    chequing = next(row for row in plan.accounts if row.account_id == accounts["chequing"])

    assert chequing.projected_balance == Decimal("700.00")
    assert [leg.source_id for leg in plan.fact_legs] == ["transfer-out"]
    assert plan.planning_legs == ()
    assert [row.source_id for row in plan.suppressions] == ["pending-destination"]


def test_cash_plan_payload_is_json_safe_and_does_not_encode_missing_anchor_as_zero(
    planning_db: tuple[Path, dict[str, str]],
) -> None:
    db_path, accounts = planning_db
    _add_absolute_balance(db_path, accounts["chequing"], "1000.00")

    payload = (
        CashPlanService(db_path, prepare_schema=False)
        .build(
            start_date=date(2026, 9, 2),
            end_date=date(2026, 9, 10),
            as_of=date(2026, 9, 2),
        )
        .to_dict()
    )

    unanchored = next(
        row for row in payload["accounts"] if row["account_id"] == accounts["unanchored"]
    )
    assert unanchored["anchor"]["balance"] is None
    assert unanchored["projected_balance"] is None
    assert payload["assumptions"]
    json.dumps(payload)


def test_cash_plan_ui_helpers_keep_hypothetical_and_missing_anchor_visible(
    planning_db: tuple[Path, dict[str, str]],
) -> None:
    db_path, accounts = planning_db
    _add_absolute_balance(db_path, accounts["chequing"], "1000.00")
    _create_schedule(
        db_path,
        accounts,
        "possible",
        name="Possible repair",
        amount=Decimal("75.00"),
        next_date=date(2026, 9, 5),
        frequency="once",
        provenance="hypothetical",
    )
    schedule_service = ScheduleService(db_path, prepare_schema=False)
    plan = CashPlanService(db_path, prepare_schema=False).build(
        start_date=date(2026, 9, 2),
        end_date=date(2026, 9, 10),
        as_of=date(2026, 9, 2),
    )
    labels = {account_id: name for name, account_id in accounts.items()}

    schedule_rows = _schedule_dataframe(schedule_service.list(), labels)
    leg_rows = _cash_plan_leg_rows(plan, labels)
    account_rows = _cash_plan_account_rows(plan, labels)

    assert schedule_rows.loc[0, "Provenance"] == "hypothetical"
    assert leg_rows[0]["Provenance"] == "hypothetical"
    unanchored = next(row for row in account_rows if row["Account"] == "unanchored")
    assert unanchored["Anchor"] == "No statement anchor"
    assert unanchored["Projected balance"] is None

    schedule_rows_without_labels = _schedule_dataframe(schedule_service.list(), {})
    leg_rows_without_labels = _cash_plan_leg_rows(plan, {})
    account_rows_without_labels = _cash_plan_account_rows(plan, {})
    assert set(schedule_rows_without_labels["Account"]) == {"Unknown account"}
    assert {row["Account"] for row in leg_rows_without_labels} == {"Unknown account"}
    assert {row["Account"] for row in account_rows_without_labels} == {"Unknown account"}
    rendered = json.dumps(
        {
            "schedules": schedule_rows_without_labels.to_dict("records"),
            "legs": leg_rows_without_labels,
            "accounts": account_rows_without_labels,
        },
        default=str,
    )
    assert all(account_id not in rendered for account_id in accounts.values())
