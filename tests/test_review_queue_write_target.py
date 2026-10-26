"""Unit G — the Review Queue must name the database it is about to write.

Two defects, both silent:

* ``get_review_source`` retargets the LIVE ``data/ledger.db`` whenever no import run
  is active, and Save then runs ``apply_review_decisions`` against real transactions.
  The page said only "Current DB"; nothing distinguished a staged dry run from an
  irreversible live write.
* The category dropdown was built from the LIVE config while the applier loads the
  REVIEW database's config. A category added during the review window was selectable,
  then fell through ``category_exc.get(name, False)`` at apply time — resolved, frozen
  behind 'manual' confidence, and silently counted as spending.
"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import SQLAlchemyError
from streamlit.testing.v1 import AppTest

from ledger.config.repository import SQLiteConfigRepository
from ledger.config.schemas import (
    CategoriesConfig,
    CategoryRule,
    MerchantRule,
    MerchantRulesConfig,
)
from ledger.import_review import get_review_rows, save_review_rows, seed_review_candidates
from ledger.imports.pdf.files import finalize_run_lock, write_run_state
from ledger.storage.database import clear_engine_cache, init_database
from ledger.ui.context import get_review_source, load_review_category_config
from ledger.ui.pages.review_queue import (
    REVIEW_SAVE_CONFIRM,
    REVIEW_UPDATE_UNAVAILABLE,
    _category_options,
    _confirm_phrase,
    _review_source_digest,
    _write_target_notice,
    _writes_live_db,
)


def _seed_config(db_path: Path, category_names: list[str]) -> None:
    init_database(db_path)
    db_path.parent.chmod(0o700)
    db_path.chmod(0o600)
    SQLiteConfigRepository(db_path).replace_from_configs(
        MerchantRulesConfig(
            version="test",
            rules=[
                MerchantRule(
                    merchant_norm="SYNTHETIC ACORN",
                    category=category_names[0],
                    patterns=[r"ACORN"],
                )
            ],
        ),
        CategoriesConfig(
            version="test",
            default_category="Other",
            categories=[CategoryRule(name=name, merchants=[]) for name in category_names],
        ),
    )


def _seed_review_row(db_path: Path) -> None:
    seed_review_candidates(
        db_path,
        [
            {
                "key": "SYNTHETIC ACORN CAFE",
                "merchant": "",
                "current_category": "Other",
                "txn_ids": ["txn_1"],
                "examples": [{"description": "SYNTHETIC ACORN CAFE", "posting_date": "2025-04-10"}],
                "count": 1,
                "total_abs_amount": Decimal("18.00"),
            }
        ],
    )


def _seed_named_review_row(db_path: Path, *, key: str, txn_id: str) -> None:
    seed_review_candidates(
        db_path,
        [
            {
                "key": key,
                "merchant": "",
                "current_category": "Other",
                "txn_ids": [txn_id],
                "examples": [{"description": key, "posting_date": "2025-04-10"}],
                "count": 1,
                "total_abs_amount": Decimal("18.00"),
            }
        ],
    )


def _insert_review_transaction(db_path: Path, txn_id: str, merchant_raw: str) -> None:
    now = datetime.now(UTC).replace(microsecond=0).isoformat()
    with closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute(
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
                "2025-04-10",
                "synthetic",
                "checking",
                "Chequing",
                "expense",
                "expense",
                1,
                "CAD",
                "18.00",
                "-18.00",
                merchant_raw,
                "",
                "Other",
                "low",
                "low",
                0,
                merchant_raw,
                "",
                "statement.csv",
                "2025-04/statement.csv",
                "2025-04",
                1,
                now,
                "test",
                f"fingerprint-{txn_id}",
                0,
                10,
                15,
                0,
                0,
                0,
            ),
        )


def _review_and_transaction_snapshot(db_path: Path) -> tuple[list[tuple], list[tuple]]:
    with closing(sqlite3.connect(db_path)) as connection:
        reviews = connection.execute(
            """
            select review_key, status, merchant, category, note, scope, txn_ids, examples,
                   transaction_count, total_abs_amount, updated_at
            from import_review order by review_key
            """
        ).fetchall()
        transactions = connection.execute(
            """
            select txn_id, merchant, category, merchant_confidence, category_confidence,
                   exclude_from_spending
            from transactions order by txn_id
            """
        ).fetchall()
    return reviews, transactions


def _direct_review_context(
    live_db: Path,
    review_db: Path,
    *,
    run_dir: Path,
    runs_dir: Path,
) -> SimpleNamespace:
    return SimpleNamespace(
        review_db=review_db,
        review_source={
            "is_live_db": False,
            "status": "needs_review",
            "run_dir": run_dir,
            "runs_dir": runs_dir,
        },
    )


def _seed_complete_review_row(db_path: Path) -> None:
    _seed_review_row(db_path)
    [row] = get_review_rows(db_path)
    row.update({"status": "pending", "merchant": "SYNTHETIC ACORN", "category": "Dining"})
    save_review_rows(db_path, [row])


def _stage_active_run(root: Path, staging_db: Path) -> None:
    """Write the on-disk shape ``get_active_import_run`` reads, rooted at *root*."""
    runs_dir = root / "import-runs"
    run_dir = runs_dir / "2026-08-09T000000Z"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(
        json.dumps({"status": "staged", "staging_db_path": str(staging_db)}),
        encoding="utf-8",
    )
    (runs_dir / "latest-run.txt").write_text(str(run_dir), encoding="utf-8")


def _coordinated_run(
    root: Path,
    *,
    target_db: Path,
    staging_db: Path,
    status: str = "needs_review",
) -> tuple[Path, Path]:
    runs_dir = root / "custom-runs"
    run_dir = runs_dir / "run-1"
    runs_dir.mkdir(parents=True, exist_ok=True)
    runs_dir.chmod(0o700)
    run_dir.mkdir(parents=True, exist_ok=True)
    write_run_state(
        run_dir,
        {
            "version": 1,
            "status": status,
            "target_db_path": str(target_db),
            "staging_db_path": str(staging_db),
        },
    )
    return runs_dir, run_dir


# ===========================================================================
# 1. The review source must say, explicitly, that it is the live database
# ===========================================================================


def test_review_source_flags_the_live_database_explicitly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    db_path = tmp_path / "ledger.db"
    _seed_config(db_path, ["Dining"])

    source = get_review_source(db_path)

    assert source["is_live_db"] is True, (
        "with no active import run the Review Queue writes the live database; "
        "that has to be an explicit flag, not something inferred from a label"
    )
    assert source["db_path"] == db_path


def test_review_source_flags_a_staged_run_as_not_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    db_path = tmp_path / "ledger.db"
    staging_db = tmp_path / "staging.db"
    _seed_config(db_path, ["Dining"])
    _seed_config(staging_db, ["Dining"])
    _stage_active_run(tmp_path, staging_db)

    source = get_review_source(db_path)

    assert source["is_live_db"] is False
    assert source["db_path"] == staging_db
    assert source["runs_dir"] == db_path.parent / "import-runs"


# ===========================================================================
# 2. The page turns that flag into something the user actually sees
# ===========================================================================


def test_writes_live_db_falls_back_to_the_status_sentinel():
    """A hand-built context (embedders, older callers) without the explicit flag must
    still resolve, and must resolve to the SAFE reading — a 'live_db' status is live."""
    assert _writes_live_db({"status": "live_db"}) is True
    assert _writes_live_db({"status": "staged"}) is False
    assert _writes_live_db({"status": "staged", "is_live_db": True}) is True
    # Unidentifiable source: resolve toward LIVE. A spurious warning is noise; a
    # missed one is a silent write to real transactions.
    assert _writes_live_db({}) is True


def test_write_target_notice_warns_and_names_the_live_database(tmp_path: Path):
    db_path = tmp_path / "ledger.db"

    severity, message = _write_target_notice(
        {"label": "ledger.db", "status": "live_db", "is_live_db": True}, db_path
    )

    assert severity == "warning"
    assert str(db_path) in message, "the user must see WHICH database is about to change"
    assert "live" in message.casefold()


def test_write_target_notice_is_informational_for_a_staged_run(tmp_path: Path):
    staging_db = tmp_path / "staging.db"

    severity, message = _write_target_notice(
        {"label": "2026-08-09T000000Z", "status": "staged", "is_live_db": False}, staging_db
    )

    assert severity == "info"
    assert str(staging_db) in message
    assert "2026-08-09T000000Z" in message


def test_live_writes_require_a_distinct_confirmation_phrase():
    """The confirmation the user types has to differ, so a live write cannot be
    performed with the muscle memory of a staged one."""
    staged = _confirm_phrase(REVIEW_SAVE_CONFIRM, writes_live_db=False)
    live = _confirm_phrase(REVIEW_SAVE_CONFIRM, writes_live_db=True)

    assert staged == REVIEW_SAVE_CONFIRM
    assert live != staged
    assert "live" in live


def test_review_queue_page_warns_before_a_live_write(tmp_path: Path):
    db_path = tmp_path / "ledger.db"
    _seed_config(db_path, ["Dining", "Groceries"])
    _seed_review_row(db_path)

    at = AppTest.from_function(
        _review_page_script,
        default_timeout=60,
        args=(str(db_path), str(db_path), True, "ledger.db"),
    )
    at.run()

    assert not at.exception, at.exception
    warnings = [w.value for w in at.warning]
    assert any(str(db_path) in text for text in warnings), (
        f"no visible warning named the live write target; warnings={warnings}"
    )


def test_review_queue_page_does_not_warn_for_a_staged_run(tmp_path: Path):
    db_path = tmp_path / "ledger.db"
    staging_db = tmp_path / "staging.db"
    _seed_config(db_path, ["Dining", "Groceries"])
    _seed_config(staging_db, ["Dining", "Groceries"])
    _seed_review_row(staging_db)

    at = AppTest.from_function(
        _review_page_script,
        default_timeout=60,
        args=(str(db_path), str(staging_db), False, "2026-08-09T000000Z"),
    )
    at.run()

    assert not at.exception, at.exception
    assert [w.value for w in at.warning] == []
    assert any(str(staging_db) in i.value for i in at.info)


@pytest.mark.parametrize(
    ("action", "confirm_label", "confirmation", "button_label"),
    [
        (
            "bulk_resolve",
            "Confirm bulk resolve",
            "resolve visible review rows",
            "Resolve complete visible rows",
        ),
        (
            "bulk_as_is",
            "Confirm bulk reviewed as-is",
            "review visible rows as-is",
            "Review complete visible rows as-is",
        ),
        (
            "save",
            "Confirm review save",
            "save review decisions",
            "Save Review Decisions",
        ),
    ],
)
def test_all_staged_review_actions_refuse_while_finalize_holds_the_run_lock(
    tmp_path: Path,
    action: str,
    confirm_label: str,
    confirmation: str,
    button_label: str,
) -> None:
    live_db = tmp_path / "ledger.db"
    staging_db = tmp_path / "custom-runs" / "run-1" / "staging.db"
    staging_db.parent.mkdir(parents=True)
    _seed_config(live_db, ["Dining", "Groceries"])
    _seed_config(staging_db, ["Dining", "Groceries"])
    _seed_complete_review_row(staging_db)
    _insert_review_transaction(staging_db, "txn_1", "SYNTHETIC ACORN CAFE")
    runs_dir, run_dir = _coordinated_run(
        tmp_path,
        target_db=live_db,
        staging_db=staging_db,
    )
    before = _review_and_transaction_snapshot(staging_db)

    with finalize_run_lock(runs_dir, run_dir):
        at = AppTest.from_function(
            _review_action_script,
            default_timeout=60,
            args=(
                str(live_db),
                str(staging_db),
                False,
                "run-1",
                str(run_dir),
                str(runs_dir),
                action,
            ),
        ).run()
        assert not at.exception, at.exception
        next(item for item in at.text_input if item.label == confirm_label).input(confirmation)
        at.run()
        button = next(item for item in at.button if item.label == button_label)
        assert button.disabled is False
        button.click()
        at.run()

    assert not at.exception, at.exception
    assert _review_and_transaction_snapshot(staging_db) == before
    assert len(at.error) == 1
    assert "temporarily unavailable" in at.error[0].value.casefold()


def test_live_review_action_refuses_a_custom_runs_dir_import_without_partial_write(
    tmp_path: Path,
) -> None:
    live_db = tmp_path / "ledger.db"
    staging_db = tmp_path / "custom-runs" / "run-1" / "staging.db"
    staging_db.parent.mkdir(parents=True)
    _seed_config(live_db, ["Dining", "Groceries"])
    _seed_complete_review_row(live_db)
    _insert_review_transaction(live_db, "txn_1", "SYNTHETIC ACORN CAFE")
    _seed_config(staging_db, ["Dining", "Groceries"])
    _coordinated_run(tmp_path, target_db=live_db, staging_db=staging_db)
    before = _review_and_transaction_snapshot(live_db)

    at = AppTest.from_function(
        _review_action_script,
        default_timeout=60,
        args=(str(live_db), str(live_db), True, "ledger.db", "", "", "bulk_resolve"),
    ).run()
    assert not at.exception, at.exception
    next(item for item in at.text_input if item.label == "Confirm bulk resolve").input(
        "resolve visible review rows to live db"
    )
    at.run()
    button = next(item for item in at.button if item.label == "Resolve complete visible rows")
    assert button.disabled is False
    button.click()
    at.run()

    assert not at.exception, at.exception
    assert _review_and_transaction_snapshot(live_db) == before
    assert len(at.error) == 1
    assert "temporarily unavailable" in at.error[0].value.casefold()


def test_staged_review_action_succeeds_under_its_run_lock_and_exact_database(
    tmp_path: Path,
) -> None:
    live_db = tmp_path / "ledger.db"
    staging_db = tmp_path / "custom-runs" / "run-1" / "staging.db"
    staging_db.parent.mkdir(parents=True)
    _seed_config(live_db, ["Dining", "Groceries"])
    _seed_config(staging_db, ["Dining", "Groceries"])
    _seed_complete_review_row(staging_db)
    runs_dir, run_dir = _coordinated_run(
        tmp_path,
        target_db=live_db,
        staging_db=staging_db,
    )

    at = AppTest.from_function(
        _review_action_script,
        default_timeout=60,
        args=(
            str(live_db),
            str(staging_db),
            False,
            "run-1",
            str(run_dir),
            str(runs_dir),
            "bulk_resolve",
        ),
    ).run()
    assert not at.exception, at.exception
    next(item for item in at.text_input if item.label == "Confirm bulk resolve").input(
        "resolve visible review rows"
    )
    at.run()
    next(item for item in at.button if item.label == "Resolve complete visible rows").click()
    at.run()

    assert not at.exception, at.exception
    assert [row["status"] for row in get_review_rows(staging_db)] == ["resolved"], [
        error.value for error in at.error
    ]
    assert len(at.error) == 0


@pytest.mark.parametrize(
    ("action", "confirm_label", "confirmation", "button_label", "expected_status"),
    [
        (
            "bulk_resolve",
            "Confirm bulk resolve",
            "resolve visible review rows",
            "Resolve complete visible rows",
            "resolved",
        ),
        (
            "bulk_as_is",
            "Confirm bulk reviewed as-is",
            "review visible rows as-is",
            "Review complete visible rows as-is",
            "reviewed_as_is",
        ),
        (
            "save",
            "Confirm review save",
            "save review decisions",
            "Save Review Decisions",
            "pending",
        ),
    ],
)
def test_all_successful_review_actions_clear_cache_and_request_rerun(
    tmp_path: Path,
    action: str,
    confirm_label: str,
    confirmation: str,
    button_label: str,
    expected_status: str,
) -> None:
    live_db = tmp_path / "ledger.db"
    staging_db = tmp_path / "custom-runs" / "run-1" / "staging.db"
    staging_db.parent.mkdir(parents=True)
    _seed_config(live_db, ["Dining", "Groceries"])
    _seed_config(staging_db, ["Dining", "Groceries"])
    _seed_complete_review_row(staging_db)
    _insert_review_transaction(staging_db, "txn_1", "SYNTHETIC ACORN CAFE")
    runs_dir, run_dir = _coordinated_run(
        tmp_path,
        target_db=live_db,
        staging_db=staging_db,
    )

    at = AppTest.from_function(
        _review_action_script,
        default_timeout=60,
        args=(
            str(live_db),
            str(staging_db),
            False,
            "run-1",
            str(run_dir),
            str(runs_dir),
            action,
            True,
        ),
    ).run()
    next(item for item in at.text_input if item.label == confirm_label).input(confirmation)
    at.run()
    next(item for item in at.button if item.label == button_label).click()
    at.run()

    assert not at.exception, at.exception
    assert at.session_state["_review_success_events"] == ["cache_clear", "rerun"]
    [saved] = get_review_rows(staging_db)
    assert saved["status"] == expected_status
    assert saved["note"] == ("synthetic changed note" if action == "save" else "")


@pytest.mark.parametrize(
    ("action", "confirm_label", "confirmation", "button_label"),
    [
        (
            "bulk_resolve",
            "Confirm bulk resolve",
            "resolve visible review rows",
            "Resolve complete visible rows",
        ),
        (
            "bulk_as_is",
            "Confirm bulk reviewed as-is",
            "review visible rows as-is",
            "Review complete visible rows as-is",
        ),
        (
            "save",
            "Confirm review save",
            "save review decisions",
            "Save Review Decisions",
        ),
    ],
)
def test_each_review_action_uses_the_previous_completed_render_as_its_cas_baseline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    confirm_label: str,
    confirmation: str,
    button_label: str,
) -> None:
    """An unseen same-second group expansion must refuse every rendered action."""
    from ledger import import_review

    monkeypatch.setattr(import_review, "_now", lambda: "2026-08-11T12:00:00Z")
    live_db = tmp_path / "ledger.db"
    staging_db = tmp_path / "custom-runs" / "run-1" / "staging.db"
    staging_db.parent.mkdir(parents=True)
    _seed_config(live_db, ["Dining", "Groceries"])
    _seed_config(staging_db, ["Dining", "Groceries"])
    _seed_complete_review_row(staging_db)
    _insert_review_transaction(staging_db, "txn_1", "SYNTHETIC ACORN CAFE")
    _insert_review_transaction(staging_db, "txn_2", "SYNTHETIC ACORN CAFE SECOND")
    runs_dir, run_dir = _coordinated_run(
        tmp_path,
        target_db=live_db,
        staging_db=staging_db,
    )

    at = AppTest.from_function(
        _review_action_script,
        default_timeout=60,
        args=(
            str(live_db),
            str(staging_db),
            False,
            "run-1",
            str(run_dir),
            str(runs_dir),
            action,
        ),
    ).run()
    [rendered] = get_review_rows(staging_db)
    assert rendered["transaction_count"] == 1
    next(item for item in at.text_input if item.label == confirm_label).input(confirmation)
    at.run()
    button = next(item for item in at.button if item.label == button_label)
    assert button.disabled is False

    seed_review_candidates(
        staging_db,
        [
            {
                "key": "SYNTHETIC ACORN CAFE",
                "merchant": "",
                "current_category": "Other",
                "txn_ids": ["txn_1", "txn_2"],
                "examples": [
                    {"description": "SYNTHETIC ACORN CAFE", "posting_date": "2025-04-10"},
                    {
                        "description": "SYNTHETIC ACORN CAFE SECOND",
                        "posting_date": "2025-04-11",
                    },
                ],
                "count": 2,
                "total_abs_amount": Decimal("36.00"),
            }
        ],
    )
    [reseeded] = get_review_rows(staging_db)
    assert reseeded["updated_at"] == rendered["updated_at"]
    before = _review_and_transaction_snapshot(staging_db)

    button.click()
    at.run()

    assert not at.exception, at.exception
    assert [error.value for error in at.error] == [REVIEW_UPDATE_UNAVAILABLE]
    assert _review_and_transaction_snapshot(staging_db) == before


@pytest.mark.parametrize("rendered_baseline", [None, {}, [None]])
def test_missing_or_malformed_rendered_baseline_refuses_without_an_uncaught_type_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rendered_baseline: object,
) -> None:
    from ledger.ui.pages import review_queue

    live_db = tmp_path / "ledger.db"
    staging_db = tmp_path / "custom-runs" / "run-1" / "staging.db"
    staging_db.parent.mkdir(parents=True)
    _seed_config(live_db, ["Dining", "Groceries"])
    _seed_config(staging_db, ["Dining", "Groceries"])
    _seed_complete_review_row(staging_db)
    _insert_review_transaction(staging_db, "txn_1", "SYNTHETIC ACORN CAFE")
    runs_dir, run_dir = _coordinated_run(
        tmp_path,
        target_db=live_db,
        staging_db=staging_db,
    )
    edited = get_review_rows(staging_db)
    edited[0]["status"] = "resolved"
    before = _review_and_transaction_snapshot(staging_db)
    errors: list[str] = []
    monkeypatch.setattr(review_queue.st, "error", errors.append)

    updated = review_queue._save_and_apply_review(
        _direct_review_context(
            live_db,
            staging_db,
            run_dir=run_dir,
            runs_dir=runs_dir,
        ),
        rendered_baseline,  # type: ignore[arg-type]
        edited,
    )

    assert updated is None
    assert errors == [REVIEW_UPDATE_UNAVAILABLE]
    assert _review_and_transaction_snapshot(staging_db) == before


def test_review_action_refuses_when_the_active_render_baseline_belongs_to_another_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger import import_review

    monkeypatch.setattr(import_review, "_now", lambda: "2026-08-11T12:00:00Z")
    live_db = tmp_path / "ledger.db"
    first_staging_db = tmp_path / "custom-runs" / "run-1" / "staging.db"
    first_staging_db.parent.mkdir(parents=True)
    _seed_config(live_db, ["Dining", "Groceries"])
    _seed_config(first_staging_db, ["Dining", "Groceries"])
    _seed_complete_review_row(first_staging_db)
    _insert_review_transaction(first_staging_db, "txn_1", "SYNTHETIC ACORN CAFE")
    runs_dir, first_run_dir = _coordinated_run(
        tmp_path,
        target_db=live_db,
        staging_db=first_staging_db,
    )
    second_run_dir = runs_dir / "run-2"
    second_run_dir.mkdir(mode=0o700)
    second_staging_db = second_run_dir / "staging.db"
    _seed_config(second_staging_db, ["Dining", "Groceries"])
    _seed_complete_review_row(second_staging_db)
    _insert_review_transaction(second_staging_db, "txn_1", "SYNTHETIC ACORN CAFE")
    write_run_state(
        second_run_dir,
        {
            "version": 1,
            "status": "needs_review",
            "target_db_path": str(live_db),
            "staging_db_path": str(second_staging_db),
        },
    )
    first_before = _review_and_transaction_snapshot(first_staging_db)
    second_before = _review_and_transaction_snapshot(second_staging_db)
    assert first_before == second_before

    at = AppTest.from_function(
        _review_action_script,
        default_timeout=60,
        args=(
            str(live_db),
            str(first_staging_db),
            False,
            "run-1",
            str(first_run_dir),
            str(runs_dir),
            "bulk_resolve",
        ),
    ).run()
    next(item for item in at.text_input if item.label == "Confirm bulk resolve").input(
        "resolve visible review rows"
    )
    at.run()
    at.session_state["_review_test_run_dir"] = str(second_run_dir)
    at.session_state["_review_test_db"] = str(second_staging_db)
    at.session_state["_review_test_label"] = "run-2"

    next(item for item in at.button if item.label == "Resolve complete visible rows").click()
    at.run()

    assert not at.exception, at.exception
    assert [error.value for error in at.error] == [REVIEW_UPDATE_UNAVAILABLE]
    assert _review_and_transaction_snapshot(first_staging_db) == first_before
    assert _review_and_transaction_snapshot(second_staging_db) == second_before


@pytest.mark.parametrize(
    ("component", "replacement"),
    [
        ("is_live_db", True),
        ("review_db", "alternate.db"),
        ("run_dir", "run-2"),
        ("runs_dir", "alternate-runs"),
    ],
)
def test_review_source_digest_independently_binds_each_source_component(
    tmp_path: Path,
    component: str,
    replacement: object,
) -> None:
    base = SimpleNamespace(
        review_db=tmp_path / "runs" / "run-1" / "staging.db",
        review_source={
            "is_live_db": False,
            "run_dir": tmp_path / "runs" / "run-1",
            "runs_dir": tmp_path / "runs",
        },
    )
    changed = copy.deepcopy(base)
    if component == "review_db":
        changed.review_db = tmp_path / str(replacement)
    elif component in {"run_dir", "runs_dir"}:
        changed.review_source[component] = tmp_path / str(replacement)
    else:
        changed.review_source[component] = replacement

    assert _review_source_digest(changed) != _review_source_digest(base)


def test_bulk_resolve_conflict_advances_the_completed_render_baseline_for_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deliberate second click uses the evidence shown by the refusal render."""
    from ledger import import_review

    monkeypatch.setattr(import_review, "_now", lambda: "2026-08-11T12:00:00Z")
    live_db = tmp_path / "ledger.db"
    staging_db = tmp_path / "custom-runs" / "run-1" / "staging.db"
    staging_db.parent.mkdir(parents=True)
    _seed_config(live_db, ["Dining", "Groceries"])
    _seed_config(staging_db, ["Dining", "Groceries"])
    _seed_complete_review_row(staging_db)
    _insert_review_transaction(staging_db, "txn_1", "SYNTHETIC ACORN CAFE")
    _insert_review_transaction(staging_db, "txn_2", "SYNTHETIC ACORN CAFE SECOND")
    runs_dir, run_dir = _coordinated_run(
        tmp_path,
        target_db=live_db,
        staging_db=staging_db,
    )

    at = AppTest.from_function(
        _review_action_script,
        default_timeout=60,
        args=(
            str(live_db),
            str(staging_db),
            False,
            "run-1",
            str(run_dir),
            str(runs_dir),
            "bulk_resolve",
        ),
    ).run()
    next(item for item in at.text_input if item.label == "Confirm bulk resolve").input(
        "resolve visible review rows"
    )
    at.run()
    seed_review_candidates(
        staging_db,
        [
            {
                "key": "SYNTHETIC ACORN CAFE",
                "merchant": "",
                "current_category": "Other",
                "txn_ids": ["txn_1", "txn_2"],
                "examples": [
                    {"description": "SYNTHETIC ACORN CAFE", "posting_date": "2025-04-10"},
                    {
                        "description": "SYNTHETIC ACORN CAFE SECOND",
                        "posting_date": "2025-04-11",
                    },
                ],
                "count": 2,
                "total_abs_amount": Decimal("36.00"),
            }
        ],
    )
    before = _review_and_transaction_snapshot(staging_db)

    next(item for item in at.button if item.label == "Resolve complete visible rows").click()
    at.run()

    assert [error.value for error in at.error] == [REVIEW_UPDATE_UNAVAILABLE]
    assert _review_and_transaction_snapshot(staging_db) == before
    retry = next(item for item in at.button if item.label == "Resolve complete visible rows")
    assert retry.disabled is False

    retry.click()
    at.run()

    assert not at.exception, at.exception
    assert [error.value for error in at.error] == []
    [review] = get_review_rows(staging_db)
    assert review["status"] == "resolved"
    assert review["transaction_count"] == 2
    _, transactions = _review_and_transaction_snapshot(staging_db)
    assert transactions == [
        ("txn_1", "SYNTHETIC ACORN", "Dining", "manual", "manual", 0),
        ("txn_2", "SYNTHETIC ACORN", "Dining", "manual", "manual", 0),
    ]


