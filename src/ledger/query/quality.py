"""Focused data quality query module."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from ledger.core import Transaction
from ledger.finance_policy import is_cad_row
from ledger.query.base import QueryModule
from ledger.storage.database import TransactionDB
from ledger.storage.mappers import transaction_from_db


class QualityQueries(QueryModule):
    """Quality query namespace used by the compatibility facade."""

    def get_duplicate_candidates(
        self,
        threshold_days: int = 2,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[tuple[Transaction, Transaction]]:
        """Find transactions with same merchant/amount within a small date window."""
        with self._open_session() as session:
            # merchant is nullable=False default="" so isnot(None) never excludes a row;
            # unresolved-merchant rows (merchant=="") would otherwise sort together and
            # produce false duplicate pairs. Only compare rows with a real merchant.
            query = session.query(TransactionDB).filter(TransactionDB.merchant != "")
            if start_date:
                query = query.filter(TransactionDB.posting_date >= start_date)
            if end_date:
                query = query.filter(TransactionDB.posting_date <= end_date)
            transactions = query.order_by(
                TransactionDB.merchant,
                TransactionDB.posting_date,
                TransactionDB.txn_id,
            ).all()

            candidates = []
            for i, first in enumerate(transactions[:-1]):
                for second in transactions[i + 1 :]:
                    if first.merchant != second.merchant:
                        break
                    # Rows are sorted by (merchant, posting_date) so within a
                    # merchant group dates are non-decreasing: once the gap exceeds
                    # threshold_days, all remaining rows are even farther away.
                    # This break is safe: date_diff > threshold_days means the pair
                    # would be rejected by the date_diff <= threshold_days check below
                    # anyway, and every subsequent row is at least as far out.
                    if (second.posting_date - first.posting_date).days > threshold_days:
                        break
                    # Amounts are only comparable within a currency: a USD and a CAD
                    # row of equal magnitude are NOT duplicates (neither is FX-converted).
                    # Treat the legacy untracked-currency values as CAD-equivalent.
                    if not (
                        first.currency == second.currency
                        or (
                            is_cad_row(currency=str(first.currency))
                            and is_cad_row(currency=str(second.currency))
                        )
                    ):
                        continue
                    if abs(first.amount_signed - second.amount_signed) >= Decimal("0.01"):
                        continue
                    date_diff = abs((first.posting_date - second.posting_date).days)
                    if date_diff <= threshold_days:
                        candidates.append(
                            (
                                transaction_from_db(first),
                                transaction_from_db(second),
                            )
                        )
            return candidates[:50]
