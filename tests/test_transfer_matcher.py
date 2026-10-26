"""Transfer matcher: a transfer must be money OUT of one account and INTO another.

Two same-sign legs (both outflows) cannot be a single transfer; requiring
opposite amount_raw signs prevents bogus pairings (the bonus-only check let a
0.50+0.20+0.10 = 0.767 same-sign pair slip through).
"""

from datetime import date
from decimal import Decimal
from itertools import permutations

import pytest

from ledger.analysis.transfer_matcher import TransferMatcher
from ledger.core import Transaction


@pytest.fixture
def fictional_payment_policy(monkeypatch):
    from ledger.adapters import registry

    # This is an explicit fictional extension policy, never a core provider default.
    policy = registry.compatibility_policy()
    policy["payment_tokens"] = {"lumen": ["lumen"]}
    monkeypatch.setattr(registry, "compatibility_policy", lambda: policy)


def _transfer(
    i: int,
    amount_raw: Decimal,
    account: str,
    account_type: str,
    posting_date: date,
    description: str = "TRANSFER",
    institution: str = "harbor",
) -> Transaction:
    return Transaction(
        txn_id=f"tr_{i}",
        txn_fingerprint=f"fp_{i}",
        posting_date=posting_date,
        institution=institution,
        account=account,
        account_type=account_type,
        flow="transfer",
        cashflow_type="transfer",
        include_in_cashflow=False,
        currency="CAD",
        amount_raw=amount_raw,
        amount_signed=Decimal("0"),
        description=description,
        source_file="x.csv",
        source_path="x.csv",
        source_period="2025-01",
        source_row=i,
        imported_at=date(2025, 1, 1),
        parser_version="test",
    )


def test_match_transfers_rejects_same_sign():
    """Two same-sign (both outflow) legs cannot be a single transfer."""
    matcher = TransferMatcher()
    a = _transfer(1, Decimal("-500.00"), "harbor-chequing", "Chequing", date(2025, 1, 10))
    b = _transfer(2, Decimal("-500.00"), "harbor-savings", "Savings", date(2025, 1, 11))

    assert matcher.match_transfers([a, b]) == []


def test_match_transfers_pairs_same_sign_credit_card_payment():
    """A deposit -> credit-card payment can be same-signed (issuers show payments
    with different sign conventions, e.g. HARBOR chequing -X paying a Cedar card -X);
    it must still pair. Only deposit<->deposit transfers require opposite signs."""
    matcher = TransferMatcher()
    chequing = _transfer(1, Decimal("-75.00"), "harbor-chequing", "Chequing", date(2025, 6, 18))
    card = _transfer(2, Decimal("-75.00"), "cedar-mc", "Silver Mastercard", date(2025, 6, 18))

    matches = matcher.match_transfers([chequing, card])

    assert len(matches) == 1


def test_match_transfers_pairs_opposite_sign():
    """An outflow + matching inflow across accounts should still pair."""
    matcher = TransferMatcher()
    out = _transfer(1, Decimal("-500.00"), "harbor-chequing", "Chequing", date(2025, 1, 10))
    inn = _transfer(2, Decimal("500.00"), "harbor-spark-visa", "SPARK Visa", date(2025, 1, 10))

    matches = matcher.match_transfers([out, inn])

    assert len(matches) == 1


def test_match_transfers_rejects_dates_outside_window():
    """Legs more than the date window apart cannot pair, even with exact amounts."""
    matcher = TransferMatcher()  # default window is 5 days
    out = _transfer(1, Decimal("-500.00"), "harbor-chequing", "Chequing", date(2025, 1, 1))
    inn = _transfer(2, Decimal("500.00"), "harbor-spark-visa", "SPARK Visa", date(2025, 1, 20))

    assert matcher.match_transfers([out, inn]) == []


