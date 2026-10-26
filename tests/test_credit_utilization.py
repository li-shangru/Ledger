"""Credit utilization: a real percentage, or nothing at all.

``account_balances.credit_utilization`` has never held a value -- both write paths
hardcoded NULL while the compute gate demanded an ABSOLUTE basis that no row carried.
Statement repair now anchors cards with absolute balances, so the numerator finally
exists; ``AccountDB.detected_credit_limit`` has always supplied the denominator.

Every test here pins one half of the same rule: compute it wherever a true (absolute)
card balance meets a known limit, and leave it NULL everywhere else. A guessed
utilization is worse than an absent one -- the UI prints whatever it finds.

Sign convention is Unit H's, shared by both bases: POSITIVE is money HELD, NEGATIVE is
money OWED. A card owing $1,500 stores -1500.00, so the numerator is the NEGATED
balance, never ``abs()``.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from account_fixtures import seed_account

from ledger.analysis import AnalysisRunner
from ledger.analysis.balance_calculator import (
    BalanceCalculator,
    credit_utilization_pct,
)
from ledger.core import Transaction
from ledger.core.account_identity import account_identity
from ledger.processing import ProcessingResult
from ledger.storage.database import (
    AccountBalanceDB,
    init_database,
    session_scope,
)
from ledger.storage.operations import upsert_statement_credit_limit
from ledger.storage.writer import DatabaseWriter
from ledger.utils.dates import naive_utc_now

CARD_INSTITUTION = "test-bank"
CARD_SLUG = "test-visa"
CARD_ID = account_identity(CARD_INSTITUTION, CARD_SLUG)
CHEQUING_SLUG = "test-chequing"
CHEQUING_ID = account_identity(CARD_INSTITUTION, CHEQUING_SLUG)


def _seed_balance(
    db_path: Path,
    account_id: str,
    *,
    balance_date: date,
    balance: str,
    balance_basis: str,
    credit_utilization: Decimal | None = None,
) -> None:
    value = Decimal(balance)
    with session_scope(db_path) as session:
        session.merge(
            AccountBalanceDB(
                account_id=account_id,
                balance_date=balance_date,
                balance=value,
                balance_basis=balance_basis,
                transaction_count=1,
                balance_7d_avg=value,
                balance_30d_avg=value,
                balance_7d_min=value,
                balance_7d_max=value,
                credit_utilization=credit_utilization,
                computed_at=naive_utc_now(),
            )
        )


def _stored_utilization(db_path: Path, account_id: str, balance_basis: str) -> list:
    with session_scope(db_path) as session:
        rows = (
            session.query(AccountBalanceDB)
            .filter_by(account_id=account_id, balance_basis=balance_basis)
            .order_by(AccountBalanceDB.balance_date)
            .all()
        )
        return [row.credit_utilization for row in rows]


# ---------------------------------------------------------------------------
# The rule itself
# ---------------------------------------------------------------------------


def test_absolute_card_balance_with_limit_yields_a_real_percentage() -> None:
    """$1,500 owed (stored -1500.00) against a $5,000 limit is 30%."""
    assert credit_utilization_pct(
        balance=Decimal("-1500.00"),
        balance_basis="absolute",
        is_credit_card=True,
        credit_limit=Decimal("5000"),
    ) == Decimal("30.00")


def test_relative_basis_never_yields_a_percentage() -> None:
    """The relative series is seeded at 0 on the first transaction, so its level is a
    change-since-then, not a balance. Dividing it by a limit is affirmatively wrong."""
    assert (
        credit_utilization_pct(
            balance=Decimal("-1500.00"),
            balance_basis="relative",
            is_credit_card=True,
            credit_limit=Decimal("5000"),
        )
        is None
    )


def test_non_card_account_never_yields_a_percentage() -> None:
    """A chequing overdraft is not credit utilization; there is no limit to be a
    percentage OF."""
    assert (
        credit_utilization_pct(
            balance=Decimal("-1500.00"),
            balance_basis="absolute",
            is_credit_card=False,
            credit_limit=Decimal("5000"),
        )
        is None
    )


def test_unknown_limit_yields_no_percentage() -> None:
    assert (
        credit_utilization_pct(
            balance=Decimal("-1500.00"),
            balance_basis="absolute",
            is_credit_card=True,
            credit_limit=None,
        )
        is None
    )


def test_zero_limit_yields_no_percentage() -> None:
    """A zero (or negative) limit is a detection failure, not a card state: every
    balance would be infinitely utilized. NULL says 'unknown', which is the truth."""
    assert (
        credit_utilization_pct(
            balance=Decimal("-1500.00"),
            balance_basis="absolute",
            is_credit_card=True,
            credit_limit=Decimal("0"),
        )
        is None
    )
    assert (
        credit_utilization_pct(
            balance=Decimal("-1500.00"),
            balance_basis="absolute",
            is_credit_card=True,
            credit_limit=Decimal("-5000"),
        )
        is None
    )


def test_over_limit_is_reported_above_one_hundred() -> None:
    """Over-limit is a real state -- cards go over on fees, FX, and pre-authorizations.
    Capping at 100.00 would make 'exactly at the limit' and '$1,000 over it' identical,
    hiding the single most alarming thing utilization can say."""
    assert credit_utilization_pct(
        balance=Decimal("-6000.00"),
        balance_basis="absolute",
        is_credit_card=True,
        credit_limit=Decimal("5000"),
    ) == Decimal("120.00")


def test_utilization_is_clamped_to_the_column_range() -> None:
    """credit_utilization is NUMERIC(5, 2). A figure past 999.99 cannot be stored, and
    is a data error rather than a card state; clamp like debt_ratio does instead of
    silently overflowing the column."""
    assert credit_utilization_pct(
        balance=Decimal("-10000000.00"),
        balance_basis="absolute",
        is_credit_card=True,
        credit_limit=Decimal("1000"),
    ) == Decimal("999.99")


def test_card_in_credit_is_zero_utilization_not_a_positive_percentage() -> None:
    """A positive card balance means the ISSUER owes the holder (overpayment/refund).
    Nothing is drawn on the limit, so utilization is 0.00. The old abs() would have
    reported a $250 credit as 5% utilized -- debt that does not exist."""
    assert credit_utilization_pct(
        balance=Decimal("250.00"),
        balance_basis="absolute",
        is_credit_card=True,
        credit_limit=Decimal("5000"),
    ) == Decimal("0.00")


def test_utilization_is_cent_quantized() -> None:
    """1000/3000 is 33.3333...; the column is NUMERIC(5, 2)."""
    util = credit_utilization_pct(
        balance=Decimal("-1000.00"),
        balance_basis="absolute",
        is_credit_card=True,
        credit_limit=Decimal("3000"),
    )
    assert util == Decimal("33.33")
    assert util.as_tuple().exponent == -2


# ---------------------------------------------------------------------------
# Applying the rule to stored rows
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_db(tmp_path: Path) -> Path:
    """A fictional card with a printed limit and a separate bank account."""
    db_path = tmp_path / "utilization.db"
    init_database(db_path)
    seed_account(
        db_path,
        CARD_INSTITUTION,
        CARD_SLUG,
        account_type="credit_card",
        detected_credit_limit=Decimal("5000"),
    )
    upsert_statement_credit_limit(db_path, CARD_SLUG, Decimal("5000.00"), date(2026, 4, 30))
    seed_account(db_path, CARD_INSTITUTION, CHEQUING_SLUG, account_type="chequing")
    return db_path


def test_apply_credit_utilization_populates_absolute_card_rows(seeded_db: Path) -> None:
    _seed_balance(
        seeded_db,
        CARD_ID,
        balance_date=date(2026, 5, 1),
        balance="-1000.00",
        balance_basis="absolute",
    )
    _seed_balance(
        seeded_db,
        CARD_ID,
        balance_date=date(2026, 5, 2),
        balance="-2500.00",
        balance_basis="absolute",
    )

    counts = BalanceCalculator(seeded_db).apply_credit_utilization()

    assert counts["populated"] == 2
    assert _stored_utilization(seeded_db, CARD_ID, "absolute") == [
        Decimal("20.00"),
        Decimal("50.00"),
    ]


def test_apply_credit_utilization_leaves_relative_and_deposit_rows_null(
    seeded_db: Path,
) -> None:
    _seed_balance(
        seeded_db,
        CARD_ID,
        balance_date=date(2026, 5, 1),
        balance="-1000.00",
        balance_basis="relative",
    )
    _seed_balance(
        seeded_db,
        CHEQUING_ID,
        balance_date=date(2026, 5, 1),
        balance="-1000.00",
        balance_basis="absolute",
    )

    BalanceCalculator(seeded_db).apply_credit_utilization()

    assert _stored_utilization(seeded_db, CARD_ID, "relative") == [None]
    assert _stored_utilization(seeded_db, CHEQUING_ID, "absolute") == [None]


def test_apply_credit_utilization_clears_a_stale_value(seeded_db: Path) -> None:
    """A row that no longer qualifies must lose its number. Leaving yesterday's
    percentage on a relative row is exactly the 'plausible but wrong' failure this
    column is supposed to avoid."""
    _seed_balance(
        seeded_db,
        CARD_ID,
        balance_date=date(2026, 5, 1),
        balance="-1000.00",
        balance_basis="relative",
        credit_utilization=Decimal("55.00"),
    )

    counts = BalanceCalculator(seeded_db).apply_credit_utilization()

    assert counts["cleared"] == 1
    assert _stored_utilization(seeded_db, CARD_ID, "relative") == [None]


def test_apply_credit_utilization_is_idempotent(seeded_db: Path) -> None:
    _seed_balance(
        seeded_db,
        CARD_ID,
        balance_date=date(2026, 5, 1),
        balance="-1000.00",
        balance_basis="absolute",
    )
    calculator = BalanceCalculator(seeded_db)

    first = calculator.apply_credit_utilization()
    second = calculator.apply_credit_utilization()

    assert first == {"populated": 1, "cleared": 0}
    assert second == {"populated": 0, "cleared": 0}
    assert _stored_utilization(seeded_db, CARD_ID, "absolute") == [Decimal("20.00")]


def _card_txn(i: int, txn_date: date, amount: Decimal) -> Transaction:
    return Transaction(
        txn_id=f"c{i}",
        txn_fingerprint=f"fp{i}",
        posting_date=txn_date,
        institution=CARD_INSTITUTION,
        account=CARD_SLUG,
        account_type="Visa",
        flow="expense",
        cashflow_type="expense",
        include_in_cashflow=True,
        currency="CAD",
        amount_raw=abs(amount),
        amount_signed=amount,
        description="x",
        source_file="x.csv",
        source_path="x.csv",
        source_period="2026-05",
        source_row=i,
        imported_at=date(2026, 5, 1),
        parser_version="t",
    )


def test_run_account_analysis_populates_utilization_on_existing_anchors(
    tmp_path: Path,
) -> None:
    """The anchors are written by statement repair, which runs outside the calculator.
    A normal analysis pass must pick them up rather than wait for another repair -- the
    repair path is the one place a card's absolute series appears, and it writes NULL."""
    db_path = tmp_path / "runner.db"
    init_database(db_path)
    DatabaseWriter(db_path).write_result(
        ProcessingResult(
            transactions=[_card_txn(0, date(2026, 5, 1), Decimal("-1250"))],
            transfers=[],
            exceptions=[],
            duplicates_removed=0,
            total_scanned=1,
        ),
        Path("/x"),
        "t",
    )
    upsert_statement_credit_limit(db_path, CARD_SLUG, Decimal("5000.00"), date(2026, 5, 31))
    # The accounts row is the balance rows' parent; detection would create it, but the
    # anchor has to exist BEFORE the analysis pass this test is about.
    seed_account(db_path, CARD_INSTITUTION, CARD_SLUG, account_type="Visa")
    # Stand in for statement repair's absolute anchor for that day.
    _seed_balance(
        db_path,
        CARD_ID,
        balance_date=date(2026, 5, 1),
        balance="-1250.00",
        balance_basis="absolute",
    )

    result = AnalysisRunner(db_path).run_account_analysis()

    assert result["credit_utilization_populated"] == 1
    assert _stored_utilization(db_path, CARD_ID, "absolute") == [Decimal("25.00")]
    # The relative series the same run rewrites stays NULL.
    assert set(_stored_utilization(db_path, CARD_ID, "relative")) == {None}


