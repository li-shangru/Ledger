"""Regression tests for the 2026-06-29 third-pass review fixes.

Each test pins a behavior the review found broken; they are written to FAIL
against the pre-fix tree and pass after the corresponding fix.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path


def _make_txn(
    txn_id: str,
    category: str,
    merchant: str = "TEST MERCHANT",
    *,
    amount_signed: Decimal | None = None,
    posting_date: date = date(2026, 5, 1),
    currency: str = "CAD",
    exclude_from_spending: bool = False,
):
    from ledger.core import Transaction

    amt = Decimal("-25.00") if amount_signed is None else amount_signed
    flow = "income" if amt > 0 else "expense"
    cashflow_type = "income" if amt > 0 else "expense"
    return Transaction(
        txn_id=txn_id,
        txn_fingerprint=f"fp-{txn_id}",
        posting_date=posting_date,
        institution="harbor",
        account="chequing",
        flow=flow,
        cashflow_type=cashflow_type,
        include_in_cashflow=True,
        exclude_from_spending=exclude_from_spending,
        amount_raw=abs(amt),
        amount_signed=amt,
        description=f"DESC {txn_id}",
        merchant=merchant,
        category=category,
        currency=currency,
        source_file="test.csv",
        source_path="test/test.csv",
        source_row=1,
        imported_at=datetime.now(UTC).replace(tzinfo=None),
        parser_version="test",
    )


def _load_pdf_module():
    """Import the package PDF extractor module directly (source under test)."""
    from ledger.imports.pdf import statement_pdf_to_csv as mod

    return mod


# ---------------------------------------------------------------------------
# R1 — HARBOR Visa table boundary must not latch closed on a mid-table total,
# dropping the continuation rows that follow it.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# H1 — statement repair must carry MANUAL/RULE per-transaction overrides forward
# from the live DB into the freshly-built candidate, not silently discard them.
# ---------------------------------------------------------------------------


def test_repair_carry_forward_preserves_manual_and_rule_overrides(tmp_path):
    from ledger.processing import ProcessingResult
    from ledger.repair.statements import carry_forward_overrides
    from ledger.storage.database import TransactionDB, init_database, session_scope
    from ledger.storage.writer import DatabaseWriter

    live_db = tmp_path / "live.db"
    candidate_db = tmp_path / "candidate.db"
    init_database(live_db)
    init_database(candidate_db)

    # Live DB: three rows. "m1" carries a MANUAL override, "r1" a RULE override,
    # "i1" only IMPORT-confidence (must NOT be carried).
    DatabaseWriter(live_db).write_result(
        ProcessingResult(
            transactions=[
                _make_txn("m1", "Entertainment", merchant="RAW M1"),
                _make_txn("r1", "Entertainment", merchant="RAW R1"),
                _make_txn("i1", "Entertainment", merchant="RAW I1"),
            ],
            transfers=[],
            exceptions=[],
            total_scanned=3,
        ),
        tmp_path,
        "live",
    )
    with session_scope(live_db) as s:
        m1 = s.get(TransactionDB, "m1")
        m1.category, m1.category_confidence = "Groceries", "manual"
        m1.merchant, m1.merchant_confidence = "USER MERCHANT", "manual"
        m1.exclude_from_spending = True
        r1 = s.get(TransactionDB, "r1")
        r1.category, r1.category_confidence = "Dining", "rule"

    # Candidate: freshly rebuilt from the same statements -> IMPORT confidence,
    # raw parsed merchant, default category.
    DatabaseWriter(candidate_db).write_result(
        ProcessingResult(
            transactions=[
                _make_txn("m1", "Uncategorized", merchant="RAW M1"),
                _make_txn("r1", "Uncategorized", merchant="RAW R1"),
                _make_txn("i1", "Uncategorized", merchant="RAW I1"),
            ],
            transfers=[],
            exceptions=[],
            total_scanned=3,
        ),
        tmp_path,
        "candidate",
    )

    carried = carry_forward_overrides(live_db, candidate_db)
    assert carried == 2, f"expected 2 rows carried (m1 manual, r1 rule), got {carried}"

    with session_scope(candidate_db) as s:
        m1 = s.get(TransactionDB, "m1")
        assert m1.category == "Groceries" and m1.category_confidence == "manual"
        assert m1.merchant == "USER MERCHANT" and m1.merchant_confidence == "manual"
        assert m1.exclude_from_spending is True
        r1 = s.get(TransactionDB, "r1")
        assert r1.category == "Dining" and r1.category_confidence == "rule"
        i1 = s.get(TransactionDB, "i1")
        assert i1.category == "Uncategorized" and i1.category_confidence != "manual"


# ---------------------------------------------------------------------------
# H2 — Transactions page must not silently drop rows above a hardcoded $10k cap.
# ---------------------------------------------------------------------------


def test_get_max_abs_amount_reports_largest_in_range(tmp_path):
    from ledger.processing import ProcessingResult
    from ledger.query.service import LedgerQueryService
    from ledger.storage.database import init_database
    from ledger.storage.writer import DatabaseWriter

    db = tmp_path / "ledger.db"
    init_database(db)
    DatabaseWriter(db).write_result(
        ProcessingResult(
            transactions=[
                _make_txn(
                    "a", "Income", amount_signed=Decimal("12500.00"), posting_date=date(2026, 5, 10)
                ),
                _make_txn(
                    "b",
                    "Groceries",
                    amount_signed=Decimal("-42.00"),
                    posting_date=date(2026, 5, 11),
                ),
                # Outside the queried range -> must be ignored.
                _make_txn(
                    "c", "Income", amount_signed=Decimal("99999.00"), posting_date=date(2026, 1, 1)
                ),
            ],
            transfers=[],
            exceptions=[],
            total_scanned=3,
        ),
        tmp_path,
        "t",
    )
    svc = LedgerQueryService(db)
    result = svc.get_max_abs_amount(date(2026, 5, 1), date(2026, 5, 31))
    assert result == Decimal("12500.00"), f"expected the $12,500 row, got {result}"


def test_resolve_amount_bounds_treats_extremes_as_unbounded():
    from ledger.ui.pages.transactions import _resolve_amount_bounds

    cap = 12500
    # Full range -> no filtering at all (the data-loss-prone default).
    assert _resolve_amount_bounds(0, cap, cap) == (None, None)
    # Lower bound raised -> min set, max still unbounded.
    lo, hi = _resolve_amount_bounds(50, cap, cap)
    assert lo == Decimal("50") and hi is None
    # Upper bound lowered below cap -> explicit upper bound applies.
    lo, hi = _resolve_amount_bounds(0, 500, cap)
    assert lo is None and hi == Decimal("500")


# ---------------------------------------------------------------------------
# P1 — currency-blindness: the CAD-only money invariant (enforced in the SQL
# aggregate layer + balance_calculator) must also hold in the Python analysis
# layer. A genuine USD row must never be summed into a CAD total at par.
# ---------------------------------------------------------------------------


def _write(db, txns):
    from ledger.processing import ProcessingResult
    from ledger.storage.writer import DatabaseWriter

    DatabaseWriter(db).write_result(
        ProcessingResult(transactions=txns, transfers=[], exceptions=[], total_scanned=len(txns)),
        db.parent,
        "t",
    )


def test_detect_credit_limit_ignores_non_cad_rows(tmp_path):
    from ledger.analysis.account_detector import AccountDetector

    det = AccountDetector(tmp_path / "x.db")
    cad = _make_txn("c1", "Shopping", amount_signed=Decimal("-100.00"), currency="CAD")
    usd = _make_txn("u1", "Shopping", amount_signed=Decimal("-9000.00"), currency="USD")
    assert det._detect_credit_limit([cad, usd]) == Decimal("100"), (
        "a USD charge inflated the CAD credit-limit estimate"
    )


def test_health_cashflow_period_excludes_non_cad(tmp_path):
    from ledger.analysis.health_scorer import FinancialHealthScorer
    from ledger.storage.database import init_database

    db = tmp_path / "ledger.db"
    init_database(db)
    scorer = FinancialHealthScorer(db)
    period = scorer._get_cashflow_period(
        [
            _make_txn("ci", "Income", amount_signed=Decimal("5000.00"), currency="CAD"),
            _make_txn("ui", "Income", amount_signed=Decimal("9000.00"), currency="USD"),
            _make_txn("ce", "Rent", amount_signed=Decimal("-1000.00"), currency="CAD"),
            _make_txn("ue", "Rent", amount_signed=Decimal("-8000.00"), currency="USD"),
        ]
    )
    assert period["income"] == Decimal("5000") and period["expenses"] == Decimal("1000"), (
        f"non-CAD amounts leaked into the health cashflow totals: {period}"
    )


def test_trend_total_spent_excludes_usd(tmp_path):
    from ledger.analysis.trend_aggregator import TrendAggregator
    from ledger.storage.database import init_database

    db = tmp_path / "ledger.db"
    init_database(db)
    target = date(2026, 5, 15)
    _write(
        db,
        [
            _make_txn(
                "a", "Dining", amount_signed=Decimal("-25.00"), posting_date=target, currency="CAD"
            ),
            _make_txn(
                "b", "Dining", amount_signed=Decimal("-45.00"), posting_date=target, currency="CAD"
            ),
            _make_txn(
                "c",
                "Dining",
                amount_signed=Decimal("-9000.00"),
                posting_date=target,
                currency="USD",
            ),
        ],
    )
    trends = TrendAggregator(db).compute_daily_trends(target)
    dining = next(t for t in trends if t.category == "Dining")
    assert dining.total_spent == Decimal("70.00"), (
        f"USD row inflated the persisted trend total_spent: {dining.total_spent}"
    )


def test_detect_anomalies_excludes_usd_rows(tmp_path):
    from ledger.query.service import LedgerQueryService
    from ledger.storage.database import init_database

    db = tmp_path / "ledger.db"
    init_database(db)
    txns = [
        _make_txn(
            f"t{i}",
            "Travel",
            amount_signed=Decimal("-20.00"),
            posting_date=date(2026, 5, i + 1),
            currency="CAD",
        )
        for i in range(6)
    ]
    txns.append(
        _make_txn(
            "usd",
            "Travel",
            amount_signed=Decimal("-5000.00"),
            posting_date=date(2026, 5, 20),
            currency="USD",
        )
    )
    _write(db, txns)
    anomalies = LedgerQueryService(db).detect_anomalies(date(2026, 5, 1), date(2026, 5, 31))
    assert all(a.currency != "USD" for a in anomalies), "a non-CAD row surfaced as a CAD anomaly"


def test_analyze_merchants_excludes_usd_spend(tmp_path):
    from ledger.config.schemas import MerchantRulesConfig
    from ledger.optimize.categories import analyze_merchants
    from ledger.storage.database import init_database

    db = tmp_path / "ledger.db"
    init_database(db)
    _write(
        db,
        [
            _make_txn(
                "c", "Other", merchant="GLOBEX", amount_signed=Decimal("-50.00"), currency="CAD"
            ),
            _make_txn(
                "u", "Other", merchant="GLOBEX", amount_signed=Decimal("-9000.00"), currency="USD"
            ),
        ],
    )
    recs, _auto, _manual = analyze_merchants(db, MerchantRulesConfig(rules=[]))
    globex = next(r for r in recs if r["merchant"] == "GLOBEX")
    assert globex["spend"] == "50.00", (
        f"USD spend folded into the CAD merchant total: {globex['spend']}"
    )


def test_duplicate_candidates_ignore_cross_currency(tmp_path):
    from ledger.query.service import LedgerQueryService
    from ledger.storage.database import init_database

    db = tmp_path / "ledger.db"
    init_database(db)
    _write(
        db,
        [
            # Cross-currency pair of equal magnitude -> NOT a duplicate.
            _make_txn(
                "x1",
                "Shopping",
                merchant="ACME",
                amount_signed=Decimal("-50.00"),
                posting_date=date(2026, 5, 10),
                currency="CAD",
            ),
            _make_txn(
                "x2",
                "Shopping",
                merchant="ACME",
                amount_signed=Decimal("-50.00"),
                posting_date=date(2026, 5, 11),
                currency="USD",
            ),
            # Same-currency pair -> still a real duplicate candidate.
            _make_txn(
                "y1",
                "Shopping",
                merchant="BETA",
                amount_signed=Decimal("-30.00"),
                posting_date=date(2026, 5, 10),
                currency="CAD",
            ),
            _make_txn(
                "y2",
                "Shopping",
                merchant="BETA",
                amount_signed=Decimal("-30.00"),
                posting_date=date(2026, 5, 11),
                currency="CAD",
            ),
        ],
    )
    pairs = LedgerQueryService(db).get_duplicate_candidates(
        threshold_days=2, start_date=date(2026, 5, 1), end_date=date(2026, 5, 31)
    )
    merchants = {a.merchant for a, _b in pairs}
    assert "ACME" not in merchants, "cross-currency rows were flagged as duplicates"
    assert "BETA" in merchants, "a genuine same-currency duplicate was lost"


# ---------------------------------------------------------------------------
# P2 — correctness & consistency.
# ---------------------------------------------------------------------------


def test_fuzzy_does_not_match_digit_distinct_merchants():
    from ledger.utils.fuzzy import find_merchant_match

    names = ["407 ETR", "M2 CAKE", "CAMP 31 BBQ"]
    assert find_merchant_match("408 ETR", names) != "407 ETR"
    assert find_merchant_match("M5 CAKE", names) != "M2 CAKE"
    # An exact digit match still resolves.
    assert find_merchant_match("407 ETR TORONTO ON", names) == "407 ETR"
    # A store-number suffix is noise, not a significant digit: still matches.
    assert find_merchant_match("407 ETR #1234", names) == "407 ETR"


def test_compute_kpis_excludes_exclude_from_spending_expenses(tmp_path):
    from ledger.query.service import LedgerQueryService
    from ledger.storage.database import init_database

    db = tmp_path / "ledger.db"
    init_database(db)
    _write(
        db,
        [
            _make_txn("inc", "Income", amount_signed=Decimal("5000.00")),
            _make_txn("exp", "Groceries", amount_signed=Decimal("-1000.00")),
            _make_txn(
                "rmb",
                "Reimbursements",
                amount_signed=Decimal("-2000.00"),
                exclude_from_spending=True,
            ),
        ],
    )
    kpis = LedgerQueryService(db).compute_kpis(date(2026, 5, 1), date(2026, 5, 31))
    assert kpis["expenses"] == Decimal("1000.00"), (
        f"exclude_from_spending expense leaked into KPI expenses: {kpis['expenses']}"
    )
    assert kpis["net"] == Decimal("4000.00"), f"net diverged from the spend universe: {kpis['net']}"


def test_spending_view_footer_merchant_is_honest_subtotal():
    from ledger.commands.build import _spending_view_rows

    data = {"A": Decimal("60"), "B": Decimal("40")}  # top-N shown
    period_total = Decimal("200")  # true period spend (long tail not shown)
    rows, footer = _spending_view_rows("merchant", data, period_total)
    # Percentages are relative to the TRUE period total, not the shown subtotal.
    assert rows[0][2] == "30.0%"  # 60 / 200
    assert footer[0] == "TOP 20 SUBTOTAL"
    assert footer[2] == "50.0%"  # 100 / 200, not a fake 100%

    # Category view spans the whole universe, so its footer is a true TOTAL.
    rows, footer = _spending_view_rows("category", {"A": Decimal("75")}, Decimal("75"))
    assert footer[0] == "TOTAL" and footer[2] == "100.0%"


# ---------------------------------------------------------------------------
# P2 — error handling / input boundaries.
# ---------------------------------------------------------------------------


def test_non_utf8_csv_does_not_abort_build(tmp_path, sample_app_config):
    from ledger.processing.pipeline import process

    csv_dir = tmp_path / "csvs"
    csv_dir.mkdir()
    header = "Ledger Format,Date,Description,Amount,Currency,Reference,Institution,Account Type\n"
    (csv_dir / "harbor_good_2025-01.csv").write_text(
        header + "ledger-mapped-v1,2025-01-15,GROCERY STORE,-50.00,CAD,,harbor,Chequing\n",
        encoding="utf-8",
    )
    # A Latin-1 byte (0xC9 = É) that is invalid UTF-8 -> decode error on read.
    (csv_dir / "harbor_bad_2025-02.csv").write_bytes(
        header.encode()
        + b"ledger-mapped-v1,2025-02-15,CAF\xc9 MONTREAL,-12.00,CAD,,harbor,Chequing\n"
    )

    # Must NOT raise UnicodeDecodeError; the good file still imports.
    result = process(sample_app_config, csv_dir)
    assert any("GROCERY STORE" in (t.merchant or t.description or "") for t in result.transactions)
    assert any("file_read_error" in e.reason for e in result.exceptions), (
        "the unreadable file was not isolated as an ExceptionRecord"
    )


def test_save_merchants_skips_rows_without_patterns():
    import pandas as pd

    from ledger.ui.pages.rules_settings import _merchants_from_dataframe

    df = pd.DataFrame(
        [
            {
                "Merchant": "TIM HORTONS",
                "Category": "Coffee",
                "Exclude": False,
                "Patterns": "TIM\nHORTONS",
            },
            # In-progress row: merchant typed, patterns not yet -> must be skipped,
            # not crash the whole save with a pydantic ValidationError.
            {"Merchant": "NEW PLACE", "Category": "Other", "Exclude": False, "Patterns": ""},
        ]
    )
    cfg = _merchants_from_dataframe(df, "v1")
    assert {r.merchant_norm for r in cfg.rules} == {"TIM HORTONS"}


# ---------------------------------------------------------------------------
# P2 — data integrity.
# ---------------------------------------------------------------------------


def test_config_in_memory_guard_rejects_parent_with_merchants():
    from ledger.config.repository import _validate_configs_in_memory
    from ledger.config.schemas import (
        CategoriesConfig,
        CategoryRule,
        MerchantRulesConfig,
    )

    categories = CategoriesConfig(
        categories=[
            CategoryRule(name="Food", merchants=["TIM"]),  # parent that lists a merchant
            CategoryRule(name="Coffee", parent="Food"),  # makes "Food" a referenced parent
            CategoryRule(name="Other"),
        ]
    )
    issues = _validate_configs_in_memory(MerchantRulesConfig(rules=[]), categories)
    assert any("parent" in i.lower() and "merchant" in i.lower() for i in issues), (
        "the pre-replace guard did not catch a parent category that lists merchants"
    )


def test_insight_refresh_auto_closes_orphans(tmp_path):
    from ledger.insights.service import InsightEngine
    from ledger.storage.database import InsightFindingDB, init_database, session_scope

    db = tmp_path / "ledger.db"
    init_database(db)
    engine = InsightEngine(db)  # ensure_schema runs in __init__
    start, end = date(2026, 5, 1), date(2026, 5, 31)

    def _seed(fp, status):
        with session_scope(db) as s:
            s.add(
                InsightFindingDB(
                    fingerprint=fp,
                    period_start=start,
                    period_end=end,
                    finding_type="budget_overrun",
                    severity="high",
                    title="Stale finding",
                    explanation="x",
                    evidence={},
                    suggested_action="y",
                    status=status,
                    created_at=datetime.now(UTC).replace(tzinfo=None),
                )
            )

    _seed("orphan-open", "open")
    _seed("orphan-ignored", "ignored")

    # No transactions -> generate() yields nothing, so both seeded rows are orphans.
    engine.refresh_period(start, end)

    open_titles = {f["fingerprint"] for f in engine.list_findings(start, end, include_closed=False)}
    assert "orphan-open" not in open_titles, "an orphaned open finding was not auto-closed"
    with session_scope(db) as s:
        ignored = s.query(InsightFindingDB).filter_by(fingerprint="orphan-ignored").one()
        assert ignored.status == "ignored", "a user-set 'ignored' finding must be preserved"


# ---------------------------------------------------------------------------
# P3 — privacy.
# ---------------------------------------------------------------------------


def test_redact_money_strips_amount_tokens():
    from ledger.utils.text import redact_money

    out = redact_money("income in cashflow must have non-negative amount, got -4250.00")
    assert "4250.00" not in out and "<redacted>" in out
    out2 = redact_money("value $1,234.56 and 99.00 CR")
    assert "1,234.56" not in out2 and "99.00" not in out2


def test_parser_exception_reason_has_no_raw_amount():
    """An unexpected (non-deliberate) parse exception whose message embeds an amount
    must not write that amount into the ExceptionRecord.reason."""
    from types import SimpleNamespace

    from ledger.parsers.base import BaseParser

    class _Boom(BaseParser):
        INSTITUTION = "test"
        FORMAT_NAME = "test_boom"

        def parse_row(self, row, row_num):
            raise ValueError("amount -4250.00 failed validation")

    ctx = SimpleNamespace(source_file="s.csv", source_path="s.csv", include_raw=False, config=None)
    parser = _Boom(ctx)
    records = list(parser.parse(iter([{"x": "1"}])))
    assert len(records) == 1
    assert "4250.00" not in records[0].reason, f"raw amount leaked: {records[0].reason!r}"


def test_backup_database_artifact_is_owner_only(tmp_path):
    import stat

    from ledger.storage.database import init_database
    from ledger.storage.operations import backup_database

    db = tmp_path / "ledger.db"
    init_database(db)
    result = backup_database(db, backup_dir=tmp_path / "backups", reason="test")
    assert result.ok, result.errors
    artifact = Path(str(result.artifacts["backup"]))
    mode = stat.S_IMODE(artifact.stat().st_mode)
    assert mode == 0o600, f"backup is not owner-only (0o600); got {oct(mode)}"


def test_neutralize_csv_cell_blocks_formula_injection():
    from ledger.utils.text import neutralize_csv_cell

    assert neutralize_csv_cell('=HYPERLINK("http://evil","x")').startswith("'=")
    assert neutralize_csv_cell("@SUM(A1)").startswith("'@")
    assert neutralize_csv_cell("+1+1").startswith("'+")
    assert neutralize_csv_cell("-1+cmd").startswith("'-")
    # Plain text, numbers, and non-strings are left intact.
    assert neutralize_csv_cell("STARBUCKS") == "STARBUCKS"
    assert neutralize_csv_cell("-50.00") == "-50.00"
    assert neutralize_csv_cell(None) is None


def test_write_csv_neutralizes_formula_in_merchant(tmp_path):
    from ledger.output.writers import write_csv

    txn = _make_txn("x", "Shopping", merchant='=cmd|"/c calc"!A1')
    out = tmp_path / "export.csv"
    write_csv(out, [txn])
    content = out.read_text(encoding="utf-8")
    assert "'=cmd" in content, "merchant formula was not neutralized in the CSV export"
    assert "\n=cmd" not in content and ",=cmd" not in content
