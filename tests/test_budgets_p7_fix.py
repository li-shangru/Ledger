"""P7 fix-round regressions for durable budget planning writes."""

from __future__ import annotations

import ast
import csv
import inspect
import json
import os
import selectors
import shutil
import sqlite3
import stat
import subprocess
import sys
import textwrap
import threading
import time
from contextlib import closing, contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ledger.cli import app
from ledger.core.results import RunResult
from ledger.mcp.server import LedgerMCPService, call_tool
from ledger.planning import BudgetService
from ledger.query import LedgerQueryService
from ledger.storage.database import (
    AccountDB,
    BudgetDB,
    DBOperationAuditDB,
    TransactionDB,
    init_database,
    session_scope,
)
from ledger.storage.snapshots import (
    DatabaseTargetLockError,
    DatabaseWriteCommittedError,
    database_target_lock,
)
from ledger.utils.private_files import PinnedPrivateFile, pin_private_directory

runner = CliRunner()


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _budget_rows(db_path: Path) -> list[tuple[str, str, str, str]]:
    with closing(sqlite3.connect(db_path)) as connection:
        return [
            tuple(str(value) for value in row)
            for row in connection.execute(
                "SELECT budget_month, category, amount, notes FROM budgets "
                "ORDER BY budget_month, category"
            )
        ]


def _audit_operations(db_path: Path) -> list[str]:
    with session_scope(db_path) as session:
        return [
            str(row.operation)
            for row in session.query(DBOperationAuditDB).order_by(DBOperationAuditDB.id).all()
        ]


def _goal_rows(db_path: Path) -> list[tuple[str, str, str, str]]:
    with closing(sqlite3.connect(db_path)) as connection:
        return [
            tuple(str(value) for value in row)
            for row in connection.execute(
                "SELECT name, account_id, target_amount, status FROM savings_goals ORDER BY name"
            )
        ]


def _logical_database_dump(db_path: Path) -> str:
    with closing(sqlite3.connect(db_path)) as connection:
        return "\n".join(connection.iterdump())


def _seed_account(db_path: Path) -> str:
    account_id = "synthetic-account-id"
    with session_scope(db_path) as session:
        session.add(
            AccountDB(
                account_id=account_id,
                institution="Synthetic Bank",
                account_number="1001",
                account_type="checking",
                account_subtype="Chequing",
                first_transaction_date=date(2026, 1, 1),
                last_transaction_date=date(2026, 1, 31),
                is_active=True,
                transaction_count=1,
                avg_monthly_transactions=Decimal("1"),
                is_credit_card=False,
                detected_credit_limit=None,
                created_at=_now(),
                updated_at=_now(),
            )
        )
    return account_id


def _seed_budget(db_path: Path, *, category: str = "Original", amount: str = "10") -> None:
    BudgetService(db_path).replace_budgets(
        [{"month": "*", "category": category, "amount": amount, "notes": "before"}]
    )


def _write_import(source: Path, *, category: str = "Replacement", amount: str = "20") -> None:
    source.write_text(
        f"month,category,amount,notes\n*,{category},{amount},after\n",
        encoding="utf-8",
    )


def test_import_backup_and_replacement_stay_bound_to_one_target_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connect-time lexical decoy must never become the advertised backup."""
    from ledger.storage import snapshots

    db_path = tmp_path / "ledger.db"
    decoy = tmp_path / "decoy.db"
    displaced = tmp_path / "original.db"
    init_database(db_path)
    _seed_budget(db_path)
    init_database(decoy)
    _seed_budget(decoy, category="Decoy", amount="999")
    source = tmp_path / "import.csv"
    _write_import(source)
    original_connect = snapshots.sqlite3.connect
    original_bound_reader = snapshots.bound_database_read_connection
    swapped = False
    path_handoff_seen = False

    @contextmanager
    def reject_path_handoff(source_object: object, **kwargs: object):
        nonlocal path_handoff_seen
        is_live_path_handoff = (
            not isinstance(source_object, PinnedPrivateFile)
            and Path(os.fspath(source_object)).absolute() == db_path.absolute()
        )
        if is_live_path_handoff:
            path_handoff_seen = True
            os.replace(db_path, displaced)
            os.replace(decoy, db_path)
            try:
                with original_bound_reader(source_object, **kwargs) as connection:
                    yield connection
            finally:
                os.replace(db_path, decoy)
                os.replace(displaced, db_path)
            return
        with original_bound_reader(source_object, **kwargs) as connection:
            yield connection

    def connect_with_canonical_aba(database: object, *args: object, **kwargs: object):
        nonlocal swapped
        database_text = os.fspath(database) if isinstance(database, (str, os.PathLike)) else ""
        in_legacy_snapshot = any(
            frame.function == "_snapshot_database" for frame in inspect.stack()
        )
        if not swapped and in_legacy_snapshot and str(db_path) in database_text:
            swapped = True
            os.replace(db_path, displaced)
            os.replace(decoy, db_path)
            try:
                return original_connect(database, *args, **kwargs)
            finally:
                os.replace(db_path, decoy)
                os.replace(displaced, db_path)
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(snapshots.sqlite3, "connect", connect_with_canonical_aba)
    monkeypatch.setattr(
        snapshots,
        "bound_database_read_connection",
        reject_path_handoff,
    )
    result = BudgetService(db_path, prepare_schema=False).import_csv(source, execute=True)

    assert result.ok
    assert not path_handoff_seen, "the backup must pass the live pin into its bound reader"
    assert not swapped, "the bound backup must not reopen the canonical target pathname"
    assert _budget_rows(db_path) == [("*", "Replacement", "20", "after")]
    backup = next((tmp_path / "db-backups").glob("*.db"))
    assert _budget_rows(backup) == [("*", "Original", "10", "before")]


@pytest.mark.parametrize(
    ("method_name", "operation", "initial", "replacement"),
    [
        (
            "replace_budgets",
            "budget_replace",
            [{"month": "*", "category": "Original", "amount": "10", "notes": "before"}],
            [{"month": "*", "category": "Replacement", "amount": "20", "notes": "after"}],
        ),
        (
            "replace_goals",
            "goals_replace",
            [
                {
                    "name": "Original goal",
                    "account_id": "synthetic-account-id",
                    "target_amount": "10",
                    "status": "active",
                }
            ],
            [
                {
                    "name": "Replacement goal",
                    "account_id": "synthetic-account-id",
                    "target_amount": "20",
                    "status": "active",
                }
            ],
        ),
    ],
)
def test_replacement_and_its_audit_are_one_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    operation: str,
    initial: list[dict[str, str]],
    replacement: list[dict[str, str]],
) -> None:
    """An audit insertion failure rolls back the corresponding replacement."""
    from ledger.planning import service as planning_service

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    service = BudgetService(db_path)
    if method_name == "replace_budgets":
        assert service.replace_budgets(initial).ok
        before = _budget_rows(db_path)
    else:
        _seed_account(db_path)
        assert service.replace_goals(initial).ok
        before = _goal_rows(db_path)
    audits_before = _audit_operations(db_path)
    original_audit = planning_service.record_db_operation

    def fail_selected_audit(*args: object, **kwargs: object) -> None:
        if kwargs.get("operation") == operation:
            assert isinstance(kwargs.get("connection"), sqlite3.Connection)
            raise sqlite3.OperationalError("synthetic audit failure")
        original_audit(*args, **kwargs)

    monkeypatch.setattr(planning_service, "record_db_operation", fail_selected_audit)
    with pytest.raises(sqlite3.OperationalError, match="synthetic audit failure"):
        getattr(service, method_name)(replacement)

    if method_name == "replace_budgets":
        assert _budget_rows(db_path) == before
    else:
        assert _goal_rows(db_path) == before
    assert _audit_operations(db_path) == audits_before


@pytest.mark.parametrize("method_name", ["replace_budgets", "replace_goals"])
@pytest.mark.parametrize("failing_boundary", ["pin", "lock"])
def test_planning_replacement_reports_commit_when_outer_boundary_exit_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    method_name: str,
    failing_boundary: str,
) -> None:
    """A post-commit pin/lock exit failure must never invite a duplicate retry."""
    from ledger.planning import service as planning_service

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    service = BudgetService(db_path)
    if method_name == "replace_budgets":
        replacement = [{"month": "*", "category": "Replacement", "amount": "20", "notes": "after"}]
    else:
        account_id = _seed_account(db_path)
        replacement = [
            {
                "name": "Replacement goal",
                "account_id": account_id,
                "target_amount": "20",
                "status": "active",
            }
        ]

    if failing_boundary == "pin":
        original_boundary = service._pin_mutable_target

        @contextmanager
        def fail_after_pin_exit():
            with original_boundary() as pinned:
                yield pinned
            raise OSError("synthetic post-commit pin cleanup failure")

        monkeypatch.setattr(service, "_pin_mutable_target", fail_after_pin_exit)
    else:
        original_boundary = planning_service.database_target_lock

        @contextmanager
        def fail_after_lock_exit(path: Path):
            with original_boundary(path) as lock_path:
                yield lock_path
            raise OSError("synthetic post-commit lock cleanup failure")

        monkeypatch.setattr(planning_service, "database_target_lock", fail_after_lock_exit)

    with pytest.raises(DatabaseWriteCommittedError) as raised:
        getattr(service, method_name)(replacement)

    assert "synthetic" not in str(raised.value)
    if method_name == "replace_budgets":
        assert _budget_rows(db_path) == [("*", "Replacement", "20", "after")]
    else:
        assert _goal_rows(db_path) == [("Replacement goal", "synthetic-account-id", "20", "active")]


def test_default_schema_preparation_obeys_the_target_lock(tmp_path: Path) -> None:
    db_path = tmp_path / "missing.db"

    with database_target_lock(db_path), pytest.raises(DatabaseTargetLockError):
        BudgetService(db_path)

    assert not db_path.exists()


def test_execute_refuses_stale_schema_after_backup_then_migrate_and_retry_succeeds(
    tmp_path: Path,
) -> None:
    from ledger.storage.migrations import run_migrations

    db_path = tmp_path / "legacy.db"
    source = tmp_path / "budgets.csv"
    _write_import(source)
    with closing(sqlite3.connect(db_path)) as connection:
        connection.executescript(
            """
            CREATE TABLE budgets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                budget_month VARCHAR(7) NOT NULL,
                category VARCHAR(100) NOT NULL,
                amount NUMERIC(12, 2) NOT NULL,
                notes TEXT NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL
            );
            INSERT INTO budgets (
                budget_month, category, amount, notes, created_at, updated_at
            ) VALUES ('*', 'Legacy', 7, 'before migration', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
            """
        )
    os.chmod(db_path, 0o600)
    inode_before = db_path.stat().st_ino
    before = db_path.read_bytes()

    refused = BudgetService(db_path, prepare_schema=False).import_csv(source, execute=True)

    assert not refused.ok
    assert "ledger db migrate" in " ".join(refused.errors)
    assert db_path.stat().st_ino == inode_before
    assert db_path.read_bytes() == before
    assert _budget_rows(db_path) == [("*", "Legacy", "7", "before migration")]
    backup = Path(refused.artifacts["backup"])
    assert backup.is_file()
    with closing(sqlite3.connect(backup)) as connection:
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name = 'schema_migrations'"
            ).fetchone()
            is None
        )
    assert _budget_rows(backup) == [("*", "Legacy", "7", "before migration")]

    migrated = run_migrations(db_path)
    retried = BudgetService(db_path, prepare_schema=False).import_csv(source, execute=True)

    assert migrated.ok
    assert retried.ok, retried.errors
    assert db_path.stat().st_ino == inode_before
    assert _budget_rows(db_path) == [("*", "Replacement", "20", "after")]


@pytest.mark.parametrize("drift", ["migration-set", "missing-table", "missing-column"])
def test_budget_import_refuses_noncurrent_live_schema_after_retaining_exact_backup(
    tmp_path: Path,
    drift: str,
) -> None:
    from ledger.storage.migrations import MIGRATIONS, run_migrations

    db_path = tmp_path / "ledger.db"
    source = tmp_path / "budgets.csv"
    init_database(db_path)
    assert run_migrations(db_path).ok
    _seed_budget(db_path)
    _write_import(source)
    with closing(sqlite3.connect(db_path)) as connection, connection:
        if drift == "migration-set":
            connection.execute(
                "DELETE FROM schema_migrations WHERE version = ?",
                (MIGRATIONS[-1].version,),
            )
        elif drift == "missing-table":
            connection.execute("DROP TABLE report_snapshots")
        else:
            connection.execute("ALTER TABLE schema_migrations DROP COLUMN description")
    before = db_path.read_bytes()
    before_budgets = _budget_rows(db_path)
    before_audits = _audit_operations(db_path)
    before_dump = _logical_database_dump(db_path)

    result = BudgetService(db_path, prepare_schema=False).import_csv(source, execute=True)

    assert not result.ok
    assert "ledger db migrate" in " ".join(result.errors)
    assert result.details["committed"] is False
    assert db_path.read_bytes() == before
    assert _budget_rows(db_path) == before_budgets
    assert _audit_operations(db_path) == before_audits
    backup = Path(result.artifacts["backup"])
    assert backup.is_file()
    assert _logical_database_dump(backup) == before_dump


def test_budget_import_accepts_an_extra_nullable_legacy_column(tmp_path: Path) -> None:
    from ledger.storage.migrations import run_migrations

    db_path = tmp_path / "ledger.db"
    source = tmp_path / "budgets.csv"
    init_database(db_path)
    assert run_migrations(db_path).ok
    _seed_budget(db_path)
    _write_import(source)
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute("ALTER TABLE budgets ADD COLUMN legacy_note TEXT")

    result = BudgetService(db_path, prepare_schema=False).import_csv(source, execute=True)

    assert result.ok, result.errors
    assert _budget_rows(db_path) == [("*", "Replacement", "20", "after")]


def test_budget_import_refuses_an_extra_required_column_without_a_default(
    tmp_path: Path,
) -> None:
    from ledger.storage.migrations import run_migrations

    db_path = tmp_path / "ledger.db"
    source = tmp_path / "budgets.csv"
    init_database(db_path)
    assert run_migrations(db_path).ok
    _seed_budget(db_path)
    _write_import(source)
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executescript(
            """
            CREATE TABLE budgets_incompatible (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                budget_month VARCHAR(7) NOT NULL,
                category VARCHAR(100) NOT NULL,
                amount NUMERIC(12, 2) NOT NULL,
                notes TEXT NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                legacy_required TEXT NOT NULL
            );
            INSERT INTO budgets_incompatible (
                id, budget_month, category, amount, notes, created_at, updated_at,
                legacy_required
            )
            SELECT id, budget_month, category, amount, notes, created_at, updated_at,
                   'legacy'
            FROM budgets;
            DROP TABLE budgets;
            ALTER TABLE budgets_incompatible RENAME TO budgets;
            """
        )
    before = db_path.read_bytes()
    before_rows = _budget_rows(db_path)

    result = BudgetService(db_path, prepare_schema=False).import_csv(source, execute=True)

    assert not result.ok
    assert "ledger db migrate" in " ".join(result.errors)
    assert db_path.read_bytes() == before
    assert _budget_rows(db_path) == before_rows
    backup = Path(result.artifacts["backup"])
    assert backup.is_file()
    assert _budget_rows(backup) == before_rows


def test_budget_import_checks_schema_only_after_the_exact_backup_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.planning import service as planning_service

    db_path = tmp_path / "ledger.db"
    source = tmp_path / "budgets.csv"
    service = BudgetService(db_path)
    _seed_budget(db_path)
    _write_import(source)
    original_backup = planning_service.backup_database
    original_check = planning_service._read_budget_import_schema_issues
    observed: list[str] = []

    def observe_backup(*args: object, **kwargs: object):
        observed.append("backup")
        return original_backup(*args, **kwargs)

    def observe_check(*args: object, **kwargs: object) -> dict[str, int]:
        observed.append("schema")
        return original_check(*args, **kwargs)

    monkeypatch.setattr(planning_service, "backup_database", observe_backup)
    monkeypatch.setattr(planning_service, "_read_budget_import_schema_issues", observe_check)

    result = service.import_csv(source, execute=True)

    assert result.ok, result.errors
    assert observed == ["backup", "schema"]


def test_import_schema_migration_cannot_mutate_a_lexical_decoy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact pre-write inode, not a swapped pathname, owns schema validation."""
    from ledger.planning import service as planning_service

    db_path = tmp_path / "legacy.db"
    decoy = tmp_path / "decoy.db"
    displaced = tmp_path / "displaced.db"
    source = tmp_path / "budgets.csv"
    _write_import(source)
    BudgetService(db_path)
    _seed_budget(db_path, category="Original", amount="7")
    BudgetService(decoy)
    _seed_budget(decoy, category="Decoy", amount="999")
    decoy_audits_before = _audit_operations(decoy)
    original_check = planning_service._budget_import_schema_issues
    swapped = False

    def swap_during_schema_check(connection: sqlite3.Connection) -> dict[str, int]:
        nonlocal swapped
        swapped = True
        os.replace(db_path, displaced)
        os.replace(decoy, db_path)
        try:
            return original_check(connection)
        finally:
            os.replace(db_path, decoy)
            os.replace(displaced, db_path)

    monkeypatch.setattr(
        planning_service,
        "_budget_import_schema_issues",
        swap_during_schema_check,
    )
    result = BudgetService(db_path, prepare_schema=False).import_csv(source, execute=True)

    assert swapped
    assert result.ok, result.errors
    assert _budget_rows(db_path) == [("*", "Replacement", "20", "after")]
    assert _budget_rows(decoy) == [("*", "Decoy", "999", "before")]
    assert _audit_operations(decoy) == decoy_audits_before
    backup = Path(result.artifacts["backup"])
    assert _budget_rows(backup) == [("*", "Original", "7", "before")]


def test_budget_import_never_erases_an_unrelated_commit_after_its_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A table-scoped replacement cannot publish a stale whole-database clone."""
    db_path = tmp_path / "ledger.db"
    source = tmp_path / "budgets.csv"
    init_database(db_path)
    _seed_budget(db_path)
    _write_import(source)
    original_replace = BudgetService._replace_budgets_pinned
    concurrent_commit_visible = False

    def commit_unrelated_row_then_replace(
        self: BudgetService,
        rows: list[dict[str, object]],
        **kwargs: object,
    ):
        nonlocal concurrent_commit_visible
        with closing(sqlite3.connect(db_path)) as connection:
            connection.execute(
                "INSERT INTO config_metadata (key, value) VALUES (?, ?)",
                ("synthetic-concurrent-key", "committed after backup"),
            )
            connection.commit()
            concurrent_commit_visible = connection.execute(
                "SELECT value FROM config_metadata WHERE key = ?",
                ("synthetic-concurrent-key",),
            ).fetchone() == ("committed after backup",)
        return original_replace(self, rows, **kwargs)

    monkeypatch.setattr(
        BudgetService,
        "_replace_budgets_pinned",
        commit_unrelated_row_then_replace,
    )

    result = BudgetService(db_path, prepare_schema=False).import_csv(source, execute=True)

    assert concurrent_commit_visible
    assert result.ok, result.errors
    assert _budget_rows(db_path) == [("*", "Replacement", "20", "after")]
    with closing(sqlite3.connect(db_path)) as connection:
        assert connection.execute(
            "SELECT value FROM config_metadata WHERE key = ?",
            ("synthetic-concurrent-key",),
        ).fetchone() == ("committed after backup",)


def test_budget_writer_serializes_with_an_ordinary_competing_sqlite_writer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A waited-out canonical writer gets a fresh pinned journal and no residue."""
    from ledger.storage import snapshots

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _seed_budget(db_path)
    assert not any(Path(f"{db_path}{suffix}").exists() for suffix in ("-journal", "-wal", "-shm"))
    created_journal_identities: list[tuple[int, int]] = []
    real_create_journal = snapshots._create_pinned_writer_journal
    writer_ready = threading.Event()

    def observed_create_journal(*args: object, **kwargs: object):
        journal = real_create_journal(*args, **kwargs)
        created_journal_identities.append(journal.identity)
        writer_ready.set()
        return journal

    monkeypatch.setattr(snapshots, "_create_pinned_writer_journal", observed_create_journal)
    started = threading.Event()
    finished = threading.Event()
    outcomes: list[object] = []

    def replace_after_competitor() -> None:
        started.set()
        try:
            outcomes.append(
                BudgetService(db_path, prepare_schema=False).replace_budgets(
                    [{"month": "*", "category": "Replacement", "amount": "20"}]
                )
            )
        except BaseException as exc:  # pragma: no cover - asserted below
            outcomes.append(exc)
        finally:
            finished.set()

    with closing(sqlite3.connect(db_path, isolation_level=None)) as competitor:
        competitor.execute("BEGIN IMMEDIATE")
        worker = threading.Thread(target=replace_after_competitor)
        worker.start()
        assert started.wait(timeout=2)
        assert writer_ready.wait(timeout=2)
        assert not finished.wait(timeout=0.2)
        competitor.execute(
            "INSERT INTO config_metadata (key, value) VALUES (?, ?)",
            ("ordinary-writer", "must survive"),
        )
        competitor.commit()
        worker.join(timeout=10)

    assert not worker.is_alive()
    assert len(created_journal_identities) == 2
    assert created_journal_identities[0] != created_journal_identities[1]
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], RunResult) and outcomes[0].ok
    assert _budget_rows(db_path) == [("*", "Replacement", "20", "")]
    with closing(sqlite3.connect(db_path)) as connection:
        assert connection.execute(
            "SELECT value FROM config_metadata WHERE key = ?",
            ("ordinary-writer",),
        ).fetchone() == ("must survive",)
    assert not any(Path(f"{db_path}{suffix}").exists() for suffix in ("-journal", "-wal", "-shm"))
    assert not any(path.name.startswith(snapshots._ALIAS_PREFIX) for path in tmp_path.iterdir())
    assert not (tmp_path / snapshots._ALIAS_PARENT_LOCK).exists()


