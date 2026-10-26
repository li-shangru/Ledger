"""Local-only preview, atomic application, and conditional targeted undo."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass, replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from ledger.core.results import RunResult
from ledger.imports.pdf.readiness import live_mutation_boundary
from ledger.insights.preferences import normalize_preference_name
from ledger.planning.cash_plan import schedule_occurrence_dates
from ledger.planning.discovery import (
    content_revision,
    read_reference_salt,
    record_snapshot,
    resolve_reference,
)
from ledger.planning.schedules import ScheduleDraft, ScheduleService
from ledger.planning.service import BudgetService
from ledger.proposals.database import proposal_session, recover_proposal_database
from ledger.proposals.intents import (
    BudgetCreate,
    BudgetRemove,
    BudgetUpdate,
    PlanningReference,
    PreferenceChange,
    PreferenceRemove,
    PreferenceSet,
    ScheduleAction,
    ScheduleCreate,
    ScheduleUpdate,
)
from ledger.proposals.planning import (
    PlanningPreview,
    PlanningProposal,
    PlanningProposalMixin,
    _proposal,
    canonical_json,
)
from ledger.storage.migrations import invalidate_db_caches
from ledger.storage.operations import record_db_operation
from ledger.storage.snapshots import (
    DatabaseWriteCommittedError,
    audited_database_write_connection,
    bound_database_read_connection,
    database_write_outcome,
    recover_database_journal,
)
from ledger.utils.dates import naive_utc_now

_TARGETS = {
    "budget": ("budgets", "id"),
    "preference": ("spending_preferences", "id"),
    "schedule": ("planned_schedules", "schedule_id"),
}
_METADATA = {"id", "schedule_id", "created_at", "updated_at", "_history_revision"}


@dataclass(frozen=True)
class _Resolved:
    kind: str
    target_id: str
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    effective_date: date | None
    dependencies: tuple[dict[str, Any], ...]
    next_occurrences: tuple[str, ...]
    assumptions: tuple[str, ...]


def _definition(record: dict[str, Any] | None) -> dict[str, Any] | None:
    return (
        {key: value for key, value in record.items() if key not in _METADATA}
        if record is not None
        else None
    )


def _read_target(
    connection: sqlite3.Connection, kind: str, identifier: str
) -> dict[str, Any] | None:
    table, key = _TARGETS[kind]
    row = connection.execute(f"SELECT * FROM {table} WHERE {key} = ?", (identifier,)).fetchone()
    return record_snapshot(kind, row, connection=connection) if row is not None else None


def _state_revision(
    connection: sqlite3.Connection,
    salt: str,
    kind: str,
    identifier: str,
    *,
    footprint: tuple[dict[str, Any] | None, ...] = (),
) -> str:
    current = _read_target(connection, kind, identifier)
    # Removal is state too. For schedules, bind the tombstone to the latest
    # append-only revision, so a restore/remove cycle cannot masquerade as it.
    history = None
    if kind == "schedule":
        row = connection.execute(
            "SELECT * FROM planned_schedule_revisions WHERE schedule_id=? ORDER BY revision_id DESC LIMIT 1",
            (identifier,),
        ).fetchone()
        history = dict(row) if row is not None else None
    return content_revision(
        salt,
        kind + "-receipt",
        {
            "target_id": identifier,
            "record": current,
            "history": history,
            "logical_generations": _logical_generations(connection, kind, footprint or (current,)),
        },
    )


def _logical_generations(
    connection: sqlite3.Connection, kind: str, records: tuple[dict[str, Any] | None, ...]
) -> list[dict[str, Any]]:
    """Bind absence to logical-key history, including ordinary local edits.

    Shared Unicode normalization combines equivalent preference names. Every
    spelling's durable generation participates; SQLite lower() is ASCII-only.
    """
    if kind == "schedule":
        return []
    triggers = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'planning_generation_%'"
        )
    }
    required = {
        f"planning_generation_{table}_{operation}"
        for table in ("budgets", "spending_preferences")
        for operation in ("insert", "delete", "update", "rename")
    }
    if not required <= triggers:
        raise ValueError(
            "Planning mutation guards are not installed; run the local database migration before reviewing proposals."
        )
    keys = set()
    for record in records:
        if record is None:
            continue
        if kind == "budget":
            keys.add((str(record["budget_month"]), str(record["category"])))
        else:
            scope = str(record["scope"])
            keys.add((scope, normalize_preference_name(scope, record["name"]).casefold()))
    result = []
    for key_a, key_b in sorted(keys):
        if kind == "budget":
            rows = connection.execute(
                "SELECT key_a,key_b,generation FROM planning_definition_generations WHERE target_kind=? AND key_a=? AND key_b=?",
                (kind, key_a, key_b),
            ).fetchall()
        else:
            candidates = connection.execute(
                "SELECT key_a,key_b,generation FROM planning_definition_generations WHERE target_kind=? AND key_a=? ORDER BY key_b LIMIT 20001",
                (kind, key_a),
            ).fetchall()
            if len(candidates) > 20000:
                raise ValueError(
                    "Preference change history is too broad for safe conditional application."
                )
            rows = [
                row
                for row in candidates
                if normalize_preference_name(key_a, row["key_b"]).casefold() == key_b
            ]
        result.append({"key_a": key_a, "key_b": key_b, "generations": [dict(row) for row in rows]})
    return result


def _draft(values: dict[str, Any]) -> ScheduleDraft:
    return ScheduleDraft(
        name=values["name"],
        event_type=values["event_type"],
        amount=Decimal(values["amount"]),
        currency=values["currency"],
        account_id=values["account_id"],
        counterparty_account_id=values["counterparty_account_id"],
        next_date=date.fromisoformat(values["next_date"]),
        frequency=values["frequency"],
        provenance=values["provenance"],
        pattern_id=values.get("pattern_id"),
        is_active=bool(values["is_active"]),
        note=values["note"],
    )


def _draft_dict(draft: ScheduleDraft) -> dict[str, Any]:
    values = asdict(draft)
    values["amount"] = f"{draft.amount:.2f}"
    values["next_date"] = draft.next_date.isoformat()
    return values


def _resolve_change(
    connection: sqlite3.Connection, proposal: PlanningProposal, salt: str
) -> _Resolved:
    intent = proposal.intent
    operation = intent.operation
    kind = operation.split("_", 1)[0]
    dependencies: list[dict[str, Any]] = []

    def resolve(domain: str, reference: PlanningReference) -> dict[str, Any]:
        record = resolve_reference(
            connection, salt=salt, kind=domain, ref=reference.ref, revision=reference.revision
        )
        dependencies.append({"kind": domain, "revision": content_revision(salt, domain, record)})
        return record

    target = getattr(intent, "target", None)
    before = resolve(kind, target) if target is not None else None
    identifier = (
        str(before[_TARGETS[kind][1]])
        if before
        else (
            proposal.proposal_id.replace("-", "")
            if kind == "schedule"
            else str(int(proposal.proposal_id.replace("-", "")[:15], 16))
        )
    )
    if before is None and _read_target(connection, kind, identifier) is not None:
        raise ValueError("Proposed record identity is already occupied; file a new proposal.")
    after = _definition(before)
    effective: date | None = date.today()
    occurrences: tuple[str, ...] = ()
    assumptions: tuple[str, ...]

    if isinstance(intent, (BudgetCreate, BudgetUpdate)):
        category = resolve("category", intent.category)
        if (
            connection.execute(
                "SELECT 1 FROM category_config WHERE parent=? LIMIT 1", (category["name"],)
            ).fetchone()
            is not None
        ):
            raise ValueError("Budget category must be a current leaf category.")
        after = {
            "budget_month": intent.budget_month,
            "category": category["name"],
            "amount": intent.amount,
            "notes": intent.notes
            if "notes" in intent.model_fields_set or before is None
            else before["notes"],
        }
        errors = BudgetService._budget_errors(
            [{**after, "month": intent.budget_month, "amount": Decimal(intent.amount)}]
        )
        if errors:
            raise ValueError(
                "Budget definition is invalid; review its month, category, and amount."
            )
        duplicate = connection.execute(
            "SELECT id FROM budgets WHERE budget_month=? AND category=? AND id!=? LIMIT 1",
            (intent.budget_month, category["name"], identifier),
        ).fetchone()
        if duplicate is not None:
            raise ValueError(
                "A budget already exists for that month and category; discover and update it explicitly."
            )
    elif isinstance(intent, BudgetRemove):
        after = None
    elif isinstance(intent, PreferenceSet):
        subject = resolve(intent.scope, intent.subject)
        name = normalize_preference_name(
            intent.scope,
            subject["name"] if intent.scope == "category" else subject["merchant_norm"],
        )
        rows = connection.execute(
            "SELECT name FROM spending_preferences WHERE scope=?", (intent.scope,)
        )
        if any(
            normalize_preference_name(intent.scope, row[0]).casefold() == name.casefold()
            for row in rows
        ):
            raise ValueError(
                "A preference already exists for this subject; discover and change it explicitly."
            )
        after = {
            "scope": intent.scope,
            "name": name,
            "disposition": intent.disposition,
            "note": intent.note,
        }
    elif isinstance(intent, PreferenceChange):
        assert after is not None
        after["disposition"] = intent.disposition
        if intent.note is not None:
            after["note"] = intent.note
    elif isinstance(intent, PreferenceRemove):
        after = None
    elif isinstance(intent, (ScheduleCreate, ScheduleUpdate, ScheduleAction)):
        effective = date.fromisoformat(intent.effective_date)
        if effective <= date.today():
            raise ValueError(
                "Schedule proposals must take effect after today; choose a future effective date."
            )
        latest = connection.execute(
            "SELECT effective_date FROM planned_schedule_revisions WHERE schedule_id=? ORDER BY effective_date DESC,revision_id DESC LIMIT 1",
            (identifier,),
        ).fetchone()
        if latest is not None and effective < date.fromisoformat(latest[0]):
            raise ValueError(
                "A later schedule revision is already queued; use its effective date or later."
            )
        if isinstance(intent, ScheduleCreate):
            after = intent.model_dump(
                mode="json",
                exclude={"operation", "effective_date", "account", "counterparty_account"},
            )
            after["account_id"] = resolve("account", intent.account)["account_id"]
            after["counterparty_account_id"] = (
                resolve("account", intent.counterparty_account)["account_id"]
                if intent.counterparty_account
                else None
            )
            after["pattern_id"] = None
            if date.fromisoformat(intent.next_date) < effective:
                raise ValueError(
                    "A new schedule's first occurrence must be on or after its effective date."
                )
        elif isinstance(intent, ScheduleUpdate):
            assert after is not None
            changes = intent.changes.model_dump(
                mode="json", exclude_unset=True, exclude={"account", "counterparty_account"}
            )
            after.update(changes)
            if intent.changes.account is not None:
                after["account_id"] = resolve("account", intent.changes.account)["account_id"]
            if "counterparty_account" in intent.changes.model_fields_set:
                after["counterparty_account_id"] = (
                    resolve("account", intent.changes.counterparty_account)["account_id"]
                    if intent.changes.counterparty_account
                    else None
                )
        else:
            assert after is not None
            if operation == "schedule_remove":
                after = None
            elif operation == "schedule_confirm":
                if after["provenance"] == "confirmed":
                    raise ValueError("Schedule is already confirmed.")
                after["provenance"] = "confirmed"
            elif operation in {"schedule_deactivate", "schedule_reactivate"}:
                active = operation == "schedule_reactivate"
                if after["is_active"] == active:
                    raise ValueError("Schedule already has the requested active state.")
                after["is_active"] = active
        if after is not None:
            normalized = ScheduleService._validate_draft(
                connection, _draft(after), exclude_schedule_id=identifier
            )
            ScheduleService._require_pattern_window_available(
                connection, identifier, normalized, effective
            )
            after = _draft_dict(normalized)
            # Bind existing account semantics too, not only supplied new refs.
            for account_id in (after["account_id"], after["counterparty_account_id"]):
                if account_id:
                    row = connection.execute(
                        "SELECT * FROM accounts WHERE account_id=?", (account_id,)
                    ).fetchone()
                    dependencies.append(
                        {
                            "kind": "account",
                            "revision": content_revision(
                                salt, "account", record_snapshot("account", row)
                            ),
                        }
                    )
            if normalized.is_active:
                end = min(
                    date.max, effective + timedelta(days=min(2192, (date.max - effective).days))
                )
                occurrences = tuple(
                    day.isoformat()
                    for day in schedule_occurrence_dates(
                        normalized.next_date, normalized.frequency, effective, end
                    )[:6]
                )
                if not occurrences:
                    raise ValueError(
                        "The effective schedule has no upcoming occurrence in the next six years."
                    )

    if kind == "budget":
        assumptions = (
            "Budget currency is CAD; amounts are category limits, not cash movements.",
            "Actuals use gross spending; recurring '*' monthly budgets are prorated to the selected window.",
            "Explicit monthly budgets override the recurring budget for that month.",
        )
    elif kind == "preference":
        assumptions = (
            "Preference changes recommendation priority only; imported facts and material new evidence remain unchanged.",
        )
    else:
        assumptions = (
            "Only future expectations from the effective date change; earlier expectations are preserved.",
            "Amounts are CAD magnitudes; income credits its account and bills debit it.",
            "Transfers and card payments create opposite account legs and do not create additional spending.",
            "Occurrence previews are planning inputs; cash plans disclose statement coverage and suppress only uniquely matched imported facts.",
        )
    if before is not None and after == _definition(before):
        raise ValueError("This proposal makes no change to the current definition.")
    if kind in {"budget", "preference"}:
        dependencies.append(
            {
                "kind": kind + "-logical-generation",
                "revision": content_revision(
                    salt, kind, _logical_generations(connection, kind, (before, after))
                ),
            }
        )
    return _Resolved(
        kind, identifier, before, after, effective, tuple(dependencies), occurrences, assumptions
    )


def _impact(resolved: _Resolved) -> dict[str, Any]:
    """Quantify definition changes, without claiming a balance forecast."""
    before, after = resolved.before, resolved.after
    if resolved.kind == "preference":
        subject = after or before
        assert subject is not None
        return {
            "scope": subject["scope"],
            "before_disposition": before["disposition"] if before else None,
            "after_disposition": after["disposition"] if after else None,
            "effect": "recommendation_priority_only",
        }
    if resolved.kind == "budget":
        changes: dict[tuple[str, str], dict[str, Any]] = {}
        for side, record in (("before", before), ("after", after)):
            if record is None:
                continue
            key = (record["category"], record["budget_month"])
            item = changes.setdefault(
                key,
                {
                    "category": key[0],
                    "budget_month": key[1],
                    "currency": "CAD",
                    "before_limit": None,
                    "after_limit": None,
                },
            )
            item[side + "_limit"] = record["amount"]
        for item in changes.values():
            old, new = item["before_limit"], item["after_limit"]
            item["delta"] = (
                f"{Decimal(new) - Decimal(old):.2f}"
                if old is not None and new is not None
                else None
            )
            item["change_type"] = (
                "added" if old is None else "removed" if new is None else "changed"
            )
        return {
            "basis": "budget_definition_limit",
            "budget_limit_changes": list(changes.values()),
            "missing_limit_semantics": "An absent definition is not a zero limit; removing a monthly override may expose its recurring budget.",
        }

    assert resolved.effective_date is not None
    start = resolved.effective_date
    end = start + timedelta(days=min(365, (date.max - start).days))
    next_end = start + timedelta(days=min(2192, (date.max - start).days))

    def movements(record: dict[str, Any] | None) -> tuple[dict[str, Decimal], Decimal, list[str]]:
        if record is None or not record["is_active"]:
            return {}, Decimal("0"), []
        dates = schedule_occurrence_dates(
            date.fromisoformat(record["next_date"]), record["frequency"], start, end
        )
        upcoming = schedule_occurrence_dates(
            date.fromisoformat(record["next_date"]), record["frequency"], start, next_end
        )[:6]
        total = Decimal(record["amount"]) * len(dates)
        source = str(record["account_id"])
        values = {source: total if record["event_type"] == "income" else -total}
        if record["event_type"] in {"card_payment", "transfer"}:
            values[str(record["counterparty_account_id"])] = total
        spending = total if record["event_type"] == "bill" else Decimal("0")
        return values, spending, [day.isoformat() for day in upcoming]

    old, old_spending, old_dates = movements(before)
    new, new_spending, new_dates = movements(after)
    return {
        "basis": "unsuppressed_planning_inputs",
        "currency": "CAD",
        "horizon_start": start.isoformat(),
        "horizon_end": end.isoformat(),
        "horizon_days": (end - start).days + 1,
        "before_next_occurrences": old_dates,
        "after_next_occurrences": new_dates,
        "account_movements": [
            {
                "account_id": account,
                "before_movement": f"{old.get(account, Decimal('0')):.2f}",
                "after_movement": f"{new.get(account, Decimal('0')):.2f}",
                "delta": f"{new.get(account, Decimal('0')) - old.get(account, Decimal('0')):.2f}",
            }
            for account in sorted(old.keys() | new.keys())
        ],
        "planned_spending_delta": f"{new_spending - old_spending:.2f}",
        "coverage_assumption": "These are planned magnitudes before imported-event suppression; no actual account balance or statement completeness is inferred.",
    }


def _build_preview(
    connection: sqlite3.Connection, proposal: PlanningProposal
) -> tuple[PlanningPreview, _Resolved | None]:
    salt = read_reference_salt(connection)
    errors: tuple[str, ...] = ()
    resolved: _Resolved | None = None
    receipt = connection.execute(
        "SELECT * FROM planning_proposal_receipts WHERE proposal_id=?", (proposal.proposal_id,)
    ).fetchone()
    receipt_after = str(receipt["after_revision"]) if receipt is not None else None
    before = after = None
    target_revision = getattr(getattr(proposal.intent, "target", None), "revision", None)
    effective = getattr(proposal.intent, "effective_date", date.today().isoformat())
    assumptions: tuple[str, ...] = ()
    occurrences: tuple[str, ...] = ()
    impact: dict[str, Any] = {}
    if receipt is not None:
        before = _definition(json.loads(receipt["before_json"])) if receipt["before_json"] else None
        after = _definition(json.loads(receipt["after_json"])) if receipt["after_json"] else None
        errors = ("Proposal was already applied; inspect its receipt or use targeted undo.",)
    else:
        try:
            if not salt:
                raise ValueError(
                    "Reference salt is unavailable; initialize it locally and refresh discovery."
                )
            resolved = _resolve_change(connection, proposal, salt)
            before, after = _definition(resolved.before), resolved.after
            occurrences, assumptions = resolved.next_occurrences, resolved.assumptions
            impact = _impact(resolved)
        except ValueError as exc:
            errors = (str(exc),)
    if proposal.status != "pending":
        errors += ("Only a pending proposal can be applied.",)
    payload = {
        "proposal_id": proposal.proposal_id,
        "proposal_revision": proposal.proposal_revision,
        "before": before,
        "after": after,
        "target_revision": target_revision,
        "effective_date": effective,
        "next_occurrences": occurrences,
        "assumptions": assumptions,
        "dependencies": resolved.dependencies if resolved else (),
        "validation_errors": errors,
        "impact": impact,
    }
    digest = content_revision(salt, "preview", payload) if salt else ""
    actions = (
        ("apply_exact_reviewed_revisions", "reject_exact_proposal_revision")
        if not errors
        else (
            ("undo_exact_receipt_after_revision",)
            if receipt is not None and not receipt["undone_at"]
            else ("refresh_discovery_and_file_new_proposal",)
        )
    )
    return PlanningPreview(
        proposal.proposal_id,
        proposal.proposal_revision,
        digest,
        proposal.status,
        proposal.intent.operation,
        before,
        after,
        target_revision,
        effective,
        occurrences,
        assumptions,
        errors,
        not errors,
        receipt_after,
        actions,
        impact,
    ), resolved


def preview_planning_proposal(
    service: PlanningProposalMixin, proposal_id: str, *, ledger_db: Path
) -> PlanningPreview:
    with proposal_session(service.proposals_db, create=False, writable=False) as store:
        proposal = _proposal(
            store.execute(
                "SELECT * FROM planning_proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
        )
        claim_table = store.execute(
            "SELECT 1 FROM sqlite_master WHERE name='planning_proposal_applications'"
        ).fetchone()
        claim = (
            store.execute(
                "SELECT preview_revision FROM planning_proposal_applications WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
            if claim_table
            else None
        )
    with bound_database_read_connection(
        Path(ledger_db),
        label="Planning proposal preview",
        require_main_owner_only=False,
        require_parent_owner_only=False,
    ) as connection:
        connection.row_factory = sqlite3.Row
        preview, _resolved = _build_preview(connection, proposal)
        if claim is not None:
            preview = replace(
                preview,
                recovery_required=True,
                application_preview_revision=str(claim[0]),
                actions=("recover_exact_reviewed_application",),
            )
        return preview


def _result(
    status: str, started_at: Any, *, errors: list[str] | None = None, **details: Any
) -> RunResult:
    return RunResult(
        status=status,
        started_at=started_at,
        finished_at=naive_utc_now(),
        errors=errors or [],
        details=details,
    )


def _write_definition(connection: sqlite3.Connection, resolved: _Resolved) -> None:
    now = naive_utc_now()
    if resolved.kind == "schedule":
        assert resolved.effective_date is not None
        expected = (
            ScheduleService._require_current_schedule(connection, resolved.target_id, None)
            if resolved.before
            else None
        )
        ScheduleService.write_on_connection(
            connection,
            resolved.target_id,
            _draft(resolved.after) if resolved.after else None,
            effective_date=resolved.effective_date,
            expected_record=expected,
            now=now,
        )
        return
    table, key = _TARGETS[resolved.kind]
    if resolved.after is None:
        connection.execute(f"DELETE FROM {table} WHERE {key}=?", (resolved.target_id,))
        return
    values = dict(resolved.after)
    values["updated_at"] = now.isoformat(sep=" ")
    if resolved.before is None:
        values.update(id=int(resolved.target_id), created_at=now.isoformat(sep=" "))
        columns = tuple(values)
        connection.execute(
            f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            tuple(values[column] for column in columns),
        )
    else:
        columns = tuple(values)
        connection.execute(
            f"UPDATE {table} SET {','.join(column + '=?' for column in columns)} WHERE {key}=?",
            (*tuple(values[column] for column in columns), resolved.target_id),
        )


def _ledger_target_identity(connection: sqlite3.Connection, ledger_db: Path) -> str:
    """Identify one financial lineage at a stable publication pathname.

    Imports may replace the inode. The canonical path and retained reference
    salt distinguish both another pathname holding a clone and an unrelated
    database later occupying the original pathname.
    """
    salt = read_reference_salt(connection)
    if not salt:
        raise ValueError("Financial target has no reference identity; use the original ledger.")
    return content_revision(salt, "planning-application-target", {"path": str(ledger_db.resolve())})


def _remember_ledger_target(
    store: sqlite3.Connection, proposal_id: str, ledger_target: str
) -> None:
    """Retain the origin in the same transaction as the completed queue tail.

    Until that transaction commits, the separately committed application
    reservation protects recovery. This binding survives reservation removal
    and undo without presenting completed work as an in-progress application.
    """
    existing = store.execute(
        "SELECT ledger_target FROM planning_proposal_targets WHERE proposal_id=?", (proposal_id,)
    ).fetchone()
    if existing is not None:
        if existing["ledger_target"] != ledger_target:
            raise ValueError("The original financial target binding cannot be changed.")
        return
    store.execute(
        "INSERT INTO planning_proposal_targets (proposal_id,ledger_target,bound_at) VALUES (?,?,?)",
        (proposal_id, ledger_target, naive_utc_now().isoformat()),
    )


def recover_planning_storage(service: PlanningProposalMixin, *, ledger_db: Path) -> RunResult:
    """Reopen local review after a crash, preserving intent, reservations and receipts."""
    started = naive_utc_now()
    ledger_db = Path(ledger_db).expanduser().absolute()
    if ledger_db.resolve() == service.proposals_db.resolve():
        raise ValueError("The proposal store and financial database must be different files.")
    proposal_recovered = recover_proposal_database(service.proposals_db)
    with live_mutation_boundary(ledger_db, operation="planning_storage_recovery"):
        ledger_recovered = recover_database_journal(ledger_db, label="Planning storage recovery")
    if ledger_recovered:
        invalidate_db_caches()
    return _result(
        "success",
        started,
        proposal_store_recovered=proposal_recovered,
        financial_database_recovered=ledger_recovered,
    )


def apply_planning_proposal(
    service: PlanningProposalMixin,
    proposal_id: str,
    *,
    ledger_db: Path,
    expected_proposal_revision: str,
    expected_preview_revision: str,
) -> RunResult:
    started = naive_utc_now()
    ledger_db = Path(ledger_db).expanduser().absolute()
    if ledger_db.resolve() == service.proposals_db.resolve():
        raise ValueError("The proposal store and financial database must be different files.")
    # This reservation is durable before opening the financial writer. If the
    # process stops after that commit, an isolated rejection cannot mislabel an
    # already-effective financial change. Exact retry reconciles its receipt.
    with proposal_session(service.proposals_db, create=False, writable=True) as claim_store:
        claimed = _proposal(
            claim_store.execute(
                "SELECT * FROM planning_proposals WHERE proposal_id=?", (proposal_id,)
            ).fetchone()
        )
        if claimed.proposal_revision != expected_proposal_revision or not expected_preview_revision:
            return _result(
                "conflict",
                started,
                errors=["Reviewed proposal revision does not match; refresh preview."],
            )
        existing_claim = claim_store.execute(
            "SELECT * FROM planning_proposal_applications WHERE proposal_id=?", (proposal_id,)
        ).fetchone()
        binding = claim_store.execute(
            "SELECT ledger_target FROM planning_proposal_targets WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
        if binding is None and existing_claim is None and claimed.status in {"applied", "undone"}:
            # A pre-upgrade receipt can also exist in a copied database. Do not
            # invent its origin from the first target supplied after upgrade.
            return _result(
                "conflict",
                started,
                errors=[
                    "Legacy completed proposal has no original financial target binding; automatic recovery cannot verify its origin."
                ],
            )
        if existing_claim is not None and (
            existing_claim["proposal_revision"] != expected_proposal_revision
            or existing_claim["preview_revision"] != expected_preview_revision
        ):
            return _result(
                "conflict",
                started,
                errors=[
                    "An application needs recovery with its original exact reviewed revisions."
                ],
            )
        with live_mutation_boundary(ledger_db, operation="planning_proposal_apply"):
            recover_database_journal(ledger_db, label="Planning application recovery")
        with bound_database_read_connection(
            ledger_db,
            label="Planning application target identity",
            require_main_owner_only=False,
            require_parent_owner_only=False,
        ) as target_reader:
            ledger_target = _ledger_target_identity(target_reader, ledger_db)
            if binding is not None and binding["ledger_target"] != ledger_target:
                return _result(
                    "conflict",
                    started,
                    errors=[
                        "Proposal belongs to another ledger target; use the original financial database."
                    ],
                )
            if existing_claim is not None:
                if existing_claim["ledger_target"] is None:
                    # An exact receipt in a clone is not proof of origin. Only
                    # an independently retained binding may repair an old
                    # reservation that predates the target field.
                    if binding is None:
                        return _result(
                            "conflict",
                            started,
                            errors=[
                                "Legacy application has no original financial target binding; automatic recovery cannot verify its origin."
                            ],
                        )
                    claim_store.execute(
                        "UPDATE planning_proposal_applications SET ledger_target=? WHERE proposal_id=?",
                        (ledger_target, proposal_id),
                    )
                elif existing_claim["ledger_target"] != ledger_target:
                    return _result(
                        "conflict",
                        started,
                        errors=[
                            "Application belongs to another ledger target; retry with the original financial database."
                        ],
                    )
        if claimed.status == "pending" and existing_claim is None:
            claim_store.execute(
                "INSERT INTO planning_proposal_applications (proposal_id,proposal_revision,preview_revision,ledger_target,started_at) VALUES (?,?,?,?,?)",
                (
                    proposal_id,
                    expected_proposal_revision,
                    expected_preview_revision,
                    ledger_target,
                    naive_utc_now().isoformat(),
                ),
            )
    committed = False
    result: RunResult | None = None
    try:
        # Keep rejection and application mutually exclusive in the proposal
        # store, but commit financial data and its receipt before the store tail.
        with (
            database_write_outcome() as outcome,
            proposal_session(service.proposals_db, create=False, writable=True) as store,
        ):
            proposal = _proposal(
                store.execute(
                    "SELECT * FROM planning_proposals WHERE proposal_id=?", (proposal_id,)
                ).fetchone()
            )
            if (
                proposal.proposal_revision != expected_proposal_revision
                or not expected_preview_revision
            ):
                return _result(
                    "conflict",
                    started,
                    errors=["Reviewed proposal revision does not match; refresh preview."],
                )
            with live_mutation_boundary(ledger_db, operation="planning_proposal_apply"):
                with audited_database_write_connection(
                    ledger_db, label="Planning proposal apply target"
                ) as connection:
                    connection.row_factory = sqlite3.Row
                    if _ledger_target_identity(connection, ledger_db) != ledger_target:
                        return _result(
                            "conflict",
                            started,
                            errors=[
                                "Financial target changed after application reservation; use the original ledger."
                            ],
                        )
                    receipt = connection.execute(
                        "SELECT * FROM planning_proposal_receipts WHERE proposal_id=?",
                        (proposal_id,),
                    ).fetchone()
                    if receipt is not None:
                        if (
                            receipt["proposal_revision"] != expected_proposal_revision
                            or receipt["preview_revision"] != expected_preview_revision
                        ):
                            return _result(
                                "conflict",
                                started,
                                errors=[
                                    "Reviewed revisions do not match the durable application receipt."
                                ],
                            )
                        status = "undone" if receipt["undone_at"] else "applied"
                        result = _result(
                            "success",
                            started,
                            proposal_id=proposal_id,
                            after_revision=receipt["after_revision"],
                            expected_after_revision=receipt["after_revision"],
                            recovered_from_receipt=True,
                            already_undone=bool(receipt["undone_at"]),
                            applied_state=status,
                        )
                    else:
                        if proposal.status != "pending":
                            store.execute(
                                "DELETE FROM planning_proposal_applications WHERE proposal_id=?",
                                (proposal_id,),
                            )
                            return _result(
                                "conflict",
                                started,
                                errors=[
                                    "Proposal status has no receipt in this financial database; inspect the original target."
                                ],
                            )
                        preview, resolved = _build_preview(connection, proposal)
                        if not preview.can_apply or resolved is None:
                            store.execute(
                                "DELETE FROM planning_proposal_applications WHERE proposal_id=?",
                                (proposal_id,),
                            )
                            return _result(
                                "validation_failed", started, errors=list(preview.validation_errors)
                            )
                        if preview.preview_revision != expected_preview_revision:
                            store.execute(
                                "DELETE FROM planning_proposal_applications WHERE proposal_id=?",
                                (proposal_id,),
                            )
                            return _result(
                                "conflict",
                                started,
                                errors=[
                                    "Preview changed; refresh and review the new exact values before applying."
                                ],
                            )
                        salt = read_reference_salt(connection)
                        assert salt is not None
                        _write_definition(connection, resolved)
                        after = _read_target(connection, resolved.kind, resolved.target_id)
                        revision = _state_revision(
                            connection,
                            salt,
                            resolved.kind,
                            resolved.target_id,
                            footprint=(resolved.before, after),
                        )
                        connection.execute(
                            "INSERT INTO planning_proposal_receipts (proposal_id,proposal_revision,preview_revision,target_kind,target_id,before_json,after_json,after_revision,effective_date,applied_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (
                                proposal_id,
                                proposal.proposal_revision,
                                preview.preview_revision,
                                resolved.kind,
                                resolved.target_id,
                                canonical_json(resolved.before)
                                if resolved.before is not None
                                else None,
                                canonical_json(after) if after is not None else None,
                                revision,
                                resolved.effective_date.isoformat()
                                if resolved.effective_date
                                else None,
                                naive_utc_now().isoformat(sep=" "),
                            ),
                        )
                        result = _result(
                            "success",
                            started,
                            proposal_id=proposal_id,
                            proposal_revision=proposal.proposal_revision,
                            preview_revision=preview.preview_revision,
                            after_revision=revision,
                            expected_after_revision=revision,
                            recovered_from_receipt=False,
                            target_kind=resolved.kind,
                            applied_state="applied",
                        )
                        record_db_operation(
                            ledger_db,
                            operation="planning_proposal_apply",
                            result=result,
                            connection=connection,
                        )
                        status = "applied"
                committed = True
                outcome.mark_committed()
                invalidate_db_caches()
            _remember_ledger_target(store, proposal_id, ledger_target)
            service._mark_planning_status(store, proposal_id, status)
    except DatabaseWriteCommittedError:
        return _result(
            "committed_unconfirmed",
            started,
            errors=[
                "Financial commit succeeded or may have succeeded; retry the exact reviewed revisions to recover the proposal status from its receipt."
            ],
            committed=True,
            proposal_id=proposal_id,
        )
    except Exception:
        if committed:
            return _result(
                "committed_unconfirmed",
                started,
                errors=[
                    "Financial change is durable; retry the exact reviewed revisions to finish the proposal-store status."
                ],
                committed=True,
                proposal_id=proposal_id,
            )
        # A second process may have begun recovery after this failed writer
        # released its queue lock. Recheck the receipt under the queue lock
        # before clearing the reservation; never clear another successful tail.
        try:
            with proposal_session(
                service.proposals_db, create=False, writable=True
            ) as release_store:
                with bound_database_read_connection(
                    ledger_db,
                    label="Planning application recovery check",
                    require_main_owner_only=False,
                    require_parent_owner_only=False,
                ) as reader:
                    recovery_target = _ledger_target_identity(reader, ledger_db)
                    exists = reader.execute(
                        "SELECT 1 FROM planning_proposal_receipts WHERE proposal_id=?",
                        (proposal_id,),
                    ).fetchone()
                if exists is None and recovery_target == ledger_target:
                    release_store.execute(
                        "DELETE FROM planning_proposal_applications WHERE proposal_id=? AND proposal_revision=? AND preview_revision=? AND ledger_target=?",
                        (
                            proposal_id,
                            expected_proposal_revision,
                            expected_preview_revision,
                            ledger_target,
                        ),
                    )
        except OSError, sqlite3.Error, ValueError:
            # Keep the durable reservation when receipt state cannot be read.
            pass
        raise
    assert result is not None
    return result


def _restore_target(connection: sqlite3.Connection, receipt: sqlite3.Row) -> None:
    kind, identifier = str(receipt["target_kind"]), str(receipt["target_id"])
    before = json.loads(receipt["before_json"]) if receipt["before_json"] else None
    now = naive_utc_now()
    if kind == "schedule":
        effective = max(
            date.fromisoformat(receipt["effective_date"]), date.today() + timedelta(days=1)
        )
        current = _read_target(connection, kind, identifier)
        expected = (
            ScheduleService._require_current_schedule(connection, identifier, None)
            if current
            else None
        )
        ScheduleService.write_on_connection(
            connection,
            identifier,
            _draft(before) if before else None,
            effective_date=effective,
            expected_record=expected,
            now=now,
            allow_restore=True,
        )
        return
    table, key = _TARGETS[kind]
    if before is None:
        connection.execute(f"DELETE FROM {table} WHERE {key}=?", (identifier,))
        return
    if kind == "budget":
        if (
            connection.execute(
                "SELECT 1 FROM category_config WHERE name=?", (before["category"],)
            ).fetchone()
            is None
        ):
            raise ValueError("Undo cannot restore a budget whose category no longer exists.")
        duplicate = connection.execute(
            "SELECT 1 FROM budgets WHERE budget_month=? AND category=? AND id!=?",
            (before["budget_month"], before["category"], identifier),
        ).fetchone()
        if duplicate:
            raise ValueError("Undo conflicts with a newer budget for this category and month.")
    if kind == "preference":
        normalized = normalize_preference_name(before["scope"], before["name"]).casefold()
        rows = connection.execute(
            "SELECT id,name FROM spending_preferences WHERE scope=? AND id!=?",
            (before["scope"], identifier),
        )
        if any(
            normalize_preference_name(before["scope"], row["name"]).casefold() == normalized
            for row in rows
        ):
            raise ValueError("Undo conflicts with a newer preference for this subject.")
    before["updated_at"] = now.isoformat(sep=" ")
    current = _read_target(connection, kind, identifier)
    if current is None:
        columns = tuple(before)
        connection.execute(
            f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
            tuple(before[column] for column in columns),
        )
    else:
        values = {column: value for column, value in before.items() if column != key}
        columns = tuple(values)
        connection.execute(
            f"UPDATE {table} SET {','.join(column + '=?' for column in columns)} WHERE {key}=?",
            (*tuple(values[column] for column in columns), identifier),
        )


def undo_planning_proposal(
    service: PlanningProposalMixin,
    proposal_id: str,
    *,
    ledger_db: Path,
    expected_after_revision: str,
) -> RunResult:
    started = naive_utc_now()
    ledger_db = Path(ledger_db).expanduser().absolute()
    result: RunResult | None = None
    committed = False
    try:
        with (
            database_write_outcome() as outcome,
            proposal_session(service.proposals_db, create=False, writable=True) as store,
        ):
            proposal = _proposal(
                store.execute(
                    "SELECT * FROM planning_proposals WHERE proposal_id=?", (proposal_id,)
                ).fetchone()
            )
            claim = store.execute(
                "SELECT ledger_target FROM planning_proposal_applications WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
            binding = store.execute(
                "SELECT ledger_target FROM planning_proposal_targets WHERE proposal_id=?",
                (proposal_id,),
            ).fetchone()
            expected_target = (
                binding["ledger_target"]
                if binding is not None
                else claim["ledger_target"]
                if claim is not None
                else None
            )
            if expected_target is None:
                return _result(
                    "conflict",
                    started,
                    errors=[
                        "Proposal has no original financial target binding; automatic undo cannot verify its origin."
                    ],
                )
            with live_mutation_boundary(ledger_db, operation="planning_proposal_undo"):
                recover_database_journal(ledger_db, label="Planning undo recovery")
            with bound_database_read_connection(
                ledger_db,
                label="Planning undo target identity",
                require_main_owner_only=False,
                require_parent_owner_only=False,
            ) as target_reader:
                ledger_target = _ledger_target_identity(target_reader, ledger_db)
            if expected_target != ledger_target or (
                claim is not None and claim["ledger_target"] not in {None, ledger_target}
            ):
                return _result(
                    "conflict",
                    started,
                    errors=[
                        "Application belongs to another ledger target; use the original financial database for undo."
                    ],
                )
            with live_mutation_boundary(ledger_db, operation="planning_proposal_undo"):
                with audited_database_write_connection(
                    ledger_db, label="Planning proposal undo target"
                ) as connection:
                    connection.row_factory = sqlite3.Row
                    if _ledger_target_identity(connection, ledger_db) != ledger_target:
                        return _result(
                            "conflict",
                            started,
                            errors=[
                                "Financial target changed after reservation validation; use the original ledger."
                            ],
                        )
                    receipt = connection.execute(
                        "SELECT * FROM planning_proposal_receipts WHERE proposal_id=?",
                        (proposal_id,),
                    ).fetchone()
                    if (
                        receipt is None
                        or receipt["proposal_revision"] != proposal.proposal_revision
                    ):
                        return _result(
                            "conflict",
                            started,
                            errors=["No matching application receipt exists in this database."],
                        )
                    if (
                        not expected_after_revision
                        or receipt["after_revision"] != expected_after_revision
                    ):
                        return _result(
                            "conflict",
                            started,
                            errors=["Reviewed undo revision does not match the receipt."],
                        )
                    if receipt["undone_at"]:
                        result = _result(
                            "success",
                            started,
                            proposal_id=proposal_id,
                            already_undone=True,
                            undo_revision=receipt["undo_revision"],
                            applied_state="undone",
                        )
                    else:
                        salt = read_reference_salt(connection)
                        footprint = tuple(
                            json.loads(receipt[key]) if receipt[key] else None
                            for key in ("before_json", "after_json")
                        )
                        if (
                            not salt
                            or _state_revision(
                                connection,
                                salt,
                                receipt["target_kind"],
                                receipt["target_id"],
                                footprint=footprint,
                            )
                            != receipt["after_revision"]
                        ):
                            return _result(
                                "conflict",
                                started,
                                errors=[
                                    "The affected record changed after application; undo would overwrite a later change. Review it locally."
                                ],
                            )
                        try:
                            _restore_target(connection, receipt)
                        except ValueError as exc:
                            return _result("conflict", started, errors=[str(exc)])
                        revision = _state_revision(
                            connection,
                            salt,
                            receipt["target_kind"],
                            receipt["target_id"],
                            footprint=footprint,
                        )
                        connection.execute(
                            "UPDATE planning_proposal_receipts SET undone_at=?,undo_revision=? WHERE proposal_id=?",
                            (naive_utc_now().isoformat(sep=" "), revision, proposal_id),
                        )
                        result = _result(
                            "success",
                            started,
                            proposal_id=proposal_id,
                            undo_revision=revision,
                            already_undone=False,
                            applied_state="undone",
                        )
                        record_db_operation(
                            ledger_db,
                            operation="planning_proposal_undo",
                            result=result,
                            connection=connection,
                        )
                committed = True
                outcome.mark_committed()
                invalidate_db_caches()
            _remember_ledger_target(store, proposal_id, ledger_target)
            service._mark_planning_status(store, proposal_id, "undone")
    except DatabaseWriteCommittedError:
        return _result(
            "committed_unconfirmed",
            started,
            errors=[
                "Undo commit succeeded or may have succeeded; retry the same receipt revision to recover its status."
            ],
            committed=True,
            proposal_id=proposal_id,
        )
    except Exception:
        if committed:
            return _result(
                "committed_unconfirmed",
                started,
                errors=["Undo is durable; retry the same receipt revision to finish its status."],
                committed=True,
                proposal_id=proposal_id,
            )
        raise
    assert result is not None
    return result
