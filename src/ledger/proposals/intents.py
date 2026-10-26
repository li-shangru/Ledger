"""Strict, database-independent planning intent accepted by the proposal writer."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator


class IntentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    @field_validator("*", mode="after")
    @classmethod
    def safe_text(cls, value: Any) -> Any:
        if isinstance(value, str):
            if "\x00" in value:
                raise ValueError("Text contains a forbidden character.")
            try:
                value.encode("utf-8")
            except UnicodeError:
                raise ValueError("Text is not valid UTF-8.") from None
        return value

    @model_validator(mode="after")
    def reference_namespaces(self) -> Self:
        operation = getattr(self, "operation", "")
        expected = {"category": "category", "account": "acct", "counterparty_account": "acct"}
        if operation:
            expected["target"] = operation.split("_", 1)[0]
            expected["subject"] = getattr(self, "scope", "")
        for key, namespace in expected.items():
            reference = getattr(self, key, None)
            if reference is not None and not reference.ref.startswith(namespace + "-"):
                raise ValueError("Reference belongs to a different planning domain.")
        return self


class PlanningReference(IntentModel):
    ref: str = Field(
        pattern=r"^(acct-[0-9a-f]{10}|(?:budget|schedule|preference|category|merchant)-[0-9a-f]{40})$"
    )
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")


def _money(value: str) -> str:
    if not re.fullmatch(r"(?:0|[1-9][0-9]{0,9})(?:\.[0-9]{1,2})?", value):
        raise ValueError("Amount must be an explicit nonnegative decimal string in whole cents.")
    return f"{Decimal(value):.2f}"


def _date(value: str) -> str:
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("Date must be YYYY-MM-DD.")
    return date.fromisoformat(value).isoformat()


class BudgetValues(IntentModel):
    category: PlanningReference
    budget_month: str
    amount: str
    currency: Literal["CAD"]
    notes: str = Field(default="", max_length=500)

    _amount = field_validator("amount")(_money)

    @field_validator("budget_month")
    @classmethod
    def month(cls, value: str) -> str:
        if value != "*":
            if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", value):
                raise ValueError("Budget month must be '*' or YYYY-MM.")
            date.fromisoformat(value + "-01")
        return value


class BudgetCreate(BudgetValues):
    operation: Literal["budget_create"]


class BudgetUpdate(BudgetValues):
    operation: Literal["budget_update"]
    target: PlanningReference


class BudgetRemove(IntentModel):
    operation: Literal["budget_remove"]
    target: PlanningReference


class ScheduleValues(IntentModel):
    name: str = Field(min_length=1, max_length=160)
    event_type: Literal["income", "bill", "card_payment", "transfer"]
    amount: str
    currency: Literal["CAD"]
    account: PlanningReference
    counterparty_account: PlanningReference | None
    next_date: str
    frequency: Literal[
        "once", "weekly", "biweekly", "semimonthly", "monthly", "quarterly", "yearly"
    ]
    provenance: Literal["confirmed", "hypothetical"]
    is_active: bool
    note: str = Field(default="", max_length=500)

    _amount = field_validator("amount")(_money)
    _next_date = field_validator("next_date")(_date)

    @model_validator(mode="after")
    def semantics(self) -> ScheduleValues:
        if Decimal(self.amount) <= 0:
            raise ValueError("Schedule amount must be positive.")
        if not self.name.strip():
            raise ValueError("Schedule name is required.")
        needs_counterparty = self.event_type in {"transfer", "card_payment"}
        if needs_counterparty != (self.counterparty_account is not None):
            raise ValueError("Supply a destination account exactly for a transfer or card payment.")
        if (
            self.counterparty_account is not None
            and self.counterparty_account.ref == self.account.ref
        ):
            raise ValueError("Source and destination accounts must differ.")
        return self


class ScheduleChanges(IntentModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    event_type: Literal["income", "bill", "card_payment", "transfer"] | None = None
    amount: str | None = None
    currency: Literal["CAD"] | None = None
    account: PlanningReference | None = None
    counterparty_account: PlanningReference | None = None
    next_date: str | None = None
    frequency: (
        Literal["once", "weekly", "biweekly", "semimonthly", "monthly", "quarterly", "yearly"]
        | None
    ) = None
    provenance: Literal["confirmed", "hypothetical"] | None = None
    is_active: bool | None = None
    note: str | None = Field(default=None, max_length=500)

    @field_validator("amount")
    @classmethod
    def amount_value(cls, value: str | None) -> str | None:
        if value is not None:
            normalized = _money(value)
            if Decimal(normalized) <= 0:
                raise ValueError("Schedule amount must be positive.")
            return normalized
        return value

    @field_validator("next_date")
    @classmethod
    def date_value(cls, value: str | None) -> str | None:
        return _date(value) if value is not None else value

    @model_validator(mode="after")
    def nonempty(self) -> ScheduleChanges:
        if not self.model_fields_set:
            raise ValueError("At least one explicit schedule change is required.")
        if any(
            getattr(self, key) is None for key in self.model_fields_set - {"counterparty_account"}
        ):
            raise ValueError("A supplied schedule field cannot be null.")
        return self


class ScheduleCreate(ScheduleValues):
    operation: Literal["schedule_create"]
    effective_date: str
    _effective = field_validator("effective_date")(_date)


class ScheduleUpdate(IntentModel):
    operation: Literal["schedule_update"]
    target: PlanningReference
    effective_date: str
    changes: ScheduleChanges
    _effective = field_validator("effective_date")(_date)


class ScheduleAction(IntentModel):
    operation: Literal[
        "schedule_confirm", "schedule_deactivate", "schedule_reactivate", "schedule_remove"
    ]
    target: PlanningReference
    effective_date: str
    _effective = field_validator("effective_date")(_date)


class PreferenceSet(IntentModel):
    operation: Literal["preference_set"]
    scope: Literal["merchant", "category"]
    subject: PlanningReference
    disposition: Literal["intentional", "necessary", "worthwhile"]
    note: str = Field(default="", max_length=500)


class PreferenceChange(IntentModel):
    operation: Literal["preference_change"]
    target: PlanningReference
    disposition: Literal["intentional", "necessary", "worthwhile"]
    note: str | None = Field(default=None, max_length=500)


class PreferenceRemove(IntentModel):
    operation: Literal["preference_remove"]
    target: PlanningReference


PlanningIntent = Annotated[
    BudgetCreate
    | BudgetUpdate
    | BudgetRemove
    | ScheduleCreate
    | ScheduleUpdate
    | ScheduleAction
    | PreferenceSet
    | PreferenceChange
    | PreferenceRemove,
    Field(discriminator="operation"),
]
PLANNING_INTENT_ADAPTER: TypeAdapter[PlanningIntent] = TypeAdapter(PlanningIntent)


def validate_planning_intent(value: object) -> PlanningIntent:
    """Validate without opening either the ledger or the proposal store."""
    if isinstance(value, IntentModel):
        value = value.model_dump(mode="json", exclude_unset=True)
    return PLANNING_INTENT_ADAPTER.validate_python(value)


def planning_intent_schema() -> dict[str, Any]:
    return PLANNING_INTENT_ADAPTER.json_schema()
