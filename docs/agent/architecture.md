# Architecture and implementation reference

Use this map to locate the affected code, then read its task reference and focused
regressions. Product semantics belong here; current account state and financial
coverage come from authorized evidence. [Development](development.md) owns checks
and [AGENTS.md](../../AGENTS.md) owns the safety and authorization boundaries.

## Implementation map

| Area | Entry points | Task reference |
| --- | --- | --- |
| CLI | [CLI facade](../../src/ledger/cli.py), [commands](../../src/ledger/commands/) | [Local operations](local-operations.md) |
| Runtime paths and packaged seeds | [Runtime resolver](../../src/ledger/runtime.py), [config loader](../../src/ledger/config/loader.py) | [Runtime defaults](local-operations.md#runtime-and-cli-defaults) |
| PDF import | [workflow](../../src/ledger/imports/pdf/workflow.py), [extractors](../../src/ledger/imports/pdf/statement_pdf_to_csv/) | [Imports and recovery](imports-and-recovery.md), [PDF Skill](skills/ledger-pdf-import/SKILL.md) |
| Historical investments | [adapter contract](../../src/ledger/adapters/contracts.py), [workflow](../../src/ledger/investments/workflow.py) | [Investment imports](imports-and-recovery.md#historical-investment-imports) |
| Transaction processing | [pipeline](../../src/ledger/processing/pipeline.py), [parser registry](../../src/ledger/parsers/registry.py) | Processing and identity below |
| Merchant/category rules | [schemas](../../src/ledger/config/schemas.py), [runtime repository](../../src/ledger/config/repository.py), [rules](../../src/ledger/rules/) | [Configuration](configuration.md) |
| Database and publication | [schema](../../src/ledger/storage/database.py), [writer](../../src/ledger/storage/writer.py), [snapshots](../../src/ledger/storage/snapshots.py) | Storage and publication below |
| Analysis | [runner](../../src/ledger/analysis/runner.py), [finance policy](../../src/ledger/finance_policy.py), [adjustments](../../src/ledger/adjustments/) | Analysis limits below |
| Transfer pairing | [matcher](../../src/ledger/analysis/transfer_matcher.py), [transfer queries](../../src/ledger/query/transfers.py) | [Transfer matching](transfer-matching.md) |
| Queries and MCP | [query facade](../../src/ledger/query/service.py), [read server](../../src/ledger/mcp/server.py), [proposal writer](../../src/ledger/mcp/write_server.py) | [MCP](mcp.md), [query Skill](skills/ledger-query/SKILL.md) |
| Investment details | [read builders](../../src/ledger/mcp/investments.py), [wire schemas](../../src/ledger/mcp/investment_contracts.py) | [Investment evidence](mcp.md#investment-detail-evidence) |
| Planning | [planning](../../src/ledger/planning/), [proposals](../../src/ledger/proposals/) | [Planning and explanations](planning.md) |
| Dashboard | [navigation shell](../../streamlit_app.py), [pages/components](../../src/ledger/ui/) | Dashboard below |

## Processing and identity

CSV processing runs parsing → validation → deduplication → enrichment → transaction/
transfer partitioning → transfer matching → storage and requested exports. The PDF
workflow wraps that pipeline in private staging, review, and atomic publication.
Historical investment imports own separate investment tables.

Institution-specific parsing lives in separately installed, explicitly enabled Python
adapters. The [API v1 contract and registry](import-adapters.md) define CSV parser
classes, PDF facts and reconciliation anchors, complete investment statements, and
inert historical interpretation metadata. Registration order never chooses an
ambiguous format. CSV profiles provide a core-owned alternative for transaction
exports, with explicit signs, currencies, account binding and exact-header checks.

The core owns validation, identity, deduplication, private staging, review, backups,
publication and recovery. Adapters are trusted Python code, not sandboxed plugins.
Read-only consumers use validated declarative metadata without importing them.
Staged work pins core bytes, enabled package artifacts, source bytes and relevant
profiles/configuration; an upgrade cannot silently reinterpret an existing review.

Investment facts preserve source-reported native currencies and nulls. Account
lifecycle remains `unknown` with no confirmation date unless an operator explicitly
records closure. NAV series and dashboard totals partition by statement currency;
activity and holdings retain their own currencies. Neither statement values nor a
recent import imply a live market valuation. The canonical performance total is
`Total (All Assets)`; a unique `Total` is a fallback only when that canonical total
is absent. Multiple fallback totals are ambiguous, never summed as a substitute.

Core models are frozen Pydantic objects. Use `Decimal` for financial calculations;
amount signs are negative for expense and positive for income. Parse money and
dates through [shared utilities](../../src/ledger/utils/). Transaction dates use
`date`; operational receipts may use timestamps.

`txn_id` is the stable 40-character hash used for deduplication. It includes
institution and canonical account plus a bank reference when available, otherwise
date, amount, and description. Non-CAD rows add a normalized currency suffix;
legacy CAD IDs stay stable. `txn_fingerprint` is the full-content 32-character
hash used to detect collisions. Strict mode rejects collisions and validation
failures; non-strict mode records warnings. Parser failures produce exception
records, and publication still requires its own gates to pass.

`flow_type` describes institution activity; `cashflow_type` and
`include_in_cashflow` govern reporting. Transfer matching uses `amount_raw`, because
transfer `amount_signed` is zero. See [transfer matching](transfer-matching.md) for
pairing evidence and [MCP semantics](mcp.md) for reporting policies.

## Storage and publication

Source and financial state have separate roots. The runtime resolver rejects
locations in any Git checkout or Ledger source archive, including lexical and
resolved symlink aliases. `LEDGER_HOME` and explicit path flags select external
state; public configuration seeds are packaged under `ledger/seeds` in wheels.
Resolving a default path does not itself create directories or open a database.

SQLite runtime configuration, imported facts, derived analysis, and planning state
are separate responsibilities. CSV/Parquet output is optional for `ledger build`;
those outputs do not replace SQLite for the normal import, MCP, or planning workflow.

The writer upserts by `txn_id` through SQLAlchemy `session.merge()`. Unchanged IDs
retain row-owned MANUAL/RULE overrides. Statement-authoritative reimports snapshot
those fields, match unchanged IDs first, and permit only a unique guarded
`(source_path, source_row)` fallback. Ambiguous MANUAL mappings abort; see
[override reconciliation](../../src/ledger/storage/overrides.py).

Use the shared private-file and SQLite snapshot/publication primitives. Final drift
validation and publication must stay inside the same write-exclusion boundary.
Preserve pre-mutation backups, owner-only modes, durable manifests, idempotent
recovery, and the open-import gate. The exact preserved tables and separate runtime
configuration-copying mechanism live in [recovery](imports-and-recovery.md#recovery-and-statement-repair).

For storage maintenance, retain two current implementation limits: the pinned
writer's descriptor check observes process-wide file activity and can refuse an
unrelated concurrent open; migration `0009` rebuilds its table using `executescript`
without an explicit transaction. Inspect [snapshots](../../src/ledger/storage/snapshots.py)
and [migrations](../../src/ledger/storage/migrations.py) before changing either path;
do not weaken identity checks to work around a refusal.

## Analysis limits

[AnalysisRunner](../../src/ledger/analysis/runner.py) refreshes accounts, balances,
recurring patterns, trends, health scores, adjustments, snapshots, insights, and
quality checks. Refreshing a report alone does not recompute those tables.

Account discovery starts from ordinary transactions. Transfer legs enrich discovered
accounts but do not create transfer-only accounts; statement-balance repair refuses
when a required account row is absent. Repair's seven/thirty-day statistics use the
last seven/thirty stored rows, which can span longer calendar windows across gaps;
see [statement repair](../../src/ledger/repair/statements.py).

[Income treatment](../../src/ledger/finance_policy.py) excludes an entire auto-applied
`reduce_category_cost` credit row. Any future partial-credit allocation support must
make income treatment amount-aware. Missing anchors, unsupported currencies, and
incomplete coverage remain disclosed limitations, not zero-valued facts.

## Dashboard

The sidebar groups Import Center, Review Queue, and Transactions for work;
Overview, Categories, Accounts, and historical Investments for reporting;
Budgets / Goals and Insights for planning; and Rules / Settings plus Admin / Data
for local configuration and maintenance. An empty database opens Import Center;
a missing database offers **Create ledger** without requiring a CSV rebuild.

Pages delegate calculations and mutations to shared services. Financial brief links
retain their exact period and evidence, explanations reconcile to displayed totals,
and proposal controls use the same approval/recovery boundary as the CLI. Read
[planning](planning.md) for those contracts and [imports](imports-and-recovery.md)
for staging/finalize behavior. Owner-visible UI changes require the synthetic UI gate
in [development](development.md).
