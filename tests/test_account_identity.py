"""Canonical account identity and the referential integrity built on it."""

from __future__ import annotations

import ast
import contextlib
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from ledger.analysis.account_detector import AccountDetector
from ledger.core.account_identity import account_identity, institution_from_statement_type
from ledger.core.analysis_models import Account
from ledger.maintenance.doctor import DatabaseDoctor
from ledger.storage.database import (
    AccountBalanceDB,
    AccountDB,
    AccountStatementCreditLimitDB,
    RecurringPatternDB,
    session_scope,
)
from ledger.storage.migrations import run_migrations
from ledger.utils.hashing import stable_id


def test_account_identity_matches_the_historical_derivation() -> None:
    """The helper must reproduce ids already stored by account_detector."""
    assert account_identity("harbor", "harbor-orbit-visa-9105") == stable_id(
        "harbor", "harbor-orbit-visa-9105"
    )


def test_account_identity_reproduces_a_known_stored_id() -> None:
    """Pinned so a refactor cannot silently re-key an existing database.

    The literals are independent fictional slugs. Their expected hashes are
    fixed examples of the normalized SHA-256 derivation.
    """
    assert account_identity("harbor", "harbor-orbit-visa-9105") == (
        "bcad904347f73b8657c86f93ed2fd66913567f95"
    )
    assert account_identity("meadow", "meadow-silver-mastercard-9106") == (
        "dc919b4b6a969722dc73fdfbca3b9f2a4d135120"
    )


def test_account_identity_is_case_insensitive() -> None:
    assert account_identity("HARBOR", "HARBOR-Orbit-Visa-9105") == account_identity(
        "harbor", "harbor-orbit-visa-9105"
    )


@pytest.mark.parametrize("institution", ["", "   "])
def test_account_identity_rejects_a_blank_institution(institution: str) -> None:
    with pytest.raises(ValueError):
        account_identity(institution, "harbor-orbit-visa-9105")


@pytest.mark.parametrize("slug", ["", "   "])
def test_account_identity_rejects_a_blank_slug(slug: str) -> None:
    with pytest.raises(ValueError):
        account_identity("harbor", slug)


@pytest.mark.parametrize(
    ("statement_type", "expected"),
    [
        ("harbor_deposit", "harbor"),
        ("harbor_visa", "harbor"),
        ("cedar_mastercard", "cedar"),
        ("meadow_mastercard", "meadow"),
        ("lumen_deposit", "lumen"),
        ("lumen_credit", "lumen"),
    ],
)
def test_institution_comes_from_the_statement_type(statement_type: str, expected: str) -> None:
    assert institution_from_statement_type(statement_type) == expected


def test_institution_from_statement_type_rejects_an_unknown_type() -> None:
    """Guessing would mint an id matching no account, so balances would attach to nothing."""
    with pytest.raises(ValueError):
        institution_from_statement_type("managed_by_investment_importer")


def test_an_aliased_slug_does_not_determine_the_institution() -> None:
    """Account aliases rename a slug to anything, so its leading segment is not the bank.

    `harbor-chequing-1001` may be aliased to `household-chequing`; the account id
    must still be the HARBOR one.
    """
    aliased = "household-chequing"
    assert account_identity(institution_from_statement_type("harbor_deposit"), aliased) == (
        account_identity("harbor", aliased)
    )


def test_no_module_derives_an_account_id_inline() -> None:
    """Every account id must come from account_identity().

    An inline institution constant would forge an identity for other accounts
    routed through statement-balance repair. The structural guard rejects that.
    """

    def _is_stable_id_call(node: ast.expr) -> bool:
        if not isinstance(node, ast.Call):
            return False
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        return name == "stable_id"

    offenders: list[str] = []
    for path in sorted(Path("src/ledger").rglob("*.py")):
        if path.name == "account_identity.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # `account_id = stable_id(...)` / `self.account_id = stable_id(...)`
            if isinstance(node, ast.Assign) and _is_stable_id_call(node.value):
                targets = [
                    target.id if isinstance(target, ast.Name) else getattr(target, "attr", "")
                    for target in node.targets
                ]
                if "account_id" in targets:
                    offenders.append(f"{path}:{node.lineno}")
            # `account_id=stable_id(...)` passed as a keyword
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg == "account_id" and _is_stable_id_call(keyword.value):
                        offenders.append(f"{path}:{keyword.value.lineno}")
    assert offenders == [], f"derive account ids via account_identity(): {offenders}"


