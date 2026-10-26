"""Freshly authored investment evidence for isolated delivery probes."""

from datetime import date
from decimal import Decimal

from ledger.storage.database import (
    InvestmentActivityDB,
    InvestmentAssetBalanceDB,
    InvestmentCashBalanceDB,
    InvestmentPositionDB,
    InvestmentStatementDB,
    session_scope,
)


def populate_investment_probe(db_path):
    with session_scope(db_path) as session:
        statement = (
            session.query(InvestmentStatementDB).filter_by(period_end=date(2025, 10, 31)).one()
        )
        key = statement.statement_id
        session.add_all(
            [
                InvestmentAssetBalanceDB(
                    statement_id=key, asset_class="Stocks", current_total=Decimal("9.2535")
                ),
                InvestmentAssetBalanceDB(
                    statement_id=key, asset_class="Cash", current_total=Decimal("0.7515")
                ),
                InvestmentAssetBalanceDB(
                    statement_id=key,
                    asset_class="Dividend Accruals",
                    current_total=Decimal("0.000000000001"),
                ),
                InvestmentAssetBalanceDB(
                    statement_id=key,
                    asset_class="Interest Accruals",
                    current_total=Decimal("-0.000000000001"),
                ),
                InvestmentCashBalanceDB(
                    statement_id=key,
                    currency="CAD",
                    opening_balance=Decimal("1"),
                    debits=Decimal("0"),
                    credits=Decimal("0.002"),
                    closing_balance=Decimal("1.002"),
                    opening_fx_rate=Decimal("0.75"),
                    closing_fx_rate=Decimal("0.75"),
                ),
                InvestmentPositionDB(
                    statement_id=key,
                    source_row=1,
                    asset_category="Stocks",
                    currency="USD",
                    symbol="SYN1",
                    discriminator="Summary",
                    quantity=Decimal("3"),
                    close_price=Decimal("3.0845"),
                    market_value=Decimal("9.2535"),
                ),
                InvestmentActivityDB(
                    statement_id=key,
                    activity_id="synthetic-probe-deposit",
                    source_row=1,
                    activity_type="deposit",
                    activity_date=date(2025, 10, 2),
                    currency="CAD",
                    amount=Decimal("0.002"),
                    direction="in",
                    description="PRIVATE SYNTHETIC PROBE TEXT",
                ),
            ]
        )
