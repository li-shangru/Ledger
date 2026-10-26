"""Account balance levels for the read-only Ledger MCP.

Two rules make this surface different from every other one. A LEVEL is served
only from the `absolute` basis, because the relative series is seeded at zero on
each account's first imported transaction -- presenting it as "your balance" is
not an approximation, it is a different quantity. And the money here is SIGNED:
a card owing $1,250 is `-1250.00`, which the unsigned money schema every other
tool uses would reject as corrupt data.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest
from mcp_synthetic import (
    ACCOUNT_REF_SALT,
    BANK_INSTITUTION,
    CARD_INSTITUTION,
    CARD_REPORTED_UTILIZATION,
    CARD_UNLIMITED_ESTIMATED_LIMIT,
    CARD_UNLIMITED_STALE_UTILIZATION,
    PRIVATE_ACCOUNT_NUMBER,
    PRIVATE_CARD_NUMBER,
    PRIVATE_SAVINGS_NUMBER,
    PRIVATE_UNCOMPUTED_CARD_NUMBER,
    PRIVATE_UNLIMITED_CARD_NUMBER,
    PRIVATE_VALUES,
    build_synthetic_mcp_database,
)

from ledger.mcp.contracts import (
    BALANCE_LEVEL_STATEMENT,
    CREDIT_UTILIZATION_STATEMENT,
    SIGNED_MONEY_SCHEMA,
    tool_schemas,
)
from ledger.mcp.server import call_tool
from ledger.mcp.service import LedgerMCPService
from ledger.query.account_refs import banking_account_ref
from ledger.query.agent import AgentQueryError

_AS_OF = "2025-12-31"
# Every level the RELATIVE series holds. None of these may ever appear in a
# balances envelope as a balance: the tool may report a relative CHANGE, never a
# relative level.
_RELATIVE_LEVELS = (
    "111.11",
    "250.05",
    "333.33",
    "-4242.42",
    "4242.42",
    "-808.08",
    "-606.06",
    "-707.07",
)


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
    return {entry["account_ref"]: entry for entry in _data(result)["entries"]}  # type: ignore[index,union-attr]


def _ref(institution: str, account_number: str, account_type: str) -> str:
    return banking_account_ref(ACCOUNT_REF_SALT, institution, account_number, account_type)


CHEQUING_REF = _ref(BANK_INSTITUTION, PRIVATE_ACCOUNT_NUMBER, "chequing")
SAVINGS_REF = _ref(BANK_INSTITUTION, PRIVATE_SAVINGS_NUMBER, "savings")
CARD_REPORTED_REF = _ref(CARD_INSTITUTION, PRIVATE_CARD_NUMBER, "credit_card")
CARD_UNCOMPUTED_REF = _ref(CARD_INSTITUTION, PRIVATE_UNCOMPUTED_CARD_NUMBER, "credit_card")
CARD_UNLIMITED_REF = _ref(CARD_INSTITUTION, PRIVATE_UNLIMITED_CARD_NUMBER, "credit_card")


# --- 1. a level comes from the absolute basis, or it is refused --------------


def test_balances_report_a_level_only_from_the_absolute_basis(
    service: LedgerMCPService,
) -> None:
    result = call_tool(service, "ledger.balances", {"as_of": _AS_OF})
    entry = _by_ref(result)[CHEQUING_REF]

    assert entry["institution"] == BANK_INSTITUTION
    assert entry["account_type"] == "chequing"
    assert entry["is_credit_card"] is False
    assert entry["level"] == {
        "as_of_lag_days": 153,
        "balance": "4800.00",
        "balance_basis": "absolute",
        "balance_date": "2025-07-31",
        "credit_utilization_pct": None,
        "credit_utilization_status": "not_a_credit_card",
        "status": "reported",
    }
    assert _data(result)["level_semantics"] == BALANCE_LEVEL_STATEMENT


def test_balances_refuse_a_level_for_an_account_that_has_only_a_relative_series(
    service: LedgerMCPService,
) -> None:
    """The refusal this tool exists for.

    The savings account has a complete relative series and no statement anchor.
    Reporting its last relative value as a balance would be affirmatively wrong:
    that series starts at zero on the first imported transaction, so it measures
    change since then and not what the account holds.
    """
    result = call_tool(service, "ledger.balances", {"as_of": _AS_OF})
    entry = _by_ref(result)[SAVINGS_REF]

    assert entry["level"] == {
        "as_of_lag_days": None,
        "balance": None,
        "balance_basis": None,
        "balance_date": None,
        "credit_utilization_pct": None,
        "credit_utilization_status": "no_level",
        "status": "no_absolute_anchor",
    }


def test_no_relative_level_reaches_the_wire_as_a_balance(service: LedgerMCPService) -> None:
    encoded = json.dumps(
        [
            call_tool(service, "ledger.balances", {"as_of": _AS_OF}),
            call_tool(
                service,
                "ledger.balances",
                {"as_of": _AS_OF, "change_since": "2025-06-10", "limit": 100},
            ),
        ],
        ensure_ascii=False,
    )

    for level in _RELATIVE_LEVELS:
        assert f'"{level}"' not in encoded, level


def test_balances_separate_a_missing_anchor_from_an_anchor_that_postdates_the_query(
    service: LedgerMCPService,
) -> None:
    """Two different answers that a single null would collapse into one."""
    early = call_tool(service, "ledger.balances", {"as_of": "2025-06-01", "limit": 100})
    entries = _by_ref(early)

    assert entries[CHEQUING_REF]["level"]["status"] == "no_anchor_on_or_before_as_of"  # type: ignore[index]
    assert entries[SAVINGS_REF]["level"]["status"] == "no_absolute_anchor"  # type: ignore[index]
    assert _data(early)["level_counts"] == {
        "no_absolute_anchor": 1,
        "no_anchor_on_or_before_as_of": 4,
        "reported": 0,
    }


def test_balances_count_every_account_by_what_its_level_could_be_answered_from(
    service: LedgerMCPService,
) -> None:
    data = _data(call_tool(service, "ledger.balances", {"as_of": _AS_OF, "limit": 100}))

    assert data["entry_count"] == 5
    assert data["level_counts"] == {
        "no_absolute_anchor": 1,
        "no_anchor_on_or_before_as_of": 0,
        "reported": 4,
    }
    assert data["queried_as_of"] == _AS_OF
    assert data["queried_change_since"] is None


def test_balances_report_how_stale_each_anchor_is(service: LedgerMCPService) -> None:
    """An anchor only exists inside a statement window, so its age is the answer's
    biggest caveat: a card paid off in June still reads as May's debt otherwise."""
    entries = _by_ref(call_tool(service, "ledger.balances", {"as_of": _AS_OF, "limit": 100}))

    assert entries[CARD_REPORTED_REF]["level"]["as_of_lag_days"] == 194  # type: ignore[index]
    assert entries[CARD_UNCOMPUTED_REF]["level"]["as_of_lag_days"] == 196  # type: ignore[index]
    near = _by_ref(call_tool(service, "ledger.balances", {"as_of": "2025-06-21", "limit": 100}))
    assert near[CARD_REPORTED_REF]["level"]["as_of_lag_days"] == 1  # type: ignore[index]


