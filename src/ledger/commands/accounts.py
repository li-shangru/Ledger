"""CLI commands for the operator-authored account lifecycle record.

`account_lifecycle` records the one fact no import can derive: the operator
saying "this account is closed, stop telling me its statements are overdue".
`ledger.coverage`'s gap classifier reads it and nothing else for closure --
never `accounts.is_active`, which is a stored
`(today - last_transaction_date).days <= 60` snapshot that decays from the very
gap being measured (D8).

The verbs are keyed by the account SLUG used in local statement records,
not by the `account_id` the table stores, because the id is an internal hash.
The translation goes through
`core.account_identity.lifecycle_account_ids`, which is the same function the
gaps block joins with -- so a row written here is a row that block will find, and
goes on finding after the `accounts` row this account once had is gone.
"""

from __future__ import annotations

from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Annotated

import typer
from rich.markup import escape

from ledger.commands.common import console, resolve_db_path

# Every value below that came from the database or from the command line is
# escaped before it reaches `console.print`. Rich parses `[...]` as markup, so an
# account slug or an operator note containing a bracket either mangles the line
# or raises MarkupError -- turning a clean refusal into a traceback, on the exact
# path whose job is to report bad input.

accounts_app = typer.Typer(help="Record operator-known account open/closed state.")


def _parsed_date(value: str, option: str) -> date:
    """A calendar date, or a clean parameter error.

    `typer.BadParameter` and not a printed message: a malformed date must never
    reach the database as text, and the parser's own exit code says the input was
    wrong rather than the operation.
    """
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise typer.BadParameter(f"{option} must be a calendar date (YYYY-MM-DD)") from error


def _require_lifecycle_table(db_path: Path) -> None:
    """Refuse cleanly on a database that predates the table.

    ``account_lifecycle`` ships in this release, so every database from before it
    reaches this path, and a bare ``OperationalError`` traceback names nothing
    the operator can act on. Deliberately NOT ``ensure_schema``: that applies
    every pending migration without taking a backup, which is a much larger
    action than a lifecycle edit asked for.
    """
    import sqlite3

    with closing(sqlite3.connect(db_path)) as connection:
        present = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'account_lifecycle'"
        ).fetchone()
    if present is None:
        console.print(
            "[red]Error:[/red] This database has no account_lifecycle table. "
            "Run `ledger db migrate` to upgrade it, then retry."
        )
        raise typer.Exit(1)


def _known_accounts(db_path: Path) -> dict[str, str]:
    """Every account slug the operator could mean, mapped to its institution.

    The union of two tables, deliberately. `accounts` is derived FROM
    transactions and `save_accounts(replace_all=True)` deletes a row once its
    last transaction ages out -- so an account that has been closed long enough
    to matter is exactly the one missing from it. Its `statement_coverage` rows
    survive, and closing that account is the whole point of the command.

    A BLANK institution is still an account. `imports/pdf/post_processing.py`
    writes `institution = ''` whenever `institution_from_statement_type` raises
    -- a newly onboarded institution whose statement type the identity map does
    not know yet -- and `ledger.coverage` scores those rows and raises real gap
    alerts for them. Admitting the slug only `if institution:` made the set this
    command can close NARROWER than the set the agent surface complains about, so
    the operator was told an account did not exist by the same system that had
    just reported it overdue. The institution is carried as `''` and
    `lifecycle_account_ids` keys the record anyway.
    """
    from ledger.storage.database import AccountDB, StatementCoverageDB, session_scope

    spellings: dict[str, set[str]] = {}
    with session_scope(db_path) as session:
        for slug, institution in session.query(
            StatementCoverageDB.account_slug, StatementCoverageDB.institution
        ).all():
            spellings.setdefault(str(slug), set()).add(str(institution or ""))
        stored = {
            str(slug): str(institution or "")
            for slug, institution in session.query(
                AccountDB.account_number, AccountDB.institution
            ).all()
        }
    # The stored spelling wins; a coverage-only slug takes the smallest NON-BLANK
    # spelling its own rows carried, falling back to blank only when every row is
    # blank. Chosen by sort and not by whichever row arrived first, because this
    # value feeds an account-id derivation and `query/agent.py`'s gap block
    # resolves the SAME slug the SAME way -- a row written under one spelling and
    # looked up under another is a row that silences nothing.
    known = {slug: min(values - {""}, default="") for slug, values in spellings.items()}
    known.update(stored)
    return known


