"""Wire, privacy, and runtime tests for the read-only Ledger MCP."""

from __future__ import annotations

import contextlib
import hmac
import json
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import TextIO

import pytest
from mcp_synthetic import (
    ACCOUNT_REF_SALT,
    BANK_INSTITUTION,
    CARD_INSTITUTION,
    CHINESE_SOURCE,
    CHINESE_TOTAL,
    NORTHSTAR_TOTAL,
    PRIVATE_ACCOUNT_NUMBER,
    PRIVATE_CARD_NUMBER,
    PRIVATE_SCHEDULE_ID,
    PRIVATE_SCHEDULE_NAME,
    PRIVATE_SCHEDULE_NOTE,
    PRIVATE_VALUES,
    _identifier,
    build_insight_producer_database,
    build_synthetic_mcp_database,
)

from ledger import __version__
from ledger.mcp.contracts import SERVER_INSTRUCTIONS, tool_schemas
from ledger.mcp.server import LedgerMCPService, call_tool, create_server, main, run_stdio
from ledger.mcp.service import _brief_observations, _safe_explanation
from ledger.planning.cash_plan import CashPlanError, CashPlanService
from ledger.query.account_refs import banking_account_ref
from ledger.query.agent import AgentQueryError
from ledger.storage.database import PlannedScheduleDB, TransferDB, session_scope

MCP_PROTOCOL_MODERN = "2026-07-28"
MCP_PROTOCOL_LEGACY = "2025-11-25"


def _artifacts(path: Path) -> dict[str, tuple[bytes, int]]:
    result: dict[str, tuple[bytes, int]] = {}
    for suffix in ("", "-journal", "-wal", "-shm"):
        candidate = Path(f"{path}{suffix}")
        if candidate.exists():
            result[suffix] = (candidate.read_bytes(), candidate.stat().st_mtime_ns)
    return result


# A filesystem path, as distinct from a merchant name containing a slash: an
# absolute path, a traversal, a directory-ish segment, or a file extension.
_PATH_SHAPED = re.compile(r"(^|\s)(/|\.{1,2}/)|/[\w.-]+/|\.(csv|db|pdf|json|jsonl|toml|py)\b")
_PUBLIC_DASHBOARD_PATHS = frozenset(
    {
        "/accounts",
        "/categories",
        "/import-center",
        "/insights",
        "/overview",
        "/plan-reconciliation",
        "/transactions",
    }
)


def _structured(result: dict[str, object]) -> dict[str, object]:
    return result["structuredContent"]  # type: ignore[return-value]


def _aggregate_arguments(source: str) -> dict[str, object]:
    return {
        "start_date": "2025-01-01",
        "end_date": "2025-12-31",
        "flow": "income",
        "group_by": "source",
        "filters": {"sources": [source]},
        "limit": 50,
    }


def test_status_is_path_free_read_only_and_honest_about_coverage(tmp_path: Path) -> None:
    db_path = build_synthetic_mcp_database(tmp_path / "private-ledger.db")
    service = LedgerMCPService(db_path)

    envelope = _structured(call_tool(service, "ledger.status", {}))
    encoded = json.dumps(envelope, ensure_ascii=False)

    assert envelope["schema"] == "ledger.mcp.v1"
    assert envelope["instruction_trust"] == "inert"
    assert envelope["data"]["product"] == "ledger"  # type: ignore[index]
    assert envelope["data"]["read_only"] is True  # type: ignore[index]
    assert envelope["data"]["database_ready"] is True  # type: ignore[index]
    assert envelope["coverage"]["completeness"] == "not_asserted"  # type: ignore[index]
    assert str(db_path) not in encoded
    assert PRIVATE_ACCOUNT_NUMBER not in encoded


def test_status_revalidates_database_identity_after_startup(tmp_path: Path) -> None:
    """A replaced database must never be served from the superseded snapshot.

    This used to assert `database_changed`, because the server refused a changed
    database until restart. It now re-pins and answers from the NEW file, which
    is the same guarantee stated positively: what it must never do is keep
    serving the old snapshot. The published generation is the proof, and it is a
    stronger assertion than the error code was -- an error tells you the server
    stopped, whereas the generation tells you which database actually answered.
    """
    db_path = build_synthetic_mcp_database(tmp_path / "private-ledger.db")
    service = LedgerMCPService(db_path)
    before = _structured(call_tool(service, "ledger.status", {}))["coverage"]["generation"]  # type: ignore[index]

    replacement = build_synthetic_mcp_database(tmp_path / "replacement.db")
    os.replace(replacement, db_path)

    result = call_tool(service, "ledger.status", {})

    assert result.get("isError") is not True, result
    after = _structured(result)["coverage"]["generation"]  # type: ignore[index]
    assert after != before, "the server answered from the superseded snapshot"
    assert str(db_path) not in json.dumps(result)


def test_status_revalidates_sidecars_after_startup(tmp_path: Path) -> None:
    """A sidecar means a writer is mid-transaction, so this one IS retryable.

    The flag used to be False here and True on the permanent-until-restart codes,
    which told a conforming client to do the wrong thing in both directions.
    """
    db_path = build_synthetic_mcp_database(tmp_path / "private-ledger.db")
    service = LedgerMCPService(db_path)
    Path(f"{db_path}-wal").write_bytes(b"synthetic-sidecar")

    result = call_tool(service, "ledger.status", {})

    assert result["isError"] is True
    assert _structured(result) == {
        "schema": "ledger.mcp.v1",
        "error": {"code": "read_only_unavailable", "retryable": True},
    }
    assert str(db_path) not in json.dumps(result)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("Northstar University", NORTHSTAR_TOTAL),
        (CHINESE_SOURCE, CHINESE_TOTAL),
    ],
)
def test_bilingual_aggregate_returns_exact_strings_matches_policy_and_coverage(
    tmp_path: Path,
    source: str,
    expected: object,
) -> None:
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    service = LedgerMCPService(db_path)

    envelope = _structured(call_tool(service, "ledger.aggregate", _aggregate_arguments(source)))

    assert envelope["data"]["total"] == f"{expected:.2f}"  # type: ignore[index]
    assert envelope["data"]["matching"]["matched_sources"]  # type: ignore[index]
    assert envelope["data"]["queried_period"] == {  # type: ignore[index]
        "start": "2025-01-01",
        "end": "2025-12-31",
    }
    assert envelope["policy"] == {
        "amount_basis": "positive_income",
        "currency": "CAD_native_only",
        "date_bounds": "inclusive",
        "exclude_from_spending": False,
        "flow": "income",
        "income_treatment_exclusions": [
            "exclude_from_income",
            "reduce_category_cost",
        ],
        "requires_include_in_cashflow": True,
    }
    assert envelope["coverage"]["relation"] == "partially_outside_available_range"  # type: ignore[index]
    assert envelope["coverage"]["completeness"] == "not_asserted"  # type: ignore[index]


def test_evidence_is_paginated_and_omits_accounts_paths_and_raw_descriptions(
    tmp_path: Path,
) -> None:
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    service = LedgerMCPService(db_path)
    arguments = {
        "start_date": "2025-01-01",
        "end_date": "2025-12-31",
        "flow": "income",
        "filters": {"sources": ["northstar university"]},
        "limit": 1,
    }

    first = _structured(call_tool(service, "ledger.transactions.search", arguments))
    second = _structured(
        call_tool(
            service,
            "ledger.transactions.search",
            {**arguments, "cursor": first["page"]["next_cursor"]},  # type: ignore[index]
        )
    )
    encoded = json.dumps([first, second], ensure_ascii=False)

    assert first["page"]["has_more"] is True  # type: ignore[index]
    assert second["page"]["has_more"] is False  # type: ignore[index]
    amounts = [
        Decimal(item["amount"])
        for page in (first, second)
        for item in page["data"]["items"]  # type: ignore[index]
    ]
    assert sum(amounts, Decimal("0.00")) == NORTHSTAR_TOTAL
    # Every raw slug, account number, source path and raw description the fixture
    # stores. This replaces a list of bare KEY substrings ("account",
    # "description", "source_file", "source_path") that a key named `account_ref`
    # would trip while emitting nothing private at all. Key names are policed
    # against an explicit allowlist by the whole-surface test below, and the
    # values are policed here, where the actual disclosure would happen.
    for private in PRIVATE_VALUES:
        assert private not in encoded, private


