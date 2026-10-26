"""Operator-reviewed typed planning changes with record-scoped durable receipts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from ledger.core.results import RunResult
from ledger.proposals.database import proposal_session
from ledger.proposals.intents import PlanningIntent, validate_planning_intent
from ledger.utils.dates import naive_utc_now

PLANNING_STATUSES = frozenset({"pending", "applied", "rejected", "undone"})


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def proposal_revision(proposal_id: str, intent: PlanningIntent) -> str:
    return hashlib.sha256(
        canonical_json([proposal_id, intent.model_dump(mode="json", exclude_unset=True)]).encode()
    ).hexdigest()


@dataclass(frozen=True)
class PlanningProposal:
    proposal_id: str
    proposal_revision: str
    status: str
    intent: PlanningIntent
    created_at: datetime
    applied_at: datetime | None = None
    rejected_at: datetime | None = None
    undone_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "proposal_revision": self.proposal_revision,
            "status": self.status,
            "intent": self.intent.model_dump(mode="json", exclude_unset=True),
            **{
                key: value.isoformat() if value else None
                for key, value in (
                    ("created_at", self.created_at),
                    ("applied_at", self.applied_at),
                    ("rejected_at", self.rejected_at),
                    ("undone_at", self.undone_at),
                )
            },
        }


@dataclass(frozen=True)
class PlanningPreview:
    proposal_id: str
    proposal_revision: str
    preview_revision: str
    status: str
    operation: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    target_revision: str | None
    effective_date: str | None
    next_occurrences: tuple[str, ...]
    assumptions: tuple[str, ...]
    validation_errors: tuple[str, ...]
    can_apply: bool
    receipt_after_revision: str | None = None
    actions: tuple[str, ...] = ()
    impact: dict[str, Any] = field(default_factory=dict)
    recovery_required: bool = False
    application_preview_revision: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _proposal(row: sqlite3.Row | None) -> PlanningProposal:
    if row is None:
        raise KeyError("Planning proposal not found.")
    intent = validate_planning_intent(json.loads(row["intent_json"]))
    if proposal_revision(str(row["proposal_id"]), intent) != row["proposal_revision"]:
        raise ValueError("Planning proposal content changed; file and review a new proposal.")
    return PlanningProposal(
        proposal_id=str(row["proposal_id"]),
        proposal_revision=str(row["proposal_revision"]),
        status=str(row["status"]),
        intent=intent,
        created_at=datetime.fromisoformat(row["created_at"]),
        applied_at=datetime.fromisoformat(row["applied_at"]) if row["applied_at"] else None,
        rejected_at=datetime.fromisoformat(row["rejected_at"]) if row["rejected_at"] else None,
        undone_at=datetime.fromisoformat(row["undone_at"]) if row["undone_at"] else None,
    )


class PlanningProposalMixin:
    """Separate typed methods preserve the legacy merchant-rule API unchanged."""

    proposals_db: Path

    def file_planning_change(self, intent: object) -> PlanningProposal:
        normalized = validate_planning_intent(intent)
        identifier = str(uuid4())
        revision = proposal_revision(identifier, normalized)
        now = naive_utc_now()
        with proposal_session(self.proposals_db, create=True, writable=True) as connection:
            connection.execute(
                "INSERT INTO planning_proposals "
                "(proposal_id,proposal_revision,intent_json,status,created_at) VALUES (?,?,?,'pending',?)",
                (
                    identifier,
                    revision,
                    canonical_json(normalized.model_dump(mode="json", exclude_unset=True)),
                    now.isoformat(),
                ),
            )
        return PlanningProposal(identifier, revision, "pending", normalized, now)

    def list_planning_proposals(self, *, status: str = "pending") -> list[PlanningProposal]:
        if status not in PLANNING_STATUSES | {"all"}:
            raise ValueError("Planning proposal status is invalid.")
        if not self.proposals_db.exists():
            return []
        with proposal_session(self.proposals_db, create=False, writable=False) as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='planning_proposals'"
                ).fetchone()
                is None
            ):
                return []
            query = "SELECT * FROM planning_proposals"
            args = () if status == "all" else (status,)
            if status != "all":
                query += " WHERE status = ?"
            rows = connection.execute(query + " ORDER BY created_at, proposal_id", args)
            return [_proposal(row) for row in rows]

    def show_planning_proposal(self, proposal_id: str) -> PlanningProposal:
        with proposal_session(self.proposals_db, create=False, writable=False) as connection:
            return _proposal(
                connection.execute(
                    "SELECT * FROM planning_proposals WHERE proposal_id = ?", (proposal_id,)
                ).fetchone()
            )

    def reject_planning_proposal(
        self, proposal_id: str, *, expected_proposal_revision: str
    ) -> PlanningProposal:
        with proposal_session(self.proposals_db, create=False, writable=True) as connection:
            proposal = _proposal(
                connection.execute(
                    "SELECT * FROM planning_proposals WHERE proposal_id = ?", (proposal_id,)
                ).fetchone()
            )
            if (
                connection.execute(
                    "SELECT 1 FROM planning_proposal_applications WHERE proposal_id=?",
                    (proposal_id,),
                ).fetchone()
                is not None
            ):
                raise ValueError(
                    "Proposal application is in progress or needs receipt recovery; retry the exact reviewed application before rejecting."
                )
            if (
                proposal.status != "pending"
                or proposal.proposal_revision != expected_proposal_revision
            ):
                raise ValueError(
                    "Only the exact pending proposal may be rejected; refresh its review."
                )
            connection.execute(
                "UPDATE planning_proposals SET status='rejected',rejected_at=? WHERE proposal_id=?",
                (naive_utc_now().isoformat(), proposal_id),
            )
            return _proposal(
                connection.execute(
                    "SELECT * FROM planning_proposals WHERE proposal_id = ?", (proposal_id,)
                ).fetchone()
            )

    def preview_planning_proposal(self, proposal_id: str, *, ledger_db: Path) -> PlanningPreview:
        from ledger.proposals.planning_apply import preview_planning_proposal

        return preview_planning_proposal(self, proposal_id, ledger_db=ledger_db)

    def recover_planning_storage(self, *, ledger_db: Path) -> RunResult:
        from ledger.proposals.planning_apply import recover_planning_storage

        return recover_planning_storage(self, ledger_db=ledger_db)

    def apply_planning_proposal(
        self,
        proposal_id: str,
        *,
        ledger_db: Path,
        expected_proposal_revision: str,
        expected_preview_revision: str,
    ) -> RunResult:
        from ledger.proposals.planning_apply import apply_planning_proposal

        return apply_planning_proposal(
            self,
            proposal_id,
            ledger_db=ledger_db,
            expected_proposal_revision=expected_proposal_revision,
            expected_preview_revision=expected_preview_revision,
        )

    def undo_planning_proposal(
        self, proposal_id: str, *, ledger_db: Path, expected_after_revision: str
    ) -> RunResult:
        from ledger.proposals.planning_apply import undo_planning_proposal

        return undo_planning_proposal(
            self, proposal_id, ledger_db=ledger_db, expected_after_revision=expected_after_revision
        )

    @staticmethod
    def _mark_planning_status(
        connection: sqlite3.Connection, proposal_id: str, status: str
    ) -> None:
        if status not in {"applied", "undone"}:
            raise ValueError("Invalid receipt tail status.")
        column = "applied_at" if status == "applied" else "undone_at"
        connection.execute(
            f"UPDATE planning_proposals SET status=?,{column}=? WHERE proposal_id=?",
            (status, naive_utc_now().isoformat(), proposal_id),
        )
        connection.execute(
            "DELETE FROM planning_proposal_applications WHERE proposal_id=?", (proposal_id,)
        )