def _resolve_or_refuse(db_path: Path, slug: str) -> tuple[str, ...]:
    """Every `account_id` a slug's lifecycle row could be keyed by, canonical first.

    An unrecognized slug is refused rather than accepted: writing a row under an
    id nothing references is silent, and the operator would be left believing an
    account had been silenced while its alert kept firing.

    A TUPLE and not one id, because the id a slug resolves to is not stable over
    the account's life -- see `lifecycle_account_ids`. The writer moves the record
    onto element 0 and clears the rest, so the account ends with exactly one row
    keyed on the derivation that survives its `accounts` row ageing out.
    """
    from ledger.core.account_identity import lifecycle_account_ids
    from ledger.storage.database import AccountDB, session_scope

    known = _known_accounts(db_path)
    if slug not in known:
        console.print(f"[red]Error:[/red] Unknown account slug: {escape(slug)}")
        if known:
            console.print("  Known account slugs:")
            for candidate in sorted(known):
                console.print(f"    • {escape(candidate)}")
        else:
            console.print("  This database has no imported accounts or statement coverage.")
        raise typer.Exit(1)

    with session_scope(db_path) as session:
        stored_ids = {
            str(number): str(account_id)
            for number, account_id in session.query(
                AccountDB.account_number, AccountDB.account_id
            ).all()
        }
    account_ids = lifecycle_account_ids(stored_ids, slug, known[slug])
    if not account_ids:
        # Reachable, and only here: `account_slug` is unconstrained TEXT, so a
        # blank one is storable and lands in `known` like any other. It names no
        # account, and hashing it would key every blank row in the table together.
        console.print(
            f"[red]Error:[/red] {escape(slug) or '(blank)'} is a blank account slug, so it "
            "names no account; its lifecycle cannot be keyed."
        )
        raise typer.Exit(1)
    return account_ids


def _write_lifecycle(
    db_path: Path,
    account_ids: tuple[str, ...],
    *,
    status: str,
    confirmed_on: date | None,
    closed_on: date | None,
    clear_closed_on: bool,
    note: str | None,
) -> None:
    """Upsert the one row for this account, onto the canonical id.

    ONE row per account: a second row would be a second answer to a question with
    one answer. The primary key does not forbid it -- the key is an `account_id`,
    and one slug has more than one candidate id over its life -- so this function
    is what enforces it, by reading every candidate, keeping the first record it
    finds, deleting the rest, and writing under `account_ids[0]`.

    OMITTED FIELDS ARE PRESERVED, never blanked. `--note` and `--closed-on` are
    operator-authored text that no import can regenerate, and the likeliest
    reason to run `mark-closed` twice is to re-confirm a closure already
    recorded. A blind full-row overwrite silently erased both on that run --
    exactly the silent, permanent loss `imports/pdf/readiness.py` protects the
    table from during a repair. `None` means "not supplied"; an explicit
    `--note ''` still clears, and `clear_closed_on` is how `reopen` states that
    the contradiction of a closing date on an active account must go.
    """
    from ledger.storage.database import AccountLifecycleDB, session_scope
    from ledger.utils.dates import naive_utc_now

    canonical = account_ids[0]
    now = naive_utc_now()
    with session_scope(db_path) as session:
        existing = [
            found
            for found in (session.get(AccountLifecycleDB, key) for key in account_ids)
            if found is not None
        ]
        kept = existing[0] if existing else None
        preserved_note = "" if kept is None else str(kept.note or "")
        preserved_closed_on = None if kept is None else kept.closed_on
        preserved_created_at = now if kept is None else kept.created_at
        for row in existing:
            session.delete(row)
        # Deleted and re-added rather than mutated in place, because the row may
        # have to move to a different primary key.
        session.flush()
        session.add(
            AccountLifecycleDB(
                account_id=canonical,
                status=status,
                status_confirmed_on=confirmed_on,
                closed_on=(
                    None
                    if clear_closed_on
                    else (preserved_closed_on if closed_on is None else closed_on)
                ),
                note=preserved_note if note is None else note,
                created_at=preserved_created_at,
                updated_at=now,
            )
        )


