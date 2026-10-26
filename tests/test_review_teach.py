"""D23/D24: teaching from the review queue, and the refusal that has a version.

Two surfaces offer teaching after P6 — the review CLI (``review resolve --teach``) and
the Streamlit Transactions page. The read-only MCP server stays read-only.

``review resolve --teach`` writes the rule into the RUN'S STAGING database, which is
what finalize promotes. Teaching the LIVE database while a run is open is refused
instead: config tables and ``transactions`` are both import-owned by deliberate fail-safe
choice (``imports/pdf/readiness.py``), so a live teach trips ``import_owned_fingerprint``
twice over and can strand the staged review decisions. The refusal covers ordinary
finalizable states and finalize-recovery states before that write occurs.

The refusal message is VERSIONED. From P6 it names the remedies that exist: close the run,
or teach inside it. P9 ships the proposal queue and must amend the sentence in the same
commit that makes the queue real — a refusal naming a command the operator cannot run is
worse than one naming a command they can. Two tests below hold that hedge to its
expiry date.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import textwrap
import traceback
from contextlib import closing, contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from click import unstyle
from typer.testing import CliRunner

from ledger.cli import app
from ledger.config.repository import SQLiteConfigRepository
from ledger.config.schemas import (
    CategoriesConfig,
    CategoryRule,
    MerchantRule,
    MerchantRulesConfig,
)
from ledger.import_review import seed_review_candidates
from ledger.imports.pdf import workflow
from ledger.imports.pdf.files import run_coordination_dir, write_run_state
from ledger.imports.pdf.paths import Paths
from ledger.imports.pdf.readiness import (
    OpenImportCoordinationError,
    import_owned_fingerprint,
    import_owned_match,
    open_runs_for_target,
)
from ledger.rules.categorization import (
    OPEN_IMPORT_RUN_REMEDY,
    CategorizationService,
    TeachingDuringOpenImportRefused,
)
from ledger.rules.configuration import RuntimeConfigMutationService
from ledger.storage.database import init_database

runner = CliRunner()
REPO_ROOT = Path(__file__).resolve().parents[1]

RAW_TEXT = "DEMO FERN/MEALCO TESTVILLE ON"


# ── fixtures ──────────────────────────────────────────────────────────────────


def _seed_config(db_path: Path) -> None:
    """A runtime config with at least one rule, so the staging clone does not fall back
    to seeding from the scrubbed placeholder TOMLs."""
    SQLiteConfigRepository(db_path).replace_from_configs(
        MerchantRulesConfig(
            version="test",
            rules=[
                MerchantRule(
                    merchant_norm="PLACEHOLDER",
                    category="Other",
                    patterns=["ZZZ_MATCHES_NOTHING"],
                )
            ],
        ),
        CategoriesConfig(
            version="test",
            default_category="Other",
            categories=[
                CategoryRule(name="Other"),
                CategoryRule(name="Dining"),
                CategoryRule(name="Investing", exclude_from_spending=True),
            ],
        ),
    )


def _merchant_rules(db_path: Path) -> dict[str, tuple[str, list[str], bool]]:
    return {
        rule.merchant_norm: (rule.category, list(rule.patterns), rule.exclude_from_spending)
        for rule in SQLiteConfigRepository(db_path).load_merchant_rules().rules
    }


def _staged_run(tmp_path: Path, *, status: str = "needs_review") -> tuple[Path, Path, Path, Path]:
    """A minimal staged run: staging DB with one transaction and one pending review row."""
    data_dir = tmp_path / "data"
    runs_dir = data_dir / "import-runs"
    run_dir = runs_dir / "20260809T120000Z"
    run_dir.mkdir(parents=True, mode=0o700)

    target_db = data_dir / "ledger.db"
    init_database(target_db)
    _seed_config(target_db)

    staging_db = run_dir / "staging.db"
    init_database(staging_db)
    _seed_config(staging_db)
    _insert_txn(staging_db, "txn-1")
    seed_review_candidates(
        staging_db,
        [
            {
                "key": RAW_TEXT,
                "merchant": "",
                "current_category": "Other",
                "txn_ids": ["txn-1"],
                "examples": [{"description": f"Card purchase - 7301 {RAW_TEXT}"}],
                "count": 1,
                "total_abs_amount": "26.40",
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
            "pending_review_count": 1,
        },
    )
    return runs_dir, run_dir, staging_db, target_db


def _insert_txn(db_path: Path, txn_id: str) -> None:
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
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                txn_id,
                "2045-01-22",
                "harbor",
                "harbor-chequing",
                "Chequing",
                "expense",
                "expense",
                1,
                "CAD",
                "26.40",
                "-26.40",
                f"Card purchase - 7301 {RAW_TEXT}",
                "",
                "Other",
                "low",
                "low",
                0,
                RAW_TEXT,
                "",
                "statement.csv",
                "2045-01/statement.csv",
                "2045-01",
                1,
                "2045-01-23T00:00:00",
                "test",
                f"fingerprint-{txn_id}",
                6,
                22,
                3,
                1,
                0,
                0,
            ),
        )


def _resolve(runs_dir: Path, run_dir: Path, *extra: str):
    return runner.invoke(
        app,
        [
            "import",
            "pdf",
            "review",
            "resolve",
            "--run-dir",
            str(run_dir),
            "--runs-dir",
            str(runs_dir),
            "--key",
            RAW_TEXT,
            *extra,
        ],
    )


# ── review resolve --teach ────────────────────────────────────────────────────


def test_teach_creates_the_rule_in_the_runs_staging_config(tmp_path: Path) -> None:
    runs_dir, run_dir, staging_db, _target = _staged_run(tmp_path)

    result = _resolve(
        runs_dir, run_dir, "--category", "Dining", "--merchant", "SYNTHETIC MEALS", "--teach"
    )

    assert result.exit_code == 0, result.output
    rules = _merchant_rules(staging_db)
    assert "SYNTHETIC MEALS" in rules, rules
    category, patterns, _exclude = rules["SYNTHETIC MEALS"]
    assert category == "Dining"
    # Derived from the RAW statement text, so it can fire at a future import.
    assert any(re.search(pattern, RAW_TEXT, re.IGNORECASE) for pattern in patterns), patterns
    assert "taught=true" in result.output


def test_resolve_without_teach_creates_no_rule(tmp_path: Path) -> None:
    runs_dir, run_dir, staging_db, _target = _staged_run(tmp_path)

    result = _resolve(runs_dir, run_dir, "--category", "Dining", "--merchant", "SYNTHETIC MEALS")

    assert result.exit_code == 0, result.output
    assert "SYNTHETIC MEALS" not in _merchant_rules(staging_db)
    assert "taught=false" in result.output


def test_teach_never_touches_the_live_target_database(tmp_path: Path) -> None:
    """Review is staging-only. Teaching does not get to be the exception: a live write
    is exactly what would abort the run it is part of."""
    runs_dir, run_dir, _staging_db, target_db = _staged_run(tmp_path)
    before = import_owned_fingerprint(target_db)

    result = _resolve(
        runs_dir, run_dir, "--category", "Dining", "--merchant", "SYNTHETIC MEALS", "--teach"
    )

    assert result.exit_code == 0, result.output
    assert import_owned_match(before, import_owned_fingerprint(target_db))
    assert "SYNTHETIC MEALS" not in _merchant_rules(target_db)


def test_teach_inherits_the_target_categorys_exclusion(tmp_path: Path) -> None:
    """The F10 guard reaches this surface too: a new rule starts with the target
    category's spend semantics, and the CLI discloses that result."""
    runs_dir, run_dir, staging_db, _target = _staged_run(tmp_path)

    result = _resolve(
        runs_dir, run_dir, "--category", "Investing", "--merchant", "SYNTHETIC MEALS", "--teach"
    )

    assert result.exit_code == 0, result.output
    _category, _patterns, exclude = _merchant_rules(staging_db)["SYNTHETIC MEALS"]
    assert exclude is True
    assert "taught_stored_merchant_exclude_from_spending=true" in result.output
    assert "taught_effective_exclude_from_spending=true" in result.output


def test_teach_preserves_and_discloses_an_existing_rules_stored_exclusion(
    tmp_path: Path,
) -> None:
    """Mutation guard for the production CLI path: injecting an explicit False into
    ``_teach_from_review_row`` must flip this assertion red."""
    runs_dir, run_dir, staging_db, _target = _staged_run(tmp_path)
    repo = SQLiteConfigRepository(staging_db)
    repo.replace_from_configs(
        MerchantRulesConfig(
            version="test",
            rules=[
                MerchantRule(
                    merchant_norm="SYNTHETIC MEALS",
                    category="Investing",
                    patterns=["LEGACY_PATTERN"],
                    exclude_from_spending=True,
                )
            ],
        ),
        repo.load_taxonomy(),
    )

    result = _resolve(
        runs_dir, run_dir, "--category", "Dining", "--merchant", "SYNTHETIC MEALS", "--teach"
    )

    assert result.exit_code == 0, result.output
    category, patterns, exclude = _merchant_rules(staging_db)["SYNTHETIC MEALS"]
    assert category == "Dining"
    assert "LEGACY_PATTERN" in patterns
    assert exclude is True
    assert "taught_stored_merchant_exclude_from_spending=true" in result.output
    assert "taught_effective_exclude_from_spending=true" in result.output


