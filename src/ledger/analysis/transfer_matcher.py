"""
Intelligent transfer matching across accounts.

Matches transfer transactions to identify pairs (source + destination)
using amount, date proximity, and account information.
"""

import re
from dataclasses import dataclass
from decimal import Decimal

from ledger.adapters.registry import CompatibilityPolicySnapshot, compatibility_policy_snapshot
from ledger.core.models import Transaction


def _is_credit_account_type(account_type: str, policy: CompatibilityPolicySnapshot) -> bool:
    account = account_type.lower()
    keywords = ("visa", "mastercard", "credit", "card", *policy.credit_account_tokens)
    return any(keyword in account for keyword in keywords)


def is_credit_leg(txn: Transaction, *, policy: CompatibilityPolicySnapshot | None = None) -> bool:
    """True if this transfer leg is a credit-card account.

    Canonical credit-card-leg predicate, shared by the matcher (the same-sign
    deposit->card exception) and the query layer (reconstructing a transfer pair's
    outflow/inflow direction). A credit-card leg is the DESTINATION of a payment --
    money flows INTO the card to pay it down -- so it reliably identifies the inflow
    side even when both legs carry the same native amount sign across institutions.
    """
    snapshot = policy if policy is not None else compatibility_policy_snapshot()
    return (
        _is_credit_account_type(txn.account_type or "", snapshot)
        or txn.institution in snapshot.credit_institutions
    )


@dataclass
class TransferMatch:
    """Represents a matched transfer pair."""

    source_txn_id: str
    dest_txn_id: str
    confidence: float  # 0.0-1.0


