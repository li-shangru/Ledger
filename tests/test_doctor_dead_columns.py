"""Dead-column diagnostics follow the static producer manifest.

An all-NULL lifecycle field is valid until its lifecycle event occurs. Empty
columns therefore cannot identify dead schema fields."""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ledger.maintenance import doctor as doctor_module
from ledger.maintenance.doctor import DatabaseDoctor
from ledger.storage.database import init_database
from ledger.storage.migrations import run_migrations


def _diagnose(db_path: Path, tmp_path: Path) -> dict[str, object]:
    return {
        finding.check: finding
        for finding in DatabaseDoctor(db_path, audit_runs_dir=tmp_path / "none").diagnose()
    }


def _prepared_db(tmp_path: Path) -> Path:
    """A database with every migration applied -- so 0023 will not re-drop columns."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    assert run_migrations(db_path).ok
    return db_path


def _insert_transaction(db_path: Path, txn_id: str, extra: dict[str, object]) -> None:
    columns = [
        "txn_id",
        "posting_date",
        "institution",
        "account",
        "account_type",
        "flow",
        "cashflow_type",
        "include_in_cashflow",
        "currency",
        "amount_raw",
        "amount_signed",
        "description",
        "merchant",
        "category",
        "merchant_confidence",
        "category_confidence",
        "exclude_from_spending",
        "merchant_raw",
        "category_raw",
        "source_file",
        "source_path",
        "source_period",
        "source_row",
        "imported_at",
        "parser_version",
        "txn_fingerprint",
        "day_of_week",
        "day_of_month",
        "week_of_year",
        "is_weekend",
        "is_month_start",
        "is_month_end",
    ]
    values: list[object] = [
        txn_id,
        "2044-02-10",
        "harbor",
        "harbor-chequing",
        "Chequing",
        "expense",
        "expense",
        1,
        "CAD",
        "7.40",
        "-7.40",
        "TEST",
        "TEST",
        "Other",
        "low",
        "low",
        0,
        "TEST",
        "",
        "stmt.csv",
        "2044-02/stmt.csv",
        "2044-02",
        1,
        datetime.now(UTC).replace(tzinfo=None, microsecond=0).isoformat(),
        "test",
        f"fingerprint-{txn_id}",
        2,
        10,
        6,
        0,
        0,
        0,
    ]
    columns += list(extra)
    values += list(extra.values())
    placeholders = ",".join("?" * len(values))
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            f"INSERT INTO transactions ({', '.join(columns)}) VALUES ({placeholders})", values
        )


def test_manifest_names_only_columns_the_orm_has_retired() -> None:
    """Every manifest entry must be a column the ORM no longer declares.

    A live column in the manifest would mean the check is asserting something is
    dead while the schema still offers it -- the manifest would be describing an
    intention, not a fact.
    """
    from ledger.storage.database import Base

    for table, column in doctor_module._KNOWN_DEAD_COLUMNS:
        assert table in Base.metadata.tables, f"manifest names unknown table {table!r}"
        declared = set(Base.metadata.tables[table].columns.keys())
        assert column not in declared, (
            f"{table}.{column} is in the dead-column manifest but the ORM still declares it"
        )


def test_no_findings_on_a_freshly_built_database(tmp_path: Path) -> None:
    db_path = _prepared_db(tmp_path)
    assert "dead_columns" not in _diagnose(db_path, tmp_path)


def test_no_findings_when_every_manifest_column_is_absent(tmp_path: Path) -> None:
    """The post-drop steady state: the manifest is satisfied by removal."""
    db_path = _prepared_db(tmp_path)
    with closing(sqlite3.connect(db_path)) as conn:
        present = {
            (table, str(row[1]))
            for table, _column in doctor_module._KNOWN_DEAD_COLUMNS
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
    for entry in doctor_module._KNOWN_DEAD_COLUMNS:
        assert entry not in present, f"{entry} still exists; this test asserts the post-drop state"

    assert "dead_columns" not in _diagnose(db_path, tmp_path)


def test_an_all_null_lifecycle_column_is_not_reported(tmp_path: Path) -> None:
    """An all null lifecycle column is not reported."""
    db_path = _prepared_db(tmp_path)
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0).isoformat()
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO investment_accounts ("
            "  account_id, institution, source_account_id, account_alias, account_type,"
            "  base_currency, status, created_at, updated_at"
            ") VALUES ('ia-1', 'summit', 'U1', 'RRSP', 'rrsp', 'USD', 'active', ?, ?)",
            (now, now),
        )

    assert "dead_columns" not in _diagnose(db_path, tmp_path)


def test_a_populated_manifest_column_is_reported(tmp_path: Path) -> None:
    """A legacy database still holding values in a retired column.

    Reconstructing the pre-0023 shape after 0023 is recorded means the migration
    will not re-drop it, which is exactly the shape a manifest entry is meant to
    catch: the column is back AND something wrote to it.
    """
    db_path = _prepared_db(tmp_path)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("ALTER TABLE transactions ADD COLUMN days_since_payday INTEGER")
    _insert_transaction(db_path, "txn-dead-1", {"days_since_payday": 3})

    findings = _diagnose(db_path, tmp_path)
    finding = findings.get("dead_columns")
    assert finding is not None, sorted(findings)
    assert finding.severity == "low"
    assert finding.repairable is False
    assert finding.count == 1
    assert any("transactions.days_since_payday" in detail for detail in finding.details), (
        finding.details
    )


def test_a_present_but_empty_manifest_column_is_not_reported(tmp_path: Path) -> None:
    """Presence alone is not staleness -- the column is still dead.

    This is the branch that keeps an un-migrated legacy database quiet. Only a
    non-NULL value proves a producer appeared.
    """
    db_path = _prepared_db(tmp_path)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("ALTER TABLE transactions ADD COLUMN days_since_payday INTEGER")
    _insert_transaction(db_path, "txn-dead-2", {"days_since_payday": None})

    assert "dead_columns" not in _diagnose(db_path, tmp_path)


def test_a_manifest_entry_for_a_missing_table_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manifest that names nothing checkable is stale in the other direction."""
    db_path = _prepared_db(tmp_path)
    monkeypatch.setattr(
        doctor_module,
        "_KNOWN_DEAD_COLUMNS",
        (("table_that_never_existed", "some_column"),),
    )

    findings = _diagnose(db_path, tmp_path)
    finding = findings.get("dead_columns")
    assert finding is not None, sorted(findings)
    assert any("table_that_never_existed" in detail for detail in finding.details), finding.details