def test_staged_review_action_updates_only_changed_rows_and_preserves_a_concurrent_other_row(
    tmp_path: Path,
) -> None:
    from ledger.ui.pages.review_queue import _save_and_apply_review

    live_db = tmp_path / "ledger.db"
    staging_db = tmp_path / "custom-runs" / "run-1" / "staging.db"
    staging_db.parent.mkdir(parents=True)
    _seed_config(live_db, ["Dining", "Groceries"])
    _seed_config(staging_db, ["Dining", "Groceries"])
    _seed_named_review_row(staging_db, key="ALPHA", txn_id="txn-alpha")
    _seed_named_review_row(staging_db, key="BETA", txn_id="txn-beta")
    _insert_review_transaction(staging_db, "txn-alpha", "ALPHA")
    _insert_review_transaction(staging_db, "txn-beta", "BETA")
    runs_dir, run_dir = _coordinated_run(
        tmp_path,
        target_db=live_db,
        staging_db=staging_db,
    )
    baseline = get_review_rows(staging_db)
    edited = copy.deepcopy(baseline)
    alpha = next(row for row in edited if row["review_key"] == "ALPHA")
    alpha.update({"status": "resolved", "merchant": "ALPHA", "category": "Dining"})
    concurrent = next(row for row in get_review_rows(staging_db) if row["review_key"] == "BETA")
    concurrent.update({"status": "reviewed_as_is", "note": "independent concurrent decision"})
    save_review_rows(staging_db, [concurrent])

    updated = _save_and_apply_review(
        _direct_review_context(
            live_db,
            staging_db,
            run_dir=run_dir,
            runs_dir=runs_dir,
        ),
        baseline,
        edited,
    )

    assert updated == 1
    rows = {row["review_key"]: row for row in get_review_rows(staging_db)}
    assert rows["ALPHA"]["status"] == "resolved"
    assert rows["BETA"]["status"] == "reviewed_as_is"
    assert rows["BETA"]["note"] == "independent concurrent decision"


