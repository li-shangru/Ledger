"""Quality responses expose only the bounded fields of a recorded check run.

Stored diagnostic details can contain arbitrary names or inert instructions;
synthetic fixtures verify that these private details never reach the response."""

from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from typing import Any

import pytest
from mcp_synthetic import (
    PRIVATE_VALUES,
    QUALITY_PRIOR_CHECK_NAME,
    QUALITY_RUN_AT,
    QUALITY_STORED_DETAIL,
    QUALITY_UNSERVABLE_CHECK_NAME,
    QUALITY_UNSERVABLE_SEVERITY,
    build_synthetic_mcp_database,
)
from sqlalchemy import text

from ledger.mcp.contracts import (
    INSIGHT_TRIAGE_STATEMENT,
    QUALITY_DETAILS_STATEMENT,
    QUALITY_SCOPE_STATEMENT,
    QUALITY_UNSERVABLE_STATEMENT,
    QUALITY_VOCABULARY_STATEMENT,
    tool_schemas,
)
from ledger.mcp.server import call_tool
from ledger.mcp.service import LedgerMCPService
from ledger.quality.service import DataQualityService
from ledger.query.agent import (
    _MAX_QUALITY_CHECKS,
    INSIGHT_SEVERITIES,
    INSIGHT_STATUSES,
    QUALITY_SEVERITIES,
    QUALITY_STATUSES,
)
from ledger.storage.database import DataQualityCheckDB, session_scope

_ALL = {"limit": 100}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return build_synthetic_mcp_database(tmp_path / "ledger.db")


@pytest.fixture
def service(db_path: Path) -> LedgerMCPService:
    built = LedgerMCPService(db_path)
    yield built
    built.close()


def _data(result: dict[str, object]) -> dict[str, Any]:
    assert result.get("isError") is not True, result
    return result["structuredContent"]["data"]  # type: ignore[index,return-value]


def _entry_schema() -> dict[str, Any]:
    tool = next(tool for tool in tool_schemas() if tool["name"] == "ledger.quality")
    success = next(
        branch for branch in tool["outputSchema"]["oneOf"] if "data" in branch.get("properties", {})
    )
    return success["properties"]["data"]["properties"]["entries"]["items"]


# --- 1. exactly four fields --------------------------------------------------


def test_every_entry_carries_exactly_the_four_permitted_fields(
    service: LedgerMCPService,
) -> None:
    data = _data(call_tool(service, "ledger.quality", _ALL))
    schema = _entry_schema()

    assert data["entries"], "the fixture seeded no servable checks"
    for entry in data["entries"]:
        assert set(entry) == {"check_name", "severity", "status", "issue_count"}, entry
    assert set(schema["properties"]) == {"check_name", "severity", "status", "issue_count"}
    assert set(schema["required"]) == set(schema["properties"])
    assert schema["additionalProperties"] is False


def test_the_details_blob_never_crosses_even_when_it_is_populated(
    service: LedgerMCPService,
) -> None:
    """The live column is empty; the fixture's is not, which is the real case."""
    result = call_tool(service, "ledger.quality", _ALL)
    encoded = json.dumps(result, ensure_ascii=False)
    data = _data(result)

    # As a KEY, at any depth. `details_semantics` is the const that says the blob
    # is withheld, so a bare substring check would trip on the disclosure itself.
    assert '"details":' not in encoded
    assert all("details" not in entry for entry in data["entries"])
    assert QUALITY_STORED_DETAIL not in encoded
    for private in PRIVATE_VALUES:
        assert private not in encoded, private
    assert data["details_semantics"] == QUALITY_DETAILS_STATEMENT
    assert "details" in QUALITY_DETAILS_STATEMENT
    assert '"details"' not in json.dumps(
        next(tool for tool in tool_schemas() if tool["name"] == "ledger.quality"),
        ensure_ascii=False,
    )


def test_the_stored_detail_really_is_in_the_database(db_path: Path) -> None:
    """Or the assertion above passes by having nothing to find."""
    assert QUALITY_STORED_DETAIL.encode("utf-8") in db_path.read_bytes()


# --- 2. scope ----------------------------------------------------------------


def test_the_scope_is_the_newest_run(service: LedgerMCPService) -> None:
    data = _data(call_tool(service, "ledger.quality", _ALL))
    encoded = json.dumps(data, ensure_ascii=False)

    assert data["run_at"] == QUALITY_RUN_AT.isoformat()
    assert data["scope_semantics"] == QUALITY_SCOPE_STATEMENT
    assert QUALITY_PRIOR_CHECK_NAME not in encoded
    assert "only ever" in QUALITY_SCOPE_STATEMENT


def test_an_empty_table_reports_no_run_rather_than_a_passing_one(
    db_path: Path,
) -> None:
    with session_scope(db_path) as session:
        session.query(DataQualityCheckDB).delete()
    service = LedgerMCPService(db_path)
    try:
        data = _data(call_tool(service, "ledger.quality", _ALL))
    finally:
        service.close()

    assert data["run_at"] is None
    assert data["entries"] == []
    assert data["entry_count"] == 0
    assert data["unservable_count"] == 0


