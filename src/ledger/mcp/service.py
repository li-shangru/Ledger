"""Typed application service behind the Ledger MCP wire adapter."""

from __future__ import annotations

import hashlib
import hmac
import re
import sqlite3
import threading
from collections.abc import Callable, Mapping
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import Path
from typing import TypeVar, cast

from sqlalchemy.exc import SQLAlchemyError

from ledger import __version__
from ledger.adapters.contracts import AdapterError

# The numbers that decide a gap verdict and the one that bounds a projection
# window, emitted on the wire as schema consts. Imported from the modules that
# APPLY them so the published value can never drift from the applied one -- a
# caller reproducing a classification, or choosing a target date under the
# published bound, from a copy of the number would otherwise be reading a
# different rule than the one enforced.
from ledger.analysis.projection import DEFAULT_MAX_LINE_ITEMS, PROJECTION_HORIZON_DAYS
from ledger.analysis.statement_cadence import (
    DELIVERY_GRACE_DAYS,
    DORMANCY_MULTIPLIER,
    MIN_CADENCE_OBSERVATIONS,
)
from ledger.investments.contracts import AssetCategoryVocabulary, asset_category_vocabulary
from ledger.mcp import planning as planning_wire
from ledger.mcp.contracts import (
    ACCOUNT_REF_SEMANTICS,
    BALANCE_LEVEL_STATEMENT,
    BUDGET_ACTUAL_BASIS_STATEMENT,
    BUDGET_SELECTION_STATEMENT,
    CHANGES_BASIS_STATEMENT,
    CHANGES_COUNT_STATEMENT,
    CHANGES_INVESTMENT_STATEMENT,
    CONTAINS_SEMANTICS,
    COVERAGE_ABSENCE_STATEMENT,
    COVERAGE_GAP_STATEMENT,
    COVERAGE_ROW_COUNT_STATEMENT,
    COVERAGE_STATEMENT,
    CREDIT_UTILIZATION_STATEMENT,
    ENVELOPE_SCHEMA,
    EXPLANATION_UNAVAILABLE_REASONS,
    FLOW_ANY_UNION_STATEMENT,
    INSIGHT_ACCOUNT_IDENTITY_STATEMENT,
    INSIGHT_AMOUNT_STATEMENT,
    INSIGHT_PERIOD_STATEMENT,
    INSIGHT_TRIAGE_STATEMENT,
    INSIGHT_UNSERVABLE_STATEMENT,
    INSIGHT_WITHHELD_FIELD_STATEMENT,
    INSTRUCTION_TRUST,
    MAX_BUDGET_ENTRIES,
    NET_WORTH_CLOSURE_STATEMENT,
    NET_WORTH_COMPONENT_COUNTS_STATEMENT,
    NET_WORTH_CURRENCY_STATEMENT,
    NET_WORTH_QUANTIZATION_STATEMENT,
    PROJECTION_AMOUNT_STATEMENT,
    PROJECTION_ATTRIBUTION_STATEMENT,
    PROJECTION_MISSED_COUNT_STATEMENT,
    PROJECTION_RECONCILIATION_STATEMENT,
    PROJECTION_ROLL_FORWARD_STATEMENT,
    PROJECTION_SCOPE_STATEMENT,
    QUALITY_DETAILS_STATEMENT,
    QUALITY_SCOPE_STATEMENT,
    QUALITY_UNSERVABLE_STATEMENT,
    QUALITY_VOCABULARY_STATEMENT,
    RECURRING_CLASSIFICATION_CAVEAT,
    RECURRING_STORED_FIELD_STATEMENT,
    TRANSFER_DIRECTION_STATEMENT,
    TRANSFER_TOTAL_STATEMENT,
    contract_digest,
)
from ledger.mcp.investments import build_investment_result
from ledger.planning import BudgetService
from ledger.planning import discovery as planning_discovery
from ledger.planning.cash_plan import CashPlan, CashPlanError, CashPlanLeg, CashPlanService
from ledger.planning.discovery import PlanningDiscoveryLimitError, discover_planning_definitions
from ledger.query.account_refs import account_id_ref_map, reverse_account_ref_map
from ledger.query.agent import (
    ACCOUNT_REFS_WITHHELD,
    INSIGHT_TYPE_FIELDS,
    AgentFilters,
    AgentQueryError,
    AggregateGroup,
    AggregateResult,
    BalanceEntry,
    ChangeCountSource,
    ChangeCountSources,
    ChangeEntry,
    CoverageEntry,
    CoverageGap,
    DimensionValue,
    EvidencePage,
    EvidenceRow,
    ExcludedCounts,
    InsightFinding,
    NetWorthComponent,
    ProjectionEntry,
    ProjectionLineItem,
    QualityCheck,
    RecurringEntry,
    TransferGroup,
    _cursor_fingerprint,
    _decode_cursor,
    _encode_cursor,
    _normalize_match,
)
from ledger.query.service import LedgerQueryService
from ledger.reporting.brief import FinancialBriefService
from ledger.reporting.explanations import MetricExplanationService
from ledger.storage.database import BudgetDB, ConfigMetadataDB, TransactionDB
from ledger.storage.operations import ACCOUNT_REF_SALT_KEY
from ledger.storage.read_only import (
    DatabaseChangedError,
    DatabaseSnapshot,
    DatabaseUnavailableError,
    IncompatibleSchemaError,
    ReadOnlyDatabase,
)
from ledger.utils.dates import naive_utc_now

_ServedT = TypeVar("_ServedT")


def _money(value: Decimal) -> str:
    return format(value, ".2f")


def _signed_money(value: Decimal) -> str:
    """Format a balance that may legitimately be negative.

    Decimal keeps a negated zero, and ``format(Decimal("-0.00"), ".2f")`` is
    ``"-0.00"`` -- a sign on a quantity that has no direction. The signed money
    schema refuses that string, so normalizing here is what keeps a zero movement
    from failing as corrupt data.
    """
    quantized = value.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    if quantized == 0:
        return "0.00"
    return format(quantized, ".2f")


