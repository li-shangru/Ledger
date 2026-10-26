"""Pinned public contract for the local read-only Ledger MCP."""

from __future__ import annotations

import re
from pathlib import Path

from ledger.mcp.contracts import (
    ACCOUNT_REF_SEMANTICS,
    INSIGHT_PERIOD_STATEMENT,
    INSIGHT_UNSERVABLE_REASONS,
    INSIGHT_UNSERVABLE_STATEMENT,
    QUALITY_UNSERVABLE_REASONS,
    QUALITY_UNSERVABLE_STATEMENT,
    SERVER_INSTRUCTIONS,
    SIGNED_MONEY_SCHEMA,
    contract_digest,
    contract_document,
    tool_schemas,
)
from ledger.mcp.investments import EXACT_DECIMAL_PATTERN
from ledger.storage.database import TransactionDB

EXPECTED_TOOLS = [
    "ledger.status",
    "ledger.brief",
    "ledger.explain",
    "ledger.cash_plan",
    "ledger.planning.definitions",
    "ledger.plan_reconciliation",
    "ledger.aggregate",
    "ledger.transactions.search",
    "ledger.transaction.get",
    "ledger.coverage",
    "ledger.recurring",
    "ledger.transfers",
    "ledger.dimensions",
    "ledger.balances",
    "ledger.changes",
    "ledger.projection",
    "ledger.insights",
    "ledger.quality",
    "ledger.budgets",
    "ledger.net_worth",
    "ledger.investments.statements",
    "ledger.investments.statement.get",
    "ledger.investments.positions",
    "ledger.investments.activities",
]
EXPECTED_CONTRACT_DIGEST = "f352df8ba05997d0a8a23576d06aa964d733019b15058dcde9bf230785133d26"


def _property_names(schema: object) -> set[str]:
    if isinstance(schema, dict):
        names = set(schema.get("properties", {}))
        for value in schema.values():
            names.update(_property_names(value))
        return names
    if isinstance(schema, list):
        names: set[str] = set()
        for value in schema:
            names.update(_property_names(value))
        return names
    return set()


# The two reference namespaces, spelled as the schemas spell them. Read off the
# pattern rather than a field name, because a reference reaches the wire under
# three different names already (`account_ref`, an aggregate group `key`) and the
# next surface is free to invent a fourth.
_ACCOUNT_REF_PATTERNS = {"^acct-[0-9a-f]{10}$", "^unresolved-[0-9a-f]{10}$"}


def _emits_account_reference(schema: object) -> bool:
    if isinstance(schema, dict):
        if schema.get("pattern") in _ACCOUNT_REF_PATTERNS:
            return True
        return any(_emits_account_reference(value) for value in schema.values())
    if isinstance(schema, list):
        return any(_emits_account_reference(value) for value in schema)
    return False


def _data_variants(tool: dict[str, object]) -> list[dict[str, object]]:
    """Every shape one tool's `data` block can take, as separately-checkable units."""
    success = next(
        branch
        for branch in tool["outputSchema"]["oneOf"]  # type: ignore[index]
        if "data" in branch.get("properties", {})
    )
    data = success["properties"]["data"]
    return list(data["oneOf"]) if "oneOf" in data else [data]


def test_contract_registers_only_the_approved_read_only_surface() -> None:
    tools = tool_schemas()

    assert [tool["name"] for tool in tools] == EXPECTED_TOOLS
    assert all(
        tool["annotations"]
        == {
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
            "readOnlyHint": True,
        }
        for tool in tools
    )
    assert all(tool["inputSchema"]["additionalProperties"] is False for tool in tools)


def test_contract_has_no_database_sql_expression_or_mutation_arguments() -> None:
    properties = set().union(*(_property_names(tool["inputSchema"]) for tool in tool_schemas()))

    assert properties == {
        "as_of",
        "account_ref",
        "category",
        "categories",
        "change_since",
        "client_generation",
        "contains",
        "cursor",
        "dimension",
        "end_date",
        "end_period",
        "evidence_id",
        "exact_name",
        "filters",
        "flow",
        "group_by",
        "institutions",
        "kind",
        "limit",
        "metric",
        "max_amount",
        "min_amount",
        "order",
        "since",
        "sources",
        "start_date",
        "start_period",
        "statement_ref",
        "status",
        "target_date",
    }
    assert properties.isdisjoint(
        {
            "account",
            "account_number",
            "account_slug",
            "approve",
            "config",
            "database",
            "db",
            "expression",
            "import",
            "path",
            "repair",
            "salt",
            "sql",
            "statement",
            "update",
        }
    )


