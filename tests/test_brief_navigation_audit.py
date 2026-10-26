"""Synthetic AppTest regressions for Financial Brief supporting-detail links."""

from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs

import pytest
from streamlit.testing.v1 import AppTest
from test_financial_brief import _insert_transaction

from ledger.config.repository import seed_config_database
from ledger.insights.service import InsightEngine, StaleFindingError
from ledger.reporting.brief import FinancialBriefService
from ledger.storage.database import (
    CategoryConfigDB,
    InsightFindingDB,
    clear_engine_cache,
    session_scope,
)
from ledger.storage.migrations import ensure_schema
from ledger.storage.snapshots import database_target_lock, publish_database

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def brief_db(tmp_path: Path, monkeypatch) -> Path:
    db = tmp_path / "ledger.db"
    ensure_schema(db)
    seed_config_database(
        db, REPO_ROOT / "configs/merchants.toml", REPO_ROOT / "configs/categories.toml"
    )
    with session_scope(db) as session:
        session.add_all(
            [
                CategoryConfigDB(name="Devices", parent=None, sort_order=100),
                CategoryConfigDB(name="Gadgets", parent="Devices", sort_order=101),
                CategoryConfigDB(name="Synthetic Rent", parent=None, sort_order=102),
            ]
        )
    for label, on, amount, merchant, category in (
        ("old", date(2026, 1, 1), "-100", "RENT", "Synthetic Rent"),
        ("gadget", date(2026, 8, 10), "-30", "DEVICE SHOP", "Gadgets"),
        ("rent", date(2026, 8, 20), "-1000", "RENT", "Synthetic Rent"),
        ("new", date(2026, 9, 4), "-100", "RENT", "Synthetic Rent"),
    ):
        _insert_transaction(db, label, on, amount, merchant, category)
    monkeypatch.setenv("LEDGER_DB", str(db))
    return db


def _brief_app(brief: dict) -> None:
    import streamlit as st

    from ledger.ui.context import build_app_context
    from ledger.ui.financial_brief import render_financial_brief
    from ledger.ui.pages import categories, insights

    ctx = build_app_context()

    def dates() -> None:
        st.write(f"Selected dates: {ctx.start_date} through {ctx.end_date}")

    pages = [
        st.Page(lambda: render_financial_brief(brief), title="Brief", default=True),
        st.Page(dates, title="Overview", url_path="overview"),
        st.Page(lambda: categories.render(ctx), title="Categories", url_path="categories"),
        st.Page(lambda: insights.render(ctx), title="Insights", url_path="insights"),
    ]
    st.session_state["ledger_pages"] = {page.url_path: page for page in pages}
    st.navigation(pages).run()


def _app(
    *, path: str = "/overview", period: tuple[str, str] = ("2026-01-01", "2026-09-04"), **subject
) -> AppTest:
    brief = {
        "period": {"start": period[0], "end": period[1]},
        "observations": [
            {
                "rank": 1,
                "label": "arithmetic",
                "title": "Synthetic observation",
                "statement": "Synthetic supporting evidence.",
                "dashboard_path": path,
                **subject,
            }
        ],
    }
    return AppTest.from_function(_brief_app, args=(brief,), default_timeout=30)


def _follow_supporting_link(at: AppTest, *, page: str | None = None) -> None:
    links = at.get("page_link")
    if page is not None:
        links = [link for link in links if link.proto.page == page]
    assert len(links) == 1, "Brief must render a native Streamlit internal link"
    link = links[0].proto
    assert not link.external
    # AppTest has no page_link click adapter. Replay the actual native link's
    # page hash and query string, as the browser does, into its script runner.
    at._page_hash = link.page_script_hash
    at.query_params = parse_qs(link.query_string)
    at.run()
    assert not at.exception


def test_brief_uses_native_link_and_preserves_all_time(brief_db: Path) -> None:
    at = _app().run()
    assert not at.exception
    at.selectbox(key="period_preset").select("All Time").run()
    _follow_supporting_link(at)
    assert at.selectbox(key="period_preset").value == "All Time"
    assert "Selected dates: 2026-01-01 through 2026-09-04" in [row.value for row in at.markdown]


def test_historical_brief_restores_its_dates_before_sidebar_controls(brief_db: Path) -> None:
    at = _app(period=("2026-08-01", "2026-08-31")).run()
    at.selectbox(key="period_preset").select("All Time").run()
    _follow_supporting_link(at)
    assert at.selectbox(key="period_preset").value == "Custom Range"
    assert at.date_input(key="custom_start_date").value == date(2026, 8, 1)
    assert at.date_input(key="custom_end_date").value == date(2026, 8, 31)
    at.selectbox(key="period_preset").select("All Time").run()
    assert "Selected dates: 2026-01-01 through 2026-09-04" in [row.value for row in at.markdown]
    assert not at.query_params, "Consumed context must not override later period choices"


