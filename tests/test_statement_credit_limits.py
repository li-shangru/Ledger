"""Tests for persisted printed statement credit limits (P3.1).

Covers:
  - the account_statement_credit_limits migration (idempotent; table exists),
  - the upsert / get-latest storage helpers,
  - AccountDetector preferring a stored printed limit over the estimate,
  - the run_account_analysis integration path using a stored limit,
  - the finalize-population helper that reads the extraction manifest.
"""

import json
import sqlite3
from contextlib import closing
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ledger.analysis.account_detector import AccountDetector
from ledger.analysis.runner import AnalysisRunner
from ledger.core import Transaction
from ledger.imports.pdf.post_processing import persist_printed_credit_limits
from ledger.processing import ProcessingResult
from ledger.storage.database import AccountDB, init_database, session_scope
from ledger.storage.migrations import run_migrations
from ledger.storage.operations import (
    get_latest_statement_credit_limit,
    upsert_statement_credit_limit,
)
from ledger.storage.writer import DatabaseWriter


def _table_exists(db_path: Path, table: str) -> bool:
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# 1. Migration idempotency + table creation
# ---------------------------------------------------------------------------


def test_credit_limit_migration_is_idempotent_and_creates_table(tmp_path):
    """Running migrations (twice) must not error and must create the table."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)

    first = run_migrations(db_path)
    assert first.status == "success"
    assert _table_exists(db_path, "account_statement_credit_limits")

    # Running again is a no-op (idempotent) and still succeeds.
    second = run_migrations(db_path)
    assert second.status == "success"
    assert second.counts["applied"] == 0
    assert _table_exists(db_path, "account_statement_credit_limits")


# ---------------------------------------------------------------------------
# 2. Storage helpers: upsert + get-latest-by-statement-date
# ---------------------------------------------------------------------------


def test_upsert_then_get_returns_latest_by_statement_date(tmp_path):
    db_path = tmp_path / "ledger.db"
    init_database(db_path)

    # No limit stored yet.
    assert get_latest_statement_credit_limit(db_path, "lumen-visa-9108") is None

    # Store an older statement's limit, then a newer one with a different value.
    upsert_statement_credit_limit(db_path, "lumen-visa-9108", Decimal("6000.00"), date(2040, 1, 31))
    upsert_statement_credit_limit(db_path, "lumen-visa-9108", Decimal("8000.00"), date(2040, 6, 30))

    # Latest by statement_date wins.
    assert get_latest_statement_credit_limit(db_path, "lumen-visa-9108") == Decimal("8000.00")

    # A different account is unaffected.
    assert get_latest_statement_credit_limit(db_path, "harbor-spark-visa-1004") is None


def test_upsert_same_statement_date_replaces_value(tmp_path):
    """Re-importing the same statement (same slug + date) updates in place."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)

    upsert_statement_credit_limit(db_path, "lumen-visa-9108", Decimal("6000.00"), date(2040, 6, 30))
    upsert_statement_credit_limit(db_path, "lumen-visa-9108", Decimal("8000.00"), date(2040, 6, 30))

    assert get_latest_statement_credit_limit(db_path, "lumen-visa-9108") == Decimal("8000.00")
    # Exactly one row for that (slug, date).
    with closing(sqlite3.connect(db_path)) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM account_statement_credit_limits WHERE account_slug=?",
            ("lumen-visa-9108",),
        ).fetchone()[0]
    assert count == 1


# ---------------------------------------------------------------------------
# 3. AccountDetector prefers the stored printed limit over the estimate
# ---------------------------------------------------------------------------


def _card_txn(i: int, d: date, amt: Decimal, account: str) -> Transaction:
    return Transaction(
        txn_id=f"c{account}{i}",
        txn_fingerprint=f"fp{account}{i}",
        posting_date=d,
        institution="lumen",
        account=account,
        account_type="Visa",
        flow="expense",
        cashflow_type="expense",
        include_in_cashflow=True,
        currency="CAD",
        amount_raw=abs(amt),
        amount_signed=amt,
        description="x",
        source_file="x.csv",
        source_path="x.csv",
        source_period="2040-06",
        source_row=i,
        imported_at=date(2040, 6, 1),
        parser_version="t",
    )