def _date_or_none(value: date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _timestamp_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _coverage(
    snapshot: DatabaseSnapshot,
    *,
    start_date: date | None,
    end_date: date | None,
    matched_rows: int | None = None,
) -> dict[str, object]:
    """State how the requested period sits against the imported range.

    `matched_rows` separates the two zeros that used to look identical: a period
    the ledger covers where nothing matched the query, versus a period the ledger
    covers where the caller simply has nothing of that kind. Without it, an
    over-narrow filter reads as an authoritative absence.
    """
    available_start = snapshot.earliest_date
    available_end = snapshot.latest_date
    if start_date is None or end_date is None:
        relation = "not_queried"
    elif available_start is None or available_end is None:
        relation = "empty_ledger"
    elif end_date < available_start or start_date > available_end:
        relation = "outside_available_range"
    elif start_date >= available_start and end_date <= available_end:
        relation = (
            "within_available_range_no_matching_rows"
            if matched_rows == 0
            else "within_available_range"
        )
    else:
        relation = "partially_outside_available_range"
    return {
        "available_end": _date_or_none(available_end),
        "available_start": _date_or_none(available_start),
        "completeness": "not_asserted",
        "generation": snapshot.generation,
        "relation": relation,
        "requested_end": _date_or_none(end_date),
        "requested_start": _date_or_none(start_date),
        "scope": "imported_transaction_rows",
        "statement": COVERAGE_STATEMENT,
    }


def _policy(flow: str) -> dict[str, object]:
    """The rules that produced this answer.

    `flow="any"` is a union of two predicate sets that report different amount
    bases and apply different exclusions, so reporting either branch's block
    would describe half the rows. The union block therefore takes an
    `amount_basis` neither branch uses -- a client handling only the two old
    values fails on it rather than silently reading a union under one branch's
    rules -- and carries both branch policies verbatim under `flows`.

    The union block asserts at top level ONLY what covers both branches. The rules
    it may state are the ones `income_sql_filters()` and `spend_sql_filters()`
    both apply: `include_in_cashflow`, CAD-native rows, inclusive date bounds.
    `exclude_from_spending` and `income_treatment_exclusions` are NOT among them
    -- the income predicate never reads the spending flag and the spending
    predicate never nets a treatment -- so they appear only inside the branch they
    belong to. Emitting them at top level would claim a filter over rows it was
    never applied to: an income row can carry `exclude_from_spending` and still
    belong in the union result.
    """
    if flow == "any":
        return {
            "amount_basis": "absolute_magnitude",
            "currency": "CAD_native_only",
            "date_bounds": "inclusive",
            "flow": "any",
            "flows": [_policy("income"), _policy("spending")],
            "requires_include_in_cashflow": True,
            "union_semantics": FLOW_ANY_UNION_STATEMENT,
        }
    is_income = flow == "income"
    return {
        "amount_basis": "positive_income" if is_income else "absolute_spending",
        "currency": "CAD_native_only",
        "date_bounds": "inclusive",
        "exclude_from_spending": not is_income,
        "flow": flow,
        "income_treatment_exclusions": (
            ["exclude_from_income", "reduce_category_cost"] if is_income else []
        ),
        "requires_include_in_cashflow": True,
    }


def _excluded(counts: ExcludedCounts, flow: str) -> dict[str, object]:
    """Counts only. Reasons overlap; `any_reason` is the reconciliation figure."""
    block: dict[str, object] = {
        "any_reason": counts.any_reason,
        "blank_merchant": counts.blank_merchant,
        "candidate_rows": counts.candidate_rows,
        "non_cad": counts.non_cad,
        "not_in_cashflow": counts.not_in_cashflow,
    }
    if flow == "spending":
        block["exclude_from_spending"] = counts.exclude_from_spending
    else:
        block["income_treatment"] = counts.income_treatment
    return block


def _aggregate_group(group: AggregateGroup) -> dict[str, object]:
    """One grouped total. `account_ref_status` appears only when the key is a ref."""
    block: dict[str, object] = {
        "amount": _money(group.amount),
        "key": group.key,
        "transaction_count": group.transaction_count,
    }
    if group.account_ref_status is not None:
        block["account_ref_status"] = group.account_ref_status
    return block


def _matching(result: AggregateResult | EvidencePage) -> dict[str, object]:
    return {
        "matched_categories": list(result.matched_categories),
        "matched_institutions": list(result.matched_institutions),
        "matched_sources": list(result.matched_sources),
        "semantics": result.match_semantics,
        "unmatched_categories": list(result.unmatched_categories),
        "unmatched_institutions": list(result.unmatched_institutions),
        "unmatched_sources": list(result.unmatched_sources),
    }


def _coverage_entry(entry: CoverageEntry) -> dict[str, object]:
    return {
        "account_ref": entry.account_ref,
        "account_ref_status": entry.account_ref_status,
        "imported_at": entry.imported_at.isoformat(),
        "institution": entry.institution,
        "period": entry.period,
        "row_count": entry.row_count,
        "statement_count": entry.statement_count,
        "statement_type": entry.statement_type,
        "status": entry.status,
    }


def _coverage_gap(gap: CoverageGap) -> dict[str, object]:
    """One account's gap verdict.

    `cadence_confidence` is a two-decimal STRING like every other fraction this
    server emits, and null exactly when the cadence is: a share of no intervals
    is not 0.00, which would read as "never on cadence" rather than "unknown".
    """
    return {
        "account_ref": gap.account_ref,
        "account_ref_status": gap.account_ref_status,
        "cadence_confidence": (
            None if gap.cadence_confidence is None else f"{gap.cadence_confidence:.2f}"
        ),
        "cadence_months": gap.cadence_months,
        "cadence_observations": gap.cadence_observations,
        "classification": gap.classification,
        "deciding_signal": gap.deciding_signal,
        "elapsed_months": gap.elapsed_months,
        "last_covered_period": gap.last_covered_period,
        "missing_period_count": gap.missing_period_count,
        "missing_periods": list(gap.missing_periods),
    }


def _change_entry(entry: ChangeEntry) -> dict[str, object]:
    return {
        "account_ref": entry.account_ref,
        "account_ref_status": entry.account_ref_status,
        "institution": entry.institution,
        "period": entry.period,
        "rows_added": entry.rows_added,
        "statement_count": entry.statement_count,
        "statement_row_count": entry.statement_row_count,
        "statement_status": entry.statement_status,
    }


def _count_source(source: ChangeCountSource) -> dict[str, object]:
    return {
        "basis": source.basis,
        "earliest_imported_at": _timestamp_or_none(source.earliest_imported_at),
        "latest_imported_at": _timestamp_or_none(source.latest_imported_at),
        "since_relation": source.since_relation,
    }


def _count_sources(sources: ChangeCountSources) -> dict[str, object]:
    """One verdict per COUNTED TABLE. Always present, because a count always is.

    Named for the counts they govern rather than for the tables: `rows` answers
    for `total_added` and every entry's `rows_added`, `statements` for the two
    statement counts. The two verdicts really can differ -- a statement that
    produced no rows is older than every transaction row when it was imported
    first -- and collapsing them let the whole transactions table be served as a
    delta.
    """
    return {
        "rows": _count_source(sources.rows),
        "statements": _count_source(sources.statements),
    }


def _recurring_entry(entry: RecurringEntry) -> dict[str, object]:
    return {
        "category": entry.category,
        "frequency": entry.frequency,
        "institution": entry.institution,
        "is_active": entry.is_active,
        "merchant": entry.merchant,
        "missed_count": entry.missed_count,
        "next_expected_date": entry.next_expected_date.isoformat(),
        "pattern_type": entry.pattern_type,
        "typical_amount": _money(entry.typical_amount),
        "updated_at": _timestamp_or_none(entry.updated_at),
    }


def _transfer_group(group: TransferGroup) -> dict[str, object]:
    return {
        "amount": _money(group.amount),
        "counterparty_institution": group.counterparty_institution,
        "direction": group.direction,
        "institution": group.institution,
        "transfer_count": group.transfer_count,
    }


def _balance_entry(entry: BalanceEntry) -> dict[str, object]:
    level = entry.level
    change = entry.change
    return {
        "account_ref": entry.account_ref,
        "account_type": entry.account_type,
        "change": (
            None
            if change is None
            else {
                "amount": None if change.amount is None else _signed_money(change.amount),
                # Named on every emitted block, so a caller never has to infer
                # which series produced a number.
                "balance_basis": change.balance_basis,
                "from_date": change.from_date.isoformat(),
                "observed_through": (
                    None if change.observed_through is None else change.observed_through.isoformat()
                ),
                "status": change.status,
                "to_date": change.to_date.isoformat(),
            }
        ),
        "institution": entry.institution,
        "is_credit_card": entry.is_credit_card,
        "level": {
            "as_of_lag_days": level.as_of_lag_days,
            "balance": None if level.balance is None else _signed_money(level.balance),
            "balance_basis": level.balance_basis,
            "balance_date": _date_or_none(level.balance_date),
            "credit_utilization_pct": (
                None
                if level.credit_utilization_pct is None
                else _money(level.credit_utilization_pct)
            ),
            "credit_utilization_status": level.credit_utilization_status,
            "status": level.status,
        },
    }


def _net_worth_component(component: NetWorthComponent) -> dict[str, object]:
    """Map one cross-domain component without retaining any raw identity."""
    return {
        "account_ref": component.account_ref,
        "account_type": component.account_type,
        "amount": _signed_money(component.amount),
        "as_of": component.as_of.isoformat(),
        "as_of_lag_days": component.as_of_lag_days,
        "basis": component.basis,
        "closed_on": _date_or_none(component.closed_on),
        "component_type": component.component_type,
        "currency": component.currency,
        "final_statement_outstanding": component.final_statement_outstanding,
        "institution": component.institution,
        "status": component.status,
    }


def _projection_line_item(line: ProjectionLineItem) -> dict[str, object]:
    """One expected flow.

    `amount` is the SIGNED value; `_signed_money` normalizes a negated zero,
    which the signed schema refuses because a zero movement has no direction.
    The pattern's own identifier is deliberately absent from `ProjectionLineItem`
    itself, so it cannot be emitted by an edit here.
    """
    return {
        "amount": _signed_money(line.amount),
        "category": line.category,
        "confidence": f"{line.confidence:.2f}",
        "frequency": line.frequency,
        "is_overdue": line.is_overdue,
        "merchant": line.merchant,
        "missed_count": line.missed_count,
        "next_expected_date": line.next_expected_date.isoformat(),
        "next_expected_is_retrospective": line.next_expected_is_retrospective,
        "pattern_type": line.pattern_type,
        "periods_from_next_expected": line.periods_from_next_expected,
        "projected_date": line.projected_date.isoformat(),
    }


def _projection_entry(entry: ProjectionEntry) -> dict[str, object]:
    """One account's projection, or the refusal that replaced it.

    `pattern_disposition` is null exactly when no projection was built -- the
    anchor was refused, or the window implied more flows than one answer serves.
    With no window there is nothing to classify occurrences against, and a block
    of zeros would say the account's patterns were considered and produced
    nothing. `pattern_count` is reported either way, and `status` names which of
    the four outcomes this entry is, so a caller never infers it from a null.
    """
    anchor = entry.anchor
    disposition = entry.disposition
    return {
        "account_ref": entry.account_ref,
        "account_type": entry.account_type,
        "anchor": {
            "as_of_lag_days": anchor.as_of_lag_days,
            "balance": None if anchor.balance is None else _signed_money(anchor.balance),
            "balance_basis": anchor.balance_basis,
            "balance_date": _date_or_none(anchor.balance_date),
            "status": anchor.status,
        },
        "institution": entry.institution,
        "is_credit_card": entry.is_credit_card,
        "line_item_count": len(entry.line_items),
        "line_items": [_projection_line_item(line) for line in entry.line_items],
        "net_projected_flow": (
            None if entry.net_projected_flow is None else _signed_money(entry.net_projected_flow)
        ),
        "pattern_count": entry.pattern_count,
        "pattern_disposition": (
            None
            if disposition is None
            else {
                "inactive": disposition.inactive,
                "no_occurrence_in_window": disposition.no_occurrence_in_window,
                "projected": disposition.projected,
                "transfer_direction_ambiguous": disposition.transfer_direction_ambiguous,
                "unclassified_pattern_type": disposition.unclassified_pattern_type,
                "unsupported_frequency": disposition.unsupported_frequency,
            }
        ),
        "patterns_past_due": entry.patterns_past_due,
        "projected_balance": (
            None if entry.projected_balance is None else _signed_money(entry.projected_balance)
        ),
        "status": entry.status,
    }


def _insight_finding(entry: InsightFinding) -> dict[str, object]:
    """One triage row: five common fields plus the scalars its TYPE guarantees.

    The per-type field list is read from `INSIGHT_TYPE_FIELDS`, the same mapping
    the published branch schemas are built from, so a scalar cannot be added to
    one and forgotten in the other. Nothing else about the stored row reaches
    here: `InsightFinding.scalars` holds only what the query layer decided the
    type guarantees, already in wire form.
    """
    served: dict[str, object] = {
        "action_priority": entry.action_priority,
        "finding_type": entry.finding_type,
        "period_end": entry.period_end.isoformat(),
        "period_start": entry.period_start.isoformat(),
        "severity": entry.severity,
        "status": entry.status,
        "preference": (
            None
            if entry.preference_scope is None
            else {
                "disposition": entry.preference_disposition,
                "effect": entry.preference_effect,
                "scope": entry.preference_scope,
            }
        ),
    }
    for field in INSIGHT_TYPE_FIELDS[entry.finding_type]:
        served[field] = entry.scalars[field]
    return served


def _quality_check(check: QualityCheck) -> dict[str, object]:
    return {
        "check_name": check.check_name,
        "issue_count": check.issue_count,
        "severity": check.severity,
        "status": check.status,
    }


def _dimension_value(value: DimensionValue) -> dict[str, object]:
    return {"transaction_count": value.transaction_count, "value": value.value}


def _item(row: EvidenceRow) -> dict[str, object]:
    return {
        "amount": _money(row.amount),
        "cashflow_type": row.cashflow_type,
        "category": row.category,
        "currency": row.currency,
        "evidence_id": row.evidence_id,
        "flow": row.flow,
        "institution": row.institution,
        "posting_date": row.posting_date.isoformat(),
        "source": row.source,
    }


def _wire_cent(value: object) -> str:
    """Turn a trusted calculation value into one exact signed cent string."""
    try:
        amount = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise AgentQueryError("corrupt_data") from error
    if not amount.is_finite():
        raise AgentQueryError("corrupt_data")
    return _signed_money(amount)


def _wire_optional_cent(value: object) -> str | None:
    return None if value is None else _wire_cent(value)


def _wire_confidence(value: object) -> str | None:
    if value is None:
        return None
    try:
        confidence = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)
    except (InvalidOperation, TypeError, ValueError) as error:
        raise AgentQueryError("corrupt_data") from error
    if not confidence.is_finite() or confidence < 0 or confidence > 1:
        raise AgentQueryError("corrupt_data")
    return format(confidence, ".2f")


def _validate_financial_period(start_date: date, end_date: date) -> None:
    if end_date < start_date:
        raise AgentQueryError("invalid_argument")
    if (end_date - start_date).days >= 366:
        raise AgentQueryError("filter_too_broad")


