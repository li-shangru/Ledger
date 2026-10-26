"""Deterministic financial brief service tests."""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from account_fixtures import seed_account

from ledger.core.account_identity import account_identity
from ledger.planning.reconciliation import PlanReconciliationService
from ledger.planning.schedules import ScheduleDraft, ScheduleService
from ledger.query.agent import InsightFinding
from ledger.reporting.brief import FinancialBriefService
from ledger.storage.database import (
    AccountBalanceDB,
    AccountDB,
    AccountLifecycleDB,
    ConfigMetadataDB,
    InsightFindingDB,
    StatementCoverageDB,
    StatementCoverageIntervalDB,
    TransactionDB,
    init_database,
    session_scope,
)
from ledger.storage.operations import ACCOUNT_REF_SALT_KEY

_PERIOD_START = date(2026, 8, 1)
_PERIOD_END = date(2026, 8, 31)
_IMPORTED_AT = datetime(2026, 9, 1, 12, 0)
_OPAQUE_ID = hashlib.sha1(b"unusual-current-transaction").hexdigest()


def test_brief_ranks_material_reconciliation_across_pages(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    _seed_database(db)
    account = account_identity("synthetic-bank", "synthetic-chequing-1234")
    start, end = date(2026, 9, 1), date(2026, 9, 30)
    with session_scope(db) as session:
        session.add(
            StatementCoverageIntervalDB(
                interval_id="complete-september",
                institution="synthetic-bank",
                account_slug="synthetic-chequing-1234",
                start_date=start,
                end_date=end,
                evidence_basis="printed_statement_period",
                imported_at=datetime(2026, 10, 1),
            )
        )
    schedules = ScheduleService(db)
    with pytest.MonkeyPatch.context() as clock:
        clock.setattr("ledger.planning.schedules.naive_utc_now", lambda: datetime(2026, 9, 1))
        for index in range(100):
            name = f"Synthetic small bill {index:03d}"
            schedules.create(
                ScheduleDraft(
                    name=name,
                    event_type="bill",
                    amount=Decimal("100.00"),
                    currency="CAD",
                    account_id=account,
                    counterparty_account_id=None,
                    next_date=date(2026, 9, 5),
                    frequency="once",
                    provenance="confirmed",
                ),
                schedule_id=f"small-bill-{index:03d}",
            )
            _insert_transaction(db, name, date(2026, 9, 5), "-100.01", name, "Bills")
        schedules.create(
            ScheduleDraft(
                name="Synthetic missing payroll",
                event_type="income",
                amount=Decimal("3000.00"),
                currency="CAD",
                account_id=account,
                counterparty_account_id=None,
                next_date=date(2026, 9, 20),
                frequency="once",
                provenance="confirmed",
            ),
            schedule_id="missing-payroll",
        )
    report = PlanReconciliationService(db).build(start, end, as_of=end, offset=100)
    [payroll] = report["findings"]
    assert payroll["status"] == "missing"
    assert payroll["coverage"]["complete"]
    service = FinancialBriefService(db)
    brief = service.build(start, end, as_of=end)
    selected = [
        row for row in brief["observations"] if row["dashboard_path"] == "/plan-reconciliation"
    ]
    assert 3 <= len(brief["observations"]) <= 5
    assert len(selected) == 4
    assert selected[0]["subject"] == payroll["occurrence_id"]
    assert "fully covered posting window" in selected[0]["statement"]
    assert selected[0]["amount"] is None
    assert selected[0]["direction"] == "not_applicable"
    repeated = service.build(start, end, as_of=end)
    assert repeated["observations"] == brief["observations"]


@pytest.mark.parametrize("actual_payroll", ["3000.00", "3000.01"])
def test_payroll_timing_priority_survives_a_coexisting_amount_difference(
    tmp_path: Path, actual_payroll: str
) -> None:
    db = tmp_path / "ledger.db"
    _seed_database(db)
    account = account_identity("synthetic-bank", "synthetic-chequing-1234")
    schedules = ScheduleService(db)
    with pytest.MonkeyPatch.context() as clock:
        clock.setattr("ledger.planning.schedules.naive_utc_now", lambda: datetime(2026, 9, 1))
        for index in range(4):
            name = f"Synthetic small bill {index}"
            schedules.create(
                ScheduleDraft(
                    name=name,
                    event_type="bill",
                    amount=Decimal("100.00"),
                    currency="CAD",
                    account_id=account,
                    counterparty_account_id=None,
                    next_date=date(2026, 9, 5 + index),
                    frequency="once",
                    provenance="confirmed",
                )
            )
            _insert_transaction(db, name, date(2026, 9, 5 + index), "-100.02", name, "Bills")
        schedules.create(
            ScheduleDraft(
                name="Synthetic delayed payroll",
                event_type="income",
                amount=Decimal("3000.00"),
                currency="CAD",
                account_id=account,
                counterparty_account_id=None,
                next_date=date(2026, 9, 20),
                frequency="once",
                provenance="confirmed",
            ),
            schedule_id="delayed-payroll",
        )
    evidence_id = _insert_transaction(
        db,
        "delayed-payroll",
        date(2026, 9, 23),
        actual_payroll,
        "Synthetic delayed payroll",
        "Income",
    )
    start, end = date(2026, 9, 1), date(2026, 9, 30)
    report = PlanReconciliationService(db).build(start, end, as_of=end)
    payroll = next(row for row in report["findings"] if row["schedule_id"] == "delayed-payroll")
    assert payroll["date_changed"] is True
    assert payroll["date_difference"] == 3
    assert payroll["amount_changed"] is (actual_payroll == "3000.01")
    difference = Decimal(actual_payroll) - Decimal("3000.00")
    assert Decimal(payroll["amount_difference"]) == difference
    brief = FinancialBriefService(db).build(start, end, as_of=end)
    selected = [
        row for row in brief["observations"] if row["dashboard_path"] == "/plan-reconciliation"
    ]
    assert selected[0]["subject"] == payroll["occurrence_id"]
    assert selected[0]["evidence_ids"] == [evidence_id]
    assert Decimal(str(selected[0]["amount"])) == difference
    assert "2026-09-20" in selected[0]["statement"]
    assert "2026-09-23" in selected[0]["statement"]
    assert f"CAD {actual_payroll}" in selected[0]["statement"]


def _temporal_fields(posted: date) -> dict[str, object]:
    return {
        "day_of_week": posted.weekday(),
        "day_of_month": posted.day,
        "week_of_year": posted.isocalendar()[1],
        "is_weekend": posted.weekday() >= 5,
        "is_month_start": posted.day <= 3,
        "is_month_end": posted.day >= 28,
    }


def _insert_transaction(
    db_path: Path,
    label: str,
    posted: date,
    amount: str,
    merchant: str,
    category: str,
    *,
    imported_at: datetime = _IMPORTED_AT,
) -> str:
    txn_id = hashlib.sha1(label.encode()).hexdigest()
    signed = Decimal(amount)
    flow = "income" if signed > 0 else "expense"
    with session_scope(db_path) as session:
        session.add(
            TransactionDB(
                txn_id=txn_id,
                posting_date=posted,
                transaction_date=posted,
                institution="synthetic-bank",
                account="synthetic-chequing-1234",
                account_type="chequing",
                flow=flow,
                cashflow_type=flow,
                include_in_cashflow=True,
                currency="CAD",
                amount_raw=abs(signed),
                amount_signed=signed,
                description=f"SYNTHETIC {label}",
                merchant=merchant,
                category=category,
                source_file="synthetic.csv",
                source_path="synthetic/synthetic.csv",
                source_period=posted.strftime("%Y-%m"),
                source_row=posted.day,
                imported_at=imported_at,
                parser_version="synthetic",
                txn_fingerprint=hashlib.md5(label.encode()).hexdigest(),
                **_temporal_fields(posted),
            )
        )
    return txn_id


def _seed_database(db_path: Path) -> None:
    init_database(db_path)

    _insert_transaction(
        db_path,
        "previous-income",
        date(2026, 7, 1),
        "2000.00",
        "PAYROLL",
        "Income",
        imported_at=datetime(2026, 8, 1, 12, 0),
    )
    _insert_transaction(
        db_path,
        "previous-groceries",
        date(2026, 7, 10),
        "-400.00",
        "MARKET",
        "Groceries",
        imported_at=datetime(2026, 8, 1, 12, 0),
    )
    _insert_transaction(
        db_path,
        "previous-dining",
        date(2026, 7, 20),
        "-100.00",
        "CAFE",
        "Dining",
        imported_at=datetime(2026, 8, 1, 12, 0),
    )
    _insert_transaction(db_path, "current-income", date(2026, 8, 5), "2100.00", "PAYROLL", "Income")
    _insert_transaction(
        db_path, "current-groceries", date(2026, 8, 10), "-700.00", "MARKET", "Groceries"
    )
    _insert_transaction(db_path, "current-dining", date(2026, 8, 20), "-150.00", "CAFE", "Dining")
    unusual_id = _insert_transaction(
        db_path,
        "unusual-current-transaction",
        date(2026, 8, 25),
        "-750.00",
        "ELECTRONICS",
        "Shopping",
    )
    assert unusual_id == _OPAQUE_ID

    account_id = seed_account(
        db_path,
        "synthetic-bank",
        "synthetic-chequing-1234",
        account_type="checking",
        first_transaction_date=date(2026, 7, 1),
        last_transaction_date=date(2026, 8, 25),
    )
    with session_scope(db_path) as session:
        session.add_all(
            [
                ConfigMetadataDB(key=ACCOUNT_REF_SALT_KEY, value="5f" * 32),
                StatementCoverageDB(
                    account_slug="synthetic-chequing-1234",
                    period="2026-07",
                    institution="synthetic-bank",
                    statement_type="synthetic-deposit",
                    row_count=3,
                    statement_count=1,
                    skipped_reason="",
                    imported_at=datetime(2026, 8, 1, 12, 0),
                ),
                StatementCoverageDB(
                    account_slug="synthetic-chequing-1234",
                    period="2026-08",
                    institution="synthetic-bank",
                    statement_type="synthetic-deposit",
                    row_count=4,
                    statement_count=1,
                    skipped_reason="",
                    imported_at=_IMPORTED_AT,
                ),
                AccountBalanceDB(
                    account_id=account_id,
                    balance_date=date(2026, 8, 30),
                    balance=Decimal("1234.56"),
                    balance_basis="absolute",
                    transaction_count=7,
                    balance_7d_avg=Decimal("1234.56"),
                    balance_30d_avg=Decimal("1234.56"),
                    balance_7d_min=Decimal("1234.56"),
                    balance_7d_max=Decimal("1234.56"),
                    credit_utilization=None,
                    computed_at=_IMPORTED_AT,
                ),
                InsightFindingDB(
                    fingerprint="a" * 32,
                    period_start=_PERIOD_START,
                    period_end=_PERIOD_END,
                    finding_type="anomalies",
                    severity="high",
                    title="stored private title",
                    explanation="stored private explanation",
                    evidence={
                        "amount": 750.0,
                        "category": "Shopping",
                        "date": "2026-08-25",
                        "merchant": "ELECTRONICS",
                        "txn_id": unusual_id,
                    },
                    suggested_action="stored private action",
                    status="open",
                    created_at=_IMPORTED_AT,
                ),
            ]
        )


def test_build_names_an_equal_length_comparison_and_uses_exact_metrics(tmp_path: Path) -> None:
    """A shifted or shortened baseline would make period movement incomparable."""
    db_path = tmp_path / "ledger.db"
    _seed_database(db_path)

    brief = FinancialBriefService(db_path).build(_PERIOD_START, _PERIOD_END)

    assert brief["period"] == {
        "name": "selected_period",
        "start": "2026-08-01",
        "end": "2026-08-31",
        "days": 31,
    }
    assert brief["comparison"] == {
        "name": "previous_equal_length_period",
        "start": "2026-07-01",
        "end": "2026-07-31",
        "days": 31,
        "comparable": True,
        "coverage_reason": "selected_and_comparison_periods_fully_covered_by_statements",
    }
    assert brief["metrics"] == {
        "current": {
            "income": 2100.0,
            "gross_spend": 1600.0,
            "offsets": 0.0,
            "true_cost": 1600.0,
            "net": 500.0,
            "transaction_count": 4,
        },
        "comparison": {
            "income": 2000.0,
            "gross_spend": 500.0,
            "offsets": 0.0,
            "true_cost": 500.0,
            "net": 1500.0,
            "transaction_count": 3,
        },
        "change": {
            "income": 100.0,
            "gross_spend": 1100.0,
            "offsets": 0.0,
            "true_cost": 1100.0,
            "net": -1000.0,
            "transaction_count": 1,
        },
    }


def test_build_ranks_three_to_five_labelled_safe_observations(tmp_path: Path) -> None:
    """Unranked prose or raw evidence could leak detail and imply unsupported causality."""
    db_path = tmp_path / "ledger.db"
    _seed_database(db_path)

    first = FinancialBriefService(db_path).build(_PERIOD_START, _PERIOD_END)
    second = FinancialBriefService(db_path).build(_PERIOD_START, _PERIOD_END)

    assert first == second
    observations = first["observations"]
    assert 3 <= len(observations) <= 5
    assert [item["rank"] for item in observations] == list(range(1, len(observations) + 1))
    assert {item["label"] for item in observations} == {
        "arithmetic",
        "possibility",
        "coverage",
    }
    assert all(re.fullmatch(r"/[a-z-]+", item["dashboard_path"]) for item in observations)
    assert all(
        re.fullmatch(r"[0-9a-f]{40}", evidence_id)
        for item in observations
        for evidence_id in item["evidence_ids"]
    )
    possibility = next(item for item in observations if item["label"] == "possibility")
    assert possibility["evidence_ids"] == [_OPAQUE_ID]

    prose = " ".join(str(item["statement"]).casefold() for item in observations)
    for causal_phrase in ("because", "caused", "drove", "driven by", "led to", "resulted in"):
        assert causal_phrase not in prose
    serialized = json.dumps(first, sort_keys=True)
    assert "synthetic/synthetic.csv" not in serialized
    assert "synthetic-chequing-1234" not in serialized
    assert "stored private" not in serialized


def test_missing_comparison_statement_is_not_treated_as_zero_activity(tmp_path: Path) -> None:
    """An older row cannot make an uncovered prior period comparable."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_transaction(
        db_path,
        "older-history",
        date(2026, 6, 15),
        "-1.00",
        "OLD MERCHANT",
        "Other",
    )
    _insert_transaction(
        db_path,
        "current-with-missing-prior-statement",
        date(2026, 8, 15),
        "-200.00",
        "MARKET",
        "Groceries",
    )
    with session_scope(db_path) as session:
        session.add(
            StatementCoverageDB(
                account_slug="synthetic-chequing-1234",
                period="2026-08",
                institution="synthetic-bank",
                statement_type="synthetic-deposit",
                row_count=1,
                statement_count=1,
                skipped_reason="",
                imported_at=_IMPORTED_AT,
            )
        )

    brief = FinancialBriefService(db_path).build(_PERIOD_START, _PERIOD_END)

    assert brief["comparison"]["comparable"] is False
    assert (
        brief["comparison"]["coverage_reason"]
        == "statement_coverage_cannot_establish_comparability"
    )
    assert not [
        item for item in brief["observations"] if item["title"] == "Largest category movement"
    ]
    coverage = next(
        item for item in brief["observations"] if item["title"] == "Comparison coverage"
    )
    assert "statement coverage" in coverage["statement"].casefold()


def test_coverage_only_account_requires_coverage_in_both_compared_periods(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ledger.db"
    _seed_database(db_path)
    with session_scope(db_path) as session:
        session.add(
            StatementCoverageDB(
                account_slug="coverage-only-card",
                period="2026-08",
                institution="second-bank",
                statement_type="synthetic-card",
                row_count=0,
                statement_count=1,
                skipped_reason="no_transaction_rows",
                imported_at=_IMPORTED_AT,
            )
        )

    brief = FinancialBriefService(db_path).build(_PERIOD_START, _PERIOD_END)

    assert brief["comparison"]["comparable"] is False
    assert (
        brief["comparison"]["coverage_reason"]
        == "statement_coverage_cannot_establish_comparability"
    )


def test_quiet_inactive_account_without_recorded_closure_requires_statement_coverage(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ledger.db"
    _seed_database(db_path)
    account_id = seed_account(
        db_path,
        "second-bank",
        "quiet-with-no-window-rows",
        first_transaction_date=date(2026, 6, 1),
        last_transaction_date=date(2026, 6, 30),
    )
    with session_scope(db_path) as session:
        session.get(AccountDB, account_id).is_active = False

    brief = FinancialBriefService(db_path).build(_PERIOD_START, _PERIOD_END)

    assert brief["comparison"]["comparable"] is False
    assert (
        brief["comparison"]["coverage_reason"]
        == "statement_coverage_cannot_establish_comparability"
    )


def test_account_closed_before_comparison_does_not_require_later_coverage(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ledger.db"
    _seed_database(db_path)
    account_id = seed_account(
        db_path,
        "second-bank",
        "closed-before-comparison",
        first_transaction_date=date(2026, 1, 1),
        last_transaction_date=date(2026, 6, 15),
    )
    with session_scope(db_path) as session:
        session.add(
            AccountLifecycleDB(
                account_id=account_id,
                status="closed",
                status_confirmed_on=date(2026, 8, 1),
                closed_on=date(2026, 6, 30),
                note="",
                created_at=_IMPORTED_AT,
                updated_at=_IMPORTED_AT,
            )
        )

    brief = FinancialBriefService(db_path).build(_PERIOD_START, _PERIOD_END)

    assert brief["comparison"]["comparable"] is True


def test_build_exposes_structured_drivers_and_detail_routes(tmp_path: Path) -> None:
    """Agent adapters need bounded driver fields; parsing dashboard prose would drift."""
    db_path = tmp_path / "ledger.db"
    _seed_database(db_path)

    observations = FinancialBriefService(db_path).build(_PERIOD_START, _PERIOD_END)["observations"]

    category = next(item for item in observations if item["title"] == "Largest category movement")
    assert category["dashboard_path"] == "/categories"
    assert category["subject_kind"] == "category"
    assert category["subject"] == "Shopping"
    assert category["amount"] == 750.0
    assert category["direction"] == "higher"

    possibility = next(item for item in observations if item["label"] == "possibility")
    assert possibility["dashboard_path"] == "/insights"
    assert possibility["subject_kind"] == "category"
    assert possibility["subject"] == "Shopping"
    assert possibility["amount"] == 750.0
    assert possibility["direction"] == "not_applicable"


def test_ranked_possibility_prefers_material_evidence_over_suppressed_size_only_advice() -> None:
    suppressed = InsightFinding(
        finding_type="cost_cutting",
        severity="high",
        status="open",
        period_start=_PERIOD_START,
        period_end=_PERIOD_END,
        scalars={"amount": "1000.00", "merchant": "INTENTIONAL MERCHANT"},
        action_priority="suppressed",
        preference_scope="merchant",
        preference_disposition="intentional",
        preference_effect="size_only_reduction_suppressed",
    )
    material = InsightFinding(
        finding_type="anomalies",
        severity="medium",
        status="open",
        period_start=_PERIOD_START,
        period_end=_PERIOD_END,
        scalars={
            "amount": "250.00",
            "category": "Shopping",
            "date": "2026-08-25",
            "evidence_id": _OPAQUE_ID,
            "merchant": "REVIEW MERCHANT",
        },
        action_priority="normal",
        preference_scope=None,
        preference_disposition=None,
        preference_effect=None,
    )

    selected = FinancialBriefService._ranked_possibility(
        [suppressed, material],
        current_period=(_PERIOD_START, _PERIOD_END),
        insight_status="current_for_requested_period",
    )

    assert selected is not None
    assert selected["subject"] == "Shopping"
    assert selected["evidence_ids"] == [_OPAQUE_ID]


def test_build_discloses_coverage_freshness_completeness_and_since_basis(tmp_path: Path) -> None:
    """Zeros and fresh-looking findings must not hide bounded or stale source coverage."""
    db_path = tmp_path / "ledger.db"
    _seed_database(db_path)

    brief = FinancialBriefService(db_path).build(
        _PERIOD_START,
        date(2026, 9, 2),
        since=datetime(2026, 8, 31, 12, 0),
    )

    coverage = brief["coverage"]
    assert coverage["completeness"] == {
        "status": "not_asserted",
        "statement": "Results cover imported records only; absence is not proof of real-world absence.",
    }
    assert coverage["freshness"] == {
        "earliest_transaction_date": "2026-07-01",
        "latest_transaction_date": "2026-08-25",
        "requested_end": "2026-09-02",
        "latest_transaction_lag_days": 8,
        "status": "requested_period_extends_beyond_imported_transactions",
    }
    assert coverage["statements"]["observed_periods"] == {
        "start": "2026-07",
        "end": "2026-08",
    }
    assert coverage["balances"] == {
        "as_of": "2026-09-02",
        "reported": 1,
        "unavailable": 0,
        "maximum_anchor_lag_days": 3,
    }
    assert coverage["insights"]["status"] == "stale_for_requested_period"
    assert coverage["changes"]["queried_since"] == "2026-08-31T12:00:00"
    assert coverage["changes"]["basis"] == "delta"
    assert coverage["changes"]["total_added"] == 4

    whole_history = FinancialBriefService(db_path).build(
        _PERIOD_START,
        _PERIOD_END,
        since=datetime(2026, 7, 1, 0, 0),
    )
    assert whole_history["coverage"]["changes"]["statement_count"] == 2


def test_build_default_uses_the_latest_trailing_window_up_to_ninety_days(tmp_path: Path) -> None:
    """Finalize callers should not duplicate or drift from the default-period rule."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_transaction(db_path, "oldest", date(2026, 1, 1), "-10.00", "MARKET", "Groceries")
    _insert_transaction(db_path, "latest", date(2026, 5, 31), "-20.00", "MARKET", "Groceries")

    brief = FinancialBriefService(db_path).build_default()

    assert brief["period"] == {
        "name": "selected_period",
        "start": "2026-03-03",
        "end": "2026-05-31",
        "days": 90,
    }


def test_statement_gap_assessment_is_pinned_to_the_requested_period_end(tmp_path: Path) -> None:
    """A historical brief must not change as the wall clock advances."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_transaction(
        db_path, "april-activity", date(2026, 4, 15), "-25.00", "MARKET", "Groceries"
    )
    seed_account(
        db_path,
        "synthetic-bank",
        "synthetic-chequing-1234",
        account_type="checking",
        first_transaction_date=date(2026, 1, 1),
        last_transaction_date=date(2026, 4, 15),
    )
    with session_scope(db_path) as session:
        session.add(ConfigMetadataDB(key=ACCOUNT_REF_SALT_KEY, value="5f" * 32))
        session.add_all(
            StatementCoverageDB(
                account_slug="synthetic-chequing-1234",
                period=f"2026-{month:02d}",
                institution="synthetic-bank",
                statement_type="synthetic-deposit",
                row_count=1,
                statement_count=1,
                skipped_reason="",
                imported_at=_IMPORTED_AT,
            )
            for month in range(1, 5)
        )

    brief = FinancialBriefService(db_path).build(date(2026, 4, 1), date(2026, 4, 30))

    statements = brief["coverage"]["statements"]
    assert statements["gaps_evaluated_on"] == "2026-04-30"
    assert statements["gap_counts"]["naming_a_missing_statement"] == 0


def test_empty_database_returns_a_json_safe_brief_with_fewer_observations(tmp_path: Path) -> None:
    """Empty imported data is unknown coverage, not three invented financial findings."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)

    brief = FinancialBriefService(db_path).build(date(2026, 8, 1), date(2026, 8, 31))

    assert brief["status"] == "empty"
    assert len(brief["observations"]) < 3
    assert {item["label"] for item in brief["observations"]} <= {"coverage"}
    assert brief["coverage"]["freshness"]["status"] == "no_imported_transactions"
    assert brief["coverage"]["statements"]["matched_account_months"] == 0
    assert brief["coverage"]["balances"]["reported"] == 0
    assert brief["coverage"]["insights"]["status"] == "absent"
    json.dumps(brief)


def test_session_factory_path_does_not_prepare_or_create_the_nominal_database(
    tmp_path: Path,
) -> None:
    """A pinned MCP reader must never invoke schema preparation on its public path."""
    actual_db = tmp_path / "actual.db"
    _seed_database(actual_db)
    nominal_db = tmp_path / "must-not-be-created.db"

    @contextmanager
    def supplied_scope(_ignored_path: Path):
        with session_scope(actual_db) as session:
            yield session

    service = FinancialBriefService(
        nominal_db,
        session_scope_factory=supplied_scope,
    )
    brief = service.build(_PERIOD_START, _PERIOD_END)

    assert brief["status"] == "ready"
    assert not nominal_db.exists()