def test_import_result_never_claims_rollback_after_the_replacement_commits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-commit validation failure cannot be presented as an atomic rollback."""
    from ledger.storage import snapshots

    db_path = tmp_path / "ledger.db"
    source = tmp_path / "budgets.csv"
    init_database(db_path)
    _seed_budget(db_path)
    _write_import(source)
    original_borrow = snapshots._borrow_or_pin_database_file
    exits = 0

    @contextmanager
    def fail_only_after_writer_exit(source: object):
        nonlocal exits
        with original_borrow(source) as pinned:
            yield pinned
        exits += 1
        if exits == 3:
            raise OSError("synthetic post-commit pin validation drift")

    monkeypatch.setattr(
        snapshots,
        "_borrow_or_pin_database_file",
        fail_only_after_writer_exit,
    )
    result = BudgetService(db_path, prepare_schema=False).import_csv(source, execute=True)

    replacement_committed = _budget_rows(db_path) == [("*", "Replacement", "20", "after")]
    audits_committed = _audit_operations(db_path)[-2:] == ["backup", "budget_replace"]
    assert not result.ok
    assert result.details["committed"] is True
    assert "commit succeeded" in " ".join(result.errors).casefold()
    assert "no replacement was committed" not in " ".join(result.errors).casefold()
    assert replacement_committed
    assert audits_committed
    backup = Path(result.artifacts["backup"])
    assert backup.is_file()
    assert _budget_rows(backup) == [("*", "Original", "10", "before")]


def test_import_truthfully_reports_a_final_pin_failure_after_live_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failure after the live commit cannot be described as no replacement."""
    db_path = tmp_path / "ledger.db"
    source = tmp_path / "budgets.csv"
    init_database(db_path)
    _seed_budget(db_path)
    _write_import(source)
    original_replace = BudgetService._replace_budgets_pinned
    original_validate = PinnedPrivateFile.validate
    live_write_returned = False

    def observed_replace(self: BudgetService, *args: object, **kwargs: object):
        nonlocal live_write_returned
        result = original_replace(self, *args, **kwargs)
        live_write_returned = True
        return result

    def fail_after_live_write(self: PinnedPrivateFile) -> None:
        if live_write_returned and self.path == db_path:
            raise OSError("synthetic final live-pin drift")
        original_validate(self)

    monkeypatch.setattr(BudgetService, "_replace_budgets_pinned", observed_replace)
    monkeypatch.setattr(PinnedPrivateFile, "validate", fail_after_live_write)

    result = BudgetService(db_path, prepare_schema=False).import_csv(source, execute=True)

    assert live_write_returned
    assert not result.ok
    assert result.details["committed"] is True
    assert "commit succeeded" in " ".join(result.errors).casefold()
    assert "no replacement was committed" not in " ".join(result.errors).casefold()
    assert _budget_rows(db_path) == [("*", "Replacement", "20", "after")]
    assert _audit_operations(db_path)[-2:] == ["backup", "budget_replace"]
    backup = Path(result.artifacts["backup"])
    assert backup.is_file()
    assert _budget_rows(backup) == [("*", "Original", "10", "before")]


def test_import_preserves_committed_truth_for_postcommit_keyboard_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.planning import service as planning_service

    db_path = tmp_path / "ledger.db"
    source = tmp_path / "budgets.csv"
    init_database(db_path)
    _seed_budget(db_path)
    _write_import(source)

    def interrupt_after_commit() -> None:
        raise KeyboardInterrupt("synthetic postcommit interrupt")

    monkeypatch.setattr(planning_service, "invalidate_db_caches", interrupt_after_commit)

    result = BudgetService(db_path, prepare_schema=False).import_csv(source, execute=True)

    assert not result.ok
    assert result.details["committed"] is True
    assert "commit succeeded" in " ".join(result.errors).casefold()
    assert "synthetic" not in " ".join(result.errors)
    assert _budget_rows(db_path) == [("*", "Replacement", "20", "after")]
    assert _audit_operations(db_path)[-2:] == ["backup", "budget_replace"]
    backup = Path(result.artifacts["backup"])
    assert backup.is_file()
    assert _budget_rows(backup) == [("*", "Original", "10", "before")]


def test_post_backup_schema_failure_returns_and_exposes_the_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every post-backup preparation failure returns the recoverable artifact."""
    from ledger.planning import service as planning_service

    db_path = tmp_path / "ledger.db"
    source = tmp_path / "budgets.csv"
    init_database(db_path)
    _seed_budget(db_path)
    _write_import(source)
    before_rows = _budget_rows(db_path)
    audits_before = _audit_operations(db_path)

    def fail_schema(_connection: sqlite3.Connection) -> dict[str, int]:
        raise ValueError("synthetic migration refusal")

    monkeypatch.setattr(planning_service, "_budget_import_schema_issues", fail_schema)
    result = BudgetService(db_path, prepare_schema=False).import_csv(source, execute=True)

    assert not result.ok
    assert _budget_rows(db_path) == before_rows
    assert _audit_operations(db_path) == audits_before
    backup = Path(result.artifacts["backup"])
    assert backup.is_file()
    assert _budget_rows(backup) == before_rows


def test_budget_replace_audit_finishes_at_the_transaction_recording_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The recorded operation timestamp is captured with the rows before commit."""
    from ledger.planning import service as planning_service

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    original_writer = planning_service.audited_database_write_connection
    inside_writer = False
    clock_phases: list[bool] = []

    @contextmanager
    def observed_writer(*args: object, **kwargs: object):
        nonlocal inside_writer
        with original_writer(*args, **kwargs) as connection:
            inside_writer = True
            try:
                yield connection
            finally:
                inside_writer = False

    def observed_now() -> datetime:
        clock_phases.append(inside_writer)
        return datetime(2026, 1, 1, 0, 0, len(clock_phases))

    monkeypatch.setattr(planning_service, "audited_database_write_connection", observed_writer)
    monkeypatch.setattr(planning_service, "utc_now", observed_now)

    result = BudgetService(db_path, prepare_schema=False).replace_budgets(
        [{"month": "*", "category": "Dining", "amount": "20", "notes": ""}]
    )

    with session_scope(db_path) as session:
        audit_finished_at = (
            session.query(DBOperationAuditDB)
            .filter_by(operation="budget_replace")
            .order_by(DBOperationAuditDB.id.desc())
            .first()
        ).finished_at
    assert clock_phases[-1] is True
    assert result.finished_at == audit_finished_at


