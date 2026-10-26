# Ledger developer agent factory

The [implementation Skill](../skills/ledger-implement-ticket/SKILL.md) discovers source-grounded knowledge and records a deliberate adviser allocation. The [standard-library runner](../../../scripts/agent_factory.py) composes contexts and records actual host observations; it does not call a model, authorize financial operations or create a sandbox. One implementation owner may also coordinate and schedule synthetic checks. A separate fresh reviewer remains required for substantive changes.

## Responsibilities, knowledge and procedure

The canonical [catalog](catalog.json) revision 2 separates four reusable [responsibilities](roles/) from four [knowledge packs](knowledge/). Coordination, implementation, read-only advice and independent review define activities, outputs and authority limits; they do not require four separate agents. Packs hold domain interpretations, source pointers, failure modes and retrieval/routing metadata. The Skill owns the procedure. Stable adviser profile IDs compose the advisory responsibility, canonical pack and a distinct question. Their thin [profile files](profiles/) contain no parallel fact/routing copy. Revision-1 catalog behavior is retained only for compatibility.

An implementer and a fresh reviewer can load the same pack with different responsibilities. A separate adviser is useful when a bounded unresolved question warrants investigation. Delegate a writing slice only with agreed interfaces, ownership and independently checkable acceptance; one owner retains integration and complete behavior. A controller/service/repository writer split is not the default. Role restrictions use inherited host permissions; files are neither registered custom agents nor a sandbox. Knowledge never grants owner-data, financial or external-action authority.

## External records and source admission

From Ledger use `python3 scripts/agent_factory.py`; elsewhere supply global `--repo /absolute/Ledger`. Every command except `validate` requires global `--records-root /absolute/developer-records` before the subcommand. This existing physical directory must be external to and must not contain the checkout. All task, run, result, launch, check and measurement evidence stays inside it. Never choose owner data/runtime directories or a broad parent such as your home. Parent symlinks, hard-linked records, aliases into the checkout and unsafe sources are refused before content reads.

Developer paths are restricted to admitted `src/`, `tests/`, `scripts/`, `docs/`, `configs/`, `vendor/` and named root build/guidance files including `.gitignore`. Hidden/traversing/non-normalized paths, symlinks, hard links, runtime/export directories, financial artifacts and owner `data/` remain excluded. Admission is a filename boundary, not content classification or a host sandbox. Keep developer sources free of owner content. The recorder claims atomic replacement, not authenticated host execution or a power-loss-durable audit log.

## Author and route

Normal task schema 2 requires `schema_version`, `id`, `title`, `description`, `paths`, `risks`, `acceptance`, `constraints`, `knowledge_packs` and `adviser_allocation`. Paths are existing repository-relative files. Optional `planned_paths` retains future source absence/ownership gaps; `required_sources` adds existing developer dependencies. `baseline_exclusions` contains `{path, reason}` for unchanged pre-existing unrelated changes, never task sources; later changed bytes enter the candidate. Schema 1 retains its old automatic selection, `adviser_questions` and explicit `exclusions` only as a compatibility path; use schema 2 for maintained composed work.

Discovery is uncapped and independent of actual adviser count. It retains matched packs, required sources, path/explicit/text risks, ranking recommendations and unknown/omitted obligations. Requesting reference knowledge does not create a worker or invent every possible risk from that domain. A schema-2 planning draft uses explicit `adviser_allocation: null`; `route`, `context` and `author` may inspect/canonicalize that draft, but `init` refuses it.

Before initialization, the coordinator records `selected` entries `{profile_id, question}` for distinct task-specific unresolved questions, plus `omitted` entries `{profile_id, reason, coverage_owner: "coordinator"}`. Account for every suggested lane exactly once, without unknown/duplicate/over-cap IDs or duplicate questions. Preserve cap 3. A justified zero-adviser allocation is valid for a known-domain task; its discovered packs/sources/real risks remain unchanged. Omission reasons establish ownership, not passed checks. The initial allocation/questions/reasons/ownership are immutable within that run, including during source rebinds. Context loads or edits cannot remove established pending/failed/unavailable lanes, erase findings or reset retry/failed-candidate history. A separately scoped new run never rewrites old failures.

