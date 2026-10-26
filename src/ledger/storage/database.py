"""
SQLAlchemy models for persistent storage.

Maps Transaction Pydantic models to database tables.
"""

import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from threading import Lock
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    event,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker, validates
from sqlalchemy.pool import NullPool

from ledger.storage.exact_decimal import ExactFixedPoint
from ledger.utils.json_io import dumps_json_column


class Base(DeclarativeBase):
    """Base class for all models."""

    pass


class TransactionDB(Base):
    """Transaction table - main ledger data."""

    __tablename__ = "transactions"

    # Primary Key
    txn_id: Mapped[str] = mapped_column(String(40), primary_key=True)

    # Dates (indexed for range queries)
    posting_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    transaction_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Source
    institution: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    account: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    account_type: Mapped[str] = mapped_column(String(100), nullable=False, default="", index=True)

    # Classification
    flow: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    cashflow_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    include_in_cashflow: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    # Amounts
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CAD")
    amount_raw: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    amount_signed: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False, index=True)

    # Description
    description: Mapped[str] = mapped_column(Text, nullable=False)

    # Enrichment
    merchant: Mapped[str] = mapped_column(String(200), nullable=False, default="", index=True)
    category: Mapped[str] = mapped_column(String(100), nullable=False, default="Other", index=True)
    merchant_confidence: Mapped[str] = mapped_column(String(20), nullable=False, default="none")
    category_confidence: Mapped[str] = mapped_column(String(20), nullable=False, default="low")
    exclude_from_spending: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Raw data
    merchant_raw: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    category_raw: Mapped[str] = mapped_column(String(100), nullable=False, default="")

    # Provenance
    source_file: Mapped[str] = mapped_column(String(255), nullable=False)
    source_path: Mapped[str] = mapped_column(String(500), nullable=False)
    source_period: Mapped[str] = mapped_column(String(7), nullable=False, default="")  # YYYY-MM
    source_row: Mapped[int] = mapped_column(Integer, nullable=False)

    # Metadata
    imported_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    parser_version: Mapped[str] = mapped_column(String(40), nullable=False)
    txn_fingerprint: Mapped[str] = mapped_column(String(32), nullable=False)

    # Analysis Features
    recurring_pattern_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)

    # NOTE: transfer pairing (paired_transfer_id / transfer_confidence /
    # counterparty_account / counterparty_institution) lives on TransferDB ONLY.
    # It was declared here too, but the matcher never ran over transactions and
    # every write site hardcoded None, so migration 20260810_0023 dropped the
    # columns. Pairing fields on transfers remain populated by the matcher.
    # Do not re-add them here without a producer.
    # days_since_payday went with them: its "computed later by the analysis
    # pipeline" comment deferred to a pipeline that was never written.

    # Temporal features
    day_of_week: Mapped[int] = mapped_column(Integer, nullable=False)
    day_of_month: Mapped[int] = mapped_column(Integer, nullable=False)
    week_of_year: Mapped[int] = mapped_column(Integer, nullable=False)
    is_weekend: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_month_start: Mapped[bool] = mapped_column(Boolean, nullable=False)
    is_month_end: Mapped[bool] = mapped_column(Boolean, nullable=False)

    # Composite indexes for common queries
    __table_args__ = (
        Index("ix_date_category", "posting_date", "category"),
        Index("ix_date_merchant", "posting_date", "merchant"),
        Index("ix_period_institution", "source_period", "institution"),
    )


class TransferDB(Base):
    """Transfer transactions (separate table for clarity)."""

    __tablename__ = "transfers"

    txn_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    posting_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    institution: Mapped[str] = mapped_column(String(50), nullable=False)
    account: Mapped[str] = mapped_column(String(100), nullable=False)
    account_type: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    flow: Mapped[str] = mapped_column(String(20), nullable=False)
    amount_raw: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    # Native currency of the leg (no FX conversion applied), mirroring
    # TransactionDB.currency so the CAD-only balance/credit-limit guards apply
    # identically on both tables. Legacy rows predate the column -> "CAD".
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CAD")
    description: Mapped[str] = mapped_column(Text, nullable=False)
    source_file: Mapped[str] = mapped_column(String(255), nullable=False)
    source_path: Mapped[str | None] = mapped_column(String(500), nullable=True, index=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    # Transfer pairing fields
    paired_transfer_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    transfer_confidence: Mapped[Decimal | None] = mapped_column(
        Numeric(3, 2), nullable=True
    )  # 0.00-1.00
    counterparty_account: Mapped[str | None] = mapped_column(String(100), nullable=True)
    counterparty_institution: Mapped[str | None] = mapped_column(String(50), nullable=True)

    __table_args__ = (Index("ix_transfer_institution_account", "institution", "account"),)


class ImportRunDB(Base):
    """Track import runs for incremental processing."""

    __tablename__ = "import_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    source_path: Mapped[str] = mapped_column(String(500), nullable=False)
    files_processed: Mapped[int] = mapped_column(Integer, nullable=False)
    transactions_imported: Mapped[int] = mapped_column(Integer, nullable=False)
    transfers_imported: Mapped[int] = mapped_column(Integer, nullable=False)
    duplicates_skipped: Mapped[int] = mapped_column(Integer, nullable=False)
    errors: Mapped[int] = mapped_column(Integer, nullable=False)
    parser_version: Mapped[str] = mapped_column(String(40), nullable=False)


class ImportReviewDB(Base):
    """Manual-review rows for staged PDF imports.

    Read/written via the raw-SQL helpers in ``ledger.import_review``; declared
    here so the table is created by ``init_database`` and checked by migration
    validation rather than being an invisible side-table.

    The review queue is gated by the ``status`` field: only rows with
    ``status='pending'`` (or resolved-but-incomplete) appear as actionable.
    ``reviewed_as_is`` rows already have a distinct status value and are
    therefore excluded from the queue automatically — no extra column needed.
    """

    __tablename__ = "import_review"

    review_key: Mapped[str] = mapped_column(String, primary_key=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")
    merchant: Mapped[str] = mapped_column(String, nullable=False, default="")
    category: Mapped[str] = mapped_column(String, nullable=False, default="Other")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    scope: Mapped[str] = mapped_column(String, nullable=False, default="same_raw_merchant_in_run")
    txn_ids: Mapped[str] = mapped_column(Text, nullable=False)
    examples: Mapped[str] = mapped_column(Text, nullable=False)
    transaction_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_abs_amount: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[str] = mapped_column(String, nullable=False)
    updated_at: Mapped[str] = mapped_column(String, nullable=False)


class ProcessedFileDB(Base):
    """Track which files have been processed (for incremental imports)."""

    __tablename__ = "processed_files"

    file_path: Mapped[str] = mapped_column(String(500), primary_key=True)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)  # SHA-256 of content
    processed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    transaction_count: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (Index("ix_processed_at", "processed_at"),)


# === Analysis Tables ===


class AccountDB(Base):
    """Account table - auto-detected accounts."""

    __tablename__ = "accounts"

    # Primary Key
    account_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    # Identity
    institution: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    account_number: Mapped[str] = mapped_column(String(100), nullable=False)
    account_type: Mapped[str] = mapped_column(String(50), nullable=False)
    account_subtype: Mapped[str] = mapped_column(String(100), nullable=False)

    # Lifecycle
    first_transaction_date: Mapped[date] = mapped_column(Date, nullable=False)
    last_transaction_date: Mapped[date] = mapped_column(Date, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, index=True)

    # Statistics
    transaction_count: Mapped[int] = mapped_column(Integer, nullable=False)
    avg_monthly_transactions: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    # Credit card specifics
    is_credit_card: Mapped[bool] = mapped_column(Boolean, nullable=False)
    detected_credit_limit: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)

    # Metadata
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        # The slug is the join key every other banking table uses -- it is the
        # same value space as TransactionDB.account and
        # AccountStatementCreditLimitDB.account_slug. It must be unique for those
        # references to be well-defined.
        Index("ux_accounts_account_number", "account_number", unique=True),
    )


