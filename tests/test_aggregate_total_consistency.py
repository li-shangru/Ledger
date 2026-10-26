"""One period, one flow, one filter set: one total, whatever the grouping."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from decimal import Decimal
from pathlib import Path

import pytest
from mcp_synthetic import build_synthetic_mcp_database

from ledger.mcp.server import call_tool
from ledger.mcp.service import LedgerMCPService

GROUPINGS = ["none", "category", "month", "merchant", "source"]


def _add_blank_merchant_expense(path: Path) -> Decimal:
    """A fictional expense with no resolved merchant must still count as spending."""
    amount = Decimal("99.00")
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "INSERT INTO transactions (txn_id, posting_date, institution, account, "
            "account_type, flow, cashflow_type, include_in_cashflow, currency, amount_raw, "
            "amount_signed, description, merchant, category, merchant_confidence, "
            "category_confidence, exclude_from_spending, merchant_raw, category_raw, "
            "source_file, source_path, source_period, source_row, imported_at, "
            "parser_version, txn_fingerprint, day_of_week, day_of_month, week_of_year, "
            "is_weekend, is_month_start, is_month_end) VALUES "
            "('blank-merchant-1', '2025-06-15', 'harbor', 'harbor-chequing-9101', 'checking', "
            "'expense', 'expense', 1, 'CAD', 99.00, -99.00, 'UNRESOLVED', '', 'Other', "
            "'none', 'low', 0, 'UNRESOLVED RAW', '', 'f.csv', '/f.csv', '2025-06', 1, "
            "'2025-06-16 00:00:00', 'v1', 'fp-blank', 6, 15, 24, 1, 0, 0)"
        )
    return amount


@pytest.fixture
def service(tmp_path: Path) -> LedgerMCPService:
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    _add_blank_merchant_expense(db_path)
    built = LedgerMCPService(db_path)
    yield built
    built.close()


def _spending(service: LedgerMCPService, group_by: str) -> dict[str, object]:
    return call_tool(
        service,
        "ledger.aggregate",
        {
            "start_date": "2020-01-01",
            "end_date": "2030-12-31",
            "flow": "spending",
            "group_by": group_by,
            "limit": 100,
        },
    )["structuredContent"]["data"]


def test_total_is_identical_across_every_grouping(service: LedgerMCPService) -> None:
    """`merchant != ''` narrowed the GROUPS and leaked into the reported TOTAL.

    A blank-merchant row made `group_by=merchant` report a smaller total than
    `group_by=category` for the same period, flow and filters, with nothing in
    the envelope to explain the difference.
    """
    totals = {grouping: _spending(service, grouping)["total"] for grouping in GROUPINGS}
    assert len(set(totals.values())) == 1, totals


def test_transaction_count_is_identical_across_every_grouping(
    service: LedgerMCPService,
) -> None:
    counts = {grouping: _spending(service, grouping)["transaction_count"] for grouping in GROUPINGS}
    assert len(set(counts.values())) == 1, counts


def test_the_blank_merchant_row_is_in_the_total(service: LedgerMCPService) -> None:
    ungrouped = Decimal(_spending(service, "none")["total"])
    by_merchant = Decimal(_spending(service, "merchant")["total"])
    assert ungrouped == by_merchant
    assert ungrouped >= Decimal("99.00")


def test_a_blank_merchant_is_still_not_a_named_group(service: LedgerMCPService) -> None:
    """Fixing the total must not invent an empty-string merchant group."""
    groups = _spending(service, "merchant")["groups"]
    assert all(group["key"] for group in groups), groups


def test_the_gap_between_total_and_groups_is_disclosed(service: LedgerMCPService) -> None:
    """If the groups cannot sum to the total, the envelope must say why."""
    data = _spending(service, "merchant")
    grouped_sum = sum(Decimal(group["amount"]) for group in data["groups"])
    total = Decimal(data["total"])

    assert grouped_sum != total, "fixture must exercise the gap"
    excluded = data["excluded"]
    assert excluded["blank_merchant"] == 1
    assert total - grouped_sum == Decimal("99.00")


def test_no_blank_merchant_gap_for_groupings_that_keep_every_row(
    service: LedgerMCPService,
) -> None:
    for grouping in ("none", "category", "month"):
        data = _spending(service, grouping)
        assert data["excluded"]["blank_merchant"] == 0, grouping
