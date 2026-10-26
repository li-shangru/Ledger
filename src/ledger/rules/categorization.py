"""Turn a one-off category correction into a reusable merchant rule.

When you fix a transaction's category, `remember_category` upserts a `MerchantRule` and
re-applies it across every transaction, so you stop re-fixing the same merchant on each
import. The pattern derivation is intentionally conservative (it prefers a specific literal),
and the UI shows the derived pattern + a live match count before saving so an over-broad
rule can never silently rewrite history.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from ledger.config.repository import SQLiteConfigRepository
from ledger.config.schemas import (
    CategoriesConfig,
    MerchantRule,
    MerchantRulesConfig,
    merchant_match_texts,
)
from ledger.core.enums import Confidence
from ledger.core.results import RunResult
from ledger.imports.pdf.readiness import (
    OPEN_IMPORT_RUN_REMEDY as _OPEN_IMPORT_RUN_REMEDY,
)
from ledger.imports.pdf.readiness import (
    LiveMutationDuringOpenImportRefused,
    live_mutation_boundary,
    refuse_live_mutation_during_open_import,
)
from ledger.rules.configuration import RuntimeConfigMutationService
from ledger.rules.service import RuleApplicationResult
from ledger.storage.database import TransactionDB, session_scope
from ledger.storage.operations import record_db_operation
from ledger.storage.operations import utc_now_dt as utc_now

logger = logging.getLogger(__name__)

# Processor / point-of-sale prefixes that prepend the real merchant name. The word prefixes
# are \b-anchored so "POSITANO" / "DEBITS R US" are NOT stripped; the "*" forms cover Square,
# PayPal, Toast, etc.
_LEADING_NOISE = re.compile(
    r"^(?:POS\b|PURCHASE\b|DEBIT\b|INTERAC\b|SQ\s*\*|TST\s*\*|PP\s*\*|PAYPAL\s*\*|SP\s*\*)\s*",
    re.IGNORECASE,
)
# Statement suffixes routinely append a store, terminal, phone, or reference token before
# the city/province. A token with at least two digits is treated as visit-specific once a
# merchant head has already been retained; short names such as "A1" remain intact.
_VARIABLE_TOKEN = re.compile(r"^(?:#?\d{2,}|(?=\S*\d.*\d)\S+)$")
_TRAILING_KIND = re.compile(r"\s*\((?:purchase|credit|refund|reversal|reward)\)\s*$", re.IGNORECASE)
_TRAILING_PROVINCE = re.compile(r"^(?:AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT)$", re.IGNORECASE)


def _strip_private_transaction_prefix(value: str) -> str:
    from ledger.adapters.registry import compatibility_policy

    for pattern in compatibility_policy().get("merchant_prefix_patterns", []):
        value = re.sub(pattern, "", value, flags=re.IGNORECASE)
    return value


_LEADING_ETRANSFER_CHANNEL = re.compile(
    r"^(?:INTERAC\s+)?E-?TRANSFER\b",
    re.IGNORECASE,
)
_ETRANSFER_DIRECTION_TOKENS = {"SENT", "RECEIVED", "FROM", "TO", "OUT", "IN", "MONEY"}
_ETRANSFER_NUMERIC_REFERENCE = re.compile(r"^#?\d{2,}$")


@dataclass(frozen=True)
class RuleSuggestion:
    """A pre-filled, user-editable rule proposal derived from one transaction."""

    merchant_norm: str
    pattern: str
    category: str


class AutomaticRuleDerivationUnavailable(ValueError):  # noqa: N818 - a refusal, not a failure
    """A safe, expected refusal to infer a rule from insufficient raw evidence."""

    def __init__(self, reason: str):
        self.reason = reason
        if reason == "missing_raw":
            message = (
                "Automatic rule derivation requires raw statement text from merchant_raw or "
                "description; supply an explicit pattern derived from raw statement evidence."
            )
        elif reason == "ambiguous_etransfer":
            message = (
                "Automatic rule derivation cannot identify an e-transfer counterparty; "
                "supply an explicit pattern derived from raw statement evidence."
            )
        else:  # pragma: no cover - internal programming error
            raise ValueError("Unknown automatic rule derivation refusal")
        super().__init__(message)


def _etransfer_counterparty(raw_text: str, *, channel_required: bool) -> str:
    """Return counterparty evidence after supported channel/reference noise."""
    normalized = re.sub(r"\s+", " ", raw_text).strip()
    channel = _LEADING_ETRANSFER_CHANNEL.match(normalized)
    if channel_required and channel is None:
        return ""
    remainder = normalized[channel.end() :].strip() if channel else normalized
    tokens = remainder.split()
    while tokens:
        token = tokens[0].strip(" :-/").upper()
        if token in _ETRANSFER_DIRECTION_TOKENS or _ETRANSFER_NUMERIC_REFERENCE.fullmatch(token):
            tokens.pop(0)
            continue
        break
    return " ".join(tokens).strip()


@dataclass(frozen=True)
class RuleUpdatePreview:
    """Target-rule-specific result of evaluating the updated full ruleset."""

    transactions_updated: int
    stored_merchant_exclude_from_spending: bool
    effective_exclude_from_spending: bool


class SpendSemanticsChangeRefused(ValueError):  # noqa: N818 - a refusal, not a failure
    """Raised when teaching would change what a stored rule counts as spending.

    ``exclude_from_spending`` decides whether a merchant's rows are inside or outside
    the spend universe every report is computed over, so flipping it is a semantic
    change to the ledger, not a detail of the rule being taught. It is therefore never
    a side effect: an explicit value that DIFFERS from the stored one must be paired
    with ``allow_spend_semantics_change=True``.

    Subclasses ``ValueError`` deliberately so any caller that explicitly requests a
    spend-semantics change gets the same validation contract as an invalid rule. The
    current teaching surfaces intentionally omit that request and therefore preserve
    the stored value; the exception remains part of the lower-level explicit-change API.
    """


TeachingDuringOpenImportRefused = LiveMutationDuringOpenImportRefused
OPEN_IMPORT_RUN_REMEDY = _OPEN_IMPORT_RUN_REMEDY


# ── The versioned refusal (D23) ────────────────────────────────────────
#
# Teaching rewrites BOTH the merchant-rule config tables and `transactions`, and both are
# import-owned by deliberate fail-safe choice (`imports/pdf/readiness.py`, the
# DRIFT_EXEMPT_TABLES boundary). So a live teach during an open run trips
# `import_owned_fingerprint` twice over: the pre-promote drift recheck aborts and staged
# decisions can be stranded. The central guard covers ordinary finalizable states and
# finalize-recovery states before any live write can create that failure.
#
# The sentence below names ONLY remedies that exist at 1.4.0. P9 adds the deferred path
# (`data/proposals.db` + `ledger proposals`) and MUST rewrite the imported constant in
# ``imports/pdf/readiness.py`` in the same commit that makes the queue real: a refusal
# naming a command the operator cannot run is
# worse than one naming a command they can. That is not left to memory —
# `tests/test_review_teach.py::
# test_the_refusal_offers_the_proposal_queue_exactly_when_the_queue_exists`
# fails the moment `ledger proposals` is registered without this being amended.
def _literal_pattern(text: str) -> str:
    """A literal regex that matches `text` as a substring, kept readable (spaces unescaped)."""
    return re.escape(text).replace("\\ ", " ")


def _etransfer_pattern(counterparty: str) -> str:
    """Bind a counterparty literal to an e-transfer channel and token boundaries."""
    literal = _literal_pattern(counterparty)
    bounded_counterparty = rf"(?<![A-Z0-9]){literal}(?![A-Z0-9])"
    return (
        r"(?:^|\s)(?:INTERAC\s+)?E-?TRANSFER\b"
        rf"(?=[^\r\n]{{0,160}}{bounded_counterparty})"
    )


def _clean_identifier(text: str) -> str:
    collapsed = re.sub(r"\s+", " ", text).strip()
    cleaned = _LEADING_NOISE.sub(
        "", _strip_private_transaction_prefix(_TRAILING_KIND.sub("", collapsed))
    ).strip()
    # Store markers and statement references delimit a stable merchant head even when
    # the statement glues the suffix to the next token (for example ``#1234CITY``).
    head = re.split(r"[#*]", cleaned, maxsplit=1)[0].strip() or cleaned
    tokens: list[str] = []
    for token in head.split():
        if tokens and _VARIABLE_TOKEN.match(token):
            break
        tokens.append(token)

    # Province-qualified descriptions append a location. Remove the province and one
    # city token while retaining at least the merchant head. Store-number cases were
    # already cut at the stronger delimiter above.
    if len(tokens) > 1 and _TRAILING_PROVINCE.match(tokens[-1]):
        tokens.pop()
        if len(tokens) > 1 and tokens[-1].isalpha():
            tokens.pop()
    return " ".join(tokens).rstrip(" -/").upper()


def derive_rule_suggestion(
    merchant: str, merchant_raw: str, description: str, current_category: str
) -> RuleSuggestion:
    """Propose (merchant_norm, pattern, category) from a transaction.

    The PATTERN always comes from the transaction's RAW statement text
    (``merchant_raw``, else ``description``), cleaned of POS prefixes and statement-varying
    store, reference, phone, and location suffixes so it generalizes across visits (D21).
    It is deliberately NOT derived from
    ``merchant``: that is a RESOLVED DISPLAY NAME, an output of a previous resolution,
    and import enrichment (``parsers/base.py``) matches rule patterns against raw text
    only — it never passes a display name. A label-derived pattern therefore fired at
    import only when the label happened to be a case-insensitive substring of the raw
    text. Synthetic tests pin label/raw mismatches and future statements with changing
    store, phone, fulfilment, and location suffixes.

    The MERCHANT_NORM is still the resolved label when there is one: it is the rule's
    key, so keeping it makes ``upsert_merchant_rule`` land on the merchant's existing
    rule and append this pattern, rather than creating a near-duplicate rule keyed on
    raw text. Never emits an empty identifier: over-stripping falls back to the
    whitespace-normalized RAW source. A row with neither raw field is refused; deriving
    from its resolved label would violate D21 and falsely promise future-import reuse.
    """
    category = current_category or "Other"
    label = merchant.strip().upper()

    raw_merchant = merchant_raw.strip()
    raw_description = description.strip()
    source = raw_merchant or raw_description
    if not source:
        raise AutomaticRuleDerivationUnavailable("missing_raw")
    etransfer_identifier = ""
    if _LEADING_ETRANSFER_CHANNEL.match(source):
        counterparty = _etransfer_counterparty(source, channel_required=True)
        if not counterparty and raw_description and raw_description != source:
            counterparty = _etransfer_counterparty(raw_description, channel_required=False)
        if not counterparty:
            raise AutomaticRuleDerivationUnavailable("ambiguous_etransfer")
        source = counterparty
        etransfer_identifier = _clean_identifier(counterparty)
    identifier = _clean_identifier(source)
    if len(identifier) < 3:
        identifier = re.sub(r"\s+", " ", source).strip().upper()
    pattern = (
        _etransfer_pattern(etransfer_identifier or identifier)
        if etransfer_identifier
        else _literal_pattern(identifier)
    )
    return RuleSuggestion(label or identifier, pattern, category)


def upsert_merchant_rule(
    config: MerchantRulesConfig,
    merchant_norm: str,
    pattern: str,
    category: str,
    exclude_from_spending: bool | None = None,
    *,
    category_exclusions: Mapping[str, bool] | None = None,
    allow_spend_semantics_change: bool = False,
) -> MerchantRulesConfig:
    """Return a new config with the merchant's rule created or updated.

    An existing merchant (matched case-insensitively) keeps its other patterns; its category
    is overwritten to `category` and `pattern` is added if missing. Raises ValueError via the
    MerchantRule validator if `pattern` is not a valid regex.

    ``exclude_from_spending`` is TRI-STATE (D20):

    * ``None`` (the default, and what the teaching surfaces pass) PRESERVES an existing
      rule's stored flag even when its category changes. This can deliberately leave the
      merchant-level flag different from the target category: D20 makes preserving stored
      spend semantics the stronger invariant. For a NEW rule, ``None`` derives from the
      target category's own flag via ``category_exclusions`` and falls back to ``False``
      only when the caller supplied no category exclusions at all.
    * An explicit ``True``/``False`` that MATCHES the stored flag is a no-op.
    * An explicit value that DIFFERS from the stored flag raises
      :class:`SpendSemanticsChangeRefused` unless ``allow_spend_semantics_change`` is set.
      A new rule has no stored flag, so an explicit value there needs no authorization.

    The default used to be a plain ``False``, which the existing-rule branch wrote back
    unconditionally while carrying ``merchant_norm`` and ``patterns`` forward — so the
    one production caller, which omits the kwarg, silently un-excluded any excluding
    merchant it re-taught.
    """
    key = merchant_norm.strip().upper()
    if not pattern.strip():
        # An empty pattern compiles and then matches every transaction, so a rule built
        # from one claims the whole ledger under this merchant's name.
        raise ValueError(
            f"Refusing to build a rule for {key or merchant_norm!r} from an empty pattern: "
            "an empty regex matches every transaction."
        )
    rules = list(config.rules)
    for index, rule in enumerate(rules):
        if rule.merchant_norm.strip().upper() == key:
            patterns = list(rule.patterns)
            if pattern not in patterns:
                patterns.append(pattern)
            rules[index] = MerchantRule(
                merchant_norm=rule.merchant_norm,
                category=category,
                patterns=patterns,
                exclude_from_spending=_resolved_exclusion(
                    stored=bool(rule.exclude_from_spending),
                    requested=exclude_from_spending,
                    merchant_norm=rule.merchant_norm,
                    allow_spend_semantics_change=allow_spend_semantics_change,
                ),
            )
            return MerchantRulesConfig(version=config.version, rules=rules)

    if exclude_from_spending is None:
        new_exclusion = bool((category_exclusions or {}).get(category, False))
    else:
        new_exclusion = bool(exclude_from_spending)
    rules.append(
        MerchantRule(
            merchant_norm=merchant_norm,
            category=category,
            patterns=[pattern],
            exclude_from_spending=new_exclusion,
        )
    )
    return MerchantRulesConfig(version=config.version, rules=rules)


def _resolved_exclusion(
    *,
    stored: bool,
    requested: bool | None,
    merchant_norm: str,
    allow_spend_semantics_change: bool,
) -> bool:
    """Resolve a stored flag against a requested one, refusing an unauthorized change."""
    if requested is None or bool(requested) == stored:
        return stored
    if not allow_spend_semantics_change:
        raise SpendSemanticsChangeRefused(
            f"Refusing to change what counts as spending for merchant {merchant_norm!r}: "
            f"its rule stores exclude_from_spending={stored}, and this call asks for "
            f"{bool(requested)}. That flag decides whether every transaction this rule "
            "matches is inside or outside the spend universe your reports are computed "
            "over, so it is never changed as a side effect of teaching a category. Pass "
            "allow_spend_semantics_change=True to make the change deliberately."
        )
    return bool(requested)


class CategorizationService:
    """Apply category corrections — one-off, or remembered as a merchant rule."""

    def __init__(self, db_path: Path, runs_dir: Path | None = None):
        self.db_path = db_path
        # New runs are discoverable from the database path through durable coordination.
        # The sibling/default value remains a compatibility scan for older runs; passing
        # it explicitly keeps focused tests and legacy layouts deterministic.
        self.runs_dir = (
            Path(runs_dir) if runs_dir is not None else Path(db_path).parent / "import-runs"
        )

    def _refuse_if_an_import_run_is_open(self, merchant_norm: str) -> None:
        """Refuse to teach this database while a run can still promote into it (D23)."""
        refuse_live_mutation_during_open_import(
            self.db_path,
            operation=f"teach {merchant_norm!r}",
            runs_dir=self.runs_dir,
        )

    def suggest_for_transaction(self, txn_id: str) -> RuleSuggestion | None:
        with session_scope(self.db_path) as session:
            row = session.get(TransactionDB, txn_id)
            if row is None:
                return None
            return derive_rule_suggestion(
                str(row.merchant or ""),
                str(row.merchant_raw or ""),
                str(row.description or ""),
                str(row.category or "Other"),
            )

    def preview_rule_update(
        self,
        pattern: str,
        *,
        merchant_norm: str = "",
        category: str = "Other",
    ) -> RuleUpdatePreview:
        """Evaluate one taught rule inside the updated full ruleset.

        The target rule is upserted exactly as save would upsert it, including append-last
        precedence for a new merchant and D20 spend semantics. The count includes only
        non-MANUAL rows the target rule itself would change; unrelated repairs made by the
        same full rules pass are deliberately outside the number.
        """
        probe_norm = merchant_norm.strip().upper() or "_preview"
        repo = SQLiteConfigRepository(self.db_path, prepare_schema=False)
        categories = repo.load_taxonomy()
        category_exclusions = {
            rule.name: bool(rule.exclude_from_spending) for rule in categories.categories
        }
        updated = upsert_merchant_rule(
            repo.load_merchant_rules(),
            probe_norm,
            pattern,
            category,
            category_exclusions=category_exclusions,
        )
        return self._preview_updated_rule(
            updated,
            categories,
            merchant_norm=probe_norm,
            category=category,
        )

    def preview_match_count(
        self,
        pattern: str,
        *,
        merchant_norm: str = "",
        category: str = "Other",
    ) -> int:
        """How many transactions saving this rule would let a rules pass rewrite.

        Raises ValueError on a bad regex.

        This is the confirm step's safety valve, so it runs the APPLIER's semantics, not
        an approximation of them (D22):

        * the raw texts (``description``, ``merchant_raw``) are matched by PATTERN, and
          the resolved display name goes in as ``display_name=`` — matched by IDENTITY
          against ``merchant_norm``. Passing it POSITIONALLY as a third raw text (what
          this did before) pattern-matched a label that neither real path pattern-matches;
        * MANUAL rows are skipped, because ``RuleApplicationService.apply_all`` skips
          them permanently — counting them promised changes that can never happen.

        The candidate is evaluated in the ACTUAL UPDATED FULL RULESET, so existing
        first-match precedence is preserved. The returned count is target-rule-specific:
        an unrelated row repaired by the whole rules pass does not inflate it.
        """
        return self.preview_rule_update(
            pattern,
            merchant_norm=merchant_norm,
            category=category,
        ).transactions_updated

    def _preview_updated_rule(
        self,
        merchants: MerchantRulesConfig,
        categories: CategoriesConfig,
        *,
        merchant_norm: str,
        category: str,
    ) -> RuleUpdatePreview:
        key = merchant_norm.strip().upper()
        category_exclusions = {
            rule.name: bool(rule.exclude_from_spending) for rule in categories.categories
        }
        target_rule = next(
            rule for rule in merchants.rules if rule.merchant_norm.strip().upper() == key
        )
        stored_exclusion = bool(target_rule.exclude_from_spending)
        effective_exclusion = bool(
            target_rule.exclude_from_spending or category_exclusions.get(category, False)
        )
        count = 0
        with session_scope(self.db_path) as session:
            for row in session.query(TransactionDB).all():
                if Confidence.MANUAL in (row.category_confidence, row.merchant_confidence):
                    continue
                merchant, matched_category, merchant_exclusion = merchants.find_merchant(
                    *merchant_match_texts(
                        str(row.merchant_raw or ""),
                        str(row.description or ""),
                    ),
                    display_name=str(row.merchant or ""),
                )
                if merchant.strip().upper() != key:
                    continue
                exclude = bool(
                    merchant_exclusion or category_exclusions.get(matched_category, False)
                )
                if (
                    row.merchant != merchant
                    or row.category != matched_category
                    or bool(row.exclude_from_spending) != exclude
                    or row.merchant_confidence != Confidence.RULE
                    or row.category_confidence != Confidence.RULE
                ):
                    count += 1
        return RuleUpdatePreview(
            transactions_updated=count,
            stored_merchant_exclude_from_spending=stored_exclusion,
            effective_exclude_from_spending=effective_exclusion,
        )

    def remember_category(
        self,
        *,
        merchant_norm: str,
        pattern: str,
        category: str,
        exclude_from_spending: bool | None = None,
        allow_spend_semantics_change: bool = False,
        source: str = "streamlit_recategorize",
        refresh_analytics: bool = True,
    ) -> RuleApplicationResult:
        """Upsert the merchant rule, persist it, and re-apply across all transactions.

        ``exclude_from_spending`` is tri-state — see :func:`upsert_merchant_rule`. The
        default ``None`` preserves an existing rule's stored flag and derives a new
        rule's flag from the target category, so teaching a CATEGORY never silently
        changes what counts as spending.

        Refused outright while an import run can still promote into this database (D23):
        see :data:`OPEN_IMPORT_RUN_REMEDY`.
        """
        self._refuse_if_an_import_run_is_open(merchant_norm)
        repo = SQLiteConfigRepository(self.db_path)
        merchants = repo.load_merchant_rules()
        categories = repo.load_taxonomy()

        # Guard: reject parent categories before persisting anything.
        # A category is a "parent" when it is referenced as another category's parent field —
        # exactly the condition that repo.validate() rejects with "must not list merchants."
        if category in categories.referenced_parent_names():
            issue = (
                f"Parent category {category!r} cannot be used as a merchant rule target. "
                "Choose a leaf (non-parent) category instead."
            )
            return RuleApplicationResult(
                status="invalid_target",
                source=source,
                transactions_scanned=0,
                transactions_updated=0,
                changed_merchants=0,
                changed_categories=0,
                analytics_refreshed=False,
                validation_issues=[issue],
                details={"validation_issues": [issue]},
            )

        updated = upsert_merchant_rule(
            merchants,
            merchant_norm,
            pattern,
            category,
            exclude_from_spending,
            # A NEW rule's flag comes from the target category. Existing rules still
            # preserve their stored flag under D20, even if the category changes.
            category_exclusions={
                rule.name: bool(rule.exclude_from_spending) for rule in categories.categories
            },
            allow_spend_semantics_change=allow_spend_semantics_change,
        )
        preview = self._preview_updated_rule(
            updated,
            categories,
            merchant_norm=merchant_norm,
            category=category,
        )
        result = RuntimeConfigMutationService(self.db_path).replace_and_apply(
            updated,
            categories,
            source=source,
            refresh_analytics=refresh_analytics,
        )
        return replace(
            result,
            details={
                **result.details,
                "taught_transactions_updated": preview.transactions_updated,
                "taught_stored_merchant_exclude_from_spending": (
                    preview.stored_merchant_exclude_from_spending
                ),
                "taught_effective_exclude_from_spending": (preview.effective_exclude_from_spending),
            },
        )

    def set_transaction_category(self, txn_id: str, category: str) -> bool:
        """One-off: set just this transaction's category (no rule). Marked 'manual' so the
        override is durable — it survives both future rules-apply passes and re-imports of the
        same source file. The only way to clear it is to set a new category explicitly."""
        with live_mutation_boundary(
            self.db_path,
            operation="transaction_category",
            runs_dir=self.runs_dir,
        ):
            repo = SQLiteConfigRepository(self.db_path)
            categories = repo.load_categories()
            category_exclude = next(
                (c.exclude_from_spending for c in categories.categories if c.name == category),
                False,
            )
            merchants = repo.load_merchant_rules()
            started_at = utc_now()
            with session_scope(self.db_path) as session:
                row = session.get(TransactionDB, txn_id)
                if row is None:
                    return False
                # Same F16 semantics as rules apply and review resolve: the row's
                # matched merchant rule contributes its spend exclusion with OR.
                # Computing the flag from the target category alone silently
                # started counting an excluded merchant's row as spending — and
                # manual confidence then froze that answer against every later
                # rules pass.
                _, _, merchant_exclude = merchants.find_merchant(
                    *merchant_match_texts(
                        str(row.merchant_raw or ""),
                        str(row.description or ""),
                    ),
                    display_name=str(row.merchant or ""),
                )
                old_category = str(row.category or "")
                row.category = category
                row.category_confidence = Confidence.MANUAL
                row.exclude_from_spending = bool(merchant_exclude or category_exclude)

            try:
                record_db_operation(
                    self.db_path,
                    operation="category_override",
                    result=RunResult(
                        status="success",
                        started_at=started_at,
                        finished_at=utc_now(),
                        counts={"transactions_updated": 1},
                        details={
                            "txn_id": txn_id,
                            "old_category": old_category,
                            "new_category": category,
                        },
                    ),
                )
            except Exception as exc:
                logger.warning("audit write failed for category_override: %s", exc)
        return True