# --- 2. the relative series, only ever as a labelled change ------------------


def test_balances_report_the_relative_series_as_an_explicitly_labelled_change(
    service: LedgerMCPService,
) -> None:
    result = call_tool(
        service,
        "ledger.balances",
        {"as_of": _AS_OF, "change_since": "2025-06-02", "limit": 100},
    )
    entry = _by_ref(result)[CHEQUING_REF]

    assert entry["change"] == {
        # 111.11 on the day before the window opens, 333.33 at the close.
        "amount": "222.22",
        "balance_basis": "relative",
        "from_date": "2025-06-02",
        "observed_through": "2025-06-30",
        "status": "reported",
        "to_date": _AS_OF,
    }
    # The level is still the absolute one. Both bases in one entry, each named.
    assert entry["level"]["balance_basis"] == "absolute"  # type: ignore[index]
    assert _data(result)["queried_change_since"] == "2025-06-02"


def test_a_change_is_reported_for_an_account_whose_level_is_refused(
    service: LedgerMCPService,
) -> None:
    """Refusing the level does not refuse the movement: the difference between two
    points of the relative series is exact even though its zero is arbitrary."""
    entry = _by_ref(
        call_tool(
            service,
            "ledger.balances",
            {"as_of": _AS_OF, "change_since": "2025-06-10", "limit": 100},
        )
    )[SAVINGS_REF]

    assert entry["change"] == {
        "amount": "8484.84",
        "balance_basis": "relative",
        "from_date": "2025-06-10",
        "observed_through": "2025-06-25",
        "status": "reported",
        "to_date": _AS_OF,
    }
    assert entry["level"]["status"] == "no_absolute_anchor"  # type: ignore[index]


