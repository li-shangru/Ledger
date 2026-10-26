# Ledger documentation

Start with [Getting started](getting-started.md) to install Ledger and choose its
private runtime. Contributors should read [Contributing](contributing.md) and
[AGENTS.md](../AGENTS.md). Financial coverage and account state come from authorized
Ledger evidence, never from these documents.

## Task map

| Need | Read first |
| --- | --- |
| Install the CLI, import a first statement, or connect MCP | [Getting started](getting-started.md) |
| Understand privacy and vulnerability reporting | [Security](security.md) |
| Review licensing | [Third-party notices](third-party-notices.md) |
| Implement a development task with domain advice | [Implementation Skill](agent/skills/ledger-implement-ticket/SKILL.md) and [factory](agent/factory/README.md) |
| Change dependencies, test, or integrate source changes | [Development and verification](agent/development.md) |
| Explicitly install into configured maintainer clients | [Local delivery and freshness](agent/delivery.md) |
| Import PDFs and review staging | [PDF import Skill](agent/skills/ledger-pdf-import/SKILL.md) |
| Import investments, audit selected statements, or recover an import/database | [Imports and recovery](agent/imports-and-recovery.md) |
| Map CSVs or install and author private importers | [Import adapters and template](agent/import-adapters.md) |
| Configure runtime defaults, run maintenance, or start the dashboard | [Local operations](agent/local-operations.md) |
| Answer financial questions or file typed intent | [Ledger query Skill](agent/skills/ledger-query/SKILL.md) |
| Inspect or change the MCP boundary | [MCP reference](agent/mcp.md) |
| Explain figures, reconcile schedules, or review/apply/undo proposals | [Planning and explanations](agent/planning.md) |
| Change rules, teaching, category hierarchy, or budget CSV workflows | [Configuration](agent/configuration.md) |
| Locate implementation layers and regressions | [Architecture](agent/architecture.md) |
| Change transfer pairing | [Transfer matching](agent/transfer-matching.md) |
| Run an explicitly requested weekly review | [Weekly review recipe](agent/weekly-review.md) |

## Documentation ownership

- `agent/` contains maintained operator and implementation references. Keep each
  procedure or contract in one primary home and link to it from entry points.
- `agent/skills/` contains the canonical operator Skills and the repository-local
  developer Skill. Preserve these paths; scripts and tests consume them.
- Examples use synthetic content and generic external paths. Replace placeholder
  paths with selected private locations before an authorized operation.
- Temporary plans, review records, build products, and audit output belong outside
  the source tree. Do not add private histories, runtime receipts, or financial
  artifacts to public documentation.

Update the relevant reference when behavior changes. Source checks establish
source behavior; installed-client checks and operator-selected statement audits
are separate evidence.
