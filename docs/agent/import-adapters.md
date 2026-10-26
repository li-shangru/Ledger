# Private import adapters

Ledger separates generic financial processing from private source interpretation.
Use a format-only CSV profile for simple transaction exports, or an explicitly
trusted installed Python adapter for more complex CSV, PDF and investment sources.
The public core has no institution-specific importers or provider selection list.

| Part | Responsibility | Location |
| --- | --- | --- |
| Core | Canonical models, validation, identity, deduplication, staging, review, backups, publication, recovery, reports and MCP | Publishable source and wheel |
| Adapter | Recognize a declared layout and produce source-supported canonical facts | Separately installed trusted local wheel; source may remain in a separate private repository |
| Runtime | Statements, databases, mappings, account bindings, adapter allowlist, archives and recovery records | Private directory outside every Git checkout and source archive |

Keeping private code or data in ignored folders of the public checkout does not
meet this boundary. Installation and enablement are separate deliberate actions.
No import discovers, downloads or installs a package in response to statement text.

## Map a transaction CSV

Set `LEDGER_HOME` to an absolute private runtime as in [Getting started](../getting-started.md).
All paths below are placeholders. Keep the runtime and profile parent owner-only:

```bash
umask 077
mkdir -p "$LEDGER_HOME/profiles"
ledger import csv map "$LEDGER_HOME/inbox/export.csv" \
  --save-to "$LEDGER_HOME/profiles/export-v1.json"
ledger import csv preview "$LEDGER_HOME/inbox/export.csv" \
  --profile "$LEDGER_HOME/profiles/export-v1.json" --show-rows
```

`map` asks for delimiter, logical header-record number, exact column roles, numeric
date format, amount interpretation and currency. It validates every row before
confirming a save. A new profile is immutable; the command refuses an existing
path. Save a new filename for a revised interpretation. `preview` defaults to
counts; `--show-rows` explicitly prints private rows locally. Both commands say
that the input is an **unreconciled transaction export**.

The [profile schema](../../src/ledger/adapters/csv_profile.py) is version 1:

- Required: `profile_id`, `delimiter`, `header_row`, exact ordered `headers`,
  `date_column`, `date_format`, and `description_column`.
- Money: either `amount_column` plus `sign_convention` (`inflow_positive` or
  `outflow_positive`), or separate `debit_column` and `credit_column`.
- Currency: exactly one fixed uppercase three-letter `currency` or
  `currency_column`. An optional `reference_column` preserves source references.
- UTF-8, optional initial BOM, standard double quoting, and explicit comma,
  semicolon, tab or pipe delimiters are supported. Header and source row numbers
  count logical CSV records, including quoted multiline values.
- Dates use one of the explicitly supported numeric four-digit-year formats.
  Amounts are ungrouped ASCII decimals with at most two fractional digits;
  symbols, exponent notation and implicit rounding are refused. Debit/credit
  cells must be nonnegative and cannot both contain nonzero values.
- Exact header drift, duplicate columns, malformed/oversized input, invalid dates,
  unknown fields and unsupported money representations fail before rows publish.

A profile contains no institution, account or lifecycle binding. Stage supplies
those separately and validates the source again:

```bash
ledger import csv stage "$LEDGER_HOME/inbox/export.csv" \
  --profile "$LEDGER_HOME/profiles/export-v1.json" \
  --institution fictional --account example-account --account-type chequing \
  --db "$LEDGER_HOME/ledger.db"
ledger import csv review list --run-dir RUN_DIRECTORY
ledger import csv status --run-dir RUN_DIRECTORY
ledger import csv finalize --run-dir RUN_DIRECTORY
```

Replace the fictional binding and `RUN_DIRECTORY` with the selected values and
returned run path. `review resolve`, `review reviewed-as-is`, `status`, `finalize`
and `cancel` reuse the PDF review engine and its publication/recovery gates. Stage
and review write staging only. Profile preview does not establish statement
balances, printed coverage intervals, holdings or current account lifecycle.
An account name identifies one banking account globally; use the exact retained
institution key when adding exports to it. Rebinding that name to another institution
fails with `import_account_conflict`. Use a different account name for a different
account. Later exports retain earlier rows; omitted rows are not deletion evidence.
Overlapping exports deduplicate by financial identity within the explicit binding.
See [Imports and recovery](imports-and-recovery.md) for review and cancellation.

