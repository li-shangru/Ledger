"""`group_by: "account_ref"` on ledger.aggregate.

The ref is not a SQL expression -- it is an HMAC minted from the stored `accounts`
record by the ONE resolver in `query.account_refs` -- so the grouping happens in
SQL on `transactions.account` and is translated afterwards. Two consequences are
load-bearing and both are tested here: the group ordering must NOT use the
aggregate's string-length magnitude trick (account totals are not a magnitude
ranking, and that trick inverts on a negative), and an account with no `accounts`
row must get its own opaque sentinel rather than a blank key two different
accounts would share.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from pathlib import Path

import jsonschema
import pytest
from mcp_synthetic import (
    ACCOUNT_REF_SALT,
    BANK_INSTITUTION,
    CARD_INSTITUTION,
    PRIVATE_ACCOUNT_NUMBER,
    PRIVATE_CARD_NUMBER,
    PRIVATE_VALUES,
    build_synthetic_mcp_database,
)

from ledger.mcp.contracts import ACCOUNT_REF_SEMANTICS, tool_schemas
from ledger.mcp.server import call_tool
from ledger.mcp.service import LedgerMCPService
from ledger.query.account_refs import banking_account_ref
from ledger.storage.database import TransactionDB, session_scope

_PERIOD = {"start_date": "2025-01-01", "end_date": "2025-12-31"}
_UNRESOLVED_REF = re.compile(r"unresolved-[0-9a-f]{10}")
UNJOINABLE_ONE = "synthetic-private-number-0000"
UNJOINABLE_TWO = "synthetic-private-number-1111"


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


def _aggregate(service: LedgerMCPService, **overrides: object) -> dict[str, object]:
    arguments: dict[str, object] = {
        **_PERIOD,
        "flow": "spending",
        "group_by": "account_ref",
        "limit": 100,
    }
    arguments.update(overrides)
    return _structured(call_tool(service, "ledger.aggregate", arguments))


def _amounts(data: dict[str, object]) -> dict[str, str]:
    return {str(group["key"]): str(group["amount"]) for group in data["groups"]}  # type: ignore[index]


def _seed_expense(
    db_path: Path,
    *,
    label: str,
    account: str,
    amount: str,
    posted: date = date(2025, 6, 15),
) -> None:
    signed = -abs(float(amount))
    with session_scope(db_path) as session:
        session.add(
            TransactionDB(
                txn_id=label.ljust(40, "0"),
                posting_date=posted,
                transaction_date=posted,
                institution="synthetic-orphan-bank",
                account=account,
                account_type="Synthetic Orphan",
                flow="expense",
                cashflow_type="expense",
                include_in_cashflow=True,
                exclude_from_spending=False,
                currency="CAD",
                amount_raw=abs(signed),
                amount_signed=signed,
                description="SYNTHETIC ORPHAN ROW",
                merchant="SYNTHETIC ORPHAN",
                category="Shopping",
                source_file="synthetic.csv",
                source_path="synthetic/synthetic.csv",
                source_period=posted.strftime("%Y-%m"),
                source_row=1,
                imported_at=datetime(2030, 1, 1, tzinfo=UTC).replace(tzinfo=None),
                parser_version="synthetic",
                txn_fingerprint=label.ljust(32, "0")[:32],
                day_of_week=posted.weekday(),
                day_of_month=posted.day,
                week_of_year=posted.isocalendar()[1],
                is_weekend=posted.weekday() >= 5,
                is_month_start=posted.day <= 3,
                is_month_end=posted.day >= 28,
            )
        )


# --- 1. one group per account, keyed by the ONE shared ref -------------------


def test_grouping_by_account_ref_uses_the_ref_every_other_tool_emits(
    service: LedgerMCPService,
) -> None:
    """The P0 namespace, not a fourth one minted here."""
    balances = _structured(call_tool(service, "ledger.balances", {"as_of": "2025-12-31"}))
    balance_refs = {str(entry["account_ref"]) for entry in balances["data"]["entries"]}  # type: ignore[index]
    keys = set(_amounts(_aggregate(service)["data"]))  # type: ignore[arg-type]

    assert keys, "no account groups returned"
    assert balance_refs, "fixture seeded no balance refs"
    assert keys <= balance_refs, sorted(keys - balance_refs)


def test_each_group_total_equals_that_account_own_spending(
    service: LedgerMCPService,
) -> None:
    """Derived independently from the fixture's rows, not from the tool.

    Card: 10.10 + 20.20 (the 500.00 row is excluded_from_spending).
    Chequing: the 12.34 bookstore row.
    """
    card = banking_account_ref(
        ACCOUNT_REF_SALT, CARD_INSTITUTION, PRIVATE_CARD_NUMBER, "credit_card"
    )
    chequing = banking_account_ref(
        ACCOUNT_REF_SALT, BANK_INSTITUTION, PRIVATE_ACCOUNT_NUMBER, "chequing"
    )
    data = _aggregate(service)["data"]

    assert _amounts(data) == {card: "30.30", chequing: "12.34"}  # type: ignore[arg-type]
    assert data["total"] == "42.64"  # type: ignore[index]
    assert data["group_count"] == 2  # type: ignore[index]


def test_the_groups_reconcile_to_the_reported_total(service: LedgerMCPService) -> None:
    from decimal import Decimal

    data = _aggregate(service)["data"]

    amounts = _amounts(data)  # type: ignore[arg-type]
    assert amounts
    assert sum(Decimal(value) for value in amounts.values()) == Decimal(str(data["total"]))  # type: ignore[index]


def test_account_groups_are_ordered_by_stored_slug_and_not_by_magnitude(
    db_path: Path,
    service: LedgerMCPService,
) -> None:
    """The aggregate's `length(amount) DESC` trick is a magnitude ranking.

    Account totals are not one -- balances already refuses to order by amount for
    the same reason -- so this grouping orders by the STORED ACCOUNT SLUG, which
    is stable whatever the amounts do AND independent of the account-ref salt the
    cursor does not bind.
    """
    # The biggest total sits on the LAST slug, so a magnitude ranking and a slug
    # ranking cannot agree by accident.
    _seed_expense(db_path, label="orphan-small", account=UNJOINABLE_ONE, amount="1.00")
    _seed_expense(db_path, label="orphan-big", account=UNJOINABLE_TWO, amount="900.00")
    slug_of = {ref: slug for slug, ref in _refs_by_slug(db_path, ACCOUNT_REF_SALT).items()}
    amounts = _amounts(_aggregate(service)["data"])  # type: ignore[arg-type]

    keys = list(amounts)
    assert len(keys) == 4, keys
    assert [slug_of[key] for key in keys] == sorted(slug_of.values())
    # The magnitude ranking the other groupings use would put 900.00 first.
    assert next(iter(amounts.values())) != "900.00"


def test_the_published_ordering_wording_is_the_order_actually_served(
    db_path: Path,
    service: LedgerMCPService,
) -> None:
    """The ordering claim travels on the wire, and the wire said the wrong thing.

    Moving the order onto the stored slug is what broke the cursor/salt coupling,
    and both digest-bearing strings kept saying "ordered by reference". The ref is
    an HMAC, so slug order and ref order are unrelated by construction: this can
    never be a harmless approximation. README, the in-code comments and the tests
    all said slug; only the two strings a CLIENT reads were missed, and nothing
    pinned either phrase.
    """
    from ledger.mcp.contracts import SERVER_INSTRUCTIONS

    # An unjoinable slug sorts BEFORE the fixture's own accounts, and every
    # `unresolved-` ref sorts AFTER every `acct-` one, so slug order and ref order
    # are guaranteed to disagree here rather than disagreeing by luck.
    _seed_expense(db_path, label="orphan-one", account=UNJOINABLE_ONE, amount="11.00")
    _seed_expense(db_path, label="orphan-two", account=UNJOINABLE_TWO, amount="22.00")
    slug_of = {ref: slug for slug, ref in _refs_by_slug(db_path, ACCOUNT_REF_SALT).items()}
    description = next(
        str(tool["description"]) for tool in tool_schemas() if tool["name"] == "ledger.aggregate"
    )

    keys = list(_amounts(_aggregate(service)["data"]))  # type: ignore[arg-type]

    assert [slug_of[key] for key in keys] == sorted(slug_of.values())
    assert keys != sorted(keys), "the served order coincided with the ref order"
    for text in (description, SERVER_INSTRUCTIONS):
        assert "ordered by that reference" not in text
        assert "ordered by reference" not in text
        assert "stored account slug" in text


# --- 2. the sentinel: two unjoinable accounts must not merge -----------------


def test_two_accounts_with_no_stored_record_do_not_collapse_into_one_group(
    db_path: Path,
    service: LedgerMCPService,
) -> None:
    """The exact defect P0 removed from coverage and the export, on a new surface.

    A blank or null key for an unjoinable account is byte-identical for two
    DIFFERENT accounts, so a consumer grouping by ref sums two real accounts'
    money into one row.
    """
    _seed_expense(db_path, label="orphan-one", account=UNJOINABLE_ONE, amount="11.00")
    _seed_expense(db_path, label="orphan-two", account=UNJOINABLE_TWO, amount="22.00")
    data = _aggregate(service)["data"]

    amounts = _amounts(data)  # type: ignore[arg-type]
    sentinels = {key: value for key, value in amounts.items() if _UNRESOLVED_REF.fullmatch(key)}
    assert len(sentinels) == 2, amounts
    assert sorted(sentinels.values()) == ["11.00", "22.00"]
    assert "" not in amounts
    for group in data["groups"]:  # type: ignore[index]
        expected = "resolved" if str(group["key"]).startswith("acct-") else "no_account_record"
        assert group["account_ref_status"] == expected, group


def test_an_unjoinable_group_validates_against_the_declared_schema(
    db_path: Path,
    service: LedgerMCPService,
) -> None:
    _seed_expense(db_path, label="orphan-one", account=UNJOINABLE_ONE, amount="11.00")
    schema = next(
        tool["outputSchema"] for tool in tool_schemas() if tool["name"] == "ledger.aggregate"
    )

    envelope = _aggregate(service)

    assert any(_UNRESOLVED_REF.fullmatch(str(key)) for key in _amounts(envelope["data"]))  # type: ignore[arg-type]
    jsonschema.validate(envelope, schema)


def test_the_grouping_carries_the_two_namespace_rule_on_the_wire(
    service: LedgerMCPService,
) -> None:
    data = _aggregate(service)["data"]

    assert data["account_ref_semantics"] == ACCOUNT_REF_SEMANTICS  # type: ignore[index]
    assert data["account_refs"] == "disclosed"  # type: ignore[index]
    assert data["account_refs_withheld_reason"] is None  # type: ignore[index]


def test_the_namespace_rule_is_worded_for_a_surface_whose_ref_lives_under_key(
    service: LedgerMCPService,
) -> None:
    """An aggregate group carries no field called `account_ref` -- the reference is
    the group `key` -- so a statement opening "An account_ref is always present"
    described a field this surface does not have."""
    assert "account_ref is always present" not in ACCOUNT_REF_SEMANTICS
    assert "group key" in ACCOUNT_REF_SEMANTICS
    keys = set(_amounts(_aggregate(service)["data"]))  # type: ignore[arg-type]
    assert keys and all(key.startswith("acct-") for key in keys)


@pytest.mark.parametrize(
    "substitute",
    ["harbor-chequing-9101", "", "acct-", "unresolved-", "acct-NOTHEX1234"],
    ids=["raw-slug", "blank", "bare-prefix", "bare-sentinel", "non-hex"],
)
def test_the_published_schema_refuses_an_account_group_key_outside_the_two_namespaces(
    service: LedgerMCPService,
    substitute: str,
) -> None:
    """`_GROUP_SCHEMA["key"]` is a shared `{maxLength: 200, type: string}` because
    six other groupings key on a stored value. Under `group_by: "account_ref"`
    that admitted the raw account slug and the blank key P0 forbids -- the one
    that makes two different unjoinable accounts byte-identical -- so the rule was
    documented but not machine-checkable."""
    import jsonschema

    schema = next(
        tool["outputSchema"] for tool in tool_schemas() if tool["name"] == "ledger.aggregate"
    )
    envelope = _aggregate(service)
    envelope["data"]["groups"][0]["key"] = substitute  # type: ignore[index]

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(envelope, schema)


def test_a_non_account_grouping_still_validates_with_an_ordinary_string_key(
    service: LedgerMCPService,
) -> None:
    """The narrowing must apply to the account grouping alone: a category key is
    a stored value and is not in either ref namespace."""
    import jsonschema

    schema = next(
        tool["outputSchema"] for tool in tool_schemas() if tool["name"] == "ledger.aggregate"
    )
    envelope = _structured(
        call_tool(
            service,
            "ledger.aggregate",
            {**_PERIOD, "flow": "spending", "group_by": "category", "limit": 100},
        )
    )

    assert envelope["data"]["groups"], "the fixture produced no category groups"  # type: ignore[index]
    jsonschema.validate(envelope, schema)


def test_a_non_account_grouping_carries_no_account_ref_disclosure(
    service: LedgerMCPService,
) -> None:
    """The three keys are meaningless for a category grouping and are absent."""
    data = _aggregate(service, group_by="category")["data"]

    assert "account_refs" not in data
    assert "account_refs_withheld_reason" not in data
    assert "account_ref_semantics" not in data
    for group in data["groups"]:  # type: ignore[index]
        assert "account_ref_status" not in group


# --- 3. no salt: the standard withheld pattern, never raw slugs -------------


def test_without_a_minted_salt_the_groups_are_withheld_and_never_degraded(
    tmp_path: Path,
) -> None:
    """Minting the salt is a WRITE and this server cannot write.

    Coverage and balances both fail closed here; emitting the raw slug as a group
    key would put an account identifier on a surface that has never carried one.
    """
    import json

    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db", account_ref_salt=False)
    service = LedgerMCPService(db_path)
    try:
        envelope = _aggregate(service)
    finally:
        service.close()

    data = envelope["data"]
    assert data["groups"] == []  # type: ignore[index]
    assert data["account_refs"] == "withheld"  # type: ignore[index]
    assert data["account_refs_withheld_reason"] == "account_ref_salt_absent"  # type: ignore[index]
    # The counts survive: a withheld identity is not a withheld total.
    assert data["total"] == "42.64"  # type: ignore[index]
    assert data["transaction_count"] == 3  # type: ignore[index]
    assert envelope["page"]["has_more"] is False  # type: ignore[index]
    assert envelope["page"]["next_cursor"] is None  # type: ignore[index]
    encoded = json.dumps(envelope, ensure_ascii=False)
    for private in PRIVATE_VALUES:
        assert private not in encoded, private


# --- 4. the contract and the cursor -----------------------------------------


def test_only_the_aggregate_advertises_the_account_ref_grouping() -> None:
    schema = next(
        tool["inputSchema"] for tool in tool_schemas() if tool["name"] == "ledger.aggregate"
    )

    assert schema["properties"]["group_by"]["enum"] == [  # type: ignore[index]
        "source",
        "merchant",
        "category",
        "month",
        "day",
        "none",
        "account_ref",
    ]


def test_the_account_grouping_refuses_more_accounts_than_it_can_translate(
    service: LedgerMCPService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cap bounds the ACCOUNT-REF TRANSLATION, not the page.

    The grouped rows themselves page in SQL -- `group_by(...).order_by(...)
    .offset(...).limit(...)` -- so only one page of groups is ever materialized,
    and an earlier docstring here claiming the whole grouped set was materialized
    first was simply false. What is unbounded is the translation: the ref is an
    HMAC, not a SQL expression, so `account_ref_map` loads every account before
    the page is cut. Past the cap it fails closed with a named code rather than
    minting an unbounded set of identities behind one call.
    """
    monkeypatch.setattr("ledger.query.agent._MAX_ACCOUNT_GROUPS", 1)

    result = call_tool(
        service,
        "ledger.aggregate",
        {**_PERIOD, "flow": "spending", "group_by": "account_ref"},
    )

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "filter_too_broad"  # type: ignore[index]


