"""Test database models."""

import contextlib
import os
import sqlite3
import stat
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ledger.core import Transaction
from ledger.core.enums import Confidence
from ledger.processing import ProcessingResult
from ledger.rules.categorization import CategorizationService
from ledger.storage.database import (
    TransactionDB,
    TransactionOffsetDB,
    TransactionTreatmentDB,
    TransferDB,
    get_session,
    init_database,
    session_scope,
)
from ledger.storage.writer import DatabaseWriter
from ledger.utils.hashing import stable_id


def temporal_fields(posting_date: date) -> dict:
    return {
        "day_of_week": posting_date.weekday(),
        "day_of_month": posting_date.day,
        "week_of_year": posting_date.isocalendar()[1],
        "is_weekend": posting_date.weekday() >= 5,
        "is_month_start": posting_date.day <= 3,
        "is_month_end": posting_date.day >= 28,
    }


@pytest.fixture
def test_db(tmp_path):
    """Create a temporary test database."""
    db_path = tmp_path / "test.db"
    init_database(db_path)
    yield db_path
    # Cleanup
    if db_path.exists():
        db_path.unlink()


def test_create_database(test_db):
    """Test database creation."""
    assert test_db.exists()


def test_init_database_creates_owner_only_file_before_schema_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.storage import database

    db_path = tmp_path / "fresh.db"
    observed_modes: list[int | None] = []
    real_create_all = database.Base.metadata.create_all

    def inspect_initial_mode(engine) -> None:
        observed_modes.append(stat.S_IMODE(db_path.stat().st_mode) if db_path.exists() else None)
        real_create_all(engine)

    monkeypatch.setattr(database.Base.metadata, "create_all", inspect_initial_mode)
    previous_umask = os.umask(0o022)
    try:
        database.init_database(db_path)
    finally:
        os.umask(previous_umask)

    assert observed_modes == [0o600]
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o600


def test_init_database_chmod_failure_removes_fresh_db_and_sidecars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.storage import database

    db_path = tmp_path / "fresh.db"
    sidecars = [Path(f"{db_path}{suffix}") for suffix in ("-journal", "-wal", "-shm")]
    for sidecar in sidecars:
        sidecar.write_bytes(b"synthetic partial state")
    real_fchmod = database.os.fchmod

    def fail_db_chmod(descriptor: int, mode: int) -> None:
        if os.fstat(descriptor).st_ino == db_path.stat().st_ino:
            raise PermissionError("simulated chmod failure")
        real_fchmod(descriptor, mode)

    monkeypatch.setattr(database.os, "fchmod", fail_db_chmod)

    with pytest.raises(PermissionError, match="simulated chmod failure"):
        database.init_database(db_path)

    assert not db_path.exists()
    assert all(not sidecar.exists() for sidecar in sidecars)
    assert str(db_path.resolve()) not in database._engine_cache


def test_init_database_existing_chmod_failure_precedes_schema_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.storage import database

    db_path = tmp_path / "existing.db"
    with contextlib.closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute("create table sentinel (value text)")
    db_path.chmod(0o644)
    expected_inode = db_path.stat().st_ino
    real_fchmod = database.os.fchmod

    def fail_db_chmod(descriptor: int, mode: int) -> None:
        if os.fstat(descriptor).st_ino == expected_inode:
            raise PermissionError("simulated existing chmod failure")
        real_fchmod(descriptor, mode)

    monkeypatch.setattr(database.os, "fchmod", fail_db_chmod)

    with pytest.raises(PermissionError, match="simulated existing chmod failure"):
        database.init_database(db_path)

    with contextlib.closing(sqlite3.connect(db_path)) as connection, connection:
        tables = {
            str(row[0])
            for row in connection.execute("select name from sqlite_master where type = 'table'")
        }
    assert tables == {"sentinel"}


def test_init_database_fails_closed_on_real_parent_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.storage import database

    parent = tmp_path / "data"
    parent.mkdir()
    db_path = parent / "fresh.db"
    moved_parent = tmp_path / "moved-data"
    substitute_parent = tmp_path / "substitute-data"
    substitute_parent.mkdir()
    real_create_all = database.Base.metadata.create_all

    def swap_parent_then_create(bind) -> None:
        parent.rename(moved_parent)
        substitute_parent.rename(parent)
        real_create_all(bind)

    monkeypatch.setattr(database.Base.metadata, "create_all", swap_parent_then_create)
    try:
        with pytest.raises(OSError, match="changed"):
            database.init_database(db_path)
    finally:
        if parent.exists():
            parent.rename(substitute_parent)
        if moved_parent.exists():
            moved_parent.rename(parent)

    assert not db_path.exists()
    assert not (substitute_parent / db_path.name).exists()


