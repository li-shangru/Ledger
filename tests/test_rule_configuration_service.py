"""Atomic runtime configuration replacement and rule application."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from stat import S_IMODE
from types import SimpleNamespace

import pytest

from ledger.config.repository import SQLiteConfigRepository
from ledger.config.schemas import (
    CategoriesConfig,
    CategoryRule,
    MerchantRule,
    MerchantRulesConfig,
)
from ledger.core.results import RunResult
from ledger.rules.service import RuleApplicationResult, RuleApplicationService
from ledger.storage.database import TransactionDB, session_scope


def _rule_config(category: str) -> MerchantRulesConfig:
    return MerchantRulesConfig(
        version="test",
        rules=[MerchantRule(merchant_norm="ACME", category=category, patterns=["ACME"])],
    )


def _categories() -> CategoriesConfig:
    return CategoriesConfig(
        version="test",
        categories=[
            CategoryRule(name="Old", merchants=["ACME", "SEED ONLY"]),
            CategoryRule(name="New"),
        ],
    )


def _insert_acme(db_path: Path) -> None:
    posted = date(2026, 7, 1)
    with session_scope(db_path) as session:
        session.add(
            TransactionDB(
                txn_id="acme-1",
                posting_date=posted,
                transaction_date=posted,
                institution="test-card",
                account="test-account",
                account_type="credit",
                flow="expense",
                cashflow_type="expense",
                include_in_cashflow=True,
                currency="CAD",
                amount_raw=Decimal("10.00"),
                amount_signed=Decimal("-10.00"),
                description="ACME MARKET",
                merchant="ACME",
                category="Old",
                merchant_confidence="rule",
                category_confidence="rule",
                exclude_from_spending=False,
                merchant_raw="ACME MARKET",
                category_raw="Old",
                source_file="statement.csv",
                source_path="/synthetic/statement.csv",
                source_period="2026-07",
                source_row=1,
                imported_at=datetime.now(UTC).replace(tzinfo=None),
                parser_version="test",
                txn_fingerprint="acme-1-fingerprint",
                day_of_week=posted.weekday(),
                day_of_month=posted.day,
                week_of_year=posted.isocalendar()[1],
                is_weekend=False,
                is_month_start=True,
                is_month_end=False,
            )
        )


def _seed(db_path: Path) -> SQLiteConfigRepository:
    repo = SQLiteConfigRepository(db_path)
    repo.replace_from_configs(_rule_config("Old"), _categories())
    _insert_acme(db_path)
    return repo


def _service(db_path: Path):
    from ledger.rules.configuration import RuntimeConfigMutationService

    return RuntimeConfigMutationService(db_path)


def _state(db_path: Path) -> tuple[str, dict[str, tuple[str, bool]], list[tuple[str, str]]]:
    repo = SQLiteConfigRepository(db_path)
    rule_category = repo.load_merchant_rules().rules[0].category
    merchant_map = repo.load_categories().build_merchant_map()
    with session_scope(db_path) as session:
        rows = [
            (str(row.merchant), str(row.category))
            for row in session.query(TransactionDB).order_by(TransactionDB.txn_id).all()
        ]
    return rule_category, merchant_map, rows


def _failed_apply(source: str) -> RuleApplicationResult:
    return RuleApplicationResult(
        status="validation_failed",
        source=source,
        transactions_scanned=1,
        transactions_updated=1,
        changed_merchants=1,
        changed_categories=1,
        analytics_refreshed=False,
        validation_issues=["injected failure"],
        details={"validation_issues": ["injected failure"]},
    )


def test_move_replaces_derived_membership_and_updates_history(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    repo = _seed(db_path)

    result = _service(db_path).replace_and_apply(
        _rule_config("New"),
        repo.load_categories(),  # deliberately carries the stale ACME -> Old read-model row
        source="test_move",
        refresh_analytics=False,
    )

    assert result.ok
    memberships = [
        category.name
        for category in repo.load_categories().categories
        for merchant in category.merchants
        if merchant.strip().upper() == "ACME"
    ]
    assert memberships == ["New"]
    assert repo.validate().ok
    assert _state(db_path)[2] == [("ACME", "New")]


def test_backup_failure_leaves_database_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "ledger.db"
    repo = _seed(db_path)
    before = _state(db_path)

    import ledger.rules.configuration as configuration

    now = datetime.now(UTC).replace(tzinfo=None)
    monkeypatch.setattr(
        configuration,
        "backup_database",
        lambda *_args, **_kwargs: RunResult(
            status="failed",
            started_at=now,
            finished_at=now,
            errors=["injected backup failure"],
        ),
    )

    result = _service(db_path).replace_and_apply(
        _rule_config("New"),
        repo.load_categories(),
        source="test_backup_failure",
        refresh_analytics=False,
    )

    assert result.status == "backup_failed"
    assert not result.ok
    assert _state(db_path) == before


def test_restore_publishes_backup_as_private_file(tmp_path: Path) -> None:
    from ledger.storage.snapshots import snapshot_database

    db_path = tmp_path / "ledger.db"
    _seed(db_path)
    backup_path = tmp_path / "backup.db"
    snapshot_database(db_path, backup_path)
    backup_path.chmod(0o644)
    db_path.chmod(0o644)

    _service(db_path)._restore(backup_path)

    assert S_IMODE(db_path.stat().st_mode) == 0o600


def test_restore_replaces_committed_wal_state_with_snapshot(tmp_path: Path) -> None:
    from ledger.storage.snapshots import snapshot_database

    db_path = tmp_path / "ledger.db"
    _seed(db_path)
    before = _state(db_path)
    backup_path = tmp_path / "backup.db"
    snapshot_database(db_path, backup_path)

    keeper = sqlite3.connect(db_path)
    try:
        assert keeper.execute("pragma journal_mode=wal").fetchone()[0] == "wal"
        keeper.execute("pragma wal_autocheckpoint=0")
        keeper.execute("pragma wal_checkpoint(truncate)")
        keeper.execute("update merchant_rules set category = 'New' where merchant_norm = 'ACME'")
        keeper.execute("update transactions set category = 'New' where txn_id = 'acme-1'")
        keeper.commit()
        assert db_path.with_name(db_path.name + "-wal").exists()

        _service(db_path)._restore(backup_path)
    finally:
        keeper.close()

    assert _state(db_path) == before


def test_non_success_after_partial_apply_restores_full_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "ledger.db"
    repo = _seed(db_path)
    before = _state(db_path)

    def partially_apply(self, **_kwargs):
        with session_scope(self.db_path) as session:
            row = session.get(TransactionDB, "acme-1")
            row.category = "New"
        return _failed_apply("test_non_success")

    monkeypatch.setattr(RuleApplicationService, "apply_all", partially_apply)

    result = _service(db_path).replace_and_apply(
        _rule_config("New"),
        repo.load_categories(),
        source="test_non_success",
        refresh_analytics=False,
    )

    assert result.status == "validation_failed"
    assert _state(db_path) == before


def test_exception_after_partial_apply_restores_full_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "ledger.db"
    repo = _seed(db_path)
    before = _state(db_path)

    def partially_apply_then_raise(self, **_kwargs):
        with session_scope(self.db_path) as session:
            row = session.get(TransactionDB, "acme-1")
            row.category = "New"
        raise RuntimeError("injected apply exception")

    monkeypatch.setattr(RuleApplicationService, "apply_all", partially_apply_then_raise)

    with pytest.raises(RuntimeError, match="injected apply exception"):
        _service(db_path).replace_and_apply(
            _rule_config("New"),
            repo.load_categories(),
            source="test_exception",
            refresh_analytics=False,
        )

    assert _state(db_path) == before


def test_ui_save_routes_candidate_through_mutation_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ledger.ui.pages import rules_settings

    captured: dict[str, object] = {}

    class FakeMutationService:
        def __init__(self, db_path: Path):
            captured["db_path"] = db_path

        def replace_and_apply(self, merchants, categories, **kwargs):
            captured.update(
                merchants=merchants,
                categories=categories,
                source=kwargs["source"],
            )
            return RuleApplicationResult(
                status="success",
                source=str(kwargs["source"]),
                transactions_scanned=0,
                transactions_updated=0,
                changed_merchants=0,
                changed_categories=0,
                analytics_refreshed=False,
                validation_issues=[],
                details={},
            )

    fake_streamlit = SimpleNamespace(session_state={}, rerun=lambda: captured.update(reran=True))
    monkeypatch.setattr(
        rules_settings,
        "RuntimeConfigMutationService",
        FakeMutationService,
        raising=False,
    )
    monkeypatch.setattr(rules_settings, "st", fake_streamlit)
    monkeypatch.setattr(rules_settings, "_refresh_dashboard_caches", lambda: None)
    merchants = _rule_config("New")
    categories = _categories()

    rules_settings._save_apply_and_rerun(
        SimpleNamespace(db_path=tmp_path / "ledger.db"),
        merchants,
        categories,
        "streamlit_test",
    )

    assert captured["merchants"] == merchants
    assert captured["categories"] == categories
    assert captured["source"] == "streamlit_test"
    assert captured["reran"] is True


def test_taxonomy_editor_preserves_unruled_memberships() -> None:
    import pandas as pd

    from ledger.ui.pages.rules_settings import _categories_from_dataframe

    taxonomy = CategoriesConfig(
        version="test",
        default_category="Other",
        categories=[
            CategoryRule(name="Old", merchants=["SEED ONLY"]),
            CategoryRule(name="New"),
        ],
    )
    edited = pd.DataFrame(
        [
            {"Category": "Old", "Parent": "", "Exclude": False},
            {"Category": "New", "Parent": "", "Exclude": True},
        ]
    )

    candidate = _categories_from_dataframe(
        edited,
        taxonomy.version,
        taxonomy.default_category,
        existing=taxonomy,
    )

    assert candidate.build_merchant_map() == {"SEED ONLY": ("Old", False)}


def test_taxonomy_editor_moves_unruled_membership_on_category_rename() -> None:
    from ledger.ui.pages.rules_settings import (
        _categories_from_dataframe,
        _category_dataframe,
    )

    taxonomy = CategoriesConfig(
        version="test",
        default_category="Other",
        categories=[
            CategoryRule(name="Old", merchants=["SEED ONLY"]),
            CategoryRule(name="Other"),
        ],
    )
    edited = _category_dataframe(taxonomy)
    edited.loc[edited["Category"] == "Old", "Category"] = "Renamed"

    candidate = _categories_from_dataframe(
        edited,
        taxonomy.version,
        taxonomy.default_category,
        existing=taxonomy,
    )

    assert candidate.build_merchant_map() == {"SEED ONLY": ("Renamed", False)}
