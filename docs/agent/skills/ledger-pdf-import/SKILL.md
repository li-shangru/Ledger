---
name: ledger-pdf-import
description: Use when processing statement PDFs supported by explicitly enabled trusted local adapters through Ledger's staged import workflows, including review, finalize/resume, cancellation or synthetic validation.
---

# Ledger PDF Import

## Choose the operation

1. Confirm the repo root is `<repo-root>` (the Ledger project checkout) and inspect `git status --short`. Preserve unrelated dirty files.
2. Keep raw PDFs and generated ledger artifacts local. Keep every runtime outside source repositories. Never stage or commit `ledger.db`, statement PDFs, generated CSVs, Parquet, import run directories, or manifests. Use freshly authored synthetic fixtures for tests and examples.

Select the requested operation before running a command. For an existing run,
start with `./scripts/import-pdf-inbox status --run-dir <run-dir>` and use its
current state and gates to choose the requested follow-up. Do not stage a new run
for a status, review, resume, or cancel request.

| Requested operation | Route |
| --- | --- |
| Investment statement import | Use the separate [investment workflow](#investment-statements); investments never enter transaction CSV processing. |
| New import | `stage`, then [review](#review-staging) and [finalize](#finalize-or-resume) within the authorized import scope. |
| Existing-run status | Report `status` and its gates; a status request does not authorize review writes or promotion. |
| Existing-run review | After `status`, use `review list --run-dir <run-dir>` and the authorized [staging decisions](#review-staging). |
| Finalize or resume an existing run | After `status`, use `finalize --run-dir <run-dir>`; `finalize` also resumes interrupted finalization. |
| Cancel an existing run | After `status`, use `cancel --run-dir <run-dir>` only where the cancellation gate permits it. Finalized runs cannot be cancelled; finalize recovery states require authorized `finalize`, not cancellation. |
| Synthetic validation | Use the complete [temporary validation](#temporary-validation) path overrides for every operation. |

Commands below use operator-authorized external runtime defaults.
`/absolute/private-ledger` is a generic placeholder: replace it with the selected
absolute private root and set `LEDGER_HOME` to that same directory. Runtime paths
inside any Git checkout or Ledger source archive are refused. The platform defaults
and database precedence are described in `<repo-root>/docs/agent/local-operations.md`. Readiness output is
diagnostic: it may list `staging` as potentially finalizable, but the finalizer
refuses an incomplete `staging` run. Follow the actual gates and recovery evidence;
never edit status, bypass a gate, or restage to evade a recovery refusal. Keep
custom paths from the selected run on follow-up commands.

The skill never writes SQLite rows directly; use repo APIs/scripts. The SQLite DB
is the working source of truth; retain PDFs as source-document archives in private
storage. Finalization does not authorize uploading or sharing them.

## Investment statements

Read `<repo-root>/docs/agent/imports-and-recovery.md` for the investment contract.
An authorized new import uses `./scripts/import-investment-inbox stage --institution <enabled-format>`,
followed by aggregate-safe `status --run-dir <run-dir>` and authorized
`finalize --run-dir <run-dir>`. Existing investment status, finalize/resume and cancel
requests use that same wrapper and selected run; do not start another stage.

The enabled adapter declares whether a PDF is the structured source or accompanies
a structured CSV. Retain the actual sources in the private investment archive;
never fabricate a missing counterpart. Missing printed cash, FX, performance,
generation time and prior values stay unavailable. Statement evidence does not
confirm lifecycle; leave it unknown unless the operator explicitly confirms closure.
An installation does not backfill evidence missing from earlier imports.

After an authorized import, use `ledger.investments.statements` to discover exact
statement references, then `ledger.investments.statement.get`,
`ledger.investments.positions`, and `ledger.investments.activities` for read-only
verification. Stored arithmetic is distinct from an independent source audit.
Use fresh synthetic sources and explicit `--inbox-dir`, `--db`, `--runs-dir`,
`--processed-dir` and `--backup-dir` under a disposable owner-only root for tests.
Never use operator defaults for development.

## Stage a new import

Use `./scripts/import-pdf-inbox stage` for a new import. Stage clones the target DB into `/absolute/private-ledger/import-runs/<run-id>/staging.db`, extracts PDFs into temporary CSVs, reconciles supported statements, and writes only to the staging DB. Normal imports clone from `--db /absolute/private-ledger/ledger.db`; operator-authorized fresh reruns can pass `--base-db /absolute/private-ledger/db-backups/<pre-import-snapshot>.db --db /absolute/private-ledger/ledger.db` (any snapshot taken BEFORE those PDFs were imported; `ledger db backup` writes them there).

## Review staging

Review the generated `merchant-review.md` and work the queue with `./scripts/import-pdf-inbox review list --run-dir <run-dir>`; the Streamlit `Import Center` / `Review Queue` pages show the same rows for a human. If the operator authorized merchant research, search only a sanitized merchant token—never an amount, account, date, statement, or full transaction description. Choose only from existing categories in the SQLite runtime config. While the run is open, create a confident recurring match with staging-only `review resolve --teach`; do not edit the live Rules / Settings config, which is guarded because it could strand this run's decisions. After finalize, an operator-requested TOML snapshot stays in an external owner-only destination such as `/absolute/private-ledger/exported`. Never copy runtime exports into tracked seeds; author tracked seed changes independently with synthetic placeholders. Truly ambiguous, sensitive, or one-off merchants can be marked `reviewed_as_is`; do not invent categories.

- `review resolve --key "<review-key>" --category "<category>"` records a decision, defaulting the merchant to the row's own; pass `--merchant` to override it and `--note` to record why.
- Add `--teach` to also create the reusable merchant rule, so this merchant stops coming back as a review row. It is written to the run's staging config and promoted by finalize; `--pattern` overrides the pattern otherwise derived from the staged RAW statement text. Never mutate live runtime config or transactions while this run can promote; the published live editing surfaces refuse and direct the operator to finalize, cancel, or teach in staging.
- `review reviewed-as-is --key "<review-key>"` accepts the auto-derived values and stops the prompt without freezing confidence, so a later rule pass can still improve the row.
- The subcommands are staging-only (no `--db`), `list` reports the live review gate rather than the manifest's stage-time snapshot, `resolve` refuses a category the run's config does not define, and a finalized or cancelled run is refused because no promotion would carry the decision.

## Finalize or resume

When finalization is authorized, use `./scripts/import-pdf-inbox finalize --run-dir <run-dir>` after merchant config edits and review decisions. For interrupted finalization, use that same run and command; its recovery proof determines whether promotion happened and resumes the remaining work without publishing twice. Finalize reprocesses the temporary CSVs, applies review decisions in staging, blocks on unresolved review rows, runs DB-wide post-processing, records processed PDF hashes, backs up the current target DB, atomically promotes the staging DB, moves PDFs to `/absolute/private-ledger/processed`, prints the merchant summary, removes staged CSV/PDF working copies, and retains the run manifest for traceability. It also builds one deterministic `financial_brief` after promotion and persists the same value in `run.json`, `manifest.json`, and `final-report.json`; interrupted finalize recovery reuses that persisted brief rather than rebuilding it.

Processed PDF archive names must keep the statement date first: `{statement-end-date}-{institution}-{statement-kind}-{account-type}-{last4}.pdf`, with institution and account values supplied only by the selected private source.

## Data Retention

Keep `/absolute/private-ledger/` lean. The normal durable local state is the SQLite DB plus `/absolute/private-ledger/inbox`, `/absolute/private-ledger/processed`, and small finalized import manifests/reports. CSVs produced from PDFs are temporary staging artifacts under the import run directory and must be removed by finalize. Do not keep a durable `/absolute/private-ledger/csv` archive for the incremental workflow.

## Dangerous Command

`./scripts/rebuild-db` is destructive for this workflow. It replaces the target DB from whatever CSVs currently exist under the CSV source directory. Because this workflow does not retain all historical CSVs in `/absolute/private-ledger/`, do not use `./scripts/rebuild-db` for routine PDF imports. Treat it as a recovery/admin command only.

The rebuild wrapper copies only the rebuilt DB by default. Set `LEDGER_COPY_EXPORTS=1` only when a recovery/admin run explicitly needs derived CSV/Parquet/JSON outputs.

## Commands

Use the selected external runtime for authorized owner operations; choose only the requested
operation. Maintenance and tests use [temporary validation](#temporary-validation).

```bash
export LEDGER_HOME=/absolute/private-ledger
./scripts/import-pdf-inbox stage

# The agent reviews merchant-review.md, optionally researches sanitized merchant tokens,
# and teaches confident matches into staging with review resolve --teach.

./scripts/import-pdf-inbox status --run-dir /absolute/private-ledger/import-runs/<run-id>

# Decide the review queue without a browser (staging-only; add --json for machine output)
./scripts/import-pdf-inbox review list --run-dir /absolute/private-ledger/import-runs/<run-id> [--status pending]
./scripts/import-pdf-inbox review resolve --run-dir /absolute/private-ledger/import-runs/<run-id> --key "<review-key>" --category "<category>" [--merchant "<merchant>"] [--note "<note>"] [--teach] [--pattern "<regex>"]
./scripts/import-pdf-inbox review reviewed-as-is --run-dir /absolute/private-ledger/import-runs/<run-id> --key "<review-key>" [--note "<note>"]

./scripts/import-pdf-inbox finalize --run-dir /absolute/private-ledger/import-runs/<run-id>

./scripts/import-pdf-inbox cancel --run-dir /absolute/private-ledger/import-runs/<run-id>
```

Package-backed CLI equivalents:

```bash
.venv/bin/python -m ledger.cli import pdf stage --db /absolute/private-ledger/ledger.db
.venv/bin/python -m ledger.cli import pdf status --run-dir /absolute/private-ledger/import-runs/<run-id>
.venv/bin/python -m ledger.cli import pdf review list --run-dir /absolute/private-ledger/import-runs/<run-id>
.venv/bin/python -m ledger.cli import pdf review resolve --run-dir /absolute/private-ledger/import-runs/<run-id> --key "<review-key>" --category "<category>"
.venv/bin/python -m ledger.cli import pdf review reviewed-as-is --run-dir /absolute/private-ledger/import-runs/<run-id> --key "<review-key>"
.venv/bin/python -m ledger.cli import pdf finalize --run-dir /absolute/private-ledger/import-runs/<run-id>
.venv/bin/python -m ledger.cli import pdf cancel --run-dir /absolute/private-ledger/import-runs/<run-id>
```

For an operator-authorized fresh reimport from the reviewed pre-import snapshot:

```bash
./scripts/import-pdf-inbox stage \
  --pdf-dir /absolute/private-ledger/processed \
  --processed-dir /absolute/private-ledger/processed \
  --base-db /absolute/private-ledger/db-backups/<pre-import-snapshot>.db \
  --db /absolute/private-ledger/ledger.db

# Review in Streamlit, then:
./scripts/import-pdf-inbox finalize --run-dir /absolute/private-ledger/import-runs/<run-id>
```

## Temporary validation

Use freshly authored synthetic PDFs and a synthetic baseline. Never copy owner
statements or databases. Create one owner-only disposable root at a canonical real
path outside every source checkout, then explicitly set all six data paths:

```bash
./scripts/import-pdf-inbox stage \
  --pdf-dir <temporary-root>/inbox \
  --processed-dir <temporary-root>/processed \
  --runs-dir <temporary-root>/runs \
  --base-db <temporary-root>/baseline.db \
  --db <temporary-root>/ledger.db \
  --backup-dir <temporary-root>/backups

./scripts/import-pdf-inbox review list --run-dir <temporary-root>/runs/<run-id>
./scripts/import-pdf-inbox review reviewed-as-is \
  --run-dir <temporary-root>/runs/<run-id> \
  --runs-dir <temporary-root>/runs --key "<synthetic-review-key>"

./scripts/import-pdf-inbox finalize \
  --run-dir <temporary-root>/runs/<run-id> \
  --pdf-dir <temporary-root>/inbox \
  --processed-dir <temporary-root>/processed \
  --runs-dir <temporary-root>/runs \
  --base-db <temporary-root>/baseline.db \
  --db <temporary-root>/ledger.db \
  --backup-dir <temporary-root>/backups
```

Keep the same overrides on status, finalize/resume, and cancel; review writes take
the custom `--runs-dir` alongside `--run-dir`. If validating custom configuration,
also point all three config flags at independently authored synthetic seed files.
The archived PDFs, backup, manifests, and databases must all remain inside the
disposable root. Remove only that root after validation.

## Supported statements and adapter changes

The public core includes no institution-specific statement layouts. Review, install
and explicitly enable an adapter before importing its supported PDFs. Follow
`<repo-root>/docs/agent/import-adapters.md` for API v1, trust and migration controls.
Unknown or ambiguous layouts fail closed; do not route a similar-looking statement
to a different parser or infer complete coverage from observed transaction dates.

Enabled adapters are trusted Python code with process permissions, not sandboxed.
Discovery and read-only metadata do not import packages. Unpublished runs require
their pinned core, adapter, source and profile bytes. Preserve the old executable
for legacy unbound runs, or cancel while permitted and restage under the new
version. A proven published recovery tail does not need parser execution.
Never edit manifests, pins or statuses to bypass recovery checks.

## Verification

After extraction, check:

- Stage reports the run directory, row counts, staging DB path, and merchant review path.
- Stage writes `manifest.json` with run status, source PDFs, artifacts, gates, counts, and next actions.
- `/absolute/private-ledger/ledger.db` is unchanged after stage.
- Statement validation has no blocking errors before DB promotion.
- Finalize reports resolved merchants, unresolved manual-review merchants, DB post-processing stats, and processed PDFs.
- The persisted `financial_brief` is identical in `run.json`, `manifest.json`, and `final-report.json`. A `ready` brief has three to five ranked observations; read its `coverage` before reporting any metric or observation. An `empty` brief may have fewer coverage-only observations. In both cases, coverage is limited to imported records only and absence does not prove real-world absence.
- Material plan-versus-actual exceptions link to the same reporting period in
  **Plan versus actual**. Inspect expected/actual dates and amounts, evidence,
  matching basis, history limitations, and the full posting-window coverage before
  describing a missing occurrence. Month labels alone do not prove that coverage.
- An unchanged reviewed reconciliation exception should not be raised again.
  **Update future schedule** creates a proposal for local preview and approval;
  it does not change the schedule during import. Approved future changes retain
  earlier expectations. Finalize or cancel an open import before applying or
  undoing a planning proposal; never mutate SQLite directly to bypass this gate.
- Finalize creates a pre-promotion DB backup under `/absolute/private-ledger/db-backups/`.
- Successfully finalized PDFs moved to `/absolute/private-ledger/processed`; skipped or failed PDFs remain in `/absolute/private-ledger/inbox`.
- Temporary CSV and staging-inbox files are removed after successful finalize; `manifest.json`, `run.json`, and final reports remain for traceability.
- `processed_files` contains the processed PDF path, SHA-256 hash, processed timestamp, and transaction count so repeated PDFs can be skipped.
