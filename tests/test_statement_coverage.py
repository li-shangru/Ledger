"""statement_coverage: the account-months for which a statement was imported.

A month with no transaction rows is indistinguishable from a month never imported,
so this table records EVERY statement the extractor saw -- including the zero-row
ones finalize otherwise drops. Two things are load-bearing and both are tested here:
the aggregation rules, and that BOTH writers (PDF finalize and statement repair)
populate it. A table written only at finalize is silently emptied by every
`repair statements`, which is exactly how the investment domain was destroyed
before 1.2.0.
"""

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from import_binding_fixtures import bound_import_state

from ledger.config.repository import SQLiteConfigRepository
from ledger.config.schemas import CategoriesConfig, CategoryRule, MerchantRule, MerchantRulesConfig
from ledger.imports.pdf import workflow
from ledger.imports.pdf.paths import Paths
from ledger.imports.pdf.post_processing import persist_statement_coverage
from ledger.imports.pdf.readiness import database_fingerprint, import_owned_fingerprint
from ledger.processing import ProcessingResult
from ledger.repair.statements import StatementRepairOptions, StatementRepairService
from ledger.storage.database import (
    ProcessedFileDB,
    StatementCoverageDB,
    init_database,
    session_scope,
)


def _write_manifest(path: Path, entries: list[dict]) -> None:
    path.write_text(json.dumps(entries), encoding="utf-8")


def _coverage_rows(db_path: Path) -> list[StatementCoverageDB]:
    with session_scope(db_path) as session:
        rows = (
            session.query(StatementCoverageDB)
            .order_by(StatementCoverageDB.account_slug, StatementCoverageDB.period)
            .all()
        )
        # Detach plain values: the session closes when the scope exits.
        return [
            SimpleNamespace(
                account_slug=row.account_slug,
                period=row.period,
                institution=row.institution,
                statement_type=row.statement_type,
                row_count=row.row_count,
                statement_count=row.statement_count,
                skipped_reason=row.skipped_reason,
                imported_at=row.imported_at,
            )
            for row in rows
        ]


# ---------------------------------------------------------------------------
# 1. persist_statement_coverage: what lands, and how entries aggregate
# ---------------------------------------------------------------------------


def test_zero_row_statement_is_recorded_with_its_skipped_reason(tmp_path: Path) -> None:
    """The whole point of the table: a statement that produced no CSV and no rows
    still proves the month was imported. persist_printed_credit_limits skips these
    (no csv_path); this must NOT."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    manifest = tmp_path / "extraction-manifest.json"
    _write_manifest(
        manifest,
        [
            {
                "csv_path": "",
                "statement_type": "harbor_deposit",
                "transaction_count": 0,
                "skipped_reason": "no_transactions_extracted",
                "account_slug": "harbor-chequing-1001",
                "period": "2026-04",
            }
        ],
    )

    written = persist_statement_coverage(db_path, manifest)

    assert written == 1
    [row] = _coverage_rows(db_path)
    assert (row.account_slug, row.period) == ("harbor-chequing-1001", "2026-04")
    assert row.row_count == 0
    assert row.statement_count == 1
    assert row.skipped_reason == "no_transactions_extracted"
    assert row.institution == "harbor"


def test_normal_statement_is_recorded_with_rows_and_no_skipped_reason(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    manifest = tmp_path / "extraction-manifest.json"
    _write_manifest(
        manifest,
        [
            {
                "csv_path": str(tmp_path / "csv" / "2026-04" / "harbor-chequing-1001.csv"),
                "statement_type": "harbor_deposit",
                "transaction_count": 42,
                "skipped_reason": "",
                "account_slug": "harbor-chequing-1001",
                "period": "2026-04",
            }
        ],
    )

    written = persist_statement_coverage(db_path, manifest)

    assert written == 1
    [row] = _coverage_rows(db_path)
    assert row.row_count == 42
    assert row.statement_count == 1
    assert row.skipped_reason == ""
    assert row.statement_type == "harbor_deposit"


def test_two_statements_for_one_account_month_aggregate_into_one_row(tmp_path: Path) -> None:
    """A month can be covered by more than one PDF (a reissued statement, or a card
    that splits its cycle). They must collapse onto the single (slug, period) key
    with the row counts summed rather than the second silently replacing the first."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    manifest = tmp_path / "extraction-manifest.json"
    _write_manifest(
        manifest,
        [
            {
                "csv_path": str(tmp_path / "csv" / "2026-04" / "harbor-chequing-1001.csv"),
                "statement_type": "harbor_deposit",
                "transaction_count": 10,
                "skipped_reason": "",
                "account_slug": "harbor-chequing-1001",
                "period": "2026-04",
            },
            {
                "csv_path": str(tmp_path / "csv" / "2026-04" / "harbor-chequing-1001-2.csv"),
                "statement_type": "harbor_deposit",
                "transaction_count": 5,
                "skipped_reason": "",
                "account_slug": "harbor-chequing-1001",
                "period": "2026-04",
            },
        ],
    )

    written = persist_statement_coverage(db_path, manifest)

    assert written == 1
    [row] = _coverage_rows(db_path)
    assert row.row_count == 15
    assert row.statement_count == 2


