"""Synthetic mapped exports use the same review and publication boundaries."""

import json
import os
import sqlite3
from contextlib import closing
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from ledger.adapters.contracts import AdapterError
from ledger.adapters.csv_profile import CsvProfile, load_profile, save_profile
from ledger.import_review import get_review_rows
from ledger.imports.pdf import workflow
from ledger.imports.pdf.paths import Paths
from ledger.runtime import seed_config_path
from ledger.storage.database import StatementCoverageDB, TransactionDB, init_database, session_scope


def staged(tmp_path, monkeypatch, *, existing_target=False):
    monkeypatch.setenv("LEDGER_ADAPTER_CONFIG", str(tmp_path / "no-adapters.json"))
    inbox = tmp_path / "inbox"
    inbox.mkdir(mode=0o700)
    source = inbox / "export.csv"
    source.write_text(
        "Day,Text,Value\n2031-04-02,Example shop,-12.34\n2031-05-03,Example refund,2.00\n"
    )
    profile = tmp_path / "profile.json"
    save_profile(
        profile,
        CsvProfile(
            profile_id="fictional",
            delimiter=",",
            header_row=1,
            headers=("Day", "Text", "Value"),
            date_column="Day",
            date_format="%Y-%m-%d",
            description_column="Text",
            amount_column="Value",
            sign_convention="inflow_positive",
            currency="CAD",
        ),
    )
    base = tmp_path / "base.db"
    init_database(base)
    paths = Paths(
        pdf_dir=inbox,
        processed_dir=tmp_path / "processed",
        db_path=tmp_path / "live.db",
        base_db_path=base,
        backup_dir=tmp_path / "backups",
        runs_dir=tmp_path / "runs",
        ledger_config=seed_config_path("ledger.toml"),
        merchant_rules=seed_config_path("merchants.toml"),
        categories=seed_config_path("categories.toml"),
    )
    args = SimpleNamespace(
        csv_source=source,
        csv_profile=profile,
        institution="fictional",
        account="fictional-cash",
        account_type="chequing",
    )
    if existing_target:
        init_database(paths.db_path)
    workflow.stage(args, paths)
    run_dir = next(path.parent for path in paths.runs_dir.glob("*/run.json"))
    return paths, run_dir, source, profile


def test_mapped_stage_is_private_review_only_and_has_no_invented_coverage(tmp_path, monkeypatch):
    paths, run, source, _ = staged(tmp_path, monkeypatch)
    state = json.loads((run / "run.json").read_text())
    assert state["source_kind"] == "mapped_csv"
    assert state["import_binding"]
    assert source.exists()
    assert not paths.db_path.exists()
    with session_scope(Path(state["staging_db_path"])) as session:
        rows = session.query(TransactionDB).order_by(TransactionDB.posting_date).all()
        assert [row.amount_signed for row in rows] == [Decimal("-12.34"), Decimal("2.00")]
        assert [row.source_period for row in rows] == ["2031-04", "2031-05"]
        assert {row.account for row in rows} == {"fictional-cash"}
        assert session.query(StatementCoverageDB).count() == 0
    assert all(not path.is_symlink() for path in (run / "inbox").iterdir())


@pytest.mark.parametrize("change", ["source", "profile", "csv", "account", "configuration_path"])
def test_finalize_refuses_changed_reviewed_interpretation(tmp_path, monkeypatch, change):
    paths, run, source, profile = staged(tmp_path, monkeypatch)
    state = json.loads((run / "run.json").read_text())
    if change in {"source", "profile", "csv"}:
        path = {"source": source, "profile": profile, "csv": Path(state["approved_csv_paths"][0])}[
            change
        ]
        path.write_bytes(path.read_bytes() + b"\n")
    elif change == "account":
        state["account_slugs"] = dict.fromkeys(state["account_slugs"], "different")
        workflow.write_run_state(run, state)
    else:
        changed = tmp_path / "other-config.toml"
        changed.write_bytes(paths.ledger_config.read_bytes())
        paths = replace(paths, ledger_config=changed)
    with pytest.raises((AdapterError, SystemExit), match="import_binding_changed"):
        workflow.finalize(SimpleNamespace(run_dir=run), paths)
    assert not paths.db_path.exists()


