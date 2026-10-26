"""The tracked TOML seed must be a generic, importable placeholder set that
carries no real merchant names (the real ruleset lives only in SQLite)."""

from __future__ import annotations

import re
from pathlib import Path

from ledger.config.repository import SQLiteConfigRepository

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIGS = REPO_ROOT / "configs"


def test_seed_imports_and_validates_round_trip(tmp_path):
    db_path = tmp_path / "seed.db"
    result = SQLiteConfigRepository(db_path).import_from_toml(CONFIGS)
    assert result.ok, result.issues
    assert result.merchant_rules > 0
    assert result.categories > 0

    repo = SQLiteConfigRepository(db_path)
    merchants = repo.load_merchant_rules()
    categories = repo.load_categories()
    # Round-trip: every rule category resolves and matches the category map.
    cat_names = {c.name for c in categories.categories}
    merchant_map = categories.build_merchant_map()
    for rule in merchants.rules:
        assert rule.category in cat_names
        mapped = merchant_map.get(rule.merchant_norm.upper().strip())
        assert mapped is not None and mapped[0] == rule.category


def _placeholder_issues(merchants, categories) -> list[str]:
    """Validate the entire positive seed vocabulary without retaining a blacklist."""
    issues: list[str] = []
    names = {rule.merchant_norm for rule in merchants.rules}
    for rule in merchants.rules:
        if not re.fullmatch(r"MERCHANT_\d{3}", rule.merchant_norm):
            issues.append("merchant name is not a numbered placeholder")
        expected = rf"(?i)\b{rule.merchant_norm}\b"
        if rule.patterns != [expected]:
            issues.append("merchant pattern does not match its placeholder")
    for category in categories.categories:
        if any(name not in names for name in category.merchants):
            issues.append("category refers to a name outside the placeholder set")
    return issues


def test_seed_contains_only_generic_merchant_placeholders(tmp_path):
    repo = SQLiteConfigRepository(tmp_path / "placeholder-seed.db")
    result = repo.import_from_toml(CONFIGS)
    assert result.ok, result.issues
    merchants = repo.load_merchant_rules()
    categories = repo.load_categories()
    assert merchants.rules, "expected at least one placeholder rule"
    assert not _placeholder_issues(merchants, categories)


def test_placeholder_guard_rejects_an_invented_merchant_and_pattern(tmp_path):
    """An unrelated invented business must not be admitted as a public seed."""
    repo = SQLiteConfigRepository(tmp_path / "adversarial-seed.db")
    assert repo.import_from_toml(CONFIGS).ok
    merchants = repo.load_merchant_rules()
    categories = repo.load_categories()
    invented = merchants.model_copy(deep=True)
    invented.rules[0] = invented.rules[0].model_copy(
        update={
            "merchant_norm": "FICTIONAL MOONLIT TEAPOT",
            "patterns": [r"(?i)\bFICTIONAL MOONLIT TEAPOT\b"],
        }
    )
    assert "merchant name is not a numbered placeholder" in _placeholder_issues(
        invented, categories
    )
    altered = merchants.model_copy(deep=True)
    altered.rules[0] = altered.rules[0].model_copy(
        update={"patterns": [r"(?i)\bFICTIONAL MOONLIT TEAPOT\b"]}
    )
    assert "merchant pattern does not match its placeholder" in _placeholder_issues(
        altered, categories
    )
