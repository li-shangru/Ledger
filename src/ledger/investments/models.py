"""Immutable parsed investment-statement domain models."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict


class InvestmentActivityType(StrEnum):
    """The statement activity categories represented by investment imports."""

    TRADE = "trade"
    DIVIDEND = "dividend"
    DEPOSIT = "deposit"
    WITHDRAWAL = "withdrawal"
    TRANSFER = "transfer"
    FEE = "fee"
    INTEREST = "interest"
    WITHHOLDING_TAX = "withholding_tax"
    STOCK_LOAN = "stock_loan"
    STOCK_RECALL = "stock_recall"


class InvestmentAccount(BaseModel):
    """Investment account identity from a source statement."""

    model_config = ConfigDict(frozen=True)

    account_id: str
    institution: str
    source_account_id: str
    account_alias: str
    account_type: str
    base_currency: str
    status: Literal["active", "closed", "unknown"]
    transfer_destination: str | None = None
    status_confirmed_on: date | None = None
    closed_on: date | None = None


class InvestmentAssetBalance(BaseModel):
    """Statement-level balance summary for one asset class."""

    model_config = ConfigDict(frozen=True)

    asset_class: str
    prior_total: Decimal | None = None
    current_long: Decimal | None = None
    current_short: Decimal | None = None
    current_total: Decimal
    change: Decimal | None = None


class InvestmentCashBalance(BaseModel):
    """Native-currency cash and any source-printed rates to statement currency."""

    model_config = ConfigDict(frozen=True)

    currency: str
    opening_balance: Decimal
    debits: Decimal
    credits: Decimal
    closing_balance: Decimal
    opening_fx_rate: Decimal | None = None
    closing_fx_rate: Decimal | None = None


class InvestmentPosition(BaseModel):
    """One position snapshot row exactly as reported by the source statement."""

    model_config = ConfigDict(frozen=True)

    asset_category: str
    currency: str
    symbol: str
    discriminator: str
    quantity: Decimal
    multiplier: Decimal | None = None
    cost_price: Decimal | None = None
    cost_basis: Decimal | None = None
    close_price: Decimal | None = None
    market_value: Decimal | None = None
    unrealized_pl: Decimal | None = None
    source_row: int


class InvestmentPerformance(BaseModel):
    """One statement-reported realized/unrealized performance row."""

    model_config = ConfigDict(frozen=True)

    asset_category: str
    symbol: str
    cost_adjustment: Decimal | None = None
    realized_short_profit: Decimal | None = None
    realized_short_loss: Decimal | None = None
    realized_long_profit: Decimal | None = None
    realized_long_loss: Decimal | None = None
    realized_total: Decimal | None = None
    unrealized_short_profit: Decimal | None = None
    unrealized_short_loss: Decimal | None = None
    unrealized_long_profit: Decimal | None = None
    unrealized_long_loss: Decimal | None = None
    unrealized_total: Decimal | None = None
    total: Decimal | None = None
    code: str = ""
    source_row: int


class InvestmentActivity(BaseModel):
    """One source-statement investment activity row."""

    model_config = ConfigDict(frozen=True)

    activity_id: str
    activity_type: InvestmentActivityType
    activity_date: date
    activity_time: datetime | None = None
    symbol: str | None = None
    currency: str
    quantity: Decimal | None = None
    trade_price: Decimal | None = None
    close_price: Decimal | None = None
    amount: Decimal | None = None
    commission_fee: Decimal | None = None
    basis: Decimal | None = None
    realized_pl: Decimal | None = None
    mtm_pl: Decimal | None = None
    description: str = ""
    direction: str | None = None
    source_row: int


class InvestmentStatement(BaseModel):
    """Complete parsed investment statement, kept outside cashflow aggregates."""

    model_config = ConfigDict(frozen=True)

    statement_id: str
    account: InvestmentAccount
    period_start: date
    period_end: date
    generated_at: datetime | None = None
    base_currency: str
    total_nav: Decimal
    time_weighted_return: Decimal | None = None
    asset_balances: tuple[InvestmentAssetBalance, ...]
    cash_balances: tuple[InvestmentCashBalance, ...] = ()
    positions: tuple[InvestmentPosition, ...]
    performance: tuple[InvestmentPerformance, ...] = ()
    activities: tuple[InvestmentActivity, ...]
    parsed_fingerprint: str
