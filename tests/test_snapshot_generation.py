"""Snapshot generation, cursor binding, and re-pinning after the database rotates."""

from __future__ import annotations

import shutil
import sqlite3
from contextlib import closing, suppress
from pathlib import Path

import pytest
from mcp_synthetic import build_synthetic_mcp_database

from ledger.mcp.contracts import ERROR_ENVELOPE_SCHEMA
from ledger.mcp.server import call_tool
from ledger.mcp.service import LedgerMCPService
from ledger.storage.read_only import ReadOnlyDatabase


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "ledger.db"
    build_synthetic_mcp_database(path)
    return path


def _add_row(path: Path) -> None:
    """Append one CAD expense in place, so the pin's size/mtime change."""
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "INSERT INTO transactions (txn_id, posting_date, institution, account, "
            "account_type, flow, cashflow_type, include_in_cashflow, currency, amount_raw, "
            "amount_signed, description, merchant, category, merchant_confidence, "
            "category_confidence, exclude_from_spending, merchant_raw, category_raw, "
            "source_file, source_path, source_period, source_row, imported_at, "
            "parser_version, txn_fingerprint, day_of_week, day_of_month, week_of_year, "
            "is_weekend, is_month_start, is_month_end) VALUES "
            "('rotated-1', '2026-01-15', 'harbor', 'harbor-chequing-9101', 'checking', 'expense', "
            "'expense', 1, 'CAD', 5.00, -5.00, 'ROTATED', 'ROTATED', 'Other', 'high', "
            "'high', 0, 'ROTATED', '', 'f.csv', '/f.csv', '2026-01', 1, "
            "'2026-01-16 00:00:00', 'v1', 'fp-rot', 3, 15, 3, 0, 0, 0)"
        )


def test_generation_is_not_the_migration_version(db_path: Path) -> None:
    """migration_version is computed from the code, so it is a build constant."""
    database = ReadOnlyDatabase(db_path)
    try:
        snapshot = database.preflight()
    finally:
        database.close()
    assert snapshot.generation
    assert snapshot.generation != snapshot.migration_version
    assert len(snapshot.generation) == 16


def test_generation_changes_when_the_database_changes(db_path: Path, tmp_path: Path) -> None:
    database = ReadOnlyDatabase(db_path)
    try:
        before = database.preflight().generation
    finally:
        database.close()

    _add_row(db_path)

    database = ReadOnlyDatabase(db_path)
    try:
        after = database.preflight().generation
    finally:
        database.close()

    assert before != after


def test_generation_is_stable_for_an_unchanged_database(db_path: Path) -> None:
    generations = []
    for _ in range(2):
        database = ReadOnlyDatabase(db_path)
        try:
            generations.append(database.preflight().generation)
        finally:
            database.close()
    assert generations[0] == generations[1]


def test_status_publishes_the_generation(db_path: Path) -> None:
    service = LedgerMCPService(db_path)
    try:
        envelope = service.status()
    finally:
        service.close()
    assert envelope["coverage"]["generation"] == service.snapshot.generation


def test_a_cursor_from_another_generation_is_rejected(db_path: Path, tmp_path: Path) -> None:
    """The review's only demonstrated silently-WRONG answer.

    A cursor minted before an import and replayed after the documented restart
    used to validate cleanly and return page two of a different dataset.
    """
    service = LedgerMCPService(db_path)
    try:
        first = call_tool(
            service,
            "ledger.aggregate",
            {
                "start_date": "2020-01-01",
                "end_date": "2030-12-31",
                "flow": "spending",
                "group_by": "category",
                "limit": 1,
            },
        )
        cursor = first["structuredContent"]["page"]["next_cursor"]
        assert cursor, "fixture must paginate for this test to mean anything"
    finally:
        service.close()

    # Rotate the database, then start a fresh server exactly as the documented
    # restart-after-import workflow does.
    _add_row(db_path)

    service = LedgerMCPService(db_path)
    try:
        replayed = call_tool(
            service,
            "ledger.aggregate",
            {
                "start_date": "2020-01-01",
                "end_date": "2030-12-31",
                "flow": "spending",
                "group_by": "category",
                "limit": 1,
                "cursor": cursor,
            },
        )
    finally:
        service.close()

    assert replayed["isError"] is True
    assert replayed["structuredContent"]["error"]["code"] == "invalid_cursor"


