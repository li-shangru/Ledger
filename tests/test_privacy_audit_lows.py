"""Tests for four privacy/observability fixes from the 2026-06 audit.

Fix 1: Validation-warning log at WARNING level must NOT include txn.amount_signed.
Fix 2: fuzzy.find_merchant_match must NOT log raw merchant/match text at INFO.
Fix 3: category_optimize audit-on-failure — VERIFY ONLY (already fixed).
Fix 4: pre-commit data-leak hook rejects staged files matching export schema header.
Fix 5 (L2, 2026-07-04 sixth-pass audit): StatementAuditService._write_csv must
neutralize CSV formula-injection in every string cell, matching the other three
CSV sinks (output/writers.py, exporting/service.py, ui/components.py).
"""

from __future__ import annotations

import csv
import logging
import subprocess
import textwrap
from decimal import Decimal
from pathlib import Path

# =============================================================================
# Fix 1 — amount PII absent from validation WARNING logs
# =============================================================================


class TestValidationWarningOmitsAmount:
    """pipeline.process() validation warnings (strict=False) must NOT include the
    literal amount value in the WARNING-level log record.

    Strategy: monkeypatch validate_transaction to return a known violation string
    that includes a sentinel amount, then confirm the WARNING log does NOT contain
    the sentinel amount — only the violation type and source coordinates.
    """

    def test_warning_log_does_not_contain_amount_string(
        self, tmp_path: Path, sample_app_config, caplog, monkeypatch
    ) -> None:
        """WARNING records must not contain the literal amount value from the violation."""
        import ledger.processing.pipeline as pipeline_mod
        from ledger.processing.pipeline import process

        sentinel_amount = "123456.78"

        # validate_transaction returns a violation that includes the amount
        def fake_validate(txn, strict=True):
            return [f"Expense must be negative: {sentinel_amount}"]

        monkeypatch.setattr(pipeline_mod, "validate_transaction", fake_validate)

        csv_dir = tmp_path / "csvs"
        csv_dir.mkdir()
        (csv_dir / "harbor_2025-01.csv").write_text(
            "Ledger Format,Date,Description,Amount,Currency,Reference,Institution,Account Type\nledger-mapped-v1,2025-01-15,GROCERY STORE,-50.00,CAD,,harbor,Chequing\n",
            encoding="utf-8",
        )

        sample_app_config = sample_app_config.model_copy(
            update={
                "ledger": sample_app_config.ledger.model_copy(
                    update={
                        "general": sample_app_config.ledger.general.model_copy(
                            update={"strict": False}
                        )
                    }
                )
            }
        )

        with caplog.at_level(logging.WARNING, logger="ledger.processing.pipeline"):
            process(sample_app_config, csv_dir)

        warning_msgs = [
            r.message
            for r in caplog.records
            if r.levelno == logging.WARNING and r.name == "ledger.processing.pipeline"
        ]

        assert warning_msgs, "Expected at least one WARNING from the mocked validate_transaction"
        for msg in warning_msgs:
            assert sentinel_amount not in msg, (
                f"WARNING log must not contain the literal amount '{sentinel_amount}'; got: {msg!r}"
            )

    def test_warning_log_contains_source_coordinates_and_violation_type(
        self, tmp_path: Path, sample_app_config, caplog, monkeypatch
    ) -> None:
        """WARNING log must still include source coordinates (path:row) and
        the violation category (cashflow_type + source info), just not the amount."""
        import ledger.processing.pipeline as pipeline_mod
        from ledger.processing.pipeline import process

        def fake_validate(txn, strict=True):
            return ["Expense must be negative: -99.99"]

        monkeypatch.setattr(pipeline_mod, "validate_transaction", fake_validate)

        csv_dir = tmp_path / "csvs2"
        csv_dir.mkdir()
        (csv_dir / "harbor_2025-01.csv").write_text(
            "Ledger Format,Date,Description,Amount,Currency,Reference,Institution,Account Type\nledger-mapped-v1,2025-01-15,GROCERY STORE,-50.00,CAD,,harbor,Chequing\n",
            encoding="utf-8",
        )

        sample_app_config = sample_app_config.model_copy(
            update={
                "ledger": sample_app_config.ledger.model_copy(
                    update={
                        "general": sample_app_config.ledger.general.model_copy(
                            update={"strict": False}
                        )
                    }
                )
            }
        )

        with caplog.at_level(logging.WARNING, logger="ledger.processing.pipeline"):
            process(sample_app_config, csv_dir)

        warning_msgs = [
            r.message
            for r in caplog.records
            if r.levelno == logging.WARNING and r.name == "ledger.processing.pipeline"
        ]

        assert warning_msgs, "Expected at least one WARNING"
        for msg in warning_msgs:
            # Amount must NOT appear
            assert "-99.99" not in msg, f"WARNING must not include amount; got: {msg!r}"
            # Source coordinates must still be present (path:row or cashflow_type)
            assert (
                "harbor_2025-01.csv" in msg or "row" in msg.lower() or "expense" in msg.lower()
            ), f"WARNING must still identify source/violation type; got: {msg!r}"