def test_statement_repair_does_not_hardcode_an_institution() -> None:
    text = Path("src/ledger/repair/statements.py").read_text(encoding="utf-8")
    tree = ast.parse(text)
    literal_institution_calls = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"stable_id", "account_identity"}
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ]
    assert literal_institution_calls == []
    assert "account_identity(" in text


def _account(account_id: str, slug: str) -> Account:
    return Account(
        account_id=account_id,
        institution="harbor",
        account_number=slug,
        account_type="checking",
        account_subtype="Chequing",
        first_transaction_date=date(2025, 1, 1),
        last_transaction_date=date(2025, 6, 1),
        is_active=True,
        transaction_count=3,
        avg_monthly_transactions=Decimal("1.00"),
        is_credit_card=False,
        detected_credit_limit=None,
        created_at=datetime(2025, 6, 1, 12, 0),
        updated_at=datetime(2025, 6, 1, 12, 0),
    )


def _pattern(account_id: str) -> RecurringPatternDB:
    return RecurringPatternDB(
        pattern_id="pattern-stale",
        pattern_type="subscription",
        merchant="NETFLIX",
        category="Entertainment",
        institution="harbor",
        account_id=account_id,
        frequency="monthly",
        expected_day_of_period=1,
        is_fixed_amount=True,
        expected_amount=Decimal("10.00"),
        amount_std_dev=Decimal("0.00"),
        confidence=0.9,
        detection_method="rule_based",
        occurrence_count=3,
        first_occurrence=date(2025, 1, 1),
        last_occurrence=date(2025, 3, 1),
        next_expected_date=date(2025, 4, 1),
        is_active=True,
        missed_count=0,
        detected_at=datetime(2025, 6, 1, 12, 0),
        updated_at=datetime(2025, 6, 1, 12, 0),
    )


def test_replace_all_retires_patterns_for_absent_accounts(tmp_path: Path) -> None:
    """A retired account must not leave a recurring pattern pointing at it."""
    db_path = tmp_path / "ledger.db"
    run_migrations(db_path)

    stale_id = account_identity("harbor", "harbor-closed-0001")
    kept_id = account_identity("harbor", "harbor-chequing-9101")

    detector = AccountDetector(db_path)
    detector.save_accounts(
        [_account(stale_id, "harbor-closed-0001"), _account(kept_id, "harbor-chequing-9101")],
        replace_all=True,
    )
    with session_scope(db_path) as session:
        session.add(_pattern(stale_id))

    detector.save_accounts([_account(kept_id, "harbor-chequing-9101")], replace_all=True)

    with session_scope(db_path) as session:
        assert session.query(AccountDB).count() == 1
        assert session.query(RecurringPatternDB).count() == 0


