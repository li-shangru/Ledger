# Planning and financial explanations

Read this reference for explanations, schedules, reconciliation, and local proposal
review/application. [AGENTS.md](../../AGENTS.md) defines the financial approval
boundary: filing intent does not approve application. The commands below describe
operator-authorized local operations; maintenance uses explicit disposable paths.
Use the [query Skill](skills/ledger-query/SKILL.md) for agent routing and
[configuration](configuration.md) for budget CSV suggestions/imports.

## Financial briefs and explanations

After a successful PDF finalize, Ledger creates a local financial brief over the
latest reporting window. It names the equal-length comparison period, ranks three
to five observations, points to supporting dashboard/evidence details, and states
data freshness and statement coverage. Detected associations are labelled as
possibilities; incomplete statement coverage is never rendered as inactivity.
Supporting links stay in the same app session, retain the brief's exact reporting
period, and open its category, merchant, or matching findings when available.
Links without a specific finding subject show findings for that exact period.

Headline figures expose **Explain this number** details from the same calculation
service used by the read-only agent tools. Explanations state the formula, period,
CAD policy, source dates, contributing transactions, exclusions, refunds,
reimbursements, and adjustments. Their components reconcile to the displayed
total. Budgeted, Actual gross spending, and Variance have separate explanation
metrics; Actual and Variance still reconcile against zero when no budget has been
defined. Statement balances retain their anchor date and projections list every
assumption, so neither appears as a live bank balance.

## Spending preferences

Insights can remember that a merchant or category is intentional, necessary, or
worthwhile. Preferences are editable and reversible. They lower the priority of a
size-only reduction suggestion while leaving new price changes, duplicate-charge
possibilities, unresolved reimbursements, anomalies, and existing Watch/Ignore/
Reviewed state visible.

## Schedules and cash plans

Planning can store confirmed or hypothetical schedules for income, bills, card
payments, and transfers. These records are separate from imports. Cash plans make
both legs of transfers and card payments explicit, avoid treating payments as new
spending, disclose occurrences suppressed by matching imported activity, and show
the lowest projected statement-anchored balance and date. Before-next-payday views
appear only when the supporting income schedule exists, including unpaid income
due today. A bill or income occurrence is suppressed only when a unique
account/date/amount/type match also identifies the same normalized merchant or
linked recurring pattern. If that identity is unavailable, the occurrence stays
planned and can overlap imported activity.

Schedule forms preview the next six dates before saving. Semimonthly schedules
keep two calendar days 15 days apart from their starting date; a start on the 15th
or month-end uses the 15th and calendar month-end. Short months clamp a date
without changing the pair used in later months.

## Plan versus actual

**Plan versus actual** compares confirmed schedule occurrences with imported
activity over one explicit reporting period. It shows expected and actual dates
and amounts, independent amount/date changes, matching evidence, ambiguous
candidates, and account-specific coverage. Only a precise imported statement
interval covering the complete allowed posting window can support a missing
occurrence; a statement month or sparse activity cannot. Recent windows, old
month-only evidence, and incomplete schedule history disclose their limitations.
Hypothetical schedules and detected possibilities do not create authoritative
missing-payment findings. Approximate reconciliation does not change cash-plan
suppression or cashflow arithmetic; transfers and card payments retain both legs.

Matching uses CAD, canonical account identity, a posting window of three days on
either side, and an amount difference no larger than the smaller of $100 or 20%
of the expected amount, with a one-cent minimum. Bills and income also require a
matching normalized merchant or linked recurring pattern. Every plausible
candidate participates in ambiguity checks, including competing occurrences just
outside the selected period; one imported event cannot satisfy two expectations.
Reporting periods are limited to 366 days with pages of at most 100 findings.
Unsupported or incomplete statement summaries cannot establish absence, even
when imported transactions are available for comparison.

Schedule edits have effective dates and retain previous definitions. Future
corrections and removals preserve historical expectations, while schedules that
predate revision tracking disclose incomplete history. A reviewed reconciliation
exception stays reviewed until its material evidence changes. Actionable exceptions
appear in the finalized financial brief and link to the same period and evidence.

## Typed planning proposals

An agent can discover a budget, schedule, or spending preference through bounded
`ledger.planning.definitions` reads using exact user-supplied filters and opaque
references. `ledger.proposals.planning.file` files typed intent in the separate
proposal store. The writer never opens the financial database. Essential account,
category, currency, cadence, and confirmation ambiguity must be resolved before
filing; natural-language interpretation remains in the calling agent.

