"""Coverage for the transfer query module: matched pairs, unmatched, stats."""

from datetime import UTC, date, datetime
from decimal import Decimal

from ledger.query import LedgerQueryService
from ledger.storage.database import TransferDB, init_database, session_scope


def _add_transfer(session, txn_id, amount_raw, account_type, paired=None):
    session.add(
        TransferDB(
            txn_id=txn_id,
            posting_date=date(2025, 6, 18),
            institution="harbor",
            account="acct",
            account_type=account_type,
            flow="transfer",
            amount_raw=Decimal(amount_raw),
            description="X",
            source_file="s.csv",
            imported_at=datetime.now(UTC).replace(tzinfo=None),
            paired_transfer_id=paired,
        )
    )


def test_transfer_matches_unmatched_and_stats(tmp_path):
    db_path = tmp_path / "transfers.db"
    init_database(db_path)
    with session_scope(db_path) as session:
        _add_transfer(session, "t1", "-500.00", "Chequing", paired="t2")
        _add_transfer(session, "t2", "500.00", "SPARK Visa", paired="t1")
        _add_transfer(session, "t3", "-100.00", "Chequing", paired=None)

    service = LedgerQueryService(db_path)

    pairs = service.get_transfer_matches()
    assert len(pairs) == 1
    outflow, inflow = pairs[0]
    assert outflow.txn_id == "t1" and outflow.amount_signed < 0  # negative leg first
    assert inflow.txn_id == "t2"

    assert [t.txn_id for t in service.get_unmatched_transfers()] == ["t3"]

    stats = service.get_transfer_stats()
    assert stats["total_transfers"] == 3
    assert stats["matched_pairs"] == 1
    assert stats["matched_transfers"] == 2
    assert stats["unmatched_transfers"] == 1
    assert abs(stats["match_rate"] - 2 / 3) < 1e-9


def test_minimal_transfer_preserves_currency():
    """M5c: minimal_transfer must carry the row's native currency onto the rebuilt
    Transaction, not hardcode CAD.

    On the refresh_transfer_matches rebuild path each TransferDB row is projected to a
    minimal Transaction fed to the (now currency-aware) matcher. Forcing currency="CAD"
    made a USD leg read as CAD, which -- after the M5a currency guard -- would wrongly
    let a USD leg match a genuine CAD leg (and vice versa). The currency must round-trip.
    """
    from ledger.imports.pdf.post_processing import minimal_transfer

    row = TransferDB(
        txn_id="t_usd",
        posting_date=date(2025, 6, 18),
        institution="harbor",
        account="acct",
        account_type="Chequing",
        flow="transfer",
        amount_raw=Decimal("-500.00"),
        currency="USD",
        description="Online transfer",
        source_file="s.csv",
        imported_at=datetime.now(UTC).replace(tzinfo=None),
    )

    txn = minimal_transfer(row)

    assert txn.currency == "USD"


def test_minimal_transfer_falls_back_to_cad_when_currency_absent():
    """M5c companion: minimal_transfer must tolerate a row without a populated currency
    (e.g. a bare stub) and fall back to CAD rather than raising."""
    from types import SimpleNamespace

    from ledger.imports.pdf.post_processing import minimal_transfer

    row = SimpleNamespace(
        txn_id="t_stub",
        posting_date=date(2025, 6, 18),
        institution="harbor",
        account="acct",
        account_type="Chequing",
        flow="transfer",
        amount_raw=Decimal("-500.00"),
        currency=None,
        description="Online transfer",
        source_file="s.csv",
        imported_at=datetime.now(UTC).replace(tzinfo=None),
    )

    txn = minimal_transfer(row)  # type: ignore[arg-type]

    assert txn.currency == "CAD"
