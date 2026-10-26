"""Planning adapters preserve typed evidence without granting financial writes."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
from mcp_synthetic import PRIVATE_VALUES, build_synthetic_mcp_database

from ledger.mcp.contracts import tool_schemas
from ledger.mcp.server import call_tool
from ledger.mcp.service import LedgerMCPService
from ledger.storage.database import (
    BudgetDB,
    CategoryConfigDB,
    ConfigMetadataDB,
    MerchantRuleDB,
    MerchantRulePatternDB,
    session_scope,
)
from ledger.storage.operations import ACCOUNT_REF_SALT_KEY


def _reference(kind: str, marker: str = "a") -> dict[str, str]:
    return {"ref": f"{kind}-{marker * (10 if kind == 'acct' else 40)}", "revision": "f" * 64}


_PLANNING_INTENTS = [
    {
        "operation": "budget_create",
        "category": _reference("category"),
        "budget_month": "*",
        "amount": "123.45",
        "currency": "CAD",
    },
    {
        "operation": "budget_update",
        "target": _reference("budget"),
        "category": _reference("category"),
        "budget_month": "2026-10",
        "amount": "124.45",
        "currency": "CAD",
    },
    {"operation": "budget_remove", "target": _reference("budget")},
    {
        "operation": "schedule_create",
        "name": "PRIVATE-SYNTHETIC-SCHEDULE",
        "event_type": "bill",
        "amount": "22.23",
        "currency": "CAD",
        "account": _reference("acct"),
        "counterparty_account": None,
        "next_date": "2026-10-15",
        "frequency": "monthly",
        "provenance": "confirmed",
        "is_active": True,
        "effective_date": "2026-10-01",
    },
    {
        "operation": "schedule_update",
        "target": _reference("schedule"),
        "effective_date": "2026-10-01",
        "changes": {"amount": "23.24"},
    },
    *[
        {
            "operation": operation,
            "target": _reference("schedule"),
            "effective_date": "2026-10-01",
        }
        for operation in (
            "schedule_confirm",
            "schedule_deactivate",
            "schedule_reactivate",
            "schedule_remove",
        )
    ],
    {
        "operation": "preference_set",
        "scope": "category",
        "subject": _reference("category"),
        "disposition": "intentional",
    },
    {
        "operation": "preference_change",
        "target": _reference("preference"),
        "disposition": "worthwhile",
    },
    {"operation": "preference_remove", "target": _reference("preference")},
]


def _data(result: dict[str, object]) -> dict[str, object]:
    assert result.get("isError") is not True, result
    return result["structuredContent"]["data"]  # type: ignore[index,return-value]


def _artifacts(path: Path) -> dict[str, tuple[bytes, int]]:
    return {
        item.name: (item.read_bytes(), item.stat().st_mode)
        for item in path.parent.iterdir()
        if item.is_file()
    }


def test_definition_discovery_pages_stable_private_references_without_writes(
    tmp_path: Path,
) -> None:
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    before = _artifacts(db_path)
    service = LedgerMCPService(db_path)
    try:
        first = call_tool(service, "ledger.planning.definitions", {"kind": "schedule", "limit": 1})
        first_data = _data(first)
        assert first_data["status"] == "ready"
        assert len(first_data["entries"]) == 1  # type: ignore[arg-type]
        assert first == call_tool(
            service, "ledger.planning.definitions", {"kind": "schedule", "limit": 1}
        )
        cursor = first["structuredContent"]["page"]["next_cursor"]  # type: ignore[index]
        assert cursor
        second = call_tool(
            service,
            "ledger.planning.definitions",
            {"kind": "schedule", "limit": 2, "cursor": cursor},
        )
        refs = [entry["ref"] for entry in [*first_data["entries"], *_data(second)["entries"]]]  # type: ignore[union-attr]
        assert len(refs) == len(set(refs)) == 3
        for result in (first, second):
            encoded = json.dumps(result)
            assert all(private not in encoded for private in PRIVATE_VALUES)
    finally:
        service.close()
    assert _artifacts(db_path) == before


def test_definition_cursor_refuses_changed_selection_and_generation(tmp_path: Path) -> None:
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    service = LedgerMCPService(db_path)
    try:
        result = call_tool(service, "ledger.planning.definitions", {"limit": 1})
        _data(result)
        cursor = result["structuredContent"]["page"]["next_cursor"]  # type: ignore[index]
        refused = call_tool(
            service, "ledger.planning.definitions", {"kind": "schedule", "cursor": cursor}
        )
        assert refused["structuredContent"]["error"]["code"] == "invalid_cursor"  # type: ignore[index]
        with session_scope(db_path) as session:
            session.add(ConfigMetadataDB(key="synthetic-generation-change", value="changed"))
        refused = call_tool(service, "ledger.planning.definitions", {"cursor": cursor})
        assert refused["structuredContent"]["error"]["code"] == "invalid_cursor"  # type: ignore[index]
    finally:
        service.close()


def test_exact_definition_lookup_discloses_ambiguity_without_echoing_labels(tmp_path: Path) -> None:
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    label = "PRIVATE SYNTHETIC GROCERIES"
    with session_scope(db_path) as session:
        session.add(CategoryConfigDB(name=label, sort_order=10, exclude_from_spending=False))
        for month in ("*", "2026-10"):
            session.add(
                BudgetDB(
                    category=label,
                    budget_month=month,
                    amount=Decimal("100.00"),
                    notes="PRIVATE-NOTE",
                    created_at=datetime(2026, 1, 1),
                    updated_at=datetime(2026, 1, 1),
                )
            )
    service = LedgerMCPService(db_path)
    try:
        selected = _data(
            call_tool(
                service,
                "ledger.planning.definitions",
                {"kind": "budget", "exact_name": "  private  synthetic  groceries  ", "limit": 1},
            )
        )
        assert selected["selection_status"] == "ambiguous"
        assert selected["matched_count"] == 1
        categories = _data(
            call_tool(
                service, "ledger.planning.definitions", {"kind": "category", "exact_name": label}
            )
        )
        assert categories["selection_status"] == "matched"
        assert selected["entries"][0]["category_ref"] == categories["entries"][0]["ref"]  # type: ignore[index]
        absent = _data(
            call_tool(
                service,
                "ledger.planning.definitions",
                {"kind": "category", "exact_name": "PRIVATE UNKNOWN LABEL"},
            )
        )
        assert absent["selection_status"] == "unmatched"
        assert absent["entries"] == []
        before = _data(
            call_tool(
                service,
                "ledger.planning.definitions",
                {"kind": "budget", "exact_name": label, "limit": 1},
            )
        )["entries"][0]  # type: ignore[index]
        with session_scope(db_path) as session:
            session.query(BudgetDB).filter(
                BudgetDB.category == label, BudgetDB.budget_month == "*"
            ).update({BudgetDB.amount: Decimal("110.00")})
        after = _data(
            call_tool(
                service,
                "ledger.planning.definitions",
                {"kind": "budget", "exact_name": label, "limit": 1},
            )
        )["entries"][0]  # type: ignore[index]
        assert before["ref"] == after["ref"]
        assert before["revision"] != after["revision"]
        assert after["amount"] == "110.00"
        assert label not in json.dumps([selected, categories, before, after])
    finally:
        service.close()


def test_reconciliation_cursor_binds_period_as_of_and_generation(tmp_path: Path) -> None:
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    arguments = {
        "start_date": "2025-07-01",
        "end_date": "2025-07-31",
        "as_of": "2025-08-10",
        "limit": 1,
    }
    service = LedgerMCPService(db_path)
    try:
        first = call_tool(service, "ledger.plan_reconciliation", arguments)
        first_data = _data(first)
        cursor = first["structuredContent"]["page"]["next_cursor"]  # type: ignore[index]
        assert cursor
        second = _data(
            call_tool(
                service, "ledger.plan_reconciliation", {**arguments, "cursor": cursor, "limit": 2}
            )
        )
        assert len(second["findings"]) == 2  # type: ignore[arg-type]
        assert first_data["findings"][0]["occurrence_ref"] not in {
            row["occurrence_ref"] for row in second["findings"]
        }  # type: ignore[index,union-attr]
        for changed in (
            {"as_of": "2025-08-11"},
            {"end_date": "2025-08-01"},
            {"start_date": "2025-07-02"},
        ):
            result = call_tool(
                service, "ledger.plan_reconciliation", {**arguments, **changed, "cursor": cursor}
            )
            assert result["structuredContent"]["error"]["code"] == "invalid_cursor"  # type: ignore[index]
        with session_scope(db_path) as session:
            session.add(ConfigMetadataDB(key="synthetic-generation-change", value="changed"))
        result = call_tool(service, "ledger.plan_reconciliation", {**arguments, "cursor": cursor})
        assert result["structuredContent"]["error"]["code"] == "invalid_cursor"  # type: ignore[index]
    finally:
        service.close()


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("ledger.planning.definitions", {}),
        (
            "ledger.plan_reconciliation",
            {"start_date": "2025-07-01", "end_date": "2025-07-31", "as_of": "2025-08-10"},
        ),
    ],
)
def test_new_planning_reads_withhold_results_when_salt_is_absent(
    tmp_path: Path, name: str, arguments: dict[str, object]
) -> None:
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    with session_scope(db_path) as session:
        session.query(ConfigMetadataDB).filter(
            ConfigMetadataDB.key == ACCOUNT_REF_SALT_KEY
        ).delete()
    before = _artifacts(db_path)
    service = LedgerMCPService(db_path)
    try:
        data = _data(call_tool(service, name, arguments))
    finally:
        service.close()
    assert data["status"] == "unavailable"
    assert data["unavailable_reason"] == "account_ref_salt_absent"
    assert _artifacts(db_path) == before


def test_reconciliation_is_period_bound_and_withholds_stored_names(tmp_path: Path) -> None:
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    before = _artifacts(db_path)
    service = LedgerMCPService(db_path)
    try:
        result = call_tool(
            service,
            "ledger.plan_reconciliation",
            {"start_date": "2025-07-01", "end_date": "2025-07-31", "as_of": "2025-08-10"},
        )
        data = _data(result)
    finally:
        service.close()
    assert data["period"] == {"start_date": "2025-07-01", "end_date": "2025-07-31"}
    assert data["as_of"] == "2025-08-10"
    assert data["findings"]
    assert all(private not in json.dumps(result) for private in PRIVATE_VALUES)
    assert _artifacts(db_path) == before


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("ledger.planning.definitions", {"kind": "sql"}),
        ("ledger.planning.definitions", {"limit": True}),
        ("ledger.planning.definitions", {"limit": 101}),
        ("ledger.planning.definitions", {"db": "/private/synthetic.db"}),
        ("ledger.plan_reconciliation", {"start_date": "2025-07-01", "end_date": "2025-07-31"}),
    ],
)
def test_planning_read_inputs_are_enforced_before_dispatch(
    tmp_path: Path, name: str, arguments: dict[str, object]
) -> None:
    service = LedgerMCPService(build_synthetic_mcp_database(tmp_path / "ledger.db"))
    try:
        result = call_tool(service, name, arguments)
    finally:
        service.close()
    assert result["structuredContent"]["error"]["code"] == "invalid_argument"  # type: ignore[index]
    assert "private/synthetic" not in json.dumps(result)
    assert name in {schema["name"] for schema in tool_schemas()}


def test_reconciliation_brief_observation_never_emits_internal_occurrence_identity() -> None:
    from ledger.mcp.service import _brief_observations

    observations = _brief_observations(
        [
            {
                "title": "Plan versus actual exception",
                "label": "possibility",
                "dashboard_path": "/plan-reconciliation",
                "direction": "higher",
                "amount": "12.00",
                "evidence_ids": ["a" * 40],
                "rank": 1,
                "subject_kind": "occurrence",
                "subject": "private-schedule-occurrence-marker",
            },
            {
                "title": "No imported activity in period",
                "label": "coverage",
                "dashboard_path": "/import-center",
                "direction": "not_applicable",
                "amount": None,
                "evidence_ids": [],
                "rank": 2,
                "subject_kind": "none",
                "subject": None,
            },
        ],
        status="empty",
    )
    assert observations[0]["code"] == "reconciliation_exception"
    assert observations[0]["subject_kind"] == "none"
    assert observations[0]["subject"] is None
    assert "private-schedule-occurrence-marker" not in json.dumps(observations)


@pytest.mark.parametrize("intent", _PLANNING_INTENTS, ids=lambda intent: intent["operation"])
def test_typed_proposal_filing_supports_each_operation_without_opening_financial_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, intent: dict[str, object]
) -> None:
    import jsonschema

    from ledger.mcp.write_contracts import write_tool_schemas
    from ledger.mcp.write_server import call_tool as write_call_tool
    from ledger.proposals import ProposalService

    def financial_access(*_args: object, **_kwargs: object) -> None:
        pytest.fail("The isolated proposal writer opened the financial database")

    monkeypatch.setattr("ledger.storage.read_only.ReadOnlyDatabase.__init__", financial_access)
    monkeypatch.setattr("ledger.storage.database.init_database", financial_access)
    monkeypatch.setattr("ledger.storage.database.session_scope", financial_access)
    proposals_db = tmp_path / "isolated" / "proposals.db"
    service = ProposalService(proposals_db)
    result = write_call_tool(service, "ledger.proposals.planning.file", {"intent": intent})
    data = _data(result)
    assert data["status"] == "pending"
    assert len(data["proposal_revision"]) == 64  # type: ignore[arg-type]
    stored = service.show_planning_proposal(data["proposal_id"])  # type: ignore[arg-type]
    assert stored.intent.operation == intent["operation"]
    assert "PRIVATE-SYNTHETIC-SCHEDULE" not in json.dumps(result)
    assert not (proposals_db.parent / "ledger.db").exists()
    schema = next(
        tool for tool in write_tool_schemas() if tool["name"] == "ledger.proposals.planning.file"
    )
    jsonschema.validate({"intent": intent}, schema["inputSchema"])
    jsonschema.validate(result["structuredContent"], schema["outputSchema"])


@pytest.mark.parametrize(
    "intent",
    [
        {"operation": "budget_create", "amount": "100.00"},
        {"operation": "schedule_update", "target": _reference("schedule")},
        {
            "operation": "schedule_remove",
            "target": _reference("category"),
            "effective_date": "2026-10-01",
        },
        {"operation": "preference_set", "scope": "category", "subject": _reference("category")},
        {"operation": "execute_sql", "sql": "PRIVATE-SYNTHETIC-SQL"},
        {**_PLANNING_INTENTS[0], "amount": 100.0},
        {**_PLANNING_INTENTS[3], "provenance": "maybe"},
        {**_PLANNING_INTENTS[3], "counterparty_account": _reference("acct", "b")},
    ],
)
def test_ambiguous_or_untyped_planning_intent_never_creates_a_proposal(
    tmp_path: Path, intent: dict[str, object]
) -> None:
    from ledger.mcp.write_server import call_tool as write_call_tool
    from ledger.proposals import ProposalService

    proposals_db = tmp_path / "isolated" / "proposals.db"
    result = write_call_tool(
        ProposalService(proposals_db), "ledger.proposals.planning.file", {"intent": intent}
    )
    assert result["structuredContent"]["error"]["code"] == "invalid_argument"  # type: ignore[index]
    assert "PRIVATE-SYNTHETIC-SQL" not in json.dumps(result)
    assert not proposals_db.exists()


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_real_protocol_reads_target_and_files_only_the_isolated_typed_intent(
    tmp_path: Path, mode: str
) -> None:
    import anyio
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters, stdio_client

    from ledger.mcp.server import create_server
    from ledger.proposals import ProposalService

    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    proposals_db = tmp_path / "isolated" / "proposals.db"
    before = _artifacts(db_path)
    service = LedgerMCPService(db_path)
    errors_path = tmp_path / "writer.stderr"

    async def exercise(errors):
        async with Client(create_server(service), mode=mode) as client:
            definitions = await client.call_tool(
                "ledger.planning.definitions", {"kind": "schedule", "limit": 1}
            )
            target = definitions.structured_content["data"]["entries"][0]
            reconciliation = await client.call_tool(
                "ledger.plan_reconciliation",
                {"start_date": "2025-07-01", "end_date": "2025-07-31", "as_of": "2025-08-10"},
            )
            assert not reconciliation.is_error
        parameters = StdioServerParameters(
            command=str(Path(sys.executable).with_name("ledger-mcp-write")),
            args=["--proposals-db", str(proposals_db)],
        )
        with anyio.fail_after(10):
            async with Client(stdio_client(parameters, errlog=errors), mode=mode) as client:
                return await client.call_tool(
                    "ledger.proposals.planning.file",
                    {
                        "intent": {
                            "operation": "schedule_update",
                            "target": {"ref": target["ref"], "revision": target["revision"]},
                            "effective_date": "2026-10-01",
                            "changes": {"amount": "45.67"},
                        }
                    },
                )

    try:
        with errors_path.open("w+") as errors:
            filed = anyio.run(exercise, errors)
            errors.seek(0)
            assert errors.read() == ""
        assert not filed.is_error
        proposal = ProposalService(proposals_db).show_planning_proposal(
            filed.structured_content["data"]["proposal_id"]
        )
        assert proposal.intent.operation == "schedule_update"
        assert proposal.intent.changes.amount == "45.67"
    finally:
        service.close()
    after = _artifacts(db_path)
    assert {name: after[name] for name in before} == before


def test_reconciliation_resource_cap_returns_a_narrowable_content_free_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    service = LedgerMCPService(db_path)
    monkeypatch.setattr("ledger.planning.reconciliation.MAX_PLAN_ACCOUNTS", 0)
    try:
        result = call_tool(
            service,
            "ledger.plan_reconciliation",
            {"start_date": "2025-07-01", "end_date": "2025-07-31", "as_of": "2025-08-10"},
        )
    finally:
        service.close()
    assert result["structuredContent"]["error"]["code"] == "filter_too_broad"  # type: ignore[index]


@pytest.mark.parametrize("candidate_count", [1, 21])
def test_reconciliation_wire_preserves_changes_and_full_ambiguous_evidence_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, candidate_count: int
) -> None:
    from datetime import date

    from test_cash_planning import _account, _add_transaction
    from test_plan_reconciliation import _coverage, _schedule

    from ledger.core.account_identity import account_identity
    from ledger.storage.migrations import ensure_schema
    from ledger.storage.operations import ensure_account_ref_salt

    db_path = tmp_path / "synthetic.db"
    ensure_schema(db_path)
    ids = {"chequing": account_identity("test", "daily-chequing")}
    with session_scope(db_path) as session:
        session.add(_account("test", "daily-chequing"))
    monkeypatch.setattr("ledger.planning.schedules.naive_utc_now", lambda: datetime(2026, 9, 1, 12))
    _schedule(db_path, ids)
    _coverage(db_path, ids)
    for index in range(candidate_count):
        _add_transaction(
            db_path,
            txn_id=f"{index:040x}",
            on=date(2026, 9, 12),
            amount="-95.00",
            cashflow_type="expense",
            merchant="Synthetic Internet",
        )
    ensure_account_ref_salt(db_path)
    service = LedgerMCPService(db_path)
    try:
        data = _data(
            call_tool(
                service,
                "ledger.plan_reconciliation",
                {"start_date": "2026-09-01", "end_date": "2026-09-30", "as_of": "2026-10-01"},
            )
        )
        target = _data(call_tool(service, "ledger.planning.definitions", {"kind": "schedule"}))[
            "entries"
        ][0]  # type: ignore[index]
    finally:
        service.close()
    [finding] = data["findings"]  # type: ignore[misc]
    if candidate_count == 1:
        assert finding["status"] == "amount_and_date_difference"
        assert finding["amount_changed"] is True and finding["date_changed"] is True
        assert finding["amount_difference"] == "-5.00"
        assert finding["date_difference"] == 2
        assert finding["evidence_ids"] == ["0" * 40]
    else:
        assert finding["status"] == "ambiguous"
        assert finding["actual_amount"] is None
        assert len(finding["evidence_ids"]) == 20
    assert finding["evidence_count"] == candidate_count
    assert finding["candidate_count"] == candidate_count
    assert finding["evidence_truncated"] is (candidate_count > 20)
    assert finding["coverage"]["complete"] is True
    assert finding["history_complete"] is True
    assert finding["schedule_ref"] == target["ref"]
    assert "Synthetic Internet" not in json.dumps(data)


def test_imported_merchant_without_rule_can_be_discovered_and_filed_as_preference(
    tmp_path: Path,
) -> None:
    from ledger.mcp.write_server import call_tool as write_call_tool
    from ledger.proposals import ProposalService

    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    merchant = "SYNTHETIC MARKET"
    with session_scope(db_path) as session:
        assert (
            session.query(MerchantRuleDB).filter(MerchantRuleDB.merchant_norm == merchant).count()
            == 0
        )
    before = db_path.read_bytes()
    service = LedgerMCPService(db_path)
    try:
        evidence = _data(
            call_tool(
                service,
                "ledger.transactions.search",
                {"start_date": "2025-01-01", "end_date": "2025-12-31", "flow": "spending"},
            )
        )
        assert merchant in {item["source"] for item in evidence["items"]}  # type: ignore[union-attr]
        discovered = _data(
            call_tool(
                service,
                "ledger.planning.definitions",
                {"kind": "merchant", "exact_name": " synthetic market "},
            )
        )
        assert discovered["selection_status"] == "matched"
        [target] = discovered["entries"]  # type: ignore[misc]
        assert set(target) == {"kind", "ref", "revision"}
        assert merchant not in json.dumps(discovered)
        proposals = ProposalService(tmp_path / "isolated" / "proposals.db")
        filed = _data(
            write_call_tool(
                proposals,
                "ledger.proposals.planning.file",
                {
                    "intent": {
                        "operation": "preference_set",
                        "scope": "merchant",
                        "subject": {"ref": target["ref"], "revision": target["revision"]},
                        "disposition": "intentional",
                    }
                },
            )
        )
        stored = proposals.show_planning_proposal(filed["proposal_id"])  # type: ignore[arg-type]
        assert stored.intent.operation == "preference_set"
        assert stored.intent.subject.ref == target["ref"]
        assert merchant not in json.dumps(filed)
        preview = proposals.preview_planning_proposal(stored.proposal_id, ledger_db=db_path)
        assert preview.can_apply, preview.validation_errors
        assert preview.after["name"] == merchant  # type: ignore[index]
    finally:
        service.close()
    assert db_path.read_bytes() == before


def test_definition_discovery_source_cap_is_a_content_free_capacity_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = LedgerMCPService(build_synthetic_mcp_database(tmp_path / "ledger.db"))
    monkeypatch.setattr("ledger.planning.discovery.MAX_DISCOVERY_SOURCE_ROWS", 0)
    try:
        result = call_tool(
            service,
            "ledger.planning.definitions",
            {"kind": "merchant", "exact_name": "SYNTHETIC MARKET"},
        )
    finally:
        service.close()
    assert result["structuredContent"]["error"]["code"] == "filter_too_broad"  # type: ignore[index]
    assert "SYNTHETIC MARKET" not in json.dumps(result)


def _add_private_merchant_rule(db_path: Path, name: str, *, order: int) -> None:
    from ledger.config.schemas import MerchantRule

    definition = MerchantRule(
        merchant_norm=name, category="Other", patterns=[f"^SYNTHETIC-{order}$"]
    )
    with session_scope(db_path) as session:
        if session.get(CategoryConfigDB, "Other") is None:
            session.add(CategoryConfigDB(name="Other", sort_order=0, exclude_from_spending=False))
        row = MerchantRuleDB(
            merchant_norm=definition.merchant_norm,
            category=definition.category,
            exclude_from_spending=False,
            sort_order=order,
        )
        session.add(row)
        session.flush()
        session.add(
            MerchantRulePatternDB(rule_id=row.id, pattern=definition.patterns[0], sort_order=0)
        )


@pytest.mark.parametrize("kind", ["merchant", "all"])
@pytest.mark.parametrize("name_length", [161, 200])
def test_long_merchant_labels_preserve_discovery_and_unrelated_exact_lookup(
    tmp_path: Path, kind: str, name_length: int
) -> None:
    from ledger.planning.discovery import local_reference

    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    names = ["A" * (name_length - 1) + ending for ending in ("X", "Y")]
    for index, name in enumerate(names):
        _add_private_merchant_rule(db_path, name, order=index)
    before = _artifacts(db_path)
    service = LedgerMCPService(db_path)
    try:
        unrelated = _data(
            call_tool(
                service,
                "ledger.planning.definitions",
                {"kind": kind, "exact_name": " synthetic market "},
            )
        )
        assert unrelated["selection_status"] == "matched"
        assert unrelated["matched_count"] == 1
        listed = _data(call_tool(service, "ledger.planning.definitions", {"kind": kind}))
        listed_refs = {entry["ref"] for entry in listed["entries"]}  # type: ignore[union-attr]
        selected_refs = set()
        local_targets = {}
        for name in names:
            selected = _data(
                call_tool(
                    service,
                    "ledger.planning.definitions",
                    {"kind": kind, "exact_name": name.lower()},
                )
            )
            assert selected["selection_status"] == "matched"
            [target] = selected["entries"]  # type: ignore[misc]
            assert set(target) == {"kind", "ref", "revision"}
            local_targets[name] = {
                "ref": target["ref"],
                "revision": target["revision"],
            }
            assert target["ref"] in listed_refs
            selected_refs.add(target["ref"])
            assert name not in json.dumps(selected)
        # Distinct suffixes remain part of the identity, even beyond the write limit.
        assert len(selected_refs) == 2
        assert all(name not in json.dumps(listed) for name in names)
    finally:
        service.close()
    assert _artifacts(db_path) == before
    # Local operators use the alias lock; the MCP reads above create no artifacts.
    for name, reference in local_targets.items():
        assert local_reference(db_path, "merchant", name.lower()) == reference
    assert db_path.read_bytes() == before[db_path.name][0]


@pytest.mark.parametrize(
    "name", ["a" * 161, "\uff41" * 161, "\u00df" * 81], ids=["ascii", "nfkc", "case-expansion"]
)
def test_overlong_discovered_merchant_preference_preview_refuses_clearly(
    tmp_path: Path, name: str
) -> None:
    from ledger.mcp.write_server import call_tool as write_call_tool
    from ledger.proposals import ProposalService

    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    _add_private_merchant_rule(db_path, name, order=0)
    before = _artifacts(db_path)
    service = LedgerMCPService(db_path)
    try:
        selected = _data(
            call_tool(
                service,
                "ledger.planning.definitions",
                {"kind": "merchant", "exact_name": name},
            )
        )
        assert selected["selection_status"] == "matched"
        [target] = selected["entries"]  # type: ignore[misc]
        proposals = ProposalService(tmp_path / "isolated" / "proposals.db")
        filed = _data(
            write_call_tool(
                proposals,
                "ledger.proposals.planning.file",
                {
                    "intent": {
                        "operation": "preference_set",
                        "scope": "merchant",
                        "subject": {"ref": target["ref"], "revision": target["revision"]},
                        "disposition": "intentional",
                    }
                },
            )
        )
        assert name not in json.dumps(selected)
        assert name not in json.dumps(filed)
        assert _artifacts(db_path) == before
        preview = proposals.preview_planning_proposal(
            filed["proposal_id"],
            ledger_db=db_path,  # type: ignore[arg-type]
        )
        assert not preview.can_apply
        assert preview.after is None
        assert any("at most 160 characters" in error for error in preview.validation_errors)
        assert "apply_exact_reviewed_revisions" not in preview.actions
    finally:
        service.close()
    assert db_path.read_bytes() == before[db_path.name][0]


@pytest.mark.parametrize("kind", ["merchant", "all"])
def test_definition_cursor_walk_completes_beyond_ten_thousand_rows(
    tmp_path: Path, kind: str
) -> None:
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    service = LedgerMCPService(db_path)
    try:
        initial = _data(call_tool(service, "ledger.planning.definitions", {"kind": kind}))
        initial_refs = {entry["ref"] for entry in initial["entries"]}  # type: ignore[union-attr]
    finally:
        service.close()
    merchant_count = 10101 if kind == "merchant" else 5050
    category_count = 0 if kind == "merchant" else 5051
    with session_scope(db_path) as session:
        session.execute(
            MerchantRuleDB.__table__.insert(),
            [
                {
                    "id": 1000 + index,
                    "merchant_norm": f"PRIVATE SYNTHETIC MERCHANT {index:05}",
                    "category": "Other",
                    "sort_order": index,
                    "exclude_from_spending": False,
                }
                for index in range(merchant_count)
            ],
        )
        session.execute(
            MerchantRulePatternDB.__table__.insert(),
            [
                {"rule_id": 1000 + index, "pattern": f"^SYNTHETIC-{index}$", "sort_order": 0}
                for index in range(merchant_count)
            ],
        )
        if category_count:
            session.execute(
                CategoryConfigDB.__table__.insert(),
                [
                    {
                        "name": f"PRIVATE SYNTHETIC CATEGORY {index:05}",
                        "sort_order": index,
                        "exclude_from_spending": False,
                    }
                    for index in range(category_count)
                ],
            )
    before = _artifacts(db_path)
    service = LedgerMCPService(db_path)
    cursor = None
    entries = []
    try:
        for _page_number in range(202):
            result = call_tool(
                service,
                "ledger.planning.definitions",
                {"kind": kind, "limit": 100, **({"cursor": cursor} if cursor else {})},
            )
            data = _data(result)
            entries.extend(data["entries"])  # type: ignore[arg-type]
            page = result["structuredContent"]["page"]  # type: ignore[index]
            assert page["returned"] == len(data["entries"])  # type: ignore[arg-type]
            assert page["has_more"] is (page["next_cursor"] is not None)
            assert "PRIVATE SYNTHETIC" not in json.dumps(result)
            cursor = page["next_cursor"]
            if cursor is None:
                break
        else:
            pytest.fail("Definition discovery did not finish its bounded cursor walk")
        assert _page_number >= 101
        refs = {entry["ref"] for entry in entries}
        assert initial_refs <= refs
        assert len(refs) == len(entries) == len(initial_refs) + merchant_count + category_count
    finally:
        service.close()
    assert _artifacts(db_path) == before


def test_all_definition_discovery_shares_one_content_free_source_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    service = LedgerMCPService(db_path)
    try:
        initial = _data(call_tool(service, "ledger.planning.definitions", {}))
    finally:
        service.close()
    for index in range(3):
        _add_private_merchant_rule(db_path, f"PRIVATE SYNTHETIC MERCHANT {index}", order=index)
    monkeypatch.setattr(
        "ledger.planning.discovery.MAX_DISCOVERY_SOURCE_ROWS",
        len(initial["entries"]) + 2,  # type: ignore[arg-type]
    )
    service = LedgerMCPService(db_path)
    try:
        _data(call_tool(service, "ledger.planning.definitions", {"kind": "merchant"}))
        result = call_tool(service, "ledger.planning.definitions", {"kind": "all"})
        assert result["structuredContent"]["error"]["code"] == "filter_too_broad"  # type: ignore[index]
        assert "PRIVATE SYNTHETIC" not in json.dumps(result)
    finally:
        service.close()
