"""
Configuration loading utilities.

Loads TOML configs and validates them against Pydantic schemas.
"""

import logging
import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ledger.config.schemas import (
    AppConfig,
    CategoriesConfig,
    CategoryRule,
    ClassificationPatterns,
    CLIDefaultsConfig,
    FuzzyConfig,
    GeneralConfig,
    LedgerConfig,
    MerchantRule,
    MerchantRulesConfig,
)
from ledger.core.exceptions import ConfigError

logger = logging.getLogger(__name__)


def _warn_unknown_keys(section: str, data: dict[str, Any], known: Iterable[str]) -> None:
    """Log a WARNING for any key in *data* not in *known*.

    A typo'd setting is otherwise silently dropped (it never matches a schema
    field), so the user gets no feedback that their config line did nothing. The
    keys are surfaced; the values are not logged (they may be config PII).
    """
    known_set = set(known)
    unknown = sorted(key for key in data if key not in known_set)
    if unknown:
        logger.warning(
            "Ignoring unrecognized key(s) in [%s]: %s. Known keys: %s",
            section,
            ", ".join(unknown),
            ", ".join(sorted(known_set)),
        )


def load_toml(path: Path) -> dict[str, Any]:
    """Load a TOML file."""
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"Invalid TOML in {path}: {e}")


def load_ledger_config(path: Path) -> LedgerConfig:
    """Load ledger configuration from TOML."""
    data = load_toml(path)

    general_data = data.get("general", {})
    # Pass only the keys explicitly present in the TOML; absent keys fall back to
    # the Field(default=...) declared in GeneralConfig so defaults live in one place.
    general_known = (
        "strict",
        "etransfer_is_cashflow",
        "cc_payment_is_transfer",
        "enable_transfer_matching",
        "transfer_match_window",
        "transfer_amount_tolerance",
        "transfer_min_confidence",
        "account_aliases",
    )
    _warn_unknown_keys("general", general_data, general_known)
    general_kwargs: dict[str, Any] = {}
    for key in (
        "strict",
        "etransfer_is_cashflow",
        "cc_payment_is_transfer",
        "enable_transfer_matching",
        "transfer_match_window",
        "transfer_amount_tolerance",
        "transfer_min_confidence",
    ):
        if key in general_data:
            general_kwargs[key] = general_data[key]
    from ledger.adapters.registry import compatibility_policy

    declared_aliases = compatibility_policy().get("account_aliases", {})
    explicit_aliases = general_data.get("account_aliases", {})
    if not isinstance(explicit_aliases, dict):
        raise ConfigError("account_aliases must be a mapping")
    general_kwargs["account_aliases"] = {**declared_aliases, **explicit_aliases}
    general = GeneralConfig(**general_kwargs)

    fuzzy_data = data.get("fuzzy", {})
    fuzzy_known = ("enabled", "threshold", "min_merchant_length")
    _warn_unknown_keys("fuzzy", fuzzy_data, fuzzy_known)
    # Same pattern: only forward keys that are explicitly set in the TOML.
    fuzzy_kwargs: dict[str, Any] = {k: fuzzy_data[k] for k in fuzzy_known if k in fuzzy_data}
    fuzzy = FuzzyConfig(**fuzzy_kwargs)

    rules = data.get("rules", {})

    def load_patterns(key: str) -> ClassificationPatterns:
        r = rules.get(key, {})
        # Forward only explicitly-set pattern lists; absent lists default to [] via the schema.
        pattern_fields = (
            "transfer_patterns",
            "etransfer_patterns",
            "cc_payment_patterns",
            "fee_patterns",
            "interest_patterns",
            "income_patterns",
            "payment_patterns",
            "reward_patterns",
        )
        # Warn on typo'd pattern keys (e.g. transfr_patterns): the schema silently
        # drops them, so an inert rule line would otherwise give no feedback.
        _warn_unknown_keys(f"rules.{key}", r, pattern_fields)
        return ClassificationPatterns(**{f: r[f] for f in pattern_fields if f in r})

    return LedgerConfig(
        general=general,
        fuzzy=fuzzy,
        rules={key.lower(): load_patterns(key) for key in rules},
    )