def test_match_transfers_rejects_same_account():
    """Two legs on the SAME account cannot be the two ends of one transfer.

    Regression: the matcher never compared the account number, so two payments
    landing on the same card were paired as a 'transfer' and wrongly excluded.
    """
    matcher = TransferMatcher()
    a = _transfer(1, Decimal("-600.00"), "cedar-mc", "Silver Mastercard", date(2025, 1, 10))
    b = _transfer(2, Decimal("-600.00"), "cedar-mc", "Silver Mastercard", date(2025, 1, 12))

    assert matcher.match_transfers([a, b]) == []


def test_match_transfers_rejects_two_same_sign_card_legs():
    """The same-sign exception is only for a deposit -> card payment (exactly one
    credit leg). Two same-sign credit-card legs on different cards are not a
    transfer between accounts and must be rejected."""
    matcher = TransferMatcher()
    a = _transfer(1, Decimal("-600.00"), "cedar-mc", "Cedar Mastercard", date(2025, 1, 10))
    b = _transfer(2, Decimal("-600.00"), "meadow-mc", "Meadow Mastercard", date(2025, 1, 11))

    assert matcher.match_transfers([a, b]) == []


def test_match_transfers_pairs_lumen_cross_institution_payment(fictional_payment_policy):
    """A fictional payment leg and a receiving deposit leg establish a
    cross-institution payment between two DEPOSIT accounts. Neither leg is a credit
    card (no Pattern-1 account-type link) and there is no shared reference/distinctive
    token, so the only possible link signal is the configured policy in
    _is_cross_institution_payment. Without it match_transfers returns nothing."""
    matcher = TransferMatcher()
    lumen = _transfer(
        1,
        Decimal("-350.00"),
        "lumen-chequing-9107",
        "Chequing",
        date(2042, 6, 9),
        description="Payment out",
        institution="lumen",
    )
    harbor = _transfer(
        2,
        Decimal("350.00"),
        "harbor-chequing-9101",
        "Chequing",
        date(2042, 6, 9),
        description="Incoming funds",
        institution="harbor",
    )
    assert len(matcher.match_transfers([lumen, harbor])) == 1


def test_transfer_matcher_uses_reference_to_disambiguate():
    """Within a cluster of equal-amount, same-window legs, a shared trailing reference
    number must pick the correct partner instead of relying on date-sort luck."""
    matcher = TransferMatcher()
    # Without the reference signal, leg 1 (day 10) would greedily take leg 4 (day 10,
    # closer date) -- the wrong, 1613 partner. The shared 9816 must override that.
    a = _transfer(
        1,
        Decimal("-1000.00"),
        "harbor-chq",
        "Chequing",
        date(2025, 1, 10),
        "Online transfer - 9816",
    )
    b = _transfer(
        2,
        Decimal("1000.00"),
        "harbor-sav",
        "Savings",
        date(2025, 1, 11),
        "Online Banking transfer - 9816",
    )
    c = _transfer(
        3,
        Decimal("-1000.00"),
        "harbor-chq",
        "Chequing",
        date(2025, 1, 11),
        "Online transfer - 1613",
    )
    d = _transfer(
        4,
        Decimal("1000.00"),
        "harbor-sav",
        "Savings",
        date(2025, 1, 10),
        "Online Banking transfer - 1613",
    )

    matches = matcher.match_transfers([a, b, c, d])
    pairs = {frozenset((m.source_txn_id, m.dest_txn_id)) for m in matches}

    assert frozenset(("tr_1", "tr_2")) in pairs  # 9816 <-> 9816
    assert frozenset(("tr_3", "tr_4")) in pairs  # 1613 <-> 1613


