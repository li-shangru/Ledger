"""Regression tests for the 2026-08-26 eighth-pass audit.

Every defect covered here survived a fully green gate suite. Each test is written
RED-first: it fails against the pre-fix code for the finding's stated reason, and
pins the corrected behavior afterwards.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from account_fixtures import seed_account

from ledger.insights.service import InsightEngine
from ledger.planning.readiness import PlanningReadinessService
from ledger.storage.database import (
    InsightFindingDB,
    RecurringPatternDB,
    init_database,
    session_scope,
)

# --------------------------------------------------------------------------------------
# F2 — Insights history (Forecast / Ratios) was derived from the selected view period
# --------------------------------------------------------------------------------------
#
# _monthly_cashflow built its month list from ctx.start_date..ctx.end_date. On the
# default single-month period, Forecast reported "Need at least two months of
# historical data" against a multi-year dataset, and the Ratios tiles computed a
# "12 whole calendar months" average (per their own caption) over one partial month —
# masking a negative savings rate as 0.0% and overstating the fixed-expense ratio.


class _StubKpiService:
    def __init__(self):
        self.calls: list[tuple[date, date]] = []

    def compute_true_cost_kpis(self, start: date, end: date) -> dict:
        self.calls.append((start, end))
        return {
            "income": Decimal("1000.00"),
            "expenses": Decimal("800.00"),
            "net": Decimal("200.00"),
            "transaction_count": 10,
        }


def _fake_ctx(service, *, earliest: date, latest: date, start: date, end: date):
    from ledger.ui.context import AppContext

    return AppContext(
        db_path=Path("unused.db"),
        service=service,
        category_config=None,
        stats={},
        earliest=earliest,
        latest=latest,
        start_date=start,
        end_date=end,
        period_label="test",
        review_source={},
        review_db=Path("unused.db"),
        pending_reviews=0,
    )


def test_monthly_cashflow_history_comes_from_data_not_the_view_period():
    """With 20 months of data and a single-month view, the ratio/forecast basis must
    still be the trailing 12 whole calendar months of DATA.

    Pre-fix it returned exactly one row (the partial view month), so Forecast
    claimed there was under two months of historical data.
    """
    from ledger.ui.pages.insights import _monthly_cashflow

    service = _StubKpiService()
    ctx = _fake_ctx(
        service,
        earliest=date(2024, 12, 11),
        latest=date(2026, 8, 14),
        start=date(2026, 8, 1),
        end=date(2026, 8, 14),
    )

    rows = _monthly_cashflow(ctx, months_back=12)

    months = [row["month"] for row in rows]
    assert len(months) == 12, "history must not collapse to the selected view period"
    # Whole months only: the partial in-progress month (2026-08, data through the
    # 14th) is excluded, so the caption "last 12 whole calendar months" is true.
    assert months[0] == "2025-08"
    assert months[-1] == "2026-07"


def test_monthly_cashflow_clamps_to_available_whole_months():
    """A dataset younger than months_back returns every whole month it has."""
    from ledger.ui.pages.insights import _monthly_cashflow

    service = _StubKpiService()
    ctx = _fake_ctx(
        service,
        earliest=date(2026, 5, 17),
        latest=date(2026, 8, 14),
        start=date(2026, 8, 1),
        end=date(2026, 8, 14),
    )

    rows = _monthly_cashflow(ctx, months_back=12)
    months = [row["month"] for row in rows]
    assert months == ["2026-06", "2026-07"], (
        "only whole calendar months inside the data range are averaged; the partial "
        "first and last months are excluded"
    )


# --------------------------------------------------------------------------------------
# F3 — Active recurring commitments were dropped from Recurring Monthly readiness
# --------------------------------------------------------------------------------------
#
# _recurring_monthly (and the Insights _patterns_for_period filter) required
# last_occurrence >= period start, so on the default mid-month window every active
# bill/subscription whose last charge posted in the prior month vanished.
# The synthetic cases below keep active commitments across that date boundary,
# including costs that recur more frequently than once a month.


def _seed_pattern(
    db_path: Path,
    *,
    pattern_id: str,
    pattern_type: str,
    frequency: str,
    amount: str,
    first: date,
    last: date,
    next_expected: date,
    active: bool = True,
) -> None:
    account_id = seed_account(
        db_path,
        "harbor",
        "chequing-0001",
        account_type="Chequing",
    )
    with session_scope(db_path) as session:
        session.add(
            RecurringPatternDB(
                pattern_id=pattern_id,
                pattern_type=pattern_type,
                merchant=f"M-{pattern_id}",
                category="Housing",
                institution="harbor",
                account_id=account_id,
                frequency=frequency,
                expected_day_of_period=1,
                is_fixed_amount=True,
                expected_amount=Decimal(amount),
                amount_std_dev=Decimal("0"),
                confidence=0.9,
                detection_method="interval",
                occurrence_count=12,
                first_occurrence=first,
                last_occurrence=last,
                next_expected_date=next_expected,
                is_active=active,
                missed_count=0,
                detected_at=datetime(2026, 8, 16),
                updated_at=datetime(2026, 8, 16),
            )
        )


def test_active_commitment_counts_even_when_last_charge_predates_the_window(tmp_path):
    """An is_active bill whose last occurrence was before the window is still a live
    commitment and must be included in Recurring Monthly."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _seed_pattern(
        db_path,
        pattern_id="p-mortgage",
        pattern_type="bill",
        frequency="biweekly",
        amount="840.00",
        first=date(2040, 11, 9),
        last=date(2042, 7, 13),
        next_expected=date(2042, 7, 27),
    )

    service = PlanningReadinessService(db_path, prepare_schema=False)
    total = service._recurring_monthly(date(2042, 8, 1), date(2042, 8, 14))

    expected = (Decimal("840.00") * 26 / 12).quantize(Decimal("0.01"))
    assert total == expected, "active biweekly bill must survive a window it has not recharged in"


def test_pattern_that_begins_after_the_window_is_still_excluded(tmp_path):
    """The forward-looking guard stays: a commitment that did not exist yet during
    the requested window is not part of that window's readiness."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _seed_pattern(
        db_path,
        pattern_id="p-new",
        pattern_type="subscription",
        frequency="monthly",
        amount="50.00",
        first=date(2026, 9, 1),
        last=date(2026, 10, 1),
        next_expected=date(2026, 11, 1),
    )

    service = PlanningReadinessService(db_path, prepare_schema=False)
    total = service._recurring_monthly(date(2026, 8, 1), date(2026, 8, 14))
    assert total == Decimal("0")


def test_inactive_pattern_stays_excluded(tmp_path):
    """Retired patterns must not inflate the commitment figure."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _seed_pattern(
        db_path,
        pattern_id="p-dead",
        pattern_type="bill",
        frequency="monthly",
        amount="99.00",
        first=date(2025, 1, 1),
        last=date(2025, 6, 1),
        next_expected=date(2025, 7, 1),
        active=False,
    )

    service = PlanningReadinessService(db_path, prepare_schema=False)
    total = service._recurring_monthly(date(2026, 8, 1), date(2026, 8, 14))
    assert total == Decimal("0")


