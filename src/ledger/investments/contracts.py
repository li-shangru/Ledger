"""Canonical investment facts shared by ingestion and public read boundaries.

Validation rejects unsupported facts without normalizing economic values, labels,
identities or fingerprints. Completeness of source extraction remains an adapter
obligation; capabilities declare the domains that its complete parser supports.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date

from ledger.adapters.contracts import AdapterError, InvestmentCapability
from ledger.adapters.registry import compatibility_policy_snapshot
from ledger.investments.models import InvestmentActivityType, InvestmentStatement
from ledger.storage.exact_decimal import (
    INVESTMENT_EXACT_DECIMAL_COLUMNS,
    canonical_fixed_point_text,
)

# Preserve source ticker spelling, class shares and compact option identifiers;
# spaces are limited to a class suffix or the structured option suffix.
SYMBOL_PATTERN = (
    r"^[A-Za-z0-9]{1,32}(?:[.\-:/][A-Za-z0-9]{1,10}){0,2}(?: [A-Za-z]{1,2}| [0-9]{6}[CP][0-9]{8})?$"
)
LABEL_PATTERN = r"^[A-Za-z][A-Za-z0-9 &'()\-]*$"
EXACT_DECIMAL_PATTERN = r"^-?(?:0|[1-9]\d*)(?:\.\d{1,12})?$"
ASSET_CLASSES = (
    "Cash",
    "Stocks",
    "Options",
    "Bonds",
    "Funds",
    "Mutual Funds",
    "ETFs",
    "Forex",
    "Futures",
    "Futures Options",
    "Warrants",
    "CFDs",
    "Other",
    "Total",
)
# NAV rows include balances that are not position categories. Preserve the
# supported source category spellings; do not
# relabel accruals as cash or widen the position vocabulary.
NAV_ASSET_CLASSES = (
    *ASSET_CLASSES,
    "Stock",
    "Cash Collateral",
    "Securities Lent",
    "Interest Accruals",
    "Dividend Accruals",
)


@dataclass(frozen=True)
class AssetCategoryVocabulary:
    """One immutable, validated category vocabulary for ingestion or a read session."""

    asset_classes: tuple[str, ...]
    nav_asset_classes: tuple[str, ...]

    @property
    def digest(self) -> str:
        value = (self.asset_classes, self.nav_asset_classes)
        return hashlib.sha256(json.dumps(value, separators=(",", ":")).encode()).hexdigest()


GENERIC_ASSET_VOCABULARY = AssetCategoryVocabulary(ASSET_CLASSES, NAV_ASSET_CLASSES)


def asset_category_vocabulary() -> AssetCategoryVocabulary:
    """Retain registered historical categories without loading extension code."""
    categories = compatibility_policy_snapshot().investment_asset_categories
    return AssetCategoryVocabulary(
        tuple(dict.fromkeys((*ASSET_CLASSES, *categories))),
        tuple(dict.fromkeys((*NAV_ASSET_CLASSES, *categories))),
    )


DIRECTIONS = (
    "buy",
    "sell",
    "loan",
    "recall",
    "in",
    "out",
    "deposit",
    "withdrawal",
    "BUY",
    "SELL",
    "IN",
    "OUT",
    "In",
    "Out",
    "Order",
    "Trade",
    "Summary",
    "ClosedLot",
)
DISCRIMINATORS = ("Summary", "Lot", "ClosedLot", "OpenLot")
POSITION_DECIMALS = (
    "quantity",
    "multiplier",
    "cost_price",
    "cost_basis",
    "close_price",
    "market_value",
    "unrealized_pl",
)
ACTIVITY_DECIMALS = (
    "quantity",
    "trade_price",
    "close_price",
    "amount",
    "commission_fee",
    "basis",
    "realized_pl",
    "mtm_pl",
)
ASSET_DECIMALS = ("prior_total", "current_long", "current_short", "current_total", "change")
CASH_DECIMALS = (
    "opening_balance",
    "debits",
    "credits",
    "closing_balance",
    "opening_fx_rate",
    "closing_fx_rate",
)
_REQUIRED_CAPABILITIES = frozenset(
    {
        InvestmentCapability.COMPLETE_STATEMENT,
        InvestmentCapability.POSITIONS,
        InvestmentCapability.BALANCES,
    }
)
_ACTIVITY_CAPABILITIES = {
    InvestmentActivityType.TRADE: InvestmentCapability.TRADES,
    InvestmentActivityType.DIVIDEND: InvestmentCapability.DIVIDENDS_INTEREST,
    InvestmentActivityType.INTEREST: InvestmentCapability.DIVIDENDS_INTEREST,
    InvestmentActivityType.DEPOSIT: InvestmentCapability.CASH_ACTIVITY,
    InvestmentActivityType.WITHDRAWAL: InvestmentCapability.CASH_ACTIVITY,
    InvestmentActivityType.TRANSFER: InvestmentCapability.CASH_ACTIVITY,
    InvestmentActivityType.FEE: InvestmentCapability.FEES_TAXES,
    InvestmentActivityType.WITHHOLDING_TAX: InvestmentCapability.FEES_TAXES,
    InvestmentActivityType.STOCK_LOAN: InvestmentCapability.SECURITIES_LENDING,
    InvestmentActivityType.STOCK_RECALL: InvestmentCapability.SECURITIES_LENDING,
}
_CASH_AMOUNT_REQUIRED = frozenset(
    {
        InvestmentActivityType.TRADE,
        InvestmentActivityType.DEPOSIT,
        InvestmentActivityType.WITHDRAWAL,
        InvestmentActivityType.DIVIDEND,
        InvestmentActivityType.INTEREST,
        InvestmentActivityType.FEE,
        InvestmentActivityType.WITHHOLDING_TAX,
    }
)


def _require(condition: bool) -> None:
    if not condition:
        raise AdapterError("adapter_invalid_contract")


def _text(value: object, maximum: int, pattern: str | None = None) -> None:
    _require(isinstance(value, str) and 0 < len(value) <= maximum)
    assert isinstance(value, str)
    value.encode("utf-8", errors="strict")
    if pattern is not None:
        _require(re.fullmatch(pattern, value) is not None)


def _currency(value: object) -> None:
    _text(value, 3, r"[A-Z]{3}")


def _source_row(value: object) -> None:
    _require(type(value) is int and 0 <= value <= 2_147_483_647)


def _decimals(row: object, names: tuple[str, ...], *, required: tuple[str, ...] = ()) -> None:
    for name in names:
        value = getattr(row, name)
        if value is None:
            _require(name not in required)
        else:
            canonical_fixed_point_text(value)


def validate_statement_contract(
    statement: InvestmentStatement, capabilities: frozenset[InvestmentCapability]
) -> None:
    """Reject facts that the canonical storage/read contract cannot represent."""
    try:
        _validate_statement_contract(statement, capabilities, asset_category_vocabulary())
    except AdapterError:
        raise
    except AttributeError, TypeError, ValueError, OverflowError:
        raise AdapterError("adapter_invalid_contract") from None


def _validate_statement_contract(
    statement: InvestmentStatement,
    capabilities: frozenset[InvestmentCapability],
    vocabulary: AssetCategoryVocabulary,
) -> None:
    required_capabilities = set(_REQUIRED_CAPABILITIES)
    _text(statement.statement_id, 128)
    _text(statement.parsed_fingerprint, 64, r"[0-9a-f]{64}")
    _text(statement.account.account_id, 128)
    _text(statement.account.source_account_id, 128)
    _text(statement.account.institution, 50, LABEL_PATTERN)
    _text(statement.account.account_type, 100, LABEL_PATTERN)
    _require(
        isinstance(statement.account.account_alias, str)
        and len(statement.account.account_alias) <= 200
    )
    _currency(statement.base_currency)
    _currency(statement.account.base_currency)
    _require(type(statement.period_start) is date and type(statement.period_end) is date)
    _require(statement.period_start <= statement.period_end)
    _decimals(statement, ("total_nav", "time_weighted_return"), required=("total_nav",))

    for row in statement.asset_balances:
        _require(row.asset_class in vocabulary.nav_asset_classes)
        _decimals(row, ASSET_DECIMALS, required=("current_total",))
    if statement.cash_balances:
        required_capabilities.add(InvestmentCapability.CASH_ACTIVITY)
    for cash in statement.cash_balances:
        _currency(cash.currency)
        _decimals(
            cash,
            CASH_DECIMALS,
            required=("opening_balance", "debits", "credits", "closing_balance"),
        )
        _require(cash.debits >= 0 and cash.credits >= 0)
        _require(cash.opening_fx_rate is None or cash.opening_fx_rate > 0)
        _require(cash.closing_fx_rate is None or cash.closing_fx_rate > 0)
    for position in statement.positions:
        _source_row(position.source_row)
        _require(position.asset_category in vocabulary.asset_classes)
        _require(position.discriminator in DISCRIMINATORS)
        _currency(position.currency)
        _text(position.symbol, 48, SYMBOL_PATTERN)
        _decimals(position, POSITION_DECIMALS, required=("quantity",))
    if statement.performance or statement.time_weighted_return is not None:
        required_capabilities.add(InvestmentCapability.PERFORMANCE)
    for performance in statement.performance:
        _source_row(performance.source_row)
        _decimals(performance, INVESTMENT_EXACT_DECIMAL_COLUMNS["investment_performance"])
    for activity in statement.activities:
        _require(activity.activity_type in _ACTIVITY_CAPABILITIES)
        required_capabilities.add(_ACTIVITY_CAPABILITIES[activity.activity_type])
        _text(activity.activity_id, 128)
        _source_row(activity.source_row)
        _require(type(activity.activity_date) is date)
        _currency(activity.currency)
        if activity.symbol is not None:
            _text(activity.symbol, 48, SYMBOL_PATTERN)
        _require(activity.direction is None or activity.direction in DIRECTIONS)
        _decimals(activity, ACTIVITY_DECIMALS)
        if activity.activity_type in _CASH_AMOUNT_REQUIRED:
            _require(activity.amount is not None)
        elif activity.activity_type == InvestmentActivityType.TRANSFER and activity.amount is None:
            _require(activity.symbol is not None and activity.quantity is not None)
    if not required_capabilities <= capabilities:
        raise AdapterError("adapter_unsupported_capability")
