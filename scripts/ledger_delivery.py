#!/usr/bin/env python3
"""Local delivery and live freshness verification; no owner runtime inputs exist."""

from __future__ import annotations

import argparse
import ast
import contextlib
import hashlib
import importlib.metadata
import io
import json
import os
import re
import runpy
import shutil
import socket
import subprocess
import sys
import tempfile
import tomllib
import uuid
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlparse

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ("ledger-query", "ledger-pdf-import")
RECEIPT_NAME = "ledger-delivery.json"


class DeliveryError(RuntimeError):
    """A bounded actionable failure, without runtime arguments or subprocess output."""


def failure_codes(error: BaseException) -> list[str]:
    if isinstance(error, BaseExceptionGroup):
        return sorted({code for child in error.exceptions for code in failure_codes(child)})
    if isinstance(error, DeliveryError):
        return [str(error)]
    return [type(error).__name__]


def require(condition: bool, code: str) -> None:
    if not condition:
        raise DeliveryError(code)


def digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def no_symlink(path: Path) -> None:
    require(not any(part.is_symlink() for part in (path, *path.parents)), "symlink_refused")


def same_location(left: Path, right: Path) -> bool:
    left, right = left.resolve(), right.resolve()
    return left == right or (
        str(left).casefold() == str(right).casefold()
        and left.exists()
        and right.exists()
        and left.samefile(right)
    )


def fingerprint(root: Path) -> dict[str, str]:
    no_symlink(root)
    require(root.is_dir(), "package_or_skill_missing")
    result = {}
    for path in sorted(root.rglob("*")):
        no_symlink(path)
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        require(path.is_file() or path.is_dir(), "non_regular_package_file")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    require(bool(result), "empty_package_or_skill")
    return result


def environment(root: Path) -> dict[str, str]:
    result = {key: os.environ[key] for key in ("PATH", "LANG", "LC_CTYPE") if key in os.environ}
    result.update(
        HOME=str(root),
        TMPDIR=str(root),
        LEDGER_HOME=str(root / "synthetic-runtime"),
        LEDGER_ADAPTER_CONFIG=str(root / "no-adapters.json"),
        PYTHONDONTWRITEBYTECODE="1",
        PIP_DISABLE_PIP_VERSION_CHECK="1",
    )
    return result


def run_command(
    command: list[str],
    *,
    cwd: Path,
    stage: str,
    timeout: int = 180,
    env: dict | None = None,
    log: Path | None = None,
    json_errors: bool = False,
) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            env=environment(cwd) if env is None else env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DeliveryError(f"{stage}_failed:{type(error).__name__}") from None
    if log is not None:
        no_symlink(log)
        log.write_text(result.stdout + result.stderr)
        log.chmod(0o600)
    if result.returncode != 0:
        detail = f"exit_{result.returncode}"
        if json_errors:
            with contextlib.suppress(ValueError, TypeError, AttributeError):
                error_code = json.loads(result.stdout).get("error", "")
                if isinstance(error_code, str) and re.fullmatch(r"[A-Za-z0-9_:,+]+", error_code):
                    detail = error_code
        raise DeliveryError(f"{stage}_failed:{detail}")
    return result.stdout


def check_configured_server(config: dict, expected: Path) -> dict:
    command = config.get("command")
    if not isinstance(command, str) or not Path(command).is_absolute():
        raise DeliveryError("configured_entrypoint_drift")
    require(same_location(Path(command), expected), "configured_entrypoint_drift")
    require(config.get("enabled", True) is True, "configured_server_disabled")
    require(
        config.get("experimental_environment") in (None, "local"),
        "configured_execution_environment_unsupported",
    )
    require(
        config.get("type", "stdio") == "stdio" and not config.get("url"),
        "configured_transport_unsupported",
    )
    require(
        not config.get("env") and not config.get("env_vars"), "configured_environment_unsupported"
    )
    require(not config.get("cwd"), "configured_cwd_unsupported")
    args = config.get("args")
    flag = "--proposals-db" if expected.name == "ledger-mcp-write" else "--db"
    require(
        isinstance(args, list)
        and len(args) == 2
        and args[0] == flag
        and isinstance(args[1], str)
        and bool(args[1]),
        "configured_arguments_unsupported",
    )
    return {
        "command": command,
        "resolved_command": str(Path(command).resolve()),
        "runtime_flag": flag,
        "execution_environment": "local",
        "runtime_arguments_replaced": True,
    }


