#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_PY="$ROOT/.venv/bin/python"
PYTHON_BIN="${LEDGER_PYTHON:-python3.14}"
WHEEL_DIR="${LEDGER_WHEEL_DIR:-$ROOT/.ledger-downstream-wheels}"

_python_env_ready() {
  local profile="$1"
  case "$profile" in
    core)
      "$VENV_PY" - "$ROOT" <<'PY' >/dev/null 2>&1
from importlib.metadata import version as distribution_version
from pathlib import Path
import sys

import ledger
import ledger.audit.statements
import ledger.cli
import ledger.imports.pdf.workflow
import ledger.investments.workflow

if sys.version_info[:2] != (3, 14):
    raise RuntimeError("Ledger requires Python 3.14")
expected_source = Path(sys.argv[1]).resolve() / "src" / "ledger"
Path(ledger.__file__).resolve().relative_to(expected_source)
if distribution_version("ledger") != ledger.__version__:
    raise RuntimeError("ledger distribution metadata does not match source version")
PY
      ;;
    analysis)
      "$VENV_PY" - "$ROOT" <<'PY' >/dev/null 2>&1
from importlib.metadata import version as distribution_version
from pathlib import Path
from polars import DataFrame
import sys

import ledger
import ledger.cli
import ledger.maintenance.rebuild_guard
import ledger.maintenance.rebuild_promote

if sys.version_info[:2] != (3, 14):
    raise RuntimeError("Ledger requires Python 3.14")
expected_source = Path(sys.argv[1]).resolve() / "src" / "ledger"
Path(ledger.__file__).resolve().relative_to(expected_source)
if distribution_version("ledger") != ledger.__version__:
    raise RuntimeError("ledger distribution metadata does not match source version")
PY
      ;;
    ui)
      "$VENV_PY" - "$ROOT" <<'PY' >/dev/null 2>&1
from importlib.metadata import distribution, version as distribution_version
from pathlib import Path
import sys

import ledger
import ledger.ui.context
import ledger.ui.pages.accounts
import ledger.ui.pages.admin_data
import ledger.ui.pages.budgets_goals
import ledger.ui.pages.categories
import ledger.ui.pages.import_center
import ledger.ui.pages.insights
import ledger.ui.pages.investments
import ledger.ui.pages.overview
import ledger.ui.pages.review_queue
import ledger.ui.pages.rules_settings
import ledger.ui.pages.transactions
from ledger.maintenance.environment_check import pinned_wheel_source_violations
import plotly.express
import plotly.graph_objects
import streamlit.web.cli

if sys.version_info[:2] != (3, 14):
    raise RuntimeError("Ledger requires Python 3.14")
expected_source = Path(sys.argv[1]).resolve() / "src" / "ledger"
Path(ledger.__file__).resolve().relative_to(expected_source)
if distribution_version("ledger") != ledger.__version__:
    raise RuntimeError("ledger distribution metadata does not match source version")
if distribution_version("streamlit") != "1.66.1":
    raise RuntimeError("ui profile requires Ledger's pinned official Streamlit wheel")
if pinned_wheel_source_violations([distribution("streamlit")]):
    raise RuntimeError("ui profile requires exact Streamlit wheel provenance")
PY
      ;;
    full-dev)
      "$VENV_PY" - "$ROOT" <<'PY' >/dev/null 2>&1
from importlib.metadata import distribution, version as distribution_version
from pathlib import Path
from polars import DataFrame
import sys

import ledger
import ledger.audit.statements
import ledger.cli
import ledger.imports.pdf.workflow
import ledger.investments.workflow
import ledger.maintenance.environment_check
from ledger.maintenance.environment_check import pinned_wheel_source_violations
import ledger.maintenance.lock_check
import ledger.maintenance.rebuild_guard
import ledger.maintenance.rebuild_promote
import ledger.ui.context
import ledger.ui.pages.accounts
import ledger.ui.pages.admin_data
import ledger.ui.pages.budgets_goals
import ledger.ui.pages.categories
import ledger.ui.pages.import_center
import ledger.ui.pages.insights
import ledger.ui.pages.investments
import ledger.ui.pages.overview
import ledger.ui.pages.review_queue
import ledger.ui.pages.rules_settings
import ledger.ui.pages.transactions
import ledger.ui.smoke
import mypy.__main__
import pip_audit.__main__
import playwright.__main__
import playwright.sync_api
import plotly.express
import plotly.graph_objects
import pytest.__main__
import pytest_cov.plugin
import ruff.__main__
import streamlit.web.cli

if sys.version_info[:2] != (3, 14):
    raise RuntimeError("Ledger requires Python 3.14")
expected_source = Path(sys.argv[1]).resolve() / "src" / "ledger"
Path(ledger.__file__).resolve().relative_to(expected_source)
if distribution_version("ledger") != ledger.__version__:
    raise RuntimeError("ledger distribution metadata does not match source version")
if distribution_version("streamlit") != "1.66.1":
    raise RuntimeError("full-dev profile requires Ledger's pinned official Streamlit wheel")
if distribution_version("playwright") != "1.62.0+ledger.1":
    raise RuntimeError("full-dev profile requires Ledger's audited Playwright derivative")
runtime_wheels = [distribution(name) for name in ("streamlit", "playwright")]
if pinned_wheel_source_violations(runtime_wheels):
    raise RuntimeError("full-dev profile requires exact runtime wheel provenance")
PY
      ;;
  esac
}

ensure_python_env() {
  local profile="${1:-full-dev}"
  local install_target
  case "$profile" in
    core) install_target="." ;;
    analysis) install_target=".[analysis]" ;;
    ui) install_target=".[ui]" ;;
    full-dev) install_target=".[dev,analysis,ui]" ;;
    *)
      echo "Unknown bootstrap profile: $profile" >&2
      return 2
      ;;
  esac

  # Recreate the venv if it is missing, or if it was created for a different
  # repo path (e.g. the repo was moved/renamed), which breaks pip's interpreter.
  if [[ ! -x "$VENV_PY" ]] || ! grep -q "$ROOT/.venv" "$ROOT/.venv/pyvenv.cfg" 2>/dev/null; then
    rm -rf "$ROOT/.venv"
    "$PYTHON_BIN" -m venv "$ROOT/.venv"
  fi

  if _python_env_ready "$profile"; then
    return 0
  fi

  (
    cd "$ROOT"
    if [[ "$profile" == "ui" || "$profile" == "full-dev" ]]; then
      "$ROOT/scripts/build-downstream-wheels" "$WHEEL_DIR"
      "$VENV_PY" -m pip install --no-index --no-deps --force-reinstall \
        "$WHEEL_DIR/streamlit-1.66.1-py3-none-any.whl"
    fi
    if [[ "$profile" == "full-dev" ]]; then
      "$VENV_PY" -m pip install --no-index --no-deps --force-reinstall \
        "$WHEEL_DIR/playwright-1.62.0+ledger.1-py3-none-macosx_26_0_arm64.whl"
    fi
    if [[ "$profile" == "ui" || "$profile" == "full-dev" ]]; then
      "$VENV_PY" -m pip install --find-links "$WHEEL_DIR" \
        -e "$install_target"
    else
      "$VENV_PY" -m pip install -e "$install_target"
    fi
  )
  if ! _python_env_ready "$profile"; then
    echo "Python environment is still unhealthy after reinstalling profile: $profile" >&2
    return 1
  fi
}
