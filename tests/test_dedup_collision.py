"""deduplicate() must surface hash collisions (same txn_id, different content).

A collision means two genuinely different transactions hash to the same txn_id.
Strict mode (the default) must raise rather than silently drop one.
Non-strict mode must PRESERVE both distinct transactions via disambiguation,
not silently drop the colliding row.
"""

from datetime import date
from decimal import Decimal

import pytest

from ledger.core import Transaction
from ledger.core.exceptions import CollisionError
from ledger.processing.pipeline import deduplicate, disambiguate_within_file


def _txn(txn_id: str, fingerprint: str, row: int) -> Transaction:
    return Transaction(
        txn_id=txn_id,
        txn_fingerprint=fingerprint,
        posting_date=date(2025, 1, 15),
        institution="harbor",
        account="harbor-chequing",
        flow="expense",
        cashflow_type="expense",
        include_in_cashflow=True,
        currency="CAD",
        amount_raw=Decimal("10.00"),
        amount_signed=Decimal("-10.00"),
        description=f"ROW {row}",
        source_file="x.csv",
        source_path="x.csv",
        source_period="2025-01",
        source_row=row,
        imported_at=date(2025, 1, 16),
        parser_version="test",
    )


def test_strict_mode_raises_on_collision():
    items = [_txn("dup-id", "fp_a", 1), _txn("dup-id", "fp_b", 2)]
    with pytest.raises(CollisionError):
        deduplicate(items, strict=True)


def test_non_strict_mode_keeps_first_and_counts_collision():
    """Legacy test: collision counter is still incremented in non-strict mode."""
    items = [_txn("dup-id", "fp_a", 1), _txn("dup-id", "fp_b", 2)]

    unique, _duplicates, collisions = deduplicate(items, strict=False)

    # Both distinct transactions must be preserved (the colliding row is disambiguated).
    assert len(unique) == 2
    # The collision counter must still be incremented so the caller knows it happened.
    assert collisions == 1


def test_non_strict_collision_both_transactions_preserved():
    """Non-strict collision MUST preserve BOTH distinct transactions.

    Two genuinely-distinct transactions that happen to hash to the same txn_id
    (same id, DIFFERENT fingerprint) must BOTH survive deduplicate() in non-strict
    mode.  Previously the second was silently dropped — data loss.

    Content-keyed scheme (post-A15 fix): BOTH rows are remapped to
    stable_id(original_id, fingerprint) — including the first-seen row.
    This achieves full order-independence (no "bare id" preference).
    """
    txn_a = _txn("collide-id", "fp_alpha", 1)  # amount_raw=10, description="ROW 1"
    txn_b = _txn("collide-id", "fp_beta", 2)  # amount_raw=10, description="ROW 2"

    unique, _duplicates, collisions = deduplicate([txn_a, txn_b], strict=False)

    # Both transactions must survive.
    assert len(unique) == 2, (
        f"Expected 2 (both distinct txns preserved), got {len(unique)}. "
        "The colliding transaction was silently dropped — data loss!"
    )
    # Their txn_ids must be distinct (disambiguated).
    ids = {t.txn_id for t in unique}
    assert len(ids) == 2, f"txn_ids are not distinct after disambiguation: {ids}"
    # Neither row retains the original bare id — both are content-keyed.
    # (This is intentional: it achieves full order-independence.)
    assert "collide-id" not in ids, (
        f"Expected bare 'collide-id' to be remapped in content-keyed scheme, but it survived: {ids}"
    )
    # Fingerprints must remain distinct (each row has a content-keyed fingerprint).
    fps = {t.txn_fingerprint for t in unique}
    assert len(fps) == 2

    # Collision counter must be incremented.
    assert collisions == 1


def test_non_strict_collision_disambiguated_id_is_deterministic():
    """Re-running deduplicate on the same collision input produces the same ids.

    This ensures re-imports are idempotent: the disambiguated txn_id for the
    colliding row must be stable across runs.
    """
    items_a = [_txn("collide-id", "fp_alpha", 1), _txn("collide-id", "fp_beta", 2)]
    items_b = [_txn("collide-id", "fp_alpha", 1), _txn("collide-id", "fp_beta", 2)]

    unique_a, _, _ = deduplicate(items_a, strict=False)
    unique_b, _, _ = deduplicate(items_b, strict=False)

    assert sorted(t.txn_id for t in unique_a) == sorted(t.txn_id for t in unique_b)
    assert sorted(t.txn_fingerprint for t in unique_a) == sorted(
        t.txn_fingerprint for t in unique_b
    )


