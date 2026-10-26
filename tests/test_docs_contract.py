from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _agent_reference(name: str) -> str:
    relative = f"docs/agent/{name}.md"
    router = (REPO_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert f"]({relative})" in router, f"the root guide must route to {relative}"
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    index = (REPO_ROOT / "docs/README.md").read_text(encoding="utf-8")
    assert f"]({relative})" in readme, f"the overview must route to {relative}"
    assert f"](agent/{name}.md)" in index, f"the docs index must route to {relative}"
    return (REPO_ROOT / relative).read_text(encoding="utf-8")


def test_private_defaults_template_does_not_select_a_financial_target() -> None:
    example = tomllib.loads((REPO_ROOT / ".ledgerrc.example").read_text())["defaults"]
    assert not {"src", "out_dir", "db"} & example.keys()
    assert example["formats"] == "none"
    operations = _agent_reference("local-operations")
    assert "LEDGER_HOME" in operations and "defaults.toml" in operations


def test_root_entry_points_route_to_the_documentation_index() -> None:
    for name in ("README.md", "AGENTS.md"):
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        assert "](docs/README.md)" in text


def test_pdf_import_skill_verifies_the_persisted_financial_brief() -> None:
    skill = (REPO_ROOT / "docs/agent/skills/ledger-pdf-import/SKILL.md").read_text(encoding="utf-8")

    assert "financial_brief" in skill
    for artifact in ("run.json", "manifest.json", "final-report.json"):
        assert artifact in skill
    assert "three to five" in skill
    assert "coverage" in skill
    assert "imported records only" in skill


def test_pdf_import_skill_routes_existing_runs_before_staging() -> None:
    skill = (REPO_ROOT / "docs/agent/skills/ledger-pdf-import/SKILL.md").read_text(encoding="utf-8")
    routing = skill.split("## Choose the operation", 1)[1].split("## Stage a new import", 1)[0]
    prose = " ".join(routing.split())

    assert "start with `./scripts/import-pdf-inbox status --run-dir <run-dir>`" in prose
    assert "Do not stage a new run for a status, review, resume, or cancel request" in prose
    routes = {
        cells[1].strip(): cells[2].strip()
        for line in routing.splitlines()
        if line.startswith("|") and len(cells := line.split("|")) == 4
    }
    for operation, command in (
        ("New import", "stage"),
        ("Existing-run status", "status"),
        ("Existing-run review", "review list --run-dir <run-dir>"),
        ("Finalize or resume an existing run", "finalize --run-dir <run-dir>"),
        ("Cancel an existing run", "cancel --run-dir <run-dir>"),
    ):
        assert f"`{command}`" in routes[operation]
    assert "does not authorize review writes or promotion" in routes["Existing-run status"]
    assert "finalize recovery states require authorized `finalize`, not cancellation" in prose
    assert "refuses an incomplete `staging` run" in prose
    assert "never edit status, bypass a gate, or restage to evade a recovery refusal" in prose
    assert "temporary-validation" in routes["Synthetic validation"]


def test_bootstrap_and_local_lock_ownership_are_documented() -> None:
    readme = _agent_reference("development")

    assert "interpreter/platform" in readme
    assert "pip freeze --all --exclude-editable" in readme
    for wrapper, profile in (
        ("import-pdf-inbox", "core"),
        ("audit-statements", "core"),
        ("rebuild-db", "analysis"),
        ("run-app", "ui"),
        ("freeze-check", "full-dev"),
        ("ui-smoke", "full-dev"),
    ):
        assert wrapper in readme and profile in readme


def test_fresh_install_and_lock_refresh_docs_cover_local_derivatives_end_to_end() -> None:
    required_install_steps = (
        "./scripts/build-downstream-wheels",
        ".venv/bin/python -m pip install --no-index --no-deps",
        "$LEDGER_WHEEL_DIR/streamlit-1.66.1-py3-none-any.whl",
        "$LEDGER_WHEEL_DIR/playwright-1.62.0+ledger.1-py3-none-macosx_26_0_arm64.whl",
        '.venv/bin/python -m pip install --find-links "$LEDGER_WHEEL_DIR" -r requirements.lock',
        ".venv/bin/python -m pip install --no-deps --force-reinstall -e .",
        ".venv/bin/python -m pip check",
    )
    required_lock_steps = (
        ".venv/bin/python -m pip freeze --all --exclude-editable",
        "s#^playwright @ .*$#playwright==1.62.0+ledger.1#",
        "s#^streamlit @ .*$#streamlit==1.66.1#",
        "LC_ALL=C sort",
    )

    _agent_reference("development")
    for doc_name in ("docs/agent/development.md",):
        docs = (REPO_ROOT / doc_name).read_text(encoding="utf-8")
        install_positions = [docs.index(step) for step in required_install_steps]
        assert install_positions == sorted(install_positions), (
            f"{doc_name} must document the complete local-parent install in executable order"
        )
        lock_positions = [docs.index(step) for step in required_lock_steps]
        assert lock_positions == sorted(lock_positions), (
            f"{doc_name} must normalize both local parent URLs before writing the canonical lock"
        )


def test_public_docs_distinguish_source_verification_from_client_activation() -> None:
    readme = (REPO_ROOT / "README.md").read_text()
    development = _agent_reference("development")
    for text in (readme, development):
        assert "Python 3.14" in text
    assert "MCP Python SDK 2.2" in development
    assert "LEDGER_FREEZE_SYNTHETIC=1" in development
    assert "LEDGER_FREEZE_SOURCE_ONLY=1" in development
    project_version = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"][
        "version"
    ]
    assert project_version in readme
    assert "docs/release/" not in readme


