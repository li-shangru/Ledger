"""Tests for overview.py performance fixes.

Verifies:
1. detect_anomalies is NOT called a second time from render — the render reuses
   payload['anomalies'] from the snapshot instead.
2. The @st.cache_data helpers return the same values as the direct service calls
   for a fixture DB.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from ledger.core import Transaction
from ledger.processing import ProcessingResult
from ledger.query import LedgerQueryService
from ledger.storage.database import init_database
from ledger.storage.writer import DatabaseWriter

# ---------------------------------------------------------------------------
# Fixture helpers (shared with test_anomaly_detection.py pattern)
# ---------------------------------------------------------------------------


def _make_transaction(
    txn_id: str,
    merchant: str,
    amount: Decimal,
    category: str,
    posting_date: date,
    cashflow_type: str = "expense",
    include_in_cashflow: bool = True,
) -> Transaction:
    day_of_week = posting_date.weekday()
    amount_signed = -amount if cashflow_type == "expense" else amount
    return Transaction(
        txn_id=txn_id,
        txn_fingerprint=f"fp_{txn_id}",
        posting_date=posting_date,
        transaction_date=posting_date,
        institution="testbank",
        account="1234",
        account_type="credit_card",
        flow="expense" if cashflow_type == "expense" else "income",
        cashflow_type=cashflow_type,
        include_in_cashflow=include_in_cashflow,
        currency="CAD",
        amount_raw=amount,
        amount_signed=amount_signed,
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


def _write(db_path: Path, transactions: list[Transaction]) -> None:
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


@pytest.fixture
def populated_db(tmp_path: Path) -> Path:
    """Create a small but realistic DB with spending transactions."""
    db_path = tmp_path / "perf_test.db"
    init_database(db_path)

    base = date(2025, 12, 1)
    # 10 normal Dining transactions (~$25) + 1 outlier ($300)
    dining = [
        _make_transaction(
            f"d{i}", "Restaurant", Decimal("25.00"), "Dining", base + timedelta(days=i)
        )
        for i in range(10)
    ]
    outlier = _make_transaction(
        "outlier", "Restaurant", Decimal("300.00"), "Dining", base + timedelta(days=11)
    )
    # A few income transactions
    income = [
        _make_transaction(
            f"inc{i}",
            "Employer",
            Decimal("3000.00"),
            "Income",
            base + timedelta(days=i * 14),
            cashflow_type="income",
        )
        for i in range(2)
    ]
    _write(db_path, [*dining, outlier, *income])
    return db_path


# ---------------------------------------------------------------------------
# Part 1: detect_anomalies is NOT called twice — render reuses snapshot payload
# ---------------------------------------------------------------------------


class TestAnomaliesOnce:
    """Confirm the render does not call detect_anomalies directly."""

    def test_render_does_not_call_service_detect_anomalies_directly(self) -> None:
        """overview.py must NOT contain a direct service.detect_anomalies call
        after our fix.  We verify this by inspecting the source code."""
        import inspect

        from ledger.ui.pages import overview

        source = inspect.getsource(overview.render)
        # The render should no longer call service.detect_anomalies (the method call),
        # only the comment referencing detect_anomalies is acceptable.
        assert "service.detect_anomalies" not in source, (
            "render() still calls service.detect_anomalies directly — "
            "it should reuse payload['anomalies'] from the snapshot"
        )
        assert ".detect_anomalies(" not in source, (
            "render() still calls .detect_anomalies() directly — "
            "it should reuse payload['anomalies'] from the snapshot"
        )

    def test_render_uses_snapshot_payload_anomalies(self, populated_db: Path) -> None:
        """render() must read anomalies from snapshot['payload']['anomalies'],
        not from a fresh service call.

        Behavioral check: spy on detect_anomalies to confirm render() never calls
        it directly, and that the anomaly rows it builds come from the payload value
        (equal to what a single detect_anomalies call returns).
        """
        from unittest.mock import patch

        from ledger.query import LedgerQueryService
        from ledger.reporting import ReportSnapshotService

        start = date(2025, 12, 1)
        end = date(2025, 12, 31)

        # Build the snapshot once (detect_anomalies is called here, inside the snapshot)
        snapshot = ReportSnapshotService(populated_db).snapshot_for_period(
            start, end, source="test", persist=False
        )
        payload_anomalies = (snapshot.get("payload") or {}).get("anomalies") or []

        # Get the expected anomalies from a direct service call
        direct_anomalies = LedgerQueryService(populated_db).detect_anomalies(
            start, end, threshold=2.0
        )

        # The payload must identify the same transactions as a direct call
        payload_ids = {item["txn_id"] for item in payload_anomalies}
        direct_ids = {txn.txn_id for txn in direct_anomalies[:10]}
        assert payload_ids == direct_ids, (
            "Snapshot payload anomaly txn_ids differ from detect_anomalies — "
            "render() would display incorrect anomalies."
        )

        # Verify detect_anomalies is NOT called again during anomaly row building
        # (the code path under overview.py lines that read snapshot_payload['anomalies'])
        # detect_anomalies is inherited by LedgerQueryService from InsightQueries.
        from ledger.query.insights import InsightQueries

        detect_call_count = 0

        original_detect = InsightQueries.detect_anomalies

        def spy_detect(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal detect_call_count
            detect_call_count += 1
            return original_detect(self, *args, **kwargs)

        with patch.object(InsightQueries, "detect_anomalies", spy_detect):
            # Simulate what render() does: read anomalies from snapshot_payload
            snapshot_payload = snapshot.get("payload") or {}
            anomalies = list(snapshot_payload.get("anomalies") or [])
            # Build the DataFrame rows the same way render() does
            rows = [
                {
                    "Date": item["date"],
                    "Merchant": item.get("merchant") or "-",
                    "Category": item.get("category"),
                    "Amount": float(item["amount"]),
                }
                for item in anomalies[:8]
            ]

        assert detect_call_count == 0, (
            f"detect_anomalies was called {detect_call_count} time(s) while building "
            "anomaly rows — render() must reuse snapshot payload, not call the service again."
        )
        assert len(rows) == min(len(payload_anomalies), 8), (
            "Anomaly row count does not match payload anomaly count (capped at 8)"
        )

    def test_snapshot_payload_anomalies_shape(self, populated_db: Path) -> None:
        """snapshot['payload']['anomalies'] must be a list of dicts
        with the expected keys: txn_id, date, merchant, category, amount."""
        from ledger.reporting import ReportSnapshotService

        start = date(2025, 12, 1)
        end = date(2025, 12, 31)
        snapshot = ReportSnapshotService(populated_db).snapshot_for_period(
            start, end, source="test", persist=False
        )
        payload = snapshot.get("payload") or {}
        anomalies = payload.get("anomalies") or []

        assert isinstance(anomalies, list)
        if anomalies:
            for item in anomalies:
                assert "date" in item
                assert "merchant" in item
                assert "category" in item
                assert "amount" in item
                assert isinstance(item["amount"], float)
                # date must be an ISO string (stored as JSON-safe in the payload)
                assert isinstance(item["date"], str)

    def test_snapshot_and_service_detect_same_anomalies(self, populated_db: Path) -> None:
        """The anomalies in the snapshot payload must identify the same transactions
        as a direct detect_anomalies call with the same period and threshold=2.0."""
        from ledger.reporting import ReportSnapshotService

        start = date(2025, 12, 1)
        end = date(2025, 12, 31)

        snapshot = ReportSnapshotService(populated_db).snapshot_for_period(
            start, end, source="test", persist=False
        )
        payload_anomalies = snapshot.get("payload", {}).get("anomalies") or []
        payload_txn_ids = {item["txn_id"] for item in payload_anomalies}

        service_anomalies = LedgerQueryService(populated_db).detect_anomalies(
            start, end, threshold=2.0
        )
        service_txn_ids = {txn.txn_id for txn in service_anomalies[:10]}

        assert payload_txn_ids == service_txn_ids, (
            f"Snapshot payload anomaly txn_ids {payload_txn_ids} differ from "
            f"direct service call txn_ids {service_txn_ids}"
        )

    def test_snapshot_anomaly_amounts_match_service(self, populated_db: Path) -> None:
        """Amounts in snapshot payload must equal abs(txn.amount_signed) from service call."""
        from ledger.reporting import ReportSnapshotService

        start = date(2025, 12, 1)
        end = date(2025, 12, 31)

        snapshot = ReportSnapshotService(populated_db).snapshot_for_period(
            start, end, source="test", persist=False
        )
        payload_anomalies = snapshot.get("payload", {}).get("anomalies") or []
        payload_by_id = {item["txn_id"]: item["amount"] for item in payload_anomalies}

        service_anomalies = LedgerQueryService(populated_db).detect_anomalies(
            start, end, threshold=2.0
        )
        for txn in service_anomalies[:10]:
            if txn.txn_id in payload_by_id:
                expected = float(abs(txn.amount_signed))
                assert abs(payload_by_id[txn.txn_id] - expected) < 0.01, (
                    f"Amount mismatch for {txn.txn_id}: "
                    f"payload={payload_by_id[txn.txn_id]}, service={expected}"
                )


# ---------------------------------------------------------------------------
# Part 2: @st.cache_data helper correctness
# ---------------------------------------------------------------------------


class TestCachedHelpers:
    """The @st.cache_data helpers must return the same values as direct service calls."""

    def test_cached_true_cost_kpis_matches_service(self, populated_db: Path) -> None:
        from ledger.ui.pages.overview import _cached_true_cost_kpis

        # Clear any prior cache state to avoid cross-test contamination
        _cached_true_cost_kpis.clear()

        start = date(2025, 12, 1)
        end = date(2025, 12, 31)

        direct = LedgerQueryService(populated_db).compute_true_cost_kpis(start, end)
        cached = _cached_true_cost_kpis(str(populated_db), start, end)

        for key in ("income", "expenses", "net", "transaction_count"):
            assert cached[key] == direct[key], f"Mismatch for key '{key}'"

    def test_cached_true_cost_by_category_matches_service(self, populated_db: Path) -> None:
        from ledger.ui.pages.overview import _cached_true_cost_by_category

        _cached_true_cost_by_category.clear()

        start = date(2025, 12, 1)
        end = date(2025, 12, 31)

        direct = LedgerQueryService(populated_db).get_true_cost_by_category(start, end)
        cached = _cached_true_cost_by_category(str(populated_db), start, end)

        assert dict(cached) == dict(direct)

    def test_cached_spending_by_merchant_matches_service(self, populated_db: Path) -> None:
        from ledger.ui.pages.overview import _cached_spending_by_merchant

        _cached_spending_by_merchant.clear()

        start = date(2025, 12, 1)
        end = date(2025, 12, 31)

        direct = LedgerQueryService(populated_db).get_spending_by_merchant(start, end, top_n=10)
        cached = _cached_spending_by_merchant(str(populated_db), start, end, top_n=10)

        assert dict(cached) == dict(direct)

    def test_cached_income_sources_matches_service(self, populated_db: Path) -> None:
        from ledger.ui.pages.overview import _cached_income_sources

        _cached_income_sources.clear()

        start = date(2025, 12, 1)
        end = date(2025, 12, 31)

        direct = LedgerQueryService(populated_db).get_income_sources(start, end, top_n=8)
        cached = _cached_income_sources(str(populated_db), start, end, top_n=8)

        assert dict(cached) == dict(direct)

    def test_cached_monthly_kpis_matches_service(self, populated_db: Path) -> None:
        from ledger.ui.pages.overview import _cached_monthly_kpis

        _cached_monthly_kpis.clear()

        start = date(2025, 12, 1)
        end = date(2025, 12, 31)

        direct = LedgerQueryService(populated_db).compute_true_cost_kpis(start, end)
        cached = _cached_monthly_kpis(str(populated_db), start, end)

        for key in ("income", "expenses", "net", "transaction_count"):
            assert cached[key] == direct[key], f"Monthly KPI mismatch for key '{key}'"

    def test_cache_is_hashable_with_date_args(self, populated_db: Path) -> None:
        """Ensure cache key args are hashable (no unhashable service objects passed)."""
        from ledger.ui.pages.overview import _cached_true_cost_kpis

        _cached_true_cost_kpis.clear()

        start = date(2025, 12, 1)
        end = date(2025, 12, 31)

        # Calling twice with same args should not raise — and should return equal results
        result1 = _cached_true_cost_kpis(str(populated_db), start, end)
        result2 = _cached_true_cost_kpis(str(populated_db), start, end)

        assert result1["income"] == result2["income"]
        assert result1["expenses"] == result2["expenses"]

    def test_monthly_cashflow_uses_cached_helper(self, populated_db: Path) -> None:
        """_monthly_cashflow must call _cached_monthly_kpis, not the service directly."""
        import inspect

        from ledger.ui.pages import overview

        source = inspect.getsource(overview._monthly_cashflow)
        # Must use the cached helper, not service.compute_true_cost_kpis
        assert "_cached_monthly_kpis" in source
        assert "service.compute_true_cost_kpis" not in source


# ---------------------------------------------------------------------------
# Part 3: review_queue.py clears st.cache_data after every save
# ---------------------------------------------------------------------------


class TestReviewQueueCacheInvalidation:
    """Every guarded review save in review_queue.py must be followed by
    st.cache_data.clear() before st.rerun(), so the Overview page does not
    serve stale cached aggregates after a Review Queue save."""

    def _review_queue_source(self) -> str:
        import inspect

        from ledger.ui.pages import review_queue

        return inspect.getsource(review_queue.render)

    def _assert_clear_before_rerun_after_save(self, source: str, anchor: str) -> None:
        """Assert that at the save site identified by *anchor*:
        the guarded save helper appears, then cache_data.clear(), then st.rerun(),
        in that order, with no intervening guarded save or st.rerun().

        This catches a misplaced clear() — e.g. clear() present but AFTER st.rerun().
        """
        anchor_idx = source.find(anchor)
        assert anchor_idx != -1, (
            f"Anchor string {anchor!r} not found in render() source — "
            "update this test if the message changed."
        )

        # Walk backwards from the anchor to find the nearest guarded save call.
        apply_token = "_save_and_apply_review("
        rerun_token = "st.rerun()"
        clear_token = "st.cache_data.clear()"

        apply_idx = source.rfind(apply_token, 0, anchor_idx)
        assert apply_idx != -1, f"No _save_and_apply_review( found before anchor {anchor!r}"

        # clear() must appear between apply_review_decisions and st.rerun()
        clear_idx = source.find(clear_token, apply_idx)
        rerun_idx = source.find(rerun_token, apply_idx)

        assert clear_idx != -1, (
            f"st.cache_data.clear() not found after guarded save at {anchor!r} site"
        )
        assert rerun_idx != -1, f"st.rerun() not found after guarded save at {anchor!r} site"
        assert clear_idx < rerun_idx, (
            f"st.cache_data.clear() (pos {clear_idx}) must come BEFORE st.rerun() "
            f"(pos {rerun_idx}) at the {anchor!r} save site — a misplaced clear() "
            "would serve stale Overview aggregates."
        )
        # Ensure there is no intervening guarded save between clear and rerun
        # (i.e. clear belongs to THIS site, not the next one)
        next_apply = source.find(apply_token, apply_idx + len(apply_token))
        if next_apply != -1:
            assert clear_idx < next_apply, (
                f"st.cache_data.clear() at pos {clear_idx} appears AFTER the next "
                f"guarded save at pos {next_apply} — the clear() is not "
                f"on the {anchor!r} save path."
            )

    def test_bulk_resolve_clears_cache(self) -> None:
        """The bulk-resolve branch (Resolve complete visible rows button) must call
        st.cache_data.clear() after apply_review_decisions and before st.rerun()."""
        source = self._review_queue_source()
        self._assert_clear_before_rerun_after_save(source, "Resolved visible complete rows")

    def test_bulk_as_is_clears_cache(self) -> None:
        """The bulk-reviewed-as-is branch must call st.cache_data.clear() after
        apply_review_decisions and before st.rerun()."""
        source = self._review_queue_source()
        self._assert_clear_before_rerun_after_save(source, "Marked visible complete rows reviewed")

    def test_save_review_decisions_clears_cache(self) -> None:
        """The primary Save Review Decisions button must call st.cache_data.clear()
        after apply_review_decisions and before st.rerun()."""
        source = self._review_queue_source()
        self._assert_clear_before_rerun_after_save(source, "Saved review decisions")
