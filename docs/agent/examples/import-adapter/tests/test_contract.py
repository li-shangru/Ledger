"""Starter contract kit, authored independently from any real financial source."""

from decimal import Decimal

import pytest
from fictional_ledger_adapter import FictionalParser, create_adapter

from ledger.adapters.contracts import API_VERSION
from ledger.config import (
    AppConfig,
    CategoriesConfig,
    CategoryRule,
    LedgerConfig,
    MerchantRulesConfig,
)
from ledger.core import ExceptionRecord
from ledger.parsers.base import ParseContext


@pytest.fixture
def parser(tmp_path):
    config = AppConfig(
        ledger=LedgerConfig(),
        merchants=MerchantRulesConfig(version="synthetic", rules=[]),
        categories=CategoriesConfig(
            version="synthetic",
            default_category="Other",
            categories=[CategoryRule(name="Other", merchants=[])],
        ),
    )
    return FictionalParser(
        ParseContext(
            config,
            tmp_path,
            tmp_path / "invented.csv",
            account_slug="fictional-account",
        )
    )


def row(**changes):
    return {
        "Day": "2026-02-03",
        "Memo": "INVENTED KITE SHOP",
        "Delta": "-4.25",
        "Unit": "CAD",
        "Receipt": "fictional-001",
        **changes,
    }


def test_contract_and_exact_recognition():
    definition = create_adapter()
    assert definition.api_version == API_VERSION == 1
    assert definition.adapter_id == "fictional-example"
    assert definition.csv_formats[0].parser is FictionalParser
    assert not definition.pdf_formats and not definition.investment_formats
    assert FictionalParser.can_parse(set(row()))
    assert not FictionalParser.can_parse(set(row()) | {"Unknown"})
    assert not FictionalParser.can_parse(set(row()) - {"Delta"})


def test_exact_money_currency_and_stable_reference_identity(parser):
    first = parser.parse_row(row(), 2)
    duplicate = parser.parse_row(row(), 99)
    changed = parser.parse_row(row(Delta="-5.25"), 2)
    other_currency = parser.parse_row(row(Unit="USD"), 2)
    assert first.amount_raw == Decimal("-4.25")
    assert first.currency == "CAD" and first.source_row == 2
    assert first.txn_id == duplicate.txn_id == changed.txn_id
    assert first.txn_fingerprint == duplicate.txn_fingerprint
    assert first.txn_fingerprint != changed.txn_fingerprint
    assert first.txn_id != other_currency.txn_id


@pytest.mark.parametrize(
    "changes",
    [
        {"Delta": "1e2"},
        {"Delta": "1.234"},
        {"Delta": "NaN"},
        {"Delta": "1,000.00"},
        {"Unit": "cad"},
        {"Day": "2026-02-30"},
        {"Day": "20260203"},
        {"Memo": " "},
        {"Unknown": "value"},
    ],
)
def test_unsupported_rows_are_exception_records_not_silent_skips(parser, changes):
    outcomes = list(parser.parse(iter([row(**changes)])))
    assert len(outcomes) == 1
    assert isinstance(outcomes[0], ExceptionRecord)
    assert outcomes[0].reason == "row_parse_failed"
    assert outcomes[0].raw_data == {}