def test_build_account_prefers_stored_printed_limit(tmp_path):
    """A credit-card account with a stored printed limit must use it, not the estimate."""
    db_path = tmp_path / "acct.db"
    init_database(db_path)

    slug = "lumen-visa-9108"
    # Purchases would estimate a deepest-balance limit of ~$2000.
    txns = [
        _card_txn(0, date(2040, 6, 5), Decimal("-1000"), slug),
        _card_txn(1, date(2040, 6, 15), Decimal("-1000"), slug),
    ]

    detector = AccountDetector(db_path)

    # Without a stored limit: falls back to the deepest-balance estimate.
    accounts = detector.detect_accounts(txns)
    assert len(accounts) == 1
    assert accounts[0].detected_credit_limit == Decimal("2000")

    # Store the printed limit; now it must win.
    upsert_statement_credit_limit(db_path, slug, Decimal("8000.00"), date(2040, 6, 30))
    accounts = detector.detect_accounts(txns)
    assert accounts[0].detected_credit_limit == Decimal("8000.00")


# ---------------------------------------------------------------------------
# 4. Integration: run_account_analysis uses the stored limit
# ---------------------------------------------------------------------------


def test_run_account_analysis_uses_stored_printed_limit(tmp_path):
    db_path = tmp_path / "runner.db"
    init_database(db_path)

    slug = "lumen-visa-9108"
    purchases = [
        _card_txn(0, date(2040, 6, 5), Decimal("-1000"), slug),
        _card_txn(1, date(2040, 6, 15), Decimal("-1000"), slug),
    ]
    DatabaseWriter(db_path).write_result(
        ProcessingResult(
            transactions=purchases,
            transfers=[],
            exceptions=[],
            duplicates_removed=0,
            total_scanned=len(purchases),
        ),
        Path("/x"),
        "t",
    )

    upsert_statement_credit_limit(db_path, slug, Decimal("8000.00"), date(2040, 6, 30))

    AnalysisRunner(db_path).run_account_analysis()

    with session_scope(db_path) as session:
        account = (
            session.query(AccountDB).filter_by(institution="lumen", account_number=slug).first()
        )
        assert account is not None
        assert account.detected_credit_limit == Decimal("8000.00")


def test_run_account_analysis_without_stored_limit_uses_estimate(tmp_path):
    """Regression: with no stored limit, the estimate behaviour is unchanged."""
    db_path = tmp_path / "runner2.db"
    init_database(db_path)

    slug = "lumen-visa-9108"
    purchases = [
        _card_txn(0, date(2040, 6, 5), Decimal("-1000"), slug),
        _card_txn(1, date(2040, 6, 15), Decimal("-1000"), slug),
    ]
    DatabaseWriter(db_path).write_result(
        ProcessingResult(
            transactions=purchases,
            transfers=[],
            exceptions=[],
            duplicates_removed=0,
            total_scanned=len(purchases),
        ),
        Path("/x"),
        "t",
    )

    AnalysisRunner(db_path).run_account_analysis()

    with session_scope(db_path) as session:
        account = (
            session.query(AccountDB).filter_by(institution="lumen", account_number=slug).first()
        )
        assert account is not None
        # Deepest balance owed across two $1000 purchases with no payments = $2000.
        assert account.detected_credit_limit == Decimal("2000")


# ---------------------------------------------------------------------------
# 5. Finalize wiring: populate the table from the extraction manifest
# ---------------------------------------------------------------------------


def _write_manifest(path: Path, entries: list[dict]) -> None:
    path.write_text(json.dumps(entries), encoding="utf-8")


