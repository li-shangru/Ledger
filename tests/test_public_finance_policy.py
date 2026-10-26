"""Invented reimbursement declarations preserve exact financial interpretation."""

import json
from contextlib import closing
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from ledger.adapters import registry
from ledger.adapters.contracts import (
    API_VERSION,
    AdapterDefinition,
    AdapterError,
    CompatibilityPolicy,
)
from ledger.adjustments import AdjustmentDetectionService
from ledger.storage.database import (
    TransactionDB,
    TransactionOffsetDB,
    TransactionTreatmentDB,
    clear_engine_cache,
    init_database,
    session_scope,
)
from ledger.storage.migrations import ensure_schema
from ledger.storage.read_only import ReadOnlyDatabase

_OMITTED = object()


def _write_policy(path: Path, merchants=_OMITTED, *, enabled=False, records=None) -> None:
    metadata = registry._definition_metadata(
        AdapterDefinition("invented-history", API_VERSION, "1")
    )
    metadata["policy"].pop("reimbursement_merchants", None)
    if merchants is not _OMITTED:
        metadata["policy"]["reimbursement_merchants"] = merchants
    record = {
        "distribution": "invented-history",
        "version": "1.0.0",
        "entry_point": "invented",
        "sha256": "9" * 64,
        "enabled": enabled,
        "metadata": metadata,
    }
    path.write_text(json.dumps({"schema_version": 1, "adapters": records or [record]}))
    path.chmod(0o600)


@pytest.fixture
def policy_path(tmp_path, monkeypatch):
    root = tmp_path / "invented-policy"
    root.mkdir(mode=0o700)
    path = root / "adapters.json"
    monkeypatch.setenv("LEDGER_ADAPTER_CONFIG", str(path))
    monkeypatch.setattr(registry, "_distribution", lambda *_a: pytest.fail("Package inspected"))
    monkeypatch.setattr(registry, "_load_record", lambda *_a: pytest.fail("Extension loaded"))
    monkeypatch.setattr(registry, "load_adapters", lambda *_a: pytest.fail("Extension loaded"))
    return path


def _transaction(identifier: str, amount: str, merchant: str, category: str, **changes):
    value = Decimal(amount)
    fields = {
        "txn_id": identifier,
        "posting_date": date(2048, 3, 2 if value < 0 else 8),
        "transaction_date": date(2048, 3, 2 if value < 0 else 8),
        "institution": "invented",
        "account": "invented-account",
        "account_type": "Chequing",
        "flow": "expense" if value < 0 else "income",
        "cashflow_type": "expense" if value < 0 else "income",
        "include_in_cashflow": True,
        "exclude_from_spending": False,
        "currency": "CAD",
        "amount_raw": abs(value),
        "amount_signed": value,
        "merchant": merchant,
        "description": "INVENTED PAYMENT",
        "category": category,
        "source_file": "invented.csv",
        "source_path": "invented/invented.csv",
        "source_period": "2048-03",
        "source_row": 1,
        "parser_version": "invented",
        "day_of_week": 0,
        "day_of_month": 2,
        "week_of_year": 10,
        "is_weekend": False,
        "is_month_start": False,
        "is_month_end": False,
        "imported_at": datetime(2048, 4, 1),
        "txn_fingerprint": identifier + "-fingerprint",
    }
    return TransactionDB(**(fields | changes))


def _database(tmp_path: Path, *rows) -> Path:
    db = tmp_path / "synthetic.db"
    init_database(db)
    with session_scope(db) as session:
        session.add_all(rows)
    return db


def _financial_rows(db: Path):
    with session_scope(db) as session:
        return [
            (row.txn_id, row.amount_raw, row.amount_signed, row.currency, row.merchant)
            for row in session.query(TransactionDB).order_by(TransactionDB.txn_id)
        ]


def test_public_reimbursement_policy_has_no_provider_defaults(policy_path):
    assert CompatibilityPolicy().reimbursement_merchants == ()
    assert registry.compatibility_policy()["reimbursement_merchants"] == []
    assert registry.compatibility_policy_snapshot().reimbursement_merchants == frozenset()


@pytest.mark.parametrize("enabled", [False, True])
def test_old_historical_metadata_is_empty_read_only_and_inert(policy_path, enabled):
    _write_policy(policy_path, enabled=enabled)
    before = (policy_path.read_bytes(), policy_path.stat().st_mtime_ns)
    snapshot = registry.compatibility_policy_snapshot()
    assert snapshot.reimbursement_merchants == frozenset()
    assert (policy_path.read_bytes(), policy_path.stat().st_mtime_ns) == before