# --- 3. the vocabularies -----------------------------------------------------


def _producible_vocabularies() -> dict[str, set[str]]:
    """Every severity and status literal any check CAN construct, off the source.

    A run over a fixture only records the branches that fixture triggers. There
    is no shared severity helper to sweep here -- unlike `ledger.insights`, the
    nine checks each hardcode their own ternary -- so the producible set is read
    from every `DataQualityIssue(...)` construction in the module, taking both
    arms of each conditional. A tenth check whose warning branch the fixture
    never triggers is caught here and nowhere else.
    """
    source = Path(inspect.getsourcefile(DataQualityService) or "").read_text(encoding="utf-8")
    found: dict[str, set[str]] = {"severity": set(), "status": set()}
    constructions = 0
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        if not (isinstance(node.func, ast.Name) and node.func.id == "DataQualityIssue"):
            continue
        constructions += 1
        for keyword in node.keywords:
            if keyword.arg not in found:
                continue
            arms = (
                [keyword.value.body, keyword.value.orelse]
                if isinstance(keyword.value, ast.IfExp)
                else [keyword.value]
            )
            for arm in arms:
                assert isinstance(arm, ast.Constant), ast.dump(arm)
                found[keyword.arg].add(str(arm.value))
    assert constructions == 9, constructions
    return found


def test_the_wire_vocabularies_are_the_ones_the_checks_can_produce(tmp_path: Path) -> None:
    """The vocabulary is DERIVED twice over, not asserted against a literal.

    A subset assertion plus a hand-written literal is not a derivation: `<=` is
    satisfied by a run that happens to trigger fewer branches than exist, and
    the literal beside it then makes the published enum look checked while
    nothing checks it. Three-way equality between what the source can produce,
    what a real run records, and what the wire publishes.
    """
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    issues = DataQualityService(db_path).run_checks()
    producible = _producible_vocabularies()
    schema = _entry_schema()

    assert issues, "the checks recorded nothing"
    assert (
        producible["severity"]
        == {issue.severity for issue in issues}
        == set(QUALITY_SEVERITIES)
        == set(schema["properties"]["severity"]["enum"])
    )
    assert (
        producible["status"]
        == {issue.status for issue in issues}
        == set(QUALITY_STATUSES)
        == set(schema["properties"]["status"]["enum"])
    )


def test_the_quality_vocabulary_is_not_the_insight_vocabulary(
    service: LedgerMCPService,
) -> None:
    """Two tables, two column names in common, and no values in common.

    A caller that read one enum as the other would rank an `error` against a
    `high`. Both surfaces say so, on the wire, because they ship together.
    """
    data = _data(call_tool(service, "ledger.quality", _ALL))

    assert data["vocabulary_semantics"] == QUALITY_VOCABULARY_STATEMENT
    assert set(QUALITY_SEVERITIES).isdisjoint(INSIGHT_SEVERITIES)
    assert set(QUALITY_STATUSES).isdisjoint(INSIGHT_STATUSES)
    assert "ledger.insights" in QUALITY_VOCABULARY_STATEMENT
    assert "ledger.quality" in INSIGHT_TRIAGE_STATEMENT


def test_the_nine_recorded_check_names_are_what_one_run_records(
    tmp_path: Path,
) -> None:
    """The names are a fixed hardcoded list, so the fixture may not invent them."""
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    recorded = {issue.check_name for issue in DataQualityService(db_path).run_checks()}
    service = LedgerMCPService(db_path)
    try:
        data = _data(call_tool(service, "ledger.quality", _ALL))
    finally:
        service.close()

    assert len(recorded) == 9
    assert {entry["check_name"] for entry in data["entries"]} == recorded
    assert data["entry_count"] == 9


def test_a_row_outside_the_published_vocabularies_is_counted_not_dropped(
    service: LedgerMCPService,
) -> None:
    data = _data(call_tool(service, "ledger.quality", _ALL))
    encoded = json.dumps(data, ensure_ascii=False)

    assert data["unservable_count"] == 1
    assert data["unservable_semantics"] == QUALITY_UNSERVABLE_STATEMENT
    assert QUALITY_UNSERVABLE_CHECK_NAME not in encoded
    assert QUALITY_UNSERVABLE_SEVERITY not in encoded
    assert data["entry_count"] == 9


def test_the_counts_reconcile_with_the_served_entries(
    service: LedgerMCPService,
) -> None:
    data = _data(call_tool(service, "ledger.quality", {"limit": 3}))

    assert sum(data["severity_counts"].values()) == data["entry_count"] == 9
    assert sum(data["status_counts"].values()) == data["entry_count"]
    assert set(data["severity_counts"]) == set(QUALITY_SEVERITIES)
    assert set(data["status_counts"]) == set(QUALITY_STATUSES)
    assert len(data["entries"]) == 3


