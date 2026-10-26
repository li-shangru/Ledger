"""Tests for deterministic true-cost adjustment treatments."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ledger.adapters.registry import CompatibilityPolicySnapshot
from ledger.adjustments import AdjustmentDetectionService
from ledger.exporting import ExportPackageService
from ledger.storage.database import (
    TransactionDB,
    TransactionOffsetDB,
    TransactionTreatmentDB,
    init_database,
    session_scope,
)


@pytest.fixture
def invented_reimbursement_policy(monkeypatch):
    policy = CompatibilityPolicySnapshot(
        digest="invented-reimbursement-policy",
        credit_institutions=frozenset(),
        credit_account_tokens=(),
        payment_tokens=(),
        reimbursement_merchants=frozenset({"INVENTED BENEFITS"}),
    )
    monkeypatch.setattr("ledger.adjustments.service.compatibility_policy_snapshot", lambda: policy)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _temporal_fields(posting_date: date) -> dict:
    return {
        "day_of_week": posting_date.weekday(),
        "day_of_month": posting_date.day,
        "week_of_year": posting_date.isocalendar()[1],
        "is_weekend": posting_date.weekday() >= 5,
        "is_month_start": posting_date.day <= 3,
        "is_month_end": posting_date.day >= 28,
    }


def _insert_txn(
    db_path: Path,
    txn_id: str,
    posted: date,
    amount: str,
    merchant: str,
    category: str,
    *,
    description: str | None = None,
    account_type: str = "visa",
) -> None:
    value = Decimal(amount)
    with session_scope(db_path) as session:
        session.add(
            TransactionDB(
                txn_id=txn_id,
                posting_date=posted,
                transaction_date=posted,
                institution="test-bank",
                account="account-0001",
                account_type=account_type,
                flow="expense" if value < 0 else "income",
                cashflow_type="expense" if value < 0 else "income",
                include_in_cashflow=True,
                currency="CAD",
                amount_raw=abs(value),
                amount_signed=value,
                description=description or merchant,
                merchant=merchant,
                category=category,
                source_file="statement.csv",
                source_path="/private/statement.csv",
                source_period=posted.strftime("%Y-%m"),
                source_row=1,
                imported_at=_now(),
                parser_version="test",
                txn_fingerprint=f"{txn_id}-fingerprint",
                **_temporal_fields(posted),
            )
        )


def test_adjustment_dry_run_does_not_mutate_db(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_txn(db_path, "debit", date(2026, 1, 1), "-100.00", "APPLE", "Electronics")
    _insert_txn(db_path, "credit", date(2026, 1, 5), "100.00", "APPLE", "Electronics")

    result = AdjustmentDetectionService(db_path).detect(execute=False)

    assert result.ok
    assert result.counts["product_return"] == 1
    with session_scope(db_path) as session:
        assert session.query(TransactionTreatmentDB).count() == 0
        assert session.query(TransactionOffsetDB).count() == 0


def test_product_return_execute_records_offset_without_rewriting_transactions(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_txn(db_path, "debit", date(2026, 1, 1), "-500.00", "BEST BUY", "Electronics")
    _insert_txn(
        db_path,
        "credit",
        date(2026, 1, 8),
        "500.00",
        "BEST BUY",
        "Electronics",
        description="BEST BUY (Credit)",
    )

    result = AdjustmentDetectionService(db_path).detect(execute=True)

    assert result.ok
    with session_scope(db_path) as session:
        treatment = session.get(TransactionTreatmentDB, "credit")
        offset = session.query(TransactionOffsetDB).one()
        debit = session.get(TransactionDB, "debit")
        credit = session.get(TransactionDB, "credit")
        assert treatment is not None
        assert treatment.treatment_type == "product_return"
        assert treatment.netting_policy == "reduce_category_cost"
        assert offset.debit_txn_id == "debit"
        assert offset.offset_amount == Decimal("500.00")
        assert debit.amount_signed == Decimal("-500.00")
        assert credit.amount_signed == Decimal("500.00")


def test_reimbursement_reward_and_insurance_premium_classification(
    tmp_path: Path, invented_reimbursement_policy
) -> None:
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_txn(
        db_path,
        "health-expense",
        date(2026, 1, 1),
        "-200.00",
        "DENTIST",
        "Health & Wellness",
    )
    _insert_txn(
        db_path,
        "invented-benefits",
        date(2026, 1, 20),
        "200.00",
        "INVENTED BENEFITS",
        "Insurance",
        description="Misc Payment INVENTED BENEFITS",
        account_type="chequing",
    )
    _insert_txn(
        db_path,
        "reward",
        date(2026, 1, 21),
        "25.00",
        "CEDAR",
        "Utilities",
        description="Cashback reward",
    )
    _insert_txn(
        db_path,
        "premium",
        date(2026, 1, 22),
        "-150.00",
        "PINE HOME & AUTO INSURANCE",
        "Insurance",
        account_type="chequing",
    )

    result = AdjustmentDetectionService(db_path).detect(execute=True)

    assert result.ok
    with session_scope(db_path) as session:
        rows = {
            row.txn_id: (row.treatment_type, row.target_category, row.netting_policy)
            for row in session.query(TransactionTreatmentDB).order_by(TransactionTreatmentDB.txn_id)
        }

    assert rows["invented-benefits"] == (
        "reimbursement",
        "Health & Wellness",
        "reduce_category_cost",
    )
    assert rows["reward"] == ("reward_rebate", None, "exclude_from_income")
    assert rows["premium"] == ("insurance_premium", "Insurance", "gross_cost")


def test_true_cost_summary_nets_offsets_but_preserves_gross(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_txn(db_path, "income", date(2026, 1, 1), "1000.00", "PAYROLL", "Income")
    _insert_txn(db_path, "debit", date(2026, 1, 2), "-100.00", "APPLE", "Electronics")
    _insert_txn(db_path, "credit", date(2026, 1, 3), "40.00", "APPLE", "Electronics")
    _insert_txn(
        db_path,
        "reward",
        date(2026, 1, 4),
        "10.00",
        "CEDAR",
        "Utilities",
        description="Cashback reward",
    )

    service = AdjustmentDetectionService(db_path)
    service.detect(execute=True)
    summary = service.true_cost_summary(date(2026, 1, 1), date(2026, 1, 31))
    categories = service.true_cost_by_category(date(2026, 1, 1), date(2026, 1, 31))

    assert summary.gross_spend == Decimal("100.00")
    assert summary.offsets == Decimal("40.00")
    assert summary.true_cost == Decimal("60.00")
    assert summary.gross_income == Decimal("1050.00")
    assert summary.adjusted_income == Decimal("1000.00")
    assert categories["Electronics"]["gross_spend"] == Decimal("100.00")
    assert categories["Electronics"]["offsets"] == Decimal("40.00")
    assert categories["Electronics"]["true_cost"] == Decimal("60.00")


def test_reimbursement_matches_clinic_debit_across_merchants(
    tmp_path: Path, invented_reimbursement_policy
) -> None:
    """An insurer reimbursement (INVENTED BENEFITS, Insurance) must offset the health expense it
    pays back, even though the expense is at a different merchant (the clinic). Before
    this, reimbursements required merchant equality and so offset nothing."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_txn(
        db_path, "dental", date(2026, 1, 5), "-200.00", "MARKHAM DENTAL", "Health & Wellness"
    )
    _insert_txn(
        db_path,
        "invented-benefits",
        date(2026, 1, 20),
        "200.00",
        "INVENTED BENEFITS",
        "Insurance",
        description="Misc Payment INVENTED BENEFITS",
        account_type="chequing",
    )

    service = AdjustmentDetectionService(db_path)
    service.detect(execute=True)

    with session_scope(db_path) as session:
        offset = session.query(TransactionOffsetDB).one()
        assert offset.credit_txn_id == "invented-benefits"
        assert offset.debit_txn_id == "dental"
        assert offset.offset_amount == Decimal("200.00")

    summary = service.true_cost_summary(date(2026, 1, 1), date(2026, 1, 31))
    assert summary.true_cost == Decimal("0.00")  # $200 health spend fully reimbursed


