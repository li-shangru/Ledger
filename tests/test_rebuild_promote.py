from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ledger.storage.database import ImportRunDB, TransferDB, init_database, session_scope

REPO_ROOT = Path(__file__).resolve().parents[1]


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _seed_live(path: Path, value: str = "live") -> None:
    with contextlib.closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("create table marker (value text not null)")
        connection.execute("insert into marker values (?)", (value,))


def _seed_candidate(path: Path, value: str = "candidate") -> None:
    init_database(path)
    now = datetime.now(UTC).replace(tzinfo=None)
    with session_scope(path) as session:
        session.add(
            TransferDB(
                txn_id=f"transfer-{value}",
                posting_date=date(2026, 7, 10),
                institution="synthetic",
                account="test",
                flow="transfer",
                amount_raw=Decimal("1.00"),
                description="SYNTHETIC",
                source_file="synthetic.csv",
                imported_at=now,
            )
        )
        session.add(
            ImportRunDB(
                run_at=now,
                source_path="synthetic",
                files_processed=1,
                transactions_imported=0,
                transfers_imported=1,
                duplicates_skipped=0,
                errors=0,
                parser_version="test",
            )
        )
    with contextlib.closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("create table marker (value text not null)")
        connection.execute("insert into marker values (?)", (value,))


def _marker(path: Path) -> str:
    with contextlib.closing(sqlite3.connect(path)) as connection, connection:
        return str(connection.execute("select value from marker").fetchone()[0])


def _last_option_value(arguments: list[str], option: str) -> str:
    values: list[str] = []
    for index, argument in enumerate(arguments):
        if argument == option:
            values.append(arguments[index + 1])
        elif argument.startswith(f"{option}="):
            values.append(argument.split("=", 1)[1])
    assert values, f"missing {option} in {arguments}"
    return values[-1]


def _assert_failed_promotion_is_clean(
    *,
    target: Path,
    target_bytes: bytes,
    target_marker: str,
    backup_dir: Path,
) -> None:
    assert target.read_bytes() == target_bytes
    assert _marker(target) == target_marker
    assert not list(target.parent.glob(f".{target.name}.rebuild-*.tmp*"))
    assert not list(target.parent.glob(f".{target.name}.rebuild-observe-*"))
    assert not list(backup_dir.glob("*"))


def test_capture_and_promote_preserve_wal_complete_private_rollback(tmp_path: Path) -> None:
    from ledger.maintenance.rebuild_promote import capture_rebuild_state, promote_rebuild

    target = tmp_path / "private" / "ledger.db"
    target.parent.mkdir()
    _seed_live(target, "main")
    keeper = sqlite3.connect(target)
    try:
        assert keeper.execute("pragma journal_mode=wal").fetchone()[0] == "wal"
        keeper.execute("pragma wal_autocheckpoint=0")
        keeper.execute("update marker set value = 'committed-in-wal'")
        keeper.commit()
        assert Path(f"{target}-wal").exists()

        state = tmp_path / "state" / "baseline.json"
        capture_rebuild_state(target, state)
        baseline = json.loads(state.read_text(encoding="utf-8"))
        with contextlib.closing(sqlite3.connect(baseline["snapshot"])) as snapshot, snapshot:
            assert snapshot.execute("select value from marker").fetchone()[0] == "committed-in-wal"
    finally:
        keeper.close()

    candidate = tmp_path / "candidate.db"
    _seed_candidate(candidate)
    previous_umask = os.umask(0o022)
    try:
        result = promote_rebuild(candidate, target, tmp_path / "backups", state)
    finally:
        os.umask(previous_umask)

    assert _marker(target) == "candidate"
    assert result.backup_path is not None
    assert _marker(result.backup_path) == "committed-in-wal"
    assert _mode(result.backup_path) == 0o600
    assert _mode(result.backup_path.parent) == 0o700
    assert _mode(target.parent / ".ledger.db.rebuild.lock") == 0o600


