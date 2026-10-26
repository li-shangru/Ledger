"""M22 — money is serialized and aggregated as exact Decimal, never float.

Tests cover:
1. Parquet stores amount_raw / amount_signed as exact strings (pl.Utf8), not Float64.
2. Export summary (non-share-safe) aggregates in Decimal — no float-sum drift.
3. Share-safe bucketing still works when aggregation is Decimal-based (regression).
"""

from __future__ import annotations

import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from ledger.exporting import ExportPackageService
from ledger.storage.database import TransactionDB, init_database, session_scope

# ---------------------------------------------------------------------------
# Helpers shared by all three tests
# ---------------------------------------------------------------------------


def _temporal(d: date) -> dict:
    return {
        "day_of_week": d.weekday(),
        "day_of_month": d.day,
        "week_of_year": d.isocalendar()[1],
        "is_weekend": d.weekday() >= 5,
        "is_month_start": d.day <= 3,
        "is_month_end": d.day >= 28,
    }


def _insert(
    db_path: Path,
    txn_id: str,
    *,
    amount: Decimal,
    posted: date = date(2026, 3, 15),
    category: str = "TestCat",
    flow: str = "expense",
    cashflow_type: str = "expense",
) -> None:
    """Insert a minimal transaction row; amount is treated as a spend (amount_signed = -amount)."""
    raw = abs(amount)
    signed = amount  # caller passes signed value directly
    with session_scope(db_path) as session:
        session.add(
            TransactionDB(
                txn_id=txn_id,
                posting_date=posted,
                transaction_date=posted,
                institution="testbank",
                account="test-acct",
                account_type="chequing",
                flow=flow,
                cashflow_type=cashflow_type,
                include_in_cashflow=True,
                currency="CAD",
                amount_raw=raw,
                amount_signed=signed,
                description=f"txn {txn_id}",
                merchant="TEST MERCHANT",
                category=category,
                source_file="test.csv",
                source_path="/test/test.csv",
                source_period="2026-03",
                source_row=1,
                imported_at=datetime.now(UTC).replace(tzinfo=None),
                parser_version="test",
                txn_fingerprint=f"{txn_id}-fp",
                **_temporal(posted),
            )
        )


# ---------------------------------------------------------------------------
# Test 1 — Export summary reconciles to the cent (no float drift)
# ---------------------------------------------------------------------------

# Values chosen so that naive float aggregation drifts when serialized by
# to_csv() (which shows full float precision, unlike to_json()'s rounding).
#
#   pandas groupby agg with float lambdas:
#     sum([-0.10]*34 + [-0.20]*34 + [-0.07]*33)  →  -12.510000000000002  (float)
#   Decimal-exact:
#     34*Decimal("0.10") + 34*Decimal("0.20") + 33*Decimal("0.07") = Decimal("12.51")
#
# The drift shows up in category_summary.csv (written via to_csv which doesn't
# round floats), confirming the bug. Post-fix the CSV must contain "12.51" not
# "12.510000000000002".

_SPEND_VALUES: list[Decimal] = (
    [Decimal("0.10")] * 34 + [Decimal("0.20")] * 34 + [Decimal("0.07")] * 33
)  # 101 transactions; exact total = Decimal("12.51")

_EXACT_SPEND = sum(_SPEND_VALUES, Decimal("0"))  # Decimal("12.51")

# The drifted float representation that must NOT appear after the fix
_DRIFTED_STR = "12.510000000000002"


def test_export_summary_aggregates_are_cent_exact(tmp_path: Path) -> None:
    """Category summary gross_spend must be cent-exact (no float-sum drift).

    Pre-fix: float groupby aggregation produces 12.510000000000002 in the CSV.
    Post-fix: Decimal aggregation produces exactly "12.51".

    Uses category_summary.csv (written via to_csv which preserves full float
    precision) rather than summary.json (which rounds via pandas double_precision).
    """
    db_path = tmp_path / "ledger.db"
    init_database(db_path)

    for i, val in enumerate(_SPEND_VALUES):
        _insert(db_path, f"spend-{i}", amount=-val, category="DriftCat")  # expenses are negative

    result = ExportPackageService(db_path).create_package(
        dest_dir=tmp_path / "exports",
        preset="full-local-audit",
        export_format="zip",
    )

    with zipfile.ZipFile(result.path) as zf:
        category_csv = zf.read("category_summary.csv").decode()

    # The drifted float string must not appear (pre-fix it would)
    assert _DRIFTED_STR not in category_csv, (
        f"Float drift string {_DRIFTED_STR!r} found in category_summary.csv; "
        "aggregation is not Decimal-exact"
    )

    # The exact Decimal value must appear
    assert str(_EXACT_SPEND) in category_csv, (
        f"Exact amount {_EXACT_SPEND!r} not found in category_summary.csv"
    )


# ---------------------------------------------------------------------------
# Test 2 — Parquet stores exact money strings, not Float64
# ---------------------------------------------------------------------------