@pytest.mark.parametrize("enabled", [False, True])
def test_explicit_reimbursement_policy_normalizes_only_exact_merchants(policy_path, enabled):
    _write_policy(policy_path, ["  invented benefits  ", "INVENTED CLAIMS"], enabled=enabled)
    before = policy_path.read_bytes()
    policy = registry.compatibility_policy_snapshot()
    assert policy.reimbursement_merchants == frozenset({"INVENTED BENEFITS", "INVENTED CLAIMS"})
    assert policy_path.read_bytes() == before


@pytest.mark.parametrize(
    "merchants",
    [
        "INVENTED BENEFITS",
        None,
        True,
        {},
        [False],
        [""],
        [" "],
        ["BAD\nTOKEN"],
        ["X" * 513],
        ["ß" * 257],
        ["INVENTED BENEFITS", "INVENTED BENEFITS"],
        [f"INVENTED {i}" for i in range(129)],
    ],
)
def test_malformed_reimbursement_policy_fails_without_fallback(policy_path, merchants):
    _write_policy(policy_path, merchants)
    with pytest.raises(AdapterError, match=r"^adapter_unsafe_configuration$"):
        registry.compatibility_policy_snapshot()


def test_combined_reimbursement_policy_is_bounded(policy_path):
    _write_policy(policy_path, [f"INVENTED {i}" for i in range(64)])
    first = json.loads(policy_path.read_text())["adapters"][0]
    _write_policy(policy_path, [f"INVENTED {i}" for i in range(64, 129)])
    second = json.loads(policy_path.read_text())["adapters"][0]
    second["distribution"] = second["metadata"]["adapter_id"] = "invented-second"
    _write_policy(policy_path, records=[first, second])
    with pytest.raises(AdapterError, match=r"^adapter_unsafe_configuration$"):
        registry.compatibility_policy_snapshot()


def test_reimbursement_policy_accepts_the_declared_bounds(policy_path):
    merchants = ["X" * 512, *(f"INVENTED {i}" for i in range(127))]
    _write_policy(policy_path, merchants)
    assert registry.compatibility_policy_snapshot().reimbursement_merchants == frozenset(merchants)


def test_malformed_policy_stops_detection_without_financial_changes(policy_path, tmp_path):
    _write_policy(policy_path, ["INVENTED BENEFITS"])
    db = _database(
        tmp_path,
        _transaction("debit", "-80.25", "INVENTED CLINIC", "Health & Wellness"),
        _transaction("credit", "80.25", "INVENTED BENEFITS", "Insurance"),
    )
    service = AdjustmentDetectionService(db)
    before = _financial_rows(db)
    _write_policy(policy_path, [" "])
    result = service.detect()
    assert result.status == "failed" and not result.ok
    assert result.errors == ["adapter_unsafe_configuration"]
    assert result.treatments == [] and result.offsets == []
    assert _financial_rows(db) == before
    with session_scope(db) as session:
        assert session.query(TransactionTreatmentDB).count() == 0
        assert session.query(TransactionOffsetDB).count() == 0


def test_declared_reimbursement_keeps_exact_offsets_and_idempotency(policy_path, tmp_path):
    _write_policy(policy_path, ["INVENTED BENEFITS"])
    db = _database(
        tmp_path,
        _transaction("debit", "-125.37", "INVENTED CLINIC", "Health & Wellness"),
        _transaction("credit-a", "75.11", " invented benefits ", "Insurance"),
        _transaction("credit-b", "50.26", "INVENTED BENEFITS", "Insurance"),
    )
    before = _financial_rows(db)
    service = AdjustmentDetectionService(db)
    first = service.detect(execute=True)
    assert first.ok
    assert {(row.txn_id, row.treatment_type, row.status) for row in first.treatments} == {
        ("credit-a", "reimbursement", "auto_applied"),
        ("credit-b", "reimbursement", "auto_applied"),
    }
    assert sorted(row.offset_amount for row in first.offsets) == [
        Decimal("50.26"),
        Decimal("75.11"),
    ]
    second = service.detect(execute=True)
    assert second.ok and second.treatments == first.treatments and second.offsets == first.offsets
    with session_scope(db) as session:
        assert session.query(TransactionTreatmentDB).count() == 2
        assert session.query(TransactionOffsetDB).count() == 2
    assert _financial_rows(db) == before
    summary = service.true_cost_summary(date(2048, 3, 1), date(2048, 3, 31))
    assert (summary.gross_spend, summary.offsets, summary.true_cost) == (
        Decimal("125.37"),
        Decimal("125.37"),
        Decimal("0.00"),
    )


