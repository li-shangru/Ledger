"""Source-backed read models for historical investment statements."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import event

from ledger.investments.models import (
    InvestmentAccount,
    InvestmentActivity,
    InvestmentActivityType,
    InvestmentAssetBalance,
    InvestmentPerformance,
    InvestmentPosition,
    InvestmentStatement,
)
from ledger.investments.repository import InvestmentRepository
from ledger.query import LedgerQueryService
from ledger.storage.database import (
    InvestmentPerformanceDB,
    get_engine,
    session_scope,
)


def synthetic_statement(
    *, alias: str, period_end: date, nav: str, symbol: str, activity_type: InvestmentActivityType
) -> InvestmentStatement:
    account = InvestmentAccount(
        account_id=f"account-{alias}",
        institution="broker",
        source_account_id=f"opaque-{alias}",
        account_alias=alias,
        account_type="registered",
        base_currency="USD",
        status="closed",
        transfer_destination="destination",
        status_confirmed_on=date(2026, 8, 1),
    )
    period_start = date(period_end.year, period_end.month, 1)
    return InvestmentStatement(
        statement_id=f"statement-{alias}-{period_end.isoformat()}",
        account=account,
        period_start=period_start,
        period_end=period_end,
        generated_at=datetime.combine(period_end, datetime.min.time()),
        base_currency="USD",
        total_nav=Decimal(nav),
        time_weighted_return=Decimal("0.010000000000"),
        asset_balances=(
            InvestmentAssetBalance(
                asset_class="Stocks",
                prior_total=Decimal("0"),
                current_long=Decimal(nav),
                current_short=Decimal("0"),
                current_total=Decimal(nav),
                change=Decimal(nav),
            ),
        ),
        positions=(
            InvestmentPosition(
                asset_category="Stocks",
                currency="USD",
                symbol=symbol,
                discriminator="Summary",
                quantity=Decimal("1.123456789"),
                close_price=Decimal(nav),
                market_value=Decimal(nav),
                source_row=10,
            ),
        ),
        performance=(
            InvestmentPerformance(
                asset_category="Total",
                symbol="",
                cost_adjustment=Decimal("0"),
                realized_short_profit=Decimal("2.5"),
                realized_short_loss=Decimal("-0.5"),
                realized_long_profit=Decimal("0"),
                realized_long_loss=Decimal("0"),
                realized_total=Decimal("2"),
                unrealized_short_profit=Decimal("3"),
                unrealized_short_loss=Decimal("-1"),
                unrealized_long_profit=Decimal("0"),
                unrealized_long_loss=Decimal("0"),
                unrealized_total=Decimal("2"),
                total=Decimal("4"),
                code="",
                source_row=15,
            ),
        ),
        activities=(
            InvestmentActivity(
                activity_id=f"activity-{alias}-{period_end.isoformat()}",
                activity_type=activity_type,
                activity_date=period_end,
                symbol=symbol,
                currency="USD",
                amount=Decimal("1.123456789"),
                source_row=20,
            ),
        ),
        parsed_fingerprint=(alias[0].lower() + str(period_end.month)) * 32,
    )


def populate_investments(db_path: Path) -> tuple[InvestmentStatement, InvestmentStatement]:
    repo = InvestmentRepository(db_path)
    first = synthetic_statement(
        alias="TFSA",
        period_end=date(2026, 6, 30),
        nav="100.123456789",
        symbol="AAA",
        activity_type=InvestmentActivityType.DIVIDEND,
    )
    second = synthetic_statement(
        alias="RRSP",
        period_end=date(2026, 7, 31),
        nav="200.123456789",
        symbol="BBB",
        activity_type=InvestmentActivityType.INTEREST,
    )
    later_tfsa = synthetic_statement(
        alias="TFSA",
        period_end=date(2026, 7, 31),
        nav="110.123456789",
        symbol="AAA",
        activity_type=InvestmentActivityType.DIVIDEND,
    )
    for index, value in enumerate((first, second, later_tfsa)):
        repo.store_statement(
            value,
            source_csv_path=Path(f"/synthetic/{index}.csv"),
            source_csv_sha256=str(index + 1) * 64,
            source_pdf_path=Path(f"/synthetic/{index}.pdf"),
            source_pdf_sha256=str(index + 4) * 64,
        )
    return first, second


def test_investment_currency_scopes_keep_cad_and_usd_separate(tmp_path) -> None:
    """Equal-date CAD and USD statements never form a monetary grand total."""
    db_path = tmp_path / "ledger.db"
    repository = InvestmentRepository(db_path)
    for index, currency in enumerate(("CAD", "USD")):
        statement = synthetic_statement(
            alias=currency,
            period_end=date(2026, 7, 31),
            nav="123.456789012345" if currency == "CAD" else "234.567890123456",
            symbol=f"{currency}STOCK",
            activity_type=InvestmentActivityType.DIVIDEND,
        )
        statement = statement.model_copy(
            update={
                "base_currency": currency,
                "account": statement.account.model_copy(update={"base_currency": currency}),
                "performance": () if currency == "CAD" else statement.performance,
                "time_weighted_return": None
                if currency == "CAD"
                else statement.time_weighted_return,
                "activities": (statement.activities[0].model_copy(update={"currency": currency}),),
                "positions": (statement.positions[0].model_copy(update={"currency": currency}),),
            }
        )
        repository.store_statement(
            statement,
            source_csv_path=Path(f"/synthetic/currency-{index}.csv"),
            source_csv_sha256=str(index + 1) * 64,
            source_pdf_path=Path(f"/synthetic/currency-{index}.pdf"),
            source_pdf_sha256=str(index + 4) * 64,
        )

    service = LedgerQueryService(db_path)
    history = service.get_investment_nav_history()
    assert {row["base_currency"]: row["total_nav"] for row in history} == {
        "CAD": Decimal("123.456789012345"),
        "USD": Decimal("234.567890123456"),
    }
    assert all(row["contributing_accounts"] == 1 for row in history)
    assert len(service.get_investment_nav_history(base_currency="CAD")) == 1
    assert [
        row["base_currency"] for row in service.get_investment_nav_series(base_currency="CAD")
    ] == ["CAD"]
    performance = service.get_investment_performance(base_currency="CAD")
    assert len(performance) == 1
    assert performance[0]["base_currency"] == "CAD"
    assert performance[0]["realized_pl"] is None
    assert performance[0]["unrealized_pl"] is None
    assert performance[0]["time_weighted_return"] is None
    assert service.get_investment_activity_summary(base_currency="CAD", currency="CAD") == {
        "net_deposits_withdrawals": Decimal("0"),
        "dividends_interest": Decimal("1.123456789"),
    }
    assert service.get_investment_activity_summary(base_currency="CAD")[
        "dividends_interest"
    ] == Decimal("0")
    assert service.get_investment_activity_summary()["dividends_interest"] == Decimal("1.123456789")
    assert service.get_investment_activity_symbols(base_currency="CAD") == ["CADSTOCK"]
    assert [row["currency"] for row in service.get_investment_activities(base_currency="CAD")] == [
        "CAD"
    ]


def test_investment_queries_keep_nav_coverage_and_returns_account_scoped(tmp_path) -> None:
    """Combined NAV reports only exact source dates and never invents a return."""
    first, second = populate_investments(tmp_path / "ledger.db")
    service = LedgerQueryService(tmp_path / "ledger.db")

    assert service.investments is service
    assert service.get_investment_date_range() == (date(2026, 6, 30), date(2026, 7, 31))
    accounts = service.get_investment_accounts()
    assert [row["account_alias"] for row in accounts] == ["RRSP", "TFSA"]
    history = service.get_investment_nav_history()
    assert history[0] == {
        "period_end": date(2026, 6, 30),
        "total_nav": Decimal("100.123456789"),
        "base_currency": "USD",
        "contributing_accounts": 1,
    }
    assert history[-1]["total_nav"] == Decimal("310.246913578")
    assert history[-1]["contributing_accounts"] == 2
    assert "combined_return" not in history[-1]
    series = service.get_investment_nav_series()
    assert [(row["period_end"], row["account_alias"], row["total_nav"]) for row in series] == [
        (date(2026, 6, 30), "TFSA", Decimal("100.123456789")),
        (date(2026, 7, 31), "RRSP", Decimal("200.123456789")),
        (date(2026, 7, 31), "TFSA", Decimal("110.123456789")),
    ]
    assert service.get_investment_statement(first.statement_id)["total_nav"] == first.total_nav
    first_position = service.get_investment_positions(first.statement_id)[0]
    assert first_position["quantity"] == Decimal("1.123456789")
    assert first_position["account_alias"] == "TFSA"
    latest_performance = service.get_investment_performance()[0]
    assert latest_performance["account_alias"] == "RRSP"
    assert latest_performance["realized_pl"] == Decimal("2")
    assert latest_performance["unrealized_pl"] == Decimal("2")
    assert all("combined_return" not in row for row in service.get_investment_performance())
    assert second.statement_id != first.statement_id


def test_identical_overlapping_reports_contribute_one_deterministic_account_snapshot(
    tmp_path,
) -> None:
    """Monthly and YTD reports with one end date cannot double NAV, holdings, or coverage."""
    db_path = tmp_path / "ledger.db"
    repo = InvestmentRepository(db_path)
    monthly = synthetic_statement(
        alias="TFSA",
        period_end=date(2026, 7, 31),
        nav="110.123456789",
        symbol="AAA",
        activity_type=InvestmentActivityType.DIVIDEND,
    )
    ytd = monthly.model_copy(
        update={
            "statement_id": "statement-TFSA-2026-ytd",
            "period_start": date(2026, 1, 1),
            "parsed_fingerprint": "f" * 64,
        }
    )
    for index, value in enumerate((monthly, ytd)):
        repo.store_statement(
            value,
            source_csv_path=Path(f"/synthetic/overlap-{index}.csv"),
            source_csv_sha256=("7" if index == 0 else "9") * 64,
            source_pdf_path=Path(f"/synthetic/overlap-{index}.pdf"),
            source_pdf_sha256=("8" if index == 0 else "a") * 64,
        )

    service = LedgerQueryService(db_path)
    assert service.get_investment_nav_history() == [
        {
            "period_end": date(2026, 7, 31),
            "total_nav": Decimal("110.123456789"),
            "base_currency": "USD",
            "contributing_accounts": 1,
        }
    ]
    assert service.get_investment_nav_series() == [
        {
            "period_end": date(2026, 7, 31),
            "account_alias": "TFSA",
            "base_currency": "USD",
            "total_nav": Decimal("110.123456789"),
        }
    ]
    performance = service.get_investment_performance()
    assert len(performance) == 1
    assert performance[0]["statement_id"] == monthly.statement_id
    assert len(service.get_investment_positions(performance[0]["statement_id"])) == 1


def test_investment_activity_filters_are_statement_owned_and_deterministic(tmp_path) -> None:
    """Investment filters do not fall through to banking transaction data."""
    populate_investments(tmp_path / "ledger.db")
    service = LedgerQueryService(tmp_path / "ledger.db")

    rows = service.get_investment_activities(
        account_alias="TFSA", activity_types=["dividend"], symbol="AAA"
    )

    assert len(rows) == 2
    assert all(row["account_alias"] == "TFSA" for row in rows)
    assert all(row["activity_type"] == "dividend" for row in rows)
    assert all(row["amount"] == Decimal("1.123456789") for row in rows)
    assert len(service.get_investment_activities(activity_types=None)) == 3
    assert service.get_investment_activities(activity_types=[]) == []


def test_investment_activity_filter_options_are_distinct_and_uncapped(tmp_path) -> None:
    """Filter options include values outside the bounded detail-result window."""
    db_path = tmp_path / "ledger.db"
    repo = InvestmentRepository(db_path)
    statement = synthetic_statement(
        alias="TFSA",
        period_end=date(2026, 7, 31),
        nav="1",
        symbol="COMMON",
        activity_type=InvestmentActivityType.TRADE,
    ).model_copy(
        update={
            "activities": (
                *(
                    InvestmentActivity(
                        activity_id=f"common-{index}",
                        activity_type=InvestmentActivityType.TRADE,
                        activity_date=date(2026, 7, 31),
                        symbol="COMMON",
                        currency="USD",
                        amount=Decimal("1"),
                        source_row=index,
                    )
                    for index in range(1000)
                ),
                InvestmentActivity(
                    activity_id="rare-outside-detail-cap",
                    activity_type=InvestmentActivityType.WITHHOLDING_TAX,
                    activity_date=date(2026, 7, 31),
                    symbol="RARE",
                    currency="USD",
                    amount=Decimal("-1"),
                    source_row=1000,
                ),
            ),
            "parsed_fingerprint": "9" * 64,
        }
    )
    repo.store_statement(
        statement,
        source_csv_path=Path("/synthetic/options.csv"),
        source_csv_sha256="1" * 64,
        source_pdf_path=Path("/synthetic/options.pdf"),
        source_pdf_sha256="2" * 64,
    )
    service = LedgerQueryService(db_path)

    bounded = service.get_investment_activities(limit=1000)

    assert "RARE" not in {row["symbol"] for row in bounded}
    assert service.get_investment_activity_types() == ["trade", "withholding_tax"]
    assert service.get_investment_activity_symbols() == ["COMMON", "RARE"]


def test_investment_activity_summary_is_uncapped_filtered_and_decimal_exact(tmp_path) -> None:
    """Summary totals every selected row without SQLite float loss or detail caps."""
    db_path = tmp_path / "ledger.db"
    repo = InvestmentRepository(db_path)
    statements = (
        synthetic_statement(
            alias="TFSA",
            period_end=date(2026, 12, 31),
            nav="1",
            symbol="AAA",
            activity_type=InvestmentActivityType.DEPOSIT,
        ).model_copy(
            update={
                "activities": (
                    *(
                        InvestmentActivity(
                            activity_id=f"tfsa-deposit-{index}",
                            activity_type=InvestmentActivityType.DEPOSIT,
                            activity_date=date(2026, 6, 1),
                            currency="USD",
                            amount=Decimal("1.000000000000"),
                            source_row=index,
                        )
                        for index in range(5000)
                    ),
                    InvestmentActivity(
                        activity_id="tfsa-deposit-tiny",
                        activity_type=InvestmentActivityType.DEPOSIT,
                        activity_date=date(2026, 6, 2),
                        currency="USD",
                        amount=Decimal("0.000000005001"),
                        source_row=5000,
                    ),
                    *(
                        InvestmentActivity(
                            activity_id=f"tfsa-dividend-{index}",
                            activity_type=InvestmentActivityType.DIVIDEND,
                            activity_date=date(2026, 7, 1),
                            currency="USD",
                            amount=Decimal("1.000000000000"),
                            source_row=5001 + index,
                        )
                        for index in range(25)
                    ),
                    InvestmentActivity(
                        activity_id="tfsa-dividend-tiny",
                        activity_type=InvestmentActivityType.DIVIDEND,
                        activity_date=date(2026, 7, 2),
                        currency="USD",
                        amount=Decimal("0.000000000001"),
                        source_row=5026,
                    ),
                    InvestmentActivity(
                        activity_id="tfsa-native-cad-deposit",
                        activity_type=InvestmentActivityType.DEPOSIT,
                        activity_date=date(2026, 8, 1),
                        currency="CAD",
                        amount=Decimal("700000000.000000000000"),
                        source_row=5027,
                    ),
                    InvestmentActivity(
                        activity_id="tfsa-native-cad-dividend",
                        activity_type=InvestmentActivityType.DIVIDEND,
                        activity_date=date(2026, 8, 2),
                        currency="CAD",
                        amount=Decimal("600000000.000000000000"),
                        source_row=5028,
                    ),
                ),
                "parsed_fingerprint": "a" * 64,
            }
        ),
        synthetic_statement(
            alias="RRSP",
            period_end=date(2025, 12, 31),
            nav="1",
            symbol="BBB",
            activity_type=InvestmentActivityType.DEPOSIT,
        ).model_copy(
            update={
                "activities": (
                    InvestmentActivity(
                        activity_id="rrsp-deposit-outside-account-and-date",
                        activity_type=InvestmentActivityType.DEPOSIT,
                        activity_date=date(2025, 12, 31),
                        currency="USD",
                        amount=Decimal("900000000.000000000000"),
                        source_row=1,
                    ),
                ),
                "parsed_fingerprint": "b" * 64,
            }
        ),
        synthetic_statement(
            alias="TFSA",
            period_end=date(2025, 12, 31),
            nav="1",
            symbol="AAA",
            activity_type=InvestmentActivityType.DEPOSIT,
        ).model_copy(
            update={
                "activities": (
                    InvestmentActivity(
                        activity_id="tfsa-deposit-outside-date",
                        activity_type=InvestmentActivityType.DEPOSIT,
                        activity_date=date(2025, 12, 31),
                        currency="USD",
                        amount=Decimal("800000000.000000000000"),
                        source_row=1,
                    ),
                ),
                "parsed_fingerprint": "c" * 64,
            }
        ),
    )
    for index, statement in enumerate(statements):
        repo.store_statement(
            statement,
            source_csv_path=Path(f"/synthetic/aggregate-{index}.csv"),
            source_csv_sha256=str(index + 1) * 64,
            source_pdf_path=Path(f"/synthetic/aggregate-{index}.pdf"),
            source_pdf_sha256=str(index + 4) * 64,
        )
    service = LedgerQueryService(db_path)

    summary = service.get_investment_activity_summary(
        account_alias="TFSA",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 12, 31),
    )

    assert summary == {
        "net_deposits_withdrawals": Decimal("5000.000000005001"),
        "dividends_interest": Decimal("25.000000000001"),
    }
    assert all(isinstance(value, Decimal) for value in summary.values())
    assert service.get_investment_activity_summary(account_alias="RESP") == {
        "net_deposits_withdrawals": Decimal("0.000000000000"),
        "dividends_interest": Decimal("0.000000000000"),
    }


def test_investment_performance_loads_total_rows_in_one_select(tmp_path) -> None:
    """Performance loads each statement's account total without per-row selects."""
    db_path = tmp_path / "ledger.db"
    first, second = populate_investments(db_path)
    with session_scope(db_path) as session:
        session.add_all(
            [
                InvestmentPerformanceDB(
                    statement_id=first.statement_id,
                    source_row=30,
                    asset_category="Total (All Assets)",
                    symbol="",
                    realized_total=Decimal("30.000000000000"),
                    unrealized_total=Decimal("31.000000000000"),
                    total=Decimal("61.000000000000"),
                    code="",
                ),
                InvestmentPerformanceDB(
                    statement_id=first.statement_id,
                    source_row=99,
                    asset_category="Stocks",
                    symbol="AAA",
                    realized_total=Decimal("99.000000000000"),
                    unrealized_total=Decimal("99.000000000000"),
                    total=Decimal("198.000000000000"),
                    code="",
                ),
                InvestmentPerformanceDB(
                    statement_id="statement-TFSA-2026-07-31",
                    source_row=40,
                    asset_category="Total (All Assets)",
                    symbol="",
                    realized_total=Decimal("40.000000000000"),
                    unrealized_total=Decimal("41.000000000000"),
                    total=Decimal("81.000000000000"),
                    code="",
                ),
            ]
        )

    select_count = 0

    def count_selects(_connection, _cursor, statement, _parameters, _context, _executemany):
        nonlocal select_count
        if statement.lstrip().upper().startswith("SELECT"):
            select_count += 1

    engine = get_engine(db_path)
    event.listen(engine, "before_cursor_execute", count_selects)
    try:
        rows = LedgerQueryService(db_path).get_investment_performance()
    finally:
        event.remove(engine, "before_cursor_execute", count_selects)

    assert select_count == 1
    assert [(row["statement_id"], row["realized_pl"]) for row in rows] == [
        (second.statement_id, Decimal("2.000000000000")),
        ("statement-TFSA-2026-07-31", Decimal("40.000000000000")),
        (first.statement_id, Decimal("30.000000000000")),
    ]