@accounts_app.command("mark-closed")
def mark_closed(
    slug: Annotated[str, typer.Argument(help="Account slug, as `ledger accounts list` shows it")],
    confirmed_on: Annotated[
        str,
        typer.Option(
            "--confirmed-on",
            help="Date you confirmed the account is closed (YYYY-MM-DD). Required.",
        ),
    ],
    closed_on: Annotated[
        str | None,
        typer.Option(
            "--closed-on",
            help=(
                "Date the account actually closed (YYYY-MM-DD), if known. Usually earlier "
                "than --confirmed-on; left unrecorded when omitted."
            ),
        ),
    ] = None,
    note: Annotated[
        str | None, typer.Option("--note", help="Free-text note for your own records")
    ] = None,
    db_path: Annotated[Path | None, typer.Option("--db", help="SQLite database path")] = None,
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="CLI defaults config file (overrides default locations)"),
    ] = None,
) -> None:
    """Record that an account is closed, so its statements stop reading overdue.

    `--confirmed-on` is required and is when YOU checked. It is stored separately
    from `--closed-on`, which is when the account actually closed: defaulting one
    to the other would record a date nobody stated as though they had.

    Re-running this on an already-closed account updates the confirmation date
    and PRESERVES anything you recorded before and did not restate.
    """
    resolved = resolve_db_path(db_path, config_file)
    _require_lifecycle_table(resolved)
    confirmed = _parsed_date(confirmed_on, "--confirmed-on")
    closed = None if closed_on is None else _parsed_date(closed_on, "--closed-on")
    account_ids = _resolve_or_refuse(resolved, slug)
    _write_lifecycle(
        resolved,
        account_ids,
        status="closed",
        confirmed_on=confirmed,
        closed_on=closed,
        clear_closed_on=False,
        note=note,
    )
    console.print(
        f"[green]Marked closed:[/green] {escape(slug)} (confirmed {confirmed.isoformat()})"
    )
    console.print("  • ledger.coverage will report this account dormant rather than overdue.")


@accounts_app.command("reopen")
def reopen(
    slug: Annotated[str, typer.Argument(help="Account slug, as `ledger accounts list` shows it")],
    confirmed_on: Annotated[
        str | None,
        typer.Option(
            "--confirmed-on",
            help="Date you confirmed the account is open (YYYY-MM-DD); defaults to today",
        ),
    ] = None,
    note: Annotated[
        str | None, typer.Option("--note", help="Free-text note for your own records")
    ] = None,
    db_path: Annotated[Path | None, typer.Option("--db", help="SQLite database path")] = None,
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="CLI defaults config file (overrides default locations)"),
    ] = None,
) -> None:
    """Record that an account is open, restoring the gap alerts a closure silenced.

    `--confirmed-on` is optional here and defaults to today, because reopening
    asserts a present-tense fact. `closed_on` is cleared: a closing date beside
    an active status is a contradiction, not history. The note is NOT cleared --
    it is text you typed and nothing else can restore it -- so pass `--note ''`
    if the old one no longer applies.
    """
    from ledger.analysis.statement_cadence import gap_evaluation_date

    resolved = resolve_db_path(db_path, config_file)
    _require_lifecycle_table(resolved)
    confirmed = (
        gap_evaluation_date()
        if confirmed_on is None
        else _parsed_date(confirmed_on, "--confirmed-on")
    )
    account_ids = _resolve_or_refuse(resolved, slug)
    _write_lifecycle(
        resolved,
        account_ids,
        status="active",
        confirmed_on=confirmed,
        closed_on=None,
        clear_closed_on=True,
        note=note,
    )
    console.print(f"[green]Reopened:[/green] {escape(slug)} (confirmed {confirmed.isoformat()})")


