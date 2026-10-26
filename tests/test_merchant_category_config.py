from pathlib import Path

from ledger.config import load_categories, load_merchant_rules
from ledger.config.schemas import CategoriesConfig, CategoryRule

ROOT = Path(__file__).resolve().parents[1]


def load_configs():
    return (
        load_merchant_rules(ROOT / "configs/merchants.toml"),
        load_categories(ROOT / "configs/categories.toml"),
    )


def test_merchant_rule_categories_are_canonical():
    merchants, categories = load_configs()
    category_names = {category.name for category in categories.categories}
    merchant_map = categories.build_merchant_map()

    missing_categories = [
        (rule.merchant_norm, rule.category)
        for rule in merchants.rules
        if rule.category not in category_names
    ]
    missing_merchants = [
        (rule.merchant_norm, rule.category)
        for rule in merchants.rules
        if rule.merchant_norm.upper() not in merchant_map
    ]
    mismatched_categories = [
        (rule.merchant_norm, rule.category, merchant_map[rule.merchant_norm.upper()][0])
        for rule in merchants.rules
        if rule.merchant_norm.upper() in merchant_map
        and rule.category != merchant_map[rule.merchant_norm.upper()][0]
    ]

    assert missing_categories == []
    assert missing_merchants == []
    assert mismatched_categories == []


def test_category_merchants_are_unique_and_parents_are_empty():
    _merchants, categories = load_configs()
    seen: dict[str, str] = {}
    duplicates = []

    for category in categories.categories:
        for merchant in category.merchants:
            key = merchant.upper().strip()
            if key in seen:
                duplicates.append((merchant, seen[key], category.name))
            seen[key] = category.name

    referenced_parents = {category.parent for category in categories.categories if category.parent}
    parent_merchants = [
        (category.name, category.merchants)
        for category in categories.categories
        if category.name in referenced_parents and category.merchants
    ]

    assert duplicates == []
    assert parent_merchants == []


def test_no_configured_merchants_remain_other():
    _merchants, categories = load_configs()
    merchant_map = categories.build_merchant_map()
    other_merchants = {
        merchant for merchant, (category, _exclude) in merchant_map.items() if category == "Other"
    }

    assert other_merchants == set()


def test_generic_seed_income_and_transfers_remain_excluded_from_spending():
    _, categories = load_configs()
    merchant_map = categories.build_merchant_map()
    assert merchant_map["MERCHANT_007"] == ("Income (Excluded)", True)
    assert merchant_map["MERCHANT_008"] == ("Transfers (Excluded)", True)
    assert merchant_map["MERCHANT_001"] == ("Groceries", False)


def test_seed_rules_are_first_match_wins_and_self_matching():
    """The tracked seed is a GENERIC placeholder set (the real, order-sensitive
    ruleset lives in the user's SQLite runtime config). We can no longer assert
    real merchant precedence here, but the seed must still exhibit the same
    matching mechanics: each MERCHANT_NNN rule matches its own placeholder, and
    find_merchant returns the FIRST rule whose pattern matches (first-match-wins).
    """
    merchants, _categories = load_configs()
    assert merchants.rules, "expected the placeholder seed to load rules"

    # Each placeholder rule matches its own text and resolves to its category.
    for rule in merchants.rules:
        found_merchant, found_category, _exclude = merchants.find_merchant(rule.merchant_norm)
        assert found_merchant == rule.merchant_norm
        assert found_category == rule.category

    # First-match-wins: a text containing two placeholders resolves to whichever
    # rule appears earliest in the ruleset.
    first, second = merchants.rules[0], merchants.rules[1]
    combined = f"{second.merchant_norm} {first.merchant_norm}"
    assert merchants.find_merchant(combined)[0] == first.merchant_norm


def test_referenced_parent_names_returns_only_referenced_parents():
    """referenced_parent_names() returns categories used as a parent field, not leaf categories."""
    config = CategoriesConfig(
        categories=[
            CategoryRule(name="Food", merchants=[]),
            CategoryRule(name="Groceries", parent="Food", merchants=["ACME"]),
            CategoryRule(name="Dining", parent="Food", merchants=["MCDONALD'S"]),
            CategoryRule(name="Tech", merchants=[]),
            CategoryRule(name="Software", parent="Tech", merchants=["GITHUB"]),
            CategoryRule(name="Leaf", merchants=["SOME STORE"]),
        ]
    )
    result = config.referenced_parent_names()
    assert result == {"Food", "Tech"}
    # Leaf categories with merchants are not included
    assert "Leaf" not in result
    assert "Groceries" not in result
    assert "Software" not in result


def test_referenced_parent_names_empty_when_no_hierarchy():
    """referenced_parent_names() returns an empty set when no categories have parents."""
    config = CategoriesConfig(
        categories=[
            CategoryRule(name="Groceries", merchants=["ACME"]),
            CategoryRule(name="Tech", merchants=["GITHUB"]),
        ]
    )
    assert config.referenced_parent_names() == set()


def test_referenced_parent_names_matches_real_config():
    """referenced_parent_names() on the real config agrees with the inline set comprehension."""
    _, categories = load_configs()
    expected = {c.parent for c in categories.categories if c.parent}
    assert categories.referenced_parent_names() == expected
