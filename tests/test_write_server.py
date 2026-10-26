"""P9's opt-in proposal writer is distinct from the frozen read MCP."""

from __future__ import annotations

import copy
import json
import logging
import stat
import sys
import time
import tomllib
from pathlib import Path
from typing import TextIO

import pytest
from test_review_teach import _insert_txn, _merchant_rules, _seed_config
from typer.testing import CliRunner

from ledger import __version__
from ledger.cli import app
from ledger.mcp.contracts import contract_digest as read_contract_digest
from ledger.mcp.contracts import tool_schemas as read_tool_schemas
from ledger.mcp.write_contracts import write_contract_digest, write_tool_schemas
from ledger.mcp.write_server import create_server
from ledger.proposals import ProposalService
from ledger.storage.database import init_database

READ_CONTRACT_DIGEST = "f352df8ba05997d0a8a23576d06aa964d733019b15058dcde9bf230785133d26"
WRITE_CONTRACT_DIGEST = "fd83f70610243aaf6fdd324ac1d0f9abfe696679f631f5a9c90e44b03f0e6283"
MCP_PROTOCOL_MODERN = "2026-07-28"
MCP_PROTOCOL_LEGACY = "2025-11-25"
runner = CliRunner()


def _file_arguments(marker: str = "SYNTHETIC CAFE") -> dict[str, object]:
    return {
        "merchant_norm": marker,
        "pattern": marker,
        "category": "Dining",
        "evidence_id": "e" * 40,
    }


def test_write_contract_import_cannot_change_the_read_contract() -> None:
    before_tools = copy.deepcopy(read_tool_schemas())
    before_digest = read_contract_digest()

    assert read_contract_digest() == READ_CONTRACT_DIGEST == before_digest
    assert read_tool_schemas() == before_tools
    assert [tool["name"] for tool in write_tool_schemas()] == [
        "ledger.proposals.file",
        "ledger.proposals.planning.file",
    ]
    assert write_contract_digest() == WRITE_CONTRACT_DIGEST
    assert write_contract_digest() != read_contract_digest()


def test_write_tool_files_only_to_proposals_db_and_does_not_echo_payload(
    tmp_path: Path,
) -> None:
    from ledger.mcp.write_server import call_tool

    data_dir = tmp_path / "private-data"
    proposals_db = data_dir / "proposals.db"
    ledger_db = data_dir / "ledger.db"
    marker = "PRIVATE-SYNTHETIC-MERCHANT"

    result = call_tool(
        ProposalService(proposals_db), "ledger.proposals.file", _file_arguments(marker)
    )

    encoded = json.dumps(result, sort_keys=True)
    assert result.get("isError") is not True
    assert result["structuredContent"]["data"]["status"] == "pending"
    proposal_id = result["structuredContent"]["data"]["proposal_id"]
    stored = ProposalService(proposals_db).show_proposal(proposal_id)
    assert stored.merchant_norm == marker
    assert proposals_db.exists()
    assert not ledger_db.exists()
    assert marker not in encoded


@pytest.mark.parametrize(
    "arguments",
    [
        {**_file_arguments(), "private-extra-key": "PRIVATE-ARGUMENT"},
        {**_file_arguments(), "pattern": "PRIVATE-REGEX-(.*)+"},
        {**_file_arguments(), "evidence_id": "PRIVATE-EVIDENCE"},
    ],
)
def test_write_tool_argument_errors_are_content_free(
    tmp_path: Path,
    arguments: dict[str, object],
) -> None:
    from ledger.mcp.write_server import call_tool

    result = call_tool(
        ProposalService(tmp_path / "proposals.db"),
        "ledger.proposals.file",
        arguments,
    )
    encoded = json.dumps(result, sort_keys=True)

    assert result["isError"] is True
    assert result["structuredContent"] == {
        "schema": "ledger.mcp.write.v1",
        "error": {"code": "invalid_argument", "retryable": False},
    }
    assert "PRIVATE-" not in encoded


@pytest.mark.parametrize(
    ("mode", "expected_protocol"),
    [("auto", MCP_PROTOCOL_MODERN), ("legacy", MCP_PROTOCOL_LEGACY)],
)
def test_write_protocol_lists_only_filing_tools_and_unknown_tool_is_opaque(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    mode: str,
    expected_protocol: str,
) -> None:
    import anyio
    from mcp import Client

    async def exercise_protocol() -> tuple[str, object, object]:
        server = create_server(ProposalService(tmp_path / "proposals.db"))
        async with Client(server, mode=mode) as client:
            protocol_version = client.protocol_version
            listing = await client.list_tools()
            invalid = await client.call_tool(
                "PRIVATE-UNKNOWN-TOOL",
                {"value": "PRIVATE-UNKNOWN-ARGUMENT"},
            )
        return protocol_version, listing, invalid

    caplog.set_level(logging.ERROR)
    protocol_version, listing, invalid = anyio.run(exercise_protocol)
    encoded = json.dumps(invalid.model_dump(by_alias=True), sort_keys=True)

    assert protocol_version == expected_protocol
    assert [tool.model_dump(by_alias=True, exclude_none=True) for tool in listing.tools] == (
        write_tool_schemas()
    )
    assert invalid.structured_content == {
        "schema": "ledger.mcp.write.v1",
        "error": {"code": "invalid_argument", "retryable": False},
    }
    assert "PRIVATE-" not in encoded
    assert "PRIVATE-" not in caplog.text


