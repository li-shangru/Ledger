"""Balance series must fold in the account's transfer/payment legs."""

from datetime import date, datetime
from decimal import Decimal

from ledger.analysis.balance_calculator import BalanceCalculator
from ledger.core import Transaction
from ledger.core.analysis_models import Account


def _account(account_id: str, account_type: str, is_credit_card: bool, limit=None) -> Account:
    now = datetime(2026, 1, 1)
    return Account(
        account_id=account_id,
        institution="harbor",
        account_number=f"{account_id}-num",
        account_type=account_type,
        account_subtype=account_type,
        first_transaction_date=date(2026, 1, 1),
        last_transaction_date=date(2026, 2, 5),
        is_active=True,
        transaction_count=2,
        avg_monthly_transactions=Decimal("2"),
        is_credit_card=is_credit_card,
        detected_credit_limit=limit,
        created_at=now,
        updated_at=now,
    )


def _txn(i: int, d: date, amt: Decimal) -> Transaction:
    # Derive flow/cashflow_type from sign so fixtures are always internally consistent.
    cf = "income" if amt > 0 else "expense"
    return Transaction(
        txn_id=f"t{i}",
        txn_fingerprint=f"fp{i}",
        posting_date=d,
        institution="harbor",
        account="acct",
        account_type="visa",
        flow=cf,
        cashflow_type=cf,
        include_in_cashflow=True,
        currency="CAD",
        amount_raw=abs(amt),
        amount_signed=amt,
        description="x",
        source_file="x.csv",
        source_path="x.csv",
        source_period="2026-01",
        source_row=i,
        imported_at=date(2026, 1, 1),
        parser_version="t",
    )


def test_card_balance_folds_in_payment_legs(tmp_path):
    """Two $1000 purchases with a $1000 payment between -> deepest owed $1000, not $2000.
    The payment leg is stored NEGATIVE (Cedar-style) but on a card still reduces debt."""
    calc = BalanceCalculator(tmp_path / "x.db")
    txns = [
        _txn(0, date(2026, 1, 5), Decimal("-1000")),
        _txn(1, date(2026, 2, 5), Decimal("-1000")),
    ]
    legs = [{"posting_date": date(2026, 1, 20), "amount_raw": Decimal("-1000")}]

    balances = calc.compute_daily_balances(
        "a1", txns, _account("a1", "credit_card", True, Decimal("5000")), legs
    )

    assert balances[-1].balance == Decimal("-1000.00")  # was -2000 ignoring the payment
    assert balances[-1].balance_basis == "relative"
    # A zero-seeded change series is not an absolute card balance and therefore
    # cannot support a utilization percentage.
    assert balances[-1].credit_utilization is None


def test_deposit_balance_uses_natural_transfer_sign(tmp_path):
    """An outbound chequing transfer (stored negative) reduces the balance."""
    calc = BalanceCalculator(tmp_path / "x.db")
    txns = [_txn(0, date(2026, 1, 5), Decimal("5000"))]
    legs = [{"posting_date": date(2026, 1, 10), "amount_raw": Decimal("-2000")}]

    balances = calc.compute_daily_balances("c1", txns, _account("c1", "checking", False), legs)

    assert balances[-1].balance == Decimal("3000.00")  # 5000 - 2000


def test_balance_series_skips_non_cad_transfer_leg(tmp_path):
    """A non-CAD transfer leg is stored without FX conversion; it must NOT be folded
    into the CAD balance series at par (mirrors the transaction-side CAD guard)."""
    calc = BalanceCalculator(tmp_path / "x.db")
    txns = [_txn(0, date(2026, 1, 5), Decimal("5000"))]
    legs = [
        {"posting_date": date(2026, 1, 10), "amount_raw": Decimal("-2000"), "currency": "CAD"},
        {"posting_date": date(2026, 1, 10), "amount_raw": Decimal("-9999"), "currency": "USD"},
    ]

    balances = calc.compute_daily_balances("c1", txns, _account("c1", "checking", False), legs)

    # 5000 - 2000 (CAD leg only); the USD leg is skipped, not summed at par.
    assert balances[-1].balance == Decimal("3000.00")


def test_credit_limit_estimate_skips_non_cad_transfer_leg(tmp_path):
    """The deepest-balance credit-limit estimate must ignore non-CAD payment legs;
    an un-FX-converted USD leg would otherwise distort the estimate + utilization."""
    from ledger.analysis.account_detector import AccountDetector

    detector = AccountDetector(tmp_path / "x.db")
    txns = [_txn(0, date(2026, 1, 20), Decimal("-5000"))]  # CAD purchase
    legs = [{"posting_date": date(2026, 1, 5), "amount_raw": Decimal("3000"), "currency": "USD"}]

    limit = detector._detect_credit_limit(txns, legs)

    # USD leg skipped -> deepest owed is the full -5000 -> limit 5000. If the USD leg
    # were folded (as a +3000 payment before the purchase) the estimate would be 2000.
    assert limit == Decimal("5000")