def test_rebuild_drift_after_capture_aborts_and_preserves_current_target(tmp_path: Path) -> None:
    from ledger.maintenance.rebuild_promote import (
        RebuildDriftError,
        capture_rebuild_state,
        promote_rebuild,
    )

    target = tmp_path / "ledger.db"
    _seed_live(target, "before")
    keeper = sqlite3.connect(target)
    try:
        assert keeper.execute("pragma journal_mode=wal").fetchone()[0] == "wal"
        keeper.execute("pragma wal_autocheckpoint=0")
        keeper.execute("pragma wal_checkpoint(truncate)")
        state = tmp_path / "baseline.json"
        capture_rebuild_state(target, state)
        # Same row count and unchanged main file: only a WAL-aware observation can
        # see this in-place update while the writer remains open.
        keeper.execute("update marker set value = 'concurrent-change'")
        keeper.commit()
        assert Path(f"{target}-wal").exists()

        candidate = tmp_path / "candidate.db"
        _seed_candidate(candidate)
        with pytest.raises(RebuildDriftError, match="changed during rebuild"):
            promote_rebuild(candidate, target, tmp_path / "backups", state)
    finally:
        keeper.close()

    assert _marker(target) == "concurrent-change"
    assert not list((tmp_path / "backups").glob("*.db"))
    assert not list(tmp_path.glob(".ledger.db.rebuild-*.tmp*"))


def test_rebuild_lock_is_nonblocking_and_private(tmp_path: Path) -> None:
    from ledger.maintenance.rebuild_promote import RebuildLockError, rebuild_target_lock

    target = tmp_path / "ledger.db"
    _seed_live(target)
    with (
        rebuild_target_lock(target),
        pytest.raises(RebuildLockError, match="already in progress"),
        rebuild_target_lock(target),
    ):
        raise AssertionError("unreachable")

    assert _mode(tmp_path / ".ledger.db.rebuild.lock") == 0o600


@pytest.mark.parametrize("publisher", ["import", "repair", "rebuild"])
def test_all_database_publishers_share_target_lock(tmp_path: Path, publisher: str) -> None:
    from ledger.imports.pdf.files import promote_database
    from ledger.maintenance.rebuild_promote import capture_rebuild_state, promote_rebuild
    from ledger.repair.statements import promote_candidate
    from ledger.storage.snapshots import (
        DatabaseTargetLockError,
        database_target_lock,
    )

    target = tmp_path / "ledger.db"
    _seed_live(target, "live")
    state = tmp_path / "baseline.json"
    capture_rebuild_state(target, state)
    candidate = tmp_path / "candidate.db"
    _seed_candidate(candidate, "candidate")

    with (
        database_target_lock(target),
        pytest.raises(
            DatabaseTargetLockError,
            match="already in progress",
        ),
    ):
        if publisher == "import":
            promote_database(candidate, target, tmp_path / "backups")
        elif publisher == "repair":
            promote_candidate(candidate, target)
        else:
            promote_rebuild(candidate, target, tmp_path / "backups", state)

    assert _marker(target) == "live"


def test_rebuild_promotes_through_open_target_wal(tmp_path: Path) -> None:
    from ledger.maintenance.rebuild_promote import capture_rebuild_state, promote_rebuild

    target = tmp_path / "ledger.db"
    _seed_live(target, "base")
    keeper = sqlite3.connect(target)
    try:
        assert keeper.execute("pragma journal_mode=wal").fetchone()[0] == "wal"
        keeper.execute("pragma wal_autocheckpoint=0")
        keeper.execute("pragma wal_checkpoint(truncate)")
        keeper.execute("update marker set value = 'committed-in-old-wal'")
        keeper.commit()

        state = tmp_path / "baseline.json"
        capture_rebuild_state(target, state)
        candidate = tmp_path / "candidate.db"
        _seed_candidate(candidate, "candidate")

        promote_rebuild(candidate, target, tmp_path / "backups", state)

        assert _marker(target) == "candidate"
        assert keeper.execute("select value from marker").fetchone()[0] == "candidate"
    finally:
        keeper.close()


