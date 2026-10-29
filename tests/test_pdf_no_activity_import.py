"""Fictional complete statements with no activity use the normal import protocol."""

from __future__ import annotations

import calendar
import json
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from ledger.adapters import registry
from ledger.adapters.contracts import PdfFacts, PdfFormat
from ledger.imports.pdf import statement_pdf_to_csv, workflow
from ledger.imports.pdf.paths import Paths
from ledger.imports.pdf.statement_pdf_to_csv import _dispatch
from ledger.processing import process
from ledger.storage.database import (
    ProcessedFileDB,
    StatementCoverageDB,
    StatementCoverageIntervalDB,
    TransactionDB,
    TransferDB,
    init_database,
    session_scope,
)

HEADERS = (
    "Ledger Format",
    "Date",
    "Description",
    "Amount",
    "Currency",
    "Reference",
    "Institution",
    "Account Type",
)


@pytest.fixture
def no_activity_import(tmp_path, monkeypatch, sample_app_config):
    paths = Paths(
        pdf_dir=tmp_path / "inbox",
        processed_dir=tmp_path / "processed",
        db_path=tmp_path / "ledger.db",
        base_db_path=None,
        backup_dir=tmp_path / "backups",
        ledger_config=tmp_path / "ledger.toml",
        merchant_rules=tmp_path / "merchants.toml",
        categories=tmp_path / "categories.toml",
        runs_dir=tmp_path / "runs",
    )
    paths.pdf_dir.mkdir()
    paths.processed_dir.mkdir()
    init_database(paths.db_path)
    baseline = tmp_path / "baseline" / "2033-12" / "harbor-savings-1001.csv"
    baseline.parent.mkdir(parents=True)
    baseline.write_text(
        ",".join(HEADERS)
        + "\nledger-mapped-v1,2033-12-02,SYNTHETIC BASELINE,42.00,CAD,,harbor,Savings\n"
    )
    result = process(sample_app_config, tmp_path / "baseline", source_files=[baseline])
    workflow.write_result_to_db(paths.db_path, tmp_path / "baseline", result)

    def facts_for(text, _name):
        month = int(text.rsplit(" ", 1)[1])
        end = date(2034, month, calendar.monthrange(2034, month)[1])
        return PdfFacts(
            rows=(),
            period=end,
            account_type="Savings",
            account_number="1001",
            account_slug="harbor-savings-1001",
            complete=True,
            printed_interval=(date(2034, month, 1), end),
            summary={"opening_balance": "42.00", "closing_balance": "42.00"},
        )

    fmt = PdfFormat(
        "harbor_deposit",
        "harbor",
        HEADERS,
        lambda _: True,
        facts_for,
        lambda *_: "harbor-savings-1001",
        archive_kind="deposit",
    )
    monkeypatch.setattr(statement_pdf_to_csv, "run_pdftotext_bytes", lambda b: b.decode())
    monkeypatch.setattr(_dispatch, "detect_statement_type", lambda _: fmt.format_id)
    monkeypatch.setattr(_dispatch, "get_pdf_format", lambda _: fmt)
    monkeypatch.setattr(registry, "get_pdf_format", lambda _: fmt)
    monkeypatch.setattr(workflow, "load_config", lambda *_: sample_app_config)
    for month in (1, 2, 3):
        (paths.pdf_dir / f"fictional-{month}.pdf").write_bytes(
            f"Fictional Harbor no-activity statement {month}".encode()
        )
    return paths, fmt


def _stage(paths):
    workflow.stage(SimpleNamespace(), paths)
    run_dir = max(path for path in paths.runs_dir.iterdir() if path.is_dir())
    return run_dir, json.loads((run_dir / "run.json").read_text())


