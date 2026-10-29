"""Tests for privacy-safe export packages."""

import csv
import io
import json
import re
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from account_fixtures import seed_account

from ledger.exporting import ExportPackageService
from ledger.storage.database import (
    AccountDB,
    ExportRunDB,
    RecurringPatternDB,
    TransactionDB,
    init_database,
    session_scope,
)


def _detailed_rows(path: Path, export_format: str) -> list[dict]:
    if export_format == "zip":
        with zipfile.ZipFile(path) as archive:
            return [json.loads(line) for line in archive.read("transactions.jsonl").splitlines()]
    if export_format == "jsonl":
        return [json.loads(line) for line in path.read_text().splitlines()]
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


@pytest.mark.parametrize("export_format", ["csv", "jsonl", "zip"])
def test_detailed_export_uses_canonical_product_labels_without_changing_facts(
    tmp_path: Path, export_format: str
) -> None:
    db = tmp_path / "ledger.db"
    init_database(db)
    variants = ("Harbor Orbit Visa", "Harbor Orbit Visaj", "Harbor Orbit Visa Terms And Conditions")
    for index, label in enumerate(variants):
        _insert_transaction(
            db,
            f"orbit-{index}",
            account="fictional-orbit",
            account_type=label,
            amount=Decimal("12.34"),
            currency="EUR",
        )
    _insert_transaction(db, "summit-0", account="fictional-summit", account_type="Parser Suffixj")
    with session_scope(db) as session:
        for account in session.query(AccountDB).all():
            account.account_type = "credit_card"
            account.account_subtype = (
                "Harbor Orbit Visa"
                if account.account_number == "fictional-orbit"
                else "Harbor Summit Visa"
            )
        before = [
            (row.txn_id, row.account_type, row.amount_signed)
            for row in session.query(TransactionDB).all()
        ]

    result = ExportPackageService(db).create_package(
        dest_dir=tmp_path / "exports", preset="full-local-audit", export_format=export_format
    )
    rows = _detailed_rows(result.path, export_format)
    orbit = [row for row in rows if row["txn_id"].startswith("orbit-")]
    summit = next(row for row in rows if row["txn_id"] == "summit-0")

    assert result.rows_exported == len(rows) == 4
    assert {row["account_type"] for row in orbit} == {"Harbor Orbit Visa"}
    assert summit["account_type"] == "Harbor Summit Visa"
    assert len({row["account_ref"] for row in orbit}) == 1
    assert summit["account_ref"] != orbit[0]["account_ref"]
    assert all(row["account_ref_status"] == "resolved" for row in rows)
    assert all(Decimal(str(row["amount"])) == Decimal("-12.34") for row in orbit)
    assert {row["currency"] for row in orbit} == {"EUR"}
    assert {row["date"] for row in rows} == {"2026-05-01"}
    with session_scope(db) as session:
        after = [
            (row.txn_id, row.account_type, row.amount_signed)
            for row in session.query(TransactionDB).all()
        ]
        assert after == before
        assert {row.account_subtype for row in session.query(AccountDB).all()} == {
            "Harbor Orbit Visa",
            "Harbor Summit Visa",
        }


@pytest.mark.parametrize("subtype", ["", "   "])
def test_export_missing_product_label_falls_back_to_canonical_classification(
    tmp_path: Path, subtype: str
) -> None:
    db = tmp_path / "ledger.db"
    init_database(db)
    _insert_transaction(db, account_type="Unreliable Productj")
    with session_scope(db) as session:
        account = session.query(AccountDB).one()
        account.account_type = "credit_card"
        account.account_subtype = subtype
    result = ExportPackageService(db).create_package(
        dest_dir=tmp_path / "exports", preset="full-local-audit", export_format="jsonl"
    )
    assert _detailed_rows(result.path, "jsonl")[0]["account_type"] == "credit_card"


