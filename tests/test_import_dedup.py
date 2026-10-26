"""Re-import must replace a statement's rows, not duplicate them.

The writer upserts by txn_id, so if a re-import changes a row's txn_id (e.g. a
parser/normalization tweak alters the description that feeds the hash), the old
row would survive and the transaction would be double-counted. The writer is
made authoritative per source_file: importing a statement replaces exactly that
statement's rows and touches no others.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ledger.core import Transaction
from ledger.imports.pdf.processing import validate_processing_result, write_result_to_db
from ledger.processing import ProcessingResult
from ledger.storage.database import TransactionDB, init_database, session_scope


def _txn(
    txn_id: str,
    source_file: str,
    description: str = "GROCERY",
    amount: Decimal = Decimal("-50.00"),
    source_path: str | None = None,
    *,
    source_row: int = 1,
    institution: str = "harbor",
    account: str = "harbor-chequing",
    posting_date: date = date(2025, 1, 15),
    currency: str = "CAD",
    merchant: str = "GROCERY",
    category: str = "Groceries",
) -> Transaction:
    return Transaction(
        txn_id=txn_id,
        txn_fingerprint=f"fp_{txn_id}",
        posting_date=posting_date,
        institution=institution,
        account=account,
        account_type="Chequing",
        flow="expense",
        cashflow_type="expense",
        include_in_cashflow=True,
        currency=currency,
        amount_raw=abs(amount),
        amount_signed=amount,
        description=description,
        merchant=merchant,
        category=category,
        exclude_from_spending=False,
        source_file=source_file,
        source_path=source_path or f"/x/{source_file}",
        source_period="2025-01",
        source_row=source_row,
        imported_at=date(2025, 1, 16),
        parser_version="test",
    )


def _result(transactions: list[Transaction]) -> ProcessingResult:
    return ProcessingResult(
        transactions=transactions,
        transfers=[],
        exceptions=[],
        total_scanned=len(transactions),
        duplicates_removed=0,
        collisions=0,
    )


def _txn_ids(db_path: Path, source_file: str | None = None) -> list[str]:
    with session_scope(db_path) as session:
        query = session.query(TransactionDB)
        if source_file is not None:
            query = query.filter(TransactionDB.source_file == source_file)
        return sorted(row.txn_id for row in query.all())


def test_reimport_with_changed_txn_id_replaces_row(tmp_path):
    db_path = tmp_path / "dedup.db"
    init_database(db_path)

    write_result_to_db(db_path, Path("/x"), _result([_txn("id_old", "stmt-A.csv", "GROCERY OLD")]))
    write_result_to_db(db_path, Path("/x"), _result([_txn("id_new", "stmt-A.csv", "GROCERY NEW")]))

    # Old row replaced, not duplicated.
    assert _txn_ids(db_path, "stmt-A.csv") == ["id_new"]


def test_reimport_changed_ids_preserve_independent_manual_fields(tmp_path):
    db_path = tmp_path / "manual-overrides.db"
    init_database(db_path)
    statement = "2025-01/stmt.csv"

    write_result_to_db(
        db_path,
        Path("/x"),
        _result(
            [
                _txn("old-category", "stmt.csv", source_path=statement, source_row=1),
                _txn("old-merchant", "stmt.csv", source_path=statement, source_row=2),
            ]
        ),
    )
    with session_scope(db_path) as session:
        category_row = session.get(TransactionDB, "old-category")
        assert category_row is not None
        category_row.category = "Manual Category"
        category_row.category_confidence = "manual"
        category_row.exclude_from_spending = True

        merchant_row = session.get(TransactionDB, "old-merchant")
        assert merchant_row is not None
        merchant_row.merchant = "MANUAL MERCHANT"
        merchant_row.merchant_confidence = "manual"

    write_result_to_db(
        db_path,
        Path("/x"),
        _result(
            [
                _txn(
                    "new-category",
                    "stmt.csv",
                    "DESCRIPTION CHANGED",
                    source_path=statement,
                    source_row=1,
                    account="renamed-account",
                    merchant="PARSED CATEGORY MERCHANT",
                    category="Parsed Category",
                ),
                _txn(
                    "new-merchant",
                    "stmt.csv",
                    "DESCRIPTION ALSO CHANGED",
                    source_path=statement,
                    source_row=2,
                    account="renamed-account",
                    merchant="PARSED MERCHANT",
                    category="Parsed Merchant Category",
                ),
            ]
        ),
    )

    with session_scope(db_path) as session:
        category_row = session.get(TransactionDB, "new-category")
        assert category_row is not None
        assert (category_row.category, category_row.category_confidence) == (
            "Manual Category",
            "manual",
        )
        assert category_row.exclude_from_spending is True
        assert category_row.merchant == "PARSED CATEGORY MERCHANT"

        merchant_row = session.get(TransactionDB, "new-merchant")
        assert merchant_row is not None
        assert (merchant_row.merchant, merchant_row.merchant_confidence) == (
            "MANUAL MERCHANT",
            "manual",
        )
        assert merchant_row.category == "Parsed Merchant Category"
        assert merchant_row.exclude_from_spending is False


def test_reimport_normalized_source_path_deletes_old_changed_id(tmp_path):
    db_path = tmp_path / "normalized-path.db"
    init_database(db_path)

    write_result_to_db(
        db_path,
        Path("/x"),
        _result(
            [
                _txn(
                    "old-normalized",
                    "stmt.csv",
                    source_path="2025-01/./stmt.csv",
                )
            ]
        ),
    )
    with session_scope(db_path) as session:
        row = session.get(TransactionDB, "old-normalized")
        assert row is not None
        row.category = "Manual Category"
        row.category_confidence = "manual"

    write_result_to_db(
        db_path,
        Path("/x"),
        _result(
            [
                _txn(
                    "new-normalized",
                    "stmt.csv",
                    "DESCRIPTION CHANGED",
                    source_path="2025-01/stmt.csv",
                    category="Parsed Category",
                )
            ]
        ),
    )

    assert _txn_ids(db_path) == ["new-normalized"]
    with session_scope(db_path) as session:
        row = session.get(TransactionDB, "new-normalized")
        assert row is not None
        assert (row.category, row.category_confidence) == ("Manual Category", "manual")


def test_reimport_changed_id_preserves_rule_fields(tmp_path):
    db_path = tmp_path / "rule-overrides.db"
    init_database(db_path)
    statement = "2025-01/stmt.csv"

    write_result_to_db(
        db_path,
        Path("/x"),
        _result([_txn("old-rule", "stmt.csv", source_path=statement)]),
    )
    with session_scope(db_path) as session:
        row = session.get(TransactionDB, "old-rule")
        assert row is not None
        row.merchant = "RULE MERCHANT"
        row.merchant_confidence = "rule"
        row.category = "Rule Category"
        row.category_confidence = "rule"
        row.exclude_from_spending = True

    write_result_to_db(
        db_path,
        Path("/x"),
        _result(
            [
                _txn(
                    "new-rule",
                    "stmt.csv",
                    "NEW DESCRIPTION",
                    source_path=statement,
                    account="new-account",
                    merchant="PARSED MERCHANT",
                    category="Parsed Category",
                )
            ]
        ),
    )

    with session_scope(db_path) as session:
        row = session.get(TransactionDB, "new-rule")
        assert row is not None
        assert (row.merchant, row.merchant_confidence) == ("RULE MERCHANT", "rule")
        assert (row.category, row.category_confidence) == ("Rule Category", "rule")
        assert row.exclude_from_spending is True


@pytest.mark.parametrize(
    "changed_guard",
    [
        {"institution": "cedar"},
        {"posting_date": date(2025, 1, 16)},
        {"currency": "USD"},
        {"amount": Decimal("-51.00")},
    ],
    ids=["institution", "posting-date", "currency", "raw-amount"],
)
def test_reimport_manual_guard_mismatch_rolls_back_statement(tmp_path, changed_guard):
    db_path = tmp_path / "guard-rollback.db"
    init_database(db_path)
    statement = "2025-01/stmt.csv"

    write_result_to_db(
        db_path,
        Path("/x"),
        _result([_txn("old-manual", "stmt.csv", source_path=statement)]),
    )
    with session_scope(db_path) as session:
        row = session.get(TransactionDB, "old-manual")
        assert row is not None
        row.category = "Manual Category"
        row.category_confidence = "manual"

    with pytest.raises(RuntimeError, match="MANUAL"):
        write_result_to_db(
            db_path,
            Path("/x"),
            _result(
                [
                    _txn(
                        "new-manual",
                        "stmt.csv",
                        source_path=statement,
                        **changed_guard,
                    )
                ]
            ),
        )

    assert _txn_ids(db_path) == ["old-manual"]
    with session_scope(db_path) as session:
        row = session.get(TransactionDB, "old-manual")
        assert row is not None
        assert (row.category, row.category_confidence) == ("Manual Category", "manual")


def test_reimport_duplicate_source_coordinate_rolls_back_manual_override(tmp_path):
    db_path = tmp_path / "duplicate-coordinate.db"
    init_database(db_path)
    statement = "2025-01/stmt.csv"

    write_result_to_db(
        db_path,
        Path("/x"),
        _result([_txn("old-manual", "stmt.csv", source_path=statement)]),
    )
    with session_scope(db_path) as session:
        row = session.get(TransactionDB, "old-manual")
        assert row is not None
        row.merchant = "MANUAL MERCHANT"
        row.merchant_confidence = "manual"

    with pytest.raises(RuntimeError, match="MANUAL"):
        write_result_to_db(
            db_path,
            Path("/x"),
            _result(
                [
                    _txn("new-a", "stmt.csv", source_path=statement),
                    _txn(
                        "new-b",
                        "stmt.csv",
                        "SECOND PARSED ROW",
                        source_path=statement,
                    ),
                ]
            ),
        )

    assert _txn_ids(db_path) == ["old-manual"]


def test_reimport_manual_does_not_fallback_onto_unchanged_non_durable_row(tmp_path):
    db_path = tmp_path / "reserved-unchanged-id.db"
    init_database(db_path)
    statement = "2025-01/stmt.csv"

    write_result_to_db(
        db_path,
        Path("/x"),
        _result(
            [
                _txn(
                    "old-manual",
                    "stmt.csv",
                    "MANUAL SOURCE ORIGINAL",
                    source_path=statement,
                    category="Manual Source Parsed",
                ),
                _txn(
                    "unchanged-neighbor",
                    "stmt.csv",
                    "NEIGHBOR ORIGINAL",
                    source_path=statement,
                    category="Neighbor Original",
                ),
            ]
        ),
    )
    with session_scope(db_path) as session:
        row = session.get(TransactionDB, "old-manual")
        assert row is not None
        row.category = "Manual Category"
        row.category_confidence = "manual"

    with pytest.raises(RuntimeError, match="MANUAL"):
        write_result_to_db(
            db_path,
            Path("/x"),
            _result(
                [
                    _txn(
                        "unchanged-neighbor",
                        "stmt.csv",
                        "NEIGHBOR REPARSED",
                        source_path=statement,
                        category="Neighbor Reparsed",
                    )
                ]
            ),
        )

    assert _txn_ids(db_path) == ["old-manual", "unchanged-neighbor"]
    with session_scope(db_path) as session:
        manual = session.get(TransactionDB, "old-manual")
        neighbor = session.get(TransactionDB, "unchanged-neighbor")
        assert manual is not None and neighbor is not None
        assert (manual.category, manual.category_confidence) == ("Manual Category", "manual")
        assert (neighbor.description, neighbor.category) == (
            "NEIGHBOR ORIGINAL",
            "Neighbor Original",
        )


def test_reimport_does_not_touch_other_statements(tmp_path):
    db_path = tmp_path / "dedup2.db"
    init_database(db_path)

    write_result_to_db(
        db_path,
        Path("/x"),
        # Distinct financial facts keep this case focused on statement isolation.
        _result(
            [
                _txn("a1", "stmt-A.csv"),
                _txn("b1", "stmt-B.csv", "SEPARATE STATEMENT PURCHASE"),
            ]
        ),
    )
    # Re-import only statement A with a changed txn_id; statement B must remain.
    write_result_to_db(db_path, Path("/x"), _result([_txn("a2", "stmt-A.csv")]))

    assert _txn_ids(db_path) == ["a2", "b1"]


def test_reimport_one_month_keeps_other_months(tmp_path):
    """Real data reuses one basename across months; re-importing one month must
    not delete the others. The statement key is source_path, not source_file."""
    db_path = tmp_path / "months.db"
    init_database(db_path)

    write_result_to_db(
        db_path,
        Path("/x"),
        _result(
            [
                _txn("jan1", "acct.csv", source_path="2025-01/acct.csv"),
                _txn(
                    "feb1",
                    "acct.csv",
                    source_path="2025-02/acct.csv",
                    posting_date=date(2025, 2, 15),
                ),
            ]
        ),
    )
    # Re-import ONLY February (a changed txn_id).
    write_result_to_db(
        db_path,
        Path("/x"),
        _result(
            [
                _txn(
                    "feb2",
                    "acct.csv",
                    source_path="2025-02/acct.csv",
                    posting_date=date(2025, 2, 15),
                )
            ]
        ),
    )

    # January survives; February replaced.
    assert _txn_ids(db_path) == ["feb2", "jan1"]


def test_reimport_moving_transaction_to_transfer_removes_old_row(tmp_path):
    """A row that becomes a transfer on re-import is dropped from transactions."""
    from ledger.storage.database import TransferDB

    db_path = tmp_path / "xtable.db"
    init_database(db_path)

    write_result_to_db(
        db_path, Path("/x"), _result([_txn("x1", "s.csv", source_path="2025-01/s.csv")])
    )
    moved = _txn("x2", "s.csv", source_path="2025-01/s.csv")
    write_result_to_db(
        db_path,
        Path("/x"),
        ProcessingResult(
            transactions=[],
            transfers=[moved],
            exceptions=[],
            total_scanned=1,
            duplicates_removed=0,
            collisions=0,
        ),
    )

    assert _txn_ids(db_path) == []  # no stale transaction row
    with session_scope(db_path) as session:
        assert sorted(r.txn_id for r in session.query(TransferDB).all()) == ["x2"]


def test_validate_processing_result_blocks_empty_import(tmp_path):
    """The zero-scan guard blocks an empty import before it can reach the
    delete-then-upsert write path (a safety net for replace_statement_rows)."""
    empty = ProcessingResult(
        transactions=[],
        transfers=[],
        exceptions=[],
        total_scanned=0,
        duplicates_removed=0,
        collisions=0,
    )
    with pytest.raises(SystemExit):
        validate_processing_result(empty, tmp_path / "report.json")