def test_relative_refresh_does_not_resurrect_utilization(seeded_db: Path) -> None:
    """compute_all_balances rewrites the whole relative series every run; none of those
    rows may carry a percentage no matter what the absolute rows say."""
    _seed_balance(
        seeded_db,
        CARD_ID,
        balance_date=date(2026, 5, 1),
        balance="-1250.00",
        balance_basis="absolute",
    )
    _seed_balance(
        seeded_db,
        CARD_ID,
        balance_date=date(2026, 5, 1),
        balance="-1250.00",
        balance_basis="relative",
    )

    BalanceCalculator(seeded_db).apply_credit_utilization()

    assert _stored_utilization(seeded_db, CARD_ID, "absolute") == [Decimal("25.00")]
    assert _stored_utilization(seeded_db, CARD_ID, "relative") == [None]


def test_estimated_credit_limit_does_not_produce_a_percentage(tmp_path: Path) -> None:
    """``detected_credit_limit`` is only a printed figure when a statement supplied one;
    otherwise it is AccountDetector's fallback estimate -- the deepest balance ever owed,
    rounded up to the nearest $100. Dividing by that lower bound pegs utilization near
    100% at the very moment the card was most drawn, by construction. That is a guess
    wearing a percent sign, so the denominator must be the PRINTED limit or nothing."""
    db_path = tmp_path / "estimated-limit.db"
    init_database(db_path)
    seed_account(
        db_path,
        CARD_INSTITUTION,
        CARD_SLUG,
        account_type="credit_card",
        detected_credit_limit=Decimal("1300"),  # estimate: deepest owed was ~$1,250
    )
    _seed_balance(
        db_path,
        CARD_ID,
        balance_date=date(2026, 5, 1),
        balance="-1250.00",
        balance_basis="absolute",
    )

    counts = BalanceCalculator(db_path).apply_credit_utilization()

    assert counts == {"populated": 0, "cleared": 0}
    assert _stored_utilization(db_path, CARD_ID, "absolute") == [None]


