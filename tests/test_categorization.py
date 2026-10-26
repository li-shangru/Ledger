"""Categorization learning loop: a correction becomes a reusable merchant rule."""

from __future__ import annotations

import re
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
from ledger.rules import RuleApplicationService
from ledger.rules.categorization import (
    CategorizationService,
    derive_rule_suggestion,
    upsert_merchant_rule,
)
from ledger.storage.database import TransactionDB, session_scope


def _temporal_fields(posting_date: date) -> dict:
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
    merchant_raw: str | None = None,
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
                merchant_confidence="none" if not merchant else "rule",
                category_confidence="none",
                exclude_from_spending=False,
                merchant_raw=merchant_raw if merchant_raw is not None else description,
                category_raw="",
                source_file="statement.csv",
                source_path="/private/statement.csv",
                source_period="2026-05",
                source_row=1,
                imported_at=datetime.now(UTC).replace(tzinfo=None),
                parser_version="test",
                txn_fingerprint=f"{txn_id}-fp",
                **_temporal_fields(posted),
            )
        )


def _seed_config(db_path: Path, category_names: list[str]) -> None:
    SQLiteConfigRepository(db_path).replace_from_configs(
        MerchantRulesConfig(version="test", rules=[]),
        CategoriesConfig(
            version="test",
            categories=[CategoryRule(name=name) for name in category_names],
        ),
    )


# --- derivation (the risky pure function) ---


def test_derive_uses_recognized_merchant():
    s = derive_rule_suggestion("NETFLIX", "NETFLIX.COM", "NETFLIX.COM 8665797", "Subscriptions")
    assert s.merchant_norm == "NETFLIX"
    assert s.category == "Subscriptions"
    assert re.search(s.pattern, "NETFLIX.COM 8665797", re.IGNORECASE)


def test_derive_strips_noise_and_generalizes_for_unknown_merchant():
    s = derive_rule_suggestion("", "SQ *BLUE BOTTLE 00123", "SQ *BLUE BOTTLE 00123", "Other")
    assert "BLUE BOTTLE" in s.merchant_norm
    assert "SQ" not in s.merchant_norm  # processor prefix stripped
    assert "00123" not in s.pattern  # variable store number stripped
    # The derived pattern must still match a FUTURE visit with a different store number.
    assert re.search(s.pattern, "SQ *BLUE BOTTLE 00999", re.IGNORECASE)


def test_derive_does_not_overstrip_short_names():
    s = derive_rule_suggestion("", "A1", "A1", "Other")
    assert s.merchant_norm  # non-empty fallback, not blanked by stripping


# --- rule upsert (pure) ---


def test_upsert_updates_existing_merchant_category_and_dedups_pattern():
    cfg = MerchantRulesConfig(
        rules=[MerchantRule(merchant_norm="NETFLIX", category="Other", patterns=["NETFLIX"])]
    )
    out = upsert_merchant_rule(cfg, "netflix", "NETFLIX", "Subscriptions")  # case-insensitive
    assert len(out.rules) == 1
    assert out.rules[0].category == "Subscriptions"
    assert out.rules[0].patterns == ["NETFLIX"]  # not duplicated


def test_upsert_creates_new_rule():
    out = upsert_merchant_rule(
        MerchantRulesConfig(rules=[]), "BLUE BOTTLE", "BLUE BOTTLE", "Coffee"
    )
    assert len(out.rules) == 1
    assert out.rules[0].merchant_norm == "BLUE BOTTLE"
    assert out.rules[0].category == "Coffee"
    assert out.rules[0].patterns == ["BLUE BOTTLE"]


# --- orchestration on a real DB ---