def test_staged_review_save_with_no_edits_is_a_successful_no_op(
    tmp_path: Path,
) -> None:
    from ledger.ui.pages.review_queue import _save_and_apply_review

    live_db = tmp_path / "ledger.db"
    staging_db = tmp_path / "custom-runs" / "run-1" / "staging.db"
    staging_db.parent.mkdir(parents=True)
    _seed_config(live_db, ["Dining", "Groceries"])
    _seed_config(staging_db, ["Dining", "Groceries"])
    _seed_named_review_row(staging_db, key="ALPHA", txn_id="txn-alpha")
    _insert_review_transaction(staging_db, "txn-alpha", "ALPHA")
    runs_dir, run_dir = _coordinated_run(
        tmp_path,
        target_db=live_db,
        staging_db=staging_db,
    )
    baseline = get_review_rows(staging_db)
    before = _review_and_transaction_snapshot(staging_db)

    updated = _save_and_apply_review(
        _direct_review_context(
            live_db,
            staging_db,
            run_dir=run_dir,
            runs_dir=runs_dir,
        ),
        baseline,
        copy.deepcopy(baseline),
    )

    assert updated == 0
    assert _review_and_transaction_snapshot(staging_db) == before


def test_staged_review_same_row_conflict_rolls_back_the_entire_action(
    tmp_path: Path,
) -> None:
    from ledger.ui.pages.review_queue import _save_and_apply_review

    live_db = tmp_path / "ledger.db"
    staging_db = tmp_path / "custom-runs" / "run-1" / "staging.db"
    staging_db.parent.mkdir(parents=True)
    _seed_config(live_db, ["Dining", "Groceries"])
    _seed_config(staging_db, ["Dining", "Groceries"])
    for key, txn_id in (("ALPHA", "txn-alpha"), ("BETA", "txn-beta")):
        _seed_named_review_row(staging_db, key=key, txn_id=txn_id)
        _insert_review_transaction(staging_db, txn_id, key)
    runs_dir, run_dir = _coordinated_run(
        tmp_path,
        target_db=live_db,
        staging_db=staging_db,
    )
    baseline = get_review_rows(staging_db)
    edited = copy.deepcopy(baseline)
    for row in edited:
        row.update({"status": "resolved", "merchant": row["review_key"], "category": "Dining"})
    concurrent = next(row for row in get_review_rows(staging_db) if row["review_key"] == "BETA")
    concurrent.update({"note": "newer same-row edit"})
    save_review_rows(staging_db, [concurrent])
    before = _review_and_transaction_snapshot(staging_db)

    updated = _save_and_apply_review(
        _direct_review_context(
            live_db,
            staging_db,
            run_dir=run_dir,
            runs_dir=runs_dir,
        ),
        baseline,
        edited,
    )

    assert updated is None
    assert _review_and_transaction_snapshot(staging_db) == before


