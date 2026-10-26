"""
Account detection from transaction data.

Automatically detects accounts from transactions and computes statistics.
"""

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from ledger.core import Transaction
from ledger.core.account_identity import account_identity
from ledger.core.analysis_models import Account
from ledger.finance_policy import is_cad_row
from ledger.storage.database import (
    AccountBalanceDB,
    AccountDB,
    RecurringPatternDB,
    session_scope,
)
from ledger.storage.operations import get_latest_statement_credit_limit
from ledger.utils.dates import naive_utc_now


class AccountDetector:
    """
    Auto-detect accounts from transaction data.

    Groups transactions by (institution, account) and computes account-level statistics.
    """

    def __init__(self, db_path: Path):
        """
        Initialize account detector.

        Args:
            db_path: Path to SQLite database
        """
        self.db_path = db_path

    def detect_accounts(self, transactions: list[Transaction]) -> list[Account]:
        """
        Detect accounts from a list of transactions.

        Args:
            transactions: List of Transaction objects

        Returns:
            List of Account objects
        """
        # Group transactions by account
        accounts_data = defaultdict(list)

        for txn in transactions:
            key = (txn.institution, txn.account)
            accounts_data[key].append(txn)

        # Build Account objects
        accounts = []
        now = naive_utc_now()

        for (institution, account_number), txns in accounts_data.items():
            account = self._build_account(institution, account_number, txns, now)
            accounts.append(account)

        return accounts

    def _build_account(
        self,
        institution: str,
        account_number: str,
        txns: list[Transaction],
        now: datetime,
        transfer_legs: list[dict] | None = None,
    ) -> Account:
        """Build Account object from transaction list."""
        # Sort by date
        sorted_txns = sorted(txns, key=lambda t: t.posting_date)

        first_date = sorted_txns[0].posting_date
        last_date = sorted_txns[-1].posting_date

        # Check if active (most recent transaction within the last 60 days, inclusive).
        # BASIS: compare DATES, not tz-naive datetimes. posting_date is a calendar date
        # (no time/zone); `now` is a tz-naive UTC datetime, so subtracting it directly and
        # taking .days mixes a UTC instant against a local calendar date and can be off by
        # a day near the boundary (e.g. just after UTC midnight while it is still
        # "yesterday" locally). Reducing `now` to its date first makes both operands plain
        # dates and the day-count a stable, zone-free calendar difference.
        reference_date = now.date()
        is_active = (reference_date - last_date).days <= 60

        # Account type detection
        # Use the most common account_type from transactions
        account_types = [txn.account_type for txn in txns if txn.account_type]
        account_type = (
            max(set(account_types), key=account_types.count) if account_types else "unknown"
        )

        # Determine account subtype (same as account_type for now)
        account_subtype = account_type

        # Detect if credit card
        is_credit_card = self._is_credit_card(account_type, txns)

        # Compute statistics
        transaction_count = len(txns)

        # Average monthly transactions
        total_months = max(1, ((last_date - first_date).days / 30.44))  # Average days per month
        avg_monthly_transactions = Decimal(str(transaction_count / total_months))

        # Detect credit limit for credit cards. printed_limit is the SEAM (decision 3):
        # a statement's printed credit limit wins over the deepest-balance estimate.
        # The slug persisted at finalize is the account_number (== TransactionDB.account
        # == the CSV stem), so look up the latest stored printed limit for it. Falls back
        # to None (the estimate) when no statement limit has been persisted.
        detected_credit_limit = None
        if is_credit_card:
            printed_limit = get_latest_statement_credit_limit(self.db_path, account_number)
            detected_credit_limit = self._detect_credit_limit(
                txns, transfer_legs, printed_limit=printed_limit
            )

        # Generate account ID
        account_id = account_identity(institution, account_number)

        return Account(
            account_id=account_id,
            institution=institution,
            account_number=account_number,
            account_type=self._normalize_account_type(account_type),
            account_subtype=account_subtype,
            first_transaction_date=first_date,
            last_transaction_date=last_date,
            is_active=is_active,
            transaction_count=transaction_count,
            avg_monthly_transactions=avg_monthly_transactions,
            is_credit_card=is_credit_card,
            detected_credit_limit=detected_credit_limit,
            created_at=now,
            updated_at=now,
        )

    def _is_credit_card(self, account_type: str, txns: list[Transaction]) -> bool:
        """
        Detect if account is a credit card.

        Uses account_type keywords and transaction patterns.
        """
        account_type_lower = account_type.lower()

        # Clear deposit accounts are never credit cards, regardless of balance sign or
        # purchase ratio -- prevents a net-negative chequing slice from being misflagged.
        deposit_keywords = ("chequing", "checking", "savings", "deposit")
        if any(keyword in account_type_lower for keyword in deposit_keywords):
            return False

        # Check account_type for credit card keywords
        credit_card_keywords = ["visa", "mastercard", "credit card", "credit"]
        if any(keyword in account_type_lower for keyword in credit_card_keywords):
            return True

        # Check transaction patterns:
        # Credit cards typically have negative balance (debt)
        # and many purchase transactions. Non-CAD rows are not FX-converted, so a
        # foreign net must not flip the CAD purchase/payment balance; judge on the
        # CAD-equivalent rows (falling back to all rows for an all-foreign card).
        cad_txns = [txn for txn in txns if is_cad_row(currency=txn.currency)] or txns
        total_amount = sum(txn.amount_signed for txn in cad_txns)
        purchase_count = sum(1 for txn in cad_txns if txn.flow in ["purchase", "expense"])

        # If average balance is negative and >50% purchases, likely credit card
        return total_amount < 0 and purchase_count / len(cad_txns) > 0.5

    def _detect_credit_limit(
        self,
        txns: list[Transaction],
        transfer_legs: list[dict] | None = None,
        printed_limit: Decimal | None = None,
    ) -> Decimal | None:
        """Estimate the credit limit as the highest balance ever owed.

        SEAM (decision 3): when a statement's PRINTED credit limit is supplied it wins
        outright -- the printed figure is authoritative, the balance estimate is only a
        lower-bound fallback. ``printed_limit`` is supplied by ``_build_account`` via
        ``get_latest_statement_credit_limit`` (persisted to the
        ``account_statement_credit_limits`` table by the import post-processing), so the
        printed figure flows end-to-end and wins over the balance estimate below.

        Otherwise estimate: purchases (negative) drive the balance down; the card's
        payment legs -- which live in the transfers table, not the transactions table --
        pay it back up. We fold those payments in (as debt-reducing positives) so the
        running balance is the REAL card balance, not a monotonic cumulative-purchase
        total that massively overstates the limit. The deepest balance owed, rounded up
        to the nearest $100, is a lower bound on the limit.
        """
        if printed_limit is not None:
            return printed_limit
        # Non-CAD rows are not FX-converted; including a USD figure at par would
        # massively distort the deepest-balance-owed estimate (and the derived
        # credit_utilization). Build the running balance from CAD-equivalent rows only.
        events: list[tuple[date, Decimal]] = [
            (t.posting_date, t.amount_signed) for t in txns if is_cad_row(currency=t.currency)
        ]
        for leg in transfer_legs or []:
            # A non-CAD payment leg is not FX-converted; folding it at par would
            # distort the deepest-balance estimate. Skip it (missing/blank == CAD).
            if not is_cad_row(currency=leg.get("currency")):
                continue
            # A transfer leg on a card is a payment -> reduces debt regardless of the
            # issuer's sign convention, so apply its magnitude as a positive.
            events.append((leg["posting_date"], abs(Decimal(str(leg["amount_raw"])))))
        events.sort(key=lambda event: event[0])

        running_balance = Decimal("0.00")
        max_negative_balance = Decimal("0.00")
        for _, delta in events:
            running_balance += delta
            if running_balance < max_negative_balance:
                max_negative_balance = running_balance

        if max_negative_balance < 0:
            limit = abs(max_negative_balance)
            return (limit / 100).to_integral_value(rounding="ROUND_UP") * 100
        return None

    def _normalize_account_type(self, account_type: str) -> str:
        """
        Normalize account type to standard categories.

        Returns: "checking", "credit_card", "savings", "investment", or original
        """
        account_type_lower = account_type.lower()

        # Credit cards
        if any(
            keyword in account_type_lower
            for keyword in ["visa", "mastercard", "credit card", "credit"]
        ):
            return "credit_card"

        # Checking accounts
        if any(keyword in account_type_lower for keyword in ["chequing", "checking"]):
            return "checking"

        # Savings accounts
        if any(keyword in account_type_lower for keyword in ["savings", "tfsa", "rrsp", "save"]):
            return "savings"

        # Investment accounts
        if any(keyword in account_type_lower for keyword in ["investment", "invest", "brokerage"]):
            return "investment"

        return account_type

    def save_accounts(self, accounts: list[Account], *, replace_all: bool = False) -> int:
        """
        Save accounts to database.

        Uses upsert for targeted updates. ``replace_all=True`` makes a full account
        detection run replacement-complete: absent derived account and balance rows
        are retired while user-authored savings goals remain untouched.

        Args:
            accounts: List of Account objects
            replace_all: Replace the complete derived account set when true

        Returns:
            Number of accounts saved
        """
        with session_scope(self.db_path) as session:
            if replace_all:
                old_account_ids = {
                    str(account_id) for (account_id,) in session.query(AccountDB.account_id).all()
                }
                old_balance_account_ids = {
                    str(account_id)
                    for (account_id,) in session.query(AccountBalanceDB.account_id).distinct().all()
                }
                new_account_ids = {account.account_id for account in accounts}
                absent_account_ids = old_account_ids - new_account_ids
                absent_balance_account_ids = (
                    old_account_ids | old_balance_account_ids
                ) - new_account_ids
                if absent_balance_account_ids:
                    session.query(AccountBalanceDB).filter(
                        AccountBalanceDB.account_id.in_(absent_balance_account_ids)
                    ).delete(synchronize_session=False)
                    # Recurring patterns are derived per account exactly as balances are.
                    # A retired account must not leave one dangling: the pattern would
                    # keep a now-unresolvable account_id, and once accounts is a
                    # foreign-key parent the delete below would fail outright.
                    session.query(RecurringPatternDB).filter(
                        RecurringPatternDB.account_id.in_(absent_balance_account_ids)
                    ).delete(synchronize_session=False)
                if absent_account_ids:
                    session.query(AccountDB).filter(
                        AccountDB.account_id.in_(absent_account_ids)
                    ).delete(synchronize_session=False)
            for account in accounts:
                # Convert to database model
                db_account = AccountDB(
                    account_id=account.account_id,
                    institution=account.institution,
                    account_number=account.account_number,
                    account_type=account.account_type,
                    account_subtype=account.account_subtype,
                    first_transaction_date=account.first_transaction_date,
                    last_transaction_date=account.last_transaction_date,
                    is_active=account.is_active,
                    transaction_count=account.transaction_count,
                    avg_monthly_transactions=account.avg_monthly_transactions,
                    is_credit_card=account.is_credit_card,
                    detected_credit_limit=account.detected_credit_limit,
                    created_at=account.created_at,
                    updated_at=account.updated_at,
                )
                session.merge(db_account)  # Upsert

        return len(accounts)

    def update_account_stats(self, account_id: str) -> Account | None:
        """
        Recompute account statistics from current transaction data.

        Fetches all transactions for this account and recomputes stats.

        Args:
            account_id: Account identifier

        Returns:
            Updated Account object, or None if account not found
        """
        # Fetch transactions for this account from database
        from ledger.storage.mappers import transaction_from_db

        # Get all transactions (simplified - in production, filter by account_id)
        # For now, we'll need to filter in Python
        with session_scope(self.db_path) as session:
            from ledger.storage.database import TransactionDB

            # Get existing account to extract institution + account_number
            existing = session.query(AccountDB).filter_by(account_id=account_id).first()

            if not existing:
                return None

            # Capture identity fields while the instance is still session-bound; the
            # account is rebuilt after the session closes, which expires the instance.
            institution = existing.institution
            account_number = existing.account_number

            # Fetch all transactions for this account
            db_txns = (
                session.query(TransactionDB)
                .filter_by(institution=institution, account=account_number)
                .all()
            )

            # Convert to Transaction Pydantic models
            txns = [transaction_from_db(db_txn) for db_txn in db_txns]

            # The card's payment legs live in the transfers table; fold them into the
            # credit-limit balance so it reflects real (paid-down) balances, not just
            # cumulative purchases.
            from ledger.storage.database import TransferDB

            transfer_legs = [
                {
                    "posting_date": row.posting_date,
                    "amount_raw": row.amount_raw,
                    "currency": row.currency,
                }
                for row in session.query(TransferDB).filter_by(
                    institution=institution, account=account_number
                )
            ]

        # Rebuild account
        if not txns:
            return None

        now = naive_utc_now()
        account = self._build_account(institution, account_number, txns, now, transfer_legs)

        # Save updated account
        self.save_accounts([account])

        return account

    def update_account_stats_with_data(
        self,
        account: Account,
        txns: list[Transaction],
        transfer_legs: list[dict],
    ) -> Account | None:
        """Fast-path variant of update_account_stats that skips DB queries.

        Called by run_account_analysis after it has already loaded all
        transactions and transfer legs grouped by account.  Identical output to
        update_account_stats — the rebuild + save logic is the same.

        Args:
            account: The Account object (identity fields already known).
            txns: Pre-loaded transactions for this account.
            transfer_legs: Pre-loaded transfer/payment legs for this account.

        Returns:
            Updated Account object, or None if txns is empty.
        """
        if not txns:
            return None

        now = naive_utc_now()
        updated = self._build_account(
            account.institution, account.account_number, txns, now, transfer_legs
        )
        self.save_accounts([updated])
        return updated
