"""Projection responses reconcile each fictional account to the cent.

The suite exercises unsigned stored amounts, retrospective dates, account-local
patterns, missing anchors, cadence limits and pagination disclosures. Detected
patterns use synthetic transaction streams that span multiple accounts."""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from mcp_synthetic import (
    ACCOUNT_REF_SALT,
    BANK_INSTITUTION,
    CARD_INSTITUTION,
    CHINESE_SOURCE,
    LEGACY_FREQUENCY_MERCHANT,
    LEGACY_TYPE_MERCHANT,
    MIGRATED_FROM_NUMBER,
    MIGRATED_TO_AMOUNT,
    MIGRATED_TO_NUMBER,
    PAYROLL_MAIN_AMOUNT,
    PAYROLL_MAIN_NUMBER,
    PAYROLL_SECOND_AMOUNT,
    PAYROLL_SECOND_NUMBER,
    POOLED_MIGRATED_MEAN,
    POOLED_PAYROLL_MEAN,
    PRIVATE_ACCOUNT_NUMBER,
    PRIVATE_CARD_NUMBER,
    PRIVATE_SAVINGS_NUMBER,
    PRIVATE_UNCOMPUTED_CARD_NUMBER,
    PRIVATE_UNLIMITED_CARD_NUMBER,
    PRIVATE_VALUES,
    RECURRING_BILL,
    RECURRING_LAPSED,
    RECURRING_PAST_DUE,
    RECURRING_SAVINGS_INTEREST,
    RECURRING_SUBSCRIPTION,
    RECURRING_TRANSFER,
    SPLIT_PAYROLL_MERCHANT,
    build_detected_pattern_database,
    build_synthetic_mcp_database,
)

from ledger.analysis.projection import DEFAULT_MAX_LINE_ITEMS, PROJECTION_HORIZON_DAYS
from ledger.analysis.runner import AnalysisRunner
from ledger.mcp.contracts import (
    ACCOUNT_REF_SEMANTICS,
    BALANCE_LEVEL_STATEMENT,
    PROJECTION_AMOUNT_STATEMENT,
    PROJECTION_ATTRIBUTION_STATEMENT,
    PROJECTION_MISSED_COUNT_STATEMENT,
    PROJECTION_RECONCILIATION_STATEMENT,
    PROJECTION_ROLL_FORWARD_STATEMENT,
    PROJECTION_SCOPE_STATEMENT,
    RECURRING_CLASSIFICATION_CAVEAT,
    RECURRING_STORED_FIELD_STATEMENT,
    SERVER_INSTRUCTIONS,
    SIGNED_MONEY_SCHEMA,
    tool_schemas,
)
from ledger.mcp.server import call_tool
from ledger.mcp.service import LedgerMCPService
from ledger.query.account_refs import banking_account_ref
from ledger.storage.database import (
    AccountBalanceDB,
    InvestmentActivityDB,
    InvestmentPositionDB,
    InvestmentStatementDB,
    RecurringPatternDB,
    session_scope,
)
from ledger.utils.dates import naive_utc_now

# The anchors this fixture holds are all in June 2025, so this as-of selects the
# newest of each and leaves the savings account -- which has no absolute series at
# all -- as the refusal case.
_AS_OF = "2025-06-30"
_TARGET = "2025-08-31"
_WINDOW = {"as_of": _AS_OF, "target_date": _TARGET}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return build_synthetic_mcp_database(tmp_path / "ledger.db")


@pytest.fixture
def service(db_path: Path) -> LedgerMCPService:
    built = LedgerMCPService(db_path)
    yield built
    built.close()


def _data(result: dict[str, object]) -> dict[str, object]:
    assert result.get("isError") is not True, result
    return result["structuredContent"]["data"]  # type: ignore[index]


def _by_ref(result: dict[str, object]) -> dict[str, dict[str, object]]:
    entries = _data(result)["entries"]
    assert entries, "an empty entry list proves nothing about the entries"
    return {entry["account_ref"]: entry for entry in entries}  # type: ignore[index,union-attr]


def _ref(institution: str, account_number: str, account_type: str) -> str:
    return banking_account_ref(ACCOUNT_REF_SALT, institution, account_number, account_type)


CHEQUING_REF = _ref(BANK_INSTITUTION, PRIVATE_ACCOUNT_NUMBER, "chequing")
SAVINGS_REF = _ref(BANK_INSTITUTION, PRIVATE_SAVINGS_NUMBER, "savings")
CARD_REPORTED_REF = _ref(CARD_INSTITUTION, PRIVATE_CARD_NUMBER, "credit_card")
CARD_UNCOMPUTED_REF = _ref(CARD_INSTITUTION, PRIVATE_UNCOMPUTED_CARD_NUMBER, "credit_card")
CARD_UNLIMITED_REF = _ref(CARD_INSTITUTION, PRIVATE_UNLIMITED_CARD_NUMBER, "credit_card")


def _lines(entry: dict[str, object]) -> list[dict[str, object]]:
    return entry["line_items"]  # type: ignore[return-value]


# --- 1. the reconciliation gate ----------------------------------------------


def test_every_account_reconciles_anchor_plus_line_items_to_the_projection(
    service: LedgerMCPService,
) -> None:
    """THE release gate. Exactly, per account, as Decimal -- not within a cent."""
    entries = _by_ref(call_tool(service, "ledger.projection", {**_WINDOW, "limit": 100}))
    projected = [entry for entry in entries.values() if entry["projected_balance"] is not None]

    assert len(projected) == 4, entries
    for entry in projected:
        anchor = Decimal(entry["anchor"]["balance"])  # type: ignore[index,arg-type]
        flow = Decimal(entry["net_projected_flow"])  # type: ignore[arg-type]
        items = sum(
            (Decimal(line["amount"]) for line in _lines(entry)),  # type: ignore[arg-type]
            start=Decimal("0.00"),
        )

        assert items == flow
        assert anchor + flow == Decimal(entry["projected_balance"])  # type: ignore[arg-type]
        assert entry["line_item_count"] == len(_lines(entry))