def test_staged_review_missing_rendered_key_rolls_back_the_entire_action(
    tmp_path: Path,
) -> None:
    from ledger.ui.pages.review_queue import _save_and_apply_review

    live_db = tmp_path / "ledger.db"
    staging_db = tmp_path / "custom-runs" / "run-1" / "staging.db"
    staging_db.parent.mkdir(parents=True)
    _seed_config(live_db, ["Dining", "Groceries"])
    _seed_config(staging_db, ["Dining", "Groceries"])
    for key, txn_id in (("ALPHA", "txn-alpha"), ("BETA", "txn-beta")):
        _seed_named_review_row(staging_db, key=key, txn_id=txn_id)
        _insert_review_transaction(staging_db, txn_id, key)
    runs_dir, run_dir = _coordinated_run(
        tmp_path,
        target_db=live_db,
        staging_db=staging_db,
    )
    baseline = get_review_rows(staging_db)
    edited = [copy.deepcopy(next(row for row in baseline if row["review_key"] == "ALPHA"))]
    edited[0].update({"status": "resolved", "merchant": "ALPHA", "category": "Dining"})
    before = _review_and_transaction_snapshot(staging_db)

    updated = _save_and_apply_review(
        _direct_review_context(
            live_db,
            staging_db,
            run_dir=run_dir,
            runs_dir=runs_dir,
        ),
        baseline,
        edited,
    )

    assert updated is None
    assert _review_and_transaction_snapshot(staging_db) == before


