# Contributing

Ledger welcomes improvements to its offline, single-user workflows. Start with
[AGENTS.md](../AGENTS.md), the affected implementation, and the relevant reference
in the [documentation index](README.md). The project uses Python 3.14.

## Keep source separate from financial state

Use independently authored synthetic statements, accounts, merchants, and amounts.
Do not attach real statements, database files, private exports, credentials,
personal paths, account identifiers, or runtime logs to a contribution. Redacting
a real financial file does not make it a suitable fixture. Preserve the
source/runtime path checks and owner-only permissions.

Keep test databases, temporary workspaces, wheels, coverage reports, screenshots,
and audit output outside the source checkout. Pass external destinations through
arguments or environment variables. A runtime path inside any Git checkout or
Ledger source archive is refused; `.gitignore` is not a storage boundary.

## Make and verify a change

For substantive work, write concise acceptance criteria and inspect the affected
tests. Keep one implementation owner, obtain independent review, and resolve
substantiated findings before integration. Add focused synthetic regressions for
changes to money, privacy, path handling, approval, publication, or recovery.
Prose-only changes need a diff and referenced-path review rather than a test that
merely mirrors the prose.

Follow [Development](agent/development.md) for the platform-specific environment.
The full lock and local Playwright derivative target macOS 26 arm64. Core package
installation is portable dependency resolution, not proof that another platform
passes the complete development or UI gates.

The normal contributor gate is:

```bash
LEDGER_FREEZE_SYNTHETIC=1 LEDGER_FREEZE_SOURCE_ONLY=1 ./scripts/freeze-check
```

Those modes are also the public defaults. Supply `LEDGER_WHEEL_DIR` for the
external wheel directory and an external `TMPDIR` when routing temporary output.
For dashboard changes, include `LEDGER_FREEZE_UI=1` and an available `PORT`.
Synthetic checks never establish the coverage or correctness of a real source set.

`scripts/deliver` is an explicit maintainer installation workflow. It can reinstall
the checkout environment and synchronize Skills in configured client directories.
It is not required for a source contribution and must not run merely because a
source check passed. Source-only work does not inspect or change existing client
configuration or private runtimes.

## Preserve financial contracts

Keep read-only MCP read-only. Preserve staging review, unresolved-review gates,
pre-mutation backups, atomic publication, durable recovery, exact decimal
arithmetic, currency separation, and coverage disclosures. A filed proposal is
intent; application requires its exact reviewed proposal and preview revisions.
Never infer financial approval from a coding request or bypass an open-import gate.

Document the behavior change, relevant checks, platform limitations, and any
remaining verification gap. Submit only source and synthetic evidence appropriate
for public review. Publishing a contribution or release is a separate action from
preparing it locally.

Contributions to Ledger-authored code are provided under the project's
[MIT License](../LICENSE). Preserve third-party notices and licenses when adapting
dependency packaging; see [Third-party notices](third-party-notices.md).
