"""
Core enumerations for the ledger system.

Using StrEnum for JSON/Parquet serialization compatibility.
"""

from enum import StrEnum


class FlowType(StrEnum):
    """Transaction flow classification."""

    EXPENSE = "expense"
    INCOME = "income"
    TRANSFER = "transfer"
    PAYMENT = "payment"  # Credit card payment
    PURCHASE = "purchase"  # Credit card purchase
    REFUND = "refund"
    FEE = "fee"
    INTEREST = "interest"
    REWARD = "reward"
    UNKNOWN = "unknown"


class CashflowType(StrEnum):
    """Cashflow impact classification."""

    INCOME = "income"
    EXPENSE = "expense"
    TRANSFER = "transfer"
    NEUTRAL = "neutral"


class Confidence(StrEnum):
    """Classification confidence level."""

    HIGH = "high"  # Matched explicit rule
    MEDIUM = "medium"  # Derived from partial match
    LOW = "low"  # Fallback/default
    NONE = "none"  # No match possible
    MANUAL = "manual"  # User-supplied override (durable; survives re-import and rules passes)
    RULE = "rule"  # Applied by a runtime rule-application pass


class Institution(StrEnum):
    """Legacy unknown marker; institution identities are extensible private keys."""

    UNKNOWN = "unknown"

    @classmethod
    def from_format(cls, format_name: str) -> str:
        from ledger.adapters.registry import configured_metadata

        matches = {
            entry["institution"]
            for metadata in configured_metadata()
            for entry in metadata.get("csv_formats", [])
            if entry["format_id"] == format_name
        }
        return str(next(iter(matches))) if len(matches) == 1 else cls.UNKNOWN


class Currency(StrEnum):
    """Supported currencies."""

    CAD = "CAD"
    USD = "USD"
