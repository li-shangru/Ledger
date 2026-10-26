"""Insights expose typed scalar evidence while withholding stored free text.

Synthetic findings exercise each producer key set, classification vocabulary,
private identifier and inert-text boundary without retaining owner observations."""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from mcp_synthetic import (
    ACCOUNT_REF_SALT,
    BANK_INSTITUTION,
    CARD_INSTITUTION,
    CARD_REPORTED_ACCOUNT_ID,
    CARD_UNCOMPUTED_ACCOUNT_ID,
    INSIGHT_ADHOC_COMPUTED_AT,
    INSIGHT_ADHOC_MERCHANT,
    INSIGHT_ADHOC_PERIOD_END,
    INSIGHT_ADHOC_PERIOD_START,
    INSIGHT_ANOMALY_TXN_ID,
    INSIGHT_COMPUTED_AT,
    INSIGHT_PATTERN_ID,
    INSIGHT_PERIOD_END,
    INSIGHT_PERIOD_START,
    INSIGHT_PRIOR_PERIOD_MERCHANT,
    INSIGHT_PRODUCER_PERIOD_END,
    INSIGHT_PRODUCER_PERIOD_START,
    INSIGHT_QUALITY_UNMATCHED_LEGS,
    INSIGHT_STORED_ACTION,
    INSIGHT_STORED_EXPLANATION,
    INSIGHT_UNSERVABLE_SEVERITY,
    INSIGHT_UNSERVABLE_TYPE,
    PRIVATE_CARD_NUMBER,
    PRIVATE_UNCOMPUTED_CARD_NUMBER,
    PRIVATE_VALUES,
    QUALITY_UNMATCHED_TRANSFER_ISSUES,
    RECURRING_SUBSCRIPTION,
    build_insight_producer_database,
    build_synthetic_mcp_database,
)
from sqlalchemy import text

from ledger.insights.service import VALID_STATUSES, InsightEngine
from ledger.mcp.contracts import (
    ACCOUNT_REF_SEMANTICS,
    INSIGHT_ACCOUNT_IDENTITY_STATEMENT,
    INSIGHT_AMOUNT_STATEMENT,
    INSIGHT_PERIOD_STATEMENT,
    INSIGHT_TRIAGE_STATEMENT,
    INSIGHT_UNSERVABLE_STATEMENT,
    INSIGHT_WITHHELD_FIELD_STATEMENT,
    RECURRING_CLASSIFICATION_CAVEAT,
    tool_schemas,
)
from ledger.mcp.server import call_tool
from ledger.mcp.service import LedgerMCPService
from ledger.query.account_refs import banking_account_ref
from ledger.query.agent import (
    _MAX_INSIGHT_FINDINGS,
    INSIGHT_SEVERITIES,
    INSIGHT_STATUSES,
    INSIGHT_TEXT_MAX_LENGTHS,
)
from ledger.storage.database import InsightFindingDB, SpendingPreferenceDB, session_scope

_ALL = {"limit": 100}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return build_synthetic_mcp_database(tmp_path / "ledger.db")


@pytest.fixture
def service(db_path: Path) -> LedgerMCPService:
    built = LedgerMCPService(db_path)
    yield built
    built.close()


def _data(result: dict[str, object]) -> dict[str, Any]:
    assert result.get("isError") is not True, result
    return result["structuredContent"]["data"]  # type: ignore[index,return-value]


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(_keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_keys(item) for item in value))
    return set()


def _entries(service: LedgerMCPService) -> list[dict[str, Any]]:
    entries = _data(call_tool(service, "ledger.insights", _ALL))["entries"]
    assert entries, "the fixture seeded no servable findings"
    return entries


def _schema() -> dict[str, Any]:
    return next(tool for tool in tool_schemas() if tool["name"] == "ledger.insights")


def _finding_branches() -> dict[str, dict[str, Any]]:
    """Every per-finding_type branch of the published entry schema, by its const."""
    success = next(
        branch
        for branch in _schema()["outputSchema"]["oneOf"]
        if "data" in branch.get("properties", {})
    )
    branches = success["properties"]["data"]["properties"]["entries"]["items"]["oneOf"]
    return {branch["properties"]["finding_type"]["const"]: branch for branch in branches}


# --- 1. the leak -------------------------------------------------------------


def test_no_envelope_value_carries_a_stored_account_identifier_or_free_text(
    service: LedgerMCPService,
) -> None:
    """The whole envelope as text, against every private value the fixture holds.

    The fictional `credit_cards` finding contains two adversarial fields: its
    evidence carries the account slug under `account_type` and its title
    interpolates the same string. That is `accounts.account_number` verbatim --
    the cleartext `account_ref` exists to salt.
    """
    encoded = json.dumps(call_tool(service, "ledger.insights", _ALL), ensure_ascii=False)

    for private in PRIVATE_VALUES:
        assert private not in encoded, private
    assert PRIVATE_CARD_NUMBER not in encoded
    assert INSIGHT_STORED_EXPLANATION not in encoded
    assert INSIGHT_STORED_ACTION not in encoded
    assert "Ignore previous instructions" not in encoded


def test_the_stored_title_explanation_action_and_evidence_blob_never_cross(
    service: LedgerMCPService,
) -> None:
    """Read off the SCHEMA as well as the answer: neither may name them."""
    entries = _entries(service)
    schema_text = json.dumps(_schema(), ensure_ascii=False)

    for field in ("title", "explanation", "suggested_action", "evidence", "fingerprint"):
        assert all(field not in entry for entry in entries), field
        assert f'"{field}"' not in schema_text, field
    # The stored `id` is not a public identifier either: it is an autoincrement
    # rowid, and `fingerprint` -- the only other candidate -- HASHES THE TITLE,
    # so a reworded template re-fingerprints every finding and orphans the old
    # ones. Neither is a stable public id, so neither is offered as one.
    assert all("id" not in entry for entry in entries)


