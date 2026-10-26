"""
Integration tests for full processing pipeline.

Tests the complete flow: CSV files → Processing → Database → Query
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ledger.core import Transaction
from ledger.processing.pipeline import ProcessingResult, compute_summary, process
from ledger.query.service import LedgerQueryService
from ledger.storage.database import init_database
from ledger.storage.writer import DatabaseWriter


@pytest.fixture
def sample_csv_dir(tmp_path):
    """Create sample CSV files for testing."""
    csv_dir = tmp_path / "csvs"
    csv_dir.mkdir()

    # HARBOR CSV
    harbor_file = csv_dir / "harbor_2025-01.csv"
    harbor_file.write_text(
        "Ledger Format,Date,Description,Amount,Currency,Reference,Institution,Account Type\nledger-mapped-v1,2025-01-15,GROCERY STORE,-50.00,CAD,,harbor,Chequing\nledger-mapped-v1,2025-01-20,COFFEE SHOP,-5.50,CAD,,harbor,Chequing\nledger-mapped-v1,2025-01-25,SALARY DEPOSIT,2000.00,CAD,,harbor,Chequing\n"
    )
    return csv_dir


def test_full_pipeline_csv_to_query(tmp_path, sample_csv_dir, sample_app_config):
    """Test complete pipeline: CSV → Process → Database → Query."""
    # Step 1: Process CSV files
    result = process(sample_app_config, sample_csv_dir)
    assert result.total_scanned == 3
    assert len(result.transactions) == 3
    assert len(result.exceptions) == 0

    # Step 2: Write to database
    db_path = tmp_path / "test.db"
    init_database(db_path)
    writer = DatabaseWriter(db_path)
    writer.write_result(result, sample_csv_dir, "test-v1.0.0")

    # Step 3: Query from database
    service = LedgerQueryService(db_path)
    all_txns = service.get_transactions(limit=100)
    assert len(all_txns) == 3

    # Verify transaction details
    # Debug: Check what institutions we actually got
    institutions = [t.institution for t in all_txns]
    assert len(set(institutions)) == 1, f"Expected single institution, got: {set(institutions)}"
    # Canonical rows retain the explicitly supplied fictional institution.
    assert all(t.institution == institutions[0] for t in all_txns)

    # Test: Filter by category
    expenses = [t for t in all_txns if t.cashflow_type == "expense"]
    assert len(expenses) == 2

    income = [t for t in all_txns if t.cashflow_type == "income"]
    assert len(income) == 1


def test_deduplication_across_runs(tmp_path, sample_csv_dir, sample_app_config):
    """Test that re-importing same files deduplicates correctly."""
    result1 = process(sample_app_config, sample_csv_dir)
    db_path = tmp_path / "test.db"
    init_database(db_path)
    writer = DatabaseWriter(db_path)
    writer.write_result(result1, sample_csv_dir, "test-v1.0.0")

    # Second import (same files)
    result2 = process(sample_app_config, sample_csv_dir)
    writer.write_result(result2, sample_csv_dir, "test-v1.0.0")

    service = LedgerQueryService(db_path)
    all_txns = service.get_transactions(limit=100)
    assert len(all_txns) == 3  # Still 3, not 6 (deduplication works)


def test_query_service_aggregations(tmp_path, sample_csv_dir, sample_app_config):
    """Test query service aggregation functions."""
    # Process and load data
    result = process(sample_app_config, sample_csv_dir)
    db_path = tmp_path / "test.db"
    init_database(db_path)
    writer = DatabaseWriter(db_path)
    writer.write_result(result, sample_csv_dir, "test-v1.0.0")

    service = LedgerQueryService(db_path)

    # Test: Get spending by category
    spending = service.get_spending_by_category(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 31),
    )
    assert isinstance(spending, dict)
    assert all(isinstance(v, Decimal) for v in spending.values())

    # Test: Get date range
    earliest, latest = service.get_date_range()
    assert earliest == date(2025, 1, 15)
    assert latest == date(2025, 1, 25)

    # Test: Get stats
    stats = service.get_stats()
    assert stats["total_transactions"] == 3
    assert stats["earliest_date"] == date(2025, 1, 15)
    assert stats["latest_date"] == date(2025, 1, 25)


def test_compute_kpis(tmp_path, sample_csv_dir, sample_app_config):
    """Test KPI computation in SQL."""
    # Process and load data
    result = process(sample_app_config, sample_csv_dir)
    db_path = tmp_path / "test.db"
    init_database(db_path)
    writer = DatabaseWriter(db_path)
    writer.write_result(result, sample_csv_dir, "test-v1.0.0")

    service = LedgerQueryService(db_path)

    # Test: Compute KPIs
    kpis = service.compute_kpis(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 31),
    )

    # Verify structure
    assert "income" in kpis
    assert "expenses" in kpis
    assert "net" in kpis
    assert "transaction_count" in kpis

    # Verify calculations
    assert kpis["income"] == Decimal("2000.00")
    assert kpis["expenses"] == Decimal("55.50")  # 50 + 5.50
    assert kpis["net"] == Decimal("1944.50")  # 2000 - 55.50
    assert kpis["transaction_count"] == 3


def test_compute_summary_matches_canonical_sql_policy(tmp_path, sample_app_config):
    """Import summaries and SQL KPIs must classify the same money rows."""

    def txn(
        row: int,
        amount: str,
        *,
        cashflow_type: str,
        currency: str = "CAD",
        include_in_cashflow: bool = True,
        exclude_from_spending: bool = False,
    ) -> Transaction:
        posting_date = date(2025, 4, 10)
        return Transaction(
            txn_id=f"summary-policy-{row}",
            txn_fingerprint=f"summary-policy-fp-{row}",
            posting_date=posting_date,
            transaction_date=posting_date,
            institution="test-bank",
            account="test-account",
            flow=cashflow_type,
            cashflow_type=cashflow_type,
            include_in_cashflow=include_in_cashflow,
            currency=currency,
            amount_raw=abs(Decimal(amount)),
            amount_signed=Decimal(amount),
            description=f"SUMMARY POLICY {row}",
            category="Dining" if cashflow_type == "expense" else "Income",
            exclude_from_spending=exclude_from_spending,
            source_file="policy.csv",
            source_path="policy.csv",
            source_row=row,
            imported_at=posting_date,
            parser_version="test",
        )

    transactions = [
        txn(1, "-10.00", cashflow_type="expense"),
        txn(2, "100.00", cashflow_type="income"),
        txn(3, "-20.00", cashflow_type="expense", currency="USD"),
        txn(4, "-30.00", cashflow_type="expense", exclude_from_spending=True),
        txn(5, "-40.00", cashflow_type="expense", include_in_cashflow=False),
    ]
    result = ProcessingResult(
        transactions=transactions,
        total_scanned=len(transactions),
    )
    summary = compute_summary(
        result,
        sample_app_config,
        tmp_path,
        filters={},
        config_versions={},
        parser_version="test",
    )
    db_path = tmp_path / "summary-policy.db"
    init_database(db_path)
    DatabaseWriter(db_path).write_result(result, Path("/test"), "test")
    kpis = LedgerQueryService(db_path).compute_kpis(
        date(2025, 4, 1),
        date(2025, 4, 30),
    )

    assert summary.cashflow_income == kpis["income"] == Decimal("100.00")
    assert summary.cashflow_expense == -kpis["expenses"] == Decimal("-10.00")
    assert summary.cashflow_net == kpis["net"] == Decimal("90.00")
    assert summary.by_category == {"Dining": Decimal("10.00")}
