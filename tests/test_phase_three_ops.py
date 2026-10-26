"""Tests for phase three operational infrastructure."""

import contextlib
import json
import os
import sqlite3
import stat
import threading
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from account_fixtures import seed_account

from ledger.exporting import ExportPackageService
from ledger.imports.pdf.manifest import read_manifest, write_manifest
from ledger.maintenance import DataCleanupService
from ledger.query import LedgerQueryService
from ledger.reporting import ReportSnapshotService
from ledger.storage.database import (
    DBOperationAuditDB,
    ExportRunDB,
    ImportRunDB,
    RecurringPatternDB,
    ReportSnapshotDB,
    TransactionDB,
    init_database,
    session_scope,
)
from ledger.storage.migrations import migration_status, run_migrations, validate_database
from ledger.storage.operations import backup_database


def _file_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _seed_snapshot_db(path: Path, value: str) -> None:
    with contextlib.closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("create table snapshot_rows (value text not null)")
        connection.execute("insert into snapshot_rows values (?)", (value,))


def _corrupt_index_root(path: Path, index_name: str) -> None:
    with contextlib.closing(sqlite3.connect(path)) as connection:
        page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
        row = connection.execute(
            "SELECT rootpage FROM sqlite_schema WHERE type = 'index' AND name = ?",
            (index_name,),
        ).fetchone()
    assert row is not None
    root_page = int(row[0])
    assert root_page > 1
    offset = (root_page - 1) * page_size
    with path.open("r+b") as handle:
        assert handle.read(16) == b"SQLite format 3\x00"
        handle.seek(offset)
        assert handle.read(1) in {b"\x02", b"\x0a"}
        handle.seek(offset)
        handle.write(b"\x00")
        handle.flush()
        os.fsync(handle.fileno())


def _temporal(posting_date: date) -> dict:
    return {
        "day_of_week": posting_date.weekday(),
        "day_of_month": posting_date.day,
        "week_of_year": posting_date.isocalendar()[1],
        "is_weekend": posting_date.weekday() >= 5,
        "is_month_start": posting_date.day <= 3,
        "is_month_end": posting_date.day >= 28,
    }


def _seed_txn(db_path: Path) -> None:
    init_database(db_path)
    posted = date(2026, 5, 1)
    with session_scope(db_path) as session:
        session.add(
            TransactionDB(
                txn_id="phase3",
                posting_date=posted,
                transaction_date=posted,
                institution="harbor",
                account="1111222233334444",
                account_type="visa",
                flow="expense",
                cashflow_type="expense",
                include_in_cashflow=True,
                currency="CAD",
                amount_raw=Decimal("123.45"),
                amount_signed=Decimal("-123.45"),
                description="PHASE THREE TEST",
                merchant="PHASE THREE",
                category="Software & Digital",
                source_file="statement.csv",
                source_path="/private/statement.csv",
                source_period="2026-05",
                source_row=1,
                imported_at=datetime.now(UTC).replace(tzinfo=None),
                parser_version="test",
                txn_fingerprint="phase3fp",
                **_temporal(posted),
            )
        )


