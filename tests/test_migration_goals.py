"""The account-linked-goals migration must not destroy user-entered savings goals.

Savings goals are user-entered and not reconstructable from statements, so the
migration must preserve existing rows (legacy rows keep account_id NULL) instead
of wiping the table.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

from ledger.storage.database import SavingsGoalDB, init_database, session_scope
from ledger.storage.migrations import run_migrations


def test_account_linked_goals_migration_preserves_existing_goals(tmp_path):
    db_path = tmp_path / "goals.db"
    init_database(db_path)

    now = datetime.now(UTC).replace(tzinfo=None)
    with session_scope(db_path) as session:
        session.add(
            SavingsGoalDB(
                name="Emergency Fund",
                account_id="acct-savings",
                target_amount=Decimal("10000.00"),
                target_date=date(2026, 12, 31),
                status="active",
                notes="",
                created_at=now,
                updated_at=now,
            )
        )

    run_migrations(db_path)

    with session_scope(db_path) as session:
        goals = session.query(SavingsGoalDB).all()
        assert len(goals) == 1
        assert goals[0].name == "Emergency Fund"
