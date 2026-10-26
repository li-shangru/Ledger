"""Render-invocation tests for every Streamlit page via AppTest.

Guards the A1 crash class: render-time exceptions in any page (or non-default
sub-view) now surface as test failures instead of going unnoticed until a
browser smoke run.

Strategy
--------
* AppTest.from_file() runs a small per-page driver script that builds a minimal
  AppContext against a tmp-path-seeded SQLite DB and calls the page's render().
* The Categories page also drives the Merchant segment (the A1 crash site) by
  switching the segmented_control value and re-running.
* The rules_settings page drives all four segments (Merchant Profiles, Category
  Taxonomy, Apply History, Import / Export).
* Import Center is excluded from AppTest (it reads data/import-runs at module
  level and calls st.stop() when no run is active -- the headless runner cannot
  satisfy that path without a full import run fixture; we document why below).

All tests share a module-level seeded DB via a session-scoped fixture so the
heavy init work (init_database, run_migrations, seed_config_database, write 30
transactions) runs once per pytest session.
"""

from __future__ import annotations

import datetime as dt
import os
import textwrap
from datetime import UTC, date, timedelta
from decimal import Decimal
from html import unescape
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Shared seed fixture
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
REF_DATE = date(2026, 1, 15)
PERIOD_START = date(2025, 12, 1)
PERIOD_END = REF_DATE


