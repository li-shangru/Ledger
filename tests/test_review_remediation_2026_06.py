"""Regression tests for the 2026-06-21 comprehensive-review remediation.

Regressions cover exact money, deterministic processing, and safe persistence.
"""

from __future__ import annotations

import contextlib
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

import ledger.imports.pdf.statement_pdf_to_csv as spc
from ledger.config.repository import SQLiteConfigRepository
from ledger.config.schemas import (
    CategoriesConfig,
    CategoryRule,
    MerchantRule,
    MerchantRulesConfig,
)
from ledger.core import Transaction
from ledger.import_review import (
    apply_review_decisions,
    get_review_rows,
    save_review_rows,
    seed_review_candidates,
)
from ledger.imports.pdf.statement_pdf_to_csv import ExtractedCsv
from ledger.processing import ProcessingResult
from ledger.query import LedgerQueryService
from ledger.rules import RuleApplicationService
from ledger.storage.database import TransactionDB, TransferDB, init_database, session_scope
from ledger.storage.mappers import transfer_from_db
from ledger.storage.writer import DatabaseWriter


def _write_period_csv(tmp_path: Path, name: str, content: str) -> tuple[Path, Path]:
    period_dir = tmp_path / "exports" / "2025-02"
    period_dir.mkdir(parents=True, exist_ok=True)
    file_path = period_dir / name
    file_path.write_text(content)
    return tmp_path / "exports", file_path


def _make_txn(
    i: int,
    *,
    amount: Decimal,
    merchant: str,
    cashflow_type: str = "expense",
    include_in_cashflow: bool = True,
    posting_date: date = date(2025, 4, 10),
    merchant_raw: str = "",
) -> Transaction:
    return Transaction(
        txn_id=f"rem_txn_{i}",
        txn_fingerprint=f"rem_fp_{i}",
        posting_date=posting_date,
        transaction_date=posting_date,
        institution="harbor",
        account="harbor-chequing",
        flow="income" if amount > 0 else "expense",
        cashflow_type=cashflow_type,
        include_in_cashflow=include_in_cashflow,
        currency="CAD",
        amount_raw=abs(amount),
        amount_signed=amount,
        description=f"REMEDIATION TEST {i}",
        merchant=merchant,
        category="Income" if amount > 0 else "Dining",
        exclude_from_spending=False,
        merchant_raw=merchant_raw,
        category_raw="",
        source_file="test.csv",
        source_path="/test/test.csv",
        source_period="2025-04",
        source_row=i,
        imported_at=date(2025, 4, 15),
        parser_version="test",
    )


def _write_txns(db_path: Path, txns: list[Transaction]) -> None:
    result = ProcessingResult(
        transactions=txns,
        transfers=[],
        exceptions=[],
        total_scanned=len(txns),
        duplicates_removed=0,
        collisions=0,
    )
    DatabaseWriter(db_path).write_result(result, Path("/test"), "test")


# --- #2: header whitespace stripped for detection but not row access ---------


# --- #12 / #54: HARBOR income/fee branches must sign by the raw amount -----------


# --- #16: stored transfer_confidence of 0.00 must round-trip (falsy bug) ------


