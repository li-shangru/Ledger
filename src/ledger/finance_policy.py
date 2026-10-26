"""Canonical predicates for CAD-denominated financial reporting."""

from __future__ import annotations

from decimal import Decimal


def is_cad_row(*, currency: str | None) -> bool:
    """Return whether a native-currency row can be treated as CAD."""
    return currency in ("CAD", "", None)


def is_income_row(
    *,
    amount_signed: Decimal,
    cashflow_type: str,
    include_in_cashflow: bool,
    currency: str | None,
) -> bool:
    """Return whether a row belongs to CAD income totals."""
    return (
        amount_signed > 0
        and cashflow_type == "income"
        and bool(include_in_cashflow)
        and is_cad_row(currency=currency)
    )


def is_expense_row(
    *,
    amount_signed: Decimal,
    cashflow_type: str,
    include_in_cashflow: bool,
    currency: str | None,
) -> bool:
    """Return whether a row belongs to CAD expense cashflow."""
    return (
        amount_signed < 0
        and cashflow_type == "expense"
        and bool(include_in_cashflow)
        and is_cad_row(currency=currency)
    )


def is_spend_row(
    *,
    amount_signed: Decimal,
    cashflow_type: str,
    include_in_cashflow: bool,
    exclude_from_spending: bool,
    currency: str | None,
) -> bool:
    """Return whether a row belongs to reportable CAD spending."""
    return (
        is_expense_row(
            amount_signed=amount_signed,
            cashflow_type=cashflow_type,
            include_in_cashflow=include_in_cashflow,
            currency=currency,
        )
        and not exclude_from_spending
    )


def cad_sql_filters() -> list:
    """SQLAlchemy filters matching :func:`is_cad_row`."""
    from sqlalchemy import or_

    from ledger.storage.database import TransactionDB

    return [
        or_(
            TransactionDB.currency == "CAD",
            TransactionDB.currency.is_(None),
            TransactionDB.currency == "",
        )
    ]


def income_sql_filters() -> list:
    """SQLAlchemy filters matching :func:`is_income_row`."""
    from ledger.storage.database import TransactionDB

    return [
        TransactionDB.amount_signed > 0,
        TransactionDB.cashflow_type == "income",
        TransactionDB.include_in_cashflow == True,  # noqa: E712
        *cad_sql_filters(),
    ]


def income_treatment_sql_filter():
    """Exclude auto-applied rows that canonical income reporting nets away."""
    from sqlalchemy import or_

    from ledger.storage.database import TransactionTreatmentDB

    return or_(
        TransactionTreatmentDB.txn_id.is_(None),
        TransactionTreatmentDB.status != "auto_applied",
        TransactionTreatmentDB.netting_policy.notin_(
            ["reduce_category_cost", "exclude_from_income"]
        ),
    )


def expense_sql_filters() -> list:
    """SQLAlchemy filters matching :func:`is_expense_row`."""
    from ledger.storage.database import TransactionDB

    return [
        TransactionDB.amount_signed < 0,
        TransactionDB.cashflow_type == "expense",
        TransactionDB.include_in_cashflow == True,  # noqa: E712
        *cad_sql_filters(),
    ]


def spend_sql_filters() -> list:
    """SQLAlchemy filters matching :func:`is_spend_row`."""
    from ledger.storage.database import TransactionDB

    return [
        *expense_sql_filters(),
        TransactionDB.exclude_from_spending == False,  # noqa: E712
    ]


__all__ = [
    "cad_sql_filters",
    "expense_sql_filters",
    "income_sql_filters",
    "income_treatment_sql_filter",
    "is_cad_row",
    "is_expense_row",
    "is_income_row",
    "is_spend_row",
    "spend_sql_filters",
]