def test_a_missing_table_is_reported_once_not_once_per_manifest_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One missing table is ONE problem, however many columns the manifest names.

    All five real entries name ``transactions``, so an un-deduped census emitted
    the identical detail line five times and a count of 5 for a single missing
    table -- and ``commands/db.py`` prints both to the operator.
    """
    db_path = _prepared_db(tmp_path)
    monkeypatch.setattr(
        doctor_module,
        "_KNOWN_DEAD_COLUMNS",
        (
            ("table_that_never_existed", "column_one"),
            ("table_that_never_existed", "column_two"),
            ("table_that_never_existed", "column_three"),
        ),
    )

    findings = _diagnose(db_path, tmp_path)
    finding = findings.get("dead_columns")
    assert finding is not None, sorted(findings)
    assert finding.details == list(dict.fromkeys(finding.details)), finding.details
    assert len(finding.details) == 1, finding.details
    assert finding.count == 1, finding.count


def _build_pre_0023_database(tmp_path: Path) -> Path:
    """A database that predates 0023, with real values in all five dead columns."""
    db_path = _prepared_db(tmp_path)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("ALTER TABLE transactions ADD COLUMN paired_transfer_id VARCHAR(40)")
        conn.execute("ALTER TABLE transactions ADD COLUMN transfer_confidence NUMERIC(3, 2)")
        conn.execute("ALTER TABLE transactions ADD COLUMN counterparty_account VARCHAR(100)")
        conn.execute("ALTER TABLE transactions ADD COLUMN counterparty_institution VARCHAR(50)")
        conn.execute("ALTER TABLE transactions ADD COLUMN days_since_payday INTEGER")
        conn.execute("DELETE FROM schema_migrations WHERE version LIKE '20260810_0023%'")
    _insert_transaction(
        db_path,
        "txn-pre-0023",
        {
            "paired_transfer_id": "txn-other",
            "transfer_confidence": 0.95,
            "counterparty_account": "harbor-savings",
            "counterparty_institution": "harbor",
            "days_since_payday": 3,
        },
    )
    return db_path


def test_a_pre_0023_database_is_reported_rather_than_silently_migrated_away(
    tmp_path: Path,
) -> None:
    """The branch the remedy text describes must be observable through the doctor.

    ``diagnose()`` calls ``ensure_schema()``, which runs the migrations, which
    applies 0023 and DISCARDS these five columns' values. With the check ordered
    after that call, a database holding real values in all five came back with the
    columns GONE and no finding raised -- and ``ledger db doctor`` takes a backup
    only under ``--execute``, so the default read-only report destroyed them with
    nothing to roll back to.
    """
    db_path = _build_pre_0023_database(tmp_path)
    with closing(sqlite3.connect(db_path)) as conn:
        before = {str(row[1]) for row in conn.execute("PRAGMA table_info(transactions)")}
    assert before >= {column for _table, column in doctor_module._KNOWN_DEAD_COLUMNS}

    findings = _diagnose(db_path, tmp_path)

    finding = findings.get("dead_columns")
    assert finding is not None, (
        "the doctor migrated the populated columns away instead of reporting them: "
        f"{sorted(findings)}"
    )
    assert finding.count == len(doctor_module._KNOWN_DEAD_COLUMNS), finding.details
    for _table, column in doctor_module._KNOWN_DEAD_COLUMNS:
        assert any(f"transactions.{column}" in detail for detail in finding.details), (
            column,
            finding.details,
        )
    # And the remedy names the migration whose values are at stake, and admits that
    # this very run applied it -- an operator reading "will discard those values"
    # would otherwise think they still had time to act.
    assert "ledger db migrate" in finding.remedy, finding.remedy
    assert "THIS run already applied pending migrations" in finding.remedy, finding.remedy

    # The columns really are gone afterwards: the finding is the only record.
    with closing(sqlite3.connect(db_path)) as conn:
        after = {str(row[1]) for row in conn.execute("PRAGMA table_info(transactions)")}
    assert after.isdisjoint({column for _table, column in doctor_module._KNOWN_DEAD_COLUMNS})
