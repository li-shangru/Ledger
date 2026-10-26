"""Registered fictional asset vocabularies remain exact, bounded, and inert."""

import asyncio
import json
from contextlib import closing
from copy import deepcopy
from dataclasses import FrozenInstanceError
from decimal import Decimal
from types import SimpleNamespace

import pytest
from test_generic_investment_adapter import statement
from test_mcp_investment_details import (
    GET,
    POSITIONS,
    PRIVATE_STATEMENT,
    _call,
    _reference,
)
from test_mcp_investment_details import (
    investment_db as investment_db,
)

from ledger.adapters import registry
from ledger.adapters.contracts import (
    API_VERSION,
    AdapterDefinition,
    AdapterError,
    CompatibilityPolicy,
    InvestmentCapability,
)
from ledger.investments import contracts
from ledger.investments.models import InvestmentAssetBalance, InvestmentPosition
from ledger.mcp import investments, server
from ledger.mcp.contracts import contract_digest, tool_schemas
from ledger.mcp.service import LedgerMCPService
from ledger.storage.database import InvestmentAssetBalanceDB, InvestmentPositionDB, session_scope

FIRST = "Fictional Regional Holdings"
SECOND = "Fictional Overseas Holdings"
GENERIC = (
    "Cash",
    "Stocks",
    "Options",
    "Bonds",
    "Funds",
    "Mutual Funds",
    "ETFs",
    "Forex",
    "Futures",
    "Futures Options",
    "Warrants",
    "CFDs",
    "Other",
    "Total",
)
CAPABILITIES = frozenset(
    {
        InvestmentCapability.COMPLETE_STATEMENT,
        InvestmentCapability.POSITIONS,
        InvestmentCapability.BALANCES,
    }
)


def write_policy(path, categories=(), *, enabled=False):
    definition = AdapterDefinition(
        "fictional-assets",
        API_VERSION,
        "1",
        policy=CompatibilityPolicy(investment_asset_categories=tuple(categories)),
    )
    record = {
        "distribution": "fictional-assets",
        "version": "1.0.0",
        "entry_point": "example",
        "sha256": "1" * 64,
        "metadata": registry._definition_metadata(definition),
        "enabled": enabled,
    }
    path.write_text(json.dumps({"schema_version": 1, "adapters": [record]}))
    path.chmod(0o600)


@pytest.fixture
def policy_path(tmp_path, monkeypatch):
    root = tmp_path / "asset-policy"
    root.mkdir(mode=0o700)
    path = root / "adapters.json"
    monkeypatch.setenv("LEDGER_ADAPTER_CONFIG", str(path))

    def forbidden(*args, **kwargs):
        pytest.fail("Historical category metadata must not inspect or execute an extension")

    monkeypatch.setattr(registry, "_distribution", forbidden)
    monkeypatch.setattr(registry, "load_adapters", forbidden)
    return path


def custom_statement():
    return statement(b"fictional vocabulary").model_copy(
        update={
            "asset_balances": tuple(
                InvestmentAssetBalance(asset_class=category, current_total=Decimal(amount))
                for category, amount in ((FIRST, "7.00"), (SECOND, "10.00"))
            ),
            "positions": tuple(
                InvestmentPosition(
                    source_row=index,
                    asset_category=category,
                    discriminator="Summary",
                    currency="CAD",
                    symbol=symbol,
                    quantity=Decimal("1.25"),
                )
                for index, category, symbol in ((1, FIRST, "AZZ"), (2, SECOND, "BZZ"))
            ),
        }
    )


def custom_database(path):
    with session_scope(path) as session:
        for row in session.query(InvestmentPositionDB).filter_by(statement_id=PRIVATE_STATEMENT):
            row.asset_category = FIRST if row.source_row == 3 else SECOND
        session.query(InvestmentAssetBalanceDB).filter_by(
            statement_id=PRIVATE_STATEMENT, asset_class="Stocks"
        ).one().asset_class = FIRST


def category_enum(schemas, tool, array, field):
    schema = next(item for item in schemas if item["name"] == tool)
    return schema["outputSchema"]["oneOf"][0]["properties"]["data"]["oneOf"][0]["properties"][
        array
    ]["items"]["properties"][field]["enum"]


def test_public_defaults_are_only_generic_and_environment_free(policy_path, monkeypatch):
    monkeypatch.setattr(
        registry, "read_configuration", lambda *a: pytest.fail("Static schema read policy")
    )
    assert contracts.ASSET_CLASSES == GENERIC
    assert category_enum(tool_schemas(), POSITIONS, "items", "asset_category") == list(GENERIC)


