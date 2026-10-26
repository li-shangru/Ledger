"""Mappings between SQLAlchemy rows and core models."""

from __future__ import annotations

from ledger.core import Transaction
from ledger.storage.database import TransactionDB, TransferDB


def transaction_from_db(row: TransactionDB) -> Transaction:
    """Convert a transaction database row to the core Transaction model."""
    return Transaction(
        txn_id=row.txn_id,
        txn_fingerprint=row.txn_fingerprint,
        posting_date=row.posting_date,
        transaction_date=row.transaction_date,
        institution=row.institution,
        account=row.account,
        account_type=row.account_type,
        flow=row.flow,
        cashflow_type=row.cashflow_type,
        include_in_cashflow=row.include_in_cashflow,
        currency=row.currency,
        amount_raw=row.amount_raw,
        amount_signed=row.amount_signed,
        description=row.description,
        merchant=row.merchant,
        category=row.category,
        merchant_confidence=row.merchant_confidence,
        category_confidence=row.category_confidence,
        exclude_from_spending=row.exclude_from_spending,
        merchant_raw=row.merchant_raw,
        category_raw=row.category_raw,
        source_file=row.source_file,
        source_path=row.source_path,
        source_period=row.source_period,
        source_row=row.source_row,
        imported_at=row.imported_at,
        parser_version=row.parser_version,
        recurring_pattern_id=row.recurring_pattern_id,
        # No transfer-pairing fields: TransactionDB stopped declaring them in
        # migration 20260810_0023 because they had no producer. They keep their
        # model defaults of None here and are only ever set on a transfer leg.
        day_of_week=row.day_of_week,
        day_of_month=row.day_of_month,
        week_of_year=row.week_of_year,
        is_weekend=row.is_weekend,
        is_month_start=row.is_month_start,
        is_month_end=row.is_month_end,
    )


def transfer_from_db(row: TransferDB) -> Transaction:
    """Convert a transfer row to the subset of Transaction needed for matching."""
    return Transaction(
        txn_id=row.txn_id,
        txn_fingerprint="",
        posting_date=row.posting_date,
        transaction_date=None,
        institution=row.institution,
        account=row.account,
        account_type=row.account_type,
        flow=row.flow,
        cashflow_type="transfer",
        include_in_cashflow=False,
        # Legacy/unflushed rows may carry NULL; treat as CAD (the column default and
        # is_cad's legacy semantics) so the reconstructed Transaction is always valid.
        currency=row.currency or "CAD",
        amount_raw=row.amount_raw,
        amount_signed=row.amount_raw,
        description=row.description,
        merchant="",
        category="Transfer",
        merchant_confidence="none",
        category_confidence="none",
        exclude_from_spending=True,
        merchant_raw="",
        category_raw="",
        source_file=row.source_file,
        source_path="",
        source_period="",
        source_row=0,
        imported_at=row.imported_at,
        parser_version="",
        recurring_pattern_id=None,
        paired_transfer_id=row.paired_transfer_id,
        transfer_confidence=float(row.transfer_confidence)
        if row.transfer_confidence is not None
        else None,
        counterparty_account=row.counterparty_account,
        counterparty_institution=row.counterparty_institution,
        day_of_week=row.posting_date.weekday(),
        day_of_month=row.posting_date.day,
        week_of_year=row.posting_date.isocalendar()[1],
        is_weekend=row.posting_date.weekday() >= 5,
        is_month_start=row.posting_date.day <= 3,
        is_month_end=row.posting_date.day >= 28,
    )
