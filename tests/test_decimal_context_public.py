import hashlib
from datetime import date
from decimal import Inexact, Rounded, localcontext
from pathlib import Path

import pytest
from mcp_synthetic import _transaction, build_synthetic_mcp_database

from ledger.mcp.server import call_tool
from ledger.mcp.service import LedgerMCPService
from ledger.storage.database import session_scope


@pytest.mark.parametrize("flow", ["income", "spending"])
@pytest.mark.parametrize("trap", [False, True])
def test_decimal_context_public_wire(tmp_path: Path, flow, trap):
    path = build_synthetic_mcp_database(tmp_path / "fixture.db")
    with session_scope(path) as session:
        for index, amount in enumerate(("12345.67", "0.04", "999.99")):
            session.add(
                _transaction(
                    f"context-{index}",
                    posted=date(2029, 3, 1),
                    amount=amount if flow == "income" else "-" + amount,
                    merchant="SYNTHETIC CONTEXT",
                    category="Synthetic Context",
                    flow="income" if flow == "income" else "expense",
                    cashflow_type="income" if flow == "income" else "expense",
                    currency="USD" if index == 2 else "CAD",
                )
            )
    service = LedgerMCPService(path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    try:
        with localcontext() as context:
            context.prec = 6
            context.traps[Inexact] = trap
            context.traps[Rounded] = trap
            result = call_tool(
                service,
                "ledger.aggregate",
                {
                    "start_date": "2029-03-01",
                    "end_date": "2029-03-01",
                    "flow": flow,
                    "group_by": "category",
                    "limit": 100,
                },
            )
        assert result.get("isError") is not True, result
        data = result["structuredContent"]["data"]
        assert data["total"] == "12345.71"
        assert data["groups"][0]["amount"] == "12345.71"
        assert data["transaction_count"] == 2
        assert data["excluded"]["non_cad"] == 1
    finally:
        service.close()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
