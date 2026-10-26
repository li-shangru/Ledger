"""F10, the spend-semantics guard: teaching must never silently flip what counts as spending.

``upsert_merchant_rule`` carried an existing rule's ``merchant_norm`` and ``patterns``
forward but rebuilt the frozen model from the ``exclude_from_spending`` PARAMETER, whose
default was ``False`` — and the only production caller
(``ui/pages/transactions.py``'s "Remember for this merchant") omits the kwarg. Executed
against the real code that turned ``LUMEN / Investing / exclude=True`` into
``exclude=False``. Persistence is delete-all-and-reinsert, so the flip lands in SQL, and
``remember_category`` then calls ``replace_and_apply``, which rewrites every transaction.

``rules/service.py`` ORs the merchant and category flags, so the merchant flag can be
masked while a rule points at an excluded category. It becomes decisive as soon as the
rule points at an included category or a manually frozen row retains another category.

D20: ``exclude_from_spending`` becomes ``bool | None = None``, where ``None`` PRESERVES
the stored flag. For a NEW rule ``None`` derives from the target category's own flag
rather than hardcoding ``False``. Changing a stored flag requires an explicit argument;
without it the call is refused and the refusal names the merchant, the stored value, and
why.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ledger.config.repository import SQLiteConfigRepository
from ledger.config.schemas import (
    CategoriesConfig,
    CategoryRule,
    MerchantRule,
    MerchantRulesConfig,
)
from ledger.rules.categorization import (
    CategorizationService,
    SpendSemanticsChangeRefused,
    upsert_merchant_rule,
)
from ledger.storage.database import TransactionDB, session_scope


def _lumen_config() -> MerchantRulesConfig:
    return MerchantRulesConfig(
        rules=[
            MerchantRule(
                merchant_norm="LUMEN",
                category="Investing",
                patterns=["LUMEN"],
                exclude_from_spending=True,
            )
        ]
    )


# ── the pure guard ────────────────────────────────────────────────────────────


def test_upsert_preserves_an_existing_exclude_flag_by_default() -> None:
    """The production call shape: the UI passes no exclude kwarg at all."""
    updated = upsert_merchant_rule(_lumen_config(), "lumen", "LUMEN", "Investing")

    assert updated.rules[0].exclude_from_spending is True


def test_changing_spend_semantics_without_the_explicit_flag_is_refused() -> None:
    with pytest.raises(SpendSemanticsChangeRefused) as excinfo:
        upsert_merchant_rule(
            _lumen_config(),
            "lumen",
            "LUMEN",
            "Investing",
            exclude_from_spending=False,
        )

    message = str(excinfo.value)
    assert "LUMEN" in message
    assert "exclude_from_spending" in message
    # The refusal must name the STORED value, not just the requested one.
    assert "True" in message
    assert "allow_spend_semantics_change" in message


def test_restating_the_stored_flag_is_not_a_change() -> None:
    """Only a DIFFERENT explicit value is a change; restating the stored one is a no-op."""
    updated = upsert_merchant_rule(
        _lumen_config(),
        "lumen",
        "LUMEN",
        "Investing",
        exclude_from_spending=True,
    )

    assert updated.rules[0].exclude_from_spending is True


def test_an_explicit_change_is_applied_once_it_is_authorized() -> None:
    updated = upsert_merchant_rule(
        _lumen_config(),
        "lumen",
        "LUMEN",
        "Investing",
        exclude_from_spending=False,
        allow_spend_semantics_change=True,
    )

    assert updated.rules[0].exclude_from_spending is False


def test_a_new_rule_inherits_the_target_category_exclusion() -> None:
    """NOT hardcoded False: a new rule starts with its category's spend semantics."""
    updated = upsert_merchant_rule(
        MerchantRulesConfig(rules=[]),
        "LUMEN",
        "LUMEN",
        "Investing",
        category_exclusions={"Investing": True, "Dining": False},
    )

    assert updated.rules[0].exclude_from_spending is True


def test_a_new_rule_into_an_included_category_is_not_excluded() -> None:
    updated = upsert_merchant_rule(
        MerchantRulesConfig(rules=[]),
        "BLUE BOTTLE",
        "BLUE BOTTLE",
        "Dining",
        category_exclusions={"Investing": True, "Dining": False},
    )

    assert updated.rules[0].exclude_from_spending is False


def test_a_new_rule_may_still_be_told_its_flag_without_a_refusal() -> None:
    """There is no stored value to change, so an explicit flag needs no authorization."""
    updated = upsert_merchant_rule(
        MerchantRulesConfig(rules=[]),
        "BLUE BOTTLE",
        "BLUE BOTTLE",
        "Dining",
        exclude_from_spending=True,
        category_exclusions={"Dining": False},
    )

    assert updated.rules[0].exclude_from_spending is True


def test_an_empty_pattern_is_refused_because_it_matches_every_transaction() -> None:
    """``re.compile("")`` is valid and ``.search`` then matches every row, so a blank
    pattern is a rule that silently claims the whole ledger."""
    with pytest.raises(ValueError, match="pattern"):
        upsert_merchant_rule(MerchantRulesConfig(rules=[]), "BLUE BOTTLE", "   ", "Dining")