def test_init_database_rejects_existing_final_symlink_without_mutating_target(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside.db"
    with contextlib.closing(sqlite3.connect(outside)) as connection, connection:
        connection.execute("create table sentinel (value text)")
    db_path = tmp_path / "ledger.db"
    db_path.symlink_to(outside)

    with pytest.raises(OSError, match="symlink"):
        init_database(db_path)

    with contextlib.closing(sqlite3.connect(outside)) as connection, connection:
        tables = {
            str(row[0])
            for row in connection.execute("select name from sqlite_master where type = 'table'")
        }
    assert tables == {"sentinel"}


def test_init_database_rejects_symlinked_parent_without_creating_outside_db(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    redirected = tmp_path / "data"
    redirected.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError, match="symlink"):
        init_database(redirected / "ledger.db")

    assert not (outside / "ledger.db").exists()


def test_insert_transaction(test_db):
    """Test inserting a transaction."""
    session = get_session(test_db)

    txn = TransactionDB(
        txn_id="abc123",
        posting_date=date(2025, 1, 15),
        transaction_date=date(2025, 1, 15),
        institution="harbor",
        account="chequing",
        flow="expense",
        cashflow_type="expense",
        include_in_cashflow=True,
        amount_raw=Decimal("100.00"),
        amount_signed=Decimal("-100.00"),
        description="TEST TRANSACTION",
        merchant="TEST MERCHANT",
        category="Test",
        source_file="test.csv",
        source_path="test/test.csv",
        source_row=1,
        imported_at=datetime.now(UTC).replace(tzinfo=None),
        parser_version="test",
        txn_fingerprint="fingerprint123",
        **temporal_fields(date(2025, 1, 15)),
    )

    session.add(txn)
    session.commit()

    # Query it back
    result = session.query(TransactionDB).filter_by(txn_id="abc123").first()
    assert result is not None
    assert result.merchant == "TEST MERCHANT"
    assert result.amount_signed == Decimal("-100.00")

    session.close()


def test_query_by_date_range(test_db):
    """Test querying by date range."""
    session = get_session(test_db)

    # Insert multiple transactions
    for i in range(5):
        txn = TransactionDB(
            txn_id=f"txn{i}",
            posting_date=date(2025, 1, i + 1),
            institution="harbor",
            account="test",
            flow="expense",
            cashflow_type="expense",
            include_in_cashflow=True,
            amount_raw=Decimal("10.00"),
            amount_signed=Decimal("-10.00"),
            description=f"Transaction {i}",
            source_file="test.csv",
            source_path="test/test.csv",
            source_row=i,
            imported_at=datetime.now(UTC).replace(tzinfo=None),
            parser_version="test",
            txn_fingerprint=f"fp{i}",
            **temporal_fields(date(2025, 1, i + 1)),
        )
        session.add(txn)

    session.commit()

    # Query date range
    results = (
        session.query(TransactionDB)
        .filter(
            TransactionDB.posting_date >= date(2025, 1, 2),
            TransactionDB.posting_date <= date(2025, 1, 4),
        )
        .all()
    )

    assert len(results) == 3

    session.close()


def test_database_writer(test_db, tmp_path):
    """Test writing transactions via DatabaseWriter."""
    writer = DatabaseWriter(test_db)

    # Create a test transaction
    txn = Transaction(
        txn_id="test123",
        txn_fingerprint="fp123",
        posting_date=date(2025, 1, 1),
        institution="harbor",
        account="test",
        flow="expense",
        cashflow_type="expense",
        include_in_cashflow=True,
        amount_raw=Decimal("50.00"),
        amount_signed=Decimal("-50.00"),
        description="Test expense",
        merchant="TEST MERCHANT",
        category="Test",
        source_file="test.csv",
        source_path="test/test.csv",
        source_row=1,
        imported_at=datetime.now(UTC).replace(tzinfo=None),
        parser_version="test",
    )

    # Create result
    result = ProcessingResult(
        transactions=[txn],
        transfers=[],
        exceptions=[],
        total_scanned=1,
    )

    # Write to database
    writer.write_result(result, tmp_path, "test")

    # Check stats
    stats = writer.get_stats()
    assert stats["total_transactions"] == 1
    assert stats["total_transfers"] == 0
    assert stats["import_runs"] == 1


def test_database_writer_upsert(test_db, tmp_path):
    """Test that re-importing the same transaction updates instead of duplicating."""
    writer = DatabaseWriter(test_db)

    # Create a transaction
    txn = Transaction(
        txn_id="test456",
        txn_fingerprint="fp456",
        posting_date=date(2025, 1, 1),
        institution="harbor",
        account="test",
        flow="expense",
        cashflow_type="expense",
        include_in_cashflow=True,
        amount_raw=Decimal("100.00"),
        amount_signed=Decimal("-100.00"),
        description="Original description",
        merchant="ORIGINAL MERCHANT",
        category="Test",
        source_file="test.csv",
        source_path="test/test.csv",
        source_row=1,
        imported_at=datetime.now(UTC).replace(tzinfo=None),
        parser_version="test",
    )

    result = ProcessingResult(
        transactions=[txn],
        transfers=[],
        exceptions=[],
        total_scanned=1,
    )

    # Write first time
    writer.write_result(result, tmp_path, "test")
    stats1 = writer.get_stats()
    assert stats1["total_transactions"] == 1

    # Update the transaction (same ID)
    txn_updated = Transaction(
        txn_id="test456",  # Same ID
        txn_fingerprint="fp456_updated",
        posting_date=date(2025, 1, 1),
        institution="harbor",
        account="test",
        flow="expense",
        cashflow_type="expense",
        include_in_cashflow=True,
        amount_raw=Decimal("100.00"),
        amount_signed=Decimal("-100.00"),
        description="Updated description",
        merchant="UPDATED MERCHANT",
        category="Test",
        source_file="test.csv",
        source_path="test/test.csv",
        source_row=1,
        imported_at=datetime.now(UTC).replace(tzinfo=None),
        parser_version="test",
    )

    result2 = ProcessingResult(
        transactions=[txn_updated],
        transfers=[],
        exceptions=[],
        total_scanned=1,
    )

    # Write second time (should update, not insert)
    writer.write_result(result2, tmp_path, "test")
    stats2 = writer.get_stats()
    assert stats2["total_transactions"] == 1  # Still only 1, not 2

    # Verify the merchant was updated
    session = get_session(test_db)
    result_txn = session.query(TransactionDB).filter_by(txn_id="test456").first()
    assert result_txn.merchant == "UPDATED MERCHANT"
    assert result_txn.description == "Updated description"
    session.close()


def _make_txn(txn_id: str, category: str, merchant: str = "TEST MERCHANT") -> Transaction:
    """Build a minimal Transaction for writer tests."""
    return Transaction(
        txn_id=txn_id,
        txn_fingerprint=f"fp-{txn_id}",
        posting_date=date(2026, 5, 1),
        institution="harbor",
        account="chequing",
        flow="expense",
        cashflow_type="expense",
        include_in_cashflow=True,
        amount_raw=Decimal("25.00"),
        amount_signed=Decimal("-25.00"),
        description=f"DESC {txn_id}",
        merchant=merchant,
        category=category,
        source_file="test.csv",
        source_path="test/test.csv",
        source_row=1,
        imported_at=datetime.now(UTC).replace(tzinfo=None),
        parser_version="test",
    )


def test_reimport_preserves_manual_category(test_db, tmp_path):
    """Re-importing the same txn must NOT overwrite a manually-set category (M1 fix)."""
    writer = DatabaseWriter(test_db)

    # First import: parsed category is "Shopping"
    result1 = ProcessingResult(
        transactions=[_make_txn("manual-001", "Shopping")],
        transfers=[],
        exceptions=[],
        total_scanned=1,
    )
    writer.write_result(result1, tmp_path, "test")

    # User manually overrides the category to "Groceries"
    ok = CategorizationService(test_db).set_transaction_category("manual-001", "Groceries")
    assert ok

    with session_scope(test_db) as session:
        row = session.get(TransactionDB, "manual-001")
        assert row.category == "Groceries"
        assert row.category_confidence == "manual"

    # Re-import the SAME txn_id but with a DIFFERENT parsed category ("Entertainment")
    result2 = ProcessingResult(
        transactions=[_make_txn("manual-001", "Entertainment")],
        transfers=[],
        exceptions=[],
        total_scanned=1,
    )
    writer.write_result(result2, tmp_path, "test")

    # Manual override must survive the re-import
    with session_scope(test_db) as session:
        row = session.get(TransactionDB, "manual-001")
        assert row.category == "Groceries", (
            f"Manual category was overwritten to {row.category!r} on re-import; expected 'Groceries'"
        )
        assert row.category_confidence == "manual", (
            f"category_confidence changed to {row.category_confidence!r}; expected 'manual'"
        )


def test_reimport_preserves_manual_merchant(test_db, tmp_path):
    """Re-importing the same txn must NOT overwrite a manually-set merchant — the mirror
    of the manual-category guard, previously untested (review remediation #25)."""
    writer = DatabaseWriter(test_db)

    writer.write_result(
        ProcessingResult(
            transactions=[_make_txn("mm-001", "Shopping", merchant="AUTO MERCHANT")],
            transfers=[],
            exceptions=[],
            total_scanned=1,
        ),
        tmp_path,
        "test",
    )

    # User manually overrides the merchant.
    with session_scope(test_db) as session:
        row = session.get(TransactionDB, "mm-001")
        row.merchant = "MY MANUAL MERCHANT"
        row.merchant_confidence = Confidence.MANUAL

    # Re-import the SAME txn_id with a DIFFERENT parser-derived merchant.
    writer.write_result(
        ProcessingResult(
            transactions=[_make_txn("mm-001", "Shopping", merchant="AUTO MERCHANT 2")],
            transfers=[],
            exceptions=[],
            total_scanned=1,
        ),
        tmp_path,
        "test",
    )

    with session_scope(test_db) as session:
        row = session.get(TransactionDB, "mm-001")
        assert row.merchant == "MY MANUAL MERCHANT", (
            f"Manual merchant was overwritten to {row.merchant!r} on re-import"
        )
        assert row.merchant_confidence == "manual"


def test_reimport_updates_non_manual_merchant(test_db, tmp_path):
    """Regression: re-import must still overwrite a non-manual merchant."""
    writer = DatabaseWriter(test_db)
    for merchant in ("OLD MERCHANT", "NEW MERCHANT"):
        writer.write_result(
            ProcessingResult(
                transactions=[_make_txn("nmm-001", "Shopping", merchant=merchant)],
                transfers=[],
                exceptions=[],
                total_scanned=1,
            ),
            tmp_path,
            "test",
        )
    with session_scope(test_db) as session:
        row = session.get(TransactionDB, "nmm-001")
        assert row.merchant == "NEW MERCHANT"


def test_reimport_updates_non_manual_category(test_db, tmp_path):
    """Regression: re-import must still overwrite a non-manual category."""
    writer = DatabaseWriter(test_db)

    # First import
    result1 = ProcessingResult(
        transactions=[_make_txn("non-manual-001", "Shopping")],
        transfers=[],
        exceptions=[],
        total_scanned=1,
    )
    writer.write_result(result1, tmp_path, "test")

    # Re-import with updated category (non-manual — default confidence)
    result2 = ProcessingResult(
        transactions=[_make_txn("non-manual-001", "Entertainment")],
        transfers=[],
        exceptions=[],
        total_scanned=1,
    )
    writer.write_result(result2, tmp_path, "test")

    with session_scope(test_db) as session:
        row = session.get(TransactionDB, "non-manual-001")
        assert row.category == "Entertainment", (
            f"Non-manual category was NOT updated on re-import; still {row.category!r}"
        )


# ---------------------------------------------------------------------------
# Schema-width tests (TDD for the String(16) → String(40) fix)
# ---------------------------------------------------------------------------

STABLE_ID_LEN = len(stable_id("probe"))  # must be 40


def test_stable_id_is_40_chars():
    """stable_id() must always produce a 40-character hex string."""
    sid = stable_id("harbor", "chequing", "2025-01-15", "-100.00", "MCDONALDS")
    assert len(sid) == 40, f"Expected 40, got {len(sid)}"


def test_schema_txn_id_column_width():
    """TransactionDB.txn_id declared width must be >= stable_id output length.

    This test FAILS with String(16) and PASSES after the fix to String(40).
    """
    col_type = TransactionDB.__table__.c.txn_id.type
    assert col_type.length >= STABLE_ID_LEN, (
        f"TransactionDB.txn_id is String({col_type.length}), "
        f"but stable_id() produces {STABLE_ID_LEN}-char strings"
    )


# TransactionDB.paired_transfer_id had a width test here too. The column was never
# populated by any writer and migration 20260810_0023 dropped it; the TransferDB
# twin below is the one that actually stores a stable_id.


def test_schema_transfer_txn_id_column_width():
    """TransferDB.txn_id declared width must be >= stable_id output length."""
    col_type = TransferDB.__table__.c.txn_id.type
    assert col_type.length >= STABLE_ID_LEN, (
        f"TransferDB.txn_id is String({col_type.length}), "
        f"but stable_id() produces {STABLE_ID_LEN}-char strings"
    )


def test_schema_transfer_paired_transfer_id_column_width():
    """TransferDB.paired_transfer_id width must be >= stable_id length."""
    col_type = TransferDB.__table__.c.paired_transfer_id.type
    assert col_type.length >= STABLE_ID_LEN, (
        f"TransferDB.paired_transfer_id is String({col_type.length}), "
        f"but stable_id() produces {STABLE_ID_LEN}-char strings"
    )


def test_schema_treatment_txn_id_column_width():
    """TransactionTreatmentDB.txn_id width must be >= stable_id length."""
    col_type = TransactionTreatmentDB.__table__.c.txn_id.type
    assert col_type.length >= STABLE_ID_LEN, (
        f"TransactionTreatmentDB.txn_id is String({col_type.length}), "
        f"but stable_id() produces {STABLE_ID_LEN}-char strings"
    )


def test_schema_offset_credit_txn_id_column_width():
    """TransactionOffsetDB.credit_txn_id width must be >= stable_id length."""
    col_type = TransactionOffsetDB.__table__.c.credit_txn_id.type
    assert col_type.length >= STABLE_ID_LEN, (
        f"TransactionOffsetDB.credit_txn_id is String({col_type.length}), "
        f"but stable_id() produces {STABLE_ID_LEN}-char strings"
    )


def test_schema_offset_debit_txn_id_column_width():
    """TransactionOffsetDB.debit_txn_id width must be >= stable_id length."""
    col_type = TransactionOffsetDB.__table__.c.debit_txn_id.type
    assert col_type.length >= STABLE_ID_LEN, (
        f"TransactionOffsetDB.debit_txn_id is String({col_type.length}), "
        f"but stable_id() produces {STABLE_ID_LEN}-char strings"
    )


def test_round_trip_full_length_stable_id(tmp_path):
    """Full-length stable_id values round-trip through the DB untruncated.

    On SQLite this would pass even with String(16) (length ignored), but
    combined with the schema-width tests above, it confirms end-to-end
    correctness after the fix.
    """
    db_path = tmp_path / "rt.db"
    init_database(db_path)

    txn_sid = stable_id("harbor", "chequing", "2025-06-01", "-100.00", "TEST ROUNDTRIP")
    transfer_sid = stable_id("harbor", "savings", "2025-06-01", "100.00", "TRANSFER OUT")
    paired_sid = stable_id("harbor", "chequing", "2025-06-01", "-100.00", "TRANSFER IN")

    assert len(txn_sid) == 40
    assert len(transfer_sid) == 40
    assert len(paired_sid) == 40

    now = datetime.now(UTC).replace(tzinfo=None)
    posting = date(2025, 6, 1)

    with session_scope(db_path) as session:
        txn_row = TransactionDB(
            txn_id=txn_sid,
            posting_date=posting,
            institution="harbor",
            account="chequing",
            flow="expense",
            cashflow_type="expense",
            include_in_cashflow=True,
            amount_raw=Decimal("100.00"),
            amount_signed=Decimal("-100.00"),
            description="TEST ROUNDTRIP",
            merchant="TEST",
            category="Test",
            source_file="rt.csv",
            source_path="rt/rt.csv",
            source_row=1,
            imported_at=now,
            parser_version="test",
            txn_fingerprint="a" * 32,
            day_of_week=posting.weekday(),
            day_of_month=posting.day,
            week_of_year=posting.isocalendar()[1],
            is_weekend=posting.weekday() >= 5,
            is_month_start=posting.day <= 3,
            is_month_end=posting.day >= 28,
        )
        transfer_row = TransferDB(
            txn_id=transfer_sid,
            posting_date=posting,
            institution="harbor",
            account="savings",
            flow="transfer",
            amount_raw=Decimal("100.00"),
            description="TRANSFER OUT",
            source_file="rt.csv",
            source_path="rt/rt.csv",
            imported_at=now,
            paired_transfer_id=paired_sid,
        )
        session.add(txn_row)
        session.add(transfer_row)

    # Read back and verify IDs are untruncated
    with session_scope(db_path) as session:
        t = session.get(TransactionDB, txn_sid)
        assert t is not None, "TransactionDB row not found by full-length stable_id PK"
        assert t.txn_id == txn_sid, f"txn_id truncated: {t.txn_id!r}"

        tr = session.get(TransferDB, transfer_sid)
        assert tr is not None, "TransferDB row not found by full-length stable_id PK"
        assert tr.txn_id == transfer_sid, f"transfer txn_id truncated: {tr.txn_id!r}"
        assert tr.paired_transfer_id == paired_sid, (
            f"transfer paired_transfer_id truncated: {tr.paired_transfer_id!r}"
        )


def test_backup_artifact_is_owner_only(tmp_path):
    """A fresh backup snapshot must be 0o600 (not world-readable 0o644)."""
    from ledger.storage.operations import backup_database

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    result = backup_database(db_path, backup_dir=tmp_path / "b", reason="perm")
    assert result.ok, result.errors
    artifact = Path(result.artifacts["backup"])
    assert (artifact.stat().st_mode & 0o777) == 0o600
    assert (artifact.parent.stat().st_mode & 0o777) == 0o700


def test_tighten_db_permissions_sweeps_legacy_world_readable_files(tmp_path):
    """A one-shot sweep must tighten a pre-existing 0o644 live DB and any
    legacy *.db / *.db.backup-* files it finds under the data dir."""
    from ledger.storage.operations import tighten_db_permissions

    data_dir = tmp_path / "data"
    (data_dir / "db-backups").mkdir(parents=True)
    live = data_dir / "ledger.db"
    init_database(live)
    legacy_backup = data_dir / "ledger.db.backup-20260101-000000"
    snapshot = data_dir / "db-backups" / "ledger-20260101T000000Z-old.db"
    for p in (legacy_backup, snapshot):
        p.write_bytes(b"SQLite format 3\x00")
    # Simulate the world-readable state the audit found.
    for p in (live, legacy_backup, snapshot):
        os.chmod(p, 0o644)

    touched = tighten_db_permissions(data_dir)

    for p in (live, legacy_backup, snapshot):
        assert (p.stat().st_mode & 0o777) == 0o600, p
    assert (data_dir / "db-backups").stat().st_mode & 0o777 == 0o700
    assert set(touched) >= {live, legacy_backup, snapshot}


def test_tighten_db_permissions_never_follows_symlinks(tmp_path):
    from ledger.storage.operations import tighten_db_permissions

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    outside_file = tmp_path / "outside.db"
    outside_file.write_bytes(b"outside")
    outside_file.chmod(0o644)
    (data_dir / "linked.db").symlink_to(outside_file)
    outside_dir = tmp_path / "outside-backups"
    outside_dir.mkdir(mode=0o755)
    outside_dir.chmod(0o755)
    (data_dir / "db-backups").symlink_to(outside_dir, target_is_directory=True)

    touched = tighten_db_permissions(data_dir)

    assert touched == []
    assert outside_file.stat().st_mode & 0o777 == 0o644
    assert outside_dir.stat().st_mode & 0o777 == 0o755


def test_tighten_db_permissions_rejects_symlinked_root(tmp_path):
    from ledger.storage.operations import tighten_db_permissions

    outside = tmp_path / "outside-data"
    outside.mkdir()
    private_db = outside / "ledger.db"
    private_db.write_bytes(b"SQLite format 3\x00")
    private_db.chmod(0o644)
    redirected_root = tmp_path / "data"
    redirected_root.symlink_to(outside, target_is_directory=True)

    assert tighten_db_permissions(redirected_root) == []
    assert private_db.stat().st_mode & 0o777 == 0o644
