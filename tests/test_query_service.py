"""
Test query service functionality.

Tests LedgerQueryService for querying, filtering, and aggregating transaction data.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from importlib import import_module
from importlib.util import find_spec
from inspect import signature
from pathlib import Path

import pytest

from ledger.core import Transaction
from ledger.processing import ProcessingResult
from ledger.query import LedgerQueryService
from ledger.storage.database import AccountDB, TransactionDB, init_database, session_scope
from ledger.storage.writer import DatabaseWriter
from ledger.utils.hashing import stable_id


@pytest.fixture
def populated_db(tmp_path):
    """Create a database populated with test transactions."""
    db_path = tmp_path / "test_query.db"
    init_database(db_path)

    # Create diverse test transactions
    transactions = [
        # January 2025 - Dining
        Transaction(
            txn_id="txn_dining_001",
            txn_fingerprint="fp_dining_001",
            posting_date=date(2025, 1, 15),
            transaction_date=date(2025, 1, 15),
            institution="examplebank",
            account="examplebank-sapphire",
            flow="expense",
            cashflow_type="expense",
            include_in_cashflow=True,
            currency="CAD",
            amount_raw=Decimal("45.50"),
            amount_signed=Decimal("-45.50"),
            description="MCDONALDS #12345",
            merchant="MCDONALDS",
            category="Dining",
            merchant_confidence="high",
            category_confidence="high",
            exclude_from_spending=False,
            merchant_raw="MCDONALDS #12345",
            category_raw="",
            source_file="examplebank_jan.csv",
            source_path="/test/examplebank_jan.csv",
            source_period="2025-01",
            source_row=1,
            imported_at=date(2025, 1, 20),
            parser_version="test",
        ),
        # January 2025 - Groceries
        Transaction(
            txn_id="txn_grocery_001",
            txn_fingerprint="fp_grocery_001",
            posting_date=date(2025, 1, 18),
            transaction_date=date(2025, 1, 18),
            institution="examplebank",
            account="examplebank-sapphire",
            flow="expense",
            cashflow_type="expense",
            include_in_cashflow=True,
            currency="CAD",
            amount_raw=Decimal("120.75"),
            amount_signed=Decimal("-120.75"),
            description="WHOLE FOODS MARKET",
            merchant="WHOLE FOODS",
            category="Groceries",
            merchant_confidence="high",
            category_confidence="high",
            exclude_from_spending=False,
            merchant_raw="WHOLE FOODS MARKET",
            category_raw="",
            source_file="examplebank_jan.csv",
            source_path="/test/examplebank_jan.csv",
            source_period="2025-01",
            source_row=2,
            imported_at=date(2025, 1, 20),
            parser_version="test",
        ),
        # January 2025 - Income
        Transaction(
            txn_id="txn_income_001",
            txn_fingerprint="fp_income_001",
            posting_date=date(2025, 1, 5),
            transaction_date=date(2025, 1, 5),
            institution="examplebank",
            account="examplebank-checking",
            flow="income",
            cashflow_type="income",
            include_in_cashflow=True,
            currency="CAD",
            amount_raw=Decimal("5000.00"),
            amount_signed=Decimal("5000.00"),
            description="PAYROLL DEPOSIT",
            merchant="",
            category="Income",
            merchant_confidence="none",
            category_confidence="high",
            exclude_from_spending=False,
            merchant_raw="",
            category_raw="",
            source_file="examplebank_jan.csv",
            source_path="/test/examplebank_jan.csv",
            source_period="2025-01",
            source_row=3,
            imported_at=date(2025, 1, 20),
            parser_version="test",
        ),
        # February 2025 - Dining
        Transaction(
            txn_id="txn_dining_002",
            txn_fingerprint="fp_dining_002",
            posting_date=date(2025, 2, 10),
            transaction_date=date(2025, 2, 10),
            institution="examplebank",
            account="examplebank-sapphire",
            flow="expense",
            cashflow_type="expense",
            include_in_cashflow=True,
            currency="CAD",
            amount_raw=Decimal("68.25"),
            amount_signed=Decimal("-68.25"),
            description="STARBUCKS STORE 5678",
            merchant="STARBUCKS",
            category="Dining",
            merchant_confidence="high",
            category_confidence="high",
            exclude_from_spending=False,
            merchant_raw="STARBUCKS STORE 5678",
            category_raw="",
            source_file="examplebank_feb.csv",
            source_path="/test/examplebank_feb.csv",
            source_period="2025-02",
            source_row=1,
            imported_at=date(2025, 2, 15),
            parser_version="test",
        ),
        # February 2025 - Shopping (excluded from spending)
        Transaction(
            txn_id="txn_shop_001",
            txn_fingerprint="fp_shop_001",
            posting_date=date(2025, 2, 14),
            transaction_date=date(2025, 2, 14),
            institution="exampleissuer",
            account="exampleissuer-gold",
            flow="expense",
            cashflow_type="expense",
            include_in_cashflow=True,
            currency="CAD",
            amount_raw=Decimal("1500.00"),
            amount_signed=Decimal("-1500.00"),
            description="AMAZON PURCHASE",
            merchant="AMAZON",
            category="Shopping",
            merchant_confidence="high",
            category_confidence="high",
            exclude_from_spending=True,  # Excluded from spending metrics
            merchant_raw="AMAZON PURCHASE",
            category_raw="",
            source_file="exampleissuer_feb.csv",
            source_path="/test/exampleissuer_feb.csv",
            source_period="2025-02",
            source_row=1,
            imported_at=date(2025, 2, 15),
            parser_version="test",
        ),
    ]

    # Write to database
    result = ProcessingResult(
        transactions=transactions,
        transfers=[],
        exceptions=[],
        total_scanned=5,
        duplicates_removed=0,
        collisions=0,
    )
    writer = DatabaseWriter(db_path)
    writer.write_result(result, Path("/test"), "test_version")

    now = datetime.now(UTC).replace(tzinfo=None)
    with session_scope(db_path) as session:
        session.add_all(
            [
                AccountDB(
                    account_id=stable_id("examplebank", "examplebank-sapphire"),
                    institution="examplebank",
                    account_number="examplebank-sapphire",
                    account_type="credit_card",
                    account_subtype="sapphire",
                    first_transaction_date=date(2025, 1, 15),
                    last_transaction_date=date(2025, 2, 10),
                    is_active=True,
                    transaction_count=3,
                    avg_monthly_transactions=Decimal("1.50"),
                    is_credit_card=True,
                    detected_credit_limit=None,
                    created_at=now,
                    updated_at=now,
                ),
                AccountDB(
                    account_id=stable_id("examplebank", "examplebank-checking"),
                    institution="examplebank",
                    account_number="examplebank-checking",
                    account_type="bank",
                    account_subtype="checking",
                    first_transaction_date=date(2025, 1, 5),
                    last_transaction_date=date(2025, 1, 5),
                    is_active=True,
                    transaction_count=1,
                    avg_monthly_transactions=Decimal("1.00"),
                    is_credit_card=False,
                    detected_credit_limit=None,
                    created_at=now,
                    updated_at=now,
                ),
                AccountDB(
                    account_id=stable_id("exampleissuer", "exampleissuer-gold"),
                    institution="exampleissuer",
                    account_number="exampleissuer-gold",
                    account_type="credit_card",
                    account_subtype="gold",
                    first_transaction_date=date(2025, 2, 14),
                    last_transaction_date=date(2025, 2, 14),
                    is_active=True,
                    transaction_count=1,
                    avg_monthly_transactions=Decimal("1.00"),
                    is_credit_card=True,
                    detected_credit_limit=None,
                    created_at=now,
                    updated_at=now,
                ),
            ]
        )

    return db_path


def test_query_service_init(populated_db):
    """Test query service initialization."""
    service = LedgerQueryService(populated_db)
    assert service.db_path == populated_db


def test_query_service_init_missing_db(tmp_path):
    """Test that query service raises error for missing database."""
    nonexistent = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError, match="Database not found"):
        LedgerQueryService(nonexistent)


def test_get_transactions_all(populated_db):
    """Test getting all transactions."""
    service = LedgerQueryService(populated_db)
    transactions = service.get_transactions()

    assert len(transactions) == 5
    assert all(isinstance(t, Transaction) for t in transactions)
    # Default ordering is date_desc (newest first)
    assert transactions[0].posting_date >= transactions[-1].posting_date


def test_get_transactions_by_date_range(populated_db):
    """Test filtering transactions by date range."""
    service = LedgerQueryService(populated_db)

    # January only
    jan_txns = service.get_transactions(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 31),
    )
    assert len(jan_txns) == 3
    assert all(t.posting_date.month == 1 for t in jan_txns)

    # February only
    feb_txns = service.get_transactions(
        start_date=date(2025, 2, 1),
        end_date=date(2025, 2, 28),
    )
    assert len(feb_txns) == 2
    assert all(t.posting_date.month == 2 for t in feb_txns)


def test_get_transactions_by_category(populated_db):
    """Test filtering by category."""
    service = LedgerQueryService(populated_db)

    dining_txns = service.get_transactions(categories=["Dining"])
    assert len(dining_txns) == 2
    assert all(t.category == "Dining" for t in dining_txns)

    grocery_txns = service.get_transactions(categories=["Groceries"])
    assert len(grocery_txns) == 1
    assert grocery_txns[0].merchant == "WHOLE FOODS"


def test_get_transactions_by_merchant(populated_db):
    """Test filtering by merchant."""
    service = LedgerQueryService(populated_db)

    mcdonalds = service.get_transactions(merchants=["MCDONALDS"])
    assert len(mcdonalds) == 1
    assert mcdonalds[0].merchant == "MCDONALDS"

    starbucks = service.get_transactions(merchants=["STARBUCKS"])
    assert len(starbucks) == 1
    assert starbucks[0].merchant == "STARBUCKS"


def test_get_transactions_by_institution(populated_db):
    """Test filtering by institution."""
    service = LedgerQueryService(populated_db)

    examplebank_txns = service.get_transactions(institutions=["examplebank"])
    assert len(examplebank_txns) == 4
    assert all(t.institution == "examplebank" for t in examplebank_txns)

    exampleissuer_txns = service.get_transactions(institutions=["exampleissuer"])
    assert len(exampleissuer_txns) == 1
    assert exampleissuer_txns[0].institution == "exampleissuer"


def test_get_transactions_by_account(populated_db):
    """Test filtering by account."""
    service = LedgerQueryService(populated_db)

    sapphire = service.get_transactions(accounts=["examplebank-sapphire"])
    assert len(sapphire) == 3
    assert all(t.account == "examplebank-sapphire" for t in sapphire)


def test_get_transactions_by_amount_range(populated_db):
    """Test filtering by amount range."""
    service = LedgerQueryService(populated_db)

    # Transactions between $50 and $150 (absolute value)
    mid_range = service.get_transactions(
        min_amount=Decimal("50"),
        max_amount=Decimal("150"),
    )
    assert len(mid_range) == 2  # $68.25 and $120.75

    # Transactions over $1000
    large = service.get_transactions(min_amount=Decimal("1000"))
    assert len(large) == 2  # $5000 income and $1500 shopping


def test_get_transactions_description_search(populated_db):
    """Test full-text search on descriptions."""
    service = LedgerQueryService(populated_db)

    # Search for "PAYROLL"
    payroll = service.get_transactions(description_search="PAYROLL")
    assert len(payroll) == 1
    assert "PAYROLL" in payroll[0].description

    # Search for "STORE" (should match Starbucks)
    store = service.get_transactions(description_search="STORE")
    assert len(store) == 1
    assert "STARBUCKS" in store[0].description


def test_get_transactions_cashflow_filter(populated_db):
    """Test filtering by cashflow inclusion."""
    service = LedgerQueryService(populated_db)

    # All test transactions have include_in_cashflow=True
    cashflow_txns = service.get_transactions(include_in_cashflow=True)
    assert len(cashflow_txns) == 5


def test_get_transactions_ordering(populated_db):
    """Test different ordering options."""
    service = LedgerQueryService(populated_db)

    # Date descending (default)
    desc = service.get_transactions(order_by="date_desc")
    assert desc[0].posting_date >= desc[-1].posting_date

    # Date ascending
    asc = service.get_transactions(order_by="date_asc")
    assert asc[0].posting_date <= asc[-1].posting_date

    # Amount descending (largest first)
    amt_desc = service.get_transactions(order_by="amount_desc")
    assert abs(amt_desc[0].amount_signed) >= abs(amt_desc[-1].amount_signed)

    # Amount ascending (smallest first)
    amt_asc = service.get_transactions(order_by="amount_asc")
    assert abs(amt_asc[0].amount_signed) <= abs(amt_asc[-1].amount_signed)


def test_get_transactions_pagination(populated_db):
    """Test limit and offset for pagination."""
    service = LedgerQueryService(populated_db)

    # First 2 transactions
    page1 = service.get_transactions(limit=2, offset=0, order_by="date_desc")
    assert len(page1) == 2

    # Next 2 transactions
    page2 = service.get_transactions(limit=2, offset=2, order_by="date_desc")
    assert len(page2) == 2

    # Should be different transactions
    assert page1[0].txn_id != page2[0].txn_id


def test_get_spending_by_category(populated_db):
    """Test spending aggregation by category."""
    service = LedgerQueryService(populated_db)

    # January spending
    jan_spending = service.get_spending_by_category(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 31),
    )

    assert "Dining" in jan_spending
    assert "Groceries" in jan_spending
    assert "Income" not in jan_spending  # Income is not expense

    # Verify amounts
    assert jan_spending["Dining"] == Decimal("45.50")
    assert jan_spending["Groceries"] == Decimal("120.75")

    # February spending (excluding excluded transactions)
    feb_spending = service.get_spending_by_category(
        start_date=date(2025, 2, 1),
        end_date=date(2025, 2, 28),
        exclude_from_spending=True,
    )

    assert "Dining" in feb_spending
    assert "Shopping" not in feb_spending  # Excluded from spending
    assert feb_spending["Dining"] == Decimal("68.25")


def test_get_spending_by_merchant(populated_db):
    """Test spending aggregation by merchant."""
    service = LedgerQueryService(populated_db)

    # All time spending
    merchant_spending = service.get_spending_by_merchant(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 2, 28),
        top_n=10,
    )

    # Should only include expense transactions with merchants, excluding any
    # merchant flagged exclude_from_spending (AMAZON), consistent with category spend
    assert "AMAZON" not in merchant_spending  # exclude_from_spending=True
    assert "MCDONALDS" in merchant_spending
    assert "STARBUCKS" in merchant_spending
    assert "WHOLE FOODS" in merchant_spending

    # Verify amounts
    assert merchant_spending["WHOLE FOODS"] == Decimal("120.75")


def test_get_monthly_cashflow(populated_db):
    """Test monthly cashflow aggregation."""
    service = LedgerQueryService(populated_db)

    cashflow = service.get_monthly_cashflow(year=2025)

    assert len(cashflow) == 2  # Jan and Feb

    # January
    jan = next(m for m in cashflow if m["month"] == "2025-01")
    assert jan["income"] == Decimal("5000.00")
    assert jan["expense"] == Decimal("-166.25")  # -45.50 + -120.75
    assert jan["net"] == Decimal("4833.75")

    # February. The AMAZON -1500.00 row is exclude_from_spending=True, so it must
    # NOT count toward the month's expense or net -- consistent with the spend
    # universe used by get_spending_by_category (see test_get_spending_by_category,
    # where this same row is excluded). Only the -68.25 Dining row remains.
    feb = next(m for m in cashflow if m["month"] == "2025-02")
    assert feb["income"] == Decimal("0")
    assert feb["expense"] == Decimal("-68.25")
    assert feb["net"] == Decimal("-68.25")


def test_get_categories(populated_db):
    """Test getting list of all categories."""
    service = LedgerQueryService(populated_db)

    categories = service.get_categories()

    assert len(categories) == 4
    assert "Dining" in categories
    assert "Groceries" in categories
    assert "Income" in categories
    assert "Shopping" in categories


def test_get_merchants(populated_db):
    """Test getting list of all merchants."""
    service = LedgerQueryService(populated_db)

    merchants = service.get_merchants()

    # Should exclude empty merchant names
    assert len(merchants) == 4
    assert "MCDONALDS" in merchants
    assert "STARBUCKS" in merchants
    assert "WHOLE FOODS" in merchants
    assert "AMAZON" in merchants


def test_get_institutions(populated_db):
    """Test getting list of all institutions."""
    service = LedgerQueryService(populated_db)

    institutions = service.get_institutions()

    assert len(institutions) == 2
    assert "examplebank" in institutions
    assert "exampleissuer" in institutions


def test_get_accounts(populated_db):
    """Test getting account metadata."""
    service = LedgerQueryService(populated_db)

    accounts = service.get_accounts()

    assert len(accounts) == 3
    account_numbers = {account["account_number"] for account in accounts}
    assert "examplebank-sapphire" in account_numbers
    assert "examplebank-checking" in account_numbers
    assert "exampleissuer-gold" in account_numbers
    assert all(account["is_active"] is True for account in accounts)


def test_get_date_range(populated_db):
    """Test getting earliest and latest transaction dates."""
    service = LedgerQueryService(populated_db)

    earliest, latest = service.get_date_range()

    assert earliest == date(2025, 1, 5)
    assert latest == date(2025, 2, 14)


def test_search_descriptions(populated_db):
    """Test full-text search convenience method."""
    service = LedgerQueryService(populated_db)

    results = service.search_descriptions("AMAZON")

    assert len(results) == 1
    assert results[0].merchant == "AMAZON"


def test_get_stats(populated_db):
    """Test database statistics."""
    service = LedgerQueryService(populated_db)

    stats = service.get_stats()

    assert stats["total_transactions"] == 5
    assert stats["total_transfers"] == 0
    assert stats["earliest_date"] == date(2025, 1, 5)
    assert stats["latest_date"] == date(2025, 2, 14)


def test_complex_query_combination(populated_db):
    """Test combining multiple filters."""
    service = LedgerQueryService(populated_db)

    # Find Examplebank transactions in January under $100
    results = service.get_transactions(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 31),
        institutions=["examplebank"],
        max_amount=Decimal("100"),
        order_by="amount_desc",
    )

    assert len(results) == 1  # Only MCDONALDS $45.50
    assert results[0].merchant == "MCDONALDS"
    assert results[0].institution == "examplebank"
    assert abs(results[0].amount_signed) < Decimal("100")


def test_category_spending_trend_excludes_flagged(populated_db):
    """get_category_spending_trend must skip exclude_from_spending transactions."""
    service = LedgerQueryService(populated_db)

    # The only Shopping transaction (AMAZON) is exclude_from_spending=True
    trend = service.get_category_spending_trend("Shopping", date(2025, 1, 1), date(2025, 2, 28))

    assert trend == []


def test_get_latest_import_run(populated_db):
    """The overview page's import summary is served by the query layer."""
    run = LedgerQueryService(populated_db).get_latest_import_run()

    assert run is not None
    assert run["files_processed"] == 5
    assert run["transactions_imported"] == 5
    assert "run_at" in run


