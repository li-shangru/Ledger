"""Deterministic true-cost adjustment detection."""

from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from sqlalchemy import func
from sqlalchemy.orm import Session

from ledger.adapters.registry import CompatibilityPolicySnapshot, compatibility_policy_snapshot
from ledger.core.results import RunResult
from ledger.finance_policy import cad_sql_filters, income_sql_filters, spend_sql_filters
from ledger.storage.database import (
    TransactionDB,
    TransactionOffsetDB,
    TransactionTreatmentDB,
    session_scope,
)
from ledger.storage.migrations import ensure_schema
from ledger.storage.operations import record_db_operation
from ledger.utils.dates import naive_utc_now
from ledger.utils.money import to_cents

DETECTOR_SOURCE = "deterministic_v1"
REWARD_PATTERN = re.compile(r"cash\s*back|cashback|remises\s+rebate|reward|rebate", re.I)
CORE_CREDIT_CATEGORIES = {"Income", "Transfers", "Credit Card Payments", "Adjustments"}
AUTO_NET_POLICY = "reduce_category_cost"
EXCLUDE_POLICY = "exclude_from_income"
GROSS_POLICY = "gross_cost"
REVIEW_POLICY = "review_required"


@dataclass(frozen=True)
class TreatmentCandidate:
    txn_id: str
    treatment_type: str
    status: str
    confidence: Decimal
    netting_policy: str
    target_category: str | None
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OffsetCandidate:
    credit_txn_id: str
    debit_txn_id: str
    offset_amount: Decimal
    confidence: Decimal
    status: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AdjustmentDetectionResult:
    status: str
    started_at: datetime
    finished_at: datetime
    counts: dict[str, int]
    treatments: list[TreatmentCandidate]
    offsets: list[OffsetCandidate]
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "success" and not self.errors


@dataclass(frozen=True)
class TrueCostSummary:
    gross_spend: Decimal
    offsets: Decimal
    true_cost: Decimal
    gross_income: Decimal
    adjusted_income: Decimal
    net: Decimal
    transaction_count: int


def _now() -> datetime:
    return naive_utc_now()


def _money(value: Decimal | float | int | str | None) -> Decimal:
    return to_cents(value)


def _confidence(value: str) -> Decimal:
    return Decimal(value).quantize(Decimal("0.01"))


def _norm(value: object) -> str:
    return str(value or "").strip().upper()


def _txn_id(row: TransactionDB) -> str:
    return str(cast(Any, row.txn_id))


def _txn_dict(row: TransactionDB, *, include_raw: bool = False) -> dict[str, Any]:
    """Serialize a transaction for evidence JSON.

    ``merchant`` and ``description`` are raw, user-identifying text. Evidence JSON
    is persisted in the DB and could be surfaced by future review tooling, so the
    raw fields are gated behind ``include_raw`` (privacy-by-default), mirroring the
    parser ``include_raw`` flag. The non-PII structural fields (ids, dates, amount,
    category, account_type) always remain so evidence stays auditable.
    """
    data: dict[str, Any] = {
        "txn_id": _txn_id(row),
        "posting_date": row.posting_date.isoformat(),
        "merchant": row.merchant if include_raw else None,
        "category": row.category,
        "amount": float(_money(cast(Any, row.amount_signed))),
        "description": row.description if include_raw else None,
        "account_type": row.account_type,
    }
    return data


