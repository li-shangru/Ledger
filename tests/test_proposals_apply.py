"""Ledger-first, crash-recoverable application of deferred proposals."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from test_review_teach import RAW_TEXT, _insert_txn, _merchant_rules, _seed_config, _staged_run
from test_statement_coverage import _patch_repair_dependencies, _repair_options

from ledger.config.repository import SQLiteConfigRepository
from ledger.config.schemas import (
    CategoriesConfig,
    CategoryRule,
    MerchantRule,
    MerchantRulesConfig,
)
from ledger.core.results import RunResult
from ledger.imports.pdf.readiness import (
    LiveMutationDuringOpenImportRefused,
    _selected_table_fingerprint,
)
from ledger.proposals import ProposalService
from ledger.proposals.service import ProposalRestoreError
from ledger.repair.statements import StatementRepairService
from ledger.storage.database import init_database
from ledger.utils.dates import naive_utc_now
from ledger.utils.hashing import stable_id

_MUTATED_TABLES = {
    "transactions",
    "merchant_rules",
    "merchant_rule_patterns",
    "category_config",
    "category_merchants",
}


def _setup(tmp_path: Path) -> tuple[Path, Path, ProposalService, str]:
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o700)
    ledger_db = data_dir / "ledger.db"
    proposals_db = data_dir / "proposals.db"
    init_database(ledger_db)
    _seed_config(ledger_db)
    _insert_txn(ledger_db, "txn-proposal")
    service = ProposalService(proposals_db)
    proposal = service.file_merchant_rule(
        merchant_norm="DEMO FERN",
        pattern="MEALCO",
        category="Dining",
        evidence_id="d" * 40,
    )
    return ledger_db, proposals_db, service, proposal.proposal_id


def _audit_rows(db_path: Path, operation: str) -> list[sqlite3.Row]:
    with closing(sqlite3.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            "select * from db_operation_audit where operation = ? order by id",
            (operation,),
        ).fetchall()


def _transaction(db_path: Path) -> sqlite3.Row:
    with closing(sqlite3.connect(db_path)) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            "select * from transactions where txn_id = 'txn-proposal'"
        ).fetchone()


def test_apply_backs_up_then_commits_rule_transaction_and_stable_audit(
    tmp_path: Path,
) -> None:
    ledger_db, _proposals_db, service, proposal_id = _setup(tmp_path)
    backup_dir = tmp_path / "backups"

    result = service.apply_proposal(
        proposal_id,
        ledger_db=ledger_db,
        backup_dir=backup_dir,
    )

    assert result.ok, result.errors
    assert service.show_proposal(proposal_id).status == "applied"
    assert _merchant_rules(ledger_db)["DEMO FERN"] == (
        "Dining",
        ["MEALCO"],
        False,
    )
    row = _transaction(ledger_db)
    assert (
        row["merchant"],
        row["category"],
        row["merchant_confidence"],
        row["category_confidence"],
    ) == ("DEMO FERN", "Dining", "rule", "rule")

    [audit] = _audit_rows(ledger_db, "proposal_apply")
    details = json.loads(audit["details"])
    assert details["proposal_id"] == proposal_id
    assert details["idempotency_key"] == stable_id("proposal_apply", proposal_id)
    assert details["evidence_id"] == "d" * 40
    [backup_audit] = _audit_rows(ledger_db, "backup")
    backup_path = Path(backup_audit["artifact_path"])
    assert backup_path.parent == backup_dir
    assert backup_path.exists()
    assert "DEMO FERN" not in _merchant_rules(backup_path)
    assert _transaction(backup_path)["category"] == "Other"


def test_apply_preserves_existing_merchant_spend_semantics(tmp_path: Path) -> None:
    ledger_db, _proposals_db, service, proposal_id = _setup(tmp_path)
    SQLiteConfigRepository(ledger_db).replace_from_configs(
        MerchantRulesConfig(
            version="test",
            rules=[
                MerchantRule(
                    merchant_norm="DEMO FERN",
                    category="Investing",
                    patterns=["DEMO FERN"],
                    exclude_from_spending=True,
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

    result = service.apply_proposal(proposal_id, ledger_db=ledger_db)

    assert result.ok, result.errors
    assert _merchant_rules(ledger_db)["DEMO FERN"] == (
        "Dining",
        ["DEMO FERN", "MEALCO"],
        True,
    )
    assert bool(_transaction(ledger_db)["exclude_from_spending"]) is True


def test_apply_revalidates_live_config_and_refuses_drift_before_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger_db, _proposals_db, service, proposal_id = _setup(tmp_path)
    SQLiteConfigRepository(ledger_db).replace_from_configs(
        MerchantRulesConfig(
            version="changed",
            rules=[
                MerchantRule(
                    merchant_norm="PLACEHOLDER",
                    category="Other",
                    patterns=["ZZZ_MATCHES_NOTHING"],
                )
            ],
        ),
        CategoriesConfig(
            version="changed",
            default_category="Other",
            categories=[CategoryRule(name="Other")],
        ),
    )
    before = _selected_table_fingerprint(ledger_db, _MUTATED_TABLES)
    backup_called = False

    def fail_if_called(*_args, **_kwargs):
        nonlocal backup_called
        backup_called = True
        raise AssertionError("backup must not run for an invalid live target")

    monkeypatch.setattr("ledger.proposals.service.backup_database", fail_if_called)

    result = service.apply_proposal(proposal_id, ledger_db=ledger_db)

    assert result.status == "validation_failed"
    assert not result.ok
    assert not backup_called
    assert _selected_table_fingerprint(ledger_db, _MUTATED_TABLES) == before
    assert not _audit_rows(ledger_db, "proposal_apply")
    assert service.show_proposal(proposal_id).status == "pending"


def test_backup_failure_aborts_before_any_ledger_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger_db, _proposals_db, service, proposal_id = _setup(tmp_path)
    before = _selected_table_fingerprint(ledger_db, _MUTATED_TABLES)
    now = naive_utc_now()
    monkeypatch.setattr(
        "ledger.proposals.service.backup_database",
        lambda *_args, **_kwargs: RunResult(
            status="failed",
            started_at=now,
            finished_at=now,
            errors=["synthetic backup refusal"],
        ),
    )

    result = service.apply_proposal(proposal_id, ledger_db=ledger_db)

    assert result.status == "backup_failed"
    assert _selected_table_fingerprint(ledger_db, _MUTATED_TABLES) == before
    assert not _audit_rows(ledger_db, "proposal_apply")
    assert service.show_proposal(proposal_id).status == "pending"


def test_failed_ledger_mutation_restores_backup_and_keeps_proposal_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger_db, _proposals_db, service, proposal_id = _setup(tmp_path)
    before = _selected_table_fingerprint(ledger_db, _MUTATED_TABLES)
    restored: list[tuple[Path, Path]] = []
    from ledger.proposals import service as proposal_service

    real_restore = proposal_service.restore_database

    def restore(snapshot: Path, target: Path) -> Path:
        restored.append((snapshot, target))
        return real_restore(snapshot, target)

    def break_after_config(*_args, **_kwargs):
        raise RuntimeError("synthetic rule-application failure")

    monkeypatch.setattr(proposal_service, "restore_database", restore)
    monkeypatch.setattr(proposal_service, "_apply_rules_in_connection", break_after_config)

    with pytest.raises(RuntimeError, match="synthetic rule-application failure"):
        service.apply_proposal(proposal_id, ledger_db=ledger_db)

    assert len(restored) == 1
    assert restored[0][0].exists()
    assert restored[0][1] == ledger_db
    assert _selected_table_fingerprint(ledger_db, _MUTATED_TABLES) == before
    assert service.show_proposal(proposal_id).status == "pending"


def test_restore_failure_is_surfaced_without_claiming_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger_db, _proposals_db, service, proposal_id = _setup(tmp_path)
    from ledger.proposals import service as proposal_service

    monkeypatch.setattr(
        proposal_service,
        "_apply_rules_in_connection",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("mutation failed")),
    )
    monkeypatch.setattr(
        proposal_service,
        "restore_database",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("restore failed")),
    )

    with pytest.raises(ProposalRestoreError) as error:
        service.apply_proposal(proposal_id, ledger_db=ledger_db)

    assert isinstance(error.value.__cause__, OSError)
    assert service.show_proposal(proposal_id).status == "pending"


def test_crash_after_ledger_commit_recovers_once_by_audit_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger_db, proposals_db, service, proposal_id = _setup(tmp_path)
    real_mark = service._mark_applied
    monkeypatch.setattr(
        service,
        "_mark_applied",
        lambda _proposal_id: (_ for _ in ()).throw(RuntimeError("synthetic mark crash")),
    )

    with pytest.raises(RuntimeError, match="synthetic mark crash"):
        service.apply_proposal(proposal_id, ledger_db=ledger_db)

    assert service.show_proposal(proposal_id).status == "pending"
    assert len(_audit_rows(ledger_db, "proposal_apply")) == 1
    committed_tables = _selected_table_fingerprint(ledger_db, _MUTATED_TABLES)
    committed_backups = len(_audit_rows(ledger_db, "backup"))

    monkeypatch.setattr(service, "_mark_applied", real_mark)
    retry = ProposalService(proposals_db).apply_proposal(proposal_id, ledger_db=ledger_db)

    assert retry.ok
    assert retry.details["recovered_from_audit"] is True
    assert ProposalService(proposals_db).show_proposal(proposal_id).status == "applied"
    assert _selected_table_fingerprint(ledger_db, _MUTATED_TABLES) == committed_tables
    assert len(_audit_rows(ledger_db, "proposal_apply")) == 1
    assert len(_audit_rows(ledger_db, "backup")) == committed_backups


def test_manual_transaction_override_is_never_rewritten_by_apply(tmp_path: Path) -> None:
    ledger_db, _proposals_db, service, proposal_id = _setup(tmp_path)
    with closing(sqlite3.connect(ledger_db)) as connection, connection:
        connection.execute(
            """
            update transactions
            set merchant = 'OWNER CHOICE', category = 'Other',
                merchant_confidence = 'manual', category_confidence = 'manual'
            where txn_id = 'txn-proposal'
            """
        )

    result = service.apply_proposal(proposal_id, ledger_db=ledger_db)

    assert result.ok
    row = _transaction(ledger_db)
    assert (row["merchant"], row["category"]) == ("OWNER CHOICE", "Other")
    assert RAW_TEXT in row["description"]


def test_apply_refuses_an_open_import_before_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _runs_dir, _run_dir, _staging_db, ledger_db = _staged_run(tmp_path)
    service = ProposalService(ledger_db.parent / "proposals.db")
    proposal = service.file_merchant_rule(
        merchant_norm="DEMO FERN",
        pattern="MEALCO",
        category="Dining",
        evidence_id="c" * 40,
    )
    backup_called = False

    def fail_if_called(*_args, **_kwargs):
        nonlocal backup_called
        backup_called = True
        raise AssertionError("backup must follow the open-import refusal")

    monkeypatch.setattr("ledger.proposals.service.backup_database", fail_if_called)

    with pytest.raises(LiveMutationDuringOpenImportRefused):
        service.apply_proposal(proposal.proposal_id, ledger_db=ledger_db)

    assert not backup_called
    assert service.show_proposal(proposal.proposal_id).status == "pending"
    assert not _audit_rows(ledger_db, "proposal_apply")


def test_statement_repair_carries_proposal_apply_audit_forward(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    (pdf_dir / "dummy.pdf").write_bytes(b"%PDF-1.4 synthetic")
    ledger_db = tmp_path / "ledger.db"
    init_database(ledger_db)
    audit_key = stable_id("proposal_apply", "synthetic-proposal")
    with closing(sqlite3.connect(ledger_db)) as connection, connection:
        connection.execute(
            """
            insert into db_operation_audit (
                id, operation, status, started_at, finished_at, db_path,
                artifact_path, counts, warnings, errors, details
            ) values (4242, 'proposal_apply', 'success', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "2026-08-12 10:00:00",
                "2026-08-12 10:00:01",
                str(ledger_db),
                str(tmp_path / "backups" / "pre-proposal.db"),
                json.dumps({"transactions_updated": 1}),
                "[]",
                "[]",
                json.dumps({"idempotency_key": audit_key}),
            ),
        )
    _patch_repair_dependencies(monkeypatch)

    result = StatementRepairService().run(_repair_options(tmp_path, pdf_dir, dry_run=False))

    assert result.status == "success", result.summary
    with closing(sqlite3.connect(ledger_db)) as connection:
        connection.row_factory = sqlite3.Row
        carried = connection.execute("select * from db_operation_audit where id = 4242").fetchone()
    assert carried is not None
    assert carried["operation"] == "proposal_apply"
    assert json.loads(carried["details"])["idempotency_key"] == audit_key
