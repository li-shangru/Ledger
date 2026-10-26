"""Preserve durable transaction overrides across identity changes."""

from __future__ import annotations

import posixpath
from collections import Counter, defaultdict
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy.orm import Session

from ledger.core.enums import Confidence
from ledger.storage.database import TransactionDB
from ledger.utils.money import quantize_cents

_DURABLE_CONFIDENCES = frozenset((Confidence.MANUAL.value, Confidence.RULE.value))


@dataclass(frozen=True, slots=True)
class DurableOverride:
    """Old-row identity plus any fields eligible to survive a rebuild."""

    txn_id: str
    source_path: str
    source_row: int
    institution: str
    posting_date: date
    currency: str
    amount_raw: Decimal
    merchant: str
    merchant_confidence: str
    category: str
    category_confidence: str
    exclude_from_spending: bool

    @property
    def has_durable_value(self) -> bool:
        return (
            self.merchant_confidence in _DURABLE_CONFIDENCES
            or self.category_confidence in _DURABLE_CONFIDENCES
        )

    @property
    def has_manual_value(self) -> bool:
        return (
            self.merchant_confidence == Confidence.MANUAL.value
            or self.category_confidence == Confidence.MANUAL.value
        )

    @property
    def source_coordinate(self) -> tuple[str, int]:
        return (normalize_source_path(self.source_path), self.source_row)


def collect_durable_overrides(
    session: Session,
    *,
    source_paths: Collection[str] | None = None,
) -> tuple[DurableOverride, ...]:
    """Snapshot all old identities and their potentially durable fields.

    ``source_paths`` is matched lexically after slash and dot-segment normalization.
    Non-durable rows are included so their unchanged IDs and source coordinates cannot
    be claimed by a different row's MANUAL/RULE fallback. Treatments, offsets, and
    other derived analysis state remain excluded and are recomputed post-processing.
    """

    rows = session.query(
        TransactionDB.txn_id,
        TransactionDB.source_path,
        TransactionDB.source_row,
        TransactionDB.institution,
        TransactionDB.posting_date,
        TransactionDB.currency,
        TransactionDB.amount_raw,
        TransactionDB.merchant,
        TransactionDB.merchant_confidence,
        TransactionDB.category,
        TransactionDB.category_confidence,
        TransactionDB.exclude_from_spending,
    ).all()
    normalized_paths = (
        {normalize_source_path(path) for path in source_paths} if source_paths is not None else None
    )

    return tuple(
        DurableOverride(
            txn_id=row.txn_id,
            source_path=row.source_path,
            source_row=row.source_row,
            institution=row.institution,
            posting_date=row.posting_date,
            currency=row.currency,
            amount_raw=quantize_cents(Decimal(row.amount_raw)),
            merchant=row.merchant,
            merchant_confidence=str(row.merchant_confidence),
            category=row.category,
            category_confidence=str(row.category_confidence),
            exclude_from_spending=row.exclude_from_spending,
        )
        for row in rows
        if normalized_paths is None or normalize_source_path(row.source_path) in normalized_paths
    )


def reconcile_durable_overrides(
    session: Session,
    overrides: Sequence[DurableOverride],
    *,
    target_txn_ids: Collection[str] | None = None,
) -> int:
    """Map and apply durable fields to rebuilt rows in ``session``.

    An unchanged ``txn_id`` wins immediately. Changed IDs may fall back only to a
    one-to-one normalized ``(source_path, source_row)`` match with equal institution,
    posting date, normalized currency, and cent-quantized raw amount. Account and
    description are deliberately not guards because parser/account-identity changes
    may legitimately alter them.

    Unsafe MANUAL rows abort before any override is applied. RULE-only rows are
    best-effort and are applied only when a safe target exists.
    """

    if not overrides:
        return 0

    session.flush()
    query = session.query(TransactionDB)
    if target_txn_ids is not None:
        target_ids = tuple(dict.fromkeys(target_txn_ids))
        targets = query.filter(TransactionDB.txn_id.in_(target_ids)).all() if target_ids else []
    else:
        targets = query.all()

    targets_by_id = {row.txn_id: row for row in targets}
    targets_by_coordinate: dict[tuple[str, int], list[TransactionDB]] = defaultdict(list)
    for row in targets:
        targets_by_coordinate[(normalize_source_path(row.source_path), row.source_row)].append(row)

    matches: list[tuple[DurableOverride, TransactionDB]] = []
    claimed_target_ids: set[str] = set()
    fallback_overrides: list[DurableOverride] = []

    for override in overrides:
        target = targets_by_id.get(override.txn_id)
        if target is None:
            fallback_overrides.append(override)
            continue
        claimed_target_ids.add(target.txn_id)
        if override.has_durable_value:
            matches.append((override, target))

    source_coordinate_counts = Counter(
        override.source_coordinate for override in fallback_overrides
    )
    manual_failures: list[tuple[str, str]] = []

    for override in fallback_overrides:
        if not override.has_durable_value:
            continue
        coordinate = override.source_coordinate
        candidates = [
            row
            for row in targets_by_coordinate.get(coordinate, ())
            if row.txn_id not in claimed_target_ids
        ]
        reason: str | None = None
        if source_coordinate_counts[coordinate] != 1 or len(candidates) > 1:
            reason = "ambiguous source coordinate"
        elif not candidates:
            reason = "no source-coordinate target"
        elif not _guards_match(override, candidates[0]):
            reason = "identity guard mismatch"

        if reason is not None:
            if override.has_manual_value:
                manual_failures.append((override.txn_id, reason))
            continue

        target = candidates[0]
        matches.append((override, target))
        claimed_target_ids.add(target.txn_id)

    if manual_failures:
        details = ", ".join(f"{txn_id}: {reason}" for txn_id, reason in manual_failures)
        raise RuntimeError(
            f"Cannot safely reconcile {len(manual_failures)} MANUAL override row(s): {details}"
        )

    for override, target in matches:
        if override.category_confidence in _DURABLE_CONFIDENCES:
            target.category = override.category
            target.category_confidence = override.category_confidence
            target.exclude_from_spending = override.exclude_from_spending
        if override.merchant_confidence in _DURABLE_CONFIDENCES:
            target.merchant = override.merchant
            target.merchant_confidence = override.merchant_confidence

    return len(matches)


def normalize_source_path(source_path: str) -> str:
    """Return a platform-independent lexical source-path identity."""

    return posixpath.normpath(source_path.replace("\\", "/"))


def _guards_match(override: DurableOverride, target: TransactionDB) -> bool:
    return (
        override.institution == target.institution
        and override.posting_date == target.posting_date
        and _normalize_currency(override.currency) == _normalize_currency(target.currency)
        and override.amount_raw == quantize_cents(Decimal(target.amount_raw))
    )


def _normalize_currency(currency: str) -> str:
    return currency.strip().upper()