```json
{
  "schema_version": 2,
  "id": "synthetic-exact-query",
  "title": "Verify exact transaction totals",
  "description": "Preserve literal amounts, currency and evidence limits.",
  "paths": ["src/ledger/query/agent.py"],
  "risks": ["exact-arithmetic", "currency", "source-coverage"],
  "acceptance": ["Independent synthetic expected totals and coverage disclosures agree."],
  "constraints": ["No owner data, connected Ledger MCP, defaults or financial changes."],
  "knowledge_packs": ["exact-arithmetic"],
  "adviser_allocation": {
    "selected": [{"profile_id": "exact-arithmetic", "question": "Can caller precision 6 or Decimal traps change a represented cent value before fractional-cent rejection? Identify an independent integer-ratio oracle."}],
    "omitted": [{"profile_id": "query-provenance", "reason": "The coordinator owns the public source/coverage trace and a final disposable read-only check.", "coverage_owner": "coordinator"}]
  }
}
```

```bash
python3 scripts/agent_factory.py validate
python3 scripts/agent_factory.py --records-root /absolute/records author --task /absolute/records/draft.json --output /absolute/records/task.json
python3 scripts/agent_factory.py --records-root /absolute/records route --task /absolute/records/task.json
python3 scripts/agent_factory.py --records-root /absolute/records init --task /absolute/records/task.json --run /absolute/records/run
```

`author` writes a new canonical task and never overwrites an active input. Known Ledger risk aliases, including `currency-boundaries`, normalize to canonical labels. Title, description and acceptance supply topic signals, including canonical risk names and aliases that become required obligations; constraints do not supply positive routing signals. Bounded English negation and Chinese domain phrases are supported deliberately. Generic words such as “unavailable” alone do not spawn advisers. This is heuristic routing, not complete semantic detection: the coordinator still reads requirements, dependencies, omitted profiles and uncovered risks.

Recommendations first cover marginal required risks, then use path/risk/whole-term relevance with stable ties. A matched source path creates a primary-domain obligation. Investment query arithmetic and PDF workflow ingestion have explicit catalog mappings. All matched knowledge/source dependencies remain available beyond the three-adviser recommendation cap. Actual schema-2 worker creation follows the bound coordinator allocation, not pack matching. Unknown, omitted or capped obligations require evidenced coordinator coverage or a genuine blocker; no silent cap increase or fabricated zero-match route is permitted.

## Load context without dispatch

```bash
python3 scripts/agent_factory.py --records-root /absolute/records context --task /absolute/records/task.json --role implementer --routed
python3 scripts/agent_factory.py --records-root /absolute/records context --task /absolute/records/task.json --role reviewer --routed
# Explicit bounded selection; task-declared packs remain dependencies.
python3 scripts/agent_factory.py --records-root /absolute/records context --task /absolute/records/task.json --role implementer --pack exact-arithmetic
```

For the monetary example above, implementer and reviewer receive the same canonical `exact-arithmetic` knowledge and current sources with different role contracts. `--routed` additionally loads other discovered relevant packs, such as query provenance, without another worker. The allocated adviser receives that same pack for the distinct precision/trap question; the coordinator retains the omitted provenance check and actual fresh review. Zero, one or several advisers changes worker obligations rather than source discovery.

The deterministic `context-only` output includes role/pack content and metadata hashes, task/question/acceptance limits, required inspection/source identities, applicable instructions/Skill references, full discovery and unloaded packs. It creates no run, lock, evidence, worker, contribution or review result. Context is limited to 64 required inspections and 512 KiB; narrow an excessive task rather than silently truncate it. Unknown IDs, unsafe paths and missing required sources fail. Context output cannot be ingested as an adviser result or used as check/risk/review execution evidence. Required inspection identity is not authenticated proof of reading.

## Required dependencies and immutable launches

Schema-4 runs bind root/applicable nested `AGENTS.md`, canonical adapter Skill/factory guide, all role/pack/profile content and catalog metadata, complete discovered starting sources, task allocation/questions, dependencies, planned absence, factory bytes and HEAD before dispatch. Instruction changes require rebind even during candidate qualification. A supplemental or candidate-only source with previously unbound applicable instructions must be declared as a dependency and rebound before advice/capture can be accepted. All role/pack/profile hashes remain conservative dependencies; no incremental cache relaxes freshness. Supplemental inspected files are separately captured at result ingestion and remain freshness dependencies through task round trips. Their receipt-time hashes do not establish read-time bytes and never rewrite the immutable dispatch binding.

```bash
python3 scripts/agent_factory.py --records-root /absolute/records packet --run /absolute/records/run --agent SELECTED_ID
# Submit exactly the returned launch_prompt with fork_turns:none and inherited settings.
# Save the actual host-returned identity; prepared text alone is not execution.
python3 scripts/agent_factory.py --records-root /absolute/records start --run /absolute/records/run --agent SELECTED_ID --worker-id ACTUAL_HOST_ID --launch-sha256 RETURNED_LAUNCH_HASH
python3 scripts/agent_factory.py --records-root /absolute/records result --run /absolute/records/run --agent SELECTED_ID --worker-id ACTUAL_HOST_ID --result /absolute/records/result.json
```