def test_both_canonical_skills_have_explicit_client_installation_contracts() -> None:
    text = _agent_reference("delivery")
    assert "byte-exact" in text
    for skill in ("ledger-query", "ledger-pdf-import"):
        for prefix in ("docs/agent/skills/", "~/.agents/skills/", "~/.claude/skills/"):
            assert f"{prefix}{skill}/" in text


def test_public_source_license_and_identity_are_consistent() -> None:
    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())["project"]
    assert project["license"] == "MIT"
    assert "Private :: Do Not Upload" not in project["classifiers"]
    assert "MIT License" in (REPO_ROOT / "LICENSE").read_text()
    assert project["version"] in (REPO_ROOT / "README.md").read_text()


def test_private_historical_release_records_are_not_distributed() -> None:
    assert not (REPO_ROOT / "docs/release").exists()


def test_public_docs_preserve_unavailable_evidence_semantics() -> None:
    readme = (REPO_ROOT / "README.md").read_text()
    assert "missing evidence as zero" in readme
    imports = _agent_reference("imports-and-recovery")
    assert "current value as unavailable (not zero)" in imports


def test_investment_import_docs_describe_the_supported_workflow() -> None:
    agents = _agent_reference("imports-and-recovery")

    for docs in (agents,):
        assert "./scripts/import-investment-inbox stage" in docs
        assert "USD" in docs and "historical" in docs
        assert "closed_on" in docs and "operator" in docs
        assert "archive_pending" in docs
        assert "rebuild-db" in docs
        assert "investment tables" in docs


def test_investment_docs_distinguish_private_manifests_and_structured_csv_retention() -> None:
    agents = _agent_reference("imports-and-recovery")

    assert "ordinary transaction-PDF workflow" in agents
    assert "archived" in agents and "structured CSV" in agents
    for docs in (agents,):
        assert "private manifests" in docs
        assert "redacted manifests" not in docs


def test_investment_documentation_preserves_private_and_historical_boundaries() -> None:
    imports = _agent_reference("imports-and-recovery")
    assert "evidence-only PDF" in imports
    assert "historical" in imports
    assert "private" in imports


def test_investment_docs_require_confirmed_lifecycle_without_inventing_a_date() -> None:
    texts = [
        _agent_reference("imports-and-recovery"),
    ]
    for text in texts:
        docs = " ".join(text.split())

        assert "--mark-closed <operator-confirmed-account-alias>" in docs
        assert "--transfer-destination <operator-confirmed-destination>" in docs
        assert "--status-confirmed-on <YYYY-MM-DD>" in docs
        assert "Leave `closed_on` unset" in docs
        assert "no exact closure date" in docs