def test_a_change_with_no_observation_before_its_window_is_refused(
    service: LedgerMCPService,
) -> None:
    """Without a reading from before the window opens there is no opening level to
    subtract, and the series' seeded zero is not one."""
    entry = _by_ref(
        call_tool(
            service,
            "ledger.balances",
            {"as_of": _AS_OF, "change_since": "2025-06-02", "limit": 100},
        )
    )[SAVINGS_REF]

    assert entry["change"] == {
        "amount": None,
        "balance_basis": "relative",
        "from_date": "2025-06-02",
        "observed_through": "2025-06-25",
        "status": "no_opening_observation",
        "to_date": _AS_OF,
    }


def test_a_change_with_no_observation_inside_its_window_is_refused(
    service: LedgerMCPService,
) -> None:
    """The window closes before the card's series begins, so there is nothing to
    difference. Reported as its own status: a zero here would claim the card did
    not move, when the truth is that it was not yet observed."""
    entry = _by_ref(
        call_tool(
            service,
            "ledger.balances",
            {"as_of": "2025-06-01", "change_since": "2025-06-01", "limit": 100},
        )
    )[CARD_REPORTED_REF]

    assert entry["change"] == {
        "amount": None,
        "balance_basis": "relative",
        "from_date": "2025-06-01",
        "observed_through": None,
        "status": "no_closing_observation",
        "to_date": "2025-06-01",
    }


def test_no_change_block_is_emitted_unless_a_window_was_requested(
    service: LedgerMCPService,
) -> None:
    entries = _by_ref(call_tool(service, "ledger.balances", {"as_of": _AS_OF, "limit": 100}))

    assert all(entry["change"] is None for entry in entries.values())


# --- 3. credit utilization, only where it is real ---------------------------


def test_credit_utilization_is_reported_for_a_card_with_a_printed_limit(
    service: LedgerMCPService,
) -> None:
    entry = _by_ref(call_tool(service, "ledger.balances", {"as_of": _AS_OF, "limit": 100}))[
        CARD_REPORTED_REF
    ]

    assert entry["is_credit_card"] is True
    assert entry["level"]["balance"] == "-1250.00"  # type: ignore[index]
    assert entry["level"]["credit_utilization_pct"] == CARD_REPORTED_UTILIZATION  # type: ignore[index]
    assert entry["level"]["credit_utilization_status"] == "reported"  # type: ignore[index]


@pytest.mark.parametrize(
    ("ref", "status"),
    [
        (CHEQUING_REF, "not_a_credit_card"),
        (SAVINGS_REF, "no_level"),
        (CARD_UNCOMPUTED_REF, "not_computed"),
        (CARD_UNLIMITED_REF, "no_printed_credit_limit"),
    ],
)
def test_every_absent_credit_utilization_says_why(
    service: LedgerMCPService,
    ref: str,
    status: str,
) -> None:
    """A null percentage is not zero utilization, and the four ways it can be null
    are four different things to do about it."""
    entry = _by_ref(call_tool(service, "ledger.balances", {"as_of": _AS_OF, "limit": 100}))[ref]

    assert entry["level"]["credit_utilization_pct"] is None  # type: ignore[index]
    assert entry["level"]["credit_utilization_status"] == status  # type: ignore[index]


