"""SQLite-backed runtime merchant and category configuration."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ledger.config.loader import load_categories, load_merchant_rules
from ledger.config.schemas import (
    AppConfig,
    CategoriesConfig,
    CategoryRule,
    LedgerConfig,
    MerchantRule,
    MerchantRulesConfig,
)
from ledger.core.exceptions import ConfigError
from ledger.storage.database import (
    CategoryConfigDB,
    CategoryMerchantDB,
    ConfigMetadataDB,
    MerchantRuleDB,
    MerchantRulePatternDB,
    init_database,
    session_scope,
)
from ledger.utils.private_files import pin_private_directory

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ledger.storage.read_only import ReadOnlyDatabase


def _canonicalize_category_memberships(
    merchants: MerchantRulesConfig,
    categories: CategoriesConfig,
) -> tuple[CategoriesConfig, list[str]]:
    """Return taxonomy with rule-owned memberships derived from merchant rules.

    Category merchant lists may also contain seed-only merchants that have no merchant
    rule. Preserve those associations, but require each unruled merchant to belong to
    exactly one category. Any merchant with a rule is removed from every seed list and
    materialized once under the rule's category.
    """
    issues: list[str] = []
    rules_by_merchant: dict[str, MerchantRule] = {}
    for rule in merchants.rules:
        key = rule.merchant_norm.upper().strip()
        previous = rules_by_merchant.get(key)
        if previous is not None:
            issues.append(
                f"Merchant {rule.merchant_norm!r} has duplicate rules targeting "
                f"{previous.category!r} and {rule.category!r}."
            )
            continue
        rules_by_merchant[key] = rule

    category_merchants: dict[str, list[str]] = {
        category.name: [] for category in categories.categories
    }
    seen_unruled: dict[str, str] = {}
    for category in categories.categories:
        for merchant in category.merchants:
            key = merchant.upper().strip()
            if not key or key in rules_by_merchant:
                continue
            previous_category = seen_unruled.get(key)
            if previous_category is not None:
                issues.append(
                    f"Unruled merchant {merchant!r} appears in both "
                    f"{previous_category!r} and {category.name!r}."
                )
                continue
            seen_unruled[key] = category.name
            category_merchants[category.name].append(merchant)

    for rule in rules_by_merchant.values():
        if rule.category in category_merchants:
            category_merchants[rule.category].append(rule.merchant_norm)

    canonical = CategoriesConfig(
        version=categories.version,
        default_category=categories.default_category,
        categories=[
            CategoryRule(
                name=category.name,
                parent=category.parent,
                merchants=category_merchants[category.name],
                exclude_from_spending=category.exclude_from_spending,
            )
            for category in categories.categories
        ],
    )
    return canonical, issues


def _validate_configs_in_memory(
    merchants: MerchantRulesConfig,
    categories: CategoriesConfig,
) -> list[str]:
    """Cross-check the in-memory config models before any destructive DB replace.

    Enforces the STRUCTURAL invariants whose violation would leave the runtime
    config broken — a merchant rule pointing at a category that does not exist, and
    a category whose parent does not exist — so a bad import can be REFUSED before a
    single row is deleted. (CategoriesConfig already rejects duplicate names and
    dangling parents at construction; this re-checks the parent and adds the
    merchant-rule -> category existence cross-reference, which construction does not
    see.) Returns a list of issue strings (empty == valid).

    Rule-owned category seed associations are canonicalized before these checks, so a
    stale seed membership cannot override the merchant rule's category.
    """
    canonical_categories, issues = _canonicalize_category_memberships(merchants, categories)
    category_names = {category.name for category in canonical_categories.categories}

    for rule in merchants.rules:
        if rule.category not in category_names:
            issues.append(
                f"Merchant rule {rule.merchant_norm!r} references missing category "
                f"{rule.category!r}."
            )

    for category in canonical_categories.categories:
        if category.parent and category.parent not in category_names:
            issues.append(
                f"Category {category.name!r} references missing parent {category.parent!r}."
            )

    # A category that other categories use as a parent must not also list merchants
    # directly (validate() enforces this post-import). Check it HERE too so an import
    # that violates it is refused BEFORE replace_from_configs deletes the good config,
    # rather than only being flagged after the destructive replace has happened.
    referenced_parents = canonical_categories.referenced_parent_names()
    for category in canonical_categories.categories:
        if category.name in referenced_parents and category.merchants:
            issues.append(
                f"Category {category.name!r} is a parent and must not list merchants directly."
            )

    # A merchant RULE whose target category is a parent is the rule-derived form of the
    # same invariant: replace_from_configs materializes each rule as a (category, merchant)
    # association INSIDE the destructive replace, so a rule pointing at a parent would make
    # that parent own a merchant and fail validate() only AFTER the good config is gone.
    # Check it here against the in-memory rules (which category.merchants above cannot see)
    # so the optimizer's leaf->parent reparenting can never commit an invalid taxonomy.
    for rule in merchants.rules:
        if rule.category in referenced_parents:
            issues.append(
                f"Merchant rule {rule.merchant_norm!r} targets parent category "
                f"{rule.category!r}, which must not own merchants directly."
            )

    return issues


@dataclass(frozen=True)
class ConfigValidationResult:
    """Summary of runtime config validation."""

    merchant_rules: int
    merchant_patterns: int
    categories: int
    category_merchants: int
    issues: list[str]

    @property
    def ok(self) -> bool:
        return not self.issues


class SQLiteConfigRepository:
    """Persist merchant/category rules in the ledger SQLite database."""

    def __init__(
        self,
        db_path: Path,
        *,
        prepare_schema: bool = True,
        read_only_database: ReadOnlyDatabase | None = None,
    ):
        """Optionally bind every config read to an explicit guarded snapshot.

        The caller owns that snapshot's lifetime. Its path must match this
        repository, and a bound repository cannot initialize or write the DB.
        Existing callers keep their normal writable schema/read behavior.
        """
        if (
            read_only_database is not None
            and db_path.expanduser().absolute() != read_only_database.path
        ):
            raise ValueError("Read-only configuration database does not match")
        self.db_path = db_path
        self._read_only_database = read_only_database
        self.prepare_schema = prepare_schema if read_only_database is None else False

    def _prepare_for_read(self) -> None:
        if self._read_only_database is not None:
            self._read_only_database.validate_current()
        elif self.prepare_schema:
            self.initialize()
        elif not self.db_path.is_file():
            raise FileNotFoundError(f"Runtime config database does not exist: {self.db_path}")

    def initialize(self) -> None:
        if self._read_only_database is not None:
            raise ConfigError("Read-only configuration repository cannot write")
        init_database(self.db_path)

    @contextmanager
    def _read_session(self) -> Iterator[Session]:
        self._prepare_for_read()
        if self._read_only_database is not None:
            with self._read_only_database.session_scope(self.db_path) as session:
                yield session
        else:
            with session_scope(self.db_path) as session:
                yield session

    def has_runtime_config(self) -> bool:
        if self._read_only_database is not None:
            with self._read_session() as session:
                tables = {
                    str(row[0])
                    for row in session.connection().exec_driver_sql(
                        "select name from sqlite_master where type = 'table' "
                        "and name in ('merchant_rules', 'category_config')"
                    )
                }
                if tables != {"merchant_rules", "category_config"}:
                    return False
                return (
                    session.query(MerchantRuleDB).count() > 0
                    and session.query(CategoryConfigDB).count() > 0
                )
        if self.prepare_schema:
            self.initialize()
            with session_scope(self.db_path) as session:
                merchant_count = session.query(MerchantRuleDB).count()
                category_count = session.query(CategoryConfigDB).count()
                return merchant_count > 0 and category_count > 0

        self._prepare_for_read()
        uri = f"{self.db_path.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "select name from sqlite_master where type = 'table' "
                    "and name in ('merchant_rules', 'category_config')"
                )
            }
            if tables != {"merchant_rules", "category_config"}:
                return False
            merchant_count = int(
                connection.execute("select count(*) from merchant_rules").fetchone()[0]
            )
            category_count = int(
                connection.execute("select count(*) from category_config").fetchone()[0]
            )
            return merchant_count > 0 and category_count > 0

    def replace_from_configs(
        self,
        merchants: MerchantRulesConfig,
        categories: CategoriesConfig,
    ) -> None:
        """Replace all runtime merchant/category config from validated models.

        Validates the candidate config IN MEMORY before any destructive replace, so
        a bad import (e.g. a merchant rule pointing at a missing category) is refused
        without overwriting the existing valid runtime config. The whole replace runs
        inside a single session_scope transaction, which rolls back on any error.
        """
        self.initialize()

        issues = _validate_configs_in_memory(merchants, categories)
        if issues:
            raise ConfigError(
                "Refusing to replace runtime config: candidate config is invalid "
                "(existing config left intact). Issues: " + "; ".join(issues)
            )

        canonical_categories, _ = _canonicalize_category_memberships(merchants, categories)

        now = datetime.now(UTC).replace(microsecond=0).isoformat()

        with session_scope(self.db_path) as session:
            session.query(MerchantRulePatternDB).delete()
            session.query(MerchantRuleDB).delete()
            session.query(CategoryMerchantDB).delete()
            session.query(CategoryConfigDB).delete()

            session.merge(ConfigMetadataDB(key="merchants_version", value=merchants.version))
            session.merge(
                ConfigMetadataDB(key="categories_version", value=canonical_categories.version)
            )
            session.merge(
                ConfigMetadataDB(
                    key="categories_default", value=canonical_categories.default_category
                )
            )
            session.merge(ConfigMetadataDB(key="config_imported_at", value=now))

            for rule_order, rule in enumerate(merchants.rules):
                db_rule = MerchantRuleDB(
                    merchant_norm=rule.merchant_norm,
                    category=rule.category,
                    exclude_from_spending=rule.exclude_from_spending,
                    sort_order=rule_order,
                )
                session.add(db_rule)
                session.flush()

                for pattern_order, pattern in enumerate(rule.patterns):
                    session.add(
                        MerchantRulePatternDB(
                            rule_id=db_rule.id,
                            pattern=pattern,
                            sort_order=pattern_order,
                        )
                    )

            category_names = {category.name for category in canonical_categories.categories}
            category_merchant_orders: dict[str, int] = {}
            seen_category_merchants: set[tuple[str, str]] = set()

            def _add_category_merchant(category_name: str, merchant: str) -> None:
                """Append a (category, merchant) association, deduped + ordered."""
                merchant_key = merchant.upper().strip()
                if not merchant_key or category_name not in category_names:
                    return
                pair = (category_name, merchant_key)
                if pair in seen_category_merchants:
                    return
                seen_category_merchants.add(pair)
                merchant_order = category_merchant_orders.get(category_name, 0)
                session.add(
                    CategoryMerchantDB(
                        category_name=category_name,
                        merchant=merchant,
                        sort_order=merchant_order,
                    )
                )
                category_merchant_orders[category_name] = merchant_order + 1

            for category_order, category in enumerate(canonical_categories.categories):
                session.add(
                    CategoryConfigDB(
                        name=category.name,
                        parent=category.parent,
                        exclude_from_spending=category.exclude_from_spending,
                        sort_order=category_order,
                    )
                )
            # Persist the canonical read model: unruled seed-only memberships plus
            # rule-owned memberships derived from MerchantRule.category.
            for category in canonical_categories.categories:
                for merchant in category.merchants:
                    _add_category_merchant(category.name, merchant)

    def import_from_toml(self, source_dir: Path) -> ConfigValidationResult:
        merchants = load_merchant_rules(source_dir / "merchants.toml")
        categories = load_categories(source_dir / "categories.toml")
        self.replace_from_configs(merchants, categories)
        return self.validate()

    def load_merchant_rules(self) -> MerchantRulesConfig:
        with self._read_session() as session:
            version = self._metadata(session, "merchants_version", "sqlite")
            rows = session.query(MerchantRuleDB).order_by(MerchantRuleDB.sort_order).all()
            rules = []
            for row in rows:
                patterns = [
                    pattern.pattern
                    for pattern in session.query(MerchantRulePatternDB)
                    .filter_by(rule_id=row.id)
                    .order_by(MerchantRulePatternDB.sort_order)
                    .all()
                ]
                rules.append(
                    MerchantRule(
                        merchant_norm=row.merchant_norm,
                        category=row.category,
                        patterns=patterns,
                        exclude_from_spending=bool(row.exclude_from_spending),
                    )
                )
            return MerchantRulesConfig(version=version, rules=rules)

    def load_categories(self) -> CategoriesConfig:
        with self._read_session() as session:
            version = self._metadata(session, "categories_version", "sqlite")
            default_category = self._metadata(session, "categories_default", "Other")
            rows = session.query(CategoryConfigDB).order_by(CategoryConfigDB.sort_order).all()
            categories = []
            for row in rows:
                merchants = [
                    item.merchant
                    for item in session.query(CategoryMerchantDB)
                    .filter_by(category_name=row.name)
                    .order_by(CategoryMerchantDB.sort_order)
                    .all()
                ]
                categories.append(
                    CategoryRule(
                        name=row.name,
                        parent=row.parent,
                        merchants=merchants,
                        exclude_from_spending=bool(row.exclude_from_spending),
                    )
                )
            return CategoriesConfig(
                version=version,
                default_category=default_category,
                categories=categories,
            )

    def load_taxonomy(self) -> CategoriesConfig:
        """Load category definitions plus only seed-only, unruled memberships.

        ``load_categories`` is the complete read model consumed by classification and
        reporting. This method is the mutation input: rule-owned memberships are
        omitted because ``MerchantRule.category`` is their canonical source.
        """
        merchants = self.load_merchant_rules()
        categories = self.load_categories()
        rule_owned = {rule.merchant_norm.upper().strip() for rule in merchants.rules}
        return CategoriesConfig(
            version=categories.version,
            default_category=categories.default_category,
            categories=[
                CategoryRule(
                    name=category.name,
                    parent=category.parent,
                    merchants=[
                        merchant
                        for merchant in category.merchants
                        if merchant.upper().strip() not in rule_owned
                    ],
                    exclude_from_spending=category.exclude_from_spending,
                )
                for category in categories.categories
            ],
        )

    def load_app_config(self, ledger: LedgerConfig) -> AppConfig:
        return AppConfig(
            ledger=ledger,
            merchants=self.load_merchant_rules(),
            categories=self.load_categories(),
        )

    def validate(self) -> ConfigValidationResult:
        merchants = self.load_merchant_rules()
        categories = self.load_categories()
        merchant_map = categories.build_merchant_map()
        category_names = {category.name for category in categories.categories}
        issues: list[str] = []

        if not merchants.rules:
            issues.append("No merchant rules are configured.")
        if not categories.categories:
            issues.append("No categories are configured.")

        # Duplicate merchant rules are rejected on the WRITE path by
        # _canonicalize_category_memberships, but merchant_rules has no unique
        # constraint, so a DB can hold them. Without this check validate() reports
        # such a config as healthy while every replace_from_configs raises ConfigError
        # -- i.e. the UI says "valid" on a config that cannot be saved.
        rule_owners: dict[str, MerchantRule] = {}
        for rule in merchants.rules:
            merchant_key = rule.merchant_norm.upper().strip()
            previous_rule = rule_owners.get(merchant_key)
            if previous_rule is not None:
                issues.append(
                    f"Merchant {rule.merchant_norm!r} has duplicate rules targeting "
                    f"{previous_rule.category!r} and {rule.category!r}."
                )
            else:
                rule_owners[merchant_key] = rule

        for rule in merchants.rules:
            merchant_key = rule.merchant_norm.upper().strip()
            mapped = merchant_map.get(merchant_key)
            if rule.category not in category_names:
                issues.append(
                    f"Merchant rule {rule.merchant_norm!r} references missing category "
                    f"{rule.category!r}."
                )
            if mapped is None:
                issues.append(
                    f"Merchant rule {rule.merchant_norm!r} is not assigned to any category."
                )
            elif mapped[0] != rule.category:
                issues.append(
                    f"Merchant rule {rule.merchant_norm!r} points to {rule.category!r}, "
                    f"but category mapping points to {mapped[0]!r}."
                )

        seen_merchants: dict[str, str] = {}
        for category in categories.categories:
            if category.parent and category.parent not in category_names:
                issues.append(
                    f"Category {category.name!r} references missing parent {category.parent!r}."
                )
            for merchant in category.merchants:
                key = merchant.upper().strip()
                previous = seen_merchants.get(key)
                if previous:
                    issues.append(
                        f"Merchant {merchant!r} appears in both {previous!r} and {category.name!r}."
                    )
                seen_merchants[key] = category.name

        referenced_parents = categories.referenced_parent_names()
        for category in categories.categories:
            if category.name in referenced_parents and category.merchants:
                issues.append(f"Parent category {category.name!r} must not list merchants.")

        return ConfigValidationResult(
            merchant_rules=len(merchants.rules),
            merchant_patterns=sum(len(rule.patterns) for rule in merchants.rules),
            categories=len(categories.categories),
            category_merchants=sum(len(category.merchants) for category in categories.categories),
            issues=issues,
        )

    def export_to_directory(self, dest_dir: Path) -> None:
        with pin_private_directory(dest_dir) as pinned:
            merchants = self.load_merchant_rules()
            categories = self.load_categories()
            pinned.validate()
            exports = [
                ("merchants.toml", self._merchants_to_toml(merchants)),
                ("categories.toml", self._categories_to_toml(categories)),
            ]
            previous = {name: pinned.read_text_if_exists(name) for name, _payload in exports}

            published: list[str] = []
            try:
                for name, payload in exports:
                    with pinned.atomic_text(name) as handle:
                        handle.write(payload)
                    published.append(name)
            except BaseException as export_error:
                rollback_errors: list[str] = []
                for name in reversed(published):
                    try:
                        old_text = previous[name]
                        if old_text is None:
                            pinned.unlink(name, missing_ok=True)
                        else:
                            with pinned.atomic_text(name) as handle:
                                handle.write(old_text)
                    except BaseException as rollback_error:
                        rollback_errors.append(f"{name}: {type(rollback_error).__name__}")
                if rollback_errors:
                    raise RuntimeError(
                        "Config export failed and rollback was incomplete: "
                        + ", ".join(rollback_errors)
                    ) from export_error
                raise
            pinned.validate()

    @staticmethod
    def _metadata(session, key: str, default: str) -> str:
        row = session.get(ConfigMetadataDB, key)
        return str(row.value) if row else default

    @staticmethod
    def _toml_string(value: str) -> str:
        return json.dumps(value)

    @classmethod
    def _toml_bool(cls, value: bool) -> str:
        return "true" if value else "false"

    @classmethod
    def _toml_list(cls, values: list[str]) -> str:
        return "[" + ", ".join(cls._toml_string(value) for value in values) + "]"

    @classmethod
    def _merchants_to_toml(cls, config: MerchantRulesConfig) -> str:
        lines = [
            f"version = {cls._toml_string(config.version)}",
            "",
        ]
        for rule in config.rules:
            lines.extend(
                [
                    "[[rule]]",
                    f"merchant_norm = {cls._toml_string(rule.merchant_norm)}",
                    f"category = {cls._toml_string(rule.category)}",
                    f"exclude_from_spending = {cls._toml_bool(rule.exclude_from_spending)}",
                    f"patterns = {cls._toml_list(rule.patterns)}",
                    "",
                ]
            )
        return "\n".join(lines).rstrip() + "\n"

    @classmethod
    def _categories_to_toml(cls, config: CategoriesConfig) -> str:
        lines = [
            f"version = {cls._toml_string(config.version)}",
            f"default_category = {cls._toml_string(config.default_category)}",
            "",
        ]
        for category in config.categories:
            lines.extend(
                [
                    "[[category]]",
                    f"category_norm = {cls._toml_string(category.name)}",
                ]
            )
            if category.parent:
                lines.append(f"parent = {cls._toml_string(category.parent)}")
            lines.extend(
                [
                    f"merchants = {cls._toml_list(category.merchants)}",
                    "exclude_from_spend_metrics = "
                    f"{cls._toml_bool(category.exclude_from_spending)}",
                    "",
                ]
            )
        return "\n".join(lines).rstrip() + "\n"


def seed_config_database(
    db_path: Path,
    merchant_rules_path: Path,
    categories_path: Path,
) -> ConfigValidationResult:
    """Replace SQLite runtime config from TOML seed files."""
    repo = SQLiteConfigRepository(db_path)
    repo.replace_from_configs(
        load_merchant_rules(merchant_rules_path),
        load_categories(categories_path),
    )
    return repo.validate()


def load_runtime_app_config(
    db_path: Path,
    ledger: LedgerConfig,
    merchant_rules_path: Path,
    categories_path: Path,
) -> AppConfig:
    """Load runtime config from SQLite, seeding from TOML when the DB is new."""
    repo = SQLiteConfigRepository(db_path)
    if not repo.has_runtime_config():
        repo.replace_from_configs(
            load_merchant_rules(merchant_rules_path),
            load_categories(categories_path),
        )
    return repo.load_app_config(ledger)
