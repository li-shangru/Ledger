"""Synthetic behavioral contracts for routing, launch evidence and qualification."""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import pytest
import test_agent_factory as baseline

factory = baseline.factory


@pytest.fixture
def case():
    fixture = baseline.FactoryTests("runTest")
    try:
        fixture.setUp()
        yield fixture
    finally:
        fixture.doCleanups()


def live_task(**changes):
    task = {
        "schema_version": 1,
        "id": "routing",
        "title": "Inspect helper behavior",
        "description": "Preserve the stated requirements.",
        "paths": [],
        "risks": [],
        "acceptance": ["Behavior remains correct."],
        "constraints": ["Synthetic only."],
    }
    task.update(changes)
    return task


def route(**changes):
    repo = Path(__file__).resolve().parents[1]
    return factory.select(factory.load_catalog(repo), live_task(**changes))


def ids(result):
    return {row["id"] for row in result["selected"]}


def test_coupled_paths_and_aliases_cover_distinct_domains():
    assert ids(route(paths=["src/ledger/query/investments.py"])) == {
        "exact-arithmetic",
        "query-provenance",
    }
    assert ids(route(paths=["src/ledger/imports/pdf/workflow.py"])) == {
        "statement-ingestion",
        "publication-recovery",
    }
    result = route(paths=["src/ledger/query/agent.py"], risks=["currency-boundaries"])
    assert not result["uncovered_risks"]
    assert result["canonical_risks"] == ["currency"]


def test_acceptance_and_bilingual_topics_but_not_generic_or_negated_terms():
    assert ids(route(acceptance=["Preserve exact arithmetic and native currency totals."])) == {
        "exact-arithmetic"
    }
    assert ids(route(title="修复小数精度并保留来源覆盖")) == {
        "exact-arithmetic",
        "query-provenance",
    }
    assert route(title="Clarify unavailable help in README")["abstained"]
    assert route(title="Documentation only, no decimal changes")["abstained"]


def test_canonical_risk_vocabulary_in_acceptance_creates_domain_obligations():
    result = route(
        paths=["README.md"],
        acceptance=["Preserve exact-arithmetic, currency and source-coverage invariants."],
    )
    assert ids(result) == {"exact-arithmetic", "query-provenance"}
    assert set(result["canonical_risks"]) == {"exact-arithmetic", "currency", "source-coverage"}
    assert not result["uncovered_risks"]
    assert route(title="Documentation only, no exact-arithmetic changes")["abstained"]


def test_negated_hyphenated_risk_does_not_reactivate_interior_terms():
    for text in [
        "Documentation only, no atomic-publication changes",
        "Documentation only, no numeric-precision changes",
        "Documentation only, no currency-boundaries changes",
    ]:
        result = route(title=text)
        assert result["abstained"]
        assert not result["canonical_risks"]


def test_generic_catalog_preserves_hyphenated_profile_term_matching():
    catalog = {
        "factory_id": "fixture",
        "max_advisors": 3,
        "profiles": [
            {
                "id": "menu-ui",
                "version": "1.0.0",
                "terms": ["menu", "button"],
                "paths": [],
                "risks": ["ui"],
            }
        ],
    }
    result = factory.select(catalog, live_task(title="Add menu-button action"))
    assert ids(result) == {"menu-ui"}


def test_exclusion_keeps_domain_obligation_visible():
    result = route(
        paths=["src/ledger/query/investments.py"],
        exclusions=[{"profile_id": "exact-arithmetic", "reason": "Coordinator checks literals."}],
    )
    assert "exact-arithmetic" not in ids(result)
    assert any(r["risk"] == "exact-arithmetic" for r in result["uncovered_risks"])
    assert result["excluded"][0]["id"] == "exact-arithmetic"


def test_missing_new_path_owner_is_an_explicit_gap():
    result = route(planned_paths=["src/ledger/new_domain.py"])
    assert result["unowned_paths"] == ["src/ledger/new_domain.py"]
    assert any(r["risk"] == "unowned-path" for r in result["uncovered_risks"])