def test_agents_states_both_repair_survival_mechanisms_separately() -> None:
    """The root guide routes destructive verbs to their full recovery contract.

    TWO different mechanisms carry state through `repair statements`, and the
    rebuild-hazard paragraph attributed both to one. `merge_forward` copies
    `MERGE_FORWARD_TABLES` and nothing else; merchant rules are deliberately NOT
    in that tuple (readiness.py names them in its FAIL-SAFE BOUNDARY comment).
    They survive through `copy_runtime_config`, which reads them from the LIVE
    database whenever `source_repo.has_runtime_config()` -- the normal case -- and
    falls back to the scrubbed placeholder TOMLs only when it does not.

    Saying merchant rules are "merged forward" tells an agent to trust a tuple
    membership that does not exist; saying they are always reseeded from the
    placeholder TOMLs tells it to expect data loss that does not happen. Both
    mislead a destructive-verb decision, so both are pinned.
    """
    from ledger.imports.pdf.readiness import MERGE_FORWARD_TABLES

    agents = " ".join(_agent_reference("imports-and-recovery").split())

    assert "merchant_rules" not in MERGE_FORWARD_TABLES, (
        "merchant rules joined MERGE_FORWARD_TABLES; the recovery docs describe the wrong mechanism"
    )
    # Mechanism one: the tuple, named in full so the doc cannot drift from it.
    assert "MERGE_FORWARD_TABLES" in agents
    for table in MERGE_FORWARD_TABLES:
        assert f"`{table}`" in agents, f"recovery docs omit merge-forward table {table}"
    # Mechanism two: named, and stated as reading the LIVE database first.
    assert "copy_runtime_config" in agents
    assert "has_runtime_config" in agents
    assert "synthetic seed TOMLs only when" in agents
    # And the two are not conflated.
    assert "not in that tuple" in agents


def test_investment_docs_require_private_directory_and_file_modes() -> None:
    _agent_reference("imports-and-recovery")
    for doc_name in ("docs/agent/imports-and-recovery.md",):
        docs = (REPO_ROOT / doc_name).read_text(encoding="utf-8")

        assert "0700" in docs
        assert "0600" in docs
        assert "managed directories" in docs
        assert "managed files" in docs


def test_the_mcp_reference_names_the_same_signed_surfaces_the_schemas_serve() -> None:
    """The same sentence lives in the MCP reference, in SERVER_INSTRUCTIONS and in the
    `account_balances` docstring, and only the schemas decide whether it is true.

    None of the three is digest-bound for its prose, so another tool adopting the
    signed money schema rotates the digest for its own reason and leaves all
    three false with nothing to catch it.
    """
    from ledger.mcp.contracts import SIGNED_MONEY_SCHEMA, tool_schemas
    from ledger.mcp.investments import EXACT_DECIMAL_PATTERN

    patterns = {SIGNED_MONEY_SCHEMA["pattern"], EXACT_DECIMAL_PATTERN}

    def serves(schema: object) -> bool:
        if isinstance(schema, dict):
            return schema.get("pattern") in patterns or any(
                serves(value) for value in schema.values()
            )
        return isinstance(schema, list) and any(serves(value) for value in schema)

    serving = {str(tool["name"]) for tool in tool_schemas() if serves(tool["outputSchema"])}
    readme = " ".join(_agent_reference("mcp").split())
    claim = next(
        sentence
        for sentence in readme.split(". ")
        if "only tools whose monetary fields go negative" in sentence
    )

    assert serving == {
        "ledger.balances",
        "ledger.brief",
        "ledger.budgets",
        "ledger.cash_plan",
        "ledger.explain",
        "ledger.investments.activities",
        "ledger.investments.positions",
        "ledger.investments.statement.get",
        "ledger.investments.statements",
        "ledger.net_worth",
        "ledger.plan_reconciliation",
        "ledger.projection",
    }
    assert set(re.findall(r"ledger\.[a-z_]+(?:\.[a-z_]+)*", claim.replace("`", ""))) == serving


def test_projection_documentation_discloses_omitted_transfers() -> None:
    """The limitation is a current contract, not a historical owner-data amount."""
    from ledger.query.agent import AgentQueries

    reference = " ".join(_agent_reference("mcp").split())
    docstring = " ".join((AgentQueries.projection.__doc__ or "").split())
    for text in (reference, docstring):
        assert "not a forecast" in text
        assert "card payments" in text
        assert "savings sweeps" in text
    assert "accounts are not projected" in reference


