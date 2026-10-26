"""Delivery failures must not be mistaken for fresh installed behavior."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def delivery_module():
    path = REPO / "scripts/ledger_delivery.py"
    assert path.is_file(), "Ledger needs an executable installed-delivery workflow"
    spec = importlib.util.spec_from_file_location("ledger_delivery_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_same_version_source_changes_invalidate_last_delivery(tmp_path: Path) -> None:
    delivery = delivery_module()
    receipt = tmp_path / "receipt.json"
    baseline = {"version": "9.0.0", "package": {"core.py": "old"}, "skills": {}}
    receipt.write_text(json.dumps({"schema": 1, "verified": baseline, "status": "passed"}))
    current = {**baseline, "package": {"core.py": "new"}}

    with pytest.raises(delivery.DeliveryError, match="source_not_delivered"):
        delivery.require_receipt(receipt, current)


def test_missing_receipt_cannot_claim_delivery(tmp_path: Path) -> None:
    delivery = delivery_module()
    with pytest.raises(delivery.DeliveryError, match="delivery_receipt_missing"):
        delivery.require_receipt(tmp_path / "absent.json", {"version": "9.0.0"})


def test_package_manifest_catches_extra_files_and_rejects_symlink(tmp_path: Path) -> None:
    delivery = delivery_module()
    root = tmp_path / "package"
    root.mkdir()
    (root / "core.py").write_text("synthetic\n")
    baseline = delivery.fingerprint(root)
    (root / "extra.txt").write_text("unexpected packaged content\n")
    assert delivery.fingerprint(root) != baseline
    (root / "link").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(delivery.DeliveryError, match="symlink_refused"):
        delivery.fingerprint(root)


def test_configuration_only_accepts_expected_executable_and_hides_runtime_values(
    tmp_path: Path,
) -> None:
    delivery = delivery_module()
    expected = tmp_path / ".venv/bin/ledger-mcp"
    expected.parent.mkdir(parents=True)
    expected.write_text("#!/synthetic/python\n")
    config = {
        "command": str(expected),
        "args": ["--db", "/private/never-open-this/owner.db"],
    }
    descriptor = delivery.check_configured_server(config, expected)
    assert "never-open" not in json.dumps(descriptor)
    assert descriptor["runtime_arguments_replaced"] is True
    config["command"] = str(tmp_path / "wrong-checkout/ledger-mcp")
    with pytest.raises(delivery.DeliveryError, match="configured_entrypoint_drift") as caught:
        delivery.check_configured_server(config, expected)
    assert "never-open" not in str(caught.value)


@pytest.mark.parametrize(
    "override",
    [
        {"env": {"PYTHONPATH": "/synthetic/other"}},
        {"args": ["--db", "/synthetic/db", "--unexpected"]},
        {"command": "ledger-mcp"},
        {"enabled": False},
        {"experimental_environment": "remote"},
        {"experimental_environment": "unknown-future-executor"},
    ],
)
def test_unsupported_configured_launch_cannot_pass(tmp_path: Path, override: dict) -> None:
    delivery = delivery_module()
    executable = tmp_path / "ledger-mcp"
    executable.touch()
    config = {"command": str(executable), "args": ["--db", "/synthetic/db"], **override}
    with pytest.raises(delivery.DeliveryError):
        delivery.check_configured_server(config, executable)


def test_claude_project_command_override_cannot_hide_behind_correct_global_command(
    tmp_path: Path,
) -> None:
    delivery = delivery_module()
    expected = tmp_path / ".venv/bin/ledger-mcp"
    codex = tmp_path / "codex.toml"
    codex.write_text(
        f'[mcp_servers.ledger]\ncommand = "{expected}"\nargs = ["--db", "/synthetic/db"]\n'
    )
    claude = tmp_path / "claude.json"
    global_server = {"command": str(expected), "args": ["--db", "/synthetic/db"]}
    claude.write_text(
        json.dumps(
            {
                "mcpServers": {"ledger": global_server},
                "projects": {
                    str(tmp_path): {
                        "mcpServers": {
                            "ledger": {
                                **global_server,
                                "command": str(tmp_path / "wrong-install/ledger-mcp"),
                            }
                        }
                    }
                },
            }
        )
    )
    with pytest.raises(delivery.DeliveryError, match="configured_entrypoint_drift"):
        delivery.configured_clients(tmp_path, codex, claude)


def test_claude_project_override_matches_filesystem_case_alias(tmp_path: Path) -> None:
    delivery = delivery_module()
    source = tmp_path / "Checkout"
    source.mkdir()
    alias = source.with_name("checkout")
    if not alias.exists() or not alias.samefile(source):
        pytest.skip("This filesystem distinguishes case; the paths are different projects.")
    expected = source / ".venv/bin/ledger-mcp"
    codex = tmp_path / "codex.toml"
    codex.write_text(
        f'[mcp_servers.ledger]\ncommand = "{expected}"\nargs = ["--db", "/synthetic/db"]\n'
    )
    claude = tmp_path / "claude.json"
    server = {"command": str(expected), "args": ["--db", "/synthetic/db"]}
    claude.write_text(
        json.dumps(
            {
                "mcpServers": {"ledger": server},
                "projects": {
                    str(alias): {
                        "mcpServers": {
                            "ledger": {**server, "command": str(tmp_path / "wrong/ledger-mcp")}
                        }
                    }
                },
            }
        )
    )
    with pytest.raises(delivery.DeliveryError, match="configured_entrypoint_drift"):
        delivery.configured_clients(source, codex, claude)


@pytest.mark.parametrize(
    "relative",
    [
        "streamlit_app.py",
        "scripts/run-app",
        "scripts/_common.sh",
        "scripts/ui-smoke",
        "AGENTS.md",
        ".gitignore",
        "docs/agent/development.md",
        "docs/agent/factory/profiles/exact-arithmetic.md",
        "docs/agent/skills/ledger-implement-ticket/SKILL.md",
    ],
)
def test_changes_to_release_or_ui_executables_invalidate_source_binding(
    tmp_path: Path,
    relative: str,
) -> None:
    import shutil

    delivery = delivery_module()
    for name in (
        "pyproject.toml",
        "README.md",
        "requirements.lock",
        "streamlit_app.py",
        "AGENTS.md",
        ".gitignore",
        "docs/README.md",
        "docs/agent/development.md",
        "docs/agent/delivery.md",
    ):
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO / name, tmp_path / name)
    (tmp_path / ".streamlit").mkdir()
    shutil.copyfile(REPO / ".streamlit/config.toml", tmp_path / ".streamlit/config.toml")
    for name in (
        "src",
        "scripts",
        "docs/agent/skills",
        "docs/agent/factory",
        "tests",
        "configs",
        "vendor",
    ):
        shutil.copytree(
            REPO / name, tmp_path / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
        )
    before = delivery.source_state(tmp_path)
    changed = tmp_path / relative
    changed.write_text(changed.read_text() + "\n# changed synthetic release input\n")
    assert delivery.source_state(tmp_path) != before


def test_skill_sync_removes_stale_files_and_reruns_without_replacing_exact_tree(
    tmp_path: Path,
) -> None:
    delivery = delivery_module()
    source = tmp_path / "canonical"
    source.mkdir()
    (source / "SKILL.md").write_text("Synthetic instructions\n")
    target = tmp_path / "skills/ledger-query"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("stale\n")
    (target / "obsolete.py").write_text("stale\n")
    delivery.sync_skill(source, target)
    assert sorted(path.name for path in target.iterdir()) == ["SKILL.md"]
    assert (target / "SKILL.md").read_text() == "Synthetic instructions\n"
    inode = target.stat().st_ino
    delivery.sync_skill(source, target)
    assert target.stat().st_ino == inode


def test_skill_sync_refuses_symlink_target_without_modifying_destination(tmp_path: Path) -> None:
    delivery = delivery_module()
    source = tmp_path / "canonical"
    source.mkdir()
    (source / "SKILL.md").write_text("new\n")
    destination = tmp_path / "unrelated"
    destination.mkdir()
    (destination / "SKILL.md").write_text("keep\n")
    target = tmp_path / "skills/ledger-query"
    target.parent.mkdir()
    target.symlink_to(destination, target_is_directory=True)
    with pytest.raises(delivery.DeliveryError, match="symlink_refused"):
        delivery.sync_skill(source, target)
    assert (destination / "SKILL.md").read_text() == "keep\n"


def test_stale_editable_metadata_detected_even_when_version_matches() -> None:
    delivery = delivery_module()
    with pytest.raises(delivery.DeliveryError, match="editable_metadata_stale"):
        delivery.compare_metadata(
            {"METADATA": "Name: ledger\nVersion: 9.0.0\nRequires-Dist: old\n"},
            {"METADATA": "Name: ledger\nVersion: 9.0.0\nRequires-Dist: new\n"},
        )


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        ({"package_origin": "/synthetic/other-checkout/src/ledger"}, "installed_origin_drift"),
        ({"version": "8.0.0"}, "installed_version_drift"),
        ({"package": {"core.py": "different"}}, "installed_package_bytes_drift"),
    ],
)
def test_installation_drift_is_rejected_before_launching_any_mcp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: dict, code: str
) -> None:
    delivery = delivery_module()
    expected = {"version": "9.0.0", "package": {"core.py": "canonical"}}
    inventory = {
        "editable": True,
        "version": "9.0.0",
        "module_version": "9.0.0",
        "package_origin": str(tmp_path / "src/ledger"),
        "direct_origin": str(tmp_path),
        "prefix": str(tmp_path / ".venv"),
        "package": {"core.py": "canonical"},
        **mutation,
    }
    # The isolated interpreter is the external boundary; all comparison logic is real.
    monkeypatch.setattr(delivery, "run_command", lambda *_args, **_kwargs: json.dumps(inventory))
    with pytest.raises(delivery.DeliveryError, match=code):
        delivery.check_installation(tmp_path, tmp_path, expected, {})


def test_source_only_never_consults_global_config_or_writes_delivery_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    delivery = delivery_module()
    monkeypatch.setattr(delivery, "source_state", lambda _source: {"version": "9.0.0"})
    monkeypatch.setattr(delivery, "wheel_metadata", lambda *_args: {})
    monkeypatch.setattr(
        delivery, "adapter_allowance", lambda *_: pytest.fail("Adapter config read")
    )
    # Only the artifact build is stubbed: absent config must remain irrelevant.
    result = delivery.workflow(
        "source",
        tmp_path,
        tmp_path / "absent",
        tmp_path / "absent",
        [],
        allowed_adapter_config=tmp_path / "absent-adapters",
    )
    assert result["installed_delivery"] == "not_verified"
    assert not (tmp_path / ".venv/ledger-delivery.json").exists()


def test_symlinked_environment_is_refused_before_any_build_or_bootstrap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delivery = delivery_module()
    source = tmp_path / "checkout"
    source.mkdir()
    other = tmp_path / "other-environment"
    other.mkdir()
    (source / ".venv").symlink_to(other, target_is_directory=True)
    monkeypatch.setattr(delivery, "source_state", lambda _source: {"version": "9.0.0"})

    def build_attempt(*_args):
        (other / "unintended-execution").write_text("synthetic write outside checkout\n")
        raise RuntimeError("A foreign interpreter was invoked before refusal")

    monkeypatch.setattr(delivery, "wheel_metadata", build_attempt)
    with pytest.raises(delivery.DeliveryError, match="symlink_refused"):
        delivery.workflow("deliver", source, source / "codex", source / "claude", [])
    assert list(other.iterdir()) == []


@pytest.mark.parametrize(
    "failed_stage", ["release_gates", "installation", "fresh_process_verification"]
)
def test_failed_delivery_stage_cannot_create_success_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_stage: str
) -> None:
    delivery = delivery_module()
    monkeypatch.setattr(delivery, "source_state", lambda _source: {"version": "9.0.0"})
    monkeypatch.setattr(delivery, "wheel_metadata", lambda *_args: {})
    monkeypatch.setattr(delivery, "configured_clients", lambda *_args: {})
    monkeypatch.setattr(delivery, "check_installation", lambda *_args: {})
    monkeypatch.setattr(delivery, "check_skills", lambda *_args: {})

    def boundary(_command, *, stage, **_kwargs):
        if stage == failed_stage:
            raise delivery.DeliveryError(f"{stage}_failed")
        return "{}"

    monkeypatch.setattr(delivery, "run_command", boundary)
    with pytest.raises(delivery.DeliveryError, match=f"{failed_stage}_failed"):
        delivery.workflow("deliver", tmp_path, tmp_path / "codex", tmp_path / "claude", [])
    assert not (tmp_path / ".venv/ledger-delivery.json").exists()


def test_failing_command_is_actionable_and_does_not_echo_output(tmp_path: Path) -> None:
    delivery = delivery_module()
    with pytest.raises(delivery.DeliveryError, match="installation_failed") as caught:
        delivery.run_command(
            [sys.executable, "-c", "print('PRIVATE-SYNTHETIC-OUTPUT'); raise SystemExit(7)"],
            cwd=tmp_path,
            stage="installation",
        )
    assert "PRIVATE-SYNTHETIC" not in str(caught.value)


def test_explicit_installed_freeze_propagates_failed_delivery_gate(tmp_path: Path) -> None:
    """Exercise the real freeze wrapper with command fakes, never its owner DB default."""
    import shutil
    import subprocess

    root = tmp_path / "checkout"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    shutil.copyfile(REPO / "scripts/freeze-check", scripts / "freeze-check")
    (scripts / "_common.sh").write_text(
        f'ROOT="{root}"\nVENV_PY=/usr/bin/true\nensure_python_env() {{ :; }}\n'
    )
    check = scripts / "delivery-check"
    check.write_text("#!/bin/sh\nexit 37\n")
    check.chmod(0o700)
    result = subprocess.run(
        ["bash", str(scripts / "freeze-check")],
        cwd=root,
        env={
            "PATH": "/usr/bin:/bin",
            "LEDGER_FREEZE_SYNTHETIC": "1",
            "LEDGER_FREEZE_SOURCE_ONLY": "0",
            "TMPDIR": str(tmp_path),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0, (
        "explicit installed verification must refuse missing or failed delivery"
    )
    assert "FAIL  delivery" in result.stdout


def test_synthetic_environment_keeps_parsing_disabled_and_drops_ambient_allowance(
    tmp_path, monkeypatch
):
    delivery = delivery_module()
    monkeypatch.setenv("LEDGER_ADAPTER_CONFIG", "/private/never-read.json")
    monkeypatch.setenv("LEDGER_ALLOWED_ADAPTER_CONFIG", "/private/never-implicitly-forward.json")
    result = delivery.environment(tmp_path)
    assert result["LEDGER_ADAPTER_CONFIG"] == str(tmp_path / "no-adapters.json")
    assert result["LEDGER_HOME"] == str(tmp_path / "synthetic-runtime")
    assert "LEDGER_ALLOWED_ADAPTER_CONFIG" not in result


@pytest.mark.parametrize("change", [None, "original", "snapshot"])
def test_delivery_binds_explicit_adapter_snapshot_and_never_enables_parsing(
    tmp_path, monkeypatch, change
):
    from ledger.adapters import registry
    from ledger.adapters.contracts import API_VERSION, AdapterDefinition
    from ledger.maintenance.environment_check import check_environment

    delivery = delivery_module()
    monkeypatch.setattr(delivery, "source_state", lambda _source: {"version": "9.0.0"})
    monkeypatch.setattr(delivery, "wheel_metadata", lambda *_args: {})
    monkeypatch.setattr(delivery, "configured_clients", lambda *_args: {})
    monkeypatch.setattr(delivery, "check_installation", lambda *_args: {})
    monkeypatch.setattr(delivery, "check_skills", lambda *_args: {})
    original = tmp_path / "selected-pins.json"
    config = {"schema_version": 1, "adapters": []}
    original.write_text(json.dumps(config))
    original.chmod(0o600)
    changed = {
        "schema_version": 1,
        "adapters": [
            {
                "distribution": "fictional-disabled-adapter",
                "version": "1.0",
                "entry_point": "fictional",
                "sha256": "0" * 64,
                "enabled": False,
                "metadata": registry._definition_metadata(
                    AdapterDefinition("fictional-disabled", API_VERSION, "1")
                ),
            }
        ],
    }
    receipts = []
    gate_snapshots = []

    # Keep the real allowance reader: snapshot permissions must satisfy exactly
    # the same private-file contract as the original, explicitly selected file.
    monkeypatch.setattr(delivery, "write_receipt", lambda _path, report: receipts.append(report))

    def boundary(command, *, stage, cwd, env=None, **_kwargs):
        if stage == "release_gates":
            snapshot = Path(env["LEDGER_ALLOWED_ADAPTER_CONFIG"])
            gate_snapshots.append(snapshot)
            assert snapshot != original
            assert json.loads(snapshot.read_text()) == config
            assert snapshot.stat().st_mode & 0o777 == 0o600
            assert Path(env["LEDGER_ADAPTER_CONFIG"]) != snapshot
            assert not Path(env["LEDGER_ADAPTER_CONFIG"]).exists()
            if change == "snapshot":
                snapshot.write_text(json.dumps(changed))
        elif stage == "dependency_environment":
            assert "--allowed-adapter-config" in command
            selected = Path(command[command.index("--allowed-adapter-config") + 1])
            assert selected != original and selected.is_file()
            # Exercise the actual environment gate and registry reader, without
            # requiring or inspecting any installed private adapter inventory.
            # Core packages are intentionally absent from this synthetic input;
            # only the allowance-file result is relevant to this regression.
            assert check_environment([], allowed_adapter_config=selected).adapter_error is None
        elif stage == "fresh_process_verification":
            probe_env = delivery.environment(cwd)
            assert "LEDGER_ALLOWED_ADAPTER_CONFIG" not in probe_env
            assert not Path(probe_env["LEDGER_ADAPTER_CONFIG"]).exists()
            if change == "original":
                original.write_text(json.dumps(changed))
            return '{"status":"passed"}'
        return "{}"

    monkeypatch.setattr(delivery, "run_command", boundary)
    arguments = ("deliver", tmp_path, tmp_path / "codex", tmp_path / "claude", [])
    if change:
        with pytest.raises(delivery.DeliveryError, match="adapter_allowance_changed"):
            delivery.workflow(*arguments, allowed_adapter_config=original)
        assert not receipts
    else:
        result = delivery.workflow(*arguments, allowed_adapter_config=original)
        assert result["verified"]["allowed_adapters"] == {
            "configuration_sha256": delivery.digest(config),
            "enabled_records": 0,
        }
        assert receipts == [result]
    assert len(gate_snapshots) == 1
    assert not gate_snapshots[0].exists()
