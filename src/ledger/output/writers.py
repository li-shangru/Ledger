"""
Output writers for various formats.

Supports:
- CSV: Universal compatibility
- Parquet: Best for data analysis (pandas, polars, DuckDB)
- JSON: Summary and metadata
"""

import csv
import json
from pathlib import Path
from typing import Any

from ledger.core import ExceptionRecord, Summary, Transaction
from ledger.utils.json_io import decimal_json
from ledger.utils.private_files import (
    ensure_private_dir,
    private_atomic_binary,
    private_atomic_text,
)
from ledger.utils.text import neutralize_csv_row

# === Serialization helpers ===


def _transaction_to_dict(txn: Transaction) -> dict[str, Any]:
    """Convert Transaction to dict for serialization."""
    return {
        "txn_id": txn.txn_id,
        "txn_fingerprint": txn.txn_fingerprint,
        "posting_date": txn.posting_date.isoformat() if txn.posting_date else "",
        "transaction_date": txn.transaction_date.isoformat() if txn.transaction_date else "",
        "institution": txn.institution,
        "account": txn.account,
        "flow": txn.flow,
        "cashflow_type": txn.cashflow_type,
        "include_in_cashflow": txn.include_in_cashflow,
        "currency": txn.currency,
        "amount_raw": str(txn.amount_raw),
        "amount_signed": str(txn.amount_signed),
        "description": txn.description,
        "merchant": txn.merchant,
        "category": txn.category,
        "merchant_confidence": txn.merchant_confidence,
        "category_confidence": txn.category_confidence,
        "exclude_from_spending": txn.exclude_from_spending,
        "merchant_raw": txn.merchant_raw,
        "category_raw": txn.category_raw,
        "source_file": txn.source_file,
        "source_path": txn.source_path,
        "source_period": txn.source_period,
        "source_row": txn.source_row,
        "imported_at": txn.imported_at.isoformat() if txn.imported_at else "",
        "parser_version": txn.parser_version,
    }


# CSV column order (full export)
CSV_COLUMNS = [
    "txn_id",
    "txn_fingerprint",
    "posting_date",
    "transaction_date",
    "institution",
    "account",
    "flow",
    "cashflow_type",
    "include_in_cashflow",
    "currency",
    "amount_raw",
    "amount_signed",
    "description",
    "merchant",
    "category",
    "merchant_confidence",
    "category_confidence",
    "exclude_from_spending",
    "merchant_raw",
    "category_raw",
    "source_file",
    "source_path",
    "source_period",
    "source_row",
    "imported_at",
    "parser_version",
]

# LLM-optimized columns (removes technical fields, adds ML features)
LLM_COLUMNS = [
    # Identity
    "txn_id",
    # Dates
    "posting_date",
    "transaction_date",
    # Source
    "institution",
    "account",
    "account_type",
    # Classification
    "flow",
    "cashflow_type",
    "category",
    "merchant",
    # Amounts
    "currency",
    "amount_signed",
    # Description
    "description",
    # Confidence & flags
    "merchant_confidence",
    "category_confidence",
    "exclude_from_spending",
    "include_in_cashflow",
    # Pattern linking
    "recurring_pattern_id",
    # Temporal features
    "day_of_week",
    "is_weekend",
    "is_month_start",
    "is_month_end",
    # Provenance (minimal)
    "source_file",
    "source_period",
]


# === CSV Writer ===