# ---------------------------------------------------------------------------
# Task-4 regression tests: exclude_from_spending / include_in_cashflow
# consistency across spend aggregates
# ---------------------------------------------------------------------------


def test_get_spending_by_merchant_excludes_include_in_cashflow_false(tmp_path):
    """get_spending_by_merchant must respect include_in_cashflow==True.

    Insert one normal merchant and one merchant whose transaction has
    include_in_cashflow=False.  Only the normal merchant must appear.
    """
    db_path = tmp_path / "test_merchant_cashflow.db"
    from ledger.storage.database import init_database

    init_database(db_path)

    normal_txn = Transaction(
        txn_id="m_normal",
        txn_fingerprint="fp_m_normal",
        posting_date=date(2025, 3, 10),
        transaction_date=date(2025, 3, 10),
        institution="examplebank",
        account="examplebank-sapphire",
        account_type="credit_card",
        flow="expense",
        cashflow_type="expense",
        include_in_cashflow=True,
        currency="CAD",
        amount_raw=Decimal("50.00"),
        amount_signed=Decimal("-50.00"),
        description="NORMAL STORE",
        merchant="NormalStore",
        category="Shopping",
        merchant_confidence="high",
        category_confidence="high",
        exclude_from_spending=False,
        merchant_raw="NORMAL STORE",
        category_raw="",
        source_file="test.csv",
        source_path="/test/test.csv",
        source_period="2025-03",
        source_row=1,
        imported_at=date(2025, 3, 15),
        parser_version="test",
    )

    # This transaction has include_in_cashflow=False — should be excluded
    excluded_cashflow_txn = Transaction(
        txn_id="m_excluded_cashflow",
        txn_fingerprint="fp_m_excluded_cashflow",
        posting_date=date(2025, 3, 12),
        transaction_date=date(2025, 3, 12),
        institution="examplebank",
        account="examplebank-sapphire",
        account_type="credit_card",
        flow="expense",
        cashflow_type="expense",
        include_in_cashflow=False,  # <-- the gap under test
        currency="CAD",
        amount_raw=Decimal("200.00"),
        amount_signed=Decimal("-200.00"),
        description="HIDDEN STORE",
        merchant="HiddenStore",
        category="Shopping",
        merchant_confidence="high",
        category_confidence="high",
        exclude_from_spending=False,
        merchant_raw="HIDDEN STORE",
        category_raw="",
        source_file="test.csv",
        source_path="/test/test.csv",
        source_period="2025-03",
        source_row=2,
        imported_at=date(2025, 3, 15),
        parser_version="test",
    )

    result = ProcessingResult(
        transactions=[normal_txn, excluded_cashflow_txn],
        transfers=[],
        exceptions=[],
        total_scanned=2,
        duplicates_removed=0,
        collisions=0,
    )
    writer = DatabaseWriter(db_path)
    writer.write_result(result, Path("/test"), "test_version")

    service = LedgerQueryService(db_path)
    merchant_spending = service.get_spending_by_merchant(
        start_date=date(2025, 3, 1),
        end_date=date(2025, 3, 31),
    )

    assert "NormalStore" in merchant_spending
    assert merchant_spending["NormalStore"] == Decimal("50.00")
    assert "HiddenStore" not in merchant_spending, (
        "get_spending_by_merchant must omit merchants with include_in_cashflow=False"
    )


