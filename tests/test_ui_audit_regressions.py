from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest
from test_cash_planning import _add_transaction

from ledger.config.repository import seed_config_database
from ledger.query import LedgerQueryService
from ledger.storage.database import clear_engine_cache
from ledger.storage.migrations import ensure_schema, run_migrations
from ledger.ui.context import get_category_config, resolve_default_db
from ledger.ui.pages.overview import _cached_true_cost_kpis

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_overview_cache_refreshes_after_external_committed_write(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    ensure_schema(db)
    _add_transaction(
        db, txn_id="first", on=date(2026, 9, 4), amount="-100.00", cashflow_type="expense"
    )
    st.cache_data.clear()
    try:
        assert _cached_true_cost_kpis(str(db), date(2026, 9, 1), date(2026, 9, 4))[
            "expenses"
        ] == Decimal("100.00")
        _add_transaction(
            db, txn_id="second", on=date(2026, 9, 4), amount="-200.00", cashflow_type="expense"
        )
        assert _cached_true_cost_kpis(str(db), date(2026, 9, 1), date(2026, 9, 4))[
            "expenses"
        ] == Decimal("300.00")
    finally:
        st.cache_data.clear()


def test_overview_cache_refreshes_when_only_wal_changes(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    ensure_schema(db)
    _add_transaction(
        db, txn_id="first", on=date(2026, 9, 4), amount="-100.00", cashflow_type="expense"
    )
    clear_engine_cache()
    st.cache_data.clear()
    try:
        with closing(sqlite3.connect(db)) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            assert _cached_true_cost_kpis(str(db), date(2026, 9, 1), date(2026, 9, 4))[
                "expenses"
            ] == Decimal("100.00")
            before = db.stat().st_mtime_ns
            connection.execute("UPDATE transactions SET amount_signed = -300, amount_raw = -300")
            connection.commit()
            assert db.stat().st_mtime_ns == before
            assert _cached_true_cost_kpis(str(db), date(2026, 9, 1), date(2026, 9, 4))[
                "expenses"
            ] == Decimal("300.00")
    finally:
        st.cache_data.clear()


def test_overview_cache_refreshes_after_atomic_database_replacement(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    replacement = tmp_path / "candidate.db"
    for path, amount in ((db, "-100.00"), (replacement, "-300.00")):
        ensure_schema(path)
        _add_transaction(
            path, txn_id="first", on=date(2026, 9, 4), amount=amount, cashflow_type="expense"
        )
    st.cache_data.clear()
    try:
        assert _cached_true_cost_kpis(str(db), date(2026, 9, 1), date(2026, 9, 4))[
            "expenses"
        ] == Decimal("100.00")
        original_time = db.stat().st_mtime_ns
        os.utime(replacement, ns=(original_time, original_time))
        os.replace(replacement, db)
        assert _cached_true_cost_kpis(str(db), date(2026, 9, 1), date(2026, 9, 4))[
            "expenses"
        ] == Decimal("300.00")
    finally:
        st.cache_data.clear()


def test_category_configuration_cache_refreshes_after_external_update(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    ensure_schema(db)
    merchants = REPO_ROOT / "configs/merchants.toml"
    categories = REPO_ROOT / "configs/categories.toml"
    seed_config_database(db, merchants, categories)
    get_category_config.clear()
    before = get_category_config(db, merchants, categories)
    category = before.categories[0]
    try:
        with closing(sqlite3.connect(db)) as connection:
            connection.execute(
                "UPDATE category_config SET exclude_from_spending = ? WHERE name = ?",
                (int(not category.exclude_from_spending), category.name),
            )
            connection.commit()
        after = get_category_config(db, merchants, categories)
        assert after.categories[0].exclude_from_spending is not category.exclude_from_spending
    finally:
        get_category_config.clear()


def test_unchanged_startup_preserves_cached_overview_totals(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "ledger.db"
    ensure_schema(db)
    _add_transaction(
        db, txn_id="first", on=date(2026, 9, 4), amount="-100.00", cashflow_type="expense"
    )
    original = LedgerQueryService.compute_true_cost_kpis
    reads = []

    def count_reads(self, start_date, end_date):
        reads.append((start_date, end_date))
        return original(self, start_date, end_date)

    monkeypatch.setattr(LedgerQueryService, "compute_true_cost_kpis", count_reads)
    st.cache_data.clear()
    try:
        for _ in range(3):
            assert run_migrations(db).counts["applied"] == 0
            assert _cached_true_cost_kpis(str(db), date(2026, 9, 1), date(2026, 9, 4))[
                "expenses"
            ] == Decimal("100.00")
        assert len(reads) == 1
    finally:
        st.cache_data.clear()


def test_explicit_database_override_does_not_load_cli_defaults(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "ledger.db"
    monkeypatch.setenv("LEDGER_DB", str(db))

    def refuse_defaults():
        raise RuntimeError("Synthetic invalid CLI defaults must not affect this override")

    monkeypatch.setattr("ledger.config.loader.load_cli_defaults", refuse_defaults)
    assert resolve_default_db() == db.resolve()


def test_empty_database_opens_import_center(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "ledger.db"
    ensure_schema(db)
    seed_config_database(
        db, REPO_ROOT / "configs/merchants.toml", REPO_ROOT / "configs/categories.toml"
    )
    monkeypatch.setenv("LEDGER_DB", str(db))
    at = AppTest.from_file(str(REPO_ROOT / "streamlit_app.py"), default_timeout=25).run()
    assert not at.exception
    assert len(at.get("file_uploader")) == 1
    assert any(button.label == "Stage inbox PDFs" for button in at.button)


@pytest.mark.parametrize("previously_initialized", [False, True])
def test_missing_database_can_be_created_without_rebuild(
    tmp_path: Path, monkeypatch, previously_initialized: bool
) -> None:
    db = tmp_path / "new-ledger" / "ledger.db"
    if previously_initialized:
        db.parent.mkdir(mode=0o700)
        ensure_schema(db)
        clear_engine_cache()
        db.rename(db.with_name("previous.db"))
    monkeypatch.setenv("LEDGER_DB", str(db))
    at = AppTest.from_file(str(REPO_ROOT / "streamlit_app.py"), default_timeout=25).run()
    assert not at.exception
    assert not db.exists()
    create = next((button for button in at.button if button.label == "Create ledger"), None)
    assert create is not None
    create.click().run()
    assert not at.exception
    assert db.is_file()
    assert len(at.get("file_uploader")) == 1
