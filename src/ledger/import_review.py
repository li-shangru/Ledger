"""Review-queue persistence and apply-decisions against a (possibly live) ledger DB.

Manages the ``import_review`` table: seeding review candidates from an import run,
reading/saving user decisions (resolved, reviewed_as_is, pending), and applying those
decisions by writing merchant/category back to the main ``transactions`` table.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from ledger.config.repository import SQLiteConfigRepository
from ledger.config.schemas import MerchantRule, MerchantRulesConfig, merchant_match_texts
from ledger.core.results import RunResult
from ledger.storage.migrations import ensure_schema
from ledger.storage.operations import record_db_operation
from ledger.storage.operations import utc_now_dt as utc_now
from ledger.storage.snapshots import bound_database_read_connection
from ledger.utils.json_io import decimal_json
from ledger.utils.json_io import utc_now as _utc_now_iso

logger = logging.getLogger(__name__)

REVIEW_STATUSES = ("pending", "resolved", "reviewed_as_is")
REVIEW_MUTATION_FIELDS = ("status", "merchant", "category", "note", "scope")
REVIEW_EVIDENCE_FIELDS = ("txn_ids", "examples", "transaction_count", "total_abs_amount")
REVIEW_EXPECTED_FIELDS = (*REVIEW_MUTATION_FIELDS, *REVIEW_EVIDENCE_FIELDS, "updated_at")


class ReviewConflictError(RuntimeError):
    """Raised when a rendered review baseline no longer matches the stored row."""


# A row still needs a human decision when it is untouched, or when it was marked
# resolved without actually supplying both fields (which would leave the transaction
# unmapped). Held as one constant so the counters below and the CLI's per-row
# ``needs_decision`` flag can never drift apart.
_NEEDS_DECISION_SQL = """
select count(*)
from import_review
where status = 'pending'
   or (status = 'resolved' and (merchant = '' or category = ''))