_BRIEF_OBSERVATION_CODES = {
    "Statement coverage gap": "statement_coverage_gap",
    "Transaction freshness boundary": "transaction_freshness",
    "Detected possibility for review": "detected_possibility",
    "Largest category movement": "largest_category_movement",
    "Comparison coverage": "comparison_coverage",
    "Selected-period cash flow": "cash_flow",
    "Imported changes boundary": "import_changes",
    "Imported activity in period": "activity",
    "Balance coverage": "balance_coverage",
    "Balance anchor freshness": "balance_freshness",
    "Imported-record boundary": "imported_record_boundary",
    "No imported activity in period": "no_activity",
    "Plan versus actual exception": "reconciliation_exception",
    "Plan comparison period boundary": "reconciliation_period_boundary",
}
_BRIEF_DASHBOARD_PATHS = frozenset(
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
_BRIEF_SUBJECT_KINDS = frozenset({"category", "institution", "merchant", "none"})
_BRIEF_DIRECTIONS = frozenset({"higher", "lower", "not_applicable"})
_BRIEF_KINDS = frozenset({"arithmetic", "coverage", "possibility"})
_EVIDENCE_ID_PATTERN = re.compile(r"^[0-9a-f]{40}$")


def _brief_metrics(raw: object) -> dict[str, object] | None:
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise AgentQueryError("corrupt_data")

    def block(value: object, *, change: bool = False) -> dict[str, object]:
        if not isinstance(value, Mapping):
            raise AgentQueryError("corrupt_data")
        return {
            "gross_spend": _wire_cent(value.get("gross_spend")),
            "income": _wire_cent(value.get("income")),
            "net": _wire_cent(value.get("net")),
            "offsets": _wire_cent(value.get("offsets")),
            "transaction_count": int(value.get("transaction_count", 0)),
            "true_cost": _wire_cent(value.get("true_cost")),
        }

    return {
        "change": block(raw.get("change"), change=True),
        "comparison": block(raw.get("comparison")),
        "current": block(raw.get("current")),
    }


def _brief_observations(raw: object, *, status: str) -> list[dict[str, object]]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= 5:
        raise AgentQueryError("corrupt_data")
    if status == "ready" and len(raw) < 3:
        raise AgentQueryError("corrupt_data")
    served: list[dict[str, object]] = []
    for expected_rank, item in enumerate(raw, 1):
        if not isinstance(item, Mapping):
            raise AgentQueryError("corrupt_data")
        required = {
            "amount",
            "dashboard_path",
            "direction",
            "evidence_ids",
            "label",
            "rank",
            "subject",
            "subject_kind",
            "title",
        }
        if not required <= item.keys():
            raise AgentQueryError("corrupt_data")
        evidence = item.get("evidence_ids", [])
        if (
            not isinstance(evidence, list)
            or len(evidence) > 20
            or any(
                not isinstance(value, str) or not _EVIDENCE_ID_PATTERN.fullmatch(value)
                for value in evidence
            )
        ):
            raise AgentQueryError("corrupt_data")
        dashboard_path = item.get("dashboard_path")
        subject_kind = item.get("subject_kind")
        reconciliation = item.get("title") == "Plan versus actual exception"
        if reconciliation and subject_kind == "occurrence":
            # Local links may carry an internal occurrence id. The read
            # reconciliation tool supplies its salted reference separately.
            subject_kind = "none"
        direction = item.get("direction")
        kind = item.get("label")
        rank = item.get("rank")
        if (
            not isinstance(dashboard_path, str)
            or not isinstance(subject_kind, str)
            or not isinstance(direction, str)
            or not isinstance(kind, str)
            or dashboard_path not in _BRIEF_DASHBOARD_PATHS
            or subject_kind not in _BRIEF_SUBJECT_KINDS
            or direction not in _BRIEF_DIRECTIONS
            or kind not in _BRIEF_KINDS
            or isinstance(rank, bool)
            or not isinstance(rank, int)
            or rank != expected_rank
        ):
            raise AgentQueryError("corrupt_data")
        raw_subject = None if reconciliation else item.get("subject")
        if subject_kind == "none":
            if raw_subject is not None:
                raise AgentQueryError("corrupt_data")
            subject = None
        elif not isinstance(raw_subject, str) or not raw_subject.strip() or len(raw_subject) > 200:
            raise AgentQueryError("corrupt_data")
        else:
            subject = raw_subject
        amount = _wire_optional_cent(item.get("amount"))
        if (direction == "higher" and (amount is None or Decimal(amount) <= 0)) or (
            direction == "lower" and (amount is None or Decimal(amount) >= 0)
        ):
            raise AgentQueryError("corrupt_data")
        served.append(
            {
                "amount": amount,
                "code": _BRIEF_OBSERVATION_CODES.get(str(item.get("title")), "other"),
                "dashboard_path": dashboard_path,
                "direction": direction,
                "evidence_ids": list(evidence),
                "kind": kind,
                "rank": rank,
                "subject": subject,
                "subject_kind": subject_kind,
            }
        )
    if status == "empty" and any(
        item["kind"] != "coverage"
        and not (item["kind"] == "possibility" and item["code"] == "reconciliation_exception")
        for item in served
    ):
        raise AgentQueryError("corrupt_data")
    return served


def _safe_brief(raw: Mapping[str, object]) -> dict[str, object]:
    """Project the UI brief onto bounded scalars; generated/stored prose never crosses."""
    status = raw.get("status")
    if not isinstance(status, str) or status not in {"empty", "ready"}:
        raise AgentQueryError("corrupt_data")
    period = raw.get("period")
    comparison = raw.get("comparison")
    coverage = raw.get("coverage")
    if not isinstance(coverage, Mapping):
        raise AgentQueryError("corrupt_data")

    def period_block(value: object, *, comparison_block: bool) -> dict[str, object] | None:
        if value is None:
            return None
        if not isinstance(value, Mapping):
            raise AgentQueryError("corrupt_data")
        result: dict[str, object] = {
            "days": int(value.get("days", 0)),
            "end": str(value.get("end")),
            "start": str(value.get("start")),
        }
        if comparison_block:
            result["comparable"] = bool(value.get("comparable"))
            result["coverage_reason"] = str(value.get("coverage_reason"))
        return result

    freshness = coverage.get("freshness")
    statements = coverage.get("statements")
    balances = coverage.get("balances")
    insights = coverage.get("insights")
    changes = coverage.get("changes")
    if not all(
        isinstance(item, Mapping) for item in (freshness, statements, balances, insights, changes)
    ):
        raise AgentQueryError("corrupt_data")
    assert isinstance(freshness, Mapping)
    assert isinstance(statements, Mapping)
    assert isinstance(balances, Mapping)
    assert isinstance(insights, Mapping)
    assert isinstance(changes, Mapping)
    return {
        "status": status,
        "period": period_block(period, comparison_block=False),
        "comparison": period_block(comparison, comparison_block=True),
        "currency": "CAD",
        "metrics": _brief_metrics(raw.get("metrics")),
        "observations": _brief_observations(raw.get("observations"), status=status),
        "coverage": {
            "balances": {
                "as_of": balances.get("as_of"),
                "maximum_anchor_lag_days": balances.get("maximum_anchor_lag_days"),
                "reported": int(balances.get("reported", 0)),
                "unavailable": int(balances.get("unavailable", 0)),
            },
            "changes": {
                "basis": changes.get("basis"),
                "delta_available": changes.get("delta_available"),
                "latest_imported_at": changes.get("latest_imported_at"),
                "queried_since": changes.get("queried_since"),
                "statement_count": changes.get("statement_count"),
                "status": str(changes.get("status")),
                "total_added": changes.get("total_added"),
            },
            "freshness": {
                "earliest_transaction_date": freshness.get("earliest_transaction_date"),
                "latest_transaction_date": freshness.get("latest_transaction_date"),
                "latest_transaction_lag_days": freshness.get("latest_transaction_lag_days"),
                "requested_end": freshness.get("requested_end"),
                "status": str(freshness.get("status")),
            },
            "insights": {
                "computed_at": insights.get("computed_at"),
                "entry_count": int(insights.get("entry_count", 0)),
                "scoped_period": insights.get("scoped_period"),
                "status": str(insights.get("status")),
                "unservable_count": int(insights.get("unservable_count", 0)),
            },
            "statements": {
                "account_refs": str(statements.get("account_refs")),
                "gap_counts": dict(statements.get("gap_counts", {})),
                "gaps_evaluated_on": statements.get("gaps_evaluated_on"),
                "matched_account_months": int(statements.get("matched_account_months", 0)),
                "observed_periods": dict(statements.get("observed_periods", {})),
                "queried_periods": dict(statements.get("queried_periods", {})),
                "status_counts": dict(statements.get("status_counts", {})),
            },
        },
    }


def _safe_explanation(raw: Mapping[str, object]) -> dict[str, object]:
    """Remove category names and prose while preserving every arithmetic proof."""
    status = str(raw.get("status"))
    raw_components = raw.get("components", [])
    if not isinstance(raw_components, list):
        raise AgentQueryError("corrupt_data")
    raw_adjustments = raw.get("adjustments")
    adjustments = None
    if isinstance(raw_adjustments, Mapping):
        income = raw_adjustments.get("income_adjustments")
        adjustments = {
            "applied_to_total": bool(raw_adjustments.get("applied_to_total")),
            "income_adjustments": (
                None
                if not isinstance(income, Mapping)
                else {
                    key: income.get(key)
                    for key in ("other_adjustments", "refunds", "reimbursements", "total")
                }
            ),
            "other_adjustments": raw_adjustments.get("other_adjustments"),
            "refunds": raw_adjustments.get("refunds"),
            "reimbursements": raw_adjustments.get("reimbursements"),
            "total": raw_adjustments.get("total"),
        }
    base: dict[str, object] = {
        "adjustments": adjustments,
        "components": [
            {
                "amount": item.get("amount"),
                "component": item.get("name"),
                "operation": item.get("operation"),
            }
            for item in raw_components
            if isinstance(item, Mapping)
        ],
        "currency": "CAD",
        "evidence": raw.get("evidence"),
        "exclusions": raw.get("exclusions"),
        "formula": raw.get("formula"),
        "metric": str(raw.get("metric")),
        "period": raw.get("period"),
        "reconciliation": raw.get("reconciliation"),
        "relevant_transactions": [],
        "source_date_bounds": raw.get("source_date_bounds"),
        "status": status,
        "total": raw.get("total"),
        "unavailable_reason": None,
        "anchor": None,
        "budget": None,
        "projection": None,
    }
    evidence_rows = raw.get("relevant_transactions", [])
    if not isinstance(evidence_rows, list) or len(evidence_rows) > 20:
        raise AgentQueryError("corrupt_data")
    base["relevant_transactions"] = [
        {
            "amount": row.get("amount"),
            "currency": row.get("currency"),
            "evidence_id": row.get("evidence_id"),
            "posting_date": row.get("posting_date"),
            "role": row.get("role"),
        }
        for row in evidence_rows
        if isinstance(row, Mapping)
    ]
    if status == "unavailable":
        unavailable = raw.get("unavailable")
        if not isinstance(unavailable, Mapping):
            raise AgentQueryError("corrupt_data")
        unavailable_reason = unavailable.get("reason")
        if (
            not isinstance(unavailable_reason, str)
            or unavailable_reason not in EXPLANATION_UNAVAILABLE_REASONS
        ):
            raise AgentQueryError("corrupt_data")
        base["unavailable_reason"] = unavailable_reason
        return base
    anchor = raw.get("statement_anchor")
    if isinstance(anchor, Mapping):
        base["anchor"] = dict(anchor)
    budget = raw.get("budget_context")
    if isinstance(budget, Mapping):
        base["budget"] = {
            key: budget.get(key)
            for key in (
                "actual_gross_spending",
                "actual_basis",
                "definition_count",
                "status",
                "variance",
            )
        }
    projection = raw.get("projection_context")
    if isinstance(projection, Mapping):
        lines = projection.get("line_items", [])
        suppressions = projection.get("suppressions", [])
        if (
            not isinstance(lines, list)
            or len(lines) > 20
            or not isinstance(suppressions, list)
            or len(suppressions) > 20
        ):
            raise AgentQueryError("corrupt_data")
        source_coverage = projection.get("source_coverage")
        if not isinstance(source_coverage, Mapping):
            raise AgentQueryError("corrupt_data")
        assumption_codes = ["cad_only", "statement_anchors", "arithmetic_not_prediction"]
        if int(source_coverage.get("detected_pattern_count", 0)):
            assumption_codes.append("detected_patterns_are_possibilities")
        if int(source_coverage.get("hypothetical_schedule_count", 0)):
            assumption_codes.append("hypothetical_included")
        if int(source_coverage.get("suppressed_occurrence_count", 0)):
            assumption_codes.append("unique_import_match_suppression")
        if int(source_coverage.get("ambiguous_suppression_count", 0)):
            assumption_codes.append("ambiguous_matches_not_suppressed")
        base["projection"] = {
            "anchor": projection.get("anchor"),
            "as_of": projection.get("as_of"),
            "assumption_codes": assumption_codes,
            "line_item_summary": projection.get("line_item_summary"),
            "line_items": [
                {
                    "amount": line.get("amount"),
                    "confidence": (
                        None
                        if line.get("confidence") is None
                        else f"{Decimal(str(line.get('confidence'))):.2f}"
                    ),
                    "currency": line.get("currency"),
                    "direction": line.get("direction"),
                    "event_type": line.get("event_type"),
                    "in_selected_period": line.get("in_selected_period"),
                    "is_spending": line.get("is_spending"),
                    "occurrence_date": line.get("occurrence_date"),
                    "provenance": line.get("provenance"),
                    "source_kind": line.get("source_kind"),
                }
                for line in lines
                if isinstance(line, Mapping)
            ],
            "lowest_projected_balance": projection.get("lowest_projected_balance"),
            "lowest_projected_date": projection.get("lowest_projected_date"),
            "net_projected_flow": projection.get("net_projected_flow"),
            "opening_projected_balance": projection.get("opening_projected_balance"),
            "period_projected_movement": projection.get("period_projected_movement"),
            "source_coverage": dict(source_coverage),
            "suppression_summary": projection.get("suppression_summary"),
            "suppressions": [
                {
                    "amount": row.get("amount"),
                    "currency": row.get("currency"),
                    "event_type": row.get("event_type"),
                    "matched_evidence_ids": row.get("matched_evidence_ids"),
                    "matched_kind": row.get("matched_kind"),
                    "occurrence_date": row.get("occurrence_date"),
                    "provenance": row.get("provenance"),
                    "source_kind": row.get("source_kind"),
                }
                for row in suppressions
                if isinstance(row, Mapping)
            ],
            "target_date": projection.get("target_date"),
        }
    return base


def _event_ref(salt: str, internal_id: str) -> str:
    digest = hmac.new(salt.encode(), f"cash-plan-event|{internal_id}".encode(), hashlib.sha256)
    return f"event-{digest.hexdigest()[:10]}"


def _safe_cash_event_type(value: object) -> str:
    event_type = str(value)
    if event_type in {"bill", "card_payment", "income", "transfer"}:
        return event_type
    return "activity"


def _safe_cash_plan(
    plan: CashPlan,
    *,
    refs_by_id: Mapping[str, str],
    salt: str,
) -> dict[str, object]:
    """Map plan identities to opaque refs and drop every user/stored label."""

    def ref(account_id: str | None) -> str | None:
        if account_id is None:
            return None
        value = refs_by_id.get(account_id)
        if value is None:
            raise AgentQueryError("corrupt_data")
        return value

    accounts_by_event: dict[str, set[str]] = {}
    for leg in plan.legs:
        accounts_by_event.setdefault(leg.occurrence_id, set()).add(leg.account_id)

    def leg_block(leg: CashPlanLeg) -> dict[str, object]:
        account_id = str(leg.account_id)
        event_id = str(leg.occurrence_id)
        peers = sorted(accounts_by_event.get(event_id, set()) - {account_id})
        counterparty = ref(peers[0]) if len(peers) == 1 else None
        return {
            "account_ref": ref(account_id),
            "amount": _wire_cent(leg.amount),
            "confidence": _wire_confidence(leg.confidence),
            "counterparty_account_ref": counterparty,
            "currency": "CAD",
            "direction": str(leg.direction),
            "event_ref": _event_ref(salt, event_id),
            "event_type": _safe_cash_event_type(leg.event_type),
            "in_selected_period": bool(leg.in_selected_period),
            "is_spending": bool(leg.is_spending),
            "occurrence_date": leg.occurrence_date.isoformat(),
            "provenance": str(leg.provenance),
            "source_kind": str(leg.source_kind),
        }

    accounts = []
    for account in plan.accounts:
        anchor = account.anchor
        accounts.append(
            {
                "account_ref": ref(account.account_id),
                "account_type": account.account_type,
                "anchor": {
                    "as_of_lag_days": anchor.as_of_lag_days,
                    "balance": None if anchor.balance is None else _wire_cent(anchor.balance),
                    "balance_date": (
                        None if anchor.balance_date is None else anchor.balance_date.isoformat()
                    ),
                    "basis": anchor.basis,
                    "status": anchor.status,
                },
                "institution": account.institution,
                "is_credit_card": account.is_credit_card,
                "fact_legs": [leg_block(leg) for leg in account.fact_legs],
                "lowest_projected_balance": (
                    None
                    if account.lowest_projected_balance is None
                    else _wire_cent(account.lowest_projected_balance)
                ),
                "lowest_projected_date": (
                    None
                    if account.lowest_projected_date is None
                    else account.lowest_projected_date.isoformat()
                ),
                "actual_roll_forward_movement": _wire_cent(account.actual_roll_forward_movement),
                "movement_since_anchor": _wire_cent(account.movement_since_anchor),
                "opening_projected_balance": (
                    None
                    if account.opening_projected_balance is None
                    else _wire_cent(account.opening_projected_balance)
                ),
                "projected_balance": (
                    None
                    if account.projected_balance is None
                    else _wire_cent(account.projected_balance)
                ),
                "planned_movement": _wire_cent(account.planned_movement),
                "planning_legs": [leg_block(leg) for leg in account.planning_legs],
            }
        )

    suppressions = [
        {
            "account_ref": ref(row.account_id),
            "amount": _wire_cent(row.amount),
            "counterparty_account_ref": ref(row.counterparty_account_id),
            "currency": "CAD",
            "event_ref": _event_ref(salt, row.occurrence_id),
            "event_type": _safe_cash_event_type(row.event_type),
            "matched_evidence_ids": list(row.matched_evidence_ids),
            "matched_kind": row.matched_kind,
            "occurrence_date": row.occurrence_date.isoformat(),
            "provenance": row.provenance,
            "source_kind": row.source_kind,
        }
        for row in plan.suppressions
    ]
    coverage = plan.coverage
    assumption_codes = ["cad_only", "statement_anchors", "arithmetic_not_prediction"]
    if coverage.detected_pattern_count:
        assumption_codes.append("detected_patterns_are_possibilities")
    if coverage.hypothetical_schedule_count:
        assumption_codes.append("hypothetical_included")
    if coverage.missing_anchor_account_ids:
        assumption_codes.append("missing_anchors_unavailable")
    if coverage.suppressed_occurrence_count:
        assumption_codes.append("unique_import_match_suppression")
    if coverage.ambiguous_suppression_count:
        assumption_codes.append("ambiguous_matches_not_suppressed")
    if coverage.excluded_pre_as_of_planning_occurrence_count:
        assumption_codes.append("pre_as_of_planning_occurrences_excluded")
    facts = plan.facts
    assumption_codes.append("imported_facts_applied_through_as_of")
    if facts.deduplicated_representation_count:
        assumption_codes.append("duplicate_transfer_representations_excluded")
    if facts.excluded_non_cad_count:
        assumption_codes.append("non_cad_facts_excluded")
    if facts.unmapped_account_count:
        assumption_codes.append("unmapped_fact_accounts_excluded")
    before = plan.before_next_payday
    return {
        "account_ref_semantics": ACCOUNT_REF_SEMANTICS,
        "account_refs": "disclosed",
        "account_refs_withheld_reason": None,
        "accounts": accounts,
        "as_of": plan.as_of.isoformat(),
        "assumption_codes": assumption_codes,
        "before_next_payday": (
            None
            if before is None
            else {
                "accounts": [
                    {
                        "account_ref": ref(account.account_id),
                        "projected_balance_before_payday": (
                            None
                            if account.projected_balance_before_payday is None
                            else _wire_cent(account.projected_balance_before_payday)
                        ),
                        "planned_movement": _wire_cent(account.projected_movement),
                    }
                    for account in before.accounts
                ],
                "includes_hypothetical": before.includes_hypothetical,
                "income_amount": _wire_cent(before.income_amount),
                "income_provenances": list(before.income_provenances),
                "payday_date": before.payday_date.isoformat(),
            }
        ),
        "coverage": {
            "account_count": coverage.account_count,
            "active_schedule_count": coverage.active_schedule_count,
            "applied_planning_occurrence_count": coverage.applied_planning_occurrence_count,
            "ambiguous_suppression_count": coverage.ambiguous_suppression_count,
            "anchored_account_count": coverage.anchored_account_count,
            "confirmed_schedule_count": coverage.confirmed_schedule_count,
            "detected_pattern_count": coverage.detected_pattern_count,
            "excluded_pre_as_of_planning_occurrence_count": (
                coverage.excluded_pre_as_of_planning_occurrence_count
            ),
            "hypothetical_schedule_count": coverage.hypothetical_schedule_count,
            "missing_anchor_account_refs": [
                ref(account_id) for account_id in coverage.missing_anchor_account_ids
            ],
            "occurrence_count": coverage.occurrence_count,
            "omitted_detected_patterns": [
                {"count": row.count, "reason": row.reason}
                for row in coverage.omitted_detected_patterns
            ],
            "overridden_detected_pattern_count": coverage.overridden_detected_pattern_count,
            "stored_schedule_count": coverage.stored_schedule_count,
            "suppressed_occurrence_count": coverage.suppressed_occurrence_count,
        },
        "currency": "CAD",
        "end_date": plan.end_date.isoformat(),
        "facts": {
            "actual_through": facts.actual_through.isoformat(),
            "deduplicated_representation_count": facts.deduplicated_representation_count,
            "excluded_non_cad_count": facts.excluded_non_cad_count,
            "fact_leg_count": facts.fact_leg_count,
            "transaction_leg_count": facts.transaction_leg_count,
            "transfer_leg_count": facts.transfer_leg_count,
            "unmapped_account_count": facts.unmapped_account_count,
        },
        "fact_legs": [leg_block(leg) for leg in plan.fact_legs],
        "planning_legs": [leg_block(leg) for leg in plan.planning_legs],
        "start_date": plan.start_date.isoformat(),
        "status": "partial" if coverage.missing_anchor_account_ids else "ready",
        "suppressions": suppressions,
    }


class LedgerMCPService:
    """Own one pinned database snapshot and expose only bounded read queries."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._lifecycle_lock = threading.RLock()
        self._investment_vocabulary = asset_category_vocabulary()
        self.database, self.snapshot, self.query, self.account_ref_salt = self._open()

    @property
    def investment_vocabulary(self) -> AssetCategoryVocabulary:
        """The immutable vocabulary shared by this session's schema and reads."""
        return self._investment_vocabulary

    def _require_investment_vocabulary(self) -> None:
        try:
            if asset_category_vocabulary() != self._investment_vocabulary:
                raise IncompatibleSchemaError
        except AdapterError:
            raise IncompatibleSchemaError from None

    def _open(self) -> tuple[ReadOnlyDatabase, DatabaseSnapshot, LedgerQueryService, str | None]:
        """Pin the database, preflight it, and build a query service over that pin.

        Returns them together because they are one unit: LedgerQueryService
        captures its session factory at construction, so a query service built
        over a superseded pin would read through a closed database. The
        account-ref salt is read here for the same reason -- it belongs to the
        database that was just pinned, and it is re-read on every re-pin.
        """
        database = ReadOnlyDatabase(self._db_path)
        try:
            snapshot = database.preflight()
            query = LedgerQueryService(
                database.path,
                session_scope_factory=database.session_scope,
                snapshot_generation=snapshot.generation,
            )
            # Inside the guard: anything raised here must still close the pin,
            # which holds two file descriptors.
            salt = self._read_account_ref_salt(database)
        except FileNotFoundError:
            # LedgerQueryService raises FileNotFoundError, an OSError, which the
            # wire adapter would otherwise map to `internal_error`.
            database.close()
            raise DatabaseUnavailableError from None
        except BaseException:
            database.close()
            raise
        return database, snapshot, query, salt

    def _read_account_ref_salt(self, database: ReadOnlyDatabase) -> str | None:
        """Read the salt that turns an account into an opaque ref, or None.

        The salt is minted by a WRITE (`ledger config ensure-account-salt`) and
        this server cannot write, so absence is a legitimate state and never an
        error: the coverage tool withholds its per-account entries instead of
        falling back to raw account slugs.

        A failure to read it is treated as absence for the same reason. Failing
        the whole server here would take down four tools over one optional value.
        """
        try:
            with database.session_scope(database.path) as session:
                row = session.get(ConfigMetadataDB, ACCOUNT_REF_SALT_KEY)
                return str(row.value) if row is not None else None
        except SQLAlchemyError, OSError:
            return None

    def _repin(self) -> None:
        """Replace the pinned database after it changed underneath us.

        Build first, swap second, close last. A partial swap would leave the
        query service bound to a database that is about to be closed, and the
        old pin holds two file descriptors that leak unless closed explicitly.

        A failed re-pin propagates and leaves the existing pin in place, so the
        fail-closed posture survives: every answer is still served from exactly
        one validated snapshot.
        """
        with self._lifecycle_lock:
            database, snapshot, query, salt = self._open()
            superseded = self.database
            self.database, self.snapshot, self.query = database, snapshot, query
            self.account_ref_salt = salt
            superseded.close()

    def _served(self, call: Callable[[], _ServedT]) -> _ServedT:
        """Run one read, re-pinning and retrying once if the database rotated.

        The retry is safe only because the snapshot generation is bound into
        every cursor: a cursor minted against the old database fails
        `invalid_cursor` rather than silently paginating the new one. The
        generation is also on the wire, so a caller can see that the data
        changed mid-conversation.

        MCP v2 offloads synchronous calls to worker threads. Hold the lifecycle
        lock across the complete attempt/re-pin/retry sequence so one response
        reads one immutable generation bundle and a superseded pin cannot close
        while another request still borrows it.
        """
        with self._lifecycle_lock:
            try:
                return self._attempt(call)
            except DatabaseChangedError:
                self._repin()
                return self._attempt(call)

    @staticmethod
    def _attempt(call: Callable[[], _ServedT]) -> _ServedT:
        """One read, with a stored-value failure named as one.

        Everything inside a `_impl` is a read of STORED data: the caller's
        arguments were parsed and validated in `server.py` before this was
        reached, so a bare `ValueError` raised in here is the database, not the
        request. It reaches this frame most often from the driver's own result
        processor -- a DATETIME column holding text that will not parse raises
        `ValueError` in SQLAlchemy before `_as_datetime` can name it -- and
        `server.py` mapped every such error to `invalid_argument`, telling a
        caller to fix a request that was never wrong. `AgentQueryError` is a
        ValueError subclass and carries its own code, so it passes through
        untouched. Both codes are non-retryable: only the diagnosis changes.

        Applied here rather than per tool, because a stored-value failure has
        exactly one meaning on all eighteen surfaces and a per-tool fix would be
        the class of half-applied change this release exists to remove.
        """
        try:
            return call()
        except AgentQueryError:
            raise
        except ValueError as error:
            raise AgentQueryError("corrupt_data") from error

    def status(self) -> dict[str, object]:
        return self._served(self._status)

    def _status(self) -> dict[str, object]:
        self.database.validate_current()
        return {
            "schema": ENVELOPE_SCHEMA,
            "instruction_trust": INSTRUCTION_TRUST,
            "data": {
                "available_date_range": {
                    "end": _date_or_none(self.snapshot.latest_date),
                    "start": _date_or_none(self.snapshot.earliest_date),
                },
                "contract": ENVELOPE_SCHEMA,
                "contract_digest": contract_digest(
                    investment_vocabulary=self.investment_vocabulary
                ),
                "database_ready": True,
                "latest_imported_at": _timestamp_or_none(self.snapshot.latest_imported_at),
                "migration_version": self.snapshot.migration_version,
                "package_version": __version__,
                "product": "ledger",
                "read_only": True,
                "transaction_count": self.snapshot.transaction_count,
                "transport": "stdio",
            },
            "coverage": _coverage(self.snapshot, start_date=None, end_date=None),
        }

    def aggregate(
        self,
        *,
        start_date: date,
        end_date: date,
        flow: str,
        group_by: str,
        filters: AgentFilters,
        limit: int,
        cursor: str | None,
        min_amount: Decimal | None = None,
        max_amount: Decimal | None = None,
    ) -> dict[str, object]:
        return self._served(
            lambda: self._aggregate(
                start_date=start_date,
                end_date=end_date,
                flow=flow,
                group_by=group_by,
                filters=filters,
                limit=limit,
                cursor=cursor,
                min_amount=min_amount,
                max_amount=max_amount,
            )
        )

    def _aggregate(
        self,
        *,
        start_date: date,
        end_date: date,
        flow: str,
        group_by: str,
        filters: AgentFilters,
        limit: int,
        cursor: str | None,
        min_amount: Decimal | None,
        max_amount: Decimal | None,
    ) -> dict[str, object]:
        result = self.query.agent.aggregate_transaction_facts(
            start_date=start_date,
            end_date=end_date,
            flow=flow,
            group_by=group_by,
            filters=filters,
            limit=limit,
            cursor=cursor,
            min_amount=min_amount,
            max_amount=max_amount,
            account_ref_salt=self.account_ref_salt,
        )
        data: dict[str, object] = {
            "flow": flow,
            "group_by": group_by,
            "group_count": result.group_count,
            "groups": [_aggregate_group(group) for group in result.groups],
            "excluded": _excluded(result.excluded, flow),
            "matching": _matching(result),
            "queried_period": {
                "end": end_date.isoformat(),
                "start": start_date.isoformat(),
            },
            "total": _money(result.total),
            "transaction_count": result.transaction_count,
        }
        if group_by == "account_ref":
            # Only this grouping emits an account identity, so only this grouping
            # states the two-namespace rule and the fail-closed withholding.
            data["account_ref_semantics"] = ACCOUNT_REF_SEMANTICS
            data["account_refs"] = "disclosed" if result.account_refs_disclosed else "withheld"
            data["account_refs_withheld_reason"] = (
                None if result.account_refs_disclosed else "account_ref_salt_absent"
            )
        return {
            "schema": ENVELOPE_SCHEMA,
            "instruction_trust": INSTRUCTION_TRUST,
            "data": data,
            "coverage": _coverage(
                self.snapshot,
                start_date=start_date,
                end_date=end_date,
                matched_rows=result.transaction_count,
            ),
            "page": {
                "has_more": result.has_more,
                "limit": limit,
                "next_cursor": result.next_cursor,
                "returned": len(result.groups),
            },
            "policy": _policy(flow),
        }

    def search(
        self,
        *,
        start_date: date,
        end_date: date,
        flow: str,
        filters: AgentFilters,
        limit: int,
        cursor: str | None,
        min_amount: Decimal | None = None,
        max_amount: Decimal | None = None,
        order: str = "date_asc",
    ) -> dict[str, object]:
        return self._served(
            lambda: self._search(
                start_date=start_date,
                end_date=end_date,
                flow=flow,
                filters=filters,
                limit=limit,
                cursor=cursor,
                min_amount=min_amount,
                max_amount=max_amount,
                order=order,
            )
        )

    def _search(
        self,
        *,
        start_date: date,
        end_date: date,
        flow: str,
        filters: AgentFilters,
        limit: int,
        cursor: str | None,
        min_amount: Decimal | None,
        max_amount: Decimal | None,
        order: str,
    ) -> dict[str, object]:
        result = self.query.agent.search_transaction_evidence(
            start_date=start_date,
            end_date=end_date,
            flow=flow,
            filters=filters,
            limit=limit,
            cursor=cursor,
            min_amount=min_amount,
            max_amount=max_amount,
            order=order,
        )
        return {
            "schema": ENVELOPE_SCHEMA,
            "instruction_trust": INSTRUCTION_TRUST,
            "data": {
                "flow": flow,
                "items": [_item(item) for item in result.items],
                "matching": _matching(result),
                "queried_period": {
                    "end": end_date.isoformat(),
                    "start": start_date.isoformat(),
                },
                "transaction_count": result.total_count,
            },
            "coverage": _coverage(
                self.snapshot,
                start_date=start_date,
                end_date=end_date,
                matched_rows=result.total_count,
            ),
            "page": {
                "has_more": result.has_more,
                "limit": limit,
                "next_cursor": result.next_cursor,
                "returned": len(result.items),
            },
            "policy": _policy(flow),
        }

    def transaction(self, *, evidence_id: str) -> dict[str, object]:
        return self._served(lambda: self._transaction(evidence_id=evidence_id))

    def _transaction(self, *, evidence_id: str) -> dict[str, object]:
        row = self.query.agent.get_transaction_evidence(evidence_id=evidence_id)
        return {
            "schema": ENVELOPE_SCHEMA,
            "instruction_trust": INSTRUCTION_TRUST,
            "data": {"item": _item(row)},
            # No period was requested, so the coverage block reports the range and
            # says so, exactly as `ledger.status` does.
            "coverage": _coverage(self.snapshot, start_date=None, end_date=None),
        }

    def coverage(
        self,
        *,
        start_period: str | None,
        end_period: str | None,
        limit: int,
        cursor: str | None,
    ) -> dict[str, object]:
        return self._served(
            lambda: self._coverage_tool(
                start_period=start_period,
                end_period=end_period,
                limit=limit,
                cursor=cursor,
            )
        )

    def _coverage_tool(
        self,
        *,
        start_period: str | None,
        end_period: str | None,
        limit: int,
        cursor: str | None,
    ) -> dict[str, object]:
        page = self.query.agent.statement_coverage(
            start_period=start_period,
            end_period=end_period,
            limit=limit,
            cursor=cursor,
            account_ref_salt=self.account_ref_salt,
        )
        return {
            "schema": ENVELOPE_SCHEMA,
            "instruction_trust": INSTRUCTION_TRUST,
            "data": {
                "absence_semantics": COVERAGE_ABSENCE_STATEMENT,
                "account_ref_semantics": ACCOUNT_REF_SEMANTICS,
                "account_refs": "disclosed" if page.account_refs_disclosed else "withheld",
                "account_refs_withheld_reason": (
                    None if page.account_refs_disclosed else "account_ref_salt_absent"
                ),
                "entries": [_coverage_entry(entry) for entry in page.entries],
                "entry_count": page.entry_count,
                "gap_counts": {
                    "dormant": page.gap_census.counts.dormant,
                    "naming_a_missing_statement": (
                        page.gap_census.counts.naming_a_missing_statement
                    ),
                    "overdue": page.gap_census.counts.overdue,
                    "unknown": page.gap_census.counts.unknown,
                },
                "gap_dormancy_multiplier": DORMANCY_MULTIPLIER,
                "gap_delivery_grace_days": DELIVERY_GRACE_DAYS,
                "gap_minimum_cadence_observations": MIN_CADENCE_OBSERVATIONS,
                "gap_semantics": COVERAGE_GAP_STATEMENT,
                "gaps": [_coverage_gap(gap) for gap in page.gap_census.gaps],
                "gaps_assessed_accounts": page.gap_census.assessed_accounts,
                "gaps_evaluated_on": page.gaps_evaluated_on.isoformat(),
                "gaps_truncated": page.gap_census.truncated,
                "gaps_unscoreable_accounts": page.gap_census.unscoreable_accounts,
                "row_count_semantics": COVERAGE_ROW_COUNT_STATEMENT,
                "observed_periods": {
                    "end": page.observed_end,
                    "start": page.observed_start,
                },
                "queried_periods": {"end": end_period, "start": start_period},
                "status_counts": {
                    "imported_no_activity": page.status_counts.imported_no_activity,
                    "imported_no_rows_unexplained": (
                        page.status_counts.imported_no_rows_unexplained
                    ),
                    "imported_with_activity": page.status_counts.imported_with_activity,
                },
            },
            # The statement periods are months and cover accounts that may have no
            # transactions at all, so the transaction-derived range answers a
            # different question. It is reported unqueried rather than implied.
            "coverage": _coverage(self.snapshot, start_date=None, end_date=None),
            "page": {
                "has_more": page.has_more,
                "limit": limit,
                "next_cursor": page.next_cursor,
                "returned": len(page.entries),
            },
        }

    def balances(
        self,
        *,
        as_of: date,
        change_since: date | None,
        limit: int,
        cursor: str | None,
    ) -> dict[str, object]:
        return self._served(
            lambda: self._balances_tool(
                as_of=as_of,
                change_since=change_since,
                limit=limit,
                cursor=cursor,
            )
        )

    def _balances_tool(
        self,
        *,
        as_of: date,
        change_since: date | None,
        limit: int,
        cursor: str | None,
    ) -> dict[str, object]:
        page = self.query.agent.account_balances(
            as_of=as_of,
            change_since=change_since,
            limit=limit,
            cursor=cursor,
            account_ref_salt=self.account_ref_salt,
        )
        return {
            "schema": ENVELOPE_SCHEMA,
            "instruction_trust": INSTRUCTION_TRUST,
            "data": {
                # Emitted here too, though this surface serves only the `acct-`
                # namespace: its refs are joinable to ledger.coverage's and
                # ledger.changes', and the rule a caller needs is which namespace a
                # ref they are holding belongs to. Carried on three of the four
                # ref-emitting surfaces, the fourth's refs read as a different kind
                # of value.
                "account_ref_semantics": ACCOUNT_REF_SEMANTICS,
                "account_refs": "disclosed" if page.account_refs_disclosed else "withheld",
                "account_refs_withheld_reason": (
                    None if page.account_refs_disclosed else "account_ref_salt_absent"
                ),
                "entries": [_balance_entry(entry) for entry in page.entries],
                "entry_count": page.entry_count,
                "level_counts": {
                    "no_absolute_anchor": page.level_counts.no_absolute_anchor,
                    "no_anchor_on_or_before_as_of": (
                        page.level_counts.no_anchor_on_or_before_as_of
                    ),
                    "reported": page.level_counts.reported,
                },
                # On the wire as well as in the tool description: a rule stated
                # only in the description is one an answer can be produced
                # without ever having read.
                "level_semantics": BALANCE_LEVEL_STATEMENT,
                "queried_as_of": as_of.isoformat(),
                "queried_change_since": _date_or_none(change_since),
                "utilization_semantics": CREDIT_UTILIZATION_STATEMENT,
            },
            # The balance series is per-account and per-day; the transaction range
            # answers a different question, so `as_of` is reported against it
            # rather than implied to be covered by it.
            "coverage": _coverage(
                self.snapshot,
                start_date=change_since if change_since is not None else as_of,
                end_date=as_of,
            ),
            "page": {
                "has_more": page.has_more,
                "limit": limit,
                "next_cursor": page.next_cursor,
                "returned": len(page.entries),
            },
        }

    def investment_details(
        self,
        *,
        tool: str,
        start_date: date | None = None,
        end_date: date | None = None,
        account_ref: str | None = None,
        statement_ref: str | None = None,
        limit: int = 50,
        cursor: str | None = None,
    ) -> dict[str, object]:
        def read() -> dict[str, object]:
            self._require_investment_vocabulary()
            with self.database.session_scope(self.database.path) as session:
                result = build_investment_result(
                    session,
                    tool=tool,
                    generation=self.snapshot.generation,
                    salt=self.account_ref_salt,
                    start_date=start_date,
                    end_date=end_date,
                    account_ref=account_ref,
                    statement_ref=statement_ref,
                    limit=limit,
                    cursor=cursor,
                    vocabulary=self.investment_vocabulary,
                )
            self._require_investment_vocabulary()
            cast(dict[str, object], result["data"])["account_ref_semantics"] = ACCOUNT_REF_SEMANTICS
            return {
                "schema": ENVELOPE_SCHEMA,
                "instruction_trust": INSTRUCTION_TRUST,
                # This outer range is banking transaction coverage. The data
                # block separately states investment statement evidence scope.
                "coverage": _coverage(self.snapshot, start_date=start_date, end_date=end_date),
                **result,
            }

        return self._served(read)

    def net_worth(
        self,
        *,
        as_of: date,
        limit: int,
        cursor: str | None,
    ) -> dict[str, object]:
        return self._served(lambda: self._net_worth_tool(as_of=as_of, limit=limit, cursor=cursor))

    def _net_worth_tool(
        self,
        *,
        as_of: date,
        limit: int,
        cursor: str | None,
    ) -> dict[str, object]:
        page = self.query.agent.net_worth(
            as_of=as_of,
            limit=limit,
            cursor=cursor,
            account_ref_salt=self.account_ref_salt,
        )
        return {
            "schema": ENVELOPE_SCHEMA,
            "instruction_trust": INSTRUCTION_TRUST,
            "data": {
                "account_ref_semantics": ACCOUNT_REF_SEMANTICS,
                "account_refs": "disclosed" if page.account_refs_disclosed else "withheld",
                "account_refs_withheld_reason": (
                    None if page.account_refs_disclosed else "account_ref_salt_absent"
                ),
                "closure_semantics": NET_WORTH_CLOSURE_STATEMENT,
                "component_count": page.component_count,
                "component_counts": {
                    "banking_reported": page.component_counts.banking_reported,
                    "banking_no_anchor_on_or_before_as_of": (
                        page.component_counts.banking_no_anchor_on_or_before_as_of
                    ),
                    "banking_no_absolute_anchor": (
                        page.component_counts.banking_no_absolute_anchor
                    ),
                    "investment_reported": page.component_counts.investment_reported,
                    "investment_no_statement_on_or_before_as_of": (
                        page.component_counts.investment_no_statement_on_or_before_as_of
                    ),
                },
                "component_counts_semantics": NET_WORTH_COMPONENT_COUNTS_STATEMENT,
                "components": [_net_worth_component(component) for component in page.components],
                "currency_semantics": NET_WORTH_CURRENCY_STATEMENT,
                "quantization_semantics": NET_WORTH_QUANTIZATION_STATEMENT,
                "queried_as_of": as_of.isoformat(),
                "subtotals": [
                    {
                        "amount": _signed_money(subtotal.amount),
                        "component_count": subtotal.component_count,
                        "currency": subtotal.currency,
                    }
                    for subtotal in page.subtotals
                ],
            },
            # Banking anchors and investment statements are not transaction rows,
            # so the transaction-derived range is disclosed, never promoted into
            # a claim that either statement domain is complete.
            "coverage": _coverage(self.snapshot, start_date=as_of, end_date=as_of),
            "page": {
                "has_more": page.has_more,
                "limit": limit,
                "next_cursor": page.next_cursor,
                "returned": len(page.components),
            },
        }

    def projection(
        self,
        *,
        as_of: date,
        target_date: date,
        limit: int,
        cursor: str | None,
    ) -> dict[str, object]:
        return self._served(
            lambda: self._projection_tool(
                as_of=as_of,
                target_date=target_date,
                limit=limit,
                cursor=cursor,
            )
        )

    def _projection_tool(
        self,
        *,
        as_of: date,
        target_date: date,
        limit: int,
        cursor: str | None,
    ) -> dict[str, object]:
        page = self.query.agent.projection(
            as_of=as_of,
            target_date=target_date,
            limit=limit,
            cursor=cursor,
            account_ref_salt=self.account_ref_salt,
        )
        return {
            "schema": ENVELOPE_SCHEMA,
            "instruction_trust": INSTRUCTION_TRUST,
            "data": {
                "account_ref_semantics": ACCOUNT_REF_SEMANTICS,
                "account_refs": "disclosed" if page.account_refs_disclosed else "withheld",
                "account_refs_withheld_reason": (
                    None if page.account_refs_disclosed else "account_ref_salt_absent"
                ),
                # Every one of these is on the wire as well as in the tool
                # description: a rule stated only in the description is one an
                # answer can be produced without ever having read.
                "amount_semantics": PROJECTION_AMOUNT_STATEMENT,
                # Over EVERY account, not just the page. Unpaginated, and the
                # schema says so, because a caller following the cursor protocol
                # and accumulating this block would count every account once per
                # page.
                "anchor_counts": {
                    "no_absolute_anchor": page.anchor_counts.no_absolute_anchor,
                    "no_anchor_on_or_before_as_of": (
                        page.anchor_counts.no_anchor_on_or_before_as_of
                    ),
                    "reported": page.anchor_counts.reported,
                },
                # The anchor IS a balance level, selected the way ledger.balances
                # selects one, so it carries that tool's statement rather than a
                # paraphrase of it that could drift.
                "anchor_semantics": BALANCE_LEVEL_STATEMENT,
                # What "its OWN account" means and what it does not: the detector
                # groups by account and merchant together, so every occurrence a
                # pattern was built from posted to the account it names -- and
                # nothing reattributes a merchant that MOVES.
                "attribution_semantics": PROJECTION_ATTRIBUTION_STATEMENT,
                "classification": {
                    "fallthrough_default": "subscription",
                    "statement": RECURRING_CLASSIFICATION_CAVEAT,
                },
                "entries": [_projection_entry(entry) for entry in page.entries],
                "entry_count": page.entry_count,
                # The bound that actually governs the served list, per account.
                # The horizon below bounds (as_of, target]; this one bounds
                # (anchor, target], which is the window that makes line items.
                "max_line_items_per_account": DEFAULT_MAX_LINE_ITEMS,
                "missed_count_semantics": PROJECTION_MISSED_COUNT_STATEMENT,
                "projection_horizon_days": PROJECTION_HORIZON_DAYS,
                "queried_as_of": as_of.isoformat(),
                "queried_target_date": target_date.isoformat(),
                "reconciliation_semantics": PROJECTION_RECONCILIATION_STATEMENT,
                "roll_forward_semantics": PROJECTION_ROLL_FORWARD_STATEMENT,
                "scope_semantics": PROJECTION_SCOPE_STATEMENT,
            },
            # The window reaches past the imported range by construction, and the
            # coverage block says so rather than implying the projected dates are
            # covered by imported rows.
            "coverage": _coverage(self.snapshot, start_date=as_of, end_date=target_date),
            "page": {
                "has_more": page.has_more,
                "limit": limit,
                "next_cursor": page.next_cursor,
                "returned": len(page.entries),
            },
        }

    def changes(
        self,
        *,
        since: datetime,
        client_generation: str | None,
        limit: int,
        cursor: str | None,
    ) -> dict[str, object]:
        return self._served(
            lambda: self._changes_tool(
                since=since,
                client_generation=client_generation,
                limit=limit,
                cursor=cursor,
            )
        )

    def _changes_tool(
        self,
        *,
        since: datetime,
        client_generation: str | None,
        limit: int,
        cursor: str | None,
    ) -> dict[str, object]:
        page = self.query.agent.changes(
            since=since,
            client_generation=client_generation,
            limit=limit,
            cursor=cursor,
            account_ref_salt=self.account_ref_salt,
        )
        return {
            "schema": ENVELOPE_SCHEMA,
            "instruction_trust": INSTRUCTION_TRUST,
            "data": {
                # This surface emits BOTH ref namespaces -- a coverage row lands
                # before account detection has seen a newly-imported account -- so
                # it carries the rule its entries must be read under, exactly as
                # ledger.coverage and the account grouping do.
                "account_ref_semantics": ACCOUNT_REF_SEMANTICS,
                # Two-valued: the salt was minted and the entries carry refs, or it
                # was not and they are withheld. A reason belongs to the one case
                # that actually withheld.
                "account_refs": page.account_refs,
                "account_refs_withheld_reason": (
                    "account_ref_salt_absent"
                    if page.account_refs == ACCOUNT_REFS_WITHHELD
                    else None
                ),
                "basis": page.basis,
                # On the wire as well as in the tool description: a rule stated
                # only in the description is one an answer can be produced
                # without ever having read.
                "basis_semantics": CHANGES_BASIS_STATEMENT,
                # Echoed, never consulted. Null when it was not supplied, which is
                # allowed: it is an optional input whose whole yield is
                # `generation_changed` below.
                "client_generation": client_generation,
                "count_semantics": CHANGES_COUNT_STATEMENT,
                "investment_semantics": CHANGES_INVESTMENT_STATEMENT,
                "count_sources": _count_sources(page.count_sources),
                "delta_available": page.delta_available,
                "earliest_imported_at": _timestamp_or_none(page.earliest_imported_at),
                "entries": [_change_entry(entry) for entry in page.entries],
                "entry_count": page.entry_count,
                # The data moved, or it did not, or nothing was supplied to compare
                # against. It informs the caller and reaches no count: every write
                # rotates the generation, so an import moves it exactly as much as
                # a repair, and letting it decide made the surface report a rebuild
                # after every routine import.
                "generation_changed": page.generation_changed,
                "latest_imported_at": _timestamp_or_none(page.latest_imported_at),
                "queried_since": since.isoformat(),
                "since_relation": page.since_relation,
                "total_added": page.total_added,
            },
            # The boundary is an import timestamp and the periods are statement
            # months, so the transaction-derived posting range answers a
            # different question. It is reported unqueried rather than implied.
            "coverage": _coverage(self.snapshot, start_date=None, end_date=None),
            "page": {
                "has_more": page.has_more,
                "limit": limit,
                "next_cursor": page.next_cursor,
                "returned": len(page.entries),
            },
        }

    def insights(self, *, limit: int, cursor: str | None) -> dict[str, object]:
        return self._served(lambda: self._insights_tool(limit=limit, cursor=cursor))

    def brief(
        self,
        *,
        start_date: date,
        end_date: date,
        since: datetime | None,
    ) -> dict[str, object]:
        return self._served(
            lambda: self._brief_tool(start_date=start_date, end_date=end_date, since=since)
        )

    def _brief_tool(
        self,
        *,
        start_date: date,
        end_date: date,
        since: datetime | None,
    ) -> dict[str, object]:
        _validate_financial_period(start_date, end_date)
        raw = FinancialBriefService(
            self.database.path,
            session_scope_factory=self.database.session_scope,
        ).build(start_date, end_date, since=since)
        return {
            "schema": ENVELOPE_SCHEMA,
            "instruction_trust": INSTRUCTION_TRUST,
            "data": _safe_brief(raw),
            "coverage": _coverage(
                self.snapshot,
                start_date=start_date,
                end_date=end_date,
            ),
        }

    def explain(
        self,
        *,
        metric: str,
        start_date: date,
        end_date: date,
        account_ref: str | None,
        category: str | None,
    ) -> dict[str, object]:
        return self._served(
            lambda: self._explain_tool(
                metric=metric,
                start_date=start_date,
                end_date=end_date,
                account_ref=account_ref,
                category=category,
            )
        )

    def _explain_tool(
        self,
        *,
        metric: str,
        start_date: date,
        end_date: date,
        account_ref: str | None,
        category: str | None,
    ) -> dict[str, object]:
        _validate_financial_period(start_date, end_date)
        account_id: str | None = None
        unavailable_reason: str | None = None
        if account_ref is not None:
            if self.account_ref_salt is None:
                unavailable_reason = "account_ref_salt_absent"
            else:
                with self.database.session_scope(self.database.path) as session:
                    account_id = reverse_account_ref_map(session, self.account_ref_salt).get(
                        account_ref
                    )
                if account_id is None:
                    unavailable_reason = "account_ref_not_found"
        if unavailable_reason is not None:
            data: dict[str, object] = {
                "adjustments": None,
                "anchor": None,
                "budget": None,
                "components": [],
                "currency": "CAD",
                "evidence": {
                    "limit": 20,
                    "returned_count": 0,
                    "total_count": 0,
                    "truncated": False,
                },
                "exclusions": None,
                "formula": None,
                "metric": metric,
                "period": {
                    "end": end_date.isoformat(),
                    "inclusive": True,
                    "start": start_date.isoformat(),
                },
                "projection": None,
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
                "unavailable_reason": unavailable_reason,
            }
        else:
            resolved_category = category
            if category is not None and metric not in {"balance", "projection"}:
                with self.database.session_scope(self.database.path) as session:
                    candidates = {
                        str(value)
                        for column in (TransactionDB.category, BudgetDB.category)
                        for (value,) in session.query(column).distinct().all()
                        if value
                    }
                matches = sorted(
                    value
                    for value in candidates
                    if _normalize_match(value) == _normalize_match(category)
                )
                if len(matches) != 1:
                    raise AgentQueryError("invalid_argument")
                resolved_category = matches[0]
            raw = MetricExplanationService(
                self.database.path,
                session_scope_factory=self.database.session_scope,
            ).explain(
                metric,
                start_date,
                end_date,
                account_id=account_id,
                category=resolved_category,
            )
            data = _safe_explanation(raw)
        return {
            "schema": ENVELOPE_SCHEMA,
            "instruction_trust": INSTRUCTION_TRUST,
            "data": data,
            "coverage": _coverage(
                self.snapshot,
                start_date=start_date,
                end_date=end_date,
            ),
        }

    def cash_plan(
        self,
        *,
        start_date: date,
        end_date: date,
        as_of: date,
    ) -> dict[str, object]:
        return self._served(
            lambda: self._cash_plan_tool(
                start_date=start_date,
                end_date=end_date,
                as_of=as_of,
            )
        )

    def _cash_plan_tool(
        self,
        *,
        start_date: date,
        end_date: date,
        as_of: date,
    ) -> dict[str, object]:
        _validate_financial_period(start_date, end_date)
        if not start_date <= as_of <= end_date:
            raise AgentQueryError("invalid_argument")
        if self.account_ref_salt is None:
            data: dict[str, object] = {
                "account_ref_semantics": ACCOUNT_REF_SEMANTICS,
                "account_refs": "withheld",
                "account_refs_withheld_reason": "account_ref_salt_absent",
                "status": "unavailable",
                "unavailable_reason": "account_ref_salt_absent",
            }
        else:
            with self.database.session_scope(self.database.path) as session:
                refs_by_id = account_id_ref_map(session, self.account_ref_salt)
            try:
                plan = CashPlanService(
                    self.database.path,
                    session_scope_factory=self.database.session_scope,
                    prepare_schema=False,
                ).build(start_date=start_date, end_date=end_date, as_of=as_of)
            except CashPlanError as error:
                if error.code in {
                    "actual_activity_too_broad",
                    "filter_too_broad",
                    "too_many_occurrences",
                }:
                    raise AgentQueryError("filter_too_broad") from error
                raise AgentQueryError("corrupt_data") from error
            if plan.coverage.account_count == 0:
                data = {
                    "account_ref_semantics": ACCOUNT_REF_SEMANTICS,
                    "account_refs": "disclosed",
                    "account_refs_withheld_reason": None,
                    "status": "unavailable",
                    "unavailable_reason": "no_accounts",
                }
            else:
                data = _safe_cash_plan(
                    plan,
                    refs_by_id=refs_by_id,
                    salt=self.account_ref_salt,
                )
        return {
            "schema": ENVELOPE_SCHEMA,
            "instruction_trust": INSTRUCTION_TRUST,
            "data": data,
            "coverage": _coverage(
                self.snapshot,
                start_date=start_date,
                end_date=end_date,
            ),
        }

    def planning_definitions(
        self,
        *,
        kind: str,
        exact_name: str | None,
        account_ref: str | None,
        limit: int,
        cursor: str | None,
    ) -> dict[str, object]:
        return self._served(
            lambda: self._planning_definitions_tool(
                kind=kind,
                exact_name=exact_name,
                account_ref=account_ref,
                limit=limit,
                cursor=cursor,
            )
        )

    def _planning_definitions_tool(
        self,
        *,
        kind: str,
        exact_name: str | None,
        account_ref: str | None,
        limit: int,
        cursor: str | None,
    ) -> dict[str, object]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise AgentQueryError
        if account_ref is not None and kind not in {"all", "schedule"}:
            raise AgentQueryError
        if kind == "account" and exact_name is not None:
            raise AgentQueryError
        fingerprint = _cursor_fingerprint(
            kind="planning_definitions",
            generation=self.snapshot.generation,
            scope={
                "kind": kind,
                "exact_name": None if exact_name is None else _normalize_match(exact_name),
                "account_ref": account_ref,
            },
        )
        offset = _decode_cursor(cursor, fingerprint=fingerprint)
        if offset > planning_discovery.MAX_DISCOVERY_SOURCE_ROWS:
            raise AgentQueryError("invalid_cursor")
        has_more = False
        returned = 0
        if not self.account_ref_salt:
            self.database.validate_current()
            data = planning_wire.unavailable(ACCOUNT_REF_SEMANTICS)
        else:
            with self.database.session_scope(self.database.path) as session:
                connection = cast(
                    sqlite3.Connection, session.connection().connection.driver_connection
                )
                previous = connection.row_factory
                try:
                    connection.row_factory = sqlite3.Row
                    raw = discover_planning_definitions(
                        connection,
                        salt=self.account_ref_salt,
                        kind=kind,
                        exact_name=exact_name,
                        account_ref=account_ref,
                        limit=limit,
                        offset=offset,
                    )
                except PlanningDiscoveryLimitError as error:
                    raise AgentQueryError("filter_too_broad") from error
                finally:
                    connection.row_factory = previous
            data = planning_wire.definitions(
                raw,
                semantics=ACCOUNT_REF_SEMANTICS,
                kind=kind,
                filtered=exact_name is not None or account_ref is not None,
                offset=offset,
            )
            has_more = bool(raw["has_more"])
            returned = len(raw["entries"])
        return {
            "schema": ENVELOPE_SCHEMA,
            "instruction_trust": INSTRUCTION_TRUST,
            "data": data,
            "coverage": _coverage(self.snapshot, start_date=None, end_date=None),
            "page": {
                "has_more": has_more,
                "limit": limit,
                "returned": returned,
                "next_cursor": _encode_cursor(offset=offset + returned, fingerprint=fingerprint)
                if has_more
                else None,
            },
        }

    def plan_reconciliation(
        self,
        *,
        start_date: date,
        end_date: date,
        as_of: date,
        limit: int,
        cursor: str | None,
    ) -> dict[str, object]:
        return self._served(
            lambda: self._plan_reconciliation_tool(
                start_date=start_date,
                end_date=end_date,
                as_of=as_of,
                limit=limit,
                cursor=cursor,
            )
        )

    def _plan_reconciliation_tool(
        self,
        *,
        start_date: date,
        end_date: date,
        as_of: date,
        limit: int,
        cursor: str | None,
    ) -> dict[str, object]:
        from ledger.planning.reconciliation import PlanReconciliationService

        _validate_financial_period(start_date, end_date)
        if type(limit) is not int or not 1 <= limit <= 100:
            raise AgentQueryError
        fingerprint = _cursor_fingerprint(
            kind="plan_reconciliation",
            generation=self.snapshot.generation,
            start_date=start_date,
            end_date=end_date,
            scope={"as_of": as_of.isoformat()},
        )
        offset = _decode_cursor(cursor, fingerprint=fingerprint)
        if offset > 10000:
            raise AgentQueryError("invalid_cursor")
        has_more = False
        returned = 0
        if not self.account_ref_salt:
            self.database.validate_current()
            data = planning_wire.unavailable(ACCOUNT_REF_SEMANTICS)
        else:
            with self.database.session_scope(self.database.path) as session:
                refs = account_id_ref_map(session, self.account_ref_salt)
            try:
                raw = PlanReconciliationService(
                    self.database.path,
                    session_scope_factory=self.database.session_scope,
                    prepare_schema=False,
                ).build(start_date, end_date, as_of=as_of, limit=limit, offset=offset)
            except CashPlanError as error:
                if error.code in {
                    "filter_too_broad",
                    "too_many_occurrences",
                    "actual_activity_too_broad",
                }:
                    raise AgentQueryError("filter_too_broad") from error
                raise AgentQueryError("corrupt_data") from error
            data = planning_wire.reconciliation(
                raw,
                semantics=ACCOUNT_REF_SEMANTICS,
                salt=self.account_ref_salt,
                refs=refs,
            )
            returned = len(raw["findings"])
            has_more = offset + returned < raw["total_count"]
        return {
            "schema": ENVELOPE_SCHEMA,
            "instruction_trust": INSTRUCTION_TRUST,
            "data": data,
            "coverage": _coverage(self.snapshot, start_date=start_date, end_date=end_date),
            "page": {
                "has_more": has_more,
                "limit": limit,
                "returned": returned,
                "next_cursor": _encode_cursor(offset=offset + returned, fingerprint=fingerprint)
                if has_more
                else None,
            },
        }

    def _insights_tool(self, *, limit: int, cursor: str | None) -> dict[str, object]:
        page = self.query.agent.insight_findings(
            limit=limit,
            cursor=cursor,
            account_ref_salt=self.account_ref_salt,
        )
        return {
            "schema": ENVELOPE_SCHEMA,
            "instruction_trust": INSTRUCTION_TRUST,
            "data": {
                "account_ref_semantics": ACCOUNT_REF_SEMANTICS,
                "account_refs": ("disclosed" if self.account_ref_salt is not None else "withheld"),
                "account_refs_withheld_reason": (
                    None if self.account_ref_salt is not None else "account_ref_salt_absent"
                ),
                "account_identity_semantics": INSIGHT_ACCOUNT_IDENTITY_STATEMENT,
                "amount_semantics": INSIGHT_AMOUNT_STATEMENT,
                "classification_semantics": RECURRING_CLASSIFICATION_CAVEAT,
                "computed_at": _timestamp_or_none(page.computed_at),
                "entries": [_insight_finding(entry) for entry in page.entries],
                "entry_count": page.entry_count,
                "period_semantics": INSIGHT_PERIOD_STATEMENT,
                "scoped_period": (
                    None
                    if page.period_start is None or page.period_end is None
                    else {
                        "end": page.period_end.isoformat(),
                        "start": page.period_start.isoformat(),
                    }
                ),
                "severity_counts": dict(page.severity_counts),
                "status_counts": dict(page.status_counts),
                "triage_semantics": INSIGHT_TRIAGE_STATEMENT,
                "unservable_count": page.unservable_count,
                "unservable_semantics": INSIGHT_UNSERVABLE_STATEMENT,
                "withheld_field_semantics": INSIGHT_WITHHELD_FIELD_STATEMENT,
            },
            # The scoped period is DERIVED from the stored findings rather than
            # requested, so nothing here was queried: reporting it as a requested
            # window would describe a range the caller never named.
            "coverage": _coverage(self.snapshot, start_date=None, end_date=None),
            "page": {
                "has_more": page.has_more,
                "limit": limit,
                "next_cursor": page.next_cursor,
                "returned": len(page.entries),
            },
        }

    def quality(self, *, limit: int, cursor: str | None) -> dict[str, object]:
        return self._served(lambda: self._quality_tool(limit=limit, cursor=cursor))

    def _quality_tool(self, *, limit: int, cursor: str | None) -> dict[str, object]:
        page = self.query.agent.quality_checks(limit=limit, cursor=cursor)
        return {
            "schema": ENVELOPE_SCHEMA,
            "instruction_trust": INSTRUCTION_TRUST,
            "data": {
                "details_semantics": QUALITY_DETAILS_STATEMENT,
                "entries": [_quality_check(check) for check in page.entries],
                "entry_count": page.entry_count,
                "run_at": _timestamp_or_none(page.run_at),
                "scope_semantics": QUALITY_SCOPE_STATEMENT,
                "severity_counts": dict(page.severity_counts),
                "status_counts": dict(page.status_counts),
                "unservable_count": page.unservable_count,
                "unservable_semantics": QUALITY_UNSERVABLE_STATEMENT,
                "vocabulary_semantics": QUALITY_VOCABULARY_STATEMENT,
            },
            # A check run is not a transaction window: it covers the database as
            # it stood at `run_at`, so the transaction-derived range answers a
            # different question and is reported unqueried.
            "coverage": _coverage(self.snapshot, start_date=None, end_date=None),
            "page": {
                "has_more": page.has_more,
                "limit": limit,
                "next_cursor": page.next_cursor,
                "returned": len(page.entries),
            },
        }

    def budgets(self, *, start_date: date, end_date: date) -> dict[str, object]:
        return self._served(lambda: self._budgets_tool(start_date=start_date, end_date=end_date))

    def _budgets_tool(self, *, start_date: date, end_date: date) -> dict[str, object]:
        if end_date < start_date:
            raise AgentQueryError("invalid_argument")
        service = BudgetService(
            self.database.path,
            session_scope_factory=self.database.session_scope,
            prepare_schema=False,
        )
        try:
            definitions = service.list_budgets()
            if service._budget_errors(definitions):
                raise AgentQueryError("corrupt_data")
        except AgentQueryError:
            raise
        except (InvalidOperation, SQLAlchemyError, TypeError) as error:
            raise AgentQueryError("corrupt_data") from error
        common: dict[str, object] = {
            "actual_basis": BUDGET_ACTUAL_BASIS_STATEMENT,
            "computed_at": _timestamp_or_none(naive_utc_now()),
            "definition_count": len(definitions),
            "queried_period": {
                "end": end_date.isoformat(),
                "start": start_date.isoformat(),
            },
            "selection_semantics": BUDGET_SELECTION_STATEMENT,
        }
        if not definitions:
            data = {
                **common,
                "entries": [],
                "entry_count": 0,
                "status": "no_budgets_defined",
                "totals": None,
            }
        else:
            try:
                rows = sorted(
                    service.budget_variance(start_date, end_date),
                    key=lambda row: str(row["category"]),
                )
            except AgentQueryError:
                raise
            except (InvalidOperation, SQLAlchemyError, TypeError) as error:
                raise AgentQueryError("corrupt_data") from error
            if len(rows) > MAX_BUDGET_ENTRIES:
                raise AgentQueryError("filter_too_broad")
            entries = [
                {
                    "actual": _money(Decimal(str(row["actual"]))),
                    "budget": _money(Decimal(str(row["budget"]))),
                    "category": str(row["category"]),
                    "status": str(row["status"]),
                    "variance": _signed_money(Decimal(str(row["variance"]))),
                    "pct_used": (
                        None if row["pct_used"] is None else _money(Decimal(str(row["pct_used"])))
                    ),
                }
                for row in rows
            ]
            total_budget = sum((Decimal(str(row["budget"])) for row in rows), Decimal("0"))
            total_actual = sum((Decimal(str(row["actual"])) for row in rows), Decimal("0"))
            data = {
                **common,
                "entries": entries,
                "entry_count": len(entries),
                "status": "reported",
                "totals": {
                    "actual": _money(total_actual),
                    "budget": _money(total_budget),
                    "variance": _signed_money(total_budget - total_actual),
                },
            }
        return {
            "schema": ENVELOPE_SCHEMA,
            "instruction_trust": INSTRUCTION_TRUST,
            "data": data,
            "coverage": _coverage(
                self.snapshot,
                start_date=start_date,
                end_date=end_date,
            ),
        }

    def recurring(self, *, status: str, limit: int, cursor: str | None) -> dict[str, object]:
        return self._served(lambda: self._recurring_tool(status=status, limit=limit, cursor=cursor))

    def _recurring_tool(self, *, status: str, limit: int, cursor: str | None) -> dict[str, object]:
        page = self.query.agent.recurring_patterns(status=status, limit=limit, cursor=cursor)
        return {
            "schema": ENVELOPE_SCHEMA,
            "instruction_trust": INSTRUCTION_TRUST,
            "data": {
                # On the wire as well as in the tool description: a caveat only in
                # the description is one an answer can be produced without ever
                # having read.
                "classification": {
                    "fallthrough_default": "subscription",
                    "statement": RECURRING_CLASSIFICATION_CAVEAT,
                },
                "entries": [_recurring_entry(entry) for entry in page.entries],
                "entry_count": page.entry_count,
                "status": status,
                # What next_expected_date, missed_count and typical_amount
                # actually mean as stored. ledger.projection published a
                # statement for each of them and this surface, which hands the
                # same columns straight to the caller, published none.
                "stored_field_semantics": RECURRING_STORED_FIELD_STATEMENT,
            },
            "coverage": _coverage(self.snapshot, start_date=None, end_date=None),
            "page": {
                "has_more": page.has_more,
                "limit": limit,
                "next_cursor": page.next_cursor,
                "returned": len(page.entries),
            },
        }

    def transfers(
        self,
        *,
        start_date: date,
        end_date: date,
        limit: int,
        cursor: str | None,
    ) -> dict[str, object]:
        return self._served(
            lambda: self._transfers_tool(
                start_date=start_date,
                end_date=end_date,
                limit=limit,
                cursor=cursor,
            )
        )

    def _transfers_tool(
        self,
        *,
        start_date: date,
        end_date: date,
        limit: int,
        cursor: str | None,
    ) -> dict[str, object]:
        result = self.query.agent.transfer_aggregates(
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            cursor=cursor,
        )
        return {
            "schema": ENVELOPE_SCHEMA,
            "instruction_trust": INSTRUCTION_TRUST,
            "data": {
                "direction_semantics": TRANSFER_DIRECTION_STATEMENT,
                "total_semantics": TRANSFER_TOTAL_STATEMENT,
                "excluded": {
                    "candidate_rows": result.candidate_rows,
                    "non_cad": result.non_cad,
                },
                "group_count": result.group_count,
                "groups": [_transfer_group(group) for group in result.groups],
                "pairing": {
                    "legs": result.transfer_count,
                    "paired_legs": result.paired_legs,
                    "with_counterparty_institution": result.with_counterparty_institution,
                },
                "queried_period": {
                    "end": end_date.isoformat(),
                    "start": start_date.isoformat(),
                },
                "total": _money(result.total),
                "transfer_count": result.transfer_count,
            },
            "coverage": _coverage(
                self.snapshot,
                start_date=start_date,
                end_date=end_date,
                matched_rows=result.transfer_count,
            ),
            "page": {
                "has_more": result.has_more,
                "limit": limit,
                "next_cursor": result.next_cursor,
                "returned": len(result.groups),
            },
        }

    def dimensions(
        self,
        *,
        start_date: date,
        end_date: date,
        flow: str,
        dimension: str,
        contains: str | None,
        limit: int,
        cursor: str | None,
    ) -> dict[str, object]:
        return self._served(
            lambda: self._dimensions_tool(
                start_date=start_date,
                end_date=end_date,
                flow=flow,
                dimension=dimension,
                contains=contains,
                limit=limit,
                cursor=cursor,
            )
        )

    def _dimensions_tool(
        self,
        *,
        start_date: date,
        end_date: date,
        flow: str,
        dimension: str,
        contains: str | None,
        limit: int,
        cursor: str | None,
    ) -> dict[str, object]:
        page = self.query.agent.dimension_values(
            start_date=start_date,
            end_date=end_date,
            flow=flow,
            dimension=dimension,
            contains=contains,
            limit=limit,
            cursor=cursor,
        )
        return {
            "schema": ENVELOPE_SCHEMA,
            "instruction_trust": INSTRUCTION_TRUST,
            "data": {
                # Echoed verbatim, like the unmatched filter values: a caller must
                # be able to see WHICH request produced an empty list.
                "contains": contains,
                "dimension": dimension,
                "flow": flow,
                "match_semantics": CONTAINS_SEMANTICS,
                "queried_period": {
                    "end": end_date.isoformat(),
                    "start": start_date.isoformat(),
                },
                "value_count": page.value_count,
                "values": [_dimension_value(value) for value in page.values],
            },
            "coverage": _coverage(
                self.snapshot,
                start_date=start_date,
                end_date=end_date,
                matched_rows=page.value_count,
            ),
            "page": {
                "has_more": page.has_more,
                "limit": limit,
                "next_cursor": page.next_cursor,
                "returned": len(page.values),
            },
            # The counts come from the same flow predicates an aggregate applies,
            # so the policy that produced them is reported alongside them.
            "policy": _policy(flow),
        }

    def close(self) -> None:
        with self._lifecycle_lock:
            self.database.close()


__all__ = ["LedgerMCPService"]
