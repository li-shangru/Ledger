"""`ledger accounts` -- the operator's half of the coverage-gap loop.

`ledger.coverage` reports a statement as overdue from an account's own cadence.
The only thing that can honestly stop it is the operator saying "that account is
closed", and until this command existed `account_lifecycle` had no writer at all:
its own docstring said so, and the doctor's remedy hedged around a command that
did not exist.

The load-bearing assertion here is not that a row appears. It is that the row
appears under the SAME `account_id` the gaps block joins on, so the loop closes:
`ledger accounts mark-closed <slug>` must actually silence `<slug>`.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date
from pathlib import Path

import pytest
from click import unstyle
from mcp_synthetic import ACCOUNT_REF_SALT, build_synthetic_mcp_database
from test_coverage_gaps import (
    GAPS_INSTITUTION,
    OVERDUE_SLUG,
    SILENT_SLUG,
    SIX_MONTHLY,
    UNJOINABLE_SLUG,
    _insert_account,
    _insert_coverage,
)
from typer.testing import CliRunner

from ledger.cli import app
from ledger.core.account_identity import account_identity
from ledger.mcp.server import call_tool
from ledger.mcp.service import LedgerMCPService
from ledger.query.account_refs import banking_account_ref, unresolved_account_ref

TODAY = date(2025, 8, 15)

runner = CliRunner()


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Two accounts with coverage: one joinable, one that aged out of `accounts`."""
    path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    _insert_account(path, OVERDUE_SLUG, is_active=True, last_transaction="2025-06-28")
    _insert_coverage(path, OVERDUE_SLUG, SIX_MONTHLY)
    # A closed account whose last transaction aged out of the accounts dimension:
    # coverage rows remain, the accounts row is gone. This is the end state
    # `save_accounts(replace_all=True)` produces, and it is precisely the account
    # an operator most wants to silence.
    _insert_coverage(path, UNJOINABLE_SLUG, SIX_MONTHLY)
    return path


def _run(*arguments: str) -> object:
    return runner.invoke(app, ["accounts", *arguments])


def _lifecycle(path: Path) -> list[dict[str, object]]:
    with closing(sqlite3.connect(path)) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("SELECT * FROM account_lifecycle")]


def _gaps(path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, object]]:
    """The `ledger.coverage` gap verdicts, keyed by account ref."""
    monkeypatch.setattr("ledger.analysis.statement_cadence.gap_evaluation_date", lambda: TODAY)
    service = LedgerMCPService(path)
    try:
        result = call_tool(service, "ledger.coverage", {"limit": 100})
        assert result.get("isError") is not True, result
        data = result["structuredContent"]["data"]  # type: ignore[index]
        return {gap["account_ref"]: gap for gap in data["gaps"]}  # type: ignore[index,union-attr]
    finally:
        service.close()


# --- the loop closes ---------------------------------------------------------