def _seed_db(db_path: Path) -> None:
    """Create a minimal DB with 30 expense transactions and seeded config."""
    from ledger.config.repository import seed_config_database
    from ledger.core import Transaction
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
    from ledger.processing import ProcessingResult
    from ledger.storage.database import (
        InvestmentSourceAssociationDB,
        InvestmentSourceFileDB,
        init_database,
        session_scope,
    )
    from ledger.storage.migrations import run_migrations
    from ledger.storage.writer import DatabaseWriter

    init_database(db_path)
    run_migrations(db_path)

    txns: list[Transaction] = []
    for i in range(30):
        txn_date = REF_DATE - timedelta(days=i)
        dow = txn_date.weekday()
        woy = txn_date.isocalendar()[1]
        txns.append(
            Transaction(
                txn_id=f"render_test_{i}",
                posting_date=txn_date,
                transaction_date=txn_date,
                institution="TestBank",
                account="1234",
                account_type="credit_card",
                flow="expense",
                cashflow_type="expense",
                include_in_cashflow=True,
                currency="CAD",
                amount_raw=Decimal("50.00"),
                amount_signed=Decimal("-50.00"),
                description="Coffee Shop",
                merchant="TimHortons",
                category="Dining",
                merchant_confidence="high",
                category_confidence="high",
                exclude_from_spending=False,
                merchant_raw="TIM HORTONS",
                category_raw="Dining",
                source_file="test.csv",
                source_path="/test",
                source_period=txn_date.strftime("%Y-%m"),
                source_row=i,
                imported_at=dt.datetime.now(UTC).replace(tzinfo=None),
                parser_version="1.0.0",
                txn_fingerprint=f"fp_render_{i}",
                day_of_week=dow,
                day_of_month=txn_date.day,
                week_of_year=woy,
                is_weekend=dow >= 5,
                is_month_start=txn_date.day <= 3,
                is_month_end=txn_date.day >= 28,
            )
        )

    DatabaseWriter(db_path).write_result(
        ProcessingResult(
            transactions=txns,
            transfers=[],
            exceptions=[],
            duplicates_removed=0,
            total_scanned=len(txns),
        ),
        Path("/test"),
        "1.0.0",
    )

    investment_account = InvestmentAccount(
        account_id="sanitized-investment-account",
        institution="SUMMIT",
        source_account_id="masked-source-account",
        account_alias="TFSA",
        account_type="TFSA",
        base_currency="USD",
        status="closed",
        transfer_destination="Lumen",
        status_confirmed_on=date(2026, 8, 1),
        closed_on=None,
    )
    InvestmentRepository(db_path).store_statement(
        InvestmentStatement(
            statement_id="sanitized-investment-statement",
            account=investment_account,
            period_start=date(2025, 12, 1),
            period_end=date(2025, 12, 31),
            generated_at=dt.datetime(2025, 12, 31),
            base_currency="USD",
            total_nav=Decimal("150.00"),
            time_weighted_return=Decimal("0.015"),
            asset_balances=(
                InvestmentAssetBalance(
                    asset_class="Stocks",
                    prior_total=Decimal("125"),
                    current_long=Decimal("150"),
                    current_short=Decimal("0"),
                    current_total=Decimal("150"),
                    change=Decimal("25"),
                ),
            ),
            positions=(
                InvestmentPosition(
                    asset_category="Stocks",
                    currency="USD",
                    symbol="SAN",
                    discriminator="Summary",
                    quantity=Decimal("2"),
                    close_price=Decimal("75"),
                    cost_basis=Decimal("125"),
                    market_value=Decimal("150"),
                    unrealized_pl=Decimal("25"),
                    source_row=1,
                ),
            ),
            performance=(
                InvestmentPerformance(
                    asset_category="Total",
                    symbol="",
                    cost_adjustment=Decimal("0"),
                    realized_short_profit=Decimal("2.75"),
                    realized_short_loss=Decimal("-0.50"),
                    realized_long_profit=Decimal("0"),
                    realized_long_loss=Decimal("0"),
                    realized_total=Decimal("2.25"),
                    unrealized_short_profit=Decimal("4.00"),
                    unrealized_short_loss=Decimal("-0.25"),
                    unrealized_long_profit=Decimal("0"),
                    unrealized_long_loss=Decimal("0"),
                    unrealized_total=Decimal("3.75"),
                    total=Decimal("6.00"),
                    code="",
                    source_row=2,
                ),
            ),
            activities=(
                InvestmentActivity(
                    activity_id="sanitized-activity",
                    activity_type=InvestmentActivityType.DIVIDEND,
                    activity_date=date(2025, 12, 31),
                    symbol="SAN",
                    currency="USD",
                    amount=Decimal("1.50"),
                    source_row=3,
                ),
                InvestmentActivity(
                    activity_id="sanitized-deposit",
                    activity_type=InvestmentActivityType.DEPOSIT,
                    activity_date=date(2025, 12, 10),
                    currency="USD",
                    amount=Decimal("100"),
                    source_row=4,
                ),
                InvestmentActivity(
                    activity_id="sanitized-withdrawal",
                    activity_type=InvestmentActivityType.WITHDRAWAL,
                    activity_date=date(2025, 12, 11),
                    currency="USD",
                    amount=Decimal("-25"),
                    source_row=5,
                ),
                InvestmentActivity(
                    activity_id="sanitized-interest",
                    activity_type=InvestmentActivityType.INTEREST,
                    activity_date=date(2025, 12, 12),
                    currency="USD",
                    amount=Decimal("0.50"),
                    source_row=6,
                ),
            ),
            parsed_fingerprint="a" * 64,
        ),
        source_csv_path=Path("/sanitized/statement.csv"),
        source_csv_sha256="b" * 64,
        source_pdf_path=Path("/sanitized/statement.pdf"),
        source_pdf_sha256="c" * 64,
    )
    InvestmentRepository(db_path).store_statement(
        InvestmentStatement(
            statement_id="sanitized-missing-return-statement",
            account=InvestmentAccount(
                account_id="sanitized-missing-return-account",
                institution="SUMMIT",
                source_account_id="masked-missing-return-account",
                account_alias="RRSP",
                account_type="RRSP",
                base_currency="USD",
                status="closed",
                transfer_destination="Lumen",
                status_confirmed_on=date(2026, 8, 1),
                closed_on=None,
            ),
            period_start=date(2025, 12, 1),
            period_end=date(2025, 12, 31),
            generated_at=dt.datetime(2025, 12, 31),
            base_currency="USD",
            total_nav=Decimal("250.00"),
            time_weighted_return=None,
            asset_balances=(
                InvestmentAssetBalance(
                    asset_class="Stocks",
                    prior_total=Decimal("250"),
                    current_long=Decimal("250"),
                    current_short=Decimal("0"),
                    current_total=Decimal("250"),
                    change=Decimal("0"),
                ),
            ),
            positions=(),
            performance=(),
            activities=(),
            parsed_fingerprint="d" * 64,
        ),
        source_csv_path=Path("/sanitized/missing-return-statement.csv"),
        source_csv_sha256="e" * 64,
        source_pdf_path=Path("/sanitized/missing-return-statement.pdf"),
        source_pdf_sha256="f" * 64,
    )
    with session_scope(db_path) as session:
        for source in session.query(InvestmentSourceFileDB):
            source.status = "finalized"
            source.archive_status = "archived"
            source.archived_path = "/sanitized/archive"
            source.archived_at = dt.datetime(2026, 8, 1)
        for association in session.query(InvestmentSourceAssociationDB):
            association.status = "finalized"
            association.archive_status = "archived"
            association.archived_path = "/sanitized/archive"
            association.archived_at = dt.datetime(2026, 8, 1)

    seed_config_database(
        db_path,
        REPO_ROOT / "configs" / "merchants.toml",
        REPO_ROOT / "configs" / "categories.toml",
    )


