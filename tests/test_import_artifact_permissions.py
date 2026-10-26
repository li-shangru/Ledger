from __future__ import annotations

import contextlib
import os
import sqlite3
import stat
from pathlib import Path

import pytest


def _mode(path: Path, *, follow_symlinks: bool = True) -> int:
    metadata = path.stat() if follow_symlinks else path.lstat()
    return stat.S_IMODE(metadata.st_mode)


def test_ensure_private_dir_creates_private_components_and_tightens_final_dir(
    tmp_path: Path,
) -> None:
    from ledger.utils.private_files import ensure_private_dir

    existing = tmp_path / "existing"
    existing.mkdir(mode=0o755)
    existing.chmod(0o755)

    previous_umask = os.umask(0o022)
    try:
        nested = ensure_private_dir(tmp_path / "managed" / "nested" / "leaf")
        tightened = ensure_private_dir(existing)
    finally:
        os.umask(previous_umask)

    assert nested == tmp_path / "managed" / "nested" / "leaf"
    assert tightened == existing
    assert _mode(tmp_path / "managed") == 0o700
    assert _mode(tmp_path / "managed" / "nested") == 0o700
    assert _mode(nested) == 0o700
    assert _mode(existing) == 0o700


def test_private_directory_creation_syncs_each_new_component_before_descending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.utils import private_files
    from ledger.utils.private_files import pin_private_directory

    destination = tmp_path / "archive-root" / "account" / "2026"
    events: list[tuple[str, int | str]] = []
    real_mkdir = private_files.os.mkdir
    real_fsync = private_files.os.fsync

    def record_mkdir(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> None:
        events.append(("mkdir", os.fsdecode(path)))
        real_mkdir(path, mode, dir_fd=dir_fd)

    def record_fsync(descriptor: int) -> None:
        events.append(("fsync", descriptor))
        real_fsync(descriptor)

    monkeypatch.setattr(private_files.os, "mkdir", record_mkdir)
    monkeypatch.setattr(private_files.os, "fsync", record_fsync)

    with pin_private_directory(destination) as pinned:
        events.append(("yield", pinned.descriptor))

    mkdir_indexes = [index for index, event in enumerate(events) if event[0] == "mkdir"]
    yield_index = next(index for index, event in enumerate(events) if event[0] == "yield")
    assert mkdir_indexes
    assert all(
        any(event[0] == "fsync" for event in events[index + 1 : next_index])
        for index, next_index in zip(mkdir_indexes, [*mkdir_indexes[1:], yield_index], strict=True)
    )


def test_ensure_private_dir_rejects_symlinked_path_component(tmp_path: Path) -> None:
    from ledger.utils.private_files import ensure_private_dir

    outside = tmp_path / "outside"
    outside.mkdir()
    redirected = tmp_path / "redirected"
    redirected.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError, match="symlink"):
        ensure_private_dir(redirected / "run")

    assert not (outside / "run").exists()


def test_write_private_text_atomic_uses_unique_private_temps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.utils import private_files

    target = tmp_path / "state.json"
    real_replace = private_files.os.replace
    replaced_from: list[str] = []

    def inspect_replace(
        source: str | os.PathLike[str],
        destination: str | os.PathLike[str],
        **kwargs: object,
    ) -> None:
        source_name = os.fspath(source)
        replaced_from.append(source_name)
        assert os.fspath(destination) == target.name
        source_dir_fd = kwargs.get("src_dir_fd")
        assert isinstance(source_dir_fd, int)
        assert stat.S_IMODE(os.stat(source_name, dir_fd=source_dir_fd).st_mode) == 0o600
        real_replace(source, destination, **kwargs)

    monkeypatch.setattr(private_files.os, "replace", inspect_replace)

    previous_umask = os.umask(0o022)
    try:
        private_files.write_private_text_atomic(target, "first")
        private_files.write_private_text_atomic(target, "second")
    finally:
        os.umask(previous_umask)

    assert target.read_text(encoding="utf-8") == "second"
    assert _mode(target) == 0o600
    assert len(replaced_from) == 2
    assert replaced_from[0] != replaced_from[1]