def test_transfer_confidence_zero_round_trips(tmp_path: Path):
    """Transfer confidence zero round trips."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)

    with session_scope(db_path) as session:
        session.add(
            TransferDB(
                txn_id="rem_transfer_1",
                posting_date=date(2025, 4, 10),
                institution="harbor",
                account="harbor-chequing",
                account_type="Chequing",
                flow="transfer",
                amount_raw=Decimal("-50.00"),
                currency="CAD",
                description="E-TRANSFER OUT",
                source_file="harbor.csv",
                source_path="harbor/harbor.csv",
                imported_at=datetime(2025, 4, 10, 12, 0),
                paired_transfer_id="rem_transfer_other",
                transfer_confidence=Decimal("0.00"),
            )
        )

    with session_scope(db_path) as session:
        row = session.query(TransferDB).filter_by(txn_id="rem_transfer_1").one()
        txn = transfer_from_db(row)

    assert txn.transfer_confidence == 0.0
    assert txn.transfer_confidence is not None


# --- #15: get_income_sources must not emit a blank-merchant bucket -----------


def test_income_sources_excludes_blank_merchant(tmp_path: Path):
    db_path = tmp_path / "ledger.db"
    _write_txns(
        db_path,
        [
            _make_txn(
                1,
                amount=Decimal("2000.00"),
                merchant="ACME PAYROLL",
                cashflow_type="income",
            ),
            _make_txn(2, amount=Decimal("500.00"), merchant="", cashflow_type="income"),
        ],
    )

    sources = LedgerQueryService(db_path).get_income_sources(
        start_date=date(2025, 4, 1), end_date=date(2025, 4, 30)
    )

    assert "" not in sources, f"blank-merchant bucket leaked into income sources: {sources}"
    assert "ACME PAYROLL" in sources
    # quantization (#14): summed income stays exact cents
    assert sources["ACME PAYROLL"].as_tuple().exponent == -2


# --- #16 (quality): duplicate detection must skip blank-merchant rows --------


def test_duplicate_candidates_skip_blank_merchant(tmp_path: Path):
    db_path = tmp_path / "ledger.db"
    _write_txns(
        db_path,
        [
            _make_txn(
                1,
                amount=Decimal("-9.99"),
                merchant="",
                posting_date=date(2025, 4, 10),
            ),
            _make_txn(
                2,
                amount=Decimal("-9.99"),
                merchant="",
                posting_date=date(2025, 4, 11),
            ),
        ],
    )

    candidates = LedgerQueryService(db_path).get_duplicate_candidates(threshold_days=2)

    assert candidates == [], "two unrelated blank-merchant rows must not be reported as duplicates"


# --- #1: two statements for the same account+period must not overwrite -------

# Independently authored one-withdrawal format fixture.


# --- #11: standalone batch must survive one corrupt PDF ----------------------


def test_main_batch_survives_one_failing_pdf(tmp_path: Path, monkeypatch):
    """One PDF that raises during extraction must not abort the whole batch; the
    manifest must still record the good PDF and mark the bad one skipped."""
    pdf_dir = tmp_path / "inbox"
    pdf_dir.mkdir()
    good = pdf_dir / "good.pdf"
    bad = pdf_dir / "bad.pdf"
    good.write_bytes(b"%PDF-1.4")
    bad.write_bytes(b"%PDF-1.4")

    def fake_process(pdf: Path, *_args, **_kwargs):
        if pdf.name == "bad.pdf":
            raise RuntimeError("pdftotext exploded")
        return ExtractedCsv(pdf, tmp_path / "out.csv", None, "harbor_deposit", 3)

    monkeypatch.setattr(spc, "process_pdf", fake_process)
    manifest = tmp_path / "manifest.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "statement_pdf_to_csv",
            "--pdf-dir",
            str(pdf_dir),
            "--csv-root",
            str(tmp_path / "csv"),
            "--manifest",
            str(manifest),
            "--processed-dir",
            str(tmp_path / "processed"),
        ],
    )

    rc = spc.main()

    assert rc == 1, "a skipped PDF should make main() return nonzero"
    assert manifest.exists(), "manifest must still be written despite the failure"
    import json

    payload = json.loads(manifest.read_text())
    reasons = {entry["pdf"].rsplit("/", 1)[-1]: entry["skipped_reason"] for entry in payload}
    assert reasons["good.pdf"] == ""
    assert "extraction_error" in reasons["bad.pdf"]


# --- #13: get_monthly_cashflow totals stay exact cents -----------------------


def test_monthly_cashflow_is_cent_quantized(tmp_path: Path):
    db_path = tmp_path / "ledger.db"
    _write_txns(
        db_path,
        [
            _make_txn(1, amount=Decimal("1000.01"), merchant="PAY", cashflow_type="income"),
            _make_txn(2, amount=Decimal("-999.99"), merchant="STORE", cashflow_type="expense"),
        ],
    )

    rows = LedgerQueryService(db_path).get_monthly_cashflow(2025)

    assert rows, "expected at least one month of cashflow"
    for row in rows:
        for key in ("income", "expense", "net"):
            assert isinstance(row[key], Decimal)
            assert row[key].as_tuple().exponent == -2, f"{key} not cent-quantized: {row[key]!r}"


# --- #81: strict-mode validation error must not embed the raw amount ---------


# --- #9: recurring ('*') budget prorates partial months ----------------------


def test_recurring_budget_prorates_partial_months(tmp_path: Path):
    """A per-month ('*') budget over a window that clips partial calendar months must
    prorate, not count every whole calendar month touched."""
    from ledger.planning import BudgetService
    from ledger.storage.database import init_database

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    svc = BudgetService(db_path)
    svc.replace_budgets([{"month": "*", "category": "Groceries", "amount": "300.00", "notes": ""}])

    # Jan 15 to Apr 15 clips four calendar months but is ~3 months of elapsed time.
    rows = svc.budget_variance(date(2026, 1, 15), date(2026, 4, 15))
    groceries = next(r for r in rows if r["category"] == "Groceries")

    # Old behaviour counted 4 whole months → 4 * 300 = 1200. Prorated ≈ 3.05 months.
    assert groceries["budget"] < Decimal("1200"), "partial months were not prorated"
    assert Decimal("850") < groceries["budget"] < Decimal("950")


def test_recurring_budget_full_single_month_is_exact(tmp_path: Path):
    """A whole single month must still yield exactly the monthly amount (multiplier 1)."""
    from ledger.planning import BudgetService
    from ledger.storage.database import init_database

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    svc = BudgetService(db_path)
    svc.replace_budgets([{"month": "*", "category": "Groceries", "amount": "300.00", "notes": ""}])

    rows = svc.budget_variance(date(2026, 5, 1), date(2026, 5, 31))
    groceries = next(r for r in rows if r["category"] == "Groceries")
    assert groceries["budget"] == Decimal("300.00")


# --- #8: duplicate category name raises ConfigError, not raw IntegrityError ---


def test_duplicate_category_in_toml_raises_config_error(tmp_path: Path):
    import pytest

    from ledger.config.loader import load_categories
    from ledger.core.exceptions import ConfigError

    toml = tmp_path / "categories.toml"
    toml.write_text(
        'version = "test"\n'
        '[[category]]\ncategory_norm = "Dining"\nmerchants = ["A"]\n'
        '[[category]]\ncategory_norm = "Dining"\nmerchants = ["B"]\n',
        encoding="utf-8",
    )

    with pytest.raises(ConfigError) as excinfo:
        load_categories(toml)
    assert "Dining" in str(excinfo.value)


def test_categories_config_rejects_duplicate_name():
    import pytest

    from ledger.config import CategoriesConfig, CategoryRule

    with pytest.raises(ValueError, match="Duplicate category"):
        CategoriesConfig(
            version="test",
            categories=[
                CategoryRule(name="X", merchants=[]),
                CategoryRule(name="X", merchants=["A"]),
            ],
        )


# --- #18: a legacy resolved-incomplete row must not block saving other edits --


def _review_row(key, status, merchant, category, count=1):
    return {
        "review_key": key,
        "status": status,
        "merchant": merchant,
        "category": category,
        "scope": "merchant",
        "note": "",
        "count": count,
    }


def test_review_preview_untouched_incomplete_row_does_not_block():
    from ledger.ui.pages.review_queue import _review_decision_preview

    # A legacy resolved-but-incomplete row (no category) the user did NOT touch,
    # alongside a pending row the user resolves correctly.
    original = [
        _review_row("legacy", "resolved", "STORE", ""),
        _review_row("edit", "pending", "", ""),
    ]
    edited = [
        _review_row("legacy", "resolved", "STORE", ""),  # untouched
        _review_row("edit", "resolved", "CAFE", "Dining"),  # valid edit
    ]

    preview = _review_decision_preview(original, edited)
    assert preview["errors"] == [], f"untouched legacy row blocked save: {preview}"
    assert preview["changed"] == 1


def test_review_preview_edited_incomplete_row_still_blocks():
    from ledger.ui.pages.review_queue import _review_decision_preview

    original = [_review_row("edit", "pending", "", "")]
    edited = [_review_row("edit", "resolved", "CAFE", "")]  # resolved, missing category

    preview = _review_decision_preview(original, edited)
    assert preview["errors"], "an edited resolved row missing category must block save"


# --- #79: validation warnings must not embed raw description/amount ----------


# --- #24: validate_database failure branches (previously never exercised) -----


def test_validate_database_reports_missing_table(tmp_path: Path, monkeypatch):
    import sqlite3

    import ledger.storage.migrations as migrations
    from ledger.storage.database import clear_engine_cache

    db_path = tmp_path / "ledger.db"
    migrations.init_database(db_path)
    with contextlib.closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("DROP TABLE transactions")
    clear_engine_cache()
    # Stop validate_database from auto-recreating the dropped table so the
    # missing-table branch is actually reachable.
    monkeypatch.setattr(migrations, "init_database", lambda _p: None)

    result = migrations.validate_database(db_path, record=False)

    assert result.status == "failed"
    assert any("Missing table" in e and "transactions" in e for e in result.errors), result.errors


def test_validate_database_reports_corruption(tmp_path: Path, monkeypatch):
    import ledger.storage.migrations as migrations
    from ledger.storage.database import clear_engine_cache

    db_path = tmp_path / "ledger.db"
    migrations.init_database(db_path)
    # Zero out the file body so schema reflection and/or the integrity PRAGMA fail.
    data = bytearray(db_path.read_bytes())
    for i in range(100, len(data)):
        data[i] = 0
    db_path.write_bytes(bytes(data))
    clear_engine_cache()
    # Don't let validate_database re-create the schema over the corrupt file.
    monkeypatch.setattr(migrations, "init_database", lambda _p: None)

    # Must report failed (not raise) on a corrupt DB.
    result = migrations.validate_database(db_path, record=False)
    assert result.status == "failed"
    assert result.errors


# --- #68: register_parser must reject a FORMAT_NAME collision -----------------


def test_register_parser_rejects_format_name_collision(monkeypatch):
    from ledger.parsers import registry
    from ledger.parsers.base import BaseParser

    monkeypatch.setattr(registry, "_PARSERS", {})

    @registry.register_parser
    class FirstParser(BaseParser):
        FORMAT_NAME = "fictional-collision"
        INSTITUTION = "fictional"
        REQUIRED_HEADERS = {"x"}

        def parse_row(self, row, row_num):
            return None

    with pytest.raises(ValueError, match="collision"):

        @registry.register_parser
        class SecondParser(FirstParser):
            pass


# --- H4 (2026-07-02 audit): review-resolve must recompute exclude_from_spending ---


def _seed_categories_with_excluded(db_path: Path) -> None:
    """Seed a category config where 'Income' is excluded from spending and
    'Dining' is not — the source of truth apply_review_decisions must consult."""
    SQLiteConfigRepository(db_path).replace_from_configs(
        MerchantRulesConfig(version="test", rules=[]),
        CategoriesConfig(
            version="test",
            default_category="Other",
            categories=[
                CategoryRule(name="Dining", merchants=[]),
                CategoryRule(name="Income", merchants=[], exclude_from_spending=True),
            ],
        ),
    )


def _resolve_review_into(db_path: Path, txn_id: str, merchant: str, category: str) -> int:
    seed_review_candidates(
        db_path,
        [
            {
                "key": f"merchant:{merchant}",
                "merchant": merchant,
                "current_category": "Other",
                "txn_ids": [txn_id],
                "examples": [{"description": merchant}],
                "count": 1,
                "total_abs_amount": Decimal("40.00"),
            }
        ],
    )
    rows = get_review_rows(db_path)
    rows[0]["status"] = "resolved"
    rows[0]["merchant"] = merchant
    rows[0]["category"] = category
    save_review_rows(db_path, rows)
    return apply_review_decisions(db_path)


def test_review_resolve_into_excluded_category_sets_exclude_flag(tmp_path: Path):
    """Resolving a review row into an excluded category (Income) must set
    exclude_from_spending=True on the transaction AND drop it from
    get_spending_by_category — not leave it silently counted as spending."""
    db_path = tmp_path / "ledger.db"
    _write_txns(db_path, [_make_txn(1, amount=Decimal("-40.00"), merchant="REFUND SRC")])
    _seed_categories_with_excluded(db_path)

    assert _resolve_review_into(db_path, "rem_txn_1", "REFUND SRC", "Income") == 1

    with session_scope(db_path) as session:
        row = session.query(TransactionDB).filter_by(txn_id="rem_txn_1").one()
        assert bool(row.exclude_from_spending) is True, (
            "resolving into an excluded category left exclude_from_spending unset"
        )

    by_cat = LedgerQueryService(db_path).get_spending_by_category(
        date(2025, 4, 1), date(2025, 4, 30)
    )
    assert "Income" not in by_cat, f"excluded-category row still counted as spend: {by_cat}"


def test_review_resolve_out_of_excluded_category_clears_exclude_flag(tmp_path: Path):
    """Re-categorizing a row OUT of an excluded category into a spending one must
    clear exclude_from_spending so it re-enters spending."""
    db_path = tmp_path / "ledger.db"
    txn = _make_txn(2, amount=Decimal("-25.00"), merchant="CAFE X")
    _write_txns(db_path, [txn])
    _seed_categories_with_excluded(db_path)

    # Start it excluded (as if a prior resolve put it in Income).
    with session_scope(db_path) as session:
        row = session.query(TransactionDB).filter_by(txn_id="rem_txn_2").one()
        row.exclude_from_spending = True

    assert _resolve_review_into(db_path, "rem_txn_2", "CAFE X", "Dining") == 1

    with session_scope(db_path) as session:
        row = session.query(TransactionDB).filter_by(txn_id="rem_txn_2").one()
        assert bool(row.exclude_from_spending) is False, (
            "re-categorizing into a spending category left the row excluded"
        )
    by_cat = LedgerQueryService(db_path).get_spending_by_category(
        date(2025, 4, 1), date(2025, 4, 30)
    )
    assert by_cat.get("Dining") == Decimal("25.00")


# --- F16 (2026-07-03 fifth-pass audit): review-apply must derive merchant
# ---     exclusion from the same find_merchant pattern-match rules-apply uses,
# ---     not a byte-exact lookup on the resolved display name. -----------------


def _seed_excluding_merchant_rule(db_path: Path) -> None:
    """Config where an e-transfer merchant RULE excludes from spending by PATTERN
    (``E-TRANSFER``), while the plain 'Dining' category a user resolves into does
    not. Parity with ``RuleApplicationService.apply_all`` requires review-apply to
    honour the rule's pattern match against the raw txn text."""
    SQLiteConfigRepository(db_path).replace_from_configs(
        MerchantRulesConfig(
            version="test",
            rules=[
                MerchantRule(
                    merchant_norm="INTERAC ETRANSFER",
                    category="Transfers",
                    patterns=[r"E-TRANSFER"],
                    exclude_from_spending=True,
                )
            ],
        ),
        CategoriesConfig(
            version="test",
            default_category="Other",
            categories=[
                CategoryRule(name="Dining", merchants=[]),
                CategoryRule(
                    name="Transfers",
                    merchants=["INTERAC ETRANSFER"],
                    exclude_from_spending=True,
                ),
            ],
        ),
    )