def test_staged_review_updated_at_only_conflict_rolls_back_the_action(
    tmp_path: Path,
) -> None:
    from ledger.ui.pages.review_queue import _save_and_apply_review

    live_db = tmp_path / "ledger.db"
    staging_db = tmp_path / "custom-runs" / "run-1" / "staging.db"
    staging_db.parent.mkdir(parents=True)
    _seed_config(live_db, ["Dining", "Groceries"])
    _seed_config(staging_db, ["Dining", "Groceries"])
    _seed_named_review_row(staging_db, key="ALPHA", txn_id="txn-alpha")
    _insert_review_transaction(staging_db, "txn-alpha", "ALPHA")
    runs_dir, run_dir = _coordinated_run(
        tmp_path,
        target_db=live_db,
        staging_db=staging_db,
    )
    baseline = get_review_rows(staging_db)
    edited = copy.deepcopy(baseline)
    edited[0].update({"status": "resolved", "merchant": "ALPHA", "category": "Dining"})
    with closing(sqlite3.connect(staging_db)) as connection, connection:
        connection.execute(
            "update import_review set updated_at = ? where review_key = ?",
            ("2099-01-01T00:00:00Z", "ALPHA"),
        )
    before = _review_and_transaction_snapshot(staging_db)

    updated = _save_and_apply_review(
        _direct_review_context(
            live_db,
            staging_db,
            run_dir=run_dir,
            runs_dir=runs_dir,
        ),
        baseline,
        edited,
    )

    assert updated is None
    assert _review_and_transaction_snapshot(staging_db) == before