class AccountLifecycleDB(Base):
    """Operator-recorded open/closed state for one banking account.

    Nothing in an import can derive this. ``accounts.is_active`` looks like it does,
    but it is ``(today - last_transaction_date) <= 60`` recomputed at analysis time,
    and both coverage laggards tripped that threshold five times between them while
    open and still receiving statements. So the coverage-gap classifier reads THIS
    table for closure and never ``is_active``. Modelled on ``InvestmentAccountDB``'s
    lifecycle fields, the only working closure model in the repo.

    ``ledger accounts mark-closed | reopen`` is the ONLY writer -- an operator
    command, never an import. It is keyed by ``account_id`` while the operator
    thinks in slugs, so the CLI and ``ledger.coverage``'s gap classifier both
    translate through ``core.account_identity.lifecycle_account_ids``, which
    returns EVERY id a slug's record could be keyed by rather than one answer.
    One answer is not stable: the stored ``accounts.account_id`` disappears when
    the account ages out -- the case this table exists for -- and a coverage-only
    slug's institution is blank until account detection runs, so a record written
    under whichever id resolved at the time stopped applying at exactly the
    moment it was needed. The CLI writes under the canonical (derived) id and
    clears any row under the others, so one account keeps one row. An absent row
    means "the operator has said nothing", which is a meaningful answer and the
    state a fresh database is in.

    Two structural properties carry the whole point of the table:

    * Its name is in ``MERGE_FORWARD_TABLES``. That single tuple is read by BOTH the
      import drift gate and repair's promote, so membership is what makes a closure
      record survive ``repair statements`` -- which otherwise publishes an empty
      candidate wholesale. Classifying it merge-forward in prose alone buys nothing.
    * It carries NO ForeignKey to ``accounts``. ``save_accounts(replace_all=True)``
      deletes account rows that stop appearing in transactions, and a closed account
      is the likeliest to age out. An FK would make ``PRAGMA foreign_key_check`` read
      that aged-out row as referential corruption -- which ``validate_database``
      reports as an error and ``storage/read_only.py``'s preflight turns into a
      refusal to serve the database at all -- on exactly the case this table records.
      Orphans are censused by the doctor's ``account_lifecycle_orphans`` check
      instead, at LOW severity, because an orphan here is the expected end state and
      not drift.

    It does NOT survive ``rebuild-db``, no better than budgets do (see AGENTS.md).
    """

    __tablename__ = "account_lifecycle"

    account_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active", index=True)
    # When the operator last confirmed the status -- distinct from closed_on, which
    # is when the account actually closed (often earlier, sometimes unknown).
    status_confirmed_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    closed_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'closed')",
            name="ck_account_lifecycle_status",
        ),
    )

    @validates("status")
    def _validate_status(self, _key: str, value: str) -> str:
        if value not in {"active", "closed"}:
            raise ValueError("Account lifecycle status must be active or closed.")
        return value


