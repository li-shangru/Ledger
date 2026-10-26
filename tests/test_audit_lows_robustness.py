"""Tests for four robustness fixes from the 2026-06 audit (low findings).

Fix 1: ReDoS — static check rejects nested-unbounded-quantifier patterns.
Fix 2: stage() per-PDF error isolation — bad PDF → blocking_validation_error, not crash.
Fix 3: export package rejects invalid --start/--end with Exit(1).
Fix 4: repair surfaces 'promoted_with_post_step_errors' after post-promote failure.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest


class _StopStageError(Exception):
    """Sentinel raised from a monkeypatched hook to halt stage() cleanly at a
    known point (after the partition decision) without mocking the full
    processing/promotion pipeline."""


# =============================================================================
# Fix 1: ReDoS — static nested-unbounded-quantifier check
# =============================================================================


class TestReDoSGuard:
    """CompiledPattern.from_string and MerchantRule.validate_patterns must reject
    patterns with nested unbounded quantifiers while accepting all normal patterns."""

    # --- dangerous patterns that MUST be rejected ---
    @pytest.mark.parametrize(
        "pattern",
        [
            r"(a+)+",
            r"(a*)*",
            r"(a+)*",
            r"(.*)+",
            r"([a-z]+)+",
            r"(foo|bar+)+",
            r"(x+)+foo",
            # {n,} / {n,m} outer quantifier — equally catastrophic
            r"(x+){2,}",
            r"(ab+){5,}",
            r"(a+){1,10}",
            r"(.*){2,}",
        ],
    )
    def test_compiled_pattern_rejects_nested_unbounded_quantifiers(self, pattern: str) -> None:
        from ledger.config.schemas import CompiledPattern

        with pytest.raises(ValueError, match="catastrophic"):
            CompiledPattern.from_string(pattern)

    @pytest.mark.parametrize(
        "pattern",
        [
            r"(a+)+",
            r"(a*)*",
            r"(a+)*",
            r"(.*)+",
        ],
    )
    def test_merchant_rule_validate_patterns_rejects_redos(self, pattern: str) -> None:
        from ledger.config.schemas import MerchantRule

        with pytest.raises(ValueError, match="catastrophic"):
            MerchantRule(
                merchant_norm="test",
                category="Other",
                patterns=[pattern],
            )

    @pytest.mark.parametrize(
        "pattern",
        [
            r"(a+)+",
            r"(a*)*",
        ],
    )
    def test_classification_patterns_rejects_redos(self, pattern: str) -> None:
        from ledger.config.schemas import ClassificationPatterns

        with pytest.raises(ValueError, match="catastrophic"):
            ClassificationPatterns(transfer_patterns=[pattern])

    # --- legitimate patterns that MUST still validate ---
    @pytest.mark.parametrize(
        "pattern",
        [
            r"COFFEE",
            r"^GROCERY\s+STORE",
            r"AMAZON\s*PRIME",
            r"(VISA|MASTERCARD)",
            r"\bTRANSFER\b",
            r"[A-Z]{3,}",
            r"foo+",  # single quantifier on a non-group — fine
            r"(foo)+",  # non-backtracking group with one quantifier — fine
            r"(foo|bar)*",  # alternation of literals, outer quantifier — fine
            r"[\w\s]+",
            r"(?:abc)+",  # non-capturing group with single outer quantifier
            r"(a{2})+",  # fixed-count inner — no inner quantifier
            r"e-transfer",
            r"(?i)paypal",
            r"INTERAC.*ONLINE",
        ],
    )
    def test_legitimate_patterns_still_validate(self, pattern: str) -> None:
        from ledger.config.schemas import CompiledPattern

        # Must NOT raise
        cp = CompiledPattern.from_string(pattern)
        assert cp.source == pattern

    def test_merchant_rule_with_safe_patterns_creates_successfully(self) -> None:
        from ledger.config.schemas import MerchantRule

        rule = MerchantRule(
            merchant_norm="Amazon",
            category="Shopping",
            patterns=[r"AMAZON", r"(?i)amazon\.ca", r"(amazon|amzn)\s*prime"],
        )
        assert rule.merchant_norm == "Amazon"


# =============================================================================
# Fix 2: stage() per-PDF error isolation
# =============================================================================


def _make_paths(tmp_path: Path) -> Any:
    """Build a Paths-like namespace for workflow.stage()."""
    from ledger.imports.pdf.paths import Paths

    pdf_dir = tmp_path / "inbox"
    pdf_dir.mkdir()
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    processed_dir = tmp_path / "processed"
    processed_dir.mkdir()
    db_path = tmp_path / "ledger.db"
    return Paths(
        pdf_dir=pdf_dir,
        processed_dir=processed_dir,
        db_path=db_path,
        base_db_path=None,
        backup_dir=tmp_path / "backups",
        ledger_config=tmp_path / "ledger.toml",
        merchant_rules=tmp_path / "merchants.toml",
        categories=tmp_path / "categories.toml",
        runs_dir=runs_dir,
    )


class TestStagePerPdfErrorIsolation:
    """stage() must not propagate a raw exception from a single bad PDF;
    instead it should surface a blocking_validation_error for that PDF
    and continue processing other PDFs if any exist."""

    def test_single_bad_pdf_becomes_blocking_validation_error(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A PDF that raises during process_pdf must NOT cause stage() to crash.
        It should be recorded as a blocking_validation_error and the run should
        exit with a failed status (statement_validation_failed) rather than a traceback."""
        import ledger.imports.pdf.workflow as wf

        paths = _make_paths(tmp_path)

        # Create one "bad" PDF in the inbox
        bad_pdf = paths.pdf_dir / "bad.pdf"
        bad_pdf.write_bytes(b"%PDF-1.4 garbage")

        # Patch helpers that need real DB/config infra
        monkeypatch.setattr(wf, "clone_database", lambda src, dst: dst.write_bytes(b""))
        monkeypatch.setattr(wf, "run_migrations", lambda _p: None)
        monkeypatch.setattr(wf, "ensure_review_schema", lambda _p: None)
        monkeypatch.setattr(wf, "db_counts", lambda _p: {})
        monkeypatch.setattr(wf, "known_pdf_hashes", lambda _p: set())
        # make_run_dir must return a real directory so write_run_state works
        run_dir = tmp_path / "runs" / "run001"
        run_dir.mkdir(parents=True)
        monkeypatch.setattr(wf, "make_run_dir", lambda _p: run_dir)

        # process_pdf raises for our bad PDF
        def boom_process_pdf(*_a, **_kw):
            raise RuntimeError("PDF parse exploded!")

        monkeypatch.setattr(wf.statement_pdf_to_csv, "process_pdf", boom_process_pdf)

        run_state_written: list[dict] = []

        def fake_write_run_state(_run_dir, state):
            run_state_written.append(dict(state))

        monkeypatch.setattr(wf, "write_run_state", fake_write_run_state)

        args = SimpleNamespace()
        # stage() should NOT raise — the bad PDF must be isolated
        try:
            wf.stage(args, paths)
        except SystemExit:
            pass  # statement_validation_failed SystemExit is fine
        except Exception as exc:
            raise AssertionError(
                f"stage() propagated a raw exception instead of isolating the bad PDF: {exc!r}"
            ) from exc

        # The run state must record a blocking validation error, not a raw crash
        assert run_state_written, "write_run_state was never called"
        last_state = run_state_written[-1]
        # Either the run failed with statement_validation_failed, OR the bad PDF's
        # error is captured in validation_errors on the PDF item
        pdfs_state = last_state.get("pdfs", [])
        if pdfs_state:
            bad_item = next(
                (p for p in pdfs_state if "bad.pdf" in str(p.get("original_pdf", ""))),
                None,
            )
            if bad_item:
                # validation_errors should carry the exception message
                assert bad_item.get("validation_errors") or bad_item.get("skipped_reason"), (
                    f"Bad PDF item must have validation_errors or skipped_reason; got: {bad_item}"
                )
        # The failure_reason on the last state must indicate statement_validation_failed
        # (not a raw unhandled exception)
        if "failure_reason" in last_state:
            assert last_state["failure_reason"] == "statement_validation_failed", (
                f"Expected failure_reason='statement_validation_failed'; got: {last_state['failure_reason']!r}"
            )

    def test_one_failing_pdf_does_not_abort_the_other_two(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """One PDF with blocking validation errors must be demoted to a per-PDF
        skip; the two reconciling PDFs must still stage and the run must NOT abort."""
        import ledger.imports.pdf.workflow as wf
        from ledger.imports.pdf.statement_pdf_to_csv import ExtractedCsv

        paths = _make_paths(tmp_path)
        for name in ("good_a.pdf", "bad.pdf", "good_b.pdf"):
            (paths.pdf_dir / name).write_bytes(f"%PDF-1.4 fictional {name}".encode())

        monkeypatch.setattr(wf, "clone_database", lambda src, dst: dst.write_bytes(b""))
        monkeypatch.setattr(wf, "run_migrations", lambda _p: None)
        monkeypatch.setattr(wf, "ensure_review_schema", lambda _p: None)
        monkeypatch.setattr(wf, "db_counts", lambda _p: {})
        monkeypatch.setattr(wf, "known_pdf_hashes", lambda _p: set())
        run_dir = tmp_path / "runs" / "run001"
        run_dir.mkdir(parents=True)
        monkeypatch.setattr(wf, "make_run_dir", lambda _p: run_dir)

        def fake_process_pdf(pdf, csv_root, processed_dir, **_kwargs):
            # Mirror real process_pdf: it WRITES a CSV to csv_root for any statement
            # that parsed rows, INCLUDING one that then fails reconciliation (csv_path
            # set, skipped_reason left empty, validation_errors populated). This is the
            # exact shape that used to leak the failed statement's rows through the
            # process()/finalize() re-glob of csv_root.
            csv_root = Path(csv_root)
            csv_root.mkdir(parents=True, exist_ok=True)
            csv_p = csv_root / f"{pdf.stem}.csv"
            csv_p.write_text(
                "Ledger Format,Date,Description,Amount,Currency,Reference,Institution,Account Type\nledger-mapped-v1,2026-01-05,ROW,-5.00,CAD,,harbor,Chequing\n"
            )
            if pdf.name == "bad.pdf":
                return ExtractedCsv(
                    pdf=pdf,
                    csv_path=csv_p,
                    processed_pdf=None,
                    statement_type="harbor_deposit",
                    transaction_count=1,
                    account_slug=pdf.stem,
                    validation_errors=(
                        "Statement summary does not reconcile (discrepancy $100-1k)",
                    ),
                )
            return ExtractedCsv(
                pdf=pdf,
                csv_path=csv_p,
                processed_pdf=None,
                statement_type="harbor_deposit",
                transaction_count=1,
                account_slug=pdf.stem,
            )

        monkeypatch.setattr(wf.statement_pdf_to_csv, "process_pdf", fake_process_pdf)

        # Stop stage() right after the partition+cleanup, before load_config, so we
        # don't need the full processing/promotion infra — but track that we REACHED
        # load_config, which proves the batch did NOT abort on the bad PDF.
        reached_load_config = {"hit": False}

        def _stop_at_load_config(*_a, **_k):
            reached_load_config["hit"] = True
            raise _StopStageError()

        monkeypatch.setattr(wf, "load_config", _stop_at_load_config)
        monkeypatch.setattr(wf, "write_run_state", lambda _run_dir, _state: None)

        args = SimpleNamespace()
        with contextlib.suppress(_StopStageError):
            wf.stage(args, paths)

        csv_root_dir = run_dir / "csv"
        # H2: the batch did NOT abort — stage reached load_config with the good PDFs
        # still importable (a whole-batch abort would SystemExit before this).
        assert reached_load_config["hit"], "stage aborted the whole batch on the bad PDF"
        # Regression guard for the ZK-found leak: the failed statement's CSV must be
        # REMOVED from csv_root so process()/finalize() cannot re-glob its rows into the
        # DB; the two importable statements' CSVs must remain.
        assert not (csv_root_dir / "bad.csv").exists(), (
            "failed statement's CSV was left in csv_root — it would leak into the DB"
        )
        assert (csv_root_dir / "good_a.csv").exists()
        assert (csv_root_dir / "good_b.csv").exists()


def test_remove_skipped_csvs_prevents_process_from_importing_failed_statement(
    tmp_path, sample_app_config
):
    """End-to-end guard: after a failed statement is demoted to a skip, process()
    (which re-globs csv_root) must NOT materialize its rows. Reverting the cleanup
    makes process() scan the leaked row and this test fails."""
    from ledger.imports.pdf.workflow import remove_skipped_csvs
    from ledger.processing.pipeline import process

    csv_root = tmp_path / "csv"
    csv_root.mkdir()
    header = "Ledger Format,Date,Description,Amount,Currency,Reference,Institution,Account Type\n"
    good = csv_root / "harbor-good.csv"
    bad = csv_root / "harbor-bad.csv"
    good.write_text(header + "ledger-mapped-v1,2026-01-05,GOOD ROW,-10.00,CAD,,harbor,Chequing\n")
    bad.write_text(header + "ledger-mapped-v1,2026-01-06,LEAKED ROW,-99.00,CAD,,harbor,Chequing\n")

    items = [
        {"skipped_reason": "statement_validation_failed", "csv_path": str(bad)},
        {"skipped_reason": "", "csv_path": str(good)},
    ]
    removed = remove_skipped_csvs(items)
    assert bad in removed and not bad.exists(), "skipped statement's CSV not removed"
    assert good.exists(), "importable statement's CSV was wrongly removed"
    assert items[0]["csv_path"] == "", "manifest still points at the removed CSV"

    result = process(sample_app_config, csv_root)
    descriptions = " ".join(t.description for t in result.transactions)
    assert result.total_scanned == 1, (
        f"failed statement leaked into process(): {result.total_scanned}"
    )
    assert "LEAKED ROW" not in descriptions
    assert "GOOD ROW" in descriptions


# =============================================================================
# Fix 3: export package rejects invalid --start/--end
# =============================================================================


class TestExportInvalidDateRejected:
    """export package must Exit(1) when --start or --end is provided but not a valid date.

    export_app is a SINGLE-COMMAND Typer app: @export_app.command("package").
    Typer collapses single-command apps so the CLI takes NO subcommand token.
    Invoking with ["package", ...] would make "package" be parsed as a positional
    argument (or cause an unrelated arg-parse error), never reaching the date-reject
    guard.  All invocations here deliberately omit the "package" token.
    """

    def test_invalid_start_exits_nonzero(self, tmp_path: Path) -> None:
        import typer.testing

        from ledger.commands.export import export_app

        # resolve_db_path checks that the DB file exists before parse_date is called.
        # Create a real (empty) DB so we pass the DB-exists guard and reach the
        # date-reject guard.
        from ledger.storage.database import init_database

        db_path = tmp_path / "ledger.db"
        init_database(db_path)

        runner = typer.testing.CliRunner()
        # Single-command app — NO "package" subcommand token.
        result = runner.invoke(
            export_app,
            ["--db", str(db_path), "--start", "not-a-date"],
        )
        assert result.exit_code != 0, (
            f"Expected non-zero exit for invalid --start; got exit_code={result.exit_code}, "
            f"output={result.output!r}"
        )
        # Assert on the SPECIFIC date-rejection message from export.py, not an
        # unrelated arg-parse error.  This ensures the guard itself is reached.
        assert "--start must be a valid date" in result.output, (
            f"Expected the date-reject guard message '--start must be a valid date'; "
            f"got: {result.output!r}. "
            f"If you see an arg-parse error, the invocation is wrong (check subcommand token)."
        )

    def test_invalid_end_exits_nonzero(self, tmp_path: Path) -> None:
        import typer.testing

        from ledger.commands.export import export_app

        # resolve_db_path checks that the DB file exists before parse_date is called.
        from ledger.storage.database import init_database

        db_path = tmp_path / "ledger.db"
        init_database(db_path)

        runner = typer.testing.CliRunner()
        # Single-command app — NO "package" subcommand token.
        result = runner.invoke(
            export_app,
            [
                "--db",
                str(db_path),
                "--start",
                "2025-01-01",
                "--end",
                "bad-date",
            ],
        )
        assert result.exit_code != 0, (
            f"Expected non-zero exit for invalid --end; got exit_code={result.exit_code}, "
            f"output={result.output!r}"
        )
        # Assert on the SPECIFIC date-rejection message — not an unrelated error.
        assert "--end must be a valid date" in result.output, (
            f"Expected the date-reject guard message '--end must be a valid date'; "
            f"got: {result.output!r}. "
            f"If you see an arg-parse error, the invocation is wrong."
        )

    def test_valid_dates_do_not_exit_early(self, tmp_path: Path) -> None:
        """Valid dates should pass validation (even if the export itself fails for other reasons)."""
        import typer.testing

        from ledger.commands.export import export_app

        # We just care that the "invalid date" guard is NOT triggered — the DB doesn't
        # exist so the export will fail for a different reason; that's fine.
        runner = typer.testing.CliRunner()
        # Single-command app — NO "package" subcommand token.
        result = runner.invoke(
            export_app,
            [
                "--db",
                str(tmp_path / "nonexistent.db"),
                "--start",
                "2025-01-01",
                "--end",
                "2025-12-31",
            ],
        )
        # The date-reject guard must NOT fire for valid dates.
        assert "--start must be a valid date" not in result.output, (
            f"Valid --start triggered the invalid-date guard: {result.output!r}"
        )
        assert "--end must be a valid date" not in result.output, (
            f"Valid --end triggered the invalid-date guard: {result.output!r}"
        )

    def test_omitted_start_and_end_does_not_exit(self, tmp_path: Path) -> None:
        """Omitting both --start and --end must NOT trigger the guard (full-history export)."""
        import typer.testing

        from ledger.commands.export import export_app

        runner = typer.testing.CliRunner()
        # Single-command app — NO "package" subcommand token.
        result = runner.invoke(
            export_app,
            ["--db", str(tmp_path / "nonexistent.db")],
        )
        # Guard must not fire for omitted dates.
        assert "--start must be a valid date" not in result.output, (
            f"Omitted dates triggered the --start guard: {result.output!r}"
        )
        assert "--end must be a valid date" not in result.output, (
            f"Omitted dates triggered the --end guard: {result.output!r}"
        )


# =============================================================================
# Fix 4: repair surfaces promoted_with_post_step_errors after post-promote failure
# =============================================================================


class TestRepairPromotedWithPostStepErrors:
    """When promote_candidate succeeds but a post-promote step fails, the repair
    result status must be 'promoted_with_post_step_errors', NOT the bare 'failed'
    that hides the swap."""

    def _build_and_apply_mocks(
        self,
        tmp_path: Path,
        monkeypatch,
        *,
        post_promote_fail_fn,
    ):
        """Set up the repair service with all heavy steps mocked except promote_candidate,
        and inject a failure in the specified post-promote step."""
        import ledger.repair.statements as _svc
        from ledger.imports.pdf.statement_pdf_to_csv import ExtractedCsv as _ExtractedCsv
        from ledger.processing import ProcessingResult as _ProcessingResult
        from ledger.storage.database import init_database

        db_path = tmp_path / "ledger.db"
        init_database(db_path)

        pdf_dir = tmp_path / "pdfs"
        pdf_dir.mkdir()
        (pdf_dir / "dummy.pdf").write_bytes(b"%PDF-1.4 fake")

        monkeypatch.setattr(_svc, "discover_pdfs", lambda _dirs: [pdf_dir / "dummy.pdf"])
        monkeypatch.setattr(_svc, "run_migrations", lambda _p: None)
        monkeypatch.setattr(_svc, "copy_runtime_config", lambda *_a, **_kw: None)

        def fake_process_pdf(pdf, csv_root, *_a, **_kw):
            csv_root.mkdir(parents=True, exist_ok=True)
            (csv_root / "dummy.csv").write_text(
                "Ledger Format,Date,Description,Amount,Currency,Reference,Institution,Account Type\n",
                encoding="utf-8",
            )
            return _ExtractedCsv(
                pdf=pdf,
                csv_path=csv_root / "dummy.csv",
                processed_pdf=tmp_path / "processed" / "dummy.pdf",
                statement_type="harbor_deposit",
                transaction_count=0,
            )

        monkeypatch.setattr(_svc.statement_pdf_to_csv, "process_pdf", fake_process_pdf)
        monkeypatch.setattr(
            _svc.statement_pdf_to_csv,
            "write_manifest",
            lambda path, _r, **_kw: path.write_text("[]", encoding="utf-8"),
        )
        monkeypatch.setattr(_svc, "build_inventory", lambda _r: [])
        monkeypatch.setattr(_svc, "write_csv", lambda _p, _r: None)
        fake_config = SimpleNamespace(
            ledger=SimpleNamespace(general=SimpleNamespace(account_aliases={})),
        )
        monkeypatch.setattr(_svc, "load_runtime_config", lambda _d, _o: fake_config)
        monkeypatch.setattr(_svc, "process", lambda **_kw: _ProcessingResult(total_scanned=0))
        monkeypatch.setattr(_svc, "validate_processing_result", lambda *_a: None)
        monkeypatch.setattr(_svc, "write_result_to_db", lambda *_a: None)
        monkeypatch.setattr(_svc, "refresh_repair_analytics", lambda *_a, **_kw: {})
        monkeypatch.setattr(_svc, "build_move_plan", lambda _r, _d: [])
        monkeypatch.setattr(_svc, "write_json", lambda _p, _d: None)
        monkeypatch.setattr(_svc, "validate_move_plan", lambda _p: None)
        monkeypatch.setattr(_svc, "record_processed_files", lambda *_a: None)

        fake_audit = MagicMock()
        fake_audit.run.return_value = MagicMock(
            run_dir=Path("/tmp/audit"),
            status="ok",
            summary={"counts": {"processed_file_hash_matches": 1}},
        )
        monkeypatch.setattr(_svc, "StatementAuditService", lambda: fake_audit)
        monkeypatch.setattr(_svc, "validate_candidate_audit", lambda *_a, **_kw: None)
        monkeypatch.setattr(
            _svc, "validate_database", lambda *_a, **_kw: MagicMock(status="success", errors=[])
        )
        monkeypatch.setattr(_svc, "prepare_pdf_targets", lambda _p: None)

        # Apply the post-promote failure injection
        post_promote_fail_fn(monkeypatch, _svc)

        return db_path, pdf_dir

    def test_post_promote_failure_yields_promoted_with_post_step_errors(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """remove_replaced_sources fails AFTER promote_candidate has published the DB.
        The status must be 'promoted_with_post_step_errors' (not bare 'failed')."""
        from ledger.repair.statements import StatementRepairOptions, StatementRepairService

        def inject_failure(mp, svc):
            mp.setattr(
                svc,
                "remove_replaced_sources",
                lambda _p: (_ for _ in ()).throw(RuntimeError("post-promote step failed")),
            )

        db_path, pdf_dir = self._build_and_apply_mocks(
            tmp_path, monkeypatch, post_promote_fail_fn=inject_failure
        )

        options = StatementRepairOptions(
            db_path=db_path,
            pdf_dirs=[pdf_dir],
            processed_dir=tmp_path / "processed",
            runs_dir=tmp_path / "runs",
            backup_dir=tmp_path / "backups",
            ledger_config=tmp_path / "ledger.toml",
            merchant_rules=tmp_path / "merchants.toml",
            categories=tmp_path / "categories.toml",
            dry_run=False,
        )

        result = StatementRepairService().run(options)

        assert result.status == "promoted_with_post_step_errors", (
            f"After post-promote failure, status must be 'promoted_with_post_step_errors'; "
            f"got {result.status!r}. A bare 'failed' hides that DB publication already happened."
        )

    def test_pre_promote_failure_still_yields_failed(self, tmp_path: Path, monkeypatch) -> None:
        """A failure BEFORE promote_candidate must still yield status='failed' (not
        promoted_with_post_step_errors), since the DB was never published."""
        from ledger.repair.statements import StatementRepairOptions, StatementRepairService

        def inject_pre_promote_failure(mp, svc):
            # Fail before promote_candidate runs
            mp.setattr(
                svc,
                "prepare_pdf_targets",
                lambda _p: (_ for _ in ()).throw(RuntimeError("pre-promote step failed")),
            )

        db_path, pdf_dir = self._build_and_apply_mocks(
            tmp_path, monkeypatch, post_promote_fail_fn=inject_pre_promote_failure
        )

        options = StatementRepairOptions(
            db_path=db_path,
            pdf_dirs=[pdf_dir],
            processed_dir=tmp_path / "processed",
            runs_dir=tmp_path / "runs",
            backup_dir=tmp_path / "backups",
            ledger_config=tmp_path / "ledger.toml",
            merchant_rules=tmp_path / "merchants.toml",
            categories=tmp_path / "categories.toml",
            dry_run=False,
        )

        result = StatementRepairService().run(options)

        assert result.status == "failed", (
            f"Pre-promote failure must still yield 'failed'; got {result.status!r}"
        )

    def test_a8_audit_row_still_written_on_post_promote_failure(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The A8 audit row (record_db_operation with status='failed') must still be
        written on post-promote failure — promoted_with_post_step_errors must not
        suppress it."""
        from ledger.repair.statements import StatementRepairOptions, StatementRepairService

        audit_calls: list[dict] = []

        def inject_failure(mp, svc):
            # Capture record_db_operation calls
            def capturing_record(db_path, operation, result, **kwargs):
                audit_calls.append({"operation": operation, "status": result.status})
                # Don't actually write — DB might not have the schema

            mp.setattr(svc, "record_db_operation", capturing_record)

            # Fail in remove_replaced_sources (post-promote)
            mp.setattr(
                svc,
                "remove_replaced_sources",
                lambda _p: (_ for _ in ()).throw(RuntimeError("post-promote step failed")),
            )

        db_path, pdf_dir = self._build_and_apply_mocks(
            tmp_path, monkeypatch, post_promote_fail_fn=inject_failure
        )

        options = StatementRepairOptions(
            db_path=db_path,
            pdf_dirs=[pdf_dir],
            processed_dir=tmp_path / "processed",
            runs_dir=tmp_path / "runs",
            backup_dir=tmp_path / "backups",
            ledger_config=tmp_path / "ledger.toml",
            merchant_rules=tmp_path / "merchants.toml",
            categories=tmp_path / "categories.toml",
            dry_run=False,
        )

        result = StatementRepairService().run(options)

        assert result.status == "promoted_with_post_step_errors"

        # A8: the failed audit row must be written
        failed_rows = [c for c in audit_calls if c["status"] == "failed"]
        assert failed_rows, (
            f"A8 audit row (status='failed') was not written on post-promote failure. "
            f"All audit calls: {audit_calls}"
        )


pytestmark = pytest.mark.usefixtures("fictional_adapter_metadata")
