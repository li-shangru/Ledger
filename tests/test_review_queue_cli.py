"""Unit F — the sanctioned review-queue CLI (`ledger import pdf review ...`).

AGENTS.md and the ledger-pdf-import skill both say the AGENT performs Review Queue
decisions, but until this surface existed the only interfaces were the Streamlit page
and raw sqlite3 against the staging database. These tests pin the contract that makes
the CLI usable *and* safe:

* every subcommand operates on the run's STAGING database, never the live target;
* `resolve` refuses a category the applier would not recognise (an unknown name makes
  ``apply_review_decisions`` compute ``exclude_from_spending=False`` and freeze it
  behind 'manual' confidence);
* `list` reports the LIVE readiness gate, not the stale stage-time manifest snapshot;
* a finalized or cancelled run is refused outright (its staging DB survives finalize
  cleanup and would accept writes that are never promoted);
* unknown review keys and invalid status filters are loud, not silent;
* `scope` is never surfaced (the column is written but never read).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from ledger.cli import app
from ledger.config.repository import SQLiteConfigRepository
from ledger.config.schemas import (
    CategoriesConfig,
    CategoryRule,
    MerchantRulesConfig,
)
from ledger.evidence import EvidenceService
from ledger.import_review import (
    apply_review_decisions,
    pending_review_count,
    pending_review_count_if_present,
    seed_review_candidates,
)
from ledger.imports.pdf import workflow
from ledger.imports.pdf.files import write_run_state
from ledger.imports.pdf.readiness import database_fingerprint
from ledger.storage.database import init_database

runner = CliRunner()

REVIEW_KEY = "SYNTHETIC ACORN CAFE"


def _insert_txn(db_path: Path, txn_id: str, *, exclude: int = 0) -> None:
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
                "Card purchase - 7001 SYNTHETIC ACORN CAFE",
                "SYNTHETIC ACORN",
                "Other",
                "low",
                "low",
                exclude,
                REVIEW_KEY,
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


def _seed_categories(db_path: Path) -> None:
    SQLiteConfigRepository(db_path).replace_from_configs(
        MerchantRulesConfig(version="test"),
        CategoriesConfig(
            version="test",
            default_category="Other",
            categories=[
                CategoryRule(name="Other"),
                CategoryRule(name="Dining"),
                CategoryRule(name="Income", exclude_from_spending=True),
            ],
        ),
    )


def _exclude_flag(db_path: Path, txn_id: str) -> int:
    with closing(sqlite3.connect(db_path)) as conn:
        return int(
            conn.execute(
                "select exclude_from_spending from transactions where txn_id = ?",
                (txn_id,),
            ).fetchone()[0]
        )


def _staged_run(
    tmp_path: Path,
    *,
    status: str = "needs_review",
    exclude: int = 0,
) -> tuple[Path, Path, Path, Path]:
    """Build a staged run whose manifest snapshot is deliberately STALE.

    ``pending_review_count`` is written as 0 into run.json, so the manifest's
    top-level ``gates.review`` reads "pass" while the staging queue actually holds a
    pending row. That divergence is the whole point of the live-gate requirement.
    """
    runs_dir = tmp_path / "import-runs"
    run_dir = runs_dir / "20260809T120000Z"
    run_dir.mkdir(parents=True)

    target_db = tmp_path / "ledger.db"
    init_database(target_db)

    staging_db = run_dir / "staging.db"
    init_database(staging_db)
    _seed_categories(staging_db)
    _insert_txn(staging_db, "txn-1", exclude=exclude)
    seed_review_candidates(
        staging_db,
        [
            {
                "key": REVIEW_KEY,
                "merchant": "SYNTHETIC ACORN",
                "current_category": "Other",
                "txn_ids": ["txn-1"],
                "examples": [{"description": "SYNTHETIC ACORN CAFE"}],
                "count": 1,
                "total_abs_amount": Decimal("18.60"),
            }
        ],
    )

    write_run_state(
        run_dir,
        {
            "version": 1,
            "status": status,
            "target_db_path": str(target_db),
            "staging_db_path": str(staging_db),
            "csv_root": str(run_dir / "csv"),
            "pdf_dir": str(tmp_path / "inbox"),
            "processed_dir": str(tmp_path / "processed"),
            # Stage-time snapshot: stale the moment a decision is made.
            "pending_review_count": 0,
        },
    )
    return runs_dir, run_dir, staging_db, target_db


def _list_args(run_dir: Path, *extra: str) -> list[str]:
    return ["import", "pdf", "review", "list", "--run-dir", str(run_dir), *extra]


def _resolve_args(runs_dir: Path, run_dir: Path, *extra: str) -> list[str]:
    return [
        "import",
        "pdf",
        "review",
        "resolve",
        "--run-dir",
        str(run_dir),
        "--runs-dir",
        str(runs_dir),
        *extra,
    ]


def _as_is_args(runs_dir: Path, run_dir: Path, *extra: str) -> list[str]:
    return [
        "import",
        "pdf",
        "review",
        "reviewed-as-is",
        "--run-dir",
        str(run_dir),
        "--runs-dir",
        str(runs_dir),
        *extra,
    ]


# ── list ──────────────────────────────────────────────────────────────────────


def test_list_reports_the_live_readiness_gate_not_the_stale_manifest_snapshot(
    tmp_path: Path,
) -> None:
    _runs_dir, run_dir, _staging_db, _target = _staged_run(tmp_path)

    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["gates"]["review"] == "pass", "fixture must carry a stale snapshot"

    result = runner.invoke(app, _list_args(run_dir))

    assert result.exit_code == 0, result.output
    assert "review_gate=fail" in result.output
    assert "pending_review=1" in result.output
    assert f"key={REVIEW_KEY}" in result.output
    assert "status=pending" in result.output
    assert "needs_decision=true" in result.output


def test_list_json_is_stable_and_omits_dead_scope_and_filesystem_paths(
    tmp_path: Path,
) -> None:
    _runs_dir, run_dir, _staging_db, target_db = _staged_run(tmp_path)

    result = runner.invoke(app, _list_args(run_dir, "--json"))

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["run_dir"] == str(run_dir)
    assert payload["review_gate"] == "fail"
    assert payload["pending_review"] == 1
    assert payload["rows"] == [
        {
            "key": REVIEW_KEY,
            "status": "pending",
            "needs_decision": True,
            "merchant": "SYNTHETIC ACORN",
            "category": "Other",
            "note": "",
            "transactions": 1,
            "amount": "18.60",
        }
    ]
    assert "scope" not in result.output
    assert str(target_db) not in result.output
    assert "staging.db" not in result.output


def test_list_rejects_an_invalid_status_filter(tmp_path: Path) -> None:
    _runs_dir, run_dir, _staging_db, _target = _staged_run(tmp_path)

    result = runner.invoke(app, _list_args(run_dir, "--status", "done"))

    assert result.exit_code != 0
    assert "done" in result.output
    for valid in ("all", "pending", "resolved", "reviewed_as_is"):
        assert valid in result.output


def test_list_status_filter_selects_only_matching_rows(tmp_path: Path) -> None:
    runs_dir, run_dir, _staging_db, _target = _staged_run(tmp_path)

    assert runner.invoke(app, _as_is_args(runs_dir, run_dir, "--key", REVIEW_KEY)).exit_code == 0

    pending = runner.invoke(app, _list_args(run_dir, "--status", "pending", "--json"))
    as_is = runner.invoke(app, _list_args(run_dir, "--status", "reviewed_as_is", "--json"))

    assert json.loads(pending.output)["rows"] == []
    assert [row["key"] for row in json.loads(as_is.output)["rows"]] == [REVIEW_KEY]


# ── resolve: category validation (the exclude_from_spending trap) ──────────────


def test_resolve_refuses_an_unknown_category_and_lists_the_valid_ones(
    tmp_path: Path,
) -> None:
    """An unknown category is the silent-corruption path.

    ``apply_review_decisions`` does ``category_exc.get(row["category"], False)``, so an
    unknown name yields False and FLIPS exclude_from_spending from True to False while
    stamping 'manual' confidence, which no later rule pass ever repairs.
    """
    runs_dir, run_dir, staging_db, _target = _staged_run(tmp_path, exclude=1)

    result = runner.invoke(
        app,
        _resolve_args(runs_dir, run_dir, "--key", REVIEW_KEY, "--category", "Investing"),
    )

    assert result.exit_code != 0
    assert "Investing" in result.output
    assert "Dining" in result.output and "Income" in result.output

    # Nothing was written, so the flag survives an apply pass.
    assert pending_review_count(staging_db) == 1
    apply_review_decisions(staging_db)
    assert _exclude_flag(staging_db, "txn-1") == 1


def test_resolve_to_an_excluded_category_keeps_the_transaction_excluded(
    tmp_path: Path,
) -> None:
    runs_dir, run_dir, staging_db, _target = _staged_run(tmp_path, exclude=1)

    result = runner.invoke(
        app,
        _resolve_args(runs_dir, run_dir, "--key", REVIEW_KEY, "--category", "Income"),
    )

    assert result.exit_code == 0, result.output
    assert "review_gate=pass" in result.output
    assert "pending_review=0" in result.output

    apply_review_decisions(staging_db)
    assert _exclude_flag(staging_db, "txn-1") == 1
    with closing(sqlite3.connect(staging_db)) as conn:
        category, confidence = conn.execute(
            "select category, category_confidence from transactions where txn_id = 'txn-1'"
        ).fetchone()
    assert category == "Income"
    assert confidence == "manual"


def test_resolve_rejects_an_unknown_review_key(tmp_path: Path) -> None:
    runs_dir, run_dir, staging_db, _target = _staged_run(tmp_path)

    result = runner.invoke(
        app,
        _resolve_args(runs_dir, run_dir, "--key", "NO SUCH MERCHANT", "--category", "Dining"),
    )

    assert result.exit_code != 0
    assert "NO SUCH MERCHANT" in result.output
    assert pending_review_count(staging_db) == 1


def test_resolve_can_override_the_merchant_and_records_a_note(tmp_path: Path) -> None:
    runs_dir, run_dir, staging_db, _target = _staged_run(tmp_path)

    result = runner.invoke(
        app,
        _resolve_args(
            runs_dir,
            run_dir,
            "--key",
            REVIEW_KEY,
            "--category",
            "Dining",
            "--merchant",
            "SYNTHETIC ACORN DINING",
            "--note",
            "searched online",
        ),
    )

    assert result.exit_code == 0, result.output
    assert pending_review_count(staging_db) == 0

    listed = runner.invoke(app, _list_args(run_dir, "--json"))
    row = json.loads(listed.output)["rows"][0]
    assert row["merchant"] == "SYNTHETIC ACORN DINING"
    assert row["category"] == "Dining"
    assert row["note"] == "searched online"
    assert row["needs_decision"] is False


def test_resolve_preserves_the_dead_scope_column_it_never_surfaces(tmp_path: Path) -> None:
    runs_dir, run_dir, staging_db, _target = _staged_run(tmp_path)

    result = runner.invoke(
        app,
        _resolve_args(runs_dir, run_dir, "--key", REVIEW_KEY, "--category", "Dining"),
    )
    assert result.exit_code == 0, result.output

    with closing(sqlite3.connect(staging_db)) as conn:
        scope = conn.execute(
            "select scope from import_review where review_key = ?", (REVIEW_KEY,)
        ).fetchone()[0]
    assert scope == "same_raw_merchant_in_run"


def test_resolve_does_not_offer_a_scope_option(tmp_path: Path) -> None:
    runs_dir, run_dir, _staging_db, _target = _staged_run(tmp_path)

    result = runner.invoke(
        app,
        _resolve_args(
            runs_dir, run_dir, "--key", REVIEW_KEY, "--category", "Dining", "--scope", "x"
        ),
    )

    assert result.exit_code != 0


# ── reviewed-as-is ────────────────────────────────────────────────────────────


def test_reviewed_as_is_clears_the_gate_without_freezing_confidence(tmp_path: Path) -> None:
    runs_dir, run_dir, staging_db, _target = _staged_run(tmp_path)

    result = runner.invoke(app, _as_is_args(runs_dir, run_dir, "--key", REVIEW_KEY))

    assert result.exit_code == 0, result.output
    assert "review_gate=pass" in result.output
    assert pending_review_count(staging_db) == 0

    apply_review_decisions(staging_db)
    with closing(sqlite3.connect(staging_db)) as conn:
        confidence = conn.execute(
            "select category_confidence from transactions where txn_id = 'txn-1'"
        ).fetchone()[0]
    assert confidence == "low", "reviewed_as_is must leave the row updatable by rule passes"


def test_reviewed_as_is_rejects_an_unknown_review_key(tmp_path: Path) -> None:
    runs_dir, run_dir, staging_db, _target = _staged_run(tmp_path)

    result = runner.invoke(app, _as_is_args(runs_dir, run_dir, "--key", "GHOST"))

    assert result.exit_code != 0
    assert "GHOST" in result.output
    assert pending_review_count(staging_db) == 1


# ── staging-only safety ───────────────────────────────────────────────────────


@pytest.mark.parametrize("status", ["finalized", "cancelled"])
def test_every_subcommand_refuses_a_completed_run(tmp_path: Path, status: str) -> None:
    """`_cleanup_finalize_staging` removes only csv_root and staging_inbox.

    The staging DB therefore survives finalize and would accept writes that are never
    promoted, reporting success while changing nothing.
    """
    runs_dir, run_dir, staging_db, _target = _staged_run(tmp_path, status=status)

    listed = runner.invoke(app, _list_args(run_dir))
    resolved = runner.invoke(
        app, _resolve_args(runs_dir, run_dir, "--key", REVIEW_KEY, "--category", "Dining")
    )
    as_is = runner.invoke(app, _as_is_args(runs_dir, run_dir, "--key", REVIEW_KEY))

    for result in (listed, resolved, as_is):
        assert result.exit_code != 0, result.output
        assert status in result.output
    assert pending_review_count(staging_db) == 1


def test_resolve_never_touches_the_live_target_database(tmp_path: Path) -> None:
    runs_dir, run_dir, staging_db, target_db = _staged_run(tmp_path)
    before = database_fingerprint(target_db)

    result = runner.invoke(
        app,
        _resolve_args(runs_dir, run_dir, "--key", REVIEW_KEY, "--category", "Dining"),
    )

    assert result.exit_code == 0, result.output
    assert database_fingerprint(target_db) == before
    assert pending_review_count(staging_db) == 0


def test_a_doctored_run_state_cannot_retarget_the_live_database(tmp_path: Path) -> None:
    """The staging DB must be a contained child of the run directory.

    The GUI's equivalent silently retargets live; a run.json that points outside the
    run directory must be refused rather than followed.
    """
    runs_dir, run_dir, _staging_db, target_db = _staged_run(tmp_path)
    run_state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    run_state["staging_db_path"] = str(target_db)
    write_run_state(run_dir, run_state)
    before = database_fingerprint(target_db)

    result = runner.invoke(
        app,
        _resolve_args(runs_dir, run_dir, "--key", REVIEW_KEY, "--category", "Dining"),
    )

    assert result.exit_code != 0
    assert database_fingerprint(target_db) == before


def test_a_missing_run_directory_is_a_clean_error(tmp_path: Path) -> None:
    result = runner.invoke(app, _list_args(tmp_path / "does-not-exist"))

    assert result.exit_code != 0
    assert "run.json" in result.output


# ── both command trees ────────────────────────────────────────────────────────


def test_review_list_is_registered_on_the_script_command_tree(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`./scripts/import-pdf-inbox` is the documented entrypoint (argparse tree)."""
    _runs_dir, run_dir, _staging_db, _target = _staged_run(tmp_path)
    monkeypatch.setattr(
        sys, "argv", ["import-pdf-inbox", "review", "list", "--run-dir", str(run_dir)]
    )

    assert workflow.main() == 0

    output = capsys.readouterr().out
    assert "review_gate=fail" in output
    assert "pending_review=1" in output