def configured_clients(source: Path, codex: Path, claude: Path) -> dict:
    require(codex.is_file() and claude.is_file(), "client_configuration_missing")
    # Read only launch metadata. No configured runtime value is returned or executed.
    codex_payload = tomllib.loads(codex.read_text())
    claude_payload = json.loads(claude.read_text())
    configs = {
        "codex": codex_payload.get("mcp_servers", {}),
        "claude": claude_payload.get("mcpServers", {}),
    }
    # Project/local definitions can shadow otherwise-correct user definitions.
    # Resolve explicit direct commands; reject unhandled enable/disable policy
    # rather than claiming to have reproduced a client's different effective setup.
    project_file = source / ".mcp.json"
    if project_file.is_file():
        no_symlink(project_file)
        configs["claude"] = {
            **configs["claude"],
            **json.loads(project_file.read_text()).get("mcpServers", {}),
        }
    for project_path, settings in claude_payload.get("projects", {}).items():
        if same_location(Path(project_path), source):
            require(not settings.get("disabledMcpServers"), "configured_project_policy_unsupported")
            require(
                not settings.get("enabledMcpjsonServers")
                and not settings.get("disabledMcpjsonServers"),
                "configured_project_policy_unsupported",
            )
            configs["claude"] = {**configs["claude"], **settings.get("mcpServers", {})}
    for directory in (*reversed(source.parents), source):
        codex_project = directory / ".codex/config.toml"
        if codex_project.is_file() and not same_location(codex_project, codex):
            no_symlink(codex_project)
            project_servers = tomllib.loads(codex_project.read_text()).get("mcp_servers", {})
            for name, config in project_servers.items():
                configs["codex"][name] = {**configs["codex"].get(name, {}), **config}
    result = {}
    for client, servers in configs.items():
        require(isinstance(servers, dict) and "ledger" in servers, "configured_server_missing")
        selected = {}
        for name, config in servers.items():
            if name == "ledger" or (
                isinstance(config, dict) and "ledger-mcp" in str(config.get("command", ""))
            ):
                require(isinstance(config, dict), "configured_server_invalid")
                writer = Path(str(config.get("command", ""))).name == "ledger-mcp-write"
                executable = "ledger-mcp-write" if writer else "ledger-mcp"
                selected[name] = check_configured_server(config, source / ".venv/bin" / executable)
        result[client] = selected
    return result


def compare_metadata(installed: dict, expected: dict) -> None:
    require(installed == expected, "editable_metadata_stale")


def source_state(source: Path) -> dict:
    project = tomllib.loads((source / "pyproject.toml").read_text())["project"]
    return {
        "version": project["version"],
        "package": fingerprint(source / "src/ledger"),
        "skills": {name: fingerprint(source / "docs/agent/skills" / name) for name in SKILLS},
        "delivery_inputs": {
            name: hashlib.sha256((source / name).read_bytes()).hexdigest()
            for name in (
                "pyproject.toml",
                "README.md",
                "requirements.lock",
                "streamlit_app.py",
                ".streamlit/config.toml",
                "AGENTS.md",
                ".gitignore",
                "docs/README.md",
                "docs/agent/development.md",
                "docs/agent/delivery.md",
            )
        },
        "release_input_trees": {
            name: fingerprint(source / name)
            for name in (
                "scripts",
                "tests",
                "configs",
                "vendor",
                "docs/agent/factory",
                "docs/agent/skills/ledger-implement-ticket",
            )
        },
    }


