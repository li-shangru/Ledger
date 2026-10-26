"""Canonical card identity at the privacy-safe export boundary."""

from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from account_fixtures import seed_account

from ledger.core.account_identity import account_identity
from ledger.exporting import ExportPackageService
from ledger.query.account_refs import banking_account_ref
from ledger.storage.database import (
    ConfigMetadataDB,
    ReportSnapshotDB,
    TransactionDB,
    init_database,
    session_scope,
)
from ledger.storage.operations import ACCOUNT_REF_SALT_KEY

_ACCOUNT_REF_SALT = "card-export-identity-test-salt"
_PERIOD_START = date(2026, 8, 1)
_PERIOD_END = date(2026, 8, 31)


def _database(path: Path) -> Path:
    init_database(path)
    with session_scope(path) as session:
        session.add(ConfigMetadataDB(key=ACCOUNT_REF_SALT_KEY, value=_ACCOUNT_REF_SALT))
    return path


def _insert_charge(
    db_path: Path,
    *,
    txn_id: str,
    posted: date,
    institution: str,
    account_slug: str,
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
                account=account_slug,
                account_type=account_type,
                flow="expense",
                cashflow_type="expense",
                include_in_cashflow=True,
                exclude_from_spending=False,
                currency="CAD",
                amount_raw=amount,
                amount_signed=-amount,
                description="SYNTHETIC CARD CHARGE",
                merchant="SYNTHETIC CARD MERCHANT",
                category="Shopping",
                source_file="synthetic.csv",
                source_path="/synthetic/card-export-identity.csv",
                source_period=posted.strftime("%Y-%m"),
                source_row=1,
                imported_at=datetime(2026, 9, 1, tzinfo=UTC).replace(tzinfo=None),
                parser_version="synthetic",
                txn_fingerprint=f"{txn_id}-fingerprint",
                day_of_week=posted.weekday(),
                day_of_month=posted.day,
                week_of_year=posted.isocalendar()[1],
                is_weekend=posted.weekday() >= 5,
                is_month_start=posted.day <= 3,
                is_month_end=posted.day >= 28,
            )
        )


def _share_safe_files(db_path: Path, dest_dir: Path) -> dict[str, str]:
    result = ExportPackageService(db_path).create_package(
        dest_dir=dest_dir,
        preset="chatgpt-safe",
        export_format="zip",
        start_date=_PERIOD_START,
        end_date=_PERIOD_END,
    )
    with zipfile.ZipFile(result.path) as package:
        return {name: package.read(name).decode("utf-8") for name in package.namelist()}


def _card_rows(files: dict[str, str]) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(files["card_account_summary.csv"])))


def _assert_identity_absent(files: dict[str, str], *private_values: str) -> None:
    for name, content in files.items():
        for private_value in private_values:
            assert private_value not in content, (
                f"raw card identity {private_value!r} leaked in {name}"
            )


def test_naming_changes_export_one_card_under_the_existing_account_ref(
    tmp_path: Path,
) -> None:
    """Dropping canonical account_id or regrouping display labels splits one card."""
    db_path = _database(tmp_path / "ledger.db")
    institution = "private-bank-naming-variant"
    account_slug = "private-card-naming-variant-7788"
    old_label = "Private Card Label Version One"
    new_label = "Private Card Label Version Two"
    canonical_id = seed_account(
        db_path,
        institution,
        account_slug,
        account_type="credit_card",
        is_credit_card=True,
    )
    _insert_charge(
        db_path,
        txn_id="card-name-old",
        posted=date(2026, 8, 3),
        institution=institution,
        account_slug=account_slug,
        account_type=old_label,
        amount=Decimal("12.34"),
    )
    _insert_charge(
        db_path,
        txn_id="card-name-new",
        posted=date(2026, 8, 20),
        institution=institution,
        account_slug=account_slug,
        account_type=new_label,
        amount=Decimal("17.66"),
    )

    files = _share_safe_files(db_path, tmp_path / "exports")
    rows = _card_rows(files)

    expected_ref = banking_account_ref(
        _ACCOUNT_REF_SALT,
        institution,
        account_slug,
        "credit_card",
    )
    assert rows == [
        {
            "account_ref": expected_ref,
            "account_ref_status": "resolved",
            "transaction_count": "2",
            "net": "-30.0",
            "volume": "30.0",
        }
    ]
    _assert_identity_absent(
        files,
        canonical_id,
        institution,
        account_slug,
        old_label,
        new_label,
    )