def test_write_private_text_atomic_replace_failure_preserves_target_and_cleans_temp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.utils import private_files

    target = tmp_path / "state.json"
    target.write_text("old", encoding="utf-8")

    def fail_replace(_source: object, _destination: object, **_kwargs: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(private_files.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        private_files.write_private_text_atomic(target, "new")

    assert target.read_text(encoding="utf-8") == "old"
    assert not list(tmp_path.glob(".state.json.*.tmp"))


def test_private_atomic_replace_and_backup_cleanup_are_directory_durable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rename and rollback-link removal each have a containing-dir barrier."""
    from ledger.utils import private_files

    target = tmp_path / "state.json"
    target.write_text("old", encoding="utf-8")
    events: list[str] = []
    real_replace = private_files.os.replace
    real_unlink = private_files.os.unlink
    real_fsync = private_files.os.fsync

    def record_replace(source, destination, **kwargs):
        events.append("replace")
        return real_replace(source, destination, **kwargs)

    def record_unlink(name, **kwargs):
        if os.fsdecode(name).endswith(".rollback"):
            events.append("unlink-backup")
        return real_unlink(name, **kwargs)

    def record_fsync(descriptor: int) -> None:
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            events.append("fsync-directory")
        real_fsync(descriptor)

    monkeypatch.setattr(private_files.os, "replace", record_replace)
    monkeypatch.setattr(private_files.os, "unlink", record_unlink)
    monkeypatch.setattr(private_files.os, "fsync", record_fsync)

    private_files.write_private_text_atomic(target, "new")

    replace_index = events.index("replace")
    unlink_index = events.index("unlink-backup")
    assert "fsync-directory" in events[replace_index + 1 : unlink_index]
    assert "fsync-directory" in events[unlink_index + 1 :]
    assert target.read_text(encoding="utf-8") == "new"


@pytest.mark.parametrize("target_exists", [False, True])
def test_private_atomic_post_replace_directory_fsync_failure_rolls_back_durably(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_exists: bool,
) -> None:
    """A non-durable rename restores the prior directory state before returning."""
    from ledger.utils import private_files

    target = tmp_path / "state.json"
    if target_exists:
        target.write_text("old", encoding="utf-8")
    real_replace = private_files.os.replace
    real_fsync = private_files.os.fsync
    replace_finished = False
    injected = False
    rollback_directory_syncs = 0

    def mark_replace(source, destination, **kwargs):
        nonlocal replace_finished
        result = real_replace(source, destination, **kwargs)
        replace_finished = True
        return result

    def fail_first_post_replace_directory_sync(descriptor: int) -> None:
        nonlocal injected, rollback_directory_syncs
        if stat.S_ISDIR(os.fstat(descriptor).st_mode) and replace_finished:
            if not injected:
                injected = True
                raise OSError("synthetic post-replace directory fsync failure")
            rollback_directory_syncs += 1
        real_fsync(descriptor)

    monkeypatch.setattr(private_files.os, "replace", mark_replace)
    monkeypatch.setattr(private_files.os, "fsync", fail_first_post_replace_directory_sync)

    with pytest.raises(OSError, match="post-replace directory fsync failure"):
        private_files.write_private_text_atomic(target, "new")

    assert injected is True
    assert rollback_directory_syncs >= 1
    assert target.exists() is target_exists
    if target_exists:
        assert target.read_text(encoding="utf-8") == "old"
    assert not list(tmp_path.glob(".state.json.*.rollback"))


def test_private_atomic_cleanup_barrier_failure_keeps_durable_new_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Once rename durability is confirmed, cleanup failure must not restore stale bytes."""
    from ledger.utils import private_files

    target = tmp_path / "state.json"
    target.write_text("old", encoding="utf-8")
    real_replace = private_files.os.replace
    real_unlink = private_files.os.unlink
    real_fsync = private_files.os.fsync
    replace_finished = False
    backup_unlinked = False

    def mark_replace(source, destination, **kwargs):
        nonlocal replace_finished
        result = real_replace(source, destination, **kwargs)
        replace_finished = True
        return result

    def mark_backup_unlink(name, **kwargs):
        nonlocal backup_unlinked
        result = real_unlink(name, **kwargs)
        if os.fsdecode(name).endswith(".rollback"):
            backup_unlinked = True
        return result

    def fail_cleanup_barrier(descriptor: int) -> None:
        if replace_finished and backup_unlinked and stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("synthetic cleanup directory fsync failure")
        real_fsync(descriptor)

    monkeypatch.setattr(private_files.os, "replace", mark_replace)
    monkeypatch.setattr(private_files.os, "unlink", mark_backup_unlink)
    monkeypatch.setattr(private_files.os, "fsync", fail_cleanup_barrier)

    with pytest.raises(OSError, match="cleanup directory fsync failure"):
        private_files.write_private_text_atomic(target, "new")

    assert target.read_text(encoding="utf-8") == "new"
    assert not list(tmp_path.glob(".state.json.*.rollback"))


def test_write_private_text_atomic_rejects_symlink_destination(tmp_path: Path) -> None:
    from ledger.utils.private_files import write_private_text_atomic

    outside = tmp_path / "outside.json"
    outside.write_text("outside", encoding="utf-8")
    target = tmp_path / "state.json"
    target.symlink_to(outside)

    with pytest.raises(OSError, match="symlink"):
        write_private_text_atomic(target, "private")

    assert target.is_symlink()
    assert outside.read_text(encoding="utf-8") == "outside"


def test_copy_private_file_does_not_chmod_symlinked_source(tmp_path: Path) -> None:
    from ledger.utils.private_files import copy_private_file

    source = tmp_path / "inbox.pdf"
    source.write_bytes(b"private statement")
    source.chmod(0o644)
    source_link = tmp_path / "staged.pdf"
    source_link.symlink_to(source)
    source_link_mode = _mode(source_link, follow_symlinks=False)
    target = tmp_path / "archive" / "statement.pdf"

    previous_umask = os.umask(0o022)
    try:
        copied = copy_private_file(source_link, target)
    finally:
        os.umask(previous_umask)

    assert copied == target
    assert target.read_bytes() == b"private statement"
    assert _mode(target) == 0o600
    assert _mode(target.parent) == 0o700
    assert _mode(source) == 0o644
    assert _mode(source_link, follow_symlinks=False) == source_link_mode


def test_tighten_private_file_restricts_regular_file_and_rejects_symlink(tmp_path: Path) -> None:
    from ledger.utils.private_files import tighten_private_file

    regular = tmp_path / "artifact.db"
    regular.write_bytes(b"artifact")
    regular.chmod(0o644)
    outside = tmp_path / "outside.db"
    outside.write_bytes(b"outside")
    outside.chmod(0o644)
    linked = tmp_path / "linked.db"
    linked.symlink_to(outside)

    assert tighten_private_file(regular) == regular
    with pytest.raises(OSError, match="symlink"):
        tighten_private_file(linked)

    assert _mode(regular) == 0o600
    assert _mode(outside) == 0o644


def test_write_json_atomic_publishes_private_file_and_directory(tmp_path: Path) -> None:
    from ledger.utils.json_io import write_json_atomic

    target = tmp_path / "run" / "manifest.json"

    previous_umask = os.umask(0o022)
    try:
        write_json_atomic(target, {"status": "staged"})
    finally:
        os.umask(previous_umask)

    assert _mode(target.parent) == 0o700
    assert _mode(target) == 0o600


def test_import_run_directory_pointer_and_lock_are_private(tmp_path: Path) -> None:
    from ledger.imports.pdf.files import make_run_dir

    runs_dir = tmp_path / "import-runs"

    previous_umask = os.umask(0o022)
    try:
        run_dir = make_run_dir(runs_dir)
    finally:
        os.umask(previous_umask)

    assert _mode(runs_dir) == 0o700
    assert _mode(run_dir) == 0o700
    assert _mode(runs_dir / ".lock") == 0o600
    assert _mode(runs_dir / "latest-run.txt") == 0o600


def test_review_markdown_and_extractor_csv_are_private(tmp_path: Path) -> None:
    from ledger.imports.pdf.review import write_merchant_review_markdown
    from ledger.imports.pdf.statement_pdf_to_csv import write_csv

    review_path = tmp_path / "run" / "merchant-review.md"
    csv_path = tmp_path / "run" / "csv" / "2026-07" / "statement.csv"

    previous_umask = os.umask(0o022)
    try:
        write_merchant_review_markdown(review_path, [])
        write_csv(
            csv_path,
            [{"Description": "PRIVATE MERCHANT"}],
            ["Description"],
            dry_run=False,
        )
    finally:
        os.umask(previous_umask)

    assert _mode(review_path) == 0o600
    assert _mode(csv_path) == 0o600
    assert _mode(csv_path.parent) == 0o700
    assert _mode(csv_path.parent.parent) == 0o700


def test_staged_pdf_symlink_is_protected_by_private_dir_without_chmodding_source(
    tmp_path: Path,
) -> None:
    from ledger.imports.pdf.files import link_or_copy_pdf

    source = tmp_path / "inbox.pdf"
    source.write_bytes(b"statement")
    source.chmod(0o644)

    previous_umask = os.umask(0o022)
    try:
        staged = link_or_copy_pdf(source, tmp_path / "run" / "inbox")
    finally:
        os.umask(previous_umask)

    assert staged.is_symlink()
    assert _mode(staged.parent) == 0o700
    assert _mode(source) == 0o644


def test_staged_pdf_copy_fallback_is_private(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.imports.pdf.files import link_or_copy_pdf

    source = tmp_path / "inbox.pdf"
    source.write_bytes(b"statement")
    source.chmod(0o644)

    def reject_symlink(_self: Path, _target: object, **_kwargs: object) -> None:
        raise OSError("symlinks unavailable")

    monkeypatch.setattr(Path, "symlink_to", reject_symlink)

    previous_umask = os.umask(0o022)
    try:
        staged = link_or_copy_pdf(source, tmp_path / "run" / "inbox")
    finally:
        os.umask(previous_umask)

    assert not staged.is_symlink()
    assert staged.read_bytes() == b"statement"
    assert _mode(staged.parent) == 0o700
    assert _mode(staged) == 0o600
    assert _mode(source) == 0o644


def test_archived_pdf_is_private_and_source_is_removed(tmp_path: Path) -> None:
    from ledger.imports.pdf.statement_pdf_to_csv import move_pdf

    source = tmp_path / "inbox.pdf"
    source.write_bytes(b"statement")
    source.chmod(0o644)
    target = tmp_path / "processed" / "statement.pdf"

    previous_umask = os.umask(0o022)
    try:
        archived = move_pdf(source, target, dry_run=False)
    finally:
        os.umask(previous_umask)

    assert archived == target
    assert not source.exists()
    assert archived.read_bytes() == b"statement"
    assert _mode(archived.parent) == 0o700
    assert _mode(archived) == 0o600


def test_archive_unlink_failure_removes_published_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.imports.pdf.statement_pdf_to_csv import move_pdf

    source = tmp_path / "inbox.pdf"
    source.write_bytes(b"statement")
    target = tmp_path / "processed" / "statement.pdf"
    real_unlink = Path.unlink

    def fail_source_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if path == source:
            raise OSError("source unlink failed")
        real_unlink(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "unlink", fail_source_unlink)

    with pytest.raises(OSError, match="source unlink failed"):
        move_pdf(source, target, dry_run=False)

    assert source.exists()
    assert not target.exists()


def test_existing_processed_pdf_is_tightened_without_recopying(tmp_path: Path) -> None:
    from ledger.imports.pdf.files import sha256_file
    from ledger.repair.statements import prepare_pdf_targets

    source = tmp_path / "source.pdf"
    target = tmp_path / "processed" / "statement.pdf"
    source.write_bytes(b"statement")
    target.parent.mkdir()
    target.write_bytes(b"statement")
    target.chmod(0o644)

    prepare_pdf_targets(
        [
            {
                "source_pdf": str(source),
                "target_pdf": str(target),
                "source_hash": sha256_file(source),
            }
        ]
    )

    assert _mode(target.parent) == 0o700
    assert _mode(target) == 0o600


def test_statement_repair_run_csv_and_copied_pdf_are_private(tmp_path: Path) -> None:
    from ledger.imports.pdf.files import sha256_file
    from ledger.repair.statements import make_run_dir, prepare_pdf_targets, write_csv

    source = tmp_path / "statement.pdf"
    source.write_bytes(b"statement")
    source.chmod(0o644)
    target = tmp_path / "processed" / "statement.pdf"

    previous_umask = os.umask(0o022)
    try:
        run_dir = make_run_dir(tmp_path / "repair-runs")
        inventory = run_dir / "statement_inventory.csv"
        write_csv(inventory, [{"source_pdf": str(source)}])
        prepare_pdf_targets(
            [
                {
                    "source_pdf": str(source),
                    "target_pdf": str(target),
                    "source_hash": sha256_file(source),
                }
            ]
        )
    finally:
        os.umask(previous_umask)

    assert _mode(run_dir.parent) == 0o700
    assert _mode(run_dir) == 0o700
    assert _mode(inventory) == 0o600
    assert _mode(target.parent) == 0o700
    assert _mode(target) == 0o600
    assert _mode(source) == 0o644


def test_statement_repair_candidate_promotion_is_private(tmp_path: Path) -> None:
    from ledger.repair.statements import promote_candidate

    candidate = tmp_path / "candidate.db"
    target = tmp_path / "ledger.db"
    for path, value in ((candidate, "candidate"), (target, "live")):
        with contextlib.closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("create table marker (value text not null)")
            connection.execute("insert into marker values (?)", (value,))
    candidate.chmod(0o644)

    previous_umask = os.umask(0o022)
    try:
        promote_candidate(candidate, target)
    finally:
        os.umask(previous_umask)

    with contextlib.closing(sqlite3.connect(target)) as promoted, promoted:
        assert promoted.execute("select value from marker").fetchall() == [("candidate",)]
    assert _mode(target) == 0o600