@pytest.fixture(scope="session")
def render_test_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Session-scoped seeded DB shared across all render-invocation tests."""
    db_path = tmp_path_factory.mktemp("render_db") / "render_test.db"
    _seed_db(db_path)
    return db_path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DRIVER_TEMPLATE = textwrap.dedent("""\
    # Auto-generated AppTest driver — do not edit.
    from pathlib import Path
    from datetime import date
    import os

    from ledger.query import LedgerQueryService
    from ledger.config.repository import SQLiteConfigRepository
    from ledger.ui.context import AppContext

    _db = Path(os.environ["_LEDGER_APPTEST_DB"])
    _service = LedgerQueryService(_db)
    {setup_code}
    _repo = SQLiteConfigRepository(_db)
    _category_config = _repo.load_categories()
    _ctx = AppContext(
        db_path=_db,
        service=_service,
        category_config=_category_config,
        stats=_service.get_stats(),
        earliest=date(2025, 12, 1),
        latest=date(2026, 1, 15),
        start_date=date(2025, 12, 1),
        end_date=date(2026, 1, 15),
        period_label="Dec 2025 - Jan 2026",
        review_source={{
            "label": "render_test.db",
            "status": "live_db",
            "db_path": _db,
            "source_kind": "Current DB",
        }},
        review_db=_db,
        pending_reviews=0,
    )

    {page_import}
    {render_call}
