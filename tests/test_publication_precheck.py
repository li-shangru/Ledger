"""Interpretation checks run at the final SQLite or no-clobber publication boundary."""

import sqlite3
from contextlib import closing

import pytest

from ledger.storage import snapshots


def seed_database(path, value):
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE snapshot_rows (value TEXT NOT NULL)")
        connection.execute("INSERT INTO snapshot_rows VALUES (?)", (value,))
    path.chmod(0o600)


def rows(path):
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute("SELECT value FROM snapshot_rows").fetchall()


@pytest.mark.parametrize("with_guard", [False, True])
def test_existing_prepublication_check_runs_under_sqlite_write_exclusion(tmp_path, with_guard):
    candidate = tmp_path / "candidate.db"
    target = tmp_path / "target.db"
    seed_database(candidate, "candidate-state")
    seed_database(target, "live-state")
    calls = []

    def guard(connection):
        assert connection.execute("SELECT value FROM snapshot_rows").fetchall() == [("live-state",)]
        calls.append("guard")

    def prepublication_check():
        with (
            closing(sqlite3.connect(target, timeout=0)) as competing_writer,
            pytest.raises(sqlite3.OperationalError, match="locked"),
        ):
            competing_writer.execute("INSERT INTO snapshot_rows VALUES ('concurrent-state')")
        calls.append("check")

    assert (
        snapshots.publish_database(
            candidate,
            target,
            guard=guard if with_guard else None,
            prepublication_check=prepublication_check,
        )
        == target
    )
    assert calls == (["guard", "check"] if with_guard else ["check"])
    assert rows(target) == [("candidate-state",)]


def test_absent_prepublication_check_observes_drift_after_snapshot(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate.db"
    target = tmp_path / "target.db"
    seed_database(candidate, "candidate-state")
    original_write = snapshots._write_snapshot_connection
    prepared = []

    def finish_snapshot_then_drift(*args, **kwargs):
        original_write(*args, **kwargs)
        prepared.append("changed")

    def prepublication_check():
        assert prepared == ["changed"]
        raise RuntimeError("synthetic late interpretation drift")

    monkeypatch.setattr(snapshots, "_write_snapshot_connection", finish_snapshot_then_drift)
    with pytest.raises(RuntimeError, match="synthetic late interpretation drift"):
        snapshots.publish_database(candidate, target, prepublication_check=prepublication_check)
    assert not target.exists()
    assert not list(tmp_path.glob(".target.db.no-clobber-*"))
    assert rows(candidate) == [("candidate-state",)]


def test_absent_prepublication_check_preserves_concurrently_created_target(tmp_path):
    candidate = tmp_path / "candidate.db"
    target = tmp_path / "target.db"
    seed_database(candidate, "candidate-state")

    def prepublication_check():
        seed_database(target, "concurrent-state")

    with pytest.raises(FileExistsError):
        snapshots.publish_database(candidate, target, prepublication_check=prepublication_check)
    assert rows(target) == [("concurrent-state",)]
    assert not list(tmp_path.glob(".target.db.no-clobber-*"))


def test_absent_prepublication_check_follows_sync_and_precedes_atomic_link(tmp_path, monkeypatch):
    candidate = tmp_path / "candidate.db"
    target = tmp_path / "target.db"
    seed_database(candidate, "candidate-state")
    calls = []
    original_sync = snapshots._fsync_pinned_directory
    original_link = snapshots.os.link

    def sync(parent):
        original_sync(parent)
        calls.append("sync")

    def link(source, destination, **kwargs):
        if destination == target.name:
            calls.append("link")
        return original_link(source, destination, **kwargs)

    monkeypatch.setattr(snapshots, "_fsync_pinned_directory", sync)
    monkeypatch.setattr(snapshots.os, "link", link)
    snapshots.publish_database(
        candidate,
        target,
        prepublication_check=lambda: calls.append("check"),
    )
    assert calls == ["sync", "check", "link", "sync", "sync"]
    assert rows(target) == [("candidate-state",)]
