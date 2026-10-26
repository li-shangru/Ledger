"""Bounded, deterministic comparison of preserved plans and canonical imported events.

Matching admits a three-day posting window and a difference of at most the
smaller of CAD 100 or 20% of the expectation (minimum one cent). Identity is
mandatory for single-account events. Every plausible candidate participates in
the ambiguity graph; neither exact preference nor report clipping assigns an
event twice. This diagnostic service never supplies cash-plan suppression.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func, tuple_
from sqlalchemy.orm import Session

from ledger.core.account_identity import account_identity
from ledger.finance_policy import is_cad_row
from ledger.query.base import SessionScopeFactory
from ledger.storage.database import (
    AccountDB,
    PlannedScheduleDB,
    PlannedScheduleRevisionDB,
    PlanReconciliationReviewDB,
    RecurringPatternDB,
    StatementCoverageIntervalDB,
    TransactionDB,
    TransferDB,
    session_scope,
)
from ledger.storage.snapshots import (
    audited_database_write_connection,
    database_target_lock,
    database_write_outcome,
)
from ledger.utils.dates import naive_utc_now
from ledger.utils.text import normalize_whitespace

from .cash_plan import (
    MAX_ACTUAL_FACT_ROWS,
    MAX_PLAN_ACCOUNTS,
    MAX_PLAN_OCCURRENCES,
    CashPlanError,
    CashPlanService,
    schedule_occurrence_dates,
)
from .history import (
    EffectiveSchedule,
    definition_revision,
    effective_schedules,
    incomplete_history_before_baseline,
)
from .schedules import ScheduleRecord, ScheduleService

POSTING_WINDOW_DAYS = 3
MAX_RECONCILIATION_ROWS = 100
MAX_CANDIDATE_EDGES = 50_000


class ReconciliationConflictError(ValueError):
    """The reviewed occurrence or its material evidence changed."""


class _BorrowedReadConnection:
    """SQLAlchemy can read an audited writer without owning its transaction."""

    def __init__(self, connection):
        self.connection = connection

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def rollback(self):
        pass

    def close(self):
        pass

    def commit(self):
        raise RuntimeError("Borrowed reconciliation reader cannot commit")


@dataclass(frozen=True)
class _Expected:
    version: EffectiveSchedule
    day: date

    @property
    def occurrence_id(self) -> str:
        return f"schedule:{self.version.record.schedule_id}:{self.day.isoformat()}"


@dataclass(frozen=True)
class _Event:
    account_id: str
    counterparty_account_id: str | None
    event_type: str
    day: date
    amount: Decimal
    evidence: tuple[dict[str, Any], ...]
    pattern_id: str | None = None
    merchant: str = ""
    pair_complete: bool = True

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(sorted(row["evidence_id"] for row in self.evidence))


def _shift(day: date, days: int) -> date:
    return date.fromordinal(min(date.max.toordinal(), max(1, day.toordinal() + days)))


def _normalized(value: str) -> str:
    return normalize_whitespace(value).casefold()


class PlanReconciliationService:
    def __init__(
        self,
        db_path: Path,
        *,
        session_scope_factory: SessionScopeFactory = session_scope,
        prepare_schema: bool = False,
    ) -> None:
        self.db_path = Path(db_path)
        self._reader = ScheduleService(
            db_path,
            session_scope_factory=session_scope_factory,
            prepare_schema=prepare_schema,
        )

    def build(
        self,
        start_date: date,
        end_date: date,
        *,
        as_of: date,
        limit: int = MAX_RECONCILIATION_ROWS,
        offset: int = 0,
    ) -> dict[str, Any]:
        self._validate(start_date, end_date, as_of, limit, offset)
        with self._reader._open_session() as session:
            return self._build(session, start_date, end_date, as_of, limit, offset)

    @staticmethod
    def _validate(start_date, end_date, as_of, limit, offset):
        if not all(
            isinstance(day, date) and not isinstance(day, datetime)
            for day in (start_date, end_date, as_of)
        ):
            raise CashPlanError("invalid_date", "Reconciliation requires explicit dates.")
        if start_date > end_date or (end_date - start_date).days >= 366:
            raise CashPlanError("invalid_window", "Reconciliation period must be one to 366 days.")
        if (
            type(limit) is not int
            or not 1 <= limit <= MAX_RECONCILIATION_ROWS
            or type(offset) is not int
            or not 0 <= offset <= MAX_PLAN_OCCURRENCES
        ):
            raise CashPlanError(
                "filter_too_broad", "Reconciliation page is outside supported bounds."
            )

    def _build(self, session, start, end, as_of, limit, offset):
        accounts = (
            session.query(AccountDB)
            .order_by(AccountDB.account_id)
            .limit(MAX_PLAN_ACCOUNTS + 1)
            .all()
        )
        if len(accounts) > MAX_PLAN_ACCOUNTS:
            raise CashPlanError("filter_too_broad", "Reconciliation account set is too broad.")
        account_by_id = {row.account_id: row for row in accounts}
        versions = effective_schedules(session, _shift(start, -6), _shift(end, 6))
        occurrences: list[_Expected] = []
        hypothetical_count = 0
        incomplete_ids = incomplete_history_before_baseline(session, start, end)
        for version in versions:
            record = version.record
            CashPlanService._validate_stored_schedule(record, account_by_id)
            if not version.history_complete:
                incomplete_ids.add(record.schedule_id)
            if not record.is_active:
                continue
            dates = schedule_occurrence_dates(
                record.next_date, record.frequency, version.start_date, version.end_date
            )
            if record.provenance != "confirmed":
                hypothetical_count += sum(start <= day <= end for day in dates)
                continue
            occurrences.extend(_Expected(version, day) for day in dates)
            if len(occurrences) > MAX_PLAN_OCCURRENCES:
                raise CashPlanError(
                    "too_many_occurrences", "Reconciliation has too many occurrences."
                )
        events = self._events(session, account_by_id, _shift(start, -3), min(_shift(end, 3), as_of))
        buckets: dict[tuple[str, str, str | None, date], list[_Event]] = defaultdict(list)
        partial_buckets: dict[tuple[str, date], list[_Event]] = defaultdict(list)
        for event in events:
            if not event.pair_complete:
                partial_buckets[(event.account_id, event.day)].append(event)
                continue
            buckets[
                (event.account_id, event.event_type, event.counterparty_account_id, event.day)
            ].append(event)
        candidates: dict[str, list[_Event]] = {}
        claimants: Counter[tuple[str, ...]] = Counter()
        edge_count = 0
        for occurrence in occurrences:
            record = occurrence.version.record
            matched: list[_Event] = []
            for delta in range(-3, 4):
                for event in buckets.get(
                    (
                        record.account_id,
                        record.event_type,
                        record.counterparty_account_id,
                        _shift(occurrence.day, delta),
                    ),
                    [],
                ):
                    if self._plausible(occurrence, event):
                        matched.append(event)
                if record.counterparty_account_id is not None:
                    for identity in (record.account_id, record.counterparty_account_id):
                        for partial in partial_buckets.get(
                            (identity, _shift(occurrence.day, delta)), []
                        ):
                            if self._plausible(occurrence, partial):
                                matched.append(partial)
            # date.min/max shifts can collapse two lookup days.
            unique = {event.evidence_ids: event for event in matched}
            edge_count += len(unique)
            if edge_count > MAX_CANDIDATE_EDGES:
                raise CashPlanError(
                    "filter_too_broad", "Reconciliation candidate set is too broad."
                )
            candidates[occurrence.occurrence_id] = [unique[key] for key in sorted(unique)]
            claimants.update(unique.keys())
        intervals = (
            session.query(StatementCoverageIntervalDB)
            .filter(
                StatementCoverageIntervalDB.start_date <= _shift(end, 3),
                StatementCoverageIntervalDB.end_date >= _shift(start, -3),
                StatementCoverageIntervalDB.evidence_basis == "printed_statement_period",
            )
            .limit(MAX_ACTUAL_FACT_ROWS + 1)
            .all()
        )
        if len(intervals) > MAX_ACTUAL_FACT_ROWS:
            raise CashPlanError("filter_too_broad", "Statement coverage set is too broad.")
        findings = []
        for occurrence in sorted(occurrences, key=lambda item: (item.day, item.occurrence_id)):
            if not start <= occurrence.day <= end:
                continue
            findings.append(
                self._finding(
                    occurrence,
                    candidates[occurrence.occurrence_id],
                    claimants,
                    intervals,
                    account_by_id,
                    as_of,
                )
            )
        # Review lookup is bounded to the returned occurrence set, not all historical reviews.
        for chunk in range(0, len(findings), 500):
            rows = findings[chunk : chunk + 500]
            reviewed = {
                (row.occurrence_id, row.material_revision)
                for row in session.query(PlanReconciliationReviewDB).filter(
                    tuple_(
                        PlanReconciliationReviewDB.occurrence_id,
                        PlanReconciliationReviewDB.material_revision,
                    ).in_([(row["occurrence_id"], row["material_revision"]) for row in rows])
                )
            }
            for finding in rows:
                finding["reviewed"] = (
                    finding["occurrence_id"],
                    finding["material_revision"],
                ) in reviewed
                if finding["reviewed"]:
                    finding["actionable"] = False
        pattern_count = int(
            session.query(func.count())
            .select_from(RecurringPatternDB)
            .filter(RecurringPatternDB.is_active.is_(True))
            .scalar()
            or 0
        )
        return {
            "period": {"start_date": start.isoformat(), "end_date": end.isoformat()},
            "as_of": as_of.isoformat(),
            "currency": "CAD",
            "posting_window_days": POSTING_WINDOW_DAYS,
            "total_count": len(findings),
            "returned_count": len(findings[offset : offset + limit]),
            "truncated": offset + limit < len(findings),
            "offset": offset,
            "findings": findings[offset : offset + limit],
            "disclosures": {
                "hypothetical_occurrence_count": hypothetical_count,
                "detected_pattern_count": pattern_count,
                "incomplete_history_schedule_count": len(incomplete_ids),
            },
            "assumptions": [
                "cad_only",
                "posting_window_three_days",
                "amount_tolerance_twenty_percent_capped_100_cad",
                "single_event_requires_merchant_or_pattern",
                "no_event_reuse",
                "missing_requires_complete_statement_window",
                "legacy_history_incomplete",
                "diagnostic_matches_do_not_suppress_cash_plan",
            ],
        }

    @staticmethod
    def _plausible(expected: _Expected, event: _Event) -> bool:
        record = expected.version.record
        tolerance = max(Decimal("0.01"), min(Decimal("100.00"), record.amount * Decimal("0.20")))
        if abs(event.amount - record.amount) > tolerance:
            return False
        if any(
            abs((date.fromisoformat(row["date"]) - expected.day).days) > POSTING_WINDOW_DAYS
            for row in event.evidence
        ):
            return False
        if not event.pair_complete:
            if record.counterparty_account_id is None:
                return False
            if event.counterparty_account_id is not None and {
                event.account_id,
                event.counterparty_account_id,
            } != {record.account_id, record.counterparty_account_id}:
                return False
            observed = Decimal(event.evidence[0]["amount"])
            return (event.account_id == record.account_id and observed < 0) or (
                event.account_id == record.counterparty_account_id
                and (record.event_type == "card_payment" or observed > 0)
            )
        return (
            record.counterparty_account_id is not None
            or (
                bool(_normalized(event.merchant))
                and _normalized(record.name) == _normalized(event.merchant)
            )
            or bool(record.pattern_id and record.pattern_id == event.pattern_id)
        )

    @staticmethod
    def _coverage(expected, intervals, accounts):
        record = expected.version.record
        start, end = _shift(expected.day, -3), _shift(expected.day, 3)
        result = []
        for identity in (record.account_id, record.counterparty_account_id):
            if identity is None:
                continue
            account = accounts[identity]
            selected = sorted(
                (max(start, row.start_date), min(end, row.end_date))
                for row in intervals
                if row.institution == account.institution
                and row.account_slug == account.account_number
                and row.start_date <= end
                and row.end_date >= start
            )
            merged: list[tuple[date, date]] = []
            for left, right in selected:
                if merged and left <= _shift(merged[-1][1], 1):
                    merged[-1] = (merged[-1][0], max(right, merged[-1][1]))
                else:
                    merged.append((left, right))
            complete = len(merged) == 1 and merged[0] == (start, end)
            result.append(
                {
                    "account_id": identity,
                    "complete": complete,
                    "intervals": [
                        {"start_date": left.isoformat(), "end_date": right.isoformat()}
                        for left, right in merged
                    ],
                }
            )
        return {
            "complete": all(row["complete"] for row in result),
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "accounts": result,
        }

    def _finding(self, expected, candidates, claimants, intervals, accounts, as_of):
        record = expected.version.record
        coverage = self._coverage(expected, intervals, accounts)
        ambiguous = len(candidates) > 1 or any(
            claimants[event.evidence_ids] > 1 or not event.pair_complete for event in candidates
        )
        event = candidates[0] if len(candidates) == 1 and not ambiguous else None
        amount_changed = event is not None and event.amount != record.amount
        date_changed = event is not None and any(
            row["date"] != expected.day.isoformat() for row in event.evidence
        )
        if ambiguous:
            status = "ambiguous"
        elif event is not None:
            status = (
                "amount_and_date_difference"
                if amount_changed and date_changed
                else "amount_difference"
                if amount_changed
                else "date_difference"
                if date_changed
                else "matched"
            )
        elif as_of <= _shift(expected.day, POSTING_WINDOW_DAYS):
            status = "pending"
        elif coverage["complete"] and expected.version.history_complete:
            status = "missing"
        else:
            status = "coverage_gap"
        basis = "ambiguous_candidates" if ambiguous else "no_candidate"
        if event is not None:
            basis = (
                "reciprocal_transfer_pair"
                if len(event.evidence) == 2
                else (
                    "linked_pattern"
                    if record.pattern_id and record.pattern_id == event.pattern_id
                    else "normalized_merchant"
                )
            )
        evidence_ids = sorted(
            {identity for candidate in candidates for identity in candidate.evidence_ids}
        )
        material = {
            "expected_revision": expected.version.revision,
            "expected_date": expected.day.isoformat(),
            "status": status,
            "coverage": coverage,
            "history_complete": expected.version.history_complete,
            "candidates": [
                {
                    "ids": item.evidence_ids,
                    "dates": [row["date"] for row in item.evidence],
                    "amount": str(item.amount),
                    "basis_merchant": _normalized(item.merchant),
                    "pattern_id": item.pattern_id,
                }
                for item in candidates
            ],
        }
        revision = hashlib.sha256(
            json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return {
            "occurrence_id": expected.occurrence_id,
            "schedule_id": record.schedule_id,
            "schedule_revision": expected.version.revision,
            "name": record.name,
            "expected_date": expected.day.isoformat(),
            "expected_amount": f"{record.amount:.2f}",
            "event_type": record.event_type,
            "currency": record.currency,
            "account_id": record.account_id,
            "counterparty_account_id": record.counterparty_account_id,
            "status": status,
            "actual_date": event.day.isoformat() if event else None,
            "actual_amount": f"{event.amount:.2f}" if event else None,
            "amount_changed": amount_changed,
            "date_changed": date_changed,
            "amount_difference": f"{event.amount - record.amount:.2f}" if event else None,
            "date_difference": (event.day - expected.day).days if event else None,
            "evidence_ids": evidence_ids[:20],
            "evidence_count": len(evidence_ids),
            "evidence_truncated": len(evidence_ids) > 20,
            "candidate_count": len(candidates),
            "matching_basis": basis,
            "material_revision": revision,
            "reviewed": False,
            "actionable": status not in {"matched", "pending"},
            "coverage": coverage,
            "history_complete": expected.version.history_complete,
        }

    @staticmethod
    def _events(session: Session, accounts, start, through) -> tuple[_Event, ...]:
        if through < start:
            return ()
        transactions = (
            session.query(TransactionDB)
            .filter(TransactionDB.posting_date >= start, TransactionDB.posting_date <= through)
            .order_by(TransactionDB.txn_id)
            .limit(MAX_ACTUAL_FACT_ROWS + 1)
            .all()
        )
        transfers = (
            session.query(TransferDB)
            .filter(TransferDB.posting_date >= start, TransferDB.posting_date <= through)
            .order_by(TransferDB.txn_id)
            .limit(MAX_ACTUAL_FACT_ROWS + 1)
            .all()
        )
        if len(transactions) + len(transfers) > MAX_ACTUAL_FACT_ROWS:
            raise CashPlanError(
                "filter_too_broad", "Imported reconciliation activity is too broad."
            )
        transfer_by_id = {row.txn_id: row for row in transfers}
        missing = sorted(
            {
                row.paired_transfer_id
                for row in transfers
                if row.paired_transfer_id and row.paired_transfer_id not in transfer_by_id
            }
        )
        for chunk in range(0, len(missing), 500):
            transfer_by_id.update(
                {
                    row.txn_id: row
                    for row in session.query(TransferDB).filter(
                        TransferDB.txn_id.in_(missing[chunk : chunk + 500])
                    )
                }
            )
        found: list[_Event] = []
        for row in transactions:
            if row.txn_id in transfer_by_id or not is_cad_row(currency=row.currency):
                continue
            identity = account_identity(row.institution, row.account)
            if identity not in accounts:
                continue
            amount = CashPlanService._fact_amount(row.amount_signed)
            if amount > 0 and row.cashflow_type == "income":
                kind = "income"
            elif amount < 0 and row.cashflow_type == "expense":
                kind = "bill"
            else:
                continue
            evidence = {
                "evidence_id": row.txn_id,
                "date": row.posting_date.isoformat(),
                "amount": f"{amount:.2f}",
                "currency": "CAD",
                "event_type": kind,
                "account_id": identity,
                "merchant": row.merchant,
                "description": row.description,
            }
            found.append(
                _Event(
                    identity,
                    None,
                    kind,
                    row.posting_date,
                    abs(amount),
                    (evidence,),
                    row.recurring_pattern_id,
                    row.merchant,
                )
            )
        seen: set[tuple[str, ...]] = set()
        for transfer_row in transfers:
            pair = transfer_by_id.get(str(transfer_row.paired_transfer_id or ""))
            if (
                pair is None
                or pair.paired_transfer_id != transfer_row.txn_id
                or pair.posting_date > through
            ):
                continue
            key = tuple(sorted((str(transfer_row.txn_id), str(pair.txn_id))))
            if key in seen:
                continue
            seen.add(key)
            if not is_cad_row(currency=transfer_row.currency) or not is_cad_row(
                currency=pair.currency
            ):
                continue
            first, second = (
                account_identity(transfer_row.institution, transfer_row.account),
                account_identity(pair.institution, pair.account),
            )
            if first == second or first not in accounts or second not in accounts:
                continue
            left, right = (
                CashPlanService._fact_amount(transfer_row.amount_raw),
                CashPlanService._fact_amount(pair.amount_raw),
            )
            if abs(left) != abs(right) or left == 0:
                continue
            first_card, second_card = (
                bool(accounts[first].is_credit_card),
                bool(accounts[second].is_credit_card),
            )
            if first_card != second_card:
                source, destination, source_id, destination_id = (
                    (pair, transfer_row, second, first)
                    if first_card
                    else (transfer_row, pair, first, second)
                )
                if CashPlanService._fact_amount(source.amount_raw) >= 0 or str(
                    destination.flow
                ) not in {"payment", "transfer"}:
                    continue
                kind = "card_payment"
            elif left < 0 < right:
                source, destination, source_id, destination_id, kind = (
                    transfer_row,
                    pair,
                    first,
                    second,
                    "transfer",
                )
            elif right < 0 < left:
                source, destination, source_id, destination_id, kind = (
                    pair,
                    transfer_row,
                    second,
                    first,
                    "transfer",
                )
            else:
                continue
            if not start <= source.posting_date <= through:
                continue
            pair_evidence = tuple(
                {
                    "evidence_id": item.txn_id,
                    "date": item.posting_date.isoformat(),
                    "amount": f"{effect:.2f}",
                    "currency": "CAD",
                    "event_type": kind,
                    "account_id": identity,
                    "merchant": "",
                    "description": item.description,
                }
                for item, identity, effect in (
                    (source, source_id, -abs(left)),
                    (destination, destination_id, abs(left)),
                )
            )
            found.append(
                _Event(
                    source_id, destination_id, kind, source.posting_date, abs(left), pair_evidence
                )
            )
        represented = {identity for event in found for identity in event.evidence_ids}
        for unmatched in transfers:
            if unmatched.txn_id in represented or not is_cad_row(currency=unmatched.currency):
                continue
            identity = account_identity(unmatched.institution, unmatched.account)
            if identity not in accounts:
                continue
            amount = CashPlanService._fact_amount(unmatched.amount_raw)
            if amount == 0:
                continue
            counterpart = None
            if unmatched.counterparty_institution and unmatched.counterparty_account:
                counterpart = account_identity(
                    unmatched.counterparty_institution, unmatched.counterparty_account
                )
            partial_evidence = {
                "evidence_id": unmatched.txn_id,
                "date": unmatched.posting_date.isoformat(),
                "amount": f"{amount:.2f}",
                "currency": "CAD",
                "event_type": "unpaired_transfer",
                "account_id": identity,
                "merchant": "",
                "description": unmatched.description,
            }
            found.append(
                _Event(
                    identity,
                    counterpart,
                    "unpaired_transfer",
                    unmatched.posting_date,
                    abs(amount),
                    (partial_evidence,),
                    pair_complete=False,
                )
            )
        return tuple(sorted(found, key=lambda event: (event.day, event.evidence_ids)))

    def current_schedule_for_finding(
        self,
        start_date: date,
        end_date: date,
        *,
        as_of: date,
        occurrence_id: str,
        material_revision: str,
    ) -> ScheduleRecord:
        """Resolve a correction without overwriting a newer future definition."""
        self._validate(start_date, end_date, as_of, MAX_RECONCILIATION_ROWS, 0)
        with self._reader._open_session() as session:
            result = self._build(session, start_date, end_date, as_of, MAX_PLAN_OCCURRENCES, 0)
            finding = next(
                (row for row in result["findings"] if row["occurrence_id"] == occurrence_id), None
            )
            if (
                finding is None
                or finding["material_revision"] != material_revision
                or finding["status"]
                not in {"amount_difference", "date_difference", "amount_and_date_difference"}
            ):
                raise ReconciliationConflictError(
                    "Plan evidence changed or has no unique correction; refresh the finding."
                )
            current = session.get(PlannedScheduleDB, finding["schedule_id"])
            latest = (
                session.query(PlannedScheduleRevisionDB)
                .filter(PlannedScheduleRevisionDB.schedule_id == finding["schedule_id"])
                .order_by(
                    PlannedScheduleRevisionDB.effective_date.desc(),
                    PlannedScheduleRevisionDB.revision_id.desc(),
                )
                .first()
            )
            if (
                current is None
                or latest is None
                or latest.is_removed
                or not current.is_active
                or current.provenance != "confirmed"
            ):
                raise ReconciliationConflictError(
                    "Schedule is no longer an active confirmed definition; refresh its current state."
                )
            record = ScheduleService._from_orm(current)
            if definition_revision(record, latest.effective_date) != finding["schedule_revision"]:
                raise ReconciliationConflictError(
                    "A newer schedule definition exists; review it before proposing a correction."
                )
            return record

    def mark_reviewed(
        self,
        start_date: date,
        end_date: date,
        *,
        as_of: date,
        occurrence_id: str,
        material_revision: str,
    ) -> dict[str, Any]:
        self._validate(start_date, end_date, as_of, MAX_RECONCILIATION_ROWS, 0)
        # Read and write share the writer transaction, so evidence cannot change
        # between the material-revision check and the review insertion.
        from sqlalchemy import create_engine
        from sqlalchemy.pool import StaticPool

        with database_write_outcome() as outcome, database_target_lock(self.db_path):
            with audited_database_write_connection(
                self.db_path, label="Plan reconciliation review target"
            ) as connection:
                engine = create_engine(
                    "sqlite://",
                    creator=lambda: _BorrowedReadConnection(connection),
                    poolclass=StaticPool,
                )
                sa_connection = engine.connect()
                session = Session(bind=sa_connection)
                try:
                    result = self._build(
                        session, start_date, end_date, as_of, MAX_PLAN_OCCURRENCES, 0
                    )
                    finding = next(
                        (
                            row
                            for row in result["findings"]
                            if row["occurrence_id"] == occurrence_id
                        ),
                        None,
                    )
                    if finding is None or finding["material_revision"] != material_revision:
                        raise ReconciliationConflictError(
                            "Plan evidence changed; refresh before marking reviewed."
                        )
                    connection.execute(
                        "INSERT OR IGNORE INTO plan_reconciliation_reviews (occurrence_id,material_revision,reviewed_at) VALUES (?,?,?)",
                        (occurrence_id, material_revision, naive_utc_now().isoformat(sep=" ")),
                    )
                    finding["reviewed"] = True
                    finding["actionable"] = False
                finally:
                    session.close()
                    sa_connection.close()
                    engine.dispose()
            outcome.mark_committed()
        return finding

    def evidence(self, evidence_ids: list[str]) -> list[dict[str, Any]]:
        if (
            not isinstance(evidence_ids, list)
            or len(evidence_ids) > 20
            or not all(isinstance(value, str) and 0 < len(value) <= 64 for value in evidence_ids)
        ):
            raise CashPlanError("filter_too_broad", "Evidence lookup accepts up to 20 identifiers.")
        if not evidence_ids:
            return []
        with self._reader._open_session() as session:
            days = [
                row[0]
                for row in session.query(TransactionDB.posting_date).filter(
                    TransactionDB.txn_id.in_(evidence_ids)
                )
            ]
            days.extend(
                row[0]
                for row in session.query(TransferDB.posting_date).filter(
                    TransferDB.txn_id.in_(evidence_ids)
                )
            )
            if not days:
                return []
            accounts = {
                row.account_id: row for row in session.query(AccountDB).limit(MAX_PLAN_ACCOUNTS + 1)
            }
            if len(accounts) > MAX_PLAN_ACCOUNTS:
                raise CashPlanError("filter_too_broad", "Evidence account set is too broad.")
            events = self._events(session, accounts, _shift(min(days), -3), _shift(max(days), 3))
            return sorted(
                (
                    row
                    for event in events
                    for row in event.evidence
                    if row["evidence_id"] in evidence_ids
                ),
                key=lambda row: (row["date"], row["evidence_id"]),
            )


__all__ = ["PlanReconciliationService", "ReconciliationConflictError", "POSTING_WINDOW_DAYS"]
