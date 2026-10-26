from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ledger.core.account_identity import account_identity
from ledger.insights.preferences import (
    PREFERENCE_NOTE_MAX_LENGTH,
    SpendingPreferenceService,
)
from ledger.insights.service import GeneratedFinding, InsightEngine
from ledger.storage.database import (
    AccountDB,
    InsightFindingDB,
    RecurringPatternDB,
    TransactionDB,
    TransactionTreatmentDB,
    session_scope,
)
from ledger.storage.migrations import ensure_schema


def _finding(
    finding_type: str,
    *,
    merchant: str = "LOCAL GROCER",
    category: str = "Groceries",
    evidence: dict | None = None,
    action: str = "Reduce this expense.",
) -> GeneratedFinding:
    return GeneratedFinding(
        finding_type=finding_type,
        severity="high",
        title=f"Review {merchant}",
        explanation="Synthetic finding explanation.",
        evidence={"merchant": merchant, "category": category, **(evidence or {})},
        suggested_action=action,
    )


def _insert_expense(
    db_path: Path,
    *,
    merchant: str,
    category: str,
    posted: date,
    txn_id: str = "synthetic-preference-expense",
    amount: Decimal = Decimal("900.00"),
) -> None:
    with session_scope(db_path) as session:
        session.add(
            TransactionDB(
                txn_id=txn_id,
                posting_date=posted,
                transaction_date=posted,
                institution="test-bank",
                account="test-account",
                account_type="visa",
                flow="expense",
                cashflow_type="expense",
                include_in_cashflow=True,
                currency="CAD",
                amount_raw=amount,
                amount_signed=-amount,
                description=merchant,
                merchant=merchant,
                category=category,
                source_file="synthetic.csv",
                source_path="synthetic/synthetic.csv",
                source_period=posted.strftime("%Y-%m"),
                source_row=1,
                imported_at=datetime.now(UTC).replace(tzinfo=None),
                parser_version="test",
                txn_fingerprint=f"{txn_id}-fingerprint",
                day_of_week=posted.weekday(),
                day_of_month=posted.day,
                week_of_year=posted.isocalendar()[1],
                is_weekend=posted.weekday() >= 5,
                is_month_start=posted.day <= 3,
                is_month_end=posted.day >= 28,
            )
        )


