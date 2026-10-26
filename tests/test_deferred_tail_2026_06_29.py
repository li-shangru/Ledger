"""Regression tests for the 2026-06-29 deferred-tail (LOW/INFO) fixes."""

from __future__ import annotations

import contextlib
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path


def _write_period_csv(tmp_path: Path, name: str, content: str) -> tuple[Path, Path]:
    period_dir = tmp_path / "exports" / "2025-02"
    period_dir.mkdir(parents=True, exist_ok=True)
    file_path = period_dir / name
    file_path.write_text(content)
    return tmp_path / "exports", file_path


def _make_txn(txn_id: str, category: str, *, amount_signed: Decimal):
    from ledger.core import Transaction

    flow = "income" if amount_signed > 0 else "expense"
    return Transaction(
        txn_id=txn_id,
        txn_fingerprint=f"fp-{txn_id}",
        posting_date=date(2026, 5, 1),
        institution="harbor",
        account="chequing",
        flow=flow,
        cashflow_type=flow,
        include_in_cashflow=True,
        amount_raw=abs(amount_signed),
        amount_signed=amount_signed,
        description=f"DESC {txn_id}",
        merchant="M",
        category=category,
        currency="CAD",
        source_file="test.csv",
        source_path="test/test.csv",
        source_row=1,
        imported_at=datetime.now(UTC).replace(tzinfo=None),
        parser_version="test",
    )


def _write(db: Path, txns: list) -> None:
    from ledger.processing import ProcessingResult
    from ledger.storage.writer import DatabaseWriter

    DatabaseWriter(db).write_result(
        ProcessingResult(transactions=txns, transfers=[], exceptions=[], total_scanned=len(txns)),
        db.parent,
        "t",
    )


# ---------------------------------------------------------------------------
# Wave A — correctness.
# ---------------------------------------------------------------------------


# (Meadow's reward classifier got the byte-identical fix; the Cedar path
# above exercises the shared sign-by-raw-amount behavior.)


def test_is_month_folder_rejects_impossible_months():
    from ledger.utils.text import is_month_folder

    assert is_month_folder("2025-01") and is_month_folder("2025-12")
    assert not is_month_folder("2025-00")
    assert not is_month_folder("2025-13")
    assert not is_month_folder("2025-99")


def test_coerce_decimal_raises_valueerror_not_invalidoperation():
    import pytest

    from ledger.core import Transaction

    with pytest.raises(ValueError):
        Transaction(
            txn_id="x",
            txn_fingerprint="fp",
            posting_date=date(2025, 1, 1),
            institution="harbor",
            account="chequing",
            flow="expense",
            cashflow_type="expense",
            include_in_cashflow=True,
            amount_raw="not-a-number",
            amount_signed=Decimal("-1.00"),
            description="d",
            merchant="m",
            category="c",
            source_file="s.csv",
            source_path="s.csv",
            source_row=1,
        )


def test_load_categories_bad_row_raises_config_error(tmp_path):
    import pytest

    from ledger.config.loader import load_categories
    from ledger.core.exceptions import ConfigError

    # Empty category_norm trips CategoryRule(min_length=1); must be a ConfigError,
    # not a raw pydantic ValidationError.
    bad = tmp_path / "categories.toml"
    bad.write_text('version = "1"\n\n[[category]]\ncategory_norm = ""\n', encoding="utf-8")
    with pytest.raises(ConfigError):
        load_categories(bad)


# ---------------------------------------------------------------------------
# Wave B — data integrity.
# ---------------------------------------------------------------------------


def test_validate_database_flags_missing_column(tmp_path):
    import sqlite3

    from ledger.storage.database import init_database
    from ledger.storage.migrations import validate_database

    db = tmp_path / "ledger.db"
    init_database(db)
    # A clean DB validates.
    assert validate_database(db, record=False).status == "success"

    # Drop a migration-added column; the table is still present but broken.
    # transaction_date is nullable and carries no index, which is what SQLite's
    # DROP COLUMN requires -- the check under test is column presence, not the column.
    # (This used counterparty_account until migration 20260810_0023 dropped that
    # never-populated column from transactions outright.)
    with contextlib.closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("ALTER TABLE transactions DROP COLUMN transaction_date")
    result = validate_database(db, record=False)
    assert result.status == "failed"
    assert any("transactions.transaction_date" in e for e in result.errors), result.errors


def test_backup_same_second_does_not_overwrite(tmp_path):
    from ledger.storage.database import init_database
    from ledger.storage.operations import backup_database

    db = tmp_path / "ledger.db"
    init_database(db)
    backups = tmp_path / "backups"
    a = backup_database(db, backup_dir=backups, reason="test")
    b = backup_database(db, backup_dir=backups, reason="test")
    assert a.ok and b.ok
    pa = Path(str(a.artifacts["backup"]))
    pb = Path(str(b.artifacts["backup"]))
    assert pa != pb, "second same-second backup overwrote the first"
    assert pa.exists() and pb.exists()