def test_printed_limit_supersedes_a_stale_account_row(tmp_path: Path) -> None:
    """The denominator is read from the statement limits table, not from the accounts
    row, so a limit increase printed on a newer statement applies immediately instead of
    waiting for account detection to refresh the dimension."""
    db_path = tmp_path / "raised-limit.db"
    init_database(db_path)
    seed_account(
        db_path,
        CARD_INSTITUTION,
        CARD_SLUG,
        account_type="credit_card",
        detected_credit_limit=Decimal("5000"),
    )
    upsert_statement_credit_limit(db_path, CARD_SLUG, Decimal("5000.00"), date(2026, 4, 30))
    upsert_statement_credit_limit(db_path, CARD_SLUG, Decimal("10000.00"), date(2026, 5, 31))
    _seed_balance(
        db_path,
        CARD_ID,
        balance_date=date(2026, 6, 1),
        balance="-2500.00",
        balance_basis="absolute",
    )

    BalanceCalculator(db_path).apply_credit_utilization()

    assert _stored_utilization(db_path, CARD_ID, "absolute") == [Decimal("25.00")]


def test_apply_credit_utilization_skips_card_without_detected_limit(tmp_path: Path) -> None:
    db_path = tmp_path / "no-limit.db"
    init_database(db_path)
    seed_account(db_path, CARD_INSTITUTION, CARD_SLUG, account_type="credit_card")
    _seed_balance(
        db_path,
        CARD_ID,
        balance_date=date(2026, 5, 1),
        balance="-1000.00",
        balance_basis="absolute",
    )

    counts = BalanceCalculator(db_path).apply_credit_utilization()

    assert counts == {"populated": 0, "cleared": 0}
    assert _stored_utilization(db_path, CARD_ID, "absolute") == [None]


