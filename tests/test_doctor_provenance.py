"""Doctor's legacy row hash proves core-code provenance, not private interpretation."""

import sqlite3
from contextlib import closing

import pytest

from ledger.maintenance import doctor as doctor_module
from ledger.maintenance.doctor import DatabaseDoctor
from ledger.utils.hashing import UNKNOWN_VERSION


@pytest.fixture
def provenance_database(tmp_path):
    path = tmp_path / "synthetic-provenance.db"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            "CREATE TABLE transactions (txn_id TEXT PRIMARY KEY, parser_version TEXT)"
        )
    return path


def _rows(path, versions):
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executemany(
            "INSERT INTO transactions VALUES (?, ?)",
            [(f"synthetic-{index}", version) for index, version in enumerate(versions)],
        )


def test_different_hash_reports_core_scope_without_claiming_older_adapter_code(
    provenance_database, monkeypatch
):
    _rows(provenance_database, ["different-core", "current-core", UNKNOWN_VERSION])
    monkeypatch.setattr(doctor_module, "script_version", lambda: "current-core")
    finding = DatabaseDoctor(provenance_database)._check_parser_version_drift()
    assert finding is not None
    assert finding.check == "parser_version_drift"
    assert finding.count == 2
    assert finding.repairable is False
    assert "core-code provenance" in finding.summary
    assert "older" not in finding.summary
    assert "private adapters" in finding.remedy
    assert "profiles" in finding.remedy
    assert "data/processed" not in finding.remedy
    assert any("core-code provenance unavailable" in item for item in finding.details)


@pytest.mark.parametrize("stored", ["current-core", "different-core", UNKNOWN_VERSION, None])
def test_core_hash_never_claims_adapter_or_profile_freshness(
    provenance_database, monkeypatch, stored
):
    _rows(provenance_database, [stored])
    monkeypatch.setattr(doctor_module, "script_version", lambda: "current-core")
    doctor = DatabaseDoctor(provenance_database)
    finding = doctor._check_import_interpretation_freshness()
    assert finding is not None
    assert finding.check == "adapter_profile_freshness_unavailable"
    assert finding.severity == "low"
    assert finding.count == 1
    assert finding.repairable is False
    assert "unavailable" in finding.summary
    assert "verified import receipt" in finding.remedy
    with closing(sqlite3.connect(provenance_database)) as connection:
        assert connection.execute("SELECT txn_id, parser_version FROM transactions").fetchall() == [
            ("synthetic-0", stored)
        ]


def test_unknown_current_core_hash_does_not_treat_unknown_rows_as_a_match(
    provenance_database, monkeypatch
):
    _rows(provenance_database, [UNKNOWN_VERSION])
    monkeypatch.setattr(doctor_module, "script_version", lambda: UNKNOWN_VERSION)
    finding = DatabaseDoctor(provenance_database)._check_parser_version_drift()
    assert finding is not None
    assert finding.count == 1
    assert "unavailable" in finding.summary


def test_empty_database_has_no_interpretation_freshness_finding(provenance_database):
    assert DatabaseDoctor(provenance_database)._check_import_interpretation_freshness() is None


def test_diagnose_and_serialized_report_keep_legacy_finding_fields(tmp_path, monkeypatch):
    from test_doctor_dead_columns import _insert_transaction, _prepared_db

    path = _prepared_db(tmp_path)
    _insert_transaction(path, "synthetic-row", {})
    monkeypatch.setattr(doctor_module, "script_version", lambda: "current-core")
    result = DatabaseDoctor(path, audit_runs_dir=tmp_path / "no-audits").run()
    findings = {finding["check"]: finding for finding in result.details["findings"]}
    assert {"parser_version_drift", "adapter_profile_freshness_unavailable"} <= set(findings)
    for name in ("parser_version_drift", "adapter_profile_freshness_unavailable"):
        assert set(findings[name]) == {
            "check",
            "severity",
            "summary",
            "count",
            "remedy",
            "repairable",
            "details",
        }
        assert findings[name]["repairable"] is False
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT txn_id, parser_version FROM transactions").fetchall() == [
            ("synthetic-row", "test")
        ]