def test_teach_cli_discloses_effective_category_exclusion_without_flipping_stored_flag(
    tmp_path: Path,
) -> None:
    """D20 preserves a stored False even when an excluded category makes rows excluded.

    The operator-facing fields report the stored and effective results independently,
    while the persisted rule proves no silent invariant rewrite occurred.
    """
    runs_dir, run_dir, staging_db, _target = _staged_run(tmp_path)
    repo = SQLiteConfigRepository(staging_db)
    repo.replace_from_configs(
        MerchantRulesConfig(
            version="test",
            rules=[
                MerchantRule(
                    merchant_norm="SYNTHETIC MEALS",
                    category="Dining",
                    patterns=["LEGACY_PATTERN"],
                    exclude_from_spending=False,
                )
            ],
        ),
        repo.load_taxonomy(),
    )

    result = _resolve(
        runs_dir,
        run_dir,
        "--category",
        "Investing",
        "--merchant",
        "SYNTHETIC MEALS",
        "--teach",
    )

    assert result.exit_code == 0, result.output
    category, _patterns, stored_exclude = _merchant_rules(staging_db)["SYNTHETIC MEALS"]
    assert category == "Investing"
    assert stored_exclude is False
    assert "taught_stored_merchant_exclude_from_spending=false" in result.output
    assert "taught_effective_exclude_from_spending=true" in result.output
    assert "taught_exclude_from_spending=" not in result.output


def test_teach_with_an_unusable_pattern_refuses_and_records_no_decision(
    tmp_path: Path,
) -> None:
    """The rule is built and persisted BEFORE the decision, so a rejected rule leaves
    the review row exactly as it was rather than half-applying the operator's intent."""
    runs_dir, run_dir, staging_db, _target = _staged_run(tmp_path)

    result = _resolve(
        runs_dir,
        run_dir,
        "--category",
        "Dining",
        "--merchant",
        "SYNTHETIC MEALS",
        "--teach",
        "--pattern",
        "DEMO (FERN",
    )

    assert result.exit_code != 0
    assert "SYNTHETIC MEALS" not in _merchant_rules(staging_db)
    with closing(sqlite3.connect(staging_db)) as conn:
        status = conn.execute(
            "select status from import_review where review_key = ?", (RAW_TEXT,)
        ).fetchone()[0]
    assert status == "pending"


def test_teach_uses_an_explicit_pattern_when_one_is_given(tmp_path: Path) -> None:
    runs_dir, run_dir, staging_db, _target = _staged_run(tmp_path)

    result = _resolve(
        runs_dir,
        run_dir,
        "--category",
        "Dining",
        "--merchant",
        "SYNTHETIC MEALS",
        "--teach",
        "--pattern",
        "MEALCO",
    )

    assert result.exit_code == 0, result.output
    _category, patterns, _exclude = _merchant_rules(staging_db)["SYNTHETIC MEALS"]
    assert patterns == ["MEALCO"]


def test_automatic_teach_refuses_when_the_staged_row_has_no_raw_text(tmp_path: Path) -> None:
    runs_dir, run_dir, staging_db, _target = _staged_run(tmp_path)
    with closing(sqlite3.connect(staging_db)) as conn, conn:
        conn.execute(
            "update transactions set merchant_raw = '', description = '' where txn_id = 'txn-1'"
        )

    result = _resolve(
        runs_dir, run_dir, "--category", "Dining", "--merchant", "SYNTHETIC LABEL", "--teach"
    )

    assert result.exit_code != 0
    assert "requires raw statement text" in result.output
    assert "SYNTHETIC LABEL" not in _merchant_rules(staging_db)


def test_explicit_raw_derived_pattern_allows_teach_when_staged_raw_text_is_absent(
    tmp_path: Path,
) -> None:
    runs_dir, run_dir, staging_db, _target = _staged_run(tmp_path)
    with closing(sqlite3.connect(staging_db)) as conn, conn:
        conn.execute(
            "update transactions set merchant_raw = '', description = '' where txn_id = 'txn-1'"
        )

    result = _resolve(
        runs_dir,
        run_dir,
        "--category",
        "Dining",
        "--merchant",
        "SYNTHETIC LABEL",
        "--teach",
        "--pattern",
        "SYNTHETIC RAW EVIDENCE",
    )

    assert result.exit_code == 0, result.output
    assert _merchant_rules(staging_db)["SYNTHETIC LABEL"][1] == ["SYNTHETIC RAW EVIDENCE"]


