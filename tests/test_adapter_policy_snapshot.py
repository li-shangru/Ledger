"""Fictional historical policies must not change a query halfway through its facts."""

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ledger.adapters import registry
from ledger.adapters.contracts import (
    API_VERSION,
    AdapterDefinition,
    AdapterError,
    CompatibilityPolicy,
)
from ledger.analysis import transfer_matcher
from ledger.query.agent import AgentQueryError
from ledger.query.service import LedgerQueryService
from ledger.storage.database import TransferDB, init_database, session_scope
from ledger.storage.mappers import transfer_from_db

START = date(2043, 6, 1)
END = date(2043, 6, 30)


def write_policy(path: Path, *, credit=(), tokens=(), reimbursements=()) -> None:
    definition = AdapterDefinition(
        "fictional-history",
        API_VERSION,
        "1",
        policy=CompatibilityPolicy(
            credit_institutions=tuple(credit),
            credit_account_tokens=tuple(tokens),
            reimbursement_merchants=tuple(reimbursements),
        ),
    )
    record = {
        "distribution": "fictional-history",
        "version": "1.0.0",
        "entry_point": "example",
        "sha256": "1" * 64,
        "metadata": registry._definition_metadata(definition),
        "enabled": False,
    }
    path.write_text(json.dumps({"schema_version": 1, "adapters": [record]}))
    path.chmod(0o600)


@pytest.fixture
def policy_path(tmp_path, monkeypatch):
    runtime = tmp_path / "private-policy"
    runtime.mkdir(mode=0o700)
    path = runtime / "adapters.json"
    monkeypatch.setenv("LEDGER_ADAPTER_CONFIG", str(path))
    write_policy(path)
    monkeypatch.setattr(
        registry, "_distribution", lambda *a: pytest.fail("Query inspected a package")
    )
    monkeypatch.setattr(
        registry, "load_adapters", lambda *a: pytest.fail("Query loaded plugin code")
    )
    return path


def row(identifier, institution, amount, *, paired=None, account_type="unclassified"):
    return TransferDB(
        txn_id=identifier,
        institution=institution,
        amount_raw=Decimal(amount),
        account=identifier,
        account_type=account_type,
        posting_date=START,
        flow="transfer",
        currency="CAD",
        description="Fictional transfer",
        source_file="fictional.csv",
        imported_at=datetime(2043, 7, 1),
        paired_transfer_id=paired,
    )


def query_service(tmp_path, *rows):
    db = tmp_path / "synthetic.db"
    init_database(db)
    with session_scope(db) as session:
        session.add_all(rows)
    return LedgerQueryService(db, snapshot_generation="unchanged-synthetic-generation")


def aggregate(service, **kwargs):
    return service.agent.transfer_aggregates(start_date=START, end_date=END, **kwargs)


def test_policy_change_invalidates_transfer_cursor_without_database_change(policy_path, tmp_path):
    write_policy(policy_path, credit=("fictional-a",))
    service = query_service(
        tmp_path,
        row("a", "fictional-a", "-30.00"),
        row("b", "fictional-b", "-20.00"),
        row("c", "fictional-c", "-10.00"),
    )
    first = aggregate(service, limit=1)
    assert first.next_cursor is not None
    assert first.groups[0].direction == "inbound"
    # The database generation and caller's filters are unchanged.
    write_policy(policy_path, credit=("fictional-b",))
    with pytest.raises(AgentQueryError, match=r"^invalid_cursor$"):
        aggregate(service, limit=2, cursor=first.next_cursor)


