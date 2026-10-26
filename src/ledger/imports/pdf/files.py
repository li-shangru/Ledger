"""Filesystem, JSON, hashing, and promotion helpers for PDF imports."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX host (Windows)
    fcntl = None  # type: ignore[assignment]

from ledger.imports.pdf.manifest import write_manifest
from ledger.storage.database import ProcessedFileDB, init_database, session_scope
from ledger.storage.migrations import invalidate_db_caches
from ledger.storage.snapshots import (
    bound_database_read_connection,
    cleanup_database_artifacts,
    database_target_lock,
    publish_database,
    snapshot_bound_database,
    snapshot_database,
)
from ledger.storage.writer import DatabaseWriter
from ledger.utils.json_io import (  # noqa: F401  (re-exported)
    decimal_json,
    utc_now,
    write_json_atomic,
)
from ledger.utils.private_files import (
    copy_private_file,
    ensure_private_dir,
    pin_existing_directory,
    pin_private_directory,
    write_private_text_atomic,
)


class FinalizeLockError(RuntimeError):
    """Raised when one import run cannot be locked for finalization."""


def write_json(path: Path, payload: Any) -> None:
    # All JSON state writes go through the atomic temp+os.replace helper so a
    # crash or concurrent reader never sees a half-written file (final-report.json,
    # merchant_review.json, validation_report.json all flow through here).
    write_json_atomic(path, payload)


_RUN_COORDINATION_DIR = ".ledger-import-coordination"


def run_coordination_dir(target_db: Path | str) -> Path:
    """Return the owner-only registry directory for runs targeting one database.

    A target-path hash keeps multiple ledgers in the same parent directory separate
    without putting the database path in a filename. The registry itself lives beside
    the target, so a live mutation surface that knows only ``--db`` can discover runs
    staged under a custom ``--runs-dir`` (D23).
    """
    target = Path(target_db).expanduser().resolve()
    target_key = hashlib.sha256(str(target).encode("utf-8")).hexdigest()
    return target.parent / _RUN_COORDINATION_DIR / target_key


def register_run_coordination(
    run_dir: Path,
    run_state: dict[str, Any],
    *,
    can_promote: bool,
) -> Path | None:
    """Write the bounded coordination record for one run.

    Returns the record path, or None when the state names no target database.
    """
    from ledger.imports.pdf.readiness import _MAX_COORDINATION_RECORD_BYTES

    target_value = run_state.get("target_db_path")
    if not target_value:
        return None
    target = Path(str(target_value)).expanduser().resolve()
    resolved_run = run_dir.expanduser().resolve()
    coordination_dir = ensure_private_dir(run_coordination_dir(target))
    run_key = hashlib.sha256(str(resolved_run).encode("utf-8")).hexdigest()
    payload = json.dumps(
        {
            "version": 1,
            "target_db_path": str(target),
            "run_dir": str(resolved_run),
            "status": str(run_state.get("status", "")),
            "can_promote": can_promote,
        },
        indent=2,
    )
    # This record is the guard's bounded fallback when run.json — which doubles as
    # the unbounded run archive — is too large to read. A record the reader would
    # itself refuse would silently recreate that dead end, so never write one.
    if len(payload.encode("utf-8")) > _MAX_COORDINATION_RECORD_BYTES:
        raise ValueError(
            "Refusing to write a coordination record larger than the read cap "
            f"({_MAX_COORDINATION_RECORD_BYTES} bytes)."
        )
    record_path = coordination_dir / f"{run_key}.json"
    write_private_text_atomic(record_path, payload, encoding="utf-8")
    return record_path


def write_run_state(run_dir: Path, run_state: dict[str, Any]) -> None:
    from ledger.imports.pdf.readiness import (
        _run_can_still_promote,
        validate_publication_protocol_state,
    )

    validate_publication_protocol_state(run_state)
    can_promote = _run_can_still_promote(run_state)
    if can_promote:
        # Publish an OPEN pointer first. If the process dies before run.json is
        # replaced, live mutation fails closed on the registered but unreadable run
        # instead of silently missing a custom --runs-dir.
        register_run_coordination(run_dir, run_state, can_promote=True)
        write_json_atomic(run_dir / "run.json", run_state)
    else:
        # Publish terminal state first, then mark its pointer terminal. A crash between
        # these writes leaves an open pointer whose terminal run.json is still safe to
        # inspect. Once both land, later cleanup may remove the retained run directory
        # without leaving a stale pointer that blocks live edits forever.
        write_json_atomic(run_dir / "run.json", run_state)
        register_run_coordination(run_dir, run_state, can_promote=False)
    write_manifest(run_dir, run_state)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def db_counts(db_path: Path) -> dict[str, int]:
    writer = DatabaseWriter(db_path)
    stats = writer.get_stats()

    def as_int(value: Any) -> int:
        return int(value or 0)

    return {
        "transactions": as_int(stats["total_transactions"]),
        "transfers": as_int(stats["total_transfers"]),
        "import_runs": as_int(stats["import_runs"]),
    }


def known_pdf_hashes(db_path: Path) -> set[str]:
    DatabaseWriter(db_path)
    with session_scope(db_path) as session:
        return {row.file_hash for row in session.query(ProcessedFileDB.file_hash).all()}


def clone_database(source: Path | None, target: Path) -> None:
    ensure_private_dir(target.parent)
    if source is not None:
        snapshot_database(source, target)
        return
    if target.is_symlink():
        raise OSError(f"Database destination is a symlink: {target}")
    if target.exists():
        target.unlink()
    init_database(target)


def promote_database(
    staging_db: Path,
    target_db: Path,
    backup_dir: Path,
    *,
    expected_fingerprint: dict | None = None,
    expected_import_owned: dict | None = None,
    expected_candidate_import_owned: dict | None = None,
    expected_preserved_tables: dict | None = None,
    prepublication_check: Callable[[], None] | None = None,
    label: str = "pre-finalize",
    rollback_path: Path | None = None,
) -> Path | None:
    """Promote *staging_db* onto *target_db* atomically.

    Sequence:
      1. online-snapshot staging.db (including retained WAL) → temp_target
      2. merge_forward(live=target_db, into=temp_target)   ← NEW
      3. Re-check drift (import-owned tables only) via *expected_import_owned*.
         If *expected_import_owned* is supplied, compare it against the CURRENT
         target's import-owned fingerprint.  A change confined to MERGE_FORWARD_TABLES
         is invisible to this check (that is the point).  A change to any import-owned
         table (transactions, import_runs, etc.) aborts the promote.
      4. publish temp_target through SQLite's WAL-aware backup API
      5. verify the published target against *expected_candidate_import_owned*

    Legacy *expected_fingerprint* (whole-file) is still accepted for callers that
    have not yet been migrated, but the import path now uses *expected_import_owned*.
    If BOTH are supplied, *expected_import_owned* takes precedence for the recheck.
    *expected_candidate_import_owned* is the separately persisted provenance for the
    staged logical candidate; when supplied, the exact bound snapshot must match it
    before publication and the WAL-aware published target must match it afterward.

    *label* names the pre-promote rollback backup so its filename records what run
    type produced it: "pre-finalize" for a normal finalize (base DB == target), or
    "pre-fresh-rerun" for a fresh rerun rebuilt from a separate base DB.

    CRITICAL: merge_forward writes into *temp_target*, NEVER into *staging_db*.
    """
    with database_target_lock(target_db):
        return _promote_database_locked(
            staging_db,
            target_db,
            backup_dir,
            expected_fingerprint=expected_fingerprint,
            expected_import_owned=expected_import_owned,
            expected_candidate_import_owned=expected_candidate_import_owned,
            expected_preserved_tables=expected_preserved_tables,
            prepublication_check=prepublication_check,
            label=label,
            rollback_path=rollback_path,
            publication_token=None,
            on_published=None,
        )


def _promote_database_locked(
    staging_db: Path,
    target_db: Path,
    backup_dir: Path,
    *,
    expected_fingerprint: dict | None,
    expected_import_owned: dict | None,
    expected_candidate_import_owned: dict | None = None,
    expected_preserved_tables: dict | None,
    label: str,
    rollback_path: Path | None,
    publication_token: str | None = None,
    on_published: Callable[[dict[str, int]], None] | None = None,
    prepublication_check: Callable[[], None] | None = None,
) -> Path | None:
    from ledger.imports.pdf.readiness import (
        PUBLICATION_TOKEN_METADATA_KEY,
        PublicationProofError,
        database_fingerprint,
        fingerprints_match,
        import_owned_fingerprint,
        import_owned_match,
        investment_preserved_fingerprint,
        investment_preserved_match,
        is_sqlite_db,
        merge_forward,
        merge_forward_contract_matches,
    )

    if target_db.is_symlink():
        raise OSError(f"Database destination is a symlink: {target_db}")
    backup_path = None
    ensure_private_dir(target_db.parent)
    if target_db.exists():
        ensure_private_dir(backup_dir)
        if rollback_path is not None:
            backup_path = Path(rollback_path)
            if backup_path.parent.resolve() != backup_dir.resolve():
                raise ValueError("Rollback backup must be inside the configured backup directory")
        else:
            # Whole-second stamp alone can collide if two promotes of the same target run in
            # the same second, silently overwriting the only rollback artifact. Add a
            # pid+uuid suffix (mirrors the temp_target naming below) to guarantee uniqueness.
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            unique = f"{os.getpid()}-{uuid4().hex[:8]}"
            backup_path = (
                backup_dir / f"{target_db.stem}-{stamp}-{unique}-{label}{target_db.suffix}"
            )
        snapshot_database(target_db, backup_path)

    temp_target = target_db.with_name(f".{target_db.name}.promote-{os.getpid()}-{uuid4().hex}.tmp")
    merge_forward_contract: dict[str, Any] | None = None
    try:
        if expected_candidate_import_owned is None:
            snapshot_database(staging_db, temp_target)
        else:
            try:
                snapshot_bound_database(
                    staging_db,
                    temp_target,
                    label="Publication candidate",
                    require_main_owner_only=True,
                    require_parent_owner_only=True,
                )
                frozen_candidate = import_owned_fingerprint(temp_target)
                if not import_owned_match(
                    frozen_candidate,
                    expected_candidate_import_owned,
                ):
                    raise PublicationProofError(
                        "Publication recovery proof is unavailable"
                    ) from None
            except PublicationProofError:
                raise
            except Exception:
                raise PublicationProofError("Publication recovery proof is unavailable") from None

        # Merge live runtime tables (budgets/goals/salt) FORWARD into the temp copy.
        # This preserves any concurrent UI edits to those tables without aborting.
        # NEVER merge into staging_db — only into temp_target.
        # Guard: only merge when the target is a real SQLite DB (not a raw binary blob
        # or absent).  The SQLite magic header starts with b"SQLite format 3".
        if target_db.exists() and is_sqlite_db(target_db):
            completed_merge = merge_forward(live_db=target_db, into_db=temp_target)
            if expected_import_owned is not None and expected_preserved_tables is None:
                merge_forward_contract = completed_merge

        if publication_token is not None:
            if len(publication_token) != 32 or any(
                char not in "0123456789abcdef" for char in publication_token
            ):
                raise ValueError("Publication token is invalid")
            with (
                contextlib.closing(sqlite3.connect(temp_target)) as candidate_connection,
                candidate_connection,
            ):
                candidate_connection.execute(
                    "insert or replace into config_metadata (key, value) values (?, ?)",
                    (PUBLICATION_TOKEN_METADATA_KEY, publication_token),
                )

        if expected_candidate_import_owned is not None and not import_owned_match(
            import_owned_fingerprint(temp_target),
            expected_candidate_import_owned,
        ):
            raise PublicationProofError("Publication recovery proof is unavailable") from None

        # Reject already-observed drift early. For table-aware callers the
        # authoritative copies of these checks also run on the write-excluded
        # destination connection immediately before publication.
        if expected_preserved_tables is not None:
            current_preserved = investment_preserved_fingerprint(target_db)
            if not investment_preserved_match(expected_preserved_tables, current_preserved):
                raise RuntimeError(
                    "target DB changed during finalize; aborting promote to preserve "
                    "non-investment state"
                )
        if expected_import_owned is not None:
            current_owned = import_owned_fingerprint(target_db)
            if not import_owned_match(expected_import_owned, current_owned):
                raise RuntimeError(
                    "target DB changed during finalize; aborting promote to avoid losing concurrent edits"
                )
        elif expected_fingerprint is not None:
            # Legacy whole-file check (kept for backward-compat with callers that
            # have not yet been updated to pass expected_import_owned).
            current = database_fingerprint(target_db)
            if not fingerprints_match(expected_fingerprint, current):
                raise RuntimeError(
                    "target DB changed during finalize; aborting promote to avoid losing concurrent edits"
                )

        if expected_preserved_tables is not None or expected_import_owned is not None:

            def guard(live_connection: sqlite3.Connection) -> None:
                if expected_preserved_tables is not None:
                    current_preserved = investment_preserved_fingerprint(live_connection)
                    if not investment_preserved_match(expected_preserved_tables, current_preserved):
                        raise RuntimeError(
                            "target DB changed during finalize; aborting promote to preserve "
                            "non-investment state"
                        )
                if expected_import_owned is not None:
                    current_owned = import_owned_fingerprint(live_connection)
                    if not import_owned_match(expected_import_owned, current_owned):
                        raise RuntimeError(
                            "target DB changed during finalize; aborting promote to avoid "
                            "losing concurrent edits"
                        )
                if merge_forward_contract is not None:
                    if not merge_forward_contract_matches(
                        merge_forward_contract,
                        live_connection,
                    ):
                        raise RuntimeError(
                            "target DB changed during finalize; aborting promote to avoid "
                            "losing concurrent runtime edits"
                        )

            publish_database(
                temp_target,
                target_db,
                guard=guard,
                prepublication_check=prepublication_check,
            )
        else:
            publish_database(temp_target, target_db, prepublication_check=prepublication_check)
        if expected_candidate_import_owned is not None:
            try:
                with (
                    pin_existing_directory(target_db.parent) as target_parent,
                    target_parent.pin_private_file(target_db.name) as target_file,
                    bound_database_read_connection(
                        target_file,
                        label="Published target",
                        require_main_owner_only=True,
                        require_parent_owner_only=True,
                    ) as published_connection,
                ):
                    target_parent.require_owner_only()
                    published_candidate = import_owned_fingerprint(published_connection)
                    if not import_owned_match(
                        published_candidate,
                        expected_candidate_import_owned,
                    ):
                        raise PublicationProofError(
                            "Publication recovery proof is unavailable"
                        ) from None
                    if publication_token is not None:
                        stored_token = published_connection.execute(
                            "select value from config_metadata where key = ?",
                            (PUBLICATION_TOKEN_METADATA_KEY,),
                        ).fetchone()
                        if stored_token != (publication_token,):
                            raise PublicationProofError(
                                "Publication recovery proof is unavailable"
                            ) from None
                    if on_published is not None:
                        if publication_token is None:
                            raise PublicationProofError(
                                "Publication recovery proof is unavailable"
                            ) from None
                        on_published(
                            {
                                "dev": target_file.identity[0],
                                "ino": target_file.identity[1],
                            }
                        )
                    target_file.validate()
            except PublicationProofError:
                raise
            except Exception:
                raise PublicationProofError("Publication recovery proof is unavailable") from None
    except Exception:
        cleanup_database_artifacts(temp_target)
        raise
    cleanup_database_artifacts(temp_target)
    invalidate_db_caches()
    return backup_path


def latest_run_file(runs_dir: Path) -> Path:
    return runs_dir / "latest-run.txt"


def clear_latest_run_file(runs_dir: Path, run_dir: Path) -> None:
    with _runs_lock(runs_dir), pin_private_directory(runs_dir) as pinned:
        try:
            raw_target = pinned.read_text_if_exists("latest-run.txt", encoding="utf-8")
        except OSError:
            return
        if raw_target is None:
            return
        latest_target = Path(raw_target.strip()).expanduser().resolve()
        if latest_target == run_dir.resolve():
            pinned.unlink("latest-run.txt")
            os.fsync(pinned.descriptor)
            pinned.validate()


@contextlib.contextmanager
def _runs_lock(runs_dir: Path):
    """Exclusive flock over the import-runs dir for the create+pointer critical section.

    fcntl is POSIX-only; on a non-POSIX host (fcntl is None) this degrades to a no-op
    lock, matching the project's other os.name == "posix" guarded branches.
    """
    ensure_private_dir(runs_dir)
    if fcntl is None:  # pragma: no cover - non-POSIX host
        yield
        return
    lock_path = runs_dir / ".lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    if os.name == "posix":
        os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "r+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def make_run_dir(runs_dir: Path) -> Path:
    with _runs_lock(runs_dir):
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        run_dir = runs_dir / run_id
        suffix = 2
        while run_dir.exists():
            run_dir = runs_dir / f"{run_id}-{suffix}"
            suffix += 1
        ensure_private_dir(run_dir)
        # CAS-style pointer write: only advance latest-run.txt under the same lock so a
        # concurrent make_run_dir / finalize can't interleave a stale pointer.
        write_private_text_atomic(latest_run_file(runs_dir), str(run_dir), encoding="utf-8")
        return run_dir


def resolve_run_dir(runs_dir: Path, run_dir: Path | None) -> Path:
    if run_dir:
        return run_dir.expanduser().resolve()

    latest = latest_run_file(runs_dir)
    if not latest.exists():
        raise SystemExit(f"No latest run file found: {latest}")
    return Path(latest.read_text(encoding="utf-8").strip()).expanduser().resolve()


@contextlib.contextmanager
def finalize_run_lock(runs_dir: Path, run_dir: Path):
    """Nonblocking private lock serializing one import run's finalization."""
    runs_root = runs_dir.expanduser().resolve()
    resolved_run = run_dir.expanduser().resolve()
    try:
        relative_run = resolved_run.relative_to(runs_root)
    except ValueError:
        raise FinalizeLockError(
            "Import run is outside the configured import-runs directory."
        ) from None
    if relative_run == Path("."):
        raise FinalizeLockError("Import run is outside the configured import-runs directory.")
    if not resolved_run.is_dir():
        raise FinalizeLockError("Import run directory was not found.")

    ensure_private_dir(resolved_run)
    if fcntl is None:  # pragma: no cover - non-POSIX host
        yield
        return

    lock_path = resolved_run / ".finalize.lock"
    if lock_path.is_symlink():
        raise FinalizeLockError("Import run finalize lock is a symlink.")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(lock_path, flags, 0o600)
    if os.name == "posix":
        os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "r+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise FinalizeLockError(
                "Finalize is already in progress for this import run."
            ) from None
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def link_or_copy_pdf(source: Path, target_dir: Path) -> Path:
    ensure_private_dir(target_dir)
    target = target_dir / source.name
    if target.exists() or target.is_symlink():
        target.unlink()
    try:
        target.symlink_to(source.resolve())
    except OSError:
        copy_private_file(source, target)
    return target
