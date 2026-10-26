"""Regression tests for the 2026-08-02 seventh-pass audit.

Every defect covered here survived a fully green gate suite, so each test is written
to fail against the pre-fix code rather than merely to exercise the new code.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from account_fixtures import seed_account

from ledger.analysis.health_scorer import FinancialHealthScorer
from ledger.config.repository import SQLiteConfigRepository
from ledger.config.schemas import CategoriesConfig, CategoryRule, MerchantRule, MerchantRulesConfig
from ledger.core.exceptions import ConfigError
from ledger.insights.service import InsightEngine
from ledger.planning.service import BudgetService
from ledger.query.service import LedgerQueryService
from ledger.storage.database import (
    FinancialHealthScoreDB,
    TransactionDB,
    init_database,
    session_scope,
)
from ledger.storage.migrations import run_migrations, validate_database
from ledger.ui.pages.review_queue import NEEDS_DECISION, _visible_rows
from ledger.utils.json_io import json_safe

# --------------------------------------------------------------------------------------
# H1 — Review Queue default filter matched zero rows
# --------------------------------------------------------------------------------------


def _review_row(review_key: str, status: str, merchant: str = "M", category: str = "C") -> dict:
    return {
        "review_key": review_key,
        "status": status,
        "merchant": merchant,
        "category": category,
        "scope": "same_raw_merchant_in_run",
        "note": "",
        "transaction_count": 1,
        "total_abs_amount": "1.00",
        "examples": "",
    }


def test_needs_decision_is_the_default_and_matches_pending_rows():
    """The landing page's default filter must not match zero rows.

    'Needs decision' is a Status *option*, not a status *value*; matching it against
    row["status"] made the Review Queue permanently render "No rows match the current
    filters" no matter how many rows were pending.
    """
    rows = [
        _review_row("a", "pending"),
        _review_row("b", "resolved"),
        _review_row("c", "reviewed_as_is"),
    ]

    visible = _visible_rows(rows, NEEDS_DECISION, "Any", None)

    assert [row["review_key"] for row in visible] == ["a"]


def test_needs_decision_includes_resolved_rows_that_are_still_unmapped():
    rows = [
        _review_row("complete", "resolved", merchant="M", category="C"),
        _review_row("no-merchant", "resolved", merchant="", category="C"),
        _review_row("no-category", "resolved", merchant="M", category=""),
    ]

    visible = _visible_rows(rows, NEEDS_DECISION, "Any", None)

    assert sorted(row["review_key"] for row in visible) == ["no-category", "no-merchant"]


def test_literal_status_and_all_filters_still_work():
    rows = [_review_row("a", "pending"), _review_row("b", "resolved")]

    assert [r["review_key"] for r in _visible_rows(rows, "resolved", "Any", None)] == ["b"]
    assert len(_visible_rows(rows, "All", "Any", None)) == 2


# --------------------------------------------------------------------------------------
# H2 — validate() reported a config as healthy that no write path would accept
# --------------------------------------------------------------------------------------


def _single_rule_config() -> MerchantRulesConfig:
    return MerchantRulesConfig(
        version="v",
        rules=[MerchantRule(merchant_norm="ACME", patterns=[r"(?i)ACME"], category="Groceries")],
    )


def _categories() -> CategoriesConfig:
    return CategoriesConfig(
        version="v",
        default_category="Other",
        categories=[
            CategoryRule(name="Groceries", merchants=["ACME"]),
            CategoryRule(name="Other", merchants=[]),
        ],
    )


def _seed_duplicate_rules(db_path: Path) -> SQLiteConfigRepository:
    """Persist a duplicate merchant rule the way a legacy DB holds one.

    replace_from_configs refuses to write duplicates, and merchant_rules has no unique
    constraint, so the only faithful way to reproduce the live state is a direct insert.
    """
    repo = SQLiteConfigRepository(db_path)
    repo.replace_from_configs(_single_rule_config(), _categories())
    with closing(sqlite3.connect(db_path)) as conn, conn:
        cursor = conn.execute(
            "INSERT INTO merchant_rules (merchant_norm, category, exclude_from_spending, sort_order)"
            " VALUES ('ACME', 'Groceries', 0, 99)"
        )
        conn.execute(
            "INSERT INTO merchant_rule_patterns (rule_id, pattern, sort_order) VALUES (?, ?, 0)",
            (cursor.lastrowid, r"(?i)ACME CORP"),
        )
    return repo


def test_validate_detects_duplicate_merchant_rules(tmp_path: Path):
    repo = _seed_duplicate_rules(tmp_path / "ledger.db")

    result = repo.validate()

    assert not result.ok
    assert any("duplicate rules" in issue for issue in result.issues)


def test_validate_agrees_with_what_the_write_path_rejects(tmp_path: Path):
    """The invariant behind H2: validate() must never call a config healthy that
    replace_from_configs would refuse. Before the fix, validate() returned zero issues
    while every save raised ConfigError, so the UI said "valid" on an unsaveable config.
    """
    db_path = tmp_path / "ledger.db"
    repo = _seed_duplicate_rules(db_path)

    assert not repo.validate().ok

    with pytest.raises(ConfigError, match="duplicate rules"):
        repo.replace_from_configs(repo.load_merchant_rules(), repo.load_categories())


def test_validate_accepts_a_clean_config(tmp_path: Path):
    repo = SQLiteConfigRepository(tmp_path / "ledger.db")
    repo.replace_from_configs(_single_rule_config(), _categories())

    assert repo.validate().ok


# --------------------------------------------------------------------------------------
# M11 / M13 — a legacy NOT NULL column the ORM no longer maps
# --------------------------------------------------------------------------------------


def _add_legacy_savings_goal_column(db_path: Path) -> None:
    """Recreate the pre-account-link schema: NOT NULL with no server-side default."""
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("DROP TABLE IF EXISTS savings_goals")
        conn.execute(
            """
            CREATE TABLE savings_goals (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(160) NOT NULL,
                target_amount NUMERIC(12, 2) NOT NULL,
                current_amount NUMERIC(12, 2) NOT NULL,
                target_date DATE,
                status VARCHAR(30) NOT NULL,
                notes TEXT NOT NULL,
                created_at DATETIME NOT NULL,
                updated_at DATETIME NOT NULL,
                account_id VARCHAR(64)
            )
            """
        )


def test_validate_database_reports_a_stale_non_nullable_column(tmp_path: Path):
    """Only (expected - actual) was checked, so a legacy extra NOT NULL column made
    validate_database report SUCCESS on a database where every insert fails."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _add_legacy_savings_goal_column(db_path)

    result = validate_database(db_path, record=False)

    assert result.status == "failed"
    assert result.details["stale_columns"] == 1
    assert any("savings_goals.current_amount" in error for error in result.errors)


