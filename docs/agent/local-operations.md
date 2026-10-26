# Local CLI and dashboard operations

Read this reference for operator-authorized local operations and CLI-defaults maintenance. Do not resolve real owner defaults during development or testing; pass explicit disposable paths. Configuration and retention details live in [configuration.md](configuration.md) and [imports-and-recovery.md](imports-and-recovery.md).

## Runtime and CLI defaults

`LEDGER_HOME` selects an absolute private runtime directory. When unset, the default
is `~/Library/Application Support/Ledger` on macOS, or `$XDG_DATA_HOME/ledger` on
other systems with `~/.local/share/ledger` as the fallback. `XDG_DATA_HOME`, when set,
must also be absolute. Runtime locations inside any Git checkout or Ledger source
archive are refused, including paths hidden by symlinks.

The default database is `ledger.db` inside that root. Database selection precedence
is:

1. Explicit CLI `--db PATH`
2. `LEDGER_DB`
3. `db` in the explicitly selected `--config PATH`, or runtime `defaults.toml`
4. `ledger.db` inside the selected runtime home

Other CLI flags override their defaults. The source tree's `.ledgerrc` and legacy
home-directory `.ledgerrc` are not searched. Use the tracked
[template](../../.ledgerrc.example) as a starting point for an optional
`defaults.toml` inside the private runtime; do not copy it into the project root.
The installed package supplies public seed configuration, so no source-root
configuration path is needed for normal use.

Examples in this reference use `/absolute/private-ledger` as a placeholder for
one dedicated private root. Replace it with your selected location and set:

```bash
export LEDGER_HOME=/absolute/private-ledger
```

The dashboard resolves the same database defaults and `LEDGER_DB`. Selecting
`--db` alone does not relocate inbox, archive, run, or backup defaults: keep
`LEDGER_HOME` aligned with the intended runtime, or supply all relevant path flags.
Do not select a broad directory such as your home as the database's parent;
maintenance and backup routines use that directory as their data boundary.

The normal PDF workflow uses `inbox/`, `import-runs/`, and `processed/` inside the
private root. Temporary CSV/PDF working copies are removed after finalize; small
run manifests and reports remain for recovery and traceability. Do not keep a
durable CSV archive for incremental PDF imports. Investment source CSV retention
is different; see [imports and recovery](imports-and-recovery.md).

## Local maintenance

The supported import and review commands live in [imports and recovery](imports-and-recovery.md);
financial questions use [read-only MCP](mcp.md). The commands below describe
operator-authorized operations, with explicit local paths.

```bash
# Local database operations and reports
.venv/bin/python -m ledger.cli db migrate --db /absolute/private-ledger/ledger.db
.venv/bin/python -m ledger.cli db validate --db /absolute/private-ledger/ledger.db
.venv/bin/python -m ledger.cli db backup --db /absolute/private-ledger/ledger.db
.venv/bin/python -m ledger.cli reports refresh --db /absolute/private-ledger/ledger.db
.venv/bin/python -m ledger.cli data clean --db /absolute/private-ledger/ledger.db
.venv/bin/python -m ledger.cli data clean --db /absolute/private-ledger/ledger.db --execute

# Diagnose drift between stored data and current code. Even without --execute,
# db doctor can initialize/migrate schema; --execute controls repairs, not all writes.
# Reports duplicate config rows, corrupted merchant patterns, schema drift, stale
# health scores, parser-version drift, statement-audit row mismatches, dangling
# references, and unmapped merchants -- each with its remedy. --execute repairs only
# the unambiguous subset, after a pre-mutation backup.
.venv/bin/python -m ledger.cli db doctor --db /absolute/private-ledger/ledger.db
.venv/bin/python -m ledger.cli db doctor --db /absolute/private-ledger/ledger.db --execute

# Recompute the derived analysis tables (accounts, balances, recurring patterns,
# trends, health scores, adjustments, snapshot, insights, quality checks).
# `reports refresh` only rebuilds the snapshot from what those tables already hold;
# this rebuilds the tables themselves. Needed after a code change that alters how a
# derived value is computed -- fixing the code does not re-derive stored rows.
.venv/bin/python -m ledger.cli analysis refresh --db /absolute/private-ledger/ledger.db

# List available parsers
.venv/bin/python -m ledger.cli parsers

# Show version
.venv/bin/python -m ledger.cli version
```

`db doctor` is a local mutation boundary even in diagnostic mode, and refuses an
open import before schema preparation. Development probes use disposable databases.
Do not infer read-only behavior from the absence of `--execute`; see
[the implementation](../../src/ledger/maintenance/doctor.py) and
[its regression](../../tests/test_review_teach.py).

Taxonomy and budget commands live in [configuration](configuration.md). For an
operator-selected statement audit, backup restore, or destructive CSV rebuild,
read [recovery](imports-and-recovery.md#recovery-and-statement-repair) first.
Proposal review, application, undo, and journal recovery are in [planning](planning.md).

## Account lifecycle and adjustments

`ledger accounts list --db <ledger>` shows covered months, observed cadence, and
the current gap verdict. Record closure only from operator confirmation:

```bash
.venv/bin/python -m ledger.cli accounts mark-closed <slug> --db <ledger> \
  --confirmed-on <YYYY-MM-DD>
.venv/bin/python -m ledger.cli accounts reopen <slug> --db <ledger>
```

Add `--closed-on <YYYY-MM-DD>` only when the exact closure date is known. A quiet
account or its stored `is_active` flag does not establish closure. The commands
write `account_lifecycle`; never close an account merely to quiet a coverage alert.

For true-cost adjustments, `ledger adjustments detect --db <ledger> --dry-run`
previews deterministic treatments for refunds, reimbursements, rewards, and offsets.
Use `--execute` instead of `--dry-run` only for an authorized application, then
`ledger adjustments validate --db <ledger>`. See [analysis limits](architecture.md#analysis-limits).

## Local exports and report evidence

`ledger reports list --db <ledger>` and `ledger evidence status --db <ledger>`
inspect retained report/evidence status. To create an authorized local aggregate
export, use `ledger export package --db <ledger> --preset chatgpt-safe --format zip`.
That preset omits transaction rows and merchant/payee names, buckets recurring
amounts to the nearest $5, and uses salted account references. Other presets, such
as `full-local-audit`, retain transaction detail and belong in private local storage.
Export creation does not authorize sharing or publication; all artifacts stay in private external storage.

## Dashboard

```bash
# Launch the dashboard; it offers Create ledger when no database exists.
./scripts/run-app

# Then open http://localhost:8501 in browser
```

Do not assume plain `streamlit run streamlit_app.py` works in a fresh shell.
Plain `streamlit` only exists after activating `.venv` or adding `.venv/bin` to
`PATH`. The supported no-activation command is `./scripts/run-app`.

`LEDGER_DB=/absolute/path/to/ledger.db ./scripts/run-app` selects another database;
`PORT=8502 ./scripts/run-app` selects another port. Both are operator choices, not
maintenance defaults. See [architecture](architecture.md#dashboard) for the page map.