def wheel_metadata(source: Path, scratch: Path, expected: dict) -> dict:
    # Use the same admitted inventory as direct PEP 517 builds. A recursive local
    # fingerprint is freshness evidence, never authority to add archive members.
    backend = runpy.run_path(str(source / "scripts/build_backend.py"))
    snapshot = backend["source_snapshot"](source)
    wheel_dir = scratch / "wheel"
    wheel_dir.mkdir()
    run_command(
        [
            str(source / ".venv/bin/python"),
            "-I",
            "-m",
            "pip",
            "wheel",
            "--no-index",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheel_dir),
            str(source),
        ],
        cwd=scratch,
        stage="wheel_build",
    )
    wheels = list(wheel_dir.glob("ledger-*.whl"))
    require(len(wheels) == 1, "wheel_artifact_missing")
    backend["validate_wheel"](wheels[0], snapshot)
    with zipfile.ZipFile(wheels[0]) as wheel:
        package = {
            name.removeprefix("ledger/"): hashlib.sha256(wheel.read(name)).hexdigest()
            for name in wheel.namelist()
            if name.startswith("ledger/") and not name.endswith("/")
        }
        expected_package = {
            name.removeprefix("ledger/"): hashlib.sha256(content).hexdigest()
            for name, content in backend["wheel_payload"](snapshot).items()
        }
        require(package == expected_package, "wheel_package_bytes_drift")
        source_package = {
            name.removeprefix("src/ledger/"): hashlib.sha256(content).hexdigest()
            for name, content in snapshot.contents.items()
            if name.startswith("src/ledger/")
        }
        require(expected["package"] == source_package, "source_package_has_undeclared_files")
        result = {}
        for name in ("METADATA", "entry_points.txt"):
            entries = [item for item in wheel.namelist() if item.endswith(f".dist-info/{name}")]
            require(len(entries) == 1, "wheel_metadata_missing")
            result[name] = wheel.read(entries[0]).decode()
        return result


def installed_inventory() -> dict:
    import ledger

    distribution = importlib.metadata.distribution("ledger")
    direct = json.loads(distribution.read_text("direct_url.json") or "{}")
    return {
        "version": distribution.version,
        "module_version": ledger.__version__,
        "package_origin": str(Path(ledger.__file__).resolve().parent),
        "prefix": sys.prefix,
        "python": sys.executable,
        "editable": direct.get("dir_info", {}).get("editable") is True,
        "direct_origin": unquote(urlparse(direct.get("url", "")).path),
        "metadata": {
            name: distribution.read_text(name) for name in ("METADATA", "entry_points.txt")
        },
        "package": fingerprint(Path(ledger.__file__).resolve().parent),
    }


def check_installation(source: Path, scratch: Path, expected: dict, metadata: dict) -> dict:
    no_symlink(source / ".venv")
    python = source / ".venv/bin/python"
    inventory = json.loads(
        run_command(
            [str(python), "-I", str(source / "scripts/ledger_delivery.py"), "_inventory"],
            cwd=scratch,
            stage="installed_inventory",
        )
    )
    require(
        inventory["editable"]
        and inventory["package_origin"] == str(source / "src/ledger")
        and inventory["direct_origin"] == str(source)
        and inventory["prefix"] == str(source / ".venv"),
        "installed_origin_drift",
    )
    require(
        inventory["version"] == inventory["module_version"] == expected["version"],
        "installed_version_drift",
    )
    require(inventory["package"] == expected["package"], "installed_package_bytes_drift")
    compare_metadata(inventory["metadata"], metadata)
    executables = {}
    for name in ("ledger", "ledger-mcp", "ledger-mcp-write"):
        path = source / ".venv/bin" / name
        require(path.is_file() and os.access(path, os.X_OK), "executable_missing")
        shebang = path.read_text().splitlines()[0]
        require(shebang == f"#!{python}", "executable_interpreter_drift")
        executables[name] = {
            "path": str(path),
            "resolved": str(path.resolve()),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    version = run_command(
        [str(source / ".venv/bin/ledger"), "version"], cwd=scratch, stage="cli_version"
    )
    require(expected["version"] in version, "cli_version_drift")
    return {
        "python": inventory["python"],
        "prefix": inventory["prefix"],
        "package_origin": inventory["package_origin"],
        "metadata_sha256": digest(metadata),
        "executables": executables,
    }


def sync_skill(source: Path, target: Path) -> None:
    canonical = fingerprint(source)
    no_symlink(target)
    if target.exists() and fingerprint(target) == canonical:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.delivery-", dir=target.parent))
    backup = target.with_name(f".{target.name}.previous-{uuid.uuid4().hex}")
    try:
        shutil.copytree(
            source,
            staging,
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
        )
        require(fingerprint(staging) == canonical, "skill_staging_drift")
        if target.exists():
            target.rename(backup)
        try:
            staging.rename(target)
        except OSError:
            if backup.exists():
                backup.rename(target)
            raise
        require(fingerprint(target) == canonical, "skill_sync_failed")
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def check_skills(source: Path, roots: list[Path]) -> dict:
    result = {}
    for root in roots:
        for name in SKILLS:
            canonical = fingerprint(source / "docs/agent/skills" / name)
            require(fingerprint(root / name) == canonical, "copied_skill_drift")
            result[str(root / name)] = digest(canonical)
    return result


def require_receipt(path: Path, verified: dict) -> None:
    require(path.is_file(), "delivery_receipt_missing")
    no_symlink(path)
    previous = json.loads(path.read_text())
    require(
        previous.get("schema") == 1 and previous.get("status") == "passed",
        "delivery_receipt_invalid",
    )
    require(previous.get("verified") == verified, "source_not_delivered")


def write_receipt(path: Path, report: dict) -> None:
    no_symlink(path)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}")
    try:
        with temporary.open("x") as output:
            temporary.chmod(0o600)
            json.dump(report, output, sort_keys=True, indent=2)
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def instruction_literal(source: Path) -> str:
    for node in ast.parse((source / "src/ledger/mcp/contracts.py").read_text()).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "SERVER_INSTRUCTIONS"
            for target in node.targets
        ):
            value = ast.literal_eval(node.value)
            require(isinstance(value, str), "canonical_instructions_invalid")
            return value
    raise DeliveryError("canonical_instructions_missing")