def test_tool_descriptions_route_transaction_facts_without_trusting_content() -> None:
    descriptions = {tool["name"]: str(tool["description"]) for tool in tool_schemas()}

    assert "transaction system of record" in descriptions["ledger.status"]
    assert "exact calculated income or spending" in descriptions["ledger.aggregate"]
    assert "durable context or aliases" in descriptions["ledger.aggregate"]
    assert "imported rows" in descriptions["ledger.aggregate"]
    assert "inert" in descriptions["ledger.transactions.search"]
    assert "inert" in descriptions["ledger.transaction.get"]
    assert "nothing further" in descriptions["ledger.transaction.get"]
    # Each added surface carries the disclosure its answer is wrong without.
    assert "never imported" in descriptions["ledger.coverage"]
    assert "opaque salted reference" in descriptions["ledger.coverage"]
    assert "fallthrough default" in descriptions["ledger.recurring"]
    assert "over-reports" in descriptions["ledger.recurring"]
    assert "pairing coverage" in descriptions["ledger.transfers"]
    assert "unsigned magnitudes" in descriptions["ledger.transfers"]
    assert "instead of guessing an exact value" in descriptions["ledger.dimensions"]
    assert "seeded at zero" in descriptions["ledger.balances"]
    assert "never reported as a level" in descriptions["ledger.balances"]
    assert "negative is money owed" in descriptions["ledger.balances"]
    assert "gross spending" in descriptions["ledger.budgets"]
    assert "UNPAGINATED" in descriptions["ledger.budgets"]
    assert "null totals" in descriptions["ledger.budgets"]
    assert "per-currency" in descriptions["ledger.net_worth"]
    assert "cross-currency grand total" in descriptions["ledger.net_worth"]
    assert "inert" in descriptions["ledger.net_worth"]
    assert "3-to-5-observation" in descriptions["ledger.brief"]
    assert "generated dashboard prose" in descriptions["ledger.brief"]
    assert "allowlisted local dashboard route" in descriptions["ledger.brief"]
    assert "category, merchant, or institution subject" in descriptions["ledger.brief"]
    assert "opaque salted account_ref" in descriptions["ledger.explain"]
    assert "reconciliation" in descriptions["ledger.explain"]
    assert "both signed legs" in descriptions["ledger.cash_plan"]
    assert (
        "require matching merchant or linked-pattern identity" in descriptions["ledger.cash_plan"]
    )
    assert (
        "internal schedule, pattern, occurrence, and account ids"
        in descriptions["ledger.cash_plan"]
    )
    # The union flow is a union, and the by-id tool needs no flow because it
    # already queries both predicate sets.
    assert "union of the income and spending" in descriptions["ledger.transactions.search"]
    assert "unsigned magnitudes" in descriptions["ledger.transactions.search"]
    assert "takes no flow" in descriptions["ledger.transaction.get"]
    # The account grouping states both the shared namespace and the fail-closed
    # withholding, because a raw slug is the tempting fallback.
    assert "group_by: account_ref" in descriptions["ledger.aggregate"]
    assert "withholds the groups entirely" in descriptions["ledger.aggregate"]
    # `ledger.changes` is wrong without the rebuild disclosure, the loop, and the
    # name of the ONE field that seeds the next boundary.
    assert "naive UTC" in descriptions["ledger.changes"]
    assert "seed it with the `latest_imported_at` field" in descriptions["ledger.changes"]
    assert "both emit and which both mean the same instant" in descriptions["ledger.changes"]
    assert (
        "echo back the `latest_imported_at` this tool returns as the next call's `since`"
        in descriptions["ledger.changes"]
    )
    # The generation is described as what it is: an optional, informational
    # comparison. The description used to instruct a caller to pair it with the
    # boundary, which produced a loop that could never report an import.
    assert "`client_generation` is optional and informational" in descriptions["ledger.changes"]
    assert (
        "never from the snapshot generation, which rotates on every write"
        in descriptions["ledger.changes"]
    )
    assert "never changes a count and never withholds one" in descriptions["ledger.changes"]
    # `ledger.projection` is wrong without the disclosures its arithmetic cannot
    # carry: what is outside the projection, what attribution does and does not
    # follow, that the dates were derived, that the sign comes from the type
    # rather than from the amount, and that `subscription` is a residue bucket --
    # the last of which `ledger.recurring` states twice and this description
    # omitted, although both surfaces emit the identical labels and a routing
    # model reads the description before it sees any response.
    assert "are therefore NOT projected" in descriptions["ledger.projection"]
    assert (
        "Every occurrence a pattern was detected from posted to the ONE account it is "
        "attributed to" in descriptions["ledger.projection"]
    )
    assert "attribution does not follow a merchant that MOVES" in descriptions["ledger.projection"]
    assert (
        "the SIGNED number of periods between that stored date" in descriptions["ledger.projection"]
    )
    assert "never from `missed_count`" in descriptions["ledger.projection"]
    assert "comes from `pattern_type` alone" in descriptions["ledger.projection"]
    assert "refused with a reason" in descriptions["ledger.projection"]
    assert "fallthrough default" in descriptions["ledger.projection"]
    assert "over-report subscriptions" in descriptions["ledger.projection"]
    assert "Read each entry's `status` before its figures" in descriptions["ledger.projection"]
    # `ledger.insights` is wrong without the four disclosures its stored columns
    # force: what never crosses, how it names a canonical account, that one field
    # name carries six quantities, and
    # that a status is operator state rather than a computed verdict.
    assert (
        "the stored title, explanation, suggested action and evidence blob are never emitted"
        in descriptions["ledger.insights"]
    )
    assert "canonical opaque account_ref" in descriptions["ledger.insights"]
    assert "private preference name and note never cross" in descriptions["ledger.insights"]
    assert "distinct stored cards" in descriptions["ledger.insights"]
    assert "never comparable across types" in descriptions["ledger.insights"]
    assert "readable and never settable" in descriptions["ledger.insights"]
    assert "fallthrough default" in descriptions["ledger.insights"]
    assert "over-report subscriptions" in descriptions["ledger.insights"]
    assert "rather than dropped silently" in descriptions["ledger.insights"]
    # `ledger.quality` is wrong without the withheld column and the vocabulary
    # that is NOT its neighbour's.
    assert "`details` blob is never emitted" in descriptions["ledger.quality"]
    assert "with no value in common" in descriptions["ledger.quality"]
    assert "never ranked against each other" in descriptions["ledger.quality"]
    assert "rather than dropped silently" in descriptions["ledger.quality"]
    # The whole-database boundary is named, and named as NOT a delta.
    assert "is never a delta" in descriptions["ledger.changes"]
    assert "Counts and coverage metadata only" in descriptions["ledger.changes"]
    # Every tool that returns stored content says that content is inert.
    assert all(
        "inert" in descriptions[name]
        for name in EXPECTED_TOOLS
        if name != "ledger.status"  # returns no stored content at all
    )