""")


def _driver_script(tmp_path: Path, page_module: str, setup_code: str = "") -> Path:
    """Write a driver script for the given page module and return its path."""
    script = tmp_path / f"_driver_{page_module.split('.')[-1]}.py"
    import_stmt = f"from ledger.ui.pages import {page_module.split('.')[-1]}"
    mod_name = page_module.split(".")[-1]
    render_stmt = f"{mod_name}.render(_ctx)"
    script.write_text(
        _DRIVER_TEMPLATE.format(
            setup_code=setup_code,
            page_import=import_stmt,
            render_call=render_stmt,
        )
    )
    return script


def _run_page(render_test_db: Path, tmp_path: Path, page_module: str) -> None:
    """Run a page driver via AppTest and assert no uncaught exception."""
    from streamlit.testing.v1 import AppTest

    os.environ["_LEDGER_APPTEST_DB"] = str(render_test_db)
    script = _driver_script(tmp_path, page_module)
    at = AppTest.from_file(str(script), default_timeout=30)
    at.run()
    assert not at.exception, f"{page_module} render() raised: {at.exception}"


# ---------------------------------------------------------------------------
# Per-page tests
# ---------------------------------------------------------------------------


def test_render_overview(render_test_db: Path, tmp_path: Path) -> None:
    _run_page(render_test_db, tmp_path, "ledger.ui.pages.overview")


def test_render_transactions(render_test_db: Path, tmp_path: Path) -> None:
    _run_page(render_test_db, tmp_path, "ledger.ui.pages.transactions")


def test_render_accounts(render_test_db: Path, tmp_path: Path) -> None:
    _run_page(render_test_db, tmp_path, "ledger.ui.pages.accounts")


def test_render_insights(render_test_db: Path, tmp_path: Path) -> None:
    _run_page(render_test_db, tmp_path, "ledger.ui.pages.insights")


def test_render_investments_uses_uncapped_summary_and_bounded_detail_table(
    render_test_db: Path, tmp_path: Path
) -> None:
    """Selected KPIs use exact summary values while activity details remain bounded."""
    from streamlit.testing.v1 import AppTest

    os.environ["_LEDGER_APPTEST_DB"] = str(render_test_db)
    script = _driver_script(
        tmp_path,
        "ledger.ui.pages.investments",
        setup_code=textwrap.dedent("""\
            from decimal import Decimal

            class _InvestmentsDouble:
                def __init__(self, delegate):
                    self._delegate = delegate

                def __getattr__(self, name):
                    return getattr(self._delegate, name)

                def get_investment_activity_summary(self, **_filters):
                    return {
                        "net_deposits_withdrawals": Decimal("5075.000000005001"),
                        "dividends_interest": Decimal("27.000000000001"),
                    }

                def get_investment_activities(self, **filters):
                    if filters.get("limit", 1000) > 1000:
                        raise AssertionError("Investment detail queries must remain bounded")
                    return self._delegate.get_investment_activities(**filters)

            _service.investments = _InvestmentsDouble(_service.investments)
        """),
    )
    at = AppTest.from_file(str(script), default_timeout=30)
    at.run()

    assert not at.exception, f"investments render() raised: {at.exception}"
    charts = at.get("plotly_chart")
    assert charts, "Expected the historical NAV chart to render"
    assert at.dataframe, "Expected a source-backed holdings or activity detail table"
    default_text = unescape(
        "\n".join(
            str(element.value)
            for kind in ("caption", "markdown", "info")
            for element in at.get(kind)
        )
    )
    assert "Dec 2025 - Jan 2026" not in default_text
    assert "Contributing accounts: RRSP, TFSA; missing accounts: none." in default_text
    assert "cash and other reported NAV classes" in default_text
    assert (
        "RRSP and TFSA are closed and transferred to Lumen; status confirmed "
        "Aug 1, 2026; exact closure dates are unavailable."
    ) in default_text

    at.selectbox[0].select("TFSA").run()
    assert not at.exception, f"TFSA investments render() raised: {at.exception}"
    rendered_text = unescape(
        "\n".join(
            str(element.value)
            for kind in ("caption", "markdown", "info")
            for element in at.get(kind)
        )
    )
    for expected in (
        "Latest reported NAV (USD)",
        "$150.00",
        "Cost basis (USD)",
        "$125.00",
        "Market value (USD)",
        "Net deposits / withdrawals (USD)",
        "$5,075.00",
        "Dividends + interest (USD)",
        "$27.00",
        "Realized P&L (USD)",
        "$2.25",
        "Unrealized P&L (USD)",
        "$3.75",
        "Statement-reported return",
        "1.50%",
        "Activity amounts retain their source-reported currency",
    ):
        assert expected in rendered_text
    holdings = next(frame.value for frame in at.dataframe if "Return (%)" in frame.value.columns)
    assert holdings["Account"].tolist() == ["TFSA"]
    assert holdings.loc[0, "Close price (USD)"] == 75.0
    assert holdings.loc[0, "Return (%)"] == 20.0
    assert "TFSA statement Dec 1, 2025 to Dec 31, 2025" in rendered_text
    assert "import finalized" in rendered_text
    assert "archive verified" in rendered_text
    assert "masked-source-account" not in rendered_text
    assert "/sanitized/" not in rendered_text


@pytest.mark.parametrize(
    ("mode", "expected_fragments"),
    [
        (
            "mixed",
            (
                "Cost basis (USD) — partial (1/2)",
                "Market value (USD) — partial (1/2)",
                "Unrealized P&amp;L (USD) — partial (1/2)",
                "Reported positions only",
            ),
        ),
        (
            "missing",
            (
                "Cost basis (USD)",
                "Market value (USD)",
                "Unrealized P&amp;L (USD)",
                "Unavailable",
                "0/2 positions reported",
            ),
        ),
    ],
)
def test_render_investment_holdings_reports_metric_coverage_without_nan_or_false_tone(
    render_test_db: Path,
    tmp_path: Path,
    mode: str,
    expected_fragments: tuple[str, ...],
) -> None:
    """Mixed and absent position fields remain visibly partial or unavailable."""
    from streamlit.testing.v1 import AppTest

    values = (
        "Decimal('10.00'), Decimal('12.00'), Decimal('-1.00')"
        if mode == "mixed"
        else "None, None, None"
    )
    script = _driver_script(
        tmp_path,
        "ledger.ui.pages.investments",
        setup_code=textwrap.dedent(f"""\
            from decimal import Decimal

            class _HoldingsCoverageDouble:
                def __init__(self, delegate):
                    self._delegate = delegate

                def __getattr__(self, name):
                    return getattr(self._delegate, name)

                def get_investment_positions(self, statement_id):
                    if statement_id != "sanitized-investment-statement":
                        return []
                    first_cost, first_value, first_pl = {values}
                    return [
                        {{
                            "symbol": "ONE",
                            "asset_category": "Stocks",
                            "quantity": Decimal("1"),
                            "close_price": Decimal("12"),
                            "cost_basis": first_cost,
                            "market_value": first_value,
                            "unrealized_pl": first_pl,
                        }},
                        {{
                            "symbol": "TWO",
                            "asset_category": "Stocks",
                            "quantity": Decimal("1"),
                            "close_price": None,
                            "cost_basis": None,
                            "market_value": None,
                            "unrealized_pl": None,
                        }},
                    ]

            _service.investments = _HoldingsCoverageDouble(_service.investments)
        """),
    )
    at = AppTest.from_file(str(script), default_timeout=30)
    at.run()

    assert not at.exception, f"investments render() raised: {at.exception}"
    holdings_markup = next(
        str(element.value)
        for element in at.get("markdown")
        if "Cost basis (USD)" in str(element.value)
    )
    assert all(fragment in holdings_markup for fragment in expected_fragments)
    assert "$nan" not in holdings_markup.casefold()
    assert "ledger-status-value pos" not in holdings_markup
    assert "ledger-status-value neg" not in holdings_markup


def test_render_investments_performance_displays_fractional_and_missing_twr(
    render_test_db: Path, tmp_path: Path
) -> None:
    """The rendered performance table presents fractional and missing TWR values honestly."""
    from streamlit.testing.v1 import AppTest

    os.environ["_LEDGER_APPTEST_DB"] = str(render_test_db)
    script = _driver_script(tmp_path, "ledger.ui.pages.investments")
    at = AppTest.from_file(str(script), default_timeout=30)
    at.run()

    performance_tables = [
        dataframe.value for dataframe in at.dataframe if "Reported TWR" in dataframe.value.columns
    ]
    assert len(performance_tables) == 1
    assert sorted(performance_tables[0]["Reported TWR"].tolist()) == ["1.50%", "—"]
    assert sorted(performance_tables[0]["Account"].tolist()) == ["RRSP", "TFSA"]


def test_render_investment_currency_selection_keeps_native_holdings_and_missing_pl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Switching reporting currency separates NAV while foreign holdings remain visible."""
    import pandas as pd
    from streamlit.testing.v1 import AppTest

    from ledger.storage.database import (
        InvestmentAccountDB,
        InvestmentPositionDB,
        InvestmentStatementDB,
        session_scope,
    )

    db_path = tmp_path / "mixed-investments.db"
    _seed_db(db_path)
    with session_scope(db_path) as session:
        session.add(
            InvestmentAccountDB(
                account_id="synthetic-cad-account",
                institution="Lumen",
                source_account_id="synthetic-cad-source",
                account_alias="Lumen TFSA synthetic",
                account_type="TFSA",
                base_currency="CAD",
                status="unknown",
                status_confirmed_on=None,
                first_statement_date=PERIOD_START,
                last_statement_date=PERIOD_END,
                created_at=dt.datetime(2026, 1, 15),
                updated_at=dt.datetime(2026, 1, 15),
            )
        )
        session.flush()
        session.add(
            InvestmentStatementDB(
                statement_id="synthetic-cad-statement",
                account_id="synthetic-cad-account",
                period_start=PERIOD_START,
                period_end=PERIOD_END,
                generated_at=None,
                base_currency="CAD",
                total_nav=Decimal("1234.56"),
                time_weighted_return=None,
                parsed_fingerprint="c" * 64,
                imported_at=dt.datetime(2026, 1, 15),
            )
        )
        session.flush()
        for row, currency in enumerate(("CAD", "USD")):
            session.add(
                InvestmentPositionDB(
                    statement_id="synthetic-cad-statement",
                    source_row=row,
                    asset_category="Stocks",
                    currency=currency,
                    symbol=f"NATIVE{currency}",
                    discriminator="",
                    quantity=Decimal("1"),
                    market_value=Decimal("123") if currency == "CAD" else Decimal("456"),
                )
            )
    monkeypatch.setenv("_LEDGER_APPTEST_DB", str(db_path))
    at = AppTest.from_file(
        str(_driver_script(tmp_path, "ledger.ui.pages.investments")), default_timeout=30
    )
    at.run()
    assert not at.exception
    assert at.selectbox[0].label == "Reporting currency"
    assert at.selectbox[0].value == "CAD"
    assert "1 status unconfirmed" in "\n".join(str(element.value) for element in at.markdown)
    tables = [frame.value for frame in at.dataframe]
    performance = next(frame for frame in tables if "Reported TWR" in frame.columns)
    assert performance["Reported NAV (CAD)"].tolist() == [1234.56]
    assert performance["Reported TWR"].tolist() == ["—"]
    assert pd.isna(performance["Realized P&L (CAD)"].iloc[0])
    assert pd.isna(performance["Unrealized P&L (CAD)"].iloc[0])
    holdings = [frame for frame in tables if "Return (%)" in frame.columns]
    assert len(holdings) == 2
    assert holdings[0]["Market value (CAD)"].tolist() == [123.0]
    assert holdings[1]["Market value (USD)"].tolist() == [456.0]
    assert all(frame["Return (%)"].isna().all() for frame in holdings)
    at.selectbox[0].select("USD").run()
    assert not at.exception
    performance = next(
        frame.value for frame in at.dataframe if "Reported TWR" in frame.value.columns
    )
    assert "Reported NAV (USD)" in performance.columns
    assert sorted(performance["Account"].tolist()) == ["RRSP", "TFSA"]