def test_no_activity_batch_finalizes_coverage_archives_and_deduplicates(no_activity_import):
    paths, _ = no_activity_import
    before = paths.db_path.read_bytes()
    originals = {p.name: p.read_bytes() for p in paths.pdf_dir.glob("*.pdf")}
    run_dir, staged = _stage(paths)
    assert staged["status"] == "ready_to_finalize"
    assert paths.db_path.read_bytes() == before
    assert staged["stage_counts"]["rows_scanned"] == 0
    assert staged["pending_review_count"] == 0

    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)
    final = json.loads((run_dir / "run.json").read_text())
    assert final["status"] == "finalized"
    with session_scope(paths.db_path) as session:
        assert session.query(TransactionDB).count() == 1
        assert session.query(TransactionDB).one().description == "SYNTHETIC BASELINE"
        assert session.query(TransferDB).count() == 0
        rows = (
            session.query(StatementCoverageDB)
            .filter(StatementCoverageDB.period >= "2034-01")
            .order_by(StatementCoverageDB.period)
            .all()
        )
        assert [r.period for r in rows] == ["2034-01", "2034-02", "2034-03"]
        assert all(r.row_count == 0 and r.statement_count == 1 for r in rows)
        assert all(r.skipped_reason == "no_transactions_extracted" for r in rows)
        intervals = session.query(StatementCoverageIntervalDB).all()
        assert {(r.start_date, r.end_date) for r in intervals} == {
            (date(2034, m, 1), date(2034, m, calendar.monthrange(2034, m)[1])) for m in (1, 2, 3)
        }
        assert (
            session.query(ProcessedFileDB).filter(ProcessedFileDB.transaction_count == 0).count()
            == 3
        )
    assert len(list(paths.backup_dir.glob("*.db"))) == 1
    assert not list(paths.pdf_dir.glob("*.pdf"))
    assert not Path(staged["csv_root"]).exists()
    assert not Path(staged["staging_inbox"]).exists()
    assert {p.read_bytes() for p in paths.processed_dir.glob("*.pdf")} == set(originals.values())

    # A retry of the finalized run must not publish a second time.
    after = paths.db_path.read_bytes()
    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)
    assert paths.db_path.read_bytes() == after
    for name, content in originals.items():
        (paths.pdf_dir / name).write_bytes(content)
    _, duplicate = _stage(paths)
    assert duplicate["status"] == "cancelled"
    assert duplicate["cancel_reason"] == "no_new_pdfs"
    assert len(duplicate["duplicate_pdfs"]) == 3


@pytest.mark.parametrize("invalid", ["incomplete", "no_period", "movement", "errors"])
def test_empty_statement_without_complete_valid_evidence_is_not_published(
    no_activity_import, monkeypatch, invalid
):
    paths, fmt = no_activity_import
    original = fmt.parse

    def invalid_facts(text, name):
        facts = original(text, name)
        changes = {
            "incomplete": {"complete": False},
            "no_period": {"printed_interval": None},
            "movement": {"summary": {"total_deposits": "10.00"}},
            "errors": {"errors": ("missing source rows",)},
        }
        return replace(facts, **changes[invalid])

    invalid_fmt = replace(fmt, parse=invalid_facts)
    monkeypatch.setattr(_dispatch, "get_pdf_format", lambda _: invalid_fmt)
    before = paths.db_path.read_bytes()
    _, state = _stage(paths)
    assert state["status"] in {"failed", "cancelled"}
    assert paths.db_path.read_bytes() == before
    assert not list(paths.processed_dir.glob("*.pdf"))
    with session_scope(paths.db_path) as session:
        assert session.query(StatementCoverageDB).count() == 0


def test_no_activity_sources_remain_bound_before_finalize(no_activity_import):
    paths, _ = no_activity_import
    before = paths.db_path.read_bytes()
    run_dir, staged = _stage(paths)
    assert staged["status"] == "ready_to_finalize"
    Path(staged["extraction_manifest"]).write_text("[]")
    with pytest.raises(ValueError, match="import_binding_changed"):
        workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)
    assert paths.db_path.read_bytes() == before
    assert len(list(paths.pdf_dir.glob("*.pdf"))) == 3


def test_no_activity_batch_can_be_the_first_import(no_activity_import):
    paths, _ = no_activity_import
    paths = replace(paths, db_path=paths.db_path.with_name("empty-ledger.db"))
    init_database(paths.db_path)
    run_dir, staged = _stage(paths)
    assert staged["status"] == "ready_to_finalize"
    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)
    with session_scope(paths.db_path) as session:
        assert session.query(TransactionDB).count() == 0
        assert session.query(TransferDB).count() == 0
        assert session.query(StatementCoverageDB).count() == 3
        assert session.query(ProcessedFileDB).count() == 3
    assert json.loads((run_dir / "run.json").read_text())["status"] == "finalized"


