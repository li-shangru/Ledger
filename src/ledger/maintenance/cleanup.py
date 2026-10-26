"""Safe cleanup for local Ledger data artifacts."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from ledger.core.results import RunResult
from ledger.storage.database import ExportRunDB, ImportRunDB, init_database, session_scope
from ledger.storage.operations import backup_database, record_db_operation
from ledger.storage.operations import utc_now_dt as utc_now
from ledger.utils.private_files import ensure_private_dir, tighten_private_file

# Import-run dirs whose work is done are safe to prune (keeping the newest one). An
# in-flight run (staged / needs_review / ready_to_finalize / finalizing) is NEVER pruned.
_PRUNABLE_RUN_STATES = {"finalized", "cancelled", "failed"}


class DataCleanupService:
    """Plan and execute conservative cleanup for the local data directory."""

    def __init__(self, db_path: Path, data_dir: Path | None = None):
        lexical_db_path = self._absolute_lexical(db_path)
        requested_data_dir = data_dir if data_dir is not None else lexical_db_path.parent
        self.data_dir = self._absolute_lexical(requested_data_dir)
        self._reject_symlink_components(self.data_dir)
        self._reject_unbounded_data_dir(self.data_dir, explicit=data_dir is not None)
        self.db_path = db_path.expanduser().resolve()
        init_database(self.db_path)

    @staticmethod
    def _reject_unbounded_data_dir(data_dir: Path, *, explicit: bool) -> None:
        """Refuse to treat a home or filesystem root as the ledger data directory.

        The default scope is the database's parent. A database directly in a
        home directory would make cleanup walk unrelated personal files, so
        refuse broad roots even when the database itself is valid. Cleanup
        only operates on a directory dedicated to application state.
        """
        home = Path.home().resolve()
        if data_dir == home or data_dir == data_dir.anchor or data_dir.parent == data_dir:
            hint = (
                "Point --db at a database inside a managed data directory"
                if not explicit
                else "Pass a --data-dir inside the managed data directory"
            )
            raise ValueError(
                f"Refusing to clean {data_dir}: it is a home or filesystem root, not a "
                f"ledger data directory. {hint}."
            )

    def dry_run(self) -> RunResult:
        return self.clean(execute=False)

    def clean(self, *, execute: bool = False) -> RunResult:
        started_at = utc_now()
        filesystem = self._filesystem_targets()
        metadata = self._metadata_targets()
        details: dict[str, Any] = {
            "mode": "execute" if execute else "dry_run",
            "data_dir": str(self.data_dir),
            "delete_roots": [str(path) for path in filesystem["delete_roots"]],
            "filesystem_targets": [str(path) for path in filesystem["display_targets"]],
            "stale_import_run_ids": metadata["import_run_ids"],
            "stale_export_run_ids": metadata["export_run_ids"],
            "ensured_dirs": [
                str(self.data_dir / "inbox"),
                str(self.data_dir / "import-runs"),
                str(self.data_dir / "processed"),
            ],
        }
        counts = {
            "filesystem_targets": len(filesystem["display_targets"]),
            "delete_roots": len(filesystem["delete_roots"]),
            "stale_import_runs": len(metadata["import_run_ids"]),
            "stale_export_runs": len(metadata["export_run_ids"]),
            "backups_to_prune": len(filesystem["backup_targets"]),
            "audit_runs_to_prune": len(filesystem["audit_run_targets"]),
            "import_runs_to_prune": len(filesystem["import_run_targets"]),
            "orphan_root_backups": len(self._orphan_root_backups()),
        }

        if not execute:
            return RunResult(
                status="success",
                started_at=started_at,
                finished_at=utc_now(),
                counts=counts,
                details=details,
            )

        backup = backup_database(
            self.db_path,
            backup_dir=self.data_dir / "db-backups",
            reason="cleanup",
        )
        if not backup.ok:
            failed_result = RunResult(
                status="failed",
                started_at=started_at,
                finished_at=utc_now(),
                counts=counts,
                warnings=backup.warnings,
                errors=backup.errors,
                details=details,
            )
            with contextlib.suppress(Exception):
                record_db_operation(
                    self.db_path,
                    operation="data_cleanup",
                    result=failed_result,
                )
            return failed_result

        retained_backup = self._absolute_lexical(Path(str(backup.artifacts["backup"])))
        # Execute deletes EXACTLY the set the dry-run reported (filesystem["delete_roots"]),
        # which already includes the backup-pruning decision from _backup_targets(). We only
        # subtract the freshly created pre-cleanup backup as a safety belt: it post-dates the
        # dry-run glob, so it is not in delete_roots, but guarding here guarantees the backup
        # the run just took is never deleted even if a future _backup_targets() change would
        # otherwise list a same-named path.
        delete_roots = [
            path
            for path in filesystem["delete_roots"]
            if self._absolute_lexical(path) != retained_backup
        ]

        try:
            # Relocate orphan root backups into db-backups/ BEFORE deleting, so they enter
            # the managed retention + chmod path rather than lingering as un-tracked PII.
            relocated = self._relocate_orphan_backups()
            deleted_paths = self._delete_paths(delete_roots)
            self._ensure_directories()
            pruned = self._prune_metadata(metadata)
            self._vacuum()
        except Exception as exc:
            # A destructive step failed mid-way. Write a FAILED audit row best-effort
            # (the retained backup is still on disk so recovery is possible), then
            # re-raise so the caller knows something went wrong.
            failed_counts = {**counts, "deleted_paths": 0}
            failed_details = {**details, "retained_backup": str(retained_backup)}
            failed_result = RunResult(
                status="failed",
                started_at=started_at,
                finished_at=utc_now(),
                counts=failed_counts,
                artifacts={"backup": retained_backup},
                errors=[str(exc)],
                details=failed_details,
            )
            with contextlib.suppress(Exception):
                record_db_operation(
                    self.db_path,
                    operation="data_cleanup",
                    result=failed_result,
                    artifact_path=retained_backup,
                )
            raise

        counts.update(
            {
                "deleted_paths": len(deleted_paths),
                "pruned_import_runs": pruned["import_runs"],
                "pruned_export_runs": pruned["export_runs"],
                "relocated_orphan_backups": len(relocated),
            }
        )
        details.update(
            {
                "deleted_paths": [str(path) for path in deleted_paths],
                "retained_backup": str(retained_backup),
                "relocated_orphan_backups": [str(path) for path in relocated],
            }
        )
        result = RunResult(
            status="success",
            started_at=started_at,
            finished_at=utc_now(),
            counts=counts,
            artifacts={"backup": retained_backup},
            details=details,
        )
        record_db_operation(
            self.db_path,
            operation="data_cleanup",
            result=result,
            artifact_path=retained_backup,
        )
        return result

    def _filesystem_targets(self) -> dict[str, list[Path]]:
        delete_roots: list[Path] = []
        display_targets: list[Path] = []

        smoke_dir = self.data_dir / "exports-smoke"
        if smoke_dir.is_symlink():
            delete_roots.append(smoke_dir)
            display_targets.append(smoke_dir)
        elif smoke_dir.exists():
            delete_roots.append(smoke_dir)
            display_targets.append(smoke_dir)
            display_targets.extend(sorted(path for path in smoke_dir.rglob("*") if path.is_file()))

        old_top_level_backup = self.data_dir / "ledger-backup.db"
        if old_top_level_backup.is_symlink() or old_top_level_backup.exists():
            delete_roots.append(old_top_level_backup)
            display_targets.append(old_top_level_backup)

        junk_files = sorted(self.data_dir.rglob(".DS_Store"))
        delete_roots.extend(junk_files)
        display_targets.extend(junk_files)

        backup_targets = self._backup_targets()
        delete_roots.extend(backup_targets)
        display_targets.extend(backup_targets)

        audit_run_targets = self._audit_run_targets()
        delete_roots.extend(audit_run_targets)
        display_targets.extend(audit_run_targets)

        import_run_targets = self._import_run_targets()
        delete_roots.extend(import_run_targets)
        display_targets.extend(import_run_targets)

        unique_roots = list(
            dict.fromkeys(self._validate_delete_target(path) for path in delete_roots)
        )
        unique_display = list(
            dict.fromkeys(self._absolute_lexical(path) for path in display_targets)
        )
        return {
            "delete_roots": unique_roots,
            "display_targets": unique_display,
            "backup_targets": [self._absolute_lexical(path) for path in backup_targets],
            "audit_run_targets": [self._absolute_lexical(path) for path in audit_run_targets],
            "import_run_targets": [self._absolute_lexical(path) for path in import_run_targets],
        }

    def _metadata_targets(self) -> dict[str, list[int]]:
        import_run_ids: list[int] = []
        export_run_ids: list[int] = []
        import_runs_dir = self.data_dir / "import-runs"

        with session_scope(self.db_path) as session:
            for import_row in session.query(ImportRunDB).all():
                source_path = Path(str(import_row.source_path)).expanduser()
                source_resolved = source_path.resolve()
                if not source_path.exists() or self._is_relative_to(
                    source_resolved, import_runs_dir
                ):
                    import_run_ids.append(int(import_row.id))

            for export_row in session.query(ExportRunDB).all():
                output_path = Path(str(export_row.output_path)).expanduser()
                if "exports-smoke" in output_path.parts:
                    export_run_ids.append(int(export_row.id))

        return {"import_run_ids": import_run_ids, "export_run_ids": export_run_ids}

    def _backup_targets(self) -> list[Path]:
        """Prunable DB backups: the newest of each REASON is always retained.

        Backups are named ``<stem>-<timestamp>-<uuid>-<reason>.db``. Retaining only
        the newest ``-cleanup`` snapshot deleted every ``-pre-*`` one — precisely the
        snapshots that `rules apply --execute`, `optimize categories --execute-safe`,
        `config import --execute`, and `db doctor --execute` print as the rollback
        path. A cleanup run minutes after a mutation would discard the only way back.

        Nothing newer than the newest retained snapshot is ever removed, so the most
        recent state is always recoverable regardless of which command produced it.
        """
        backups_dir = self._validate_managed_parent(self.data_dir / "db-backups")
        backups = sorted(backups_dir.glob("*.db"))
        if not backups:
            return []

        def sort_key(path: Path) -> tuple[float, str]:
            return (path.lstat().st_mtime, path.name)

        retained: set[Path] = set()
        # Cleanup takes its own snapshot during execute (AFTER this plan is built), so
        # when no prior '-cleanup' backup exists everything else is prunable; the fresh
        # one becomes the survivor.
        cleanup_backups = [path for path in backups if "-cleanup" in path.stem]
        if cleanup_backups:
            retained.add(max(cleanup_backups, key=sort_key))

        # But never the most recent pre-mutation snapshot: that is the rollback path
        # other commands explicitly print, and a cleanup run minutes after a mutation
        # used to delete it.
        pre_mutation = [path for path in backups if self._is_pre_mutation_backup(path)]
        if pre_mutation:
            retained.add(max(pre_mutation, key=sort_key))

        return [path for path in backups if path not in retained]

    @staticmethod
    def _is_pre_mutation_backup(path: Path) -> bool:
        """Whether a backup is one of the advertised rollback snapshots.

        backup_database names these ``<stem>-<timestamp>-<uuid>-pre-<operation>.db``.
        `rules apply --execute`, `optimize categories --execute-safe`,
        `config import --execute`, and `db doctor --execute` all print the path of one
        of these as the way to roll back, so cleanup must never leave a user without
        the most recent one.
        """
        return "-pre-" in path.stem

    def _audit_run_targets(self) -> list[Path]:
        audit_runs_dir = self._validate_managed_parent(self.data_dir / "audit-runs")
        if not audit_runs_dir.exists():
            return []

        audit_runs: list[Path] = []
        for path in audit_runs_dir.iterdir():
            if path.is_symlink():
                raise ValueError(f"Cleanup refuses symlinked audit run: {path}")
            if path.is_dir():
                audit_runs.append(path)
        audit_runs.sort()
        if len(audit_runs) <= 1:
            return []

        passing_runs = [path for path in audit_runs if self._is_passing_audit_run(path)]
        retain_candidates = passing_runs or audit_runs
        retained = max(retain_candidates, key=lambda path: (path.lstat().st_mtime, path.name))
        return [path for path in audit_runs if path != retained]

    @staticmethod
    def _is_passing_audit_run(path: Path) -> bool:
        summary_path = path / "summary.json"
        if not summary_path.exists():
            return False
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError, OSError:
            return False
        return bool(summary.get("ok")) and summary.get("status") == "pass"

    def _import_run_targets(self) -> list[Path]:
        """Prunable finalized/cancelled/failed import-run dirs, keeping the newest.

        In-flight runs (staged / needs_review / ready_to_finalize / finalizing) are
        never pruned, and at least one finalized run is always retained for traceability.
        """
        runs_dir = self._validate_managed_parent(self.data_dir / "import-runs")
        if not runs_dir.exists():
            return []
        run_dirs: list[Path] = []
        for path in runs_dir.iterdir():
            if path.is_symlink():
                raise ValueError(f"Cleanup refuses symlinked import run: {path}")
            if path.is_dir():
                run_dirs.append(path)
        run_dirs.sort()
        if len(run_dirs) <= 1:
            return []
        finalized = [
            p
            for p in run_dirs
            if self._import_run_status(p) in _PRUNABLE_RUN_STATES
            and not self._is_recoverable_run(p)
        ]
        if not finalized:
            return []
        retained = max(finalized, key=lambda p: (p.lstat().st_mtime, p.name))
        return [p for p in finalized if p != retained]

    @staticmethod
    def _is_recoverable_run(run_dir: Path) -> bool:
        """A finalize_failed run is RECOVERABLE, not done: finalize() retries it and
        reads staging.db + csv/ from the run dir. manifest.json collapses its status to
        'failed' (so _import_run_status reports it prunable), but run.json carries the
        real failure_reason -- read it so a retriable run is never pruned out from under
        the user (which would destroy its staging.db and applied review decisions)."""
        run_json = run_dir / "run.json"
        if not run_json.exists():
            return False
        try:
            data = json.loads(run_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError, OSError:
            return False
        status = str(data.get("status", ""))
        return status in {"finalizing", "finalize_failed"} or (
            status == "failed" and data.get("failure_reason") == "finalize_failed"
        )

    @staticmethod
    def _import_run_status(run_dir: Path) -> str:
        for name in ("manifest.json", "run.json"):
            f = run_dir / name
            if f.exists():
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except json.JSONDecodeError, OSError:
                    return "unknown"
                return str(data.get("status", "unknown"))
        return "unknown"

    def _orphan_root_backups(self) -> list[Path]:
        """DB backup files sitting directly under data_dir, outside db-backups/.

        e.g. data/ledger.db.backup-YYYYMMDD-HHMMSS left by an old promote path. The
        managed db-backups/ retention (+chmod 0600) never sees these, so they linger
        as un-tracked derived PII. Excludes ledger-backup.db (handled separately) and
        anything already inside db-backups/.
        """
        candidates = list(self.data_dir.glob("*.backup*")) + list(
            self.data_dir.glob("ledger.db.backup*")
        )
        orphans = [
            p
            for p in candidates
            if not p.is_symlink() and p.is_file() and p.parent == self.data_dir
        ]
        return sorted(dict.fromkeys(orphans))

    def _relocate_orphan_backups(self) -> list[Path]:
        orphans = self._orphan_root_backups()
        if not orphans:
            return []
        dest = ensure_private_dir(self.data_dir / "db-backups")
        moved: list[Path] = []
        for src in orphans:
            target = dest / src.name
            if target.exists():
                target = dest / f"{src.stem}-relocated{src.suffix}"
            shutil.move(str(src), str(target))
            tighten_private_file(target)
            moved.append(target)
        return moved

    def _delete_paths(self, roots: list[Path]) -> list[Path]:
        deleted: list[Path] = []
        for root in roots:
            path = self._validate_delete_target(root)
            if path.is_symlink():
                path.unlink()
                deleted.append(path)
                continue
            if not path.exists():
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
            deleted.append(path)
        return deleted

    def _ensure_directories(self) -> None:
        for name in ["inbox", "import-runs", "processed", "db-backups"]:
            ensure_private_dir(self.data_dir / name)

    def _prune_metadata(self, targets: dict[str, list[int]]) -> dict[str, int]:
        with session_scope(self.db_path) as session:
            import_count = 0
            export_count = 0
            if targets["import_run_ids"]:
                import_count = (
                    session.query(ImportRunDB)
                    .filter(ImportRunDB.id.in_(targets["import_run_ids"]))
                    .delete(synchronize_session=False)
                )
            if targets["export_run_ids"]:
                export_count = (
                    session.query(ExportRunDB)
                    .filter(ExportRunDB.id.in_(targets["export_run_ids"]))
                    .delete(synchronize_session=False)
                )
        return {"import_runs": int(import_count), "export_runs": int(export_count)}

    def _vacuum(self) -> None:
        with closing(sqlite3.connect(self.db_path)) as conn:
            conn.execute("VACUUM")

    @staticmethod
    def _absolute_lexical(path: Path) -> Path:
        """Return an absolute normalized path without resolving symlinks."""
        return Path(os.path.abspath(os.fspath(path.expanduser())))

    @staticmethod
    def _reject_symlink_components(path: Path) -> None:
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current /= part
            if current.is_symlink():
                raise ValueError(f"Cleanup refuses symlinked data directory: {current}")

    def _validate_delete_target(self, path: Path) -> Path:
        """Require a lexical in-tree target with no symlinked parent component."""
        self._reject_symlink_components(self.data_dir)
        candidate = self._absolute_lexical(path)
        data_dir = self._absolute_lexical(self.data_dir)
        try:
            relative = candidate.relative_to(data_dir)
        except ValueError:
            raise ValueError(f"Cleanup target is outside data directory: {candidate}")
        if not relative.parts:
            raise ValueError(f"Cleanup refuses to delete the data directory: {candidate}")

        current = data_dir
        for part in relative.parts[:-1]:
            current /= part
            if current.is_symlink():
                raise ValueError(f"Cleanup target has symlinked parent: {current}")
        return candidate

    def _validate_managed_parent(self, path: Path) -> Path:
        """Reject a cleanup-owned directory when the directory itself is a symlink."""
        parent = self._validate_delete_target(path)
        if parent.is_symlink():
            raise ValueError(f"Cleanup refuses symlinked managed parent: {parent}")
        return parent

    @staticmethod
    def _is_relative_to(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent.resolve())
            return True
        except ValueError:
            return False
