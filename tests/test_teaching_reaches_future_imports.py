"""D21/D22: a taught rule must match what a future import actually sees, and the
confirm-step preview must show the number that will actually change.

**Derivation (D21).** ``derive_rule_suggestion`` built the pattern from the RESOLVED
DISPLAY NAME whenever ``transactions.merchant`` was non-empty. ``find_merchant``
pattern-matches RAW statement text only (``parsers/base.py`` never passes
``display_name``), so the label only matched by luck. ``CompiledPattern.matches`` uses
``.search()`` with ``IGNORECASE``, so it matched whenever the label happened to be a
case-insensitive substring of the raw text. The fix derives the pattern from
``merchant_raw``/``description`` via ``_clean_identifier``, never from the label, and
removes statement-varying references, store identifiers, and location suffixes.

**The preview (D22).** ``preview_match_count`` passed the resolved display name
POSITIONALLY as a third raw text — pattern-matched, a semantic NEITHER real path uses —
and applied no confidence filter while ``apply_all`` skips MANUAL rows. Synthetic
label/raw mismatches demonstrate that the old preview could count rows that import would
not match. The UI rendered that as "Will recategorize N transaction(s)".

The whole-cycle proof that a taught rule reaches a real re-import lives in
``tests/test_review_teach.py``'s round trip; this file pins the two semantics it rests on.
"""

from __future__ import annotations

import os
import textwrap
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from ledger.config import AppConfig
from ledger.config.repository import SQLiteConfigRepository
from ledger.config.schemas import (
    CategoriesConfig,
    CategoryRule,
    MerchantRule,
    MerchantRulesConfig,
)
from ledger.core import TransactionBuilder
from ledger.parsers.base import BaseParser
from ledger.rules.categorization import (
    CategorizationService,
    derive_rule_suggestion,
    upsert_merchant_rule,
)
from ledger.storage.database import TransactionDB, clear_engine_cache, session_scope

# Synthetic (label, merchant_raw, description) shapes representing supported statement
# formats. Empty ``merchant_raw`` exercises derivation from ``description``.
SYNTHETIC_SHAPES = [
    ("UBER EATS", "", "UBER CANADA/UBEREATS FIRSTCITY ON"),
    (
        "UBER EATS",
        "UBER CANADA/UBEREATS FIRSTCITY ON",
        "UBER CANADA/UBEREATS FIRSTCITY ON (Purchase)",
    ),
    ("WALMART", "", "WAL-MART SUPERCENTER#9001FIRSTCITY ON"),
    ("WENDYS", "", "WENDY'S #9002 FIRSTCITY ON"),
    ("APPLE SERVICES", "", "APPLE.COM/BILL FIRSTCITY ON"),
]


def _production_import_match(
    sample_app_config: AppConfig,
    config: MerchantRulesConfig,
    merchant_raw: str,
    description: str,
) -> str:
    """Resolve through the production parser wiring, not a local lookup reimplementation."""
    app_config = sample_app_config.model_copy(update={"merchants": config})
    parser = SimpleNamespace(config=app_config)
    enriched = BaseParser._enrich(
        parser,
        TransactionBuilder(),
        merchant_raw,
        "",
        description,
    )
    return str(enriched.to_dict().get("merchant") or "")


# ── D21: derivation ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(("label", "merchant_raw", "description"), SYNTHETIC_SHAPES)
def test_a_taught_rule_matches_the_raw_text_a_future_import_sees(
    sample_app_config: AppConfig, label: str, merchant_raw: str, description: str
) -> None:
    suggestion = derive_rule_suggestion(label, merchant_raw, description, "Dining")
    config = upsert_merchant_rule(
        MerchantRulesConfig(rules=[]),
        suggestion.merchant_norm,
        suggestion.pattern,
        suggestion.category,
    )

    assert _production_import_match(sample_app_config, config, merchant_raw, description) == label


def test_a_platform_channel_rule_cannot_claim_an_unrelated_merchant(
    sample_app_config: AppConfig,
) -> None:
    """Deleting merchant identity down to a delivery-channel prefix must fail this test."""
    suggestion = derive_rule_suggestion(
        "SYNTHETIC PIZZA",
        "DD/DOORDASHSYNTHETIC PIZZA FIRSTCITY ON",
        "",
        "Dining",
    )
    config = upsert_merchant_rule(
        MerchantRulesConfig(rules=[]),
        suggestion.merchant_norm,
        suggestion.pattern,
        suggestion.category,
    )

    assert (
        _production_import_match(
            sample_app_config,
            config,
            "DD/DOORDASHSYNTHETIC SUSHI SECONDCITY ON",
            "",
        )
        != "SYNTHETIC PIZZA"
    )