class AccountBalanceDB(Base):
    """Account balance snapshots.

    SIGN CONVENTION -- one convention, both bases, every institution: ``balance``
    is signed from the ACCOUNT HOLDER'S point of view. POSITIVE is money HELD (an
    asset); NEGATIVE is money OWED (a liability). A $2,000 credit-card debt is
    ``-2000.00`` here.

    Issuers do not print it that way. A card statement prints its "New balance"
    as a POSITIVE amount owed, and some card statement layouts print purchases
    as positive charges. Storing either unchanged would make a $2,000 debt read
    as a $2,000 asset, so ``repair.statements`` negates them on the way in (see
    ``STATEMENT_BALANCE_SPECS``).

    The convention is not a new choice: the relative series has always produced
    it, because ``BalanceCalculator`` accumulates ``Transaction.amount_signed``
    (negative = outflow) and a card purchase is an outflow, so a card's relative
    balance falls as debt accrues. The consumers agree -- ``_compute_debt_ratio``
    counts a negative balance as debt and a positive one as an asset, and
    ``_compute_emergency_fund_months`` counts only positive non-card balances as
    liquid.
    """

    __tablename__ = "account_balances"

    # Composite primary key.
    #
    # RESTRICT, not CASCADE: save_accounts(replace_all=True) deletes these rows
    # explicitly and in order before retiring their account, so a cascade could
    # only ever hide a deletion the code did not intend.
    account_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("accounts.account_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    balance_date: Mapped[date] = mapped_column(Date, primary_key=True)

    # Balance
    balance: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    balance_basis: Mapped[str] = mapped_column(
        String(16),
        primary_key=True,
        default="relative",
        server_default="relative",
    )
    transaction_count: Mapped[int] = mapped_column(Integer, nullable=False)

    # Rolling statistics
    balance_7d_avg: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    balance_30d_avg: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    balance_7d_min: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    balance_7d_max: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    # Credit card specific
    credit_utilization: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)

    # Metadata
    computed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        Index("ix_balance_date", "balance_date"),
        Index("ix_balance_account_date", "account_id", "balance_date"),
    )


class AccountStatementCreditLimitDB(Base):
    """Printed credit limit parsed from a statement, keyed by account slug.

    The account slug is the CSV stem (== ``TransactionDB.account``).
    Populated during PDF finalize from each
    statement's ``validation_summary["printed_credit_limit"]``; read by
    AccountDetector, which prefers the printed figure over the deepest-balance
    estimate. Keyed by (account_slug, statement_date) so re-importing a statement
    updates in place while a card's history across months is retained (the
    detector reads the LATEST by statement_date).
    """

    __tablename__ = "account_statement_credit_limits"

    account_slug: Mapped[str] = mapped_column(String(100), primary_key=True)
    statement_date: Mapped[date] = mapped_column(Date, primary_key=True)
    credit_limit: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    # No explicit (account_slug, statement_date) index: the composite PRIMARY KEY
    # already provides one on exactly those leading columns.


class StatementCoverageDB(Base):
    """One row per account-month for which a statement has been imported.

    Answers whether an account's monthly statement is imported -- a question the
    transaction table cannot answer, because a month with no rows is
    indistinguishable from a month never imported.

    Deliberately records statements that produced ZERO transactions, keyed by the
    slug the extractor assigned. Those are exactly the case this table exists for:
    finalize skips every item carrying a ``skipped_reason``, so without an explicit
    record "imported, no activity" and "never imported" look identical.

    No foreign key to ``accounts``. Rows land at PDF finalize, before account
    detection has created the account row for a newly-seen account; a constraint
    would fail the import. ``DatabaseDoctor._check_account_orphans`` covers it.
    """

    __tablename__ = "statement_coverage"

    account_slug: Mapped[str] = mapped_column(String(100), primary_key=True)
    period: Mapped[str] = mapped_column(String(7), primary_key=True)  # YYYY-MM

    institution: Mapped[str] = mapped_column(String(50), nullable=False, default="")
    statement_type: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    statement_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    skipped_reason: Mapped[str] = mapped_column(String(60), nullable=False, default="")
    imported_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (Index("ix_statement_coverage_period", "period"),)


class StatementCoverageIntervalDB(Base):
    """Precise printed statement bounds, never inferred from month labels or activity."""

    __tablename__ = "statement_coverage_intervals"

    interval_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    institution: Mapped[str] = mapped_column(String(50), nullable=False)
    account_slug: Mapped[str] = mapped_column(String(100), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    evidence_basis: Mapped[str] = mapped_column(String(40), nullable=False)
    imported_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        CheckConstraint("start_date <= end_date", name="ck_statement_interval_dates"),
        Index("ix_statement_interval_account", "institution", "account_slug", "start_date"),
    )


class PlannedScheduleRevisionDB(Base):
    """Append-only complete definitions; tombstones retain removed expectations."""

    __tablename__ = "planned_schedule_revisions"

    revision_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    schedule_id: Mapped[str] = mapped_column(String(64), nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    definition_json: Mapped[str] = mapped_column(Text, nullable=False)
    is_removed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    history_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        Index("ix_schedule_revision_effective", "schedule_id", "effective_date", "revision_id"),
    )


class PlanReconciliationReviewDB(Base):
    """Review applies only to a particular occurrence's material evidence."""

    __tablename__ = "plan_reconciliation_reviews"

    occurrence_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    material_revision: Mapped[str] = mapped_column(String(64), primary_key=True)
    reviewed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class PlanningDefinitionGenerationDB(Base):
    """Persistent logical-key generations include removed budgets/preferences."""

    __tablename__ = "planning_definition_generations"

    target_kind: Mapped[str] = mapped_column(String(20), primary_key=True)
    key_a: Mapped[str] = mapped_column(String(20), primary_key=True)
    key_b: Mapped[str] = mapped_column(String(160), primary_key=True)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "target_kind IN ('budget', 'preference')", name="ck_planning_generation_kind"
        ),
        CheckConstraint("generation >= 1", name="ck_planning_generation_positive"),
    )


class PlanningProposalReceiptDB(Base):
    """Atomic per-record application and conditional undo receipt."""

    __tablename__ = "planning_proposal_receipts"

    proposal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    proposal_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    preview_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    target_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    target_id: Mapped[str] = mapped_column(String(160), nullable=False)
    before_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    after_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    effective_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    undo_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)


