# Imports and recovery

Read this reference for an operator-authorized import or recovery, or while
maintaining those paths with synthetic fixtures. Commands below describe
authorized operator actions, not maintenance or test commands.
`/absolute/private-ledger` is a placeholder for a dedicated external runtime;
replace it with the selected absolute path and set `LEDGER_HOME` to that same
root. For isolated validation, use the complete disposable-path recipe in
[the PDF import Skill](skills/ledger-pdf-import/SKILL.md#temporary-validation).

## PDF review and finalize

Use the repo wrapper; it does not require activating the virtual environment.
The [PDF import Skill](skills/ledger-pdf-import/SKILL.md) owns operator review and
the complete isolated validation recipe. Supported layouts come only from
[explicitly enabled trusted adapters](import-adapters.md).

```bash
export LEDGER_HOME=/absolute/private-ledger
./scripts/import-pdf-inbox stage
# The agent performs merchant review/config updates and Review Queue decisions.
# Decide them from the CLI -- no browser and no ad-hoc SQL required:
./scripts/import-pdf-inbox review list --run-dir /absolute/private-ledger/import-runs/<run-id>
./scripts/import-pdf-inbox review resolve --run-dir /absolute/private-ledger/import-runs/<run-id> \
  --key "<review-key>" --category "<existing-category>"
./scripts/import-pdf-inbox review reviewed-as-is --run-dir /absolute/private-ledger/import-runs/<run-id> \
  --key "<review-key>"
./scripts/import-pdf-inbox finalize --run-dir /absolute/private-ledger/import-runs/<run-id>
```

`review` is staging-only: it reads and writes the run's `staging.db` and never
`/absolute/private-ledger/ledger.db`, which is why it takes no `--db`. `review list` reports the LIVE
review gate (the manifest's top-level `gates.review` is a stage-time snapshot that
goes stale as soon as a decision is saved). `review resolve` refuses a category the
run's config does not define -- an unknown name would make the applier clear
`exclude_from_spending` and freeze it behind `manual` confidence, which no later rule
pass repairs. Finalized and cancelled runs are refused outright: their staging DB
survives cleanup and would accept writes that are never promoted. Use
`--merchant` to override the row's merchant, and read the run's `merchant-review.md`
for verbatim transaction examples.

`review resolve --teach` additionally creates the reusable **merchant rule**, so the same
merchant does not come back as a review row on the next import. It is written to the run's
staging config and promoted by finalize, like every other review write; `--pattern`
overrides the pattern otherwise derived from the staged RAW statement text (never from the
merchant NAME you type — import enrichment only ever sees raw text). A rule taught into an
excluded category inherits that category's `exclude_from_spending`; an existing rule's flag
is preserved, never silently flipped.

**Do not teach the live database while a run is open.** Any live edit to runtime config or
`transactions` during the stage→finalize window can strand the run's staged decisions. The
Transactions page's remembered-rule and one-off category buttons, every Rules / Settings save,
`config import --execute`, and `rules apply --execute` refuse while a run can still promote,
including finalize-recovery states. Runs staged under a custom `--runs-dir` are found through
durable, owner-only coordination beside the target database. Finalize or cancel the run first,
or teach inside it with `review resolve --teach`, which remains staging-only.

The incremental import flow:
1. Extracts supported inbox PDFs into temporary parser-compatible CSVs
2. Reconciles statement balances when the extractor supports it
3. Clones the target DB, or `--base-db` for fresh reruns, into a staging DB
4. Normalizes merchants and assigns categories in staging
5. Blocks finalize until Review Queue rows are resolved or marked reviewed
6. Promotes staging atomically only after post-processing succeeds
7. Writes `manifest.json` with status, gates, artifacts, counts, and next actions
8. Builds a deterministic three-to-five-observation financial brief from the promoted database

The Streamlit Import Center exposes the same upload, stage, review, finalize/resume,
and cancel state machine. Its controls do not bypass CLI gates: uploaded PDFs are
copied into the owner-only inbox without clobbering an existing file, review writes
remain staging-only, and finalize/resume uses the publication protocol's existing
idempotency and recovery proof.

Fresh rerun from the reviewed baseline:
```bash
./scripts/import-pdf-inbox stage \
  --pdf-dir /absolute/private-ledger/processed \
  --processed-dir /absolute/private-ledger/processed \
  --base-db /absolute/private-ledger/db-backups/<pre-import-snapshot>.db \
  --db /absolute/private-ledger/ledger.db
./scripts/import-pdf-inbox finalize --run-dir /absolute/private-ledger/import-runs/<run-id>
```

Finalize:
- Detects and matches transfers across accounts
- Writes to the promoted database with all analysis:
   - Account detection & balance calculation
   - Recurring pattern detection (subscriptions, bills, income)
   - Spending trend aggregation
   - Financial health scoring
   - Transfer pairing
- Records processed PDF hashes, backs up the previous target DB, prints the merchant summary, and retains the run manifest after finalize
- Stores the financial brief in the final report and presents it in Import Center

For status or cancellation use `./scripts/import-pdf-inbox status --run-dir <run>`
or `./scripts/import-pdf-inbox cancel --run-dir <run>`. The package-backed equivalent
is `.venv/bin/python -m ledger.cli import pdf <subcommand>`.

Readiness output is diagnostic: it includes `staging` among potentially finalizable
states, but the finalizer refuses an incomplete `staging` run. Follow the actual
finalizer gates and recovery evidence; never force a status to make publication pass.

## Mapped CSV imports

Use [CSV mapping](import-adapters.md#map-a-transaction-csv) for an unfamiliar
transaction export. A saved profile describes the exact format, without an account
binding. Stage requires explicit `--profile`, `--institution`, `--account`, and
`--account-type` choices. It previews all rows again and binds the selected source,
profile and account interpretation before review:

```bash
ledger import csv stage /absolute/private-ledger/inbox/export.csv \
  --profile /absolute/private-ledger/profiles/export-v1.json \
  --institution fictional --account example-account --account-type chequing \
  --db /absolute/private-ledger/ledger.db
ledger import csv review list --run-dir /absolute/private-ledger/import-runs/<run-id>
ledger import csv status --run-dir /absolute/private-ledger/import-runs/<run-id>
ledger import csv finalize --run-dir /absolute/private-ledger/import-runs/<run-id>
```

The account names above are fictional placeholders, not inferred defaults. CSV
`review`, `status`, `finalize`, and `cancel` use the same staging/publication engine
and gates as PDF imports. Mapping and preview do not publish. A mapped export is
unreconciled transaction evidence: no balances, printed statement interval,
complete statement coverage, or account lifecycle are inferred from parsed rows.

## Historical investment imports

Investment imports use an explicitly enabled trusted local adapter. The adapter
must declare complete statements, position snapshots and statement balances;
partial trade or cash exports cannot masquerade as complete statements. Read
[adapter installation and capabilities](import-adapters.md) first.

```bash
./scripts/import-investment-inbox stage --institution <enabled-format>
./scripts/import-investment-inbox status --run-dir /absolute/private-ledger/investment-import-runs/<run-id>
./scripts/import-investment-inbox finalize --run-dir /absolute/private-ledger/investment-import-runs/<run-id>
```

Review aggregate-safe status and gates before finalization. `cancel` and
interrupted-finalize recovery use the same selected run. The selected format
declares a flat PDF source or account folders containing structured CSV/PDF pairs.
For paired input, the CSV is structured evidence and the PDF confirms account and
period; an allowed unmatched overlapping PDF remains an evidence-only PDF and
creates no structured investment record. A PDF-only format retains its PDF as the
structured source. Never fabricate a missing source or silently substitute a format.

Keep source folders, private manifests, archives, hashes, identifiers, balances
and holdings outside Git and shared output. Runtime paths include
`/absolute/private-ledger/inbox/`, `investment-import-runs/`,
`processed/investments/<institution>/`, and `db-backups/` beneath the same external
root. The importer enforces `0700` on managed directories and `0600` on managed files. Preserve owner-only modes during recovery.

The ordinary transaction-PDF workflow removes generated CSV working copies after
finalize. Investment imports instead retain each archived structured CSV source
and its evidence PDF, or the structured PDF for a PDF-only format. Do not apply
the temporary-CSV cleanup rule to durable private investment sources.

Investment data writes only investment tables. It does not enter `transactions`,
`transfers`, spending, income, cashflow, or transaction account balances. Native
currency cash, holdings and activity retain printed values. Missing cash balances,
FX rates, performance, prior values and generation time remain null. Cash activity
reconciles exactly; any adapter-specific quantity/price display allowance must be
explicit and cannot relax cash reconciliation. Installing new code does not
backfill missing evidence or certify an independent audit of original statements.

Statement evidence alone leaves account lifecycle `unknown` with a null
confirmation date. Only operator-confirmed closure uses these flags:

```bash
./scripts/import-investment-inbox stage --institution <enabled-format> \
  --mark-closed <operator-confirmed-account-alias> \
  --transfer-destination <operator-confirmed-destination> \
  --status-confirmed-on <YYYY-MM-DD>
```

Resolve lifecycle, destination and confirmation date from authorized current
evidence or the operator. Leave `closed_on` unset when no exact closure date is
confirmed; never infer closure from the last statement, zero holdings, account
label, or confirmation date. Parser defaults are not owner confirmation.

Historical views select a reporting currency before combining statement values.
CAD and USD portfolios are never summed together; other native currencies are
not converted into a selected activity total. There are no live quotes or
forward-filled valuations. Report current value as unavailable (not zero) when
no supported current source has been imported. Performance uses the canonical
`Total (All Assets)` row, or a unique `Total` only when the canonical row is absent.
Do not add overlapping totals or manufacture a total from incomplete components.

If publication completed but archival did not, state is `archive_pending`. Run
`./scripts/import-investment-inbox finalize --run-dir <run>` to resume the same
idempotent archive tail. Do not restage or publish again. A different file at an
archive destination is a blocking conflict and must never be overwritten.
Recovery validates investment-owned and preserved-table fingerprints plus
immutable transition provenance before deciding whether publication happened.
Archive retries complete file/directory durability barriers before success;
cancellation records its aggregate audit before deleting staging.

New flat-source runs also bind the original source basename. If a different file
reuses that name while archival is pending, move the new file aside before retrying.
The archive tail verifies the reviewed bytes without loading an adapter, so it
cannot safely classify the replacement as unrelated. Both files and the published
database remain unchanged on this refusal; harmless renames of the reviewed bytes
still work.

## Pinned interpretation and upgrades

New runs pin source bytes, core Python bytes, the selected adapter artifact/version
and API/layout metadata, and applicable configuration/profile revisions. Replacing,
disabling or losing an adapter needed for unpublished work fails closed. A changed
profile or source cannot silently reuse an earlier review. Read-only reports use
validated inert metadata, so they do not execute extension code.

Keep the original executable and environment to finish a legacy unpublished run
that lacks these bindings, or cancel it while cancellation is allowed and restage
with the new version. Do not invent pins for an old run. Once immutable recovery
proof establishes publication, the remaining archive/report/cleanup tail can run
without loading the adapter. A `published` flag alone is not that proof. The
[adapter migration guide](import-adapters.md#upgrade-and-recovery) covers the handoff.

## Recovery and statement repair

`ledger repair statements --db <ledger> --pdf-dir <selected-pdf-directory>` validates
a candidate by default. Add `--execute` only for authorized backup and promotion
after reviewing the candidate. Preserve the import gate and the two state-survival
mechanisms below; this is distinct from a CSV rebuild.

`./scripts/rebuild-db` is destructive for the incremental PDF workflow because
it replaces the target database from the current CSV source. Use it only for
recovery/admin work with a complete CSV archive. It builds into a temporary
database first and only replaces the target database after it verifies that the
build produced rows and zero parse exceptions. By default it copies only the DB;
set `LEDGER_COPY_EXPORTS=1` only when derived CSV/Parquet/JSON outputs are
explicitly needed.

It also cannot reconstruct investment tables from transaction CSVs. Investment recovery requires
an investment-aware database snapshot; never use `rebuild-db` to recreate historical investments.

The same hazard covers everything else no transaction CSV can regenerate: runtime **config**
(merchant rules, their exclusions, and the category hierarchy), **budgets** and savings goals,
spending preferences, planned schedules, schedule revision history, reconciliation
reviews, planning application/undo receipts, and the operator's own "this account is closed" records
(`account_lifecycle`, written only by `ledger accounts mark-closed | reopen`). `rebuild-db`
regenerates none of it.

`repair statements` is different, and it carries this state forward by **two separate
mechanisms** — do not assume one covers the other:

- **`merge_forward` copies `MERGE_FORWARD_TABLES` and nothing else**: `budgets`,
  `savings_goals`, `spending_preferences`, `planned_schedules`,
  `planned_schedule_revisions`, `plan_reconciliation_reviews`,
  `planning_proposal_receipts`, `planning_definition_generations`, `config_metadata`,
  `account_lifecycle`. Promote publishes an otherwise
  empty candidate wholesale, so membership in that tuple is exactly what makes a table
  survive. **Merchant rules are not in that tuple** — `readiness.py` names them in its
  FAIL-SAFE BOUNDARY comment as deliberately excluded.
- **Merchant rules and categories survive through `copy_runtime_config`** instead, which reads
  them from the **live database** whenever `source_repo.has_runtime_config()` — the normal
  case — and reseeds from the synthetic seed TOMLs only when it does not.

Recovery for anything either mechanism misses is the pre-promote backup.

### Backups

Two separate backup directories are used depending on the operation:

- `/absolute/private-ledger/db-backups/` — created automatically by PDF import finalize, `repair statements`, and `optimize categories`. Timestamped copies named `ledger-<timestamp>-<uniquifier>-<reason>.db`.
- `/absolute/private-ledger/backups/` — created by `./scripts/rebuild-db` before it promotes a newly rebuilt database. Timestamped copies named `ledger-<timestamp>-<uniquifier>-pre-rebuild.db`.

You can also take a manual backup at any time:

```bash
.venv/bin/python -m ledger.cli db backup --db /absolute/private-ledger/ledger.db
```

### Snapshot restore

Stop the dashboard and import/rebuild processes, then restore through the SQLite-aware snapshot
service. Replace the backup path below with either retained backup form:

```bash
.venv/bin/python - <<'PY'
from pathlib import Path

from ledger.storage.snapshots import restore_database

restore_database(
    Path("/absolute/private-ledger/db-backups/ledger-<timestamp>-<reason>.db"),
    Path("/absolute/private-ledger/ledger.db"),
)
PY
```

This validates the snapshot, uses SQLite's online backup API, and tightens the live database before
transferring backup bytes. Do not copy a backup directly over a potentially active SQLite database.

After an authorized restore, use `ledger analysis refresh --db <ledger>` when derived
tables need recomputation. `reports refresh` only rebuilds the snapshot from stored
analysis; it does not recompute those tables. Neither operation reconstructs missing
runtime state. Read [local operations](local-operations.md) for their commands.

Whole-database restore is recovery/admin work, not ordinary planning undo. For an
interrupted planning apply or undo, use [guarded proposal recovery](planning.md#interrupted-application-and-recovery).
Do not delete SQLite journals or overwrite an active database.

## Operator-selected statement audit

`./scripts/audit-statements` runs the source-to-ledger audit in failing mode and
writes private external evidence under `/absolute/private-ledger/audit-runs`. The selected database,
retained PDF set, period, and output directory can be supplied through `LEDGER_DB`,
`LEDGER_AUDIT_PDF_DIR`, `LEDGER_AUDIT_START`, `LEDGER_AUDIT_END`, and
`LEDGER_AUDIT_RUNS_DIR`. The lower-level entry point is `ledger audit statements`.

This audit requires an explicitly selected operator source set. It is separate from
[synthetic development verification](development.md); synthetic freeze refuses the
retained-source audit combination and never proves real-world completeness.
