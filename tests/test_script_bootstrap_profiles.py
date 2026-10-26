from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _copy_bootstrap_scripts(scripts: Path) -> None:
    shutil.copy2(REPO_ROOT / "scripts" / "_common.sh", scripts / "_common.sh")
    builder = scripts / "build-downstream-wheels"
    builder.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    builder.chmod(0o755)


@pytest.mark.parametrize(
    ("profile", "expected_imports", "expected_install"),
    [
        (
            "core",
            {
                "from importlib.metadata import version as distribution_version",
                "from pathlib import Path",
                "import sys",
                "import ledger",
                "import ledger.audit.statements",
                "import ledger.cli",
                "import ledger.imports.pdf.workflow",
                "import ledger.investments.workflow",
            },
            ".",
        ),
        (
            "analysis",
            {
                "from importlib.metadata import version as distribution_version",
                "from pathlib import Path",
                "from polars import DataFrame",
                "import sys",
                "import ledger",
                "import ledger.cli",
                "import ledger.maintenance.rebuild_guard",
                "import ledger.maintenance.rebuild_promote",
            },
            ".[analysis]",
        ),
        (
            "ui",
            {
                "from importlib.metadata import distribution, version as distribution_version",
                "from pathlib import Path",
                "import sys",
                "import ledger",
                "import ledger.ui.context",
                "import ledger.ui.pages.accounts",
                "import ledger.ui.pages.admin_data",
                "import ledger.ui.pages.budgets_goals",
                "import ledger.ui.pages.categories",
                "import ledger.ui.pages.import_center",
                "import ledger.ui.pages.insights",
                "import ledger.ui.pages.investments",
                "import ledger.ui.pages.overview",
                "import ledger.ui.pages.review_queue",
                "import ledger.ui.pages.rules_settings",
                "import ledger.ui.pages.transactions",
                "from ledger.maintenance.environment_check import pinned_wheel_source_violations",
                "import plotly.express",
                "import plotly.graph_objects",
                "import streamlit.web.cli",
            },
            ".[ui]",
        ),
        (
            "full-dev",
            {
                "from importlib.metadata import distribution, version as distribution_version",
                "from pathlib import Path",
                "from polars import DataFrame",
                "import sys",
                "import ledger",
                "import ledger.audit.statements",
                "import ledger.cli",
                "import ledger.imports.pdf.workflow",
                "import ledger.investments.workflow",
                "import ledger.maintenance.environment_check",
                "from ledger.maintenance.environment_check import pinned_wheel_source_violations",
                "import ledger.maintenance.lock_check",
                "import ledger.maintenance.rebuild_guard",
                "import ledger.maintenance.rebuild_promote",
                "import ledger.ui.context",
                "import ledger.ui.pages.accounts",
                "import ledger.ui.pages.admin_data",
                "import ledger.ui.pages.budgets_goals",
                "import ledger.ui.pages.categories",
                "import ledger.ui.pages.import_center",
                "import ledger.ui.pages.insights",
                "import ledger.ui.pages.investments",
                "import ledger.ui.pages.overview",
                "import ledger.ui.pages.review_queue",
                "import ledger.ui.pages.rules_settings",
                "import ledger.ui.pages.transactions",
                "import ledger.ui.smoke",
                "import mypy.__main__",
                "import pip_audit.__main__",
                "import playwright.__main__",
                "import playwright.sync_api",
                "import plotly.express",
                "import plotly.graph_objects",
                "import pytest.__main__",
                "import pytest_cov.plugin",
                "import ruff.__main__",
                "import streamlit.web.cli",
            },
            ".[dev,analysis,ui]",
        ),
    ],
)
def test_bootstrap_profile_uses_minimal_probe_and_extra_set(
    tmp_path: Path,
    profile: str,
    expected_imports: set[str],
    expected_install: str,
) -> None:
    repo = tmp_path / profile
    scripts = repo / "scripts"
    venv = repo / ".venv"
    scripts.mkdir(parents=True)
    (venv / "bin").mkdir(parents=True)
    _copy_bootstrap_scripts(scripts)
    (venv / "pyvenv.cfg").write_text(f"command = {venv}/bin/python\n", encoding="utf-8")

    fake_python = venv / "bin" / "python"
    fake_python.write_text(
        """#!/usr/bin/env bash
printf 'ARGS:' >> "${BOOTSTRAP_LOG:?}"
printf ' <%s>' "$@" >> "$BOOTSTRAP_LOG"
printf '\n' >> "$BOOTSTRAP_LOG"
if [[ "${1:-}" == "-" ]]; then
  cat >> "$BOOTSTRAP_LOG"
  printf 'END-PROBE\n' >> "$BOOTSTRAP_LOG"
  [[ -f "${PROFILE_READY:?}" ]]
  exit
fi
touch "${PROFILE_READY:?}"
exit 0
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    log = repo / "bootstrap.log"
    env = os.environ.copy()
    env["BOOTSTRAP_LOG"] = str(log)
    env["PROFILE_READY"] = str(repo / "profile-ready")
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{scripts / "_common.sh"}"; ensure_python_env "{profile}"',
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

    calls = log.read_text(encoding="utf-8")
    probe = calls.split("END-PROBE", 1)[0]
    imported_statements = {
        line.strip()
        for line in probe.splitlines()
        if line.startswith("import ") or line.startswith("from ")
    }
    assert imported_statements == expected_imports
    assert 'distribution_version("ledger")' in probe
    assert "ledger.__version__" in probe
    assert "relative_to(expected_source)" in probe
    assert "sys.version_info[:2] != (3, 14)" in probe
    if profile in {"ui", "full-dev"}:
        assert 'distribution_version("streamlit") != "1.66.1"' in probe
        assert "pinned_wheel_source_violations" in probe
        assert "<--find-links>" in calls
    if profile == "full-dev":
        assert 'distribution_version("playwright") != "1.62.0+ledger.1"' in probe
    assert f"<{expected_install}>" in calls
    assert calls.count("END-PROBE") == 2


@pytest.mark.parametrize(
    ("profile", "provenance_state", "expected_parent_installs"),
    [
        ("ui", "missing-direct-url", 1),
        ("full-dev", "wrong-archive-hash", 2),
    ],
)
def test_same_version_untrusted_derivative_forces_exact_no_index_parent_reinstall(
    tmp_path: Path,
    profile: str,
    provenance_state: str,
    expected_parent_installs: int,
) -> None:
    repo = tmp_path / provenance_state
    scripts = repo / "scripts"
    venv = repo / ".venv"
    scripts.mkdir(parents=True)
    (venv / "bin").mkdir(parents=True)
    _copy_bootstrap_scripts(scripts)
    (venv / "pyvenv.cfg").write_text(f"command = {venv}/bin/python\n", encoding="utf-8")

    fake_python = venv / "bin" / "python"
    fake_python.write_text(
        """#!/usr/bin/env bash
printf 'ARGS:' >> "${BOOTSTRAP_LOG:?}"
printf ' <%s>' "$@" >> "$BOOTSTRAP_LOG"
printf '\n' >> "$BOOTSTRAP_LOG"
if [[ "${1:-}" == "-" ]]; then
  probe="$(cat)"
  printf '%s\nEND-PROBE:%s\n' "$probe" "${PROVENANCE_STATE:?}" >> "$BOOTSTRAP_LOG"
  [[ -f "${PROVENANCE_REPAIRED:?}" ]]
  exit
fi
if [[ "$*" == *"pip install --no-index --no-deps --force-reinstall"* ]]; then
  touch "$PROVENANCE_REPAIRED"
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    log = repo / "bootstrap.log"
    env = os.environ.copy()
    env.update(
        {
            "BOOTSTRAP_LOG": str(log),
            "PROVENANCE_REPAIRED": str(repo / "provenance-repaired"),
            "PROVENANCE_STATE": provenance_state,
        }
    )
    result = subprocess.run(
        ["bash", "-c", f'source "{scripts / "_common.sh"}"; ensure_python_env "{profile}"'],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    calls = log.read_text(encoding="utf-8")
    assert calls.count("END-PROBE:") == 2
    assert calls.count("<pip> <install> <--no-index> <--no-deps>") == expected_parent_installs
    assert calls.count("<--force-reinstall>") == expected_parent_installs
    assert "pinned_wheel_source_violations" in calls
    assert 'distribution_version("streamlit") != "1.66.1"' in calls
    if profile == "full-dev":
        assert 'distribution_version("playwright") != "1.62.0+ledger.1"' in calls


@pytest.mark.parametrize(
    ("profile", "broken_probe_line", "expected_install"),
    [
        ("core", "import ledger.imports.pdf.workflow", "."),
        ("analysis", "import ledger.maintenance.rebuild_promote", ".[analysis]"),
        ("ui", "import streamlit.web.cli", ".[ui]"),
        ("full-dev", "import pip_audit.__main__", ".[dev,analysis,ui]"),
    ],
)
def test_persistently_damaged_entrypoint_fails_after_profile_reinstall(
    tmp_path: Path,
    profile: str,
    broken_probe_line: str,
    expected_install: str,
) -> None:
    repo = tmp_path / f"damaged-{profile}"
    scripts = repo / "scripts"
    venv = repo / ".venv"
    scripts.mkdir(parents=True)
    (venv / "bin").mkdir(parents=True)
    _copy_bootstrap_scripts(scripts)
    (venv / "pyvenv.cfg").write_text(f"command = {venv}/bin/python\n", encoding="utf-8")

    fake_python = venv / "bin" / "python"
    fake_python.write_text(
        """#!/usr/bin/env bash
printf 'ARGS:' >> "${BOOTSTRAP_LOG:?}"
printf ' <%s>' "$@" >> "$BOOTSTRAP_LOG"
printf '\n' >> "$BOOTSTRAP_LOG"
if [[ "${1:-}" == "-" ]]; then
  probe="$(cat)"
  printf '%s\nEND-PROBE\n' "$probe" >> "$BOOTSTRAP_LOG"
  if grep -Fq "${BROKEN_PROBE_LINE:?}" <<< "$probe"; then
    exit 1
  fi
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    log = repo / "bootstrap.log"
    env = os.environ.copy()
    env["BOOTSTRAP_LOG"] = str(log)
    env["BROKEN_PROBE_LINE"] = broken_probe_line
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{scripts / "_common.sh"}"; ensure_python_env "{profile}"',
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0, result.stdout + result.stderr
    calls = log.read_text(encoding="utf-8")
    assert f"<{expected_install}>" in calls
    assert calls.count("END-PROBE") == 2


@pytest.mark.parametrize(
    ("metadata_version", "expect_reinstall"),
    [("1.1.0", True), ("1.2.0.dev0", False)],
)
def test_distribution_and_source_version_mismatch_controls_reinstall(
    tmp_path: Path,
    metadata_version: str,
    expect_reinstall: bool,
) -> None:
    repo = tmp_path / f"version-{metadata_version}"
    scripts = repo / "scripts"
    venv = repo / ".venv"
    fake_site = repo / "src"
    ledger_package = fake_site / "ledger"
    scripts.mkdir(parents=True)
    (venv / "bin").mkdir(parents=True)
    (ledger_package / "imports" / "pdf").mkdir(parents=True)
    (ledger_package / "investments").mkdir(parents=True)
    (ledger_package / "audit").mkdir(parents=True)
    _copy_bootstrap_scripts(scripts)
    (venv / "pyvenv.cfg").write_text(f"command = {venv}/bin/python\n", encoding="utf-8")

    (ledger_package / "__init__.py").write_text('__version__ = "1.2.0.dev0"\n', encoding="utf-8")
    (ledger_package / "cli.py").write_text("", encoding="utf-8")
    (ledger_package / "imports" / "__init__.py").write_text("", encoding="utf-8")
    (ledger_package / "imports" / "pdf" / "__init__.py").write_text("", encoding="utf-8")
    (ledger_package / "imports" / "pdf" / "workflow.py").write_text("", encoding="utf-8")
    (ledger_package / "investments" / "__init__.py").write_text("", encoding="utf-8")
    (ledger_package / "investments" / "workflow.py").write_text("", encoding="utf-8")
    (ledger_package / "audit" / "__init__.py").write_text("", encoding="utf-8")
    (ledger_package / "audit" / "statements.py").write_text("", encoding="utf-8")
    dist_info = fake_site / f"ledger-{metadata_version}.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: ledger\nVersion: {metadata_version}\n",
        encoding="utf-8",
    )

    fake_python = venv / "bin" / "python"
    fake_python.write_text(
        """#!/usr/bin/env bash
printf 'ARGS:' >> "${BOOTSTRAP_LOG:?}"
printf ' <%s>' "$@" >> "$BOOTSTRAP_LOG"
printf '\n' >> "$BOOTSTRAP_LOG"
if [[ "${1:-}" == "-" ]]; then
  exec "${REAL_PYTHON:?}" -S "$@"
fi
if [[ -n "${METADATA_PATH:-}" ]]; then
  printf 'Metadata-Version: 2.1\nName: ledger\nVersion: 1.2.0.dev0\n' > "$METADATA_PATH"
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    log = repo / "bootstrap.log"
    env = os.environ.copy()
    env["BOOTSTRAP_LOG"] = str(log)
    env["PYTHONPATH"] = str(fake_site)
    env["REAL_PYTHON"] = sys.executable
    env["METADATA_PATH"] = str(dist_info / "METADATA") if expect_reinstall else ""
    result = subprocess.run(
        [
            "bash",
            "-c",
            f'source "{scripts / "_common.sh"}"; ensure_python_env core',
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    calls = log.read_text(encoding="utf-8")
    reinstall_attempted = " <.>" in calls
    assert reinstall_attempted is expect_reinstall
    assert calls.count("ARGS: <->") == (2 if expect_reinstall else 1)


def test_matching_version_from_wrong_checkout_fails_closed_after_reinstall(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "current-repo"
    scripts = repo / "scripts"
    venv = repo / ".venv"
    wrong_site = tmp_path / "other-checkout" / "src"
    ledger_package = wrong_site / "ledger"
    scripts.mkdir(parents=True)
    (venv / "bin").mkdir(parents=True)
    (ledger_package / "imports" / "pdf").mkdir(parents=True)
    (ledger_package / "investments").mkdir(parents=True)
    (ledger_package / "audit").mkdir(parents=True)
    _copy_bootstrap_scripts(scripts)
    (venv / "pyvenv.cfg").write_text(f"command = {venv}/bin/python\n", encoding="utf-8")

    (ledger_package / "__init__.py").write_text('__version__ = "1.2.0.dev0"\n', encoding="utf-8")
    (ledger_package / "cli.py").write_text("", encoding="utf-8")
    (ledger_package / "imports" / "__init__.py").write_text("", encoding="utf-8")
    (ledger_package / "imports" / "pdf" / "__init__.py").write_text("", encoding="utf-8")
    (ledger_package / "imports" / "pdf" / "workflow.py").write_text("", encoding="utf-8")
    (ledger_package / "investments" / "__init__.py").write_text("", encoding="utf-8")
    (ledger_package / "investments" / "workflow.py").write_text("", encoding="utf-8")
    (ledger_package / "audit" / "__init__.py").write_text("", encoding="utf-8")
    (ledger_package / "audit" / "statements.py").write_text("", encoding="utf-8")
    dist_info = wrong_site / "ledger-1.2.0.dev0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: ledger\nVersion: 1.2.0.dev0\n",
        encoding="utf-8",
    )

    fake_python = venv / "bin" / "python"
    fake_python.write_text(
        """#!/usr/bin/env bash
printf 'ARGS:' >> "${BOOTSTRAP_LOG:?}"
printf ' <%s>' "$@" >> "$BOOTSTRAP_LOG"
printf '\n' >> "$BOOTSTRAP_LOG"
if [[ "${1:-}" == "-" ]]; then
  exec "${REAL_PYTHON:?}" -S "$@"
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    log = repo / "bootstrap.log"
    env = os.environ.copy()
    env.update(
        {
            "BOOTSTRAP_LOG": str(log),
            "PYTHONPATH": str(wrong_site),
            "REAL_PYTHON": sys.executable,
        }
    )
    result = subprocess.run(
        ["bash", "-c", f'source "{scripts / "_common.sh"}"; ensure_python_env core'],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0, result.stdout + result.stderr
    calls = log.read_text(encoding="utf-8")
    assert " <.>" in calls
    assert calls.count("ARGS: <->") == 2


def test_wrappers_request_their_intended_bootstrap_profiles() -> None:
    expected = {
        "scripts/import-pdf-inbox": "core",
        "scripts/import-investment-inbox": "core",
        "scripts/audit-statements": "core",
        "scripts/rebuild-db": "analysis",
        "scripts/run-app": "ui",
        "scripts/freeze-check": "full-dev",
        "scripts/ui-smoke": "full-dev",
    }

    for relative, profile in expected.items():
        content = (REPO_ROOT / relative).read_text(encoding="utf-8")
        assert re.search(rf"^ensure_python_env\s+{re.escape(profile)}$", content, re.MULTILINE), (
            f"{relative} must request the {profile!r} bootstrap profile"
        )