def test_the_chequing_projection_is_the_exact_arithmetic_it_discloses(
    service: LedgerMCPService,
) -> None:
    """Every number spelled out, so a silent change to any of the three inputs --
    the anchor, the direction map, or the window -- fails here.

    The 2025-07-08 income line is the one a forward-only window drops: its
    pattern's stored next_expected_date is 2025-08-08, so that occurrence lies
    strictly after the 2025-06-20 anchor and strictly before the stored date.
    Neither term covered it, and the reconciliation identity could not see it
    go, because a dropped line item is dropped from both of its sides.
    """
    entry = _by_ref(call_tool(service, "ledger.projection", _WINDOW))[CHEQUING_REF]

    assert entry["status"] == "projected"
    assert entry["anchor"] == {
        "as_of_lag_days": 10,
        "balance": "5100.00",
        "balance_basis": "absolute",
        "balance_date": "2025-06-20",
        "status": "reported",
    }
    assert [
        (line["merchant"], line["projected_date"], line["amount"]) for line in _lines(entry)
    ] == [
        (RECURRING_PAST_DUE, "2025-06-25", "-45.00"),
        (CHINESE_SOURCE, "2025-07-08", "1000.10"),
        (RECURRING_BILL, "2025-07-20", "-88.20"),
        (RECURRING_PAST_DUE, "2025-07-25", "-45.00"),
        (CHINESE_SOURCE, "2025-08-08", "1000.10"),
        (RECURRING_BILL, "2025-08-20", "-88.20"),
        (RECURRING_PAST_DUE, "2025-08-25", "-45.00"),
    ]
    assert entry["net_projected_flow"] == "1688.80"
    assert entry["projected_balance"] == "6788.80"


def test_the_window_opens_after_the_anchor_and_closes_on_the_target(
    service: LedgerMCPService,
) -> None:
    """A flow ON the anchor date is already inside the anchor -- the absolute
    series is daily -- and a flow on the target date is what the question asks
    about."""
    entry = _by_ref(
        call_tool(service, "ledger.projection", {**_WINDOW, "target_date": "2025-08-20"})
    )[CHEQUING_REF]
    dates = [line["projected_date"] for line in _lines(entry)]

    assert dates
    assert min(dates) > "2025-06-20"
    assert max(dates) == "2025-08-20"


# --- 2. attribution ----------------------------------------------------------


def test_a_card_hosted_pattern_draws_down_the_card_and_never_the_chequing_account(
    service: LedgerMCPService,
) -> None:
    """A card hosted pattern draws down the card and never the chequing account."""
    entries = _by_ref(call_tool(service, "ledger.projection", _WINDOW))
    card = entries[CARD_REPORTED_REF]

    assert [line["merchant"] for line in _lines(card)] == [
        RECURRING_SUBSCRIPTION,
        RECURRING_SUBSCRIPTION,
    ]
    assert card["net_projected_flow"] == "-31.98"
    assert card["projected_balance"] == "-1281.98"
    assert RECURRING_SUBSCRIPTION not in {
        line["merchant"] for line in _lines(entries[CHEQUING_REF])
    }


def test_an_account_with_no_patterns_projects_its_anchor_unchanged(
    service: LedgerMCPService,
) -> None:
    entry = _by_ref(call_tool(service, "ledger.projection", _WINDOW))[CARD_UNCOMPUTED_REF]

    assert _lines(entry) == []
    assert entry["net_projected_flow"] == "0.00"
    assert entry["projected_balance"] == entry["anchor"]["balance"] == "-600.00"  # type: ignore[index]
    assert entry["pattern_count"] == 0


def test_the_recurring_account_key_is_composed_server_side_and_never_emitted(
    service: LedgerMCPService,
) -> None:
    """D5. `recurring_patterns.account_id` is an unsalted truncated hash of
    institution|slug, so the join happens in the query layer and the wire gains
    no recurring-to-account key: the only account identity on this surface is the
    same salted ref every other account-bearing tool emits."""
    result = call_tool(service, "ledger.projection", {**_WINDOW, "limit": 100})
    encoded = json.dumps(result, ensure_ascii=False)

    assert "account_id" not in encoded
    assert "pattern_id" not in encoded
    for private in PRIVATE_VALUES:
        assert private not in encoded, private


# --- 3. the refusal ----------------------------------------------------------


def test_an_account_with_only_a_relative_series_is_refused_with_a_reason(
    service: LedgerMCPService,
) -> None:
    """The relative series is seeded at zero on the account's first imported
    transaction. Projecting from it would not be a rougher answer, it would be a
    different quantity -- and the savings account has a pattern attributed to it,
    so the tempting move is exactly the wrong one."""
    entry = _by_ref(call_tool(service, "ledger.projection", _WINDOW))[SAVINGS_REF]

    assert entry["anchor"] == {
        "as_of_lag_days": None,
        "balance": None,
        "balance_basis": None,
        "balance_date": None,
        "status": "no_absolute_anchor",
    }
    assert entry["projected_balance"] is None
    assert entry["net_projected_flow"] is None
    assert _lines(entry) == []
    # The patterns are still counted: an empty list beside a zero count would say
    # this account has nothing recurring on it, which is false.
    assert entry["pattern_count"] == 1
    assert entry["pattern_disposition"] is None
    assert _data(call_tool(service, "ledger.projection", _WINDOW))["anchor_semantics"] == (
        BALANCE_LEVEL_STATEMENT
    )


def test_the_anchor_note_quotes_the_clause_it_disclaims_rather_than_placing_it() -> None:
    """The note pointed at BALANCE_LEVEL_STATEMENT's "final clause" as the one
    describing ledger.balances alone. The final clause is the sign convention,
    which governs both tools -- and governs `anchor.balance` and
    `projected_balance`, the only signed levels this surface serves, so a caller
    following the pointer would have read the sign rule as balances-only. Quoting
    the clause is what makes the pointer unable to drift off it."""
    description = str(
        _projection_data_schema()["properties"]["anchor_semantics"]["description"]  # type: ignore[index]
    )
    quoted = "only as an explicitly labelled change between two dates"

    assert quoted in description
    assert quoted in BALANCE_LEVEL_STATEMENT
    # The clause it must NOT be read as disclaiming is the last one, and the note
    # says so positively.
    assert BALANCE_LEVEL_STATEMENT.rstrip().endswith(
        "POSITIVE is money held, NEGATIVE is money owed."
    )
    assert "including the closing sign convention" in description
    assert "final clause" not in description


