"""Focused transactions query module."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import asc, desc, func, or_

from ledger.core import Transaction
from ledger.query.base import QueryModule
from ledger.storage.database import (
    TransactionDB,
)
from ledger.storage.mappers import transaction_from_db


class TransactionQueries(QueryModule):
    """Focused query methods owned by the transactions namespace."""

    def get_transactions(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
        categories: list[str] | None = None,
        merchants: list[str] | None = None,
        institutions: list[str] | None = None,
        accounts: list[str] | None = None,
        account_types: list[str] | None = None,
        min_amount: Decimal | None = None,
        max_amount: Decimal | None = None,
        description_search: str | None = None,
        include_in_cashflow: bool | None = None,
        limit: int = 1000,
        offset: int = 0,
        order_by: str = "date_desc",  # date_desc, date_asc, amount_desc, amount_asc
    ) -> list[Transaction]:
        """
        Query transactions with flexible filters.

        Returns list of Transaction Pydantic models.
        """
        with self._open_session() as session:
            query = session.query(TransactionDB)

            # Date filters
            if start_date:
                query = query.filter(TransactionDB.posting_date >= start_date)
            if end_date:
                query = query.filter(TransactionDB.posting_date <= end_date)

            # Category filter
            if categories:
                query = query.filter(TransactionDB.category.in_(categories))

            # Merchant filter
            if merchants:
                query = query.filter(TransactionDB.merchant.in_(merchants))

            # Institution filter
            if institutions:
                query = query.filter(TransactionDB.institution.in_(institutions))

            # Account filter
            if accounts:
                query = query.filter(TransactionDB.account.in_(accounts))

            # Account type filter
            if account_types:
                query = query.filter(TransactionDB.account_type.in_(account_types))

            # Amount filters
            if min_amount is not None:
                query = query.filter(func.abs(TransactionDB.amount_signed) >= min_amount)
            if max_amount is not None:
                query = query.filter(func.abs(TransactionDB.amount_signed) <= max_amount)

            # Description search. Escape LIKE metacharacters so a user searching
            # for a literal "%" or "_" gets substring matches, not wildcards.
            if description_search:
                escaped = (
                    description_search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                )
                pattern = f"%{escaped}%"
                query = query.filter(
                    or_(
                        TransactionDB.description.like(pattern, escape="\\"),
                        TransactionDB.merchant.like(pattern, escape="\\"),
                    )
                )

            # Cashflow filter
            if include_in_cashflow is not None:
                query = query.filter(TransactionDB.include_in_cashflow == include_in_cashflow)

            # Ordering
            if order_by == "date_desc":
                query = query.order_by(desc(TransactionDB.posting_date))
            elif order_by == "date_asc":
                query = query.order_by(asc(TransactionDB.posting_date))
            elif order_by == "amount_desc":
                query = query.order_by(desc(func.abs(TransactionDB.amount_signed)))
            elif order_by == "amount_asc":
                query = query.order_by(asc(func.abs(TransactionDB.amount_signed)))

            # Pagination
            query = query.limit(limit).offset(offset)

            # Execute and convert to Pydantic models
            results = query.all()
            return [transaction_from_db(row) for row in results]

    def get_transaction_by_id(self, txn_id: str) -> dict | None:
        """
        Get a single transaction by ID.

        Args:
            txn_id: Transaction ID

        Returns:
            Transaction as dict or None if not found
        """
        with self._open_session() as session:
            db_txn = session.query(TransactionDB).filter_by(txn_id=txn_id).first()
            if not db_txn:
                return None

            # Convert to dict for insight generator
            return {
                "txn_id": db_txn.txn_id,
                "posting_date": db_txn.posting_date,
                "merchant": db_txn.merchant,
                "category": db_txn.category,
                "amount_signed": db_txn.amount_signed,
                "description": db_txn.description,
                "institution": db_txn.institution,
                "account": db_txn.account,
                "cashflow_type": db_txn.cashflow_type,
                "recurring_pattern_id": db_txn.recurring_pattern_id,
            }

    def get_categories(self) -> list[str]:
        """Get list of all categories."""
        with self._open_session() as session:
            results = (
                session.query(TransactionDB.category)
                .distinct()
                .order_by(TransactionDB.category)
                .all()
            )
            return [row.category for row in results]

    def get_merchants(self) -> list[str]:
        """Get list of all merchants."""
        with self._open_session() as session:
            results = (
                session.query(TransactionDB.merchant)
                .filter(TransactionDB.merchant != "")
                .distinct()
                .order_by(TransactionDB.merchant)
                .all()
            )
            return [row.merchant for row in results]

    def get_institutions(self) -> list[str]:
        """Get list of all institutions."""
        with self._open_session() as session:
            results = (
                session.query(TransactionDB.institution)
                .distinct()
                .order_by(TransactionDB.institution)
                .all()
            )
            return [row.institution for row in results]

    def get_account_types(self) -> list[str]:
        """Get list of all account types."""
        with self._open_session() as session:
            results = (
                session.query(TransactionDB.account_type)
                .filter(TransactionDB.account_type != "")
                .distinct()
                .order_by(TransactionDB.account_type)
                .all()
            )
            return [row.account_type for row in results]

    def get_date_range(self) -> tuple[date | None, date | None]:
        """Get earliest and latest transaction dates."""
        with self._open_session() as session:
            result = session.query(
                func.min(TransactionDB.posting_date),
                func.max(TransactionDB.posting_date),
            ).first()
            if result is None:
                return (None, None)
            return (result[0], result[1])

    def search_descriptions(self, query: str, limit: int = 50) -> list[Transaction]:
        """Full-text search on descriptions and merchants."""
        return self.get_transactions(
            description_search=query,
            limit=limit,
            order_by="date_desc",
        )

    def get_merchant_transactions(
        self,
        merchant: str,
        start_date: date | None = None,
        end_date: date | None = None,
        limit: int = 5000,
    ) -> list[Transaction]:
        """
        Get all transactions for a specific merchant.

        Args:
            merchant: Merchant name to filter by
            start_date: Optional start date filter
            end_date: Optional end date filter
            limit: Maximum number of transactions to return

        Returns:
            List of Transaction objects for the merchant
        """
        return self.get_transactions(
            merchants=[merchant],
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            order_by="date_desc",
        )
