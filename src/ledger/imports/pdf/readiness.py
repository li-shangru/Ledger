"""Read-only readiness checks for assisted PDF import runs."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
from collections.abc import Callable, Iterator
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from typing import Any

from ledger.storage.snapshots import bound_database_read_connection
from ledger.utils.private_files import PinnedPrivateFile, pin_existing_directory

FINALIZABLE_STATUSES = {"staging", "staged", "needs_review", "ready_to_finalize"}
_KNOWN_RUN_STATUSES = FINALIZABLE_STATUSES | {
    "failed",
    "finalize_failed",
    "finalizing",
    "finalized",
    "cancelled",
}
_MAX_COORDINATION_RECORD_BYTES = 64 * 1024
_MAX_COORDINATION_RECORDS = 4096
PUBLICATION_TOKEN_METADATA_KEY = "__ledger_pdf_publication_token"
PUBLICATION_PROTOCOL_VERSION = 2
_PUBLICATION_PROTOCOL_KEYS = {
    "finalize_protocol_version",
    "publication_phase",
    "publication_token",
    "publication_candidate_import_owned",
    "publication_completed",
    "publication_target_identity",
    "finalize_tail",
}
_CURRENT_PROTOCOL_DISCRIMINATOR_KEYS = {
    "finalize_protocol_version",
    "publication_phase",
    "publication_token",
    "publication_target_identity",
}

UNREADABLE_IMPORT_RUN_REMEDY = (
    "One run's coordination state could not be read, so promotability cannot be ruled "
    "out. The usual cause is an oversized `run.json`: it doubles as the run archive and "
    f"can exceed the {_MAX_COORDINATION_RECORD_BYTES}-byte coordination cap. Find it with "
    "`ls -l <runs-dir>/*/run.json`. For a run that already finalized or cancelled, "
    "register a bounded coordination record with "
    "`ledger import pdf repair-coordination --run-dir <run-dir>`; a run that is still "
    "open must be finalized or cancelled instead."
)

OPEN_IMPORT_RUN_REMEDY = (
    "Close the run first — `ledger import pdf finalize --run-dir <run-dir>` or "
    "`ledger import pdf cancel --run-dir <run-dir>` — or teach inside the run itself "
    "with `ledger import pdf review resolve --teach`, which writes the rule to that "
    "run's staging database so finalize promotes it. To defer the teaching action instead, "
    "ask an agent using `ledger-mcp-write` to file it in the proposal queue, then inspect "
    "that queue with `ledger proposals list`."
)


class LiveMutationDuringOpenImportRefused(ValueError):  # noqa: N818
    """Raised when a live write could strand decisions in a promotable import run."""


class OpenImportCoordinationError(RuntimeError):
    """Raised when durable import-run coordination cannot be read safely."""


def clear_publication_protocol(state: dict[str, Any]) -> None:
    """Remove every publication artifact before a non-recovery state write."""
    for key in _PUBLICATION_PROTOCOL_KEYS:
        state.pop(key, None)


def validate_publication_protocol_state(state: dict[str, Any]) -> None:
    """Enforce legacy-v1, transitional tokenized-v1, and canonical-v2 states.

    Commit ``67836aa`` emitted tokenless v1 states. Commit ``2d10bf9`` then emitted
    tokenized v1 states before the token grammar received its own version. Both are
    durable on disk and remain readable, but only tokenized-v1 and v2 may use the
    target inode/token proof. New writes use v2.
    """
    version = state.get("finalize_protocol_version")
    phase = state.get("publication_phase")
    token = state.get("publication_token")
    marker = state.get("publication_completed")
    identity = state.get("publication_target_identity")
    candidate = state.get("publication_candidate_import_owned")
    tail = state.get("finalize_tail")
    status = str(state.get("status", ""))

    if version is None:
        # Legacy states may carry the old candidate/tail/marker fields, but v1-only
        # evidence can never be accepted without the explicit protocol discriminator.
        if phase is not None or token is not None or identity is not None:
            raise ValueError("Import publication state is invalid")
        return
    if version not in {1, PUBLICATION_PROTOCOL_VERSION} or phase not in {
        "preparing",
        "candidate_ready",
        "published",
    }:
        raise ValueError("Import publication state is invalid")
    tokenized = version == PUBLICATION_PROTOCOL_VERSION or token is not None
    if tokenized and not _valid_publication_token(token):
        raise ValueError("Import publication state is invalid")
    if not tokenized and identity is not None:
        raise ValueError("Import publication state is invalid")
    recovery_status = status in {"finalizing", "finalize_failed"} or (
        status == "failed" and state.get("failure_reason") == "finalize_failed"
    )
    if phase in {"preparing", "candidate_ready"} and not recovery_status:
        raise ValueError("Import publication state is invalid")
    if phase == "published" and not (recovery_status or status == "finalized"):
        raise ValueError("Import publication state is invalid")

    if phase == "preparing":
        if any(value is not None for value in (candidate, tail, marker, identity)):
            raise ValueError("Import publication state is invalid")
        return
    if not isinstance(candidate, dict) or not isinstance(candidate.get("tables"), dict):
        raise ValueError("Import publication state is invalid")
    if not isinstance(tail, dict):
        raise ValueError("Import publication state is invalid")
    if phase == "candidate_ready":
        if marker is not None or identity is not None:
            raise ValueError("Import publication state is invalid")
        return
    if marker is not True:
        raise ValueError("Import publication state is invalid")
    if tokenized and not _valid_target_identity(identity):
        raise ValueError("Import publication state is invalid")
    if not tokenized and identity is not None:
        raise ValueError("Import publication state is invalid")


def _valid_publication_token(token: object) -> bool:
    return (
        isinstance(token, str)
        and len(token) == 32
        and all(char in "0123456789abcdef" for char in token)
    )


def _valid_target_identity(identity: object) -> bool:
    return (
        isinstance(identity, dict)
        and set(identity) == {"dev", "ino"}
        and all(isinstance(identity[key], int) and identity[key] >= 0 for key in ("dev", "ino"))
    )


def publication_protocol_is_tokenized(state: dict[str, Any]) -> bool:
    """Whether a validated state carries the inode/token publication proof."""
    version = state.get("finalize_protocol_version")
    return version == PUBLICATION_PROTOCOL_VERSION or (
        version == 1 and state.get("publication_token") is not None
    )


def publication_protocol_is_tokenless_v1(state: dict[str, Any]) -> bool:
    """Whether a validated state is the durable tokenless-v1 compatibility form."""
    return state.get("finalize_protocol_version") == 1 and state.get("publication_token") is None


_LIVE_MUTATION_LABELS = {
    "runtime_config": "replace live runtime configuration",
    "runtime_rules": "apply live runtime rules",
    "transaction_category": "change a live transaction category",
    "category_optimizer": "optimize live categories",
    "database_doctor": "repair the live database",
    "analysis_refresh": "refresh live analysis",
    "statement_repair": "publish a statement repair",
    "ui_config_seed": "seed live runtime configuration",
    "ui_review_apply": "apply live review decisions",
    "pdf_stage": "stage another PDF import",
    "proposal_apply": "apply a deferred proposal",
}


def _live_mutation_label(operation: str) -> str:
    return _LIVE_MUTATION_LABELS.get(operation, "perform a live ledger mutation")


# ── Merge-forward / import-owned split ────────────────────────────────────────
#
# These tables are pure-user runtime state that the import pipeline never
# writes.  Before an atomic promote we:
#  1. Merge them FORWARD from the live DB into the promote temp (so a budget/goal/
#     salt/closure edit made after stage is preserved, not clobbered).
#  2. Exclude them from the import-owned fingerprint (so an edit to these tables
#     does NOT trigger the drift-abort guard — only import-owned changes abort).
#
# CRITICAL: both mechanisms MUST reference this single tuple.  The invariant test
# (test 9) asserts that the set excluded from import_owned_fingerprint exactly
# equals this set.
#
# account_lifecycle is here for a third reason on top of those two: repair's
# _promote_candidate_locked calls merge_forward, so this tuple is ALSO what makes a
# table survive `repair statements`, which otherwise publishes an empty candidate
# wholesale.  An operator's "this card is closed" record is unrecoverable from any
# PDF, so losing it to a repair would be silent, permanent data loss.
MERGE_FORWARD_TABLES: tuple[str, ...] = (
    "budgets",
    "savings_goals",
    "spending_preferences",
    "planned_schedules",
    "planned_schedule_revisions",
    "plan_reconciliation_reviews",
    "planning_proposal_receipts",
    "planning_definition_generations",
    "config_metadata",
    "account_lifecycle",
)

# ── Drift-exempt side-effect / derived tables (M6) ────────────────────────────
#
# These tables are ALSO excluded from import_owned_fingerprint (in ADDITION to
# MERGE_FORWARD_TABLES), but for a DIFFERENT reason: they are not user-config that
# gets merged forward — they are pure SIDE-EFFECT logs and finalize-RECOMPUTED
# derived caches.  Routine, innocent use of the LIVE target during the
# stage→finalize review window writes to them:
#   * opening the Reporting page snapshots → report_snapshots + db_operation_audit
#   * `ledger db validate`                 → db_operation_audit
#   * a `reports refresh`                  → spending_trends / financial_health_scores
# Before this exemption, ANY such action changed the target's fingerprint, so the
# pre-promote drift recheck aborted ("target DB changed during finalize") and the
# whole staged import had to be cancelled and restaged — a repeatable, spurious
# block on harmless activity.
#
# Every table here is either an APPEND-ONLY audit/log (harmless to have extra rows)
# or is a fully-RECOMPUTED derived cache that carries NO user state: the finalize
# AnalysisRunner / post-processing pipeline (run_post_processing → trend aggregation,
# health scoring, data-quality checks, report snapshot) discards the target's copy
# and rebuilds it wholesale.  Drift on them therefore cannot represent lost user data.
#
# FAIL-SAFE BOUNDARY — deliberately NOT exempted:
#   * import-owned DATA tables (transactions, transfers, import_runs,
#     processed_files, accounts, account_balances, and every investment_* table)
#     stay in the gate: drift on them is real potential data loss. New investment
#     tables participate automatically through Base.metadata.
#   * config tables (merchant_rules, merchant_rule_patterns, category_config,
#     category_merchants) stay in the gate: this is entangled with the open
#     "which DB does the operator edit during review" question, so blocking is the
#     fail-safe choice (better to abort than to silently discard a live config edit).
#   * recurring_patterns is NOT exempted: save_patterns only rebuilds the
#     detection_method='rule_based' rows, so manually-created / other-method
#     patterns survive across finalize — it is not fully recomputed, so it stays in
#     the gate (fail-safe: over-blocking is acceptable, under-detecting is not).
#   * insight_findings is NOT exempted: unlike the pure caches above it holds USER
#     TRIAGE STATE (the `status` 'ignored'/'reviewed'/'watch' + `reviewed_at`,
#     written to the LIVE target via the Insights page → InsightEngine.update_status).
#     refresh_period runs on the STAGING clone and preserves user status only for
#     findings already present in that clone; promote then overwrites the target's
#     insight_findings wholesale. So a triage decision made during the review window
#     would be silently lost if it were exempt — it stays in the gate (same
#     "holds user decisions / only partially recomputed" property as recurring_patterns).
#
# Every name below is verified to be a real __tablename__ in ledger.storage.database.
DRIFT_EXEMPT_TABLES: tuple[str, ...] = (
    "db_operation_audit",  # append-only audit log of DB operations (side-effect)
    "report_snapshots",  # recomputed at finalize (ReportSnapshotService.create_snapshot)
    "data_quality_checks",  # recomputed at finalize (DataQualityService.run_checks)
    "export_runs",  # append-only log of export operations (side-effect)
    "rule_application_runs",  # append-only log of rule-application runs (side-effect)
    "financial_health_scores",  # recomputed at finalize (AnalysisRunner.run_health_scoring)
    "spending_trends",  # recomputed at finalize (AnalysisRunner.run_trend_aggregation)
)

# Investment publication owns only these tables. Every other table is live state
# that an investment import must preserve or fail closed on at the publication
# boundary. Keep the owned set explicit so a newly-added table is preserved by
# default until investment publication intentionally adopts it.
INVESTMENT_OWNED_TABLES: tuple[str, ...] = (
    "investment_accounts",
    "investment_statements",
    "investment_asset_balances",
    "investment_cash_balances",
    "investment_positions",
    "investment_performance",
    "investment_activities",
    "investment_source_files",
    "investment_source_associations",
    "investment_import_runs",
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _table_count(conn: sqlite3.Connection, table: str) -> int:
    exists = conn.execute(
        "select 1 from sqlite_master where type = 'table' and name = ?",
        (table,),
    ).fetchone()
    if not exists:
        return 0
    return int(conn.execute(f"select count(*) from {table}").fetchone()[0])


def _db_counts(path: Path) -> dict[str, int]:
    # `with sqlite3.connect(...)` manages the transaction, NOT closing the
    # connection — wrap in contextlib.closing so the handle is released.
    with closing(sqlite3.connect(path)) as conn:
        return {
            "transactions": _table_count(conn, "transactions"),
            "transfers": _table_count(conn, "transfers"),
            "import_runs": _table_count(conn, "import_runs"),
        }


def database_fingerprint(path: Path | str) -> dict[str, Any]:
    """Return a compact fingerprint for detecting live DB drift after staging."""
    db_path = Path(path)
    if not db_path.exists():
        return {
            "path": str(db_path),
            "exists": False,
            "sha256": "",
            "size_bytes": 0,
            "modified_ns": 0,
            "counts": {},
        }

    stat = db_path.stat()
    fingerprint: dict[str, Any] = {
        "path": str(db_path),
        "exists": True,
        "sha256": _sha256_file(db_path),
        "size_bytes": stat.st_size,
        "modified_ns": stat.st_mtime_ns,
        "counts": _db_counts(db_path),
    }
    return fingerprint


_FINGERPRINT_KEYS = ("exists", "sha256", "size_bytes", "modified_ns", "counts")


def fingerprints_match(a: dict, b: dict) -> bool:
    """Return True iff the canonical fingerprint keys are identical in both dicts.

    Used by the drift-recheck guard in promote_database, promote_candidate, and
    target_db_unchanged so the compared key tuple lives in exactly one place.
    """
    return all(a.get(k) == b.get(k) for k in _FINGERPRINT_KEYS)


# ── Table-aware import-owned fingerprint ──────────────────────────────────────


def _pk_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """Return the primary-key columns for *table* in declaration order."""
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    pks = [(row[5], row[1]) for row in rows if row[5] > 0]  # (pk_seq, col_name)
    if pks:
        return [col for _seq, col in sorted(pks)]
    # No PK — fall back to rowid ordering (stable within a transaction).
    return []


def _table_hash(conn: sqlite3.Connection, table: str) -> list[int | str]:
    """Return [row_count, sha256_of_rows] for *table*.

    Returns a list (not a tuple) so it round-trips through JSON without type
    change (json.dumps turns tuples into arrays → on load they come back as
    lists; using list here keeps the equality check in import_owned_match
    stable across JSON round-trips).

    Rows are ordered by PK columns (or rowid) so the hash is deterministic
    regardless of insertion order.  Values are serialised via repr() which is
    stable for the Python types sqlite3 returns (int, float, str, bytes, None).
    Numeric columns stored as TEXT (e.g. Decimal amounts) are read verbatim.
    """
    pk_cols = _pk_columns(conn, table)
    order_clause = ", ".join(pk_cols) if pk_cols else "rowid"

    rows = conn.execute(f"SELECT * FROM {table} ORDER BY {order_clause}").fetchall()
    digest = hashlib.sha256()
    for row in rows:
        digest.update(repr(row).encode())
    return [len(rows), digest.hexdigest()]


def _selected_table_fingerprint_from_connection(
    connection: sqlite3.Connection,
    table_names: set[str],
) -> dict[str, Any]:
    table_hashes: dict[str, list[int | str]] = {}
    for table in sorted(table_names):
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if exists:
            table_hashes[table] = _table_hash(connection, table)
        else:
            if table == "investment_cash_balances":
                from ledger.storage.migrations import is_legacy_investment_cash_schema

                if is_legacy_investment_cash_schema(connection):
                    # A reviewed 0029 run predates this table and its hash key.
                    # Keep that schema-specific fingerprint byte-for-byte until
                    # an explicit migration adds evidence storage and its marker.
                    continue
            table_hashes[table] = [0, hashlib.sha256(b"__absent__").hexdigest()]
    return {"tables": table_hashes}


def _selected_table_fingerprint(
    database: Path | sqlite3.Connection,
    table_names: set[str],
) -> dict[str, Any]:
    if isinstance(database, sqlite3.Connection):
        return _selected_table_fingerprint_from_connection(database, table_names)
    if not database.exists():
        return {"tables": {}}
    with closing(sqlite3.connect(database)) as connection:
        return _selected_table_fingerprint_from_connection(connection, table_names)


def import_owned_fingerprint(
    path: Path | str | sqlite3.Connection,
) -> dict[str, Any]:
    """Return a per-table content hash for every table EXCEPT the excluded set.

    The excluded set is ``MERGE_FORWARD_TABLES | DRIFT_EXEMPT_TABLES``.

    The returned dict has the form::

        {"tables": {table_name: [row_count, sha256_hex], ...}}

    Only tables present in SQLAlchemy's Base.metadata are hashed — new tables are
    import-owned by default.  MERGE_FORWARD_TABLES are excluded so that
    budget/goal/salt edits do NOT trigger the drift-abort guard, and
    DRIFT_EXEMPT_TABLES are excluded because they are pure side-effect logs or
    finalize-recomputed derived caches whose drift on the live target is harmless
    (see the constant's docstring for the fail-safe boundary).

    Uses raw ``sqlite3`` (no SQLAlchemy) to avoid type-coercion: Decimal values
    stored as TEXT read verbatim, matching the existing ``_db_counts`` style.

    If *path* does not exist, returns the absent-sentinel ``{"tables": {}}`` WITHOUT
    calling ``sqlite3.connect`` (which would create a 0-byte file as a side effect).
    This mirrors ``database_fingerprint``'s ``if not db_path.exists(): return ...``
    guard.  The sentinel is stable: two absent-sentinels compare equal via
    ``import_owned_match``, so a first-import (target absent at both stage-capture
    and the pre-promote recheck) does NOT spuriously abort the promote.
    """
    from ledger.storage.database import Base  # lazy import to avoid circularity

    all_tables = set(Base.metadata.tables.keys())
    excluded = set(MERGE_FORWARD_TABLES) | set(DRIFT_EXEMPT_TABLES)
    owned_tables = sorted(all_tables - excluded)
    database = path if isinstance(path, sqlite3.Connection) else Path(path)
    return _selected_table_fingerprint(database, set(owned_tables))


def import_owned_match(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Return True iff the per-table import-owned fingerprints are identical.

    Both *a* and *b* must be dicts produced by ``import_owned_fingerprint``.
    """
    return a.get("tables") == b.get("tables")


def investment_preserved_fingerprint(
    path: Path | str | sqlite3.Connection,
) -> dict[str, Any]:
    """Hash every table whose live state investment publication does not own."""
    from ledger.storage.database import Base  # lazy import to avoid circularity

    all_tables = set(Base.metadata.tables)
    investment_tables = set(INVESTMENT_OWNED_TABLES)
    unknown = investment_tables - all_tables
    if unknown:
        raise RuntimeError(f"Investment-owned table contract is invalid: {sorted(unknown)}")
    database = path if isinstance(path, sqlite3.Connection) else Path(path)
    return _selected_table_fingerprint(database, all_tables - investment_tables)


def investment_preserved_match(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Return whether two non-investment aggregate/hash fingerprints match."""
    return a.get("tables") == b.get("tables")


# ── merge_forward helper ───────────────────────────────────────────────────────


class SchemaMismatchError(RuntimeError):
    """Raised when *live_db* and *into_db* have incompatible schemas for a
    MERGE_FORWARD_TABLE so the merge cannot proceed safely.

    Raised BEFORE any destructive write so a failed promote never leaves a
    partially-merged temp copy.
    """


def _table_columns(conn: sqlite3.Connection, table: str, schema: str = "main") -> list[str]:
    """Return the column names of *table* in attached database *schema*.

    SQLite's ``PRAGMA`` does not accept a ``schema.table`` argument; the schema
    must prefix the pragma name instead (``PRAGMA live.table_info(budgets)``).
    """
    rows = conn.execute(f"PRAGMA {schema}.table_info({table})").fetchall()
    return [str(row[1]) for row in rows]


def _table_schema(
    conn: sqlite3.Connection,
    table: str,
    schema: str = "main",
) -> list[list[Any]]:
    """Return a JSON-stable representation of a table's declared columns."""
    rows = conn.execute(f"PRAGMA {schema}.table_info({table})").fetchall()
    return [
        [int(row[0]), str(row[1]), str(row[2]), int(row[3]), row[4], int(row[5])] for row in rows
    ]


def _table_hash_for_columns(
    conn: sqlite3.Connection,
    table: str,
    columns: list[str],
    *,
    schema: str = "main",
) -> list[int | str]:
    """Hash selected table values in deterministic primary-key order."""
    schema_rows = _table_schema(conn, table, schema)
    pk_columns = [str(row[1]) for row in sorted(schema_rows, key=lambda row: int(row[5])) if row[5]]
    order_clause = ", ".join(pk_columns) if pk_columns else "rowid"
    column_list = ", ".join(columns)
    rows = conn.execute(
        f"SELECT {column_list} FROM {schema}.{table} ORDER BY {order_clause}"
    ).fetchall()
    digest = hashlib.sha256()
    for row in rows:
        digest.update(repr(row).encode())
    return [len(rows), digest.hexdigest()]


def merge_forward_contract_matches(
    contract: dict[str, Any],
    database: Path | str | sqlite3.Connection,
) -> bool:
    """Compare live runtime state with the exact schema/content copy contract."""

    def matches(connection: sqlite3.Connection) -> bool:
        tables = contract.get("tables")
        if not isinstance(tables, dict) or set(tables) != set(MERGE_FORWARD_TABLES):
            return False
        for table in MERGE_FORWARD_TABLES:
            expected = tables.get(table)
            if not isinstance(expected, dict):
                return False
            current_schema = _table_schema(connection, table)
            current_exists = bool(current_schema)
            if expected.get("source_exists") is not current_exists:
                return False
            if not current_exists:
                continue
            if expected.get("destination_exists") is not True:
                return False
            if expected.get("source_schema") != current_schema:
                return False
            copied_columns = expected.get("copied_columns")
            if not isinstance(copied_columns, list):
                return False
            current_columns = [str(row[1]) for row in current_schema]
            if len(copied_columns) != len(current_columns) or set(copied_columns) != set(
                current_columns
            ):
                return False
            current_content = _table_hash_for_columns(
                connection,
                table,
                copied_columns,
            )
            if expected.get("content") != current_content:
                return False
        return True

    if isinstance(database, sqlite3.Connection):
        return matches(database)
    db_path = Path(database)
    if not db_path.exists():
        return False
    with closing(sqlite3.connect(db_path)) as connection:
        return matches(connection)


def merge_forward(live_db: Path, into_db: Path) -> dict[str, Any]:
    """Copy MERGE_FORWARD_TABLES from *live_db* INTO *into_db* atomically.

    For each table in MERGE_FORWARD_TABLES the live rows replace the temp copy's
    rows. Column ordering is preserved by copying an EXPLICIT, schema-aware column
    list (never a bare ``SELECT *``) so a live target on an OLDER schema — one
    missing a column the migrated staging copy has (e.g. ``savings_goals.account_id``
    pre-migration) — is handled gracefully instead of crashing the promote with a
    raw ``sqlite3.OperationalError`` (column-count mismatch) mid-merge.

    Schema-mismatch handling (no partial promote):
      * ALL tables are validated UP FRONT, before any ``DELETE``. If a live table
        carries a column the temp copy's table lacks (live somehow NEWER), the
        merge is aborted with :class:`SchemaMismatchError` and nothing is written.
      * When the live table is on an OLDER schema (a subset of the temp copy's
        columns), only the shared columns are copied; the columns the live DB did
        not yet have take their declared defaults in the temp copy. This is the
        "migrate forward" semantic — preserve the user's runtime-table edits while
        the newer schema's added columns default cleanly.

    All writes run in a single transaction on *into_db*. The returned contract
    records each source table's exact presence, declared schema, shared-column
    copy plan, and copied content hash. Publication can therefore compare the
    same source representation at its exclusive boundary without mistaking
    destination-only migration defaults for live drift.

    CRITICAL: *into_db* must be the promote TEMP copy — never the run-dir
    staging_db (A6 depends on staging_db being unchanged post-promote).
    """
    with closing(sqlite3.connect(into_db)) as conn:
        conn.execute("ATTACH DATABASE ? AS live", (str(live_db),))
        try:
            # ── Pass 1: validate every table's schema BEFORE any destructive write ──
            # Build the per-table shared-column plan up front so a mismatch aborts
            # cleanly (no DELETE has run yet → no partial promote).
            copy_plan: dict[str, dict[str, Any]] = {}
            for table in MERGE_FORWARD_TABLES:
                live_exists = conn.execute(
                    "SELECT 1 FROM live.sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                if not live_exists:
                    # Table absent in live DB (pre-migration); leave into_db's copy as-is.
                    copy_plan[table] = {
                        "source_exists": False,
                        "destination_exists": bool(_table_columns(conn, table)),
                        "source_schema": [],
                        "copied_columns": [],
                        "content": None,
                    }
                    continue

                into_cols = _table_columns(conn, table, schema="main")
                live_schema = _table_schema(conn, table, schema="live")
                live_cols = [str(row[1]) for row in live_schema]
                if not into_cols:
                    # Table absent in the temp copy (e.g. a bare placeholder DB):
                    # nothing to merge INTO; leave it alone.
                    copy_plan[table] = {
                        "source_exists": True,
                        "destination_exists": False,
                        "source_schema": live_schema,
                        "copied_columns": [],
                        "content": None,
                    }
                    continue
                into_set = set(into_cols)
                live_set = set(live_cols)

                extra_in_live = live_set - into_set
                if extra_in_live:
                    # Live target carries columns the migrated temp copy lacks: the
                    # live DB is on a NEWER/divergent schema than this import expects.
                    # Abort BEFORE writing anything rather than silently dropping data.
                    raise SchemaMismatchError(
                        f"Cannot merge runtime table '{table}': the live database has "
                        f"column(s) {sorted(extra_in_live)} that the migrated staging "
                        "copy does not. The live database appears to be on a newer or "
                        "divergent schema. Migrate/restage so the schemas match before "
                        "finalizing; no changes were written."
                    )

                # Copy only the columns present in BOTH schemas, in the temp copy's
                # declared order. Columns the older live DB lacks default cleanly.
                shared = [col for col in into_cols if col in live_set]
                copy_plan[table] = {
                    "source_exists": True,
                    "destination_exists": True,
                    "source_schema": live_schema,
                    "copied_columns": shared,
                    "content": None,
                }

            # ── Pass 2: apply the plan in a single transaction ──
            contract: dict[str, Any] = {"tables": copy_plan}
            with conn:  # savepoint/transaction
                for table, table_plan in copy_plan.items():
                    if not table_plan["source_exists"]:
                        continue
                    if not table_plan["destination_exists"]:
                        continue
                    shared = table_plan["copied_columns"]
                    conn.execute(f"DELETE FROM {table}")
                    if not shared:
                        continue
                    collist = ", ".join(shared)
                    conn.execute(
                        f"INSERT INTO {table} ({collist}) SELECT {collist} FROM live.{table}"
                    )
                    table_plan["content"] = _table_hash_for_columns(
                        conn,
                        table,
                        shared,
                        schema="live",
                    )
            return contract
        finally:
            conn.execute("DETACH DATABASE live")


def is_sqlite_db(path: Path) -> bool:
    """Return True iff *path* starts with the SQLite3 magic header."""
    try:
        with path.open("rb") as fh:
            return fh.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def target_db_unchanged(run_state: dict[str, Any]) -> bool:
    before = run_state.get("target_db_fingerprint_before")
    target_db = run_state.get("target_db_path")
    if not isinstance(before, dict) or not target_db:
        return False

    current = database_fingerprint(target_db)
    return fingerprints_match(before, current)


def _run_can_still_promote(state: dict[str, Any]) -> bool:
    status = str(state.get("status", ""))
    if status in {"finalizing", "finalize_failed"} or (
        status == "failed" and state.get("failure_reason") == "finalize_failed"
    ):
        # Only the durable marker may release live writers. Equality between the
        # target and staging databases is a volatile recovery signal: a writer
        # admitted on equality could immediately make them differ again.
        return state.get("publication_completed") is not True
    return status in FINALIZABLE_STATUSES


def run_publication_completed(state: dict[str, Any]) -> bool:
    """Return only the persisted proof that publication completed."""
    return state.get("publication_completed") is True


class PublicationProofError(RuntimeError):
    """Raised when legacy publication proof cannot be obtained safely."""


def _wal_aware_import_owned_fingerprint(
    source: Path | PinnedPrivateFile,
    *,
    label: str,
    require_parent_owner_only: bool,
) -> dict[str, Any]:
    """Fingerprint the exact pinned main/WAL/SHM view through a private alias."""
    with bound_database_read_connection(
        source,
        label=label,
        require_main_owner_only=True,
        require_parent_owner_only=require_parent_owner_only,
    ) as connection:
        return import_owned_fingerprint(connection)


def publication_candidate_fingerprint(run_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    """Return the exact contained staging candidate's WAL-aware logical fingerprint."""
    staging_value = state.get("staging_db_path")
    if not isinstance(staging_value, str) or not staging_value:
        raise PublicationProofError("Publication recovery proof is unavailable") from None
    lexical_run = _absolute_lexical(run_dir)
    staging = _absolute_lexical(staging_value)
    if staging != lexical_run / "staging.db":
        raise PublicationProofError("Publication recovery proof is unavailable") from None
    try:
        with pin_existing_directory(lexical_run) as pinned_run:
            pinned_run.require_owner_only()
            with pinned_run.pin_private_file("staging.db") as staging_file:
                staging_file.validate()
                return _wal_aware_import_owned_fingerprint(
                    staging_file,
                    label="Publication candidate",
                    require_parent_owner_only=True,
                )
    except PublicationProofError:
        raise
    except Exception:
        raise PublicationProofError("Publication recovery proof is unavailable") from None


def recover_publication_completed(
    run_dir: Path,
    state: dict[str, Any],
    *,
    on_published: Callable[[dict[str, int]], None] | None = None,
) -> bool:
    """Affirm markerless publication from provenance and pinned SQLite files.

    The persisted candidate fingerprint proves staging provenance; WAL-aware reads
    then prove whether that exact candidate reached the target. This proof is
    intentionally recovery-only. Live writers use only the durable completion marker
    and therefore cannot invalidate a transient equality observation.
    """
    return _recover_publication_completed(run_dir, state, on_published=on_published)


def _target_namespace_absent(parent_descriptor: int, target_name: str) -> bool:
    return all(
        _sidecar_absent(parent_descriptor, f"{target_name}{suffix}")
        for suffix in ("", "-journal", "-wal", "-shm")
    )


def _sidecar_absent(parent_descriptor: int, name: str) -> bool:
    try:
        os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return True
    return False


def _published_token(connection: sqlite3.Connection) -> str | None:
    try:
        row = connection.execute(
            "select value from config_metadata where key = ?",
            (PUBLICATION_TOKEN_METADATA_KEY,),
        ).fetchone()
    except sqlite3.Error:
        return None
    return str(row[0]) if row is not None else None


def verify_published_marker(state: dict[str, Any]) -> None:
    """Validate the durable proof represented by one nonterminal published marker.

    Tokenized-v1 and v2 bind the marker to the exact target inode and reserved token.
    Tokenless-v1 predates those fields; its structurally valid published marker is the
    legacy durable proof. Requiring candidate equality here would strand an old run
    after a live writer legitimately changed the target following that marker.
    """
    validate_publication_protocol_state(state)
    if state.get("publication_phase") != "published":
        raise PublicationProofError("Publication recovery proof is unavailable") from None
    if publication_protocol_is_tokenless_v1(state):
        return
    if not publication_protocol_is_tokenized(state):
        raise PublicationProofError("Publication recovery proof is unavailable") from None
    target_value = state.get("target_db_path")
    identity = state.get("publication_target_identity")
    token = state.get("publication_token")
    if not isinstance(target_value, str) or not isinstance(identity, dict):
        raise PublicationProofError("Publication recovery proof is unavailable") from None
    target = _absolute_lexical(target_value)
    try:
        with pin_existing_directory(target.parent) as target_parent:
            target_parent.require_owner_only()
            with target_parent.pin_private_file(target.name) as target_file:
                if target_file.identity[:2] != (identity["dev"], identity["ino"]):
                    raise PublicationProofError(
                        "Publication recovery proof is unavailable"
                    ) from None
                with bound_database_read_connection(
                    target_file,
                    label="Published target",
                    require_main_owner_only=True,
                    require_parent_owner_only=True,
                ) as target_connection:
                    if _published_token(target_connection) != token:
                        raise PublicationProofError(
                            "Publication recovery proof is unavailable"
                        ) from None
                target_file.validate()
    except PublicationProofError:
        raise
    except Exception:
        raise PublicationProofError("Publication recovery proof is unavailable") from None


def _recover_publication_completed(
    run_dir: Path,
    state: dict[str, Any],
    *,
    on_published: Callable[[dict[str, int]], None] | None,
) -> bool:
    """Recovery implementation with an optional under-pin durable-marker callback."""
    try:
        validate_publication_protocol_state(state)
    except ValueError:
        raise PublicationProofError("Publication recovery proof is unavailable") from None
    target_value = state.get("target_db_path")
    staging_value = state.get("staging_db_path")
    if not isinstance(target_value, str) or not target_value:
        raise PublicationProofError("Publication recovery proof is unavailable") from None
    if not isinstance(staging_value, str) or not staging_value:
        raise PublicationProofError("Publication recovery proof is unavailable") from None
    target = _absolute_lexical(target_value)
    lexical_run = _absolute_lexical(run_dir)
    staging = _absolute_lexical(staging_value)
    if staging != lexical_run / "staging.db" or target == staging:
        raise PublicationProofError("Publication recovery proof is unavailable") from None
    candidate = state.get("publication_candidate_import_owned")
    if not isinstance(candidate, dict) or not isinstance(candidate.get("tables"), dict):
        raise PublicationProofError("Publication recovery proof is unavailable") from None

    try:
        with ExitStack() as stack:
            target_parent = stack.enter_context(pin_existing_directory(target.parent))
            if _sidecar_absent(target_parent.descriptor, target.name):
                before = state.get("target_db_fingerprint_before")
                if (
                    state.get("finalize_protocol_version")
                    not in {
                        1,
                        PUBLICATION_PROTOCOL_VERSION,
                    }
                    or state.get("publication_phase") != "candidate_ready"
                    or not isinstance(before, dict)
                    or before.get("exists") is not False
                ):
                    raise PublicationProofError(
                        "Publication recovery proof is unavailable"
                    ) from None
                target_parent.require_owner_only()
                if not _target_namespace_absent(target_parent.descriptor, target.name):
                    raise PublicationProofError(
                        "Publication recovery proof is unavailable"
                    ) from None
                pinned_run = stack.enter_context(pin_existing_directory(lexical_run))
                pinned_run.require_owner_only()
                staging_file = stack.enter_context(pinned_run.pin_private_file("staging.db"))
                staging_fingerprint = _wal_aware_import_owned_fingerprint(
                    staging_file,
                    label="Publication candidate",
                    require_parent_owner_only=True,
                )
                if not import_owned_match(staging_fingerprint, candidate):
                    raise PublicationProofError(
                        "Publication recovery proof is unavailable"
                    ) from None
                target_parent.validate()
                staging_file.validate()
                if not _target_namespace_absent(target_parent.descriptor, target.name):
                    raise PublicationProofError(
                        "Publication recovery proof is unavailable"
                    ) from None
                return False
            target_file = stack.enter_context(target_parent.pin_private_file(target.name))
            pinned_run = stack.enter_context(pin_existing_directory(lexical_run))
            pinned_run.require_owner_only()
            staging_file = stack.enter_context(pinned_run.pin_private_file("staging.db"))
            if target_file.identity[:2] == staging_file.identity[:2]:
                raise PublicationProofError("Publication recovery proof is unavailable")
            with bound_database_read_connection(
                target_file,
                label="Publication target",
                require_main_owner_only=True,
                require_parent_owner_only=True,
            ) as target_connection:
                target_fingerprint = import_owned_fingerprint(target_connection)
                with bound_database_read_connection(
                    staging_file,
                    label="Publication candidate",
                    require_main_owner_only=True,
                    require_parent_owner_only=True,
                ) as staging_connection:
                    staging_fingerprint = import_owned_fingerprint(staging_connection)
                    if not import_owned_match(staging_fingerprint, candidate):
                        raise PublicationProofError(
                            "Publication recovery proof is unavailable"
                        ) from None
                    published = import_owned_match(target_fingerprint, candidate)
                    if published and publication_protocol_is_tokenized(state):
                        if _published_token(target_connection) != state.get("publication_token"):
                            raise PublicationProofError(
                                "Publication recovery proof is unavailable"
                            ) from None
                    if published and on_published is not None:
                        on_published(
                            {
                                "dev": target_file.identity[0],
                                "ino": target_file.identity[1],
                            }
                        )
                    target_file.validate()
                    staging_file.validate()
                    return published
    except PublicationProofError:
        raise
    except Exception:
        raise PublicationProofError("Publication recovery proof is unavailable") from None


def _read_coordinated_run(run_dir: Path) -> dict[str, Any]:
    try:
        with pin_existing_directory(run_dir) as pinned_run:
            pinned_run.require_owner_only()
            state = json.loads(
                pinned_run.read_private_text(
                    "run.json",
                    max_bytes=_MAX_COORDINATION_RECORD_BYTES,
                    encoding="utf-8",
                )
            )
    except OSError, json.JSONDecodeError, ValueError:
        raise OpenImportCoordinationError("Registered import run state is unreadable") from None
    if not isinstance(state, dict):
        raise OpenImportCoordinationError("Registered import run state is invalid") from None
    return state


def _absolute_lexical(path: Path | str) -> Path:
    return Path(os.path.abspath(os.fspath(Path(path).expanduser())))


def _validate_coordinated_state(
    *,
    record: dict[str, Any],
    state: dict[str, Any],
    target: Path,
) -> None:
    try:
        validate_publication_protocol_state(state)
    except ValueError:
        raise OpenImportCoordinationError("Import-run coordination state is invalid") from None
    if record.get("version") != 1 or state.get("version") != 1:
        raise OpenImportCoordinationError("Import-run coordination state is invalid") from None
    record_status = record.get("status")
    state_status = state.get("status")
    if record_status not in _KNOWN_RUN_STATUSES or state_status not in _KNOWN_RUN_STATUSES:
        raise OpenImportCoordinationError("Import-run coordination state is invalid") from None
    if record_status != state_status:
        raise OpenImportCoordinationError(
            "Import-run coordination state is contradictory"
        ) from None
    target_value = state.get("target_db_path")
    staging_value = state.get("staging_db_path")
    if not isinstance(target_value, str) or not target_value:
        raise OpenImportCoordinationError("Import-run coordination state is invalid") from None
    if not isinstance(staging_value, str) or not staging_value:
        raise OpenImportCoordinationError("Import-run coordination state is invalid") from None
    if _absolute_lexical(target_value) != target:
        raise OpenImportCoordinationError(
            "Import-run coordination state is contradictory"
        ) from None
    marker = state.get("publication_completed")
    if marker is not None and not isinstance(marker, bool):
        raise OpenImportCoordinationError("Import-run coordination state is invalid") from None
    if marker is True and not (
        state_status in {"finalizing", "finalize_failed", "finalized"}
        or (state_status == "failed" and state.get("failure_reason") == "finalize_failed")
    ):
        raise OpenImportCoordinationError(
            "Import-run coordination state is contradictory"
        ) from None
    if (
        marker is True
        and state_status != "finalized"
        and state.get("finalize_protocol_version") in {1, PUBLICATION_PROTOCOL_VERSION}
    ):
        try:
            verify_published_marker(state)
        except PublicationProofError:
            raise OpenImportCoordinationError(
                "Import-run coordination state is contradictory"
            ) from None
    hint = record.get("can_promote")
    if not isinstance(hint, bool) or hint != _run_can_still_promote(state):
        raise OpenImportCoordinationError(
            "Import-run coordination state is contradictory"
        ) from None


def _validate_terminal_record(
    *,
    record: dict[str, Any],
    run_dir: Path,
    target: Path,
) -> None:
    """Prove from the bounded record alone that a run reached a terminal status.

    The registered record is small and fixed-shape, so it stays readable even when
    the run's own ``run.json`` does not. Proving terminality from it is what lets a
    run whose archive is unreadable stop gating live writes, so the check must be
    exact: canonical terminal status, an explicit non-promotable claim, and both
    paths matching the run being considered.
    """
    raw_target = record.get("target_db_path")
    raw_run = record.get("run_dir")
    if (
        record.get("version") != 1
        or record.get("status") not in {"finalized", "cancelled"}
        or record.get("can_promote") is not False
        or not isinstance(raw_target, str)
        or not raw_target
        or not isinstance(raw_run, str)
        or not raw_run
        or _absolute_lexical(raw_target) != target
        or _absolute_lexical(raw_run) != run_dir
    ):
        raise OpenImportCoordinationError(
            "Import-run coordination state is contradictory"
        ) from None


def _read_unregistered_run(run_dir: Path) -> dict[str, Any] | None:
    """Classify legacy/current state from one descriptor-pinned run.json inode."""
    try:
        with pin_existing_directory(run_dir) as pinned_run:
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            if hasattr(os, "O_NONBLOCK"):
                flags |= os.O_NONBLOCK
            try:
                descriptor = os.open("run.json", flags, dir_fd=pinned_run.descriptor)
            except FileNotFoundError:
                return None
            try:
                metadata = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_size > _MAX_COORDINATION_RECORD_BYTES
                ):
                    raise OSError("Unregistered import state is invalid")
                raw = os.read(descriptor, _MAX_COORDINATION_RECORD_BYTES + 1)
                if len(raw) > _MAX_COORDINATION_RECORD_BYTES:
                    raise OSError("Unregistered import state is invalid")
                state = json.loads(raw)
                if not isinstance(state, dict):
                    raise ValueError("Unregistered import state is invalid")
                current = os.stat(
                    "run.json",
                    dir_fd=pinned_run.descriptor,
                    follow_symlinks=False,
                )
                if not stat.S_ISREG(current.st_mode) or (
                    current.st_dev,
                    current.st_ino,
                    current.st_size,
                    current.st_mtime_ns,
                ) != (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_size,
                    metadata.st_mtime_ns,
                ):
                    raise OSError("Unregistered import state changed")
                if _declares_publication_protocol(state):
                    pinned_run.require_owner_only()
                    if os.name == "posix" and stat.S_IMODE(metadata.st_mode) != 0o600:
                        raise PermissionError("Unregistered import state is not private")
                pinned_run.validate()
                return state
            finally:
                os.close(descriptor)
    except OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError:
        raise OpenImportCoordinationError("Import-run coordination state is invalid") from None


def _declares_publication_protocol(state: dict[str, Any]) -> bool:
    return any(key in state for key in _CURRENT_PROTOCOL_DISCRIMINATOR_KEYS)


def _validate_unregistered_current_state(
    *,
    run_dir: Path,
    state: dict[str, Any],
    target: Path,
) -> dict[str, Any]:
    """Strictly validate a current-protocol sibling omitted from coordination."""
    try:
        version = state.get("finalize_protocol_version")
        if version not in {1, PUBLICATION_PROTOCOL_VERSION}:
            raise ValueError("unknown publication protocol")
        validate_publication_protocol_state(state)
        status = state.get("status")
        target_value = state.get("target_db_path")
        staging_value = state.get("staging_db_path")
        if status not in _KNOWN_RUN_STATUSES:
            raise ValueError("invalid run status")
        if not isinstance(target_value, str) or _absolute_lexical(target_value) != target:
            raise ValueError("invalid target")
        if (
            not isinstance(staging_value, str)
            or _absolute_lexical(staging_value) != _absolute_lexical(run_dir) / "staging.db"
        ):
            raise ValueError("invalid staging database")
        if state.get("publication_completed") is True and status != "finalized":
            verify_published_marker(state)
        return state
    except OpenImportCoordinationError, PublicationProofError, OSError, ValueError:
        raise OpenImportCoordinationError("Import-run coordination state is invalid") from None


def _open_runs_for_target_locked(
    target_db: Path | str,
    runs_dir: Path | str | None = None,
) -> list[Path]:
    """Return every run that can still promote into *target_db*.

    The default sibling run directory is scanned for compatibility with runs created
    before durable coordination existed. New runs also register an owner-only pointer
    beside the target database, allowing custom ``--runs-dir`` layouts to be discovered
    from ``--db`` alone. A malformed coordinated record fails closed: this function is
    called on live WRITE paths, where missing a promotable run would lose staged work.
    """
    from ledger.imports.pdf.files import run_coordination_dir

    target = Path(target_db).expanduser().resolve()
    roots = {
        Path(runs_dir) if runs_dir is not None else target.parent / "import-runs",
    }
    candidates: set[Path] = set()
    coordination_hints: dict[Path, bool | None] = {}
    coordination_records: dict[Path, dict[str, Any]] = {}
    for runs_root in roots:
        if not runs_root.is_dir():
            continue
        candidates.update(
            _absolute_lexical(path)
            for path in runs_root.iterdir()
            if path.is_dir() and not path.is_symlink()
        )

    coordination_dir = run_coordination_dir(target)
    coordination_root = coordination_dir.parent
    if coordination_root.exists() or coordination_root.is_symlink():
        try:
            with pin_existing_directory(coordination_root) as pinned_root:
                pinned_root.require_owner_only()
                try:
                    child_metadata = os.stat(
                        coordination_dir.name,
                        dir_fd=pinned_root.descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    child_metadata = None
                if child_metadata is not None:
                    if not stat.S_ISDIR(child_metadata.st_mode):
                        raise OSError("Import-run coordination registry is invalid")
                    with pinned_root.pin_child_directory(
                        coordination_dir.name
                    ) as pinned_coordination:
                        pinned_coordination.require_owner_only()
                        for record_name in pinned_coordination.child_names(
                            suffix=".json",
                            max_entries=_MAX_COORDINATION_RECORDS,
                        ):
                            record = json.loads(
                                pinned_coordination.read_private_text(
                                    record_name,
                                    max_bytes=_MAX_COORDINATION_RECORD_BYTES,
                                    encoding="utf-8",
                                )
                            )
                            if not isinstance(record, dict):
                                raise TypeError("coordination record is not an object")
                            raw_target = record.get("target_db_path")
                            raw_run = record.get("run_dir")
                            if not isinstance(raw_target, str) or not raw_target:
                                raise ValueError("coordination target is invalid")
                            if not isinstance(raw_run, str) or not raw_run:
                                raise ValueError("coordination run is invalid")
                            recorded_target = _absolute_lexical(raw_target)
                            recorded_run = _absolute_lexical(raw_run)
                            coordinated_can_promote = record.get("can_promote")
                            if recorded_target != target:
                                raise ValueError("coordination target is invalid")
                            if not isinstance(coordinated_can_promote, bool):
                                raise ValueError("coordination status is invalid")
                            candidates.add(recorded_run)
                            coordination_hints[recorded_run] = coordinated_can_promote
                            coordination_records[recorded_run] = record
        except OSError, KeyError, TypeError, json.JSONDecodeError, ValueError:
            raise OpenImportCoordinationError(
                "Import-run coordination registry is unreadable"
            ) from None

    open_runs: list[Path] = []
    for run_dir in sorted(candidates):
        if run_dir in coordination_hints:
            hint = coordination_hints[run_dir]
            try:
                state = _read_coordinated_run(run_dir)
            except OpenImportCoordinationError:
                # The archive is unreadable. Fall back to the bounded record, which
                # is validated independently before it may excuse anything — a
                # contradictory open status cannot exploit a rename-away window.
                #
                # A terminal claim retires this run either way: it may have outlived
                # an intentionally cleaned run dir, or the dir may still be there
                # with an unreadable run.json. run.json doubles as the UNBOUNDED run
                # archive (finalize_tail, the whole review queue, DB-wide
                # post-processing), and that payload scales with the database rather
                # than with a run's PDF count, so ordinary runs can exceed the
                # coordination read cap. Refusing on those bricked every live write
                # permanently, with a remedy — finalize or cancel — that an already
                # terminal run can never satisfy.
                #
                # An OPEN record proves nothing about promotability, so it still
                # fails closed: admitting a writer there could lose staged decisions.
                if hint is False:
                    _validate_terminal_record(
                        record=coordination_records[run_dir],
                        run_dir=run_dir,
                        target=target,
                    )
                    continue
                raise
            _validate_coordinated_state(
                record=coordination_records[run_dir],
                state=state,
                target=target,
            )
        else:
            unregistered_state = _read_unregistered_run(run_dir)
            if unregistered_state is None:
                # Legacy unregistered sibling runs retain the old best-effort behavior.
                continue
            state = unregistered_state
            if _declares_publication_protocol(state):
                state = _validate_unregistered_current_state(
                    run_dir=run_dir,
                    state=state,
                    target=target,
                )
        if not isinstance(state, dict):
            continue
        if not _run_can_still_promote(state):
            continue
        recorded = str(state.get("target_db_path") or "")
        if not recorded:
            continue
        if Path(recorded).expanduser().resolve() == target:
            open_runs.append(run_dir)
    return open_runs


def open_runs_for_target(
    target_db: Path | str,
    runs_dir: Path | str | None = None,
) -> list[Path]:
    """Return promotable runs while serializing any current-protocol proof."""
    from ledger.storage.snapshots import database_target_lock

    with database_target_lock(Path(target_db)):
        return _open_runs_for_target_locked(target_db, runs_dir)


def repair_run_coordination(run_dir: Path | str) -> Path:
    """Register a bounded coordination record for an already-terminal legacy run.

    A run staged before durable coordination existed has no registered record, so
    the guard must fall back to reading its ``run.json`` under the coordination
    cap. That file doubles as the unbounded run archive, so a large one is
    unreadable to the guard and fails closed forever — and the refusal names
    finalize/cancel, which a terminal run cannot do. Writing the record retires it.

    Reading ``run.json`` uncapped here is deliberate and safe: this is an explicit
    operator action naming one run, not the guard's own scan of every candidate.

    Refuses any run that can still promote, so repair can never launder an open
    run into a terminal claim. Returns the path of the record written.
    """
    from ledger.imports.pdf.files import register_run_coordination

    resolved = Path(run_dir).expanduser().resolve()
    run_path = resolved / "run.json"
    try:
        state = json.loads(run_path.read_text(encoding="utf-8"))
    except OSError, UnicodeDecodeError, json.JSONDecodeError:
        raise ValueError(f"Run state is missing or unreadable: {run_path}") from None
    if not isinstance(state, dict):
        raise ValueError(f"Run state is not an object: {run_path}")

    validate_publication_protocol_state(state)
    if _run_can_still_promote(state):
        raise ValueError(
            "Refusing to repair coordination for a run that can still promote. "
            "Finalize or cancel it instead."
        )
    # The reader accepts only these two as terminal, so registering anything else
    # would write a record that still fails closed — a silent no-op repair.
    status = str(state.get("status", ""))
    if status not in {"finalized", "cancelled"}:
        raise ValueError(
            f"Refusing to repair coordination for a run in status {status!r}; "
            "only 'finalized' or 'cancelled' runs are provably terminal."
        )
    if not str(state.get("target_db_path") or ""):
        raise ValueError(f"Run state names no target database: {run_path}")

    record_path = register_run_coordination(resolved, state, can_promote=False)
    if record_path is None:  # pragma: no cover - guarded by the target check above
        raise ValueError(f"Run state names no target database: {run_path}")
    return record_path


def refuse_live_mutation_during_open_import(
    target_db: Path | str,
    *,
    operation: str,
    runs_dir: Path | str | None = None,
) -> None:
    """Fail before a live config/transaction write can strand a promotable run."""
    label = _live_mutation_label(operation)
    try:
        open_runs = open_runs_for_target(target_db, runs_dir)
    except OpenImportCoordinationError:
        raise LiveMutationDuringOpenImportRefused(
            f"Refusing to {label} because import-run coordination is unreadable; "
            f"the write is blocked fail-closed. {UNREADABLE_IMPORT_RUN_REMEDY}"
        ) from None
    _raise_if_open_import_runs(open_runs, label=label)


def _raise_if_open_import_runs(open_runs: list[Path], *, label: str) -> None:
    if not open_runs:
        return
    run_refs = ", ".join(
        hashlib.sha256(str(run_dir).encode("utf-8")).hexdigest()[:12] for run_dir in open_runs
    )
    raise LiveMutationDuringOpenImportRefused(
        f"Refusing to {label} because an import run is open "
        f"(open count: {len(open_runs)}; opaque refs: {run_refs}). "
        f"This live write would change state that an open run "
        f"can still promote over, so staged decisions could be lost. {OPEN_IMPORT_RUN_REMEDY}"
    )


@contextmanager
def live_mutation_boundary(
    target_db: Path | str,
    *,
    operation: str,
    runs_dir: Path | str | None = None,
) -> Iterator[None]:
    """Hold the target publication lock from the open-run recheck through a live write.

    PDF staging uses this same boundary and registers its provisional run before
    releasing it. Consequently a staging baseline and a conflicting live mutation can
    never pass independent point-in-time checks and then overlap.
    """
    from ledger.storage.snapshots import database_target_lock

    with database_target_lock(Path(target_db)):
        label = _live_mutation_label(operation)
        try:
            open_runs = _open_runs_for_target_locked(target_db, runs_dir)
        except OpenImportCoordinationError:
            raise LiveMutationDuringOpenImportRefused(
                f"Refusing to {label} because import-run coordination is unreadable; "
                f"the write is blocked fail-closed. {UNREADABLE_IMPORT_RUN_REMEDY}"
            ) from None
        _raise_if_open_import_runs(open_runs, label=label)
        yield


def _pending_review_count(db_path: Path) -> int | None:
    if not db_path.exists():
        return None
    with closing(sqlite3.connect(db_path)) as conn:
        exists = conn.execute(
            "select 1 from sqlite_master where type = 'table' and name = 'import_review'"
        ).fetchone()
        if not exists:
            return 0
        return int(
            conn.execute(
                """
                select count(*)
                from import_review
                where status = 'pending'
                   or (status = 'resolved' and (merchant = '' or category = ''))
                """
            ).fetchone()[0]
        )


def _statement_validation_gate(run_state: dict[str, Any]) -> str:
    errors = [
        error for item in run_state.get("pdfs", []) for error in item.get("validation_errors", [])
    ]
    if errors:
        return "fail"

    validation_path = run_state.get("statement_validation")
    if not validation_path:
        return "unknown"
    path = Path(validation_path)
    if not path.exists():
        return "unknown"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "fail"
    for item in payload:
        if item.get("errors"):
            return "fail"
    return "pass"


def build_import_readiness(run_dir: Path | str) -> dict[str, Any]:
    run_path = Path(run_dir) / "run.json"
    try:
        run_state = json.loads(run_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        raise FileNotFoundError(f"run.json missing or corrupt in {run_dir}: {exc}") from exc
    status = str(run_state.get("status", "unknown"))
    staging_db = Path(run_state.get("staging_db_path", ""))
    csv_root = Path(run_state.get("csv_root", ""))
    pending = _pending_review_count(staging_db)
    pending_count = (
        pending if pending is not None else int(run_state.get("pending_review_count") or 0)
    )
    validation_gate = _statement_validation_gate(run_state)

    # Use the import-owned gate when available (new runs): runtime-table edits
    # (budgets/goals/salt) do NOT count as drift for the finalize gate.
    # Fall back to whole-file fingerprint for old runs that predate merge-forward.
    io_before = run_state.get("target_db_import_owned_before")
    target_path_str = run_state.get("target_db_path")
    if io_before and target_path_str:
        current_io = import_owned_fingerprint(Path(target_path_str))
        target_ok = import_owned_match(io_before, current_io)
    else:
        target_ok = target_db_unchanged(run_state)
    target_gate = "pass" if target_ok else "fail"
    if status in {"finalized", "cancelled"}:
        target_gate = "not_required"

    gates = {
        "target_db_unchanged": target_gate,
        "staging_db": "pass" if staging_db.exists() or status == "finalized" else "fail",
        "csv_root": "pass" if csv_root.exists() or status == "finalized" else "fail",
        "statement_validation": validation_gate,
        "review": "pass" if pending_count == 0 else "fail",
    }

    # Read-only inspection validates inert pins; it never imports adapter code.
    from ledger.adapters.contracts import AdapterError
    from ledger.adapters.provenance import verify_import_inputs

    binding_error = ""
    if status in {"finalized", "cancelled"} or run_publication_completed(run_state):
        gates["interpretation"] = "not_required"
    else:
        try:
            verify_import_inputs(
                run_state.get("import_binding"),
                csv_root=csv_root,
                source_files=[Path(value) for value in run_state.get("approved_csv_paths", [])],
                account_slugs=run_state.get("account_slugs", {}),
            )
            gates["interpretation"] = "pass"
        except AdapterError, OSError, TypeError, ValueError:
            gates["interpretation"] = "fail"
            binding_error = (
                "Reviewed import inputs or interpretation are unavailable or changed; "
                "use the retained matching installation, or cancel and restage."
            )

    blockers: list[str] = []
    if binding_error:
        blockers.append(binding_error)
    if gates["target_db_unchanged"] == "fail":
        if run_state.get("target_db_fingerprint_before"):
            blockers.append("Target database changed since stage; cancel and restage this import.")
        else:
            blockers.append(
                "Target database fingerprint is missing; cancel and restage this import."
            )
    if status in FINALIZABLE_STATUSES and gates["staging_db"] == "fail":
        blockers.append("Staging database is missing.")
    if status in FINALIZABLE_STATUSES and gates["csv_root"] == "fail":
        blockers.append("Staged CSV root is missing.")
    if gates["statement_validation"] == "fail":
        blockers.append("Statement validation has blocking errors.")
    if pending_count:
        blockers.append(f"{pending_count} review row(s) still need decisions.")

    return {
        "status": status,
        "can_finalize": status in FINALIZABLE_STATUSES and not blockers,
        "gates": gates,
        "blockers": blockers,
        "counts": {"pending_review": pending_count},
        "target_db": {
            "before": run_state.get("target_db_fingerprint_before") or {},
            "current": database_fingerprint(run_state["target_db_path"])
            if run_state.get("target_db_path")
            else {},
        },
    }