def test_instruction_and_explicit_dependency_drift_are_bound(case):
    case.write("AGENTS.md", "Keep records synthetic.\n")
    case.write("docs/contract.md", "Required contract.\n")
    case.task["required_sources"] = ["docs/contract.md"]
    case.task_file.write_text(json.dumps(case.task))
    case.initialize()
    case.write("AGENTS.md", "Changed instruction.\n")
    status = case.cli("status", "--run", case.run)
    assert "instruction_sha256" in status["drift_fields"]
    case.cli("candidate", "--run", case.run, expected=2)


def test_author_canonicalizes_without_rewriting_active_task(case):
    output = case.records / "canonical.json"
    case.cli("author", "--task", case.task_file, "--output", output)
    assert output.read_bytes() == factory.encoded(case.task)
    case.cli("author", "--task", case.task_file, "--output", output, expected=2)


def test_prepared_launch_is_persisted_and_wrong_hash_is_refused(case):
    case.initialize()
    prepared = case.cli("packet", "--run", case.run, "--agent", "ui")
    assert prepared["launch_sha256"] == factory.digest(prepared["launch_prompt"].encode())
    case.cli(
        "start",
        "--run",
        case.run,
        "--agent",
        "ui",
        "--worker-id",
        "host/test",
        "--launch-sha256",
        "0" * 64,
        expected=2,
    )
    case.cli(
        "start",
        "--run",
        case.run,
        "--agent",
        "ui",
        "--worker-id",
        "host/test",
        "--launch-sha256",
        prepared["launch_sha256"],
    )
    status = case.cli("status", "--run", case.run)
    assert status["workers"]["ui"]["launch_sha256"] == prepared["launch_sha256"]


def test_finding_requires_real_inspected_location(case):
    case.initialize()
    packet = case.cli("packet", "--run", case.run, "--agent", "ui")
    payload = json.loads(case.result_file().read_text())
    payload["findings"][0].pop("locations")
    payload["findings"][0]["evidence"] = "Invented conclusion without a location"
    with pytest.raises(factory.FactoryError):
        factory.validate_result(case.repo, payload, packet["dispatch"])
    payload["findings"][0]["evidence"] = "src/ui.swift:9999"
    with pytest.raises(factory.FactoryError):
        factory.validate_result(case.repo, payload, packet["dispatch"])


def test_candidate_cannot_omit_never_declared_change_or_later_new_file(case):
    case.complete_advice()
    case.write("src/other.py", "VALUE = 1\n")
    case.cli("candidate", "--run", case.run, expected=2)
    candidate = case.cli("candidate", "--run", case.run, "--path", "src/other.py")
    case.write("src/later.py", "VALUE = 2\n")
    evidence = case.records / "checks.json"
    evidence.write_text("Synthetic check evidence")
    case.cli(
        "checkpoint",
        "--run",
        case.run,
        "--candidate",
        candidate["candidate_id"],
        "--stage",
        "verification",
        "--outcome",
        "passed",
        "--evidence",
        evidence,
        expected=2,
    )


def test_measurement_unavailable_is_explicit_and_not_retry_identity(case):
    case.initialize()
    before = json.loads((case.run / "run.json").read_text())["epochs"][0]["binding"]
    evidence = case.records / "timing.json"
    evidence.write_text(json.dumps({"actual": "coordinator observation"}))
    case.cli(
        "measure",
        "--run",
        case.run,
        "--stage",
        "exploration",
        "--worker-id",
        "host/owner",
        "--seconds",
        "1.25",
        "--tool-calls",
        "2",
        "--evidence",
        evidence,
        "--usage-unavailable",
        "Host exposes no aggregate token telemetry.",
    )
    status = case.cli("status", "--run", case.run)
    assert status["metrics"]["usage"] is None
    assert status["metrics"]["measurements"] == 1
    after = json.loads((case.run / "run.json").read_text())["epochs"][0]["binding"]
    assert before == after


def test_cap_omissions_include_implicit_path_domains():
    catalog = factory.load_catalog(Path(__file__).resolve().parents[1])
    catalog = copy.deepcopy(catalog)
    catalog["max_advisors"] = 1
    result = factory.select(catalog, live_task(paths=["src/ledger/query/investments.py"]))
    assert result["uncovered_risks"]


def test_planned_source_can_be_missing_during_dispatch(case):
    case.task["planned_paths"] = ["src/new.swift"]
    case.task_file.write_text(json.dumps(case.task))
    case.initialize()
    prepared = case.cli("packet", "--run", case.run, "--agent", "ui")
    assert prepared["dispatch_status"] == "not_started"


