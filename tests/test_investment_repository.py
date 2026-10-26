"""Repository behavior for synthetic investment statement aggregates."""

from __future__ import annotations

import contextlib
import hashlib
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import inspect

from ledger.investments.models import (
    InvestmentAccount,
    InvestmentActivity,
    InvestmentActivityType,
    InvestmentAssetBalance,
    InvestmentPerformance,
    InvestmentPosition,
    InvestmentStatement,
)
from ledger.investments.repository import InvestmentConflictError, InvestmentRepository
from ledger.storage.database import (
    InvestmentAccountDB,
    InvestmentActivityDB,
    InvestmentAssetBalanceDB,
    InvestmentPerformanceDB,
    InvestmentPositionDB,
    InvestmentSourceAssociationDB,
    InvestmentSourceFileDB,
    InvestmentStatementDB,
    get_engine,
    init_database,
    session_scope,
)
from ledger.storage.migrations import run_migrations


def statement(
    *, total_nav: str = "100.123456789", fingerprint: str = "a" * 64
) -> InvestmentStatement:
    """Build a complete, deliberately synthetic statement aggregate."""
    account = InvestmentAccount(
        account_id="account-opaque",
        institution="summit",
        source_account_id="private-source-id",
        account_alias="TFSA",
        account_type="registered",
        base_currency="USD",
        status="active",
        status_confirmed_on=date(2026, 8, 1),
    )
    return InvestmentStatement(
        statement_id=f"statement-{total_nav}",
        account=account,
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        generated_at=datetime(2026, 8, 1, 12),
        base_currency="USD",
        total_nav=Decimal(total_nav),
        time_weighted_return=Decimal("0.012345678901"),
        asset_balances=(
            InvestmentAssetBalance(
                asset_class="Stocks",
                prior_total=Decimal("90"),
                current_long=Decimal(total_nav),
                current_short=Decimal("0"),
                current_total=Decimal(total_nav),
                change=Decimal("10.123456789"),
            ),
        ),
        positions=(
            InvestmentPosition(
                asset_category="Stocks",
                currency="USD",
                symbol="AAA",
                discriminator="Summary",
                quantity=Decimal("1.123456789"),
                market_value=Decimal(total_nav),
                source_row=10,
            ),
        ),
        performance=(
            InvestmentPerformance(
                asset_category="Total",
                symbol="",
                cost_adjustment=Decimal("0.000000000001"),
                realized_short_profit=Decimal("2.123456789012"),
                realized_short_loss=Decimal("-1"),
                realized_long_profit=Decimal("0"),
                realized_long_loss=Decimal("0"),
                realized_total=Decimal("1.123456789012"),
                unrealized_short_profit=Decimal(total_nav),
                unrealized_short_loss=Decimal("0"),
                unrealized_long_profit=Decimal("0"),
                unrealized_long_loss=Decimal("0"),
                unrealized_total=Decimal(total_nav),
                total=Decimal(total_nav) + Decimal("1.123456789012"),
                code="",
                source_row=15,
            ),
        ),
        activities=(
            InvestmentActivity(
                activity_id=f"activity-{total_nav}",
                activity_type=InvestmentActivityType.DIVIDEND,
                activity_date=date(2026, 7, 15),
                currency="USD",
                amount=Decimal("1.123456789"),
                description="synthetic dividend",
                source_row=20,
            ),
        ),
        parsed_fingerprint=fingerprint,
    )


def store(
    repo: InvestmentRepository,
    value: InvestmentStatement,
    *,
    suffix: str = "one",
    replace_conflict: bool = False,
) -> str:
    return repo.store_statement(
        value,
        source_csv_path=Path(f"/synthetic/{suffix}.csv"),
        source_csv_sha256=hashlib.sha256(f"{suffix}:csv".encode()).hexdigest(),
        source_pdf_path=Path(f"/synthetic/{suffix}.pdf"),
        source_pdf_sha256=hashlib.sha256(f"{suffix}:pdf".encode()).hexdigest(),
        replace_conflict=replace_conflict,
    )


