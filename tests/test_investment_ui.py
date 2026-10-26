"""Pure presentation helpers for the historical investments dashboard."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pandas as pd
import pytest

from ledger.ui.pages import investments as investments_page
from ledger.ui.pages.investments import (
    _account_nav_frame,
    _activity_frame,
    _combined_nav_frame,
    _historical_kpis,
    _holding_frame,
    _lifecycle_caption,
    _nav_figure,
    _performance_frame,
    _provenance_caption,
    _reported_allocation,
    _reported_twr,
    _status_summary,
)


def test_status_summary_labels_closed_history_without_current_value() -> None:
    accounts = [
        {
            "account_alias": "RRSP",
            "status": "closed",
            "transfer_destination": "Lumen",
            "status_confirmed_on": date(2026, 8, 1),
            "closed_on": None,
            "last_statement_date": date(2026, 7, 31),
        },
        {
            "account_alias": "TFSA",
            "status": "closed",
            "transfer_destination": "Lumen",
            "status_confirmed_on": date(2026, 8, 1),
            "closed_on": None,
            "last_statement_date": date(2026, 7, 31),
        },
    ]
    summary = _status_summary(accounts)

    assert summary["current_value"] is None
    assert summary["current_label"] == "Live investment value unavailable"
    assert summary["historical_accounts"] == 2
    assert summary["closed_accounts"] == 2
    assert summary["transferred_accounts"] == 2
    assert summary["lifecycle_confirmed_on"] == date(2026, 8, 1)
    assert summary["latest_statement_date"] == date(2026, 7, 31)
    assert _lifecycle_caption(accounts) == (
        "RRSP and TFSA are closed and transferred to Lumen; status confirmed "
        "Aug 1, 2026; exact closure dates are unavailable."
    )


def test_collective_lifecycle_date_requires_confirmation_from_every_closed_account() -> None:
    """One confirmed account cannot lend its confirmation date to an unconfirmed peer."""
    accounts = [
        {
            "account_alias": "RRSP",
            "status": "closed",
            "transfer_destination": "Lumen",
            "status_confirmed_on": date(2026, 8, 1),
            "closed_on": None,
            "last_statement_date": date(2026, 7, 31),
        },
        {
            "account_alias": "TFSA",
            "status": "closed",
            "transfer_destination": "Lumen",
            "status_confirmed_on": None,
            "closed_on": None,
            "last_statement_date": date(2026, 7, 31),
        },
    ]

    assert _status_summary(accounts)["lifecycle_confirmed_on"] is None
    caption = _lifecycle_caption(accounts)
    assert caption is not None
    assert "status confirmed" not in caption


def test_cad_presentation_excludes_usd_totals_and_keeps_unknown_values_absent() -> None:
    """CAD snapshots cannot borrow USD holdings, NAV, performance, or confirmation."""
    rows = [
        {
            "period_end": date(2026, 7, 31),
            "account_alias": currency,
            "base_currency": currency,
            "total_nav": Decimal(value),
            "contributing_accounts": 1,
            "realized_pl": None if currency == "CAD" else Decimal("45"),
            "unrealized_pl": None,
            "time_weighted_return": None,
        }
        for currency, value in (("CAD", "123"), ("USD", "456"))
    ]
    combined = _combined_nav_frame(rows, "CAD")
    series = _account_nav_frame(rows, "CAD")
    assert combined["total_nav"].tolist() == [123.0]
    assert series["account_alias"].tolist() == ["CAD"]
    assert _nav_figure(combined, series, "CAD").layout.yaxis.title.text == "CAD"
    metrics = _historical_kpis(
        rows, {"net_deposits_withdrawals": Decimal("0"), "dividends_interest": Decimal("0")}, "CAD"
    )
    assert metrics["latest_nav"] == Decimal("123")
    assert metrics["realized_pl"] is None
    assert metrics["unrealized_pl"] is None
    assert metrics["reported_return"] is None
    assert _performance_frame(rows, "CAD")["Account"].tolist() == ["CAD"]

    positions = [
        {"currency": "CAD", "market_value": Decimal("123"), "unrealized_pl": None},
        {"currency": "USD", "market_value": Decimal("456"), "unrealized_pl": Decimal("45")},
    ]
    coverage = investments_page._holding_coverage(positions, "CAD")
    assert coverage["market_value"].total == Decimal("123")
    assert coverage["unrealized_pl"].total is None
    assert _holding_frame(positions, "USD")["Market value (USD)"].tolist() == [456.0]
    allocation = _reported_allocation([], positions, "CAD")
    assert allocation.frame["Market value (CAD)"].tolist() == [123.0]
    summary = _status_summary([{"status": "unknown", "status_confirmed_on": None}])
    assert summary["unknown_accounts"] == 1
    assert summary["closed_accounts"] == 0
    assert summary["current_value"] is None
    assert summary["lifecycle_confirmed_on"] is None


def test_combined_nav_frame_preserves_source_coverage_without_forward_fill() -> None:
    frame = _combined_nav_frame(
        [
            {
                "period_end": date(2026, 6, 30),
                "total_nav": Decimal("100.12"),
                "contributing_accounts": 1,
            },
            {
                "period_end": date(2026, 7, 31),
                "total_nav": Decimal("310.25"),
                "contributing_accounts": 2,
            },
        ]
    )

    assert frame.loc[0, "contributing_accounts"] == 1
    assert frame.loc[1, "contributing_accounts"] == 2
    assert frame["total_nav"].tolist() == [100.12, 310.25]
    assert "return" not in frame.columns

    account_frame = _account_nav_frame(
        [
            {
                "period_end": date(2026, 6, 30),
                "account_alias": "TFSA",
                "total_nav": Decimal("100.12"),
            },
            {
                "period_end": date(2026, 7, 31),
                "account_alias": "TFSA",
                "total_nav": Decimal("110.13"),
            },
            {
                "period_end": date(2026, 7, 31),
                "account_alias": "RRSP",
                "total_nav": Decimal("200.12"),
            },
        ]
    )
    figure = _nav_figure(frame, account_frame)
    assert [trace.name for trace in figure.data] == ["RRSP", "TFSA", "Combined reported NAV"]
    assert list(figure.data[0].y) == [200.12]
    assert list(figure.data[1].y) == [100.12, 110.13]
    assert list(figure.data[2].y) == [100.12, 310.25]
    assert list(figure.data[2].customdata[:, 0]) == [1, 2]


def test_holding_and_activity_frames_are_bounded_render_safe_views() -> None:
    holdings = _holding_frame(
        [
            {
                "account_alias": "TFSA",
                "symbol": "AAA",
                "asset_category": "Stocks",
                "quantity": Decimal("2"),
                "close_price": Decimal("100"),
                "cost_basis": Decimal("180"),
                "market_value": Decimal("200"),
                "unrealized_pl": Decimal("20"),
                "currency": "USD",
            }
        ]
    )
    activities = _activity_frame(
        [
            {
                "activity_date": date(2026, 7, 31),
                "account_alias": "TFSA",
                "activity_type": "dividend",
                "symbol": "AAA",
                "amount": Decimal("1.25"),
                "currency": "CAD",
                "description": "Dividend",
            }
        ]
    )

    assert holdings.columns.tolist() == [
        "Account",
        "Symbol",
        "Asset class",
        "Quantity",
        "Close price (USD)",
        "Cost basis (USD)",
        "Market value (USD)",
        "Unrealized P&L (USD)",
        "Return (%)",
    ]
    assert holdings.loc[0, "Close price (USD)"] == 100.0
    assert holdings.loc[0, "Account"] == "TFSA"
    assert holdings.loc[0, "Return (%)"] == pytest.approx(11.11111111111111)
    assert activities.columns.tolist() == [
        "Date",
        "Account",
        "Type",
        "Symbol",
        "Amount",
        "Currency",
        "Description",
    ]
    assert activities.loc[0, "Amount"] == 1.25
    assert activities.loc[0, "Currency"] == "CAD"


def test_reported_allocation_prefers_complete_asset_balances_including_cash() -> None:
    """A complete statement allocation cannot silently collapse NAV to open positions."""
    view = _reported_allocation(
        [
            {
                "account_alias": "TFSA",
                "total_nav": Decimal("150"),
                "asset_balances": [
                    {"asset_class": "Stocks", "current_total": Decimal("125")},
                    {"asset_class": "Cash", "current_total": Decimal("20")},
                    {"asset_class": "Dividend Accruals", "current_total": Decimal("5")},
                ],
            }
        ],
        [
            {
                "account_alias": "TFSA",
                "asset_category": "Stocks",
                "market_value": Decimal("125"),
            }
        ],
    )

    assert view.title == "Statement-reported allocation"
    assert view.complete is True
    assert dict(zip(view.frame["Asset class"], view.frame["Market value (USD)"], strict=True)) == {
        "Cash": 20.0,
        "Dividend Accruals": 5.0,
        "Stocks": 125.0,
    }
    assert "cash and other reported NAV classes" in view.detail


def test_reported_allocation_labels_position_only_fallback_as_partial() -> None:
    """Missing asset balances must make cash exclusion explicit, never look complete."""
    view = _reported_allocation(
        [{"account_alias": "TFSA", "total_nav": Decimal("150"), "asset_balances": []}],
        [
            {
                "account_alias": "TFSA",
                "asset_category": "Stocks",
                "market_value": Decimal("125"),
            }
        ],
    )

    assert view.title == "Open-position-only mix (partial)"
    assert view.complete is False
    assert "cash and other nav classes excluded" in view.detail.casefold()


def test_reported_twr_formats_fractional_returns_without_fabricating_missing_value() -> None:
    performance = _performance_frame(
        [
            {
                "account_alias": "TFSA",
                "period_end": date(2026, 7, 31),
                "total_nav": Decimal("150"),
                "time_weighted_return": Decimal("0.015"),
            },
            {
                "account_alias": "RRSP",
                "period_end": date(2026, 7, 31),
                "total_nav": Decimal("250"),
                "time_weighted_return": None,
            },
        ]
    )

    assert _reported_twr(Decimal("0.015")) == "1.50%"
    assert _reported_twr(None) == "—"
    assert performance.loc[performance["Account"] == "TFSA", "Reported TWR"].item() == "1.50%"
    assert performance.loc[performance["Account"] == "RRSP", "Reported TWR"].item() == "—"


def test_holding_return_is_unavailable_for_zero_or_missing_cost() -> None:
    frame = _holding_frame(
        [
            {
                "symbol": "ZERO",
                "asset_category": "Stocks",
                "quantity": Decimal("1"),
                "close_price": Decimal("10"),
                "cost_basis": Decimal("0"),
                "market_value": Decimal("10"),
                "unrealized_pl": Decimal("10"),
            },
            {
                "symbol": "MISSING",
                "asset_category": "Stocks",
                "quantity": Decimal("1"),
                "close_price": None,
                "cost_basis": None,
                "market_value": None,
                "unrealized_pl": None,
            },
        ]
    )

    assert frame["Return (%)"].isna().all()
    assert pd.isna(frame.loc[1, "Close price (USD)"])


def test_holding_kpis_sum_exact_decimals_when_coverage_is_complete() -> None:
    rows = [
        {
            "cost_basis": Decimal("9999999999999999.123456789012"),
            "market_value": Decimal("9999999999999999.223456789012"),
            "unrealized_pl": Decimal("0.100000000000"),
        },
        {
            "cost_basis": Decimal("0.000000000001"),
            "market_value": Decimal("0.000000000002"),
            "unrealized_pl": Decimal("-0.000000000001"),
        },
    ]

    coverage = investments_page._holding_coverage(rows)
    kpis = investments_page._holding_kpi_rows(coverage)

    assert coverage["cost_basis"].total == Decimal("9999999999999999.123456789013")
    assert coverage["market_value"].total == Decimal("9999999999999999.223456789014")
    assert coverage["unrealized_pl"].total == Decimal("0.099999999999")
    assert all(metric.reported_count == 2 for metric in coverage.values())
    assert all(metric.position_count == 2 for metric in coverage.values())
    assert [row[0] for row in kpis] == [
        "Cost basis (USD)",
        "Market value (USD)",
        "Unrealized P&L (USD)",
    ]
    assert len(kpis[-1]) == 4
    assert kpis[-1][-1] == "pos"


def test_holding_kpis_label_partial_coverage_independently() -> None:
    coverage = investments_page._holding_coverage(
        [
            {
                "cost_basis": Decimal("10.00"),
                "market_value": None,
                "unrealized_pl": Decimal("-1.00"),
            },
            {
                "cost_basis": None,
                "market_value": Decimal("12.00"),
                "unrealized_pl": None,
            },
        ]
    )

    kpis = investments_page._holding_kpi_rows(coverage)

    assert [metric.reported_count for metric in coverage.values()] == [1, 1, 1]
    assert all(metric.position_count == 2 for metric in coverage.values())
    assert [row[0] for row in kpis] == [
        "Cost basis (USD) — partial (1/2)",
        "Market value (USD) — partial (1/2)",
        "Unrealized P&L (USD) — partial (1/2)",
    ]
    assert all(len(row) == 3 for row in kpis)


def test_holding_kpis_render_all_missing_as_unavailable_without_tone_or_nan() -> None:
    coverage = investments_page._holding_coverage(
        [
            {"cost_basis": None, "market_value": None, "unrealized_pl": None},
            {"cost_basis": None, "market_value": None, "unrealized_pl": None},
        ]
    )

    kpis = investments_page._holding_kpi_rows(coverage)

    assert all(metric.total is None for metric in coverage.values())
    assert all(metric.reported_count == 0 for metric in coverage.values())
    assert all(metric.position_count == 2 for metric in coverage.values())
    assert all(row[1] == "Unavailable" for row in kpis)
    assert all(row[2] == "0/2 positions reported" for row in kpis)
    assert all(len(row) == 3 for row in kpis)
    assert "$nan" not in repr(kpis).casefold()


def test_selected_account_kpis_and_provenance_are_source_backed_and_masked() -> None:
    performance = {
        "account_alias": "TFSA",
        "period_start": date(2026, 7, 1),
        "period_end": date(2026, 7, 31),
        "total_nav": Decimal("250.123456789"),
        "time_weighted_return": Decimal("0.015"),
        "realized_pl": Decimal("2.25"),
        "unrealized_pl": Decimal("3.75"),
    }
    activity_summary = {
        "net_deposits_withdrawals": Decimal("75.000000000000"),
        "dividends_interest": Decimal("5.500000000000"),
    }

    assert _historical_kpis([performance], activity_summary) == {
        "latest_nav": Decimal("250.123456789"),
        "latest_period_end": date(2026, 7, 31),
        "net_deposits_withdrawals": Decimal("75.000000000000"),
        "dividends_interest": Decimal("5.500000000000"),
        "realized_pl": Decimal("2.25"),
        "unrealized_pl": Decimal("3.75"),
        "reported_return": Decimal("0.015"),
    }
    caption = _provenance_caption(
        {
            "account_alias": "TFSA",
            "period_start": date(2026, 7, 1),
            "period_end": date(2026, 7, 31),
            "import_state": "finalized",
            "archive_verified": True,
            "source_account_id": "PRIVATE-ID",
            "source_path": "/private/sentinel.csv",
        }
    )
    assert caption == (
        "TFSA statement Jul 1, 2026 to Jul 31, 2026 · import finalized · archive verified"
    )
    assert "PRIVATE-ID" not in caption
    assert "/private/" not in caption


def test_missing_account_kpis_display_unavailable_instead_of_zero() -> None:
    assert _historical_kpis(
        [],
        {
            "net_deposits_withdrawals": Decimal("0.000000000000"),
            "dividends_interest": Decimal("0.000000000000"),
        },
    ) == {
        "latest_nav": None,
        "latest_period_end": None,
        "net_deposits_withdrawals": None,
        "dividends_interest": None,
        "realized_pl": None,
        "unrealized_pl": None,
        "reported_return": None,
    }
