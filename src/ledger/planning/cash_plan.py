"""Deterministic cash planning over dated statement anchors and disclosed inputs.

This module does not forecast or fetch live balances.  It combines user schedules
with active recurring-pattern possibilities, expands their calendar occurrences,
and applies the signed legs to each account's latest statement-backed absolute
balance on or before the selected period.  Every assumption, missing anchor, and
imported-activity suppression remains in the result.
"""

from __future__ import annotations

import calendar
from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import and_, create_engine, func
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ledger.analysis.pattern_detector import next_occurrence
from ledger.analysis.projection import align_to_window
from ledger.finance_policy import is_cad_row
from ledger.query.base import SessionScopeFactory
from ledger.storage.database import (
    AccountBalanceDB,
    AccountDB,
    PlannedScheduleDB,
    RecurringPatternDB,
    TransactionDB,
    TransferDB,
    session_scope,
)
from ledger.storage.migrations import ensure_schema
from ledger.storage.snapshots import bound_database_read_connection, database_target_lock
from ledger.utils.money import to_cents
from ledger.utils.text import normalize_whitespace

from .history import effective_schedules
from .schedules import EVENT_TYPES, FREQUENCIES, PROVENANCES, ScheduleRecord

MAX_PLAN_DAYS = 366
MAX_PLAN_ACCOUNTS = 500
MAX_PLAN_SOURCES = 20_000
MAX_PLAN_OCCURRENCES = 10_000
MAX_ACTUAL_FACT_ROWS = 20_000

_DETECTED_EVENT_TYPES = {"income": "income", "bill": "bill", "subscription": "bill"}
_DETECTED_FREQUENCIES = frozenset({"weekly", "biweekly", "monthly", "quarterly", "annual"})
_PROVENANCE_ORDER = {"imported": 0, "confirmed": 1, "hypothetical": 2, "detected": 3}