class RecurringPatternDB(Base):
    """Recurring transaction patterns."""

    __tablename__ = "recurring_patterns"

    # Primary Key
    pattern_id: Mapped[str] = mapped_column(String(64), primary_key=True)

    # Classification
    pattern_type: Mapped[str] = mapped_column(String(20), nullable=False, index=True)

    # Pattern details
    merchant: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    institution: Mapped[str] = mapped_column(String(50), nullable=False)
    account_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("accounts.account_id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )

    # Frequency
    frequency: Mapped[str] = mapped_column(String(20), nullable=False)
    expected_day_of_period: Mapped[int] = mapped_column(Integer, nullable=False)

    # Amount pattern
    is_fixed_amount: Mapped[bool] = mapped_column(Boolean, nullable=False)
    expected_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    amount_std_dev: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    # Detection metadata
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    detection_method: Mapped[str] = mapped_column(String(20), nullable=False)

    # Statistics
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False)
    first_occurrence: Mapped[date] = mapped_column(Date, nullable=False)
    last_occurrence: Mapped[date] = mapped_column(Date, nullable=False)
    next_expected_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)

    # Health
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, index=True)
    missed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Metadata
    detected_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class SpendingTrendDB(Base):
    """Pre-aggregated spending trends."""

    __tablename__ = "spending_trends"

    # Composite primary key
    time_period: Mapped[str] = mapped_column(String(20), primary_key=True)
    period_type: Mapped[str] = mapped_column(String(20), primary_key=True)
    category: Mapped[str] = mapped_column(String(100), primary_key=True)

    # Metrics
    transaction_count: Mapped[int] = mapped_column(Integer, nullable=False)
    total_spent: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    avg_transaction: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    median_transaction: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)

    # Comparisons
    vs_previous_period_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    vs_previous_period_abs: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    vs_avg_6m_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)

    # Top merchant
    top_merchant: Mapped[str | None] = mapped_column(String(200), nullable=True)
    top_merchant_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)

    # Trend
    trend_direction: Mapped[str] = mapped_column(String(20), nullable=False)

    # Metadata
    computed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        Index("ix_trend_period", "time_period", "period_type"),
        Index("ix_trend_category", "category"),
    )


class FinancialHealthScoreDB(Base):
    """Financial health scores."""

    __tablename__ = "financial_health_scores"

    # Primary Key
    score_date: Mapped[date] = mapped_column(Date, primary_key=True)

    # Overall score
    overall_score: Mapped[int] = mapped_column(Integer, nullable=False, index=True)

    # Component scores
    cashflow_health: Mapped[int] = mapped_column(Integer, nullable=False)
    spending_consistency: Mapped[int] = mapped_column(Integer, nullable=False)
    savings_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    # Nullable: None = not assessable without an explicit absolute balance anchor.
    emergency_fund_months: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    debt_ratio: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)

    # Trends
    score_trend_7d: Mapped[str] = mapped_column(String(20), nullable=False)
    score_change_7d: Mapped[int] = mapped_column(Integer, nullable=False)

    # Risks
    risk_factors: Mapped[str] = mapped_column(Text, nullable=False)  # JSON array

    # Metadata
    computed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


# === Runtime Configuration Tables ===


class ConfigMetadataDB(Base):
    """Key-value metadata for SQLite-backed runtime configuration."""

    __tablename__ = "config_metadata"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


class MerchantRuleDB(Base):
    """Ordered merchant normalization rule."""

    __tablename__ = "merchant_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    merchant_norm: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    exclude_from_spending: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, index=True)


class MerchantRulePatternDB(Base):
    """Regex pattern belonging to a merchant rule."""

    __tablename__ = "merchant_rule_patterns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    pattern: Mapped[str] = mapped_column(Text, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (Index("ix_rule_pattern_order", "rule_id", "sort_order"),)


class CategoryConfigDB(Base):
    """Runtime category definition."""

    __tablename__ = "category_config"

    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    parent: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    exclude_from_spending: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, index=True)


class CategoryMerchantDB(Base):
    """Merchant assigned to a runtime category."""

    __tablename__ = "category_merchants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    category_name: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    merchant: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        Index("ix_category_merchant_order", "category_name", "sort_order"),
        Index("ix_category_merchant_lookup", "merchant"),
    )


class RuleApplicationRunDB(Base):
    """Audit trail for applying merchant/category rules to historical rows."""

    __tablename__ = "rule_application_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(80), nullable=False, default="manual")
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="success")
    merchant_profiles_changed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    categories_changed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    transactions_scanned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    transactions_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    analytics_refreshed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    validation_issues: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    __table_args__ = (Index("ix_rule_apply_source_date", "source", "applied_at"),)


class ReportSnapshotDB(Base):
    """Cached reporting summary for passive dashboard reads."""

    __tablename__ = "report_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # No index=True: the explicit ix_report_snapshot_date below already indexes
    # created_at (index=True would emit a second, redundant index on the same column).
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    source: Mapped[str] = mapped_column(String(80), nullable=False, default="manual")
    source_kind: Mapped[str] = mapped_column(
        String(40), nullable=False, default="reporting", index=True
    )
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=3, index=True)
    period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="success")
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_report_snapshot_date", "created_at"),
        Index(
            "ix_report_snapshot_period_schema_source",
            "period_start",
            "period_end",
            "schema_version",
            "source_kind",
        ),
    )


class InsightFindingDB(Base):
    """Explainable rules-based insight finding for one reporting period."""

    __tablename__ = "insight_findings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # unique=True already creates a unique index; index=True would add a second,
    # redundant non-unique index on the same column.
    fingerprint: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    period_start: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    period_end: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    finding_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(240), nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    suggested_action: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="open", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index(
            "ix_insight_period_type_status", "period_start", "period_end", "finding_type", "status"
        ),
    )