def test_an_as_of_before_every_anchor_names_the_other_absence(
    service: LedgerMCPService,
) -> None:
    """Two different absences, and the fixes differ: import a statement, or ask a
    later date."""
    data = _data(
        call_tool(service, "ledger.projection", {"as_of": "2025-06-01", "target_date": _TARGET})
    )
    entries = {entry["account_ref"]: entry for entry in data["entries"]}  # type: ignore[index,union-attr]

    assert entries[CHEQUING_REF]["anchor"]["status"] == "no_anchor_on_or_before_as_of"
    assert entries[SAVINGS_REF]["anchor"]["status"] == "no_absolute_anchor"
    assert data["anchor_counts"] == {
        "no_absolute_anchor": 1,
        "no_anchor_on_or_before_as_of": 4,
        "reported": 0,
    }


def test_anchor_staleness_passes_through_unmodified_and_is_never_suppressed(
    service: LedgerMCPService,
) -> None:
    entries = _by_ref(call_tool(service, "ledger.projection", _WINDOW))

    assert {
        ref: entry["anchor"]["as_of_lag_days"]  # type: ignore[index]
        for ref, entry in entries.items()
    } == {
        CHEQUING_REF: 10,
        SAVINGS_REF: None,
        CARD_REPORTED_REF: 10,
        CARD_UNCOMPUTED_REF: 12,
        CARD_UNLIMITED_REF: 11,
    }


# --- 4. roll-forward, overdue, and the omissions -----------------------------


def test_a_retrospective_expected_date_is_rolled_forward_and_says_so(
    service: LedgerMCPService,
) -> None:
    """`SYNTHETIC GYM` stores 2025-05-25, a month before the anchor. Serving that
    date would present a stored value as a projection; serving 2025-06-25 without
    the count would present a derived one as stored."""
    entry = _by_ref(call_tool(service, "ledger.projection", _WINDOW))[CHEQUING_REF]
    gym = [line for line in _lines(entry) if line["merchant"] == RECURRING_PAST_DUE]

    assert [(line["next_expected_date"], line["periods_from_next_expected"]) for line in gym] == [
        ("2025-05-25", 1),
        ("2025-05-25", 2),
        ("2025-05-25", 3),
    ]
    assert _data(call_tool(service, "ledger.projection", _WINDOW))["roll_forward_semantics"] == (
        PROJECTION_ROLL_FORWARD_STATEMENT
    )


def test_the_wire_separates_the_occurrence_offset_from_the_pattern_s_lateness(
    service: LedgerMCPService,
) -> None:
    """`periods_from_next_expected` counts OCCURRENCES and is signed;
    `next_expected_is_retrospective` is the lateness fact.

    Sorting or filtering by the offset to find stale patterns returns every
    far-future occurrence of a perfectly current one -- the income pattern below
    reaches offset 0 with its stored date still ahead of the request -- so the
    two have to be separate fields, and the second is a property of the PATTERN
    and identical on each of its lines.
    """
    # A later as_of, so the already-accepted 2025-07-08 occurrence is in the past
    # while its pattern's stored date, 2025-08-08, is still ahead of the request.
    entry = _by_ref(
        call_tool(service, "ledger.projection", {"as_of": "2025-07-15", "target_date": _TARGET})
    )[CHEQUING_REF]
    by_merchant: dict[str, list[dict[str, object]]] = {}
    for line in _lines(entry):
        by_merchant.setdefault(str(line["merchant"]), []).append(line)

    current = by_merchant[CHINESE_SOURCE]
    stale = by_merchant[RECURRING_PAST_DUE]
    assert current and stale
    assert [line["periods_from_next_expected"] for line in current] == [-1, 0]
    assert {line["next_expected_is_retrospective"] for line in current} == {False}
    assert {line["next_expected_is_retrospective"] for line in stale} == {True}
    # The negative offset names an occurrence the detector had already accepted,
    # so it is not reported as a missing payment even though its date has passed.
    assert current[0]["projected_date"] == "2025-07-08"
    assert current[0]["projected_date"] < "2025-07-15"
    assert current[0]["is_overdue"] is False


def test_a_past_due_pattern_is_counted_even_with_no_overdue_line_item(
    service: LedgerMCPService,
) -> None:
    """Occurrences at or before the anchor are outside the window, so a pattern
    that is genuinely late stops flagging any line item as soon as the anchor
    advances past its stored date -- the signal would decay exactly as the ledger
    got more current. The census is what carries it.

    `SYNTHETIC GYM` stores 2025-05-25 and is late in both readings below. Against
    the 2025-06-20 anchor its 2025-06-25 occurrence is in the window and flags;
    against the 2025-07-31 anchor -- the next statement the operator imports --
    the window opens in August and nothing of its flags at all.
    """
    late = _by_ref(call_tool(service, "ledger.projection", _WINDOW))[CHEQUING_REF]
    fresher = _by_ref(
        call_tool(
            service,
            "ledger.projection",
            {"as_of": "2025-08-05", "target_date": "2025-08-31"},
        )
    )[CHEQUING_REF]

    assert late["anchor"]["balance_date"] == "2025-06-20"  # type: ignore[index]
    assert fresher["anchor"]["balance_date"] == "2025-07-31"  # type: ignore[index]
    assert any(line["is_overdue"] for line in _lines(late))
    assert _lines(fresher)
    assert not any(line["is_overdue"] for line in _lines(fresher))
    assert late["patterns_past_due"] == 1
    assert fresher["patterns_past_due"] == 2