On this Codex host use `collaboration.spawn_agent` and inspect its live schema. `packet` retains the full assignment as hashed evidence and returns a concise exact `launch_prompt` loader plus `launch_sha256`. The start receipt references that prepared launch and assignment; it never substitutes a regenerated prompt. If another result expands observed dependencies between preparation and start, prepare and actually submit a fresh launch. If host spawn succeeds but start recording fails, interrupt that host worker, preserve identity/failure externally and reconcile before redispatch. Do not fabricate a running state. These are coordinator-recorded submission observations, not authenticated tool-prompt attestations or native custom-agent registration.

Advisers may inspect developer source only. Adviser and fresh reviewer assignments are self-contained: no retained Memory, prior-task notes, other chats or unrelated files, including before the full assignment is read. The exact adviser loader states this boundary before naming the assignment file. A fresh fork excludes prior chat turns; it does not remove inherited host instructions or enforce isolation. Advisers cannot edit source, run tests/builds, mutate Git, spawn children, send external messages or access owner data/defaults/connected Ledger MCP, including status. One writer and test scheduler remains the coordinator; read-only instructions operate under inherited host permissions.

## Results, findings and checks

A result has exactly `dispatch`, `status`, `summary`, `inspected`, `findings`, `checks`, `open_questions`, `actions_not_performed`, `source_coverage`. Echo dispatch unchanged. Status is completed or failed. Every `required_inspection` path must have a coverage entry `{status: inspected|unavailable, reason}`; completed requires inspected coverage. Declared inspection is an observation, not authenticated proof of reading.

Every finding has `id`, `severity` (critical/high/medium/low/info), `evidence`, `recommendation`, `locations` (`[{path,start,end}]`), `claim` (scope/limits), and `oracle` (independent expected result/check). Locations must be admitted, inspected and in range. `checks`, `open_questions` and `actions_not_performed` remain string lists; suggested checks are never passing results. The recorder assigns a dispatch-bound `finding_id`.

```bash
python3 scripts/agent_factory.py --records-root /absolute/records decision --run /absolute/records/run --finding FINDING_ID --disposition accepted --reason "Independent literal oracle required." --check-id exact-total
# rejected requires a reason; duplicate requires --duplicate-of a previously decided nonduplicate finding.
python3 scripts/agent_factory.py --records-root /absolute/records candidate --run /absolute/records/run --path scripts/changed.py
python3 scripts/agent_factory.py --records-root /absolute/records check --run /absolute/records/run --candidate CANDIDATE_ID --check-id exact-total --outcome passed --assertions 3 --exit-code 0 --evidence /absolute/records/check.json
```

Candidate capture reconciles every current staged/unstaged/new/deleted developer change against declared/bound files and explicit unchanged baseline exclusions. It binds working-tree bytes plus index blob/mode and status; staged content must match qualified working-tree bytes/mode before a check or qualification can pass. Semantic content identity includes normalized working-tree executable mode, so a genuine permission repair can qualify while a mode roundtrip retains the original failure. Index/status/HEAD bookkeeping remains excluded from that semantic identity. It enumerates filenames before admission and never reads forbidden changed artifacts. Every changed file must be included; use `--delete old/path` and `--path new/path` for deletions/renames. Flags record observations and do not mutate source. Later wholly undeclared or index-only changes invalidate qualification too. Exact tracked deletion identity may come from the current index, HEAD or the recorded baseline index. Existing semantic failed-check identities and retained candidate declarations cannot be cleared by omission, restoration, task formatting or HEAD bookkeeping.

All findings require accepted/rejected/duplicate dispositions before passing verification/review. Accepted findings require passing checks on that exact candidate. Any retained failed check for the same semantic candidate content blocks qualification, including a failure recorded after an earlier pass. Passing checks and qualification require positive executed assertion counts and exit zero. Record commands, assertions, actual outcomes and limits in external evidence; the recorder does not execute or certify that evidence itself. Preserve failed/unavailable attempts. Failed behavior cannot pass on unchanged candidate content.

```bash
python3 scripts/agent_factory.py --records-root /absolute/records checkpoint --run /absolute/records/run --candidate CANDIDATE_ID --stage implementation --outcome passed --worker-id ACTUAL_AUTHOR_ID --evidence /absolute/records/implementation.json
python3 scripts/agent_factory.py --records-root /absolute/records checkpoint --run /absolute/records/run --candidate CANDIDATE_ID --stage verification --outcome passed --assertions 3 --exit-code 0 --evidence /absolute/records/check.json
python3 scripts/agent_factory.py --records-root /absolute/records checkpoint --run /absolute/records/run --candidate CANDIDATE_ID --stage review --outcome passed --worker-id ACTUAL_FRESH_REVIEWER_ID --assertions 1 --exit-code 0 --evidence /absolute/records/review.json
```