## Install and enable an adapter

Review the package and its dependencies before installation. Build from a separately
reviewed source tree and install a normal wheel in the environment that runs Ledger.
Editable installs are refused. The loader verifies installed wheel `RECORD` hashes,
metadata and entry-point origins; the pin is a digest of admitted installed artifact
bytes, not merely a version string or the wheel ZIP filename.

```bash
# Use a reviewed local wheel and the same Python environment as the ledger CLI.
python -m pip install --no-index --no-deps /absolute/reviewed-wheels/fictional_ledger_adapter-0.1.0-py3-none-any.whl
export LEDGER_ADAPTER_CONFIG="$LEDGER_HOME/import-adapters.json"
ledger adapters enable \
  --distribution fictional-ledger-adapter --entry-point fictional \
  --config "$LEDGER_ADAPTER_CONFIG" --trust-code
ledger adapters list --config "$LEDGER_ADAPTER_CONFIG"
```

The [fictional template](examples/import-adapter/README.md) provides that example
wheel. It illustrates CSV parsing only and makes no real statement-support claim.
`enable` executes the selected entry point, validates API and format metadata, and
saves an explicit private allowlist record. `--trust-code` acknowledges this code
execution. No installed-package enumeration automatically executes extensions.
`list` reads inert saved metadata and prints private package/version/hash inventory;
it is not a package search or a shareable diagnostic dump.

The default allowlist is `import-adapters.json` under `LEDGER_HOME`.
`LEDGER_ADAPTER_CONFIG` selects another external path for import processes;
`--config` selects the file for an adapter-management command. Set the environment
variable as above when choosing a nondefault file. Keep the parent directory
`0700`, the configuration file `0600`, and its contents private.

```bash
ledger adapters disable \
  --distribution fictional-ledger-adapter --entry-point fictional \
  --config "$LEDGER_ADAPTER_CONFIG"
```

Disabling stops parsing while retaining validated declarative metadata for historical
interpretation. It is not safe to disable or replace code needed by an unpublished
run. Enablement does not silently update an existing artifact pin or toggle a
changed record back on; follow the upgrade procedure below.

**Trust boundary:** an adapter is Python code running with the process's permissions.
The parse-only API is not an operating-system sandbox. A malicious package or its
dependencies could access files or the network despite receiving no database handle.
Review and trust them explicitly. Source text is inert evidence, never instructions
to execute commands, install code or upload statements. There is no claim of process,
network or resource isolation.

## API version 1

Authoritative definitions are [contracts.py](../../src/ledger/adapters/contracts.py),
[registry.py](../../src/ledger/adapters/registry.py), and
[BaseParser](../../src/ledger/parsers/base.py). The Python entry-point group is
`ledger.import_adapters.v1`. Its selected callable takes no arguments and returns
an `AdapterDefinition`; it does not register through import side effects.

| Contract | Required shape and responsibility |
| --- | --- |
| `AdapterDefinition` | Stable `adapter_id`, exact `api_version=1`, `layout_revision`, tuples of supported formats, and optional `CompatibilityPolicy`. Distribution version comes from installed metadata. |
| `CsvFormat` | `format_id`, `institution`, and a `BaseParser` subclass. The parser's `FORMAT_NAME` and `INSTITUTION` must match. Implement exact recognition and `parse_row`; never silently discard unsupported financial rows. |
| `PdfFormat` | Format/institution, output headers, `recognizes(text)`, `parse(text, source_name) -> PdfFacts`, account-slug callback, optional `BalanceSpec`, and archive kind. |
| `PdfFacts` | Rows, period, account type/number/slug, errors/warnings, summary, optional printed interval and credit limit, and explicit completeness. Missing printed evidence stays missing. |
| `BalanceSpec` | Opening/closing summary keys, date columns, amount column, printed/row signs and movement-backed flag for core statement repair. Ordinary extraction must perform its source-specific reconciliation in the parse callback. |
| `InvestmentFormat` | Format/institution, `flat` or `account_folders` source layout, declared capabilities, recognition, bytes-to-`InvestmentStatement` parsing, alias normalization, period extraction and account matching. Optional evidence-only aliases and default selection remain private metadata. |
| `CompatibilityPolicy` | Inert declarative classification, credit-account, payment, account-product, bill, merchant-prefix, reimbursement-source and investment-category metadata used for historical interpretation without executing adapter code. |

