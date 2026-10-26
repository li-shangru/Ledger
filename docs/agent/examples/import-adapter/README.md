# Fictional adapter template and starter test kit

This is a buildable API 1 package, invented independently for documentation. It
supports one five-column transaction CSV: `Day,Memo,Delta,Unit,Receipt`. It supports
no real institution, PDF, investment statement, balance or coverage inference.
`Delta` is an ungrouped signed decimal with at most two fractional digits;
positive means inflow. `Day` is exact ISO date, `Unit` is uppercase currency, and
`Receipt` is an optional stable source reference.

The package has no import-time registration. Its `fictional` entry point returns
an `AdapterDefinition` only when selected. Extend the contract tests before
changing its interpretation. Tests check exact amounts/currencies, stable IDs,
content collisions, unknown headers and unsupported rows. They are a starter kit,
not proof of full pipeline, deduplication, reconciliation or recovery behavior.

## Build and test outside the source checkout

Set `ADAPTER_WORK` to a new absolute disposable output directory selected under
your machine's storage policy. Install Ledger and the project's development tools
in a test environment first. Do not use an owner runtime, real statements or a
configured client environment. From the Ledger source root:

```bash
: "${ADAPTER_WORK:?Set a new external disposable directory}"
umask 077
mkdir -p "$ADAPTER_WORK/source" "$ADAPTER_WORK/wheels" "$ADAPTER_WORK/runtime"
cp -R docs/agent/examples/import-adapter/. "$ADAPTER_WORK/source/"
python -m pip wheel --no-build-isolation --no-deps \
  --wheel-dir "$ADAPTER_WORK/wheels" "$ADAPTER_WORK/source"
python -m pip install --no-index --no-deps \
  "$ADAPTER_WORK/wheels/fictional_ledger_adapter-0.1.0-py3-none-any.whl"
PYTHONDONTWRITEBYTECODE=1 python -m pytest \
  "$ADAPTER_WORK/source/tests" --basetemp "$ADAPTER_WORK/test-tmp"
```

Use a fresh process to prove installed enablement without pre-importing the
package under test. The config parent exists with mode `0700` from the recipe:

```bash
export LEDGER_HOME="$ADAPTER_WORK/runtime"
export LEDGER_ADAPTER_CONFIG="$LEDGER_HOME/import-adapters.json"
ledger adapters enable --distribution fictional-ledger-adapter \
  --entry-point fictional --config "$LEDGER_ADAPTER_CONFIG" --trust-code
ledger adapters list --config "$LEDGER_ADAPTER_CONFIG"
python -c 'from ledger.adapters.registry import load_adapters; assert len(load_adapters()) == 1'
ledger adapters disable --distribution fictional-ledger-adapter \
  --entry-point fictional --config "$LEDGER_ADAPTER_CONFIG"
python -c 'from ledger.adapters.registry import load_adapters; assert load_adapters() == ()'
```

The enabled package executes trusted Python code with process permissions. Review
it before granting `--trust-code`; the interface is not a sandbox. Installed
`RECORD` hashes and admitted file origins must match the private allowlist pin.
Editable installs and same-version code edits are refused.

For a real adapter, keep its source in a separate repository and keep source
statements, account bindings, profiles and allowlists in an external private
runtime. Change the fictional identifiers deliberately; never copy real exports
into this example. Add synthetic integration tests for each capability you add,
including ambiguous recognition, missing evidence, source drift, disabled or
modified packages and interrupted publication. See [the API guide](../../import-adapters.md).
