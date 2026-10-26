"""Durable user-authored context for spending recommendations."""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, cast

from sqlalchemy.orm import Session

from ledger.query.base import SessionScopeFactory
from ledger.storage.database import SpendingPreferenceDB, session_scope
from ledger.storage.migrations import ensure_schema
from ledger.storage.operations import utc_now_dt as utc_now
from ledger.storage.snapshots import database_target_lock

PREFERENCE_SCOPES = frozenset({"merchant", "category"})
PREFERENCE_DISPOSITIONS = frozenset({"intentional", "necessary", "worthwhile"})
PREFERENCE_NAME_MAX_LENGTH = 160
PREFERENCE_NOTE_MAX_LENGTH = 500


def _compact_text(value: object) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value)).split())


def normalize_preference_scope(scope: object) -> str:
    normalized = _compact_text(scope).casefold()
    if normalized not in PREFERENCE_SCOPES:
        raise ValueError(f"Unsupported spending preference scope: {scope}")
    return normalized


def normalize_preference_name(scope: object, name: object) -> str:
    normalized_scope = normalize_preference_scope(scope)
    normalized = _compact_text(name)
    if not normalized:
        raise ValueError("Spending preference name is required")
    if normalized_scope == "merchant":
        normalized = normalized.upper()
    if len(normalized) > PREFERENCE_NAME_MAX_LENGTH:
        raise ValueError(
            f"Spending preference name must be at most {PREFERENCE_NAME_MAX_LENGTH} characters"
        )
    return normalized


def _name_key(scope: object, name: object) -> str:
    return normalize_preference_name(scope, name).casefold()


def _normalize_disposition(disposition: object) -> str:
    normalized = _compact_text(disposition).casefold()
    if normalized not in PREFERENCE_DISPOSITIONS:
        raise ValueError(f"Unsupported spending preference disposition: {disposition}")
    return normalized


def _normalize_note(note: object | None) -> str:
    normalized = str(note or "").strip()
    if len(normalized) > PREFERENCE_NOTE_MAX_LENGTH:
        raise ValueError(
            f"Spending preference note must be at most {PREFERENCE_NOTE_MAX_LENGTH} characters"
        )
    return normalized


class SpendingPreferenceService:
    """Validate and persist exact merchant/category recommendation context."""

    def __init__(
        self,
        db_path: Path,
        *,
        session_scope_factory: SessionScopeFactory = session_scope,
        prepare_schema: bool = True,
    ) -> None:
        self.db_path = Path(db_path)
        self._session_scope_factory = session_scope_factory
        if prepare_schema:
            with database_target_lock(self.db_path):
                ensure_schema(self.db_path)

    def _open_session(self) -> AbstractContextManager[Session]:
        return self._session_scope_factory(self.db_path)

    @staticmethod
    def _as_dict(row: SpendingPreferenceDB) -> dict[str, Any]:
        return {
            "id": int(row.id),
            "scope": str(row.scope),
            "name": str(row.name),
            "disposition": str(row.disposition),
            "note": str(row.note or ""),
            "created_at": row.created_at,
            "updated_at": row.updated_at,
        }

    @classmethod
    def list_from_session(cls, session: Session) -> list[dict[str, Any]]:
        rows = session.query(SpendingPreferenceDB).all()
        return sorted(
            (cls._as_dict(row) for row in rows),
            key=lambda row: (row["scope"], row["name"].casefold(), row["id"]),
        )

    def list_preferences(self) -> list[dict[str, Any]]:
        with self._open_session() as session:
            return self.list_from_session(session)

    def upsert(
        self,
        scope: str,
        name: str,
        disposition: str,
        note: str = "",
    ) -> dict[str, Any]:
        normalized_scope = normalize_preference_scope(scope)
        normalized_name = normalize_preference_name(normalized_scope, name)
        name_key = normalized_name.casefold()
        normalized_disposition = _normalize_disposition(disposition)
        normalized_note = _normalize_note(note)
        now = utc_now()

        with database_target_lock(self.db_path), self._open_session() as session:
            matches = [
                row
                for row in session.query(SpendingPreferenceDB)
                .filter(SpendingPreferenceDB.scope == normalized_scope)
                .all()
                if _name_key(normalized_scope, row.name) == name_key
            ]
            if len(matches) > 1:
                raise ValueError("Stored spending preferences contain duplicate normalized names")
            if matches:
                row = cast(Any, matches[0])
                row.name = normalized_name
                row.disposition = normalized_disposition
                row.note = normalized_note
                row.updated_at = now
            else:
                row = SpendingPreferenceDB(
                    scope=normalized_scope,
                    name=normalized_name,
                    disposition=normalized_disposition,
                    note=normalized_note,
                    created_at=now,
                    updated_at=now,
                )
                session.add(row)
            session.flush()
            return self._as_dict(row)

    def delete(self, scope: str, name: str) -> bool:
        normalized_scope = normalize_preference_scope(scope)
        name_key = _name_key(normalized_scope, name)
        with database_target_lock(self.db_path), self._open_session() as session:
            matches = [
                row
                for row in session.query(SpendingPreferenceDB)
                .filter(SpendingPreferenceDB.scope == normalized_scope)
                .all()
                if _name_key(normalized_scope, row.name) == name_key
            ]
            if len(matches) > 1:
                raise ValueError("Stored spending preferences contain duplicate normalized names")
            if not matches:
                return False
            session.delete(matches[0])
            return True

    # Descriptive aliases keep call sites clear without creating a second behavior.
    list = list_preferences
    upsert_preference = upsert
    delete_preference = delete

    @staticmethod
    def match(
        evidence: dict[str, Any],
        preferences: Sequence[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Return the most specific normalized exact match for finding evidence."""
        for scope in ("merchant", "category"):
            raw_name = evidence.get(scope)
            if not isinstance(raw_name, str) or not raw_name.strip():
                continue
            try:
                wanted = _name_key(scope, raw_name)
            except ValueError:
                continue
            for preference in preferences:
                if preference.get("scope") != scope:
                    continue
                try:
                    candidate = _name_key(scope, preference.get("name", ""))
                except ValueError:
                    continue
                if candidate == wanted:
                    return preference
        return None


__all__ = [
    "PREFERENCE_DISPOSITIONS",
    "PREFERENCE_NAME_MAX_LENGTH",
    "PREFERENCE_NOTE_MAX_LENGTH",
    "PREFERENCE_SCOPES",
    "SpendingPreferenceService",
    "normalize_preference_name",
    "normalize_preference_scope",
]
