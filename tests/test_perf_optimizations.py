"""
TDD tests for four behavior-preserving performance micro-optimizations.

1. apply_matches dict lookup  (#1) — existing tests cover it; one extra here
2. run_account_analysis equivalence (#2) — multi-account fixture, bit-identical stats/balances
3. TransferDB (institution, account) index migration (#3) — fails pre-migration
4. get_duplicate_candidates date-break (#4) — break doesn't drop in-window pair
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import date
from decimal import Decimal
from pathlib import Path

from ledger.analysis.transfer_matcher import TransferMatcher
from ledger.core import Transaction
from ledger.storage.database import (
    AccountBalanceDB,
    AccountDB,
    init_database,
    session_scope,
)

# ============================================================
# Helpers shared across tests
# ============================================================


def _txn(
    i: int,
    d: date,
    amt: Decimal,
    institution: str = "harbor",
    account: str = "chq-1",
    account_type: str = "Chequing",
) -> Transaction:
    cf = "income" if amt > 0 else "expense"
    return Transaction(
        txn_id=f"t{i}",
        txn_fingerprint=f"fp{i}",
        posting_date=d,
        institution=institution,
        account=account,
        account_type=account_type,
        flow=cf,
        cashflow_type=cf,
        include_in_cashflow=True,
        currency="CAD",
        amount_raw=abs(amt),
        amount_signed=amt,
        description="x",
        source_file="x.csv",
        source_path="x.csv",
        source_period="2026-01",
        source_row=i,
        imported_at=date(2026, 1, 1),
        parser_version="t",
    )


def _transfer_txn(
    i: int,
    amount_raw: Decimal,
    account: str,
    account_type: str,
    posting_date: date,
    description: str = "TRANSFER",
    institution: str = "harbor",
) -> Transaction:
    return Transaction(
        txn_id=f"tr_{i}",
        txn_fingerprint=f"fp_{i}",
        posting_date=posting_date,
        institution=institution,
        account=account,
        account_type=account_type,
        flow="transfer",
        cashflow_type="transfer",
        include_in_cashflow=False,
        currency="CAD",
        amount_raw=amount_raw,
        amount_signed=Decimal("0"),
        description=description,
        source_file="x.csv",
        source_path="x.csv",
        source_period="2025-01",
        source_row=i,
        imported_at=date(2025, 1, 1),
        parser_version="test",
    )


def _mark_pending(db_path: Path, migration_version: str) -> None:
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            "DELETE FROM schema_migrations WHERE version = ?",
            (migration_version,),
        )


def _index_names(db_path: Path, table: str) -> set[str]:
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(f"PRAGMA index_list({table})").fetchall()
    return {str(row[1]) for row in rows}


# ============================================================
# #1 — apply_matches dict-lookup equivalence
# ============================================================


def test_apply_matches_dict_lookup_assigns_same_source_dest():
    """apply_matches must assign the same paired_transfer_id/counterparty fields
    regardless of whether it uses O(n) next() scans or the O(1) dict lookup."""
    matcher = TransferMatcher()
    out = _transfer_txn(
        1,
        Decimal("500.00"),
        "harbor-chequing",
        "Chequing",
        date(2025, 1, 10),
        description="Online Banking transfer - 9816",
    )
    inn = _transfer_txn(
        2,
        Decimal("500.00"),
        "harbor-spark-visa",
        "SPARK Visa",
        date(2025, 1, 10),
        description="Online Banking transfer - 9816",
    )
    matches = matcher.match_transfers([out, inn])
    assert len(matches) == 1

    updated = matcher.apply_matches([out, inn], matches)
    updated_by_id = {t.txn_id: t for t in updated}

    # Each leg points at the other
    assert updated_by_id["tr_1"].paired_transfer_id == "tr_2"
    assert updated_by_id["tr_2"].paired_transfer_id == "tr_1"
    # Counterparty info cross-assigned
    assert updated_by_id["tr_1"].counterparty_account == "harbor-spark-visa"
    assert updated_by_id["tr_2"].counterparty_account == "harbor-chequing"


def test_apply_matches_no_match_leaves_transfers_unchanged():
    """Transactions with no match must be returned unmodified."""
    matcher = TransferMatcher()
    out = _transfer_txn(
        1,
        Decimal("500.00"),
        "harbor-chequing",
        "Chequing",
        date(2025, 1, 10),
    )
    updated = matcher.apply_matches([out], [])
    assert len(updated) == 1
    assert updated[0].paired_transfer_id is None
    assert updated[0].transfer_confidence is None


# ============================================================
# #2 — run_account_analysis equivalence (stats + balances identical)
# ============================================================


def _write_transactions(db_path: Path, txns: list[Transaction]) -> None:
    from pathlib import Path as _Path

    from ledger.processing import ProcessingResult
    from ledger.storage.writer import DatabaseWriter

    DatabaseWriter(db_path).write_result(
        ProcessingResult(
            transactions=txns,
            transfers=[],
            exceptions=[],
            duplicates_removed=0,
            total_scanned=len(txns),
        ),
        _Path("/x"),
        "t",
    )


def test_run_account_analysis_multi_account_stats_identical(tmp_path):
    """run_account_analysis must produce the same AccountDB rows for two accounts
    (a chequing and a credit card) — exact values checked against hand-computed
    expectations also satisfied by the current implementation.

    This test enforces output parity: if the refactor changes stats or balances,
    it will fail."""
    db = tmp_path / "runner.db"
    init_database(db)

    # Account 1: chequing (harbor / chq-1) — 3 transactions
    chq_txns = [
        _txn(0, date(2026, 1, 5), Decimal("5000"), account="chq-1", account_type="Chequing"),
        _txn(1, date(2026, 1, 15), Decimal("-200"), account="chq-1", account_type="Chequing"),
        _txn(2, date(2026, 2, 5), Decimal("-300"), account="chq-1", account_type="Chequing"),
    ]

    # Account 2: credit card (harbor / card-1) — 2 purchases
    card_txns = [
        Transaction(
            txn_id="cp0",
            txn_fingerprint="cfp0",
            posting_date=date(2026, 1, 10),
            institution="harbor",
            account="card-1",
            account_type="visa",
            flow="expense",
            cashflow_type="expense",
            include_in_cashflow=True,
            currency="CAD",
            amount_raw=Decimal("500"),
            amount_signed=Decimal("-500"),
            description="x",
            source_file="x.csv",
            source_path="x.csv",
            source_period="2026-01",
            source_row=10,
            imported_at=date(2026, 1, 1),
            parser_version="t",
        ),
        Transaction(
            txn_id="cp1",
            txn_fingerprint="cfp1",
            posting_date=date(2026, 2, 10),
            institution="harbor",
            account="card-1",
            account_type="visa",
            flow="expense",
            cashflow_type="expense",
            include_in_cashflow=True,
            currency="CAD",
            amount_raw=Decimal("300"),
            amount_signed=Decimal("-300"),
            description="y",
            source_file="x.csv",
            source_path="x.csv",
            source_period="2026-02",
            source_row=11,
            imported_at=date(2026, 1, 1),
            parser_version="t",
        ),
    ]

    _write_transactions(db, chq_txns + card_txns)

    from ledger.analysis.runner import AnalysisRunner

    result = AnalysisRunner(db).run_account_analysis()

    assert result["accounts_detected"] == 2

    with session_scope(db) as s:
        chq_acct = s.query(AccountDB).filter_by(account_number="chq-1").first()
        card_acct = s.query(AccountDB).filter_by(account_number="card-1").first()

        assert chq_acct is not None
        assert card_acct is not None

        # chequing: not a credit card, no limit
        assert chq_acct.is_credit_card is False
        assert chq_acct.detected_credit_limit is None
        assert chq_acct.transaction_count == 3
        assert chq_acct.account_type == "checking"

        # credit card: is_credit_card, limit = max owed (500+300=800 cumulative without payments)
        assert card_acct.is_credit_card is True
        assert card_acct.transaction_count == 2
        # Without any payment legs, cumulative balance falls to -800 -> limit rounds up to 800
        assert card_acct.detected_credit_limit == Decimal("800")

        # Balance rows must exist for both accounts
        chq_bals = s.query(AccountBalanceDB).filter_by(account_id=chq_acct.account_id).all()
        card_bals = s.query(AccountBalanceDB).filter_by(account_id=card_acct.account_id).all()

        assert len(chq_bals) > 0
        assert len(card_bals) > 0

        # Final balance for chequing: 5000 - 200 - 300 = 4500
        chq_final = max(chq_bals, key=lambda b: b.balance_date)
        assert chq_final.balance == Decimal("4500")

        # Final balance for credit card: -500 - 300 = -800
        card_final = max(card_bals, key=lambda b: b.balance_date)
        assert card_final.balance == Decimal("-800")

        # Calculator rows are explicitly relative and cannot support an absolute
        # credit-utilization percentage even when a limit estimate exists.
        assert card_final.balance_basis == "relative"
        assert card_final.credit_utilization is None


# ============================================================
# #3 — TransferDB (institution, account) index migration
# ============================================================


def test_transfer_institution_account_index_exists_on_new_db(tmp_path):
    """A freshly initialised + migrated database must carry
    ix_transfer_institution_account on the transfers table."""
    from ledger.storage.migrations import run_migrations

    db = tmp_path / "new.db"
    init_database(db)
    run_migrations(db)

    assert "ix_transfer_institution_account" in _index_names(db, "transfers"), (
        "ix_transfer_institution_account missing from freshly created DB"
    )


def test_transfer_institution_account_index_migration_on_old_db(tmp_path):
    """Migration must add ix_transfer_institution_account to a DB that pre-dates it.

    Pattern mirrors test_migrations_upgrade.py:
      1. Build current schema (init_database) so all tables exist and all
         previously-applied migrations are recorded.
      2. Drop the target index so the DB simulates a pre-migration state.
      3. Mark the new migration as pending.
      4. Run migrations.
      5. Assert the index now exists.
    """
    from ledger.storage.migrations import run_migrations

    db = tmp_path / "old.db"
    init_database(db)
    run_migrations(db)  # apply all existing migrations first

    # Confirm the index exists after a full migration run (sanity)
    assert "ix_transfer_institution_account" in _index_names(db, "transfers")

    # 2. Simulate a pre-migration DB: drop the index.
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("DROP INDEX IF EXISTS ix_transfer_institution_account")

    # Sanity: index is gone.
    assert "ix_transfer_institution_account" not in _index_names(db, "transfers")

    # 3. Mark the new migration as pending so run_migrations re-applies it.
    _mark_pending(db, "20260621_0006_transfer_institution_account_index")

    # 4. Run migrations.
    result = run_migrations(db)

    # 5. Index must be present now.
    assert "ix_transfer_institution_account" in _index_names(db, "transfers"), (
        "Migration 0006 did not create ix_transfer_institution_account on old DB"
    )
    assert "20260621_0006_transfer_institution_account_index" in result.details["applied_versions"]


# ============================================================
# #4 — get_duplicate_candidates date-break equivalence
# ============================================================


def _store_txns(db_path: Path, txns: list[Transaction]) -> None:
    """Write transactions directly to DB for quality query tests."""
    from ledger.storage.database import TransactionDB
    from ledger.utils.dates import naive_utc_now

    with session_scope(db_path) as s:
        for txn in txns:
            s.add(
                TransactionDB(
                    txn_id=txn.txn_id,
                    posting_date=txn.posting_date,
                    transaction_date=txn.posting_date,
                    institution=txn.institution,
                    account=txn.account,
                    account_type=txn.account_type,
                    flow=txn.flow,
                    cashflow_type=txn.cashflow_type,
                    include_in_cashflow=txn.include_in_cashflow,
                    currency=txn.currency,
                    amount_raw=txn.amount_raw,
                    amount_signed=txn.amount_signed,
                    description=txn.description,
                    merchant=txn.merchant or "MERCHANT_A",
                    category="Other",
                    merchant_confidence="none",
                    category_confidence="low",
                    exclude_from_spending=False,
                    merchant_raw="",
                    category_raw="",
                    source_file=txn.source_file,
                    source_path=txn.source_path,
                    source_period=txn.source_period,
                    source_row=txn.source_row,
                    imported_at=naive_utc_now(),
                    parser_version=txn.parser_version,
                    txn_fingerprint=txn.txn_fingerprint,
                    day_of_week=txn.posting_date.weekday(),
                    day_of_month=txn.posting_date.day,
                    week_of_year=txn.posting_date.isocalendar()[1],
                    is_weekend=txn.posting_date.weekday() >= 5,
                    is_month_start=txn.posting_date.day == 1,
                    is_month_end=txn.posting_date.day >= 28,
                )
            )


def _make_txn(i: int, d: date, merchant: str, amount: Decimal) -> Transaction:
    cf = "income" if amount > 0 else "expense"
    return Transaction(
        txn_id=f"dq{i}",
        txn_fingerprint=f"dfp{i}",
        posting_date=d,
        institution="harbor",
        account="chq-1",
        account_type="Chequing",
        flow=cf,
        cashflow_type=cf,
        include_in_cashflow=True,
        currency="CAD",
        amount_raw=abs(amount),
        amount_signed=amount,
        description=f"{merchant} purchase",
        merchant=merchant,
        source_file="x.csv",
        source_path="x.csv",
        source_period="2026-01",
        source_row=i,
        imported_at=date(2026, 1, 1),
        parser_version="t",
    )


def test_duplicate_candidates_in_window_pair_found(tmp_path):
    """A same-merchant, same-amount pair within threshold_days must be found."""
    from ledger.query.quality import QualityQueries

    db = tmp_path / "dup.db"
    init_database(db)

    txns = [
        _make_txn(0, date(2026, 1, 5), "STARBUCKS", Decimal("-5.75")),
        _make_txn(1, date(2026, 1, 6), "STARBUCKS", Decimal("-5.75")),  # 1 day apart -> in window
    ]
    _store_txns(db, txns)

    candidates = QualityQueries(db).get_duplicate_candidates(threshold_days=2)
    assert len(candidates) == 1
    ids = {candidates[0][0].txn_id, candidates[0][1].txn_id}
    assert ids == {"dq0", "dq1"}


def test_duplicate_candidates_outside_window_not_found(tmp_path):
    """A same-merchant, same-amount pair BEYOND threshold_days must NOT be returned."""
    from ledger.query.quality import QualityQueries

    db = tmp_path / "dup2.db"
    init_database(db)

    txns = [
        _make_txn(0, date(2026, 1, 5), "STARBUCKS", Decimal("-5.75")),
        _make_txn(
            1, date(2026, 1, 10), "STARBUCKS", Decimal("-5.75")
        ),  # 5 days -> beyond threshold 2
    ]
    _store_txns(db, txns)

    candidates = QualityQueries(db).get_duplicate_candidates(threshold_days=2)
    assert len(candidates) == 0


def test_duplicate_candidates_break_does_not_drop_boundary_pair(tmp_path):
    """A pair exactly at threshold_days apart (date_diff == threshold_days) is
    still within the window (condition is date_diff <= threshold_days) and must
    be returned.

    This is the critical off-by-one test: the break fires only when
    (second.posting_date - first.posting_date).days > threshold_days,
    so an exact-boundary pair must still be found."""
    from ledger.query.quality import QualityQueries

    db = tmp_path / "dup3.db"
    init_database(db)

    txns = [
        _make_txn(0, date(2026, 1, 5), "STARBUCKS", Decimal("-5.75")),
        _make_txn(
            1, date(2026, 1, 7), "STARBUCKS", Decimal("-5.75")
        ),  # exactly 2 days -> at boundary
    ]
    _store_txns(db, txns)

    candidates = QualityQueries(db).get_duplicate_candidates(threshold_days=2)
    assert len(candidates) == 1, (
        "Pair exactly at threshold_days apart must be included (date_diff <= threshold_days)"
    )


def test_duplicate_candidates_span_across_threshold_cluster(tmp_path):
    """Three same-merchant transactions: first two within window, third beyond.
    Only the in-window pair must be returned; the break must skip the third
    WITHOUT incorrectly skipping the second."""
    from ledger.query.quality import QualityQueries

    db = tmp_path / "dup4.db"
    init_database(db)

    txns = [
        _make_txn(0, date(2026, 1, 5), "STARBUCKS", Decimal("-5.75")),
        _make_txn(1, date(2026, 1, 6), "STARBUCKS", Decimal("-5.75")),  # 1 day -> in window
        _make_txn(2, date(2026, 1, 20), "STARBUCKS", Decimal("-5.75")),  # 15 days from #0 -> beyond
    ]
    _store_txns(db, txns)

    candidates = QualityQueries(db).get_duplicate_candidates(threshold_days=2)
    # Only the (dq0, dq1) pair is within window; (dq0, dq2) and (dq1, dq2) are not.
    assert len(candidates) == 1
    ids = {candidates[0][0].txn_id, candidates[0][1].txn_id}
    assert ids == {"dq0", "dq1"}
