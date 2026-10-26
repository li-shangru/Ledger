"""Regression tests: money aggregates must be cent-exact Decimals (no float tail).

func.sum(func.abs(amount_signed)) loses SQLAlchemy's Numeric typing, so SQLite
returns a Python float and Decimal(str(float)) carries binary FP error. These
tests pin the results to exact cents.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

from ledger.core import Transaction
from ledger.processing import ProcessingResult
from ledger.query import LedgerQueryService
from ledger.storage.database import init_database
from ledger.storage.writer import DatabaseWriter


def _expense(
    i: int, amount: Decimal, category: str = "TestCat", merchant: str = "TESTMERCHANT"
) -> Transaction:
    return Transaction(
        txn_id=f"txn_cents_{i}",
        txn_fingerprint=f"fp_cents_{i}",
        posting_date=date(2025, 3, 1),
        transaction_date=date(2025, 3, 1),
        institution="test",
        account="test-acct",
        flow="expense",
        cashflow_type="expense",
        include_in_cashflow=True,
        currency="CAD",
        amount_raw=abs(amount),
        amount_signed=amount,
        description=f"ITEM {i}",
        merchant=merchant,
        category=category,
        exclude_from_spending=False,
        merchant_raw="",
        category_raw="",
        source_file="test.csv",
        source_path="/test/test.csv",
        source_period="2025-03",
        source_row=i,
        imported_at=date(2025, 3, 2),
        parser_version="test",
    )


def _write(db_path: Path, txns: list[Transaction]) -> None:
    result = ProcessingResult(
        transactions=txns,
        transfers=[],
        exceptions=[],
        total_scanned=len(txns),
        duplicates_removed=0,
        collisions=0,
    )
    DatabaseWriter(db_path).write_result(result, Path("/test"), "test")


def test_spending_by_category_is_cent_quantized(tmp_path):
    # 0.10 + 0.20 == 0.30000000000000004 in float; the result must be exact cents.
    db_path = tmp_path / "cents.db"
    init_database(db_path)
    _write(db_path, [_expense(1, Decimal("-0.10")), _expense(2, Decimal("-0.20"))])

    spending = LedgerQueryService(db_path).get_spending_by_category(
        date(2025, 3, 1), date(2025, 3, 31)
    )

    total = spending["TestCat"]
    assert total == Decimal("0.30")
    assert total.as_tuple().exponent == -2  # quantized to cents, no float tail


def test_spending_by_merchant_is_cent_quantized(tmp_path):
    db_path = tmp_path / "merch.db"
    init_database(db_path)
    _write(db_path, [_expense(1, Decimal("-0.10")), _expense(2, Decimal("-0.20"))])

    spending = LedgerQueryService(db_path).get_spending_by_merchant(
        date(2025, 3, 1), date(2025, 3, 31)
    )

    total = spending["TESTMERCHANT"]
    assert total == Decimal("0.30")
    assert total.as_tuple().exponent == -2


def test_compute_kpis_expenses_are_cent_quantized(tmp_path):
    db_path = tmp_path / "kpis.db"
    init_database(db_path)
    _write(db_path, [_expense(1, Decimal("-0.10")), _expense(2, Decimal("-0.20"))])

    kpis = LedgerQueryService(db_path).compute_kpis(date(2025, 3, 1), date(2025, 3, 31))

    assert kpis["expenses"] == Decimal("0.30")
    assert kpis["expenses"].as_tuple().exponent == -2
    assert kpis["net"].as_tuple().exponent == -2  # income - expenses stays cent-exact