"""


def review_needs_decision(row: dict[str, Any]) -> bool:
    """Whether one review row (as returned by ``get_review_rows``) is still open.

    The Python mirror of ``_NEEDS_DECISION_SQL`` so a per-row flag agrees with the
    aggregate counts that gate finalize.
    """
    status = row.get("status")
    if status == "pending":
        return True
    return status == "resolved" and not (row.get("merchant") and row.get("category"))


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # Wait briefly for a competing writer instead of failing immediately with
    # "database is locked" (the SQLAlchemy engine sets this; the raw writers here did not).
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def _connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    conn = _connect(db_path)
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def _now() -> str:
    # Canonical UTC ISO-Z timestamp helper (shared with the PDF import JSON writers).
    return _utc_now_iso()


def _json_default(value: Any) -> Any:
    return decimal_json(value)


def ensure_review_schema(db_path: Path) -> None:
    # The import_review table is owned by the ImportReviewDB ORM model;
    # ensure_schema creates it (memoized) so the schema has a single source.
    ensure_schema(db_path)


def seed_review_candidates(db_path: Path, candidates: list[dict[str, Any]]) -> None:
    ensure_review_schema(db_path)
    now = _now()
    with _connection(db_path) as conn:
        for candidate in candidates:
            existing = conn.execute(
                "select status, merchant, category, note, scope from import_review where review_key = ?",
                (candidate["key"],),
            ).fetchone()
            if existing:
                status = existing["status"]
                merchant = existing["merchant"]
                category = existing["category"]
                note = existing["note"]
                scope = existing["scope"]
            else:
                status = "pending"
                merchant = candidate.get("merchant") or ""
                category = candidate.get("current_category") or "Other"
                note = ""
                scope = "same_raw_merchant_in_run"

            conn.execute(
                """
                insert into import_review (
                    review_key, status, merchant, category, note, scope, txn_ids, examples,
                    transaction_count, total_abs_amount, created_at, updated_at
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                on conflict(review_key) do update set
                    txn_ids = excluded.txn_ids,
                    examples = excluded.examples,
                    transaction_count = excluded.transaction_count,
                    total_abs_amount = excluded.total_abs_amount,
                    updated_at = excluded.updated_at
                """,
                (
                    candidate["key"],
                    status,
                    merchant,
                    category,
                    note,
                    scope,
                    json.dumps(candidate.get("txn_ids", [])),
                    json.dumps(candidate.get("examples", []), default=_json_default),
                    int(candidate.get("count", 0)),
                    str(candidate.get("total_abs_amount", "0")),
                    now,
                    now,
                ),
            )


def get_review_rows(db_path: Path) -> list[dict[str, Any]]:
    """Read review rows without initializing or migrating the database."""
    if not db_path.is_file():
        return []
    with bound_database_read_connection(
        db_path,
        label="Review Queue render source",
        require_main_owner_only=True,
        require_parent_owner_only=True,
    ) as conn:
        conn.row_factory = sqlite3.Row
        table_exists = conn.execute(
            "select 1 from sqlite_master where type = 'table' and name = 'import_review'"
        ).fetchone()
        if table_exists is None:
            return []
        rows = conn.execute(
            """
            select review_key, status, merchant, category, note, scope, txn_ids, examples,
                   transaction_count, total_abs_amount, created_at, updated_at
            from import_review
            order by status = 'pending' desc, transaction_count desc, review_key
            """
        ).fetchall()
    return [dict(row) for row in rows]


def save_review_rows(db_path: Path, rows: list[dict[str, Any]]) -> None:
    ensure_review_schema(db_path)
    now = _now()
    with _connection(db_path) as conn:
        for row in rows:
            status = row.get("status") or "pending"
            if status not in REVIEW_STATUSES:
                status = "pending"
            conn.execute(
                """
                update import_review
                set status = ?, merchant = ?, category = ?, note = ?, scope = ?, updated_at = ?
                where review_key = ?
                """,
                (
                    status,
                    row.get("merchant") or "",
                    row.get("category") or "Other",
                    row.get("note") or "",
                    row.get("scope") or "same_raw_merchant_in_run",
                    now,
                    row["review_key"],
                ),
            )


def pending_review_count(db_path: Path) -> int:
    ensure_review_schema(db_path)
    with _connection(db_path) as conn:
        return int(conn.execute(_NEEDS_DECISION_SQL).fetchone()[0])


def pending_review_count_if_present(db_path: Path) -> int:
    """Count open review rows WITHOUT creating anything.

    ``pending_review_count`` calls ``ensure_review_schema`` first, which creates the
    schema — unacceptable on a read-only path (the evidence summary counts against the
    live DB and against staged run databases it must never modify, and connecting to a
    missing path would materialise an empty file). Returns 0 when the database or the
    ``import_review`` table does not exist.
    """
    if not db_path.exists():
        return 0
    try:
        with _connection(db_path) as conn:
            exists = conn.execute(
                "select 1 from sqlite_master where type = 'table' and name = 'import_review'"
            ).fetchone()
            if not exists:
                return 0
            return int(conn.execute(_NEEDS_DECISION_SQL).fetchone()[0])
    except sqlite3.Error:
        # An unreadable/locked database is an evidence gap, not a review decision.
        logger.warning("could not count pending review rows")
        return 0


def _resolution_exclusions(
    db_path: Path,
) -> tuple[MerchantRulesConfig, dict[str, bool]] | None:
    """Return (merchant-rules config, category_name→exclude), or ``None`` if the
    runtime config cannot be loaded.

    A resolved row's ``exclude_from_spending`` is computed exactly as the rules-apply
    pass does (``rules/service.py``): the OR of the matched merchant-RULE exclusion and
    the category exclusion. The merchant side is derived with
    ``MerchantRulesConfig.find_merchant`` against the transaction's raw text (the same
    regex pattern-match rules-apply performs), NOT a byte-exact lookup on the resolved
    display merchant name. A name lookup diverged from rules-apply whenever the raw text
    pattern-matched an excluding rule but the resolved display name was not identical to
    that rule's ``merchant_norm`` — and because resolved rows are MANUAL (which
    rules-apply skips), that divergent flag never self-healed (audit finding F16). We
    therefore return the whole rules config so the caller can re-match per transaction.

    Returning ``None`` on load failure (rather than empty maps) lets the caller leave the
    flag UNCHANGED instead of silently forcing it to ``False`` and un-excluding
    correctly-excluded rows.
    """
    try:
        repo = SQLiteConfigRepository(db_path)
        merchants = repo.load_merchant_rules()
        categories = repo.load_categories()
    except Exception:  # config load must never block apply
        logger.warning("could not load exclusion config; leaving exclude_from_spending unchanged")
        return None
    category_exc = {c.name: bool(c.exclude_from_spending) for c in categories.categories}
    return merchants, category_exc


def _resolution_exclusions_connection(
    connection: sqlite3.Connection,
) -> tuple[MerchantRulesConfig, dict[str, bool]] | None:
    """Raw-connection equivalent of ``_resolution_exclusions`` for atomic UI writes."""
    try:
        metadata = {
            str(row["key"]): str(row["value"])
            for row in connection.execute(
                "select key, value from config_metadata where key = 'merchants_version'"
            ).fetchall()
        }
        rows = connection.execute(
            """
            select r.id, r.merchant_norm, r.category, r.exclude_from_spending,
                   p.pattern
            from merchant_rules r
            left join merchant_rule_patterns p on p.rule_id = r.id
            order by r.sort_order, p.sort_order
            """
        ).fetchall()
        grouped: dict[int, dict[str, Any]] = {}
        for row in rows:
            rule = grouped.setdefault(
                int(row["id"]),
                {
                    "merchant_norm": str(row["merchant_norm"]),
                    "category": str(row["category"]),
                    "exclude_from_spending": bool(row["exclude_from_spending"]),
                    "patterns": [],
                },
            )
            if row["pattern"] is not None:
                rule["patterns"].append(str(row["pattern"]))
        merchants = MerchantRulesConfig(
            version=metadata.get("merchants_version", "sqlite"),
            rules=[MerchantRule(**rule) for rule in grouped.values()],
        )
        category_exc = {
            str(row["name"]): bool(row["exclude_from_spending"])
            for row in connection.execute(
                "select name, exclude_from_spending from category_config"
            ).fetchall()
        }
        if not grouped or not category_exc:
            return None
        return merchants, category_exc
    except Exception:  # parity with the existing best-effort config read
        logger.warning("could not load exclusion config; leaving exclude_from_spending unchanged")
        return None


def _apply_review_decisions_connection(
    connection: sqlite3.Connection,
    review_keys: list[str] | None,
    exclusion_maps: tuple[MerchantRulesConfig, dict[str, bool]] | None,
) -> int:
    """Apply selected decisions through the caller-owned SQLite transaction."""
    parameters: list[Any] = []
    key_filter = ""
    if review_keys is not None:
        if not review_keys:
            return 0
        key_filter = f" and review_key in ({','.join('?' for _ in review_keys)})"
        parameters.extend(review_keys)
    rows = connection.execute(
        f"""
        select review_key, status, merchant, category, txn_ids
        from import_review
        where status in ('resolved', 'reviewed_as_is'){key_filter}
        """,
        parameters,
    ).fetchall()
    updated = 0
    for row in rows:
        txn_ids = json.loads(row["txn_ids"] or "[]")
        if row["status"] == "resolved":
            if not row["merchant"] or not row["category"] or not txn_ids:
                continue
            placeholders = ",".join("?" for _ in txn_ids)
            if exclusion_maps is not None:
                merchants_cfg, category_exc = exclusion_maps
                if str(row["category"]) not in category_exc:
                    raise ReviewConflictError(
                        "Authoritative category configuration changed; reload and retry"
                    )
                category_exclude = category_exc[str(row["category"])]
                text_rows = connection.execute(
                    f"""
                    select txn_id, description, merchant_raw
                    from transactions
                    where txn_id in ({placeholders})
                    """,
                    txn_ids,
                ).fetchall()
                ids_by_exclude: dict[bool, list[str]] = {True: [], False: []}
                for transaction in text_rows:
                    _, _, merchant_exclude = merchants_cfg.find_merchant(
                        *merchant_match_texts(
                            str(transaction["merchant_raw"] or ""),
                            str(transaction["description"] or ""),
                        ),
                        display_name=row["merchant"],
                    )
                    exclude = bool(merchant_exclude) or category_exclude
                    ids_by_exclude[exclude].append(transaction["txn_id"])
                for exclude_value, ids in ids_by_exclude.items():
                    if not ids:
                        continue
                    group_placeholders = ",".join("?" for _ in ids)
                    cursor = connection.execute(
                        f"""
                        update transactions
                        set merchant = ?, category = ?, exclude_from_spending = ?,
                            merchant_confidence = 'manual', category_confidence = 'manual'
                        where txn_id in ({group_placeholders})
                        """,
                        [row["merchant"], row["category"], exclude_value, *ids],
                    )
                    updated += int(cursor.rowcount or 0)
            else:
                cursor = connection.execute(
                    f"""
                    update transactions
                    set merchant = ?, category = ?, merchant_confidence = 'manual',
                        category_confidence = 'manual'
                    where txn_id in ({placeholders})
                    """,
                    [row["merchant"], row["category"], *txn_ids],
                )
                updated += int(cursor.rowcount or 0)
        else:
            updated += len(txn_ids)
    return updated


def _record_review_audit_savepoint(
    connection: sqlite3.Connection,
    *,
    db_path: Path,
    started_at: Any,
    updated: int,
) -> None:
    """Record one atomic audit insert without taking caller-owned transaction control."""
    try:
        connection.execute(
            """
            insert into db_operation_audit (
                operation, status, started_at, finished_at, db_path, artifact_path,
                counts, warnings, errors, details
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "review_apply",
                "success",
                started_at.isoformat(),
                utc_now().isoformat(),
                str(db_path),
                None,
                json.dumps({"transactions_updated": updated}),
                "[]",
                "[]",
                json.dumps({"source": "import_review"}),
            ),
        )
    except sqlite3.Error as exc:
        logger.warning("audit write failed for review_apply: %s", exc)


