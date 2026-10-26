from __future__ import annotations

import os
import subprocess
import sys
from datetime import date
from pathlib import Path

from ledger.planning.reconciliation import PlanReconciliationService
from ledger.ui import smoke


def test_route_manifest_has_unique_paths_and_expected_labels() -> None:
    routes = smoke.SMOKE_ROUTES

    assert [route.label for route in routes] == [
        "Import Center",
        "Review Queue",
        "Transactions",
        "Overview",
        "Categories",
        "Accounts",
        "Investments",
        "Insights",
        "Budgets / Goals",
        "Plan versus actual",
        "Proposals",
        "Rules / Settings",
        "Admin / Data",
    ]
    assert len({route.path for route in routes}) == len(routes)
    assert smoke.OVERVIEW_ROUTE.path == "/overview"
    assert {route.label for route in routes if route.expects_status_grid} == {
        "Transactions",
        "Overview",
        "Categories",
        "Accounts",
        "Insights",
        "Budgets / Goals",
        "Admin / Data",
    }


def test_detect_banned_tokens_flags_tracebacks_and_page_not_found() -> None:
    body_text = "Overview\nTraceback\nPage not found\nStreamlitAPIException"

    assert smoke.detect_banned_tokens(body_text) == [
        "Traceback",
        "StreamlitAPIException",
        "Page not found",
    ]


def test_status_cell_fit_helper_detects_horizontal_overflow() -> None:
    cells = [
        {"text": "Period 2026-05-01 to 2026-05-07", "width": 240, "scrollWidth": 240},
        {"text": "Source Snapshot + live queries", "width": 180, "scrollWidth": 208},
    ]

    assert not smoke.status_cells_fit(cells)


def test_console_filter_ignores_only_favicon_404() -> None:
    favicon_404 = {
        "type": "error",
        "text": "Failed to load resource: the server responded with a status of 404 (Not Found)",
        "url": "http://localhost:8502/favicon.ico",
    }
    missing_chart = {
        "type": "error",
        "text": "Failed to load resource: the server responded with a status of 404 (Not Found)",
        "url": "http://localhost:8502/static/chart.js",
    }

    assert not smoke.console_message_is_relevant(favicon_404)
    assert smoke.console_message_is_relevant(missing_chart)


def test_console_filter_ignores_streamlit_nested_route_probe_404s() -> None:
    health_probe = {
        "type": "error",
        "text": "Failed to load resource: the server responded with a status of 404 (Not Found)",
        "url": "http://localhost:8502/budgets-goals/_stcore/health",
    }
    host_config_probe = {
        "type": "error",
        "text": "Failed to load resource: the server responded with a status of 404 (Not Found)",
        "url": "http://localhost:8502/budgets-goals/_stcore/host-config",
    }

    assert not smoke.console_message_is_relevant(health_probe)
    assert not smoke.console_message_is_relevant(host_config_probe)


def test_console_filter_ignores_streamlit_metrics_fetch_noise() -> None:
    metrics_404 = {
        "type": "error",
        "text": "Failed to load resource: net::ERR_NAME_NOT_RESOLVED",
        "url": "https://data.streamlit.io/metrics.json",
    }
    metrics_config = {
        "type": "error",
        "text": "Failed to fetch metrics config: TypeError: Failed to fetch",
        "url": "http://localhost:8502/static/js/index.js",
    }
    deactivated = {
        "type": "error",
        "text": "Undefined metrics config - deactivating metrics tracking.",
        "url": "http://localhost:8502/static/js/index.js",
    }

    assert not smoke.console_message_is_relevant(metrics_404)
    assert not smoke.console_message_is_relevant(metrics_config)
    assert not smoke.console_message_is_relevant(deactivated)


def test_smoke_refuses_to_click_mutating_controls() -> None:
    for label in (
        "Save Goals",
        "Finalize import",
        "Watch",
        "Ignore",
        "Mark reviewed",
        "Refresh Insights",
        "Add or update preference",
        "Save changes",
        "Remove preference",
        "Add Schedule",
        "Save Schedule",
        "Confirm Selected Schedule",
        "Delete Selected Schedule",
        "Confirm Detected Pattern",
        "Review unchanged exception",
        "Update future schedule",
        "Apply reviewed proposal",
        "Reject proposal",
        "Undo applied change",
        "Recover reviewed application",
    ):
        try:
            smoke.ensure_read_only_click(label)
        except ValueError as exc:
            assert "mutating control" in str(exc)
        else:
            raise AssertionError(f"{label} should be blocked from smoke clicks")

    smoke.ensure_read_only_click("Goals")


def test_ui_smoke_evidence_text_expectations_are_non_mutating() -> None:
    assert "Ledger Evidence" in smoke.ADMIN_EVIDENCE_TEXT
    assert "STATEMENT AUDIT" in smoke.ADMIN_EVIDENCE_TEXT
    assert "Evidence" in smoke.OVERVIEW_EVIDENCE_TEXT
    assert smoke.ROUTE_TEXT_EXPECTATIONS["Insights"] == ("Priority Review",)
    assert set(smoke.PRIORITY_REVIEW_EVIDENCE_TEXT) == {
        "Possible duplicate charge",
        "Recurring price increase possibility",
        "Unresolved reimbursement possibility",
    }
    smoke.ensure_read_only_click("Preferences")


def test_ui_smoke_script_help_exits_successfully() -> None:
    repo_root = Path(__file__).resolve().parents[1]

    result = subprocess.run(
        [str(repo_root / "scripts" / "ui-smoke"), "--help"],
        cwd=repo_root,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "Ledger UI smoke" in result.stdout


def test_synthetic_freeze_seeds_imported_reconciliation_evidence(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    source = (root / "scripts/freeze-check").read_text()
    fixture = source.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    db_path = tmp_path / "ledger.db"
    environment = {**os.environ, "PYTHONPATH": str(root / "tests")}
    result = subprocess.run(
        [sys.executable, "-", str(db_path)],
        input=fixture,
        text=True,
        capture_output=True,
        env=environment,
        cwd=root,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = PlanReconciliationService(db_path).build(
        date(2025, 8, 1), date(2025, 8, 31), as_of=date(2025, 8, 31)
    )
    finding = next(
        (row for row in report["findings"] if row["schedule_id"] == "ui-reconciliation-smoke"), None
    )
    assert finding is not None
    assert finding["status"] == "amount_and_date_difference"
    assert finding["expected_amount"] == "11.00"
    assert finding["actual_amount"] == "12.00"
    assert len(finding["evidence_ids"]) == 1


def test_synthetic_smoke_requires_reconciliation_interaction(monkeypatch) -> None:
    monkeypatch.setenv("LEDGER_FREEZE_SYNTHETIC", "1")
    assert smoke.build_parser().parse_args([]).reconciliation_fixture is True
    monkeypatch.delenv("LEDGER_FREEZE_SYNTHETIC")
    assert smoke.build_parser().parse_args([]).reconciliation_fixture is False