def test_a_skipped_reason_does_not_survive_a_sibling_statement_that_had_rows(
    tmp_path: Path,
) -> None:
    """The month IS covered once any statement for it produced rows. Keeping the
    skipped duplicate's reason would report a genuinely-imported month as skipped."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    manifest = tmp_path / "extraction-manifest.json"
    _write_manifest(
        manifest,
        [
            {
                "csv_path": "",
                "statement_type": "harbor_deposit",
                "transaction_count": 0,
                "skipped_reason": "no_transactions_extracted",
                "account_slug": "harbor-chequing-1001",
                "period": "2026-04",
            },
            {
                "csv_path": str(tmp_path / "csv" / "2026-04" / "harbor-chequing-1001.csv"),
                "statement_type": "harbor_deposit",
                "transaction_count": 7,
                "skipped_reason": "",
                "account_slug": "harbor-chequing-1001",
                "period": "2026-04",
            },
        ],
    )

    persist_statement_coverage(db_path, manifest)

    [row] = _coverage_rows(db_path)
    assert row.row_count == 7
    assert row.statement_count == 2
    assert row.skipped_reason == ""


def test_re_running_the_same_manifest_updates_in_place(tmp_path: Path) -> None:
    """Re-importing a statement must upsert, not accumulate duplicate rows or
    double-count statement_count."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    manifest = tmp_path / "extraction-manifest.json"
    _write_manifest(
        manifest,
        [
            {
                "csv_path": str(tmp_path / "csv" / "2026-04" / "harbor-chequing-1001.csv"),
                "statement_type": "harbor_deposit",
                "transaction_count": 42,
                "skipped_reason": "",
                "account_slug": "harbor-chequing-1001",
                "period": "2026-04",
            }
        ],
    )

    first = persist_statement_coverage(db_path, manifest)
    second = persist_statement_coverage(db_path, manifest)

    assert (first, second) == (1, 1)
    [row] = _coverage_rows(db_path)
    assert row.row_count == 42
    assert row.statement_count == 1


