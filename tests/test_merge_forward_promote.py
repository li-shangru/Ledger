"""Tests for targeted merge-forward of runtime tables before promote.

Design reference: A4/A6 import-safety core.

Tests are written FIRST (TDD) and must FAIL before implementation.

Coverage:
 1. Budget edit during finalize PRESERVED (not aborted)
 2. Import-owned change still ABORTS
 3. import_owned_fingerprint unit tests
 4. A6 survives merge-forward (import-owned equality, not sha256)
 5. End-to-end: stage+N txns, edit budgets+goals live, finalize → merged result
 6. Merge went into temp not staging (A6 invariant guard)
 7. Repair parity: budget edit during repair preserved; import-owned change aborts
 8. Merge/abort failure leaves live intact + temp cleaned
 9. INVARIANT: excluded tables == MERGE_FORWARD_TABLES exactly
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from import_binding_fixtures import bound_import_state

from ledger.imports.pdf import workflow
from ledger.imports.pdf.files import promote_database, write_run_state
from ledger.imports.pdf.paths import Paths
from ledger.imports.pdf.readiness import database_fingerprint
from ledger.processing import ProcessingResult
from ledger.storage.database import init_database

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def insert_minimal_transaction(db_path: Path, txn_id: str) -> None:
    """Insert a minimal transaction row via raw sqlite3 (no SQLAlchemy)."""
    now = _now()
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
            ) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
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
                "Test transaction",
                "SYNTHETIC ACORN",
                "Other",
                "low",
                "low",
                0,
                "SYNTHETIC ACORN CAFE",
                "",
                "statement.csv",
                "2044-02/statement.csv",
                "2044-02",
                1,
                now,
                "test",
                f"fingerprint-{txn_id}",
                2,
                10,
                6,
                0,
                0,
                0,
            ),
        )


def insert_budget(db_path: Path, category: str, amount: str = "500.00", month: str = "*") -> None:
    """Insert a budget row directly via sqlite3."""
    now = _now()
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO budgets (budget_month, category, amount, notes, created_at, updated_at)"
            " VALUES (?, ?, ?, '', ?, ?)",
            (month, category, amount, now, now),
        )


def insert_savings_goal(db_path: Path, name: str, target: str = "10000.00") -> None:
    """Insert a savings_goal row directly via sqlite3."""
    now = _now()
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO savings_goals (name, account_id, target_amount, status, notes, created_at, updated_at)"
            " VALUES (?, NULL, ?, 'active', '', ?, ?)",
            (name, target, now, now),
        )


def insert_config_metadata(db_path: Path, key: str, value: str) -> None:
    """Insert a config_metadata row directly via sqlite3."""
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT OR REPLACE INTO config_metadata (key, value) VALUES (?, ?)",
            (key, value),
        )


def insert_db_operation_audit(db_path: Path, operation: str = "db_validate") -> None:
    """Insert a db_operation_audit row directly via sqlite3 (a DRIFT-EXEMPT side-effect log)."""
    now = _now()
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO db_operation_audit "
            "(operation, status, started_at, finished_at, db_path, artifact_path, counts, warnings, errors, details)"
            " VALUES (?, 'success', ?, ?, ?, NULL, '{}', '[]', '[]', '{}')",
            (operation, now, now, str(db_path)),
        )


def insert_report_snapshot(db_path: Path, source: str = "reporting") -> None:
    """Insert a report_snapshots row directly via sqlite3 (a DRIFT-EXEMPT derived-cache table)."""
    now = _now()
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO report_snapshots "
            "(created_at, source, source_kind, schema_version, period_start, period_end, status, payload)"
            " VALUES (?, ?, 'reporting', 2, NULL, NULL, 'success', '{}')",
            (now, source),
        )


def insert_insight_finding(
    db_path: Path,
    *,
    fingerprint: str = "finding-1",
    status: str = "open",
) -> int:
    """Insert an insight_findings row directly via sqlite3, returning its id.

    ``insight_findings`` carries USER TRIAGE STATE (``status`` +
    ``reviewed_at``) written to the live target via the Insights page, so it is
    NOT drift-exempt — a triage decision made during the stage→finalize window
    must not be silently discarded by promote.
    """
    now = _now()
    with closing(sqlite3.connect(db_path)) as conn, conn:
        cursor = conn.execute(
            "INSERT INTO insight_findings "
            "(fingerprint, period_start, period_end, finding_type, severity, title, "
            "explanation, evidence, suggested_action, status, created_at, reviewed_at)"
            " VALUES (?, '2026-04-01', '2026-04-30', 'cost_cutting', 'medium', 'Test finding',"
            " 'expl', '{}', 'act', ?, ?, NULL)",
            (fingerprint, status, now),
        )
        return int(cursor.lastrowid or 0)


def set_insight_finding_status(db_path: Path, finding_id: int, status: str) -> None:
    """Update an insight_findings row's triage status directly via sqlite3."""
    now = _now()
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            "UPDATE insight_findings SET status=?, reviewed_at=? WHERE id=?",
            (status, now, finding_id),
        )


def get_budgets(db_path: Path) -> list[dict]:
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT budget_month, category, amount FROM budgets ORDER BY category"
        ).fetchall()
        return [{"month": r[0], "category": r[1], "amount": r[2]} for r in rows]


def get_savings_goals(db_path: Path) -> list[dict]:
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(
            "SELECT name, target_amount FROM savings_goals ORDER BY name"
        ).fetchall()
        return [{"name": r[0], "target_amount": r[1]} for r in rows]


def get_config_meta(db_path: Path, key: str) -> str | None:
    with closing(sqlite3.connect(db_path)) as conn:
        row = conn.execute("SELECT value FROM config_metadata WHERE key=?", (key,)).fetchone()
        return row[0] if row else None


def count_transactions(db_path: Path) -> int:
    with closing(sqlite3.connect(db_path)) as conn:
        return conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]


def _make_finalize_paths(tmp_path: Path, runs_dir: Path) -> Paths:
    return Paths(
        pdf_dir=tmp_path / "inbox",
        processed_dir=tmp_path / "processed",
        db_path=tmp_path / "ledger.db",
        base_db_path=None,
        backup_dir=tmp_path / "backups",
        ledger_config=tmp_path / "ledger.toml",
        merchant_rules=tmp_path / "merchants.toml",
        categories=tmp_path / "categories.toml",
        runs_dir=runs_dir,
    )


def _setup_staged_run(
    tmp_path: Path,
    runs_dir: Path,
    run_dir: Path,
) -> tuple[Path, Path, Path]:
    """Create a staged run ready for finalize(). Returns (target_db, staging_db, csv_root)."""
    from ledger.imports.pdf.readiness import import_owned_fingerprint

    csv_root = run_dir / "csv"
    target_db = tmp_path / "ledger.db"
    staging_db = run_dir / "staging.db"
    csv_root.mkdir(parents=True)
    init_database(target_db)
    init_database(staging_db)
    before = database_fingerprint(target_db)
    import_owned_before = import_owned_fingerprint(target_db)
    (run_dir / "merchant-review.json").write_text("[]", encoding="utf-8")
    write_run_state(
        run_dir,
        bound_import_state(
            {
                "status": "ready_to_finalize",
                "target_db_path": str(target_db),
                "staging_db_path": str(staging_db),
                "csv_root": str(csv_root),
                "staging_inbox": str(run_dir / "inbox"),
                "merchant_review_json": str(run_dir / "merchant-review.json"),
                "pending_review_count": 0,
                "target_db_fingerprint_before": before,
                "target_db_import_owned_before": import_owned_before,
                "pdfs": [],
            },
            _make_finalize_paths(tmp_path, runs_dir),
        ),
    )
    return target_db, staging_db, csv_root


def _apply_finalize_mocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(workflow, "load_config", lambda _paths, _db: object())
    monkeypatch.setattr(workflow, "run_migrations", lambda _db: None)
    monkeypatch.setattr(workflow, "process", lambda **_kwargs: ProcessingResult(total_scanned=1))
    monkeypatch.setattr(workflow, "validate_processing_result", lambda *_args: None)
    monkeypatch.setattr(workflow, "write_result_to_db", lambda *_args: None)
    monkeypatch.setattr(workflow, "build_merchant_review", lambda _result: [])
    monkeypatch.setattr(workflow, "seed_review_candidates", lambda *_args: None)
    monkeypatch.setattr(workflow, "get_review_rows", lambda _db: [])
    monkeypatch.setattr(workflow, "apply_review_decisions", lambda _db: 0)
    monkeypatch.setattr(workflow, "pending_review_count", lambda _db: 0)
    monkeypatch.setattr(workflow, "run_post_processing", lambda _db, _config: {})
    monkeypatch.setattr(workflow, "record_processed_files", lambda *_args: None)
    monkeypatch.setattr(workflow, "record_db_operation", lambda *_args, **_kwargs: None)