class BudgetDB(Base):
    """Monthly or recurring category budget."""

    __tablename__ = "budgets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    budget_month: Mapped[str] = mapped_column(String(7), nullable=False, default="*", index=True)
    category: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (Index("ix_budget_month_category", "budget_month", "category", unique=True),)


class SavingsGoalDB(Base):
    """Local savings or wealth-building goal."""

    __tablename__ = "savings_goals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False, index=True)
    account_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    target_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    target_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active", index=True)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (Index("ix_savings_goal_account_status", "account_id", "status"),)


class SpendingPreferenceDB(Base):
    """Editable intent attached to a merchant or category, separate from facts."""

    __tablename__ = "spending_preferences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scope: Mapped[str] = mapped_column(String(20), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    disposition: Mapped[str] = mapped_column(String(20), nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        CheckConstraint("scope IN ('merchant', 'category')", name="ck_spending_preference_scope"),
        CheckConstraint(
            "disposition IN ('intentional', 'necessary', 'worthwhile')",
            name="ck_spending_preference_disposition",
        ),
        CheckConstraint(
            "length(trim(name)) BETWEEN 1 AND 160",
            name="ck_spending_preference_name",
        ),
        CheckConstraint("length(note) <= 500", name="ck_spending_preference_note"),
        Index("ux_spending_preference_scope_name", "scope", "name", unique=True),
    )


class PlannedScheduleDB(Base):
    """User-authored future cash event, kept outside imported financial facts."""

    __tablename__ = "planned_schedules"

    schedule_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    event_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="CAD")
    account_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    counterparty_account_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    next_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    frequency: Mapped[str] = mapped_column(String(20), nullable=False)
    provenance: Mapped[str] = mapped_column(String(20), nullable=False)
    pattern_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "event_type IN ('income', 'bill', 'card_payment', 'transfer')",
            name="ck_planned_schedule_event_type",
        ),
        CheckConstraint(
            "frequency IN ('once', 'weekly', 'biweekly', 'semimonthly', 'monthly', "
            "'quarterly', 'yearly')",
            name="ck_planned_schedule_frequency",
        ),
        CheckConstraint(
            "provenance IN ('confirmed', 'hypothetical')",
            name="ck_planned_schedule_provenance",
        ),
        CheckConstraint(
            "amount > 0 AND amount <= 9999999999.99",
            name="ck_planned_schedule_amount",
        ),
        CheckConstraint("currency = 'CAD'", name="ck_planned_schedule_currency"),
        CheckConstraint(
            "length(trim(name)) BETWEEN 1 AND 160",
            name="ck_planned_schedule_name",
        ),
        CheckConstraint(
            "event_type NOT IN ('card_payment', 'transfer') OR counterparty_account_id IS NOT NULL",
            name="ck_planned_schedule_counterparty",
        ),
        CheckConstraint(
            "event_type IN ('card_payment', 'transfer') OR counterparty_account_id IS NULL",
            name="ck_planned_schedule_single_account",
        ),
        CheckConstraint(
            "counterparty_account_id IS NULL OR counterparty_account_id != account_id",
            name="ck_planned_schedule_distinct_accounts",
        ),
        CheckConstraint("length(note) <= 500", name="ck_planned_schedule_note"),
        Index("ix_planned_schedule_active_date", "is_active", "next_date"),
        Index(
            "ux_planned_schedule_confirmed_pattern",
            "pattern_id",
            unique=True,
            sqlite_where=text("pattern_id IS NOT NULL AND provenance = 'confirmed'"),
        ),
    )


class ExportRunDB(Base):
    """Local audit trail for generated analysis exports."""

    __tablename__ = "export_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    preset: Mapped[str] = mapped_column(String(80), nullable=False)
    export_format: Mapped[str] = mapped_column(String(30), nullable=False)
    output_path: Mapped[str] = mapped_column(String(500), nullable=False)
    rows_exported: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="success")
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    __table_args__ = (Index("ix_export_run_preset_date", "preset", "created_at"),)


class SchemaMigrationDB(Base):
    """Applied local SQLite schema migration."""

    __tablename__ = "schema_migrations"

    version: Mapped[str] = mapped_column(String(80), primary_key=True)
    description: Mapped[str] = mapped_column(String(300), nullable=False)
    applied_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)


class DBOperationAuditDB(Base):
    """Audit trail for local database operations."""

    __tablename__ = "db_operation_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operation: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="success", index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    db_path: Mapped[str] = mapped_column(String(500), nullable=False)
    artifact_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    counts: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    warnings: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    errors: Mapped[list[Any]] = mapped_column(JSON, nullable=False, default=list)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    __table_args__ = (Index("ix_db_operation_status_date", "operation", "status", "started_at"),)


class DataQualityCheckDB(Base):
    """Recorded data quality check result."""

    __tablename__ = "data_quality_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    check_name: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    issue_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)

    __table_args__ = (Index("ix_data_quality_run", "run_at", "severity", "status"),)


class TransactionTreatmentDB(Base):
    """Deterministic reporting treatment for one raw transaction."""

    __tablename__ = "transaction_treatments"

    txn_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    treatment_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 2), nullable=False)
    netting_policy: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    target_category: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    detected_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    source: Mapped[str] = mapped_column(
        String(60), nullable=False, default="deterministic_v1", index=True
    )

    __table_args__ = (
        Index("ix_treatment_type_status", "treatment_type", "status"),
        Index("ix_treatment_policy_category", "netting_policy", "target_category"),
    )


class TransactionOffsetDB(Base):
    """Offset link from a credit transaction to a matched original debit."""

    __tablename__ = "transaction_offsets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    credit_txn_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    debit_txn_id: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    offset_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 2), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    detected_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    source: Mapped[str] = mapped_column(
        String(60), nullable=False, default="deterministic_v1", index=True
    )

    __table_args__ = (
        Index("ix_offset_credit_debit", "credit_txn_id", "debit_txn_id"),
        Index("ix_offset_status_source", "status", "source"),
    )