def test_match_confidence_never_exceeds_one_with_reference_bonus():
    """The reference bonus must fold UNDER the 1.0 cap, not stack on top. A max-signal
    deposit<->deposit pair scores 0.90; adding +0.15 on top stored 1.05, which renders
    as '>100%' and breaks the documented 0.0-1.0 range."""
    matcher = TransferMatcher()
    out = _transfer(
        1,
        Decimal("-1000.00"),
        "harbor-chq",
        "Chequing",
        date(2025, 1, 10),
        "Online transfer - 9816",
    )
    inn = _transfer(
        2,
        Decimal("1000.00"),
        "harbor-sav",
        "Savings",
        date(2025, 1, 10),
        "Online Banking transfer - 9816",
    )

    matches = matcher.match_transfers([out, inn])

    assert len(matches) == 1
    assert matches[0].confidence <= 1.0


def test_apply_matches_sets_symmetric_pairing():
    """apply_matches links both legs to each other with counterparty info."""
    matcher = TransferMatcher()
    out = _transfer(1, Decimal("-500.00"), "harbor-chequing", "Chequing", date(2025, 1, 10))
    inn = _transfer(2, Decimal("500.00"), "harbor-spark-visa", "SPARK Visa", date(2025, 1, 10))

    matches = matcher.match_transfers([out, inn])
    updated = matcher.apply_matches([out, inn], matches)

    by_id = {t.txn_id: t for t in updated}
    assert by_id["tr_1"].paired_transfer_id == "tr_2"
    assert by_id["tr_2"].paired_transfer_id == "tr_1"
    assert by_id["tr_1"].counterparty_account == "harbor-spark-visa"
    assert by_id["tr_2"].counterparty_account == "harbor-chequing"


def test_match_transfers_rejects_coincidental_unrelated_legs():
    """Bare amount + opposite-sign + same-day is NOT enough to declare two legs the
    two ends of ONE transfer.

    Two unrelated transfer legs on DIFFERENT institutions, with no shared reference,
    no matching descriptions, and no deposit<->card / cross-institution payment
    relationship, happen to have equal amounts on the same day. They must NOT pair --
    there is no positive signal that links them as the same transfer.

    Pre-fix this scored 0.50 (amount) + 0.20 (same day) + 0.10 (opposite sign) =
    0.80 and wrongly paired.
    """
    matcher = TransferMatcher()
    a = _transfer(
        1,
        Decimal("-742.00"),
        "acme-broker-1",
        "Brokerage",
        date(2025, 3, 4),
        description="WITHDRAWAL",
        institution="acme",
    )
    b = _transfer(
        2,
        Decimal("742.00"),
        "zenith-cash-9",
        "Cash Account",
        date(2025, 3, 4),
        description="DEPOSIT",
        institution="zenith",
    )

    assert matcher.match_transfers([a, b]) == []


def test_match_transfers_is_order_independent():
    """With one +X inflow and two equal -X same-day debits, the pairing must be the
    SAME regardless of input list order.

    Pre-fix the greedy, date-sorted loop picked whichever equal-scoring debit happened
    to appear first, so the chosen partner flipped with input order -- and the two call
    sites (pipeline.partition vs post_processing order_by) feed different orders, making
    the result non-deterministic.

    The legs carry a savings<->chequing linking signal so a pair is legitimately
    formed; the assertion is purely that WHICH pair forms is stable.
    """
    matcher = TransferMatcher()
    inflow = _transfer(
        1,
        Decimal("1000.00"),
        "harbor-sav",
        "Savings",
        date(2025, 1, 10),
        description="Online Banking transfer",
    )
    debit_a = _transfer(
        2,
        Decimal("-1000.00"),
        "harbor-chq",
        "Chequing",
        date(2025, 1, 10),
        description="Online transfer A",
    )
    debit_b = _transfer(
        3,
        Decimal("-1000.00"),
        "harbor-chq2",
        "Chequing",
        date(2025, 1, 10),
        description="Online transfer B",
    )

    legs = [inflow, debit_a, debit_b]
    chosen_pairs = set()
    for perm in permutations(legs):
        matches = matcher.match_transfers(list(perm))
        assert len(matches) == 1
        chosen_pairs.add(frozenset((matches[0].source_txn_id, matches[0].dest_txn_id)))

    # Every permutation must yield the SAME single pair.
    assert len(chosen_pairs) == 1


