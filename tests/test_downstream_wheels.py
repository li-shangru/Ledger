"""Release contract for official Streamlit and the private Playwright wheel."""

from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

from packaging.requirements import Requirement

from ledger.maintenance.lock_check import _read_lock_pins, _read_pyproject_deps

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_dependency_contract_selects_verified_runtime_wheels_and_security_fixes() -> None:
    dependencies = {Requirement(value).name: Requirement(value) for value in _read_pyproject_deps()}
    pins = _read_lock_pins()

    assert str(dependencies["streamlit"].specifier) == "==1.66.1"
    assert str(dependencies["playwright"].specifier) == "==1.62.0+ledger.1"
    assert str(dependencies["pyarrow"].specifier) == "<26,>=25.0.1"
    assert pins["streamlit"] == "1.66.1"
    assert pins["pillow"] == "13.0.0"
    assert pins["mcp"] == pins["mcp-types"] == "2.2.0"
    assert pins["certifi"] == "2026.10.19"
    assert pins["pyjwt"] == "2.15.1"
    assert pins["urllib3"] == "2.8.0"
    assert pins["playwright"] == "1.62.0+ledger.1"
    assert pins["pyarrow"] == "25.0.1"
    assert pins["starlette"] == "1.6.0"
    assert pins["websockets"] == "17.0.1"
    assert pins["pyee"] == "14.0.0"


def test_downstream_manifest_records_exact_sources_outputs_and_metadata_deltas() -> None:
    manifest = tomllib.loads(
        (REPO_ROOT / "vendor/downstream-wheels/manifest.toml").read_text(encoding="utf-8")
    )

    assert manifest["build"] == {
        "source_date_epoch": 1700000000,
        "wheel": "0.48.0",
        "wheel_sha256": "3217dcc807155e45db462d7ef2431f5ddda0d7273b700d05a67b271ceb1287ab",
        "packaging": "26.3",
        "packaging_sha256": ("d7193f7c8e4e93f444fde0262bf90af30e16fa0ad0ad44cb553c87339b23cd1c"),
    }
    streamlit = manifest["streamlit"]
    assert streamlit["upstream_sha256"] == (
        "29607190a589fe1fa9bd7e154635d335d47dc09b4010c6cc273daa6e4430ee31"
    )
    assert "downstream_sha256" not in streamlit
    assert streamlit["version"] == streamlit["upstream_version"] == "1.66.1"
    assert streamlit["metadata_replacements"] == []

    playwright = manifest["playwright"]
    assert playwright["upstream_sha256"] == (
        "db755ab27db21a04186f1fe8169888e42356086e439b1059b923ef417f0b6034"
    )
    assert playwright["downstream_sha256"] == (
        "3c9c9e0ce4ece2ade7d611735327877a7a32234a2cc7b4ab5403768d66ade04a"
    )
    assert playwright["version"] == "1.62.0+ledger.1"
    assert playwright["tag"] == "py3-none-macosx_26_0_arm64"
    assert playwright["native_node_minos"] == "13.5"
    assert playwright["declared_macos_floor"] == "26.0"
    assert playwright["metadata_replacements"] == [
        "Version: 1.62.0 -> Version: 1.62.0+ledger.1",
        "Requires-Dist: pyee<14,>=13 -> Requires-Dist: pyee<15,>=14",
        "Tag: py3-none-any -> Tag: py3-none-macosx_26_0_arm64",
    ]


def test_builder_and_notice_enforce_no_fallback_and_license_custody() -> None:
    builder = REPO_ROOT / "scripts/build-downstream-wheels"
    content = builder.read_text(encoding="utf-8")
    notice = (REPO_ROOT / "vendor/downstream-wheels/README.md").read_text(encoding="utf-8")
    license_text = (REPO_ROOT / "vendor/downstream-wheels/LICENSE-APACHE-2.0").read_text(
        encoding="utf-8"
    )

    assert builder.stat().st_mode & 0o111
    assert "--no-index" in content
    assert "--find-links" in content
    common = (REPO_ROOT / "scripts/_common.sh").read_text(encoding="utf-8")
    assert common.count("pip install --no-index --no-deps") == 2
    assert "streamlit-1.66.1-py3-none-any.whl" in common
    assert "playwright-1.62.0+ledger.1-py3-none-macosx_26_0_arm64.whl" in common
    assert "-m zipfile --test" in content
    assert "cp -pR" in content
    assert "playwright/driver/node" in content
    assert "vtool -show-build" in content
    assert "Do not upload it to a package index under the upstream project name." in notice
    assert "modified distribution" in notice
    assert "Apache License" in license_text
    assert "Version 2.0, January 2004" in license_text


def test_builder_refuses_replacing_an_existing_artifact_before_building(tmp_path: Path) -> None:
    artifact = tmp_path / "streamlit-1.66.1-py3-none-any.whl"
    artifact.write_bytes(b"synthetic existing artifact")
    marker = tmp_path / "build-attempted"
    interpreter = tmp_path / "python-probe"
    interpreter.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\nfrom pathlib import Path\n"
        f"if sys.argv[1:3] == ['-m', 'venv']:\n    Path({str(marker)!r}).touch()\n    sys.exit(98)\n"
        "os.execv(sys.executable, [sys.executable, *sys.argv[1:]])\n"
    )
    interpreter.chmod(0o700)
    completed = subprocess.run(
        [str(REPO_ROOT / "scripts/build-downstream-wheels"), str(tmp_path)],
        env={**os.environ, "LEDGER_PYTHON": str(interpreter)},
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert completed.returncode == 1
    assert "Refusing to replace an existing runtime artifact" in completed.stderr
    assert artifact.read_bytes() == b"synthetic existing artifact"
    assert not marker.exists()
