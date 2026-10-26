"""The `gaps` block on `ledger.coverage`.

`ledger.coverage` could already say "this account-month was never imported". It
could not say which absences MATTER: on a database where six accounts are
strictly monthly and two are irregular, every consumer had to invent its own
rule, and the obvious ones are wrong. This block answers per account, from that
account's own observed cadence, and names the fact that decided each answer.

The load-bearing negative is D8: `accounts.is_active` is never read. It is a
stored `(today - last_transaction_date).days <= 60` snapshot that decays from
the very gap being measured, and the fixture below contains a matched pair of
accounts that differ in NOTHING ELSE -- if the classifier ever consults it, the
pair stops matching and this module fails.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import date
from pathlib import Path

import pytest
from mcp_synthetic import (
    ACCOUNT_REF_SALT,
    BANK_INSTITUTION,
    CARD_INSTITUTION,
    PRIVATE_ACCOUNT_NUMBER,
    PRIVATE_CARD_NUMBER,
    build_synthetic_mcp_database,
)

from ledger.core.account_identity import account_identity
from ledger.mcp.contracts import COVERAGE_GAP_STATEMENT, tool_schemas
from ledger.mcp.server import call_tool
from ledger.mcp.service import LedgerMCPService
from ledger.query.account_refs import banking_account_ref, unresolved_account_ref

# The reference date every assertion in this module is written against. Never
# `date.today()`: a gap classification is a function of the clock, and a test
# that reads the real one starts failing on a date nobody chose.
TODAY = date(2025, 8, 15)

GAPS_INSTITUTION = "synthetic-gaps-bank"

# Six monthly statements ending 2025-06. Two months elapsed by TODAY, so the
# 2025-07 statement is missing and the 2025-08 one is still being written.
SIX_MONTHLY = ("2025-01", "2025-02", "2025-03", "2025-04", "2025-05", "2025-06")
# Six monthly statements ending 2025-01: seven elapsed months, past 3x1.
SIX_MONTHLY_LONG_AGO = ("2024-08", "2024-09", "2024-10", "2024-11", "2024-12", "2025-01")
# Current under a monthly cadence: 2025-07 is the newest statement that can
# exist on 2025-08-15.
SEVEN_MONTHLY_CURRENT = (*SIX_MONTHLY, "2025-07")
# Three rows is below the minimum, so nothing is claimed however it looks.
THREE_ROWS = ("2025-04", "2025-05", "2025-06")

OVERDUE_SLUG = "synthetic-gaps-overdue-0001"
# The matched pair. Identical coverage, identical lifecycle, DIFFERENT is_active.
DECAY_FRESH_SLUG = "synthetic-gaps-decay-fresh-0002"
DECAY_STALE_SLUG = "synthetic-gaps-decay-stale-0003"
CLOSED_SLUG = "synthetic-gaps-closed-0004"
YOUNG_SLUG = "synthetic-gaps-young-0005"
CURRENT_SLUG = "synthetic-gaps-current-0006"
SILENT_SLUG = "synthetic-gaps-silent-0007"
# A coverage slug with no `accounts` row, which `persist_statement_coverage`
# reaches routinely: it runs at finalize BEFORE account detection.
UNJOINABLE_SLUG = "synthetic-gaps-unjoinable-0008"
# Closed AND fully current: seven gapless monthly periods through 2025-07, so on
# TODAY it is current by the block's own arithmetic (cadence 1, elapsed 1,
# nothing missing) while carrying an operator closure. The published sentence
# "an account current under its own cadence has no entry" is decided HERE. The
# fixture had no such account, so the assertion that quoted that sentence checked
# CURRENT_SLUG -- which has no lifecycle record -- and could not fail.
CLOSED_CURRENT_SLUG = "synthetic-gaps-closed-current-0009"


def _insert_account(path: Path, slug: str, *, is_active: bool, last_transaction: str) -> None:
    """One accounts row. `is_active`/`last_transaction_date` are the D8 bait."""
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "INSERT INTO accounts (account_id, institution, account_number, account_type,"
            " account_subtype, first_transaction_date, last_transaction_date, is_active,"
            " transaction_count, avg_monthly_transactions, is_credit_card,"
            " detected_credit_limit, created_at, updated_at) VALUES"
            " (?, ?, ?, 'checking', '', '2024-08-01', ?, ?, 6, 1.0, 0, NULL,"
            "  '2030-01-01 00:00:00', '2030-01-01 00:00:00')",
            (
                account_identity(GAPS_INSTITUTION, slug),
                GAPS_INSTITUTION,
                slug,
                last_transaction,
                1 if is_active else 0,
            ),
        )


def _insert_coverage(path: Path, slug: str, periods: tuple[str, ...]) -> None:
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executemany(
            "INSERT INTO statement_coverage (account_slug, period, institution,"
            " statement_type, row_count, statement_count, skipped_reason, imported_at)"
            " VALUES (?, ?, ?, 'synthetic_deposit', 4, 1, '', '2030-01-01 00:00:00')",
            [(slug, period, GAPS_INSTITUTION) for period in periods],
        )


def _insert_lifecycle(path: Path, account_id: str, *, status: str) -> None:
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO account_lifecycle (account_id, status,"
            " status_confirmed_on, closed_on, note, created_at, updated_at) VALUES"
            " (?, ?, '2025-07-31', '2025-07-31', '', '2030-01-01 00:00:00',"
            "  '2030-01-01 00:00:00')",
            (account_id, status),
        )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """The synthetic database plus one account per reachable classification."""
    path = build_synthetic_mcp_database(tmp_path / "ledger.db")

    _insert_account(path, OVERDUE_SLUG, is_active=True, last_transaction="2025-06-28")
    _insert_coverage(path, OVERDUE_SLUG, SIX_MONTHLY)

    # The D8 pair. Same statements, same absence of a lifecycle record; one has
    # transacted inside the 60-day window `is_active` is derived from and the
    # other has not. Nothing else differs.
    _insert_account(path, DECAY_FRESH_SLUG, is_active=True, last_transaction="2025-08-01")
    _insert_coverage(path, DECAY_FRESH_SLUG, SIX_MONTHLY)
    _insert_account(path, DECAY_STALE_SLUG, is_active=False, last_transaction="2025-01-02")
    _insert_coverage(path, DECAY_STALE_SLUG, SIX_MONTHLY)

    _insert_account(path, CLOSED_SLUG, is_active=True, last_transaction="2025-06-28")
    _insert_coverage(path, CLOSED_SLUG, SIX_MONTHLY)
    _insert_lifecycle(path, account_identity(GAPS_INSTITUTION, CLOSED_SLUG), status="closed")

    _insert_account(path, YOUNG_SLUG, is_active=True, last_transaction="2025-06-28")
    _insert_coverage(path, YOUNG_SLUG, THREE_ROWS)

    _insert_account(path, CURRENT_SLUG, is_active=True, last_transaction="2025-07-28")
    _insert_coverage(path, CURRENT_SLUG, SEVEN_MONTHLY_CURRENT)

    _insert_account(path, SILENT_SLUG, is_active=False, last_transaction="2025-01-20")
    _insert_coverage(path, SILENT_SLUG, SIX_MONTHLY_LONG_AGO)

    _insert_account(path, CLOSED_CURRENT_SLUG, is_active=True, last_transaction="2025-07-28")
    _insert_coverage(path, CLOSED_CURRENT_SLUG, SEVEN_MONTHLY_CURRENT)
    _insert_lifecycle(
        path, account_identity(GAPS_INSTITUTION, CLOSED_CURRENT_SLUG), status="closed"
    )

    # Deliberately NO accounts row.
    _insert_coverage(path, UNJOINABLE_SLUG, SIX_MONTHLY)
    return path


@pytest.fixture
def service(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[LedgerMCPService]:
    monkeypatch.setattr("ledger.analysis.statement_cadence.gap_evaluation_date", lambda: TODAY)
    built = LedgerMCPService(db_path)
    yield built
    built.close()


def _data(result: dict[str, object]) -> dict[str, object]:
    assert result.get("isError") is not True, result
    return result["structuredContent"]["data"]  # type: ignore[index]


def _gaps(service: LedgerMCPService, **arguments: object) -> list[dict[str, object]]:
    data = _data(call_tool(service, "ledger.coverage", {"limit": 100, **arguments}))
    return list(data["gaps"])  # type: ignore[arg-type]


def _ref(slug: str, *, institution: str = GAPS_INSTITUTION) -> str:
    """The opaque ref the seeded account resolves to.

    Computed through the shipped resolver rather than scraped off the response,
    so a gap entry is located by the account it is ABOUT. Whether that value is
    the right one is `test_account_ref_namespace.py`'s question, not this
    module's.
    """
    return banking_account_ref(ACCOUNT_REF_SALT, institution, slug, "checking")


def _for(service: LedgerMCPService, ref: str, **arguments: object) -> dict[str, object]:
    matched = [gap for gap in _gaps(service, **arguments) if gap["account_ref"] == ref]
    assert len(matched) == 1, f"expected exactly one gap entry for {ref}, got {matched}"
    return matched[0]


# --- the four reachable answers ---------------------------------------------


def test_overdue_is_reachable_and_names_the_statement_that_did_not_arrive(
    service: LedgerMCPService,
) -> None:
    """Overdue is reachable and names the statement that did not arrive."""
    gap = _for(service, _ref(OVERDUE_SLUG))

    assert gap["classification"] == "overdue"
    assert gap["deciding_signal"] == "elapsed_beyond_one_cadence"
    assert gap["last_covered_period"] == "2025-06"
    assert gap["elapsed_months"] == 2
    assert gap["cadence_months"] == 1
    assert gap["cadence_confidence"] == "1.00"
    assert gap["cadence_observations"] == 6
    assert gap["missing_periods"] == ["2025-07"]
    assert gap["missing_period_count"] == 1


def test_a_lifecycle_closed_account_is_dormant_on_the_operators_own_record(
    service: LedgerMCPService,
) -> None:
    gap = _for(service, _ref(CLOSED_SLUG))

    assert gap["classification"] == "dormant"
    assert gap["deciding_signal"] == "operator_recorded_closure"
    assert gap["missing_periods"] == []


def test_an_account_with_three_coverage_rows_is_unknown_not_current(
    service: LedgerMCPService,
) -> None:
    gap = _for(service, _ref(YOUNG_SLUG))

    assert gap["classification"] == "unknown"
    assert gap["deciding_signal"] == "cadence_unknown_insufficient_history"
    assert gap["cadence_months"] is None
    assert gap["cadence_confidence"] is None
    assert gap["cadence_observations"] == 3
    assert gap["missing_periods"] == []


def test_long_silence_past_the_dormancy_multiple_is_dormant_without_any_closure(
    service: LedgerMCPService,
) -> None:
    gap = _for(service, _ref(SILENT_SLUG))

    assert gap["classification"] == "dormant"
    assert gap["deciding_signal"] == "elapsed_beyond_cadence_dormancy_multiple"
    assert gap["elapsed_months"] == 7
    assert gap["missing_periods"] == [
        "2025-02",
        "2025-03",
        "2025-04",
        "2025-05",
        "2025-06",
        "2025-07",
    ]


def test_an_account_current_under_its_own_cadence_gets_no_entry_at_all(
    service: LedgerMCPService,
) -> None:
    """Quiet on the current accounts, or the block is unreadable."""
    refs = {gap["account_ref"] for gap in _gaps(service)}

    assert _ref(CURRENT_SLUG) not in refs


def test_a_closed_account_that_is_current_is_entered_and_the_wire_says_so(
    service: LedgerMCPService,
) -> None:
    """A closed account that is current is entered and the wire says so."""
    gap = _for(service, _ref(CLOSED_CURRENT_SLUG))

    assert gap["classification"] == "dormant"
    assert gap["deciding_signal"] == "operator_recorded_closure"
    assert gap["elapsed_months"] == 1
    assert gap["cadence_months"] == 1
    assert gap["missing_period_count"] == 0, "current by its own numbers, and still entered"

    from ledger.mcp.contracts import SERVER_INSTRUCTIONS

    qualified = (
        "An account current under its own cadence has no entry UNLESS the operator "
        "recorded it closed or its history is too short"
    )
    assert qualified in " ".join(SERVER_INSTRUCTIONS.split())
    assert (
        "An account current under its own cadence has no entry UNLESS the operator "
        "recorded it closed or its history is too short to establish a cadence"
    ) in COVERAGE_GAP_STATEMENT
    assert (
        "An account current under its own cadence has no entry unless the operator "
        "recorded it closed or its history is below `gap_minimum_cadence_observations`"
    ) in _coverage_tool_description()


def test_an_entry_is_not_by_itself_an_absence_and_a_count_says_which_are(
    service: LedgerMCPService,
) -> None:
    """`len(gaps)` over-reports the question an agent asks of a block named
    `gaps`; dormant and unknown entries do not name a missing statement."""
    data = _data(call_tool(service, "ledger.coverage", {"limit": 100}))
    gaps = list(data["gaps"])  # type: ignore[call-overload]
    counts = data["gap_counts"]

    assert counts["overdue"] == sum(1 for gap in gaps if gap["classification"] == "overdue")  # type: ignore[index]
    assert counts["dormant"] == sum(1 for gap in gaps if gap["classification"] == "dormant")  # type: ignore[index]
    assert counts["unknown"] == sum(1 for gap in gaps if gap["classification"] == "unknown")  # type: ignore[index]
    naming = sum(1 for gap in gaps if int(gap["missing_period_count"]))
    assert counts["naming_a_missing_statement"] == naming  # type: ignore[index]
    assert naming < len(gaps), (
        "the fixture must contain an entry that names no missing statement, "
        "or this test cannot distinguish the two populations"
    )


# --- D8: is_active is never read --------------------------------------------


def test_two_accounts_that_differ_only_in_is_active_classify_identically(
    service: LedgerMCPService,
) -> None:
    """Two accounts that differ only in is active classify identically."""
    fresh = _for(service, _ref(DECAY_FRESH_SLUG))
    stale = _for(service, _ref(DECAY_STALE_SLUG))

    assert stale["classification"] == fresh["classification"] == "overdue"
    assert {key: value for key, value in stale.items() if key != "account_ref"} == {
        key: value for key, value in fresh.items() if key != "account_ref"
    }


def test_the_stale_twin_really_is_the_one_is_active_would_have_silenced(
    db_path: Path,
) -> None:
    """Guards the fixture, not the code.

    If both seeded rows carried the same `is_active`, the test above would pass
    against a classifier that reads it -- green as coverage, proving nothing.
    """
    with closing(sqlite3.connect(db_path)) as conn:
        stored = dict(
            conn.execute(
                "SELECT account_number, is_active FROM accounts WHERE account_number IN (?, ?)",
                (DECAY_FRESH_SLUG, DECAY_STALE_SLUG),
            ).fetchall()
        )

    assert stored == {DECAY_FRESH_SLUG: 1, DECAY_STALE_SLUG: 0}


def test_no_deciding_signal_anywhere_in_the_answer_mentions_is_active(
    service: LedgerMCPService,
) -> None:
    signals = {str(gap["deciding_signal"]) for gap in _gaps(service)}

    assert signals, "the fixture produced no gap entries, so this proves nothing"
    assert not [signal for signal in signals if "is_active" in signal], signals
    assert not [signal for signal in signals if "active" in signal], signals


def test_the_published_gap_signal_vocabulary_cannot_admit_an_is_active_answer() -> None:
    """Derived from the SCHEMA, not from the emitter. P1 had to fix the same
    omission twice because the second audit was still a reading of the code."""
    published = _gap_entry_schema()
    signals = published["properties"]["deciding_signal"]["enum"]

    assert signals
    assert not [signal for signal in signals if "active" in str(signal)], signals


# --- vocabulary: what a gap entry is allowed to claim ------------------------


def _coverage_data_schema() -> dict[str, object]:
    tool = next(tool for tool in tool_schemas() if tool["name"] == "ledger.coverage")
    success = next(
        branch
        for branch in tool["outputSchema"]["oneOf"]  # type: ignore[index]
        if "data" in branch.get("properties", {})
    )
    return success["properties"]["data"]  # type: ignore[index,return-value]


def _gap_entry_schema() -> dict[str, object]:
    return _coverage_data_schema()["properties"]["gaps"]["items"]  # type: ignore[index,return-value]


def _coverage_tool_description() -> str:
    """The third surface the same claims ship on, after SERVER_INSTRUCTIONS and
    README. This one IS digest-caught; the other two are not, which is why every
    claim below is quoted against all of them rather than against this one."""
    tool = next(tool for tool in tool_schemas() if tool["name"] == "ledger.coverage")
    return str(tool["description"])


def test_a_gap_entry_declares_no_count_of_anything_a_statement_contains() -> None:
    """A gap entry declares no count of anything a statement contains."""
    properties = set(_gap_entry_schema()["properties"])  # type: ignore[arg-type]

    assert not [
        name
        for name in properties
        if "row_count" in name or "transaction" in name or "statement_count" in name
    ], properties


def test_the_gap_statement_is_a_wire_const_and_the_response_carries_it(
    service: LedgerMCPService,
) -> None:
    data = _data(call_tool(service, "ledger.coverage", {"limit": 100}))

    assert _coverage_data_schema()["properties"]["gap_semantics"] == {  # type: ignore[index]
        "const": COVERAGE_GAP_STATEMENT
    }
    assert "gap_semantics" in _coverage_data_schema()["required"]  # type: ignore[index,operator]
    assert data["gap_semantics"] == COVERAGE_GAP_STATEMENT


def test_the_published_statement_makes_the_four_claims_it_has_to_make() -> None:
    """Quoted verbatim, so a reword that drops one of them fails here.

    Each fragment is a claim the block is misread without: no bank-side
    existence, the period-end convention with its consequence, statement
    presence rather than transaction counts, and the D8 refusal.
    """
    assert "never a claim that the institution issued one or holds one" in COVERAGE_GAP_STATEMENT
    assert (
        "A period is the month a statement ENDS in; a mid-month card cycle can contain "
        "activity from the previous calendar month."
    ) in COVERAGE_GAP_STATEMENT
    assert (
        "Gaps report statement PRESENCE only and imply nothing about how many "
        "transactions a statement carried."
    ) in COVERAGE_GAP_STATEMENT
    assert "`accounts.is_active` is never read" in COVERAGE_GAP_STATEMENT


def test_the_statement_never_asserts_that_a_statement_exists_at_the_bank() -> None:
    """The one sentence shape that would contradict `completeness: not_asserted`."""
    lowered = COVERAGE_GAP_STATEMENT.casefold()

    assert "the bank issued" not in lowered
    assert "the institution issued a statement" not in lowered
    assert "own observed cadence" in lowered or "own covered periods" in lowered


# --- scope, ordering and the withheld case -----------------------------------


def test_gaps_are_derived_from_the_whole_history_not_the_requested_window(
    service: LedgerMCPService,
) -> None:
    """A window narrower than an account's history would manufacture a cadence
    it does not have -- and would report `unknown` for every account whenever a
    caller happened to ask about three months."""
    everything = _gaps(service)
    narrow = _gaps(service, start_period="2025-05", end_period="2025-06")

    assert everything, "the fixture produced no gap entries"
    assert narrow == everything


def test_the_gaps_block_is_withheld_with_the_entries_when_no_salt_was_minted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every entry names an account, so it fails closed on the same terms. A
    salted ref cannot be minted without a write, and this server cannot write."""
    monkeypatch.setattr("ledger.analysis.statement_cadence.gap_evaluation_date", lambda: TODAY)
    path = build_synthetic_mcp_database(tmp_path / "unsalted.db", account_ref_salt=False)
    _insert_account(path, OVERDUE_SLUG, is_active=True, last_transaction="2025-06-28")
    _insert_coverage(path, OVERDUE_SLUG, SIX_MONTHLY)
    built = LedgerMCPService(path)
    try:
        data = _data(call_tool(built, "ledger.coverage", {"limit": 100}))
    finally:
        built.close()

    assert data["account_refs"] == "withheld"
    assert data["gaps"] == []
    assert data["gaps_assessed_accounts"] == 0
    assert data["gap_semantics"] == COVERAGE_GAP_STATEMENT


