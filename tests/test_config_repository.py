"""Tests for SQLite-backed runtime configuration."""

import contextlib
import os
import sqlite3
import stat
from pathlib import Path

import pytest

from ledger.config import load_categories, load_merchant_rules
from ledger.config.repository import SQLiteConfigRepository, seed_config_database
from ledger.config.schemas import (
    CategoriesConfig,
    CategoryRule,
    MerchantRule,
    MerchantRulesConfig,
)
from ledger.core.exceptions import ConfigError


def write_seed_configs(root: Path) -> None:
    root.mkdir()
    (root / "merchants.toml").write_text(
        """
version = "test"

[[rule]]
merchant_norm = "ACME"
category = "Groceries"
patterns = ["ACME MARKET"]
exclude_from_spending = false

[[rule]]
merchant_norm = "PAYROLL"
category = "Income (Excluded)"
patterns = ["PAYROLL"]
exclude_from_spending = true
""",
        encoding="utf-8",
    )
    (root / "categories.toml").write_text(
        """
version = "test"
default_category = "Other"

[[category]]
category_norm = "Food"
merchants = []
exclude_from_spend_metrics = false

[[category]]
category_norm = "Groceries"
parent = "Food"
merchants = ["ACME"]
exclude_from_spend_metrics = false

[[category]]
category_norm = "Income (Excluded)"
merchants = ["PAYROLL"]
exclude_from_spend_metrics = true
""",
        encoding="utf-8",
    )


def test_import_toml_to_sqlite_and_load_runtime_configs(tmp_path: Path):
    source = tmp_path / "configs"
    write_seed_configs(source)
    db_path = tmp_path / "ledger.db"

    result = SQLiteConfigRepository(db_path).import_from_toml(source)

    assert result.ok
    assert result.merchant_rules == 2
    assert result.categories == 3

    repo = SQLiteConfigRepository(db_path)
    merchants = repo.load_merchant_rules()
    categories = repo.load_categories()

    assert merchants.find_merchant("ACME MARKET #123")[0:2] == ("ACME", "Groceries")
    assert categories.get_category_with_parent("Groceries") == "Food"
    assert categories.build_merchant_map()["PAYROLL"] == ("Income (Excluded)", True)


def test_sqlite_config_export_round_trip(tmp_path: Path):
    source = tmp_path / "configs"
    dest = tmp_path / "exported"
    write_seed_configs(source)
    db_path = tmp_path / "ledger.db"

    seed_config_database(db_path, source / "merchants.toml", source / "categories.toml")
    SQLiteConfigRepository(db_path).export_to_directory(dest)

    exported_merchants = load_merchant_rules(dest / "merchants.toml")
    exported_categories = load_categories(dest / "categories.toml")

    assert exported_merchants.find_merchant("PAYROLL MAY")[0:2] == (
        "PAYROLL",
        "Income (Excluded)",
    )
    assert exported_categories.get_child_categories("Food")[0].name == "Groceries"


def test_sqlite_config_export_is_private_under_ambient_umask(tmp_path: Path) -> None:
    source = tmp_path / "configs"
    dest = tmp_path / "exported"
    write_seed_configs(source)
    db_path = tmp_path / "ledger.db"
    seed_config_database(db_path, source / "merchants.toml", source / "categories.toml")

    previous_umask = os.umask(0o022)
    try:
        SQLiteConfigRepository(db_path).export_to_directory(dest)
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(dest.stat().st_mode) == 0o700
    assert stat.S_IMODE((dest / "merchants.toml").stat().st_mode) == 0o600
    assert stat.S_IMODE((dest / "categories.toml").stat().st_mode) == 0o600
    assert load_merchant_rules(dest / "merchants.toml").version == "test"
    assert load_categories(dest / "categories.toml").version == "test"


def test_sqlite_config_export_failure_restores_existing_tomls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.utils import private_files

    source = tmp_path / "configs"
    dest = tmp_path / "exported"
    write_seed_configs(source)
    dest.mkdir()
    old_merchants = 'version = "old-merchants"\n'
    old_categories = 'version = "old-categories"\ndefault_category = "Other"\n'
    (dest / "merchants.toml").write_text(old_merchants, encoding="utf-8")
    (dest / "categories.toml").write_text(old_categories, encoding="utf-8")
    db_path = tmp_path / "ledger.db"
    seed_config_database(db_path, source / "merchants.toml", source / "categories.toml")
    real_replace = private_files.os.replace

    def fail_categories(source_name: object, destination_name: object, **kwargs: object) -> None:
        if os.fspath(destination_name) == "categories.toml":
            raise OSError("simulated category export failure")
        real_replace(source_name, destination_name, **kwargs)

    monkeypatch.setattr(private_files.os, "replace", fail_categories)

    with pytest.raises(OSError, match="simulated category export failure"):
        SQLiteConfigRepository(db_path).export_to_directory(dest)

    assert (dest / "merchants.toml").read_text(encoding="utf-8") == old_merchants
    assert (dest / "categories.toml").read_text(encoding="utf-8") == old_categories
    assert not list(dest.glob(".*.tmp"))


