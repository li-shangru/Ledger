"""Bounded, content-free investment statement reads for the MCP boundary."""

from __future__ import annotations

import hashlib
import hmac
import re
from datetime import date
from decimal import Decimal, localcontext
from typing import Any

from sqlalchemy import func, inspect
from sqlalchemy.orm import Session

from ledger.investments.contracts import (
    ACTIVITY_DECIMALS,
    ASSET_DECIMALS,
    CASH_DECIMALS,
    DIRECTIONS,
    DISCRIMINATORS,
    GENERIC_ASSET_VOCABULARY,
    LABEL_PATTERN,
    POSITION_DECIMALS,
    SYMBOL_PATTERN,
    AssetCategoryVocabulary,
)
from ledger.investments.contracts import EXACT_DECIMAL_PATTERN as EXACT_DECIMAL_PATTERN
from ledger.investments.models import InvestmentActivityType
from ledger.query.agent import AgentQueryError, _cursor_fingerprint, _decode_cursor, _encode_cursor
from ledger.storage.database import (
    InvestmentAccountDB,
    InvestmentActivityDB,
    InvestmentAssetBalanceDB,
    InvestmentCashBalanceDB,
    InvestmentPerformanceDB,
    InvestmentPositionDB,
    InvestmentSourceAssociationDB,
    InvestmentSourceFileDB,
    InvestmentStatementDB,
)
from ledger.storage.exact_decimal import canonical_fixed_point_text
from ledger.storage.operations import account_ref as mint_account_ref

MAX_INVESTMENT_CANDIDATES = 10_000
MAX_INVESTMENT_ACCOUNTS = 1_000
MAX_INVESTMENT_SUMMARY_ROWS = 32
STATEMENT_REF_PATTERN = r"^stmt-[0-9a-f]{24}$"
COMMON_SEMANTICS = {
    "evidence_basis": "retained_structured_investment_statements",
    "currency_semantics": "native_currency_only_no_conversion_or_cross_currency_total",
    "period_semantics": "statement_end_inclusive_selection_overlapping_statements_do_not_sum",
    "freshness_semantics": "statement_period_end_snapshot_not_live_market_value",
}
RECONCILIATION_SEMANTICS = (
    "exact_stored_field_arithmetic_only_not_raw_source_verification; "
    "quantity_times_price_differences_do_not_establish_importer_failure"
)