def test_remember_category_recategorizes_all_matching_transactions(tmp_path: Path):
    """The payoff: teaching a category from ONE transaction recategorizes every matching
    transaction (including ones imported before the rule existed), and unrelated rows
    are untouched."""
    db = tmp_path / "ledger.db"
    _seed_config(db, ["Other", "Coffee Shops"])
    _insert(db, "t1", "SQ *BLUE BOTTLE 00123", category="Other")
    _insert(
        db, "t2", "SQ *BLUE BOTTLE 00999", category="Other"
    )  # a later, still-uncategorized visit
    _insert(db, "t3", "SOME OTHER STORE 5", category="Other")  # unrelated -> must NOT change

    result = CategorizationService(db).remember_category(
        merchant_norm="BLUE BOTTLE",
        pattern="BLUE BOTTLE",
        category="Coffee Shops",
        refresh_analytics=False,
    )

    assert result.ok
    with session_scope(db) as s:
        assert s.get(TransactionDB, "t1").category == "Coffee Shops"
        assert s.get(TransactionDB, "t2").category == "Coffee Shops"  # learned across transactions
        assert s.get(TransactionDB, "t1").category_confidence == "rule"
        assert s.get(TransactionDB, "t1").merchant == "BLUE BOTTLE"
        assert s.get(TransactionDB, "t3").category == "Other"  # untouched