def test_match_transfers_pairs_with_matching_descriptions_signal():
    """A genuine transfer whose ONLY linking signal is matching descriptions must
    still pair.

    The two legs share a distinctive counterparty token (the recipient name
    'Acme Holdings') in BOTH descriptions -- that genuine complementarity, not two
    independent generic transfer keywords, is what links them. The pair has no
    shared trailing reference, is not a deposit<->card pattern, and is not a
    cross-institution payment by account type, so _has_matching_descriptions is the
    sole linking signal exercised here."""
    matcher = TransferMatcher()
    a = _transfer(
        1,
        Decimal("-300.00"),
        "bank-a-1",
        "Settlement",
        date(2025, 5, 2),
        description="Online transfer to ACME HOLDINGS",
        institution="banka",
    )
    b = _transfer(
        2,
        Decimal("300.00"),
        "bank-b-1",
        "Settlement",
        date(2025, 5, 2),
        description="Payment from ACME HOLDINGS - thank you",
        institution="bankb",
    )

    matches = matcher.match_transfers([a, b])

    assert len(matches) == 1


def test_match_transfers_rejects_unrelated_interac_sent_vs_received():
    """DOOR 1: two UNRELATED legs that merely each contain a generic transfer keyword
    must NOT be treated as the same transfer.

    'INTERAC e-Transfer sent' (money to one party) and 'INTERAC e-Transfer received'
    (an unrelated equal amount from a different party, same day) carry NO shared
    distinctive token, NO reference, NO counterparty name in common -- only the
    generic 'transfer' keyword each. The pipeline feeds all institutions' transfer
    legs into one match_transfers call, so these DO get compared.

    Pre-fix _has_matching_descriptions returned True (both contained 'transfer'),
    so this scored 0.50 (amount) + 0.20 (same day) + 0.10 (opposite sign) = 0.80 and
    wrongly paired. After tightening the helper to require GENUINE complementarity,
    there is no linking signal and they must not pair."""
    matcher = TransferMatcher()
    sent = _transfer(
        1,
        Decimal("-100.00"),
        "harbor-chequing",
        "Chequing",
        date(2025, 4, 7),
        description="INTERAC e-Transfer sent",
        institution="harbor",
    )
    received = _transfer(
        2,
        Decimal("100.00"),
        "fern-1",
        "Chequing",
        date(2025, 4, 7),
        description="INTERAC e-Transfer received",
        institution="fern",
    )

    assert matcher.match_transfers([sent, received]) == []


def test_match_transfers_rejects_unrelated_generic_transfer_vs_bill_payment():
    """DOOR 1: 'Online transfer to John' vs 'Hydro bill payment' -- one generic
    transfer keyword and one generic payment keyword, but NO shared distinctive
    token tying the two specific legs together. Must NOT pair."""
    matcher = TransferMatcher()
    a = _transfer(
        1,
        Decimal("-225.00"),
        "harbor-chequing",
        "Chequing",
        date(2025, 4, 9),
        description="Online transfer to John",
        institution="harbor",
    )
    b = _transfer(
        2,
        Decimal("225.00"),
        "birch-1",
        "Chequing",
        date(2025, 4, 9),
        description="Hydro bill payment",
        institution="birch",
    )

    assert matcher.match_transfers([a, b]) == []