def writer_instruction_literal(source: Path) -> str:
    tree = ast.parse((source / "src/ledger/mcp/write_server.py").read_text())
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Server"
        ):
            for keyword in node.keywords:
                if keyword.arg == "instructions":
                    value = ast.literal_eval(keyword.value)
                    require(isinstance(value, str), "canonical_writer_instructions_invalid")
                    return value
    raise DeliveryError("canonical_writer_instructions_missing")


async def probe_processes(source: Path, root: Path, version: str) -> list[dict]:
    import anyio
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters, stdio_client

    from ledger.mcp.contracts import tool_schemas
    from ledger.mcp.write_contracts import write_tool_schemas

    expected_instructions = instruction_literal(source)
    expected_writer_instructions = writer_instruction_literal(source)
    modes = []
    for mode, protocol in (("auto", "2026-07-28"), ("legacy", "2025-11-25")):
        for writer in (False, True):
            executable = "ledger-mcp-write" if writer else "ledger-mcp"
            args = (
                ["--proposals-db", str(root / f"proposals-{mode}/proposals.db")]
                if writer
                else ["--db", str(root / "ledger.db")]
            )
            with tempfile.TemporaryFile(mode="w+") as errors:
                parameters = StdioServerParameters(
                    command=str(source / ".venv/bin" / executable),
                    args=args,
                    cwd=str(root),
                    env=environment(root),
                )
                with anyio.fail_after(30):
                    async with Client(stdio_client(parameters, errlog=errors), mode=mode) as client:
                        if client.server_info is None:
                            raise DeliveryError("fresh_server_identity_missing")
                        require(
                            client.server_info.name
                            == ("Ledger Proposal Writer" if writer else "Ledger"),
                            "fresh_server_identity_drift",
                        )
                        require(client.server_info.version == version, "fresh_server_version_drift")
                        require(client.protocol_version == protocol, "fresh_protocol_drift")
                        require(
                            client.instructions
                            == (expected_writer_instructions if writer else expected_instructions),
                            "fresh_instructions_drift",
                        )
                        listing = await client.list_tools()
                        actual = [
                            tool.model_dump(by_alias=True, exclude_none=True)
                            for tool in listing.tools
                        ]
                        require(
                            actual == (write_tool_schemas() if writer else tool_schemas()),
                            "fresh_tools_drift",
                        )
                        if writer:
                            result = await client.call_tool(
                                "ledger.proposals.file",
                                {
                                    "merchant_norm": "SYNTHETIC DELIVERY CAFE",
                                    "pattern": "SYNTHETIC DELIVERY CAFE",
                                    "category": "Dining",
                                    "evidence_id": "e" * 40,
                                },
                            )
                            require(
                                not result.is_error
                                and result.structured_content["data"]["status"] == "pending",
                                "fresh_proposal_filing_failed",
                            )
                        else:
                            result = await client.call_tool("ledger.status", {})
                            require(
                                not result.is_error
                                and result.structured_content["data"]["read_only"] is True,
                                "fresh_read_status_failed",
                            )
                            result = await client.call_tool(
                                "ledger.aggregate",
                                {
                                    "start_date": "2025-01-01",
                                    "end_date": "2025-12-31",
                                    "flow": "income",
                                    "group_by": "source",
                                    "filters": {"sources": ["Northstar University"]},
                                    "limit": 50,
                                },
                            )
                            require(
                                not result.is_error
                                and result.structured_content["data"]["total"] == "3141.59",
                                "fresh_synthetic_aggregate_failed",
                            )
                            result = await client.call_tool(
                                "ledger.investments.statements",
                                {"start_date": "2025-10-01", "end_date": "2025-10-31"},
                            )
                            require(not result.is_error, "fresh_investment_discovery_failed")
                            entries = result.structured_content["data"]["items"]
                            require(len(entries) == 1, "fresh_investment_discovery_failed")
                            reference = entries[0]["statement_ref"]
                            result = await client.call_tool(
                                "ledger.investments.statement.get", {"statement_ref": reference}
                            )
                            require(
                                not result.is_error
                                and result.structured_content["data"]["nav_reconciliation"][
                                    "status"
                                ]
                                == "matches"
                                and result.structured_content["data"]["cash_balances"][0][
                                    "reconciliation"
                                ]["status"]
                                == "matches",
                                "fresh_investment_reconciliation_failed",
                            )
                            require(
                                {
                                    row["asset_class"]: row["current_total"]
                                    for row in result.structured_content["data"]["asset_balances"]
                                }
                                == {
                                    "Stocks": "9.2535",
                                    "Cash": "0.7515",
                                    "Dividend Accruals": "0.000000000001",
                                    "Interest Accruals": "-0.000000000001",
                                },
                                "fresh_investment_nav_components_failed",
                            )
                            for tool, field, expected in (
                                ("ledger.investments.positions", "market_value", "9.2535"),
                                ("ledger.investments.activities", "amount", "0.002"),
                            ):
                                result = await client.call_tool(tool, {"statement_ref": reference})
                                require(
                                    not result.is_error
                                    and result.structured_content["data"]["total_count"] == 1
                                    and result.structured_content["data"]["items"][0][field]
                                    == expected,
                                    "fresh_investment_detail_failed",
                                )
                errors.seek(0)
                require(not errors.read(), "fresh_server_stderr")
            modes.append(
                {
                    "executable": executable,
                    "mode": mode,
                    "protocol": protocol,
                    "fresh_process": True,
                }
            )
    return modes


