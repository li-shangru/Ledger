"""Exact fixed-point persistence for source-authored investment numerics."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

FIXED_POINT_PRECISION = 28
FIXED_POINT_SCALE = 12
_FIXED_POINT_LIMIT = Decimal("10000000000000000")

INVESTMENT_EXACT_DECIMAL_COLUMNS: dict[str, tuple[str, ...]] = {
    "investment_statements": ("total_nav", "time_weighted_return"),
    "investment_asset_balances": (
        "prior_total",
        "current_long",
        "current_short",
        "current_total",
        "change",
    ),
    "investment_cash_balances": (
        "opening_balance",
        "debits",
        "credits",
        "closing_balance",
        "opening_fx_rate",
        "closing_fx_rate",
    ),
    "investment_positions": (
        "quantity",
        "multiplier",
        "cost_price",
        "cost_basis",
        "close_price",
        "market_value",
        "unrealized_pl",
    ),
    "investment_performance": (
        "cost_adjustment",
        "realized_short_profit",
        "realized_short_loss",
        "realized_long_profit",
        "realized_long_loss",
        "realized_total",
        "unrealized_short_profit",
        "unrealized_short_loss",
        "unrealized_long_profit",
        "unrealized_long_loss",
        "unrealized_total",
        "total",
    ),
    "investment_activities": (
        "quantity",
        "trade_price",
        "close_price",
        "amount",
        "commission_fee",
        "basis",
        "realized_pl",
        "mtm_pl",
    ),
}


def canonical_fixed_point_text(value: Decimal) -> str:
    """Validate and serialize one Decimal without rounding or exponent notation."""
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("exact fixed-point value must be a finite Decimal")
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int):
        raise ValueError("exact fixed-point value has an invalid exponent")
    if exponent < -FIXED_POINT_SCALE:
        raise ValueError("exact fixed-point value exceeds scale 12")
    if value.copy_abs() >= _FIXED_POINT_LIMIT:
        raise ValueError("exact fixed-point value exceeds precision 28 and scale 12")
    canonical_value = value.copy_abs() if value.is_zero() else value
    return format(canonical_value, "f")


def decimal_from_canonical_text(value: object) -> Decimal:
    """Load canonical persisted text and reject coerced or malformed storage."""
    if not isinstance(value, str):
        raise ValueError("exact fixed-point storage must contain canonical text")
    try:
        decimal_value = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("exact fixed-point storage contains an invalid value") from exc
    if canonical_fixed_point_text(decimal_value) != value:
        raise ValueError("exact fixed-point storage is not canonical")
    return decimal_value


class ExactFixedPoint(TypeDecorator[Decimal]):
    """SQLAlchemy Decimal type backed by SQLite TEXT rather than numeric affinity."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Decimal | None, dialect: Any) -> str | None:
        del dialect
        if value is None:
            return None
        return canonical_fixed_point_text(value)

    def process_result_value(self, value: object | None, dialect: Any) -> Decimal | None:
        del dialect
        if value is None:
            return None
        return decimal_from_canonical_text(value)
