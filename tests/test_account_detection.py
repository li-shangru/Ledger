"""Account / credit-card detection heuristics."""

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from ledger.analysis.account_detector import AccountDetector
from ledger.analysis.runner import AnalysisRunner
from ledger.core import Transaction
from ledger.processing import ProcessingResult
from ledger.storage.database import (
    AccountBalanceDB,
    AccountDB,
    TransferDB,
    init_database,
    session_scope,
)
from ledger.storage.writer import DatabaseWriter


def _txn(
    amount_signed: Decimal,
    account_type: str = "SPARK Visa",
    flow: str = "expense",
    d: date = date(2025, 1, 5),
) -> Transaction:
    return Transaction(
        txn_id=f"t_{amount_signed}_{d}_{flow}",
        txn_fingerprint=f"fp_{amount_signed}_{d}_{flow}",
        posting_date=d,
        institution="harbor",
        account="acct",
        account_type=account_type,
        flow=flow,
        cashflow_type="income" if flow == "income" else "expense",
        include_in_cashflow=True,
        currency="CAD",
        amount_raw=abs(amount_signed),
        amount_signed=amount_signed,
        description="x",
        source_file="x.csv",
        source_path="x.csv",
        source_period="2025-01",
        source_row=0,
        imported_at=date(2025, 1, 1),
        parser_version="t",
    )


def test_chequing_not_flagged_as_credit_card(tmp_path):
    """A net-negative chequing slice with mostly expenses must NOT be a credit card."""
    detector = AccountDetector(tmp_path / "x.db")
    txns = [_txn(Decimal("-100"), "Chequing") for _ in range(8)]
    txns.append(_txn(Decimal("50"), "Chequing", "income"))

    assert detector._is_credit_card("Chequing", txns) is False


def test_credit_limit_folds_in_payments(tmp_path):
    """The limit estimate must reflect the real (oscillating) card balance, not the
    monotonic cumulative-purchase total that ignores payments."""
    detector = AccountDetector(tmp_path / "x.db")
    txns = [
        _txn(Decimal("-1000"), d=date(2025, 1, 5)),
        _txn(Decimal("-1000"), d=date(2025, 2, 5)),
    ]
    payments = [
        {"posting_date": date(2025, 1, 20), "amount_raw": Decimal("1000")},
        {"posting_date": date(2025, 2, 20), "amount_raw": Decimal("1000")},
    ]

    # Purchases-only (old behaviour) overstates: -1000 then -2000 -> $2000.
    assert detector._detect_credit_limit(txns) == Decimal("2000")
    # Folding payments in: balance oscillates 0..-1000 -> realistic $1000.
    assert detector._detect_credit_limit(txns, payments) == Decimal("1000")


def test_update_account_stats_runs_and_folds_payments(tmp_path):
    """update_account_stats must not raise (regression: it read an expired AccountDB
    after the session closed) and must fold the card's transfer/payment legs into the
    credit-limit balance."""
    db = tmp_path / "acct.db"
    init_database(db)

    def _purchase(i: int, d: date) -> Transaction:
        return Transaction(
            txn_id=f"p{i}",
            txn_fingerprint=f"fp{i}",
            posting_date=d,
            institution="harbor",
            account="card-1",
            account_type="visa",
            flow="expense",
            cashflow_type="expense",
            include_in_cashflow=True,
            currency="CAD",
            amount_raw=Decimal("1000"),
            amount_signed=Decimal("-1000"),
            description="x",
            source_file="x.csv",
            source_path="x.csv",
            source_period="2026-01",
            source_row=i,
            imported_at=date(2026, 1, 1),
            parser_version="t",
        )

    purchases = [_purchase(0, date(2026, 1, 5)), _purchase(1, date(2026, 2, 5))]
    DatabaseWriter(db).write_result(
        ProcessingResult(
            transactions=purchases,
            transfers=[],
            exceptions=[],
            duplicates_removed=0,
            total_scanned=2,
        ),
        Path("/x"),
        "t",
    )
    det = AccountDetector(db)
    det.save_accounts(det.detect_accounts(purchases))
    with session_scope(db) as s:
        account_id = s.query(AccountDB).first().account_id
        s.add(
            TransferDB(
                txn_id="pay1",
                posting_date=date(2026, 1, 20),
                institution="harbor",
                account="card-1",
                account_type="visa",
                flow="transfer",
                amount_raw=Decimal("1000"),
                description="payment",
                source_file="x.csv",
                imported_at=datetime(2026, 1, 1),
            )
        )

    account = det.update_account_stats(account_id)

    assert account is not None  # did not raise DetachedInstanceError
    # purchases -1000 / -1000 with a +1000 payment between -> deepest owed 1000.
    assert account.detected_credit_limit == Decimal("1000")


def _card_purchase(i: int, d: date, amt: Decimal) -> Transaction:
    return Transaction(
        txn_id=f"cp{i}",
        txn_fingerprint=f"cfp{i}",
        posting_date=d,
        institution="harbor",
        account="card-1",
        account_type="visa",
        flow="expense",
        cashflow_type="expense",
        include_in_cashflow=True,
        currency="CAD",
        amount_raw=abs(amt),
        amount_signed=amt,
        description="x",
        source_file="x.csv",
        source_path="x.csv",
        source_period="2026-01",
        source_row=i,
        imported_at=date(2026, 1, 1),
        parser_version="t",
    )


