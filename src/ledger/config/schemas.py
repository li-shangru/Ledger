"""
Configuration schemas with full Pydantic validation.

All configuration is validated at load time to catch errors early.
"""

import re
from functools import cached_property

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# ReDoS static guard
# ---------------------------------------------------------------------------
# Detect the classic catastrophic-backtracking shape: a group that contains an
# unbounded quantifier (+ or *) and is itself followed by an unbounded quantifier
# (+, *, or {n,} / {n,m}).  Only the nested form causes exponential backtracking;
# a single outer quantifier on a literal group is fine.
#
# Pattern breakdown:
#   \(           — open paren (start of group)
#   [^)]*        — anything inside (conservative: avoids crossing group boundaries)
#   [+*]         — inner unbounded quantifier anywhere inside the group
#   [^)]*        — rest of group content
#   \)           — close paren (end of group)
#   (?:[+*]|\{[0-9]*,[0-9]*\})
#                — outer unbounded quantifier: bare +/*, or a {n,}/{n,m} form
#
# This is conservative: it only matches the naked nested form.  Things like
# possessive quantifiers or atomic groups (not available in Python's `re`) are
# outside scope; the goal is to catch obviously dangerous user-authored patterns
# without rejecting any legitimate ones.
_REDOS_RE = re.compile(r"\([^)]*[+*][^)]*\)(?:[+*]|\{[0-9]*,[0-9]*\})")

# NOTE (fix 7, DEFERRED): strengthening this guard to also flag alternation-overlap
# under a quantifier (e.g. (a|a)* / (a|ab)*) cannot be done statically with a simple
# regex without false-positives. A flat-alternation matcher also flags SAFE patterns
# like (foo|bar)* — non-overlapping alternatives that do NOT backtrack catastrophically
# — which tests/test_audit_lows_robustness.py explicitly documents as legitimate.
# Distinguishing overlapping from disjoint alternatives needs real prefix/automaton
# analysis (out of scope for a literal-string guard), so the alternation case is left
# to a future structural check rather than risk rejecting real configs.


def _check_redos(pattern: str) -> None:
    """Raise ValueError if *pattern* contains a nested unbounded quantifier.

    Only the shape ``(x+)+``, ``(x*)*``, ``(x+)*``, ``(.*)+`` etc. is flagged —
    see module-level comment for details.
    """
    if _REDOS_RE.search(pattern):
        raise ValueError(
            f"Potentially catastrophic regex pattern '{pattern}': "
            "nested unbounded quantifiers (e.g. (x+)+, (x*)*) can cause "
            "catastrophic backtracking (ReDoS). "
            "Rewrite using possessive quantifiers, atomic groups, or anchors."
        )


def merchant_match_texts(merchant_raw: str, description: str) -> tuple[str, str, str]:
    """Return the normalized raw evidence sequence every rule path must search."""

    def normalized(value: str) -> str:
        return re.sub(r"\s+", " ", value or "").strip()

    raw = normalized(merchant_raw)
    detail = normalized(description)
    return raw, detail, normalized(f"{raw} {detail}")


class CompiledPattern(BaseModel):
    """A compiled regex pattern with its source string."""

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    source: str
    pattern: re.Pattern

    @classmethod
    def from_string(cls, pattern_str: str) -> CompiledPattern:
        """Compile a pattern string."""
        _check_redos(pattern_str)
        try:
            compiled = re.compile(pattern_str, re.IGNORECASE)
            return cls(source=pattern_str, pattern=compiled)
        except re.error as e:
            raise ValueError(f"Invalid regex pattern '{pattern_str}': {e}")

    def matches(self, text: str) -> bool:
        """Check if pattern matches text."""
        return bool(self.pattern.search(text or ""))


# === Merchant Rules ===