def test_mark_closed_writes_the_row_that_actually_silences_that_account(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point. A lifecycle row under the wrong `account_id` is a row
    that changes nothing, and nothing else in the codebase would notice."""
    ref = banking_account_ref(ACCOUNT_REF_SALT, GAPS_INSTITUTION, OVERDUE_SLUG, "checking")
    assert _gaps(db_path, monkeypatch)[ref]["classification"] == "overdue"

    result = _run(
        "mark-closed",
        OVERDUE_SLUG,
        "--confirmed-on",
        "2025-08-01",
        "--note",
        "card cancelled by phone",
        "--db",
        str(db_path),
    )

    assert result.exit_code == 0, result.output
    after = _gaps(db_path, monkeypatch)[ref]
    assert after["classification"] == "dormant"
    assert after["deciding_signal"] == "operator_recorded_closure"


def test_mark_closed_stores_exactly_what_the_operator_stated(db_path: Path) -> None:
    result = _run(
        "mark-closed",
        OVERDUE_SLUG,
        "--confirmed-on",
        "2025-08-01",
        "--closed-on",
        "2025-06-30",
        "--note",
        "card cancelled by phone",
        "--db",
        str(db_path),
    )

    assert result.exit_code == 0, result.output
    rows = _lifecycle(db_path)
    assert len(rows) == 1, rows
    assert rows[0]["account_id"] == account_identity(GAPS_INSTITUTION, OVERDUE_SLUG)
    assert rows[0]["status"] == "closed"
    assert rows[0]["status_confirmed_on"] == "2025-08-01"
    assert rows[0]["closed_on"] == "2025-06-30"
    assert rows[0]["note"] == "card cancelled by phone"


def test_an_unstated_closing_date_stays_null_rather_than_borrowing_the_confirmation(
    db_path: Path,
) -> None:
    """`--confirmed-on` is when the operator CHECKED; `closed_on` is when the
    account closed, which is usually earlier and often unknown. Defaulting one
    to the other would record a date nobody stated as if they had."""
    assert (
        _run(
            "mark-closed", OVERDUE_SLUG, "--confirmed-on", "2025-08-01", "--db", str(db_path)
        ).exit_code
        == 0
    )

    rows = _lifecycle(db_path)
    assert rows[0]["closed_on"] is None
    assert rows[0]["status_confirmed_on"] == "2025-08-01"


def test_reopen_sets_the_status_active_and_clears_the_closing_date(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(
        "mark-closed",
        OVERDUE_SLUG,
        "--confirmed-on",
        "2025-08-01",
        "--closed-on",
        "2025-06-30",
        "--db",
        str(db_path),
    )

    result = _run("reopen", OVERDUE_SLUG, "--confirmed-on", "2025-08-10", "--db", str(db_path))

    assert result.exit_code == 0, result.output
    rows = _lifecycle(db_path)
    assert len(rows) == 1, "reopen must update the record, not add a second one"
    assert rows[0]["status"] == "active"
    assert rows[0]["closed_on"] is None
    assert rows[0]["status_confirmed_on"] == "2025-08-10"
    ref = banking_account_ref(ACCOUNT_REF_SALT, GAPS_INSTITUTION, OVERDUE_SLUG, "checking")
    assert _gaps(db_path, monkeypatch)[ref]["classification"] == "overdue", (
        "reopening must restore the alert the closure suppressed"
    )


def test_an_account_that_aged_out_of_the_dimension_can_still_be_marked_closed(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case the whole table exists for. If closure could only be recorded
    against a surviving `accounts` row, the account that most needs silencing --
    the closed one whose last transaction aged out -- could never be silenced."""
    ref = unresolved_account_ref(ACCOUNT_REF_SALT, UNJOINABLE_SLUG)
    assert _gaps(db_path, monkeypatch)[ref]["classification"] == "overdue"

    result = _run(
        "mark-closed", UNJOINABLE_SLUG, "--confirmed-on", "2025-08-01", "--db", str(db_path)
    )

    assert result.exit_code == 0, result.output
    assert _gaps(db_path, monkeypatch)[ref]["deciding_signal"] == "operator_recorded_closure"


# --- refusals ----------------------------------------------------------------


def test_an_unknown_slug_is_refused_with_a_message_naming_the_known_slugs(
    db_path: Path,
) -> None:
    """A silent no-op here writes a row keyed to nothing, which would leave the
    operator believing an account was silenced while the alert kept firing."""
    result = _run(
        "mark-closed", "not-an-account", "--confirmed-on", "2025-08-01", "--db", str(db_path)
    )

    assert result.exit_code == 1, result.output
    assert "not-an-account" in result.output
    assert OVERDUE_SLUG in result.output
    assert UNJOINABLE_SLUG in result.output, (
        "an account known only to statement_coverage is still a real account to close"
    )
    assert _lifecycle(db_path) == [], "a refusal must write nothing"


def test_reopen_refuses_an_unknown_slug_the_same_way(db_path: Path) -> None:
    result = _run("reopen", "not-an-account", "--db", str(db_path))

    assert result.exit_code == 1, result.output
    assert OVERDUE_SLUG in result.output
    assert _lifecycle(db_path) == []


def test_mark_closed_requires_confirmed_on(db_path: Path) -> None:
    """Recording a closure with no date of confirmation records an opinion. The
    option is required by the parser, so the refusal costs no database access."""
    result = _run("mark-closed", OVERDUE_SLUG, "--db", str(db_path))

    assert result.exit_code != 0
    assert "confirmed-on" in unstyle(result.output)
    assert _lifecycle(db_path) == []


def test_a_malformed_date_is_refused_rather_than_stored_as_text(db_path: Path) -> None:
    result = _run("mark-closed", OVERDUE_SLUG, "--confirmed-on", "01/08/2025", "--db", str(db_path))

    assert result.exit_code != 0
    assert _lifecycle(db_path) == []


# --- list --------------------------------------------------------------------


def test_list_shows_the_operator_record_beside_every_detected_account(
    db_path: Path,
) -> None:
    _run(
        "mark-closed",
        OVERDUE_SLUG,
        "--confirmed-on",
        "2025-08-01",
        "--note",
        "cancelled",
        "--db",
        str(db_path),
    )

    result = _run("list", "--db", str(db_path))

    assert result.exit_code == 0, result.output
    assert OVERDUE_SLUG in result.output
    assert UNJOINABLE_SLUG in result.output
    assert "closed" in result.output
    assert "2025-08-01" in result.output


def test_list_never_presents_the_is_active_flag_as_a_closure_signal(
    tmp_path: Path,
) -> None:
    """D8 reaches the CLI too. An operator shown "inactive" against an account
    that has simply been quiet for two months would mark it closed -- and the
    statements it is still issuing would stop being reported as missing."""
    path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    _insert_account(path, SILENT_SLUG, is_active=False, last_transaction="2025-01-20")
    _insert_coverage(path, SILENT_SLUG, SIX_MONTHLY)

    result = _run("list", "--db", str(path))

    assert result.exit_code == 0, result.output
    assert SILENT_SLUG in result.output
    assert "is_active" not in result.output
    assert "not recorded" in result.output, (
        "an account with no operator record must read as unrecorded, not as inactive"
    )


def test_list_on_a_ledger_with_no_lifecycle_records_says_so(db_path: Path) -> None:
    result = _run("list", "--db", str(db_path))

    assert result.exit_code == 0, result.output
    assert "not recorded" in result.output


# --- registration ------------------------------------------------------------


def test_the_command_group_is_registered_on_the_cli() -> None:
    assert "accounts" in {group.name for group in app.registered_groups}


def test_every_verb_the_doctor_remedy_prescribes_actually_exists(tmp_path: Path) -> None:
    """The hedge P0 left behind, inverted: the remedy now names a real command,
    and this asserts the command it names can be invoked."""
    from ledger.maintenance.doctor import DatabaseDoctor
    from ledger.storage.database import init_database

    path = tmp_path / "ledger.db"
    init_database(path)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "INSERT INTO account_lifecycle (account_id, status, status_confirmed_on,"
            " closed_on, note, created_at, updated_at) VALUES"
            " ('acct-long-gone', 'closed', '2025-08-01', NULL, '',"
            "  '2030-01-01 00:00:00', '2030-01-01 00:00:00')"
        )
    finding = DatabaseDoctor(path, audit_runs_dir=tmp_path / "none")._check_lifecycle_orphans()

    assert finding is not None
    assert "ledger accounts reopen" in finding.remedy
    assert runner.invoke(app, ["accounts", "reopen", "--help"]).exit_code == 0