class AdjustmentDetectionService:
    """Detect and persist non-mutating true-cost interpretations."""

    def __init__(
        self,
        db_path: Path,
        *,
        include_raw: bool = False,
        session_scope_factory: Callable[[Path], AbstractContextManager[Session]] = session_scope,
        prepare_schema: bool = True,
    ):
        self.db_path = db_path
        # Privacy-by-default: raw merchant/description are kept out of persisted
        # evidence JSON unless explicitly opted in (mirrors the parser flag).
        self.include_raw = include_raw
        self._session_scope_factory = session_scope_factory
        if prepare_schema:
            ensure_schema(self.db_path)

    def _open_session(self) -> AbstractContextManager[Session]:
        return self._session_scope_factory(self.db_path)

    def detect(self, *, execute: bool = False) -> AdjustmentDetectionResult:
        """Detect deterministic transaction treatments, optionally persisting them."""
        started_at = _now()
        warnings: list[str] = []
        errors: list[str] = []
        treatments: list[TreatmentCandidate] = []
        offsets: list[OffsetCandidate] = []
        seen_treatments: set[str] = set()

        try:
            policy = compatibility_policy_snapshot()
            with self._open_session() as session:
                # CAD-only candidates: a non-CAD credit's amount is not FX-comparable
                # to a CAD debit, so it must not become an offset/treatment (which
                # would net a CAD purchase by a foreign-currency figure). Same
                # canonical predicate as the dashboard money aggregates.
                positives = (
                    session.query(TransactionDB)
                    .filter(*income_sql_filters())
                    .order_by(TransactionDB.posting_date, TransactionDB.txn_id)
                    .all()
                )
                insurance_premiums = (
                    session.query(TransactionDB)
                    .filter(TransactionDB.category == "Insurance")
                    .filter(*spend_sql_filters())
                    .order_by(TransactionDB.posting_date, TransactionDB.txn_id)
                    .all()
                )

                consumed: dict[str, Decimal] = {}
                for credit in positives:
                    treatment, credit_offsets = self._classify_credit(
                        session, credit, consumed, policy=policy
                    )
                    if treatment and treatment.txn_id not in seen_treatments:
                        treatments.append(treatment)
                        seen_treatments.add(treatment.txn_id)
                    for offset in credit_offsets:
                        offsets.append(offset)
                        # Track how much of each debit is already offset so a later credit
                        # (or a later spill of this one) cannot net the same purchase twice.
                        consumed[offset.debit_txn_id] = (
                            consumed.get(offset.debit_txn_id, Decimal("0")) + offset.offset_amount
                        )

                for debit in insurance_premiums:
                    debit_txn_id = _txn_id(debit)
                    if debit_txn_id in seen_treatments:
                        continue
                    treatments.append(
                        TreatmentCandidate(
                            txn_id=debit_txn_id,
                            treatment_type="insurance_premium",
                            status="informational",
                            confidence=_confidence("0.95"),
                            netting_policy=GROSS_POLICY,
                            target_category="Insurance",
                            evidence={"reason": "negative insurance payment remains gross spend"},
                        )
                    )
                    seen_treatments.add(debit_txn_id)

            counts = self._counts(treatments, offsets)
            result = AdjustmentDetectionResult(
                status="success",
                started_at=started_at,
                finished_at=_now(),
                counts=counts,
                treatments=treatments,
                offsets=offsets,
                warnings=warnings,
                errors=errors,
                details={"execute": execute, "source": DETECTOR_SOURCE},
            )
            if execute:
                self._persist(result)
            return result
        except Exception as exc:
            errors.append(str(exc))
            result = AdjustmentDetectionResult(
                status="failed",
                started_at=started_at,
                finished_at=_now(),
                counts={},
                treatments=[],
                offsets=[],
                warnings=warnings,
                errors=errors,
                details={"execute": execute, "source": DETECTOR_SOURCE},
            )
            if execute:
                record_db_operation(
                    self.db_path,
                    operation="adjustments_detect",
                    result=self._run_result(result),
                )
            return result

    def validate(self) -> RunResult:
        """Validate the persisted true-cost adjustment layer.

        Checks:
        1. Offset rows whose credit_txn_id has no matching treatment (existing check).
        2. Treatment rows whose txn_id has no matching row in ``transactions``
           (orphaned after a txn_id-changing re-import).
        3. Offset rows whose credit_txn_id or debit_txn_id has no matching row in
           ``transactions`` (orphaned after a txn_id-changing re-import).
        """
        started_at = _now()
        with self._open_session() as session:
            treatment_count = session.query(func.count(TransactionTreatmentDB.txn_id)).scalar() or 0
            offset_count = session.query(func.count(TransactionOffsetDB.id)).scalar() or 0

            # Existing check: offsets without a matching credit treatment
            orphan_offsets_no_treatment = (
                session.query(func.count(TransactionOffsetDB.id))
                .outerjoin(
                    TransactionTreatmentDB,
                    TransactionOffsetDB.credit_txn_id == TransactionTreatmentDB.txn_id,
                )
                .filter(TransactionTreatmentDB.txn_id.is_(None))
                .scalar()
                or 0
            )

            # New check: treatments whose txn_id is not in the transactions table
            orphan_treatments = (
                session.query(func.count(TransactionTreatmentDB.txn_id))
                .outerjoin(
                    TransactionDB,
                    TransactionTreatmentDB.txn_id == TransactionDB.txn_id,
                )
                .filter(TransactionDB.txn_id.is_(None))
                .scalar()
                or 0
            )

            # New check: offsets whose credit_txn_id is not in the transactions table
            orphan_offsets_credit = (
                session.query(func.count(TransactionOffsetDB.id))
                .outerjoin(
                    TransactionDB,
                    TransactionOffsetDB.credit_txn_id == TransactionDB.txn_id,
                )
                .filter(TransactionDB.txn_id.is_(None))
                .scalar()
                or 0
            )

            # New check: offsets whose debit_txn_id is not in the transactions table
            # (use aliased join to avoid collision with credit side)
            from sqlalchemy.orm import aliased

            debit_txn = aliased(TransactionDB)
            orphan_offsets_debit = (
                session.query(func.count(TransactionOffsetDB.id))
                .outerjoin(
                    debit_txn,
                    TransactionOffsetDB.debit_txn_id == debit_txn.txn_id,
                )
                .filter(debit_txn.txn_id.is_(None))
                .scalar()
                or 0
            )

        orphan_offsets = int(orphan_offsets_no_treatment)
        orphan_treatments_count = int(orphan_treatments)
        orphan_offsets_credit_count = int(orphan_offsets_credit)
        orphan_offsets_debit_count = int(orphan_offsets_debit)

        errors: list[str] = []
        if orphan_offsets:
            errors.append(f"Offset rows without credit treatment: {orphan_offsets}")
        if orphan_treatments_count:
            errors.append(f"Treatment rows with no matching transaction: {orphan_treatments_count}")
        if orphan_offsets_credit_count:
            errors.append(f"Offset rows with orphaned credit_txn_id: {orphan_offsets_credit_count}")
        if orphan_offsets_debit_count:
            errors.append(f"Offset rows with orphaned debit_txn_id: {orphan_offsets_debit_count}")

        result = RunResult(
            status="success" if not errors else "failed",
            started_at=started_at,
            finished_at=_now(),
            counts={
                "treatments": int(treatment_count),
                "offsets": int(offset_count),
                "orphan_offsets": orphan_offsets,
                "orphan_treatments": orphan_treatments_count,
                "orphan_offsets_credit": orphan_offsets_credit_count,
                "orphan_offsets_debit": orphan_offsets_debit_count,
            },
            errors=errors,
        )
        record_db_operation(self.db_path, operation="adjustments_validate", result=result)
        return result

    def true_cost_summary(self, start_date: date, end_date: date) -> TrueCostSummary:
        """Return gross and adjusted income/spending totals for a period."""
        with self._open_session() as session:
            # M7: route the spend side through the ONE canonical spend universe
            # (finance_policy.spend_sql_filters) instead of re-deriving
            # amount_signed<0 + include + exclude + CAD, so the SQL spend
            # definition lives in a single place. cashflow_type=="expense" is
            # equivalent to the old amount_signed<0 expense predicate under the
            # enforced invariant that a transfer is never include_in_cashflow, and
            # it still carries the CAD-only money predicate (non-CAD un-FX-converted
            # rows must never be summed into a CAD total).
            gross_spend = _money(
                session.query(func.sum(func.abs(TransactionDB.amount_signed)))
                .filter(TransactionDB.posting_date >= start_date)
                .filter(TransactionDB.posting_date <= end_date)
                .filter(*spend_sql_filters())
                .scalar()
            )
            gross_income = _money(
                session.query(func.sum(TransactionDB.amount_signed))
                .filter(TransactionDB.posting_date >= start_date)
                .filter(TransactionDB.posting_date <= end_date)
                .filter(*income_sql_filters())
                .scalar()
            )
            transaction_count = (
                session.query(func.count(TransactionDB.txn_id))
                .filter(TransactionDB.posting_date >= start_date)
                .filter(TransactionDB.posting_date <= end_date)
                .filter(TransactionDB.include_in_cashflow == True)  # noqa: E712
                .filter(*cad_sql_filters())
                .scalar()
                or 0
            )
            offsets = self._period_offset_total(session, start_date, end_date)
            credit_offsets = self._period_credit_offset_total(session, start_date, end_date)
            excluded_income = self._period_excluded_income_total(session, start_date, end_date)

        true_cost = max(gross_spend - offsets, Decimal("0.00"))
        # Income is netted by the CREDIT-scoped offset total: a refund/reimbursement credit
        # is not real income, and it must be removed in the credit's OWN period. The
        # debit-scoped `offsets` above stays reserved for the expense-side true_cost, so a
        # cross-period refund no longer inflates this period's income (H6).
        adjusted_income = max(gross_income - credit_offsets - excluded_income, Decimal("0.00"))
        return TrueCostSummary(
            gross_spend=gross_spend,
            offsets=offsets,
            true_cost=true_cost,
            gross_income=gross_income,
            adjusted_income=adjusted_income,
            net=adjusted_income - true_cost,
            transaction_count=int(transaction_count),
        )

    def true_cost_by_category(
        self, start_date: date, end_date: date
    ) -> dict[str, dict[str, Decimal]]:
        """Return gross spend, offsets, and true cost by target category."""
        with self._open_session() as session:
            gross_rows = (
                session.query(
                    TransactionDB.category,
                    func.sum(func.abs(TransactionDB.amount_signed)).label("gross_spend"),
                    func.count(TransactionDB.txn_id).label("transaction_count"),
                )
                .filter(TransactionDB.posting_date >= start_date)
                .filter(TransactionDB.posting_date <= end_date)
                # M7: the ONE canonical spend universe (expense + include +
                # exclude + CAD-only) instead of hand-rolling the predicates, so a
                # USD or excluded debit never inflates a CAD category total and the
                # SQL spend definition stays in a single place.
                .filter(*spend_sql_filters())
                .group_by(TransactionDB.category)
                .all()
            )
            offset_rows = (
                session.query(
                    TransactionDB.category,
                    func.sum(TransactionOffsetDB.offset_amount).label("offsets"),
                )
                .select_from(TransactionOffsetDB)
                .join(TransactionDB, TransactionDB.txn_id == TransactionOffsetDB.debit_txn_id)
                .filter(TransactionOffsetDB.status == "auto_applied")
                .filter(TransactionOffsetDB.source == DETECTOR_SOURCE)
                .filter(TransactionDB.posting_date >= start_date)
                .filter(TransactionDB.posting_date <= end_date)
                .filter(TransactionDB.include_in_cashflow == True)  # noqa: E712
                .filter(TransactionDB.exclude_from_spending == False)  # noqa: E712
                # Same CAD-only scope as gross_rows so an offset's category total can
                # never include a non-CAD debit the gross side excluded.
                .filter(*cad_sql_filters())
                .group_by(TransactionDB.category)
                .all()
            )

        rows: dict[str, dict[str, Decimal]] = {
            row.category: {
                "gross_spend": _money(row.gross_spend),
                "offsets": Decimal("0.00"),
                "true_cost": _money(row.gross_spend),
                "transaction_count": Decimal(str(row.transaction_count or 0)),
            }
            for row in gross_rows
        }
        for row in offset_rows:
            category = str(row.category)
            values = rows.setdefault(
                category,
                {
                    "gross_spend": Decimal("0.00"),
                    "offsets": Decimal("0.00"),
                    "true_cost": Decimal("0.00"),
                    "transaction_count": Decimal("0"),
                },
            )
            values["offsets"] += _money(row.offsets)

        for values in rows.values():
            values["offsets"] = _money(values["offsets"])
            values["true_cost"] = max(
                _money(values["gross_spend"]) - values["offsets"],
                Decimal("0.00"),
            )
        return rows

    def treatment_rows(self, limit: int = 200) -> list[dict[str, Any]]:
        """Return recent persisted treatment rows for review surfaces."""
        with self._open_session() as session:
            rows = (
                session.query(TransactionTreatmentDB, TransactionDB)
                .join(TransactionDB, TransactionDB.txn_id == TransactionTreatmentDB.txn_id)
                .order_by(TransactionDB.posting_date.desc(), TransactionTreatmentDB.txn_id)
                .limit(limit)
                .all()
            )
            return [
                {
                    "Date": txn.posting_date,
                    "Merchant": txn.merchant,
                    "Category": txn.category,
                    "Amount": float(_money(txn.amount_signed)),
                    "Treatment": treatment.treatment_type,
                    "Status": treatment.status,
                    "Policy": treatment.netting_policy,
                    "Target Category": treatment.target_category or "",
                    "Confidence": float(treatment.confidence),
                }
                for treatment, txn in rows
            ]

    def annotation_map(self, txn_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Return export-safe adjustment annotations keyed by transaction id.

        Treatment fields describe the treated (credit) transaction. ``true_cost_offset``
        is attached to each DEBIT — the per-debit capped/spilled offset actually applied
        (sum of TransactionOffsetDB.offset_amount), not the credit's full amount — so the
        export's per-category offsets/true_cost match true_cost_by_category. A reimbursement
        is allocated across several debits and each offset is capped at the debit's headroom,
        so the credit's full amount over-stated the offset and attributed it to the wrong
        (credit's) category.
        """
        if not txn_ids:
            return {}
        empty: dict[str, Any] = {
            "treatment_type": "",
            "treatment_status": "",
            "netting_policy": "",
            "target_category": "",
            "true_cost_offset": 0.0,
        }
        annotations: dict[str, dict[str, Any]] = {}
        with self._open_session() as session:
            treatments = (
                session.query(TransactionTreatmentDB)
                .filter(TransactionTreatmentDB.txn_id.in_(txn_ids))
                .all()
            )
            for treatment in treatments:
                annotations[str(treatment.txn_id)] = {
                    "treatment_type": treatment.treatment_type,
                    "treatment_status": treatment.status,
                    "netting_policy": treatment.netting_policy,
                    "target_category": treatment.target_category,
                    "true_cost_offset": 0.0,
                }
            # Mirror the authoritative dashboard offset query (true_cost_by_category /
            # _period_offset_total): join each offset to its DEBIT and keep only
            # spend-universe rows, so the EXPORT's per-debit true_cost_offset matches
            # the DASHBOARD's per-category offsets. Without these filters an offset
            # whose debit is exclude_from_spending / not-in-cashflow would appear in
            # the export but not the dashboard, diverging the two.
            offset_rows = (
                session.query(
                    TransactionOffsetDB.debit_txn_id,
                    func.sum(TransactionOffsetDB.offset_amount).label("offset"),
                )
                .select_from(TransactionOffsetDB)
                .join(TransactionDB, TransactionDB.txn_id == TransactionOffsetDB.debit_txn_id)
                .filter(TransactionOffsetDB.debit_txn_id.in_(txn_ids))
                .filter(TransactionOffsetDB.status == "auto_applied")
                .filter(TransactionOffsetDB.source == DETECTOR_SOURCE)
                .filter(TransactionDB.include_in_cashflow == True)  # noqa: E712
                .filter(TransactionDB.exclude_from_spending == False)  # noqa: E712
                # CAD-only, mirroring true_cost_by_category so the export's per-debit
                # offset never diverges from the dashboard on a non-CAD debit.
                .filter(*cad_sql_filters())
                .group_by(TransactionOffsetDB.debit_txn_id)
                .all()
            )
            for row in offset_rows:
                entry = annotations.setdefault(str(row.debit_txn_id), dict(empty))
                entry["true_cost_offset"] = float(_money(row.offset))
        return annotations

    def _classify_credit(
        self,
        session,
        credit: TransactionDB,
        consumed: dict[str, Decimal] | None = None,
        *,
        policy: CompatibilityPolicySnapshot,
    ) -> tuple[TreatmentCandidate | None, list[OffsetCandidate]]:
        merchant = _norm(credit.merchant)
        description = _norm(credit.description)
        category = str(credit.category or "")

        if REWARD_PATTERN.search(description):
            return (
                TreatmentCandidate(
                    txn_id=_txn_id(credit),
                    treatment_type="reward_rebate",
                    status="auto_applied",
                    confidence=_confidence("0.95"),
                    netting_policy=EXCLUDE_POLICY,
                    target_category=None,
                    evidence={
                        "reason": "cashback/reward/rebate credit",
                        "credit": _txn_dict(credit, include_raw=self.include_raw),
                    },
                ),
                [],
            )

        if merchant in policy.reimbursement_merchants:
            target_category = self._reimbursement_target(session, credit) or "Health & Wellness"
            offsets = self._best_offsets(
                session, credit, category=target_category, consumed=consumed, match_merchant=False
            )
            # Only an actually-offsetting reimbursement may claim it reduces a
            # category's cost. An unmatched reimbursement offsets nothing and is
            # never removed from income, so flagging it auto_applied /
            # reduce_category_cost would double-count it (income AND cost reducer).
            if offsets:
                status = "auto_applied"
                netting_policy = AUTO_NET_POLICY
                confidence = offsets[0].confidence
            else:
                status = "needs_review"
                netting_policy = REVIEW_POLICY
                confidence = _confidence("0.90")
            return (
                TreatmentCandidate(
                    txn_id=_txn_id(credit),
                    treatment_type="reimbursement",
                    status=status,
                    confidence=confidence,
                    netting_policy=netting_policy,
                    target_category=target_category,
                    evidence={
                        "reason": "known reimbursement source",
                        "credit": _txn_dict(credit, include_raw=self.include_raw),
                        "matched_debit": [o.debit_txn_id for o in offsets] or None,
                    },
                ),
                offsets,
            )

        if category in CORE_CREDIT_CATEGORIES or not merchant:
            return None, []

        offsets = self._best_offsets(session, credit, category=category, consumed=consumed)
        explicit_credit = (
            "CREDIT" in description or "REFUND" in description or "RETURN" in description
        )
        if offsets:
            return (
                TreatmentCandidate(
                    txn_id=_txn_id(credit),
                    treatment_type="product_return",
                    status="auto_applied",
                    confidence=offsets[0].confidence,
                    netting_policy=AUTO_NET_POLICY,
                    target_category=category,
                    evidence={
                        "reason": "positive credit matched to prior same merchant/category debit",
                        "credit": _txn_dict(credit, include_raw=self.include_raw),
                        "matched_debit": [o.debit_txn_id for o in offsets],
                    },
                ),
                offsets,
            )
        if explicit_credit:
            return (
                TreatmentCandidate(
                    txn_id=_txn_id(credit),
                    treatment_type="product_return",
                    status="needs_review",
                    confidence=_confidence("0.50"),
                    netting_policy=REVIEW_POLICY,
                    target_category=category,
                    evidence={
                        "reason": "explicit credit without strong debit match",
                        "credit": _txn_dict(credit, include_raw=self.include_raw),
                    },
                ),
                [],
            )
        return None, []

    def _best_offsets(
        self,
        session,
        credit: TransactionDB,
        *,
        category: str,
        consumed: dict[str, Decimal] | None = None,
        match_merchant: bool = True,
    ) -> list[OffsetCandidate]:
        """Match a credit to one or more prior debits, SPILLING across multiple expenses
        when a single debit cannot absorb the whole credit -- a reimbursement that pays back
        several visits, or an exact-match debit already partly consumed by an earlier credit.

        Allocation is consumed-aware (a pristine debit beats one an earlier credit drained)
        so a reimbursement's remainder is never silently dropped on the floor.
        """
        consumed = consumed or {}
        amount = _money(cast(Any, credit.amount_signed))
        window_start = credit.posting_date - timedelta(days=180)
        query = (
            session.query(TransactionDB)
            .filter(TransactionDB.amount_signed < 0)
            .filter(TransactionDB.posting_date <= credit.posting_date)
            .filter(TransactionDB.posting_date >= window_start)
            .filter(TransactionDB.category == category)
            # Only debits in the spend universe may be offset, matching the
            # authoritative dashboard (true_cost_by_category / _period_offset_total)
            # and the export annotation_map: a credit must never net against a row
            # that the dashboard does not count as spending.
            .filter(TransactionDB.include_in_cashflow == True)  # noqa: E712
            .filter(TransactionDB.exclude_from_spending == False)  # noqa: E712
            # CAD-only: a non-CAD debit is excluded from the CAD spend total, so it
            # must not be offset either (amounts are not FX-comparable across
            # currencies). This also keeps a CAD credit from netting a USD debit.
            .filter(*cad_sql_filters())
        )
        if match_merchant:
            # Product returns come back from the SAME merchant; reimbursements come from
            # the insurer (a different merchant), so they match on category + amount alone.
            query = query.filter(TransactionDB.merchant == credit.merchant)
        candidates = query.order_by(TransactionDB.posting_date.desc(), TransactionDB.txn_id).all()

        rank_confidence = {
            0: _confidence("0.98"),
            1: _confidence("0.95"),
            2: _confidence("0.90"),
            3: _confidence("0.85"),
            4: _confidence("0.80"),
        }
        # `working` overlays this credit's own allocations on top of cross-credit consumption,
        # so a debit is never double-used and headroom is always live.
        working = dict(consumed)
        remaining_credit = amount
        offsets: list[OffsetCandidate] = []
        while remaining_credit > Decimal("0.01"):
            best: tuple[tuple[int, Decimal, Decimal], TransactionDB, int, str, Decimal] | None = (
                None
            )
            for debit in candidates:
                debit_amount = _money(abs(cast(Any, debit.amount_signed)))
                headroom = debit_amount - working.get(_txn_id(debit), Decimal("0"))
                if headroom <= Decimal("0.01"):
                    continue  # already fully offset
                ranked = self._rank_debit(debit, credit, debit_amount, remaining_credit)
                if ranked is None:
                    continue
                rank, reason, diff = ranked
                # Best tier first, then smallest amount gap, then the debit with the MOST
                # headroom (a pristine expense beats one an earlier credit already drained).
                key = (rank, diff, -headroom)
                if best is None or key < best[0]:
                    best = (key, debit, rank, reason, diff)
            if best is None:
                break
            _, debit, rank, reason, diff = best
            headroom = _money(abs(cast(Any, debit.amount_signed))) - working.get(
                _txn_id(debit), Decimal("0")
            )
            alloc = min(remaining_credit, headroom)
            offsets.append(
                OffsetCandidate(
                    credit_txn_id=_txn_id(credit),
                    debit_txn_id=_txn_id(debit),
                    offset_amount=alloc,
                    confidence=rank_confidence[rank],
                    status="auto_applied",
                    evidence={
                        "match_reason": reason,
                        "amount_difference": float(diff),
                        "credit": _txn_dict(credit, include_raw=self.include_raw),
                        "debit": _txn_dict(debit, include_raw=self.include_raw),
                    },
                )
            )
            working[_txn_id(debit)] = working.get(_txn_id(debit), Decimal("0")) + alloc
            remaining_credit -= alloc
        return offsets

    def _rank_debit(
        self,
        debit: TransactionDB,
        credit: TransactionDB,
        debit_amount: Decimal,
        amount: Decimal,
    ) -> tuple[int, str, Decimal] | None:
        """Quality tier (lower is better) of applying `amount` of the credit to this debit,
        or None if the debit is not a plausible match for that amount."""
        diff = abs(debit_amount - amount)
        same_description = _norm(debit.description).replace(" (PURCHASE)", "") == _norm(
            credit.description
        ).replace(" (CREDIT)", "")
        if diff <= Decimal("0.01"):
            return (0 if same_description else 1), "exact_amount", diff
        if debit_amount >= amount and same_description:
            return 2, "same_description_partial", diff
        if debit_amount >= amount and diff <= max(Decimal("5.00"), debit_amount * Decimal("0.15")):
            return 3, "near_amount", diff
        if debit_amount >= amount and amount >= debit_amount * Decimal("0.05"):
            return 4, "partial_return", diff
        return None

    def _reimbursement_target(self, session, credit: TransactionDB) -> str | None:
        if self._best_offsets(session, credit, category="Health & Wellness", match_merchant=False):
            return "Health & Wellness"
        return None

    def _persist(self, result: AdjustmentDetectionResult) -> None:
        detected_at = _now()
        with self._open_session() as session:
            session.query(TransactionOffsetDB).filter(
                TransactionOffsetDB.source == DETECTOR_SOURCE
            ).delete(synchronize_session=False)
            session.query(TransactionTreatmentDB).filter(
                TransactionTreatmentDB.source == DETECTOR_SOURCE
            ).delete(synchronize_session=False)

            for treatment in result.treatments:
                session.add(
                    TransactionTreatmentDB(
                        txn_id=treatment.txn_id,
                        treatment_type=treatment.treatment_type,
                        status=treatment.status,
                        confidence=treatment.confidence,
                        netting_policy=treatment.netting_policy,
                        target_category=treatment.target_category,
                        evidence=treatment.evidence,
                        detected_at=detected_at,
                        source=DETECTOR_SOURCE,
                    )
                )
            for offset in result.offsets:
                session.add(
                    TransactionOffsetDB(
                        credit_txn_id=offset.credit_txn_id,
                        debit_txn_id=offset.debit_txn_id,
                        offset_amount=offset.offset_amount,
                        confidence=offset.confidence,
                        status=offset.status,
                        evidence=offset.evidence,
                        detected_at=detected_at,
                        source=DETECTOR_SOURCE,
                    )
                )

        run_result = self._run_result(result)
        record_db_operation(self.db_path, operation="adjustments_detect", result=run_result)

    def _period_offset_total(self, session, start_date: date, end_date: date) -> Decimal:
        # Sum the capped offset_amount, scoped to the DEBIT being reduced (same scope as
        # gross_spend): only offsets whose matched expense is in the period and counts as
        # spending. Driving this off the credit's date/amount instead would let a refund
        # for a prior-period or excluded purchase wrongly cut this period's cost.
        # Income-side analogue: _period_credit_offset_total (scoped to the credit's period).
        total = (
            session.query(func.sum(TransactionOffsetDB.offset_amount))
            .select_from(TransactionOffsetDB)
            .join(TransactionDB, TransactionDB.txn_id == TransactionOffsetDB.debit_txn_id)
            .filter(TransactionOffsetDB.status == "auto_applied")
            .filter(TransactionOffsetDB.source == DETECTOR_SOURCE)
            .filter(TransactionDB.posting_date >= start_date)
            .filter(TransactionDB.posting_date <= end_date)
            .filter(TransactionDB.include_in_cashflow == True)  # noqa: E712
            .filter(TransactionDB.exclude_from_spending == False)  # noqa: E712
            # CAD-only debit scope, matching gross_spend so the subtraction composes.
            .filter(*cad_sql_filters())
            .scalar()
        )
        return _money(total)

    def _period_credit_offset_total(self, session, start_date: date, end_date: date) -> Decimal:
        # Sum the capped offset_amount, scoped to the CREDIT being netted out of income:
        # only offsets whose refund/reimbursement credit posts in the period and counts as
        # income. A refund credit is stored as cashflow_type=='income' but is not real
        # income; netting it here (in the credit's own period) keeps a cross-period refund
        # from inflating income, while the debit-scoped offset total above stays reserved
        # for the expense-side true_cost.
        # exclude_from_spending is a debit-side flag and does not apply to credits;
        # the filters here mirror gross_income so the subtraction composes exactly.
        total = (
            session.query(func.sum(TransactionOffsetDB.offset_amount))
            .select_from(TransactionOffsetDB)
            .join(TransactionDB, TransactionDB.txn_id == TransactionOffsetDB.credit_txn_id)
            .filter(TransactionOffsetDB.status == "auto_applied")
            .filter(TransactionOffsetDB.source == DETECTOR_SOURCE)
            .filter(TransactionDB.posting_date >= start_date)
            .filter(TransactionDB.posting_date <= end_date)
            .filter(*income_sql_filters())
            .scalar()
        )
        return _money(total)

    def _period_excluded_income_total(self, session, start_date: date, end_date: date) -> Decimal:
        # Mirror the gross_income filters exactly so this subtraction composes
        # correctly regardless of future classifier policy changes:
        #   • include_in_cashflow = True  — only counted-in-cashflow rows
        #   • cashflow_type = 'income'    — only income-typed rows
        #   • amount_signed > 0           — only positive (credit) amounts
        # Without these guards a mis-classified row (wrong cashflow_type, excluded
        # from cashflow, or negative) could wrongly deflate adjusted_income.
        total = (
            session.query(func.sum(TransactionDB.amount_signed))
            .join(TransactionTreatmentDB, TransactionTreatmentDB.txn_id == TransactionDB.txn_id)
            .filter(TransactionDB.posting_date >= start_date)
            .filter(TransactionDB.posting_date <= end_date)
            .filter(*income_sql_filters())
            .filter(TransactionTreatmentDB.status == "auto_applied")
            .filter(TransactionTreatmentDB.netting_policy == EXCLUDE_POLICY)
            # CAD-only, matching gross_income so this excluded-income subtraction composes.
            .scalar()
        )
        return _money(total)

    @staticmethod
    def _counts(
        treatments: list[TreatmentCandidate],
        offsets: list[OffsetCandidate],
    ) -> dict[str, int]:
        counts: dict[str, int] = {
            "treatments": len(treatments),
            "offsets": len(offsets),
            "auto_applied": sum(1 for item in treatments if item.status == "auto_applied"),
            "needs_review": sum(1 for item in treatments if item.status == "needs_review"),
        }
        for treatment in treatments:
            key = treatment.treatment_type
            counts[key] = counts.get(key, 0) + 1
        return counts

    @staticmethod
    def _run_result(result: AdjustmentDetectionResult) -> RunResult:
        return RunResult(
            status=result.status,
            started_at=result.started_at,
            finished_at=result.finished_at,
            counts=result.counts,
            warnings=result.warnings,
            errors=result.errors,
            details=result.details,
        )
