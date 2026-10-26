"""
Tests for the current query-service anomaly and duplicate helpers.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ledger.core import Transaction
from ledger.processing import ProcessingResult
from ledger.query import LedgerQueryService
from ledger.storage.database import init_database
from ledger.storage.writer import DatabaseWriter


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "test_anomalies.db"
    init_database(db_path)
    return db_path


def create_transaction(
    txn_id: str,
    merchant: str,
    amount: Decimal,
    category: str,
    posting_date: date,
) -> Transaction:
    day_of_week = posting_date.weekday()

    return Transaction(
        txn_id=txn_id,
        txn_fingerprint=f"fp_{txn_id}",
        posting_date=posting_date,
        transaction_date=posting_date,
        institution="testbank",
        account="1234",
        account_type="credit_card",
        flow="expense",
        cashflow_type="expense",
        include_in_cashflow=True,
        currency="CAD",
        amount_raw=amount,
        amount_signed=-amount,
        description=f"{merchant} purchase",
        merchant=merchant,
        category=category,
        merchant_confidence="high",
        category_confidence="high",
        exclude_from_spending=False,
        merchant_raw=merchant,
        category_raw=category,
        source_file="test.csv",
        source_path="test.csv",
        source_period=posting_date.strftime("%Y-%m"),
        source_row=0,
        imported_at=datetime.now(UTC).replace(tzinfo=None),
        parser_version="test",
        day_of_week=day_of_week,
        day_of_month=posting_date.day,
        week_of_year=posting_date.isocalendar()[1],
        is_weekend=day_of_week >= 5,
        is_month_start=posting_date.day <= 3,
        is_month_end=posting_date.day >= 28,
    )


def write_transactions(db_path: Path, transactions: list[Transaction]) -> None:
    writer = DatabaseWriter(db_path)
    writer.write_result(
        ProcessingResult(
            transactions=transactions,
            transfers=[],
            exceptions=[],
            total_scanned=len(transactions),
        ),
        Path("/test"),
        "test",
    )


def test_detect_anomalies_flags_category_outlier(temp_db: Path):
    base_date = date(2025, 12, 1)
    transactions = [
        create_transaction(
            txn_id=f"normal_{idx}",
            merchant="Restaurant",
            amount=Decimal("25.00"),
            category="Dining",
            posting_date=base_date + timedelta(days=idx),
        )
        for idx in range(10)
    ]
    transactions.append(
        create_transaction(
            txn_id="outlier",
            merchant="Restaurant",
            amount=Decimal("200.00"),
            category="Dining",
            posting_date=base_date + timedelta(days=11),
        )
    )
    write_transactions(temp_db, transactions)

    anomalies = LedgerQueryService(temp_db).detect_anomalies(
        start_date=base_date,
        end_date=base_date + timedelta(days=31),
        threshold=2.0,
    )

    assert [txn.txn_id for txn in anomalies] == ["outlier"]


def test_detect_anomalies_leave_one_out_unmasks_self_hiding_max(temp_db: Path):
    """The category's largest spend must not inflate its OWN threshold and hide itself;
    leave-one-out judges each candidate against the OTHER transactions."""
    base = date(2025, 12, 1)
    amounts = [Decimal(a) for a in ("90", "100", "110", "100", "400")]
    txns = [
        create_transaction(f"t{i}", "Cafe", amt, "Dining", base + timedelta(days=i))
        for i, amt in enumerate(amounts)
    ]
    write_transactions(temp_db, txns)

    anomalies = LedgerQueryService(temp_db).detect_anomalies(
        start_date=base, end_date=base + timedelta(days=31), threshold=2.0
    )

    # Inclusive mean+2*std ~= 400.3 would NOT flag the $400; leave-one-out (others mean
    # 100, std ~7) flags it clearly.
    assert [txn.txn_id for txn in anomalies] == ["t4"]


def test_detect_anomalies_needs_category_history(temp_db: Path):
    base_date = date(2025, 12, 1)
    write_transactions(
        temp_db,
        [
            create_transaction("small", "Store", Decimal("10.00"), "Shopping", base_date),
            create_transaction("large", "Store", Decimal("500.00"), "Shopping", base_date),
        ],
    )

    anomalies = LedgerQueryService(temp_db).detect_anomalies(
        start_date=base_date,
        end_date=base_date + timedelta(days=1),
        threshold=2.0,
    )

    assert anomalies == []


def test_get_duplicate_candidates_matches_same_merchant_amount_nearby(temp_db: Path):
    base_date = date(2025, 12, 15)
    write_transactions(
        temp_db,
        [
            create_transaction("original", "Coffee Shop", Decimal("5.99"), "Dining", base_date),
            create_transaction(
                "duplicate",
                "Coffee Shop",
                Decimal("5.99"),
                "Dining",
                base_date + timedelta(days=1),
            ),
            create_transaction(
                "different",
                "Coffee Shop",
                Decimal("7.99"),
                "Dining",
                base_date + timedelta(days=1),
            ),
        ],
    )

    candidates = LedgerQueryService(temp_db).get_duplicate_candidates(threshold_days=2)

    assert len(candidates) == 1
    assert {candidates[0][0].txn_id, candidates[0][1].txn_id} == {"original", "duplicate"}


def test_get_duplicate_candidates_respects_period(temp_db: Path):
    base_date = date(2025, 12, 15)
    write_transactions(
        temp_db,
        [
            create_transaction("old_a", "Coffee Shop", Decimal("5.99"), "Dining", base_date),
            create_transaction(
                "old_b",
                "Coffee Shop",
                Decimal("5.99"),
                "Dining",
                base_date + timedelta(days=1),
            ),
            create_transaction(
                "new_a",
                "Coffee Shop",
                Decimal("5.99"),
                "Dining",
                base_date + timedelta(days=10),
            ),
            create_transaction(
                "new_b",
                "Coffee Shop",
                Decimal("5.99"),
                "Dining",
                base_date + timedelta(days=11),
            ),
        ],
    )

    candidates = LedgerQueryService(temp_db).get_duplicate_candidates(
        threshold_days=2,
        start_date=base_date + timedelta(days=10),
        end_date=base_date + timedelta(days=12),
    )

    assert len(candidates) == 1
    assert {candidates[0][0].txn_id, candidates[0][1].txn_id} == {"new_a", "new_b"}


def test_detect_anomalies_excludes_exclude_from_spending(temp_db: Path):
    """detect_anomalies must NOT surface transactions with exclude_from_spending=True.

    Setup: 5+ normal small expenses in "Dining" + one large excluded expense in the
    same category/window. Without the filter the excluded transaction would be the
    clear outlier; with it, no anomaly is returned.
    """
    base_date = date(2025, 12, 1)

    # 5 normal $30 dining transactions
    transactions = [
        create_transaction(
            txn_id=f"normal_{idx}",
            merchant="Restaurant",
            amount=Decimal("30.00"),
            category="Dining",
            posting_date=base_date + timedelta(days=idx),
        )
        for idx in range(5)
    ]

    # One large expense that is excluded from spending — would be a clear outlier
    # if the filter were missing (1500 >> mean 30, std ~0)
    excluded = Transaction(
        txn_id="excluded_big",
        txn_fingerprint="fp_excluded_big",
        posting_date=base_date + timedelta(days=5),
        transaction_date=base_date + timedelta(days=5),
        institution="testbank",
        account="1234",
        account_type="credit_card",
        flow="expense",
        cashflow_type="expense",
        include_in_cashflow=True,
        currency="CAD",
        amount_raw=Decimal("1500.00"),
        amount_signed=Decimal("-1500.00"),
        description="Big reimbursable",
        merchant="Supplier",
        category="Dining",
        merchant_confidence="high",
        category_confidence="high",
        exclude_from_spending=True,  # <-- this is the flag under test
        merchant_raw="Supplier",
        category_raw="Dining",
        source_file="test.csv",
        source_path="test.csv",
        source_period=base_date.strftime("%Y-%m"),
        source_row=99,
        imported_at=datetime.now(UTC).replace(tzinfo=None),
        parser_version="test",
        day_of_week=(base_date + timedelta(days=5)).weekday(),
        day_of_month=(base_date + timedelta(days=5)).day,
        week_of_year=(base_date + timedelta(days=5)).isocalendar()[1],
        is_weekend=(base_date + timedelta(days=5)).weekday() >= 5,
        is_month_start=(base_date + timedelta(days=5)).day <= 3,
        is_month_end=(base_date + timedelta(days=5)).day >= 28,
    )
    transactions.append(excluded)
    write_transactions(temp_db, transactions)

    anomalies = LedgerQueryService(temp_db).detect_anomalies(
        start_date=base_date,
        end_date=base_date + timedelta(days=31),
        threshold=2.0,
    )

    txn_ids = [t.txn_id for t in anomalies]
    assert "excluded_big" not in txn_ids, (
        "detect_anomalies must not surface exclude_from_spending=True transactions"
    )


def test_detect_anomalies_excludes_include_in_cashflow_false(temp_db: Path):
    """detect_anomalies must NOT surface transactions with include_in_cashflow=False.

    Setup: 5 normal small expenses in "Groceries" (exclude_from_spending=False so only
    include_in_cashflow=False can exclude it) plus one large expense with
    include_in_cashflow=False. Without the filter the large txn would be the clear outlier;
    with it, no anomaly is returned.
    """
    base_date = date(2025, 11, 1)

    # 5 normal $40 grocery transactions (all flags matching the spend universe)
    transactions = [
        create_transaction(
            txn_id=f"normal_{idx}",
            merchant="Superstore",
            amount=Decimal("40.00"),
            category="Groceries",
            posting_date=base_date + timedelta(days=idx),
        )
        for idx in range(5)
    ]

    # One large expense that is excluded via include_in_cashflow=False.
    # exclude_from_spending=False so ONLY include_in_cashflow can block it.
    excluded_cashflow = Transaction(
        txn_id="excluded_cashflow",
        txn_fingerprint="fp_excluded_cashflow",
        posting_date=base_date + timedelta(days=5),
        transaction_date=base_date + timedelta(days=5),
        institution="testbank",
        account="1234",
        account_type="credit_card",
        flow="expense",
        cashflow_type="expense",
        include_in_cashflow=False,  # <-- this is the flag under test
        currency="CAD",
        amount_raw=Decimal("2000.00"),
        amount_signed=Decimal("-2000.00"),
        description="Non-cashflow big spend",
        merchant="Wholesaler",
        category="Groceries",
        merchant_confidence="high",
        category_confidence="high",
        exclude_from_spending=False,  # intentionally False — only include_in_cashflow blocks it
        merchant_raw="Wholesaler",
        category_raw="Groceries",
        source_file="test.csv",
        source_path="test.csv",
        source_period=base_date.strftime("%Y-%m"),
        source_row=99,
        imported_at=datetime.now(UTC).replace(tzinfo=None),
        parser_version="test",
        day_of_week=(base_date + timedelta(days=5)).weekday(),
        day_of_month=(base_date + timedelta(days=5)).day,
        week_of_year=(base_date + timedelta(days=5)).isocalendar()[1],
        is_weekend=(base_date + timedelta(days=5)).weekday() >= 5,
        is_month_start=(base_date + timedelta(days=5)).day <= 3,
        is_month_end=(base_date + timedelta(days=5)).day >= 28,
    )
    transactions.append(excluded_cashflow)
    write_transactions(temp_db, transactions)

    anomalies = LedgerQueryService(temp_db).detect_anomalies(
        start_date=base_date,
        end_date=base_date + timedelta(days=31),
        threshold=2.0,
    )

    txn_ids = [t.txn_id for t in anomalies]
    assert "excluded_cashflow" not in txn_ids, (
        "detect_anomalies must not surface include_in_cashflow=False transactions"
    )