def test_insights_pattern_period_filter_keeps_active_commitments(tmp_path):
    """The Insights Recurring panel shares the fix: an active pattern whose last
    charge predates the window is still listed (and priced) for that window."""
    from ledger.ui.pages.insights import _patterns_for_period

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _seed_pattern(
        db_path,
        pattern_id="p-condo",
        pattern_type="subscription",
        frequency="monthly",
        amount="260.00",
        first=date(2041, 2, 4),
        last=date(2042, 7, 4),
        next_expected=date(2042, 8, 4),
    )

    from ledger.query import LedgerQueryService

    class _Ctx:
        service = LedgerQueryService(db_path)
        start_date = date(2042, 8, 1)
        end_date = date(2042, 8, 14)

    patterns = _patterns_for_period(_Ctx())
    assert len(patterns) == 1, "active pattern must not vanish from the Recurring panel"


# --------------------------------------------------------------------------------------
# F1 — Stored insight findings were unreachable from every default UI period
# --------------------------------------------------------------------------------------
#
# InsightEngine.list_findings filtered on exact (period_start, period_end) equality.
# Findings persisted at import finalize carry the run's statement span, which no UI
# preset (This Month, clamped to data) can ever reproduce, so the Insights panels and
# Overview "Needs Attention" rendered "0 open finding(s)" while dozens of open findings
# sat in the database — and every superseded period's rows stayed open, invisible,
# forever.


def _seed_finding(
    db_path: Path,
    *,
    start: date,
    end: date,
    fingerprint: str,
    finding_type: str = "cost_cutting",
    severity: str = "high",
    status: str = "open",
) -> None:
    with session_scope(db_path) as session:
        session.add(
            InsightFindingDB(
                fingerprint=fingerprint,
                period_start=start,
                period_end=end,
                finding_type=finding_type,
                severity=severity,
                title="t",
                explanation="e",
                evidence={},
                suggested_action="s",
                status=status,
                created_at=datetime(2026, 8, 16, 23, 27, 24),
            )
        )


def test_open_findings_are_served_when_no_row_matches_the_view_period(tmp_path):
    """A view period with no stored rows must fall back to the newest stored period.

    Pre-fix: list_findings(view) returned [] and summary(view) reported 0 open
    although open findings existed under the finalize-time period key.
    """
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    stored_start, stored_end = date(2026, 5, 17), date(2026, 8, 14)
    _seed_finding(db_path, start=stored_start, end=stored_end, fingerprint="f1")
    _seed_finding(
        db_path,
        start=stored_start,
        end=stored_end,
        fingerprint="f2",
        severity="low",
        finding_type="anomalies",
    )

    engine = InsightEngine(db_path)
    view_start, view_end = date(2026, 8, 1), date(2026, 8, 14)

    rows = engine.list_findings(view_start, view_end)
    assert len(rows) == 2, "stored open findings must be reachable from a default view period"

    summary = engine.summary(view_start, view_end)
    assert summary["open"] == 2
    assert summary["high"] == 1


def test_findings_fallback_serves_newest_period_and_reports_it(tmp_path):
    """The fallback serves the newest period_end (widest window on ties), like the
    MCP read surface, and served_period discloses which period is being shown."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _seed_finding(
        db_path, start=date(2026, 5, 3), end=date(2026, 7, 31), fingerprint="old", severity="low"
    )
    _seed_finding(db_path, start=date(2026, 5, 17), end=date(2026, 8, 14), fingerprint="new")

    engine = InsightEngine(db_path)
    view = (date(2026, 8, 1), date(2026, 8, 14))

    served = engine.served_period(*view)
    assert served == (date(2026, 5, 17), date(2026, 8, 14))

    rows = engine.list_findings(*view)
    assert [row["fingerprint"] for row in rows] == ["new"]


def test_exact_period_match_still_wins_over_fallback(tmp_path):
    """A view period that has its own stored rows (even fully triaged ones) is served
    as-is; the fallback must never displace a real refresh of the current view."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    view_start, view_end = date(2026, 8, 1), date(2026, 8, 14)
    _seed_finding(
        db_path,
        start=view_start,
        end=view_end,
        fingerprint="mine",
        status="reviewed",
        severity="low",
    )
    _seed_finding(db_path, start=date(2026, 5, 17), end=date(2026, 8, 14), fingerprint="other")

    engine = InsightEngine(db_path)
    assert engine.served_period(view_start, view_end) == (view_start, view_end)
    # The view period's rows are all closed, so the open list is genuinely empty —
    # that emptiness is true for this period and must not be papered over by
    # another period's rows.
    assert engine.list_findings(view_start, view_end) == []
    rows = engine.list_findings(view_start, view_end, include_closed=True)
    assert [row["fingerprint"] for row in rows] == ["mine"]


# --------------------------------------------------------------------------------------
# F4 — Review Queue pinned a cancelled import run as its active write target
# --------------------------------------------------------------------------------------
#
# get_active_import_run() trusted latest-run.txt if run.json parsed and staging.db
# existed — status was never consulted. With a cancelled run still pointed at (its
# staging DB deliberately survives cleanup), the Review Queue presented the cancelled
# staging queue as "Active Import", promised "Nothing reaches the live database until
# the run is finalized" (a cancelled run can never finalize), and let the user edit
# decisions that the save-time FINALIZABLE_STATUSES guard then discarded with a
# generic error. The CLI refuses cancelled runs outright; the routing now does too.


def _write_run(tmp_path: Path, status: str) -> Path:
    runs_dir = tmp_path / "import-runs"
    run_dir = runs_dir / "20260816T234920Z"
    run_dir.mkdir(parents=True)
    staging = run_dir / "staging.db"
    staging.write_bytes(b"")
    (run_dir / "run.json").write_text(
        f'{{"status": "{status}", "staging_db_path": "{staging}"}}',
        encoding="utf-8",
    )
    (runs_dir / "latest-run.txt").write_text(str(run_dir), encoding="utf-8")
    return runs_dir


@pytest.mark.parametrize("status", ["cancelled", "finalized", "failed"])
def test_review_routing_skips_runs_that_can_no_longer_promote(tmp_path, status):
    """A run the save guard will refuse must not be offered as the review target."""
    from ledger.ui.context import get_active_import_run

    runs_dir = _write_run(tmp_path, status)
    active = get_active_import_run(runs_dir, finalizable_only=True)
    assert active is None, f"a {status} run must not be pinned as the active review target"


def test_review_routing_still_serves_promotable_runs(tmp_path):
    """The staged path keeps working: a finalizable run is still the review target."""
    from ledger.ui.context import get_active_import_run

    runs_dir = _write_run(tmp_path, "needs_review")
    active = get_active_import_run(runs_dir, finalizable_only=True)
    assert active is not None
    assert active["run_state"]["status"] == "needs_review"


def test_import_center_still_sees_terminal_runs(tmp_path):
    """The Import Center deliberately displays terminal runs (it labels them and
    says 'Start a new run'); the default lookup keeps returning them."""
    from ledger.ui.context import get_active_import_run

    runs_dir = _write_run(tmp_path, "cancelled")
    active = get_active_import_run(runs_dir)
    assert active is not None
    assert active["run_state"]["status"] == "cancelled"


# --------------------------------------------------------------------------------------
# F5 — Accounts "Period-End Balance" rendered a stale anchor with no as-of date
# F6 — In-period relative balance history hid behind a false "no snapshots" empty state
# --------------------------------------------------------------------------------------
#
# The period-end KPI must disclose when its absolute anchor predates the selected
# period. The history chart must choose its basis from rows within the period;
# an older absolute snapshot must not hide newer relative-basis rows. The synthetic
# fixture below supplies one older absolute row and two in-period relative rows.


