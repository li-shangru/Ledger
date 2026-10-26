"""Statement coverage, recurring patterns, transfers, and dimension discovery.

Each of these reads a table the transaction tools cannot answer from, and each
carries a disclosure the answer is wrong without: which account-months were
imported at all, that `subscription` is a fallthrough label, how much of the
transfer set has a known counterparty, and what a filter value must be spelled as.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from mcp_synthetic import (
    ACCOUNT_REF_SALT,
    BANK_INSTITUTION,
    CARD_INSTITUTION,
    CHINESE_SOURCE,
    PRIVATE_ACCOUNT_NUMBER,
    PRIVATE_CARD_NUMBER,
    PRIVATE_VALUES,
    RECURRING_BILL,
    RECURRING_LAPSED,
    RECURRING_PAST_DUE,
    RECURRING_SAVINGS_INTEREST,
    RECURRING_SUBSCRIPTION,
    RECURRING_TRANSFER,
    build_synthetic_mcp_database,
)

from ledger.mcp.contracts import (
    ACCOUNT_REF_SEMANTICS,
    COVERAGE_ABSENCE_STATEMENT,
    RECURRING_CLASSIFICATION_CAVEAT,
    TRANSFER_DIRECTION_STATEMENT,
    tool_schemas,
)
from ledger.mcp.server import call_tool
from ledger.mcp.service import LedgerMCPService
from ledger.query.account_refs import banking_account_ref
from ledger.storage.database import StatementCoverageDB, session_scope

_PERIOD = {"start_date": "2025-01-01", "end_date": "2025-12-31"}


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


# Derived the way the ONE resolver derives it -- from the `accounts` row, never
# from the coverage row's parser-supplied `statement_type`. Deriving the expected
# value the way the code under test happens to derive it is what let coverage and
# balances drift into two namespaces without a failing test.
def _chequing_ref() -> str:
    return banking_account_ref(
        ACCOUNT_REF_SALT, BANK_INSTITUTION, PRIVATE_ACCOUNT_NUMBER, "chequing"
    )


def _card_ref() -> str:
    return banking_account_ref(
        ACCOUNT_REF_SALT, CARD_INSTITUTION, PRIVATE_CARD_NUMBER, "credit_card"
    )


# --- 1. ledger.coverage -----------------------------------------------------


def test_coverage_reports_each_account_month_behind_an_opaque_salted_ref(
    service: LedgerMCPService,
) -> None:
    data = _data(call_tool(service, "ledger.coverage", {}))

    assert data["account_refs"] == "disclosed"
    assert data["entry_count"] == 5
    assert [(entry["period"], entry["account_ref"]) for entry in data["entries"]] == [  # type: ignore[index]
        ("2025-01", _chequing_ref()),
        ("2025-01", _card_ref()),
        ("2025-02", _chequing_ref()),
        ("2025-03", _card_ref()),
        ("2025-04", _chequing_ref()),
    ]
    assert data["observed_periods"] == {"start": "2025-01", "end": "2025-04"}


def test_coverage_separates_imported_with_no_activity_from_never_imported(
    service: LedgerMCPService,
) -> None:
    """The entire point of the table: an empty month is not a missing month."""
    data = _data(call_tool(service, "ledger.coverage", {}))
    by_period = {
        (entry["period"], entry["account_ref"]): entry  # type: ignore[index]
        for entry in data["entries"]  # type: ignore[union-attr]
    }

    assert by_period[("2025-02", _chequing_ref())]["status"] == "imported_no_activity"
    assert by_period[("2025-02", _chequing_ref())]["row_count"] == 0
    assert by_period[("2025-01", _chequing_ref())]["status"] == "imported_with_activity"
    assert by_period[("2025-03", _card_ref())]["status"] == "imported_no_rows_unexplained"
    # 2025-03 was never imported for the chequing account, and absence is the
    # only way the table can say so.
    assert ("2025-03", _chequing_ref()) not in by_period
    assert data["absence_semantics"] == COVERAGE_ABSENCE_STATEMENT
    assert data["status_counts"] == {
        "imported_no_activity": 1,
        "imported_no_rows_unexplained": 1,
        "imported_with_activity": 3,
    }


def test_coverage_states_what_an_unresolved_account_ref_means_as_a_data_block_const(
    db_path: Path,
) -> None:
    """The two-namespace rule must reach the wire, not just the prose.

    Every comparable rule on this surface is a schema `const` backed by a module
    constant, so a caller reads it from the envelope and the contract digest moves
    if it changes. The rule that an `unresolved-` ref is never joinable to an
    `acct-` one arrived in docstrings only -- and a caller that assumes otherwise
    joins two unrelated accounts.
    """
    coverage_schema = next(
        tool["outputSchema"] for tool in tool_schemas() if tool["name"] == "ledger.coverage"
    )
    # The success branch of the success/error `oneOf`.
    success = next(
        branch for branch in coverage_schema["oneOf"] if "data" in branch.get("properties", {})
    )
    data_schema = success["properties"]["data"]
    assert data_schema["properties"]["account_ref_semantics"] == {"const": ACCOUNT_REF_SEMANTICS}
    assert "account_ref_semantics" in data_schema["required"]

    # And it is actually emitted, not merely declared.
    service = LedgerMCPService(db_path)
    try:
        data = _data(call_tool(service, "ledger.coverage", {}))
    finally:
        service.close()
    assert data["account_ref_semantics"] == ACCOUNT_REF_SEMANTICS
    assert "never be treated as joinable" in ACCOUNT_REF_SEMANTICS
    assert "two different accounts never share one" in ACCOUNT_REF_SEMANTICS


def test_an_unjoinable_coverage_row_validates_against_the_declared_schema(
    db_path: Path,
) -> None:
    """The sentinel branch is the one no conformance test had ever reached.

    The `unresolved-` namespace and `account_ref_status: "no_account_record"` are
    a schema branch of their own, and the fixture's coverage rows all resolve, so
    the envelope validated only ever exercised the `acct-` side.
    """
    import jsonschema

    with session_scope(db_path) as session:
        session.add(
            StatementCoverageDB(
                account_slug="synthetic-private-number-0000",
                period="2025-05",
                institution=BANK_INSTITUTION,
                statement_type="synthetic_deposit",
                row_count=2,
                statement_count=1,
                skipped_reason="",
                imported_at=datetime(2030, 1, 1, tzinfo=UTC).replace(tzinfo=None),
            )
        )
    schema = next(
        tool["outputSchema"] for tool in tool_schemas() if tool["name"] == "ledger.coverage"
    )
    service = LedgerMCPService(db_path)
    try:
        envelope = call_tool(service, "ledger.coverage", {})
    finally:
        service.close()

    entries = envelope["structuredContent"]["data"]["entries"]  # type: ignore[index]
    unjoinable = [entry for entry in entries if entry["account_ref_status"] == "no_account_record"]
    assert len(unjoinable) == 1, entries
    # A string in the sentinel namespace, so the `oneOf` branch the fixture never
    # exercised is the one this envelope is validated against.
    assert re.fullmatch(r"unresolved-[0-9a-f]{10}", str(unjoinable[0]["account_ref"])), unjoinable
    jsonschema.validate(envelope["structuredContent"], schema)


def test_coverage_bounds_the_reported_periods(service: LedgerMCPService) -> None:
    data = _data(
        call_tool(service, "ledger.coverage", {"start_period": "2025-02", "end_period": "2025-03"})
    )

    assert [entry["period"] for entry in data["entries"]] == ["2025-02", "2025-03"]  # type: ignore[index]
    assert data["entry_count"] == 2
    assert data["queried_periods"] == {"start": "2025-02", "end": "2025-03"}
    # The observed range describes the whole table, so a caller can see that
    # coverage exists outside the window it asked about.
    assert data["observed_periods"] == {"start": "2025-01", "end": "2025-04"}


def test_coverage_withholds_every_account_ref_when_the_salt_was_never_minted(
    tmp_path: Path,
) -> None:
    """Read-only: it cannot mint the salt, so it must not fall back to slugs."""
    unsalted = build_synthetic_mcp_database(tmp_path / "unsalted.db", account_ref_salt=False)
    service = LedgerMCPService(unsalted)
    try:
        result = call_tool(service, "ledger.coverage", {})
    finally:
        service.close()
    data = _data(result)

    assert data["account_refs"] == "withheld"
    assert data["account_refs_withheld_reason"] == "account_ref_salt_absent"
    assert data["entries"] == []
    # The counts still answer "how much is imported" without naming an account.
    assert data["entry_count"] == 5
    assert data["status_counts"]["imported_with_activity"] == 3  # type: ignore[index]
    assert PRIVATE_ACCOUNT_NUMBER not in json.dumps(result, ensure_ascii=False)
    # No next page is offered, because no entry will ever be served: a cursor
    # here would page a caller through nothing forever.
    assert result["structuredContent"]["page"] == {  # type: ignore[index]
        "has_more": False,
        "limit": 50,
        "next_cursor": None,
        "returned": 0,
    }


def test_coverage_paginates_and_binds_its_period_bounds_into_the_cursor(
    service: LedgerMCPService,
) -> None:
    first = call_tool(service, "ledger.coverage", {"limit": 2})
    cursor = first["structuredContent"]["page"]["next_cursor"]  # type: ignore[index]
    assert cursor

    second = call_tool(service, "ledger.coverage", {"limit": 2, "cursor": cursor})
    rebound = call_tool(
        service,
        "ledger.coverage",
        {"limit": 2, "cursor": cursor, "start_period": "2025-02"},
    )

    assert [entry["period"] for entry in _data(second)["entries"]] == ["2025-02", "2025-03"]  # type: ignore[index]
    assert rebound["isError"] is True
    assert rebound["structuredContent"]["error"]["code"] == "invalid_cursor"  # type: ignore[index]


@pytest.mark.parametrize(
    "arguments",
    [
        {"start_period": "2025-13"},
        {"start_period": "2025-1"},
        {"end_period": "not-a-period"},
        {"start_period": "2025-05", "end_period": "2025-01"},
        {"account_slug": "synthetic-private-slug-0001"},
    ],
)
def test_coverage_refuses_a_malformed_or_unknown_argument(
    service: LedgerMCPService,
    arguments: dict[str, object],
) -> None:
    result = call_tool(service, "ledger.coverage", arguments)

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "invalid_argument"  # type: ignore[index]


# --- 2. ledger.recurring ----------------------------------------------------


def test_recurring_discloses_that_subscription_is_the_fallthrough_default(
    service: LedgerMCPService,
) -> None:
    data = _data(call_tool(service, "ledger.recurring", {}))
    description = {tool["name"]: str(tool["description"]) for tool in tool_schemas()}

    assert data["classification"] == {
        "fallthrough_default": "subscription",
        "statement": RECURRING_CLASSIFICATION_CAVEAT,
    }
    assert "fallthrough" in description["ledger.recurring"]
    assert "subscription" in description["ledger.recurring"]


def test_recurring_returns_active_patterns_by_next_expected_date(
    service: LedgerMCPService,
) -> None:
    data = _data(call_tool(service, "ledger.recurring", {}))

    assert data["status"] == "active"
    assert [pattern["merchant"] for pattern in data["entries"]] == [  # type: ignore[index]
        RECURRING_PAST_DUE,
        RECURRING_SAVINGS_INTEREST,
        RECURRING_TRANSFER,
        RECURRING_SUBSCRIPTION,
        RECURRING_BILL,
        CHINESE_SOURCE,
    ]
    # The card-hosted pattern, which this surface reports without any account
    # identity at all: `recurring_patterns.account_id` is an unsalted truncated
    # hash, so the account it belongs to is composed server-side by
    # ledger.projection and never emitted here.
    assert data["entries"][3] == {  # type: ignore[index]
        "category": "Entertainment",
        "frequency": "monthly",
        "institution": CARD_INSTITUTION,
        "is_active": True,
        "merchant": RECURRING_SUBSCRIPTION,
        "missed_count": 0,
        "next_expected_date": "2025-07-15",
        "pattern_type": "subscription",
        "typical_amount": "15.99",
        "updated_at": "2030-01-01T00:00:00",
    }


def test_recurring_can_report_the_lapsed_patterns_it_hides_by_default(
    service: LedgerMCPService,
) -> None:
    inactive = _data(call_tool(service, "ledger.recurring", {"status": "inactive"}))
    everything = _data(call_tool(service, "ledger.recurring", {"status": "any"}))

    assert [pattern["merchant"] for pattern in inactive["entries"]] == [RECURRING_LAPSED]  # type: ignore[index]
    assert inactive["entries"][0]["missed_count"] == 3  # type: ignore[index]
    assert inactive["entries"][0]["is_active"] is False  # type: ignore[index]
    assert everything["entry_count"] == 7


def test_recurring_binds_status_into_the_cursor(service: LedgerMCPService) -> None:
    first = call_tool(service, "ledger.recurring", {"status": "any", "limit": 1})
    cursor = first["structuredContent"]["page"]["next_cursor"]  # type: ignore[index]
    assert cursor

    replayed = call_tool(
        service,
        "ledger.recurring",
        {"status": "active", "limit": 1, "cursor": cursor},
    )

    assert replayed["isError"] is True
    assert replayed["structuredContent"]["error"]["code"] == "invalid_cursor"  # type: ignore[index]


def test_recurring_never_emits_the_unsalted_account_hash(service: LedgerMCPService) -> None:
    """`recurring_patterns.account_id` is the low-entropy construction the export
    layer deprecated as brute-forceable, so it stays out of the envelope."""
    result = call_tool(service, "ledger.recurring", {"status": "any", "limit": 100})
    encoded = json.dumps(result, ensure_ascii=False)

    assert "account_id" not in encoded
    for pattern in _data(result)["entries"]:  # type: ignore[union-attr]
        assert set(pattern) == {
            "category",
            "frequency",
            "institution",
            "is_active",
            "merchant",
            "missed_count",
            "next_expected_date",
            "pattern_type",
            "typical_amount",
            "updated_at",
        }


# --- 3. ledger.transfers ----------------------------------------------------


def test_transfers_aggregate_by_counterparty_institution_with_unsigned_magnitudes(
    service: LedgerMCPService,
) -> None:
    data = _data(call_tool(service, "ledger.transfers", _PERIOD))

    assert data["groups"] == [
        {
            "amount": "250.00",
            "counterparty_institution": CARD_INSTITUTION,
            "direction": "outbound",
            "institution": BANK_INSTITUTION,
            "transfer_count": 1,
        },
        {
            "amount": "250.00",
            "counterparty_institution": BANK_INSTITUTION,
            "direction": "inbound",
            "institution": CARD_INSTITUTION,
            "transfer_count": 1,
        },
        {
            "amount": "75.00",
            "counterparty_institution": None,
            "direction": "inbound",
            "institution": BANK_INSTITUTION,
            "transfer_count": 1,
        },
    ]
    assert data["total"] == "575.00"
    assert data["transfer_count"] == 3
    assert data["direction_semantics"] == TRANSFER_DIRECTION_STATEMENT


def test_transfers_state_their_own_pairing_and_currency_coverage(
    service: LedgerMCPService,
) -> None:
    data = _data(call_tool(service, "ledger.transfers", _PERIOD))

    assert data["pairing"] == {
        "legs": 3,
        "paired_legs": 2,
        "with_counterparty_institution": 2,
    }
    assert data["excluded"] == {"candidate_rows": 4, "non_cad": 1}


def test_transfers_never_emit_a_signed_amount(service: LedgerMCPService) -> None:
    """A signed value fails the unsigned money schema as corrupt_data, and the
    group ordering inverts on negatives, so nothing signed may reach either."""
    result = call_tool(service, "ledger.transfers", _PERIOD)
    data = _data(result)

    amounts = [str(group["amount"]) for group in data["groups"]] + [str(data["total"])]  # type: ignore[index,union-attr]
    assert all(not amount.startswith("-") for amount in amounts), amounts
    assert result.get("isError") is not True


def test_transfers_paginate_within_one_generation(service: LedgerMCPService) -> None:
    first = call_tool(service, "ledger.transfers", {**_PERIOD, "limit": 1})
    cursor = first["structuredContent"]["page"]["next_cursor"]  # type: ignore[index]
    assert cursor

    second = call_tool(service, "ledger.transfers", {**_PERIOD, "limit": 1, "cursor": cursor})
    rebound = call_tool(
        service,
        "ledger.transfers",
        {**_PERIOD, "end_date": "2025-06-30", "limit": 1, "cursor": cursor},
    )

    assert _data(second)["groups"][0]["institution"] == CARD_INSTITUTION  # type: ignore[index]
    assert rebound["isError"] is True
    assert rebound["structuredContent"]["error"]["code"] == "invalid_cursor"  # type: ignore[index]


def test_transfers_bind_the_start_of_the_period_into_the_cursor(
    service: LedgerMCPService,
) -> None:
    first = call_tool(service, "ledger.transfers", {**_PERIOD, "limit": 1})
    cursor = first["structuredContent"]["page"]["next_cursor"]  # type: ignore[index]
    assert cursor

    replayed = call_tool(
        service,
        "ledger.transfers",
        {**_PERIOD, "start_date": "2025-06-01", "limit": 1, "cursor": cursor},
    )

    assert replayed["isError"] is True
    assert replayed["structuredContent"]["error"]["code"] == "invalid_cursor"  # type: ignore[index]


def test_transfers_report_an_empty_period_as_zero_matching_rows(
    service: LedgerMCPService,
) -> None:
    result = call_tool(
        service,
        "ledger.transfers",
        {"start_date": "2025-09-01", "end_date": "2025-09-02"},
    )

    assert _data(result)["groups"] == []
    assert _data(result)["total"] == "0.00"
    assert result["structuredContent"]["coverage"]["relation"] == (  # type: ignore[index]
        "within_available_range_no_matching_rows"
    )


# --- 4. ledger.dimensions ---------------------------------------------------


def test_dimensions_lists_the_stored_spelling_of_every_filter_value(
    service: LedgerMCPService,
) -> None:
    data = _data(
        call_tool(
            service,
            "ledger.dimensions",
            {**_PERIOD, "dimension": "sources", "flow": "spending"},
        )
    )

    assert data["values"] == [
        {"transaction_count": 2, "value": "SYNTHETIC MARKET"},
        {"transaction_count": 1, "value": "星河书店"},
    ]
    assert data["value_count"] == 2
    assert data["dimension"] == "sources"


def test_dimensions_matches_a_substring_under_the_same_normalization_as_a_filter(
    service: LedgerMCPService,
) -> None:
    """`contains` is what removes the guess-then-page loop exact matching creates."""
    data = _data(
        call_tool(
            service,
            "ledger.dimensions",
            {
                **_PERIOD,
                "dimension": "sources",
                "flow": "income",
                "contains": "  northstar   UNIVERSITY  ",
            },
        )
    )

    assert [value["value"] for value in data["values"]] == ["NORTHSTAR UNIVERSITY"]  # type: ignore[index]
    assert data["contains"] == "  northstar   UNIVERSITY  "
    assert "substring" in str(data["match_semantics"])


def test_dimensions_covers_categories_and_institutions_too(
    service: LedgerMCPService,
) -> None:
    categories = _data(
        call_tool(
            service,
            "ledger.dimensions",
            {**_PERIOD, "dimension": "categories", "flow": "spending"},
        )
    )
    institutions = _data(
        call_tool(
            service,
            "ledger.dimensions",
            {**_PERIOD, "dimension": "institutions", "flow": "spending", "contains": "合成"},
        )
    )

    assert [value["value"] for value in categories["values"]] == ["Groceries", "Books"]  # type: ignore[index]
    assert [value["value"] for value in institutions["values"]] == ["合成银行"]  # type: ignore[index]


def test_dimensions_counts_only_the_rows_the_same_flow_policy_would_total(
    service: LedgerMCPService,
) -> None:
    """A discovered value must be one an aggregate can actually reproduce."""
    dimensions = _data(
        call_tool(
            service,
            "ledger.dimensions",
            {**_PERIOD, "dimension": "sources", "flow": "spending", "contains": "market"},
        )
    )
    aggregated = _data(
        call_tool(
            service,
            "ledger.aggregate",
            {
                **_PERIOD,
                "flow": "spending",
                "group_by": "source",
                "filters": {"sources": ["SYNTHETIC MARKET"]},
            },
        )
    )

    assert dimensions["values"] == [{"transaction_count": 2, "value": "SYNTHETIC MARKET"}]
    assert aggregated["transaction_count"] == 2


def test_dimensions_reports_the_policy_its_counts_were_taken_under(
    service: LedgerMCPService,
) -> None:
    """The counts are policy-filtered, so the policy travels with them."""
    envelope = call_tool(
        service,
        "ledger.dimensions",
        {**_PERIOD, "dimension": "sources", "flow": "spending"},
    )["structuredContent"]

    assert envelope["policy"]["currency"] == "CAD_native_only"  # type: ignore[index]
    assert envelope["policy"]["flow"] == "spending"  # type: ignore[index]
    assert envelope["policy"]["amount_basis"] == "absolute_spending"  # type: ignore[index]


def test_dimensions_binds_dimension_and_contains_into_the_cursor(
    service: LedgerMCPService,
) -> None:
    first = call_tool(
        service,
        "ledger.dimensions",
        {**_PERIOD, "dimension": "sources", "flow": "spending", "limit": 1},
    )
    cursor = first["structuredContent"]["page"]["next_cursor"]  # type: ignore[index]
    assert cursor

    changed_dimension = call_tool(
        service,
        "ledger.dimensions",
        {**_PERIOD, "dimension": "categories", "flow": "spending", "limit": 1, "cursor": cursor},
    )
    changed_contains = call_tool(
        service,
        "ledger.dimensions",
        {
            **_PERIOD,
            "dimension": "sources",
            "flow": "spending",
            "limit": 1,
            "cursor": cursor,
            "contains": "market",
        },
    )

    for replayed in (changed_dimension, changed_contains):
        assert replayed["isError"] is True
        assert replayed["structuredContent"]["error"]["code"] == "invalid_cursor"  # type: ignore[index]


def test_dimensions_normalizes_the_bound_contains_value(service: LedgerMCPService) -> None:
    """Two spellings that normalize identically must not mint two cursor shapes."""
    base = {**_PERIOD, "dimension": "sources", "flow": "income", "limit": 1}
    first = call_tool(service, "ledger.dimensions", {**base, "contains": "A"})
    cursor = first["structuredContent"]["page"]["next_cursor"]  # type: ignore[index]
    assert cursor

    replayed = call_tool(
        service, "ledger.dimensions", {**base, "contains": " a ", "cursor": cursor}
    )

    assert replayed.get("isError") is not True, replayed


# --- 5. shared invariants ---------------------------------------------------


def _artifacts(path: Path) -> dict[str, tuple[bytes, int]]:
    result: dict[str, tuple[bytes, int]] = {}
    for suffix in ("", "-journal", "-wal", "-shm"):
        candidate = Path(f"{path}{suffix}")
        if candidate.exists():
            result[suffix] = (candidate.read_bytes(), candidate.stat().st_mtime_ns)
    return result


def _every_surface(service: LedgerMCPService) -> list[dict[str, object]]:
    return [
        call_tool(service, "ledger.coverage", {}),
        call_tool(service, "ledger.recurring", {"status": "any"}),
        call_tool(service, "ledger.transfers", _PERIOD),
        call_tool(
            service,
            "ledger.dimensions",
            {**_PERIOD, "dimension": "sources", "flow": "spending"},
        ),
    ]


def test_the_new_surfaces_leave_the_database_and_sidecars_byte_identical(
    db_path: Path,
) -> None:
    before = _artifacts(db_path)
    service = LedgerMCPService(db_path)
    try:
        _every_surface(service)
    finally:
        service.close()

    assert _artifacts(db_path) == before


def test_every_new_surface_validates_against_its_declared_output_schema(
    service: LedgerMCPService,
) -> None:
    import jsonschema

    schemas = {tool["name"]: tool["outputSchema"] for tool in tool_schemas()}
    results = {
        "ledger.coverage": [
            call_tool(service, "ledger.coverage", {}),
            call_tool(service, "ledger.coverage", {"start_period": "bad"}),
        ],
        "ledger.recurring": [call_tool(service, "ledger.recurring", {"status": "any"})],
        "ledger.transfers": [call_tool(service, "ledger.transfers", _PERIOD)],
        "ledger.dimensions": [
            call_tool(
                service,
                "ledger.dimensions",
                {**_PERIOD, "dimension": "categories", "flow": "income"},
            )
        ],
    }

    for name, envelopes in results.items():
        for envelope in envelopes:
            jsonschema.validate(envelope["structuredContent"], schemas[name])


def test_no_new_surface_emits_a_raw_slug_number_path_or_description(
    service: LedgerMCPService,
) -> None:
    encoded = json.dumps(_every_surface(service), ensure_ascii=False)

    for private in PRIVATE_VALUES:
        assert private not in encoded, private