def test_import_review_now_is_iso_z_string():
    from ledger import import_review

    now = import_review._now()
    assert isinstance(now, str) and now.endswith("Z")


# ---------------------------------------------------------------------------
# Wave D — performance (behavior-preserving).
# ---------------------------------------------------------------------------


def test_get_latest_account_balance_returns_most_recent(tmp_path):
    from account_fixtures import seed_account

    from ledger.query.service import LedgerQueryService
    from ledger.storage.database import AccountBalanceDB, init_database, session_scope

    db = tmp_path / "ledger.db"
    init_database(db)
    account_id = seed_account(db, "harbor", "chequing")
    with session_scope(db) as s:
        for d, bal in [
            (date(2026, 5, 1), "100.00"),
            (date(2026, 5, 15), "250.00"),
            (date(2026, 6, 1), "999.00"),  # after end_date below -> excluded
        ]:
            amt = Decimal(bal)
            s.add(
                AccountBalanceDB(
                    account_id=account_id,
                    balance_date=d,
                    balance=amt,
                    transaction_count=1,
                    balance_7d_avg=amt,
                    balance_30d_avg=amt,
                    balance_7d_min=amt,
                    balance_7d_max=amt,
                    computed_at=datetime.now(UTC).replace(tzinfo=None),
                )
            )
    svc = LedgerQueryService(db)
    row = svc.get_latest_account_balance(
        account_id,
        end_date=date(2026, 5, 31),
        balance_basis="relative",
    )
    assert row is not None and Decimal(str(row["balance"])) == Decimal("250.00")
    assert svc.get_latest_account_balance("missing", balance_basis="relative") is None


def test_true_cost_by_category_matches_details(tmp_path):
    from ledger.query.service import LedgerQueryService
    from ledger.storage.database import init_database

    db = tmp_path / "ledger.db"
    init_database(db)
    _write(
        db,
        [
            _make_txn("a", "Dining", amount_signed=Decimal("-25.00")),
            _make_txn("b", "Groceries", amount_signed=Decimal("-40.00")),
        ],
    )
    svc = LedgerQueryService(db)
    by_cat = svc.get_true_cost_by_category(date(2026, 5, 1), date(2026, 5, 31))
    details = svc.get_true_cost_category_details(date(2026, 5, 1), date(2026, 5, 31))
    derived = {cat: vals["true_cost"] for cat, vals in details.items()}
    assert derived == by_cat, "categories page derivation diverges from get_true_cost_by_category"


# ---------------------------------------------------------------------------
# Wave F — import_review carry across repair promote.
# ---------------------------------------------------------------------------


def test_carry_forward_import_review(tmp_path):
    from ledger.import_review import get_review_rows, save_review_rows, seed_review_candidates
    from ledger.repair.statements import carry_forward_import_review
    from ledger.storage.database import init_database

    live = tmp_path / "live.db"
    candidate = tmp_path / "candidate.db"
    init_database(live)
    init_database(candidate)

    seed_review_candidates(
        live,
        [{"key": "rk-1", "merchant": "RAW CO", "current_category": "Other", "txn_ids": ["t1"]}],
    )
    save_review_rows(
        live,
        [{"review_key": "rk-1", "status": "resolved", "merchant": "USER CO", "category": "Dining"}],
    )

    assert get_review_rows(candidate) == []  # candidate starts empty
    carried = carry_forward_import_review(live, candidate)
    assert carried == 1

    rows = {r["review_key"]: r for r in get_review_rows(candidate)}
    assert rows["rk-1"]["status"] == "resolved"
    assert rows["rk-1"]["merchant"] == "USER CO"
    assert rows["rk-1"]["category"] == "Dining"


# ---------------------------------------------------------------------------
# Wave H — testing.
# ---------------------------------------------------------------------------


def test_run_migrations_is_idempotent(tmp_path):
    from ledger.storage.database import init_database
    from ledger.storage.migrations import migration_status, run_migrations, validate_database

    db = tmp_path / "ledger.db"
    init_database(db)

    first = run_migrations(db)
    assert first.status == "success"
    status_after_first = migration_status(db)
    assert status_after_first["pending"] == []

    # A second run must be a no-op (no pending, no error) and leave the schema valid.
    second = run_migrations(db)
    assert second.status == "success"
    assert migration_status(db)["applied"] == status_after_first["applied"]
    assert migration_status(db)["pending"] == []
    assert validate_database(db, record=False).status == "success"