def test_teach_rolls_back_the_rule_if_recording_the_review_decision_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs_dir, run_dir, staging_db, _target = _staged_run(tmp_path)

    def fail_decision_write(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("synthetic decision-write failure")

    monkeypatch.setattr(workflow, "save_review_rows", fail_decision_write)
    result = _resolve(
        runs_dir, run_dir, "--category", "Dining", "--merchant", "SYNTHETIC MEALS", "--teach"
    )

    assert result.exit_code != 0
    assert "SYNTHETIC MEALS" not in _merchant_rules(staging_db)
    with closing(sqlite3.connect(staging_db)) as conn:
        status = conn.execute(
            "select status from import_review where review_key = ?", (RAW_TEXT,)
        ).fetchone()[0]
    assert status == "pending"


def test_teach_rolls_back_both_rule_and_decision_if_save_raises_after_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs_dir, run_dir, staging_db, _target = _staged_run(tmp_path)
    real_save = workflow.save_review_rows

    def save_then_fail(*args: object, **kwargs: object) -> None:
        real_save(*args, **kwargs)  # type: ignore[arg-type]
        raise RuntimeError("synthetic post-commit failure")

    monkeypatch.setattr(workflow, "save_review_rows", save_then_fail)
    result = _resolve(
        runs_dir, run_dir, "--category", "Dining", "--merchant", "SYNTHETIC MEALS", "--teach"
    )

    assert result.exit_code != 0
    assert "SYNTHETIC MEALS" not in _merchant_rules(staging_db)
    with closing(sqlite3.connect(staging_db)) as conn:
        status, merchant, category = conn.execute(
            "select status, merchant, category from import_review where review_key = ?",
            (RAW_TEXT,),
        ).fetchone()
    assert (status, merchant, category) == ("pending", "", "Other")


def test_pattern_without_teach_is_refused_and_records_nothing(tmp_path: Path) -> None:
    runs_dir, run_dir, staging_db, _target = _staged_run(tmp_path)

    result = _resolve(
        runs_dir,
        run_dir,
        "--category",
        "Dining",
        "--merchant",
        "SYNTHETIC MEALS",
        "--pattern",
        "MEALCO",
    )

    assert result.exit_code != 0
    assert "--pattern requires --teach" in result.output
    assert "SYNTHETIC MEALS" not in _merchant_rules(staging_db)
    with closing(sqlite3.connect(staging_db)) as conn:
        status = conn.execute(
            "select status from import_review where review_key = ?", (RAW_TEXT,)
        ).fetchone()[0]
    assert status == "pending"


def test_teach_is_refused_on_a_run_nothing_can_promote(tmp_path: Path) -> None:
    runs_dir, run_dir, staging_db, _target = _staged_run(tmp_path, status="finalized")

    result = _resolve(
        runs_dir, run_dir, "--category", "Dining", "--merchant", "SYNTHETIC MEALS", "--teach"
    )

    assert result.exit_code != 0
    assert "finalized" in result.output
    assert "SYNTHETIC MEALS" not in _merchant_rules(staging_db)


# ── both command trees, and the sentences that publish them ───────────────────


def test_teach_is_registered_on_the_script_command_tree(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`./scripts/import-pdf-inbox` is the documented entrypoint (the argparse tree)."""
    import sys

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
            RAW_TEXT,
            "--category",
            "Dining",
            "--merchant",
            "SYNTHETIC MEALS",
            "--teach",
        ],
    )

    assert workflow.main() == 0

    assert "taught=true" in capsys.readouterr().out
    assert "SYNTHETIC MEALS" in _merchant_rules(staging_db)


_DOCS = (
    REPO_ROOT / "docs/agent/imports-and-recovery.md",
    REPO_ROOT / "docs/agent/skills/ledger-pdf-import/SKILL.md",
)


def _documented_resolve_flags() -> set[str]:
    """Every ``--flag`` the docs show on a ``review resolve`` line, read from the docs.

    Derived rather than hand-listed: a flag published in either document is checked
    without anyone remembering to add it here, which is the failure mode where a doc
    advertises an option one of the two command trees never grew.
    """
    flags: set[str] = set()
    for doc in _DOCS:
        for line in doc.read_text(encoding="utf-8").splitlines():
            if "review resolve" not in line:
                continue
            tail = line.split("review resolve", 1)[1]
            flags.update(re.findall(r"--[a-z][a-z-]+", tail))
    return flags


def test_every_documented_resolve_flag_exists_on_both_command_trees(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys

    documented = _documented_resolve_flags()
    assert "--teach" in documented, documented
    assert "--pattern" in documented, documented

    typer_help = runner.invoke(app, ["import", "pdf", "review", "resolve", "--help"])
    assert typer_help.exit_code == 0, typer_help.output
    typer_output = unstyle(typer_help.output)

    monkeypatch.setattr(sys, "argv", ["import-pdf-inbox", "review", "resolve", "--help"])
    with pytest.raises(SystemExit) as exit_info:
        workflow.main()
    assert exit_info.value.code == 0
    script_help = unstyle(capsys.readouterr().out)

    for flag in sorted(documented):
        assert flag in typer_output, (flag, typer_output)
        assert flag in script_help, (flag, script_help)


_TEACHING_REFERENCE = REPO_ROOT / "docs/agent/configuration.md"


def test_teaching_reference_preserves_spend_semantics_and_mutation_boundaries() -> None:
    reference = " ".join(_TEACHING_REFERENCE.read_text(encoding="utf-8").split())

    assert "preserves an existing rule's stored `exclude_from_spending`" in reference
    assert "A new rule inherits the target category's exclusion" in reference
    assert "allow_spend_semantics_change=True" in reference
    assert "raw statement evidence, never the resolved display label" in reference
    assert "Streamlit" in reference
    assert "ledger import pdf review resolve --teach" in reference
    assert "read-only MCP server (`ledger.mcp.v1`) stays read-only" in reference
    assert "does not survive `rebuild-db`" in reference


def test_every_symbol_the_teaching_reference_names_actually_exists() -> None:
    """A renamed symbol or test must not leave current guidance pointing at a missing target."""
    import ledger.rules.categorization as categorization

    spec = " ".join(_TEACHING_REFERENCE.read_text(encoding="utf-8").split())

    for symbol in (
        "SpendSemanticsChangeRefused",
        "TeachingDuringOpenImportRefused",
        "OPEN_IMPORT_RUN_REMEDY",
    ):
        assert symbol in spec, symbol
        assert hasattr(categorization, symbol), symbol

    named_tests = re.findall(r"tests/test_review_teach\.py::([a-z_]+)", spec)
    assert named_tests, "the reference cites no test in this module; the check is vacuous"
    for named_test in named_tests:
        assert named_test in globals(), named_test

    named_files = re.findall(r"`(tests/test_[a-z0-9_]+\.py)`", spec)
    assert named_files, "the reference cites no test file; the check is vacuous"
    for named_file in named_files:
        assert (REPO_ROOT / named_file).is_file(), named_file


def test_agents_documents_that_teaching_the_live_database_during_a_run_is_refused() -> None:
    """The refusal only helps if the agent reads about it before it hits it."""
    agents = " ".join(
        (REPO_ROOT / "docs/agent/imports-and-recovery.md").read_text(encoding="utf-8").split()
    )

    assert "Do not teach the live database while a run is open" in agents
    assert "review resolve --teach" in agents
    assert "Finalize or cancel the run first" in agents
    assert "Rules / Settings" in agents
    assert "one-off category buttons" in agents
    assert "custom `--runs-dir`" in agents


def test_generated_review_artifact_and_operator_docs_use_staging_teaching(tmp_path: Path) -> None:
    from ledger.imports.pdf.review import write_merchant_review_markdown

    artifact = tmp_path / "merchant-review.md"
    try:
        write_merchant_review_markdown(artifact, [])
        texts = [
            artifact.read_text(encoding="utf-8"),
            (REPO_ROOT / "docs/agent/imports-and-recovery.md").read_text(encoding="utf-8"),
            (REPO_ROOT / "docs/agent/skills/ledger-pdf-import/SKILL.md").read_text(
                encoding="utf-8"
            ),
        ]
    finally:
        artifact.unlink(missing_ok=True)

    for text in texts:
        assert "review resolve --teach" in text
    assert "add confident matches to the SQLite runtime Rules/Settings config" not in texts[0]
    assert "updates SQLite runtime Rules / Settings config for confident matches" not in texts[2]


# ── D23: teaching the live database during an open run ────────────────────────


def _live_ledger_with_run(
    tmp_path: Path,
    *,
    status: str,
    runs_dir: Path | None = None,
    failure_reason: str | None = None,
) -> Path:
    """A live database with a sibling ``import-runs`` directory — the real layout.

    ``CategorizationService`` is constructed with the database path alone (that is all
    the Streamlit page has), so the runs directory it consults must be derived, not
    handed to it by the test.
    """
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "ledger.db"
    init_database(db_path)
    _seed_config(db_path)

    run_dir = (runs_dir or data_dir / "import-runs") / "20260809T120000Z"
    run_dir.mkdir(parents=True, mode=0o700)
    state = {
        "version": 1,
        "status": status,
        "target_db_path": str(db_path),
        "staging_db_path": str(run_dir / "staging.db"),
    }
    if failure_reason is not None:
        state["failure_reason"] = failure_reason
    write_run_state(
        run_dir,
        state,
    )
    return db_path


def test_teaching_the_live_database_during_an_open_run_is_refused(tmp_path: Path) -> None:
    db_path = _live_ledger_with_run(tmp_path, status="needs_review")

    with pytest.raises(TeachingDuringOpenImportRefused) as excinfo:
        CategorizationService(db_path).remember_category(
            merchant_norm="SYNTHETIC MEALS",
            pattern="MEALCO",
            category="Dining",
            refresh_analytics=False,
        )

    message = str(excinfo.value)
    assert "an import run is open" in message
    assert "open count: 1" in message
    assert re.search(r"opaque refs: [0-9a-f]{12}", message)
    assert "20260809T120000Z" not in message
    assert str(tmp_path) not in message
    assert "SYNTHETIC MEALS" not in message
    assert OPEN_IMPORT_RUN_REMEDY in message
    assert "SYNTHETIC MEALS" not in _merchant_rules(db_path)


def test_a_custom_runs_directory_is_discovered_from_durable_coordination(
    tmp_path: Path,
) -> None:
    custom_runs = tmp_path / "operator-selected" / "runs"
    db_path = _live_ledger_with_run(
        tmp_path,
        status="needs_review",
        runs_dir=custom_runs,
    )

    with pytest.raises(TeachingDuringOpenImportRefused) as excinfo:
        CategorizationService(db_path).remember_category(
            merchant_norm="SYNTHETIC MEALS",
            pattern="MEALCO",
            category="Dining",
            refresh_analytics=False,
        )

    message = str(excinfo.value)
    assert "opaque refs:" in message
    assert "20260809T120000Z" not in message
    assert str(custom_runs) not in message
    assert "SYNTHETIC MEALS" not in _merchant_rules(db_path)
    coordination_dir = run_coordination_dir(db_path)
    [record] = coordination_dir.glob("*.json")
    assert coordination_dir.stat().st_mode & 0o777 == 0o700
    assert record.stat().st_mode & 0o777 == 0o600


def test_custom_run_coordination_fails_closed_when_registered_state_is_unreadable(
    tmp_path: Path,
) -> None:
    custom_runs = tmp_path / "operator-selected" / "runs"
    db_path = _live_ledger_with_run(
        tmp_path,
        status="needs_review",
        runs_dir=custom_runs,
    )
    (custom_runs / "20260809T120000Z" / "run.json").write_text("{", encoding="utf-8")

    with pytest.raises(TeachingDuringOpenImportRefused) as excinfo:
        CategorizationService(db_path).remember_category(
            merchant_norm="SYNTHETIC MEALS",
            pattern="MEALCO",
            category="Dining",
            refresh_analytics=False,
        )

    assert "fail-closed" in str(excinfo.value)
    assert "SYNTHETIC MEALS" not in _merchant_rules(db_path)


def test_coordination_refusal_traceback_does_not_chain_private_paths(tmp_path: Path) -> None:
    from ledger.imports.pdf import readiness

    custom_runs = tmp_path / "operator-selected" / "runs"
    db_path = _live_ledger_with_run(tmp_path, status="needs_review", runs_dir=custom_runs)
    run_dir = custom_runs / "20260809T120000Z"
    saved_run = custom_runs / "saved-run"
    run_dir.rename(saved_run)
    run_dir.symlink_to(saved_run, target_is_directory=True)

    with pytest.raises(readiness.OpenImportCoordinationError) as inner_excinfo:
        readiness._read_coordinated_run(run_dir)
    inner_formatted = "".join(traceback.format_exception(inner_excinfo.value))
    assert str(tmp_path) not in inner_formatted

    with pytest.raises(TeachingDuringOpenImportRefused) as excinfo:
        CategorizationService(db_path).remember_category(
            merchant_norm="SYNTHETIC MERCHANT",
            pattern="SYNTHETIC",
            category="Dining",
            refresh_analytics=False,
        )

    formatted = "".join(traceback.format_exception(excinfo.value))
    assert "fail-closed" in formatted
    assert str(tmp_path) not in formatted


def test_top_level_coordination_refusal_suppresses_a_path_bearing_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.imports.pdf import readiness

    db_path = _live_ledger_with_run(tmp_path, status="needs_review")

    def path_bearing_registry_failure(*_args: object, **_kwargs: object) -> list[Path]:
        try:
            os.stat(tmp_path / "private-missing-registry")
        except OSError as exc:
            raise readiness.OpenImportCoordinationError("registry unreadable") from exc
        raise AssertionError("missing path unexpectedly existed")

    monkeypatch.setattr(readiness, "open_runs_for_target", path_bearing_registry_failure)

    with pytest.raises(TeachingDuringOpenImportRefused) as excinfo:
        CategorizationService(db_path).remember_category(
            merchant_norm="SYNTHETIC MERCHANT",
            pattern="SYNTHETIC",
            category="Dining",
            refresh_analytics=False,
        )

    formatted = "".join(traceback.format_exception(excinfo.value))
    assert "fail-closed" in formatted
    assert str(tmp_path) not in formatted


def test_coordination_ancestor_symlink_swap_fails_closed(tmp_path: Path) -> None:
    custom_runs = tmp_path / "operator-selected" / "runs"
    db_path = _live_ledger_with_run(tmp_path, status="needs_review", runs_dir=custom_runs)
    coordination_root = run_coordination_dir(db_path).parent
    saved_root = coordination_root.with_name("saved-coordination")
    coordination_root.rename(saved_root)
    empty = tmp_path / "empty-coordination"
    empty.mkdir(mode=0o700)
    coordination_root.symlink_to(empty, target_is_directory=True)

    with pytest.raises(TeachingDuringOpenImportRefused) as excinfo:
        CategorizationService(db_path).remember_category(
            merchant_norm="SYNTHETIC MERCHANT",
            pattern="SYNTHETIC",
            category="Dining",
            refresh_analytics=False,
        )

    assert "fail-closed" in str(excinfo.value)


def test_symlinked_registered_run_directory_fails_closed(tmp_path: Path) -> None:
    custom_runs = tmp_path / "operator-selected" / "runs"
    db_path = _live_ledger_with_run(tmp_path, status="needs_review", runs_dir=custom_runs)
    run_dir = custom_runs / "20260809T120000Z"
    saved_run = custom_runs / "saved-run"
    run_dir.rename(saved_run)
    run_dir.symlink_to(saved_run, target_is_directory=True)

    with pytest.raises(TeachingDuringOpenImportRefused) as excinfo:
        CategorizationService(db_path).remember_category(
            merchant_norm="SYNTHETIC MERCHANT",
            pattern="SYNTHETIC",
            category="Dining",
            refresh_analytics=False,
        )

    assert "fail-closed" in str(excinfo.value)


def test_terminal_coordination_hint_cannot_hide_an_open_run(tmp_path: Path) -> None:
    custom_runs = tmp_path / "operator-selected" / "runs"
    db_path = _live_ledger_with_run(tmp_path, status="needs_review", runs_dir=custom_runs)
    [record_path] = run_coordination_dir(db_path).glob("*.json")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["can_promote"] = False
    record["status"] = "finalized"
    record_path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(TeachingDuringOpenImportRefused):
        CategorizationService(db_path).remember_category(
            merchant_norm="SYNTHETIC MERCHANT",
            pattern="SYNTHETIC",
            category="Dining",
            refresh_analytics=False,
        )


@pytest.mark.parametrize("widen", ["directory", "record"])
def test_coordination_permission_drift_fails_closed(tmp_path: Path, widen: str) -> None:
    custom_runs = tmp_path / "operator-selected" / "runs"
    db_path = _live_ledger_with_run(tmp_path, status="needs_review", runs_dir=custom_runs)
    coordination_dir = run_coordination_dir(db_path)
    [record] = coordination_dir.glob("*.json")
    changed = coordination_dir if widen == "directory" else record
    changed.chmod(0o755 if widen == "directory" else 0o644)

    with pytest.raises(TeachingDuringOpenImportRefused) as excinfo:
        CategorizationService(db_path).remember_category(
            merchant_norm="SYNTHETIC MERCHANT",
            pattern="SYNTHETIC",
            category="Dining",
            refresh_analytics=False,
        )

    assert "fail-closed" in str(excinfo.value)
    assert changed.stat().st_mode & 0o777 == (0o755 if widen == "directory" else 0o644)


def test_non_regular_coordination_record_fails_closed_without_blocking(tmp_path: Path) -> None:
    custom_runs = tmp_path / "operator-selected" / "runs"
    db_path = _live_ledger_with_run(tmp_path, status="needs_review", runs_dir=custom_runs)
    [record] = run_coordination_dir(db_path).glob("*.json")
    record.unlink()
    os.mkfifo(record, 0o600)

    with pytest.raises(TeachingDuringOpenImportRefused) as excinfo:
        CategorizationService(db_path).remember_category(
            merchant_norm="SYNTHETIC MERCHANT",
            pattern="SYNTHETIC",
            category="Dining",
            refresh_analytics=False,
        )

    assert "fail-closed" in str(excinfo.value)


def test_pin_private_file_rejects_a_fifo_without_waiting_for_a_writer(tmp_path: Path) -> None:
    fifo = tmp_path / "private.fifo"
    os.mkfifo(fifo, 0o600)
    script = textwrap.dedent(
        f"""
        from pathlib import Path
        from ledger.utils.private_files import pin_existing_directory

        with pin_existing_directory(Path({str(tmp_path)!r})) as directory:
            try:
                with directory.pin_private_file({fifo.name!r}):
                    pass
            except OSError:
                raise SystemExit(0)
        raise SystemExit(2)
        """
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=3,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_missing_run_cleanup_rejects_a_contradictory_record_during_restore_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.imports.pdf import readiness

    custom_runs = tmp_path / "operator-selected" / "runs"
    db_path = _live_ledger_with_run(tmp_path, status="needs_review", runs_dir=custom_runs)
    run_dir = custom_runs / "20260809T120000Z"
    saved_run = custom_runs / "temporarily-missing-run"
    run_dir.rename(saved_run)
    [record_path] = run_coordination_dir(db_path).glob("*.json")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["can_promote"] = False
    record_path.write_text(json.dumps(record), encoding="utf-8")
    real_lstat = readiness.os.lstat

    def restore_then_report_missing(path: Path):
        if Path(path) == run_dir and saved_run.exists():
            saved_run.rename(run_dir)
            raise FileNotFoundError(str(run_dir))
        return real_lstat(path)

    monkeypatch.setattr(readiness.os, "lstat", restore_then_report_missing)

    try:
        with pytest.raises(readiness.OpenImportCoordinationError):
            readiness.open_runs_for_target(db_path)
    finally:
        if saved_run.exists():
            saved_run.rename(run_dir)

    assert run_dir.is_dir()


def test_oversized_coordination_record_fails_closed(tmp_path: Path) -> None:
    custom_runs = tmp_path / "operator-selected" / "runs"
    db_path = _live_ledger_with_run(tmp_path, status="needs_review", runs_dir=custom_runs)
    [record] = run_coordination_dir(db_path).glob("*.json")
    payload = json.loads(record.read_text(encoding="utf-8"))
    payload["padding"] = "x" * (65 * 1024)
    record.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(TeachingDuringOpenImportRefused) as excinfo:
        CategorizationService(db_path).remember_category(
            merchant_norm="SYNTHETIC MERCHANT",
            pattern="SYNTHETIC",
            category="Dining",
            refresh_analytics=False,
        )

    assert "fail-closed" in str(excinfo.value)


def test_terminal_custom_run_record_does_not_block_after_run_directory_cleanup(
    tmp_path: Path,
) -> None:
    custom_runs = tmp_path / "operator-selected" / "runs"
    db_path = _live_ledger_with_run(
        tmp_path,
        status="needs_review",
        runs_dir=custom_runs,
    )
    run_dir = custom_runs / "20260809T120000Z"
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    state["status"] = "finalized"
    write_run_state(run_dir, state)
    shutil.rmtree(custom_runs)

    result = CategorizationService(db_path).remember_category(
        merchant_norm="SYNTHETIC MEALS",
        pattern="MEALCO",
        category="Dining",
        refresh_analytics=False,
    )

    assert result.ok, result.validation_issues
    assert "SYNTHETIC MEALS" in _merchant_rules(db_path)


def test_post_publication_finalizing_tail_does_not_block_live_mutation(tmp_path: Path) -> None:
    from ledger.storage.snapshots import snapshot_database

    db_path = _live_ledger_with_run(tmp_path, status="finalizing")
    run_dir = db_path.parent / "import-runs" / "20260809T120000Z"
    staging_db = run_dir / "staging.db"
    snapshot_database(db_path, staging_db)
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    state["publication_completed"] = True
    write_run_state(run_dir, state)

    result = CategorizationService(db_path).remember_category(
        merchant_norm="SYNTHETIC MERCHANT",
        pattern="SYNTHETIC",
        category="Dining",
        refresh_analytics=False,
    )

    assert result.ok, result.validation_issues


def test_markerless_finalizing_run_blocks_live_mutation_even_when_databases_match(
    tmp_path: Path,
) -> None:
    from ledger.storage.snapshots import snapshot_database

    db_path = _live_ledger_with_run(tmp_path, status="finalizing")
    run_dir = db_path.parent / "import-runs" / "20260809T120000Z"
    snapshot_database(db_path, run_dir / "staging.db")

    with pytest.raises(TeachingDuringOpenImportRefused):
        CategorizationService(db_path).remember_category(
            merchant_norm="SYNTHETIC MERCHANT",
            pattern="SYNTHETIC",
            category="Dining",
            refresh_analytics=False,
        )


@pytest.mark.parametrize("protocol_version", [1, 2])
def test_unregistered_tokenized_current_protocol_marker_with_wrong_inode_fails_closed(
    tmp_path: Path,
    protocol_version: int,
) -> None:
    """Default-sibling discovery must not demote declared current states to legacy."""
    db_path = _live_ledger_with_run(tmp_path, status="finalizing")
    run_dir = db_path.parent / "import-runs" / "20260809T120000Z"
    staging_db = run_dir / "staging.db"
    from ledger.storage.snapshots import snapshot_database

    snapshot_database(db_path, staging_db)
    target_metadata = db_path.stat()
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    state.update(
        {
            "status": "finalizing",
            "finalize_protocol_version": protocol_version,
            "publication_phase": "published",
            "publication_token": "a" * 32,
            "publication_candidate_import_owned": import_owned_fingerprint(staging_db),
            "finalize_tail": {"idempotency_key": "synthetic"},
            "publication_completed": True,
            "publication_target_identity": {
                "dev": target_metadata.st_dev,
                "ino": target_metadata.st_ino + 1,
            },
        }
    )
    (run_dir / "run.json").write_text(json.dumps(state), encoding="utf-8")
    shutil.rmtree(run_coordination_dir(db_path).parent)

    with pytest.raises(TeachingDuringOpenImportRefused) as excinfo:
        CategorizationService(db_path).remember_category(
            merchant_norm="SYNTHETIC MERCHANT",
            pattern="SYNTHETIC",
            category="Dining",
            refresh_analytics=False,
        )

    assert "fail-closed" in str(excinfo.value)
    assert "SYNTHETIC MERCHANT" not in _merchant_rules(db_path)


def test_unregistered_malformed_current_protocol_state_fails_closed(tmp_path: Path) -> None:
    db_path = _live_ledger_with_run(tmp_path, status="finalizing")
    run_dir = db_path.parent / "import-runs" / "20260809T120000Z"
    state = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    state.update(
        {
            "finalize_protocol_version": 2,
            "publication_phase": "published",
            "publication_completed": True,
        }
    )
    (run_dir / "run.json").write_text(json.dumps(state), encoding="utf-8")
    shutil.rmtree(run_coordination_dir(db_path).parent)

    with pytest.raises(TeachingDuringOpenImportRefused):
        CategorizationService(db_path).remember_category(
            merchant_norm="SYNTHETIC MERCHANT",
            pattern="SYNTHETIC",
            category="Dining",
            refresh_analytics=False,
        )

    assert "SYNTHETIC MERCHANT" not in _merchant_rules(db_path)


@pytest.mark.parametrize(
    "legacy_evidence",
    [
        {"finalize_tail": {"idempotency_key": "synthetic"}},
        {"publication_completed": True},
        {"publication_candidate_import_owned": {"tables": {}}},
        {
            "finalize_tail": {"idempotency_key": "synthetic"},
            "publication_completed": True,
            "publication_candidate_import_owned": {"tables": {}},
        },
    ],
)
def test_unregistered_terminal_legacy_evidence_does_not_claim_a_current_protocol(
    tmp_path: Path,
    legacy_evidence: dict[str, object],
) -> None:
    db_path = _live_ledger_with_run(tmp_path, status="finalized")
    run_dir = db_path.parent / "import-runs" / "20260809T120000Z"
    run_path = run_dir / "run.json"
    state = json.loads(run_path.read_text(encoding="utf-8"))
    state.update(legacy_evidence)
    write_run_state(run_dir, state)
    shutil.rmtree(run_coordination_dir(db_path).parent)

    assert stat.S_IMODE(run_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(run_path.stat().st_mode) == 0o600
    assert open_runs_for_target(db_path) == []


@pytest.mark.parametrize(
    "current_discriminator",
    [
        {"finalize_protocol_version": 99},
        {"publication_phase": "published"},
        {"publication_token": "a" * 32},
        {"publication_target_identity": {"dev": 1, "ino": 2}},
    ],
)
def test_unregistered_unknown_or_incomplete_current_discriminator_fails_closed(
    tmp_path: Path,
    current_discriminator: dict[str, object],
) -> None:
    db_path = _live_ledger_with_run(tmp_path, status="finalized")
    run_dir = db_path.parent / "import-runs" / "20260809T120000Z"
    run_path = run_dir / "run.json"
    state = json.loads(run_path.read_text(encoding="utf-8"))
    state.update(current_discriminator)
    run_path.write_text(json.dumps(state), encoding="utf-8")
    run_path.chmod(0o600)
    shutil.rmtree(run_coordination_dir(db_path).parent)

    with pytest.raises(OpenImportCoordinationError):
        open_runs_for_target(db_path)


def test_unregistered_protocol_classification_refuses_a_legacy_inode_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.imports.pdf import readiness

    db_path = _live_ledger_with_run(tmp_path, status="finalizing")
    run_dir = db_path.parent / "import-runs" / "20260809T120000Z"
    run_path = run_dir / "run.json"
    current_state = json.loads(run_path.read_text(encoding="utf-8"))
    current_state.update(
        {
            "finalize_protocol_version": 2,
            "publication_phase": "published",
            "publication_token": "a" * 32,
            "publication_candidate_import_owned": {"tables": {}},
            "finalize_tail": {"idempotency_key": "synthetic"},
            "publication_completed": True,
            "publication_target_identity": {"dev": 1, "ino": 2},
        }
    )
    run_path.write_text(json.dumps(current_state), encoding="utf-8")
    saved_current = run_dir / "current-run.json"
    legacy = run_dir / "legacy-run.json"
    legacy.write_text(
        json.dumps(
            {
                "version": 1,
                "status": "finalized",
                "target_db_path": str(db_path),
                "staging_db_path": str(run_dir / "staging.db"),
            }
        ),
        encoding="utf-8",
    )
    legacy.chmod(0o600)
    shutil.rmtree(run_coordination_dir(db_path).parent)
    real_open = readiness.os.open
    attacked = False

    def swap_only_for_classification(path: object, *args: object, **kwargs: object) -> int:
        nonlocal attacked
        if not attacked and Path(path).name == "run.json":
            attacked = True
            run_path.rename(saved_current)
            legacy.rename(run_path)
            try:
                return real_open(path, *args, **kwargs)
            finally:
                run_path.rename(legacy)
                saved_current.rename(run_path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(readiness.os, "open", swap_only_for_classification)

    with pytest.raises(OpenImportCoordinationError):
        readiness.open_runs_for_target(db_path)

    assert attacked


@pytest.mark.parametrize(
    ("state_change", "record_change"),
    [
        ({"status": "invented"}, {"status": "invented", "can_promote": False}),
        ({"staging_db_path": None}, {}),
        ({"target_db_path": "mismatch.db"}, {}),
        ({"publication_completed": "yes"}, {}),
        ({"publication_completed": True}, {}),
        ({}, {"version": 99}),
        ({}, {"status": "invented"}),
    ],
)
def test_semantically_corrupt_coordination_fails_closed(
    tmp_path: Path,
    state_change: dict[str, object],
    record_change: dict[str, object],
) -> None:
    custom_runs = tmp_path / "operator-selected" / "runs"
    db_path = _live_ledger_with_run(tmp_path, status="needs_review", runs_dir=custom_runs)
    run_path = custom_runs / "20260809T120000Z" / "run.json"
    state = json.loads(run_path.read_text(encoding="utf-8"))
    state.update(state_change)
    run_path.write_text(json.dumps(state), encoding="utf-8")
    [record_path] = run_coordination_dir(db_path).glob("*.json")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record.update(record_change)
    record_path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(TeachingDuringOpenImportRefused) as excinfo:
        CategorizationService(db_path).remember_category(
            merchant_norm="SYNTHETIC MERCHANT",
            pattern="SYNTHETIC",
            category="Dining",
            refresh_analytics=False,
        )

    formatted = "".join(traceback.format_exception(excinfo.value))
    assert "fail-closed" in formatted
    assert str(tmp_path) not in formatted


def test_v1_phase_marker_contradiction_fails_closed_in_coordination_reads(
    tmp_path: Path,
) -> None:
    custom_runs = tmp_path / "operator-selected" / "runs"
    db_path = _live_ledger_with_run(tmp_path, status="finalizing", runs_dir=custom_runs)
    run_path = custom_runs / "20260809T120000Z" / "run.json"
    state = json.loads(run_path.read_text(encoding="utf-8"))
    state.update(
        {
            "finalize_protocol_version": 1,
            "publication_phase": "candidate_ready",
            "publication_token": "d" * 32,
            "publication_candidate_import_owned": {"tables": {}},
            "finalize_tail": {"idempotency_key": "synthetic"},
            "publication_completed": True,
        }
    )
    run_path.write_text(json.dumps(state), encoding="utf-8")
    [record_path] = run_coordination_dir(db_path).glob("*.json")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["can_promote"] = False
    record_path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(TeachingDuringOpenImportRefused):
        CategorizationService(db_path).remember_category(
            merchant_norm="SYNTHETIC MERCHANT",
            pattern="SYNTHETIC",
            category="Dining",
            refresh_analytics=False,
        )

    assert "SYNTHETIC MERCHANT" not in _merchant_rules(db_path)


def test_coordination_read_independently_rejects_v1_preparing_with_candidate_debris(
    tmp_path: Path,
) -> None:
    custom_runs = tmp_path / "operator-selected" / "runs"
    db_path = _live_ledger_with_run(tmp_path, status="finalizing", runs_dir=custom_runs)
    run_path = custom_runs / "20260809T120000Z" / "run.json"
    state = json.loads(run_path.read_text(encoding="utf-8"))
    state.update(
        {
            "finalize_protocol_version": 1,
            "publication_phase": "preparing",
            "publication_token": "e" * 32,
            "publication_candidate_import_owned": {"tables": {}},
        }
    )
    run_path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(OpenImportCoordinationError):
        open_runs_for_target(db_path)


def test_coordination_root_is_pinned_until_hashed_child_has_been_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.imports.pdf import readiness
    from ledger.imports.pdf.readiness import OpenImportCoordinationError
    from ledger.utils.private_files import PinnedPrivateDirectory

    custom_runs = tmp_path / "operator-selected" / "runs"
    db_path = _live_ledger_with_run(tmp_path, status="needs_review", runs_dir=custom_runs)
    coordination_root = run_coordination_dir(db_path).parent
    real_pin_child = PinnedPrivateDirectory.pin_child_directory
    swapped = False

    @contextmanager
    def swap_before_child_pin(self: PinnedPrivateDirectory, name: str):
        nonlocal swapped
        if self.path == coordination_root and not swapped:
            saved = coordination_root.with_name("saved-coordination")
            coordination_root.rename(saved)
            empty = tmp_path / "empty-coordination"
            empty.mkdir(mode=0o700)
            coordination_root.symlink_to(empty, target_is_directory=True)
            swapped = True
        with real_pin_child(self, name) as pinned:
            yield pinned

    monkeypatch.setattr(PinnedPrivateDirectory, "pin_child_directory", swap_before_child_pin)

    with pytest.raises(OpenImportCoordinationError):
        readiness.open_runs_for_target(db_path)

    assert swapped


@pytest.mark.parametrize(
    ("status", "failure_reason"),
    [
        ("finalizing", None),
        ("finalize_failed", None),
        ("failed", "finalize_failed"),
    ],
)
def test_recovery_promotable_runs_still_refuse_live_teaching(
    tmp_path: Path, status: str, failure_reason: str | None
) -> None:
    db_path = _live_ledger_with_run(
        tmp_path,
        status=status,
        failure_reason=failure_reason,
    )

    with pytest.raises(TeachingDuringOpenImportRefused):
        CategorizationService(db_path).remember_category(
            merchant_norm="SYNTHETIC MEALS",
            pattern="MEALCO",
            category="Dining",
            refresh_analytics=False,
        )

    assert "SYNTHETIC MEALS" not in _merchant_rules(db_path)


def test_runtime_config_mutation_refuses_before_any_write(tmp_path: Path) -> None:
    db_path = _live_ledger_with_run(tmp_path, status="needs_review")
    repo = SQLiteConfigRepository(db_path)
    before = import_owned_fingerprint(db_path)

    with pytest.raises(TeachingDuringOpenImportRefused):
        RuntimeConfigMutationService(db_path).replace_and_apply(
            repo.load_merchant_rules(),
            repo.load_taxonomy(),
            source="test_open_run",
            refresh_analytics=False,
        )

    assert import_owned_match(before, import_owned_fingerprint(db_path))


def test_one_off_category_change_refuses_before_any_write(tmp_path: Path) -> None:
    db_path = _live_ledger_with_run(tmp_path, status="needs_review")
    _insert_txn(db_path, "txn-1")
    before = import_owned_fingerprint(db_path)

    with pytest.raises(TeachingDuringOpenImportRefused):
        CategorizationService(db_path).set_transaction_category("txn-1", "Dining")

    assert import_owned_match(before, import_owned_fingerprint(db_path))


def test_runtime_config_boundary_is_held_from_recheck_through_backup_and_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ledger.rules.configuration as configuration
    from ledger.storage.snapshots import DatabaseTargetLockError, database_target_lock

    db_path = tmp_path / "data" / "ledger.db"
    db_path.parent.mkdir()
    init_database(db_path)
    _seed_config(db_path)
    repo = SQLiteConfigRepository(db_path)
    real_backup = configuration.backup_database
    observed = False

    def probe_backup(*args: object, **kwargs: object):
        nonlocal observed
        with pytest.raises(DatabaseTargetLockError), database_target_lock(db_path):
            pass
        observed = True
        return real_backup(*args, **kwargs)

    monkeypatch.setattr(configuration, "backup_database", probe_backup)
    result = RuntimeConfigMutationService(db_path).replace_and_apply(
        repo.load_merchant_rules(),
        repo.load_taxonomy(),
        source="boundary_probe",
        refresh_analytics=False,
    )

    assert result.ok, result.validation_issues
    assert observed


def test_category_optimizer_execute_safe_refuses_an_open_import(tmp_path: Path) -> None:
    from ledger.optimize.categories import CategoryOptimizationOptions, CategoryOptimizer

    db_path = _live_ledger_with_run(tmp_path, status="needs_review")
    before = import_owned_fingerprint(db_path)
    result = CategoryOptimizer().run(
        CategoryOptimizationOptions(
            db_path=db_path,
            runs_dir=tmp_path / "optimization-runs",
            execute_safe=True,
            exported_config_dir=tmp_path / "exported",
            require_repaired=False,
        )
    )

    assert not result.ok
    assert any("an import run is open" in error for error in result.errors)
    assert import_owned_match(before, import_owned_fingerprint(db_path))


def test_database_doctor_execute_refuses_an_open_import(tmp_path: Path) -> None:
    from ledger.maintenance.doctor import DatabaseDoctor

    db_path = _live_ledger_with_run(tmp_path, status="needs_review")
    before = import_owned_fingerprint(db_path)

    with pytest.raises(TeachingDuringOpenImportRefused):
        DatabaseDoctor(db_path).run(execute=True)

    assert import_owned_match(before, import_owned_fingerprint(db_path))


def _pre_migration_open_target(tmp_path: Path) -> tuple[Path, bytes]:
    db_path = tmp_path / "data" / "ledger.db"
    db_path.parent.mkdir()
    with contextlib.closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute("create table sentinel (value text)")
        connection.execute("insert into sentinel values ('unchanged')")
    db_path.chmod(0o644)
    run_dir = tmp_path / "custom-runs" / "open-run"
    run_dir.mkdir(parents=True, mode=0o700)
    write_run_state(
        run_dir,
        {
            "version": 1,
            "status": "needs_review",
            "target_db_path": str(db_path),
            "staging_db_path": str(run_dir / "staging.db"),
        },
    )
    return db_path, db_path.read_bytes()


def _assert_pre_migration_target_unchanged(db_path: Path, before: bytes) -> None:
    assert db_path.read_bytes() == before
    assert db_path.stat().st_mode & 0o777 == 0o644
    with (
        contextlib.closing(sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)) as connection,
        connection,
    ):
        assert connection.execute(
            "select name from sqlite_master where type='table' order by name"
        ).fetchall() == [("sentinel",)]


def test_database_doctor_default_refuses_before_its_documented_migration(
    tmp_path: Path,
) -> None:
    from ledger.maintenance.doctor import DatabaseDoctor

    db_path, before = _pre_migration_open_target(tmp_path)

    with pytest.raises(TeachingDuringOpenImportRefused):
        DatabaseDoctor(db_path).run()

    _assert_pre_migration_target_unchanged(db_path, before)


@pytest.mark.parametrize("execute_safe", [False, True])
def test_category_optimizer_read_phase_does_not_prepare_live_schema(
    tmp_path: Path,
    execute_safe: bool,
) -> None:
    from ledger.optimize.categories import CategoryOptimizationOptions, CategoryOptimizer

    db_path, before = _pre_migration_open_target(tmp_path)
    result = CategoryOptimizer().run(
        CategoryOptimizationOptions(
            db_path=db_path,
            runs_dir=tmp_path / "optimization-runs",
            execute_safe=execute_safe,
            exported_config_dir=tmp_path / "exported",
            require_repaired=False,
        )
    )

    assert not result.ok
    if execute_safe:
        assert any("import run is open" in error for error in result.errors)
    _assert_pre_migration_target_unchanged(db_path, before)


def test_statement_repair_config_copy_reads_live_source_without_preparing_it(
    tmp_path: Path,
) -> None:
    from ledger.repair.statements import StatementRepairOptions, copy_runtime_config

    db_path, before = _pre_migration_open_target(tmp_path)
    candidate = tmp_path / "candidate.db"
    init_database(candidate)
    options = StatementRepairOptions(
        db_path=db_path,
        pdf_dirs=[],
        processed_dir=tmp_path / "processed",
        runs_dir=tmp_path / "repair-runs",
        ledger_config=REPO_ROOT / "configs" / "ledger.toml",
        merchant_rules=REPO_ROOT / "configs" / "merchants.toml",
        categories=REPO_ROOT / "configs" / "categories.toml",
    )

    copy_runtime_config(db_path, candidate, options)

    _assert_pre_migration_target_unchanged(db_path, before)


def test_analysis_refresh_cli_refuses_an_open_import(tmp_path: Path) -> None:
    db_path = _live_ledger_with_run(tmp_path, status="needs_review")
    before = import_owned_fingerprint(db_path)

    result = runner.invoke(app, ["analysis", "refresh", "--db", str(db_path)])

    assert result.exit_code != 0
    assert "an import run is open" in result.output
    assert import_owned_match(before, import_owned_fingerprint(db_path))


def test_statement_repair_publication_refuses_an_open_import(tmp_path: Path) -> None:
    from ledger.repair.statements import promote_candidate
    from ledger.storage.snapshots import snapshot_database

    db_path = _live_ledger_with_run(tmp_path, status="needs_review")
    candidate = tmp_path / "candidate.db"
    snapshot_database(db_path, candidate)
    before = import_owned_fingerprint(db_path)

    with pytest.raises(TeachingDuringOpenImportRefused):
        promote_candidate(candidate, db_path, expected_import_owned=before)

    assert import_owned_match(before, import_owned_fingerprint(db_path))


def test_ui_config_seed_refuses_an_open_first_import(tmp_path: Path) -> None:
    from ledger.ui.context import get_category_config

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_path = data_dir / "ledger.db"
    init_database(db_path)
    run_dir = data_dir / "import-runs" / "open-run"
    run_dir.mkdir(parents=True)
    write_run_state(
        run_dir,
        {
            "version": 1,
            "status": "needs_review",
            "target_db_path": str(db_path),
            "staging_db_path": str(run_dir / "staging.db"),
        },
    )
    before = import_owned_fingerprint(db_path)
    get_category_config.clear()

    with pytest.raises(TeachingDuringOpenImportRefused):
        get_category_config(
            db_path,
            REPO_ROOT / "configs" / "merchants.toml",
            REPO_ROOT / "configs" / "categories.toml",
        )

    assert import_owned_match(before, import_owned_fingerprint(db_path))


def test_ui_config_seed_precheck_does_not_prepare_a_pre_migration_target(
    tmp_path: Path,
) -> None:
    from ledger.ui.context import get_category_config

    db_path, before = _pre_migration_open_target(tmp_path)
    get_category_config.clear()

    with pytest.raises(TeachingDuringOpenImportRefused):
        get_category_config(
            db_path,
            REPO_ROOT / "configs" / "merchants.toml",
            REPO_ROOT / "configs" / "categories.toml",
        )

    _assert_pre_migration_target_unchanged(db_path, before)


def test_a_second_stage_refuses_before_taking_a_baseline(tmp_path: Path) -> None:
    db_path = _live_ledger_with_run(tmp_path, status="needs_review")
    pdf_dir = tmp_path / "inbox"
    pdf_dir.mkdir()
    (pdf_dir / "synthetic.pdf").write_bytes(b"%PDF-1.4 synthetic")
    paths = Paths(
        pdf_dir=pdf_dir,
        processed_dir=tmp_path / "processed",
        db_path=db_path,
        base_db_path=None,
        backup_dir=tmp_path / "backups",
        ledger_config=REPO_ROOT / "configs" / "ledger.toml",
        merchant_rules=REPO_ROOT / "configs" / "merchants.toml",
        categories=REPO_ROOT / "configs" / "categories.toml",
        runs_dir=db_path.parent / "import-runs",
    )
    before_runs = sorted(paths.runs_dir.iterdir())

    with pytest.raises(TeachingDuringOpenImportRefused):
        workflow.stage(SimpleNamespace(), paths)

    assert sorted(paths.runs_dir.iterdir()) == before_runs


def test_a_coordinated_run_target_mismatch_fails_closed(
    tmp_path: Path,
) -> None:
    """A registry/state target contradiction is corruption, not a terminal hint."""
    db_path = _live_ledger_with_run(tmp_path, status="needs_review")
    run_json = db_path.parent / "import-runs" / "20260809T120000Z" / "run.json"
    state = json.loads(run_json.read_text(encoding="utf-8"))
    state["target_db_path"] = str(tmp_path / "data" / "somewhere-else.db")
    run_json.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(TeachingDuringOpenImportRefused, match="fail-closed"):
        CategorizationService(db_path).remember_category(
            merchant_norm="SYNTHETIC MEALS",
            pattern="MEALCO",
            category="Dining",
            refresh_analytics=False,
        )

    assert "SYNTHETIC MEALS" not in _merchant_rules(db_path)


@pytest.mark.parametrize("status", ["finalized", "cancelled", "failed"])
def test_teaching_is_allowed_once_the_run_can_no_longer_promote(
    tmp_path: Path, status: str
) -> None:
    db_path = _live_ledger_with_run(tmp_path, status=status)

    result = CategorizationService(db_path).remember_category(
        merchant_norm="SYNTHETIC MEALS",
        pattern="MEALCO",
        category="Dining",
        refresh_analytics=False,
    )

    assert result.ok, result.validation_issues
    assert "SYNTHETIC MEALS" in _merchant_rules(db_path)


# ── D23: the versioned refusal ────────────────────────────────────────────────


def _commands_named_in(message: str) -> set[tuple[str, ...]]:
    """Every ``ledger ...`` invocation the message prescribes, read out of the TEXT.

    Derived, not hand-maintained: a command added to the sentence is checked without
    anyone remembering to add it to a list here.
    """
    commands: set[tuple[str, ...]] = set()
    for quoted in re.findall(r"`([^`]+)`", message):
        parts = quoted.split()
        if parts[:1] != ["ledger"]:
            continue
        verbs = [
            part
            for part in parts[1:]
            if not part.startswith("-") and not part.startswith("<") and part.isascii()
        ]
        if verbs:
            commands.add(tuple(verbs))
    return commands


def test_the_open_run_refusal_names_only_commands_this_cli_can_run() -> None:
    prescribed = _commands_named_in(OPEN_IMPORT_RUN_REMEDY)

    assert prescribed, OPEN_IMPORT_RUN_REMEDY
    for command in sorted(prescribed):
        assert runner.invoke(app, [*command, "--help"]).exit_code == 0, command


def test_the_refusal_offers_the_proposal_queue_exactly_when_the_queue_exists() -> None:
    """A hedge with an expiry date (the P0/P2 precedent).

    D23 makes the refusal versioned: until ``ledger proposals`` exists it must name
    finalize-or-cancel, and the moment P9 registers the queue it must offer it instead.
    This test fails by design in the commit that adds ``ledger proposals`` unless
    ``OPEN_IMPORT_RUN_REMEDY`` is amended in the same commit.
    """
    registered = {group.name for group in app.registered_groups} | {
        command.name for command in app.registered_commands if command.name
    }

    if "proposals" in registered:
        assert "ledger proposals" in OPEN_IMPORT_RUN_REMEDY, (
            "`ledger proposals` exists now: D23 requires the refusal to offer the "
            "deferred path instead of only finalize-or-cancel"
        )
    else:
        assert "proposals" not in OPEN_IMPORT_RUN_REMEDY, (
            "the refusal names a command this CLI cannot run"
        )
        assert "finalize" in OPEN_IMPORT_RUN_REMEDY and "cancel" in OPEN_IMPORT_RUN_REMEDY


# ── D24: the Streamlit surface renders the refusal ────────────────────────────

_DRIVER = textwrap.dedent("""\
    from pathlib import Path
    from datetime import date
    import os

    from ledger.query import LedgerQueryService
    from ledger.config.repository import SQLiteConfigRepository
    from ledger.ui.context import AppContext
    from ledger.ui.pages import transactions

    _db = Path(os.environ["_LEDGER_TEACH_REFUSAL_DB"])
    _service = LedgerQueryService(_db)
    _repo = SQLiteConfigRepository(_db)
    _ctx = AppContext(
        db_path=_db,
        service=_service,
        category_config=_repo.load_categories(),
        stats=_service.get_stats(),
        earliest=date(2045, 1, 1),
        latest=date(2045, 1, 31),
        start_date=date(2045, 1, 1),
        end_date=date(2045, 1, 31),
        period_label="Jan 2045",
        review_source={
            "label": "ledger.db",
            "status": "live_db",
            "db_path": _db,
            "source_kind": "Current DB",
        },
        review_db=_db,
        pending_reviews=0,
    )
    transactions.render(_ctx)
""")


def test_the_streamlit_page_shows_the_refusal_instead_of_crashing(tmp_path: Path) -> None:
    """The refusal reaches an operator only if the page renders it. Driven through the
    real page and the real Save button, not through the service."""
    from streamlit.testing.v1 import AppTest

    db_path = _live_ledger_with_run(tmp_path, status="needs_review")
    _insert_txn(db_path, "txn-1")

    os.environ["_LEDGER_TEACH_REFUSAL_DB"] = str(db_path)
    script = tmp_path / "_driver_refusal.py"
    script.write_text(_DRIVER)
    page = AppTest.from_file(str(script), default_timeout=60)
    page.run()
    assert not page.exception, page.exception
    page.radio(key="recat_mode").set_value("Remember for this merchant").run()
    assert not page.exception, page.exception

    page.button(key="recat_save").click().run()

    assert not page.exception, page.exception
    errors = [str(element.value) for element in page.error]
    assert any("an import run is open" in error for error in errors), errors
    assert "MEALCO" not in json.dumps(_merchant_rules(db_path))


def test_the_streamlit_one_off_button_shows_the_same_refusal(tmp_path: Path) -> None:
    """The adjacent default-mode button mutates the same import-owned transaction table."""
    from streamlit.testing.v1 import AppTest

    db_path = _live_ledger_with_run(tmp_path, status="needs_review")
    _insert_txn(db_path, "txn-1")
    before = import_owned_fingerprint(db_path)

    os.environ["_LEDGER_TEACH_REFUSAL_DB"] = str(db_path)
    script = tmp_path / "_driver_one_off_refusal.py"
    script.write_text(_DRIVER)
    page = AppTest.from_file(str(script), default_timeout=60)
    page.run()
    assert not page.exception, page.exception

    page.button(key="recat_apply_one").click().run()

    assert not page.exception, page.exception
    errors = [str(element.value) for element in page.error]
    assert any("an import run is open" in error for error in errors), errors
    assert import_owned_match(before, import_owned_fingerprint(db_path))


_RULES_DRIVER = textwrap.dedent("""\
    from pathlib import Path
    from datetime import date
    import os

    from ledger.query import LedgerQueryService
    from ledger.config.repository import SQLiteConfigRepository
    from ledger.ui.context import AppContext
    from ledger.ui.pages import rules_settings

    _db = Path(os.environ["_LEDGER_RULES_REFUSAL_DB"])
    _service = LedgerQueryService(_db)
    _repo = SQLiteConfigRepository(_db)
    _ctx = AppContext(
        db_path=_db,
        service=_service,
        category_config=_repo.load_categories(),
        stats=_service.get_stats(),
        earliest=date(2026, 4, 1),
        latest=date(2026, 4, 30),
        start_date=date(2026, 4, 1),
        end_date=date(2026, 4, 30),
        period_label="Apr 2026",
        review_source={
            "label": "ledger.db",
            "status": "live_db",
            "db_path": _db,
            "source_kind": "Current DB",
        },
        review_db=_db,
        pending_reviews=0,
    )
    rules_settings.render(_ctx)
""")


def test_the_rules_settings_page_refuses_merchant_profile_save_during_a_run(
    tmp_path: Path,
) -> None:
    """Drive the published Rules / Settings editor, not only its mutation service."""
    from streamlit.testing.v1 import AppTest

    from ledger.ui.pages.rules_settings import SAVE_MERCHANTS_CONFIRM

    db_path = _live_ledger_with_run(tmp_path, status="needs_review")
    before = import_owned_fingerprint(db_path)

    os.environ["_LEDGER_RULES_REFUSAL_DB"] = str(db_path)
    script = tmp_path / "_driver_rules_refusal.py"
    script.write_text(_RULES_DRIVER)
    page = AppTest.from_file(str(script), default_timeout=60)
    page.run()
    assert not page.exception, page.exception
    page.text_input(key="rules_confirm_merchants").input(SAVE_MERCHANTS_CONFIRM).run()

    save_button = next(button for button in page.button if button.label == "Save Merchant Profiles")
    save_button.click().run()

    assert not page.exception, page.exception
    errors = [str(element.value) for element in page.error]
    assert any("an import run is open" in error for error in errors), errors
    assert import_owned_match(before, import_owned_fingerprint(db_path))


@pytest.mark.parametrize(
    ("view", "confirm_key", "confirmation", "button_label"),
    [
        (
            "Category Taxonomy",
            "rules_confirm_categories",
            "save category taxonomy",
            "Save Category Taxonomy",
        ),
        (
            "Import / Export",
            "rules_confirm_seed_import",
            "import seed config",
            "Import Seed TOML",
        ),
    ],
)
def test_every_other_rules_settings_save_button_refuses_during_a_run(
    tmp_path: Path,
    view: str,
    confirm_key: str,
    confirmation: str,
    button_label: str,
) -> None:
    from streamlit.testing.v1 import AppTest

    db_path = _live_ledger_with_run(tmp_path, status="needs_review")
    before = import_owned_fingerprint(db_path)
    os.environ["_LEDGER_RULES_REFUSAL_DB"] = str(db_path)
    script = tmp_path / f"_driver_{confirm_key}.py"
    script.write_text(_RULES_DRIVER)
    page = AppTest.from_file(str(script), default_timeout=60)
    page.run()
    assert not page.exception, page.exception
    page.segmented_control[0].set_value(view).run()
    page.text_input(key=confirm_key).input(confirmation).run()

    next(button for button in page.button if button.label == button_label).click().run()

    assert not page.exception, page.exception
    errors = [str(element.value) for element in page.error]
    assert any("an import run is open" in error for error in errors), errors
    assert import_owned_match(before, import_owned_fingerprint(db_path))


# ── the round trip (a release gate) ───────────────────────────────────────────


# Independently authored statements: each balance follows from its stated activity.


def _paths(tmp_path: Path) -> Paths:
    data_dir = tmp_path / "data"
    inbox = data_dir / "inbox"
    processed = data_dir / "processed"
    inbox.mkdir(parents=True)
    processed.mkdir(parents=True)
    target_db = data_dir / "ledger.db"
    init_database(target_db)
    _seed_config(target_db)
    return Paths(
        pdf_dir=inbox,
        processed_dir=processed,
        db_path=target_db,
        base_db_path=None,
        backup_dir=data_dir / "db-backups",
        ledger_config=REPO_ROOT / "configs" / "ledger.toml",
        merchant_rules=REPO_ROOT / "configs" / "merchants.toml",
        categories=REPO_ROOT / "configs" / "categories.toml",
        runs_dir=data_dir / "import-runs",
    )


def _stage_statement(
    paths: Paths, monkeypatch: pytest.MonkeyPatch, *, name: str, text: str
) -> Path:
    from ledger.imports.pdf import statement_pdf_to_csv

    for stale in paths.pdf_dir.glob("*.pdf"):
        stale.unlink()
    (paths.pdf_dir / name).write_bytes(f"%PDF-1.4 {name}".encode())
    monkeypatch.setattr(statement_pdf_to_csv, "run_pdftotext", lambda _path: text)
    result = runner.invoke(
        app,
        [
            "import",
            "pdf",
            "stage",
            "--pdf-dir",
            str(paths.pdf_dir),
            "--processed-dir",
            str(paths.processed_dir),
            "--db",
            str(paths.db_path),
            "--backup-dir",
            str(paths.backup_dir),
            "--runs-dir",
            str(paths.runs_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    return workflow.resolve_run_dir(paths.runs_dir, None)


def _staged_transactions(staging_db: Path) -> list[tuple[str, str, str]]:
    with closing(sqlite3.connect(staging_db)) as conn:
        return [
            (str(row[0]), str(row[1]), str(row[2]))
            for row in conn.execute(
                "select merchant, category, source_period from transactions order by posting_date"
            )
        ]