# ---------------------------------------------------------------------------
# Test 9: INVARIANT — excluded tables == MERGE_FORWARD_TABLES exactly
# ---------------------------------------------------------------------------


def test_invariant_merge_forward_tables_equals_excluded_from_import_owned_fingerprint(
    tmp_path: Path,
) -> None:
    """The set of tables excluded from import_owned_fingerprint MUST equal exactly
    ``MERGE_FORWARD_TABLES | DRIFT_EXEMPT_TABLES`` — so 'ignored by drift' ==
    'merged forward' PLUS 'pure side-effect/derived (harmless drift)'.
    This is a structural test: any divergence between the excluded set and those two
    mechanisms is a bug.
    """
    from ledger.imports.pdf.readiness import (
        DRIFT_EXEMPT_TABLES,
        MERGE_FORWARD_TABLES,
        import_owned_fingerprint,
    )
    from ledger.storage.database import Base

    db_path = tmp_path / "test.db"
    init_database(db_path)

    # Pin the membership by name as well as by construction. The set comparison
    # below derives BOTH sides from the tuple, so it stays green no matter what the
    # tuple contains -- it proves the two mechanisms agree, not that the tuple is
    # right. Exempting a table from the drift gate is a data-safety decision, so
    # adding or removing a name must be deliberate enough to edit this literal.
    assert set(MERGE_FORWARD_TABLES) == {
        "budgets",
        "savings_goals",
        "spending_preferences",
        "planned_schedules",
        "planned_schedule_revisions",
        "plan_reconciliation_reviews",
        "planning_proposal_receipts",
        "planning_definition_generations",
        "config_metadata",
        "account_lifecycle",
    }, MERGE_FORWARD_TABLES

    all_tables = set(Base.metadata.tables.keys())
    excluded_set = set(MERGE_FORWARD_TABLES) | set(DRIFT_EXEMPT_TABLES)

    # Compute which tables are skipped by import_owned_fingerprint:
    # it should cover all tables EXCEPT MERGE_FORWARD_TABLES | DRIFT_EXEMPT_TABLES.
    fp = import_owned_fingerprint(db_path)
    covered_tables = set(fp["tables"].keys())

    expected_covered = all_tables - excluded_set

    assert covered_tables == expected_covered, (
        f"import_owned_fingerprint covers {covered_tables!r} "
        f"but expected to cover all tables minus MERGE_FORWARD_TABLES | DRIFT_EXEMPT_TABLES "
        f"(missing: {expected_covered - covered_tables!r}, "
        f"extra: {covered_tables - expected_covered!r})"
    )

    # Ensure the excluded tables are NOT in the fingerprint.
    for tbl in excluded_set:
        assert tbl not in covered_tables, (
            f"'{tbl}' is excluded (merge-forward or drift-exempt) but appears in "
            "import_owned_fingerprint"
        )

    # Genuinely IMPORT-OWNED data tables MUST stay covered (drift on them = real
    # potential data loss, so they must remain in the gate).
    for tbl in (
        "transactions",
        "transfers",
        "import_runs",
        "processed_files",
        "accounts",
        "account_balances",
    ):
        assert tbl in covered_tables, (
            f"import-owned data table '{tbl}' MUST stay in the drift gate; "
            "exempting it would risk silent data loss on concurrent import-owned writes"
        )

    # Config tables are DELIBERATELY left in the gate (fail-safe: the 'which DB does
    # the operator edit during review' question is unresolved, so we block rather than
    # risk silently discarding a live config edit).
    for tbl in (
        "merchant_rules",
        "merchant_rule_patterns",
        "category_config",
        "category_merchants",
    ):
        assert tbl in covered_tables, (
            f"config table '{tbl}' must remain in the drift gate (deliberate fail-safe; "
            "see the 'which DB to edit during review' open question)"
        )


# ---------------------------------------------------------------------------
# M6: DRIFT_EXEMPT_TABLES — side-effect/derived writes on the live target during
# the stage→finalize window must NOT be read as import-owned drift.
# ---------------------------------------------------------------------------


def test_drift_exempt_db_operation_audit_does_not_change_fingerprint(tmp_path: Path) -> None:
    """Writing a db_operation_audit row (a side-effect log written by e.g.
    ``ledger db validate`` on the LIVE target during the review window) must NOT
    change import_owned_fingerprint — otherwise a routine action blocks finalize.
    """
    from ledger.imports.pdf.readiness import import_owned_fingerprint, import_owned_match

    db_path = tmp_path / "test.db"
    init_database(db_path)

    fp_before = import_owned_fingerprint(db_path)
    insert_db_operation_audit(db_path, operation="db_validate")
    fp_after = import_owned_fingerprint(db_path)

    assert import_owned_match(fp_before, fp_after), (
        "Writing db_operation_audit (a DRIFT_EXEMPT side-effect log) must NOT change "
        "the import-owned fingerprint"
    )


def test_drift_exempt_report_snapshot_does_not_change_fingerprint(tmp_path: Path) -> None:
    """Writing a report_snapshots row (opening the Reporting page snapshots the live
    target) must NOT change import_owned_fingerprint — it is a finalize-recomputed
    derived cache, so its drift is harmless."""
    from ledger.imports.pdf.readiness import import_owned_fingerprint, import_owned_match

    db_path = tmp_path / "test.db"
    init_database(db_path)

    fp_before = import_owned_fingerprint(db_path)
    insert_report_snapshot(db_path, source="reporting")
    fp_after = import_owned_fingerprint(db_path)

    assert import_owned_match(fp_before, fp_after), (
        "Writing report_snapshots (a DRIFT_EXEMPT derived cache) must NOT change "
        "the import-owned fingerprint"
    )


def test_insight_finding_triage_state_change_flips_target_db_unchanged_gate(
    tmp_path: Path,
) -> None:
    """insight_findings carries USER TRIAGE STATE (status/reviewed_at), so it is NOT
    drift-exempt: a triage decision written to the LIVE target during the
    stage→finalize review window MUST flip target_db_unchanged to fail (block the
    promote) rather than being silently overwritten wholesale by refresh_period.

    Pre-fix (insight_findings ∈ DRIFT_EXEMPT_TABLES): the row change is excluded from
    import_owned_fingerprint → gate stays 'pass' → the decision is silently discarded.
    Post-fix (removed from the exempt set): the change is import-owned → gate fails.
    """
    from ledger.imports.pdf.readiness import (
        build_import_readiness,
        import_owned_fingerprint,
        import_owned_match,
    )

    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "run-1"
    target_db, _staging_db, _csv_root = _setup_staged_run(tmp_path, runs_dir, run_dir)

    # A finding already exists on the live target at stage time.
    finding_id = insert_insight_finding(target_db, fingerprint="finding-triage", status="open")

    # Re-stamp the readiness baseline to include the pre-existing finding so ONLY the
    # subsequent triage-state change is what could flip the gate.
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    state["target_db_import_owned_before"] = import_owned_fingerprint(target_db)
    state["target_db_fingerprint_before"] = database_fingerprint(target_db)
    write_run_state(run_dir, state)

    assert build_import_readiness(run_dir)["gates"]["target_db_unchanged"] == "pass"
    fp_before = import_owned_fingerprint(target_db)

    # User triages the finding on the LIVE target during the review window.
    set_insight_finding_status(target_db, finding_id, status="ignored")

    fp_after = import_owned_fingerprint(target_db)
    assert not import_owned_match(fp_before, fp_after), (
        "A triage-state change on insight_findings MUST change the import-owned "
        "fingerprint — insight_findings holds user decisions and is NOT drift-exempt."
    )

    readiness = build_import_readiness(run_dir)
    assert readiness["gates"]["target_db_unchanged"] == "fail", (
        "A triage decision written to insight_findings on the live target during the "
        "review window MUST flip target_db_unchanged to fail (promote blocked, decision "
        "protected) rather than being silently overwritten."
    )
    assert not readiness["can_finalize"], (
        "The staged import must NOT be finalizable after a live triage-state change "
        "(fail-safe: block rather than discard the user's decision)."
    )