@pytest.mark.parametrize("button_label", ["Save Budgets", "Save Goals"])
def test_planning_save_lock_refusal_is_content_free_in_real_ui(
    tmp_path: Path,
    button_label: str,
) -> None:
    from streamlit.testing.v1 import AppTest
    from test_ui_render_invocation import _driver_script, _seed_db

    db_path = tmp_path / "ledger.db"
    _seed_db(db_path)
    service = BudgetService(db_path)
    assert service.replace_budgets(
        [{"month": "*", "category": "Dining", "amount": "10", "notes": ""}]
    ).ok
    account_id = _seed_account(db_path)
    assert service.replace_goals(
        [
            {
                "name": "Synthetic goal",
                "account_id": account_id,
                "target_amount": "100",
                "target_date": None,
                "status": "active",
                "notes": "",
            }
        ]
    ).ok
    original_replace_budgets = BudgetService.replace_budgets
    original_replace_goals = BudgetService.replace_goals
    before = db_path.read_bytes()
    setup = textwrap.dedent(
        """
        from ledger.planning import BudgetService
        from ledger.storage.snapshots import DatabaseTargetLockError

        def _refuse(*_args, **_kwargs):
            raise DatabaseTargetLockError('/private/synthetic/operator/path')

        BudgetService.replace_budgets = _refuse
        BudgetService.replace_goals = _refuse
        """
    )
    os.environ["_LEDGER_APPTEST_DB"] = str(db_path)
    try:
        at = AppTest.from_file(
            str(_driver_script(tmp_path, "ledger.ui.pages.budgets_goals", setup_code=setup)),
            default_timeout=30,
        ).run()
        confirmation = (
            "save budget replacements"
            if button_label == "Save Budgets"
            else "save goal replacements"
        )
        input_label = (
            "Confirm budget save" if button_label == "Save Budgets" else "Confirm goal save"
        )
        next(item for item in at.text_input if item.label == input_label).input(confirmation).run()
        next(item for item in at.button if item.label == button_label).click().run()

        rendered_errors = [str(item.value) for item in at.error]
        assert not at.exception
        assert rendered_errors == [
            "Planning changes could not be saved; retry after other database work completes."
        ]
        assert "/private" not in " ".join(rendered_errors)
        assert db_path.read_bytes() == before
    finally:
        BudgetService.replace_budgets = original_replace_budgets
        BudgetService.replace_goals = original_replace_goals


@pytest.mark.parametrize("button_label", ["Save Budgets", "Save Goals"])
def test_planning_save_reports_a_committed_validation_failure_truthfully(
    tmp_path: Path,
    button_label: str,
) -> None:
    from streamlit.testing.v1 import AppTest
    from test_ui_render_invocation import _driver_script, _seed_db

    db_path = tmp_path / "ledger.db"
    _seed_db(db_path)
    service = BudgetService(db_path)
    assert service.replace_budgets(
        [{"month": "*", "category": "Dining", "amount": "10", "notes": ""}]
    ).ok
    account_id = _seed_account(db_path)
    assert service.replace_goals(
        [
            {
                "name": "Synthetic goal",
                "account_id": account_id,
                "target_amount": "100",
                "target_date": None,
                "status": "active",
                "notes": "",
            }
        ]
    ).ok
    original_replace_budgets = BudgetService.replace_budgets
    original_replace_goals = BudgetService.replace_goals
    setup = textwrap.dedent(
        """
        from ledger.planning import BudgetService
        from ledger.storage.snapshots import DatabaseWriteCommittedError

        def _committed(*_args, **_kwargs):
            raise DatabaseWriteCommittedError('/private/synthetic/operator/path')

        BudgetService.replace_budgets = _committed
        BudgetService.replace_goals = _committed
        """
    )
    os.environ["_LEDGER_APPTEST_DB"] = str(db_path)
    try:
        at = AppTest.from_file(
            str(_driver_script(tmp_path, "ledger.ui.pages.budgets_goals", setup_code=setup)),
            default_timeout=30,
        ).run()
        confirmation = (
            "save budget replacements"
            if button_label == "Save Budgets"
            else "save goal replacements"
        )
        input_label = (
            "Confirm budget save" if button_label == "Save Budgets" else "Confirm goal save"
        )
        next(item for item in at.text_input if item.label == input_label).input(confirmation).run()
        next(item for item in at.button if item.label == button_label).click().run()

        rendered_errors = [str(item.value) for item in at.error]
        assert not at.exception
        assert rendered_errors == [
            "Planning change commit succeeded, but final validation or cleanup did not "
            "complete; reload and inspect before retrying."
        ]
        assert "/private" not in " ".join(rendered_errors)
    finally:
        BudgetService.replace_budgets = original_replace_budgets
        BudgetService.replace_goals = original_replace_goals


def test_planning_actual_kpi_includes_unbudgeted_gross_spending(tmp_path: Path) -> None:
    from streamlit.testing.v1 import AppTest
    from test_ui_render_invocation import _driver_script, _seed_db

    db_path = tmp_path / "ledger.db"
    _seed_db(db_path)
    with session_scope(db_path) as session:
        session.query(TransactionDB).delete()
        session.query(BudgetDB).delete()
        session.add(
            BudgetDB(
                budget_month="*",
                category="Dining",
                amount=Decimal("1500"),
                notes="",
                created_at=_now(),
                updated_at=_now(),
            )
        )
    for txn_id, category, amount in (
        ("dining", "Dining", "-1500"),
        ("groceries", "Groceries", "-200"),
    ):
        with session_scope(db_path) as session:
            session.add(
                TransactionDB(
                    txn_id=txn_id,
                    posting_date=date(2026, 1, 10),
                    transaction_date=date(2026, 1, 10),
                    institution="synthetic",
                    account="synthetic",
                    account_type="checking",
                    flow="expense",
                    cashflow_type="expense",
                    include_in_cashflow=True,
                    exclude_from_spending=False,
                    currency="CAD",
                    amount_raw=abs(Decimal(amount)),
                    amount_signed=Decimal(amount),
                    description="SYNTHETIC",
                    merchant="SYNTHETIC",
                    category=category,
                    source_file="synthetic.csv",
                    source_path="synthetic.csv",
                    source_period="2026-01",
                    source_row=1,
                    imported_at=_now(),
                    parser_version="test",
                    txn_fingerprint=f"{txn_id}-fingerprint",
                    day_of_week=5,
                    day_of_month=10,
                    week_of_year=2,
                    is_weekend=True,
                    is_month_start=False,
                    is_month_end=False,
                )
            )
    os.environ["_LEDGER_APPTEST_DB"] = str(db_path)
    at = AppTest.from_file(
        str(_driver_script(tmp_path, "ledger.ui.pages.budgets_goals")), default_timeout=30
    ).run()

    markup = "\n".join(str(item.value) for item in at.markdown)
    assert not at.exception
    assert "Actual" in markup
    assert "$1,700" in markup


def test_planning_budget_tab_keeps_empty_state_when_only_unbudgeted_spend_exists(
    tmp_path: Path,
) -> None:
    from streamlit.testing.v1 import AppTest
    from test_ui_render_invocation import _driver_script, _seed_db

    db_path = tmp_path / "ledger.db"
    _seed_db(db_path)
    with session_scope(db_path) as session:
        session.query(BudgetDB).delete()
    service = BudgetService(db_path, prepare_schema=False)
    assert not service.list_budgets()
    assert service.budget_variance(date(2025, 12, 1), date(2026, 1, 15))

    os.environ["_LEDGER_APPTEST_DB"] = str(db_path)
    at = AppTest.from_file(
        str(_driver_script(tmp_path, "ledger.ui.pages.budgets_goals")), default_timeout=30
    ).run()

    assert not at.exception
    assert "Add category rows below when you are ready" in " ".join(
        str(item.value) for item in at.caption
    )


def test_budget_suggest_reversed_dates_are_a_content_free_cli_error(tmp_path: Path) -> None:
    db_path = tmp_path / "private-ledger.db"
    dest = tmp_path / "private-suggestion.csv"
    init_database(db_path)

    result = runner.invoke(
        app,
        [
            "budget",
            "suggest",
            "--db",
            str(db_path),
            "--start-date",
            "2026-03-31",
            "--end-date",
            "2026-01-01",
            "--dest",
            str(dest),
        ],
    )

    assert result.exit_code == 2
    assert "must be on or after" in result.output
    assert str(tmp_path) not in result.output
    assert not dest.exists()