def _seed_recurring_pattern(db_path: Path) -> None:
    account_id = seed_account(db_path, "harbor", "1111222233334444", account_type="visa")
    with session_scope(db_path) as session:
        session.add(
            RecurringPatternDB(
                pattern_id="recurring-phase3",
                pattern_type="subscription",
                merchant="ANTHROPIC",
                category="Software & Digital",
                institution="harbor",
                account_id=account_id,
                frequency="monthly",
                expected_day_of_period=1,
                is_fixed_amount=True,
                expected_amount=Decimal("32.50"),
                amount_std_dev=Decimal("0.00"),
                confidence=0.95,
                detection_method="test",
                occurrence_count=3,
                first_occurrence=date(2026, 3, 1),
                last_occurrence=date(2026, 5, 1),
                next_expected_date=date(2026, 6, 1),
                is_active=True,
                missed_count=0,
                detected_at=datetime.now(UTC).replace(tzinfo=None),
                updated_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )


def _seed_cleanup_state(data_dir: Path) -> Path:
    db_path = data_dir / "ledger.db"
    init_database(db_path)
    (data_dir / "exports-smoke" / "ledger-chatgpt-safe-smoke").mkdir(parents=True)
    (data_dir / "exports-smoke" / "ledger-chatgpt-safe-smoke" / "transactions.csv").write_text(
        "id\n1\n",
        encoding="utf-8",
    )
    (data_dir / "exports-smoke" / "ledger-chatgpt-safe-smoke.zip").write_text(
        "zip",
        encoding="utf-8",
    )
    (data_dir / "ledger-backup.db").write_text("old backup", encoding="utf-8")
    (data_dir / "db-backups").mkdir()
    (data_dir / "db-backups" / "old-backup.db").write_text("old backup", encoding="utf-8")
    old_audit = data_dir / "audit-runs" / "20260501T000000Z"
    latest_audit = data_dir / "audit-runs" / "20260514T000000Z"
    old_audit.mkdir(parents=True)
    latest_audit.mkdir(parents=True)
    (old_audit / "summary.json").write_text('{"status": "pass", "ok": true}', encoding="utf-8")
    (latest_audit / "summary.json").write_text('{"status": "pass", "ok": true}', encoding="utf-8")
    (data_dir / "processed").mkdir()
    (data_dir / "processed" / "statement.pdf").write_text("source pdf", encoding="utf-8")
    stale_csv = data_dir / "import-runs" / "old-run" / "csv"
    with session_scope(db_path) as session:
        session.add(
            ImportRunDB(
                run_at=datetime.now(UTC).replace(tzinfo=None),
                source_path=str(stale_csv),
                files_processed=1,
                transactions_imported=1,
                transfers_imported=0,
                duplicates_skipped=0,
                errors=0,
                parser_version="test",
            )
        )
        session.add(
            ExportRunDB(
                created_at=datetime.now(UTC).replace(tzinfo=None),
                preset="chatgpt-safe",
                export_format="zip",
                output_path=str(data_dir / "exports-smoke" / "ledger-chatgpt-safe-smoke.zip"),
                rows_exported=1,
                status="success",
                details={},
            )
        )
    return db_path


def test_migration_runner_records_version_and_backup(tmp_path: Path):
    db_path = tmp_path / "ledger.db"
    init_database(db_path)

    result = run_migrations(db_path)
    validation = validate_database(db_path)
    backup = backup_database(db_path, backup_dir=tmp_path / "backups", reason="test")

    assert result.ok
    assert validation.ok
    assert migration_status(db_path)["applied"] >= 1
    assert backup.ok
    assert Path(backup.artifacts["backup"]).exists()


# === Fix 2: VACUUM INTO escaping ===


def test_backup_database_default_dir_is_db_backups(tmp_path: Path):
    """backup_database() without backup_dir must default to db-backups/, not backups/."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)

    result = backup_database(db_path, reason="test_default_dir")

    assert result.ok, result.errors
    backup_path = Path(result.artifacts["backup"])
    assert backup_path.parent.name == "db-backups", (
        f"Expected default backup dir 'db-backups', got '{backup_path.parent.name}'"
    )
    assert backup_path.exists()


def test_backup_database_path_with_single_quote(tmp_path: Path):
    """VACUUM INTO must handle paths that contain a single-quote character."""
    # Create a backup directory whose name contains a single quote.
    backup_dir = tmp_path / "back'ups"
    backup_dir.mkdir()
    db_path = tmp_path / "ledger.db"
    init_database(db_path)

    result = backup_database(db_path, backup_dir=backup_dir, reason="squote")

    assert result.ok, result.errors
    backup_path = Path(result.artifacts["backup"])
    # The backup must actually exist in the directory with the single quote.
    assert backup_path.exists(), f"Backup file not found at {backup_path}"
    assert "back'ups" in str(backup_path)


def test_snapshot_database_captures_committed_wal_rows_privately(tmp_path: Path) -> None:
    from ledger.storage.snapshots import snapshot_database

    source = tmp_path / "live.db"
    target = tmp_path / "snapshots" / "backup.db"
    connection = sqlite3.connect(source)
    try:
        assert connection.execute("pragma journal_mode=wal").fetchone()[0] == "wal"
        connection.execute("pragma wal_autocheckpoint=0")
        connection.execute("create table snapshot_rows (value text not null)")
        connection.commit()
        connection.execute("insert into snapshot_rows values ('committed-in-wal')")
        connection.commit()
        assert source.with_name(source.name + "-wal").exists()

        previous_umask = os.umask(0o022)
        try:
            result = snapshot_database(source, target)
        finally:
            os.umask(previous_umask)
    finally:
        connection.close()

    assert result == target
    with contextlib.closing(sqlite3.connect(target)) as snapshot, snapshot:
        assert snapshot.execute("select value from snapshot_rows").fetchall() == [
            ("committed-in-wal",)
        ]
        assert snapshot.execute("pragma integrity_check").fetchall() == [("ok",)]
    assert _file_mode(target.parent) == 0o700
    assert _file_mode(target) == 0o600


def test_snapshot_database_rejects_missing_source_without_creating_paths(tmp_path: Path) -> None:
    from ledger.storage.snapshots import snapshot_database

    source = tmp_path / "missing.db"
    target = tmp_path / "snapshots" / "backup.db"

    with pytest.raises(FileNotFoundError, match="Database not found"):
        snapshot_database(source, target)

    assert not source.exists()
    assert not target.exists()
    assert not target.parent.exists()


@pytest.mark.parametrize("payload", [b"", b"not a sqlite database"])
def test_snapshot_database_rejects_non_sqlite_header_before_creating_destination(
    tmp_path: Path,
    payload: bytes,
) -> None:
    from ledger.storage.snapshots import snapshot_database

    source = tmp_path / "invalid.db"
    source.write_bytes(payload)
    target = tmp_path / "snapshots" / "backup.db"

    with pytest.raises(sqlite3.DatabaseError, match="SQLite database header"):
        snapshot_database(source, target)

    assert source.read_bytes() == payload
    assert not target.parent.exists()


def test_snapshot_database_corrupt_source_preserves_old_target_and_cleans_temps(
    tmp_path: Path,
) -> None:
    from ledger.storage.snapshots import snapshot_database

    source = tmp_path / "corrupt.db"
    source.write_bytes(b"not sqlite")
    target = tmp_path / "backup.db"
    _seed_snapshot_db(target, "old-target")

    with pytest.raises(sqlite3.DatabaseError):
        snapshot_database(source, target)

    with contextlib.closing(sqlite3.connect(target)) as preserved, preserved:
        assert preserved.execute("select value from snapshot_rows").fetchall() == [("old-target",)]
    assert not list(tmp_path.glob(f".{target.name}.snapshot-*.tmp*"))


def test_snapshot_database_zero_byte_source_preserves_old_target_and_cleans_temps(
    tmp_path: Path,
) -> None:
    from ledger.storage.snapshots import snapshot_database

    source = tmp_path / "empty.db"
    source.write_bytes(b"")
    target = tmp_path / "backup.db"
    _seed_snapshot_db(target, "old-target")

    with pytest.raises(sqlite3.DatabaseError, match="SQLite database header"):
        snapshot_database(source, target)

    with contextlib.closing(sqlite3.connect(target)) as preserved, preserved:
        assert preserved.execute("select value from snapshot_rows").fetchall() == [("old-target",)]
    assert source.read_bytes() == b""
    assert not list(tmp_path.glob(f".{target.name}.snapshot-*.tmp*"))


def test_snapshot_database_replace_failure_preserves_target_and_cleans_temps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.storage import snapshots

    source = tmp_path / "live.db"
    target = tmp_path / "backup.db"
    _seed_snapshot_db(source, "new-source")
    _seed_snapshot_db(target, "old-target")

    def fail_replace(_source: object, _target: object, **_kwargs: object) -> None:
        raise OSError("publish failed")

    monkeypatch.setattr(snapshots.os, "replace", fail_replace)

    with pytest.raises(OSError, match="publish failed"):
        snapshots.snapshot_database(source, target)

    with contextlib.closing(sqlite3.connect(target)) as preserved, preserved:
        assert preserved.execute("select value from snapshot_rows").fetchall() == [("old-target",)]
    assert not list(tmp_path.glob(f".{target.name}.snapshot-*.tmp*"))


def test_snapshot_database_rejects_symlink_destination(tmp_path: Path) -> None:
    from ledger.storage.snapshots import snapshot_database

    source = tmp_path / "live.db"
    outside = tmp_path / "outside.db"
    _seed_snapshot_db(source, "new-source")
    _seed_snapshot_db(outside, "outside-target")
    target = tmp_path / "backup.db"
    target.symlink_to(outside)

    with pytest.raises(OSError, match="symlink"):
        snapshot_database(source, target)

    assert target.is_symlink()
    with contextlib.closing(sqlite3.connect(outside)) as preserved, preserved:
        assert preserved.execute("select value from snapshot_rows").fetchall() == [
            ("outside-target",)
        ]


def test_snapshot_database_fails_closed_on_destination_parent_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.storage import snapshots

    source = tmp_path / "live.db"
    parent = tmp_path / "snapshots"
    parent.mkdir()
    target = parent / "backup.db"
    _seed_snapshot_db(source, "new-source")
    _seed_snapshot_db(target, "old-target")
    moved_parent = tmp_path / "moved-snapshots"
    redirect = tmp_path / "redirect"
    redirect.mkdir()
    real_connect = snapshots.sqlite3.connect
    swapped = False

    def swap_parent_then_connect(*args: object, **kwargs: object):
        nonlocal swapped
        if not swapped:
            swapped = True
            parent.rename(moved_parent)
            parent.symlink_to(redirect, target_is_directory=True)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(snapshots.sqlite3, "connect", swap_parent_then_connect)
    try:
        with pytest.raises(OSError, match=r"changed|symlink"):
            snapshots.snapshot_database(source, target)
    finally:
        if parent.is_symlink():
            parent.unlink()
        if moved_parent.exists():
            moved_parent.rename(parent)

    with contextlib.closing(sqlite3.connect(target)) as preserved, preserved:
        assert preserved.execute("select value from snapshot_rows").fetchall() == [("old-target",)]
    assert not (redirect / target.name).exists()
    assert not list(parent.glob(f".{target.name}.*.tmp"))
    assert not list(redirect.glob(f".{target.name}.*.tmp"))


def test_snapshot_database_fails_closed_on_real_destination_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.storage import snapshots

    source = tmp_path / "live.db"
    _seed_snapshot_db(source, "new-source")
    parent = tmp_path / "snapshots"
    parent.mkdir()
    target = parent / "backup.db"
    _seed_snapshot_db(target, "old-target")
    moved_parent = tmp_path / "moved-snapshots"
    substitute_parent = tmp_path / "substitute-snapshots"
    substitute_parent.mkdir()
    substitute_target = substitute_parent / target.name
    _seed_snapshot_db(substitute_target, "outside-target")
    real_connect = snapshots.sqlite3.connect
    swapped = False

    def swap_parent_then_connect(*args: object, **kwargs: object):
        nonlocal swapped
        if not swapped:
            swapped = True
            parent.rename(moved_parent)
            substitute_parent.rename(parent)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(snapshots.sqlite3, "connect", swap_parent_then_connect)
    try:
        with pytest.raises(OSError, match="changed"):
            snapshots.snapshot_database(source, target)
    finally:
        if parent.exists():
            parent.rename(substitute_parent)
        if moved_parent.exists():
            moved_parent.rename(parent)

    with contextlib.closing(sqlite3.connect(target)) as preserved, preserved:
        assert preserved.execute("select value from snapshot_rows").fetchall() == [("old-target",)]
    with contextlib.closing(sqlite3.connect(substitute_target)) as outside, outside:
        assert outside.execute("select value from snapshot_rows").fetchall() == [
            ("outside-target",)
        ]


def test_snapshot_database_fails_closed_on_source_file_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.storage import snapshots

    source = tmp_path / "live.db"
    substitute = tmp_path / "substitute.db"
    moved_source = tmp_path / "moved-live.db"
    target = tmp_path / "backup.db"
    _seed_snapshot_db(source, "intended-source")
    _seed_snapshot_db(substitute, "substitute-source")
    _seed_snapshot_db(target, "old-target")
    real_connect = snapshots.sqlite3.connect
    swapped = False

    def swap_source_then_connect(*args: object, **kwargs: object):
        nonlocal swapped
        if not swapped:
            swapped = True
            source.rename(moved_source)
            substitute.rename(source)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(snapshots.sqlite3, "connect", swap_source_then_connect)
    try:
        with pytest.raises(OSError, match="changed"):
            snapshots.snapshot_database(source, target)
    finally:
        if source.exists():
            source.rename(substitute)
        if moved_source.exists():
            moved_source.rename(source)

    with contextlib.closing(sqlite3.connect(target)) as preserved, preserved:
        assert preserved.execute("select value from snapshot_rows").fetchall() == [("old-target",)]


def test_restore_database_corrupt_snapshot_preserves_live_target(tmp_path: Path) -> None:
    from ledger.storage.snapshots import restore_database

    snapshot = tmp_path / "corrupt.db"
    snapshot.write_bytes(b"SQLite format 3\x00" + b"corrupt pages")
    target = tmp_path / "ledger.db"
    _seed_snapshot_db(target, "live-target")

    with pytest.raises(sqlite3.DatabaseError):
        restore_database(snapshot, target)

    with contextlib.closing(sqlite3.connect(target)) as preserved, preserved:
        assert preserved.execute("select value from snapshot_rows").fetchall() == [("live-target",)]


def test_restore_database_rejects_symlink_destination(tmp_path: Path) -> None:
    from ledger.storage.snapshots import restore_database

    snapshot = tmp_path / "snapshot.db"
    outside = tmp_path / "outside.db"
    _seed_snapshot_db(snapshot, "snapshot")
    _seed_snapshot_db(outside, "outside")
    target = tmp_path / "ledger.db"
    target.symlink_to(outside)

    with pytest.raises(OSError, match="symlink"):
        restore_database(snapshot, target)

    assert target.is_symlink()
    with contextlib.closing(sqlite3.connect(outside)) as preserved, preserved:
        assert preserved.execute("select value from snapshot_rows").fetchall() == [("outside",)]


def test_restore_database_rejects_symlinked_destination_parent(tmp_path: Path) -> None:
    from ledger.storage.snapshots import restore_database

    snapshot = tmp_path / "snapshot.db"
    _seed_snapshot_db(snapshot, "snapshot")
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    outside_target = outside_dir / "ledger.db"
    _seed_snapshot_db(outside_target, "outside")
    redirected = tmp_path / "redirected"
    redirected.symlink_to(outside_dir, target_is_directory=True)

    with pytest.raises(OSError, match="symlink"):
        restore_database(snapshot, redirected / "ledger.db")

    with contextlib.closing(sqlite3.connect(outside_target)) as preserved, preserved:
        assert preserved.execute("select value from snapshot_rows").fetchall() == [("outside",)]


def test_restore_database_fails_closed_on_destination_parent_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.storage import snapshots

    snapshot = tmp_path / "snapshot.db"
    _seed_snapshot_db(snapshot, "snapshot-value")
    parent = tmp_path / "live"
    parent.mkdir()
    target = parent / "ledger.db"
    _seed_snapshot_db(target, "live-value")
    moved_parent = tmp_path / "moved-live"
    redirect = tmp_path / "redirect-live"
    redirect.mkdir()
    redirected_target = redirect / target.name
    _seed_snapshot_db(redirected_target, "outside-value")
    real_connect = snapshots.sqlite3.connect
    swapped = False

    def swap_parent_then_connect(*args: object, **kwargs: object):
        nonlocal swapped
        if not swapped:
            swapped = True
            parent.rename(moved_parent)
            parent.symlink_to(redirect, target_is_directory=True)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(snapshots.sqlite3, "connect", swap_parent_then_connect)
    try:
        with pytest.raises(OSError, match=r"changed|symlink"):
            snapshots.restore_database(snapshot, target)
    finally:
        if parent.is_symlink():
            parent.unlink()
        if moved_parent.exists():
            moved_parent.rename(parent)

    with contextlib.closing(sqlite3.connect(target)) as preserved, preserved:
        assert preserved.execute("select value from snapshot_rows").fetchall() == [("live-value",)]
    with contextlib.closing(sqlite3.connect(redirected_target)) as outside, outside:
        assert outside.execute("select value from snapshot_rows").fetchall() == [("outside-value",)]


def test_restore_database_pins_target_before_path_header_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.storage import snapshots

    snapshot = tmp_path / "snapshot.db"
    _seed_snapshot_db(snapshot, "snapshot-value")
    parent = tmp_path / "live"
    parent.mkdir()
    target = parent / "ledger.db"
    _seed_snapshot_db(target, "live-value")
    moved_parent = tmp_path / "moved-live"
    substitute_parent = tmp_path / "substitute-live"
    substitute_parent.mkdir()
    substitute_target = substitute_parent / target.name
    _seed_snapshot_db(substitute_target, "outside-value")
    real_require_header = snapshots._require_sqlite_header
    swapped = False

    def validate_then_swap(path: Path, *, label: str) -> None:
        nonlocal swapped
        real_require_header(path, label=label)
        if label == "Restore destination":
            swapped = True
            parent.rename(moved_parent)
            substitute_parent.rename(parent)

    monkeypatch.setattr(snapshots, "_require_sqlite_header", validate_then_swap)
    try:
        result = snapshots.restore_database(snapshot, target)
    finally:
        if swapped:
            if parent.exists():
                parent.rename(substitute_parent)
            if moved_parent.exists():
                moved_parent.rename(parent)

    assert result == target
    assert not swapped
    with contextlib.closing(sqlite3.connect(target)) as restored, restored:
        assert restored.execute("select value from snapshot_rows").fetchall() == [
            ("snapshot-value",)
        ]
    with contextlib.closing(sqlite3.connect(substitute_target)) as outside, outside:
        assert outside.execute("select value from snapshot_rows").fetchall() == [("outside-value",)]


def test_restore_database_supports_relative_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_snapshot_db(tmp_path / "snapshot.db", "snapshot-value")
    _seed_snapshot_db(tmp_path / "ledger.db", "live-value")
    monkeypatch.chdir(tmp_path)

    from ledger.storage.snapshots import restore_database

    result = restore_database(Path("snapshot.db"), Path("ledger.db"))

    assert result == Path("ledger.db")
    with contextlib.closing(sqlite3.connect(tmp_path / "ledger.db")) as restored, restored:
        assert restored.execute("select value from snapshot_rows").fetchall() == [
            ("snapshot-value",)
        ]


def test_restore_database_fails_closed_on_snapshot_file_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.storage import snapshots

    snapshot = tmp_path / "snapshot.db"
    substitute = tmp_path / "substitute-snapshot.db"
    moved_snapshot = tmp_path / "moved-snapshot.db"
    target = tmp_path / "ledger.db"
    _seed_snapshot_db(snapshot, "intended-snapshot")
    _seed_snapshot_db(substitute, "substitute-snapshot")
    _seed_snapshot_db(target, "live-value")
    real_connect = snapshots.sqlite3.connect
    swapped = False

    def swap_snapshot_then_connect(*args: object, **kwargs: object):
        nonlocal swapped
        if not swapped:
            swapped = True
            snapshot.rename(moved_snapshot)
            substitute.rename(snapshot)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(snapshots.sqlite3, "connect", swap_snapshot_then_connect)
    try:
        with pytest.raises(OSError, match="changed"):
            snapshots.restore_database(snapshot, target)
    finally:
        if snapshot.exists():
            snapshot.rename(substitute)
        if moved_snapshot.exists():
            moved_snapshot.rename(snapshot)

    with contextlib.closing(sqlite3.connect(target)) as preserved, preserved:
        assert preserved.execute("select value from snapshot_rows").fetchall() == [("live-value",)]


def test_restore_database_permission_failure_precedes_backup_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.storage import snapshots

    snapshot = tmp_path / "snapshot.db"
    target = tmp_path / "ledger.db"
    _seed_snapshot_db(snapshot, "snapshot-secret")
    _seed_snapshot_db(target, "live-value")
    target.chmod(0o644)
    real_fchmod = snapshots.os.fchmod

    def fail_regular_file(descriptor: int, mode: int) -> None:
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise PermissionError("simulated restore permission failure")
        real_fchmod(descriptor, mode)

    monkeypatch.setattr(snapshots.os, "fchmod", fail_regular_file)

    with pytest.raises(PermissionError, match="simulated restore permission failure"):
        snapshots.restore_database(snapshot, target)

    with contextlib.closing(sqlite3.connect(target)) as preserved, preserved:
        assert preserved.execute("select value from snapshot_rows").fetchall() == [("live-value",)]
    assert _file_mode(target) == 0o644


def test_guarded_publication_serializes_wal_writer_until_after_backup(tmp_path: Path) -> None:
    from ledger.storage.snapshots import publish_database

    candidate = tmp_path / "candidate.db"
    target = tmp_path / "ledger.db"
    _seed_snapshot_db(candidate, "candidate-state")
    _seed_snapshot_db(target, "live-state")
    with contextlib.closing(sqlite3.connect(target)) as connection, connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        connection.execute("PRAGMA wal_autocheckpoint=0")

    writer_ready = threading.Event()
    writer_allowed = threading.Event()
    writer_attempting = threading.Event()
    writer_finished = threading.Event()
    writer_errors: list[BaseException] = []

    def write_during_publication() -> None:
        try:
            with contextlib.closing(sqlite3.connect(target, timeout=10)) as connection, connection:
                connection.execute("PRAGMA busy_timeout=10000")
                writer_ready.set()
                assert writer_allowed.wait(timeout=10)
                writer_attempting.set()
                connection.execute("INSERT INTO snapshot_rows VALUES ('writer-state')")
        except BaseException as exc:
            writer_errors.append(exc)
        finally:
            writer_finished.set()

    def guard(connection: sqlite3.Connection) -> None:
        assert connection.execute("SELECT value FROM snapshot_rows").fetchall() == [("live-state",)]
        assert writer_ready.wait(timeout=10)
        writer_allowed.set()
        assert writer_attempting.wait(timeout=10)
        assert not writer_finished.wait(timeout=0.1)

    writer = threading.Thread(target=write_during_publication)
    writer.start()
    try:
        result = publish_database(candidate, target, guard=guard)
    finally:
        writer_allowed.set()
        writer.join(timeout=10)

    assert result == target
    assert not writer.is_alive()
    assert writer_errors == []
    with contextlib.closing(sqlite3.connect(target)) as connection, connection:
        assert connection.execute("SELECT value FROM snapshot_rows ORDER BY rowid").fetchall() == [
            ("candidate-state",),
            ("writer-state",),
        ]


def test_guarded_publication_preserves_live_target_when_guard_raises(tmp_path: Path) -> None:
    from ledger.storage.snapshots import publish_database

    candidate = tmp_path / "candidate.db"
    target = tmp_path / "ledger.db"
    _seed_snapshot_db(candidate, "candidate-state")
    _seed_snapshot_db(target, "live-state")

    def reject_live_state(connection: sqlite3.Connection) -> None:
        assert connection.execute("SELECT value FROM snapshot_rows").fetchall() == [("live-state",)]
        raise RuntimeError("guard rejected live state")

    with pytest.raises(RuntimeError, match="guard rejected live state"):
        publish_database(candidate, target, guard=reject_live_state)

    with contextlib.closing(sqlite3.connect(target)) as connection, connection:
        assert connection.execute("SELECT value FROM snapshot_rows").fetchall() == [("live-state",)]


def test_validated_database_target_tightens_modes_and_keeps_pinned_identity(
    tmp_path: Path,
) -> None:
    """Recovery validation keeps one private regular database identity authoritative."""
    from ledger.storage.snapshots import database_target_lock, validated_database_target

    target = tmp_path / "live" / "ledger.db"
    target.parent.mkdir()
    _seed_snapshot_db(target, "live-state")
    observed_identities: list[tuple[int, int]] = []

    def guard(connection: sqlite3.Connection) -> None:
        metadata = target.stat(follow_symlinks=False)
        observed_identities.append((metadata.st_dev, metadata.st_ino))
        assert connection.execute("SELECT value FROM snapshot_rows").fetchall() == [("live-state",)]
        assert _file_mode(target.parent) == 0o700
        assert _file_mode(target) == 0o600

    with database_target_lock(target):
        target.parent.chmod(0o755)
        target.chmod(0o644)
        expected = target.stat(follow_symlinks=False)
        expected_identity = (expected.st_dev, expected.st_ino)

        with validated_database_target(
            target,
            label="Synthetic recovery target",
            guard=guard,
        ) as connection:
            current = target.stat(follow_symlinks=False)
            observed_identities.append((current.st_dev, current.st_ino))
            assert connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]

    final = target.stat(follow_symlinks=False)
    observed_identities.append((final.st_dev, final.st_ino))
    assert observed_identities == [expected_identity, expected_identity, expected_identity]
    assert _file_mode(target.parent) == 0o700
    assert _file_mode(target) == 0o600


def test_validated_database_target_rejects_corruption_before_caller_transition(
    tmp_path: Path,
) -> None:
    """A corrupt secondary index cannot reach the caller-controlled success path."""
    from ledger.storage.snapshots import database_target_lock, validated_database_target

    target = tmp_path / "ledger.db"
    with contextlib.closing(sqlite3.connect(target)) as connection, connection:
        connection.execute("CREATE TABLE validation_rows (value TEXT NOT NULL)")
        connection.execute("CREATE INDEX ix_validation_rows ON validation_rows(value)")
        connection.execute("INSERT INTO validation_rows VALUES ('live-state')")
    _corrupt_index_root(target, "ix_validation_rows")
    guard_called = False

    def guard(_connection: sqlite3.Connection) -> None:
        nonlocal guard_called
        guard_called = True

    with (
        database_target_lock(target),
        pytest.raises(sqlite3.DatabaseError),
        validated_database_target(
            target,
            label="Synthetic corrupt target",
            guard=guard,
        ),
    ):
        pytest.fail("corrupt database reached caller transition")

    assert guard_called is False


def test_validated_database_target_blocks_wal_writer_until_context_exit(
    tmp_path: Path,
) -> None:
    """The guard snapshot and caller transition share one SQLite exclusion window."""
    from ledger.storage.snapshots import database_target_lock, validated_database_target

    target = tmp_path / "ledger.db"
    _seed_snapshot_db(target, "live-state")
    with contextlib.closing(sqlite3.connect(target)) as connection, connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        connection.execute("PRAGMA wal_autocheckpoint=0")

    writer_ready = threading.Event()
    writer_allowed = threading.Event()
    writer_attempting = threading.Event()
    writer_finished = threading.Event()
    writer_errors: list[BaseException] = []

    def write_during_validation() -> None:
        try:
            with contextlib.closing(sqlite3.connect(target, timeout=10)) as connection, connection:
                connection.execute("PRAGMA busy_timeout=10000")
                writer_ready.set()
                assert writer_allowed.wait(timeout=10)
                writer_attempting.set()
                connection.execute("INSERT INTO snapshot_rows VALUES ('writer-state')")
        except BaseException as exc:
            writer_errors.append(exc)
        finally:
            writer_finished.set()

    writer = threading.Thread(target=write_during_validation)
    writer.start()
    try:
        assert writer_ready.wait(timeout=10)
        with (
            database_target_lock(target),
            validated_database_target(
                target,
                label="Synthetic WAL target",
                guard=lambda _connection: None,
            ),
        ):
            writer_allowed.set()
            assert writer_attempting.wait(timeout=10)
            assert not writer_finished.wait(timeout=0.1)
    finally:
        writer_allowed.set()
        writer.join(timeout=10)

    assert not writer.is_alive()
    assert writer_errors == []
    with contextlib.closing(sqlite3.connect(target)) as connection, connection:
        assert connection.execute("SELECT value FROM snapshot_rows ORDER BY rowid").fetchall() == [
            ("live-state",),
            ("writer-state",),
        ]


@pytest.mark.parametrize("replacement_kind", ["symlink", "regular"])
def test_validated_database_target_rejects_replacement_during_context(
    tmp_path: Path,
    replacement_kind: str,
) -> None:
    """The named target cannot change identity during the protected transition."""
    from ledger.storage.snapshots import database_target_lock, validated_database_target

    target = tmp_path / "ledger.db"
    moved_target = tmp_path / "moved-ledger.db"
    substitute = tmp_path / "substitute.db"
    _seed_snapshot_db(target, "live-state")
    _seed_snapshot_db(substitute, "substitute-state")

    with database_target_lock(target):
        try:
            with (
                pytest.raises(OSError, match=r"changed|symlink"),
                validated_database_target(
                    target,
                    label="Synthetic replaced target",
                    guard=lambda _connection: None,
                ),
            ):
                target.rename(moved_target)
                if replacement_kind == "symlink":
                    target.symlink_to(substitute)
                else:
                    substitute.rename(target)
        finally:
            if target.is_symlink():
                target.unlink()
            elif target.exists():
                target.rename(substitute)
            if moved_target.exists():
                moved_target.rename(target)

    with contextlib.closing(sqlite3.connect(target)) as connection, connection:
        assert connection.execute("SELECT value FROM snapshot_rows").fetchall() == [("live-state",)]


def test_validated_database_target_restores_and_rejects_permission_drift(
    tmp_path: Path,
) -> None:
    """Private parent/file modes remain authoritative across the caller transition."""
    from ledger.storage.snapshots import database_target_lock, validated_database_target

    target = tmp_path / "live" / "ledger.db"
    target.parent.mkdir()
    _seed_snapshot_db(target, "live-state")

    with (
        database_target_lock(target),
        pytest.raises(PermissionError, match="permissions"),
        validated_database_target(
            target,
            label="Synthetic permission target",
            guard=lambda _connection: None,
        ),
    ):
        target.parent.chmod(0o755)
        target.chmod(0o644)

    assert _file_mode(target.parent) == 0o700
    assert _file_mode(target) == 0o600


@pytest.mark.parametrize("guard_action", ["insert", "commit", "rollback"])
def test_validated_database_target_rejects_guard_mutation_or_transaction_control(
    tmp_path: Path,
    guard_action: str,
) -> None:
    """The borrowed guard connection cannot modify or release the validation snapshot."""
    from ledger.storage.snapshots import database_target_lock, validated_database_target

    target = tmp_path / "ledger.db"
    _seed_snapshot_db(target, "live-state")
    body_reached = False

    def guard(connection: sqlite3.Connection) -> None:
        if guard_action == "insert":
            connection.execute("INSERT INTO snapshot_rows VALUES ('guard-state')")
        elif guard_action == "commit":
            connection.commit()
        else:
            connection.rollback()

    with (
        database_target_lock(target),
        pytest.raises(
            (RuntimeError, sqlite3.DatabaseError),
            match=r"authoriz|read.?only|transaction|write",
        ),
        validated_database_target(
            target,
            label="Synthetic guard target",
            guard=guard,
        ),
    ):
        body_reached = True

    assert body_reached is False
    with contextlib.closing(sqlite3.connect(target)) as connection, connection:
        assert connection.execute("SELECT value FROM snapshot_rows").fetchall() == [("live-state",)]


def test_validated_database_target_preserves_primary_error_when_cleanup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A secondary cleanup failure cannot replace the transition's primary error."""
    from ledger.storage import snapshots

    target = tmp_path / "ledger.db"
    _seed_snapshot_db(target, "live-state")
    real_tighten = snapshots._tighten_private_database_modes
    tighten_calls = 0

    def fail_after_cleanup_tighten(pinned) -> None:
        nonlocal tighten_calls
        tighten_calls += 1
        real_tighten(pinned)
        if tighten_calls == 2:
            raise OSError("synthetic cleanup failure")

    monkeypatch.setattr(
        snapshots,
        "_tighten_private_database_modes",
        fail_after_cleanup_tighten,
    )

    with (
        snapshots.database_target_lock(target),
        pytest.raises(RuntimeError, match="synthetic primary failure") as error,
        snapshots.validated_database_target(
            target,
            label="Synthetic primary-error target",
            guard=lambda _connection: None,
        ),
    ):
        raise RuntimeError("synthetic primary failure")

    assert tighten_calls == 2
    assert any(
        "synthetic cleanup failure" in note for note in getattr(error.value, "__notes__", ())
    )


def test_guarded_publication_does_not_clobber_target_that_appears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.storage import snapshots

    candidate = tmp_path / "candidate.db"
    target = tmp_path / "ledger.db"
    _seed_snapshot_db(candidate, "candidate-state")
    real_private_atomic = snapshots.private_atomic_binary

    @contextlib.contextmanager
    def target_appears_before_snapshot_publish(path: Path):
        with real_private_atomic(path) as handle:
            yield handle
            if not target.exists():
                _seed_snapshot_db(target, "concurrent-state")

    monkeypatch.setattr(
        snapshots,
        "private_atomic_binary",
        target_appears_before_snapshot_publish,
    )

    with pytest.raises(FileExistsError):
        snapshots.publish_database(candidate, target, guard=lambda _connection: None)

    with contextlib.closing(sqlite3.connect(target)) as connection, connection:
        assert connection.execute("SELECT value FROM snapshot_rows").fetchall() == [
            ("concurrent-state",)
        ]


def test_guarded_absent_publication_fsyncs_names_in_durable_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh target is durable before its staged recovery name is removed."""
    from ledger.storage import snapshots

    candidate = tmp_path / "candidate.db"
    target = tmp_path / "ledger.db"
    _seed_snapshot_db(candidate, "candidate-state")
    events: list[str] = []
    publication_name: str | None = None
    linked = False
    staged_unlinked = False
    real_link = snapshots.os.link
    real_stat = snapshots.os.stat
    real_unlink = snapshots.os.unlink
    real_fsync = snapshots.os.fsync

    def record_link(source, destination, *args, **kwargs):
        nonlocal linked, publication_name
        result = real_link(source, destination, *args, **kwargs)
        if destination == target.name and kwargs.get("dst_dir_fd") is not None:
            publication_name = str(source)
            linked = True
            events.append("link")
        return result

    def record_stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if linked and path == target.name and kwargs.get("dir_fd") is not None:
            events.append("validate-target")
        return result

    def record_unlink(path, *args, **kwargs):
        nonlocal staged_unlinked
        if linked and path == publication_name and kwargs.get("dir_fd") is not None:
            staged_unlinked = True
            events.append("unlink-staged")
        return real_unlink(path, *args, **kwargs)

    def record_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            if linked and not staged_unlinked:
                events.append("fsync-linked-target")
            elif staged_unlinked:
                events.append("fsync-staged-unlink")
        real_fsync(descriptor)

    monkeypatch.setattr(snapshots.os, "link", record_link)
    monkeypatch.setattr(snapshots.os, "stat", record_stat)
    monkeypatch.setattr(snapshots.os, "unlink", record_unlink)
    monkeypatch.setattr(snapshots.os, "fsync", record_fsync)

    assert snapshots.publish_database(candidate, target, guard=lambda _connection: None) == target

    assert publication_name is not None
    assert events.index("link") < events.index("validate-target")
    assert events.index("validate-target") < events.index("fsync-linked-target")
    assert events.index("fsync-linked-target") < events.index("unlink-staged")
    assert events.index("unlink-staged") < events.index("fsync-staged-unlink")
    with contextlib.closing(sqlite3.connect(target)) as connection, connection:
        assert connection.execute("SELECT value FROM snapshot_rows").fetchall() == [
            ("candidate-state",)
        ]
    assert _file_mode(target) == 0o600
    assert _file_mode(target.parent) == 0o700


def test_guarded_absent_publication_post_link_fsync_failure_rolls_back_durably(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed target barrier durably removes the link and preserves staging."""
    from ledger.storage import snapshots

    candidate = tmp_path / "candidate.db"
    target = tmp_path / "ledger.db"
    _seed_snapshot_db(candidate, "candidate-state")
    events: list[str] = []
    publication_name: str | None = None
    linked = False
    target_unlinked = False
    failed_link_barrier = False
    real_link = snapshots.os.link
    real_unlink = snapshots.os.unlink
    real_fsync = snapshots.os.fsync

    def record_link(source, destination, *args, **kwargs):
        nonlocal linked, publication_name
        result = real_link(source, destination, *args, **kwargs)
        if destination == target.name and kwargs.get("dst_dir_fd") is not None:
            publication_name = str(source)
            linked = True
            events.append("link")
        return result

    def record_unlink(path, *args, **kwargs):
        nonlocal target_unlinked
        if linked and path == target.name and kwargs.get("dir_fd") is not None:
            target_unlinked = True
            events.append("unlink-target")
        return real_unlink(path, *args, **kwargs)

    def fail_target_barrier(descriptor: int) -> None:
        nonlocal failed_link_barrier
        if stat.S_ISDIR(os.fstat(descriptor).st_mode) and linked:
            if not failed_link_barrier:
                failed_link_barrier = True
                events.append("fail-target-fsync")
                raise OSError("synthetic post-link fsync failure")
            if target_unlinked:
                events.append("fsync-rollback")
        real_fsync(descriptor)

    monkeypatch.setattr(snapshots.os, "link", record_link)
    monkeypatch.setattr(snapshots.os, "unlink", record_unlink)
    monkeypatch.setattr(snapshots.os, "fsync", fail_target_barrier)

    with pytest.raises(OSError, match="durability") as error:
        snapshots.publish_database(candidate, target, guard=lambda _connection: None)

    assert getattr(error.value, "target_durable", None) is False
    assert events.index("fail-target-fsync") < events.index("unlink-target")
    assert events.index("unlink-target") < events.index("fsync-rollback")
    assert not target.exists()
    assert publication_name is not None
    recovery = target.parent / publication_name
    assert recovery.is_file()
    with contextlib.closing(sqlite3.connect(recovery)) as connection, connection:
        assert connection.execute("SELECT value FROM snapshot_rows").fetchall() == [
            ("candidate-state",)
        ]
    assert _file_mode(recovery) == 0o600


def test_guarded_absent_publication_rollback_fsync_failure_preserves_staged_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even a failed rollback barrier cannot erase the known staged recovery name."""
    from ledger.storage import snapshots

    candidate = tmp_path / "candidate.db"
    target = tmp_path / "ledger.db"
    _seed_snapshot_db(candidate, "candidate-state")
    publication_name: str | None = None
    linked = False
    target_unlinked = False
    directory_failures = 0
    real_link = snapshots.os.link
    real_unlink = snapshots.os.unlink
    real_fsync = snapshots.os.fsync

    def record_link(source, destination, *args, **kwargs):
        nonlocal linked, publication_name
        result = real_link(source, destination, *args, **kwargs)
        if destination == target.name and kwargs.get("dst_dir_fd") is not None:
            publication_name = str(source)
            linked = True
        return result

    def record_unlink(path, *args, **kwargs):
        nonlocal target_unlinked
        if linked and path == target.name and kwargs.get("dir_fd") is not None:
            target_unlinked = True
        return real_unlink(path, *args, **kwargs)

    def fail_publication_and_rollback_barriers(descriptor: int) -> None:
        nonlocal directory_failures
        if stat.S_ISDIR(os.fstat(descriptor).st_mode) and linked:
            directory_failures += 1
            if directory_failures == 1:
                raise OSError("synthetic post-link fsync failure")
            if target_unlinked:
                raise OSError("synthetic rollback fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(snapshots.os, "link", record_link)
    monkeypatch.setattr(snapshots.os, "unlink", record_unlink)
    monkeypatch.setattr(snapshots.os, "fsync", fail_publication_and_rollback_barriers)

    with pytest.raises(OSError, match="durability") as error:
        snapshots.publish_database(candidate, target, guard=lambda _connection: None)

    assert getattr(error.value, "target_durable", None) is False
    assert getattr(error.value, "rollback_error", None) is not None
    assert directory_failures == 2
    assert target_unlinked
    assert not target.exists()
    assert publication_name is not None
    recovery = target.parent / publication_name
    assert recovery.is_file()
    with contextlib.closing(sqlite3.connect(recovery)) as connection, connection:
        assert connection.execute("SELECT value FROM snapshot_rows").fetchall() == [
            ("candidate-state",)
        ]
    assert _file_mode(recovery) == 0o600


def test_guarded_absent_publication_post_unlink_fsync_failure_retains_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cleanup-barrier failure propagates without rolling back a durable target."""
    from ledger.storage import snapshots

    candidate = tmp_path / "candidate.db"
    target = tmp_path / "ledger.db"
    _seed_snapshot_db(candidate, "candidate-state")
    publication_name: str | None = None
    linked = False
    staged_unlinked = False
    target_unlinked = False
    real_link = snapshots.os.link
    real_unlink = snapshots.os.unlink
    real_fsync = snapshots.os.fsync

    def record_link(source, destination, *args, **kwargs):
        nonlocal linked, publication_name
        result = real_link(source, destination, *args, **kwargs)
        if destination == target.name and kwargs.get("dst_dir_fd") is not None:
            publication_name = str(source)
            linked = True
        return result

    def record_unlink(path, *args, **kwargs):
        nonlocal staged_unlinked, target_unlinked
        if linked and path == publication_name and kwargs.get("dir_fd") is not None:
            staged_unlinked = True
        if linked and path == target.name and kwargs.get("dir_fd") is not None:
            target_unlinked = True
        return real_unlink(path, *args, **kwargs)

    def fail_cleanup_barrier(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode) and staged_unlinked:
            raise OSError("synthetic post-unlink fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(snapshots.os, "link", record_link)
    monkeypatch.setattr(snapshots.os, "unlink", record_unlink)
    monkeypatch.setattr(snapshots.os, "fsync", fail_cleanup_barrier)

    with pytest.raises(OSError, match="durability") as error:
        snapshots.publish_database(candidate, target, guard=lambda _connection: None)

    assert getattr(error.value, "target_durable", None) is True
    assert target.is_file()
    assert not target_unlinked
    assert publication_name is not None
    assert not (target.parent / publication_name).exists()
    with contextlib.closing(sqlite3.connect(target)) as connection, connection:
        assert connection.execute("SELECT value FROM snapshot_rows").fetchall() == [
            ("candidate-state",)
        ]
    assert _file_mode(target) == 0o600


def test_clone_database_captures_committed_wal_rows_and_publishes_privately(
    tmp_path: Path,
) -> None:
    from ledger.imports.pdf.files import clone_database

    source = tmp_path / "live.db"
    target = tmp_path / "run" / "staging.db"
    connection = sqlite3.connect(source)
    try:
        connection.execute("pragma journal_mode=wal")
        connection.execute("pragma wal_autocheckpoint=0")
        connection.execute("create table snapshot_rows (value text not null)")
        connection.execute("insert into snapshot_rows values ('main-file')")
        connection.commit()
        connection.execute("pragma wal_checkpoint(truncate)")
        connection.execute("insert into snapshot_rows values ('committed-in-wal')")
        connection.commit()
        assert source.with_name(source.name + "-wal").exists()

        previous_umask = os.umask(0o022)
        try:
            clone_database(source, target)
        finally:
            os.umask(previous_umask)
    finally:
        connection.close()

    with contextlib.closing(sqlite3.connect(target)) as clone, clone:
        assert clone.execute("select value from snapshot_rows order by rowid").fetchall() == [
            ("main-file",),
            ("committed-in-wal",),
        ]
    assert _file_mode(target.parent) == 0o700
    assert _file_mode(target) == 0o600


def test_clone_database_corrupt_source_preserves_existing_target(tmp_path: Path) -> None:
    from ledger.imports.pdf.files import clone_database

    source = tmp_path / "corrupt.db"
    source.write_bytes(b"not sqlite")
    target = tmp_path / "staging.db"
    _seed_snapshot_db(target, "old-staging")

    with pytest.raises(sqlite3.DatabaseError):
        clone_database(source, target)

    with contextlib.closing(sqlite3.connect(target)) as preserved, preserved:
        assert preserved.execute("select value from snapshot_rows").fetchall() == [("old-staging",)]


def test_promote_database_backup_captures_wal_and_candidate_copy_is_private(
    tmp_path: Path,
) -> None:
    from ledger.imports.pdf import files

    target = tmp_path / "live" / "ledger.db"
    target.parent.mkdir()
    staging = tmp_path / "run" / "staging.db"
    staging.parent.mkdir()
    backups = tmp_path / "backups"
    _seed_snapshot_db(target, "main-file")
    _seed_snapshot_db(staging, "promoted")
    staging.chmod(0o644)

    connection = sqlite3.connect(target)
    try:
        target_inode = target.stat().st_ino
        connection.execute("pragma journal_mode=wal")
        connection.execute("pragma wal_autocheckpoint=0")
        connection.execute("pragma wal_checkpoint(truncate)")
        connection.execute("insert into snapshot_rows values ('committed-in-wal')")
        connection.commit()
        assert target.with_name(target.name + "-wal").exists()

        previous_umask = os.umask(0o022)
        try:
            backup = files.promote_database(staging, target, backups)
        finally:
            os.umask(previous_umask)
        assert connection.execute("select value from snapshot_rows").fetchall() == [("promoted",)]
        assert target.stat().st_ino == target_inode
    finally:
        connection.close()

    assert backup is not None
    with contextlib.closing(sqlite3.connect(backup)) as snapshot, snapshot:
        assert snapshot.execute("select value from snapshot_rows order by rowid").fetchall() == [
            ("main-file",),
            ("committed-in-wal",),
        ]
    with contextlib.closing(sqlite3.connect(target)) as promoted, promoted:
        assert promoted.execute("select value from snapshot_rows").fetchall() == [("promoted",)]
        assert promoted.execute("pragma integrity_check").fetchall() == [("ok",)]
    assert _file_mode(backups) == 0o700
    assert _file_mode(backup) == 0o600
    assert _file_mode(target) == 0o600


def test_backup_database_corrupt_source_fails_without_raw_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.storage import operations

    source = tmp_path / "corrupt.db"
    source.write_bytes(b"not sqlite")
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(operations, "record_db_operation", lambda *_args, **_kwargs: None)

    result = operations.backup_database(source, backup_dir=backup_dir, reason="corrupt")

    assert result.status == "failed"
    assert result.errors
    assert not list(backup_dir.glob("*.db"))


def test_backup_database_zero_byte_source_fails_without_mutating_source(tmp_path: Path) -> None:
    from ledger.storage.operations import backup_database

    source = tmp_path / "empty.db"
    source.write_bytes(b"")
    backup_dir = tmp_path / "backups"

    result = backup_database(source, backup_dir=backup_dir, reason="empty")

    assert result.status == "failed"
    assert any("SQLite database header" in error for error in result.errors)
    assert source.read_bytes() == b""
    assert not backup_dir.exists()


def test_database_promotions_reject_symlink_targets(tmp_path: Path) -> None:
    from ledger.imports.pdf.files import promote_database
    from ledger.repair.statements import promote_candidate

    outside = tmp_path / "outside.db"
    staging = tmp_path / "staging.db"
    candidate = tmp_path / "candidate.db"
    _seed_snapshot_db(outside, "outside")
    _seed_snapshot_db(staging, "staging")
    _seed_snapshot_db(candidate, "candidate")
    target = tmp_path / "ledger.db"
    target.symlink_to(outside)

    with pytest.raises(OSError, match="symlink"):
        promote_database(staging, target, tmp_path / "backups")
    with pytest.raises(OSError, match="symlink"):
        promote_candidate(candidate, target)

    assert target.is_symlink()
    with contextlib.closing(sqlite3.connect(outside)) as preserved, preserved:
        assert preserved.execute("select value from snapshot_rows").fetchall() == [("outside",)]


def test_database_promotion_failures_clean_temp_sqlite_sidecars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.imports.pdf import files, readiness
    from ledger.repair import statements

    created: list[Path] = []

    def fail_after_sidecars(*, live_db: Path, into_db: Path) -> None:
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = Path(f"{into_db}{suffix}")
            sidecar.write_bytes(b"sidecar")
            created.append(sidecar)
        raise RuntimeError("merge failed")

    monkeypatch.setattr(readiness, "merge_forward", fail_after_sidecars)

    for prefix, promote in (
        (
            "import",
            lambda candidate, target: files.promote_database(
                candidate,
                target,
                tmp_path / "backups",
            ),
        ),
        ("repair", lambda candidate, target: statements.promote_candidate(candidate, target)),
    ):
        target = tmp_path / f"{prefix}-target.db"
        candidate = tmp_path / f"{prefix}-candidate.db"
        _seed_snapshot_db(target, "live")
        _seed_snapshot_db(candidate, "candidate")
        with pytest.raises(RuntimeError, match="merge failed"):
            promote(candidate, target)

    assert created
    assert all(not path.exists() for path in created)


def test_concurrent_backups_publish_distinct_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from ledger.storage import operations

    source = tmp_path / "ledger.db"
    _seed_snapshot_db(source, "live")
    barrier = Barrier(2)
    real_snapshot = operations.snapshot_database

    def synchronized_snapshot(source_path: Path, target_path: Path) -> Path:
        barrier.wait()
        return real_snapshot(source_path, target_path)

    monkeypatch.setattr(operations, "snapshot_database", synchronized_snapshot)
    monkeypatch.setattr(operations, "record_db_operation", lambda *_args, **_kwargs: None)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _index: operations.backup_database(
                    source,
                    backup_dir=tmp_path / "backups",
                    reason="concurrent",
                ),
                range(2),
            )
        )

    artifacts = [Path(result.artifacts["backup"]) for result in results]
    assert all(result.ok for result in results)
    assert len(set(artifacts)) == 2
    assert all(path.exists() for path in artifacts)