def test_stale_value_is_cleared_when_no_card_has_a_usable_limit(tmp_path: Path) -> None:
    """The 'nothing qualifies' case must still CLEAR. It is the one that exercises an
    empty exclusion set in the reconciliation query, and getting it backwards would
    preserve every stale percentage in the database instead of removing it."""
    db_path = tmp_path / "no-limits-at-all.db"
    init_database(db_path)
    seed_account(db_path, CARD_INSTITUTION, CARD_SLUG, account_type="credit_card")
    _seed_balance(
        db_path,
        CARD_ID,
        balance_date=date(2026, 5, 1),
        balance="-2000.00",
        balance_basis="absolute",
        credit_utilization=Decimal("40.00"),
    )

    counts = BalanceCalculator(db_path).apply_credit_utilization()

    assert counts == {"populated": 0, "cleared": 1}
    assert _stored_utilization(db_path, CARD_ID, "absolute") == [None]


def test_over_limit_percentage_is_stored_uncapped(seeded_db: Path) -> None:
    """End-to-end companion to the unit test: the >100 value survives the DB round trip
    (NUMERIC(5, 2) holds three integer digits)."""
    _seed_balance(
        seeded_db,
        CARD_ID,
        balance_date=date(2026, 5, 1),
        balance="-5750.00",
        balance_basis="absolute",
    )

    BalanceCalculator(seeded_db).apply_credit_utilization()

    assert _stored_utilization(seeded_db, CARD_ID, "absolute") == [Decimal("115.00")]


