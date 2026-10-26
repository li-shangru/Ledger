"""P9 proposal-store isolation and survival contracts.

The queue is deliberately outside ``ledger.db``.  These tests exercise the
observable consequences of that decision rather than only checking a filename:
filing leaves every import-owned/config table unchanged, an open import still
finalizes, repair and cleanup leave pending operator intent alone, and the store
is absent from the ledger ORM/migration graph.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_merge_forward_promote import (
    _apply_finalize_mocks,
    _make_finalize_paths,
    _setup_staged_run,
    insert_minimal_transaction,
)
from test_review_teach import _insert_txn, _seed_config
from test_statement_coverage import _patch_repair_dependencies, _repair_options

from ledger.imports.pdf import workflow
from ledger.imports.pdf.readiness import _selected_table_fingerprint
from ledger.maintenance import DataCleanupService
from ledger.proposals import ProposalService
from ledger.proposals import database as proposal_database
from ledger.proposals.database import ProposalBase
from ledger.repair.statements import StatementRepairService
from ledger.storage import snapshots
from ledger.storage.database import Base as LedgerBase
from ledger.storage.database import init_database
from ledger.storage.migrations import REQUIRED_TABLES

_LEDGER_TABLES = {
    "transactions",
    "merchant_rules",
    "merchant_rule_patterns",
    "category_config",
    "category_merchants",
}
_EVIDENCE_ID = "a" * 40


def _file_pending(proposals_db: Path):
    return ProposalService(proposals_db).file_merchant_rule(
        merchant_norm="SYNTHETIC CAFE",
        pattern="SYNTHETIC CAFE",
        category="Dining",
        evidence_id=_EVIDENCE_ID,
    )


def test_filing_is_full_table_non_mutating_and_uses_an_isolated_base(
    tmp_path: Path,
) -> None:
    ledger_db = tmp_path / "data" / "ledger.db"
    ledger_db.parent.mkdir(mode=0o700)
    init_database(ledger_db)
    _seed_config(ledger_db)
    _insert_txn(ledger_db, "txn-isolation")
    before = _selected_table_fingerprint(ledger_db, _LEDGER_TABLES)

    service = ProposalService(ledger_db.parent / "proposals.db")
    filed = _file_pending(ledger_db.parent / "proposals.db")
    [listed] = service.list_proposals(status="pending")
    shown = service.show_proposal(filed.proposal_id)
    rejected = service.reject_proposal(filed.proposal_id)

    assert (filed.status, listed.status, shown.status, rejected.status) == (
        "pending",
        "pending",
        "pending",
        "rejected",
    )
    assert _selected_table_fingerprint(ledger_db, _LEDGER_TABLES) == before
    assert "proposals" in ProposalBase.metadata.tables
    assert "proposals" not in LedgerBase.metadata.tables
    assert "proposals" not in REQUIRED_TABLES


def test_proposal_store_is_owner_only_at_creation_and_read_back(tmp_path: Path) -> None:
    proposals_db = tmp_path / "private" / "proposals.db"
    previous_umask = os.umask(0)
    try:
        filed = _file_pending(proposals_db)
    finally:
        os.umask(previous_umask)

    [read_back] = ProposalService(proposals_db).list_proposals(status="pending")

    assert read_back.proposal_id == filed.proposal_id
    assert stat.S_IMODE(proposals_db.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(proposals_db.stat().st_mode) == 0o600


def test_initialization_never_reopens_a_replaced_proposal_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "private"
    data_dir.mkdir(mode=0o700)
    proposals_db = data_dir / "proposals.db"
    unrelated_db = data_dir / "ledger.db"
    with contextlib.closing(sqlite3.connect(unrelated_db)) as connection, connection:
        connection.execute("CREATE TABLE financial_marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO financial_marker VALUES ('unchanged')")
    unrelated_db.chmod(0o600)
    before = unrelated_db.read_bytes()
    original_schema = proposal_database._new_schema_bytes

    def replace_before_path_reopen() -> bytes:
        payload = original_schema()
        proposals_db.symlink_to(unrelated_db.name)
        return payload

    monkeypatch.setattr(proposal_database, "_new_schema_bytes", replace_before_path_reopen)

    with pytest.raises(OSError, match=r"exists|changed|symlink|Too many"):
        proposal_database.init_proposal_database(proposals_db)

    assert unrelated_db.read_bytes() == before
    with contextlib.closing(sqlite3.connect(unrelated_db)) as connection:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert tables == {"financial_marker"}


def test_transaction_never_reopens_a_replaced_proposal_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "private"
    proposals_db = data_dir / "proposals.db"
    filed = _file_pending(proposals_db)
    displaced = data_dir / "displaced-proposals.db"
    unrelated_db = data_dir / "ledger.db"
    shutil.copy2(proposals_db, unrelated_db)
    with contextlib.closing(sqlite3.connect(unrelated_db)) as connection, connection:
        connection.execute("CREATE TABLE financial_marker (value TEXT NOT NULL)")
        connection.execute("INSERT INTO financial_marker VALUES ('unchanged')")
    unrelated_db.chmod(0o600)
    before = unrelated_db.read_bytes()
    original_connect = sqlite3.connect

    def replace_before_path_reopen(
        database: str,
        *args: Any,
        **kwargs: Any,
    ) -> sqlite3.Connection:
        if database == f"{proposals_db.as_uri()}?mode=rw":
            os.replace(proposals_db, displaced)
            proposals_db.symlink_to(unrelated_db.name)
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(snapshots.sqlite3, "connect", replace_before_path_reopen)

    with pytest.raises(OSError, match=r"changed|symlink|Too many|unbound"):
        ProposalService(proposals_db).reject_proposal(filed.proposal_id)

    assert unrelated_db.read_bytes() == before
    assert ProposalService(displaced).show_proposal(filed.proposal_id).status == "pending"


def test_real_processes_serialize_first_use_and_commit_every_proposal(tmp_path: Path) -> None:
    proposals_db = tmp_path / "private" / "proposals.db"
    coordination = tmp_path / "coordination"
    coordination.mkdir()
    gate = coordination / "go"
    worker = """