def test_store_statement_is_idempotent_and_requires_explicit_period_replacement(tmp_path) -> None:
    """A changed source cannot silently replace an existing account-period aggregate."""
    repo = InvestmentRepository(tmp_path / "ledger.db")
    original = statement()
    changed = statement(total_nav="101.123456789", fingerprint="b" * 64)

    assert store(repo, original) == "inserted"
    assert store(repo, original) == "unchanged"
    with pytest.raises(InvestmentConflictError) as raised:
        store(repo, changed, suffix="two")
    assert "private-source-id" not in str(raised.value)
    assert store(repo, changed, suffix="two", replace_conflict=True) == "replaced"

    with session_scope(repo.db_path) as session:
        assert session.query(InvestmentPositionDB).count() == 1
        assert session.query(InvestmentPerformanceDB).count() == 1
        assert session.query(InvestmentActivityDB).count() == 1
        assert session.query(InvestmentSourceFileDB).count() == 4
        position = session.query(InvestmentPositionDB).one()
        assert position.market_value == Decimal("101.123456789")
        performance = session.query(InvestmentPerformanceDB).one()
        assert performance.realized_total == Decimal("1.123456789012")
        assert performance.unrealized_total == Decimal("101.123456789")
        assert {row.status for row in session.query(InvestmentSourceFileDB)} == {"staged"}
        assert {row.archive_status for row in session.query(InvestmentSourceFileDB)} == {"pending"}


def test_replacement_preserves_append_only_source_associations(tmp_path: Path) -> None:
    """Replacement supersedes associations without deleting prior source facts."""
    repo = InvestmentRepository(tmp_path / "ledger.db")
    original = statement()
    changed = statement(total_nav="101.123456789", fingerprint="b" * 64)

    assert store(repo, original) == "inserted"
    assert store(repo, changed, suffix="two", replace_conflict=True) == "replaced"

    with session_scope(repo.db_path) as session:
        sources = session.query(InvestmentSourceFileDB).all()
        associations = session.query(InvestmentSourceAssociationDB).all()
        assert len(sources) == 4
        assert len(associations) == 4
        original_associations = [
            row for row in associations if row.statement_id == original.statement_id
        ]
        assert len(original_associations) == 2
        assert {row.superseded_by_statement_id for row in original_associations} == {
            changed.statement_id
        }
        assert all(row.superseded_at is not None for row in original_associations)
        replacement_associations = [
            row for row in associations if row.statement_id == changed.statement_id
        ]
        assert len(replacement_associations) == 2
        assert all(row.superseded_at is None for row in replacement_associations)
        assert {row.source_path for row in associations} == {
            "/synthetic/one.csv",
            "/synthetic/one.pdf",
            "/synthetic/two.csv",
            "/synthetic/two.pdf",
        }

    with contextlib.closing(sqlite3.connect(repo.db_path)) as connection, connection:
        connection.execute("PRAGMA foreign_keys = ON")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_repeated_replacement_allows_reused_path_without_reusing_old_hash(
    tmp_path: Path,
) -> None:
    """Detached historical identities do not block a later version at the same path."""
    repo = InvestmentRepository(tmp_path / "ledger.db")
    first = statement()
    second = statement(total_nav="101.123456789", fingerprint="b" * 64)
    third = statement(total_nav="102.123456789", fingerprint="c" * 64)

    def persist(value: InvestmentStatement, token: str, *, replace_conflict: bool = False):
        return repo.store_statement(
            value,
            source_csv_path=Path("/synthetic/reused.csv"),
            source_csv_sha256=hashlib.sha256(f"{token}:csv".encode()).hexdigest(),
            source_pdf_path=Path("/synthetic/reused.pdf"),
            source_pdf_sha256=hashlib.sha256(f"{token}:pdf".encode()).hexdigest(),
            replace_conflict=replace_conflict,
        )

    assert persist(first, "first") == "inserted"
    assert persist(second, "second", replace_conflict=True) == "replaced"
    assert persist(third, "third", replace_conflict=True) == "replaced"

    with session_scope(repo.db_path) as session:
        associations = session.query(InvestmentSourceAssociationDB).all()
        assert len(associations) == 6
        assert sum(row.superseded_at is None for row in associations) == 2
        assert {row.statement_id for row in associations} == {
            first.statement_id,
            second.statement_id,
            third.statement_id,
        }