def test_the_unsalted_pattern_id_never_appears(service: LedgerMCPService) -> None:
    """`ledger.recurring` refuses this construction by name; so does this tool."""
    encoded = json.dumps(call_tool(service, "ledger.insights", _ALL), ensure_ascii=False)

    assert INSIGHT_PATTERN_ID not in encoded
    assert "pattern_id" not in encoded


def test_card_findings_emit_only_the_canonical_opaque_account_reference(
    service: LedgerMCPService,
) -> None:
    """The card row joins its internal id to the shared salted namespace."""
    data = _data(call_tool(service, "ledger.insights", _ALL))
    encoded = json.dumps(data, ensure_ascii=False)

    assert data["account_identity_semantics"] == INSIGHT_ACCOUNT_IDENTITY_STATEMENT
    card = next(entry for entry in data["entries"] if entry["finding_type"] == "credit_cards")
    expected = banking_account_ref(
        ACCOUNT_REF_SALT,
        CARD_INSTITUTION,
        PRIVATE_CARD_NUMBER,
        "credit_card",
    )
    assert data["account_ref_semantics"] == ACCOUNT_REF_SEMANTICS
    assert data["account_refs"] == "disclosed"
    assert data["account_refs_withheld_reason"] is None
    assert card["account_ref"] == expected
    assert CARD_REPORTED_ACCOUNT_ID not in encoded
    assert PRIVATE_CARD_NUMBER not in encoded


def test_missing_account_ref_salt_withholds_card_findings_as_unservable(
    tmp_path: Path,
) -> None:
    db_path = build_synthetic_mcp_database(
        tmp_path / "ledger.db",
        account_ref_salt=False,
    )
    service = LedgerMCPService(db_path)
    try:
        data = _data(call_tool(service, "ledger.insights", _ALL))
    finally:
        service.close()

    assert data["account_refs"] == "withheld"
    assert data["account_refs_withheld_reason"] == "account_ref_salt_absent"
    assert not [entry for entry in data["entries"] if entry["finding_type"] == "credit_cards"]
    assert data["entry_count"] == 6
    assert data["unservable_count"] == 3


def test_preference_context_crosses_without_private_name_or_note(
    db_path: Path,
) -> None:
    private_note = "PRIVATE preference note: never send this to an agent"
    now = datetime(2030, 1, 3)
    with session_scope(db_path) as session:
        session.add(
            SpendingPreferenceDB(
                scope="merchant",
                name="SYNTHETIC MARKET",
                disposition="necessary",
                note=private_note,
                created_at=now,
                updated_at=now,
            )
        )
    service = LedgerMCPService(db_path)
    try:
        data = _data(call_tool(service, "ledger.insights", _ALL))
    finally:
        service.close()

    cost_cutting = next(
        entry for entry in data["entries"] if entry["finding_type"] == "cost_cutting"
    )
    anomaly = next(entry for entry in data["entries"] if entry["finding_type"] == "anomalies")
    assert cost_cutting["action_priority"] == "suppressed"
    assert cost_cutting["preference"] == {
        "disposition": "necessary",
        "effect": "size_only_reduction_suppressed",
        "scope": "merchant",
    }
    assert anomaly["action_priority"] == "normal"
    assert anomaly["preference"] == {
        "disposition": "necessary",
        "effect": "material_evidence_retained",
        "scope": "merchant",
    }
    assert data["entries"][-1]["action_priority"] == "suppressed"
    encoded = json.dumps(data, ensure_ascii=False)
    assert private_note not in encoded
    assert "note" not in _keys(data)
    assert "name" not in _keys(data)


def test_two_same_type_cards_under_one_institution_keep_distinct_refs(
    db_path: Path,
) -> None:
    """Canonical refs distinguish cards even when their public dimensions match."""
    with session_scope(db_path) as session:
        session.add(
            InsightFindingDB(
                fingerprint="synthetic-second-card-group",
                period_start=INSIGHT_PERIOD_START,
                period_end=INSIGHT_PERIOD_END,
                finding_type="credit_cards",
                severity="high",
                status="open",
                title="Card spend: a second spelling of one card",
                explanation="",
                evidence={
                    "account_id": CARD_UNCOMPUTED_ACCOUNT_ID,
                    "institution": CARD_INSTITUTION,
                    "account_type": f"{PRIVATE_CARD_NUMBER} Terms And Conditions",
                    "net": -120.0,
                    "volume": 120.0,
                    "transaction_count": 1,
                },
                suggested_action="",
                created_at=INSIGHT_COMPUTED_AT,
            )
        )
    service = LedgerMCPService(db_path)
    try:
        data = _data(call_tool(service, "ledger.insights", _ALL))
    finally:
        service.close()
    cards = [entry for entry in data["entries"] if entry["finding_type"] == "credit_cards"]

    assert len(cards) == 2
    assert {entry["institution"] for entry in cards} == {CARD_INSTITUTION}
    assert {entry["account_ref"] for entry in cards} == {
        banking_account_ref(ACCOUNT_REF_SALT, CARD_INSTITUTION, PRIVATE_CARD_NUMBER, "credit_card"),
        banking_account_ref(
            ACCOUNT_REF_SALT,
            CARD_INSTITUTION,
            PRIVATE_UNCOMPUTED_CARD_NUMBER,
            "credit_card",
        ),
    }
    assert all(set(entry) == set(cards[0]) for entry in cards)
    # And the envelope says so, instead of leaving a caller to read them as two
    # accounts, or as one account counted twice.
    assert "same institution and type remain distinguishable" in INSIGHT_ACCOUNT_IDENTITY_STATEMENT
    assert data["account_identity_semantics"] == INSIGHT_ACCOUNT_IDENTITY_STATEMENT