def test_rebuild_publication_failure_preserves_target_and_cleans_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.maintenance import rebuild_promote

    target = tmp_path / "ledger.db"
    _seed_live(target, "old")
    state = tmp_path / "baseline.json"
    rebuild_promote.capture_rebuild_state(target, state)
    candidate = tmp_path / "candidate.db"
    _seed_candidate(candidate)
    real_publish = rebuild_promote.publish_database

    def fail_candidate_publish(source: Path, destination: Path) -> Path:
        if destination == target and ".rebuild-" in source.name:
            raise OSError("synthetic publication failure")
        return real_publish(source, destination)

    monkeypatch.setattr(rebuild_promote, "publish_database", fail_candidate_publish)
    with pytest.raises(OSError, match="synthetic publication failure"):
        rebuild_promote.promote_rebuild(candidate, target, tmp_path / "backups", state)

    assert _marker(target) == "old"
    assert not list(tmp_path.glob(".ledger.db.rebuild-*.tmp*"))
    assert not list((tmp_path / "backups").glob("*.db"))


def test_first_rebuild_supports_absent_target_and_uses_unique_temps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.maintenance import rebuild_promote

    target = tmp_path / "new" / "ledger.db"
    state = tmp_path / "baseline.json"
    rebuild_promote.capture_rebuild_state(target, state)
    candidate = tmp_path / "candidate.db"
    _seed_candidate(candidate, "first")
    copied_targets: list[Path] = []
    real_copy = rebuild_promote.copy_private_file

    def record_copy(source: Path, destination: Path) -> Path:
        copied_targets.append(destination)
        return real_copy(source, destination)

    monkeypatch.setattr(rebuild_promote, "copy_private_file", record_copy)
    result = rebuild_promote.promote_rebuild(candidate, target, tmp_path / "backups", state)

    assert result.backup_path is None
    assert _marker(target) == "first"
    assert copied_targets and all(path.parent == target.parent for path in copied_targets)
    assert len({path.name for path in copied_targets}) == len(copied_targets)
    assert all(".rebuild-" in path.name for path in copied_targets)


@pytest.mark.parametrize(
    "fault_phase",
    [
        "candidate_copy",
        "migration",
        "validation",
        "audit",
        "post_audit_validation",
        "final_drift_observation",
        "publication",
    ],
)
def test_rebuild_phase_faults_preserve_target_and_clean_private_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault_phase: str,
) -> None:
    from ledger.maintenance import rebuild_promote

    target = tmp_path / "ledger.db"
    _seed_live(target, "original")
    state = tmp_path / "baseline.json"
    rebuild_promote.capture_rebuild_state(target, state)
    candidate = tmp_path / "candidate.db"
    _seed_candidate(candidate, fault_phase)
    backup_dir = tmp_path / "backups"
    original_bytes = target.read_bytes()

    if fault_phase == "candidate_copy":

        def fail_copy(_source: Path, _destination: Path) -> Path:
            raise RuntimeError("synthetic candidate copy fault")

        monkeypatch.setattr(rebuild_promote, "copy_private_file", fail_copy)
    elif fault_phase == "migration":

        def fail_migration(_path: Path) -> None:
            raise RuntimeError("synthetic migration fault")

        monkeypatch.setattr(rebuild_promote, "run_migrations", fail_migration)
    elif fault_phase in {"validation", "post_audit_validation"}:
        real_validate = rebuild_promote.validate_database
        validation_calls = 0
        failure_call = 1 if fault_phase == "validation" else 2

        def fail_selected_validation(*args: object, **kwargs: object):
            nonlocal validation_calls
            validation_calls += 1
            if validation_calls == failure_call:
                raise RuntimeError(f"synthetic {fault_phase.replace('_', ' ')} fault")
            return real_validate(*args, **kwargs)

        monkeypatch.setattr(rebuild_promote, "validate_database", fail_selected_validation)
    elif fault_phase == "audit":

        def fail_audit(*_args: object, **_kwargs: object) -> None:
            raise RuntimeError("synthetic audit fault")

        monkeypatch.setattr(rebuild_promote, "record_db_operation", fail_audit)
    elif fault_phase == "final_drift_observation":
        real_match = rebuild_promote._snapshot_matches_baseline

        def fail_final_observation(
            observed_target: Path,
            baseline: dict[str, object],
            observation: Path,
        ) -> None:
            if observation.name.startswith(f".{target.name}.rebuild-observe-"):
                raise RuntimeError("synthetic final drift observation fault")
            real_match(observed_target, baseline, observation)

        monkeypatch.setattr(rebuild_promote, "_snapshot_matches_baseline", fail_final_observation)
    else:
        real_publish = rebuild_promote.publish_database

        def fail_publication(source: Path, destination: Path) -> Path:
            if destination == target and source.name.startswith(f".{target.name}.rebuild-"):
                raise RuntimeError("synthetic publication fault")
            return real_publish(source, destination)

        monkeypatch.setattr(rebuild_promote, "publish_database", fail_publication)

    with pytest.raises(RuntimeError, match=f"synthetic {fault_phase.replace('_', ' ')} fault"):
        rebuild_promote.promote_rebuild(candidate, target, backup_dir, state)

    _assert_failed_promotion_is_clean(
        target=target,
        target_bytes=original_bytes,
        target_marker="original",
        backup_dir=backup_dir,
    )