# ---------------------------------------------------------------------------
# Spend-universe correctness tests (pre-fix these FAIL)
# ---------------------------------------------------------------------------


def _make_txn(
    i: int,
    *,
    amount: Decimal,
    category: str = "Dining",
    merchant: str = "DINER",
    cashflow_type: str = "expense",
    include_in_cashflow: bool = True,
    exclude_from_spending: bool = False,
    currency: str = "CAD",
    account: str = "examplebank-sapphire",
    posting_date: date = date(2025, 4, 10),
) -> Transaction:
    """Factory for spend-universe test transactions."""
    return Transaction(
        txn_id=f"su_txn_{i}",
        txn_fingerprint=f"su_fp_{i}",
        posting_date=posting_date,
        transaction_date=posting_date,
        institution="examplebank",
        account=account,
        flow="expense",
        cashflow_type=cashflow_type,
        include_in_cashflow=include_in_cashflow,
        currency=currency,
        amount_raw=abs(amount),
        amount_signed=amount,
        description=f"SPEND UNIVERSE TEST {i}",
        merchant=merchant,
        category=category,
        exclude_from_spending=exclude_from_spending,
        merchant_raw="",
        category_raw="",
        source_file="test.csv",
        source_path="/test/test.csv",
        source_period="2025-04",
        source_row=i,
        imported_at=date(2025, 4, 15),
        parser_version="test",
    )