def test_a_cursor_from_the_same_generation_still_paginates(db_path: Path) -> None:
    service = LedgerMCPService(db_path)
    try:
        arguments = {
            "start_date": "2020-01-01",
            "end_date": "2030-12-31",
            "flow": "spending",
            "group_by": "category",
            "limit": 1,
        }
        first = call_tool(service, "ledger.aggregate", arguments)
        cursor = first["structuredContent"]["page"]["next_cursor"]
        assert cursor
        second = call_tool(service, "ledger.aggregate", {**arguments, "cursor": cursor})
    finally:
        service.close()

    assert "error" not in second["structuredContent"]
    assert second["structuredContent"]["data"]["groups"]


def test_an_in_place_write_is_served_after_a_repin(db_path: Path) -> None:
    """An ordinary `ledger import` used to brick a running server permanently."""
    service = LedgerMCPService(db_path)
    try:
        before = service.status()["data"]["transaction_count"]
        _add_row(db_path)

        after_envelope = service.status()
        after = after_envelope["data"]["transaction_count"]
    finally:
        service.close()

    assert after == before + 1
    assert after_envelope["coverage"]["generation"] != ""


def test_no_published_document_still_tells_the_operator_to_restart_after_an_import(
    db_path: Path,
) -> None:
    """The behaviour above has a published claim attached, and it was stale.

    README said "Restart the MCP after a successful Ledger import publishes a new
    database snapshot", which was true before the server learned to re-pin and is
    false now -- proved one function up, where an in-place write is served without
    a restart. `ledger.changes` makes the staleness consequential rather than
    merely untidy: its whole loop is a long-lived session watching for an import
    to arrive, so an instruction to restart tells the operator to destroy the
    session that the loop exists to run.

    Pinned against the running server rather than against the sentence, so the
    prose cannot drift back without the behaviour drifting with it.
    """
    service = LedgerMCPService(db_path)
    try:
        before = service.status()["data"]["transaction_count"]
        _add_row(db_path)
        after = service.status()["data"]["transaction_count"]
    finally:
        service.close()

    assert after == before + 1, "the server no longer re-pins; the docs claim may be true again"
    readme = (Path(__file__).resolve().parents[1] / "docs/agent/mcp.md").read_text(encoding="utf-8")
    skill = (
        Path(__file__).resolve().parents[1] / "docs/agent/skills/ledger-query/SKILL.md"
    ).read_text(encoding="utf-8")
    for document, name in ((readme, "docs/agent/mcp.md"), (skill, "SKILL.md")):
        assert "Restart the MCP" not in document, name


def test_a_repin_reports_the_new_generation(db_path: Path) -> None:
    service = LedgerMCPService(db_path)
    try:
        before = service.status()["coverage"]["generation"]
        _add_row(db_path)
        after = service.status()["coverage"]["generation"]
    finally:
        service.close()
    assert before != after, "an agent must be able to see that the data moved"


def test_a_repin_does_not_leak_file_descriptors(db_path: Path) -> None:
    """Each superseded pin holds two descriptors, released only by close()."""
    import os

    if not os.path.isdir("/dev/fd"):  # pragma: no cover - platform guard
        pytest.skip("no /dev/fd on this platform")

    service = LedgerMCPService(db_path)
    try:
        service.status()
        opened = len(os.listdir("/dev/fd"))
        for index in range(5):
            _add_row_unique(db_path, index)
            service.status()
        after = len(os.listdir("/dev/fd"))
    finally:
        service.close()

    # Five re-pins would leak ten descriptors if the superseded pin were dropped
    # rather than closed. Allow a small margin for the listdir call itself.
    assert after <= opened + 2, f"descriptors grew from {opened} to {after}"