def fresh_probe(source: Path) -> dict:
    import anyio

    import ledger

    with tempfile.TemporaryDirectory(
        prefix="ledger-delivery-probe-",
        dir=Path(os.environ.get("LEDGER_DELIVERY_WORK_ROOT", "/tmp")).resolve(),
    ) as directory:
        root = Path(directory)
        root.chmod(0o700)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            helper = runpy.run_path(str(source / "tests/mcp_synthetic.py"))
            helper["build_synthetic_mcp_database"](root / "ledger.db")
            investment_helper = runpy.run_path(str(source / "tests/investment_mcp_fixture.py"))
            investment_helper["populate_investment_probe"](root / "ledger.db")
        before = {
            path.name: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
            for path in root.glob("ledger.db*")
        }
        processes = anyio.run(probe_processes, source, root, ledger.__version__)
        after = {
            path.name: (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
            for path in root.glob("ledger.db*")
        }
        require(before == after, "synthetic_financial_database_changed")
    require(not root.exists(), "synthetic_cleanup_failed")
    return {
        "processes": processes,
        "financial_database_unchanged": True,
        "synthetic_runtime_removed": True,
        "existing_connections": "not_restarted_or_verified",
    }


def adapter_allowance(path: Path) -> dict:
    """Read only the explicitly selected pin file and validate installed bytes."""
    from ledger.adapters.contracts import AdapterError
    from ledger.maintenance.environment_check import validated_adapter_configuration

    try:
        return validated_adapter_configuration(path)
    except AdapterError as exc:
        raise DeliveryError(f"adapter_allowance_failed:{exc}") from None


def workflow(
    mode: str,
    source: Path,
    codex: Path,
    claude: Path,
    skill_roots: list[Path],
    *,
    allowed_adapter_config: Path | None = None,
) -> dict:
    no_symlink(source / ".venv")
    expected = source_state(source)
    receipt = source / ".venv" / RECEIPT_NAME
    with tempfile.TemporaryDirectory(
        prefix="ledger-delivery-build-",
        dir=Path(os.environ.get("LEDGER_DELIVERY_WORK_ROOT", "/tmp")).resolve(),
    ) as directory:
        scratch = Path(directory)
        metadata = wheel_metadata(source, scratch, expected)
        if mode == "source":
            return {
                "status": "passed",
                "scope": "source_only",
                "version": expected["version"],
                "installed_delivery": "not_verified",
            }
        clients = configured_clients(source, codex, claude)
        allowance = None
        allowance_path = None
        allowance_state = None
        if allowed_adapter_config is not None:
            allowance = adapter_allowance(allowed_adapter_config)
            allowance_state = {
                "configuration_sha256": digest(allowance),
                "enabled_records": sum(record["enabled"] for record in allowance["adapters"]),
            }
            # Only this checked snapshot is passed to release gates. The parser
            # configuration remains a different, empty synthetic path throughout.
            allowance_path = scratch / "allowed-adapters.json"
            allowance_path.write_text(json.dumps(allowance, sort_keys=True), encoding="utf-8")
            allowance_path.chmod(0o600)
        dependency_command = [
            str(source / ".venv/bin/python"),
            "-I",
            "-m",
            "ledger.maintenance.environment_check",
        ]
        if allowance_path is not None:
            dependency_command += ["--allowed-adapter-config", str(allowance_path)]
        run_command(dependency_command, cwd=scratch, stage="dependency_environment")
        if mode == "deliver":
            # The full release gate is mandatory. It cannot call this installed
            # gate recursively; its explicit source-only boundary is reported.
            gate_env = environment(scratch)
            if allowance_path is not None:
                gate_env["LEDGER_ALLOWED_ADAPTER_CONFIG"] = str(allowance_path)
            with socket.socket() as available:
                available.bind(("127.0.0.1", 0))
                gate_env["PORT"] = os.environ.get("PORT") or str(available.getsockname()[1])
            gate_env.update(
                LEDGER_FREEZE_SYNTHETIC="1",
                LEDGER_FREEZE_UI="1",
                LEDGER_FREEZE_SOURCE_ONLY="1",
                LEDGER_FREEZE_AUDIT="0",
            )
            print(
                "delivery: running complete synthetic freeze and UI gates",
                file=sys.stderr,
                flush=True,
            )
            run_command(
                [str(source / "scripts/freeze-check")],
                cwd=source,
                stage="release_gates",
                timeout=1800,
                env=gate_env,
                log=source / ".venv/ledger-delivery-freeze.log",
            )
            require(source_state(source) == expected, "source_changed_during_release_gates")
            if allowance_path is not None:
                require(
                    adapter_allowance(allowance_path) == allowance,
                    "adapter_allowance_changed_during_verification",
                )
            print(
                "delivery: refreshing editable package metadata and entry points",
                file=sys.stderr,
                flush=True,
            )
            run_command(
                [
                    str(source / ".venv/bin/python"),
                    "-I",
                    "-m",
                    "pip",
                    "install",
                    "--no-deps",
                    "--force-reinstall",
                    "-e",
                    str(source),
                ],
                cwd=scratch,
                stage="installation",
                timeout=300,
                log=source / ".venv/ledger-delivery-install.log",
            )
            for root in skill_roots:
                for name in SKILLS:
                    sync_skill(source / "docs/agent/skills" / name, root / name)
        installation = check_installation(source, scratch, expected, metadata)
        copies = check_skills(source, skill_roots)
        verified = {
            **expected,
            "installation": installation,
            "clients": clients,
            "copied_skills": copies,
            "allowed_adapters": allowance_state,
        }
        if mode == "check":
            require_receipt(receipt, verified)
        run_command(
            [str(source / ".venv/bin/python"), "-I", "-m", "pip", "check"],
            cwd=scratch,
            stage="dependency_compatibility",
        )
        print("delivery: verifying four fresh synthetic MCP processes", file=sys.stderr, flush=True)
        fresh = json.loads(
            run_command(
                [
                    str(source / ".venv/bin/python"),
                    "-I",
                    str(source / "scripts/ledger_delivery.py"),
                    "_probe",
                ],
                cwd=scratch,
                stage="fresh_process_verification",
                timeout=180,
                log=source / ".venv/ledger-delivery-probe.log",
                json_errors=True,
            )
        )
        require(fresh.get("status") == "passed", "fresh_process_verification_failed")
        require(source_state(source) == expected, "source_changed_during_verification")
        require(
            configured_clients(source, codex, claude) == clients,
            "configuration_changed_during_verification",
        )
        require(check_skills(source, skill_roots) == copies, "skills_changed_during_verification")
        require(
            check_installation(source, scratch, expected, metadata) == installation,
            "installation_changed_during_verification",
        )
        run_command(dependency_command, cwd=scratch, stage="dependency_environment")
        if allowed_adapter_config is not None:
            require(
                adapter_allowance(allowed_adapter_config) == allowance,
                "adapter_allowance_changed_during_verification",
            )
            require(
                allowance_path is not None and adapter_allowance(allowance_path) == allowance,
                "adapter_allowance_changed_during_verification",
            )
        report = {
            "schema": 1,
            "status": "passed",
            "scope": "installed_fresh_synthetic_processes",
            "verified": verified,
            "fresh": fresh,
            "release_gates": "full_synthetic_freeze_with_ui",
        }
        if mode == "deliver":
            write_receipt(receipt, report)
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("deliver", "check", "source", "_inventory", "_probe"))
    parser.add_argument("--codex-config", type=Path, default=Path.home() / ".codex/config.toml")
    parser.add_argument("--claude-config", type=Path, default=Path.home() / ".claude.json")
    parser.add_argument("--skill-root", type=Path, action="append")
    parser.add_argument(
        "--allowed-adapter-config",
        type=Path,
        default=os.environ.get("LEDGER_ALLOWED_ADAPTER_CONFIG"),
        help="Explicit private installed-adapter pins; never enables parsing in probes.",
    )
    args = parser.parse_args()
    try:
        require(bool(sys.flags.isolated), "isolated_python_required")
        if args.mode == "_inventory":
            report = installed_inventory()
        elif args.mode == "_probe":
            report = {"status": "passed", **fresh_probe(ROOT)}
        else:
            roots = args.skill_root or [
                Path.home() / ".agents/skills",
                Path.home() / ".claude/skills",
            ]
            require(len(set(roots)) == len(roots) and bool(roots), "skill_targets_invalid")
            report = workflow(
                args.mode,
                ROOT,
                args.codex_config,
                args.claude_config,
                roots,
                allowed_adapter_config=args.allowed_adapter_config,
            )
        print(json.dumps(report, sort_keys=True, indent=2))
        return 0
    except Exception as error:
        code = ",".join(failure_codes(error))
        explanations = {
            "source_not_delivered": "Current source, build, executable, configured launch or Skill fingerprints differ from the last delivered state, even if versions still match.",
            "delivery_receipt_missing": "No successful complete delivery is recorded for this installation.",
            "editable_metadata_stale": "Installed editable METADATA or console-script metadata differs from a newly built canonical wheel; a matching version number is insufficient.",
            "installed_origin_drift": "The isolated interpreter imports a different checkout or virtual environment.",
            "installed_package_bytes_drift": "The imported package has missing, extra or changed files compared with canonical source.",
            "configured_entrypoint_drift": "A configured MCP command selects a different executable or checkout.",
            "copied_skill_drift": "An installed Skill has missing, extra or changed files compared with its canonical tree.",
            "release_gates_failed": "The required synthetic release/UI gate failed. Inspect .venv/ledger-delivery-freeze.log before retrying.",
            "installation_failed": "The editable reinstall failed. Inspect .venv/ledger-delivery-install.log; no success receipt was written.",
            "fresh_process_verification_failed": "A newly launched synthetic MCP process failed verification. Inspect .venv/ledger-delivery-probe.log for the bounded failure code; no success receipt was written.",
        }
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": code,
                    "explanation": explanations.get(
                        code.split(":")[0],
                        "The named delivery invariant or supported launch shape failed verification.",
                    ),
                    "action": "Inspect the named stage; correct its installation/configuration/Skill drift and rerun ./scripts/deliver. Never substitute an owner runtime or infer that existing connections restarted.",
                },
                sort_keys=True,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