class CashPlanError(ValueError):
    """A bounded query or incompatible stored-data failure."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _add_calendar_months(base: date, months: int, expected_day: int) -> date | None:
    month_index = base.month - 1 + months
    year = base.year + month_index // 12
    if year > date.max.year:
        return None
    month = month_index % 12 + 1
    return date(year, month, min(expected_day, calendar.monthrange(year, month)[1]))


def _next_schedule_occurrence(
    current: date, frequency: str, expected_days: tuple[int, ...]
) -> date | None:
    if frequency in {"weekly", "biweekly"}:
        days = 7 if frequency == "weekly" else 14
        return current + timedelta(days=days) if (date.max - current).days >= days else None
    if frequency == "semimonthly":
        last_day = calendar.monthrange(current.year, current.month)[1]
        for expected_day in expected_days:
            candidate = current.replace(day=min(expected_day, last_day))
            if candidate > current:
                return candidate
        return _add_calendar_months(current, 1, expected_days[0])
    if frequency == "monthly":
        return _add_calendar_months(current, 1, expected_days[0])
    if frequency == "quarterly":
        return _add_calendar_months(current, 3, expected_days[0])
    if frequency == "yearly":
        return _add_calendar_months(current, 12, expected_days[0])
    raise CashPlanError("invalid_frequency", "Schedule frequency is invalid.")


def schedule_occurrence_dates(
    next_date: date,
    frequency: str,
    start_date: date,
    end_date: date,
    *,
    max_occurrences: int = MAX_PLAN_OCCURRENCES,
) -> tuple[date, ...]:
    """Expand one schedule into calendar dates in the inclusive selected range.

    Monthly, quarterly, and yearly schedules preserve the original day and clamp
    only in shorter months. Semimonthly schedules preserve two calendar days
    fifteen days apart from the original next date, except that an original 15th
    or month-end selects the 15th and calendar month-end. Dates before next_date
    are never generated, and expansion stops at the supported date limit.
    """
    if frequency not in FREQUENCIES:
        raise CashPlanError("invalid_frequency", "Schedule frequency is invalid.")
    if not all(
        isinstance(value, date) and not isinstance(value, datetime)
        for value in (
            next_date,
            start_date,
            end_date,
        )
    ):
        raise CashPlanError("invalid_date", "Cash plan dates are invalid.")
    if end_date < start_date:
        raise CashPlanError("invalid_window", "Cash plan period is invalid.")
    if frequency == "once":
        return (next_date,) if start_date <= next_date <= end_date else ()

    expected_days: tuple[int, ...] = (next_date.day,)
    if frequency == "semimonthly":
        if next_date.day in {15, calendar.monthrange(next_date.year, next_date.month)[1]}:
            expected_days = (15, 31)
        elif next_date.day < 15:
            expected_days = (next_date.day, next_date.day + 15)
        else:
            expected_days = (next_date.day - 15, next_date.day)
    current: date | None = next_date
    # Walk to the range without materializing discarded occurrences.  The cap
    # also bounds corrupt/stale dates that would otherwise create an unbounded loop.
    steps = 0
    while current is not None and current < start_date:
        current = _next_schedule_occurrence(current, frequency, expected_days)
        steps += 1
        if steps > MAX_PLAN_OCCURRENCES:
            raise CashPlanError("too_many_occurrences", "Cash schedule is too broad.")

    occurrences: list[date] = []
    while current is not None and current <= end_date:
        occurrences.append(current)
        if len(occurrences) > max_occurrences:
            raise CashPlanError("too_many_occurrences", "Cash schedule is too broad.")
        current = _next_schedule_occurrence(current, frequency, expected_days)
    return tuple(occurrences)


def _money(value: object) -> str | None:
    return None if value is None else f"{Decimal(str(value)):.2f}"


@dataclass(frozen=True)
class CashPlanAnchor:
    status: str
    balance: Decimal | None
    basis: str | None
    balance_date: date | None
    as_of_lag_days: int | None

    def to_dict(self) -> dict[str, object]:
        statement = None
        if self.balance_date is not None:
            statement = (
                f"Statement-backed absolute balance dated {self.balance_date.isoformat()}; "
                "this is not a live balance."
            )
        return {
            "status": self.status,
            "balance": _money(self.balance),
            "basis": self.basis,
            "balance_date": self.balance_date.isoformat() if self.balance_date else None,
            "as_of_lag_days": self.as_of_lag_days,
            "statement": statement,
        }


@dataclass(frozen=True)
class CashPlanLeg:
    occurrence_id: str
    source_id: str
    source_kind: str
    name: str
    event_type: str
    account_id: str
    counterparty_account_id: str | None
    occurrence_date: date
    amount: Decimal
    currency: str
    provenance: str
    is_spending: bool
    confidence: float | None
    direction: str
    in_selected_period: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "occurrence_id": self.occurrence_id,
            "source_id": self.source_id,
            "source_kind": self.source_kind,
            "name": self.name,
            "event_type": self.event_type,
            "account_id": self.account_id,
            "counterparty_account_id": self.counterparty_account_id,
            "occurrence_date": self.occurrence_date.isoformat(),
            "amount": _money(self.amount),
            "currency": self.currency,
            "provenance": self.provenance,
            "is_spending": self.is_spending,
            "confidence": self.confidence,
            "direction": self.direction,
            "in_selected_period": self.in_selected_period,
        }


@dataclass(frozen=True)
class SuppressedOccurrence:
    occurrence_id: str
    source_id: str
    source_kind: str
    name: str
    event_type: str
    account_id: str
    counterparty_account_id: str | None
    occurrence_date: date
    amount: Decimal
    currency: str
    provenance: str
    matched_kind: str
    matched_evidence_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "occurrence_id": self.occurrence_id,
            "source_id": self.source_id,
            "source_kind": self.source_kind,
            "name": self.name,
            "event_type": self.event_type,
            "account_id": self.account_id,
            "counterparty_account_id": self.counterparty_account_id,
            "occurrence_date": self.occurrence_date.isoformat(),
            "amount": _money(self.amount),
            "currency": self.currency,
            "provenance": self.provenance,
            "matched_kind": self.matched_kind,
            "matched_evidence_ids": list(self.matched_evidence_ids),
            "match_rule": (
                "unique canonical account/date/amount/type and merchant or linked-pattern match"
                if self.matched_kind == "transaction"
                else "unique canonical accounts/source date/amount/type match"
            ),
        }


@dataclass(frozen=True)
class AccountCashPlan:
    account_id: str
    institution: str
    account_type: str
    is_credit_card: bool
    anchor: CashPlanAnchor
    opening_projected_balance: Decimal | None
    actual_roll_forward_movement: Decimal
    planned_movement: Decimal
    movement_since_anchor: Decimal
    projected_movement: Decimal
    projected_balance: Decimal | None
    lowest_projected_balance: Decimal | None
    lowest_projected_date: date | None
    legs: tuple[CashPlanLeg, ...]

    @property
    def fact_legs(self) -> tuple[CashPlanLeg, ...]:
        return tuple(leg for leg in self.legs if leg.provenance == "imported")

    @property
    def planning_legs(self) -> tuple[CashPlanLeg, ...]:
        return tuple(leg for leg in self.legs if leg.provenance != "imported")

    def to_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "institution": self.institution,
            "account_type": self.account_type,
            "is_credit_card": self.is_credit_card,
            "anchor": self.anchor.to_dict(),
            "opening_projected_balance": _money(self.opening_projected_balance),
            "actual_roll_forward_movement": _money(self.actual_roll_forward_movement),
            "planned_movement": _money(self.planned_movement),
            "movement_since_anchor": _money(self.movement_since_anchor),
            "projected_movement": _money(self.projected_movement),
            "projected_balance": _money(self.projected_balance),
            "lowest_projected_balance": _money(self.lowest_projected_balance),
            "lowest_projected_date": (
                self.lowest_projected_date.isoformat() if self.lowest_projected_date else None
            ),
            "legs": [leg.to_dict() for leg in self.legs],
            "fact_legs": [leg.to_dict() for leg in self.fact_legs],
            "planning_legs": [leg.to_dict() for leg in self.planning_legs],
        }


@dataclass(frozen=True)
class BeforePaydayAccount:
    account_id: str
    projected_movement: Decimal
    projected_balance_before_payday: Decimal | None

    def to_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "projected_movement": _money(self.projected_movement),
            "projected_balance_before_payday": _money(self.projected_balance_before_payday),
        }


@dataclass(frozen=True)
class BeforeNextPayday:
    payday_date: date
    income_amount: Decimal
    income_provenances: tuple[str, ...]
    accounts: tuple[BeforePaydayAccount, ...]
    includes_hypothetical: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "payday_date": self.payday_date.isoformat(),
            "income_amount": _money(self.income_amount),
            "income_provenances": list(self.income_provenances),
            "accounts": [account.to_dict() for account in self.accounts],
            "includes_hypothetical": self.includes_hypothetical,
        }


@dataclass(frozen=True)
class OmittedPatternCount:
    reason: str
    count: int

    def to_dict(self) -> dict[str, object]:
        return {"reason": self.reason, "count": self.count}


@dataclass(frozen=True)
class CashPlanCoverage:
    account_count: int
    anchored_account_count: int
    missing_anchor_account_ids: tuple[str, ...]
    stored_schedule_count: int
    active_schedule_count: int
    confirmed_schedule_count: int
    hypothetical_schedule_count: int
    detected_pattern_count: int
    overridden_detected_pattern_count: int
    omitted_detected_patterns: tuple[OmittedPatternCount, ...]
    occurrence_count: int
    applied_planning_occurrence_count: int
    excluded_pre_as_of_planning_occurrence_count: int
    suppressed_occurrence_count: int
    ambiguous_suppression_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "account_count": self.account_count,
            "anchored_account_count": self.anchored_account_count,
            "missing_anchor_account_ids": list(self.missing_anchor_account_ids),
            "stored_schedule_count": self.stored_schedule_count,
            "active_schedule_count": self.active_schedule_count,
            "confirmed_schedule_count": self.confirmed_schedule_count,
            "hypothetical_schedule_count": self.hypothetical_schedule_count,
            "detected_pattern_count": self.detected_pattern_count,
            "overridden_detected_pattern_count": self.overridden_detected_pattern_count,
            "omitted_detected_patterns": [row.to_dict() for row in self.omitted_detected_patterns],
            "occurrence_count": self.occurrence_count,
            "applied_planning_occurrence_count": self.applied_planning_occurrence_count,
            "excluded_pre_as_of_planning_occurrence_count": (
                self.excluded_pre_as_of_planning_occurrence_count
            ),
            "suppressed_occurrence_count": self.suppressed_occurrence_count,
            "ambiguous_suppression_count": self.ambiguous_suppression_count,
        }


@dataclass(frozen=True)
class CashPlanFactCoverage:
    """Imported facts applied between account anchors and the caller's as-of date."""

    actual_through: date
    fact_leg_count: int
    transaction_leg_count: int
    transfer_leg_count: int
    deduplicated_representation_count: int
    excluded_non_cad_count: int
    unmapped_account_count: int

    def to_dict(self) -> dict[str, object]:
        return {
            "basis": (
                "imported account activity after each usable anchor, or from plan start when "
                "no anchor exists, through as-of"
            ),
            "currency_rule": "CAD and legacy blank-currency rows; native non-CAD rows excluded",
            "representation_rule": (
                "when one transaction id exists in both imported tables, the transfer row is used"
            ),
            "actual_through": self.actual_through.isoformat(),
            "fact_leg_count": self.fact_leg_count,
            "transaction_leg_count": self.transaction_leg_count,
            "transfer_leg_count": self.transfer_leg_count,
            "deduplicated_representation_count": self.deduplicated_representation_count,
            "excluded_non_cad_count": self.excluded_non_cad_count,
            "unmapped_account_count": self.unmapped_account_count,
        }


@dataclass(frozen=True)
class CashPlan:
    start_date: date
    end_date: date
    as_of: date
    currency: str
    accounts: tuple[AccountCashPlan, ...]
    legs: tuple[CashPlanLeg, ...]
    suppressions: tuple[SuppressedOccurrence, ...]
    coverage: CashPlanCoverage
    facts: CashPlanFactCoverage
    assumptions: tuple[str, ...]
    before_next_payday: BeforeNextPayday | None

    @property
    def fact_legs(self) -> tuple[CashPlanLeg, ...]:
        return tuple(leg for leg in self.legs if leg.provenance == "imported")

    @property
    def planning_legs(self) -> tuple[CashPlanLeg, ...]:
        return tuple(leg for leg in self.legs if leg.provenance != "imported")

    def to_dict(self) -> dict[str, object]:
        return {
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "as_of": self.as_of.isoformat(),
            "currency": self.currency,
            "accounts": [account.to_dict() for account in self.accounts],
            "legs": [leg.to_dict() for leg in self.legs],
            "fact_legs": [leg.to_dict() for leg in self.fact_legs],
            "planning_legs": [leg.to_dict() for leg in self.planning_legs],
            "suppressions": [row.to_dict() for row in self.suppressions],
            "coverage": self.coverage.to_dict(),
            "facts": self.facts.to_dict(),
            "assumptions": list(self.assumptions),
            "before_next_payday": (
                self.before_next_payday.to_dict() if self.before_next_payday else None
            ),
        }