def test_reimbursement_spills_across_multiple_debits(
    tmp_path: Path, invented_reimbursement_policy
) -> None:
    """One reimbursement must net every expense it covers, spilling across multiple debits
    instead of dropping the remainder once its single best debit is exhausted.

    A sibling credit consumed
    most of the exact-match debit first, so the next credit found only a sliver of headroom
    and the rest was silently lost. The matcher must be consumed-aware and spill."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_txn(db_path, "d1", date(2026, 1, 1), "-100.00", "SYNTHETIC CLINIC", "Health & Wellness")
    _insert_txn(
        db_path, "d2", date(2026, 1, 2), "-200.00", "SYNTHETIC WELLNESS STORE", "Health & Wellness"
    )
    # c0 ($70) lands first and partly consumes d1 (its nearest match); c ($100) then can
    # only take d1's $30 leftover and must spill the remaining $70 onto d2.
    _insert_txn(
        db_path,
        "c0",
        date(2026, 1, 10),
        "70.00",
        "INVENTED BENEFITS",
        "Insurance",
        description="Misc Payment INVENTED BENEFITS",
        account_type="chequing",
    )
    _insert_txn(
        db_path,
        "c",
        date(2026, 1, 15),
        "100.00",
        "INVENTED BENEFITS",
        "Insurance",
        description="Misc Payment INVENTED BENEFITS",
        account_type="chequing",
    )

    service = AdjustmentDetectionService(db_path)
    service.detect(execute=True)

    with session_scope(db_path) as session:
        c_offsets = session.query(TransactionOffsetDB).filter_by(credit_txn_id="c").all()
        assert sum((o.offset_amount for o in c_offsets), Decimal("0")) == Decimal("100.00")
        assert len(c_offsets) == 2  # spilled across two debits, nothing dropped
        by_debit: dict[str, Decimal] = {}
        for o in session.query(TransactionOffsetDB):
            by_debit[o.debit_txn_id] = by_debit.get(o.debit_txn_id, Decimal("0")) + o.offset_amount
        assert by_debit["d1"] == Decimal("100.00")  # capped at its own amount
        assert by_debit["d2"] == Decimal("70.00")

    summary = service.true_cost_summary(date(2026, 1, 1), date(2026, 1, 31))
    assert summary.offsets == Decimal("170.00")  # 70 + 100, nothing dropped
    assert summary.true_cost == Decimal("130.00")  # 300 gross health spend - 170 reimbursed


def test_multiple_credits_cannot_over_offset_one_debit(tmp_path: Path) -> None:
    """Two refunds for the same single purchase must not offset more than the purchase
    (regression: nothing tracked debit consumption, so both credits netted the debit)."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_txn(db_path, "debit", date(2026, 1, 1), "-100.00", "APPLE", "Electronics")
    _insert_txn(
        db_path,
        "credit1",
        date(2026, 1, 5),
        "100.00",
        "APPLE",
        "Electronics",
        description="APPLE (Credit)",
    )
    _insert_txn(
        db_path,
        "credit2",
        date(2026, 1, 6),
        "100.00",
        "APPLE",
        "Electronics",
        description="APPLE (Credit)",
    )

    service = AdjustmentDetectionService(db_path)
    service.detect(execute=True)
    summary = service.true_cost_summary(date(2026, 1, 1), date(2026, 1, 31))

    assert summary.offsets == Decimal("100.00")  # capped at the single $100 purchase
    assert summary.true_cost == Decimal("0.00")