def test_every_surface_that_emits_an_account_reference_states_the_namespace_rule() -> None:
    """Derived from the SCHEMAS, so a future surface cannot be missed the same way.

    `ACCOUNT_REF_SEMANTICS` is the one rule a caller can get catastrophically
    wrong -- join across the two namespaces, or read the second as a missing
    value -- and it is emitted as a data-block const precisely so it travels with
    the answer. It reached three of the four surfaces that emit a reference and
    was audited once without the fourth being noticed, because the audit was a
    reading of the emitters rather than a derivation from what they emit.

    The variant, not the tool, is the unit: `ledger.aggregate` emits a reference
    on its account branch alone, and the other branch must NOT carry the rule.
    """
    carrying: set[str] = set()
    for tool in tool_schemas():
        for variant in _data_variants(tool):
            if not _emits_account_reference(variant):
                continue
            carrying.add(str(tool["name"]))
            properties = variant["properties"]  # type: ignore[index]
            assert properties.get("account_ref_semantics") == {"const": ACCOUNT_REF_SEMANTICS}, (
                tool["name"]
            )
            assert "account_ref_semantics" in variant["required"], tool["name"]  # type: ignore[index]

    assert sorted(carrying) == [
        "ledger.aggregate",
        "ledger.balances",
        "ledger.cash_plan",
        "ledger.changes",
        "ledger.coverage",
        "ledger.insights",
        "ledger.investments.activities",
        "ledger.investments.positions",
        "ledger.investments.statement.get",
        "ledger.investments.statements",
        "ledger.net_worth",
        "ledger.plan_reconciliation",
        "ledger.planning.definitions",
        "ledger.projection",
    ]