# Every key any tool is permitted to emit, written out rather than derived from
# the schemas: a derived list would silently bless whatever the schema grew.
_PERMITTED_KEYS = frozenset(
    {
        # Investment detail fields: retained scalars and opaque selectors only.
        "account_status",
        "activities",
        "activity_date",
        "activity_type",
        "asset_balances",
        "asset_balances_availability",
        "asset_category",
        "asset_class",
        "availability",
        "base_currency",
        "cash_balances",
        "cash_balances_availability",
        "close_price",
        "closing_balance",
        "closing_fx_rate",
        "commission_fee",
        "cost_basis",
        "cost_price",
        "credits",
        "current_long",
        "current_short",
        "current_total",
        "debits",
        "discriminator",
        "evidence_basis",
        "expected_closing_balance",
        "explicit_total",
        "explicit_total_difference",
        "explicit_total_excluded_from_components",
        "freshness_semantics",
        "investment_account_type",
        "is_latest_statement",
        "latest_statement_end",
        "market_value",
        "mtm_pl",
        "multiplier",
        "nav_reconciliation",
        "opening_balance",
        "opening_fx_rate",
        "performance_rows",
        "positions",
        "prior_total",
        "quantity",
        "realized_pl",
        "source_associations",
        "source_counts",
        "source_files",
        "statement_ref",
        "symbol",
        "time_weighted_return",
        "total_nav",
        "trade_price",
        "unrealized_pl",
        "ref",
        "revision",
        "category_ref",
        "budget_month",
        "is_parent",
        "is_credit_card",
        "subject_ref",
        "disposition",
        "provenance",
        "is_active",
        "next_date",
        "counterparty_account_ref",
        "selection_status",
        "matched_count",
        "privacy_statement",
        "occurrence_ref",
        "schedule_ref",
        "schedule_revision",
        "material_revision",
        "posting_window_days",
        "total_count",
        "returned_count",
        "findings",
        "hypothetical_occurrence_count",
        "detected_pattern_count",
        "incomplete_history_schedule_count",
        "reconciliation_statement",
        "expected_date",
        "expected_amount",
        "actual_date",
        "actual_amount",
        "amount_difference",
        "date_difference",
        "amount_changed",
        "date_changed",
        "evidence_count",
        "evidence_truncated",
        "candidate_count",
        "matching_basis",
        "reviewed",
        "actionable",
        "history_complete",
        "complete",
        "window_start",
        "window_end",
        "intervals",
        "assumptions",
        # envelope
        "coverage",
        "data",
        "error",
        "instruction_trust",
        "page",
        "policy",
        "schema",
        "code",
        "retryable",
        # coverage / page / policy blocks
        "available_end",
        "available_start",
        "completeness",
        "generation",
        "relation",
        "requested_end",
        "requested_start",
        "scope",
        "statement",
        "has_more",
        "limit",
        "next_cursor",
        "returned",
        "amount_basis",
        "currency",
        "date_bounds",
        "exclude_from_spending",
        "flow",
        "income_treatment_exclusions",
        "requires_include_in_cashflow",
        # ledger.status
        "available_date_range",
        "contract",
        "contract_digest",
        "database_ready",
        "end",
        "latest_imported_at",
        "migration_version",
        "package_version",
        "product",
        "read_only",
        "start",
        "transaction_count",
        "transport",
        # ledger.brief: calculated period/comparison blocks, bounded observation
        # codes, allowlisted dashboard route, structured driver and aggregate
        # coverage. No observation statement, title, or filesystem path is
        # permitted.
        "balances",
        "changes",
        "comparable",
        "comparison",
        "coverage_reason",
        "current",
        "dashboard_path",
        "days",
        "earliest_transaction_date",
        "evidence_ids",
        "freshness",
        "gross_spend",
        "income",
        "insights",
        "kind",
        "latest_transaction_date",
        "latest_transaction_lag_days",
        "matched_account_months",
        "maximum_anchor_lag_days",
        "metrics",
        "net",
        "observations",
        "offsets",
        "rank",
        "subject",
        "subject_kind",
        "true_cost",
        "unavailable",
        # ledger.explain: fixed formula/component vocabulary, exact arithmetic
        # reconciliation and bounded evidence rows.
        "adjustments",
        "amounts",
        "applied_to_total",
        "component",
        "component_total",
        "counts",
        "counts_may_overlap",
        "difference",
        "displayed_total",
        "earliest",
        "evidence",
        "excluded_from_spending",
        "exclusions",
        "formula",
        "inclusive",
        "income_adjustments",
        "latest",
        "metric",
        "operation",
        "other_adjustments",
        "projection",
        "reconciliation",
        "refunds",
        "reimbursements",
        "relevant_transactions",
        "role",
        "source_date_bounds",
        "truncated",
        "unavailable_reason",
        # ledger.cash_plan: opaque identities plus derived balances, typed event
        # legs and aggregate schedule/fact coverage. Schedule labels and internal
        # identifiers remain deliberately absent.
        "account_count",
        "accounts",
        "active_schedule_count",
        "actual_roll_forward_movement",
        "actual_through",
        "ambiguous_suppression_count",
        "anchored_account_count",
        "applied_planning_occurrence_count",
        "assumption_codes",
        "before_next_payday",
        "confirmed_schedule_count",
        "count",
        "deduplicated_representation_count",
        "end_date",
        "event_ref",
        "event_type",
        "excluded_non_cad_count",
        "excluded_pre_as_of_planning_occurrence_count",
        "fact_leg_count",
        "fact_legs",
        "facts",
        "hypothetical_schedule_count",
        "in_selected_period",
        "includes_hypothetical",
        "income_amount",
        "income_provenances",
        "is_spending",
        "lowest_projected_balance",
        "lowest_projected_date",
        "missing_anchor_account_refs",
        "movement_since_anchor",
        "occurrence_count",
        "occurrence_date",
        "omitted_detected_patterns",
        "opening_projected_balance",
        "overridden_detected_pattern_count",
        "payday_date",
        "planned_movement",
        "planning_legs",
        "projected_balance_before_payday",
        "projected_movement",
        "reason",
        "source_kind",
        "start_date",
        "stored_schedule_count",
        "suppressed_occurrence_count",
        "suppressions",
        "transaction_leg_count",
        "transfer_leg_count",
        "unmapped_account_count",
        # ledger.aggregate
        "any_reason",
        "blank_merchant",
        "candidate_rows",
        "excluded",
        "group_by",
        "group_count",
        "groups",
        "income_treatment",
        "key",
        "matched_categories",
        "matched_institutions",
        "matched_sources",
        "matching",
        "non_cad",
        "not_in_cashflow",
        "queried_period",
        "semantics",
        "total",
        "unmatched_categories",
        "unmatched_institutions",
        "unmatched_sources",
        # ledger.transactions.search / ledger.transaction.get
        "amount",
        "cashflow_type",
        "category",
        "evidence_id",
        "institution",
        "item",
        "items",
        "posting_date",
        "source",
        # ledger.coverage
        "absence_semantics",
        "account_ref",
        "account_ref_semantics",
        "account_ref_status",
        "account_refs",
        "account_refs_withheld_reason",
        "entries",
        "entry_count",
        "imported_at",
        "imported_no_activity",
        "imported_no_rows_unexplained",
        "imported_with_activity",
        "observed_periods",
        "period",
        "queried_periods",
        "row_count",
        "statement_count",
        "statement_type",
        "status",
        "status_counts",
        # ledger.coverage, the gaps block. Every one of these is a DERIVED verdict
        # about an account's statement rhythm: a cadence in months, how many
        # covered periods it was estimated from, the periods it predicts, and the
        # closed-vocabulary signal that decided the entry. None of them is a
        # stored value, and none of them counts anything a statement contains.
        "cadence_confidence",
        "cadence_months",
        "cadence_observations",
        "deciding_signal",
        "elapsed_months",
        "gap_semantics",
        "gaps",
        "gaps_assessed_accounts",
        "gaps_evaluated_on",
        "last_covered_period",
        "missing_period_count",
        "missing_periods",
        # The gap census: per-classification counts, the count of entries that
        # actually name a missing statement, the truncation flag, the count of
        # accounts whose stored periods cannot be scored, and the three
        # thresholds that decide every verdict. Counts and module constants, so
        # none of them can carry an account identifier.
        "dormant",
        "gap_counts",
        "gap_delivery_grace_days",
        "gap_dormancy_multiplier",
        "gap_minimum_cadence_observations",
        "gaps_truncated",
        "gaps_unscoreable_accounts",
        "naming_a_missing_statement",
        "overdue",
        "row_count_semantics",
        "unknown",
        # ledger.recurring
        "classification",
        "fallthrough_default",
        "frequency",
        "merchant",
        "missed_count",
        "next_expected_date",
        "pattern_type",
        "typical_amount",
        # ledger.transfers
        "counterparty_institution",
        "direction",
        "direction_semantics",
        "legs",
        "pairing",
        "paired_legs",
        "transfer_count",
        "with_counterparty_institution",
        # ledger.dimensions
        "contains",
        "dimension",
        "match_semantics",
        "value",
        "value_count",
        "values",
        # ledger.balances
        "account_type",
        "as_of_lag_days",
        "balance",
        "balance_basis",
        "balance_date",
        "change",
        "credit_utilization_pct",
        "credit_utilization_status",
        "from_date",
        "level",
        "level_counts",
        "level_semantics",
        "no_absolute_anchor",
        "no_anchor_on_or_before_as_of",
        "queried_as_of",
        "queried_change_since",
        "reported",
        "to_date",
        "utilization_semantics",
        # ledger.transactions.search, flow "any" only
        "flows",
        "union_semantics",
        # ledger.aggregate, group_by "account_ref" only -- every key it adds was
        # already permitted for ledger.coverage.
        # ledger.changes
        "basis",
        "basis_semantics",
        "client_generation",
        "count_semantics",
        # Discloses that investment-statement imports never advance these
        # stamps: a scope statement, no stored content.
        "investment_semantics",
        # The two counted tables' verdicts. Names of TABLES this surface counts,
        # not of anything stored: a verdict discloses no row content.
        "count_sources",
        "rows",
        "statements",
        "delta_available",
        "earliest_imported_at",
        # A boolean comparison of two opaque hashes. It discloses that the pinned
        # data moved, never what moved it or what is in it.
        "generation_changed",
        "queried_since",
        "rows_added",
        "since_relation",
        "statement_row_count",
        "statement_status",
        # ledger.recurring: what its three stored columns mean as stored, and
        # when the detector last rewrote each pattern (a timestamp, no content).
        "stored_field_semantics",
        "updated_at",
        "total_added",
        # ledger.transfers: the leg-sum basis of `total`, a statement only.
        "total_semantics",
        # ledger.balances change block: the closing observation's own date — the
        # staleness disclosure, mirroring as_of_lag_days.
        "observed_through",
        # ledger.net_worth: the census of accounts considered/left out, counts
        # by absence reason only.
        "component_counts",
        "component_counts_semantics",
        "banking_reported",
        "banking_no_anchor_on_or_before_as_of",
        "banking_no_absolute_anchor",
        "investment_reported",
        "investment_no_statement_on_or_before_as_of",
        # ledger.projection. Every key here is either a DERIVED figure -- a
        # projected balance, the exact sum of the line items, a signed offset in
        # periods -- or a count of patterns by what became of them. The pattern's
        # own identifier is deliberately not among them: it is reminted on every
        # analysis refresh and is used server-side to close an ordering.
        # `merchant`, `category`, `pattern_type`, `frequency`, `missed_count` and
        # `next_expected_date` were already permitted for ledger.recurring, and
        # `anchor`'s five fields for ledger.balances.
        "amount_semantics",
        "anchor",
        "anchor_counts",
        "anchor_semantics",
        "attribution_semantics",
        "confidence",
        "inactive",
        "is_overdue",
        "line_item_count",
        "line_items",
        "max_line_items_per_account",
        "missed_count_semantics",
        "next_expected_is_retrospective",
        "net_projected_flow",
        "no_occurrence_in_window",
        "pattern_count",
        "pattern_disposition",
        "patterns_past_due",
        "periods_from_next_expected",
        "projected",
        "projected_balance",
        "projected_date",
        "projection_horizon_days",
        # `queried_as_of` is already permitted for ledger.balances, which names
        # the same input.
        "queried_target_date",
        "reconciliation_semantics",
        "roll_forward_semantics",
        "scope_semantics",
        "transfer_direction_ambiguous",
        "unclassified_pattern_type",
        "unsupported_frequency",
        # ledger.insights. `finding_type` is a closed per-branch const; `date`
        # is a typed scalar whose meaning that const fixes; the rest are counts,
        # a period pair, and the disclosures. The four stored columns the table
        # actually holds -- `title`, `explanation`, `suggested_action`,
        # `evidence` -- are absent from this list ON PURPOSE, so adding one of
        # them back would have to be a deliberate edit here as well.
        # `amount`, `category`, `institution`, `merchant` and `evidence_id` were
        # already permitted for the transaction surfaces.
        "account_identity_semantics",
        "action_priority",
        "classification_semantics",
        # When the served findings were computed, so stale derived state is
        # visible against the coverage block rather than presented as current.
        "computed_at",
        "date",
        "finding_type",
        "period_end",
        "period_semantics",
        "period_start",
        "preference",
        "scoped_period",
        "severity",
        "severity_counts",
        "triage_semantics",
        "unservable_count",
        "unservable_semantics",
        "withheld_field_semantics",
        # The two vocabularies, as COUNT KEYS. They are closed enums this server
        # publishes and every one of them is a bucket name, never stored content.
        "high",
        "ignored",
        "low",
        "medium",
        "open",
        "resolved",
        "watch",
        "fail",
        "info",
        "pass",
        "warn",
        "warning",
        # ledger.quality. `details` is absent for the same reason the four
        # insight columns are.
        "check_name",
        "details_semantics",
        "issue_count",
        "run_at",
        "vocabulary_semantics",
        # ledger.budgets. Category is already permitted above; all other values
        # are computed amounts, counts, or fixed basis/selection disclosures.
        "actual",
        "actual_basis",
        "budget",
        "definition_count",
        "pct_used",
        "selection_semantics",
        "totals",
        "variance",
        # ledger.net_worth. Identity is the already-permitted account_ref; these
        # are fixed disclosures, signed values, dates, and whole-answer counts.
        "as_of",
        "closed_on",
        "closure_semantics",
        "component_count",
        "component_type",
        "components",
        "currency_semantics",
        "final_statement_outstanding",
        "quantization_semantics",
        "subtotals",
    }
)