def test_staged_review_same_second_reseed_conflicts_without_losing_expanded_group(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A seed payload revision must conflict even when its timestamp cannot advance."""
    from ledger import import_review
    from ledger.ui.pages.review_queue import _save_and_apply_review

    monkeypatch.setattr(import_review, "_now", lambda: "2026-08-11T12:00:00Z")
    live_db = tmp_path / "ledger.db"
    staging_db = tmp_path / "custom-runs" / "run-1" / "staging.db"
    staging_db.parent.mkdir(parents=True)
    _seed_config(live_db, ["Dining", "Groceries"])
    _seed_config(staging_db, ["Dining", "Groceries"])
    _seed_named_review_row(staging_db, key="GROUP", txn_id="txn-1")
    _insert_review_transaction(staging_db, "txn-1", "GROUP")
    _insert_review_transaction(staging_db, "txn-2", "GROUP")
    runs_dir, run_dir = _coordinated_run(
        tmp_path,
        target_db=live_db,
        staging_db=staging_db,
    )
    baseline = get_review_rows(staging_db)
    edited = copy.deepcopy(baseline)
    edited[0].update({"status": "resolved", "merchant": "GROUP", "category": "Dining"})

    seed_review_candidates(
        staging_db,
        [
            {
                "key": "GROUP",
                "merchant": "",
                "current_category": "Other",
                "txn_ids": ["txn-1", "txn-2"],
                "examples": [
                    {"description": "GROUP ONE", "posting_date": "2025-04-10"},
                    {"description": "GROUP TWO", "posting_date": "2025-04-11"},
                ],
                "count": 2,
                "total_abs_amount": Decimal("36.00"),
            }
        ],
    )
    [reseeded] = get_review_rows(staging_db)
    assert reseeded["updated_at"] == baseline[0]["updated_at"]
    before = _review_and_transaction_snapshot(staging_db)

    updated = _save_and_apply_review(
        _direct_review_context(
            live_db,
            staging_db,
            run_dir=run_dir,
            runs_dir=runs_dir,
        ),
        baseline,
        edited,
    )

    assert updated is None
    assert _review_and_transaction_snapshot(staging_db) == before


@pytest.mark.parametrize(
    ("evidence_field", "replacement"),
    [
        ("txn_ids", '["txn-alpha", "txn-extra"]'),
        (
            "examples",
            '[{"description": "new evidence", "posting_date": "2025-04-10"}]',
        ),
        ("transaction_count", 2),
        ("total_abs_amount", "99.00"),
    ],
)
def test_each_stored_review_evidence_field_independently_participates_in_cas(
    tmp_path: Path,
    evidence_field: str,
    replacement: object,
) -> None:
    """Deleting any one evidence predicate must admit a stale row and fail this case."""
    from ledger.ui.pages.review_queue import _save_and_apply_review

    live_db = tmp_path / "ledger.db"
    staging_db = tmp_path / "custom-runs" / "run-1" / "staging.db"
    staging_db.parent.mkdir(parents=True)
    _seed_config(live_db, ["Dining", "Groceries"])
    _seed_config(staging_db, ["Dining", "Groceries"])
    _seed_named_review_row(staging_db, key="ALPHA", txn_id="txn-alpha")
    _insert_review_transaction(staging_db, "txn-alpha", "ALPHA")
    runs_dir, run_dir = _coordinated_run(
        tmp_path,
        target_db=live_db,
        staging_db=staging_db,
    )
    baseline = get_review_rows(staging_db)
    edited = copy.deepcopy(baseline)
    edited[0].update({"status": "resolved", "merchant": "ALPHA", "category": "Dining"})
    assert evidence_field in {
        "txn_ids",
        "examples",
        "transaction_count",
        "total_abs_amount",
    }
    with closing(sqlite3.connect(staging_db)) as connection, connection:
        connection.execute(
            f"update import_review set {evidence_field} = ? where review_key = ?",
            (replacement, "ALPHA"),
        )
    [current] = get_review_rows(staging_db)
    assert current["updated_at"] == baseline[0]["updated_at"]
    before = _review_and_transaction_snapshot(staging_db)

    updated = _save_and_apply_review(
        _direct_review_context(
            live_db,
            staging_db,
            run_dir=run_dir,
            runs_dir=runs_dir,
        ),
        baseline,
        edited,
    )

    assert updated is None
    assert _review_and_transaction_snapshot(staging_db) == before


def test_staged_review_apply_failure_rolls_back_cas_and_transactions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger import import_review
    from ledger.ui.pages.review_queue import _save_and_apply_review

    live_db = tmp_path / "ledger.db"
    staging_db = tmp_path / "custom-runs" / "run-1" / "staging.db"
    staging_db.parent.mkdir(parents=True)
    _seed_config(live_db, ["Dining", "Groceries"])
    _seed_config(staging_db, ["Dining", "Groceries"])
    _seed_named_review_row(staging_db, key="ALPHA", txn_id="txn-alpha")
    _insert_review_transaction(staging_db, "txn-alpha", "ALPHA")
    runs_dir, run_dir = _coordinated_run(
        tmp_path,
        target_db=live_db,
        staging_db=staging_db,
    )
    baseline = get_review_rows(staging_db)
    edited = copy.deepcopy(baseline)
    edited[0].update({"status": "resolved", "merchant": "ALPHA", "category": "Dining"})
    before = _review_and_transaction_snapshot(staging_db)

    def fail_after_cas(*_args: object, **_kwargs: object) -> int:
        raise sqlite3.OperationalError("synthetic apply failure")

    monkeypatch.setattr(
        import_review,
        "_apply_review_decisions_connection",
        fail_after_cas,
        raising=False,
    )

    updated = _save_and_apply_review(
        _direct_review_context(
            live_db,
            staging_db,
            run_dir=run_dir,
            runs_dir=runs_dir,
        ),
        baseline,
        edited,
    )

    assert updated is None
    assert _review_and_transaction_snapshot(staging_db) == before


def test_atomic_ui_apply_matches_the_existing_exclusion_behavior(
    tmp_path: Path,
) -> None:
    from ledger.import_review import apply_review_decisions, apply_review_mutations
    from ledger.storage.snapshots import (
        audited_database_write_connection,
        database_target_lock,
        snapshot_database,
    )
    from ledger.ui.pages.review_queue import _review_mutations
    from ledger.utils.private_files import pin_private_directory

    standard_db = tmp_path / "standard.db"
    atomic_db = tmp_path / "atomic.db"
    init_database(standard_db)
    SQLiteConfigRepository(standard_db).replace_from_configs(
        MerchantRulesConfig(
            version="parity",
            rules=[
                MerchantRule(
                    merchant_norm="SYNTHETIC ACORN",
                    category="Dining",
                    patterns=[r"ACORN"],
                    exclude_from_spending=True,
                )
            ],
        ),
        CategoriesConfig(
            version="parity",
            default_category="Other",
            categories=[
                CategoryRule(name="Dining", merchants=[]),
                CategoryRule(name="Other", merchants=[]),
            ],
        ),
    )
    _seed_named_review_row(standard_db, key="ACORN", txn_id="txn-acorn")
    _insert_review_transaction(standard_db, "txn-acorn", "ACORN RAW")
    snapshot_database(standard_db, atomic_db)
    baseline = get_review_rows(standard_db)
    edited = copy.deepcopy(baseline)
    edited[0].update({"status": "resolved", "merchant": "SYNTHETIC ACORN", "category": "Dining"})

    save_review_rows(standard_db, edited)
    standard_updated = apply_review_decisions(standard_db)
    with (
        database_target_lock(atomic_db),
        pin_private_directory(atomic_db.parent) as parent,
        parent.pin_private_file(atomic_db.name, mutable=True) as pinned,
        audited_database_write_connection(
            pinned,
            label="atomic parity",
        ) as connection,
    ):
        atomic_updated = apply_review_mutations(
            connection,
            atomic_db,
            _review_mutations(baseline, edited),
        )

    assert atomic_updated == standard_updated == 1
    standard_reviews, standard_transactions = _review_and_transaction_snapshot(standard_db)
    atomic_reviews, atomic_transactions = _review_and_transaction_snapshot(atomic_db)
    assert [row[:6] for row in atomic_reviews] == [row[:6] for row in standard_reviews]
    assert atomic_transactions == standard_transactions
    assert atomic_transactions[0][-1] == 1


@pytest.mark.parametrize("config_state", ["missing", "stale-category"])
def test_atomic_ui_apply_revalidates_authoritative_categories_on_its_write_connection(
    tmp_path: Path,
    config_state: str,
) -> None:
    from ledger.import_review import ReviewConflictError, apply_review_mutations
    from ledger.storage.snapshots import audited_database_write_connection, database_target_lock
    from ledger.ui.pages.review_queue import _review_mutations
    from ledger.utils.private_files import pin_private_directory

    review_db = tmp_path / "staging.db"
    if config_state == "missing":
        init_database(review_db)
    else:
        _seed_config(review_db, ["Dining", "Groceries"])
    _seed_named_review_row(review_db, key="ALPHA", txn_id="txn-alpha")
    _insert_review_transaction(review_db, "txn-alpha", "ALPHA RAW")
    baseline = get_review_rows(review_db)
    edited = copy.deepcopy(baseline)
    edited[0].update(
        {
            "status": "resolved",
            "merchant": "ALPHA",
            "category": "Dining" if config_state == "missing" else "Pet Care",
        }
    )
    before = _review_and_transaction_snapshot(review_db)

    with (
        pytest.raises(ReviewConflictError, match="category configuration"),
        database_target_lock(review_db),
        pin_private_directory(review_db.parent) as parent,
        parent.pin_private_file(review_db.name, mutable=True) as pinned,
        audited_database_write_connection(
            pinned,
            label="atomic config revalidation",
        ) as connection,
    ):
        apply_review_mutations(
            connection,
            review_db,
            _review_mutations(baseline, edited),
        )

    assert _review_and_transaction_snapshot(review_db) == before


def test_staged_review_write_refuses_a_connect_time_database_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.storage import snapshots
    from ledger.storage.snapshots import snapshot_database
    from ledger.ui.pages import review_queue
    from ledger.ui.pages.review_queue import _save_and_apply_review

    live_db = tmp_path / "ledger.db"
    staging_db = tmp_path / "custom-runs" / "run-1" / "staging.db"
    staging_db.parent.mkdir(parents=True)
    _seed_config(live_db, ["Dining", "Groceries"])
    _seed_config(staging_db, ["Dining", "Groceries"])
    _seed_named_review_row(staging_db, key="ALPHA", txn_id="txn-alpha")
    _insert_review_transaction(staging_db, "txn-alpha", "ALPHA")
    runs_dir, run_dir = _coordinated_run(
        tmp_path,
        target_db=live_db,
        staging_db=staging_db,
    )
    baseline = get_review_rows(staging_db)
    edited = copy.deepcopy(baseline)
    edited[0].update({"status": "resolved", "merchant": "ALPHA", "category": "Dining"})
    reviews_before, transactions_before = _review_and_transaction_snapshot(staging_db)
    alias_lock = staging_db.parent / ".ledger-sqlite-alias.lock"
    alias_lock_before = (
        (alias_lock.stat().st_dev, alias_lock.stat().st_ino, alias_lock.read_bytes())
        if alias_lock.exists()
        else None
    )
    decoy = run_dir / "decoy.db"
    snapshot_database(staging_db, decoy)
    displaced = run_dir / "real-staging.db"
    real_connect = snapshots.sqlite3.connect
    attacked = False
    errors: list[str] = []

    def swap_only_during_connect(database: object, *args: object, **kwargs: object):
        nonlocal attacked
        if not attacked and str(staging_db) in str(database):
            attacked = True
            staging_db.rename(displaced)
            decoy.rename(staging_db)
            try:
                return real_connect(database, *args, **kwargs)
            finally:
                staging_db.rename(decoy)
                displaced.rename(staging_db)
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(snapshots.sqlite3, "connect", swap_only_during_connect)
    monkeypatch.setattr(review_queue.st, "error", lambda message: errors.append(str(message)))

    updated = _save_and_apply_review(
        _direct_review_context(
            live_db,
            staging_db,
            run_dir=run_dir,
            runs_dir=runs_dir,
        ),
        baseline,
        edited,
    )

    assert attacked
    assert updated is None
    assert errors == [review_queue.REVIEW_UPDATE_UNAVAILABLE]
    assert str(tmp_path) not in errors[0]
    reviews_after, transactions_after = _review_and_transaction_snapshot(staging_db)
    assert reviews_after == reviews_before
    assert transactions_after == transactions_before
    assert not displaced.exists()
    assert not staging_db.with_name(f"{staging_db.name}-journal").exists()
    alias_lock_after = (
        (alias_lock.stat().st_dev, alias_lock.stat().st_ino, alias_lock.read_bytes())
        if alias_lock.exists()
        else None
    )
    assert alias_lock_after == alias_lock_before
    assert list(staging_db.parent.glob(".ledger-sqlite-alias-*")) == []


def test_staged_review_passes_the_exact_audited_connection_to_atomic_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import contextmanager

    from ledger.ui.pages import review_queue

    live_db = tmp_path / "ledger.db"
    staging_db = tmp_path / "custom-runs" / "run-1" / "staging.db"
    staging_db.parent.mkdir(parents=True)
    _seed_config(live_db, ["Dining", "Groceries"])
    _seed_config(staging_db, ["Dining", "Groceries"])
    _seed_named_review_row(staging_db, key="ALPHA", txn_id="txn-alpha")
    _insert_review_transaction(staging_db, "txn-alpha", "ALPHA")
    runs_dir, run_dir = _coordinated_run(
        tmp_path,
        target_db=live_db,
        staging_db=staging_db,
    )
    baseline = get_review_rows(staging_db)
    edited = copy.deepcopy(baseline)
    edited[0].update({"status": "resolved", "merchant": "ALPHA", "category": "Dining"})
    exact_connection = object()

    @contextmanager
    def yield_exact_connection(*_args: object, **_kwargs: object):
        yield exact_connection

    def require_exact_connection(
        connection: object,
        _db_path: Path,
        mutations: list[dict],
    ) -> int:
        assert connection is exact_connection
        assert len(mutations) == 1
        return 1

    monkeypatch.setattr(
        review_queue,
        "audited_database_write_connection",
        yield_exact_connection,
    )
    monkeypatch.setattr(
        review_queue,
        "apply_review_mutations",
        require_exact_connection,
    )

    assert (
        review_queue._save_and_apply_review(
            _direct_review_context(
                live_db,
                staging_db,
                run_dir=run_dir,
                runs_dir=runs_dir,
            ),
            baseline,
            edited,
        )
        == 1
    )


@pytest.mark.parametrize(
    "failure",
    [
        sqlite3.OperationalError("database is locked"),
        SQLAlchemyError("synthetic database failure"),
    ],
)
def test_staged_review_database_errors_are_content_free_and_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> None:
    from ledger import import_review
    from ledger.ui.pages import review_queue

    live_db = tmp_path / "ledger.db"
    staging_db = tmp_path / "custom-runs" / "run-1" / "staging.db"
    staging_db.parent.mkdir(parents=True)
    _seed_config(live_db, ["Dining", "Groceries"])
    _seed_config(staging_db, ["Dining", "Groceries"])
    _seed_named_review_row(staging_db, key="ALPHA", txn_id="txn-alpha")
    _insert_review_transaction(staging_db, "txn-alpha", "ALPHA")
    runs_dir, run_dir = _coordinated_run(
        tmp_path,
        target_db=live_db,
        staging_db=staging_db,
    )
    baseline = get_review_rows(staging_db)
    edited = copy.deepcopy(baseline)
    edited[0].update({"status": "resolved", "merchant": "ALPHA", "category": "Dining"})
    before = _review_and_transaction_snapshot(staging_db)
    errors: list[str] = []

    def fail_apply(*_args: object, **_kwargs: object) -> int:
        raise failure

    monkeypatch.setattr(
        import_review,
        "_apply_review_decisions_connection",
        fail_apply,
        raising=False,
    )
    monkeypatch.setattr(review_queue.st, "error", lambda message: errors.append(str(message)))

    updated = review_queue._save_and_apply_review(
        _direct_review_context(
            live_db,
            staging_db,
            run_dir=run_dir,
            runs_dir=runs_dir,
        ),
        baseline,
        edited,
    )

    assert updated is None
    assert errors == [review_queue.REVIEW_UPDATE_UNAVAILABLE]
    assert str(tmp_path) not in errors[0]
    assert _review_and_transaction_snapshot(staging_db) == before


def test_staged_review_reports_a_committed_validation_failure_truthfully(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger.storage.snapshots import DatabaseWriteCommittedError
    from ledger.ui.pages import review_queue

    live_db = tmp_path / "ledger.db"
    staging_db = tmp_path / "custom-runs" / "run-1" / "staging.db"
    staging_db.parent.mkdir(parents=True)
    _seed_config(live_db, ["Dining", "Groceries"])
    _seed_config(staging_db, ["Dining", "Groceries"])
    _seed_named_review_row(staging_db, key="ALPHA", txn_id="txn-alpha")
    _insert_review_transaction(staging_db, "txn-alpha", "ALPHA")
    runs_dir, run_dir = _coordinated_run(
        tmp_path,
        target_db=live_db,
        staging_db=staging_db,
    )
    baseline = get_review_rows(staging_db)
    edited = copy.deepcopy(baseline)
    edited[0].update({"status": "resolved", "merchant": "ALPHA", "category": "Dining"})
    errors: list[str] = []

    @contextmanager
    def committed_writer(*_args: object, **_kwargs: object):
        raise DatabaseWriteCommittedError("/private/synthetic/operator/path")
        yield  # pragma: no cover

    monkeypatch.setattr(review_queue, "audited_database_write_connection", committed_writer)
    monkeypatch.setattr(review_queue.st, "error", lambda message: errors.append(str(message)))

    updated = review_queue._save_and_apply_review(
        _direct_review_context(
            live_db,
            staging_db,
            run_dir=run_dir,
            runs_dir=runs_dir,
        ),
        baseline,
        edited,
    )

    assert updated is None
    assert errors == [review_queue.REVIEW_UPDATE_COMMITTED_WARNING]
    assert "/private" not in errors[0]


@pytest.mark.parametrize("is_live", [False, True])
@pytest.mark.parametrize("failing_boundary", ["pin", "lock"])
def test_review_reports_commit_when_outer_boundary_exit_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    is_live: bool,
    failing_boundary: str,
) -> None:
    """Review commits remain truthful through every surrounding boundary exit."""
    from ledger.ui.pages import review_queue

    live_db = tmp_path / "ledger.db"
    staging_db = tmp_path / "custom-runs" / "run-1" / "staging.db"
    staging_db.parent.mkdir(parents=True)
    _seed_config(live_db, ["Dining", "Groceries"])
    if is_live:
        review_db = live_db
        ctx = SimpleNamespace(review_db=review_db, review_source={"is_live_db": True})
    else:
        review_db = staging_db
        _seed_config(staging_db, ["Dining", "Groceries"])
        runs_dir, run_dir = _coordinated_run(
            tmp_path,
            target_db=live_db,
            staging_db=staging_db,
        )
        ctx = _direct_review_context(
            live_db,
            staging_db,
            run_dir=run_dir,
            runs_dir=runs_dir,
        )
    _seed_named_review_row(review_db, key="ALPHA", txn_id="txn-alpha")
    _insert_review_transaction(review_db, "txn-alpha", "ALPHA")
    baseline = get_review_rows(review_db)
    edited = copy.deepcopy(baseline)
    edited[0].update({"status": "resolved", "merchant": "ALPHA", "category": "Dining"})
    errors: list[str] = []

    if failing_boundary == "pin":
        boundary_name = (
            "_validated_live_review_source" if is_live else "_validated_staged_review_source"
        )
        original_boundary = getattr(review_queue, boundary_name)

        @contextmanager
        def fail_after_pin_exit(*args: object, **kwargs: object):
            with original_boundary(*args, **kwargs) as pinned:
                yield pinned
            raise OSError("synthetic post-commit review pin cleanup failure")

    else:
        boundary_name = "live_mutation_boundary" if is_live else "finalize_run_lock"
        original_boundary = getattr(review_queue, boundary_name)

        @contextmanager
        def fail_after_pin_exit(*args: object, **kwargs: object):
            with original_boundary(*args, **kwargs) as lock_value:
                yield lock_value
            raise OSError("synthetic post-commit review lock cleanup failure")

    monkeypatch.setattr(review_queue, boundary_name, fail_after_pin_exit)
    monkeypatch.setattr(review_queue.st, "error", lambda message: errors.append(str(message)))

    updated = review_queue._save_and_apply_review(ctx, baseline, edited)

    assert updated is None
    assert errors == [review_queue.REVIEW_UPDATE_COMMITTED_WARNING]
    [review] = get_review_rows(review_db)
    assert review["status"] == "resolved"
    assert review["merchant"] == "ALPHA"
    assert review["category"] == "Dining"
    _, transactions = _review_and_transaction_snapshot(review_db)
    assert transactions == [("txn-alpha", "ALPHA", "Dining", "manual", "manual", 0)]


# ===========================================================================
# 3. Dropdown and applier must read the same config
# ===========================================================================


def test_load_review_category_config_reads_the_review_database(tmp_path: Path):
    live_db = tmp_path / "ledger.db"
    staging_db = tmp_path / "staging.db"
    _seed_config(live_db, ["Dining", "Groceries", "Pet Care"])
    _seed_config(staging_db, ["Dining", "Groceries"])

    config = load_review_category_config(staging_db)

    assert config is not None
    assert sorted(cat.name for cat in config.categories) == ["Dining", "Groceries"]


def test_load_review_category_config_returns_none_without_runtime_config(tmp_path: Path):
    bare_db = tmp_path / "bare.db"
    init_database(bare_db)

    assert load_review_category_config(bare_db) is None


def test_review_render_reads_never_prepare_or_create_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ledger import import_review
    from ledger.config import repository as config_repository

    db_path = tmp_path / "ledger.db"
    _seed_config(db_path, ["Dining", "Groceries"])
    _seed_review_row(db_path)

    monkeypatch.setattr(
        import_review,
        "ensure_review_schema",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("review read prepared schema")
        ),
    )
    monkeypatch.setattr(
        config_repository.SQLiteConfigRepository,
        "initialize",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("runtime config read prepared schema")
        ),
    )

    assert len(get_review_rows(db_path)) == 1
    config = load_review_category_config(db_path)
    assert config is not None
    assert sorted(category.name for category in config.categories) == ["Dining", "Groceries"]


def test_review_render_reads_leave_a_missing_database_absent(tmp_path: Path) -> None:
    db_path = tmp_path / "missing.db"

    assert get_review_rows(db_path) == []
    assert load_review_category_config(db_path) is None
    assert not db_path.exists()


@pytest.mark.parametrize("wal_format", [False, True], ids=["rollback", "wal-header"])
@pytest.mark.parametrize("reader", ["review-rows", "category-config"])
def test_review_render_reads_leave_the_canonical_sqlite_namespace_unchanged(
    tmp_path: Path,
    wal_format: bool,
    reader: str,
) -> None:
    db_path = tmp_path / "ledger.db"
    _seed_config(db_path, ["Dining", "Groceries"])
    _seed_review_row(db_path)
    if wal_format:
        with closing(sqlite3.connect(db_path)) as connection:
            assert connection.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        Path(f"{db_path}-wal").unlink(missing_ok=True)
        Path(f"{db_path}-shm").unlink(missing_ok=True)
    clear_engine_cache()

    before_hash = hashlib.sha256(db_path.read_bytes()).hexdigest()
    before_mode = db_path.stat().st_mode
    before_sidecars = {
        suffix: (
            (path.stat().st_dev, path.stat().st_ino, hashlib.sha256(path.read_bytes()).hexdigest())
            if (path := Path(f"{db_path}{suffix}")).exists()
            else None
        )
        for suffix in ("-wal", "-shm", "-journal")
    }

    if reader == "review-rows":
        assert len(get_review_rows(db_path)) == 1
    else:
        config = load_review_category_config(db_path)
        assert config is not None
        assert sorted(category.name for category in config.categories) == [
            "Dining",
            "Groceries",
        ]
    assert hashlib.sha256(db_path.read_bytes()).hexdigest() == before_hash
    assert db_path.stat().st_mode == before_mode
    after_sidecars = {
        suffix: (
            (path.stat().st_dev, path.stat().st_ino, hashlib.sha256(path.read_bytes()).hexdigest())
            if (path := Path(f"{db_path}{suffix}")).exists()
            else None
        )
        for suffix in ("-wal", "-shm", "-journal")
    }
    assert after_sidecars == before_sidecars


def test_category_options_exclude_categories_only_the_live_config_knows(tmp_path: Path):
    """'Pet Care' was added to the live config during the review window. The applier
    loads the STAGING config, where it does not exist, so offering it in the dropdown
    resolves the row into a category that then evaluates exclude_from_spending=False
    by default and freezes behind 'manual' confidence."""
    live_db = tmp_path / "ledger.db"
    staging_db = tmp_path / "staging.db"
    _seed_config(live_db, ["Dining", "Groceries", "Pet Care"])
    _seed_config(staging_db, ["Dining", "Groceries"])

    options, warning = _category_options(_FakeCtx(live_db, staging_db))

    assert options == ["Dining", "Groceries"]
    assert "Pet Care" not in options
    assert warning is None


def test_category_options_fail_closed_without_disclosing_the_review_database_path(
    tmp_path: Path,
):
    live_db = tmp_path / "ledger.db"
    bare_db = tmp_path / "bare.db"
    _seed_config(live_db, ["Dining", "Groceries"])
    init_database(bare_db)

    options, warning = _category_options(_FakeCtx(live_db, bare_db))

    assert options == []
    assert warning is not None
    assert "disabled" in warning.casefold()
    assert str(bare_db) not in warning
    assert "live configuration" not in warning.casefold()


def test_review_page_disables_all_actions_when_authoritative_config_is_unavailable(
    tmp_path: Path,
) -> None:
    live_db = tmp_path / "ledger.db"
    review_db = tmp_path / "bare.db"
    _seed_config(live_db, ["Dining", "Groceries"])
    init_database(review_db)
    _seed_complete_review_row(review_db)

    at = AppTest.from_function(
        _review_page_script,
        default_timeout=60,
        args=(str(live_db), str(review_db), False, "bare"),
    ).run()

    assert not at.exception, at.exception
    buttons = {
        button.label: button
        for button in at.button
        if button.label
        in {
            "Resolve complete visible rows",
            "Review complete visible rows as-is",
            "Save Review Decisions",
        }
    }
    assert set(buttons) == {
        "Resolve complete visible rows",
        "Review complete visible rows as-is",
        "Save Review Decisions",
    }
    assert all(button.disabled for button in buttons.values())
    warnings = [warning.value for warning in at.warning]
    assert any("disabled" in warning.casefold() for warning in warnings)
    assert all(str(review_db) not in warning for warning in warnings)


class _FakeCtx:
    """Only the two attributes ``_category_options`` reads."""

    def __init__(self, live_db: Path, review_db: Path) -> None:
        self.review_db = review_db
        self.category_config = SQLiteConfigRepository(live_db).load_categories()


# ---------------------------------------------------------------------------
# AppTest script — source-extracted by AppTest.from_function, so it must be
# self-contained (no closures, all imports inline).
# ---------------------------------------------------------------------------


def _review_page_script(
    db_str: str, review_db_str: str, is_live_db: bool, label: str
) -> None:  # pragma: no cover - executed inside AppTest's script runner
    from datetime import date
    from pathlib import Path

    from ledger.config.repository import SQLiteConfigRepository
    from ledger.query import LedgerQueryService
    from ledger.ui.context import AppContext
    from ledger.ui.pages import review_queue

    db = Path(db_str)
    review_db = Path(review_db_str)
    service = LedgerQueryService(db)
    ctx = AppContext(
        db_path=db,
        service=service,
        category_config=SQLiteConfigRepository(db).load_categories(),
        stats=service.get_stats(),
        earliest=date(2025, 4, 1),
        latest=date(2025, 4, 30),
        start_date=date(2025, 4, 1),
        end_date=date(2025, 4, 30),
        period_label="April 2025",
        review_source={
            "label": label,
            "status": "live_db" if is_live_db else "staged",
            "db_path": review_db,
            "source_kind": "Current DB" if is_live_db else "Active Import",
            "is_live_db": is_live_db,
        },
        review_db=review_db,
        pending_reviews=1,
    )
    review_queue.render(ctx)


def _review_action_script(
    db_str: str,
    review_db_str: str,
    is_live_db: bool,
    label: str,
    run_dir_str: str,
    runs_dir_str: str,
    action: str,
    observe_success: bool = False,
) -> None:  # pragma: no cover - executed inside AppTest's script runner
    from datetime import date
    from pathlib import Path

    from ledger.config.repository import SQLiteConfigRepository
    from ledger.query import LedgerQueryService
    from ledger.ui.context import AppContext
    from ledger.ui.pages import review_queue

    db = Path(db_str)
    review_db = Path(review_queue.st.session_state.get("_review_test_db", review_db_str))
    source_label = str(review_queue.st.session_state.get("_review_test_label", label))
    service = LedgerQueryService(db)
    real_clear = review_queue.st.cache_data.clear
    real_rerun = review_queue.st.rerun
    if observe_success:

        def observed_clear():
            review_queue.st.session_state.setdefault("_review_success_events", []).append(
                "cache_clear"
            )
            real_clear()

        def observed_rerun():
            review_queue.st.session_state.setdefault("_review_success_events", []).append("rerun")

        review_queue.st.cache_data.clear = observed_clear
        review_queue.st.rerun = observed_rerun
    if action == "save":
        real_data_editor = review_queue.st.data_editor

        def edited_frame(data, *args, **kwargs):
            del args, kwargs
            edited = data.copy()
            edited.loc[:, "note"] = "synthetic changed note"
            return edited

        review_queue.st.data_editor = edited_frame
    ctx = AppContext(
        db_path=db,
        service=service,
        category_config=SQLiteConfigRepository(db).load_categories(),
        stats=service.get_stats(),
        earliest=date(2025, 4, 1),
        latest=date(2025, 4, 30),
        start_date=date(2025, 4, 1),
        end_date=date(2025, 4, 30),
        period_label="April 2025",
        review_source={
            "label": source_label,
            "status": "live_db" if is_live_db else "needs_review",
            "db_path": review_db,
            "source_kind": "Current DB" if is_live_db else "Active Import",
            "is_live_db": is_live_db,
            "run_dir": (
                Path(review_queue.st.session_state.get("_review_test_run_dir", run_dir_str))
                if run_dir_str
                else None
            ),
            "runs_dir": Path(runs_dir_str) if runs_dir_str else None,
        },
        review_db=review_db,
        pending_reviews=1,
    )
    review_queue.render(ctx)
    if action == "save":
        review_queue.st.data_editor = real_data_editor
    if observe_success:
        review_queue.st.cache_data.clear = real_clear
        review_queue.st.rerun = real_rerun