def test_no_activity_batch_recovers_archive_failure_without_republication(
    no_activity_import, monkeypatch
):
    paths, _ = no_activity_import
    run_dir, staged = _stage(paths)
    assert staged["status"] == "ready_to_finalize"
    move = statement_pdf_to_csv.move_pdf

    def fail_move(*_args, **_kwargs):
        raise OSError("synthetic archive interruption")

    monkeypatch.setattr(statement_pdf_to_csv, "move_pdf", fail_move)
    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)
    published = paths.db_path.read_bytes()
    assert len(list(paths.pdf_dir.glob("*.pdf"))) == 3
    assert not list(paths.processed_dir.glob("*.pdf"))
    monkeypatch.setattr(statement_pdf_to_csv, "move_pdf", move)
    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)
    assert paths.db_path.read_bytes() == published
    assert json.loads((run_dir / "run.json").read_text())["status"] == "finalized"
    assert len(list(paths.processed_dir.glob("*.pdf"))) == 3


def test_no_activity_and_active_statements_share_the_normal_batch(no_activity_import, monkeypatch):
    paths, fmt = no_activity_import
    original = fmt.parse

    def mixed_facts(text, name):
        facts = original(text, name)
        if facts.period.month == 3:
            row = dict(
                zip(
                    HEADERS,
                    (
                        "ledger-mapped-v1",
                        "2034-03-04",
                        "Costco",
                        "-5.00",
                        "CAD",
                        "",
                        "harbor",
                        "Savings",
                    ),
                    strict=True,
                )
            )
            return replace(facts, rows=(row,))
        return facts

    monkeypatch.setattr(_dispatch, "get_pdf_format", lambda _: replace(fmt, parse=mixed_facts))
    run_dir, staged = _stage(paths)
    assert staged["status"] == "ready_to_finalize"
    assert staged["stage_counts"]["rows_scanned"] == 1
    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)
    with session_scope(paths.db_path) as session:
        assert session.query(TransactionDB).count() == 2
        assert (
            session.query(ProcessedFileDB).filter(ProcessedFileDB.transaction_count == 0).count()
            == 2
        )
    assert len(list(paths.processed_dir.glob("*.pdf"))) == 3


@pytest.mark.parametrize("invalid", ["incomplete", "no_period", "movement", "errors"])
@pytest.mark.parametrize("existing", [False, True])
def test_invalid_empty_sibling_cannot_publish_or_replace_coverage(
    no_activity_import, monkeypatch, invalid, existing
):
    paths, fmt = no_activity_import
    if existing:
        with session_scope(paths.db_path) as session:
            session.add(
                StatementCoverageDB(
                    account_slug="harbor-savings-1001",
                    period="2034-02",
                    institution="harbor",
                    statement_type="harbor_deposit",
                    row_count=7,
                    statement_count=1,
                    skipped_reason="",
                    imported_at=datetime(2034, 3, 1),
                )
            )
            session.add(
                StatementCoverageIntervalDB(
                    interval_id="synthetic-prior-interval",
                    institution="harbor",
                    account_slug="harbor-savings-1001",
                    start_date=date(2034, 2, 1),
                    end_date=date(2034, 2, 25),
                    evidence_basis="printed_statement_period",
                    imported_at=datetime(2034, 3, 1),
                )
            )
    original = fmt.parse

    def mixed_facts(text, name):
        facts = original(text, name)
        if facts.period.month != 2:
            return facts
        changes = {
            "incomplete": {"complete": False},
            "no_period": {"printed_interval": None},
            "movement": {"summary": {"total_deposits": "10.00"}},
            "errors": {"errors": ("missing source rows",)},
        }
        return replace(facts, **changes[invalid])

    monkeypatch.setattr(_dispatch, "get_pdf_format", lambda _: replace(fmt, parse=mixed_facts))
    run_dir, staged = _stage(paths)
    assert staged["status"] == "ready_to_finalize"
    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)
    with session_scope(paths.db_path) as session:
        row = session.get(StatementCoverageDB, ("harbor-savings-1001", "2034-02"))
        if existing:
            assert row.row_count == 7
            assert row.skipped_reason == ""
            assert row.imported_at == datetime(2034, 3, 1)
            prior = session.get(StatementCoverageIntervalDB, "synthetic-prior-interval")
            assert prior.end_date == date(2034, 2, 25)
        else:
            assert row is None
        assert (
            session.query(ProcessedFileDB).filter(ProcessedFileDB.transaction_count == 0).count()
            == 2
        )
    assert (paths.pdf_dir / "fictional-2.pdf").is_file()
    assert len(list(paths.processed_dir.glob("*.pdf"))) == 2