def test_non_strict_triple_collision_all_preserved():
    """Three distinct transactions that all hash to the same txn_id must all survive."""
    txn_a = _txn("triple-id", "fp_x", 1)
    txn_b = _txn("triple-id", "fp_y", 2)
    txn_c = _txn("triple-id", "fp_z", 3)

    unique, _, collisions = deduplicate([txn_a, txn_b, txn_c], strict=False)

    assert len(unique) == 3, f"Expected 3 distinct txns, got {len(unique)}"
    assert len({t.txn_id for t in unique}) == 3
    assert collisions == 2  # Two collisions (rows 2 and 3 each collide with row 1)


def test_disambiguate_keeps_first_and_resolves_identical_rows():
    """Two genuinely-distinct identical-content rows in one file must survive.

    Same txn_id AND same fingerprint (two identical coffees). The first keeps
    its id (no migration); the second is re-hashed so it is a distinct row.
    """
    a = _txn("same-id", "same-fp", 1)
    b = _txn("same-id", "same-fp", 2)

    out = disambiguate_within_file([a, b])

    assert len(out) == 2
    # First occurrence is untouched.
    assert out[0].txn_id == "same-id"
    assert out[0].txn_fingerprint == "same-fp"
    # Second occurrence is re-identified -> distinct id and fingerprint.
    assert out[1].txn_id != "same-id"
    assert out[1].txn_fingerprint != "same-fp"
    assert out[0].txn_id != out[1].txn_id

    # And deduplicate must now keep BOTH rows, with no duplicates/collisions.
    unique, dups, cols = deduplicate(out, strict=True)
    assert len(unique) == 2
    assert dups == 0
    assert cols == 0


def test_disambiguate_is_deterministic_for_reimport():
    """Re-importing the same file reproduces the same ids (idempotency)."""
    first = disambiguate_within_file([_txn("same-id", "same-fp", 1), _txn("same-id", "same-fp", 2)])
    second = disambiguate_within_file(
        [_txn("same-id", "same-fp", 1), _txn("same-id", "same-fp", 2)]
    )

    assert [t.txn_id for t in first] == [t.txn_id for t in second]
    assert [t.txn_fingerprint for t in first] == [t.txn_fingerprint for t in second]


def test_disambiguate_three_identical_rows():
    """Three rows with the same txn_id/fingerprint must each get a distinct id.

    Guards the occurrence-index increment against an off-by-one: if n were not
    incremented before the second duplicate, the 2nd and 3rd occurrences would
    hash identically and one would be silently dropped by deduplicate().
    """
    a = _txn("same-id", "same-fp", 1)
    b = _txn("same-id", "same-fp", 2)
    c = _txn("same-id", "same-fp", 3)

    out = disambiguate_within_file([a, b, c])

    assert len(out) == 3
    ids = [t.txn_id for t in out]
    fps = [t.txn_fingerprint for t in out]
    # All three ids must be distinct.
    assert len(set(ids)) == 3, f"Expected 3 distinct ids, got: {ids}"
    # All three fingerprints must also be distinct.
    assert len(set(fps)) == 3, f"Expected 3 distinct fingerprints, got: {fps}"
    # First occurrence is untouched.
    assert out[0].txn_id == "same-id"
    assert out[0].txn_fingerprint == "same-fp"

    # After disambiguation, deduplicate must keep all three with no drops or collisions.
    unique, dups, cols = deduplicate(out, strict=True)
    assert len(unique) == 3
    assert dups == 0
    assert cols == 0


def test_disambiguate_three_rows_is_deterministic():
    """Same three rows disambiguated twice produce an identical id sequence."""

    def _run():
        return disambiguate_within_file(
            [
                _txn("same-id", "same-fp", 1),
                _txn("same-id", "same-fp", 2),
                _txn("same-id", "same-fp", 3),
            ]
        )

    first = _run()
    second = _run()

    assert [t.txn_id for t in first] == [t.txn_id for t in second]
    assert [t.txn_fingerprint for t in first] == [t.txn_fingerprint for t in second]