def test_review_resolve_merchant_rule_pattern_excludes_despite_different_display_name(
    tmp_path: Path,
):
    """F16: when the raw txn text matches an excluding merchant RULE by pattern, a
    manual resolution to a DIFFERENT display merchant and a NON-excluded category
    ('Dining') must still set exclude_from_spending=True — full parity with
    RuleApplicationService, which derives merchant exclusion from find_merchant
    against the raw text rather than a byte-exact display-name lookup."""
    db_path = tmp_path / "ledger.db"
    txn = _make_txn(
        16,
        amount=Decimal("-40.00"),
        merchant="RAW ETRANSFER",
        merchant_raw="INTERAC E-TRANSFER SENT 8842",
    )
    _write_txns(db_path, [txn])
    _seed_excluding_merchant_rule(db_path)

    # 'Money to Alex' is not byte-identical to the rule's 'INTERAC ETRANSFER'
    # merchant_norm, and 'Dining' is not excluded — only the merchant RULE's pattern
    # match against the raw text can drive the exclusion.
    assert _resolve_review_into(db_path, "rem_txn_16", "Money to Alex", "Dining") == 1

    with session_scope(db_path) as session:
        row = session.query(TransactionDB).filter_by(txn_id="rem_txn_16").one()
        assert bool(row.exclude_from_spending) is True, (
            "review-apply ignored the excluding merchant rule the raw text matches "
            "by pattern (F16: must reach parity with rules-apply's find_merchant)"
        )