def load_merchant_rules(path: Path) -> MerchantRulesConfig:
    """Load merchant rules from TOML."""
    data = load_toml(path)

    version = str(data.get("version", "unknown"))
    rules_data = data.get("rule", [])

    rules = []
    for idx, r in enumerate(rules_data):
        try:
            # Map old field names to new schema
            merchant_norm = str(r.get("merchant_norm", "")).strip()
            category = str(r.get("merchant_group", r.get("category", "Other"))).strip()
            patterns = r.get("patterns", [])

            if not merchant_norm:
                continue

            rules.append(
                MerchantRule(
                    merchant_norm=merchant_norm,
                    category=category,
                    patterns=patterns,
                    exclude_from_spending=r.get("exclude_from_spending", False),
                )
            )
        except Exception as e:
            raise ConfigError(f"Invalid merchant rule at index {idx}: {e}")

    return MerchantRulesConfig(version=version, rules=rules)


def load_categories(path: Path) -> CategoriesConfig:
    """Load categories from TOML."""
    data = load_toml(path)

    version = str(data.get("version", "unknown"))
    default_category = str(data.get("default_category", "Other"))
    cats_data = data.get("category", [])

    try:
        # The per-row build is inside the guard too: a single bad row (an empty
        # category_norm tripping CategoryRule's min_length, or a malformed TOML where
        # `category` is not a list of tables, raising AttributeError on c.get) must
        # surface as a ConfigError naming the file — like the other loaders — not a
        # raw pydantic ValidationError / AttributeError.
        categories = []
        for c in cats_data:
            parent = c.get("parent")
            categories.append(
                CategoryRule(
                    name=str(c.get("category_norm", "")).strip(),
                    parent=str(parent).strip() if parent else None,
                    merchants=[str(m).strip() for m in c.get("merchants", [])],
                    exclude_from_spending=c.get("exclude_from_spend_metrics", False),
                )
            )

        return CategoriesConfig(
            version=version,
            default_category=default_category,
            categories=categories,
        )
    except ConfigError:
        raise
    except Exception as e:
        raise ConfigError(f"Invalid categories config in {path}: {e}") from e


def load_app_config(
    ledger_path: Path,
    merchants_path: Path,
    categories_path: Path,
) -> AppConfig:
    """
    Load complete application configuration.

    Validates all configs and builds lookup tables.
    """
    ledger = load_ledger_config(ledger_path)
    merchants = load_merchant_rules(merchants_path)
    categories = load_categories(categories_path)

    return AppConfig(
        ledger=ledger,
        merchants=merchants,
        categories=categories,
    )


# === CLI Defaults ===


def find_cli_defaults_file() -> Path | None:
    """Read defaults only from the selected private runtime, never a checkout."""
    from ledger.runtime import runtime_path

    path = runtime_path("defaults.toml")
    return path if path.is_file() else None


def load_cli_defaults(path: Path | None = None) -> CLIDefaultsConfig:
    """
    Load CLI defaults from configuration file.

    If path is None, searches standard locations.
    Returns empty config if no file is found.
    """
    if path is None:
        path = find_cli_defaults_file()

    if path is None or not path.exists():
        return CLIDefaultsConfig()

    try:
        data = load_toml(path)
        defaults_data = data.get("defaults", {})
        _warn_unknown_keys("defaults", defaults_data, CLIDefaultsConfig.model_fields)
        return CLIDefaultsConfig(**defaults_data)
    except ConfigError:
        raise
    except Exception as e:
        raise ConfigError(f"Invalid CLI defaults in {path}: {e}")


# === Config version extraction ===


def get_config_versions(
    ledger_path: Path,
    merchants_path: Path,
    categories_path: Path,
) -> dict[str, str]:
    """Extract version strings from config files."""
    versions = {}

    for name, path in [
        ("ledger", ledger_path),
        ("merchants", merchants_path),
        ("categories", categories_path),
    ]:
        try:
            data = load_toml(path)
            versions[name] = str(data.get("version", "unknown"))
        except Exception:
            versions[name] = "error"

    return versions
