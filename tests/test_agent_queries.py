"""Synthetic tests for agent-facing reusable Ledger query services."""

from __future__ import annotations

from dataclasses import fields
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from mcp_synthetic import (
    CHINESE_SOURCE,
    CHINESE_TOTAL,
    FULLWIDTH_ACME,
    NORTHSTAR_TOTAL,
    _transaction,
    build_synthetic_mcp_database,
)

from ledger.query.agent import AgentFilters, AgentQueryError
from ledger.query.service import LedgerQueryService
from ledger.storage.database import session_scope
from ledger.storage.read_only import ReadOnlyDatabase


def _service(path: Path) -> tuple[ReadOnlyDatabase, LedgerQueryService]:
    database = ReadOnlyDatabase(path)
    database.preflight()
    return database, LedgerQueryService(
        path,
        session_scope_factory=database.session_scope,
    )


def test_income_source_aggregate_is_exact_and_reports_normalized_matches(
    tmp_path: Path,
) -> None:
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    _database, service = _service(db_path)

    result = service.agent.aggregate_transaction_facts(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 12, 31),
        flow="income",
        group_by="source",
        filters=AgentFilters(sources=("northstar university",)),
    )

    assert result.total == NORTHSTAR_TOTAL
    assert result.transaction_count == 2
    assert result.matched_sources == ("NORTHSTAR UNIVERSITY",)
    assert [(group.key, group.amount, group.transaction_count) for group in result.groups] == [
        ("NORTHSTAR UNIVERSITY", NORTHSTAR_TOTAL, 2)
    ]
    # Renamed to name the whitespace collapse _normalize_match has always
    # performed; the behaviour is unchanged, the advertisement was wrong.
    assert result.match_semantics == "unicode_nfkc_whitespace_collapse_casefold_exact"


def test_chinese_source_and_month_grouping_preserve_unicode_and_decimal(
    tmp_path: Path,
) -> None:
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    _database, service = _service(db_path)

    result = service.agent.aggregate_transaction_facts(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 12, 31),
        flow="income",
        group_by="month",
        filters=AgentFilters(sources=(CHINESE_SOURCE,)),
    )

    assert result.total == CHINESE_TOTAL
    assert result.matched_sources == (CHINESE_SOURCE,)
    assert [(group.key, group.amount) for group in result.groups] == [
        ("2025-02", Decimal("1718.18")),
        ("2025-08", Decimal("1000.10")),
    ]


def test_normalization_collision_returns_every_exact_match_without_fuzzy_matching(
    tmp_path: Path,
) -> None:
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    _database, service = _service(db_path)

    exact = service.agent.aggregate_transaction_facts(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 12, 31),
        flow="income",
        group_by="none",
        filters=AgentFilters(sources=("ACME LAB",)),
    )
    fuzzy = service.agent.aggregate_transaction_facts(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 12, 31),
        flow="income",
        group_by="none",
        filters=AgentFilters(sources=("ACM LAB",)),
    )

    assert exact.total == Decimal("3.33")
    assert exact.matched_sources == ("acme lab", FULLWIDTH_ACME)
    assert fuzzy.total == Decimal("0.00")
    assert fuzzy.matched_sources == ()


def test_normalized_match_collision_fails_instead_of_overflowing_contract(
    tmp_path: Path,
) -> None:
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    with session_scope(db_path) as session:
        session.add_all(
            _transaction(
                f"collision-{index}",
                posted=date(2025, 10, 1),
                amount="1.00",
                merchant=f"COLLISION{' ' * index}SOURCE",
                category="Research Income",
                flow="income",
                cashflow_type="income",
            )
            for index in range(1, 52)
        )
    _database, service = _service(db_path)

    # Still fails closed rather than truncating the matched set; it now says WHY,
    # because "too broad" is a request the caller can narrow and "invalid" is not.
    with pytest.raises(AgentQueryError, match="filter_too_broad"):
        service.agent.aggregate_transaction_facts(
            start_date=date(2025, 1, 1),
            end_date=date(2025, 12, 31),
            flow="income",
            group_by="source",
            filters=AgentFilters(sources=("COLLISION SOURCE",)),
        )


def test_income_and_spending_use_existing_classification_and_exclusion_policies(
    tmp_path: Path,
) -> None:
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    _database, service = _service(db_path)

    income = service.agent.aggregate_transaction_facts(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 12, 31),
        flow="income",
        group_by="none",
        filters=AgentFilters(sources=("NORTHSTAR UNIVERSITY",)),
    )
    spending = service.agent.aggregate_transaction_facts(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 12, 31),
        flow="spending",
        group_by="category",
        filters=AgentFilters(),
    )

    assert income.total == NORTHSTAR_TOTAL
    assert income.transaction_count == 2
    assert spending.total == Decimal("42.64")
    assert spending.transaction_count == 3
    assert [(group.key, group.amount) for group in spending.groups] == [
        ("Groceries", Decimal("30.30")),
        ("Books", Decimal("12.34")),
    ]