def _decimal(value: object, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    try:
        return canonical_fixed_point_text(value)  # type: ignore[arg-type]
    except ValueError, TypeError:
        raise AgentQueryError("corrupt_data") from None


def _enum(value: object, values: tuple[str, ...]) -> str:
    if not isinstance(value, str) or value not in values:
        raise AgentQueryError("corrupt_data")
    return value


def _day(value: object) -> str:
    if type(value) is not date:
        raise AgentQueryError("corrupt_data")
    return value.isoformat()


def _currency(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Z]{3}", value) is None:
        raise AgentQueryError("corrupt_data")
    return value


def _symbol(value: object, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    if not isinstance(value, str) or len(value) > 48 or re.fullmatch(SYMBOL_PATTERN, value) is None:
        raise AgentQueryError("corrupt_data")
    return value


def _private_key(value: object, *, maximum: int = 200) -> str:
    # Keys are never emitted, but malformed keys cannot mint a convincing ref.
    if not isinstance(value, str) or not 1 <= len(value) <= maximum:
        raise AgentQueryError("corrupt_data")
    try:
        value.encode("utf-8", errors="strict")
    except UnicodeError:
        raise AgentQueryError("corrupt_data") from None
    return value


def _label(value: object, *, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or len(value) > maximum
        or re.fullmatch(LABEL_PATTERN, value) is None
    ):
        raise AgentQueryError("corrupt_data")
    return value


def _statement_ref(salt: str, statement_id: str, fingerprint: str) -> str:
    identity = _private_key(statement_id, maximum=128)
    if not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
        raise AgentQueryError("corrupt_data")
    digest = hmac.new(
        salt.encode(), f"investment-statement|{identity}|{fingerprint}".encode(), "sha256"
    )
    return f"stmt-{digest.hexdigest()[:24]}"


def _count(query: Any, *, maximum: int | None = None) -> int:
    value = query.count()
    if value > (MAX_INVESTMENT_CANDIDATES if maximum is None else maximum):
        raise AgentQueryError("filter_too_broad")
    return value


def _accounts(session: Session, salt: str) -> dict[str, tuple[InvestmentAccountDB, str]]:
    rows = session.query(InvestmentAccountDB).limit(MAX_INVESTMENT_ACCOUNTS + 1).all()
    if len(rows) > MAX_INVESTMENT_ACCOUNTS:
        raise AgentQueryError("filter_too_broad")
    result: dict[str, tuple[InvestmentAccountDB, str]] = {}
    seen: set[str] = set()
    for row in rows:
        identity = (
            _private_key(row.institution, maximum=80),
            row.account_alias,
            _private_key(row.account_type, maximum=100),
        )
        # An empty configured alias is valid and uses the canonical resolver's inputs.
        if not isinstance(identity[1], str) or len(identity[1]) > 200:
            raise AgentQueryError("corrupt_data")
        ref = mint_account_ref(salt, *identity)
        if ref in seen:
            raise AgentQueryError("corrupt_data")
        seen.add(ref)
        result[_private_key(row.account_id, maximum=128)] = (row, ref)
    return result


def _header(session: Session, row: InvestmentStatementDB, accounts: dict, salt: str) -> dict:
    start, end = _day(row.period_start), _day(row.period_end)
    if start > end or row.account_id not in accounts:
        raise AgentQueryError("corrupt_data")
    account, ref = accounts[row.account_id]
    latest = (
        session.query(func.max(InvestmentStatementDB.period_end))
        .filter(InvestmentStatementDB.account_id == row.account_id)
        .scalar()
    )
    latest_day = _day(latest)
    return {
        "statement_ref": _statement_ref(salt, row.statement_id, row.parsed_fingerprint),
        "account_ref": ref,
        "institution": _label(account.institution, maximum=50),
        "investment_account_type": _label(account.account_type, maximum=100),
        "period": {"start": start, "end": end},
        "base_currency": _currency(row.base_currency),
        "total_nav": _decimal(row.total_nav),
        "time_weighted_return": _decimal(row.time_weighted_return, nullable=True),
        "account_status": _enum(account.status, ("active", "closed", "unknown")),
        "latest_statement_end": latest_day,
        "is_latest_statement": end == latest_day,
    }


def _resolve_statement(session: Session, reference: str, salt: str) -> InvestmentStatementDB:
    rows = (
        session.query(InvestmentStatementDB.statement_id, InvestmentStatementDB.parsed_fingerprint)
        .limit(MAX_INVESTMENT_CANDIDATES + 1)
        .all()
    )
    if len(rows) > MAX_INVESTMENT_CANDIDATES:
        raise AgentQueryError("filter_too_broad")
    matches = [
        key
        for key, fingerprint in rows
        if hmac.compare_digest(_statement_ref(salt, key, fingerprint), reference)
    ]
    if len(matches) != 1:
        raise AgentQueryError("invalid_argument")
    row = session.get(InvestmentStatementDB, matches[0])
    if row is None:
        raise AgentQueryError("corrupt_data")
    return row


def _page(items: list, total: int, offset: int, limit: int, fingerprint: str) -> dict:
    if offset > total:
        raise AgentQueryError("invalid_cursor")
    next_offset = offset + len(items)
    more = next_offset < total
    return {
        "has_more": more,
        "limit": limit,
        "next_cursor": _encode_cursor(offset=next_offset, fingerprint=fingerprint)
        if more
        else None,
        "returned": len(items),
    }


def _row_number(value: object) -> int:
    if type(value) is not int or not 0 <= value <= 2_147_483_647:
        raise AgentQueryError("corrupt_data")
    return value


def _position(row: InvestmentPositionDB, vocabulary: AssetCategoryVocabulary) -> dict:
    _row_number(row.source_row)
    return {
        "asset_category": _enum(row.asset_category, vocabulary.asset_classes),
        "currency": _currency(row.currency),
        "symbol": _symbol(row.symbol),
        "discriminator": _enum(row.discriminator, DISCRIMINATORS),
        **{
            name: _decimal(getattr(row, name), nullable=name != "quantity")
            for name in POSITION_DECIMALS
        },
    }


def _activity(row: InvestmentActivityDB) -> dict:
    _row_number(row.source_row)
    return {
        "activity_type": _enum(
            row.activity_type, tuple(item.value for item in InvestmentActivityType)
        ),
        "activity_date": _day(row.activity_date),
        "symbol": _symbol(row.symbol, nullable=True),
        "currency": _currency(row.currency),
        "direction": None if row.direction is None else _enum(row.direction, DIRECTIONS),
        **{name: _decimal(getattr(row, name), nullable=True) for name in ACTIVITY_DECIMALS},
    }


def _summary(
    session: Session, statement: InvestmentStatementDB, vocabulary: AssetCategoryVocabulary
) -> dict:
    """Retain source cash FX in base-currency units per one native cash unit.

    Missing FX stays null, including base-currency cash; identity rates are never
    inferred. The arithmetic below uses native cash amounts without conversion.
    """
    key = statement.statement_id
    assets = (
        session.query(InvestmentAssetBalanceDB)
        .filter_by(statement_id=key)
        .order_by(InvestmentAssetBalanceDB.asset_class)
        .limit(MAX_INVESTMENT_SUMMARY_ROWS + 1)
        .all()
    )
    if len(assets) > MAX_INVESTMENT_SUMMARY_ROWS:
        raise AgentQueryError("filter_too_broad")
    balances = [
        {
            "asset_class": _enum(row.asset_class, vocabulary.nav_asset_classes),
            **{
                name: _decimal(getattr(row, name), nullable=name != "current_total")
                for name in ASSET_DECIMALS
            },
        }
        for row in assets
    ]
    # Read-only clients can open older databases: absence is unavailable evidence.
    cash = []
    if inspect(session.connection()).has_table("investment_cash_balances"):
        cash = (
            session.query(InvestmentCashBalanceDB)
            .filter_by(statement_id=key)
            .order_by(InvestmentCashBalanceDB.currency)
            .limit(MAX_INVESTMENT_SUMMARY_ROWS + 1)
            .all()
        )
    if len(cash) > MAX_INVESTMENT_SUMMARY_ROWS:
        raise AgentQueryError("filter_too_broad")
    cash_balances = []
    with localcontext() as context:
        context.prec = 60
        for row in cash:
            block: dict[str, Any] = {
                "currency": _currency(row.currency),
                **{
                    name: _decimal(getattr(row, name), nullable=name.endswith("fx_rate"))
                    for name in CASH_DECIMALS
                },
            }
            if (
                row.debits < 0
                or row.credits < 0
                or any(
                    rate is not None and rate <= 0
                    for rate in (row.opening_fx_rate, row.closing_fx_rate)
                )
            ):
                raise AgentQueryError("corrupt_data")
            expected = row.opening_balance + row.credits - row.debits
            cash_difference = row.closing_balance - expected
            block["reconciliation"] = {
                "status": "matches" if cash_difference == 0 else "differs",
                "expected_closing_balance": format(expected, "f"),
                "difference": format(cash_difference, "f"),
            }
            cash_balances.append(block)
        components = [row.current_total for row in assets if row.asset_class != "Total"]
        component_total = sum(components, Decimal(0)) if components else None
        difference = None if component_total is None else statement.total_nav - component_total
        explicit_total = next(
            (row.current_total for row in assets if row.asset_class == "Total"), None
        )
        explicit_difference = (
            None if explicit_total is None else statement.total_nav - explicit_total
        )
        nav_reconciliation = {
            "currency": _currency(statement.base_currency),
            "status": "unavailable"
            if difference is None
            else "matches"
            if difference == 0
            else "differs",
            "component_total": None if component_total is None else format(component_total, "f"),
            "difference": None if difference is None else format(difference, "f"),
            "explicit_total": _decimal(explicit_total, nullable=True),
            "explicit_total_difference": None
            if explicit_difference is None
            else format(explicit_difference, "f"),
            "explicit_total_excluded_from_components": True,
        }
    counts = {
        "positions": _count(session.query(InvestmentPositionDB).filter_by(statement_id=key)),
        "activities": _count(session.query(InvestmentActivityDB).filter_by(statement_id=key)),
        "performance_rows": _count(
            session.query(InvestmentPerformanceDB).filter_by(statement_id=key)
        ),
        "source_files": _count(session.query(InvestmentSourceFileDB).filter_by(statement_id=key)),
        "source_associations": _count(
            session.query(InvestmentSourceAssociationDB).filter_by(statement_id=key)
        ),
    }
    return {
        "asset_balances": balances,
        "asset_balances_availability": "available" if balances else "unavailable",
        "cash_balances": cash_balances,
        "cash_balances_availability": "available" if cash_balances else "unavailable",
        "source_counts": counts,
        "nav_reconciliation": nav_reconciliation,
        "reconciliation_semantics": RECONCILIATION_SEMANTICS,
    }


def build_investment_result(
    session: Session,
    *,
    tool: str,
    generation: str,
    salt: str | None,
    start_date: date | None = None,
    end_date: date | None = None,
    account_ref: str | None = None,
    statement_ref: str | None = None,
    limit: int = 50,
    cursor: str | None = None,
    vocabulary: AssetCategoryVocabulary = GENERIC_ASSET_VOCABULARY,
) -> dict:
    """Build data/page blocks; the service owns the guarded common envelope."""
    listing = tool == "ledger.investments.statements"
    detail = tool == "ledger.investments.statement.get"
    if (
        tool
        not in {
            "ledger.investments.statements",
            "ledger.investments.statement.get",
            "ledger.investments.positions",
            "ledger.investments.activities",
        }
        or type(limit) is not int
        or not 1 <= limit <= 100
    ):
        raise AgentQueryError("invalid_argument")
    if listing:
        if type(start_date) is not date or type(end_date) is not date or start_date > end_date:
            raise AgentQueryError("invalid_argument")
        if account_ref is not None and (
            not isinstance(account_ref, str)
            or re.fullmatch(r"acct-[0-9a-f]{10}", account_ref) is None
        ):
            raise AgentQueryError("invalid_argument")
    elif (
        not isinstance(statement_ref, str)
        or re.fullmatch(STATEMENT_REF_PATTERN, statement_ref) is None
    ):
        raise AgentQueryError("invalid_argument")
    fingerprint = _cursor_fingerprint(
        kind=tool,
        start_date=start_date,
        end_date=end_date,
        generation=generation,
        scope={
            "account_ref": account_ref,
            "statement_ref": statement_ref,
            "salt_digest": hashlib.sha256((salt or "").encode()).hexdigest(),
            "asset_vocabulary": vocabulary.digest,
        },
    )
    offset = _decode_cursor(cursor, fingerprint=fingerprint)
    data: dict = {**COMMON_SEMANTICS, "availability": "available", "unavailable_reason": None}
    if not salt:
        data.update(availability="unavailable", unavailable_reason="account_ref_salt_absent")
        result = {"data": data}
        if not detail:
            result["page"] = _page([], 0, offset, limit, fingerprint)
        return result
    accounts = _accounts(session, salt)
    if listing:
        query = session.query(InvestmentStatementDB).filter(
            InvestmentStatementDB.period_end >= start_date,
            InvestmentStatementDB.period_end <= end_date,
        )
        if account_ref is not None:
            ids = [
                key for key, (_, ref) in accounts.items() if hmac.compare_digest(ref, account_ref)
            ]
            if len(ids) != 1:
                raise AgentQueryError("invalid_argument")
            query = query.filter(InvestmentStatementDB.account_id == ids[0])
        total = _count(query)
        rows = (
            query.order_by(
                InvestmentStatementDB.period_end,
                InvestmentStatementDB.period_start,
                InvestmentStatementDB.account_id,
                InvestmentStatementDB.statement_id,
            )
            .offset(offset)
            .limit(limit)
            .all()
        )
        items = [_header(session, row, accounts, salt) for row in rows]
        data.update(
            items=items,
            total_count=total,
            queried_period={"start": _day(start_date), "end": _day(end_date)},
        )
    else:
        statement = _resolve_statement(session, statement_ref, salt)  # type: ignore[arg-type]
        data["statement"] = _header(session, statement, accounts, salt)
        if detail:
            data.update(_summary(session, statement, vocabulary))
            return {"data": data}
        model: Any = (
            InvestmentPositionDB if tool == "ledger.investments.positions" else InvestmentActivityDB
        )
        query = session.query(model).filter_by(statement_id=statement.statement_id)
        total = _count(query)
        order = (
            (model.source_row,)
            if model is InvestmentPositionDB
            else (model.source_row, model.activity_id)
        )
        rows = query.order_by(*order).offset(offset).limit(limit).all()
        items = [
            _position(row, vocabulary) if model is InvestmentPositionDB else _activity(row)
            for row in rows
        ]
        data.update(items=items, total_count=total)
    return {"data": data, "page": _page(items, total, offset, limit, fingerprint)}
