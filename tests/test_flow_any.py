"""`flow: "any"` on ledger.transactions.search: a union, never a relaxation.

The two flows select DIFFERENT SQL predicate sets and report DIFFERENT amount
bases (`query/agent.py`: income is the signed positive value under the
income-treatment filter, spending is the absolute value of an expense row that is
not excluded from spending). So `any` cannot be implemented by loosening one of
them, and it cannot report one flow's policy as if it covered both.

It is deliberately NOT accepted by `ledger.aggregate` or `ledger.dimensions`: a
single total over a union of income and spending is a meaningless number, and a
dimension list counted over both would report values no single-flow aggregate can
reproduce.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp_synthetic import build_synthetic_mcp_database

from ledger.mcp.contracts import tool_schemas
from ledger.mcp.server import call_tool
from ledger.mcp.service import LedgerMCPService

_PERIOD = {"start_date": "2025-01-01", "end_date": "2025-12-31"}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return build_synthetic_mcp_database(tmp_path / "ledger.db")


@pytest.fixture
def service(db_path: Path) -> LedgerMCPService:
    built = LedgerMCPService(db_path)
    yield built
    built.close()


def _structured(result: dict[str, object]) -> dict[str, object]:
    assert result.get("isError") is not True, result
    return result["structuredContent"]  # type: ignore[return-value]


def _search(service: LedgerMCPService, flow: str) -> dict[str, object]:
    return _structured(
        call_tool(
            service,
            "ledger.transactions.search",
            {**_PERIOD, "flow": flow, "limit": 100},
        )
    )


def _ids(envelope: dict[str, object]) -> set[str]:
    items = envelope["data"]["items"]  # type: ignore[index]
    return {str(item["evidence_id"]) for item in items}


def _seed_excluded_income(db_path: Path) -> str:
    """A fictional income row carries exclude_from_spending, which does not exclude income."""
    from datetime import date, datetime

    from ledger.storage.database import TransactionDB, session_scope

    # Hex, because an evidence id is published as one and the envelope is
    # validated against that pattern before it leaves the kernel.
    txn_id = "ec1de".ljust(40, "d")
    posted = date(2025, 6, 15)
    with session_scope(db_path) as session:
        session.add(
            TransactionDB(
                txn_id=txn_id,
                posting_date=posted,
                transaction_date=posted,
                institution="synthetic-bank",
                account="synthetic-private-number-4321",
                account_type="chequing",
                flow="income",
                cashflow_type="income",
                include_in_cashflow=True,
                exclude_from_spending=True,
                currency="CAD",
                amount_raw=99.00,
                amount_signed=99.00,
                description="SYNTHETIC REBATE",
                merchant="SYNTHETIC REBATE",
                category="Income",
                source_file="synthetic.csv",
                source_path="synthetic/synthetic.csv",
                source_period="2025-06",
                source_row=1,
                imported_at=datetime(2030, 1, 1),
                parser_version="synthetic",
                txn_fingerprint=txn_id[:32],
                day_of_week=posted.weekday(),
                day_of_month=posted.day,
                week_of_year=posted.isocalendar()[1],
                is_weekend=posted.weekday() >= 5,
                is_month_start=False,
                is_month_end=False,
            )
        )
    return txn_id


def _input_schema(name: str) -> dict[str, object]:
    return next(  # type: ignore[return-value]
        tool["inputSchema"] for tool in tool_schemas() if tool["name"] == name
    )


# --- 1. the union itself ----------------------------------------------------


def test_any_returns_exactly_the_union_of_the_two_flow_result_sets(
    service: LedgerMCPService,
) -> None:
    income = _ids(_search(service, "income"))
    spending = _ids(_search(service, "spending"))
    combined = _ids(_search(service, "any"))

    # Without these, an implementation returning nothing at all satisfies every
    # set relation below.
    assert income, "fixture seeded no reportable income"
    assert spending, "fixture seeded no reportable spending"
    assert not income & spending, "the two predicate sets must not overlap"
    assert combined == income | spending


def test_any_counts_the_union_and_not_one_side(service: LedgerMCPService) -> None:
    income = _search(service, "income")["data"]["transaction_count"]  # type: ignore[index]
    spending = _search(service, "spending")["data"]["transaction_count"]  # type: ignore[index]
    combined = _search(service, "any")["data"]["transaction_count"]  # type: ignore[index]

    assert income and spending
    assert combined == income + spending


def test_amounts_stay_unsigned_and_direction_is_carried_by_cashflow_type(
    service: LedgerMCPService,
) -> None:
    """The whole reason a union is safe: nothing about the amount changes sign."""
    items = _search(service, "any")["data"]["items"]  # type: ignore[index]

    directions = {str(item["cashflow_type"]) for item in items}
    assert {"income", "expense"} <= directions, directions
    for item in items:
        assert not str(item["amount"]).startswith("-"), item


def test_an_item_amount_matches_the_single_flow_answer_for_the_same_row(
    service: LedgerMCPService,
) -> None:
    """`any` must not re-base an amount, only widen which rows are selected."""
    single = {
        str(item["evidence_id"]): str(item["amount"])
        for flow in ("income", "spending")
        for item in _search(service, flow)["data"]["items"]  # type: ignore[index]
    }
    combined = {
        str(item["evidence_id"]): str(item["amount"])
        for item in _search(service, "any")["data"]["items"]  # type: ignore[index]
    }

    assert single
    assert combined == single


def test_the_item_flow_field_is_the_parser_label_and_never_the_input_enum(
    service: LedgerMCPService,
) -> None:
    """Two different vocabularies share one word.

    The INPUT `flow` is income/spending/any. The `flow` FIELD on an evidence item
    is the raw parser label (expense, purchase, income, interest, fee, reward,
    refund). A caller that reads the item field as the input enum mis-buckets
    every row, so `any` must never write itself onto an item.
    """
    envelope = _search(service, "any")

    assert envelope["data"]["flow"] == "any"  # type: ignore[index]
    labels = {str(item["flow"]) for item in envelope["data"]["items"]}  # type: ignore[index]
    assert labels, "no items to inspect"
    assert "any" not in labels
    assert labels <= {"expense", "purchase", "income", "interest", "fee", "reward", "refund"}


# --- 2. the policy block, which must not claim one flow covers both ----------


def test_the_policy_block_reports_both_flow_policies_and_neither_alone(
    service: LedgerMCPService,
) -> None:
    policy = _search(service, "any")["policy"]  # type: ignore[index]

    assert policy["flow"] == "any"
    # NOT positive_income and NOT absolute_spending: either one would describe a
    # single branch while the result set holds both.
    assert policy["amount_basis"] == "absolute_magnitude"
    assert [branch["flow"] for branch in policy["flows"]] == ["income", "spending"]
    assert [branch["amount_basis"] for branch in policy["flows"]] == [
        "positive_income",
        "absolute_spending",
    ]


def test_the_policy_block_states_the_union_rule_on_the_wire(
    service: LedgerMCPService,
) -> None:
    from ledger.mcp.contracts import FLOW_ANY_UNION_STATEMENT

    policy = _search(service, "any")["policy"]  # type: ignore[index]

    assert policy["union_semantics"] == FLOW_ANY_UNION_STATEMENT
    assert "cashflow_type" in FLOW_ANY_UNION_STATEMENT
    assert "union" in FLOW_ANY_UNION_STATEMENT.casefold()


def test_a_single_flow_search_keeps_its_own_policy_block_unchanged(
    service: LedgerMCPService,
) -> None:
    """The new keys are for the union case only; nothing else moves."""
    policy = _search(service, "spending")["policy"]  # type: ignore[index]

    assert policy["flow"] == "spending"
    assert policy["amount_basis"] == "absolute_spending"
    assert "flows" not in policy
    assert "union_semantics" not in policy


def test_the_union_block_asserts_no_per_flow_rule_at_the_top_level(
    service: LedgerMCPService,
) -> None:
    """The union block asserts no per flow rule at the top level."""
    policy = _search(service, "any")["policy"]  # type: ignore[index]

    assert "exclude_from_spending" not in policy, policy
    assert "income_treatment_exclusions" not in policy, policy
    # The rules that DO cover both branches stay, because both predicate sets
    # apply them: `include_in_cashflow`, CAD-only, and inclusive date bounds.
    assert policy["requires_include_in_cashflow"] is True
    assert policy["currency"] == "CAD_native_only"
    assert policy["date_bounds"] == "inclusive"
    branches = {branch["flow"]: branch for branch in policy["flows"]}
    assert branches["spending"]["exclude_from_spending"] is True
    assert branches["income"]["income_treatment_exclusions"] == [
        "exclude_from_income",
        "reduce_category_cost",
    ]


def test_the_income_half_of_a_union_really_does_carry_excluded_from_spending_rows(
    db_path: Path,
    service: LedgerMCPService,
) -> None:
    """The income half of a union really does carry excluded from spending rows."""
    from ledger.storage.database import TransactionDB, session_scope

    seeded = _seed_excluded_income(db_path)
    ids = {
        str(item["evidence_id"])
        for item in _search(service, "any")["data"]["items"]  # type: ignore[index]
        if str(item["cashflow_type"]) == "income"
    }
    assert seeded in ids, "the seeded income row was not returned by the union"
    assert ids, "fixture seeded no reportable income"
    with session_scope(db_path) as session:
        flagged = (
            session.query(TransactionDB.txn_id)
            .filter(
                TransactionDB.txn_id.in_(ids),
                TransactionDB.exclude_from_spending == True,  # noqa: E712
            )
            .count()
        )

    assert flagged > 0, "the fixture no longer exercises the case that was misreported"


# --- 2b. the SCHEMA must require what makes the union answer honest ----------


def _output_schema(name: str) -> dict[str, object]:
    return next(  # type: ignore[return-value]
        tool["outputSchema"] for tool in tool_schemas() if tool["name"] == name
    )


def test_a_conforming_union_answer_validates_against_the_published_schema(
    service: LedgerMCPService,
) -> None:
    import jsonschema

    jsonschema.validate(_search(service, "any"), _output_schema("ledger.transactions.search"))
    jsonschema.validate(_search(service, "spending"), _output_schema("ledger.transactions.search"))


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda policy: {
                key: value
                for key, value in policy.items()
                if key not in {"flows", "union_semantics"}
            },
            id="union-without-flolumen-or-union-semantics",
        ),
        pytest.param(
            lambda policy: {**policy, "flows": [policy["flows"][1], policy["flows"][1]]},
            id="union-listing-one-branch-twice",
        ),
        pytest.param(
            lambda policy: {**policy, "exclude_from_spending": True},
            id="union-reasserting-a-single-flow-rule",
        ),
    ],
)
def test_a_union_answer_missing_what_makes_it_honest_is_refused_by_the_schema(
    service: LedgerMCPService,
    mutate,
) -> None:
    """The 1.3.0 cursor-fingerprint shape again: a documented capability the schema
    did not require. `_ANY_POLICY_SCHEMA` reused the single-flow required tuple,
    so `flows` and `union_semantics` were OPTIONAL -- a conforming server could
    return a union result presenting one flow's policy as covering both.
    """
    import jsonschema

    envelope = _search(service, "any")
    envelope["policy"] = mutate(envelope["policy"])  # type: ignore[index]

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(envelope, _output_schema("ledger.transactions.search"))


def test_a_single_flow_answer_carrying_a_union_block_is_refused_by_the_schema(
    service: LedgerMCPService,
) -> None:
    """The other direction: `flows` and `union_semantics` must be ABSENT when the
    flow is not `any`, or a single-flow answer can claim to have covered both."""
    import jsonschema

    union = _search(service, "any")["policy"]  # type: ignore[index]
    envelope = _search(service, "spending")
    envelope["policy"] = {  # type: ignore[index]
        **envelope["policy"],  # type: ignore[index]
        "flows": union["flows"],
        "union_semantics": union["union_semantics"],
    }

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(envelope, _output_schema("ledger.transactions.search"))


def test_a_union_result_set_carrying_a_single_flow_policy_is_refused_by_the_schema(
    service: LedgerMCPService,
) -> None:
    """The policy `oneOf` discriminated on the WRONG field.

    `data.flow` (the echoed input) and `policy` are independent envelope siblings,
    and the branches keyed on `policy.flow` alone with nothing coupling them. So a
    union DATA block -- both cashflow_type values present, both predicate sets
    selected -- carrying the spending policy block verbatim VALIDATED. That is
    exactly the substitution the union block exists to refuse, and the schema
    refused it only when the answer already agreed to describe itself as a union.
    """
    import jsonschema

    envelope = _search(service, "any")
    assert {str(row["cashflow_type"]) for row in envelope["data"]["items"]} == {  # type: ignore[index]
        "expense",
        "income",
    }
    envelope["policy"] = _search(service, "spending")["policy"]

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(envelope, _output_schema("ledger.transactions.search"))


def test_a_single_flow_result_set_carrying_the_other_flow_policy_is_refused(
    service: LedgerMCPService,
) -> None:
    """The same coupling in the other direction: an income answer described by the
    spending policy is a well-formed single-flow envelope and a false one."""
    import jsonschema

    envelope = _search(service, "income")
    envelope["policy"] = _search(service, "spending")["policy"]

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(envelope, _output_schema("ledger.transactions.search"))


def test_the_row_direction_signal_is_a_closed_enum_the_wire_text_names(
    service: LedgerMCPService,
) -> None:
    """Under `any`, cashflow_type is the SOLE direction signal on a row.

    It was declared as an open `{maxLength: 20, type: string}` and no wire text
    named its values, while the set is closed by construction: the income branch
    requires `income` and the spending branch requires `expense`.
    """
    from ledger.mcp.contracts import FLOW_ANY_UNION_STATEMENT

    schema = _output_schema("ledger.transactions.search")
    success = next(
        branch
        for branch in schema["oneOf"]  # type: ignore[index]
        if "data" in branch.get("properties", {})
    )
    item = success["properties"]["data"]["properties"]["items"]["items"]

    assert item["properties"]["cashflow_type"] == {"enum": ["expense", "income"], "type": "string"}
    assert "`income`" in FLOW_ANY_UNION_STATEMENT
    assert "`expense`" in FLOW_ANY_UNION_STATEMENT
    observed = {
        str(row["cashflow_type"])
        for row in _search(service, "any")["data"]["items"]  # type: ignore[index]
    }
    assert observed == {"expense", "income"}


# --- 3. only search accepts it ----------------------------------------------


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("ledger.aggregate", {**_PERIOD, "flow": "any", "group_by": "none"}),
        ("ledger.dimensions", {**_PERIOD, "flow": "any", "dimension": "sources"}),
    ],
)
def test_a_union_flow_is_refused_where_a_union_total_would_be_meaningless(
    service: LedgerMCPService,
    tool: str,
    arguments: dict[str, object],
) -> None:
    result = call_tool(service, tool, arguments)

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "invalid_argument"  # type: ignore[index]


def test_only_the_search_input_schema_advertises_the_third_flow_value() -> None:
    def flow_enum(name: str) -> list[str]:
        return _input_schema(name)["properties"]["flow"]["enum"]  # type: ignore[index]

    assert flow_enum("ledger.transactions.search") == ["income", "spending", "any"]
    assert flow_enum("ledger.aggregate") == ["income", "spending"]
    assert flow_enum("ledger.dimensions") == ["income", "spending"]


def test_the_agent_layer_refuses_a_union_aggregate_directly(db_path: Path) -> None:
    """The kernel argument check is one gate; the query layer is the other."""
    from datetime import date

    from ledger.query.agent import AgentFilters, AgentQueryError
    from ledger.query.service import LedgerQueryService
    from ledger.storage.read_only import ReadOnlyDatabase

    database = ReadOnlyDatabase(db_path)
    query = LedgerQueryService(
        db_path,
        session_scope_factory=database.session_scope,
        snapshot_generation=database.preflight().generation,
    )
    try:
        with pytest.raises(AgentQueryError, match="invalid_argument"):
            query.agent.aggregate_transaction_facts(
                start_date=date(2025, 1, 1),
                end_date=date(2025, 12, 31),
                flow="any",
                group_by="none",
                filters=AgentFilters(),
            )
    finally:
        database.close()


# --- 4. the cursor is bound to the third value ------------------------------


def test_a_cursor_minted_with_a_union_flow_is_refused_when_replayed_as_one_flow(
    service: LedgerMCPService,
) -> None:
    """`any` selects a different row set, so an offset addresses a different row."""
    base: dict[str, object] = {**_PERIOD, "flow": "any", "limit": 1}
    first = call_tool(service, "ledger.transactions.search", base)
    cursor = first["structuredContent"]["page"]["next_cursor"]  # type: ignore[index]
    assert cursor, first

    replayed = call_tool(
        service,
        "ledger.transactions.search",
        {**base, "flow": "spending", "cursor": cursor},
    )

    assert replayed["isError"] is True
    assert replayed["structuredContent"]["error"]["code"] == "invalid_cursor"  # type: ignore[index]


def test_a_union_cursor_still_paginates_the_union(service: LedgerMCPService) -> None:
    base: dict[str, object] = {**_PERIOD, "flow": "any", "limit": 1}
    first = call_tool(service, "ledger.transactions.search", base)
    cursor = str(first["structuredContent"]["page"]["next_cursor"])  # type: ignore[index]

    second = call_tool(service, "ledger.transactions.search", {**base, "cursor": cursor})

    assert second.get("isError") is not True, second
    assert _ids(_structured(second)) and _ids(_structured(second)) != _ids(_structured(first))


# --- 5. the by-id tool was already flow-agnostic; it must say so -------------


def test_the_by_id_tool_documents_that_it_takes_no_flow(
    service: LedgerMCPService,
) -> None:
    """Its input schema is `{evidence_id}` and it queries the union of both
    predicate sets, so an id from either flow resolves. That was true before this
    change and undocumented, which is indistinguishable from untrue."""
    description = next(
        str(tool["description"])
        for tool in tool_schemas()
        if tool["name"] == "ledger.transaction.get"
    )

    assert "flow" in description
    assert "union" in description.casefold()
    assert set(_input_schema("ledger.transaction.get")["properties"]) == {"evidence_id"}  # type: ignore[index]

    for evidence_id in sorted(_ids(_search(service, "any"))):
        result = call_tool(service, "ledger.transaction.get", {"evidence_id": evidence_id})
        assert result.get("isError") is not True, result
