"""Invented transaction/transfer evidence for cross-source identity refusal."""

import hashlib
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from datetime import date, datetime
from decimal import Decimal, Inexact, localcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from import_binding_fixtures import bound_import_state

from ledger.commands import build as build_commands
from ledger.core import Transaction
from ledger.imports.pdf import workflow
from ledger.imports.pdf.paths import Paths
from ledger.imports.pdf.processing import write_result_to_db
from ledger.imports.pdf.statement_pdf_to_csv import ExtractedCsv
from ledger.processing import ProcessingResult, pipeline
from ledger.processing.identity import (
    AmbiguousCrossSourceIdentityError,
    require_unambiguous_cross_source_identity,
    transaction_identity,
    transfer_identity,
)
from ledger.processing.pipeline import deduplicate, disambiguate_within_file, partition
from ledger.storage.database import (
    TransactionDB,
    TransactionOffsetDB,
    TransactionTreatmentDB,
    TransferDB,
    init_database,
    session_scope,
)
from ledger.storage.writer import DatabaseWriter
from ledger.utils.hashing import fingerprint, stable_id
from ledger.utils.text import clean_account_name


def _row(txn_id: str, source: str, *, kind: str = "transaction", **changes) -> Transaction:
    fields = {
        "txn_id": txn_id,
        "txn_fingerprint": "invented-content-fingerprint",
        "posting_date": date(2044, 5, 23),
        "transaction_date": date(2044, 5, 22),
        "institution": "fictional",
        "account": "fictional-chequing",
        "account_type": "Chequing",
        "flow": "payment" if kind == "transfer" else "expense",
        "cashflow_type": "transfer" if kind == "transfer" else "expense",
        "include_in_cashflow": kind != "transfer",
        "amount_raw": Decimal("-24.50"),
        "amount_signed": Decimal("0.00") if kind == "transfer" else Decimal("-24.50"),
        "currency": "CAD",
        "description": "INVENTED IDENTICAL FINANCIAL EVENT",
        "merchant": "Invented merchant",
        "category": "Other",
        "source_file": Path(source.replace("\\", "/")).name,
        "source_path": source,
        "source_period": "2044-05",
        "source_row": 2,
        "imported_at": datetime(2044, 6, 1, 12),
        "parser_version": "synthetic-identity-test",
    }
    return Transaction(**(fields | changes))


def _result(*rows: Transaction) -> ProcessingResult:
    transactions, transfers = partition(list(rows))
    return ProcessingResult(transactions=transactions, transfers=transfers, total_scanned=len(rows))


def _assert_ambiguity(operation) -> None:
    with pytest.raises(
        AmbiguousCrossSourceIdentityError, match=r"^ambiguous_cross_source_identity$"
    ) as caught:
        operation()
    assert type(caught.value).__name__ == "AmbiguousCrossSourceIdentityError"
    assert caught.value.code == "ambiguous_cross_source_identity"
    assert caught.value.args == ("ambiguous_cross_source_identity",)


def _financial_rows(db: Path) -> dict[str, list[tuple]]:
    tables = (
        "transactions",
        "transfers",
        "transaction_treatments",
        "transaction_offsets",
        "import_runs",
        "processed_files",
    )
    with closing(sqlite3.connect(db)) as connection, connection:
        return {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in tables
        }


@pytest.mark.parametrize("kind", ["transaction", "transfer"])
@pytest.mark.parametrize("strict", [True, False])
@pytest.mark.parametrize("reverse", [False, True])
def test_batch_refuses_unresolved_novel_cross_source_identity(kind, strict, reverse):
    rows = [
        _row("invented-id-a", "2044-05/account.csv", kind=kind),
        _row("invented-id-b", "2044-06/account.csv", kind=kind),
    ]
    if kind == "transfer":
        # TransferDB has no fingerprint: comparison must use its stored fields.
        rows[1] = rows[1].model_copy(update={"txn_fingerprint": "different-or-missing"})
    if reverse:
        rows.reverse()
    before = [row.model_dump() for row in rows]
    _assert_ambiguity(lambda: deduplicate(rows, strict=strict))
    assert [row.model_dump() for row in rows] == before


@pytest.mark.parametrize("kind", ["transaction", "transfer"])
@pytest.mark.parametrize("reverse", [False, True])
def test_sequential_novel_cross_source_identity_refuses_without_mutation(tmp_path, kind, reverse):
    db = tmp_path / "synthetic.db"
    rows = [
        _row("invented-id-a", "2044-05/account.csv", kind=kind),
        _row("invented-id-b", "2044-06/account.csv", kind=kind),
    ]
    if reverse:
        rows.reverse()
    write_result_to_db(db, tmp_path, _result(rows[0]))
    before = _financial_rows(db)
    _assert_ambiguity(lambda: write_result_to_db(db, tmp_path, _result(rows[1])))
    assert _financial_rows(db) == before


@pytest.mark.parametrize("kind", ["transaction", "transfer"])
def test_writer_checks_incoming_batch_even_without_pipeline_deduplication(tmp_path, kind):
    db = tmp_path / "synthetic.db"
    writer = DatabaseWriter(db)
    before = _financial_rows(db)
    result = _result(
        _row("invented-id-a", "2044-05/account.csv", kind=kind),
        _row("invented-id-b", "2044-06/account.csv", kind=kind),
    )
    _assert_ambiguity(lambda: writer.write_result(result, tmp_path, "test"))
    assert _financial_rows(db) == before


def test_refusal_rolls_back_statement_replacement_treatments_offsets_and_run(tmp_path):
    db = tmp_path / "synthetic.db"
    retained = _row("retained-id", "2044-05/account.csv")
    replaced = _row(
        "replaced-id",
        "2044-06/account.csv",
        txn_fingerprint="older-different-content",
        description="INVENTED REPLACEMENT ROW",
    )
    transfer = _row("replaced-transfer", "2044-06/account.csv", kind="transfer")
    write_result_to_db(db, tmp_path, _result(retained, replaced, transfer))
    with session_scope(db) as session:
        session.add(
            TransactionTreatmentDB(
                txn_id=replaced.txn_id,
                treatment_type="synthetic",
                status="confirmed",
                confidence=Decimal("1.00"),
                netting_policy="synthetic",
                evidence={"synthetic": True},
                detected_at=datetime(2044, 6, 1),
            )
        )
        session.add(
            TransactionOffsetDB(
                credit_txn_id=replaced.txn_id,
                debit_txn_id=retained.txn_id,
                offset_amount=Decimal("2.31"),
                confidence=Decimal("1.00"),
                status="confirmed",
                evidence={"synthetic": True},
                detected_at=datetime(2044, 6, 1),
            )
        )
    before = _financial_rows(db)
    harmless = _row(
        "harmless-new-id",
        "2044-06/account.csv",
        txn_fingerprint="different-content",
        description="INVENTED NEW ROW",
    )
    ambiguous = _row("novel-ambiguous-id", "2044-06/account.csv")
    _assert_ambiguity(lambda: write_result_to_db(db, tmp_path, _result(harmless, ambiguous)))
    assert _financial_rows(db) == before


def _paths(tmp_path: Path) -> Paths:
    inbox, processed = tmp_path / "inbox", tmp_path / "processed"
    inbox.mkdir()
    processed.mkdir()
    db = tmp_path / "synthetic.db"
    init_database(db)
    return Paths(
        pdf_dir=inbox,
        processed_dir=processed,
        db_path=db,
        base_db_path=None,
        backup_dir=tmp_path / "backups",
        ledger_config=tmp_path / "ledger.toml",
        merchant_rules=tmp_path / "merchants.toml",
        categories=tmp_path / "categories.toml",
        runs_dir=tmp_path / "runs",
    )


def _extract(pdf: Path, csv_root: Path, processed_dir: Path) -> ExtractedCsv:
    csv_path = csv_root / "2044-06" / "account.csv"
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_text("INVENTED HEADER\nINVENTED ROW\n")
    return ExtractedCsv(
        pdf=pdf,
        csv_path=csv_path,
        processed_pdf=processed_dir / "invented-statement.pdf",
        statement_type="invented-test",
        transaction_count=1,
        account_slug="fictional-chequing",
    )


