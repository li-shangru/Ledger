"""Focused transfer query module."""

from __future__ import annotations

from datetime import date

from sqlalchemy import func

from ledger.adapters.registry import CompatibilityPolicySnapshot, compatibility_policy_snapshot
from ledger.core import Transaction
from ledger.query.base import QueryModule
from ledger.storage.database import TransferDB
from ledger.storage.mappers import transfer_from_db


def _order_outflow_inflow(
    first: Transaction, second: Transaction, *, policy: CompatibilityPolicySnapshot | None = None
) -> tuple[Transaction, Transaction]:
    """Return (outflow_source, inflow_destination) for a matched transfer pair.

    Direction must NOT be reconstructed from the native amount sign. These mapped
    transfer objects carry ``amount_signed == amount_raw`` (the native amount, for
    display magnitude), and ``amount_raw`` carries the institution's NATIVE sign,
    which differs across issuers (deposit and card accounts may both show a
    payment as negative), so two legs of one cross-institution
    payment can share the same sign -- making sign-ordering arbitrary (M7).

    Reliable signal:
    - If exactly one leg is a credit-card account, that leg is the DESTINATION
      (money flows INTO the card to pay it down); the other is the source. This is
      the same ``is_credit_leg`` predicate the matcher uses for its same-sign
      deposit->card exception.
    - Otherwise (deposit<->deposit) the matcher required opposite signs, so the
      negative-``amount_raw`` leg is the outflow and the positive leg is the inflow.
    """
    # Imported lazily: ledger.analysis.__init__ pulls in health_scorer, which
    # imports ledger.query, so a module-level import here would be circular.
    from ledger.analysis.transfer_matcher import is_credit_leg

    snapshot = policy if policy is not None else compatibility_policy_snapshot()
    first_is_card = is_credit_leg(first, policy=snapshot)
    second_is_card = is_credit_leg(second, policy=snapshot)
    if first_is_card != second_is_card:
        # The credit-card leg is the destination (inflow); the funding deposit leg
        # is the source (outflow). Reliable even when both legs share a sign.
        return (second, first) if first_is_card else (first, second)
    # Deposit<->deposit (or two card legs, which the matcher does not pair): fall
    # back to the natural sign, which is reliable here because these legs are
    # opposite-signed.
    return (first, second) if first.amount_raw < 0 else (second, first)


class TransferQueries(QueryModule):
    """Transfer query namespace used by the compatibility facade."""

    def get_transfer_matches(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[tuple[Transaction, Transaction]]:
        """Return matched transfer pairs using one inert interpretation snapshot."""
        policy = compatibility_policy_snapshot()
        with self._open_session() as session:
            query = session.query(TransferDB).filter(TransferDB.paired_transfer_id.isnot(None))
            if start_date:
                query = query.filter(TransferDB.posting_date >= start_date)
            if end_date:
                query = query.filter(TransferDB.posting_date <= end_date)

            first_rows = query.all()

            # Batch the partner lookups into a single IN query instead of one
            # query per leg (was an N+1). A partner may live outside the date
            # window, so look partners up unconditionally by id.
            partner_ids = {row.paired_transfer_id for row in first_rows}
            partner_rows = (
                session.query(TransferDB).filter(TransferDB.txn_id.in_(partner_ids)).all()
                if partner_ids
                else []
            )
            by_id = {row.txn_id: row for row in partner_rows}

            pairs = []
            seen = set()
            for first_db in first_rows:
                if first_db.txn_id in seen:
                    continue
                if first_db.paired_transfer_id is None:
                    continue
                second_db = by_id.get(first_db.paired_transfer_id)
                if second_db is None:
                    continue
                seen.add(first_db.txn_id)
                seen.add(second_db.txn_id)
                first = transfer_from_db(first_db)
                second = transfer_from_db(second_db)
                # Direction (which leg is the outflow vs inflow) is reconstructed
                # from a reliable signal (credit-card leg = destination; else the
                # opposite-signed deposit legs), NOT the native amount sign, which
                # is unreliable across institutions (M7).
                pairs.append(_order_outflow_inflow(first, second, policy=policy))
            return pairs

    def get_unmatched_transfers(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> list[Transaction]:
        """Return transfers that are not paired."""
        with self._open_session() as session:
            query = session.query(TransferDB).filter(TransferDB.paired_transfer_id.is_(None))
            if start_date:
                query = query.filter(TransferDB.posting_date >= start_date)
            if end_date:
                query = query.filter(TransferDB.posting_date <= end_date)
            return [
                transfer_from_db(row)
                for row in query.order_by(TransferDB.posting_date.desc()).all()
            ]

    def get_transfer_stats(
        self,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> dict:
        """Return transfer matching counts and match rate.

        ``matched_pairs`` is counted by canonical pair key (the unordered
        {txn_id, paired_transfer_id} set), not as ``matched_legs // 2``. The
        old leg//2 rule undercounted pairs whose two legs straddle the date
        boundary: one leg inside the window contributed an odd leg count that
        ``// 2`` floored to zero, hiding a real matched pair.
        """
        with self._open_session() as session:
            total_query = session.query(func.count(TransferDB.txn_id))
            matched_query = session.query(TransferDB.txn_id, TransferDB.paired_transfer_id).filter(
                TransferDB.paired_transfer_id.isnot(None)
            )
            if start_date:
                total_query = total_query.filter(TransferDB.posting_date >= start_date)
                matched_query = matched_query.filter(TransferDB.posting_date >= start_date)
            if end_date:
                total_query = total_query.filter(TransferDB.posting_date <= end_date)
                matched_query = matched_query.filter(TransferDB.posting_date <= end_date)

            total = total_query.scalar() or 0

            matched_legs = matched_query.all()
            matched = len(matched_legs)
            # Dedup by canonical pair key so a pair is counted once regardless of
            # how many of its legs fall inside the window.
            pair_keys = {frozenset((txn_id, paired_id)) for txn_id, paired_id in matched_legs}
            return {
                "total_transfers": total,
                "matched_pairs": len(pair_keys),
                "matched_transfers": matched,
                "unmatched_transfers": total - matched,
                "match_rate": matched / total if total > 0 else 0.0,
            }