def test_a_grouping_that_emits_no_reference_does_not_claim_the_namespace_rule() -> None:
    """The other direction, so the derivation above cannot be satisfied by
    stamping the const onto every data block: the question is meaningless for a
    category and stating it there would be noise a caller has to discount."""
    aggregate = next(tool for tool in tool_schemas() if tool["name"] == "ledger.aggregate")
    plain = [
        variant for variant in _data_variants(aggregate) if not _emits_account_reference(variant)
    ]

    assert len(plain) == 1, plain
    assert "account_ref_semantics" not in plain[0]["properties"]  # type: ignore[index]


def test_contract_document_and_digest_are_deterministic() -> None:
    assert contract_document() == contract_document()
    assert contract_digest() == EXPECTED_CONTRACT_DIGEST


# The signed money schema, found by its PATTERN rather than by the field names it
# happens to sit under. `ledger.projection` alone serves it under four names, and
# the next surface is free to invent a fifth.
_SIGNED_MONEY_PATTERN = SIGNED_MONEY_SCHEMA["pattern"]


def _serves_signed_money(schema: object) -> bool:
    if isinstance(schema, dict):
        if schema.get("pattern") in {_SIGNED_MONEY_PATTERN, EXACT_DECIMAL_PATTERN}:
            return True
        return any(_serves_signed_money(value) for value in schema.values())
    if isinstance(schema, list):
        return any(_serves_signed_money(value) for value in schema)
    return False


def _tools_named_in(sentence: str) -> set[str]:
    return set(re.findall(r"ledger\.[a-z_]+(?:\.[a-z_]+)*", sentence))


def test_the_tools_named_as_serving_signed_amounts_are_the_ones_that_do() -> None:
    """Derived from the SCHEMAS, so a third signed surface cannot land silently.

    `SERVER_INSTRUCTIONS` states, in prose the routing model reads before it ever
    sees a response, that the named tools "are the only tools whose monetary
    fields go negative; ... every other tool reports unsigned magnitudes". That string is
    passed as `instructions=` and contributes NOTHING to `contract_digest()`, so
    a new tool adopting the signed schema rotates the digest for its own reason
    and leaves the sentence false with nothing to catch it. This is the check
    that catches it.

    The unsigned schema is not cosmetic: a negative reaching it fails closed as
    corrupt data, which is what makes a wrong sign a refusal rather than a silent
    flip.
    """
    serving = {
        str(tool["name"]) for tool in tool_schemas() if _serves_signed_money(tool["outputSchema"])
    }
    claim = next(
        sentence
        for sentence in " ".join(SERVER_INSTRUCTIONS.split()).split(". ")
        if "only tools whose monetary fields go negative" in sentence
    )

    assert serving == {
        "ledger.balances",
        "ledger.brief",
        "ledger.budgets",
        "ledger.cash_plan",
        "ledger.explain",
        "ledger.investments.activities",
        "ledger.investments.positions",
        "ledger.investments.statement.get",
        "ledger.investments.statements",
        "ledger.net_worth",
        "ledger.plan_reconciliation",
        "ledger.projection",
    }
    assert _tools_named_in(claim) == serving
    assert "every other tool reports unsigned magnitudes" in claim


def _declares_property(schema: object, name: str) -> bool:
    if isinstance(schema, dict):
        if name in schema.get("properties", {}):
            return True
        return any(_declares_property(value, name) for value in schema.values())
    if isinstance(schema, list):
        return any(_declares_property(value, name) for value in schema)
    return False


def _published_constants(variant: dict[str, object]) -> str:
    """Every const string a caller receives in this data block, folded together."""
    collected: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            value = node.get("const")
            if isinstance(value, str):
                collected.append(value)
            for inner in node.values():
                walk(inner)
        elif isinstance(node, list):
            for inner in node:
                walk(inner)

    walk(variant)
    return " ".join(collected).lower()


