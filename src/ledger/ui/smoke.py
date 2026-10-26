"""Repeatable browser smoke checks for the Streamlit dashboard."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from ledger.utils.private_files import ensure_private_dir, private_atomic_binary


@dataclass(frozen=True)
class SmokeRoute:
    label: str
    path: str
    expects_status_grid: bool = False


OVERVIEW_ROUTE = SmokeRoute("Overview", "/overview", expects_status_grid=True)
SMOKE_ROUTES = [
    SmokeRoute("Import Center", "/import-center"),
    SmokeRoute("Review Queue", "/review"),
    SmokeRoute("Transactions", "/transactions", expects_status_grid=True),
    OVERVIEW_ROUTE,
    SmokeRoute("Categories", "/categories", expects_status_grid=True),
    SmokeRoute("Accounts", "/accounts", expects_status_grid=True),
    SmokeRoute("Investments", "/investments"),
    SmokeRoute("Insights", "/insights", expects_status_grid=True),
    SmokeRoute("Budgets / Goals", "/budgets-goals", expects_status_grid=True),
    SmokeRoute("Plan versus actual", "/plan-reconciliation"),
    SmokeRoute("Proposals", "/proposals"),
    SmokeRoute("Rules / Settings", "/rules"),
    SmokeRoute("Admin / Data", "/admin-data", expects_status_grid=True),
]

BAD_TOKENS = [
    "Traceback",
    "TypeError",
    "NameError",
    "StreamlitAPIException",
    "Page not found",
]
MUTATING_BUTTON_LABELS = {
    "Create ledger",
    "Resolve complete visible rows",
    "Review complete visible rows as-is",
    "Save Review Decisions",
    "Save Budgets",
    "Save Goals",
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
    "Add PDFs to inbox",
    "Stage inbox PDFs",
    "Finalize import",
    "Resume finalize",
    "Cancel import run",
    "Apply Adjustments",
    "Execute Cleanup",
    "Create Export",
    "Save Merchant Profiles",
    "Save Category Taxonomy",
    "Import Seed TOML",
    "Export Runtime TOML",
}
OVERVIEW_EVIDENCE_TEXT = (
    "Evidence",
    "AUDIT",
    "QUALITY",
    "SNAPSHOT",
    "Financial Brief",
    "Explain this number",
)
PRIORITY_REVIEW_EVIDENCE_TEXT = (
    "Possible duplicate charge",
    "Recurring price increase possibility",
    "Unresolved reimbursement possibility",
)
ADMIN_EVIDENCE_TEXT = ("Ledger Evidence", "STATEMENT AUDIT", "REVIEW READINESS")
ROUTE_TEXT_EXPECTATIONS = {
    "Import Center": ("Add statements", "Stage inbox PDFs"),
    "Overview": OVERVIEW_EVIDENCE_TEXT,
    "Accounts": ("Explain this number",),
    "Insights": ("Priority Review",),
    "Budgets / Goals": ("Explain this number", "Cash Plan"),
    "Plan versus actual": ("As of", "Coverage and matching assumptions"),
    "Proposals": ("Proposal store", "reviewed preview"),
    "Admin / Data": ADMIN_EVIDENCE_TEXT,
}


def detect_banned_tokens(body_text: str) -> list[str]:
    return [token for token in BAD_TOKENS if token in body_text]


def status_cells_fit(cells: list[dict[str, Any]]) -> bool:
    return all(int(cell["scrollWidth"]) <= int(cell["width"]) + 2 for cell in cells)


def ensure_read_only_click(label: str) -> None:
    if label in MUTATING_BUTTON_LABELS:
        raise ValueError(f"UI smoke may not click mutating control: {label}")


def console_message_is_relevant(message: dict[str, str]) -> bool:
    if message["url"].endswith("data.streamlit.io/metrics.json"):
        return False
    if message["text"].startswith("Failed to fetch metrics config:"):
        return False
    if message["text"].startswith("Undefined metrics config - deactivating metrics tracking"):
        return False

    is_404 = (
        message["type"] == "error"
        and message["text"]
        == "Failed to load resource: the server responded with a status of 404 (Not Found)"
    )
    if is_404 and message["url"].endswith("/favicon.ico"):
        return False
    if is_404 and (
        message["url"].endswith("/_stcore/health")
        or message["url"].endswith("/_stcore/host-config")
    ):
        return False
    return message["type"] in {"error", "warning"}


def _route_url(base_url: str, route: SmokeRoute) -> str:
    return urljoin(base_url.rstrip("/") + "/", route.path.lstrip("/"))


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _wait_for_server(base_url: str, timeout_seconds: float) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(base_url, timeout=2):
                return
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            time.sleep(0.5)
    raise RuntimeError(f"Timed out waiting for {base_url}: {last_error}")


def _page_state(page) -> dict[str, Any]:
    return page.evaluate(
        """() => {
            const text = document.body ? document.body.innerText : "";
            const headings = Array.from(document.querySelectorAll("h1,h2,h3"))
                .map((el) => el.textContent?.trim())
                .filter(Boolean);
            const statusCells = Array.from(document.querySelectorAll(".ledger-status-cell"))
                .map((el) => {
                    const rect = el.getBoundingClientRect();
                    return {
                        text: el.textContent?.trim().replace(/\\s+/g, " "),
                        width: Math.round(rect.width),
                        scrollWidth: Math.round(el.scrollWidth),
                    };
                });
            const visibleOverflowing = Array.from(document.querySelectorAll("body *"))
                .filter((el) => {
                    const visibleText = el.textContent?.trim().replace(/\\s+/g, " ") || "";
                    if (!visibleText) {
                        return false;
                    }
                    const style = getComputedStyle(el);
                    if (style.visibility === "hidden" || style.display === "none" || Number(style.opacity) === 0) {
                        return false;
                    }
                    const rect = el.getBoundingClientRect();
                    if (rect.width <= 0 || rect.height <= 0) {
                        return false;
                    }
                    if (rect.right <= 0 || rect.left >= window.innerWidth) {
                        return false;
                    }
                    return rect.left < -2 || rect.right > window.innerWidth + 2;
                })
                .slice(0, 10)
                .map((el) => {
                    const rect = el.getBoundingClientRect();
                    return {
                        tag: el.tagName,
                        text: el.textContent?.trim().replace(/\\s+/g, " ").slice(0, 80),
                        left: Math.round(rect.left),
                        right: Math.round(rect.right),
                    };
                });
            return {
                title: document.title,
                textLength: text.trim().length,
                headings,
                statusCells,
                visibleOverflowing,
                bannedTokens: [],
                bodyText: text,
            };
        }"""
    )


def _body_text(page) -> str:
    return page.locator("body").inner_text()


def _wait_for_streamlit_idle(page, timeout: int = 15_000) -> None:
    """Best-effort wait until Streamlit settles its current script run.

    Streamlit renders incrementally, so a page heading can appear before the body
    content finishes. The app container exposes ``data-test-script-state``, which
    flips to ``notRunning`` once a rerun settles; waiting on it avoids racing the
    render when we read body text or take screenshots. Failures are non-fatal -- the
    text assertions that follow remain the source of truth."""
    with contextlib.suppress(Exception):
        page.wait_for_function(
            """() => {
                const app = document.querySelector('[data-testid="stApp"]');
                return app && app.getAttribute('data-test-script-state') === 'notRunning';
            }""",
            timeout=timeout,
        )


def _assert_body_has_any(page, options: tuple[str, ...], label: str) -> None:
    # Case-insensitive: the dashboard CSS renders KPI/metric labels with
    # `text-transform: uppercase`, and innerText reflects the transformed text. These
    # assertions check that a label is PRESENT, not how it is cased.
    try:
        page.wait_for_function(
            """(options) => {
                const text = (document.body ? document.body.innerText : "").toLowerCase();
                return options.some((item) => text.includes(item.toLowerCase()));
            }""",
            arg=list(options),
            timeout=15_000,
        )
    except Exception as exc:
        raise AssertionError(f"{label} missing expected text: {options!r}") from exc


def _assert_body_contains(page, expected: tuple[str, ...], label: str) -> None:
    # Case-insensitive for the same reason as _assert_body_has_any.
    try:
        page.wait_for_function(
            """(expected) => {
                const text = (document.body ? document.body.innerText : "").toLowerCase();
                return expected.every((item) => text.includes(item.toLowerCase()));
            }""",
            arg=list(expected),
            timeout=15_000,
        )
    except Exception as exc:
        body_text = _body_text(page).lower()
        missing = [text for text in expected if text.lower() not in body_text]
        raise AssertionError(f"{label} missing expected text: {missing!r}") from exc


def _assert_button_disabled(page, label: str) -> None:
    button = page.get_by_role("button", name=label)
    button.first.wait_for(state="attached", timeout=15_000)
    if button.count() == 0:
        raise AssertionError(f"Button not found: {label}")
    if not button.first.is_disabled():
        raise AssertionError(f"Button should be disabled before confirmation: {label}")


def _scroll_until_text(page, expected: str, label: str) -> None:
    for _ in range(12):
        if expected in _body_text(page):
            return
        page.mouse.wheel(0, 800)
        page.wait_for_timeout(150)
    raise AssertionError(f"{label} missing expected text after scroll: {expected!r}")


def _capture_screenshot(page, path: Path) -> None:
    content = page.screenshot(full_page=False)
    with private_atomic_binary(path) as handle:
        handle.write(content)


def _assert_route(
    page, route: SmokeRoute, base_url: str, screenshot_dir: Path, viewport_name: str
) -> dict[str, Any]:
    url = _route_url(base_url, route)
    page.goto(url, wait_until="domcontentloaded")
    try:
        page.wait_for_function(
            """([label]) => {
                const text = document.body ? document.body.innerText : "";
                const headings = Array.from(document.querySelectorAll("h1,h2,h3"))
                    .map((el) => el.textContent?.trim())
                    .filter(Boolean);
                return text.trim().length > 200 && headings.includes(label);
            }""",
            arg=[route.label],
            timeout=15_000,
        )
    except Exception as exc:
        raise AssertionError(
            f"{viewport_name} {route.label}: page did not load expected heading"
        ) from exc
    if route.expects_status_grid:
        try:
            page.wait_for_function(
                """() => document.querySelectorAll(".ledger-status-cell").length > 0""",
                timeout=15_000,
            )
        except Exception as exc:
            raise AssertionError(
                f"{viewport_name} {route.label}: status grid did not render"
            ) from exc
    expected_text = ROUTE_TEXT_EXPECTATIONS.get(route.label, ())
    if expected_text:
        try:
            page.wait_for_function(
                """(expected) => {
                    const text = document.body ? document.body.innerText : "";
                    return expected.every((item) => text.includes(item));
                }""",
                arg=list(expected_text),
                timeout=15_000,
            )
        except Exception as exc:
            raise AssertionError(
                f"{viewport_name} {route.label}: expected route text not visible: {expected_text!r}"
            ) from exc
    _wait_for_streamlit_idle(page)
    state = _page_state(page)
    banned_tokens = detect_banned_tokens(state["bodyText"])
    screenshot_path = screenshot_dir / f"{viewport_name}-{_slug(route.label)}.png"
    _capture_screenshot(page, screenshot_path)

    failures = []
    if state["title"] != "Ledger":
        failures.append(f"title is {state['title']!r}, expected 'Ledger'")
    if state["textLength"] < 200:
        failures.append(f"body is too short: {state['textLength']} characters")
    if route.label not in state["headings"]:
        failures.append(f"heading {route.label!r} not found in {state['headings']!r}")
    missing_text = [text for text in expected_text if text not in state["bodyText"]]
    if missing_text:
        failures.append(f"expected route text missing: {missing_text!r}")
    if banned_tokens:
        failures.append(f"banned tokens present: {', '.join(banned_tokens)}")
    if not status_cells_fit(state["statusCells"]):
        failures.append("status cells overflow horizontally")
    if state["visibleOverflowing"]:
        failures.append(f"visible elements overflow viewport: {state['visibleOverflowing']!r}")
    if failures:
        raise AssertionError(f"{viewport_name} {route.label}: " + "; ".join(failures))

    return {
        "route": route.label,
        "path": route.path,
        "viewport": viewport_name,
        "screenshot": str(screenshot_path),
        "status_cells": len(state["statusCells"]),
    }


def _assert_goals_tab(page, base_url: str, screenshot_dir: Path) -> dict[str, Any]:
    page.goto(
        _route_url(base_url, SmokeRoute("Budgets / Goals", "/budgets-goals")),
        wait_until="domcontentloaded",
    )
    page.wait_for_function(
        """() => {
            const headings = Array.from(document.querySelectorAll("h1,h2,h3"))
                .map((el) => el.textContent?.trim())
                .filter(Boolean);
            return headings.includes("Budgets / Goals");
        }""",
        timeout=15_000,
    )
    _wait_for_streamlit_idle(page)
    _assert_button_disabled(page, "Save Budgets")
    _assert_body_contains(page, ("Save preview:",), "Budgets tab")
    ensure_read_only_click("Goals")
    page.get_by_role("tab", name="Goals").click()
    _wait_for_streamlit_idle(page)
    body_text = page.locator("body").inner_text()
    screenshot_path = screenshot_dir / "interaction-budgets-goals-goals-tab.png"
    _capture_screenshot(page, screenshot_path)
    banned_tokens = detect_banned_tokens(body_text)
    if banned_tokens:
        raise AssertionError(f"Goals tab contains banned tokens: {', '.join(banned_tokens)}")
    if "No savings goals are configured yet." not in body_text and "Active Goals" not in body_text:
        raise AssertionError("Goals tab did not show goal-state content")
    _assert_button_disabled(page, "Save Goals")
    _assert_body_contains(page, ("Save preview:",), "Goals tab")
    return {"interaction": "Budgets / Goals -> Goals tab", "screenshot": str(screenshot_path)}


def _assert_cash_plan_tab(page, base_url: str, screenshot_dir: Path) -> dict[str, Any]:
    page.goto(
        _route_url(base_url, SmokeRoute("Budgets / Goals", "/budgets-goals")),
        wait_until="domcontentloaded",
    )
    page.wait_for_function(
        """() => document.body && document.body.innerText.includes("Budgets / Goals")""",
        timeout=15_000,
    )
    _wait_for_streamlit_idle(page)
    ensure_read_only_click("Cash Plan")
    page.get_by_role("tab", name="Cash Plan").click()
    _wait_for_streamlit_idle(page)
    _assert_body_contains(
        page,
        ("Schedules", "Detected possibilities", "Cash plan", "Coverage and assumptions"),
        "Cash Plan tab",
    )
    body_text = _body_text(page)
    banned_tokens = detect_banned_tokens(body_text)
    if banned_tokens:
        raise AssertionError(f"Cash Plan tab contains banned tokens: {', '.join(banned_tokens)}")
    screenshot_path = screenshot_dir / "interaction-budgets-goals-cash-plan-tab.png"
    _capture_screenshot(page, screenshot_path)
    return {"interaction": "Budgets / Goals -> Cash Plan tab", "screenshot": str(screenshot_path)}


def _assert_preferences_view(page, base_url: str, screenshot_dir: Path) -> dict[str, Any]:
    page.goto(
        _route_url(base_url, SmokeRoute("Insights", "/insights")),
        wait_until="domcontentloaded",
    )
    page.wait_for_function(
        """() => document.body && document.body.innerText.includes("Insights")""",
        timeout=15_000,
    )
    _wait_for_streamlit_idle(page)
    ensure_read_only_click("Preferences")
    page.get_by_text("Preferences", exact=True).click()
    _wait_for_streamlit_idle(page)
    _assert_body_contains(
        page,
        (
            "Spending Preferences",
            "Exact merchant or category name",
            "Add or update preference",
        ),
        "Insights preferences view",
    )
    body_text = _body_text(page)
    banned_tokens = detect_banned_tokens(body_text)
    if banned_tokens:
        raise AssertionError(
            f"Insights preferences view contains banned tokens: {', '.join(banned_tokens)}"
        )
    screenshot_path = screenshot_dir / "interaction-insights-preferences-view.png"
    _capture_screenshot(page, screenshot_path)
    return {"interaction": "Insights -> Preferences view", "screenshot": str(screenshot_path)}


def _assert_priority_review_view(page, base_url: str, screenshot_dir: Path) -> dict[str, Any]:
    page.goto(
        _route_url(base_url, SmokeRoute("Insights", "/insights")),
        wait_until="domcontentloaded",
    )
    page.wait_for_function(
        """() => document.body && document.body.innerText.includes("Insights")""",
        timeout=15_000,
    )
    _wait_for_streamlit_idle(page)
    _assert_body_contains(page, PRIORITY_REVIEW_EVIDENCE_TEXT, "Insights priority review view")
    body_text = _body_text(page)
    banned_tokens = detect_banned_tokens(body_text)
    if banned_tokens:
        raise AssertionError(
            f"Insights priority review view contains banned tokens: {', '.join(banned_tokens)}"
        )
    screenshot_path = screenshot_dir / "interaction-insights-priority-review-view.png"
    _capture_screenshot(page, screenshot_path)
    return {"interaction": "Insights -> Priority Review", "screenshot": str(screenshot_path)}


def _assert_review_queue_guardrails(page, base_url: str, screenshot_dir: Path) -> dict[str, Any]:
    page.goto(
        _route_url(base_url, SmokeRoute("Review Queue", "/review")), wait_until="domcontentloaded"
    )
    page.wait_for_function(
        """() => document.body && document.body.innerText.includes("Review Queue")""",
        timeout=15_000,
    )
    _wait_for_streamlit_idle(page)
    _assert_body_has_any(page, ("No review rows found.", "Save Review Decisions"), "Review Queue")
    body_text = _body_text(page)
    if "Save Review Decisions" in body_text:
        _assert_body_contains(
            page,
            (
                "Status",
                "Missing",
                "Search",
                "Detail",
                "Selected Row",
                "Changed Rows",
                "Confirm review save",
            ),
            "Review Queue guardrails",
        )
        _assert_button_disabled(page, "Save Review Decisions")
    screenshot_path = screenshot_dir / "interaction-review-queue-guardrails.png"
    _capture_screenshot(page, screenshot_path)
    return {"interaction": "Review Queue guardrails", "screenshot": str(screenshot_path)}


def _assert_import_center_readiness(page, base_url: str, screenshot_dir: Path) -> dict[str, Any]:
    page.goto(
        _route_url(base_url, SmokeRoute("Import Center", "/import-center")),
        wait_until="domcontentloaded",
    )
    page.wait_for_function(
        """() => document.body && document.body.innerText.includes("Import Center")""",
        timeout=15_000,
    )
    _wait_for_streamlit_idle(page)
    _assert_body_has_any(
        page, ("Finalize Readiness", "No import run manifest found."), "Import Center"
    )
    body_text = _body_text(page)
    if "Finalize Readiness" in body_text:
        _assert_body_contains(
            page,
            ("Target DB unchanged", "Staging DB present", "Staged CSV root present"),
            "Import Center readiness",
        )
    # Import Center intentionally exposes mutations now. This read-only smoke path
    # observes their state and never clicks them; ensure_read_only_click carries the
    # shared denylist for any future generic interaction helper.
    screenshot_path = screenshot_dir / "interaction-import-center-readiness.png"
    _capture_screenshot(page, screenshot_path)
    return {"interaction": "Import Center readiness", "screenshot": str(screenshot_path)}


def _assert_admin_rules_guardrails(
    page, base_url: str, screenshot_dir: Path
) -> list[dict[str, Any]]:
    results = []
    page.goto(
        _route_url(base_url, SmokeRoute("Admin / Data", "/admin-data")),
        wait_until="domcontentloaded",
    )
    page.wait_for_function(
        """() => document.body && document.body.innerText.includes("Admin / Data")""",
        timeout=15_000,
    )
    _scroll_until_text(page, "Confirm adjustment apply", "Admin / Data guardrails")
    _scroll_until_text(page, "Confirm cleanup", "Admin / Data guardrails")
    _scroll_until_text(page, "Confirm export", "Admin / Data guardrails")
    admin_screenshot = screenshot_dir / "interaction-admin-data-guardrails.png"
    _capture_screenshot(page, admin_screenshot)
    results.append({"interaction": "Admin / Data guardrails", "screenshot": str(admin_screenshot)})

    page.goto(
        _route_url(base_url, SmokeRoute("Rules / Settings", "/rules")),
        wait_until="domcontentloaded",
    )
    page.wait_for_function(
        """() => document.body && document.body.innerText.includes("Rules / Settings")""",
        timeout=15_000,
    )
    _scroll_until_text(page, "Confirm merchant profile save", "Rules / Settings guardrails")
    page.keyboard.press("Home")
    ensure_read_only_click("Category Taxonomy")
    page.get_by_text("Category Taxonomy", exact=True).click()
    _scroll_until_text(page, "Confirm category taxonomy save", "Rules / Settings guardrails")
    page.keyboard.press("Home")
    ensure_read_only_click("Import / Export")
    page.get_by_text("Import / Export", exact=True).click()
    _scroll_until_text(page, "Confirm seed import", "Rules / Settings guardrails")
    rules_screenshot = screenshot_dir / "interaction-rules-settings-guardrails.png"
    _capture_screenshot(page, rules_screenshot)
    results.append(
        {"interaction": "Rules / Settings guardrails", "screenshot": str(rules_screenshot)}
    )
    return results


def _assert_reconciliation_evidence(page, base_url: str, screenshot_dir: Path) -> dict[str, Any]:
    """Follow the disposable freeze fixture through its real native evidence link."""
    from playwright.sync_api import expect

    page.goto(
        _route_url(base_url, SmokeRoute("Plan versus actual", "/plan-reconciliation")),
        wait_until="domcontentloaded",
    )
    _wait_for_streamlit_idle(page)
    ensure_read_only_click("All Time")
    period = page.get_by_role("combobox", name="Period", exact=True)
    period.scroll_into_view_if_needed()
    period.click()
    expect(period).to_have_attribute("aria-expanded", "true")
    period.fill("All Time")
    period_option = page.get_by_role("option", name="All Time", exact=True)
    expect(period_option).to_be_visible()
    period_option_id = period_option.get_attribute("id")
    assert period_option_id, "The All Time option must expose an ID for keyboard focus"
    period.press("ArrowDown")
    expect(period).to_have_attribute("aria-activedescendant", period_option_id)
    # Select the focused option without racing the dropdown's positioning animation.
    period.press("Enter")
    _wait_for_streamlit_idle(page)
    expect(period).to_have_value("All Time")
    expect(period).to_have_attribute("aria-expanded", "false")
    label = "2025-08-13 · SYNTHETIC VARIABLE SUBSCRIPTION · amount and date difference"
    ensure_read_only_click(label)
    occurrence = page.get_by_role("combobox", name="Expected occurrence", exact=True)
    expect(occurrence).to_be_visible(timeout=15_000)
    occurrence.scroll_into_view_if_needed()
    occurrence.click()
    expect(occurrence).to_have_attribute("aria-expanded", "true")
    occurrence.fill(label)
    occurrence_option = page.get_by_role("option", name=label, exact=True)
    expect(occurrence_option).to_be_visible()
    occurrence_option_id = occurrence_option.get_attribute("id")
    assert occurrence_option_id, "The expected occurrence must expose an ID for keyboard focus"
    occurrence.press("ArrowDown")
    expect(occurrence).to_have_attribute("aria-activedescendant", occurrence_option_id)
    occurrence.press("Enter")
    _wait_for_streamlit_idle(page)
    expect(occurrence).to_have_value(label)
    expect(occurrence).to_have_attribute("aria-expanded", "false")
    _assert_body_contains(
        page,
        ("Amount changed", "Date changed", "Matching basis:", "Open imported evidence"),
        "Plan versus actual detail",
    )
    ensure_read_only_click("Statement coverage and expectation revision")
    page.get_by_text("Statement coverage and expectation revision", exact=True).click()
    _assert_body_contains(page, ("material_revision", "coverage"), "Reconciliation evidence basis")
    detail_path = screenshot_dir / "interaction-plan-reconciliation-detail.png"
    _capture_screenshot(page, detail_path)
    ensure_read_only_click("Open imported evidence")
    page.get_by_role("link", name=re.compile(r"\bOpen imported evidence$")).click()
    _wait_for_streamlit_idle(page)
    _assert_body_contains(
        page, ("Imported evidence", "Show all transactions"), "Linked imported evidence"
    )
    if detect_banned_tokens(_body_text(page)):
        raise AssertionError("Linked reconciliation evidence contains an application error")
    evidence_path = screenshot_dir / "interaction-plan-reconciliation-imported-evidence.png"
    _capture_screenshot(page, evidence_path)
    return {
        "interaction": "Plan versus actual -> exact finding -> imported evidence",
        "screenshot": str(evidence_path),
        "detail_screenshot": str(detail_path),
    }


def run_smoke(
    base_url: str,
    screenshot_dir: Path,
    timeout_seconds: float,
    *,
    reconciliation_fixture: bool = False,
) -> dict[str, Any]:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            'Playwright is not installed. Run `.venv/bin/python -m pip install -e ".[dev,analysis]"`.'
        ) from exc

    ensure_private_dir(screenshot_dir)
    _wait_for_server(base_url, timeout_seconds)
    results: list[dict[str, Any]] = []
    console_messages: list[dict[str, str]] = []

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch()
        except PlaywrightError as exc:
            raise RuntimeError(
                "Chromium is not installed for Playwright. Run `.venv/bin/python -m playwright install chromium`."
            ) from exc
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.on(
                "console",
                lambda message: (
                    console_messages.append(
                        {
                            "type": message.type,
                            "text": message.text,
                            "url": message.location.get("url", ""),
                        }
                    )
                    if message.type in {"error", "warning"}
                    else None
                ),
            )
            for route in SMOKE_ROUTES:
                results.append(_assert_route(page, route, base_url, screenshot_dir, "desktop"))

            page.set_viewport_size({"width": 390, "height": 844})
            for route in SMOKE_ROUTES:
                results.append(_assert_route(page, route, base_url, screenshot_dir, "mobile"))

            page.set_viewport_size({"width": 1280, "height": 900})
            results.append(_assert_import_center_readiness(page, base_url, screenshot_dir))
            results.append(_assert_review_queue_guardrails(page, base_url, screenshot_dir))
            results.append(_assert_goals_tab(page, base_url, screenshot_dir))
            results.append(_assert_cash_plan_tab(page, base_url, screenshot_dir))
            results.append(_assert_priority_review_view(page, base_url, screenshot_dir))
            results.append(_assert_preferences_view(page, base_url, screenshot_dir))
            results.extend(_assert_admin_rules_guardrails(page, base_url, screenshot_dir))
            if reconciliation_fixture:
                results.append(_assert_reconciliation_evidence(page, base_url, screenshot_dir))
        finally:
            browser.close()

    relevant_console_messages = [
        message for message in console_messages if console_message_is_relevant(message)
    ]
    if relevant_console_messages:
        rendered = "; ".join(
            f"{message['type']}: {message['text']} ({message['url']})"
            for message in relevant_console_messages
        )
        raise AssertionError(f"Console warnings/errors found: {rendered}")

    return {"base_url": base_url, "screenshots": str(screenshot_dir), "checks": results}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Ledger UI smoke checks.")
    parser.add_argument(
        "--base-url",
        default="http://localhost:8502",
        help="Base URL for an already-running Ledger Streamlit app.",
    )
    parser.add_argument(
        "--screenshots",
        type=Path,
        default=Path("/tmp/ledger-ui-smoke"),
        help="Directory for smoke screenshots. Defaults outside the repo.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=45.0,
        help="Seconds to wait for the Streamlit app to respond.",
    )
    parser.add_argument(
        "--reconciliation-fixture",
        action="store_true",
        default=os.environ.get("LEDGER_FREEZE_SYNTHETIC") == "1",
        help="Require the disposable synthetic freeze finding and follow its imported evidence link.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = run_smoke(
            args.base_url,
            args.screenshots,
            args.timeout,
            reconciliation_fixture=args.reconciliation_fixture,
        )
    except Exception as exc:
        print(f"UI smoke failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