@dataclass(frozen=True)
class _Occurrence:
    occurrence_id: str
    source_id: str
    source_kind: str
    name: str
    event_type: str
    account_id: str
    counterparty_account_id: str | None
    occurrence_date: date
    amount: Decimal
    provenance: str
    confidence: float | None
    pattern_id: str | None

    @property
    def single_match_key(self) -> tuple[str, date, Decimal, str]:
        return (self.account_id, self.occurrence_date, self.amount, self.event_type)

    @property
    def pair_match_key(self) -> tuple[str, str, date, Decimal, str] | None:
        if self.counterparty_account_id is None:
            return None
        return (
            self.account_id,
            self.counterparty_account_id,
            self.occurrence_date,
            self.amount,
            self.event_type,
        )


@dataclass(frozen=True)
class _ImportedPair:
    source_account_id: str
    destination_account_id: str
    posting_date: date
    amount: Decimal
    event_type: str
    evidence_ids: tuple[str, str]

    @property
    def key(self) -> tuple[str, str, date, Decimal, str]:
        return (
            self.source_account_id,
            self.destination_account_id,
            self.posting_date,
            self.amount,
            self.event_type,
        )


@dataclass(frozen=True)
class _TransferFactMetadata:
    event_type: str
    counterparty_account_id: str
    direction: str
    amount: Decimal
    occurrence_id: str