@pytest.mark.parametrize("merchants", [[], ["INVENTED BENEFITS PLUS"], ["OTHER INVENTED SOURCE"]])
def test_insurance_category_and_partial_provider_names_do_not_imply_reimbursement(
    policy_path, tmp_path, merchants
):
    _write_policy(policy_path, merchants)
    db = _database(
        tmp_path,
        _transaction("debit", "-80.25", "INVENTED CLINIC", "Health & Wellness"),
        _transaction("credit", "80.25", "INVENTED BENEFITS", "Insurance"),
    )
    result = AdjustmentDetectionService(db).detect()
    assert result.ok and result.treatments == [] and result.offsets == []


@pytest.mark.parametrize("currency", ["USD", "EUR"])
@pytest.mark.parametrize("foreign_side", ["credit", "debit"])
def test_reimbursement_policy_preserves_native_currency_boundaries(
    policy_path, tmp_path, currency, foreign_side
):
    _write_policy(policy_path, ["INVENTED BENEFITS"])
    db = _database(
        tmp_path,
        _transaction(
            "debit",
            "-80.25",
            "INVENTED CLINIC",
            "Health & Wellness",
            currency=currency if foreign_side == "debit" else "CAD",
        ),
        _transaction(
            "credit",
            "80.25",
            "INVENTED BENEFITS",
            "Insurance",
            currency=currency if foreign_side == "credit" else "CAD",
        ),
    )
    result = AdjustmentDetectionService(db).detect()
    assert result.ok and result.offsets == []
    if foreign_side == "credit":
        assert result.treatments == []
    else:
        assert [(t.treatment_type, t.status, t.netting_policy) for t in result.treatments] == [
            ("reimbursement", "needs_review", "review_required")
        ]


@pytest.mark.parametrize("currency", [None, "", "CAD"])
def test_declared_reimbursement_preserves_legacy_cad_semantics(policy_path, tmp_path, currency):
    _write_policy(policy_path, ["INVENTED BENEFITS"])
    db = _database(
        tmp_path,
        _transaction("debit", "-80.25", "INVENTED CLINIC", "Health & Wellness", currency=currency),
        _transaction("credit", "80.25", "INVENTED BENEFITS", "Insurance", currency=currency),
    )
    result = AdjustmentDetectionService(db).detect()
    assert result.ok and [row.offset_amount for row in result.offsets] == [Decimal("80.25")]


def test_detection_uses_one_inert_policy_snapshot_per_operation(policy_path, tmp_path, monkeypatch):
    _write_policy(policy_path, ["INVENTED BENEFITS"])
    db = _database(
        tmp_path,
        _transaction("debit", "-40.00", "INVENTED CLINIC", "Health & Wellness"),
        _transaction("credit-a", "20.00", "INVENTED BENEFITS", "Insurance"),
        _transaction("credit-b", "20.00", "INVENTED BENEFITS", "Insurance"),
    )
    service = AdjustmentDetectionService(db)
    original_classify = service._classify_credit
    original_policy = registry.compatibility_policy
    reads = 0

    def counted_policy():
        nonlocal reads
        reads += 1
        return original_policy()

    def classify_then_change(*args, **kwargs):
        result = original_classify(*args, **kwargs)
        _write_policy(policy_path, ["OTHER INVENTED SOURCE"])
        return result

    monkeypatch.setattr(registry, "compatibility_policy", counted_policy)
    monkeypatch.setattr(service, "_classify_credit", classify_then_change)
    first = service.detect()
    assert first.ok and len(first.offsets) == 2 and reads == 1
    second = service.detect()
    assert second.ok and second.offsets == [] and reads == 2


def test_reimbursement_policy_dry_run_uses_pinned_read_only_session(policy_path, tmp_path):
    _write_policy(policy_path, ["INVENTED BENEFITS"])
    db = _database(
        tmp_path,
        _transaction("debit", "-80.25", "INVENTED CLINIC", "Health & Wellness"),
        _transaction("credit", "80.25", "INVENTED BENEFITS", "Insurance"),
    )
    ensure_schema(db)
    clear_engine_cache()
    before = (db.read_bytes(), db.stat().st_mtime_ns)
    with closing(ReadOnlyDatabase(db)) as database:
        database.preflight()
        result = AdjustmentDetectionService(
            db, prepare_schema=False, session_scope_factory=database.session_scope
        ).detect(execute=False)
    assert result.ok and [row.offset_amount for row in result.offsets] == [Decimal("80.25")]
    assert (db.read_bytes(), db.stat().st_mtime_ns) == before