class MerchantRule(BaseModel):
    """Single merchant matching rule."""

    model_config = ConfigDict(frozen=True)

    merchant_norm: str = Field(min_length=1, description="Normalized merchant name")
    category: str = Field(default="Other", description="Default category for this merchant")
    patterns: list[str] = Field(min_length=1, description="Regex patterns to match")
    exclude_from_spending: bool = Field(default=False)

    @field_validator("patterns")
    @classmethod
    def validate_patterns(cls, v: list[str]) -> list[str]:
        """Validate all patterns are valid regex and free of nested unbounded quantifiers."""
        for p in v:
            _check_redos(p)
            try:
                re.compile(p, re.IGNORECASE)
            except re.error as e:
                raise ValueError(f"Invalid regex '{p}': {e}")
        return v

    @cached_property
    def _compiled(self) -> list[CompiledPattern]:
        """Get compiled patterns (cached)."""
        return [CompiledPattern.from_string(p) for p in self.patterns]

    def matches(self, text: str) -> bool:
        """Check if any pattern matches the text."""
        return any(cp.matches(text) for cp in self._compiled)


class MerchantRulesConfig(BaseModel):
    """Complete merchant rules configuration."""

    model_config = ConfigDict(frozen=True)

    version: str = Field(default="1.0")
    rules: list[MerchantRule] = Field(default_factory=list)

    def find_merchant(self, *texts: str, display_name: str | None = None) -> tuple[str, str, bool]:
        """
        Resolve a merchant from raw statement text.

        ``texts`` are RAW statement fields (description, merchant_raw, ...) in
        priority order and are matched by PATTERN. The loop is text-major on
        purpose: an earlier text's match wins over a later text's match even when
        the later text's rule sorts first. Rule-major iteration let a broad rule
        with a low ``sort_order`` beat a specific rule regardless of which text
        matched, so the least authoritative field could decide the outcome.

        ``display_name`` is an ALREADY-RESOLVED merchant label (``transactions.merchant``,
        or a name a user typed in the Review Queue). It is deliberately NOT matched
        against rule patterns: a label is an output of resolution, not evidence about
        the transaction, and pattern-matching it made any name that merely contained
        another rule's pattern inherit that rule's ``exclude_from_spending`` — silently
        dropping the row out of spending. It is instead matched by IDENTITY against
        ``merchant_norm`` (case- and whitespace-insensitive), so naming a configured
        merchant still resolves to that merchant's own rule and keeps a genuine
        exclusion, and only as a fallback when no raw text matched.

        Returns: (merchant_norm, category, exclude_from_spending)
        """
        for text in texts:
            if not text:
                continue
            for rule in self.rules:
                if rule.matches(text):
                    return rule.merchant_norm, rule.category, rule.exclude_from_spending

        if display_name:
            key = display_name.strip().casefold()
            for rule in self.rules:
                if rule.merchant_norm.strip().casefold() == key:
                    return rule.merchant_norm, rule.category, rule.exclude_from_spending

        return "", "Other", False


# === Categories ===


