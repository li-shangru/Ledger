"""P7 budget read safety, import safety, suggestions, and MCP surface."""

from __future__ import annotations

import csv
import json
import os
import stat
from contextlib import closing, contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.exc import SQLAlchemyError
from typer.testing import CliRunner

from ledger.cli import app
from ledger.core.results import RunResult
from ledger.mcp.server import LedgerMCPService, call_tool
from ledger.planning import BudgetService, PlanningReadinessService
from ledger.storage.database import (
    BudgetDB,
    SavingsGoalDB,
    TransactionDB,
    init_database,
    session_scope,
)
from ledger.storage.migrations import run_migrations
from ledger.storage.snapshots import DatabaseTargetLockError, database_target_lock

runner = CliRunner()


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _seed_transaction(
    db_path: Path,
    *,
    txn_id: str,
    posted: date,
    amount: str,
    category: str,
    currency: str = "CAD",
    include_in_cashflow: bool = True,
    exclude_from_spending: bool = False,
) -> None:
    with session_scope(db_path) as session:
        session.add(
            TransactionDB(
                txn_id=txn_id,
                posting_date=posted,
                transaction_date=posted,
                institution="synthetic-bank",
                account="synthetic-account",
                account_type="checking",
                flow="expense",
                cashflow_type="expense",
                include_in_cashflow=include_in_cashflow,
                exclude_from_spending=exclude_from_spending,
                currency=currency,
                amount_raw=abs(Decimal(amount)),
                amount_signed=Decimal(amount),
                description="SYNTHETIC PURCHASE",
                merchant="SYNTHETIC MERCHANT",
                category=category,
                source_file="synthetic.csv",
                source_path="synthetic.csv",
                source_period=posted.strftime("%Y-%m"),
                source_row=1,
                imported_at=_now(),
                parser_version="test",
                txn_fingerprint=f"{txn_id}-fingerprint",
                day_of_week=posted.weekday(),
                day_of_month=posted.day,
                week_of_year=posted.isocalendar()[1],
                is_weekend=posted.weekday() >= 5,
                is_month_start=posted.day <= 3,
                is_month_end=posted.day >= 28,
            )
        )


def _stored_budgets(db_path: Path) -> list[tuple[str, str, str, str]]:
    with closing(__import__("sqlite3").connect(db_path)) as connection:
        return [
            tuple(str(value) for value in row)
            for row in connection.execute(
                "select budget_month, category, amount, notes from budgets "
                "order by budget_month, category"
            )
        ]