def test_a_corrected_reimport_replaces_the_stored_counts(tmp_path: Path) -> None:
    """An upsert, not an insert-if-absent: a re-extraction that recovers dropped rows
    must overwrite the stale count rather than leaving the old figure in place."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    manifest = tmp_path / "extraction-manifest.json"
    _write_manifest(
        manifest,
        [
            {
                "csv_path": "",
                "statement_type": "harbor_deposit",
                "transaction_count": 0,
                "skipped_reason": "no_transactions_extracted",
                "account_slug": "harbor-chequing-1001",
                "period": "2026-04",
            }
        ],
    )
    persist_statement_coverage(db_path, manifest)

    _write_manifest(
        manifest,
        [
            {
                "csv_path": str(tmp_path / "csv" / "2026-04" / "harbor-chequing-1001.csv"),
                "statement_type": "harbor_deposit",
                "transaction_count": 12,
                "skipped_reason": "",
                "account_slug": "harbor-chequing-1001",
                "period": "2026-04",
            }
        ],
    )
    persist_statement_coverage(db_path, manifest)

    [row] = _coverage_rows(db_path)
    assert row.row_count == 12
    assert row.skipped_reason == ""


def test_distinct_accounts_and_months_stay_separate_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    manifest = tmp_path / "extraction-manifest.json"
    _write_manifest(
        manifest,
        [
            {
                "csv_path": "x.csv",
                "statement_type": "harbor_deposit",
                "transaction_count": 3,
                "account_slug": "harbor-chequing-1001",
                "period": "2026-03",
            },
            {
                "csv_path": "y.csv",
                "statement_type": "harbor_deposit",
                "transaction_count": 4,
                "account_slug": "harbor-chequing-1001",
                "period": "2026-04",
            },
            {
                "csv_path": "z.csv",
                "statement_type": "meadow_mastercard",
                "transaction_count": 5,
                "account_slug": "meadow-mastercard-9012",
                "period": "2026-04",
            },
        ],
    )

    written = persist_statement_coverage(db_path, manifest)

    assert written == 3
    rows = _coverage_rows(db_path)
    assert [(row.account_slug, row.period, row.row_count) for row in rows] == [
        ("harbor-chequing-1001", "2026-03", 3),
        ("harbor-chequing-1001", "2026-04", 4),
        ("meadow-mastercard-9012", "2026-04", 5),
    ]
    assert [row.institution for row in rows] == ["harbor", "harbor", "meadow"]


def test_coverage_key_is_the_aliased_slug_ingestion_wrote(tmp_path: Path) -> None:
    """Same normalization as persist_printed_credit_limits: a configured alias renames
    the slug on TransactionDB.account, so a raw-stem coverage key would name an account
    that holds no rows."""
    from ledger.utils.text import clean_account_name

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    aliases = {"harbor-chequing-1001": "household-chequing"}
    manifest = tmp_path / "extraction-manifest.json"
    _write_manifest(
        manifest,
        [
            {
                "csv_path": "x.csv",
                "statement_type": "harbor_deposit",
                "transaction_count": 3,
                "account_slug": "harbor-chequing-1001",
                "period": "2026-04",
            }
        ],
    )

    persist_statement_coverage(db_path, manifest, account_aliases=aliases)

    [row] = _coverage_rows(db_path)
    assert row.account_slug == clean_account_name("harbor-chequing-1001", aliases)
    assert row.account_slug == "household-chequing"


def test_entries_without_a_slug_or_a_period_are_skipped(tmp_path: Path) -> None:
    """Neither half of the primary key can be guessed. An unsupported/delegated
    statement carries no period at all, and a coverage row keyed on "" would collapse
    every such statement onto one meaningless row."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    manifest = tmp_path / "extraction-manifest.json"
    _write_manifest(
        manifest,
        [
            {
                "csv_path": "",
                "statement_type": "unknown",
                "transaction_count": 0,
                "skipped_reason": "unsupported_statement",
                "account_slug": "",
                "period": "",
            },
            {
                "csv_path": "",
                "statement_type": "harbor_deposit",
                "transaction_count": 0,
                "skipped_reason": "no_transactions_extracted",
                "account_slug": "harbor-chequing-1001",
                "period": "",
            },
            {
                "csv_path": "x.csv",
                "statement_type": "harbor_deposit",
                "transaction_count": 1,
                "account_slug": "",
                "period": "2026-04",
            },
        ],
    )

    assert persist_statement_coverage(db_path, manifest) == 0
    assert _coverage_rows(db_path) == []


def test_an_unknown_statement_type_records_coverage_without_an_institution(
    tmp_path: Path,
) -> None:
    """institution_from_statement_type raises on an unregistered type. Letting that
    escape would abort a whole import over a newly-supported statement kind; the
    account-month is still worth recording."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    manifest = tmp_path / "extraction-manifest.json"
    _write_manifest(
        manifest,
        [
            {
                "csv_path": "x.csv",
                "statement_type": "brand_new_bank_chequing",
                "transaction_count": 6,
                "account_slug": "newbank-chequing-0001",
                "period": "2026-04",
            }
        ],
    )

    written = persist_statement_coverage(db_path, manifest)

    assert written == 1
    [row] = _coverage_rows(db_path)
    assert row.institution == ""
    assert row.statement_type == "brand_new_bank_chequing"
    assert row.row_count == 6


def test_missing_manifest_is_a_noop(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    init_database(db_path)

    assert persist_statement_coverage(db_path, tmp_path / "does-not-exist.json") == 0


@pytest.mark.parametrize(
    "payload",
    ["not json at all", '{"entries": []}', "null"],
    ids=["corrupt", "not-a-list", "null"],
)
def test_an_unusable_manifest_is_a_noop(tmp_path: Path, payload: str) -> None:
    """Same tolerance as persist_printed_credit_limits: an artifact problem must not
    lose the statements that did parse."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    manifest = tmp_path / "extraction-manifest.json"
    manifest.write_text(payload, encoding="utf-8")

    assert persist_statement_coverage(db_path, manifest) == 0