def test_persist_printed_credit_limits_from_manifest(tmp_path):
    """The finalize helper reads each statement's printed_credit_limit + slug
    (CSV stem) and its statement month, and upserts non-empty limits."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)

    manifest = tmp_path / "extraction-manifest.json"
    _write_manifest(
        manifest,
        [
            {
                "csv_path": str(tmp_path / "csv" / "2040-06" / "lumen-visa-9108.csv"),
                "statement_type": "lumen_credit",
                "validation_summary": {"printed_credit_limit": "8000.00"},
            },
            # HARBOR Visa card in an earlier month.
            {
                "csv_path": str(tmp_path / "csv" / "2040-05" / "harbor-spark-visa-1004.csv"),
                "statement_type": "harbor_visa",
                "validation_summary": {"printed_credit_limit": "5000.00"},
            },
            # A deposit statement with an empty limit must be skipped.
            {
                "csv_path": str(tmp_path / "csv" / "2040-06" / "harbor-chequing-1001.csv"),
                "statement_type": "harbor_deposit",
                "validation_summary": {"printed_credit_limit": ""},
            },
        ],
    )

    count = persist_printed_credit_limits(db_path, manifest)

    assert count == 2
    assert get_latest_statement_credit_limit(db_path, "lumen-visa-9108") == Decimal("8000.00")
    assert get_latest_statement_credit_limit(db_path, "harbor-spark-visa-1004") == Decimal(
        "5000.00"
    )
    # The empty deposit limit was skipped.
    assert get_latest_statement_credit_limit(db_path, "harbor-chequing-1001") is None

    # The stored statement_date is derived from the CSV's YYYY-MM parent folder.
    with closing(sqlite3.connect(db_path)) as conn:
        stored = conn.execute(
            "SELECT statement_date FROM account_statement_credit_limits WHERE account_slug=?",
            ("lumen-visa-9108",),
        ).fetchone()[0]
    assert str(stored).startswith("2040-06")


def test_persist_printed_credit_limits_uses_aliased_read_key(tmp_path):
    """The persist key must be normalized the SAME way ingestion normalizes
    TransactionDB.account (clean_account_name(stem, aliases)). Otherwise, when an
    account_aliases entry is configured, the persisted limit is keyed by the raw stem
    while AccountDetector reads it back by the aliased name — a silent miss that drops
    the printed limit to the estimate."""
    from ledger.utils.text import clean_account_name

    db_path = tmp_path / "ledger.db"
    init_database(db_path)

    # Configured alias: the CSV stem maps to a canonical account name, exactly as the
    # parser applies it to TransactionDB.account.
    aliases = {"lumen-visa-9108": "lumen-visa-main"}

    manifest = tmp_path / "extraction-manifest.json"
    _write_manifest(
        manifest,
        [
            {
                "csv_path": str(tmp_path / "csv" / "2040-06" / "lumen-visa-9108.csv"),
                "statement_type": "lumen_credit",
                "validation_summary": {"printed_credit_limit": "8000.00"},
            }
        ],
    )

    count = persist_printed_credit_limits(db_path, manifest, account_aliases=aliases)
    assert count == 1

    # The detector reads back through the normalized/aliased account name.
    read_key = clean_account_name("lumen-visa-9108", aliases)
    assert read_key == "lumen-visa-main"
    assert get_latest_statement_credit_limit(db_path, read_key) == Decimal("8000.00")
    # The raw stem must NOT be the stored key (that was the bug).
    assert get_latest_statement_credit_limit(db_path, "lumen-visa-9108") is None


def test_persist_printed_credit_limits_prefers_manifest_account_slug_over_storage_stem(tmp_path):
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    aliases = {"lumen-visa-9108": "lumen-visa-main"}
    manifest = tmp_path / "extraction-manifest.json"
    _write_manifest(
        manifest,
        [
            {
                "csv_path": str(tmp_path / "csv" / "2040-06" / "lumen-visa-9108-2.csv"),
                "account_slug": "lumen-visa-9108",
                "statement_type": "lumen_credit",
                "validation_summary": {"printed_credit_limit": "8000.00"},
            }
        ],
    )

    count = persist_printed_credit_limits(db_path, manifest, account_aliases=aliases)

    assert count == 1
    assert get_latest_statement_credit_limit(db_path, "lumen-visa-main") == Decimal("8000.00")
    assert get_latest_statement_credit_limit(db_path, "lumen-visa-9108-2") is None


def test_persist_printed_credit_limits_missing_manifest_is_noop(tmp_path):
    """A missing manifest path must not raise; it stores nothing."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)

    count = persist_printed_credit_limits(db_path, tmp_path / "does-not-exist.json")
    assert count == 0


def test_persist_printed_credit_limits_skips_entries_without_csv(tmp_path):
    """Skipped/failed statements (no csv_path) contribute no rows even if a limit
    somehow appears in their summary."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)

    manifest = tmp_path / "extraction-manifest.json"
    _write_manifest(
        manifest,
        [
            {
                "csv_path": "",
                "statement_type": "parser_exception",
                "validation_summary": {"printed_credit_limit": "9999.00"},
            }
        ],
    )

    count = persist_printed_credit_limits(db_path, manifest)
    assert count == 0


pytestmark = pytest.mark.usefixtures("fictional_adapter_metadata")
