"""Independent fixed identities for reordered mixed-currency/reference exports."""

from itertools import permutations

from ledger.parsers.base import ParseContext
from ledger.parsers.canonical import CanonicalParser


def test_occurrences_are_scoped_to_full_identity_and_unreferenced_rows(tmp_path, sample_app_config):
    base = {
        "Ledger Format": "ledger-mapped-v1",
        "Date": "2031-04-02",
        "Description": "Example",
        "Amount": "-10.00",
        "Currency": "CAD",
        "Reference": "",
        "Institution": "fictional",
        "Account Type": "chequing",
    }
    rows = [base, dict(base), base | {"Currency": "USD"}, base | {"Reference": "ref-7"}]
    expected = {
        "8ede600962fe636105750c4d7520e657056ff2a9",
        "0642085ca0bb5c2b58aae3704fc4c236e414f6fa",
        "8d8c1847f9103f321f3ef019855066145085295d",
        "2121b10005192a3566c292d0a6528c58815550e9",
    }
    for ordered in permutations(rows):
        parser = CanonicalParser(
            ParseContext(
                sample_app_config, tmp_path, tmp_path / "export.csv", account_slug="fictional-cash"
            )
        )
        parsed = [parser.parse_row(row, number) for number, row in enumerate(ordered, 1)]
        assert {row.txn_id for row in parsed} == expected
        assert len(parsed) == len(expected)