def test_overdue_is_derived_here_and_missed_count_is_reported_as_stored(
    service: LedgerMCPService,
) -> None:
    """`missed_count` floors, so a pattern less than one full period late stores
    0 -- indistinguishable from on time. The stored value is still reported."""
    entry = _by_ref(call_tool(service, "ledger.projection", _WINDOW))[CHEQUING_REF]
    elapsed = [line for line in _lines(entry) if line["is_overdue"]]

    assert [(line["merchant"], line["projected_date"]) for line in elapsed] == [
        (RECURRING_PAST_DUE, "2025-06-25")
    ]
    assert elapsed[0]["missed_count"] == 0
    assert _data(call_tool(service, "ledger.projection", _WINDOW))["missed_count_semantics"] == (
        PROJECTION_MISSED_COUNT_STATEMENT
    )


def test_a_transfer_typed_pattern_is_omitted_and_counted_rather_than_signed(
    service: LedgerMCPService,
) -> None:
    entry = _by_ref(call_tool(service, "ledger.projection", _WINDOW))[CHEQUING_REF]

    assert RECURRING_TRANSFER not in {line["merchant"] for line in _lines(entry)}
    assert RECURRING_LAPSED not in {line["merchant"] for line in _lines(entry)}
    assert entry["pattern_disposition"] == {
        "inactive": 1,
        "no_occurrence_in_window": 0,
        "projected": 3,
        "transfer_direction_ambiguous": 1,
        "unclassified_pattern_type": 0,
        "unsupported_frequency": 0,
    }


def test_the_disposition_accounts_for_every_pattern_on_the_account(
    service: LedgerMCPService,
) -> None:
    """A caller reading zero line items has to be able to tell "nothing recurs
    here" from "everything here was omitted"."""
    for entry in _by_ref(call_tool(service, "ledger.projection", _WINDOW)).values():
        disposition = entry["pattern_disposition"]
        if disposition is None:
            continue
        assert sum(disposition.values()) == entry["pattern_count"]  # type: ignore[union-attr]


# --- 5. the published statements ---------------------------------------------


def test_the_envelope_states_that_transfers_are_not_projected(
    service: LedgerMCPService,
) -> None:
    """Omitted transfers must be disclosed because they can dominate cash movement."""
    data = _data(call_tool(service, "ledger.projection", _WINDOW))
    description = {tool["name"]: str(tool["description"]) for tool in tool_schemas()}

    assert data["scope_semantics"] == PROJECTION_SCOPE_STATEMENT
    assert "are therefore NOT projected" in PROJECTION_SCOPE_STATEMENT
    assert "card payments and savings sweeps" in description["ledger.projection"]
    assert "inert" in description["ledger.projection"]


def test_the_envelope_repeats_the_subscription_fallthrough_caveat(
    service: LedgerMCPService,
) -> None:
    """Three of the six projected line items on this fixture are `subscription`,
    and the detector ends with `return "subscription"` for every recurring expense
    it could not classify."""
    data = _data(call_tool(service, "ledger.projection", _WINDOW))

    assert data["classification"] == {
        "fallthrough_default": "subscription",
        "statement": RECURRING_CLASSIFICATION_CAVEAT,
    }


def test_the_envelope_states_the_amount_and_reconciliation_rules(
    service: LedgerMCPService,
) -> None:
    data = _data(call_tool(service, "ledger.projection", _WINDOW))

    assert data["amount_semantics"] == PROJECTION_AMOUNT_STATEMENT
    assert data["reconciliation_semantics"] == PROJECTION_RECONCILIATION_STATEMENT
    assert data["account_ref_semantics"] == ACCOUNT_REF_SEMANTICS
    assert data["queried_as_of"] == _AS_OF
    assert data["queried_target_date"] == _TARGET
    assert data["projection_horizon_days"] == PROJECTION_HORIZON_DAYS


def test_the_server_instructions_describe_this_surface_as_it_behaves() -> None:
    """SERVER_INSTRUCTIONS is digest-NEUTRAL, so only a test can catch a false
    claim in it. Every sentence quoted here is quoted from the shipped string --
    with its hard wrapping collapsed, so the quote survives a reflow -- and each
    one is a claim the tests above measure."""
    published = " ".join(SERVER_INSTRUCTIONS.split())

    assert "ledger.projection" in published
    assert "Transfers between accounts are not projected" in published
    assert "projected_balance is the anchor plus every line item beside it, exactly" in published
    assert "the SIGNED periods between them" in published
    assert "is_overdue is derived from those dates and is an inference" in published
    assert "read patterns_past_due for that" in published
    assert (
        "Every occurrence a pattern was detected from posted to the one account it is "
        "attributed to" in published
    )
    assert "Attribution does not follow a merchant that moves between accounts" in published
    # The count of non-transaction surfaces was pinned here as a literal, which
    # made it P4's job to remember to edit a projection test when it added two
    # tools. It is now derived from the schemas in
    # `test_mcp_contract.py::test_the_instructions_count_the_surfaces_they_actually_describe`,
    # so what stays here is that the sentence exists at all.
    assert "surfaces answer what the transaction tools cannot" in published


# --- 6. the wire shape -------------------------------------------------------


def _data_schema(name: str) -> dict[str, object]:
    tool = next(tool for tool in tool_schemas() if tool["name"] == name)
    success = next(
        branch
        for branch in tool["outputSchema"]["oneOf"]  # type: ignore[index]
        if "data" in branch.get("properties", {})
    )
    data = success["properties"]["data"]  # type: ignore[index]
    if "oneOf" in data:
        # One tool serves several data shapes; merge their properties, because
        # the invariant below is about a key wherever it is served.
        merged: dict[str, object] = {"properties": {}}
        for variant in data["oneOf"]:
            merged["properties"].update(variant["properties"])  # type: ignore[union-attr]
        return merged
    return data  # type: ignore[return-value]


def _projection_data_schema() -> dict[str, object]:
    return _data_schema("ledger.projection")