def test_render_budgets_goals(render_test_db: Path, tmp_path: Path) -> None:
    _run_page(render_test_db, tmp_path, "ledger.ui.pages.budgets_goals")


def test_cash_schedule_confirmation_displays_pattern_conflict(tmp_path: Path) -> None:
    from streamlit.testing.v1 import AppTest

    from ledger.planning import BudgetService
    from ledger.planning.schedules import ScheduleDraft, ScheduleService
    from ledger.storage.database import AccountDB, session_scope

    db_path = tmp_path / "ledger.db"
    _seed_db(db_path)
    now = dt.datetime.now(UTC).replace(tzinfo=None)
    with session_scope(db_path) as session:
        session.add(
            AccountDB(
                account_id="synthetic-cash-account",
                institution="TestBank",
                account_number="cash-account",
                account_type="chequing",
                account_subtype="chequing",
                first_transaction_date=date(2025, 12, 1),
                last_transaction_date=date(2026, 1, 15),
                is_active=True,
                transaction_count=30,
                avg_monthly_transactions=Decimal("30.00"),
                is_credit_card=False,
                detected_credit_limit=None,
                created_at=now,
                updated_at=now,
            )
        )
    account_id = str(BudgetService(db_path).list_goal_accounts()[0]["account_id"])
    schedule_service = ScheduleService(db_path, prepare_schema=False)
    common = {
        "name": "Utility",
        "event_type": "bill",
        "amount": Decimal("100.00"),
        "currency": "CAD",
        "account_id": account_id,
        "counterparty_account_id": None,
        "next_date": date(2026, 10, 1),
        "frequency": "monthly",
        "pattern_id": "detected-utility",
        "is_active": True,
        "note": "",
    }
    schedule_service.create(
        ScheduleDraft(**common, provenance="hypothetical"), schedule_id="original"
    )
    schedule_service.create(
        ScheduleDraft(**common, provenance="confirmed"), schedule_id="replacement"
    )

    os.environ["_LEDGER_APPTEST_DB"] = str(db_path)
    at = AppTest.from_file(
        str(_driver_script(tmp_path, "ledger.ui.pages.budgets_goals")), default_timeout=30
    ).run()
    next(item for item in at.button if item.label == "Confirm Selected Schedule").click().run()

    assert not at.exception
    assert [str(item.value) for item in at.error] == ["Detected pattern is already confirmed."]