class CashPlanService:
    """Build one deterministic, CAD-only cash plan from a database snapshot."""

    def __init__(
        self,
        db_path: Path,
        *,
        session_scope_factory: SessionScopeFactory = session_scope,
        prepare_schema: bool = True,
    ) -> None:
        self.db_path = Path(db_path)
        self._session_scope_factory = session_scope_factory
        self._bound_default_reads = not prepare_schema and session_scope_factory is session_scope
        if prepare_schema:
            with database_target_lock(self.db_path):
                ensure_schema(self.db_path)

    def _open_session(self) -> AbstractContextManager[Session]:
        if self._bound_default_reads:
            return self._open_read_only_session()
        return self._session_scope_factory(self.db_path)

    @contextmanager
    def _open_read_only_session(self) -> Iterator[Session]:
        """Bind one cash plan to one exact main/WAL snapshot."""
        path = self.db_path.expanduser().absolute()
        if not path.is_file():
            raise FileNotFoundError(f"Database not found: {path}")
        with bound_database_read_connection(
            path,
            label="Cash-plan read source",
            require_main_owner_only=False,
            require_parent_owner_only=False,
        ) as raw_connection:
            engine = create_engine(
                "sqlite://",
                creator=lambda: raw_connection,
                poolclass=StaticPool,
            )
            sqlalchemy_connection = engine.connect()
            session = Session(bind=sqlalchemy_connection)
            try:
                yield session
            finally:
                session.rollback()
                session.close()
                sqlalchemy_connection.close()
                engine.dispose()

    def build(self, *, start_date: date, end_date: date, as_of: date) -> CashPlan:
        self._validate_window(start_date=start_date, end_date=end_date, as_of=as_of)
        with self._open_session() as session:
            accounts = (
                session.query(AccountDB)
                .order_by(AccountDB.institution, AccountDB.account_type, AccountDB.account_id)
                .all()
            )
            if len(accounts) > MAX_PLAN_ACCOUNTS:
                raise CashPlanError("filter_too_broad", "Cash plan account set is too broad.")
            account_by_id = {str(row.account_id): row for row in accounts}
            anchors = self._anchors(session, account_by_id, start_date=start_date, as_of=as_of)
            anchored_dates = [
                anchor.balance_date
                for anchor in anchors.values()
                if anchor.balance_date is not None
            ]
            generation_start = min(
                [start_date, *(day + timedelta(days=1) for day in anchored_dates)]
            )

            schedules = (
                session.query(PlannedScheduleDB)
                .order_by(
                    PlannedScheduleDB.next_date,
                    PlannedScheduleDB.name,
                    PlannedScheduleDB.schedule_id,
                )
                .all()
            )
            pattern_count = int(
                session.query(func.count()).select_from(RecurringPatternDB).scalar() or 0
            )
            if len(schedules) + pattern_count > MAX_PLAN_SOURCES:
                raise CashPlanError("filter_too_broad", "Cash plan source set is too broad.")
            occurrences, source_counts, omission_counts = self._occurrences(
                session,
                schedules,
                account_by_id,
                generation_start=generation_start,
                end_date=end_date,
            )
            if len(occurrences) > MAX_PLAN_OCCURRENCES:
                raise CashPlanError("too_many_occurrences", "Cash plan has too many occurrences.")
            fact_legs, fact_stats = self._actual_fact_legs(
                session,
                account_by_id,
                start_date=start_date,
                not_before=generation_start,
                through=as_of,
            )
            suppressions, included, ambiguous = self._suppress_imported_activity(
                session,
                occurrences,
                account_by_id,
                through=min(as_of, end_date),
                not_before=generation_start,
            )
            # Materialize the public account values while the ORM rows remain
            # attached.  The default session helper commits (and expires ORM
            # attributes) even for a read transaction.
            planning_legs = self._legs(included, start_date=start_date)
            candidate_legs = self._sorted_legs((*fact_legs, *planning_legs))
            account_plans = tuple(
                self._account_plan(
                    account,
                    anchors[str(account.account_id)],
                    candidate_legs,
                    start_date=start_date,
                    end_date=end_date,
                    as_of=as_of,
                )
                for account in accounts
            )
            legs = self._applied_legs(account_plans)
        before_next_payday = self._before_next_payday(
            account_plans,
            legs,
            as_of=as_of,
        )
        missing = tuple(
            account.account_id for account in account_plans if account.anchor.balance is None
        )
        applied_planning_occurrence_ids = {
            leg.occurrence_id for leg in legs if leg.provenance != "imported"
        }
        excluded_pre_as_of_occurrence_ids = {
            occurrence.occurrence_id
            for occurrence in included
            if occurrence.occurrence_date < as_of
        }
        coverage = CashPlanCoverage(
            account_count=len(accounts),
            anchored_account_count=len(accounts) - len(missing),
            missing_anchor_account_ids=missing,
            stored_schedule_count=len(schedules),
            active_schedule_count=source_counts["active_schedules"],
            confirmed_schedule_count=source_counts["confirmed_schedules"],
            hypothetical_schedule_count=source_counts["hypothetical_schedules"],
            detected_pattern_count=source_counts["detected_patterns"],
            overridden_detected_pattern_count=source_counts["overridden_patterns"],
            omitted_detected_patterns=tuple(
                OmittedPatternCount(reason=reason, count=count)
                for reason, count in sorted(omission_counts.items())
            ),
            occurrence_count=len(occurrences),
            applied_planning_occurrence_count=len(applied_planning_occurrence_ids),
            excluded_pre_as_of_planning_occurrence_count=len(excluded_pre_as_of_occurrence_ids),
            suppressed_occurrence_count=len(suppressions),
            ambiguous_suppression_count=ambiguous,
        )
        applied_fact_legs = tuple(leg for leg in legs if leg.provenance == "imported")
        facts = CashPlanFactCoverage(
            actual_through=as_of,
            fact_leg_count=len(applied_fact_legs),
            transaction_leg_count=sum(
                leg.source_kind == "imported_transaction" for leg in applied_fact_legs
            ),
            transfer_leg_count=sum(
                leg.source_kind == "imported_transfer" for leg in applied_fact_legs
            ),
            deduplicated_representation_count=fact_stats["deduplicated_representations"],
            excluded_non_cad_count=fact_stats["excluded_non_cad"],
            unmapped_account_count=fact_stats["unmapped_accounts"],
        )
        assumptions = self._assumptions(
            missing=bool(missing),
            detected=source_counts["detected_patterns"] > 0,
            hypothetical=source_counts["hypothetical_schedules"] > 0,
        )
        return CashPlan(
            start_date=start_date,
            end_date=end_date,
            as_of=as_of,
            currency="CAD",
            accounts=account_plans,
            legs=legs,
            suppressions=tuple(suppressions),
            coverage=coverage,
            facts=facts,
            assumptions=assumptions,
            before_next_payday=before_next_payday,
        )

    @staticmethod
    def _validate_window(*, start_date: date, end_date: date, as_of: date) -> None:
        if not all(
            isinstance(value, date) and not isinstance(value, datetime)
            for value in (start_date, end_date, as_of)
        ):
            raise CashPlanError("invalid_date", "Cash plan dates are invalid.")
        if start_date > as_of or as_of > end_date:
            raise CashPlanError(
                "invalid_window", "Cash plan requires start date <= as-of date <= end date."
            )
        if (end_date - start_date).days > MAX_PLAN_DAYS:
            raise CashPlanError("filter_too_broad", "Cash plan period exceeds one year.")

    @staticmethod
    def _anchors(
        session: Session,
        account_by_id: dict[str, AccountDB],
        *,
        start_date: date,
        as_of: date,
    ) -> dict[str, CashPlanAnchor]:
        all_anchored = {
            str(account_id)
            for (account_id,) in session.query(AccountBalanceDB.account_id)
            .filter(AccountBalanceDB.balance_basis == "absolute")
            .distinct()
        }
        latest = (
            session.query(
                AccountBalanceDB.account_id,
                func.max(AccountBalanceDB.balance_date).label("balance_date"),
            )
            .filter(
                AccountBalanceDB.balance_basis == "absolute",
                AccountBalanceDB.balance_date <= start_date,
            )
            .group_by(AccountBalanceDB.account_id)
            .subquery()
        )
        rows = (
            session.query(AccountBalanceDB)
            .join(
                latest,
                and_(
                    AccountBalanceDB.account_id == latest.c.account_id,
                    AccountBalanceDB.balance_date == latest.c.balance_date,
                ),
            )
            .filter(AccountBalanceDB.balance_basis == "absolute")
            .all()
        )
        by_id = {str(row.account_id): row for row in rows}
        result: dict[str, CashPlanAnchor] = {}
        for account_id in account_by_id:
            row = by_id.get(account_id)
            if row is None:
                result[account_id] = CashPlanAnchor(
                    status=(
                        "no_anchor_on_or_before_period"
                        if account_id in all_anchored
                        else "no_absolute_anchor"
                    ),
                    balance=None,
                    basis=None,
                    balance_date=None,
                    as_of_lag_days=None,
                )
            else:
                result[account_id] = CashPlanAnchor(
                    status="statement_anchor",
                    balance=to_cents(row.balance),
                    basis="statement_absolute",
                    balance_date=row.balance_date,
                    as_of_lag_days=(as_of - row.balance_date).days,
                )
        return result

    def _occurrences(
        self,
        session: Session,
        schedules: list[PlannedScheduleDB],
        account_by_id: dict[str, AccountDB],
        *,
        generation_start: date,
        end_date: date,
    ) -> tuple[list[_Occurrence], Counter[str], Counter[str]]:
        source_counts: Counter[str] = Counter()
        omission_counts: Counter[str] = Counter()
        found: list[_Occurrence] = []
        versions = effective_schedules(session, generation_start, end_date)
        override_windows: dict[str, list[tuple[date, date]]] = defaultdict(list)
        for version in versions:
            if version.record.provenance == "confirmed" and version.record.pattern_id is not None:
                override_windows[version.record.pattern_id].append(
                    (version.start_date, version.end_date)
                )
        counted: set[str] = set()
        for version in versions:
            schedule_row = version.record
            self._validate_stored_schedule(schedule_row, account_by_id)
            if not bool(schedule_row.is_active):
                continue
            if schedule_row.schedule_id not in counted:
                source_counts["active_schedules"] += 1
                source_counts[f"{schedule_row.provenance}_schedules"] += 1
                counted.add(schedule_row.schedule_id)
            amount = to_cents(schedule_row.amount)
            for occurrence_date in schedule_occurrence_dates(
                schedule_row.next_date,
                str(schedule_row.frequency),
                version.start_date,
                version.end_date,
                max_occurrences=MAX_PLAN_OCCURRENCES - len(found),
            ):
                found.append(
                    _Occurrence(
                        occurrence_id=(
                            f"schedule:{schedule_row.schedule_id}:{occurrence_date.isoformat()}"
                        ),
                        source_id=str(schedule_row.schedule_id),
                        source_kind="schedule",
                        name=str(schedule_row.name),
                        event_type=str(schedule_row.event_type),
                        account_id=str(schedule_row.account_id),
                        counterparty_account_id=(
                            None
                            if schedule_row.counterparty_account_id is None
                            else str(schedule_row.counterparty_account_id)
                        ),
                        occurrence_date=occurrence_date,
                        amount=amount,
                        provenance=str(schedule_row.provenance),
                        confidence=None,
                        pattern_id=(
                            None
                            if schedule_row.pattern_id is None
                            else str(schedule_row.pattern_id)
                        ),
                    )
                )
                if len(found) > MAX_PLAN_OCCURRENCES:
                    raise CashPlanError(
                        "too_many_occurrences", "Cash plan has too many occurrences."
                    )

        pattern_rows = session.query(RecurringPatternDB).order_by(
            RecurringPatternDB.next_expected_date,
            RecurringPatternDB.merchant,
            RecurringPatternDB.pattern_id,
        )
        for pattern_row in pattern_rows:
            pattern_id = str(pattern_row.pattern_id)
            if not bool(pattern_row.is_active):
                omission_counts["inactive"] += 1
                continue
            event_type = _DETECTED_EVENT_TYPES.get(str(pattern_row.pattern_type))
            if event_type is None:
                omission_counts["unsupported_event_type"] += 1
                continue
            frequency = str(pattern_row.frequency)
            if frequency not in _DETECTED_FREQUENCIES:
                omission_counts["unsupported_frequency"] += 1
                continue
            account_id = str(pattern_row.account_id)
            if account_id not in account_by_id:
                omission_counts["unknown_account"] += 1
                continue
            try:
                amount = to_cents(abs(Decimal(str(pattern_row.expected_amount))))
            except InvalidOperation, TypeError, ValueError:
                omission_counts["invalid_amount"] += 1
                continue
            if not amount.is_finite() or amount <= 0:
                omission_counts["invalid_amount"] += 1
                continue
            occurrence, _ = align_to_window(
                pattern_row.next_expected_date,
                frequency=frequency,
                expected_day=int(pattern_row.expected_day_of_period),
                not_before=max(generation_start, pattern_row.first_occurrence),
            )
            if occurrence > end_date:
                omission_counts["no_occurrence_in_window"] += 1
                continue
            has_detected = False
            has_override = False
            while occurrence <= end_date:
                if any(
                    left <= occurrence <= right
                    for left, right in override_windows.get(pattern_id, [])
                ):
                    has_override = True
                    occurrence = next_occurrence(
                        occurrence, frequency, int(pattern_row.expected_day_of_period)
                    )
                    continue
                has_detected = True
                found.append(
                    _Occurrence(
                        occurrence_id=f"pattern:{pattern_id}:{occurrence.isoformat()}",
                        source_id=pattern_id,
                        source_kind="detected_pattern",
                        name=str(pattern_row.merchant),
                        event_type=event_type,
                        account_id=account_id,
                        counterparty_account_id=None,
                        occurrence_date=occurrence,
                        amount=amount,
                        provenance="detected",
                        confidence=max(0.0, min(1.0, float(pattern_row.confidence))),
                        pattern_id=pattern_id,
                    )
                )
                if len(found) > MAX_PLAN_OCCURRENCES:
                    raise CashPlanError(
                        "too_many_occurrences", "Cash plan has too many occurrences."
                    )
                occurrence = next_occurrence(
                    occurrence, frequency, int(pattern_row.expected_day_of_period)
                )
            source_counts["detected_patterns"] += int(has_detected)
            source_counts["overridden_patterns"] += int(has_override)
        found.sort(
            key=lambda item: (
                item.occurrence_date,
                _PROVENANCE_ORDER[item.provenance],
                item.name,
                item.source_id,
            )
        )
        return found, source_counts, omission_counts

    @staticmethod
    def _validate_stored_schedule(
        row: PlannedScheduleDB | ScheduleRecord, account_by_id: dict[str, AccountDB]
    ) -> None:
        try:
            amount = Decimal(str(row.amount))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise CashPlanError("invalid_stored_schedule", "Stored schedule is invalid.") from exc
        event_type = str(row.event_type)
        counterparty = (
            None if row.counterparty_account_id is None else str(row.counterparty_account_id)
        )
        if (
            not str(row.name).strip()
            or event_type not in EVENT_TYPES
            or str(row.frequency) not in FREQUENCIES
            or str(row.provenance) not in PROVENANCES
            or str(row.currency).upper() != "CAD"
            or not amount.is_finite()
            or amount <= 0
            or to_cents(amount) != amount
            or str(row.account_id) not in account_by_id
            or (event_type in {"card_payment", "transfer"} and counterparty is None)
            or (event_type in {"income", "bill"} and counterparty is not None)
            or (counterparty is not None and counterparty not in account_by_id)
            or (counterparty is not None and counterparty == str(row.account_id))
            or (
                event_type == "card_payment"
                and counterparty is not None
                and not bool(account_by_id[counterparty].is_credit_card)
            )
        ):
            raise CashPlanError("invalid_stored_schedule", "Stored schedule is invalid.")

    def _actual_fact_legs(
        self,
        session: Session,
        account_by_id: dict[str, AccountDB],
        *,
        start_date: date,
        not_before: date,
        through: date,
    ) -> tuple[tuple[CashPlanLeg, ...], Counter[str]]:
        """Load exact imported account effects once, preferring transfer rows by id."""
        transfer_rows = (
            session.query(TransferDB)
            .filter(
                TransferDB.posting_date >= not_before,
                TransferDB.posting_date <= through,
            )
            .order_by(TransferDB.posting_date, TransferDB.txn_id)
            .limit(MAX_ACTUAL_FACT_ROWS + 1)
            .all()
        )
        transaction_rows = (
            session.query(TransactionDB)
            .filter(
                TransactionDB.posting_date >= not_before,
                TransactionDB.posting_date <= through,
            )
            .order_by(TransactionDB.posting_date, TransactionDB.txn_id)
            .limit(MAX_ACTUAL_FACT_ROWS + 1)
            .all()
        )
        if len(transfer_rows) + len(transaction_rows) > MAX_ACTUAL_FACT_ROWS:
            raise CashPlanError(
                "actual_activity_too_broad",
                "Imported activity between the statement anchors and as-of is too broad.",
            )

        account_key = {
            (str(row.institution), str(row.account_number)): str(row.account_id)
            for row in account_by_id.values()
        }
        transfer_ids = {str(row.txn_id) for row in transfer_rows}
        transfer_metadata = self._transfer_fact_metadata(
            session,
            transfer_rows,
            account_key,
            account_by_id,
        )
        stats: Counter[str] = Counter()
        facts: list[CashPlanLeg] = []

        for transfer_row in transfer_rows:
            if not is_cad_row(currency=transfer_row.currency):
                stats["excluded_non_cad"] += 1
                continue
            account_id = account_key.get((str(transfer_row.institution), str(transfer_row.account)))
            if account_id is None:
                stats["unmapped_accounts"] += 1
                continue
            metadata = transfer_metadata.get(str(transfer_row.txn_id))
            amount = (
                metadata.amount
                if metadata is not None
                else self._fact_amount(transfer_row.amount_raw)
            )
            if amount == 0:
                continue
            event_type = (
                metadata.event_type
                if metadata is not None
                else "card_payment"
                if str(transfer_row.flow) == "payment"
                else "transfer"
            )
            facts.append(
                CashPlanLeg(
                    occurrence_id=(
                        metadata.occurrence_id
                        if metadata is not None
                        else f"fact:transfer:{transfer_row.txn_id}"
                    ),
                    source_id=str(transfer_row.txn_id),
                    source_kind="imported_transfer",
                    name=str(transfer_row.description).strip() or "Imported transfer",
                    event_type=event_type,
                    account_id=account_id,
                    counterparty_account_id=(
                        metadata.counterparty_account_id if metadata is not None else None
                    ),
                    occurrence_date=transfer_row.posting_date,
                    amount=amount,
                    currency="CAD",
                    provenance="imported",
                    is_spending=False,
                    confidence=None,
                    direction=metadata.direction if metadata is not None else "account",
                    in_selected_period=transfer_row.posting_date >= start_date,
                )
            )

        for transaction_row in transaction_rows:
            if str(transaction_row.txn_id) in transfer_ids:
                stats["deduplicated_representations"] += 1
                continue
            if not is_cad_row(currency=transaction_row.currency):
                stats["excluded_non_cad"] += 1
                continue
            account_id = account_key.get(
                (str(transaction_row.institution), str(transaction_row.account))
            )
            if account_id is None:
                stats["unmapped_accounts"] += 1
                continue
            amount = self._fact_amount(transaction_row.amount_signed)
            if amount == 0:
                continue
            cashflow_type = str(transaction_row.cashflow_type)
            flow = str(transaction_row.flow)
            if cashflow_type == "income" and amount > 0:
                event_type = "income"
            elif cashflow_type == "expense" and amount < 0:
                event_type = "bill"
            elif flow == "payment":
                event_type = "card_payment"
            elif flow == "transfer" or cashflow_type == "transfer":
                event_type = "transfer"
            else:
                event_type = flow or cashflow_type or "activity"
            is_spending = (
                event_type not in {"card_payment", "transfer"}
                and cashflow_type == "expense"
                and amount < 0
                and bool(transaction_row.include_in_cashflow)
                and not bool(transaction_row.exclude_from_spending)
            )
            facts.append(
                CashPlanLeg(
                    occurrence_id=f"fact:transaction:{transaction_row.txn_id}",
                    source_id=str(transaction_row.txn_id),
                    source_kind="imported_transaction",
                    name=(
                        str(transaction_row.merchant or transaction_row.description).strip()
                        or "Imported activity"
                    ),
                    event_type=event_type,
                    account_id=account_id,
                    counterparty_account_id=None,
                    occurrence_date=transaction_row.posting_date,
                    amount=amount,
                    currency="CAD",
                    provenance="imported",
                    is_spending=is_spending,
                    confidence=None,
                    direction="account",
                    in_selected_period=transaction_row.posting_date >= start_date,
                )
            )
        return self._sorted_legs(facts), stats

    @staticmethod
    def _fact_amount(value: object) -> Decimal:
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise CashPlanError(
                "invalid_imported_activity", "Imported account activity has an invalid amount."
            ) from exc
        if not amount.is_finite():
            raise CashPlanError(
                "invalid_imported_activity", "Imported account activity has an invalid amount."
            )
        return to_cents(amount)

    def _transfer_fact_metadata(
        self,
        session: Session,
        rows: list[TransferDB],
        account_key: dict[tuple[str, str], str],
        account_by_id: dict[str, AccountDB],
    ) -> dict[str, _TransferFactMetadata]:
        by_id = {str(row.txn_id): row for row in rows}
        missing_pair_ids = sorted(
            {
                str(row.paired_transfer_id)
                for row in rows
                if row.paired_transfer_id is not None and str(row.paired_transfer_id) not in by_id
            }
        )
        for offset in range(0, len(missing_pair_ids), 500):
            counterparts = (
                session.query(TransferDB)
                .filter(TransferDB.txn_id.in_(missing_pair_ids[offset : offset + 500]))
                .all()
            )
            by_id.update({str(row.txn_id): row for row in counterparts})
        result: dict[str, _TransferFactMetadata] = {}
        for row in rows:
            row_id = str(row.txn_id)
            pair = by_id.get(str(row.paired_transfer_id or ""))
            if pair is None or str(pair.paired_transfer_id or "") != row_id:
                continue
            pair_id = str(pair.txn_id)
            if row_id in result or pair_id in result:
                continue
            row_account = account_key.get((str(row.institution), str(row.account)))
            pair_account = account_key.get((str(pair.institution), str(pair.account)))
            if row_account is None or pair_account is None or row_account == pair_account:
                continue
            if not is_cad_row(currency=row.currency) or not is_cad_row(currency=pair.currency):
                continue
            row_amount = CashPlanService._fact_amount(row.amount_raw)
            pair_amount = CashPlanService._fact_amount(pair.amount_raw)
            magnitude = to_cents(abs(row_amount))
            if magnitude <= 0 or magnitude != to_cents(abs(pair_amount)):
                continue
            row_card = bool(account_by_id[row_account].is_credit_card)
            pair_card = bool(account_by_id[pair_account].is_credit_card)
            if row_card != pair_card:
                source_id, destination_id = (pair_id, row_id) if row_card else (row_id, pair_id)
                source_account, destination_account = (
                    (pair_account, row_account) if row_card else (row_account, pair_account)
                )
                event_type = "card_payment"
            elif row_amount < 0 < pair_amount:
                source_id, destination_id = row_id, pair_id
                source_account, destination_account = row_account, pair_account
            elif pair_amount < 0 < row_amount and abs(pair_amount) == row_amount:
                source_id, destination_id = pair_id, row_id
                source_account, destination_account = pair_account, row_account
            else:
                continue
            if row_card == pair_card:
                event_type = "transfer"
            evidence_ids = tuple(sorted((row_id, pair_id)))
            occurrence_id = f"fact:transfer-pair:{evidence_ids[0]}:{evidence_ids[1]}"
            result[source_id] = _TransferFactMetadata(
                event_type=event_type,
                counterparty_account_id=destination_account,
                direction="source",
                amount=-magnitude,
                occurrence_id=occurrence_id,
            )
            result[destination_id] = _TransferFactMetadata(
                event_type=event_type,
                counterparty_account_id=source_account,
                direction="destination",
                amount=magnitude,
                occurrence_id=occurrence_id,
            )
        return result

    def _suppress_imported_activity(
        self,
        session: Session,
        occurrences: list[_Occurrence],
        account_by_id: dict[str, AccountDB],
        *,
        through: date,
        not_before: date,
    ) -> tuple[list[SuppressedOccurrence], list[_Occurrence], int]:
        account_key = {
            (str(row.institution), str(row.account_number)): str(row.account_id)
            for row in account_by_id.values()
        }
        activity: defaultdict[tuple[str, date, Decimal, str], list[TransactionDB]] = defaultdict(
            list
        )
        represented_as_transfer = {
            str(txn_id)
            for (txn_id,) in session.query(TransferDB.txn_id)
            .filter(
                TransferDB.posting_date >= not_before,
                TransferDB.posting_date <= through,
            )
            .all()
        }
        transaction_rows = session.query(TransactionDB).filter(
            TransactionDB.posting_date >= not_before,
            TransactionDB.posting_date <= through,
        )
        for row in transaction_rows:
            if str(row.txn_id) in represented_as_transfer:
                continue
            if not is_cad_row(currency=row.currency):
                continue
            account_id = account_key.get((str(row.institution), str(row.account)))
            if account_id is None:
                continue
            raw = Decimal(str(row.amount_signed))
            if str(row.cashflow_type) == "income" and raw > 0:
                event_type = "income"
            elif str(row.cashflow_type) == "expense" and raw < 0:
                event_type = "bill"
            else:
                continue
            activity[(account_id, row.posting_date, to_cents(abs(raw)), event_type)].append(row)

        pairs: defaultdict[tuple[str, str, date, Decimal, str], list[_ImportedPair]] = defaultdict(
            list
        )
        for pair in self._imported_pairs(
            session,
            account_key,
            account_by_id,
            not_before=not_before,
            through=through,
        ):
            pairs[pair.key].append(pair)

        single_counts = Counter(
            occurrence.single_match_key
            for occurrence in occurrences
            if occurrence.counterparty_account_id is None and occurrence.occurrence_date <= through
        )
        pair_counts = Counter(
            occurrence.pair_match_key
            for occurrence in occurrences
            if occurrence.pair_match_key is not None and occurrence.occurrence_date <= through
        )
        suppressed: list[SuppressedOccurrence] = []
        included: list[_Occurrence] = []
        ambiguous = 0
        for occurrence in occurrences:
            evidence_ids: tuple[str, ...] | None = None
            matched_kind = ""
            if occurrence.occurrence_date <= through:
                pair_key = occurrence.pair_match_key
                if pair_key is None:
                    transaction_candidates = activity.get(occurrence.single_match_key, [])
                    if (
                        single_counts[occurrence.single_match_key] == 1
                        and len(transaction_candidates) == 1
                    ):
                        transaction = transaction_candidates[0]
                        merchant = normalize_whitespace(transaction.merchant).casefold()
                        merchant_matches = (
                            bool(merchant)
                            and merchant == normalize_whitespace(occurrence.name).casefold()
                        )
                        pattern_matches = (
                            occurrence.pattern_id is not None
                            and bool(occurrence.pattern_id.strip())
                            and occurrence.pattern_id == transaction.recurring_pattern_id
                        )
                        if merchant_matches or pattern_matches:
                            evidence_ids = (str(transaction.txn_id),)
                            matched_kind = "transaction"
                    elif transaction_candidates:
                        ambiguous += 1
                else:
                    pair_candidates = pairs.get(pair_key, [])
                    if pair_counts[pair_key] == 1 and len(pair_candidates) == 1:
                        evidence_ids = pair_candidates[0].evidence_ids
                        matched_kind = "paired_transfer"
                    elif pair_candidates:
                        ambiguous += 1
            if evidence_ids is None:
                included.append(occurrence)
                continue
            suppressed.append(
                SuppressedOccurrence(
                    occurrence_id=occurrence.occurrence_id,
                    source_id=occurrence.source_id,
                    source_kind=occurrence.source_kind,
                    name=occurrence.name,
                    event_type=occurrence.event_type,
                    account_id=occurrence.account_id,
                    counterparty_account_id=occurrence.counterparty_account_id,
                    occurrence_date=occurrence.occurrence_date,
                    amount=occurrence.amount,
                    currency="CAD",
                    provenance=occurrence.provenance,
                    matched_kind=matched_kind,
                    matched_evidence_ids=evidence_ids,
                )
            )
        return suppressed, included, ambiguous

    def _imported_pairs(
        self,
        session: Session,
        account_key: dict[tuple[str, str], str],
        account_by_id: dict[str, AccountDB],
        *,
        not_before: date,
        through: date,
    ) -> tuple[_ImportedPair, ...]:
        candidate_rows = (
            session.query(TransferDB)
            .filter(
                TransferDB.posting_date >= not_before,
                TransferDB.posting_date <= through,
                TransferDB.paired_transfer_id.is_not(None),
            )
            .all()
        )
        # The reciprocal id is the durable result of the configured transfer
        # matcher. Fetch that primary key directly instead of imposing a second,
        # hard-coded date window here: posting dates often differ, and a custom
        # matcher window must remain authoritative. Chunking keeps the lookup
        # bounded even on the maximum supported fact set.
        by_id = {str(row.txn_id): row for row in candidate_rows}
        missing_pair_ids = sorted(
            {
                str(row.paired_transfer_id)
                for row in candidate_rows
                if str(row.paired_transfer_id) not in by_id
            }
        )
        for offset in range(0, len(missing_pair_ids), 500):
            counterparts = (
                session.query(TransferDB)
                .filter(TransferDB.txn_id.in_(missing_pair_ids[offset : offset + 500]))
                .all()
            )
            by_id.update({str(row.txn_id): row for row in counterparts})
        found: list[_ImportedPair] = []
        seen: set[tuple[str, str]] = set()
        for row in candidate_rows:
            pair_id = str(row.paired_transfer_id)
            pair = by_id.get(pair_id)
            if pair is None or str(pair.paired_transfer_id or "") != str(row.txn_id):
                continue
            first_evidence_id, second_evidence_id = sorted((str(row.txn_id), str(pair.txn_id)))
            evidence_ids = (first_evidence_id, second_evidence_id)
            if evidence_ids in seen:
                continue
            seen.add(evidence_ids)
            if not is_cad_row(currency=row.currency) or not is_cad_row(currency=pair.currency):
                continue
            row_account = account_key.get((str(row.institution), str(row.account)))
            pair_account = account_key.get((str(pair.institution), str(pair.account)))
            if row_account is None or pair_account is None or row_account == pair_account:
                continue
            row_amount = Decimal(str(row.amount_raw))
            pair_amount = Decimal(str(pair.amount_raw))
            magnitude = to_cents(abs(row_amount))
            if magnitude <= 0 or magnitude != to_cents(abs(pair_amount)):
                continue
            row_card = bool(account_by_id[row_account].is_credit_card)
            pair_card = bool(account_by_id[pair_account].is_credit_card)
            if row_card != pair_card:
                source_row = pair if row_card else row
                source = pair_account if row_card else row_account
                destination = row_account if row_card else pair_account
                source_amount = pair_amount if row_card else row_amount
                if source_amount >= 0:
                    continue
                event_type = "card_payment"
            else:
                if row_amount < 0 < pair_amount:
                    source_row = row
                    source, destination = row_account, pair_account
                elif pair_amount < 0 < row_amount:
                    source_row = pair
                    source, destination = pair_account, row_account
                else:
                    continue
                event_type = "transfer"
            source_date = source_row.posting_date
            if source_date < not_before or source_date > through:
                continue
            found.append(
                _ImportedPair(
                    source_account_id=source,
                    destination_account_id=destination,
                    posting_date=source_date,
                    amount=magnitude,
                    event_type=event_type,
                    evidence_ids=evidence_ids,
                )
            )
        return tuple(found)

    @staticmethod
    def _legs(occurrences: list[_Occurrence], *, start_date: date) -> tuple[CashPlanLeg, ...]:
        legs: list[CashPlanLeg] = []
        for occurrence in occurrences:
            if occurrence.event_type == "income":
                legs.append(
                    CashPlanService._leg(
                        occurrence,
                        account_id=occurrence.account_id,
                        amount=occurrence.amount,
                        direction="account",
                        start_date=start_date,
                    )
                )
            elif occurrence.event_type == "bill":
                legs.append(
                    CashPlanService._leg(
                        occurrence,
                        account_id=occurrence.account_id,
                        amount=-occurrence.amount,
                        direction="account",
                        start_date=start_date,
                    )
                )
            else:
                assert occurrence.counterparty_account_id is not None
                legs.extend(
                    (
                        CashPlanService._leg(
                            occurrence,
                            account_id=occurrence.account_id,
                            amount=-occurrence.amount,
                            direction="source",
                            start_date=start_date,
                        ),
                        CashPlanService._leg(
                            occurrence,
                            account_id=occurrence.counterparty_account_id,
                            amount=occurrence.amount,
                            direction="destination",
                            start_date=start_date,
                        ),
                    )
                )
        return CashPlanService._sorted_legs(legs)

    @staticmethod
    def _sorted_legs(legs: Iterable[CashPlanLeg]) -> tuple[CashPlanLeg, ...]:
        return tuple(
            sorted(
                legs,
                key=lambda leg: (
                    leg.occurrence_date,
                    leg.occurrence_id,
                    0 if leg.direction in {"account", "source"} else 1,
                    leg.account_id,
                ),
            )
        )

    @staticmethod
    def _applied_legs(accounts: tuple[AccountCashPlan, ...]) -> tuple[CashPlanLeg, ...]:
        by_key = {
            (leg.occurrence_id, leg.account_id, leg.direction): leg
            for account in accounts
            for leg in account.legs
        }
        return CashPlanService._sorted_legs(by_key.values())

    @staticmethod
    def _leg(
        occurrence: _Occurrence,
        *,
        account_id: str,
        amount: Decimal,
        direction: str,
        start_date: date,
    ) -> CashPlanLeg:
        counterparty_account_id = occurrence.counterparty_account_id
        if direction == "destination":
            counterparty_account_id = occurrence.account_id
        return CashPlanLeg(
            occurrence_id=occurrence.occurrence_id,
            source_id=occurrence.source_id,
            source_kind=occurrence.source_kind,
            name=occurrence.name,
            event_type=occurrence.event_type,
            account_id=account_id,
            counterparty_account_id=counterparty_account_id,
            occurrence_date=occurrence.occurrence_date,
            amount=to_cents(amount),
            currency="CAD",
            provenance=occurrence.provenance,
            is_spending=occurrence.event_type == "bill",
            confidence=occurrence.confidence,
            direction=direction,
            in_selected_period=occurrence.occurrence_date >= start_date,
        )

    @staticmethod
    def _account_plan(
        account: AccountDB,
        anchor: CashPlanAnchor,
        legs: tuple[CashPlanLeg, ...],
        *,
        start_date: date,
        end_date: date,
        as_of: date,
    ) -> AccountCashPlan:
        first_fact_date = (
            start_date if anchor.balance_date is None else anchor.balance_date + timedelta(days=1)
        )
        fact_legs = tuple(
            leg
            for leg in legs
            if leg.account_id == str(account.account_id)
            and leg.provenance == "imported"
            and first_fact_date <= leg.occurrence_date <= as_of
        )
        planning_legs = tuple(
            leg
            for leg in legs
            if leg.account_id == str(account.account_id)
            and leg.provenance != "imported"
            and as_of <= leg.occurrence_date <= end_date
        )
        account_legs = CashPlanService._sorted_legs((*fact_legs, *planning_legs))
        actual_roll_forward = sum((leg.amount for leg in fact_legs), Decimal("0.00"))
        planned_movement = sum((leg.amount for leg in planning_legs), Decimal("0.00"))
        movement_since_anchor = actual_roll_forward + planned_movement
        opening = None if anchor.balance is None else anchor.balance + actual_roll_forward
        projected = None if opening is None else opening + planned_movement
        lowest = opening
        lowest_date = as_of if opening is not None else None
        if opening is not None:
            running = opening
            by_date: defaultdict[date, Decimal] = defaultdict(lambda: Decimal("0.00"))
            for leg in planning_legs:
                by_date[leg.occurrence_date] += leg.amount
            for occurrence_date in sorted(by_date):
                running += by_date[occurrence_date]
                if lowest is None or running < lowest:
                    lowest = running
                    lowest_date = occurrence_date
        return AccountCashPlan(
            account_id=str(account.account_id),
            institution=str(account.institution),
            account_type=str(account.account_type),
            is_credit_card=bool(account.is_credit_card),
            anchor=anchor,
            opening_projected_balance=opening,
            actual_roll_forward_movement=actual_roll_forward,
            planned_movement=planned_movement,
            movement_since_anchor=movement_since_anchor,
            projected_movement=planned_movement,
            projected_balance=projected,
            lowest_projected_balance=lowest,
            lowest_projected_date=lowest_date,
            legs=account_legs,
        )

    @staticmethod
    def _before_next_payday(
        accounts: tuple[AccountCashPlan, ...],
        legs: tuple[CashPlanLeg, ...],
        *,
        as_of: date,
    ) -> BeforeNextPayday | None:
        incomes = [
            leg
            for leg in legs
            if leg.event_type == "income"
            and leg.amount > 0
            and leg.provenance in {"confirmed", "detected"}
            and leg.occurrence_date >= as_of
        ]
        if not incomes:
            return None
        payday = min(leg.occurrence_date for leg in incomes)
        payday_incomes = [leg for leg in incomes if leg.occurrence_date == payday]
        before_legs = [
            leg
            for leg in legs
            if leg.provenance != "imported" and as_of <= leg.occurrence_date < payday
        ]
        account_rows: list[BeforePaydayAccount] = []
        for account in accounts:
            movement = sum(
                (leg.amount for leg in before_legs if leg.account_id == account.account_id),
                Decimal("0.00"),
            )
            projected = (
                None
                if account.opening_projected_balance is None
                else account.opening_projected_balance + movement
            )
            account_rows.append(
                BeforePaydayAccount(
                    account_id=account.account_id,
                    projected_movement=movement,
                    projected_balance_before_payday=projected,
                )
            )
        provenances = tuple(
            sorted(
                {leg.provenance for leg in payday_incomes},
                key=lambda value: _PROVENANCE_ORDER[value],
            )
        )
        return BeforeNextPayday(
            payday_date=payday,
            income_amount=sum((leg.amount for leg in payday_incomes), Decimal("0.00")),
            income_provenances=provenances,
            accounts=tuple(account_rows),
            includes_hypothetical=any(leg.provenance == "hypothetical" for leg in before_legs),
        )

    @staticmethod
    def _assumptions(
        *,
        missing: bool,
        detected: bool,
        hypothetical: bool,
    ) -> tuple[str, ...]:
        assumptions = [
            "All amounts are CAD; no foreign-exchange conversion or live quote lookup is used.",
            "Cash schedules select the definition effective on each occurrence date. Older records without preserved revisions have incomplete history.",
            "Balance levels start from explicitly dated statement-backed absolute rows; they are not live balances.",
            "The plan is arithmetic over disclosed anchors and occurrences, not a prediction that an event will happen.",
            "Imported facts through the as-of date roll the statement anchor into the opening balance. Unmatched planning occurrences before as-of are excluded; an unsuppressed as-of-day planning occurrence remains upcoming.",
            "Bills and income require a unique account/date/amount/type match plus the same normalized merchant name or linked recurring pattern to be suppressed. Without that identity evidence, an upcoming occurrence remains planned and may overlap imported activity.",
        ]
        if detected:
            assumptions.append(
                "Detected recurring patterns are historical possibilities with mean amounts; their stored schema has no currency field, so this CAD plan assumes those patterns describe CAD activity."
            )
        if hypothetical:
            assumptions.append(
                "Hypothetical schedule occurrences are included and labelled hypothetical on every leg."
            )
        if missing:
            assumptions.append(
                "Accounts without a statement-backed absolute anchor keep projected balance and lowest balance unavailable; absence is never reported as zero."
            )
        return tuple(assumptions)


__all__ = [
    "AccountCashPlan",
    "BeforeNextPayday",
    "BeforePaydayAccount",
    "CashPlan",
    "CashPlanAnchor",
    "CashPlanCoverage",
    "CashPlanError",
    "CashPlanFactCoverage",
    "CashPlanLeg",
    "CashPlanService",
    "MAX_ACTUAL_FACT_ROWS",
    "MAX_PLAN_DAYS",
    "OmittedPatternCount",
    "SuppressedOccurrence",
    "schedule_occurrence_dates",
]