def test_sqlite_config_export_fails_closed_on_real_destination_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "configs"
    write_seed_configs(source)
    db_path = tmp_path / "ledger.db"
    seed_config_database(db_path, source / "merchants.toml", source / "categories.toml")
    dest = tmp_path / "exported"
    dest.mkdir()
    (dest / "merchants.toml").write_text('version = "old"\n', encoding="utf-8")
    (dest / "categories.toml").write_text(
        'version = "old"\ndefault_category = "Other"\n', encoding="utf-8"
    )
    moved_dest = tmp_path / "moved-exported"
    substitute_dest = tmp_path / "substitute-exported"
    substitute_dest.mkdir()
    repository = SQLiteConfigRepository(db_path)
    real_load = repository.load_merchant_rules
    swapped = False

    def load_then_swap():
        nonlocal swapped
        result = real_load()
        swapped = True
        dest.rename(moved_dest)
        substitute_dest.rename(dest)
        return result

    monkeypatch.setattr(repository, "load_merchant_rules", load_then_swap)
    try:
        with pytest.raises(OSError, match="changed"):
            repository.export_to_directory(dest)
    finally:
        if dest.exists():
            dest.rename(substitute_dest)
        if moved_dest.exists():
            moved_dest.rename(dest)

    assert swapped
    assert (dest / "merchants.toml").read_text(encoding="utf-8") == 'version = "old"\n'
    assert not (substitute_dest / "merchants.toml").exists()
    assert not (substitute_dest / "categories.toml").exists()


def test_sqlite_config_validation_reports_mismatched_categories(tmp_path: Path):
    source = tmp_path / "configs"
    write_seed_configs(source)
    db_path = tmp_path / "ledger.db"
    repo = SQLiteConfigRepository(db_path)
    repo.import_from_toml(source)

    from ledger.storage.database import MerchantRuleDB, session_scope

    with session_scope(db_path) as session:
        rule = session.query(MerchantRuleDB).filter_by(merchant_norm="ACME").one()
        rule.category = "Missing"

    result = repo.validate()

    assert not result.ok
    assert any("missing category" in issue for issue in result.issues)


def test_sqlite_config_validation_rejects_empty_runtime_config(tmp_path: Path):
    result = SQLiteConfigRepository(tmp_path / "ledger.db").validate()

    assert not result.ok
    assert "No merchant rules are configured." in result.issues
    assert "No categories are configured." in result.issues


@pytest.mark.parametrize("mode", [0o400, 0o644])
def test_has_runtime_config_prepare_false_is_observational(
    tmp_path: Path,
    mode: int,
) -> None:
    db_path = tmp_path / "pre-migration.db"
    with contextlib.closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute("create table sentinel (value text)")
    db_path.chmod(mode)
    before = db_path.read_bytes()

    assert SQLiteConfigRepository(db_path, prepare_schema=False).has_runtime_config() is False

    assert db_path.read_bytes() == before
    assert stat.S_IMODE(db_path.stat().st_mode) == mode
    with (
        contextlib.closing(sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)) as connection,
        connection,
    ):
        assert connection.execute(
            "select name from sqlite_master where type='table' order by name"
        ).fetchall() == [("sentinel",)]


def test_replace_canonicalizes_rule_owned_membership_and_preserves_unruled(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ledger.db"
    repo = SQLiteConfigRepository(db_path)
    repo.replace_from_configs(
        MerchantRulesConfig(
            version="test",
            rules=[MerchantRule(merchant_norm="ACME", category="New", patterns=["ACME"])],
        ),
        CategoriesConfig(
            version="test",
            categories=[
                CategoryRule(name="Old", merchants=["ACME", "SEED ONLY"]),
                CategoryRule(name="New", merchants=["acme"]),
            ],
        ),
    )

    categories = repo.load_categories()
    memberships = [
        category.name
        for category in categories.categories
        for merchant in category.merchants
        if merchant.strip().upper() == "ACME"
    ]

    assert memberships == ["New"]
    assert categories.build_merchant_map()["ACME"] == ("New", False)
    assert repo.validate().ok
    taxonomy = repo.load_taxonomy()
    assert taxonomy.build_merchant_map() == {"SEED ONLY": ("Old", False)}


def test_replace_rejects_duplicate_unruled_membership_without_mutating(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ledger.db"
    repo = SQLiteConfigRepository(db_path)
    original_merchants = MerchantRulesConfig(
        version="original",
        rules=[MerchantRule(merchant_norm="ACME", category="Old", patterns=["ACME"])],
    )
    original_categories = CategoriesConfig(
        version="original",
        categories=[CategoryRule(name="Old"), CategoryRule(name="New")],
    )
    repo.replace_from_configs(original_merchants, original_categories)

    duplicate_unruled = CategoriesConfig(
        version="candidate",
        categories=[
            CategoryRule(name="Old", merchants=["SEED ONLY"]),
            CategoryRule(name="New", merchants=[" seed only "]),
        ],
    )

    with pytest.raises(ConfigError, match="appears in both"):
        repo.replace_from_configs(MerchantRulesConfig(version="candidate"), duplicate_unruled)

    assert repo.load_merchant_rules() == original_merchants
    assert repo.load_categories().build_merchant_map() == {"ACME": ("Old", False)}