def test_aggregate_group_pagination_is_stable_and_query_bound(tmp_path: Path) -> None:
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    _database, service = _service(db_path)
    arguments = {
        "start_date": date(2025, 1, 1),
        "end_date": date(2025, 12, 31),
        "flow": "income",
        "group_by": "source",
        "filters": AgentFilters(),
        "limit": 2,
    }

    first = service.agent.aggregate_transaction_facts(**arguments)
    second = service.agent.aggregate_transaction_facts(**arguments, cursor=first.next_cursor)

    assert len(first.groups) == 2
    assert first.has_more is True
    assert first.next_cursor is not None
    assert set(first.groups).isdisjoint(second.groups)
    assert first.total == second.total
    assert first.transaction_count == second.transaction_count
    with pytest.raises(AgentQueryError, match="invalid_cursor"):
        service.agent.aggregate_transaction_facts(
            **{**arguments, "flow": "spending"}, cursor=first.next_cursor
        )


def test_evidence_search_is_bounded_paginated_and_has_no_private_fields(tmp_path: Path) -> None:
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    _database, service = _service(db_path)
    arguments = {
        "start_date": date(2025, 1, 1),
        "end_date": date(2025, 12, 31),
        "flow": "income",
        "filters": AgentFilters(sources=("northstar university",)),
        "limit": 1,
    }

    first = service.agent.search_transaction_evidence(**arguments)
    second = service.agent.search_transaction_evidence(**arguments, cursor=first.next_cursor)

    assert first.total_count == 2
    assert first.has_more is True
    assert first.next_cursor is not None
    assert second.has_more is False
    assert first.items[0].amount + second.items[0].amount == NORTHSTAR_TOTAL
    assert first.items[0].source == "NORTHSTAR UNIVERSITY"
    assert {field.name for field in fields(first.items[0])} == {
        "evidence_id",
        "posting_date",
        "amount",
        "currency",
        "flow",
        "cashflow_type",
        "source",
        "category",
        "institution",
    }


@pytest.mark.parametrize(
    ("start_date", "end_date", "flow", "group_by", "limit"),
    [
        (date(2025, 2, 1), date(2025, 1, 1), "income", "none", 10),
        (date(2025, 1, 1), date(2025, 2, 1), "refund", "none", 10),
        (date(2025, 1, 1), date(2025, 2, 1), "income", "account", 10),
        (date(2025, 1, 1), date(2025, 2, 1), "income", "none", 0),
        (date(2025, 1, 1), date(2025, 2, 1), "income", "none", 101),
    ],
)
def test_agent_queries_reject_unsupported_or_unbounded_arguments(
    tmp_path: Path,
    start_date: date,
    end_date: date,
    flow: str,
    group_by: str,
    limit: int,
) -> None:
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    _database, service = _service(db_path)

    with pytest.raises(AgentQueryError, match="invalid_argument"):
        service.agent.aggregate_transaction_facts(
            start_date=start_date,
            end_date=end_date,
            flow=flow,
            group_by=group_by,
            filters=AgentFilters(),
            limit=limit,
        )


def test_injected_read_only_boundary_is_used_by_existing_income_source_query(
    tmp_path: Path,
) -> None:
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    _database, service = _service(db_path)

    assert (
        service.get_income_sources(date(2025, 1, 1), date(2025, 12, 31), top_n=20)[
            "NORTHSTAR UNIVERSITY"
        ]
        == NORTHSTAR_TOTAL
    )


def test_agent_aggregate_sums_large_cent_values_exactly(tmp_path: Path) -> None:
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    with session_scope(db_path) as session:
        session.add_all(
            _transaction(
                f"precision-{index}",
                posted=date(2025, 11, 1),
                amount="9999999999.99",
                merchant="PRECISION SOURCE",
                category="Research Income",
                flow="income",
                cashflow_type="income",
            )
            for index in range(10_000)
        )
        session.add(
            _transaction(
                "precision-cent",
                posted=date(2025, 11, 1),
                amount="0.01",
                merchant="PRECISION SOURCE",
                category="Research Income",
                flow="income",
                cashflow_type="income",
            )
        )
    _database, service = _service(db_path)

    result = service.agent.aggregate_transaction_facts(
        start_date=date(2025, 11, 1),
        end_date=date(2025, 11, 1),
        flow="income",
        group_by="source",
        filters=AgentFilters(sources=("PRECISION SOURCE",)),
    )

    expected = Decimal("9999999999.99") * 10_000 + Decimal("0.01")
    assert result.total == expected
    assert result.groups[0].amount == expected


def test_all_nested_query_delegations_preserve_injected_session_boundary(
    tmp_path: Path,
) -> None:
    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    database = ReadOnlyDatabase(db_path)
    database.preflight()
    opened_paths: list[Path] = []

    def tracked_session(path: Path):
        opened_paths.append(path)
        return database.session_scope(path)

    service = LedgerQueryService(db_path, session_scope_factory=tracked_session)
    current_start = date(2025, 7, 1)
    current_end = date(2025, 12, 31)
    previous_start = date(2025, 1, 1)
    previous_end = date(2025, 6, 30)

    service.get_period_comparison(current_start, current_end, previous_start, previous_end)
    service.get_spending_by_category_comparison(
        current_start,
        current_end,
        previous_start,
        previous_end,
    )
    service.compute_true_cost_kpis(current_start, current_end)
    service.get_true_cost_by_category(current_start, current_end)
    service.get_true_cost_category_details(current_start, current_end)

    assert opened_paths == [db_path] * 7