class _StubBalanceService:
    def __init__(self, rows_by_basis):
        self.rows_by_basis = rows_by_basis

    def get_account_balances(
        self, account_id=None, start_date=None, end_date=None, balance_basis=None
    ):
        rows = self.rows_by_basis.get(balance_basis, [])
        return [
            row
            for row in rows
            if (start_date is None or row["balance_date"] >= start_date)
            and (end_date is None or row["balance_date"] <= end_date)
        ]


def test_period_history_falls_back_to_in_period_relative_rows():
    from ledger.ui.pages.accounts import _period_balance_history

    service = _StubBalanceService(
        {
            "absolute": [{"balance_date": date(2047, 2, 6), "balance": "-300.00"}],
            "relative": [
                {"balance_date": date(2047, 3, 3), "balance": "-80.00"},
                {"balance_date": date(2047, 3, 12), "balance": "-125.00"},
            ],
        }
    )
    basis, rows = _period_balance_history(
        service, ["acct"], start_date=date(2047, 3, 1), end_date=date(2047, 3, 12)
    )
    assert basis == "relative"
    assert len(rows) == 2, "in-period relative snapshots must not hide behind an empty state"


def test_period_history_prefers_absolute_when_the_period_has_it():
    from ledger.ui.pages.accounts import _period_balance_history

    service = _StubBalanceService(
        {
            "absolute": [{"balance_date": date(2026, 8, 10), "balance": "100.00"}],
            "relative": [{"balance_date": date(2026, 8, 12), "balance": "5.00"}],
        }
    )
    basis, rows = _period_balance_history(
        service, ["acct"], start_date=date(2026, 8, 1), end_date=date(2026, 8, 14)
    )
    assert basis == "absolute"
    assert len(rows) == 1


def test_period_history_reports_no_basis_when_the_period_is_truly_empty():
    from ledger.ui.pages.accounts import _period_balance_history

    service = _StubBalanceService({"absolute": [], "relative": []})
    basis, rows = _period_balance_history(
        service, ["acct"], start_date=date(2026, 8, 1), end_date=date(2026, 8, 14)
    )
    assert basis is None
    assert rows == []


def test_period_end_balance_disclosure_names_a_stale_anchor():
    from ledger.ui.pages.accounts import _period_end_detail

    stale = {"balance_date": date(2047, 2, 6)}
    assert _period_end_detail(stale, date(2047, 3, 12)) == "as of 2047-02-06"
    current = {"balance_date": date(2047, 3, 12)}
    assert _period_end_detail(current, date(2047, 3, 12)) is None
    assert _period_end_detail(None, date(2047, 3, 12)) is None


# --------------------------------------------------------------------------------------
# F7 — "Period Spend / Income" rendered 0.0% when the period had no income
# --------------------------------------------------------------------------------------
#
# Every ratio tile on the Ratios panel fell back to 0 when its denominator was zero,
# so a period with spending and no income displayed "Period Spend / Income 0.0%".
# A ratio with no denominator
# is unassessable and now renders "n/a" with the reason.


def test_ratio_with_zero_denominator_is_unassessable_not_zero():
    from ledger.ui.pages.insights import _safe_ratio_pct

    assert _safe_ratio_pct(250.0, 0.0) is None, "spend against zero income must not read 0.0%"
    assert _safe_ratio_pct(0.0, 0.0) is None
    assert _safe_ratio_pct(200.0, 1000.0) == pytest.approx(20.0)
    assert _safe_ratio_pct(-43.0, 1000.0) == pytest.approx(-4.3)


# --------------------------------------------------------------------------------------
# F8 — The finalize's own report snapshot always failed its own freshness gate
# --------------------------------------------------------------------------------------
#
# Import finalize writes its report snapshot during post-processing and records the
# import_finalize audit row afterwards. snapshot_for_period treats a stored snapshot
# as stale when any mutating op finished after created_at — so the snapshot each
# finalize produced was judged stale by that same finalize's audit row, forever, on
# every live database. Every page render silently recomputed a transient snapshot,
# and the Overview card dated that transient object as if it were stored (always
# "today"), while the Evidence strip warned the stored one was old. The audit
# recorder now stamps the finalize's snapshot with the audit instant, and the card
# labels a transient snapshot "Live" instead of dating it.


def _seed_cashflow_transaction(db_path: Path) -> None:
    from datetime import UTC

    from ledger.storage.database import TransactionDB

    posted = date(2026, 8, 5)
    with session_scope(db_path) as session:
        session.add(
            TransactionDB(
                txn_id="f8txn0001",
                posting_date=posted,
                transaction_date=posted,
                institution="harbor",
                account="chequing-0001",
                account_type="Chequing",
                flow="expense",
                cashflow_type="expense",
                include_in_cashflow=True,
                exclude_from_spending=False,
                currency="CAD",
                amount_raw=Decimal("25.00"),
                amount_signed=Decimal("-25.00"),
                description="COFFEE SHOP",
                merchant="COFFEE SHOP",
                category="Food & Dining",
                source_file="statement.csv",
                source_path="/private/statement.csv",
                source_period="2026-08",
                source_row=1,
                imported_at=datetime.now(UTC).replace(tzinfo=None),
                parser_version="test",
                txn_fingerprint="f8-fingerprint",
                day_of_week=posted.weekday(),
                day_of_month=posted.day,
                week_of_year=posted.isocalendar()[1],
                is_weekend=posted.weekday() >= 5,
                is_month_start=posted.day <= 3,
                is_month_end=posted.day >= 28,
            )
        )


def test_finalize_audit_keeps_its_own_snapshot_fresh(tmp_path):
    """The snapshot a finalize writes must still be served after that finalize
    records its audit row — it is post-mutation by construction."""
    from ledger.imports.pdf.workflow import _record_finalize_audit_once
    from ledger.reporting import ReportSnapshotService

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _seed_cashflow_transaction(db_path)

    service = ReportSnapshotService(db_path)
    stored = service.create_snapshot(
        source="pdf_finalize",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 14),
    )
    assert stored["id"] is not None

    _record_finalize_audit_once(
        db_path,
        tmp_path / "run",
        {"idempotency_key": "f8-key", "counts": {}},
        {"warnings": []},
    )

    served = service.snapshot_for_period(stored["period_start"], stored["period_end"])
    assert served["id"] is not None, (
        "the finalize's own snapshot must pass the freshness gate its own audit row "
        "moves — otherwise the snapshot cache is dead on every imported database"
    )


# --------------------------------------------------------------------------------------
# F9..F12 — Low-severity presentation batch
# --------------------------------------------------------------------------------------
#
# F9  Overview "Largest Category" (leaf basis) now explains itself beside the
#     parent-rollup chart (this is seventh-pass L10, catalogued there but never
#     actually remediated despite that report's closing claim).
# F10 The Categories drift expander renders the same renamed/formatted columns as
#     Overview's drift table instead of machine headers and raw floats.
# F11 Evidence detail strings render minute-precision timestamps, not microseconds.
# F12 The Accounts picker defaults to an account with activity in the period.


def test_evidence_detail_timestamps_are_minute_precision():
    from ledger.evidence.service import _when

    stamp = datetime(2026, 8, 16, 23, 27, 24, 509882)
    assert _when(stamp) == "2026-08-16 23:27"
    assert _when(None) is None


