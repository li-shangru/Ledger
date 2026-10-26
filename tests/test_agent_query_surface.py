"""Amount filters, ordering, day grouping, honest matching, and the by-id tool.

Every input added here changes what a pagination offset addresses, so each one is
also exercised against `_cursor_fingerprint`: a cursor minted with one value must
be refused when replayed with another, or paging silently skips and duplicates.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from mcp_synthetic import PRIVATE_ACCOUNT_NUMBER, _identifier, build_synthetic_mcp_database

from ledger.mcp.server import call_tool
from ledger.mcp.service import LedgerMCPService
from ledger.query.agent import AgentFilters, AgentQueryError
from ledger.query.service import LedgerQueryService
from ledger.storage.read_only import ReadOnlyDatabase

_TRANSACTION_COLUMNS = (
    "txn_id, posting_date, institution, account, account_type, flow, cashflow_type, "
    "include_in_cashflow, currency, amount_raw, amount_signed, description, merchant, "
    "category, merchant_confidence, category_confidence, exclude_from_spending, "
    "merchant_raw, category_raw, source_file, source_path, source_period, source_row, "
    "imported_at, parser_version, txn_fingerprint, day_of_week, day_of_month, "
    "week_of_year, is_weekend, is_month_start, is_month_end"
)


def _add_expense(
    path: Path,
    *,
    txn_id: str,
    posted: str,
    amount: str,
    merchant: str = "SURFACE MARKET",
    category: str = "Groceries",
    imported_at: str = "2026-02-02 03:04:05.000000",
    source_period: str = "2027-03",
) -> str:
    """Insert one reportable CAD expense of `amount` magnitude.

    `source_period` is the STATEMENT month, which `ledger.changes` groups by and
    which is independent of the posting date.
    """
    identifier = _identifier(txn_id, length=40)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            f"INSERT INTO transactions ({_TRANSACTION_COLUMNS}) VALUES "
            "(?, ?, 'harbor', 'harbor-chequing-9101', 'checking', 'expense', 'expense', 1, "
            "'CAD', ?, ?, 'SURFACE', ?, ?, 'high', 'high', 0, '', '', 'f.csv', '/f.csv', "
            "?, 1, ?, 'v1', ?, 3, 3, 9, 0, 0, 0)",
            (
                identifier,
                posted,
                amount,
                f"-{amount}",
                merchant,
                category,
                source_period,
                imported_at,
                identifier[:32],
            ),
        )
    return identifier


def _add_transfer(path: Path, *, posted: str) -> None:
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "INSERT INTO transfers (txn_id, posting_date, institution, account, "
            "account_type, flow, amount_raw, currency, description, source_file, "
            "source_path, imported_at) VALUES "
            "(?, ?, 'harbor', 'harbor-chequing-9101', 'checking', 'transfer', 25.00, 'CAD', "
            "'SURFACE TRANSFER', 'f.csv', '/f.csv', '2026-06-01 00:00:00.000000')",
            (_identifier(f"transfer-{posted}", length=40), posted),
        )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return build_synthetic_mcp_database(tmp_path / "ledger.db")


@pytest.fixture
def service(db_path: Path) -> LedgerMCPService:
    built = LedgerMCPService(db_path)
    yield built
    built.close()


def _agent(db_path: Path) -> LedgerQueryService:
    database = ReadOnlyDatabase(db_path)
    snapshot = database.preflight()
    return LedgerQueryService(
        db_path,
        session_scope_factory=database.session_scope,
        snapshot_generation=snapshot.generation,
    )


def _data(result: dict[str, object]) -> dict[str, object]:
    assert result.get("isError") is not True, result
    return result["structuredContent"]["data"]  # type: ignore[index]


def _spread(db_path: Path) -> None:
    """Three magnitudes whose lexical order differs from their numeric order."""
    _add_expense(db_path, txn_id="spread-9", posted="2027-03-01", amount="9.00")
    _add_expense(db_path, txn_id="spread-50", posted="2027-03-02", amount="50.00")
    _add_expense(db_path, txn_id="spread-100", posted="2027-03-03", amount="100.00")


def _spending(service: LedgerMCPService, **overrides: object) -> dict[str, object]:
    arguments: dict[str, object] = {
        "start_date": "2027-03-01",
        "end_date": "2027-03-31",
        "flow": "spending",
        "group_by": "none",
        "limit": 100,
    }
    return _data(call_tool(service, "ledger.aggregate", {**arguments, **overrides}))


def _search(service: LedgerMCPService, **overrides: object) -> dict[str, object]:
    arguments: dict[str, object] = {
        "start_date": "2027-03-01",
        "end_date": "2027-03-31",
        "flow": "spending",
        "limit": 100,
    }
    return _data(call_tool(service, "ledger.transactions.search", {**arguments, **overrides}))


# --- 1. amount filters ------------------------------------------------------


def test_min_amount_filters_on_the_reported_spending_magnitude(
    db_path: Path,
    service: LedgerMCPService,
) -> None:
    _spread(db_path)

    kept = _spending(service, min_amount="50.00")

    assert kept["total"] == "150.00"
    assert kept["transaction_count"] == 2


def test_max_amount_filters_on_the_reported_spending_magnitude(
    db_path: Path,
    service: LedgerMCPService,
) -> None:
    _spread(db_path)

    kept = _spending(service, max_amount="50.00")

    assert kept["total"] == "59.00"
    assert kept["transaction_count"] == 2


def test_amount_bounds_are_inclusive_and_compose(
    db_path: Path,
    service: LedgerMCPService,
) -> None:
    _spread(db_path)

    kept = _spending(service, min_amount="50.00", max_amount="50.00")

    assert kept["total"] == "50.00"
    assert kept["transaction_count"] == 1


def test_amount_filters_narrow_the_groups_and_the_evidence_alike(
    db_path: Path,
    service: LedgerMCPService,
) -> None:
    _spread(db_path)

    grouped = _spending(service, group_by="merchant", min_amount="100.00")
    evidence = _search(service, min_amount="100.00")

    assert [group["amount"] for group in grouped["groups"]] == ["100.00"]  # type: ignore[index]
    assert [item["amount"] for item in evidence["items"]] == ["100.00"]  # type: ignore[index]


def test_income_amount_filters_compare_the_positive_reported_amount(db_path: Path) -> None:
    service = _agent(db_path)

    kept = service.agent.aggregate_transaction_facts(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 12, 31),
        flow="income",
        group_by="none",
        filters=AgentFilters(sources=("NORTHSTAR UNIVERSITY",)),
        min_amount=Decimal("2000.00"),
    )

    assert kept.total == Decimal("2141.49")
    assert kept.transaction_count == 1


def test_min_amount_above_max_amount_is_an_invalid_argument(service: LedgerMCPService) -> None:
    result = call_tool(
        service,
        "ledger.aggregate",
        {
            "start_date": "2027-03-01",
            "end_date": "2027-03-31",
            "flow": "spending",
            "group_by": "none",
            "min_amount": "50.00",
            "max_amount": "49.99",
        },
    )

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "invalid_argument"  # type: ignore[index]


def test_a_malformed_amount_is_refused_rather_than_coerced(service: LedgerMCPService) -> None:
    # The last is fullwidth digits: NFKC would fold them to 5.00, and amounts are
    # deliberately not normalized the way labels are.
    for value in ("-5.00", "5", "5.000", "1e3", "\uff15.\uff10\uff10"):
        result = call_tool(
            service,
            "ledger.transactions.search",
            {
                "start_date": "2027-03-01",
                "end_date": "2027-03-31",
                "flow": "spending",
                "min_amount": value,
            },
        )
        assert result["isError"] is True, value
        assert result["structuredContent"]["error"]["code"] == "invalid_argument"  # type: ignore[index]


# --- 2. order ---------------------------------------------------------------


def test_search_orders_by_date_ascending_by_default(
    db_path: Path,
    service: LedgerMCPService,
) -> None:
    _spread(db_path)

    default = _search(service)
    explicit = _search(service, order="date_asc")

    assert default["items"] == explicit["items"]
    assert [item["posting_date"] for item in default["items"]] == [  # type: ignore[index]
        "2027-03-01",
        "2027-03-02",
        "2027-03-03",
    ]


@pytest.mark.parametrize(
    ("order", "expected"),
    [
        ("date_asc", ["9.00", "50.00", "100.00"]),
        ("date_desc", ["100.00", "50.00", "9.00"]),
        ("amount_desc", ["100.00", "50.00", "9.00"]),
        ("amount_asc", ["9.00", "50.00", "100.00"]),
    ],
)
def test_every_order_ranks_by_the_magnitude_the_tool_reports(
    db_path: Path,
    service: LedgerMCPService,
    order: str,
    expected: list[str],
) -> None:
    """Lexical ordering would put 100.00 before 50.00 before 9.00."""
    _spread(db_path)

    page = _search(service, order=order)

    assert [item["amount"] for item in page["items"]] == expected  # type: ignore[index]


@pytest.mark.parametrize("order", ["date_asc", "date_desc", "amount_desc", "amount_asc"])
def test_pagination_under_every_order_returns_each_row_exactly_once(
    db_path: Path,
    service: LedgerMCPService,
    order: str,
) -> None:
    """Same date, same amount: only txn_id can make the order total."""
    for index in range(5):
        _add_expense(db_path, txn_id=f"tie-{index}", posted="2027-03-09", amount="7.77")
    seen: list[str] = []
    cursor: object = None

    while True:
        arguments: dict[str, object] = {
            "start_date": "2027-03-09",
            "end_date": "2027-03-09",
            "flow": "spending",
            "limit": 2,
            "order": order,
        }
        if cursor is not None:
            arguments["cursor"] = cursor
        page = call_tool(service, "ledger.transactions.search", arguments)
        envelope = page["structuredContent"]
        assert page.get("isError") is not True, page
        seen.extend(item["evidence_id"] for item in envelope["data"]["items"])  # type: ignore[index]
        cursor = envelope["page"]["next_cursor"]  # type: ignore[index]
        if cursor is None:
            break

    assert len(seen) == 5
    assert len(set(seen)) == 5


def test_an_unknown_order_is_refused(service: LedgerMCPService) -> None:
    result = call_tool(
        service,
        "ledger.transactions.search",
        {
            "start_date": "2027-03-01",
            "end_date": "2027-03-31",
            "flow": "spending",
            "order": "amount",
        },
    )

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "invalid_argument"  # type: ignore[index]


def test_aggregate_has_no_order_input(service: LedgerMCPService) -> None:
    result = call_tool(
        service,
        "ledger.aggregate",
        {
            "start_date": "2027-03-01",
            "end_date": "2027-03-31",
            "flow": "spending",
            "group_by": "none",
            "order": "date_desc",
        },
    )

    assert result["isError"] is True


# --- 3. group_by day, and chronological time series -------------------------


def test_day_grouping_returns_one_key_per_posting_date(
    db_path: Path,
    service: LedgerMCPService,
) -> None:
    _spread(db_path)

    grouped = _spending(service, group_by="day")

    assert [(group["key"], group["amount"]) for group in grouped["groups"]] == [  # type: ignore[index]
        ("2027-03-01", "9.00"),
        ("2027-03-02", "50.00"),
        ("2027-03-03", "100.00"),
    ]


def test_month_grouping_is_chronological_not_amount_ranked(
    db_path: Path,
    service: LedgerMCPService,
) -> None:
    """A time series read as a ranking misreads the trend it is asked for."""
    _add_expense(db_path, txn_id="ts-jan", posted="2027-01-05", amount="10.00")
    _add_expense(db_path, txn_id="ts-feb", posted="2027-02-05", amount="900.00")
    _add_expense(db_path, txn_id="ts-mar", posted="2027-03-05", amount="20.00")

    grouped = _spending(
        service,
        start_date="2027-01-01",
        end_date="2027-03-31",
        group_by="month",
    )

    assert [group["key"] for group in grouped["groups"]] == [  # type: ignore[index]
        "2027-01",
        "2027-02",
        "2027-03",
    ]


def test_day_grouping_is_chronological_not_amount_ranked(
    db_path: Path,
    service: LedgerMCPService,
) -> None:
    _add_expense(db_path, txn_id="day-one", posted="2027-03-01", amount="10.00")
    _add_expense(db_path, txn_id="day-two", posted="2027-03-02", amount="900.00")
    _add_expense(db_path, txn_id="day-three", posted="2027-03-03", amount="20.00")

    grouped = _spending(service, group_by="day")

    assert [group["key"] for group in grouped["groups"]] == [  # type: ignore[index]
        "2027-03-01",
        "2027-03-02",
        "2027-03-03",
    ]


def test_category_grouping_stays_amount_ranked(
    db_path: Path,
    service: LedgerMCPService,
) -> None:
    _add_expense(db_path, txn_id="rank-small", posted="2027-03-01", amount="10.00", category="Aaa")
    _add_expense(db_path, txn_id="rank-big", posted="2027-03-02", amount="900.00", category="Zzz")

    grouped = _spending(service, group_by="category")

    assert [group["key"] for group in grouped["groups"]] == ["Zzz", "Aaa"]  # type: ignore[index]


# --- 4. unmatched filter values ---------------------------------------------


def test_a_filter_value_that_matched_nothing_is_reported(service: LedgerMCPService) -> None:
    """'Restaurants' against 'Restaurants & Dining' used to return a silent zero."""
    matching = _spending(
        service,
        start_date="2025-01-01",
        end_date="2025-12-31",
        filters={"categories": ["Groceries", "Restaurants"]},
    )["matching"]

    assert matching["matched_categories"] == ["Groceries"]  # type: ignore[index]
    assert matching["unmatched_categories"] == ["Restaurants"]  # type: ignore[index]
    assert matching["unmatched_institutions"] == []  # type: ignore[index]
    assert matching["unmatched_sources"] == []  # type: ignore[index]


def test_unmatched_values_are_reported_as_the_caller_supplied_them(
    service: LedgerMCPService,
) -> None:
    matching = _spending(
        service,
        start_date="2025-01-01",
        end_date="2025-12-31",
        filters={"sources": ["  Näme  Not  Here  "], "institutions": ["No Such Bank"]},
    )["matching"]

    assert matching["unmatched_sources"] == ["  Näme  Not  Here  "]  # type: ignore[index]
    assert matching["unmatched_institutions"] == ["No Such Bank"]  # type: ignore[index]


def test_a_normalized_hit_is_matched_not_unmatched(service: LedgerMCPService) -> None:
    matching = _spending(
        service,
        start_date="2025-01-01",
        end_date="2025-12-31",
        flow="income",
        filters={"sources": ["  northstar   university  "]},
    )["matching"]

    assert matching["matched_sources"] == ["NORTHSTAR UNIVERSITY"]  # type: ignore[index]
    assert matching["unmatched_sources"] == []  # type: ignore[index]


def test_evidence_search_reports_unmatched_values_too(service: LedgerMCPService) -> None:
    matching = _search(
        service,
        start_date="2025-01-01",
        end_date="2025-12-31",
        filters={"categories": ["Restaurants"]},
    )["matching"]

    assert matching["unmatched_categories"] == ["Restaurants"]  # type: ignore[index]


# --- 5. ledger.transaction.get ----------------------------------------------


def test_transaction_get_returns_exactly_the_item_search_emits(
    service: LedgerMCPService,
) -> None:
    page = _search(
        service,
        start_date="2025-01-01",
        end_date="2025-12-31",
        flow="income",
        filters={"sources": ["NORTHSTAR UNIVERSITY"]},
    )
    expected = page["items"][0]  # type: ignore[index]

    fetched = _data(
        call_tool(service, "ledger.transaction.get", {"evidence_id": expected["evidence_id"]})
    )

    assert fetched["item"] == expected


def test_transaction_get_exposes_no_account_path_or_raw_description(
    service: LedgerMCPService,
) -> None:
    import json

    page = _search(service, start_date="2025-01-01", end_date="2025-12-31", flow="spending")
    fetched = call_tool(
        service,
        "ledger.transaction.get",
        {"evidence_id": page["items"][0]["evidence_id"]},  # type: ignore[index]
    )
    encoded = json.dumps(fetched, ensure_ascii=False)

    assert set(_data(fetched)["item"]) == {  # type: ignore[arg-type]
        "amount",
        "cashflow_type",
        "category",
        "currency",
        "evidence_id",
        "flow",
        "institution",
        "posting_date",
        "source",
    }
    for forbidden in (PRIVATE_ACCOUNT_NUMBER, "synthetic/synthetic.csv", "SYNTHETIC market-one"):
        assert forbidden not in encoded


def test_an_unknown_evidence_id_is_a_clean_not_found(service: LedgerMCPService) -> None:
    result = call_tool(service, "ledger.transaction.get", {"evidence_id": "0" * 40})

    assert result["isError"] is True
    assert result["structuredContent"] == {
        "schema": "ledger.mcp.v1",
        "error": {"code": "invalid_argument", "retryable": False},
    }


@pytest.mark.parametrize(
    "label",
    [
        "northstar-usd",
        "northstar-no-cashflow",
        "northstar-excluded-treatment",
        "market-excluded",
    ],
)
def test_a_policy_hidden_row_is_indistinguishable_from_a_missing_one(
    service: LedgerMCPService,
    label: str,
) -> None:
    """It must not become an existence oracle for rows search will not return."""
    hidden = call_tool(
        service,
        "ledger.transaction.get",
        {"evidence_id": _identifier(label, length=40)},
    )
    missing = call_tool(service, "ledger.transaction.get", {"evidence_id": "0" * 40})

    assert hidden["structuredContent"] == missing["structuredContent"]


@pytest.mark.parametrize(
    "evidence_id",
    ["", "0" * 39, "0" * 41, "Z" * 40, "0" * 20 + "A" * 20],
)
def test_a_malformed_evidence_id_is_refused(
    service: LedgerMCPService,
    evidence_id: str,
) -> None:
    result = call_tool(service, "ledger.transaction.get", {"evidence_id": evidence_id})

    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "invalid_argument"  # type: ignore[index]


def test_transaction_get_takes_no_other_argument(service: LedgerMCPService) -> None:
    result = call_tool(
        service,
        "ledger.transaction.get",
        {"evidence_id": "0" * 40, "flow": "income"},
    )

    assert result["isError"] is True


# --- 6. snapshot age --------------------------------------------------------


def test_status_reports_the_age_of_the_pinned_snapshot(
    db_path: Path,
    service: LedgerMCPService,
) -> None:
    latest = _data(call_tool(service, "ledger.status", {}))["latest_imported_at"]

    assert latest == "2030-01-01T00:00:00"


def test_the_snapshot_age_moves_with_the_pinned_database(db_path: Path) -> None:
    _add_expense(
        db_path,
        txn_id="later-import",
        posted="2027-03-01",
        amount="1.00",
        imported_at="2031-05-06 07:08:09.000000",
    )
    service = LedgerMCPService(db_path)
    try:
        latest = _data(call_tool(service, "ledger.status", {}))["latest_imported_at"]
    finally:
        service.close()

    assert latest == "2031-05-06T07:08:09"


def test_a_statement_that_produced_no_rows_still_dates_the_snapshot(
    tmp_path: Path,
) -> None:
    """The stamp is taken over BOTH stamped tables, not over `transactions`.

    A statement that produced no rows leaves nothing in `transactions` at all, and
    finalize's single coverage stamp is written after every per-file parse stamp,
    so a maximum over transactions alone names an instant the import had not
    finished at. `ledger.changes` takes this exact value as its boundary, so a
    lagging one made the documented loop re-report its own import forever.
    """
    db_path = build_synthetic_mcp_database(tmp_path / "rolumen-deleted.db")
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("DELETE FROM transactions")
    service = LedgerMCPService(db_path)
    try:
        data = _data(call_tool(service, "ledger.status", {}))
    finally:
        service.close()

    assert data["transaction_count"] == 0
    assert data["latest_imported_at"] == "2030-01-01T00:00:00"


def test_an_empty_ledger_reports_a_null_snapshot_age(tmp_path: Path) -> None:
    db_path = build_synthetic_mcp_database(tmp_path / "empty.db")
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("DELETE FROM transactions")
        conn.execute("DELETE FROM statement_coverage")
    service = LedgerMCPService(db_path)
    try:
        data = _data(call_tool(service, "ledger.status", {}))
    finally:
        service.close()

    assert data["latest_imported_at"] is None


# --- 7. coverage honesty ----------------------------------------------------


def test_a_zero_match_filter_is_distinguishable_from_an_empty_period(
    service: LedgerMCPService,
) -> None:
    arguments = {
        "start_date": "2025-01-01",
        "end_date": "2025-06-30",
        "flow": "spending",
        "group_by": "none",
    }

    matched = call_tool(service, "ledger.aggregate", arguments)
    unmatched = call_tool(
        service,
        "ledger.aggregate",
        {**arguments, "filters": {"categories": ["Restaurants"]}},
    )

    assert matched["structuredContent"]["coverage"]["relation"] == "within_available_range"  # type: ignore[index]
    assert (
        unmatched["structuredContent"]["coverage"]["relation"]  # type: ignore[index]
        == "within_available_range_no_matching_rows"
    )


def test_the_zero_match_relation_applies_to_evidence_search_too(
    service: LedgerMCPService,
) -> None:
    result = call_tool(
        service,
        "ledger.transactions.search",
        {
            "start_date": "2025-01-01",
            "end_date": "2025-06-30",
            "flow": "spending",
            "filters": {"categories": ["Restaurants"]},
        },
    )

    assert (
        result["structuredContent"]["coverage"]["relation"]  # type: ignore[index]
        == "within_available_range_no_matching_rows"
    )


def test_a_period_outside_the_range_keeps_its_own_relation(service: LedgerMCPService) -> None:
    result = call_tool(
        service,
        "ledger.aggregate",
        {
            "start_date": "2040-01-01",
            "end_date": "2040-12-31",
            "flow": "spending",
            "group_by": "none",
        },
    )

    assert result["structuredContent"]["coverage"]["relation"] == "outside_available_range"  # type: ignore[index]


# --- 8. transfers in the available range ------------------------------------


def test_the_available_range_covers_transfers_as_well_as_transactions(db_path: Path) -> None:
    """The available range covers transfers as well as transactions."""
    _add_transfer(db_path, posted="2026-05-05")
    database = ReadOnlyDatabase(db_path)
    try:
        snapshot = database.preflight()
    finally:
        database.close()

    assert snapshot.latest_date == date(2026, 5, 5)


def test_an_early_transfer_widens_the_start_of_the_range(db_path: Path) -> None:
    _add_transfer(db_path, posted="2019-01-01")
    database = ReadOnlyDatabase(db_path)
    try:
        snapshot = database.preflight()
    finally:
        database.close()

    assert snapshot.earliest_date == date(2019, 1, 1)


def test_transaction_count_still_counts_only_transactions(db_path: Path) -> None:
    database = ReadOnlyDatabase(db_path)
    try:
        before = database.preflight().transaction_count
    finally:
        database.close()

    _add_transfer(db_path, posted="2026-05-05")

    database = ReadOnlyDatabase(db_path)
    try:
        after = database.preflight().transaction_count
    finally:
        database.close()

    assert after == before


# --- 9. a too-broad filter has its own code ---------------------------------


def test_a_filter_matching_more_stored_values_than_the_contract_carries(
    db_path: Path,
    service: LedgerMCPService,
) -> None:
    """A content-free `invalid_argument` reads as a malformed request.

    It is not: the request was well formed and the filter was too broad, which is
    the caller's cue to narrow it rather than to rewrite it.
    """
    for index in range(1, 52):
        _add_expense(
            db_path,
            txn_id=f"broad-{index}",
            posted="2027-03-01",
            amount="1.00",
            merchant=f"BROAD{' ' * index}SOURCE",
        )

    result = call_tool(
        service,
        "ledger.aggregate",
        {
            "start_date": "2027-03-01",
            "end_date": "2027-03-31",
            "flow": "spending",
            "group_by": "source",
            "filters": {"sources": ["BROAD SOURCE"]},
        },
    )

    assert result["isError"] is True
    assert result["structuredContent"] == {
        "schema": "ledger.mcp.v1",
        "error": {"code": "filter_too_broad", "retryable": False},
    }


# --- 10. advertised match semantics -----------------------------------------


def test_the_published_semantics_name_the_whitespace_collapse_it_performs(
    service: LedgerMCPService,
) -> None:
    """A client reimplementing "NFKC plus casefold" gets multi-word inputs wrong."""
    from ledger.query.agent import MATCH_SEMANTICS, _normalize_match

    matching = _spending(
        service,
        start_date="2025-01-01",
        end_date="2025-12-31",
        flow="income",
        filters={"sources": ["northstar    university"]},
    )["matching"]

    assert _normalize_match("northstar    university") == "northstar university"
    assert "whitespace" in MATCH_SEMANTICS
    assert matching["semantics"] == MATCH_SEMANTICS  # type: ignore[index]
    assert matching["matched_sources"] == ["NORTHSTAR UNIVERSITY"]  # type: ignore[index]


def test_the_mcp_reference_documents_the_rule_the_code_applies() -> None:
    readme = (Path(__file__).resolve().parents[1] / "docs/agent/mcp.md").read_text(encoding="utf-8")

    assert "whitespace" in readme.casefold()


# --- 11. cursor binding for every new input ---------------------------------


def _minted(service: LedgerMCPService, tool: str, arguments: dict[str, object]) -> str:
    first = call_tool(service, tool, {**arguments, "limit": 1})
    cursor = first["structuredContent"]["page"]["next_cursor"]  # type: ignore[index]
    assert cursor, first
    return str(cursor)


def _replayed(
    service: LedgerMCPService,
    tool: str,
    arguments: dict[str, object],
    cursor: str,
) -> dict[str, object]:
    return call_tool(service, tool, {**arguments, "limit": 1, "cursor": cursor})


_BOUND_PERIOD: dict[str, object] = {
    "start_date": "2027-01-01",
    "end_date": "2027-03-31",
    "flow": "spending",
}


@pytest.mark.parametrize(
    ("tool", "base", "changed"),
    [
        ("ledger.aggregate", {**_BOUND_PERIOD, "group_by": "source"}, {"min_amount": "1.00"}),
        ("ledger.aggregate", {**_BOUND_PERIOD, "group_by": "source"}, {"max_amount": "999.00"}),
        ("ledger.aggregate", {**_BOUND_PERIOD, "group_by": "month"}, {"group_by": "day"}),
        # The account grouping keys on a derived ref rather than a stored value,
        # so its offsets address an entirely different set of rows.
        (
            "ledger.aggregate",
            {**_BOUND_PERIOD, "group_by": "source"},
            {"group_by": "account_ref"},
        ),
        ("ledger.transactions.search", _BOUND_PERIOD, {"min_amount": "1.00"}),
        ("ledger.transactions.search", _BOUND_PERIOD, {"max_amount": "999.00"}),
        ("ledger.transactions.search", _BOUND_PERIOD, {"order": "amount_desc"}),
        # `flow` was already bound and is now three-valued: `any` is a union of
        # both predicate sets, so the row an offset addresses moves.
        ("ledger.transactions.search", _BOUND_PERIOD, {"flow": "any"}),
    ],
)
def test_a_cursor_is_refused_when_a_new_input_changes(
    db_path: Path,
    service: LedgerMCPService,
    tool: str,
    base: dict[str, object],
    changed: dict[str, object],
) -> None:
    """An offset addresses a different row once any of these moves."""
    _spread(db_path)
    _add_expense(db_path, txn_id="bind-jan", posted="2027-01-04", amount="7.00", merchant="OTHER")
    _add_expense(db_path, txn_id="bind-feb", posted="2027-02-04", amount="8.00", merchant="OTHER")
    cursor = _minted(service, tool, base)

    replayed = _replayed(service, tool, {**base, **changed}, cursor)

    assert replayed["isError"] is True
    assert replayed["structuredContent"]["error"]["code"] == "invalid_cursor"  # type: ignore[index]


def test_an_unchanged_query_still_paginates(
    db_path: Path,
    service: LedgerMCPService,
) -> None:
    _spread(db_path)
    base = {
        "start_date": "2027-03-01",
        "end_date": "2027-03-31",
        "flow": "spending",
        "min_amount": "1.00",
        "order": "amount_desc",
    }
    cursor = _minted(service, "ledger.transactions.search", base)

    second = _replayed(service, "ledger.transactions.search", base, cursor)

    assert second.get("isError") is not True, second
    assert _data(second)["items"]


def _changes_base(service: LedgerMCPService, db_path: Path) -> dict[str, object]:
    """Three changed account-periods, and the boundary/generation that select them."""
    for index, period in enumerate(("2027-01", "2027-02", "2027-03")):
        _add_expense(
            db_path,
            txn_id=f"changed-{period}",
            posted=f"{period}-04",
            amount="3.00",
            imported_at=f"2030-02-0{index + 1} 00:00:00.000000",
            source_period=period,
        )
    status = call_tool(service, "ledger.status", {})
    return {
        "since": "2030-01-01T00:00:00",
        "client_generation": status["structuredContent"]["coverage"]["generation"],  # type: ignore[index]
    }


@pytest.mark.parametrize(
    "changed",
    [{"since": "2030-01-15T00:00:00"}, {"client_generation": "f" * 16}],
    ids=["since", "client_generation"],
)
def test_the_changes_boundary_and_its_generation_are_both_bound_to_the_cursor(
    db_path: Path,
    service: LedgerMCPService,
    changed: dict[str, object],
) -> None:
    """Every INPUT is bound, whether or not it selects rows.

    A boundary change renumbers the entries, so replaying an old offset against a
    new one is a silently different answer under a valid-looking cursor.
    ``client_generation`` selects nothing -- it is echoed and compared, and the
    counts are identical with it correct, wrong, or absent -- but the cursor law
    binds every input rather than every input that happens to matter today. An
    earlier design DID gate the answer on it; that gate was withdrawn because any
    write rotates the generation, so it fired on exactly the imports the surface
    exists to report. The binding stays.
    """
    base = _changes_base(service, db_path)
    cursor = _minted(service, "ledger.changes", base)

    replayed = _replayed(service, "ledger.changes", {**base, **changed}, cursor)

    assert replayed["isError"] is True
    assert replayed["structuredContent"]["error"]["code"] == "invalid_cursor"  # type: ignore[index]


def test_an_unchanged_changes_boundary_still_paginates(
    db_path: Path,
    service: LedgerMCPService,
) -> None:
    """The other half: binding must not refuse a legitimately identical replay."""
    base = _changes_base(service, db_path)
    cursor = _minted(service, "ledger.changes", base)

    second = _replayed(service, "ledger.changes", base, cursor)

    assert second.get("isError") is not True, second
    assert _data(second)["entries"]


def test_the_default_order_binds_the_same_cursor_as_an_explicit_one(
    db_path: Path,
    service: LedgerMCPService,
) -> None:
    """`order` defaults to date_asc, so omitting it must not mint a second shape."""
    _spread(db_path)
    base: dict[str, object] = {
        "start_date": "2027-03-01",
        "end_date": "2027-03-31",
        "flow": "spending",
    }
    cursor = _minted(service, "ledger.transactions.search", base)

    replayed = _replayed(
        service,
        "ledger.transactions.search",
        {**base, "order": "date_asc"},
        cursor,
    )

    assert replayed.get("isError") is not True, replayed


def test_the_agent_layer_rejects_an_inverted_amount_window(db_path: Path) -> None:
    service = _agent(db_path)

    with pytest.raises(AgentQueryError, match="invalid_argument"):
        service.agent.search_transaction_evidence(
            start_date=date(2025, 1, 1),
            end_date=date(2025, 12, 31),
            flow="spending",
            filters=AgentFilters(),
            min_amount=Decimal("10.00"),
            max_amount=Decimal("9.99"),
        )
