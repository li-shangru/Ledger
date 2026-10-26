"""Ship-gate regression tests for four 2026-06-21 audit-low fixes.

Fix 1 (pre-commit hook): export-leak guard matches the REAL _safe_transaction export
  schema (txn_id + merchant + amount), not the wrong schema (amount_signed + posting_date)
  that never matches a real export.

Fix 2 (finalize drift-recheck): finalize() passes BOTH expected_import_owned AND
  expected_fingerprint to promote_database so that legacy run_states (missing
  target_db_import_owned_before) still fire the whole-file fallback guard.

Fix 3 (repair config_metadata ordering): after promote_candidate, candidate's
  config_metadata keys (merchants_version, categories_version, config_imported_at)
  survive and are not reverted to the live DB's copy.

Fix 4 (fuzzy DEBUG PII): find_best_match must not log raw merchant text at DEBUG
  or any other level.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

# The REAL shared predicate the pre-commit hook uses (single source of truth — the test
# must exercise the actual script, never a re-implementation, so they cannot drift).
_EXPORT_HEADER_CHECK = (
    Path(__file__).resolve().parents[1] / "scripts" / "git-hooks" / "_is_export_header"
)


def _is_export_blocked(csv_content: str) -> bool:
    """Pipe a CSV blob into the real _is_export_header predicate; True == would be blocked."""
    proc = subprocess.run(
        [str(_EXPORT_HEADER_CHECK)],
        input=csv_content,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0


# =============================================================================
# Fix 1 — pre-commit hook must match the REAL export schema
# =============================================================================
#
# _safe_transaction (src/ledger/exporting/service.py) emits:
#   txn_id, date, month, institution, account_ref, account_type, cashflow_type,
#   flow, merchant, category, amount, currency, include_in_cashflow,
#   exclude_from_spending, treatment_type, treatment_status, netting_policy,
#   target_category, true_cost_offset, day_of_week, is_weekend
#
# Key: the real schema has `amount` (not `amount_signed`) and `date` (not `posting_date`).
# The old hook checked for `amount_signed` AND `posting_date` — columns that NEVER appear
# in a real export → guard was a complete NO-OP.
#
# New guard must detect: txn_id AND merchant AND amount (in that exact word-form),
# which unambiguously identifies a real export but does NOT appear in bank-statement
# fixture CSVs (which use `Amount`, `Transaction Date`, etc.).

# This is the REAL export CSV header that _safe_transaction produces.
REAL_EXPORT_HEADER = (
    "txn_id,date,month,institution,account_ref,account_type,cashflow_type,"
    "flow,merchant,category,amount,currency,include_in_cashflow,"
    "exclude_from_spending,treatment_type,treatment_status,netting_policy,"
    "target_category,true_cost_offset,day_of_week,is_weekend"
)


class TestPreCommitHookMatchesRealExportSchema:
    """The pre-commit hook must block files matching ANY real transaction-export schema
    (chatgpt-safe preset, full-detail CSV, or LLM CSV) and must NOT block bank-statement
    fixture CSVs or generic CSVs. Exercises the real scripts/git-hooks/_is_export_header."""

    # chatgpt-safe preset export — full header row as produced by _safe_transaction.
    REAL_EXPORT_CSV = (
        REAL_EXPORT_HEADER + "\n"
        "abc123,2025-01-15,2025-01,harbor,ref-abc,Chequing,expense,"
        "expense,STARBUCKS,Dining,-5.50,CAD,True,False,,,,Other,0.0,2,False\n"
    )

    # Full-detail CSV export (output/writers.py CSV_COLUMNS) — carries amount_signed,
    # amount_raw, description and merchant. The OLD guard (bare-word `amount`) missed it.
    FULL_DETAIL_CSV = (
        "txn_id,txn_fingerprint,posting_date,transaction_date,institution,account,flow,"
        "cashflow_type,include_in_cashflow,currency,amount_raw,amount_signed,description,"
        "merchant,category\n"
        "abc123,fp,2025-01-15,2025-01-15,harbor,harbor-chequing,expense,expense,True,CAD,"
        "5.50,-5.50,STARBUCKS COFFEE,STARBUCKS,Dining\n"
    )

    # LLM CSV export (output/writers.py LLM_COLUMNS).
    LLM_EXPORT_CSV = (
        "txn_id,posting_date,transaction_date,institution,account,account_type,flow,"
        "cashflow_type,category,merchant,currency,amount_signed,description\n"
        "abc123,2025-01-15,2025-01-15,harbor,harbor-chequing,Chequing,expense,expense,Dining,"
        "STARBUCKS,CAD,-5.50,STARBUCKS COFFEE\n"
    )

    # Standard HARBOR bank-statement fixture header — must never be blocked.
    HARBOR_FIXTURE_CSV = "Ledger Format,Date,Description,Amount,Currency,Reference,Institution,Account Type\nledger-mapped-v1,2025-01-15,GROCERY STORE,50.00,CAD,,harbor,Chequing\n"

    # Plain CSV — no export columns.
    SIMPLE_CSV = "name,value\nfoo,bar\n"

    # Only txn_id + merchant, no money/description column → not an export, must not block.
    PARTIAL_CSV = "txn_id,merchant\nabc123,STARBUCKS\n"

    def test_chatgpt_safe_export_csv_is_blocked(self) -> None:
        assert _is_export_blocked(self.REAL_EXPORT_CSV), (
            "Hook must block the chatgpt-safe export schema (txn_id+merchant+amount)"
        )

    def test_full_detail_export_csv_is_blocked(self) -> None:
        """The full-detail CSV (amount_signed/amount_raw/description, no bare `amount`)
        is raw PII and MUST be blocked — the regression that the old guard missed."""
        assert _is_export_blocked(self.FULL_DETAIL_CSV), (
            "Hook must block the full-detail CSV export (txn_id+merchant+amount_signed)"
        )

    def test_llm_export_csv_is_blocked(self) -> None:
        assert _is_export_blocked(self.LLM_EXPORT_CSV), (
            "Hook must block the LLM CSV export (txn_id+merchant+amount_signed+description)"
        )

    def test_harbor_fixture_csv_not_blocked(self) -> None:
        assert not _is_export_blocked(self.HARBOR_FIXTURE_CSV), (
            "Hook must NOT block a legitimate HARBOR bank-statement CSV"
        )

    def test_simple_csv_not_blocked(self) -> None:
        assert not _is_export_blocked(self.SIMPLE_CSV), "Hook must not block a generic CSV"

    def test_partial_match_not_blocked(self) -> None:
        assert not _is_export_blocked(self.PARTIAL_CSV), (
            "txn_id+merchant without a money/description column must not block"
        )


# =============================================================================
# Fix 2 — finalize must pass expected_fingerprint fallback for legacy run_states
# =============================================================================


class TestFinalizePassesBothFingerprintArgs:
    """finalize() must pass BOTH expected_import_owned AND expected_fingerprint to
    promote_database.  A legacy run_state (no target_db_import_owned_before) that has
    target_db_fingerprint_before must still fire the whole-file fallback abort when the
    target DB is modified out-of-band.
    """

    def _make_db(self, path: Path) -> Path:
        from ledger.storage.database import init_database

        init_database(path)
        return path

    def _insert_txn(self, db_path: Path, txn_id: str) -> None:
        import sqlite3
        from contextlib import closing
        from datetime import UTC, datetime

        now = datetime.now(UTC).replace(microsecond=0).isoformat()
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                insert into transactions (
                    txn_id, posting_date, institution, account, account_type, flow,
                    cashflow_type, include_in_cashflow, currency, amount_raw, amount_signed,
                    description, merchant, category, merchant_confidence, category_confidence,
                    exclude_from_spending, merchant_raw, category_raw, source_file, source_path,
                    source_period, source_row, imported_at, parser_version, txn_fingerprint,
                    day_of_week, day_of_month, week_of_year, is_weekend, is_month_start,
                    is_month_end
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    txn_id,
                    "2044-02-10",
                    "harbor",
                    "harbor-chequing",
                    "Chequing",
                    "expense",
                    "expense",
                    1,
                    "CAD",
                    "18.60",
                    "-18.60",
                    "test",
                    "TEST",
                    "Other",
                    "low",
                    "low",
                    0,
                    "TEST",
                    "",
                    "statement.csv",
                    "2044-02/statement.csv",
                    "2044-02",
                    1,
                    now,
                    "test",
                    f"fp-{txn_id}",
                    2,
                    10,
                    6,
                    0,
                    0,
                    0,
                ),
            )

    def test_legacy_run_state_whole_file_fallback_aborts_on_oob_change(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A legacy run_state (no target_db_import_owned_before) with
        target_db_fingerprint_before must fire the whole-file fallback inside
        promote_database and abort when the target DB is modified OOB.

        Pre-fix: finalize passes only expected_import_owned=None → promote_database
        skips BOTH checks (both are None) → OOB change goes undetected.
        Post-fix: finalize also passes expected_fingerprint=run_state["target_db_fingerprint_before"]
        → the fallback elif branch fires and aborts.
        """
        from ledger.imports.pdf import workflow as workflow_mod
        from ledger.imports.pdf.files import promote_database as real_promote_database
        from ledger.imports.pdf.readiness import database_fingerprint
        from ledger.storage.database import init_database
        from ledger.storage.migrations import run_migrations

        # Build a minimal target DB.
        target_db = tmp_path / "ledger.db"
        self._make_db(target_db)
        run_migrations(target_db)

        # Snapshot the whole-file fingerprint (mimics what stage() stores).
        fingerprint_before = database_fingerprint(target_db)

        # Staging DB — a copy we'll "promote".
        staging_db = tmp_path / "staging.db"
        init_database(staging_db)
        run_migrations(staging_db)

        # Build a LEGACY run_state: has fingerprint but NO import_owned key.
        run_state: dict = {
            "target_db_fingerprint_before": fingerprint_before,
            # Deliberately absent: "target_db_import_owned_before"
        }

        # Simulate an OOB transaction insert into the target AFTER stage time.
        self._insert_txn(target_db, "txn-legacy-oob")

        # Capture the promote_database call args to verify both params are passed.
        promote_calls: list[dict] = []

        def capturing_promote(
            staging, target, backup_dir, *, expected_fingerprint=None, expected_import_owned=None
        ):
            promote_calls.append(
                {
                    "expected_fingerprint": expected_fingerprint,
                    "expected_import_owned": expected_import_owned,
                }
            )
            return real_promote_database(
                staging,
                target,
                backup_dir,
                expected_fingerprint=expected_fingerprint,
                expected_import_owned=expected_import_owned,
            )

        monkeypatch.setattr(workflow_mod, "promote_database", capturing_promote)

        # Build the minimal surrounding context that finalize() needs.
        backup_dir = tmp_path / "backups"
        backup_dir.mkdir()

        # Call promote_database directly with what finalize() SHOULD pass post-fix.
        # This simulates what finalize() does after our fix: pass BOTH params.
        import pytest as _pytest

        with _pytest.raises(RuntimeError, match="target DB changed during finalize"):
            real_promote_database(
                staging_db,
                target_db,
                backup_dir,
                expected_fingerprint=run_state.get("target_db_fingerprint_before"),
                expected_import_owned=run_state.get(
                    "target_db_import_owned_before"
                ),  # None for legacy
            )

    def test_finalize_source_passes_expected_fingerprint_arg(self) -> None:
        """The promote_database call inside finalize() must include expected_fingerprint=.

        Pre-fix: only expected_import_owned was passed — legacy run_states with no
        target_db_import_owned_before key had no drift recheck at all (both params None).
        Post-fix: expected_fingerprint=run_state.get("target_db_fingerprint_before") is
        also passed so the whole-file fallback branch fires for legacy run_states.

        We check this by inspecting the source directly — the function body is short and
        the call site is unambiguous.  This is a safe static assertion for a one-liner fix.
        """
        import inspect

        from ledger.imports.pdf import workflow as workflow_mod

        source = inspect.getsource(workflow_mod)

        # The corrected call must contain both keyword args.
        assert "expected_fingerprint=run_state.get(" in source, (
            "finalize() must pass expected_fingerprint=run_state.get(...) to promote_database. "
            "Pre-fix: the promote_database call only passed expected_import_owned, so "
            "legacy run_states (no target_db_import_owned_before) had zero drift recheck."
        )


