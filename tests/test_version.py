from __future__ import annotations

import tomllib
import zipfile
from pathlib import Path
from subprocess import run

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version
from typer.testing import CliRunner

import ledger
from ledger.cli import app


def test_package_version_matches_pyproject() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))

    assert ledger.__version__ == pyproject["project"]["version"]


def test_package_targets_only_python_3_14() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))

    requires_python = SpecifierSet(pyproject["project"]["requires-python"])
    assert Version("3.14.0") in requires_python
    assert Version("3.14.99") in requires_python
    assert Version("3.13.99") not in requires_python
    assert Version("3.15.0") not in requires_python
    python_classifiers = {
        classifier
        for classifier in pyproject["project"]["classifiers"]
        if classifier.startswith("Programming Language :: Python :: 3.")
    }
    assert python_classifiers == {"Programming Language :: Python :: 3.14"}
    assert pyproject["tool"]["ruff"]["target-version"] == "py314"
    assert pyproject["tool"]["mypy"]["python_version"] == "3.14"


def test_built_wheel_metadata_targets_only_python_3_14(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    result = run(
        [
            str(repo_root / ".venv/bin/python"),
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(tmp_path),
            str(repo_root),
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    wheel = next(tmp_path.glob(f"ledger-{ledger.__version__}-*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        metadata = archive.read(f"ledger-{ledger.__version__}.dist-info/METADATA").decode("utf-8")
    requires_python_line = next(
        line.removeprefix("Requires-Python: ")
        for line in metadata.splitlines()
        if line.startswith("Requires-Python: ")
    )
    requires_python = SpecifierSet(requires_python_line)
    assert Version("3.14.0") in requires_python
    assert Version("3.14.99") in requires_python
    assert Version("3.13.99") not in requires_python
    assert Version("3.15.0") not in requires_python


def test_package_declares_current_stable_dependency_lines() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    declarations = {
        Requirement(raw).name.lower(): Requirement(raw)
        for raw in pyproject["project"]["dependencies"]
    }
    extras = pyproject["project"]["optional-dependencies"]
    for requirements in extras.values():
        declarations.update(
            {Requirement(raw).name.lower(): Requirement(raw) for raw in requirements}
        )

    expected = {
        "mcp": "<3,>=2.2",
        "rich": "<16,>=15.0",
        "pyarrow": "<26,>=25.0.1",
        "typer": "<1,>=0.27",
        "pandas": "<4,>=3.0",
        "sqlalchemy": "<3,>=2.0.52",
        "streamlit": "==1.66.1",
        "plotly": "<7,>=6.9",
        "polars": "<2,>=1.43",
        "pytest": "<10,>=9.1",
        "playwright": "==1.62.0+ledger.1",
        "ruff": "<1,>=0.16",
        "mypy": "<3,>=2.3",
    }
    assert {name: str(declarations[name].specifier) for name in expected} == expected


def test_cli_version_uses_package_version() -> None:
    result = CliRunner().invoke(app, ["version"])

    assert result.exit_code == 0, result.output
    assert f"Ledger v{ledger.__version__}" in result.output