def test_true_duplicate_still_collapses_in_non_strict():
    """A true duplicate (same txn_id AND same fingerprint) must collapse to one row.

    Non-strict collision disambiguation must not break the ordinary cross-file
    deduplication of overlapping bank statements.
    """
    txn_a = _txn("same-id", "same-fp", 1)
    txn_b = _txn("same-id", "same-fp", 2)

    unique, duplicates, collisions = deduplicate([txn_a, txn_b], strict=False)

    assert len(unique) == 1
    assert duplicates == 1
    assert collisions == 0  # No collision — they're identical content


def test_true_duplicate_still_collapses_in_strict():
    """Strict mode: true duplicate (same txn_id AND fingerprint) collapses to one row,
    no CollisionError raised (it's not a collision, it's an exact duplicate).
    """
    txn_a = _txn("same-id", "same-fp", 1)
    txn_b = _txn("same-id", "same-fp", 2)

    unique, duplicates, collisions = deduplicate([txn_a, txn_b], strict=True)

    assert len(unique) == 1
    assert duplicates == 1
    assert collisions == 0


# ---------------------------------------------------------------------------
# [3]: True-duplicate-of-a-colliding-row must collapse, not be kept as a
#      third distinct row.
# ---------------------------------------------------------------------------
def test_true_dup_of_collision_collapses():
    """[Finding 3] txn(X,fp_a), txn(X,fp_b), txn(X,fp_b) → exactly 2 rows.

    The two fp_b rows are a true duplicate of each other (same id+fingerprint).
    The positional scheme broke this by giving them DIFFERENT collision counters
    (collision:1 vs collision:2), so both survived and the result was 3 rows.
    The content-keyed fix maps (X, fp_b) deterministically to one id → they
    collapse to one row (2 rows total, not 3).
    """
    txn_a = _txn("X", "fp_a", 1)
    txn_b1 = _txn("X", "fp_b", 2)
    txn_b2 = _txn("X", "fp_b", 3)  # true duplicate of txn_b1

    unique, _duplicates, collisions = deduplicate([txn_a, txn_b1, txn_b2], strict=False)

    assert len(unique) == 2, (
        f"Expected 2 rows (true-dup-of-collision collapses), got {len(unique)}. "
        "The positional scheme gave the two fp_b rows different ids — data duplication!"
    )
    # Exactly one collision event (fp_b first appears as a collision against fp_a;
    # the second fp_b is a true duplicate of the already-disambiguated fp_b row).
    assert collisions == 1, f"Expected 1 collision, got {collisions}"


# ---------------------------------------------------------------------------
# [4]: Disambiguated ids must be content-keyed, not position-keyed, so they
#      are invariant to processing order.
# ---------------------------------------------------------------------------
def test_collision_disambiguation_is_order_independent():
    """[Finding 4] Swapping the order of colliding rows must not change the
    disambiguated id assigned to each fingerprint.

    With the positional scheme, whichever fingerprint arrives *second* gets
    collision:1 — so the id assigned to fp_b content differs between orderings.
    The content-keyed fix derives the new id from (X, fingerprint), so the
    same fingerprint always resolves to the same id regardless of input order.

    We assert: the set of (new_txn_id, txn_fingerprint) pairs is IDENTICAL for
    both orderings.  (The bare-X owner is still order-dependent — see code comment
    in deduplicate() — but the disambiguated rows are fully stable.)
    """
    txn_a = _txn("X", "fp_a", 1)
    txn_b = _txn("X", "fp_b", 2)

    unique_ab, _, _ = deduplicate([txn_a, txn_b], strict=False)
    unique_ba, _, _ = deduplicate([txn_b, txn_a], strict=False)

    pairs_ab = {(t.txn_id, t.txn_fingerprint) for t in unique_ab}
    pairs_ba = {(t.txn_id, t.txn_fingerprint) for t in unique_ba}

    assert pairs_ab == pairs_ba, (
        f"Disambiguation is order-dependent!\n"
        f"  [A,B] → {sorted(pairs_ab)}\n"
        f"  [B,A] → {sorted(pairs_ba)}\n"
        "The positional scheme assigns collision:1 to whichever fingerprint arrives "
        "second — flipping order assigns different ids to the same content."
    )
