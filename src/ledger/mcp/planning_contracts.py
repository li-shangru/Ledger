"""Allowlisted wire shapes for planning reads, independent of private definitions."""

from __future__ import annotations

from collections.abc import Mapping


def obj(properties: dict[str, object]) -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": list(properties),
    }


DATE = {"type": "string", "format": "date", "pattern": r"^\d{4}-\d{2}-\d{2}$"}
COUNT = {"type": "integer", "minimum": 0}
BOOLEAN = {"type": "boolean"}
MONEY = {"type": "string", "pattern": r"^(?:0|[1-9]\d*)\.\d{2}$"}
REVISION = {"type": "string", "pattern": r"^[0-9a-f]{64}$"}
ACCOUNT_REF = {"type": "string", "pattern": r"^acct-[0-9a-f]{10}$"}
DEFINITION_KINDS = ("all", "budget", "schedule", "preference", "account", "category", "merchant")
EVENT_TYPES = ("income", "bill", "card_payment", "transfer")
FREQUENCIES = ("once", "weekly", "biweekly", "semimonthly", "monthly", "quarterly", "yearly")
RECONCILIATION_STATUSES = (
    "matched",
    "amount_difference",
    "date_difference",
    "amount_and_date_difference",
    "ambiguous",
    "missing",
    "coverage_gap",
    "pending",
)
PLANNING_PRIVACY_STATEMENT = (
    "References and revisions are salted opaque values. Private labels, notes, raw account "
    "identities, descriptions, and paths are withheld. Exact lookup text is caller supplied "
    "and never echoed. Multiple matches require explicit disambiguation before filing intent. "
    "matched_count counts only the returned page; follow has_more to complete selection."
)
RECONCILIATION_STATEMENT = (
    "Confirmed expectations are compared with imported activity in the explicit period as of "
    "the requested date. Approximate matches never suppress cash-plan occurrences. Missing "
    "requires precise account statement coverage of the entire passed posting window; "
    "absence of rows alone is insufficient. Historical expectation revisions are not the "
    "current definition revision required to file a planning proposal. Matching uses a "
    "three-day posting window and a twenty-percent amount tolerance capped at CAD 100, "
    "with a one-cent minimum. Evidence is bounded to twenty ids per finding, with explicit "
    "evidence_count and evidence_truncated disclosure."
)
RECONCILIATION_ASSUMPTIONS = (
    "cad_only",
    "posting_window_three_days",
    "amount_tolerance_twenty_percent_capped_100_cad",
    "single_event_requires_merchant_or_pattern",
    "no_event_reuse",
    "missing_requires_complete_statement_window",
    "legacy_history_incomplete",
    "diagnostic_matches_do_not_suppress_cash_plan",
)


def nullable(schema: Mapping[str, object]) -> dict[str, object]:
    return {"oneOf": [dict(schema), {"type": "null"}]}


def definition_ref(kind: str) -> dict[str, object]:
    return {"type": "string", "pattern": rf"^{kind}-[0-9a-f]{{40}}$"}


def reference_fields(kind: str) -> dict[str, object]:
    return {
        "kind": {"const": kind},
        "ref": ACCOUNT_REF if kind == "account" else definition_ref(kind),
        "revision": REVISION,
    }


DEFINITION_ENTRY_SCHEMA = {
    "oneOf": [
        obj(
            {
                **reference_fields("budget"),
                "category_ref": definition_ref("category"),
                "budget_month": {"type": "string", "pattern": r"^(?:\*|\d{4}-(?:0[1-9]|1[0-2]))$"},
                "amount": MONEY,
                "currency": {"const": "CAD"},
            }
        ),
        obj(
            {
                **reference_fields("schedule"),
                "event_type": {"enum": list(EVENT_TYPES), "type": "string"},
                "amount": MONEY,
                "currency": {"const": "CAD"},
                "account_ref": nullable(ACCOUNT_REF),
                "counterparty_account_ref": nullable(ACCOUNT_REF),
                "next_date": DATE,
                "frequency": {"enum": list(FREQUENCIES), "type": "string"},
                "provenance": {"enum": ["confirmed", "hypothetical"], "type": "string"},
                "is_active": BOOLEAN,
            }
        ),
        obj(
            {
                **reference_fields("preference"),
                "scope": {"enum": ["category", "merchant"], "type": "string"},
                "subject_ref": {"oneOf": [definition_ref("category"), definition_ref("merchant")]},
                "disposition": {
                    "enum": ["intentional", "necessary", "worthwhile"],
                    "type": "string",
                },
            }
        ),
        obj({**reference_fields("account"), "is_credit_card": BOOLEAN}),
        obj(
            {**reference_fields("category"), "exclude_from_spending": BOOLEAN, "is_parent": BOOLEAN}
        ),
        obj(reference_fields("merchant")),
    ]
}


def disclosure_fields(semantics: str, *, withheld: bool) -> dict[str, object]:
    return {
        "account_ref_semantics": {"const": semantics},
        "account_refs": {"const": "withheld" if withheld else "disclosed"},
        "account_refs_withheld_reason": {"const": "account_ref_salt_absent" if withheld else None},
    }