def test_one_policy_snapshot_covers_all_aggregate_rows(policy_path, tmp_path, monkeypatch):
    write_policy(policy_path, credit=("fictional-a",))
    service = query_service(
        tmp_path, row("a", "fictional-a", "-30.00"), row("b", "fictional-b", "-20.00")
    )
    original = transfer_matcher.is_credit_leg
    calls = 0

    def classify_then_change(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 1:
            write_policy(policy_path, credit=("fictional-b",))
        return result

    monkeypatch.setattr(transfer_matcher, "is_credit_leg", classify_then_change)
    result = aggregate(service)
    assert [(group.institution, group.direction) for group in result.groups] == [
        ("fictional-a", "inbound"),
        ("fictional-b", "outbound"),
    ]
    # A separate whole query sees the new interpretation.
    assert [(group.institution, group.direction) for group in aggregate(service).groups] == [
        ("fictional-a", "outbound"),
        ("fictional-b", "inbound"),
    ]


def test_matched_pair_query_uses_one_snapshot_for_both_legs(policy_path, tmp_path, monkeypatch):
    write_policy(policy_path, credit=("fictional-receiver",))
    service = query_service(
        tmp_path,
        row("a-receiver", "fictional-receiver", "-10.00", paired="b-funding"),
        row("b-funding", "fictional-funding", "-10.00", paired="a-receiver"),
    )
    original = transfer_matcher.is_credit_leg
    calls = 0

    def classify_then_change(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 1:
            write_policy(policy_path, credit=("fictional-funding",))
        return result

    monkeypatch.setattr(transfer_matcher, "is_credit_leg", classify_then_change)
    pairs = service.get_transfer_matches(START, END)
    assert [(out.txn_id, inflow.txn_id) for out, inflow in pairs] == [("b-funding", "a-receiver")]


def test_private_credit_account_token_controls_historical_direction(policy_path):
    txn = transfer_from_db(row("a", "fictional", "-12.00", account_type="account-raven"))
    assert transfer_matcher.is_credit_leg(txn) is False
    write_policy(policy_path, tokens=("-raven",))
    assert transfer_matcher.is_credit_leg(txn) is True


def test_malformed_policy_refuses_query_instead_of_generic_fallback(policy_path, tmp_path):
    service = query_service(tmp_path, row("a", "fictional-a", "-30.00"))
    config = json.loads(policy_path.read_text())
    config["adapters"][0]["metadata"]["policy"].pop("credit_institutions")
    policy_path.write_text(json.dumps(config))
    with pytest.raises(AdapterError, match=r"^adapter_unsafe_configuration$"):
        aggregate(service)


def test_unchanged_policy_cursor_allows_page_size_and_json_format_changes(policy_path, tmp_path):
    write_policy(policy_path, credit=("fictional-a",))
    service = query_service(
        tmp_path,
        row("a", "fictional-a", "-30.00"),
        row("b", "fictional-b", "-20.00"),
        row("c", "fictional-c", "-10.00"),
    )
    first = aggregate(service, limit=1)
    policy_path.write_text(json.dumps(json.loads(policy_path.read_text()), indent=4))
    rest = aggregate(service, limit=2, cursor=first.next_cursor)
    assert [item.institution for item in (*first.groups, *rest.groups)] == [
        "fictional-a",
        "fictional-b",
        "fictional-c",
    ]
    assert rest.has_more is False
    assert rest.next_cursor is None
    assert first.total == rest.total == Decimal("60.00")


def test_removing_previous_policy_invalidates_its_cursor(policy_path, tmp_path):
    write_policy(policy_path, credit=("fictional-a",))
    service = query_service(
        tmp_path, row("a", "fictional-a", "-30.00"), row("b", "fictional-b", "-20.00")
    )
    first = aggregate(service, limit=1)
    policy_path.unlink()
    with pytest.raises(AgentQueryError, match=r"^invalid_cursor$"):
        aggregate(service, cursor=first.next_cursor)


@pytest.mark.parametrize("kind", ["aggregates", "matched_pairs"])
def test_query_reads_historical_policy_once(policy_path, tmp_path, monkeypatch, kind):
    write_policy(policy_path, credit=("fictional-a",))
    service = query_service(
        tmp_path,
        row("a", "fictional-a", "-30.00", paired="b"),
        row("b", "fictional-b", "-30.00", paired="a"),
    )
    original = registry.read_configuration
    reads = 0

    def read_once(*args, **kwargs):
        nonlocal reads
        reads += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(registry, "read_configuration", read_once)
    if kind == "aggregates":
        aggregate(service)
    else:
        service.get_transfer_matches()
    assert reads == 1


def test_policy_snapshot_remains_immutable_after_saved_change(policy_path):
    from dataclasses import FrozenInstanceError

    write_policy(policy_path, credit=("fictional-a",))
    before = registry.compatibility_policy_snapshot()
    write_policy(policy_path, credit=("fictional-b",))
    after = registry.compatibility_policy_snapshot()
    assert before.credit_institutions == frozenset({"fictional-a"})
    assert after.credit_institutions == frozenset({"fictional-b"})
    assert before.digest != after.digest
    assert len(before.digest) == 64
    with pytest.raises(FrozenInstanceError):
        before.digest = after.digest


def test_declared_account_token_preserves_same_sign_matcher_behavior(policy_path):
    write_policy(policy_path, tokens=("-raven",))
    source = transfer_from_db(row("a", "fictional-source", "-12.00", account_type="Chequing"))
    destination = transfer_from_db(
        row("b", "fictional-target", "-12.00", account_type="account-raven")
    )
    matcher = transfer_matcher.TransferMatcher()
    assert len(matcher.match_transfers([source, destination])) == 1
    write_policy(policy_path)
    # A new whole matcher operation sees the now-empty private token policy.
    assert matcher.match_transfers([source, destination]) == []


def test_reimbursement_policy_is_immutable_and_changes_snapshot_digest(policy_path):
    from dataclasses import FrozenInstanceError

    write_policy(policy_path, reimbursements=("INVENTED BENEFITS",))
    before = registry.compatibility_policy_snapshot()
    write_policy(policy_path, reimbursements=("INVENTED CLAIMS",))
    after = registry.compatibility_policy_snapshot()
    assert before.reimbursement_merchants == frozenset({"INVENTED BENEFITS"})
    assert after.reimbursement_merchants == frozenset({"INVENTED CLAIMS"})
    assert before.digest != after.digest
    with pytest.raises(FrozenInstanceError):
        before.reimbursement_merchants = after.reimbursement_merchants
    with pytest.raises(AttributeError):
        before.reimbursement_merchants.add("INVENTED OTHER")


def test_reimbursement_policy_normalization_preserves_semantic_digest(policy_path):
    write_policy(policy_path, reimbursements=("INVENTED BENEFITS", "INVENTED CLAIMS"))
    before = registry.compatibility_policy_snapshot()
    write_policy(
        policy_path,
        reimbursements=(" invented claims ", "invented benefits", "INVENTED BENEFITS"),
    )
    after = registry.compatibility_policy_snapshot()
    assert before == after
