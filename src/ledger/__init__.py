"""
Ledger - Personal finance ledger with multi-institution support.

A modular, plugin-based transaction processing system designed for:
- Parsing bank and credit card CSV exports
- Normalizing merchants and categories
- Deduplication and validation
- Output to analysis-friendly formats (Parquet, CSV)

Usage:
    # As CLI
    ledger build --src ./exports --out-dir ./output ...

    # As library
    from ledger.config import load_app_config
    from ledger.processing import process
    from ledger.output import write_all

Example:
    >>> from ledger.config import load_app_config
    >>> from ledger.processing import process
    >>>
    >>> config = load_app_config(
    ...     ledger_path=Path("ledger.toml"),
    ...     merchants_path=Path("merchants.toml"),
    ...     categories_path=Path("categories.toml"),
    ... )
    >>>
    >>> result = process(
    ...     config=config,
    ...     source_root=Path("./exports"),
    ...     year="2025",
    ... )
    >>>
    >>> print(f"Processed {len(result.transactions)} transactions")
"""

__version__ = "1.11.2"
__author__ = "Shangru Li"

# Re-export commonly used items
from ledger.config import AppConfig, load_app_config
from ledger.core import ExceptionRecord, Summary, Transaction
from ledger.output import write_all, write_csv, write_parquet
from ledger.processing import ProcessingResult, process

__all__ = [
    # Config
    "load_app_config",
    "AppConfig",
    # Models
    "Transaction",
    "ExceptionRecord",
    "Summary",
    # Processing
    "process",
    "ProcessingResult",
    # Output
    "write_all",
    "write_csv",
    "write_parquet",
]