def test_stage_writer_failure_persists_current_failed_run(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    original = paths.pdf_dir / "invented-statement.pdf"
    original.write_bytes(b"%PDF invented synthetic test")
    monkeypatch.setattr(
        workflow.statement_pdf_to_csv,
        "process_pdf",
        lambda pdf, csv_root, processed_dir, **_kw: _extract(pdf, csv_root, processed_dir),
    )
    monkeypatch.setattr(workflow, "load_config", lambda *_args: object())
    monkeypatch.setattr(
        workflow,
        "process",
        lambda **_kw: _result(_row("new-id", "2044-06/account.csv")),
    )

    def refuse_writer(*_args):
        raise RuntimeError("ambiguous_cross_source_identity")

    monkeypatch.setattr(workflow, "write_result_to_db", refuse_writer)
    before = _financial_rows(paths.db_path)
    with pytest.raises(RuntimeError, match=r"^ambiguous_cross_source_identity$"):
        workflow.stage(SimpleNamespace(), paths)
    [run] = [p for p in paths.runs_dir.iterdir() if p.is_dir()]
    state = json.loads((run / "run.json").read_text())
    assert state["status"] == "failed"
    assert state["failure_reason"] == "processing_exception"
    assert state["failure_exception_type"] == "RuntimeError"
    assert state["approved_csv_paths"] == [str(run / "csv/2044-06/account.csv")]
    assert (paths.runs_dir / "latest-run.txt").read_text().strip() == str(run)
    assert original.read_bytes() == b"%PDF invented synthetic test"
    assert not list(paths.processed_dir.iterdir())
    assert _financial_rows(paths.db_path) == before


@pytest.mark.parametrize("kind", ["transaction", "transfer"])
@pytest.mark.parametrize("reverse", [False, True])
def test_cooccurrence_proves_multiplicity_even_when_another_source_has_a_subset(
    tmp_path, kind, reverse
):
    only_a = [_row("stable-a", "2044-05/account.csv", kind=kind)]
    together = [
        _row("stable-a", "2044-06/account.csv", kind=kind),
        _row("stable-b", "2044-06/account.csv", kind=kind, source_row=3),
    ]
    batches = [only_a, together]
    if reverse:
        batches.reverse()
    unique, duplicates, collisions = deduplicate([row for batch in batches for row in batch])
    assert {row.txn_id for row in unique} == {"stable-a", "stable-b"}
    assert duplicates == 1 and collisions == 0
    assert all(row.amount_raw == Decimal("-24.50") for row in unique)

    db = tmp_path / "synthetic.db"
    for batch in batches:
        write_result_to_db(db, tmp_path, _result(*batch))
    model = TransactionDB if kind == "transaction" else TransferDB
    with session_scope(db) as session:
        saved = session.query(model).order_by(model.txn_id).all()
        assert [row.txn_id for row in saved] == ["stable-a", "stable-b"]
        assert [row.amount_raw for row in saved] == [Decimal("-24.50"), Decimal("-24.50")]
        first_sources = {row.txn_id: row.source_path for row in saved}
        first_imports = {row.txn_id: row.imported_at for row in saved}
    # First-arrival provenance can differ after incremental imports; repeating
    # these known IDs still cannot be mistaken for novel ambiguous identities.
    later = [
        row.model_copy(
            update={
                "source_path": "2044-07/account.csv",
                "imported_at": datetime(2044, 8, 1),
            }
        )
        for row in together
    ]
    write_result_to_db(db, tmp_path, _result(*later))
    with session_scope(db) as session:
        saved = session.query(model).all()
        assert {row.txn_id: row.source_path for row in saved} == first_sources
        assert {row.txn_id: row.imported_at for row in saved} == first_imports


@pytest.mark.parametrize("kind", ["transaction", "transfer"])
@pytest.mark.parametrize("reverse", [False, True])
def test_shared_intermediate_id_does_not_resolve_other_novel_ids(tmp_path, kind, reverse):
    batches = [
        [
            _row("stable-a", "2044-05/account.csv", kind=kind),
            _row("stable-b", "2044-05/account.csv", kind=kind, source_row=3),
        ],
        [
            _row("stable-b", "2044-06/account.csv", kind=kind),
            _row("stable-c", "2044-06/account.csv", kind=kind, source_row=3),
        ],
    ]
    if reverse:
        batches.reverse()
    _assert_ambiguity(lambda: deduplicate([row for batch in batches for row in batch]))
    db = tmp_path / "synthetic.db"
    write_result_to_db(db, tmp_path, _result(*batches[0]))
    before = _financial_rows(db)
    _assert_ambiguity(lambda: write_result_to_db(db, tmp_path, _result(*batches[1])))
    assert _financial_rows(db) == before


@pytest.mark.parametrize("kind", ["transaction", "transfer"])
def test_within_file_occurrence_ids_and_fingerprints_survive_overlapping_sources(tmp_path, kind):
    first = disambiguate_within_file(
        [
            _row("base-id", "2044-05/account.csv", kind=kind),
            _row("base-id", "2044-05/account.csv", kind=kind, source_row=3),
        ]
    )
    expected = [(row.txn_id, row.txn_fingerprint) for row in first]
    assert len({identity for identity, _ in expected}) == 2
    assert expected[0] == ("base-id", "invented-content-fingerprint")
    repeated = [row.model_copy(update={"source_path": "2044-06/account.csv"}) for row in first]
    unique, duplicates, collisions = deduplicate([*first, *repeated])
    assert [(row.txn_id, row.txn_fingerprint) for row in unique] == expected
    assert duplicates == 2 and collisions == 0
    db = tmp_path / "synthetic.db"
    write_result_to_db(db, tmp_path, _result(*first))
    write_result_to_db(db, tmp_path, _result(*repeated))
    with session_scope(db) as session:
        model = TransactionDB if kind == "transaction" else TransferDB
        saved = session.query(model).all()
        assert {row.txn_id for row in saved} == {identity for identity, _ in expected}
        assert all(row.amount_raw == Decimal("-24.50") for row in saved)
        if kind == "transaction":
            assert {(row.txn_id, row.txn_fingerprint) for row in saved} == set(expected)


@pytest.mark.parametrize("confidence", ["manual", "rule"])
def test_same_source_identity_change_preserves_multiplicity_and_durable_overrides(
    tmp_path, confidence
):
    db = tmp_path / "synthetic.db"
    old = [
        _row("old-a", "2044-06/account.csv", source_row=2),
        _row("old-b", "2044-06/account.csv", source_row=3),
    ]
    write_result_to_db(db, tmp_path, _result(*old))
    with session_scope(db) as session:
        for index, row in enumerate(session.query(TransactionDB).order_by(TransactionDB.txn_id)):
            row.merchant = f"Invented durable merchant {index}"
            row.category = f"Invented durable category {index}"
            row.merchant_confidence = confidence
            row.category_confidence = confidence
            row.exclude_from_spending = True
    replacements = [
        row.model_copy(update={"txn_id": f"new-{index}", "source_path": r"2044-06\.\account.csv"})
        for index, row in enumerate(old)
    ]
    identities = [(row.txn_id, row.txn_fingerprint) for row in replacements]
    write_result_to_db(db, tmp_path, _result(*replacements))
    with session_scope(db) as session:
        saved = session.query(TransactionDB).order_by(TransactionDB.source_row).all()
        assert [(row.txn_id, row.txn_fingerprint) for row in saved] == identities
        assert len(saved) == 2
        for index, row in enumerate(saved):
            assert row.merchant == f"Invented durable merchant {index}"
            assert row.category == f"Invented durable category {index}"
            assert row.merchant_confidence == row.category_confidence == confidence
            assert row.exclude_from_spending is True
            assert row.amount_raw == row.amount_signed == Decimal("-24.50")


@pytest.mark.parametrize(
    "changes",
    [
        {"institution": "other-fictional"},
        {"institution": "FICTIONAL"},
        {"account": "different-account"},
        {"account": "FICTIONAL-CHEQUING"},
        {"account_type": "Savings"},
        {"posting_date": date(2044, 5, 24)},
        {"transaction_date": date(2044, 5, 21)},
        {"transaction_date": None},
        {"currency": "USD"},
        {"currency": "cad"},
        {"amount_raw": Decimal("-24.51")},
        {"amount_signed": Decimal("-24.51")},
        {"description": "INVENTED DIFFERENT FINANCIAL EVENT"},
        {"flow": "fee"},
        {"cashflow_type": "neutral"},
        {"include_in_cashflow": False},
    ],
)
def test_equal_transaction_fingerprints_with_different_financial_fields_stay_distinct(
    tmp_path, changes
):
    a = _row("distinct-a", "2044-05/account.csv")
    b = _row("distinct-b", "2044-06/account.csv", **changes)
    before = [a.model_dump(), b.model_dump()]
    for rows in ([a, b], [b, a]):
        unique, duplicates, collisions = deduplicate(rows)
        assert {row.txn_id for row in unique} == {"distinct-a", "distinct-b"}
        assert duplicates == collisions == 0
    for index, rows in enumerate(([a, b], [b, a])):
        db = tmp_path / f"synthetic-{index}.db"
        for row in rows:
            write_result_to_db(db, tmp_path, _result(row))
        with session_scope(db) as session:
            saved = session.query(TransactionDB).all()
            assert {row.txn_id for row in saved} == {"distinct-a", "distinct-b"}
    assert [a.model_dump(), b.model_dump()] == before


@pytest.mark.parametrize("fingerprint", ["", "  "])
def test_absent_transaction_fingerprint_is_not_invented(tmp_path, fingerprint):
    a = _row("distinct-a", "2044-05/account.csv", txn_fingerprint=fingerprint)
    b = _row("distinct-b", "2044-06/account.csv", txn_fingerprint=fingerprint)
    unique, duplicates, collisions = deduplicate([a, b])
    assert len(unique) == 2 and duplicates == collisions == 0
    assert all(row.txn_fingerprint == "" for row in unique)
    db = tmp_path / "synthetic.db"
    for row in unique:
        write_result_to_db(db, tmp_path, _result(row))
    with session_scope(db) as session:
        assert {row.txn_id for row in session.query(TransactionDB)} == {"distinct-a", "distinct-b"}
        assert all(row.txn_fingerprint == "" for row in session.query(TransactionDB))


@pytest.mark.parametrize(
    "changes",
    [
        {"merchant": "New enrichment"},
        {"category": "New enrichment", "category_confidence": "rule"},
        {"exclude_from_spending": True},
    ],
)
def test_enrichment_cannot_hide_cross_source_ambiguity(changes):
    rows = [
        _row("novel-a", "2044-05/account.csv"),
        _row("novel-b", "2044-06/account.csv", **changes),
    ]
    _assert_ambiguity(lambda: deduplicate(rows))


@pytest.mark.parametrize(
    "changes",
    [
        {"institution": "other-fictional"},
        {"account": "fictional-credit-card"},
        {"account_type": "Credit card"},
        {"posting_date": date(2044, 5, 24)},
        {"currency": "USD"},
        {"amount_raw": Decimal("24.50"), "amount_signed": Decimal("24.50")},
        {"description": "INVENTED OTHER PAYMENT"},
        {"flow": "transfer"},
    ],
)
def test_distinct_persisted_transfer_fields_allow_legitimate_cross_source_payments(
    tmp_path, changes
):
    a = _row("payment-a", "2044-05/account.csv", kind="transfer")
    b = _row("payment-b", "2044-06/account.csv", kind="transfer", **changes)
    for index, rows in enumerate(([a, b], [b, a])):
        unique, duplicates, collisions = deduplicate(rows)
        assert {row.txn_id for row in unique} == {"payment-a", "payment-b"}
        assert duplicates == collisions == 0
        db = tmp_path / f"synthetic-{index}.db"
        for row in rows:
            write_result_to_db(db, tmp_path, _result(row))
        with session_scope(db) as session:
            assert {row.txn_id for row in session.query(TransferDB)} == {"payment-a", "payment-b"}


def test_transfer_fields_that_are_not_persisted_cannot_prove_distinctness():
    rows = [
        _row("payment-a", "2044-05/account.csv", kind="transfer"),
        _row(
            "payment-b",
            "2044-06/account.csv",
            kind="transfer",
            txn_fingerprint="",
            transaction_date=date(2044, 5, 21),
            amount_signed=Decimal("24.50"),
        ),
    ]
    _assert_ambiguity(lambda: deduplicate(rows))


def test_identity_comparison_does_not_round_or_depend_on_decimal_context():
    rows = [
        _row("novel-a", "2044-05/account.csv", amount_raw=Decimal("-98765.43")),
        _row("novel-b", "2044-06/account.csv", amount_raw=Decimal("-98765.43")),
    ]
    before = [row.model_dump() for row in rows]
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        _assert_ambiguity(lambda: deduplicate(rows, strict=False))
    assert [row.model_dump() for row in rows] == before
    assert all(row.amount_raw == Decimal("-98765.43") for row in rows)


@pytest.mark.parametrize("strict", [True, False])
def test_process_checks_cross_source_identity_after_within_file_disambiguation(
    tmp_path, monkeypatch, sample_app_config, strict
):
    first, second = tmp_path / "a.csv", tmp_path / "b.csv"
    first.write_text("INVENTED SOURCE A\n")
    second.write_text("INVENTED SOURCE B\n")
    rows = {
        first: [_row("a", "a.csv"), _row("a", "a.csv", source_row=3)],
        second: [_row("b", "b.csv")],
    }
    monkeypatch.setattr(pipeline, "parse_file", lambda _config, _root, path, *_a, **_k: rows[path])
    general = sample_app_config.ledger.general.model_copy(update={"strict": strict})
    ledger = sample_app_config.ledger.model_copy(update={"general": general})
    config = sample_app_config.model_copy(update={"ledger": ledger})
    single = pipeline.process(config, tmp_path, source_files=[first])
    expected = disambiguate_within_file(rows[first])
    assert [(r.txn_id, r.txn_fingerprint) for r in single.transactions] == [
        (r.txn_id, r.txn_fingerprint) for r in expected
    ]
    for sources in ([first, second], [second, first]):
        _assert_ambiguity(
            lambda sources=sources: pipeline.process(config, tmp_path, source_files=sources)
        )


def test_real_stage_identity_refusal_preserves_target_and_source(tmp_path, monkeypatch):
    paths = _paths(tmp_path)
    write_result_to_db(paths.db_path, tmp_path, _result(_row("existing", "2044-05/account.csv")))
    original = paths.pdf_dir / "invented-statement.pdf"
    original.write_bytes(b"%PDF invented synthetic test")
    monkeypatch.setattr(
        workflow.statement_pdf_to_csv,
        "process_pdf",
        lambda pdf, csv_root, processed_dir, **_kw: _extract(pdf, csv_root, processed_dir),
    )
    monkeypatch.setattr(workflow, "load_config", lambda *_args: object())
    monkeypatch.setattr(
        workflow, "process", lambda **_kw: _result(_row("novel", "2044-06/account.csv"))
    )
    before = _financial_rows(paths.db_path)
    _assert_ambiguity(lambda: workflow.stage(SimpleNamespace(), paths))
    [run] = [p for p in paths.runs_dir.iterdir() if p.is_dir()]
    state = json.loads((run / "run.json").read_text())
    assert state["status"] == "failed"
    assert state["failure_reason"] == "processing_exception"
    assert state["failure_exception_type"] == "AmbiguousCrossSourceIdentityError"
    assert _financial_rows(paths.db_path) == before
    assert _financial_rows(Path(state["staging_db_path"])) == before
    assert original.read_bytes() == b"%PDF invented synthetic test"
    assert not list(paths.processed_dir.iterdir())


def test_finalize_identity_refusal_rolls_back_without_publication_or_source_archive(
    tmp_path, monkeypatch
):
    paths = _paths(tmp_path)
    retained = _row("retained-id", "2044-05/account.csv")
    replaced = _row(
        "old-source-id", "2044-06/account.csv", txn_fingerprint="old-fp", description="OLD INVENTED"
    )
    write_result_to_db(paths.db_path, tmp_path, _result(retained, replaced))
    with session_scope(paths.db_path) as session:
        session.add(
            TransactionTreatmentDB(
                txn_id=replaced.txn_id,
                treatment_type="synthetic",
                status="confirmed",
                confidence=Decimal("1.00"),
                netting_policy="synthetic",
                evidence={"synthetic": True},
                detected_at=datetime(2044, 6, 1),
            )
        )
        session.add(
            TransactionOffsetDB(
                credit_txn_id=replaced.txn_id,
                debit_txn_id=retained.txn_id,
                offset_amount=Decimal("2.31"),
                confidence=Decimal("1.00"),
                status="confirmed",
                evidence={"synthetic": True},
                detected_at=datetime(2044, 6, 1),
            )
        )
    run = paths.runs_dir / "invented-run"
    run.mkdir(mode=0o700, parents=True)
    csv_root = run / "csv"
    csv_file = csv_root / "2044-06/account.csv"
    csv_file.parent.mkdir(parents=True)
    csv_file.write_text("INVENTED HEADER\nINVENTED ROW\n")
    original = paths.pdf_dir / "invented-statement.pdf"
    original.write_bytes(b"%PDF invented synthetic test")
    archive = paths.processed_dir / original.name
    review = run / "merchant-review.json"
    review.write_text("[]")
    staging = run / "staging.db"
    workflow.clone_database(paths.db_path, staging)
    state = bound_import_state(
        {
            "status": "ready_to_finalize",
            "target_db_path": str(paths.db_path),
            "staging_db_path": str(staging),
            "csv_root": str(csv_root),
            "approved_csv_paths": [str(csv_file)],
            "account_slugs": {"2044-06/account.csv": "fictional-chequing"},
            "staging_inbox": str(run / "inbox"),
            "merchant_review_json": str(review),
            "pending_review_count": 0,
            "target_db_fingerprint_before": workflow.database_fingerprint(paths.db_path),
            "target_db_import_owned_before": workflow.import_owned_fingerprint(paths.db_path),
            "pdfs": [
                {
                    "original_pdf": str(original),
                    "processed_pdf": str(archive),
                    "file_hash": hashlib.sha256(original.read_bytes()).hexdigest(),
                    "csv_path": str(csv_file),
                    "transaction_count": 1,
                    "skipped_reason": "",
                }
            ],
        },
        paths,
    )
    workflow.write_run_state(run, state)
    monkeypatch.setattr(workflow, "load_config", lambda *_args: object())
    monkeypatch.setattr(
        workflow, "process", lambda **_kw: _result(_row("novel", "2044-06/account.csv"))
    )

    def unexpected_publication(*_args, **_kwargs):
        pytest.fail("Ambiguous identity must refuse before publication or source archive")

    monkeypatch.setattr(workflow, "_promote_database_locked", unexpected_publication)
    monkeypatch.setattr(workflow, "_archive_finalize_pdfs", unexpected_publication)
    before = _financial_rows(paths.db_path)
    before_staging = _financial_rows(staging)
    _assert_ambiguity(lambda: workflow.finalize(SimpleNamespace(run_dir=run), paths))
    after_state = json.loads((run / "run.json").read_text())
    assert after_state["status"] == "failed"
    assert after_state["failure_reason"] == "finalize_failed"
    assert not after_state.get("publication_completed")
    assert _financial_rows(paths.db_path) == before
    assert _financial_rows(staging) == before_staging
    assert original.read_bytes() == b"%PDF invented synthetic test"
    assert not archive.exists()
    assert not list(paths.processed_dir.iterdir())


@pytest.mark.parametrize("kind", ["transaction", "transfer"])
def test_allowed_case_distinct_account_aliases_are_not_conflated(tmp_path, kind):
    aliases = {"first-export": "Canonical-Account", "second-export": "canonical-account"}
    first_account = clean_account_name("FIRST-EXPORT", aliases)
    second_account = clean_account_name("SECOND-EXPORT", aliases)
    assert first_account == "Canonical-Account" and second_account == "canonical-account"
    rows = [
        _row("case-a", "2044-05/account.csv", kind=kind, account=first_account),
        _row("case-b", "2044-06/account.csv", kind=kind, account=second_account),
    ]
    unique, duplicates, collisions = deduplicate(rows)
    assert len(unique) == 2 and duplicates == collisions == 0
    db = tmp_path / "synthetic.db"
    for row in rows:
        write_result_to_db(db, tmp_path, _result(row))
    with session_scope(db) as session:
        model = TransactionDB if kind == "transaction" else TransferDB
        assert {row.account for row in session.query(model)} == {first_account, second_account}


@pytest.mark.parametrize("kind", ["transaction", "transfer"])
@pytest.mark.parametrize("legacy_currency", [None, ""])
def test_missing_legacy_currency_is_preserved_without_inventing_cad(kind, legacy_currency):
    incoming = _row("current-cad", "2044-06/account.csv", kind=kind)
    # Legacy read policy permits None/empty currency. Model-copy represents such
    # already-read evidence without weakening the current non-null storage schema.
    legacy = _row("legacy-id", "2044-05/account.csv", kind=kind).model_copy(
        update={"currency": legacy_currency}
    )
    evidence = transaction_identity if kind == "transaction" else transfer_identity
    legacy_evidence = evidence(legacy)
    require_unambiguous_cross_source_identity([evidence(incoming)], [legacy_evidence])
    assert legacy.currency is legacy_currency
    assert incoming.currency == "CAD"
    assert legacy_evidence.financial_identity != evidence(incoming).financial_identity


@pytest.mark.parametrize("kind", ["transaction", "transfer"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("persisted_anchor", [False, True])
def test_process_to_writer_retains_cooccurrence_witnesses_after_deduplication(
    tmp_path, monkeypatch, sample_app_config, kind, reverse, persisted_anchor
):
    first, second = tmp_path / "first.csv", tmp_path / "second.csv"
    first.write_text("INVENTED SOURCE A\n")
    second.write_text("INVENTED SOURCE B\n")
    rows = {
        first: [_row("stable-a", "first.csv", kind=kind)],
        second: [
            _row("stable-a", "second.csv", kind=kind),
            _row("stable-b", "second.csv", kind=kind, source_row=3),
        ],
    }
    monkeypatch.setattr(pipeline, "parse_file", lambda _config, _root, path, *_a, **_k: rows[path])
    sources = [second, first] if reverse else [first, second]
    result = pipeline.process(sample_app_config, tmp_path, source_files=sources)
    actual = [*result.transactions, *result.transfers]
    assert {row.txn_id for row in actual} == {"stable-a", "stable-b"}
    assert result.duplicates_removed == 1
    db = tmp_path / "synthetic.db"
    if persisted_anchor:
        write_result_to_db(db, tmp_path, _result(_row("stable-a", "earlier.csv", kind=kind)))
    write_result_to_db(db, tmp_path, result)
    with session_scope(db) as session:
        model = TransactionDB if kind == "transaction" else TransferDB
        saved = {row.txn_id: row for row in session.query(model)}
        assert set(saved) == {"stable-a", "stable-b"}
        assert all(row.amount_raw == Decimal("-24.50") for row in saved.values())
        assert saved["stable-a"].source_path == (
            "earlier.csv" if persisted_anchor else sources[0].name
        )
        assert saved["stable-b"].source_path == "second.csv"


@pytest.mark.parametrize("kind", ["transaction", "transfer"])
@pytest.mark.parametrize(
    "changes",
    [
        {"txn_id": "unrelated-id"},
        {"amount_raw": Decimal("-24.51")},
        {"description": "DIFFERENT INVENTED EVENT"},
    ],
)
def test_transient_evidence_must_match_final_incoming_ids_and_financial_fields(
    tmp_path, kind, changes
):
    a = _row("actual-a", "first.csv", kind=kind)
    b = _row("actual-b", "second.csv", kind=kind)
    stale = _row("actual-a", "second.csv", kind=kind).model_copy(update=changes)
    identity = transaction_identity if kind == "transaction" else transfer_identity
    result = _result(a, b)
    result.identity_evidence = (identity(stale), identity(b))
    db = tmp_path / "synthetic.db"
    writer = DatabaseWriter(db)
    before = _financial_rows(db)
    _assert_ambiguity(lambda: writer.write_result(result, tmp_path, "test"))
    assert _financial_rows(db) == before


@pytest.mark.parametrize("kind", ["transaction", "transfer"])
def test_old_witness_without_a_matching_current_incoming_row_cannot_prove_distinctness(
    tmp_path, kind
):
    db = tmp_path / "synthetic.db"
    write_result_to_db(db, tmp_path, _result(_row("old-a", "old.csv", kind=kind)))
    result = _result(_row("new-b", "new.csv", kind=kind))
    identity = transaction_identity if kind == "transaction" else transfer_identity
    result.identity_evidence = (identity(_row("old-a", "new.csv", kind=kind)),)
    before = _financial_rows(db)
    _assert_ambiguity(lambda: write_result_to_db(db, tmp_path, result))
    assert _financial_rows(db) == before


def test_transaction_witness_cannot_use_a_stale_fingerprint(tmp_path):
    a = _row("actual-a", "first.csv")
    b = _row("actual-b", "second.csv")
    stale = _row("actual-a", "second.csv", txn_fingerprint="different-fingerprint")
    result = _result(a, b)
    result.identity_evidence = (transaction_identity(stale), transaction_identity(b))
    writer = DatabaseWriter(tmp_path / "synthetic.db")
    _assert_ambiguity(lambda: writer.write_result(result, tmp_path, "test"))


def test_nonstrict_collision_evidence_binds_final_rekeyed_ids_without_changing_api(
    tmp_path, monkeypatch, sample_app_config
):
    files = [tmp_path / name for name in ("first.csv", "second.csv", "overlap.csv")]
    for file in files:
        file.write_text("INVENTED SOURCE\n")
    first = _row("colliding-id", files[0].name, txn_fingerprint="fp-a", description="INVENTED A")
    second = _row("colliding-id", files[1].name, txn_fingerprint="fp-b", description="INVENTED B")
    repeated = first.model_copy(update={"source_path": files[2].name})
    parsed = {files[0]: [first], files[1]: [second], files[2]: [repeated]}
    monkeypatch.setattr(
        pipeline, "parse_file", lambda _config, _root, path, *_a, **_k: parsed[path]
    )
    general = sample_app_config.ledger.general.model_copy(update={"strict": False})
    config = sample_app_config.model_copy(
        update={"ledger": sample_app_config.ledger.model_copy(update={"general": general})}
    )
    # Ordinary callers still receive exactly the established three outputs.
    ordinary = deduplicate([first, second, repeated], strict=False)
    assert len(ordinary) == 3
    expected_ids = {(row.txn_id, row.txn_fingerprint) for row in ordinary[0]}
    assert len(expected_ids) == 2 and all(
        identity != "colliding-id" for identity, _ in expected_ids
    )
    db = tmp_path / "synthetic.db"
    for source_files in (files, list(reversed(files))):
        result = pipeline.process(config, tmp_path, source_files=source_files)
        assert result.duplicates_removed == result.collisions == 1
        assert {(r.txn_id, r.txn_fingerprint) for r in result.transactions} == expected_ids
        assert len(result.identity_evidence) == 3
        admitted = {
            (row.txn_id, transaction_identity(row).financial_identity)
            for row in result.transactions
        }
        assert all(
            (evidence.txn_id, evidence.financial_identity) in admitted
            for evidence in result.identity_evidence
        )
        assert {evidence.source_path for evidence in result.identity_evidence} == {
            file.name for file in files
        }
        write_result_to_db(db, tmp_path, result)
    with session_scope(db) as session:
        saved = session.query(TransactionDB).all()
        assert {(row.txn_id, row.txn_fingerprint) for row in saved} == expected_ids
        assert {row.description for row in saved} == {"INVENTED A", "INVENTED B"}
        assert all(row.amount_raw == row.amount_signed == Decimal("-24.50") for row in saved)


@pytest.mark.parametrize("kind", ["transaction", "transfer"])
def test_complete_pairwise_source_evidence_survives_processing_and_writing(
    tmp_path, monkeypatch, sample_app_config, kind
):
    files = [tmp_path / name for name in ("ab.csv", "ac.csv", "bc.csv")]
    groups = (("a", "b"), ("a", "c"), ("b", "c"))
    parsed = {}
    for file, ids in zip(files, groups, strict=True):
        file.write_text("INVENTED SOURCE\n")
        parsed[file] = [
            _row(identity, file.name, kind=kind, source_row=index + 2)
            for index, identity in enumerate(ids)
        ]
    monkeypatch.setattr(
        pipeline, "parse_file", lambda _config, _root, path, *_a, **_k: parsed[path]
    )
    result = pipeline.process(sample_app_config, tmp_path, source_files=files)
    assert result.duplicates_removed == 3
    db = tmp_path / "synthetic.db"
    write_result_to_db(db, tmp_path, result)
    with session_scope(db) as session:
        model = TransactionDB if kind == "transaction" else TransferDB
        assert {row.txn_id for row in session.query(model)} == {"a", "b", "c"}


@pytest.mark.parametrize("kind", ["transaction", "transfer"])
@pytest.mark.parametrize("repeat_identity", ["original", "derived"])
def test_collision_evidence_follows_rows_remapped_more_than_once(
    tmp_path, monkeypatch, sample_app_config, kind, repeat_identity
):
    files = [tmp_path / name for name in ("first.csv", "second.csv", "third.csv", "repeat.csv")]
    for file in files:
        file.write_text("INVENTED SOURCE\n")
    rows = [
        _row("collision-root", files[0].name, kind=kind, txn_fingerprint="fp-a", description="A"),
        _row("collision-root", files[1].name, kind=kind, txn_fingerprint="fp-b", description="B"),
        _row(
            stable_id("collision-root", "fp-a"),
            files[2].name,
            kind=kind,
            txn_fingerprint="fp-c",
            description="C",
        ),
    ]
    repeated = rows[0].model_copy(update={"source_path": files[3].name})
    if repeat_identity == "derived":
        repeated = repeated.model_copy(
            update={
                "txn_id": stable_id("collision-root", "fp-a"),
                "txn_fingerprint": fingerprint("fp-a", "collision-root"),
            }
        )
    rows.append(repeated)
    # A derived alias repeats the same financial fields under a different raw
    # ID. Database-bound processing compares the surviving final IDs.
    defer_identity_check = repeat_identity == "derived"
    expected = deduplicate(rows, strict=False, defer_identity_check_to_writer=defer_identity_check)
    assert len(expected) == 3
    assert len(expected[0]) == 3 and expected[1:] == (1, 2)
    evidence = []
    actual = deduplicate(
        rows,
        strict=False,
        identity_evidence=evidence,
        defer_identity_check_to_writer=defer_identity_check,
    )
    assert actual == expected
    identity = transaction_identity if kind == "transaction" else transfer_identity
    by_description = {row.description: identity(row) for row in actual[0]}
    assert len(evidence) == len(rows)
    for original, witness in zip(rows, evidence, strict=True):
        target = by_description[original.description]
        assert witness.txn_id == target.txn_id
        assert witness.financial_identity == target.financial_identity
        assert witness.source_path == original.source_path

    parsed = {file: [row] for file, row in zip(files, rows, strict=True)}
    monkeypatch.setattr(pipeline, "parse_file", lambda _c, _r, path, *_a, **_k: parsed[path])
    general = sample_app_config.ledger.general.model_copy(update={"strict": False})
    config = sample_app_config.model_copy(
        update={"ledger": sample_app_config.ledger.model_copy(update={"general": general})}
    )
    result = pipeline.process(
        config,
        tmp_path,
        source_files=files,
        defer_identity_check_to_writer=defer_identity_check,
    )
    assert {row.txn_id for row in [*result.transactions, *result.transfers]} == {
        row.txn_id for row in expected[0]
    }
    assert result.duplicates_removed == 1 and result.collisions == 2
    assert result.identity_evidence == tuple(evidence)
    db = tmp_path / "synthetic.db"
    write_result_to_db(db, tmp_path, result)
    with session_scope(db) as session:
        model = TransactionDB if kind == "transaction" else TransferDB
        assert {row.txn_id for row in session.query(model)} == {row.txn_id for row in expected[0]}


@pytest.mark.parametrize("kind", ["transaction", "transfer"])
def test_process_repeat_of_established_ids_on_separate_sources_is_idempotent(
    tmp_path, monkeypatch, sample_app_config, kind
):
    files = [tmp_path / name for name in ("first.csv", "together.csv", "later.csv")]
    for file in files:
        file.write_text("INVENTED SOURCE\n")
    a = _row("known-a", files[0].name, kind=kind)
    together = [
        _row("known-a", files[1].name, kind=kind),
        _row("known-b", files[1].name, kind=kind, source_row=3),
    ]
    b_later = _row("known-b", files[2].name, kind=kind)
    db = tmp_path / "synthetic.db"
    write_result_to_db(db, tmp_path, _result(a))
    write_result_to_db(db, tmp_path, _result(*together))
    before = _financial_rows(db)
    parsed = {files[0]: [a], files[2]: [b_later]}
    monkeypatch.setattr(pipeline, "parse_file", lambda _c, _r, path, *_a, **_k: parsed[path])
    repeated = pipeline.process(
        sample_app_config,
        tmp_path,
        source_files=[files[0], files[2]],
        defer_identity_check_to_writer=True,
    )
    write_result_to_db(db, tmp_path, repeated)
    after = _financial_rows(db)
    assert len(after.pop("import_runs")) == len(before.pop("import_runs")) + 1
    assert after == before


@pytest.mark.parametrize("kind", ["transaction", "transfer"])
@pytest.mark.parametrize("novel_phase", ["none", "stage", "finalize"])
def test_pdf_callsites_preserve_known_repeats_and_refuse_novel_ids(
    tmp_path, monkeypatch, sample_app_config, kind, novel_phase
):
    paths = _paths(tmp_path)
    write_result_to_db(paths.db_path, tmp_path, _result(_row("known-a", "first.csv", kind=kind)))
    write_result_to_db(
        paths.db_path,
        tmp_path,
        _result(
            _row("known-a", "together.csv", kind=kind),
            _row("known-b", "together.csv", kind=kind, source_row=3),
        ),
    )
    for name in ("first.pdf", "later.pdf"):
        (paths.pdf_dir / name).write_bytes(b"%PDF SYNTHETIC " + name.encode())

    def extract(pdf, csv_root, processed_dir, **_kwargs):
        csv_file = csv_root / (pdf.stem + ".csv")
        csv_file.parent.mkdir(parents=True, exist_ok=True)
        csv_file.write_text("INVENTED CSV\n")
        return ExtractedCsv(
            pdf=pdf,
            csv_path=csv_file,
            processed_pdf=processed_dir / pdf.name,
            statement_type="invented-test",
            transaction_count=1,
            account_slug="fictional-chequing",
        )

    phase = "stage"

    def parse(_config, _root, path, *_args, **_kwargs):
        identity = "known-a" if path.name == "first.csv" else "known-b"
        if path.name == "later.csv" and phase == novel_phase:
            identity = "novel-c"
        return [_row(identity, path.name, kind=kind)]

    monkeypatch.setattr(workflow.statement_pdf_to_csv, "process_pdf", extract)
    monkeypatch.setattr(workflow, "load_config", lambda *_a: sample_app_config)
    monkeypatch.setattr(pipeline, "parse_file", parse)
    monkeypatch.setattr(workflow, "build_merchant_review", lambda _r: [])
    monkeypatch.setattr(workflow, "run_post_processing", lambda *_a: {})
    before = _financial_rows(paths.db_path)
    originals = {p.name: p.read_bytes() for p in paths.pdf_dir.iterdir()}
    if novel_phase == "stage":
        _assert_ambiguity(lambda: workflow.stage(SimpleNamespace(), paths))
    else:
        workflow.stage(SimpleNamespace(), paths)
    [run] = [path for path in paths.runs_dir.iterdir() if path.is_dir()]
    state = json.loads((run / "run.json").read_text())
    staging = Path(state["staging_db_path"])
    assert _financial_rows(paths.db_path) == before
    if novel_phase == "stage":
        assert state["status"] == "failed"
        assert state["failure_exception_type"] == "AmbiguousCrossSourceIdentityError"
        assert _financial_rows(staging) == before
    else:
        assert state["status"] == "ready_to_finalize"
        before_finalize = _financial_rows(staging)
        phase = "finalize"
        if novel_phase == "finalize":
            _assert_ambiguity(lambda: workflow.finalize(SimpleNamespace(run_dir=run), paths))
            state = json.loads((run / "run.json").read_text())
            assert state["status"] == "failed" and state["failure_reason"] == "finalize_failed"
            assert _financial_rows(paths.db_path) == before
            assert _financial_rows(staging) == before_finalize
        else:
            workflow.finalize(SimpleNamespace(run_dir=run), paths)
            state = json.loads((run / "run.json").read_text())
            assert state["status"] == "finalized" and state["publication_completed"]
            after = _financial_rows(paths.db_path)
            assert after["transactions"] == before["transactions"]
            assert after["transfers"] == before["transfers"]
            assert {p.name: p.read_bytes() for p in paths.processed_dir.iterdir()} == originals
            return
    assert {p.name: p.read_bytes() for p in paths.pdf_dir.iterdir()} == originals
    assert not list(paths.processed_dir.iterdir())


def _build_options(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    defaults = tmp_path / "defaults.toml"
    ledger = tmp_path / "ledger.toml"
    merchants = tmp_path / "merchants.toml"
    categories = tmp_path / "categories.toml"
    defaults.write_text("")
    ledger.write_text("[general]\nstrict = false\n")
    merchants.write_text(
        'version = "synthetic"\n[[rule]]\nmerchant_norm = "Invented merchant"\n'
        'category = "Other"\npatterns = ["INVENTED"]\n'
    )
    categories.write_text('version = "synthetic"\n[[category]]\ncategory_norm = "Other"\n')
    return {
        "src": source,
        "out_dir": tmp_path / "exports",
        "ledger_config": ledger,
        "merchant_rules": merchants,
        "categories": categories,
        "config_file": defaults,
        "formats": "none",
        "log_level": "ERROR",
        "dry_run": True,
    }


@pytest.mark.parametrize("kind", ["transaction", "transfer"])
@pytest.mark.parametrize("novel", [False, True])
@pytest.mark.parametrize("dry_run", [False, True])
def test_database_build_preflight_preserves_known_ids_and_refuses_before_summary(
    tmp_path, monkeypatch, capsys, kind, novel, dry_run
):
    from ledger.config import load_app_config
    from ledger.config.repository import SQLiteConfigRepository

    options = _build_options(tmp_path)
    options["dry_run"] = dry_run
    db = tmp_path / "synthetic.db"
    config = load_app_config(
        options["ledger_config"], options["merchant_rules"], options["categories"]
    )
    SQLiteConfigRepository(db).replace_from_configs(config.merchants, config.categories)
    write_result_to_db(db, tmp_path, _result(_row("known-a", "first.csv", kind=kind)))
    write_result_to_db(
        db,
        tmp_path,
        _result(
            _row("known-a", "together.csv", kind=kind),
            _row("known-b", "together.csv", kind=kind, source_row=3),
        ),
    )
    rows = {
        "first.csv": _row("known-a", "first.csv", kind=kind),
        "later.csv": _row("novel-c" if novel else "known-b", "later.csv", kind=kind),
    }
    for name in rows:
        (options["src"] / name).write_text("INVENTED CSV\n")
    monkeypatch.setattr(pipeline, "parse_file", lambda _c, _r, path, *_a, **_k: [rows[path.name]])
    before = db.read_bytes()
    before_rows = _financial_rows(db)
    if novel:
        _assert_ambiguity(lambda: build_commands.build(**options, db_path=db))
        assert "Processing Results" not in capsys.readouterr().out
    else:
        build_commands.build(**options, db_path=db)
        assert "Processing Results" in capsys.readouterr().out
    if dry_run or novel:
        assert db.read_bytes() == before
        assert not options["out_dir"].exists()
    else:
        after_rows = _financial_rows(db)
        assert after_rows["transactions"] == before_rows["transactions"]
        assert after_rows["transfers"] == before_rows["transfers"]
        assert len(after_rows["import_runs"]) == len(before_rows["import_runs"]) + 1


@pytest.mark.parametrize("target", ["none", "new", "empty"])
@pytest.mark.parametrize("ambiguous", [False, True])
def test_build_dry_run_keeps_missing_and_empty_targets_unmodified(
    tmp_path, monkeypatch, capsys, target, ambiguous
):
    options = _build_options(tmp_path)
    db = None if target == "none" else tmp_path / "new-runtime" / "synthetic.db"
    if target == "empty":
        db.parent.mkdir(mode=0o700)
        init_database(db)
    before = db.read_bytes() if db is not None and db.exists() else None
    rows = {"first.csv": _row("first-id", "first.csv")}
    if ambiguous:
        rows["later.csv"] = _row("novel-id", "later.csv")
    for name in rows:
        (options["src"] / name).write_text("INVENTED CSV\n")
    monkeypatch.setattr(pipeline, "parse_file", lambda _c, _r, path, *_a, **_k: [rows[path.name]])
    if ambiguous:
        _assert_ambiguity(lambda: build_commands.build(**options, db_path=db))
        assert "Processing Results" not in capsys.readouterr().out
    else:
        build_commands.build(**options, db_path=db)
    if db is not None:
        if before is None:
            assert not db.exists() and not db.parent.exists()
        else:
            assert db.read_bytes() == before
    assert not options["out_dir"].exists()


def test_database_build_writer_rechecks_identity_after_read_only_preflight(tmp_path, monkeypatch):
    from ledger.config import load_app_config
    from ledger.config.repository import SQLiteConfigRepository

    options = _build_options(tmp_path)
    options["dry_run"] = False
    db = tmp_path / "synthetic.db"
    config = load_app_config(
        options["ledger_config"], options["merchant_rules"], options["categories"]
    )
    SQLiteConfigRepository(db).replace_from_configs(config.merchants, config.categories)
    file = options["src"] / "incoming.csv"
    file.write_text("INVENTED CSV\n")
    incoming = _row("incoming-id", file.name)
    monkeypatch.setattr(pipeline, "parse_file", lambda *_a, **_k: [incoming])
    summarize = build_commands.compute_summary
    inserted = {}

    def summary_after_concurrent_write(**kwargs):
        # The preflight saw an empty ledger. An independent write now adds the
        # conflicting fact before build opens its own atomic writer transaction.
        DatabaseWriter(db).write_result(_result(_row("other-id", "other.csv")), tmp_path, "test")
        inserted.update(_financial_rows(db))
        return summarize(**kwargs)

    monkeypatch.setattr(build_commands, "compute_summary", summary_after_concurrent_write)
    _assert_ambiguity(lambda: build_commands.build(**options, db_path=db))
    assert inserted and _financial_rows(db) == inserted
    assert not options["out_dir"].exists()


@pytest.mark.parametrize("dry_run", [False, True])
def test_build_keeps_writable_wal_support_and_guarded_dry_run_refusal(
    tmp_path, monkeypatch, dry_run
):
    from ledger.config import load_app_config
    from ledger.config.repository import SQLiteConfigRepository
    from ledger.storage.read_only import ReadOnlyUnavailableError

    options = _build_options(tmp_path)
    options["dry_run"] = dry_run
    db = tmp_path / "synthetic.db"
    config = load_app_config(
        options["ledger_config"], options["merchant_rules"], options["categories"]
    )
    SQLiteConfigRepository(db).replace_from_configs(config.merchants, config.categories)
    with closing(sqlite3.connect(db)) as connection, connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    before = {path.name: path.read_bytes() for path in tmp_path.glob("synthetic.db*")}
    file = options["src"] / "incoming.csv"
    file.write_text("INVENTED CSV\n")
    monkeypatch.setattr(pipeline, "parse_file", lambda *_a, **_k: [_row("single-id", file.name)])
    if dry_run:
        with pytest.raises(ReadOnlyUnavailableError):
            build_commands.build(**options, db_path=db)
        assert {path.name: path.read_bytes() for path in tmp_path.glob("synthetic.db*")} == before
        assert not options["out_dir"].exists()
    else:
        build_commands.build(**options, db_path=db)
        with session_scope(db) as session:
            assert {row.txn_id for row in session.query(TransactionDB)} == {"single-id"}
        with closing(sqlite3.connect(db)) as connection, connection:
            assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)


@pytest.mark.parametrize("persisted", [False, True])
@pytest.mark.parametrize("entrypoint", ["writer", "build"])
def test_nonstrict_collision_cannot_hide_original_cross_source_ambiguity(
    tmp_path, monkeypatch, sample_app_config, capsys, persisted, entrypoint
):
    from ledger.config import load_app_config
    from ledger.config.repository import SQLiteConfigRepository

    options = _build_options(tmp_path)
    options["dry_run"] = False
    db = tmp_path / "synthetic.db"
    config = load_app_config(
        options["ledger_config"], options["merchant_rules"], options["categories"]
    )
    SQLiteConfigRepository(db).replace_from_configs(config.merchants, config.categories)
    first = _row("collision-id", "a.csv", txn_fingerprint="fp-f", description="FINANCIAL F")
    different = _row("collision-id", "b.csv", txn_fingerprint="fp-g", description="FINANCIAL G")
    duplicate = _row("other-id", "c.csv", txn_fingerprint="fp-f", description="FINANCIAL F")
    rows = [first, different]
    if persisted:
        write_result_to_db(db, tmp_path, _result(duplicate))
    else:
        rows.append(duplicate)
    parsed = {row.source_path: row for row in rows}
    for name in parsed:
        (options["src"] / name).write_text("INVENTED CSV\n")
    monkeypatch.setattr(pipeline, "parse_file", lambda _c, _r, path, *_a, **_k: [parsed[path.name]])
    before = _financial_rows(db)
    if entrypoint == "writer":
        result = pipeline.process(
            sample_app_config,
            options["src"],
            defer_identity_check_to_writer=True,
        )
        _assert_ambiguity(lambda: write_result_to_db(db, options["src"], result))
    else:
        _assert_ambiguity(lambda: build_commands.build(**options, db_path=db))
        assert "Processing Results" not in capsys.readouterr().out
    assert _financial_rows(db) == before
    assert not options["out_dir"].exists()


@pytest.mark.parametrize("after_method", ["has_runtime_config", "load_app_config", "validate"])
def test_build_dry_run_config_reads_cannot_recover_a_new_hot_journal(
    tmp_path, monkeypatch, capsys, after_method
):
    from ledger.config import load_app_config
    from ledger.config.repository import SQLiteConfigRepository
    from ledger.storage.database import clear_engine_cache
    from ledger.storage.read_only import ReadOnlyDatabaseError

    options = _build_options(tmp_path)
    db = tmp_path / "synthetic.db"
    config = load_app_config(
        options["ledger_config"], options["merchant_rules"], options["categories"]
    )
    SQLiteConfigRepository(db).replace_from_configs(config.merchants, config.categories)
    with closing(sqlite3.connect(db)) as connection, connection:
        connection.execute("CREATE TABLE synthetic_padding (id INTEGER PRIMARY KEY, payload TEXT)")
        connection.executemany(
            "INSERT INTO synthetic_padding (payload) VALUES (?)", [("a" * 5000,)] * 20
        )
    clear_engine_cache()
    file = options["src"] / "incoming.csv"
    file.write_text("INVENTED CSV\n")
    monkeypatch.setattr(pipeline, "parse_file", lambda *_a, **_k: [_row("single-id", file.name)])
    original = getattr(SQLiteConfigRepository, after_method)
    crashed = {}
    crash_code = """import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1])
connection.execute("PRAGMA cache_size=1")
connection.execute("PRAGMA cache_spill=ON")
connection.execute("BEGIN IMMEDIATE")
connection.execute("UPDATE synthetic_padding SET payload = ?", ("b" * 5000,))
os._exit(73)
"""

    def read_then_crash(self, *args, **kwargs):
        answer = original(self, *args, **kwargs)
        if not crashed:
            completed = subprocess.run(
                [sys.executable, "-B", "-c", crash_code, str(db)],
                capture_output=True,
                timeout=20,
            )
            assert completed.returncode == 73, completed.stderr
            journal = Path(str(db) + "-journal")
            assert journal.is_file() and journal.read_bytes()[:8] != b"\0" * 8
            crashed.update(
                {path.name: path.read_bytes() for path in tmp_path.glob("synthetic.db*")}
            )
        return answer

    monkeypatch.setattr(SQLiteConfigRepository, after_method, read_then_crash)
    with pytest.raises(ReadOnlyDatabaseError):
        build_commands.build(**options, db_path=db)
    assert crashed
    assert {path.name: path.read_bytes() for path in tmp_path.glob("synthetic.db*")} == crashed
    assert "Processing Results" not in capsys.readouterr().out
    assert not options["out_dir"].exists()


def test_original_collision_identity_preserves_proven_multiplicity_and_known_repeats(
    tmp_path, monkeypatch, sample_app_config
):
    files = [tmp_path / name for name in ("together.csv", "different.csv")]
    for file in files:
        file.write_text("INVENTED CSV\n")
    parsed = {
        files[0]: [
            _row("collision-id", files[0].name, txn_fingerprint="fp-f", description="F"),
            _row("distinct-id", files[0].name, txn_fingerprint="fp-f", description="F"),
        ],
        files[1]: [_row("collision-id", files[1].name, txn_fingerprint="fp-g", description="G")],
    }
    monkeypatch.setattr(pipeline, "parse_file", lambda _c, _r, path, *_a, **_k: parsed[path])
    result = pipeline.process(
        sample_app_config, tmp_path, source_files=files, defer_identity_check_to_writer=True
    )
    db = tmp_path / "synthetic.db"
    write_result_to_db(db, tmp_path, result)
    before = _financial_rows(db)
    result.transactions = [
        row.model_copy(update={"source_path": "later-" + row.source_path})
        for row in result.transactions
    ]
    # This result no longer carries valid source witnesses. Its already-admitted
    # final IDs alone must keep the repeat idempotent.
    result.identity_evidence = ()
    write_result_to_db(db, tmp_path, result)
    after = _financial_rows(db)
    assert after["transactions"] == before["transactions"]
    with session_scope(db) as session:
        assert session.query(TransactionDB).count() == 3
        assert session.query(TransactionDB).filter_by(description="F").count() == 2


def test_guarded_config_repository_preserves_runtime_semantics_and_refuses_writes(
    tmp_path, monkeypatch
):
    from ledger.config import load_app_config
    from ledger.config.repository import SQLiteConfigRepository
    from ledger.core.exceptions import ConfigError
    from ledger.storage.read_only import ReadOnlyDatabase

    options = _build_options(tmp_path)
    db = tmp_path / "synthetic.db"
    toml = load_app_config(
        options["ledger_config"], options["merchant_rules"], options["categories"]
    )
    runtime_merchants = toml.merchants.model_copy(
        update={
            "rules": [
                toml.merchants.rules[0].model_copy(
                    update={"merchant_norm": "Runtime-only merchant"}
                )
            ]
        }
    )
    SQLiteConfigRepository(db).replace_from_configs(runtime_merchants, toml.categories)
    before = db.read_bytes()
    guarded = ReadOnlyDatabase(db)
    try:
        repository = SQLiteConfigRepository(db, read_only_database=guarded)
        assert repository.has_runtime_config() and repository.validate().ok
        assert repository.load_app_config(toml.ledger).merchants == runtime_merchants
        with pytest.raises(ConfigError, match="cannot write"):
            repository.replace_from_configs(toml.merchants, toml.categories)
        with pytest.raises(ValueError, match="does not match"):
            SQLiteConfigRepository(tmp_path / "other.db", read_only_database=guarded)
    finally:
        guarded.close()
    file = options["src"] / "incoming.csv"
    file.write_text("INVENTED CSV\n")
    observed = []

    def parse(config, *_args, **_kwargs):
        observed.append(config.merchants)
        return [_row("single-id", file.name)]

    monkeypatch.setattr(pipeline, "parse_file", parse)
    build_commands.build(**options, db_path=db)
    assert observed == [runtime_merchants]
    assert db.read_bytes() == before
    assert not (tmp_path / "other.db").exists()


def test_build_dry_run_refuses_a_target_created_after_configuration_selection(
    tmp_path, monkeypatch, capsys
):
    from ledger.storage.read_only import DatabaseChangedError

    options = _build_options(tmp_path)
    db = tmp_path / "new.db"
    file = options["src"] / "incoming.csv"
    file.write_text("INVENTED CSV\n")
    appeared = {}

    def parse(*_args, **_kwargs):
        init_database(db)
        appeared["bytes"] = db.read_bytes()
        return [_row("single-id", file.name)]

    monkeypatch.setattr(pipeline, "parse_file", parse)
    with pytest.raises(DatabaseChangedError):
        build_commands.build(**options, db_path=db)
    assert appeared and db.read_bytes() == appeared["bytes"]
    assert "Processing Results" not in capsys.readouterr().out
    assert not options["out_dir"].exists()


@pytest.mark.parametrize("collision_first", [False, True])
def test_sequential_build_cannot_repeat_financial_fact_after_collision_rekey(
    tmp_path, monkeypatch, capsys, collision_first
):
    from ledger.config import load_app_config
    from ledger.config.repository import SQLiteConfigRepository

    options = _build_options(tmp_path)
    options["dry_run"] = False
    db = tmp_path / "synthetic.db"
    config = load_app_config(
        options["ledger_config"], options["merchant_rules"], options["categories"]
    )
    SQLiteConfigRepository(db).replace_from_configs(config.merchants, config.categories)
    collided = [
        _row("X", "a.csv", txn_fingerprint="fp-f", description="FINANCIAL F"),
        _row("X", "b.csv", txn_fingerprint="fp-g", description="FINANCIAL G"),
    ]
    plain = [_row("Y", "c.csv", txn_fingerprint="fp-f", description="FINANCIAL F")]
    first, second = (collided, plain) if collision_first else (plain, collided)
    parsed = {row.source_path: row for row in first}
    for name in parsed:
        (options["src"] / name).write_text("INVENTED CSV\n")
    monkeypatch.setattr(pipeline, "parse_file", lambda _c, _r, path, *_a, **_k: [parsed[path.name]])
    build_commands.build(**options, db_path=db)
    with session_scope(db) as session:
        saved = session.query(TransactionDB).all()
        expected, _, _ = deduplicate(first, strict=False)
        assert {(row.txn_id, row.txn_fingerprint) for row in saved} == {
            (row.txn_id, row.txn_fingerprint) for row in expected
        }
    before = _financial_rows(db)
    capsys.readouterr()
    options["src"] = tmp_path / "second-source"
    options["src"].mkdir()
    options["out_dir"] = tmp_path / "second-exports"
    parsed = {row.source_path: row for row in second}
    for name in parsed:
        (options["src"] / name).write_text("INVENTED CSV\n")
    _assert_ambiguity(lambda: build_commands.build(**options, db_path=db))
    assert _financial_rows(db) == before
    assert "Processing Results" not in capsys.readouterr().out
    assert not options["out_dir"].exists()
    with session_scope(db) as session:
        assert session.query(TransactionDB).filter_by(description="FINANCIAL F").count() == 1


@pytest.mark.parametrize("mode", ["batch", "sequential"])
@pytest.mark.parametrize("reverse", [False, True])
def test_different_fingerprints_cannot_prove_distinct_financial_facts(tmp_path, mode, reverse):
    rows = [
        _row("first-id", "first.csv", txn_fingerprint="fp-one"),
        _row("second-id", "second.csv", txn_fingerprint="fp-two"),
    ]
    if reverse:
        rows.reverse()
    original = [row.model_dump() for row in rows]
    if mode == "batch":
        _assert_ambiguity(lambda: deduplicate(rows, strict=False))
    else:
        db = tmp_path / "synthetic.db"
        write_result_to_db(db, tmp_path, _result(rows[0]))
        before = _financial_rows(db)
        _assert_ambiguity(lambda: write_result_to_db(db, tmp_path, _result(rows[1])))
        assert _financial_rows(db) == before
    assert [row.model_dump() for row in rows] == original


@pytest.mark.parametrize("stale", [False, True])
def test_financial_comparison_keeps_exact_final_fingerprint_witness_binding(tmp_path, stale):
    first = _row("first-id", "first.csv", txn_fingerprint="fp-one")
    second = _row("second-id", "second.csv", txn_fingerprint="fp-two")
    witness = first.model_copy(
        update={"source_path": "second.csv", "txn_fingerprint": "stale-fp" if stale else "fp-one"}
    )
    result = _result(first, second)
    result.identity_evidence = (transaction_identity(witness), transaction_identity(second))
    db = tmp_path / "synthetic.db"
    writer = DatabaseWriter(db)
    before = _financial_rows(db)
    if stale:
        _assert_ambiguity(lambda: writer.write_result(result, tmp_path, "test"))
        assert _financial_rows(db) == before
    else:
        writer.write_result(result, tmp_path, "test")
        with session_scope(db) as session:
            assert {(row.txn_id, row.txn_fingerprint) for row in session.query(TransactionDB)} == {
                ("first-id", "fp-one"),
                ("second-id", "fp-two"),
            }


@pytest.mark.parametrize("with_sink", [False, True])
def test_source_only_collision_cannot_duplicate_the_same_financial_fact(
    tmp_path, monkeypatch, sample_app_config, with_sink
):
    rows = [
        _row("shared-id", "first.csv", txn_fingerprint="fp-one"),
        _row("shared-id", "second.csv", txn_fingerprint="fp-two"),
    ]
    evidence = [] if with_sink else None
    _assert_ambiguity(lambda: deduplicate(rows, strict=False, identity_evidence=evidence))
    for row in rows:
        (tmp_path / row.source_path).write_text("INVENTED CSV\n")
    parsed = {row.source_path: row for row in rows}
    monkeypatch.setattr(pipeline, "parse_file", lambda _c, _r, path, *_a, **_k: [parsed[path.name]])
    _assert_ambiguity(lambda: pipeline.process(sample_app_config, tmp_path))


@pytest.mark.parametrize("with_sink", [False, True])
@pytest.mark.parametrize("overlap", [False, True])
def test_source_only_collision_retains_real_same_source_multiplicity(with_sink, overlap):
    first = _row("X", "first.csv", txn_fingerprint="fp-f", description="F")
    if overlap:
        rows = [
            first,
            first.model_copy(update={"source_path": "together.csv"}),
            _row("Y", "together.csv", txn_fingerprint="fp-y", description="F"),
            _row("X", "different.csv", txn_fingerprint="fp-g", description="G"),
        ]
    else:
        rows = [first, first.model_copy(update={"txn_fingerprint": "fp-other"})]
    evidence = [] if with_sink else None
    output = deduplicate(rows, strict=False, identity_evidence=evidence)
    assert len(output) == 3
    unique, duplicates, collisions = output
    assert len(unique) == (3 if overlap else 2)
    assert duplicates == int(overlap) and collisions == 1
    assert sum(row.description == "F" for row in unique) == 2
    assert len({row.txn_id for row in unique}) == len(unique)
    if overlap:
        assert (
            next(row for row in unique if row.txn_id != "Y" and row.description == "F").source_path
            == "first.csv"
        )
    if evidence is not None:
        exact_bindings = {
            (row.txn_id, transaction_identity(row).financial_identity, row.txn_fingerprint)
            for row in unique
        }
        assert len(evidence) == len(rows)
        assert all(
            (witness.txn_id, witness.financial_identity, witness.txn_fingerprint) in exact_bindings
            for witness in evidence
        )