def test_repeated_successful_rebuilds_use_unique_candidate_and_backup_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.maintenance import rebuild_promote

    target = tmp_path / "ledger.db"
    _seed_live(target, "original")
    backup_dir = tmp_path / "backups"
    copied_candidates: list[Path] = []
    real_copy = rebuild_promote.copy_private_file

    def record_candidate_copy(source: Path, destination: Path) -> Path:
        copied_candidates.append(destination)
        return real_copy(source, destination)

    monkeypatch.setattr(rebuild_promote, "copy_private_file", record_candidate_copy)

    state_one = tmp_path / "baseline-one.json"
    rebuild_promote.capture_rebuild_state(target, state_one)
    candidate_one = tmp_path / "candidate-one.db"
    _seed_candidate(candidate_one, "first")
    first = rebuild_promote.promote_rebuild(candidate_one, target, backup_dir, state_one)

    state_two = tmp_path / "baseline-two.json"
    rebuild_promote.capture_rebuild_state(target, state_two)
    candidate_two = tmp_path / "candidate-two.db"
    _seed_candidate(candidate_two, "second")
    second = rebuild_promote.promote_rebuild(candidate_two, target, backup_dir, state_two)

    assert _marker(target) == "second"
    assert first.backup_path is not None
    assert second.backup_path is not None
    assert first.backup_path != second.backup_path
    assert _marker(first.backup_path) == "original"
    assert _marker(second.backup_path) == "first"
    assert len(copied_candidates) == 2
    assert copied_candidates[0] != copied_candidates[1]
    assert all(path.parent == target.parent for path in copied_candidates)
    assert all(not path.exists() for path in copied_candidates)


def test_rebuild_export_copy_is_private_and_skips_symlinks(tmp_path: Path) -> None:
    from ledger.maintenance.rebuild_promote import copy_rebuild_exports

    source = tmp_path / "generated"
    source.mkdir()
    (source / "summary.json").write_text('{"private": true}', encoding="utf-8")
    outside = tmp_path / "outside.csv"
    outside.write_text("private", encoding="utf-8")
    (source / "linked.csv").symlink_to(outside)

    copied = copy_rebuild_exports(source, tmp_path / "exports")

    assert copied == [tmp_path / "exports" / "summary.json"]
    assert _mode(copied[0]) == 0o600
    assert _mode(copied[0].parent) == 0o700
    assert not (tmp_path / "exports" / "linked.csv").exists()


