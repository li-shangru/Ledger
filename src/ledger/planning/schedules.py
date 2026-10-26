"""Validated user-authored cash schedules.

Schedules are planning inputs, never imported facts.  Writes use Ledger's shared
database target lock and audited SQLite writer so an interactive edit cannot race
statement publication.  Read callers may inject an already-pinned session factory.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ledger.query.base import SessionScopeFactory
from ledger.storage.database import PlannedScheduleDB, RecurringPatternDB, session_scope
from ledger.storage.migrations import ensure_schema
from ledger.storage.snapshots import (
    audited_database_write_connection,
    bound_database_read_connection,
    database_target_lock,
    database_write_outcome,
)
from ledger.utils.dates import naive_utc_now
from ledger.utils.money import to_cents

EVENT_TYPES = frozenset({"income", "bill", "card_payment", "transfer"})
FREQUENCIES = frozenset(
    {"once", "weekly", "biweekly", "semimonthly", "monthly", "quarterly", "yearly"}
)
PROVENANCES = frozenset({"confirmed", "hypothetical"})
_PATTERN_EVENT_TYPES = {"income": "income", "bill": "bill", "subscription": "bill"}
_PATTERN_FREQUENCIES = {
    "weekly": "weekly",
    "biweekly": "biweekly",
    "monthly": "monthly",
    "quarterly": "quarterly",
    "annual": "yearly",
}
_MAX_AMOUNT = Decimal("9999999999.99")


class ScheduleValidationError(ValueError):
    """A content-safe schedule validation failure."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class ScheduleNotFoundError(KeyError):
    """The requested schedule or detected pattern no longer exists."""


class ScheduleConflictError(ValueError):
    """The saved schedule no longer matches the caller's reviewed record."""


@dataclass(frozen=True)
class ScheduleDraft:
    name: str
    event_type: str
    amount: Decimal
    currency: str
    account_id: str
    counterparty_account_id: str | None
    next_date: date
    frequency: str
    provenance: str
    pattern_id: str | None = None
    is_active: bool = True
    note: str = ""