def _keys(node: object) -> set[str]:
    if isinstance(node, dict):
        found = set(node)
        for value in node.values():
            found |= _keys(value)
        return found
    if isinstance(node, list):
        found = set()
        for value in node:
            found |= _keys(value)
        return found
    return set()


def _strings(node: object) -> list[str]:
    if isinstance(node, dict):
        return [text for value in node.values() for text in _strings(value)]
    if isinstance(node, list):
        return [text for value in node for text in _strings(value)]
    return [node] if isinstance(node, str) else []


def _every_tool_call(service: LedgerMCPService) -> list[tuple[str, dict[str, object]]]:
    """Every registered tool, with the arguments that walk its widest answer.

    The NAME travels with each call so the audit below can compare this list
    against `tool_schemas()` instead of trusting it. More than one entry per tool
    is fine and several tools have one; what must never happen is a registered
    tool with no entry.
    """
    period: dict[str, object] = {"start_date": "2025-01-01", "end_date": "2025-12-31"}
    identity = _identifier("investment-statement:active-latest", length=64)
    fingerprint = _identifier("investment-fingerprint:active-latest", length=64)
    investment_ref = (
        "stmt-"
        + hmac.new(
            ACCOUNT_REF_SALT.encode(),
            f"investment-statement|{identity}|{fingerprint}".encode(),
            "sha256",
        ).hexdigest()[:24]
    )
    return [
        ("ledger.status", {}),
        ("ledger.planning.definitions", {"limit": 100}),
        (
            "ledger.plan_reconciliation",
            {"start_date": "2025-07-01", "end_date": "2025-07-31", "as_of": "2025-08-10"},
        ),
        ("ledger.brief", period),
        ("ledger.explain", {**period, "metric": "net"}),
        (
            "ledger.cash_plan",
            {"start_date": "2025-06-20", "as_of": "2025-06-20", "end_date": "2025-07-20"},
        ),
        ("ledger.aggregate", _aggregate_arguments("Northstar University")),
        # The account grouping too: it is the one grouping whose KEY is an
        # account identity, so it is the one that could put a raw slug on the
        # wire, and the value check below is where that would be caught.
        (
            "ledger.aggregate",
            {**period, "flow": "spending", "group_by": "account_ref", "limit": 100},
        ),
        ("ledger.transactions.search", {**period, "flow": "income", "limit": 50}),
        # The union flow too: its policy block carries two keys no single-flow
        # answer emits, and an allowlist that never walks them polices nothing.
        ("ledger.transactions.search", {**period, "flow": "any", "limit": 50}),
        ("ledger.transaction.get", {"evidence_id": _identifier("northstar-one", length=40)}),
        ("ledger.coverage", {"limit": 100}),
        ("ledger.recurring", {"status": "any", "limit": 100}),
        ("ledger.transfers", {**period, "limit": 100}),
        (
            "ledger.dimensions",
            {**period, "dimension": "sources", "flow": "spending", "limit": 100},
        ),
        # With `change_since`, so the change block is walked too: it is the one
        # place a relative figure reaches the wire at all.
        (
            "ledger.balances",
            {"as_of": "2025-12-31", "change_since": "2025-06-10", "limit": 100},
        ),
        # A window wide enough that line items are actually served on more than
        # one account: an answer whose entries all carry an empty list would walk
        # none of the per-line-item keys, which are where a merchant, a category
        # and a derived date reach this wire.
        (
            "ledger.projection",
            {"as_of": "2025-06-30", "target_date": "2025-08-31", "limit": 100},
        ),
        # Boundary before every fixture stamp, so the entries are actually served
        # and the per-entry keys are walked. An answer with no entries in it would
        # prove nothing about them.
        (
            "ledger.changes",
            {
                "since": "2029-01-01T00:00:00",
                "client_generation": service.snapshot.generation,
                "limit": 100,
            },
        ),
        # The two surfaces over tables whose payload columns have no schema at
        # all: a finding's `evidence` blob and a check's `details` blob. The
        # fixture seeds a finding whose evidence carries the raw account slug and
        # a check whose details interpolate a merchant name, so the value scan
        # below is reading the actual leak rather than an empty column.
        ("ledger.insights", {"limit": 100}),
        ("ledger.quality", {"limit": 100}),
        ("ledger.budgets", period),
        ("ledger.net_worth", {"as_of": "2025-12-31", "limit": 100}),
        ("ledger.investments.statements", {**period, "limit": 100}),
        ("ledger.investments.statement.get", {"statement_ref": investment_ref}),
        ("ledger.investments.positions", {"statement_ref": investment_ref, "limit": 100}),
        ("ledger.investments.activities", {"statement_ref": investment_ref, "limit": 100}),
    ]