# === Investment Tables ===
# Investment statements are historical source records. They deliberately do not
# reference TransactionDB or participate in cashflow classification, transfer
# pairing, or transaction-derived account analysis.


class InvestmentAccountDB(Base):
    """Persisted identity and statement lifecycle for one investment account."""

    __tablename__ = "investment_accounts"

    account_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    institution: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    source_account_id: Mapped[str] = mapped_column(String(128), nullable=False)
    account_alias: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    account_type: Mapped[str] = mapped_column(String(100), nullable=False)
    base_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="active", index=True)
    transfer_destination: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status_confirmed_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    closed_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    first_statement_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_statement_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        Index("ix_investment_account_institution_source", "institution", "source_account_id"),
        CheckConstraint(
            "status IN ('active', 'closed', 'unknown')",
            name="ck_investment_account_status",
        ),
    )

    @validates("status")
    def _validate_status(self, _key: str, value: str) -> str:
        if value not in {"active", "closed", "unknown"}:
            raise ValueError("Investment account status must be active, closed, or unknown.")
        return value


class InvestmentStatementDB(Base):
    """Historical investment statement header and aggregate performance values."""

    __tablename__ = "investment_statements"

    statement_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    account_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("investment_accounts.account_id"), nullable=False, index=True
    )
    period_start: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    period_end: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    generated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    base_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    total_nav: Mapped[Decimal] = mapped_column(ExactFixedPoint(), nullable=False)
    time_weighted_return: Mapped[Decimal | None] = mapped_column(ExactFixedPoint(), nullable=True)
    parsed_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    imported_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "account_id",
            "period_start",
            "period_end",
            name="uq_investment_statement_account_period",
        ),
        Index("ix_investment_statement_account_period", "account_id", "period_start", "period_end"),
    )


class InvestmentAssetBalanceDB(Base):
    """Statement-owned balance summary for one asset class."""

    __tablename__ = "investment_asset_balances"

    statement_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("investment_statements.statement_id"), primary_key=True
    )
    asset_class: Mapped[str] = mapped_column(String(100), primary_key=True)
    prior_total: Mapped[Decimal | None] = mapped_column(ExactFixedPoint(), nullable=True)
    current_long: Mapped[Decimal | None] = mapped_column(ExactFixedPoint(), nullable=True)
    current_short: Mapped[Decimal | None] = mapped_column(ExactFixedPoint(), nullable=True)
    current_total: Mapped[Decimal] = mapped_column(ExactFixedPoint(), nullable=False)
    change: Mapped[Decimal | None] = mapped_column(ExactFixedPoint(), nullable=True)


class InvestmentCashBalanceDB(Base):
    """Source-printed native cash summary and FX evidence for one statement."""

    __tablename__ = "investment_cash_balances"

    statement_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("investment_statements.statement_id"), primary_key=True
    )
    currency: Mapped[str] = mapped_column(String(3), primary_key=True)
    opening_balance: Mapped[Decimal] = mapped_column(ExactFixedPoint(), nullable=False)
    debits: Mapped[Decimal] = mapped_column(ExactFixedPoint(), nullable=False)
    credits: Mapped[Decimal] = mapped_column(ExactFixedPoint(), nullable=False)
    closing_balance: Mapped[Decimal] = mapped_column(ExactFixedPoint(), nullable=False)
    opening_fx_rate: Mapped[Decimal | None] = mapped_column(ExactFixedPoint(), nullable=True)
    closing_fx_rate: Mapped[Decimal | None] = mapped_column(ExactFixedPoint(), nullable=True)


