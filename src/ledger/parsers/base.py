"""
Abstract base parser and parser protocol.

All institution-specific parsers inherit from BaseParser.
"""

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterator
from datetime import date
from pathlib import Path

from ledger.config import AppConfig
from ledger.config.schemas import merchant_match_texts
from ledger.core import ExceptionRecord, Transaction, TransactionBuilder
from ledger.utils import (
    clean_account_name,
    find_merchant_match,
    is_month_folder,
    naive_utc_now,
    normalize_whitespace,
    script_version,
)

logger = logging.getLogger(__name__)


class ParseContext:
    """
    Context passed to parsers containing shared state.

    Immutable reference to configuration and source metadata.
    """

    def __init__(
        self,
        config: AppConfig,
        source_root: Path,
        file_path: Path,
        include_raw: bool = False,
        account_slug: str | None = None,
    ):
        self.config = config
        self.include_raw = include_raw

        # Derived
        self.source_file = file_path.name
        self.source_path = str(file_path.relative_to(source_root))
        self.source_period = self._detect_period(file_path)
        self.account = clean_account_name(
            account_slug or file_path.stem,
            config.ledger.general.account_aliases,
        )
        self.imported_at = naive_utc_now()
        self.parser_version = script_version()

    def _detect_period(self, file_path: Path) -> str:
        """Detect YYYY-MM period from folder structure."""
        for parent in (file_path.parent, file_path.parent.parent):
            if is_month_folder(parent.name):
                return parent.name
        return ""