def apply_review_mutations(
    connection: sqlite3.Connection,
    db_path: Path,
    mutations: list[dict[str, Any]],
) -> int:
    """CAS-save changed rows and apply exactly those decisions atomically."""
    keys = [str(mutation.get("review_key") or "") for mutation in mutations]
    if not keys:
        return 0
    if any(not key for key in keys) or len(keys) != len(set(keys)):
        raise ReviewConflictError("Review rows changed; reload and retry")
    exclusion_maps = _resolution_exclusions_connection(connection)
    if exclusion_maps is None:
        raise ReviewConflictError(
            "Authoritative category configuration is unavailable; reload and retry"
        )
    valid_categories = exclusion_maps[1]
    for mutation in mutations:
        desired = mutation.get("desired")
        if not isinstance(desired, dict):
            raise ReviewConflictError("Review rows changed; reload and retry")
        if (
            str(desired.get("status") or "pending") == "resolved"
            and str(desired.get("category") or "") not in valid_categories
        ):
            raise ReviewConflictError(
                "Authoritative category configuration changed; reload and retry"
            )
    now = _now()
    started_at = utc_now()
    for mutation in mutations:
        expected = mutation.get("expected")
        desired = mutation.get("desired")
        if not isinstance(expected, dict) or not isinstance(desired, dict):
            raise ReviewConflictError("Review rows changed; reload and retry")
        if any(field not in expected for field in REVIEW_EXPECTED_FIELDS):
            raise ReviewConflictError("Review rows changed; reload and retry")
        if any(field not in desired for field in REVIEW_MUTATION_FIELDS):
            raise ReviewConflictError("Review rows changed; reload and retry")
        status = str(desired["status"] or "pending")
        if status not in REVIEW_STATUSES:
            status = "pending"
        cursor = connection.execute(
            """
            update import_review
            set status = ?, merchant = ?, category = ?, note = ?, scope = ?, updated_at = ?
            where review_key = ? and status = ? and merchant = ? and category = ?
              and note = ? and scope = ? and txn_ids = ? and examples = ?
              and transaction_count = ? and total_abs_amount = ? and updated_at = ?
            """,
            (
                status,
                str(desired["merchant"] or ""),
                str(desired["category"] or "Other"),
                str(desired["note"] or ""),
                str(desired["scope"] or "same_raw_merchant_in_run"),
                now,
                mutation["review_key"],
                str(expected["status"] or "pending"),
                str(expected["merchant"] or ""),
                str(expected["category"] or "Other"),
                str(expected["note"] or ""),
                str(expected["scope"] or "same_raw_merchant_in_run"),
                str(expected["txn_ids"] or "[]"),
                str(expected["examples"] or "[]"),
                int(expected["transaction_count"] or 0),
                str(expected["total_abs_amount"] or "0"),
                str(expected["updated_at"]),
            ),
        )
        if cursor.rowcount != 1:
            raise ReviewConflictError("Review rows changed; reload and retry")
    updated = _apply_review_decisions_connection(
        connection,
        keys,
        exclusion_maps,
    )
    _record_review_audit_savepoint(
        connection,
        db_path=db_path,
        started_at=started_at,
        updated=updated,
    )
    return updated


