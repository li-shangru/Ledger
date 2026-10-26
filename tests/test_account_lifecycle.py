"""``account_lifecycle`` is operator-authored state, so durability is the feature.

The table records a fact no import can derive: the operator saying "this account is
closed, stop telling me its statements are overdue". Everything that makes that
record worth writing is a survival property, and each one is pinned here:

* it survives an import finalize (staging clones the live DB, so this is nearly
  free -- but it is asserted, not assumed);
* it survives a ``repair statements`` promote, which builds an EMPTY candidate and
  publishes it wholesale. Only ``MERGE_FORWARD_TABLES`` membership buys this. A table
  merely *described* as merge-forward is silently emptied by the next repair -- the
  exact mechanism that destroyed the investment domain;
* an orphaned row -- ``account_id`` with no ``accounts`` row -- leaves the database
  VALID. ``save_accounts(replace_all=True)`` deletes accounts that stop appearing in
  transactions, and an operator-closed account is the likeliest to age out, so a
  ForeignKey here would make ``PRAGMA foreign_key_check`` read that row as corruption
  and the read-only server would refuse the whole database. The doctor censuses the
  orphan instead, at low severity, because it is the expected end state;
* editing it during the stage→finalize review window does NOT abort the run. It is
  excluded from ``import_owned_fingerprint`` by the same tuple membership.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_merge_forward_promote import (
    _apply_finalize_mocks,
    _make_finalize_paths,
    _setup_staged_run,
    count_transactions,
    insert_minimal_transaction,
)

from ledger.imports.pdf import workflow
from ledger.imports.pdf.readiness import MERGE_FORWARD_TABLES, import_owned_fingerprint
from ledger.storage.database import AccountLifecycleDB, init_database


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def insert_account_lifecycle(
    db_path: Path,
    account_id: str,
    *,
    status: str = "closed",
    closed_on: str | None = "2026-07-31",
    note: str = "",
) -> None:
    """Insert a lifecycle row directly via sqlite3 (what the CLI will write)."""
    now = _now()
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO account_lifecycle ("
            "  account_id, status, status_confirmed_on, closed_on, note, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (account_id, status, "2026-08-01", closed_on, note, now, now),
        )


def get_account_lifecycle(db_path: Path) -> list[dict]:
    with closing(sqlite3.connect(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("SELECT * FROM account_lifecycle")]


def insert_account(db_path: Path, account_id: str, account_number: str) -> None:
    """Insert the accounts row a lifecycle record would normally point at."""
    now = _now()
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO accounts ("
            "  account_id, institution, account_number, account_type, account_subtype,"
            "  first_transaction_date, last_transaction_date, is_active, transaction_count,"
            "  avg_monthly_transactions, is_credit_card, detected_credit_limit,"
            "  created_at, updated_at"
            ") VALUES (?, 'harbor', ?, 'checking', 'Chequing', '2026-01-01', '2026-07-31',"
            "          1, 10, 3.0, 0, NULL, ?, ?)",
            (account_id, account_number, now, now),
        )


# ---------------------------------------------------------------------------
# Durability
# ---------------------------------------------------------------------------


def test_lifecycle_row_written_during_review_window_survives_finalize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Marking an account closed after stage must survive the promote."""
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "run-1"
    target_db, staging_db, _csv_root = _setup_staged_run(tmp_path, runs_dir, run_dir)
    paths = _make_finalize_paths(tmp_path, runs_dir)
    _apply_finalize_mocks(monkeypatch)

    insert_minimal_transaction(staging_db, "txn-staged-001")
    insert_account_lifecycle(target_db, "acct-cedar-red", note="card cancelled")

    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state["status"] == "finalized", (
        f"a lifecycle edit aborted finalize: {state.get('status')!r}"
    )

    rows = get_account_lifecycle(target_db)
    assert [row["account_id"] for row in rows] == ["acct-cedar-red"], rows
    assert rows[0]["status"] == "closed"
    assert rows[0]["note"] == "card cancelled"
    assert count_transactions(target_db) >= 1, "import data must still land"