The reviewer receives requirements, acceptance, final diff/source identity and failure modes, excluding author rationale and adviser conclusions. It must differ from every recorded adviser and implementer, regardless of receipt order. Normal composed work records the actual implementation author before passing review; final delivery additionally requires current-candidate implementation, verification, distinct independent review and completion of every allocated adviser. Failed/unavailable advice is not completion. Context-only files cannot supply execution evidence. Commits change HEAD; retain prior evidence and record post-integration identity/parity rather than relabeling old qualification.

## Failure, resume and measurement

Use `status` to inspect actual statuses, dispositions, candidates/checks and metrics. `unavailable` is only for an unstarted worker with an actual reason. `interrupt` requires the recorded running identity and can close a real interrupted worker after inputs disappear. `rebind --reason ...` retains history, refuses live workers, permits one retry per semantic input across all history and caps the run at 20 epochs. HEAD-only/task-format changes do not reset budgets. Completed advice carries forward only for exact unchanged binding, with its original identity. Failed/unavailable advice is not completion.

Uncovered risks require `risk --risk ID --disposition coordinator|not-applicable|blocked --reason ... --evidence ...`. In normal schema-2 work, coordinator/not-applicable also requires `--check-id ID`. Record source inspection, ownership and the named pending check while source is fresh, before product edits. Blocked/unresolved risks prevent candidate capture. Ownership or a planned check is not qualification: execute and record the named check against the exact final candidate before passing verification/review/delivery. A corrected candidate needs its own passing check; retained failures cannot be waived by allocation changes. No disposition authorizes financial changes or waives project gates.

```bash
python3 scripts/agent_factory.py --records-root /absolute/records measure --run /absolute/records/run --stage exploration --worker-id ACTUAL_HOST_ID --seconds 12.5 --tool-calls 4 --evidence /absolute/records/timing.json --usage-unavailable "No scoped host usage measurement."
# Or --usage-file /absolute/records/usage.json, never both.
```

Actual usage JSON has nonnegative integer `input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_output_tokens`, `total_tokens` plus a unique `source` identifier. Cached tokens are part of input, reasoning is part of output, total is input+output. Retain source evidence; duplicate cumulative source receipts are refused. Record disjoint attributable windows; the recorder cannot infer overlap from different source labels. Metrics distinguish allocated attempts/statuses, actual recorded adviser IDs/peak and bound assignment role/pack/context identities. Passive context loads add no attempts or contributions. Measurements link already recorded actual adviser assignments; unmapped author/coordinator/reviewer context remains unavailable rather than inferred from a stage name. Partial usage and unattributable overhead remain explicit; cost stays unavailable. Measurement never changes retry/failure identity. Host-wide tool enforcement is distinct from a logged command-wrapper ceiling.

Earlier schema-1/2/3 runs support only explicitly scoped historical status, without current catalog/task/source reads or locks. Status exposes the original stored record and its byte hash as historical evidence, including identities and outcomes; it does not invent role/pack fields, recompute current qualification or migrate anything. Begin a fresh schema-4 run. Per-profile incremental reuse remains deferred until measured benefit and a safe complete dependency manifest justify it; conservative source/role/pack/profile/instruction binding remains in force.

A focused source or host check establishes only its declared behavior. It is not a team-size benchmark and establishes no measured productivity or token-cost improvement.

## Verify and deliver

Run focused factory/contract/public-path tests and catalog validation, then the project's [development](../development.md) and [delivery](../delivery.md) gates. Use only freshly authored synthetic fixtures and explicit disposable paths. `deliver` runs synthetic source/UI gates, no-dependency editable reinstall, normal operator Skill parity and four new synthetic MCP processes. `delivery-check` rechecks freshness. The developer Skill/profiles remain repository-local. Existing client processes/catalogs need restart; fresh-process proof covers only new processes.

For changes to profiles/adapter, freeze the candidate and exercise actual host workers from the ordinary entrypoint. Retain raw output, exact launch observations, failed attempts and independent review. Controlled comparisons require preregistered identical source/model/settings, explicit budgets, fresh implementation contexts and separate reviewers, frozen independent hidden checks outside worker scope, repeated A/B/C and eligible larger arrangements. Keep one writer per candidate. Separate fixed-deadline findings from equal-total-compute claims; record actual usage if available and unattributable overhead separately. Catalog/route simulations are not coding trials.