def test_offset_does_not_reduce_period_when_debit_is_out_of_period(tmp_path: Path) -> None:
    """A refund for a prior-period purchase must not cut THIS period's true cost; the
    offset is scoped to the matched expense (debit), not the credit."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_txn(db_path, "debit", date(2025, 12, 20), "-100.00", "APPLE", "Electronics")
    _insert_txn(
        db_path,
        "credit",
        date(2026, 1, 5),
        "100.00",
        "APPLE",
        "Electronics",
        description="APPLE (Credit)",
    )
    _insert_txn(db_path, "jan-spend", date(2026, 1, 10), "-500.00", "MARKET", "Groceries")

    service = AdjustmentDetectionService(db_path)
    service.detect(execute=True)
    summary = service.true_cost_summary(date(2026, 1, 1), date(2026, 1, 31))

    assert summary.gross_spend == Decimal("500.00")
    assert summary.offsets == Decimal("0.00")
    assert summary.true_cost == Decimal("500.00")


def test_cross_period_refund_does_not_inflate_income(tmp_path: Path) -> None:
    """A refund credit that posts in a LATER period than the purchase it offsets must not
    count as real income in the credit's period. The cancelling offset is scoped to the
    debit's (prior) period, so income must be netted in the credit's own period instead.

    Mirrors test_offset_does_not_reduce_period_when_debit_is_out_of_period, but asserts the
    INCOME side: previously gross_income=100 leaked through as adjusted_income."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_txn(db_path, "debit", date(2025, 12, 20), "-100.00", "APPLE", "Electronics")
    _insert_txn(
        db_path,
        "credit",
        date(2026, 1, 5),
        "100.00",
        "APPLE",
        "Electronics",
        description="APPLE (Credit)",
    )

    service = AdjustmentDetectionService(db_path)
    service.detect(execute=True)
    summary = service.true_cost_summary(date(2026, 1, 1), date(2026, 1, 31))

    # The refund credit posts in Jan but is not income; it nets in its own period.
    assert summary.gross_income == Decimal("100.00")
    assert summary.adjusted_income == Decimal("0.00")
    # Debit-scoped expense side is untouched: the offset matches a Dec purchase, so it does
    # not reduce Jan's (empty) spend.
    assert summary.offsets == Decimal("0.00")
    assert summary.true_cost == Decimal("0.00")