def test_an_etransfer_rule_keeps_counterparty_identity_in_production_wiring(
    sample_app_config: AppConfig,
) -> None:
    suggestion = derive_rule_suggestion(
        "PAYEE JANE",
        "",
        "E-TRANSFER 12345 JANE DOE",
        "Other",
    )
    config = upsert_merchant_rule(
        MerchantRulesConfig(rules=[]),
        suggestion.merchant_norm,
        suggestion.pattern,
        suggestion.category,
    )

    assert "JANE" in suggestion.pattern
    assert (
        _production_import_match(
            sample_app_config,
            config,
            "",
            "E-TRANSFER 98765 JOHN SMITH",
        )
        != "PAYEE JANE"
    )


def test_short_etransfer_counterparty_is_channel_and_token_bounded_in_production(
    sample_app_config: AppConfig,
) -> None:
    suggestion = derive_rule_suggestion(
        "PAYEE ANN",
        "INTERAC E-TRANSFER SENT 8842",
        "Money to Ann",
        "Other",
    )
    config = upsert_merchant_rule(
        MerchantRulesConfig(rules=[]),
        suggestion.merchant_norm,
        suggestion.pattern,
        suggestion.category,
    )

    assert "TRANSFER" in suggestion.pattern.upper()
    assert (
        _production_import_match(
            sample_app_config,
            config,
            "INTERAC E-TRANSFER SENT 9917",
            "Money to Ann",
        )
        == "PAYEE ANN"
    )
    for raw, description in (
        ("INTERAC E-TRANSFER SENT 1122", "Money to Joanna"),
        ("INTERAC E-TRANSFER SENT 2233", "Money to Bob"),
        ("", "ANNABEL BOOKS"),
        ("", "ANN BOOKS"),
    ):
        assert _production_import_match(sample_app_config, config, raw, description) != "PAYEE ANN"


def test_etransfer_counterparty_context_is_finitely_bounded_in_production(
    sample_app_config: AppConfig,
) -> None:
    suggestion = derive_rule_suggestion(
        "PAYEE ANN",
        "INTERAC E-TRANSFER SENT 8842",
        "Money to Ann",
        "Other",
    )
    config = upsert_merchant_rule(
        MerchantRulesConfig(rules=[]),
        suggestion.merchant_norm,
        suggestion.pattern,
        suggestion.category,
    )
    within_bound = "INTERAC E-TRANSFER SENT 9917 " + ("X" * 140) + " ANN"
    beyond_bound = "INTERAC E-TRANSFER SENT 9917 " + ("X" * 161) + " ANN"

    assert _production_import_match(sample_app_config, config, within_bound, "") == "PAYEE ANN"
    assert _production_import_match(sample_app_config, config, beyond_bound, "") != "PAYEE ANN"


@pytest.mark.parametrize(
    ("merchant_raw", "description", "counterparty", "unrelated_raw", "unrelated_description"),
    [
        (
            "INTERAC E-TRANSFER SENT 8842",
            "Money to Alex",
            "ALEX",
            "INTERAC E-TRANSFER SENT 9917",
            "Money to Bob",
        ),
        ("", "E-TRANSFER 12345 JANE DOE", "JANE", "", "E-TRANSFER 99881 JOHN SMITH"),
        (
            "",
            "ETRANSFER RECEIVED 7319 FROM ALICE",
            "ALICE",
            "",
            "ETRANSFER RECEIVED 4421 FROM CAROL",
        ),
        ("", "INTERAC E-TRANSFER TO BOB", "BOB", "", "INTERAC E-TRANSFER TO DANA"),
        ("", "E-TRANSFER OUT 1234 CASEY", "CASEY", "", "E-TRANSFER OUT 5678 MORGAN"),
        ("", "E-TRANSFER IN 8765 TAYLOR", "TAYLOR", "", "E-TRANSFER IN 4321 JORDAN"),
    ],
)
def test_supported_etransfer_shapes_keep_the_raw_counterparty_namespace(
    sample_app_config: AppConfig,
    merchant_raw: str,
    description: str,
    counterparty: str,
    unrelated_raw: str,
    unrelated_description: str,
) -> None:
    suggestion = derive_rule_suggestion(f"PAYEE {counterparty}", merchant_raw, description, "Other")
    config = upsert_merchant_rule(
        MerchantRulesConfig(rules=[]),
        suggestion.merchant_norm,
        suggestion.pattern,
        suggestion.category,
    )

    assert counterparty in suggestion.pattern
    assert (
        _production_import_match(sample_app_config, config, merchant_raw, description)
        == f"PAYEE {counterparty}"
    )
    assert (
        _production_import_match(
            sample_app_config,
            config,
            unrelated_raw,
            unrelated_description,
        )
        != f"PAYEE {counterparty}"
    )


