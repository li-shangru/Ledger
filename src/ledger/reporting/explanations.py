"""Deterministic, bounded explanations for displayed financial metrics."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, aliased

from ledger.adjustments.service import DETECTOR_SOURCE, EXCLUDE_POLICY
from ledger.finance_policy import cad_sql_filters, income_sql_filters
from ledger.storage.database import (
    AccountBalanceDB,
    AccountDB,
    TransactionDB,
    TransactionOffsetDB,
    TransactionTreatmentDB,
    session_scope,
)
from ledger.utils.money import to_cents

SessionScopeFactory = Callable[[Path], AbstractContextManager[Session]]

EVIDENCE_LIMIT = 20
SUPPORTED_METRICS = frozenset(
    {
        "income",
        "gross_spending",
        "true_cost",
        "net",
        "offsets",
        "balance",
        "budget",
        "budget_actual",
        "budget_variance",
        "projection",
    }
)
CURRENCY_POLICY = (
    "CAD totals include CAD plus legacy blank-currency rows; native non-CAD rows are excluded "
    "without conversion."
)


def _money(value: Decimal | float | int | str | None) -> Decimal:
    return to_cents(value)


def _money_text(value: Decimal | float | int | str | None) -> str:
    amount = _money(value)
    if amount == 0:
        amount = Decimal("0.00")
    return format(amount, ".2f")


def _component(name: str, operation: str, amount: Decimal) -> dict[str, str]:
    return {"name": name, "operation": operation, "amount": _money_text(amount)}


def _component_total(components: list[dict[str, str]]) -> Decimal:
    total = Decimal("0.00")
    for component in components:
        amount = Decimal(component["amount"])
        total += amount if component["operation"] == "add" else -amount
    return _money(total)


class MetricExplanationService:
    """Explain one metric using the same immutable read-session boundary as its caller."""

    def __init__(
        self,
        db_path: Path,
        *,
        session_scope_factory: SessionScopeFactory = session_scope,
    ) -> None:
        self.db_path = Path(db_path)
        self._session_scope_factory = session_scope_factory
        if session_scope_factory is session_scope and not self.db_path.is_file():
            raise FileNotFoundError(f"Database not found: {self.db_path}")

    def _open_session(self) -> AbstractContextManager[Session]:
        return self._session_scope_factory(self.db_path)

    def explain(
        self,
        metric: str,
        start_date: date,
        end_date: date,
        account_id: str | None = None,
        category: str | None = None,
        balance_basis: str | None = None,
        balance_date: date | None = None,
    ) -> dict[str, Any]:
        """Return a JSON-safe explanation for one inclusive reporting window."""
        if metric not in SUPPORTED_METRICS:
            raise ValueError(f"Unsupported metric: {metric}")
        if not isinstance(start_date, date) or not isinstance(end_date, date):
            raise TypeError("start_date and end_date must be date values")
        if end_date < start_date:
            raise ValueError("end_date precedes start_date")
        if metric == "income":
            return self._explain_income(
                start_date=start_date,
                end_date=end_date,
                account_id=account_id,
                category=category,
            )
        if metric in {"gross_spending", "offsets", "true_cost", "net"}:
            return self._explain_period_metric(
                metric=metric,
                start_date=start_date,
                end_date=end_date,
                account_id=account_id,
                category=category,
            )
        if metric == "balance":
            return self._explain_balance(
                start_date=start_date,
                end_date=end_date,
                account_id=account_id,
                category=category,
                balance_basis=balance_basis,
                balance_date=balance_date,
            )
        if metric in {"budget", "budget_actual", "budget_variance"}:
            return self._explain_budget(
                metric=metric,
                start_date=start_date,
                end_date=end_date,
                account_id=account_id,
                category=category,
            )
        if metric == "projection":
            return self._explain_projection(
                start_date=start_date,
                end_date=end_date,
                account_id=account_id,
                category=category,
            )
        return self._unavailable(
            metric,
            start_date,
            end_date,
            reason="metric_not_implemented",
            detail=f"{metric} explanation is not available.",
        )

    def _explain_income(
        self,
        *,
        start_date: date,
        end_date: date,
        account_id: str | None,
        category: str | None,
    ) -> dict[str, Any]:
        with self._open_session() as session:
            base = self._transaction_base(
                session,
                start_date=start_date,
                end_date=end_date,
                account_id=account_id,
                category=category,
            )
            if base is None:
                return self._unavailable(
                    "income",
                    start_date,
                    end_date,
                    reason="account_not_found",
                    detail="The requested account is not available.",
                )
            values = self._income_values(base)
            components = [
                _component("gross_income", "add", values["gross_income"]),
                _component("refunds", "subtract", values["refunds"]),
                _component("reimbursements", "subtract", values["reimbursements"]),
                _component("other_adjustments", "subtract", values["other_adjustments"]),
            ]
            floor_at_zero = _money(
                max(values["adjustments"] - values["gross_income"], Decimal("0.00"))
            )
            if floor_at_zero:
                components.append(_component("floor_at_zero", "add", floor_at_zero))
            relevant_transactions, evidence_count, source_earliest, source_latest = (
                self._income_evidence(base)
            )
            candidate = base.filter(
                TransactionDB.amount_signed > 0,
                TransactionDB.cashflow_type == "income",
            )
            exclusion_values = self._exclusion_values(candidate, spending=False)

        return self._reported(
            metric="income",
            formula="max(0, gross_income - refunds - reimbursements - other_adjustments)",
            start_date=start_date,
            end_date=end_date,
            total=values["adjusted_income"],
            components=components,
            source_earliest=source_earliest,
            source_latest=source_latest,
            exclusions=self._exclusions(**exclusion_values),
            adjustments={
                "refunds": _money_text(values["refunds"]),
                "reimbursements": _money_text(values["reimbursements"]),
                "other_adjustments": _money_text(values["other_adjustments"]),
                "total": _money_text(values["adjustments"]),
                "applied_to_total": True,
            },
            relevant_transactions=relevant_transactions,
            evidence_count=evidence_count,
        )

    def _explain_balance(
        self,
        *,
        start_date: date,
        end_date: date,
        account_id: str | None,
        category: str | None,
        balance_basis: str | None,
        balance_date: date | None,
    ) -> dict[str, Any]:
        if account_id is None:
            return self._unavailable(
                "balance",
                start_date,
                end_date,
                reason="missing_account_id",
                detail="A balance explanation requires one account_id.",
            )
        if category is not None:
            return self._unavailable(
                "balance",
                start_date,
                end_date,
                reason="unsupported_filter",
                detail="Category does not apply to a statement balance.",
            )
        basis = balance_basis or "absolute"
        if basis not in {"absolute", "relative"}:
            return self._unavailable(
                "balance",
                start_date,
                end_date,
                reason="unsupported_filter",
                detail="Balance basis must be absolute or relative.",
            )
        with self._open_session() as session:
            if session.get(AccountDB, account_id) is None:
                return self._unavailable(
                    "balance",
                    start_date,
                    end_date,
                    reason="account_not_found",
                    detail="The requested account is not available.",
                )
            anchor_query = session.query(AccountBalanceDB).filter(
                AccountBalanceDB.account_id == account_id,
                AccountBalanceDB.balance_basis == basis,
                AccountBalanceDB.balance_date <= end_date,
            )
            if balance_date is not None:
                anchor_query = anchor_query.filter(AccountBalanceDB.balance_date == balance_date)
            anchor = anchor_query.order_by(AccountBalanceDB.balance_date.desc()).first()
            if anchor is None:
                if balance_date is not None:
                    return self._unavailable(
                        "balance",
                        start_date,
                        end_date,
                        reason="displayed_snapshot_not_found",
                        detail="The displayed balance snapshot is no longer available.",
                    )
                future_anchor = (
                    session.query(AccountBalanceDB.account_id)
                    .filter(
                        AccountBalanceDB.account_id == account_id,
                        AccountBalanceDB.balance_basis == basis,
                    )
                    .first()
                )
                reason = (
                    "no_anchor_on_or_before_as_of"
                    if future_anchor is not None
                    else f"no_{basis}_anchor"
                )
                return self._unavailable(
                    "balance",
                    start_date,
                    end_date,
                    reason=reason,
                    detail=(
                        f"No {basis} balance snapshot is available on or before the as-of date."
                        if future_anchor is not None
                        else f"No {basis} balance snapshot is available for this account."
                    ),
                )
            total = _money(anchor.balance)
            anchor_date = anchor.balance_date

        is_absolute = basis == "absolute"
        detail_key = "statement_anchor" if is_absolute else "balance_snapshot"
        return self._reported(
            metric="balance",
            formula="statement_anchor_as_of" if is_absolute else "relative_change_as_of",
            start_date=start_date,
            end_date=end_date,
            total=total,
            components=[
                _component(
                    "statement_anchor" if is_absolute else "relative_account_change",
                    "add",
                    total,
                )
            ],
            source_earliest=anchor_date,
            source_latest=anchor_date,
            exclusions=self._exclusions(),
            adjustments=self._empty_adjustments(applied=False),
            relevant_transactions=[],
            evidence_count=0,
            details={
                detail_key: {
                    "basis": basis,
                    "balance_date": anchor_date.isoformat(),
                    "as_of": end_date.isoformat(),
                    "lag_days": (end_date - anchor_date).days,
                    "is_historical": anchor_date < end_date,
                }
            },
        )

    def _explain_budget(
        self,
        *,
        metric: str,
        start_date: date,
        end_date: date,
        account_id: str | None,
        category: str | None,
    ) -> dict[str, Any]:
        from ledger.planning.service import BudgetService

        if account_id is not None:
            return self._unavailable(
                metric,
                start_date,
                end_date,
                reason="unsupported_filter",
                detail="Budgets are category-scoped and do not accept account_id.",
            )
        service = BudgetService(
            self.db_path,
            session_scope_factory=self._session_scope_factory,
            prepare_schema=False,
        )
        definitions = service.list_budgets()
        months = self._month_labels(start_date, end_date)
        matching = [
            row
            for row in definitions
            if (category is None or str(row["category"]) == category)
            and str(row["month"] or "*") in {"*", *months}
        ]
        rows = service.budget_variance(start_date, end_date)
        selected = [row for row in rows if category is None or str(row["category"]) == category]
        if not selected or (metric == "budget" and category is not None and not matching):
            return self._unavailable(
                metric,
                start_date,
                end_date,
                reason="no_budget_defined",
                detail="No matching budget definition applies to this period.",
            )
        total = _money(sum((Decimal(str(row["budget"])) for row in selected), Decimal("0.00")))
        actual = _money(sum((Decimal(str(row["actual"])) for row in selected), Decimal("0.00")))
        variance = _money(total - actual)
        budget_categories = sorted({str(row["category"]) for row in selected})

        with self._open_session() as session:
            base = session.query(TransactionDB).filter(
                TransactionDB.posting_date >= start_date,
                TransactionDB.posting_date <= end_date,
            )
            if category is not None:
                base = base.filter(TransactionDB.category == category)
            else:
                base = base.filter(TransactionDB.category.in_(budget_categories))
            candidate = base.filter(
                TransactionDB.amount_signed < 0,
                TransactionDB.cashflow_type == "expense",
            )
            exclusion_values = self._exclusion_values(candidate, spending=True)
            evidence_query = base.filter(*self._spend_filters()).with_entities(TransactionDB)
            evidence_count = evidence_query.count()
            bounds = evidence_query.with_entities(
                func.min(TransactionDB.posting_date), func.max(TransactionDB.posting_date)
            ).first()
            evidence_rows = (
                evidence_query.order_by(TransactionDB.posting_date, TransactionDB.txn_id)
                .limit(EVIDENCE_LIMIT)
                .all()
            )
            relevant_transactions = [
                {
                    "evidence_id": str(row.txn_id),
                    "posting_date": row.posting_date.isoformat(),
                    "amount": _money_text(abs(row.amount_signed)),
                    "currency": row.currency or "CAD",
                    "category": str(row.category),
                    "merchant": str(row.merchant),
                    "role": "actual_gross_spending",
                }
                for row in evidence_rows
            ]

        statuses = {str(row["status"]) for row in selected}
        if metric == "budget_actual":
            formula = "sum(gross_CAD_spending_in_budget_scope)"
            displayed_total = actual
            components = [_component("gross_CAD_spending_in_budget_scope", "add", actual)]
        elif metric == "budget_variance":
            formula = "budgeted - actual_gross_spending"
            displayed_total = variance
            components = [
                _component("budgeted", "add", total),
                _component("actual_gross_spending", "subtract", actual),
            ]
        else:
            formula = "sum(prorated_matching_budget_definitions)"
            displayed_total = total
            components = [_component("prorated_matching_budget_definitions", "add", total)]
        return self._reported(
            metric=metric,
            formula=formula,
            start_date=start_date,
            end_date=end_date,
            total=displayed_total,
            components=components,
            source_earliest=None if bounds is None else bounds[0],
            source_latest=None if bounds is None else bounds[1],
            exclusions=self._exclusions(**exclusion_values),
            adjustments=self._empty_adjustments(applied=False),
            relevant_transactions=relevant_transactions,
            evidence_count=evidence_count,
            details={
                "budget_context": {
                    "category": category,
                    "actual_gross_spending": _money_text(actual),
                    "variance": _money_text(variance),
                    "status": next(iter(statuses)) if len(statuses) == 1 else "mixed",
                    "definition_count": len(matching),
                    "actual_basis": "gross CAD reportable spending",
                }
            },
        )

    def _explain_projection(
        self,
        *,
        start_date: date,
        end_date: date,
        account_id: str | None,
        category: str | None,
    ) -> dict[str, Any]:
        from ledger.planning.cash_plan import CashPlanError, CashPlanService

        if account_id is None:
            return self._unavailable(
                "projection",
                start_date,
                end_date,
                reason="missing_account_id",
                detail="A projection explanation requires one account_id.",
            )
        if category is not None:
            return self._unavailable(
                "projection",
                start_date,
                end_date,
                reason="unsupported_filter",
                detail="Category cannot scope a whole-account projected balance.",
            )
        service = CashPlanService(
            self.db_path,
            session_scope_factory=self._session_scope_factory,
            prepare_schema=False,
        )
        try:
            plan = service.build(
                start_date=start_date,
                end_date=end_date,
                as_of=start_date,
            )
        except CashPlanError as exc:
            return self._unavailable(
                "projection",
                start_date,
                end_date,
                reason=exc.code,
                detail=str(exc),
            )

        account = next(
            (row for row in plan.accounts if row.account_id == account_id),
            None,
        )
        if account is None:
            return self._unavailable(
                "projection",
                start_date,
                end_date,
                reason="account_not_found",
                detail="The requested account is not available.",
            )
        anchor = account.anchor
        if anchor.balance is None or anchor.balance_date is None:
            return self._unavailable(
                "projection",
                start_date,
                end_date,
                reason=anchor.status,
                detail="A projection requires an absolute statement anchor on or before as-of.",
            )
        if account.projected_balance is None:
            return self._unavailable(
                "projection",
                start_date,
                end_date,
                reason="projected_balance_unavailable",
                detail="The cash plan did not produce a balance for this account.",
            )

        anchor_balance = _money(anchor.balance)
        movement = _money(account.movement_since_anchor)
        total = _money(account.projected_balance)
        account_legs = list(account.legs)
        lines = [
            {
                "occurrence_id": line.occurrence_id,
                "source_id": line.source_id,
                "source_kind": line.source_kind,
                "name": line.name,
                "event_type": line.event_type,
                "amount": _money_text(line.amount),
                "currency": line.currency,
                "provenance": line.provenance,
                "occurrence_date": line.occurrence_date.isoformat(),
                "confidence": line.confidence,
                "direction": line.direction,
                "is_spending": line.is_spending,
                "in_selected_period": line.in_selected_period,
            }
            for line in account_legs[:EVIDENCE_LIMIT]
        ]
        suppressions = [
            row
            for row in plan.suppressions
            if row.account_id == account_id or row.counterparty_account_id == account_id
        ]
        suppression_rows = [
            {
                "occurrence_id": row.occurrence_id,
                "source_id": row.source_id,
                "source_kind": row.source_kind,
                "name": row.name,
                "event_type": row.event_type,
                "occurrence_date": row.occurrence_date.isoformat(),
                "amount": _money_text(row.amount),
                "currency": row.currency,
                "provenance": row.provenance,
                "matched_kind": row.matched_kind,
                "matched_evidence_ids": list(row.matched_evidence_ids),
            }
            for row in suppressions[:EVIDENCE_LIMIT]
        ]
        source_dates = [
            anchor.balance_date,
            *(line.occurrence_date for line in account_legs),
            *(row.occurrence_date for row in suppressions),
        ]
        coverage = plan.coverage
        return self._reported(
            metric="projection",
            formula="statement_anchor + cash_plan_movement_since_anchor",
            start_date=start_date,
            end_date=end_date,
            total=total,
            components=[
                _component("statement_anchor", "add", anchor_balance),
                _component("cash_plan_movement_since_anchor", "add", movement),
            ],
            source_earliest=min(source_dates),
            source_latest=max(source_dates),
            exclusions=self._exclusions(),
            adjustments=self._empty_adjustments(applied=False),
            relevant_transactions=[],
            evidence_count=0,
            details={
                "projection_context": {
                    "as_of": start_date.isoformat(),
                    "target_date": end_date.isoformat(),
                    "anchor": {
                        "basis": "absolute",
                        "balance": _money_text(anchor_balance),
                        "balance_date": anchor.balance_date.isoformat(),
                        "as_of": start_date.isoformat(),
                        "lag_days": anchor.as_of_lag_days,
                    },
                    "opening_projected_balance": _money_text(account.opening_projected_balance),
                    "period_projected_movement": _money_text(account.projected_movement),
                    "net_projected_flow": _money_text(movement),
                    "lowest_projected_balance": _money_text(account.lowest_projected_balance),
                    "lowest_projected_date": (
                        None
                        if account.lowest_projected_date is None
                        else account.lowest_projected_date.isoformat()
                    ),
                    "line_items": lines,
                    "line_item_summary": {
                        "limit": EVIDENCE_LIMIT,
                        "total_count": len(account_legs),
                        "returned_count": len(lines),
                        "truncated": len(account_legs) > len(lines),
                    },
                    "suppressions": suppression_rows,
                    "suppression_summary": {
                        "limit": EVIDENCE_LIMIT,
                        "total_count": len(suppressions),
                        "returned_count": len(suppression_rows),
                        "truncated": len(suppressions) > len(suppression_rows),
                    },
                    "source_coverage": {
                        "stored_schedule_count": coverage.stored_schedule_count,
                        "active_schedule_count": coverage.active_schedule_count,
                        "confirmed_schedule_count": coverage.confirmed_schedule_count,
                        "hypothetical_schedule_count": coverage.hypothetical_schedule_count,
                        "detected_pattern_count": coverage.detected_pattern_count,
                        "overridden_detected_pattern_count": (
                            coverage.overridden_detected_pattern_count
                        ),
                        "suppressed_occurrence_count": (coverage.suppressed_occurrence_count),
                        "ambiguous_suppression_count": (coverage.ambiguous_suppression_count),
                    },
                    "assumptions": list(plan.assumptions),
                }
            },
        )

    def _explain_period_metric(
        self,
        *,
        metric: str,
        start_date: date,
        end_date: date,
        account_id: str | None,
        category: str | None,
    ) -> dict[str, Any]:
        with self._open_session() as session:
            base = self._transaction_base(
                session,
                start_date=start_date,
                end_date=end_date,
                account_id=account_id,
                category=category,
            )
            if base is None:
                return self._unavailable(
                    metric,
                    start_date,
                    end_date,
                    reason="account_not_found",
                    detail="The requested account is not available.",
                )

            gross_spending = _money(
                base.filter(*self._spend_filters())
                .with_entities(func.sum(func.abs(TransactionDB.amount_signed)))
                .scalar()
            )
            expense_adjustments = self._expense_adjustments(base)
            expense_offsets = _money(sum(expense_adjustments.values(), Decimal("0.00")))
            true_cost = _money(max(gross_spending - expense_offsets, Decimal("0.00")))
            true_cost_floor = _money(max(expense_offsets - gross_spending, Decimal("0.00")))

            income_values = self._income_values(base)
            adjusted_income = income_values["adjusted_income"]

            if metric == "gross_spending":
                formula = "reportable_spending"
                total = gross_spending
                components = [_component("reportable_spending", "add", gross_spending)]
            elif metric == "offsets":
                formula = "refunds + reimbursements + other_adjustments"
                total = expense_offsets
                components = [
                    _component("refunds", "add", expense_adjustments["refunds"]),
                    _component("reimbursements", "add", expense_adjustments["reimbursements"]),
                    _component(
                        "other_adjustments", "add", expense_adjustments["other_adjustments"]
                    ),
                ]
            elif metric == "true_cost":
                formula = "max(0, gross_spending - refunds - reimbursements - other_adjustments)"
                total = true_cost
                components = [
                    _component("gross_spending", "add", gross_spending),
                    _component("refunds", "subtract", expense_adjustments["refunds"]),
                    _component("reimbursements", "subtract", expense_adjustments["reimbursements"]),
                    _component(
                        "other_adjustments",
                        "subtract",
                        expense_adjustments["other_adjustments"],
                    ),
                ]
                if true_cost_floor:
                    components.append(_component("floor_at_zero", "add", true_cost_floor))
            else:
                formula = "adjusted_income - true_cost"
                total = _money(adjusted_income - true_cost)
                components = [
                    _component("adjusted_income", "add", adjusted_income),
                    _component("true_cost", "subtract", true_cost),
                ]

            candidate = base.filter(
                TransactionDB.amount_signed < 0,
                TransactionDB.cashflow_type == "expense",
            )
            exclusion_values = self._exclusion_values(candidate, spending=True)
            if metric == "net":
                income_candidate = base.filter(
                    TransactionDB.amount_signed > 0,
                    TransactionDB.cashflow_type == "income",
                )
                income_exclusions = self._exclusion_values(income_candidate, spending=False)
                for key in (
                    "non_cad",
                    "not_in_cashflow",
                    "excluded_from_spending",
                ):
                    exclusion_values[key] += income_exclusions[key]
                for key in (
                    "non_cad_amount",
                    "not_in_cashflow_amount",
                    "excluded_from_spending_amount",
                ):
                    exclusion_values[key] = _money(exclusion_values[key] + income_exclusions[key])

            relevant_transactions, evidence_count, source_earliest, source_latest = (
                self._spending_evidence(base)
            )
            if metric == "net":
                income_rows, income_count, income_earliest, income_latest = self._income_evidence(
                    base
                )
                relevant_transactions, evidence_count = self._merge_evidence(
                    relevant_transactions,
                    evidence_count,
                    income_rows,
                    income_count,
                )
                source_earliest = self._earliest(source_earliest, income_earliest)
                source_latest = self._latest(source_latest, income_latest)

        adjustments: dict[str, Any] = {
            "refunds": _money_text(expense_adjustments["refunds"]),
            "reimbursements": _money_text(expense_adjustments["reimbursements"]),
            "other_adjustments": _money_text(expense_adjustments["other_adjustments"]),
            "total": _money_text(expense_offsets),
            "applied_to_total": metric != "gross_spending",
        }
        if metric == "net":
            adjustments["income_adjustments"] = {
                "refunds": _money_text(income_values["refunds"]),
                "reimbursements": _money_text(income_values["reimbursements"]),
                "other_adjustments": _money_text(income_values["other_adjustments"]),
                "total": _money_text(income_values["adjustments"]),
            }

        return self._reported(
            metric=metric,
            formula=formula,
            start_date=start_date,
            end_date=end_date,
            total=total,
            components=components,
            source_earliest=source_earliest,
            source_latest=source_latest,
            exclusions=self._exclusions(**exclusion_values),
            adjustments=adjustments,
            relevant_transactions=relevant_transactions,
            evidence_count=evidence_count,
        )

    @staticmethod
    def _spend_filters() -> list[Any]:
        from ledger.finance_policy import spend_sql_filters

        return spend_sql_filters()

    @staticmethod
    def _transaction_base(
        session: Session,
        *,
        start_date: date,
        end_date: date,
        account_id: str | None,
        category: str | None,
    ) -> Any | None:
        query = session.query(TransactionDB).filter(
            TransactionDB.posting_date >= start_date,
            TransactionDB.posting_date <= end_date,
        )
        if category is not None:
            query = query.filter(TransactionDB.category == category)
        if account_id is None:
            return query
        account = session.get(AccountDB, account_id)
        if account is None:
            return None
        return query.filter(
            TransactionDB.account == account.account_number,
            TransactionDB.institution == account.institution,
        )

    @staticmethod
    def _income_values(base: Any) -> dict[str, Decimal]:
        gross_income = _money(
            base.filter(*income_sql_filters())
            .with_entities(func.sum(TransactionDB.amount_signed))
            .scalar()
        )
        offset_rows = (
            base.join(
                TransactionOffsetDB,
                TransactionOffsetDB.credit_txn_id == TransactionDB.txn_id,
            )
            .outerjoin(
                TransactionTreatmentDB,
                TransactionTreatmentDB.txn_id == TransactionDB.txn_id,
            )
            .filter(
                *income_sql_filters(),
                TransactionOffsetDB.status == "auto_applied",
                TransactionOffsetDB.source == DETECTOR_SOURCE,
            )
            .with_entities(
                TransactionTreatmentDB.treatment_type,
                func.sum(TransactionOffsetDB.offset_amount),
            )
            .group_by(TransactionTreatmentDB.treatment_type)
            .all()
        )
        grouped = {
            "refunds": Decimal("0.00"),
            "reimbursements": Decimal("0.00"),
            "other_adjustments": Decimal("0.00"),
        }
        for treatment_type, raw_total in offset_rows:
            key = (
                "refunds"
                if treatment_type == "product_return"
                else "reimbursements"
                if treatment_type == "reimbursement"
                else "other_adjustments"
            )
            grouped[key] += _money(raw_total)
        excluded_income = _money(
            base.join(
                TransactionTreatmentDB,
                TransactionTreatmentDB.txn_id == TransactionDB.txn_id,
            )
            .filter(
                *income_sql_filters(),
                TransactionTreatmentDB.status == "auto_applied",
                TransactionTreatmentDB.netting_policy == EXCLUDE_POLICY,
            )
            .with_entities(func.sum(TransactionDB.amount_signed))
            .scalar()
        )
        grouped["other_adjustments"] = _money(grouped["other_adjustments"] + excluded_income)
        adjustments = _money(sum(grouped.values(), Decimal("0.00")))
        return {
            "gross_income": gross_income,
            **grouped,
            "adjustments": adjustments,
            "adjusted_income": _money(max(gross_income - adjustments, Decimal("0.00"))),
        }

    @staticmethod
    def _expense_adjustments(base: Any) -> dict[str, Decimal]:
        rows = (
            base.join(
                TransactionOffsetDB,
                TransactionOffsetDB.debit_txn_id == TransactionDB.txn_id,
            )
            .outerjoin(
                TransactionTreatmentDB,
                TransactionTreatmentDB.txn_id == TransactionOffsetDB.credit_txn_id,
            )
            .filter(
                *MetricExplanationService._spend_filters(),
                TransactionOffsetDB.status == "auto_applied",
                TransactionOffsetDB.source == DETECTOR_SOURCE,
            )
            .with_entities(
                TransactionTreatmentDB.treatment_type,
                func.sum(TransactionOffsetDB.offset_amount),
            )
            .group_by(TransactionTreatmentDB.treatment_type)
            .all()
        )
        grouped = {
            "refunds": Decimal("0.00"),
            "reimbursements": Decimal("0.00"),
            "other_adjustments": Decimal("0.00"),
        }
        for treatment_type, raw_total in rows:
            key = (
                "refunds"
                if treatment_type == "product_return"
                else "reimbursements"
                if treatment_type == "reimbursement"
                else "other_adjustments"
            )
            grouped[key] += _money(raw_total)
        return {key: _money(value) for key, value in grouped.items()}

    @staticmethod
    def _income_evidence(
        base: Any,
    ) -> tuple[list[dict[str, Any]], int, date | None, date | None]:
        query = (
            base.outerjoin(
                TransactionTreatmentDB,
                TransactionTreatmentDB.txn_id == TransactionDB.txn_id,
            )
            .filter(*income_sql_filters())
            .with_entities(TransactionDB, TransactionTreatmentDB.treatment_type)
        )
        count = query.count()
        bounds = query.with_entities(
            func.min(TransactionDB.posting_date), func.max(TransactionDB.posting_date)
        ).first()
        rows = query.order_by(TransactionDB.posting_date, TransactionDB.txn_id).limit(
            EVIDENCE_LIMIT
        )
        evidence = [
            {
                "evidence_id": str(row.txn_id),
                "posting_date": row.posting_date.isoformat(),
                "amount": _money_text(row.amount_signed),
                "currency": row.currency or "CAD",
                "category": str(row.category),
                "merchant": str(row.merchant),
                "role": (
                    "refund"
                    if treatment_type == "product_return"
                    else "reimbursement"
                    if treatment_type == "reimbursement"
                    else "other_adjustment"
                    if treatment_type is not None
                    else "gross_income"
                ),
            }
            for row, treatment_type in rows
        ]
        return (
            evidence,
            count,
            None if bounds is None else bounds[0],
            None if bounds is None else bounds[1],
        )

    @staticmethod
    def _spending_evidence(
        base: Any,
    ) -> tuple[list[dict[str, Any]], int, date | None, date | None]:
        debit_query = base.filter(*MetricExplanationService._spend_filters()).with_entities(
            TransactionDB
        )
        debit_count = debit_query.count()
        debit_bounds = debit_query.with_entities(
            func.min(TransactionDB.posting_date), func.max(TransactionDB.posting_date)
        ).first()
        debit_rows = (
            debit_query.order_by(TransactionDB.posting_date, TransactionDB.txn_id)
            .limit(EVIDENCE_LIMIT)
            .all()
        )
        credit = aliased(TransactionDB)
        offset_query = (
            base.join(
                TransactionOffsetDB,
                TransactionOffsetDB.debit_txn_id == TransactionDB.txn_id,
            )
            .join(credit, credit.txn_id == TransactionOffsetDB.credit_txn_id)
            .outerjoin(
                TransactionTreatmentDB,
                TransactionTreatmentDB.txn_id == credit.txn_id,
            )
            .filter(
                *MetricExplanationService._spend_filters(),
                TransactionOffsetDB.status == "auto_applied",
                TransactionOffsetDB.source == DETECTOR_SOURCE,
            )
            .with_entities(credit, TransactionTreatmentDB.treatment_type)
        )
        credit_count = int(
            offset_query.with_entities(func.count(func.distinct(credit.txn_id))).scalar() or 0
        )
        credit_bounds = offset_query.with_entities(
            func.min(credit.posting_date), func.max(credit.posting_date)
        ).first()
        offset_rows = (
            offset_query.distinct()
            .order_by(credit.posting_date, credit.txn_id)
            .limit(EVIDENCE_LIMIT)
            .all()
        )
        by_id: dict[str, tuple[date, dict[str, Any]]] = {}
        for row in debit_rows:
            by_id[str(row.txn_id)] = (
                row.posting_date,
                {
                    "evidence_id": str(row.txn_id),
                    "posting_date": row.posting_date.isoformat(),
                    "amount": _money_text(abs(row.amount_signed)),
                    "currency": row.currency or "CAD",
                    "category": str(row.category),
                    "merchant": str(row.merchant),
                    "role": "gross_spending",
                },
            )
        for row, treatment_type in offset_rows:
            by_id[str(row.txn_id)] = (
                row.posting_date,
                {
                    "evidence_id": str(row.txn_id),
                    "posting_date": row.posting_date.isoformat(),
                    "amount": _money_text(row.amount_signed),
                    "currency": row.currency or "CAD",
                    "category": str(row.category),
                    "merchant": str(row.merchant),
                    "role": (
                        "refund"
                        if treatment_type == "product_return"
                        else "reimbursement"
                        if treatment_type == "reimbursement"
                        else "other_adjustment"
                    ),
                },
            )
        ordered = sorted(by_id.values(), key=lambda item: (item[0], item[1]["evidence_id"]))
        source_earliest = MetricExplanationService._earliest(
            None if debit_bounds is None else debit_bounds[0],
            None if credit_bounds is None else credit_bounds[0],
        )
        source_latest = MetricExplanationService._latest(
            None if debit_bounds is None else debit_bounds[1],
            None if credit_bounds is None else credit_bounds[1],
        )
        return (
            [item[1] for item in ordered[:EVIDENCE_LIMIT]],
            debit_count + credit_count,
            source_earliest,
            source_latest,
        )

    @staticmethod
    def _merge_evidence(
        first: list[dict[str, Any]],
        first_count: int,
        second: list[dict[str, Any]],
        second_count: int,
    ) -> tuple[list[dict[str, Any]], int]:
        # The canonical income and spending predicates are disjoint by sign, so
        # counts compose and evidence ids cannot collide across the two lists.
        rows = sorted(
            [*first, *second],
            key=lambda row: (str(row["posting_date"]), str(row["evidence_id"])),
        )[:EVIDENCE_LIMIT]
        return rows, first_count + second_count

    @staticmethod
    def _earliest(first: date | None, second: date | None) -> date | None:
        return min((value for value in (first, second) if value is not None), default=None)

    @staticmethod
    def _latest(first: date | None, second: date | None) -> date | None:
        return max((value for value in (first, second) if value is not None), default=None)

    @staticmethod
    def _exclusion_values(candidate: Any, *, spending: bool) -> dict[str, Any]:
        non_cad_predicate = ~or_(*cad_sql_filters())
        not_in_cashflow_predicate = TransactionDB.include_in_cashflow == False  # noqa: E712
        excluded_predicate = TransactionDB.exclude_from_spending == True  # noqa: E712

        def _count(predicate: Any) -> int:
            return int(candidate.filter(predicate).count())

        def _amount(predicate: Any) -> Decimal:
            return _money(
                candidate.filter(predicate)
                .with_entities(func.sum(func.abs(TransactionDB.amount_signed)))
                .scalar()
            )

        return {
            "non_cad": _count(non_cad_predicate),
            "not_in_cashflow": _count(not_in_cashflow_predicate),
            "excluded_from_spending": _count(excluded_predicate) if spending else 0,
            "non_cad_amount": _amount(non_cad_predicate),
            "not_in_cashflow_amount": _amount(not_in_cashflow_predicate),
            "excluded_from_spending_amount": (
                _amount(excluded_predicate) if spending else Decimal("0.00")
            ),
        }

    @staticmethod
    def _month_labels(start_date: date, end_date: date) -> set[str]:
        labels: set[str] = set()
        current = start_date.replace(day=1)
        last = end_date.replace(day=1)
        while current <= last:
            labels.add(current.strftime("%Y-%m"))
            current = (
                date(current.year + 1, 1, 1)
                if current.month == 12
                else date(current.year, current.month + 1, 1)
            )
        return labels

    @staticmethod
    def _exclusions(
        *,
        non_cad: int = 0,
        not_in_cashflow: int = 0,
        excluded_from_spending: int = 0,
        non_cad_amount: Decimal = Decimal("0.00"),
        not_in_cashflow_amount: Decimal = Decimal("0.00"),
        excluded_from_spending_amount: Decimal = Decimal("0.00"),
    ) -> dict[str, Any]:
        return {
            "counts": {
                "non_cad": non_cad,
                "not_in_cashflow": not_in_cashflow,
                "excluded_from_spending": excluded_from_spending,
            },
            "amounts": {
                "non_cad": _money_text(non_cad_amount),
                "not_in_cashflow": _money_text(not_in_cashflow_amount),
                "excluded_from_spending": _money_text(excluded_from_spending_amount),
            },
            "counts_may_overlap": True,
        }

    @staticmethod
    def _empty_adjustments(*, applied: bool) -> dict[str, Any]:
        return {
            "refunds": "0.00",
            "reimbursements": "0.00",
            "other_adjustments": "0.00",
            "total": "0.00",
            "applied_to_total": applied,
        }

    @staticmethod
    def _reported(
        *,
        metric: str,
        formula: str,
        start_date: date,
        end_date: date,
        total: Decimal,
        components: list[dict[str, str]],
        source_earliest: date | None,
        source_latest: date | None,
        exclusions: dict[str, Any],
        adjustments: dict[str, Any],
        relevant_transactions: list[dict[str, Any]],
        evidence_count: int,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        reconciled = _component_total(components)
        result: dict[str, Any] = {
            "schema_version": 1,
            "status": "reported",
            "metric": metric,
            "formula": formula,
            "period": {
                "start": start_date.isoformat(),
                "end": end_date.isoformat(),
                "inclusive": True,
            },
            "currency": "CAD",
            "currency_policy": CURRENCY_POLICY,
            "source_date_bounds": {
                "earliest": None if source_earliest is None else source_earliest.isoformat(),
                "latest": None if source_latest is None else source_latest.isoformat(),
            },
            "total": _money_text(total),
            "components": components,
            "exclusions": exclusions,
            "adjustments": adjustments,
            "relevant_transactions": relevant_transactions,
            "evidence": {
                "limit": EVIDENCE_LIMIT,
                "total_count": evidence_count,
                "returned_count": len(relevant_transactions),
                "truncated": evidence_count > len(relevant_transactions),
            },
            "reconciliation": {
                "status": "exact" if reconciled == total else "mismatch",
                "component_total": _money_text(reconciled),
                "displayed_total": _money_text(total),
                "difference": _money_text(reconciled - total),
            },
            "details": details or {},
        }
        if details:
            result.update(details)
        return result

    @staticmethod
    def _unavailable(
        metric: str,
        start_date: date,
        end_date: date,
        *,
        reason: str,
        detail: str,
    ) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "status": "unavailable",
            "metric": metric,
            "formula": None,
            "period": {
                "start": start_date.isoformat(),
                "end": end_date.isoformat(),
                "inclusive": True,
            },
            "currency": "CAD",
            "currency_policy": CURRENCY_POLICY,
            "source_date_bounds": {"earliest": None, "latest": None},
            "total": None,
            "components": [],
            "exclusions": None,
            "adjustments": None,
            "relevant_transactions": [],
            "evidence": {
                "limit": EVIDENCE_LIMIT,
                "total_count": 0,
                "returned_count": 0,
                "truncated": False,
            },
            "reconciliation": {
                "status": "unavailable",
                "component_total": None,
                "displayed_total": None,
                "difference": None,
            },
            "unavailable": {"reason": reason, "detail": detail},
        }


__all__ = ["EVIDENCE_LIMIT", "MetricExplanationService", "SUPPORTED_METRICS"]
