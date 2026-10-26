from __future__ import annotations

from pathlib import Path

import pytest

from ledger.maintenance import dependency_audit
from ledger.maintenance.dependency_audit import (
    LOCAL_DERIVATIVE_VERSIONS,
    UPSTREAM_PARENT_VERSIONS,
    DependencyAuditError,
    public_audit_versions,
    validate_audit_payload,
)
from ledger.maintenance.lock_check import LOCK_PATH, _read_lock_pins


def _clean_payload(versions=None):
    return {
        "dependencies": [
            {"name": name, "version": version, "vulns": []}
            for name, version in (versions or public_audit_versions()).items()
        ],
        "fixes": [],
    }


def test_public_audit_covers_every_core_lock_pin_and_reviewed_upstream_parent():
    locked = _read_lock_pins(LOCK_PATH)
    versions = public_audit_versions()
    assert versions == {**locked, **UPSTREAM_PARENT_VERSIONS}
    assert {name: locked[name] for name in LOCAL_DERIVATIVE_VERSIONS} == LOCAL_DERIVATIVE_VERSIONS
    assert versions["streamlit"] == locked["streamlit"]
    validate_audit_payload(_clean_payload(), expected_skips=frozenset(), expected_versions=versions)


@pytest.mark.parametrize(
    "failure",
    ["missing", "extra", "version", "skip", "vulnerability", "duplicate", "duplicate-skip"],
)
def test_public_audit_requires_exact_complete_clean_evidence(failure):
    versions = {"safe-child": "1.0", "streamlit": "1.66.1"}
    payload = _clean_payload(versions)
    rows = payload["dependencies"]
    if failure == "missing":
        rows.pop()
    elif failure == "extra":
        rows.append({"name": "unexpected", "version": "1.0", "vulns": []})
    elif failure == "version":
        rows[0]["version"] = "2.0"
    elif failure == "skip":
        rows[1] = {"name": "streamlit", "skip_reason": "synthetic failure"}
    elif failure == "vulnerability":
        rows[1]["vulns"] = [{"id": "SYNTHETIC-STREAMLIT"}]
    elif failure == "duplicate":
        rows.append(dict(rows[0]))
    elif failure == "duplicate-skip":
        rows.append({"name": "safe-child", "skip_reason": "synthetic failure"})
    with pytest.raises(DependencyAuditError):
        validate_audit_payload(payload, expected_skips=frozenset(), expected_versions=versions)


@pytest.mark.parametrize("target", [LOCK_PATH, dependency_audit.UPSTREAM_REQUIREMENTS])
def test_changed_derivative_lock_or_parent_file_requires_explicit_review(monkeypatch, target):
    actual = dependency_audit._read_lock_pins

    def changed(path):
        values = actual(path)
        if path == target:
            values["playwright"] = "0.0"
        return values

    monkeypatch.setattr(dependency_audit, "_read_lock_pins", changed)
    with pytest.raises(DependencyAuditError):
        public_audit_versions()


def test_network_audit_uses_only_public_lock_input_and_never_installed_inventory(monkeypatch):
    import importlib.metadata

    expected = public_audit_versions()
    observed = []
    monkeypatch.setattr(
        importlib.metadata, "distributions", lambda **_: pytest.fail("Installed names enumerated")
    )

    def audit(arguments):
        assert "--local" not in arguments
        assert {"--no-deps", "--disable-pip", "--strict"} <= set(arguments)
        path = Path(arguments[arguments.index("--requirement") + 1])
        pins = _read_lock_pins(path)
        assert pins == expected
        assert path.stat().st_mode & 0o777 == 0o600
        assert "ledger" not in pins
        assert all("+ledger" not in version for version in pins.values())
        observed.append(path)
        return _clean_payload(pins)

    monkeypatch.setattr(dependency_audit, "_run_pip_audit", audit)
    assert dependency_audit.main() == 0
    assert len(observed) == 1
    assert not observed[0].exists()