def test_a_database_predating_the_table_is_refused_with_the_migration_to_run(
    db_path: Path,
) -> None:
    """`account_lifecycle` is new in this release, so every pre-upgrade database
    reaches this path. A bare OperationalError traceback tells the operator
    nothing they can act on; the migration command does."""
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("DROP TABLE account_lifecycle")

    for arguments in (
        ("mark-closed", OVERDUE_SLUG, "--confirmed-on", "2025-08-01"),
        ("reopen", OVERDUE_SLUG),
        ("list",),
    ):
        result = _run(*arguments, "--db", str(db_path))
        assert result.exit_code == 1, (arguments, result.output)
        assert "ledger db migrate" in result.output, (arguments, result.output)


def test_a_malformed_stored_period_does_not_take_the_listing_down(db_path: Path) -> None:
    """`statement_coverage.period` is unconstrained TEXT, and `list` is how an
    operator finds the slug the other two verbs need. One unparseable row must
    not cost them every other account's line."""
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO statement_coverage (account_slug, period, institution,"
            " statement_type, row_count, statement_count, skipped_reason, imported_at)"
            " VALUES (?, 'not-a-month', ?, 'synthetic_deposit', 0, 1, '',"
            "         '2030-01-01 00:00:00')",
            (OVERDUE_SLUG, GAPS_INSTITUTION),
        )

    result = _run("list", "--db", str(db_path))

    assert result.exit_code == 0, result.output
    assert OVERDUE_SLUG in result.output
    assert UNJOINABLE_SLUG in result.output