def _write_txns(db_path, txns: list[Transaction]) -> None:
    result = ProcessingResult(
        transactions=txns,
        transfers=[],
        exceptions=[],
        total_scanned=len(txns),
        duplicates_removed=0,
        collisions=0,
    )
    DatabaseWriter(db_path).write_result(result, Path("/test"), "test")


@pytest.fixture
def high_volume_finance_policy_db(tmp_path):
    """A capped-account reconciliation fixture with policy edge cases."""
    db_path = tmp_path / "high_volume_finance_policy.db"
    init_database(db_path)
    primary = "primary-chequing"
    txns = [_make_txn(i, amount=Decimal("-1.00"), account=primary) for i in range(1, 5002)]
    txns.extend(
        [
            _make_txn(
                5002,
                amount=Decimal("100.00"),
                cashflow_type="income",
                account=primary,
            ),
            _make_txn(5003, amount=Decimal("-200.00"), currency="USD", account=primary),
            _make_txn(
                5004,
                amount=Decimal("-300.00"),
                exclude_from_spending=True,
                account=primary,
            ),
            _make_txn(
                5005,
                amount=Decimal("-400.00"),
                include_in_cashflow=False,
                account=primary,
            ),
            _make_txn(5006, amount=Decimal("-900.00"), account="other-chequing"),
        ]
    )
    _write_txns(db_path, txns)
    return db_path, primary