def test_preference_upsert_is_normalized_editable_and_reversible(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    service = SpendingPreferenceService(db_path)

    created = service.upsert(
        " merchant ",
        "  Local\t Grocer  ",
        " necessary ",
        note=" Household food. ",
    )
    updated = service.upsert(
        "MERCHANT",
        "local grocer",
        "worthwhile",
        note="Good ingredients",
    )
    category = service.upsert("category", "  Dining  ", "intentional")

    assert created["name"] == "LOCAL GROCER"
    assert created["note"] == "Household food."
    assert updated == {
        **created,
        "disposition": "worthwhile",
        "note": "Good ingredients",
        "updated_at": updated["updated_at"],
    }
    assert updated["id"] == created["id"]
    assert category["name"] == "Dining"
    assert [(row["scope"], row["name"]) for row in service.list_preferences()] == [
        ("category", "Dining"),
        ("merchant", "LOCAL GROCER"),
    ]

    assert service.delete("merchant", " local   grocer ") is True
    assert service.delete("merchant", "LOCAL GROCER") is False
    assert [(row["scope"], row["name"]) for row in service.list_preferences()] == [
        ("category", "Dining")
    ]

    restored = service.upsert("merchant", "local grocer", "necessary")
    assert restored["name"] == "LOCAL GROCER"
    assert {row["name"] for row in SpendingPreferenceService(db_path).list_preferences()} == {
        "Dining",
        "LOCAL GROCER",
    }


@pytest.mark.parametrize(
    ("scope", "name", "disposition", "note"),
    [
        ("account", "LOCAL GROCER", "necessary", ""),
        ("merchant", "", "necessary", ""),
        ("merchant", "x" * 161, "necessary", ""),
        ("merchant", "ß" * 81, "necessary", ""),
        ("merchant", "LOCAL GROCER", "optional", ""),
        ("merchant", "LOCAL GROCER", "necessary", "x" * (PREFERENCE_NOTE_MAX_LENGTH + 1)),
    ],
)
def test_preference_validation_rejects_invalid_rows_without_replacing_existing_state(
    tmp_path: Path,
    scope: str,
    name: str,
    disposition: str,
    note: str,
) -> None:
    db_path = tmp_path / "ledger.db"
    service = SpendingPreferenceService(db_path)
    service.upsert("merchant", "LOCAL GROCER", "intentional", note="Keep")

    with pytest.raises(ValueError):
        service.upsert(scope, name, disposition, note=note)

    rows = service.list_preferences()
    assert len(rows) == 1
    assert rows[0]["disposition"] == "intentional"
    assert rows[0]["note"] == "Keep"


def test_preference_reads_use_the_injected_session_boundary(tmp_path: Path) -> None:
    actual_db = tmp_path / "actual.db"
    ensure_schema(actual_db)
    SpendingPreferenceService(actual_db, prepare_schema=False).upsert(
        "merchant", "LOCAL GROCER", "necessary"
    )

    @contextmanager
    def actual_session(_requested_path: Path):
        with session_scope(actual_db) as session:
            yield session

    service = SpendingPreferenceService(
        tmp_path / "must-not-be-opened.db",
        session_scope_factory=actual_session,
        prepare_schema=False,
    )

    assert [row["name"] for row in service.list_preferences()] == ["LOCAL GROCER"]
    [ranked] = InsightEngine(
        tmp_path / "must-not-be-opened.db",
        session_scope_factory=actual_session,
        prepare_schema=False,
    ).rank_findings([_finding("cost_cutting")])
    assert ranked.action_priority == "suppressed"
    assert not (tmp_path / "must-not-be-opened.db").exists()


def test_normalized_matching_is_exact_and_merchant_specific_preference_wins(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ledger.db"
    preferences = SpendingPreferenceService(db_path)
    preferences.upsert("category", "Groceries", "intentional")
    preferences.upsert("merchant", "Local Grocer", "necessary")
    engine = InsightEngine(db_path)

    ranked = engine.rank_findings(
        [
            _finding("cost_cutting", merchant=" local   grocer "),
            _finding("cost_cutting", merchant="LOCAL GROCER EXPRESS", category="Transport"),
            _finding("cost_cutting", merchant="OTHER", category="fine groceries"),
            _finding("cost_cutting", merchant="x" * 161, category="Transport"),
        ]
    )
    by_merchant = {row.evidence["merchant"]: row for row in ranked}

    exact = by_merchant[" local   grocer "]
    assert exact.preference_context == {
        "scope": "merchant",
        "name": "LOCAL GROCER",
        "disposition": "necessary",
        "note": "",
        "effect": "size_only_reduction_suppressed",
    }
    assert exact.action_priority == "suppressed"
    assert exact.effective_suggested_action is None
    assert by_merchant["LOCAL GROCER EXPRESS"].preference_context is None
    assert by_merchant["OTHER"].preference_context is None
    assert by_merchant["x" * 161].preference_context is None


def test_category_preference_matches_merchant_only_cost_finding_from_period_facts(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ledger.db"
    preferences = SpendingPreferenceService(db_path)
    _insert_expense(
        db_path,
        merchant="Local   Grocer",
        category="Groceries",
        posted=date(2026, 8, 12),
    )
    _insert_expense(
        db_path,
        merchant="LOCAL GROCER",
        category=" groceries ",
        posted=date(2026, 8, 13),
        txn_id="same-normalized-category",
    )
    preferences.upsert("category", "groceries", "necessary")
    cost_finding = _finding("cost_cutting", evidence={})
    cost_finding.evidence.pop("category")

    [ranked] = InsightEngine(db_path).rank_findings(
        [cost_finding],
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 31),
    )

    assert ranked.evidence == {"merchant": "LOCAL GROCER"}
    assert ranked.action_priority == "suppressed"
    assert ranked.preference_context is not None
    assert ranked.preference_context["scope"] == "category"
    assert ranked.preference_context["name"] == "groceries"


def test_category_preference_does_not_guess_for_a_multi_category_merchant(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ledger.db"
    preferences = SpendingPreferenceService(db_path)
    _insert_expense(
        db_path,
        merchant="MARKETPLACE",
        category="Groceries",
        posted=date(2026, 8, 12),
        txn_id="groceries",
        amount=Decimal("900.00"),
    )
    _insert_expense(
        db_path,
        merchant="MARKETPLACE",
        category="Dining",
        posted=date(2026, 8, 13),
        txn_id="dining",
        amount=Decimal("100.00"),
    )
    preferences.upsert("category", "Groceries", "necessary")
    cost_finding = _finding("cost_cutting", merchant="MARKETPLACE")
    cost_finding.evidence.pop("category")

    [ranked] = InsightEngine(db_path).rank_findings(
        [cost_finding],
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 31),
    )

    assert ranked.preference_context is None
    assert ranked.action_priority == "normal"
    assert ranked.effective_suggested_action == ranked.suggested_action


def test_credit_card_fingerprints_use_canonical_identity_not_display_name() -> None:
    start = date(2026, 8, 1)
    end = date(2026, 8, 31)

    def card(account_id: str, account_type: str) -> GeneratedFinding:
        return GeneratedFinding(
            finding_type="credit_cards",
            severity="medium",
            title=f"Card spend: harbor {account_type}",
            explanation="Synthetic card finding.",
            evidence={
                "account_id": account_id,
                "institution": "harbor",
                "account_type": account_type,
                "net": -Decimal("50.00"),
            },
            suggested_action="Review card rewards.",
        )

    first_id = account_identity("harbor", "card-1001")
    second_id = account_identity("harbor", "card-2002")
    distinct = InsightEngine._dedupe(
        [card(first_id, "credit_card"), card(second_id, "credit_card")],
        start,
        end,
    )
    variants = InsightEngine._dedupe(
        [card(first_id, "Visa Infinite"), card(first_id, "credit_card")],
        start,
        end,
    )

    assert [row.evidence["account_id"] for row in distinct] == [first_id, second_id]
    assert len(variants) == 1


def test_duplicate_pairs_are_deterministic_and_keep_distinct_review_state(
    tmp_path: Path,
) -> None:
    start = date(2026, 8, 1)
    end = date(2026, 8, 31)
    txn_ids = [character * 40 for character in ("1", "2", "3")]
    observed: list[list[tuple[str, str]]] = []
    first_db: Path | None = None

    for name, insertion_order in (
        ("forward", txn_ids),
        ("reverse", list(reversed(txn_ids))),
    ):
        db_path = tmp_path / f"{name}.db"
        ensure_schema(db_path)
        for txn_id in insertion_order:
            _insert_expense(
                db_path,
                merchant="TIED CHARGE",
                category="Shopping",
                posted=date(2026, 8, 20),
                txn_id=txn_id,
                amount=Decimal("25.00"),
            )

        engine = InsightEngine(db_path, prepare_schema=False)
        findings = engine._possible_duplicates(start, end)
        pairs = [
            (
                str(finding.evidence["txn_id"]),
                str(finding.evidence["related_txn_id"]),
            )
            for finding in findings
        ]
        observed.append(pairs)
        assert len({finding.fingerprint(start, end) for finding in findings}) == 3
        if first_db is None:
            first_db = db_path

    assert (
        observed[0]
        == observed[1]
        == [
            (txn_ids[0], txn_ids[1]),
            (txn_ids[0], txn_ids[2]),
            (txn_ids[1], txn_ids[2]),
        ]
    )

    assert first_db is not None
    engine = InsightEngine(first_db, prepare_schema=False)
    engine.refresh_period(start, end)
    ignored_pair = (txn_ids[0], txn_ids[1])
    with session_scope(first_db) as session:
        rows = (
            session.query(InsightFindingDB)
            .filter(InsightFindingDB.finding_type == "possible_duplicates")
            .all()
        )
        assert len(rows) == 3
        for row in rows:
            evidence = dict(row.evidence or {})
            if (evidence.get("txn_id"), evidence.get("related_txn_id")) == ignored_pair:
                row.status = "ignored"

    engine.refresh_period(start, end)
    with session_scope(first_db) as session:
        states = {
            (
                str(dict(row.evidence or {})["txn_id"]),
                str(dict(row.evidence or {})["related_txn_id"]),
            ): str(row.status)
            for row in session.query(InsightFindingDB)
            .filter(InsightFindingDB.finding_type == "possible_duplicates")
            .all()
        }
    assert states == {
        (txn_ids[0], txn_ids[1]): "ignored",
        (txn_ids[0], txn_ids[2]): "open",
        (txn_ids[1], txn_ids[2]): "open",
    }


def test_material_change_producers_surface_supported_possibilities(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    ensure_schema(db_path)
    account_id = account_identity("test-bank", "test-account")
    now = datetime(2026, 9, 1, tzinfo=UTC).replace(tzinfo=None)
    with session_scope(db_path) as session:
        session.add(
            AccountDB(
                account_id=account_id,
                institution="test-bank",
                account_number="test-account",
                account_type="chequing",
                account_subtype="chequing",
                first_transaction_date=date(2026, 7, 1),
                last_transaction_date=date(2026, 8, 31),
                is_active=True,
                transaction_count=4,
                avg_monthly_transactions=Decimal("2.00"),
                is_credit_card=False,
                detected_credit_limit=None,
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            RecurringPatternDB(
                pattern_id="a" * 40,
                pattern_type="subscription",
                merchant="STREAMING",
                category="Entertainment",
                institution="test-bank",
                account_id=account_id,
                frequency="monthly",
                expected_day_of_period=15,
                is_fixed_amount=False,
                expected_amount=Decimal("12.00"),
                amount_std_dev=Decimal("0.00"),
                confidence=1.0,
                detection_method="synthetic",
                occurrence_count=4,
                first_occurrence=date(2026, 5, 15),
                last_occurrence=date(2026, 8, 15),
                next_expected_date=date(2026, 9, 15),
                is_active=True,
                missed_count=0,
                detected_at=now,
                updated_at=now,
            )
        )

    _insert_expense(
        db_path,
        merchant="STREAMING",
        category="Entertainment",
        posted=date(2026, 7, 15),
        txn_id="1" * 40,
        amount=Decimal("10.00"),
    )
    _insert_expense(
        db_path,
        merchant="STREAMING",
        category="Entertainment",
        posted=date(2026, 8, 15),
        txn_id="2" * 40,
        amount=Decimal("12.00"),
    )
    _insert_expense(
        db_path,
        merchant="DUPLICATE SHOP",
        category="Shopping",
        posted=date(2026, 8, 20),
        txn_id="3" * 40,
        amount=Decimal("25.00"),
    )
    _insert_expense(
        db_path,
        merchant="DUPLICATE SHOP",
        category="Shopping",
        posted=date(2026, 8, 21),
        txn_id="4" * 40,
        amount=Decimal("25.00"),
    )
    with session_scope(db_path) as session:
        session.add(
            TransactionDB(
                txn_id="5" * 40,
                posting_date=date(2026, 8, 25),
                transaction_date=date(2026, 8, 25),
                institution="test-bank",
                account="test-account",
                account_type="chequing",
                flow="income",
                cashflow_type="income",
                include_in_cashflow=True,
                currency="CAD",
                amount_raw=Decimal("80.00"),
                amount_signed=Decimal("80.00"),
                description="INSURER REIMBURSEMENT",
                merchant="INSURER",
                category="Insurance",
                source_file="synthetic.csv",
                source_path="synthetic/synthetic.csv",
                source_period="2026-08",
                source_row=5,
                imported_at=now,
                parser_version="test",
                txn_fingerprint="5" * 32,
                day_of_week=1,
                day_of_month=25,
                week_of_year=35,
                is_weekend=False,
                is_month_start=False,
                is_month_end=False,
            )
        )
        session.add(
            TransactionTreatmentDB(
                txn_id="5" * 40,
                treatment_type="reimbursement",
                status="needs_review",
                confidence=Decimal("0.90"),
                netting_policy="exclude_from_adjusted_income_only",
                target_category="Health & Wellness",
                evidence={"reason": "synthetic"},
                detected_at=now,
                source="test",
            )
        )

    engine = InsightEngine(db_path, prepare_schema=False)
    duplicates = engine._possible_duplicates(date(2026, 8, 1), date(2026, 8, 31))
    increases = engine._recurring_price_increases(date(2026, 8, 1), date(2026, 8, 31))
    reimbursements = engine._unresolved_reimbursements(date(2026, 8, 1), date(2026, 8, 31))

    assert len(duplicates) == 1
    assert duplicates[0].finding_type == "possible_duplicates"
    assert {
        duplicates[0].evidence["txn_id"],
        duplicates[0].evidence["related_txn_id"],
    } == {"3" * 40, "4" * 40}
    assert duplicates[0].evidence["possibility"] == "detected"
    assert len(increases) == 1
    assert increases[0].finding_type == "recurring_price_increase"
    assert increases[0].evidence["amount"] == "2.00"
    assert increases[0].evidence["possibility"] == "detected"
    assert len(reimbursements) == 1
    assert reimbursements[0].finding_type == "unresolved_reimbursements"
    assert reimbursements[0].evidence["txn_id"] == "5" * 40
    assert reimbursements[0].evidence["possibility"] == "detected"


def test_generation_caps_detected_facts_before_preference_ranking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "ledger.db"
    SpendingPreferenceService(db_path).upsert("merchant", "APPROVED", "intentional")
    engine = InsightEngine(db_path)
    candidates = [
        _finding("cost_cutting", merchant="APPROVED", category="Transport"),
        *[
            _finding("cost_cutting", merchant=f"MERCHANT {index}", category="Transport")
            for index in range(60)
        ],
    ]
    monkeypatch.setattr(
        "ledger.insights.service.ReportSnapshotService.snapshot_for_period",
        lambda *_args, **_kwargs: {"payload": {}},
    )
    monkeypatch.setattr(engine, "_cost_cutting", lambda _payload: candidates)
    monkeypatch.setattr(engine, "_category_drift", lambda _payload: [])
    monkeypatch.setattr(engine, "_credit_cards", lambda _payload: [])
    monkeypatch.setattr(engine, "_anomalies", lambda _payload: [])
    monkeypatch.setattr(engine, "_recurring", lambda _payload, _start, _end: [])
    monkeypatch.setattr(engine, "_budget_findings", lambda _start, _end: [])
    monkeypatch.setattr(engine, "_transfer_quality", lambda _start, _end: [])

    generated = engine.generate(date(2026, 8, 1), date(2026, 8, 31))
    merchants = {row.evidence["merchant"] for row in generated}

    assert len(generated) == 60
    assert "APPROVED" in merchants
    assert "MERCHANT 59" not in merchants


@pytest.mark.parametrize(
    ("finding_type", "evidence"),
    [
        ("recurring", {"price_increase": "5.00", "possibility": "detected"}),
        ("recurring", {"material_change": True, "possibility": "detected"}),
        ("quality", {"issue": "possible_duplicate", "possibility": "detected"}),
        ("quality", {"issue": "unresolved_reimbursement", "possibility": "detected"}),
        ("anomalies", {"txn_id": "synthetic-anomaly", "possibility": "detected"}),
        ("category_drift", {"change": "125.00", "possibility": "detected"}),
    ],
)
def test_preference_never_downgrades_or_reclassifies_material_new_evidence(
    tmp_path: Path,
    finding_type: str,
    evidence: dict,
) -> None:
    db_path = tmp_path / "ledger.db"
    SpendingPreferenceService(db_path).upsert(
        "merchant", "LOCAL GROCER", "worthwhile", note="Chosen expense"
    )
    original = _finding(finding_type, evidence=evidence, action="Investigate this detection.")

    [ranked] = InsightEngine(db_path).rank_findings([original])

    assert ranked.finding_type == original.finding_type
    assert ranked.title == original.title
    assert ranked.explanation == original.explanation
    assert ranked.evidence == original.evidence
    assert ranked.evidence["possibility"] == "detected"
    assert ranked.action_priority == "normal"
    assert ranked.effective_suggested_action == "Investigate this detection."
    assert ranked.preference_context == {
        "scope": "merchant",
        "name": "LOCAL GROCER",
        "disposition": "worthwhile",
        "note": "Chosen expense",
        "effect": "material_evidence_retained",
    }


@pytest.mark.parametrize(
    "comparison_evidence",
    [
        {"price_change": "0.00"},
        {"price_change": "-2.00"},
        {"price_increase": False},
        {"price_increase": "false"},
        {"material_change": False},
        {"previous_amount": "45.00"},
    ],
)
def test_non_increases_remain_ordinary_preference_rank(
    tmp_path: Path,
    comparison_evidence: dict,
) -> None:
    db_path = tmp_path / "ledger.db"
    SpendingPreferenceService(db_path).upsert("merchant", "LOCAL GROCER", "intentional")
    ordinary = _finding(
        "recurring",
        evidence=comparison_evidence,
        action="Review this recurring charge.",
    )

    [ranked] = InsightEngine(db_path).rank_findings([ordinary])

    assert ranked.action_priority == "low"
    assert ranked.effective_suggested_action == "Review this recurring charge."
    assert ranked.preference_context is not None
    assert ranked.preference_context["effect"] == "ordinary_recommendation_deprioritized"


def test_preference_reduces_ordinary_action_rank_without_changing_finding_severity(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ledger.db"
    SpendingPreferenceService(db_path).upsert("merchant", "LOCAL GROCER", "intentional")
    ordinary = _finding("recurring", action="Cancel or downgrade this recurring charge.")

    [ranked] = InsightEngine(db_path).rank_findings([ordinary])

    assert ranked.severity == "high"
    assert ranked.action_priority == "low"
    assert ranked.effective_suggested_action == "Cancel or downgrade this recurring charge."
    assert ranked.suggested_action == "Cancel or downgrade this recurring charge."


def test_stored_finding_status_and_raw_evidence_stay_independent_from_preferences(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ledger.db"
    ensure_schema(db_path)
    raw_evidence = {
        "merchant": "LOCAL GROCER",
        "category": "Groceries",
        "amount": 900.0,
        "txn_id": "synthetic-only",
    }
    with session_scope(db_path) as session:
        session.add_all(
            [
                InsightFindingDB(
                    fingerprint="size-only",
                    period_start=date(2026, 8, 1),
                    period_end=date(2026, 8, 31),
                    finding_type="cost_cutting",
                    severity="high",
                    title="Review spend at LOCAL GROCER",
                    explanation="A top spend driver.",
                    evidence=raw_evidence,
                    suggested_action="Reduce this expense.",
                    status="watch",
                    created_at=datetime(2026, 9, 1),
                ),
                InsightFindingDB(
                    fingerprint="material-anomaly",
                    period_start=date(2026, 8, 1),
                    period_end=date(2026, 8, 31),
                    finding_type="anomalies",
                    severity="high",
                    title="Unusual transaction at LOCAL GROCER",
                    explanation="A detected possibility.",
                    evidence={**raw_evidence, "possibility": "detected"},
                    suggested_action="Investigate this detection.",
                    status="ignored",
                    created_at=datetime(2026, 9, 1),
                ),
            ]
        )
    preferences = SpendingPreferenceService(db_path, prepare_schema=False)
    preferences.upsert("merchant", " local grocer ", "necessary", note="Household food")

    rows = InsightEngine(db_path).list_findings(
        date(2026, 8, 1), date(2026, 8, 31), include_closed=True
    )
    by_type = {row["type"]: row for row in rows}

    assert by_type["cost_cutting"]["status"] == "watch"
    assert by_type["cost_cutting"]["evidence"] == raw_evidence
    assert by_type["cost_cutting"]["suggested_action"] == "Reduce this expense."
    assert by_type["cost_cutting"]["effective_suggested_action"] is None
    assert by_type["cost_cutting"]["action_priority"] == "suppressed"
    assert by_type["anomalies"]["status"] == "ignored"
    assert by_type["anomalies"]["evidence"]["possibility"] == "detected"
    assert by_type["anomalies"]["action_priority"] == "normal"

    preferences.delete("merchant", "LOCAL GROCER")
    restored = InsightEngine(db_path).list_findings(
        date(2026, 8, 1), date(2026, 8, 31), include_closed=True
    )
    restored_by_type = {row["type"]: row for row in restored}
    assert restored_by_type["cost_cutting"]["status"] == "watch"
    assert restored_by_type["cost_cutting"]["effective_suggested_action"] == (
        "Reduce this expense."
    )
    assert restored_by_type["cost_cutting"]["action_priority"] == "normal"
    assert restored_by_type["cost_cutting"]["preference_context"] is None