class InvestmentPositionDB(Base):
    """Statement-owned position snapshot preserving source precision and order."""

    __tablename__ = "investment_positions"

    statement_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("investment_statements.statement_id"), primary_key=True
    )
    source_row: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_category: Mapped[str] = mapped_column(String(100), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    symbol: Mapped[str] = mapped_column(String(100), nullable=False)
    discriminator: Mapped[str] = mapped_column(String(100), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(ExactFixedPoint(), nullable=False)
    multiplier: Mapped[Decimal | None] = mapped_column(ExactFixedPoint(), nullable=True)
    cost_price: Mapped[Decimal | None] = mapped_column(ExactFixedPoint(), nullable=True)
    cost_basis: Mapped[Decimal | None] = mapped_column(ExactFixedPoint(), nullable=True)
    close_price: Mapped[Decimal | None] = mapped_column(ExactFixedPoint(), nullable=True)
    market_value: Mapped[Decimal | None] = mapped_column(ExactFixedPoint(), nullable=True)
    unrealized_pl: Mapped[Decimal | None] = mapped_column(ExactFixedPoint(), nullable=True)

    __table_args__ = (Index("ix_investment_position_statement_symbol", "statement_id", "symbol"),)


class InvestmentPerformanceDB(Base):
    """Statement-owned realized/unrealized performance values from the source."""

    __tablename__ = "investment_performance"

    statement_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("investment_statements.statement_id"), primary_key=True
    )
    source_row: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_category: Mapped[str] = mapped_column(String(100), nullable=False)
    symbol: Mapped[str] = mapped_column(String(100), nullable=False, default="")
    cost_adjustment: Mapped[Decimal | None] = mapped_column(ExactFixedPoint(), nullable=True)
    realized_short_profit: Mapped[Decimal | None] = mapped_column(ExactFixedPoint(), nullable=True)
    realized_short_loss: Mapped[Decimal | None] = mapped_column(ExactFixedPoint(), nullable=True)
    realized_long_profit: Mapped[Decimal | None] = mapped_column(ExactFixedPoint(), nullable=True)
    realized_long_loss: Mapped[Decimal | None] = mapped_column(ExactFixedPoint(), nullable=True)
    realized_total: Mapped[Decimal | None] = mapped_column(ExactFixedPoint(), nullable=True)
    unrealized_short_profit: Mapped[Decimal | None] = mapped_column(
        ExactFixedPoint(), nullable=True
    )
    unrealized_short_loss: Mapped[Decimal | None] = mapped_column(ExactFixedPoint(), nullable=True)
    unrealized_long_profit: Mapped[Decimal | None] = mapped_column(ExactFixedPoint(), nullable=True)
    unrealized_long_loss: Mapped[Decimal | None] = mapped_column(ExactFixedPoint(), nullable=True)
    unrealized_total: Mapped[Decimal | None] = mapped_column(ExactFixedPoint(), nullable=True)
    total: Mapped[Decimal | None] = mapped_column(ExactFixedPoint(), nullable=True)
    code: Mapped[str] = mapped_column(String(80), nullable=False, default="")

    __table_args__ = (
        Index("ix_investment_performance_statement_asset", "statement_id", "asset_category"),
    )


class InvestmentActivityDB(Base):
    """Statement-owned investment activity, independent from cashflow rows."""

    __tablename__ = "investment_activities"

    statement_id: Mapped[str] = mapped_column(
        String(128), ForeignKey("investment_statements.statement_id"), primary_key=True
    )
    activity_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    activity_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    activity_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    activity_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    symbol: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    quantity: Mapped[Decimal | None] = mapped_column(ExactFixedPoint(), nullable=True)
    trade_price: Mapped[Decimal | None] = mapped_column(ExactFixedPoint(), nullable=True)
    close_price: Mapped[Decimal | None] = mapped_column(ExactFixedPoint(), nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(ExactFixedPoint(), nullable=True)
    commission_fee: Mapped[Decimal | None] = mapped_column(ExactFixedPoint(), nullable=True)
    basis: Mapped[Decimal | None] = mapped_column(ExactFixedPoint(), nullable=True)
    realized_pl: Mapped[Decimal | None] = mapped_column(ExactFixedPoint(), nullable=True)
    mtm_pl: Mapped[Decimal | None] = mapped_column(ExactFixedPoint(), nullable=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    direction: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_row: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        Index("ix_investment_activity_statement_date", "statement_id", "activity_date"),
    )


class InvestmentSourceFileDB(Base):
    """Content identity plus the current operational source projection."""

    __tablename__ = "investment_source_files"

    file_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    source_path: Mapped[str] = mapped_column(String(500), nullable=False)
    source_role: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    original_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    archived_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    structured_import: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    byte_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    archive_status: Mapped[str | None] = mapped_column(String(30), nullable=True, index=True)
    import_run_id: Mapped[str | None] = mapped_column(
        String(128), ForeignKey("investment_import_runs.run_id"), nullable=True, index=True
    )
    statement_id: Mapped[str | None] = mapped_column(
        String(128), ForeignKey("investment_statements.statement_id"), nullable=True, index=True
    )
    discovered_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    staged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default="discovered", index=True
    )


class InvestmentImportRunDB(Base):
    """Lifecycle record for a standalone investment-statement import run."""

    __tablename__ = "investment_import_runs"

    run_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    staged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finalizing_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    archive_pending_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    source_path: Mapped[str] = mapped_column(String(500), nullable=False)
    private_manifest_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    baseline_fingerprint: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    staging_fingerprint: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    target_fingerprint: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    statements_imported: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    structured_source_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    evidence_source_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    inserted_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    replaced_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    unchanged_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lifecycle_inputs: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    conflict_review: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    conflict_authorization_fingerprint: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    redacted_failure: Mapped[str | None] = mapped_column(String(120), nullable=True)
    errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    __table_args__ = (Index("ix_investment_import_run_status_started", "status", "started_at"),)


class InvestmentSourceAssociationDB(Base):
    """Durable source occurrence retained across reimports and supersession."""

    __tablename__ = "investment_source_associations"

    association_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    file_hash: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("investment_source_files.file_hash"),
        nullable=False,
        index=True,
    )
    import_run_id: Mapped[str | None] = mapped_column(
        String(128),
        ForeignKey("investment_import_runs.run_id"),
        nullable=True,
        index=True,
    )
    # Historical statement identifiers intentionally outlive the mutable current
    # statement row. File/run ownership remains FK-enforced; supersession is explicit.
    statement_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    statement_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_path: Mapped[str] = mapped_column(String(500), nullable=False)
    source_role: Mapped[str] = mapped_column(String(30), nullable=False)
    original_path: Mapped[str] = mapped_column(String(500), nullable=False)
    archived_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    structured_import: Mapped[bool] = mapped_column(Boolean, nullable=False)
    byte_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    archive_status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    staged_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    superseded_by_statement_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index(
            "ix_investment_source_association_run_hash",
            "import_run_id",
            "file_hash",
        ),
    )


# Database initialization

# Module-level engine cache (thread-safe singleton pattern)
_engine_cache: dict[str, Engine] = {}
_engine_lock = Lock()