def test_a_cursor_minted_on_the_account_grouping_fails_across_a_changed_group_by(
    db_path: Path,
    service: LedgerMCPService,
) -> None:
    _seed_expense(db_path, label="orphan-one", account=UNJOINABLE_ONE, amount="11.00")
    base: dict[str, object] = {**_PERIOD, "flow": "spending", "group_by": "account_ref", "limit": 1}
    first = call_tool(service, "ledger.aggregate", base)
    cursor = first["structuredContent"]["page"]["next_cursor"]  # type: ignore[index]
    assert cursor, first

    replayed = call_tool(
        service,
        "ledger.aggregate",
        {**base, "group_by": "category", "cursor": cursor},
    )

    assert replayed["isError"] is True
    assert replayed["structuredContent"]["error"]["code"] == "invalid_cursor"  # type: ignore[index]


def test_the_account_grouping_paginates_over_every_account_exactly_once(
    db_path: Path,
    service: LedgerMCPService,
) -> None:
    _seed_expense(db_path, label="orphan-one", account=UNJOINABLE_ONE, amount="11.00")
    base: dict[str, object] = {**_PERIOD, "flow": "spending", "group_by": "account_ref", "limit": 1}
    pages: list[str] = []
    cursor: object = None
    for _ in range(3):
        arguments = base if cursor is None else {**base, "cursor": cursor}
        envelope = _structured(call_tool(service, "ledger.aggregate", arguments))
        pages.extend(_amounts(envelope["data"]))  # type: ignore[arg-type]
        cursor = envelope["page"]["next_cursor"]  # type: ignore[index]

    assert len(pages) == 3
    assert len(set(pages)) == 3
    assert set(pages) == set(_amounts(_aggregate(service)["data"]))  # type: ignore[arg-type]