# --- 2. what does cross ------------------------------------------------------


def test_every_entry_carries_the_common_fields_and_only_its_types_scalars(
    service: LedgerMCPService,
) -> None:
    entries = _entries(service)
    branches = _finding_branches()

    for entry in entries:
        branch = branches[entry["finding_type"]]
        assert set(entry) == set(branch["properties"]), entry["finding_type"]
        assert set(branch["required"]) == set(branch["properties"]), entry["finding_type"]
        assert branch["additionalProperties"] is False
        assert entry["period_start"] == INSIGHT_PERIOD_START.isoformat()
        assert entry["period_end"] == INSIGHT_PERIOD_END.isoformat()


def test_the_typed_scalars_are_the_ones_the_type_guarantees(
    service: LedgerMCPService,
) -> None:
    by_type = {entry["finding_type"]: entry for entry in _entries(service)}

    assert by_type["anomalies"]["merchant"] == "SYNTHETIC MARKET"
    assert by_type["anomalies"]["category"] == "Groceries"
    assert by_type["anomalies"]["date"] == "2025-01-20"
    assert by_type["anomalies"]["evidence_id"] == INSIGHT_ANOMALY_TXN_ID
    assert by_type["anomalies"]["amount"] == "10.10"
    # An overrun is served as its MAGNITUDE: the stored `variance` is
    # budget - actual, so it is negative exactly when the finding exists.
    assert by_type["budget"]["amount"] == "50.00"
    assert by_type["category_drift"]["amount"] == "20.30"
    assert by_type["cost_cutting"]["amount"] == "30.30"
    # Likewise the card group's stored `net`, which is negative for spend.
    assert by_type["credit_cards"]["amount"] == "530.30"
    assert by_type["credit_cards"]["institution"] == CARD_INSTITUTION
    assert by_type["credit_cards"]["account_ref"].startswith("acct-")
    assert by_type["recurring"]["merchant"] == RECURRING_SUBSCRIPTION
    assert by_type["recurring"]["institution"] == BANK_INSTITUTION
    assert by_type["recurring"]["amount"] == "12.50"
    assert by_type["recurring"]["date"] == "2025-02-10"
    # The transfer-quality finding guarantees no scalar at all: its evidence is
    # transfer match statistics, which `ledger.transfers` reports as its own
    # pairing block over the same window.
    assert set(by_type["quality"]) == {
        "action_priority",
        "finding_type",
        "period_end",
        "period_start",
        "preference",
        "severity",
        "status",
    }


def test_the_quality_branch_does_not_route_a_caller_to_a_whole_database_count(
    service: LedgerMCPService,
) -> None:
    """`ledger.quality`'s unmatched-transfer count is NOT this finding's number.

    The finding is period-scoped; `DataQualityService._transfer_check` calls
    `get_transfer_stats()` with no date arguments, so its check is
    whole-database. The independently authored fixture gives the two scopes
    different counts so an incorrect referral cannot appear numerically valid.
    The branch must disclose the scope of the count it recommends.
    """
    branch = _finding_branches()["quality"]
    quality = _data(call_tool(service, "ledger.quality", {"limit": 100}))
    served = next(
        entry for entry in quality["entries"] if entry["check_name"] == "unmatched_transfers"
    )

    # The two really are different numbers on this database, or the sentence
    # below would be describing a distinction the fixture cannot show.
    assert served["issue_count"] == QUALITY_UNMATCHED_TRANSFER_ISSUES
    assert served["issue_count"] != INSIGHT_QUALITY_UNMATCHED_LEGS
    assert "ledger.transfers" in branch["description"]
    assert "whole-database" in branch["description"]
    assert "two numbers for one fact" not in branch["description"]


def test_the_evidence_id_resolves_on_the_tool_that_accepts_one(
    service: LedgerMCPService,
) -> None:
    """The one join this surface offers, exercised end to end.

    `txn_id` is emitted as `evidence_id` deliberately: an anomaly finding is
    ABOUT one transaction, and without the id an agent has to re-identify it by
    fuzzy-matching merchant, date and amount. The id grants no access a search
    over the same period does not already grant -- `ledger.transaction.get` omits
    accounts, paths and raw descriptions like every other surface.
    """
    entry = next(e for e in _entries(service) if e["finding_type"] == "anomalies")

    resolved = _data(
        call_tool(service, "ledger.transaction.get", {"evidence_id": entry["evidence_id"]})
    )

    assert resolved["item"]["evidence_id"] == entry["evidence_id"]


def test_the_recurring_finding_states_the_two_caveats_its_values_need(
    service: LedgerMCPService,
) -> None:
    """Both are the published constants, not a paraphrase.

    Every `recurring` finding comes from `payload["subscriptions"]`, which is
    `pattern_type == "subscription"` -- the detector's FALLTHROUGH DEFAULT for any
    recurring expense it could not classify. And its amount is the stored
    `expected_amount`, a long-run mean rather than the current payment.
    """
    data = _data(call_tool(service, "ledger.insights", _ALL))

    assert data["classification_semantics"] == RECURRING_CLASSIFICATION_CAVEAT
    assert data["amount_semantics"] == INSIGHT_AMOUNT_STATEMENT
    assert "fallthrough default" in RECURRING_CLASSIFICATION_CAVEAT
    assert "long-run mean" in INSIGHT_AMOUNT_STATEMENT
    assert "never comparable across finding types" in INSIGHT_AMOUNT_STATEMENT