def _every_tool(service: LedgerMCPService) -> list[dict[str, object]]:
    return [call_tool(service, name, arguments) for name, arguments in _every_tool_call(service)]


def test_the_privacy_audit_calls_every_registered_tool(tmp_path: Path) -> None:
    """The audit's tool list is hand-written; `tool_schemas()` is what ships.

    Nothing compared them, so a tool added to the server and forgotten here would
    be policed by the key allowlist and the private-value scan -- the last line of
    defence for account numbers, raw slugs, source paths and descriptions --
    exactly not at all, silently, behind a green suite. P2 through P8 each add a
    surface. Same shape as the account-ref-semantics defect one file over, and the
    same fix: DERIVE the audit from the published schemas instead of listing it.

    Set equality in both directions: a name called here but no longer registered
    is a stale case that proves nothing about the shipped surface.
    """
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    service = LedgerMCPService(db_path)
    try:
        called = {name for name, _arguments in _every_tool_call(service)}
    finally:
        service.close()

    assert called == {str(tool["name"]) for tool in tool_schemas()}


def test_no_tool_emits_a_key_outside_the_allowlist_or_a_private_value(
    tmp_path: Path,
) -> None:
    """The whole surface, keys and values, against one explicit permission list.

    Stronger than the bare substring check it replaces, which could only ever see
    one tool's envelope and confused a KEY named `account_ref` with a disclosed
    account. Here a new key is refused unless it was added to the allowlist
    deliberately, and no emitted VALUE may carry a raw slug, account number,
    source path, or raw description.
    """
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    stored = db_path.read_bytes()
    service = LedgerMCPService(db_path)
    try:
        results = _every_tool(service)
    finally:
        service.close()

    # The fixture must actually carry every private value, or the assertion
    # below passes by having nothing to find.
    for private in PRIVATE_VALUES:
        assert private.encode("utf-8") in stored, private
    for result in results:
        emitted = _keys(_structured(result))
        assert emitted <= _PERMITTED_KEYS, sorted(emitted - _PERMITTED_KEYS)
        encoded = json.dumps(result, ensure_ascii=False)
        for private in PRIVATE_VALUES:
            assert private not in encoded, private
        # Nothing path-shaped, whatever the fixture happens to store: the
        # markers above only catch the paths this fixture knows about.
        for text in _strings(_structured(result)):
            if text in _PUBLIC_DASHBOARD_PATHS:
                continue
            # Not a bare "/": real merchant names legitimately contain one
            # (BLUE JAYS 50/50, CRA / CCRA), and ledger.dimensions emits
            # merchants. What must never appear is a PATH shape.
            assert not _PATH_SHAPED.search(text), text


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("ledger.aggregate", {"start_date": "private-marker"}),
        (
            "ledger.aggregate",
            {
                "start_date": "2025-01-01",
                "end_date": "2025-12-31",
                "flow": "income",
                "group_by": "none",
                "filters": {"account": "private-marker"},
            },
        ),
        ("ledger.mutate", {"value": "private-marker"}),
    ],
)
def test_invalid_requests_fail_content_free_without_echoing_values_or_paths(
    tmp_path: Path,
    tool: str,
    arguments: dict[str, object],
) -> None:
    db_path = build_synthetic_mcp_database(tmp_path / "private-ledger.db")
    service = LedgerMCPService(db_path)

    result = call_tool(service, tool, arguments)
    encoded = json.dumps(result, ensure_ascii=False)

    assert result["isError"] is True
    assert _structured(result) == {
        "schema": "ledger.mcp.v1",
        "error": {"code": "invalid_argument", "retryable": False},
    }
    assert "private-marker" not in encoded
    assert str(db_path) not in encoded


def test_direct_tool_calls_leave_database_and_sidecars_byte_identical(tmp_path: Path) -> None:
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    before = _artifacts(db_path)
    service = LedgerMCPService(db_path)
    try:
        call_tool(service, "ledger.status", {})
        call_tool(service, "ledger.aggregate", _aggregate_arguments("Northstar University"))
        call_tool(
            service,
            "ledger.transactions.search",
            {
                "start_date": "2025-01-01",
                "end_date": "2025-12-31",
                "flow": "income",
                "limit": 20,
            },
        )
        call_tool(
            service,
            "ledger.brief",
            {"start_date": "2025-01-01", "end_date": "2025-12-31"},
        )
        call_tool(
            service,
            "ledger.explain",
            {"metric": "net", "start_date": "2025-01-01", "end_date": "2025-12-31"},
        )
        call_tool(
            service,
            "ledger.cash_plan",
            {
                "start_date": "2025-06-20",
                "as_of": "2025-06-20",
                "end_date": "2025-07-20",
            },
        )
    finally:
        service.close()

    assert _artifacts(db_path) == before


def test_brief_is_bounded_typed_and_contains_no_narrative(tmp_path: Path) -> None:
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    # Keep this fixture's category/merchant subject projection exercised;
    # reconciliation observations have their own privacy contract below.
    with session_scope(db_path) as session:
        session.query(PlannedScheduleDB).update({PlannedScheduleDB.is_active: False})
    service = LedgerMCPService(db_path)
    try:
        result = call_tool(
            service,
            "ledger.brief",
            {
                "start_date": "2025-01-01",
                "end_date": "2025-12-31",
                "since": "2029-01-01T00:00:00",
            },
        )
    finally:
        service.close()
    data = _structured(result)["data"]

    assert result.get("isError") is not True, result
    assert data["status"] == "ready"
    assert data["period"] == {
        "days": 365,
        "end": "2025-12-31",
        "start": "2025-01-01",
    }
    assert 3 <= len(data["observations"]) <= 5
    assert all(
        set(row)
        == {
            "amount",
            "code",
            "dashboard_path",
            "direction",
            "evidence_ids",
            "kind",
            "rank",
            "subject",
            "subject_kind",
        }
        for row in data["observations"]
    )
    assert all(row["code"] != "other" for row in data["observations"])
    assert all(row["dashboard_path"] in _PUBLIC_DASHBOARD_PATHS for row in data["observations"])
    assert any(row["subject_kind"] != "none" for row in data["observations"])
    for row in data["observations"]:
        if row["amount"] is not None:
            assert re.fullmatch(r"-?(?:0|[1-9]\d*)\.\d{2}", row["amount"])
    assert data["coverage"]["changes"]["status"] == "reported"
    for block in data["metrics"].values():
        assert isinstance(block["transaction_count"], int)
        for key, value in block.items():
            if key != "transaction_count":
                assert re.fullmatch(r"-?(?:0|[1-9]\d*)\.\d{2}", value), (key, value)
    assert {
        "description",
        "name",
        "path",
        "statement",
        "title",
    }.isdisjoint(_keys(data))