def test_the_response_names_the_date_the_elapsed_arithmetic_used(
    service: LedgerMCPService,
) -> None:
    """`overdue` is a function of the clock as well as of the data, so the clock
    reading is disclosed rather than left for the caller to guess."""
    data = _data(call_tool(service, "ledger.coverage", {"limit": 100}))

    assert data["gaps_evaluated_on"] == TODAY.isoformat()


def test_every_assessed_account_is_counted_even_when_it_has_no_gap(
    service: LedgerMCPService,
) -> None:
    """Eleven accounts have coverage; one of them is current AND unrecorded, so
    `gaps` is shorter than the census. Without the census a caller cannot tell
    "no gaps" from "nothing was assessed" -- which is all the census can do: it
    cannot report a truncation, because a shorter list is also the healthy
    state. `gaps_truncated` carries that."""
    data = _data(call_tool(service, "ledger.coverage", {"limit": 100}))

    assert data["gaps_assessed_accounts"] == 11
    assert len(list(data["gaps"])) == 10  # type: ignore[arg-type]
    assert data["gaps_truncated"] is False
    assert data["gaps_unscoreable_accounts"] == 0


def test_gap_entries_are_ordered_by_the_stored_slug_not_by_the_derived_ref(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cursor law's second half, applied to an unpaginated block.

    Ordering on a salted ref makes the served order depend on a value no
    fingerprint binds. The order is asserted against the stored column by
    reading the slugs directly and mapping them through the resolver.
    """
    monkeypatch.setattr("ledger.analysis.statement_cadence.gap_evaluation_date", lambda: TODAY)
    built = LedgerMCPService(db_path)
    try:
        served = [gap["account_ref"] for gap in _gaps(built)]
    finally:
        built.close()

    with closing(sqlite3.connect(db_path)) as conn:
        slugs = [
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT account_slug FROM statement_coverage ORDER BY account_slug"
            )
        ]
    expected = {
        PRIVATE_ACCOUNT_NUMBER: banking_account_ref(
            ACCOUNT_REF_SALT, BANK_INSTITUTION, PRIVATE_ACCOUNT_NUMBER, "chequing"
        ),
        PRIVATE_CARD_NUMBER: banking_account_ref(
            ACCOUNT_REF_SALT, CARD_INSTITUTION, PRIVATE_CARD_NUMBER, "credit_card"
        ),
        UNJOINABLE_SLUG: unresolved_account_ref(ACCOUNT_REF_SALT, UNJOINABLE_SLUG),
    }
    ordered = [expected.get(slug) or _ref(slug) for slug in slugs if slug != CURRENT_SLUG]

    assert served == ordered


# --- the unjoinable slug -----------------------------------------------------


def test_a_coverage_slug_with_no_account_record_gets_the_sentinel_here_too(
    service: LedgerMCPService,
) -> None:
    """`persist_statement_coverage` runs at finalize BEFORE account detection, so
    this is a state production reaches. A blank or null key would make two
    different unjoinable accounts byte-identical (D3)."""
    gap = _for(service, unresolved_account_ref(ACCOUNT_REF_SALT, UNJOINABLE_SLUG))

    assert gap["account_ref_status"] == "no_account_record"
    assert gap["classification"] == "overdue"


def test_an_account_that_aged_out_of_the_dimension_can_still_be_marked_closed(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The end state the lifecycle table exists for.

    `save_accounts(replace_all=True)` deletes an account once its last
    transaction ages out, and a closed account is the likeliest to. If closure
    could only be recorded against a surviving `accounts` row, the one account
    that most needs silencing could never be silenced.
    """
    monkeypatch.setattr("ledger.analysis.statement_cadence.gap_evaluation_date", lambda: TODAY)
    _insert_lifecycle(db_path, account_identity(GAPS_INSTITUTION, UNJOINABLE_SLUG), status="closed")
    built = LedgerMCPService(db_path)
    try:
        gap = _for(built, unresolved_account_ref(ACCOUNT_REF_SALT, UNJOINABLE_SLUG))
    finally:
        built.close()

    assert gap["classification"] == "dormant"
    assert gap["deciding_signal"] == "operator_recorded_closure"


# --- the envelope stays valid ------------------------------------------------


def test_the_whole_answer_still_validates_against_its_published_schema(
    service: LedgerMCPService,
) -> None:
    """`call_tool` validates before returning, so a schema violation would come
    back as `corrupt_data` rather than as a wrong answer. Asserted explicitly
    because a silently-error envelope reads like a passing test otherwise."""
    result = call_tool(service, "ledger.coverage", {"limit": 100})

    assert result.get("isError") is not True, result
    assert "gaps" in json.dumps(result["structuredContent"], ensure_ascii=False)


# --- the digest-neutral surface ---------------------------------------------


def test_the_server_instructions_gap_claims_are_true_of_the_shipped_answer(
    service: LedgerMCPService,
) -> None:
    """`instructions=` is digest-neutral, so NOTHING but a test catches a false
    claim there -- which is exactly how the account-ref falsehood survived a
    whole release. Each sentence below is quoted from the shipped text so a
    REWORD breaks this test (whitespace is flattened first, so a rewrap does
    not), and each is then proven against a real envelope rather than read off
    the code that produces it.
    """
    from ledger.mcp.contracts import SERVER_INSTRUCTIONS

    published = " ".join(SERVER_INSTRUCTIONS.split())
    gaps = _gaps(service)
    assert gaps, "the fixture produced no gap entries, so every claim below is vacuous"
    by_class: dict[str, list[dict[str, object]]] = {}
    for gap in gaps:
        by_class.setdefault(str(gap["classification"]), []).append(gap)

    assert "One entry per account" in published
    refs = [gap["account_ref"] for gap in gaps]
    assert len(refs) == len(set(refs))

    assert (
        "An account current under its own cadence has no entry UNLESS the operator "
        "recorded it closed or its history is too short to claim a cadence"
    ) in published
    assert _ref(CURRENT_SLUG) not in set(refs)
    # The exception, proven rather than asserted: an account that IS current and
    # IS entered, which is what makes the qualification load-bearing.
    closed_current = _for(service, _ref(CLOSED_CURRENT_SLUG))
    assert closed_current["missing_period_count"] == 0
    assert int(closed_current["elapsed_months"]) <= int(closed_current["cadence_months"])  # type: ignore[arg-type]

    assert "`deciding_signal` names the one fact behind every entry" in published
    signals = set(_gap_entry_schema()["properties"]["deciding_signal"]["enum"])  # type: ignore[index]
    assert {str(gap["deciding_signal"]) for gap in gaps} <= signals

    assert (
        "`unknown` is too little history to establish a cadence, in which case no "
        "statement is called missing"
    ) in published
    assert by_class["unknown"], "no unknown entry, so the claim above is untested"
    assert all(gap["missing_periods"] == [] for gap in by_class["unknown"])
    assert all(gap["cadence_months"] is None for gap in by_class["unknown"])

    assert "`overdue` is silence past one cadence step" in published
    assert by_class["overdue"], "no overdue entry, so the claim above is untested"
    overdue = [
        gap for gap in by_class["overdue"] if gap["deciding_signal"] == "elapsed_beyond_one_cadence"
    ]
    assert overdue
    assert all(int(gap["elapsed_months"]) > int(gap["cadence_months"]) for gap in overdue)  # type: ignore[arg-type]

    assert (
        "`dormant` is silence past a multiple of it or a closure the operator recorded locally"
    ) in published
    assert by_class["dormant"], "no dormant entry, so the claim above is untested"
    assert {str(gap["deciding_signal"]) for gap in by_class["dormant"]} == {
        "operator_recorded_closure",
        "elapsed_beyond_cadence_dormancy_multiple",
    }

    assert 'never as "the bank issued a statement"' in published
    assert 'none of it is derived from any stored "active" flag' in published
    assert "A period is the month a statement ENDS in" in published

    # The clause the previous version of this audit stopped one short of. It
    # said "nothing in this server can see the institution", which the same
    # response contradicts: every coverage entry carries one, and
    # `ledger.dimensions` lists them all. The replacement is quoted in full and
    # then checked against the envelope that is supposed to satisfy it.
    assert (
        "this server reads the local import record and nothing else, so the institution "
        "it names is the one the imported statement carried, never a report of what that "
        "institution issued or holds"
    ) in published
    assert "nothing in this server can see the institution" not in published
    entries = list(
        _data(call_tool(service, "ledger.coverage", {"limit": 100}))["entries"]  # type: ignore[call-overload]
    )
    assert entries
    assert [entry for entry in entries if entry["institution"]], (
        "the server emits an institution on its own entries, so it cannot claim it sees none"
    )

    assert (
        "The block is per ACCOUNT and UNPAGINATED: it repeats in full on every page of "
        "one cursor walk"
    ) in published


# --- derived from the published schema, not from the emitter ------------------


def test_every_declared_gap_property_is_also_required(
    service: LedgerMCPService,
) -> None:
    """An optional property is a property a consumer must branch on.

    Derived from the schema rather than from a list kept in step with it: adding
    a field to `_COVERAGE_GAP_SCHEMA` without adding it to `required` would let
    the same tool answer with two different shapes, and every entry the fixture
    serves is checked to carry the full set.
    """
    schema = _gap_entry_schema()
    declared = set(schema["properties"])  # type: ignore[arg-type]

    assert set(schema["required"]) == declared, declared.symmetric_difference(  # type: ignore[arg-type]
        set(schema["required"])  # type: ignore[arg-type]
    )
    gaps = _gaps(service)
    assert gaps
    assert all(set(gap) == declared for gap in gaps)


def test_a_very_long_silence_truncates_the_period_list_and_says_how_many(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`missing_periods` is capped by the published `maxItems`, so the count has
    to carry the real total -- and the cap has to be applied in code, because the
    envelope is validated against that schema before it is served."""
    monkeypatch.setattr("ledger.analysis.statement_cadence.gap_evaluation_date", lambda: TODAY)
    path = build_synthetic_mcp_database(tmp_path / "ancient.db")
    ancient = ("2015-01", "2015-02", "2015-03", "2015-04")
    _insert_account(path, OVERDUE_SLUG, is_active=False, last_transaction="2015-04-28")
    _insert_coverage(path, OVERDUE_SLUG, ancient)
    built = LedgerMCPService(path)
    try:
        gap = _for(built, _ref(OVERDUE_SLUG))
    finally:
        built.close()

    maximum = int(_gap_entry_schema()["properties"]["missing_periods"]["maxItems"])  # type: ignore[index]
    assert gap["classification"] == "dormant"
    # 2015-05 through 2025-07 inclusive: ten years and three months of silence.
    assert gap["missing_period_count"] == 123
    assert len(list(gap["missing_periods"])) == maximum == 60  # type: ignore[arg-type]
    assert next(iter(gap["missing_periods"])) == "2015-05"  # type: ignore[arg-type]


def test_the_published_vocabularies_equal_the_ones_the_classifier_can_produce() -> None:
    """Two hand-written lists in two files, and drift between them is silent.

    A signal added to `statement_cadence` but not to the schema does not raise:
    the envelope fails its own validation and `ledger.coverage` starts answering
    `corrupt_data` for EVERY caller, on a database that is perfectly fine. The
    schema's enums are therefore derived here from the module's own published
    vocabularies rather than compared by eye.

    `NO_ENTRY_SIGNALS` are the ones excluded: each decides that there is NO
    entry, so neither can appear on an entry, and listing them would advertise
    values no answer can carry. Subtracted through the module's own tuple rather
    than by naming them here, because a third such signal added there and not
    here would make every answer fail its own schema.
    """
    from ledger.analysis.statement_cadence import (
        ENTRY_DECIDING_SIGNALS,
        GAP_CLASSIFICATIONS,
        NO_ENTRY_SIGNALS,
    )

    schema = _gap_entry_schema()["properties"]  # type: ignore[index]

    assert set(schema["classification"]["enum"]) == set(GAP_CLASSIFICATIONS)  # type: ignore[index]
    assert set(schema["deciding_signal"]["enum"]) == set(ENTRY_DECIDING_SIGNALS)  # type: ignore[index]
    assert NO_ENTRY_SIGNALS
    assert not set(schema["deciding_signal"]["enum"]) & set(NO_ENTRY_SIGNALS)  # type: ignore[index]


def test_an_absurd_but_well_formed_stored_period_does_not_take_the_tool_down(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`statement_coverage.period` is unconstrained TEXT and the ENTRIES block
    serves `0001-01` happily, so a month bound on the gaps block tighter than
    what a period can express would turn one absurd row into `corrupt_data` for
    the whole tool -- including for the eight accounts answering correctly."""
    monkeypatch.setattr("ledger.analysis.statement_cadence.gap_evaluation_date", lambda: TODAY)
    path = build_synthetic_mcp_database(tmp_path / "absurd.db")
    _insert_account(path, OVERDUE_SLUG, is_active=True, last_transaction="2025-06-28")
    _insert_coverage(path, OVERDUE_SLUG, ("0001-01", "0001-02", "0001-03", "0001-04"))
    built = LedgerMCPService(path)
    try:
        result = call_tool(built, "ledger.coverage", {"limit": 100})
        assert result.get("isError") is not True, result
        gap = _for(built, _ref(OVERDUE_SLUG))
    finally:
        built.close()

    assert gap["classification"] == "dormant"
    # 0001-04 to 2025-08 in months. Comfortably past the 12,000 the first draft
    # of the schema allowed, which is exactly how this bound was found.
    assert gap["elapsed_months"] == 24_292
    assert gap["missing_period_count"] == 24_291


# --- the block is unpaginated, and the wire has to say so --------------------


def test_the_gaps_block_repeats_in_full_on_every_page_and_the_wire_says_so(
    service: LedgerMCPService,
) -> None:
    """`gaps` has exactly the shape the pagination protocol trains a client to
    accumulate -- an array of per-row objects in the same `data` object as
    `entries` -- while `page.returned` describes only `entries`. A caller
    following the documented cursor walk and concatenating `data.gaps`
    multiplies every account by the page count.
    """
    opened = call_tool(service, "ledger.coverage", {"limit": 3})
    assert opened.get("isError") is not True, opened
    first = opened["structuredContent"]["data"]  # type: ignore[index]
    page = opened["structuredContent"]["page"]  # type: ignore[index]
    assert first["entry_count"] > 3
    assert page["has_more"] is True
    second = _data(
        call_tool(service, "ledger.coverage", {"limit": 3, "cursor": page["next_cursor"]})
    )

    assert len(list(first["entries"])) == 3  # type: ignore[arg-type]
    assert len(list(second["entries"])) == 3  # type: ignore[arg-type]
    assert first["gaps"] == second["gaps"], "the block is per account, not per page"
    assert len(list(first["gaps"])) > 3  # type: ignore[arg-type]

    published = _coverage_tool_description()
    assert "The block is per account and UNPAGINATED" in published
    assert "accumulate `entries` across pages and take `gaps` from one page" in published
    assert (
        "The block is per ACCOUNT and unpaginated: it is identical on every page of one "
        "cursor walk, so accumulate `entries` across pages and take `gaps` from any "
        "single page."
    ) in COVERAGE_GAP_STATEMENT


# --- the numbers that decide a verdict are on the wire -----------------------


def test_the_three_thresholds_that_decide_a_verdict_are_published_and_applied(
    service: LedgerMCPService,
) -> None:
    """Held privately they were outside the contract digest, so a one-character
    edit reclassified accounts with the published contract unchanged -- and a
    caller holding `cadence_observations: 3, classification: "unknown"` could not
    tell whether one more statement or thirty would change the answer.

    Asserted against the module that APPLIES them, not against copies.
    """
    from ledger.analysis.statement_cadence import (
        DELIVERY_GRACE_DAYS,
        DORMANCY_MULTIPLIER,
        MIN_CADENCE_OBSERVATIONS,
    )

    schema = _coverage_data_schema()["properties"]  # type: ignore[index]
    data = _data(call_tool(service, "ledger.coverage", {"limit": 100}))

    assert schema["gap_minimum_cadence_observations"] == {"const": MIN_CADENCE_OBSERVATIONS}  # type: ignore[index]
    assert schema["gap_dormancy_multiplier"] == {"const": DORMANCY_MULTIPLIER}  # type: ignore[index]
    assert schema["gap_delivery_grace_days"] == {"const": DELIVERY_GRACE_DAYS}  # type: ignore[index]
    assert data["gap_minimum_cadence_observations"] == MIN_CADENCE_OBSERVATIONS
    assert data["gap_dormancy_multiplier"] == DORMANCY_MULTIPLIER
    assert data["gap_delivery_grace_days"] == DELIVERY_GRACE_DAYS

    # And the published numbers explain the answers actually served: the account
    # reported `unknown` sits below the minimum, and the account reported
    # `dormant` on elapsed time alone sits past the multiple.
    young = _for(service, _ref(YOUNG_SLUG))
    assert int(young["cadence_observations"]) < int(data["gap_minimum_cadence_observations"])  # type: ignore[arg-type]
    silent = _for(service, _ref(SILENT_SLUG))
    assert int(silent["elapsed_months"]) > int(silent["cadence_months"]) * int(  # type: ignore[arg-type,operator]
        data["gap_dormancy_multiplier"]  # type: ignore[arg-type]
    )


# --- row_count is not a transaction count, and that belongs on the wire -------


def test_the_entry_row_count_semantics_are_a_wire_const_the_response_carries(
    service: LedgerMCPService,
) -> None:
    """`status` is literally `row_count > 0` under the name
    `imported_with_activity`, and `row_count` sums the extractor's CSV rows
    INCLUDING transfer legs. The sibling `ledger.changes` publishes
    CHANGES_COUNT_STATEMENT for the identical column; this tool published
    nothing, so the explanation lived only in a Python comment and in README --
    the two places an MCP client never reads.
    """
    from ledger.mcp.contracts import COVERAGE_ROW_COUNT_STATEMENT

    data = _data(call_tool(service, "ledger.coverage", {"limit": 100}))

    assert _coverage_data_schema()["properties"]["row_count_semantics"] == {  # type: ignore[index]
        "const": COVERAGE_ROW_COUNT_STATEMENT
    }
    assert "row_count_semantics" in _coverage_data_schema()["required"]  # type: ignore[index,operator]
    assert data["row_count_semantics"] == COVERAGE_ROW_COUNT_STATEMENT
    assert "is not a transaction count" in COVERAGE_ROW_COUNT_STATEMENT
    assert "INCLUDING rows the importer routed into the separate transfers table" in (
        COVERAGE_ROW_COUNT_STATEMENT
    )
    assert (
        "The status imported_with_activity means only row_count is positive"
    ) in COVERAGE_ROW_COUNT_STATEMENT


def test_an_account_month_can_be_imported_with_activity_and_hold_no_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An account month can be imported with activity and hold no transaction."""
    monkeypatch.setattr("ledger.analysis.statement_cadence.gap_evaluation_date", lambda: TODAY)
    path = build_synthetic_mcp_database(tmp_path / "transfer-only.db")
    _insert_account(path, OVERDUE_SLUG, is_active=True, last_transaction="2025-06-28")
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "INSERT INTO statement_coverage (account_slug, period, institution,"
            " statement_type, row_count, statement_count, skipped_reason, imported_at)"
            " VALUES (?, '2025-06', ?, 'synthetic_deposit', 1, 1, '',"
            "         '2030-01-01 00:00:00')",
            (OVERDUE_SLUG, GAPS_INSTITUTION),
        )
        stored = conn.execute(
            "SELECT count(*) FROM transactions WHERE account = ? AND source_period = '2025-06'",
            (OVERDUE_SLUG,),
        ).fetchone()[0]
    assert stored == 0

    built = LedgerMCPService(path)
    try:
        data = _data(call_tool(built, "ledger.coverage", {"limit": 100}))
    finally:
        built.close()

    served = [
        entry
        for entry in data["entries"]  # type: ignore[call-overload]
        if entry["account_ref"] == _ref(OVERDUE_SLUG) and entry["period"] == "2025-06"
    ]
    assert len(served) == 1, served
    assert served[0]["row_count"] == 1
    assert served[0]["status"] == "imported_with_activity"


# --- truncation, unscoreable slugs, and the scan cap -------------------------


def test_a_truncated_gaps_list_is_flagged_rather_than_left_to_be_inferred(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`len(gaps) < gaps_assessed_accounts` can describe either healthy accounts or
    truncation, so a separate flag must disclose a cut list."""
    monkeypatch.setattr("ledger.analysis.statement_cadence.gap_evaluation_date", lambda: TODAY)
    path = build_synthetic_mcp_database(tmp_path / "many.db")
    for index in range(505):
        _insert_account(
            path, f"synthetic-bulk-{index:04d}", is_active=True, last_transaction="2025-06-28"
        )
        _insert_coverage(path, f"synthetic-bulk-{index:04d}", SIX_MONTHLY)
    built = LedgerMCPService(path)
    try:
        result = call_tool(built, "ledger.coverage", {"limit": 100})
        assert result.get("isError") is not True, result
        data = result["structuredContent"]["data"]  # type: ignore[index]
    finally:
        built.close()

    maximum = int(_coverage_data_schema()["properties"]["gaps"]["maxItems"])  # type: ignore[index]
    assert len(list(data["gaps"])) == maximum == 500
    assert data["gaps_truncated"] is True
    assert data["gaps_assessed_accounts"] == 507


def test_a_slug_whose_periods_are_all_unscoreable_is_counted_not_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`statement_coverage.period` is unconstrained TEXT. An account whose rows
    are all unparseable can be neither classified nor censused as assessed, and
    dropping it from both is the "absent means fine" reading this tool exists to
    prevent."""
    monkeypatch.setattr("ledger.analysis.statement_cadence.gap_evaluation_date", lambda: TODAY)
    path = build_synthetic_mcp_database(tmp_path / "unscoreable.db")
    _insert_account(path, OVERDUE_SLUG, is_active=True, last_transaction="2025-06-28")
    with closing(sqlite3.connect(path)) as conn, conn:
        # `2025-013` fails the month pattern (an extra digit) and sorts strictly
        # BETWEEN the fixture's own `2025-01` and `2025-02`. Both properties are
        # needed: `observed_periods` is a min/max over the whole table and is
        # published under the month schema, so a malformed value outside the
        # real range fails the envelope for an unrelated, pre-existing reason and
        # would prove nothing about this block.
        conn.execute(
            "INSERT INTO statement_coverage (account_slug, period, institution,"
            " statement_type, row_count, statement_count, skipped_reason, imported_at)"
            " VALUES (?, '2025-013', ?, 'synthetic_deposit', 4, 1, '',"
            "         '2030-01-01 00:00:00')",
            (OVERDUE_SLUG, GAPS_INSTITUTION),
        )
    built = LedgerMCPService(path)
    try:
        # Windowed so the malformed row stays out of the ENTRIES block, whose
        # `period` schema it would fail. `gaps` still reads the whole table
        # regardless of the window, which is exactly the point.
        data = _data(call_tool(built, "ledger.coverage", {"limit": 100, "start_period": "2025-02"}))
    finally:
        built.close()

    assert data["gaps_unscoreable_accounts"] == 1
    assert _ref(OVERDUE_SLUG) not in {gap["account_ref"] for gap in data["gaps"]}  # type: ignore[call-overload]


def test_the_uncapped_gap_scan_raises_rather_than_reading_an_unbounded_table(
    db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one add-a-tool checklist item this surface skipped. The gap scan is
    deliberately unfiltered -- cadence is a property of an account's COMPLETE
    history -- so it is the read with no caller-supplied bound at all."""
    monkeypatch.setattr("ledger.analysis.statement_cadence.gap_evaluation_date", lambda: TODAY)
    monkeypatch.setattr("ledger.query.agent._MAX_GAP_COVERAGE_ROWS", 4)
    built = LedgerMCPService(db_path)
    try:
        result = call_tool(built, "ledger.coverage", {"limit": 100})
    finally:
        built.close()

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "filter_too_broad"


# --- derived from the published schemas, not from a reading of the emitters ----


def test_every_count_the_published_schemas_declare_is_explained_on_the_wire() -> None:
    """A NAME-shaped audit over the schemas, not a list of the counts someone
    remembered to disclose.

    `row_count` shipped as `status: "imported_with_activity"` with no wire
    disclosure at all, while the sibling `ledger.changes` published
    CHANGES_COUNT_STATEMENT for the identical column -- the explanation existed,
    in a Python comment and in README, which is nowhere an MCP client reads. This
    fails for the NEXT count added without one, which is the only kind of audit
    that would have caught the last one.
    """
    from ledger.mcp.contracts import (
        ACCOUNT_REF_SEMANTICS,
        COVERAGE_ABSENCE_STATEMENT,
        COVERAGE_ROW_COUNT_STATEMENT,
    )

    published = " ".join(
        (
            COVERAGE_ABSENCE_STATEMENT,
            COVERAGE_GAP_STATEMENT,
            COVERAGE_ROW_COUNT_STATEMENT,
            ACCOUNT_REF_SEMANTICS,
        )
    )
    data = _coverage_data_schema()
    declared = {
        *data["properties"],  # type: ignore[misc]
        *data["properties"]["entries"]["items"]["properties"],  # type: ignore[index]
        *_gap_entry_schema()["properties"],  # type: ignore[misc]
    }
    # Suffix-matched, not substring-matched: "account" contains "count", so a
    # substring test sweeps in every account-ref field and turns the audit into
    # noise nobody keeps green.
    counts = sorted(name for name in declared if name.endswith(("_count", "_counts", "_accounts")))

    assert counts, "the audit found no count-shaped property, so it proves nothing"
    assert not [name for name in counts if name not in published], (
        f"declared but unexplained on the wire: "
        f"{[name for name in counts if name not in published]}"
    )


def test_the_status_vocabulary_is_explained_where_it_is_not_self_evident() -> None:
    """`imported_with_activity` is literally `row_count > 0`, and row_count
    includes transfer legs, so the NAME asserts something the value does not."""
    from ledger.mcp.contracts import COVERAGE_ABSENCE_STATEMENT, COVERAGE_ROW_COUNT_STATEMENT

    statuses = _coverage_data_schema()["properties"]["entries"]["items"]["properties"]["status"][  # type: ignore[index]
        "enum"
    ]
    published = COVERAGE_ABSENCE_STATEMENT + COVERAGE_ROW_COUNT_STATEMENT

    assert "imported_with_activity" in statuses
    assert "imported_with_activity" in published


# ---------------------------------------------------------------------------
# The dashboard cites THIS tool's rule, so the two must quantify over one set.
#
# `statement_frontier` publishes "every non-dormant account with imported
# statement coverage has a statement for {period} or later" and spec §10 says
# outright that "the claim follows the same rule `ledger.coverage` publishes".
# Both once assembled that population themselves -- the same slug/period gather,
# the same `min()` institution tie-break, the same `lifecycle_account_ids`
# fallback -- with nothing binding the copies together. They share a gatherer
# now; this is the test that keeps them sharing one.
# ---------------------------------------------------------------------------


def test_the_health_frontier_partitions_the_same_accounts_this_tool_classifies(
    service: LedgerMCPService, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One fixture, one dormant/non-dormant split, checked both ways.

    The fixture carries a dormant account by closure, a dormant one by silence,
    an overdue one, an unknown one, a current one with no entry at all, and a
    coverage slug with no `accounts` row -- so a population narrowed on either
    surface moves one of the two numbers below.
    """
    from ledger.analysis.health_scorer import statement_frontier

    monkeypatch.setattr("ledger.analysis.statement_cadence.gap_evaluation_date", lambda: TODAY)
    data = _data(call_tool(service, "ledger.coverage", {"limit": 100}))
    gaps = list(data["gaps"])  # type: ignore[arg-type]
    dormant_refs = {str(gap["account_ref"]) for gap in gaps if gap["classification"] == "dormant"}
    assessed = int(data["gaps_assessed_accounts"])  # type: ignore[call-overload]

    # Every account-month this tool serves, walked to completion, so the newest
    # covered period per account comes from the tool rather than from the fixture.
    newest: dict[str, str] = {}
    cursor: object | None = None
    while True:
        arguments: dict[str, object] = {"limit": 100}
        if cursor is not None:
            arguments["cursor"] = cursor
        envelope = call_tool(service, "ledger.coverage", arguments)["structuredContent"]
        for entry in envelope["data"]["entries"]:  # type: ignore[index]
            ref = str(entry["account_ref"])
            period = str(entry["period"])
            newest[ref] = max(newest.get(ref, period), period)
        if not envelope["page"]["has_more"]:  # type: ignore[index]
            break
        cursor = envelope["page"]["next_cursor"]  # type: ignore[index]

    frontier = statement_frontier(db_path)

    assert dormant_refs, "the fixture produced no dormant account, so the split proves nothing"
    assert len(newest) > len(dormant_refs), "every account is dormant; the frontier is untested"
    assert frontier.dormant_accounts == len(dormant_refs)
    assert frontier.non_dormant_accounts == assessed - len(dormant_refs)
    # And the published minimum is the minimum over exactly that non-dormant set.
    assert frontier.period == min(
        period for ref, period in newest.items() if ref not in dormant_refs
    )