def test_write_protocol_parallel_first_use_commits_every_isolated_proposal(
    tmp_path: Path,
) -> None:
    import asyncio

    import anyio
    from mcp import Client

    proposals_db = tmp_path / "private" / "proposals.db"
    service = ProposalService(proposals_db)

    async def exercise_protocol() -> list[object]:
        async with Client(create_server(service), mode="auto") as client:
            return await asyncio.gather(
                *(
                    client.call_tool(
                        "ledger.proposals.file",
                        _file_arguments(f"SYNTHETIC CAFE {index:02d}"),
                    )
                    for index in range(12)
                )
            )

    results = anyio.run(exercise_protocol)

    assert all(not result.is_error for result in results)
    proposal_ids = {str(result.structured_content["data"]["proposal_id"]) for result in results}
    assert len(proposal_ids) == 12
    assert {
        proposal.proposal_id
        for proposal in ProposalService(proposals_db).list_proposals(status="pending")
    } == proposal_ids
    assert stat.S_IMODE(proposals_db.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(proposals_db.stat().st_mode) == 0o600
    assert not (proposals_db.parent / "ledger.db").exists()
    assert not any(
        Path(f"{proposals_db.parent / 'ledger.db'}{suffix}").exists()
        for suffix in ("-journal", "-wal", "-shm")
    )


@pytest.mark.parametrize(
    ("mode", "expected_protocol"),
    [("auto", MCP_PROTOCOL_MODERN), ("legacy", MCP_PROTOCOL_LEGACY)],
)
def test_actual_write_stdio_files_only_to_proposal_store(
    tmp_path: Path,
    mode: str,
    expected_protocol: str,
) -> None:
    import anyio
    from mcp import Client
    from mcp.client.stdio import StdioServerParameters, stdio_client

    proposals_db = tmp_path / "private" / "proposals.db"
    command = Path(sys.executable).with_name("ledger-mcp-write")
    error_path = tmp_path / f"write-{mode}.stderr"

    async def exercise_protocol(errors: TextIO) -> tuple[object, str, object, object, object]:
        parameters = StdioServerParameters(
            command=str(command), args=["--proposals-db", str(proposals_db)]
        )
        with anyio.fail_after(10):
            async with Client(stdio_client(parameters, errlog=errors), mode=mode) as client:
                listing = await client.list_tools()
                filed = await client.call_tool("ledger.proposals.file", _file_arguments())
                unknown = await client.call_tool(
                    "PRIVATE-UNKNOWN-TOOL",
                    {"value": "PRIVATE-UNKNOWN-ARGUMENT"},
                )
                return client.server_info, client.protocol_version, listing, filed, unknown

    started = time.monotonic()
    with error_path.open("w+", encoding="utf-8") as errors:
        server_info, protocol_version, listing, filed, unknown = anyio.run(
            exercise_protocol, errors
        )
        errors.seek(0)
        stderr = errors.read()
    elapsed = time.monotonic() - started

    assert elapsed < 10
    assert stderr == ""
    assert server_info is not None
    assert (server_info.name, server_info.version) == ("Ledger Proposal Writer", __version__)
    assert protocol_version == expected_protocol
    assert [tool.model_dump(by_alias=True, exclude_none=True) for tool in listing.tools] == (
        write_tool_schemas()
    )
    assert not filed.is_error
    assert filed.structured_content["data"]["status"] == "pending"
    assert unknown.structured_content == {
        "schema": "ledger.mcp.write.v1",
        "error": {"code": "invalid_argument", "retryable": False},
    }
    assert "PRIVATE-" not in json.dumps(unknown.model_dump(by_alias=True), sort_keys=True)
    assert proposals_db.exists()
    assert not (proposals_db.parent / "ledger.db").exists()


@pytest.mark.parametrize(
    "arguments",
    [
        ["--proposals-db", "/private/owner/proposals.db", "--unexpected", "PRIVATE-VALUE"],
        ["--proposals-db", "/private/owner/proposals.db", "PRIVATE-POSITIONAL"],
    ],
)
def test_write_server_parser_errors_do_not_echo_private_values(
    arguments: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ledger.mcp.write_server import main

    assert main(arguments) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "ledger_mcp_write_startup_failed:invalid_argument\n"
    assert "PRIVATE" not in captured.err
    assert "/private/" not in captured.err


def test_write_server_startup_failure_is_opaque(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ledger.mcp.write_server import main

    private_marker = tmp_path / "PRIVATE-STORE"
    private_marker.mkdir()

    assert main(["--proposals-db", str(private_marker)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "ledger_mcp_write_startup_failed:store_unavailable\n"
    assert "PRIVATE-STORE" not in captured.err


def test_write_server_owns_stdio_and_has_a_distinct_entry_point(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import asynccontextmanager

    from mcp.server import Server

    from ledger.mcp.write_server import run_stdio

    proposals_db = tmp_path / "private" / "proposals.db"
    transports: list[str] = []

    @asynccontextmanager
    async def fake_stdio():
        transports.append("stdio")
        yield object(), object()

    async def fake_run(_server: object, *_args: object, **_kwargs: object) -> None:
        transports.append("run")

    monkeypatch.setattr("ledger.mcp.write_server.stdio_server", fake_stdio)
    monkeypatch.setattr(Server, "run", fake_run)

    run_stdio(proposals_db)

    project = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert transports == ["stdio", "run"]
    assert proposals_db.exists()
    assert not (proposals_db.parent / "ledger.db").exists()
    assert project["project"]["scripts"]["ledger-mcp-write"] == ("ledger.mcp.write_server:main")


def test_proposal_cli_non_apply_commands_never_create_ledger_db(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    proposals_db = data_dir / "proposals.db"
    ledger_db = data_dir / "ledger.db"
    filed = ProposalService(proposals_db).file_merchant_rule(
        merchant_norm="SYNTHETIC CAFE",
        pattern="SYNTHETIC CAFE",
        category="Dining",
        evidence_id="f" * 40,
    )

    listed = runner.invoke(
        app,
        ["proposals", "list", "--proposals-db", str(proposals_db)],
    )
    shown = runner.invoke(
        app,
        ["proposals", "show", filed.proposal_id, "--proposals-db", str(proposals_db)],
    )
    rejected = runner.invoke(
        app,
        ["proposals", "reject", filed.proposal_id, "--proposals-db", str(proposals_db)],
    )

    assert listed.exit_code == 0, listed.output
    assert shown.exit_code == 0, shown.output
    assert rejected.exit_code == 0, rejected.output
    assert filed.proposal_id in listed.output
    assert "SYNTHETIC CAFE" in shown.output
    assert "rejected" in rejected.output
    assert not ledger_db.exists()


def test_proposal_cli_errors_are_content_free(tmp_path: Path) -> None:
    proposals_db = tmp_path / "data" / "proposals.db"
    ProposalService(proposals_db).file_merchant_rule(
        merchant_norm="SYNTHETIC CAFE",
        pattern="SYNTHETIC CAFE",
        category="Dining",
        evidence_id="a" * 40,
    )
    private_id = "PRIVATE-MISSING-PROPOSAL"

    missing = runner.invoke(
        app,
        ["proposals", "show", private_id, "--proposals-db", str(proposals_db)],
    )
    bad_status = runner.invoke(
        app,
        [
            "proposals",
            "list",
            "--status",
            "PRIVATE-STATUS",
            "--proposals-db",
            str(proposals_db),
        ],
    )

    assert missing.exit_code == 1
    assert missing.output.strip() == "proposal_command_failed:not_found"
    assert private_id not in missing.output
    assert bad_status.exit_code == 1
    assert bad_status.output.strip() == "proposal_command_failed:invalid_argument"
    assert "PRIVATE-STATUS" not in bad_status.output


def test_proposal_cli_apply_is_the_command_that_opens_and_mutates_the_ledger(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(mode=0o700)
    ledger_db = data_dir / "ledger.db"
    proposals_db = data_dir / "proposals.db"
    init_database(ledger_db)
    _seed_config(ledger_db)
    _insert_txn(ledger_db, "txn-cli-proposal")
    filed = ProposalService(proposals_db).file_merchant_rule(
        merchant_norm="UBER EATS",
        pattern="UBEREATS",
        category="Dining",
        evidence_id="b" * 40,
    )

    applied = runner.invoke(
        app,
        [
            "proposals",
            "apply",
            filed.proposal_id,
            "--proposals-db",
            str(proposals_db),
            "--db",
            str(ledger_db),
            "--backup-dir",
            str(tmp_path / "backups"),
        ],
    )

    assert applied.exit_code == 0, applied.output
    payload = json.loads(applied.output)
    assert payload["status"] == "applied"
    assert payload["proposal_id"] == filed.proposal_id
    assert _merchant_rules(ledger_db)["UBER EATS"][0] == "Dining"