def test_migration_0018_backfills_source_associations_with_fresh_schema_parity(
    tmp_path: Path,
) -> None:
    """Upgrade backfills durable source links and matches the fresh FK contract."""
    db_path = tmp_path / "legacy-associations.db"
    fresh_path = tmp_path / "fresh-associations.db"
    run_migrations(db_path)
    repo = InvestmentRepository(db_path)
    assert store(repo, statement()) == "inserted"
    with contextlib.closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute("DELETE FROM investment_source_associations")
        connection.execute(
            "DELETE FROM schema_migrations "
            "WHERE version = '20260801_0018_investment_source_associations'"
        )

    result = run_migrations(db_path)
    init_database(fresh_path)

    assert result.details["applied_versions"] == ["20260801_0018_investment_source_associations"]
    with contextlib.closing(sqlite3.connect(db_path)) as connection, connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM investment_source_associations"
        ).fetchone() == (2,)
        foreign_keys = {
            (str(row[3]), str(row[2]), str(row[4]))
            for row in connection.execute("PRAGMA foreign_key_list(investment_source_associations)")
        }
        assert foreign_keys == {
            ("file_hash", "investment_source_files", "file_hash"),
            ("import_run_id", "investment_import_runs", "run_id"),
        }
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    def table_contract(path: Path) -> tuple:
        inspector = inspect(get_engine(path))
        return (
            tuple(
                (column["name"], str(column["type"]), column["nullable"])
                for column in inspector.get_columns("investment_source_associations")
            ),
            tuple(
                sorted(
                    (foreign_key["constrained_columns"][0], foreign_key["referred_table"])
                    for foreign_key in inspector.get_foreign_keys("investment_source_associations")
                )
            ),
        )

    assert table_contract(db_path) == table_contract(fresh_path)


def test_repository_rejects_inconsistent_same_account_end_date_snapshots_and_replacement(
    tmp_path: Path,
) -> None:
    """Monthly/YTD statements cannot persist contradictory snapshots for one report date."""
    repo = InvestmentRepository(tmp_path / "ledger.db")
    monthly = statement()
    identical_ytd = monthly.model_copy(
        update={
            "statement_id": "statement-ytd-identical",
            "period_start": date(2026, 1, 1),
            "parsed_fingerprint": "b" * 64,
            "asset_balances": (
                monthly.asset_balances[0].model_copy(
                    update={
                        "prior_total": Decimal("80"),
                        "change": Decimal("20.123456789"),
                    }
                ),
            ),
        }
    )
    inconsistent_ytd = statement(total_nav="101.123456789", fingerprint="c" * 64).model_copy(
        update={
            "statement_id": "statement-ytd-inconsistent",
            "period_start": date(2026, 2, 1),
        }
    )

    assert store(repo, monthly) == "inserted"
    assert store(repo, identical_ytd, suffix="two") == "inserted"
    with pytest.raises(InvestmentConflictError, match="end-date snapshot"):
        store(repo, inconsistent_ytd, suffix="three")

    changed_monthly = statement(total_nav="102.123456789", fingerprint="d" * 64)
    with pytest.raises(InvestmentConflictError, match="end-date snapshot"):
        store(repo, changed_monthly, suffix="four", replace_conflict=True)


def test_same_end_snapshot_comparison_handles_nullable_duplicate_position_keys(
    tmp_path: Path,
) -> None:
    """Valid duplicate position keys cannot fail comparison on None versus Decimal."""
    repo = InvestmentRepository(tmp_path / "ledger.db")
    original = statement()
    positions = (
        original.positions[0].model_copy(update={"cost_price": Decimal("1.25"), "source_row": 10}),
        original.positions[0].model_copy(update={"cost_price": None, "source_row": 11}),
    )
    monthly = original.model_copy(update={"positions": positions})
    ytd = monthly.model_copy(
        update={
            "statement_id": "statement-ytd-nullable-positions",
            "period_start": date(2026, 1, 1),
            "parsed_fingerprint": "b" * 64,
        }
    )

    assert store(repo, monthly) == "inserted"
    assert store(repo, ytd, suffix="two") == "inserted"