def test_credit_utilization_is_never_served_from_an_estimated_limit_or_a_stale_row(
    service: LedgerMCPService,
) -> None:
    """The unlimited card carries BOTH traps: an AccountDetector limit estimated
    from its deepest balance, and a percentage a previous run left behind. The
    estimate is a lower bound, so anything divided by it reads near 100% at the
    card's worst moment, and the stale percentage was computed from it."""
    result = call_tool(service, "ledger.balances", {"as_of": _AS_OF, "limit": 100})
    encoded = json.dumps(result, ensure_ascii=False)

    assert _by_ref(result)[CARD_UNLIMITED_REF]["level"]["credit_utilization_pct"] is None  # type: ignore[index]
    assert CARD_UNLIMITED_STALE_UTILIZATION not in encoded
    assert CARD_UNLIMITED_ESTIMATED_LIMIT not in encoded
    assert _data(result)["utilization_semantics"] == CREDIT_UTILIZATION_STATEMENT


# --- 4. signed money --------------------------------------------------------


def test_a_negative_level_survives_its_declared_output_schema(
    service: LedgerMCPService,
) -> None:
    """A signed amount against the unsigned money schema fails as `corrupt_data`
    rather than as anything a caller could act on, so the signed variant and the
    first signed emitter have to agree exactly."""
    schemas = {tool["name"]: tool["outputSchema"] for tool in tool_schemas()}
    result = call_tool(service, "ledger.balances", {"as_of": _AS_OF, "limit": 100})

    assert result.get("isError") is not True, result
    jsonschema.validate(result["structuredContent"], schemas["ledger.balances"])
    assert _by_ref(result)[CARD_REPORTED_REF]["level"]["balance"] == "-1250.00"  # type: ignore[index]


def test_the_signed_money_schema_did_not_leak_into_the_unsigned_aggregate(
    service: LedgerMCPService,
) -> None:
    """`ledger.aggregate` orders its groups with a string-length trick that inverts
    on negatives, so its money must stay unsigned. This pins that the signed
    variant added here reaches only the balances tool."""
    schemas = {tool["name"]: tool["outputSchema"] for tool in tool_schemas()}
    signed_group = {
        "schema": "ledger.mcp.v1",
        "instruction_trust": "inert",
        "data": {
            "excluded": {
                "any_reason": 0,
                "blank_merchant": 0,
                "candidate_rows": 0,
                "non_cad": 0,
                "not_in_cashflow": 0,
            },
            "flow": "spending",
            "group_by": "source",
            "group_count": 1,
            "groups": [{"amount": "-10.00", "key": "X", "transaction_count": 1}],
            "matching": {
                "matched_categories": [],
                "matched_institutions": [],
                "matched_sources": [],
                "semantics": "unicode_nfkc_whitespace_collapse_casefold_exact",
                "unmatched_categories": [],
                "unmatched_institutions": [],
                "unmatched_sources": [],
            },
            "queried_period": {"end": "2025-12-31", "start": "2025-01-01"},
            "total": "10.00",
            "transaction_count": 1,
        },
        "coverage": {
            "available_end": None,
            "available_start": None,
            "completeness": "not_asserted",
            "generation": "0" * 16,
            "relation": "not_queried",
            "requested_end": None,
            "requested_start": None,
            "scope": "imported_transaction_rows",
            "statement": (
                "Results cover imported Ledger transaction rows only; absence is not "
                "proof of real-world absence."
            ),
        },
        "page": {"has_more": False, "limit": 50, "next_cursor": None, "returned": 1},
        "policy": {
            "amount_basis": "absolute_spending",
            "currency": "CAD_native_only",
            "date_bounds": "inclusive",
            "exclude_from_spending": True,
            "flow": "spending",
            "income_treatment_exclusions": [],
            "requires_include_in_cashflow": True,
        },
    }

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(signed_group, schemas["ledger.aggregate"])