def apply_review_decisions(db_path: Path) -> int:
    """Apply resolved/reviewed_as_is decisions from the import_review table.

    ``resolved``: user overrode merchant and/or category → write those values to
    the transactions table and set ``merchant_confidence``/``category_confidence``
    to ``'manual'`` so future rule passes leave the rows alone (unchanged).

    ``reviewed_as_is``: user accepted the current auto-derived values and only wants
    to stop being prompted about this row.  We deliberately do NOT set confidence
    to ``'manual'`` on the transaction — the merchant/category stay auto-derived and
    remain updatable by future rule passes.  The row already has
    ``status='reviewed_as_is'``, which is sufficient to exclude it from the review
    queue (``pending_review_count`` and the UI both filter on ``status='pending'`` /
    resolved-with-missing); no extra column is needed.
    """
    ensure_review_schema(db_path)
    started_at = utc_now()
    exclusion_maps = _resolution_exclusions(db_path)
    with _connection(db_path) as conn:
        updated = _apply_review_decisions_connection(conn, None, exclusion_maps)

    try:
        record_db_operation(
            db_path,
            operation="review_apply",
            result=RunResult(
                status="success",
                started_at=started_at,
                finished_at=utc_now(),
                counts={"transactions_updated": updated},
                details={"source": "import_review"},
            ),
        )
    except Exception as exc:
        logger.warning("audit write failed for review_apply: %s", exc)
    return updated