def test_etransfer_derivation_refuses_when_no_raw_counterparty_is_distinguishable() -> None:
    with pytest.raises(ValueError, match="cannot identify an e-transfer counterparty"):
        derive_rule_suggestion(
            "DISPLAY PAYEE",
            "INTERAC E-TRANSFER SENT 8842",
            "",
            "Other",
        )


def test_automatic_derivation_refuses_when_raw_statement_text_is_absent() -> None:
    """Restoring the resolved-label fallback must fail this test (D21)."""
    with pytest.raises(ValueError, match="raw statement text"):
        derive_rule_suggestion("LABEL ONLY", "", "", "Other")


@pytest.mark.parametrize(
    ("label", "observed", "future"),
    [
        ("TIM HORTONS", "TIM HORTONS #1001 FIRSTCITY ON", "TIM HORTONS #2002 SECONDCITY ON"),
        (
            "COSTCO",
            "COSTCO WHOLESALE W101 FIRSTCITY ON",
            "COSTCO WHOLESALE W202 SECONDCITY ON",
        ),
        ("NETFLIX", "NETFLIX.COM 800-111-2222 ON", "NETFLIX.COM SECONDCITY ON (Purchase)"),
        (
            "APPLE SERVICES",
            "APPLE.COM/CA 800-111-2222 ON",
            "APPLE.COM/CA SECONDCITY ON (Purchase)",
        ),
        (
            "SYNTHETIC DELIVERY",
            "DD/DOORDASHFIRSTPLACE FIRSTCITY BC",
            "DD/DOORDASHFIRSTPLACE SECONDCITY BC",
        ),
    ],
)
def test_a_derived_pattern_generalizes_across_statement_varying_suffixes(
    sample_app_config: AppConfig, label: str, observed: str, future: str
) -> None:
    """Changing the store, phone, fulfilment partner, or city must not bring the
    merchant back to the review queue on its next statement."""
    suggestion = derive_rule_suggestion(label, observed, observed, "Dining")
    config = upsert_merchant_rule(
        MerchantRulesConfig(rules=[]),
        suggestion.merchant_norm,
        suggestion.pattern,
        suggestion.category,
    )

    assert _production_import_match(sample_app_config, config, future, future) == label


@pytest.mark.parametrize(("label", "merchant_raw", "description"), SYNTHETIC_SHAPES)
def test_the_label_itself_would_not_have_matched(
    sample_app_config: AppConfig, label: str, merchant_raw: str, description: str
) -> None:
    """The reason the fix is needed, stated as an executable fact rather than a claim.

    These are exactly the merchants whose label never appears in their raw text, so the
    old label-derived pattern produced a rule that could not fire at import.
    """
    label_derived = upsert_merchant_rule(
        MerchantRulesConfig(rules=[]), label, label.replace("(", r"\("), "Dining"
    )

    assert (
        _production_import_match(sample_app_config, label_derived, merchant_raw, description)
        != label
    )


def test_the_suggested_merchant_norm_is_still_the_resolved_label() -> None:
    """Only the PATTERN moves to raw text. The label stays the rule's key, so an upsert
    lands on the merchant's existing hand-authored rule and APPENDS to its patterns
    instead of creating a near-duplicate under a raw-text name."""
    suggestion = derive_rule_suggestion(
        "UBER EATS", "", "UBER CANADA/UBEREATS FIRSTCITY ON", "Dining"
    )

    assert suggestion.merchant_norm == "UBER EATS"

    existing = MerchantRulesConfig(
        rules=[MerchantRule(merchant_norm="UBER EATS", category="Dining", patterns=["UBEREATS"])]
    )
    updated = upsert_merchant_rule(
        existing, suggestion.merchant_norm, suggestion.pattern, suggestion.category
    )
    assert len(updated.rules) == 1
    assert updated.rules[0].patterns[0] == "UBEREATS"
    assert suggestion.pattern in updated.rules[0].patterns


