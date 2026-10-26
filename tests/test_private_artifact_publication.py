"""Privacy and crash-safety contracts for local financial artifacts."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd
import pytest

from ledger.audit.statements import StatementAuditService
from ledger.core import Summary
from ledger.exporting import ExportPackageService
from ledger.optimize import categories as category_optimize
from ledger.output.writers import write_all
from ledger.planning import BudgetService
from ledger.storage.database import TransactionDB, init_database, session_scope
from ledger.ui import smoke as ui_smoke
from ledger.utils import private_files
from ledger.utils.private_files import (
    private_atomic_binary,
    private_atomic_text,
    write_private_text_atomic,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@contextmanager
def _umask(mask: int) -> Iterator[None]:
    previous = os.umask(mask)
    try:
        yield
    finally:
        os.umask(previous)


def _summary() -> Summary:
    return Summary(
        generated_at=datetime(2026, 1, 16, 12, 0, 0),
        parser_version="test-parser",
        config_versions={},
        source_path="synthetic",
        filters={},
        total_scanned=0,
        total_processed=0,
        duplicates_removed=0,
        collisions=0,
        exceptions=0,
        transactions_count=0,
        transfers_count=0,
        by_institution={},
        by_flow={},
        by_category={},
        cashflow_income=Decimal("0.00"),
        cashflow_expense=Decimal("0.00"),
        cashflow_net=Decimal("0.00"),
        by_quarter={},
    )


def _seed_export_db(db_path: Path) -> None:
    init_database(db_path)
    posted = date(2026, 5, 1)
    with session_scope(db_path) as session:
        session.add(
            TransactionDB(
                txn_id="private-artifact-test",
                txn_fingerprint="private-artifact-test-fingerprint",
                posting_date=posted,
                transaction_date=posted,
                institution="test-bank",
                account="test-account",
                account_type="Chequing",
                flow="expense",
                cashflow_type="expense",
                include_in_cashflow=True,
                currency="CAD",
                amount_raw=Decimal("12.34"),
                amount_signed=Decimal("-12.34"),
                description="TEST MERCHANT",
                merchant="TEST MERCHANT",
                category="Other",
                source_file="statement.csv",
                source_path="private/statement.csv",
                source_period="2026-05",
                source_row=1,
                imported_at=datetime.now(UTC).replace(tzinfo=None),
                parser_version="test",
                day_of_week=posted.weekday(),
                day_of_month=posted.day,
                week_of_year=posted.isocalendar()[1],
                is_weekend=False,
                is_month_start=True,
                is_month_end=False,
            )
        )


def test_private_atomic_text_is_private_and_preserves_old_target_on_failure(
    tmp_path: Path,
) -> None:
    target = tmp_path / "exports" / "transactions.CSV"
    target.parent.mkdir()
    target.write_text("known-good", encoding="utf-8")

    previous_umask = os.umask(0o022)
    try:
        with (
            pytest.raises(OSError, match="simulated write fault"),
            private_atomic_text(target) as handle,
        ):
            assert stat.S_IMODE(os.fstat(handle.fileno()).st_mode) == 0o600
            handle.write("partial")
            raise OSError("simulated write fault")
    finally:
        os.umask(previous_umask)

    assert target.read_text(encoding="utf-8") == "known-good"
    assert not list(target.parent.glob(f".{target.name}.*.tmp"))


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_private_atomic_text_cleans_temp_on_base_exception(
    tmp_path: Path, interruption: type[BaseException]
) -> None:
    target = tmp_path / "exports" / "transactions.csv"
    target.parent.mkdir()
    target.write_text("known-good", encoding="utf-8")

    with pytest.raises(interruption), private_atomic_text(target) as handle:
        handle.write("partial")
        raise interruption()

    assert target.read_text(encoding="utf-8") == "known-good"
    assert not list(target.parent.glob(f".{target.name}.*.tmp"))


def test_private_atomic_binary_cleans_descriptor_and_temp_when_fchmod_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "exports" / "report.zip"
    captured_descriptors: list[int] = []
    real_fchmod = private_files.os.fchmod

    def fail_fchmod(descriptor: int, mode: int) -> None:
        captured_descriptors.append(descriptor)
        raise OSError("simulated fchmod failure")

    monkeypatch.setattr(private_files.os, "fchmod", fail_fchmod)
    try:
        with (
            pytest.raises(OSError, match="simulated fchmod failure"),
            private_atomic_binary(target),
        ):
            pass
        leaked = list(target.parent.glob(f".{target.name}.*.tmp"))
        leaked_descriptors = []
        for descriptor in captured_descriptors:
            try:
                os.fstat(descriptor)
            except OSError:
                continue
            leaked_descriptors.append(descriptor)
    finally:
        monkeypatch.setattr(private_files.os, "fchmod", real_fchmod)
        for descriptor in captured_descriptors:
            with suppress(OSError):
                os.close(descriptor)
        for path in target.parent.glob(f".{target.name}.*.tmp"):
            path.unlink()

    assert leaked == []
    assert leaked_descriptors == []


def test_private_atomic_text_fails_closed_on_parent_directory_swap(tmp_path: Path) -> None:
    parent = tmp_path / "exports"
    parent.mkdir()
    target = parent / "transactions.csv"
    target.write_text("known-good", encoding="utf-8")
    moved_parent = tmp_path / "moved-exports"
    redirect = tmp_path / "redirect"
    redirect.mkdir()

    try:
        with (
            pytest.raises(OSError, match=r"changed|symlink"),
            private_atomic_text(target) as handle,
        ):
            parent.rename(moved_parent)
            parent.symlink_to(redirect, target_is_directory=True)
            handle.write("partial")
    finally:
        if parent.is_symlink():
            parent.unlink()
        if moved_parent.exists():
            moved_parent.rename(parent)

    assert target.read_text(encoding="utf-8") == "known-good"
    assert not (redirect / target.name).exists()
    assert not list(parent.glob(f".{target.name}.*.tmp"))
    assert not list(redirect.glob(f".{target.name}.*.tmp"))


def test_private_atomic_text_rejects_real_directory_substitution_before_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "exports"
    parent.mkdir()
    target = parent / "transactions.csv"
    target.write_text("known-good", encoding="utf-8")
    moved_parent = tmp_path / "moved-exports"
    substitute_parent = tmp_path / "substitute-exports"
    substitute_parent.mkdir()
    substitute_target = substitute_parent / target.name
    substitute_target.write_text("outside", encoding="utf-8")
    real_chmod = private_files.os.chmod
    real_fchmod = private_files.os.fchmod
    swapped = False

    def swap_parent() -> None:
        nonlocal swapped
        if swapped:
            return
        swapped = True
        parent.rename(moved_parent)
        substitute_parent.rename(parent)

    def chmod_then_swap(path: object, mode: int, **kwargs: object) -> None:
        real_chmod(path, mode, **kwargs)
        if Path(path) == parent:
            swap_parent()

    def fchmod_then_swap(descriptor: int, mode: int) -> None:
        real_fchmod(descriptor, mode)
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            swap_parent()

    monkeypatch.setattr(private_files.os, "chmod", chmod_then_swap)
    monkeypatch.setattr(private_files.os, "fchmod", fchmod_then_swap)
    try:
        with (
            pytest.raises(OSError, match="changed"),
            private_atomic_text(target) as handle,
        ):
            handle.write("financial-secret")
    finally:
        if parent.exists():
            parent.rename(substitute_parent)
        if moved_parent.exists():
            moved_parent.rename(parent)

    assert target.read_text(encoding="utf-8") == "known-good"
    assert substitute_target.read_text(encoding="utf-8") == "outside"


def test_private_atomic_text_detects_parent_swap_during_final_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "exports"
    parent.mkdir()
    target = parent / "transactions.csv"
    target.write_text("known-good", encoding="utf-8")
    moved_parent = tmp_path / "moved-exports"
    substitute_parent = tmp_path / "substitute-exports"
    substitute_parent.mkdir()
    substitute_target = substitute_parent / target.name
    substitute_target.write_text("outside", encoding="utf-8")
    real_replace = private_files.os.replace
    swapped = False

    def swap_then_replace(source: object, destination: object, **kwargs: object) -> None:
        nonlocal swapped
        if not swapped and os.fspath(destination) == target.name:
            swapped = True
            parent.rename(moved_parent)
            substitute_parent.rename(parent)
        real_replace(source, destination, **kwargs)

    monkeypatch.setattr(private_files.os, "replace", swap_then_replace)
    try:
        with (
            pytest.raises(OSError, match="changed"),
            private_atomic_text(target) as handle,
        ):
            handle.write("financial-secret")
    finally:
        if parent.exists():
            parent.rename(substitute_parent)
        if moved_parent.exists():
            moved_parent.rename(parent)

    assert target.read_text(encoding="utf-8") == "known-good"
    assert substitute_target.read_text(encoding="utf-8") == "outside"
    assert not list(parent.glob(f".{target.name}.*.rollback"))


def test_private_atomic_text_cleans_rollback_link_on_prepublish_identity_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent = tmp_path / "exports"
    parent.mkdir()
    target = parent / "transactions.csv"
    target.write_text("known-good", encoding="utf-8")
    moved_target = parent / "moved-transactions.csv"
    substitute = parent / "substitute.csv"
    substitute.write_text("outside", encoding="utf-8")
    real_link = private_files.os.link
    swapped = False

    def link_then_swap(source: object, destination: object, **kwargs: object) -> None:
        nonlocal swapped
        real_link(source, destination, **kwargs)
        if not swapped:
            swapped = True
            target.rename(moved_target)
            substitute.rename(target)

    monkeypatch.setattr(private_files.os, "link", link_then_swap)
    try:
        with (
            pytest.raises(OSError, match="changed before publication"),
            private_atomic_text(target) as handle,
        ):
            handle.write("financial-secret")
    finally:
        if target.exists():
            target.rename(substitute)
        if moved_target.exists():
            moved_target.rename(target)

    assert target.read_text(encoding="utf-8") == "known-good"
    assert substitute.read_text(encoding="utf-8") == "outside"
    assert not list(parent.glob(f".{target.name}.*.rollback"))
    assert not list(parent.glob(f".{target.name}.*.tmp"))


def test_private_atomic_text_rejects_final_symlink_inserted_before_publish(
    tmp_path: Path,
) -> None:
    target = tmp_path / "exports" / "transactions.csv"
    target.parent.mkdir()
    outside = tmp_path / "outside.csv"
    outside.write_text("outside", encoding="utf-8")

    with pytest.raises(OSError, match="symlink"), private_atomic_text(target) as handle:
        target.symlink_to(outside)
        handle.write("private")

    assert target.is_symlink()
    assert outside.read_text(encoding="utf-8") == "outside"
    assert not list(target.parent.glob(f".{target.name}.*.tmp"))


def test_tighten_private_file_rejects_same_name_regular_file_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "ledger.db"
    target.write_text("financial-secret", encoding="utf-8")
    target.chmod(0o644)
    moved_target = tmp_path / "moved-ledger.db"
    substitute = tmp_path / "substitute.db"
    substitute.write_text("outside", encoding="utf-8")
    substitute.chmod(0o644)
    real_chmod = private_files.os.chmod
    real_fchmod = private_files.os.fchmod
    swapped = False

    def swap_files() -> None:
        nonlocal swapped
        if swapped:
            return
        swapped = True
        target.rename(moved_target)
        substitute.rename(target)

    def swap_then_chmod(path: object, mode: int, **kwargs: object) -> None:
        if Path(path) == target:
            swap_files()
        real_chmod(path, mode, **kwargs)

    def swap_then_fchmod(descriptor: int, mode: int) -> None:
        if stat.S_ISREG(os.fstat(descriptor).st_mode):
            swap_files()
        real_fchmod(descriptor, mode)

    monkeypatch.setattr(private_files.os, "chmod", swap_then_chmod)
    monkeypatch.setattr(private_files.os, "fchmod", swap_then_fchmod)
    try:
        with pytest.raises(OSError, match="changed"):
            private_files.tighten_private_file(target)
    finally:
        if target.exists():
            target.rename(substitute)
        if moved_target.exists():
            moved_target.rename(target)

    assert target.read_text(encoding="utf-8") == "financial-secret"
    assert _mode(target) == 0o600
    assert substitute.read_text(encoding="utf-8") == "outside"
    assert _mode(substitute) == 0o644


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_output_writers_publish_private_artifacts_under_ambient_umask(tmp_path: Path) -> None:
    output_dir = tmp_path / "generated"

    with _umask(0o022):
        outputs = write_all(
            output_dir,
            [],
            [],
            [],
            _summary(),
            formats=["csv", "parquet", "json"],
        )

    assert _mode(output_dir) == 0o700
    assert outputs
    assert {_mode(path) for path in outputs.values()} == {0o600}


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_export_service_publishes_every_format_privately(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    _seed_export_db(db_path)
    service = ExportPackageService(db_path)
    dest_dir = tmp_path / "exports"

    with _umask(0o022):
        results = [
            service.create_package(
                dest_dir=dest_dir,
                preset="full-local-audit",
                export_format=export_format,
            )
            for export_format in ("csv", "jsonl", "parquet", "zip")
        ]

    assert _mode(dest_dir) == 0o700
    assert {_mode(result.path) for result in results} == {0o600}


def test_export_csv_write_fault_preserves_existing_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "exports" / "transactions.csv"
    target.parent.mkdir()
    target.write_text("known-good", encoding="utf-8")

    def fail_after_partial_write(_self, handle, *args, **kwargs) -> None:
        handle.write("partial")
        raise OSError("simulated pandas write fault")

    monkeypatch.setattr(pd.DataFrame, "to_csv", fail_after_partial_write)

    with pytest.raises(OSError, match="simulated pandas write fault"):
        ExportPackageService._safe_to_csv(pd.DataFrame([{"value": "private"}]), target)

    assert target.read_text(encoding="utf-8") == "known-good"
    assert not list(target.parent.glob(".transactions.csv.*.tmp"))


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_planning_audit_and_optimizer_artifacts_are_private(tmp_path: Path) -> None:
    budget_db = tmp_path / "budget.db"
    budget_path = tmp_path / "planning" / "budgets.csv"
    audit_root = tmp_path / "audit-runs"
    optimize_root = tmp_path / "optimize-runs"
    budget_path.parent.mkdir(mode=0o755)
    budget_path.parent.chmod(0o755)

    with _umask(0o022):
        budget_result = BudgetService(budget_db).export_csv(budget_path)

        audit_service = StatementAuditService()
        audit_run = audit_service._make_run_dir(audit_root)
        audit_csv = audit_run / "inventory.csv"
        audit_json = audit_run / "summary.json"
        audit_md = audit_run / "findings.md"
        audit_service._write_csv(audit_csv, [{"description": "private"}])
        audit_service._write_json(audit_json, {"status": "private"})
        audit_service._write_findings(
            audit_md, [], [], {}, start_date=date(2043, 6, 1), end_date=date(2043, 6, 30)
        )

        optimize_run = category_optimize.make_run_dir(optimize_root)
        optimize_csv = optimize_run / "recommendations.csv"
        optimize_json = optimize_run / "summary.json"
        category_optimize.write_csv(optimize_csv, [{"merchant": "private"}])
        category_optimize.write_json(optimize_json, {"status": "private"})

    assert budget_result.ok
    assert _mode(budget_path.parent) == 0o755
    for directory in (
        audit_root,
        audit_run,
        optimize_root,
        optimize_run,
    ):
        assert _mode(directory) == 0o700
    for artifact in (
        budget_path,
        audit_csv,
        audit_json,
        audit_md,
        optimize_csv,
        optimize_json,
    ):
        assert _mode(artifact) == 0o600


class _ScreenshotPage:
    def __init__(self, *, fail: bool = False):
        self.fail = fail

    def screenshot(self, *, full_page: bool) -> bytes:
        assert full_page is False
        if self.fail:
            raise OSError("simulated screenshot fault")
        return b"complete"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission contract")
def test_ui_smoke_screenshot_is_private(tmp_path: Path) -> None:
    target = tmp_path / "screenshots" / "overview.png"

    with _umask(0o022):
        ui_smoke._capture_screenshot(_ScreenshotPage(), target)

    assert _mode(target.parent) == 0o700
    assert _mode(target) == 0o600
    assert target.read_bytes() == b"complete"


def test_ui_smoke_screenshot_fault_preserves_existing_artifact(tmp_path: Path) -> None:
    target = tmp_path / "screenshots" / "overview.png"
    target.parent.mkdir()
    target.write_bytes(b"known-good")

    with pytest.raises(OSError, match="simulated screenshot fault"):
        ui_smoke._capture_screenshot(_ScreenshotPage(fail=True), target)

    assert target.read_bytes() == b"known-good"
    assert not list(target.parent.glob(".overview.png.*.tmp"))


@pytest.mark.skipif(not Path("/tmp").is_symlink(), reason="platform /tmp is not a symlink alias")
def test_ui_smoke_accepts_platform_tmp_alias_without_weakening_other_symlink_checks(
    tmp_path: Path,
) -> None:
    target = Path("/tmp") / f"ledger-{os.getpid()}-{tmp_path.name}" / "overview.png"
    try:
        ui_smoke._capture_screenshot(_ScreenshotPage(), target)
        assert target.read_bytes() == b"complete"
        assert _mode(target.parent.resolve()) == 0o700
        assert _mode(target.resolve()) == 0o600
    finally:
        shutil.rmtree(target.parent.resolve(), ignore_errors=True)

    real_dir = tmp_path / "real"
    real_dir.mkdir()
    redirected = tmp_path / "redirected"
    redirected.symlink_to(real_dir, target_is_directory=True)
    with pytest.raises(OSError, match="symlink"):
        ui_smoke._capture_screenshot(_ScreenshotPage(), redirected / "overview.png")


@pytest.mark.skipif(not Path("/tmp").is_symlink(), reason="platform /tmp is not a symlink alias")
def test_private_text_writer_accepts_platform_tmp_alias(tmp_path: Path) -> None:
    target = Path("/tmp") / f"ledger-{os.getpid()}-{tmp_path.name}" / "summary.json"
    try:
        write_private_text_atomic(target, '{"private": true}')
        assert target.read_text(encoding="utf-8") == '{"private": true}'
        assert _mode(target.parent.resolve()) == 0o700
        assert _mode(target.resolve()) == 0o600
    finally:
        shutil.rmtree(target.parent.resolve(), ignore_errors=True)


def _run(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    child_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return subprocess.run(
        command, cwd=cwd, env=child_env, capture_output=True, text=True, check=False
    )


@pytest.fixture
def hostile_git_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Path, bytes, bytes]]:
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    clean_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    subprocess.run(
        ["git", "init", "-q"],
        cwd=ambient,
        env=clean_env,
        capture_output=True,
        text=True,
        check=True,
    )
    (ambient / "ambient.txt").write_text("ambient", encoding="utf-8")
    subprocess.run(
        ["git", "add", "ambient.txt"],
        cwd=ambient,
        env=clean_env,
        capture_output=True,
        text=True,
        check=True,
    )
    config = ambient / ".git" / "config"
    index = ambient / ".git" / "index"
    config_before = config.read_bytes()
    index_before = index.read_bytes()
    monkeypatch.setenv("GIT_DIR", str(ambient / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(ambient))
    yield ambient, config_before, index_before

    assert config.read_bytes() == config_before
    assert index.read_bytes() == index_before


def _hook_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    hook_dir = repo / "scripts" / "git-hooks"
    hook_dir.mkdir(parents=True)
    for name in ("pre-commit", "_is_export_header"):
        shutil.copy2(REPO_ROOT / "scripts" / "git-hooks" / name, hook_dir / name)
    initialized = _run(["git", "init", "-q"], cwd=repo)
    assert initialized.returncode == 0, initialized.stderr
    return repo, hook_dir / "pre-commit"


def test_precommit_blocks_uppercase_sensitive_binary_extensions(
    tmp_path: Path, hostile_git_context: tuple[Path, bytes, bytes]
) -> None:
    repo, hook = _hook_repo(tmp_path)
    sensitive_paths = (
        "private/statement.PDF",
        "private/ledger.DB",
        "private/report.PARQUET",
        "private/ledger.db-wal",
        "private/ledger.sqlite3-journal",
    )
    for relative in sensitive_paths:
        path = repo / relative
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"synthetic private payload")
    staged = _run(["git", "add", *sensitive_paths], cwd=repo)
    assert staged.returncode == 0, staged.stderr

    result = _run(["bash", str(hook)], cwd=repo)
    output = result.stdout + result.stderr

    assert result.returncode != 0, output
    assert "BLOCKED" in output
    for relative in sensitive_paths:
        assert relative in output


@pytest.mark.parametrize("suffix", ["CSV", "JSONL"])
def test_precommit_blocks_uppercase_transaction_export_extensions(
    tmp_path: Path, suffix: str, hostile_git_context: tuple[Path, bytes, bytes]
) -> None:
    repo, hook = _hook_repo(tmp_path)
    export = repo / f"private/report.{suffix}"
    export.parent.mkdir()
    content = (
        '{"txn_id":"abc","merchant":"PRIVATE MERCHANT","amount":"-12.34"}\n'
        if suffix == "JSONL"
        else "txn_id,merchant,amount\nabc,PRIVATE MERCHANT,-12.34\n"
    )
    export.write_text(content, encoding="utf-8")
    staged = _run(["git", "add", str(export.relative_to(repo))], cwd=repo)
    assert staged.returncode == 0, staged.stderr

    result = _run(["bash", str(hook)], cwd=repo)
    output = result.stdout + result.stderr

    assert result.returncode != 0, output
    assert "financial-export column pattern" in output


def test_precommit_allows_safe_uppercase_statement_csv(
    tmp_path: Path, hostile_git_context: tuple[Path, bytes, bytes]
) -> None:
    repo, hook = _hook_repo(tmp_path)
    statement = repo / "fixtures/statement.CSV"
    statement.parent.mkdir()
    statement.write_text(
        "Ledger Format,Date,Description,Amount,Currency,Reference,Institution,Account Type\nledger-mapped-v1,2026-01-15,GROCERY STORE,12.34,CAD,,harbor,Chequing\n",
        encoding="utf-8",
    )
    staged = _run(["git", "add", str(statement.relative_to(repo))], cwd=repo)
    assert staged.returncode == 0, staged.stderr

    result = _run(["bash", str(hook)], cwd=repo)
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    assert "BLOCKED" not in output