def test_inactive_card_still_gets_utilization(seeded_db: Path) -> None:
    """A closed card still carries a balance history worth reading; utilization is a
    property of the row, not of the account's activity flag."""
    with session_scope(seeded_db) as session:
        from ledger.storage.database import AccountDB

        card = session.get(AccountDB, CARD_ID)
        assert card is not None
        card.is_active = False
    _seed_balance(
        seeded_db,
        CARD_ID,
        balance_date=date(2026, 5, 1),
        balance="-1000.00",
        balance_basis="absolute",
    )

    BalanceCalculator(seeded_db).apply_credit_utilization()

    assert _stored_utilization(seeded_db, CARD_ID, "absolute") == [Decimal("20.00")]


def test_utilization_tracks_the_day_not_the_latest_balance(seeded_db: Path) -> None:
    """Each day's row gets ITS OWN day's percentage -- the series is what makes a
    utilization trend readable."""
    start = date(2026, 5, 1)
    for offset, amount in enumerate(["-500.00", "-1500.00", "-4000.00", "0.00"]):
        _seed_balance(
            seeded_db,
            CARD_ID,
            balance_date=start + timedelta(days=offset),
            balance=amount,
            balance_basis="absolute",
        )

    BalanceCalculator(seeded_db).apply_credit_utilization()

    assert _stored_utilization(seeded_db, CARD_ID, "absolute") == [
        Decimal("10.00"),
        Decimal("30.00"),
        Decimal("80.00"),
        Decimal("0.00"),
    ]


def test_utilization_uses_the_limit_in_force_on_each_day(tmp_path: Path) -> None:
    """Utilization uses the limit in force on each day."""
    from ledger.storage.operations import credit_limit_in_force

    history = [
        (date(2025, 3, 31), Decimal("2500.00")),
        (date(2025, 9, 30), Decimal("5000.00")),
    ]

    # A statement states the limit for the period it COVERS, so a day inside the
    # first cycle takes the first statement's figure.
    assert credit_limit_in_force(history, date(2025, 2, 1)) == Decimal("2500.00")
    assert credit_limit_in_force(history, date(2025, 3, 31)) == Decimal("2500.00")
    assert credit_limit_in_force(history, date(2025, 4, 1)) == Decimal("5000.00")
    # Past the last statement the most recent figure carries forward.
    assert credit_limit_in_force(history, date(2026, 1, 1)) == Decimal("5000.00")
    # With no statements the limit is unknown, and unknown stays unknown.
    assert credit_limit_in_force([], date(2025, 4, 1)) is None


def test_the_same_balance_yields_different_utilization_across_a_limit_change(
    tmp_path: Path,
) -> None:
    from ledger.storage.operations import credit_limit_in_force

    history = [
        (date(2025, 3, 31), Decimal("2500.00")),
        (date(2025, 9, 30), Decimal("5000.00")),
    ]
    owed = Decimal("765.00")

    early = owed / credit_limit_in_force(history, date(2025, 2, 26))
    late = owed / credit_limit_in_force(history, date(2025, 12, 26))

    assert early == owed / Decimal("2500.00")
    assert late == owed / Decimal("5000.00")
    assert early == late * 2, "the earlier reading is exactly double, not equal"