# --- 5. the cursor law: the sort key must not be salt-derived ----------------

# A second salt whose refs sort the fixture's accounts into a DIFFERENT order
# from the first's. Any salt exercises the replay; this one makes the old
# ref-ordered implementation actually skip an account instead of coincidentally
# addressing the same one.
_OTHER_SALT = "f" * 64


def _refs_by_slug(db_path: Path, salt: str) -> dict[str, str]:
    """Every account slug in the fixture, and the ref it mints under `salt`."""
    from ledger.query.account_refs import account_ref_map, resolve_account_ref

    with session_scope(db_path) as session:
        slugs = sorted({str(row[0]) for row in session.query(TransactionDB.account).distinct()})
        refs = account_ref_map(session, salt)
        return {slug: resolve_account_ref(refs, slug, salt)[0] for slug in slugs}


def _agent(db_path: Path):
    from ledger.query.service import LedgerQueryService
    from ledger.storage.read_only import ReadOnlyDatabase

    database = ReadOnlyDatabase(db_path)
    query = LedgerQueryService(
        db_path,
        session_scope_factory=database.session_scope,
        snapshot_generation=database.preflight().generation,
    )
    return database, query.agent


def _account_page(agent, salt: str, cursor: str | None) -> tuple[list[str], str | None]:
    from ledger.query.agent import AgentFilters

    result = agent.aggregate_transaction_facts(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 12, 31),
        flow="spending",
        group_by="account_ref",
        filters=AgentFilters(),
        limit=1,
        cursor=cursor,
        account_ref_salt=salt,
    )
    return [group.key for group in result.groups], result.next_cursor