def test_compute_kpis_reconciles_uncapped_account_activity(high_volume_finance_policy_db):
    db_path, account = high_volume_finance_policy_db
    service = LedgerQueryService(db_path)

    assert "accounts" in signature(service.compute_kpis).parameters

    kpis = service.compute_kpis(
        date(2025, 4, 1),
        date(2025, 4, 30),
        accounts=[account],
    )
    capped_rows = service.get_transactions(
        start_date=date(2025, 4, 1),
        end_date=date(2025, 4, 30),
        accounts=[account],
        limit=5000,
    )
    capped_expenses = sum(
        (abs(txn.amount_signed) for txn in capped_rows if txn.amount_signed < 0),
        Decimal("0.00"),
    )

    assert len(capped_rows) == 5000
    assert kpis["activity_count"] == 5005
    assert kpis["income"] == Decimal("100.00")
    assert kpis["expenses"] == Decimal("5001.00")
    assert kpis["net"] == Decimal("-4901.00")
    assert capped_expenses != kpis["expenses"]


def test_finance_policy_publishes_sql_and_in_memory_predicates():
    spec = find_spec("ledger.finance_policy")
    assert spec is not None
    policy = import_module("ledger.finance_policy")

    for name in (
        "cad_sql_filters",
        "income_sql_filters",
        "expense_sql_filters",
        "spend_sql_filters",
        "is_cad_row",
        "is_income_row",
        "is_expense_row",
        "is_spend_row",
    ):
        assert callable(getattr(policy, name, None)), name

    common = {
        "amount_signed": Decimal("-1.00"),
        "cashflow_type": "expense",
        "include_in_cashflow": True,
        "currency": "CAD",
    }
    assert policy.is_cad_row(currency=None)
    assert policy.is_cad_row(currency="")
    assert not policy.is_cad_row(currency="USD")
    assert policy.is_expense_row(**common)
    assert policy.is_spend_row(**common, exclude_from_spending=False)
    assert not policy.is_spend_row(**common, exclude_from_spending=True)
    assert policy.is_income_row(
        amount_signed=Decimal("1.00"),
        cashflow_type="income",
        include_in_cashflow=True,
        currency="CAD",
    )
    assert not policy.is_income_row(
        amount_signed=Decimal("1.00"),
        cashflow_type="income",
        include_in_cashflow=False,
        currency="CAD",
    )


