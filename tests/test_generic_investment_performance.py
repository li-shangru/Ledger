"""Canonical account performance from independently authored fictional reports."""

from datetime import date
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import pytest

from ledger.investments.models import (
    InvestmentAccount,
    InvestmentAssetBalance,
    InvestmentPerformance,
    InvestmentStatement,
)
from ledger.investments.repository import InvestmentRepository
from ledger.query import LedgerQueryService
from ledger.storage.database import InvestmentStatementDB, session_scope


def performance(
    category: str,
    source_row: int,
    realized: str | None = "1234567890123456.123456789012",
    unrealized: str | None = "0.000000000001",
    total: str | None = "1234567890123456.123456789013",
) -> InvestmentPerformance:
    return InvestmentPerformance(
        asset_category=category,
        symbol="",
        source_row=source_row,
        realized_total=Decimal(realized) if realized is not None else None,
        unrealized_total=Decimal(unrealized) if unrealized is not None else None,
        total=Decimal(total) if total is not None else None,
    )


def statement(
    rows: tuple[InvestmentPerformance, ...],
    *,
    alias: str = "EXAMPLE-FUND",
    period_start: date = date(2031, 4, 1),
    period_end: date = date(2031, 4, 30),
    currency: str = "CAD",
) -> InvestmentStatement:
    statement_id = f"fictional-{alias}-{period_start}-{period_end}"
    return InvestmentStatement(
        statement_id=statement_id,
        account=InvestmentAccount(
            account_id=f"fictional-{alias}",
            institution="fictional-custodian",
            source_account_id=f"fictional-source-{alias}",
            account_alias=alias,
            account_type="investment",
            base_currency=currency,
            status="unknown",
        ),
        period_start=period_start,
        period_end=period_end,
        base_currency=currency,
        total_nav=Decimal("4321.123456789012"),
        time_weighted_return=Decimal("0.012345678901"),
        asset_balances=(
            InvestmentAssetBalance(asset_class="Cash", current_total=Decimal("4321.123456789012")),
        ),
        positions=(),
        performance=rows,
        activities=(),
        parsed_fingerprint=sha256(statement_id.encode()).hexdigest(),
    )


def store(repository: InvestmentRepository, value: InvestmentStatement, root: Path) -> None:
    repository.store_statement(
        value,
        source_pdf_path=root / f"{value.statement_id}.pdf",
        source_pdf_sha256=sha256(f"fictional-pdf-{value.statement_id}".encode()).hexdigest(),
        source_pdf_role="statement_pdf",
    )


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        pytest.param(
            (performance("Total (All Assets)", 0),),
            ("1234567890123456.123456789012", "0.000000000001", "1234567890123456.123456789013"),
            id="account-total-only",
        ),
        pytest.param(
            (performance("Total", 4),),
            ("1234567890123456.123456789012", "0.000000000001", "1234567890123456.123456789013"),
            id="unique-legacy-total",
        ),
        pytest.param(
            (
                performance("Total (All Assets)", 2),
                performance("Total", 9, "2", "3", "5"),
                performance("Total", 15, "7", "11", "18"),
            ),
            ("1234567890123456.123456789012", "0.000000000001", "1234567890123456.123456789013"),
            id="account-total-precedes-subtotals",
        ),
        pytest.param(
            (performance("Total", 4), performance("Total", 8, "0", "0", "0")),
            (None, None, None),
            id="multiple-subtotals-are-unavailable",
        ),
        pytest.param(
            (
                performance("Total (All Assets)", 2),
                performance("Total (All Assets)", 3),
                performance("Total", 4, "0", "0", "0"),
            ),
            (None, None, None),
            id="duplicate-account-totals-are-unavailable",
        ),
        pytest.param((), (None, None, None), id="missing-performance"),
        pytest.param((performance("Shares", 2),), (None, None, None), id="detail-is-not-total"),
        pytest.param(
            (performance("Total (All Assets)", 2, "0", "0", "0"),),
            ("0", "0", "0"),
            id="reported-zero-is-zero",
        ),
        pytest.param(
            (
                performance("Total (All Assets)", 2, None, "0", None),
                performance("Total", 4),
            ),
            (None, "0", None),
            id="missing-fields-never-fall-back-to-subtotal",
        ),
    ],
)
def test_canonical_performance_total_selection(tmp_path, rows, expected) -> None:
    db_path = tmp_path / "synthetic.db"
    repository = InvestmentRepository(db_path)
    value = statement(rows)
    original = value.model_dump(mode="json")
    store(repository, value, tmp_path)

    actual = LedgerQueryService(db_path).get_investment_performance()

    assert actual == [
        {
            "statement_id": value.statement_id,
            "account_alias": value.account.account_alias,
            "base_currency": value.base_currency,
            "period_start": value.period_start,
            "period_end": value.period_end,
            "total_nav": value.total_nav,
            "time_weighted_return": value.time_weighted_return,
            **{
                key: Decimal(amount) if amount is not None else None
                for key, amount in zip(
                    ("realized_pl", "unrealized_pl", "total_pl"), expected, strict=True
                )
            },
        }
    ]
    assert value.model_dump(mode="json") == original
    with session_scope(db_path) as session:
        stored = session.get(InvestmentStatementDB, value.statement_id)
        assert stored is not None
        assert stored.parsed_fingerprint == value.parsed_fingerprint