def test_review_resolve_exclusion_survives_rules_apply(tmp_path: Path):
    """After resolve sets exclude_from_spending, the transaction is MANUAL so the
    rules-apply pass must skip it and NOT re-count it into spending."""
    db_path = tmp_path / "ledger.db"
    _write_txns(db_path, [_make_txn(3, amount=Decimal("-40.00"), merchant="REFUND SRC")])
    _seed_categories_with_excluded(db_path)

    assert _resolve_review_into(db_path, "rem_txn_3", "REFUND SRC", "Income") == 1

    RuleApplicationService(db_path).apply_all(source="test", refresh_analytics=False)

    with session_scope(db_path) as session:
        row = session.query(TransactionDB).filter_by(txn_id="rem_txn_3").one()
        assert bool(row.exclude_from_spending) is True, (
            "rules-apply pass un-excluded a manually-resolved excluded row"
        )
    by_cat = LedgerQueryService(db_path).get_spending_by_category(
        date(2025, 4, 1), date(2025, 4, 30)
    )
    assert "Income" not in by_cat


def test_review_resolve_honours_merchant_level_exclusion(tmp_path: Path):
    """A merchant rule with exclude_from_spending=True must exclude a resolved row
    even when its CATEGORY is not excluded — matching rules/service.py (which ORs the
    merchant-rule exclusion with the category exclusion). Otherwise the same
    merchant+category is excluded by rules-apply but counted by review-apply."""
    db_path = tmp_path / "ledger.db"
    _write_txns(db_path, [_make_txn(7, amount=Decimal("-500.00"), merchant="REIMBURSE")])
    # 'Dining' is NOT category-excluded; the merchant rule IS exclude=True.
    SQLiteConfigRepository(db_path).replace_from_configs(
        MerchantRulesConfig(
            version="test",
            rules=[
                MerchantRule(
                    merchant_norm="REIMBURSE",
                    category="Dining",
                    patterns=["REIMBURSE"],
                    exclude_from_spending=True,
                )
            ],
        ),
        CategoriesConfig(
            version="test",
            default_category="Other",
            categories=[CategoryRule(name="Dining", merchants=[])],
        ),
    )

    assert _resolve_review_into(db_path, "rem_txn_7", "REIMBURSE", "Dining") == 1

    with session_scope(db_path) as session:
        row = session.query(TransactionDB).filter_by(txn_id="rem_txn_7").one()
        assert bool(row.exclude_from_spending) is True, (
            "merchant-level exclusion was dropped on review-resolve"
        )
    by_cat = LedgerQueryService(db_path).get_spending_by_category(
        date(2025, 4, 1), date(2025, 4, 30)
    )
    assert "Dining" not in by_cat, "merchant-excluded reimbursement wrongly inflated spend"