def test_required_dependency_coverage_cannot_be_omitted(case):
    case.initialize()
    case.cli("start", "--run", case.run, "--agent", "ui", "--worker-id", "host/ui")
    result = case.result_file()
    payload = json.loads(result.read_text())
    payload["source_coverage"] = {}
    result.write_text(json.dumps(payload))
    case.cli(
        "result",
        "--run",
        case.run,
        "--agent",
        "ui",
        "--worker-id",
        "host/ui",
        "--result",
        result,
        expected=2,
    )


def test_supplemental_observation_does_not_rewrite_dispatch_and_stales_launch(case):
    case.initialize()
    second = case.cli("packet", "--run", case.run, "--agent", "state")
    case.cli("start", "--run", case.run, "--agent", "ui", "--worker-id", "host/ui")
    case.write("docs/extra.md", "Supplemental synthetic source.\n")
    result = case.result_file()
    payload = json.loads(result.read_text())
    payload["inspected"].append("docs/extra.md")
    result.write_text(json.dumps(payload))
    case.cli(
        "result", "--run", case.run, "--agent", "ui", "--worker-id", "host/ui", "--result", result
    )
    case.cli(
        "start",
        "--run",
        case.run,
        "--agent",
        "state",
        "--worker-id",
        "host/state",
        "--launch-sha256",
        second["launch_sha256"],
        expected=2,
    )
    fresh = case.cli("packet", "--run", case.run, "--agent", "state")
    assert fresh["dispatch"] == second["dispatch"]
    state = json.loads((case.run / "run.json").read_text())
    assert (
        factory.digest(factory.encoded(state["epochs"][0]["workers"]["state"]["dispatch_binding"]))
        == fresh["dispatch"]["input_sha256"]
    )


def test_findings_need_decisions_and_accepted_checks_on_exact_candidate(case):
    case.complete_advice()
    status = case.cli("status", "--run", case.run)
    findings = [w["findings"][0]["finding_id"] for w in status["workers"].values()]
    case.cli(
        "decision",
        "--run",
        case.run,
        "--finding",
        findings[0],
        "--disposition",
        "accepted",
        "--reason",
        "Independent tap oracle required.",
        "--check-id",
        "tap",
    )
    case.cli(
        "decision",
        "--run",
        case.run,
        "--finding",
        findings[1],
        "--disposition",
        "duplicate",
        "--reason",
        "Same synthetic oracle.",
        "--duplicate-of",
        findings[0],
    )
    candidate = case.cli("candidate", "--run", case.run)
    proof = case.records / "proof.json"
    proof.write_text(
        json.dumps({"command": "synthetic assertion fixture", "assertions": 1, "exit": 0})
    )
    common = [
        "checkpoint",
        "--run",
        case.run,
        "--candidate",
        candidate["candidate_id"],
        "--stage",
        "verification",
        "--outcome",
        "passed",
        "--evidence",
        proof,
    ]
    case.cli(*common, "--assertions", "0", "--exit-code", "0", expected=2)
    case.cli(*common, "--assertions", "1", "--exit-code", "0", expected=2)
    case.cli(
        "check",
        "--run",
        case.run,
        "--candidate",
        candidate["candidate_id"],
        "--check-id",
        "tap",
        "--outcome",
        "passed",
        "--assertions",
        "1",
        "--exit-code",
        "0",
        "--evidence",
        proof,
    )
    case.cli(*common, "--assertions", "1", "--exit-code", "0")


def test_actual_usage_components_are_not_double_counted(case):
    case.initialize()
    evidence = case.records / "timing.json"
    evidence.write_text("Actual coordinator timing observation.")
    usage = case.records / "usage.json"
    usage.write_text(
        json.dumps(
            {
                "input_tokens": 100,
                "cached_input_tokens": 40,
                "output_tokens": 30,
                "reasoning_output_tokens": 20,
                "total_tokens": 130,
                "source": "scoped-host-event-1",
            }
        )
    )
    args = [
        "measure",
        "--run",
        case.run,
        "--stage",
        "exploration",
        "--worker-id",
        "host/a",
        "--seconds",
        "1",
        "--tool-calls",
        "2",
        "--evidence",
        evidence,
        "--usage-file",
        usage,
    ]
    case.cli(*args)
    status = case.cli("status", "--run", case.run)
    assert status["metrics"]["usage"]["total_tokens"] == 130
    case.cli(*args, expected=2)


