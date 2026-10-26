"""Private planning records cross the agent boundary only through these projections."""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping

from ledger.mcp.planning_contracts import (
    PLANNING_PRIVACY_STATEMENT,
    RECONCILIATION_STATEMENT,
)
from ledger.planning.discovery import content_revision, definition_ref
from ledger.query.agent import AgentQueryError

_DEFINITION_FIELDS = {
    "account": ("is_credit_card",),
    "budget": ("budget_month", "amount", "currency", "category_ref"),
    "category": ("exclude_from_spending", "is_parent"),
    "merchant": (),
    "preference": ("scope", "disposition", "subject_ref"),
    "schedule": (
        "amount",
        "currency",
        "event_type",
        "next_date",
        "frequency",
        "provenance",
        "is_active",
        "account_ref",
        "counterparty_account_ref",
    ),
}


def disclosures(semantics: str, *, withheld: bool = False) -> dict[str, object]:
    return {
        "account_ref_semantics": semantics,
        "account_refs": "withheld" if withheld else "disclosed",
        "account_refs_withheld_reason": "account_ref_salt_absent" if withheld else None,
    }


def unavailable(semantics: str) -> dict[str, object]:
    return {
        **disclosures(semantics, withheld=True),
        "status": "unavailable",
        "unavailable_reason": "account_ref_salt_absent",
    }


def definitions(
    raw: Mapping[str, object],
    *,
    semantics: str,
    kind: str,
    filtered: bool,
    offset: int,
) -> dict[str, object]:
    rows = raw.get("entries")
    if not isinstance(rows, list) or len(rows) > 100:
        raise AgentQueryError("corrupt_data")
    entries: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, Mapping) or row.get("kind") not in _DEFINITION_FIELDS:
            raise AgentQueryError("corrupt_data")
        fields = ("kind", "ref", "revision", *_DEFINITION_FIELDS[str(row["kind"])])
        if not set(fields) <= row.keys():
            raise AgentQueryError("corrupt_data")
        entries.append({key: row[key] for key in fields})
    has_more = raw.get("has_more")
    if not isinstance(has_more, bool) or raw.get("matched_count") != len(entries):
        raise AgentQueryError("corrupt_data")
    selection = "all"
    if filtered:
        selection = "unmatched" if not entries else "matched"
        if entries and (has_more or offset > 0 or len(entries) > 1):
            selection = "ambiguous"
    return {
        **disclosures(semantics),
        "status": "ready",
        "kind": kind,
        "selection_status": selection,
        "matched_count": len(entries),
        "privacy_statement": PLANNING_PRIVACY_STATEMENT,
        "entries": entries,
    }


def _account_ref(refs: Mapping[str, str], value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AgentQueryError("corrupt_data")
    return refs.get(value)


def _opaque_occurrence(salt: str, value: object) -> str:
    if not isinstance(value, str) or not value:
        raise AgentQueryError("corrupt_data")
    return (
        "occurrence-"
        + hmac.new(
            salt.encode("utf-8"),
            ("plan-occurrence-v1\0" + value).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:40]
    )


def _coverage(raw: object, refs: Mapping[str, str]) -> dict[str, object]:
    if not isinstance(raw, Mapping) or not isinstance(raw.get("accounts"), list):
        raise AgentQueryError("corrupt_data")
    accounts: list[dict[str, object]] = []
    for account in raw["accounts"]:
        if not isinstance(account, Mapping) or not isinstance(account.get("intervals"), list):
            raise AgentQueryError("corrupt_data")
        intervals: list[dict[str, object]] = []
        for interval in account["intervals"]:
            if not isinstance(interval, Mapping):
                raise AgentQueryError("corrupt_data")
            intervals.append({key: interval.get(key) for key in ("start_date", "end_date")})
        accounts.append(
            {
                "account_ref": _account_ref(refs, account.get("account_id")),
                "complete": account.get("complete"),
                "intervals": intervals,
            }
        )
    return {
        "complete": raw.get("complete"),
        "window_start": raw.get("window_start"),
        "window_end": raw.get("window_end"),
        "accounts": accounts,
    }


def reconciliation(
    raw: Mapping[str, object],
    *,
    semantics: str,
    salt: str,
    refs: Mapping[str, str],
) -> dict[str, object]:
    rows = raw.get("findings")
    period = raw.get("period")
    counts = raw.get("disclosures")
    if (
        not isinstance(rows, list)
        or len(rows) > 100
        or not isinstance(period, Mapping)
        or not isinstance(counts, Mapping)
    ):
        raise AgentQueryError("corrupt_data")
    findings: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, Mapping) or not isinstance(row.get("schedule_id"), str):
            raise AgentQueryError("corrupt_data")
        evidence = row.get("evidence_ids")
        if not isinstance(evidence, list) or any(
            not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{40}", item)
            for item in evidence
        ):
            raise AgentQueryError("corrupt_data")
        evidence_count = row.get("evidence_count")
        if (
            len(evidence) > 20
            or type(evidence_count) is not int
            or evidence_count < len(evidence)
            or row.get("evidence_truncated") is not (evidence_count > len(evidence))
        ):
            raise AgentQueryError("corrupt_data")
        finding = {
            key: row.get(key)
            for key in (
                "expected_date",
                "expected_amount",
                "event_type",
                "currency",
                "status",
                "actual_date",
                "actual_amount",
                "amount_difference",
                "date_difference",
                "amount_changed",
                "date_changed",
                "evidence_ids",
                "matching_basis",
                "reviewed",
                "actionable",
                "history_complete",
                "candidate_count",
            )
        }
        finding.update(
            occurrence_ref=_opaque_occurrence(salt, row.get("occurrence_id")),
            schedule_ref=definition_ref(salt, "schedule", row["schedule_id"]),
            schedule_revision=content_revision(
                salt, "schedule-expectation", row.get("schedule_revision")
            ),
            material_revision=content_revision(
                salt, "reconciliation-evidence", row.get("material_revision")
            ),
            account_ref=_account_ref(refs, row.get("account_id")),
            counterparty_account_ref=_account_ref(refs, row.get("counterparty_account_id")),
            coverage=_coverage(row.get("coverage"), refs),
            evidence_ids=evidence,
            evidence_count=evidence_count,
            evidence_truncated=row["evidence_truncated"],
        )
        findings.append(finding)
    return {
        **disclosures(semantics),
        "status": "ready",
        "period": {"start_date": period.get("start_date"), "end_date": period.get("end_date")},
        **{
            key: raw.get(key)
            for key in (
                "as_of",
                "currency",
                "posting_window_days",
                "total_count",
                "returned_count",
                "assumptions",
            )
        },
        **{
            key: counts.get(key)
            for key in (
                "hypothetical_occurrence_count",
                "detected_pattern_count",
                "incomplete_history_schedule_count",
            )
        },
        "findings": findings,
        "reconciliation_statement": RECONCILIATION_STATEMENT,
    }