def test_same_period_refund_nets_income_without_double_reduction(tmp_path: Path) -> None:
    """A purchase and its refund credit in the SAME period must net once: the refund is
    removed from income and the expense side is reduced by the offset. Real payroll income
    in the period stays fully counted."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_txn(db_path, "income", date(2026, 1, 1), "1000.00", "PAYROLL", "Income")
    _insert_txn(db_path, "debit", date(2026, 1, 2), "-100.00", "APPLE", "Electronics")
    _insert_txn(
        db_path,
        "credit",
        date(2026, 1, 3),
        "100.00",
        "APPLE",
        "Electronics",
        description="APPLE (Credit)",
    )

    service = AdjustmentDetectionService(db_path)
    service.detect(execute=True)
    summary = service.true_cost_summary(date(2026, 1, 1), date(2026, 1, 31))

    # gross_income counts payroll + refund credit; adjusted strips the refund only.
    assert summary.gross_income == Decimal("1100.00")
    assert summary.adjusted_income == Decimal("1000.00")
    # Expense side reduced by the same-period offset.
    assert summary.gross_spend == Decimal("100.00")
    assert summary.offsets == Decimal("100.00")
    assert summary.true_cost == Decimal("0.00")
    assert summary.net == Decimal("1000.00")


def test_real_income_unaffected_by_offset_netting(tmp_path: Path) -> None:
    """Genuine income (payroll) that is not the credit side of any offset must count fully
    toward gross_income and adjusted_income."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_txn(db_path, "payroll", date(2026, 1, 15), "3000.00", "PAYROLL", "Income")
    _insert_txn(db_path, "rent", date(2026, 1, 1), "-1500.00", "LANDLORD", "Housing")

    service = AdjustmentDetectionService(db_path)
    service.detect(execute=True)
    summary = service.true_cost_summary(date(2026, 1, 1), date(2026, 1, 31))

    assert summary.gross_income == Decimal("3000.00")
    assert summary.adjusted_income == Decimal("3000.00")
    assert summary.offsets == Decimal("0.00")
    assert summary.net == Decimal("1500.00")


def test_export_includes_true_cost_fields_without_raw_source_data(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_txn(db_path, "debit", date(2026, 1, 2), "-100.00", "APPLE", "Electronics")
    _insert_txn(db_path, "credit", date(2026, 1, 3), "40.00", "APPLE", "Electronics")
    AdjustmentDetectionService(db_path).detect(execute=True)

    # The per-transaction CSV (true-cost fields + source-path redaction) is a
    # full-detail-export feature. chatgpt-safe is aggregates-only and refuses a
    # per-transaction csv, so this property is exercised via a full-detail preset.
    result = ExportPackageService(db_path).create_package(
        dest_dir=tmp_path / "exports",
        preset="full-local-audit",
        export_format="csv",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 31),
    )
    content = result.path.read_text()

    assert "true_cost_offset" in content
    assert "product_return" in content
    assert "/private/statement.csv" not in content


def test_annotation_map_attaches_offset_to_debit_not_credit(tmp_path: Path) -> None:
    """true_cost_offset must be the per-debit applied offset (attached to the DEBIT),
    not the credit's full amount on the credit, so the export's per-category offsets
    match true_cost_by_category (review remediation #19)."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_txn(db_path, "debit", date(2026, 1, 2), "-100.00", "APPLE", "Electronics")
    _insert_txn(db_path, "credit", date(2026, 1, 3), "40.00", "APPLE", "Electronics")
    service = AdjustmentDetectionService(db_path)
    service.detect(execute=True)

    annotations = service.annotation_map(["debit", "credit"])

    # The applied offset lands on the debit; the credit (the source) carries no offset.
    assert annotations["debit"]["true_cost_offset"] == 40.0
    assert annotations["credit"]["true_cost_offset"] == 0.0
    # The credit still carries its treatment annotation.
    assert annotations["credit"]["treatment_type"]