def test_aggregate_spend_predicate_preserves_historical_call_shape() -> None:
    from ledger.query.aggregates import is_spend_row

    historical = {
        "cashflow_type": "expense",
        "include_in_cashflow": True,
        "exclude_from_spending": False,
        "currency": "CAD",
    }
    assert is_spend_row(**historical)
    assert not is_spend_row(**historical, amount_signed=Decimal("1.00"))


def test_finance_policy_sql_and_in_memory_predicates_enforce_strict_sign_parity(tmp_path):
    """Malformed persisted signs must never enter canonical financial totals."""
    policy = import_module("ledger.finance_policy")
    for predicate_name in ("is_income_row", "is_expense_row", "is_spend_row"):
        predicate = getattr(policy, predicate_name)
        assert "amount_signed" in signature(predicate).parameters

    db_path = tmp_path / "finance_policy_sign_parity.db"
    init_database(db_path)
    cases = [
        ("income-positive-cad", Decimal("10.00"), "income", True, False, "CAD"),
        ("income-negative-cad", Decimal("-10.00"), "income", True, False, "CAD"),
        ("income-zero-cad", Decimal("0.00"), "income", True, False, "CAD"),
        ("income-positive-legacy", Decimal("10.00"), "income", True, False, ""),
        ("income-positive-foreign", Decimal("10.00"), "income", True, False, "USD"),
        ("expense-negative-cad", Decimal("-10.00"), "expense", True, False, "CAD"),
        ("expense-positive-cad", Decimal("10.00"), "expense", True, False, "CAD"),
        ("expense-zero-cad", Decimal("0.00"), "expense", True, False, "CAD"),
        ("expense-negative-excluded", Decimal("-10.00"), "expense", True, True, "CAD"),
        ("expense-negative-legacy", Decimal("-10.00"), "expense", True, False, ""),
        ("expense-negative-foreign", Decimal("-10.00"), "expense", True, False, "USD"),
        ("expense-negative-not-cashflow", Decimal("-10.00"), "expense", False, False, "CAD"),
        ("transfer-negative-cad", Decimal("-10.00"), "transfer", True, False, "CAD"),
    ]
    with session_scope(db_path) as session:
        session.add_all(
            [
                TransactionDB(
                    txn_id=txn_id,
                    txn_fingerprint=f"fp-{index}",
                    posting_date=date(2025, 4, 10),
                    transaction_date=date(2025, 4, 10),
                    institution="test",
                    account="test-account",
                    account_type="chequing",
                    flow=cashflow_type,
                    cashflow_type=cashflow_type,
                    include_in_cashflow=include_in_cashflow,
                    currency=currency,
                    amount_raw=abs(amount_signed),
                    amount_signed=amount_signed,
                    description="SYNTHETIC POLICY ROW",
                    merchant="TEST",
                    category="Test",
                    merchant_confidence="none",
                    category_confidence="low",
                    exclude_from_spending=exclude_from_spending,
                    merchant_raw="",
                    category_raw="",
                    source_file="synthetic.csv",
                    source_path="/synthetic/synthetic.csv",
                    source_period="2025-04",
                    source_row=index,
                    imported_at=datetime(2025, 4, 15, tzinfo=UTC),
                    parser_version="test",
                    day_of_week=3,
                    day_of_month=10,
                    week_of_year=15,
                    is_weekend=False,
                    is_month_start=False,
                    is_month_end=False,
                )
                for index, (
                    txn_id,
                    amount_signed,
                    cashflow_type,
                    include_in_cashflow,
                    exclude_from_spending,
                    currency,
                ) in enumerate(cases, start=1)
            ]
        )

    expected_income = {"income-positive-cad", "income-positive-legacy"}
    expected_expense = {
        "expense-negative-cad",
        "expense-negative-excluded",
        "expense-negative-legacy",
    }
    expected_spend = {"expense-negative-cad", "expense-negative-legacy"}
    with session_scope(db_path) as session:
        rows = session.query(TransactionDB).all()
        python_income = {
            row.txn_id
            for row in rows
            if policy.is_income_row(
                amount_signed=row.amount_signed,
                cashflow_type=row.cashflow_type,
                include_in_cashflow=row.include_in_cashflow,
                currency=row.currency,
            )
        }
        python_expense = {
            row.txn_id
            for row in rows
            if policy.is_expense_row(
                amount_signed=row.amount_signed,
                cashflow_type=row.cashflow_type,
                include_in_cashflow=row.include_in_cashflow,
                currency=row.currency,
            )
        }
        python_spend = {
            row.txn_id
            for row in rows
            if policy.is_spend_row(
                amount_signed=row.amount_signed,
                cashflow_type=row.cashflow_type,
                include_in_cashflow=row.include_in_cashflow,
                exclude_from_spending=row.exclude_from_spending,
                currency=row.currency,
            )
        }
        sql_income = {
            row.txn_id
            for row in session.query(TransactionDB).filter(*policy.income_sql_filters()).all()
        }
        sql_expense = {
            row.txn_id
            for row in session.query(TransactionDB).filter(*policy.expense_sql_filters()).all()
        }
        sql_spend = {
            row.txn_id
            for row in session.query(TransactionDB).filter(*policy.spend_sql_filters()).all()
        }

    assert python_income == sql_income == expected_income
    assert python_expense == sql_expense == expected_expense
    assert python_spend == sql_spend == expected_spend


