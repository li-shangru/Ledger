"""
Tests for TOML-backed configuration loading.
"""

from pathlib import Path

import pytest
from pydantic import ValidationError

from ledger.config import (
    load_categories,
    load_cli_defaults,
    load_ledger_config,
    load_merchant_rules,
)
from ledger.config.schemas import ClassificationPatterns


def test_load_ledger_config_includes_fuzzy_and_transfer_settings(tmp_path: Path):
    config_path = tmp_path / "ledger.toml"
    config_path.write_text(
        """
[general]
strict = false
etransfer_is_cashflow = false
cc_payment_is_transfer = false
enable_transfer_matching = false
transfer_match_window = 9
transfer_amount_tolerance = 0.02
transfer_min_confidence = 0.8

[general.account_aliases]
"old-account" = "new-account"

[fuzzy]
enabled = true
threshold = 77
min_merchant_length = 4

[rules.harbor]
transfer_patterns = ["transfer"]
""",
        encoding="utf-8",
    )

    config = load_ledger_config(config_path)

    assert config.general.strict is False
    assert config.general.etransfer_is_cashflow is False
    assert config.general.cc_payment_is_transfer is False
    assert config.general.account_aliases == {"old-account": "new-account"}
    assert config.general.enable_transfer_matching is False
    assert config.general.transfer_match_window == 9
    assert config.general.transfer_amount_tolerance == 0.02
    assert config.general.transfer_min_confidence == 0.8
    assert config.fuzzy.enabled is True
    assert config.fuzzy.threshold == 77
    assert config.fuzzy.min_merchant_length == 4
    assert config.rules["harbor"].transfer_patterns == ["transfer"]


def test_load_cli_defaults_reads_db_path(tmp_path: Path):
    defaults_path = tmp_path / "defaults.toml"
    defaults_path.write_text(
        """
[defaults]
db = "/tmp/ledger.db"
formats = "csv,json"
""",
        encoding="utf-8",
    )

    defaults = load_cli_defaults(defaults_path)

    assert defaults.db == "/tmp/ledger.db"
    assert defaults.formats == "csv,json"


def test_seed_rules_match_their_placeholder_and_map_to_category():
    """The tracked seed is a GENERIC placeholder set (the real ruleset lives in the
    user's SQLite runtime config). Each MERCHANT_NNN rule must still match its own
    placeholder text and round-trip to the same category via the category map, so
    the seed remains a valid, importable config skeleton."""
    repo_root = Path(__file__).resolve().parents[1]
    merchants = load_merchant_rules(repo_root / "configs/merchants.toml")
    categories = load_categories(repo_root / "configs/categories.toml")

    assert merchants.rules, "expected the placeholder seed to load rules"
    merchant_map = categories.build_merchant_map()
    for rule in merchants.rules:
        # The placeholder is its own match text; matching returns the rule.
        found_merchant, found_category, _exclude = merchants.find_merchant(rule.merchant_norm)
        assert found_merchant == rule.merchant_norm
        assert found_category == rule.category
        # And the category map resolves that placeholder to the same category.
        assert merchant_map[rule.merchant_norm.upper()][0] == rule.category


# === ClassificationPatterns regex validation ===


def test_classification_patterns_rejects_invalid_regex():
    """An invalid regex in any pattern list must raise at construction time."""
    with pytest.raises((ValueError, ValidationError)):
        ClassificationPatterns(transfer_patterns=["BAD(REGEX"])


def test_classification_patterns_invalid_regex_in_each_field():
    """All 8 pattern fields are validated eagerly."""
    fields = (
        "transfer_patterns",
        "etransfer_patterns",
        "cc_payment_patterns",
        "fee_patterns",
        "interest_patterns",
        "income_patterns",
        "payment_patterns",
        "reward_patterns",
    )
    for field in fields:
        with pytest.raises((ValueError, ValidationError)):
            ClassificationPatterns(**{field: ["BAD(REGEX"]})