def test_investment_provenance_is_alias_and_state_only(tmp_path) -> None:
    """Render provenance exposes lifecycle facts but no source IDs, paths, or hashes."""
    first, _second = populate_investments(tmp_path / "ledger.db")
    service = LedgerQueryService(tmp_path / "ledger.db")

    provenance = service.get_investment_provenance(first.statement_id)

    assert provenance == {
        "account_alias": "TFSA",
        "period_start": date(2026, 6, 1),
        "period_end": date(2026, 6, 30),
        "import_state": "staged",
        "archive_verified": False,
    }
    assert "opaque-TFSA" not in repr(provenance)
    assert "/synthetic/" not in repr(provenance)


def test_investment_nav_history_sums_materialized_decimals_without_sqlite_loss(tmp_path) -> None:
    """A tiny source-backed NAV remains visible beside a large reported NAV."""
    db_path = tmp_path / "ledger.db"
    repo = InvestmentRepository(db_path)
    values = (
        synthetic_statement(
            alias="TFSA",
            period_end=date(2026, 7, 31),
            nav="100000000.000000000000",
            symbol="AAA",
            activity_type=InvestmentActivityType.DIVIDEND,
        ),
        synthetic_statement(
            alias="RRSP",
            period_end=date(2026, 7, 31),
            nav="0.000000000001",
            symbol="BBB",
            activity_type=InvestmentActivityType.INTEREST,
        ),
    )
    for index, value in enumerate(values):
        repo.store_statement(
            value,
            source_csv_path=Path(f"/synthetic/stress-{index}.csv"),
            source_csv_sha256=str(index + 1) * 64,
            source_pdf_path=Path(f"/synthetic/stress-{index}.pdf"),
            source_pdf_sha256=str(index + 3) * 64,
        )

    history = LedgerQueryService(db_path).get_investment_nav_history()

    assert history == [
        {
            "period_end": date(2026, 7, 31),
            "total_nav": Decimal("100000000.000000000001"),
            "base_currency": "USD",
            "contributing_accounts": 2,
        }
    ]