def _add_row_unique(path: Path, index: int) -> None:
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "INSERT INTO transactions (txn_id, posting_date, institution, account, "
            "account_type, flow, cashflow_type, include_in_cashflow, currency, amount_raw, "
            "amount_signed, description, merchant, category, merchant_confidence, "
            "category_confidence, exclude_from_spending, merchant_raw, category_raw, "
            "source_file, source_path, source_period, source_row, imported_at, "
            "parser_version, txn_fingerprint, day_of_week, day_of_month, week_of_year, "
            "is_weekend, is_month_start, is_month_end) VALUES "
            f"('rot-{index}', '2026-01-15', 'harbor', 'harbor-chequing-9101', 'checking', 'expense', "
            "'expense', 1, 'CAD', 5.00, -5.00, 'ROTATED', 'ROTATED', 'Other', 'high', "
            f"'high', 0, 'ROTATED', '', 'f.csv', '/f.csv', '2026-01', 1, "
            f"'2026-01-16 00:00:00', 'v1', 'fp-{index}', 3, 15, 3, 0, 0, 0)"
        )


def _prime(service: LedgerMCPService) -> None:
    """Establish the pin through the SERVED path, retrying the transient codes.

    The bare `service.status()` this replaced let a `ReadOnlyUnavailableError`
    escape from the setup step of a test about something else entirely: under
    concurrent load the read-only open can fail transiently, and the traceback
    landed before the assertions the test exists for had run. `call_tool` is the
    path this module tests anyway, and it classifies that failure as retryable --
    the parametrized test below pins exactly that classification -- so retrying
    it here removes the flake without hiding a real one: a persistent failure
    still fails, and with a legible message.
    """
    for _ in range(10):
        result = call_tool(service, "ledger.status", {})
        if result.get("isError") is not True:
            return
        code = result["structuredContent"]["error"]["code"]  # type: ignore[index]
        assert code in _TRANSIENT_CODES, result
    raise AssertionError("ledger.status never succeeded while priming the pin")


_TRANSIENT_CODES = frozenset({"read_only_unavailable", "database_changed", "database_unavailable"})


def test_a_missing_database_after_rotation_fails_closed(db_path: Path, tmp_path: Path) -> None:
    """A failed re-pin must not serve stale rows, and must not say `internal_error`."""
    service = LedgerMCPService(db_path)
    try:
        _prime(service)
        shutil.move(db_path, tmp_path / "moved-away.db")

        failure = call_tool(service, "ledger.status", {})
    finally:
        with suppress(Exception):
            service.close()

    assert failure["isError"] is True
    assert failure["structuredContent"]["error"]["code"] in {
        "database_unavailable",
        "database_changed",
        "read_only_unavailable",
    }
    assert failure["structuredContent"]["error"]["code"] != "internal_error"


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        # Transient: a -journal sidecar exists only while a writer is mid-transaction.
        ("read_only_unavailable", True),
        ("database_changed", True),
        ("database_unavailable", True),
        # A bug. Retrying reproduces it.
        ("internal_error", False),
        ("corrupt_data", False),
        ("incompatible_schema", False),
        ("invalid_argument", False),
        ("invalid_cursor", False),
        # The request was well formed; only a narrower filter can change it.
        ("filter_too_broad", False),
    ],
)
def test_retryable_reflects_whether_retrying_can_help(code: str, expected: bool) -> None:
    from ledger.mcp.server import _failure

    envelope = _failure(code)["structuredContent"]
    assert envelope["error"]["retryable"] is expected
    assert set(ERROR_ENVELOPE_SCHEMA["properties"]) == {"error", "schema"}
