"""Synthetic public contracts for knowledge loading and worker allocation."""

from __future__ import annotations

import copy
import importlib.util
import json
import os
import shutil
import unittest
from pathlib import Path
from unittest.mock import patch

_BASE_SPEC = importlib.util.spec_from_file_location(
    "composition_baseline", Path(__file__).with_name("test_agent_factory.py")
)
assert _BASE_SPEC is not None and _BASE_SPEC.loader is not None
baseline = importlib.util.module_from_spec(_BASE_SPEC)
_BASE_SPEC.loader.exec_module(baseline)
factory = baseline.factory
PACKS = (
    "exact-arithmetic",
    "query-provenance",
    "statement-ingestion",
    "publication-recovery",
)


class CompositionTests(unittest.TestCase):
    def setUp(self):
        self.case = baseline.FactoryTests("runTest")
        self.addCleanup(self.case.doCleanups)
        self.case.setUp()
        self.repo, self.records = self.case.repo, self.case.records
        self.catalog = {
            "schema_version": 2,
            "factory_id": "fixture",
            "max_advisors": 3,
            "routing": {
                "risk_aliases": {},
                "weak_terms": [],
                "whole_hyphen_tokens": False,
                "infer_text_risks": False,
            },
            "roles": [],
            "knowledge": [],
            "profiles": [],
        }
        for role in ("coordinator", "implementer", "adviser", "reviewer"):
            path = f"docs/roles/{role}.md"
            self.case.write(path, f"Synthetic {role} responsibility; no owner data.\n")
            self.catalog["roles"].append({"id": role, "version": "1.0.0", "content": path})
        for name in PACKS:
            content = f"docs/knowledge/{name}.md"
            source = f"docs/knowledge/{name}-source.md"
            profile = f"docs/profiles/{name}.md"
            self.case.write(content, f"Synthetic {name} invariant.\n")
            self.case.write(source, f"Independent synthetic {name} evidence.\n")
            self.case.write(profile, f"Use canonical adviser composition {name}.\n")
            self.catalog["knowledge"].append(
                {
                    "id": name,
                    "version": "1.0.0",
                    "description": f"Synthetic {name} boundary.",
                    "content": content,
                    "terms": [name],
                    "paths": ["src"],
                    "risks": [name],
                    "sources": [source],
                    "retrieval_triggers": [f"Changes to the synthetic {name} boundary."],
                }
            )
            self.catalog["profiles"].append(
                {
                    "id": name,
                    "version": "2.0.0",
                    "profile": profile,
                    "permissions": "advisory-read-only",
                    "role": "adviser",
                    "knowledge_packs": [name],
                    "question_template": f"Which synthetic {name} boundary needs an oracle?",
                }
            )
        self.case.write_json("docs/catalog.json", self.catalog)
        self.case.task.update(
            schema_version=2,
            title="Preserve all four synthetic boundaries",
            description="One implementation owner and actual fresh independent review.",
            risks=list(PACKS),
            knowledge_packs=list(PACKS),
        )
        self.allocate(0)

    def test_failed_base_setup_restores_parent_hook_environment(self):
        original = baseline.FactoryTests.setUp

        def setup_then_fail(case):
            original(case)
            raise RuntimeError("Synthetic base setup failure")

        class FailedSetup(CompositionTests):
            def test_body(self):
                raise AssertionError("A failed setup must not execute the test body")

        inherited = str(self.repo / ".git")
        with patch.dict(os.environ, {"GIT_DIR": inherited}):
            case = FailedSetup("test_body")
            try:
                with patch.object(baseline.FactoryTests, "setUp", setup_then_fail):
                    result = unittest.TestResult()
                    case.run(result)
                self.assertEqual(len(result.errors), 1)
                self.assertIn("Synthetic base setup failure", result.errors[0][1])
                self.assertEqual(os.environ.get("GIT_DIR"), inherited)
            finally:
                case.case.doCleanups()

    def allocate(self, count):
        self.case.task["adviser_allocation"] = {
            "selected": [
                {"profile_id": name, "question": f"Does the task preserve {name} exactly?"}
                for name in PACKS[:count]
            ],
            "omitted": [
                {
                    "profile_id": name,
                    "reason": "Coordinator owns source inspection and a final candidate check.",
                    "coverage_owner": "coordinator",
                }
                for name in PACKS[count:]
            ],
        }
        self.case.task_file.write_text(json.dumps(self.case.task))

    def context(self, role="implementer", *packs):
        return self.case.cli(
            "context",
            "--task",
            self.case.task_file,
            "--role",
            role,
            *(arg for pack in packs for arg in ("--pack", pack)),
        )

    def state(self):
        return json.loads((self.case.run / "run.json").read_text())

    def evidence(self, name="checks.json"):
        path = self.records / name
        path.write_text(
            json.dumps(
                {
                    "scope": "modeled unittest receipt, not actual host execution",
                    "source": "src/ui.swift:1",
                    "oracle": "Independently specified synthetic boundary holds.",
                    "assertions": 4,
                    "exit_code": 0,
                }
            )
        )
        return path

    def own_risks(self):
        for risk in self.state()["epochs"][-1]["risk_dispositions"]:
            self.case.cli(
                "risk",
                "--run",
                self.case.run,
                "--risk",
                risk,
                "--disposition",
                "coordinator",
                "--reason",
                "Coordinator has inspected synthetic source; final check remains required.",
                "--check-id",
                "risk-suite",
                "--evidence",
                self.evidence(),
            )

    def checkpoint(self, candidate, stage, *, worker=None, expected=0, evidence=None):
        return self.case.cli(
            "checkpoint",
            "--run",
            self.case.run,
            "--candidate",
            candidate["candidate_id"],
            "--stage",
            stage,
            "--outcome",
            "passed",
            "--assertions",
            "4",
            "--exit-code",
            "0",
            *(["--worker-id", worker] if worker else []),
            "--evidence",
            evidence or self.evidence(),
            expected=expected,
        )

    def check(self, candidate, *, evidence=None, expected=0):
        return self.case.cli(
            "check",
            "--run",
            self.case.run,
            "--candidate",
            candidate["candidate_id"],
            "--check-id",
            "risk-suite",
            "--outcome",
            "passed",
            "--assertions",
            "4",
            "--exit-code",
            "0",
            "--evidence",
            evidence or self.evidence(),
            expected=expected,
        )

    def test_same_knowledge_different_responsibility_without_worker_receipts(self):
        before = {
            p.relative_to(self.records): p.read_bytes()
            for p in self.records.rglob("*")
            if p.is_file()
        }
        author = self.context("implementer", "exact-arithmetic")
        reviewer = self.context("reviewer", "exact-arithmetic")
        self.assertEqual(author["kind"], "context-only")
        self.assertEqual(author["knowledge"], reviewer["knowledge"])
        self.assertNotEqual(author["role"], reviewer["role"])
        self.assertEqual(author, self.context("implementer", "exact-arithmetic"))
        self.assertNotIn("dispatch", author)
        self.assertNotIn("workers", author)
        after = {
            p.relative_to(self.records): p.read_bytes()
            for p in self.records.rglob("*")
            if p.is_file()
        }
        self.assertEqual(before, after)

    def test_allocation_changes_workers_without_reducing_knowledge_sources_or_risks(self):
        signatures = []
        for count in (0, 1, 3):
            with self.subTest(advisers=count):
                self.allocate(count)
                context = self.context()
                run = self.records / f"run-{count}"
                self.case.cli("init", "--task", self.case.task_file, "--run", run)
                state = json.loads((run / "run.json").read_text())
                self.assertEqual(state["schema_version"], 4)
                epoch = state["epochs"][-1]
                self.assertEqual(set(epoch["workers"]), set(PACKS[:count]))
                signatures.append(
                    (
                        context["knowledge"],
                        epoch["binding"]["source_sha256"],
                        epoch["selection"]["required_risks"],
                    )
                )
        self.assertEqual(signatures[0], signatures[1])
        self.assertEqual(signatures[0], signatures[2])
        self.assertEqual({p["id"] for p in signatures[0][0]}, set(PACKS))

    def test_route_separates_pending_allocation_omissions_and_unknown_ownership(self):
        self.case.task["paths"].append("docs/unmapped.md")
        self.case.write("docs/unmapped.md", "Synthetic uncovered developer source.\n")
        self.case.task["adviser_allocation"] = None
        self.case.task_file.write_text(json.dumps(self.case.task))
        pending = self.case.cli("route", "--task", self.case.task_file)
        self.assertFalse(pending["abstained"])
        self.assertEqual(pending["allocation_kind"], "unallocated-draft")
        self.assertEqual(pending["unowned_paths"], ["docs/unmapped.md"])
        self.assertTrue(pending["recommended"])
        self.assertEqual(
            {row["reason"] for row in pending["uncovered_risks"] if row["risk"] in PACKS},
            {"allocation_pending"},
        )
        self.allocate(0)
        chosen = self.case.cli("route", "--task", self.case.task_file)
        self.assertTrue(chosen["abstained"])
        self.assertEqual(chosen["excluded"], [])
        self.assertEqual({row["id"] for row in chosen["omitted"]}, set(PACKS))
        self.assertEqual({row["coverage_owner"] for row in chosen["omitted"]}, {"coordinator"})
        self.assertTrue(all(row["reason"] for row in chosen["omitted"]))
        self.assertEqual(
            {row["reason"] for row in chosen["uncovered_risks"] if row["risk"] in PACKS},
            {"allocation_omission"},
        )
        self.assertIn("coordinator allocation", chosen["method"])

    def test_unknown_role_pack_and_unallocated_init_fail_without_creating_a_run(self):
        self.case.cli("context", "--task", self.case.task_file, "--role", "intruder", expected=2)
        self.case.cli(
            "context",
            "--task",
            self.case.task_file,
            "--role",
            "implementer",
            "--pack",
            "unknown",
            expected=2,
        )
        self.case.task["adviser_allocation"] = None
        self.case.task_file.write_text(json.dumps(self.case.task))
        self.context()
        self.case.cli("init", "--task", self.case.task_file, "--run", self.case.run, expected=2)
        self.assertFalse(self.case.run.exists())

    def test_invalid_allocations_refuse_before_worker_records_exist(self):
        original = copy.deepcopy(self.case.task)
        cases = []
        unknown = copy.deepcopy(original)
        unknown["adviser_allocation"]["omitted"][0]["profile_id"] = "intruder"
        cases.append(unknown)
        missing = copy.deepcopy(original)
        missing["adviser_allocation"]["omitted"].pop()
        cases.append(missing)
        self.allocate(4)
        cases.append(copy.deepcopy(self.case.task))
        duplicate = copy.deepcopy(original)
        duplicate["adviser_allocation"]["omitted"].append(
            copy.deepcopy(duplicate["adviser_allocation"]["omitted"][0])
        )
        cases.append(duplicate)
        self.allocate(2)
        repeated_question = copy.deepcopy(self.case.task)
        repeated_question["adviser_allocation"]["selected"][1]["question"] = repeated_question[
            "adviser_allocation"
        ]["selected"][0]["question"]
        cases.append(repeated_question)
        for number, task in enumerate(cases):
            with self.subTest(number=number):
                self.case.task_file.write_text(json.dumps(task))
                run = self.records / f"invalid-{number}"
                self.case.cli("init", "--task", self.case.task_file, "--run", run, expected=2)
                self.assertFalse(run.exists())

    def test_allocation_edit_cannot_remove_pending_or_failed_workers_or_reset_retry(self):
        self.allocate(1)
        self.case.initialize()
        original = (self.case.run / "run.json").read_bytes()
        self.allocate(0)
        self.case.cli(
            "rebind", "--run", self.case.run, "--reason", "Remove pending lane", expected=2
        )
        self.assertEqual((self.case.run / "run.json").read_bytes(), original)
        self.allocate(1)
        self.case.cli(
            "unavailable",
            "--run",
            self.case.run,
            "--agent",
            PACKS[0],
            "--reason",
            "Modeled host unavailable",
        )
        self.case.cli("rebind", "--run", self.case.run, "--reason", "Only retry")
        self.case.cli(
            "unavailable",
            "--run",
            self.case.run,
            "--agent",
            PACKS[0],
            "--reason",
            "Still unavailable",
        )
        exhausted = (self.case.run / "run.json").read_bytes()
        self.allocate(0)
        self.case.cli(
            "rebind", "--run", self.case.run, "--reason", "Remove failed lane", expected=2
        )
        self.allocate(1)
        self.case.cli(
            "rebind", "--run", self.case.run, "--reason", "Allocation roundtrip", expected=2
        )
        self.assertEqual((self.case.run / "run.json").read_bytes(), exhausted)

    def test_zero_advisers_needs_real_check_receipts_and_a_distinct_review(self):
        self.case.initialize()
        self.case.cli("candidate", "--run", self.case.run, expected=2)
        self.own_risks()
        candidate = self.case.cli("candidate", "--run", self.case.run)
        self.checkpoint(candidate, "implementation", worker="synthetic-author")
        self.checkpoint(candidate, "verification", expected=2)
        context_file = self.records / "context.json"
        context_file.write_text(json.dumps(self.context("reviewer")))
        self.check(candidate, evidence=context_file, expected=2)
        self.checkpoint(
            candidate, "review", worker="synthetic-reviewer", evidence=context_file, expected=2
        )
        self.checkpoint(candidate, "delivery", expected=2)
        self.check(candidate)
        self.checkpoint(candidate, "verification")
        self.checkpoint(candidate, "delivery", expected=2)
        self.checkpoint(candidate, "review", worker="synthetic-author", expected=2)
        self.checkpoint(candidate, "review", worker="synthetic-reviewer")
        self.checkpoint(candidate, "delivery")

    def test_role_pack_and_nested_instruction_drift_blocks_stale_start(self):
        self.case.write("docs/roles/AGENTS.md", "Synthetic nested role instructions.\n")
        self.allocate(1)
        self.case.initialize()
        packet = self.case.cli("packet", "--run", self.case.run, "--agent", PACKS[0])
        self.assertEqual(packet["assignment_context"]["role"]["id"], "adviser")
        # Scope must be explicit before the loader's external-file instruction;
        # a fresh fork still inherits the host's prior-memory lookup guidance.
        self.assertLess(
            packet["launch_prompt"].index("Do not consult retained Memory"),
            packet["launch_prompt"].index(str(self.case.run / packet["assignment"]["path"])),
        )
        for path in (
            "docs/roles/coordinator.md",
            "docs/knowledge/publication-recovery.md",
            "docs/roles/AGENTS.md",
        ):
            with self.subTest(path=path):
                target = self.repo / path
                old = target.read_bytes()
                target.write_bytes(old + b"Changed boundary.\n")
                self.case.cli(
                    "start",
                    "--run",
                    self.case.run,
                    "--agent",
                    PACKS[0],
                    "--worker-id",
                    "synthetic-adviser",
                    "--launch-sha256",
                    packet["launch_sha256"],
                    expected=2,
                )
                target.write_bytes(old)

    def test_historical_schema3_is_inspectable_without_current_inputs_or_mutation(self):
        self.case.initialize()
        state = self.state()
        state["schema_version"] = 3
        state["checkpoints"] = [
            {
                "outcome": "failed",
                "candidate_id": "synthetic-original-candidate",
                "stage": "verification",
            }
        ]
        path = self.case.run / "run.json"
        path.write_text(json.dumps(state))
        original = path.read_bytes()
        self.case.task_file.unlink()
        (self.repo / "docs/catalog.json").unlink()
        (self.case.run / ".lock").unlink()
        status = self.case.cli("status", "--run", self.case.run)
        self.assertTrue(status["historical"])
        self.assertEqual(status["record"], state)
        read = factory.read_bytes
        reads = 0

        def changing_record(target, limit=factory.LIMIT):
            nonlocal reads
            if target == path:
                reads += 1
                return original if reads == 1 else original + b" "
            return read(target, limit)

        args = factory.parser().parse_args(
            [
                "--repo",
                str(self.repo),
                "--catalog",
                "docs/catalog.json",
                "--records-root",
                str(self.records),
                "status",
                "--run",
                str(self.case.run),
            ]
        )
        with patch.object(factory, "read_bytes", changing_record):
            observed = factory.execute(args)
        self.assertEqual(observed["record"], state)
        self.assertEqual(observed["record_sha256"], factory.digest(original))
        self.case.cli("rebind", "--run", self.case.run, "--reason", "Do not migrate", expected=2)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse((self.case.run / ".lock").exists())

    def test_real_ledger_packs_keep_the_same_sources_and_risks_for_zero_one_or_three_advisers(self):
        source = Path(__file__).resolve().parents[1]
        catalog = json.loads((source / factory.DEFAULT_CATALOG).read_text())
        paths = {
            "AGENTS.md",
            "docs/agent/factory/README.md",
            "docs/agent/skills/ledger-implement-ticket/SKILL.md",
        }
        paths.update(entry["profile"] for entry in catalog["profiles"])
        paths.update(entry["content"] for kind in ("roles", "knowledge") for entry in catalog[kind])
        paths.update(path for pack in catalog["knowledge"] for path in pack["sources"])
        for pack in catalog["knowledge"]:
            for name in pack["paths"]:
                if (source / name).is_dir():
                    (self.repo / name).mkdir(parents=True, exist_ok=True)
                else:
                    paths.add(name)
        for name in paths:
            target = self.repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source / name, target)
        self.case.write_json("docs/catalog.json", catalog)
        self.case.task.update(
            paths=["src/ledger/query/agent.py", "src/ledger/imports/pdf/workflow.py"],
            risks=sorted({risk for pack in catalog["knowledge"] for risk in pack["risks"]}),
        )
        signatures = []
        for count in (0, 1, 3):
            self.allocate(count)
            context = self.context()
            run = self.records / f"ledger-{count}"
            self.case.cli("init", "--task", self.case.task_file, "--run", run)
            epoch = json.loads((run / "run.json").read_text())["epochs"][-1]
            self.assertEqual(len(epoch["workers"]), count)
            signatures.append(
                (
                    context["knowledge"],
                    epoch["binding"]["source_sha256"],
                    epoch["selection"]["required_risks"],
                )
            )
        self.assertEqual(signatures[0], signatures[1])
        self.assertEqual(signatures[0], signatures[2])
        self.assertEqual({pack["id"] for pack in signatures[0][0]}, set(PACKS))

    def test_context_rejects_missing_or_unsafe_required_sources_and_role_paths(self):
        target = self.repo / self.catalog["knowledge"][0]["sources"][0]
        target.unlink()
        self.case.cli("context", "--task", self.case.task_file, "--role", "implementer", expected=2)
        target.write_text("Restored synthetic source.\n")
        for field, value in (("sources", ["data/owner.db"]), ("content", "../owner.md")):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.catalog)
                changed["knowledge"][0][field] = value
                self.case.write_json("docs/catalog.json", changed)
                self.case.cli(
                    "context", "--task", self.case.task_file, "--role", "implementer", expected=2
                )
        self.case.write_json("docs/catalog.json", self.catalog)
        role = self.repo / self.catalog["roles"][0]["content"]
        role.unlink()
        role.symlink_to(self.repo / "src/ui.swift")
        self.case.cli("context", "--task", self.case.task_file, "--role", "implementer", expected=2)

    def test_allocation_roundtrip_does_not_clear_failed_candidate_checks(self):
        self.case.initialize()
        self.own_risks()
        candidate = self.case.cli("candidate", "--run", self.case.run)
        self.case.cli(
            "check",
            "--run",
            self.case.run,
            "--candidate",
            candidate["candidate_id"],
            "--check-id",
            "risk-suite",
            "--outcome",
            "failed",
            "--assertions",
            "4",
            "--exit-code",
            "1",
            "--evidence",
            self.evidence(),
        )
        failed = (self.case.run / "run.json").read_bytes()
        self.allocate(1)
        self.case.cli(
            "rebind", "--run", self.case.run, "--reason", "Try another allocation", expected=2
        )
        self.allocate(0)
        restored = self.case.cli("candidate", "--run", self.case.run)
        self.assertEqual(restored["candidate_id"], candidate["candidate_id"])
        self.check(candidate, expected=2)
        self.assertEqual((self.case.run / "run.json").read_bytes(), failed)
        self.case.write("src/ui.swift", "struct ActuallyRepairedWidget {}\n")
        repaired = self.case.cli("candidate", "--run", self.case.run)
        self.assertNotEqual(repaired["content_id"], candidate["content_id"])
        self.check(repaired)
        self.assertEqual(self.state()["checks"][0]["outcome"], "failed")

    def test_context_is_not_an_adviser_result_and_measurement_keeps_actual_context_separate(self):
        self.allocate(1)
        self.case.initialize()
        packet = self.case.cli("packet", "--run", self.case.run, "--agent", PACKS[0])
        self.case.cli(
            "start",
            "--run",
            self.case.run,
            "--agent",
            PACKS[0],
            "--worker-id",
            "synthetic-actual-adviser",
            "--launch-sha256",
            packet["launch_sha256"],
        )
        context_path = self.records / "passive.json"
        context_path.write_text(json.dumps(self.context("adviser")))
        self.case.cli(
            "result",
            "--run",
            self.case.run,
            "--agent",
            PACKS[0],
            "--worker-id",
            "synthetic-actual-adviser",
            "--result",
            context_path,
            expected=2,
        )
        state_before = (self.case.run / "run.json").read_bytes()
        self.context("reviewer")
        self.assertEqual((self.case.run / "run.json").read_bytes(), state_before)
        known = self.case.cli(
            "measure",
            "--run",
            self.case.run,
            "--stage",
            "advice",
            "--worker-id",
            "synthetic-actual-adviser",
            "--seconds",
            "1",
            "--tool-calls",
            "1",
            "--evidence",
            self.evidence(),
            "--usage-unavailable",
            "Modeled fixture has no actual telemetry",
        )
        self.assertEqual(known["assignment_context"], packet["assignment_context"])
        unknown = self.case.cli(
            "measure",
            "--run",
            self.case.run,
            "--stage",
            "implementation",
            "--worker-id",
            "unmapped-author",
            "--seconds",
            "1",
            "--tool-calls",
            "1",
            "--evidence",
            self.evidence(),
            "--usage-unavailable",
            "Unmapped fixture identity",
        )
        self.assertIsNone(unknown["assignment_context"])
        self.assertEqual(
            len(self.case.cli("status", "--run", self.case.run)["metrics"]["attempts"]), 1
        )

    def test_unavailable_adviser_cannot_be_removed_or_reclassified_as_completed_delivery(self):
        self.allocate(1)
        self.case.initialize()
        self.case.cli(
            "unavailable",
            "--run",
            self.case.run,
            "--agent",
            PACKS[0],
            "--reason",
            "Modeled actual host unavailable",
        )
        self.own_risks()
        candidate = self.case.cli("candidate", "--run", self.case.run)
        self.checkpoint(candidate, "implementation", worker="synthetic-author")
        self.check(candidate)
        self.checkpoint(candidate, "verification")
        self.checkpoint(candidate, "review", worker="synthetic-reviewer")
        self.checkpoint(candidate, "delivery", expected=2)
        self.assertFalse(self.case.cli("status", "--run", self.case.run)["all_advice_completed"])
        self.allocate(0)
        self.case.cli(
            "rebind", "--run", self.case.run, "--reason", "Waive unavailable lane", expected=2
        )

    def test_excessive_composition_fails_before_partial_worker_records(self):
        self.allocate(1)
        dependencies = [f"docs/required-{number}.md" for number in range(64)]
        for path in dependencies:
            self.case.write(path, "Fresh synthetic required developer evidence.\n")
        self.case.task["required_sources"] = dependencies
        self.case.task_file.write_text(json.dumps(self.case.task))
        self.case.cli("context", "--task", self.case.task_file, "--role", "implementer", expected=2)
        self.case.cli("init", "--task", self.case.task_file, "--run", self.case.run, expected=2)
        self.assertFalse(self.case.run.exists())

    def test_retry_context_identity_includes_retained_supplemental_dependencies(self):
        self.allocate(1)
        self.case.initialize()
        first = self.case.cli("packet", "--run", self.case.run, "--agent", PACKS[0])
        self.case.cli(
            "start",
            "--run",
            self.case.run,
            "--agent",
            PACKS[0],
            "--worker-id",
            "synthetic-first",
            "--launch-sha256",
            first["launch_sha256"],
        )
        self.case.write("src/extra.swift", "Independent synthetic supplemental evidence.\n")
        inspected = sorted(set(first["required_inspection"]) | {"src/extra.swift"})
        result = {
            "dispatch": first["dispatch"],
            "status": "failed",
            "summary": "Modeled negative result with extra source.",
            "inspected": inspected,
            "findings": [],
            "checks": [],
            "open_questions": ["A modeled unresolved boundary."],
            "actions_not_performed": ["No actual host execution."],
            "source_coverage": {
                path: {"status": "inspected", "reason": "Modeled source inspection."}
                for path in first["required_inspection"]
            },
        }
        path = self.records / "failed-result.json"
        path.write_text(json.dumps(result))
        self.case.cli(
            "result",
            "--run",
            self.case.run,
            "--agent",
            PACKS[0],
            "--worker-id",
            "synthetic-first",
            "--result",
            path,
        )
        self.case.cli(
            "rebind", "--run", self.case.run, "--reason", "One explicit retry with retained source"
        )
        second = self.case.cli("packet", "--run", self.case.run, "--agent", PACKS[0])
        self.assertIn(
            "src/extra.swift",
            self.state()["epochs"][-1]["workers"][PACKS[0]]["dispatch_binding"]["source_sha256"],
        )
        self.assertNotEqual(
            first["assignment_context"]["context_sha256"],
            second["assignment_context"]["context_sha256"],
        )
        self.assertEqual(self.state()["epochs"][0]["workers"][PACKS[0]]["status"], "failed")


if __name__ == "__main__":
    unittest.main()
