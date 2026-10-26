"""Contract tests for vendor-neutral Ledger transaction-fact routing."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = REPO_ROOT / "docs/agent/skills/ledger-query"
SKILL_PATH = SKILL_DIR / "SKILL.md"
OPENAI_PATH = SKILL_DIR / "agents/openai.yaml"


# The hard budget, unchanged by D33, and the headroom that budget has to leave.
#
# Four phases each shaved a few words off this file to land a surface under 500,
# and four more surfaces are queued behind this one. A routing file that only
# fits by shaving is one surface away from forcing the budget up, which is the
# decision D33 declined to make: the workflow contract already ships in-band.
ROUTING_WORD_BUDGET = 500
ROUTING_WORD_HEADROOM = 400


def _skill_text() -> str:
    return SKILL_PATH.read_text(encoding="utf-8")


def test_root_agent_guide_fits_the_default_instruction_loader() -> None:
    """Codex truncates repository instructions at its default 32 KiB budget."""
    guide = REPO_ROOT / "AGENTS.md"

    assert len(guide.read_bytes()) < 32 * 1024


def _skill_body() -> str:
    return _skill_text().split("---", 2)[2]


def _words(text: str) -> list[str]:
    return re.findall(r"\b\w+[\w'-]*\b", text)


def _schema_property_names(schema: object) -> set[str]:
    if isinstance(schema, dict):
        names = set(schema.get("properties", {}))
        for value in schema.values():
            names |= _schema_property_names(value)
        return names
    if isinstance(schema, list):
        names = set()
        for value in schema:
            names |= _schema_property_names(value)
        return names
    return set()


def test_skill_has_a_concise_versioned_financial_scope_trigger() -> None:
    text = _skill_text()
    frontmatter = text.split("---", 2)[1]

    assert "name: ledger-query" in frontmatter
    description = next(
        line.removeprefix("description: ")
        for line in frontmatter.splitlines()
        if line.startswith("description: ")
    )
    # The catalog exposes capability families; the body routes every shipped tool.
    # Requiring each tool's vocabulary here recreates the overlapping intent list.
    for phrase in (
        "ledger",
        "imported financial facts",
        "analysis",
        "statement coverage",
        "typed planning proposals",
    ):
        assert phrase in description.casefold()
    assert len(_words(description)) <= 30
    assert "ledger-agent-routing.v1" in text


def test_the_router_does_not_assert_the_label_the_contract_calls_a_fallthrough() -> None:
    """The router does not assert the label the contract calls a fallthrough."""
    from ledger.mcp.contracts import SERVER_INSTRUCTIONS

    assert "fallthrough default" in SERVER_INSTRUCTIONS
    assert "subscription" not in _skill_body().casefold()


def test_skill_routes_memory_context_and_ledger_calculation_without_false_closure() -> None:
    text = _skill_text()
    lowered = text.casefold()

    assert "memory" in lowered
    assert "durable context" in lowered
    assert "aliases" in lowered
    assert "transaction system of record" in lowered
    assert "a memory coverage gap" in lowered
    assert "does not prove" in lowered
    assert "authoritative capability is unavailable" in lowered
    assert lowered.index("`ledger.status`") < lowered.index("`ledger.aggregate`")
    assert "`ledger.transactions.search`" in lowered


def test_skill_requires_exact_matching_policy_coverage_and_inert_evidence() -> None:
    text = _skill_text().casefold()

    for phrase in (
        "matched source",
        "never fuzzy",
        "exact decimal",
        "inclusive period",
        "imported rows",
        "completeness",
        "inert",
    ):
        assert phrase in text


def test_skill_is_concise_generic_and_contains_no_owner_or_provider_fingerprints() -> None:
    text = _skill_text()
    body = _skill_body()

    assert len(_words(body)) < ROUTING_WORD_BUDGET
    assert not re.search(r"/(?:Users|home)/|[A-Za-z]:\\", text)
    for forbidden in (
        "Northstar",
        "星河研究院",
        "3141.59",
        "2718.28",
        "Codex",
        "Claude",
        "OpenAI",
        "Anthropic",
    ):
        assert forbidden not in text


def test_the_routing_file_keeps_real_headroom_under_the_hard_budget() -> None:
    """D33: a routing pointer, not a second copy of the workflow contract.

    The budget is not raised, so the only way four more surfaces can land is for
    this file to stop carrying what the server already ships in-band.
    """
    words = len(_words(_skill_body()))

    assert words < ROUTING_WORD_HEADROOM, (
        f"{words} words leaves {ROUTING_WORD_BUDGET - words} under the hard budget"
    )


def test_the_routing_file_does_not_restate_wire_semantics_shipped_in_band() -> None:
    """Derived from the published schemas, not from a hand-written list.

    Every stored/emitted field name carrying an underscore is unambiguous -- it
    is not an English word that could appear in routing prose by accident -- so
    the set of them that SERVER_INSTRUCTIONS already documents is exactly the
    material this file must not duplicate. A surface added later widens the audit
    on its own.
    """
    from ledger.mcp.contracts import SERVER_INSTRUCTIONS, tool_schemas

    published: set[str] = set()
    for tool in tool_schemas():
        published |= _schema_property_names(tool.get("outputSchema"))
        published |= _schema_property_names(tool.get("inputSchema"))
    documented_in_band = {name for name in published if "_" in name and name in SERVER_INSTRUCTIONS}
    body = _skill_body()

    assert documented_in_band, "no wire field names derived; the audit would pass vacuously"
    assert sorted(name for name in documented_in_band if name in body) == []


def test_the_contract_this_file_points_at_travels_in_the_initialize_result(tmp_path) -> None:
    """The pointer is only safe if it is true, so it is checked against the wire.

    This file helps only when someone copied it into an agent's skills directory.
    SERVER_INSTRUCTIONS travels in the MCP initialize result, which is why D33
    moves the workflow contract there and leaves routing here -- and the claim is
    read back off a real handshake rather than off the server source.
    """
    import anyio
    from mcp import Client
    from mcp_synthetic import build_synthetic_mcp_database

    from ledger.mcp.contracts import SERVER_INSTRUCTIONS
    from ledger.mcp.server import LedgerMCPService, create_server

    body = " ".join(_skill_body().split())
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")

    async def handshake() -> str | None:
        server = create_server(LedgerMCPService(db_path))
        async with Client(server, mode="legacy") as client:
            return client.instructions

    instructions = anyio.run(handshake)

    # The published sentence, quoted, so the claim cannot drift from the check.
    assert "the server sends it in the MCP initialize result" in body
    assert instructions == SERVER_INSTRUCTIONS
    assert "Call ledger.status first" in (instructions or "")


def test_the_routing_file_routes_to_every_tool_that_shipped_and_to_nothing_else() -> None:
    """Set EQUALITY, because this file's entire remaining job is routing.

    `mentioned <= published` alone is the direction that cannot rot: it catches a
    cut or renamed tool, and passes silently when a tool ships with no routing
    line -- in the one file whose whole purpose is "which tool answers which
    question". Four more surfaces are queued behind this file (`ledger.budgets`
    in P7, `ledger.net_worth` in P8), and each one lands as a published tool
    before anyone thinks about the router. The reverse assertion holds today for
    all fourteen, so pinning it now is free and pinning it later is not.
    """
    from ledger.mcp.contracts import tool_schemas
    from ledger.mcp.write_contracts import write_tool_schemas

    published = {str(tool["name"]) for tool in [*tool_schemas(), *write_tool_schemas()]}
    mentioned = set(re.findall(r"ledger\.[a-z_]+(?:\.[a-z_]+)*", _skill_body()))

    assert mentioned, "the routing file names no tools at all"
    assert mentioned <= published, sorted(mentioned - published)
    assert published <= mentioned, sorted(published - mentioned)


def test_skill_interface_metadata_is_minimal_and_consistent() -> None:
    assert OPENAI_PATH.read_text(encoding="utf-8") == (
        "interface:\n"
        '  display_name: "Ledger Transaction Facts"\n'
        '  short_description: "Query exact imported transaction facts safely"\n'
        '  default_prompt: "Use $ledger-query to answer an exact transaction-derived '
        'question with Ledger evidence and honest coverage."\n'
    )