class BaseParser(ABC):
    """
    Abstract base class for institution-specific parsers.

    Subclasses must implement:
    - FORMAT_NAME: str identifier
    - REQUIRED_HEADERS: set of required column names
    - INSTITUTION: institution code
    - parse_row(): parse a single CSV row
    """

    FORMAT_NAME: str = ""
    REQUIRED_HEADERS: set[str] = set()
    INSTITUTION: str = ""

    def __init__(self, ctx: ParseContext):
        self.ctx = ctx
        self.config = ctx.config

    @classmethod
    def can_parse(cls, headers: set[str]) -> bool:
        """Check if this parser can handle the given headers."""
        return cls.REQUIRED_HEADERS.issubset(headers)

    def parse(self, rows: Iterator[dict]) -> Iterator[Transaction | ExceptionRecord]:
        """
        Parse all rows from a CSV file.

        Yields Transaction objects for successful parses,
        ExceptionRecord for failures.
        """
        for row_num, row in enumerate(rows, start=2):  # Start at 2 (1=header)
            try:
                txn = self.parse_row(row, row_num)
                if txn:
                    yield txn
            except Exception as e:
                # Full detail (which may embed the raw amount) stays at DEBUG for
                # local debugging; the persisted reason is redacted (see _exception).
                logger.debug("row_parse_failed at source row %d", row_num)
                yield self._exception(row_num, str(e), row)

    @abstractmethod
    def parse_row(self, row: dict, row_num: int) -> Transaction | None:
        """
        Parse a single CSV row into a Transaction.

        Return None to skip the row.
        Raise exception to generate ExceptionRecord.
        """
        pass

    def _exception(self, row_num: int, reason: str, raw: dict) -> ExceptionRecord:
        """Create an exception record.

        The reason is redacted of money amounts: an unexpected exception (e.g. a
        pydantic ValidationError from the model validator) can embed the literal
        amount in its message, and ``reason`` is written to ``exceptions_*.csv``
        unconditionally — even when the caller did not pass ``include_raw``.
        """
        return ExceptionRecord(
            source_file=self.ctx.source_file,
            source_path=self.ctx.source_path,
            source_row=row_num,
            reason="row_parse_failed",
            raw_data=raw if self.ctx.include_raw else {},
        )

    def _builder(self, row_num: int) -> TransactionBuilder:
        """Create a pre-populated transaction builder."""
        return TransactionBuilder().set(
            institution=self.INSTITUTION,
            account=self.ctx.account,
            source_file=self.ctx.source_file,
            source_path=self.ctx.source_path,
            source_period=self.ctx.source_period,
            source_row=row_num,
            imported_at=self.ctx.imported_at,
            parser_version=self.ctx.parser_version,
        )

    def _enrich(
        self,
        builder: TransactionBuilder,
        merchant_raw: str,
        category_raw: str,
        description: str,
    ) -> TransactionBuilder:
        """
        Enrich transaction with normalized merchant and category.

        Tries merchant rules first, then fuzzy matching fallback, then raw merchant.
        """
        # Try merchant rules
        merchant_norm = ""
        category = self.config.default_category
        merchant_conf = "none"
        category_conf = "low"
        exclude = False

        # Step 1: Check merchant rules (exact patterns)
        found_merchant, found_cat, found_excl = self.config.merchants.find_merchant(
            *merchant_match_texts(merchant_raw, description)
        )
        if found_merchant:
            merchant_norm = found_merchant.upper()
            merchant_conf = "high"
            # Honour the merchant rule's exclude_from_spending flag. The
            # category-config lookup below may also set it; OR the two so
            # either source can exclude the row (the rule flag was
            # previously discarded, making it dead config).
            exclude = exclude or found_excl
            # Look up category from categories config
            cat_lookup = self.config.merchant_to_category.get(merchant_norm.upper())
            if cat_lookup:
                category, cat_exclude = cat_lookup
                exclude = exclude or cat_exclude
                category_conf = "high"
            else:
                category = found_cat  # Use category from merchant rule
                # The category may still declare exclude_from_spending even
                # though the merchant isn't listed under it in the map; OR in
                # its flag so import matches rules-apply (rules/service.py:103).
                exclude = exclude or self.config.category_exclusions.get(category, False)
                category_conf = "medium"

        # Step 2: Try fuzzy matching if enabled and no exact match found
        if not merchant_norm and merchant_raw and self.config.ledger.fuzzy.enabled:
            # Get all known merchant names from categories config
            known_merchants = list(self.config.merchant_to_category.keys())

            if known_merchants:
                fuzzy_match = find_merchant_match(
                    normalize_whitespace(merchant_raw),
                    known_merchants,
                    threshold=self.config.ledger.fuzzy.threshold,
                    min_length=self.config.ledger.fuzzy.min_merchant_length,
                )

                if fuzzy_match:
                    merchant_norm = fuzzy_match.upper()
                    merchant_conf = "medium"  # Lower confidence than exact match
                    # Look up category from categories config
                    cat_lookup = self.config.merchant_to_category.get(merchant_norm.upper())
                    if cat_lookup:
                        category, exclude = cat_lookup
                        category_conf = "high"

        # Step 3: Fallback - use raw merchant name
        if not merchant_norm and merchant_raw:
            merchant_norm = normalize_whitespace(merchant_raw).upper()
            merchant_conf = "low"
            # Try category lookup
            cat_lookup = self.config.merchant_to_category.get(merchant_norm.upper())
            if cat_lookup:
                category, exclude = cat_lookup
                category_conf = "medium"

        return builder.set(
            merchant=merchant_norm,
            category=category,
            merchant_confidence=merchant_conf,
            category_confidence=category_conf,
            exclude_from_spending=exclude,
            merchant_raw=merchant_raw,
            category_raw=category_raw,
        )

    def _compute_temporal_features(self, posting_date: date) -> dict:
        """
        Compute temporal features from a date.

        Returns dict with temporal feature fields for TransactionBuilder.
        """
        day_of_week = posting_date.weekday()  # 0=Monday, 6=Sunday
        day_of_month = posting_date.day
        # Use isocalendar()[1] for ISO week number (1-53)
        week_of_year = posting_date.isocalendar()[1]
        is_weekend = day_of_week >= 5  # Saturday=5, Sunday=6
        is_month_start = day_of_month <= 3
        is_month_end = day_of_month >= 28

        return {
            "day_of_week": day_of_week,
            "day_of_month": day_of_month,
            "week_of_year": week_of_year,
            "is_weekend": is_weekend,
            "is_month_start": is_month_start,
            "is_month_end": is_month_end,
            # days_since_payday used to be seeded here as None, deferring to an
            # "analysis pipeline" that was never written. Dropped with the column in
            # migration 20260810_0023 rather than left as a promise nothing keeps.
            # Analysis fields initialized to defaults
            "recurring_pattern_id": None,
        }
