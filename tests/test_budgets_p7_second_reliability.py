"""Second-round P7 reliability regressions for exact reads and CSV publication."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from ledger.cli import app
from ledger.mcp.server import LedgerMCPService, call_tool
from ledger.planning import BudgetService
from ledger.storage.database import init_database, session_scope

runner = CliRunner()


def _seed_budget(db_path: Path, *, category: str, amount: str) -> None:
    result = BudgetService(db_path).replace_budgets(
        [{"month": "*", "category": category, "amount": amount, "notes": "synthetic"}]
    )
    assert result.ok, result.errors


def _seed_spend(db_path: Path, *, category: str, amount: str) -> None:
    from test_budgets_p7 import _seed_transaction

    _seed_transaction(
        db_path,
        txn_id=f"spend-{category}-{amount}",
        posted=date(2026, 1, 15),
        amount=f"-{amount}",
        category=category,
    )


def _file_signature(path: Path) -> tuple[int, int, int, bytes] | None:
    try:
        metadata = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return None
    return (
        metadata.st_ino,
        stat.S_IMODE(metadata.st_mode),
        metadata.st_nlink,
        path.read_bytes(),
    )


def _sqlite_namespace(db_path: Path) -> dict[str, tuple[int, int, int, bytes] | None]:
    return {
        suffix or "main": _file_signature(Path(f"{db_path}{suffix}"))
        for suffix in ("", "-wal", "-shm", "-journal")
    }


@contextmanager
def _wal_without_shm(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Keep a fresh committed WAL alive while removing only its recreatable SHM."""
    init_database(db_path)
    _seed_budget(db_path, category="Canonical", amount="25.00")
    keeper = sqlite3.connect(db_path)
    try:
        assert keeper.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        keeper.execute("PRAGMA wal_autocheckpoint=0")
        keeper.execute("UPDATE budgets SET notes = 'committed in WAL'")
        keeper.commit()
        wal = Path(f"{db_path}-wal")
        shm = Path(f"{db_path}-shm")
        assert wal.is_file()
        wal.chmod(0o600)
        shm.unlink()
        assert not shm.exists()
        db_path.chmod(0o444)
        yield keeper
    finally:
        db_path.chmod(0o600)
        keeper.close()


@pytest.mark.parametrize(
    "method",
    ["list_budgets", "list_goals", "list_goal_accounts", "budget_variance", "validate"],
)
def test_all_default_read_safe_methods_preserve_fresh_wal_without_canonical_shm(
    tmp_path: Path,
    method: str,
) -> None:
    db_path = tmp_path / "ledger.db"
    with _wal_without_shm(db_path):
        before = _sqlite_namespace(db_path)
        service = BudgetService(db_path, prepare_schema=False)

        if method == "budget_variance":
            value = service.budget_variance(date(2026, 1, 1), date(2026, 1, 31))
        else:
            value = getattr(service, method)()

        assert value is not None
        assert _sqlite_namespace(db_path) == before