def test_a_merchant_whose_own_name_contains_purchase_is_not_eaten() -> None:
    """The reference strip requires the institution token (interac / visa debit /
    mobile cheque), so a merchant whose own name reads like a transaction type survives."""
    suggestion = derive_rule_suggestion("", "", "SYNTHETIC PURCHASE - 4400 FIRSTCITY", "Other")

    assert suggestion.merchant_norm == "SYNTHETIC PURCHASE"


def test_an_unknown_merchant_still_strips_processor_noise_and_store_numbers(
    sample_app_config: AppConfig,
) -> None:
    """Regression: the unknown-merchant path is unchanged by D21."""
    suggestion = derive_rule_suggestion("", "SQ *BLUE BOTTLE 00123", "SQ *BLUE BOTTLE 00123", "x")

    assert suggestion.merchant_norm == "BLUE BOTTLE"
    config = upsert_merchant_rule(
        MerchantRulesConfig(rules=[]), suggestion.merchant_norm, suggestion.pattern, "Dining"
    )
    assert (
        _production_import_match(sample_app_config, config, "SQ *BLUE BOTTLE 00999", "")
        == "BLUE BOTTLE"
    )


# ── D22: the preview ──────────────────────────────────────────────────────────


def _temporal(posting_date: date) -> dict:
    return {
        "day_of_week": posting_date.weekday(),
        "day_of_month": posting_date.day,
        "week_of_year": posting_date.isocalendar()[1],
        "is_weekend": posting_date.weekday() >= 5,
        "is_month_start": posting_date.day <= 3,
        "is_month_end": posting_date.day >= 28,
    }


def _insert(
    db_path: Path,
    txn_id: str,
    description: str,
    *,
    merchant_raw: str = "",
    merchant: str = "",
    category: str = "Other",
    confidence: str = "none",
) -> None:
    posted = date(2026, 5, 1)
    with session_scope(db_path) as session:
        session.add(
            TransactionDB(
                txn_id=txn_id,
                posting_date=posted,
                transaction_date=posted,
                institution="test-card",
                account="123456789",
                account_type="credit",
                flow="expense",
                cashflow_type="expense",
                include_in_cashflow=True,
                currency="CAD",
                amount_raw=Decimal("10.00"),
                amount_signed=Decimal("-10.00"),
                description=description,
                merchant=merchant,
                category=category,
                merchant_confidence=confidence,
                category_confidence=confidence,
                exclude_from_spending=False,
                merchant_raw=merchant_raw,
                category_raw="",
                source_file="statement.csv",
                source_path="/private/statement.csv",
                source_period="2026-05",
                source_row=1,
                imported_at=datetime.now(UTC).replace(tzinfo=None),
                parser_version="test",
                txn_fingerprint=f"{txn_id}-fp",
                **_temporal(posted),
            )
        )


def _seed(db_path: Path) -> None:
    SQLiteConfigRepository(db_path).replace_from_configs(
        MerchantRulesConfig(version="test", rules=[]),
        CategoriesConfig(
            version="test",
            categories=[CategoryRule(name="Other"), CategoryRule(name="Dining")],
        ),
    )


def test_combined_raw_evidence_rule_has_identical_preview_and_apply_semantics(
    tmp_path: Path,
) -> None:
    db = tmp_path / "combined-transfer.db"
    _seed(db)
    _insert(
        db,
        "combined",
        "Money to Ann",
        merchant_raw="INTERAC E-TRANSFER SENT 8842",
    )
    pattern = (
        r"(?:^|\s)(?:INTERAC\s+)?E-?TRANSFER\b"
        r"(?=[^\r\n]{0,160}(?<![A-Z0-9])ANN(?![A-Z0-9]))"
    )
    service = CategorizationService(db)

    predicted = service.preview_match_count(
        pattern,
        merchant_norm="PAYEE ANN",
        category="Dining",
    )
    result = service.remember_category(
        merchant_norm="PAYEE ANN",
        pattern=pattern,
        category="Dining",
        refresh_analytics=False,
    )

    assert predicted == 1
    assert result.details["taught_transactions_updated"] == 1
    with session_scope(db) as session:
        assert session.get(TransactionDB, "combined").category == "Dining"


