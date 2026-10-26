"""Independently authored CSV fixtures; no owner statements or runtime defaults."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import date
from decimal import Decimal, localcontext

import pytest
from typer.testing import CliRunner

from ledger.adapters import csv_profile as module
from ledger.adapters.csv_profile import (
    PREVIEW_NOTICE,
    CsvProfile,
    CsvProfileError,
    inspect_csv_headers,
    load_profile,
    parse_csv_bytes,
    profile_digest,
    save_profile,
)
from ledger.commands.csv_profiles import csv_profiles_app


@pytest.fixture
def profile() -> CsvProfile:
    return CsvProfile(
        profile_id="fictional-v1",
        delimiter=",",
        header_row=1,
        headers=["Posted", "Text", "Value", "CCY", "Ref"],
        date_column="Posted",
        date_format="%Y-%m-%d",
        description_column="Text",
        amount_column="Value",
        sign_convention="inflow_positive",
        currency_column="CCY",
        reference_column="Ref",
    )


@pytest.fixture
def debit_profile() -> CsvProfile:
    return CsvProfile(
        profile_id="debit-v1",
        delimiter=";",
        header_row=2,
        headers=("When", "Memo", "Debit", "Credit"),
        date_column="When",
        date_format="%d/%m/%Y",
        description_column="Memo",
        debit_column="Debit",
        credit_column="Credit",
        currency="EUR",
    )


def test_literal_amounts_dates_currencies_and_references(profile):
    rows = parse_csv_bytes(
        b"Posted,Text,Value,CCY,Ref\n"
        b"2026-01-02,FICTIONAL SHOP,-23.45,CAD,x1\n"
        b'2026-01-03,"FICTIONAL, REFUND",+6.10,USD,x2\n',
        profile,
    )
    assert len(rows) == 2
    assert rows[0].source_row == 2
    assert rows[0].transaction_date == date(2026, 1, 2)
    assert rows[0].transaction_date.isoformat() == "2026-01-02"
    assert rows[0].description == "FICTIONAL SHOP"
    assert rows[0].amount == Decimal("-23.45")
    assert rows[0].currency == "CAD"
    assert rows[0].reference == "x1"
    assert rows[1].description == "FICTIONAL, REFUND"
    assert rows[1].amount == Decimal("6.10")
    assert rows[1].currency == "USD"
    with pytest.raises(FrozenInstanceError):
        rows[0].currency = "GBP"
    with pytest.raises(FrozenInstanceError):
        profile.currency = "GBP"


def test_outflow_positive_is_explicit_and_decimal_context_independent(profile):
    content = (
        b"Posted,Text,Value,CCY,Ref\n2026-01-02,FICTIONAL SHOP,12345.67,CAD,x\n"
        b"2026-01-03,FICTIONAL REFUND,-6.10,CAD,y\n"
    )
    with localcontext() as context:
        context.prec = 3
        rows = parse_csv_bytes(content, replace(profile, sign_convention="outflow_positive"))
    assert rows[0].amount == Decimal("-12345.67")
    assert rows[1].amount == Decimal("6.10")


def test_debit_credit_fixed_currency_preamble_and_blank_counterpart(debit_profile):
    rows = parse_csv_bytes(
        b"Fictional export\nWhen;Memo;Debit;Credit\n"
        b"02/01/2026;FICTIONAL PURCHASE;12.34;\n"
        b"03/01/2026;FICTIONAL DEPOSIT;;45.67\n"
        b"04/01/2026;FICTIONAL ZERO;0.00;0.00\n",
        debit_profile,
    )
    assert [
        (r.source_row, r.transaction_date, r.amount, r.currency, r.reference) for r in rows
    ] == [
        (3, date(2026, 1, 2), Decimal("-12.34"), "EUR", ""),
        (4, date(2026, 1, 3), Decimal("45.67"), "EUR", ""),
        (5, date(2026, 1, 4), Decimal("0.00"), "EUR", ""),
    ]


@pytest.mark.parametrize("debit,credit", [("12.34", "5.67"), ("-12.34", ""), ("", "-5.67")])
def test_debit_credit_ambiguity_rejected(debit_profile, debit, credit):
    with pytest.raises(CsvProfileError, match=r"^csv_debit_credit$"):
        parse_csv_bytes(
            f"Preamble\nWhen;Memo;Debit;Credit\n02/01/2026;Fictional;{debit};{credit}\n".encode(),
            debit_profile,
        )


@pytest.mark.parametrize(
    "value",
    [
        "",
        "1.001",
        "1.000",
        "NaN",
        "Infinity",
        "1e2",
        "$1.00",
        "1,000.00",
        " 1.00",
        "1.00 ",
        "1_000",
        ".10",
        "\uff11.\uff10\uff10",
    ],
)
def test_amounts_never_guess_round_or_coerce(profile, value):
    content = f'Posted,Text,Value,CCY,Ref\n2026-01-02,Fictional,"{value}",CAD,x\n'.encode()
    with pytest.raises(CsvProfileError, match=r"^csv_amount$"):
        parse_csv_bytes(content, profile)


@pytest.mark.parametrize("currency", ["", "cad", " CAD", "CAD ", "US", "123"])
def test_row_currency_is_explicit(profile, currency):
    with pytest.raises(CsvProfileError, match=r"^csv_currency$"):
        parse_csv_bytes(
            f"Posted,Text,Value,CCY,Ref\n2026-01-02,Fictional,1.00,{currency},x\n".encode(), profile
        )


@pytest.mark.parametrize(
    "value", ["2026-1-02", "2026-01-2", "2026-02-30", "01/02/2026", "2026-01-02 "]
)
def test_dates_are_exact(profile, value):
    with pytest.raises(CsvProfileError, match=r"^csv_date$"):
        parse_csv_bytes(
            f"Posted,Text,Value,CCY,Ref\n{value},Fictional,1.00,CAD,x\n".encode(), profile
        )


@pytest.mark.parametrize(
    "headers,code",
    [
        ("Posted,Text,Value,CCY,Ref,Extra", "csv_header_drift"),
        ("Posted,Text,Value,CCY", "csv_header_drift"),
        ("Text,Posted,Value,CCY,Ref", "csv_header_drift"),
        ("posted,Text,Value,CCY,Ref", "csv_header_drift"),
        ("Posted,Text,Value,CCY, Ref", "csv_header_drift"),
        ("Posted,Text,Value,CCY,CCY", "csv_headers"),
        ("Posted,Text,Value,CCY,", "csv_headers"),
    ],
)
def test_exact_header_contract(profile, headers, code):
    with pytest.raises(CsvProfileError, match=rf"^{code}$"):
        parse_csv_bytes(f"{headers}\n2026-01-02,Fictional,1.00,CAD,x\n".encode(), profile)


@pytest.mark.parametrize(
    "row,code",
    [
        ("2026-01-02,Fictional,1.00,CAD", "csv_row_width"),
        ("2026-01-02,Fictional,1.00,CAD,x,extra", "csv_row_width"),
        ('2026-01-02,"Fictional,1.00,CAD,x', "csv_malformed"),
        ('2026-01-02,Fiction"al,1.00,CAD,x', "csv_malformed"),
        ('2026-01-02,"Fictional" tail,1.00,CAD,x', "csv_malformed"),
        ("2026-01-02,,1.00,CAD,x", "csv_description"),
        ("2026-01-02,Fictional\x00,1.00,CAD,x", "csv_malformed"),
        ("", "csv_row_width"),
    ],
)
def test_malformed_or_partial_rows_fail_entire_parse(profile, row, code):
    content = f"Posted,Text,Value,CCY,Ref\n2026-01-01,Valid,1.00,CAD,ok\n{row}\n".encode()
    with pytest.raises(CsvProfileError, match=rf"^{code}$"):
        parse_csv_bytes(content, profile)


def test_multiline_quote_and_bom_are_explicit_transport_support(profile):
    rows = parse_csv_bytes(
        b'\xef\xbb\xbfPosted,Text,Value,CCY,Ref\r\n2026-01-02,"Fictional\n""quoted""",1.00,CAD,x\r\n'
        b"2026-01-03,Fictional,2.00,CAD,y\r\n",
        profile,
    )
    assert rows[0].description == 'Fictional\n"quoted"'
    assert rows[1].source_row == 3