def test_rebuild_wrapper_delegates_capture_promote_and_private_exports() -> None:
    script = (Path(__file__).resolve().parents[1] / "scripts" / "rebuild-db").read_text(
        encoding="utf-8"
    )

    assert "ensure_python_env analysis" in script
    assert "ledger.maintenance.rebuild_promote capture" in script
    assert "ledger.maintenance.rebuild_promote promote" in script
    assert "ledger.maintenance.rebuild_promote copy-exports" in script
    assert 'cp "$DB"' not in script
    assert 'cp "$TMP_DB"' not in script
    assert 'mv "$TMP_TARGET" "$DB"' not in script


@pytest.mark.parametrize(
    ("period_flag", "period_value"),
    [
        ("--year", "2025"),
        ("--month", "2025-06"),
        ("--quarter", "2025-Q2"),
    ],
)
def test_rebuild_wrapper_keeps_owned_paths_after_caller_args_and_forwards_period(
    tmp_path: Path,
    period_flag: str,
    period_value: str,
) -> None:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "scripts" / "rebuild-db", scripts / "rebuild-db")
    (scripts / "_common.sh").write_text(
        "\n".join(
            [
                'ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"',
                'VENV_PY="$ROOT/fake-python"',
                "ensure_python_env() {",
                '  [[ "$#" -eq 1 && "$1" == "analysis" ]] || return 91',
                "}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    fake_python = repo / "fake-python"
    fake_python.write_text(
        """#!/usr/bin/env bash
printf 'CALL' >> "${ARGV_LOG:?}"
printf '\037%s' "$@" >> "$ARGV_LOG"
printf '\n' >> "$ARGV_LOG"
exit 0
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)
    csv_source = repo / "csv"
    csv_source.mkdir()
    (csv_source / "statement.csv").write_text("synthetic\n", encoding="utf-8")
    argv_log = repo / "argv.log"
    out_dir = repo / "private-output"
    live_db = out_dir / "ledger.db"
    env = os.environ.copy()
    env.update(
        {
            "ARGV_LOG": str(argv_log),
            "LEDGER_SRC": str(csv_source),
            "LEDGER_OUT_DIR": str(out_dir),
            "LEDGER_DB": str(live_db),
            "LEDGER_FORMATS": "none",
        }
    )
    caller_arguments = [
        "--src=/caller/source",
        "--out-dir",
        "/caller/output",
        "--db=/caller/ledger.db",
        "--config",
        "/caller/defaults.toml",
        "--ledger-config=/caller/ledger.toml",
        "--merchant-rules",
        "/caller/merchants.toml",
        "--categories=/caller/categories.toml",
        period_flag,
        period_value,
    ]

    completed = subprocess.run(
        ["bash", str(scripts / "rebuild-db"), *caller_arguments],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    calls = [line.split("\x1f")[1:] for line in argv_log.read_text(encoding="utf-8").splitlines()]
    build_arguments = next(
        arguments for arguments in calls if arguments[:3] == ["-m", "ledger.cli", "build"]
    )
    assert _last_option_value(build_arguments, "--src") == str(csv_source)
    assert _last_option_value(build_arguments, "--ledger-config") == str(
        repo / "configs" / "ledger.toml"
    )
    assert _last_option_value(build_arguments, "--merchant-rules") == str(
        repo / "configs" / "merchants.toml"
    )
    assert _last_option_value(build_arguments, "--categories") == str(
        repo / "configs" / "categories.toml"
    )
    temporary_output = Path(_last_option_value(build_arguments, "--out-dir"))
    temporary_db = Path(_last_option_value(build_arguments, "--db"))
    assert temporary_output.name == "out"
    assert temporary_db.name == "ledger.db"
    assert temporary_output.parent == temporary_db.parent
    assert _last_option_value(build_arguments, period_flag) == period_value