def test_date_window_early_break_does_not_change_matches():
    """Fix 3: adding a date-window early-break to the inner loop must NOT change
    which pairs are matched — it only skips candidates that already score 0.0
    (date_diff > date_window).

    This test builds a cluster of transfers spanning MORE than date_window days.
    The first two legs (days 1 and 3) are the intended pair (close date, same
    reference). The third leg (day 20, well outside the 5-day window) must be
    skipped WITHOUT pairing with anything, same as before the optimization.

    Key assertion: the matched pairs are identical regardless of whether the
    early-break is present or not. As a proxy, we run match_transfers on the
    full set and assert the expected pair forms AND the out-of-window leg stays
    unmatched.
    """
    matcher = TransferMatcher()  # default date_window=5

    # Pair that should match: day 1 and day 3 (within 5-day window).
    leg_out = _transfer(
        1,
        Decimal("-750.00"),
        "harbor-chq",
        "Chequing",
        date(2025, 3, 1),
        "Online transfer - 4242",
    )
    leg_in = _transfer(
        2,
        Decimal("750.00"),
        "harbor-sav",
        "Savings",
        date(2025, 3, 3),
        "Online Banking transfer - 4242",
    )
    # Leg outside the window: must not pair with either of the above.
    leg_late = _transfer(
        3,
        Decimal("-750.00"),
        "harbor-chq2",
        "Chequing",
        date(2025, 3, 20),
        "Online transfer - 9999",
    )

    matches = matcher.match_transfers([leg_out, leg_in, leg_late])

    # Exactly one pair, and it is the correct pair.
    assert len(matches) == 1
    pair = frozenset((matches[0].source_txn_id, matches[0].dest_txn_id))
    assert pair == frozenset(("tr_1", "tr_2")), (
        f"Expected tr_1+tr_2 to pair; got {pair}. "
        "The date-window early-break may have incorrectly excluded tr_2."
    )


def test_match_transfers_term_deposit_vs_chequing_documents_structural_heuristic():
    """The generic account-type heuristic treats a term deposit as savings.

    Equal amounts and dates therefore link it with a chequing leg even without
    matching descriptions. This regression makes that existing behavior explicit.
    """
    matcher = TransferMatcher()
    term_deposit = _transfer(
        1,
        Decimal("-1000.00"),
        "harbor-td-1",
        "Term Deposit",
        date(2025, 2, 3),
        description="WITHDRAWAL",
        institution="harbor",
    )
    chequing = _transfer(
        2,
        Decimal("1000.00"),
        "harbor-chq",
        "Chequing",
        date(2025, 2, 3),
        description="DEPOSIT",
        institution="harbor",
    )

    # Documented CURRENT behavior: the deposit<->chequing structural pattern pairs
    # them. (If a real 'Term Deposit'/GIC account type is ever added, exclude it from
    # savings_keywords and flip this assertion to == [].)
    matches = matcher.match_transfers([term_deposit, chequing])
    assert len(matches) == 1


def test_lumen_cross_institution_payment_requires_different_institutions(fictional_payment_policy):
    """Two fictional chequing + savings deposit legs at the same institution
    legs (one description merely containing 'payment') must NOT read as a
    cross-institution payment (F9 -- the link signal must key off a distinct
    counterparty, not the leg's own institution)."""
    matcher = TransferMatcher()
    a = _transfer(
        1,
        Decimal("-500"),
        "lumen-chequing",
        "Chequing",
        date(2025, 1, 10),
        description="Payment to card",
        institution="lumen",
    )
    b = _transfer(
        2,
        Decimal("500"),
        "lumen-savings",
        "Savings",
        date(2025, 1, 10),
        description="Transfer in",
        institution="lumen",
    )
    assert matcher._is_cross_institution_payment(a, b) is False


def test_lumen_to_harbor_remains_cross_institution_payment(fictional_payment_policy):
    """A genuine cross-institution Lumen->HARBOR card payment must still register."""
    matcher = TransferMatcher()
    a = _transfer(
        1,
        Decimal("-500"),
        "lumen-chequing",
        "Chequing",
        date(2025, 1, 10),
        description="Payment",
        institution="lumen",
    )
    b = _transfer(
        2,
        Decimal("-500"),
        "harbor-visa",
        "Visa",
        date(2025, 1, 10),
        description="Payment thank you",
        institution="harbor",
    )
    assert matcher._is_cross_institution_payment(a, b) is True