def test_classification_patterns_accepts_valid_regex():
    """Valid patterns must still construct without error."""
    cp = ClassificationPatterns(
        transfer_patterns=[r"TRANSFER\s+\d+"],
        etransfer_patterns=[r"e-?transfer", r"INTERAC"],
    )
    assert cp.transfer_patterns == [r"TRANSFER\s+\d+"]
    assert cp.etransfer_patterns == [r"e-?transfer", r"INTERAC"]


def test_load_ledger_config_absent_keys_use_schema_defaults(tmp_path: Path):
    """A TOML that omits transfer/fuzzy keys must yield the schema Field defaults."""
    config_path = tmp_path / "ledger.toml"
    config_path.write_text(
        """
[general]
strict = false
""",
        encoding="utf-8",
    )

    config = load_ledger_config(config_path)

    # Schema defaults from GeneralConfig
    assert config.general.transfer_match_window == 5
    assert config.general.transfer_amount_tolerance == 0.0
    assert config.general.transfer_min_confidence == 0.5
    assert config.general.enable_transfer_matching is True
    assert config.general.etransfer_is_cashflow is True
    assert config.general.cc_payment_is_transfer is True
    assert config.general.account_aliases == {}

    # Schema defaults from FuzzyConfig
    assert config.fuzzy.enabled is False
    assert config.fuzzy.threshold == 85
    assert config.fuzzy.min_merchant_length == 3

    # Explicitly-set TOML value passes through correctly
    assert config.general.strict is False


def test_load_ledger_config_explicit_toml_values_override_schema_defaults(tmp_path: Path):
    """Explicitly-set TOML values must override schema defaults (regression guard)."""
    config_path = tmp_path / "ledger.toml"
    config_path.write_text(
        """
[general]
transfer_match_window = 14
transfer_amount_tolerance = 0.05
transfer_min_confidence = 0.75
enable_transfer_matching = false

[fuzzy]
enabled = true
threshold = 70
min_merchant_length = 5
""",
        encoding="utf-8",
    )

    config = load_ledger_config(config_path)

    assert config.general.transfer_match_window == 14
    assert config.general.transfer_amount_tolerance == 0.05
    assert config.general.transfer_min_confidence == 0.75
    assert config.general.enable_transfer_matching is False
    assert config.fuzzy.enabled is True
    assert config.fuzzy.threshold == 70
    assert config.fuzzy.min_merchant_length == 5


def test_load_ledger_config_rejects_invalid_classification_regex(tmp_path: Path):
    """load_ledger_config must raise when a [rules.*] pattern is an invalid regex."""
    config_path = tmp_path / "ledger.toml"
    config_path.write_text(
        """
[rules.harbor]
transfer_patterns = ["BAD(REGEX"]
""",
        encoding="utf-8",
    )
    with pytest.raises((ValueError, ValidationError)):
        load_ledger_config(config_path)


def test_unknown_rules_field_warns(tmp_path: Path, caplog):
    """A typo'd key inside [rules.<institution>] is silently dropped by the schema;
    the loader must WARN so an inert config line is not mistaken for a live rule."""
    config_path = tmp_path / "ledger.toml"
    config_path.write_text(
        "[rules.lumen]\n"
        'transfr_patterns = ["(?i)\\\\btransfer\\\\b"]\n'  # typo: transfr_ vs transfer_
    )
    import logging

    with caplog.at_level(logging.WARNING):
        load_ledger_config(config_path)
    assert any("transfr_patterns" in rec.message for rec in caplog.records), caplog.text


def test_named_rules_section_is_loaded_without_a_fixed_institution_registry(tmp_path: Path):
    """Rules are keyed by an arbitrary private institution token."""
    config_path = tmp_path / "ledger.toml"
    config_path.write_text('[rules.fictional_institution]\ntransfer_patterns = ["transfer"]\n')
    config = load_ledger_config(config_path)
    assert config.rules["fictional_institution"].transfer_patterns == ["transfer"]