# ── the wiring: remember_category is what production actually calls ───────────


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
    merchant: str = "",
    category: str = "Other",
    confidence: str = "none",
    exclude: bool = False,
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
                exclude_from_spending=exclude,
                merchant_raw=description,
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


def _seed_lumen(db_path: Path) -> SQLiteConfigRepository:
    """An excluding merchant rule whose available categories are both included."""
    repo = SQLiteConfigRepository(db_path)
    repo.replace_from_configs(
        _lumen_config(),
        CategoriesConfig(
            version="test",
            categories=[
                CategoryRule(name="Other"),
                CategoryRule(name="Fees"),
                CategoryRule(name="Investing"),
            ],
        ),
    )
    return repo


def test_remember_category_preserves_the_stored_flag_through_the_production_call(
    tmp_path: Path,
) -> None:
    """The exact call the Streamlit page makes — no exclude kwarg — must not flip it.

    Delete the ``None``-preserves branch and this goes red: the rule comes back with
    ``exclude_from_spending=False`` and the manual-frozen Fees row loses its exclusion.
    """
    db = tmp_path / "ledger.db"
    repo = _seed_lumen(db)
    _insert(db, "t1", "LUMEN CASH", category="Investing")

    result = CategorizationService(db).remember_category(
        merchant_norm="LUMEN",
        pattern="LUMEN",
        category="Investing",
        refresh_analytics=False,
    )

    assert result.ok, result.validation_issues
    rules = repo.load_merchant_rules().rules
    assert [rule.merchant_norm for rule in rules] == ["LUMEN"]
    assert rules[0].exclude_from_spending is True
    with session_scope(db) as session:
        assert session.get(TransactionDB, "t1").exclude_from_spending is True


def test_the_manual_frozen_row_that_depends_on_the_merchant_flag_alone_stays_excluded(
    tmp_path: Path,
) -> None:
    """``LUMEN`` / ``Fees``: neither category excludes, so only the merchant
    rule's flag keeps this row out of spending, and manual confidence means no rules
    pass will ever repair it once the flag is gone."""
    db = tmp_path / "ledger.db"
    _seed_lumen(db)
    _insert(
        db,
        "frozen",
        "LUMEN FEE",
        merchant="LUMEN",
        category="Fees",
        confidence="manual",
        exclude=True,
    )

    CategorizationService(db).remember_category(
        merchant_norm="LUMEN",
        pattern="LUMEN",
        category="Investing",
        refresh_analytics=False,
    )

    with session_scope(db) as session:
        row = session.get(TransactionDB, "frozen")
        assert row.category == "Fees"  # manual confidence is untouched by a rules pass
        assert row.exclude_from_spending is True


def test_remember_category_derives_a_new_rule_flag_from_the_target_category(
    tmp_path: Path,
) -> None:
    """Drives the real entry point, not the helper: ``remember_category`` must SUPPLY
    the category exclusions. Stop passing them and this goes red."""
    db = tmp_path / "ledger.db"
    repo = SQLiteConfigRepository(db)
    repo.replace_from_configs(
        MerchantRulesConfig(version="test", rules=[]),
        CategoriesConfig(
            version="test",
            categories=[
                CategoryRule(name="Other"),
                CategoryRule(name="Investing", exclude_from_spending=True),
            ],
        ),
    )
    _insert(db, "t1", "EXAMPLEBROKER TRANSFER", category="Other")

    result = CategorizationService(db).remember_category(
        merchant_norm="EXAMPLEBROKER",
        pattern="EXAMPLEBROKER",
        category="Investing",
        refresh_analytics=False,
    )

    assert result.ok, result.validation_issues
    rules = repo.load_merchant_rules().rules
    assert len(rules) == 1
    assert rules[0].exclude_from_spending is True, (
        "a rule taught into an excluded category must not contradict its own category"
    )


def test_remember_category_refuses_an_explicit_flip_and_persists_nothing(
    tmp_path: Path,
) -> None:
    db = tmp_path / "ledger.db"
    repo = _seed_lumen(db)
    _insert(db, "t1", "LUMEN CASH", category="Investing", exclude=True)

    with pytest.raises(SpendSemanticsChangeRefused):
        CategorizationService(db).remember_category(
            merchant_norm="LUMEN",
            pattern="LUMEN",
            category="Investing",
            exclude_from_spending=False,
            refresh_analytics=False,
        )

    assert repo.load_merchant_rules().rules[0].exclude_from_spending is True
    with session_scope(db) as session:
        assert session.get(TransactionDB, "t1").exclude_from_spending is True


def test_remember_category_applies_an_authorized_flip(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    repo = _seed_lumen(db)
    _insert(db, "t1", "LUMEN CASH", category="Investing", exclude=True)

    result = CategorizationService(db).remember_category(
        merchant_norm="LUMEN",
        pattern="LUMEN",
        category="Investing",
        exclude_from_spending=False,
        allow_spend_semantics_change=True,
        refresh_analytics=False,
    )

    assert result.ok, result.validation_issues
    assert repo.load_merchant_rules().rules[0].exclude_from_spending is False
    with session_scope(db) as session:
        assert session.get(TransactionDB, "t1").exclude_from_spending is False