def test_render_admin_data(render_test_db: Path, tmp_path: Path) -> None:
    _run_page(render_test_db, tmp_path, "ledger.ui.pages.admin_data")


def test_render_review_queue(render_test_db: Path, tmp_path: Path) -> None:
    _run_page(render_test_db, tmp_path, "ledger.ui.pages.review_queue")


def test_render_rules_settings(render_test_db: Path, tmp_path: Path) -> None:
    _run_page(render_test_db, tmp_path, "ledger.ui.pages.rules_settings")


# ---------------------------------------------------------------------------
# Categories page — Category segment (default) + Merchant segment (A1 site)
# ---------------------------------------------------------------------------


def test_render_categories_default_segment(render_test_db: Path, tmp_path: Path) -> None:
    """Categories page renders without error in the default Category segment."""
    _run_page(render_test_db, tmp_path, "ledger.ui.pages.categories")


def test_render_categories_merchant_segment(render_test_db: Path, tmp_path: Path) -> None:
    """Categories page renders without error after switching to the Merchant segment.

    This is the exact sub-view that triggered the A1 AttributeError. We
    explicitly switch the segmented_control to 'Merchant' and re-run so that
    _render_merchant() is exercised.
    """
    from streamlit.testing.v1 import AppTest

    os.environ["_LEDGER_APPTEST_DB"] = str(render_test_db)
    script = _driver_script(tmp_path, "ledger.ui.pages.categories")
    at = AppTest.from_file(str(script), default_timeout=30)
    at.run()
    assert not at.exception, f"Categories Category segment raised: {at.exception}"

    # Switch to Merchant segment and re-run — this exercises _render_merchant()
    assert at.segmented_control, "Expected a segmented_control widget on Categories page"
    at.segmented_control[0].set_value("Merchant")
    at.run()
    assert not at.exception, f"Categories Merchant segment raised: {at.exception}"