def test_lifecycle_row_survives_repair_promote(tmp_path: Path) -> None:
    """`repair statements` publishes an EMPTY candidate wholesale.

    Without MERGE_FORWARD_TABLES membership the operator's closure record is
    silently gone after a repair -- the failure mode this test exists to prevent.
    """
    from ledger.repair.statements import promote_candidate

    target = tmp_path / "ledger.db"
    init_database(target)
    insert_account_lifecycle(target, "acct-meadow", note="closed by operator")

    fp_before = import_owned_fingerprint(target)

    candidate = tmp_path / "candidate.db"
    init_database(candidate)
    insert_minimal_transaction(candidate, "txn-repair-001")

    promote_candidate(candidate, target, expected_import_owned=fp_before)

    assert count_transactions(target) == 1, "repair transaction must be promoted"
    rows = get_account_lifecycle(target)
    assert [row["account_id"] for row in rows] == ["acct-meadow"], (
        "the operator's closure record was destroyed by the repair promote"
    )
    assert rows[0]["note"] == "closed by operator"


def test_orphaned_lifecycle_row_survives_the_promote(tmp_path: Path) -> None:
    """A lifecycle row for an account that has aged out of `accounts` still promotes.

    What buys this is MERGE_FORWARD_TABLES membership, NOT the absence of a foreign
    key: `merge_forward` runs on a raw sqlite3 connection, and SQLite enforces no
    foreign key unless `PRAGMA foreign_keys = ON` is set on that connection. Adding
    an FK to the model leaves this test green, which is why the no-FK decision is
    guarded by `test_an_orphaned_lifecycle_row_leaves_the_database_valid` instead.
    """
    from ledger.repair.statements import promote_candidate

    target = tmp_path / "ledger.db"
    init_database(target)
    # No matching accounts row anywhere: not in the live DB, not in the candidate.
    insert_account_lifecycle(target, "acct-long-gone")

    fp_before = import_owned_fingerprint(target)

    candidate = tmp_path / "candidate.db"
    init_database(candidate)
    insert_account(candidate, "acct-still-here", "harbor-chequing")

    promote_candidate(candidate, target, expected_import_owned=fp_before)

    rows = get_account_lifecycle(target)
    assert [row["account_id"] for row in rows] == ["acct-long-gone"], (
        "an orphaned lifecycle row must survive, not block, the promote"
    )


def test_an_orphaned_lifecycle_row_leaves_the_database_valid(tmp_path: Path) -> None:
    """The no-FK decision, guarded by the check that would actually fire.

    `PRAGMA foreign_key_check` is where a ForeignKey here would do its damage, and
    it is checked without `foreign_keys = ON`: `validate_database` reports every hit
    as an error, `storage/read_only.py`'s preflight maps one to
    `CorruptDatabaseError` and refuses to serve the database at all, and both repair
    and the table-rebuilding migrations abort on it. So an operator who marks a
    closed account and later loses its last transaction would have their whole
    ledger declared corrupt -- on exactly the case this table exists to record.
    """
    from ledger.storage.migrations import validate_database

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    insert_account_lifecycle(db_path, "acct-long-gone")

    with closing(sqlite3.connect(db_path)) as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == [], (
            "account_lifecycle gained a ForeignKey; an aged-out account now reads as "
            "referential corruption"
        )

    result = validate_database(db_path, record=False)
    assert result.status == "success", result.errors


def test_lifecycle_edit_does_not_change_the_import_owned_fingerprint(tmp_path: Path) -> None:
    """The drift gate must ignore lifecycle writes, or the run aborts mid-review."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)

    before = import_owned_fingerprint(db_path)
    insert_account_lifecycle(db_path, "acct-cedar-red")
    after = import_owned_fingerprint(db_path)

    assert before["tables"] == after["tables"], (
        "account_lifecycle is inside the import-owned fingerprint, so marking an "
        "account closed during the review window would abort the staged run"
    )


def test_lifecycle_edit_during_finalize_window_does_not_abort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: the fingerprint is captured at stage, the row is written after."""
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "run-1"
    target_db, _staging_db, _csv_root = _setup_staged_run(tmp_path, runs_dir, run_dir)
    paths = _make_finalize_paths(tmp_path, runs_dir)
    _apply_finalize_mocks(monkeypatch)

    # Written strictly AFTER _setup_staged_run captured target_db_import_owned_before.
    insert_account_lifecycle(target_db, "acct-orbit", status="active", closed_on=None)

    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state["status"] == "finalized", (
        f"a mid-window lifecycle edit aborted the run: {state.get('status')!r}"
    )
    assert [row["account_id"] for row in get_account_lifecycle(target_db)] == ["acct-orbit"]


