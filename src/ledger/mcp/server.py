"""Protocol-clean stdio adapter for the local read-only Ledger MCP."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import re
import sys
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Never, cast

from jsonschema import validate as validate_json_schema
from jsonschema.exceptions import SchemaError, ValidationError
from mcp.server import Server, ServerRequestContext
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    PaginatedRequestParams,
    Tool,
)

from ledger import __version__
from ledger.mcp.contracts import (
    ENVELOPE_SCHEMA,
    ERROR_ENVELOPE_SCHEMA,
    INSTRUCTION_TRUST,
    SERVER_INSTRUCTIONS,
    tool_schemas,
)
from ledger.mcp.planning_contracts import DEFINITION_KINDS
from ledger.mcp.service import LedgerMCPService
from ledger.query.agent import AgentFilters, AgentQueryError
from ledger.storage.read_only import ReadOnlyDatabaseError

_OUTPUT_SCHEMAS = {
    str(schema["name"]): cast(dict[str, object], schema["outputSchema"])
    for schema in tool_schemas()
}
# Kernel-side copies of the published input patterns. The declared schema is
# advisory to a client; this is what actually admits a value.
_MONEY_PATTERN = re.compile(r"(?:0|[1-9]\d*)\.\d{2}")
_EVIDENCE_ID_PATTERN = re.compile(r"[0-9a-f]{40}")
_MONTH_PATTERN = re.compile(r"\d{4}-(?:0[1-9]|1[0-2])")
_TIMESTAMP_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?")
_GENERATION_PATTERN = re.compile(r"[0-9a-f]{16}")
_ACCOUNT_REF_PATTERN = re.compile(r"acct-[0-9a-f]{10}")
_STATEMENT_REF_PATTERN = re.compile(r"stmt-[0-9a-f]{24}")
_ORDER_VALUES = {"date_asc", "date_desc", "amount_desc", "amount_asc"}
# Two flow vocabularies, deliberately. `ledger.transactions.search` admits the
# union; `ledger.aggregate` and `ledger.dimensions` must not, because a single
# total over income plus spending is meaningless and a value list counted over
# both cannot be reproduced by any single-flow aggregate.
_FLOW_VALUES = {"income", "spending"}
_SEARCH_FLOW_VALUES = {"income", "spending", "any"}
_GROUP_BY_VALUES = {"source", "merchant", "category", "month", "day", "none", "account_ref"}
_DIMENSION_VALUES = {"categories", "institutions", "sources"}
_RECURRING_STATUS_VALUES = {"active", "inactive", "any"}
_EXPLANATION_METRICS = {
    "balance",
    "budget",
    "budget_actual",
    "budget_variance",
    "gross_spending",
    "income",
    "net",
    "offsets",
    "projection",
    "true_cost",
}


def _strict_utf8(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise AgentQueryError
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise AgentQueryError from None
    return value


def _validated_arguments(
    name: str,
    arguments: Mapping[str, object] | None,
) -> dict[str, object]:
    allowed = {
        "ledger.status": set(),
        "ledger.aggregate": {
            "cursor",
            "end_date",
            "filters",
            "flow",
            "group_by",
            "limit",
            "max_amount",
            "min_amount",
            "start_date",
        },
        "ledger.transactions.search": {
            "cursor",
            "end_date",
            "filters",
            "flow",
            "limit",
            "max_amount",
            "min_amount",
            "order",
            "start_date",
        },
        "ledger.transaction.get": {"evidence_id"},
        "ledger.balances": {"as_of", "change_since", "cursor", "limit"},
        "ledger.budgets": {"end_date", "start_date"},
        "ledger.brief": {"end_date", "since", "start_date"},
        "ledger.cash_plan": {"as_of", "end_date", "start_date"},
        "ledger.planning.definitions": {"account_ref", "cursor", "exact_name", "kind", "limit"},
        "ledger.plan_reconciliation": {"as_of", "cursor", "end_date", "limit", "start_date"},
        "ledger.changes": {"client_generation", "cursor", "limit", "since"},
        "ledger.coverage": {"cursor", "end_period", "limit", "start_period"},
        "ledger.insights": {"cursor", "limit"},
        "ledger.explain": {"account_ref", "category", "end_date", "metric", "start_date"},
        "ledger.net_worth": {"as_of", "cursor", "limit"},
        "ledger.investments.statements": {
            "start_date",
            "end_date",
            "account_ref",
            "cursor",
            "limit",
        },
        "ledger.investments.statement.get": {"statement_ref"},
        "ledger.investments.positions": {"statement_ref", "cursor", "limit"},
        "ledger.investments.activities": {"statement_ref", "cursor", "limit"},
        "ledger.projection": {"as_of", "cursor", "limit", "target_date"},
        "ledger.quality": {"cursor", "limit"},
        "ledger.recurring": {"cursor", "limit", "status"},
        "ledger.transfers": {"cursor", "end_date", "limit", "start_date"},
        "ledger.dimensions": {
            "contains",
            "cursor",
            "dimension",
            "end_date",
            "flow",
            "limit",
            "start_date",
        },
    }
    required = {
        "ledger.status": set(),
        "ledger.aggregate": {"end_date", "flow", "group_by", "start_date"},
        "ledger.transactions.search": {"end_date", "flow", "start_date"},
        "ledger.transaction.get": {"evidence_id"},
        # `as_of` is required rather than defaulted: a balance is only true as of
        # a date, and a server-chosen default would let a stale anchor be read as
        # today's balance without anyone naming today.
        "ledger.balances": {"as_of"},
        "ledger.budgets": {"end_date", "start_date"},
        "ledger.brief": {"end_date", "start_date"},
        "ledger.cash_plan": {"as_of", "end_date", "start_date"},
        "ledger.planning.definitions": set(),
        "ledger.plan_reconciliation": {"as_of", "end_date", "start_date"},
        # The boundary alone (D12). `client_generation` is optional and buys the
        # caller one informational comparison: it rotates on every write, so
        # requiring it bought the ANSWER nothing and gating on it made every
        # routine import unreportable.
        "ledger.changes": {"since"},
        "ledger.coverage": set(),
        # Both surfaces scope themselves to the newest stored period or run.
        # That is derived from the data rather than requested, so there is no
        # input to require and none to make optional.
        "ledger.insights": set(),
        "ledger.explain": {"end_date", "metric", "start_date"},
        "ledger.net_worth": {"as_of"},
        "ledger.investments.statements": {"start_date", "end_date"},
        "ledger.investments.statement.get": {"statement_ref"},
        "ledger.investments.positions": {"statement_ref"},
        "ledger.investments.activities": {"statement_ref"},
        "ledger.quality": set(),
        # Both window bounds, for the reason `as_of` is required on
        # ledger.balances: a projection is only true between two named dates, and
        # a server-chosen default would let a stale anchor be read as today's
        # starting point without anyone naming today.
        "ledger.projection": {"as_of", "target_date"},
        "ledger.recurring": set(),
        "ledger.transfers": {"end_date", "start_date"},
        "ledger.dimensions": {"dimension", "end_date", "flow", "start_date"},
    }
    if name not in allowed:
        raise AgentQueryError
    if arguments is None:
        values: dict[str, object] = {}
    elif isinstance(arguments, Mapping) and all(isinstance(key, str) for key in arguments):
        values = dict(arguments)
        for key in values:
            _strict_utf8(key)
    else:
        raise AgentQueryError
    if not set(values) <= allowed[name] or not required[name] <= set(values):
        raise AgentQueryError
    return values


def _date_argument(values: Mapping[str, object], key: str) -> date:
    value = _strict_utf8(values.get(key))
    if len(value) != 10 or value[4] != "-" or value[7] != "-":
        raise AgentQueryError
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise AgentQueryError from None
    if parsed.isoformat() != value:
        raise AgentQueryError
    return parsed


def _enum_argument(
    values: Mapping[str, object],
    key: str,
    allowed: set[str],
) -> str:
    value = _strict_utf8(values.get(key))
    if value not in allowed:
        raise AgentQueryError
    return value


def _money_argument(values: Mapping[str, object], key: str) -> Decimal | None:
    if key not in values:
        return None
    value = _strict_utf8(values.get(key))
    if len(value) > 24 or not _MONEY_PATTERN.fullmatch(value):
        raise AgentQueryError
    try:
        return Decimal(value)
    except InvalidOperation:  # pragma: no cover - the pattern already excludes this
        raise AgentQueryError from None


def _order_argument(values: Mapping[str, object]) -> str:
    if "order" not in values:
        return "date_asc"
    return _enum_argument(values, "order", _ORDER_VALUES)


def _month_argument(values: Mapping[str, object], key: str) -> str | None:
    if key not in values:
        return None
    value = _strict_utf8(values.get(key))
    if not _MONTH_PATTERN.fullmatch(value):
        raise AgentQueryError
    return value


def _timestamp_argument(values: Mapping[str, object], key: str) -> datetime:
    """Parse a naive-UTC wire timestamp, refusing anything that carries a zone.

    Storage is naive UTC, so a boundary with an offset -- or a bare `Z` -- is
    either four hours wrong on this machine or an assertion the server would have
    to convert silently. The published pattern admits neither, and this is the
    kernel-side copy that actually enforces it. It also refuses a bare date and
    the space-separated form, so the one accepted spelling is the one
    `latest_imported_at` emits and a caller can echo it back unmodified.
    """
    value = _strict_utf8(values.get(key))
    if not _TIMESTAMP_PATTERN.fullmatch(value):
        raise AgentQueryError
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise AgentQueryError from None
    if parsed.tzinfo is not None:
        raise AgentQueryError
    return parsed


def _generation_argument(values: Mapping[str, object], key: str) -> str:
    value = _strict_utf8(values.get(key))
    if not _GENERATION_PATTERN.fullmatch(value):
        raise AgentQueryError
    return value


def _contains_argument(values: Mapping[str, object]) -> str | None:
    if "contains" not in values:
        return None
    value = _strict_utf8(values.get("contains"))
    if not value.strip() or len(value) > 200:
        raise AgentQueryError
    return value


def _evidence_id_argument(values: Mapping[str, object]) -> str:
    value = _strict_utf8(values.get("evidence_id"))
    if not _EVIDENCE_ID_PATTERN.fullmatch(value):
        raise AgentQueryError
    return value


def _account_ref_argument(values: Mapping[str, object]) -> str | None:
    if "account_ref" not in values:
        return None
    value = _strict_utf8(values.get("account_ref"))
    if not _ACCOUNT_REF_PATTERN.fullmatch(value):
        raise AgentQueryError
    return value


def _statement_ref_argument(values: Mapping[str, object]) -> str:
    value = _strict_utf8(values.get("statement_ref"))
    if not _STATEMENT_REF_PATTERN.fullmatch(value):
        raise AgentQueryError
    return value


def _category_argument(values: Mapping[str, object]) -> str | None:
    if "category" not in values:
        return None
    value = _strict_utf8(values.get("category"))
    if len(value) > 100:
        raise AgentQueryError
    return value


def _limit_argument(values: Mapping[str, object]) -> int:
    value = values.get("limit", 50)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise AgentQueryError
    return value


def _cursor_argument(values: Mapping[str, object]) -> str | None:
    value = values.get("cursor")
    if value is None:
        return None
    cursor = _strict_utf8(value)
    if len(cursor) > 4096:
        raise AgentQueryError
    return cursor


def _filter_values(raw: object, key: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    # An empty array is a request naming zero values, not the absence of a
    # filter: serving the unfiltered full-period total for it handed an agent
    # whose upstream lookup matched nothing exactly the wrong number.
    if not isinstance(raw, list) or not raw or len(raw) > 50:
        raise AgentQueryError
    values = tuple(_strict_utf8(item) for item in raw)
    if any(len(value) > 200 for value in values) or len(set(values)) != len(values):
        raise AgentQueryError
    return values


def _filters_argument(values: Mapping[str, object]) -> AgentFilters:
    raw = values.get("filters")
    if raw is None:
        return AgentFilters()
    if not isinstance(raw, Mapping) or not all(isinstance(key, str) for key in raw):
        raise AgentQueryError
    allowed = {"categories", "institutions", "sources"}
    if not set(raw) <= allowed:
        raise AgentQueryError
    return AgentFilters(
        sources=_filter_values(raw.get("sources"), "sources"),
        categories=_filter_values(raw.get("categories"), "categories"),
        institutions=_filter_values(raw.get("institutions"), "institutions"),
    )


# Retryable means "the same call may succeed shortly, unchanged".
#
# `read_only_unavailable` is the transient one: it is what a `-journal` sidecar
# produces while a writer is mid-transaction, and it clears when the write
# finishes. `database_changed` and `database_unavailable` survive here only for
# the case where re-pinning failed, which is usually the same mid-write window.
#
# `internal_error` is NOT retryable: it means a bug, and retrying reproduces it.
_RETRYABLE_CODES = frozenset({"database_changed", "database_unavailable", "read_only_unavailable"})


def _failure(code: str) -> dict[str, object]:
    retryable = code in _RETRYABLE_CODES
    envelope: dict[str, object] = {
        "schema": ENVELOPE_SCHEMA,
        "error": {"code": code, "retryable": retryable},
    }
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(envelope, sort_keys=True, separators=(",", ":")),
            }
        ],
        "structuredContent": envelope,
        "isError": True,
    }


def _success(envelope: dict[str, object]) -> dict[str, object]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(
                    envelope,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            }
        ],
        "structuredContent": envelope,
    }


def call_tool(
    service: LedgerMCPService,
    name: str,
    arguments: Mapping[str, object] | None,
) -> dict[str, object]:
    """Invoke one registered tool and return a typed content-free envelope."""
    try:
        values = _validated_arguments(name, arguments)
        if name == "ledger.status":
            envelope = service.status()
        elif name == "ledger.transaction.get":
            envelope = service.transaction(evidence_id=_evidence_id_argument(values))
        elif name == "ledger.balances":
            envelope = service.balances(
                as_of=_date_argument(values, "as_of"),
                change_since=(
                    _date_argument(values, "change_since") if "change_since" in values else None
                ),
                limit=_limit_argument(values),
                cursor=_cursor_argument(values),
            )
        elif name == "ledger.budgets":
            envelope = service.budgets(
                start_date=_date_argument(values, "start_date"),
                end_date=_date_argument(values, "end_date"),
            )
        elif name == "ledger.brief":
            envelope = service.brief(
                start_date=_date_argument(values, "start_date"),
                end_date=_date_argument(values, "end_date"),
                since=(_timestamp_argument(values, "since") if "since" in values else None),
            )
        elif name == "ledger.explain":
            envelope = service.explain(
                metric=_enum_argument(values, "metric", _EXPLANATION_METRICS),
                start_date=_date_argument(values, "start_date"),
                end_date=_date_argument(values, "end_date"),
                account_ref=_account_ref_argument(values),
                category=_category_argument(values),
            )
        elif name == "ledger.cash_plan":
            envelope = service.cash_plan(
                start_date=_date_argument(values, "start_date"),
                end_date=_date_argument(values, "end_date"),
                as_of=_date_argument(values, "as_of"),
            )
        elif name == "ledger.planning.definitions":
            exact_name = _strict_utf8(values["exact_name"]) if "exact_name" in values else None
            if exact_name is not None and (len(exact_name) > 200 or not exact_name.strip()):
                raise AgentQueryError
            envelope = service.planning_definitions(
                kind=_enum_argument(values, "kind", set(DEFINITION_KINDS))
                if "kind" in values
                else "all",
                exact_name=exact_name,
                account_ref=_account_ref_argument(values),
                limit=_limit_argument(values),
                cursor=_cursor_argument(values),
            )
        elif name == "ledger.plan_reconciliation":
            envelope = service.plan_reconciliation(
                start_date=_date_argument(values, "start_date"),
                end_date=_date_argument(values, "end_date"),
                as_of=_date_argument(values, "as_of"),
                limit=_limit_argument(values),
                cursor=_cursor_argument(values),
            )
        elif name == "ledger.net_worth":
            envelope = service.net_worth(
                as_of=_date_argument(values, "as_of"),
                limit=_limit_argument(values),
                cursor=_cursor_argument(values),
            )
        elif name == "ledger.investments.statements":
            envelope = service.investment_details(
                tool=name,
                start_date=_date_argument(values, "start_date"),
                end_date=_date_argument(values, "end_date"),
                account_ref=_account_ref_argument(values),
                limit=_limit_argument(values),
                cursor=_cursor_argument(values),
            )
        elif name in {
            "ledger.investments.statement.get",
            "ledger.investments.positions",
            "ledger.investments.activities",
        }:
            envelope = service.investment_details(
                tool=name,
                statement_ref=_statement_ref_argument(values),
                limit=_limit_argument(values),
                cursor=_cursor_argument(values),
            )
        elif name == "ledger.changes":
            envelope = service.changes(
                since=_timestamp_argument(values, "since"),
                client_generation=(
                    _generation_argument(values, "client_generation")
                    if "client_generation" in values
                    else None
                ),
                limit=_limit_argument(values),
                cursor=_cursor_argument(values),
            )
        elif name == "ledger.coverage":
            envelope = service.coverage(
                start_period=_month_argument(values, "start_period"),
                end_period=_month_argument(values, "end_period"),
                limit=_limit_argument(values),
                cursor=_cursor_argument(values),
            )
        elif name == "ledger.insights":
            envelope = service.insights(
                limit=_limit_argument(values),
                cursor=_cursor_argument(values),
            )
        elif name == "ledger.quality":
            envelope = service.quality(
                limit=_limit_argument(values),
                cursor=_cursor_argument(values),
            )
        elif name == "ledger.projection":
            envelope = service.projection(
                as_of=_date_argument(values, "as_of"),
                target_date=_date_argument(values, "target_date"),
                limit=_limit_argument(values),
                cursor=_cursor_argument(values),
            )
        elif name == "ledger.recurring":
            envelope = service.recurring(
                status=(
                    _enum_argument(values, "status", _RECURRING_STATUS_VALUES)
                    if "status" in values
                    else "active"
                ),
                limit=_limit_argument(values),
                cursor=_cursor_argument(values),
            )
        elif name == "ledger.transfers":
            envelope = service.transfers(
                start_date=_date_argument(values, "start_date"),
                end_date=_date_argument(values, "end_date"),
                limit=_limit_argument(values),
                cursor=_cursor_argument(values),
            )
        elif name == "ledger.dimensions":
            envelope = service.dimensions(
                start_date=_date_argument(values, "start_date"),
                end_date=_date_argument(values, "end_date"),
                flow=_enum_argument(values, "flow", _FLOW_VALUES),
                dimension=_enum_argument(values, "dimension", _DIMENSION_VALUES),
                contains=_contains_argument(values),
                limit=_limit_argument(values),
                cursor=_cursor_argument(values),
            )
        else:
            start_date = _date_argument(values, "start_date")
            end_date = _date_argument(values, "end_date")
            # Per tool, not shared: this branch serves both the aggregate and the
            # search, and only the search may admit the union value.
            flow = _enum_argument(
                values,
                "flow",
                _SEARCH_FLOW_VALUES if name == "ledger.transactions.search" else _FLOW_VALUES,
            )
            filters = _filters_argument(values)
            limit = _limit_argument(values)
            cursor = _cursor_argument(values)
            min_amount = _money_argument(values, "min_amount")
            max_amount = _money_argument(values, "max_amount")
            if name == "ledger.aggregate":
                group_by = _enum_argument(
                    values,
                    "group_by",
                    _GROUP_BY_VALUES,
                )
                envelope = service.aggregate(
                    start_date=start_date,
                    end_date=end_date,
                    flow=flow,
                    group_by=group_by,
                    filters=filters,
                    limit=limit,
                    cursor=cursor,
                    min_amount=min_amount,
                    max_amount=max_amount,
                )
            else:
                envelope = service.search(
                    start_date=start_date,
                    end_date=end_date,
                    flow=flow,
                    filters=filters,
                    limit=limit,
                    cursor=cursor,
                    min_amount=min_amount,
                    max_amount=max_amount,
                    order=_order_argument(values),
                )
        output_schema = _OUTPUT_SCHEMAS[name]
        if name.startswith("ledger.investments."):
            output_schema = next(
                cast(dict[str, object], schema["outputSchema"])
                for schema in tool_schemas(investment_vocabulary=service.investment_vocabulary)
                if schema["name"] == name
            )
        validate_json_schema(instance=envelope, schema=output_schema)
        return _success(envelope)
    except ValidationError:
        return _failure("corrupt_data")
    except SchemaError:
        return _failure("internal_error")
    except AgentQueryError as error:
        # An allowlist, so an unrecognized code can never invent an error name
        # the contract does not publish. `corrupt_data` belongs on it: the query
        # layer raises it BY NAME for a stored value it cannot read -- a
        # timestamp column that will not parse -- and translating that to
        # `invalid_argument` told the caller its request was malformed when
        # nothing about the request was. Both are non-retryable, so only the
        # diagnosis changes.
        code = (
            error.code
            if error.code
            in {"corrupt_data", "filter_too_broad", "invalid_argument", "invalid_cursor"}
            else "invalid_argument"
        )
        return _failure(code)
    except ReadOnlyDatabaseError as error:
        return _failure(error.code)
    except TypeError, ValueError, UnicodeError:
        return _failure("invalid_argument")
    except Exception:
        return _failure("internal_error")


def create_server(service: LedgerMCPService) -> Server[object]:
    """Register the exact Ledger contract on MCP SDK v2's public low-level API."""

    async def list_tools(
        _context: ServerRequestContext[object],
        _params: PaginatedRequestParams | None,
    ) -> ListToolsResult:
        return ListToolsResult(
            tools=[
                Tool.model_validate(copy.deepcopy(schema))
                for schema in tool_schemas(investment_vocabulary=service.investment_vocabulary)
            ]
        )

    async def protocol_call_tool(
        _context: ServerRequestContext[object],
        params: CallToolRequestParams,
    ) -> CallToolResult:
        # Ledger's dispatcher validates the name before inspecting arguments, so
        # unknown caller-controlled values cannot enter protocol output or logs.
        result = await asyncio.to_thread(call_tool, service, params.name, params.arguments)
        return CallToolResult.model_validate(result)

    return Server(
        "Ledger",
        version=__version__,
        instructions=SERVER_INSTRUCTIONS,
        on_list_tools=list_tools,
        on_call_tool=protocol_call_tool,
    )