def test_direct_suggestion_read_preserves_fresh_wal_without_canonical_shm(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ledger.db"
    with _wal_without_shm(db_path):
        before = _sqlite_namespace(db_path)

        rows = BudgetService(db_path, prepare_schema=False).suggest_budgets(
            date(2026, 1, 1), date(2026, 1, 31)
        )

        assert rows == []
        assert _sqlite_namespace(db_path) == before


def test_default_budget_reader_never_returns_a_connect_time_path_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.storage import snapshots

    db_path = tmp_path / "ledger.db"
    decoy = tmp_path / "decoy.db"
    displaced = tmp_path / "displaced.db"
    init_database(db_path)
    init_database(decoy)
    _seed_budget(db_path, category="Canonical", amount="25.00")
    _seed_budget(decoy, category="Alternate sentinel", amount="999.00")
    real_connect = snapshots.sqlite3.connect
    swapped = False

    def connect_during_identity_change(database: object, *args: object, **kwargs: object):
        nonlocal swapped
        if not swapped:
            swapped = True
            os.replace(db_path, displaced)
            os.replace(decoy, db_path)
            try:
                return real_connect(database, *args, **kwargs)
            finally:
                os.replace(db_path, decoy)
                os.replace(displaced, db_path)
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(snapshots.sqlite3, "connect", connect_during_identity_change)

    rows = BudgetService(db_path, prepare_schema=False).list_budgets()

    assert swapped
    assert [row["category"] for row in rows] == ["Canonical"]


def test_budget_variance_uses_one_logical_session_not_a_hybrid_source(
    tmp_path: Path,
) -> None:
    old_db = tmp_path / "old.db"
    new_db = tmp_path / "new.db"
    init_database(old_db)
    init_database(new_db)
    _seed_budget(old_db, category="Dining", amount="25.00")
    _seed_spend(old_db, category="Dining", amount="100.00")
    _seed_budget(new_db, category="Dining", amount="900.00")
    _seed_spend(new_db, category="Dining", amount="900.00")
    calls = 0

    @contextmanager
    def switched_factory(_path: Path) -> Iterator[Session]:
        nonlocal calls
        calls += 1
        selected = new_db if calls == 1 else old_db
        with session_scope(selected) as session:
            yield session

    row = BudgetService(
        old_db,
        prepare_schema=False,
        session_scope_factory=switched_factory,
    ).budget_variance(date(2026, 1, 1), date(2026, 1, 31))[0]

    assert calls == 1
    assert (row["actual"], row["budget"]) in {
        (Decimal("100.00"), Decimal("25.00")),
        (Decimal("900.00"), Decimal("900.00")),
    }
    assert (row["actual"], row["budget"]) != (Decimal("900.00"), Decimal("25.00"))


def _replace_raw_budget(db_path: Path, *, amount_sql: str, category_sql: str = "'Dining'") -> None:
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute("DELETE FROM budgets")
        connection.execute(
            f"""
            INSERT INTO budgets (
                budget_month, category, amount, notes, created_at, updated_at
            ) VALUES (
                '*', {category_sql}, {amount_sql}, 'raw sentinel notes',
                '2026-01-01 00:00:00', '2026-01-01 00:00:00'
            )
            """
        )
        connection.commit()


@pytest.mark.parametrize(
    ("case", "amount_sql", "category_sql"),
    [
        ("subcent-low", "1.001", "'Dining'"),
        ("subcent-half", "1.005", "'Dining'"),
        ("nonnumeric", "'raw numeric sentinel'", "'Dining'"),
        ("nonfinite", "1e999", "'Dining'"),
        ("overflow", "10000000000.00", "'Dining'"),
        ("blob-category", "1.00", "X'7261772063617465676f72792073656e74696e656c'"),
    ],
)
def test_raw_budget_corruption_is_content_free_in_service_cli_and_mcp(
    tmp_path: Path,
    case: str,
    amount_sql: str,
    category_sql: str,
) -> None:
    from mcp_synthetic import build_synthetic_mcp_database

    db_path = build_synthetic_mcp_database(tmp_path / f"{case}.db")
    _replace_raw_budget(db_path, amount_sql=amount_sql, category_sql=category_sql)
    before = db_path.read_bytes()

    with pytest.raises(ValueError):
        BudgetService(db_path, prepare_schema=False).list_budgets()

    validation = BudgetService(db_path, prepare_schema=False).validate()
    assert not validation.ok
    assert validation.details.get("committed") is False
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
    cli = runner.invoke(app, ["budget", "validate", "--db", str(db_path)])
    rendered = cli.output + (str(cli.exception) if cli.exception else "")
    assert cli.exit_code == 1
    assert "Traceback" not in rendered
    assert str(tmp_path) not in rendered
    assert "raw sentinel" not in rendered
    assert "SELECT" not in rendered
    assert db_path.read_bytes() == before


def test_missing_budget_table_is_a_clean_incompatible_cli_refusal(tmp_path: Path) -> None:
    db_path = tmp_path / "missing-table.db"
    init_database(db_path)
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute("DROP TABLE budgets")
        connection.commit()
    before = db_path.read_bytes()

    result = BudgetService(db_path, prepare_schema=False).validate()
    cli = runner.invoke(app, ["budget", "validate", "--db", str(db_path)])

    assert not result.ok
    assert result.details.get("committed") is False
    assert cli.exit_code == 1
    assert "Traceback" not in cli.output
    assert str(tmp_path) not in cli.output
    assert "SELECT" not in cli.output
    assert db_path.read_bytes() == before


def test_real_mcp_budget_call_uses_its_injected_pinned_session_during_identity_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_synthetic import build_synthetic_mcp_database

    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    decoy = build_synthetic_mcp_database(tmp_path / "decoy.db")
    displaced = tmp_path / "displaced.db"
    _seed_budget(db_path, category="Canonical", amount="25.00")
    _seed_budget(decoy, category="Alternate sentinel", amount="999.00")
    service = LedgerMCPService(db_path)
    real_scope = service.database.session_scope
    calls = 0

    @contextmanager
    def observed_pinned_scope(path: Path) -> Iterator[Session]:
        nonlocal calls
        calls += 1
        with real_scope(path) as session:
            os.replace(db_path, displaced)
            os.replace(decoy, db_path)
            try:
                yield session
            finally:
                os.replace(db_path, decoy)
                os.replace(displaced, db_path)

    def forbidden_fallback(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("MCP fell back to BudgetService lexical read session")

    monkeypatch.setattr(service.database, "session_scope", observed_pinned_scope)
    monkeypatch.setattr(BudgetService, "_open_read_only_session", forbidden_fallback)
    try:
        envelope = call_tool(
            service,
            "ledger.budgets",
            {"start_date": "2026-01-01", "end_date": "2026-01-31"},
        )["structuredContent"]
    finally:
        service.close()

    assert calls >= 1
    assert "error" not in envelope
    assert [entry["category"] for entry in envelope["data"]["entries"]] == ["Canonical"]
    assert "Alternate sentinel" not in json.dumps(envelope)


def test_budget_variance_accepts_the_last_representable_calendar_month(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _seed_budget(db_path, category="Dining", amount="25.00")

    rows = BudgetService(db_path, prepare_schema=False).budget_variance(
        date(9999, 12, 1), date(9999, 12, 31)
    )

    assert rows[0]["budget"] == Decimal("25.00")


def _invoke_budget_output(operation: str, db_path: Path, dest: Path):
    arguments = ["budget", operation, "--db", str(db_path), "--dest", str(dest)]
    if operation == "suggest":
        arguments.extend(["--start-date", "2026-01-01", "--end-date", "2026-01-31"])
    return runner.invoke(app, arguments)


def _tree_signature(root: Path) -> dict[str, tuple[str, int, int, bytes | str]]:
    signatures: dict[str, tuple[str, int, int, bytes | str]] = {}
    for path in sorted(root.rglob("*")):
        metadata = path.lstat()
        relative = str(path.relative_to(root))
        if path.is_symlink():
            signatures[relative] = (
                "symlink",
                metadata.st_ino,
                stat.S_IMODE(metadata.st_mode),
                os.readlink(path),
            )
        elif path.is_file():
            signatures[relative] = (
                "file",
                metadata.st_ino,
                stat.S_IMODE(metadata.st_mode),
                path.read_bytes(),
            )
        else:
            signatures[relative] = (
                "directory",
                metadata.st_ino,
                stat.S_IMODE(metadata.st_mode),
                b"",
            )
    return signatures


@pytest.mark.parametrize("operation", ["export", "suggest"])
@pytest.mark.parametrize(
    "case",
    ["traversal", "final-symlink", "parent-symlink", "reserved-lock", "reserved-prefix"],
)
def test_real_budget_cli_preserves_lexical_destination_evidence(
    tmp_path: Path,
    operation: str,
    case: str,
) -> None:
    db_parent = tmp_path / "database"
    output_parent = tmp_path / "output"
    db_parent.mkdir(mode=0o700)
    output_parent.mkdir(mode=0o700)
    db_path = db_parent / "ledger.db"
    init_database(db_path)
    _seed_budget(db_path, category="Dining", amount="25.00")
    _seed_spend(db_path, category="Dining", amount="25.00")
    protected = output_parent / "protected.txt"
    protected.write_bytes(b"unrelated sentinel")
    protected.chmod(0o600)
    if case == "traversal":
        (output_parent / "child").mkdir(mode=0o700)
        dest = output_parent / "child" / ".." / "result.csv"
    elif case == "final-symlink":
        dest = output_parent / "result.csv"
        dest.symlink_to(protected)
    elif case == "parent-symlink":
        real_parent = output_parent / "real"
        real_parent.mkdir(mode=0o700)
        linked_parent = output_parent / "linked"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        dest = linked_parent / "result.csv"
    else:
        reserved = (
            ".ledger-sqlite-alias.lock"
            if case == "reserved-lock"
            else ".ledger-sqlite-alias-parent"
        )
        reserved_parent = output_parent / reserved
        reserved_parent.mkdir(mode=0o700)
        dest = reserved_parent / "result.csv"
    before = _tree_signature(tmp_path)

    result = _invoke_budget_output(operation, db_path, dest)

    assert result.exit_code == 1
    assert _tree_signature(tmp_path) == before
    assert "Traceback" not in result.output
    assert str(tmp_path) not in result.output


@pytest.mark.parametrize("operation", ["export", "suggest"])
def test_budget_csv_publication_requires_an_absent_destination(
    tmp_path: Path,
    operation: str,
) -> None:
    db_path = tmp_path / "ledger.db"
    dest = tmp_path / "result.csv"
    init_database(db_path)
    _seed_budget(db_path, category="Dining", amount="25.00")
    _seed_spend(db_path, category="Dining", amount="25.00")
    dest.write_bytes(b"existing destination sentinel")
    dest.chmod(0o600)
    before = _file_signature(dest)
    service = BudgetService(db_path, prepare_schema=False)

    result = (
        service.export_csv(dest)
        if operation == "export"
        else service.export_suggestions(dest, date(2026, 1, 1), date(2026, 1, 31))
    )

    assert not result.ok
    assert result.details.get("committed") is False
    assert result.artifacts == {}
    assert _file_signature(dest) == before


@pytest.mark.parametrize("operation", ["export", "suggest"])
@pytest.mark.parametrize(
    "artifact_kind",
    ["regular-csv", "database-main", "wal", "shm", "journal", "alias-lock", "unrelated"],
)
def test_budget_csv_last_install_seam_never_clobbers_an_interposed_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    artifact_kind: str,
) -> None:
    from ledger.planning import service as planning_service

    database_parent = tmp_path / "database"
    output_parent = tmp_path / "output"
    artifacts_parent = tmp_path / "artifacts"
    for parent in (database_parent, output_parent, artifacts_parent):
        parent.mkdir(mode=0o700)
    db_path = database_parent / "ledger.db"
    dest = output_parent / "result.csv"
    init_database(db_path)
    _seed_budget(db_path, category="Dining", amount="25.00")
    _seed_spend(db_path, category="Dining", amount="25.00")
    if artifact_kind == "database-main":
        artifact = artifacts_parent / "other.db"
        init_database(artifact)
    else:
        artifact_name = {
            "regular-csv": "existing.csv",
            "wal": "other.db-wal",
            "shm": "other.db-shm",
            "journal": "other.db-journal",
            "alias-lock": ".ledger-sqlite-alias.lock",
            "unrelated": "unrelated.txt",
        }[artifact_kind]
        artifact = artifacts_parent / artifact_name
        artifact.write_bytes(f"{artifact_kind} sentinel".encode())
        artifact.chmod(0o600)
    BudgetService(db_path, prepare_schema=False).list_budgets()
    artifact_before = _file_signature(artifact)
    names_before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    real_link = planning_service.os.link
    interposed = False

    def interpose_before_install(
        source: object,
        destination: object,
        *args: object,
        **kwargs: object,
    ) -> None:
        nonlocal interposed
        if (
            not interposed
            and str(destination) == dest.name
            and str(source).startswith(f".{dest.name}.")
            and str(source).endswith(".tmp")
        ):
            interposed = True
            real_link(artifact, dest)
        real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(planning_service.os, "link", interpose_before_install)
    service = BudgetService(db_path, prepare_schema=False)

    result = (
        service.export_csv(dest)
        if operation == "export"
        else service.export_suggestions(dest, date(2026, 1, 1), date(2026, 1, 31))
    )

    assert interposed
    assert not result.ok
    assert result.details.get("committed") is False
    assert result.artifacts == {}
    artifact_after = _file_signature(artifact)
    assert artifact_after is not None and artifact_before is not None
    assert artifact_after[:2] == artifact_before[:2]
    assert artifact_after[3] == artifact_before[3]
    assert dest.stat().st_ino == artifact.stat().st_ino
    assert dest.read_bytes() == artifact.read_bytes()
    added_names = {str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")} - set(
        names_before
    )
    assert added_names == {str(dest.relative_to(tmp_path))}


@pytest.mark.parametrize("operation", ["export", "suggest"])
def test_budget_csv_success_is_owner_only_and_reports_durable_installation(
    tmp_path: Path,
    operation: str,
) -> None:
    db_path = tmp_path / "ledger.db"
    dest = tmp_path / "result.csv"
    init_database(db_path)
    _seed_budget(db_path, category="Dining", amount="25.00")
    _seed_spend(db_path, category="Dining", amount="25.00")
    service = BudgetService(db_path, prepare_schema=False)

    result = (
        service.export_csv(dest)
        if operation == "export"
        else service.export_suggestions(dest, date(2026, 1, 1), date(2026, 1, 31))
    )

    assert result.ok, result.errors
    assert result.details.get("committed") is True
    assert result.artifacts == {"csv": dest}
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    assert dest.read_text(encoding="utf-8").startswith("month,category,amount,notes")


def _assert_committed_artifact_failure(result: Any, dest: Path, tmp_path: Path) -> None:
    assert not result.ok
    assert result.artifacts == {"csv": dest}
    assert result.details.get("committed") is True
    assert dest.is_file()
    rendered = " ".join(result.errors)
    assert "publication succeeded" in rendered.casefold()
    assert "did not complete" in rendered.casefold()
    assert "synthetic" not in rendered
    assert str(tmp_path) not in rendered


def test_budget_export_reports_a_real_postpublication_audit_trigger_failure_truthfully(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ledger.db"
    dest = tmp_path / "result.csv"
    init_database(db_path)
    _seed_budget(db_path, category="Dining", amount="25.00")
    with closing(sqlite3.connect(db_path)) as connection:
        connection.execute(
            """
            CREATE TRIGGER fail_budget_export_audit
            BEFORE INSERT ON db_operation_audit
            WHEN NEW.operation = 'budget_export'
            BEGIN
                SELECT RAISE(ABORT, 'synthetic audit sentinel');
            END
            """
        )
        connection.commit()

    result = BudgetService(db_path, prepare_schema=False).export_csv(dest)

    _assert_committed_artifact_failure(result, dest, tmp_path)


@pytest.mark.parametrize("operation", ["export", "suggest"])
@pytest.mark.parametrize("failure", ["validation", "output-parent", "database-pin", "lock"])
def test_budget_csv_postpublication_boundary_failures_keep_committed_truth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    failure: str,
) -> None:
    from ledger.planning import service as planning_service

    database_parent = tmp_path / "database"
    output_parent = tmp_path / "output"
    database_parent.mkdir(mode=0o700)
    output_parent.mkdir(mode=0o700)
    db_path = database_parent / "ledger.db"
    dest = output_parent / "result.csv"
    init_database(db_path)
    _seed_budget(db_path, category="Dining", amount="25.00")
    _seed_spend(db_path, category="Dining", amount="25.00")
    service = BudgetService(db_path, prepare_schema=False)

    if failure == "validation":
        real_validation = planning_service._require_safe_pinned_budget_output
        calls = 0

        def fail_postpublication_validation(*args: object, **kwargs: object) -> None:
            nonlocal calls
            calls += 1
            real_validation(*args, **kwargs)
            if calls >= 2:
                raise OSError("synthetic postpublication validation failure")

        monkeypatch.setattr(
            planning_service,
            "_require_safe_pinned_budget_output",
            fail_postpublication_validation,
        )
    elif failure == "output-parent":
        real_parent_pin = planning_service.pin_existing_directory

        @contextmanager
        def fail_output_parent_exit(path: Path):
            with real_parent_pin(path) as pinned:
                yield pinned
            if Path(path) == output_parent:
                raise OSError("synthetic output-parent exit failure")

        monkeypatch.setattr(planning_service, "pin_existing_directory", fail_output_parent_exit)
    elif failure == "database-pin":
        real_database_pin = service._pin_mutable_target

        @contextmanager
        def fail_database_pin_exit():
            with real_database_pin() as pinned:
                yield pinned
            raise OSError("synthetic database-pin exit failure")

        monkeypatch.setattr(service, "_pin_mutable_target", fail_database_pin_exit)
    else:
        real_lock = planning_service.database_target_lock

        @contextmanager
        def fail_lock_exit(path: Path):
            with real_lock(path) as lock:
                yield lock
            raise OSError("synthetic lock exit failure")

        monkeypatch.setattr(planning_service, "database_target_lock", fail_lock_exit)

    result = (
        service.export_csv(dest)
        if operation == "export"
        else service.export_suggestions(dest, date(2026, 1, 1), date(2026, 1, 31))
    )

    _assert_committed_artifact_failure(result, dest, tmp_path)


def test_budget_help_and_published_docs_state_the_bounded_operator_truth() -> None:
    from ledger.mcp.contracts import BUDGET_SELECTION_STATEMENT

    root = Path(__file__).resolve().parents[1]
    agents = (root / "docs/agent/configuration.md").read_text(encoding="utf-8")
    budget_help = runner.invoke(app, ["budget", "--help"])
    export_help = runner.invoke(app, ["budget", "export", "--help"])
    suggest_help = runner.invoke(app, ["budget", "suggest", "--help"])

    assert "suggest" in budget_help.output
    for result in (export_help, suggest_help):
        assert result.exit_code == 0
        assert "must not exist" in result.output
    for text in (agents,):
        assert "destination must not exist" in " ".join(text.split()).casefold()
    selection = BUDGET_SELECTION_STATEMENT.casefold()
    assert "partial-month" in selection
    assert "prorat" in selection
    assert "add" in selection
    assert "recurring" in selection
    assert "month-specific" in selection


@pytest.mark.parametrize(
    ("operation", "outcome"),
    [
        ("export", "success"),
        ("suggest", "success"),
        ("export", "prepublication-refusal"),
        ("suggest", "no-result"),
    ],
)
def test_budget_csv_output_parent_pin_is_observational_on_existing_shared_directory(
    tmp_path: Path,
    operation: str,
    outcome: str,
) -> None:
    database_parent = tmp_path / "database"
    output_parent = tmp_path / "shared-output"
    database_parent.mkdir(mode=0o700)
    output_parent.mkdir(mode=0o755)
    output_parent.chmod(0o755)
    db_path = database_parent / "ledger.db"
    dest = output_parent / "result.csv"
    init_database(db_path)
    _seed_budget(db_path, category="Dining", amount="25.00")
    if outcome in {"success", "prepublication-refusal"}:
        _seed_spend(db_path, category="Dining", amount="25.00")
    if outcome == "prepublication-refusal":
        dest.write_bytes(b"existing destination sentinel")
        dest.chmod(0o600)
    parent_metadata = output_parent.stat(follow_symlinks=False)
    parent_before = (
        parent_metadata.st_dev,
        parent_metadata.st_ino,
        stat.S_IMODE(parent_metadata.st_mode),
    )
    destination_before = _file_signature(dest)
    service = BudgetService(db_path, prepare_schema=False)

    result = (
        service.export_csv(dest)
        if operation == "export"
        else service.export_suggestions(dest, date(2026, 1, 1), date(2026, 1, 31))
    )

    parent_metadata = output_parent.stat(follow_symlinks=False)
    assert (
        parent_metadata.st_dev,
        parent_metadata.st_ino,
        stat.S_IMODE(parent_metadata.st_mode),
    ) == parent_before
    if outcome == "success":
        assert result.ok, result.errors
        assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    else:
        assert not result.ok
        assert result.details.get("committed") is False
        assert _file_signature(dest) == destination_before