Local proposal review shows exact before/after values, the target revision,
effective date, upcoming schedule dates, and quantified impact with assumptions.
Budget previews show definition-limit changes; schedule previews show signed
movement per account over a stated horizon before imported-event suppression.
Preference changes affect recommendation priority. Schedule proposals take effect
after today; budget and preference changes take effect when applied. Application binds
to that exact reviewed proposal and preview revision, rechecks under publication
locks, and atomically records the intended change and its receipt. Retries recover
the proposal-store tail without applying twice. Targeted audited undo checks for
intervening changes and affects only that record, so later imports and unrelated
planning edits survive. **Update future schedule** files the same kind of proposal
from a reconciliation finding. Apply or undo is refused while an import can still
publish; finalize or cancel that run first. Existing merchant-rule proposals retain
their original application behavior.

Inspect and decide a typed planning proposal locally. Replace the generic
`/absolute/private-ledger` paths below with the selected external runtime; these
commands are operator actions, not a development recipe:

```bash
.venv/bin/python -m ledger.cli proposals list --kind planning --proposals-db /absolute/private-ledger/proposals.db
.venv/bin/python -m ledger.cli proposals preview <id> --db /absolute/private-ledger/ledger.db --proposals-db /absolute/private-ledger/proposals.db
.venv/bin/python -m ledger.cli proposals apply <id> --kind planning \
  --db /absolute/private-ledger/ledger.db --proposals-db /absolute/private-ledger/proposals.db \
  --proposal-revision <reviewed-proposal-revision> --preview-revision <reviewed-preview-revision>
.venv/bin/python -m ledger.cli proposals reject <id> --kind planning \
  --proposals-db /absolute/private-ledger/proposals.db --proposal-revision <reviewed-proposal-revision>
.venv/bin/python -m ledger.cli proposals undo <id> --db /absolute/private-ledger/ledger.db \
  --proposals-db /absolute/private-ledger/proposals.db --after-revision <applied-after-revision>
```

Choose apply or reject after preview. The after revision is returned by application
and shown when an applied proposal is previewed. Local typed filing is also
available with `proposals file --intent-file <private-json> --proposals-db <store>`;
keep that intent file private. A validation error or stale preview requires a
fresh discovery/preview and, when target intent changed, a replacement proposal.
If preview reports `recovery_required`, recover the reserved application using
its `application_preview_revision` before rejecting or replacing it. The dashboard
offers the same recovery action. Retrying an already undone receipt reports that
it remains undone and does not reapply the change.

## Interrupted application and recovery

If a process stops while applying or undoing a proposal, retrying with the same
reviewed revisions also recovers interrupted SQLite storage in both databases.
If the list or preview cannot open, first use **Proposals → Recover after an
interruption → Recover interrupted storage**, or run:

```bash
.venv/bin/python -m ledger.cli proposals recover \
  --db /absolute/private-ledger/ledger.db --proposals-db /absolute/private-ledger/proposals.db
```

This local operation restores storage access while preserving proposal decisions,
saved approvals, and committed receipts. Then reopen the preview and finish its
recovery action. It uses the existing write locks and refuses financial recovery
while an import can still publish. Read-only MCP access remains read-only and
refuses journals until local recovery completes. Do not delete journal files or
use a database rebuild to recover an interrupted application.

If the financial database itself needs recovery, the dashboard pauses before
initialization and offers **Recover database access**. Complete that guarded step
first, then open Proposals to recover its store and review the saved application.

## Persistence and implementation

Schedules, effective revisions, preferences, reconciliation reviews, application/undo
receipts, and definition generations are durable runtime state. Imports and statement
repair preserve them; transaction CSVs cannot reconstruct them. The exact preservation
mechanisms and backup boundary are in [imports and recovery](imports-and-recovery.md).

Start with [planning](../../src/ledger/planning/),
[typed proposals](../../src/ledger/proposals/planning.py),
[proposal application](../../src/ledger/proposals/planning_apply.py), and
[financial explanations](../../src/ledger/reporting/explanations.py). Use the planning,
reconciliation, proposal, and UI regression suites under [tests](../../tests/).

The older merchant-rule proposal path in
[ProposalService](../../src/ledger/proposals/service.py) does not share typed
planning's durable reservation protocol: application releases the proposal-store
lock before its financial write and records the applied state afterward, so a
concurrent rejection can intervene. Keep legacy apply/reject operations serialized;
do not claim typed planning's atomicity and recovery guarantees for that path.