def test_missing_legacy_binding_refuses_unpublished_run(tmp_path, monkeypatch):
    paths, run, _, _ = staged(tmp_path, monkeypatch)
    state = json.loads((run / "run.json").read_text())
    del state["import_binding"]
    workflow.write_run_state(run, state)
    with pytest.raises((AdapterError, SystemExit), match="import_binding_missing"):
        workflow.finalize(SimpleNamespace(run_dir=run), paths)
    assert not paths.db_path.exists()


def finish_review(paths, run):
    state = json.loads((run / "run.json").read_text())
    for row in get_review_rows(Path(state["staging_db_path"])):
        workflow.review_reviewed_as_is(run, runs_dir=paths.runs_dir, key=row["review_key"])


def test_mapped_finalize_archives_once_and_published_tail_needs_no_adapter(tmp_path, monkeypatch):
    paths, run, source, _ = staged(tmp_path, monkeypatch)
    finish_review(paths, run)
    workflow.finalize(SimpleNamespace(run_dir=run), paths)
    assert not source.exists()
    with session_scope(paths.db_path) as session:
        assert session.query(TransactionDB).count() == 2
        assert session.query(StatementCoverageDB).count() == 0
    assert len(list(paths.processed_dir.glob("*.csv"))) == 1
    # A proven publication replays artifact completion without reinterpreting.
    monkeypatch.setattr(
        workflow,
        "verify_import_inputs",
        lambda *a, **k: pytest.fail("published tail reloaded adapter"),
    )
    workflow.finalize(SimpleNamespace(run_dir=run), paths)
    with session_scope(paths.db_path) as session:
        assert session.query(TransactionDB).count() == 2


def test_mapped_finalize_rechecks_input_after_postprocessing(tmp_path, monkeypatch):
    paths, run, source, _ = staged(tmp_path, monkeypatch)
    finish_review(paths, run)
    original = workflow.run_post_processing

    def change_source(*args, **kwargs):
        result = original(*args, **kwargs)
        source.write_bytes(source.read_bytes() + b"\n")
        return result

    monkeypatch.setattr(workflow, "run_post_processing", change_source)
    with pytest.raises(AdapterError, match="import_binding_changed"):
        workflow.finalize(SimpleNamespace(run_dir=run), paths)
    assert not paths.db_path.exists()


@pytest.mark.parametrize("protocol", [1, 2])
@pytest.mark.parametrize("change", ["inode", "token"])
def test_finalized_replay_requires_publication_proof_before_tail(
    tmp_path, monkeypatch, protocol, change
):
    from ledger.storage.snapshots import snapshot_database

    paths, run, _, _ = staged(tmp_path, monkeypatch)
    finish_review(paths, run)
    workflow.finalize(SimpleNamespace(run_dir=run), paths)
    state_path = run / "run.json"
    state = json.loads(state_path.read_text())
    state["finalize_protocol_version"] = protocol
    workflow.write_run_state(run, state)
    if change == "inode":
        replacement = tmp_path / "replacement.db"
        snapshot_database(paths.db_path, replacement)
        os.replace(replacement, paths.db_path)
    else:
        with closing(sqlite3.connect(paths.db_path)) as connection, connection:
            connection.execute(
                "update config_metadata set value = ? where key = ?",
                ("0" * 32, "__ledger_pdf_publication_token"),
            )
    before_state = state_path.read_bytes()
    before_database = paths.db_path.read_bytes()
    before_report = Path(state["final_report"]).read_bytes()
    monkeypatch.setattr(
        workflow, "verify_import_inputs", lambda *a, **k: pytest.fail("adapter loaded")
    )
    with pytest.raises(SystemExit, match="Publication recovery proof is unavailable"):
        workflow.finalize(SimpleNamespace(run_dir=run), paths)
    assert state_path.read_bytes() == before_state
    assert paths.db_path.read_bytes() == before_database
    assert Path(state["final_report"]).read_bytes() == before_report