def write_csv(path: Path, transactions: list[Transaction]) -> None:
    """Write transactions to CSV file."""
    with private_atomic_text(path, newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()

        for txn in transactions:
            writer.writerow(neutralize_csv_row(_transaction_to_dict(txn)))


def write_exceptions_csv(path: Path, exceptions: list[ExceptionRecord]) -> None:
    """Write exceptions to CSV file."""
    fieldnames = ["source_file", "source_path", "source_row", "reason", "timestamp"]
    if not exceptions:
        # Write empty file with header
        with private_atomic_text(path, newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
        return

    with private_atomic_text(path, newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()

        for exc in exceptions:
            writer.writerow(
                neutralize_csv_row(
                    {
                        "source_file": exc.source_file,
                        "source_path": exc.source_path,
                        "source_row": exc.source_row,
                        "reason": exc.reason,
                        "timestamp": exc.timestamp.isoformat(),
                    }
                )
            )


def _transaction_to_llm_dict(txn: Transaction) -> dict[str, Any]:
    """
    Convert Transaction to LLM-optimized dict.

    Includes ML features and temporal patterns while excluding
    technical metadata like fingerprints and parser versions.
    """
    return {
        # Identity
        "txn_id": txn.txn_id,
        # Dates
        "posting_date": txn.posting_date.isoformat() if txn.posting_date else "",
        "transaction_date": txn.transaction_date.isoformat() if txn.transaction_date else "",
        # Source
        "institution": txn.institution,
        "account": txn.account,
        "account_type": txn.account_type,
        # Classification
        "flow": txn.flow,
        "cashflow_type": txn.cashflow_type,
        "category": txn.category,
        "merchant": txn.merchant,
        # Amounts
        "currency": txn.currency,
        "amount_signed": str(txn.amount_signed),
        # Description
        "description": txn.description,
        # Confidence & flags
        "merchant_confidence": txn.merchant_confidence,
        "category_confidence": txn.category_confidence,
        "exclude_from_spending": txn.exclude_from_spending,
        "include_in_cashflow": txn.include_in_cashflow,
        # Pattern linking
        "recurring_pattern_id": txn.recurring_pattern_id or "",
        # Temporal features
        "day_of_week": txn.day_of_week,
        "is_weekend": txn.is_weekend,
        "is_month_start": txn.is_month_start,
        "is_month_end": txn.is_month_end,
        # Provenance (minimal)
        "source_file": txn.source_file,
        "source_period": txn.source_period,
    }


def write_llm_csv(path: Path, transactions: list[Transaction]) -> None:
    """
    Write transactions to an analysis-oriented CSV (ML features + temporal patterns).

    WARNING — this is NOT a privacy-safe export. It carries the raw bank ``description``,
    raw ``merchant``, ``account``, and exact ``amount_signed`` for every transaction with
    NO redaction or bucketing. Do not commit it (it is gitignored as ``llm_export_*.csv``)
    and do not hand it to an external service as-is. To share with an LLM/third party, use
    the redacted ``export package`` presets (e.g. ``--preset chatgpt-safe``) in
    ``exporting/service.py`` instead.

    - Includes ML features (anomalies, patterns, temporal features)
    - Excludes technical metadata (fingerprints, parser versions)
    - Includes confidence scores for merchant/category enrichment
    """
    with private_atomic_text(path, newline="") as f:
        writer = csv.DictWriter(f, fieldnames=LLM_COLUMNS)
        writer.writeheader()

        for txn in transactions:
            writer.writerow(neutralize_csv_row(_transaction_to_llm_dict(txn)))


# === Parquet Writer ===


def write_parquet(path: Path, transactions: list[Transaction]) -> None:
    """
    Write transactions to Parquet file.

    Parquet is ideal for data analysis:
    - Columnar format (fast aggregations)
    - Compressed (small file size)
    - Works with pandas, polars, DuckDB
    """
    try:
        import polars as pl
    except ImportError:
        raise RuntimeError(
            "polars is required for Parquet output. Install with: pip install polars"
        )

    if not transactions:
        # Create empty DataFrame with schema
        df = pl.DataFrame(
            schema={
                "txn_id": pl.Utf8,
                "posting_date": pl.Date,
                "transaction_date": pl.Date,
                "institution": pl.Utf8,
                "account": pl.Utf8,
                "flow": pl.Utf8,
                "cashflow_type": pl.Utf8,
                "include_in_cashflow": pl.Boolean,
                "currency": pl.Utf8,
                "amount_raw": pl.Utf8,
                "amount_signed": pl.Utf8,
                "description": pl.Utf8,
                "merchant": pl.Utf8,
                "category": pl.Utf8,
                "merchant_confidence": pl.Utf8,
                "category_confidence": pl.Utf8,
                "exclude_from_spending": pl.Boolean,
                "source_period": pl.Utf8,
            }
        )
    else:
        # Build records for Polars
        records = []
        for txn in transactions:
            records.append(
                {
                    "txn_id": txn.txn_id,
                    "posting_date": txn.posting_date,
                    "transaction_date": txn.transaction_date,
                    "institution": txn.institution,
                    "account": txn.account,
                    "flow": txn.flow,
                    "cashflow_type": txn.cashflow_type,
                    "include_in_cashflow": txn.include_in_cashflow,
                    "currency": txn.currency,
                    "amount_raw": str(txn.amount_raw),
                    "amount_signed": str(txn.amount_signed),
                    "description": txn.description,
                    "merchant": txn.merchant,
                    "category": txn.category,
                    "merchant_confidence": txn.merchant_confidence,
                    "category_confidence": txn.category_confidence,
                    "exclude_from_spending": txn.exclude_from_spending,
                    "source_period": txn.source_period,
                }
            )

        df = pl.DataFrame(records)

    with private_atomic_binary(path) as handle:
        df.write_parquet(handle, compression="zstd")


# === JSON Writer ===


class DecimalEncoder(json.JSONEncoder):
    """JSON encoder that handles Decimal and date types (via the canonical hook)."""

    def default(self, obj: Any) -> Any:
        converted = decimal_json(obj)
        return converted if converted is not obj else super().default(obj)


def write_summary_json(path: Path, summary: Summary) -> None:
    """Write summary to JSON file."""
    data = summary.model_dump()

    with private_atomic_text(path) as f:
        json.dump(data, f, indent=2, cls=DecimalEncoder, ensure_ascii=False)


# === Convenience functions ===


def write_all(
    output_dir: Path,
    transactions: list[Transaction],
    transfers: list[Transaction],
    exceptions: list[ExceptionRecord],
    summary: Summary,
    label: str = "all",
    formats: list[str] | None = None,
) -> dict[str, Path]:
    """
    Write all output files.

    Args:
        output_dir: Output directory
        transactions: Regular transactions
        transfers: Transfer transactions
        exceptions: Exception records
        summary: Processing summary
        label: Label for filenames (e.g., "2025", "2025-Q1")
        formats: Output formats (default: ["csv"]; only parquet requires the 'analysis' extra; csv and json work on a core install)

    Returns:
        Dict mapping format/type to output path

    Note:
        When CSV format is enabled, an additional analysis-oriented export
        (llm_export_{label}.csv) is generated, combining transactions and transfers
        with ML features included. Like the other CSVs here it contains RAW,
        un-redacted PII (description/merchant/amount) — see write_llm_csv. All of
        these files are gitignored; use the redacted ``export package`` presets to
        share data externally.
    """
    formats = formats or ["csv"]
    ensure_private_dir(output_dir)

    outputs: dict[str, Path] = {}

    if "csv" in formats:
        txn_csv = output_dir / f"transactions_{label}.csv"
        write_csv(txn_csv, transactions)
        outputs["transactions_csv"] = txn_csv

        transfers_csv = output_dir / f"transfers_{label}.csv"
        write_csv(transfers_csv, transfers)
        outputs["transfers_csv"] = transfers_csv

        exceptions_csv = output_dir / f"exceptions_{label}.csv"
        write_exceptions_csv(exceptions_csv, exceptions)
        outputs["exceptions_csv"] = exceptions_csv

        # LLM-optimized export (combines transactions + transfers)
        llm_csv = output_dir / f"llm_export_{label}.csv"
        all_txns = transactions + transfers
        write_llm_csv(llm_csv, all_txns)
        outputs["llm_export_csv"] = llm_csv

    if "parquet" in formats:
        txn_parquet = output_dir / f"transactions_{label}.parquet"
        write_parquet(txn_parquet, transactions)
        outputs["transactions_parquet"] = txn_parquet

        transfers_parquet = output_dir / f"transfers_{label}.parquet"
        write_parquet(transfers_parquet, transfers)
        outputs["transfers_parquet"] = transfers_parquet

    if "json" in formats:
        summary_json = output_dir / f"summary_{label}.json"
        write_summary_json(summary_json, summary)
        outputs["summary_json"] = summary_json

    return outputs