def test_every_surface_serving_a_stored_pattern_field_states_what_it_means() -> None:
    """Derived from the SCHEMAS, exactly as the account-ref rule is.

    `next_expected_date` and `missed_count` are stored columns whose obvious
    reading is wrong: the date is one period after the last occurrence the
    DETECTOR ACCEPTED and is routinely in the past, and the count FLOORS, so a
    pattern less than one full period late is byte-identical to an on-time one.
    `ledger.projection` published a statement for each, because its arithmetic is
    wrong without them. `ledger.recurring` served the identical columns raw and
    published neither -- the same three-of-four shape the account-ref rule was
    audited into once without the fourth surface being noticed.
    """
    serving: set[str] = set()
    for tool in tool_schemas():
        for variant in _data_variants(tool):
            if not any(
                _declares_property(variant, field)
                for field in ("next_expected_date", "missed_count")
            ):
                continue
            serving.add(str(tool["name"]))
            published = _published_constants(variant)
            assert "one period after the last occurrence the detector accepted" in published, tool[
                "name"
            ]
            assert "floors" in published, tool["name"]
            assert "int(days_overdue/period_days)" in published, tool["name"]

    assert serving == {"ledger.projection", "ledger.recurring"}


# The four transaction-shaped tools. Everything else is "a surface that answers
# what the transaction tools cannot", which is the phrase SERVER_INSTRUCTIONS
# counts -- and it counts them in a number, in prose, that nothing pins.
_TRANSACTION_TOOLS = {
    "ledger.status",
    "ledger.aggregate",
    "ledger.transactions.search",
    "ledger.transaction.get",
}
_SPELLED_NUMBERS = {
    4: "Four",
    5: "Five",
    6: "Six",
    7: "Seven",
    8: "Eight",
    9: "Nine",
    10: "Ten",
    11: "Eleven",
    12: "Twelve",
    13: "Thirteen",
    14: "Fourteen",
    15: "Fifteen",
    16: "Sixteen",
    20: "Twenty",
}


def test_the_instructions_count_the_surfaces_they_actually_describe() -> None:
    """`instructions=` is digest-neutral, so only a test catches a false claim.

    The count is written out in words, in a sentence a routing model reads before
    it ever sees a response. It has to be edited by hand every time a surface
    lands, which is exactly the shape that goes stale: adding a tool rotates the
    digest for its own reason and leaves this sentence one short with nothing
    else to notice.
    """
    surfaces = [
        str(tool["name"]) for tool in tool_schemas() if tool["name"] not in _TRANSACTION_TOOLS
    ]
    claim = next(
        sentence
        for sentence in " ".join(SERVER_INSTRUCTIONS.split()).split(". ")
        if "surfaces answer what the transaction tools cannot" in sentence
    )

    assert claim.startswith(_SPELLED_NUMBERS[len(surfaces)]), (claim, len(surfaces))
    # And each of them is actually described below that sentence, by name.
    for name in surfaces:
        assert name in SERVER_INSTRUCTIONS, name


def test_the_instructions_withheld_claim_is_true_of_the_published_schema() -> None:
    """The one claim `ledger.insights` is worthless without, checked against the
    schema rather than against the emitter.

    `insight_findings` stores four columns this surface refuses: three
    interpolated strings and a schema-less JSON blob. The instructions say so;
    what makes it true is that no branch of the published entry schema declares
    any of them.
    """
    insights = next(tool for tool in tool_schemas() if tool["name"] == "ledger.insights")
    declared = _property_names(insights["outputSchema"])
    claim = " ".join(SERVER_INSTRUCTIONS.split())

    assert declared.isdisjoint({"title", "explanation", "suggested_action", "evidence"})
    assert declared.isdisjoint({"fingerprint", "pattern_id", "account_type", "txn_id"})
    assert "the stored title, explanation, suggested action and evidence blob never cross" in claim
    assert "Credit-card findings carry the same opaque account_ref" in claim
    assert _emits_account_reference(insights["outputSchema"])
    assert "account_ref" in declared