def _uber_ledger(tmp_path: Path) -> Path:
    db = tmp_path / "ledger.db"
    _seed(db)
    # Raw text only: this is what a rules pass matches on.
    _insert(db, "raw", "UBER CANADA/UBEREATS FIRSTCITY ON", merchant="UBER EATS")
    # Same raw text, but frozen by a hand fix: apply_all skips it permanently.
    _insert(
        db,
        "manual",
        "UBER CANADA/UBEREATS FIRSTCITY ON",
        merchant="UBER EATS",
        confidence="manual",
    )
    # Carries the resolved label but no matching raw text: resolved by IDENTITY only.
    _insert(db, "label_only", "SOMETHING ELSE ENTIRELY", merchant="UBER EATS")
    _insert(db, "unrelated", "STARBUCKS 55")
    return db


def test_the_preview_does_not_pattern_match_the_resolved_label(tmp_path: Path) -> None:
    """A pattern matching only resolved labels must not count raw descriptions.

    The synthetic fixture has three labeled rows but no matching raw description.
    """
    db = _uber_ledger(tmp_path)

    assert CategorizationService(db).preview_match_count("UBER EATS") == 0


def test_preview_is_read_only_and_preserves_database_permissions(tmp_path: Path) -> None:
    db = _uber_ledger(tmp_path)
    db.chmod(0o644)
    clear_engine_cache()

    preview = CategorizationService(db).preview_rule_update(
        "UBER CANADA/UBEREATS", merchant_norm="UBER EATS", category="Dining"
    )

    assert preview.transactions_updated == 2
    assert db.stat().st_mode & 0o777 == 0o644


def test_preview_succeeds_against_an_owner_read_only_complete_database(tmp_path: Path) -> None:
    db = _uber_ledger(tmp_path)
    db.chmod(0o400)
    clear_engine_cache()
    try:
        preview = CategorizationService(db).preview_rule_update(
            "UBER CANADA/UBEREATS", merchant_norm="UBER EATS", category="Dining"
        )
    finally:
        db.chmod(0o600)
        clear_engine_cache()

    assert preview.transactions_updated == 2