def test_a_cursor_minted_under_one_salt_never_skips_an_account_under_another(
    db_path: Path,
) -> None:
    """`account_ref_salt` is NOT bound into the cursor fingerprint.

    While the derived ref was the sort key, a cursor minted under salt A and
    replayed under salt B was ACCEPTED and returned a different account: one
    account was never served at all, behind an ordinary success envelope. The P0
    precedent is the fix -- order by the STORED SLUG, which is what makes
    ledger.coverage and ledger.balances safe -- because it removes the coupling
    instead of binding one more input into the fingerprint.
    """
    _seed_expense(db_path, label="orphan-one", account=UNJOINABLE_ONE, amount="11.00")
    slug_of = {
        salt: {ref: slug for slug, ref in _refs_by_slug(db_path, salt).items()}
        for salt in (ACCOUNT_REF_SALT, _OTHER_SALT)
    }
    database, agent = _agent(db_path)
    try:
        first, cursor = _account_page(agent, ACCOUNT_REF_SALT, None)
        assert cursor, "the fixture must hold more than one account"
        under_a, _ = _account_page(agent, ACCOUNT_REF_SALT, cursor)
        # The salt is not bound, so this replay is accepted. It must therefore
        # address the SAME account, not a different one.
        under_b, _ = _account_page(agent, _OTHER_SALT, cursor)
    finally:
        database.close()

    assert [slug_of[ACCOUNT_REF_SALT][ref] for ref in first] != []
    assert under_a and under_b
    assert [slug_of[ACCOUNT_REF_SALT][ref] for ref in under_a] == [
        slug_of[_OTHER_SALT][ref] for ref in under_b
    ]