# ---------------------------------------------------------------------------
# 2. Finalize wiring: coverage lands in the promoted DB
# ---------------------------------------------------------------------------


def _paths(tmp_path: Path) -> Paths:
    pdf_dir = tmp_path / "inbox"
    processed_dir = tmp_path / "processed"
    pdf_dir.mkdir()
    processed_dir.mkdir()
    init_database(tmp_path / "ledger.db")
    return Paths(
        pdf_dir=pdf_dir,
        processed_dir=processed_dir,
        db_path=tmp_path / "ledger.db",
        base_db_path=None,
        backup_dir=tmp_path / "backups",
        ledger_config=tmp_path / "ledger.toml",
        merchant_rules=tmp_path / "merchants.toml",
        categories=tmp_path / "categories.toml",
        runs_dir=tmp_path / "runs",
    )


def _ready_finalize_run(tmp_path: Path, manifest_entries: list[dict]) -> tuple[Paths, Path]:
    """A run parked at ready_to_finalize whose extraction manifest is the one under test."""
    paths = _paths(tmp_path)
    run_dir = paths.runs_dir / "run-1"
    csv_root = run_dir / "csv"
    staging_db = run_dir / "staging.db"
    csv_root.mkdir(parents=True)
    (run_dir / "inbox").mkdir()
    init_database(staging_db)
    with session_scope(staging_db) as session:
        session.add(
            ProcessedFileDB(
                file_path="synthetic-staged-marker.pdf",
                file_hash="0" * 64,
                processed_at=datetime.now(UTC).replace(tzinfo=None),
                transaction_count=1,
            )
        )
    (run_dir / "merchant-review.json").write_text("[]", encoding="utf-8")
    extraction_manifest = run_dir / "extraction-manifest.json"
    _write_manifest(extraction_manifest, manifest_entries)

    workflow.write_run_state(
        run_dir,
        bound_import_state(
            {
                "status": "ready_to_finalize",
                "target_db_path": str(paths.db_path),
                "staging_db_path": str(staging_db),
                "base_db_path": str(paths.db_path),
                "csv_root": str(csv_root),
                "staging_inbox": str(run_dir / "inbox"),
                "merchant_review_json": str(run_dir / "merchant-review.json"),
                "extraction_manifest": str(extraction_manifest),
                "pending_review_count": 0,
                "approved_csv_paths": [],
                "account_slugs": {},
                "target_db_fingerprint_before": database_fingerprint(paths.db_path),
                "target_db_import_owned_before": import_owned_fingerprint(paths.db_path),
                "stage_counts": {"transactions": 1, "transfers": 0},
                "pdfs": [],
            },
            paths,
        ),
    )
    (paths.runs_dir / "latest-run.txt").write_text(str(run_dir), encoding="utf-8")
    return paths, run_dir


def _patch_finalize_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workflow, "load_config", lambda *_args: object())
    monkeypatch.setattr(workflow, "run_migrations", lambda _db: None)
    monkeypatch.setattr(workflow, "process", lambda **_kwargs: ProcessingResult(total_scanned=0))
    monkeypatch.setattr(workflow, "validate_processing_result", lambda *_args: None)
    monkeypatch.setattr(workflow, "write_result_to_db", lambda *_args: None)
    monkeypatch.setattr(workflow, "build_merchant_review", lambda _result: [])
    monkeypatch.setattr(workflow, "seed_review_candidates", lambda *_args: None)
    monkeypatch.setattr(workflow, "get_review_rows", lambda _db: [])
    monkeypatch.setattr(workflow, "apply_review_decisions", lambda _db: 0)
    monkeypatch.setattr(workflow, "pending_review_count", lambda _db: 0)
    monkeypatch.setattr(workflow, "run_post_processing", lambda _db, _config: {})
    monkeypatch.setattr(workflow, "record_processed_files", lambda *_args: None)
    monkeypatch.setattr(
        workflow, "result_counts", lambda _result: {"transactions": 0, "transfers": 0}
    )
    monkeypatch.setattr(
        workflow,
        "summarize_final_merchants",
        lambda *_args: {"resolved_merchants": [], "manual_review_merchants": []},
    )
    monkeypatch.setattr(workflow, "print_merchant_summary", lambda _summary: None)


