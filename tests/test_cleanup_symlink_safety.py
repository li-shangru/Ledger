"""Regression tests for cleanup path containment and symlink handling."""

from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path

import pytest

import ledger.maintenance.cleanup as cleanup_module
from ledger.maintenance import DataCleanupService


def _cleanup_service(tmp_path: Path) -> tuple[DataCleanupService, Path]:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return DataCleanupService(data_dir / "ledger.db", data_dir=data_dir), data_dir


def test_cleanup_rejects_symlinked_data_dir_root_before_initializing_db(
    tmp_path: Path,
):
    real_data_dir = tmp_path / "real-data"
    real_data_dir.mkdir()
    data_dir_link = tmp_path / "data-link"
    data_dir_link.symlink_to(real_data_dir, target_is_directory=True)

    with pytest.raises(ValueError, match="symlinked data directory"):
        DataCleanupService(data_dir_link / "ledger.db", data_dir=data_dir_link)

    assert not (real_data_dir / "ledger.db").exists()


def test_cleanup_rejects_symlinked_data_dir_ancestor_before_initializing_db(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    real_data_dir = outside / "data"
    real_data_dir.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)
    requested_data_dir = alias / "data"

    with pytest.raises(ValueError, match="symlinked data directory"):
        DataCleanupService(
            requested_data_dir / "ledger.db",
            data_dir=requested_data_dir,
        )

    assert not (real_data_dir / "ledger.db").exists()


def test_cleanup_rejects_data_dir_root_swapped_to_symlink_before_delete(
    tmp_path: Path,
):
    service, data_dir = _cleanup_service(tmp_path)
    outside_dir = tmp_path / "outside-root"
    outside_dir.mkdir()
    sentinel = outside_dir / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    original_data_dir = tmp_path / "original-data"
    data_dir.rename(original_data_dir)
    data_dir.symlink_to(outside_dir, target_is_directory=True)

    with pytest.raises(ValueError, match="symlinked data directory"):
        service._delete_paths([data_dir / sentinel.name])

    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_cleanup_unlinks_exports_smoke_symlink_without_following_target(
    tmp_path: Path,
):
    service, data_dir = _cleanup_service(tmp_path)
    outside_dir = tmp_path / "outside-exports"
    outside_dir.mkdir()
    sentinel = outside_dir / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    smoke_link = data_dir / "exports-smoke"
    smoke_link.symlink_to(outside_dir, target_is_directory=True)

    result = service.clean(execute=True)

    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not smoke_link.is_symlink()
    assert str(smoke_link) in result.details["delete_roots"]
    assert str(outside_dir) not in result.details["delete_roots"]


def test_cleanup_rejects_symlinked_managed_parent(tmp_path: Path):
    service, data_dir = _cleanup_service(tmp_path)
    outside_backups = tmp_path / "outside-backups"
    outside_backups.mkdir()
    (outside_backups / "old.db").write_text("old", encoding="utf-8")
    (outside_backups / "ledger-20260709-cleanup.db").write_text(
        "retained",
        encoding="utf-8",
    )
    (data_dir / "db-backups").symlink_to(outside_backups, target_is_directory=True)

    with pytest.raises(ValueError, match="symlinked managed parent"):
        service.dry_run()


def test_cleanup_does_not_relocate_or_chmod_symlinked_orphan_backup(tmp_path: Path):
    service, data_dir = _cleanup_service(tmp_path)
    outside = tmp_path / "outside.db"
    outside.write_bytes(b"outside")
    outside.chmod(0o644)
    orphan_link = data_dir / "ledger.db.backup-symlink"
    orphan_link.symlink_to(outside)

    service.clean(execute=True)

    assert orphan_link.is_symlink()
    assert outside.stat().st_mode & 0o777 == 0o644


def test_cleanup_rejects_delete_target_with_symlinked_intermediate_parent(
    tmp_path: Path,
):
    service, data_dir = _cleanup_service(tmp_path)
    outside_dir = tmp_path / "outside-intermediate"
    outside_dir.mkdir()
    sentinel = outside_dir / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    intermediate_link = data_dir / "nested"
    intermediate_link.symlink_to(outside_dir, target_is_directory=True)

    with pytest.raises(ValueError, match="symlinked parent"):
        service._delete_paths([intermediate_link / sentinel.name])

    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_cleanup_rejects_delete_target_outside_data_dir(tmp_path: Path):
    service, _ = _cleanup_service(tmp_path)
    outside_target = tmp_path / "outside.txt"
    outside_target.write_text("keep", encoding="utf-8")

    with pytest.raises(ValueError, match="outside data directory"):
        service._delete_paths([outside_target])

    assert outside_target.read_text(encoding="utf-8") == "keep"


def test_cleanup_directory_recreation_fails_closed_when_chmod_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, data_dir = _cleanup_service(tmp_path)
    real_fchmod = cleanup_module.os.fchmod

    def fail_inbox_chmod(descriptor: int, mode: int) -> None:
        inbox = data_dir / "inbox"
        metadata = os.fstat(descriptor)
        if inbox.exists() and metadata.st_ino == inbox.stat().st_ino:
            raise PermissionError("simulated cleanup chmod failure")
        real_fchmod(descriptor, mode)

    monkeypatch.setattr(cleanup_module.os, "fchmod", fail_inbox_chmod)

    with pytest.raises(PermissionError, match="simulated cleanup chmod failure"):
        service._ensure_directories()

    assert stat.S_IMODE((data_dir / "inbox").stat().st_mode) == 0o700


def test_cleanup_revalidates_target_swapped_to_symlink_before_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    service, data_dir = _cleanup_service(tmp_path)
    old_run = data_dir / "audit-runs" / "20260701T000000Z"
    retained_run = data_dir / "audit-runs" / "20260702T000000Z"
    for run_dir, mtime in ((old_run, 1_000), (retained_run, 2_000)):
        run_dir.mkdir(parents=True)
        (run_dir / "summary.json").write_text(
            '{"status": "pass", "ok": true}',
            encoding="utf-8",
        )
        os.utime(run_dir, (mtime, mtime))

    outside_dir = tmp_path / "outside-audit"
    outside_dir.mkdir()
    sentinel = outside_dir / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    original_backup = cleanup_module.backup_database

    def backup_then_swap(*args, **kwargs):
        result = original_backup(*args, **kwargs)
        shutil.rmtree(old_run)
        old_run.symlink_to(outside_dir, target_is_directory=True)
        return result

    monkeypatch.setattr(cleanup_module, "backup_database", backup_then_swap)

    result = service.clean(execute=True)

    assert result.ok
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert not old_run.is_symlink()
    assert retained_run.exists()
