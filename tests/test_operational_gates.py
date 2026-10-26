from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None):
    child_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    if env is not None:
        child_env.update({key: value for key, value in env.items() if not key.startswith("GIT_")})
    return subprocess.run(
        command,
        cwd=cwd,
        env=child_env,
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.fixture
def hostile_git_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[tuple[Path, bytes, bytes]]:
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    clean_env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    subprocess.run(
        ["git", "init", "-q"],
        cwd=ambient,
        env=clean_env,
        capture_output=True,
        text=True,
        check=True,
    )
    (ambient / "ambient.txt").write_text("ambient", encoding="utf-8")
    subprocess.run(
        ["git", "add", "ambient.txt"],
        cwd=ambient,
        env=clean_env,
        capture_output=True,
        text=True,
        check=True,
    )
    config = ambient / ".git" / "config"
    index = ambient / ".git" / "index"
    config_before = config.read_bytes()
    index_before = index.read_bytes()
    monkeypatch.setenv("GIT_DIR", str(ambient / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(ambient))
    yield ambient, config_before, index_before

    assert config.read_bytes() == config_before
    assert index.read_bytes() == index_before


def test_copied_precommit_blocks_sensitive_staged_file_without_venv(
    tmp_path: Path, hostile_git_context: tuple[Path, bytes, bytes]
) -> None:
    repo = tmp_path / "repo"
    hook = repo / "scripts" / "git-hooks" / "pre-commit"
    hook.parent.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "scripts" / "git-hooks" / "pre-commit", hook)

    initialized = _run(["git", "init", "-q"], cwd=repo)
    assert initialized.returncode == 0, initialized.stderr

    sensitive = repo / "private" / "statement.pdf"
    sensitive.parent.mkdir()
    sensitive.write_bytes(b"synthetic test payload")
    staged = _run(["git", "add", "private/statement.pdf"], cwd=repo)
    assert staged.returncode == 0, staged.stderr
    assert not (repo / ".venv").exists()

    result = _run(["bash", str(hook)], cwd=repo)
    output = result.stdout + result.stderr

    assert result.returncode != 0, output
    assert "BLOCKED" in output
    assert "statement.pdf" in output
    assert "Skipping" not in output


def test_freeze_check_continues_after_failure_and_prints_full_summary(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    hooks = scripts / "git-hooks"
    hooks.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "scripts" / "freeze-check", scripts / "freeze-check")

    (scripts / "_common.sh").write_text(
        "\n".join(
            [
                'ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"',
                'VENV_PY="$ROOT/fake-python"',
                "ensure_python_env() {",
                '  [[ "$#" -eq 1 && "$1" == "full-dev" ]] || return 91',
                "}",
                "",
            ]
        ),
        encoding="utf-8",
    )
    fake_python = repo / "fake-python"
    fake_python.write_text(
        """#!/usr/bin/env bash
printf '%s\n' "$*" >> "${GATE_LOG:?}"
if [[ "$*" == "-m ruff "* ]]; then
  exit 23
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    for relative in (
        "rebuild-db",
        "import-pdf-inbox",
        "run-app",
        "ui-smoke",
        "audit-statements",
        "delivery-check",
    ):
        script = scripts / relative
        script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        script.chmod(0o755)
    (hooks / "pre-commit").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    (hooks / "_is_export_header").write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")

    gate_log = repo / "gates.log"
    env = os.environ.copy()
    env.update(
        {
            "GATE_LOG": str(gate_log),
            "LEDGER_FREEZE_AUDIT": "0",
            "LEDGER_FREEZE_UI": "0",
        }
    )
    result = _run(["bash", "-e", str(scripts / "freeze-check")], cwd=repo, env=env)
    output = result.stdout + result.stderr

    assert result.returncode != 0, output
    assert "Freeze-check gate summary" in output
    assert "FAIL  ruff  (exit 23)" in output
    assert "FAIL  ruff-format  (exit 23)" in output
    assert "PASS  reports-list" in output
    assert "PASS  lock-consistency" in output
    assert "PASS  environment" in output
    assert "PASS  pip-check" in output
    assert "PASS  dependency-audit" in output
    assert "One or more freeze gates FAILED." in output

    calls = gate_log.read_text(encoding="utf-8")
    assert "-m ruff" in calls
    assert "-m ruff format --check" in calls
    assert "-m ledger.maintenance.environment_check" in calls
    assert "-m pip check" in calls
    assert "-m ledger.maintenance.dependency_audit" in calls


def test_freeze_check_synthetic_mode_uses_and_removes_a_disposable_database(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    hooks = scripts / "git-hooks"
    hooks.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "scripts" / "freeze-check", scripts / "freeze-check")

    (scripts / "_common.sh").write_text(
        "\n".join(
            [
                'ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"',
                'VENV_PY="$ROOT/fake-python"',
                "ensure_python_env() { :; }",
                "",
            ]
        ),
        encoding="utf-8",
    )
    fake_python = repo / "fake-python"
    fake_python.write_text(
        """#!/usr/bin/env bash
printf '%s\n' "$*" >> "${GATE_LOG:?}"
if [[ "$1" == "-" ]]; then
  touch "$2"
fi
exit 0
""",
        encoding="utf-8",
    )
    fake_python.chmod(0o755)

    for relative in (
        "rebuild-db",
        "import-pdf-inbox",
        "import-investment-inbox",
        "run-app",
        "ui-smoke",
        "audit-statements",
        "delivery-check",
    ):
        script = scripts / relative
        script.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        script.chmod(0o755)
    (scripts / "ui-smoke").write_text(
        """#!/usr/bin/env bash
printf '%s|%s\n' "${LEDGER_DB:-}" "${LEDGER_UI_SMOKE_SCREENSHOTS:-}" > "${UI_LOG:?}"
exit 0
""",
        encoding="utf-8",
    )
    (scripts / "ui-smoke").chmod(0o755)
    (hooks / "pre-commit").write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    (hooks / "_is_export_header").write_text("#!/usr/bin/env bash\nexit 1\n", encoding="utf-8")

    live_db = repo / "data" / "ledger.db"
    live_db.parent.mkdir()
    live_db.write_bytes(b"must-not-be-read")
    gate_log = repo / "gates.log"
    ui_log = repo / "ui.log"
    result = _run(
        ["bash", str(scripts / "freeze-check")],
        cwd=repo,
        env={
            "GATE_LOG": str(gate_log),
            "LEDGER_DB": str(live_db),
            "LEDGER_FREEZE_AUDIT": "0",
            "LEDGER_FREEZE_SYNTHETIC": "1",
            "LEDGER_FREEZE_UI": "1",
            "UI_LOG": str(ui_log),
        },
    )
    output = result.stdout + result.stderr

    assert result.returncode == 0, output
    calls = gate_log.read_text(encoding="utf-8").splitlines()
    builder_call = next(
        call for call in calls if call.startswith("- ") and "/ledger-freeze." in call
    )
    disposable_db = Path(builder_call.split(maxsplit=1)[1])
    config_call = (
        f"-m ledger.cli config import --db {disposable_db} --source {repo / 'configs'} --execute"
    )
    assert str(live_db) not in "\n".join(calls)
    assert disposable_db == Path(os.path.realpath(disposable_db))
    assert config_call in calls
    assert calls.index(config_call) < next(
        index for index, call in enumerate(calls) if call.startswith("-m ruff ")
    )
    ui_db, ui_screenshots = ui_log.read_text(encoding="utf-8").strip().split("|")
    assert Path(ui_db) == disposable_db
    assert Path(ui_screenshots).parent == disposable_db.parent
    assert "Synthetic freeze database:" in output
    assert not disposable_db.parent.exists()


def test_freeze_check_refuses_source_audit_in_synthetic_mode(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    scripts = repo / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "scripts" / "freeze-check", scripts / "freeze-check")
    (scripts / "_common.sh").write_text(
        "\n".join(
            [
                'ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"',
                'VENV_PY="$ROOT/fake-python"',
                "ensure_python_env() { :; }",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = _run(
        ["bash", str(scripts / "freeze-check")],
        cwd=repo,
        env={
            "LEDGER_FREEZE_AUDIT": "1",
            "LEDGER_FREEZE_SYNTHETIC": "1",
        },
    )

    assert result.returncode != 0
    assert "cannot be combined" in result.stderr
    assert "source statement audit" in result.stderr