import sys
import time
from pathlib import Path
from ledger.proposals import ProposalService

database = Path(sys.argv[1])
coordination = Path(sys.argv[2])
index = int(sys.argv[3])
(coordination / f"ready-{index}").write_text("ready", encoding="utf-8")
while not (coordination / "go").exists():
    time.sleep(0.01)
ProposalService(database).file_merchant_rule(
    merchant_norm=f"SYNTHETIC CAFE {index:02d}",
    pattern=f"SYNTHETIC CAFE {index:02d}",
    category="Dining",
    evidence_id="d" * 40,
)
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", worker, str(proposals_db), str(coordination), str(index)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for index in range(6)
    ]
    try:
        deadline = time.monotonic() + 15
        while len(list(coordination.glob("ready-*"))) != len(processes):
            if time.monotonic() >= deadline:
                pytest.fail("proposal first-use worker startup timed out")
            time.sleep(0.01)
        gate.write_text("go", encoding="utf-8")

        results = [process.communicate(timeout=30) for process in processes]
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if process.stdout is not None:
                process.stdout.close()
            if process.stderr is not None:
                process.stderr.close()

    assert [
        (process.returncode, stdout, stderr)
        for process, (stdout, stderr) in zip(processes, results, strict=True)
    ] == [(0, "", "")] * len(processes)
    assert len(ProposalService(proposals_db).list_proposals(status="pending")) == len(processes)
    assert not (proposals_db.parent / "ledger.db").exists()


def test_filing_during_an_open_import_does_not_abort_finalize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "run-1"
    target_db, staging_db, _csv_root = _setup_staged_run(tmp_path, runs_dir, run_dir)
    paths = _make_finalize_paths(tmp_path, runs_dir)
    _apply_finalize_mocks(monkeypatch)
    insert_minimal_transaction(staging_db, "txn-staged-proposal")

    filed = _file_pending(tmp_path / "proposals.db")
    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state["status"] == "finalized", state
    assert target_db.exists()
    assert (
        ProposalService(tmp_path / "proposals.db").show_proposal(filed.proposal_id).status
        == "pending"
    )


def test_pending_proposal_survives_a_statement_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    (pdf_dir / "dummy.pdf").write_bytes(b"%PDF-1.4 synthetic")
    init_database(tmp_path / "ledger.db")
    filed = _file_pending(tmp_path / "proposals.db")
    _patch_repair_dependencies(monkeypatch)

    result = StatementRepairService().run(_repair_options(tmp_path, pdf_dir, dry_run=False))

    assert result.status == "success", result.summary
    assert (
        ProposalService(tmp_path / "proposals.db").show_proposal(filed.proposal_id).status
        == "pending"
    )


def test_data_clean_execute_leaves_every_pending_proposal_intact(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o700)
    ledger_db = data_dir / "ledger.db"
    init_database(ledger_db)
    service = ProposalService(data_dir / "proposals.db")
    first = service.file_merchant_rule(
        merchant_norm="SYNTHETIC CAFE",
        pattern="SYNTHETIC CAFE",
        category="Dining",
        evidence_id="b" * 40,
    )
    second = service.file_merchant_rule(
        merchant_norm="SYNTHETIC BOOKS",
        pattern="SYNTHETIC BOOKS",
        category="Shopping",
        evidence_id="c" * 40,
    )

    result = DataCleanupService(ledger_db, data_dir=data_dir).clean(execute=True)

    assert result.ok, result.errors
    assert [
        proposal.proposal_id
        for proposal in ProposalService(data_dir / "proposals.db").list_proposals(status="pending")
    ] == [first.proposal_id, second.proposal_id]