def test_preview_does_not_create_a_missing_database(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"

    with pytest.raises(FileNotFoundError):
        CategorizationService(missing).preview_rule_update(
            "SYNTHETIC", merchant_norm="SYNTHETIC", category="Other"
        )

    assert not missing.exists()


def test_the_preview_excludes_manual_rows_because_a_rules_pass_skips_them(
    tmp_path: Path,
) -> None:
    db = _uber_ledger(tmp_path)

    count = CategorizationService(db).preview_match_count("UBER CANADA/UBEREATS")

    assert count == 1, "the manual row is frozen; a rules pass will never rewrite it"


def test_the_preview_counts_a_label_resolved_by_identity(tmp_path: Path) -> None:
    """``find_merchant``'s display-name fallback is part of the applier's semantics, so
    the preview has to model it — with the merchant being taught, not a placeholder."""
    db = _uber_ledger(tmp_path)
    service = CategorizationService(db)

    assert (
        service.preview_match_count(
            "UBER CANADA/UBEREATS", merchant_norm="UBER EATS", category="Dining"
        )
        == 2
    )
    assert (
        service.preview_match_count(
            "UBER CANADA/UBEREATS", merchant_norm="OTHER", category="Dining"
        )
        == 1
    )


def test_the_preview_honours_first_match_precedence_in_the_updated_full_ruleset(
    tmp_path: Path,
) -> None:
    db = tmp_path / "ledger.db"
    SQLiteConfigRepository(db).replace_from_configs(
        MerchantRulesConfig(
            version="test",
            rules=[MerchantRule(merchant_norm="UBER", category="Transport", patterns=["UBER"])],
        ),
        CategoriesConfig(
            version="test",
            categories=[
                CategoryRule(name="Other"),
                CategoryRule(name="Transport"),
                CategoryRule(name="Dining"),
            ],
        ),
    )
    _insert(
        db,
        "claimed",
        "UBER CANADA/UBEREATS FIRSTCITY ON",
        merchant="UBER",
        category="Transport",
        confidence="rule",
    )

    count = CategorizationService(db).preview_match_count(
        "UBER CANADA/UBEREATS", merchant_norm="UBER EATS", category="Dining"
    )

    assert count == 0, "the earlier UBER rule wins; the appended rule cannot update this row"


def test_the_preview_equals_what_saving_the_rule_actually_changes(tmp_path: Path) -> None:
    """The confirm step is the safety valve, so its number must be the number that moves."""
    db = _uber_ledger(tmp_path)
    service = CategorizationService(db)

    predicted = service.preview_match_count(
        "UBER CANADA/UBEREATS", merchant_norm="UBER EATS", category="Dining"
    )
    result = service.remember_category(
        merchant_norm="UBER EATS",
        pattern="UBER CANADA/UBEREATS",
        category="Dining",
        refresh_analytics=False,
    )

    assert result.ok, result.validation_issues
    assert result.details["taught_transactions_updated"] == predicted
    with session_scope(db) as session:
        assert session.get(TransactionDB, "raw").category == "Dining"
        assert session.get(TransactionDB, "label_only").category == "Dining"
        assert session.get(TransactionDB, "manual").category == "Other"
        assert session.get(TransactionDB, "unrelated").category == "Other"


def test_target_preview_and_result_ignore_unrelated_changes_from_the_full_rules_pass(
    tmp_path: Path,
) -> None:
    db = _uber_ledger(tmp_path)
    repo = SQLiteConfigRepository(db)
    repo.replace_from_configs(
        MerchantRulesConfig(
            version="test",
            rules=[
                MerchantRule(merchant_norm="STARBUCKS", category="Coffee", patterns=["STARBUCKS"])
            ],
        ),
        CategoriesConfig(
            version="test",
            categories=[
                CategoryRule(name="Other"),
                CategoryRule(name="Dining"),
                CategoryRule(name="Coffee"),
            ],
        ),
    )
    service = CategorizationService(db)

    predicted = service.preview_match_count(
        "UBER CANADA/UBEREATS", merchant_norm="UBER EATS", category="Dining"
    )
    result = service.remember_category(
        merchant_norm="UBER EATS",
        pattern="UBER CANADA/UBEREATS",
        category="Dining",
        refresh_analytics=False,
    )

    assert predicted == 2
    assert result.transactions_updated == 3, "the full pass also repairs the STARBUCKS row"
    assert result.details["taught_transactions_updated"] == 2


# ── D22: the sentence the operator actually reads ─────────────────────────────

_DRIVER = textwrap.dedent("""\
    from pathlib import Path
    from datetime import date
    import os

    from ledger.query import LedgerQueryService
    from ledger.config.repository import SQLiteConfigRepository
    from ledger.ui.context import AppContext
    from ledger.ui.pages import transactions
    from ledger.rules.categorization import CategorizationService

    _original_suggest = None
    if os.environ.get("_LEDGER_UNEXPECTED_DERIVATION_ERROR") == "1":
        _original_suggest = CategorizationService.suggest_for_transaction
        def _raise_unexpected(_self, _txn_id):
            raise ValueError("synthetic unexpected derivation error")
        CategorizationService.suggest_for_transaction = _raise_unexpected

    _db = Path(os.environ["_LEDGER_TEACHING_DB"])
    _service = LedgerQueryService(_db)
    _repo = SQLiteConfigRepository(_db)
    _ctx = AppContext(
        db_path=_db,
        service=_service,
        category_config=_repo.load_categories(),
        stats=_service.get_stats(),
        earliest=date(2026, 5, 1),
        latest=date(2026, 5, 1),
        start_date=date(2026, 5, 1),
        end_date=date(2026, 5, 1),
        period_label="May 2026",
        review_source={
            "label": "teaching.db",
            "status": "live_db",
            "db_path": _db,
            "source_kind": "Current DB",
        },
        review_db=_db,
        pending_reviews=0,
    )
    try:
        transactions.render(_ctx)
    finally:
        if _original_suggest is not None:
            CategorizationService.suggest_for_transaction = _original_suggest
""")


def _remember_mode_page(tmp_path: Path, db: Path):
    """Render the real Transactions page and switch its Fix-a-category scope to Remember."""
    from streamlit.testing.v1 import AppTest

    os.environ["_LEDGER_TEACHING_DB"] = str(db)
    script = tmp_path / "_driver_teaching.py"
    script.write_text(_DRIVER)
    page = AppTest.from_file(str(script), default_timeout=60)
    page.run()
    assert not page.exception, page.exception
    page.selectbox(key="recat_txn").set_value("raw")
    page.radio(key="recat_mode").set_value("Remember for this merchant")
    page.run()
    assert not page.exception, page.exception
    return page


def test_remember_mode_handles_missing_raw_text_and_accepts_explicit_evidence(
    tmp_path: Path,
) -> None:
    from streamlit.testing.v1 import AppTest

    db = tmp_path / "no-raw.db"
    _seed(db)
    _insert(db, "no_raw", "", merchant="DISPLAY LABEL")
    os.environ["_LEDGER_TEACHING_DB"] = str(db)
    script = tmp_path / "_driver_no_raw.py"
    script.write_text(_DRIVER)
    page = AppTest.from_file(str(script), default_timeout=60)
    page.run()
    page.radio(key="recat_mode").set_value("Remember for this merchant")
    page.run()

    assert not page.exception, page.exception
    assert any("raw statement evidence" in str(element.value) for element in page.info)
    page.text_input(key="recat_merchant").set_value("DISPLAY LABEL")
    page.text_input(key="recat_pattern").set_value("SYNTHETIC RAW EVIDENCE")
    page.run()
    assert not page.exception, page.exception
    page.button(key="recat_save").click().run()
    assert not page.exception, page.exception
    assert any(
        rule.merchant_norm == "DISPLAY LABEL" and "SYNTHETIC RAW EVIDENCE" in rule.patterns
        for rule in SQLiteConfigRepository(db).load_merchant_rules().rules
    )


def test_remember_mode_discloses_ambiguous_etransfer_and_keeps_explicit_inputs(
    tmp_path: Path,
) -> None:
    from streamlit.testing.v1 import AppTest

    db = tmp_path / "ambiguous-transfer.db"
    _seed(db)
    _insert(db, "ambiguous", "INTERAC E-TRANSFER SENT 8842", merchant="DISPLAY PAYEE")
    os.environ["_LEDGER_TEACHING_DB"] = str(db)
    script = tmp_path / "_driver_ambiguous.py"
    script.write_text(_DRIVER)
    page = AppTest.from_file(str(script), default_timeout=60)
    page.run()
    page.radio(key="recat_mode").set_value("Remember for this merchant")
    page.run()

    assert not page.exception, page.exception
    notices = [str(element.value) for element in page.info]
    assert any("does not identify a counterparty" in notice for notice in notices)
    assert not any("no raw statement evidence" in notice for notice in notices)
    page.text_input(key="recat_merchant").set_value("DISPLAY PAYEE")
    page.text_input(key="recat_pattern").set_value("SYNTHETIC RAW EVIDENCE")
    page.run()
    assert not page.exception, page.exception


def test_remember_mode_does_not_swallow_an_unexpected_value_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from streamlit.testing.v1 import AppTest

    db = tmp_path / "unexpected-error.db"
    _seed(db)
    _insert(db, "unexpected", "SYNTHETIC RAW", merchant="DISPLAY PAYEE")
    monkeypatch.setenv("_LEDGER_TEACHING_DB", str(db))
    monkeypatch.setenv("_LEDGER_UNEXPECTED_DERIVATION_ERROR", "1")
    script = tmp_path / "_driver_unexpected.py"
    script.write_text(_DRIVER)
    page = AppTest.from_file(str(script), default_timeout=60)
    page.run()
    page.radio(key="recat_mode").set_value("Remember for this merchant")
    page.run()

    assert page.exception
    assert any("synthetic unexpected derivation error" in str(exc.value) for exc in page.exception)


def test_the_pattern_help_text_describes_what_the_pattern_is_matched_against(
    tmp_path: Path,
) -> None:
    """The published sentence said "its description or merchant contains this", which is
    false for both real paths: the resolved merchant is never pattern-matched."""
    db = _uber_ledger(tmp_path)

    page = _remember_mode_page(tmp_path, db)

    help_texts = [str(widget.help or "") for widget in page.text_input]
    pattern_help = next(text for text in help_texts if text)
    assert (
        "At import and during a rules pass, the regex is searched in merchant_raw, description, "
        "and their concatenation." in pattern_help
    )
    assert "resolved merchant is compared by exact identity" in pattern_help


def test_the_rendered_match_count_is_the_previewed_count_for_this_merchant(
    tmp_path: Path,
) -> None:
    """Drives the page, not the formatter: the count the operator reads must come from a
    preview that knows which merchant is being taught. Drop the ``merchant_norm=``
    argument at the call site and this goes red."""
    db = _uber_ledger(tmp_path)

    page = _remember_mode_page(tmp_path, db)

    merchant = page.text_input(key="recat_merchant").value
    pattern = page.text_input(key="recat_pattern").value
    expected = CategorizationService(db).preview_match_count(
        pattern, merchant_norm=merchant, category="Dining"
    )
    assert expected == 2, (merchant, pattern, expected)

    captions = [str(element.value) for element in page.caption]
    assert any(f"**{expected}**" in caption for caption in captions), captions
    assert not any("Will recategorize" in caption for caption in captions), captions


def test_the_page_discloses_preserved_exclusion_before_and_after_save(
    tmp_path: Path,
) -> None:
    """Drive both operator sentences for D20's category-change case.

    Removing either exclusion disclosure from the real page must make this test red;
    checking only the service result would leave the published wording unguarded.
    """
    db = _uber_ledger(tmp_path)
    repo = SQLiteConfigRepository(db)
    repo.replace_from_configs(
        MerchantRulesConfig(
            version="test",
            rules=[
                MerchantRule(
                    merchant_norm="UBER EATS",
                    category="Other",
                    patterns=["LEGACY DOES NOT MATCH"],
                    exclude_from_spending=True,
                )
            ],
        ),
        repo.load_taxonomy(),
    )

    page = _remember_mode_page(tmp_path, db)
    page.selectbox(key="recat_cat").set_value("Dining").run()
    assert not page.exception, page.exception

    captions = [str(element.value) for element in page.caption]
    assert any("merchant rule stores excluded" in caption for caption in captions)
    assert any("effectively excluded" in caption for caption in captions)

    page.button(key="recat_save").click().run()
    assert not page.exception, page.exception
    successes = [str(element.value) for element in page.success]
    assert any("merchant rule stores excluded" in message for message in successes)
    assert any("effectively excluded" in message for message in successes)
    [rule] = repo.load_merchant_rules().rules
    assert rule.category == "Dining"
    assert rule.exclude_from_spending is True


def test_the_page_distinguishes_stored_inclusion_from_effective_category_exclusion(
    tmp_path: Path,
) -> None:
    """Conflating the stored merchant flag with the category-OR result must fail."""
    db = _uber_ledger(tmp_path)
    repo = SQLiteConfigRepository(db)
    taxonomy = repo.load_taxonomy()
    repo.replace_from_configs(
        MerchantRulesConfig(
            version="test",
            rules=[
                MerchantRule(
                    merchant_norm="UBER EATS",
                    category="Dining",
                    patterns=["LEGACY DOES NOT MATCH"],
                    exclude_from_spending=False,
                )
            ],
        ),
        CategoriesConfig(
            version=taxonomy.version,
            default_category=taxonomy.default_category,
            categories=[
                *taxonomy.categories,
                CategoryRule(name="Investing", exclude_from_spending=True),
            ],
        ),
    )

    page = _remember_mode_page(tmp_path, db)
    page.selectbox(key="recat_cat").set_value("Investing").run()
    assert not page.exception, page.exception

    captions = [str(element.value) for element in page.caption]
    assert any("merchant rule stores included" in caption for caption in captions)
    assert any("target category makes them effectively excluded" in caption for caption in captions)

    page.button(key="recat_save").click().run()
    assert not page.exception, page.exception
    successes = [str(element.value) for element in page.success]
    assert any("merchant rule stores included" in message for message in successes)
    assert any(
        "target category makes them effectively excluded" in message for message in successes
    )
    [rule] = repo.load_merchant_rules().rules
    assert rule.category == "Investing"
    assert rule.exclude_from_spending is False


def test_one_off_success_survives_exactly_one_streamlit_rerun(tmp_path: Path) -> None:
    """The mutation reruns immediately, so its confirmation must cross that rerun once."""
    from streamlit.testing.v1 import AppTest

    db = _uber_ledger(tmp_path)
    os.environ["_LEDGER_TEACHING_DB"] = str(db)
    script = tmp_path / "_driver_one_off_success.py"
    script.write_text(_DRIVER)
    page = AppTest.from_file(str(script), default_timeout=60)
    page.run()
    page.selectbox(key="recat_txn").set_value("raw")
    page.selectbox(key="recat_cat").set_value("Dining")
    page.run()

    page.button(key="recat_apply_one").click().run()

    assert not page.exception, page.exception
    assert [str(element.value) for element in page.success] == ["Set to Dining."]
    with session_scope(db) as session:
        transaction = session.get(TransactionDB, "raw")
        assert transaction is not None
        assert (transaction.category, transaction.category_confidence) == ("Dining", "manual")
    after_mutation = db.read_bytes()

    page.run()

    assert not page.exception, page.exception
    assert not any("Set to Dining." in str(element.value) for element in page.success)
    assert db.read_bytes() == after_mutation