# === Fix 3: validate_database integrity checks ===


def test_validate_database_runs_integrity_check_on_healthy_db(tmp_path: Path):
    """A healthy DB must pass validate_database with status='success' and no errors."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    run_migrations(db_path)

    result = validate_database(db_path, record=False)

    assert result.status == "success", result.errors
    assert result.errors == []
    # Confirm the integrity/fk detail keys are present.
    assert result.details["integrity_errors"] == 0
    assert result.details["fk_errors"] == 0


def test_import_manifest_normalizes_state_and_next_actions(tmp_path: Path):
    run_dir = tmp_path / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    manifest = write_manifest(
        run_dir,
        {
            "status": "staged",
            "staging_db_path": str(tmp_path / "staging.db"),
            "target_db_path": str(tmp_path / "ledger.db"),
            "pending_review_count": 2,
            "pdfs": [{"original_pdf": "statement.pdf", "transaction_count": 3}],
        },
    )

    assert manifest["status"] == "staged"
    assert manifest["gates"]["review"] == "fail"
    assert read_manifest(run_dir)["next_actions"]


def test_report_snapshot_contains_reporting_sections(tmp_path: Path):
    db_path = tmp_path / "ledger.db"
    _seed_txn(db_path)
    _seed_recurring_pattern(db_path)

    snapshot = ReportSnapshotService(db_path).create_snapshot(source="test")
    payload = snapshot["payload"]

    assert payload["schema_version"] == 3
    assert "income_spend_net_movement" in payload
    assert "cost_cutting_candidates" in payload
    assert "category_drift" in payload
    assert "credit_card_optimization" in payload
    assert payload["subscriptions"][0]["first_occurrence"] == "2026-03-01"
    with session_scope(db_path) as session:
        saved = session.query(ReportSnapshotDB).filter_by(id=snapshot["id"]).one()
        assert saved.payload["subscriptions"][0]["next_expected_date"] == "2026-06-01"


def test_report_snapshot_for_period_uses_requested_range(tmp_path: Path):
    db_path = tmp_path / "ledger.db"
    _seed_txn(db_path)
    service = ReportSnapshotService(db_path)

    snapshot = service.snapshot_for_period(
        date(2026, 5, 1),
        date(2026, 5, 31),
        source="test_period",
    )
    same_period = service.snapshot_for_period(
        date(2026, 5, 1),
        date(2026, 5, 31),
        source="test_period",
    )

    assert snapshot["id"] == same_period["id"]
    assert snapshot["period_start"] == date(2026, 5, 1)
    assert snapshot["period_end"] == date(2026, 5, 31)
    assert snapshot["payload"]["period"]["start"] == "2026-05-01"
    assert snapshot["payload"]["period"]["end"] == "2026-05-31"


def test_credit_card_optimization_zip_contains_extra_summaries(tmp_path: Path):
    db_path = tmp_path / "ledger.db"
    _seed_txn(db_path)
    ReportSnapshotService(db_path).create_snapshot(source="test")

    result = ExportPackageService(db_path).create_package(
        dest_dir=tmp_path / "exports",
        preset="credit-card-optimization",
        export_format="zip",
    )

    with zipfile.ZipFile(result.path) as package:
        names = set(package.namelist())

    assert "card_account_summary.csv" in names
    assert "category_movement.csv" in names


def test_query_facade_exposes_focused_modules(tmp_path: Path):
    db_path = tmp_path / "ledger.db"
    _seed_txn(db_path)

    service = LedgerQueryService(db_path)

    assert service.transactions.db_path == db_path
    assert service.transactions.get_transactions()
    assert service.aggregates.db_path == db_path
    assert (
        service.aggregates.compute_kpis(date(2026, 5, 1), date(2026, 5, 31))["transaction_count"]
        == 1
    )
    assert service.accounts.db_path == db_path
    assert service.reports.db_path == db_path
    assert service.get_transactions() == service.transactions.get_transactions()


def test_query_facade_is_each_focused_module_in_dispatch_order(tmp_path: Path):
    from ledger.query.accounts import AccountQueries
    from ledger.query.aggregates import AggregateQueries
    from ledger.query.exports import ExportQueries
    from ledger.query.insights import InsightQueries
    from ledger.query.quality import QualityQueries
    from ledger.query.reports import ReportQueries
    from ledger.query.transactions import TransactionQueries
    from ledger.query.transfers import TransferQueries

    db_path = tmp_path / "ledger.db"
    _seed_txn(db_path)

    service = LedgerQueryService(db_path)
    module_classes = (
        TransactionQueries,
        AggregateQueries,
        AccountQueries,
        InsightQueries,
        QualityQueries,
        TransferQueries,
        ExportQueries,
        ReportQueries,
    )

    assert LedgerQueryService.__mro__[1:9] == module_classes
    assert isinstance(service, TransactionQueries)
    assert isinstance(service, AggregateQueries)
    assert isinstance(service, AccountQueries)
    assert isinstance(service, InsightQueries)
    assert isinstance(service, QualityQueries)
    assert isinstance(service, TransferQueries)
    assert isinstance(service, ExportQueries)
    assert isinstance(service, ReportQueries)
    assert "__getattr__" not in LedgerQueryService.__dict__
    assert "_modules" not in service.__dict__


def test_query_facade_namespace_attributes_alias_the_typed_service(tmp_path: Path):
    db_path = tmp_path / "ledger.db"
    _seed_txn(db_path)

    service = LedgerQueryService(db_path)

    assert service.transactions is service
    assert service.aggregates is service
    assert service.accounts is service
    assert service.insights is service
    assert service.quality is service
    assert service.transfers is service
    assert service.exports is service
    assert service.reports is service
    assert service.get_transactions() == service.transactions.get_transactions()
    assert service.compute_kpis(
        date(2026, 5, 1), date(2026, 5, 31)
    ) == service.aggregates.compute_kpis(date(2026, 5, 1), date(2026, 5, 31))


def test_query_facade_initializes_shared_query_module_once(tmp_path: Path, monkeypatch):
    from ledger.query.base import QueryModule

    db_path = tmp_path / "ledger.db"
    _seed_txn(db_path)
    calls: list[tuple[QueryModule, Path]] = []
    original_init = QueryModule.__init__

    def recording_init(self: QueryModule, path: Path) -> None:
        calls.append((self, path))
        original_init(self, path)

    monkeypatch.setattr(QueryModule, "__init__", recording_init)

    service = LedgerQueryService(db_path)

    assert calls == [(service, db_path)]


def test_query_facade_missing_database_fails_before_shared_initialization(
    tmp_path: Path, monkeypatch
):
    from ledger.query.base import QueryModule

    calls: list[Path] = []
    original_init = QueryModule.__init__

    def recording_init(self: QueryModule, path: Path) -> None:
        calls.append(path)
        original_init(self, path)

    monkeypatch.setattr(QueryModule, "__init__", recording_init)

    with pytest.raises(FileNotFoundError, match="Database not found"):
        LedgerQueryService(tmp_path / "missing.db")

    assert calls == []


def test_data_cleanup_dry_run_does_not_mutate_files_or_metadata(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = _seed_cleanup_state(data_dir)

    result = DataCleanupService(db_path).dry_run()

    assert result.ok
    assert result.details["mode"] == "dry_run"
    assert result.counts["delete_roots"] >= 3
    assert result.counts["audit_runs_to_prune"] == 1
    assert (data_dir / "exports-smoke").exists()
    assert (data_dir / "ledger-backup.db").exists()
    assert (data_dir / "audit-runs" / "20260501T000000Z").exists()
    with session_scope(db_path) as session:
        assert session.query(ImportRunDB).count() == 1
        assert session.query(ExportRunDB).count() == 1


def test_data_cleanup_execute_prunes_only_stale_artifacts_and_records_audit(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = _seed_cleanup_state(data_dir)

    result = DataCleanupService(db_path).clean(execute=True)

    assert result.ok
    retained_backup = Path(result.artifacts["backup"])
    assert retained_backup.exists()
    assert retained_backup.parent == data_dir / "db-backups"
    assert not (data_dir / "exports-smoke").exists()
    assert not (data_dir / "ledger-backup.db").exists()
    assert not (data_dir / "db-backups" / "old-backup.db").exists()
    assert not (data_dir / "audit-runs" / "20260501T000000Z").exists()
    assert (data_dir / "audit-runs" / "20260514T000000Z").exists()
    assert (data_dir / "processed" / "statement.pdf").exists()
    followup = DataCleanupService(db_path).dry_run()
    assert followup.counts["backups_to_prune"] == 0
    assert followup.counts["delete_roots"] == 0
    with session_scope(db_path) as session:
        assert session.query(ImportRunDB).count() == 0
        assert session.query(ExportRunDB).count() == 0
        assert (
            session.query(DBOperationAuditDB)
            .filter(DBOperationAuditDB.operation == "data_cleanup")
            .count()
            == 1
        )


def test_promote_backup_label_varies_by_run_type(tmp_path: Path):
    from ledger.imports.pdf.files import clone_database, promote_database

    target = tmp_path / "ledger.db"
    staging = tmp_path / "staging.db"
    backups = tmp_path / "db-backups"
    clone_database(None, target)  # a real initialized SQLite DB
    clone_database(target, staging)

    # Normal finalize (base == target) -> "pre-finalize"
    b1 = promote_database(staging, target, backups, label="pre-finalize")
    assert b1 is not None and "-pre-finalize" in b1.name
    assert "-pre-fresh-rerun" not in b1.name

    # Fresh rerun (base differs from target) -> "pre-fresh-rerun"
    b2 = promote_database(staging, target, backups, label="pre-fresh-rerun")
    assert b2 is not None and "-pre-fresh-rerun" in b2.name


def test_promote_backup_label_defaults_to_pre_finalize(tmp_path: Path):
    from ledger.imports.pdf.files import clone_database, promote_database

    target = tmp_path / "ledger.db"
    staging = tmp_path / "staging.db"
    backups = tmp_path / "db-backups"
    clone_database(None, target)
    clone_database(target, staging)

    backup = promote_database(staging, target, backups)
    assert backup is not None and "-pre-finalize" in backup.name


def test_cleanup_prunes_finalized_import_runs_and_relocates_orphan_backups(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = _seed_cleanup_state(data_dir)

    runs = data_dir / "import-runs"
    old_run = runs / "20260501T000000Z"
    new_run = runs / "20260601T000000Z"
    inflight = runs / "20260602T000000Z"
    for d in (old_run, new_run, inflight):
        d.mkdir(parents=True)
    (old_run / "manifest.json").write_text('{"status": "finalized"}', encoding="utf-8")
    (new_run / "manifest.json").write_text('{"status": "finalized"}', encoding="utf-8")
    (inflight / "manifest.json").write_text('{"status": "needs_review"}', encoding="utf-8")

    # Two orphan root backups directly under data/ (not in db-backups/)
    (data_dir / "ledger.db.backup-20260604-210450").write_text("orphan1", encoding="utf-8")
    (data_dir / "ledger.db.backup-verification-20260605").write_text("orphan2", encoding="utf-8")

    DataCleanupService(db_path).clean(execute=True)

    # Oldest finalized import run pruned; newest finalized + in-flight run kept.
    assert not old_run.exists()
    assert new_run.exists()
    assert inflight.exists()
    # Orphan root backups no longer at root; relocated under db-backups/.
    assert not (data_dir / "ledger.db.backup-20260604-210450").exists()
    relocated = list((data_dir / "db-backups").glob("ledger.db.backup-*"))
    assert relocated, "orphan root backups should be relocated into db-backups/"
    assert _file_mode(data_dir / "db-backups") == 0o700
    assert all(_file_mode(path) == 0o600 for path in relocated)


def test_cleanup_dry_run_reports_import_runs_and_orphan_backups(tmp_path: Path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = _seed_cleanup_state(data_dir)

    runs = data_dir / "import-runs"
    old_run = runs / "20260501T000000Z"
    new_run = runs / "20260601T000000Z"
    for d in (old_run, new_run):
        d.mkdir(parents=True)
    (old_run / "manifest.json").write_text('{"status": "finalized"}', encoding="utf-8")
    (new_run / "manifest.json").write_text('{"status": "finalized"}', encoding="utf-8")
    (data_dir / "ledger.db.backup-20260604-210450").write_text("orphan1", encoding="utf-8")

    result = DataCleanupService(db_path).dry_run()
    assert result.counts["import_runs_to_prune"] == 1
    assert result.counts["orphan_root_backups"] == 1
    # Dry-run must not mutate the filesystem.
    assert old_run.exists()
    assert (data_dir / "ledger.db.backup-20260604-210450").exists()


def test_manifest_surfaces_left_in_inbox(tmp_path: Path):
    from ledger.imports.pdf.manifest import build_manifest

    run_dir = tmp_path / "20260601T000000Z"
    run_dir.mkdir()
    run_state = {
        "status": "finalized",
        "pdfs": [
            {"file_hash": "a", "skipped_reason": "unsupported_statement"},
            {
                "file_hash": "b",
                "finalize_warning": "original_pdf_missing_and_processed_pdf_not_found",
            },
            {"file_hash": "c"},  # clean, imported
        ],
        "duplicate_pdfs": [{"file_hash": "d", "skipped_reason": "duplicate_pdf_hash"}],
    }
    manifest = build_manifest(run_dir, run_state)
    left = manifest["left_in_inbox"]
    assert left["counts"] == {"unsupported": 1, "duplicates": 1, "failed_move": 1}
    assert len(left["items"]) == 3
    assert any("inbox" in a.lower() for a in manifest["next_actions"])


def test_manifest_left_in_inbox_empty_when_all_clean(tmp_path: Path):
    from ledger.imports.pdf.manifest import build_manifest

    run_dir = tmp_path / "20260601T000000Z"
    run_dir.mkdir()
    run_state = {
        "status": "finalized",
        "pdfs": [{"file_hash": "c"}, {"file_hash": "e"}],
    }
    manifest = build_manifest(run_dir, run_state)
    assert manifest["left_in_inbox"]["counts"] == {
        "unsupported": 0,
        "duplicates": 0,
        "failed_move": 0,
    }
    assert manifest["left_in_inbox"]["items"] == []
    assert not any("inbox" in a.lower() for a in manifest["next_actions"])


@pytest.mark.parametrize(
    "recoverable_state",
    [
        {"status": "finalizing"},
        {"status": "finalize_failed"},
        {"status": "failed", "failure_reason": "finalize_failed"},
    ],
)
def test_cleanup_never_prunes_recoverable_finalize_failed_run(
    tmp_path: Path, recoverable_state: dict[str, str]
):
    """F5: a finalize_failed run is RECOVERABLE (finalize() retries it, reading
    staging.db + csv/ from the run dir). manifest.json collapses its status to
    'failed', so it must not be pruned as a plain failed run even when a newer
    finalized run exists -- pruning would destroy its staging.db and break retry."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = _seed_cleanup_state(data_dir)

    runs = data_dir / "import-runs"
    old_finalized = runs / "20260501T000000Z"
    finfail = runs / "20260502T000000Z"
    new_finalized = runs / "20260601T000000Z"
    for d in (old_finalized, finfail, new_finalized):
        d.mkdir(parents=True)
    (old_finalized / "manifest.json").write_text('{"status": "finalized"}', encoding="utf-8")
    (new_finalized / "manifest.json").write_text('{"status": "finalized"}', encoding="utf-8")
    # manifest collapses finalize_failed -> failed; run.json carries the real state.
    (finfail / "manifest.json").write_text('{"status": "failed"}', encoding="utf-8")
    (finfail / "run.json").write_text(json.dumps(recoverable_state), encoding="utf-8")
    # Its staging.db is exactly what a retry needs and must not be deleted.
    (finfail / "staging.db").write_text("STAGING", encoding="utf-8")

    # Pin mtimes so retention deterministically keeps new_finalized (writing files
    # above bumps dir mtimes): old < finfail < new.
    import os

    os.utime(old_finalized, (1000, 1000))
    os.utime(finfail, (2000, 2000))
    os.utime(new_finalized, (3000, 3000))

    DataCleanupService(db_path).clean(execute=True)

    assert not old_finalized.exists(), "a plain finalized run should still be pruned"
    assert new_finalized.exists()
    assert finfail.exists(), "recoverable finalize_failed run must not be pruned"
    assert (finfail / "staging.db").exists(), "retry's staging.db must survive cleanup"