def test_review_resolve_is_registered_on_the_script_command_tree(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs_dir, run_dir, staging_db, _target = _staged_run(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "import-pdf-inbox",
            "review",
            "resolve",
            "--run-dir",
            str(run_dir),
            "--runs-dir",
            str(runs_dir),
            "--key",
            REVIEW_KEY,
            "--category",
            "Dining",
        ],
    )

    assert workflow.main() == 0

    assert "review_gate=pass" in capsys.readouterr().out
    assert pending_review_count(staging_db) == 0


def test_reviewed_as_is_is_registered_on_the_script_command_tree(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runs_dir, run_dir, staging_db, _target = _staged_run(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "import-pdf-inbox",
            "review",
            "reviewed-as-is",
            "--run-dir",
            str(run_dir),
            "--runs-dir",
            str(runs_dir),
            "--key",
            REVIEW_KEY,
        ],
    )

    assert workflow.main() == 0

    assert "review_gate=pass" in capsys.readouterr().out
    assert pending_review_count(staging_db) == 0


def test_script_tree_review_does_not_accept_a_target_db_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review is staging-only, so the target-DB flags must not exist on it."""
    _runs_dir, run_dir, _staging_db, target_db = _staged_run(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        ["import-pdf-inbox", "review", "list", "--run-dir", str(run_dir), "--db", str(target_db)],
    )

    with pytest.raises(SystemExit):
        workflow.main()


# ── `ledger evidence status` must report the real pending-review count ────────


def test_evidence_summary_counts_the_active_import_runs_pending_reviews(
    tmp_path: Path,
) -> None:
    """Evidence defaulted pending_reviews=0, so a CLI-only agent was told review was
    clear in exactly the window where it is not: mid-import, with the decisions still
    sitting in the run's staging database."""
    runs_dir, _run_dir, _staging_db, target_db = _staged_run(tmp_path)

    summary = EvidenceService(
        target_db,
        audit_runs_dir=tmp_path / "audit-runs",
        import_runs_dir=runs_dir,
    ).summary()

    assert summary["review"]["pending"] == 1
    assert summary["review"]["status"] == "warn"
    assert any("review" in action.lower() for action in summary["next_actions"])