def test_brief_observation_sanitizer_rejects_malformed_stored_scalars() -> None:
    valid: dict[str, object] = {
        "amount": Decimal("12.34"),
        "dashboard_path": "/categories",
        "direction": "higher",
        "evidence_ids": ["a" * 40],
        "label": "arithmetic",
        "rank": 1,
        "subject": "Dining",
        "subject_kind": "category",
        "title": "Largest category movement",
    }
    ready = [{**valid, "rank": rank} for rank in range(1, 4)]
    assert _brief_observations(ready, status="ready")[0]["amount"] == "12.34"

    malformed = (
        {"evidence_ids": ["not-an-evidence-id"]},
        {"evidence_ids": ["a" * 40] * 21},
        {"dashboard_path": []},
        {"label": "stored prose"},
        {"rank": 2},
        {"direction": "higher", "amount": Decimal("-12.34")},
        {"direction": "lower", "amount": Decimal("12.34")},
    )
    for changes in malformed:
        candidate = {**valid, **changes}
        with pytest.raises(AgentQueryError, match="corrupt_data"):
            _brief_observations(
                [candidate, {**valid, "rank": 2}, {**valid, "rank": 3}],
                status="ready",
            )

    missing_amount = dict(valid)
    del missing_amount["amount"]
    with pytest.raises(AgentQueryError, match="corrupt_data"):
        _brief_observations(
            [missing_amount, {**valid, "rank": 2}, {**valid, "rank": 3}],
            status="ready",
        )

    with pytest.raises(AgentQueryError, match="corrupt_data"):
        _brief_observations([valid], status="ready")
    with pytest.raises(AgentQueryError, match="corrupt_data"):
        _brief_observations([valid], status="empty")


def test_explain_reverse_resolves_only_opaque_account_refs_and_reconciles(
    tmp_path: Path,
) -> None:
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    account_reference = banking_account_ref(
        ACCOUNT_REF_SALT,
        BANK_INSTITUTION,
        PRIVATE_ACCOUNT_NUMBER,
        "chequing",
    )
    service = LedgerMCPService(db_path)
    try:
        balance = call_tool(
            service,
            "ledger.explain",
            {
                "metric": "balance",
                "start_date": "2025-06-20",
                "end_date": "2025-07-20",
                "account_ref": account_reference,
            },
        )
        projection = call_tool(
            service,
            "ledger.explain",
            {
                "metric": "projection",
                "start_date": "2025-06-20",
                "end_date": "2025-07-20",
                "account_ref": account_reference,
            },
        )
        unknown = call_tool(
            service,
            "ledger.explain",
            {
                "metric": "balance",
                "start_date": "2025-06-20",
                "end_date": "2025-07-20",
                "account_ref": "acct-0000000000",
            },
        )
    finally:
        service.close()

    balance_data = _structured(balance)["data"]
    projection_data = _structured(projection)["data"]
    unknown_data = _structured(unknown)["data"]
    assert balance_data["status"] == "reported"
    assert balance_data["reconciliation"]["status"] == "exact"
    assert balance_data["reconciliation"]["difference"] == "0.00"
    assert projection_data["status"] == "reported"
    assert projection_data["formula"] == ("statement_anchor + cash_plan_movement_since_anchor")
    assert projection_data["reconciliation"]["status"] == "exact"
    assert unknown_data["status"] == "unavailable"
    assert unknown_data["unavailable_reason"] == "account_ref_not_found"
    encoded = json.dumps([balance, projection, unknown], ensure_ascii=False)
    assert PRIVATE_ACCOUNT_NUMBER not in encoded
    assert {
        "account_id",
        "name",
        "note",
        "occurrence_id",
        "pattern_id",
        "schedule_id",
        "source_id",
    }.isdisjoint(_keys([balance_data, projection_data, unknown_data]))


def test_explain_resolves_categories_exactly_before_calculating(tmp_path: Path) -> None:
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    service = LedgerMCPService(db_path)
    arguments = {
        "end_date": "2025-12-31",
        "metric": "gross_spending",
        "start_date": "2025-01-01",
    }
    try:
        canonical = call_tool(service, "ledger.explain", {**arguments, "category": "Groceries"})
        normalized = call_tool(service, "ledger.explain", {**arguments, "category": " groceries "})
        unknown = call_tool(service, "ledger.explain", {**arguments, "category": "Restaurants"})
    finally:
        service.close()

    assert _structured(canonical)["data"] == _structured(normalized)["data"]
    assert _structured(canonical)["data"]["total"] != "0.00"
    assert _structured(unknown) == {
        "schema": "ledger.mcp.v1",
        "error": {"code": "invalid_argument", "retryable": False},
    }


def test_explain_dispatches_each_budget_display_metric(tmp_path: Path) -> None:
    db_path = build_insight_producer_database(tmp_path / "ledger.db")
    service = LedgerMCPService(db_path)
    try:
        results = {
            metric: call_tool(
                service,
                "ledger.explain",
                {
                    "end_date": "2025-12-31",
                    "metric": metric,
                    "start_date": "2025-01-01",
                },
            )
            for metric in ("budget_actual", "budget_variance")
        }
    finally:
        service.close()

    expected_formulas = {
        "budget_actual": "sum(gross_CAD_spending_in_budget_scope)",
        "budget_variance": "budgeted - actual_gross_spending",
    }
    for metric, result in results.items():
        assert result.get("isError") is not True, result
        data = _structured(result)["data"]
        assert data["metric"] == metric
        assert data["formula"] == expected_formulas[metric]
        assert data["reconciliation"]["status"] == "exact"


def test_explain_sanitizer_rejects_an_unrecognized_unavailable_reason() -> None:
    with pytest.raises(AgentQueryError, match="corrupt_data"):
        _safe_explanation(
            {
                "adjustments": None,
                "components": [],
                "evidence": {
                    "limit": 20,
                    "returned_count": 0,
                    "total_count": 0,
                    "truncated": False,
                },
                "exclusions": None,
                "formula": None,
                "metric": "balance",
                "period": {"end": "2025-01-31", "inclusive": True, "start": "2025-01-01"},
                "reconciliation": {
                    "component_total": None,
                    "difference": None,
                    "displayed_total": None,
                    "status": "unavailable",
                },
                "relevant_transactions": [],
                "source_date_bounds": {"earliest": None, "latest": None},
                "status": "unavailable",
                "total": None,
                "unavailable": {
                    "detail": "private stored prose",
                    "reason": "private_stored_reason",
                },
            }
        )


def test_cash_plan_uses_opaque_refs_and_preserves_both_nonspending_transfer_legs(
    tmp_path: Path,
) -> None:
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    service = LedgerMCPService(db_path)
    try:
        result = call_tool(
            service,
            "ledger.cash_plan",
            {
                "start_date": "2025-06-20",
                "as_of": "2025-06-20",
                "end_date": "2025-07-20",
            },
        )
    finally:
        service.close()
    data = _structured(result)["data"]

    assert result.get("isError") is not True, result
    assert data["status"] == "partial"
    assert data["coverage"]["missing_anchor_account_refs"]
    assert data["facts"]["actual_through"] == "2025-06-20"
    assert all(row["provenance"] == "imported" for row in data["fact_legs"])
    assert all(row["provenance"] != "imported" for row in data["planning_legs"])
    planning = [row for row in data["planning_legs"] if row["source_kind"] == "schedule"]
    assert {row["event_type"] for row in planning} == {
        "bill",
        "card_payment",
        "income",
        "transfer",
    }
    for event_type in ("card_payment", "transfer"):
        event_groups: dict[str, list[dict[str, object]]] = {}
        for row in planning:
            if row["event_type"] == event_type:
                event_groups.setdefault(str(row["event_ref"]), []).append(row)
        assert event_groups
        for legs in event_groups.values():
            assert len(legs) == 2
            assert {row["direction"] for row in legs} == {"source", "destination"}
            assert (
                sum(
                    (Decimal(str(row["amount"])) for row in legs),
                    Decimal("0.00"),
                )
                == 0
            )
            assert all(row["is_spending"] is False for row in legs)
            assert {row["account_ref"] for row in legs} == {
                row["counterparty_account_ref"] for row in legs
            }
    assert all(re.fullmatch(r"acct-[0-9a-f]{10}", row["account_ref"]) for row in data["accounts"])
    for account in data["accounts"]:
        assert Decimal(account["movement_since_anchor"]) == (
            Decimal(account["actual_roll_forward_movement"]) + Decimal(account["planned_movement"])
        )
        if account["opening_projected_balance"] is not None:
            assert Decimal(account["projected_balance"]) == (
                Decimal(account["opening_projected_balance"]) + Decimal(account["planned_movement"])
            )
    assert {
        "account_id",
        "description",
        "name",
        "note",
        "occurrence_id",
        "path",
        "pattern_id",
        "schedule_id",
        "source_id",
    }.isdisjoint(_keys(data))


