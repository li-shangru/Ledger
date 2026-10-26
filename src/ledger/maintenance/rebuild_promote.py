"""WAL-aware, drift-checked promotion for recovery/admin rebuilds."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from ledger.core.results import RunResult
from ledger.maintenance.rebuild_guard import check_rebuild_safe
from ledger.storage.migrations import invalidate_db_caches, run_migrations, validate_database
from ledger.storage.operations import record_db_operation, utc_now_dt
from ledger.storage.snapshots import (
    DatabaseTargetLockError,
    cleanup_database_artifacts,
    database_target_lock,
    publish_database,
    snapshot_database,
)
from ledger.utils.json_io import write_json_atomic
from ledger.utils.private_files import copy_private_file, ensure_private_dir

RebuildLockError = DatabaseTargetLockError
rebuild_target_lock = database_target_lock


class RebuildDriftError(RuntimeError):
    """Raised when the live target changed after the rebuild began."""


@dataclass(frozen=True)
class RebuildPromotionResult:
    target_path: Path
    backup_path: Path | None
    transactions: int
    transfers: int


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _baseline_snapshot_path(state_path: Path) -> Path:
    return state_path.with_name(f"{state_path.stem}.db")


def capture_rebuild_state(target_db: Path, state_path: Path) -> dict[str, Any]:
    """Capture a private, WAL-complete baseline before the long rebuild starts."""
    target_db = Path(target_db)
    state_path = Path(state_path)
    if target_db.is_symlink():
        raise OSError(f"Rebuild target is a symlink: {target_db}")
    ensure_private_dir(state_path.parent)
    baseline = _baseline_snapshot_path(state_path)
    cleanup_database_artifacts(baseline)
    payload: dict[str, Any] = {
        "schema_version": 1,
        "target": str(target_db.expanduser().resolve()),
        "existed": target_db.exists(),
        "snapshot": "",
        "sha256": "",
    }
    try:
        if target_db.exists():
            snapshot_database(target_db, baseline)
            payload.update({"snapshot": str(baseline), "sha256": _sha256(baseline)})
        write_json_atomic(state_path, payload)
        return payload
    except Exception:
        cleanup_database_artifacts(baseline)
        raise


def _load_rebuild_state(target_db: Path, state_path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Could not read rebuild baseline state: {type(exc).__name__}")
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise RuntimeError("Rebuild baseline state is malformed")
    if payload.get("target") != str(target_db.expanduser().resolve()):
        raise RuntimeError("Rebuild baseline state belongs to a different target")
    existed = payload.get("existed")
    if not isinstance(existed, bool):
        raise RuntimeError("Rebuild baseline state is missing target existence")
    if existed:
        snapshot_value = payload.get("snapshot")
        digest = payload.get("sha256")
        if not isinstance(snapshot_value, str) or not snapshot_value or not isinstance(digest, str):
            raise RuntimeError("Rebuild baseline snapshot metadata is malformed")
        snapshot = Path(snapshot_value)
        if not snapshot.is_file() or _sha256(snapshot) != digest:
            raise RuntimeError("Rebuild baseline snapshot is missing or changed")
    return payload


def _assert_absent_target_unchanged(target_db: Path) -> None:
    if target_db.exists() or target_db.is_symlink():
        raise RebuildDriftError("Target database changed during rebuild; refusing promotion")


def _snapshot_matches_baseline(
    target_db: Path,
    baseline: dict[str, Any],
    observation: Path,
) -> None:
    if not baseline["existed"]:
        _assert_absent_target_unchanged(target_db)
        return
    if not target_db.exists() or target_db.is_symlink():
        raise RebuildDriftError("Target database changed during rebuild; refusing promotion")
    snapshot_database(target_db, observation)
    if _sha256(observation) != baseline["sha256"]:
        raise RebuildDriftError("Target database changed during rebuild; refusing promotion")


def _backup_path(target_db: Path, backup_dir: Path) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    unique = uuid4().hex[:10]
    return backup_dir / f"{target_db.stem}-{stamp}-{unique}-pre-rebuild{target_db.suffix}"


def promote_rebuild(
    candidate_db: Path,
    target_db: Path,
    backup_dir: Path,
    state_path: Path,
) -> RebuildPromotionResult:
    """Validate and atomically promote a quiescent rebuild candidate."""
    candidate_db = Path(candidate_db)
    target_db = Path(target_db)
    backup_dir = Path(backup_dir)
    state_path = Path(state_path)
    baseline = _load_rebuild_state(target_db, state_path)
    if not candidate_db.is_file():
        raise FileNotFoundError(f"Rebuild candidate not found: {candidate_db}")

    ensure_private_dir(target_db.parent)
    temp_target = target_db.parent / f".{target_db.name}.rebuild-{uuid4().hex}.tmp"
    observation = target_db.parent / f".{target_db.name}.rebuild-observe-{uuid4().hex}.db"
    backup: Path | None = None
    published = False
    try:
        with rebuild_target_lock(target_db):
            if baseline["existed"]:
                ensure_private_dir(backup_dir)
                backup = _backup_path(target_db, backup_dir)
                _snapshot_matches_baseline(target_db, baseline, backup)
            else:
                _assert_absent_target_unchanged(target_db)

            copy_private_file(candidate_db, temp_target)
            run_migrations(temp_target)
            safety = check_rebuild_safe(temp_target)
            if not safety.ok:
                raise RuntimeError(safety.message)
            validation = validate_database(temp_target, record=False)
            if not validation.ok:
                raise RuntimeError("; ".join(validation.errors))

            now = utc_now_dt()
            record_db_operation(
                temp_target,
                operation="rebuild_promote",
                result=RunResult(
                    status="success",
                    started_at=now,
                    finished_at=now,
                    counts={
                        "transactions": safety.transactions,
                        "transfers": safety.transfers,
                    },
                    artifacts={
                        "backup": backup or "",
                        "target_db": target_db,
                    },
                ),
                artifact_path=backup,
            )
            post_audit_validation = validate_database(temp_target, record=False)
            if not post_audit_validation.ok:
                raise RuntimeError("; ".join(post_audit_validation.errors))

            cleanup_database_artifacts(observation)
            _snapshot_matches_baseline(target_db, baseline, observation)
            if target_db.is_symlink():
                raise RebuildDriftError(
                    "Target database changed during rebuild; refusing promotion"
                )
            publish_database(temp_target, target_db)
            published = True
            invalidate_db_caches()
            return RebuildPromotionResult(
                target_path=target_db,
                backup_path=backup,
                transactions=safety.transactions,
                transfers=safety.transfers,
            )
    except Exception:
        if backup is not None and not published:
            cleanup_database_artifacts(backup)
        raise
    finally:
        cleanup_database_artifacts(temp_target)
        cleanup_database_artifacts(observation)


def copy_rebuild_exports(source_dir: Path, destination_dir: Path) -> list[Path]:
    """Privately publish top-level derived rebuild outputs, skipping symlinks."""
    source_dir = Path(source_dir)
    destination_dir = ensure_private_dir(Path(destination_dir))
    copied: list[Path] = []
    for source in sorted(source_dir.iterdir()):
        if source.is_symlink() or not source.is_file():
            continue
        target = destination_dir / source.name
        copy_private_file(source, target)
        copied.append(target)
    return copied


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    capture = subparsers.add_parser("capture")
    capture.add_argument("--target", type=Path, required=True)
    capture.add_argument("--state", type=Path, required=True)
    promote = subparsers.add_parser("promote")
    promote.add_argument("--candidate", type=Path, required=True)
    promote.add_argument("--target", type=Path, required=True)
    promote.add_argument("--backup-dir", type=Path, required=True)
    promote.add_argument("--state", type=Path, required=True)
    exports = subparsers.add_parser("copy-exports")
    exports.add_argument("--source", type=Path, required=True)
    exports.add_argument("--destination", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "capture":
            state = capture_rebuild_state(args.target, args.state)
            print("Captured existing target baseline." if state["existed"] else "Target is absent.")
        elif args.command == "promote":
            result = promote_rebuild(args.candidate, args.target, args.backup_dir, args.state)
            if result.backup_path is not None:
                print(f"Existing database backed up: {result.backup_path}")
            print(f"Database rebuilt: {result.target_path}")
        else:
            copied = copy_rebuild_exports(args.source, args.destination)
            print(f"Copied {len(copied)} derived rebuild outputs to: {args.destination}")
    except Exception as exc:
        print(f"rebuild promotion failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