def get_engine(db_path: Path, echo: bool = False) -> Engine:
    """
    Get or create SQLAlchemy engine (cached per database path).

    Thread-safe singleton pattern ensures one engine per database.
    This significantly improves performance by reusing connections.
    """
    from ledger.runtime import require_external_runtime_path

    db_path = require_external_runtime_path(db_path)
    cache_key = str(db_path.resolve())

    # Fast path: engine already exists
    if cache_key in _engine_cache:
        return _engine_cache[cache_key]

    # Slow path: create new engine (thread-safe)
    with _engine_lock:
        # Double-check after acquiring lock
        if cache_key not in _engine_cache:
            engine = create_engine(
                f"sqlite:///{db_path}",
                echo=echo,
                connect_args={"check_same_thread": False},
                # SQLite file connections are cheap, and NullPool avoids stale
                # file handles across CLI commands, tests, and Streamlit reruns.
                poolclass=NullPool,
                # Normalize Decimal/date before they reach json.dumps. Stock dumps
                # raises TypeError on a Decimal, and this codebase computes in
                # Decimal everywhere -- so any JSON column is one un-normalized
                # value away from a hard failure at flush time.
                json_serializer=dumps_json_column,
            )

            @event.listens_for(engine, "connect")
            def _set_sqlite_pragmas(dbapi_connection, connection_record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA busy_timeout=5000")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

            _engine_cache[cache_key] = engine

        return _engine_cache[cache_key]


def clear_engine_cache() -> None:
    """Clear engine cache (for testing)."""
    global _engine_cache
    with _engine_lock:
        for engine in _engine_cache.values():
            engine.dispose()
        _engine_cache.clear()


@contextmanager
def session_scope(db_path: Path) -> Iterator[Session]:
    """
    Provide a transactional scope around a series of operations.

    Auto-commits on success, auto-rolls back on exception.
    Always closes the session in the finally block.

    Usage:
        with session_scope(db_path) as session:
            session.add(obj)
            # Commit happens automatically
            # Rollback on exception
    """
    engine = get_engine(db_path)
    session_local = sessionmaker(bind=engine)
    session = session_local()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


_SQLITE_FILE_SUFFIXES = ("", "-journal", "-wal", "-shm")


def _reject_database_path_symlinks(db_path: Path) -> None:
    lexical = db_path if db_path.is_absolute() else Path.cwd() / db_path
    current = Path(lexical.anchor)
    for part in lexical.parts[1:]:
        current /= part
        if current.is_symlink():
            raise OSError(f"Database path contains a symlink: {current}")


@dataclass
class _PinnedDatabaseInit:
    path: Path
    engine_cache_key: str
    parent_descriptor: int
    parent_identity: tuple[int, int]
    descriptor: int
    file_identity: tuple[int, int]
    created_here: bool

    def validate(self) -> None:
        _reject_database_path_symlinks(self.path)
        current_parent = self.path.parent.stat(follow_symlinks=False)
        if (current_parent.st_dev, current_parent.st_ino) != self.parent_identity:
            raise OSError(f"Database parent changed during initialization: {self.path.parent}")
        current_file = os.stat(
            self.path.name,
            dir_fd=self.parent_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(current_file.st_mode)
            or (
                current_file.st_dev,
                current_file.st_ino,
            )
            != self.file_identity
        ):
            raise OSError(f"Database file changed during initialization: {self.path}")


def _discard_database_engine(cache_key: str) -> None:
    with _engine_lock:
        engine = _engine_cache.pop(cache_key, None)
    if engine is not None:
        engine.dispose()


@contextmanager
def _pin_database_for_init(db_path: Path) -> Iterator[_PinnedDatabaseInit]:
    from ledger.runtime import require_external_runtime_path

    db_path = require_external_runtime_path(db_path)
    db_path = db_path.expanduser()
    if not db_path.is_absolute():
        db_path = Path.cwd() / db_path
    _reject_database_path_symlinks(db_path)
    engine_cache_key = str(db_path.resolve())
    parent = db_path.parent
    expected_parent = parent.stat(follow_symlinks=False)
    parent_flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        parent_flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        parent_flags |= os.O_NOFOLLOW
    parent_descriptor = os.open(parent, parent_flags)
    descriptor = -1
    created_here = False
    try:
        pinned_parent = os.fstat(parent_descriptor)
        parent_identity = (expected_parent.st_dev, expected_parent.st_ino)
        if (pinned_parent.st_dev, pinned_parent.st_ino) != parent_identity:
            raise OSError(f"Database parent changed during initialization: {parent}")

        file_flags = os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            file_flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(db_path.name, file_flags, dir_fd=parent_descriptor)
        except FileNotFoundError:
            descriptor = os.open(
                db_path.name,
                file_flags | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=parent_descriptor,
            )
            created_here = True
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"Database path is not a regular file: {db_path}")
        # Keep private permissions without changing ctime on every no-op schema
        # check: dashboard read caches use that metadata to detect external writes.
        if os.name == "posix" and (created_here or stat.S_IMODE(metadata.st_mode) != 0o600):
            os.fchmod(descriptor, 0o600)
        pinned = _PinnedDatabaseInit(
            path=db_path,
            engine_cache_key=engine_cache_key,
            parent_descriptor=parent_descriptor,
            parent_identity=parent_identity,
            descriptor=descriptor,
            file_identity=(metadata.st_dev, metadata.st_ino),
            created_here=created_here,
        )
        pinned.validate()
        yield pinned
    except BaseException:
        if created_here:
            _discard_database_engine(engine_cache_key)
            for suffix in _SQLITE_FILE_SUFFIXES:
                with suppress(BaseException):
                    os.unlink(f"{db_path.name}{suffix}", dir_fd=parent_descriptor)
        raise
    finally:
        if descriptor >= 0:
            with suppress(BaseException):
                os.close(descriptor)
        with suppress(BaseException):
            os.close(parent_descriptor)


def init_database(db_path: Path) -> None:
    """Initialize database schema."""
    with _pin_database_for_init(Path(db_path)) as pinned:
        engine = get_engine(pinned.path)
        with engine.begin() as connection:
            pinned.validate()
            try:
                Base.metadata.create_all(connection)
            except BaseException:
                pinned.validate()
                raise
            pinned.validate()


def get_session(db_path: Path) -> Session:
    """Get a database session."""
    engine = get_engine(db_path)
    session_local = sessionmaker(bind=engine)
    return session_local()


def drop_all_tables(db_path: Path) -> None:
    """Drop all tables (for testing/reset)."""
    engine = get_engine(db_path)
    Base.metadata.drop_all(engine)