def _money_schemas(node: object) -> list[dict[str, object]]:
    """Every money-shaped leaf in a schema, found by its pattern rather than by
    the field name it happens to sit under."""
    found: list[dict[str, object]] = []
    if isinstance(node, dict):
        pattern = node.get("pattern")
        if isinstance(pattern, str) and pattern.endswith(r"(?:0|[1-9]\d*)\.\d{2}$"):
            found.append(node)
        for value in node.values():
            found.extend(_money_schemas(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(_money_schemas(value))
    return found


def test_every_money_value_on_this_surface_is_the_signed_schema() -> None:
    """Derived from the published schema, not from a reading of the emitter. A
    negative reaching the UNSIGNED schema fails closed as corrupt data, and a
    positive-only projection of an account that owes money is a wrong answer."""
    schemas = _money_schemas(_projection_data_schema())

    assert schemas
    assert all(schema == SIGNED_MONEY_SCHEMA for schema in schemas), schemas


def test_a_negative_projection_is_served_rather_than_failing_the_schema(
    service: LedgerMCPService,
) -> None:
    entry = _by_ref(call_tool(service, "ledger.projection", _WINDOW))[CARD_REPORTED_REF]

    assert entry["projected_balance"].startswith("-")  # type: ignore[union-attr]


def test_the_page_block_states_what_paginates_and_the_census_does_not() -> None:
    """Lesson from P2: a block that repeats in full on every page multiplies
    every row by the page count for a caller that concatenates."""
    schema = _projection_data_schema()

    assert "every account" in str(schema["properties"]["anchor_counts"]["description"])  # type: ignore[index]
    assert "identical on every page" in str(
        schema["properties"]["anchor_counts"]["description"]  # type: ignore[index]
    )


def test_the_entries_paginate_over_accounts_by_a_stored_ordering(
    service: LedgerMCPService,
) -> None:
    walked: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        page = call_tool(
            service,
            "ledger.projection",
            {**_WINDOW, "limit": 2, **({"cursor": cursor} if cursor else {})},
        )
        walked.extend(entry["account_ref"] for entry in _data(page)["entries"])  # type: ignore[index,union-attr]
        cursor = page["structuredContent"]["page"]["next_cursor"]  # type: ignore[index]
        if not cursor:
            break

    assert cursor is None
    assert len(walked) == len(set(walked)) == 5
    assert walked[:2] == [CHEQUING_REF, SAVINGS_REF]


def test_the_cursor_binds_both_scope_inputs(service: LedgerMCPService) -> None:
    first = call_tool(service, "ledger.projection", {**_WINDOW, "limit": 1})
    cursor = first["structuredContent"]["page"]["next_cursor"]  # type: ignore[index]
    assert cursor

    for changed in ({"as_of": "2025-06-29"}, {"target_date": "2025-09-30"}):
        replayed = call_tool(
            service,
            "ledger.projection",
            {**_WINDOW, **changed, "limit": 1, "cursor": cursor},
        )

        assert replayed["isError"] is True, changed
        assert replayed["structuredContent"]["error"]["code"] == "invalid_cursor"  # type: ignore[index]


def test_the_accounts_are_withheld_when_the_salt_was_never_minted(tmp_path: Path) -> None:
    """Minting the salt is a WRITE and this server cannot write, so the entries
    fail closed rather than degrading to raw slugs."""
    db_path = build_synthetic_mcp_database(tmp_path / "unsalted.db", account_ref_salt=False)
    service = LedgerMCPService(db_path)
    try:
        data = _data(call_tool(service, "ledger.projection", _WINDOW))
    finally:
        service.close()

    assert data["entries"] == []
    assert data["account_refs"] == "withheld"
    assert data["account_refs_withheld_reason"] == "account_ref_salt_absent"
    # The census still describes the whole answer.
    assert data["entry_count"] == 5
    assert data["anchor_counts"]["reported"] == 4  # type: ignore[index]


# --- 7. the bounds and the routing -------------------------------------------


def test_a_target_date_past_the_published_horizon_is_refused(
    service: LedgerMCPService,
) -> None:
    result = call_tool(service, "ledger.projection", {"as_of": _AS_OF, "target_date": "2027-01-01"})

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "invalid_argument"  # type: ignore[index]


def test_a_target_date_before_the_as_of_is_refused(service: LedgerMCPService) -> None:
    result = call_tool(service, "ledger.projection", {"as_of": _AS_OF, "target_date": "2025-06-29"})

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "invalid_argument"  # type: ignore[index]


def test_the_tool_is_routed_by_its_own_branch_and_not_the_aggregate_fallthrough(
    service: LedgerMCPService,
) -> None:
    """`call_tool`'s final `else` serves the aggregate, so a missing `elif` does
    not fail loudly -- it silently asks for `start_date`, `end_date` and `flow`
    and refuses this call as malformed."""
    assert call_tool(service, "ledger.projection", _WINDOW).get("isError") is not True
    assert (
        call_tool(service, "ledger.projection", {**_WINDOW, "flow": "spending"})["isError"] is True
    )


def test_both_window_inputs_are_required(service: LedgerMCPService) -> None:
    for arguments in ({"as_of": _AS_OF}, {"target_date": _TARGET}, {}):
        result = call_tool(service, "ledger.projection", arguments)

        assert result["isError"] is True, arguments
        assert result["structuredContent"]["error"]["code"] == "invalid_argument"  # type: ignore[index]


def test_the_savings_pattern_is_visible_on_the_recurring_surface_it_belongs_to(
    service: LedgerMCPService,
) -> None:
    """The projection refuses the savings account, so this checks the pattern
    that produced no line item is not missing from the ledger's own account of
    itself."""
    recurring = _data(call_tool(service, "ledger.recurring", {"status": "any", "limit": 100}))

    assert RECURRING_SAVINGS_INTEREST in {
        entry["merchant"]  # type: ignore[index]
        for entry in recurring["entries"]  # type: ignore[union-attr]
    }


# --- 8. attribution, read off what the DETECTOR produced ----------------------
#
# Everything above this line runs against a fixture that writes
# `recurring_patterns.account_id` by hand, which proves that this surface JOINS
# on that column and proves nothing about what the column contains. The
# attribution claim is a claim about the PRODUCER, so the fixture below seeds
# transactions and lets `AnalysisRunner.run_pattern_detection()` -- the call PDF
# import finalize makes -- write the patterns.


@pytest.fixture
def detected_db(tmp_path: Path) -> Path:
    return build_detected_pattern_database(tmp_path / "detected.db")


@pytest.fixture
def detected_service(detected_db: Path) -> LedgerMCPService:
    built = LedgerMCPService(detected_db)
    yield built
    built.close()


def _detected_window() -> dict[str, str]:
    today = naive_utc_now().date()
    return {"as_of": today.isoformat(), "target_date": (today + timedelta(days=75)).isoformat()}


def _detected_ref(institution: str, number: str, account_type: str) -> str:
    return banking_account_ref(ACCOUNT_REF_SALT, institution, number, account_type)


def test_one_merchant_on_two_accounts_is_projected_onto_each_of_them(
    detected_service: LedgerMCPService,
) -> None:
    """One merchant on two accounts is projected onto each of them."""
    entries = _by_ref(call_tool(detected_service, "ledger.projection", _detected_window()))
    main = entries[_detected_ref(BANK_INSTITUTION, PAYROLL_MAIN_NUMBER, "chequing")]
    second = entries[_detected_ref(BANK_INSTITUTION, PAYROLL_SECOND_NUMBER, "chequing")]

    main_amounts = {
        line["amount"] for line in _lines(main) if line["merchant"] == SPLIT_PAYROLL_MERCHANT
    }
    second_amounts = {
        line["amount"] for line in _lines(second) if line["merchant"] == SPLIT_PAYROLL_MERCHANT
    }

    assert main_amounts == {PAYROLL_MAIN_AMOUNT}
    assert second_amounts == {PAYROLL_SECOND_AMOUNT}
    # The pooled mean is not a number either account's stream produces, so it must
    # not appear anywhere in the answer.
    encoded = json.dumps(
        call_tool(detected_service, "ledger.projection", {**_detected_window(), "limit": 100}),
        ensure_ascii=False,
    )
    assert POOLED_PAYROLL_MEAN not in encoded
    assert POOLED_MIGRATED_MEAN not in encoded


def test_a_migrated_charge_draws_down_the_card_that_hosts_it_and_not_the_one_it_left(
    detected_service: LedgerMCPService,
) -> None:
    """The synthetic subscription moves between two cards. Its projected charge
    belongs to the current card, while the old card's pattern remains visible
    with an inactive disposition and contributes no projected cash flow."""
    entries = _by_ref(call_tool(detected_service, "ledger.projection", _detected_window()))
    left = entries[_detected_ref(CARD_INSTITUTION, MIGRATED_FROM_NUMBER, "credit_card")]
    hosting = entries[_detected_ref(CARD_INSTITUTION, MIGRATED_TO_NUMBER, "credit_card")]

    assert {line["amount"] for line in _lines(hosting)} == {f"-{MIGRATED_TO_AMOUNT}"}
    assert [line["merchant"] for line in _lines(left)] == []
    # Counted, not silently absent: the account it left still HAS the pattern,
    # and the disposition says the detector has retired it.
    assert left["pattern_count"] == 1
    assert left["pattern_disposition"]["inactive"] == 1  # type: ignore[index]
    assert left["net_projected_flow"] == "0.00"
    assert left["projected_balance"] == left["anchor"]["balance"]  # type: ignore[index]


def test_the_envelope_states_what_attribution_does_and_does_not_follow(
    detected_service: LedgerMCPService,
) -> None:
    """The claim the wire used to make without qualification was "each pattern is
    attributed to its OWN account". True of the occurrences; false as a claim
    about the present, because nothing reattributes a merchant that moves."""
    data = _data(call_tool(detected_service, "ledger.projection", _detected_window()))
    description = {tool["name"]: str(tool["description"]) for tool in tool_schemas()}

    assert data["attribution_semantics"] == PROJECTION_ATTRIBUTION_STATEMENT
    assert "Attribution does not FOLLOW a merchant that moves" in PROJECTION_ATTRIBUTION_STATEMENT
    assert "never a blend of two accounts'" in PROJECTION_ATTRIBUTION_STATEMENT
    assert "attribution does not follow a merchant that MOVES" in description["ledger.projection"]


def test_the_line_items_walk_more_than_one_cadence(
    detected_service: LedgerMCPService,
) -> None:
    """Every pattern in the hand-written fixture is `monthly`, so nothing there
    exercises the other four cadences the roll-forward has to step."""
    entries = _by_ref(call_tool(detected_service, "ledger.projection", _detected_window()))
    frequencies = {line["frequency"] for entry in entries.values() for line in _lines(entry)}

    assert {"monthly", "biweekly", "weekly"} <= frequencies


def test_the_unknown_cadence_and_type_counters_are_reachable_on_the_wire(
    detected_service: LedgerMCPService,
) -> None:
    """`frequency` and `pattern_type` are unconstrained columns, so a row written
    by an older build or a different detection method can carry a value neither
    vocabulary names. Both counters were structurally zero on every fixture,
    which makes them indistinguishable from counters that cannot count."""
    entries = _by_ref(call_tool(detected_service, "ledger.projection", _detected_window()))
    second = entries[_detected_ref(BANK_INSTITUTION, PAYROLL_SECOND_NUMBER, "chequing")]
    disposition = second["pattern_disposition"]

    assert disposition["unsupported_frequency"] == 1  # type: ignore[index]
    assert disposition["unclassified_pattern_type"] == 1  # type: ignore[index]
    assert LEGACY_FREQUENCY_MERCHANT not in {line["merchant"] for line in _lines(second)}
    assert LEGACY_TYPE_MERCHANT not in {line["merchant"] for line in _lines(second)}
    assert sum(disposition.values()) == second["pattern_count"]  # type: ignore[union-attr]


def test_every_detected_account_still_reconciles_exactly(
    detected_service: LedgerMCPService,
) -> None:
    """The release gate again, on patterns the detector wrote rather than ones a
    fixture author chose."""
    entries = _by_ref(
        call_tool(detected_service, "ledger.projection", {**_detected_window(), "limit": 100})
    )

    assert len(entries) == 4
    for entry in entries.values():
        anchor = Decimal(entry["anchor"]["balance"])  # type: ignore[index,arg-type]
        flow = Decimal(entry["net_projected_flow"])  # type: ignore[arg-type]

        assert entry["status"] == "projected"
        assert flow == sum(
            (Decimal(line["amount"]) for line in _lines(entry)),  # type: ignore[arg-type]
            start=Decimal("0.00"),
        )
        assert anchor + flow == Decimal(entry["projected_balance"])  # type: ignore[arg-type]


def test_an_analysis_refresh_moves_the_patterns_and_leaves_the_anchors_alone(
    detected_db: Path,
) -> None:
    """The clause `PROJECTION_MISSED_COUNT_STATEMENT` used to get wrong.

    An earlier draft said the patterns and the anchors "are rebuilt by that one
    refresh, so they agree about what the ledger had seen", which was the entire
    justification for calling `is_overdue` a real absence. Running the pair that
    PDF import finalize runs shows otherwise: the pattern rows are re-stamped and
    the `absolute` balance rows are not touched at all, because the only writer of
    that basis is `apply_statement_balances` on the `repair statements` path.
    """
    runner = AnalysisRunner(detected_db)
    with session_scope(detected_db) as session:
        before_anchors = {
            (row.account_id, row.balance_date): row.computed_at
            for row in session.query(AccountBalanceDB).filter(
                AccountBalanceDB.balance_basis == "absolute"
            )
        }
        before_patterns = {
            row.pattern_id: row.detected_at for row in session.query(RecurringPatternDB)
        }
    assert before_anchors and before_patterns

    runner.run_account_analysis()
    runner.run_pattern_detection()

    with session_scope(detected_db) as session:
        after_anchors = {
            (row.account_id, row.balance_date): row.computed_at
            for row in session.query(AccountBalanceDB).filter(
                AccountBalanceDB.balance_basis == "absolute"
            )
        }
        after_patterns = {
            row.pattern_id: row.detected_at for row in session.query(RecurringPatternDB)
        }

    assert after_anchors == before_anchors
    assert after_patterns != before_patterns
    assert "the absolute anchors are written only by statement repair" in (
        PROJECTION_MISSED_COUNT_STATEMENT
    )
    assert "real absence" not in PROJECTION_MISSED_COUNT_STATEMENT


# --- 9. the per-account bound -------------------------------------------------


def test_an_account_whose_window_implies_too_many_flows_is_named_not_refused(
    service: LedgerMCPService,
) -> None:
    """`projection_horizon_days` bounds `(as_of, target]`, and the window that
    generates line items is `(anchor, target]` -- which the caller does not
    choose. A request at the narrowest legal window, `target_date == as_of` with
    `limit` 1, can therefore still exceed the per-account line-item cap on a
    stale enough anchor, and there is no input left to narrow.

    Refusing the whole call would abort a cursor walk mid-way over one account
    and take every other account's answer with it, so the offending account is
    NAMED and its figures are null.
    """
    far = {"as_of": "2041-01-01", "target_date": "2041-01-01", "limit": 100}
    entries = _by_ref(call_tool(service, "ledger.projection", far))
    chequing = entries[CHEQUING_REF]
    card = entries[CARD_REPORTED_REF]

    assert chequing["status"] == "window_exceeds_line_item_cap"
    assert chequing["line_items"] == []
    assert chequing["projected_balance"] is None
    assert chequing["net_projected_flow"] is None
    assert chequing["pattern_disposition"] is None
    assert chequing["patterns_past_due"] is None
    # The anchor itself is still reported -- it was never the problem -- and the
    # census counts the account under the anchor outcome it has.
    assert chequing["anchor"]["status"] == "reported"  # type: ignore[index]
    # Every other account is still answered.
    assert card["status"] == "projected"
    assert Decimal(card["anchor"]["balance"]) + Decimal(  # type: ignore[index,arg-type]
        card["net_projected_flow"]  # type: ignore[arg-type]
    ) == Decimal(card["projected_balance"])  # type: ignore[arg-type]
    data = _data(call_tool(service, "ledger.projection", far))
    assert data["max_line_items_per_account"] == DEFAULT_MAX_LINE_ITEMS
    assert "window_exceeds_line_item_cap" in PROJECTION_RECONCILIATION_STATEMENT


# --- 10. invariants derived across EVERY tool --------------------------------


_PAGINATED_TOOL_ARGUMENTS: dict[str, dict[str, object]] = {
    "ledger.planning.definitions": {"kind": "schedule"},
    "ledger.plan_reconciliation": {
        "start_date": "2025-07-01",
        "end_date": "2025-07-31",
        "as_of": "2025-08-10",
    },
    "ledger.aggregate": {
        "start_date": "2024-01-01",
        "end_date": "2026-12-31",
        "flow": "spending",
        "group_by": "category",
    },
    "ledger.transactions.search": {
        "start_date": "2024-01-01",
        "end_date": "2026-12-31",
        "flow": "any",
    },
    "ledger.coverage": {},
    "ledger.recurring": {"status": "any"},
    "ledger.transfers": {"start_date": "2024-01-01", "end_date": "2026-12-31"},
    "ledger.dimensions": {
        "start_date": "2024-01-01",
        "end_date": "2026-12-31",
        "flow": "spending",
        "dimension": "categories",
    },
    "ledger.balances": {"as_of": _AS_OF},
    "ledger.changes": {"since": "2000-01-01T00:00:00"},
    "ledger.projection": dict(_WINDOW),
    "ledger.insights": {},
    "ledger.quality": {},
    "ledger.net_worth": {"as_of": _AS_OF},
    "ledger.investments.statements": {"start_date": "2024-01-01", "end_date": "2026-12-31"},
    "ledger.investments.positions": {},
    "ledger.investments.activities": {},
}


@pytest.fixture
def cursor_walk_service(db_path: Path) -> LedgerMCPService:
    """Only the cross-tool walk needs several retained investment child rows."""
    with session_scope(db_path) as session:
        statement = (
            session.query(InvestmentStatementDB).filter_by(period_end=date(2025, 10, 31)).one()
        )
        for number, value in enumerate(("3", "3", "4.005"), start=1):
            session.add(
                InvestmentPositionDB(
                    statement_id=statement.statement_id,
                    source_row=number,
                    asset_category="Stocks",
                    currency="USD",
                    symbol=f"PAGE{number}",
                    discriminator="Summary",
                    quantity=Decimal("1"),
                    market_value=Decimal(value),
                )
            )
            session.add(
                InvestmentActivityDB(
                    statement_id=statement.statement_id,
                    activity_id=f"synthetic-cursor-activity-{number}",
                    source_row=number,
                    activity_type="dividend",
                    activity_date=date(2025, 10, 15),
                    currency="USD",
                    symbol=f"PAGE{number}",
                    amount=Decimal("0.1"),
                    direction="in",
                )
            )
    built = LedgerMCPService(db_path)
    yield built
    built.close()


def _walk_pages(service: LedgerMCPService, name: str) -> list[dict[str, object]]:
    pages: list[dict[str, object]] = []
    cursor: str | None = None
    selection = dict(_PAGINATED_TOOL_ARGUMENTS[name])
    if name in {"ledger.investments.positions", "ledger.investments.activities"}:
        discovery = _data(
            call_tool(
                service,
                "ledger.investments.statements",
                {"start_date": "2025-10-31", "end_date": "2025-10-31"},
            )
        )
        assert len(discovery["items"]) == 1  # type: ignore[arg-type]
        selection["statement_ref"] = discovery["items"][0]["statement_ref"]  # type: ignore[index]
    for _ in range(60):
        arguments = {
            **selection,
            # Vary page lengths so a page-local count cannot accidentally
            # masquerade as a repeated whole-result count merely because all
            # pages contain one row. The fixture has four definitions and
            # three confirmed occurrences, exercising both new page counts.
            "limit": 1 if not pages else 2,
            **({"cursor": cursor} if cursor else {}),
        }
        result = call_tool(service, name, arguments)
        assert result.get("isError") is not True, (name, result)
        pages.append(result["structuredContent"]["data"])  # type: ignore[index,arg-type]
        cursor = result["structuredContent"].get("page", {}).get("next_cursor")  # type: ignore[index,union-attr]
        if not cursor:
            return pages
    raise AssertionError(f"{name} did not terminate its cursor walk")


def _is_a_count(value: object) -> bool:
    """A whole number, or a block of them. The shape a caller ACCUMULATES."""
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    return (
        isinstance(value, dict)
        and bool(value)
        and all(isinstance(inner, int) and not isinstance(inner, bool) for inner in value.values())
    )


def _paginated_tools() -> list[dict[str, object]]:
    return [
        tool
        for tool in tool_schemas()
        if "cursor" in tool["inputSchema"]["properties"]  # type: ignore[index,operator]
    ]


def test_every_count_a_cursor_walk_repeats_declares_that_it_does_not_paginate(
    cursor_walk_service: LedgerMCPService,
) -> None:
    """Every count a cursor walk repeats declares that it does not paginate."""
    checked: list[tuple[str, str]] = []
    for tool in _paginated_tools():
        name = str(tool["name"])
        pages = _walk_pages(cursor_walk_service, name)
        assert len(pages) >= 2, f"{name} produced one page and proves nothing about repetition"
        properties = _data_schema(name)["properties"]  # type: ignore[index]
        for key, value in pages[0].items():
            if not _is_a_count(value):
                continue
            if all(page.get(key) == value for page in pages[1:]) is False:
                continue
            declared = properties[key]  # type: ignore[index]
            if "const" in declared:
                continue
            assert "identical on every page" in str(declared.get("description", "")), (name, key)
            checked.append((name, key))

    assert len(checked) >= 15, checked
    assert {"ledger.balances", "ledger.changes"} <= {name for name, _ in checked}
    assert ("ledger.balances", "level_counts") in checked
    assert ("ledger.changes", "total_added") in checked
    assert ("ledger.plan_reconciliation", "total_count") in checked
    assert ("ledger.plan_reconciliation", "hypothetical_occurrence_count") in checked
    assert ("ledger.plan_reconciliation", "detected_pattern_count") in checked
    assert ("ledger.plan_reconciliation", "incomplete_history_schedule_count") in checked
    assert ("ledger.planning.definitions", "matched_count") not in checked
    assert ("ledger.plan_reconciliation", "returned_count") not in checked
    for name in (
        "ledger.investments.statements",
        "ledger.investments.positions",
        "ledger.investments.activities",
    ):
        assert (name, "total_count") in checked


def test_a_merchant_on_two_accounts_is_two_recurring_entries_and_the_caveat_says_why(
    detected_service: LedgerMCPService,
) -> None:
    """A consequence of per-account attribution that `ledger.recurring` has to
    own: it names no account, so one merchant on two accounts now reaches it as
    two entries whose only visible difference is the amount. Summing them is
    right; reading them as one subscription billed twice is not, and the stored
    field statement is what separates the two."""
    data = _data(call_tool(detected_service, "ledger.recurring", {"status": "any", "limit": 100}))
    split = [
        entry
        for entry in data["entries"]  # type: ignore[union-attr]
        if entry["merchant"] == SPLIT_PAYROLL_MERCHANT  # type: ignore[index]
    ]

    assert len(split) == 2
    assert {entry["typical_amount"] for entry in split} == {  # type: ignore[index]
        PAYROLL_MAIN_AMOUNT,
        PAYROLL_SECOND_AMOUNT,
    }
    assert data["stored_field_semantics"] == RECURRING_STORED_FIELD_STATEMENT
    assert "posted to ONE account" in RECURRING_STORED_FIELD_STATEMENT
    assert "This surface names no account" in RECURRING_STORED_FIELD_STATEMENT
