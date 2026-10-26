"""Precise source periods qualify coverage; month labels and filenames do not."""

import json
from datetime import date

import pytest

from ledger.imports.pdf.post_processing import persist_statement_coverage
from ledger.storage.database import StatementCoverageIntervalDB, session_scope
from ledger.storage.migrations import ensure_schema


def test_interval_propagation_is_qualified_and_reimport_discards_stale_coverage(tmp_path):
    path = tmp_path / "synthetic.db"
    ensure_schema(path)
    manifest = tmp_path / "manifest.json"
    entry = {
        "account_slug": "HARBOR-CHEQUING-1001",
        "period": "2026-09",
        "statement_type": "harbor_deposit",
        "transaction_count": 0,
        "skipped_reason": "no_transactions_extracted",
        "coverage_start_date": "2026-09-01",
        "coverage_end_date": "2026-09-30",
        "coverage_basis": "printed_statement_period",
    }
    manifest.write_text(json.dumps([entry]))
    persist_statement_coverage(path, manifest)
    with session_scope(path) as session:
        rows = session.query(StatementCoverageIntervalDB).all()
        assert len(rows) == 1
        assert rows[0].account_slug == "harbor-chequing-1001"
        assert rows[0].start_date == date(2026, 9, 1)
        assert rows[0].end_date == date(2026, 9, 30)
    entry["coverage_basis"] = ""
    manifest.write_text(json.dumps([entry]))
    persist_statement_coverage(path, manifest)
    with session_scope(path) as session:
        assert session.query(StatementCoverageIntervalDB).count() == 0


pytestmark = pytest.mark.usefixtures("fictional_adapter_metadata")
