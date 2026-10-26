"""Audited local database operations."""

from __future__ import annotations

import hmac
import secrets
import sqlite3
from contextlib import suppress
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from ledger.core.results import RunResult
from ledger.storage.database import (
    AccountStatementCreditLimitDB,
    ConfigMetadataDB,
    DBOperationAuditDB,
    init_database,
    session_scope,
)
from ledger.storage.snapshots import has_sqlite_header, snapshot_bound_database, snapshot_database
from ledger.utils.dates import naive_utc_now as utc_now_dt  # re-export; no import cycle
from ledger.utils.json_io import dumps_json_column
from ledger.utils.private_files import PinnedPrivateFile, ensure_private_dir


def record_db_operation(
    db_path: Path,
    *,
    operation: str,
    result: RunResult,
    artifact_path: Path | None = None,
    details: dict[str, Any] | None = None,
    connection: sqlite3.Connection | None = None,
) -> None:
    """Persist a local DB operation audit row."""
    if connection is not None:
        connection.execute(
            """
            INSERT INTO db_operation_audit (
                operation, status, started_at, finished_at, db_path,
                artifact_path, counts, warnings, errors, details
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                operation,
                result.status,
                result.started_at.isoformat(sep=" "),
                result.finished_at.isoformat(sep=" ") if result.finished_at else None,
                str(db_path),
                str(artifact_path) if artifact_path else None,
                dumps_json_column(result.counts),
                dumps_json_column(result.warnings),
                dumps_json_column(result.errors),
                dumps_json_column({**result.details, **(details or {})}),
            ),
        )
        return
    init_database(db_path)
    with session_scope(db_path) as session:
        session.add(
            DBOperationAuditDB(
                operation=operation,
                status=result.status,
                started_at=result.started_at,
                finished_at=result.finished_at,
                db_path=str(db_path),
                artifact_path=str(artifact_path) if artifact_path else None,
                counts=result.counts,
                warnings=result.warnings,
                errors=result.errors,
                details={**result.details, **(details or {})},
            )
        )


def backup_database(
    db_path: Path,
    *,
    backup_dir: Path | None = None,
    reason: str = "manual",
    pinned_source: PinnedPrivateFile | None = None,
    record: bool = True,
) -> RunResult:
    """Create a timestamped SQLite backup and record an audit row."""
    started_at = utc_now_dt()
    warnings: list[str] = []
    errors: list[str] = []
    artifact: Path | None = None

    try:
        if pinned_source is None and not db_path.exists():
            raise FileNotFoundError(f"Database not found: {db_path}")
        if pinned_source is None and not has_sqlite_header(db_path):
            raise ValueError(
                f"Snapshot source does not have a valid SQLite database header: {db_path}"
            )

        target_dir = ensure_private_dir(backup_dir or db_path.parent / "db-backups")
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        suffix = db_path.suffix or ".db"
        unique = uuid4().hex[:8]
        artifact = target_dir / f"{db_path.stem}-{stamp}-{unique}-{reason}{suffix}"

        if pinned_source is None:
            snapshot_database(db_path, artifact)
        else:
            snapshot_bound_database(
                pinned_source,
                artifact,
                label="Budget pre-write backup source",
                require_main_owner_only=True,
                require_parent_owner_only=True,
            )

        result = RunResult(
            status="success",
            started_at=started_at,
            finished_at=utc_now_dt(),
            counts={"bytes": artifact.stat().st_size},
            warnings=warnings,
            artifacts={"backup": artifact},
            details={"reason": reason},
        )
    except Exception as exc:
        errors.append(str(exc))
        result = RunResult(
            status="failed",
            started_at=started_at,
            finished_at=utc_now_dt(),
            warnings=warnings,
            errors=errors,
            details={"reason": reason},
        )

    if record and db_path.exists() and has_sqlite_header(db_path):
        try:
            record_db_operation(db_path, operation="backup", result=result, artifact_path=artifact)
        except Exception:
            if result.ok:
                raise
    return result


# DB-file glob patterns that carry raw financial data and must be owner-only.
_SENSITIVE_DB_GLOBS = ("*.db", "*.db-*", "*.db.backup-*")


def tighten_db_permissions(data_dir: Path) -> list[Path]:
    """One-shot sweep: chmod every SQLite DB file under *data_dir* to 0o600 and
    every db-backups/ directory to 0o700.

    init_database() sets 0o600 on NEW files and backup_database() locks fresh
    snapshots, but files written before those guards landed (the live DB and the
    legacy data/*.db.backup-* / db-backups/*.db snapshots) are still 0o644 —
    world-readable. This idempotent sweep tightens them. Best-effort per file:
    an OSError on a network/FAT mount is skipped, not fatal.
    """
    touched: list[Path] = []
    if data_dir.is_symlink() or not data_dir.exists():
        return touched
    # Tighten any db-backups directory first.
    for backups_dir in data_dir.rglob("db-backups"):
        if not backups_dir.is_symlink() and backups_dir.is_dir():
            with suppress(OSError):
                backups_dir.chmod(0o700)
    seen: set[Path] = set()
    for pattern in _SENSITIVE_DB_GLOBS:
        for path in data_dir.rglob(pattern):
            if path.is_symlink() or not path.is_file() or path in seen:
                continue
            seen.add(path)
            with suppress(OSError):
                path.chmod(0o600)
                touched.append(path)
    return touched


# config_metadata key under which the per-DB HMAC salt is persisted locally.
# Generated once with secrets and reused by every consumer so an account keeps a
# stable opaque reference. NEVER written into an export artifact or onto a wire.
ACCOUNT_REF_SALT_KEY = "export_account_ref_salt"


def read_account_ref_salt(db_path: Path) -> str | None:
    """Return the persisted account-ref salt, or None when it was never minted.

    Deliberately read-only: a consumer that cannot write (the read-only MCP
    server) uses this and fails closed on None rather than falling back to raw
    account slugs. Seed one first with ``ledger config ensure-account-salt``.
    """
    with session_scope(db_path) as session:
        row = session.get(ConfigMetadataDB, ACCOUNT_REF_SALT_KEY)
        return str(row.value) if row is not None else None


def ensure_account_ref_salt(db_path: Path) -> str:
    """Return the per-DB account-ref salt, generating + persisting it once.

    The salt is a random secret stored LOCALLY in config_metadata and reused
    across every derivation so the ref stays stable for the same account. An
    existing salt is never replaced: rotating it would silently change every
    reference already handed out (and every historical export).
    """
    init_database(db_path)
    with session_scope(db_path) as session:
        row = session.get(ConfigMetadataDB, ACCOUNT_REF_SALT_KEY)
        if row is None:
            salt = secrets.token_hex(32)
            session.add(ConfigMetadataDB(key=ACCOUNT_REF_SALT_KEY, value=salt))
        else:
            salt = str(row.value)
    return salt


def account_ref(salt: str, institution: str, account: str, account_type: str) -> str:
    """Stable, salted, opaque account reference.

    The hash portion is an HMAC keyed by the per-DB random salt, so it cannot be
    reconstructed from institution|account_type|last4 without the salt (defeating
    a brute-force over institution x 10^4). No cleartext identity prefix is
    exposed: callers receive only the public ``acct`` namespace and the truncated
    lowercase HMAC. The ref is stable within a database but differs across
    databases, so it cannot be correlated by a third party.
    """
    digest = hmac.new(
        salt.encode(),
        f"{institution}|{account}|{account_type}".encode(),
        "sha256",
    ).hexdigest()
    return f"acct-{digest[:10]}"


def upsert_statement_credit_limit(
    db_path: Path,
    account_slug: str,
    credit_limit: Decimal,
    statement_date: date,
) -> None:
    """Store (or replace) the printed credit limit for one statement.

    Keyed by (account_slug, statement_date): re-importing the same statement
    updates the value in place, while distinct statement dates accumulate so the
    latest can be read back. ``account_slug`` is the canonical, alias-normalized parser
    account identity (legacy/manual CSV imports may still derive it from the CSV stem).
    """
    init_database(db_path)
    with session_scope(db_path) as session:
        session.merge(
            AccountStatementCreditLimitDB(
                account_slug=account_slug,
                statement_date=statement_date,
                credit_limit=credit_limit,
                updated_at=utc_now_dt(),
            )
        )


def get_latest_statement_credit_limit(db_path: Path, account_slug: str) -> Decimal | None:
    """Return the printed credit limit from the most recent statement for a slug.

    Latest is by ``statement_date``. Returns ``None`` when no limit is stored so
    callers fall back to the deepest-balance estimate.

    For a figure attached to a specific DAY -- a utilization percentage, say --
    use :func:`get_statement_credit_limit_history` instead. Every card in a real
    ledger changes limit, and dividing a historical balance by today's limit is
    simply a different number.
    """
    init_database(db_path)
    with session_scope(db_path) as session:
        row = (
            session.query(AccountStatementCreditLimitDB)
            .filter_by(account_slug=account_slug)
            .order_by(AccountStatementCreditLimitDB.statement_date.desc())
            .first()
        )
        return row.credit_limit if row is not None else None


def get_statement_credit_limit_history(
    db_path: Path,
    account_slug: str,
) -> list[tuple[date, Decimal]]:
    """Return every printed credit limit for a slug, oldest statement first.

    The table is keyed by (account_slug, statement_date) precisely so this history
    is retained. A per-day figure must divide by the limit that was in force on
    that day. Applying the newest limit to earlier dates would misstate
    historical utilization whenever the limit increased or decreased.
    """
    init_database(db_path)
    with session_scope(db_path) as session:
        return [
            (row.statement_date, row.credit_limit)
            for row in session.query(AccountStatementCreditLimitDB)
            .filter_by(account_slug=account_slug)
            .order_by(AccountStatementCreditLimitDB.statement_date.asc())
        ]


def credit_limit_in_force(
    history: list[tuple[date, Decimal]],
    on_date: date,
) -> Decimal | None:
    """Return the printed limit in force on ``on_date``, or None if unknown.

    A statement prints the limit that applied over the period it COVERS, so the
    figure for a given day is the one on the earliest statement dated on or after
    that day -- looking forward, not back. A statement dated 31 May states May's
    limit; reading backwards would leave every day before the first statement
    unattributed and shift each change a full cycle late.

    Past the last statement the most recent figure carries forward, which is the
    best available claim. With no statements at all the limit is unknown, and
    unknown stays None: a utilization computed from a guessed denominator is a
    guess with a percent sign on it.
    """
    for statement_date, limit in history:
        if statement_date >= on_date:
            return limit
    return history[-1][1] if history else None


def recent_db_operations(db_path: Path, limit: int = 20) -> list[dict[str, Any]]:
    init_database(db_path)
    with session_scope(db_path) as session:
        rows = (
            session.query(DBOperationAuditDB)
            .order_by(DBOperationAuditDB.started_at.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "Started At": row.started_at,
                "Operation": row.operation,
                "Status": row.status,
                "Artifact": row.artifact_path or "",
                "Warnings": len(row.warnings or []),
                "Errors": len(row.errors or []),
            }
            for row in rows
        ]