def test_the_withheld_fields_are_named_on_the_wire(service: LedgerMCPService) -> None:
    data = _data(call_tool(service, "ledger.insights", _ALL))

    assert data["withheld_field_semantics"] == INSIGHT_WITHHELD_FIELD_STATEMENT
    for field in ("title", "explanation", "suggested_action", "evidence"):
        assert field in INSIGHT_WITHHELD_FIELD_STATEMENT, field


# --- 3. the vocabularies -----------------------------------------------------


def test_the_wire_severity_enum_is_exactly_what_the_producers_can_emit(
    tmp_path: Path,
) -> None:
    """Asserted, never assumed: the storage column is VARCHAR with no CHECK.

    Both halves. `_severity` is swept across its own thresholds, and the real
    engine is run on a fixture where all ten producers fire, so a producer that
    hardcodes a severity is covered as well as the shared helper.
    """
    engine = InsightEngine(build_insight_producer_database(tmp_path / "ledger.db"))
    swept = {
        InsightEngine._severity(amount)
        for amount in (0.0, 1.0, 249.99, 250.0, 999.99, 1000.0, 1_000_000.0)
    }
    produced = {
        finding.severity
        for finding in engine.generate(INSIGHT_PRODUCER_PERIOD_START, INSIGHT_PRODUCER_PERIOD_END)
    }
    published = _finding_branches()["cost_cutting"]["properties"]["severity"]["enum"]

    assert swept == {"high", "medium", "low"}
    assert produced, "the producer fixture generated no findings"
    assert produced <= swept
    assert set(published) == set(INSIGHT_SEVERITIES) == swept
    # The UI's five-value rank is not the producible set, and the wire takes the
    # producible one.
    assert "critical" not in published
    assert "info" not in published


def test_the_wire_status_enum_includes_the_one_the_write_guard_refuses(
    tmp_path: Path,
) -> None:
    """`resolved` is readable and NOT writable, proven from both sides.

    `refresh_period` writes it when a finding no longer regenerates, and
    `update_status` refuses it, because `VALID_STATUSES` omits it. A wire enum
    taken from the write guard would omit a status the refresh can produce.
    """
    db_path = build_insight_producer_database(tmp_path / "ledger.db")
    engine = InsightEngine(db_path)
    engine.refresh_period(INSIGHT_PRODUCER_PERIOD_START, INSIGHT_PRODUCER_PERIOD_END)
    with session_scope(db_path) as session:
        orphan = InsightFindingDB(
            fingerprint="orphaned-by-the-next-refresh",
            period_start=INSIGHT_PRODUCER_PERIOD_START,
            period_end=INSIGHT_PRODUCER_PERIOD_END,
            finding_type="cost_cutting",
            severity="low",
            title="A finding no producer regenerates",
            explanation="",
            evidence={},
            suggested_action="",
            status="open",
            created_at=datetime(2030, 1, 1),
        )
        session.add(orphan)
    engine.refresh_period(INSIGHT_PRODUCER_PERIOD_START, INSIGHT_PRODUCER_PERIOD_END)
    with session_scope(db_path) as session:
        reconciled = (
            session.query(InsightFindingDB)
            .filter(InsightFindingDB.fingerprint == "orphaned-by-the-next-refresh")
            .one()
        )
        assert reconciled.status == "resolved"
        finding_id = int(
            session.query(InsightFindingDB.id)
            .filter(InsightFindingDB.fingerprint == "orphaned-by-the-next-refresh")
            .scalar()
        )
    published = _finding_branches()["cost_cutting"]["properties"]["status"]["enum"]

    assert "resolved" not in VALID_STATUSES
    with pytest.raises(ValueError):
        engine.update_status(finding_id, "resolved")
    assert set(published) == set(INSIGHT_STATUSES) == VALID_STATUSES | {"resolved"}


def test_every_served_status_and_severity_is_inside_the_published_enum(
    service: LedgerMCPService,
) -> None:
    entries = _entries(service)
    branches = _finding_branches()

    for entry in entries:
        branch = branches[entry["finding_type"]]
        assert entry["severity"] in branch["properties"]["severity"]["enum"], entry
        assert entry["status"] in branch["properties"]["status"]["enum"], entry
    # The fixture actually exercises the interesting ones, or the loop above is
    # a check over three uniform rows.
    assert {entry["status"] for entry in entries} == set(INSIGHT_STATUSES)
    assert {entry["severity"] for entry in entries} == set(INSIGHT_SEVERITIES)


# --- 4. scope ----------------------------------------------------------------


def test_the_scope_is_the_newest_period_and_the_older_one_is_not_served(
    service: LedgerMCPService,
) -> None:
    """Every new statement mints a fresh row set for a fresh trailing window.

    Orphan reconciliation is scoped to the exact `(period_start, period_end)`
    pair and nothing prunes, so a previous period's rows stay `open` forever. An
    unscoped surface would serve them beside the current ones with nothing to
    tell them apart but two dates.
    """
    data = _data(call_tool(service, "ledger.insights", _ALL))
    encoded = json.dumps(data, ensure_ascii=False)

    assert data["scoped_period"] == {
        "end": INSIGHT_PERIOD_END.isoformat(),
        "start": INSIGHT_PERIOD_START.isoformat(),
    }
    assert data["period_semantics"] == INSIGHT_PERIOD_STATEMENT
    assert INSIGHT_PRIOR_PERIOD_MERCHANT not in encoded
    assert all(
        entry["period_start"] == INSIGHT_PERIOD_START.isoformat() for entry in data["entries"]
    )