def test_account_performance_filters_preserve_source_periods(tmp_path) -> None:
    db_path = tmp_path / "synthetic.db"
    repository = InvestmentRepository(db_path)
    values = (
        statement((performance("Total (All Assets)", 2),)),
        statement(
            (performance("Total (All Assets)", 2, "5", "6", "11"),),
            period_start=date(2031, 5, 1),
            period_end=date(2031, 5, 31),
        ),
        statement((performance("Total (All Assets)", 2),), alias="EXAMPLE-USD", currency="USD"),
    )
    for value in values:
        store(repository, value, tmp_path)
    service = LedgerQueryService(db_path)

    actual = service.get_investment_performance(
        account_alias="EXAMPLE-FUND",
        start_date=date(2031, 5, 1),
        end_date=date(2031, 5, 31),
        base_currency="CAD",
    )

    assert len(actual) == 1
    assert actual[0]["statement_id"] == values[1].statement_id
    assert actual[0]["period_start"] == date(2031, 5, 1)
    assert actual[0]["period_end"] == date(2031, 5, 31)
    assert actual[0]["realized_pl"] == Decimal("5")
    assert (
        service.get_investment_performance(account_alias="EXAMPLE-FUND", base_currency="USD") == []
    )


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        pytest.param(
            (performance("Total (All Assets)", 2, "1", "2", "3"),),
            (performance("Total (All Assets)", 2, "4", "5", "9"),),
            "increased",
            id="account-total-only",
        ),
        pytest.param(
            (performance("Total", 2, "1", "2", "3"),),
            (performance("Total", 2, "4", "5", "9"),),
            "increased",
            id="unique-legacy-total",
        ),
        pytest.param(
            (
                performance("Total (All Assets)", 2, "1", "2", "3"),
                performance("Total", 4, "99", "99", "198"),
            ),
            (
                performance("Total (All Assets)", 2, "4", "5", "9"),
                performance("Total", 4, "1", "1", "2"),
            ),
            "increased",
            id="account-total-over-subtotal",
        ),
        pytest.param(
            (performance("Total", 2, "1", "1", "2"), performance("Total", 4, "9", "9", "18")),
            (performance("Total (All Assets)", 2, "5", "5", "10"),),
            "added",
            id="ambiguous-before",
        ),
        pytest.param(
            (performance("Total (All Assets)", 2, "5", "5", "10"),),
            (performance("Total", 2, "1", "1", "2"), performance("Total", 4, "9", "9", "18")),
            "removed",
            id="ambiguous-after",
        ),
        pytest.param(
            (
                performance("Total (All Assets)", 2, "1", "1", "2"),
                performance("Total (All Assets)", 3, "2", "2", "4"),
                performance("Total", 4, "9", "9", "18"),
            ),
            (
                performance("Total (All Assets)", 2, "2", "2", "4"),
                performance("Total (All Assets)", 3, "3", "3", "6"),
                performance("Total", 4, "1", "1", "2"),
            ),
            "unchanged",
            id="duplicate-account-totals",
        ),
    ],
)
def test_conflict_delta_compares_only_unambiguous_account_performance(
    tmp_path, before, after, expected
) -> None:
    db_path = tmp_path / "synthetic.db"
    repository = InvestmentRepository(db_path)
    original = statement(before)
    incoming = statement(after).model_copy(
        update={"parsed_fingerprint": sha256(b"fictional-revised-statement").hexdigest()}
    )
    store(repository, original, tmp_path)

    delta = repository.conflict_delta(incoming)

    assert delta is not None
    binding, summary = delta
    assert binding["existing_fingerprint"] == original.parsed_fingerprint
    assert binding["incoming_fingerprint"] == incoming.parsed_fingerprint
    assert summary["value_changes"]["realized_pl"] == expected
    assert summary["value_changes"]["unrealized_pl"] == expected
    assert summary["value_changes"]["total_nav"] == "unchanged"
    with session_scope(db_path) as session:
        stored = session.get(InvestmentStatementDB, original.statement_id)
        assert stored is not None
        assert stored.parsed_fingerprint == original.parsed_fingerprint