def test_repository_round_trips_exact_values_across_statement_children(tmp_path) -> None:
    """Repository writes retain high nine-place and source 12-place values."""
    high = Decimal("9999999999999999.123456789")
    scale_12 = Decimal("0.123456789012")
    value = statement(total_nav=str(high)).model_copy(
        update={
            "time_weighted_return": scale_12,
            "asset_balances": (
                InvestmentAssetBalance(
                    asset_class="Stocks",
                    prior_total=high,
                    current_long=scale_12,
                    current_short=-scale_12,
                    current_total=high,
                    change=scale_12,
                ),
            ),
            "positions": (
                InvestmentPosition(
                    asset_category="Stocks",
                    currency="USD",
                    symbol="AAA",
                    discriminator="Summary",
                    quantity=high,
                    cost_price=scale_12,
                    market_value=high,
                    unrealized_pl=scale_12,
                    source_row=10,
                ),
            ),
            "performance": (
                InvestmentPerformance(
                    asset_category="Total",
                    symbol="",
                    cost_adjustment=scale_12,
                    realized_total=high,
                    unrealized_total=scale_12,
                    total=high,
                    source_row=15,
                ),
            ),
            "activities": (
                InvestmentActivity(
                    activity_id="exact-activity",
                    activity_type=InvestmentActivityType.DIVIDEND,
                    activity_date=date(2026, 7, 15),
                    currency="USD",
                    quantity=scale_12,
                    amount=high,
                    commission_fee=-scale_12,
                    source_row=20,
                ),
            ),
            "parsed_fingerprint": "9" * 64,
        }
    )
    repo = InvestmentRepository(tmp_path / "ledger.db")

    assert store(repo, value) == "inserted"

    with session_scope(repo.db_path) as session:
        persisted = session.get(InvestmentStatementDB, value.statement_id)
        assert persisted is not None
        assert persisted.total_nav == high
        assert persisted.time_weighted_return == scale_12
        assert (
            session.get(InvestmentAssetBalanceDB, (value.statement_id, "Stocks")).prior_total
            == high
        )
        assert session.get(InvestmentPositionDB, (value.statement_id, 10)).quantity == high
        assert session.get(InvestmentPerformanceDB, (value.statement_id, 15)).total == high
        assert (
            session.get(InvestmentActivityDB, (value.statement_id, "exact-activity")).amount == high
        )


def test_set_account_lifecycle_updates_alias_and_preserves_unknown_close_date(tmp_path) -> None:
    """Lifecycle confirmation changes only the selected account's explicit facts."""
    repo = InvestmentRepository(tmp_path / "ledger.db")
    store(repo, statement())

    repo.set_account_lifecycle(
        "TFSA",
        "closed",
        date(2026, 8, 1),
        "Lumen",
    )

    with session_scope(repo.db_path) as session:
        row = session.query(InvestmentAccountDB).one()
        assert row.status == "closed"
        assert row.status_confirmed_on == date(2026, 8, 1)
        assert row.transfer_destination == "Lumen"
        assert row.closed_on is None


@pytest.mark.parametrize("destination", [None, "", "   "])
def test_closed_lifecycle_requires_an_explicit_transfer_destination(
    tmp_path: Path,
    destination: str | None,
) -> None:
    """Closing an account cannot erase or omit its source-backed destination."""
    repo = InvestmentRepository(tmp_path / "ledger.db")
    store(repo, statement())

    with pytest.raises(ValueError, match="transfer destination"):
        repo.set_account_lifecycle(
            "TFSA",
            "closed",
            date(2026, 8, 1),
            destination,
        )

    with session_scope(repo.db_path) as session:
        account = session.query(InvestmentAccountDB).one()
        assert account.status == "active"
        assert account.transfer_destination is None


