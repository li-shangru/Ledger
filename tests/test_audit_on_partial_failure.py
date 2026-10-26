"""TDD tests for audit-row-on-partial-failure observability fixes.

Finding 1 — data_cleanup: if _delete_paths / _prune_metadata / _vacuum raises
  mid-way the DataCleanupService must still write a FAILED db_operation_audit row
  (best-effort) and then re-raise the exception.

Finding 2 — statement_repair_promote: if a post-promote step (StatementAuditService
  final audit, validate_candidate_audit, …) raises AFTER live DB publication has
  already happened, the StatementRepairService must still write a FAILED
  statement_repair_promote audit row.

  Conversely, a failure that aborts BEFORE publication (e.g. the A4 fingerprint-recheck
  RuntimeError) must NOT produce a statement_repair_promote row — the promote never
  happened and the audit must reflect that truthfully.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from ledger.maintenance import DataCleanupService
from ledger.repair.statements import StatementRepairOptions, StatementRepairService
from ledger.storage.database import DBOperationAuditDB, init_database, session_scope

# ---------------------------------------------------------------------------
# Helpers shared across cleanup tests
# ---------------------------------------------------------------------------


def _seed_cleanup_state(data_dir: Path) -> Path:
    """Minimal seeded state for DataCleanupService.clean(execute=True)."""
    from datetime import UTC, datetime

    from ledger.storage.database import ExportRunDB, ImportRunDB

    db_path = data_dir / "ledger.db"
    init_database(db_path)

    (data_dir / "exports-smoke" / "ledger-chatgpt-safe-smoke").mkdir(parents=True)
    (data_dir / "exports-smoke" / "ledger-chatgpt-safe-smoke" / "transactions.csv").write_text(
        "id\n1\n", encoding="utf-8"
    )
    (data_dir / "ledger-backup.db").write_text("old backup", encoding="utf-8")
    (data_dir / "db-backups").mkdir()
    (data_dir / "db-backups" / "old-backup.db").write_text("old backup", encoding="utf-8")
    (data_dir / "processed").mkdir()

    now = datetime.now(UTC).replace(tzinfo=None)
    with session_scope(db_path) as session:
        session.add(
            ImportRunDB(
                run_at=now,
                source_path=str(data_dir / "import-runs" / "old-run" / "csv"),
                files_processed=1,
                transactions_imported=1,
                transfers_imported=0,
                duplicates_skipped=0,
                errors=0,
                parser_version="test",
            )
        )
        session.add(
            ExportRunDB(
                created_at=now,
                preset="chatgpt-safe",
                export_format="zip",
                output_path=str(data_dir / "exports-smoke" / "ledger-chatgpt-safe-smoke.zip"),
                rows_exported=1,
                status="success",
                details={},
            )
        )
    return db_path


# ---------------------------------------------------------------------------
# Finding 1 — cleanup partial-failure
# ---------------------------------------------------------------------------


class TestCleanupAuditOnPartialFailure:
    """data_cleanup must write a FAILED audit row even when a destructive step raises."""

    def test_prune_metadata_failure_writes_failed_audit_row_and_reraises(
        self, tmp_path: Path
    ) -> None:
        """If _prune_metadata raises mid-clean, a FAILED data_cleanup row must be written
        and the exception must propagate (not be swallowed)."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        db_path = _seed_cleanup_state(data_dir)

        service = DataCleanupService(db_path, data_dir=data_dir)

        with (
            patch.object(
                service,
                "_prune_metadata",
                side_effect=RuntimeError("injected _prune_metadata failure"),
            ),
            pytest.raises(RuntimeError, match="injected _prune_metadata failure"),
        ):
            service.clean(execute=True)

        # The FAILED audit row must have been written despite the exception.
        with session_scope(db_path) as session:
            rows = (
                session.query(DBOperationAuditDB)
                .filter(
                    DBOperationAuditDB.operation == "data_cleanup",
                    DBOperationAuditDB.status == "failed",
                )
                .all()
            )
            assert rows, (
                "Expected a FAILED db_operation_audit row for 'data_cleanup' after "
                "_prune_metadata raised, but none was found."
            )
            row = rows[0]
            row_errors = list(row.errors or [])
            row_artifact = row.artifact_path
        assert any("injected" in e or "_prune_metadata" in e for e in row_errors), (
            f"Audit row errors did not contain the injected error; got: {row_errors}"
        )
        # The retained backup artifact must be referenced.
        assert row_artifact is not None, (
            "FAILED audit row must reference the retained backup artifact path."
        )

    def test_vacuum_failure_writes_failed_audit_row_and_reraises(self, tmp_path: Path) -> None:
        """If _vacuum raises, the same audit-on-failure contract must hold."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        db_path = _seed_cleanup_state(data_dir)

        service = DataCleanupService(db_path, data_dir=data_dir)

        with (
            patch.object(service, "_vacuum", side_effect=RuntimeError("injected _vacuum failure")),
            pytest.raises(RuntimeError, match="injected _vacuum failure"),
        ):
            service.clean(execute=True)

        with session_scope(db_path) as session:
            rows = (
                session.query(DBOperationAuditDB)
                .filter(
                    DBOperationAuditDB.operation == "data_cleanup",
                    DBOperationAuditDB.status == "failed",
                )
                .all()
            )
            assert rows, (
                "Expected a FAILED db_operation_audit row for 'data_cleanup' after "
                "_vacuum raised, but none was found."
            )

    def test_cleanup_happy_path_still_writes_exactly_one_success_row(self, tmp_path: Path) -> None:
        """The normal (no-error) path must still write exactly one SUCCESS audit row."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        db_path = _seed_cleanup_state(data_dir)

        result = DataCleanupService(db_path, data_dir=data_dir).clean(execute=True)

        assert result.ok, f"Clean should succeed; errors: {result.errors}"
        with session_scope(db_path) as session:
            count = (
                session.query(DBOperationAuditDB)
                .filter(
                    DBOperationAuditDB.operation == "data_cleanup",
                    DBOperationAuditDB.status == "success",
                )
                .count()
            )
        assert count == 1, f"Expected exactly 1 SUCCESS data_cleanup audit row; got {count}"