def test_category_link_selects_the_observed_child_not_top_parent(brief_db: Path) -> None:
    at = _app(
        path="/categories",
        period=("2026-08-01", "2026-08-31"),
        subject_kind="category",
        subject="Gadgets",
    ).run()
    _follow_supporting_link(at)
    assert at.selectbox(key="selected_category").value == "Gadgets"
    assert at.segmented_control[0].value == "Category"
    assert len(at.dataframe[-1].value) == 1


def test_merchant_link_selects_the_observed_merchant(brief_db: Path) -> None:
    at = _app(
        path="/categories",
        period=("2026-08-01", "2026-08-31"),
        subject_kind="merchant",
        subject="DEVICE SHOP",
    ).run()
    _follow_supporting_link(at)
    assert at.segmented_control[0].value == "Merchant"
    assert at.selectbox(key="selected_merchant").value == "DEVICE SHOP"
    assert len(at.dataframe[-1].value) == 1


def test_brief_date_window_is_not_clipped_to_transaction_bounds(brief_db: Path) -> None:
    at = _app(period=("2025-12-01", "2026-09-30")).run()
    _follow_supporting_link(at)
    assert "Selected dates: 2025-12-01 through 2026-09-30" in [row.value for row in at.markdown]
    at.run()
    assert not at.exception
    assert at.date_input(key="custom_end_date").value == date(2026, 9, 30)


@pytest.mark.parametrize(
    ("path", "period", "subject_kind"),
    [
        ("https://example.com/", ("2026-08-01", "2026-08-31"), "none"),
        ("/categories", ("invalid", "2026-08-31"), "category"),
        ("/categories", ("2026-08-31", "2026-08-01"), "category"),
    ],
)
def test_invalid_brief_context_does_not_render_a_destination(
    brief_db: Path,
    path: str,
    period: tuple[str, str],
    subject_kind: str,
) -> None:
    at = _app(path=path, period=period, subject_kind=subject_kind, subject="Gadgets").run()
    assert not at.exception
    assert not at.get("page_link")


def test_category_link_does_not_substitute_for_a_missing_subject(brief_db: Path) -> None:
    at = _app(path="/categories", subject_kind="category", subject="Unavailable category").run()
    _follow_supporting_link(at)
    assert not any(row.label == "Category" for row in at.selectbox)
    assert any("no longer available" in row.value for row in at.info)


def test_merchant_link_does_not_substitute_for_a_missing_subject(brief_db: Path) -> None:
    at = _app(path="/categories", subject_kind="merchant", subject="Unavailable merchant").run()
    _follow_supporting_link(at)
    assert not any(row.label == "Merchant" for row in at.selectbox)
    assert any("no longer available" in row.value for row in at.info)


def test_merchant_context_without_current_spend_survives_page_rerun(brief_db: Path) -> None:
    at = _app(
        path="/categories",
        period=("2026-09-01", "2026-09-04"),
        subject_kind="merchant",
        subject="DEVICE SHOP",
    ).run()
    _follow_supporting_link(at)
    assert at.selectbox(key="selected_merchant").value == "DEVICE SHOP"
    at.run()
    assert not at.exception
    assert at.selectbox(key="selected_merchant").value == "DEVICE SHOP"


def test_malformed_subject_kind_does_not_crash_brief_render(brief_db: Path) -> None:
    at = _app(subject_kind={"unexpected": "category"}, subject="Gadgets").run()
    assert not at.exception
    _follow_supporting_link(at)


def test_brief_token_cannot_restore_context_after_database_switch(
    brief_db: Path, tmp_path: Path
) -> None:
    at = _app(period=("2026-08-01", "2026-08-31")).run()
    link = at.get("page_link")[0].proto
    replacement = tmp_path / "other.db"
    ensure_schema(replacement)
    _insert_transaction(replacement, "other", date(2026, 6, 3), "-20", "OTHER", "Other")
    next(row for row in at.text_input if row.label == "Database").set_value(str(replacement))
    at._page_hash = link.page_script_hash
    at.query_params = parse_qs(link.query_string)
    at.run()
    assert not at.exception
    assert at.selectbox(key="period_preset").value == "This Month"
    assert "Selected dates: 2026-06-03 through 2026-06-03" in [row.value for row in at.markdown]


def _add_findings(db: Path, *, period_end: date = date(2026, 8, 31)) -> None:
    with session_scope(db) as session:
        for index, evidence_id in enumerate(("a" * 40, "b" * 40)):
            session.add(
                InsightFindingDB(
                    fingerprint=f"navigation-{index}",
                    period_start=date(2026, 8, 1),
                    period_end=period_end,
                    finding_type="anomalies",
                    severity="medium",
                    title=f"Synthetic finding {index}",
                    explanation=f"Evidence explanation {index}",
                    evidence={
                        "category": "Gadgets",
                        "merchant": "DEVICE SHOP",
                        "txn_id": evidence_id,
                    },
                    suggested_action="Review source evidence",
                    status="open",
                    created_at=datetime(2026, 9, 1),
                )
            )