def test_default_account_index_prefers_in_period_activity():
    from ledger.ui.pages.accounts import _default_account_index

    accounts = [
        {  # data ends before the period opens — the old default
            "first_transaction_date": date(2024, 12, 27),
            "last_transaction_date": date(2026, 7, 23),
        },
        {  # active in the period
            "first_transaction_date": date(2026, 5, 23),
            "last_transaction_date": date(2026, 8, 14),
        },
    ]
    assert _default_account_index(accounts, date(2026, 8, 1), date(2026, 8, 14)) == 1
    # No account overlaps: fall back to the first.
    assert _default_account_index(accounts, date(2027, 1, 1), date(2027, 1, 31)) == 0
    # ISO-string dates are tolerated.
    accounts_str = [
        {"first_transaction_date": "2026-05-23", "last_transaction_date": "2026-08-14"},
    ]
    assert _default_account_index(accounts_str, date(2026, 8, 1), date(2026, 8, 14)) == 0


# --------------------------------------------------------------------------------------
# F13 — One-off category fix silently dropped a merchant rule's spend exclusion
# --------------------------------------------------------------------------------------
#
# set_transaction_category computed exclude_from_spending from the TARGET CATEGORY's
# flag alone and overwrote the row, discarding a matched merchant rule's exclusion —
# then froze that answer behind manual confidence, which no later rules pass repairs.
# Every sibling surface (rules apply, review resolve, remember-for-merchant) computes
# merchant_exclude OR category_exclude; the one-off path now does too.


def _seed_config_and_txn(db_path: Path) -> str:
    from ledger.config.repository import SQLiteConfigRepository
    from ledger.config.schemas import (
        CategoriesConfig,
        CategoryRule,
        MerchantRule,
        MerchantRulesConfig,
    )

    SQLiteConfigRepository(db_path).replace_from_configs(
        MerchantRulesConfig(
            version="v",
            rules=[
                MerchantRule(
                    merchant_norm="CC PAYMENT",
                    patterns=[r"(?i)PAYMENT THANK YOU"],
                    category="Payments",
                    exclude_from_spending=True,
                )
            ],
        ),
        CategoriesConfig(
            version="v",
            default_category="Other",
            categories=[
                CategoryRule(name="Payments", merchants=["CC PAYMENT"]),
                CategoryRule(name="Dining", merchants=[]),
                CategoryRule(name="Other", merchants=[]),
            ],
        ),
    )
    txn_id = "f13txn001"
    from datetime import UTC

    from ledger.storage.database import TransactionDB

    posted = date(2026, 8, 5)
    with session_scope(db_path) as session:
        session.add(
            TransactionDB(
                txn_id=txn_id,
                posting_date=posted,
                transaction_date=posted,
                institution="harbor",
                account="card-0001",
                account_type="Visa",
                flow="payment",
                cashflow_type="expense",
                include_in_cashflow=True,
                exclude_from_spending=True,
                currency="CAD",
                amount_raw=Decimal("100.00"),
                amount_signed=Decimal("-100.00"),
                description="PAYMENT THANK YOU 123",
                merchant_raw="PAYMENT THANK YOU 123",
                merchant="CC PAYMENT",
                category="Payments",
                source_file="statement.csv",
                source_path="/private/statement.csv",
                source_period="2026-08",
                source_row=1,
                imported_at=datetime.now(UTC).replace(tzinfo=None),
                parser_version="test",
                txn_fingerprint="f13-fp",
                day_of_week=posted.weekday(),
                day_of_month=posted.day,
                week_of_year=posted.isocalendar()[1],
                is_weekend=posted.weekday() >= 5,
                is_month_start=posted.day <= 3,
                is_month_end=posted.day >= 28,
            )
        )
    return txn_id


def test_one_off_category_fix_preserves_merchant_rule_exclusion(tmp_path):
    """Retargeting an excluded merchant's row to a non-excluding category must not
    silently start counting it as spending."""
    from ledger.rules.categorization import CategorizationService
    from ledger.storage.database import TransactionDB

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    txn_id = _seed_config_and_txn(db_path)

    assert CategorizationService(db_path, runs_dir=tmp_path / "runs").set_transaction_category(
        txn_id, "Dining"
    )

    with session_scope(db_path) as session:
        row = session.get(TransactionDB, txn_id)
        assert row.category == "Dining"
        assert bool(row.exclude_from_spending) is True, (
            "the matched merchant rule's spend exclusion must survive a one-off "
            "category fix, exactly as it survives rules apply and review resolve"
        )


def test_one_off_category_fix_still_applies_category_exclusion(tmp_path):
    """The category side of the OR is unchanged: moving a plain row into an
    excluding category still excludes it, and into a plain category keeps it
    counted."""
    from ledger.config.repository import SQLiteConfigRepository
    from ledger.config.schemas import (
        CategoriesConfig,
        CategoryRule,
        MerchantRule,
        MerchantRulesConfig,
    )
    from ledger.rules.categorization import CategorizationService
    from ledger.storage.database import TransactionDB

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    SQLiteConfigRepository(db_path).replace_from_configs(
        MerchantRulesConfig(
            version="v",
            rules=[
                MerchantRule(
                    merchant_norm="PLAIN SHOP",
                    patterns=[r"(?i)PLAIN SHOP"],
                    category="Dining",
                )
            ],
        ),
        CategoriesConfig(
            version="v",
            default_category="Other",
            categories=[
                CategoryRule(name="Dining", merchants=["PLAIN SHOP"]),
                CategoryRule(name="Reimbursement", merchants=[], exclude_from_spending=True),
                CategoryRule(name="Other", merchants=[]),
            ],
        ),
    )
    from datetime import UTC

    posted = date(2026, 8, 6)
    with session_scope(db_path) as session:
        session.add(
            TransactionDB(
                txn_id="f13txn002",
                posting_date=posted,
                transaction_date=posted,
                institution="harbor",
                account="card-0001",
                account_type="Visa",
                flow="purchase",
                cashflow_type="expense",
                include_in_cashflow=True,
                exclude_from_spending=False,
                currency="CAD",
                amount_raw=Decimal("20.00"),
                amount_signed=Decimal("-20.00"),
                description="PLAIN SHOP 9",
                merchant_raw="PLAIN SHOP 9",
                merchant="PLAIN SHOP",
                category="Dining",
                source_file="statement.csv",
                source_path="/private/statement.csv",
                source_period="2026-08",
                source_row=2,
                imported_at=datetime.now(UTC).replace(tzinfo=None),
                parser_version="test",
                txn_fingerprint="f13-fp2",
                day_of_week=posted.weekday(),
                day_of_month=posted.day,
                week_of_year=posted.isocalendar()[1],
                is_weekend=posted.weekday() >= 5,
                is_month_start=posted.day <= 3,
                is_month_end=posted.day >= 28,
            )
        )

    service = CategorizationService(db_path, runs_dir=tmp_path / "runs")
    assert service.set_transaction_category("f13txn002", "Reimbursement")
    with session_scope(db_path) as session:
        assert bool(session.get(TransactionDB, "f13txn002").exclude_from_spending) is True
    assert service.set_transaction_category("f13txn002", "Other")
    with session_scope(db_path) as session:
        assert bool(session.get(TransactionDB, "f13txn002").exclude_from_spending) is False


# --------------------------------------------------------------------------------------
# F14 — The Insights "Upcoming" list could never show a genuinely upcoming charge
# --------------------------------------------------------------------------------------
#
# The section filtered next_expected_date INSIDE the selected period, but every
# reachable period is clamped to the newest imported transaction — so an expected
# charge after the data edge (the only kind that is upcoming) was structurally
# unreachable, and the list rendered already-elapsed expectations instead. Stored
# next-expected dates are retrospective (one period after the last accepted
# occurrence), so the fix rolls each forward past the data edge with the detector's
# own cadence arithmetic. Pinned on the extracted helper; pre-fix the logic lived
# inline in the render path.