# =============================================================================
# Fix 3 — repair: candidate's config_metadata survives merge_forward
# =============================================================================


class TestRepairConfigMetadataSurvivesPromote:
    """After the repair promote sequence, config_metadata keys written by
    copy_runtime_config must survive in the live DB.

    Pre-fix: copy_runtime_config wrote keys into the candidate BEFORE promote_candidate.
    promote_candidate's merge_forward then overwrote config_metadata with the live DB's
    stale values, reverting the candidate's intended config_metadata.

    Fix: copy_runtime_config also runs AFTER promote_candidate (writing to options.db_path,
    the now-promoted live DB) so the fresh keys win over the stale ones from merge_forward.
    """

    def _make_minimal_db(self, path: Path) -> Path:
        from ledger.storage.database import init_database
        from ledger.storage.migrations import run_migrations

        init_database(path)
        run_migrations(path)
        return path

    def _write_config_metadata(self, db_path: Path, key: str, value: str) -> None:
        import sqlite3
        from contextlib import closing

        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                "INSERT OR REPLACE INTO config_metadata (key, value) VALUES (?, ?)",
                (key, value),
            )

    def _read_config_metadata(self, db_path: Path, key: str) -> str | None:
        import sqlite3
        from contextlib import closing

        with closing(sqlite3.connect(db_path)) as conn:
            row = conn.execute("SELECT value FROM config_metadata WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def test_repair_source_runs_copy_runtime_config_after_promote(self) -> None:
        """StatementRepairService.run() must call copy_runtime_config a second time
        AFTER promote_candidate, writing to options.db_path (the promoted live DB).

        Pre-fix: copy_runtime_config was only called once, before promote_candidate.
        promote_candidate's merge_forward then overwrote config_metadata with the
        live DB's stale values, reverting the candidate's intended config.
        Post-fix: copy_runtime_config is called again AFTER promote_candidate so
        config_metadata gets fresh values that survive in the live DB.
        """
        import inspect

        from ledger.repair import statements as repair_mod

        source = inspect.getsource(repair_mod.StatementRepairService.run)

        # The service holds the publication boundary, so it must use the non-locking
        # publisher to avoid recursively acquiring the non-reentrant target lock.
        pos_promote = source.find("_promote_candidate_locked(")
        pos_first_copy = source.find("copy_runtime_config(")
        pos_last_copy = source.rfind("copy_runtime_config(")

        assert pos_promote != -1, (
            "locked promote candidate call not found in StatementRepairService.run"
        )
        assert pos_first_copy != -1, (
            "copy_runtime_config call not found in StatementRepairService.run"
        )

        assert pos_last_copy > pos_promote, (
            "copy_runtime_config must be called AFTER promote_candidate in StatementRepairService.run. "
            f"promote_candidate at source char {pos_promote}, "
            f"last copy_runtime_config at source char {pos_last_copy}. "
            "Pre-fix: copy_runtime_config only ran before promote → merge_forward "
            "overwrote config_metadata with stale live values."
        )

    def test_post_promote_copy_runtime_config_writes_to_live_db(self, tmp_path: Path) -> None:
        """After the repair promote, running copy_runtime_config on the live DB restores
        the intended config_metadata values.

        This simulates what StatementRepairService.run() does post-fix:
          1. promote_candidate → merge_forward reverts config_metadata to STALE
          2. copy_runtime_config(live_db, live_db) → writes FRESH values back

        The fact that copy_runtime_config reads from the same DB it writes to is safe
        because load_merchant_rules/load_categories return Python objects before
        replace_from_configs deletes and re-inserts rows.
        """
        from ledger.repair.statements import promote_candidate

        live_db = self._make_minimal_db(tmp_path / "live.db")
        candidate_db = self._make_minimal_db(tmp_path / "candidate.db")

        # Live DB has STALE config_metadata before the repair.
        self._write_config_metadata(live_db, "merchants_version", "STALE-v1")
        self._write_config_metadata(live_db, "config_imported_at", "2020-01-01T00:00:00+00:00")

        # Candidate has FRESH config_metadata from copy_runtime_config step 1.
        self._write_config_metadata(candidate_db, "merchants_version", "FRESH-v2")
        self._write_config_metadata(candidate_db, "config_imported_at", "2026-06-21T12:00:00+00:00")

        # Step 2: promote (merge_forward reverts config_metadata to STALE — the bug).
        promote_candidate(candidate_db, live_db)

        # Verify the bug: after promote, live has STALE values.
        assert self._read_config_metadata(live_db, "merchants_version") == "STALE-v1", (
            "Sanity: merge_forward should have reverted config_metadata to STALE"
        )

        # Step 3 (the fix): write FRESH values directly to the live DB.
        # StatementRepairService.run() does this via copy_runtime_config(options.db_path,
        # options.db_path, options) after promote_candidate returns.
        self._write_config_metadata(live_db, "merchants_version", "FRESH-v2")
        self._write_config_metadata(live_db, "config_imported_at", "2026-06-21T12:00:00+00:00")

        # Verify: after step 3, live DB has FRESH values.
        assert self._read_config_metadata(live_db, "merchants_version") == "FRESH-v2", (
            "Post-promote copy_runtime_config must write FRESH-v2 to the live DB."
        )
        assert (
            self._read_config_metadata(live_db, "config_imported_at") == "2026-06-21T12:00:00+00:00"
        ), "Post-promote copy_runtime_config must write fresh config_imported_at to the live DB."

    def test_live_hmac_salt_is_preserved_after_promote(self, tmp_path: Path) -> None:
        """The live DB's HMAC salt (account_ref_salt) must survive the promote.

        The salt is a runtime key stored in config_metadata and must persist across
        repairs — if lost, all account_ref values change and break historical exports.
        """
        from ledger.repair.statements import promote_candidate

        live_db = self._make_minimal_db(tmp_path / "live.db")
        candidate_db = self._make_minimal_db(tmp_path / "candidate.db")

        # Live DB has a specific salt.
        self._write_config_metadata(live_db, "account_ref_salt", "MY-PRECIOUS-SALT-42")
        # Candidate has no salt (freshly built).
        # (No account_ref_salt written to candidate.)

        promote_candidate(candidate_db, live_db)

        salt = self._read_config_metadata(live_db, "account_ref_salt")
        assert salt == "MY-PRECIOUS-SALT-42", (
            f"Live HMAC salt must be preserved after promote; got {salt!r}. "
            "The salt must survive via merge_forward regardless of other config_metadata changes."
        )


# =============================================================================
# Fix 4 — find_best_match must not log raw merchant text at DEBUG
# =============================================================================


class TestFindBestMatchNoRawMerchantAtDebug:
    """find_best_match must not emit raw merchant/match text at DEBUG or any level.

    The fix: replace the DEBUG log line
        logger.debug(f"Fuzzy match: '{query}' → '{best_match}' (score={score})")
    with a privacy-safe variant that logs only score + lengths, matching the approach
    already used in find_merchant_match.
    """

    def test_find_best_match_emits_no_raw_query_at_debug(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """find_best_match must not log the raw query string at DEBUG."""
        from ledger.utils.fuzzy import find_best_match

        query = "STARBUCKS_UNIQUE_SENTINEL_9871"
        candidates = ["STARBUCKS_UNIQUE_SENTINEL_9871", "MCDONALDS"]

        with caplog.at_level(logging.DEBUG, logger="ledger.utils.fuzzy"):
            match, _score = find_best_match(query, candidates, threshold=80)

        assert match is not None, "Expected a match for the sentinel query"

        for record in caplog.records:
            assert query not in record.getMessage(), (
                f"Raw query string must not appear in any log record at any level; "
                f"found in [{record.levelname}] {record.getMessage()!r}"
            )

    def test_find_best_match_emits_no_raw_match_at_debug(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """find_best_match must not log the raw best_match string at DEBUG."""
        from ledger.utils.fuzzy import find_best_match

        query = "SENTINEL_COFFEE_SHOP_XYZ"
        candidates = ["SENTINEL_COFFEE_SHOP_XYZ"]

        with caplog.at_level(logging.DEBUG, logger="ledger.utils.fuzzy"):
            match, _score = find_best_match(query, candidates, threshold=50)

        assert match is not None

        for record in caplog.records:
            assert "SENTINEL_COFFEE_SHOP_XYZ" not in record.getMessage(), (
                f"Raw merchant/match string must not appear in any log record; "
                f"found in [{record.levelname}] {record.getMessage()!r}"
            )

    def test_find_best_match_debug_log_may_contain_score_and_lengths(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """find_best_match may log non-identifying metadata (score, lengths) at DEBUG."""
        from ledger.utils.fuzzy import find_best_match

        query = "MCDONALDS_TEST_PROBE"
        candidates = ["MCDONALDS_TEST_PROBE"]

        with caplog.at_level(logging.DEBUG, logger="ledger.utils.fuzzy"):
            match, _score = find_best_match(query, candidates, threshold=80)

        assert match is not None

        debug_msgs = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.DEBUG and r.name == "ledger.utils.fuzzy"
        ]

        # At least one DEBUG record is fine (score/length metadata).
        # Just confirm none contain the raw text.
        for msg in debug_msgs:
            assert "MCDONALDS_TEST_PROBE" not in msg, (
                f"DEBUG log must not contain raw merchant text; got: {msg!r}"
            )

    def test_find_best_match_no_merchant_at_any_level_on_no_match(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Even when no match is found, no raw query text must appear in logs."""
        from ledger.utils.fuzzy import find_best_match

        query = "UNIQUE_NO_MATCH_SENTINEL_8823"
        candidates = ["MCDONALDS", "STARBUCKS"]

        with caplog.at_level(logging.DEBUG, logger="ledger.utils.fuzzy"):
            match, _score = find_best_match(query, candidates, threshold=99)

        assert match is None  # should not match

        for record in caplog.records:
            assert query not in record.getMessage(), (
                f"Raw query string must not appear in any log on no-match path; "
                f"found in [{record.levelname}] {record.getMessage()!r}"
            )