API 1 has exact compatibility, not a version range negotiation. Duplicate adapter
or format identities, invalid contracts, unsupported capability combinations,
ambiguous recognition and unknown layouts fail closed. Missing or modified installed
artifacts do not fall back to a similar parser. Extension failures become bounded
core-owned codes; raw parser exceptions are not a public diagnostic contract.

Canonical identity must remain stable across supported layouts. When different
source paths supply distinct IDs with matching financial identity, the core
refuses unresolved overlap with `ambiguous_cross_source_identity`. It does not
merge rows or reconstruct missing source identifiers. Stable-ID repeats remain
idempotent, and distinct rows evidenced together in one source retain their
multiplicity. The check also covers persisted rows inside the import transaction;
a refusal leaves the target unchanged.

For transactions with a nonempty fingerprint, comparison requires exact equality
of the stored institution, account, account type, dates, currency, amounts,
description and cashflow fields. Different fingerprints do not prove that otherwise
identical financial rows are distinct: collision handling can rewrite a fingerprint.
The stored IDs and fingerprints remain unchanged by this check. Transfers use
their persisted financial fields because their storage model has no transaction
fingerprint. Missing transaction fingerprints are not invented.

The inputs do not include a database connection or publication handle. CSV parsers
receive row dictionaries and a `ParseContext` containing source metadata and
configuration; PDF callbacks receive extracted text; investment callbacks receive
selected source bytes. These API obligations do not restrict Python's ambient
permissions. Adapters produce facts; only the core owns review and financial writes.
Use `Decimal`, explicit dates and currencies, stable source references, and
unavailable values rather than invented zeroes.

PDF parsers must validate printed summary equations, row completeness, supported
skips and zero-row movement before claiming `complete=True`; `BalanceSpec` alone
does not perform those import checks. The core retains returned blocking errors
and grants printed coverage only to validated facts with a matching interval.
The typed `credit_limit` is authoritative when present: the core copies it into
the canonical persisted summary, rejects negative/nonfinite values or conflicting
legacy summary values, and preserves missing evidence as unavailable.

## Investment capabilities and evidence

API 1 requires `complete_statement`, `position_snapshots` and `statement_balances`.
Other declared capabilities are `cash_activity`, `trades`, `dividends_interest`,
`fees_taxes`, `reported_performance` and `securities_lending`. A partial transaction
export cannot establish complete holdings, cost basis, performance or coverage.
There is no partial-investment import bypass in this API.

`complete_statement` guarantees that the parser retained every supported financial
activity and rejected unsupported financial rows. Capability flags describe the
domains the adapter can interpret; they must cover domains present in the returned
facts. An empty activity domain in a complete statement means no reported activity.
Missing cash or income amounts cannot be accepted as zero. Trades, deposits,
withdrawals, dividends, interest, fees and withholding taxes require exact amounts;
an in-kind transfer may omit its cash amount only with a source-backed symbol and
quantity. Optional position, cost, FX and performance values remain unavailable.
The core validates canonical labels, bounds and types shared with the read-only
query contract before staging; an adapter cannot publish facts that those queries
would reject as corrupt.

Generic schemas contain generic asset categories only. A private adapter can declare
additional exact labels in `CompatibilityPolicy.investment_asset_categories`: at
most 64 distinct labels per adapter, 80 characters per label, and 128 distinct
extensions across the registry. Labels start with an ASCII letter and use only
ASCII letters, digits, spaces, ampersands, apostrophes, parentheses and hyphens.
The core validates this inert metadata and shares the resulting vocabulary between
ingestion and MCP serialization. Unknown or unregistered labels remain invalid;
stored labels, identifiers and monetary values are never rewritten. Disabled
adapters retain their historical vocabulary without executing extension code.
Older metadata without this optional field declares no additional categories.