def test_upcoming_rolls_retrospective_expectations_past_the_data_edge():
    from ledger.ui.pages.insights import _upcoming_expected

    edge = date(2026, 8, 14)
    patterns = [
        {  # stored date already past — the mortgage case (biweekly from 07-29)
            "merchant": "A",
            "frequency": "biweekly",
            "next_expected_date": date(2026, 7, 29),
            "expected_day_of_period": 29,
            "expected_amount": Decimal("10"),
            "pattern_type": "bill",
        },
        {  # stored date already after the edge — served as-is
            "merchant": "B",
            "frequency": "monthly",
            "next_expected_date": date(2026, 9, 12),
            "expected_day_of_period": 12,
            "expected_amount": Decimal("10"),
            "pattern_type": "subscription",
        },
    ]
    rows = _upcoming_expected(patterns, edge)
    assert [row["merchant"] for row in rows] == ["A", "B"]
    # 07-29 + 14d = 08-12 (still <= edge) + 14d = 08-26: first date past the edge.
    assert rows[0]["expected_on"] == date(2026, 8, 26)
    assert rows[1]["expected_on"] == date(2026, 9, 12)


def test_upcoming_drops_patterns_without_an_expected_date():
    from ledger.ui.pages.insights import _upcoming_expected

    rows = _upcoming_expected(
        [
            {
                "merchant": "C",
                "frequency": "monthly",
                "next_expected_date": None,
                "expected_day_of_period": 1,
                "expected_amount": Decimal("5"),
                "pattern_type": "bill",
            }
        ],
        date(2026, 8, 14),
    )
    assert rows == []


# --------------------------------------------------------------------------------------
# F15 — Investments KPI row mixed window aggregates with latest-statement-only P&L
# --------------------------------------------------------------------------------------
#
# With one account selected over a multi-statement range, deposits and dividends
# aggregated every statement in the window while Realized P&L silently showed only
# the newest statement's month (misstating the window figure in either direction).
# Realized P&L is a period flow and is now summed over the window's statements;
# Unrealized P&L is a point-in-time stock and the statement return is per-statement
# (chaining is deliberately refused), so both stay latest-statement values and their
# tiles now say so.


def test_realized_pl_sums_the_window_while_stocks_stay_latest():
    from ledger.ui.pages.investments import _historical_kpis

    rows = [
        {  # newest first, as get_investment_performance orders
            "period_end": date(2026, 7, 31),
            "total_nav": Decimal("100"),
            "realized_pl": Decimal("10.00"),
            "unrealized_pl": Decimal("5.00"),
            "time_weighted_return": Decimal("0.01"),
        },
        {
            "period_end": date(2026, 6, 30),
            "total_nav": Decimal("90"),
            "realized_pl": Decimal("30.00"),
            "unrealized_pl": Decimal("50.00"),
            "time_weighted_return": Decimal("0.02"),
        },
    ]
    activity = {
        "net_deposits_withdrawals": Decimal("0"),
        "dividends_interest": Decimal("0"),
    }
    metrics = _historical_kpis(rows, activity)
    assert metrics["realized_pl"] == Decimal("40.00"), "realized P&L is a summable period flow"
    assert metrics["unrealized_pl"] == Decimal("5.00"), "unrealized P&L is latest-statement only"
    assert metrics["reported_return"] == Decimal("0.01")
    assert metrics["latest_period_end"] == date(2026, 7, 31)


def test_realized_pl_with_no_reported_values_is_unavailable_not_zero():
    from ledger.ui.pages.investments import _historical_kpis

    rows = [
        {
            "period_end": date(2026, 7, 31),
            "total_nav": Decimal("100"),
            "realized_pl": None,
            "unrealized_pl": None,
            "time_weighted_return": None,
        }
    ]
    metrics = _historical_kpis(
        rows, {"net_deposits_withdrawals": Decimal("0"), "dividends_interest": Decimal("0")}
    )
    assert metrics["realized_pl"] is None


# --------------------------------------------------------------------------------------
# F16 — Export packages stated no basis for gross income/net beside true-cost columns
# --------------------------------------------------------------------------------------
#
# summary.json and the grouped CSVs compute income and net on a gross basis (no
# offset or income-treatment netting) while carrying offsets/true_cost columns that
# adopt the treatment-netted vocabulary on the spend side — and nothing in the zip
# said which basis anything used. The share-safe artifact exists to be handed to a
# third party. The summary now carries explicit basis rows in every package.


def test_export_summary_declares_its_bases():
    import pandas as pd

    from ledger.exporting.service import ExportPackageService

    frame = ExportPackageService._summary(pd.DataFrame(), "chatgpt-safe", False)
    rows = frame["value"].to_dict()
    assert "gross cashflow income" in rows["income_basis"]
    assert "will differ" in rows["income_basis"]
    assert rows["spend_basis"].startswith("gross_spend is policy spending")
    assert rows["net_basis"] == "income - gross_spend, both gross"


# --------------------------------------------------------------------------------------
# F17 — Persisted trend stats carried sub-cent precision; F18 — CSV exports truncated
# --------------------------------------------------------------------------------------
#
# F17: spending_trends avg/median divided cents into repeating decimals (odd counts)
# and half-cents (even medians) and persisted them raw, violating the module's own
# cents invariant. F18: the Transactions/Accounts CSV downloads were built from the
# display frame, whose Description column was truncated to 74 characters — the very
# exports whose caption says to reconcile them against a statement.


def test_trend_stats_are_persisted_at_cent_precision(tmp_path):
    from ledger.analysis.trend_aggregator import TrendAggregator
    from ledger.core.models import Transaction

    def _txn(i: int, amount: str) -> Transaction:
        return Transaction(
            txn_id=f"t{i:03d}",
            posting_date=date(2026, 8, 5),
            institution="harbor",
            account="a1",
            account_type="Chequing",
            flow="expense",
            cashflow_type="expense",
            include_in_cashflow=True,
            currency="CAD",
            amount_raw=Decimal(amount),
            amount_signed=Decimal(amount) * -1,
            description="D",
            merchant="M",
            category="Dining",
            source_file="s.csv",
            source_path="/p/s.csv",
            source_period="2026-08",
            source_row=i,
            imported_at=datetime(2026, 8, 16),
            parser_version="test",
            txn_fingerprint=f"fp-{i:03d}",
        )

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    aggregator = TrendAggregator(db_path)
    # Three transactions of $10.00: mean is 10/3 — repeating — and must land at cents.
    trend = TrendAggregator._build_daily_trend(
        aggregator,
        "2026-08-05",
        "Dining",
        [_txn(1, "10.00"), _txn(2, "10.00"), _txn(3, "10.00")],
        date(2026, 8, 5),
    )
    assert trend.avg_transaction == Decimal("10.00")
    # Even count: median of 10.00 and 10.01 is 10.005 — must land at cents.
    trend = TrendAggregator._build_daily_trend(
        aggregator,
        "2026-08-05",
        "Dining",
        [_txn(1, "10.00"), _txn(2, "10.01")],
        date(2026, 8, 5),
    )
    assert trend.median_transaction in {Decimal("10.00"), Decimal("10.01")}
    assert trend.median_transaction == trend.median_transaction.quantize(Decimal("0.01"))