def test_investment_nav_history_sums_two_maximum_values_without_context_rounding(
    tmp_path,
) -> None:
    """Combined NAV retains every source digit when the sum exceeds precision 28."""
    db_path = tmp_path / "ledger.db"
    repo = InvestmentRepository(db_path)
    maximum = "9999999999999999.999999999999"
    for index, alias in enumerate(("TFSA", "RRSP")):
        value = synthetic_statement(
            alias=alias,
            period_end=date(2026, 7, 31),
            nav=maximum,
            symbol=f"MAX{index}",
            activity_type=InvestmentActivityType.DIVIDEND,
        )
        repo.store_statement(
            value,
            source_csv_path=Path(f"/synthetic/max-{index}.csv"),
            source_csv_sha256=str(index + 1) * 64,
            source_pdf_path=Path(f"/synthetic/max-{index}.pdf"),
            source_pdf_sha256=str(index + 3) * 64,
        )

    assert LedgerQueryService(db_path).get_investment_nav_history() == [
        {
            "period_end": date(2026, 7, 31),
            "total_nav": Decimal("19999999999999999.999999999998"),
            "base_currency": "USD",
            "contributing_accounts": 2,
        }
    ]


def test_investment_activity_summary_consumes_exact_persisted_text(tmp_path) -> None:
    """The KPI aggregate keeps high-magnitude and 12-place source digits."""
    db_path = tmp_path / "ledger.db"
    repo = InvestmentRepository(db_path)
    statement = synthetic_statement(
        alias="TFSA",
        period_end=date(2026, 7, 31),
        nav="1",
        symbol="AAA",
        activity_type=InvestmentActivityType.DEPOSIT,
    ).model_copy(
        update={
            "activities": (
                InvestmentActivity(
                    activity_id="high-deposit",
                    activity_type=InvestmentActivityType.DEPOSIT,
                    activity_date=date(2026, 7, 1),
                    currency="USD",
                    amount=Decimal("9999999999999999.123456789"),
                    source_row=1,
                ),
                InvestmentActivity(
                    activity_id="tiny-deposit",
                    activity_type=InvestmentActivityType.DEPOSIT,
                    activity_date=date(2026, 7, 2),
                    currency="USD",
                    amount=Decimal("0.000000000001"),
                    source_row=2,
                ),
            ),
            "parsed_fingerprint": "e" * 64,
        }
    )
    repo.store_statement(
        statement,
        source_csv_path=Path("/synthetic/exact-summary.csv"),
        source_csv_sha256="e" * 64,
        source_pdf_path=Path("/synthetic/exact-summary.pdf"),
        source_pdf_sha256="f" * 64,
    )

    assert LedgerQueryService(db_path).get_investment_activity_summary() == {
        "net_deposits_withdrawals": Decimal("9999999999999999.123456789001"),
        "dividends_interest": Decimal("0.000000000000"),
    }
