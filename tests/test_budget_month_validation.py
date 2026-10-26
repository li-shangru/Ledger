from pathlib import Path

import pytest

from ledger.planning.service import BudgetService


@pytest.mark.parametrize(
    "month",
    [
        "2026-00",
        "2026-13",
        "0000-01",
        "2026-1",
        "2026-001",
    ],
)
def test_budget_month_rejects_non_calendar_values(tmp_path: Path, month: str) -> None:
    result = BudgetService(tmp_path / "ledger.db").replace_budgets(
        [{"month": month, "category": "Dining", "amount": "100.00"}]
    )

    assert result.status == "failed"
    assert result.errors == [f"Budget month must be '*' or YYYY-MM: {month}"]


@pytest.mark.parametrize("month", ["2026-01", "*"])
def test_budget_month_accepts_calendar_month_and_wildcard(tmp_path: Path, month: str) -> None:
    service = BudgetService(tmp_path / "ledger.db")

    result = service.replace_budgets([{"month": month, "category": "Dining", "amount": "100.00"}])

    assert result.status == "success"
    assert [row["month"] for row in service.list_budgets()] == [month]