def test_a_narrower_window_sharing_the_newest_end_date_does_not_capture_the_surface(
    service: LedgerMCPService,
) -> None:
    """The equal-`period_end` tiebreak, which nothing covered.

    The Streamlit Insights page refreshes the OPERATOR's selected sidebar
    period, so a refresh over one month while the newest transaction sits inside
    it stores a second period whose `period_end` ties the standing window's.
    An update does not bump `created_at`, and ordering by
    `period_end DESC, period_start DESC` would prefer the narrower window when
    end dates tie, even after the wider window is refreshed.

    The rule the wire publishes is the widest, and it is published as a rule
    rather than as a claim about how the window was computed, because the server
    cannot know which refresh wrote a stored pair.
    """
    data = _data(call_tool(service, "ledger.insights", _ALL))
    encoded = json.dumps(data, ensure_ascii=False)

    assert data["scoped_period"] == {
        "end": INSIGHT_PERIOD_END.isoformat(),
        "start": INSIGHT_PERIOD_START.isoformat(),
    }
    # The fixture's two windows really do tie on the end date, or the assertion
    # above never reached the tiebreak.
    assert INSIGHT_ADHOC_PERIOD_END == INSIGHT_PERIOD_END
    assert INSIGHT_ADHOC_PERIOD_START > INSIGHT_PERIOD_START
    assert INSIGHT_ADHOC_MERCHANT not in encoded
    assert "widest" in INSIGHT_PERIOD_STATEMENT


def test_the_period_statement_never_claims_the_window_is_the_trailing_one(
    service: LedgerMCPService,
) -> None:
    """The const is served at read time; the trailing window is a refresh-time fact.

    `ledger.insights` cannot know which refresh wrote a stored pair, and the
    Insights page refreshes whatever period the operator selected, so a sentence
    asserting the served pair IS a rolling trailing window ending at the latest
    imported transaction is false for exactly the response the fixture produces.
    What the const may state is the selection RULE plus where the trailing
    window comes from.
    """
    data = _data(call_tool(service, "ledger.insights", _ALL))

    assert data["period_semantics"] == INSIGHT_PERIOD_STATEMENT
    assert "The period is a rolling trailing window" not in INSIGHT_PERIOD_STATEMENT
    assert "scoped_period" in INSIGHT_PERIOD_STATEMENT
    # The trailing window is still named -- as what the SCHEDULED refresh uses.
    assert "analysis refresh" in INSIGHT_PERIOD_STATEMENT
    assert "operator" in INSIGHT_PERIOD_STATEMENT


def test_computed_at_says_when_the_served_rows_were_computed(
    service: LedgerMCPService,
) -> None:
    """Otherwise stale derived state is presented as current with no wire signal.

    The served window is the OLDER computation of the fixture's two tying
    periods, so a caller with only `scoped_period` cannot tell how old these
    findings are; `coverage.available_end` moving past `computed_at` is the
    signal that transactions were imported after this analysis ran, and
    `ledger.quality`'s `insight_freshness` check cannot supply it because it is
    served from a run that is stale in the same way.
    """
    result = call_tool(service, "ledger.insights", _ALL)
    data = _data(result)

    assert data["computed_at"] == INSIGHT_COMPUTED_AT.isoformat()
    assert data["computed_at"] != INSIGHT_ADHOC_COMPUTED_AT.isoformat()
    assert "computed_at" in INSIGHT_PERIOD_STATEMENT
    # Emitted beside the coverage block a caller compares it against.
    assert result["structuredContent"]["coverage"]["available_end"]


def test_an_empty_table_reports_no_period_rather_than_a_zero_period(
    tmp_path: Path,
) -> None:
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    with session_scope(db_path) as session:
        session.query(InsightFindingDB).delete()
    service = LedgerMCPService(db_path)
    try:
        data = _data(call_tool(service, "ledger.insights", _ALL))
    finally:
        service.close()

    assert data["scoped_period"] is None
    assert data["computed_at"] is None
    assert data["entries"] == []
    assert data["entry_count"] == 0
    assert data["unservable_count"] == 0


# --- 5. counts ---------------------------------------------------------------


def test_the_counts_describe_the_whole_period_and_reconcile_with_each_other(
    service: LedgerMCPService,
) -> None:
    data = _data(call_tool(service, "ledger.insights", {"limit": 2}))

    assert data["entry_count"] == 7
    assert sum(data["severity_counts"].values()) == data["entry_count"]
    assert sum(data["status_counts"].values()) == data["entry_count"]
    assert set(data["severity_counts"]) == set(INSIGHT_SEVERITIES)
    assert set(data["status_counts"]) == set(INSIGHT_STATUSES)
    # A page, not the whole answer: the counts above are the period's.
    assert len(data["entries"]) == 2


def test_a_row_outside_the_published_vocabularies_is_counted_not_dropped(
    service: LedgerMCPService,
) -> None:
    """Degrade one row, never the tool.

    The alternative is a schema enum wide enough to admit whatever is stored,
    which is not an enum, or a validation failure that answers `corrupt_data` for
    every row because one is odd. Both stored oddities are seeded: a severity no
    producer emits, and a finding type this build has no schema for.
    """
    data = _data(call_tool(service, "ledger.insights", _ALL))

    assert data["unservable_count"] == 2
    assert data["unservable_semantics"] == INSIGHT_UNSERVABLE_STATEMENT
    assert INSIGHT_UNSERVABLE_SEVERITY not in json.dumps(data, ensure_ascii=False)
    assert INSIGHT_UNSERVABLE_TYPE not in json.dumps(data, ensure_ascii=False)
    assert data["entry_count"] + data["unservable_count"] == 9


def test_the_triage_statement_says_the_status_is_operator_state(
    service: LedgerMCPService,
) -> None:
    """`insight_findings` is explicitly NOT drift-exempt, unlike the quality
    table: `readiness.py` says so in prose, because the status column holds
    triage no recompute can reproduce."""
    data = _data(call_tool(service, "ledger.insights", _ALL))

    assert data["triage_semantics"] == INSIGHT_TRIAGE_STATEMENT
    assert "resolved" in INSIGHT_TRIAGE_STATEMENT
    assert "ledger.quality" in INSIGHT_TRIAGE_STATEMENT