def test_evidence_summary_ignores_a_completed_runs_leftover_staging_queue(
    tmp_path: Path,
) -> None:
    runs_dir, _run_dir, _staging_db, target_db = _staged_run(tmp_path, status="cancelled")

    summary = EvidenceService(
        target_db,
        audit_runs_dir=tmp_path / "audit-runs",
        import_runs_dir=runs_dir,
    ).summary()

    assert summary["review"]["pending"] == 0
    assert summary["review"]["status"] == "pass"


def test_evidence_summary_still_honours_an_explicit_count(tmp_path: Path) -> None:
    runs_dir, _run_dir, _staging_db, target_db = _staged_run(tmp_path)

    summary = EvidenceService(
        target_db,
        audit_runs_dir=tmp_path / "audit-runs",
        import_runs_dir=runs_dir,
    ).summary(pending_reviews=7)

    assert summary["review"]["pending"] == 7


def test_evidence_status_command_reports_the_real_pending_count(tmp_path: Path) -> None:
    runs_dir, _run_dir, _staging_db, target_db = _staged_run(tmp_path)

    result = runner.invoke(
        app,
        [
            "evidence",
            "status",
            "--db",
            str(target_db),
            "--audit-runs-dir",
            str(tmp_path / "audit-runs"),
            "--import-runs-dir",
            str(runs_dir),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["review"] == {
        "status": "warn",
        "pending": 1,
        "detail": "1 pending review row(s)",
    }


def test_counting_pending_reviews_never_creates_a_database(tmp_path: Path) -> None:
    """The evidence path is read-only; `pending_review_count` calls ensure_review_schema
    (which CREATES tables), so it must not be the counter used here."""
    missing = tmp_path / "absent.db"

    assert pending_review_count_if_present(missing) == 0
    assert not missing.exists()


# ── docs ──────────────────────────────────────────────────────────────────────


def test_the_review_cli_is_documented_for_agents() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    for relative in (
        "docs/agent/imports-and-recovery.md",
        "docs/agent/skills/ledger-pdf-import/SKILL.md",
    ):
        text = (repo_root / relative).read_text(encoding="utf-8")
        assert "review list" in text, relative
        assert "review resolve" in text, relative
        assert "review reviewed-as-is" in text, relative