@dataclass(frozen=True)
class ScheduleRecord:
    schedule_id: str
    name: str
    event_type: str
    amount: Decimal
    currency: str
    account_id: str
    counterparty_account_id: str | None
    next_date: date
    frequency: str
    provenance: str
    pattern_id: str | None
    is_active: bool
    note: str
    created_at: datetime
    updated_at: datetime

    def to_dict(self) -> dict[str, object]:
        return {
            "schedule_id": self.schedule_id,
            "name": self.name,
            "event_type": self.event_type,
            "amount": f"{self.amount:.2f}",
            "currency": self.currency,
            "account_id": self.account_id,
            "counterparty_account_id": self.counterparty_account_id,
            "next_date": self.next_date.isoformat(),
            "frequency": self.frequency,
            "provenance": self.provenance,
            "pattern_id": self.pattern_id,
            "is_active": self.is_active,
            "note": self.note,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


@dataclass(frozen=True)
class DetectedScheduleCandidate:
    pattern_id: str
    name: str
    event_type: str
    amount: Decimal
    account_id: str
    next_date: date
    frequency: str
    confidence: float

    def to_dict(self) -> dict[str, object]:
        return {
            "pattern_id": self.pattern_id,
            "name": self.name,
            "event_type": self.event_type,
            "amount": f"{self.amount:.2f}",
            "currency": "CAD",
            "account_id": self.account_id,
            "next_date": self.next_date.isoformat(),
            "frequency": self.frequency,
            "provenance": "detected",
            "confidence": self.confidence,
        }


class ScheduleService:
    """Create, edit, confirm, list, and delete local cash schedules."""

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
        """Bind reads to one exact main/WAL snapshot across atomic replacement."""
        path = self.db_path.expanduser().absolute()
        if not path.is_file():
            raise FileNotFoundError(f"Database not found: {path}")
        with bound_database_read_connection(
            path,
            label="Planned schedule read source",
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

    def list(self, *, include_inactive: bool = True) -> tuple[ScheduleRecord, ...]:
        with self._open_session() as session:
            query = session.query(PlannedScheduleDB)
            if not include_inactive:
                query = query.filter(PlannedScheduleDB.is_active.is_(True))
            rows = query.order_by(
                PlannedScheduleDB.next_date,
                PlannedScheduleDB.name,
                PlannedScheduleDB.schedule_id,
            ).all()
            return tuple(self._from_orm(row) for row in rows)

    def create(
        self,
        draft: ScheduleDraft,
        *,
        schedule_id: str | None = None,
        effective_date: date | None = None,
    ) -> ScheduleRecord:
        identifier = self._validated_identifier(schedule_id or secrets.token_hex(16))
        with database_write_outcome() as outcome, database_target_lock(self.db_path):
            with audited_database_write_connection(
                self.db_path, label="Planned schedule create target"
            ) as connection:
                if connection.execute(
                    "SELECT 1 FROM planned_schedules WHERE schedule_id = ?", (identifier,)
                ).fetchone():
                    raise ScheduleValidationError(
                        "duplicate_schedule", "Schedule id already exists."
                    )
                result = self.write_on_connection(
                    connection,
                    identifier,
                    draft,
                    effective_date=effective_date or draft.next_date,
                )
            outcome.mark_committed()
        assert result is not None
        return result

    def update(
        self,
        schedule_id: str,
        draft: ScheduleDraft,
        *,
        expected_record: ScheduleRecord | None = None,
        effective_date: date | None = None,
    ) -> ScheduleRecord:
        with database_write_outcome() as outcome, database_target_lock(self.db_path):
            with audited_database_write_connection(
                self.db_path, label="Planned schedule update target"
            ) as connection:
                current = self._require_current_schedule(connection, schedule_id, expected_record)
                result = self.write_on_connection(
                    connection,
                    schedule_id,
                    draft,
                    expected_record=current,
                    effective_date=self._effective_date(connection, schedule_id, effective_date),
                )
            outcome.mark_committed()
        assert result is not None
        return result

    def confirm(
        self,
        schedule_id: str,
        *,
        expected_record: ScheduleRecord | None = None,
        effective_date: date | None = None,
    ) -> ScheduleRecord:
        with database_write_outcome() as outcome, database_target_lock(self.db_path):
            with audited_database_write_connection(
                self.db_path, label="Planned schedule confirmation target"
            ) as connection:
                current = self._require_current_schedule(connection, schedule_id, expected_record)
                result = self.write_on_connection(
                    connection,
                    schedule_id,
                    replace(self.draft_from_record(current), provenance="confirmed"),
                    expected_record=current,
                    effective_date=self._effective_date(connection, schedule_id, effective_date),
                )
            outcome.mark_committed()
        assert result is not None
        return result

    def delete(
        self,
        schedule_id: str,
        *,
        expected_record: ScheduleRecord | None = None,
        effective_date: date | None = None,
    ) -> bool:
        with database_write_outcome() as outcome, database_target_lock(self.db_path):
            with audited_database_write_connection(
                self.db_path, label="Planned schedule delete target"
            ) as connection:
                current = self._require_current_schedule(connection, schedule_id, expected_record)
                self.write_on_connection(
                    connection,
                    schedule_id,
                    None,
                    expected_record=current,
                    effective_date=self._effective_date(connection, schedule_id, effective_date),
                )
            outcome.mark_committed()
        return True

    @staticmethod
    def _effective_date(connection: Any, schedule_id: str, value: date | None) -> date:
        if value is not None:
            return value
        latest = connection.execute(
            "SELECT MAX(effective_date) FROM planned_schedule_revisions WHERE schedule_id = ?",
            (schedule_id,),
        ).fetchone()[0]
        return max(naive_utc_now().date(), date.fromisoformat(latest) if latest else date.min)

    @staticmethod
    def draft_from_record(record: ScheduleRecord) -> ScheduleDraft:
        return ScheduleDraft(
            **{field: getattr(record, field) for field in ScheduleDraft.__dataclass_fields__}
        )

    @classmethod
    def write_on_connection(
        cls,
        connection: Any,
        schedule_id: str,
        draft: ScheduleDraft | None,
        *,
        effective_date: date,
        expected_record: ScheduleRecord | None = None,
        now: datetime | None = None,
        allow_restore: bool = False,
    ) -> ScheduleRecord | None:
        """Mutate a definition and append its revision in the caller's transaction.

        The caller owns the publication lock, open-import policy, and commit. This
        primitive never commits, makes backups, or opens another connection.
        Existing effective history cannot be overwritten retroactively. Restoring
        a removed identity is reserved for a receipt's conditional undo.
        """
        identifier = cls._validated_identifier(schedule_id)
        now = now or naive_utc_now()
        if not isinstance(effective_date, date) or isinstance(effective_date, datetime):
            raise ScheduleValidationError(
                "invalid_effective_date", "Schedule effective date is invalid."
            )
        row = connection.execute(
            "SELECT * FROM planned_schedules WHERE schedule_id = ?", (identifier,)
        ).fetchone()
        current = cls._from_sql_row(row) if row is not None else None
        if expected_record is not None and current != expected_record:
            raise ScheduleConflictError("Schedule changed or was removed; reload and retry.")
        latest = connection.execute(
            "SELECT * FROM planned_schedule_revisions WHERE schedule_id = ? "
            "ORDER BY effective_date DESC, revision_id DESC LIMIT 1",
            (identifier,),
        ).fetchone()
        if (current is not None or latest is not None) and effective_date < now.date():
            raise ScheduleValidationError(
                "past_effective_date", "Schedule changes must preserve elapsed expectations."
            )
        if latest is not None and effective_date < cls._as_date(latest["effective_date"]):
            raise ScheduleValidationError(
                "earlier_effective_date", "Schedule change precedes an already scheduled revision."
            )
        if current is None and latest is not None and not allow_restore:
            raise ScheduleValidationError(
                "retired_schedule_id", "Removed schedule identity cannot be reused."
            )
        if current is None and draft is None:
            raise ScheduleNotFoundError(identifier)
        if current is not None:
            now = max(now, current.updated_at + timedelta(microseconds=1))
        # Completeness belongs to the effective region, not the entire lineage:
        # an explicit new definition is known from its effective date onward.
        history_complete = True
        if current is not None and latest is None:
            cls._append_revision(connection, current, current.next_date, now, False, False)
        if draft is None:
            assert current is not None
            connection.execute("DELETE FROM planned_schedules WHERE schedule_id = ?", (identifier,))
            cls._append_revision(connection, current, effective_date, now, True, history_complete)
            return None
        normalized = cls._validate_draft(connection, draft, exclude_schedule_id=identifier)
        cls._require_pattern_window_available(connection, identifier, normalized, effective_date)
        created_at = current.created_at if current is not None else now
        if current is None:
            cls._insert(connection, identifier, normalized, created_at, now)
        else:
            connection.execute(
                """UPDATE planned_schedules SET
                    name=?,event_type=?,amount=?,currency=?,account_id=?,counterparty_account_id=?,
                    next_date=?,frequency=?,provenance=?,pattern_id=?,is_active=?,note=?,updated_at=?
                    WHERE schedule_id=?""",
                (*cls._draft_values(normalized), now.isoformat(sep=" "), identifier),
            )
        result = cls._record(identifier, normalized, created_at, now)
        if current is None and latest is None and effective_date < now.date():
            # The supplied cadence anchor may precede its first recording. Keep
            # it for arithmetic, but never assert that its elapsed occurrences
            # were contemporaneous confirmed expectations.
            cls._append_revision(connection, result, effective_date, now, False, False)
            effective_date = now.date()
        cls._append_revision(connection, result, effective_date, now, False, history_complete)
        return result

    @staticmethod
    def _append_revision(connection, record, effective_date, recorded_at, removed, complete):
        connection.execute(
            "INSERT INTO planned_schedule_revisions "
            "(schedule_id,effective_date,recorded_at,definition_json,is_removed,history_complete) "
            "VALUES (?,?,?,?,?,?)",
            (
                record.schedule_id,
                effective_date.isoformat(),
                recorded_at.isoformat(sep=" "),
                json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":")),
                int(removed),
                int(complete),
            ),
        )

    @staticmethod
    def _require_pattern_window_available(connection, schedule_id, draft, effective_date):
        """A future tombstone does not release a pattern's earlier reserved region."""
        if draft.provenance != "confirmed" or draft.pattern_id is None:
            return
        rows = connection.execute(
            "SELECT * FROM planned_schedule_revisions WHERE schedule_id != ? "
            "ORDER BY schedule_id,effective_date,revision_id LIMIT 20001",
            (schedule_id,),
        ).fetchall()
        if len(rows) > 20000:
            raise ScheduleValidationError(
                "history_too_broad", "Schedule history is too broad to validate safely."
            )
        for index, row in enumerate(rows):
            if row["is_removed"]:
                continue
            if index + 1 < len(rows) and rows[index + 1]["schedule_id"] == row["schedule_id"]:
                next_date = date.fromisoformat(rows[index + 1]["effective_date"])
                if next_date <= max(effective_date, date.fromisoformat(row["effective_date"])):
                    continue
            try:
                definition = json.loads(row["definition_json"])
                overlaps = (
                    definition["pattern_id"] == draft.pattern_id
                    and definition["provenance"] == "confirmed"
                )
            except KeyError, TypeError, ValueError:
                raise ScheduleValidationError(
                    "invalid_history", "Schedule history cannot be validated safely."
                ) from None
            if overlaps:
                raise ScheduleValidationError(
                    "pattern_already_confirmed",
                    "Detected pattern has an overlapping confirmed schedule; choose a later effective date.",
                )

    @classmethod
    def _require_current_schedule(
        cls,
        connection: Any,
        identifier: str,
        expected_record: ScheduleRecord | None,
    ) -> ScheduleRecord:
        """Check the reviewed record inside the caller's locked write transaction."""
        row = connection.execute(
            "SELECT * FROM planned_schedules WHERE schedule_id = ?", (identifier,)
        ).fetchone()
        if row is None:
            if expected_record is not None:
                raise ScheduleConflictError("Schedule changed or was removed; reload and retry.")
            raise ScheduleNotFoundError(identifier)
        current = cls._from_sql_row(row)
        if expected_record is not None and current != expected_record:
            raise ScheduleConflictError("Schedule changed or was removed; reload and retry.")
        return current

    def detected_candidates(self) -> tuple[DetectedScheduleCandidate, ...]:
        """Return active projectable patterns without a confirmed schedule link."""
        with self._open_session() as session:
            linked = {
                str(pattern_id)
                for (pattern_id,) in session.query(PlannedScheduleDB.pattern_id)
                .filter(
                    PlannedScheduleDB.provenance == "confirmed",
                    PlannedScheduleDB.pattern_id.is_not(None),
                )
                .all()
            }
            rows = (
                session.query(RecurringPatternDB)
                .filter(RecurringPatternDB.is_active.is_(True))
                .order_by(
                    RecurringPatternDB.next_expected_date,
                    RecurringPatternDB.merchant,
                    RecurringPatternDB.pattern_id,
                )
                .all()
            )
            return tuple(
                DetectedScheduleCandidate(
                    pattern_id=str(row.pattern_id),
                    name=str(row.merchant),
                    event_type=_PATTERN_EVENT_TYPES[str(row.pattern_type)],
                    amount=to_cents(abs(Decimal(str(row.expected_amount)))),
                    account_id=str(row.account_id),
                    next_date=row.next_expected_date,
                    frequency=_PATTERN_FREQUENCIES[str(row.frequency)],
                    confidence=max(0.0, min(1.0, float(row.confidence))),
                )
                for row in rows
                if str(row.pattern_id) not in linked
                and str(row.pattern_type) in _PATTERN_EVENT_TYPES
                and str(row.frequency) in _PATTERN_FREQUENCIES
            )

    def confirm_pattern(
        self,
        pattern_id: str,
        *,
        schedule_id: str | None = None,
        note: str = "",
        effective_date: date | None = None,
    ) -> ScheduleRecord:
        pattern_key = str(pattern_id).strip()
        if not pattern_key or len(pattern_key) > 64:
            raise ScheduleValidationError("invalid_pattern", "Detected pattern id is invalid.")
        identifier = self._validated_identifier(schedule_id or secrets.token_hex(16))
        now = naive_utc_now()
        with database_write_outcome() as outcome, database_target_lock(self.db_path):
            with audited_database_write_connection(
                self.db_path, label="Detected pattern confirmation target"
            ) as connection:
                if connection.execute(
                    "SELECT 1 FROM planned_schedules WHERE schedule_id = ?", (identifier,)
                ).fetchone():
                    raise ScheduleValidationError(
                        "duplicate_schedule", "Schedule id already exists."
                    )
                row = connection.execute(
                    "SELECT * FROM recurring_patterns WHERE pattern_id = ? AND is_active = 1",
                    (pattern_key,),
                ).fetchone()
                if row is None:
                    raise ScheduleNotFoundError(pattern_key)
                event_type = _PATTERN_EVENT_TYPES.get(str(row["pattern_type"]))
                frequency = _PATTERN_FREQUENCIES.get(str(row["frequency"]))
                if event_type is None or frequency is None:
                    raise ScheduleValidationError(
                        "unsupported_pattern", "Detected pattern cannot define a cash schedule."
                    )
                existing_link = connection.execute(
                    "SELECT 1 FROM planned_schedules "
                    "WHERE pattern_id = ? AND provenance = 'confirmed'",
                    (pattern_key,),
                ).fetchone()
                if existing_link is not None:
                    raise ScheduleValidationError(
                        "pattern_already_confirmed", "Detected pattern is already confirmed."
                    )
                draft = ScheduleDraft(
                    name=str(row["merchant"]),
                    event_type=event_type,
                    amount=to_cents(abs(Decimal(str(row["expected_amount"])))),
                    currency="CAD",
                    account_id=str(row["account_id"]),
                    counterparty_account_id=None,
                    next_date=self._as_date(row["next_expected_date"]),
                    frequency=frequency,
                    provenance="confirmed",
                    pattern_id=pattern_key,
                    is_active=True,
                    note=str(note),
                )
                normalized = self._validate_draft(connection, draft)
                result = self.write_on_connection(
                    connection,
                    identifier,
                    normalized,
                    effective_date=effective_date or normalized.next_date,
                    now=now,
                )
            outcome.mark_committed()
        assert result is not None
        return result

    @staticmethod
    def _validated_identifier(value: str) -> str:
        identifier = str(value).strip()
        if not identifier or len(identifier) > 64:
            raise ScheduleValidationError("invalid_schedule_id", "Schedule id is invalid.")
        return identifier

    @classmethod
    def _validate_draft(
        cls,
        connection: Any,
        draft: ScheduleDraft,
        *,
        exclude_schedule_id: str | None = None,
    ) -> ScheduleDraft:
        name = str(draft.name).strip()
        if not name or len(name) > 160:
            raise ScheduleValidationError("invalid_name", "Schedule name is required.")
        event_type = str(draft.event_type).strip()
        if event_type not in EVENT_TYPES:
            raise ScheduleValidationError("invalid_event_type", "Schedule event type is invalid.")
        try:
            amount = Decimal(str(draft.amount))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ScheduleValidationError("invalid_amount", "Schedule amount is invalid.") from exc
        if (
            not amount.is_finite()
            or amount <= 0
            or amount > _MAX_AMOUNT
            or to_cents(amount) != amount
        ):
            raise ScheduleValidationError(
                "invalid_amount", "Schedule amount must be positive cents."
            )
        currency = str(draft.currency).strip().upper()
        if currency != "CAD":
            raise ScheduleValidationError(
                "unsupported_currency", "Cash schedules support CAD only."
            )
        if not isinstance(draft.next_date, date) or isinstance(draft.next_date, datetime):
            raise ScheduleValidationError("invalid_date", "Schedule date is invalid.")
        frequency = str(draft.frequency).strip()
        if frequency not in FREQUENCIES:
            raise ScheduleValidationError("invalid_frequency", "Schedule frequency is invalid.")
        provenance = str(draft.provenance).strip()
        if provenance not in PROVENANCES:
            raise ScheduleValidationError("invalid_provenance", "Schedule provenance is invalid.")
        if not isinstance(draft.is_active, bool):
            raise ScheduleValidationError(
                "invalid_active_state", "Schedule active state must be true or false."
            )
        account_id = str(draft.account_id).strip()
        if not cls._account(connection, account_id):
            raise ScheduleValidationError("unknown_account", "Schedule account does not exist.")
        counterparty = (
            str(draft.counterparty_account_id).strip()
            if draft.counterparty_account_id is not None
            else None
        )
        if event_type in {"card_payment", "transfer"} and not counterparty:
            raise ScheduleValidationError(
                "counterparty_required",
                "Payment and transfer schedules need a destination account.",
            )
        if event_type in {"income", "bill"} and counterparty:
            raise ScheduleValidationError(
                "counterparty_not_allowed", "Income and bill schedules use one account."
            )
        counterparty_row = cls._account(connection, counterparty) if counterparty else None
        if counterparty and counterparty_row is None:
            raise ScheduleValidationError(
                "unknown_counterparty", "Schedule destination account does not exist."
            )
        if counterparty and counterparty == account_id:
            raise ScheduleValidationError(
                "same_account_transfer", "Schedule source and destination must differ."
            )
        if event_type == "card_payment" and (
            counterparty_row is None or not bool(counterparty_row["is_credit_card"])
        ):
            raise ScheduleValidationError(
                "card_destination_required", "Card payments must name a credit-card destination."
            )
        pattern_id = str(draft.pattern_id).strip() if draft.pattern_id is not None else None
        if pattern_id == "":
            pattern_id = None
        if pattern_id is not None and len(pattern_id) > 64:
            raise ScheduleValidationError("invalid_pattern", "Detected pattern id is invalid.")
        if provenance == "confirmed" and pattern_id is not None:
            query = (
                "SELECT 1 FROM planned_schedules WHERE pattern_id = ? AND provenance = 'confirmed'"
            )
            params: tuple[str, ...] = (pattern_id,)
            if exclude_schedule_id is not None:
                query += " AND schedule_id != ?"
                params += (exclude_schedule_id,)
            if connection.execute(query, params).fetchone() is not None:
                raise ScheduleValidationError(
                    "pattern_already_confirmed", "Detected pattern is already confirmed."
                )
        note = str(draft.note)
        if len(note) > 500:
            raise ScheduleValidationError(
                "invalid_note", "Schedule note must be 500 characters or fewer."
            )
        return replace(
            draft,
            name=name,
            event_type=event_type,
            amount=to_cents(amount),
            currency=currency,
            account_id=account_id,
            counterparty_account_id=counterparty,
            frequency=frequency,
            provenance=provenance,
            pattern_id=pattern_id,
            note=note,
        )

    @staticmethod
    def _account(connection: Any, account_id: str | None) -> Any:
        if not account_id:
            return None
        return connection.execute(
            "SELECT account_id, is_credit_card FROM accounts WHERE account_id = ?", (account_id,)
        ).fetchone()

    @staticmethod
    def _draft_values(draft: ScheduleDraft) -> tuple[object, ...]:
        return (
            draft.name,
            draft.event_type,
            f"{draft.amount:.2f}",
            draft.currency,
            draft.account_id,
            draft.counterparty_account_id,
            draft.next_date.isoformat(),
            draft.frequency,
            draft.provenance,
            draft.pattern_id,
            int(draft.is_active),
            draft.note,
        )

    @classmethod
    def _insert(
        cls,
        connection: Any,
        schedule_id: str,
        draft: ScheduleDraft,
        created_at: datetime,
        updated_at: datetime,
    ) -> None:
        connection.execute(
            """
            INSERT INTO planned_schedules (
                schedule_id, name, event_type, amount, currency, account_id,
                counterparty_account_id, next_date, frequency, provenance,
                pattern_id, is_active, note, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                schedule_id,
                *cls._draft_values(draft),
                created_at.isoformat(sep=" "),
                updated_at.isoformat(sep=" "),
            ),
        )

    @staticmethod
    def _record(
        schedule_id: str,
        draft: ScheduleDraft,
        created_at: datetime,
        updated_at: datetime,
    ) -> ScheduleRecord:
        return ScheduleRecord(
            schedule_id=schedule_id,
            name=draft.name,
            event_type=draft.event_type,
            amount=draft.amount,
            currency=draft.currency,
            account_id=draft.account_id,
            counterparty_account_id=draft.counterparty_account_id,
            next_date=draft.next_date,
            frequency=draft.frequency,
            provenance=draft.provenance,
            pattern_id=draft.pattern_id,
            is_active=draft.is_active,
            note=draft.note,
            created_at=created_at,
            updated_at=updated_at,
        )

    @classmethod
    def _from_orm(cls, row: PlannedScheduleDB) -> ScheduleRecord:
        return ScheduleRecord(
            schedule_id=str(row.schedule_id),
            name=str(row.name),
            event_type=str(row.event_type),
            amount=to_cents(row.amount),
            currency=str(row.currency),
            account_id=str(row.account_id),
            counterparty_account_id=(
                None if row.counterparty_account_id is None else str(row.counterparty_account_id)
            ),
            next_date=row.next_date,
            frequency=str(row.frequency),
            provenance=str(row.provenance),
            pattern_id=None if row.pattern_id is None else str(row.pattern_id),
            is_active=bool(row.is_active),
            note=str(row.note),
            created_at=row.created_at,
            updated_at=row.updated_at,
        )

    @classmethod
    def _from_sql_row(cls, row: Any) -> ScheduleRecord:
        return ScheduleRecord(
            schedule_id=str(row["schedule_id"]),
            name=str(row["name"]),
            event_type=str(row["event_type"]),
            amount=to_cents(row["amount"]),
            currency=str(row["currency"]),
            account_id=str(row["account_id"]),
            counterparty_account_id=(
                None
                if row["counterparty_account_id"] is None
                else str(row["counterparty_account_id"])
            ),
            next_date=cls._as_date(row["next_date"]),
            frequency=str(row["frequency"]),
            provenance=str(row["provenance"]),
            pattern_id=None if row["pattern_id"] is None else str(row["pattern_id"]),
            is_active=bool(row["is_active"]),
            note=str(row["note"]),
            created_at=cls._as_datetime(row["created_at"]),
            updated_at=cls._as_datetime(row["updated_at"]),
        )

    @staticmethod
    def _as_date(value: object) -> date:
        return value if isinstance(value, date) else date.fromisoformat(str(value))

    @staticmethod
    def _as_datetime(value: object) -> datetime:
        return value if isinstance(value, datetime) else datetime.fromisoformat(str(value))


__all__ = [
    "DetectedScheduleCandidate",
    "EVENT_TYPES",
    "FREQUENCIES",
    "PROVENANCES",
    "ScheduleConflictError",
    "ScheduleDraft",
    "ScheduleNotFoundError",
    "ScheduleRecord",
    "ScheduleService",
    "ScheduleValidationError",
]