def test_finalize_does_not_record_unapproved_zero_row_statement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unapproved manifest entry cannot assert an imported month.

    Complete accepted no-activity sources are covered by the end-to-end PDF
    import regressions; this parked run has no approved source evidence.
    """
    paths, run_dir = _ready_finalize_run(
        tmp_path,
        [
            {
                "csv_path": "",
                "statement_type": "meadow_mastercard",
                "transaction_count": 0,
                "skipped_reason": "no_transactions_extracted",
                "account_slug": "meadow-mastercard-9012",
                "period": "2026-07",
            }
        ],
    )
    _patch_finalize_dependencies(monkeypatch)

    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    assert _coverage_rows(paths.db_path) == []


# ---------------------------------------------------------------------------
# 3. Repair wiring: coverage must SURVIVE a repair, not be emptied by it
# ---------------------------------------------------------------------------


def _repair_options(tmp_path: Path, pdf_dir: Path, *, dry_run: bool) -> StatementRepairOptions:
    return StatementRepairOptions(
        db_path=tmp_path / "ledger.db",
        pdf_dirs=[pdf_dir],
        processed_dir=tmp_path / "processed",
        runs_dir=tmp_path / "runs",
        backup_dir=tmp_path / "backups",
        ledger_config=tmp_path / "ledger.toml",
        merchant_rules=tmp_path / "merchants.toml",
        categories=tmp_path / "categories.toml",
        dry_run=dry_run,
    )


def _patch_repair_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    *,
    keep_runtime_config_copy: bool = False,
) -> None:
    """Stub every heavy repair step, keeping backup_database/promote_candidate real.

    Mirrors tests/test_repair_and_optimization.py's harness, but the extraction stub
    carries a `period` and the manifest writer is the REAL one: this test is about
    what the manifest -> coverage seam does, so faking either end would test nothing.
    """
    from unittest.mock import MagicMock

    import ledger.repair.statements as repair
    from ledger.imports.pdf.statement_pdf_to_csv import ExtractedCsv

    monkeypatch.setattr(repair, "discover_pdfs", lambda pdf_dirs: [Path(pdf_dirs[0]) / "dummy.pdf"])
    monkeypatch.setattr(repair, "run_migrations", lambda _db: None)
    if not keep_runtime_config_copy:
        monkeypatch.setattr(repair, "copy_runtime_config", lambda *_a, **_k: None)

    def fake_process_pdf(pdf, csv_root, processed_dir, **_kwargs):
        csv_path = csv_root / "2026-04" / "harbor-chequing-1001.csv"
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        csv_path.write_text("Date,Description,Amount\n2026-04-01,TEST,-10.00\n", encoding="utf-8")
        return ExtractedCsv(
            pdf=pdf,
            csv_path=csv_path,
            processed_pdf=processed_dir / "dummy.pdf",
            statement_type="harbor_deposit",
            transaction_count=1,
            account_slug="harbor-chequing-1001",
            period="2026-04",
        )

    monkeypatch.setattr(repair.statement_pdf_to_csv, "process_pdf", fake_process_pdf)
    monkeypatch.setattr(repair, "build_inventory", lambda _results: [])
    monkeypatch.setattr(repair, "write_csv", lambda _path, _rows: None)
    monkeypatch.setattr(
        repair,
        "load_runtime_config",
        lambda _db, _opts: SimpleNamespace(
            ledger=SimpleNamespace(general=SimpleNamespace(account_aliases={}))
        ),
    )
    monkeypatch.setattr(repair, "process", lambda **_kw: ProcessingResult(total_scanned=1))
    monkeypatch.setattr(repair, "validate_processing_result", lambda *_a: None)
    monkeypatch.setattr(repair, "write_result_to_db", lambda *_a: None)
    monkeypatch.setattr(repair, "refresh_repair_analytics", lambda *_a, **_k: {})
    monkeypatch.setattr(repair, "build_move_plan", lambda _results, _dir: [])
    monkeypatch.setattr(repair, "write_json", lambda _path, _data: None)
    monkeypatch.setattr(repair, "validate_move_plan", lambda _plan: None)
    monkeypatch.setattr(repair, "record_processed_files", lambda *_a: None)

    audit = MagicMock()
    audit.run.return_value = MagicMock(
        run_dir=Path("/tmp/audit"),
        status="ok",
        summary={"counts": {"processed_file_hash_matches": 1}, "pdfs": 1},
    )
    monkeypatch.setattr(repair, "StatementAuditService", lambda: audit)
    monkeypatch.setattr(repair, "validate_candidate_audit", lambda *_a, **_k: None)
    monkeypatch.setattr(
        repair, "validate_database", lambda *_a, **_k: MagicMock(status="success", errors=[])
    )
    monkeypatch.setattr(repair, "prepare_pdf_targets", lambda _plan: None)
    monkeypatch.setattr(repair, "remove_replaced_sources", lambda _plan: None)
    monkeypatch.setattr(repair, "record_db_operation", lambda *_a, **_k: None)


def _seed_live_coverage(db_path: Path) -> None:
    with session_scope(db_path) as session:
        session.add(
            StatementCoverageDB(
                account_slug="harbor-chequing-1001",
                period="2026-04",
                institution="harbor",
                statement_type="harbor_deposit",
                row_count=1,
                statement_count=1,
                skipped_reason="",
                imported_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )


def test_repair_promote_does_not_empty_statement_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`repair statements` promotes a candidate DB rebuilt from transaction PDFs over
    the live one. A table populated only at finalize is wiped by that promote -- the
    exact failure that destroyed the investment domain before 1.2.0. Repair rebuilds
    coverage from its own extraction manifest, so the row must survive."""
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    (pdf_dir / "dummy.pdf").write_bytes(b"%PDF-1.4 fake")
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _seed_live_coverage(db_path)
    _patch_repair_dependencies(monkeypatch)

    result = StatementRepairService().run(_repair_options(tmp_path, pdf_dir, dry_run=False))

    assert result.status == "success", f"repair did not complete: {result.summary}"
    rows = _coverage_rows(db_path)
    assert [(row.account_slug, row.period, row.row_count) for row in rows] == [
        ("harbor-chequing-1001", "2026-04", 1)
    ], "statement_coverage was emptied by the repair promote"


