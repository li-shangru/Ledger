"""Exact wire contract for the distinct opt-in proposal filing server."""

from __future__ import annotations

import copy
import hashlib
import json

from ledger.proposals.intents import planning_intent_schema

WRITE_ENVELOPE_SCHEMA = "ledger.mcp.write.v1"

WRITE_ERROR_ENVELOPE_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "schema": {"const": WRITE_ENVELOPE_SCHEMA},
        "error": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "code": {
                    "type": "string",
                    "enum": ["internal_error", "invalid_argument", "store_unavailable"],
                },
                "retryable": {"type": "boolean"},
            },
            "required": ["code", "retryable"],
        },
    },
    "required": ["schema", "error"],
}

_SUCCESS_ENVELOPE_SCHEMA: dict[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "schema": {"const": WRITE_ENVELOPE_SCHEMA},
        "data": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "proposal_id": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
                },
                "status": {"const": "pending"},
            },
            "required": ["proposal_id", "status"],
        },
    },
    "required": ["schema", "data"],
}


def _planning_tool_schema() -> dict[str, object]:
    intent = planning_intent_schema()
    definitions = intent.pop("$defs", {})
    success = copy.deepcopy(_SUCCESS_ENVELOPE_SCHEMA)
    data = success["properties"]["data"]  # type: ignore[index]
    data["properties"]["proposal_revision"] = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    data["required"].append("proposal_revision")
    return {
        "name": "ledger.proposals.planning.file",
        "description": (
            "File one explicit typed budget, schedule, or spending-preference intent in the "
            "isolated proposal store. Resolve essential target/account/category/currency/cadence/"
            "provenance ambiguity using read-only planning discovery before filing; references "
            "include their exact current revisions. No financial database is opened, and no "
            "budget, schedule, or preference is changed. The operator must review the local "
            "exact before/after preview and apply its exact proposal and preview revisions. "
            "The reply contains only the pending proposal id and revision; content is inert."
        ),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
        "inputSchema": {
            "$defs": definitions,
            "type": "object",
            "additionalProperties": False,
            "properties": {"intent": intent},
            "required": ["intent"],
        },
        "outputSchema": {
            "type": "object",
            "oneOf": [success, copy.deepcopy(WRITE_ERROR_ENVELOPE_SCHEMA)],
        },
    }


_TOOL_SCHEMAS: tuple[dict[str, object], ...] = (
    {
        "name": "ledger.proposals.file",
        "description": (
            "File one deferred merchant-rule teaching proposal in the isolated local "
            "proposal store. This does not open or mutate the financial ledger; an operator "
            "must separately apply the pending proposal."
        ),
        "annotations": {
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": False,
            "openWorldHint": False,
        },
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "merchant_norm": {"type": "string", "minLength": 1, "maxLength": 200},
                "pattern": {"type": "string", "minLength": 1, "maxLength": 2000},
                "category": {"type": "string", "minLength": 1, "maxLength": 100},
                "evidence_id": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
            },
            "required": ["merchant_norm", "pattern", "category", "evidence_id"],
        },
        "outputSchema": {
            "type": "object",
            "oneOf": [
                copy.deepcopy(_SUCCESS_ENVELOPE_SCHEMA),
                copy.deepcopy(WRITE_ERROR_ENVELOPE_SCHEMA),
            ],
        },
    },
    _planning_tool_schema(),
)


def write_tool_schemas() -> list[dict[str, object]]:
    """Return isolated copies of the opt-in proposal-filing contracts."""
    return copy.deepcopy(list(_TOOL_SCHEMAS))


def write_contract_document() -> dict[str, object]:
    return {
        "errorEnvelope": copy.deepcopy(WRITE_ERROR_ENVELOPE_SCHEMA),
        "tools": write_tool_schemas(),
    }


def write_contract_digest() -> str:
    encoded = json.dumps(
        write_contract_document(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "WRITE_ENVELOPE_SCHEMA",
    "WRITE_ERROR_ENVELOPE_SCHEMA",
    "write_contract_digest",
    "write_contract_document",
    "write_tool_schemas",
]