async def _serve_stdio(server: Server[object]) -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


def run_stdio(db_path: Path) -> None:
    """Preflight one explicit database and own only the stdio transport."""
    service = LedgerMCPService(db_path)
    try:
        asyncio.run(_serve_stdio(create_server(service)))
    finally:
        service.close()


def _build_parser() -> argparse.ArgumentParser:
    parser = _ContentFreeArgumentParser(
        prog="ledger-mcp",
        allow_abbrev=False,
        exit_on_error=False,
    )
    parser.add_argument("--db", type=Path, required=True)
    return parser


class _ArgumentParsingError(Exception):
    pass


class _HelpRequestError(Exception):
    pass


class _ContentFreeArgumentParser(argparse.ArgumentParser):
    def error(self, _message: str) -> Never:
        raise _ArgumentParsingError

    def exit(self, status: int = 0, message: str | None = None) -> Never:
        if status == 0:
            raise _HelpRequestError
        raise _ArgumentParsingError


def main(argv: Sequence[str] | None = None) -> int:
    """Run the explicit local Ledger database over stdio."""
    try:
        arguments = _build_parser().parse_args(argv)
    except _HelpRequestError:
        return 0
    except argparse.ArgumentError, _ArgumentParsingError:
        sys.stderr.write("ledger_mcp_startup_failed:invalid_argument\n")
        return 2
    try:
        run_stdio(arguments.db)
        return 0
    except ReadOnlyDatabaseError as error:
        sys.stderr.write(f"ledger_mcp_startup_failed:{error.code}\n")
        return 2
    except Exception:
        sys.stderr.write("ledger_mcp_startup_failed:internal_error\n")
        return 2


__all__ = [
    "ERROR_ENVELOPE_SCHEMA",
    "INSTRUCTION_TRUST",
    "LedgerMCPService",
    "call_tool",
    "create_server",
    "main",
    "run_stdio",
]
