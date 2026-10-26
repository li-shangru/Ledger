"""Focused accounts query module."""

from __future__ import annotations

from datetime import date
from typing import Literal

from ledger.query.base import QueryModule
from ledger.storage.database import (
    AccountBalanceDB,
    AccountDB,
)


class AccountQueries(QueryModule):
    """Focused query methods owned by the accounts namespace."""

    def get_account_balances(
        self,
        account_id: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        balance_basis: Literal["relative", "absolute"] | None = None,
    ) -> list[dict]:
        """
        Get account balance snapshots from AccountBalanceDB.

        Args:
            account_id: Optional account ID filter
            start_date: Optional start date filter
            end_date: Optional end date filter

        Returns:
            List of balance records with all fields including rolling stats
        """
        with self._open_session() as session:
            query = session.query(AccountBalanceDB)

            if account_id:
                query = query.filter(AccountBalanceDB.account_id == account_id)
            if start_date:
                query = query.filter(AccountBalanceDB.balance_date >= start_date)
            if end_date:
                query = query.filter(AccountBalanceDB.balance_date <= end_date)
            if balance_basis:
                query = query.filter(AccountBalanceDB.balance_basis == balance_basis)

            query = query.order_by(AccountBalanceDB.balance_date)

            results = []
            for record in query:
                results.append(
                    {
                        "account_id": record.account_id,
                        "balance_date": record.balance_date,
                        "balance": record.balance,
                        "balance_basis": record.balance_basis,
                        "transaction_count": record.transaction_count,
                        "balance_7d_avg": record.balance_7d_avg,
                        "balance_30d_avg": record.balance_30d_avg,
                        "balance_7d_min": record.balance_7d_min,
                        "balance_7d_max": record.balance_7d_max,
                        "credit_utilization": record.credit_utilization,
                    }
                )

            return results

    def get_latest_account_balance(
        self,
        account_id: str,
        end_date: date | None = None,
        *,
        balance_basis: Literal["relative", "absolute"],
    ) -> dict | None:
        """Return the most recent balance row for an account on/before ``end_date``.

        Avoids materializing the whole per-day AccountBalanceDB series (one row per
        day since the account's first transaction) just to read the final balance.
        """
        with self._open_session() as session:
            query = session.query(AccountBalanceDB).filter(
                AccountBalanceDB.account_id == account_id
            )
            if end_date:
                query = query.filter(AccountBalanceDB.balance_date <= end_date)
            query = query.filter(AccountBalanceDB.balance_basis == balance_basis)
            record = query.order_by(AccountBalanceDB.balance_date.desc()).first()
            if record is None:
                return None
            return {
                "account_id": record.account_id,
                "balance_date": record.balance_date,
                "balance": record.balance,
                "balance_basis": record.balance_basis,
                "credit_utilization": record.credit_utilization,
            }

    def get_accounts(
        self,
        is_active: bool | None = None,
    ) -> list[dict]:
        """
        Get account metadata from AccountDB.

        Args:
            is_active: Optional filter for active accounts only

        Returns:
            List of account dicts with all metadata
        """
        with self._open_session() as session:
            query = session.query(AccountDB)

            if is_active is not None:
                query = query.filter(AccountDB.is_active == is_active)

            query = query.order_by(AccountDB.institution, AccountDB.account_number)

            results = []
            for record in query:
                results.append(
                    {
                        "account_id": record.account_id,
                        "institution": record.institution,
                        "account_number": record.account_number,
                        "account_type": record.account_type,
                        "account_subtype": record.account_subtype,
                        "first_transaction_date": record.first_transaction_date,
                        "last_transaction_date": record.last_transaction_date,
                        "transaction_count": record.transaction_count,
                        "avg_monthly_transactions": record.avg_monthly_transactions,
                        "is_active": record.is_active,
                        "is_credit_card": record.is_credit_card,
                        "detected_credit_limit": record.detected_credit_limit,
                    }
                )

            return results