def test_every_budget_read_method_is_observational_on_a_read_only_database(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _seed_transaction(
        db_path,
        txn_id="read-spend",
        posted=date(2026, 5, 10),
        amount="-25.00",
        category="Groceries",
    )
    with session_scope(db_path) as session:
        session.add(
            BudgetDB(
                budget_month="*",
                category="Groceries",
                amount=Decimal("100.00"),
                notes="",
                created_at=_now(),
                updated_at=_now(),
            )
        )
        session.add(
            SavingsGoalDB(
                name="Synthetic reserve",
                account_id=None,
                target_amount=Decimal("500.00"),
                target_date=None,
                status="active",
                notes="",
                created_at=_now(),
                updated_at=_now(),
            )
        )

    os.chmod(db_path, 0o444)
    before_mode = stat.S_IMODE(db_path.stat().st_mode)
    before_bytes = db_path.read_bytes()

    service = BudgetService(db_path, prepare_schema=False)
    assert service.list_budgets()[0]["amount"] == Decimal("100.00")
    assert service.list_goals()[0]["target_amount"] == Decimal("500.00")
    assert service.list_goal_accounts() == []
    assert service.budget_variance(date(2026, 5, 1), date(2026, 5, 31))[0]["actual"] == Decimal(
        "25.00"
    )
    assert service.validate().ok

    assert stat.S_IMODE(db_path.stat().st_mode) == before_mode == 0o444
    assert db_path.read_bytes() == before_bytes
    assert not any(Path(f"{db_path}{suffix}").exists() for suffix in ("-journal", "-wal", "-shm"))


def test_budget_import_requires_the_exact_header_and_preserves_existing_rows(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    BudgetService(db_path).replace_budgets(
        [{"month": "*", "category": "Groceries", "amount": "250.00", "notes": "keep"}]
    )
    before = _stored_budgets(db_path)
    source = tmp_path / "wrong.csv"
    source.write_text("Month,Category,Amount,Notes\n*,Dining,75.00,new\n", encoding="utf-8")

    result = runner.invoke(
        app,
        ["budget", "import", "--db", str(db_path), "--source", str(source), "--execute"],
    )

    assert result.exit_code == 1
    assert "header must be exactly" in result.output
    assert _stored_budgets(db_path) == before


def test_budget_import_is_dry_run_until_execute_and_takes_a_prewrite_backup(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    BudgetService(db_path).replace_budgets(
        [{"month": "*", "category": "Groceries", "amount": "250.00", "notes": "old"}]
    )
    source = tmp_path / "suggested.csv"
    source.write_text(
        "month,category,amount,notes\n*,Dining,75.00,synthetic\n",
        encoding="utf-8",
    )
    before = _stored_budgets(db_path)

    dry_run = runner.invoke(
        app,
        ["budget", "import", "--db", str(db_path), "--source", str(source)],
    )
    assert dry_run.exit_code == 0, dry_run.output
    assert _stored_budgets(db_path) == before
    assert not list((tmp_path / "db-backups").glob("*.db"))

    executed = runner.invoke(
        app,
        ["budget", "import", "--db", str(db_path), "--source", str(source), "--execute"],
    )
    assert executed.exit_code == 0, executed.output
    assert _stored_budgets(db_path) == [("*", "Dining", "75", "synthetic")]
    backups = list((tmp_path / "db-backups").glob("*.db"))
    assert len(backups) == 1
    assert _stored_budgets(backups[0]) == before


def test_budget_import_refuses_zero_rows_without_initializing_or_replacing(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    BudgetService(db_path).replace_budgets(
        [{"month": "*", "category": "Groceries", "amount": "250.00", "notes": "keep"}]
    )
    before = _stored_budgets(db_path)
    source = tmp_path / "empty.csv"
    source.write_text("month,category,amount,notes\n", encoding="utf-8")

    result = runner.invoke(
        app,
        ["budget", "import", "--db", str(db_path), "--source", str(source), "--execute"],
    )

    assert result.exit_code == 1
    assert _stored_budgets(db_path) == before
    assert not list((tmp_path / "db-backups").glob("*.db"))


@pytest.mark.parametrize(
    "contents",
    [
        "month,category,amount,notes\n",
        "Month,Category,Amount,Notes\n*,Dining,75.00,new\n",
    ],
)
def test_budget_import_prechecks_do_not_initialize_a_missing_target(
    tmp_path: Path,
    contents: str,
) -> None:
    db_path = tmp_path / "missing.db"
    source = tmp_path / "invalid.csv"
    source.write_text(contents, encoding="utf-8")

    result = runner.invoke(
        app,
        ["budget", "import", "--db", str(db_path), "--source", str(source), "--execute"],
    )

    assert result.exit_code == 1
    assert not db_path.exists()
    assert not (tmp_path / "db-backups").exists()


def test_planning_readiness_threads_the_read_only_no_prepare_mode(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    os.chmod(db_path, 0o444)
    before = db_path.read_bytes()

    summary = PlanningReadinessService(db_path, prepare_schema=False).summary(
        date(2026, 1, 1), date(2026, 1, 31)
    )

    assert summary["budgets"]["active"] == 0
    assert db_path.read_bytes() == before
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o444


def test_budget_suggest_uses_monthly_medians_on_the_aggregate_spending_basis(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    for txn_id, posted, amount in (
        ("jan", date(2026, 1, 5), "-100.00"),
        ("feb", date(2026, 2, 5), "-300.00"),
        ("mar", date(2026, 3, 5), "-900.00"),
    ):
        _seed_transaction(
            db_path,
            txn_id=txn_id,
            posted=posted,
            amount=amount,
            category="Dining",
        )
    _seed_transaction(
        db_path,
        txn_id="excluded",
        posted=date(2026, 2, 6),
        amount="-900.00",
        category="Dining",
        exclude_from_spending=True,
    )
    _seed_transaction(
        db_path,
        txn_id="foreign",
        posted=date(2026, 2, 7),
        amount="-800.00",
        category="Dining",
        currency="USD",
    )
    dest = tmp_path / "suggestions.csv"

    result = runner.invoke(
        app,
        [
            "budget",
            "suggest",
            "--db",
            str(db_path),
            "--start-date",
            "2026-01-01",
            "--end-date",
            "2026-03-31",
            "--dest",
            str(dest),
        ],
    )

    assert result.exit_code == 0, result.output
    with dest.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == ["month", "category", "amount", "notes"]
        rows = list(reader)
    assert [(row["month"], row["category"], row["amount"]) for row in rows] == [
        ("*", "Dining", "300.00")
    ]
    assert "gross" in rows[0]["notes"].casefold()


def _structured(result: dict[str, object]) -> dict[str, object]:
    return result["structuredContent"]  # type: ignore[return-value]


def test_ledger_budgets_reports_no_definition_without_fabricated_money(
    tmp_path: Path,
) -> None:
    from mcp_synthetic import build_synthetic_mcp_database

    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    with session_scope(db_path) as session:
        session.query(BudgetDB).delete()
    service = LedgerMCPService(db_path)
    try:
        envelope = _structured(
            call_tool(
                service,
                "ledger.budgets",
                {"start_date": "2025-01-01", "end_date": "2025-12-31"},
            )
        )
    finally:
        service.close()

    assert envelope["data"]["status"] == "no_budgets_defined"  # type: ignore[index]
    assert envelope["data"]["definition_count"] == 0  # type: ignore[index]
    assert envelope["data"]["entry_count"] == 0  # type: ignore[index]
    assert envelope["data"]["entries"] == []  # type: ignore[index]
    assert envelope["data"]["totals"] is None  # type: ignore[index]
    assert datetime.fromisoformat(envelope["data"]["computed_at"])  # type: ignore[index]
    assert "0.00" not in json.dumps(envelope["data"])  # type: ignore[index]


def test_budget_service_serializes_replace_across_the_complete_publication_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    service = BudgetService(db_path, prepare_schema=False)
    original = service._replace_budgets_locked
    lock_observed = False

    def guarded(rows: list[dict[str, object]]):
        nonlocal lock_observed
        with pytest.raises(DatabaseTargetLockError), database_target_lock(db_path):
            pass
        lock_observed = True
        return original(rows)

    monkeypatch.setattr(service, "_replace_budgets_locked", guarded)
    result = service.replace_budgets(
        [{"month": "*", "category": "Dining", "amount": "75.00", "notes": ""}]
    )

    assert result.ok
    assert lock_observed


def test_budget_import_holds_the_target_lock_across_backup_and_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.planning import service as planning_service

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    run_migrations(db_path)
    source = tmp_path / "budgets.csv"
    source.write_text("month,category,amount,notes\n*,Dining,75.00,new\n", encoding="utf-8")
    original_lock = planning_service.database_target_lock
    original_backup = planning_service.backup_database
    observed: list[str] = []

    @contextmanager
    def observed_target_lock(*args: object, **kwargs: object):
        observed.append("lock-enter")
        with original_lock(*args, **kwargs) as lock_value:
            try:
                yield lock_value
            finally:
                observed.append("lock-exit")

    def guarded_backup(path: Path, **kwargs: object):
        observed.append("backup")
        return original_backup(path, **kwargs)

    monkeypatch.setattr(planning_service, "database_target_lock", observed_target_lock)
    monkeypatch.setattr(planning_service, "backup_database", guarded_backup)
    original_replace = BudgetService._replace_budgets_pinned

    def guarded_replace(self: BudgetService, rows: list[dict[str, object]], **kwargs: object):
        observed.append("replace")
        return original_replace(self, rows, **kwargs)

    monkeypatch.setattr(BudgetService, "_replace_budgets_pinned", guarded_replace)

    def guarded_postcommit_validation() -> None:
        observed.append("postcommit")

    monkeypatch.setattr(planning_service, "invalidate_db_caches", guarded_postcommit_validation)

    result = BudgetService(db_path, prepare_schema=False).import_csv(source, execute=True)

    assert result.ok
    assert observed == ["lock-enter", "backup", "replace", "postcommit", "lock-exit"]
    assert observed.count("lock-enter") == observed.count("lock-exit") == 1


def test_every_budget_goal_service_writer_uses_the_shared_target_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    service = BudgetService(db_path, prepare_schema=False)
    observed: list[str] = []

    def assert_locked(label: str) -> None:
        with pytest.raises(DatabaseTargetLockError), database_target_lock(db_path):
            pass
        observed.append(label)

    original_budget = service._replace_budgets_locked
    original_goals = service._replace_goals_locked
    original_export = service._export_csv_locked

    def budgets(rows: list[dict[str, object]]) -> RunResult:
        assert_locked("budgets")
        return original_budget(rows)

    def goals(rows: list[dict[str, object]]) -> RunResult:
        assert_locked("goals")
        return original_goals(rows)

    def export(dest: Path) -> RunResult:
        assert_locked("export")
        return original_export(dest)

    monkeypatch.setattr(service, "_replace_budgets_locked", budgets)
    assert service.replace_budgets([]).ok
    monkeypatch.setattr(service, "_replace_goals_locked", goals)
    assert service.replace_goals([]).ok
    monkeypatch.setattr(service, "_export_csv_locked", export)
    assert service.export_csv(tmp_path / "export.csv").ok

    assert observed == ["budgets", "goals", "export"]


def test_ledger_budgets_reports_gross_actuals_from_the_exact_pinned_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from mcp_synthetic import build_synthetic_mcp_database

    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    BudgetService(db_path).replace_budgets(
        [{"month": "*", "category": "Groceries", "amount": "100.00", "notes": ""}]
    )
    service = LedgerMCPService(db_path)

    def lexical_reopen_forbidden(*_args: object, **_kwargs: object) -> object:
        raise SQLAlchemyError("lexical database reopen")

    # A BudgetService constructed without database.session_scope reaches the global
    # engine factory. The pinned reader never does.
    monkeypatch.setattr("ledger.storage.database.get_engine", lexical_reopen_forbidden)
    try:
        envelope = _structured(
            call_tool(
                service,
                "ledger.budgets",
                {"start_date": "2025-01-01", "end_date": "2025-12-31"},
            )
        )
    finally:
        service.close()

    assert envelope["data"]["status"] == "reported"  # type: ignore[index]
    categories = [row["category"] for row in envelope["data"]["entries"]]  # type: ignore[index]
    assert categories == sorted(categories)
    groceries = next(
        entry
        for entry in envelope["data"]["entries"]  # type: ignore[index]
        if entry["category"] == "Groceries"
    )
    assert groceries == {
        "actual": "30.30",
        "budget": "1200.00",
        "category": "Groceries",
        "status": "ok",
        "variance": "1169.70",
        "pct_used": "2.52",
    }
    assert "gross" in envelope["data"]["actual_basis"].casefold()  # type: ignore[index]


def test_ledger_budgets_schema_is_closed_bounded_and_explicitly_unpaginated() -> None:
    from ledger.mcp.contracts import MAX_BUDGET_ENTRIES, tool_schemas

    schema = next(tool for tool in tool_schemas() if tool["name"] == "ledger.budgets")
    success = next(
        branch
        for branch in schema["outputSchema"]["oneOf"]  # type: ignore[index]
        if "data" in branch.get("properties", {})
    )
    variants = success["properties"]["data"]["oneOf"]

    assert "page" not in success["properties"]
    assert variants
    assert all(
        variant["properties"]["entries"]["maxItems"] <= MAX_BUDGET_ENTRIES for variant in variants
    )
    assert all("computed_at" in variant["required"] for variant in variants)
    assert set(schema["inputSchema"]["required"]) == {"end_date", "start_date"}  # type: ignore[index]
    assert schema["inputSchema"]["additionalProperties"] is False  # type: ignore[index]


def test_ledger_budgets_refuses_a_reversed_period_content_free(tmp_path: Path) -> None:
    from mcp_synthetic import build_synthetic_mcp_database

    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    service = LedgerMCPService(db_path)
    try:
        envelope = _structured(
            call_tool(
                service,
                "ledger.budgets",
                {"start_date": "2025-12-31", "end_date": "2025-01-01"},
            )
        )
    finally:
        service.close()

    assert envelope == {
        "error": {"code": "invalid_argument", "retryable": False},
        "schema": "ledger.mcp.v1",
    }


def test_ledger_budgets_refuses_more_entries_than_the_published_unpaginated_cap(
    tmp_path: Path,
) -> None:
    from mcp_synthetic import build_synthetic_mcp_database

    from ledger.mcp.contracts import MAX_BUDGET_ENTRIES

    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    now = _now()
    with session_scope(db_path) as session:
        session.query(BudgetDB).delete()
        session.add_all(
            BudgetDB(
                budget_month="*",
                category=f"Synthetic {index:04d}",
                amount=Decimal("1.00"),
                notes="",
                created_at=now,
                updated_at=now,
            )
            for index in range(MAX_BUDGET_ENTRIES + 1)
        )
    service = LedgerMCPService(db_path)
    try:
        envelope = _structured(
            call_tool(
                service,
                "ledger.budgets",
                {"start_date": "2099-01-01", "end_date": "2099-12-31"},
            )
        )
    finally:
        service.close()

    assert envelope == {
        "error": {"code": "filter_too_broad", "retryable": False},
        "schema": "ledger.mcp.v1",
    }


def test_ledger_budgets_rebuilds_its_pinned_budget_reader_after_database_replacement(
    tmp_path: Path,
) -> None:
    from mcp_synthetic import build_synthetic_mcp_database

    db_path = build_synthetic_mcp_database(tmp_path / "ledger.db")
    replacement = build_synthetic_mcp_database(tmp_path / "replacement.db")
    BudgetService(db_path).replace_budgets(
        [{"month": "*", "category": "Groceries", "amount": "100.00", "notes": ""}]
    )
    BudgetService(replacement).replace_budgets(
        [{"month": "*", "category": "Books", "amount": "50.00", "notes": ""}]
    )
    service = LedgerMCPService(db_path)
    os.replace(replacement, db_path)
    try:
        envelope = _structured(
            call_tool(
                service,
                "ledger.budgets",
                {"start_date": "2025-01-01", "end_date": "2025-12-31"},
            )
        )
    finally:
        service.close()

    assert envelope["data"]["definition_count"] == 1  # type: ignore[index]
    categories = {row["category"] for row in envelope["data"]["entries"]}  # type: ignore[index]
    assert "Books" in categories