# ---------------------------------------------------------------------------
# Helpers shared across repair tests
# ---------------------------------------------------------------------------


def _make_minimal_repair_db(tmp_path: Path) -> Path:
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    return db_path


def _repair_options(
    tmp_path: Path, pdf_dir: Path, *, dry_run: bool = False
) -> StatementRepairOptions:
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


def _apply_repair_mocks_pre_promote(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch all heavy pre-promote steps, keeping backup_database and promote_candidate real."""
    from pathlib import Path as _Path

    import ledger.repair.statements as _svc
    from ledger.imports.pdf.statement_pdf_to_csv import ExtractedCsv as _ExtractedCsv
    from ledger.processing import ProcessingResult as _ProcessingResult

    monkeypatch.setattr(
        _svc, "discover_pdfs", lambda dirs: [_Path(dirs[0]) / "dummy.pdf"] if dirs else []
    )
    monkeypatch.setattr(_svc, "run_migrations", lambda _p: None)
    monkeypatch.setattr(_svc, "copy_runtime_config", lambda *_a, **_k: None)

    def fake_process_pdf(pdf, csv_root, processed_dir, **_kwargs):
        csv_root.mkdir(parents=True, exist_ok=True)
        csv_path = csv_root / "dummy.csv"
        csv_path.write_text("Date,Description,Amount\n2025-01-01,TEST,-10.00\n", encoding="utf-8")
        return _ExtractedCsv(
            pdf=pdf,
            csv_path=csv_path,
            processed_pdf=processed_dir / "dummy.pdf",
            statement_type="harbor_deposit",
            transaction_count=1,
            validation_summary={"opening_balance": "110.00", "closing_balance": "100.00"},
        )

    monkeypatch.setattr(_svc.statement_pdf_to_csv, "process_pdf", fake_process_pdf)
    monkeypatch.setattr(
        _svc.statement_pdf_to_csv,
        "write_manifest",
        lambda path, _results, dry_run: path.write_text("[]", encoding="utf-8"),
    )
    monkeypatch.setattr(_svc, "build_inventory", lambda _results: [])
    monkeypatch.setattr(_svc, "write_csv", lambda _path, _rows: None)
    fake_config = SimpleNamespace(
        ledger=SimpleNamespace(general=SimpleNamespace(account_aliases={})),
    )
    monkeypatch.setattr(_svc, "load_runtime_config", lambda _db, _opts: fake_config)
    monkeypatch.setattr(_svc, "process", lambda **_kw: _ProcessingResult(total_scanned=1))
    monkeypatch.setattr(_svc, "validate_processing_result", lambda *_a: None)
    monkeypatch.setattr(_svc, "write_result_to_db", lambda *_a: None)
    monkeypatch.setattr(_svc, "refresh_repair_analytics", lambda *_a, **_k: {})
    monkeypatch.setattr(_svc, "build_move_plan", lambda _results, _dir: [])
    monkeypatch.setattr(_svc, "write_json", lambda _path, _data: None)
    monkeypatch.setattr(_svc, "validate_move_plan", lambda _plan: None)
    monkeypatch.setattr(_svc, "record_processed_files", lambda *_a: None)

    # candidate audit (pre-promote): pass unconditionally
    fake_candidate_audit = MagicMock()
    fake_candidate_audit.run.return_value = MagicMock(
        run_dir=_Path("/tmp/audit"),
        status="ok",
        summary={"counts": {"processed_file_hash_matches": 1}, "pdfs": 1},
    )

    monkeypatch.setattr(_svc, "validate_candidate_audit", lambda *_a, **_k: None)
    monkeypatch.setattr(
        _svc, "validate_database", lambda *_a, **_k: MagicMock(status="success", errors=[])
    )
    monkeypatch.setattr(_svc, "prepare_pdf_targets", lambda _plan: None)
    monkeypatch.setattr(_svc, "remove_replaced_sources", lambda _plan: None)

    # StatementAuditService: first call (pre-promote candidate audit) must pass,
    # but the second call (post-promote final audit) can be overridden per test.
    monkeypatch.setattr(_svc, "StatementAuditService", lambda: fake_candidate_audit)


# ---------------------------------------------------------------------------
# Finding 2 — statement_repair_promote audit on post-promote failure
# ---------------------------------------------------------------------------


class TestRepairPromoteAuditOnPostPromoteFailure:
    """statement_repair_promote audit row must be written even when post-promote steps fail."""

    def test_post_promote_failure_writes_failed_promote_audit_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the final StatementAuditService.run raises AFTER promote_candidate published the DB,
        a FAILED statement_repair_promote audit row must still be written."""
        import ledger.repair.statements as _svc

        live_db = _make_minimal_repair_db(tmp_path)
        pdf_dir = tmp_path / "pdfs"
        pdf_dir.mkdir()
        (pdf_dir / "dummy.pdf").write_bytes(b"%PDF-1.4 fake")

        options = _repair_options(tmp_path, pdf_dir, dry_run=False)

        _apply_repair_mocks_pre_promote(monkeypatch)

        # After promote, inject a failing StatementAuditService for the *final* (post-promote) audit.
        call_count = {"n": 0}

        class _FakeAuditService:
            def run(self, audit_options):
                call_count["n"] += 1
                if call_count["n"] == 1:
                    # First call: candidate pre-promote audit — succeeds.
                    return MagicMock(
                        run_dir=tmp_path / "candidate-audit",
                        status="ok",
                        summary={"counts": {"processed_file_hash_matches": 1}, "pdfs": 1},
                    )
                # Second call: final post-promote audit — raises.
                raise RuntimeError("injected post-promote audit failure")

        monkeypatch.setattr(_svc, "StatementAuditService", _FakeAuditService)

        result = StatementRepairService().run(options)

        # The service must return a status that communicates the promote DID happen
        # while signalling that post-promote steps failed.  A bare 'failed' is
        # misleading because candidate content is already live at this point.
        assert result.status == "promoted_with_post_step_errors", (
            f"Expected status='promoted_with_post_step_errors' after post-promote audit failure; "
            f"got {result.status!r}.  "
            "The candidate DB was already published — the status must reflect that."
        )

        # A FAILED statement_repair_promote audit row must exist on the (now-promoted) live DB.
        with session_scope(live_db) as session:
            rows = (
                session.query(DBOperationAuditDB)
                .filter(
                    DBOperationAuditDB.operation == "statement_repair_promote",
                    DBOperationAuditDB.status == "failed",
                )
                .all()
            )
            assert rows, (
                "Expected a FAILED db_operation_audit row for 'statement_repair_promote' "
                "after a post-promote step raised, but none was found. "
                "The live DB has already been replaced — this destructive operation must be audited."
            )
            row = rows[0]
            row_errors = list(row.errors or [])
            row_artifact = row.artifact_path
        assert any("injected" in e or "audit" in e for e in row_errors), (
            f"Audit row errors did not contain the injected error; got: {row_errors}"
        )
        assert row_artifact is not None, (
            "FAILED promote audit row must reference a backup artifact path."
        )

    def test_pre_promote_failure_does_not_write_promote_audit_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failure that aborts BEFORE promote_candidate is called must NOT produce a
        statement_repair_promote audit row — the swap never happened."""
        import ledger.repair.statements as _svc

        live_db = _make_minimal_repair_db(tmp_path)
        pdf_dir = tmp_path / "pdfs"
        pdf_dir.mkdir()
        (pdf_dir / "dummy.pdf").write_bytes(b"%PDF-1.4 fake")

        options = _repair_options(tmp_path, pdf_dir, dry_run=False)

        _apply_repair_mocks_pre_promote(monkeypatch)

        # The service already holds the target boundary and therefore calls the explicit
        # locked publisher. Raise before its swap to simulate the A4 fingerprint recheck.
        monkeypatch.setattr(
            _svc,
            "_promote_candidate_locked",
            lambda *_a, **_k: (_ for _ in ()).throw(
                RuntimeError("target DB changed during finalize; aborting promote")
            ),
        )

        result = StatementRepairService().run(options)

        assert result.status == "failed"

        # No statement_repair_promote audit row must exist (promote never happened).
        with session_scope(live_db) as session:
            count = (
                session.query(DBOperationAuditDB)
                .filter(DBOperationAuditDB.operation == "statement_repair_promote")
                .count()
            )
        assert count == 0, (
            f"Expected NO statement_repair_promote audit row when promote aborted "
            f"before the swap; found {count} row(s). "
            "The audit must truthfully reflect that no mutation occurred."
        )

    def test_repair_happy_path_still_writes_exactly_one_success_promote_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The normal (no-error) promote path still writes exactly one SUCCESS row."""
        import ledger.repair.statements as _svc

        live_db = _make_minimal_repair_db(tmp_path)
        pdf_dir = tmp_path / "pdfs"
        pdf_dir.mkdir()
        (pdf_dir / "dummy.pdf").write_bytes(b"%PDF-1.4 fake")

        options = _repair_options(tmp_path, pdf_dir, dry_run=False)

        _apply_repair_mocks_pre_promote(monkeypatch)

        # Both audit calls succeed.
        fake_audit = MagicMock()
        fake_audit.run.return_value = MagicMock(
            run_dir=tmp_path / "final-audit",
            status="ok",
            summary={"counts": {"processed_file_hash_matches": 1}, "pdfs": 1},
        )
        monkeypatch.setattr(_svc, "StatementAuditService", lambda: fake_audit)

        result = StatementRepairService().run(options)

        assert result.status == "success", (
            f"Happy-path repair must succeed; got {result.status!r}. Summary: {result.summary}"
        )
        with session_scope(live_db) as session:
            count = (
                session.query(DBOperationAuditDB)
                .filter(
                    DBOperationAuditDB.operation == "statement_repair_promote",
                    DBOperationAuditDB.status == "success",
                )
                .count()
            )
        assert count == 1, (
            f"Expected exactly 1 SUCCESS statement_repair_promote audit row; got {count}"
        )


pytestmark = pytest.mark.usefixtures("fictional_adapter_metadata")