def test_insight_link_opens_matching_evidence_instead_of_default_priority_review(
    brief_db: Path,
) -> None:
    _add_findings(brief_db)
    at = _app(
        path="/insights",
        period=("2026-08-01", "2026-08-31"),
        subject_kind="category",
        subject="Gadgets",
        evidence_ids=["b" * 40],
    ).run()
    link = at.get("page_link")[0].proto
    assert "Gadgets" not in link.query_string and "b" * 40 not in link.query_string
    _follow_supporting_link(at)
    assert [row.label for row in at.expander] == ["Medium - Synthetic finding 1"]
    assert at.expander[0].proto.expanded


def test_insight_link_does_not_substitute_another_stored_period(brief_db: Path) -> None:
    _add_findings(brief_db, period_end=date(2026, 8, 30))
    at = _app(
        path="/insights",
        period=("2026-08-01", "2026-08-31"),
        subject_kind="category",
        subject="Gadgets",
        evidence_ids=["b" * 40],
    ).run()
    _follow_supporting_link(at)
    assert not at.expander
    assert any("brief's exact period" in row.value for row in at.info)


def _focused_insights() -> AppTest:
    at = _app(
        path="/insights",
        period=("2026-08-01", "2026-08-31"),
        subject_kind="category",
        subject="Gadgets",
        evidence_ids=["b" * 40],
    ).run()
    _follow_supporting_link(at)
    return at


def _finding_statuses(db: Path) -> dict[str, str]:
    with session_scope(db) as session:
        return {row.fingerprint: row.status for row in session.query(InsightFindingDB).all()}


@pytest.mark.parametrize(
    ("button_label", "status"),
    [("Watch", "watch"), ("Ignore", "ignored"), ("Mark reviewed", "reviewed")],
)
def test_focused_finding_actions_update_the_displayed_finding(
    brief_db: Path, button_label: str, status: str
) -> None:
    _add_findings(brief_db)
    at = _focused_insights()
    next(row for row in at.button if row.label == button_label).click().run()
    assert not at.exception
    assert _finding_statuses(brief_db) == {"navigation-0": "open", "navigation-1": status}
    assert at.selectbox(key="period_preset").value == "Custom Range"
    assert at.date_input(key="custom_start_date").value == date(2026, 8, 1)


@pytest.mark.parametrize("exit_focus", ["page", "period", "database", "browse"])
def test_focused_findings_expire_without_changing_status(
    brief_db: Path, tmp_path: Path, exit_focus: str
) -> None:
    _add_findings(brief_db)
    at = _focused_insights()
    assert any(row.value == "Supporting findings from the brief" for row in at.subheader)
    if exit_focus == "page":
        insights_hash = at._page_hash
        at._page_hash = at.session_state["ledger_pages"]["overview"]._script_hash
        at.run()
        assert not at.exception
        at._page_hash = insights_hash
        at.run()
    elif exit_focus == "period":
        at.selectbox(key="period_preset").select("All Time").run()
    elif exit_focus == "database":
        other = tmp_path / "other.db"
        ensure_schema(other)
        _insert_transaction(other, "other", date(2026, 8, 31), "-20", "OTHER", "Other")
        _add_findings(other)
        next(row for row in at.text_input if row.label == "Database").set_value(str(other)).run()
        assert _finding_statuses(other) == {"navigation-0": "open", "navigation-1": "open"}
    else:
        next(row for row in at.button if row.label == "Browse all insights").click().run()
    assert not at.exception
    assert not any(row.value == "Supporting findings from the brief" for row in at.subheader)
    assert not any(row.label == "Watch" for row in at.button)
    assert _finding_statuses(brief_db) == {"navigation-0": "open", "navigation-1": "open"}


def test_service_generated_subjectless_quality_brief_opens_its_period_findings(
    brief_db: Path,
) -> None:
    with session_scope(brief_db) as session:
        session.add(
            InsightFindingDB(
                fingerprint="quality-navigation",
                period_start=date(2026, 8, 1),
                period_end=date(2026, 9, 4),
                finding_type="quality",
                severity="medium",
                title="Synthetic quality finding",
                explanation="Synthetic quality explanation",
                evidence={},
                suggested_action="Review source evidence",
                status="open",
                created_at=datetime(2026, 9, 4),
            )
        )
    brief = FinancialBriefService(brief_db).build(date(2026, 8, 1), date(2026, 9, 4))
    observation = next(row for row in brief["observations"] if row["dashboard_path"] == "/insights")
    assert observation["subject_kind"] == "none"
    assert observation["subject"] is None
    assert observation["evidence_ids"] == []
    at = AppTest.from_function(_brief_app, args=(brief,), default_timeout=30).run()
    assert not at.exception
    _follow_supporting_link(at, page="insights")
    assert [row.label for row in at.expander] == ["Medium - Synthetic quality finding"]
    assert at.expander[0].proto.expanded