def test_cash_plan_mcp_normalizes_same_sign_card_payment_under_one_event(
    tmp_path: Path,
) -> None:
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    bank_id = "c" * 40
    card_id = "d" * 40
    posted = date(2025, 6, 25)
    imported_at = datetime(2030, 1, 1)
    rows = (
        (bank_id, BANK_INSTITUTION, PRIVATE_ACCOUNT_NUMBER, "chequing", card_id),
        (card_id, CARD_INSTITUTION, PRIVATE_CARD_NUMBER, "credit_card", bank_id),
    )
    with session_scope(db_path) as session:
        for txn_id, institution, account, account_type, paired_id in rows:
            session.add(
                TransferDB(
                    txn_id=txn_id,
                    posting_date=posted,
                    institution=institution,
                    account=account,
                    account_type=account_type,
                    flow="payment",
                    amount_raw=Decimal("-300.00"),
                    currency="CAD",
                    description="Synthetic same-sign card payment",
                    source_file="synthetic.csv",
                    source_path="synthetic/synthetic.csv",
                    imported_at=imported_at,
                    paired_transfer_id=paired_id,
                    transfer_confidence=Decimal("1.00"),
                    counterparty_account=None,
                    counterparty_institution=None,
                )
            )

    service = LedgerMCPService(db_path)
    try:
        result = call_tool(
            service,
            "ledger.cash_plan",
            {
                "start_date": "2025-06-20",
                "as_of": "2025-06-25",
                "end_date": "2025-06-30",
            },
        )
    finally:
        service.close()
    data = _structured(result)["data"]
    legs = [row for row in data["fact_legs"] if row["event_type"] == "card_payment"]

    assert result.get("isError") is not True, result
    assert len(legs) == 2
    assert {row["amount"] for row in legs} == {"-300.00", "300.00"}
    assert {row["direction"] for row in legs} == {"source", "destination"}
    assert len({row["event_ref"] for row in legs}) == 1
    assert {row["account_ref"] for row in legs} == {row["counterparty_account_ref"] for row in legs}
    assert all(row["is_spending"] is False for row in legs)
    encoded = json.dumps(data, ensure_ascii=False)
    for private in (PRIVATE_SCHEDULE_ID, PRIVATE_SCHEDULE_NAME, PRIVATE_SCHEDULE_NOTE):
        assert private not in encoded


def test_financial_workflow_surfaces_fail_closed_on_missing_salt_and_broad_windows(
    tmp_path: Path,
) -> None:
    db_path = build_synthetic_mcp_database(
        tmp_path / "ledger-without-salt.db", account_ref_salt=False
    )
    service = LedgerMCPService(db_path)
    try:
        explanation = call_tool(
            service,
            "ledger.explain",
            {
                "metric": "balance",
                "start_date": "2025-06-20",
                "end_date": "2025-07-20",
                "account_ref": "acct-0000000000",
            },
        )
        plan = call_tool(
            service,
            "ledger.cash_plan",
            {
                "start_date": "2025-06-20",
                "as_of": "2025-06-20",
                "end_date": "2025-07-20",
            },
        )
        broad = call_tool(
            service,
            "ledger.brief",
            {"start_date": "2025-01-01", "end_date": "2026-01-02"},
        )
        invalid_as_of = call_tool(
            service,
            "ledger.cash_plan",
            {
                "start_date": "2025-06-20",
                "as_of": "2025-06-19",
                "end_date": "2025-07-20",
            },
        )
    finally:
        service.close()

    assert _structured(explanation)["data"]["unavailable_reason"] == ("account_ref_salt_absent")
    plan_data = _structured(plan)["data"]
    assert set(plan_data) == {
        "account_ref_semantics",
        "account_refs",
        "account_refs_withheld_reason",
        "status",
        "unavailable_reason",
    }
    assert plan_data["account_refs"] == "withheld"
    assert plan_data["status"] == "unavailable"
    assert plan_data["unavailable_reason"] == "account_ref_salt_absent"
    assert _structured(broad) == {
        "schema": "ledger.mcp.v1",
        "error": {"code": "filter_too_broad", "retryable": False},
    }
    assert _structured(invalid_as_of) == {
        "schema": "ledger.mcp.v1",
        "error": {"code": "invalid_argument", "retryable": False},
    }


def test_cash_plan_maps_the_imported_fact_cap_to_a_narrowable_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")

    def too_many_facts(self: CashPlanService, **_: object) -> object:
        raise CashPlanError(
            "actual_activity_too_broad",
            "Imported activity between the statement anchors and as-of is too broad.",
        )

    monkeypatch.setattr(CashPlanService, "build", too_many_facts)
    service = LedgerMCPService(db_path)
    try:
        result = call_tool(
            service,
            "ledger.cash_plan",
            {
                "start_date": "2025-06-20",
                "as_of": "2025-06-20",
                "end_date": "2025-07-20",
            },
        )
    finally:
        service.close()

    assert _structured(result) == {
        "schema": "ledger.mcp.v1",
        "error": {"code": "filter_too_broad", "retryable": False},
    }


def test_every_success_and_failure_validates_against_its_declared_output_schema(
    tmp_path: Path,
) -> None:
    """The guard that catches a value reaching a schema branch that forbids it.

    It used to validate three shapes: status, a source aggregate and a spending
    search. Every branch a tool can actually take needs to be here -- the union
    flow, the account grouping and all three `ledger.changes` bases -- because
    each of those is a DIFFERENT schema branch, and a branch nothing validates
    against is a branch nothing checks.
    """
    import jsonschema

    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    service = LedgerMCPService(db_path)
    period = {"start_date": "2025-01-01", "end_date": "2025-12-31"}
    account_reference = banking_account_ref(
        ACCOUNT_REF_SALT,
        BANK_INSTITUTION,
        PRIVATE_ACCOUNT_NUMBER,
        "chequing",
    )
    generation = str(_structured(call_tool(service, "ledger.status", {}))["coverage"]["generation"])
    cases = {
        "ledger.status": [
            call_tool(service, "ledger.status", {}),
        ],
        "ledger.brief": [
            call_tool(service, "ledger.brief", period),
            call_tool(
                service,
                "ledger.brief",
                {"start_date": "2026-01-01", "end_date": "2026-01-31"},
            ),
        ],
        "ledger.explain": [
            *[
                call_tool(service, "ledger.explain", {**period, "metric": metric})
                for metric in (
                    "budget",
                    "budget_actual",
                    "budget_variance",
                    "gross_spending",
                    "income",
                    "net",
                    "offsets",
                    "true_cost",
                )
            ],
            *[
                call_tool(
                    service,
                    "ledger.explain",
                    {
                        "account_ref": account_reference,
                        "end_date": "2025-07-20",
                        "metric": metric,
                        "start_date": "2025-06-20",
                    },
                )
                for metric in ("balance", "projection")
            ],
            call_tool(
                service,
                "ledger.explain",
                {**period, "account_ref": "acct-0000000000", "metric": "balance"},
            ),
        ],
        "ledger.cash_plan": [
            call_tool(
                service,
                "ledger.cash_plan",
                {
                    "start_date": "2025-06-20",
                    "as_of": "2025-06-20",
                    "end_date": "2025-07-20",
                },
            )
        ],
        "ledger.planning.definitions": [
            call_tool(service, "ledger.planning.definitions", {"limit": 100})
        ],
        "ledger.plan_reconciliation": [
            call_tool(
                service,
                "ledger.plan_reconciliation",
                {"start_date": "2025-07-01", "end_date": "2025-07-31", "as_of": "2025-08-10"},
            )
        ],
        "ledger.aggregate": [
            call_tool(
                service,
                "ledger.aggregate",
                _aggregate_arguments("Northstar University"),
            ),
            call_tool(service, "ledger.aggregate", {"start_date": "invalid"}),
            # The account grouping is its own schema branch: a ref-namespaced key
            # and three disclosure fields the other groupings must not carry.
            call_tool(
                service,
                "ledger.aggregate",
                {**period, "flow": "spending", "group_by": "account_ref", "limit": 20},
            ),
        ],
        "ledger.transactions.search": [
            call_tool(
                service,
                "ledger.transactions.search",
                {**period, "flow": "spending", "limit": 20},
            ),
            # The union policy block is a second `oneOf` branch, and its rows are
            # the ones whose direction lives only in `cashflow_type`.
            call_tool(
                service,
                "ledger.transactions.search",
                {**period, "flow": "any", "limit": 20},
            ),
        ],
        "ledger.changes": [
            # delta: a boundary inside the snapshot's import history.
            call_tool(
                service,
                "ledger.changes",
                {"since": "2030-01-01T00:00:00", "client_generation": generation},
            ),
            # whole_database: a boundary older than every import stamp.
            call_tool(
                service,
                "ledger.changes",
                {"since": "2020-01-01T00:00:00", "client_generation": generation},
            ),
            # A rotated generation, which is now an ordinary answer with an
            # ordinary basis: it is echoed and compared, and gates nothing.
            call_tool(
                service,
                "ledger.changes",
                {"since": "2030-01-01T00:00:00", "client_generation": "0" * 16},
            ),
            # And the same call with the optional input omitted entirely.
            call_tool(service, "ledger.changes", {"since": "2030-01-01T00:00:00"}),
        ],
        "ledger.net_worth": [
            call_tool(service, "ledger.net_worth", {"as_of": "2025-12-31", "limit": 100}),
        ],
    }
    schemas = {tool["name"]: tool["outputSchema"] for tool in tool_schemas()}

    seen: set[str] = set()
    for name, results in cases.items():
        for result in results:
            envelope = _structured(result)
            jsonschema.validate(envelope, schemas[name])
            data = envelope.get("data")
            if name == "ledger.changes" and isinstance(data, dict):
                seen.add(str(data["basis"]))
    service.close()

    # Reached, not merely requested: a case that errored would validate against
    # the error branch and prove nothing about the success branch.
    assert seen == {"delta", "whole_database"}, seen


