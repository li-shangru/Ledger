"""Refuse unresolved cross-source identity without rewriting financial facts."""

from __future__ import annotations

import posixpath
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ledger.core import Transaction
    from ledger.storage.database import TransactionDB, TransferDB


class AmbiguousCrossSourceIdentityError(RuntimeError):
    """Different IDs cannot safely be counted or merged across sources."""

    code = "ambiguous_cross_source_identity"

    def __init__(self) -> None:
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class IdentityEvidence:
    """Comparison-only evidence; never a replacement ID or fingerprint."""

    txn_id: str
    source_path: str | None
    financial_identity: tuple[object, ...] | None
    # Exact fingerprint binds a witness to its final row. Its content is not a
    # financial comparison key: collision handling may legitimately rewrite it.
    txn_fingerprint: str | None = None


def _source_path(value: str | None) -> str | None:
    # Match the lexical slash/dot normalization used by statement replacement.
    # A missing source cannot prove that two IDs occurred together in a file.
    return posixpath.normpath(value.replace("\\", "/")) if value else None


def transaction_identity(row: Transaction | TransactionDB) -> IdentityEvidence:
    """Require a real fingerprint and equality of all stored financial fields.

    Enrichment and source metadata may change independently of the imported fact.
    Amounts remain exact Decimals; no rounding, arithmetic or inferred values are
    used to compare them. Ingestion already supplies canonical institution and
    account bindings: applying another case fold could conflate distinct alias
    targets. Currency is also compared as stored, without inventing a value for
    legacy missing evidence. Fingerprint content does not establish distinctness
    because collision handling may rewrite it before an earlier import is stored.
    Its exact value is retained separately to bind transient source witnesses.
    """
    identity = None
    if row.txn_fingerprint and row.txn_fingerprint.strip():
        identity = (
            row.institution,
            row.account,
            row.account_type,
            row.posting_date,
            row.transaction_date,
            row.currency,
            row.amount_raw,
            row.amount_signed,
            row.description,
            row.flow,
            row.cashflow_type,
            row.include_in_cashflow,
        )
    return IdentityEvidence(
        row.txn_id, _source_path(row.source_path), identity, txn_fingerprint=row.txn_fingerprint
    )


def transfer_identity(row: Transaction | TransferDB) -> IdentityEvidence:
    """Compare exactly the financial fields persisted for a transfer leg.

    TransferDB has no fingerprint, transaction date, signed amount or cashflow
    fields. Inventing any of them would hide sequential-import ambiguity. Two
    distinct IDs with all these stored fields equal need same-source evidence;
    different accounts, directions, currencies or other stored fields remain
    distinct. Pairing fields are derived later and cannot establish identity.
    """
    return IdentityEvidence(
        row.txn_id,
        _source_path(row.source_path),
        (
            row.institution,
            row.account,
            row.account_type,
            row.posting_date,
            row.currency,
            row.amount_raw,
            row.description,
            row.flow,
        ),
    )


def require_unambiguous_cross_source_identity(
    incoming: Iterable[IdentityEvidence],
    existing: Iterable[IdentityEvidence] = (),
    *,
    witnesses: Iterable[IdentityEvidence] = (),
) -> None:
    """Refuse a novel ID whose distinctness lacks shared-source evidence.

    Stable IDs already stored remain idempotent. Different IDs seen together in
    one source demonstrate multiplicity, including when another source repeats
    only a subset of them. Every novel ID must have such evidence against each
    other ID with the same strong financial identity; a shared intermediate ID
    cannot prove that two otherwise unresolved IDs are distinct. Transient
    witnesses may retain source occurrences discarded by deduplication, but only
    for an exact final incoming ID, full financial identity and exact fingerprint.
    Transaction comparison requires a nonempty fingerprint but ignores its
    content, so a prior collision rewrite cannot hide the same financial fact.
    """
    incoming_rows = tuple(incoming)
    if not incoming_rows:
        return
    existing_rows = tuple(existing)
    known_ids = {row.txn_id for row in existing_rows}
    novel_ids = {row.txn_id for row in incoming_rows} - known_ids
    if not novel_ids:
        return

    groups: dict[tuple[object, ...], list[IdentityEvidence]] = defaultdict(list)
    for row in (*existing_rows, *incoming_rows):
        if row.financial_identity is not None:
            groups[row.financial_identity].append(row)
    admitted = {(row.txn_id, row.financial_identity, row.txn_fingerprint) for row in incoming_rows}
    for row in witnesses:
        if (
            row.financial_identity is not None
            and (row.txn_id, row.financial_identity, row.txn_fingerprint) in admitted
        ):
            groups[row.financial_identity].append(row)

    for rows in groups.values():
        ids = {row.txn_id for row in rows}
        to_check = ids & novel_ids
        if len(ids) < 2 or not to_check:
            continue
        ids_by_source: dict[str, set[str]] = defaultdict(set)
        sources_by_id: dict[str, set[str]] = defaultdict(set)
        for row in rows:
            if row.source_path is not None:
                ids_by_source[row.source_path].add(row.txn_id)
                sources_by_id[row.txn_id].add(row.source_path)
        # The common repeated-row case is linear even for a large single file.
        if any(source_ids == ids for source_ids in ids_by_source.values()):
            continue
        for txn_id in to_check:
            cooccurring_ids = {txn_id}
            for source in sources_by_id[txn_id]:
                cooccurring_ids.update(ids_by_source[source])
            if cooccurring_ids != ids:
                raise AmbiguousCrossSourceIdentityError()