def test_the_two_triage_vocabularies_are_disjoint_as_the_instructions_claim() -> None:
    """Two tables, two columns named `severity` and two named `status`, and not
    one value in common. A shared wire enum would have been wrong on both, and a
    caller ranking an `error` against a `high` is what the claim prevents."""
    enums: dict[str, dict[str, set[str]]] = {}
    for name in ("ledger.insights", "ledger.quality"):
        tool = next(tool for tool in tool_schemas() if tool["name"] == name)
        enums[name] = {
            field: {value for node in _enum_nodes(tool["outputSchema"], field) for value in node}
            for field in ("severity", "status")
        }
    claim = " ".join(SERVER_INSTRUCTIONS.split())

    for field in ("severity", "status"):
        assert enums["ledger.insights"][field], field
        assert enums["ledger.quality"][field], field
        assert enums["ledger.insights"][field].isdisjoint(enums["ledger.quality"][field]), field
    assert enums["ledger.quality"]["severity"] == {"error", "warning", "info"}
    assert enums["ledger.quality"]["status"] == {"pass", "fail", "warn"}
    assert enums["ledger.insights"]["severity"] == {"high", "medium", "low"}
    assert enums["ledger.insights"]["status"] == {
        "open",
        "watch",
        "ignored",
        "reviewed",
        "resolved",
    }
    assert "share their COLUMN NAMES with ledger.insights and not one value" in claim


def _enum_nodes(schema: object, name: str) -> list[list[str]]:
    """Every `enum` declared for a property of this name, at any depth."""
    found: list[list[str]] = []
    if isinstance(schema, dict):
        declared = schema.get("properties")
        if isinstance(declared, dict) and isinstance(declared.get(name), dict):
            values = declared[name].get("enum")
            if isinstance(values, list):
                found.append([str(value) for value in values])
        for value in schema.values():
            found.extend(_enum_nodes(value, name))
    elif isinstance(schema, list):
        for value in schema:
            found.extend(_enum_nodes(value, name))
    return found


def _error_code_comment(code: str) -> str:
    """The comment block immediately above one code in `_ERROR_CODES`.

    Read off the source because a comment is the only place this particular
    rationale lives, and a comment nothing reads is a published sentence nothing
    keeps true.
    """
    source = (Path(__file__).resolve().parents[1] / "src/ledger/mcp/contracts.py").read_text(
        encoding="utf-8"
    )
    block = source.split("_ERROR_CODES = [", 1)[1].split("]", 1)[0]
    lines: list[str] = []
    for line in block.splitlines():
        stripped = line.strip()
        if stripped == f'"{code}",':
            return " ".join(lines)
        lines = [*lines, stripped.lstrip("# ").strip()] if stripped.startswith("#") else []
    raise AssertionError(f"{code} is not in _ERROR_CODES")


def test_the_filter_too_broad_comment_names_every_filterless_tool_that_can_answer_it() -> None:
    """A non-retryable code naming a filter, reached by a tool that has none.

    The comment is the only place that reconciles those two facts, and P4 added
    two more such tools without touching it -- so it named half the surface it
    describes. Both the tool set and the "can it even answer" test are derived
    from the published schemas: a tool whose only inputs are the pagination pair
    has nothing to narrow, and one whose success branch declares a `page` block
    is one that walks stored rows and therefore has a scan cap to exceed.
    `tests/test_insights_surface.py` and `tests/test_quality_surface.py` prove
    behaviourally that the two new ones really do answer this code.
    """
    comment = _error_code_comment("filter_too_broad")
    filterless = set()
    for tool in tool_schemas():
        if set(tool["inputSchema"].get("properties", {})) - {"cursor", "limit"}:
            continue
        success = next(
            branch
            for branch in tool["outputSchema"]["oneOf"]
            if "data" in branch.get("properties", {})
        )
        if "page" in success["properties"]:
            filterless.add(str(tool["name"]))

    assert filterless, "no tool matched, so the assertion below proves nothing"
    for name in sorted(filterless):
        assert name in comment, (
            f"{name} answers filter_too_broad with no filter, unnamed in: {comment}"
        )


