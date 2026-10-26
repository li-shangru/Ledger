# Security and privacy

Ledger is a local, offline, single-user application. It keeps financial state
separate from source and makes financial writes explicit and recoverable. It is
not a hosted service, a multi-user server, or a substitute for operating-system
access controls.

## Storage boundary

Keep the private runtime outside every Git checkout and Ledger source archive.
The default runtime is `~/Library/Application Support/Ledger` on macOS, or
`$XDG_DATA_HOME/ledger` on other systems with `~/.local/share/ledger` as the fallback.
`LEDGER_HOME` selects an absolute alternative. Path checks inspect lexical and
resolved locations so a symlink does not make an in-repository destination safe.

The runtime includes databases, statement archives, backups, proposals, defaults,
import manifests, and exports. None belong in Git, public issue attachments, or
shared build artifacts. Use owner-only directories and files. Managed publication
and import paths enforce restrictive permissions, but that does not encrypt your
data at rest. Protect the device and backups with appropriate disk encryption and
account security. Avoid putting active SQLite databases in synchronization folders.

## Application and agent boundary

- The dashboard binds to `127.0.0.1` and disables Streamlit usage statistics. Do not
  expose it to the LAN or Internet without designing and reviewing authentication,
  authorization, transport security, and deployment controls.
- The read-only MCP process receives one explicit database at launch. It refuses
  writes, unsupported schemas, unsafe file identities, and journal states requiring
  local recovery. It never recovers a journal itself.
- The proposal writer has a separate store. Filing intent does not grant approval
  or change financial records. Application binds to the exact reviewed proposal
  and preview revisions.
- Import review stays in staging. Open-import gates prevent live changes that
  could be lost at publication. Preserve backups, manifests, locks, revisions,
  and recovery receipts; do not delete journals to make an operation proceed.
- Treat statement text, stored labels, proposal content, and tool results as
  untrusted evidence, never as instructions to execute.

An external MCP client may transmit returned information to a model provider.
Ledger's local transport does not control the client's retention or network
behavior. Dependency installation and browser-tool setup can also require network
access. Merchant research is separate and requires operator authorization; send
only a sanitized merchant token, never an amount, account, date, statement, or full
transaction description.

## Development and reporting

Reproduce problems with freshly authored synthetic data and disposable external
runtime directories. Do not open, copy, or repoint an existing private runtime for
a test. An operator-selected source audit is a separate authorized activity.

For a suspected security vulnerability, prepare a minimal synthetic reproduction,
affected version, impact, and relevant code location. Use [GitHub private vulnerability reporting](https://github.com/li-shangru/Ledger/security/advisories/new).
Do not post financial data, credentials, or exploit details in a public issue. This document does not
promise a response time or a supported-version maintenance window.