def _insert_account(db_path: Path, account_id: str = "acct-1") -> str:
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO accounts (account_id, institution, account_number, account_type,"
            " account_subtype, first_transaction_date, last_transaction_date, is_active,"
            " transaction_count, avg_monthly_transactions, is_credit_card,"
            " created_at, updated_at)"
            " VALUES (?, 'harbor', 'harbor-chequing-0000', 'checking', 'Chequing',"
            " '2026-01-01', '2026-01-31', 1, 0, 0.0, 0,"
            " '2026-01-01 00:00:00', '2026-01-01 00:00:00')",
            (account_id,),
        )
    return account_id


def test_migration_drops_the_legacy_column_and_goals_can_be_saved(tmp_path: Path):
    from ledger.planning.service import BudgetService

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _add_legacy_savings_goal_column(db_path)
    account_id = _insert_account(db_path)
    goal = {
        "name": "Post",
        "target_amount": "100.00",
        "status": "active",
        "notes": "",
        "account_id": account_id,
    }

    # The failure mode, proven against the raw legacy schema: the ORM's INSERT omits
    # current_amount and SQLite rejects the row. BudgetService itself calls
    # ensure_schema, so once the migration exists the service heals this before
    # inserting -- which is why it has to be demonstrated at the SQL level.
    with (
        closing(sqlite3.connect(db_path)) as conn,
        conn,
        pytest.raises(sqlite3.IntegrityError, match="current_amount"),
    ):
        conn.execute(
            "INSERT INTO savings_goals (name, target_amount, status, notes,"
            " created_at, updated_at) VALUES ('Pre', 1.00, 'active', '',"
            " '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        )

    assert run_migrations(db_path).status == "success"

    result = BudgetService(db_path).replace_goals([goal])

    assert result.status == "success", result.errors
    assert [row["name"] for row in BudgetService(db_path).list_goals()] == ["Post"]
    assert validate_database(db_path, record=False).status == "success"


def test_rebuild_preserves_existing_goal_rows(tmp_path: Path):
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _add_legacy_savings_goal_column(db_path)
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute(
            "INSERT INTO savings_goals (name, target_amount, current_amount, status, notes,"
            " created_at, updated_at) VALUES ('Legacy', 500, 0, 'active', '',"
            " '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        )

    assert run_migrations(db_path).status == "success"

    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute("SELECT name, target_amount FROM savings_goals").fetchall()
    assert rows == [("Legacy", 500)]


def test_migration_is_idempotent_on_a_fresh_database(tmp_path: Path):
    db_path = tmp_path / "ledger.db"
    init_database(db_path)

    assert run_migrations(db_path).status == "success"
    assert run_migrations(db_path).status == "success"
    assert validate_database(db_path, record=False).status == "success"


# --------------------------------------------------------------------------------------
# H5 — a Decimal in an evidence dict crashed every insight refresh
# --------------------------------------------------------------------------------------


def _insert_txn(db_path: Path, *, txn_id: str, posted: date, amount: str, category: str) -> None:
    with session_scope(db_path) as session:
        session.add(
            TransactionDB(
                txn_id=txn_id,
                posting_date=posted,
                transaction_date=posted,
                institution="harbor",
                account="harbor-chequing-0000",
                account_type="checking",
                flow="expense",
                cashflow_type="expense",
                include_in_cashflow=True,
                currency="CAD",
                amount_raw=Decimal(amount),
                amount_signed=Decimal(amount),
                description="SOMETHING",
                merchant="SOMETHING",
                category=category,
                merchant_confidence="rule",
                category_confidence="rule",
                exclude_from_spending=False,
                merchant_raw="SOMETHING",
                category_raw=category,
                source_file="statement.csv",
                source_path="/synthetic/statement.csv",
                source_period=posted.strftime("%Y-%m"),
                source_row=1,
                imported_at=datetime.now(UTC).replace(tzinfo=None),
                parser_version="test",
                txn_fingerprint=f"{txn_id}-fp",
                day_of_week=posted.weekday(),
                day_of_month=posted.day,
                week_of_year=posted.isocalendar()[1],
                is_weekend=False,
                is_month_start=False,
                is_month_end=False,
            )
        )


def test_over_budget_category_does_not_crash_insight_refresh(tmp_path: Path):
    """budget_variance returns Decimal by contract and evidence lands in a JSON column.

    Stock json.dumps cannot encode Decimal, so a single over-budget category raised
    StatementError and rolled back the whole period's findings -- taking PDF import
    finalize and rules apply down with it, since both call refresh_period unguarded.
    """
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_txn(
        db_path, txn_id="t1", posted=date(2026, 5, 10), amount="-600.00", category="Groceries"
    )
    BudgetService(db_path).replace_budgets(
        [{"month": "*", "category": "Groceries", "amount": "10.00", "notes": ""}]
    )
    variance = BudgetService(db_path).budget_variance(date(2026, 5, 1), date(2026, 5, 31))
    assert variance[0]["status"] == "over"
    assert isinstance(variance[0]["budget"], Decimal)

    result = InsightEngine(db_path).refresh_period(date(2026, 5, 1), date(2026, 5, 31))

    assert result.status == "success", result.errors
    with closing(sqlite3.connect(db_path)) as conn:
        stored = conn.execute(
            "SELECT evidence FROM insight_findings WHERE finding_type = 'budget'"
        ).fetchone()
    assert stored is not None
    assert '"budget": 10.0' in stored[0]


def test_json_safe_normalizes_decimal_date_and_nested_structures():
    payload = {
        "amount": Decimal("1.50"),
        "when": date(2026, 5, 1),
        "nested": [{"x": Decimal("2")}, (Decimal("3"),)],
    }

    assert json_safe(payload) == {
        "amount": 1.5,
        "when": "2026-05-01",
        "nested": [{"x": 2.0}, [3.0]],
    }


def test_json_column_accepts_a_decimal_through_the_engine_serializer(tmp_path: Path):
    """Defence in depth: no JSON column should be one stray Decimal from a hard failure."""
    from ledger.storage.database import DBOperationAuditDB

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    with session_scope(db_path) as session:
        session.add(
            DBOperationAuditDB(
                operation="test",
                status="success",
                started_at=datetime.now(UTC).replace(tzinfo=None),
                finished_at=datetime.now(UTC).replace(tzinfo=None),
                db_path=str(db_path),
                details={"amount": Decimal("12.34"), "on": date(2026, 5, 1)},
            )
        )

    with closing(sqlite3.connect(db_path)) as conn:
        stored = conn.execute("SELECT details FROM db_operation_audit").fetchone()[0]
    assert '"amount": 12.34' in stored
    assert '"on": "2026-05-01"' in stored


# --------------------------------------------------------------------------------------
# H3 / N2 — health scores were written once and served forever
# --------------------------------------------------------------------------------------


def _seed_daily_history(db_path: Path, *, last_day: date, days: int = 40) -> None:
    for offset in range(days):
        posted = last_day - timedelta(days=offset)
        _insert_txn(
            db_path,
            txn_id=f"h{offset}",
            posted=posted,
            amount="-25.00",
            category="Groceries",
        )


def test_anchor_follows_the_data_when_the_wall_clock_has_outrun_it(tmp_path: Path):
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    stale_last_day = date.today() - timedelta(days=200)
    _seed_daily_history(db_path, last_day=stale_last_day)

    assert FinancialHealthScorer(db_path).default_anchor_date() == stale_last_day


def test_anchor_stays_on_yesterday_for_a_current_database(tmp_path: Path):
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _seed_daily_history(db_path, last_day=date.today())

    assert FinancialHealthScorer(db_path).default_anchor_date() == date.today() - timedelta(days=1)


def test_recompute_scores_a_stale_database_and_prunes_forward_dated_rows(tmp_path: Path):
    """The write-once failure: a wall-clock anchor scores nothing once the newest
    statement ages past the 30-day window, so the last stored row -- computed by
    whatever logic existed then -- is frozen and still served as current."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    last_day = date.today() - timedelta(days=200)
    _seed_daily_history(db_path, last_day=last_day)

    scorer = FinancialHealthScorer(db_path)

    # A leftover row from an earlier, further-forward run.
    with session_scope(db_path) as session:
        session.add(
            FinancialHealthScoreDB(
                score_date=last_day + timedelta(days=5),
                overall_score=1,
                cashflow_health=1,
                spending_consistency=1,
                savings_rate=Decimal("0"),
                score_trend_7d="stable",
                score_change_7d=0,
                risk_factors="",
                computed_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )

    assert scorer.compute_recent_scores(days=30) > 0

    latest = LedgerQueryService(db_path).get_latest_health_score()
    assert latest is not None
    assert latest["score_date"] == last_day, "newest score must track the data, not the clock"

    fresh = scorer.compute_daily_score(latest["score_date"])
    assert fresh is not None
    assert latest["overall_score"] == fresh.overall_score, (
        "a stored score must equal what the current code computes for the same day"
    )


def test_inactive_accounts_do_not_suppress_anchored_metrics(tmp_path: Path):
    """_has_absolute_balance_anchor requires every in-scope account to have an absolute
    balance, so counting closed accounts permanently zeroed emergency-fund and debt."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _seed_daily_history(db_path, last_day=date.today() - timedelta(days=200))
    _insert_account(db_path, account_id="closed-1")
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("UPDATE accounts SET is_active = 0 WHERE account_id = 'closed-1'")

    active = FinancialHealthScorer(db_path).query_service.get_accounts(is_active=True)

    assert [row["account_id"] for row in active] == []


# --------------------------------------------------------------------------------------
# H4 — a comma inside a quantifier shattered merchant regexes on save
# --------------------------------------------------------------------------------------


def test_patterns_split_on_newlines_only():
    from ledger.ui.pages.rules_settings import _split_lines

    assert _split_lines(r"(?i)^CEDAR\s+\*{2,}") == [r"(?i)^CEDAR\s+\*{2,}"]
    assert _split_lines("a\nb") == ["a", "b"]
    assert _split_lines(None) == []


def test_pattern_round_trip_survives_a_bounded_quantifier():
    """The writer joins with "\\n"; the reader must split the same way, or a save
    silently rewrites the rule into two fragments that both still compile."""
    import pandas as pd

    from ledger.ui.pages.rules_settings import _merchant_dataframe, _merchants_from_dataframe

    original = MerchantRulesConfig(
        version="v",
        rules=[
            MerchantRule(
                merchant_norm="CEDAR",
                patterns=[r"(?i)^ONLINE BANKING PAYMENT - \d{3,4}\s+CEDAR\s+BANK$"],
                category="Groceries",
            )
        ],
    )

    frame = _merchant_dataframe(original)
    assert isinstance(frame, pd.DataFrame)
    round_tripped = _merchants_from_dataframe(frame, original.version)

    assert round_tripped.rules[0].patterns == original.rules[0].patterns


def test_clearing_patterns_is_surfaced_as_a_deletion():
    import pandas as pd

    from ledger.ui.pages.rules_settings import _dropped_merchant_rows

    frame = pd.DataFrame(
        [
            {"Merchant": "KEPT", "Category": "C", "Exclude": False, "Patterns": "x"},
            {"Merchant": "CLEARED", "Category": "C", "Exclude": False, "Patterns": ""},
        ]
    )

    assert _dropped_merchant_rows(frame) == ["CLEARED"]


# --------------------------------------------------------------------------------------
# M21 — the diff preview could not show deletions or renames
# --------------------------------------------------------------------------------------


def test_diff_preview_reports_removed_rows():
    import pandas as pd

    from ledger.ui.pages.rules_settings import _changed_dataframe, _changed_rows

    original = pd.DataFrame(
        [{"Merchant": "A", "Category": "X"}, {"Merchant": "B", "Category": "Y"}]
    )
    edited = pd.DataFrame([{"Merchant": "A", "Category": "X"}])

    changed = _changed_dataframe(original, edited, "Merchant")

    assert list(changed["Merchant"]) == ["B"]
    assert list(changed["Change"]) == ["removed"]
    assert _changed_rows(original, edited, "Merchant") == 1


def test_diff_preview_is_not_confused_by_row_order():
    """Positional comparison reported every row after an insertion as changed."""
    import pandas as pd

    from ledger.ui.pages.rules_settings import _changed_rows

    original = pd.DataFrame(
        [{"Merchant": "A", "Category": "X"}, {"Merchant": "B", "Category": "Y"}]
    )
    reordered = pd.DataFrame(
        [{"Merchant": "B", "Category": "Y"}, {"Merchant": "A", "Category": "X"}]
    )

    assert _changed_rows(original, reordered, "Merchant") == 0


# --------------------------------------------------------------------------------------
# H7 — the card statement reference was purely positional
# --------------------------------------------------------------------------------------


def _card_refs(rows: list[tuple[date, str, Decimal]]) -> list[str]:
    from collections import Counter

    from ledger.imports.pdf.statement_pdf_to_csv._common import row_content_key, row_reference

    seen: Counter = Counter()
    refs = []
    for posted, description, amount in rows:
        key = row_content_key(posted, description, amount)
        occurrence = seen[key]
        seen[key] += 1
        refs.append(
            row_reference(
                "1234",
                date(2026, 5, 1),
                posting_date=posted,
                description=description,
                amount=amount,
                occurrence=occurrence,
            )
        )
    return refs


def test_recovering_an_earlier_row_does_not_re_key_later_rows():
    """The reference used to be the row's ordinal among accepted rows, so an extractor
    fix that recovered a dropped row renumbered everything after it -- silently moving
    MANUAL/RULE overrides onto different transactions on the next reimport."""
    base = [
        (date(2026, 4, 10), "COFFEE", Decimal("-5.00")),
        (date(2026, 4, 11), "GOLF DOME", Decimal("-30.00")),
    ]
    with_recovered = [(date(2026, 4, 9), "RECOVERED", Decimal("-1.00")), *base]

    assert _card_refs(base) == _card_refs(with_recovered)[1:]


def test_identical_same_day_rows_still_get_distinct_references():
    rows = [
        (date(2026, 4, 11), "GOLF DOME", Decimal("-30.00")),
        (date(2026, 4, 11), "GOLF DOME", Decimal("-30.00")),
    ]

    refs = _card_refs(rows)

    assert refs[0] != refs[1]


# --------------------------------------------------------------------------------------
# db doctor
# --------------------------------------------------------------------------------------


def test_doctor_detects_and_repairs_duplicate_merchant_rules(tmp_path: Path):
    from ledger.maintenance.doctor import DatabaseDoctor

    db_path = tmp_path / "ledger.db"
    repo = _seed_duplicate_rules(db_path)
    doctor = DatabaseDoctor(db_path, audit_runs_dir=tmp_path / "no-audits")

    checks = {finding.check for finding in doctor.diagnose()}
    assert "duplicate_merchant_rules" in checks

    result = doctor.run(execute=True)

    assert result.details["repaired"]["duplicate_merchant_rules"] == 1
    assert repo.validate().ok
    repo.replace_from_configs(repo.load_merchant_rules(), repo.load_categories())


def test_doctor_flags_shattered_patterns_without_repairing_them(tmp_path: Path):
    """Rejoining a split regex is a guess, so it must be reported, never rewritten."""
    from ledger.maintenance.doctor import DatabaseDoctor

    db_path = tmp_path / "ledger.db"
    repo = SQLiteConfigRepository(db_path)
    repo.replace_from_configs(_single_rule_config(), _categories())
    with closing(sqlite3.connect(db_path)) as conn, conn:
        rule_id = conn.execute("SELECT id FROM merchant_rules LIMIT 1").fetchone()[0]
        conn.execute("DELETE FROM merchant_rule_patterns WHERE rule_id = ?", (rule_id,))
        for order, pattern in enumerate([r"(?i)^CEDAR\s+\*{2", "}"]):
            conn.execute(
                "INSERT INTO merchant_rule_patterns (rule_id, pattern, sort_order)"
                " VALUES (?, ?, ?)",
                (rule_id, pattern, order),
            )

    doctor = DatabaseDoctor(db_path, audit_runs_dir=tmp_path / "no-audits")
    findings = {finding.check: finding for finding in doctor.diagnose()}

    assert "shattered_patterns" in findings
    assert findings["shattered_patterns"].repairable is False

    doctor.run(execute=True)
    with closing(sqlite3.connect(db_path)) as conn:
        remaining = conn.execute("SELECT COUNT(*) FROM merchant_rule_patterns").fetchone()[0]
    assert remaining == 2, "shattered patterns must be left exactly as found"


def test_doctor_reports_parser_version_drift(tmp_path: Path):
    from ledger.maintenance.doctor import DatabaseDoctor

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_txn(db_path, txn_id="d1", posted=date(2026, 5, 1), amount="-1.00", category="Other")

    findings = {
        finding.check: finding
        for finding in DatabaseDoctor(db_path, audit_runs_dir=tmp_path / "none").diagnose()
    }

    assert "parser_version_drift" in findings
    assert findings["parser_version_drift"].count == 1


def test_doctor_is_clean_on_a_freshly_built_database(tmp_path: Path):
    from ledger.maintenance.doctor import DatabaseDoctor

    db_path = tmp_path / "ledger.db"
    init_database(db_path)

    findings = DatabaseDoctor(db_path, audit_runs_dir=tmp_path / "none").diagnose()

    assert findings == [], [finding.summary for finding in findings]


# --------------------------------------------------------------------------------------
# L2 — row and balance mismatches were summed into one opaque number
# --------------------------------------------------------------------------------------


def test_audit_summary_separates_row_and_balance_mismatches(tmp_path: Path):
    import json as _json

    from ledger.evidence.service import latest_audit_summary

    run_dir = tmp_path / "20260101T000000Z"
    run_dir.mkdir(parents=True)
    (run_dir / "summary.json").write_text(
        _json.dumps(
            {
                "status": "issues_found",
                "counts": {
                    "pdfs": 95,
                    "source_rows": 1849,
                    "db_rows": 1834,
                    "row_mismatches": 15,
                    "balance_mismatches": 36,
                },
                "mismatches_by_type": {"count_mismatch": 13, "db_only": 1, "source_only": 1},
            }
        ),
        encoding="utf-8",
    )

    summary = latest_audit_summary(tmp_path)

    assert summary["row_mismatches"] == 15
    assert summary["balance_mismatches"] == 36
    assert summary["mismatches_by_type"]["count_mismatch"] == 13
    assert "15 row mismatches (15 row(s) missing)" in summary["detail"]
    assert "36 balance mismatches" in summary["detail"]


# --------------------------------------------------------------------------------------
# W4 — destructive-command safety
# --------------------------------------------------------------------------------------


def test_cleanup_retains_the_newest_backup_of_every_reason(tmp_path: Path):
    """Pre-mutation snapshots ARE the advertised rollback path.

    Retaining only the newest '-cleanup' backup deleted every '-pre-*' one, so a
    cleanup run minutes after `rules apply --execute` discarded the only way back.
    """
    import os

    from ledger.maintenance.cleanup import DataCleanupService

    data_dir = tmp_path / "data"
    backups = data_dir / "db-backups"
    backups.mkdir(parents=True)
    db_path = data_dir / "ledger.db"
    init_database(db_path)

    names = [
        "ledger-20260101T000000Z-aaaaaaaa-cleanup.db",
        "ledger-20260102T000000Z-bbbbbbbb-pre-rules-apply.db",
        "ledger-20260103T000000Z-cccccccc-pre-doctor.db",
        "ledger-20260104T000000Z-dddddddd-cleanup.db",
    ]
    for index, name in enumerate(names):
        path = backups / name
        path.write_bytes(b"x")
        os.utime(path, (1_000_000 + index * 60, 1_000_000 + index * 60))

    targets = {path.name for path in DataCleanupService(db_path)._backup_targets()}

    # The newest overall AND the newest pre-mutation snapshot both survive, so a
    # cleanup run right after a mutation never removes the advertised rollback path.
    assert "ledger-20260104T000000Z-dddddddd-cleanup.db" not in targets
    assert "ledger-20260103T000000Z-cccccccc-pre-doctor.db" not in targets
    assert "ledger-20260101T000000Z-aaaaaaaa-cleanup.db" in targets
    assert "ledger-20260102T000000Z-bbbbbbbb-pre-rules-apply.db" in targets


def test_cleanup_refuses_a_home_scoped_data_dir(tmp_path: Path):
    """A home-scoped db path makes data_dir $HOME -- and cleanup then walks it
    recursively. (.ledgerrc.example shipped exactly that before 2026-08; the
    guard outlives the example.)"""
    from ledger.maintenance.cleanup import DataCleanupService

    with pytest.raises(ValueError, match="home or filesystem root"):
        DataCleanupService(Path.home() / "ledger.db")


def test_config_import_dry_run_changes_nothing(tmp_path: Path):
    from typer.testing import CliRunner

    from ledger.cli import app

    db_path = tmp_path / "ledger.db"
    repo = SQLiteConfigRepository(db_path)
    repo.replace_from_configs(_single_rule_config(), _categories())

    source = tmp_path / "configs"
    source.mkdir()
    (source / "merchants.toml").write_text(
        '[[rule]]\nmerchant_norm = "OTHER"\npatterns = ["(?i)OTHER"]\ncategory = "Groceries"\n',
        encoding="utf-8",
    )
    (source / "categories.toml").write_text(
        '[[category]]\ncategory_norm = "Groceries"\nmerchants = ["OTHER"]\n',
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app, ["config", "import", "--db", str(db_path), "--source", str(source)]
    )

    assert result.exit_code == 0, result.output
    assert "Dry run" in result.output
    assert "would be deleted" in result.output
    assert [rule.merchant_norm for rule in repo.load_merchant_rules().rules] == ["ACME"]


def test_config_import_execute_replaces_and_backs_up(tmp_path: Path):
    from typer.testing import CliRunner

    from ledger.cli import app

    db_path = tmp_path / "ledger.db"
    repo = SQLiteConfigRepository(db_path)
    repo.replace_from_configs(_single_rule_config(), _categories())

    source = tmp_path / "configs"
    source.mkdir()
    (source / "merchants.toml").write_text(
        '[[rule]]\nmerchant_norm = "OTHER"\npatterns = ["(?i)OTHER"]\ncategory = "Groceries"\n',
        encoding="utf-8",
    )
    (source / "categories.toml").write_text(
        '[[category]]\ncategory_norm = "Groceries"\nmerchants = ["OTHER"]\n',
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        app,
        ["config", "import", "--db", str(db_path), "--source", str(source), "--execute"],
    )

    assert result.exit_code == 0, result.output
    assert [rule.merchant_norm for rule in repo.load_merchant_rules().rules] == ["OTHER"]
    assert list((db_path.parent / "db-backups").glob("*.db")), "must take a pre-mutation backup"


def test_insight_refresh_on_an_empty_database_is_a_no_op(tmp_path: Path):
    """An empty database produces a snapshot with no period, and every caller passed
    those None dates straight through -- crashing inside budget_variance."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)

    result = InsightEngine(db_path).refresh_period(None, None)

    assert result.status == "success"
    assert result.counts["generated"] == 0


# --------------------------------------------------------------------------------------
# H10 — "Recurring Monthly" dropped every non-monthly cadence
# --------------------------------------------------------------------------------------


def test_monthly_equivalent_normalizes_every_detected_frequency():
    from ledger.analysis.pattern_detector import RECURRING_FREQUENCIES, monthly_equivalent

    # A biweekly bill lands 26x/year, so its monthly equivalent is amount * 26/12 --
    # not amount (which the old frequency == "monthly" filter implied) and not 0
    # (which is what it actually contributed, since it was filtered out entirely).
    assert monthly_equivalent(Decimal("600.00"), "biweekly") == Decimal("1300.00")
    assert monthly_equivalent(Decimal("100.00"), "weekly") == Decimal("433.33")
    assert monthly_equivalent(Decimal("15.00"), "monthly") == Decimal("15.00")
    assert monthly_equivalent(Decimal("300.00"), "quarterly") == Decimal("100.00")
    assert monthly_equivalent(Decimal("1200.00"), "annual") == Decimal("100.00")
    # Unknown cadence contributes nothing rather than being guessed at.
    assert monthly_equivalent(Decimal("50.00"), "sporadic") == Decimal("0")
    assert set(RECURRING_FREQUENCIES) == {"weekly", "biweekly", "monthly", "quarterly", "annual"}


def _insert_pattern(db_path: Path, **kwargs) -> None:
    from ledger.storage.database import RecurringPatternDB

    now = datetime.now(UTC).replace(tzinfo=None)
    institution = kwargs.get("institution", "harbor")
    # The slug must vary with the institution: accounts.account_number carries a
    # unique index, so a fixed slug would make the second institution's seed fail
    # inside the fixture rather than at an assertion. Only seed when the caller has
    # not supplied its own account_id.
    account_id = kwargs.get("account_id") or seed_account(
        db_path, institution, f"{institution}-chequing-9101"
    )
    defaults: dict = {
        "pattern_type": "bill",
        "merchant": "M",
        "category": "Housing",
        "institution": institution,
        "account_id": account_id,
        "frequency": "monthly",
        "expected_day_of_period": 1,
        "is_fixed_amount": True,
        "expected_amount": Decimal("10.00"),
        "amount_std_dev": Decimal("0.00"),
        "confidence": 0.9,
        "detection_method": "test",
        "occurrence_count": 5,
        "first_occurrence": date(2026, 1, 1),
        "last_occurrence": date(2026, 6, 1),
        "next_expected_date": date(2026, 7, 1),
        "is_active": True,
        "missed_count": 0,
        "detected_at": now,
        "updated_at": now,
    }
    defaults.update(kwargs)
    with session_scope(db_path) as session:
        session.add(RecurringPatternDB(**defaults))


def test_readiness_counts_biweekly_costs_and_excludes_income(tmp_path: Path):
    """A synthetic biweekly bill contributes its monthly equivalent; income does not."""
    from ledger.planning.readiness import PlanningReadinessService

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_pattern(
        db_path,
        pattern_id="synthetic-biweekly-bill",
        frequency="biweekly",
        expected_amount=Decimal("600.00"),
        pattern_type="bill",
    )
    _insert_pattern(
        db_path,
        pattern_id="synthetic-monthly-sub",
        frequency="monthly",
        expected_amount=Decimal("15.00"),
    )
    _insert_pattern(
        db_path,
        pattern_id="payroll",
        pattern_type="income",
        frequency="monthly",
        expected_amount=Decimal("3200.00"),
    )

    summary = PlanningReadinessService(db_path).summary(date(2026, 6, 1), date(2026, 6, 30))

    # 600 * 26 / 12 = 1300; add the 15 monthly subscription and exclude income.
    assert summary["recurring_monthly"] == Decimal("1315.00")


# --------------------------------------------------------------------------------------
# H11 — a truncated baseline was reported as "comparable"
# --------------------------------------------------------------------------------------


def test_snapshot_flags_a_shorter_comparison_baseline(tmp_path: Path):
    from ledger.reporting.service import ReportSnapshotService

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    # Data starts 2026-05-20, so a June report can only look back 12 days.
    for day in (20, 25, 30):
        _insert_txn(
            db_path,
            txn_id=f"m{day}",
            posted=date(2026, 5, day),
            amount="-10.00",
            category="Groceries",
        )
    _insert_txn(
        db_path, txn_id="j1", posted=date(2026, 6, 15), amount="-500.00", category="Groceries"
    )

    payload, _start, _end = ReportSnapshotService(db_path)._compute_payload(
        date(2026, 6, 1), date(2026, 6, 30)
    )
    period = payload["period"]

    assert period["days"] == 30
    assert period["baseline_days"] == 12
    assert period["baseline_comparable"] is False
    # Percentages are suppressed rather than reporting a spurious four-digit change.
    movement = payload["income_spend_net_movement"]["spending"]
    assert movement["change_pct"] == 0
    assert movement["comparable"] is False


def test_snapshot_marks_an_equal_length_baseline_comparable(tmp_path: Path):
    from ledger.reporting.service import ReportSnapshotService

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    for month, day in ((4, 5), (5, 5), (6, 5)):
        _insert_txn(
            db_path,
            txn_id=f"e{month}",
            posted=date(2026, month, day),
            amount="-100.00",
            category="Groceries",
        )

    payload, _s, _e = ReportSnapshotService(db_path)._compute_payload(
        date(2026, 6, 1), date(2026, 6, 30)
    )

    assert payload["period"]["baseline_comparable"] is True
    assert payload["period"]["baseline_days"] == payload["period"]["days"]


# --------------------------------------------------------------------------------------
# M8 — income and expenses were not a subtractable pair
# --------------------------------------------------------------------------------------


def test_compute_kpis_exposes_gross_expenses_for_like_for_like_display(tmp_path: Path):
    """`expenses` applies exclude_from_spending but `income` cannot, so the two tiles
    were not subtractable. gross_expenses gives callers a matching pair."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_txn(
        db_path, txn_id="a", posted=date(2026, 5, 2), amount="-100.00", category="Groceries"
    )
    with session_scope(db_path) as session:
        from ledger.storage.database import TransactionDB as T

        row = session.get(T, "a")
        assert row is not None
    _insert_txn(
        db_path, txn_id="b", posted=date(2026, 5, 3), amount="-900.00", category="Transfers"
    )
    with closing(sqlite3.connect(db_path)) as conn, conn:
        conn.execute("UPDATE transactions SET exclude_from_spending = 1 WHERE txn_id = 'b'")

    kpis = LedgerQueryService(db_path).compute_kpis(date(2026, 5, 1), date(2026, 5, 31))

    assert kpis["expenses"] == Decimal("100.00")
    assert kpis["gross_expenses"] == Decimal("1000.00")
    # net stays on the spend universe, as the third-pass audit locked in.
    assert kpis["net"] == kpis["income"] - kpis["expenses"]


# --------------------------------------------------------------------------------------
# H9 — investment P&L read a section subtotal instead of the grand total
# --------------------------------------------------------------------------------------


def _seed_investment_statement(db_path: Path, *, with_forex_section: bool) -> None:
    """One statement whose performance summary has section subtotals plus a grand row.

    The fixture stores one section "Total" and one "Total (All Assets)". When a
    Forex section is present it sorts last and reports zero unrealized gain, so
    max(source_row) over "Total" picked exactly the wrong row.
    """
    from ledger.storage.database import (
        InvestmentAccountDB,
        InvestmentPerformanceDB,
        InvestmentStatementDB,
    )

    with session_scope(db_path) as session:
        now = datetime.now(UTC).replace(tzinfo=None)
        session.add(
            InvestmentAccountDB(
                account_id="acct",
                account_alias="TFSA",
                institution="summit",
                source_account_id="U000",
                account_type="TFSA",
                base_currency="USD",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            InvestmentStatementDB(
                statement_id="stmt",
                account_id="acct",
                period_start=date(2026, 5, 1),
                period_end=date(2026, 5, 31),
                generated_at=now,
                base_currency="USD",
                total_nav=Decimal("1000.000000000000"),
                parsed_fingerprint="fp",
                imported_at=now,
            )
        )
        session.flush()
        # Independently invented fixed-point values: section totals reconcile exactly.
        unrealized = Decimal("321.654987")
        realized = Decimal("-7.246813")
        stock_realized = Decimal("-31.500000") if with_forex_section else realized
        rows = [("Stocks", 10, stock_realized, unrealized)]
        if with_forex_section:
            rows.append(("Total", 20, Decimal("24.253187"), Decimal("0")))
        else:
            rows.append(("Total", 20, realized, unrealized))
        rows.append(("Total (All Assets)", 30, realized, unrealized))
        for category, source_row, realized, unrealized in rows:
            session.add(
                InvestmentPerformanceDB(
                    statement_id="stmt",
                    source_row=source_row,
                    asset_category=category,
                    realized_total=realized,
                    unrealized_total=unrealized,
                )
            )


def test_investment_performance_prefers_the_grand_total_row(tmp_path: Path):
    from ledger.query.investments import InvestmentQueries

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _seed_investment_statement(db_path, with_forex_section=True)

    rows = InvestmentQueries(db_path).get_investment_performance()

    assert len(rows) == 1
    # The Forex section subtotal reports 0; the grand total reports the real figure.
    assert Decimal(str(rows[0]["unrealized_pl"])) == Decimal("321.654987")
    assert Decimal(str(rows[0]["realized_pl"])) == Decimal("-7.246813")
    assert rows[0]["base_currency"] == "USD"


def test_investment_performance_falls_back_to_the_section_subtotal(tmp_path: Path):
    """A statement with no grand row must still report its section subtotal."""
    from ledger.query.investments import InvestmentQueries
    from ledger.storage.database import InvestmentPerformanceDB

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _seed_investment_statement(db_path, with_forex_section=False)
    with session_scope(db_path) as session:
        session.query(InvestmentPerformanceDB).filter(
            InvestmentPerformanceDB.asset_category == "Total (All Assets)"
        ).delete(synchronize_session=False)

    rows = InvestmentQueries(db_path).get_investment_performance()

    assert Decimal(str(rows[0]["unrealized_pl"])) == Decimal("321.654987")
    assert Decimal(str(rows[0]["realized_pl"])) == Decimal("-7.246813")
    assert rows[0]["base_currency"] == "USD"


# --------------------------------------------------------------------------------------
# M1 — period charts rendered a sub-millisecond datetime axis
# --------------------------------------------------------------------------------------


def test_period_charts_use_a_categorical_x_axis():
    """ "2026-06" is a period LABEL. Plotly auto-detected it as a date, and with a
    single point in range -- the default period -- auto-ranged to microseconds and
    drew ticks like "23:59:59.9996 May 31, 2026"."""
    import pandas as pd

    from ledger.ui.components import line_chart
    from ledger.ui.pages.overview import _cashflow_chart

    assert line_chart(["2026-06"], [1.0], name="x").layout.xaxis.type == "category"

    frame = pd.DataFrame([{"Month": "2026-06", "Income": 1.0, "Expenses": 2.0, "Net": -1.0}])
    assert _cashflow_chart(frame).layout.xaxis.type == "category"


# --------------------------------------------------------------------------------------
# M16 — a spending increase rendered green
# --------------------------------------------------------------------------------------


def test_spending_movement_tile_uses_inverse_delta_colour():
    """st.metric paints a positive delta green by default, so rising costs looked good."""
    import inspect

    from ledger.ui.pages import overview

    source = inspect.getsource(overview._render_reporting_signals)
    assert 'delta_color: Literal["normal", "inverse"] = "inverse" if key == "spending"' in source


# --------------------------------------------------------------------------------------
# M6 — a finalized run kept showing its stage-time gates as live instructions
# --------------------------------------------------------------------------------------


def test_finalized_run_hides_stale_gates_and_next_actions():
    import inspect

    from ledger.ui.pages import import_center

    source = inspect.getsource(import_center.render)
    assert 'is_complete = status in {"finalized", "cancelled"}' in source
    # Stale gate metrics and next-actions are suppressed for a completed run...
    assert 'manifest.get("next_actions", []) if not is_complete else []' in source
    # ...but the readiness TABLE stays: it is the record of what the run verified, and
    # the UI smoke gate asserts the page shows either it or an explicit empty state.
    assert "if readiness_rows:" in source
    assert 'st.caption(f"Gates as recorded for this {status} run. No action is pending.")' in source


def test_import_center_commands_are_runnable_without_activating_the_venv():
    """`ledger` is only on PATH after activating .venv; the docs' supported entry
    point is ./scripts/import-pdf-inbox."""
    import inspect

    from ledger.ui.pages import import_center

    source = inspect.getsource(import_center)
    assert "ledger import pdf stage" not in source
    assert "./scripts/import-pdf-inbox stage" in source


# --------------------------------------------------------------------------------------
# M14 / N1 — optimize was not idempotent and could flip spend semantics
# --------------------------------------------------------------------------------------


def _taxonomy_with_a_root_collision() -> CategoriesConfig:
    # "Food & Dining" is a seeded ROOT, so a same-named source category collides.
    return CategoriesConfig(
        version="v",
        default_category="Uncategorized",
        categories=[
            CategoryRule(
                name="Food & Dining",
                parent=None,
                merchants=["SEED ONLY"],
                exclude_from_spending=True,
            ),
            CategoryRule(
                name="Custom Leaf",
                parent="Food & Dining",
                merchants=["M2"],
                exclude_from_spending=False,
            ),
        ],
    )


def test_optimize_never_flips_spend_semantics_on_a_name_collision():
    """The locked F10 decision: optimization is taxonomy-only and must never change
    what counts as spending. A collision used to `continue`, leaving the seeded root
    (exclude=False) in place while the recommendation CSV reported "no change"."""
    from ledger.optimize.categories import optimize_taxonomy

    recommendations, optimized = optimize_taxonomy(_taxonomy_with_a_root_collision())
    by_name = {rule.name: rule for rule in optimized.categories}
    reported = {rec.old_name: rec for rec in recommendations}["Food & Dining"]

    assert by_name["Food & Dining"].exclude_from_spending is True
    assert reported.new_exclude_from_spending is True, "the CSV must match what persists"


def test_optimize_preserves_seed_memberships_parents_and_default_category():
    from ledger.optimize.categories import optimize_taxonomy

    _recs, optimized = optimize_taxonomy(_taxonomy_with_a_root_collision())
    by_name = {rule.name: rule for rule in optimized.categories}

    assert by_name["Food & Dining"].merchants == ["SEED ONLY"]
    assert by_name["Custom Leaf"].merchants == ["M2"]
    assert by_name["Custom Leaf"].parent == "Food & Dining"
    assert optimized.default_category == "Uncategorized"


def test_optimize_is_idempotent():
    """A second run must be a no-op. The unmapped-category fallback hardcoded
    parent=None, so re-running stripped parents the first run had assigned."""
    from ledger.optimize.categories import optimize_taxonomy

    _r1, first = optimize_taxonomy(_taxonomy_with_a_root_collision())
    _r2, second = optimize_taxonomy(first)

    assert {
        (c.name, c.parent, c.exclude_from_spending, tuple(c.merchants)) for c in first.categories
    } == {
        (c.name, c.parent, c.exclude_from_spending, tuple(c.merchants)) for c in second.categories
    }


# --------------------------------------------------------------------------------------
# M10 — scoped exports shipped unscoped whole-portfolio aggregates
# --------------------------------------------------------------------------------------


def test_scoped_export_omits_the_files_that_ignore_the_scope_filters():
    """recurring_spend, card_account_summary and category_movement derive from
    period-wide services and a whole-period snapshot, so they never saw the
    institution/category/merchant filters -- a package narrowed to one category
    still shipped whole-portfolio data."""
    import inspect

    from ledger.exporting import service as export_service

    source = inspect.getsource(export_service.ExportPackageService.create_package)
    assert "scoped = bool(institutions or categories or merchants)" in source
    assert "if not scoped:" in source
    assert "OMITTED.txt" in source


# --------------------------------------------------------------------------------------
# N5 — `repair statements --execute` destroyed the entire investment domain
# --------------------------------------------------------------------------------------


def test_investment_tables_are_derived_from_the_orm_not_hand_listed():
    """A newly-added investment table must be carried forward automatically."""
    from ledger.repair.statements import investment_table_names

    names = investment_table_names()

    assert "investment_statements" in names
    assert "investment_source_associations" in names
    assert all(name.startswith("investment_") for name in names)


def test_repair_carries_the_investment_domain_into_the_candidate(tmp_path: Path):
    """A statement repair rebuilds the candidate from TRANSACTION PDFs only, so its
    investment tables are empty. Promote then replaced the live ones with that
    emptiness -- destroying every statement, activity, position, and archived-source
    provenance row, none of which can be rebuilt from the sources repair reads.
    """
    from ledger.repair.statements import carry_forward_investments

    live = tmp_path / "live.db"
    candidate = tmp_path / "candidate.db"
    init_database(live)
    init_database(candidate)
    _seed_investment_statement(live, with_forex_section=True)

    with closing(sqlite3.connect(live)) as conn:
        live_statements = conn.execute("SELECT COUNT(*) FROM investment_statements").fetchone()[0]
    with closing(sqlite3.connect(candidate)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM investment_statements").fetchone()[0] == 0

    copied = carry_forward_investments(live, candidate)

    assert copied["investment_statements"] == live_statements == 1
    assert copied["investment_accounts"] == 1
    with closing(sqlite3.connect(candidate)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM investment_statements").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM investment_performance").fetchone()[0] == 3
        # Referential integrity survives the copy.
        assert conn.execute("PRAGMA foreign_key_check").fetchone() is None


def test_carry_forward_investments_is_idempotent(tmp_path: Path):
    """Repair may be re-run; carrying forward twice must not duplicate rows."""
    from ledger.repair.statements import carry_forward_investments

    live = tmp_path / "live.db"
    candidate = tmp_path / "candidate.db"
    init_database(live)
    init_database(candidate)
    _seed_investment_statement(live, with_forex_section=True)

    carry_forward_investments(live, candidate)
    carry_forward_investments(live, candidate)

    with closing(sqlite3.connect(candidate)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM investment_statements").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM investment_performance").fetchone()[0] == 3
