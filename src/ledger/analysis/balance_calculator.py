"""
Account balance calculation with rolling statistics.

Computes daily balances from transaction history.
"""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

from sqlalchemy import or_

from ledger.core import Transaction
from ledger.core.analysis_models import Account, AccountBalance
from ledger.finance_policy import is_cad_row
from ledger.storage.database import AccountBalanceDB, AccountDB, session_scope
from ledger.storage.operations import (
    credit_limit_in_force,
    get_statement_credit_limit_history,
)
from ledger.utils.dates import naive_utc_now
from ledger.utils.money import quantize_cents

# credit_utilization is a PERCENTAGE in a NUMERIC(5, 2) column: three integer digits,
# so 999.99 is the largest figure that can be stored at all.
MAX_CREDIT_UTILIZATION = Decimal("999.99")


def credit_utilization_pct(
    *,
    balance: Decimal,
    balance_basis: str,
    is_credit_card: bool,
    credit_limit: Decimal | None,
) -> Decimal | None:
    """The share of a card's limit currently drawn, as a percentage, or None.

    ONE rule, used by every writer of ``account_balances.credit_utilization``. It
    returns None far more readily than it returns a number, because the column feeds
    a UI that prints whatever it finds: a guessed utilization is worse than an absent
    one, and every refusal below marks an input that cannot support the division.

    Refusals:

    * ``balance_basis != "absolute"`` -- the relative series is seeded at 0 on the
      account's first transaction, so its level is a change-since-then, not a balance.
      Dividing it by a limit produces a plausible percentage of nothing.
    * not a credit card -- a chequing overdraft has no limit to be a percentage of.
    * no ``credit_limit``, or a limit <= 0 -- a zero limit is a detection failure
      rather than a card state (it would make every balance infinitely utilized), and
      the division is undefined. NULL says "unknown", which is the truth. Whether a
      limit is trustworthy ENOUGH to divide by is the caller's call; the persisted
      pass accepts only printed limits (see :meth:`BalanceCalculator._printed_credit_limits`).

    Sign convention is the stored one (see ``AccountBalanceDB.balance``): POSITIVE is
    money HELD, NEGATIVE is money OWED. The numerator is therefore the NEGATED balance,
    not ``abs(balance)`` -- a POSITIVE card balance means the issuer owes the holder
    (an overpayment or refund), nothing is drawn on the limit, and the answer is 0.00.
    ``abs()`` would report a $250 credit as 5% of a $5,000 limit: debt that is not there.

    Over 100% is REPORTED, not capped. Cards go over their limit on fees, FX and
    pre-authorizations, and clamping at 100.00 would make "exactly at the limit" and
    "$1,000 past it" indistinguishable -- deleting the most alarming thing this metric
    can say. The only ceiling is the column's own ``MAX_CREDIT_UTILIZATION``; a figure
    that large is a data error rather than a card state, and it is clamped (as
    debt_ratio and emergency_fund_months are) instead of overflowing NUMERIC(5, 2).
    """
    if balance_basis != "absolute" or not is_credit_card:
        return None
    if credit_limit is None or credit_limit <= 0:
        return None

    amount_owed = -balance if balance < 0 else Decimal("0.00")
    utilization = quantize_cents(amount_owed / credit_limit * 100)
    return min(utilization, MAX_CREDIT_UTILIZATION)


