from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ledger.maintenance.lock_check import check_lock_against_pyproject


@dataclass
class _FakeDistribution:
    name: str
    version: str
    direct_url: str | None = None

    @property
    def metadata(self) -> dict[str, str]:
        return {"Name": self.name}

    def read_text(self, filename: str) -> str | None:
        if filename == "direct_url.json":
            return self.direct_url
        return None


def test_runtime_check_requires_python_3_14() -> None:
    from ledger.maintenance.environment_check import runtime_is_supported

    assert runtime_is_supported((3, 14, 0))
    assert runtime_is_supported((3, 14, 99))
    assert not runtime_is_supported((3, 15, 0))
    assert not runtime_is_supported((3, 13, 9))


def test_environment_drift_reports_every_canonicalized_difference() -> None:
    from ledger.maintenance.environment_check import compare_environment, format_environment_report

    drift = compare_environment(
        lock_pins={
            "Missing_Pkg": "1.0",
            "Mixed.Name": "2.0",
            "same-package": "4.0",
        },
        installed={
            "mixed-name": "2.1",
            "Same_Package": "4.0",
            "Extra.Pkg": "3.0",
        },
    )

    assert drift.missing == {"missing-pkg": "1.0"}
    assert drift.mismatched == {"mixed-name": ("2.0", "2.1")}
    assert drift.unexpected == {"extra-pkg": "3.0"}
    assert not drift.ok

    report = format_environment_report(drift)
    assert "missing-pkg==1.0" in report
    assert "mixed-name: lock==2.0, installed==2.1" in report
    assert "extra-pkg==3.0" in report


def test_installed_distribution_collection_excludes_editable_projects() -> None:
    from ledger.maintenance.environment_check import installed_non_editable_distributions

    distributions = [
        _FakeDistribution(
            "Local_Project",
            "9.9",
            '{"dir_info": {"editable": true}, "url": "file:///tmp/local"}',
        ),
        _FakeDistribution("Regular.Project", "1.2"),
    ]

    assert installed_non_editable_distributions(distributions) == {"regular-project": "1.2"}


def test_pinned_wheel_source_check_requires_exact_archive_hashes() -> None:
    from ledger.maintenance.environment_check import pinned_wheel_source_violations

    trusted = [
        _FakeDistribution(
            "streamlit",
            "1.66.1",
            """{"archive_info":{"hashes":{"sha256":"29607190a589fe1fa9bd7e154635d335d47dc09b4010c6cc273daa6e4430ee31"}},"url":"file:///synthetic/streamlit.whl"}""",
        ),
        _FakeDistribution(
            "playwright",
            "1.62.0+ledger.1",
            """{"archive_info":{"hashes":{"sha256":"3c9c9e0ce4ece2ade7d611735327877a7a32234a2cc7b4ab5403768d66ade04a"}},"url":"file:///synthetic/playwright.whl"}""",
        ),
    ]
    untrusted = [
        _FakeDistribution("streamlit", "1.66.1"),
        _FakeDistribution(
            "playwright",
            "1.62.0+ledger.1",
            '{"archive_info":{"hashes":{"sha256":"wrong"}},"url":"https://example.invalid"}',
        ),
    ]

    assert pinned_wheel_source_violations(trusted) == {}
    assert pinned_wheel_source_violations(untrusted) == {
        "playwright": "installed archive hash is not the audited runtime wheel hash",
        "streamlit": "installed distribution has no direct wheel provenance",
    }


def test_live_non_editable_environment_exactly_matches_lock() -> None:
    from ledger.maintenance.environment_check import check_environment, format_environment_report

    selected = os.environ.get("LEDGER_ALLOWED_ADAPTER_CONFIG")
    drift = check_environment(allowed_adapter_config=Path(selected) if selected else None)

    assert drift.ok, format_environment_report(drift)


def test_lock_consistency_fails_for_applicable_dependency_missing_from_lock() -> None:
    violations = check_lock_against_pyproject(
        pyproject_deps=[
            "Required_Pkg>=1",
            "WindowsOnly>=2; sys_platform == 'win32'",
        ],
        lock_pins={},
    )

    assert violations == [
        {
            "name": "Required_Pkg",
            "pin": "<missing>",
            "specifier": ">=1",
        }
    ]