@pytest.mark.parametrize("button_label", ["Watch", "Ignore", "Mark reviewed"])
@pytest.mark.parametrize("reuse_fingerprints", [False, True])
def test_stale_finding_action_refuses_replaced_database(
    brief_db: Path, tmp_path: Path, button_label: str, reuse_fingerprints: bool
) -> None:
    _add_findings(brief_db)
    at = _focused_insights()
    candidate = tmp_path / "candidate.db"
    ensure_schema(candidate)
    _insert_transaction(candidate, "replacement", date(2026, 8, 31), "-20", "OTHER", "Other")
    _add_findings(candidate)
    if not reuse_fingerprints:
        with session_scope(candidate) as session:
            for row in session.query(InsightFindingDB).all():
                row.fingerprint = f"different-{row.id}"
                row.title = "Unrelated replacement finding"
                row.evidence = {"category": "Other"}
    expected_statuses = _finding_statuses(candidate)
    os.replace(candidate, brief_db)

    next(row for row in at.button if row.label == button_label).click().run()

    assert not at.exception
    assert _finding_statuses(brief_db) == expected_statuses
    assert any("changed since" in row.value for row in at.warning)


@pytest.mark.parametrize(
    "changed_field",
    [
        "fingerprint",
        "period_start",
        "period_end",
        "severity",
        "title",
        "explanation",
        "evidence",
        "suggested_action",
        "status",
    ],
)
def test_stale_finding_action_refuses_changed_record_in_same_database(
    brief_db: Path, changed_field: str
) -> None:
    _add_findings(brief_db)
    at = _focused_insights()
    with session_scope(brief_db) as session:
        row = session.query(InsightFindingDB).filter_by(fingerprint="navigation-1").one()
        if changed_field == "fingerprint":
            row.fingerprint = "different-finding"
        elif changed_field == "period_start":
            row.period_start = date(2026, 7, 1)
        elif changed_field == "period_end":
            row.period_end = date(2026, 9, 4)
        else:
            setattr(
                row,
                changed_field,
                {
                    "severity": "high",
                    "title": "Updated finding",
                    "explanation": "New material evidence",
                    "evidence": {"category": "Gadgets", "change": 1500},
                    "suggested_action": "Review the updated evidence",
                    "status": "ignored",
                }[changed_field],
            )
    expected_statuses = _finding_statuses(brief_db)

    next(row for row in at.button if row.label == "Watch").click().run()

    assert not at.exception
    assert _finding_statuses(brief_db) == expected_statuses
    assert any("changed since" in row.value for row in at.warning)


@pytest.mark.parametrize("status", ["watch", "ignored", "reviewed"])
def test_stale_finding_action_refuses_in_place_publication(tmp_path: Path, status: str) -> None:
    live, candidate = tmp_path / "live.db", tmp_path / "candidate.db"
    start, end = date(2026, 8, 1), date(2026, 8, 31)
    for db, change in ((live, 150), (candidate, 1500)):
        ensure_schema(db)
        engine = InsightEngine(db, prepare_schema=False)
        finding = engine._category_drift(
            {"category_drift": [{"category": "Synthetic Bills", "change": change}]}
        )[0]
        with session_scope(db) as session:
            session.add(
                InsightFindingDB(
                    fingerprint=finding.fingerprint(start, end),
                    period_start=start,
                    period_end=end,
                    finding_type=finding.finding_type,
                    severity=finding.severity,
                    title=finding.title,
                    explanation=finding.explanation,
                    evidence=finding.evidence,
                    suggested_action=finding.suggested_action,
                    status="open",
                    created_at=datetime(2026, 9, 4),
                )
            )
    engine = InsightEngine(live, prepare_schema=False)
    displayed = engine.list_findings(start, end, include_action_targets=True)[0]
    old_inode = live.stat().st_ino
    clear_engine_cache()
    with database_target_lock(live):
        publish_database(candidate, live)
    assert live.stat().st_ino == old_inode
    current = engine.list_findings(start, end, include_action_targets=True)[0]
    assert current["fingerprint"] == displayed["fingerprint"]
    assert (displayed["severity"], current["severity"]) == ("low", "high")

    with pytest.raises(StaleFindingError, match="changed since"):
        engine.update_status(displayed["id"], status, expected=displayed["_action_target"])
    assert engine.list_findings(start, end)[0]["status"] == "open"

    engine.update_status(current["id"], status, expected=current["_action_target"])
    assert engine.list_findings(start, end, include_closed=True)[0]["status"] == status