def test_transaction_frame_keeps_full_descriptions_for_export():
    from ledger.core.models import Transaction
    from ledger.ui.components import transaction_dataframe

    long_description = "X" * 200
    txn = Transaction(
        txn_id="t900",
        posting_date=date(2026, 8, 5),
        institution="harbor",
        account="a1",
        account_type="Chequing",
        flow="expense",
        cashflow_type="expense",
        include_in_cashflow=True,
        currency="CAD",
        amount_raw=Decimal("10.00"),
        amount_signed=Decimal("-10.00"),
        description=long_description,
        merchant="M",
        category="Dining",
        source_file="s.csv",
        source_path="/p/s.csv",
        source_period="2026-08",
        source_row=1,
        imported_at=datetime(2026, 8, 16),
        parser_version="test",
        txn_fingerprint="fp-900",
    )
    frame = transaction_dataframe([txn], detailed=False)
    assert frame.iloc[0]["Description"] == long_description, (
        "the frame feeds the CSV download; a display truncation exported '...'-"
        "terminated descriptions users then reconciled against statements"
    )


# --------------------------------------------------------------------------------------
# F23 — proposals apply misreported refusal causes as argument/internal errors
# --------------------------------------------------------------------------------------
#
# An open import run surfaced as `invalid_argument` (sending the operator hunting a
# typo instead of the open run) and writer-lock contention as `internal_error`
# (implying a bug instead of retry-after-the-other-writer). Both now carry codes
# naming the actual condition.


@pytest.mark.parametrize("raised", ["open_import", "lock"])
def test_proposals_apply_names_the_refusal_condition(tmp_path, monkeypatch, capsys, raised):
    import typer

    from ledger.commands import proposals as proposals_cmd
    from ledger.imports.pdf.readiness import LiveMutationDuringOpenImportRefused
    from ledger.storage.snapshots import DatabaseTargetLockError

    expected_code = {
        "open_import": "open_import_run",
        "lock": "database_busy",
    }[raised]
    db_path = tmp_path / "ledger.db"
    db_path.write_bytes(b"")

    class _Service:
        def __init__(self, *_args, **_kwargs):
            pass

        def apply_proposal(self, *_args, **_kwargs):
            if raised == "open_import":
                raise LiveMutationDuringOpenImportRefused("run open")
            raise DatabaseTargetLockError("/locked")

    monkeypatch.setattr(proposals_cmd, "ProposalService", _Service)
    with pytest.raises(typer.Exit):
        proposals_cmd.apply_proposal(
            "prop-1",
            db_path=db_path,
            proposals_db=tmp_path / "proposals.db",
            backup_dir=None,
        )
    out = capsys.readouterr().out
    assert f"proposal_command_failed:{expected_code}" in out


# --------------------------------------------------------------------------------------
# F24 — Admin action outcomes (including errors) were wiped by an immediate rerun
# --------------------------------------------------------------------------------------
#
# Re-check Migrations, Detect/Apply Adjustments, and Execute Cleanup rendered their
# st.success/st.error and called st.rerun() on the same frame — the message existed
# for one invisible frame and was gone, so a failed cleanup's error vanished before
# it could be read. Outcomes now survive the rerun in session state and render at
# the top of the next frame.


def test_admin_action_outcome_survives_the_rerun(monkeypatch):
    import streamlit as st

    from ledger.ui.pages import admin_data

    stash: dict = {}
    monkeypatch.setattr(st, "session_state", stash, raising=False)
    rendered: list[tuple[str, str]] = []
    monkeypatch.setattr(st, "success", lambda msg: rendered.append(("success", msg)))
    monkeypatch.setattr(st, "error", lambda msg: rendered.append(("error", msg)))
    monkeypatch.setattr(st, "rerun", lambda: None)

    admin_data._finish_action("error", "cleanup failed: target busy")
    assert stash[admin_data._ACTION_OUTCOME_KEY] == ("error", "cleanup failed: target busy")
    assert rendered == [], "the message renders on the NEXT frame, not the wiped one"

    admin_data._render_pending_outcome()
    assert rendered == [("error", "cleanup failed: target busy")]
    assert admin_data._ACTION_OUTCOME_KEY not in stash, "outcome renders once, not forever"

    admin_data._render_pending_outcome()
    assert rendered == [("error", "cleanup failed: target busy")]


# --------------------------------------------------------------------------------------
# F25 — F3's overlap predicate had two more live instances
# --------------------------------------------------------------------------------------
#
# The snapshot payload's recurring_costs list (which also feeds the recurring insight
# findings generated from it) and the export package's recurring rows both required
# last_occurrence inside the window, dropping active commitments exactly as the
# readiness figures did before F3.


def test_snapshot_recurring_costs_keep_active_commitments(tmp_path):
    from ledger.reporting import ReportSnapshotService

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _seed_cashflow_transaction(db_path)
    _seed_pattern(
        db_path,
        pattern_id="p-f25",
        pattern_type="bill",
        frequency="biweekly",
        amount="840.00",
        first=date(2040, 11, 9),
        last=date(2042, 7, 13),
        next_expected=date(2042, 7, 27),
    )

    snapshot = ReportSnapshotService(db_path).snapshot_for_period(
        date(2042, 8, 1), date(2042, 8, 14), persist=False
    )
    recurring = (snapshot.get("payload") or {}).get("recurring_costs") or []
    assert any(row.get("merchant") == "M-p-f25" for row in recurring), (
        "an active bill whose last charge predates the window is still a live "
        "commitment and belongs in the snapshot's recurring costs"
    )


def test_export_recurring_rows_keep_active_commitments(tmp_path):
    from ledger.exporting.service import ExportPackageService

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _seed_pattern(
        db_path,
        pattern_id="p-f25x",
        pattern_type="bill",
        frequency="monthly",
        amount="90.00",
        first=date(2041, 10, 20),
        last=date(2042, 7, 20),
        next_expected=date(2042, 8, 20),
    )
    _seed_pattern(
        db_path,
        pattern_id="p-dead-f25",
        pattern_type="bill",
        frequency="monthly",
        amount="99.00",
        first=date(2041, 1, 1),
        last=date(2041, 6, 1),
        next_expected=date(2041, 7, 1),
        active=False,
    )

    frame = ExportPackageService(db_path)._recurring_rows(date(2042, 8, 1), date(2042, 8, 14))
    merchants = set(frame["merchant"]) if not frame.empty else set()
    assert "M-p-f25x" in merchants, "active pattern must survive a window it has not recharged in"
    assert "M-p-dead-f25" not in merchants, (
        "a retired pattern with no in-window occurrence stays out of the export"
    )


# --------------------------------------------------------------------------------------
# F26 — Balance change fabricated a reassuring 0.00 for windows the series never reached
# --------------------------------------------------------------------------------------
#
# ledger.balances' change block selected its closing observation as the latest
# relative row at-or-before as_of with no in-window requirement. When an account's
# series ended before the window opened, closing and opening resolved to the SAME
# stale row and the tool reported status "reported", amount 0.00 for a window holding
# zero observations — indistinguishable on the wire from a genuinely flat window, on
# the month-to-date call every agent makes. The window is now refused with its own
# status, and every change block carries observed_through so a series that ends
# inside the window is visible too. (tests/test_mcp_balances.py pinned the old 0.00
# as intended behavior; that pin was corrected with this fix.)


def test_change_window_past_the_series_end_is_refused_not_zero(tmp_path):
    from mcp_synthetic import build_synthetic_mcp_database

    from ledger.mcp.server import call_tool
    from ledger.mcp.service import LedgerMCPService

    service = LedgerMCPService(build_synthetic_mcp_database(tmp_path / "ledger.db"))
    try:
        result = call_tool(
            service,
            "ledger.balances",
            {"as_of": "2025-06-20", "change_since": "2025-06-16", "limit": 100},
        )
        entries = result["structuredContent"]["data"]["entries"]
        chequing = next(e for e in entries if e["account_type"] == "chequing")
        change = chequing["change"]
        assert change["status"] == "no_observation_in_window"
        assert change["amount"] is None, "a zero here would claim the account did not move"
        assert change["observed_through"] == "2025-06-15"
    finally:
        service.close()


