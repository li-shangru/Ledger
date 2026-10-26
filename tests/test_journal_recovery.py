"""Real process termination at the two-store planning commit boundaries."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
import threading
from contextlib import closing
from pathlib import Path

import pytest
from test_planning_proposals import _budget_intent, _setup

from ledger.mcp.service import LedgerMCPService
from ledger.proposals import ProposalService
from ledger.storage.read_only import ReadOnlyUnavailableError
from ledger.storage.snapshots import database_target_lock, recover_database_journal

_CRASH_WRITER = """
import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path

from ledger.proposals import ProposalService
import ledger.proposals.planning_apply as implementation

request = json.load(sys.stdin)
original = implementation.audited_database_write_connection

@contextmanager
def interrupted_writer(*args, **kwargs):
    with original(*args, **kwargs) as connection:
        if request['spill']:
            connection.execute('PRAGMA cache_size=1')
            connection.execute('PRAGMA cache_spill=ON')
        yield connection
        if request['boundary'] == 'before_commit':
            os._exit(73)
    os._exit(73)

implementation.audited_database_write_connection = interrupted_writer
service = ProposalService(Path(request['store']))
method = getattr(service, request['operation'] + '_planning_proposal')
method(request['proposal_id'], ledger_db=Path(request['db']), **request['arguments'])
raise RuntimeError('The crash boundary was not reached')
"""


def _crashed_proposal(
    tmp_path: Path, *, spill: bool, boundary: str, operation: str = "apply"
) -> tuple[Path, ProposalService, str, dict[str, str]]:
    db, service = _setup(tmp_path)
    proposal = service.file_planning_change(_budget_intent(db))
    preview = service.preview_planning_proposal(proposal.proposal_id, ledger_db=db)
    arguments = {
        "expected_proposal_revision": proposal.proposal_revision,
        "expected_preview_revision": preview.preview_revision,
    }
    if operation == "undo":
        applied = service.apply_planning_proposal(proposal.proposal_id, ledger_db=db, **arguments)
        assert applied.ok, applied.errors
        arguments = {"expected_after_revision": applied.details["after_revision"]}
    process = subprocess.run(
        [sys.executable, "-c", _CRASH_WRITER],
        input=json.dumps(
            {
                "db": str(db),
                "store": str(service.proposals_db),
                "proposal_id": proposal.proposal_id,
                "arguments": arguments,
                "spill": spill,
                "boundary": boundary,
                "operation": operation,
            }
        ),
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert process.returncode == 73, process.stderr
    store_journal = Path(f"{service.proposals_db}-journal")
    assert store_journal.stat().st_size > 512
    assert store_journal.read_bytes()[:8] == bytes(8)
    if boundary == "before_commit":
        journal = Path(f"{db}-journal")
        assert journal.stat().st_size > 512
        assert journal.read_bytes()[:8] == (
            bytes.fromhex("d9d505f920a163d7") if spill else bytes(8)
        )
    return db, service, proposal.proposal_id, arguments


def _assert_recovered(db: Path, service: ProposalService, *, budget_count: int) -> None:
    for path in (db, service.proposals_db):
        assert not Path(f"{path}-journal").exists()
        assert (path.stat().st_mode & 0o777) == 0o600
        with closing(sqlite3.connect(path)) as connection:
            assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    with closing(sqlite3.connect(db)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM budgets").fetchone()[0] == budget_count
        assert (
            connection.execute("SELECT COUNT(*) FROM planning_proposal_receipts").fetchone()[0] == 1
        )
    reader = LedgerMCPService(db)
    reader.close()


@pytest.mark.parametrize("spill", [False, True], ids=["inactive-journal", "hot-journal"])
@pytest.mark.parametrize("boundary", ["before_commit", "after_commit"])
@pytest.mark.parametrize("operation", ["apply", "undo"])
def test_exact_retry_recovers_both_stores_after_process_death(
    tmp_path: Path, spill: bool, boundary: str, operation: str
) -> None:
    db, service, proposal_id, arguments = _crashed_proposal(
        tmp_path, spill=spill, boundary=boundary, operation=operation
    )
    method = getattr(service, f"{operation}_planning_proposal")
    result = method(proposal_id, ledger_db=db, **arguments)
    assert result.ok, result.errors
    repeated = method(proposal_id, ledger_db=db, **arguments)
    assert repeated.ok, repeated.errors
    _assert_recovered(db, service, budget_count=1 if operation == "apply" else 0)
    assert service.show_planning_proposal(proposal_id).status == (
        "applied" if operation == "apply" else "undone"
    )


@pytest.mark.parametrize("spill", [False, True], ids=["inactive-journal", "hot-journal"])
def test_local_storage_recovery_reopens_preview_without_applying_intent(
    tmp_path: Path, spill: bool
) -> None:
    db, service, proposal_id, arguments = _crashed_proposal(
        tmp_path, spill=spill, boundary="before_commit"
    )
    paths = (
        db,
        Path(f"{db}-journal"),
        service.proposals_db,
        Path(f"{service.proposals_db}-journal"),
    )
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
    with pytest.raises(ReadOnlyUnavailableError):
        LedgerMCPService(db)
    with pytest.raises(OSError):
        service.preview_planning_proposal(proposal_id, ledger_db=db)
    assert before == {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}

    result = service.recover_planning_storage(ledger_db=db)
    assert result.ok, result.errors
    preview = service.preview_planning_proposal(proposal_id, ledger_db=db)
    assert preview.recovery_required
    assert preview.application_preview_revision == arguments["expected_preview_revision"]
    with closing(sqlite3.connect(db)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM budgets").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM planning_proposal_receipts").fetchone()[0] == 0
        )
    assert service.show_planning_proposal(proposal_id).status == "pending"
    applied = service.apply_planning_proposal(proposal_id, ledger_db=db, **arguments)
    assert applied.ok, applied.errors
    _assert_recovered(db, service, budget_count=1)


def test_recovery_command_reopens_both_stores_without_deciding_proposal(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from ledger.cli import app

    db, service, proposal_id, _ = _crashed_proposal(tmp_path, spill=True, boundary="before_commit")
    result = CliRunner().invoke(
        app,
        ["proposals", "recover", "--db", str(db), "--proposals-db", str(service.proposals_db)],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "status": "success",
        "proposal_store_recovered": True,
        "financial_database_recovered": True,
    }
    assert service.show_planning_proposal(proposal_id).status == "pending"
    assert service.preview_planning_proposal(proposal_id, ledger_db=db).recovery_required


@pytest.mark.parametrize("boundary", ["before_commit", "after_commit"])
@pytest.mark.parametrize("spill", [False, True], ids=["inactive-journal", "hot-journal"])
def test_dashboard_can_recover_before_opening_a_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str, spill: bool
) -> None:
    from streamlit.testing.v1 import AppTest
    from test_planning_operator import _proposal_app

    db, service, proposal_id, _ = _crashed_proposal(tmp_path, spill=spill, boundary=boundary)
    monkeypatch.setenv("LEDGER_DB", str(db))
    paths = (
        db,
        Path(f"{db}-journal"),
        service.proposals_db,
        Path(f"{service.proposals_db}-journal"),
    )
    before = {path: path.read_bytes() for path in paths if path.exists()}
    page = AppTest.from_function(_proposal_app, default_timeout=30).run()
    assert not page.exception
    assert before == {path: path.read_bytes() for path in before}
    if boundary == "before_commit":
        page.button(key="ledger_storage_recover").click().run()
        assert not page.exception
    page.button(key="planning_storage_recover").click().run()
    assert not page.exception
    assert not page.error
    page.button(key="planning_preview_button").click().run()
    assert not page.exception
    assert page.button(key="planning_recover")
    assert service.show_planning_proposal(proposal_id).status == "pending"


@pytest.mark.parametrize("spill", [False, True], ids=["inactive-journal", "hot-journal"])
def test_dashboard_cannot_recover_during_a_promotable_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, spill: bool
) -> None:
    from streamlit.testing.v1 import AppTest
    from test_planning_operator import _proposal_app
    from test_planning_proposals import _open_run

    db, service, _proposal_id, _ = _crashed_proposal(
        tmp_path, spill=spill, boundary="before_commit"
    )
    _open_run(db, tmp_path / "custom-runs" / "open")
    paths = (
        db,
        Path(f"{db}-journal"),
        service.proposals_db,
        Path(f"{service.proposals_db}-journal"),
    )
    before = {path: path.read_bytes() for path in paths}
    monkeypatch.setenv("LEDGER_DB", str(db))
    page = AppTest.from_function(_proposal_app, default_timeout=30).run()
    assert not page.exception
    assert before == {path: path.read_bytes() for path in before}
    page.button(key="ledger_storage_recover").click().run()
    assert not page.exception
    assert any("import" in item.value.lower() for item in page.error)
    assert before == {path: path.read_bytes() for path in before}


def test_clean_storage_recovery_is_byte_exact_and_idempotent(tmp_path: Path) -> None:
    db, service = _setup(tmp_path)
    service.file_planning_change(_budget_intent(db))
    before = {path: path.read_bytes() for path in (db, service.proposals_db)}
    for _ in range(2):
        result = service.recover_planning_storage(ledger_db=db)
        assert result.ok
        assert result.details == {
            "proposal_store_recovered": False,
            "financial_database_recovered": False,
        }
        assert before == {path: path.read_bytes() for path in before}


@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "public", "malformed", "wal"])
def test_recovery_refuses_unsafe_journal_without_changing_the_files(
    tmp_path: Path, unsafe: str
) -> None:
    db, _service, _proposal_id, _ = _crashed_proposal(
        tmp_path, spill=False, boundary="before_commit"
    )
    journal = Path(f"{db}-journal")
    retained = db.parent / "retained"
    if unsafe == "symlink":
        journal.rename(retained)
        journal.symlink_to(retained)
    elif unsafe == "hardlink":
        retained.hardlink_to(journal)
    elif unsafe == "public":
        journal.chmod(0o644)
    elif unsafe == "malformed":
        with journal.open("r+b") as file:
            file.write(b"invalid!")
    else:
        wal = Path(f"{db}-wal")
        wal.write_bytes(b"unexpected WAL")
        wal.chmod(0o600)
    before = {path: path.read_bytes() for path in (db, journal)}
    with database_target_lock(db), pytest.raises(OSError):
        recover_database_journal(db, label="Synthetic recovery")
    assert before == {path: path.read_bytes() for path in before}
    if unsafe == "symlink":
        assert journal.is_symlink()


@pytest.mark.parametrize("operation", ["recover", "apply", "undo"])
def test_recovery_cannot_change_financial_storage_during_a_promotable_import(
    tmp_path: Path, operation: str
) -> None:
    from test_planning_proposals import _open_run

    from ledger.imports.pdf.readiness import LiveMutationDuringOpenImportRefused

    db, service, proposal_id, arguments = _crashed_proposal(
        tmp_path,
        spill=True,
        boundary="before_commit",
        operation="undo" if operation == "undo" else "apply",
    )
    _open_run(db, tmp_path / "custom-runs" / "open")
    before = {path: path.read_bytes() for path in (db, Path(f"{db}-journal"))}
    with pytest.raises(LiveMutationDuringOpenImportRefused):
        if operation == "recover":
            service.recover_planning_storage(ledger_db=db)
        else:
            getattr(service, f"{operation}_planning_proposal")(
                proposal_id, ledger_db=db, **arguments
            )
    assert before == {path: path.read_bytes() for path in before}


def test_recovery_waits_for_an_active_sqlite_writer_and_preserves_its_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ledger.storage import snapshots

    db, _service = _setup(tmp_path)
    observed = threading.Event()
    finished = threading.Event()
    original = snapshots._recover_existing_writer_journal
    outcomes: list[object] = []

    def observe_recovery(*args, **kwargs):
        result = original(*args, **kwargs)
        observed.set()
        return result

    monkeypatch.setattr(snapshots, "_recover_existing_writer_journal", observe_recovery)

    def recover() -> None:
        try:
            with database_target_lock(db):
                outcomes.append(recover_database_journal(db, label="Synthetic competing writer"))
        except BaseException as exc:
            outcomes.append(exc)
        finally:
            finished.set()

    script = """
import sqlite3, sys
from contextlib import closing
with closing(sqlite3.connect(sys.argv[1], isolation_level=None)) as connection:
    connection.execute('BEGIN IMMEDIATE')
    connection.execute("INSERT INTO config_metadata (key, value) VALUES ('other-writer', 'retained')")
    print('ready', flush=True)
    sys.stdin.readline()
    connection.commit()
"""
    with subprocess.Popen(
        [sys.executable, "-c", script, str(db)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
        assert Path(f"{db}-journal").read_bytes()[:8] == bytes(8)
        worker = threading.Thread(target=recover)
        worker.start()
        try:
            assert observed.wait(timeout=3)
            assert not finished.wait(timeout=0.1)
        finally:
            _stdout, stderr = process.communicate(input="commit\n", timeout=10)
            worker.join(timeout=10)
        assert process.returncode == 0, stderr
    assert not worker.is_alive()
    assert outcomes == [True]
    assert not Path(f"{db}-journal").exists()
    with closing(sqlite3.connect(db)) as connection:
        assert connection.execute(
            "SELECT value FROM config_metadata WHERE key='other-writer'"
        ).fetchone() == ("retained",)
