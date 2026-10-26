"""Canonical card identity regressions for the financial workflow."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from ledger.core.account_identity import account_identity
from ledger.reporting import ReportSnapshotService
from ledger.storage.database import (
    AccountDB,
    ReportSnapshotDB,
    TransactionDB,
    init_database,
    session_scope,
)
from ledger.ui.pages.accounts import _logical_account_groups

PERIOD_START = date(2026, 8, 1)
PERIOD_END = date(2026, 8, 31)


def _insert_charge(
    db_path: Path,
    *,
    txn_id: str,
    posted: date,
    institution: str,
    account: str,
    account_type: str,
    amount: Decimal,
) -> None:
    with session_scope(db_path) as session:
        session.add(
            TransactionDB(
                txn_id=txn_id,
                posting_date=posted,
                transaction_date=posted,
                institution=institution,
                account=account,
                account_type=account_type,
                flow="expense",
                cashflow_type="expense",
                include_in_cashflow=True,
                currency="CAD",
                amount_raw=abs(amount),
                amount_signed=amount,
                description=f"CARD PURCHASE {txn_id}",
                merchant="CARD PURCHASE",
                category="Shopping",
                exclude_from_spending=False,
                source_file="synthetic.csv",
                source_path="/synthetic/card-identity.csv",
                source_period=posted.strftime("%Y-%m"),
                source_row=int(txn_id.rsplit("-", maxsplit=1)[-1]),
                imported_at=datetime.now(UTC).replace(tzinfo=None),
                parser_version="test",
                txn_fingerprint=f"{txn_id}-fingerprint",
                day_of_week=posted.weekday(),
                day_of_month=posted.day,
                week_of_year=posted.isocalendar()[1],
                is_weekend=posted.weekday() >= 5,
                is_month_start=posted.day <= 3,
                is_month_end=posted.day >= 28,
            )
        )


def _payload(db_path: Path) -> dict:
    return ReportSnapshotService(db_path).snapshot_for_period(
        PERIOD_START,
        PERIOD_END,
        persist=False,
    )["payload"]


def _insert_account(
    db_path: Path,
    *,
    institution: str,
    account: str,
    account_type: str,
    is_credit_card: bool,
) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    with session_scope(db_path) as session:
        session.add(
            AccountDB(
                account_id=account_identity(institution, account),
                institution=institution,
                account_number=account,
                account_type=account_type,
                account_subtype=account_type,
                first_transaction_date=PERIOD_START,
                last_transaction_date=PERIOD_END,
                is_active=True,
                transaction_count=1,
                avg_monthly_transactions=Decimal("1.00"),
                is_credit_card=is_credit_card,
                detected_credit_limit=None,
                created_at=now,
                updated_at=now,
            )
        )


def _money(value: object) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def test_card_type_history_stays_one_canonical_summary_with_exact_gross_spend(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    slug = "harbor-orbit-visa-1001"
    expected_id = account_identity("harbor", slug)
    _insert_charge(
        db_path,
        txn_id="card-history-1",
        posted=date(2026, 8, 4),
        institution="harbor",
        account=slug,
        account_type="Harbor Orbit Visa Infinite",
        amount=Decimal("-19.99"),
    )
    _insert_charge(
        db_path,
        txn_id="card-history-2",
        posted=date(2026, 8, 20),
        institution="harbor",
        account=slug,
        account_type="credit_card",
        amount=Decimal("-30.01"),
    )

    payload = _payload(db_path)
    summaries = payload["account_card_summaries"]
    optimization = payload["credit_card_optimization"]

    assert len(summaries) == 1
    summary = summaries[0]
    assert summary["account_id"] == expected_id
    assert summary["account_type"] == "credit_card"
    assert summary["transaction_count"] == 2
    assert _money(summary["net"]) == Decimal("-50.00")
    assert _money(summary["volume"]) == Decimal("50.00")
    assert optimization["card_count"] == 1
    assert _money(optimization["total_spend"]) == Decimal("50.00")
    assert _money(optimization["total_net"]) == Decimal("-50.00")


def test_same_type_cards_at_one_institution_remain_separate_with_exact_gross_spend(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    first_slug = "harbor-orbit-visa-1001"
    second_slug = "harbor-orbit-visa-2002"
    first_id = account_identity("harbor", first_slug)
    second_id = account_identity("harbor", second_slug)
    _insert_charge(
        db_path,
        txn_id="same-type-1",
        posted=date(2026, 8, 5),
        institution="harbor",
        account=first_slug,
        account_type="credit_card",
        amount=Decimal("-10.10"),
    )
    _insert_charge(
        db_path,
        txn_id="same-type-2",
        posted=date(2026, 8, 6),
        institution="harbor",
        account=second_slug,
        account_type="credit_card",
        amount=Decimal("-20.20"),
    )

    payload = _payload(db_path)
    summaries = {row["account_id"]: row for row in payload["account_card_summaries"]}
    optimization = payload["credit_card_optimization"]

    assert set(summaries) == {first_id, second_id}
    assert _money(summaries[first_id]["volume"]) == Decimal("10.10")
    assert _money(summaries[second_id]["volume"]) == Decimal("20.20")
    assert optimization["card_count"] == 2
    assert {row["account_id"] for row in optimization["cards"]} == {
        first_id,
        second_id,
    }
    assert _money(optimization["total_spend"]) == Decimal("30.30")
    assert _money(optimization["total_net"]) == Decimal("-30.30")


def test_card_summary_uses_canonical_account_classification_not_mutable_display_label(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    slug = "harbor-orbit-visa-1001"
    _insert_account(
        db_path,
        institution="harbor",
        account=slug,
        account_type="Everyday Rewards",
        is_credit_card=True,
    )
    _insert_charge(
        db_path,
        txn_id="canonical-card-1",
        posted=date(2026, 8, 20),
        institution="harbor",
        account=slug,
        account_type="Everyday Rewards",
        amount=Decimal("-12.34"),
    )

    optimization = _payload(db_path)["credit_card_optimization"]

    assert optimization["card_count"] == 1
    assert _money(optimization["total_spend"]) == Decimal("12.34")


def test_non_card_account_is_not_promoted_by_cardish_free_text_label(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    slug = "harbor-line-of-credit-1001"
    _insert_account(
        db_path,
        institution="harbor",
        account=slug,
        account_type="Visa-like label",
        is_credit_card=False,
    )
    _insert_charge(
        db_path,
        txn_id="canonical-noncard-1",
        posted=date(2026, 8, 20),
        institution="harbor",
        account=slug,
        account_type="Visa-like label",
        amount=Decimal("-12.34"),
    )

    optimization = _payload(db_path)["credit_card_optimization"]

    assert optimization["card_count"] == 0
    assert _money(optimization["total_spend"]) == Decimal("0.00")


def test_legacy_v2_card_cache_is_recomputed_with_canonical_identity(tmp_path: Path) -> None:
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    slug = "harbor-orbit-visa-1001"
    canonical_id = account_identity("harbor", slug)
    _insert_account(
        db_path,
        institution="harbor",
        account=slug,
        account_type="credit_card",
        is_credit_card=True,
    )
    _insert_charge(
        db_path,
        txn_id="legacy-cache-1",
        posted=date(2026, 8, 20),
        institution="harbor",
        account=slug,
        account_type="Harbor Orbit Visa Infinite",
        amount=Decimal("-12.34"),
    )
    with session_scope(db_path) as session:
        session.add(
            ReportSnapshotDB(
                created_at=datetime(2099, 1, 1),
                source="legacy-v2",
                source_kind="reporting",
                schema_version=2,
                period_start=PERIOD_START,
                period_end=PERIOD_END,
                status="success",
                payload={
                    "schema_version": 2,
                    "account_card_summaries": [
                        {
                            "institution": "harbor",
                            "account_type": "Harbor Orbit Visa Infinite",
                            "transaction_count": 99,
                            "net": -999.99,
                            "volume": 999.99,
                        }
                    ],
                    "credit_card_optimization": {
                        "cards": [],
                        "card_count": 0,
                        "total_net": 0.0,
                        "total_spend": 0.0,
                    },
                },
            )
        )

    snapshot = ReportSnapshotService(db_path).snapshot_for_period(PERIOD_START, PERIOD_END)

    assert snapshot["id"] is None
    assert snapshot["schema_version"] == 3
    assert snapshot["payload"]["schema_version"] == 3
    assert snapshot["payload"]["account_card_summaries"] == [
        {
            "account_id": canonical_id,
            "institution": "harbor",
            "account_type": "Harbor Orbit Visa Infinite",
            "transaction_count": 1,
            "net": -12.34,
            "volume": 12.34,
        }
    ]


def _account_row(slug: str, *, last_date: date) -> dict:
    return {
        "account_id": account_identity("harbor", slug),
        "institution": "harbor",
        "account_number": slug,
        "account_type": "credit_card",
        "account_subtype": "Harbor Orbit Visa Infinite",
        "first_transaction_date": date(2026, 1, 1),
        "last_transaction_date": last_date,
        "transaction_count": 5,
        "avg_monthly_transactions": Decimal("1.00"),
        "is_active": True,
        "is_credit_card": True,
        "detected_credit_limit": Decimal("1000.00"),
    }


def test_accounts_page_never_links_distinct_card_slugs() -> None:
    first_slug = "harbor-orbit-visa-1001"
    second_slug = "harbor-orbit-visa-2002"

    grouped = _logical_account_groups(
        [
            _account_row(first_slug, last_date=date(2026, 8, 5)),
            _account_row(second_slug, last_date=date(2026, 8, 6)),
        ]
    )

    assert {row["account_id"] for row in grouped} == {
        account_identity("harbor", first_slug),
        account_identity("harbor", second_slug),
    }
    assert {tuple(row["account_numbers"]) for row in grouped} == {
        (first_slug,),
        (second_slug,),
    }
