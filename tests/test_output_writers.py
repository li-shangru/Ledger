"""CSV-boundary safety tests for local, human-facing exports."""

from __future__ import annotations

import csv
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from ledger.core import ExceptionRecord, Summary, Transaction
from ledger.output.writers import write_all, write_exceptions_csv
from ledger.utils import text as text_utils


def _transaction(txn_id: str) -> Transaction:
    return Transaction(
        txn_id=txn_id,
        txn_fingerprint=f"fingerprint-{txn_id}",
        posting_date=date(2026, 1, 15),
        transaction_date=date(2026, 1, 14),
        institution="test-bank",
        account="test-account",
        account_type="Chequing",
        flow="expense",
        cashflow_type="expense",
        include_in_cashflow=True,
        currency="CAD",
        amount_raw=Decimal("50.00"),
        amount_signed=Decimal("-50.00"),
        description="Test purchase",
        merchant="TEST MERCHANT",
        category="Other",
        source_file="statement.csv",
        source_path="2026-01/statement.csv",
        source_period="2026-01",
        source_row=2,
        imported_at=datetime(2026, 1, 16, 12, 0, 0),
        parser_version="test-parser",
    )


def _summary() -> Summary:
    return Summary(
        generated_at=datetime(2026, 1, 16, 12, 0, 0),
        parser_version="test-parser",
        config_versions={},
        source_path="synthetic",
        filters={},
        total_scanned=2,
        total_processed=2,
        duplicates_removed=0,
        collisions=0,
        exceptions=0,
        transactions_count=1,
        transfers_count=1,
        by_institution={"test-bank": 2},
        by_flow={"expense": 2},
        by_category={"Other": Decimal("100.00")},
        cashflow_income=Decimal("0.00"),
        cashflow_expense=Decimal("100.00"),
        cashflow_net=Decimal("-100.00"),
        by_quarter={"2026-Q1": {"expense": Decimal("100.00")}},
    )


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_neutralize_csv_row_defuses_every_prefix_and_preserves_signed_numbers() -> None:
    source = {
        "equals": "=1+1",
        "plus": "+cmd",
        "minus": "-cmd",
        "at": "@SUM(A1)",
        "tab": "\tcmd",
        "carriage_return": "\rcmd",
        "line_feed": "\ncmd",
        "negative_number": "-50.00",
        "positive_number": "+50.00",
        "decimal": Decimal("-50.00"),
    }

    safe = text_utils.neutralize_csv_row(source)

    for key in ("equals", "plus", "minus", "at", "tab", "carriage_return", "line_feed"):
        assert safe[key].startswith("'")
    assert safe["negative_number"] == "-50.00"
    assert safe["positive_number"] == "+50.00"
    assert safe["decimal"] == Decimal("-50.00")
    assert source["equals"] == "=1+1"


def test_write_all_neutralizes_every_string_cell_for_transactions_transfers_and_llm(
    tmp_path: Path,
) -> None:
    dangerous = {
        "institution": "=INSTITUTION",
        "account": "+ACCOUNT",
        "account_type": "-ACCOUNT-TYPE",
        "merchant": "@MERCHANT",
        "category": "\tCATEGORY",
        "source_file": "\rSOURCE.csv",
        "source_path": "\nSOURCE/PATH.csv",
    }
    transaction = _transaction("transaction").model_copy(update=dangerous)
    transfer = _transaction("transfer").model_copy(update=dangerous)

    outputs = write_all(
        tmp_path,
        [transaction],
        [transfer],
        [],
        _summary(),
        formats=["csv"],
    )

    for artifact in ("transactions_csv", "transfers_csv"):
        row = _read_rows(outputs[artifact])[0]
        for column in (
            "institution",
            "account",
            "merchant",
            "category",
            "source_file",
            "source_path",
        ):
            assert row[column].startswith("'")
        assert row["amount_signed"] == "-50.00"

    for row in _read_rows(outputs["llm_export_csv"]):
        for column in (
            "institution",
            "account",
            "account_type",
            "merchant",
            "category",
            "source_file",
        ):
            assert row[column].startswith("'")
        assert row["amount_signed"] == "-50.00"


def test_write_exceptions_csv_neutralizes_source_and_every_reason_prefix(tmp_path: Path) -> None:
    prefixes = ("=", "+", "-", "@", "\t", "\r", "\n")
    exceptions = [
        ExceptionRecord(
            source_file="=SOURCE.csv",
            source_path="+SOURCE/PATH.csv",
            source_row=index,
            reason=f"{prefix}reason",
            timestamp=datetime(2026, 1, 16, 12, 0, index),
        )
        for index, prefix in enumerate(prefixes, start=1)
    ]
    path = tmp_path / "exceptions.csv"

    write_exceptions_csv(path, exceptions)

    rows = _read_rows(path)
    assert len(rows) == len(prefixes)
    for row in rows:
        assert row["source_file"].startswith("'=")
        assert row["source_path"].startswith("'+")
        assert row["reason"].startswith("'")