def test_drift_exempt_write_does_not_flip_target_db_unchanged_gate(
    tmp_path: Path,
) -> None:
    """A side-effect write on the LIVE target during the review window must NOT flip
    build_import_readiness's ``target_db_unchanged`` gate to fail (i.e. the staged
    import stays finalizable — no forced cancel/restage).

    Reproduces the M6 defect end-to-end at the readiness-gate level: opening the
    Reporting page (report_snapshots) + a db validate (db_operation_audit) on the
    live target must not block finalize.
    """
    from ledger.imports.pdf.readiness import build_import_readiness

    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "run-1"
    target_db, _staging_db, _csv_root = _setup_staged_run(tmp_path, runs_dir, run_dir)

    # Sanity: gate passes right after stage.
    readiness_before = build_import_readiness(run_dir)
    assert readiness_before["gates"]["target_db_unchanged"] == "pass"

    # Routine live-DB side-effects during the stage→finalize review window.
    insert_report_snapshot(target_db, source="reporting")  # opened Reporting page
    insert_db_operation_audit(target_db, operation="db_validate")  # ran db validate

    readiness_after = build_import_readiness(run_dir)
    assert readiness_after["gates"]["target_db_unchanged"] == "pass", (
        "Routine side-effect writes (report_snapshots / db_operation_audit) on the "
        "live target must NOT flip target_db_unchanged to fail (M6)."
    )
    assert readiness_after["can_finalize"], (
        "The staged import must remain finalizable after harmless side-effect drift (M6)."
    )


def test_import_owned_change_still_flips_target_db_unchanged_gate(tmp_path: Path) -> None:
    """GUARD: a write to a genuinely import-owned table (transactions) on the live
    target STILL changes the fingerprint AND STILL flips target_db_unchanged to fail
    (drift detection intact after the M6 exemption)."""
    from ledger.imports.pdf.readiness import build_import_readiness

    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "run-1"
    target_db, _staging_db, _csv_root = _setup_staged_run(tmp_path, runs_dir, run_dir)

    assert build_import_readiness(run_dir)["gates"]["target_db_unchanged"] == "pass"

    # A genuine import-owned change on the live target.
    insert_minimal_transaction(target_db, "txn-drift-guard")

    readiness = build_import_readiness(run_dir)
    assert readiness["gates"]["target_db_unchanged"] == "fail", (
        "An import-owned change (transactions) on the live target MUST still flip "
        "target_db_unchanged to fail — drift detection must remain intact."
    )
    assert not readiness["can_finalize"], (
        "Import-owned drift must still block finalize (fail-safe boundary intact)."
    )


def test_drift_exempt_tables_are_real_tablenames() -> None:
    """Every table in DRIFT_EXEMPT_TABLES must be a real __tablename__ in the schema.
    A typo'd name would silently do nothing (over-detect drift) — this catches it."""
    from ledger.imports.pdf.readiness import DRIFT_EXEMPT_TABLES
    from ledger.storage.database import Base

    all_tables = set(Base.metadata.tables.keys())
    for tbl in DRIFT_EXEMPT_TABLES:
        assert tbl in all_tables, (
            f"DRIFT_EXEMPT_TABLES member '{tbl}' is not a real table in Base.metadata"
        )


# ---------------------------------------------------------------------------
# Test 3: import_owned_fingerprint unit tests
# ---------------------------------------------------------------------------


def test_import_owned_fingerprint_mutate_budget_no_change(tmp_path: Path) -> None:
    """Mutating budgets (a MERGE_FORWARD_TABLE) must NOT change the import-owned hash."""
    from ledger.imports.pdf.readiness import import_owned_fingerprint, import_owned_match

    db_path = tmp_path / "test.db"
    init_database(db_path)

    fp_before = import_owned_fingerprint(db_path)
    insert_budget(db_path, "Groceries", "400.00")
    fp_after = import_owned_fingerprint(db_path)

    assert import_owned_match(fp_before, fp_after), (
        "Mutating budgets (MERGE_FORWARD_TABLE) must not change import_owned_fingerprint"
    )


def test_import_owned_fingerprint_mutate_savings_goal_no_change(tmp_path: Path) -> None:
    """Mutating savings_goals (a MERGE_FORWARD_TABLE) must NOT change the import-owned hash."""
    from ledger.imports.pdf.readiness import import_owned_fingerprint, import_owned_match

    db_path = tmp_path / "test.db"
    init_database(db_path)

    fp_before = import_owned_fingerprint(db_path)
    insert_savings_goal(db_path, "Emergency Fund", "25000.00")
    fp_after = import_owned_fingerprint(db_path)

    assert import_owned_match(fp_before, fp_after), (
        "Mutating savings_goals (MERGE_FORWARD_TABLE) must not change import_owned_fingerprint"
    )


def test_import_owned_fingerprint_mutate_config_metadata_no_change(tmp_path: Path) -> None:
    """Mutating config_metadata (a MERGE_FORWARD_TABLE) must NOT change the import-owned hash."""
    from ledger.imports.pdf.readiness import import_owned_fingerprint, import_owned_match

    db_path = tmp_path / "test.db"
    init_database(db_path)

    fp_before = import_owned_fingerprint(db_path)
    insert_config_metadata(db_path, "export_account_ref_salt", "deadbeef1234")
    fp_after = import_owned_fingerprint(db_path)

    assert import_owned_match(fp_before, fp_after), (
        "Mutating config_metadata (MERGE_FORWARD_TABLE) must not change import_owned_fingerprint"
    )


def test_import_owned_fingerprint_mutate_transactions_changes_hash(tmp_path: Path) -> None:
    """Mutating transactions (import-owned) MUST change the import-owned hash."""
    from ledger.imports.pdf.readiness import import_owned_fingerprint, import_owned_match

    db_path = tmp_path / "test.db"
    init_database(db_path)

    fp_before = import_owned_fingerprint(db_path)
    insert_minimal_transaction(db_path, "txn-001")
    fp_after = import_owned_fingerprint(db_path)

    assert not import_owned_match(fp_before, fp_after), (
        "Mutating transactions (import-owned) must change import_owned_fingerprint"
    )


def test_import_owned_fingerprint_structure(tmp_path: Path) -> None:
    """import_owned_fingerprint must return {'tables': {tbl: (count, sha256), ...}}."""
    from ledger.imports.pdf.readiness import import_owned_fingerprint

    db_path = tmp_path / "test.db"
    init_database(db_path)

    fp = import_owned_fingerprint(db_path)
    assert "tables" in fp
    assert isinstance(fp["tables"], dict)
    assert len(fp["tables"]) > 0

    for tbl, (count, sha256) in fp["tables"].items():
        assert isinstance(tbl, str), f"table key must be str, got {type(tbl)}"
        assert isinstance(count, int), f"count must be int for {tbl}"
        assert isinstance(sha256, str) and len(sha256) == 64, (
            f"sha256 must be 64-char hex for {tbl}"
        )

    # transactions must be present (it is import-owned)
    assert "transactions" in fp["tables"]
    # budgets must NOT be present (MERGE_FORWARD_TABLE)
    assert "budgets" not in fp["tables"]
    assert "savings_goals" not in fp["tables"]
    assert "config_metadata" not in fp["tables"]


# ---------------------------------------------------------------------------
# Test 1: Budget/goals/salt edit during finalize PRESERVED, not aborted
# ---------------------------------------------------------------------------


