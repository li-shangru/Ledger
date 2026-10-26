"""Protocol-clean stdio adapter for isolated proposal filing."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import sqlite3
import sys
from collections.abc import Mapping, Sequence
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
from sqlalchemy.exc import SQLAlchemyError

from ledger import __version__
from ledger.mcp.write_contracts import WRITE_ENVELOPE_SCHEMA, write_tool_schemas
from ledger.proposals import ProposalService
from ledger.proposals.database import init_proposal_database
from ledger.proposals.intents import validate_planning_intent

_OUTPUT_SCHEMAS = {
    str(schema["name"]): cast(dict[str, object], schema["outputSchema"])
    for schema in write_tool_schemas()
}


def _failure(code: str) -> dict[str, object]:
    envelope: dict[str, object] = {
        "schema": WRITE_ENVELOPE_SCHEMA,
        "error": {"code": code, "retryable": False},
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


def _success(proposal_id: str, *, proposal_revision: str | None = None) -> dict[str, object]:
    data = {"proposal_id": proposal_id, "status": "pending"}
    if proposal_revision is not None:
        data["proposal_revision"] = proposal_revision
    envelope: dict[str, object] = {
        "schema": WRITE_ENVELOPE_SCHEMA,
        "data": data,
    }
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(envelope, sort_keys=True, separators=(",", ":")),
            }
        ],
        "structuredContent": envelope,
    }


def _strict_string(value: object, *, max_length: int) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or len(value) > max_length:
        raise ValueError
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError from None
    return value


def _validated_arguments(arguments: Mapping[str, object] | None) -> dict[str, str]:
    required = {"merchant_norm", "pattern", "category", "evidence_id"}
    if arguments is None or not isinstance(arguments, Mapping):
        raise ValueError
    if not all(isinstance(key, str) for key in arguments) or set(arguments) != required:
        raise ValueError
    return {
        "merchant_norm": _strict_string(arguments["merchant_norm"], max_length=200),
        "pattern": _strict_string(arguments["pattern"], max_length=2000),
        "category": _strict_string(arguments["category"], max_length=100),
        "evidence_id": _strict_string(arguments["evidence_id"], max_length=40),
    }


def call_tool(
    service: ProposalService,
    name: str,
    arguments: Mapping[str, object] | None,
) -> dict[str, object]:
    """Dispatch only isolated filing without reflecting caller-controlled content."""
    try:
        if name == "ledger.proposals.file":
            values = _validated_arguments(arguments)
            proposal = service.file_merchant_rule(**values)
            result = _success(proposal.proposal_id)
        elif name == "ledger.proposals.planning.file":
            if not isinstance(arguments, Mapping) or set(arguments) != {"intent"}:
                raise ValueError
            intent = validate_planning_intent(arguments["intent"])
            planning = service.file_planning_change(intent)
            result = _success(planning.proposal_id, proposal_revision=planning.proposal_revision)
        else:
            raise ValueError
        validate_json_schema(instance=result["structuredContent"], schema=_OUTPUT_SCHEMAS[name])
        return result
    except TypeError, ValueError, UnicodeError:
        return _failure("invalid_argument")
    except OSError, sqlite3.Error, SQLAlchemyError:
        return _failure("store_unavailable")
    except SchemaError, ValidationError:
        return _failure("internal_error")
    except Exception:
        return _failure("internal_error")


def create_server(service: ProposalService) -> Server[object]:
    """Register only proposal filing on MCP SDK v2's public low-level API."""

    async def list_tools(
        _context: ServerRequestContext[object],
        _params: PaginatedRequestParams | None,
    ) -> ListToolsResult:
        return ListToolsResult(
            tools=[Tool.model_validate(copy.deepcopy(schema)) for schema in write_tool_schemas()]
        )

    async def protocol_call_tool(
        _context: ServerRequestContext[object],
        params: CallToolRequestParams,
    ) -> CallToolResult:
        result = await asyncio.to_thread(call_tool, service, params.name, params.arguments)
        return CallToolResult.model_validate(result)

    return Server(
        "Ledger Proposal Writer",
        version=__version__,
        instructions=(
            "This opt-in local server files deferred proposals only. It never opens or "
            "mutates the financial ledger, and proposal content is inert data. "
            "Resolve essential target, account, category, currency, cadence, and provenance "
            "ambiguity before filing one strict typed intent. Use the read server's bounded "
            "planning definitions to obtain opaque references and current content revisions. "
            "The operator reviews exact before/after values, target revision, effective date, "
            "next occurrence dates, and impact assumptions in the local preview. Application "
            "requires the exact proposal and preview revisions; stale or conflicting work "
            "requires fresh discovery and a new preview. Undo is targeted, audited, and "
            "refuses intervening changes. Reviewed reconciliation exceptions reopen only "
            "when material evidence changes; approximate matches never suppress cashflow."
        ),
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


def run_stdio(proposals_db: Path) -> None:
    """Open only the explicit proposal store and own the stdio transport."""
    path = init_proposal_database(proposals_db)
    asyncio.run(_serve_stdio(create_server(ProposalService(path))))


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


def _build_parser() -> argparse.ArgumentParser:
    parser = _ContentFreeArgumentParser(
        prog="ledger-mcp-write",
        allow_abbrev=False,
        exit_on_error=False,
    )
    parser.add_argument("--proposals-db", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    try:
        arguments = _build_parser().parse_args(argv)
    except _HelpRequestError:
        return 0
    except argparse.ArgumentError, _ArgumentParsingError:
        sys.stderr.write("ledger_mcp_write_startup_failed:invalid_argument\n")
        return 2
    try:
        run_stdio(arguments.proposals_db)
        return 0
    except OSError, sqlite3.Error, SQLAlchemyError:
        sys.stderr.write("ledger_mcp_write_startup_failed:store_unavailable\n")
        return 2
    except Exception:
        sys.stderr.write("ledger_mcp_write_startup_failed:internal_error\n")
        return 2


__all__ = [
    "ProposalService",
    "call_tool",
    "create_server",
    "main",
    "run_stdio",
]