def test_the_account_group_order_is_the_slug_order_under_either_salt(
    db_path: Path,
) -> None:
    """The whole page sequence, not just the replayed page: paginating under one
    salt and under another visits the same accounts in the same order."""
    _seed_expense(db_path, label="orphan-one", account=UNJOINABLE_ONE, amount="11.00")
    _seed_expense(db_path, label="orphan-two", account=UNJOINABLE_TWO, amount="22.00")
    slugs = _refs_by_slug(db_path, ACCOUNT_REF_SALT)
    slug_of = {
        salt: {ref: slug for slug, ref in _refs_by_slug(db_path, salt).items()}
        for salt in (ACCOUNT_REF_SALT, _OTHER_SALT)
    }
    database, agent = _agent(db_path)
    try:
        walked: dict[str, list[str]] = {}
        for salt in (ACCOUNT_REF_SALT, _OTHER_SALT):
            seen: list[str] = []
            cursor: str | None = None
            for _ in range(len(slugs)):
                keys, cursor = _account_page(agent, salt, cursor)
                seen.extend(slug_of[salt][key] for key in keys)
            walked[salt] = seen
    finally:
        database.close()

    assert walked[ACCOUNT_REF_SALT] == sorted(slugs)
    assert walked[_OTHER_SALT] == walked[ACCOUNT_REF_SALT]