def test_the_projection_caveats_travel_in_band_rather_than_in_the_routing_file() -> None:
    """The same four claims, moved to where they cannot go missing (D33).

    They were pinned into `ledger-query/SKILL.md`, which helps only an agent that
    copied that file into its skills directory. `SERVER_INSTRUCTIONS` travels in
    the MCP initialize result, so every caller has it; it is also digest-neutral,
    so only a test like this one keeps it honest. The strings below are quoted
    from the shipped text rather than paraphrased.
    """
    from ledger.mcp.contracts import SERVER_INSTRUCTIONS

    skill = " ".join(
        (REPO_ROOT / "docs/agent/skills/ledger-query/SKILL.md").read_text(encoding="utf-8").split()
    )
    claim = " ".join(SERVER_INSTRUCTIONS.split())

    assert "Read each entry's status before its figures" in claim
    assert "the SIGNED periods between them" in claim
    assert "is_overdue is derived from those dates and is an inference" in claim
    assert "read patterns_past_due for that" in claim
    assert "Attribution does not follow a merchant that moves between accounts" in claim
    # And the routing file points at that contract instead of restating it.
    assert "the server sends it in the MCP initialize result" in skill


def test_the_cursor_contract_travels_in_band_after_the_routing_file_dropped_it() -> None:
    """Two caller-facing rules the D33 compaction deleted from the only place
    that carried them.

    The routing file said "follow its cursor to completion, and changing any
    input invalidates it". Nothing else did: not `SERVER_INSTRUCTIONS`, not any
    of the fourteen tool descriptions, not one schema `description`, and the
    `cursor`/`next_cursor`/`has_more` properties carry none at all. So the whole
    shipped payload contained exactly ONE sentence about a rejected cursor --
    "cursors from an earlier generation are rejected" -- which made "the
    underlying data changed" the only documented cause of `invalid_cursor`, and a
    caller that narrowed a filter mid-walk would report a data change that never
    happened.

    Verified against the WHOLE wire payload, so a claim that lives only in a
    repo file someone may never have copied does not satisfy it.
    """
    import json

    from ledger.mcp.contracts import SERVER_INSTRUCTIONS, tool_schemas

    payload = " ".join((SERVER_INSTRUCTIONS + json.dumps(tool_schemas())).split())

    assert "cursor is followed to completion" in payload
    assert "Changing one invalidates the cursor" in payload
    assert "keep sending `next_cursor` back until it is null" in payload
    # Both causes of `invalid_cursor`, told apart.
    assert "That error therefore has two causes and they are different problems" in payload
    assert "invalid_cursor" in payload


def test_the_published_cursor_binding_is_the_binding_the_server_actually_enforces(tmp_path) -> None:
    """The restored sentence is checked against the WIRE, not against its author.

    The routing file said "changing any input invalidates it" and that is not
    true: `limit` is deliberately outside `_cursor_fingerprint`, because page size
    does not renumber the result set, so a replay with a different limit is
    accepted and correct. Restoring the caveat as written would have re-published
    a false universal on a surface that reaches every caller -- so the exception
    is named, and both halves are measured here rather than asserted.
    """
    from mcp_synthetic import build_synthetic_mcp_database

    from ledger.mcp.contracts import SERVER_INSTRUCTIONS
    from ledger.mcp.server import call_tool
    from ledger.mcp.service import LedgerMCPService

    service = LedgerMCPService(build_synthetic_mcp_database(tmp_path / "ledger.db"))
    try:
        minted = call_tool(service, "ledger.coverage", {"limit": 2, "start_period": "2025-01"})
        page = minted["structuredContent"]["page"]  # type: ignore[index]
        cursor = page["next_cursor"]
        assert page["has_more"] and cursor, "the fixture did not page; the probes are vacuous"

        wider = call_tool(
            service,
            "ledger.coverage",
            {"limit": 3, "start_period": "2025-01", "cursor": cursor},
        )
        narrowed = call_tool(
            service,
            "ledger.coverage",
            {"limit": 2, "start_period": "2026-02", "cursor": cursor},
        )
    finally:
        service.close()

    claim = " ".join(SERVER_INSTRUCTIONS.split())
    assert "`limit` is the one exception: it is deliberately not bound" in claim
    assert wider.get("isError") is not True, wider
    assert narrowed["structuredContent"]["error"]["code"] == "invalid_cursor"  # type: ignore[index]