def test_run_account_analysis_folds_payments_into_detected_limit(tmp_path):
    """The production analysis path must persist a leg-aware credit limit.

    Six $1000 purchases sum to $6000 lifetime, but $1000 payments after each of the
    first five keep the balance owed oscillating 0..-$1000. Purchases-only detection
    overstates the limit at $6000; folding the payment legs in yields a realistic $1000.
    """
    db = tmp_path / "runner.db"
    init_database(db)

    # Purchase on the 5th of each month; pay it off on the 20th (except the last).
    purchases = [
        _card_purchase(0, date(2026, 1, 5), Decimal("-1000")),
        _card_purchase(1, date(2026, 2, 5), Decimal("-1000")),
        _card_purchase(2, date(2026, 3, 5), Decimal("-1000")),
        _card_purchase(3, date(2026, 4, 5), Decimal("-1000")),
        _card_purchase(4, date(2026, 5, 5), Decimal("-1000")),
        _card_purchase(5, date(2026, 6, 5), Decimal("-1000")),
    ]
    DatabaseWriter(db).write_result(
        ProcessingResult(
            transactions=purchases,
            transfers=[],
            exceptions=[],
            duplicates_removed=0,
            total_scanned=len(purchases),
        ),
        Path("/x"),
        "t",
    )
    # Payment legs (transfers table) pay down the card after the first five purchases.
    with session_scope(db) as s:
        for i, pay_date in enumerate(
            [
                date(2026, 1, 20),
                date(2026, 2, 20),
                date(2026, 3, 20),
                date(2026, 4, 20),
                date(2026, 5, 20),
            ]
        ):
            s.add(
                TransferDB(
                    txn_id=f"pay{i}",
                    posting_date=pay_date,
                    institution="harbor",
                    account="card-1",
                    account_type="visa",
                    flow="transfer",
                    amount_raw=Decimal("1000"),
                    description="payment",
                    source_file="x.csv",
                    imported_at=datetime(2026, 1, 1),
                )
            )

    AnalysisRunner(db).run_account_analysis()

    with session_scope(db) as s:
        acct = s.query(AccountDB).filter_by(account_number="card-1").first()
        assert acct.is_credit_card is True
        # Leg-aware: deepest balance owed is $1000, NOT the $6000 lifetime purchase sum.
        assert acct.detected_credit_limit == Decimal("1000")

        # The calculator row is a relative change series, so even with a corrected
        # limit it must not be presented as an absolute utilization percentage.
        last_balance = (
            s.query(AccountBalanceDB)
            .filter_by(account_id=acct.account_id)
            .order_by(AccountBalanceDB.balance_date.desc())
            .first()
        )
        assert last_balance.balance_basis == "relative"
        assert last_balance.credit_utilization is None


def test_run_account_analysis_no_payment_legs(tmp_path):
    """Leg-aware runner path is correct (and doesn't break) when there are zero payment
    legs in TransferDB for the card.

    Three $1000 purchases with no payments -> running balance falls to -$3000.
    The purchases-only cumulative peak IS the correct limit in this case, so the
    leg-aware re-run must yield the same $3000, not raise or return None.
    """
    db = tmp_path / "nopay.db"
    init_database(db)

    purchases = [
        _card_purchase(0, date(2026, 1, 5), Decimal("-1000")),
        _card_purchase(1, date(2026, 2, 5), Decimal("-1000")),
        _card_purchase(2, date(2026, 3, 5), Decimal("-1000")),
    ]
    DatabaseWriter(db).write_result(
        ProcessingResult(
            transactions=purchases,
            transfers=[],
            exceptions=[],
            duplicates_removed=0,
            total_scanned=len(purchases),
        ),
        Path("/x"),
        "t",
    )
    # No TransferDB rows inserted -- card has no payment legs at all.

    AnalysisRunner(db).run_account_analysis()

    with session_scope(db) as s:
        acct = s.query(AccountDB).filter_by(account_number="card-1").first()
        assert acct.is_credit_card is True
        # No payments -> balance falls monotonically to -$3000; limit = $3000.
        assert acct.detected_credit_limit == Decimal("3000")


def test_run_account_analysis_does_not_flag_chequing_as_card(tmp_path):
    """Regression: a deposit/chequing account is not flagged as a credit card and its
    stats stay sane (no spurious credit limit) through the production path."""
    db = tmp_path / "deposit.db"
    init_database(db)

    def _chq(i: int, d: date, amt: Decimal) -> Transaction:
        return Transaction(
            txn_id=f"c{i}",
            txn_fingerprint=f"cf{i}",
            posting_date=d,
            institution="harbor",
            account="chq-1",
            account_type="Chequing",
            flow="income" if amt > 0 else "expense",
            cashflow_type="income" if amt > 0 else "expense",
            include_in_cashflow=True,
            currency="CAD",
            amount_raw=abs(amt),
            amount_signed=amt,
            description="x",
            source_file="x.csv",
            source_path="x.csv",
            source_period="2026-01",
            source_row=i,
            imported_at=date(2026, 1, 1),
            parser_version="t",
        )

    txns = [_chq(i, date(2026, 1, 5 + i), Decimal("-100")) for i in range(8)]
    txns.append(_chq(99, date(2026, 1, 14), Decimal("2000")))
    DatabaseWriter(db).write_result(
        ProcessingResult(
            transactions=txns,
            transfers=[],
            exceptions=[],
            duplicates_removed=0,
            total_scanned=len(txns),
        ),
        Path("/x"),
        "t",
    )

    AnalysisRunner(db).run_account_analysis()

    with session_scope(db) as s:
        acct = s.query(AccountDB).filter_by(account_number="chq-1").first()
        assert acct is not None
        assert acct.is_credit_card is False
        assert acct.detected_credit_limit is None
        assert acct.account_type == "checking"
        assert acct.transaction_count == 9