@pytest.mark.parametrize("enabled", [False, True])
def test_registered_vocabulary_validates_without_execution_or_relabeling(policy_path, enabled):
    write_policy(policy_path, (FIRST, SECOND), enabled=enabled)
    parsed = custom_statement()
    before = deepcopy(parsed.model_dump())
    contracts.validate_statement_contract(parsed, CAPABILITIES)
    assert parsed.model_dump() == before
    vocabulary = contracts.asset_category_vocabulary()
    assert FIRST in vocabulary.asset_classes
    assert SECOND in vocabulary.nav_asset_classes
    with pytest.raises(FrozenInstanceError):
        vocabulary.asset_classes = ()


@pytest.mark.parametrize("domain", ["positions", "asset_balances"])
def test_unregistered_category_remains_rejected(policy_path, domain):
    write_policy(policy_path, (FIRST, SECOND))
    parsed = custom_statement()
    rows = list(getattr(parsed, domain))
    field = "asset_category" if domain == "positions" else "asset_class"
    rows[0] = rows[0].model_copy(update={field: "Unregistered Holdings"})
    parsed = parsed.model_copy(update={domain: tuple(rows)})
    with pytest.raises(AdapterError, match=r"^adapter_invalid_contract$"):
        contracts.validate_statement_contract(parsed, CAPABILITIES)


@pytest.mark.parametrize(
    "value",
    [
        "not a list",
        [False],
        [""],
        ["bad\nlabel"],
        ["private/path"],
        ["X" * 81],
        [FIRST, FIRST],
        [f"Fictional Category {index}" for index in range(65)],
    ],
)
def test_invalid_saved_vocabulary_refuses_inertly(policy_path, value):
    write_policy(policy_path)
    config = json.loads(policy_path.read_text())
    config["adapters"][0]["metadata"]["policy"]["investment_asset_categories"] = value
    policy_path.write_text(json.dumps(config))
    with pytest.raises(AdapterError, match=r"^adapter_unsafe_configuration$"):
        contracts.asset_category_vocabulary()


def test_old_inert_policy_without_new_field_uses_generic_defaults(policy_path):
    write_policy(policy_path)
    config = json.loads(policy_path.read_text())
    config["adapters"][0]["metadata"]["policy"].pop("investment_asset_categories")
    policy_path.write_text(json.dumps(config))
    assert contracts.asset_category_vocabulary().asset_classes == GENERIC


def test_merged_category_vocabulary_is_deterministic_and_globally_bounded(policy_path):
    write_policy(policy_path, (SECOND, FIRST))
    config = json.loads(policy_path.read_text())
    duplicate = deepcopy(config["adapters"][0])
    duplicate["distribution"] = "fictional-second"
    duplicate["metadata"]["adapter_id"] = "fictional-second"
    duplicate["metadata"]["policy"]["investment_asset_categories"] = [FIRST]
    config["adapters"].append(duplicate)
    policy_path.write_text(json.dumps(config))
    before = contracts.asset_category_vocabulary()
    config["adapters"].reverse()
    policy_path.write_text(json.dumps(config, indent=4))
    assert contracts.asset_category_vocabulary() == before
    assert before.asset_classes.count(FIRST) == 1

    template = deepcopy(config["adapters"][0])
    config["adapters"] = []
    for group, count in (("alpha", 64), ("beta", 64), ("gamma", 1)):
        record = deepcopy(template)
        record["distribution"] = f"fictional-{group}"
        record["metadata"]["adapter_id"] = f"fictional-{group}"
        record["metadata"]["policy"]["investment_asset_categories"] = [
            f"Fictional {group} {index}" for index in range(count)
        ]
        config["adapters"].append(record)
    policy_path.write_text(json.dumps(config))
    with pytest.raises(AdapterError, match=r"^adapter_unsafe_configuration$"):
        contracts.asset_category_vocabulary()


@pytest.mark.parametrize("enabled", [False, True])
def test_wire_schema_and_serialization_use_registered_snapshot(
    policy_path, investment_db, monkeypatch, enabled
):
    write_policy(policy_path, (FIRST, SECOND), enabled=enabled)
    custom_database(investment_db)
    with closing(LedgerMCPService(investment_db)) as service:
        reference = _reference(service)
        result = _call(service, POSITIONS, {"statement_ref": reference})
        assert [row["asset_category"] for row in result["data"]["items"]] == [FIRST, SECOND, SECOND]
        assert result["data"]["items"][0]["quantity"] == "1.000000000001"
        summary = _call(service, GET, {"statement_ref": reference})
        assert FIRST in {row["asset_class"] for row in summary["data"]["asset_balances"]}
        monkeypatch.setattr(server, "Server", lambda *a, **kw: SimpleNamespace(**kw))
        protocol = server.create_server(service)
        advertised = asyncio.run(protocol.on_list_tools(None, None))
        schemas = [item.model_dump(by_alias=True) for item in advertised.tools]
        assert FIRST in category_enum(schemas, POSITIONS, "items", "asset_category")
        assert SECOND in category_enum(schemas, GET, "asset_balances", "asset_class")
        status = _call(service, "ledger.status", {})
        assert status["data"]["contract_digest"] == contract_digest(
            investment_vocabulary=service.investment_vocabulary
        )