# --- 6. pagination -----------------------------------------------------------


def test_a_cursor_walk_returns_every_finding_exactly_once(
    service: LedgerMCPService,
) -> None:
    seen: list[dict[str, Any]] = []
    arguments: dict[str, object] = {"limit": 2}
    for _ in range(10):
        result = call_tool(service, "ledger.insights", arguments)
        page = result["structuredContent"]["page"]  # type: ignore[index]
        seen.extend(_data(result)["entries"])
        if not page["has_more"]:
            break
        arguments = {"limit": 2, "cursor": page["next_cursor"]}

    assert len(seen) == 7
    # The order the envelope claims: by finding type, then by the order the rows
    # were stored in. NEVER by severity -- a page of these is not a triage
    # ranking, and a list that looked ranked and was not is the class of defect
    # this release exists to remove.
    assert [entry["finding_type"] for entry in seen] == sorted(
        entry["finding_type"] for entry in seen
    )
    assert "never by severity" in INSIGHT_TRIAGE_STATEMENT
    assert [entry["severity"] for entry in seen] != sorted(
        (entry["severity"] for entry in seen), key=("high", "medium", "low").index
    )


def test_a_cursor_minted_against_another_generation_is_refused(
    db_path: Path,
) -> None:
    service = LedgerMCPService(db_path)
    try:
        first = call_tool(service, "ledger.insights", {"limit": 1})
        cursor = first["structuredContent"]["page"]["next_cursor"]
    finally:
        service.close()
    with session_scope(db_path) as session:
        session.query(InsightFindingDB).filter(InsightFindingDB.finding_type == "quality").delete()
    moved = LedgerMCPService(db_path)
    try:
        replayed = call_tool(moved, "ledger.insights", {"limit": 1, "cursor": cursor})
    finally:
        moved.close()

    assert replayed["isError"] is True
    assert replayed["structuredContent"]["error"]["code"] == "invalid_cursor"


def test_a_stored_period_past_the_scan_cap_answers_filter_too_broad(
    db_path: Path,
) -> None:
    """A code naming a filter, on a tool whose only inputs are cursor and limit.

    Nothing exercised this cap, and nothing named the tool in the contract's own
    explanation of why a non-retryable `filter_too_broad` can reach a request
    with nothing to narrow -- that comment listed `ledger.balances` and
    `ledger.projection` only. The condition is a property of the database, not
    of the call, which is exactly what makes it non-retryable here.
    """
    with session_scope(db_path) as session:
        session.add_all(
            InsightFindingDB(
                fingerprint=f"synthetic-scan-cap-{index}",
                period_start=INSIGHT_PERIOD_START,
                period_end=INSIGHT_PERIOD_END,
                finding_type="cost_cutting",
                severity="low",
                title="Over the cap",
                explanation="",
                evidence={"merchant": "SYNTHETIC MARKET", "amount": 1.0},
                suggested_action="",
                status="open",
                created_at=INSIGHT_COMPUTED_AT,
            )
            for index in range(_MAX_INSIGHT_FINDINGS + 1)
        )
    service = LedgerMCPService(db_path)
    try:
        refused = call_tool(service, "ledger.insights", {"limit": 1})
    finally:
        service.close()

    assert refused["isError"] is True
    assert refused["structuredContent"]["error"] == {
        "code": "filter_too_broad",
        "retryable": False,
    }
    # The tool really does take no filter, so there is nothing the caller could
    # narrow in response.
    assert set(_schema()["inputSchema"]["properties"]) == {"cursor", "limit"}


# --- 7. the producers -------------------------------------------------------
#
# Every evidence key each producer writes, and what this surface does with it.
# A key in neither column is a key nobody decided about, and the test below
# refuses one.
_EMITTED: dict[str, dict[str, str]] = {
    "anomalies": {
        "amount": "amount",
        "category": "category",
        "date": "date",
        "merchant": "merchant",
        "txn_id": "evidence_id",
    },
    "budget": {"category": "category", "variance": "amount"},
    "category_drift": {"category": "category", "change": "amount"},
    "cost_cutting": {"amount": "amount", "merchant": "merchant"},
    "credit_cards": {
        "account_id": "account_ref",
        "institution": "institution",
        "net": "amount",
    },
    "possible_duplicates": {
        "amount": "amount",
        "category": "category",
        "date": "date",
        "merchant": "merchant",
        "related_txn_id": "related_evidence_id",
        "txn_id": "evidence_id",
    },
    "quality": {},
    "recurring": {
        "category": "category",
        "expected_amount": "amount",
        "first_occurrence": "date",
        "institution": "institution",
        "merchant": "merchant",
    },
    "recurring_price_increase": {
        "amount": "amount",
        "category": "category",
        "date": "date",
        "merchant": "merchant",
        "related_txn_id": "related_evidence_id",
        "txn_id": "evidence_id",
    },
    "unresolved_reimbursements": {
        "amount": "amount",
        "category": "category",
        "date": "date",
        "merchant": "merchant",
        "txn_id": "evidence_id",
    },
}
_WITHHELD: dict[str, dict[str, str]] = {
    "anomalies": {},
    "budget": {
        "actual": "a second amount whose meaning the type does not fix",
        "budget": "a second amount whose meaning the type does not fix",
        "pct_used": "nullable, and derivable from budget and actual",
        "status": "always `over`; the finding does not exist otherwise",
    },
    "category_drift": {
        "change_pct": "0 for a category that appeared from nothing, so not a ratio",
        "current": "a second amount whose meaning the type does not fix",
        "direction": "always `up`; the producer skips a change of 100 or less",
        "is_new": "derivable from previous == 0",
        "previous": "a second amount whose meaning the type does not fix",
    },
    "cost_cutting": {"reason": "free text from a two-value producer vocabulary"},
    "credit_cards": {
        "account_type": "private account slug copied into an evidence field",
        "transaction_count": "a count of rows in a group this surface does not name",
        "volume": "a second amount whose meaning the type does not fix",
    },
    "possible_duplicates": {
        "issue": "fixed by the finding type",
        "possibility": "always detected; the branch states it is not a verdict",
    },
    "quality": {
        "match_rate": "a ratio ledger.transfers reports as its pairing block",
        "matched_pairs": "reported by ledger.transfers",
        "matched_transfers": "reported by ledger.transfers",
        "total_transfers": "reported by ledger.transfers",
        "unmatched_transfers": (
            "reported by ledger.transfers over this period; ledger.quality's "
            "check of the same name is whole-database and is a different number"
        ),
    },
    "recurring": {
        "confidence": "the detector's own score, not a fact about the money",
        "frequency": "the cadence label; ledger.recurring serves it with the pattern",
        "is_active": "a decaying stored flag; ledger.recurring serves it with its caveat",
        "last_occurrence": "a second date whose meaning the type does not fix",
        "missed_count": "FLOORS, so it needs the statement ledger.projection publishes",
        "next_expected_date": "retrospective; same",
        "occurrence_count": "a count of rows behind a pattern",
        "pattern_id": "an unsalted account-derived SHA-256",
        "pattern_type": "always `subscription`; the producer reads that list only",
    },
    "recurring_price_increase": {
        "current_amount": "a second amount available through the evidence rows",
        "issue": "fixed by the finding type",
        "pattern_id": "an unsalted account-derived SHA-256",
        "possibility": "always detected; the branch states it is not proof",
        "previous_amount": "a second amount available through the evidence rows",
        "previous_date": "a second date available through the evidence rows",
    },
    "unresolved_reimbursements": {
        "issue": "fixed by the finding type",
        "possibility": "always detected; the branch states it needs review",
    },
}


