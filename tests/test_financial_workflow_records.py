from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date
from decimal import Decimal

import pytest

from ledger.imports.pdf.readiness import MERGE_FORWARD_TABLES, merge_forward
from ledger.storage.database import PlannedScheduleDB, SpendingPreferenceDB, session_scope
from ledger.storage.migrations import ensure_schema
from ledger.storage.snapshots import restore_database, snapshot_database
from ledger.utils.dates import naive_utc_now


def test_financial_workflow_tables_are_migrated_and_merge_forward(tmp_path):
    live = tmp_path / "live.db"
    candidate = tmp_path / "candidate.db"
    ensure_schema(live)
    ensure_schema(candidate)
    now = naive_utc_now()

    with session_scope(live) as session:
        session.add(
            SpendingPreferenceDB(
                scope="merchant",
                name="LOCAL GROCER",
                disposition="necessary",
                note="Household food",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            PlannedScheduleDB(
                schedule_id="schedule-1",
                name="Rent",
                event_type="bill",
                amount=Decimal("1234.56"),
                currency="CAD",
                account_id="account-a",
                counterparty_account_id=None,
                next_date=date(2026, 10, 1),
                frequency="monthly",
                provenance="confirmed",
                pattern_id=None,
                is_active=True,
                note="",
                created_at=now,
                updated_at=now,
            )
        )

    result = merge_forward(live, candidate)

    assert {"spending_preferences", "planned_schedules"} <= set(MERGE_FORWARD_TABLES)
    assert result["tables"]["spending_preferences"]["content"][0] == 1
    assert result["tables"]["planned_schedules"]["content"][0] == 1
    with closing(sqlite3.connect(candidate)) as connection:
        preference = connection.execute(
            "select scope, name, disposition from spending_preferences"
        ).fetchone()
        schedule = connection.execute(
            "select name, amount, event_type, provenance from planned_schedules"
        ).fetchone()
    assert preference == ("merchant", "LOCAL GROCER", "necessary")
    assert schedule == ("Rent", 1234.56, "bill", "confirmed")


def test_financial_workflow_records_reject_unknown_vocabularies(tmp_path):
    db_path = tmp_path / "ledger.db"
    ensure_schema(db_path)
    now = naive_utc_now()

    with closing(sqlite3.connect(db_path)) as connection, connection:
        for statement, params in (
            (
                "insert into spending_preferences "
                "(scope, name, disposition, note, created_at, updated_at) "
                "values (?, ?, ?, '', ?, ?)",
                ("merchant", "X", "hidden", now.isoformat(), now.isoformat()),
            ),
            (
                "insert into planned_schedules "
                "(schedule_id, name, event_type, amount, currency, account_id, "
                "next_date, frequency, provenance, is_active, note, created_at, updated_at) "
                "values ('bad', 'Bad', 'expense', 1, 'CAD', 'a', '2026-09-01', "
                "'monthly', 'confirmed', 1, '', ?, ?)",
                (now.isoformat(), now.isoformat()),
            ),
        ):
            try:
                connection.execute(statement, params)
            except sqlite3.IntegrityError:
                continue
            raise AssertionError("invalid durable user vocabulary was accepted")


def test_financial_workflow_tables_enforce_user_record_boundaries(tmp_path):
    db_path = tmp_path / "ledger.db"
    ensure_schema(db_path)
    now = naive_utc_now().isoformat()

    invalid_statements = (
        (
            "insert into spending_preferences "
            "(scope, name, disposition, note, created_at, updated_at) "
            "values ('merchant', ' ', 'necessary', '', ?, ?)",
            (now, now),
        ),
        (
            "insert into planned_schedules "
            "(schedule_id, name, event_type, amount, currency, account_id, next_date, "
            "frequency, provenance, is_active, note, created_at, updated_at) "
            "values ('zero', 'Zero', 'bill', 0, 'CAD', 'a', '2026-09-01', "
            "'once', 'confirmed', 1, '', ?, ?)",
            (now, now),
        ),
        (
            "insert into planned_schedules "
            "(schedule_id, name, event_type, amount, currency, account_id, next_date, "
            "frequency, provenance, is_active, note, created_at, updated_at) "
            "values ('usd', 'USD', 'bill', 1, 'USD', 'a', '2026-09-01', "
            "'once', 'confirmed', 1, '', ?, ?)",
            (now, now),
        ),
        (
            "insert into planned_schedules "
            "(schedule_id, name, event_type, amount, currency, account_id, "
            "counterparty_account_id, next_date, frequency, provenance, is_active, note, "
            "created_at, updated_at) values ('same', 'Same', 'transfer', 1, 'CAD', "
            "'a', 'a', '2026-09-01', 'once', 'confirmed', 1, '', ?, ?)",
            (now, now),
        ),
    )
    with closing(sqlite3.connect(db_path)) as connection, connection:
        for statement, params in invalid_statements:
            try:
                connection.execute(statement, params)
            except sqlite3.IntegrityError:
                continue
            raise AssertionError("invalid durable user record was accepted")


def test_planned_schedules_allow_one_confirmed_link_per_detected_pattern(tmp_path):
    db_path = tmp_path / "ledger.db"
    ensure_schema(db_path)
    now = naive_utc_now().isoformat()
    statement = (
        "insert into planned_schedules "
        "(schedule_id, name, event_type, amount, currency, account_id, next_date, "
        "frequency, provenance, pattern_id, is_active, note, created_at, updated_at) "
        "values (?, 'Utility', 'bill', 100, 'CAD', 'account-a', '2026-10-01', "
        "'monthly', ?, 'detected-utility', 1, '', ?, ?)"
    )

    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute(statement, ("confirmed", "confirmed", now, now))
        connection.execute(statement, ("hypothetical", "hypothetical", now, now))
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(statement, ("duplicate", "confirmed", now, now))

        rows = connection.execute(
            "select schedule_id, provenance from planned_schedules "
            "where pattern_id = 'detected-utility' order by schedule_id"
        ).fetchall()

    assert rows == [("confirmed", "confirmed"), ("hypothetical", "hypothetical")]


def test_financial_workflow_records_survive_snapshot_and_restore(tmp_path):
    live = tmp_path / "live.db"
    snapshot = tmp_path / "backup.db"
    ensure_schema(live)
    now = naive_utc_now()
    with session_scope(live) as session:
        session.add(
            SpendingPreferenceDB(
                scope="category",
                name="Health",
                disposition="worthwhile",
                note="",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            PlannedScheduleDB(
                schedule_id="monthly-income",
                name="Income",
                event_type="income",
                amount=Decimal("1000.00"),
                currency="CAD",
                account_id="account-a",
                next_date=date(2026, 10, 1),
                frequency="monthly",
                provenance="confirmed",
                is_active=True,
                note="",
                created_at=now,
                updated_at=now,
            )
        )

    snapshot_database(live, snapshot)
    with closing(sqlite3.connect(live)) as connection, connection:
        connection.execute("delete from spending_preferences")
        connection.execute("delete from planned_schedules")
    restore_database(snapshot, live)

    with closing(sqlite3.connect(live)) as connection:
        assert connection.execute("select count(*) from spending_preferences").fetchone() == (1,)
        assert connection.execute("select count(*) from planned_schedules").fetchone() == (1,)