def test_a_slug_containing_console_markup_is_refused_rather_than_crashing(
    db_path: Path,
) -> None:
    """The refusal path's whole job is reporting bad input, so it must survive it.

    Rich parses `[...]` as markup: interpolating an unescaped argument turns a
    clean "unknown account slug" message into a MarkupError traceback, and the
    operator learns nothing about what they typed wrong.

    The probe is an unmatched CLOSING tag, chosen deliberately. `[bold]x[/]`
    would have passed against the unescaped code too -- Rich renders it as valid
    markup and the substring survives -- so a test built on that probe reads
    green while proving nothing.
    """
    probe = "not-an-account[/x]"

    result = _run("mark-closed", probe, "--confirmed-on", "2025-08-01", "--db", str(db_path))

    assert result.exit_code == 1, result.output
    assert result.exception is None or isinstance(result.exception, SystemExit), result.exception
    assert probe in result.output
    assert OVERDUE_SLUG in result.output


def test_an_operator_note_containing_console_markup_survives_the_listing(
    db_path: Path,
) -> None:
    """`--note` is free text the operator types and `list` prints it back."""
    assert (
        _run(
            "mark-closed",
            OVERDUE_SLUG,
            "--confirmed-on",
            "2025-08-01",
            "--note",
            "closed [see letter] 2025",
            "--db",
            str(db_path),
        ).exit_code
        == 0
    )

    result = _run("list", "--db", str(db_path))

    assert result.exit_code == 0, result.output
    assert "closed [see letter] 2025" in result.output


# --- the record has to survive the transition it exists for -------------------

LEGACY_SLUG = "synthetic-gaps-legacy-0009"
# An `accounts.account_id` that is NOT `account_identity(institution, slug)`.
# `tests/mcp_synthetic.py` mints exactly this shape, and repair/statements.py
# mints ids through a third derivation, so stored == derived is a coincidence
# rather than an invariant -- and every P2 fixture happened to satisfy it, which
# is what made the join defect invisible.
LEGACY_ACCOUNT_ID = "0" * 40


def _insert_legacy_account(path: Path) -> None:
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "INSERT INTO accounts (account_id, institution, account_number, account_type,"
            " account_subtype, first_transaction_date, last_transaction_date, is_active,"
            " transaction_count, avg_monthly_transactions, is_credit_card,"
            " detected_credit_limit, created_at, updated_at) VALUES"
            " (?, ?, ?, 'checking', '', '2024-08-01', '2025-06-28', 1, 6, 1.0, 0, NULL,"
            "  '2030-01-01 00:00:00', '2030-01-01 00:00:00')",
            (LEGACY_ACCOUNT_ID, GAPS_INSTITUTION, LEGACY_SLUG),
        )


def _block(output: str, slug: str) -> str:
    """The `list` lines for one account, whitespace-flattened.

    Rich wraps at the terminal width, so an assertion against a raw substring of
    one printed line fails on a phrase that happened to be split.
    """
    lines = output.splitlines()
    start = next(index for index, line in enumerate(lines) if slug in line)
    following = [line for line in lines[start + 1 :] if line.startswith((" ", "\t"))]
    return " ".join(" ".join(following).split())


def _age_out_of_the_dimension(path: Path, slug: str) -> None:
    """What `save_accounts(replace_all=True)` does once the last transaction goes."""
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("DELETE FROM accounts WHERE account_number = ?", (slug,))