def test_preexisting_change_requires_explicit_unchanged_exclusion(case):
    case.write("docs/unrelated.md", "Existing unrelated draft.\n")
    case.task["baseline_exclusions"] = [
        {"path": "docs/unrelated.md", "reason": "Preserve existing unrelated draft."}
    ]
    case.task_file.write_text(json.dumps(case.task))
    case.complete_advice()
    case.cli("candidate", "--run", case.run)
    case.write("docs/unrelated.md", "Changed after baseline.\n")
    case.cli("candidate", "--run", case.run, expected=2)


def test_later_failed_check_blocks_both_verification_and_review(case):
    case.complete_advice()
    status = case.cli("status", "--run", case.run)
    for worker in status["workers"].values():
        for finding in worker["findings"]:
            case.cli(
                "decision",
                "--run",
                case.run,
                "--finding",
                finding["finding_id"],
                "--disposition",
                "accepted",
                "--reason",
                "Independent oracle required.",
                "--check-id",
                "oracle",
            )
    candidate = case.cli("candidate", "--run", case.run)
    proof = case.records / "proof.json"
    proof.write_text("Synthetic executed oracle receipt.")
    args = [
        "check",
        "--run",
        case.run,
        "--candidate",
        candidate["candidate_id"],
        "--check-id",
        "oracle",
        "--assertions",
        "1",
        "--evidence",
        proof,
    ]
    case.cli(*args, "--outcome", "passed", "--exit-code", "0")
    case.cli(
        "checkpoint",
        "--run",
        case.run,
        "--candidate",
        candidate["candidate_id"],
        "--stage",
        "verification",
        "--outcome",
        "passed",
        "--assertions",
        "1",
        "--exit-code",
        "0",
        "--evidence",
        proof,
    )
    case.cli(*args, "--outcome", "failed", "--exit-code", "1")
    for stage in ("verification", "review"):
        refused = case.cli(
            "checkpoint",
            "--run",
            case.run,
            "--candidate",
            candidate["candidate_id"],
            "--stage",
            stage,
            "--outcome",
            "passed",
            "--assertions",
            "1",
            "--exit-code",
            "0",
            "--worker-id",
            "host/fresh",
            "--evidence",
            proof,
            expected=2,
        )
        assert "Recorded failed check blocks qualification" in refused["error"]


def test_index_only_content_or_mode_change_invalidates_candidate(case):
    case.complete_advice()
    case.write("src/ui.swift", "Qualified bytes.\n")
    subprocess.run(["git", "-C", str(case.repo), "add", "src/ui.swift"], check=True)
    candidate = case.cli("candidate", "--run", case.run)
    case.write("src/ui.swift", "Different staged bytes.\n")
    subprocess.run(["git", "-C", str(case.repo), "add", "src/ui.swift"], check=True)
    case.write("src/ui.swift", "Qualified bytes.\n")
    proof = case.records / "proof.json"
    proof.write_text("Synthetic executed oracle receipt.")
    case.cli(
        "check",
        "--run",
        case.run,
        "--candidate",
        candidate["candidate_id"],
        "--check-id",
        "oracle",
        "--outcome",
        "passed",
        "--assertions",
        "1",
        "--exit-code",
        "0",
        "--evidence",
        proof,
        expected=2,
    )
    case.cli(
        "checkpoint",
        "--run",
        case.run,
        "--candidate",
        candidate["candidate_id"],
        "--stage",
        "verification",
        "--outcome",
        "passed",
        "--assertions",
        "1",
        "--exit-code",
        "0",
        "--evidence",
        proof,
        expected=2,
    )
    recaptured = case.cli("candidate", "--run", case.run)
    assert recaptured["candidate_id"] != candidate["candidate_id"]
    case.cli(
        "check",
        "--run",
        case.run,
        "--candidate",
        recaptured["candidate_id"],
        "--check-id",
        "oracle",
        "--outcome",
        "passed",
        "--assertions",
        "1",
        "--exit-code",
        "0",
        "--evidence",
        proof,
        expected=2,
    )