def test_a_negative_zero_is_never_emitted(service: LedgerMCPService) -> None:
    """`-0.00` is a real Decimal formatting, and it would claim a direction that a
    zero movement does not have. The schema refuses it, so the formatter must."""
    schemas = {tool["name"]: tool["outputSchema"] for tool in tool_schemas()}

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate("-0.00", SIGNED_MONEY_SCHEMA)
    # A window whose opening and closing would land on the SAME stale observation.
    # This used to report amount 0.00 with status "reported" — the most reassuring
    # possible reading of a window holding zero observations — so the formatter
    # never had a -0.00 to emit only by luck. The window is now refused outright,
    # with the stale observation's date on the wire.
    result = call_tool(
        service,
        "ledger.balances",
        {"as_of": "2025-06-20", "change_since": "2025-06-16", "limit": 100},
    )
    entry = _by_ref(result)[CHEQUING_REF]

    assert entry["change"] == {  # type: ignore[comparison-overlap]
        "amount": None,
        "balance_basis": "relative",
        "from_date": "2025-06-16",
        "observed_through": "2025-06-15",
        "status": "no_observation_in_window",
        "to_date": "2025-06-20",
    }
    jsonschema.validate(result["structuredContent"], schemas["ledger.balances"])


def test_balances_are_ordered_by_account_and_not_by_amount(
    service: LedgerMCPService,
) -> None:
    """Ordering on a signed magnitude is exactly the inversion the aggregate's
    string-length sort suffers, so this surface never orders on money at all."""
    entries = _data(call_tool(service, "ledger.balances", {"as_of": _AS_OF, "limit": 100}))[
        "entries"
    ]
    keys = [(entry["institution"], entry["account_type"]) for entry in entries]  # type: ignore[index,union-attr]

    assert keys == sorted(keys)
    assert entries[0]["account_ref"] == CHEQUING_REF  # type: ignore[index,call-overload]


# --- 5. opaque accounts, pagination, refusals -------------------------------


def test_balances_withhold_every_account_when_the_salt_was_never_minted(
    tmp_path: Path,
) -> None:
    """Read-only: it cannot mint the salt, so it must not fall back to slugs."""
    unsalted = build_synthetic_mcp_database(tmp_path / "unsalted.db", account_ref_salt=False)
    service = LedgerMCPService(unsalted)
    try:
        result = call_tool(service, "ledger.balances", {"as_of": _AS_OF})
    finally:
        service.close()
    data = _data(result)

    assert data["account_refs"] == "withheld"
    assert data["account_refs_withheld_reason"] == "account_ref_salt_absent"
    assert data["entries"] == []
    # The counts still answer "how many accounts can be levelled at all".
    assert data["entry_count"] == 5
    assert data["level_counts"]["reported"] == 4  # type: ignore[index]
    assert result["structuredContent"]["page"] == {  # type: ignore[index]
        "has_more": False,
        "limit": 50,
        "next_cursor": None,
        "returned": 0,
    }


def test_balances_paginate_over_a_stable_account_order(service: LedgerMCPService) -> None:
    everything = [
        entry["account_ref"]  # type: ignore[index]
        for entry in _data(call_tool(service, "ledger.balances", {"as_of": _AS_OF, "limit": 100}))[
            "entries"
        ]  # type: ignore[union-attr]
    ]
    first = call_tool(service, "ledger.balances", {"as_of": _AS_OF, "limit": 2})
    cursor = first["structuredContent"]["page"]["next_cursor"]  # type: ignore[index]
    assert cursor

    second = call_tool(service, "ledger.balances", {"as_of": _AS_OF, "limit": 2, "cursor": cursor})

    assert len(everything) == 5
    assert [entry["account_ref"] for entry in _data(first)["entries"]] == everything[:2]  # type: ignore[index,union-attr]
    assert [entry["account_ref"] for entry in _data(second)["entries"]] == everything[2:4]  # type: ignore[index,union-attr]
    assert _data(second)["entry_count"] == 5