# ---------------------------------------------------------------------------
# Rules/Settings page — drive all four sub-views
# ---------------------------------------------------------------------------


def test_render_rules_settings_all_subviews(render_test_db: Path, tmp_path: Path) -> None:
    """Rules/Settings page renders all four segments without error."""
    from streamlit.testing.v1 import AppTest

    os.environ["_LEDGER_APPTEST_DB"] = str(render_test_db)
    script = _driver_script(tmp_path, "ledger.ui.pages.rules_settings")

    for segment in ["Merchant Profiles", "Category Taxonomy", "Apply History", "Import / Export"]:
        at = AppTest.from_file(str(script), default_timeout=30)
        at.run()
        assert not at.exception, f"rules_settings default render raised: {at.exception}"
        if at.segmented_control:
            at.segmented_control[0].set_value(segment)
            at.run()
        assert not at.exception, f"rules_settings segment '{segment}' raised: {at.exception}"


# ---------------------------------------------------------------------------
# Import Center — excluded from AppTest (documented)
# ---------------------------------------------------------------------------
# import_center.render() calls st.stop() immediately when no active import run
# is present (data/import-runs/latest-run.txt does not exist in tmp_path),
# which exits via Streamlit's StopException before any user-visible content
# renders. This is correct production behaviour (empty-state guard) and cannot
# be driven headlessly without constructing a full import-run fixture. The
# existing smoke tests cover it via a live browser.