def test_get_category_spending_trend_uses_full_spend_universe(tmp_path):
    """get_category_spending_trend must exclude non-cashflow and non-expense transactions.

    Pre-fix this method only filters exclude_from_spending; it lacks cashflow_type==expense
    and include_in_cashflow==True, so transactions that don't belong in the spend universe
    can inflate the trend figures.
    """
    from ledger.storage.database import init_database

    db_path = tmp_path / "trend_universe.db"
    init_database(db_path)

    normal_txn = _make_txn(1, amount=Decimal("-80.00"), category="Groceries", merchant="FOODMART")
    # include_in_cashflow=False — must be excluded from trend
    non_cashflow_txn = _make_txn(
        2,
        amount=Decimal("-300.00"),
        category="Groceries",
        merchant="FOODMART",
        include_in_cashflow=False,
    )

    _write_txns(db_path, [normal_txn, non_cashflow_txn])

    service = LedgerQueryService(db_path)
    trend = service.get_category_spending_trend(
        "Groceries",
        date(2025, 4, 1),
        date(2025, 4, 30),
        frequency="monthly",
    )

    assert len(trend) == 1, f"Expected one monthly bucket, got {len(trend)}"
    period_amount = trend[0]["amount"]
    assert period_amount == Decimal("80.00"), (
        f"Expected $80.00 (normal txn only, excluding $300 non-cashflow), got {period_amount}"
    )


def test_spend_universe_helper_regression_get_spending_by_category(tmp_path):
    """After consolidation, get_spending_by_category must return identical results.

    This is a regression guard: switching to the shared helper must not change
    totals for the already-correct method.
    """
    from ledger.storage.database import init_database

    db_path = tmp_path / "regression_cat.db"
    init_database(db_path)

    txns = [
        _make_txn(1, amount=Decimal("-50.00"), category="Dining"),
        _make_txn(2, amount=Decimal("-30.00"), category="Dining"),
        _make_txn(3, amount=Decimal("-200.00"), category="Shopping", exclude_from_spending=True),
        _make_txn(4, amount=Decimal("-100.00"), category="Dining", include_in_cashflow=False),
    ]
    _write_txns(db_path, txns)

    service = LedgerQueryService(db_path)
    spending = service.get_spending_by_category(date(2025, 4, 1), date(2025, 4, 30))

    # Only the two normal Dining txns ($50 + $30) should appear
    assert spending.get("Dining") == Decimal("80.00")
    assert "Shopping" not in spending
    # The include_in_cashflow=False Dining txn must also be excluded
    # (get_spending_by_category already filters include_in_cashflow, so this is a regression guard)


def test_spend_universe_helper_regression_get_spending_by_merchant(tmp_path):
    """After consolidation, get_spending_by_merchant must return identical results."""
    from ledger.storage.database import init_database

    db_path = tmp_path / "regression_merch.db"
    init_database(db_path)

    txns = [
        _make_txn(1, amount=Decimal("-40.00"), merchant="SHOPRITE"),
        _make_txn(2, amount=Decimal("-60.00"), merchant="SHOPRITE"),
        _make_txn(3, amount=Decimal("-500.00"), merchant="BIGSTORE", exclude_from_spending=True),
    ]
    _write_txns(db_path, txns)

    service = LedgerQueryService(db_path)
    spending = service.get_spending_by_merchant(date(2025, 4, 1), date(2025, 4, 30))

    assert spending.get("SHOPRITE") == Decimal("100.00")
    assert "BIGSTORE" not in spending


