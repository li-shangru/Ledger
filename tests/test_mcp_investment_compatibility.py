"""Known legacy investment layouts remain readable without a runtime migration."""

import hashlib
import sqlite3
from contextlib import closing

import pytest
from mcp_synthetic import build_synthetic_mcp_database

from ledger.mcp.server import call_tool
from ledger.mcp.service import LedgerMCPService
from ledger.storage.read_only import IncompatibleSchemaError

CASH_MIGRATION = "20260913_0030_investment_cash_balances"


def _old_layout(path):
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("DROP TABLE investment_cash_balances")
        connection.execute("DELETE FROM schema_migrations WHERE version = ?", (CASH_MIGRATION,))


def test_previous_schema_remains_readable_and_byte_unchanged(tmp_path):
    path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    _old_layout(path)
    before = hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns
    with closing(LedgerMCPService(path)) as service:
        result = call_tool(service, "ledger.status", {})
        assert result["structuredContent"]["data"]["migration_version"] == (
            "20260912_0029_investment_pdf_statement_fields"
        )
        result = call_tool(
            service,
            "ledger.investments.statements",
            {
                "start_date": "2025-01-01",
                "end_date": "2025-12-31",
                "limit": 100,
            },
        )
        assert not result.get("isError"), result
        for item in result["structuredContent"]["data"]["items"]:
            detail = call_tool(
                service,
                "ledger.investments.statement.get",
                {
                    "statement_ref": item["statement_ref"],
                },
            )
            assert not detail.get("isError"), detail
            assert detail["structuredContent"]["data"]["cash_balances"] == []
    assert (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns) == before


@pytest.mark.parametrize(
    "mutation", ["missing_table", "missing_marker", "older", "future", "cash_view"]
)
def test_partial_unknown_and_older_schema_are_rejected(tmp_path, mutation):
    path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    with closing(sqlite3.connect(path)) as connection, connection:
        if mutation == "missing_table":
            connection.execute("DROP TABLE investment_cash_balances")
        elif mutation == "missing_marker":
            connection.execute("DELETE FROM schema_migrations WHERE version = ?", (CASH_MIGRATION,))
        elif mutation == "older":
            connection.execute("DROP TABLE investment_cash_balances")
            connection.execute(
                "DELETE FROM schema_migrations WHERE version >= ?",
                ("20260912_0029_investment_pdf_statement_fields",),
            )
        elif mutation == "cash_view":
            connection.execute("DROP TABLE investment_cash_balances")
            connection.execute("DELETE FROM schema_migrations WHERE version = ?", (CASH_MIGRATION,))
            connection.execute(
                "CREATE VIEW investment_cash_balances AS SELECT 1 AS forged_evidence"
            )
        else:
            connection.execute(
                "UPDATE schema_migrations SET version = '29990101_unknown' WHERE version = ?",
                (CASH_MIGRATION,),
            )
    with pytest.raises(IncompatibleSchemaError):
        LedgerMCPService(path)