@accounts_app.command("list")
def list_accounts(
    db_path: Annotated[Path | None, typer.Option("--db", help="SQLite database path")] = None,
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="CLI defaults config file (overrides default locations)"),
    ] = None,
) -> None:
    """List every account with statements, beside the operator's own record.

    The `accounts.is_active` flag is deliberately NOT shown. It is derived from
    "did a transaction post in the last 60 days", and an operator shown
    "inactive" against an account that has merely been quiet would mark it closed
    -- silencing the statements it is in fact still issuing. What is shown is the
    account's observed cadence and its trailing gap, which is the evidence that
    actually bears on the decision.
    """
    from ledger.analysis.statement_cadence import (
        classify_gap,
        estimate_cadence,
        gap_evaluation_date,
        is_scoreable_period,
    )
    from ledger.core.account_identity import lifecycle_account_ids
    from ledger.storage.database import (
        AccountDB,
        AccountLifecycleDB,
        StatementCoverageDB,
        session_scope,
    )

    resolved = resolve_db_path(db_path, config_file)
    _require_lifecycle_table(resolved)
    known = _known_accounts(resolved)
    with session_scope(resolved) as session:
        periods: dict[str, list[str]] = {}
        # `statement_coverage`'s primary key is (account_slug, period) and it
        # carries its own `statement_count`, incremented once per manifest entry
        # -- so two statements landing in one month are ONE row with a count of
        # two. The month census and the statement census are different numbers
        # and are printed as both, because they diverge exactly where an
        # account's cycle is irregular, which is the population this command
        # serves.
        statements: dict[str, int] = {}
        for slug, period, statement_count in session.query(
            StatementCoverageDB.account_slug,
            StatementCoverageDB.period,
            StatementCoverageDB.statement_count,
        ).all():
            statements[str(slug)] = statements.get(str(slug), 0) + int(statement_count or 0)
            # Same predicate `ledger.coverage` uses. `period` is unconstrained
            # TEXT, and letting one malformed row raise here would take down the
            # listing for every healthy account -- including the listing an
            # operator needs to find the slug to pass to the other two verbs.
            if is_scoreable_period(str(period)):
                periods.setdefault(str(slug), []).append(str(period))
        stored_ids = {
            str(number): str(account_id)
            for number, account_id in session.query(
                AccountDB.account_number, AccountDB.account_id
            ).all()
        }
        # Materialized inside the session. Holding the ORM instances past the
        # scope raises DetachedInstanceError on the first attribute read.
        lifecycle = {
            str(account_id): (str(status), confirmed, closed, str(note))
            for account_id, status, confirmed, closed, note in session.query(
                AccountLifecycleDB.account_id,
                AccountLifecycleDB.status,
                AccountLifecycleDB.status_confirmed_on,
                AccountLifecycleDB.closed_on,
                AccountLifecycleDB.note,
            ).all()
        }

    # THE shared seam, not `date.today()`. `ledger.coverage` classifies against
    # `statement_cadence.gap_evaluation_date`, and a second clock here would let
    # the two surfaces disagree about the same account on the same day -- and
    # made every test that patched the query layer's clock inert on this path.
    today = gap_evaluation_date()
    if not known:
        console.print("No accounts with transactions or statement coverage in this database.")
        return

    # A block per account keeps long slugs visible at ordinary terminal widths.
    # A table with the slug and its metadata columns could truncate the exact
    # string the other lifecycle commands need as their argument.
    for slug in sorted(known):
        # Resolved from the ids read ONCE above. Re-opening a session per account
        # made the listing O(accounts) full scans of the accounts table.
        record = next(
            (
                found
                for found in (
                    lifecycle.get(account_id)
                    for account_id in lifecycle_account_ids(stored_ids, slug, known[slug])
                )
                if found is not None
            ),
            None,
        )
        covered = sorted(periods.get(slug, []))
        console.print(
            f"[cyan]{escape(slug)}[/cyan]  ({escape(known[slug]) or 'institution unknown'})"
        )
        if covered:
            cadence = estimate_cadence(covered)
            assessment = classify_gap(
                last_covered=covered[-1],
                cadence=cadence,
                today=today,
                lifecycle_status=None if record is None else record[0],
            )
            # Two short bullets rather than one long one: a real slug is 38
            # characters, and Rich wraps at the terminal width, so a single line
            # carrying all five facts is broken mid-phrase on an ordinary
            # terminal -- including mid-`cadence 1 month(s)`.
            console.print(
                f"  • covered through {covered[-1]}"
                f"; {len(covered)} covered months, {statements.get(slug, 0)} statements"
            )
            console.print(
                "  • cadence "
                + ("unknown" if cadence.months is None else f"{cadence.months} month(s)")
                + f"; gap {assessment.classification or 'none'}"
            )
        else:
            console.print("  • no imported statements")
        if record is None:
            console.print("  • operator record: not recorded")
        else:
            status, confirmed_on, closed_on, note = record
            confirmed = "unstated" if confirmed_on is None else confirmed_on.isoformat()
            closed = "unstated" if closed_on is None else closed_on.isoformat()
            console.print(
                f"  • operator record: {status} (confirmed {confirmed}, closed on {closed})"
            )
            if note:
                console.print(f"  • note: {escape(note)}")

    console.print(
        "The stored `active` flag is deliberately not shown: it only reports whether a "
        "transaction posted in the last 60 days, which a quiet but open account also fails."
    )


__all__ = ["accounts_app"]