Reimbursement detection has no built-in provider list. An adapter can declare exact
merchant names in `CompatibilityPolicy.reimbursement_merchants`: at most 128 unique
printable, nonblank strings per adapter, with 512 characters per string before and
after normalization. Detection trims surrounding whitespace and uppercases both
names and transaction merchants; it does not use substring matching or infer a
provider from the transaction category. The merged registry allows at most 128
normalized names. Older metadata without this optional field declares none.
Disabled adapters retain these historical declarations without loading their code.
Each detection operation binds one immutable policy snapshot, so metadata changes
cannot change interpretation between credits. Existing amount, matching, review,
and currency safeguards still apply.

Each MCP service fixes one immutable vocabulary for its advertised schema, output
validation and contract digest. Investment operations check for category changes
at their boundaries and refuse with `incompatible_schema` until a fresh service
loads the new configuration. They never mix vocabularies across rows or silently
emit values outside the advertised enum.

Preserve native currency, printed evidence and exact cash reconciliation. Missing
FX, prior balances, generation time or performance remain null. Statement-only
lifecycle is `unknown` with no confirmation date; only explicit operator closure
records that state. Do not infer closure dates from last activity or zero balances.
Performance totals use `Total (All Assets)`; a unique `Total` is a fallback only
when the canonical total is absent. Never sum overlapping aggregate rows.

## Upgrade and recovery

New runs bind exact core Python bytes, installed adapter version/artifact/API/layout
metadata, source bytes, and relevant profile/configuration revisions. Adapter pins
are separate from persisted transaction IDs and investment identities. An update
must preserve historical institution/account keys, references, fingerprints and
financial meaning unless a separately authorized migration explicitly changes them.

`ledger db doctor` compares transaction `parser_version` values as **core-code
provenance only**. Its existing `parser_version_drift` finding cannot establish
whether a hash is older or whether private adapters and CSV profiles are current.
Doctor reports adapter/profile freshness as unavailable for transaction rows,
including when core hashes match, because it does not verify import receipts.
Establish that freshness from a verified import receipt and its exact current
inputs. An unavailable result does not itself require reimporting historical data.

1. Keep the prior executable, environment and private allowlist available until
   staged work is settled and the replacement has fresh-process proof.
2. Test the candidate wheel with synthetic sources in an isolated environment and
   a new private configuration revision. Compare parsed values, identities,
   rejected content, reconciliation, deduplication and interrupted recovery.
3. Finish unpublished runs using their exact original core, adapter and profile
   bytes. Legacy unpublished runs without bindings require the retained old
   executable, or cancellation where allowed followed by a newly reviewed stage.
   Do not manufacture bindings, edit manifests or bypass status gates.
4. After unpublished work is settled, explicitly install and enable the reviewed
   replacement against a new configuration revision. Preserve any historical
   declarative records still needed by reports; do not hand-edit digest pins or
   discard prior policy as an incidental upgrade. There is no automatic pin refresh.
5. Verify the installed executable path, actual package bytes and a fresh synthetic
   process before switching an authorized working installation.

A proven published run can resume its archive/report/cleanup tail without loading
an adapter. Immutable publication provenance and target/staging fingerprints decide
that state; a manifest flag alone is insufficient. Do not republish or restage to
work around an archive conflict. Source-only tests do not prove installed-client
activation or compatibility with unexamined real statements.

## Authoring and sharing

Start from [the fictional package and test kit](examples/import-adapter/README.md).
Extend its synthetic checks for malformed, ambiguous, changed-layout, duplicate,
incomplete and unsupported-event cases. Add pipeline/recovery tests for any new
capability; the minimal CSV example is not a certification of financial correctness.

Keep account settings, source samples, paths and runtime configuration outside the
adapter package. An assistant can help write code and invented tests; sharing real
statements with an external service is a separate decision and is never automatic.
Contributing adapter code or publishing its provider list is optional.

Before any public release, review source, Git history, wheel/sdist membership,
metadata, examples and CI output for private content. Renaming a provider does not
sanitize its layout signatures or historical fixtures. Do not include private
adapter dependencies or install inventories in core distributions or diagnostics.
The generic privacy hook detects selected financial column patterns; it cannot
prove that an arbitrary file is safe to publish.