def test_repair_preserves_runtime_merchant_rules_through_the_real_copy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive StatementRepairService with copy_runtime_config left real.

    A source-inspection test only proves call order; this pins the behavioral claim
    published by the teaching-loop spec, including the rule's spending exclusion.
    """
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    (pdf_dir / "dummy.pdf").write_bytes(b"%PDF-1.4 fake")
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    repo = SQLiteConfigRepository(db_path)
    repo.replace_from_configs(
        MerchantRulesConfig(
            version="repair-test",
            rules=[
                MerchantRule(
                    merchant_norm="SYNTHETIC MERCHANT",
                    patterns=["SYNTHETIC RAW"],
                    category="Transfers",
                    exclude_from_spending=True,
                )
            ],
        ),
        CategoriesConfig(
            version="repair-test",
            categories=[CategoryRule(name="Transfers", exclude_from_spending=True)],
        ),
    )
    _patch_repair_dependencies(monkeypatch, keep_runtime_config_copy=True)

    result = StatementRepairService().run(_repair_options(tmp_path, pdf_dir, dry_run=False))

    assert result.status == "success", f"repair did not complete: {result.summary}"
    [rule] = repo.load_merchant_rules().rules
    assert rule.merchant_norm == "SYNTHETIC MERCHANT"
    assert rule.patterns == ["SYNTHETIC RAW"]
    assert rule.category == "Transfers"
    assert rule.exclude_from_spending is True


def test_repair_writes_coverage_for_a_month_the_live_db_never_had(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not merely preserved: repair is a full rebuild, so it must WRITE coverage for
    every statement it extracts even when the live DB had no row for that month."""
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    (pdf_dir / "dummy.pdf").write_bytes(b"%PDF-1.4 fake")
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _patch_repair_dependencies(monkeypatch)

    result = StatementRepairService().run(_repair_options(tmp_path, pdf_dir, dry_run=False))

    assert result.status == "success", f"repair did not complete: {result.summary}"
    [row] = _coverage_rows(db_path)
    assert (row.account_slug, row.period, row.row_count) == ("harbor-chequing-1001", "2026-04", 1)
    assert row.institution == "harbor"