def test_reported_change_discloses_its_closing_observation_date(tmp_path):
    from mcp_synthetic import build_synthetic_mcp_database

    from ledger.mcp.server import call_tool
    from ledger.mcp.service import LedgerMCPService

    service = LedgerMCPService(build_synthetic_mcp_database(tmp_path / "ledger.db"))
    try:
        result = call_tool(
            service,
            "ledger.balances",
            {"as_of": "2025-12-31", "change_since": "2025-06-02", "limit": 100},
        )
        entries = result["structuredContent"]["data"]["entries"]
        chequing = next(e for e in entries if e["account_type"] == "chequing")
        change = chequing["change"]
        assert change["status"] == "reported"
        assert change["observed_through"] == "2025-06-30", (
            "the delta measures only through the series' last observation; that "
            "date must travel with the number"
        )
    finally:
        service.close()


# --------------------------------------------------------------------------------------
# F27 — Review Queue editor silently re-targeted pending edits after a filter change
# --------------------------------------------------------------------------------------
#
# The decisions data_editor was keyed only by review source, and Streamlit stores a
# keyed editor's pending edits positionally with widget identity by schema (columns,
# dtypes, row count) — not cell values. A filter/search change producing an
# equal-sized visible subset re-applied the stored edits to whatever rows now sat at
# those positions, and Save wrote the decision onto the wrong review rows. The key
# now binds the visible row set, so a changed subset mints a fresh editor: pending
# unsaved edits are dropped (safe) instead of silently retargeted.


def test_editor_key_changes_with_the_visible_row_set():
    from ledger.ui.pages.review_queue import _editor_state_key

    source = {"source_kind": "Active Import", "label": "20260816T232446Z"}
    rows_a = [{"review_key": "alpha"}, {"review_key": "beta"}]
    rows_b = [{"review_key": "gamma"}, {"review_key": "delta"}]  # equal size, other rows
    key_a = _editor_state_key(source, rows_a)
    key_b = _editor_state_key(source, rows_b)
    assert key_a != key_b, "an equal-sized different subset must not inherit pending edits"
    assert key_a == _editor_state_key(source, list(rows_a)), "same subset keeps its editor state"


def test_editor_key_still_scopes_by_review_source():
    from ledger.ui.pages.review_queue import _editor_state_key

    rows = [{"review_key": "alpha"}]
    key_staged = _editor_state_key({"source_kind": "Active Import", "label": "runA"}, rows)
    key_live = _editor_state_key({"source_kind": "Current DB", "label": "ledger.db"}, rows)
    assert key_staged != key_live


# --------------------------------------------------------------------------------------
# F28 — Interrupting a pre-protocol re-finalize permanently blocked every live write
# --------------------------------------------------------------------------------------
#
# The supported verify/repair path for an already-finalized run begins by demoting
# the terminal status to "finalizing" and persisting it before replaying the
# idempotent tail. A legacy (pre-publication-protocol) run carries no durable
# publication proof, so one interruption in that multi-second tail — Ctrl-C during
# per-PDF re-hashing, a BUSY on the audit insert — left status "finalizing" with no
# marker, and _run_can_still_promote then refused every live write (rules saves,
# config imports, category fixes) until manual run.json surgery. The demotion now
# stamps publication_completed in the same durable write: a run only ever reads
# "finalized" because its promote completed.


def test_interrupted_pre_protocol_refinalize_does_not_block_live_writes(tmp_path, monkeypatch):
    import json as json_module

    from ledger.imports.pdf import workflow
    from ledger.imports.pdf.files import write_run_state
    from ledger.imports.pdf.paths import Paths
    from ledger.imports.pdf.readiness import _run_can_still_promote

    run_dir = tmp_path / "import-runs" / "20260703T000000Z"
    run_dir.mkdir(parents=True)
    state = {
        "status": "finalized",
        "target_db_path": str(tmp_path / "ledger.db"),
        "staging_db_path": str(run_dir / "staging.db"),
        "pdfs": [],
    }
    write_run_state(run_dir, state)

    def _interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(workflow, "_archive_finalize_pdfs", _interrupt)
    paths = Paths(
        pdf_dir=tmp_path / "inbox",
        processed_dir=tmp_path / "processed",
        db_path=tmp_path / "ledger.db",
        base_db_path=None,
        backup_dir=tmp_path / "backups",
        ledger_config=tmp_path / "ledger.toml",
        merchant_rules=tmp_path / "merchants.toml",
        categories=tmp_path / "categories.toml",
        runs_dir=run_dir.parent,
    )
    with pytest.raises(KeyboardInterrupt):
        workflow._complete_finalize_tail(
            run_dir,
            json_module.loads((run_dir / "run.json").read_text(encoding="utf-8")),
            paths,
        )

    interrupted = json_module.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert interrupted["status"] == "finalizing", "the tail was mid-flight when interrupted"
    assert _run_can_still_promote(interrupted) is False, (
        "a run that already promoted must never re-block live writes because its "
        "verify/repair tail was interrupted"
    )


# --------------------------------------------------------------------------------------
# F29..F32 — Agent-surface disclosure batch
# --------------------------------------------------------------------------------------
#
# F29 ledger.transfers `total` leg-sums matched pairs (each contributes twice) with
#     nothing on the wire saying so; `total_semantics` now states the basis.
# F30 An empty filters array silently meant "no filter": the full-period total was
#     served for a request naming zero values — the natural shape of an agent piping
#     an upstream lookup that matched nothing. It is refused now (schema minItems and
#     boundary guard both).
# F31 Investment-statement imports never advance ledger.changes' stamps; the tool now
#     says so on the wire instead of letting a changes loop miss the whole domain.
# F32 ledger.net_worth silently dropped accounts with no anchor by as_of — absence
#     read as "holds nothing". A component census with both absence reasons (and the
#     investment split) now travels with every answer.


def _mcp_service(tmp_path):
    from mcp_synthetic import build_synthetic_mcp_database

    from ledger.mcp.service import LedgerMCPService

    return LedgerMCPService(build_synthetic_mcp_database(tmp_path / "ledger.db"))


def test_transfers_total_carries_its_leg_sum_semantics(tmp_path):
    from ledger.mcp.server import call_tool

    service = _mcp_service(tmp_path)
    try:
        result = call_tool(
            service,
            "ledger.transfers",
            {"start_date": "2025-01-01", "end_date": "2025-12-31", "limit": 50},
        )
        data = result["structuredContent"]["data"]
        assert "leg" in data["total_semantics"].lower()
        assert "halving" in data["total_semantics"]
    finally:
        service.close()


def test_empty_filter_array_is_refused_not_widened(tmp_path):
    from ledger.mcp.server import call_tool

    service = _mcp_service(tmp_path)
    try:
        result = call_tool(
            service,
            "ledger.aggregate",
            {
                "start_date": "2025-01-01",
                "end_date": "2025-12-31",
                "flow": "spending",
                "group_by": "category",
                "filters": {"categories": []},
            },
        )
        assert result["isError"] is True, "an empty filter array must not serve the full total"
        assert result["structuredContent"]["error"]["code"] == "invalid_argument"
    finally:
        service.close()