def test_a_closure_still_silences_after_the_accounts_row_ages_out(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The end state a closed account reaches, on an account whose stored id is
    not the derived one.

    `mark-closed` keyed on the STORED `accounts.account_id` while the gaps block
    falls back to the derivation once that row is gone, so the closure stopped
    applying at exactly the moment it was needed -- and `list` reported "not
    recorded" for a record the same CLI had just written.
    """
    _insert_legacy_account(db_path)
    _insert_coverage(db_path, LEGACY_SLUG, SIX_MONTHLY)
    ref = banking_account_ref(ACCOUNT_REF_SALT, GAPS_INSTITUTION, LEGACY_SLUG, "checking")

    assert (
        _run("mark-closed", LEGACY_SLUG, "--confirmed-on", "2025-08-01", "--db", str(db_path))
    ).exit_code == 0
    assert _gaps(db_path, monkeypatch)[ref]["classification"] == "dormant"

    _age_out_of_the_dimension(db_path, LEGACY_SLUG)

    # The account's REF moves to the unresolved namespace once its `accounts`
    # row is gone -- that is the documented, correct behaviour of the ref
    # resolver. The lifecycle record must not move with it.
    aged = unresolved_account_ref(ACCOUNT_REF_SALT, LEGACY_SLUG)
    after = _gaps(db_path, monkeypatch)[aged]
    assert after["classification"] == "dormant", "the closure stopped applying when the row went"
    assert after["deciding_signal"] == "operator_recorded_closure"
    listing = _run("list", "--db", str(db_path))
    assert "operator record: closed" in _block(listing.output, LEGACY_SLUG), listing.output


def test_reopening_after_the_row_ages_out_leaves_exactly_one_record(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two rows for one account are two answers to a question with one answer --
    and `_write_lifecycle`'s docstring claims the primary key forbids it."""
    _insert_legacy_account(db_path)
    _insert_coverage(db_path, LEGACY_SLUG, SIX_MONTHLY)
    _run("mark-closed", LEGACY_SLUG, "--confirmed-on", "2025-08-01", "--db", str(db_path))
    _age_out_of_the_dimension(db_path, LEGACY_SLUG)

    result = _run("reopen", LEGACY_SLUG, "--confirmed-on", "2025-08-10", "--db", str(db_path))

    assert result.exit_code == 0, result.output
    rows = _lifecycle(db_path)
    assert len(rows) == 1, rows
    assert rows[0]["status"] == "active"
    aged = unresolved_account_ref(ACCOUNT_REF_SALT, LEGACY_SLUG)
    assert _gaps(db_path, monkeypatch)[aged]["classification"] == "overdue"


# --- the blank-institution account --------------------------------------------

GHOST_SLUG = "ghost-card-0001"


def _insert_blank_institution_coverage(path: Path) -> None:
    """`imports/pdf/post_processing.py` writes this whenever
    `institution_from_statement_type` raises -- a newly onboarded institution
    whose statement type the identity map does not know yet."""
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executemany(
            "INSERT INTO statement_coverage (account_slug, period, institution,"
            " statement_type, row_count, statement_count, skipped_reason, imported_at)"
            " VALUES (?, ?, '', 'parser_exception', 4, 1, '', '2030-01-01 00:00:00')",
            [(GHOST_SLUG, period) for period in SIX_MONTHLY],
        )


def test_an_account_with_no_institution_can_be_listed_and_actually_silenced(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`ledger.coverage` raises a live `overdue` alert for this account, so the
    operator half of the loop has to be able to answer it. It could not: the
    slug was invisible to all three verbs and `mark-closed` refused it as
    unknown while listing the accounts it did know."""
    _insert_blank_institution_coverage(db_path)
    ref = unresolved_account_ref(ACCOUNT_REF_SALT, GHOST_SLUG)
    assert _gaps(db_path, monkeypatch)[ref]["classification"] == "overdue"

    listing = _run("list", "--db", str(db_path))
    assert listing.exit_code == 0, listing.output
    assert GHOST_SLUG in listing.output

    result = _run("mark-closed", GHOST_SLUG, "--confirmed-on", "2025-08-01", "--db", str(db_path))

    assert result.exit_code == 0, result.output
    after = _gaps(db_path, monkeypatch)[ref]
    assert after["classification"] == "dormant"
    assert after["deciding_signal"] == "operator_recorded_closure"


def test_a_blank_account_slug_is_refused_with_the_reason_it_cannot_be_keyed(
    db_path: Path,
) -> None:
    """`statement_coverage.account_slug` is unconstrained TEXT, so a blank one is
    storable. It names no account, so there is nothing to key a closure to -- and
    the specific diagnostic for that case must be the one the operator sees."""
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO statement_coverage (account_slug, period, institution,"
            " statement_type, row_count, statement_count, skipped_reason, imported_at)"
            " VALUES ('', '2025-06', ?, 'synthetic_deposit', 4, 1, '',"
            "         '2030-01-01 00:00:00')",
            (GAPS_INSTITUTION,),
        )

    result = _run("mark-closed", "", "--confirmed-on", "2025-08-01", "--db", str(db_path))

    assert result.exit_code == 1, result.output
    # Whitespace-flattened: Rich wraps at the terminal width, so the sentence
    # this asserts is split across lines at an arbitrary point.
    assert "blank account slug" in " ".join(result.output.split()), result.output
    assert "cannot be keyed" in " ".join(result.output.split()), result.output
    assert _lifecycle(db_path) == []


# --- operator-authored text is never regenerable ------------------------------


def test_re_confirming_a_closure_preserves_the_note_and_the_closing_date(
    db_path: Path,
) -> None:
    """Re-confirming is the likeliest reason to run `mark-closed` a second time,
    and a blind full-row overwrite silently erased both fields -- operator-
    authored text no import can regenerate, which is the category
    `imports/pdf/readiness.py` calls silent, permanent data loss."""
    _run(
        "mark-closed",
        OVERDUE_SLUG,
        "--confirmed-on",
        "2025-08-01",
        "--closed-on",
        "2025-05-05",
        "--note",
        "closed by phone, ref 8842",
        "--db",
        str(db_path),
    )

    result = _run("mark-closed", OVERDUE_SLUG, "--confirmed-on", "2025-08-11", "--db", str(db_path))

    assert result.exit_code == 0, result.output
    rows = _lifecycle(db_path)
    assert len(rows) == 1, rows
    assert rows[0]["status_confirmed_on"] == "2025-08-11", "the stated date must still be recorded"
    assert rows[0]["closed_on"] == "2025-05-05"
    assert rows[0]["note"] == "closed by phone, ref 8842"


def test_an_explicitly_empty_note_still_clears_the_stored_one(db_path: Path) -> None:
    """Preserving on omission must not make the field unclearable."""
    _run(
        "mark-closed",
        OVERDUE_SLUG,
        "--confirmed-on",
        "2025-08-01",
        "--note",
        "typo",
        "--db",
        str(db_path),
    )

    _run(
        "mark-closed",
        OVERDUE_SLUG,
        "--confirmed-on",
        "2025-08-01",
        "--note",
        "",
        "--db",
        str(db_path),
    )

    assert _lifecycle(db_path)[0]["note"] == ""


def test_reopening_keeps_the_operators_note_while_clearing_the_closing_date(
    db_path: Path,
) -> None:
    """`closed_on` beside an active status is a contradiction, so it goes. The
    note is free text the operator typed and nothing else can restore it."""
    _run(
        "mark-closed",
        OVERDUE_SLUG,
        "--confirmed-on",
        "2025-08-01",
        "--closed-on",
        "2025-05-05",
        "--note",
        "closed by phone, ref 8842",
        "--db",
        str(db_path),
    )

    _run("reopen", OVERDUE_SLUG, "--confirmed-on", "2025-08-10", "--db", str(db_path))

    rows = _lifecycle(db_path)
    assert rows[0]["closed_on"] is None
    assert rows[0]["note"] == "closed by phone, ref 8842"


# --- the listing IS the decision evidence -------------------------------------


def test_list_prints_the_cadence_and_gap_verdict_it_calls_decision_evidence(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The line the module docstring calls the evidence that actually bears on
    the decision, asserted rather than assumed.

    The clock is patched at the SHARED seam. `list` read `date.today()` directly,
    so the patch this suite already applied was inert on the CLI path and the
    printed verdict was whatever the calendar said on the day the suite ran.
    """
    monkeypatch.setattr("ledger.analysis.statement_cadence.gap_evaluation_date", lambda: TODAY)

    result = _run("list", "--db", str(db_path))

    assert result.exit_code == 0, result.output
    block = _block(result.output, OVERDUE_SLUG)
    assert "covered through 2025-06" in block, result.output
    assert "cadence 1 month(s)" in block, result.output
    assert "gap overdue" in block, result.output


def test_list_counts_covered_months_and_statements_as_the_two_things_they_are(
    db_path: Path,
) -> None:
    """`statement_coverage`'s primary key is (account_slug, period) and it carries
    its own `statement_count`, incremented once per manifest entry. Two statements
    landing in one month are ONE row with `statement_count = 2`, so a line that
    calls the row count a statement census under-reports exactly where an
    account's cycle is irregular -- the population this feature serves."""
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            "UPDATE statement_coverage SET statement_count = 2"
            " WHERE account_slug = ? AND period = '2025-06'",
            (OVERDUE_SLUG,),
        )

    result = _run("list", "--db", str(db_path))

    assert result.exit_code == 0, result.output
    block = _block(result.output, OVERDUE_SLUG)
    assert "6 covered months, 7 statements" in block, result.output