def _make_transaction(txn_id: str, amount_raw: Decimal, amount_signed: Decimal):
    """Build a minimal Transaction object for write_parquet."""
    from ledger.core import Transaction

    return Transaction(
        txn_id=txn_id,
        txn_fingerprint=f"{txn_id}-fp",
        posting_date=date(2026, 3, 15),
        transaction_date=date(2026, 3, 15),
        institution="testbank",
        account="test-acct",
        account_type="chequing",
        flow="expense",
        cashflow_type="expense",
        include_in_cashflow=True,
        currency="CAD",
        amount_raw=amount_raw,
        amount_signed=amount_signed,
        description="test item",
        merchant="TEST",
        category="TestCat",
        merchant_confidence="none",
        category_confidence="low",
        exclude_from_spending=False,
        merchant_raw="",
        category_raw="",
        source_file="test.csv",
        source_path="/test/test.csv",
        source_period="2026-03",
        source_row=1,
        imported_at=datetime(2026, 3, 15, 12, 0, 0),
        parser_version="test",
    )


def test_parquet_stores_amount_as_exact_string(tmp_path: Path) -> None:
    """amount_raw and amount_signed in parquet must be exact strings, not Float64.

    0.1 + 0.2 cannot be represented exactly in IEEE 754 float64; storing as a
    string preserves the exact decimal representation.
    """
    import polars as pl

    from ledger.output.writers import write_parquet

    # These amounts are classically float-imprecise:
    # float(Decimal("0.10")) == 0.1, but 0.1 in float64 is not exactly 0.1.
    # A round-trip through Float64 would produce "0.1" not "0.10"; more importantly
    # 0.1 + 0.2 in float == 0.30000000000000004 not 0.30.
    txns = [
        _make_transaction("t1", Decimal("0.10"), Decimal("-0.10")),
        _make_transaction("t2", Decimal("0.20"), Decimal("-0.20")),
    ]

    parquet_path = tmp_path / "test.parquet"
    write_parquet(parquet_path, txns)

    df = pl.read_parquet(parquet_path)

    # Must be stored as strings (Utf8), not Float64
    assert df.schema["amount_raw"] == pl.Utf8, (
        f"amount_raw dtype is {df.schema['amount_raw']!r}; expected pl.Utf8"
    )
    assert df.schema["amount_signed"] == pl.Utf8, (
        f"amount_signed dtype is {df.schema['amount_signed']!r}; expected pl.Utf8"
    )

    # The string values must be the EXACT decimal representations
    raw_vals = df["amount_raw"].to_list()
    signed_vals = df["amount_signed"].to_list()

    assert raw_vals == ["0.10", "0.20"], (
        f"amount_raw strings are {raw_vals!r}; expected exact Decimal strings"
    )
    assert signed_vals == ["-0.10", "-0.20"], (
        f"amount_signed strings are {signed_vals!r}; expected exact Decimal strings"
    )

    # Prove that parsing them back gives exact Decimals (round-trip check)
    assert Decimal(raw_vals[0]) + Decimal(raw_vals[1]) == Decimal("0.30")


# ---------------------------------------------------------------------------
# Test 3 — Share-safe bucketing still works after Decimal aggregation (regression)
# ---------------------------------------------------------------------------


def test_share_safe_bucketing_still_works_with_decimal_aggregation(tmp_path: Path) -> None:
    """chatgpt-safe preset buckets aggregate amounts to nearest $10 (P2-9 regression).

    Insert a single transaction with amount NOT a multiple of $10 (e.g. 37.41).
    The category_summary gross_spend and net must be bucketed (40.0), not exact.
    This verifies that Decimal aggregation composes with the existing bucketing.
    """
    db_path = tmp_path / "ledger.db"
    init_database(db_path)

    # 37.41 → nearest $10 bucket → 40.0
    _insert(db_path, "bucket-test-1", amount=Decimal("-37.41"), category="ZZBucketTest")

    result = ExportPackageService(db_path).create_package(
        dest_dir=tmp_path / "exports",
        preset="chatgpt-safe",
        export_format="zip",
    )

    with zipfile.ZipFile(result.path) as zf:
        category_csv = zf.read("category_summary.csv").decode()
        summary_text = zf.read("summary.json").decode()

    # The exact amount must NOT appear in any share-safe file
    assert "37.41" not in category_csv, "exact amount leaked in category_summary (not bucketed)"
    assert "37.41" not in summary_text, "exact amount leaked in summary.json (not bucketed)"

    # The bucketed value (40.0 for nearest $10) must appear.
    # bucket_amount returns float, so 40.0 is serialized as "40.0" (not "40").
    # This precise check ensures the assertion cannot pass vacuously on "40" appearing
    # elsewhere in the CSV (e.g. in a date or count column).
    assert "40.0" in category_csv, "bucketed gross_spend (40.0) missing from category_summary"