@pytest.mark.parametrize("case", ["later", "overlap", "account", "institution", "profile"])
def test_successive_exports_retain_prior_rows_and_respect_explicit_binding(
    tmp_path, monkeypatch, case
):
    paths, run, source, profile = staged(tmp_path, monkeypatch)
    original_bytes = source.read_bytes()
    finish_review(paths, run)
    workflow.finalize(SimpleNamespace(run_dir=run), paths)
    with session_scope(paths.db_path) as session:
        original_ids = {row.txn_id for row in session.query(TransactionDB).all()}
    paths = replace(paths, base_db_path=paths.db_path)
    args = SimpleNamespace(
        csv_source=source,
        csv_profile=profile,
        institution="fictional",
        account="fictional-cash",
        account_type="chequing",
    )
    if case == "later":
        content = b"Day,Text,Value\n2031-06-01,New purchase,-3.00\n"
    elif case == "overlap":
        content = b"Day,Text,Value\n2031-04-02,Example shop,-12.34\n2031-06-01,New purchase,-3.00\n"
    else:
        content = original_bytes
    if case == "account":
        args.account = "second-cash"
    elif case == "institution":
        args.institution = "second-provider"
        args.account = "second-cash"
    elif case == "profile":
        changed_profile = tmp_path / "usd-profile.json"
        save_profile(changed_profile, replace(load_profile(profile), currency="USD"))
        args.csv_profile = changed_profile
    source.write_bytes(content)
    prior_runs = set(paths.runs_dir.glob("*/run.json"))
    workflow.stage(args, paths)
    new_states = set(paths.runs_dir.glob("*/run.json")) - prior_runs
    assert len(new_states) == 1
    new_run = new_states.pop().parent
    state = json.loads((new_run / "run.json").read_text())
    expected_count = 3 if case in {"later", "overlap"} else 4
    with session_scope(Path(state["staging_db_path"])) as session:
        assert session.query(TransactionDB).count() == expected_count
        assert original_ids <= {row.txn_id for row in session.query(TransactionDB).all()}
    finish_review(paths, new_run)
    workflow.finalize(SimpleNamespace(run_dir=new_run), paths)
    with session_scope(paths.db_path) as session:
        rows = session.query(TransactionDB).all()
        assert len(rows) == expected_count
        assert original_ids <= {row.txn_id for row in rows}
        assert session.query(StatementCoverageDB).count() == 0
        if case == "account":
            assert {row.account for row in rows} == {"fictional-cash", "second-cash"}
        elif case == "institution":
            assert {row.institution for row in rows} == {"fictional", "second-provider"}
        elif case == "profile":
            assert {row.currency for row in rows} == {"CAD", "USD"}


def test_account_name_cannot_be_rebound_to_another_institution(tmp_path, monkeypatch):
    paths, run, source, profile = staged(tmp_path, monkeypatch)
    original_bytes = source.read_bytes()
    finish_review(paths, run)
    workflow.finalize(SimpleNamespace(run_dir=run), paths)
    before = paths.db_path.read_bytes()
    source.write_bytes(original_bytes)
    with pytest.raises(AdapterError, match="import_account_conflict"):
        workflow.stage(
            SimpleNamespace(
                csv_source=source,
                csv_profile=profile,
                institution="second-provider",
                account="fictional-cash",
                account_type="chequing",
            ),
            replace(paths, base_db_path=paths.db_path),
        )
    assert paths.db_path.read_bytes() == before
    assert source.read_bytes() == original_bytes
    failed = [
        json.loads(path.read_text())
        for path in paths.runs_dir.glob("*/run.json")
        if path.parent != run
    ]
    assert len(failed) == 1 and failed[0]["status"] == "failed"


@pytest.mark.parametrize("existing_target", [False, True])
def test_late_input_drift_during_candidate_preparation_refuses_publication(
    tmp_path, monkeypatch, existing_target
):
    from ledger.imports.pdf import files

    paths, run, source, _ = staged(tmp_path, monkeypatch, existing_target=existing_target)
    finish_review(paths, run)
    before = paths.db_path.read_bytes() if existing_target else None
    original = files.snapshot_bound_database
    changed = False

    def change_source(*args, **kwargs):
        nonlocal changed
        result = original(*args, **kwargs)
        if not changed:
            source.write_bytes(source.read_bytes() + b"\n")
            changed = True
        return result

    monkeypatch.setattr(files, "snapshot_bound_database", change_source)
    with pytest.raises(AdapterError, match="import_binding_changed"):
        workflow.finalize(SimpleNamespace(run_dir=run), paths)
    assert changed
    assert source.exists()
    assert not list(paths.processed_dir.glob("*.csv"))
    if before is None:
        assert not paths.db_path.exists()
    else:
        assert paths.db_path.read_bytes() == before
