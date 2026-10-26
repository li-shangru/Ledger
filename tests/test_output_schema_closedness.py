"""No undeclared key crosses any tool's output schema (D16, spec 2.29).

`_object_schema` sets `additionalProperties: False` on everything it builds, and
`server.py` turns a `ValidationError` into a payload-dropping `corrupt_data`, so
a closed schema is what makes an undeclared value a refusal rather than a leak.
But a bare `{"type": "object"}` sub-schema is fully OPEN, and one such node
ACCEPTED a raw finding dict carrying an account slug in a live probe. Until this
file existed the guarantee was a convention carried by one helper plus review:
`tests/test_mcp_contract.py` checks `inputSchema` only.

The invariant is about the KEYS AN INSTANCE MAY CARRY, not about the literal
presence of one keyword. A node forbids undeclared keys when it says
`additionalProperties: false`, or when it dispatches through `oneOf`/`anyOf` and
every branch that could match an object does. The second form is not a loophole,
it is how every tool's root node is closed: the root is
`{"oneOf": [success, error], "type": "object"}`, where an extra top-level key
fails the success branch (closed by `_object_schema`) and fails the error branch
too, which is closed and additionally requires `error`. The root CANNOT be closed
literally -- `additionalProperties: false` beside no `properties` forbids every
key, so every envelope would be invalid -- and that is exactly why the invariant
is stated as "no undeclared key crosses" rather than "every node repeats one
keyword".

Applicator subschemas -- `if`, `then`, `else`, `not`, and `allOf` members -- are
exempt, and must be. They constrain an instance whose permitted keys are already
fixed by the declaration node they sit beside: `additionalProperties` is
evaluated against the `properties` ADJACENT to it and is never widened by an
applicator, so a key an applicator names but the declaration node does not
declare is rejected regardless. Closing `if` would make it match only an object
with nothing else in it, i.e. never, which is strictly worse than leaving it
open. The exemption is not taken on trust:
`test_the_applicator_exemption_rests_on_a_closed_declaration` proves every
property an applicator names is also declared by the closed schema that governs
it, and `test_the_walker_reports_the_probe_that_proved_this_necessary` proves
the walker actually reports the open node the P1 probe found. The exemption is
also stated in docs/agent/mcp.md, and a test compares the two rather than trusting that
someone kept them in step.

The walker itself gets a positive control per descent keyword, because losing a
descent is otherwise SILENT: dropping `items` from `_CHILD_SCHEMA_VALUES` and
changing nothing else once left every test here green against the real
contracts, while three array-reachable leaks went unreported. `items` is the
descent every per-row payload rides on -- findings, checks, and the entry arrays
of coverage, balances, projection and changes -- and it is handled in both
spellings, 2020-12's single schema and draft-07's positional list.

The behavioural twin is checked on BOTH root branches. The error envelope needs
no fixture but proves nothing about the success branch, which fails a tainted
error envelope on its missing `required` keys long before closedness is
consulted -- opening the success branch leaves those parametrizations green.
Real success envelopes, driven through `call_tool` over the synthetic database,
are what covers the branch that carries the data.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import jsonschema
import pytest
from mcp_synthetic import build_synthetic_mcp_database
from test_mcp_protocol import _every_tool_call

from ledger.mcp.contracts import tool_schemas
from ledger.mcp.server import call_tool
from ledger.mcp.service import LedgerMCPService

# Keywords whose subschemas describe a CHILD value: reaching one starts a fresh
# declaration, because the child's own keys are governed by it.
_CHILD_SCHEMA_MAPS = ("properties", "patternProperties", "$defs", "definitions")
_CHILD_SCHEMA_VALUES = ("additionalProperties", "items", "contains", "propertyNames")
# `items` appears in both: 2020-12 spells it as one schema for every member and
# draft-07 spells it as a positional list. The isinstance checks pick the form,
# and handling only the first skipped a whole spelling in silence.
_CHILD_SCHEMA_ARRAYS = ("prefixItems", "items")
# Keywords whose branches must EACH match the whole instance for the instance to
# be valid under the parent, so a closed branch closes the instance.
_UNION_KEYWORDS = ("oneOf", "anyOf")
# Keywords that add constraints to an instance the parent already declares.
# Nothing under one of these can admit a key the declaration node forbids.
_APPLICATOR_VALUES = ("if", "then", "else", "not")
_APPLICATOR_ARRAYS = ("allOf",)


@dataclass(frozen=True)
class _Node:
    path: str
    schema: Mapping[str, object]
    under_applicator: bool


def _may_match_an_object(node: object) -> bool:
    """Could an object instance satisfy this schema at all?

    A schema that cannot match an object cannot let an undeclared key cross it,
    so it is outside the invariant rather than exempted from it.
    """
    if not isinstance(node, Mapping):
        return False
    declared = node.get("type")
    if isinstance(declared, str):
        return declared == "object"
    if isinstance(declared, list):
        return "object" in declared
    if "const" in node:
        return isinstance(node["const"], Mapping)
    if "enum" in node:
        return any(isinstance(value, Mapping) for value in node["enum"])  # type: ignore[union-attr]
    return True


def _forbids_undeclared_keys(node: Mapping[str, object]) -> bool:
    if node.get("additionalProperties") is False:
        return True
    for keyword in _UNION_KEYWORDS:
        branches = node.get(keyword)
        if not isinstance(branches, list) or not branches:
            continue
        if all(
            not _may_match_an_object(branch)
            or (isinstance(branch, Mapping) and _forbids_undeclared_keys(branch))
            for branch in branches
        ):
            return True
    return False


def _schema_nodes(node: object, *, path: str, under_applicator: bool) -> Iterator[_Node]:
    """Every schema node, tagged with whether it merely constrains its parent.

    Keyword-aware rather than a blind dict walk: a blind walk descends INTO a
    `properties` map as though the map were itself a schema, so a property
    literally named `properties` or `type` would be read as one.
    """
    if not isinstance(node, Mapping):
        return
    yield _Node(path=path, schema=node, under_applicator=under_applicator)
    for keyword in _CHILD_SCHEMA_MAPS:
        children = node.get(keyword)
        if isinstance(children, Mapping):
            for name, child in children.items():
                yield from _schema_nodes(
                    child,
                    path=f"{path}.{keyword}.{name}",
                    under_applicator=under_applicator,
                )
    for keyword in _CHILD_SCHEMA_VALUES:
        child = node.get(keyword)
        if isinstance(child, Mapping):
            yield from _schema_nodes(
                child, path=f"{path}.{keyword}", under_applicator=under_applicator
            )
    for keyword in (*_CHILD_SCHEMA_ARRAYS, *_UNION_KEYWORDS):
        branches = node.get(keyword)
        if isinstance(branches, list):
            for index, branch in enumerate(branches):
                yield from _schema_nodes(
                    branch,
                    path=f"{path}.{keyword}[{index}]",
                    under_applicator=under_applicator,
                )
    # Sticky from here down: everything beneath an applicator is part of the same
    # constraint, including its own `properties`.
    for keyword in _APPLICATOR_VALUES:
        child = node.get(keyword)
        if isinstance(child, Mapping):
            yield from _schema_nodes(child, path=f"{path}.{keyword}", under_applicator=True)
    for keyword in _APPLICATOR_ARRAYS:
        branches = node.get(keyword)
        if isinstance(branches, list):
            for index, branch in enumerate(branches):
                yield from _schema_nodes(
                    branch, path=f"{path}.{keyword}[{index}]", under_applicator=True
                )


def _open_object_nodes(output_schema: object, *, name: str) -> list[str]:
    return [
        f"{name}{node.path}"
        for node in _schema_nodes(output_schema, path="$", under_applicator=False)
        if not node.under_applicator
        and _may_match_an_object(node.schema)
        and not _forbids_undeclared_keys(node.schema)
    ]


def test_every_output_schema_object_node_forbids_an_undeclared_key() -> None:
    open_nodes = [
        entry
        for tool in tool_schemas()
        for entry in _open_object_nodes(tool["outputSchema"], name=str(tool["name"]))
    ]

    assert not open_nodes, f"open object nodes would let an undeclared key cross: {open_nodes}"


def _exempt_only_nodes() -> list[str]:
    """Published nodes that are open on their own and closed only by the exemption.

    Neither `additionalProperties: false` nor a union of closed branches, and
    reported by the invariant test only because they sit under an applicator.
    These are the counterexamples to a README sentence naming two forms.
    """
    return [
        f"{tool['name']}{node.path}"
        for tool in tool_schemas()
        for node in _schema_nodes(tool["outputSchema"], path="$", under_applicator=False)
        if node.under_applicator
        and _may_match_an_object(node.schema)
        and not _forbids_undeclared_keys(node.schema)
    ]


def test_the_mcp_reference_states_the_exemption_the_shipped_schema_relies_on() -> None:
    """The documented exemption names every applicator and its closure boundary.

    The schema walker below proves the adjacent-declaration premise against all
    published schemas; documentation does not duplicate volatile node counts.
    """
    readme = (Path(__file__).resolve().parents[1] / "docs/agent/mcp.md").read_text(encoding="utf-8")
    paragraph = next(
        block
        for block in readme.split("\n\n")
        if "Every object node in every tool's `outputSchema` is closed" in block
    )
    exempt = _exempt_only_nodes()

    # The exemption is load-bearing on the shipped surface, not decorative: if
    # this were empty the README could name two forms and still be true.
    assert exempt, "no node relies on the exemption, so this test proves nothing"
    assert "applicator" in paragraph
    for keyword in (*_APPLICATOR_VALUES, *_APPLICATOR_ARRAYS):
        assert f"`{keyword}`" in paragraph, keyword
    # And the paragraph says WHY they are exempt rather than merely listing them:
    # the adjacent declaration is what forbids the key, so an applicator cannot
    # widen anything.
    assert "never widened by an applicator" in paragraph


def test_the_walk_actually_reaches_every_tool_and_its_nested_blocks() -> None:
    """Guards the invariant above against passing because it walked nothing.

    A checker that returns an empty list for a schema it never descended into is
    indistinguishable from a closed surface, and this file is the only thing
    standing between a bare `{"type": "object"}` and the wire.
    """
    tools = tool_schemas()
    reached = {
        str(tool["name"]): [
            node
            for node in _schema_nodes(tool["outputSchema"], path="$", under_applicator=False)
            if _may_match_an_object(node.schema)
        ]
        for tool in tools
    }

    assert len(tools) >= 11
    for name, nodes in reached.items():
        assert len(nodes) >= 3, name
        # The nested data block, not just the envelope and its two branches.
        assert any(".properties.data" in node.path for node in nodes), name


def _declares_an_object_array(node: object) -> bool:
    """A blind scan for an array whose members may be objects.

    Deliberately naive and independent of `_schema_nodes`: it is the control's
    job to disagree with the walker when the walker stops descending, and a
    control sharing the walker's traversal could not.
    """
    if isinstance(node, Mapping):
        if node.get("type") == "array" and _may_match_an_object(node.get("items")):
            return True
        return any(_declares_an_object_array(value) for value in node.values())
    if isinstance(node, list):
        return any(_declares_an_object_array(value) for value in node)
    return False


def test_the_walk_reaches_the_row_payloads_under_every_declared_array() -> None:
    """`.properties.data` is reached without ever descending an array.

    It is a direct property of the success branch, so the check above survives a
    walker that dropped `items` entirely -- proved by a targeted regression that
    left all 18 tests in this file green. Every per-row payload on this wire is
    under an `items`: a finding entry, a check entry, and the entry arrays of
    coverage, balances, projection and changes. Which tools have one is derived
    from the schemas, so a new row-bearing tool is covered on arrival and the
    two tools that return a single record are not asserted to have arrays they
    do not declare.
    """
    with_arrays = set()
    reached_members = set()
    for tool in tool_schemas():
        name = str(tool["name"])
        if _declares_an_object_array(tool["outputSchema"]):
            with_arrays.add(name)
        if any(
            ".items" in node.path or ".prefixItems[" in node.path
            for node in _schema_nodes(tool["outputSchema"], path="$", under_applicator=False)
            if _may_match_an_object(node.schema)
        ):
            reached_members.add(name)

    assert len(with_arrays) >= 11, sorted(with_arrays)
    assert reached_members == with_arrays


# One probe per descent keyword, each placing an OPEN node exactly one step past
# the keyword under a CLOSED root, so the walker's report is the whole evidence
# that the step happened. Written out rather than derived from the tuples below:
# a control generated from `_CHILD_SCHEMA_VALUES` disappears together with the
# entry it was meant to guard, which is precisely the silence being fixed.
_OPEN_NODE: dict[str, object] = {"type": "object"}


@dataclass(frozen=True)
class _Descent:
    keyword: str
    probe: Mapping[str, object]
    path: str
    # Applicators are exempt from the invariant, so an open node under one is
    # never REPORTED. What is observable is that the walk reached it and tagged
    # it as constraint rather than declaration.
    exempt: bool = False


def _closed_root(**keywords: object) -> dict[str, object]:
    return {"additionalProperties": False, "type": "object", **keywords}


_DESCENT_CONTROLS: tuple[_Descent, ...] = (
    _Descent("properties", _closed_root(properties={"x": _OPEN_NODE}), "$.properties.x"),
    _Descent(
        "patternProperties",
        _closed_root(patternProperties={"^x$": _OPEN_NODE}),
        "$.patternProperties.^x$",
    ),
    _Descent("$defs", _closed_root(**{"$defs": {"x": _OPEN_NODE}}), "$.$defs.x"),
    _Descent("definitions", _closed_root(definitions={"x": _OPEN_NODE}), "$.definitions.x"),
    # `additionalProperties` as a SCHEMA rather than `false`: the root is closed
    # by its union branch, and the value schema governing every undeclared key is
    # the open node.
    _Descent(
        "additionalProperties",
        {
            "additionalProperties": _OPEN_NODE,
            "oneOf": [{"additionalProperties": False}],
            "type": "object",
        },
        "$.additionalProperties",
    ),
    # The descent every per-row payload rides on, in both spellings: 2020-12's
    # single schema and draft-07's list form. The walker handled only the first,
    # so a schema written the other way was skipped in silence.
    _Descent("items", _closed_root(items=_OPEN_NODE), "$.items"),
    _Descent("items", _closed_root(items=[_OPEN_NODE]), "$.items[0]"),
    _Descent("contains", _closed_root(contains=_OPEN_NODE), "$.contains"),
    _Descent("propertyNames", _closed_root(propertyNames=_OPEN_NODE), "$.propertyNames"),
    _Descent("prefixItems", _closed_root(prefixItems=[_OPEN_NODE]), "$.prefixItems[0]"),
    _Descent("oneOf", _closed_root(oneOf=[_OPEN_NODE]), "$.oneOf[0]"),
    _Descent("anyOf", _closed_root(anyOf=[_OPEN_NODE]), "$.anyOf[0]"),
    _Descent("if", _closed_root(**{"if": _OPEN_NODE}), "$.if", exempt=True),
    _Descent("then", _closed_root(then=_OPEN_NODE), "$.then", exempt=True),
    _Descent("else", _closed_root(**{"else": _OPEN_NODE}), "$.else", exempt=True),
    _Descent("not", _closed_root(**{"not": _OPEN_NODE}), "$.not", exempt=True),
    _Descent("allOf", _closed_root(allOf=[_OPEN_NODE]), "$.allOf[0]", exempt=True),
)


def test_every_descent_keyword_has_a_control_and_no_control_is_stale() -> None:
    """Losing one entry from a descent tuple must not be a silent no-op.

    Proved necessary by a targeted regression: dropping `items` from
    `_CHILD_SCHEMA_VALUES` and changing nothing else left all 18 tests in this
    file green against the real contracts, and three separate leaks reachable
    only through an array -- an open `items` schema, an open node under `items`,
    and an open node in a `oneOf` branch under `items` -- all went unreported.
    Set equality in both directions, so a new keyword arrives with a control and
    a removed one takes its control with it.
    """
    walked = {
        *_CHILD_SCHEMA_MAPS,
        *_CHILD_SCHEMA_VALUES,
        *_CHILD_SCHEMA_ARRAYS,
        *_UNION_KEYWORDS,
        *_APPLICATOR_VALUES,
        *_APPLICATOR_ARRAYS,
    }

    assert {control.keyword for control in _DESCENT_CONTROLS} == walked


@pytest.mark.parametrize(
    "control", _DESCENT_CONTROLS, ids=lambda control: f"{control.keyword}{control.path}"
)
def test_the_walker_actually_descends_this_keyword(control: _Descent) -> None:
    reported = _open_object_nodes(control.probe, name="probe")
    visited = {
        node.path: node for node in _schema_nodes(control.probe, path="$", under_applicator=False)
    }

    assert control.path in visited, sorted(visited)
    assert visited[control.path].under_applicator is control.exempt
    assert reported == ([] if control.exempt else [f"probe{control.path}"])


def test_an_open_node_nested_inside_an_array_member_is_reported() -> None:
    """The row-payload shape itself, not just a one-step probe.

    `ledger.insights` puts a 7-branch `oneOf` under `entries.items`, and the
    predecessor of those branches is the node spec 2.29 caught accepting a raw
    finding dict with an account slug in it. Reaching that node needs
    `properties` -> `items` -> `oneOf` -> `properties`, and a walk that stops at
    any of the four reports nothing while the surface stays open.
    """
    probe = _closed_root(
        properties={
            "entries": {
                "items": {"oneOf": [_closed_root(properties={"evidence": _OPEN_NODE})]},
                "type": "array",
            }
        }
    )

    assert _open_object_nodes(probe, name="probe") == [
        "probe$.properties.entries.items.oneOf[0].properties.evidence"
    ]


def test_the_walker_reports_the_probe_that_proved_this_necessary() -> None:
    """The exact node shape spec 2.29 found accepting a raw finding dict.

    A bare `{"type": "object"}` sitting where a payload lands: `_object_schema`
    was never called for it, so nothing forbids the keys of whatever is put
    there. If the walker cannot see this, the test above proves nothing.
    """
    probe = {
        "oneOf": [
            {
                "additionalProperties": False,
                "properties": {
                    "data": {
                        "additionalProperties": False,
                        "properties": {"evidence": {"type": "object"}},
                        "type": "object",
                    }
                },
                "type": "object",
            }
        ],
        "type": "object",
    }

    assert _open_object_nodes(probe, name="probe.tool") == [
        "probe.tool$.oneOf[0].properties.data.properties.evidence"
    ]
    # And the leak the open node permits is real, not theoretical.
    jsonschema.validate(
        {"data": {"evidence": {"account_type": "cedar-red-silver-mastercard-9104"}}},
        probe,
    )


def test_the_applicator_exemption_is_narrow_and_an_open_declaration_is_not_exempt() -> None:
    """An open `if` is exempt; an open node beside it, on the same instance, is not.

    Written as one test over one schema so the exemption cannot silently widen
    into "anything reachable from an `allOf` is fine": the `then` branch's own
    `properties.policy` is a child DECLARATION, and it is reported.
    """
    schema = {
        "additionalProperties": False,
        "allOf": [
            {
                "if": {"properties": {"data": {"properties": {"flow": {"const": "any"}}}}},
                "then": {"properties": {"policy": {"type": "object"}}},
            }
        ],
        "properties": {"data": {"type": "object"}},
        "type": "object",
    }

    assert _open_object_nodes(schema, name="t") == ["t$.properties.data"]


def _declared_instance_paths(node: object, *, prefix: str) -> set[str]:
    """Every dotted property path a DECLARATION node permits an instance to carry."""
    if not isinstance(node, Mapping):
        return set()
    found: set[str] = set()
    properties = node.get("properties")
    if isinstance(properties, Mapping):
        for name, child in properties.items():
            path = f"{prefix}{name}"
            found.add(path)
            found |= _declared_instance_paths(child, prefix=f"{path}.")
    for keyword in _UNION_KEYWORDS:
        branches = node.get(keyword)
        if isinstance(branches, list):
            for branch in branches:
                found |= _declared_instance_paths(branch, prefix=prefix)
    for keyword in _CHILD_SCHEMA_VALUES:
        child = node.get(keyword)
        if isinstance(child, Mapping):
            found |= _declared_instance_paths(child, prefix=prefix)
    return found


def _constrained_instance_paths(node: object, *, prefix: str, inside: bool) -> set[str]:
    """Every dotted property path an APPLICATOR subschema names."""
    if not isinstance(node, Mapping):
        return set()
    found: set[str] = set()
    properties = node.get("properties")
    if isinstance(properties, Mapping) and inside:
        for name, child in properties.items():
            path = f"{prefix}{name}"
            found.add(path)
            found |= _constrained_instance_paths(child, prefix=f"{path}.", inside=True)
    elif isinstance(properties, Mapping):
        for name, child in properties.items():
            found |= _constrained_instance_paths(child, prefix=f"{prefix}{name}.", inside=False)
    for keyword in (*_UNION_KEYWORDS, *_CHILD_SCHEMA_ARRAYS):
        branches = node.get(keyword)
        if isinstance(branches, list):
            for branch in branches:
                found |= _constrained_instance_paths(branch, prefix=prefix, inside=inside)
    for keyword in _CHILD_SCHEMA_VALUES:
        child = node.get(keyword)
        if isinstance(child, Mapping):
            found |= _constrained_instance_paths(child, prefix=prefix, inside=inside)
    for keyword in _APPLICATOR_VALUES:
        child = node.get(keyword)
        if isinstance(child, Mapping):
            found |= _constrained_instance_paths(child, prefix=prefix, inside=True)
    for keyword in _APPLICATOR_ARRAYS:
        branches = node.get(keyword)
        if isinstance(branches, list):
            for branch in branches:
                found |= _constrained_instance_paths(branch, prefix=prefix, inside=True)
    return found


def test_the_applicator_exemption_rests_on_a_closed_declaration() -> None:
    """Every property an applicator names is one the closed schema also declares.

    This is the argument the exemption stands on, checked rather than asserted:
    an applicator can only ever narrow what a declaration node already permits,
    so a name it mentions that nothing declares is either dead schema or a key
    someone expected to cross through the applicator alone -- which it cannot.
    """
    undeclared: list[str] = []
    constrained_anywhere = 0
    for tool in tool_schemas():
        declared = _declared_instance_paths(tool["outputSchema"], prefix="")
        constrained = _constrained_instance_paths(tool["outputSchema"], prefix="", inside=False)
        constrained_anywhere += len(constrained)
        undeclared.extend(f"{tool['name']}:{path}" for path in sorted(constrained - declared))

    # `ledger.transactions.search`'s flow coupling is the one applicator on the
    # surface; a zero here would make the assertion below vacuous.
    assert constrained_anywhere > 0
    assert not undeclared, undeclared


@pytest.mark.parametrize("tool", tool_schemas(), ids=lambda tool: str(tool["name"]))
def test_an_undeclared_key_is_rejected_on_the_error_branch(tool: dict[str, object]) -> None:
    """Half the behavioural twin: the ERROR branch, which needs no fixture.

    This case cannot speak for the success branch. A tainted error envelope
    fails the success branch on its missing `required` keys -- `coverage`,
    `data`, `instruction_trust` -- long before closedness is consulted, so
    opening the success branch leaves every parametrization green while opening
    this one turns them all red. Both mutations were run. The success branch,
    which carries every byte of real data, is covered by the test below.
    """
    error_envelope = {
        "schema": "ledger.mcp.v1",
        "error": {"code": "internal_error", "retryable": False},
    }

    jsonschema.validate(error_envelope, tool["outputSchema"])
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({**error_envelope, "undeclared": "x"}, tool["outputSchema"])


def test_an_undeclared_key_is_rejected_on_the_success_branch_of_every_tool(
    tmp_path: Path,
) -> None:
    """The other half, on real envelopes the service actually produced.

    A synthesized instance would only prove what this file already asserts
    structurally. These are driven through `call_tool` over the synthetic
    database, so what is validated is what the wire carries -- and the argument
    list is `test_mcp_protocol`'s, which is itself asserted against
    `tool_schemas()` for completeness, so a new tool arrives here covered rather
    than silently skipped.
    """
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    schemas = {str(tool["name"]): tool["outputSchema"] for tool in tool_schemas()}
    service = LedgerMCPService(db_path)
    try:
        calls = _every_tool_call(service)
        covered = set()
        for name, arguments in calls:
            result = call_tool(service, name, arguments)
            envelope = result["structuredContent"]
            assert result.get("isError") is not True, (name, envelope)
            assert "data" in envelope, (name, envelope)

            jsonschema.validate(envelope, schemas[name])
            with pytest.raises(jsonschema.ValidationError):
                jsonschema.validate({**envelope, "undeclared": "x"}, schemas[name])
            covered.add(name)
    finally:
        service.close()

    assert covered == set(schemas), sorted(set(schemas) - covered)