class BalanceCalculator:
    """
    Calculate daily account balances from transactions.

    Computes running balances and rolling statistics (7d/30d averages, min/max).
    """

    def __init__(self, db_path: Path):
        """
        Initialize balance calculator.

        Args:
            db_path: Path to SQLite database
        """
        self.db_path = db_path

    def compute_daily_balances(
        self,
        account_id: str,
        transactions: list[Transaction],
        account: Account | None = None,
        transfer_legs: list[dict] | None = None,
    ) -> list[AccountBalance]:
        """
        Compute daily balances for an account.

        Args:
            account_id: Account identifier
            transactions: List of transactions for this account
            account: Account object (optional, for credit limit lookup)
            transfer_legs: the account's transfer/payment legs (from the transfers table)

        Returns:
            List of AccountBalance objects (one per day)
        """
        if not transactions:
            return []

        transfer_legs = transfer_legs or []
        is_card = bool(account and account.is_credit_card)

        # Daily balance deltas. Transactions contribute amount_signed. The account's
        # transfer/payment legs live in a SEPARATE table and must be folded in, or the
        # running balance is meaningless: outbound transfers look like the money stayed,
        # and card payments never reduce the debt. On a credit card every transfer leg is
        # a debt-reducing payment regardless of the issuer's sign convention (use +abs);
        # on a deposit account the leg carries its natural signed direction.
        sorted_txns = sorted(transactions, key=lambda t: t.posting_date)
        deltas: dict[date, Decimal] = defaultdict(Decimal)
        txn_counts: dict[date, int] = defaultdict(int)
        for txn in sorted_txns:
            # Non-CAD rows are stored without FX conversion, so a USD figure must
            # not shift the CAD balance series. Treat CAD / NULL / "" as CAD
            # (NULL/"" are backward-compat for rows that predate currency
            # tracking; the model default is "CAD"). Skip genuinely foreign rows
            # from both the delta and the activity count -- mirrors the public CAD
            # finance policy the money aggregates share.
            if not is_cad_row(currency=txn.currency):
                continue
            deltas[txn.posting_date] += txn.amount_signed
            txn_counts[txn.posting_date] += 1
        for leg in transfer_legs:
            # A transfer leg is stored in its native currency without FX conversion,
            # exactly like a transaction; a non-CAD leg must not shift the CAD balance
            # series at par. Missing/blank currency is legacy CAD by policy.
            if not is_cad_row(currency=leg.get("currency")):
                continue
            raw = Decimal(str(leg["amount_raw"]))
            deltas[leg["posting_date"]] += abs(raw) if is_card else raw
            # A transfer/payment leg is real account activity (e.g. a credit-card payment
            # day). Count it toward transaction_count too, or activity on payment days is
            # understated -- a card paid off on a day with no purchases would otherwise
            # report 0 transactions despite the payment moving money.
            txn_counts[leg["posting_date"]] += 1

        balances = []
        # NOTE: this calculator emits a RELATIVE balance series. It receives no
        # statement/opening anchor, so it is seeded at 0 on the first observed date — the
        # series tracks change-since-first-transaction, not the true account balance.
        # Day-over-day deltas are exact, but the absolute level is only meaningful
        # relative to that zero anchor, so no level-derived metric may be attached to
        # these rows -- credit_utilization on a relative row would be a percentage of a
        # change, not of a debt. Statement repair persists separate explicit absolute
        # rows, and apply_credit_utilization is what turns THOSE into percentages.
        running_balance = Decimal("0.00")
        now = naive_utc_now()

        # Date range spans both transactions and transfer legs.
        all_dates = [t.posting_date for t in sorted_txns]
        all_dates += [leg["posting_date"] for leg in transfer_legs]
        start_date = min(all_dates)
        end_date = max(all_dates)

        current_date = start_date
        while current_date <= end_date:
            running_balance += deltas.get(current_date, Decimal("0.00"))

            balance = AccountBalance(
                account_id=account_id,
                balance_date=current_date,
                balance=running_balance,
                balance_basis="relative",
                transaction_count=txn_counts.get(current_date, 0),
                # Placeholder values (will be computed in second pass)
                balance_7d_avg=Decimal("0.00"),
                balance_30d_avg=Decimal("0.00"),
                balance_7d_min=Decimal("0.00"),
                balance_7d_max=Decimal("0.00"),
                credit_utilization=None,
                computed_at=now,
            )
            balances.append(balance)

            current_date += timedelta(days=1)

        # Second pass: compute rolling statistics
        balances = self.compute_rolling_stats(balances)

        # Third pass: compute credit utilization for credit cards
        if account and account.is_credit_card and account.detected_credit_limit:
            balances = self._compute_credit_utilization(balances, account.detected_credit_limit)

        return balances

    def compute_rolling_stats(self, balances: list[AccountBalance]) -> list[AccountBalance]:
        """
        Compute rolling statistics for balances.

        Updates balance_7d_avg, balance_30d_avg, balance_7d_min, balance_7d_max.

        Args:
            balances: List of AccountBalance objects (sorted by date)

        Returns:
            Updated list of AccountBalance objects
        """
        updated_balances = []

        for i, balance in enumerate(balances):
            # Get 7-day and 30-day windows
            window_7d = self._get_window(balances, i, days=7)
            window_30d = self._get_window(balances, i, days=30)

            # Compute statistics
            balance_7d_avg = self._compute_avg(window_7d)
            balance_30d_avg = self._compute_avg(window_30d)
            balance_7d_min = self._compute_min(window_7d)
            balance_7d_max = self._compute_max(window_7d)

            # Create updated balance (Pydantic models are immutable, so create new one)
            updated_balance = AccountBalance(
                account_id=balance.account_id,
                balance_date=balance.balance_date,
                balance=balance.balance,
                balance_basis=balance.balance_basis,
                transaction_count=balance.transaction_count,
                balance_7d_avg=balance_7d_avg,
                balance_30d_avg=balance_30d_avg,
                balance_7d_min=balance_7d_min,
                balance_7d_max=balance_7d_max,
                credit_utilization=balance.credit_utilization,
                computed_at=balance.computed_at,
            )
            updated_balances.append(updated_balance)

        return updated_balances

    def _get_window(
        self, balances: list[AccountBalance], index: int, days: int
    ) -> list[AccountBalance]:
        """
        Get rolling window of balances.

        Returns balances from [index - days + 1, index] (inclusive).
        """
        start_index = max(0, index - days + 1)
        return balances[start_index : index + 1]

    def _compute_avg(self, balances: list[AccountBalance]) -> Decimal:
        """Compute average balance, quantized to cents.

        A rolling average of cent-quantized balances is generally NOT itself a
        cent-multiple (e.g. (10.00 + 20.00 + 15.00) / 3 = 15.0000...), so quantize to
        cents to keep the in-memory cents invariant (the DB column would otherwise be
        the only thing rounding it)."""
        if not balances:
            return Decimal("0.00")
        total = sum((b.balance for b in balances), Decimal("0.00"))
        return quantize_cents(total / len(balances))

    def _compute_min(self, balances: list[AccountBalance]) -> Decimal:
        """Compute minimum balance."""
        if not balances:
            return Decimal("0.00")
        return min(b.balance for b in balances)

    def _compute_max(self, balances: list[AccountBalance]) -> Decimal:
        """Compute maximum balance."""
        if not balances:
            return Decimal("0.00")
        return max(b.balance for b in balances)

    def _compute_credit_utilization(
        self, balances: list[AccountBalance], credit_limit: Decimal
    ) -> list[AccountBalance]:
        """
        Attach credit utilization to in-memory card balances.

        Delegates the whole decision to :func:`credit_utilization_pct`, which is also
        what the persisted pass uses -- one rule, so an in-memory row and a stored row
        can never disagree about the same card.

        In practice this only ever clears the field: the rows this calculator produces
        are RELATIVE (see compute_daily_balances), and a relative level is not a
        balance. Real percentages come from :meth:`apply_credit_utilization`, which
        reads the ABSOLUTE anchors statement repair writes -- and which also enforces
        the denominator's provenance (see :meth:`_printed_credit_limits`); this method
        divides by whatever limit its caller hands it.

        Args:
            balances: List of AccountBalance objects (all for one credit card)
            credit_limit: Credit limit

        Returns:
            Updated list with credit_utilization computed
        """
        updated_balances = []

        for balance in balances:
            utilization = credit_utilization_pct(
                balance=balance.balance,
                balance_basis=balance.balance_basis,
                is_credit_card=True,  # only called for cards
                credit_limit=credit_limit,
            )

            # Create updated balance
            updated_balance = AccountBalance(
                account_id=balance.account_id,
                balance_date=balance.balance_date,
                balance=balance.balance,
                balance_basis=balance.balance_basis,
                transaction_count=balance.transaction_count,
                balance_7d_avg=balance.balance_7d_avg,
                balance_30d_avg=balance.balance_30d_avg,
                balance_7d_min=balance.balance_7d_min,
                balance_7d_max=balance.balance_7d_max,
                credit_utilization=utilization,
                computed_at=balance.computed_at,
            )
            updated_balances.append(updated_balance)

        return updated_balances

    def _printed_credit_limits(self) -> dict[str, list[tuple[date, Decimal]]]:
        """Per-card denominators, taken only from limits an issuer actually PRINTED.

        ``AccountDB.detected_credit_limit`` is not proof of a known limit. It holds the
        printed figure when a statement supplied one, and otherwise AccountDetector's
        fallback estimate: the deepest balance the card ever reached, rounded up to the
        nearest $100 (see ``_detect_credit_limit``). That estimate is a LOWER BOUND, so
        a utilization derived from it necessarily reads ~100% at the card's deepest
        point no matter how much headroom really remained -- a guess with a percent sign
        on it, and the column exists to hold real figures or nothing.

        Reading the statement-limits table directly (rather than the accounts row) also
        means a printed limit increase applies as soon as the statement is imported,
        without waiting for account detection to refresh the dimension.
        """
        with session_scope(self.db_path) as session:
            cards = [
                (str(row.account_id), str(row.account_number))
                for row in session.query(AccountDB).filter(AccountDB.is_credit_card.is_(True))
            ]

        # Looked up outside the session above and before the write session below: this
        # helper opens its own connection, and nesting one inside an open write
        # transaction on the same SQLite file invites a lock.
        limits: dict[str, list[tuple[date, Decimal]]] = {}
        for account_id, account_number in cards:
            history = [
                (statement_date, Decimal(str(limit)))
                for statement_date, limit in get_statement_credit_limit_history(
                    self.db_path, account_number
                )
                if Decimal(str(limit)) > 0
            ]
            if history:
                limits[account_id] = history
        return limits

    def apply_credit_utilization(self) -> dict[str, int]:
        """Derive ``credit_utilization`` for every stored balance row, and only then.

        The column had never held a value: this calculator only ever emits RELATIVE
        rows (which cannot support a percentage), and the ABSOLUTE anchors come from
        statement repair, which writes them with no utilization of its own. This pass
        closes that gap from the analysis side, so any analysis run picks up whatever
        anchors currently exist instead of waiting for the next repair.

        It is a full reconciliation, not a backfill: rows that no longer qualify are
        cleared. A percentage left behind on a row whose account lost its printed
        limit -- or on a relative row -- is exactly the plausible-but-wrong figure
        :func:`credit_utilization_pct` exists to refuse. Re-running changes nothing,
        so it is safe to call after every balance recompute.

        Returns:
            ``{"populated": n, "cleared": n}`` -- rows given a percentage, and rows
            whose stale percentage was removed. Both count CHANGES, so a second run
            reports zeros.
        """
        limits = self._printed_credit_limits()

        with session_scope(self.db_path) as session:
            # Clear every row that must not carry a percentage. Cheaper than loading
            # the full series (one row per account per day) just to prove it is
            # already NULL, and it is what makes the pass a reconciliation.
            cleared = (
                session.query(AccountBalanceDB)
                .filter(
                    AccountBalanceDB.credit_utilization.is_not(None),
                    or_(
                        AccountBalanceDB.balance_basis != "absolute",
                        AccountBalanceDB.account_id.not_in(limits.keys()),
                    ),
                )
                .update({AccountBalanceDB.credit_utilization: None}, synchronize_session=False)
            )

            populated = 0
            if limits:
                rows = session.query(AccountBalanceDB).filter(
                    AccountBalanceDB.account_id.in_(limits.keys()),
                    AccountBalanceDB.balance_basis == "absolute",
                )
                for row in rows:
                    # The limit in force on THIS row's date, not the newest one.
                    # Every card here changed limit at least once, and one was
                    # cut, so a single figure applied across the series is a
                    # different number on a fifth of the rows.
                    in_force = credit_limit_in_force(limits[str(row.account_id)], row.balance_date)
                    utilization = credit_utilization_pct(
                        balance=Decimal(str(row.balance)),
                        balance_basis=str(row.balance_basis),
                        is_credit_card=True,
                        credit_limit=in_force,
                    )
                    current = (
                        None
                        if row.credit_utilization is None
                        else Decimal(str(row.credit_utilization))
                    )
                    if current == utilization:
                        continue
                    row.credit_utilization = utilization
                    if utilization is None:
                        cleared += 1
                    else:
                        populated += 1

        return {"populated": populated, "cleared": int(cleared)}

    def save_balances(
        self,
        balances: list[AccountBalance],
        *,
        replacement_account_ids: set[str],
        replacement_basis: Literal["relative", "absolute"],
    ) -> int:
        """
        Save balances to database.

        Replace the complete balance history for an explicit account scope.

        The scope is independent from ``balances`` so an empty computation still
        removes stale rows for the account instead of silently becoming a no-op.

        Args:
            balances: Complete replacement AccountBalance rows for the scope
            replacement_account_ids: Account IDs whose prior rows must be replaced
            replacement_basis: Basis owned by this replacement operation

        Returns:
            Number of balances saved
        """
        account_ids = {str(account_id) for account_id in replacement_account_ids}
        balance_account_ids = {balance.account_id for balance in balances}
        if not balance_account_ids <= account_ids:
            outside_scope = sorted(balance_account_ids - account_ids)
            raise ValueError(f"Balance rows fall outside replacement scope: {outside_scope}")
        wrong_basis = [
            balance.balance_basis
            for balance in balances
            if balance.balance_basis != replacement_basis
        ]
        if wrong_basis:
            raise ValueError(
                f"Balance rows fall outside {replacement_basis!r} replacement basis: {wrong_basis}"
            )
        if not account_ids:
            return 0

        with session_scope(self.db_path) as session:
            # Delete all existing rows for the affected accounts in one pass.
            session.query(AccountBalanceDB).filter(
                AccountBalanceDB.account_id.in_(account_ids),
                AccountBalanceDB.balance_basis == replacement_basis,
            ).delete(synchronize_session=False)

            # Bulk-insert all new balance rows.
            db_balances = [
                AccountBalanceDB(
                    account_id=balance.account_id,
                    balance_date=balance.balance_date,
                    balance=balance.balance,
                    balance_basis=balance.balance_basis,
                    transaction_count=balance.transaction_count,
                    balance_7d_avg=balance.balance_7d_avg,
                    balance_30d_avg=balance.balance_30d_avg,
                    balance_7d_min=balance.balance_7d_min,
                    balance_7d_max=balance.balance_7d_max,
                    credit_utilization=balance.credit_utilization,
                    computed_at=balance.computed_at,
                )
                for balance in balances
            ]
            session.bulk_save_objects(db_balances)

        return len(balances)

    def compute_balances_for_account(self, account: Account) -> list[AccountBalance]:
        """
        Compute balances for a specific account.

        Fetches transactions from database and computes daily balances.

        Args:
            account: Account object

        Returns:
            List of AccountBalance objects
        """
        # Fetch transactions for this account
        from ledger.storage.mappers import transaction_from_db

        # Get all transactions for this account
        with session_scope(self.db_path) as session:
            from ledger.storage.database import TransactionDB, TransferDB

            db_txns = (
                session.query(TransactionDB)
                .filter_by(institution=account.institution, account=account.account_number)
                .all()
            )

            # Convert to Transaction Pydantic models
            txns = [transaction_from_db(db_txn) for db_txn in db_txns]

            # The account's transfer/payment legs live in the transfers table; fold them
            # into the balance series so it reflects the real running balance.
            transfer_legs = [
                {
                    "posting_date": row.posting_date,
                    "amount_raw": row.amount_raw,
                    "currency": row.currency,
                }
                for row in session.query(TransferDB).filter_by(
                    institution=account.institution, account=account.account_number
                )
            ]

        # Compute balances
        balances = self.compute_daily_balances(account.account_id, txns, account, transfer_legs)

        return balances

    def compute_all_balances(self) -> dict[str, int]:
        """
        Compute balances for all accounts in the database.

        Loads all transactions and transfer legs in a single query each, groups
        them by (institution, account), then computes balances per account
        without re-querying the database per account.  Results are identical to
        calling compute_balances_for_account per account but require only two
        table scans instead of 2 x n_accounts scans.

        Returns:
            Dict with account_id -> balance_count mapping
        """
        from collections import defaultdict

        from ledger.storage.database import TransactionDB, TransferDB
        from ledger.storage.mappers import transaction_from_db

        results = {}

        # Single pass: load all accounts, all transactions, all transfer legs.
        with session_scope(self.db_path) as session:
            db_accounts = session.query(AccountDB).all()

            # Convert AccountDB rows to Account Pydantic models while session is open.
            accounts = [
                Account(
                    account_id=db_acct.account_id,
                    institution=db_acct.institution,
                    account_number=db_acct.account_number,
                    account_type=db_acct.account_type,
                    account_subtype=db_acct.account_subtype,
                    first_transaction_date=db_acct.first_transaction_date,
                    last_transaction_date=db_acct.last_transaction_date,
                    is_active=db_acct.is_active,
                    transaction_count=db_acct.transaction_count,
                    avg_monthly_transactions=db_acct.avg_monthly_transactions,
                    is_credit_card=db_acct.is_credit_card,
                    detected_credit_limit=db_acct.detected_credit_limit,
                    created_at=db_acct.created_at,
                    updated_at=db_acct.updated_at,
                )
                for db_acct in db_accounts
            ]

            # Group all transactions by (institution, account) key.
            txns_by_acct: dict[tuple[str, str], list] = defaultdict(list)
            for db_txn in session.query(TransactionDB).all():
                txns_by_acct[(db_txn.institution, db_txn.account)].append(
                    transaction_from_db(db_txn)
                )

            # Group all transfer legs by (institution, account) key.
            legs_by_acct: dict[tuple[str, str], list[dict]] = defaultdict(list)
            for row in session.query(TransferDB).all():
                legs_by_acct[(row.institution, row.account)].append(
                    {
                        "posting_date": row.posting_date,
                        "amount_raw": row.amount_raw,
                        "currency": row.currency,
                    }
                )

        # Compute and save balances per account using pre-loaded data.
        for account in accounts:
            key = (account.institution, account.account_number)
            txns = txns_by_acct.get(key, [])
            transfer_legs = legs_by_acct.get(key, [])

            balances = self.compute_daily_balances(account.account_id, txns, account, transfer_legs)
            self.save_balances(
                balances,
                replacement_account_ids={account.account_id},
                replacement_basis="relative",
            )
            results[account.account_id] = len(balances)

        return results