# ---------------------------------------------------------------------------
# Structure: the properties the durability above depends on
# ---------------------------------------------------------------------------


def test_account_lifecycle_is_a_merge_forward_table() -> None:
    """The tuple edit IS the deliverable; prose classification buys nothing."""
    assert "account_lifecycle" in MERGE_FORWARD_TABLES, MERGE_FORWARD_TABLES


def test_account_lifecycle_declares_no_foreign_key_to_accounts() -> None:
    foreign_keys = {
        str(fk.target_fullname)
        for column in AccountLifecycleDB.__table__.columns
        for fk in column.foreign_keys
    }
    assert not foreign_keys, (
        f"account_lifecycle must carry no ForeignKey (found {sorted(foreign_keys)}); "
        "an aged-out account would fail merge_forward's delete-then-insert"
    )


def test_status_is_constrained_to_active_or_closed(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    with pytest.raises(sqlite3.IntegrityError):
        insert_account_lifecycle(db_path, "acct-bad", status="dormant")


def test_doctor_censuses_an_orphaned_lifecycle_row_as_its_own_low_finding(
    tmp_path: Path,
) -> None:
    """No FK means orphans are possible, so they must be visible -- but not as drift.

    An orphaned lifecycle row is the state this table was DESIGNED to reach, not
    damage: `save_accounts(replace_all=True)` deletes an account once its last
    transaction ages out, and a closed account is the likeliest to. Riding the
    generic `account_orphans` finding reported it as a permanent HIGH whose remedy
    -- `ledger analysis refresh` -- can never clear it, because detection derives
    `accounts` FROM transactions and a closed account has none.
    """
    from ledger.maintenance.doctor import DatabaseDoctor

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    insert_account_lifecycle(db_path, "acct-long-gone")

    findings = {
        finding.check: finding
        for finding in DatabaseDoctor(db_path, audit_runs_dir=tmp_path / "none").diagnose()
    }
    assert "account_orphans" not in findings, (
        "a closed account with no transactions left is not accounts-dimension drift"
    )
    orphans = findings.get("account_lifecycle_orphans")
    assert orphans is not None, "an orphaned lifecycle row was censused by nothing"
    assert orphans.severity == "low", orphans.severity
    assert orphans.repairable is False
    assert "Run `ledger analysis refresh`" not in orphans.remedy, (
        "the remedy must not prescribe a command that can never clear this finding"
    )
    assert "cannot clear it" in orphans.remedy, orphans.remedy
    # The remedy must prescribe something that WORKS today. Before P2 that meant
    # naming no `ledger accounts` verb at all; now the verbs exist, so it names
    # them, and `test_the_lifecycle_remedy_prescribes_only_commands_the_cli_
    # actually_has` asserts each one dispatches.
    assert "delete the stale row" in orphans.remedy, orphans.remedy
    assert "ledger accounts reopen" in orphans.remedy, orphans.remedy
    assert any("account_lifecycle.account_id" in detail for detail in orphans.details), (
        orphans.details
    )
    # A raw account id is an account identifier, and doctor output reaches logs.
    assert not any("acct-long-gone" in detail for detail in orphans.details), orphans.details


def test_the_lifecycle_remedy_prescribes_only_commands_the_cli_actually_has(
    tmp_path: Path,
) -> None:
    """A remedy is operator-facing instructions, so every verb in it must run.

    Until P2 this test asserted the OPPOSITE -- that `ledger accounts` did not
    exist and the remedy therefore hedged around it. P2 landed the command, that
    assertion failed by design, and the hedge came out. What is pinned now is the
    invariant behind both versions: the remedy names `ledger accounts reopen`,
    and `ledger accounts reopen` is a command this CLI can dispatch.
    """
    from typer.testing import CliRunner

    from ledger.cli import app
    from ledger.maintenance.doctor import DatabaseDoctor

    registered = {group.name for group in app.registered_groups} | {
        command.name for command in app.registered_commands if command.name
    }
    assert "accounts" in registered, registered

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    insert_account_lifecycle(db_path, "acct-long-gone")
    finding = DatabaseDoctor(db_path, audit_runs_dir=tmp_path / "none")._check_lifecycle_orphans()

    assert finding is not None
    assert "ledger accounts reopen" in finding.remedy
    assert "arrives with P2" not in finding.remedy, "the P2 hedge outlived the hedge"
    # Derived from the remedy TEXT, not from a list someone kept in step with it:
    # every `ledger accounts <verb>` the operator is told to run must dispatch.
    prescribed = set(re.findall(r"ledger accounts ([a-z-]+)", finding.remedy))
    assert prescribed, finding.remedy
    runner = CliRunner()
    for verb in sorted(prescribed):
        assert runner.invoke(app, ["accounts", verb, "--help"]).exit_code == 0, verb


def test_a_closed_account_still_in_the_dimension_produces_no_finding(tmp_path: Path) -> None:
    """The census is about orphans, not about the table being populated."""
    from ledger.maintenance.doctor import DatabaseDoctor

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    insert_account(db_path, "acct-still-here", "harbor-chequing")
    insert_account_lifecycle(db_path, "acct-still-here")

    assert DatabaseDoctor(db_path)._check_lifecycle_orphans() is None


def test_the_orphan_finding_never_asserts_a_closure_the_table_does_not_hold(
    tmp_path: Path,
) -> None:
    """The doctor's own remedy produces this state.

    The remedy tells an operator whose account is in fact still open to run
    `ledger accounts reopen <slug>`. That sets `status = 'active'` and leaves the
    row orphaned -- the accounts dimension is derived from transactions, and this
    account still has none -- so the finding stands with a summary asserting a
    closure record that no longer exists anywhere in the table.
    """
    from ledger.maintenance.doctor import DatabaseDoctor
    from ledger.storage.database import init_database

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    insert_account_lifecycle(db_path, "acct-long-gone", status="active", closed_on=None)

    finding = DatabaseDoctor(db_path, audit_runs_dir=tmp_path / "none")._check_lifecycle_orphans()

    assert finding is not None
    assert "Closure records" not in finding.summary, finding.summary
    assert "closure" not in finding.summary.casefold(), finding.summary
    assert any("active" in detail for detail in finding.details), finding.details


def test_the_remedy_says_plainly_that_reopening_does_not_clear_the_finding(
    tmp_path: Path,
) -> None:
    """An operator who follows the instruction and re-runs the doctor must not
    be told the state they just fixed is still broken without an explanation."""
    from ledger.maintenance.doctor import DatabaseDoctor
    from ledger.storage.database import init_database

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    insert_account_lifecycle(db_path, "acct-long-gone")

    finding = DatabaseDoctor(db_path, audit_runs_dir=tmp_path / "none")._check_lifecycle_orphans()

    assert finding is not None
    assert "reopening does not clear this finding" in finding.remedy, finding.remedy


def test_the_orphan_details_break_the_count_down_by_recorded_status(
    tmp_path: Path,
) -> None:
    """One count over two populations reads as a count of the one the summary
    names. Details carry table names, counts and statuses only -- never an
    account id, because doctor output reaches logs and run artifacts."""
    from ledger.maintenance.doctor import DatabaseDoctor
    from ledger.storage.database import init_database

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    insert_account_lifecycle(db_path, "acct-gone-closed", status="closed")
    insert_account_lifecycle(db_path, "acct-gone-active", status="active", closed_on=None)

    finding = DatabaseDoctor(db_path, audit_runs_dir=tmp_path / "none")._check_lifecycle_orphans()

    assert finding is not None
    detail = " ".join(finding.details)
    assert "active 1" in detail, finding.details
    assert "closed 1" in detail, finding.details
    assert "acct-gone" not in detail, finding.details
