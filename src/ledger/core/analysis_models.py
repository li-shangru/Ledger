"""
Analysis-specific data models for ML and insights.

All models are immutable (frozen=True) and use Decimal for monetary values.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Account(BaseModel):
    """
    Auto-detected account from transactions.

    Aggregates transaction data to understand account lifecycle and statistics.
    """

    model_config = ConfigDict(frozen=True)

    # === Identity ===
    account_id: str = Field(description="Unique account identifier (hash of institution + account)")
    institution: str = Field(description="Financial institution code")
    account_number: str = Field(description="Account number/identifier")
    account_type: str = Field(description="Account type (checking, credit_card, savings)")
    account_subtype: str = Field(
        description="Account subtype (e.g., rewards credit card, high interest savings)"
    )

    # === Lifecycle ===
    first_transaction_date: date = Field(description="Date of first transaction")
    last_transaction_date: date = Field(description="Date of most recent transaction")
    is_active: bool = Field(description="True if transaction within last 60 days")

    # === Statistics ===
    transaction_count: int = Field(description="Total number of transactions")
    avg_monthly_transactions: Decimal = Field(description="Average transactions per month")

    # === Credit card specifics ===
    is_credit_card: bool = Field(description="True if this is a credit card account")
    detected_credit_limit: Decimal | None = Field(
        default=None, description="Detected credit limit (highest balance seen)"
    )

    # === Metadata ===
    created_at: datetime = Field(description="When account was first detected")
    updated_at: datetime = Field(description="When account stats were last updated")


class AccountBalance(BaseModel):
    """
    Daily account balance snapshot.

    Computed from transaction history to enable balance tracking and reconciliation.
    """

    model_config = ConfigDict(frozen=True)

    # === Identity ===
    account_id: str = Field(description="Account this balance belongs to")
    balance_date: date = Field(description="Date of this balance snapshot")

    # === Balance ===
    balance: Decimal = Field(description="Account balance at end of day")
    balance_basis: Literal["relative", "absolute"] = Field(
        default="relative",
        description=(
            "Whether balance is a relative change series or an absolute statement-backed value"
        ),
    )
    transaction_count: int = Field(description="Number of transactions on this day")

    # === Rolling statistics ===
    balance_7d_avg: Decimal = Field(description="7-day rolling average balance")
    balance_30d_avg: Decimal = Field(description="30-day rolling average balance")
    balance_7d_min: Decimal = Field(description="Minimum balance in last 7 days")
    balance_7d_max: Decimal = Field(description="Maximum balance in last 7 days")

    # === Credit card specific ===
    credit_utilization: Decimal | None = Field(
        default=None, description="Credit utilization ratio (balance / limit)"
    )

    # === Metadata ===
    computed_at: datetime = Field(description="When this balance was computed")


class RecurringPattern(BaseModel):
    """
    ML-detected recurring transaction pattern.

    Identifies subscriptions, bills, income, and other recurring transactions.
    """

    model_config = ConfigDict(frozen=True)

    # === Identity ===
    pattern_id: str = Field(description="Unique pattern identifier")
    pattern_type: str = Field(description="Pattern type (subscription, bill, income, transfer)")

    # === Pattern details ===
    merchant: str = Field(description="Merchant name for this pattern")
    category: str = Field(description="Transaction category")
    institution: str = Field(description="Financial institution")
    account_id: str = Field(description="Account where pattern occurs")

    # === Frequency ===
    frequency: str = Field(description="Frequency (monthly, weekly, biweekly, quarterly, annual)")
    expected_day_of_period: int = Field(description="Expected day of period (e.g., 15th of month)")

    # === Amount pattern ===
    is_fixed_amount: bool = Field(description="True if amount is consistent")
    expected_amount: Decimal = Field(description="Expected transaction amount")
    amount_std_dev: Decimal = Field(description="Standard deviation of amount")

    # === Detection metadata ===
    confidence: float = Field(description="Detection confidence (0.0-1.0)")
    detection_method: str = Field(description="Detection method (ml_clustering, rule_based)")

    # === Statistics ===
    occurrence_count: int = Field(description="Number of times pattern has occurred")
    first_occurrence: date = Field(description="Date of first occurrence")
    last_occurrence: date = Field(description="Date of most recent occurrence")
    next_expected_date: date = Field(description="Predicted next occurrence date")

    # === Health ===
    is_active: bool = Field(description="False if missed 2+ expected occurrences")
    missed_count: int = Field(description="Number of missed occurrences")

    # === Metadata ===
    detected_at: datetime = Field(description="When pattern was first detected")
    updated_at: datetime = Field(description="When pattern was last updated")

    # === Source transactions ===
    # Pipeline-internal field: the IDs of the transactions that the detector
    # consumed to build this pattern during a single detection run.  save_patterns
    # uses this list to stamp those rows with the new pattern_id immediately after
    # inserting the pattern, suppressing a double-count by match_all_transactions
    # (which skips rows whose recurring_pattern_id is already set).
    # This is NOT observable pattern state — it is meaningless outside of the
    # detect → save_patterns → match_all_transactions pipeline and must not be
    # exposed to callers or serialised as a pattern attribute.
    source_txn_ids: tuple[str, ...] = Field(
        default=(),
        description=(
            "PIPELINE-INTERNAL: transaction IDs consumed by the detector in this run. "
            "save_patterns stamps these rows with the new pattern_id so "
            "match_all_transactions skips them and does not double-count occurrences. "
            "Not observable pattern state — meaningless outside the "
            "detect → save_patterns → match pipeline."
        ),
    )


class SpendingTrend(BaseModel):
    """
    Pre-aggregated spending trend.

    Computed periodically for fast dashboard queries.
    """

    model_config = ConfigDict(frozen=True)

    # === Dimensions ===
    time_period: str = Field(description="Time period (2025-01-15, 2025-W03, 2025-01)")
    period_type: str = Field(description="Period type (daily, weekly, monthly, quarterly)")
    category: str = Field(description="Spending category")

    # === Metrics ===
    transaction_count: int = Field(description="Number of transactions")
    total_spent: Decimal = Field(description="Total amount spent")
    avg_transaction: Decimal = Field(description="Average transaction amount")
    median_transaction: Decimal = Field(description="Median transaction amount")

    # === Comparisons ===
    vs_previous_period_pct: Decimal = Field(description="% change vs previous period")
    vs_previous_period_abs: Decimal = Field(description="Absolute change vs previous period")
    vs_avg_6m_pct: Decimal = Field(description="% change vs 6-month average")

    # === Top merchant ===
    top_merchant: str | None = Field(default=None, description="Top merchant in this period")
    top_merchant_amount: Decimal | None = Field(default=None, description="Top merchant spending")

    # === Trend ===
    trend_direction: str = Field(description="Trend direction (increasing, decreasing, stable)")

    # === Metadata ===
    computed_at: datetime = Field(description="When trend was computed")


class FinancialHealthScore(BaseModel):
    """
    Daily financial health score.

    Auto-computed metric combining cashflow, spending consistency, and savings rate.
    """

    model_config = ConfigDict(frozen=True)

    # === Identity ===
    score_date: date = Field(description="Date of this score")

    # === Overall score ===
    overall_score: int = Field(description="Overall health score (0-100)")

    # === Component scores ===
    cashflow_health: int = Field(description="Cashflow health score (0-100)")
    spending_consistency: int = Field(description="Spending consistency score (0-100)")
    savings_rate: Decimal = Field(description="Savings rate as percentage of income")
    # None = not assessable: these require an explicit ABSOLUTE statement/opening
    # balance and cannot be derived from calculator-produced relative rows.
    emergency_fund_months: Decimal | None = Field(
        default=None, description="Months of expenses in liquid accounts (None if not assessable)"
    )
    debt_ratio: Decimal | None = Field(
        default=None, description="Debt to assets ratio (None if not assessable)"
    )

    # === Trends ===
    score_trend_7d: str = Field(description="7-day trend (improving, declining, stable)")
    score_change_7d: int = Field(description="Change in overall score over 7 days")

    # === Risks ===
    risk_factors: list[str] = Field(default_factory=list, description="Detected risk factors")

    # === Metadata ===
    computed_at: datetime = Field(description="When score was computed")