def test_budget_edit_during_finalize_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Editing budgets on the live DB AFTER stage but BEFORE finalize must be
    PRESERVED in the final result — the merge-forward copies live runtime tables
    into the promote temp, so the budget edit is NOT aborted.

    Pre-implementation: this aborts with RuntimeError because the budget edit
    changes the whole-file fingerprint. Post-implementation: no abort.
    """
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "run-1"
    target_db, staging_db, _csv_root = _setup_staged_run(tmp_path, runs_dir, run_dir)
    paths = _make_finalize_paths(tmp_path, runs_dir)
    _apply_finalize_mocks(monkeypatch)

    # Insert a budget into the staging DB (like it would be at stage time).
    insert_budget(staging_db, "Groceries", "300.00")

    # User edits the live DB budget AFTER stage (concurrent UI edit).
    insert_budget(target_db, "Groceries", "500.00")  # live budget differs from staging

    # Add a staged transaction to verify import data is present.
    insert_minimal_transaction(staging_db, "txn-staged-001")

    # Finalize must NOT abort — merge-forward must preserve the live budget.
    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state["status"] == "finalized", (
        f"Expected finalized but got {state.get('status')!r}; "
        "budget edit aborted the import (pre-implementation failure)"
    )

    # The promoted live DB must have the LIVE budget (500), not the staging one (300).
    budgets = get_budgets(target_db)
    grocery_budget = next((b for b in budgets if b["category"] == "Groceries"), None)
    assert grocery_budget is not None, "Grocery budget must exist in promoted DB"
    assert str(grocery_budget["amount"]) in ("500.00", "500"), (
        f"Expected live budget 500.00 to be preserved; got {grocery_budget['amount']!r}"
    )

    # The staged transaction must also be present (import data not lost).
    assert count_transactions(target_db) >= 1


def test_savings_goal_edit_during_finalize_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A savings_goal edit on the live DB after stage must be preserved post-finalize."""
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "run-1"
    target_db, _staging_db, _csv_root = _setup_staged_run(tmp_path, runs_dir, run_dir)
    paths = _make_finalize_paths(tmp_path, runs_dir)
    _apply_finalize_mocks(monkeypatch)

    # Live goal added after stage.
    insert_savings_goal(target_db, "New Car", "20000.00")

    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state["status"] == "finalized", (
        f"savings_goal edit aborted finalize: {state.get('status')!r}"
    )

    goals = get_savings_goals(target_db)
    assert any(g["name"] == "New Car" for g in goals), (
        "Live savings_goal 'New Car' must survive the promote (merge-forward)"
    )


def test_config_metadata_salt_edit_during_finalize_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A config_metadata (salt) written on the live DB after stage must be preserved post-finalize."""
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "run-1"
    target_db, _staging_db, _csv_root = _setup_staged_run(tmp_path, runs_dir, run_dir)
    paths = _make_finalize_paths(tmp_path, runs_dir)
    _apply_finalize_mocks(monkeypatch)

    # Simulate the export salt being lazily generated on the live DB after stage.
    insert_config_metadata(target_db, "export_account_ref_salt", "my-secret-salt-9999")

    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state["status"] == "finalized", (
        f"config_metadata edit aborted finalize: {state.get('status')!r}"
    )

    salt = get_config_meta(target_db, "export_account_ref_salt")
    assert salt == "my-secret-salt-9999", f"Salt must be preserved via merge-forward; got {salt!r}"


# ---------------------------------------------------------------------------
# Test 2: Import-owned change still ABORTS
# ---------------------------------------------------------------------------


def test_import_owned_transaction_change_aborts_finalize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A direct change to a transactions row in the live DB (import-owned table)
    after stage must still cause finalize to abort (SystemExit from the pre-promote
    gate, or RuntimeError from the recheck inside promote_database).

    Both are acceptable abort paths — the important invariants are:
    - Finalize does NOT complete with status='finalized'.
    - The live DB is not clobbered.
    - No leftover temp files.
    """
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "run-1"
    target_db, _staging_db, _csv_root = _setup_staged_run(tmp_path, runs_dir, run_dir)
    paths = _make_finalize_paths(tmp_path, runs_dir)
    _apply_finalize_mocks(monkeypatch)

    # Concurrently insert a transaction into the live DB (import-owned change).
    insert_minimal_transaction(target_db, "txn-concurrent-import-owned")

    # The abort may come as SystemExit (early gate) or RuntimeError (late recheck).
    with pytest.raises((SystemExit, RuntimeError)):
        workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state["status"] != "finalized", (
        "Import must NOT be marked finalized when import-owned table was changed"
    )

    # Live DB must still contain the concurrent write (not clobbered).
    assert count_transactions(target_db) >= 1, "Live DB must be intact after aborted finalize"

    # No leftover temp files.
    db_parent = target_db.parent
    leftovers = list(db_parent.glob(".*.promote-*.tmp"))
    assert not leftovers, f"Leftover temp files after abort: {leftovers}"


# ---------------------------------------------------------------------------
# Test 4: A6 survives merge-forward (import-owned equality, not sha256)
# ---------------------------------------------------------------------------