# ---------------------------------------------------------------------------
# get_merchant_spending_trend — TDD tests (written before implementation)
# ---------------------------------------------------------------------------


def test_get_merchant_spending_trend_returns_period_amount_series(tmp_path):
    """get_merchant_spending_trend returns period/amount/transaction_count rows."""
    from ledger.storage.database import init_database

    db_path = tmp_path / "merchant_trend.db"
    init_database(db_path)

    txns = [
        _make_txn(
            1, amount=Decimal("-40.00"), merchant="STARBUCKS", posting_date=date(2025, 1, 15)
        ),
        _make_txn(
            2, amount=Decimal("-60.00"), merchant="STARBUCKS", posting_date=date(2025, 1, 28)
        ),
        _make_txn(
            3, amount=Decimal("-90.00"), merchant="STARBUCKS", posting_date=date(2025, 2, 10)
        ),
        # Different merchant — must not appear
        _make_txn(4, amount=Decimal("-200.00"), merchant="AMAZON", posting_date=date(2025, 1, 20)),
    ]
    _write_txns(db_path, txns)

    service = LedgerQueryService(db_path)
    trend = service.get_merchant_spending_trend("STARBUCKS", date(2025, 1, 1), date(2025, 2, 28))

    assert len(trend) == 2, f"Expected 2 monthly buckets, got {len(trend)}"
    assert trend[0]["period"] == "2025-01"
    assert trend[0]["amount"] == Decimal("100.00"), f"Jan: {trend[0]['amount']}"
    assert trend[0]["transaction_count"] == 2
    assert trend[1]["period"] == "2025-02"
    assert trend[1]["amount"] == Decimal("90.00"), f"Feb: {trend[1]['amount']}"
    assert trend[1]["transaction_count"] == 1


def test_get_merchant_spending_trend_uses_full_spend_universe(tmp_path):
    """get_merchant_spending_trend must exclude non-cashflow and exclude_from_spending txns."""
    from ledger.storage.database import init_database

    db_path = tmp_path / "merchant_trend_universe.db"
    init_database(db_path)

    normal_txn = _make_txn(1, amount=Decimal("-80.00"), merchant="COSTCO")
    # include_in_cashflow=False — must be excluded
    non_cashflow_txn = _make_txn(
        2,
        amount=Decimal("-300.00"),
        merchant="COSTCO",
        include_in_cashflow=False,
    )
    # exclude_from_spending=True — must be excluded
    excluded_txn = _make_txn(
        3,
        amount=Decimal("-500.00"),
        merchant="COSTCO",
        exclude_from_spending=True,
    )
    _write_txns(db_path, [normal_txn, non_cashflow_txn, excluded_txn])

    service = LedgerQueryService(db_path)
    trend = service.get_merchant_spending_trend("COSTCO", date(2025, 4, 1), date(2025, 4, 30))

    assert len(trend) == 1, f"Expected 1 bucket, got {len(trend)}"
    assert trend[0]["amount"] == Decimal("80.00"), (
        f"Expected $80.00 (normal txn only), got {trend[0]['amount']}"
    )


def test_get_merchant_spending_trend_returns_decimal_amounts(tmp_path):
    """Amounts returned by get_merchant_spending_trend must be Decimal, not float."""
    from ledger.storage.database import init_database

    db_path = tmp_path / "merchant_trend_decimal.db"
    init_database(db_path)

    _write_txns(db_path, [_make_txn(1, amount=Decimal("-55.50"), merchant="TARGET")])

    service = LedgerQueryService(db_path)
    trend = service.get_merchant_spending_trend("TARGET", date(2025, 4, 1), date(2025, 4, 30))

    assert len(trend) == 1
    assert isinstance(trend[0]["amount"], Decimal), (
        f"Expected Decimal, got {type(trend[0]['amount'])}"
    )
    assert trend[0]["amount"] == Decimal("55.50")


def test_get_merchant_spending_trend_facade_resolves(tmp_path):
    """LedgerQueryService must expose get_merchant_spending_trend directly.

    This is a regression guard for the crash class: if the method is absent from
    the aggregate query module, the Categories Merchant view crashes with an
    AttributeError. The test must FAIL before the method is implemented.
    """
    from ledger.storage.database import init_database

    db_path = tmp_path / "merchant_trend_facade.db"
    init_database(db_path)

    service = LedgerQueryService(db_path)
    # Check the direct facade surface without needing transaction data.
    assert hasattr(service, "get_merchant_spending_trend"), (
        "LedgerQueryService has no attribute 'get_merchant_spending_trend' — "
        "the Categories Merchant view will crash with AttributeError"
    )
    # Also call it to confirm the facade wires through correctly
    result = service.get_merchant_spending_trend("NOBODY", date(2025, 1, 1), date(2025, 1, 31))
    assert result == [], f"Expected empty list for unknown merchant, got {result}"