def test_repair_reports_the_number_of_coverage_rows_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Surfaced in the repair summary alongside printed_credit_limits, so an operator
    can see that coverage was rebuilt rather than having to query the DB."""
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    (pdf_dir / "dummy.pdf").write_bytes(b"%PDF-1.4 fake")
    init_database(tmp_path / "ledger.db")
    _patch_repair_dependencies(monkeypatch)

    result = StatementRepairService().run(_repair_options(tmp_path, pdf_dir, dry_run=True))

    assert result.summary["post_processing"]["statement_coverage"] == 1


def test_statement_coverage_table_exists_after_init(tmp_path: Path) -> None:
    """Guards the ORM/table name the two writers above share."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)

    with closing(sqlite3.connect(db_path)) as conn:
        names = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert "statement_coverage" in names


def test_backfill_seeds_coverage_from_already_imported_transactions(tmp_path: Path) -> None:
    """A database imported before the table existed would otherwise report nothing.

    An empty coverage table is indistinguishable from a ledger nobody imported
    into -- the very distinction the table exists to make, unmade about itself.
    """
    import sqlite3
    from contextlib import closing

    from ledger.storage.migrations import run_migrations

    db_path = tmp_path / "ledger.db"
    run_migrations(db_path)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("DELETE FROM statement_coverage")
        for index, (period, source) in enumerate(
            [("2026-06", "a.csv"), ("2026-06", "b.csv"), ("2026-07", "a.csv")]
        ):
            conn.execute(
                "INSERT INTO transactions (txn_id, posting_date, institution, account, "
                "account_type, flow, cashflow_type, include_in_cashflow, currency, amount_raw, "
                "amount_signed, description, merchant, category, merchant_confidence, "
                "category_confidence, exclude_from_spending, merchant_raw, category_raw, "
                "source_file, source_path, source_period, source_row, imported_at, "
                "parser_version, txn_fingerprint, day_of_week, day_of_month, week_of_year, "
                "is_weekend, is_month_start, is_month_end) VALUES "
                f"('bf-{index}', '{period}-05', 'harbor', 'harbor-chequing-9101', 'checking', "
                "'expense', 'expense', 1, 'CAD', 5.00, -5.00, 'X', 'M', 'Other', 'high', "
                f"'high', 0, 'M', '', '{source}', '/{source}', '{period}', 1, "
                "'2026-08-01 00:00:00', 'v1', 'fp', 3, 5, 3, 0, 0, 0)"
            )
        conn.execute("DELETE FROM schema_migrations WHERE version LIKE '%backfill%'")

    run_migrations(db_path)

    with closing(sqlite3.connect(db_path)) as conn:
        rows = dict(
            conn.execute(
                "SELECT period, row_count FROM statement_coverage WHERE account_slug = ?",
                ("harbor-chequing-9101",),
            ).fetchall()
        )
        statements = conn.execute(
            "SELECT statement_count FROM statement_coverage WHERE account_slug = ? AND period = ?",
            ("harbor-chequing-9101", "2026-06"),
        ).fetchone()[0]

    assert rows == {"2026-06": 2, "2026-07": 1}
    assert statements == 2, "distinct source files per account-month"


def test_backfill_never_overwrites_a_real_import(tmp_path: Path) -> None:
    """A finalize-written row is authoritative; the backfill must not clobber it."""
    import sqlite3
    from contextlib import closing
    from datetime import datetime

    from ledger.storage.database import StatementCoverageDB, session_scope
    from ledger.storage.migrations import run_migrations

    db_path = tmp_path / "ledger.db"
    run_migrations(db_path)
    with session_scope(db_path) as session:
        session.add(
            StatementCoverageDB(
                account_slug="harbor-chequing-9101",
                period="2026-06",
                institution="harbor",
                statement_type="harbor_deposit",
                row_count=0,
                statement_count=1,
                skipped_reason="no_transactions_extracted",
                imported_at=datetime(2026, 8, 1, 12, 0),
            )
        )
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("DELETE FROM schema_migrations WHERE version LIKE '%backfill%'")

    run_migrations(db_path)

    with closing(sqlite3.connect(db_path)) as conn:
        reason, statement_type = conn.execute(
            "SELECT skipped_reason, statement_type FROM statement_coverage "
            "WHERE account_slug = ? AND period = ?",
            ("harbor-chequing-9101", "2026-06"),
        ).fetchone()
    assert reason == "no_transactions_extracted"
    assert statement_type == "harbor_deposit"


pytestmark = pytest.mark.usefixtures("fictional_adapter_metadata")