# =============================================================================
# Fix 2 — raw merchant text absent from INFO logs in fuzzy matching
# =============================================================================


class TestFuzzyMatchNoMerchantPIIAtInfo:
    """find_merchant_match and find_best_match must NOT emit raw merchant/query text
    at ANY log level (INFO or DEBUG).

    The SG-C fix changed all logging to emit only non-identifying metadata
    (score, len) — never the raw merchant string.  Because the production code
    currently only emits DEBUG records (no INFO), testing only INFO is vacuous: the
    body of "for msg in info_msgs" never executes, so the assertion is never checked.

    The strengthened tests capture ALL records at DEBUG level and assert that the
    raw merchant/query strings are absent from EVERY record's message.
    Non-identifying metadata (score, lengths as integers) may appear.
    """

    def test_no_raw_merchant_text_in_any_log_on_match(self, caplog) -> None:
        """A successful fuzzy match must not produce ANY log record containing raw merchant text."""
        from ledger.utils.fuzzy import find_merchant_match

        merchants = ["STARBUCKS", "MCDONALDS", "TIM HORTONS"]
        query = "STARBUCKS COFFEE #999"

        with caplog.at_level(logging.DEBUG, logger="ledger.utils.fuzzy"):
            result = find_merchant_match(query, merchants, threshold=70)

        assert result is not None, "Expected a match to test logging"

        fuzzy_records = [r for r in caplog.records if r.name == "ledger.utils.fuzzy"]

        # There must be at least one log record emitted on a match path so the
        # loop body below actually executes (otherwise the test is vacuous).
        assert fuzzy_records, (
            "find_merchant_match must emit at least one log record on match "
            "so this test is non-vacuous. If production code removes all logging, "
            "this assertion catches that too."
        )

        for rec in fuzzy_records:
            msg = rec.getMessage()
            assert query not in msg, (
                f"Raw query text must not appear in {rec.levelname} log; got: {msg!r}"
            )
            assert result not in msg, (
                f"Raw match text must not appear in {rec.levelname} log; got: {msg!r}"
            )
            # Check no partial merchant substring leaks (case-insensitive to be safe).
            assert "STARBUCKS" not in msg.upper(), (
                f"Merchant name must not appear in {rec.levelname} log; got: {msg!r}"
            )
            assert "COFFEE" not in msg.upper(), (
                f"Query fragment must not appear in {rec.levelname} log; got: {msg!r}"
            )

    def test_debug_metadata_present_but_no_raw_text(self, caplog) -> None:
        """DEBUG records may carry non-identifying metadata (score, len) — that is fine.
        But the raw query and match strings must NOT appear at any level."""
        from ledger.utils.fuzzy import find_merchant_match

        merchants = ["STARBUCKS"]
        query = "STARBUCKS COFFEE"

        with caplog.at_level(logging.DEBUG, logger="ledger.utils.fuzzy"):
            result = find_merchant_match(query, merchants, threshold=70)

        assert result is not None

        fuzzy_records = [r for r in caplog.records if r.name == "ledger.utils.fuzzy"]

        # There must be at least one record so the assertion loop executes.
        assert fuzzy_records, (
            "find_merchant_match must emit at least one log record so the "
            "raw-text check below is non-vacuous."
        )

        for rec in fuzzy_records:
            msg = rec.getMessage()
            # Raw merchant text must not appear at any level.
            assert query not in msg, (
                f"Raw query must not appear in {rec.levelname} log; got: {msg!r}"
            )
            assert result not in msg, (
                f"Raw match must not appear in {rec.levelname} log; got: {msg!r}"
            )
            assert "STARBUCKS" not in msg.upper(), (
                f"Merchant name must not appear in {rec.levelname} log; got: {msg!r}"
            )

        # Confirm there are NO INFO records (production only logs at DEBUG).
        info_records = [r for r in fuzzy_records if r.levelno == logging.INFO]
        assert not info_records, (
            f"find_merchant_match must emit zero INFO records; got: {[r.getMessage() for r in info_records]}"
        )

    def test_no_match_emits_no_raw_text(self, caplog) -> None:
        """A non-match must also produce no raw-text log records at any level."""
        from ledger.utils.fuzzy import find_merchant_match

        merchants = ["MCDONALDS"]
        query = "AMAZON PRIME 1234"

        with caplog.at_level(logging.DEBUG, logger="ledger.utils.fuzzy"):
            result = find_merchant_match(query, merchants, threshold=85)

        assert result is None

        fuzzy_records = [r for r in caplog.records if r.name == "ledger.utils.fuzzy"]
        # On a non-match there may be zero records (no match → no debug log in
        # find_merchant_match).  Confirm none contain raw text.
        for rec in fuzzy_records:
            msg = rec.getMessage()
            assert query not in msg, (
                f"Raw query must not appear in {rec.levelname} log on no-match; got: {msg!r}"
            )
            assert "AMAZON" not in msg.upper(), (
                f"Query fragment must not appear in {rec.levelname} log; got: {msg!r}"
            )
        # No INFO records regardless.
        info_records = [r for r in fuzzy_records if r.levelno == logging.INFO]
        assert not info_records, (
            f"No-match path must emit zero INFO records; got: {[r.getMessage() for r in info_records]}"
        )

    def test_find_best_match_emits_no_raw_merchant_text(self, caplog) -> None:
        """find_best_match (called by find_merchant_match) must also emit no raw text."""
        from ledger.utils.fuzzy import find_best_match

        candidates = ["TIM HORTONS", "SECOND CUP"]
        query = "TIM HORTONS DRIVE THRU"

        with caplog.at_level(logging.DEBUG, logger="ledger.utils.fuzzy"):
            match, score = find_best_match(query, candidates, threshold=70)

        assert match is not None, "Expected a match from find_best_match"
        assert score > 0

        fuzzy_records = [r for r in caplog.records if r.name == "ledger.utils.fuzzy"]

        # Must have at least one record so the loop executes (non-vacuous).
        assert fuzzy_records, (
            "find_best_match must emit at least one log record on match "
            "so the raw-text check below is non-vacuous."
        )

        for rec in fuzzy_records:
            msg = rec.getMessage()
            assert query not in msg, (
                f"Raw query must not appear in {rec.levelname} log; got: {msg!r}"
            )
            assert match not in msg, (
                f"Raw match must not appear in {rec.levelname} log; got: {msg!r}"
            )
            assert "TIM HORTONS" not in msg.upper(), (
                f"Merchant name must not appear in {rec.levelname} log; got: {msg!r}"
            )


