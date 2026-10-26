"""Read-only planning discovery and salted, content-bound private target lookup.

Agent readers pass their existing pinned SQLite connection. Only the explicitly
local convenience functions open a database, always through the bound reader.
"""

from __future__ import annotations

import hmac
import json
import sqlite3
import unicodedata
from collections.abc import Iterator, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

from ledger.planning.schedules import ScheduleService
from ledger.query.account_refs import banking_account_ref
from ledger.storage.operations import ACCOUNT_REF_SALT_KEY
from ledger.storage.snapshots import bound_database_read_connection

KINDS = ("account", "budget", "category", "merchant", "preference", "schedule")
# Shared by cursor validation and the total source walk, including kind="all".
MAX_DISCOVERY_SOURCE_ROWS = 20000


class PlanningDiscoveryLimitError(ValueError):
    """A bounded discovery source cannot be completely inspected."""

    code = "filter_too_broad"


_TABLES = {
    "account": ("accounts", "account_id"),
    "budget": ("budgets", "id"),
    "category": ("category_config", "name"),
    "merchant": ("merchant_rules", "merchant_norm"),
    "preference": ("spending_preferences", "id"),
    "schedule": ("planned_schedules", "schedule_id"),
}
_LABELS = {
    "budget": "category",
    "category": "name",
    "merchant": "merchant_norm",
    "preference": "name",
    "schedule": "name",
}


def read_reference_salt(connection: sqlite3.Connection) -> str | None:
    row = connection.execute(
        "SELECT value FROM config_metadata WHERE key = ?", (ACCOUNT_REF_SALT_KEY,)
    ).fetchone()
    return str(row[0]) if row is not None and row[0] else None


