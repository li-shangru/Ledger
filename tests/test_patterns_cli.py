"""CLI glue coverage for the `patterns` command (M3).

`ledger patterns --all` must show ALL patterns (active and inactive), while the
default (`--active-only`) must show only active ones. Regression test for a bug
where the `--active-only/--all` flag was mapped directly onto the `is_active`
filter, making `--all` show inactive-only patterns (the inverse of its intent).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from account_fixtures import seed_account
from typer.testing import CliRunner

from ledger.cli import app
from ledger.storage.database import RecurringPatternDB, init_database, session_scope

runner = CliRunner()


def _seed_patterns(db_path: Path) -> None:
    init_database(db_path)
    account_id = seed_account(db_path, "test-bank", "acct-chequing")
    now = datetime.now()
    with session_scope(db_path) as session:
        session.add(
            RecurringPatternDB(
                pattern_id="active-pattern",
                pattern_type="subscription",
                merchant="ACTIVEM",
                category="Entertainment",
                institution="test-bank",
                account_id=account_id,
                frequency="monthly",
                expected_day_of_period=1,
                is_fixed_amount=True,
                expected_amount=Decimal("45.00"),
                amount_std_dev=Decimal("0.00"),
                confidence=0.9,
                detection_method="test",
                occurrence_count=3,
                first_occurrence=date(2026, 3, 1),
                last_occurrence=date(2026, 5, 1),
                next_expected_date=date(2026, 6, 1),
                is_active=True,
                missed_count=0,
                detected_at=now,
                updated_at=now,
            )
        )
        session.add(
            RecurringPatternDB(
                pattern_id="inactive-pattern",
                pattern_type="subscription",
                merchant="LAPSEDM",
                category="Entertainment",
                institution="test-bank",
                account_id=account_id,
                frequency="monthly",
                expected_day_of_period=1,
                is_fixed_amount=True,
                expected_amount=Decimal("15.00"),
                amount_std_dev=Decimal("0.00"),
                confidence=0.9,
                detection_method="test",
                occurrence_count=3,
                first_occurrence=date(2025, 3, 1),
                last_occurrence=date(2025, 5, 1),
                next_expected_date=date(2025, 6, 1),
                is_active=False,
                missed_count=5,
                detected_at=now,
                updated_at=now,
            )
        )


def test_patterns_all_shows_active_and_inactive(tmp_path: Path):
    db = tmp_path / "ledger.db"
    _seed_patterns(db)

    result = runner.invoke(app, ["patterns", "--db", str(db), "--all"])

    assert result.exit_code == 0, result.output
    assert "ACTIVEM" in result.output
    assert "LAPSEDM" in result.output


def test_patterns_default_shows_only_active(tmp_path: Path):
    db = tmp_path / "ledger.db"
    _seed_patterns(db)

    result = runner.invoke(app, ["patterns", "--db", str(db)])

    assert result.exit_code == 0, result.output
    assert "ACTIVEM" in result.output
    assert "LAPSEDM" not in result.output