class CategoryRule(BaseModel):
    """Category with associated merchants."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    parent: str | None = Field(default=None, description="Parent category for hierarchy")
    merchants: list[str] = Field(default_factory=list)
    exclude_from_spending: bool = Field(default=False)

    @property
    def is_parent(self) -> bool:
        """Check if this is a parent category (has no merchants)."""
        return len(self.merchants) == 0

    @property
    def display_name(self) -> str:
        """Get display name with parent prefix if applicable."""
        if self.parent:
            return f"{self.parent} > {self.name}"
        return self.name


class CategoriesConfig(BaseModel):
    """Categories configuration."""

    model_config = ConfigDict(frozen=True)

    version: str = Field(default="1.0")
    default_category: str = Field(default="Other")
    categories: list[CategoryRule] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_hierarchy(self) -> CategoriesConfig:
        """Validate name uniqueness and parent-child relationships."""
        # Category name is the primary key of the runtime config table, so a duplicate
        # would raise a raw IntegrityError at DB insert. Catch it here (covers both the
        # TOML loader and runtime/UI construction) with a clear message instead.
        category_names: set[str] = set()
        for cat in self.categories:
            if cat.name in category_names:
                raise ValueError(f"Duplicate category name '{cat.name}'")
            category_names.add(cat.name)

        for cat in self.categories:
            if cat.parent and cat.parent not in category_names:
                raise ValueError(
                    f"Category '{cat.name}' references non-existent parent '{cat.parent}'"
                )
            # A category cannot be its own parent (a 1-node cycle).
            if cat.parent and cat.parent == cat.name:
                raise ValueError(f"Category '{cat.name}' cannot be its own parent")

        # Reject any parent cycle (A -> B -> ... -> A). Walking the parent chain
        # from each node and detecting a revisit catches the trivial 2-node case
        # (A->B, B->A) and longer loops, which would otherwise produce an infinite
        # hierarchy traversal downstream.
        parent_of = {cat.name: cat.parent for cat in self.categories}
        for start in parent_of:
            seen: set[str] = set()
            node: str | None = start
            while node is not None:
                if node in seen:
                    raise ValueError(f"Category parent cycle detected involving '{node}'")
                seen.add(node)
                node = parent_of.get(node)

        return self

    def build_merchant_map(self) -> dict[str, tuple[str, bool]]:
        """
        Build merchant -> (category, exclude) mapping.
        First category wins for duplicates.
        """
        result: dict[str, tuple[str, bool]] = {}
        for cat in self.categories:
            for merchant in cat.merchants:
                key = merchant.upper().strip()
                if key and key not in result:
                    result[key] = (cat.name, cat.exclude_from_spending)
        return result

    def get_parent_categories(self) -> list[CategoryRule]:
        """Get all parent categories (categories with no merchants)."""
        return [cat for cat in self.categories if cat.is_parent]

    def referenced_parent_names(self) -> set[str]:
        """Names of categories referenced as another category's parent.

        This is the definition that matters for the 'a parent category must not
        own merchants' rule (see config validation) — distinct from `is_parent`
        (which means 'has no merchants').
        """
        return {c.parent for c in self.categories if c.parent}

    def get_child_categories(self, parent_name: str) -> list[CategoryRule]:
        """Get all child categories for a parent."""
        return [cat for cat in self.categories if cat.parent == parent_name]

    def get_category_hierarchy(self) -> dict[str, list[str]]:
        """
        Build parent -> [children] mapping.

        Returns:
            Dict mapping parent category names to list of child category names.
            Includes entries for categories with no children (empty list).
        """
        hierarchy: dict[str, list[str]] = {}

        # Initialize all categories
        for cat in self.categories:
            if cat.is_parent or cat.parent is None:
                hierarchy[cat.name] = []

        # Build parent-child relationships
        for cat in self.categories:
            if cat.parent:
                if cat.parent not in hierarchy:
                    hierarchy[cat.parent] = []
                hierarchy[cat.parent].append(cat.name)

        return hierarchy

    def get_category_with_parent(self, category_name: str) -> str | None:
        """
        Get parent category name for a given category.

        Returns None if category has no parent or doesn't exist.
        """
        for cat in self.categories:
            if cat.name == category_name:
                return cat.parent
        return None


# === Classification Patterns ===


class ClassificationPatterns(BaseModel):
    """Patterns for transaction classification."""

    model_config = ConfigDict(frozen=True)

    transfer_patterns: list[str] = Field(default_factory=list)
    etransfer_patterns: list[str] = Field(default_factory=list)
    cc_payment_patterns: list[str] = Field(default_factory=list)
    fee_patterns: list[str] = Field(default_factory=list)
    interest_patterns: list[str] = Field(default_factory=list)
    income_patterns: list[str] = Field(default_factory=list)
    payment_patterns: list[str] = Field(default_factory=list)
    reward_patterns: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_patterns(self) -> ClassificationPatterns:
        """Validate all patterns are valid regex and free of nested unbounded quantifiers."""
        for field in type(self).model_fields:
            value = getattr(self, field, None)
            if not isinstance(value, list):
                continue
            for p in value:
                _check_redos(p)
                try:
                    re.compile(p, re.IGNORECASE)
                except re.error as e:
                    raise ValueError(f"Invalid regex in {field} {p!r}: {e}")
        return self

    @cached_property
    def _compiled_cache(self) -> dict[str, list[CompiledPattern]]:
        """Compile all patterns once (cached)."""
        return {
            field: [CompiledPattern.from_string(p) for p in getattr(self, field)]
            for field in type(self).model_fields
            if isinstance(getattr(self, field, None), list)
        }

    def matches(self, field: str, text: str) -> bool:
        """Check if any pattern in field matches text."""
        patterns = self._compiled_cache.get(field, [])
        return any(cp.matches(text) for cp in patterns)


# === Main Ledger Config ===


class GeneralConfig(BaseModel):
    """General ledger settings."""

    model_config = ConfigDict(frozen=True)

    strict: bool = Field(default=True, description="Fail on errors vs warn")
    etransfer_is_cashflow: bool = Field(default=True)
    cc_payment_is_transfer: bool = Field(default=True)
    account_aliases: dict[str, str] = Field(default_factory=dict)

    # Transfer matching settings
    enable_transfer_matching: bool = Field(
        default=True, description="Enable automatic transfer matching across accounts"
    )
    transfer_match_window: int = Field(
        default=5, description="Days before/after to search for matching transfers"
    )
    transfer_amount_tolerance: float = Field(
        default=0.0,
        description="Percentage tolerance for amount differences (0.0 = exact match only)",
    )
    transfer_min_confidence: float = Field(
        default=0.5, description="Minimum confidence score to auto-match (0.0-1.0)"
    )


class FuzzyConfig(BaseModel):
    """Fuzzy matching configuration for merchant fallback."""

    model_config = ConfigDict(frozen=True)

    enabled: bool = Field(default=False, description="Enable fuzzy matching fallback")
    threshold: int = Field(default=85, ge=0, le=100, description="Similarity threshold (0-100)")
    min_merchant_length: int = Field(
        default=3, ge=1, description="Minimum length for fuzzy matching"
    )


class LedgerConfig(BaseModel):
    """Complete ledger configuration."""

    model_config = ConfigDict(frozen=True)

    general: GeneralConfig = Field(default_factory=GeneralConfig)
    fuzzy: FuzzyConfig = Field(default_factory=FuzzyConfig)
    rules: dict[str, ClassificationPatterns] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _legacy_keyed_rules(cls, value):
        # Preserve arbitrary legacy keyed constructor inputs without a provider list.
        if isinstance(value, dict):
            value = dict(value)
            rules = dict(value.get("rules", {}))
            for key in list(value):
                if key not in {"general", "fuzzy", "rules"}:
                    if key in rules:
                        raise ValueError("Duplicate classification rule key")
                    rules[key] = value.pop(key)
            value["rules"] = rules
        return value

    def get_patterns(self, institution: str) -> ClassificationPatterns:
        """Resolve an explicitly keyed private rule set without enumerating providers."""
        from ledger.adapters.registry import compatibility_policy

        key = institution.lower()
        if key in self.rules:
            return self.rules[key]
        defaults = compatibility_policy()["classification_rules"].get(key, {})
        return ClassificationPatterns(**defaults)


# === CLI Defaults ===


class CLIDefaultsConfig(BaseModel):
    """Default values for CLI arguments."""

    model_config = ConfigDict(frozen=True)

    src: str | None = Field(default=None, description="Default source directory")
    out_dir: str | None = Field(default=None, description="Default output directory")
    ledger_config: str | None = Field(default=None, description="Default ledger config path")
    merchant_rules: str | None = Field(default=None, description="Default merchant rules path")
    categories: str | None = Field(default=None, description="Default categories path")
    db: str | None = Field(default=None, description="Default database path")
    year: str | None = Field(default=None, description="Default year filter")
    month: str | None = Field(default=None, description="Default month filter")
    quarter: str | None = Field(default=None, description="Default quarter filter")
    formats: str = Field(
        default="csv",
        description="Default output formats (only parquet requires the 'analysis' extra; csv and json work on a core install)",
    )
    log_level: str = Field(default="INFO", description="Default log level")


# === Unified Config Container ===


class AppConfig(BaseModel):
    """
    Complete application configuration.

    Holds all loaded and validated configuration.
    """

    model_config = ConfigDict(frozen=True)

    ledger: LedgerConfig
    merchants: MerchantRulesConfig
    categories: CategoriesConfig

    @cached_property
    def merchant_to_category(self) -> dict[str, tuple[str, bool]]:
        """Get merchant -> (category, exclude) mapping (cached)."""
        return self.categories.build_merchant_map()

    @cached_property
    def category_exclusions(self) -> dict[str, bool]:
        """category name -> exclude_from_spending (cached).

        The single source for deriving a category's spend-exclusion by name, so the
        import-time enrichment path (parsers.base._enrich) and the rules-apply pass
        (rules/service.py) resolve the same flag for the same (merchant, category).
        """
        return {cat.name: cat.exclude_from_spending for cat in self.categories.categories}

    @property
    def strict(self) -> bool:
        return self.ledger.general.strict

    @property
    def default_category(self) -> str:
        return self.categories.default_category