def test_a6_recovery_uses_import_owned_equality_not_sha256(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After merge-forward, target sha256 != staging sha256 (target has merged-in
    runtime tables). A6 recovery must use import_owned_match, not sha256 equality.

    Scenario:
    - Stage run as normal.
    - Simulate promote with merge-forward: target has staging import data +
      live runtime tables (budgets differ from staging).
    - Wedge the run at 'finalizing'.
    - A6 recovery must detect promote_happened=True via import_owned_match,
      NOT via byte-level sha256 (which would now be False).
    """
    from ledger.imports.pdf.files import sha256_file
    from ledger.imports.pdf.readiness import import_owned_fingerprint, import_owned_match

    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "run-1"
    target_db, staging_db, _csv_root = _setup_staged_run(tmp_path, runs_dir, run_dir)
    paths = _make_finalize_paths(tmp_path, runs_dir)

    # Staging has a transaction (import data).
    insert_minimal_transaction(staging_db, "txn-staged-a6")

    # Live DB has a budget set AFTER stage (will be merged forward into temp).
    insert_budget(target_db, "Entertainment", "200.00")

    # Simulate merge-forward promote: copy staging onto target, then merge-forward
    # the live budget into target. This is what the real promote does.
    import shutil

    shutil.copy2(staging_db, target_db)
    # Now merge-forward the budget (which was in old live, but not in staging copy).
    # This simulates the real merge_forward which takes live→temp.
    # For this test we manually do: target = staging_content + live_budgets
    insert_budget(target_db, "Entertainment", "200.00")

    # Now: target has staging import data + live budget.
    # sha256(target) != sha256(staging) because target has the budget merge.
    assert sha256_file(target_db) != sha256_file(staging_db), (
        "After merge-forward, sha256 must differ (sanity check for this test)"
    )

    # But import_owned_fingerprint must match (budgets are excluded).
    assert import_owned_match(
        import_owned_fingerprint(target_db),
        import_owned_fingerprint(staging_db),
    ), "import_owned must match between merged-target and staging"

    # Wedge run at 'finalizing'.
    run_state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    run_state["status"] = "finalizing"
    run_state["publication_candidate_import_owned"] = import_owned_fingerprint(staging_db)
    write_run_state(run_dir, run_state)

    # Patch the lock-aware publication primitive — it must NOT be called during recovery.
    promote_call_count = {"n": 0}
    real_promote = workflow._promote_database_locked

    def spy_promote(*args, **kwargs):
        promote_call_count["n"] += 1
        return real_promote(*args, **kwargs)

    monkeypatch.setattr(workflow, "_promote_database_locked", spy_promote)
    _apply_finalize_mocks(monkeypatch)

    # A6 recovery: should detect promote_happened=True via import_owned equality.
    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state["status"] == "finalized", (
        f"A6 recovery must reach 'finalized'; got {state.get('status')!r}"
    )
    assert promote_call_count["n"] == 0, (
        "A6 recovery must NOT re-promote when import_owned match detected"
    )


def test_a6_recovery_falls_through_when_import_owned_differs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When import_owned fingerprints differ (target != staging import data),
    A6 recovery must fall through to the normal finalize path (not mark finalized).

    Scenario: run is wedged at 'finalizing' but promote has NOT happened yet
    (target == old-live with no import changes since stage, staging has new txns).
    Recovery detects import_owned(target) != import_owned(staging) → not promoted →
    falls through to normal finalize → completes.
    """
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "run-1"
    _target_db, staging_db, _csv_root = _setup_staged_run(tmp_path, runs_dir, run_dir)
    paths = _make_finalize_paths(tmp_path, runs_dir)

    # Staging has a new transaction (import data that hasn't been promoted yet).
    # Target has NO changes since stage (import-owned fingerprint unchanged).
    insert_minimal_transaction(staging_db, "txn-staged-not-promoted")
    # → import_owned(target) != import_owned(staging) because staging has a txn

    # Wedge at 'finalizing'.
    from ledger.imports.pdf.readiness import import_owned_fingerprint

    run_state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    run_state["status"] = "finalizing"
    run_state["publication_candidate_import_owned"] = import_owned_fingerprint(staging_db)
    write_run_state(run_dir, run_state)

    _apply_finalize_mocks(monkeypatch)

    # Should reset to 'ready_to_finalize' and fall through to normal finalize.
    # The target is unchanged (import_owned_before matches current target), so
    # _fail_if_target_db_changed passes, and the full finalize completes.
    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state["status"] == "finalized", (
        f"Fall-through must complete finalize; got {state.get('status')!r}"
    )


# ---------------------------------------------------------------------------
# Ship-gate [1] regression: OOB MERGE_FORWARD_TABLE edit must NOT be misread
# as "promote happened" by A6 recovery.
# ---------------------------------------------------------------------------


def test_a6_oob_budget_edit_on_live_not_misread_as_promoted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DATA-LOSS SAFETY: an OOB budget edit on the live DB during the kill window
    must NOT cause A6 to conclude 'promote happened' when it did NOT.

    Scenario (the dangerous case the ship-gate [1] guards against):
    1.  A run transitions to status='finalizing' and the process is killed.
    2.  Promote has NOT yet happened — the live DB still holds the pre-import state
        (import-owned tables do NOT contain the staged transactions).
    3.  During the kill window a concurrent OOB edit writes a budget row on the
        live DB (budgets ∈ MERGE_FORWARD_TABLES, so it is excluded from
        import_owned_fingerprint — this is exactly the danger).
    4.  A6 recovery runs.

    Correct behaviour:
        import_owned(live_target, missing staged txns)
            ≠ import_owned(staging_db, has staged txns)
        → promote_happened = False
        → A6 resets to 'ready_to_finalize' and re-runs the full finalize path
        → The staged import is NOT silently discarded; it gets promoted normally.

    The test would FAIL if A6 were changed to use a budget-insensitive signal
    that does not also verify import-owned table parity — e.g. if it treated any
    change to the live DB as evidence of promote, or used a per-table hash that
    accidentally included budget rows as the sole distinguisher.  In that misread
    scenario A6 would set status='finalized' without actually promoting the
    staged transactions, silently dropping the import.

    What we assert:
    - A6 must NOT set status='finalized' via the 'promote happened' branch.
      (Because promote never happened — the staged txn is still only in staging.)
    - Instead A6 must fall through to the normal finalize path, which completes
      with status='finalized' AND the staged transaction promoted into live.
    - The staged import data is NOT lost: after recovery the live DB contains
      the staged transaction.
    """
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "run-1"
    target_db, staging_db, _csv_root = _setup_staged_run(tmp_path, runs_dir, run_dir)
    paths = _make_finalize_paths(tmp_path, runs_dir)

    # ── State: staging has a new transaction; live target does NOT ──────────
    # This makes import_owned(target) ≠ import_owned(staging): the staged import
    # has not been promoted yet.
    insert_minimal_transaction(staging_db, "txn-staged-ship-gate-1")

    # ── OOB budget edit on live during the kill window ──────────────────────
    # Budgets ∈ MERGE_FORWARD_TABLES → excluded from import_owned_fingerprint.
    # A naive detector that tested "did anything change in live?" would see this
    # budget edit and incorrectly conclude promote happened.
    insert_budget(target_db, "Groceries", "999.00")

    from ledger.imports.pdf.readiness import import_owned_fingerprint, import_owned_match

    # ── Wedge the run at 'finalizing' (simulate process kill mid-finalize) ──
    run_state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    run_state["status"] = "finalizing"
    run_state["publication_candidate_import_owned"] = import_owned_fingerprint(staging_db)
    write_run_state(run_dir, run_state)

    # ── Sanity: import_owned differs (staged txn absent from live) ──────────
    # This pre-condition is what the correct A6 logic tests.  If this assertion
    # fails the test fixture itself is wrong.
    assert not import_owned_match(
        import_owned_fingerprint(target_db),
        import_owned_fingerprint(staging_db),
    ), (
        "Pre-condition: live target must NOT match staging's import-owned fingerprint "
        "(staged txn not yet promoted) — test fixture is wrong"
    )

    # Track whether the lock-aware publication primitive is called (it should be: A6 must fall
    # through, not short-circuit via the 'promote happened' branch).
    promote_call_count = {"n": 0}
    real_promote = workflow._promote_database_locked

    def spy_promote(*args, **kwargs):
        promote_call_count["n"] += 1
        return real_promote(*args, **kwargs)

    monkeypatch.setattr(workflow, "_promote_database_locked", spy_promote)
    _apply_finalize_mocks(monkeypatch)

    # ── Run A6 recovery ─────────────────────────────────────────────────────
    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))

    # PRIMARY assertion: A6 must have taken the fall-through path (normal
    # finalize), not the 'promote happened → replay tail only' shortcut.
    # Evidence: promote_database must have been called exactly once.
    assert promote_call_count["n"] == 1, (
        f"A6 must call promote_database (fall-through path, not 'promote happened' "
        f"shortcut); got promote_call_count={promote_call_count['n']}. "
        "If this is 0, A6 misread the OOB budget edit as 'promote happened' and "
        "skipped the promote — the staged import would be silently discarded."
    )

    # STATUS assertion: finalize must still succeed end-to-end.
    assert state["status"] == "finalized", (
        f"Finalize must complete with status='finalized'; got {state.get('status')!r}"
    )

    # IMPORT DATA assertion: the staged transaction must be in the live DB.
    # If A6 had misread the budget edit as 'promote happened' and only replayed
    # the idempotent tail (no re-promote), the staged txn would be absent here.
    assert count_transactions(target_db) >= 1, (
        "Staged transaction must be present in the promoted live DB. "
        "If 0, A6 silently dropped the import by misreading the OOB budget edit."
    )

    # RECOVERY NOTE must NOT be the 'post-promote' shortcut note.
    assert state.get("recovery_note") != "recovered_from_finalizing_post_promote", (
        "A6 must NOT have taken the 'promote already happened' recovery branch; "
        "that branch skips re-promote and would silently drop the staged import."
    )


# ---------------------------------------------------------------------------
# Test 6: Merge went into temp not staging (A6 invariant guard)
# ---------------------------------------------------------------------------


def test_merge_forward_does_not_mutate_staging_db(tmp_path: Path) -> None:
    """merge_forward must write into temp_target (the promote copy), NOT into the
    run-dir staging_db. After finalize, staging.db's budgets must be the STAGING
    snapshot, not the live post-merge value.

    This guards the A6 invariant: staging_db must remain unchanged so import_owned
    equality detection still works on recovery.
    """
    from ledger.imports.pdf.readiness import merge_forward

    live_db = tmp_path / "live.db"
    staging_db = tmp_path / "staging.db"
    temp_db = tmp_path / "temp.db"

    init_database(live_db)
    init_database(staging_db)
    import shutil

    shutil.copy2(staging_db, temp_db)

    # Live has a budget; staging does not.
    insert_budget(live_db, "Travel", "1000.00")

    # merge_forward should write into temp, not staging.
    merge_forward(live_db, temp_db)

    # temp has the live budget.
    assert len(get_budgets(temp_db)) == 1, "Temp must have the merged-in live budget"
    # staging is untouched.
    assert len(get_budgets(staging_db)) == 0, (
        "Staging DB must NOT be mutated by merge_forward (A6 invariant)"
    )


def test_staging_budgets_unchanged_after_finalize_with_live_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After finalize with merge-forward, staging.db budgets must still hold the
    STAGE-time copy, not the live-edited values. The merge only goes into the temp
    that becomes the new live.
    """
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "run-1"
    target_db, staging_db, _csv_root = _setup_staged_run(tmp_path, runs_dir, run_dir)
    paths = _make_finalize_paths(tmp_path, runs_dir)
    _apply_finalize_mocks(monkeypatch)

    # Staging has a budget at stage time.
    insert_budget(staging_db, "Groceries", "300.00")
    # Live has a different budget (edited after stage).
    insert_budget(target_db, "Groceries", "500.00")

    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state["status"] == "finalized"

    # staging.db must still have the stage-time budget (300), NOT the live value (500).
    staging_budgets = get_budgets(staging_db)
    grocery = next((b for b in staging_budgets if b["category"] == "Groceries"), None)
    assert grocery is not None
    assert str(grocery["amount"]) in ("300.00", "300"), (
        f"staging.db budgets must remain at stage-time value; got {grocery['amount']!r}"
    )


# ---------------------------------------------------------------------------
# Test 5: End-to-end
# ---------------------------------------------------------------------------


def test_end_to_end_budget_and_goal_edit_preserved_after_finalize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stage a run with N staged transactions. Edit budgets+goals on live. Finalize.
    Result: live budgets/goals == live-edited, txn count == pre+N.
    """
    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "run-1"
    target_db, staging_db, _csv_root = _setup_staged_run(tmp_path, runs_dir, run_dir)
    paths = _make_finalize_paths(tmp_path, runs_dir)
    _apply_finalize_mocks(monkeypatch)

    # Insert some staged transactions.
    for i in range(3):
        insert_minimal_transaction(staging_db, f"txn-staged-{i:03d}")

    # User edits live budgets and goals after stage.
    insert_budget(target_db, "Dining", "250.00")
    insert_savings_goal(target_db, "Vacation 2027", "5000.00")

    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state["status"] == "finalized"

    # Budgets from live.
    budgets = get_budgets(target_db)
    assert any(b["category"] == "Dining" for b in budgets), (
        "Live-edited budget must be present in promoted DB"
    )

    # Goals from live.
    goals = get_savings_goals(target_db)
    assert any(g["name"] == "Vacation 2027" for g in goals), (
        "Live-edited savings_goal must be present in promoted DB"
    )

    # All staged transactions must be present.
    txn_count = count_transactions(target_db)
    assert txn_count == 3, f"Expected 3 staged transactions; got {txn_count}"


# ---------------------------------------------------------------------------
# Test 8: Merge/abort failure leaves live intact + temp cleaned
# ---------------------------------------------------------------------------


def test_promote_database_abort_leaves_live_intact_and_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If promote_database aborts (import-owned drift), live must be intact and
    no temp file left behind.

    Exercises the NEW expected_import_owned code path (not the legacy whole-file path).
    Scenario: fp captured before a concurrent transaction insert; promote receives the
    stale import-owned snapshot → import_owned_match fails → RuntimeError.
    """
    from ledger.imports.pdf.readiness import import_owned_fingerprint

    target = tmp_path / "ledger.db"
    init_database(target)

    # Capture import-owned fingerprint BEFORE the concurrent import-owned change.
    io_before = import_owned_fingerprint(target)

    # Insert a transaction (import-owned change → fingerprint will differ).
    insert_minimal_transaction(target, "txn-concurrent")

    staging = tmp_path / "staging.db"
    init_database(staging)
    # Staging has a budget (to be merged forward).
    insert_budget(staging, "Transport", "150.00")

    with pytest.raises(RuntimeError, match="target DB changed during finalize"):
        promote_database(
            staging,
            target,
            tmp_path / "backups",
            expected_import_owned=io_before,  # NEW kwarg, not legacy expected_fingerprint
        )

    # Live must still have the concurrent write (not clobbered).
    assert count_transactions(target) == 1, "Live DB must be intact after aborted promote"

    # No leftover temp files.
    leftovers = list(target.parent.glob(f".{target.name}.promote-*.tmp"))
    assert not leftovers, f"Leftover temp files after abort: {leftovers}"


def test_merge_forward_failure_leaves_live_intact_and_into_db_rolled_back(
    tmp_path: Path,
) -> None:
    """If merge_forward raises mid-transaction, BOTH live_db AND into_db must be
    left in their pre-merge state.

    The docstring in the original test claimed to verify into_db rollback, but
    the assertion only checked live_db.  This is the strengthened version that
    actually verifies the transactional rollback in into_db.

    Scenario (genuine rollback test):
    - live_db is a valid SQLite DB with one budget ("Food") and one transaction.
    - into_db is a valid SQLite DB with pre-existing merge-forward rows: a budget
      ("OldBudget") and a savings_goal ("OldGoal").  These represent the staging
      snapshot that merge_forward should REPLACE from live.
    - merge_forward is patched so it succeeds on the first table (budgets: DELETE +
      INSERT runs), but raises before committing when processing savings_goals.
      Because all ops run inside a single transaction ("with conn:" savepoint), the
      partial DELETE on budgets must be rolled back too.
    - After the failure: into_db must still have its original rows ("OldBudget"
      budget and "OldGoal" savings_goal) — not partially deleted or partially
      replaced.  live_db must be completely untouched.

    This test would FAIL if merge_forward ran each table in a separate transaction
    (so a partial DELETE from budgets was committed before the failure), which would
    leave into_db in a half-merged state.
    """
    from ledger.imports.pdf.readiness import merge_forward

    live_db = tmp_path / "live.db"
    init_database(live_db)
    insert_minimal_transaction(live_db, "txn-live-001")
    insert_budget(live_db, "Food", "200.00")
    insert_savings_goal(live_db, "Vacation", "5000.00")

    into_db = tmp_path / "into.db"
    init_database(into_db)
    # Pre-seed into_db with known merge-forward rows — these must survive a failed merge.
    insert_budget(into_db, "OldBudget", "999.00")
    insert_savings_goal(into_db, "OldGoal", "1234.00")

    # Sanity: preconditions.
    pre_budgets = get_budgets(into_db)
    pre_goals = get_savings_goals(into_db)
    assert len(pre_budgets) == 1 and pre_budgets[0]["category"] == "OldBudget"
    assert len(pre_goals) == 1 and pre_goals[0]["name"] == "OldGoal"

    # Inject a failure mid-merge: replace the module-level sqlite3.connect in
    # readiness_mod with a wrapper that returns a connection which raises on the
    # DELETE for the second MERGE_FORWARD_TABLE (savings_goals).  All ops in
    # merge_forward run inside a single "with conn:" transaction, so the partial
    # DELETE on budgets (first table) must be rolled back when savings_goals fails.
    import contextlib
    import sqlite3 as sqlite3_mod
    import types

    import ledger.imports.pdf.readiness as readiness_mod

    original_connect = readiness_mod.sqlite3.connect

    class FailingConnection:
        """Wraps a real sqlite3 connection and raises on the second DELETE."""

        def __init__(self, real_conn: sqlite3.Connection) -> None:
            self._real = real_conn

        def execute(self, sql: str, params: tuple = ()) -> object:
            if sql.startswith("DELETE FROM") and "budgets" not in sql.lower():
                # First DELETE (budgets) is allowed; subsequent DELETEs fail.
                # This simulates a failure after the first table's DELETE has been
                # issued but before the transaction commits.
                raise sqlite3_mod.OperationalError(
                    "injected mid-merge failure on second table DELETE"
                )
            return self._real.execute(sql, params)

        def __enter__(self):
            self._real.__enter__()
            return self

        def __exit__(self, *args):
            return self._real.__exit__(*args)

        def __getattr__(self, name: str) -> object:
            return getattr(self._real, name)

    class FailingContextManager:
        """Acts as both the connection AND the 'with conn:' savepoint context."""

        def __init__(self, real_conn: sqlite3.Connection) -> None:
            self._wrapped = FailingConnection(real_conn)

        def execute(self, sql: str, params: tuple = ()) -> object:
            return self._wrapped.execute(sql, params)

        def __enter__(self):
            # 'with conn:' in merge_forward — start a savepoint on the REAL conn.
            self._wrapped._real.__enter__()
            return self._wrapped  # the body executes against the failing wrapper

        def __exit__(self, *args):
            return self._wrapped._real.__exit__(*args)

        def __getattr__(self, name: str) -> object:
            return getattr(self._wrapped, name)

    def patched_connect(path: str, **kwargs: object) -> object:
        real = original_connect(path, **kwargs)
        if str(path) == str(into_db):
            return FailingContextManager(real)
        return real

    # Build a minimal fake sqlite3 module that delegates everything to the real
    # sqlite3 except connect, so readiness_mod.sqlite3.OperationalError etc. still work.
    fake_sqlite3 = types.ModuleType("sqlite3")
    for attr in dir(sqlite3_mod):
        with contextlib.suppress(AttributeError, TypeError):
            setattr(fake_sqlite3, attr, getattr(sqlite3_mod, attr))
    fake_sqlite3.connect = patched_connect  # type: ignore[attr-defined]
    readiness_mod.sqlite3 = fake_sqlite3  # type: ignore[assignment]

    try:
        with pytest.raises((sqlite3_mod.OperationalError, sqlite3_mod.DatabaseError)):
            merge_forward(live_db, into_db)
    finally:
        # Always restore the real sqlite3.
        readiness_mod.sqlite3 = sqlite3_mod  # type: ignore[assignment]

    # ── live_db must be completely untouched ─────────────────────────────────
    assert count_transactions(live_db) == 1, "Live DB must be intact after failed merge"
    live_budgets = get_budgets(live_db)
    assert len(live_budgets) == 1 and live_budgets[0]["category"] == "Food", (
        f"Live DB budgets must be untouched; got {live_budgets!r}"
    )

    # ── into_db must have ROLLED BACK to its pre-merge state ─────────────────
    # The DELETE FROM budgets (first table) was issued but NOT committed because
    # all ops run in a single transaction.  After the failure the transaction must
    # be rolled back, leaving into_db's merge-forward tables UNCHANGED.
    post_budgets = get_budgets(into_db)
    post_goals = get_savings_goals(into_db)

    assert len(post_budgets) == 1 and post_budgets[0]["category"] == "OldBudget", (
        f"into_db budgets must be rolled back to pre-merge state (OldBudget); "
        f"got {post_budgets!r}. "
        f"If budgets is empty, the DELETE committed without the INSERT — "
        f"the transaction was NOT atomic, leaving into_db half-merged."
    )
    assert len(post_goals) == 1 and post_goals[0]["name"] == "OldGoal", (
        f"into_db savings_goals must be rolled back to pre-merge state (OldGoal); "
        f"got {post_goals!r}."
    )


# ---------------------------------------------------------------------------
# Test 7: Repair parity (promote_candidate)
# ---------------------------------------------------------------------------


def test_repair_budget_edit_during_repair_preserved(tmp_path: Path) -> None:
    """Budget edit on the live DB after repair starts but before promote_candidate
    must be preserved in the promoted result (same as import path).
    """
    from ledger.imports.pdf.readiness import import_owned_fingerprint
    from ledger.repair.statements import promote_candidate

    target = tmp_path / "ledger.db"
    init_database(target)

    # User edits live budget after repair started but before promote.
    insert_budget(target, "Utilities", "200.00")

    # Capture import-owned fingerprint (no import-owned changes, only budget).
    fp_before = import_owned_fingerprint(target)

    candidate = tmp_path / "candidate.db"
    init_database(candidate)
    insert_minimal_transaction(candidate, "txn-repair-001")

    # promote_candidate with expected_import_owned must NOT abort (budget is merge-forward).
    promote_candidate(
        candidate,
        target,
        expected_import_owned=fp_before,
    )

    # Live must have the candidate transaction AND the live budget.
    assert count_transactions(target) == 1, "Repair transaction must be promoted"
    budgets = get_budgets(target)
    assert any(b["category"] == "Utilities" for b in budgets), (
        "Live budget must be preserved via merge-forward in repair promote"
    )


def test_repair_import_owned_change_aborts_promote_candidate(tmp_path: Path) -> None:
    """A transaction change on the live DB (import-owned) must abort promote_candidate."""
    from ledger.imports.pdf.readiness import import_owned_fingerprint
    from ledger.repair.statements import promote_candidate

    target = tmp_path / "ledger.db"
    init_database(target)

    fp_before = import_owned_fingerprint(target)

    # Concurrent import-owned change.
    insert_minimal_transaction(target, "txn-concurrent-repair")

    candidate = tmp_path / "candidate.db"
    init_database(candidate)

    with pytest.raises(RuntimeError, match="target DB changed during finalize"):
        promote_candidate(
            candidate,
            target,
            expected_import_owned=fp_before,
        )

    # Live must still have the concurrent write.
    assert count_transactions(target) == 1, "Live DB must be intact after aborted repair"

    # No temp files.
    leftovers = list(target.parent.glob(f".{target.name}.statement-repair-*.tmp"))
    assert not leftovers, f"Leftover temp files: {leftovers}"


# ---------------------------------------------------------------------------
# Test: merge_forward unit test
# ---------------------------------------------------------------------------


def test_merge_forward_copies_all_merge_forward_tables(tmp_path: Path) -> None:
    """merge_forward must copy ALL four MERGE_FORWARD_TABLES from live into the target."""
    from test_account_lifecycle import get_account_lifecycle, insert_account_lifecycle

    from ledger.imports.pdf.readiness import merge_forward

    live_db = tmp_path / "live.db"
    into_db = tmp_path / "into.db"

    init_database(live_db)
    init_database(into_db)

    insert_budget(live_db, "Groceries", "400.00")
    insert_savings_goal(live_db, "House Down Payment", "50000.00")
    insert_config_metadata(live_db, "export_account_ref_salt", "secret-salt-xyz")
    insert_account_lifecycle(live_db, "acct-cedar-red")

    merge_forward(live_db, into_db)

    # All four tables must be in into_db now.
    assert len(get_budgets(into_db)) == 1
    assert get_budgets(into_db)[0]["category"] == "Groceries"

    assert len(get_savings_goals(into_db)) == 1
    assert get_savings_goals(into_db)[0]["name"] == "House Down Payment"

    assert get_config_meta(into_db, "export_account_ref_salt") == "secret-salt-xyz"

    lifecycle = get_account_lifecycle(into_db)
    assert [row["account_id"] for row in lifecycle] == ["acct-cedar-red"]


def test_merge_forward_replaces_not_appends(tmp_path: Path) -> None:
    """merge_forward must DELETE existing rows in into_db before inserting live rows."""
    from ledger.imports.pdf.readiness import merge_forward

    live_db = tmp_path / "live.db"
    into_db = tmp_path / "into.db"

    init_database(live_db)
    init_database(into_db)

    # into_db has old budget.
    insert_budget(into_db, "Old Category", "100.00")
    # live_db has a new budget.
    insert_budget(live_db, "New Category", "200.00")

    merge_forward(live_db, into_db)

    budgets = get_budgets(into_db)
    assert len(budgets) == 1, "merge_forward must replace, not append"
    assert budgets[0]["category"] == "New Category"


def test_merge_forward_imports_owned_tables_untouched(tmp_path: Path) -> None:
    """merge_forward must NOT touch import-owned tables (transactions etc.)."""
    from ledger.imports.pdf.readiness import merge_forward

    live_db = tmp_path / "live.db"
    into_db = tmp_path / "into.db"

    init_database(live_db)
    init_database(into_db)

    # into_db has a transaction (import-owned).
    insert_minimal_transaction(into_db, "txn-into-only")
    # live_db has a different transaction.
    insert_minimal_transaction(live_db, "txn-live-only")

    merge_forward(live_db, into_db)

    # into_db must still have ITS transaction, NOT the live one.
    txn_ids = set()
    with closing(sqlite3.connect(into_db)) as conn:
        txn_ids = {r[0] for r in conn.execute("SELECT txn_id FROM transactions").fetchall()}

    assert "txn-into-only" in txn_ids, "import-owned data must be untouched by merge_forward"
    assert "txn-live-only" not in txn_ids, "merge_forward must not copy live import-owned tables"


# ---------------------------------------------------------------------------
# Backward-compat: promote_database still works without expected_fingerprint
# ---------------------------------------------------------------------------


def test_promote_database_without_fingerprint_still_works(tmp_path: Path) -> None:
    """promote_database with no expected_fingerprint must behave as before (no recheck)."""
    staging = tmp_path / "staging.db"
    init_database(staging)
    insert_minimal_transaction(staging, "txn-staged")

    target = tmp_path / "ledger.db"
    init_database(target)

    backup = promote_database(staging, target, tmp_path / "backups")
    assert backup is not None
    assert count_transactions(target) == 1


# ---------------------------------------------------------------------------
# G2 regression: import_owned_fingerprint must not create a 0-byte file on
# a non-existent path (first-import crash fix).
# ---------------------------------------------------------------------------


def test_import_owned_fingerprint_nonexistent_returns_absent_sentinel(
    tmp_path: Path,
) -> None:
    """import_owned_fingerprint on a non-existent path MUST:
    - Return the absent-sentinel {"tables": {}} (matching database_fingerprint style).
    - NOT create any file at that path (no side effect).

    Pre-fix: sqlite3.connect creates a 0-byte file. Post-fix: guard returns early.
    """
    from ledger.imports.pdf.readiness import import_owned_fingerprint

    nonexistent = tmp_path / "does_not_exist.db"
    assert not nonexistent.exists(), "Precondition: path must not exist before call"

    result = import_owned_fingerprint(nonexistent)

    # Must return the absent-sentinel (not crash, not create a file).
    assert result == {"tables": {}}, (
        f"Expected absent-sentinel {{'tables': {{}}}}; got {result!r}. "
        "Pre-fix this fails because sqlite3.connect creates a 0-byte file then "
        "either returns empty tables or crashes."
    )

    # Critical: no filesystem side effect — the file must NOT be created.
    assert not nonexistent.exists(), (
        "import_owned_fingerprint must NOT create a file for a non-existent path. "
        "Pre-fix: sqlite3.connect implicitly creates a 0-byte stub."
    )


def test_import_owned_match_absent_vs_absent_is_match() -> None:
    """Two absent-sentinels must compare as MATCH.

    This is critical for first-import: both stage-time capture and the
    pre-promote recheck see the absent-sentinel → match → promote proceeds.
    """
    from ledger.imports.pdf.readiness import import_owned_match

    absent_sentinel = {"tables": {}}
    assert import_owned_match(absent_sentinel, absent_sentinel), (
        "absent-sentinel vs absent-sentinel must be a MATCH. "
        "First-import depends on this: stage captures sentinel, recheck sees sentinel → promote."
    )


def test_import_owned_match_absent_vs_present_is_no_match(tmp_path: Path) -> None:
    """Absent-sentinel vs a real present fingerprint must be NO MATCH.

    This ensures A6 correctly sees target-absent (not yet promoted) as different
    from staging-present (has staged transactions).
    """
    from ledger.imports.pdf.readiness import import_owned_fingerprint, import_owned_match

    db_path = tmp_path / "real.db"
    init_database(db_path)

    absent_sentinel = {"tables": {}}
    present_fp = import_owned_fingerprint(db_path)

    assert not import_owned_match(absent_sentinel, present_fp), (
        "absent-sentinel vs a present fingerprint must be NO MATCH. "
        "A6 relies on this to distinguish 'target absent' from 'target present (promoted)'."
    )
    assert not import_owned_match(present_fp, absent_sentinel), (
        "present fingerprint vs absent-sentinel must also be NO MATCH (symmetric)."
    )


def test_first_import_stage_does_not_create_stray_live_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a first import (no live DB yet), stage() must NOT create a stray 0-byte
    file at paths.db_path. Pre-fix: import_owned_fingerprint(absent_path) calls
    sqlite3.connect which creates an empty file → db_counts() crashes on it.

    After the fix: the guard returns early → no stray file → stage() completes
    (either succeeds or exits cleanly) without creating data/ledger.db until promote.
    """
    from ledger.imports.pdf.readiness import import_owned_fingerprint

    # db_path must NOT exist before stage() to simulate a genuine first import.
    db_path = tmp_path / "ledger.db"
    assert not db_path.exists(), "Precondition: first-import has no live DB"

    # Call import_owned_fingerprint directly on the absent path (the G2 hotspot).
    result = import_owned_fingerprint(db_path)

    # Must return the absent-sentinel without any crash or file creation.
    assert result == {"tables": {}}, f"Expected absent-sentinel; got {result!r}"
    assert not db_path.exists(), (
        "stage() must NOT leave a stray 0-byte file at the live DB path. "
        "Pre-fix: sqlite3.connect(db_path) creates the file as a side effect, "
        "then db_counts() crashes with 'no such table: transactions'."
    )


def test_first_import_stage_to_finalize_no_stray_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end first-import: stage → finalize with NO pre-existing live DB.

    This mirrors _setup_staged_run but deliberately does NOT call init_database
    on the target_db. The live DB must NOT exist until promote_database runs.

    Pre-fix: stage() crashes (sqlite3.OperationalError 'no such table: transactions')
             because import_owned_fingerprint created an empty 0-byte file which
             defeated the db_counts() existence guard.
    Post-fix: stage captures the absent-sentinel, finalize recheck sees the same
              absent-sentinel → match → promote creates the live DB properly.
    """
    from ledger.imports.pdf.readiness import import_owned_fingerprint

    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "run-1"
    run_dir.mkdir(parents=True)

    csv_root = run_dir / "csv"
    csv_root.mkdir(parents=True)

    # Target DB deliberately does NOT exist (first import).
    target_db = tmp_path / "ledger.db"
    assert not target_db.exists(), "Precondition: no live DB for first import"

    # Staging DB is created fresh (no live to clone from → init_database path).
    staging_db = run_dir / "staging.db"
    init_database(staging_db)

    # Insert a staged transaction so there's real data to promote.
    insert_minimal_transaction(staging_db, "txn-first-import-001")

    # Capture fingerprints exactly as stage() does for a first import.
    target_db_import_owned_before = import_owned_fingerprint(target_db)

    # CRITICAL PRE-FIX FAILURE POINT: before the fix, the line above creates a
    # 0-byte file at target_db. The test asserts the file is still absent here.
    assert not target_db.exists(), (
        "import_owned_fingerprint must NOT create a file. "
        "Pre-fix this fails because sqlite3.connect creates a 0-byte stub."
    )

    # The sentinel must be the absent-sentinel.
    assert target_db_import_owned_before == {"tables": {}}, (
        f"Expected absent-sentinel for first import; got {target_db_import_owned_before!r}"
    )

    # Write a run.json that mirrors what stage() would produce.
    from ledger.imports.pdf.files import write_run_state

    before_fingerprint = {
        "path": str(target_db),
        "exists": False,
        "sha256": "",
        "size_bytes": 0,
        "modified_ns": 0,
        "counts": {},
    }
    write_run_state(
        run_dir,
        bound_import_state(
            {
                "status": "ready_to_finalize",
                "target_db_path": str(target_db),
                "staging_db_path": str(staging_db),
                "csv_root": str(csv_root),
                "staging_inbox": str(run_dir / "inbox"),
                "merchant_review_json": str(run_dir / "merchant-review.json"),
                "pending_review_count": 0,
                "target_db_fingerprint_before": before_fingerprint,
                "target_db_import_owned_before": target_db_import_owned_before,
                "pdfs": [],
            },
            _make_finalize_paths(tmp_path, runs_dir),
        ),
    )
    (run_dir / "merchant-review.json").write_text("[]", encoding="utf-8")

    paths = _make_finalize_paths(tmp_path, runs_dir)
    _apply_finalize_mocks(monkeypatch)

    # Finalize: the pre-promote recheck calls import_owned_fingerprint(target_db)
    # again. target_db still does not exist → absent-sentinel → matches stage-time
    # sentinel → promote proceeds → target_db is created by snapshot publication.
    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    import json

    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state["status"] == "finalized", (
        f"First-import finalize must succeed; got {state.get('status')!r}. "
        "Pre-fix: crashes with OperationalError from db_counts() on the stray 0-byte file."
    )

    # The live DB must now exist (created by promote_database's snapshot publication).
    assert target_db.exists(), (
        "After finalize, the live DB must exist (created by promote_database)."
    )

    # And must contain the staged transaction.
    assert count_transactions(target_db) >= 1, (
        "The staged transaction must be in the promoted live DB."
    )


def test_finalize_failed_post_promote_recovers_not_wedged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F4: a post-promote exception marks the run status='failed'/finalize_failed
    AFTER csv_root was deleted. Retry must recover via the same promote-already-
    happened detection as the 'finalizing' path (NOT SystemExit 'Staged CSV root
    not found', which permanently wedged the run)."""
    import shutil

    runs_dir = tmp_path / "runs"
    run_dir = runs_dir / "run-1"
    target_db, staging_db, csv_root = _setup_staged_run(tmp_path, runs_dir, run_dir)
    paths = _make_finalize_paths(tmp_path, runs_dir)

    insert_minimal_transaction(staging_db, "txn-staged-finfail")
    # Simulate a completed promote: target now carries staging's import-owned data.
    shutil.copy2(staging_db, target_db)

    # Simulate the post-promote failure state: csv_root already deleted, run wedged.
    if csv_root.exists():
        shutil.rmtree(csv_root)
    run_state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    run_state["status"] = "failed"
    run_state["failure_reason"] = "finalize_failed"
    from ledger.imports.pdf.readiness import import_owned_fingerprint

    run_state["publication_candidate_import_owned"] = import_owned_fingerprint(staging_db)
    write_run_state(run_dir, run_state)

    promote_call_count = {"n": 0}
    real_promote = workflow._promote_database_locked

    def spy_promote(*args, **kwargs):
        promote_call_count["n"] += 1
        return real_promote(*args, **kwargs)

    monkeypatch.setattr(workflow, "_promote_database_locked", spy_promote)
    _apply_finalize_mocks(monkeypatch)

    workflow.finalize(SimpleNamespace(run_dir=run_dir), paths)

    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    assert state["status"] == "finalized", (
        f"finalize_failed post-promote must recover to finalized; got {state.get('status')!r}"
    )
    assert promote_call_count["n"] == 0, "recovery must NOT re-promote"
