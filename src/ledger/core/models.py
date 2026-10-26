"""
Core data models for the ledger system.

All models use Pydantic for validation and serialization.
Decimal is used for all monetary values to ensure precision.
"""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ledger.utils.dates import naive_utc_now
from ledger.utils.money import quantize_cents


class Transaction(BaseModel):
    """
    Immutable transaction record.

    This is the canonical representation of a financial transaction
    after parsing, normalization, and enrichment.
    """

    model_config = ConfigDict(frozen=True, str_strip_whitespace=True)

    # === Identity ===
    txn_id: str = Field(description="Unique transaction identifier (hash-based)")
    txn_fingerprint: str = Field(description="Content fingerprint for collision detection")

    # === Dates ===
    posting_date: date = Field(description="Date transaction was posted")
    transaction_date: date | None = Field(default=None, description="Original transaction date")

    # === Source ===
    institution: str = Field(description="Financial institution code")
    account: str = Field(description="Account identifier")
    account_type: str = Field(
        default="", description="Account type/subtype (e.g., 'Credit card', 'Chequing')"
    )

    # === Classification ===
    flow: str = Field(description="Transaction flow type")
    cashflow_type: str = Field(description="Cashflow impact type")
    include_in_cashflow: bool = Field(description="Include in cashflow calculations")

    # === Amounts ===
    currency: str = Field(default="CAD")
    amount_raw: Decimal = Field(description="Original amount from source")
    amount_signed: Decimal = Field(description="Standardized signed amount (negative=outflow)")

    # === Description ===
    description: str = Field(description="Transaction description")

    # === Enrichment ===
    merchant: str = Field(default="", description="Normalized merchant name")
    category: str = Field(default="Other", description="Spending category")
    merchant_confidence: str = Field(default="none")
    category_confidence: str = Field(default="low")
    exclude_from_spending: bool = Field(default=False, description="Exclude from spending metrics")

    # === Raw Data (for audit) ===
    merchant_raw: str = Field(default="", description="Original merchant from source")
    category_raw: str = Field(default="", description="Original category from source")

    # === Provenance ===
    source_file: str = Field(description="Source filename")
    source_path: str = Field(description="Relative path from data root")
    source_period: str = Field(default="", description="YYYY-MM period folder")
    source_row: int = Field(description="Row number in source file")

    # === Metadata ===
    imported_at: datetime = Field(description="Import timestamp")
    parser_version: str = Field(description="Parser version hash")

    # === Analysis Features ===
    # Recurring pattern detection
    recurring_pattern_id: str | None = Field(
        default=None, description="Linked recurring pattern ID"
    )

    # Transfer pairing (for matching transfers across accounts).
    #
    # These four are TRANSFER-ONLY and persist to `transfers`, never to
    # `transactions` -- migration 20260810_0023 dropped the never-populated twins
    # from TransactionDB. They stay on this model because Transaction is also the
    # in-memory representation of a transfer leg: `transfer_from_db` /
    # `minimal_transfer` build one per leg, TransferMatcher model_copy-updates
    # exactly these fields, and `_upsert_transfer` reads them back. Removing them
    # would silently break pairing, not just drop a dead field.
    paired_transfer_id: str | None = Field(
        default=None, description="ID of the matching transfer transaction"
    )
    transfer_confidence: float | None = Field(
        default=None, description="Confidence score for transfer match (0.0-1.0)"
    )
    counterparty_account: str | None = Field(
        default=None, description="Destination/source account for transfer"
    )
    counterparty_institution: str | None = Field(
        default=None, description="Destination/source institution for transfer"
    )

    # Temporal features (auto-computed during parsing)
    day_of_week: int = Field(description="Day of week (0=Monday, 6=Sunday)")
    day_of_month: int = Field(description="Day of month (1-31)")
    week_of_year: int = Field(description="ISO week of year (1-53)")
    is_weekend: bool = Field(description="True if Saturday/Sunday")
    is_month_start: bool = Field(description="True if day 1-3 of month")
    is_month_end: bool = Field(description="True if day 28-31 of month")

    @field_validator("amount_raw", "amount_signed", mode="before")
    @classmethod
    def coerce_decimal(cls, v):
        # This validator is scoped to monetary amount fields only
        # (amount_raw / amount_signed), so quantizing to cents is safe here.
        # A Decimal constructed elsewhere can carry sub-cent precision; the
        # parsers quantize but the model is the canonical guard, so enforce
        # cent-quantization on every money value reaching the model.
        if isinstance(v, (int, float, str)):
            try:
                d = Decimal(str(v))
            except InvalidOperation as e:
                # A non-numeric / oversized token must surface as a ValueError (which
                # pydantic wraps cleanly) rather than a raw decimal.InvalidOperation.
                # Keep the value out of the message (financial PII).
                raise ValueError("amount is not a valid number") from e
            if not d.is_finite():
                raise ValueError("amount must be finite")
            return quantize_cents(d)
        if isinstance(v, Decimal):
            if not v.is_finite():
                raise ValueError("amount must be finite")
            return quantize_cents(v)
        return v

    @model_validator(mode="after")
    def validate_cashflow_sign(self) -> Transaction:
        """
        Defense-in-depth: for rows that participate in cashflow calculations,
        enforce that the sign of amount_signed is consistent with cashflow_type.

        Rules (NARROW — only the clearly-wrong cases):
        - include_in_cashflow=True, cashflow_type="expense", amount_signed > 0  → invalid
        - include_in_cashflow=True, cashflow_type="income",  amount_signed < 0  → invalid

        NOT checked:
        - Transfers  (cashflow_type="transfer") — amount_signed may be any sign
        - Non-cashflow rows (include_in_cashflow=False) — no sign constraint
        - Zero amounts — zero is permitted on either side (edge-case guard)
        """
        if self.include_in_cashflow:
            if self.cashflow_type == "expense" and self.amount_signed > 0:
                raise ValueError(
                    f"expense in cashflow must have non-positive amount_signed, "
                    f"got {self.amount_signed}"
                )
            if self.cashflow_type == "income" and self.amount_signed < 0:
                raise ValueError(
                    f"income in cashflow must have non-negative amount_signed, "
                    f"got {self.amount_signed}"
                )
        return self

    @model_validator(mode="before")
    @classmethod
    def populate_temporal_fields(cls, data):
        if not isinstance(data, dict):
            return data

        posting_date = data.get("posting_date")
        if isinstance(posting_date, datetime):
            posting_date = posting_date.date()
        elif isinstance(posting_date, str):
            try:
                posting_date = date.fromisoformat(posting_date)
            except ValueError:
                return data

        if not isinstance(posting_date, date):
            return data

        values = dict(data)
        values.setdefault("day_of_week", posting_date.weekday())
        values.setdefault("day_of_month", posting_date.day)
        values.setdefault("week_of_year", posting_date.isocalendar()[1])
        values.setdefault("is_weekend", posting_date.weekday() >= 5)
        values.setdefault("is_month_start", posting_date.day <= 3)
        values.setdefault("is_month_end", posting_date.day >= 28)
        return values