def test_source_provenance_conflict_is_redacted_before_statement_mutation(tmp_path) -> None:
    """A source identity cannot be silently attached to an unrelated statement."""
    repo = InvestmentRepository(tmp_path / "ledger.db")
    original = statement()
    other_account = original.account.model_copy(
        update={"account_id": "account-other", "account_alias": "RRSP"}
    )
    conflicting = original.model_copy(
        update={
            "statement_id": "statement-other",
            "account": other_account,
            "parsed_fingerprint": "z" * 64,
        }
    )
    assert store(repo, original) == "inserted"

    with pytest.raises(InvestmentConflictError) as raised:
        store(repo, conflicting)

    assert "/synthetic/one.csv" not in str(raised.value)


def test_repository_translates_a_concurrent_period_insert_without_leaking_details(
    tmp_path, monkeypatch
) -> None:
    """A write that races after the pre-read returns the public conflict error."""
    repo = InvestmentRepository(tmp_path / "ledger.db")
    value = statement()
    original_validate = InvestmentRepository._validate_source_provenance

    def insert_competing_statement(session, *args) -> None:
        with session_scope(repo.db_path) as competing_session:
            now = datetime(2026, 8, 1)
            competing_session.add(
                InvestmentAccountDB(
                    account_id=value.account.account_id,
                    institution=value.account.institution,
                    source_account_id=value.account.source_account_id,
                    account_alias=value.account.account_alias,
                    account_type=value.account.account_type,
                    base_currency=value.account.base_currency,
                    status=value.account.status,
                    transfer_destination=value.account.transfer_destination,
                    status_confirmed_on=value.account.status_confirmed_on,
                    closed_on=value.account.closed_on,
                    first_statement_date=value.period_start,
                    last_statement_date=value.period_end,
                    created_at=now,
                    updated_at=now,
                )
            )
            competing_session.add(
                InvestmentStatementDB(
                    statement_id="competing-statement",
                    account_id=value.account.account_id,
                    period_start=value.period_start,
                    period_end=value.period_end,
                    generated_at=value.generated_at,
                    base_currency=value.base_currency,
                    total_nav=value.total_nav,
                    time_weighted_return=value.time_weighted_return,
                    parsed_fingerprint="f" * 64,
                    imported_at=now,
                )
            )
        original_validate(session, *args)

    monkeypatch.setattr(
        InvestmentRepository,
        "_validate_source_provenance",
        staticmethod(insert_competing_statement),
    )

    with pytest.raises(InvestmentConflictError) as raised:
        store(repo, value)

    assert "account-opaque" not in str(raised.value)
    assert "UNIQUE constraint" not in str(raised.value)


def test_conflict_replacement_rolls_back_statement_children_and_source_ownership(
    tmp_path, monkeypatch
) -> None:
    """A failed replacement retains the complete prior statement-owned aggregate."""
    repo = InvestmentRepository(tmp_path / "ledger.db")
    original = statement()
    changed = statement(total_nav="101.123456789", fingerprint="b" * 64)
    assert store(repo, original) == "inserted"
    real_add_children = InvestmentRepository._add_children

    def fail_after_delete(session, replacement) -> None:
        if replacement.parsed_fingerprint == changed.parsed_fingerprint:
            raise RuntimeError("synthetic child write failure")
        real_add_children(session, replacement)

    monkeypatch.setattr(InvestmentRepository, "_add_children", staticmethod(fail_after_delete))

    with pytest.raises(RuntimeError, match="child write failure"):
        store(repo, changed, suffix="two", replace_conflict=True)

    with session_scope(repo.db_path) as session:
        persisted = session.query(InvestmentStatementDB).one()
        assert persisted.total_nav == original.total_nav
        assert persisted.parsed_fingerprint == original.parsed_fingerprint
        assert session.query(InvestmentPositionDB).one().market_value == original.total_nav
        assert session.query(InvestmentPerformanceDB).one().unrealized_total == original.total_nav
        sources = session.query(InvestmentSourceFileDB).all()
        assert len(sources) == 2
        assert {row.source_path for row in sources} == {
            "/synthetic/one.csv",
            "/synthetic/one.pdf",
        }
