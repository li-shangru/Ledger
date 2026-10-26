"""
Model-level invariant: include_in_cashflow=True expense must be negative,
include_in_cashflow=True income must be positive.

Also covers:
- transfer_from_db round-trip (must NOT raise despite non-zero amount_signed)
- Neutral/non-cashflow rows (must NOT raise regardless of sign)
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from ledger.core import Transaction
from ledger.storage.database import TransferDB

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_POSTED = date(2025, 1, 15)
_NOW = datetime.now(UTC).replace(tzinfo=None)


def _base(
    *,
    cashflow_type: str,
    include_in_cashflow: bool,
    amount_signed: Decimal,
    flow: str = "expense",
) -> Transaction:
    """Minimal valid Transaction with overridable cashflow fields."""
    return Transaction(
        txn_id="test-txn",
        txn_fingerprint="fp-test",
        posting_date=_POSTED,
        institution="testbank",
        account="acc-001",
        account_type="chequing",
        flow=flow,
        cashflow_type=cashflow_type,
        include_in_cashflow=include_in_cashflow,
        currency="CAD",
        amount_raw=abs(amount_signed),
        amount_signed=amount_signed,
        description="Test transaction",
        source_file="test.csv",
        source_path="test.csv",
        source_period="2025-01",
        source_row=1,
        imported_at=_NOW,
        parser_version="test",
    )


# ---------------------------------------------------------------------------
# Tests 1 & 2: invalid cases that must raise BEFORE the fix (xfail baseline)
# and AFTER the fix (xfail removed, they should just raise).
# ---------------------------------------------------------------------------


def test_positive_expense_in_cashflow_is_rejected():
    """A cashflow expense with positive amount_signed must raise ValidationError.

    This catches the documented sign-inconsistency bug: expense should always
    represent money out (negative), so a positive value is clearly wrong.
    """
    with pytest.raises(ValidationError, match="expense"):
        _base(
            cashflow_type="expense",
            include_in_cashflow=True,
            amount_signed=Decimal("50.00"),
        )


def test_negative_income_in_cashflow_is_rejected():
    """A cashflow income with negative amount_signed must raise ValidationError.

    Income should always be positive (money in), so a negative value is clearly
    wrong.
    """
    with pytest.raises(ValidationError, match="income"):
        _base(
            cashflow_type="income",
            include_in_cashflow=True,
            amount_signed=Decimal("-100.00"),
            flow="income",
        )


# ---------------------------------------------------------------------------
# Test 3: Legitimate constructions that must NOT raise
# ---------------------------------------------------------------------------


def test_normal_expense_constructs_without_error():
    """A standard expense (negative amount) must not raise."""
    txn = _base(
        cashflow_type="expense",
        include_in_cashflow=True,
        amount_signed=Decimal("-42.50"),
    )
    assert txn.amount_signed == Decimal("-42.50")


def test_normal_income_constructs_without_error():
    """A standard income (positive amount) must not raise."""
    txn = _base(
        cashflow_type="income",
        include_in_cashflow=True,
        amount_signed=Decimal("2400.00"),
        flow="income",
    )
    assert txn.amount_signed == Decimal("2400.00")


def test_zero_expense_in_cashflow_constructs_without_error():
    """amount_signed=0 expense must NOT raise (edge-case guard, uses >0 not >=0)."""
    txn = _base(
        cashflow_type="expense",
        include_in_cashflow=True,
        amount_signed=Decimal("0.00"),
    )
    assert txn.amount_signed == Decimal("0.00")


def test_zero_income_in_cashflow_constructs_without_error():
    """amount_signed=0 income must NOT raise (edge-case guard, uses <0 not <=0)."""
    txn = _base(
        cashflow_type="income",
        include_in_cashflow=True,
        amount_signed=Decimal("0.00"),
        flow="income",
    )
    assert txn.amount_signed == Decimal("0.00")


def test_transfer_with_zero_amount_constructs_without_error():
    """Parser-created transfer leg (amount_signed=0, include_in_cashflow=False) must not raise."""
    txn = _base(
        cashflow_type="transfer",
        include_in_cashflow=False,
        amount_signed=Decimal("0.00"),
        flow="transfer",
    )
    assert txn.cashflow_type == "transfer"


def test_transfer_with_nonzero_positive_amount_constructs_without_error():
    """transfer_from_db can set positive amount_signed — must not raise."""
    txn = _base(
        cashflow_type="transfer",
        include_in_cashflow=False,
        amount_signed=Decimal("500.00"),
        flow="transfer",
    )
    assert txn.amount_signed == Decimal("500.00")


def test_transfer_with_nonzero_negative_amount_constructs_without_error():
    """transfer_from_db can set negative amount_signed — must not raise."""
    txn = _base(
        cashflow_type="transfer",
        include_in_cashflow=False,
        amount_signed=Decimal("-500.00"),
        flow="transfer",
    )
    assert txn.amount_signed == Decimal("-500.00")


def test_neutral_row_with_cashflow_false_constructs_without_error():
    """include_in_cashflow=False neutral row must not raise regardless of amount."""
    txn = _base(
        cashflow_type="neutral",
        include_in_cashflow=False,
        amount_signed=Decimal("0.00"),
    )
    assert txn.include_in_cashflow is False


def test_expense_with_cashflow_false_constructs_without_error():
    """An expense row with include_in_cashflow=False is NOT validated for sign
    (the anomaly-detection test relies on this with a negative amount, but we
    verify the validator doesn't fire even if sign is correct)."""
    txn = _base(
        cashflow_type="expense",
        include_in_cashflow=False,
        amount_signed=Decimal("-2000.00"),
    )
    assert txn.include_in_cashflow is False


# ---------------------------------------------------------------------------
# Test 4: transfer_from_db round-trip
# ---------------------------------------------------------------------------


def test_transfer_from_db_round_trip_constructs_without_error():
    """transfer_from_db sets amount_signed=amount_raw (non-zero, any sign) with
    cashflow_type='transfer' and include_in_cashflow=False.  The model validator
    must NOT reject this — it only checks include_in_cashflow=True rows."""
    from ledger.storage.mappers import transfer_from_db

    row = TransferDB(
        txn_id="t-roundtrip",
        posting_date=date(2025, 6, 18),
        institution="harbor",
        account="harbor-chequing",
        account_type="Chequing",
        flow="transfer",
        amount_raw=Decimal("-500.00"),
        description="E-TRANSFER OUT",
        source_file="harbor-chequing.csv",
        imported_at=_NOW,
        paired_transfer_id=None,
        transfer_confidence=None,
        counterparty_account=None,
        counterparty_institution=None,
    )

    txn = transfer_from_db(row)

    # Critically: amount_signed == amount_raw (non-zero, negative) must NOT raise
    assert txn.amount_signed == Decimal("-500.00")
    assert txn.cashflow_type == "transfer"
    assert txn.include_in_cashflow is False


def test_nan_amount_raw_raises_validation_error():
    """Defense-in-depth (Fix 1): constructing a Transaction with amount_raw=NaN
    must raise ValidationError, not silently store a non-finite Decimal that
    would later crash quantize() or poison running-balance sums."""
    with pytest.raises((ValidationError, ValueError)):
        Transaction(
            txn_id="test-nan",
            txn_fingerprint="fp-nan",
            posting_date=_POSTED,
            institution="testbank",
            account="acc-001",
            account_type="chequing",
            flow="expense",
            cashflow_type="expense",
            include_in_cashflow=True,
            currency="CAD",
            amount_raw=Decimal("NaN"),
            amount_signed=Decimal("-10.00"),
            description="Test NaN",
            source_file="test.csv",
            source_path="test.csv",
            source_period="2025-01",
            source_row=1,
            imported_at=_NOW,
            parser_version="test",
        )


def test_transfer_from_db_positive_amount_round_trip():
    """transfer_from_db with positive amount_raw (inflow leg) must also construct fine."""
    from ledger.storage.mappers import transfer_from_db

    row = TransferDB(
        txn_id="t-inflow",
        posting_date=date(2025, 6, 18),
        institution="harbor",
        account="harbor-spark-visa",
        account_type="SPARK Visa",
        flow="transfer",
        amount_raw=Decimal("500.00"),
        description="PAYMENT THANK YOU",
        source_file="cedar.csv",
        imported_at=_NOW,
        paired_transfer_id="t-roundtrip",
        transfer_confidence=Decimal("0.95"),
        counterparty_account=None,
        counterparty_institution=None,
    )

    txn = transfer_from_db(row)

    assert txn.amount_signed == Decimal("500.00")
    assert txn.cashflow_type == "transfer"
    assert txn.include_in_cashflow is False