class TransactionBuilder:
    """
    Mutable builder for constructing Transaction objects.

    Use this during parsing when fields are populated incrementally.
    Call .build() to get the immutable Transaction.
    """

    def __init__(self):
        self._data: dict = {}

    def set(self, **kwargs) -> TransactionBuilder:
        """Set one or more fields."""
        self._data.update(kwargs)
        return self

    def build(self) -> Transaction:
        """Construct the immutable Transaction."""
        return Transaction(**self._data)

    def to_dict(self) -> dict:
        """Get current data as dict."""
        return self._data.copy()


class ExceptionRecord(BaseModel):
    """Record for transactions that failed parsing."""

    model_config = ConfigDict(frozen=True)

    source_file: str
    source_path: str
    source_row: int
    reason: str
    raw_data: dict = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: naive_utc_now())


class Summary(BaseModel):
    """Processing run summary."""

    model_config = ConfigDict(frozen=True)

    generated_at: datetime
    parser_version: str
    config_versions: dict[str, str]

    source_path: str
    filters: dict[str, str | None]

    total_scanned: int
    total_processed: int
    duplicates_removed: int
    collisions: int
    exceptions: int

    transactions_count: int
    transfers_count: int

    by_institution: dict[str, int]
    by_flow: dict[str, int]
    by_category: dict[str, Decimal]

    cashflow_income: Decimal
    cashflow_expense: Decimal
    cashflow_net: Decimal

    by_quarter: dict[str, dict[str, Decimal]]