def test_wire_read_refuses_unregistered_persisted_category(policy_path, investment_db):
    write_policy(policy_path, (SECOND,))
    custom_database(investment_db)
    with closing(LedgerMCPService(investment_db)) as service:
        result = server.call_tool(service, POSITIONS, {"statement_ref": _reference(service)})
        assert result["structuredContent"]["error"]["code"] == "corrupt_data"


def test_disabling_adapter_keeps_existing_historical_schema_readable(policy_path, investment_db):
    write_policy(policy_path, (FIRST, SECOND), enabled=True)
    custom_database(investment_db)
    with closing(LedgerMCPService(investment_db)) as service:
        reference = _reference(service)
        before = service.investment_vocabulary
        registry.disable_distribution("fictional-assets", "example", path=policy_path)
        result = _call(service, POSITIONS, {"statement_ref": reference})
        assert len(result["data"]["items"]) == 3
        assert service.investment_vocabulary is before


def test_new_vocabulary_invalidates_old_cursor_after_service_restart(policy_path, investment_db):
    write_policy(policy_path, (FIRST, SECOND))
    custom_database(investment_db)
    with closing(LedgerMCPService(investment_db)) as service:
        reference = _reference(service)
        first = _call(service, POSITIONS, {"statement_ref": reference, "limit": 1})
        cursor = first["page"]["next_cursor"]
    write_policy(policy_path, (FIRST, SECOND, "Fictional Additional Holdings"))
    with closing(LedgerMCPService(investment_db)) as fresh:
        result = server.call_tool(fresh, POSITIONS, {"statement_ref": reference, "cursor": cursor})
        assert result["structuredContent"]["error"]["code"] == "invalid_cursor"


def test_vocabulary_change_requires_fresh_service_and_keeps_advertised_snapshot(
    policy_path, investment_db, monkeypatch
):
    write_policy(policy_path, (FIRST, SECOND))
    custom_database(investment_db)
    with closing(LedgerMCPService(investment_db)) as service:
        reference = _reference(service)
        original_vocabulary = service.investment_vocabulary
        write_policy(policy_path, (FIRST,))
        result = server.call_tool(service, POSITIONS, {"statement_ref": reference})
        assert result["structuredContent"]["error"]["code"] == "incompatible_schema"
        assert service.investment_vocabulary is original_vocabulary
        monkeypatch.setattr(server, "Server", lambda *a, **kw: SimpleNamespace(**kw))
        protocol = server.create_server(service)
        advertised = asyncio.run(protocol.on_list_tools(None, None))
        schemas = [item.model_dump(by_alias=True) for item in advertised.tools]
        assert SECOND in category_enum(schemas, POSITIONS, "items", "asset_category")
    with closing(LedgerMCPService(investment_db)) as fresh:
        assert SECOND not in fresh.investment_vocabulary.asset_classes


def test_mid_operation_vocabulary_change_refuses_without_mixed_rows(
    policy_path, investment_db, monkeypatch
):
    write_policy(policy_path, (FIRST, SECOND))
    custom_database(investment_db)
    with closing(LedgerMCPService(investment_db)) as service:
        reference = _reference(service)
        original_position = investments._position
        seen = []

        def change_after_first_row(row, vocabulary):
            value = original_position(row, vocabulary)
            seen.append(value["asset_category"])
            if len(seen) == 1:
                write_policy(policy_path, (FIRST,))
            return value

        monkeypatch.setattr(investments, "_position", change_after_first_row)
        result = server.call_tool(service, POSITIONS, {"statement_ref": reference})
        assert seen == [FIRST, SECOND, SECOND]
        assert result["structuredContent"]["error"]["code"] == "incompatible_schema"


def test_vocabulary_reads_are_at_operation_boundaries_not_per_row(
    policy_path, investment_db, monkeypatch
):
    write_policy(policy_path, (FIRST, SECOND))
    custom_database(investment_db)
    with closing(LedgerMCPService(investment_db)) as service:
        reference = _reference(service)
        original_read = registry.read_configuration
        calls = []

        def counted(*args, **kwargs):
            calls.append("read")
            return original_read(*args, **kwargs)

        monkeypatch.setattr(registry, "read_configuration", counted)
        _call(service, POSITIONS, {"statement_ref": reference})
        assert calls == ["read", "read"]
