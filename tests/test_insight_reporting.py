"""Reporting movement math + insight fingerprinting."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ledger.insights.service import GeneratedFinding
from ledger.reporting.service import _is_fixed_obligation, _movement
from ledger.storage.database import (
    DBOperationAuditDB,
    ReportSnapshotDB,
    TransactionDB,
    init_database,
    session_scope,
)


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _insert_txn(db_path: Path, txn_id: str, posted: date) -> None:
    with session_scope(db_path) as session:
        session.add(
            TransactionDB(
                txn_id=txn_id,
                posting_date=posted,
                transaction_date=posted,
                institution="test-bank",
                account="4111111111111111",
                account_type="visa",
                flow="expense",
                cashflow_type="expense",
                include_in_cashflow=True,
                currency="CAD",
                amount_raw=Decimal("50.00"),
                amount_signed=Decimal("-50.00"),
                description="MARKET",
                merchant="MARKET",
                category="Groceries",
                source_file="stmt.csv",
                source_path="/private/stmt.csv",
                source_period=posted.strftime("%Y-%m"),
                source_row=1,
                imported_at=_now(),
                parser_version="test",
                txn_fingerprint=f"{txn_id}-fp",
                day_of_week=posted.weekday(),
                day_of_month=posted.day,
                week_of_year=posted.isocalendar()[1],
                is_weekend=posted.weekday() >= 5,
                is_month_start=posted.day <= 3,
                is_month_end=posted.day >= 28,
            )
        )


def _count_rows(db_path: Path, model: type) -> int:
    with session_scope(db_path) as session:
        return session.query(model).count()


# ---------------------------------------------------------------------------
# Read-only snapshot tests (M5 fix)
# ---------------------------------------------------------------------------


def test_snapshot_for_period_readonly_does_not_write(tmp_path: Path) -> None:
    """Page render must NOT create snapshot or audit rows — read-only path.

    This test FAILS before the fix (snapshot_for_period falls through to
    create_snapshot which persists a snapshot + audit row).
    """
    from ledger.reporting.service import ReportSnapshotService

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_txn(db_path, "t1", date(2026, 5, 10))

    # Instantiate the service first (ensure_schema in __init__ may write its own
    # audit row) — count rows AFTER that, so only snapshot_for_period's writes matter.
    svc = ReportSnapshotService(db_path)
    before_snapshots = _count_rows(db_path, ReportSnapshotDB)
    before_audit = _count_rows(db_path, DBOperationAuditDB)

    result = svc.snapshot_for_period(
        date(2026, 5, 1),
        date(2026, 5, 31),
        persist=False,
    )

    after_snapshots = _count_rows(db_path, ReportSnapshotDB)
    after_audit = _count_rows(db_path, DBOperationAuditDB)

    # Neither table must grow.
    assert after_snapshots == before_snapshots, (
        f"snapshot_for_period(persist=False) wrote {after_snapshots - before_snapshots} "
        "new row(s) to report_snapshots — dashboard render must not persist."
    )
    assert after_audit == before_audit, (
        f"snapshot_for_period(persist=False) wrote {after_audit - before_audit} "
        "new row(s) to db_operations — dashboard render must not persist."
    )

    # Payload must still be usable.
    assert result is not None
    payload = result.get("payload", {})
    assert payload.get("status") in {"fresh", "empty"}


def test_snapshot_for_period_readonly_returns_usable_payload(tmp_path: Path) -> None:
    """Read-only path must return a payload with the same key fields the pages consume."""
    from ledger.reporting.service import ReportSnapshotService

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_txn(db_path, "t1", date(2026, 5, 10))

    svc = ReportSnapshotService(db_path)
    result = svc.snapshot_for_period(
        date(2026, 5, 1),
        date(2026, 5, 31),
        persist=False,
    )

    assert isinstance(result, dict)
    assert "payload" in result
    payload = result["payload"]
    # Fields consumed by the pages.
    assert "category_drift" in payload
    assert "credit_card_optimization" in payload
    assert "cost_cutting_candidates" in payload
    # Period metadata must be present.
    assert result.get("period_start") == date(2026, 5, 1)
    assert result.get("period_end") == date(2026, 5, 31)


def test_snapshot_for_period_readonly_returns_existing_cached_row(tmp_path: Path) -> None:
    """If a cached snapshot already exists and is fresh, read-only returns it (no new write)."""
    from ledger.reporting.service import ReportSnapshotService

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_txn(db_path, "t1", date(2026, 5, 10))

    svc = ReportSnapshotService(db_path)
    # First, write one via the persist path.
    persisted = svc.snapshot_for_period(
        date(2026, 5, 1),
        date(2026, 5, 31),
        persist=True,
    )
    before_snapshots = _count_rows(db_path, ReportSnapshotDB)

    # Now read-only should return the cached row without writing another.
    result = svc.snapshot_for_period(
        date(2026, 5, 1),
        date(2026, 5, 31),
        persist=False,
    )

    after_snapshots = _count_rows(db_path, ReportSnapshotDB)
    assert after_snapshots == before_snapshots, (
        "read-only path wrote a new row when a cached row existed"
    )
    assert result["id"] == persisted["id"], "read-only path did not return the cached row"


def test_snapshot_for_period_persist_true_still_writes(tmp_path: Path) -> None:
    """Explicit refresh (persist=True) must still write a snapshot + audit row (regression guard)."""
    from ledger.reporting.service import ReportSnapshotService

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_txn(db_path, "t1", date(2026, 5, 10))

    svc = ReportSnapshotService(db_path)
    # Count after service init so ensure_schema audit rows don't interfere.
    before_snapshots = _count_rows(db_path, ReportSnapshotDB)
    before_audit = _count_rows(db_path, DBOperationAuditDB)

    svc.snapshot_for_period(
        date(2026, 5, 1),
        date(2026, 5, 31),
        persist=True,
    )

    after_snapshots = _count_rows(db_path, ReportSnapshotDB)
    after_audit = _count_rows(db_path, DBOperationAuditDB)

    assert after_snapshots > before_snapshots, "persist=True must write a snapshot row"
    assert after_audit > before_audit, "persist=True must write an audit row"


def test_snapshot_for_period_empty_db_transient_path(tmp_path: Path) -> None:
    """persist=False on an empty DB returns a usable payload, writes nothing, no exception.

    Covers the untested branch where _compute_payload returns status="empty"
    and report_start/report_end are None — the fallback to supplied dates must work.
    """
    from ledger.reporting.service import ReportSnapshotService

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    # No transactions inserted — empty DB.

    svc = ReportSnapshotService(db_path)
    before_snapshots = _count_rows(db_path, ReportSnapshotDB)
    before_audit = _count_rows(db_path, DBOperationAuditDB)

    start = date(2026, 5, 1)
    end = date(2026, 5, 31)
    result = svc.snapshot_for_period(start, end, persist=False)

    after_snapshots = _count_rows(db_path, ReportSnapshotDB)
    after_audit = _count_rows(db_path, DBOperationAuditDB)

    # Nothing written.
    assert after_snapshots == before_snapshots, (
        "empty-DB transient path must not write a snapshot row"
    )
    assert after_audit == before_audit, "empty-DB transient path must not write an audit row"

    # Payload must have status "empty".
    assert result is not None
    payload = result.get("payload", {})
    assert payload.get("status") == "empty"

    # period_start/period_end must fall back to the supplied dates.
    assert result["period_start"] == start, (
        "period_start must fall back to supplied start when DB is empty"
    )
    assert result["period_end"] == end, "period_end must fall back to supplied end when DB is empty"


def test_movement_handles_zero_and_negative_baseline():
    # New from zero -> +100%, not a misleading 0%.
    assert _movement(Decimal("500"), Decimal("0"))["change_pct"] == 100.0
    # Both zero -> 0%.
    assert _movement(Decimal("0"), Decimal("0"))["change_pct"] == 0.0
    # Negative baseline: a net that improved from -100 to -50 (change +50) must read as
    # a positive %, not -50% (the old signed-divide flipped the sign).
    improved = _movement(Decimal("-50"), Decimal("-100"))
    assert improved["change"] == 50.0
    assert improved["change_pct"] > 0
    # Ordinary positive baseline is unchanged.
    assert _movement(Decimal("150"), Decimal("100"))["change_pct"] == 50.0


def test_anomaly_findings_distinct_by_txn_id():
    """Two anomalies on the same day/merchant/amount must not collapse to one finding;
    the txn_id now carried in the payload keeps their fingerprints distinct."""
    start, end = date(2026, 5, 1), date(2026, 5, 31)
    base = {"date": "2026-05-10", "merchant": "COSTCO", "category": "Groceries", "amount": 250.0}

    def finding(txn_id: str) -> GeneratedFinding:
        return GeneratedFinding(
            finding_type="anomalies",
            severity="medium",
            title="Unusual transaction at COSTCO",
            explanation="x",
            evidence={**base, "txn_id": txn_id},
            suggested_action="y",
        )

    assert finding("a1").fingerprint(start, end) != finding("b2").fingerprint(start, end)


def test_cost_cutting_skips_fixed_obligations():
    """Cost-cutting must not suggest reviewing fixed obligations (mortgage, insurance,
    property tax, condo fees) -- they are the biggest line items but not actionable."""
    from ledger.reporting.service import ReportSnapshotService

    top = {
        "MORTGAGE": Decimal("8000"),
        "PINE HOME & AUTO INSURANCE": Decimal("4000"),
        "EXAMPLE TOWN PROPERTY TAX": Decimal("700"),
        "UBER EATS": Decimal("1000"),
        "AMAZON": Decimal("800"),
    }
    out = ReportSnapshotService._cost_cutting_candidates(top, [])
    merchants = {c["merchant"] for c in out}

    assert "MORTGAGE" not in merchants
    assert "PINE HOME & AUTO INSURANCE" not in merchants
    assert "EXAMPLE TOWN PROPERTY TAX" not in merchants
    assert {"UBER EATS", "AMAZON"} <= merchants


def test_fixed_obligation_matches_whole_words_not_substrings():
    """The keyword match must be on whole words: 'rent' the obligation must not silently
    swallow 'CURRENT RESTAURANT' (a discretionary merchant that merely contains the
    letters r-e-n-t), or the dashboard would drop real cost-cutting candidates."""
    # Genuine fixed obligations are still excluded.
    assert _is_fixed_obligation("MORTGAGE PAYMENT")
    assert _is_fixed_obligation("PINE HOME & AUTO INSURANCE")
    assert _is_fixed_obligation("CITY PROPERTY TAX")
    assert _is_fixed_obligation("CEDAR UTILITIES")
    assert _is_fixed_obligation("APT RENT")
    # Discretionary merchants that only contain a keyword as a substring are NOT excluded.
    assert not _is_fixed_obligation("CURRENT RESTAURANT")
    assert not _is_fixed_obligation("PARENT TEACHER STORE")
    assert not _is_fixed_obligation("BRENT'S BURGERS")
    assert not _is_fixed_obligation("LOANER BIKE CAFE")


def test_category_drift_includes_percentage():
    """Drift rows carry a percentage so the dashboard can tell a doubling from a big-but-
    flat fixed cost."""
    from ledger.reporting.service import ReportSnapshotService

    out = ReportSnapshotService._category_drift(
        {"Dining": Decimal("300"), "New": Decimal("500")},
        {"Dining": Decimal("100")},
    )
    by_cat = {r["category"]: r for r in out}
    assert by_cat["Dining"]["change_pct"] == 200.0  # 100 -> 300
    assert by_cat["Dining"]["is_new"] is False
    # A category appearing from nothing has no defined percentage. This previously
    # reported a flat 100, which is indistinguishable from a category that merely
    # doubled -- so it is flagged instead and the UI labels it "new".
    assert by_cat["New"]["is_new"] is True
    assert by_cat["New"]["change_pct"] == 0.0


# ---------------------------------------------------------------------------
# Snapshot money precision (float boundary regression guard)
# ---------------------------------------------------------------------------


def test_snapshot_money_float_cent_accuracy_regression() -> None:
    """Snapshot money fields (pre-quantized Decimals cast to float) render to
    the correct cents across a broad range of realistic magnitudes.

    WHY float is acceptable here
    =============================
    Every money value that enters the snapshot payload through _amount() /
    _json_safe() has already been quantized to cents by the query layer:

    * KPI fields (income, expenses, net, gross_spend, offsets, true_cost):
      produced by _money() / _cents() in the query/adjustments layer, both of
      which call .quantize(Decimal("0.01")) before returning.
    * top_categories / top_merchants amounts: from _cents() (same quantize).
    * Anomaly amounts: sum of cent-quantized amount_signed columns.
    * recurring expected_amount: Numeric(12,2) DB column (always cent-exact).
    * account_card_summaries net/volume: float from SQLite, but these are sums
      of cent-quantized rows.  The rendered :.2f result is exact for magnitudes
      that appear in personal finance.

    A float64 (IEEE 754 double) can represent every cent-quantized dollar value
    exactly up to ~$90 trillion (2^53 / 100 ≈ 90,071,992,547,409.92), which is
    far beyond any personal-finance amount.  So float -> ':.2f' is lossless for
    all realistic inputs.

    The only un-quantized fields are change_pct / change_pct columns, which
    are rendered as ':.1f%' (not money :.2f), so their fractional digits are
    not a precision concern for cent accuracy.

    This test pins that invariant: if a future refactor un-quantizes a money
    field before the float cast, the mismatch will be caught here.
    """
    from ledger.reporting.service import _amount

    def snap_fmt(d: Decimal) -> str:
        """Mimic the snapshot render path: Decimal → float → :.2f."""
        return f"{float(_amount(d)):,.2f}"

    def decimal_fmt(d: Decimal) -> str:
        """Direct Decimal render (the reference path)."""
        return f"{d:,.2f}"

    # Build a representative set of cent-quantized money values.
    # These cover small amounts, typical monthly budgets, and large totals.
    cent_quantized: list[Decimal] = []
    for dollars in [
        0,
        1,
        9,
        99,
        100,
        999,
        1_000,
        9_999,
        10_000,
        99_999,
        100_000,
        999_999,
        1_000_000,
        9_999_999,
    ]:
        for cents_val in range(0, 100, 7):  # 0, 7, 14, … 98 — a spread across the decade
            d = Decimal(f"{dollars}.{cents_val:02d}")
            cent_quantized.append(d)
            cent_quantized.append(-d)

    # Also include every boundary cent for a typical range.
    for cents_val in range(100):
        cent_quantized.append(Decimal(f"1234.{cents_val:02d}"))
        cent_quantized.append(Decimal(f"9999.{cents_val:02d}"))
        cent_quantized.append(Decimal(f"100000.{cents_val:02d}"))

    mismatches = []
    for d in cent_quantized:
        sf = snap_fmt(d)
        df = decimal_fmt(d)
        if sf != df:
            mismatches.append((d, sf, df))

    assert not mismatches, (
        f"float snapshot path produced wrong cents for {len(mismatches)} value(s); "
        f"first: Decimal({mismatches[0][0]!r}) → snap={mismatches[0][1]!r} "
        f"vs decimal={mismatches[0][2]!r}.  "
        "A money field may no longer be pre-quantized before _amount() is called."
    )


# --- M4 (2026-07-02 audit): snapshot freshness gate must cover review_apply etc. ---


def _record_op(db_path: Path, operation: str) -> None:
    from ledger.core.results import RunResult
    from ledger.storage.operations import record_db_operation, utc_now_dt

    now = utc_now_dt()
    record_db_operation(
        db_path,
        operation=operation,
        result=RunResult(status="success", started_at=now, finished_at=now),
    )


@pytest.mark.parametrize(
    "operation",
    [
        "review_apply",
        "category_override",
        "category_optimize",
        "statement_repair_promote",
        "data_cleanup",
        "proposal_apply",
    ],
)
def test_snapshot_for_period_recomputes_after_mutating_op(tmp_path: Path, operation: str) -> None:
    """A cached snapshot written before a mutating op must be treated as stale:
    snapshot_for_period must recompute (id is None) rather than serve the cache."""
    from ledger.reporting import ReportSnapshotService

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_txn(db_path, "m4-001", date(2026, 5, 10))
    svc = ReportSnapshotService(db_path)
    start, end = date(2026, 5, 1), date(2026, 5, 31)

    # Persist a cached snapshot, then record a mutating op AFTER it.
    cached = svc.snapshot_for_period(start, end, persist=True)
    assert cached["id"] is not None
    _record_op(db_path, operation)

    fresh = svc.snapshot_for_period(start, end, persist=False)
    assert fresh["id"] is None, (
        f"stale snapshot served after {operation!r} (freshness gate omits it)"
    )


def test_snapshot_for_period_recomputes_after_rules_apply(tmp_path: Path) -> None:
    """A bulk rules-apply rewrites merchant/category/exclusion on existing rows, so a
    snapshot written before it must be treated as stale. apply_all must record a
    'rules_apply' DB-operation audit row AND that op must be in the freshness allowlist,
    so snapshot_for_period recomputes (id is None) rather than serve pre-apply KPIs."""
    from ledger.config.repository import SQLiteConfigRepository
    from ledger.config.schemas import (
        CategoriesConfig,
        CategoryRule,
        MerchantRule,
        MerchantRulesConfig,
    )
    from ledger.reporting import ReportSnapshotService
    from ledger.rules import RuleApplicationService

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_txn(db_path, "ra-001", date(2026, 5, 10))
    # Seed a rule that recategorizes the MARKET row (Groceries -> Dining), so apply_all
    # actually mutates a row (transactions_updated >= 1).
    repo = SQLiteConfigRepository(db_path)
    repo.replace_from_configs(
        MerchantRulesConfig(
            version="test",
            rules=[MerchantRule(merchant_norm="MARKET", category="Dining", patterns=["MARKET"])],
        ),
        CategoriesConfig(
            version="test",
            categories=[
                CategoryRule(name="Groceries", merchants=[]),
                CategoryRule(name="Dining", merchants=[]),
            ],
        ),
    )

    svc = ReportSnapshotService(db_path)
    start, end = date(2026, 5, 1), date(2026, 5, 31)
    cached = svc.snapshot_for_period(start, end, persist=True)
    assert cached["id"] is not None

    result = RuleApplicationService(db_path).apply_all(source="test", refresh_analytics=False)
    assert result.ok
    assert result.transactions_updated == 1

    fresh = svc.snapshot_for_period(start, end, persist=False)
    assert fresh["id"] is None, (
        "stale snapshot served after a bulk rules-apply — apply_all must record a "
        "'rules_apply' op and the freshness gate must include it"
    )


# --- LOW (2026-07-02 audit): Categories page must drop true_cost<=0 categories ---


def test_positive_true_cost_spending_keeps_only_positive_categories() -> None:
    """The production spend-picker helper the Categories page calls must keep only
    net-positive true-cost categories: a fully-offset (true_cost==0) or net-credit
    (true_cost<0) category is not spending and must be dropped, matching
    get_true_cost_by_category's true_cost>0 contract.

    Exercises the REAL code path (positive_true_cost_spending), not a re-implemented
    copy of the comprehension."""
    from ledger.ui.pages.categories import positive_true_cost_spending

    details = {
        "Dining": {"true_cost": Decimal("120.00"), "offsets": Decimal("0")},
        "Refunds": {"true_cost": Decimal("0.00"), "offsets": Decimal("50.00")},
        "NetCredit": {"true_cost": Decimal("-10.00"), "offsets": Decimal("60.00")},
    }
    spending = positive_true_cost_spending(details)

    # Only the net-positive category survives; its value is the true_cost.
    assert spending == {"Dining": Decimal("120.00")}
    assert "Refunds" not in spending  # fully offset (true_cost == 0)
    assert "NetCredit" not in spending  # net-credit (true_cost < 0)


def test_positive_true_cost_spending_empty_input() -> None:
    """No categories -> empty spend map (guards the no-spend 'st.info' branch)."""
    from ledger.ui.pages.categories import positive_true_cost_spending

    assert positive_true_cost_spending({}) == {}


def test_snapshot_previous_period_absent_at_earliest_boundary(tmp_path: Path) -> None:
    """F17: when report_start == earliest data date, the previous window inverts
    (previous_start > previous_end) and matches zero rows. Movement must report NO
    comparable prior period (change_pct 0, comparable False), not a spurious +100%."""
    from ledger.reporting.service import ReportSnapshotService

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_txn(db_path, "e1", date(2026, 5, 10))
    _insert_txn(db_path, "e2", date(2026, 5, 20))

    svc = ReportSnapshotService(db_path)
    result = svc.snapshot_for_period(date(2026, 5, 10), date(2026, 5, 20), persist=False)
    payload = result["payload"]

    spending = payload["income_spend_net_movement"]["spending"]
    assert spending["change_pct"] == 0, f"expected neutral pct, got {spending['change_pct']}"
    assert spending.get("comparable") is False
    # No prior period -> no spurious category drift either.
    assert payload["category_drift"] == []
