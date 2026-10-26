"""
Pattern matching for linking transactions to recurring patterns.

Matches new transactions to existing detected patterns.
"""

from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from ledger.analysis.pattern_detector import next_occurrence
from ledger.core import Transaction
from ledger.core.account_identity import account_identity
from ledger.core.analysis_models import RecurringPattern
from ledger.finance_policy import cad_sql_filters, is_cad_row
from ledger.storage.database import RecurringPatternDB, TransactionDB, session_scope
from ledger.utils.dates import naive_utc_now


class PatternMatcher:
    """
    Match transactions to recurring patterns.

    Links new transactions to existing patterns and updates pattern statistics.
    """

    def __init__(self, db_path: Path):
        """
        Initialize pattern matcher.

        Args:
            db_path: Path to SQLite database
        """
        self.db_path = db_path

    def match_transaction_to_pattern(self, txn: Transaction) -> str | None:
        """
        Find matching pattern for a transaction (standalone entrypoint).

        Opens its own session so callers outside match_all_transactions can use
        this method directly.  For bulk matching, match_all_transactions reuses a
        single session internally and does not call this method.

        Matches based on:
        - Merchant
        - Account
        - Amount (within tolerance)
        - Date proximity to expected date

        Args:
            txn: Transaction to match

        Returns:
            Pattern ID if match found, None otherwise
        """
        if not is_cad_row(currency=txn.currency):
            return None

        account_id = account_identity(txn.institution, txn.account)

        # Fetch all active patterns for this account/merchant
        with session_scope(self.db_path) as session:
            patterns = (
                session.query(RecurringPatternDB)
                .filter_by(
                    account_id=account_id,
                    merchant=txn.merchant,
                    is_active=True,
                )
                .all()
            )

            if not patterns:
                return None

            # Find best match
            best_match = None
            best_score = 0.0

            for pattern in patterns:
                score = self._compute_match_score(txn, pattern)

                if score > best_score and score >= 0.7:  # Require 70% match
                    best_score = score
                    best_match = pattern

            if best_match:
                # Update pattern statistics
                self._update_pattern_with_transaction(best_match, txn)
                return best_match.pattern_id

            return None

    def _compute_match_score(self, txn: Transaction, pattern: RecurringPatternDB) -> float:
        """
        Compute match score between transaction and pattern.

        Returns score between 0.0 and 1.0.
        """
        score = 0.0

        # Merchant match (required)
        if txn.merchant != pattern.merchant:
            return 0.0

        score += 0.3  # 30% for merchant match

        # Amount match (within 2 std deviations, or 5% for fixed amounts)
        amount_diff = abs(abs(txn.amount_signed) - pattern.expected_amount)
        amount_tolerance = max(
            pattern.amount_std_dev * 2, pattern.expected_amount * Decimal("0.05")
        )

        if amount_diff <= amount_tolerance:
            # Score based on closeness
            if amount_tolerance > 0:
                amount_score = 1.0 - (float(amount_diff) / float(amount_tolerance))
            else:
                # Perfect match (both 0)
                amount_score = 1.0
            score += 0.4 * amount_score  # 40% for amount match
        else:
            return 0.0  # Amount too far off

        # Date proximity (how close to expected date?)
        days_from_expected = abs((txn.posting_date - pattern.next_expected_date).days)

        # Get typical period length
        if pattern.frequency == "weekly":
            period_days = 7
        elif pattern.frequency == "biweekly":
            period_days = 14
        elif pattern.frequency == "monthly":
            period_days = 30
        elif pattern.frequency == "quarterly":
            period_days = 90
        elif pattern.frequency == "annual":
            period_days = 365
        else:
            period_days = 30

        # Allow ±50% of period
        max_days = period_days * 0.5

        if days_from_expected <= max_days:
            date_score = 1.0 - (days_from_expected / max_days)
            score += 0.3 * date_score  # 30% for date proximity
        else:
            # Too far from expected date
            score += 0.0

        return score

    def _update_pattern_with_transaction(
        self, pattern: RecurringPatternDB, txn: Transaction
    ) -> None:
        """
        Update pattern statistics with new transaction (standalone path).

        Opens its own session — used by the standalone match_transaction_to_pattern
        entrypoint.  The bulk path (match_all_transactions) applies the same math
        directly on the already-fetched ORM row inside the shared session; it does
        NOT call this method.

        Updates:
        - Last occurrence date
        - Occurrence count
        - Next expected date
        - Amount statistics (running average)
        - Active status
        """
        with session_scope(self.db_path) as session:
            # Refresh pattern from database
            db_pattern = (
                session.query(RecurringPatternDB).filter_by(pattern_id=pattern.pattern_id).first()
            )

            if not db_pattern:
                return

            self._apply_pattern_stats_update(db_pattern, txn)

    def _apply_pattern_stats_update(self, db_pattern: RecurringPatternDB, txn: Transaction) -> None:
        """
        Apply Welford running-stat update to a pattern row in-place.

        This is the canonical stat-update implementation used by both the
        standalone path (_update_pattern_with_transaction) and the bulk path
        (match_all_transactions).  It does NOT open any session; the caller is
        responsible for providing an ORM row that is bound to an open session.

        Replicates the P2-5 occurrence-count logic exactly:
        - Increments occurrence_count by 1.
        - Updates expected_amount via running average.
        - Updates amount_std_dev via Welford's online algorithm (population
          variance, matching the batch _compute_std convention).
        - Advances next_expected_date.
        - Resets missed_count and ensures is_active=True.
        """
        # Update last occurrence
        if txn.posting_date > db_pattern.last_occurrence:
            db_pattern.last_occurrence = txn.posting_date

        # Increment occurrence count
        db_pattern.occurrence_count += 1

        # Update amount statistics (running average)
        old_avg = db_pattern.expected_amount
        new_avg = (
            old_avg * (db_pattern.occurrence_count - 1) + abs(txn.amount_signed)
        ) / db_pattern.occurrence_count
        db_pattern.expected_amount = new_avg

        # Update std dev via Welford's online algorithm (population variance, to
        # match the batch _compute_std). Reconstruct the prior sum-of-squares from
        # the stored std: M2_old = std_old^2 * (n-1) over the n-1 prior points.
        n = db_pattern.occurrence_count
        x = float(abs(txn.amount_signed))
        old_mean = float(old_avg)
        new_mean = float(new_avg)
        old_m2 = (float(db_pattern.amount_std_dev) ** 2) * (n - 1)
        new_m2 = old_m2 + (x - old_mean) * (x - new_mean)
        new_variance = new_m2 / n if n > 0 else 0.0
        db_pattern.amount_std_dev = Decimal(str(max(new_variance, 0.0) ** 0.5))

        # Predict next occurrence
        db_pattern.next_expected_date = self._predict_next_occurrence(
            txn.posting_date, db_pattern.frequency, db_pattern.expected_day_of_period
        )

        # Reset missed count (pattern is active again)
        db_pattern.missed_count = 0
        db_pattern.is_active = True

        # Update timestamp
        db_pattern.updated_at = naive_utc_now()

    def _predict_next_occurrence(self, last_date, frequency: str, expected_day: int):
        """Predict next occurrence (delegates to the shared month-end-safe helper)."""
        return next_occurrence(last_date, frequency, expected_day)

    def compute_variance(self, txn: Transaction, pattern: RecurringPattern) -> Decimal:
        """
        Compute variance between transaction and pattern.

        Returns absolute difference from expected amount.
        """
        return abs(abs(txn.amount_signed) - pattern.expected_amount)

    def match_all_transactions(self) -> dict[str, int]:
        """
        Match all unmatched transactions to patterns.

        Updates transactions with recurring_pattern_id.

        Performance: fetches all active patterns ONCE and scores them purely in
        Python, with NO per-transaction DB round-trips.  All stat updates are
        applied inside the same session that loaded the transactions, so the
        entire operation runs in O(1) session_scope opens regardless of how many
        unmatched transactions exist.

        Returns:
            Dict with statistics: transactions_matched, patterns_updated
        """
        transactions_matched = 0
        patterns_updated: set[str] = set()

        with session_scope(self.db_path) as session:
            # --- (a) Load all active patterns once, grouped for fast lookup. ---
            all_patterns = session.query(RecurringPatternDB).filter_by(is_active=True).all()

            # Index by (account_id, merchant) for O(1) candidate lookup per txn. Sort the
            # candidate list within each group by pattern_id so the strictly-greater
            # best-score tie-break (which keeps the FIRST candidate seen) is independent of
            # the DB's pattern row order -- a deterministic, order-independent choice.
            pattern_index: dict[tuple[str, str], list[RecurringPatternDB]] = defaultdict(list)
            for p in all_patterns:
                pattern_index[(p.account_id, p.merchant)].append(p)
            for candidate_list in pattern_index.values():
                candidate_list.sort(key=lambda p: p.pattern_id)

            # --- (b) Load unmatched transactions. ---
            # Each match folds the txn's amount into the matched pattern's running average
            # / std (mutated in-loop), so the per-pattern stat result depends on the ORDER
            # txns are applied. The query gives no ordering guarantee, so the same set of
            # rows could fold in different orders across runs/back-ends and yield different
            # stored stats + occurrence handling. Iterate in an explicit, stable order
            # (posting_date, then unique txn_id) to make the outcome deterministic.
            txns = (
                session.query(TransactionDB)
                .filter(
                    TransactionDB.recurring_pattern_id.is_(None),
                    *cad_sql_filters(),
                )
                .all()
            )
            txns.sort(key=lambda t: (t.posting_date, t.txn_id))

            # Convert to Transaction models for scoring.
            from ledger.storage.mappers import transaction_from_db

            for db_txn in txns:
                txn = transaction_from_db(db_txn)
                account_id = account_identity(txn.institution, txn.account)

                candidates = pattern_index.get((account_id, txn.merchant), [])
                if not candidates:
                    continue

                # --- Score candidates in Python (no extra DB query). ---
                best_match: RecurringPatternDB | None = None
                best_score = 0.0

                for pattern in candidates:
                    score = self._compute_match_score(txn, pattern)
                    if score > best_score and score >= 0.7:
                        best_score = score
                        best_match = pattern

                if best_match is None:
                    continue

                # --- Link the transaction. ---
                db_txn.recurring_pattern_id = best_match.pattern_id
                transactions_matched += 1
                patterns_updated.add(best_match.pattern_id)

                # --- (c) Apply stat update in-place on the already-loaded ORM row.
                # Uses _apply_pattern_stats_update so the math is identical to the
                # standalone path — no new session_scope, no re-query. ---
                self._apply_pattern_stats_update(best_match, txn)

            # Auto-commit via context manager (covers both txn links + stat updates).

        return {
            "transactions_matched": transactions_matched,
            "patterns_updated": len(patterns_updated),
        }