def test_same_type_cards_export_as_two_distinct_account_refs(tmp_path: Path) -> None:
    """Regrouping by institution/account_type merges two physical cards and their money."""
    db_path = _database(tmp_path / "ledger.db")
    institution = "private-bank-same-type"
    account_type = "Private Shared Card Type"
    first_slug = "private-same-type-card-1111"
    second_slug = "private-same-type-card-2222"
    first_id = seed_account(
        db_path,
        institution,
        first_slug,
        account_type="credit_card",
        is_credit_card=True,
    )
    second_id = seed_account(
        db_path,
        institution,
        second_slug,
        account_type="credit_card",
        is_credit_card=True,
    )
    _insert_charge(
        db_path,
        txn_id="same-type-first",
        posted=date(2026, 8, 4),
        institution=institution,
        account_slug=first_slug,
        account_type=account_type,
        amount=Decimal("20.00"),
    )
    _insert_charge(
        db_path,
        txn_id="same-type-second",
        posted=date(2026, 8, 5),
        institution=institution,
        account_slug=second_slug,
        account_type=account_type,
        amount=Decimal("40.00"),
    )

    files = _share_safe_files(db_path, tmp_path / "exports")
    rows = _card_rows(files)
    rows_by_ref = {row["account_ref"]: row for row in rows}
    expected = {
        banking_account_ref(_ACCOUNT_REF_SALT, institution, first_slug, "credit_card"): (
            Decimal("-20.0"),
            Decimal("20.0"),
        ),
        banking_account_ref(_ACCOUNT_REF_SALT, institution, second_slug, "credit_card"): (
            Decimal("-40.0"),
            Decimal("40.0"),
        ),
    }

    assert set(rows_by_ref) == set(expected)
    assert len(rows) == 2
    assert {row["account_ref_status"] for row in rows} == {"resolved"}
    assert {row["transaction_count"] for row in rows} == {"1"}
    for ref, (expected_net, expected_volume) in expected.items():
        assert Decimal(rows_by_ref[ref]["net"]) == expected_net
        assert Decimal(rows_by_ref[ref]["volume"]) == expected_volume

    monthly_refs = {
        row["account_ref"] for row in csv.DictReader(io.StringIO(files["monthly_trend.csv"]))
    }
    assert set(rows_by_ref) == monthly_refs
    _assert_identity_absent(
        files,
        first_id,
        second_id,
        institution,
        first_slug,
        second_slug,
        account_type,
    )


def test_unresolved_canonical_card_identity_is_opaque_and_disclosed(
    tmp_path: Path,
) -> None:
    """An account_id absent from accounts must not leak or masquerade as resolved."""
    db_path = _database(tmp_path / "ledger.db")
    institution = "private-bank-without-account-row"
    account_slug = "private-unresolved-card-9090"
    account_type = "Private Unresolved Card Type"
    canonical_id = account_identity(institution, account_slug)
    _insert_charge(
        db_path,
        txn_id="unresolved-card",
        posted=date(2026, 8, 9),
        institution=institution,
        account_slug=account_slug,
        account_type=account_type,
        amount=Decimal("30.00"),
    )

    files = _share_safe_files(db_path, tmp_path / "exports")
    rows = _card_rows(files)

    assert len(rows) == 1
    assert rows[0]["account_ref_status"] == "no_account_record"
    assert re.fullmatch(r"unresolved-[0-9a-f]{10}", rows[0]["account_ref"])
    assert Decimal(rows[0]["net"]) == Decimal("-30.0")
    assert Decimal(rows[0]["volume"]) == Decimal("30.0")
    _assert_identity_absent(
        files,
        canonical_id,
        institution,
        account_slug,
        account_type,
    )


def test_period_export_refreshes_cached_card_rows_without_canonical_identity(
    tmp_path: Path,
) -> None:
    """Reusing a pre-change schema-v2 payload would revive the unsafe legacy grouping."""
    db_path = _database(tmp_path / "ledger.db")
    institution = "private-bank-cached-payload"
    account_slug = "private-cached-card-3131"
    account_type = "Private Cached Card Label"
    canonical_id = seed_account(
        db_path,
        institution,
        account_slug,
        account_type="credit_card",
        is_credit_card=True,
    )
    _insert_charge(
        db_path,
        txn_id="fresh-card-row",
        posted=date(2026, 8, 12),
        institution=institution,
        account_slug=account_slug,
        account_type=account_type,
        amount=Decimal("20.00"),
    )
    with session_scope(db_path) as session:
        session.add(
            ReportSnapshotDB(
                created_at=datetime(2026, 9, 2, 12, 0),
                source="pre-canonical-card-cache",
                source_kind="reporting",
                schema_version=2,
                period_start=_PERIOD_START,
                period_end=_PERIOD_END,
                status="success",
                payload={
                    "schema_version": 2,
                    "account_card_summaries": [
                        {
                            "institution": institution,
                            "account_type": account_type,
                            "transaction_count": 99,
                            "net": -999.99,
                            "volume": 999.99,
                        }
                    ],
                    "category_drift": [],
                },
            )
        )

    files = _share_safe_files(db_path, tmp_path / "exports")
    rows = _card_rows(files)

    assert rows == [
        {
            "account_ref": banking_account_ref(
                _ACCOUNT_REF_SALT,
                institution,
                account_slug,
                "credit_card",
            ),
            "account_ref_status": "resolved",
            "transaction_count": "1",
            "net": "-20.0",
            "volume": "20.0",
        }
    ]
    _assert_identity_absent(
        files,
        canonical_id,
        institution,
        account_slug,
        account_type,
    )