def test_export_without_account_metadata_does_not_guess_a_product_label(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    init_database(db)
    _insert_transaction(db, "orphan-1", account_type="Unreliable Productj")
    _insert_transaction(db, "orphan-2", account_type="Unreliable Product Terms")
    with session_scope(db) as session:
        session.query(AccountDB).delete()
    result = ExportPackageService(db).create_package(
        dest_dir=tmp_path / "exports", preset="full-local-audit", export_format="jsonl"
    )
    rows = _detailed_rows(result.path, "jsonl")
    assert {row["account_type"] for row in rows} == {""}
    assert {row["account_ref_status"] for row in rows} == {"no_account_record"}
    assert len({row["account_ref"] for row in rows}) == 1


def test_reused_export_service_refreshes_canonical_account_metadata(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    init_database(db)
    _insert_transaction(db, account_type="Original Product")
    service = ExportPackageService(db)
    first = service.create_package(
        dest_dir=tmp_path / "first", preset="full-local-audit", export_format="jsonl"
    )
    before = _detailed_rows(first.path, "jsonl")[0]
    with session_scope(db) as session:
        account = session.query(AccountDB).one()
        account.account_subtype = "Current Product"
    second = service.create_package(
        dest_dir=tmp_path / "second", preset="full-local-audit", export_format="jsonl"
    )
    after = _detailed_rows(second.path, "jsonl")[0]
    assert before["account_type"] == "Original Product"
    assert after == {**before, "account_type": "Current Product"}


# Distinctive PII used across redaction assertions. These strings must never
# appear in any chatgpt-safe artifact.
SECRET_MERCHANT = "ZZQ DISTINCTIVE MERCHANT 9X7"
SECRET_INSTITUTION = "ZZQ-INSTITUTION-7Q9"
SECRET_ACCOUNT = "ZZQ-ACCOUNT-4M2"
SECRET_ACCOUNT_TYPE = "ZZQ-CARD-TYPE-8K3"


def _temporal_fields(posting_date: date) -> dict:
    return {
        "day_of_week": posting_date.weekday(),
        "day_of_month": posting_date.day,
        "week_of_year": posting_date.isocalendar()[1],
        "is_weekend": posting_date.weekday() >= 5,
        "is_month_start": posting_date.day <= 3,
        "is_month_end": posting_date.day >= 28,
    }


def _insert_transaction(
    db_path: Path,
    txn_id: str = "txn001",
    posted: date | None = None,
    *,
    merchant: str = "COFFEE SHOP",
    amount: Decimal = Decimal("25.00"),
    account: str = "4111111111111111",
    institution: str = "harbor",
    account_type: str = "visa",
    category: str = "Food & Dining",
    currency: str = "CAD",
    include_in_cashflow: bool = True,
    exclude_from_spending: bool = False,
    flow: str = "expense",
) -> None:
    posted = posted or date(2026, 5, 1)
    # Income rows carry a positive signed amount; expenses are negative.
    amount_signed = amount if flow == "income" else -amount
    cashflow_type = "income" if flow == "income" else "expense"
    with session_scope(db_path) as session:
        session.add(
            TransactionDB(
                txn_id=txn_id,
                posting_date=posted,
                transaction_date=posted,
                institution=institution,
                account=account,
                account_type=account_type,
                flow=flow,
                cashflow_type=cashflow_type,
                include_in_cashflow=include_in_cashflow,
                exclude_from_spending=exclude_from_spending,
                currency=currency,
                amount_raw=amount,
                amount_signed=amount_signed,
                description=merchant,
                merchant=merchant,
                category=category,
                source_file="statement.csv",
                source_path="/Users/example/private/statement.csv",
                source_period="2026-05",
                source_row=1,
                imported_at=datetime.now(UTC).replace(tzinfo=None),
                parser_version="test",
                txn_fingerprint=f"{txn_id}-fingerprint",
                **_temporal_fields(posted),
            )
        )
    # The export resolves account_ref through the `accounts` dimension rather than
    # hashing the row's own free-text account_type, so the dimension has to exist.
    # Production always has it: detection runs at every finalize. Seeded with the
    # transaction's own type, which is what detection would derive.
    seed_account(db_path, institution, account, account_type=account_type)


def _insert_recurring(
    db_path: Path,
    *,
    merchant: str = SECRET_MERCHANT,
    expected_amount: Decimal = Decimal("42.99"),
) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    # recurring_patterns.account_id references accounts.account_id, so the parent
    # row must exist for the same account the fixture transactions post to.
    account_id = seed_account(
        db_path, "harbor", "4111111111111111", account_type="visa", is_credit_card=True
    )
    with session_scope(db_path) as session:
        session.add(
            RecurringPatternDB(
                pattern_id="rec001",
                pattern_type="subscription",
                merchant=merchant,
                category="Subscriptions",
                institution="harbor",
                account_id=account_id,
                frequency="monthly",
                expected_day_of_period=1,
                is_fixed_amount=True,
                expected_amount=expected_amount,
                amount_std_dev=Decimal("0.00"),
                confidence=0.95,
                detection_method="heuristic",
                occurrence_count=6,
                first_occurrence=date(2026, 1, 1),
                last_occurrence=date(2026, 6, 1),
                next_expected_date=date(2026, 7, 1),
                is_active=True,
                missed_count=0,
                detected_at=now,
                updated_at=now,
            )
        )


def _read_all_files(zip_path: Path) -> dict[str, str]:
    """Return {filename: decoded text} for every file in the export zip."""
    with zipfile.ZipFile(zip_path) as package:
        return {
            name: package.read(name).decode("utf-8", errors="replace")
            for name in package.namelist()
        }


def test_full_local_audit_zip_omits_raw_accounts_and_source_paths(tmp_path: Path):
    # The full-detail (local) preset still emits transaction-level data, but even
    # there raw account numbers and source paths must not leak.
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_transaction(db_path)

    result = ExportPackageService(db_path).create_package(
        dest_dir=tmp_path / "exports",
        preset="full-local-audit",
        export_format="zip",
    )

    assert result.rows_exported == 1
    with zipfile.ZipFile(result.path) as package:
        assert "transactions.csv" in package.namelist()
        content = package.read("transactions.csv").decode("utf-8")

    assert "4111111111111111" not in content
    assert "/Users/example/private" not in content
    assert "source_path" not in content
    assert "account_ref" in content


def test_full_local_audit_movement_keeps_change_pct_and_exact_amounts(tmp_path: Path):
    """Full-detail (local) presets retain change_pct AND exact drift amounts.

    The share-safe redaction (bucketing + dropping change_pct) must NOT touch the
    full-detail path: local audit keeps the precise current/previous/change and
    the change_pct column for analysis.
    """
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_transaction(
        db_path,
        txn_id="audit-prev-001",
        posted=date(2026, 1, 15),
        amount=Decimal("88.88"),
        category="ZZAudit",
    )
    _insert_transaction(
        db_path,
        txn_id="audit-cur-001",
        posted=date(2026, 2, 15),
        amount=Decimal("111.11"),
        category="ZZAudit",
    )

    result = ExportPackageService(db_path).create_package(
        dest_dir=tmp_path / "exports",
        preset="full-local-audit",
        export_format="zip",
        start_date=date(2026, 2, 1),
        end_date=date(2026, 2, 28),
    )

    files = _read_all_files(result.path)
    assert "category_movement.csv" in files
    movement = files["category_movement.csv"]
    # Full-detail keeps the change_pct column and the exact (unbucketed) amounts.
    assert "change_pct" in movement.splitlines()[0]
    assert "88.88" in movement
    assert "111.11" in movement


def test_export_package_uses_selected_period_and_accepts_new_presets(tmp_path: Path):
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_transaction(db_path, "may", date(2026, 5, 1))
    _insert_transaction(db_path, "june", date(2026, 6, 1))

    result = ExportPackageService(db_path).create_package(
        dest_dir=tmp_path / "exports",
        preset="credit-card-optimization",
        export_format="zip",
        start_date=date(2026, 5, 1),
        end_date=date(2026, 5, 31),
    )

    assert result.rows_exported == 1
    with session_scope(db_path) as session:
        run = session.query(ExportRunDB).one()
        assert run.details["period_start"] == "2026-05-01"
        assert run.details["period_end"] == "2026-05-31"

    merchant_result = ExportPackageService(db_path).create_package(
        dest_dir=tmp_path / "exports",
        preset="merchant-cleanup",
        export_format="csv",
        start_date=date(2026, 5, 1),
        end_date=date(2026, 5, 31),
    )

    assert merchant_result.rows_exported == 1


def test_chatgpt_safe_omits_transaction_level_pii(tmp_path: Path):
    """chatgpt-safe must emit aggregates only: no per-txn files, merchants, amounts, ids.

    Uses a SINGLE transaction with a UNIQUE category and a distinctive exact
    amount. Because it is the only transaction in its category / month / card /
    movement group, every un-bucketed single-row aggregate would equal its exact
    amount and leak it. The fix buckets all share-safe aggregate amounts, so the
    exact amount string must appear in NONE of the emitted files.
    """
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    # A single distinctive transaction in its own unique category. Its exact
    # amount (73.41) is not a multiple of the aggregate bucket, so any raw
    # single-row aggregate (category/month/card/movement) would leak it verbatim.
    leak_amount = "73.41"
    _insert_transaction(
        db_path,
        txn_id="distinctive-txn-id-001",
        merchant=SECRET_MERCHANT,
        amount=Decimal(leak_amount),
        category="ZZMedical",
    )

    result = ExportPackageService(db_path).create_package(
        dest_dir=tmp_path / "exports",
        preset="chatgpt-safe",
        export_format="zip",
    )

    files = _read_all_files(result.path)
    names = set(files)

    # Per-transaction files must be dropped entirely.
    assert "transactions.csv" not in names
    assert "transactions.jsonl" not in names
    # Per-merchant breakdown is effectively merchant-by-merchant spend -> dropped.
    assert "merchant_summary.csv" not in names

    # No emitted file may contain raw merchant, the exact per-transaction amount,
    # or any txn_id. The exact amount must be bucketed away in EVERY aggregate.
    for name, content in files.items():
        assert SECRET_MERCHANT not in content, f"raw merchant leaked in {name}"
        assert leak_amount not in content, f"exact amount leaked in {name}"
        assert "distinctive-txn-id-001" not in content, f"txn_id leaked in {name}"


def test_chatgpt_safe_movement_drops_change_pct_no_exact_amount_recovery(tmp_path: Path):
    """category_movement in chatgpt-safe must not emit change_pct.

    A single-row drift group (one txn this period, one last period) buckets
    current/previous/change to the nearest $10, which alone only narrows the band
    to ~$20. But change_pct = change / abs(previous) * 100 is emitted at FULL
    float precision, and combined with the bucketed band + the 2-decimal cents
    constraint it makes the exact (previous, change) cent-pair UNIQUELY
    recoverable -- leaking the exact transaction amounts. The fix drops the
    change_pct column entirely in share-safe mode.

    Fixture: ZZDrift has 88.88 in the previous window (2026-01) and 111.11 in the
    report window (2026-02). The leaking change_pct mantissa is
    25.011251125112512 (22.23 / 88.88 * 100). The report period is February so
    the snapshot's equal-length previous window lands on January, giving a real
    single-row current-vs-previous drift. Assert the column is gone and no exact
    amount or recoverable mantissa survives in any file.
    """
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    prev_amount = "88.88"
    cur_amount = "111.11"
    # change = 111.11 - 88.88 = 22.23; change_pct = 22.23 / 88.88 * 100
    leaking_pct = str(Decimal(cur_amount) - Decimal(prev_amount))  # "22.23"
    recoverable_mantissa = "25.011251125112512"  # full-precision change_pct
    _insert_transaction(
        db_path,
        txn_id="zzdrift-prev-001",
        posted=date(2026, 1, 15),
        merchant="DRIFT PREV",
        amount=Decimal(prev_amount),
        category="ZZDrift",
    )
    _insert_transaction(
        db_path,
        txn_id="zzdrift-cur-001",
        posted=date(2026, 2, 15),
        merchant="DRIFT CUR",
        amount=Decimal(cur_amount),
        category="ZZDrift",
    )

    result = ExportPackageService(db_path).create_package(
        dest_dir=tmp_path / "exports",
        preset="chatgpt-safe",
        export_format="zip",
        start_date=date(2026, 2, 1),
        end_date=date(2026, 2, 28),
    )

    files = _read_all_files(result.path)

    # If category_movement is present, its header must NOT carry change_pct.
    if "category_movement.csv" in files:
        header = files["category_movement.csv"].splitlines()[0]
        assert "change_pct" not in header, "change_pct column leaks in category_movement"

    # No file may carry change_pct at all, nor the exact amounts, nor the
    # full-precision change_pct mantissa that recovers them.
    for name, content in files.items():
        assert "change_pct" not in content, f"change_pct leaked in {name}"
        assert prev_amount not in content, f"exact previous amount leaked in {name}"
        assert cur_amount not in content, f"exact current amount leaked in {name}"
        assert leaking_pct not in content, f"exact change amount leaked in {name}"
        assert recoverable_mantissa not in content, f"recoverable pct leaked in {name}"


def test_chatgpt_safe_recurring_drops_payee_name_and_buckets_amount(tmp_path: Path):
    """recurring_spend in chatgpt-safe: no merchant/payee field at all, bucketed amount.

    A recurring "merchant" is frequently a personal payee (e.g. a Zelle/PayPal
    transfer to a person). The old canonical-merchant logic only stripped
    digits/punctuation, so an alphabetic name survived intact. Share-safe output
    must drop the merchant string entirely, keeping only category + bucketed
    amount, so no payee name can leak.
    """
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_transaction(db_path)
    _insert_recurring(
        db_path,
        merchant="ZELLE TO JANE DOE",
        expected_amount=Decimal("42.99"),
    )

    result = ExportPackageService(db_path).create_package(
        dest_dir=tmp_path / "exports",
        preset="chatgpt-safe",
        export_format="zip",
    )

    files = _read_all_files(result.path)
    assert "recurring_spend.csv" in files
    recurring = files["recurring_spend.csv"]

    # The personal payee name must not survive in any form (raw or "canonical").
    assert "JANE" not in recurring
    assert "DOE" not in recurring
    assert "ZELLE" not in recurring
    # Option A: there is no merchant/payee column at all.
    header = recurring.splitlines()[0]
    assert "merchant" not in header
    # Exact expected amount must not appear; it is bucketed (nearest $5 -> 45.0).
    assert "42.99" not in recurring
    assert "45" in recurring


def test_chatgpt_safe_account_ref_is_salted_and_stable(tmp_path: Path):
    """account_ref: per-DB salted (differs across DBs), not brute-forceable, stable per DB."""
    import hashlib

    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"
    for db in (db_a, db_b):
        init_database(db)
        _insert_transaction(db)

    # monthly_trend.csv carries account_ref in chatgpt-safe (account-period aggregate).
    def _account_refs(db: Path) -> set[str]:
        result = ExportPackageService(db).create_package(
            dest_dir=tmp_path / f"exp-{db.stem}",
            preset="chatgpt-safe",
            export_format="zip",
        )
        files = _read_all_files(result.path)
        assert "monthly_trend.csv" in files
        rows = files["monthly_trend.csv"].splitlines()
        header = rows[0].split(",")
        idx = header.index("account_ref")
        return {line.split(",")[idx] for line in rows[1:] if line.strip()}

    refs_a = _account_refs(db_a)
    refs_b = _account_refs(db_b)

    assert refs_a and refs_b
    # Same account in two DBs -> different ref (different per-DB salt).
    assert refs_a.isdisjoint(refs_b)

    # Not reproducible by hashing institution|account_type|last4 without the salt.
    naive = hashlib.sha256(b"harbor|4111111111111111|visa").hexdigest()
    for ref in refs_a | refs_b:
        assert naive[:10] not in ref

    # Stable across two exports of the SAME db (salt persisted + reused).
    assert _account_refs(db_a) == refs_a


def test_chatgpt_safe_hides_account_identity_aliases_in_every_member(tmp_path: Path):
    """No raw institution, account, or account-type alias survives the ZIP boundary."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_transaction(
        db_path,
        institution=SECRET_INSTITUTION,
        account=SECRET_ACCOUNT,
        account_type=SECRET_ACCOUNT_TYPE,
    )

    result = ExportPackageService(db_path).create_package(
        dest_dir=tmp_path / "exports",
        preset="chatgpt-safe",
        export_format="zip",
    )

    files = _read_all_files(result.path)
    assert files
    for name, content in files.items():
        for alias in (SECRET_INSTITUTION, SECRET_ACCOUNT, SECRET_ACCOUNT_TYPE):
            assert alias not in content, f"raw account identity {alias!r} leaked in {name}"


def test_chatgpt_safe_identity_member_schemas_use_opaque_refs(tmp_path: Path):
    """Monthly and card aggregates expose the same opaque account-ref namespace."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_transaction(
        db_path,
        institution=SECRET_INSTITUTION,
        account=SECRET_ACCOUNT,
        account_type=SECRET_ACCOUNT_TYPE,
    )

    result = ExportPackageService(db_path).create_package(
        dest_dir=tmp_path / "exports",
        preset="chatgpt-safe",
        export_format="zip",
    )
    files = _read_all_files(result.path)

    monthly_reader = csv.DictReader(io.StringIO(files["monthly_trend.csv"]))
    monthly_rows = list(monthly_reader)
    assert monthly_rows
    assert monthly_reader.fieldnames is not None
    assert "account_ref" in monthly_reader.fieldnames
    assert all(re.fullmatch(r"acct-[0-9a-f]{10}", row["account_ref"]) for row in monthly_rows)

    card_reader = csv.DictReader(io.StringIO(files["card_account_summary.csv"]))
    card_rows = list(card_reader)
    assert card_rows
    assert card_reader.fieldnames is not None
    assert "account_ref" in card_reader.fieldnames
    assert "account_ref_status" in card_reader.fieldnames
    assert "account_id" not in card_reader.fieldnames
    assert "institution" not in card_reader.fieldnames
    assert "account_type" not in card_reader.fieldnames
    assert all(re.fullmatch(r"acct-[0-9a-f]{10}", row["account_ref"]) for row in card_rows)
    assert {row["account_ref"] for row in card_rows} == {row["account_ref"] for row in monthly_rows}


def test_chatgpt_safe_never_emits_salt(tmp_path: Path):
    """The per-DB salt must never appear in any export artifact."""
    from ledger.exporting.service import ExportPackageService as _Svc

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_transaction(db_path)
    _insert_recurring(db_path)

    svc = _Svc(db_path)
    result = svc.create_package(
        dest_dir=tmp_path / "exports",
        preset="chatgpt-safe",
        export_format="zip",
    )

    salt = svc._account_salt()  # the raw secret used for HMAC
    assert salt
    for name, content in _read_all_files(result.path).items():
        assert salt not in content, f"salt leaked in {name}"


@pytest.mark.parametrize("fmt", ["csv", "jsonl", "parquet"])
def test_chatgpt_safe_refuses_per_transaction_formats(tmp_path: Path, fmt: str):
    """chatgpt-safe must raise ValueError for csv/jsonl/parquet.

    These formats emit the raw per-transaction table (merchant, exact amount,
    txn_id). The guard ensures the caller cannot accidentally request a
    share-safe preset with a format that would leak transaction-level PII.
    """
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_transaction(db_path)

    with pytest.raises(
        ValueError,
        match=r"aggregates-only and cannot be exported as a per-transaction",
    ):
        ExportPackageService(db_path).create_package(
            dest_dir=tmp_path / "exports",
            preset="chatgpt-safe",
            export_format=fmt,
        )


def test_chatgpt_safe_card_account_summary_buckets_amounts(tmp_path: Path):
    """card_account_summary in chatgpt-safe must not emit exact net/volume amounts.

    The card summary is derived from the snapshot (account_card_summaries).
    A single transaction means its exact amount equals the card net/volume,
    so an un-bucketed card summary would leak it verbatim. This test uses a
    transaction with an amount that is NOT a multiple of $10 (the aggregate
    bucket size), so a failure to bucket would leave the exact string in the CSV.
    """
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    # 37.77 is not a multiple of $10; bucketed to nearest $10 it becomes 40.0.
    exact_amount = Decimal("37.77")
    leak_string = "37.77"
    _insert_transaction(
        db_path,
        txn_id="card-leak-test-001",
        merchant="CARD LEAK MERCHANT",
        amount=exact_amount,
        category="ZZCardTest",
        account="5555666677778888",
    )

    result = ExportPackageService(db_path).create_package(
        dest_dir=tmp_path / "exports",
        preset="chatgpt-safe",
        export_format="zip",
    )

    files = _read_all_files(result.path)

    # card_account_summary.csv must be present (even if it may be empty when the
    # snapshot yields no account_card_summaries, but our single-transaction DB
    # should populate it via the snapshot service).
    assert "card_account_summary.csv" in files, "card_account_summary.csv missing from zip"
    card_content = files["card_account_summary.csv"]

    # If the card summary is non-empty (i.e. has data rows beyond the header),
    # the exact amount must not appear — it must be bucketed.
    rows = [r for r in card_content.splitlines() if r.strip()]
    if len(rows) > 1:
        # There is at least one data row: confirm bucketing happened.
        assert leak_string not in card_content, (
            f"exact amount {leak_string!r} leaked un-bucketed in card_account_summary.csv"
        )
    # Regardless of snapshot output, no exact amount must appear in ANY file.
    for name, content in files.items():
        assert leak_string not in content, f"exact amount {leak_string!r} leaked in {name}"


def test_parquet_amount_is_decimal_exact(tmp_path: Path) -> None:
    """Per-row 'amount' in a full-detail parquet export is DECIMAL128, not float.

    pyarrow stores a Decimal-typed pandas column as decimal128 so downstream
    SUMs are exact.  Amounts that a float would distort (0.10, 0.20) must
    round-trip as the original Decimal values.
    """
    import pandas as pd
    import pyarrow.parquet as pq

    db_path = tmp_path / "ledger.db"
    init_database(db_path)

    # 0.10 and 0.20 cannot be represented exactly in IEEE-754 binary float;
    # if the column were Float64, read-back values would be 0.10000000000000001 etc.
    _insert_transaction(db_path, "txn-p1", date(2026, 5, 1), amount=Decimal("0.10"))
    _insert_transaction(db_path, "txn-p2", date(2026, 5, 2), amount=Decimal("0.20"))

    result = ExportPackageService(db_path).create_package(
        dest_dir=tmp_path / "exports",
        preset="full-local-audit",
        export_format="parquet",
    )
    assert result.path.suffix == ".parquet"

    # Verify parquet schema: amount column must be decimal128, not float
    schema = pq.read_schema(result.path)
    amount_field = schema.field("amount")
    import pyarrow as pa

    assert pa.types.is_decimal(amount_field.type), (
        f"amount column should be decimal128, got {amount_field.type}"
    )

    # Verify round-trip values are exact
    df = pd.read_parquet(result.path)
    amounts = {str(row["txn_id"]): row["amount"] for _, row in df.iterrows()}

    # amount_signed is negative for expenses (outflows)
    assert amounts["txn-p1"] == Decimal("-0.10"), (
        f"-0.10 did not round-trip exactly; got {amounts['txn-p1']!r}"
    )
    assert amounts["txn-p2"] == Decimal("-0.20"), (
        f"-0.20 did not round-trip exactly; got {amounts['txn-p2']!r}"
    )


def test_parquet_amount_csv_writes_exact_string(tmp_path: Path) -> None:
    """Per-row 'amount' in a full-detail CSV export writes as exact decimal string.

    With Decimal (not float), pandas writes '0.10' not '0.1', preserving the
    two-decimal representation expected in the transactions CSV.
    """
    db_path = tmp_path / "ledger.db"
    init_database(db_path)

    _insert_transaction(db_path, "txn-c1", date(2026, 5, 1), amount=Decimal("0.10"))

    result = ExportPackageService(db_path).create_package(
        dest_dir=tmp_path / "exports",
        preset="full-local-audit",
        export_format="csv",
    )
    assert result.path.suffix == ".csv"

    content = result.path.read_text()
    # The signed amount is -0.10 (expense); it must appear with two decimal places
    assert "-0.10" in content, (
        f"Expected exact '-0.10' in CSV output; got content snippet: {content[:500]!r}"
    )


def test_full_local_audit_csv_neutralizes_formula_injection(tmp_path: Path):
    """A merchant that begins with '=' must be defused so a spreadsheet app
    does not execute it as a formula when the export CSV is opened."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_transaction(db_path, merchant='=HYPERLINK("http://evil.test")')

    result = ExportPackageService(db_path).create_package(
        dest_dir=tmp_path / "exports",
        preset="full-local-audit",  # full-detail: emits transactions.csv + merchant_summary.csv
        export_format="zip",
    )

    files = _read_all_files(result.path)
    # The raw payload must NOT appear at the start of any cell; it must be
    # prefixed with a single quote (neutralize_csv_cell) in every emitted CSV.
    for name, content in files.items():
        if not name.endswith(".csv"):
            continue
        assert "\n=HYPERLINK" not in content and not content.startswith("=HYPERLINK"), (
            f"{name} contains an un-neutralized formula cell"
        )
    txn_csv = files["transactions.csv"]
    merchant_csv = files["merchant_summary.csv"]
    # The cell holds a double-quote, so pandas CSV-quotes it and doubles the inner
    # quotes: the neutralized value "'=HYPERLINK(\"...\")" serializes as
    # "'=HYPERLINK(""http://evil.test"")". The load-bearing fact is the leading '
    # single-quote prefix immediately before the formula's '='.
    neutralized_cell = '"\'=HYPERLINK(""http://evil.test"")"'
    assert neutralized_cell in txn_csv
    assert neutralized_cell in merchant_csv


def test_top_level_csv_export_neutralizes_formula_injection(tmp_path: Path):
    """The format='csv' path (top-level df.to_csv) must also neutralize."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_transaction(db_path, merchant="@SUM(A1:A9)")

    result = ExportPackageService(db_path).create_package(
        dest_dir=tmp_path / "exports",
        preset="full-local-audit",
        export_format="csv",
    )
    content = result.path.read_text(encoding="utf-8")
    assert "'@SUM(A1:A9)" in content
    assert "\n@SUM" not in content and not content.startswith("@SUM")


def test_export_artifacts_are_locked_0600(tmp_path: Path):
    """F29: a full-detail export carries raw merchant names + exact amounts and must be
    written 0600 (dir 0700), like the DB and its backups -- not world-readable under
    umask 022. POSIX-only assertion."""
    import os
    import stat
    import sys

    if sys.platform.startswith("win"):
        return  # POSIX perms not meaningful on Windows

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    _insert_transaction(db_path)
    dest = tmp_path / "exports"

    result = ExportPackageService(db_path).create_package(
        dest_dir=dest,
        preset="full-local-audit",
        export_format="zip",
    )

    file_mode = stat.S_IMODE(os.stat(result.path).st_mode)
    assert file_mode == 0o600, f"export artifact must be 0600, got {oct(file_mode)}"
    dir_mode = stat.S_IMODE(os.stat(dest).st_mode)
    assert dir_mode == 0o700, f"export dir must be 0700, got {oct(dir_mode)}"


def test_export_spend_columns_count_only_spend_universe(tmp_path: Path):
    """H2: gross_spend/spending/true_cost must count ONLY spend-universe rows.

    An ``exclude_from_spending`` expense and a non-CAD expense must not inflate
    the export's aggregate spend, and the export total must reconcile with the
    dashboard aggregate (``get_spending_by_category``) for the same window. The
    full DETAIL transaction rows stay complete regardless.
    """
    import csv
    import io
    import json

    from ledger.query import LedgerQueryService

    db_path = tmp_path / "ledger.db"
    init_database(db_path)

    start, end = date(2026, 5, 1), date(2026, 5, 31)
    # 1) normal CAD expense that counts.
    _insert_transaction(
        db_path,
        txn_id="spend-dining",
        posted=date(2026, 5, 3),
        amount=Decimal("100.00"),
        category="Dining",
    )
    # 2) exclude_from_spending expense (must NOT count toward spend).
    _insert_transaction(
        db_path,
        txn_id="spend-excluded",
        posted=date(2026, 5, 4),
        amount=Decimal("4000.00"),
        category="ExcludedCategory",
        exclude_from_spending=True,
    )
    # 3) non-CAD expense (stored at par, not FX-converted -> must NOT count).
    _insert_transaction(
        db_path,
        txn_id="spend-usd",
        posted=date(2026, 5, 5),
        amount=Decimal("500.00"),
        category="USDTravel",
        currency="USD",
    )
    # 4) CAD income that DOES count toward income/net.
    _insert_transaction(
        db_path,
        txn_id="income-cad",
        posted=date(2026, 5, 6),
        amount=Decimal("300.00"),
        category="Salary",
        flow="income",
    )
    # 5) non-CAD income (stored at par -> must NOT count toward income/net).
    _insert_transaction(
        db_path,
        txn_id="income-usd",
        posted=date(2026, 5, 7),
        amount=Decimal("900.00"),
        category="USDSalary",
        currency="USD",
        flow="income",
    )

    result = ExportPackageService(db_path).create_package(
        dest_dir=tmp_path / "exports",
        preset="full-local-audit",
        export_format="zip",
        start_date=start,
        end_date=end,
    )
    files = _read_all_files(result.path)

    # summary.json: only the $100 counts as spending / gross_spend / true_cost.
    summary = json.loads(files["summary.json"])
    assert Decimal(str(summary["spending"]["value"])) == Decimal("100.00")
    assert Decimal(str(summary["gross_spend"]["value"])) == Decimal("100.00")
    assert Decimal(str(summary["true_cost"]["value"])) == Decimal("100.00")

    # Income/net use the canonical strict policy. USD rows and the excluded CAD
    # expense are outside the canonical income/spend universes, so net reconciles
    # directly as canonical income minus canonical spend.
    assert Decimal(str(summary["income"]["value"])) == Decimal("300.00")
    assert Decimal(str(summary["net"]["value"])) == Decimal("200.00")

    # category_summary.csv: excluded/USD categories show 0 spend (or no row).
    rows = list(csv.DictReader(io.StringIO(files["category_summary.csv"])))
    by_category = {row["category"]: row for row in rows}
    assert Decimal(by_category["Dining"]["gross_spend"]) == Decimal("100.00")
    for excluded in ("ExcludedCategory", "USDTravel"):
        if excluded in by_category:
            assert Decimal(by_category[excluded]["gross_spend"]) == Decimal("0")
            assert Decimal(by_category[excluded]["spending"]) == Decimal("0")
            assert Decimal(by_category[excluded]["true_cost"]) == Decimal("0")

    # category_summary.csv: the USD income category must show 0 income/net (its
    # $900 is a foreign-currency figure summed at par -> excluded from CAD totals).
    # The CAD Salary row keeps the full $300 income.
    assert Decimal(by_category["Salary"]["income"]) == Decimal("300.00")
    assert Decimal(by_category["Salary"]["net"]) == Decimal("300.00")
    for foreign in ("USDTravel", "USDSalary"):
        if foreign in by_category:
            assert Decimal(by_category[foreign]["income"]) == Decimal("0")
            assert Decimal(by_category[foreign]["net"]) == Decimal("0")

    # DETAIL rows stay COMPLETE: all transactions are present, USD ones included.
    detail = list(csv.DictReader(io.StringIO(files["transactions.csv"])))
    detail_ids = {row["txn_id"] for row in detail}
    assert {
        "spend-dining",
        "spend-excluded",
        "spend-usd",
        "income-cad",
        "income-usd",
    } <= detail_ids

    # Reconciliation: export spend == dashboard aggregate for the same window.
    dashboard = LedgerQueryService(db_path).get_spending_by_category(start, end)
    assert sum(dashboard.values(), Decimal("0")) == Decimal("100.00")
    assert Decimal(str(summary["spending"]["value"])) == sum(dashboard.values(), Decimal("0"))


def _mask_row(
    *,
    txn_id: str,
    amount: Decimal,
    currency: object,
    flow: str = "expense",
    include_in_cashflow: bool = True,
    exclude_from_spending: bool = False,
    true_cost_offset: Decimal = Decimal("0.00"),
) -> dict:
    """Minimal row dict with the columns the export mask helpers read."""
    return {
        "txn_id": txn_id,
        "amount": amount,
        "currency": currency,
        "cashflow_type": "income" if flow == "income" else "expense",
        "include_in_cashflow": include_in_cashflow,
        "exclude_from_spending": exclude_from_spending,
        "category": "Uncategorized",
        "month": "2026-05",
        "true_cost_offset": true_cost_offset,
    }


def test_export_aggregates_use_strict_finance_policy_with_decimal_precision():
    import pandas as pd

    rows = [
        _mask_row(
            txn_id="valid-income",
            amount=Decimal("100.01"),
            currency="CAD",
            flow="income",
        ),
        _mask_row(
            txn_id="valid-spend",
            amount=Decimal("-30.02"),
            currency="CAD",
            true_cost_offset=Decimal("10.01"),
        ),
        _mask_row(
            txn_id="excluded-spend",
            amount=Decimal("-4000.00"),
            currency="CAD",
            exclude_from_spending=True,
            true_cost_offset=Decimal("1000.00"),
        ),
        _mask_row(
            txn_id="noncashflow-income",
            amount=Decimal("5000.00"),
            currency="CAD",
            flow="income",
            include_in_cashflow=False,
            true_cost_offset=Decimal("1000.00"),
        ),
        _mask_row(
            txn_id="invalid-income-sign",
            amount=Decimal("-6000.00"),
            currency="CAD",
            flow="income",
            true_cost_offset=Decimal("1000.00"),
        ),
        _mask_row(
            txn_id="invalid-expense-sign",
            amount=Decimal("7000.00"),
            currency="CAD",
            true_cost_offset=Decimal("1000.00"),
        ),
        _mask_row(
            txn_id="zero-income",
            amount=Decimal("0.00"),
            currency="CAD",
            flow="income",
            true_cost_offset=Decimal("1000.00"),
        ),
    ]
    df = pd.DataFrame(rows)

    grouped = ExportPackageService._group(df, ["category"], "category_summary").iloc[0]
    summary = ExportPackageService._summary(
        df,
        "full-local-audit",
        include_transfers=False,
    )["value"]

    for aggregate in (grouped, summary):
        assert Decimal(str(aggregate["income"])) == Decimal("100.01")
        assert Decimal(str(aggregate["gross_spend"])) == Decimal("30.02")
        assert Decimal(str(aggregate["spending"])) == Decimal("30.02")
        assert Decimal(str(aggregate["offsets"])) == Decimal("10.01")
        assert Decimal(str(aggregate["true_cost"])) == Decimal("20.01")
        assert Decimal(str(aggregate["net"])) == Decimal("69.99")


def test_export_mask_treats_missing_currency_as_cad_like_sql_predicate():
    """Lockstep: a row with a missing/None currency must be treated as CAD by the export
    spend/income masks, matching the SQL ``_cad_only()`` predicate (``currency IS NULL``
    -> kept as CAD).

    When a ``None`` currency is mixed with real currency strings in the same column,
    pandas coerces it to a float ``nan``. Pre-fix ``is_cad(nan)`` is False, so
    ``_spend_mask``/``_cad_amount`` DROP the NULL-currency row — the exact
    currency-blindness the shared predicate exists to eliminate, inverted. Post-fix the
    NaN/None is normalized to CAD before masking, so the row counts.
    """
    import pandas as pd

    from ledger.exporting.service import ExportPackageService

    # A real CAD row alongside the NULL-currency rows forces pandas to store the column
    # as a string column, coercing the None values to float `nan` (the buggy path).
    df = pd.DataFrame(
        [
            _mask_row(txn_id="cad-anchor", amount=Decimal("-10.00"), currency="CAD"),
            _mask_row(txn_id="null-spend", amount=Decimal("-40.00"), currency=None),
            _mask_row(txn_id="null-income", amount=Decimal("70.00"), currency=None, flow="income"),
        ]
    )
    # Fixture precondition: the None currencies are now NaN (not Python None).
    assert df["currency"].isna().loc[[1, 2]].all(), "fixture: NULL currencies coerced to NaN"

    # _spend_mask must include the NULL/NaN-currency expense (treated as CAD, like _cad_only).
    spend_mask = ExportPackageService._spend_mask(df)
    assert bool(spend_mask.loc[1]), (
        "A NULL/NaN-currency expense must be in the spend universe (CAD-equivalent), "
        "matching SQL _cad_only(); pre-fix is_cad(nan) drops it."
    )

    # _group aggregate: both CAD anchor ($10) and NULL-currency expense ($40) count as
    # gross_spend; the NULL-currency income ($70) counts as income; net nets all CAD rows.
    grouped = ExportPackageService._group(df, ["category"], "category_summary")
    row = grouped[grouped["category"] == "Uncategorized"].iloc[0]
    assert Decimal(str(row["gross_spend"])) == Decimal("50.00"), (
        "NULL-currency expense must count toward gross_spend (treated as CAD)."
    )
    assert Decimal(str(row["income"])) == Decimal("70.00"), (
        "NULL-currency income must count toward income (treated as CAD)."
    )
    assert Decimal(str(row["net"])) == Decimal("70.00") - Decimal("50.00"), (
        "NULL-currency rows must count toward net (treated as CAD)."
    )

    # _summary must reconcile too.
    summary = ExportPackageService._summary(df, "full-local-audit", include_transfers=False)
    summary_values = summary["value"]
    assert Decimal(str(summary_values["gross_spend"])) == Decimal("50.00")
    assert Decimal(str(summary_values["income"])) == Decimal("70.00")
    assert Decimal(str(summary_values["net"])) == Decimal("20.00")