# --- 4. pagination -----------------------------------------------------------


def test_a_cursor_walk_returns_every_check_exactly_once(
    service: LedgerMCPService,
) -> None:
    seen: list[str] = []
    arguments: dict[str, object] = {"limit": 4}
    for _ in range(10):
        result = call_tool(service, "ledger.quality", arguments)
        data = _data(result)
        page = result["structuredContent"]["page"]  # type: ignore[index]
        seen.extend(entry["check_name"] for entry in data["entries"])
        if not page["has_more"]:
            break
        arguments = {"limit": 4, "cursor": page["next_cursor"]}

    assert len(seen) == len(set(seen)) == 9
    assert seen == sorted(seen)
    assert "ordered by check name" in QUALITY_SCOPE_STATEMENT


def test_a_cursor_minted_against_another_generation_is_refused(
    db_path: Path,
) -> None:
    """The 1.3.0 regression this binding exists to prevent, on this tool too.

    A cursor minted before a write and replayed after it must not return page
    two of a different dataset behind an ordinary success envelope. Only
    `ledger.insights` had this; nothing here would have noticed `generation=`
    being dropped from the fingerprint at `query/agent.py`'s `quality_checks`.
    """
    service = LedgerMCPService(db_path)
    try:
        first = call_tool(service, "ledger.quality", {"limit": 1})
        cursor = first["structuredContent"]["page"]["next_cursor"]  # type: ignore[index]
    finally:
        service.close()
    with session_scope(db_path) as session:
        session.add(
            DataQualityCheckDB(
                run_at=QUALITY_RUN_AT,
                check_name="synthetic_added_after_the_cursor",
                severity="info",
                status="pass",
                issue_count=0,
                details={},
            )
        )
    moved = LedgerMCPService(db_path)
    try:
        replayed = call_tool(moved, "ledger.quality", {"limit": 1, "cursor": cursor})
    finally:
        moved.close()

    assert cursor
    assert replayed["isError"] is True
    assert replayed["structuredContent"]["error"]["code"] == "invalid_cursor"


def test_a_cursor_does_not_cross_between_the_two_sibling_surfaces(
    service: LedgerMCPService,
) -> None:
    """The `kind` half of the same fingerprint, which the generation cannot cover.

    `ledger.insights` and `ledger.quality` are served from one pinned database,
    so their cursors share a generation and differ only by `kind`. Dropping it
    would let an insights offset address a row in the check table.
    """
    minted = call_tool(service, "ledger.insights", {"limit": 1})
    cursor = minted["structuredContent"]["page"]["next_cursor"]  # type: ignore[index]

    crossed = call_tool(service, "ledger.quality", {"limit": 1, "cursor": cursor})

    assert cursor
    assert crossed["isError"] is True
    assert crossed["structuredContent"]["error"]["code"] == "invalid_cursor"


def test_a_stored_run_past_the_scan_cap_answers_filter_too_broad(
    db_path: Path,
) -> None:
    """A code naming a filter, on a tool whose only inputs are cursor and limit.

    Nothing exercised this cap, and the contract's own explanation of why a
    non-retryable `filter_too_broad` can reach a request with nothing to narrow
    named `ledger.balances` and `ledger.projection` only.
    """
    with session_scope(db_path) as session:
        session.add_all(
            DataQualityCheckDB(
                run_at=QUALITY_RUN_AT,
                check_name=f"synthetic_scan_cap_{index}",
                severity="info",
                status="pass",
                issue_count=0,
                details={},
            )
            for index in range(_MAX_QUALITY_CHECKS + 1)
        )
    service = LedgerMCPService(db_path)
    try:
        refused = call_tool(service, "ledger.quality", {"limit": 1})
    finally:
        service.close()

    tool = next(tool for tool in tool_schemas() if tool["name"] == "ledger.quality")
    assert refused["isError"] is True
    assert refused["structuredContent"]["error"] == {
        "code": "filter_too_broad",
        "retryable": False,
    }
    assert set(tool["inputSchema"]["properties"]) == {"cursor", "limit"}


def test_an_unreadable_stored_timestamp_is_corrupt_data_not_invalid_argument(
    db_path: Path,
) -> None:
    """The same stored-value failure, on the sibling surface, named the same way.

    `run_at` is a DATETIME column read exactly as `ledger.insights` reads
    `created_at`, so the fix for one that did not cover the other would be the
    class of half-applied change this release exists to remove. It is handled
    once, in `LedgerMCPService._served`, which is why this passes.
    """
    with session_scope(db_path) as session:
        session.execute(
            text("UPDATE data_quality_checks SET run_at = :stamp"),
            {"stamp": "not-a-timestamp"},
        )
    service = LedgerMCPService(db_path)
    try:
        result = call_tool(service, "ledger.quality", {"limit": 100})
    finally:
        service.close()

    assert result["isError"] is True
    assert result["structuredContent"]["error"] == {
        "code": "corrupt_data",
        "retryable": False,
    }