def test_new_applicable_instruction_needs_predispatch_binding(case):
    case.write("docs/new/AGENTS.md", "New module instructions.\n")
    case.write("docs/new/source.py", "VALUE = 1\n")
    case.initialize()
    case.cli("start", "--run", case.run, "--agent", "ui", "--worker-id", "host/ui")
    result = case.result_file()
    payload = json.loads(result.read_text())
    payload["inspected"].append("docs/new/source.py")
    result.write_text(json.dumps(payload))
    case.cli(
        "result",
        "--run",
        case.run,
        "--agent",
        "ui",
        "--worker-id",
        "host/ui",
        "--result",
        result,
        expected=2,
    )


def test_candidate_only_path_cannot_skip_its_module_instructions(case):
    case.write("docs/new/AGENTS.md", "New module instructions.\n")
    case.write("docs/new/source.py", "VALUE = 1\n")
    case.complete_advice()
    case.cli(
        "candidate",
        "--run",
        case.run,
        "--path",
        "docs/new/source.py",
        "--path",
        "docs/new/AGENTS.md",
        expected=2,
    )


def test_index_mode_alone_invalidates_candidate(case):
    case.complete_advice()
    case.write("src/ui.swift", "Qualified bytes.\n")
    subprocess.run(["git", "-C", str(case.repo), "add", "src/ui.swift"], check=True)
    candidate = case.cli("candidate", "--run", case.run)
    subprocess.run(
        ["git", "-C", str(case.repo), "update-index", "--chmod=+x", "src/ui.swift"], check=True
    )
    proof = case.records / "proof.json"
    proof.write_text("Synthetic assertion receipt.")
    case.cli(
        "checkpoint",
        "--run",
        case.run,
        "--candidate",
        candidate["candidate_id"],
        "--stage",
        "verification",
        "--outcome",
        "passed",
        "--assertions",
        "1",
        "--exit-code",
        "0",
        "--evidence",
        proof,
        expected=2,
    )


def test_bound_nested_instruction_stays_freshness_dependency(case):
    case.write("docs/new/AGENTS.md", "Required module instruction.\n")
    case.write("docs/new/source.py", "VALUE = 1\n")
    case.task["required_sources"] = ["docs/new/source.py"]
    case.task_file.write_text(json.dumps(case.task))
    case.complete_advice()
    case.write("docs/new/AGENTS.md", "Changed module instruction.\n")
    status = case.cli("status", "--run", case.run)
    assert "instruction_sha256" in status["drift_fields"]
    case.cli("candidate", "--run", case.run, expected=2)


def test_executable_repair_changes_content_identity_but_mode_roundtrip_keeps_failure(case):
    case.complete_advice()
    case.write("scripts/fixture.py", 'print("synthetic")\n')
    path = case.repo / "scripts/fixture.py"
    path.chmod(0o644)
    subprocess.run(["git", "-C", str(case.repo), "add", "scripts/fixture.py"], check=True)
    original = case.cli("candidate", "--run", case.run, "--path", "scripts/fixture.py")
    proof = case.records / "mode-check.json"
    proof.write_text("Synthetic executable-mode assertion receipt.")

    def check(candidate, outcome, expected=0):
        return case.cli(
            "check",
            "--run",
            case.run,
            "--candidate",
            candidate["candidate_id"],
            "--check-id",
            "executable",
            "--outcome",
            outcome,
            "--assertions",
            "1",
            "--exit-code",
            "0" if outcome == "passed" else "1",
            "--evidence",
            proof,
            expected=expected,
        )

    check(original, "failed")
    path.chmod(0o755)
    subprocess.run(["git", "-C", str(case.repo), "add", "scripts/fixture.py"], check=True)
    repaired = case.cli("candidate", "--run", case.run, "--path", "scripts/fixture.py")
    assert repaired["content_id"] != original["content_id"]
    check(repaired, "passed")
    path.chmod(0o644)
    subprocess.run(["git", "-C", str(case.repo), "add", "scripts/fixture.py"], check=True)
    returned = case.cli("candidate", "--run", case.run, "--path", "scripts/fixture.py")
    assert returned["content_id"] == original["content_id"]
    check(returned, "passed", expected=2)
