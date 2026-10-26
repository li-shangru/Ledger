"""Fictional typed PDF evidence reaches the canonical persistence boundary."""

from datetime import date
from decimal import Decimal

import pytest

from ledger.adapters import registry
from ledger.adapters.contracts import AdapterError, PdfFacts, PdfFormat
from ledger.imports.pdf import statement_pdf_to_csv as public_pdf
from ledger.imports.pdf.post_processing import persist_printed_credit_limits
from ledger.imports.pdf.statement_pdf_to_csv import _dispatch
from ledger.storage.database import init_database
from ledger.storage.operations import get_latest_statement_credit_limit


def extract(tmp_path, monkeypatch, limit, summary):
    source = tmp_path / "fictional.pdf"
    source.write_bytes(b"invented statement bytes")
    facts = PdfFacts(
        rows=({"Day": "2031-04-02", "Delta": "-12.34"},),
        period=date(2031, 4, 30),
        account_type="credit",
        account_number="7",
        account_slug="fictional-credit-7",
        credit_limit=limit,
        summary=summary,
    )
    fmt = PdfFormat(
        "fictional-credit",
        "fictional",
        ("Day", "Delta"),
        lambda _: True,
        lambda *_: facts,
        lambda *_: "fictional-credit-7",
    )
    monkeypatch.setattr(public_pdf, "run_pdftotext_bytes", lambda _: "invented")
    monkeypatch.setattr(_dispatch, "detect_statement_type", lambda _: fmt.format_id)
    monkeypatch.setattr(_dispatch, "get_pdf_format", lambda _: fmt)
    monkeypatch.setattr(registry, "get_pdf_format", lambda _: fmt)
    return _dispatch.process_pdf(source, tmp_path / "csv", tmp_path / "archive", False, False)


@pytest.mark.parametrize("limit", [Decimal("4321.00"), Decimal("0.00"), None])
def test_typed_credit_limit_persists_exactly_with_account_alias(tmp_path, monkeypatch, limit):
    extracted = extract(tmp_path, monkeypatch, limit, {})
    manifest = tmp_path / "manifest.json"
    _dispatch.write_manifest(manifest, [extracted], False)
    database = tmp_path / "ledger.db"
    init_database(database)
    count = persist_printed_credit_limits(
        database, manifest, {"fictional-credit-7": "fictional-main"}
    )
    assert count == (0 if limit is None else 1)
    assert get_latest_statement_credit_limit(database, "fictional-main") == limit
    assert get_latest_statement_credit_limit(database, "fictional-credit-7") is None


@pytest.mark.parametrize(
    "limit,summary",
    [
        (Decimal("NaN"), {}),
        (Decimal("Infinity"), {}),
        (Decimal("-1"), {}),
        (Decimal("5"), {"printed_credit_limit": "6"}),
        (None, {"printed_credit_limit": "NaN"}),
    ],
)
def test_invalid_or_conflicting_limits_refuse_before_output(tmp_path, monkeypatch, limit, summary):
    with pytest.raises(AdapterError, match="adapter_invalid_contract"):
        extract(tmp_path, monkeypatch, limit, summary)
    assert not (tmp_path / "csv").exists()


@pytest.mark.parametrize("typed", [None, Decimal("4321.00")])
def test_legacy_summary_limit_remains_supported(tmp_path, monkeypatch, typed):
    extracted = extract(tmp_path, monkeypatch, typed, {"printed_credit_limit": "4321.00"})
    assert Decimal(extracted.validation_summary["printed_credit_limit"]) == Decimal("4321.00")
