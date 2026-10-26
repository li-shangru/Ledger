"""Select immutable effective schedule definitions for bounded calendar expansion."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from ledger.storage.database import PlannedScheduleDB, PlannedScheduleRevisionDB

from .schedules import ScheduleRecord, ScheduleService


@dataclass(frozen=True)
class EffectiveSchedule:
    record: ScheduleRecord
    start_date: date
    end_date: date
    revision: str
    history_complete: bool


def definition_revision(record: ScheduleRecord, effective_date: date) -> str:
    definition = record.to_dict()
    # An updated annotation or recording timestamp is not new payment evidence.
    for field in ("created_at", "updated_at", "note"):
        definition.pop(field)
    definition["effective_date"] = effective_date.isoformat()
    return hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()


def incomplete_history_before_baseline(
    session: Session,
    start_date: date,
    end_date: date,
    *,
    max_sources: int = 20_000,
) -> set[str]:
    """Disclose unknown pre-baseline history without expanding a future definition.

    Revisions append in effective-date order. The first preserved legacy revision
    may start at a future cadence anchor despite the record having existed earlier.
    Its later correction or removal cannot recover that earlier missing history.
    """
    from .cash_plan import CashPlanError

    first_ids = (
        session.query(func.min(PlannedScheduleRevisionDB.revision_id))
        .group_by(PlannedScheduleRevisionDB.schedule_id)
        .scalar_subquery()
    )
    baselines = (
        session.query(PlannedScheduleRevisionDB)
        .filter(
            PlannedScheduleRevisionDB.revision_id.in_(first_ids),
            PlannedScheduleRevisionDB.history_complete.is_(False),
            PlannedScheduleRevisionDB.effective_date > start_date,
        )
        .limit(max_sources + 1)
        .all()
    )
    if len(baselines) > max_sources:
        raise CashPlanError("filter_too_broad", "Schedule baseline set is too broad.")
    incomplete = set()
    for baseline in baselines:
        try:
            record = ScheduleService._from_sql_row(json.loads(baseline.definition_json))
            if record.schedule_id != baseline.schedule_id:
                raise ValueError("Definition identity differs")
        except KeyError, TypeError, ValueError:
            raise CashPlanError(
                "invalid_stored_schedule", "Stored schedule history is invalid."
            ) from None
        if record.created_at.date() <= end_date:
            incomplete.add(record.schedule_id)
    return incomplete


def effective_schedules(
    session: Session,
    start_date: date,
    end_date: date,
    *,
    max_sources: int = 20_000,
) -> tuple[EffectiveSchedule, ...]:
    """Retain prior and removed definitions; raw legacy rows are explicitly incomplete.

    Reading never backfills history. Migration and the first subsequent mutation
    make the baseline durable; neither can recover unknown earlier definitions.
    """
    from .cash_plan import CashPlanError

    rows = (
        session.query(PlannedScheduleRevisionDB)
        .filter(PlannedScheduleRevisionDB.effective_date <= end_date)
        .order_by(
            PlannedScheduleRevisionDB.schedule_id,
            PlannedScheduleRevisionDB.effective_date,
            PlannedScheduleRevisionDB.revision_id,
        )
        .limit(max_sources + 1)
        .all()
    )
    if len(rows) > max_sources:
        raise CashPlanError("filter_too_broad", "Schedule revision history is too broad.")
    grouped: dict[str, list[PlannedScheduleRevisionDB]] = defaultdict(list)
    for row in rows:
        grouped[row.schedule_id].append(row)
    current = session.query(PlannedScheduleDB).limit(max_sources + 1).all()
    if len(current) > max_sources:
        raise CashPlanError("filter_too_broad", "Schedule definition set is too broad.")
    found: list[EffectiveSchedule] = []
    for schedule_id, revisions in grouped.items():
        for index, revision in enumerate(revisions):
            start = max(start_date, revision.effective_date)
            end = end_date
            if index + 1 < len(revisions):
                next_start = revisions[index + 1].effective_date
                if next_start <= start:
                    continue
                end = min(end, next_start - timedelta(days=1))
            if start > end or revision.is_removed:
                continue
            try:
                record = ScheduleService._from_sql_row(json.loads(revision.definition_json))
                if record.schedule_id != schedule_id:
                    raise ValueError("Definition identity differs")
            except KeyError, TypeError, ValueError:
                raise CashPlanError(
                    "invalid_stored_schedule", "Stored schedule history is invalid."
                ) from None
            found.append(
                EffectiveSchedule(
                    record,
                    start,
                    end,
                    definition_revision(record, revision.effective_date),
                    bool(revision.history_complete),
                )
            )
    # A schedule whose first revision is after end_date must not be mistaken for a
    # legacy row and projected backward from its current future definition.
    history_ids = {
        value
        for (value,) in session.query(PlannedScheduleRevisionDB.schedule_id)
        .distinct()
        .limit(max_sources + 1)
    }
    if len(history_ids) > max_sources:
        raise CashPlanError("filter_too_broad", "Schedule identity set is too broad.")
    for current_row in current:
        if current_row.schedule_id not in history_ids:
            record = ScheduleService._from_orm(current_row)
            found.append(
                EffectiveSchedule(
                    record,
                    start_date,
                    end_date,
                    definition_revision(record, record.next_date),
                    False,
                )
            )
    return tuple(
        sorted(found, key=lambda item: (item.start_date, item.record.schedule_id, item.revision))
    )


__all__ = [
    "EffectiveSchedule",
    "effective_schedules",
    "definition_revision",
    "incomplete_history_before_baseline",
]