@pytest.mark.parametrize(
    "change",
    [
        {"schema_version": 2},
        {"schema_version": True},
        {"profile_id": "../private"},
        {"header_row": True},
        {"header_row": 0},
        {"delimiter": "::"},
        {"headers": ["Posted", "Text", "Value", "CCY", "CCY"]},
        {"date_column": "Missing"},
        {"amount_column": "Text"},
        {"date_format": "%m/%d"},
        {"sign_convention": None},
        {"debit_column": "Ref"},
        {"currency": "CAD"},
        {"currency_column": None},
        {"account": "must-never-bind"},
        {"balance": "0.00"},
    ],
)
def test_invalid_profiles_and_unknown_financial_fields_rejected(profile, change):
    with pytest.raises(CsvProfileError, match=r"^profile_invalid$"):
        CsvProfile(**(asdict(profile) | change))


def test_missing_fields_and_non_string_headers_rejected(profile):
    values = asdict(profile)
    del values["description_column"]
    with pytest.raises(CsvProfileError, match=r"^profile_invalid$"):
        CsvProfile(**values)
    with pytest.raises(CsvProfileError, match=r"^profile_invalid$"):
        CsvProfile(**(asdict(profile) | {"headers": [None]}))


def test_digest_matches_canonical_json_and_tracks_every_format_revision(profile):
    expected = hashlib.sha256(
        json.dumps(
            asdict(profile), sort_keys=True, ensure_ascii=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    assert profile_digest(profile) == expected
    assert profile_digest(CsvProfile(**dict(reversed(list(asdict(profile).items()))))) == expected
    assert profile_digest(replace(profile, sign_convention="outflow_positive")) != expected
    assert profile_digest(replace(profile, profile_id="fictional-v2")) != expected


def test_bounds_and_empty_exports(profile, monkeypatch):
    with pytest.raises(CsvProfileError, match=r"^csv_empty$"):
        parse_csv_bytes(b"Posted,Text,Value,CCY,Ref\n", profile)
    with pytest.raises(CsvProfileError, match=r"^csv_encoding$"):
        parse_csv_bytes(b"\xff", profile)
    monkeypatch.setattr(module, "MAX_CSV_BYTES", 10)
    with pytest.raises(CsvProfileError, match=r"^csv_too_large$"):
        parse_csv_bytes(b"x" * 11, profile)
    monkeypatch.setattr(module, "MAX_CSV_BYTES", 10000)
    monkeypatch.setattr(module, "MAX_CSV_ROWS", 1)
    with pytest.raises(CsvProfileError, match=r"^csv_too_many_rows$"):
        parse_csv_bytes(b"Posted,Text,Value,CCY,Ref\n2026-01-02,Fictional,1.00,CAD,x\n", profile)


def test_header_inspection_has_no_detection_or_normalization():
    assert inspect_csv_headers(b"Preamble\nDate; Text;Value\n", delimiter=";", header_row=2) == (
        "Date",
        " Text",
        "Value",
    )
    with pytest.raises(CsvProfileError, match=r"^csv_headers$"):
        inspect_csv_headers(b"Date,Date\n", delimiter=",", header_row=1)


def test_profile_roundtrip_owner_only_and_no_overwrite(tmp_path, profile):
    tmp_path.chmod(0o700)
    path = tmp_path / "profile.json"
    save_profile(path, profile)
    original = path.read_bytes()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.stat().st_nlink == 1
    assert load_profile(path) == profile
    with pytest.raises(CsvProfileError, match=r"^profile_exists$"):
        save_profile(path, replace(profile, profile_id="changed"))
    assert path.read_bytes() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == ["profile.json"]


@pytest.mark.parametrize(
    "unsafe",
    [
        "symlink-file",
        "hardlink-file",
        "file-permissions",
        "parent-permissions",
        "symlink-parent",
        "source-repository",
    ],
)
def test_unsafe_profile_inputs_and_destinations(tmp_path, profile, unsafe):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    path = directory / "profile.json"
    save_profile(path, profile)
    if unsafe == "symlink-file":
        alias = directory / "alias.json"
        alias.symlink_to(path)
        path = alias
    elif unsafe == "hardlink-file":
        os.link(path, directory / "alias.json")
    elif unsafe == "file-permissions":
        path.chmod(0o644)
    elif unsafe == "parent-permissions":
        directory.chmod(0o755)
    elif unsafe == "symlink-parent":
        alias = tmp_path / "alias"
        alias.symlink_to(directory, target_is_directory=True)
        path = alias / path.name
    else:
        (directory / ".git").mkdir()
    with pytest.raises(CsvProfileError, match=r"^profile_unsafe_path$"):
        load_profile(path)
    with pytest.raises(CsvProfileError):
        save_profile(path, profile)


@pytest.mark.parametrize(
    "payload", [b'{"profile_id":"a","profile_id":"b"}', b"[]", b"{}", b"{broken", b"null"]
)
def test_invalid_profile_json_is_bounded(tmp_path, payload):
    tmp_path.chmod(0o700)
    path = tmp_path / "secret-name.json"
    path.write_bytes(payload)
    path.chmod(0o600)
    with pytest.raises(CsvProfileError) as error:
        load_profile(path)
    assert str(error.value) in {"profile_invalid_json", "profile_invalid"}
    assert "secret-name" not in str(error.value)


def test_preview_defaults_to_counts_and_requires_explicit_private_row_output(tmp_path, profile):
    tmp_path.chmod(0o700)
    path = tmp_path / "profile.json"
    save_profile(path, profile)
    source = tmp_path / "synthetic.csv"
    source.write_bytes(b"Posted,Text,Value,CCY,Ref\n2026-01-02,FICTIONAL SECRET,23.45,CAD,x\n")
    runner = CliRunner()
    result = runner.invoke(csv_profiles_app, ["preview", str(source), "--profile", str(path)])
    assert result.exit_code == 0, result.output
    assert PREVIEW_NOTICE in result.output
    assert "Valid rows: 1" in result.output
    assert "FICTIONAL SECRET" not in result.output
    assert "23.45" not in result.output
    shown = runner.invoke(
        csv_profiles_app, ["preview", str(source), "--profile", str(path), "--show-rows"]
    )
    assert shown.exit_code == 0
    assert "PRIVATE OUTPUT" in shown.output
    assert '"transaction_date": "2026-01-02"' in shown.output
    assert '"amount": "23.45"' in shown.output


@pytest.mark.parametrize("confirmation,saved", [("n", False), ("y", True)])
def test_wizard_requires_valid_preview_and_explicit_profile_save(tmp_path, confirmation, saved):
    tmp_path.chmod(0o700)
    source = tmp_path / "synthetic.csv"
    source.write_bytes(b"Date,Text,Amount\n2026-01-02,Fictional,-12.34\n")
    destination = tmp_path / "mapping.json"
    answers = f"wizard-v1\ncomma\n1\n1\n%Y-%m-%d\n2\namount\n3\ninflow_positive\nfixed\nCAD\nn\n{confirmation}\n"
    result = CliRunner().invoke(
        csv_profiles_app, ["map", str(source), "--save-to", str(destination)], input=answers
    )
    assert result.exit_code == 0, result.output
    assert "Valid rows: 1" in result.output
    assert destination.exists() is saved
    if saved:
        assert parse_csv_bytes(source.read_bytes(), load_profile(destination))[0].amount == Decimal(
            "-12.34"
        )


def test_wizard_row_failure_never_offers_save(tmp_path):
    tmp_path.chmod(0o700)
    source = tmp_path / "synthetic.csv"
    source.write_bytes(b"Date,Text,Amount\n2026-01-02,Fictional,broken\n")
    destination = tmp_path / "mapping.json"
    answers = "wizard-v1\ncomma\n1\n1\n%Y-%m-%d\n2\namount\n3\ninflow_positive\nfixed\nCAD\nn\ny\n"
    result = CliRunner().invoke(
        csv_profiles_app, ["map", str(source), "--save-to", str(destination)], input=answers
    )
    assert result.exit_code == 2
    assert "csv_amount" in result.output
    assert "Save this validated profile" not in result.output
    assert not destination.exists()


def test_profile_save_fault_leaves_no_partial_revision(tmp_path, profile, monkeypatch):
    tmp_path.chmod(0o700)
    path = tmp_path / "profile.json"

    def fail_sync(_descriptor):
        raise OSError("synthetic failure containing private-value")

    monkeypatch.setattr(module.os, "fsync", fail_sync)
    with pytest.raises(CsvProfileError) as error:
        save_profile(path, profile)
    assert str(error.value) == "profile_unsafe_path"
    assert list(tmp_path.iterdir()) == []


def test_atomic_save_collision_preserves_competing_revision(tmp_path, profile, monkeypatch):
    tmp_path.chmod(0o700)
    path = tmp_path / "profile.json"
    real_link = module.os.link

    def competing_link(source, destination, **kwargs):
        path.write_bytes(b"synthetic competing revision")
        return real_link(source, destination, **kwargs)

    monkeypatch.setattr(module.os, "link", competing_link)
    with pytest.raises(CsvProfileError, match=r"^profile_exists$"):
        save_profile(path, profile)
    assert path.read_bytes() == b"synthetic competing revision"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["profile.json"]


def test_profile_size_limit_on_read(tmp_path, monkeypatch):
    tmp_path.chmod(0o700)
    path = tmp_path / "profile.json"
    path.write_bytes(b" " * 11)
    path.chmod(0o600)
    monkeypatch.setattr(module, "MAX_PROFILE_BYTES", 10)
    with pytest.raises(CsvProfileError, match=r"^profile_too_large$"):
        load_profile(path)


def test_debit_credit_both_empty_cannot_become_zero(debit_profile):
    with pytest.raises(CsvProfileError, match=r"^csv_amount$"):
        parse_csv_bytes(
            b"Preamble\nWhen;Memo;Debit;Credit\n02/01/2026;Fictional;;\n", debit_profile
        )


def test_missing_private_parent_does_not_create_runtime_paths(tmp_path, profile):
    path = tmp_path / "absent" / "profile.json"
    with pytest.raises(CsvProfileError, match=r"^profile_unsafe_path$"):
        save_profile(path, profile)
    assert not path.parent.exists()