def _normalized(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def _merchant_identity(value: object) -> str:
    """Preserve the full canonical identity independently of preference write limits."""
    normalized = " ".join(unicodedata.normalize("NFKC", str(value)).split()).upper()
    if not normalized:
        raise ValueError("Merchant identity is required.")
    return normalized


def definition_ref(salt: str, kind: str, identifier: str, created_at: str | None = None) -> str:
    if not salt or kind not in KINDS or kind == "account":
        raise ValueError("A valid reference salt and planning domain are required.")
    identity = [kind, str(identifier)]
    if kind in {"budget", "preference"}:
        identity.append(str(created_at or ""))
    message = json.dumps(identity, separators=(",", ":"), ensure_ascii=True)
    return f"{kind}-" + hmac.new(salt.encode(), message.encode(), "sha256").hexdigest()[:40]


def content_revision(salt: str, kind: str, value: object) -> str:
    if not salt:
        raise ValueError(
            "Reference salt is unavailable; initialize it locally and refresh discovery."
        )
    message = json.dumps(
        ["planning-content-v1", kind, value],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hmac.new(salt.encode(), message.encode(), "sha256").hexdigest()


def record_snapshot(
    kind: str, row: Mapping[str, Any] | sqlite3.Row, *, connection: sqlite3.Connection | None = None
) -> dict[str, Any]:
    """Private, canonical record representation, for local preview and receipts."""
    if kind == "schedule":
        record = ScheduleService._from_sql_row(row).to_dict()
        if connection is not None:
            latest = connection.execute(
                "SELECT revision_id,effective_date FROM planned_schedule_revisions WHERE schedule_id=? ORDER BY revision_id DESC LIMIT 1",
                (record["schedule_id"],),
            ).fetchone()
            record["_history_revision"] = dict(latest) if latest is not None else None
        return record
    record = dict(row)
    if kind == "merchant":
        return {"merchant_norm": _merchant_identity(record["merchant_norm"])}
    if kind == "account":
        record = {
            key: record[key]
            for key in (
                "account_id",
                "account_number",
                "institution",
                "is_credit_card",
                "created_at",
            )
        }
    if "amount" in record:
        record["amount"] = f"{Decimal(str(record['amount'])):.2f}"
    return record


def _rows(connection: sqlite3.Connection, kind: str) -> Iterator[sqlite3.Row]:
    table, key = _TABLES[kind]
    if kind == "merchant":
        cursor = connection.execute(
            "SELECT merchant_norm FROM ("
            "SELECT merchant_norm FROM merchant_rules UNION "
            "SELECT merchant AS merchant_norm FROM category_merchants UNION "
            "SELECT DISTINCT merchant AS merchant_norm FROM transactions"
            ") WHERE merchant_norm IS NOT NULL AND trim(merchant_norm) != '' "
            "ORDER BY merchant_norm LIMIT ?",
            (MAX_DISCOVERY_SOURCE_ROWS + 1,),
        )
    else:
        cursor = connection.execute(
            f"SELECT * FROM {table} ORDER BY {key} LIMIT ?", (MAX_DISCOVERY_SOURCE_ROWS + 1,)
        )
    merchant_names: set[str] = set()
    for index, row in enumerate(cursor):
        if index >= MAX_DISCOVERY_SOURCE_ROWS:
            raise PlanningDiscoveryLimitError(
                "Planning discovery source is too broad; use the local editor to narrow or archive definitions."
            )
        if kind == "merchant":
            name = _merchant_identity(row["merchant_norm"])
            if name in merchant_names:
                continue
            merchant_names.add(name)
        yield row


def reference_for_record(salt: str, kind: str, record: Mapping[str, Any]) -> str:
    if kind == "account":
        return banking_account_ref(salt, str(record["institution"]), str(record["account_number"]))
    _table, key = _TABLES[kind]
    identifier = str(record[key])
    return definition_ref(salt, kind, identifier, str(record.get("created_at", "")))


def resolve_reference(
    connection: sqlite3.Connection, *, salt: str, kind: str, ref: str, revision: str | None = None
) -> dict[str, Any]:
    """Resolve exactly one issued reference, never guessing among collisions."""
    matches = []
    for row in _rows(connection, kind):
        record = record_snapshot(kind, row, connection=connection)
        if hmac.compare_digest(reference_for_record(salt, kind, record), ref):
            matches.append(record)
            if len(matches) > 1:
                break
    if len(matches) != 1:
        raise ValueError(
            "Planning reference is missing or ambiguous; refresh discovery and file a new proposal."
        )
    record = matches[0]
    if revision is not None and not hmac.compare_digest(
        content_revision(salt, kind, record), revision
    ):
        raise ValueError("Planning target changed; refresh discovery and file a new proposal.")
    return record


def _entry(
    connection: sqlite3.Connection, salt: str, kind: str, record: dict[str, Any]
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "kind": kind,
        "ref": reference_for_record(salt, kind, record),
        "revision": content_revision(salt, kind, record),
    }
    if kind == "budget":
        entry.update(
            budget_month=record["budget_month"],
            amount=record["amount"],
            currency="CAD",
            category_ref=definition_ref(salt, "category", record["category"]),
        )
    elif kind == "schedule":
        entry.update(
            {
                key: record[key]
                for key in (
                    "amount",
                    "currency",
                    "event_type",
                    "next_date",
                    "frequency",
                    "provenance",
                    "is_active",
                )
            }
        )
        for field in ("account_id", "counterparty_account_id"):
            account = (
                connection.execute(
                    "SELECT * FROM accounts WHERE account_id = ?", (record[field],)
                ).fetchone()
                if record[field]
                else None
            )
            entry[field.replace("_id", "_ref")] = (
                reference_for_record(salt, "account", dict(account))
                if account is not None
                else None
            )
    elif kind == "preference":
        entry.update(
            scope=record["scope"],
            disposition=record["disposition"],
            subject_ref=definition_ref(salt, record["scope"], record["name"]),
        )
    elif kind == "category":
        entry["exclude_from_spending"] = bool(record["exclude_from_spending"])
        entry["is_parent"] = (
            connection.execute(
                "SELECT 1 FROM category_config WHERE parent = ? LIMIT 1", (record["name"],)
            ).fetchone()
            is not None
        )
    elif kind == "account":
        entry["is_credit_card"] = bool(record["is_credit_card"])
    return entry


def discover_planning_definitions(
    connection: sqlite3.Connection,
    *,
    salt: str | None,
    kind: str = "all",
    exact_name: str | None = None,
    account_ref: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    """Return a bounded page without private labels, notes, or raw identities.

    ``matched_count`` counts the returned page, not the whole database. ``has_more``
    discloses further matches. Exact caller text is a filter only and is not echoed.
    """
    if (
        kind not in (*KINDS, "all")
        or type(limit) is not int
        or not 1 <= limit <= 200
        or type(offset) is not int
        or not 0 <= offset <= MAX_DISCOVERY_SOURCE_ROWS
    ):
        raise ValueError("Planning discovery bounds or kind are invalid.")
    if exact_name is not None and (
        not isinstance(exact_name, str)
        or not _normalized(exact_name)
        or len(exact_name) > 200
        or "\x00" in exact_name
    ):
        raise ValueError("Exact planning name filter is invalid.")
    if account_ref is not None and kind not in {"schedule", "all"}:
        raise ValueError("Account reference filters apply to schedules.")
    if kind == "account" and exact_name is not None:
        raise ValueError("Account discovery does not accept private identity text.")
    result: dict[str, Any] = {
        "entries": [],
        "limit": limit,
        "offset": offset,
        "has_more": False,
        "matched_count": 0,
        "status": "matched",
    }
    if not salt:
        result["status"] = "account_ref_salt_absent"
        return result
    if not isinstance(salt, str) or not 1 <= len(salt) <= 256 or "\x00" in salt:
        raise ValueError("Stored planning reference salt is invalid.")
    entries: list[dict[str, Any]] = []
    skipped = 0
    source_rows = 0
    domains = KINDS if kind == "all" else (kind,)
    for domain in domains:
        if (exact_name is not None and domain == "account") or (
            account_ref is not None and domain != "schedule"
        ):
            continue
        for row in _rows(connection, domain):
            source_rows += 1
            if source_rows > MAX_DISCOVERY_SOURCE_ROWS:
                raise PlanningDiscoveryLimitError(
                    "Planning discovery source is too broad; select one planning domain."
                )
            record = record_snapshot(domain, row, connection=connection)
            if exact_name is not None and _normalized(str(record[_LABELS[domain]])) != _normalized(
                exact_name
            ):
                continue
            entry = _entry(connection, salt, domain, record)
            if account_ref is not None and account_ref not in {
                entry.get("account_ref"),
                entry.get("counterparty_account_ref"),
            }:
                continue
            if skipped < offset:
                skipped += 1
                continue
            entries.append(entry)
            if len(entries) > limit:
                break
        if len(entries) > limit:
            break
    result.update(
        entries=entries[:limit],
        has_more=len(entries) > limit,
        matched_count=min(len(entries), limit),
        status="matched" if entries else "no_match",
    )
    return result


def discover_local_planning_definitions(db_path: Path, **filters: Any) -> dict[str, Any]:
    with bound_database_read_connection(
        Path(db_path),
        label="Planning discovery",
        require_main_owner_only=False,
        require_parent_owner_only=False,
    ) as connection:
        connection.row_factory = sqlite3.Row
        return discover_planning_definitions(
            connection, salt=read_reference_salt(connection), **filters
        )


def local_reference(db_path: Path, kind: str, record_id: str | int) -> dict[str, str]:
    if kind not in KINDS:
        raise ValueError("Planning domain is invalid.")
    table, key = _TABLES[kind]
    with bound_database_read_connection(
        Path(db_path),
        label="Local planning reference",
        require_main_owner_only=False,
        require_parent_owner_only=False,
    ) as connection:
        connection.row_factory = sqlite3.Row
        salt = read_reference_salt(connection)
        if not salt:
            raise ValueError("Reference salt is unavailable; initialize it locally first.")
        if kind == "merchant":
            name = _merchant_identity(record_id)
            rows = [
                row
                for row in _rows(connection, kind)
                if _merchant_identity(row["merchant_norm"]) == name
            ]
        else:
            rows = connection.execute(
                f"SELECT * FROM {table} WHERE {key} = ?", (record_id,)
            ).fetchmany(2)
        if len(rows) != 1:
            raise ValueError("Planning record is missing or ambiguous.")
        record = record_snapshot(kind, rows[0], connection=connection)
        return {
            "ref": reference_for_record(salt, kind, record),
            "revision": content_revision(salt, kind, record),
        }