def test_review_resolve_leaves_exclusion_untouched_when_config_unavailable(
    tmp_path: Path, monkeypatch
):
    """If the runtime config cannot load, review-apply must NOT force
    exclude_from_spending=False (which would un-exclude a correctly-excluded row); it
    leaves the existing flag alone."""
    db_path = tmp_path / "ledger.db"
    _write_txns(db_path, [_make_txn(8, amount=Decimal("-40.00"), merchant="REFUND SRC")])
    _seed_categories_with_excluded(db_path)
    # Pre-exclude the row (as a prior correct resolution would have).
    with session_scope(db_path) as session:
        session.query(TransactionDB).filter_by(
            txn_id="rem_txn_8"
        ).one().exclude_from_spending = True

    import ledger.import_review as ir

    def _boom(_self):
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(ir.SQLiteConfigRepository, "load_categories", _boom)

    assert _resolve_review_into(db_path, "rem_txn_8", "REFUND SRC", "Dining") == 1

    with session_scope(db_path) as session:
        row = session.query(TransactionDB).filter_by(txn_id="rem_txn_8").one()
        assert bool(row.exclude_from_spending) is True, (
            "config-load failure silently un-excluded a correctly-excluded row"
        )


pytestmark = pytest.mark.usefixtures("fictional_adapter_metadata")
