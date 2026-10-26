"""P8 net-worth composition for the read-only Ledger MCP.

The surface combines only statement-anchored banking balances and the latest
investment statement NAV on or before a caller-supplied date.  Its central
contract is that currencies remain separate and private investment identity
never crosses the wire.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from datetime import date
from pathlib import Path

import jsonschema
import pytest
from mcp_synthetic import (
    ACCOUNT_REF_SALT,
    BANK_INSTITUTION,
    CARD_REPORTED_ACCOUNT_ID,
    INVESTMENT_CLOSED_ACCOUNT_ID,
    INVESTMENT_INSTITUTION,
    PRIVATE_ACCOUNT_NUMBER,
    PRIVATE_INVESTMENT_ALIAS_ACTIVE,
    PRIVATE_INVESTMENT_ALIAS_CLOSED,
    PRIVATE_INVESTMENT_SOURCE_ACTIVE,
    PRIVATE_INVESTMENT_SOURCE_CLOSED,
    PRIVATE_INVESTMENT_TRANSFER_DESTINATION,
    build_synthetic_mcp_database,
)

from ledger.mcp.contracts import SIGNED_MONEY_SCHEMA, tool_schemas
from ledger.mcp.server import call_tool
from ledger.mcp.service import LedgerMCPService
from ledger.query.account_refs import banking_account_ref
from ledger.query.agent import AgentQueries, AgentQueryError
from ledger.storage.database import session_scope
from ledger.storage.operations import account_ref

_AS_OF = "2025-12-31"


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
    return result["structuredContent"]["data"]  # type: ignore[index,return-value]


def _components(result: dict[str, object]) -> list[dict[str, object]]:
    return _data(result)["components"]  # type: ignore[return-value]


def _ref(institution: str, identity: str, account_type: str) -> str:
    return account_ref(ACCOUNT_REF_SALT, institution, identity, account_type)


CLOSED_INVESTMENT_REF = _ref(
    INVESTMENT_INSTITUTION,
    PRIVATE_INVESTMENT_ALIAS_CLOSED,
    "TFSA",
)
ACTIVE_INVESTMENT_REF = _ref(
    INVESTMENT_INSTITUTION,
    PRIVATE_INVESTMENT_ALIAS_ACTIVE,
    "RRSP",
)
CHEQUING_REF = banking_account_ref(
    ACCOUNT_REF_SALT, BANK_INSTITUTION, PRIVATE_ACCOUNT_NUMBER, "chequing"
)


def test_net_worth_reports_per_currency_subtotals_without_a_grand_total(
    service: LedgerMCPService,
) -> None:
    result = call_tool(service, "ledger.net_worth", {"as_of": _AS_OF, "limit": 100})
    data = _data(result)

    assert data["queried_as_of"] == _AS_OF
    assert data["component_count"] == 6
    assert data["subtotals"] == [
        {"amount": "2873.00", "component_count": 4, "currency": "CAD"},
        {"amount": "12.68", "component_count": 2, "currency": "USD"},
    ]
    assert "total" not in data
    assert "grand_total" not in data
    assert "never converted or summed across currencies" in data["currency_semantics"]
    assert result["structuredContent"]["page"] == {  # type: ignore[index]
        "has_more": False,
        "limit": 100,
        "next_cursor": None,
        "returned": 6,
    }


def test_net_worth_uses_absolute_banking_anchors_and_discloses_closure(
    service: LedgerMCPService,
) -> None:
    by_ref = {
        component["account_ref"]: component
        for component in _components(
            call_tool(service, "ledger.net_worth", {"as_of": _AS_OF, "limit": 100})
        )
    }

    assert by_ref[CHEQUING_REF] == {
        "account_ref": CHEQUING_REF,
        "account_type": "chequing",
        "amount": "4800.00",
        "as_of": "2025-07-31",
        "as_of_lag_days": 153,
        "basis": "absolute",
        "closed_on": None,
        "component_type": "banking",
        "currency": "CAD",
        "final_statement_outstanding": None,
        "institution": BANK_INSTITUTION,
        "status": "not_recorded",
    }


def test_net_worth_discloses_an_operator_recorded_banking_closure(db_path: Path) -> None:
    with contextlib.closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute(
            """
            INSERT INTO account_lifecycle (
                account_id, status, status_confirmed_on, closed_on, note,
                created_at, updated_at
            ) VALUES (?, 'closed', '2025-07-10', '2025-07-01', '', ?, ?)
            """,
            (CARD_REPORTED_ACCOUNT_ID, "2030-01-01 00:00:00", "2030-01-01 00:00:00"),
        )
    service = LedgerMCPService(db_path)
    try:
        result = call_tool(service, "ledger.net_worth", {"as_of": _AS_OF, "limit": 100})
    finally:
        service.close()

    closed_banking = next(
        component
        for component in _components(result)
        if component["component_type"] == "banking" and component["status"] == "closed"
    )
    assert closed_banking["closed_on"] == "2025-07-01"
    assert closed_banking["final_statement_outstanding"] is None


def test_net_worth_serves_exact_last_known_investment_nav_and_outstanding_state(
    service: LedgerMCPService,
) -> None:
    result = call_tool(service, "ledger.net_worth", {"as_of": _AS_OF, "limit": 100})
    by_ref = {component["account_ref"]: component for component in _components(result)}

    assert by_ref[CLOSED_INVESTMENT_REF] == {
        "account_ref": CLOSED_INVESTMENT_REF,
        "account_type": "TFSA",
        "amount": "2.68",
        "as_of": "2025-11-30",
        "as_of_lag_days": 31,
        "basis": "last_known",
        "closed_on": None,
        "component_type": "investment",
        "currency": "USD",
        "final_statement_outstanding": True,
        "institution": INVESTMENT_INSTITUTION,
        "status": "closed",
    }
    assert by_ref[ACTIVE_INVESTMENT_REF]["amount"] == "10.00"
    assert by_ref[ACTIVE_INVESTMENT_REF]["status"] == "active"
    assert by_ref[ACTIVE_INVESTMENT_REF]["final_statement_outstanding"] is False
    assert "ROUND_HALF_EVEN" in _data(result)["quantization_semantics"]

    encoded = json.dumps(result, sort_keys=True)
    assert '"999.99"' not in encoded  # the statement after as_of
    assert '"1.11"' not in encoded  # the superseded statement before as_of


def test_net_worth_status_schema_limits_unconfirmed_status_to_investments(
    service: LedgerMCPService,
) -> None:
    result = call_tool(service, "ledger.net_worth", {"as_of": _AS_OF, "limit": 100})
    tool = next(tool for tool in tool_schemas() if tool["name"] == "ledger.net_worth")
    success = next(
        branch
        for branch in tool["outputSchema"]["oneOf"]  # type: ignore[index]
        if "data" in branch.get("properties", {})
    )
    schema = success["properties"]["data"]["properties"]["components"]["items"]
    investment = next(
        component
        for component in _components(result)
        if component["component_type"] == "investment"
    )
    unconfirmed = {**investment, "status": "unknown", "final_statement_outstanding": None}
    jsonschema.validate(unconfirmed, schema)
    for status in ("active", "closed"):
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate({**unconfirmed, "status": status}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**unconfirmed, "final_statement_outstanding": False}, schema)
    banking = next(
        component for component in _components(result) if component["component_type"] == "banking"
    )
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**banking, "status": "unknown"}, schema)


def test_net_worth_unconfirmed_lifecycle_retains_reported_value_and_date(db_path: Path) -> None:
    with contextlib.closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute(
            "UPDATE investment_accounts SET status = 'unknown', status_confirmed_on = NULL, "
            "transfer_destination = NULL WHERE account_id = ?",
            (INVESTMENT_CLOSED_ACCOUNT_ID,),
        )
    service = LedgerMCPService(db_path)
    try:
        result = call_tool(service, "ledger.net_worth", {"as_of": _AS_OF, "limit": 100})
    finally:
        service.close()
    component = next(
        component
        for component in _components(result)
        if component["account_ref"] == CLOSED_INVESTMENT_REF
    )
    assert component == {
        "account_ref": CLOSED_INVESTMENT_REF,
        "account_type": "TFSA",
        "amount": "2.68",
        "as_of": "2025-11-30",
        "as_of_lag_days": 31,
        "basis": "last_known",
        "closed_on": None,
        "component_type": "investment",
        "currency": "USD",
        "final_statement_outstanding": None,
        "institution": INVESTMENT_INSTITUTION,
        "status": "unknown",
    }
    assert "unconfirmed" in _data(result)["closure_semantics"]
    assert _data(result)["subtotals"] == [
        {"amount": "2873.00", "component_count": 4, "currency": "CAD"},
        {"amount": "12.68", "component_count": 2, "currency": "USD"},
    ]
    schema = next(tool for tool in tool_schemas() if tool["name"] == "ledger.net_worth")
    jsonschema.validate(result["structuredContent"], schema["outputSchema"])


def test_closed_investment_is_not_outstanding_when_confirmation_equals_statement_end(
    db_path: Path,
) -> None:
    with contextlib.closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute(
            "UPDATE investment_accounts SET status_confirmed_on = ? WHERE account_id = ?",
            ("2025-11-30", INVESTMENT_CLOSED_ACCOUNT_ID),
        )
    service = LedgerMCPService(db_path)
    try:
        result = call_tool(service, "ledger.net_worth", {"as_of": _AS_OF, "limit": 100})
    finally:
        service.close()

    by_ref = {component["account_ref"]: component for component in _components(result)}
    assert by_ref[CLOSED_INVESTMENT_REF]["final_statement_outstanding"] is False


def test_net_worth_never_emits_raw_investment_identity(service: LedgerMCPService) -> None:
    result = call_tool(service, "ledger.net_worth", {"as_of": _AS_OF, "limit": 100})
    encoded = json.dumps(result, ensure_ascii=False)

    for private in (
        PRIVATE_INVESTMENT_ALIAS_ACTIVE,
        PRIVATE_INVESTMENT_ALIAS_CLOSED,
        PRIVATE_INVESTMENT_SOURCE_ACTIVE,
        PRIVATE_INVESTMENT_SOURCE_CLOSED,
        PRIVATE_INVESTMENT_TRANSFER_DESTINATION,
    ):
        assert private not in encoded
    assert {component["account_ref"] for component in _components(result)} >= {
        ACTIVE_INVESTMENT_REF,
        CLOSED_INVESTMENT_REF,
    }


def test_net_worth_amounts_use_the_signed_money_contract() -> None:
    schema = next(tool for tool in tool_schemas() if tool["name"] == "ledger.net_worth")
    success = next(
        branch
        for branch in schema["outputSchema"]["oneOf"]  # type: ignore[index]
        if "data" in branch.get("properties", {})
    )
    data = success["properties"]["data"]
    component = data["properties"]["components"]["items"]
    subtotal = data["properties"]["subtotals"]["items"]

    assert component["properties"]["amount"] == SIGNED_MONEY_SCHEMA
    assert subtotal["properties"]["amount"] == SIGNED_MONEY_SCHEMA


def test_net_worth_paginates_one_stable_cross_domain_account_order(
    service: LedgerMCPService,
) -> None:
    complete = [
        component["account_ref"]
        for component in _components(
            call_tool(service, "ledger.net_worth", {"as_of": _AS_OF, "limit": 100})
        )
    ]
    first = call_tool(service, "ledger.net_worth", {"as_of": _AS_OF, "limit": 2})
    cursor = first["structuredContent"]["page"]["next_cursor"]  # type: ignore[index]
    assert cursor
    second = call_tool(
        service,
        "ledger.net_worth",
        {"as_of": _AS_OF, "limit": 2, "cursor": cursor},
    )

    assert [component["account_ref"] for component in _components(first)] == complete[:2]
    assert [component["account_ref"] for component in _components(second)] == complete[2:4]
    assert _data(second)["component_count"] == 6


def test_net_worth_cursor_refuses_a_changed_as_of(service: LedgerMCPService) -> None:
    first = call_tool(service, "ledger.net_worth", {"as_of": _AS_OF, "limit": 1})
    cursor = first["structuredContent"]["page"]["next_cursor"]  # type: ignore[index]
    assert cursor

    replayed = call_tool(
        service,
        "ledger.net_worth",
        {"as_of": "2025-11-30", "limit": 1, "cursor": cursor},
    )

    assert replayed["isError"] is True
    assert replayed["structuredContent"]["error"]["code"] == "invalid_cursor"  # type: ignore[index]


def test_net_worth_cursor_binds_snapshot_generation(db_path: Path) -> None:
    first_queries = AgentQueries(lambda: session_scope(db_path), snapshot_generation="generation-a")
    first = first_queries.net_worth(
        as_of=date.fromisoformat(_AS_OF),
        limit=1,
        account_ref_salt=ACCOUNT_REF_SALT,
    )
    assert first.next_cursor

    rotated = AgentQueries(lambda: session_scope(db_path), snapshot_generation="generation-b")
    with pytest.raises(AgentQueryError, match="invalid_cursor"):
        rotated.net_worth(
            as_of=date.fromisoformat(_AS_OF),
            limit=1,
            cursor=first.next_cursor,
            account_ref_salt=ACCOUNT_REF_SALT,
        )


def test_net_worth_withholds_components_when_the_salt_is_absent(tmp_path: Path) -> None:
    db_path = build_synthetic_mcp_database(tmp_path / "unsalted.db", account_ref_salt=False)
    service = LedgerMCPService(db_path)
    try:
        result = call_tool(service, "ledger.net_worth", {"as_of": _AS_OF})
    finally:
        service.close()

    data = _data(result)
    assert data["account_refs"] == "withheld"
    assert data["account_refs_withheld_reason"] == "account_ref_salt_absent"
    assert data["components"] == []
    assert data["component_count"] == 6
    assert data["subtotals"] == [
        {"amount": "2873.00", "component_count": 4, "currency": "CAD"},
        {"amount": "12.68", "component_count": 2, "currency": "USD"},
    ]


def test_net_worth_refuses_an_unbounded_cross_domain_account_scan(db_path: Path) -> None:
    timestamp = "2030-01-01 00:00:00"
    with contextlib.closing(sqlite3.connect(db_path)) as connection, connection:
        connection.executemany(
            """
            INSERT INTO investment_accounts (
                account_id, institution, source_account_id, account_alias,
                account_type, base_currency, status, transfer_destination,
                status_confirmed_on, closed_on, first_statement_date,
                last_statement_date, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    f"synthetic-cap-{index:04d}",
                    INVESTMENT_INSTITUTION,
                    f"synthetic-cap-source-{index:04d}",
                    f"synthetic-cap-alias-{index:04d}",
                    "RRSP",
                    "USD",
                    "active",
                    None,
                    None,
                    None,
                    None,
                    None,
                    timestamp,
                    timestamp,
                )
                for index in range(994)
            ],
        )
    service = LedgerMCPService(db_path)
    try:
        result = call_tool(service, "ledger.net_worth", {"as_of": _AS_OF})
    finally:
        service.close()

    assert result["isError"] is True
    assert result["structuredContent"] == {
        "schema": "ledger.mcp.v1",
        "error": {"code": "filter_too_broad", "retryable": False},
    }


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"as_of": "not-a-date"},
        {"as_of": "2025-6-1"},
        {"as_of": _AS_OF, "account_alias": PRIVATE_INVESTMENT_ALIAS_CLOSED},
        {"as_of": _AS_OF, "limit": 0},
    ],
)
def test_net_worth_refuses_missing_malformed_or_private_identity_inputs(
    service: LedgerMCPService,
    arguments: dict[str, object],
) -> None:
    result = call_tool(service, "ledger.net_worth", arguments)

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "invalid_argument"  # type: ignore[index]


def test_net_worth_success_validates_against_the_published_schema(
    service: LedgerMCPService,
) -> None:
    result = call_tool(service, "ledger.net_worth", {"as_of": _AS_OF, "limit": 100})
    schema = next(tool for tool in tool_schemas() if tool["name"] == "ledger.net_worth")

    jsonschema.validate(result["structuredContent"], schema["outputSchema"])
