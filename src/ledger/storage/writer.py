"""
Write transactions to database.

Handles upserts (insert or update) to support re-imports.
"""

import logging
from collections.abc import Sequence
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.orm import Session

from ledger.core import Transaction
from ledger.core.enums import Confidence
from ledger.core.types import DatabaseStats
from ledger.processing import ProcessingResult
from ledger.processing.identity import (
    require_unambiguous_cross_source_identity,
    transaction_identity,
    transfer_identity,
)
from ledger.storage.database import (
    ImportRunDB,
    TransactionDB,
    TransferDB,
    init_database,
    session_scope,
)
from ledger.storage.overrides import DurableOverride, reconcile_durable_overrides
from ledger.utils.dates import naive_utc_now

logger = logging.getLogger(__name__)


class DatabaseWriter:
    """Write ledger data to SQLite database."""

    def __init__(self, db_path: Path):
        self.db_path = db_path

        # Ensure database exists
        if not db_path.exists():
            logger.info(f"Creating database: {db_path}")
            init_database(db_path)

    def write_result(
        self,
        result: ProcessingResult,
        source_path: Path,
        parser_version: str,
        session: Session | None = None,
        durable_overrides: Sequence[DurableOverride] = (),
    ) -> None:
        """
        Write processing result to database.

        Uses UPSERT to handle re-imports gracefully. Pass ``session`` to write inside an
        existing transaction (e.g. so a delete + re-insert re-import commits atomically);
        when omitted, opens and commits its own session_scope.
        """
        if session is not None:
            self._write_into(
                session,
                result,
                source_path,
                parser_version,
                durable_overrides=durable_overrides,
            )
        else:
            with session_scope(self.db_path) as own_session:
                self._write_into(
                    own_session,
                    result,
                    source_path,
                    parser_version,
                    durable_overrides=durable_overrides,
                )
        logger.info("Database write completed successfully")

    def _write_into(
        self,
        session: Session,
        result: ProcessingResult,
        source_path: Path,
        parser_version: str,
        *,
        durable_overrides: Sequence[DurableOverride],
    ) -> None:
        # Reuse the caller's transaction after its same-source replacement. A
        # refusal rolls that replacement (including treatments/offsets) back with
        # these upserts and their import-run record. Do not open another handle.
        self.validate_identity(result, session)

        # Write transactions
        logger.info(f"Writing {len(result.transactions)} transactions to database")
        for txn in result.transactions:
            self._upsert_transaction(session, txn)

        # Write transfers
        logger.info(f"Writing {len(result.transfers)} transfers to database")
        for txn in result.transfers:
            self._upsert_transfer(session, txn)

        reconcile_durable_overrides(
            session,
            durable_overrides,
            target_txn_ids=[txn.txn_id for txn in result.transactions],
        )

        # Record import run
        import_run = ImportRunDB(
            run_at=naive_utc_now(),
            source_path=str(source_path),
            files_processed=result.total_scanned,
            transactions_imported=len(result.transactions),
            transfers_imported=len(result.transfers),
            duplicates_skipped=result.duplicates_removed,
            errors=len(result.exceptions),
            parser_version=parser_version,
        )
        session.add(import_run)

    @staticmethod
    def validate_identity(result: ProcessingResult, session: Session | None = None) -> None:
        """Check a supplied session, or an empty target, without opening a database.

        A read-only preflight may call this before displaying or exporting
        financial results. Writes always repeat it inside their own transaction.
        """
        if result.transactions:
            require_unambiguous_cross_source_identity(
                (transaction_identity(row) for row in result.transactions),
                (transaction_identity(row) for row in session.query(TransactionDB))
                if session is not None
                else (),
                witnesses=result.identity_evidence,
            )
        if result.transfers:
            require_unambiguous_cross_source_identity(
                (transfer_identity(row) for row in result.transfers),
                (transfer_identity(row) for row in session.query(TransferDB))
                if session is not None
                else (),
                witnesses=result.identity_evidence,
            )

    def _upsert_transaction(self, session: Session, txn: Transaction) -> None:
        """Insert or update a transaction."""
        db_txn = TransactionDB(
            txn_id=txn.txn_id,
            posting_date=txn.posting_date,
            transaction_date=txn.transaction_date,
            institution=txn.institution,
            account=txn.account,
            account_type=txn.account_type,
            flow=txn.flow,
            cashflow_type=txn.cashflow_type,
            include_in_cashflow=txn.include_in_cashflow,
            currency=txn.currency,
            amount_raw=txn.amount_raw,
            amount_signed=txn.amount_signed,
            description=txn.description,
            merchant=txn.merchant,
            category=txn.category,
            merchant_confidence=txn.merchant_confidence,
            category_confidence=txn.category_confidence,
            exclude_from_spending=txn.exclude_from_spending,
            merchant_raw=txn.merchant_raw,
            category_raw=txn.category_raw,
            source_file=txn.source_file,
            source_path=txn.source_path,
            source_period=txn.source_period,
            source_row=txn.source_row,
            imported_at=txn.imported_at,
            parser_version=txn.parser_version,
            txn_fingerprint=txn.txn_fingerprint,
            # Analysis fields
            recurring_pattern_id=txn.recurring_pattern_id,
            # The transfer-pairing fields are deliberately NOT copied: they persist
            # on `transfers` only (_upsert_transfer, below). TransactionDB stopped
            # declaring them in migration 20260810_0023.
            # Temporal features
            day_of_week=txn.day_of_week,
            day_of_month=txn.day_of_month,
            week_of_year=txn.week_of_year,
            is_weekend=txn.is_weekend,
            is_month_start=txn.is_month_start,
            is_month_end=txn.is_month_end,
        )

        # Preserve durable categorizations across a re-import. Two confidence levels
        # represent work that must survive the freshly-parsed IMPORT values:
        #   - MANUAL: an explicit user override.
        #   - RULE:   set by a `rules apply` pass (retroactive categorization).
        # Both outrank the IMPORT/default values, so carry the existing columns
        # forward rather than clobbering them. Only IMPORT/default-confidence fields
        # are refreshed from the parsed row. (A later `rules apply` re-applies cleanly
        # either way, but preserving RULE here avoids silently losing categorizations
        # in the window between re-import and the next rules pass.)
        _preserved = (Confidence.MANUAL, Confidence.RULE)
        existing = session.get(TransactionDB, txn.txn_id)
        if existing is not None:
            # A stable id already present in a cloned incremental database is an
            # existing fact, even when a later overlapping statement repeats it.
            # Keep its first-arrival stamp and source coordinates so
            # ``ledger.changes`` cannot restamp old activity as newly added. A
            # rebuild/repair writes into a fresh candidate and therefore has no
            # existing row here; its documented whole-database restamp remains.
            db_txn.imported_at = existing.imported_at
            db_txn.source_file = existing.source_file
            db_txn.source_path = existing.source_path
            db_txn.source_period = existing.source_period
            db_txn.source_row = existing.source_row
        if existing is not None and existing.category_confidence in _preserved:
            db_txn.category = existing.category
            db_txn.category_confidence = existing.category_confidence
            db_txn.exclude_from_spending = existing.exclude_from_spending
        if existing is not None and existing.merchant_confidence in _preserved:
            db_txn.merchant = existing.merchant
            db_txn.merchant_confidence = existing.merchant_confidence

        # Use merge for upsert behavior
        session.merge(db_txn)

    def _upsert_transfer(self, session: Session, txn: Transaction) -> None:
        """Insert or update a transfer."""
        db_transfer = TransferDB(
            txn_id=txn.txn_id,
            posting_date=txn.posting_date,
            institution=txn.institution,
            account=txn.account,
            account_type=txn.account_type,
            flow=txn.flow,
            amount_raw=txn.amount_raw,
            currency=txn.currency,
            description=txn.description,
            source_file=txn.source_file,
            source_path=txn.source_path,
            imported_at=txn.imported_at,
            # Transfer pairing fields
            paired_transfer_id=txn.paired_transfer_id,
            transfer_confidence=txn.transfer_confidence,
            counterparty_account=txn.counterparty_account,
            counterparty_institution=txn.counterparty_institution,
        )

        existing = session.get(TransferDB, txn.txn_id)
        if existing is not None:
            db_transfer.imported_at = existing.imported_at
            db_transfer.source_file = existing.source_file
            db_transfer.source_path = existing.source_path

        session.merge(db_transfer)

    def get_stats(self) -> DatabaseStats:
        """Get database statistics."""
        with session_scope(self.db_path) as session:
            stats = {
                "total_transactions": session.query(func.count(TransactionDB.txn_id)).scalar(),
                "total_transfers": session.query(func.count(TransferDB.txn_id)).scalar(),
                "import_runs": session.query(func.count(ImportRunDB.id)).scalar(),
                "earliest_date": session.query(func.min(TransactionDB.posting_date)).scalar(),
                "latest_date": session.query(func.max(TransactionDB.posting_date)).scalar(),
            }
            return stats