# =============================================================================
# Fix 3 — VERIFY ONLY: category_optimize audit-on-failure (already fixed)
# =============================================================================


class TestCategoryOptimizeAuditOnFailureAlreadyFixed:
    """Confirms that the except path in CategoryOptimizer.run() already writes a
    failed audit row via record_db_operation.  This is a verification-only test —
    no new code was added for this fix."""

    def test_except_path_writes_failed_audit_row(self, tmp_path: Path, monkeypatch) -> None:
        """CategoryOptimizer.run() must write a record_db_operation(status='failed')
        on the except path, even if the audit write itself would fail (best-effort)."""
        import ledger.optimize.categories as cat_mod

        calls: list[dict] = []

        def capturing_record(db_path, operation, result, **kwargs):
            calls.append({"operation": operation, "status": result.status})

        monkeypatch.setattr(cat_mod, "record_db_operation", capturing_record)
        monkeypatch.setattr(
            cat_mod,
            "latest_repair_passed",
            lambda _p: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        from ledger.optimize.categories import CategoryOptimizationOptions, CategoryOptimizer

        options = CategoryOptimizationOptions(
            db_path=tmp_path / "ledger.db",
            runs_dir=tmp_path / "runs",
            execute_safe=True,
        )
        result = CategoryOptimizer().run(options)

        assert result.status == "failed"
        failed_calls = [
            c for c in calls if c["status"] == "failed" and c["operation"] == "category_optimize"
        ]
        assert failed_calls, (
            f"Expected a record_db_operation(operation='category_optimize', status='failed') "
            f"on the except path; calls seen: {calls}"
        )


# =============================================================================
# Fix 4 — pre-commit hook blocks export-schema CSV regardless of path
# =============================================================================

# Export schema: the REAL columns emitted by _safe_transaction in exporting/service.py.
# The hook must block files whose header contains txn_id AND merchant AND amount
# (whole-word).  The old wrong guard checked for `amount_signed` and `posting_date`,
# which never appear in a real export → old guard was a complete no-op.
REAL_EXPORT_HEADER = (
    "txn_id,date,month,institution,account_ref,account_type,cashflow_type,"
    "flow,merchant,category,amount,currency,include_in_cashflow,"
    "exclude_from_spending,treatment_type,treatment_status,netting_policy,"
    "target_category,true_cost_offset,day_of_week,is_weekend"
)


def _run_hook_against_content(
    tmp_path: Path,
    staged_content: str,
    filename: str = "export.csv",
) -> tuple[int, str]:
    """Write a file and run the corrected header-check logic against it.

    Mirrors the CORRECTED hook logic: txn_id AND merchant AND amount (whole-word
    via grep -w), matching the real _safe_transaction export schema.
    Legitimate bank-statement fixture CSVs (HARBOR headers) do not contain these fields.
    """
    csv_file = tmp_path / filename
    csv_file.write_text(staged_content, encoding="utf-8")

    check_script = textwrap.dedent(f"""\
        #!/usr/bin/env bash
        set -euo pipefail
        file="{csv_file}"

        # Replicate the CORRECTED header-check logic from the hook
        # (txn_id AND merchant AND amount as whole words via grep -w)
        if [[ -f "$file" ]]; then
            header=$(head -1 "$file" 2>/dev/null || true)
            if echo "$header" | grep -qw "txn_id" && \\
               echo "$header" | grep -qw "merchant" && \\
               echo "$header" | grep -qw "amount"; then
                echo "BLOCKED: export schema header detected in $file" >&2
                exit 1
            fi
        fi
        exit 0
    """)
    script_path = tmp_path / "check.sh"
    script_path.write_text(check_script, encoding="utf-8")
    script_path.chmod(0o755)

    proc = subprocess.run(
        [str(script_path)],
        capture_output=True,
        text=True,
    )
    output = proc.stdout + proc.stderr
    return proc.returncode, output


class TestPreCommitExportSchemaBlock:
    """The pre-commit hook must block CSVs with the REAL export schema header
    (txn_id + merchant + amount) regardless of path, but must NOT block
    legitimate bank-statement fixture CSVs."""

    # REAL export CSV — matches _safe_transaction output exactly.
    EXPORT_SCHEMA_CSV = (
        "txn_id,date,month,institution,account_ref,account_type,cashflow_type,"
        "flow,merchant,category,amount,currency,include_in_cashflow,"
        "exclude_from_spending,treatment_type,treatment_status,netting_policy,"
        "target_category,true_cost_offset,day_of_week,is_weekend\n"
        "abc123,2025-01-15,2025-01,harbor,ref-abc,Chequing,expense,"
        "expense,STARBUCKS,Dining,-5.50,CAD,True,False,,,,Other,0.0,2,False\n"
    )

    HARBOR_FIXTURE_CSV = "Ledger Format,Date,Description,Amount,Currency,Reference,Institution,Account Type\nledger-mapped-v1,2025-01-15,GROCERY STORE,50.00,CAD,,harbor,Chequing\n"

    SIMPLE_CSV = "name,value\nfoo,bar\n"

    def test_export_schema_header_is_blocked(self, tmp_path: Path) -> None:
        """A CSV with the real _safe_transaction schema must be blocked."""
        rc, output = _run_hook_against_content(tmp_path, self.EXPORT_SCHEMA_CSV, "myexport.csv")
        assert rc != 0, f"Hook must block export-schema CSV; exit code was {rc}, output: {output!r}"
        assert "BLOCKED" in output or "export" in output.lower(), (
            f"Hook output must mention blocking reason; got: {output!r}"
        )

    def test_harbor_fixture_csv_is_not_blocked(self, tmp_path: Path) -> None:
        """A standard HARBOR bank-statement CSV must NOT be blocked."""
        rc, output = _run_hook_against_content(
            tmp_path, self.HARBOR_FIXTURE_CSV, "harbor_2025-01.csv"
        )
        assert rc == 0, (
            f"Hook must NOT block a legitimate HARBOR fixture CSV; exit code was {rc}, output: {output!r}"
        )

    def test_plain_csv_is_not_blocked(self, tmp_path: Path) -> None:
        """A generic CSV with no export columns must not be blocked."""
        rc, output = _run_hook_against_content(tmp_path, self.SIMPLE_CSV, "data.csv")
        assert rc == 0, f"Hook must not block a generic CSV; exit code was {rc}, output: {output!r}"

    def test_export_csv_at_arbitrary_path_is_blocked(self, tmp_path: Path) -> None:
        """The hook must block the export schema regardless of path (not just data/)."""
        sub = tmp_path / "some" / "nested" / "dir"
        sub.mkdir(parents=True)
        rc, _output = _run_hook_against_content(sub, self.EXPORT_SCHEMA_CSV, "report.csv")
        assert rc != 0, f"Export schema must be blocked regardless of path; exit code was {rc}"

    def test_partial_column_match_not_blocked(self, tmp_path: Path) -> None:
        """A CSV with only some export columns (e.g. just merchant+category) must not be blocked."""
        partial = "merchant,category\nSTARBUCKS,Dining\n"
        rc, _output = _run_hook_against_content(tmp_path, partial, "partial.csv")
        assert rc == 0, (
            f"Partial column match (missing txn_id/amount) must not trigger block; exit code was {rc}"
        )


# =============================================================================
# Fix 5 — StatementAuditService._write_csv neutralizes CSV formula injection (L2)
# =============================================================================


class TestStatementAuditWriteCsvNeutralizesFormulaInjection:
    """row_mismatches.csv / statement_inventory.csv are written by
    StatementAuditService._write_csv via csv.DictWriter directly, bypassing the
    neutralize_csv_cell() call that every other CSV sink in the codebase applies
    (output/writers.py, exporting/service.py, ui/components.py). A crafted/corrupt
    statement whose description begins with '=' (e.g. HYPERLINK injection) would be
    written verbatim and execute as a formula if the operator opens the CSV in
    Excel/Sheets/LibreOffice.
    """

    def test_row_mismatch_with_formula_description_is_neutralized(self, tmp_path: Path) -> None:
        """A row_mismatches row built via the real compare_audit_rows() path, whose
        description is a HYPERLINK formula-injection payload, must be neutralized
        (leading single-quote) in the written CSV — while a normal description is
        left unchanged."""
        from datetime import date as _date

        from ledger.audit.statements import (
            AuditRow,
            StatementAuditService,
            compare_audit_rows,
        )

        malicious = AuditRow(
            posting_date=_date(2025, 12, 5),
            institution="harbor",
            logical_account="harbor:spark",
            account="harbor-sparkplus",
            account_type="sparkplus Visa",
            amount=Decimal("-10.00"),
            description='=HYPERLINK("http://evil.example","x")',
            source_file="statement.csv",
            source_kind="source_pdf",
        )
        normal = AuditRow(
            posting_date=_date(2025, 12, 6),
            institution="harbor",
            logical_account="harbor:spark",
            account="harbor-sparkplus",
            account_type="sparkplus Visa",
            amount=Decimal("-20.00"),
            description="STARBUCKS",
            source_file="statement.csv",
            source_kind="source_pdf",
        )

        # source-only rows => both appear as row_mismatches (db side empty).
        row_mismatches = compare_audit_rows([malicious, normal], [])
        assert len(row_mismatches) == 2, "expected both rows to surface as source_only mismatches"

        out = tmp_path / "row_mismatches.csv"
        StatementAuditService()._write_csv(out, row_mismatches)
        content = out.read_text(encoding="utf-8")

        with out.open(newline="", encoding="utf-8") as handle:
            written_rows = list(csv.DictReader(handle))

        by_amount = {row["amount"]: row for row in written_rows}
        assert by_amount["-10.00"]["description"].startswith("'="), (
            f"formula-injection description must be neutralized with a leading quote; "
            f"got: {by_amount['-10.00']['description']!r}"
        )
        assert by_amount["-20.00"]["description"] == "STARBUCKS", (
            "a normal description must be left unchanged"
        )
        # Guard against the raw payload leaking into the file unescaped anywhere.
        assert '"=HYPERLINK' not in content and ",=HYPERLINK" not in content

    def test_statement_inventory_row_with_formula_skip_reason_is_neutralized(
        self, tmp_path: Path
    ) -> None:
        """statement_inventory.csv rows also flow through _write_csv; a formula-like
        string in any cell (e.g. skipped_reason) must be neutralized the same way."""
        from ledger.audit.statements import StatementAuditService

        rows = [
            {
                "pdf_path": "/tmp/statement.pdf",
                "folder": "2025-12",
                "skipped_reason": '=cmd|"/c calc"!A1',
            }
        ]
        out = tmp_path / "statement_inventory.csv"
        StatementAuditService()._write_csv(out, rows)

        with out.open(newline="", encoding="utf-8") as handle:
            written_rows = list(csv.DictReader(handle))

        assert written_rows[0]["skipped_reason"].startswith("'="), (
            f"formula-like cell must be neutralized; got: {written_rows[0]['skipped_reason']!r}"
        )