@pytest.mark.parametrize(
    ("mode", "expected_protocol"),
    [("auto", MCP_PROTOCOL_MODERN), ("legacy", MCP_PROTOCOL_LEGACY)],
)
def test_mcp_v2_protocol_lists_exact_tools_without_log_noise(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
    mode: str,
    expected_protocol: str,
) -> None:
    import anyio
    from mcp import Client

    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")

    async def exercise_protocol() -> tuple[object, str, object, object]:
        server = create_server(LedgerMCPService(db_path))
        async with Client(server, mode=mode) as client:
            server_info = client.server_info
            protocol_version = client.protocol_version
            listing = await client.list_tools()
            aggregate = await client.call_tool(
                "ledger.aggregate",
                _aggregate_arguments("Northstar University"),
            )
        return server_info, protocol_version, listing, aggregate

    # An earlier unrelated test may leave the process root logger at DEBUG.
    # The standalone stdio-process acceptance separately proves the real CLI is
    # silent; isolate this in-memory transport exercise from ambient logging.
    caplog.set_level(logging.ERROR)
    server_info, protocol_version, listing, aggregate = anyio.run(exercise_protocol)
    captured = capsys.readouterr()

    assert captured.out == ""
    assert captured.err == ""
    assert server_info is not None
    assert server_info.name == "Ledger"
    assert server_info.version == __version__
    assert protocol_version == expected_protocol
    assert [tool.name for tool in listing.tools] == [
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
    assert [
        tool.model_dump(by_alias=True, exclude_none=True) for tool in listing.tools
    ] == tool_schemas()
    assert aggregate.structured_content["data"]["total"] == "3141.59"


@pytest.mark.parametrize(
    ("mode", "expected_protocol"),
    [("auto", MCP_PROTOCOL_MODERN), ("legacy", MCP_PROTOCOL_LEGACY)],
)
def test_actual_read_stdio_is_protocol_clean_and_shuts_down_bounded(
    tmp_path: Path,
    mode: str,
    expected_protocol: str,
) -> None:
    import anyio
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters, stdio_client

    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    before = _artifacts(db_path)
    command = Path(sys.executable).with_name("ledger-mcp")
    error_path = tmp_path / f"read-{mode}.stderr"

    async def exercise_protocol(errors: TextIO) -> tuple[object, str, object, object, object]:
        parameters = StdioServerParameters(
            command=str(command),
            args=["--db", str(db_path)],
            env={
                "LEDGER_HOME": str(tmp_path / "runtime"),
                "LEDGER_ADAPTER_CONFIG": str(tmp_path / "no-adapters.json"),
            },
        )
        with anyio.fail_after(10):
            async with Client(stdio_client(parameters, errlog=errors), mode=mode) as client:
                assert client.instructions == SERVER_INSTRUCTIONS
                listing = await client.list_tools()
                status = await client.call_tool("ledger.status", {})
                unknown = await client.call_tool(
                    "PRIVATE-UNKNOWN-TOOL",
                    {"value": "PRIVATE-UNKNOWN-ARGUMENT"},
                )
                return (
                    client.server_info,
                    client.protocol_version,
                    listing,
                    status,
                    unknown,
                )

    started = time.monotonic()
    with error_path.open("w+", encoding="utf-8") as errors:
        server_info, protocol_version, listing, status, unknown = anyio.run(
            exercise_protocol, errors
        )
        errors.seek(0)
        stderr = errors.read()
    elapsed = time.monotonic() - started

    assert elapsed < 10
    assert stderr == ""
    assert server_info is not None
    assert (server_info.name, server_info.version) == ("Ledger", __version__)
    assert protocol_version == expected_protocol
    assert [tool.model_dump(by_alias=True, exclude_none=True) for tool in listing.tools] == (
        tool_schemas()
    )
    assert status.structured_content["data"]["read_only"] is True
    assert unknown.structured_content == {
        "schema": "ledger.mcp.v1",
        "error": {"code": "invalid_argument", "retryable": False},
    }
    assert "PRIVATE-" not in json.dumps(unknown.model_dump(by_alias=True), sort_keys=True)
    assert _artifacts(db_path) == before


def test_mcp_v2_protocol_rejects_oversized_database_content(tmp_path: Path) -> None:
    import anyio
    from mcp import Client

    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    private_marker = "PRIVATE-MERCHANT-" + "x" * 220
    with contextlib.closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute(
            "update transactions set merchant = ? where merchant = ?",
            (private_marker, "NORTHSTAR UNIVERSITY"),
        )

    async def exercise_protocol() -> object:
        server = create_server(LedgerMCPService(db_path))
        async with Client(server, mode="auto") as client:
            result = await client.call_tool(
                "ledger.aggregate",
                {
                    "start_date": "2025-01-01",
                    "end_date": "2025-12-31",
                    "flow": "income",
                    "group_by": "source",
                    "limit": 50,
                },
            )
        return result

    result = anyio.run(exercise_protocol)
    encoded = json.dumps(result.model_dump(by_alias=True), ensure_ascii=False)

    assert result.is_error is True
    assert result.structured_content == {
        "schema": "ledger.mcp.v1",
        "error": {"code": "corrupt_data", "retryable": False},
    }
    assert private_marker not in encoded


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_mcp_v2_protocol_unknown_tool_failure_is_content_free(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    mode: str,
) -> None:
    import anyio
    from mcp import Client

    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    private_marker = "synthetic-private-tool-marker"

    async def exercise_protocol() -> object:
        server = create_server(LedgerMCPService(db_path))
        async with Client(server, mode=mode) as client:
            result = await client.call_tool(
                private_marker,
                {"value": "synthetic-private-argument-marker"},
            )
        return result

    result = anyio.run(exercise_protocol)
    encoded = json.dumps(result.model_dump(by_alias=True), ensure_ascii=False)

    assert result.is_error is True
    assert result.structured_content == {
        "schema": "ledger.mcp.v1",
        "error": {"code": "invalid_argument", "retryable": False},
    }
    assert "synthetic-private" not in encoded
    assert "synthetic-private" not in caplog.text


def test_mcp_v2_parallel_reads_are_identical_and_leave_database_unchanged(
    tmp_path: Path,
) -> None:
    import asyncio

    import anyio
    from mcp import Client

    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    before = _artifacts(db_path)
    service = LedgerMCPService(db_path)

    async def exercise_protocol() -> list[object]:
        async with Client(create_server(service), mode="auto") as client:
            return await asyncio.gather(
                *(
                    client.call_tool(
                        "ledger.aggregate",
                        _aggregate_arguments("Northstar University"),
                    )
                    for _ in range(12)
                )
            )

    try:
        results = anyio.run(exercise_protocol)
    finally:
        service.close()

    assert [result.structured_content["data"]["total"] for result in results] == ["3141.59"] * 12
    assert _artifacts(db_path) == before


def test_concurrent_rotation_cannot_close_or_mix_an_active_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import contextlib
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from typing import Any

    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    replacement = build_synthetic_mcp_database(tmp_path / "replacement.db")
    with contextlib.closing(sqlite3.connect(replacement)) as connection, connection:
        connection.execute(
            "UPDATE transactions SET amount_raw = amount_raw + 100, "
            "amount_signed = amount_signed + 100 "
            "WHERE merchant = 'NORTHSTAR UNIVERSITY' AND currency = 'CAD' "
            "AND posting_date BETWEEN '2025-01-01' AND '2025-12-31'"
        )

    service = LedgerMCPService(db_path)
    old_database = service.database
    old_generation = service.snapshot.generation
    aggregate_complete = threading.Event()
    release_aggregate = threading.Event()
    status_started = threading.Event()
    repin_started = threading.Event()
    original_aggregate = service.query.agent.aggregate_transaction_facts
    original_repin = service._repin

    def delayed_aggregate(*args: Any, **kwargs: Any) -> object:
        result = original_aggregate(*args, **kwargs)
        aggregate_complete.set()
        assert release_aggregate.wait(timeout=10)
        return result

    def observed_repin() -> None:
        repin_started.set()
        original_repin()

    def read_status() -> dict[str, object]:
        status_started.set()
        return call_tool(service, "ledger.status", {})

    monkeypatch.setattr(
        service.query.agent,
        "aggregate_transaction_facts",
        delayed_aggregate,
    )
    monkeypatch.setattr(service, "_repin", observed_repin)

    with ThreadPoolExecutor(max_workers=2) as executor:
        aggregate_future = executor.submit(
            call_tool,
            service,
            "ledger.aggregate",
            _aggregate_arguments("Northstar University"),
        )
        assert aggregate_complete.wait(timeout=10)
        os.replace(replacement, db_path)
        status_future = executor.submit(read_status)
        assert status_started.wait(timeout=10)
        try:
            assert not repin_started.wait(timeout=1)
            assert service.database is old_database
        finally:
            release_aggregate.set()
        aggregate_result = aggregate_future.result(timeout=10)
        status_result = status_future.result(timeout=10)

    aggregate = _structured(aggregate_result)
    status = _structured(status_result)
    assert aggregate["data"]["total"] == "3141.59"  # type: ignore[index]
    assert aggregate["coverage"]["generation"] == old_generation  # type: ignore[index]
    assert status["coverage"]["generation"] != old_generation  # type: ignore[index]
    rotated = _structured(
        call_tool(
            service,
            "ledger.aggregate",
            _aggregate_arguments("Northstar University"),
        )
    )
    assert rotated["data"]["total"] == "3341.59"  # type: ignore[index]
    service.close()


def test_concurrent_rotation_waits_for_an_active_cursor_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from typing import Any

    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    service = LedgerMCPService(db_path)
    arguments = {
        "start_date": "2025-01-01",
        "end_date": "2025-12-31",
        "flow": "income",
        "filters": {"sources": ["northstar university"]},
        "limit": 1,
    }
    first = _structured(call_tool(service, "ledger.transactions.search", arguments))
    cursor = str(first["page"]["next_cursor"])  # type: ignore[index]
    old_generation = service.snapshot.generation
    page_complete = threading.Event()
    release_page = threading.Event()
    status_started = threading.Event()
    repin_started = threading.Event()
    original_search = service.query.agent.search_transaction_evidence
    original_repin = service._repin

    def delayed_search(*args: Any, **kwargs: Any) -> object:
        result = original_search(*args, **kwargs)
        page_complete.set()
        assert release_page.wait(timeout=10)
        return result

    def observed_repin() -> None:
        repin_started.set()
        original_repin()

    def read_status() -> dict[str, object]:
        status_started.set()
        return call_tool(service, "ledger.status", {})

    monkeypatch.setattr(service.query.agent, "search_transaction_evidence", delayed_search)
    monkeypatch.setattr(service, "_repin", observed_repin)

    with ThreadPoolExecutor(max_workers=2) as executor:
        page_future = executor.submit(
            call_tool,
            service,
            "ledger.transactions.search",
            {**arguments, "cursor": cursor},
        )
        assert page_complete.wait(timeout=10)
        replacement = build_synthetic_mcp_database(tmp_path / "replacement.db")
        os.replace(replacement, db_path)
        status_future = executor.submit(read_status)
        assert status_started.wait(timeout=10)
        try:
            assert not repin_started.wait(timeout=1)
        finally:
            release_page.set()
        page_result = page_future.result(timeout=10)
        status_result = status_future.result(timeout=10)

    page = _structured(page_result)
    status = _structured(status_result)
    assert page["coverage"]["generation"] == old_generation  # type: ignore[index]
    assert status["coverage"]["generation"] != old_generation  # type: ignore[index]
    stale = call_tool(
        service,
        "ledger.transactions.search",
        {**arguments, "cursor": cursor},
    )
    assert stale["isError"] is True
    assert _structured(stale)["error"]["code"] == "invalid_cursor"  # type: ignore[index]
    service.close()


def test_mcp_v2_owns_stdio_transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import asynccontextmanager

    from mcp.server import Server

    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    transports: list[str] = []

    @asynccontextmanager
    async def fake_stdio():
        transports.append("stdio")
        yield object(), object()

    async def fake_run(_server: object, *_args: object, **_kwargs: object) -> None:
        transports.append("run")

    monkeypatch.setattr("ledger.mcp.server.stdio_server", fake_stdio)
    monkeypatch.setattr(Server, "run", fake_run)

    run_stdio(db_path)

    assert transports == ["stdio", "run"]


def test_cli_selects_only_explicit_database_without_printing_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = build_synthetic_mcp_database(tmp_path / "private-ledger.db")
    selected: list[Path] = []
    monkeypatch.setattr("ledger.mcp.server.run_stdio", lambda path: selected.append(path))

    assert main(["--db", str(db_path)]) == 0

    captured = capsys.readouterr()
    assert selected == [db_path]
    assert captured.out == ""
    assert captured.err == ""


def test_cli_startup_failure_is_content_free(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    missing = tmp_path / "private-missing.db"

    assert main(["--db", str(missing)]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "ledger_mcp_startup_failed:database_unavailable\n"
    assert str(missing) not in captured.err


@pytest.mark.parametrize(
    "arguments",
    [
        ["--db", "/private/owner/ledger.db", "--unexpected", "/private/other.db"],
        ["--db", "/private/owner/ledger.db", "/private/positional.db"],
    ],
)
def test_cli_invalid_arguments_fail_without_echoing_private_values(
    arguments: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(arguments) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "ledger_mcp_startup_failed:invalid_argument\n"
    assert "/private/" not in captured.err


def _tools_named_beside_an_invalid_cursor_assertion(sources: dict[str, str]) -> set[str]:
    """Every tool name appearing in a test function that asserts `invalid_cursor`."""
    named: set[str] = set()
    for text in sources.values():
        for block in text.split("\ndef test_"):
            if "invalid_cursor" in block:
                named.update(re.findall(r'"(ledger\.[a-z_.]+)"', block))
    return named


def test_every_paginated_tool_has_a_cursor_refusal_test_somewhere() -> None:
    """A cursor minted before a write and replayed after it must be refused.

    That is the 1.3.0 regression the generation binding exists to prevent, and
    the binding is one keyword in one helper call per tool: nothing catches
    dropping it except a test that replays a stale cursor. `ledger.quality`
    shipped without one while its sibling `ledger.insights` had one -- the same
    shape as a fix landing on three surfaces of four. Derived from the published
    schemas, so a paginated tool added later cannot ship uncovered, and so this
    audit cannot go stale against a hand-written list.
    """
    paginated = set()
    for tool in tool_schemas():
        success = next(
            branch
            for branch in tool["outputSchema"]["oneOf"]
            if "data" in branch.get("properties", {})
        )
        if "page" in success["properties"]:
            paginated.add(str(tool["name"]))
    sources = {
        str(path): path.read_text(encoding="utf-8")
        for path in Path(__file__).resolve().parent.glob("test_*.py")
    }
    covered = _tools_named_beside_an_invalid_cursor_assertion(sources)

    assert len(paginated) >= 11, sorted(paginated)
    assert not paginated - covered, sorted(paginated - covered)