def _transfer_ccy(
    i: int,
    amount_raw: Decimal,
    account: str,
    account_type: str,
    posting_date: date,
    currency: str,
    description: str = "TRANSFER",
    institution: str = "harbor",
) -> Transaction:
    """Same as `_transfer` but with an explicit currency (default fixture is CAD-only)."""
    txn = _transfer(i, amount_raw, account, account_type, posting_date, description, institution)
    return txn.model_copy(update={"currency": currency})


def test_match_transfers_rejects_cross_currency_legs():
    """M5a: two equal-magnitude opposite-sign legs in DIFFERENT currencies must NOT
    pair, even though they carry a savings<->chequing account-type link signal.

    A USD leg and a CAD leg of the same magnitude are not the two ends of one
    transfer -- without FX they cannot be reconciled, so pairing them writes a bogus
    counterparty and inflates match_rate. Pre-fix the matcher never referenced
    currency and paired them at ~0.87 via the savings<->chequing account-type link.
    """
    matcher = TransferMatcher()
    usd = _transfer_ccy(
        1, Decimal("-500.00"), "harbor-chequing", "Chequing", date(2025, 1, 10), "USD"
    )
    cad = _transfer_ccy(2, Decimal("500.00"), "harbor-savings", "Savings", date(2025, 1, 10), "CAD")

    assert matcher.match_transfers([usd, cad]) == []


def test_match_transfers_pairs_same_currency_legs_after_guard():
    """M5a companion: the currency guard must NOT over-block -- two otherwise-identical
    same-currency legs (both USD) with the savings<->chequing link still pair."""
    matcher = TransferMatcher()
    out = _transfer_ccy(
        1, Decimal("-500.00"), "harbor-chequing", "Chequing", date(2025, 1, 10), "USD"
    )
    inn = _transfer_ccy(2, Decimal("500.00"), "harbor-savings", "Savings", date(2025, 1, 10), "USD")

    assert len(matcher.match_transfers([out, inn])) == 1


def test_match_transfers_rejects_shared_trailing_year():
    """M5b: two opposite-sign equal-amount legs to DIFFERENT counterparties whose
    descriptions merely both end in a calendar year (2024) must NOT pair.

    A bare shared trailing 3-4 digit run that is a plausible calendar year is NOT a
    genuine reference number. Pre-fix `_reference_bonus` returned 0.15 for the shared
    '2024' and `_has_link_signal` accepted that as a standalone gate-clearing signal,
    so these scored ~1.0 and wrongly paired.
    """
    matcher = TransferMatcher()
    a = _transfer(
        1,
        Decimal("-410.00"),
        "harbor-chequing",
        "Chequing",
        date(2025, 6, 3),
        description="Online transfer to John 2024",
        institution="harbor",
    )
    b = _transfer(
        2,
        Decimal("410.00"),
        "birch-1",
        "Chequing",
        date(2025, 6, 3),
        description="Deposit from Acme 2024",
        institution="birch",
    )

    assert matcher.match_transfers([a, b]) == []


def test_match_transfers_shared_non_year_reference_still_links():
    """M5b companion: a genuine shared non-year trailing reference (9816) must STILL
    link two otherwise-unconnected legs (no account-type / cross-institution signal),
    proving the tightening excluded only year-like values, not references at large."""
    matcher = TransferMatcher()
    a = _transfer(
        1,
        Decimal("-410.00"),
        "harbor-chequing",
        "Chequing",
        date(2025, 6, 3),
        description="Online transfer to John 9816",
        institution="harbor",
    )
    b = _transfer(
        2,
        Decimal("410.00"),
        "birch-1",
        "Chequing",
        date(2025, 6, 3),
        description="Deposit from Acme 9816",
        institution="birch",
    )

    assert len(matcher.match_transfers([a, b])) == 1


pytestmark = pytest.mark.usefixtures("fictional_adapter_metadata")
