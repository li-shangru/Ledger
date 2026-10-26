# Local delivery and freshness

This is an opt-in maintainer workflow for an explicitly authorized installation
into configured clients, after independent review and source integration. It can
reinstall the checkout environment and synchronize global client Skills. It is
not a contributor prerequisite or a consequence of passing source checks. It uses
the Python 3.14 environment, local wheel builder, synthetic freeze gate, and MCP fixtures. It
does not accept a financial database, statement, or Inbox target.
It refuses a symlinked environment before running any build or bootstrap command,
so a misplaced environment link cannot redirect installation into another checkout.

An installed environment with private adapters requires an explicit additional
allowance: set `LEDGER_ALLOWED_ADAPTER_CONFIG` to an absolute external private pin
file, or pass `--allowed-adapter-config` to `deliver`, `delivery-check`, or the
environment checker. These checks validate enabled adapter versions, installed
RECORD bytes, artifact pins and entry-point origins without loading adapter code.
Unlisted distributions still fail; core dependency pins and audited wheel
provenance remain exact. Delivery snapshots and rechecks the selected pins and
records their digest and count. Parsing stays disabled in synthetic probes.

Dependency auditing submits only canonical public lock requirements, substituting
the reviewed upstream parent for a local derivative. Private adapter identities
are never submitted to package or vulnerability services. The audit requires an
exact clean result set with no skipped dependencies.

```bash
# Complete local release verification, installation, Skill deployment and activation proof.
./scripts/deliver

# Recheck the actual installation and launch four new synthetic MCP processes.
./scripts/delivery-check
```

`deliver` runs the full synthetic freeze with the live UI gate, selecting an
available port unless `PORT` is supplied. A failed gate stops installation. It
then refreshes editable metadata and console scripts using the documented
no-dependencies reinstall, synchronizes both canonical Skill trees, checks
dependency compatibility, and verifies fresh read and proposal-write MCP
processes in modern and legacy protocol modes. It records success only after
every check passes. The read queries must return the known synthetic total;
proposal filing stays in a separate disposable proposal store. Financial database
bytes and timestamps must remain unchanged, and cleanup must succeed.

Public `freeze-check` defaults to synthetic source-only verification and does not
inspect configured clients or require an installed receipt. The explicit form is:

```bash
LEDGER_FREEZE_SYNTHETIC=1 LEDGER_FREEZE_SOURCE_ONLY=1 ./scripts/freeze-check
```

This runs source gates and verifies that a newly built wheel contains every
canonical package file. It reports the source-only boundary; it does not claim installed delivery.
`deliver` uses this boundary internally to avoid recursively checking its own
installation before it installs. A source-only result cannot create a delivery
receipt. Set `LEDGER_FREEZE_SOURCE_ONLY=0` only when an installed freshness gate
is explicitly in scope.

## Evidence checked every time

- The configured Codex and Claude Ledger MCP launch commands must resolve to
  this checkout's actual `.venv/bin/ledger-mcp` (or its proposal writer).
  Project and local definitions are included: Claude's `.mcp.json` and applicable
  `projects[path].mcpServers`, and Codex's ancestor/project `.codex/config.toml`
  layers. Unsupported project enable/disable policies fail closed.
  Supported commands use one explicit runtime flag and no launch environment or
  working-directory overrides. Runtime argument values are discarded. The probe
  requires local execution; remote or unknown `experimental_environment` values
  fail because a local synthetic probe cannot verify a different executor. The probe
  replaces them with paths created by the verifier; it never executes a saved
  owner command. An unsupported wrapper or override fails with a specific code
  until the agent adds and tests a safe adapter or corrects the configuration.
- The CLI and both MCP scripts must be executable and use the intended virtual
  environment's Python. An isolated new interpreter must report the intended
  prefix, editable origin, module version, and distribution version.
- Every regular package file is hashed, including resources and instructions;
  extra or missing files fail. The newly built wheel, source and imported package
  must match. Wheel `METADATA` and console-script metadata must equal the installed
  metadata exactly, including same-version README or dependency changes.
- All files in `docs/agent/skills/ledger-query/` and
  `docs/agent/skills/ledger-pdf-import/` must match the byte-exact copies in
  `~/.agents/skills/ledger-query/`, `~/.agents/skills/ledger-pdf-import/`,
  `~/.claude/skills/ledger-query/`, and `~/.claude/skills/ledger-pdf-import/`.
  Extra old files fail. Synchronization
  stages and verifies a complete tree before replacement, refuses symlinks, and
  leaves an already-exact tree untouched on reruns.
- Both MCP initialize instruction bodies and complete discovered tool schemas
  must match canonical source. Every protocol/mode pair starts a separate
  process. Source, package, configuration command metadata, and Skill state are
  rechecked before writing the receipt, so edits during verification fail.

The ignored, mode-0600 `.venv/ledger-delivery.json` binds the verified source,
build inputs (including the dashboard, all scripts/tests, canonical seed configs,
vendor inputs, developer factory/catalog/profiles/Skill and the checked-in Streamlit configuration), metadata, executable paths and bytes, sanitized configuration
descriptors, and Skill manifests. It is a local receipt, not an approval or a
substitute for current checks. `delivery-check` recomputes the live values and
starts fresh processes again. Same-version source drift returns
`source_not_delivered`; stale metadata returns `editable_metadata_stale`; wrong
origins, commands, Skills, failed installation and failed probes have distinct
stage/error codes. Failures exit nonzero and never emit subprocess content or
configured runtime values. Private synthetic release and installation logs are
`.venv/ledger-delivery-freeze.log` and `.venv/ledger-delivery-install.log`.
Bounded fresh-process results or error codes are in `.venv/ledger-delivery-probe.log`.

The developer implementation Skill and domain profiles remain repository-local; they
are included in delivery freshness but are not copied into the operator Skill roots.
Ordinary repo guidance discovers this workflow. That is separate from native host
agent registration or proof that a client already loaded the new instructions.

## Maintenance and scope

The maintained implementation is `scripts/ledger_delivery.py`; the two shell
entry points only select delivery or verification. Add focused synthetic failure
tests in `tests/test_delivery.py` when adapting packaging, launch shapes, or client
targets. `--codex-config`, `--claude-config`, and repeated `--skill-root` options
select explicit integration targets; default targets are the two normal clients.
Do not silently broaden those targets or change shared client configuration.
Keep `LEDGER_WHEEL_DIR` and bulk temporary output outside the checkout. This
workflow currently targets the canonical macOS 26 arm64 environment. Dependencies
remain governed by the canonical lock and its existing environment
gate; the local reinstall uses `--no-deps` and does not re-resolve that lock.

A product version bump is appropriate when product behavior or shipped teaching
changes. Tooling-only delivery changes need no artificial product version bump.
Keep delivery receipts private and record new evidence separately. This command
does not push, publish, approve, or apply financial changes; publication requires
its own authorization.

Fresh-process proof shows what a new session receives. Existing MCP connections
and already-loaded Skill catalogs are not restarted or inspected by this workflow.
The handoff must identify that limitation and the necessary client restart.