def test_each_unservable_enumeration_is_the_same_sentence_in_the_description() -> None:
    """A routing model reads the description before it ever sees a response.

    `ledger.insights` named three of the four reasons a stored row is counted
    rather than served, omitting the evidence-blob reason -- the one that fires
    on real producer drift, a renamed key on a schema-less JSON column, which
    the producer-key tests exist to guard. `ledger.quality` named two of its
    three, omitting the check-name and issue-count reason. Both now interpolate
    the SAME constant the wire const does, so the two cannot drift apart at all,
    and this test is what keeps the interpolation.
    """
    descriptions = {str(tool["name"]): str(tool["description"]) for tool in tool_schemas()}

    assert INSIGHT_UNSERVABLE_REASONS in INSIGHT_UNSERVABLE_STATEMENT
    assert INSIGHT_UNSERVABLE_REASONS in descriptions["ledger.insights"]
    assert QUALITY_UNSERVABLE_REASONS in QUALITY_UNSERVABLE_STATEMENT
    assert QUALITY_UNSERVABLE_REASONS in descriptions["ledger.quality"]
    # The reasons a mapper can actually take, enumerated rather than summarized:
    # `_insight_finding` has four early returns and `_quality_check` has three.
    assert "evidence blob" in INSIGHT_UNSERVABLE_REASONS
    assert "issue count" in QUALITY_UNSERVABLE_REASONS


_STORED_COLUMN_WIDTHS = {
    "category": TransactionDB.__table__.c.category.type.length,
    "institution": TransactionDB.__table__.c.institution.type.length,
    "merchant": TransactionDB.__table__.c.merchant.type.length,
}


def _declared_string_widths(schema: object, into: dict[str, set[int]]) -> None:
    """Every `maxLength` declared for a string property, by the property's name."""
    if isinstance(schema, dict):
        for name, child in schema.get("properties", {}).items():
            for branch in (child, *(child.get("oneOf", []) if isinstance(child, dict) else ())):
                if isinstance(branch, dict) and isinstance(branch.get("maxLength"), int):
                    into.setdefault(str(name), set()).add(int(branch["maxLength"]))
        for value in schema.values():
            _declared_string_widths(value, into)
    elif isinstance(schema, list):
        for value in schema:
            _declared_string_widths(value, into)


def test_one_stored_column_is_declared_at_one_width_across_every_tool() -> None:
    """Derived across the published schemas, not read off any one emitter.

    `ledger.insights` shipped `category` and `institution` at maxLength 200
    while every other tool reading the same two columns declares 100 and 50 --
    a bound 2x and 4x looser than the storage it reads, so a caller diffing the
    schemas sees one stored column described two ways. Over-declaring is the
    safe direction, which is exactly why nothing would have caught it. The
    widths themselves are taken from the SQLAlchemy columns rather than
    restated, so widening a column and forgetting the wire fails here too.
    """
    widths: dict[str, set[int]] = {}
    for tool in tool_schemas():
        _declared_string_widths(tool["outputSchema"], widths)

    disagreements = {name: sorted(seen) for name, seen in widths.items() if len(seen) > 1}
    assert not disagreements, f"one stored column, two published bounds: {disagreements}"
    for name, stored in _STORED_COLUMN_WIDTHS.items():
        assert widths[name] == {stored}, (name, sorted(widths[name]), stored)


def test_the_server_instructions_carry_the_two_facts_a_router_cannot_infer() -> None:
    """`SERVER_INSTRUCTIONS` is digest-neutral, so only a test keeps it honest.

    Two claims changed under it in P4 and it said neither: which stored period
    `ledger.insights` serves and how old that analysis is, and that
    `ledger.quality`'s unmatched-transfer count is whole-database rather than
    the number a `quality` finding is about. A routing model reads this before
    any response, so a sentence missing here is a sentence the model never sees.
    """
    claim = " ".join(SERVER_INSTRUCTIONS.split())

    assert "computed_at" in claim
    assert "widest" in claim
    assert "whole-database" in claim
    # Not a restatement: the same two facts are on the wire as consts.
    assert "computed_at" in INSIGHT_PERIOD_STATEMENT
    assert "widest" in INSIGHT_PERIOD_STATEMENT


def test_empty_import_boundary_ends_the_current_run_without_polling() -> None:
    """Reading again cannot supply an import boundary that does not yet exist."""
    claim = " ".join(SERVER_INSTRUCTIONS.split())

    assert "end the current run" in claim
    assert "next authorized scheduled run" in claim
    assert "re-read ledger.status until" not in claim