def _produced_evidence_keys(db_path: Path) -> dict[str, set[str]]:
    findings = InsightEngine(db_path).generate(
        INSIGHT_PRODUCER_PERIOD_START, INSIGHT_PRODUCER_PERIOD_END
    )
    produced: dict[str, set[str]] = {}
    for finding in findings:
        produced.setdefault(finding.finding_type, set()).update(finding.evidence)
    return produced


def test_every_producer_key_is_either_emitted_or_deliberately_withheld(
    tmp_path: Path,
) -> None:
    """Every producer key is either emitted or deliberately withheld."""
    produced = _produced_evidence_keys(build_insight_producer_database(tmp_path / "ledger.db"))

    assert set(produced) == set(_EMITTED) == set(_WITHHELD), sorted(produced)
    for finding_type, keys in sorted(produced.items()):
        decided = set(_EMITTED[finding_type]) | set(_WITHHELD[finding_type])
        assert keys == decided, (finding_type, sorted(keys ^ decided))


def test_each_finding_types_schema_serves_exactly_its_decided_keys(
    tmp_path: Path,
) -> None:
    """The other direction: the schema may not name a field no producer feeds."""
    produced = _produced_evidence_keys(build_insight_producer_database(tmp_path / "ledger.db"))
    branches = _finding_branches()
    common = {
        "action_priority",
        "finding_type",
        "period_end",
        "period_start",
        "preference",
        "severity",
        "status",
    }

    assert set(branches) == set(produced)
    for finding_type, branch in sorted(branches.items()):
        served = set(branch["properties"]) - common
        assert served == set(_EMITTED[finding_type].values()), finding_type
        assert set(_EMITTED[finding_type]) <= produced[finding_type], finding_type


def test_the_engine_produces_no_finding_type_the_schema_cannot_serve(
    tmp_path: Path,
) -> None:
    db_path = build_insight_producer_database(tmp_path / "ledger.db")
    engine = InsightEngine(db_path)
    engine.refresh_period(INSIGHT_PRODUCER_PERIOD_START, INSIGHT_PRODUCER_PERIOD_END)
    service = LedgerMCPService(db_path)
    try:
        data = _data(call_tool(service, "ledger.insights", _ALL))
    finally:
        service.close()

    assert data["unservable_count"] == 0, "a real refresh produced a row this surface drops"
    assert data["entry_count"] == len(data["entries"]) > 0
    # The producer fixture is built so all ten fire, so all ten arrive.
    assert {entry["finding_type"] for entry in data["entries"]} == set(_EMITTED)


def test_a_real_refresh_serves_amounts_that_match_the_stored_evidence(
    tmp_path: Path,
) -> None:
    """End to end against the real engine, not the hand-seeded fixture."""
    db_path = build_insight_producer_database(tmp_path / "ledger.db")
    InsightEngine(db_path).refresh_period(
        INSIGHT_PRODUCER_PERIOD_START, INSIGHT_PRODUCER_PERIOD_END
    )
    with session_scope(db_path) as session:
        stored = sorted(
            abs(Decimal(str(row.evidence["net"])).quantize(Decimal("0.01")))
            for row in session.query(InsightFindingDB).all()
            if str(row.finding_type) == "credit_cards"
        )
    service = LedgerMCPService(db_path)
    try:
        entries = _data(call_tool(service, "ledger.insights", _ALL))["entries"]
    finally:
        service.close()

    served = sorted(
        Decimal(entry["amount"]) for entry in entries if entry["finding_type"] == "credit_cards"
    )
    assert stored, "the refresh produced no card finding to compare against"
    assert served == stored


# --- 8. the readers ---------------------------------------------------------


