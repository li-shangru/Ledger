"""CLI glue coverage for the `reports` command group (F33).

The ReportSnapshotService/query layer is heavily tested; these assert the thin Typer
wiring (db-path resolution, date parsing, delegation, error handling) that had no test
-- a malformed --start must give a clean nonzero exit, not a traceback.
"""

from pathlib import Path

from typer.testing import CliRunner

from ledger.cli import app
from ledger.storage.database import init_database

runner = CliRunner()


def test_reports_refresh_happy_path(tmp_path: Path):
    db = tmp_path / "ledger.db"
    init_database(db)
    result = runner.invoke(
        app,
        ["reports", "refresh", "--db", str(db), "--start", "2026-01-01", "--end", "2026-01-31"],
    )
    assert result.exit_code == 0, result.output
    assert "Refreshed report snapshot" in result.output


def test_reports_refresh_malformed_date_exits_cleanly(tmp_path: Path):
    db = tmp_path / "ledger.db"
    init_database(db)
    result = runner.invoke(
        app,
        ["reports", "refresh", "--db", str(db), "--start", "not-a-date", "--end", "2026-01-31"],
    )
    assert result.exit_code == 1
    assert "not a valid date" in result.output.lower()
    # A clean handled error, not an unhandled exception.
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_reports_refresh_requires_both_dates(tmp_path: Path):
    db = tmp_path / "ledger.db"
    init_database(db)
    result = runner.invoke(app, ["reports", "refresh", "--db", str(db), "--start", "2026-01-01"])
    assert result.exit_code == 1
    assert "together" in result.output.lower()