def unavailable_schema(semantics: str) -> dict[str, object]:
    return obj(
        {
            **disclosure_fields(semantics, withheld=True),
            "status": {"const": "unavailable"},
            "unavailable_reason": {"const": "account_ref_salt_absent"},
        }
    )


def definitions_data_schema(semantics: str) -> dict[str, object]:
    return {
        "oneOf": [
            unavailable_schema(semantics),
            obj(
                {
                    **disclosure_fields(semantics, withheld=False),
                    "status": {"const": "ready"},
                    "kind": {"enum": list(DEFINITION_KINDS), "type": "string"},
                    "selection_status": {
                        "enum": ["all", "matched", "unmatched", "ambiguous"],
                        "type": "string",
                    },
                    "matched_count": {
                        **COUNT,
                        "description": (
                            "Number of definitions returned on this page. This page-local count "
                            "may vary with page size; sum it only after completing the cursor walk."
                        ),
                    },
                    "privacy_statement": {"const": PLANNING_PRIVACY_STATEMENT},
                    "entries": {"type": "array", "items": DEFINITION_ENTRY_SCHEMA, "maxItems": 100},
                }
            ),
        ]
    }


def reconciliation_data_schema(
    semantics: str,
    signed_money_schema: Mapping[str, object],
    whole_answer_count_description: str,
) -> dict[str, object]:
    intervals = {
        "type": "array",
        "items": obj({"start_date": DATE, "end_date": DATE}),
        "maxItems": 100,
    }
    coverage = obj(
        {
            "complete": BOOLEAN,
            "window_start": DATE,
            "window_end": DATE,
            "accounts": {
                "type": "array",
                "maxItems": 2,
                "items": obj(
                    {
                        "account_ref": nullable(ACCOUNT_REF),
                        "complete": BOOLEAN,
                        "intervals": intervals,
                    }
                ),
            },
        }
    )
    finding = obj(
        {
            "occurrence_ref": definition_ref("occurrence"),
            "schedule_ref": definition_ref("schedule"),
            "schedule_revision": REVISION,
            "material_revision": REVISION,
            "expected_date": DATE,
            "expected_amount": MONEY,
            "event_type": {"enum": list(EVENT_TYPES), "type": "string"},
            "currency": {"const": "CAD"},
            "account_ref": nullable(ACCOUNT_REF),
            "counterparty_account_ref": nullable(ACCOUNT_REF),
            "status": {"enum": list(RECONCILIATION_STATUSES), "type": "string"},
            "actual_date": nullable(DATE),
            "actual_amount": nullable(MONEY),
            "amount_difference": nullable(signed_money_schema),
            "date_difference": {
                "oneOf": [{"type": "integer", "minimum": -3, "maximum": 3}, {"type": "null"}]
            },
            "amount_changed": BOOLEAN,
            "date_changed": BOOLEAN,
            "evidence_ids": {
                "type": "array",
                "items": {"type": "string", "pattern": r"^[0-9a-f]{40}$"},
                "maxItems": 20,
            },
            "evidence_count": COUNT,
            "evidence_truncated": BOOLEAN,
            "candidate_count": COUNT,
            "matching_basis": {
                "type": "string",
                "enum": [
                    "ambiguous_candidates",
                    "no_candidate",
                    "reciprocal_transfer_pair",
                    "linked_pattern",
                    "normalized_merchant",
                ],
            },
            "reviewed": BOOLEAN,
            "actionable": BOOLEAN,
            "coverage": coverage,
            "history_complete": BOOLEAN,
        }
    )
    return {
        "oneOf": [
            unavailable_schema(semantics),
            obj(
                {
                    **disclosure_fields(semantics, withheld=False),
                    "status": {"const": "ready"},
                    "period": obj({"start_date": DATE, "end_date": DATE}),
                    "as_of": DATE,
                    "currency": {"const": "CAD"},
                    "posting_window_days": {"const": 3},
                    "total_count": {
                        **COUNT,
                        "description": "Confirmed occurrences in the requested period. "
                        + whole_answer_count_description,
                    },
                    "returned_count": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                        "description": (
                            "Number of findings returned on this page. This page-local count "
                            "may vary with page size; sum it only after completing the cursor walk."
                        ),
                    },
                    "findings": {"type": "array", "items": finding, "maxItems": 100},
                    "hypothetical_occurrence_count": {
                        **COUNT,
                        "description": "Hypothetical occurrences in the requested period. "
                        + whole_answer_count_description,
                    },
                    "detected_pattern_count": {
                        **COUNT,
                        "description": (
                            "Active detected patterns in the pinned database, independent of "
                            "the requested occurrence period. "
                        )
                        + whole_answer_count_description,
                    },
                    "incomplete_history_schedule_count": {
                        **COUNT,
                        "description": (
                            "Schedules with incomplete history considered for this comparison, "
                            "including its adjacent matching boundary. "
                        )
                        + whole_answer_count_description,
                    },
                    "assumptions": {
                        "type": "array",
                        "items": {"enum": list(RECONCILIATION_ASSUMPTIONS), "type": "string"},
                        "maxItems": len(RECONCILIATION_ASSUMPTIONS),
                        "uniqueItems": True,
                    },
                    "reconciliation_statement": {"const": RECONCILIATION_STATEMENT},
                }
            ),
        ]
    }