def test_replace_all_keeps_patterns_for_surviving_accounts(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    run_migrations(db_path)
    kept_id = account_identity("harbor", "harbor-chequing-9101")

    detector = AccountDetector(db_path)
    detector.save_accounts([_account(kept_id, "harbor-chequing-9101")], replace_all=True)
    with session_scope(db_path) as session:
        session.add(_pattern(kept_id))

    detector.save_accounts([_account(kept_id, "harbor-chequing-9101")], replace_all=True)

    with session_scope(db_path) as session:
        assert session.query(RecurringPatternDB).count() == 1


def test_account_slug_is_unique(tmp_path: Path) -> None:
    """The slug is the join key every other banking table uses."""
    db_path = tmp_path / "ledger.db"
    run_migrations(db_path)
    with contextlib.closing(sqlite3.connect(db_path)) as conn, conn:
        unique_indexes = [
            row[1] for row in conn.execute("PRAGMA index_list(accounts)").fetchall() if row[2]
        ]
        covered = [
            name
            for name in unique_indexes
            if [c[2] for c in conn.execute(f"PRAGMA index_info({name})").fetchall()]
            == ["account_number"]
        ]
    assert covered, f"accounts.account_number needs a unique index; got {unique_indexes}"


@pytest.mark.parametrize("table", ["account_balances", "recurring_patterns"])
def test_derived_tables_reference_accounts(tmp_path: Path, table: str) -> None:
    db_path = tmp_path / "ledger.db"
    run_migrations(db_path)
    with contextlib.closing(sqlite3.connect(db_path)) as conn, conn:
        keys = conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
    assert any(row[2] == "accounts" for row in keys), f"{table} has no foreign key to accounts"


def test_statement_credit_limits_have_no_foreign_key(tmp_path: Path) -> None:
    """Statement-sourced rows land at finalize, before analysis creates the account."""
    db_path = tmp_path / "ledger.db"
    run_migrations(db_path)
    with contextlib.closing(sqlite3.connect(db_path)) as conn, conn:
        keys = conn.execute("PRAGMA foreign_key_list(account_statement_credit_limits)").fetchall()
    assert keys == []


def test_orphan_balance_is_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    run_migrations(db_path)
    with pytest.raises(IntegrityError), session_scope(db_path) as session:
        session.add(
            AccountBalanceDB(
                account_id="does-not-exist",
                balance_date=date(2025, 1, 1),
                balance=Decimal("1.00"),
                balance_basis="relative",
                transaction_count=1,
                balance_7d_avg=Decimal("1.00"),
                balance_30d_avg=Decimal("1.00"),
                balance_7d_min=Decimal("1.00"),
                balance_7d_max=Decimal("1.00"),
                credit_utilization=None,
                computed_at=datetime(2025, 1, 1, 12, 0),
            )
        )


def test_orphan_pattern_is_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    run_migrations(db_path)
    with pytest.raises(IntegrityError), session_scope(db_path) as session:
        session.add(_pattern("does-not-exist"))


def test_migrating_a_populated_database_preserves_its_rows(tmp_path: Path) -> None:
    """The rebuild must carry data forward, not silently empty the table."""
    db_path = tmp_path / "ledger.db"
    run_migrations(db_path)
    kept_id = account_identity("harbor", "harbor-chequing-9101")
    AccountDetector(db_path).save_accounts([_account(kept_id, "harbor-chequing-9101")])
    with session_scope(db_path) as session:
        session.add(_pattern(kept_id))

    run_migrations(db_path)

    with session_scope(db_path) as session:
        assert session.query(RecurringPatternDB).count() == 1
        assert session.query(AccountDB).count() == 1


def test_doctor_reports_a_statement_credit_limit_orphan(tmp_path: Path) -> None:
    """The credit-limit table has no foreign key, so the doctor is its only guard."""
    db_path = tmp_path / "ledger.db"
    run_migrations(db_path)
    with session_scope(db_path) as session:
        session.add(
            AccountStatementCreditLimitDB(
                account_slug="never-detected-0000",
                statement_date=date(2025, 5, 1),
                credit_limit=Decimal("5000.00"),
                updated_at=datetime(2025, 5, 1, 12, 0),
            )
        )

    finding = DatabaseDoctor(db_path)._check_account_orphans()

    assert finding is not None, "doctor must report the orphaned credit-limit row"
    assert finding.check == "account_orphans"
    assert finding.severity == "high"
    assert finding.repairable is False
    assert not any("never-detected-0000" in detail for detail in finding.details), (
        "the finding must not echo the raw account slug"
    )


def test_doctor_is_clean_on_a_consistent_database(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    run_migrations(db_path)
    kept_id = account_identity("harbor", "harbor-chequing-9101")
    AccountDetector(db_path).save_accounts([_account(kept_id, "harbor-chequing-9101")])
    with session_scope(db_path) as session:
        session.add(_pattern(kept_id))

    assert DatabaseDoctor(db_path)._check_account_orphans() is None


def test_doctor_runs_the_orphan_census_in_diagnose(tmp_path: Path) -> None:
    """A check that is written but never registered protects nothing."""
    db_path = tmp_path / "ledger.db"
    run_migrations(db_path)
    with session_scope(db_path) as session:
        session.add(
            AccountStatementCreditLimitDB(
                account_slug="never-detected-0000",
                statement_date=date(2025, 5, 1),
                credit_limit=Decimal("5000.00"),
                updated_at=datetime(2025, 5, 1, 12, 0),
            )
        )

    findings = DatabaseDoctor(db_path).diagnose()

    assert any(finding.check == "account_orphans" for finding in findings)


def test_seeded_accounts_take_their_type_from_the_transactions(tmp_path: Path) -> None:
    """A credit card seeded as chequing lets a debt metric pass for the wrong reason."""
    from account_fixtures import seed_accounts_for_existing_rows

    db_path = tmp_path / "ledger.db"
    run_migrations(db_path)
    with session_scope(db_path) as session:
        session.execute(
            text(
                "INSERT INTO transactions (txn_id, posting_date, institution, account, "
                "account_type, flow, cashflow_type, include_in_cashflow, currency, amount_raw, "
                "amount_signed, description, merchant, category, merchant_confidence, "
                "category_confidence, exclude_from_spending, merchant_raw, category_raw, "
                "source_file, source_path, source_period, source_row, imported_at, "
                "parser_version, txn_fingerprint, day_of_week, day_of_month, week_of_year, "
                "is_weekend, is_month_start, is_month_end) VALUES "
                "('t1', '2026-01-05', 'TestBank', '5678', 'credit_card', 'expense', 'expense', "
                "1, 'CAD', 10.00, -10.00, 'X', 'M', 'Other', 'high', 'high', 0, 'M', '', "
                "'f.csv', '/f.csv', '2026-01', 1, '2026-01-06 00:00:00', 'v1', 'fp', "
                "1, 5, 1, 0, 0, 0)"
            )
        )

    seed_accounts_for_existing_rows(db_path)

    with session_scope(db_path) as session:
        account_type, is_credit_card = session.query(
            AccountDB.account_type, AccountDB.is_credit_card
        ).one()
    assert account_type == "credit_card"
    assert is_credit_card is True


def test_seeded_account_keeps_its_subtype_distinct(tmp_path: Path) -> None:
    """Collapsing type and subtype forces display-cased values into account_type."""
    from account_fixtures import seed_account

    db_path = tmp_path / "ledger.db"
    run_migrations(db_path)
    seed_account(
        db_path, "harbor", "harbor-savings-9103", account_type="savings", account_subtype="Savings"
    )

    with session_scope(db_path) as session:
        account_type, subtype, is_credit_card = session.query(
            AccountDB.account_type, AccountDB.account_subtype, AccountDB.is_credit_card
        ).one()
    assert account_type == "savings"
    assert subtype == "Savings"
    assert is_credit_card is False


# --- the ids one slug's operator-authored lifecycle row can be keyed by -------


def test_the_canonical_lifecycle_id_survives_the_accounts_row_ageing_out() -> None:
    """The transition the whole `account_lifecycle` table exists for.

    `save_accounts(replace_all=True)` deletes an account the moment its last
    transaction ages out, and an account the operator marked closed is the
    likeliest to. Keying the closure on the STORED `accounts.account_id` makes
    the record unfindable at exactly that moment, so the canonical key is the
    DERIVATION, which does not depend on a row that is about to disappear.
    """
    from ledger.core.account_identity import lifecycle_account_ids

    legacy = "0" * 40
    with_row = lifecycle_account_ids(
        {"harbor-orbit-visa-9105": legacy}, "harbor-orbit-visa-9105", "harbor"
    )
    without_row = lifecycle_account_ids({}, "harbor-orbit-visa-9105", "harbor")

    assert with_row[0] == account_identity("harbor", "harbor-orbit-visa-9105")
    assert with_row[0] == without_row[0], "the canonical key moved when the accounts row went away"
    assert legacy in with_row, "a row already written under the stored id must still be found"


def test_a_coverage_slug_with_no_institution_is_still_keyable() -> None:
    """`imports/pdf/post_processing.py` writes `institution = ''` for any statement
    type the identity map does not know, and coverage rows land at PDF finalize
    before account detection has made an `accounts` row. That account can still
    raise a gap alert, so it must still be silenceable."""
    from ledger.core.account_identity import lifecycle_account_ids

    ids = lifecycle_account_ids({}, "ghost-card-0001", "")

    assert ids, "a blank institution left the account with no lifecycle key at all"
    assert ids[0] != account_identity("harbor", "ghost-card-0001")
    assert lifecycle_account_ids({}, "other-card-0002", "")[0] != ids[0], (
        "two blank-institution accounts must not collapse onto one id"
    )


def test_a_slug_that_later_gains_an_institution_still_finds_its_old_record() -> None:
    """Account detection turns a blank-institution coverage slug into a real
    account. The canonical key moves; the previously written row must still be
    found, or the closure silently stops applying."""
    from ledger.core.account_identity import lifecycle_account_ids

    before = lifecycle_account_ids({}, "ghost-card-0001", "")
    after = lifecycle_account_ids({}, "ghost-card-0001", "harbor")

    assert after[0] == account_identity("harbor", "ghost-card-0001")
    assert before[0] in after


def test_a_blank_slug_has_no_lifecycle_identity_at_all() -> None:
    """`statement_coverage.account_slug` is unconstrained TEXT. A blank one names
    no account, and minting an id for it would key every blank row together."""
    from ledger.core.account_identity import lifecycle_account_ids

    assert lifecycle_account_ids({}, "", "harbor") == ()
    assert lifecycle_account_ids({}, "   ", "") == ()


pytestmark = pytest.mark.usefixtures("fictional_adapter_metadata")