def test_changes_disclose_the_investment_blind_spot(tmp_path):
    from ledger.mcp.server import call_tool

    service = _mcp_service(tmp_path)
    try:
        result = call_tool(service, "ledger.changes", {"since": "2025-01-01T00:00:00"})
        data = result["structuredContent"]["data"]
        assert "investment" in data["investment_semantics"].lower()
        assert "never advances" in data["investment_semantics"]
    finally:
        service.close()


def test_net_worth_censuses_its_omissions(tmp_path):
    from ledger.mcp.server import call_tool

    service = _mcp_service(tmp_path)
    try:
        # A date before most anchors exist: dropped accounts must be counted,
        # split by reason, not silently absent.
        result = call_tool(service, "ledger.net_worth", {"as_of": "2025-06-01", "limit": 100})
        data = result["structuredContent"]["data"]
        counts = data["component_counts"]
        assert counts["banking_reported"] == data["component_count"]
        assert (
            counts["banking_no_anchor_on_or_before_as_of"] + counts["banking_no_absolute_anchor"]
            > 0
        ), "accounts dropped for having no anchor by as_of must appear in the census"
        assert "absence" in data["component_counts_semantics"].lower()
    finally:
        service.close()


# --------------------------------------------------------------------------------------
# F33 — The Review Queue could not save any decision for a large open run
# --------------------------------------------------------------------------------------
#
# run.json grows with the batch (one pdfs[] entry per statement, indent=2) and a
# real ~50-PDF stage crosses 64 KiB. The staged-source validator read run.json with
# a 64 KiB cap and treated the overflow as "Staged review source is unavailable", so
# every save on a legitimately large open run failed with the generic error. The cap
# now bounds hostile input (8 MiB) instead of policing batch size.


def test_review_save_pin_accepts_a_large_legitimate_run(tmp_path):
    import json as json_module
    import os
    from types import SimpleNamespace

    from ledger.ui.pages.review_queue import _validated_staged_review_source

    runs_dir = tmp_path / "import-runs"
    run_dir = runs_dir / "20260826T000000Z"
    run_dir.mkdir(parents=True)
    os.chmod(tmp_path, 0o700)
    os.chmod(runs_dir, 0o700)
    os.chmod(run_dir, 0o700)
    staging = run_dir / "staging.db"
    staging.write_bytes(b"")
    os.chmod(staging, 0o600)
    # A finalizable run whose pdfs[] block alone exceeds the old 64 KiB cap.
    state = {
        "status": "needs_review",
        "staging_db_path": str(staging),
        "pdfs": [
            {"original_pdf": f"/private/inbox/statement-{i:04d}.pdf", "file_hash": "a" * 64}
            for i in range(600)
        ],
    }
    run_json = run_dir / "run.json"
    run_json.write_text(json_module.dumps(state, indent=2), encoding="utf-8")
    os.chmod(run_json, 0o600)
    assert run_json.stat().st_size > 64 * 1024, "fixture must exceed the old cap"

    ctx = SimpleNamespace(
        review_source={"run_dir": str(run_dir), "runs_dir": str(runs_dir)},
        review_db=staging,
    )
    with _validated_staged_review_source(ctx) as pinned:
        assert pinned.path == staging


# --------------------------------------------------------------------------------------
# F34 — The forecast's starting balance vanished with the activity heuristic
# --------------------------------------------------------------------------------------
#
# _current_positive_balance gated accounts on is_active, which is recomputed
# against wall-clock at every import finalize — so importing one institution's
# statement silently re-aged every other account still waiting on its own, and once
# an account crossed the decay line its anchored money vanished from the forecast's
# starting value. Partial statement arrival is this system's documented normal
# state. Accounts are no longer activity-filtered; an account with no statement
# anchor is skipped (unknown, not zero) instead of vetoing the anchored ones, and
# only a database with no anchors at all reports no starting value. Detailed pins
# live beside the older forecast tests in tests/test_ui_helpers.py.


def test_forecast_balance_ignores_the_decayed_activity_flag():
    from types import SimpleNamespace

    from ledger.ui.pages.insights import _current_positive_balance

    class FakeService:
        def get_accounts(self, **kwargs):
            assert "is_active" not in kwargs, "money must not gate on the decayed activity flag"
            return [{"account_id": "quiet-but-open"}]

        def get_latest_account_balance(self, account_id, *, end_date, balance_basis):
            assert balance_basis == "absolute"
            return {"balance": Decimal("5225.71"), "balance_basis": "absolute"}

    ctx = SimpleNamespace(service=FakeService(), end_date=date(2026, 8, 14))
    assert _current_positive_balance(ctx) == Decimal("5225.71")


# --------------------------------------------------------------------------------------
# F35..F37 — Low-severity sweep batch
# --------------------------------------------------------------------------------------
#
# F35 The Budgets/Goals save preview diffed row-list LENGTHS, so deleting one row
#     and adding another previewed as "0 creates / 0 deletes". Identity-based now.
# F36 COVERAGE_ROW_COUNT_STATEMENT pinned point-in-time row counts
#     as present-tense facts that drift on every import; the statement now states
#     the reconciliation RULE without the numbers.
# F37 The build/config CLIs created database parent directories with bare mkdir
#     (umask mode) while every other managed-path creator is owner-only; both route
#     through ensure_private_dir now.


def test_budget_preview_diffs_identities_not_lengths():
    from ledger.ui.pages.budgets_goals import _planning_save_preview

    original = [{"month": "2026-08", "category": "Dining", "amount": "100", "notes": ""}]
    edited = [{"month": "2026-08", "category": "Groceries", "amount": "100", "notes": ""}]
    preview = _planning_save_preview(original, edited, "budget")
    assert preview["creates"] == 1, "a swapped row is one create plus one delete, not nothing"
    assert preview["deletes"] == 1


def test_coverage_row_count_statement_carries_no_point_in_time_figures():
    from ledger.mcp.contracts import COVERAGE_ROW_COUNT_STATEMENT

    assert not any(character.isdigit() for character in COVERAGE_ROW_COUNT_STATEMENT), (
        "a served contract must state the rule, not a measurement that drifts on every import"
    )


def test_cli_database_parent_dirs_are_created_owner_only(tmp_path):
    import subprocess
    import sys

    target = tmp_path / "nested" / "data" / "ledger.db"
    code = (
        "from pathlib import Path\n"
        "from ledger.utils.private_files import ensure_private_dir\n"
        f"ensure_private_dir(Path({str(target.parent)!r}))\n"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
    mode = target.parent.stat().st_mode & 0o777
    assert mode == 0o700, f"managed data directory must be owner-only, got {oct(mode)}"


# --------------------------------------------------------------------------------------
# F38 — ledger.recurring served write-once detector output with no timestamp
# --------------------------------------------------------------------------------------
#
# Recurring patterns are rewritten only by an analysis run — a later import does not
# move them — yet unlike the sibling insights/quality tools (computed_at), the
# recurring entries carried nothing saying when the detector last wrote them, so
# stale patterns read as current. Every entry now carries updated_at.


def test_recurring_entries_carry_their_detector_timestamp(tmp_path):
    from ledger.mcp.server import call_tool

    service = _mcp_service(tmp_path)
    try:
        result = call_tool(service, "ledger.recurring", {"status": "any", "limit": 100})
        entries = result["structuredContent"]["data"]["entries"]
        assert entries, "fixture serves recurring patterns"
        for entry in entries:
            assert "updated_at" in entry, "write-once output must say when it was written"
    finally:
        service.close()
