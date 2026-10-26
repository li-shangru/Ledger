"""Version 1 parse-only interface for explicitly trusted local import packages.

An adapter receives bytes/text and bounded metadata, never a database or publication
handle. Python extensions still run with the process's permissions: this interface
is a contract, not an operating-system sandbox.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from ledger.investments.models import InvestmentStatement
    from ledger.parsers.base import BaseParser

API_VERSION = 1
ENTRY_POINT_GROUP = "ledger.import_adapters.v1"
INVESTMENT_ASSET_CATEGORY_PATTERN = r"[A-Za-z][A-Za-z0-9 &'()\-]*\Z"
MAX_INVESTMENT_ASSET_CATEGORY_LENGTH = 80
MAX_INVESTMENT_ASSET_CATEGORIES = 64


class AdapterError(ValueError):
    """A bounded, core-owned diagnostic that contains no extension details."""

    def __init__(self, code: str = "adapter_failed") -> None:
        allowed = {
            "adapter_failed",
            "adapter_not_enabled",
            "adapter_unavailable",
            "adapter_incompatible",
            "adapter_artifact_changed",
            "adapter_duplicate",
            "adapter_ambiguous",
            "adapter_unknown_format",
            "adapter_invalid_contract",
            "adapter_unsafe_configuration",
            "adapter_unsupported_capability",
            "import_binding_missing",
            "import_binding_changed",
            "import_binding_invalid",
            "import_account_conflict",
        }
        self.code = code if code in allowed else "adapter_failed"
        super().__init__(self.code)


class InvestmentCapability(StrEnum):
    COMPLETE_STATEMENT = "complete_statement"
    CASH_ACTIVITY = "cash_activity"
    TRADES = "trades"
    DIVIDENDS_INTEREST = "dividends_interest"
    FEES_TAXES = "fees_taxes"
    POSITIONS = "position_snapshots"
    BALANCES = "statement_balances"
    PERFORMANCE = "reported_performance"
    SECURITIES_LENDING = "securities_lending"


@dataclass(frozen=True)
class BalanceSpec:
    opening_key: str
    closing_key: str
    date_columns: tuple[str, ...]
    amount_column: str
    printed_sign: Literal[-1, 1]
    row_sign: Literal[-1, 1]
    movement_backed: bool = False


@dataclass(frozen=True)
class PdfFacts:
    """Parsed rows and source-supported evidence, before any artifact is written."""

    rows: tuple[dict[str, str], ...]
    period: date
    account_type: str
    account_number: str
    account_slug: str
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    summary: dict[str, str] = field(default_factory=dict)
    printed_interval: tuple[date, date] | None = None
    complete: bool = False
    credit_limit: Decimal | None = None


@dataclass(frozen=True)
class CsvFormat:
    format_id: str
    institution: str
    parser: type[BaseParser]


@dataclass(frozen=True)
class PdfFormat:
    format_id: str
    institution: str
    headers: tuple[str, ...]
    recognizes: Callable[[str], bool]
    parse: Callable[[str, str], PdfFacts]
    account_slug: Callable[[str, str], str]
    balance_spec: BalanceSpec | None = None
    archive_kind: str = "statements"


@dataclass(frozen=True)
class InvestmentFormat:
    """Complete statements only; partial exports cannot fill absent facts with zero."""

    format_id: str
    institution: str
    source_layout: Literal["account_folders", "flat"]
    capabilities: frozenset[InvestmentCapability]
    recognizes_text: Callable[[str], bool]
    parse: Callable[[bytes], InvestmentStatement]
    normalize_alias: Callable[[str], str]
    period_from_text: Callable[[str], tuple[date, date] | None]
    # Paired evidence is additionally checked against the parsed account and period
    # by core; this callback recognizes the printed account identity in text only.
    matches_account: Callable[[str, str], bool]
    evidence_only_aliases: tuple[str, ...] = ()
    default: bool = False


@dataclass(frozen=True)
class CompatibilityPolicy:
    """Declarative historical interpretation; readable without loading Python code."""

    credit_institutions: tuple[str, ...] = ()
    payment_tokens: dict[str, tuple[str, ...]] = field(default_factory=dict)
    account_product_tokens: tuple[tuple[str, str], ...] = ()
    classification_rules: dict[str, dict[str, tuple[str, ...]]] = field(default_factory=dict)
    bill_tokens: tuple[str, ...] = ()
    merchant_prefix_patterns: tuple[str, ...] = ()
    account_aliases: dict[str, str] = field(default_factory=dict)
    credit_account_tokens: tuple[str, ...] = ()
    investment_asset_categories: tuple[str, ...] = ()
    reimbursement_merchants: tuple[str, ...] = ()


@dataclass(frozen=True)
class AdapterDefinition:
    adapter_id: str
    api_version: int
    layout_revision: str
    csv_formats: tuple[CsvFormat, ...] = ()
    pdf_formats: tuple[PdfFormat, ...] = ()
    investment_formats: tuple[InvestmentFormat, ...] = ()
    policy: CompatibilityPolicy = field(default_factory=CompatibilityPolicy)