def test_budget_import_execute_prints_and_audits_its_bound_backup(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    source = tmp_path / "budgets.csv"
    init_database(db_path)
    _seed_budget(db_path)
    _write_import(source)

    result = runner.invoke(
        app,
        ["budget", "import", "--db", str(db_path), "--source", str(source), "--execute"],
    )

    assert result.exit_code == 0, result.output
    backups = list((tmp_path / "db-backups").glob("*.db"))
    assert len(backups) == 1
    flattened_output = result.output.replace("\n", "")
    assert "backup:" in flattened_output
    assert backups[0].name in flattened_output
    assert _audit_operations(db_path)[-2:] == ["backup", "budget_replace"]
    with session_scope(db_path) as session:
        backup_audit = (
            session.query(DBOperationAuditDB)
            .filter_by(operation="backup")
            .order_by(DBOperationAuditDB.id.desc())
            .one()
        )
        assert Path(str(backup_audit.artifact_path)) == backups[0]


def test_budget_export_reports_target_lock_contention_without_exposing_paths(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ledger.db"
    dest = tmp_path / "budgets.csv"
    init_database(db_path)
    _seed_budget(db_path)

    with database_target_lock(db_path):
        result = runner.invoke(
            app,
            ["budget", "export", "--db", str(db_path), "--dest", str(dest)],
        )

    assert result.exit_code == 1
    assert "could not acquire the database target lock" in result.output
    assert str(tmp_path) not in result.output
    assert not dest.exists()


def test_injected_budget_query_session_never_checks_the_lexical_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_synthetic import build_synthetic_mcp_database

    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    BudgetService(db_path).replace_budgets(
        [{"month": "*", "category": "Groceries", "amount": "100", "notes": ""}]
    )
    service = LedgerMCPService(db_path)
    original_exists = Path.exists

    def forbid_database_exists(path: Path) -> bool:
        if path == db_path:
            raise AssertionError("injected pinned sessions must not check the lexical database")
        return original_exists(path)

    monkeypatch.setattr(Path, "exists", forbid_database_exists)
    try:
        envelope = call_tool(
            service,
            "ledger.budgets",
            {"start_date": "2025-01-01", "end_date": "2025-12-31"},
        )["structuredContent"]
    finally:
        service.close()

    assert "error" not in envelope


def test_ledger_budgets_rounds_partial_window_values_before_status_and_wire_format(
    tmp_path: Path,
) -> None:
    from mcp_synthetic import build_synthetic_mcp_database
    from test_budgets_p7 import _seed_transaction

    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    with session_scope(db_path) as session:
        session.query(TransactionDB).delete()
    _seed_transaction(
        db_path,
        txn_id="partial-window-spend",
        posted=date(2026, 1, 1),
        amount="-3.23",
        category="Dining",
    )
    assert (
        BudgetService(db_path)
        .replace_budgets([{"month": "*", "category": "Dining", "amount": "100.00", "notes": ""}])
        .ok
    )
    service = LedgerMCPService(db_path)
    try:
        envelope = call_tool(
            service,
            "ledger.budgets",
            {"start_date": "2026-01-01", "end_date": "2026-01-01"},
        )["structuredContent"]
    finally:
        service.close()

    assert "error" not in envelope
    assert envelope["data"]["entries"] == [
        {
            "actual": "3.23",
            "budget": "3.23",
            "category": "Dining",
            "status": "ok",
            "variance": "0.00",
            "pct_used": "100.00",
        }
    ]
    assert envelope["data"]["totals"] == {
        "actual": "3.23",
        "budget": "3.23",
        "variance": "0.00",
    }


@pytest.mark.parametrize(
    ("amount", "start_date", "end_date"),
    [
        ("0.00", date(2026, 1, 1), date(2026, 1, 31)),
        ("0.01", date(2026, 1, 1), date(2026, 1, 1)),
    ],
    ids=["explicit-zero", "sub-cent-proration"],
)
def test_budget_presence_is_category_membership_not_rounded_amount_truthiness(
    tmp_path: Path,
    amount: str,
    start_date: date,
    end_date: date,
) -> None:
    db_path = tmp_path / "ledger.db"
    service = BudgetService(db_path)
    assert service.replace_budgets(
        [{"month": "*", "category": "Dining", "amount": amount, "notes": "defined"}]
    ).ok

    rows = service.budget_variance(start_date, end_date)

    assert rows == [
        {
            "category": "Dining",
            "budget": Decimal("0.00"),
            "actual": Decimal("0.00"),
            "variance": Decimal("0.00"),
            "pct_used": None,
            "status": "ok",
        }
    ]


def test_ledger_budgets_definition_count_is_total_stored_not_returned_entries(
    tmp_path: Path,
) -> None:
    from mcp_synthetic import build_synthetic_mcp_database

    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    service = BudgetService(db_path)
    assert service.replace_budgets(
        [
            {"month": "2025-01", "category": "Dining", "amount": "10.00", "notes": ""},
            {"month": "2026-01", "category": "Dining", "amount": "20.00", "notes": ""},
        ]
    ).ok
    mcp = LedgerMCPService(db_path)
    try:
        envelope = call_tool(
            mcp,
            "ledger.budgets",
            {"start_date": "2026-01-01", "end_date": "2026-01-31"},
        )["structuredContent"]
    finally:
        mcp.close()

    assert envelope["data"]["status"] == "reported"
    assert envelope["data"]["definition_count"] == 2
    assert envelope["data"]["entry_count"] == 1
    assert len(envelope["data"]["entries"]) == 1


def test_ledger_budgets_reports_out_of_window_definitions_without_fabricated_entries(
    tmp_path: Path,
) -> None:
    from mcp_synthetic import build_synthetic_mcp_database

    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    assert (
        BudgetService(db_path)
        .replace_budgets(
            [{"month": "2025-01", "category": "Dining", "amount": "10.00", "notes": ""}]
        )
        .ok
    )
    service = LedgerMCPService(db_path)
    try:
        envelope = call_tool(
            service,
            "ledger.budgets",
            {"start_date": "2026-01-01", "end_date": "2026-01-31"},
        )["structuredContent"]
    finally:
        service.close()

    assert envelope["data"]["status"] == "reported"
    assert envelope["data"]["definition_count"] == 1
    assert envelope["data"]["entry_count"] == 0
    assert envelope["data"]["entries"] == []
    assert envelope["data"]["totals"] == {
        "actual": "0.00",
        "budget": "0.00",
        "variance": "0.00",
    }


@pytest.mark.parametrize(
    ("column", "sql_value"),
    [
        ("budget_month", "'not-a-month'"),
        ("category", "''"),
        ("amount", "-1"),
        ("amount", "CAST('NaN' AS TEXT)"),
        ("amount", "1e999"),
    ],
    ids=["invalid-month", "empty-category", "negative", "nan", "huge-exponent"],
)
def test_ledger_budgets_rejects_any_corrupt_stored_definition_before_period_selection(
    tmp_path: Path,
    column: str,
    sql_value: str,
) -> None:
    from mcp_synthetic import build_synthetic_mcp_database

    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    assert (
        BudgetService(db_path)
        .replace_budgets(
            [
                {
                    "month": "2025-01",
                    "category": "Private stored category marker",
                    "amount": "10.00",
                    "notes": "private stored budget marker",
                }
            ]
        )
        .ok
    )
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute(f"UPDATE budgets SET {column} = {sql_value}")
    service = LedgerMCPService(db_path)
    try:
        envelope = call_tool(
            service,
            "ledger.budgets",
            {"start_date": "2026-01-01", "end_date": "2026-01-31"},
        )["structuredContent"]
    finally:
        service.close()

    assert envelope == {
        "error": {"code": "corrupt_data", "retryable": False},
        "schema": "ledger.mcp.v1",
    }
    encoded = json.dumps(envelope)
    assert "Private stored" not in encoded
    assert str(tmp_path) not in encoded


@pytest.mark.parametrize("failure_kind", ["type", "decimal", "sqlalchemy"])
def test_ledger_budgets_maps_stored_conversion_failures_to_content_free_corrupt_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_kind: str,
) -> None:
    from mcp_synthetic import build_synthetic_mcp_database
    from sqlalchemy.exc import DataError

    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    private_marker = "private stored conversion marker"

    def fail_conversion(_service: BudgetService):
        if failure_kind == "type":
            raise TypeError(private_marker)
        if failure_kind == "decimal":
            raise InvalidOperation(private_marker)
        raise DataError("stored budget conversion", {}, TypeError(private_marker))

    monkeypatch.setattr(BudgetService, "list_budgets", fail_conversion)
    service = LedgerMCPService(db_path)
    try:
        envelope = call_tool(
            service,
            "ledger.budgets",
            {"start_date": "2026-01-01", "end_date": "2026-01-31"},
        )["structuredContent"]
    finally:
        service.close()

    assert envelope == {
        "error": {"code": "corrupt_data", "retryable": False},
        "schema": "ledger.mcp.v1",
    }
    assert private_marker not in json.dumps(envelope)


def test_ledger_budgets_schema_separates_empty_and_reported_cardinality() -> None:
    from ledger.mcp.contracts import tool_schemas

    schema = next(tool for tool in tool_schemas() if tool["name"] == "ledger.budgets")
    variants = schema["outputSchema"]["oneOf"][0]["properties"]["data"]["oneOf"]
    empty = next(
        branch
        for branch in variants
        if branch["properties"]["status"].get("const") == "no_budgets_defined"
    )
    reported = next(
        branch for branch in variants if branch["properties"]["status"].get("const") == "reported"
    )

    assert empty["properties"]["definition_count"]["const"] == 0
    assert empty["properties"]["entry_count"]["const"] == 0
    assert empty["properties"]["entries"]["maxItems"] == 0
    assert reported["properties"]["definition_count"]["minimum"] == 1
    assert reported["properties"]["entry_count"]["minimum"] == 0
    assert reported["properties"]["entry_count"]["maximum"] == 500
    assert "minItems" not in reported["properties"]["entries"]
    pct_used = reported["properties"]["entries"]["items"]["properties"]["pct_used"]
    assert pct_used == {
        "oneOf": [
            {"pattern": r"^(?:0|[1-9]\d*)\.\d{2}$", "type": "string"},
            {"type": "null"},
        ]
    }


@pytest.mark.parametrize("pct", ["0.00", "100.01", "123456789.99"])
def test_ledger_budgets_pct_schema_accepts_nonnegative_decimal_values(pct: str) -> None:
    import jsonschema

    from ledger.mcp.contracts import tool_schemas

    schema = next(tool for tool in tool_schemas() if tool["name"] == "ledger.budgets")
    variants = schema["outputSchema"]["oneOf"][0]["properties"]["data"]["oneOf"]
    reported = next(
        branch for branch in variants if branch["properties"]["status"].get("const") == "reported"
    )
    pct_schema = reported["properties"]["entries"]["items"]["properties"]["pct_used"]

    jsonschema.validate(pct, pct_schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate("-0.01", pct_schema)


def test_ledger_budgets_classifies_a_midcall_rename_without_internal_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_synthetic import build_synthetic_mcp_database

    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    moved = tmp_path / "moved.db"
    BudgetService(db_path).replace_budgets(
        [{"month": "*", "category": "Groceries", "amount": "100", "notes": ""}]
    )
    service = LedgerMCPService(db_path)
    original_list = BudgetService.list_budgets
    calls = 0

    def rename_after_definitions(self: BudgetService):
        nonlocal calls
        rows = original_list(self)
        calls += 1
        if calls == 1:
            os.replace(db_path, moved)
        return rows

    monkeypatch.setattr(BudgetService, "list_budgets", rename_after_definitions)
    try:
        envelope = call_tool(
            service,
            "ledger.budgets",
            {"start_date": "2025-01-01", "end_date": "2025-12-31"},
        )["structuredContent"]
    finally:
        service.close()

    assert envelope == {
        "error": {"code": "database_unavailable", "retryable": True},
        "schema": "ledger.mcp.v1",
    }
    assert str(tmp_path) not in json.dumps(envelope)


def test_budget_import_surfaces_backup_when_atomic_replacement_rolls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.planning import service as planning_service

    db_path = tmp_path / "ledger.db"
    source = tmp_path / "budgets.csv"
    init_database(db_path)
    _seed_budget(db_path)
    before_rows = _budget_rows(db_path)
    audits_before = _audit_operations(db_path)
    _write_import(source)
    original_audit = planning_service.record_db_operation

    def fail_replace_audit(*args: object, **kwargs: object) -> None:
        if kwargs.get("operation") == "budget_replace":
            raise sqlite3.OperationalError("synthetic replacement audit failure")
        original_audit(*args, **kwargs)

    monkeypatch.setattr(planning_service, "record_db_operation", fail_replace_audit)
    result = BudgetService(db_path, prepare_schema=False).import_csv(source, execute=True)

    assert not result.ok
    assert _budget_rows(db_path) == before_rows
    assert _audit_operations(db_path) == audits_before
    backup = Path(result.artifacts["backup"])
    assert backup.is_file()
    assert _budget_rows(backup) == before_rows


@pytest.mark.parametrize(
    "amount",
    ["not-a-number", "NaN", "Infinity", "1e1000", "10000000000.00", "1.234"],
)
def test_budget_import_rejects_invalid_money_without_starting_replacement(
    tmp_path: Path,
    amount: str,
) -> None:
    db_path = tmp_path / "ledger.db"
    source = tmp_path / "budgets.csv"
    init_database(db_path)
    _seed_budget(db_path)
    before_rows = _budget_rows(db_path)
    audits_before = _audit_operations(db_path)
    source.write_text(
        f"month,category,amount,notes\n*,Replacement,{amount},after\n",
        encoding="utf-8",
    )

    result = BudgetService(db_path, prepare_schema=False).import_csv(source, execute=True)

    assert not result.ok
    assert _budget_rows(db_path) == before_rows
    assert _audit_operations(db_path) == audits_before
    assert not (tmp_path / "db-backups").exists()


def test_budget_import_refuses_a_nonblank_row_with_an_empty_category(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ledger.db"
    source = tmp_path / "budgets.csv"
    init_database(db_path)
    service = BudgetService(db_path)
    assert service.replace_budgets(
        [
            {"month": "*", "category": "First", "amount": "10", "notes": "before"},
            {"month": "*", "category": "Second", "amount": "20", "notes": "before"},
        ]
    ).ok
    before_rows = _budget_rows(db_path)
    audits_before = _audit_operations(db_path)
    source.write_text(
        "month,category,amount,notes\n*,First,30,after\n*,,40,category omitted\n",
        encoding="utf-8",
    )

    result = service.import_csv(source, execute=True)

    assert not result.ok
    assert _budget_rows(db_path) == before_rows
    assert _audit_operations(db_path) == audits_before
    assert not (tmp_path / "db-backups").exists()


@pytest.mark.parametrize(
    "row",
    [
        "*,Replacement,20.00,after,unexpected\n",
        "*,Replacement,20.00\n",
        '*,Replacement,20.00,"unterminated\n',
        f"*,Replacement,20.00,{'x' * (csv.field_size_limit() + 1)}\n",
    ],
    ids=["overflow-column", "short-row", "malformed-quote", "oversized-field"],
)
def test_budget_import_refuses_malformed_csv_structure_without_replacing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    row: str,
) -> None:
    from ledger.planning import service as planning_service

    db_path = tmp_path / "ledger.db"
    source = tmp_path / "budgets.csv"
    init_database(db_path)
    _seed_budget(db_path)
    before_rows = _budget_rows(db_path)
    audits_before = _audit_operations(db_path)
    source.write_text("month,category,amount,notes\n" + row, encoding="utf-8")
    original_lock = planning_service.database_target_lock
    original_backup = planning_service.backup_database
    observed: list[str] = []

    @contextmanager
    def observe_lock(*args: object, **kwargs: object):
        observed.append("lock")
        with original_lock(*args, **kwargs) as lock_value:
            yield lock_value

    def observe_backup(*args: object, **kwargs: object):
        observed.append("backup")
        return original_backup(*args, **kwargs)

    monkeypatch.setattr(planning_service, "database_target_lock", observe_lock)
    monkeypatch.setattr(planning_service, "backup_database", observe_backup)

    result = BudgetService(db_path, prepare_schema=False).import_csv(source, execute=True)

    assert not result.ok
    assert _budget_rows(db_path) == before_rows
    assert _audit_operations(db_path) == audits_before
    assert not (tmp_path / "db-backups").exists()
    assert observed == []


def test_budget_import_refuses_a_blank_month_instead_of_making_it_recurring(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ledger.db"
    source = tmp_path / "budgets.csv"
    init_database(db_path)
    _seed_budget(db_path)
    before_rows = _budget_rows(db_path)
    audits_before = _audit_operations(db_path)
    source.write_text(
        "month,category,amount,notes\n,Dining,10.00,month omitted\n",
        encoding="utf-8",
    )

    result = BudgetService(db_path, prepare_schema=False).import_csv(source, execute=True)

    assert not result.ok
    assert any("month" in error.casefold() for error in result.errors)
    assert _budget_rows(db_path) == before_rows
    assert _audit_operations(db_path) == audits_before
    assert not (tmp_path / "db-backups").exists()


@pytest.mark.parametrize(
    ("row", "message"),
    [
        (
            {"month": "*", "category": "Dining", "amount": "1.234"},
            "whole cents",
        ),
        (
            {"month": "*", "category": "Dining", "amount": "10000000000.00"},
            "maximum",
        ),
        (
            {"month": "*", "category": "x" * 101, "amount": "1.00"},
            "100 characters",
        ),
    ],
)
def test_every_budget_write_path_enforces_the_storage_and_wire_domain(
    tmp_path: Path,
    row: dict[str, str],
    message: str,
) -> None:
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _seed_budget(db_path)
    before_rows = _budget_rows(db_path)
    audits_before = _audit_operations(db_path)

    result = BudgetService(db_path, prepare_schema=False).replace_budgets([row])

    assert not result.ok
    assert any(message in error for error in result.errors)
    assert _budget_rows(db_path) == before_rows
    assert _audit_operations(db_path) == audits_before


@pytest.mark.parametrize("suffix", ["-wal", "-shm"])
def test_budget_writer_independently_refuses_sidecar_state_it_cannot_attribute(
    tmp_path: Path,
    suffix: str,
) -> None:
    """P7 writes fail closed on WAL/SHM instead of guessing process-wide fd ownership."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _seed_budget(db_path)
    before_rows = _budget_rows(db_path)
    sidecar = Path(f"{db_path}{suffix}")
    sidecar.write_bytes(b"synthetic unattributed sidecar")
    sidecar.chmod(0o600)

    with pytest.raises(
        OSError,
        match="SQLite sidecar presence changed during the bound read",
    ):
        BudgetService(db_path, prepare_schema=False).replace_budgets(
            [{"month": "*", "category": "Replacement", "amount": "20"}]
        )

    assert _budget_rows(db_path) == before_rows


def test_budget_writer_uses_the_canonical_delete_journal_with_full_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retained main and its canonical hot journal are the only write files."""
    from ledger.storage import snapshots

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    db_path.chmod(0o600)
    db_path.parent.chmod(0o700)
    with db_path.open("rb") as handle:
        handle.seek(18)
        assert handle.read(2) == b"\x01\x01"
    assert not any(Path(f"{db_path}{suffix}").exists() for suffix in ("-journal", "-wal", "-shm"))
    requested_database: list[str] = []
    real_connect = snapshots.sqlite3.connect

    def connect_with_hostile_sync_default(
        database: object,
        *args: object,
        **kwargs: object,
    ) -> sqlite3.Connection:
        requested_database.append(os.fspath(database))
        connection = real_connect(
            f"{db_path.as_uri()}?mode=rw",
            *args,
            **kwargs,
        )
        connection.execute("PRAGMA synchronous=OFF")
        return connection

    monkeypatch.setattr(snapshots.sqlite3, "connect", connect_with_hostile_sync_default)

    with (
        database_target_lock(db_path),
        pin_private_directory(db_path.parent) as parent,
        parent.pin_private_file(db_path.name, mutable=True) as pinned,
        snapshots.audited_database_write_connection(
            pinned,
            label="Synthetic budget target",
        ) as connection,
    ):
        opened_main = Path(str(connection.execute("PRAGMA database_list").fetchone()[2]))
        assert opened_main == db_path
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        journal = Path(f"{db_path}-journal")
        assert journal.is_file()
        assert stat.S_IMODE(journal.stat(follow_symlinks=False).st_mode) == 0o600
        assert journal.stat(follow_symlinks=False).st_size > 0

    assert requested_database == [f"{db_path.as_uri()}?mode=rw"]
    assert not any(Path(f"{db_path}{suffix}").exists() for suffix in ("-journal", "-wal", "-shm"))
    assert not any(path.name.startswith(snapshots._ALIAS_PREFIX) for path in tmp_path.iterdir())


def test_budget_writer_refuses_connect_time_extra_regular_file_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Connect must add exactly the pinned main fd before any journal setup."""
    from ledger.storage import snapshots

    db_path = tmp_path / "ledger.db"
    decoy = tmp_path / "unrelated-regular-file"
    init_database(db_path)
    decoy.write_bytes(b"not sqlite")
    db_path.chmod(0o600)
    decoy.chmod(0o600)
    db_path.parent.chmod(0o700)
    real_connect = snapshots.sqlite3.connect
    extra_descriptor = -1

    def connect_with_extra_fd(*args: object, **kwargs: object) -> sqlite3.Connection:
        nonlocal extra_descriptor
        connection = real_connect(*args, **kwargs)
        extra_descriptor = os.open(decoy, os.O_RDONLY)
        return connection

    monkeypatch.setattr(snapshots.sqlite3, "connect", connect_with_extra_fd)
    yielded = False
    try:
        with (
            database_target_lock(db_path),
            pin_private_directory(db_path.parent) as parent,
            parent.pin_private_file(db_path.name, mutable=True) as pinned,
            pytest.raises(OSError, match="exactly one database file"),
            snapshots.audited_database_write_connection(
                pinned,
                label="Synthetic budget target",
            ),
        ):
            yielded = True
    finally:
        if extra_descriptor >= 0:
            os.close(extra_descriptor)

    assert not yielded


def test_budget_writer_refuses_a_nonrollback_database_header_before_connect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 2/2 WAL-format header cannot enter the rollback-journal writer."""
    from ledger.storage import snapshots

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    db_path.chmod(0o600)
    db_path.parent.chmod(0o700)
    descriptor = os.open(db_path, os.O_RDWR)
    try:
        os.pwrite(descriptor, b"\x02\x02", 18)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    before = db_path.read_bytes()
    connect_called = False
    real_connect = snapshots.sqlite3.connect

    def observed_connect(*args: object, **kwargs: object):
        nonlocal connect_called
        connect_called = True
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(snapshots.sqlite3, "connect", observed_connect)

    with (
        pytest.raises(OSError) as captured,
        database_target_lock(db_path),
        pin_private_directory(db_path.parent) as parent,
        parent.pin_private_file(db_path.name, mutable=True) as pinned,
        snapshots.audited_database_write_connection(
            pinned,
            label="Synthetic budget target",
        ),
    ):
        pass
    assert not connect_called
    assert "rollback-journal format" in str(captured.value)
    assert db_path.read_bytes() == before
    assert not any(Path(f"{db_path}{suffix}").exists() for suffix in ("-journal", "-wal", "-shm"))


def test_budget_writer_audits_the_exact_main_inode_opened_by_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A main-file swap confined to connect must be rejected before writes."""
    from ledger.storage import snapshots

    db_path = tmp_path / "ledger.db"
    decoy = tmp_path / "decoy.db"
    displaced = tmp_path / "original.db"
    init_database(db_path)
    _seed_budget(db_path)
    snapshots.snapshot_database(db_path, decoy)
    before_rows = _budget_rows(db_path)
    real_connect = snapshots.sqlite3.connect
    swapped = False
    opened_decoy: Path | None = None

    def swap_only_during_connect(database: object, *args: object, **kwargs: object):
        nonlocal opened_decoy, swapped
        database_text = os.fspath(database) if isinstance(database, (str, os.PathLike)) else ""
        opened_path = Path(database_text.removeprefix("file://").split("?", 1)[0])
        if not swapped and opened_path == db_path:
            swapped = True
            os.replace(db_path, displaced)
            shutil.copyfile(decoy, db_path)
            db_path.chmod(0o600)
            try:
                return real_connect(database, *args, **kwargs)
            finally:
                opened_decoy = db_path.with_name("opened-decoy.db")
                os.replace(db_path, opened_decoy)
                os.replace(displaced, db_path)
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(snapshots.sqlite3, "connect", swap_only_during_connect)

    yielded = False
    try:
        with (
            database_target_lock(db_path),
            pin_private_directory(db_path.parent) as parent,
            parent.pin_private_file(db_path.name, mutable=True) as pinned,
            pytest.raises(OSError, match="unbound database inode"),
            snapshots.audited_database_write_connection(
                pinned,
                label="Synthetic budget target",
            ),
        ):
            yielded = True
    finally:
        if opened_decoy is not None and opened_decoy.exists():
            opened_decoy.unlink()

    assert swapped
    assert not yielded
    assert _budget_rows(db_path) == before_rows


def test_budget_writer_audits_the_exact_persistent_journal_opened_by_sqlite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A journal decoy spanning connect through first write is refused before yield."""
    from ledger.storage import snapshots

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _seed_budget(db_path)
    before_rows = _budget_rows(db_path)
    real_connect = snapshots.sqlite3.connect
    swapped = False

    journal = Path(f"{db_path}-journal")
    displaced = tmp_path / "pinned-original.journal"
    opened_decoy = tmp_path / "opened-decoy.journal"

    class SwapJournalDuringFirstWrite:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection

        def __getattr__(self, name: str):
            return getattr(self.connection, name)

        @property
        def row_factory(self):
            return self.connection.row_factory

        @row_factory.setter
        def row_factory(self, value: object) -> None:
            self.connection.row_factory = value

        def execute(self, statement: str, *args: object, **kwargs: object):
            nonlocal swapped
            if statement.startswith("PRAGMA user_version="):
                swapped = True
                os.replace(journal, displaced)
                journal.write_bytes(b"")
                journal.chmod(0o600)
                result = self.connection.execute(statement, *args, **kwargs)
                os.replace(journal, opened_decoy)
                os.replace(displaced, journal)
                return result
            return self.connection.execute(statement, *args, **kwargs)

    def swap_journal_across_connect(database: object, *args: object, **kwargs: object):
        connection = real_connect(database, *args, **kwargs)
        return SwapJournalDuringFirstWrite(connection)

    monkeypatch.setattr(snapshots.sqlite3, "connect", swap_journal_across_connect)

    yielded = False
    try:
        with (
            database_target_lock(db_path),
            pin_private_directory(db_path.parent) as parent,
            parent.pin_private_file(db_path.name, mutable=True) as pinned,
            pytest.raises(OSError, match="unbound journal inode"),
            snapshots.audited_database_write_connection(
                pinned,
                label="Synthetic budget target",
            ),
        ):
            yielded = True
    finally:
        displaced.unlink(missing_ok=True)
        opened_decoy.unlink(missing_ok=True)

    assert swapped
    assert not yielded
    assert _budget_rows(db_path) == before_rows


def test_budget_writer_rejects_a_wrong_journal_even_with_an_extra_exact_fd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unrelated exact-inode fd cannot impersonate SQLite's journal binding."""
    from ledger.storage import snapshots

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _seed_budget(db_path)
    before_rows = _budget_rows(db_path)
    real_connect = snapshots.sqlite3.connect
    swapped = False
    extra_exact_descriptor = -1
    journal = Path(f"{db_path}-journal")
    displaced = tmp_path / "expected-journal"
    wrong_journal = tmp_path / "sqlite-opened-wrong.journal"

    class RestoreExpectedJournalAfterFirstWrite:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection

        def __getattr__(self, name: str):
            return getattr(self.connection, name)

        @property
        def row_factory(self):
            return self.connection.row_factory

        @row_factory.setter
        def row_factory(self, value: object) -> None:
            self.connection.row_factory = value

        def execute(self, statement: str, *args: object, **kwargs: object):
            nonlocal extra_exact_descriptor, swapped
            if statement.startswith("PRAGMA user_version="):
                swapped = True
                os.replace(journal, displaced)
                journal.write_bytes(b"")
                journal.chmod(0o600)
                result = self.connection.execute(statement, *args, **kwargs)
                os.replace(journal, wrong_journal)
                os.replace(displaced, journal)
                extra_exact_descriptor = os.open(journal, os.O_RDONLY)
                return result
            return self.connection.execute(statement, *args, **kwargs)

    def install_wrong_journal(database: object, *args: object, **kwargs: object):
        connection = real_connect(database, *args, **kwargs)
        return RestoreExpectedJournalAfterFirstWrite(connection)

    monkeypatch.setattr(snapshots.sqlite3, "connect", install_wrong_journal)

    yielded = False
    try:
        with (
            database_target_lock(db_path),
            pin_private_directory(db_path.parent) as parent,
            parent.pin_private_file(db_path.name, mutable=True) as pinned,
            pytest.raises(OSError, match="exactly one journal file"),
            snapshots.audited_database_write_connection(
                pinned,
                label="Synthetic budget target",
            ),
        ):
            yielded = True
    finally:
        if extra_exact_descriptor >= 0:
            os.close(extra_exact_descriptor)
        displaced.unlink(missing_ok=True)
        wrong_journal.unlink(missing_ok=True)

    assert swapped
    assert not yielded
    assert _budget_rows(db_path) == before_rows


def test_budget_writer_crash_recovers_through_the_canonical_hot_journal(
    tmp_path: Path,
) -> None:
    """Abrupt process death cannot expose uncommitted spilled pages."""
    from ledger.storage import snapshots

    db_path = tmp_path / "ledger.db"
    row_count = 4_000
    before_value = "before-" + ("a" * 900)
    after_value = "after-" + ("b" * 900)
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute("CREATE TABLE synthetic_crash_rows (value TEXT NOT NULL)")
        connection.executemany(
            "INSERT INTO synthetic_crash_rows(value) VALUES (?)",
            [(before_value,)] * row_count,
        )
        connection.commit()
    db_path.chmod(0o600)
    db_path.parent.chmod(0o700)

    script = textwrap.dedent(
        """
        import sys
        import time
        from pathlib import Path

        from ledger.storage.snapshots import audited_database_write_connection, database_target_lock
        from ledger.utils.private_files import pin_private_directory

        target = Path(sys.argv[1])
        replacement = sys.argv[2]
        with (
            database_target_lock(target),
            pin_private_directory(target.parent) as parent,
            parent.pin_private_file(target.name, mutable=True) as pinned,
            audited_database_write_connection(pinned, label="Synthetic crash target") as connection,
        ):
            connection.execute("PRAGMA cache_size=5")
            connection.execute("PRAGMA cache_spill=ON")
            connection.execute("UPDATE synthetic_crash_rows SET value = ?", (replacement,))
            print("READY", flush=True)
            time.sleep(30)
        """
    )
    process = subprocess.Popen(
        [sys.executable, "-c", script, str(db_path), after_value],
        cwd=Path(__file__).parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    selector = selectors.DefaultSelector()
    assert process.stdout is not None
    selector.register(process.stdout, selectors.EVENT_READ)
    try:
        output = bytearray()
        deadline = time.monotonic() + 8
        while b"\n" not in output and time.monotonic() < deadline:
            ready = selector.select(timeout=max(0, deadline - time.monotonic()))
            if not ready:
                break
            chunk = os.read(process.stdout.fileno(), 4096)
            if not chunk:
                break
            output.extend(chunk)
        if b"\n" not in output:
            process.kill()
            _stdout, stderr = process.communicate(timeout=5)
            pytest.fail(f"crash worker did not become ready: {stderr}")
        assert bytes(output).splitlines()[0] == b"READY"
        process.kill()
        process.wait(timeout=5)
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()

    with closing(sqlite3.connect(db_path)) as connection:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        before_count = int(
            connection.execute(
                "SELECT count(*) FROM synthetic_crash_rows WHERE value = ?",
                (before_value,),
            ).fetchone()[0]
        )
        after_count = int(
            connection.execute(
                "SELECT count(*) FROM synthetic_crash_rows WHERE value = ?",
                (after_value,),
            ).fetchone()[0]
        )

    assert integrity == "ok"
    assert before_count == row_count
    assert after_count == 0
    assert not any(Path(f"{db_path}{suffix}").exists() for suffix in ("-journal", "-wal", "-shm"))
    assert not any(path.name.startswith(snapshots._ALIAS_PREFIX) for path in tmp_path.iterdir())
    assert not (tmp_path / snapshots._ALIAS_PARENT_LOCK).exists()


def test_next_audited_writer_recovers_a_canonical_hot_journal_before_new_work(
    tmp_path: Path,
) -> None:
    """The next app write performs canonical recovery without an ordinary reopen first."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    row_count = 2_000
    before_value = "before-" + ("a" * 900)
    after_value = "after-" + ("b" * 900)
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute("CREATE TABLE synthetic_recovery_rows (value TEXT NOT NULL)")
        connection.executemany(
            "INSERT INTO synthetic_recovery_rows(value) VALUES (?)",
            [(before_value,)] * row_count,
        )
        connection.commit()
    db_path.chmod(0o600)
    db_path.parent.chmod(0o700)
    script = textwrap.dedent(
        """
        import sys
        import time
        from pathlib import Path

        from ledger.storage.snapshots import audited_database_write_connection, database_target_lock
        from ledger.utils.private_files import pin_private_directory

        target = Path(sys.argv[1])
        replacement = sys.argv[2]
        with (
            database_target_lock(target),
            pin_private_directory(target.parent) as parent,
            parent.pin_private_file(target.name, mutable=True) as pinned,
            audited_database_write_connection(pinned, label='Synthetic recovery target') as connection,
        ):
            connection.execute('PRAGMA cache_size=5')
            connection.execute('PRAGMA cache_spill=ON')
            connection.execute('UPDATE synthetic_recovery_rows SET value = ?', (replacement,))
            print('HOT_READY', flush=True)
            time.sleep(30)
        """
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(db_path), after_value],
        cwd=Path(__file__).parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    try:
        assert child.stdout.readline().strip() == "HOT_READY"
        child.kill()
        child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        if child.stdout is not None:
            child.stdout.close()
        if child.stderr is not None:
            child.stderr.close()

    hot_journal = Path(f"{db_path}-journal")
    assert hot_journal.is_file()
    assert hot_journal.stat().st_size > 0

    result = BudgetService(db_path, prepare_schema=False).replace_budgets(
        [{"month": "*", "category": "Recovered", "amount": "25.00"}]
    )

    assert result.ok, result.errors
    with closing(sqlite3.connect(db_path)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute(
            "SELECT count(*) FROM synthetic_recovery_rows WHERE value = ?",
            (before_value,),
        ).fetchone() == (row_count,)
        assert connection.execute(
            "SELECT count(*) FROM synthetic_recovery_rows WHERE value = ?",
            (after_value,),
        ).fetchone() == (0,)
    assert _budget_rows(db_path) == [("*", "Recovered", "25", "")]
    assert not hot_journal.exists()


def test_next_audited_writer_cleans_a_sigkill_empty_prebegin_journal(
    tmp_path: Path,
) -> None:
    """A reboot between secure journal creation and BEGIN cannot wedge future writes."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _seed_budget(db_path)
    script = textwrap.dedent(
        """
        import sys
        import time
        from pathlib import Path

        from ledger.storage import snapshots
        from ledger.storage.snapshots import audited_database_write_connection, database_target_lock
        from ledger.utils.private_files import pin_private_directory

        target = Path(sys.argv[1])
        real_create = snapshots._create_pinned_writer_journal
        def stop_after_empty_journal(pinned_main):
            journal = real_create(pinned_main)
            print('EMPTY_READY', flush=True)
            time.sleep(30)
            return journal
        snapshots._create_pinned_writer_journal = stop_after_empty_journal
        with (
            database_target_lock(target),
            pin_private_directory(target.parent) as parent,
            parent.pin_private_file(target.name, mutable=True) as pinned,
            audited_database_write_connection(pinned, label='Synthetic empty-journal target'),
        ):
            pass
        """
    )
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(db_path)],
        cwd=Path(__file__).parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    try:
        assert child.stdout.readline().strip() == "EMPTY_READY"
        child.kill()
        child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        if child.stdout is not None:
            child.stdout.close()
        if child.stderr is not None:
            child.stderr.close()

    journal = Path(f"{db_path}-journal")
    assert journal.is_file()
    assert journal.stat().st_size == 0
    assert journal.stat().st_nlink == 1

    result = BudgetService(db_path, prepare_schema=False).replace_budgets(
        [{"month": "*", "category": "Replacement", "amount": "20"}]
    )

    assert result.ok, result.errors
    assert _budget_rows(db_path) == [("*", "Replacement", "20", "")]
    assert not journal.exists()


@pytest.mark.parametrize(
    "transaction_control",
    ["method-commit", "method-rollback", "sql-commit", "sql-rollback", "savepoint"],
)
def test_audited_writer_denies_caller_transaction_control(
    tmp_path: Path,
    transaction_control: str,
) -> None:
    from ledger.storage import snapshots

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    db_path.chmod(0o600)
    db_path.parent.chmod(0o700)

    with (
        pytest.raises(sqlite3.DatabaseError, match="not authorized"),
        database_target_lock(db_path),
        pin_private_directory(db_path.parent) as parent,
        parent.pin_private_file(db_path.name, mutable=True) as pinned,
        snapshots.audited_database_write_connection(
            pinned,
            label="Synthetic transaction-control target",
        ) as connection,
    ):
        connection.execute(
            "INSERT INTO config_metadata (key, value) VALUES ('txn-control', 'blocked')"
        )
        if transaction_control == "method-commit":
            connection.commit()
        elif transaction_control == "method-rollback":
            connection.rollback()
        elif transaction_control == "sql-commit":
            connection.execute("COMMIT")
        elif transaction_control == "sql-rollback":
            connection.execute("ROLLBACK")
        else:
            connection.execute("SAVEPOINT caller_owned")

    with closing(sqlite3.connect(db_path)) as connection:
        assert (
            connection.execute(
                "SELECT value FROM config_metadata WHERE key = 'txn-control'"
            ).fetchone()
            is None
        )
    assert not Path(f"{db_path}-journal").exists()


def test_audited_writer_refuses_a_journal_hardlink_before_commit(
    tmp_path: Path,
) -> None:
    from ledger.storage import snapshots

    db_path = tmp_path / "ledger.db"
    retained = tmp_path / "retained-rollback-evidence"
    init_database(db_path)
    db_path.chmod(0o600)
    db_path.parent.chmod(0o700)

    with (
        pytest.raises(OSError, match="link count"),
        database_target_lock(db_path),
        pin_private_directory(db_path.parent) as parent,
        parent.pin_private_file(db_path.name, mutable=True) as pinned,
        snapshots.audited_database_write_connection(
            pinned,
            label="Synthetic hardlinked-journal target",
        ) as connection,
    ):
        connection.execute(
            "INSERT INTO config_metadata (key, value) VALUES ('journal-link', 'blocked')"
        )
        os.link(Path(f"{db_path}-journal"), retained)

    with closing(sqlite3.connect(db_path)) as connection:
        assert (
            connection.execute(
                "SELECT value FROM config_metadata WHERE key = 'journal-link'"
            ).fetchone()
            is None
        )
    assert retained.is_file()
    assert retained.stat().st_size > 0
    assert not Path(f"{db_path}-journal").exists()


def test_audited_writer_reports_a_postcommit_journal_hardlink_truthfully(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.storage import snapshots

    db_path = tmp_path / "ledger.db"
    retained = tmp_path / "retained-postcommit-journal"
    init_database(db_path)
    db_path.chmod(0o600)
    db_path.parent.chmod(0o700)
    real_connect = snapshots.sqlite3.connect

    class LinkJournalAtCommit:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self.connection = connection

        def __getattr__(self, name: str):
            return getattr(self.connection, name)

        @property
        def row_factory(self):
            return self.connection.row_factory

        @row_factory.setter
        def row_factory(self, value: object) -> None:
            self.connection.row_factory = value

        def commit(self) -> None:
            os.link(Path(f"{db_path}-journal"), retained)
            self.connection.commit()

    def connect_with_commit_race(*args: object, **kwargs: object):
        return LinkJournalAtCommit(real_connect(*args, **kwargs))

    monkeypatch.setattr(snapshots.sqlite3, "connect", connect_with_commit_race)

    with (
        pytest.raises(DatabaseWriteCommittedError, match="committed"),
        database_target_lock(db_path),
        pin_private_directory(db_path.parent) as parent,
        parent.pin_private_file(db_path.name, mutable=True) as pinned,
        snapshots.audited_database_write_connection(
            pinned,
            label="Synthetic postcommit hardlink target",
        ) as connection,
    ):
        connection.execute(
            "INSERT INTO config_metadata (key, value) VALUES ('postcommit-link', 'committed')"
        )

    monkeypatch.setattr(snapshots.sqlite3, "connect", real_connect)
    with closing(sqlite3.connect(db_path)) as connection:
        assert connection.execute(
            "SELECT value FROM config_metadata WHERE key = 'postcommit-link'"
        ).fetchone() == ("committed",)
    assert retained.is_file()
    assert retained.stat().st_size > 0
    assert retained.read_bytes()[:8] != b"\0" * 8
    assert not Path(f"{db_path}-journal").exists()


def test_empty_budget_suggestion_refuses_an_unimportable_artifact(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    dest = tmp_path / "suggestions.csv"
    init_database(db_path)

    result = BudgetService(db_path, prepare_schema=False).export_suggestions(
        dest,
        date(2026, 1, 1),
        date(2026, 1, 31),
    )

    assert not result.ok
    assert not dest.exists()


@pytest.mark.parametrize("case", ["blank", "whitespace", "too_long", "too_large"])
def test_budget_suggestion_never_publishes_rows_its_importer_cannot_preserve(
    tmp_path: Path,
    case: str,
) -> None:
    from test_budgets_p7 import _seed_transaction

    db_path = tmp_path / "ledger.db"
    dest = tmp_path / "suggestions.csv"
    init_database(db_path)
    category = {
        "blank": "",
        "whitespace": " Dining ",
        "too_long": "x" * 101,
        "too_large": "Dining",
    }[case]
    amounts = ["-6000000000.00", "-6000000000.00"] if case == "too_large" else ["-25"]
    for index, amount in enumerate(amounts):
        _seed_transaction(
            db_path,
            txn_id=f"invalid-suggestion-{case}-{index}",
            posted=date(2026, 1, 10 + index),
            amount=amount,
            category=category,
        )

    result = BudgetService(db_path, prepare_schema=False).export_suggestions(
        dest,
        date(2026, 1, 1),
        date(2026, 1, 31),
    )

    assert not result.ok
    assert not dest.exists()


def test_budget_suggestion_holds_the_shared_database_lock_while_reading_and_publishing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_budgets_p7 import _seed_transaction

    from ledger.planning import service as planning_service

    db_path = tmp_path / "ledger.db"
    dest = tmp_path / "suggestions.csv"
    init_database(db_path)
    _seed_transaction(
        db_path,
        txn_id="suggest-lock",
        posted=date(2026, 1, 10),
        amount="-25",
        category="Dining",
    )
    service = BudgetService(db_path, prepare_schema=False)
    original_suggest = service._suggest_budgets_session
    original_publish = planning_service._publish_budget_csv_no_clobber
    original_lock = planning_service.database_target_lock
    observed: list[str] = []

    @contextmanager
    def observe_target_lock(*args: object, **kwargs: object):
        observed.append("lock-enter")
        with original_lock(*args, **kwargs) as lock_value:
            try:
                yield lock_value
            finally:
                observed.append("lock-exit")

    def observe_lock(session: object, start_date: date, end_date: date):
        observed.append("read")
        return original_suggest(session, start_date, end_date)

    def observe_publication(*args: object, **kwargs: object):
        observed.append("publish-enter")
        try:
            return original_publish(*args, **kwargs)
        finally:
            observed.append("publish-exit")

    monkeypatch.setattr(planning_service, "database_target_lock", observe_target_lock)
    monkeypatch.setattr(service, "_suggest_budgets_session", observe_lock)
    monkeypatch.setattr(planning_service, "_publish_budget_csv_no_clobber", observe_publication)

    result = service.export_suggestions(dest, date(2026, 1, 1), date(2026, 1, 31))

    assert result.ok, result.errors
    assert observed == ["lock-enter", "read", "publish-enter", "publish-exit", "lock-exit"]
    assert observed.count("lock-enter") == observed.count("lock-exit") == 1
    assert dest.is_file()


@pytest.mark.parametrize("category", ["-Fees", "+Credits", "=Formula", "@Handle", "'Literal"])
def test_budget_suggestion_refuses_unsafe_semantic_cells_without_an_artifact(
    tmp_path: Path,
    category: str,
) -> None:
    from test_budgets_p7 import _seed_transaction

    db_path = tmp_path / "ledger.db"
    dest = tmp_path / "suggestions.csv"
    init_database(db_path)
    _seed_transaction(
        db_path,
        txn_id="formula-prefix",
        posted=date(2026, 1, 10),
        amount="-25",
        category=category,
    )
    service = BudgetService(db_path)

    suggested = service.export_suggestions(dest, date(2026, 1, 1), date(2026, 1, 31))

    assert not suggested.ok
    assert not dest.exists()


@pytest.mark.parametrize("category", ["'=Legacy", "''Legacy", "'-Fees"])
def test_budget_import_preserves_unmarked_legacy_apostrophe_cells_literally(
    tmp_path: Path,
    category: str,
) -> None:
    db_path = tmp_path / "ledger.db"
    source = tmp_path / "legacy.csv"
    service = BudgetService(db_path)
    source.write_text(
        f"month,category,amount,notes\n*,{category},10.00,legacy literal\n",
        encoding="utf-8",
    )

    result = service.import_csv(source, execute=True)

    assert result.ok, result.errors
    assert [row[1] for row in _budget_rows(db_path)] == [category]


def test_budget_import_preserves_marker_shaped_legacy_literal_and_export_refuses_it(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ledger.db"
    dest = tmp_path / "budgets.csv"
    service = BudgetService(db_path)
    marker_like = "'ledger-budget-v1:PUZvcm11bGE="
    source = tmp_path / "legacy.csv"
    source.write_text(
        f"month,category,amount,notes\n*,{marker_like},10.00,legacy literal\n",
        encoding="utf-8",
    )

    imported = service.import_csv(source, execute=True)
    exported = service.export_csv(dest)

    assert imported.ok, imported.errors
    assert [row[1] for row in _budget_rows(db_path)] == [marker_like]
    assert not exported.ok
    assert not dest.exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("category", "=Formula"),
        ("category", "'Literal"),
        ("notes", "+Formula"),
        ("notes", "'Literal"),
    ],
)
def test_budget_export_refuses_unsafe_semantic_cells_without_an_artifact(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    db_path = tmp_path / "ledger.db"
    dest = tmp_path / "budgets.csv"
    service = BudgetService(db_path)
    row = {"month": "*", "category": "Dining", "amount": "10.00", "notes": "safe"}
    row[field] = value
    assert service.replace_budgets([row]).ok

    result = service.export_csv(dest)

    assert not result.ok
    assert not dest.exists()


def test_safe_budget_export_import_round_trip_is_literal(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    dest = tmp_path / "budgets.csv"
    service = BudgetService(db_path)
    row = {
        "month": "*",
        "category": "Safe category",
        "amount": "10.25",
        "notes": "punctuation, spaces, and 100% literal",
    }
    assert service.replace_budgets([row]).ok

    exported = service.export_csv(dest)
    imported = service.import_csv(dest, execute=True)

    assert exported.ok, exported.errors
    assert imported.ok, imported.errors
    assert _budget_rows(db_path) == [
        ("*", "Safe category", "10.25", "punctuation, spaces, and 100% literal")
    ]


@pytest.mark.parametrize("operation", ["suggest", "export"])
@pytest.mark.parametrize(
    "artifact",
    [
        "main",
        "wal",
        "shm",
        "journal",
        "lock",
        "alias-lock",
        "alias-lock-traversal",
        "alias-name",
        "alias-name-traversal",
        "hardlink",
    ],
)
def test_budget_csv_output_refuses_every_database_artifact_collision(
    tmp_path: Path,
    operation: str,
    artifact: str,
) -> None:
    from test_budgets_p7 import _seed_transaction

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _seed_transaction(
        db_path,
        txn_id="collision-spend",
        posted=date(2026, 1, 10),
        amount="-25",
        category="Dining",
    )
    _seed_budget(db_path)
    before_main = db_path.read_bytes()
    if artifact == "main":
        dest = db_path
    elif artifact == "lock":
        dest = db_path.parent / f".{db_path.name}.rebuild.lock"
    elif artifact == "alias-lock":
        dest = db_path.parent / ".ledger-sqlite-alias.lock"
    elif artifact == "alias-lock-traversal":
        (db_path.parent / "subdir").mkdir()
        dest = db_path.parent / "subdir" / ".." / ".ledger-sqlite-alias.lock"
    elif artifact == "alias-name":
        dest = db_path.parent / ".ledger-sqlite-alias-synthetic"
    elif artifact == "alias-name-traversal":
        (db_path.parent / "subdir").mkdir()
        dest = db_path.parent / "subdir" / ".." / ".ledger-sqlite-alias-synthetic"
    elif artifact == "hardlink":
        dest = tmp_path / "database-alias"
        os.link(db_path, dest)
    else:
        dest = Path(f"{db_path}-{artifact}")
    before_dest = dest.read_bytes() if dest.exists() else None
    service = BudgetService(db_path, prepare_schema=False)

    if operation == "suggest":
        result = service.export_suggestions(dest, date(2026, 1, 1), date(2026, 1, 31))
    else:
        result = service.export_csv(dest)

    assert not result.ok
    assert db_path.read_bytes() == before_main
    assert (dest.read_bytes() if dest.exists() else None) == before_dest


@pytest.mark.parametrize("operation", ["suggest", "export"])
@pytest.mark.parametrize(
    "reserved_name",
    [".ledger-sqlite-alias.lock", ".ledger-sqlite-alias-synthetic"],
)
def test_budget_csv_prelock_normalization_refuses_reserved_traversal_without_recheck(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    reserved_name: str,
) -> None:
    from test_budgets_p7 import _seed_transaction

    from ledger.planning import service as planning_service

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _seed_transaction(
        db_path,
        txn_id=f"{operation}-normalized-reserved-name",
        posted=date(2026, 1, 10),
        amount="-25",
        category="Dining",
    )
    _seed_budget(db_path)
    (tmp_path / "subdir").mkdir()
    dest = tmp_path / "subdir" / ".." / reserved_name
    before_main = db_path.read_bytes()
    monkeypatch.setattr(
        planning_service,
        "_pinned_budget_output_collides_with_database",
        lambda *_args: False,
    )
    monkeypatch.setattr(
        planning_service,
        "_normalized_budget_output_collides_with_database",
        lambda *_args: False,
    )
    service = BudgetService(db_path, prepare_schema=False)

    if operation == "suggest":
        result = service.export_suggestions(dest, date(2026, 1, 1), date(2026, 1, 31))
    else:
        result = service.export_csv(dest)

    assert not result.ok
    assert db_path.read_bytes() == before_main
    assert not (tmp_path / reserved_name).exists()


@pytest.mark.parametrize("operation", ["suggest", "export"])
def test_budget_csv_publication_refuses_a_destination_parent_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    from test_budgets_p7 import _seed_transaction

    from ledger.planning import service as planning_service

    root = tmp_path / operation
    database_parent = root / "database"
    output_parent = root / "output"
    displaced_output = root / "output-before-swap"
    database_parent.mkdir(parents=True, mode=0o700)
    output_parent.mkdir(mode=0o700)
    db_path = database_parent / "ledger.db"
    dest = output_parent / "ledger.db"
    init_database(db_path)
    _seed_transaction(
        db_path,
        txn_id=f"{operation}-parent-swap",
        posted=date(2026, 1, 10),
        amount="-25",
        category="Dining",
    )
    _seed_budget(db_path)
    db_path.chmod(0o600)
    database_parent.chmod(0o700)
    before_database = (
        db_path.stat().st_dev,
        db_path.stat().st_ino,
        db_path.read_bytes(),
    )
    before_sidecars = {
        suffix: (
            Path(f"{db_path}{suffix}").stat().st_dev,
            Path(f"{db_path}{suffix}").stat().st_ino,
            Path(f"{db_path}{suffix}").read_bytes(),
        )
        for suffix in ("-wal", "-shm", "-journal")
        if Path(f"{db_path}{suffix}").exists()
    }
    original_publish = planning_service._publish_budget_csv_no_clobber
    swapped = False

    def swap_parents_before_candidate(*args: object, **kwargs: object):
        nonlocal swapped
        if not swapped:
            swapped = True
            output_parent.rename(displaced_output)
            database_parent.rename(output_parent)
        return original_publish(*args, **kwargs)

    monkeypatch.setattr(
        planning_service,
        "_publish_budget_csv_no_clobber",
        swap_parents_before_candidate,
    )
    service = BudgetService(db_path, prepare_schema=False)

    if operation == "suggest":
        result = service.export_suggestions(dest, date(2026, 1, 1), date(2026, 1, 31))
    else:
        result = service.export_csv(dest)

    assert swapped
    assert not result.ok
    moved_database = output_parent / "ledger.db"
    assert (
        moved_database.stat().st_dev,
        moved_database.stat().st_ino,
        moved_database.read_bytes(),
    ) == before_database
    for suffix, signature in before_sidecars.items():
        moved_sidecar = Path(f"{moved_database}{suffix}")
        assert (
            moved_sidecar.stat().st_dev,
            moved_sidecar.stat().st_ino,
            moved_sidecar.read_bytes(),
        ) == signature
    assert not (displaced_output / "ledger.db").exists()


@pytest.mark.parametrize("operation", ["suggest", "export"])
def test_budget_csv_publication_never_reads_a_lexical_database_after_pinning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    from test_budgets_p7 import _seed_transaction

    from ledger.planning import service as planning_service

    database_parent = tmp_path / operation / "database"
    output_parent = tmp_path / operation / "output"
    database_parent.mkdir(parents=True, mode=0o700)
    output_parent.mkdir(mode=0o700)
    db_path = database_parent / "ledger.db"
    decoy = database_parent / "decoy.db"
    displaced = database_parent / "canonical-during-query.db"
    dest = output_parent / "budgets.csv"
    init_database(db_path)
    init_database(decoy)
    _seed_budget(db_path, category="Canonical category", amount="10")
    _seed_budget(decoy, category="Private decoy marker", amount="999")
    _seed_transaction(
        db_path,
        txn_id=f"{operation}-canonical-spend",
        posted=date(2026, 1, 10),
        amount="-25",
        category="Canonical category",
    )
    _seed_transaction(
        decoy,
        txn_id=f"{operation}-private-decoy-spend",
        posted=date(2026, 1, 10),
        amount="-999",
        category="Private decoy marker",
    )
    db_path.chmod(0o600)
    decoy.chmod(0o600)
    before_database = (db_path.stat().st_ino, db_path.read_bytes())
    before_decoy = (decoy.stat().st_ino, decoy.read_bytes())
    original_collision_check = planning_service._pinned_budget_output_collides_with_database
    original_connect = planning_service.sqlite3.connect
    swapped = False
    swap_observed = False

    def restore_names() -> None:
        nonlocal swapped
        if not swapped:
            return
        os.replace(db_path, decoy)
        os.replace(displaced, db_path)
        swapped = False

    def swap_after_database_pin(*args: object) -> bool:
        nonlocal swapped, swap_observed
        collision = original_collision_check(*args)
        if not swap_observed:
            os.replace(db_path, displaced)
            os.replace(decoy, db_path)
            swapped = True
            swap_observed = True
        return collision

    def restore_after_lexical_connect(database: object, *args: object, **kwargs: object):
        connection = original_connect(database, *args, **kwargs)
        if swapped:
            restore_names()
        return connection

    monkeypatch.setattr(
        planning_service,
        "_pinned_budget_output_collides_with_database",
        swap_after_database_pin,
    )
    monkeypatch.setattr(planning_service.sqlite3, "connect", restore_after_lexical_connect)
    service = BudgetService(db_path, prepare_schema=False)
    try:
        if operation == "suggest":
            result = service.export_suggestions(dest, date(2026, 1, 1), date(2026, 1, 31))
        else:
            result = service.export_csv(dest)
    finally:
        restore_names()

    assert swap_observed
    assert not result.ok
    assert not dest.exists()
    assert (db_path.stat().st_ino, db_path.read_bytes()) == before_database
    assert (decoy.stat().st_ino, decoy.read_bytes()) == before_decoy


def test_budget_export_audit_uses_the_already_pinned_database_connection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.planning import service as planning_service

    db_path = tmp_path / "ledger.db"
    decoy = tmp_path / "decoy.db"
    displaced = tmp_path / "canonical-during-audit.db"
    dest = tmp_path / "budgets.csv"
    init_database(db_path)
    init_database(decoy)
    _seed_budget(db_path, category="Canonical category", amount="10")
    _seed_budget(decoy, category="Private decoy marker", amount="999")
    before_canonical_audits = _audit_operations(db_path)
    before_decoy_audits = _audit_operations(decoy)
    original_record = planning_service.record_db_operation
    audit_hook_called = False

    def swap_only_during_audit(*args: object, **kwargs: object) -> None:
        nonlocal audit_hook_called
        audit_hook_called = True
        os.replace(db_path, displaced)
        os.replace(decoy, db_path)
        try:
            original_record(*args, **kwargs)
        finally:
            os.replace(db_path, decoy)
            os.replace(displaced, db_path)

    monkeypatch.setattr(planning_service, "record_db_operation", swap_only_during_audit)

    result = BudgetService(db_path, prepare_schema=False).export_csv(dest)

    assert result.ok, result.errors
    assert audit_hook_called
    assert _audit_operations(db_path) == [*before_canonical_audits, "budget_export"]
    assert _audit_operations(decoy) == before_decoy_audits
    assert "Private decoy marker" not in dest.read_text(encoding="utf-8")


@pytest.mark.parametrize("command", ["suggest", "validate", "export"])
def test_read_only_budget_cli_refuses_a_missing_database_without_creating_it(
    tmp_path: Path,
    command: str,
) -> None:
    db_path = tmp_path / "missing.db"
    arguments = ["budget", command, "--db", str(db_path)]
    if command == "suggest":
        arguments.extend(
            [
                "--start-date",
                "2026-01-01",
                "--end-date",
                "2026-01-31",
                "--dest",
                str(tmp_path / "suggestions.csv"),
            ]
        )
    elif command == "export":
        arguments.extend(["--dest", str(tmp_path / "budgets.csv")])

    result = runner.invoke(app, arguments)

    assert result.exit_code != 0
    assert not db_path.exists()


def test_budget_mid_insert_failure_rolls_back_rows_and_audit(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _seed_budget(db_path)
    before_rows = _budget_rows(db_path)
    audits_before = _audit_operations(db_path)
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_second_budget
            BEFORE INSERT ON budgets
            WHEN NEW.category = 'Second'
            BEGIN
                SELECT RAISE(ABORT, 'synthetic insert failure');
            END
            """
        )
        connection.commit()

    with pytest.raises(sqlite3.IntegrityError, match="synthetic insert failure"):
        BudgetService(db_path, prepare_schema=False).replace_budgets(
            [
                {"month": "*", "category": "First", "amount": "1", "notes": ""},
                {"month": "*", "category": "Second", "amount": "2", "notes": ""},
            ]
        )

    assert _budget_rows(db_path) == before_rows
    assert _audit_operations(db_path) == audits_before


def test_budget_writer_refuses_a_target_inode_handoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.planning import service as planning_service

    db_path = tmp_path / "ledger.db"
    decoy = tmp_path / "decoy.db"
    displaced = tmp_path / "displaced.db"
    init_database(db_path)
    _seed_budget(db_path)
    before = _budget_rows(db_path)
    init_database(decoy)
    _seed_budget(decoy, category="Decoy", amount="999")
    original_writer = planning_service.audited_database_write_connection

    @contextmanager
    def swap_before_writer(source: object, **kwargs: object):
        os.replace(db_path, displaced)
        os.replace(decoy, db_path)
        try:
            with original_writer(source, **kwargs) as connection:
                yield connection
        finally:
            os.replace(db_path, decoy)
            os.replace(displaced, db_path)

    monkeypatch.setattr(
        planning_service,
        "audited_database_write_connection",
        swap_before_writer,
    )
    with pytest.raises(OSError):
        BudgetService(db_path, prepare_schema=False).replace_budgets(
            [{"month": "*", "category": "Replacement", "amount": "20", "notes": ""}]
        )

    assert _budget_rows(db_path) == before
    assert _budget_rows(decoy) == [("*", "Decoy", "999", "before")]


def test_suggestions_ignore_absent_calendar_months_instead_of_zero_padding(
    tmp_path: Path,
) -> None:
    from test_budgets_p7 import _seed_transaction

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _seed_transaction(
        db_path,
        txn_id="jan",
        posted=date(2026, 1, 5),
        amount="-100",
        category="Dining",
    )
    _seed_transaction(
        db_path,
        txn_id="mar",
        posted=date(2026, 3, 5),
        amount="-300",
        category="Dining",
    )
    dest = tmp_path / "suggestions.csv"

    result = runner.invoke(
        app,
        [
            "budget",
            "suggest",
            "--db",
            str(db_path),
            "--start-date",
            "2026-01-01",
            "--end-date",
            "2026-03-31",
            "--dest",
            str(dest),
        ],
    )

    assert result.exit_code == 0, result.output
    with dest.open(newline="", encoding="utf-8") as handle:
        rows = list(__import__("csv").DictReader(handle))
    assert rows
    assert [(row["category"], row["amount"]) for row in rows] == [("Dining", "200.00")]


def test_suggestion_gross_basis_matches_ledger_aggregate_for_every_discriminator(
    tmp_path: Path,
) -> None:
    from test_budgets_p7 import _seed_transaction

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    candidates = (
        ("cad", "-100", "CAD", True, False, "expense"),
        ("null", "-20", None, True, False, "expense"),
        ("blank", "-30", "", True, False, "expense"),
        ("positive", "900", "CAD", True, False, "expense"),
        ("neutral", "-800", "CAD", True, False, "neutral"),
        ("not-cashflow", "-700", "CAD", False, False, "expense"),
        ("excluded", "-600", "CAD", True, True, "expense"),
        ("foreign", "-500", "USD", True, False, "expense"),
    )
    for txn_id, amount, currency, included, excluded, cashflow_type in candidates:
        _seed_transaction(
            db_path,
            txn_id=txn_id,
            posted=date(2026, 1, 10),
            amount=amount,
            category="Dining",
            currency=currency,
            include_in_cashflow=included,
            exclude_from_spending=excluded,
        )
        if cashflow_type != "expense":
            with session_scope(db_path) as session:
                session.query(TransactionDB).filter_by(txn_id=txn_id).update(
                    {TransactionDB.cashflow_type: cashflow_type}
                )
    aggregate = LedgerQueryService(db_path).get_spending_by_category(
        date(2026, 1, 1), date(2026, 1, 31)
    )
    dest = tmp_path / "suggestions.csv"
    result = runner.invoke(
        app,
        [
            "budget",
            "suggest",
            "--db",
            str(db_path),
            "--start-date",
            "2026-01-01",
            "--end-date",
            "2026-01-31",
            "--dest",
            str(dest),
        ],
    )

    assert result.exit_code == 0, result.output
    with dest.open(newline="", encoding="utf-8") as handle:
        rows = list(__import__("csv").DictReader(handle))
    assert rows
    assert aggregate == {"Dining": Decimal("150.00")}
    assert Decimal(rows[0]["amount"]) == aggregate["Dining"]


def _readonly_ui_driver(tmp_path: Path, page_module: str) -> Path:
    script = tmp_path / f"readonly_{page_module.rsplit('.', 1)[-1]}.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import os
            from datetime import date
            from pathlib import Path
            from ledger.config.repository import SQLiteConfigRepository
            from ledger.query import LedgerQueryService
            from ledger.ui.context import AppContext
            from ledger.planning import service as planning_service

            def _schema_write_forbidden(*_args, **_kwargs):
                raise AssertionError('BudgetService read callsite prepared schema')

            planning_service.ensure_schema = _schema_write_forbidden
            class _ReadOnlyRuleRuns:
                def __init__(self, *_args, **_kwargs):
                    pass
                def latest_run(self):
                    return None
            class _ReadOnlyInsights:
                def __init__(self, *_args, **_kwargs):
                    pass
                def list_findings(self, *_args, **_kwargs):
                    return []
                def served_period(self, *_args, **_kwargs):
                    return None
            db = Path(os.environ['_LEDGER_APPTEST_DB'])
            query = LedgerQueryService(db)
            config = SQLiteConfigRepository(db, prepare_schema=False).load_categories()
            ctx = AppContext(
                db_path=db,
                service=query,
                category_config=config,
                stats=query.get_stats(),
                earliest=date(2025, 12, 1),
                latest=date(2026, 1, 15),
                start_date=date(2025, 12, 1),
                end_date=date(2026, 1, 15),
                period_label='Synthetic period',
                review_source={{'label': 'db', 'status': 'live_db', 'db_path': db, 'source_kind': 'Current DB'}},
                review_db=db,
                pending_reviews=0,
            )
            import importlib
            page = importlib.import_module('{page_module}')
            if hasattr(page, 'RuleApplicationService'):
                page.RuleApplicationService = _ReadOnlyRuleRuns
            if hasattr(page, 'InsightEngine'):
                page.InsightEngine = _ReadOnlyInsights
            page.render(ctx)
            """
        ),
        encoding="utf-8",
    )
    return script


@pytest.mark.parametrize(
    "page_module",
    [
        "ledger.ui.pages.categories",
        "ledger.ui.pages.overview",
        "ledger.ui.pages.budgets_goals",
    ],
)
def test_every_budget_read_ui_callsite_is_no_prepare_on_owner_read_only_db(
    tmp_path: Path,
    page_module: str,
) -> None:
    from streamlit.testing.v1 import AppTest
    from test_ui_render_invocation import _seed_db

    from ledger.planning import service as planning_service
    from ledger.storage.migrations import ensure_schema

    db_path = tmp_path / "ledger.db"
    _seed_db(db_path)
    ensure_schema(db_path)
    original_ensure = planning_service.ensure_schema
    os.chmod(db_path, 0o444)
    before = db_path.read_bytes()
    os.environ["_LEDGER_APPTEST_DB"] = str(db_path)
    try:
        at = AppTest.from_file(
            str(_readonly_ui_driver(tmp_path, page_module)), default_timeout=30
        ).run()
        assert not at.exception
        assert stat.S_IMODE(db_path.stat().st_mode) == 0o444
        assert db_path.read_bytes() == before
    finally:
        planning_service.ensure_schema = original_ensure


def test_production_budget_service_callsite_census_is_explicit() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "ledger"
    calls: list[tuple[Path, int]] = []
    for source in root.rglob("*.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if isinstance(function, ast.Name) and function.id == "BudgetService":
                calls.append((source, node.lineno))
                [prepare_keyword] = [
                    keyword for keyword in node.keywords if keyword.arg == "prepare_schema"
                ]
                relative = source.relative_to(root)
                if relative == Path("planning/readiness.py"):
                    assert isinstance(prepare_keyword.value, ast.Name)
                    assert prepare_keyword.value.id == "prepare_schema"
                else:
                    assert isinstance(prepare_keyword.value, ast.Constant)
                    assert prepare_keyword.value.value is False, (
                        source,
                        node.lineno,
                    )
                assert prepare_keyword.arg == "prepare_schema", (
                    source,
                    node.lineno,
                )
    assert calls


@pytest.mark.parametrize(
    "method",
    ["list_budgets", "list_goals", "list_goal_accounts", "budget_variance", "validate"],
)
def test_prepare_schema_false_read_methods_never_create_a_missing_database(
    tmp_path: Path,
    method: str,
) -> None:
    db_path = tmp_path / "missing.db"
    before = sorted(path.name for path in tmp_path.iterdir())
    service = BudgetService(db_path, prepare_schema=False)

    with pytest.raises(FileNotFoundError):
        if method == "budget_variance":
            service.budget_variance(date(2026, 1, 1), date(2026, 1, 31))
        else:
            getattr(service, method)()

    assert not db_path.exists()
    assert sorted(path.name for path in tmp_path.iterdir()) == before


def test_prepare_schema_false_read_refuses_a_database_renamed_after_construction(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ledger.db"
    moved = tmp_path / "moved.db"
    init_database(db_path)
    before = db_path.read_bytes()
    service = BudgetService(db_path, prepare_schema=False)
    db_path.rename(moved)

    with pytest.raises(FileNotFoundError):
        service.list_budgets()

    assert not db_path.exists()
    assert moved.read_bytes() == before
    assert not Path(f"{db_path}-wal").exists()
    assert not Path(f"{db_path}-shm").exists()


@pytest.mark.parametrize("command", ["import", "export", "suggest", "validate"])
def test_budget_cli_callsites_skip_constructor_schema_preparation_at_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    from test_budgets_p7 import _seed_transaction

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _seed_budget(db_path)
    _seed_transaction(
        db_path,
        txn_id="cli-no-prepare",
        posted=date(2026, 1, 10),
        amount="-25",
        category="Dining",
    )
    source = tmp_path / "budgets.csv"
    _write_import(source)
    observed: list[object] = []
    original_init = BudgetService.__init__

    def observe_init(self: BudgetService, *args: object, **kwargs: object) -> None:
        observed.append(kwargs.get("prepare_schema", "default"))
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(BudgetService, "__init__", observe_init)
    arguments = ["budget", command, "--db", str(db_path)]
    if command == "import":
        arguments.extend(["--source", str(source)])
    elif command == "export":
        arguments.extend(["--dest", str(tmp_path / "export.csv")])
    elif command == "suggest":
        arguments.extend(
            [
                "--start-date",
                "2026-01-01",
                "--end-date",
                "2026-01-31",
                "--dest",
                str(tmp_path / "suggestions.csv"),
            ]
        )

    result = runner.invoke(app, arguments)

    assert result.exit_code == 0, result.output
    assert observed == [False]


def test_insight_budget_callsite_skips_constructor_schema_preparation_at_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.insights.service import InsightEngine

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    engine = InsightEngine(db_path)
    observed: list[object] = []
    original_init = BudgetService.__init__

    def observe_init(self: BudgetService, *args: object, **kwargs: object) -> None:
        observed.append(kwargs.get("prepare_schema", "default"))
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(BudgetService, "__init__", observe_init)

    assert engine._budget_findings(date(2026, 1, 1), date(2026, 1, 31)) == []
    assert observed == [False]


def test_readiness_and_mcp_budget_callsites_never_prepare_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_synthetic import build_synthetic_mcp_database

    from ledger.planning import PlanningReadinessService
    from ledger.planning import service as planning_service

    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    BudgetService(db_path).replace_budgets(
        [{"month": "*", "category": "Groceries", "amount": "100", "notes": ""}]
    )

    def schema_write_forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("read callsite prepared schema")

    monkeypatch.setattr(planning_service, "ensure_schema", schema_write_forbidden)
    summary = PlanningReadinessService(db_path, prepare_schema=False).summary(
        date(2025, 1, 1), date(2025, 12, 31)
    )
    assert summary["budgets"]["active"] == 1
    service = LedgerMCPService(db_path)
    try:
        envelope = call_tool(
            service,
            "ledger.budgets",
            {"start_date": "2025-01-01", "end_date": "2025-12-31"},
        )["structuredContent"]
    finally:
        service.close()
    assert "error" not in envelope


def test_published_budget_operator_contract_matches_the_real_cli() -> None:
    root = Path(__file__).resolve().parents[1]
    agents = (root / "docs/agent/configuration.md").read_text(encoding="utf-8")
    skill = (root / "docs/agent/skills/ledger-query/SKILL.md").read_text(encoding="utf-8")
    contracts = (root / "src/ledger/mcp/contracts.py").read_text(encoding="utf-8")
    command = (root / "src/ledger/commands/budget.py").read_text(encoding="utf-8")
    service = (root / "src/ledger/planning/service.py").read_text(encoding="utf-8")
    published_command = (
        "ledger budget suggest --db /absolute/private-ledger/ledger.db --start-date 2025-01-01 "
        "--end-date 2025-12-31 --dest /absolute/private-ledger/budget-suggestions.csv"
    )

    assert published_command in agents
    assert 'fieldnames=["month", "category", "amount", "notes"]' in service
    for text in (agents, skill, contracts):
        normalized = " ".join(text.split()).casefold()
        assert "month,category,amount,notes" in normalized
        assert "represented" in normalized
        assert "nonzero" in normalized
    for text in (agents, contracts):
        normalized = " ".join(text.split()).casefold()
        assert "dry run" in normalized or "dry-run" in normalized
        assert "--execute" in normalized
        assert "pre-write backup" in normalized
    assert "absent months stay absent" in service
    assert "represented nonzero-month" in command
    assert len(skill.split()) < 500
