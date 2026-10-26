"""Read-only, source-backed queries for historical investment statements."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, localcontext
from sqlite3 import Connection as SQLiteConnection
from typing import Literal

from sqlalchemy import and_, case, func

from ledger.investments.repository import (
    _ACCOUNT_TOTAL_CATEGORY,
    _LEGACY_TOTAL_CATEGORY,
    _persisted_snapshot_signature,
)
from ledger.query.base import QueryModule
from ledger.storage.database import (
    InvestmentAccountDB,
    InvestmentActivityDB,
    InvestmentAssetBalanceDB,
    InvestmentPerformanceDB,
    InvestmentPositionDB,
    InvestmentSourceAssociationDB,
    InvestmentSourceFileDB,
    InvestmentStatementDB,
)

_DECIMAL_SCALE_12 = Decimal("0.000000000001")
_DECIMAL_ZERO_12 = "0.000000000000"


def _one_snapshot_per_account_end(session, rows: list, statement_for) -> list:
    """Validate overlapping report snapshots and select the most local period deterministically."""
    grouped: dict[tuple[str, date], list] = {}
    for row in rows:
        statement = statement_for(row)
        grouped.setdefault((statement.account_id, statement.period_end), []).append(row)
    selected_ids: set[str] = set()
    for overlaps in grouped.values():
        statements = [statement_for(row) for row in overlaps]
        if len(statements) > 1:
            signatures = {
                _persisted_snapshot_signature(session, statement) for statement in statements
            }
            if len(signatures) != 1:
                raise RuntimeError(
                    "Investment reports contain inconsistent account end-date snapshots."
                )
        selected = max(
            statements,
            key=lambda statement: (statement.period_start, statement.statement_id),
        )
        selected_ids.add(selected.statement_id)
    return [row for row in rows if statement_for(row).statement_id in selected_ids]


class _DecimalSum12:
    """SQLite aggregate that retains the declared 12-place amount scale."""

    def __init__(self) -> None:
        self.total = Decimal(_DECIMAL_ZERO_12)

    def step(self, value: object | None) -> None:
        if value is None:
            return
        with localcontext() as context:
            context.prec = 64
            self.total += Decimal(str(value))

    def finalize(self) -> str:
        with localcontext() as context:
            context.prec = 64
            return format(self.total.quantize(_DECIMAL_SCALE_12), "f")


def _selected_statement_ids(session) -> set[str]:
    """Statement ids that survive the one-snapshot-per-(account, period_end) rule.

    Overlapping reports (a monthly statement plus a year-to-date one ending on the
    same date) are an explicitly supported input, and the NAV and performance queries
    already collapse them. The activity queries did not, so every restated activity
    was counted twice in the USD flow totals and listed twice in the activity table.
    """
    statements = session.query(InvestmentStatementDB).all()
    selected = _one_snapshot_per_account_end(session, statements, lambda row: row)
    return {statement.statement_id for statement in selected}


def _filtered_investment_activity_query(
    session,
    *,
    account_alias: str | None,
    start_date: date | None,
    end_date: date | None,
    base_currency: str | None = None,
):
    """Build the common statement-owned activity query and public filters."""
    query = (
        session.query(InvestmentActivityDB, InvestmentStatementDB, InvestmentAccountDB)
        .join(
            InvestmentStatementDB,
            InvestmentStatementDB.statement_id == InvestmentActivityDB.statement_id,
        )
        .join(
            InvestmentAccountDB,
            InvestmentAccountDB.account_id == InvestmentStatementDB.account_id,
        )
        .filter(InvestmentActivityDB.statement_id.in_(_selected_statement_ids(session)))
    )
    if account_alias is not None:
        query = query.filter(InvestmentAccountDB.account_alias == account_alias)
    if base_currency is not None:
        query = query.filter(InvestmentStatementDB.base_currency == base_currency)
    if start_date is not None:
        query = query.filter(InvestmentActivityDB.activity_date >= start_date)
    if end_date is not None:
        query = query.filter(InvestmentActivityDB.activity_date <= end_date)
    return query


class InvestmentQueries(QueryModule):
    """Focused queries isolated from transaction and cashflow read models."""

    def get_investment_accounts(
        self, status: Literal["active", "closed", "unknown"] | None = None
    ) -> list[dict]:
        """Return render-safe investment account metadata in stable alias order."""
        with self._open_session() as session:
            query = session.query(InvestmentAccountDB)
            if status is not None:
                query = query.filter(InvestmentAccountDB.status == status)
            return [
                {
                    "account_id": row.account_id,
                    "institution": row.institution,
                    "account_alias": row.account_alias,
                    "account_type": row.account_type,
                    "base_currency": row.base_currency,
                    "status": row.status,
                    "transfer_destination": row.transfer_destination,
                    "status_confirmed_on": row.status_confirmed_on,
                    "closed_on": row.closed_on,
                    "first_statement_date": row.first_statement_date,
                    "last_statement_date": row.last_statement_date,
                }
                for row in query.order_by(InvestmentAccountDB.account_alias).all()
            ]

    def get_investment_date_range(self) -> tuple[date | None, date | None]:
        """Return the exact first and last reported statement dates."""
        with self._open_session() as session:
            start, end = session.query(
                func.min(InvestmentStatementDB.period_end),
                func.max(InvestmentStatementDB.period_end),
            ).one()
            return start, end

    def get_investment_nav_history(
        self,
        account_alias: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        base_currency: str | None = None,
    ) -> list[dict]:
        """Combine only statements sharing both reporting date and base currency.

        Missing account-periods remain absent: this method deliberately never
        forward-fills NAV or manufactures a combined percentage return.
        """
        with self._open_session() as session:
            query = session.query(InvestmentStatementDB).join(
                InvestmentAccountDB,
                InvestmentAccountDB.account_id == InvestmentStatementDB.account_id,
            )
            if account_alias is not None:
                query = query.filter(InvestmentAccountDB.account_alias == account_alias)
            if base_currency is not None:
                query = query.filter(InvestmentStatementDB.base_currency == base_currency)
            if start_date is not None:
                query = query.filter(InvestmentStatementDB.period_end >= start_date)
            if end_date is not None:
                query = query.filter(InvestmentStatementDB.period_end <= end_date)
            rows = query.order_by(
                InvestmentStatementDB.period_end,
                InvestmentStatementDB.base_currency,
                InvestmentStatementDB.account_id,
            ).all()
            rows = _one_snapshot_per_account_end(session, rows, lambda row: row)
            history: list[dict] = []
            for row in rows:
                if (
                    not history
                    or history[-1]["period_end"] != row.period_end
                    or history[-1]["base_currency"] != row.base_currency
                ):
                    history.append(
                        {
                            "period_end": row.period_end,
                            "base_currency": row.base_currency,
                            "total_nav": row.total_nav,
                            "contributing_accounts": 1,
                        }
                    )
                    continue
                with localcontext() as context:
                    context.prec = 64
                    history[-1]["total_nav"] += row.total_nav
                history[-1]["contributing_accounts"] += 1
            return history

    def get_investment_nav_series(
        self,
        account_alias: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        base_currency: str | None = None,
    ) -> list[dict]:
        """Return each account's exact source statement points without filling gaps."""
        with self._open_session() as session:
            query = session.query(InvestmentStatementDB, InvestmentAccountDB).join(
                InvestmentAccountDB,
                InvestmentAccountDB.account_id == InvestmentStatementDB.account_id,
            )
            if account_alias is not None:
                query = query.filter(InvestmentAccountDB.account_alias == account_alias)
            if base_currency is not None:
                query = query.filter(InvestmentStatementDB.base_currency == base_currency)
            if start_date is not None:
                query = query.filter(InvestmentStatementDB.period_end >= start_date)
            if end_date is not None:
                query = query.filter(InvestmentStatementDB.period_end <= end_date)
            rows = query.order_by(
                InvestmentStatementDB.period_end,
                InvestmentAccountDB.account_alias,
            ).all()
            rows = _one_snapshot_per_account_end(session, rows, lambda row: row[0])
            return [
                {
                    "period_end": statement.period_end,
                    "account_alias": account.account_alias,
                    "base_currency": statement.base_currency,
                    "total_nav": statement.total_nav,
                }
                for statement, account in rows
            ]

    def get_investment_statement(self, statement_id: str) -> dict | None:
        """Return one statement header and its source-backed asset balances."""
        with self._open_session() as session:
            row = (
                session.query(InvestmentStatementDB, InvestmentAccountDB)
                .join(
                    InvestmentAccountDB,
                    InvestmentAccountDB.account_id == InvestmentStatementDB.account_id,
                )
                .filter(InvestmentStatementDB.statement_id == statement_id)
                .one_or_none()
            )
            if row is None:
                return None
            statement, account = row
            assets = (
                session.query(InvestmentAssetBalanceDB)
                .filter(InvestmentAssetBalanceDB.statement_id == statement_id)
                .order_by(InvestmentAssetBalanceDB.asset_class)
                .all()
            )
            performance = (
                session.query(InvestmentPerformanceDB)
                .filter(InvestmentPerformanceDB.statement_id == statement_id)
                .order_by(InvestmentPerformanceDB.source_row)
                .all()
            )
            return {
                "statement_id": statement.statement_id,
                "account_alias": account.account_alias,
                "institution": account.institution,
                "period_start": statement.period_start,
                "period_end": statement.period_end,
                "generated_at": statement.generated_at,
                "base_currency": statement.base_currency,
                "total_nav": statement.total_nav,
                "time_weighted_return": statement.time_weighted_return,
                "asset_balances": [
                    {
                        "asset_class": asset.asset_class,
                        "prior_total": asset.prior_total,
                        "current_long": asset.current_long,
                        "current_short": asset.current_short,
                        "current_total": asset.current_total,
                        "change": asset.change,
                    }
                    for asset in assets
                ],
                "performance": [
                    {
                        "asset_category": item.asset_category,
                        "symbol": item.symbol,
                        "cost_adjustment": item.cost_adjustment,
                        "realized_short_profit": item.realized_short_profit,
                        "realized_short_loss": item.realized_short_loss,
                        "realized_long_profit": item.realized_long_profit,
                        "realized_long_loss": item.realized_long_loss,
                        "realized_total": item.realized_total,
                        "unrealized_short_profit": item.unrealized_short_profit,
                        "unrealized_short_loss": item.unrealized_short_loss,
                        "unrealized_long_profit": item.unrealized_long_profit,
                        "unrealized_long_loss": item.unrealized_long_loss,
                        "unrealized_total": item.unrealized_total,
                        "total": item.total,
                        "code": item.code,
                    }
                    for item in performance
                ],
            }

    def get_investment_positions(self, statement_id: str) -> list[dict]:
        """Return a statement's snapshot positions in deterministic source order."""
        with self._open_session() as session:
            rows = (
                session.query(InvestmentPositionDB, InvestmentAccountDB)
                .join(
                    InvestmentStatementDB,
                    InvestmentStatementDB.statement_id == InvestmentPositionDB.statement_id,
                )
                .join(
                    InvestmentAccountDB,
                    InvestmentAccountDB.account_id == InvestmentStatementDB.account_id,
                )
                .filter(InvestmentPositionDB.statement_id == statement_id)
                .order_by(
                    InvestmentPositionDB.symbol,
                    InvestmentPositionDB.discriminator,
                    InvestmentPositionDB.source_row,
                )
                .all()
            )
            return [
                {
                    "statement_id": position.statement_id,
                    "account_alias": account.account_alias,
                    "asset_category": position.asset_category,
                    "currency": position.currency,
                    "symbol": position.symbol,
                    "discriminator": position.discriminator,
                    "quantity": position.quantity,
                    "multiplier": position.multiplier,
                    "cost_price": position.cost_price,
                    "cost_basis": position.cost_basis,
                    "close_price": position.close_price,
                    "market_value": position.market_value,
                    "unrealized_pl": position.unrealized_pl,
                    "source_row": position.source_row,
                }
                for position, account in rows
            ]

    def get_investment_activities(
        self,
        account_alias: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        activity_types: list[str] | None = None,
        symbol: str | None = None,
        limit: int = 1000,
        base_currency: str | None = None,
    ) -> list[dict]:
        """Return statement-owned activities without touching banking tables."""
        with self._open_session() as session:
            query = _filtered_investment_activity_query(
                session,
                account_alias=account_alias,
                start_date=start_date,
                end_date=end_date,
                base_currency=base_currency,
            )
            if activity_types is not None:
                if not activity_types:
                    return []
                query = query.filter(InvestmentActivityDB.activity_type.in_(activity_types))
            if symbol is not None:
                query = query.filter(InvestmentActivityDB.symbol == symbol)
            rows = query.order_by(
                InvestmentActivityDB.activity_date.desc(),
                InvestmentActivityDB.activity_time.desc(),
                InvestmentAccountDB.account_alias,
                InvestmentActivityDB.source_row,
            ).limit(limit)
            return [
                {
                    "statement_id": activity.statement_id,
                    "account_alias": account.account_alias,
                    "period_end": statement.period_end,
                    "activity_id": activity.activity_id,
                    "activity_type": activity.activity_type,
                    "activity_date": activity.activity_date,
                    "activity_time": activity.activity_time,
                    "symbol": activity.symbol,
                    "currency": activity.currency,
                    "quantity": activity.quantity,
                    "trade_price": activity.trade_price,
                    "close_price": activity.close_price,
                    "amount": activity.amount,
                    "commission_fee": activity.commission_fee,
                    "basis": activity.basis,
                    "realized_pl": activity.realized_pl,
                    "mtm_pl": activity.mtm_pl,
                    "description": activity.description,
                    "direction": activity.direction,
                    "source_row": activity.source_row,
                }
                for activity, statement, account in rows.all()
            ]

    def get_investment_activity_types(
        self,
        account_alias: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        base_currency: str | None = None,
    ) -> list[str]:
        """Return every distinct activity type independently of detail limits."""
        with self._open_session() as session:
            query = _filtered_investment_activity_query(
                session,
                account_alias=account_alias,
                start_date=start_date,
                end_date=end_date,
                base_currency=base_currency,
            )
            rows = (
                query.with_entities(InvestmentActivityDB.activity_type)
                .distinct()
                .order_by(InvestmentActivityDB.activity_type)
                .all()
            )
            return [str(row[0]) for row in rows]

    def get_investment_activity_symbols(
        self,
        account_alias: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        base_currency: str | None = None,
    ) -> list[str]:
        """Return every distinct non-empty symbol independently of detail limits."""
        with self._open_session() as session:
            query = _filtered_investment_activity_query(
                session,
                account_alias=account_alias,
                start_date=start_date,
                end_date=end_date,
                base_currency=base_currency,
            )
            rows = (
                query.with_entities(InvestmentActivityDB.symbol)
                .filter(InvestmentActivityDB.symbol.is_not(None))
                .filter(InvestmentActivityDB.symbol != "")
                .distinct()
                .order_by(InvestmentActivityDB.symbol)
                .all()
            )
            return [str(row[0]) for row in rows]

    def get_investment_activity_summary(
        self,
        account_alias: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        base_currency: str | None = None,
        currency: str = "USD",
    ) -> dict[str, Decimal]:
        """Return uncapped exact native-currency totals, without FX conversion."""
        with self._open_session() as session:
            query = _filtered_investment_activity_query(
                session,
                account_alias=account_alias,
                start_date=start_date,
                end_date=end_date,
                base_currency=base_currency,
            )
            query = query.filter(InvestmentActivityDB.currency == currency)
            dbapi_connection = session.connection().connection.driver_connection
            if not isinstance(dbapi_connection, SQLiteConnection):
                raise RuntimeError("Investment activity summaries require SQLite.")
            dbapi_connection.create_aggregate(
                "decimal_sum_12",
                1,
                _DecimalSum12,  # type: ignore[arg-type]  # typeshed fixes aggregate I/O to int
            )

            flow_amount = case(
                (
                    InvestmentActivityDB.activity_type.in_(("deposit", "withdrawal")),
                    InvestmentActivityDB.amount,
                ),
                else_=Decimal("0"),
            )
            income_amount = case(
                (
                    InvestmentActivityDB.activity_type.in_(("dividend", "interest")),
                    InvestmentActivityDB.amount,
                ),
                else_=Decimal("0"),
            )
            totals = query.with_entities(
                func.decimal_sum_12(flow_amount),
                func.decimal_sum_12(income_amount),
            ).one()
            return {
                "net_deposits_withdrawals": Decimal(str(totals[0] or _DECIMAL_ZERO_12)),
                "dividends_interest": Decimal(str(totals[1] or _DECIMAL_ZERO_12)),
            }

    def get_investment_performance(
        self,
        account_alias: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        base_currency: str | None = None,
    ) -> list[dict]:
        """Return statement-reported account-level performance, never a blend.

        Adapters use ``Total (All Assets)`` for the account total. A unique ``Total``
        is a legacy fallback only when no account-total row exists. Ambiguous or
        missing totals remain unavailable, and source row order implies no role.
        """
        with self._open_session() as session:
            account_row = case(
                (
                    InvestmentPerformanceDB.asset_category == _ACCOUNT_TOTAL_CATEGORY,
                    InvestmentPerformanceDB.source_row,
                )
            )
            legacy_row = case(
                (
                    InvestmentPerformanceDB.asset_category == _LEGACY_TOTAL_CATEGORY,
                    InvestmentPerformanceDB.source_row,
                )
            )
            account_total = (
                session.query(
                    InvestmentPerformanceDB.statement_id.label("statement_id"),
                    case(
                        (func.count(account_row) == 1, func.max(account_row)),
                        (
                            and_(func.count(account_row) == 0, func.count(legacy_row) == 1),
                            func.max(legacy_row),
                        ),
                    ).label("source_row"),
                )
                .filter(
                    InvestmentPerformanceDB.asset_category.in_(
                        (_ACCOUNT_TOTAL_CATEGORY, _LEGACY_TOTAL_CATEGORY)
                    )
                )
                .group_by(InvestmentPerformanceDB.statement_id)
                .subquery()
            )
            query = (
                session.query(
                    InvestmentStatementDB,
                    InvestmentAccountDB,
                    InvestmentPerformanceDB,
                )
                .join(
                    InvestmentAccountDB,
                    InvestmentAccountDB.account_id == InvestmentStatementDB.account_id,
                )
                .outerjoin(
                    account_total,
                    account_total.c.statement_id == InvestmentStatementDB.statement_id,
                )
                .outerjoin(
                    InvestmentPerformanceDB,
                    and_(
                        InvestmentPerformanceDB.statement_id == account_total.c.statement_id,
                        InvestmentPerformanceDB.source_row == account_total.c.source_row,
                    ),
                )
            )
            if account_alias is not None:
                query = query.filter(InvestmentAccountDB.account_alias == account_alias)
            if base_currency is not None:
                query = query.filter(InvestmentStatementDB.base_currency == base_currency)
            if start_date is not None:
                query = query.filter(InvestmentStatementDB.period_end >= start_date)
            if end_date is not None:
                query = query.filter(InvestmentStatementDB.period_end <= end_date)
            rows = query.order_by(
                InvestmentStatementDB.period_end.desc(), InvestmentAccountDB.account_alias
            ).all()
            rows = _one_snapshot_per_account_end(session, rows, lambda row: row[0])
            result: list[dict] = []
            for statement, account, reported in rows:
                result.append(
                    {
                        "statement_id": statement.statement_id,
                        "account_alias": account.account_alias,
                        "base_currency": statement.base_currency,
                        "period_start": statement.period_start,
                        "period_end": statement.period_end,
                        "total_nav": statement.total_nav,
                        "time_weighted_return": statement.time_weighted_return,
                        "realized_pl": reported.realized_total if reported else None,
                        "unrealized_pl": reported.unrealized_total if reported else None,
                        "total_pl": reported.total if reported else None,
                    }
                )
            return result

    def get_investment_provenance(self, statement_id: str) -> dict | None:
        """Return only alias, period, and verified import/archive lifecycle facts."""
        with self._open_session() as session:
            row = (
                session.query(InvestmentStatementDB, InvestmentAccountDB)
                .join(
                    InvestmentAccountDB,
                    InvestmentAccountDB.account_id == InvestmentStatementDB.account_id,
                )
                .filter(InvestmentStatementDB.statement_id == statement_id)
                .one_or_none()
            )
            if row is None:
                return None
            statement, account = row
            associations = (
                session.query(InvestmentSourceAssociationDB)
                .filter(
                    InvestmentSourceAssociationDB.statement_id == statement_id,
                    InvestmentSourceAssociationDB.superseded_at.is_(None),
                )
                .all()
            )
            statuses: set[str]
            archives: set[str | None]
            if associations:
                statuses = {source.status for source in associations}
                archives = {source.archive_status for source in associations}
                has_sources = True
            else:
                # Compatibility for a freshly-created/unmigrated synthetic DB. The
                # migration backfills durable associations before production use.
                legacy_sources = (
                    session.query(InvestmentSourceFileDB)
                    .filter(InvestmentSourceFileDB.statement_id == statement_id)
                    .all()
                )
                statuses = {source.status for source in legacy_sources}
                archives = {source.archive_status for source in legacy_sources}
                has_sources = bool(legacy_sources)
            if has_sources and statuses == {"finalized"}:
                import_state = "finalized"
            elif "failed" in statuses:
                import_state = "failed"
            elif has_sources and "published" in statuses:
                # The statement's rows ARE live; only the archive tail is outstanding.
                # This previously fell through to "staged" whenever NONE of the sources
                # had been archived yet, so the UI captioned already-published data as
                # "import staged" -- the opposite of the contract, which names this
                # state archive_pending and says to resume the archive tail only.
                import_state = "archive_pending"
            elif "archived" in archives:
                import_state = "archive_pending"
            else:
                import_state = "staged"
            return {
                "account_alias": account.account_alias,
                "period_start": statement.period_start,
                "period_end": statement.period_end,
                "import_state": import_state,
                "archive_verified": has_sources and archives == {"archived"},
            }
