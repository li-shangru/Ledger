"""One-shot, session-local context for Financial Brief internal navigation."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import streamlit as st

_QUERY_KEY = "brief"
_LINKS_KEY = "ledger_brief_links"
_PENDING_KEY = "ledger_brief_context"
_DATABASE_KEY = "ledger_brief_database"


@dataclass(frozen=True)
class BriefContext:
    page: str
    start_date: date
    end_date: date
    subject_kind: str
    subject: str | None
    evidence_ids: tuple[str, ...]
    as_of: date | None = None


def brief_context(period: dict[str, Any], observation: dict[str, Any]) -> BriefContext | None:
    """Accept a bounded local destination and a valid concrete brief date window."""
    path = observation.get("dashboard_path")
    if not isinstance(path, str) or path not in {
        "/overview",
        "/categories",
        "/insights",
        "/transactions",
        "/accounts",
        "/import-center",
        "/plan-reconciliation",
        "/proposals",
    }:
        return None
    try:
        start = date.fromisoformat(period["start"])
        end = date.fromisoformat(period["end"])
    except KeyError, TypeError, ValueError:
        return None
    if start > end:
        return None
    kind = observation.get("subject_kind")
    subject = observation.get("subject")
    if (
        not isinstance(kind, str)
        or kind not in {"category", "merchant", "institution", "occurrence", "proposal"}
        or not isinstance(subject, str)
        or not subject.strip()
        or len(subject) > 240
    ):
        kind, subject = "none", None
    evidence = observation.get("evidence_ids")
    evidence_ids = (
        tuple(value for value in evidence[:20] if isinstance(value, str) and 0 < len(value) <= 200)
        if isinstance(evidence, list)
        else ()
    )
    try:
        as_of = date.fromisoformat(observation["as_of"])
    except KeyError, TypeError, ValueError:
        as_of = None
    return BriefContext(path[1:], start, end, str(kind), subject, evidence_ids, as_of)


def register_brief_link(context: BriefContext) -> dict[str, str]:
    """Keep subject/evidence details in this session, out of browser URLs."""
    token = secrets.token_urlsafe(18)
    links = st.session_state.setdefault(_LINKS_KEY, {})
    links[token] = (st.session_state.get(_DATABASE_KEY), context)
    return {_QUERY_KEY: token}


def consume_brief_navigation(db_path: Path) -> BriefContext | None:
    """Consume only a link emitted in the previous run for the same database.

    Every unrelated rerun expires old links and pending subject context. A token
    copied to another session cannot resolve, and database switches/replacements
    discard both the handoff and its previous widget selections.
    """
    stat = db_path.stat() if db_path.is_file() else None
    database = (str(db_path.resolve()), (stat.st_dev, stat.st_ino) if stat else None)
    previous_database = st.session_state.get(_DATABASE_KEY)
    st.session_state[_DATABASE_KEY] = database
    st.session_state.pop(_PENDING_KEY, None)
    links = st.session_state.pop(_LINKS_KEY, {})
    token = st.query_params.get(_QUERY_KEY)
    if _QUERY_KEY in st.query_params:
        del st.query_params[_QUERY_KEY]
    if previous_database is not None and previous_database != database:
        for key in (
            "period_preset",
            "custom_start_date",
            "custom_end_date",
            "selected_category",
            "selected_merchant",
            "categories_analysis",
            "insight_view",
            "plan_reconciliation_selected",
            "plan_reconciliation_as_of",
            "plan_reconciliation_page",
            "plan_reconciliation_period",
            "plan_include_reviewed",
            "plan_review_outcome",
            "planning_proposal_selected",
            "planning_preview",
            "planning_evidence_context",
        ):
            st.session_state.pop(key, None)
        return None
    if not isinstance(token, str) or token not in links:
        return None
    source_database, context = links[token]
    if source_database != database:
        return None
    st.session_state[_PENDING_KEY] = context
    return context


def take_brief_context(page: str) -> BriefContext | None:
    context = st.session_state.pop(_PENDING_KEY, None)
    return context if isinstance(context, BriefContext) and context.page == page else None


def supporting_findings(context: BriefContext, findings: list[dict]) -> list[dict]:
    """Match the brief's subject and transaction references, without title parsing."""
    matches = []
    for finding in findings:
        evidence = finding.get("evidence") or {}
        if context.subject and evidence.get(context.subject_kind) != context.subject:
            continue
        ids = {
            evidence.get(key)
            for key in ("txn_id", "related_txn_id", "evidence_id", "related_evidence_id")
            if isinstance(evidence.get(key), str)
        }
        if context.evidence_ids and not set(context.evidence_ids).issubset(ids):
            continue
        matches.append(finding)
    return matches
