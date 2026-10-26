"""
Tests for CLI default resolution helpers.
"""

from pathlib import Path

import pytest
import typer

from ledger.cli import resolve_db_path


def test_workflow_repo_root_is_the_actual_repo_root():
    """The PDF-import default paths (--pdf-dir/--db/--processed-dir/…) hang off
    workflow.REPO_ROOT; it must resolve to the repo root (the dir holding
    pyproject.toml + src/ledger), not one level above it. A wrong index here
    silently sends `import-pdf-inbox stage` to a non-existent inbox."""
    from ledger.imports.pdf.workflow import REPO_ROOT

    assert (REPO_ROOT / "pyproject.toml").is_file()
    assert (REPO_ROOT / "src" / "ledger" / "imports" / "pdf" / "workflow.py").is_file()


def test_resolve_db_path_uses_explicit_db(tmp_path: Path):
    db_path = tmp_path / "ledger.db"
    db_path.touch()

    assert resolve_db_path(db_path) == db_path.resolve()


def test_resolve_db_path_uses_defaults_file(tmp_path: Path):
    db_path = tmp_path / "ledger.db"
    db_path.touch()
    defaults_path = tmp_path / "defaults.toml"
    defaults_path.write_text(
        f"""
[defaults]
db = "{db_path}"
""",
        encoding="utf-8",
    )

    assert resolve_db_path(None, defaults_path) == db_path.resolve()


def test_resolve_db_path_requires_db_when_no_default(tmp_path: Path):
    defaults_path = tmp_path / "defaults.toml"
    defaults_path.write_text("[defaults]\n", encoding="utf-8")

    with pytest.raises(typer.Exit):
        resolve_db_path(None, defaults_path)