@pytest.mark.parametrize(
    "stored",
    [
        # A schema-less JSON column stores whatever was put in it. None of these
        # may raise: raising inside the mapper fails the envelope and answers
        # `corrupt_data` for every other finding in the period.
        1e50,
        -1e50,
        float("inf"),
        float("nan"),
        "not a number",
        True,
        None,
        {"nested": 1},
        [1, 2],
    ],
)
def test_an_unquantizable_stored_amount_degrades_one_row_not_the_response(
    tmp_path: Path,
    stored: object,
) -> None:
    """`1e50` is the one that is not obviously hostile.

    It is a finite number that `Decimal` accepts and `quantize` refuses, because
    two decimal places on it needs more digits than the context carries. An
    unguarded `quantize` raises there, and the raise reaches the envelope.
    """
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    with session_scope(db_path) as session:
        row = (
            session.query(InsightFindingDB)
            .filter(InsightFindingDB.finding_type == "cost_cutting")
            .filter(InsightFindingDB.severity == "medium")
            .one()
        )
        row.evidence = {**dict(row.evidence), "amount": stored}
    service = LedgerMCPService(db_path)
    try:
        result = call_tool(service, "ledger.insights", _ALL)
    finally:
        service.close()
    data = _data(result)

    assert result.get("isError") is not True
    # The row is gone from the answer and counted, and every other finding is
    # still served.
    assert data["entry_count"] == 6
    assert data["unservable_count"] == 3
    assert not [entry for entry in data["entries"] if entry["finding_type"] == "cost_cutting"]
    assert str(stored) not in json.dumps(data, ensure_ascii=False)


def test_a_stored_amount_is_served_as_an_unsigned_two_decimal_magnitude(
    tmp_path: Path,
) -> None:
    """Rounding is half-up on the exact decimal, not on the float.

    The stored value is a JSON float written from a cent-quantized Decimal, so
    the digits are already there; what this pins is that they are read as an
    exact decimal rather than through binary float arithmetic, and that the sign
    is dropped rather than carried onto a wire whose amounts are magnitudes.
    """
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    with session_scope(db_path) as session:
        row = (
            session.query(InsightFindingDB)
            .filter(InsightFindingDB.finding_type == "cost_cutting")
            .filter(InsightFindingDB.severity == "medium")
            .one()
        )
        row.evidence = {**dict(row.evidence), "amount": -1.005}
    service = LedgerMCPService(db_path)
    try:
        data = _data(call_tool(service, "ledger.insights", _ALL))
    finally:
        service.close()

    served = next(entry for entry in data["entries"] if entry["finding_type"] == "cost_cutting")
    assert served["amount"] == "1.01"
    assert Decimal(served["amount"]) == Decimal("1.01")


@pytest.mark.parametrize(
    ("field", "evidence_key", "finding_type"),
    [
        ("category", "category", "budget"),
        ("institution", "institution", "credit_cards"),
        ("merchant", "merchant", "cost_cutting"),
    ],
)
def test_a_text_scalar_wider_than_its_own_column_degrades_one_row(
    tmp_path: Path,
    field: str,
    evidence_key: str,
    finding_type: str,
) -> None:
    """The mapper's bound and the published bound are the same number, per FIELD.

    `evidence` is schema-less, so a value wider than the column it was
    interpolated from is storable. A mapper looser than the schema serves it and
    the envelope fails validation, answering `corrupt_data` for every other
    finding in the period; a mapper tighter than the schema silently drops rows
    another tool would serve. Both bounds come from `INSIGHT_TEXT_MAX_LENGTHS`,
    and this is what proves the two agree at the boundary rather than nearby.
    """
    width = INSIGHT_TEXT_MAX_LENGTHS[field]
    schema = _finding_branches()[finding_type]["properties"][field]
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    with session_scope(db_path) as session:
        row = (
            session.query(InsightFindingDB)
            .filter(InsightFindingDB.finding_type == finding_type)
            .filter(InsightFindingDB.period_start == INSIGHT_PERIOD_START)
            .filter(InsightFindingDB.severity.in_(sorted(INSIGHT_SEVERITIES)))
            .first()
        )
        row.evidence = {**dict(row.evidence), evidence_key: "x" * (width + 1)}
    service = LedgerMCPService(db_path)
    try:
        result = call_tool(service, "ledger.insights", _ALL)
    finally:
        service.close()
    data = _data(result)

    assert schema["maxLength"] == width
    assert result.get("isError") is not True, result
    assert not [entry for entry in data["entries"] if entry["finding_type"] == finding_type]
    assert data["entry_count"] == 6
    assert data["unservable_count"] == 3


def test_an_unreadable_stored_timestamp_is_corrupt_data_not_invalid_argument(
    db_path: Path,
) -> None:
    """A malformed STORED value is never reported as a malformed request.

    `computed_at` is read through `_as_datetime`, which raises
    `AgentQueryError("corrupt_data")` on purpose -- and `server.py` was
    translating every `AgentQueryError` code outside a three-name allowlist to
    `invalid_argument`, so the intent never reached the wire. It is the same
    downgrade on `ledger.changes`'s import stamps and `ledger.quality`'s
    `run_at`. Both codes are non-retryable, so nothing about the client's
    behaviour changed -- what changed is that a caller told `invalid_argument`
    goes looking for a fault in a request that was never wrong.
    """
    with session_scope(db_path) as session:
        session.execute(
            text("UPDATE insight_findings SET created_at = :stamp"),
            {"stamp": "not-a-timestamp"},
        )
    service = LedgerMCPService(db_path)
    try:
        result = call_tool(service, "ledger.insights", {"limit": 100})
    finally:
        service.close()

    assert result["isError"] is True
    assert result["structuredContent"]["error"] == {
        "code": "corrupt_data",
        "retryable": False,
    }