class TransferMatcher:
    """
    Intelligent transfer matching across accounts.

    A candidate pair scores 0.0-1.0 (see ``_calculate_match_score`` and
    ``docs/agent/transfer-matching.md``). Legs in different currencies, or on the same account
    at the same institution, are rejected outright (0.0). The scoring factors are:

    - Amount match (+0.50): exact ``amount_raw`` magnitude within tolerance. Both-zero
      or beyond-tolerance amounts are rejected.
    - Date proximity (+0.20): same day, with linear decay within the window. Beyond the
      window is rejected.
    - Opposite signs (+0.10): required unless exactly one leg is a credit card (a
      cross-issuer card payment can be same-signed).
    - Account-type pattern (+0.10): chequing↔credit, savings↔chequing, or an internal
      transfer between two different accounts at one institution.
    - Cross-institution payment (+0.10): privately configured counterparty payment evidence.
    - Shared trailing reference number (+0.15 bonus): a 3-4 digit token both legs carry
      (excluding plausible calendar years, which are not genuine references). Clamped so
      the final score never exceeds 1.0.

    Amount + date + opposite-sign alone are NOT proof of a pair. ``_has_link_signal``
    gates every match on at least one positive LINKING signal (shared reference,
    cross-institution payment, chequing↔credit / savings↔chequing account-type pattern,
    or a shared distinctive description token); a pair lacking all of these scores 0.0.
    """

    def __init__(
        self,
        date_window_days: int = 5,  # Up to 5 days apart
        amount_tolerance_pct: Decimal = Decimal("0.0"),  # MUST match exactly
        min_confidence: float = 0.5,  # Lower threshold for patterns
    ):
        self.date_window = date_window_days
        self.amount_tolerance = amount_tolerance_pct
        self.min_confidence = min_confidence
        self._policy: CompatibilityPolicySnapshot | None = None

    def _interpretation_policy(self) -> CompatibilityPolicySnapshot:
        if self._policy is None:
            self._policy = compatibility_policy_snapshot()
        return self._policy

    def match_transfers(self, transfers: list[Transaction]) -> list[TransferMatch]:
        """
        Match transfers using multi-signal approach.

        Args:
            transfers: List of transactions identified as transfers

        Returns:
            List of TransferMatch objects representing paired transfers
        """
        self._policy = compatibility_policy_snapshot()
        matches = []
        unmatched = transfers.copy()

        # Sort by date for efficient searching, with txn_id as a stable secondary
        # key. The pairing loop below is greedy, so its result used to depend on the
        # INPUT order of equal-date legs (and the two call sites -- pipeline.partition
        # and post_processing's order_by -- feed different orders). Sorting on the
        # unique, stable txn_id makes both the outer pop order and the inner
        # tie-break deterministic, so the same SET of legs always pairs identically.
        unmatched.sort(key=lambda t: (t.posting_date, t.txn_id))

        while unmatched:
            candidate = unmatched.pop(0)

            # Skip if already matched
            if candidate.paired_transfer_id:
                continue

            # Find potential matches
            best_match = None
            best_score = 0.0

            for other in unmatched:
                if other.paired_transfer_id:
                    continue

                # Early-break: the list is sorted by (posting_date, txn_id). Once
                # other's date is more than date_window days past candidate's date, no
                # subsequent leg can be within the window either -- skip the rest.
                # This is purely a performance optimization; it never skips a leg that
                # would have scored > 0.0 (legs beyond the window score exactly 0.0
                # via the date_diff > self.date_window → return 0.0 path in
                # _calculate_match_score), so matched pairs are UNCHANGED.
                if (other.posting_date - candidate.posting_date).days > self.date_window:
                    break

                score = self._calculate_match_score(candidate, other)

                if score < self.min_confidence:
                    continue

                # Strictly-greater keeps the FIRST candidate seen on a tie; because
                # `unmatched` is sorted by (posting_date, txn_id), that first-on-tie
                # is the lowest txn_id -- a deterministic, order-independent choice.
                if score > best_score:
                    best_score = score
                    best_match = other

            if best_match:
                matches.append(
                    TransferMatch(
                        source_txn_id=candidate.txn_id,
                        dest_txn_id=best_match.txn_id,
                        confidence=best_score,
                    )
                )
                unmatched.remove(best_match)

        return matches

    def _calculate_match_score(self, txn1: Transaction, txn2: Transaction) -> float:
        """
        Calculate match confidence score (0.0-1.0).

        STRICT RULES:
        - Amount MUST match exactly (no tolerance)
        - Date within 5 days
        - For credit cards: chequing transfer/payment → card "payment thank you"
        - For savings: only transfers to/from chequing

        Scoring factors:
        - Amount match: 0.50 (50% weight) - MUST be exact
        - Date proximity: 0.20 (20% weight)
        - Opposite signs: 0.10 (10% weight)
        - Account type pattern: 0.10 (10% weight) - credit card payments
        - Cross-institution pattern: 0.10 (10% weight)

        Args:
            txn1: First transaction
            txn2: Second transaction

        Returns:
            Confidence score from 0.0 to 1.0
        """
        score = 0.0

        # Two legs in DIFFERENT currencies cannot be the two ends of one transfer:
        # a USD leg and a CAD leg of equal magnitude are not reconcilable without an
        # FX rate, so pairing them would write a bogus counterparty and inflate the
        # match rate. Cross-currency transfers are simply not matched (correct: we
        # cannot pair legs whose amounts are not comparable). Guard EARLY, before any
        # magnitude/sign scoring runs on incomparable amounts.
        if txn1.currency != txn2.currency:
            return 0.0

        # Two legs on the SAME physical account cannot be the two ends of a
        # transfer -- money has to move BETWEEN accounts. Without this guard the
        # matcher paired unrelated rows on one account (e.g. two card payments).
        if (
            txn1.account
            and txn2.account
            and txn1.account == txn2.account
            and txn1.institution == txn2.institution
        ):
            return 0.0

        # 1. Amount match (50% weight) - exact, or within the configured tolerance
        # NOTE: Use amount_raw instead of amount_signed because transfers have amount_signed=0
        amount1 = abs(txn1.amount_raw)
        amount2 = abs(txn2.amount_raw)

        # Reject a pair where BOTH legs are zero-amount: a zero-dollar "transfer" moves
        # no money, so it is not a transfer at all. Without this guard the tolerance test
        # below (abs(0 - 0) > 0 * tol -> 0 > 0 -> False) lets two zero-amount legs through
        # the amount gate with full +0.50 amount score, and the deposit->card path (which
        # bypasses the opposite-sign rule) could then pair them.
        if amount1 == 0 and amount2 == 0:
            return 0.0

        if abs(amount1 - amount2) > amount1 * self.amount_tolerance:
            return 0.0  # REJECT if amounts differ beyond tolerance

        score += 0.50  # Amount match

        # 2. Date proximity (20% weight)
        date_diff = abs((txn1.posting_date - txn2.posting_date).days)
        if date_diff == 0:
            score += 0.20  # Same day
        elif date_diff <= self.date_window:
            # Linear decay: closer dates get higher scores
            score += 0.20 * (1 - date_diff / (self.date_window + 1))
        else:
            return 0.0  # REJECT if dates too far apart

        # 3. Opposite signs - a transfer between two DEPOSIT accounts is money out
        # of one and into the other, so it must be opposite-signed; two same-sign
        # deposit legs can never be one transfer. A credit-card payment, however,
        # can be same-signed because issuers differ in their card-payment signs,
        # while the chequing leg is negative, so only
        # require opposite signs when NEITHER leg is a credit card.
        # NOTE: Use amount_raw for sign check (amount_signed is 0 for transfers).
        opposite_signs = (txn1.amount_raw > 0 and txn2.amount_raw < 0) or (
            txn1.amount_raw < 0 and txn2.amount_raw > 0
        )
        if not opposite_signs and not self._exactly_one_credit_leg(txn1, txn2):
            return 0.0
        if opposite_signs:
            score += 0.10

        # 4. Account type pattern (10% weight)
        # Credit card payments OR savings ↔ chequing
        if self._is_valid_transfer_pattern(txn1, txn2):
            score += 0.10

        # 5. Cross-institution credit card payment (10% weight)
        # Chequing at one institution to a credit card at another.
        if self._is_cross_institution_payment(txn1, txn2):
            score += 0.10

        # GATE: bare amount + opposite-sign + date proximity is NOT evidence that two
        # legs are the SAME transfer -- any two unrelated equal-amount opposite-signed
        # same-day legs satisfy it and would falsely pair (and corrupt counterparty
        # attribution + match-rate stats). Require at least ONE positive LINKING signal
        # that actually ties these two specific legs together. If none is present, this
        # is not a confident transfer pair regardless of how the base factors stack up.
        if not self._has_link_signal(txn1, txn2):
            return 0.0

        # A shared trailing reference number is a near-certain signal: add it to break ties
        # among equal-amount/equal-date candidates, then clamp so a maxed-out base score
        # can't push the stored confidence past 1.0 (which renders as ">100%").
        return min(score + self._reference_bonus(txn1, txn2), 1.0)

    def _has_link_signal(self, txn1: Transaction, txn2: Transaction) -> bool:
        """At least one positive signal that these two legs are the SAME transfer,
        BEYOND bare amount + opposite-sign + date proximity (which any coincidental
        equal-amount opposite-signed same-day pair would satisfy).

        Linking signals -- each genuinely ties the two specific legs together:
        - a shared trailing REFERENCE number on both legs,
        - a recognised cross-institution credit-card PAYMENT relationship,
        - a deposit<->card or savings<->chequing ACCOUNT-TYPE pattern (Patterns 1 & 2),
        - GENUINELY complementary DESCRIPTIONS -- a shared DISTINCTIVE token
          (counterparty name / reference-like token) present in BOTH legs, NOT two
          independent generic transfer keywords.

        Deliberately NOT a signal on its own: the weak Pattern-3 case (same institution,
        any two different accounts, amount + sign only) -- that is exactly the
        coincidental shape this gate exists to reject.
        """
        return (
            self._reference_bonus(txn1, txn2) > 0.0
            or self._is_cross_institution_payment(txn1, txn2)
            or self._is_account_type_transfer_pattern(txn1, txn2)
            or self._has_matching_descriptions(txn1, txn2)
        )

    _REFERENCE_RE = re.compile(r"\b(\d{3,4})\s*$")

    def _reference_token(self, txn: Transaction) -> str | None:
        """Trailing 3-4 digit reference some institutions append to BOTH legs of an online
        transfer (e.g. 'Online Banking transfer - 9816').

        A trailing 4-digit run that is a plausible calendar YEAR (~1900-2099) is NOT a
        genuine reference number -- two unrelated legs whose descriptions merely both end
        in a year like '2024' would otherwise share a "reference" and, since a shared
        reference is a STANDALONE link signal (`_has_link_signal`), pair at ~1.0. Such
        year-like tokens are excluded so they cannot clear the gate on their own; a real
        3-4 digit reference (e.g. 9816) that is not year-shaped still qualifies.
        """
        match = self._REFERENCE_RE.search(txn.description or "")
        if match is None:
            return None
        token = match.group(1)
        if len(token) == 4 and 1900 <= int(token) <= 2099:
            return None
        return token

    def _reference_bonus(self, txn1: Transaction, txn2: Transaction) -> float:
        """Decisive, deterministic disambiguator: when both legs carry the SAME trailing
        reference number they are almost certainly the two ends of one transfer."""
        ref1 = self._reference_token(txn1)
        return 0.15 if ref1 is not None and ref1 == self._reference_token(txn2) else 0.0

    def _is_credit_leg(self, txn: Transaction) -> bool:
        """True if this leg is a credit-card account (delegates to the shared,
        module-level ``is_credit_leg`` so the matcher and the query layer agree)."""
        return is_credit_leg(txn, policy=self._interpretation_policy())

    def _exactly_one_credit_leg(self, txn1: Transaction, txn2: Transaction) -> bool:
        """True only when EXACTLY ONE leg is a credit card -- the cross-issuer
        deposit->card payment, which can be same-signed when both layouts show an
        outgoing payment as negative, and so bypasses the opposite-sign
        rule. Two credit legs (card-to-card is not a transfer) or none do NOT get the
        exception, so two same-signed deposit legs still require opposite signs."""
        return sum(1 for txn in (txn1, txn2) if self._is_credit_leg(txn)) == 1

    def _is_account_type_transfer_pattern(self, txn1: Transaction, txn2: Transaction) -> bool:
        """Patterns 1 & 2 ONLY -- the account-type shapes that, on their own, link two
        legs as the same transfer:

        1. Chequing <-> Credit card (any institution)
        2. Savings <-> Chequing

        This is the LINKING subset of `_is_valid_transfer_pattern`; it deliberately
        excludes Pattern 3 (same institution, different accounts, amount + sign only),
        which is too weak to confirm a pair by itself.
        """
        acct1_lower = txn1.account_type.lower()
        acct2_lower = txn2.account_type.lower()

        chequing_keywords = {"chequing", "checking"}
        # STRUCTURAL HEURISTIC: this account-type pattern links ANY savings-type leg to
        # ANY chequing-type leg of equal amount on a near date -- the common transfer
        # shape, but it can coincidentally pair two unrelated equal-amount legs. That is
        # accepted for the real account types emitted today.
        #
        # "deposit" is matched ONLY against `account_type`, never a transaction
        # description. Adapters own the account-type values they report.
        #
        # FUTURE: a real "Term Deposit"/GIC account type (which literally contains
        # "deposit") would wrongly read as savings and pair via this pattern; exclude
        # such types from savings_keywords when they are actually introduced. See
        # test_match_transfers_term_deposit_vs_chequing_documents_structural_heuristic.
        savings_keywords = {"savings", "deposit"}

        is_chequing_1 = any(kw in acct1_lower for kw in chequing_keywords)
        is_chequing_2 = any(kw in acct2_lower for kw in chequing_keywords)
        is_savings_1 = any(kw in acct1_lower for kw in savings_keywords)
        is_savings_2 = any(kw in acct2_lower for kw in savings_keywords)
        policy = self._interpretation_policy()
        is_credit_1 = _is_credit_account_type(acct1_lower, policy)
        is_credit_2 = _is_credit_account_type(acct2_lower, policy)

        # Pattern 1: Chequing ↔ Credit card (one chequing, one credit)
        if (is_chequing_1 and is_credit_2) or (is_credit_1 and is_chequing_2):
            return True

        # Pattern 2: Savings ↔ Chequing (one savings, one chequing)
        return bool((is_savings_1 and is_chequing_2) or (is_chequing_1 and is_savings_2))

    def _is_valid_transfer_pattern(self, txn1: Transaction, txn2: Transaction) -> bool:
        """
        Check if this is a valid transfer pattern.

        Valid patterns:
        1. Chequing → Credit card (any institution)
        2. Savings ↔ Chequing only
        3. Internal transfer within one institution between different accounts
        """
        # Patterns 1 & 2: account-type shapes (the strong, linking subset).
        if self._is_account_type_transfer_pattern(txn1, txn2):
            return True

        # Pattern 3: internal transfer within one institution -- but only between
        # DIFFERENT accounts (a transfer moves money between two accounts, so two
        # unrelated rows on one account must not earn the transfer-pattern bonus).
        # NOTE: Pattern 3 still earns the +0.10 account-type bonus here, but it is NOT
        # a standalone LINKING signal (see `_has_link_signal`): a pair that only matches
        # Pattern 3 still needs another signal to clear the confidence threshold.
        return (
            txn1.institution == txn2.institution
            and bool(txn1.account)
            and bool(txn2.account)
            and txn1.account != txn2.account
        )

    def _is_cross_institution_payment(self, txn1: Transaction, txn2: Transaction) -> bool:
        """Check if this is a cross-institution credit-card payment.

        Requires the two legs to be at DIFFERENT institutions (a same-institution
        card payment is handled by the account-type chequing<->credit pattern), so two
        intra-institution deposit legs cannot false-pair on this signal.
        """
        # Cross-INSTITUTION requires distinct institutions; same-institution moves are
        # covered by the account-type transfer pattern, not this payment signal.
        if txn1.institution == txn2.institution:
            return False

        desc1_lower = txn1.description.lower()
        desc2_lower = txn2.description.lower()
        if "payment" not in desc1_lower and "payment" not in desc2_lower:
            return False

        for institution, tokens in self._interpretation_policy().payment_tokens:
            names_counterparty = (
                txn1.institution == institution
                or txn2.institution == institution
                or any(token in desc1_lower for token in tokens)
                or any(token in desc2_lower for token in tokens)
            )
            if names_counterparty:
                return True

        return False

    # Generic transfer/payment vocabulary that, on its own, links NOTHING: every
    # transfer leg contains some of these words, so two UNRELATED legs (e.g. an
    # 'INTERAC e-Transfer sent' to one party and an unrelated 'INTERAC e-Transfer
    # received' from another) would both match. These are stop-words for the
    # shared-token test below -- a shared word only counts as a link if it is NOT
    # one of these.
    _GENERIC_DESC_TOKENS = frozenset(
        {
            "transfer",
            "online",
            "banking",
            "payment",
            "misc",
            "thank",
            "you",
            "to",
            "from",
            "sent",
            "received",
            "the",
            "a",
            "of",
            "and",
            "for",
            "etransfer",
            "e",
            "interac",
            "bill",
            "deposit",
            "withdrawal",
            "account",
            "pai",
            "ement",
            "ref",
            "reference",
            "no",
        }
    )

    _DESC_TOKEN_RE = re.compile(r"[a-z0-9&]+")

    # A distinctive token must be at least this many characters to count as a link.
    # Single/short tokens (e.g. "1", "to", "ab") recur across unrelated legs and so
    # carry no individuating signal.
    _MIN_DISTINCTIVE_TOKEN_LEN = 3

    def _distinctive_desc_tokens(self, txn: Transaction) -> set[str]:
        """Tokens in a description that could distinguish THIS specific leg -- a
        counterparty name, etc. Strips the generic transfer/payment vocabulary that
        every transfer leg shares, so what remains is a candidate distinctive token
        (e.g. 'acme', 'john').

        A token only qualifies as distinctive when it is meaningfully individuating:
        - NOT a generic transfer/payment stopword,
        - long enough (>= _MIN_DISTINCTIVE_TOKEN_LEN) -- single/short tokens like '1'
          or 'to' recur across unrelated legs and link nothing,
        - NOT purely numeric -- a bare digit run is not a counterparty name, and the
          genuine "both legs share a trailing reference number" signal is handled
          separately and decisively by `_reference_bonus`. Counting any shared numeric
          token here would falsely link two rows that merely both contain, say, '1'.
        """
        tokens = self._DESC_TOKEN_RE.findall((txn.description or "").lower())
        return {
            tok
            for tok in tokens
            if tok not in self._GENERIC_DESC_TOKENS
            and len(tok) >= self._MIN_DISTINCTIVE_TOKEN_LEN
            and not tok.isdigit()
        }

    def _has_matching_descriptions(self, txn1: Transaction, txn2: Transaction) -> bool:
        """
        Check if the descriptions GENUINELY link these two SPECIFIC legs as the same
        transfer.

        A real link needs complementarity between the two specific legs, NOT two
        independent generic transfer keywords. Two descriptions that merely each
        contain a generic transfer/payment word (e.g. both contain "transfer", or one
        "transfer" + the other "payment") are NOT evidence of a pair -- every transfer
        leg has such words, so any two unrelated equal-amount legs would falsely link
        (the original false-pair bug: "INTERAC e-Transfer sent" vs "INTERAC e-Transfer
        received").

        The accepted signal is a SHARED DISTINCTIVE token appearing in BOTH
        descriptions -- a counterparty name or reference-like token that survives
        stripping the generic transfer/payment vocabulary. A directional sent/received
        pair only counts when it references the SAME such token in both legs.
        """
        shared = self._distinctive_desc_tokens(txn1) & self._distinctive_desc_tokens(txn2)
        return bool(shared)

    def apply_matches(
        self, transfers: list[Transaction], matches: list[TransferMatch]
    ) -> list[Transaction]:
        """
        Apply transfer matches to transaction list.

        Updates paired_transfer_id and transfer_confidence fields
        on matched transactions.

        Args:
            transfers: List of transfer transactions
            matches: List of TransferMatch objects

        Returns:
            Updated list of transactions with pairing information
        """
        # Create a mapping of txn_id to match info
        match_map: dict[str, tuple[str, float, str, str]] = {}

        # O(1) lookup instead of O(n) next() scan per match
        by_id = {t.txn_id: t for t in transfers}

        for match in matches:
            # Look up the actual transactions to get counterparty info
            source_txn = by_id.get(match.source_txn_id)
            dest_txn = by_id.get(match.dest_txn_id)

            if source_txn and dest_txn:
                # Source transaction points to destination
                match_map[match.source_txn_id] = (
                    match.dest_txn_id,
                    match.confidence,
                    dest_txn.account,
                    dest_txn.institution,
                )
                # Destination transaction points to source
                match_map[match.dest_txn_id] = (
                    match.source_txn_id,
                    match.confidence,
                    source_txn.account,
                    source_txn.institution,
                )

        # Apply matches by creating new Transaction objects (immutable)
        updated_transfers = []
        for txn in transfers:
            if txn.txn_id in match_map:
                paired_id, confidence, counterparty_acct, counterparty_inst = match_map[txn.txn_id]

                # Create new transaction with updated fields
                # Since Transaction is frozen, we need to use model_copy
                updated_txn = txn.model_copy(
                    update={
                        "paired_transfer_id": paired_id,
                        "transfer_confidence": confidence,
                        "counterparty_account": counterparty_acct,
                        "counterparty_institution": counterparty_inst,
                    }
                )
                updated_transfers.append(updated_txn)
            else:
                # No match found, keep original
                updated_transfers.append(txn)

        return updated_transfers