def test_remember_category_moves_existing_rule_without_stale_membership(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    repo = SQLiteConfigRepository(db)
    repo.replace_from_configs(
        MerchantRulesConfig(
            version="test",
            rules=[MerchantRule(merchant_norm="ACME", category="Old", patterns=["ACME"])],
        ),
        CategoriesConfig(
            version="test",
            categories=[
                CategoryRule(name="Old", merchants=["ACME", "SEED ONLY"]),
                CategoryRule(name="New"),
            ],
        ),
    )
    _insert(db, "t1", "ACME MARKET", merchant="ACME", category="Old")

    result = CategorizationService(db).remember_category(
        merchant_norm="ACME",
        pattern="ACME",
        category="New",
        refresh_analytics=False,
    )

    assert result.ok
    memberships = [
        category.name
        for category in repo.load_categories().categories
        for merchant in category.merchants
        if merchant.strip().upper() == "ACME"
    ]
    assert memberships == ["New"]
    assert repo.load_taxonomy().build_merchant_map() == {"SEED ONLY": ("Old", False)}
    assert repo.validate().ok
    with session_scope(db) as session:
        assert session.get(TransactionDB, "t1").category == "New"


def test_preview_match_count(tmp_path: Path):
    db = tmp_path / "ledger.db"
    _seed_config(db, ["Other"])
    _insert(db, "t1", "SQ *BLUE BOTTLE 00123", category="Other")
    _insert(db, "t2", "SQ *BLUE BOTTLE 00999", category="Other")
    _insert(db, "t3", "STARBUCKS 55", category="Other")

    assert CategorizationService(db).preview_match_count("BLUE BOTTLE") == 2


def test_set_transaction_category_one_off_survives_apply_all(tmp_path: Path):
    """A one-off correction sets just that row (confidence 'manual') and is NOT reverted by
    a later rule re-apply, since no rule matches it."""
    db = tmp_path / "ledger.db"
    _seed_config(db, ["Other", "Dining"])
    _insert(db, "t1", "OBSCURE CASH THING", category="Other")

    svc = CategorizationService(db)
    assert svc.set_transaction_category("t1", "Dining") is True
    with session_scope(db) as s:
        row = s.get(TransactionDB, "t1")
        assert row.category == "Dining"
        assert row.category_confidence == "manual"

    RuleApplicationService(db).apply_all(source="test", refresh_analytics=False)
    with session_scope(db) as s:
        assert s.get(TransactionDB, "t1").category == "Dining"  # not clobbered


def test_remember_category_rejects_invalid_regex(tmp_path: Path):
    db = tmp_path / "ledger.db"
    _seed_config(db, ["Other", "Coffee"])
    _insert(db, "t1", "BLUE BOTTLE", category="Other")
    svc = CategorizationService(db)
    try:
        svc.remember_category(
            merchant_norm="BLUE BOTTLE",
            pattern="BLUE (BOTTLE",
            category="Coffee",
            refresh_analytics=False,
        )
        raised = False
    except ValueError:
        raised = True
    assert raised  # bad regex is rejected, nothing persisted
    assert SQLiteConfigRepository(db).load_merchant_rules().rules == []


def _seed_config_with_hierarchy(db_path: Path) -> None:
    """Seed: 'Food' (parent) -> 'Groceries' (child), plus 'Other' leaf."""
    SQLiteConfigRepository(db_path).replace_from_configs(
        MerchantRulesConfig(version="test", rules=[]),
        CategoriesConfig(
            version="test",
            categories=[
                CategoryRule(name="Food"),
                CategoryRule(name="Groceries", parent="Food"),
                CategoryRule(name="Other"),
            ],
        ),
    )


def test_remember_category_rejects_parent_category(tmp_path: Path):
    """Test A: remember_category with a parent category returns a failed result,
    does NOT persist any rule, and does NOT introduce the parent-merchant validation issue."""
    db = tmp_path / "ledger.db"
    _seed_config_with_hierarchy(db)
    _insert(db, "t1", "LOBLAWS", category="Other")

    result = CategorizationService(db).remember_category(
        merchant_norm="LOBLAWS",
        pattern="LOBLAWS",
        category="Food",  # "Food" is a parent (has child "Groceries")
        refresh_analytics=False,
    )

    # Service must return a non-success result
    assert not result.ok
    assert result.status != "success"
    assert result.validation_issues  # must explain the rejection

    # No rule must have been persisted
    assert SQLiteConfigRepository(db).load_merchant_rules().rules == []

    # The parent-merchant validation issue must NOT be present — not bricked
    issues = SQLiteConfigRepository(db).validate().issues
    assert not any("must not list merchants" in issue for issue in issues)


def test_remember_category_leaf_category_still_works(tmp_path: Path):
    """Test B (regression): remember_category with a leaf category still succeeds."""
    db = tmp_path / "ledger.db"
    _seed_config_with_hierarchy(db)
    _insert(db, "t1", "LOBLAWS", category="Other")

    result = CategorizationService(db).remember_category(
        merchant_norm="LOBLAWS",
        pattern="LOBLAWS",
        category="Groceries",  # leaf — not a referenced parent
        refresh_analytics=False,
    )

    assert result.ok
    rules = SQLiteConfigRepository(db).load_merchant_rules().rules
    assert len(rules) == 1
    assert rules[0].category == "Groceries"


# --- audit trail for set_transaction_category (M6) ---


def test_set_transaction_category_records_audit_row(tmp_path: Path):
    """set_transaction_category must write a db_operations audit row on success."""
    from ledger.storage.operations import recent_db_operations

    db = tmp_path / "ledger.db"
    _seed_config(db, ["Other", "Dining"])
    _insert(db, "t1", "OBSCURE CASH THING", category="Other")

    svc = CategorizationService(db)
    result = svc.set_transaction_category("t1", "Dining")
    assert result is True

    ops = recent_db_operations(db)
    assert len(ops) == 1
    assert ops[0]["Operation"] == "category_override"
    assert ops[0]["Status"] == "success"


def test_set_transaction_category_no_audit_row_when_txn_not_found(tmp_path: Path):
    """set_transaction_category must NOT write an audit row when txn_id is missing."""
    from ledger.storage.operations import recent_db_operations

    db = tmp_path / "ledger.db"
    _seed_config(db, ["Other", "Dining"])

    svc = CategorizationService(db)
    result = svc.set_transaction_category("nonexistent", "Dining")
    assert result is False

    ops = recent_db_operations(db)
    assert ops == []


def test_set_transaction_category_audit_failure_does_not_propagate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If record_db_operation raises, set_transaction_category must still return True
    and must NOT propagate the exception — the mutation already succeeded."""
    import ledger.rules.categorization as cat_mod

    db = tmp_path / "ledger.db"
    _seed_config(db, ["Other", "Dining"])
    _insert(db, "t1", "OBSCURE CASH THING", category="Other")

    monkeypatch.setattr(cat_mod, "record_db_operation", _raise_on_call)

    svc = CategorizationService(db)
    # Must not raise, must return the normal True
    result = svc.set_transaction_category("t1", "Dining")
    assert result is True

    # Verify the actual mutation happened despite the audit failure
    with session_scope(db) as s:
        row = s.get(TransactionDB, "t1")
        assert row.category == "Dining"


def _raise_on_call(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError("simulated DB locked")