@pytest.mark.parametrize(
    "rebound",
    [
        {"as_of": "2025-11-30"},
        {"change_since": "2025-06-02"},
    ],
)
def test_every_balance_input_is_bound_into_the_cursor(
    service: LedgerMCPService,
    rebound: dict[str, object],
) -> None:
    """An unbound input renumbers the result set behind a cursor that still
    validates, which skips rows and repeats others inside an ordinary success."""
    first = call_tool(service, "ledger.balances", {"as_of": _AS_OF, "limit": 2})
    cursor = first["structuredContent"]["page"]["next_cursor"]  # type: ignore[index]
    assert cursor

    replayed = call_tool(
        service,
        "ledger.balances",
        {"as_of": _AS_OF, "limit": 2, "cursor": cursor, **rebound},
    )

    assert replayed["isError"] is True
    assert replayed["structuredContent"]["error"]["code"] == "invalid_cursor"  # type: ignore[index]


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"as_of": "2025-13-01"},
        {"as_of": "2025-6-1"},
        {"as_of": "not-a-date"},
        {"as_of": _AS_OF, "change_since": "2026-01-01"},
        {"as_of": _AS_OF, "change_since": "bad"},
        {"as_of": _AS_OF, "account_slug": "synthetic-private-slug-0001"},
        {"as_of": _AS_OF, "balance_basis": "relative"},
        {"as_of": _AS_OF, "limit": 0},
    ],
)
def test_balances_refuse_a_malformed_or_unknown_argument(
    service: LedgerMCPService,
    arguments: dict[str, object],
) -> None:
    result = call_tool(service, "ledger.balances", arguments)

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "invalid_argument"  # type: ignore[index]


def test_the_query_layer_refuses_a_non_date_as_of(service: LedgerMCPService) -> None:
    """The wire adapter parses dates before the query sees them, so this guard is
    for the CLI and library callers that reach `AgentQueries` directly."""
    with pytest.raises(AgentQueryError):
        service.query.agent.account_balances(as_of="2025-12-31")  # type: ignore[arg-type]


def test_balances_refuse_a_database_with_more_accounts_than_the_contract_serves(
    service: LedgerMCPService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The level counts cover every account, so the account table is the scan.
    Past the cap it fails closed with a named code rather than issuing thousands
    of per-account lookups behind one call."""
    monkeypatch.setattr("ledger.query.agent._MAX_BALANCE_ACCOUNTS", 1)

    result = call_tool(service, "ledger.balances", {"as_of": _AS_OF})

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "filter_too_broad"  # type: ignore[index]


def test_balances_emit_no_raw_slug_number_path_or_description(
    service: LedgerMCPService,
) -> None:
    encoded = json.dumps(
        [
            call_tool(service, "ledger.balances", {"as_of": _AS_OF, "limit": 100}),
            call_tool(
                service,
                "ledger.balances",
                {"as_of": _AS_OF, "change_since": "2025-06-10", "limit": 100},
            ),
        ],
        ensure_ascii=False,
    )

    for private in PRIVATE_VALUES:
        assert private not in encoded, private


def test_balances_leave_the_database_and_sidecars_byte_identical(db_path: Path) -> None:
    def artifacts() -> dict[str, tuple[bytes, int]]:
        found: dict[str, tuple[bytes, int]] = {}
        for suffix in ("", "-journal", "-wal", "-shm"):
            candidate = Path(f"{db_path}{suffix}")
            if candidate.exists():
                found[suffix] = (candidate.read_bytes(), candidate.stat().st_mtime_ns)
        return found

    before = artifacts()
    service = LedgerMCPService(db_path)
    try:
        call_tool(service, "ledger.balances", {"as_of": _AS_OF, "change_since": "2025-06-02"})
    finally:
        service.close()

    assert artifacts() == before
