# Getting started

Ledger 1.11.2 requires Python 3.14. Install the core CLI and both MCP servers from
a Git checkout or an extracted Ledger source distribution containing
`SOURCE_MANIFEST.json`:

```bash
git clone https://github.com/li-shangru/Ledger.git
cd Ledger
python3.14 -m venv .venv
source .venv/bin/activate
python -m pip install .
ledger version
ledger --help
```

The build backend copies only admitted source files and verifies the complete
wheel contents. A generic hosting-provider source ZIP without the manifest is
not an accepted build input; use a Git checkout or a backend-built source distribution.

This resolves core dependencies for your platform. The canonical lock and full
development environment target macOS 26 arm64; see [Development](agent/development.md)
before using optional extras or repository wrappers. Installation downloads
dependencies. Local financial processing does not require an online financial service.

## Choose private storage

Use a dedicated absolute directory outside all Git checkouts and Ledger source
archives. For example:

```bash
export LEDGER_HOME="$HOME/.local/share/ledger"
umask 077
mkdir -p "$LEDGER_HOME/inbox"
```

Keep this environment variable set for every command in this guide. Without an
override, Ledger uses `~/Library/Application Support/Ledger` on macOS. On other
systems it uses `$XDG_DATA_HOME/ledger`, or `~/.local/share/ledger` when
`XDG_DATA_HOME` is unset. Both environment overrides must be absolute.

The default database is `ledger.db` under that root. `LEDGER_DB` overrides its
location, and an explicit CLI `--db` overrides both. `defaults.toml` in the runtime
root can hold CLI defaults; the source-root `.ledgerrc` is not read. The tracked
[template](../.ledgerrc.example) contains only generic examples. Public seed
configuration is bundled in the installed wheel; private merchant rules and
category changes live in the runtime database.

For a first run, create and validate an empty database:

```bash
ledger config import --db "$LEDGER_HOME/ledger.db"
ledger db validate --db "$LEDGER_HOME/ledger.db"
```

`config import` initializes a missing database with the bundled synthetic seeds.
Replacing configuration in an existing database defaults to a dry run and requires
`--execute` after reviewing the proposed changes. Use a new private directory for
a first run; never use an existing financial database for development.

## Map a transaction CSV

The public core has no built-in institution layouts. A private CSV mapping is the
smallest path for a simple transaction export; no extension installation is needed.
Create a private profile directory, then map, preview and stage an authorized copy:

```bash
mkdir -p "$LEDGER_HOME/profiles"
ledger import csv map "$LEDGER_HOME/inbox/export.csv" \
  --save-to "$LEDGER_HOME/profiles/export-v1.json"
ledger import csv preview "$LEDGER_HOME/inbox/export.csv" \
  --profile "$LEDGER_HOME/profiles/export-v1.json" --show-rows
ledger import csv stage "$LEDGER_HOME/inbox/export.csv" \
  --profile "$LEDGER_HOME/profiles/export-v1.json" \
  --institution fictional --account example-account --account-type chequing \
  --db "$LEDGER_HOME/ledger.db"
```

Replace the fictional institution/account placeholders with the explicitly selected
private binding. The mapper asks for column roles, exact dates, signs and currency;
inspect the preview locally before staging. A profile has no account binding and
never confirms complete statement coverage or lifecycle. `--show-rows` prints
private values; omit it for count-only output. Use `ledger import csv review list`,
`status`, and `finalize` with the returned `--run-dir` after reviewing the evidence.
See [the full CSV and adapter guide](agent/import-adapters.md).

## Import a supported PDF

Review and install a trusted adapter for the selected layout, then explicitly
enable its installed wheel using [the adapter guide](agent/import-adapters.md#install-and-enable-an-adapter).
Python adapters execute with process permissions; installing or discovering one
does not automatically enable it. The core refuses unknown and ambiguous layouts.

PDF extraction requires Poppler's `pdftotext` executable on `PATH`. Install Poppler
with your operating system's package manager, then check `pdftotext -v`. Python's
core dependency install does not install this executable.

Copy an authorized bank or credit-card statement into the private `inbox` directory
and stage it:

```bash
ledger import pdf stage --db "$LEDGER_HOME/ledger.db"
```

The command prints a run directory and review information. Use the returned path
in place of `RUN_DIRECTORY` below. Stage writes to a staging database; it does not
publish transactions to the target database.

```bash
ledger import pdf status --run-dir RUN_DIRECTORY
ledger import pdf review list --run-dir RUN_DIRECTORY
```

Resolve review rows with an existing category, or mark them reviewed as-is only
after inspecting the evidence. Follow [Imports and recovery](agent/imports-and-recovery.md)
for review commands and their consequences. Finalize only after reviewing the
run and authorizing publication:

```bash
ledger import pdf finalize --run-dir RUN_DIRECTORY
```

Finalize checks review and reconciliation gates, backs up the target database,
publishes atomically, archives PDFs, and retains the run manifest. After an
interruption, resume the same run; do not restage to bypass recovery. Investment
statements use the [separate investment workflow](agent/imports-and-recovery.md#historical-investment-imports).
The destructive CSV rebuild command is unnecessary for a first PDF import.

## Dashboard

The supported dashboard wrapper uses the verified UI environment described in
[Development](agent/development.md#optional-features-and-platform-support). After
that setup, run it from the source directory with the same private runtime:

```bash
LEDGER_HOME="$HOME/.local/share/ledger" ./scripts/run-app
```

Open `http://localhost:8501`. The server binds to loopback and disables Streamlit
usage statistics. A missing database offers **Create ledger**; an empty database
opens Import Center. This single-user application has no remote authentication
or multi-user authorization layer; keep it local.

## Connect an MCP client

Configure the read server with absolute executable and database paths:

```json
{
  "command": "/absolute/path/to/Ledger/.venv/bin/ledger-mcp",
  "args": ["--db", "/absolute/private-ledger/ledger.db"]
}
```

Read tools cannot import, repair, or apply changes. An optional separate
`ledger-mcp-write` process uses `--proposals-db` and stores proposed intent only.
See [MCP](agent/mcp.md) for connection and evidence contracts. An external AI client
may send tool results to its provider; select the client and privacy settings
deliberately. A local server does not make the downstream client local.

Back up the private runtime, including the database, statement archives, and
recovery manifests. See [Security](security.md) and
[Local operations](agent/local-operations.md) before maintenance or exports.
