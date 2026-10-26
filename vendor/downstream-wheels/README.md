# Ledger verified runtime wheels

Ledger uses the unmodified official `streamlit==1.66.1` wheel and the local
`playwright==1.62.0+ledger.1` derivative. Official Streamlit supports Pillow 13 and
Ledger's current Arrow, Starlette and WebSocket versions. Existing named artifacts
are never replaced with different bytes.

`manifest.toml` pins official artifact URLs and SHA-256 hashes, the permitted
Playwright metadata changes and its deterministic output hash. Run
`./scripts/build-downstream-wheels /absolute/ledger-build/wheels` to fetch and verify official Streamlit unchanged
and reproduce Playwright. The script rejects conflicting existing output files before
starting a build. It retains exact existing files and writes outputs to the selected
external directory. Set `LEDGER_WHEEL_DIR` to reuse that directory in repository wrappers.

The Playwright build uses Python 3.14, `wheel==0.48.0`, its pinned
`packaging==26.3` dependency and `SOURCE_DATE_EPOCH=1700000000`. Only its local
version, pyee range and WHEEL/filename platform tag change. The native Node
executable is checked with Apple's `vtool` for its macOS 13.5 minimum before the
wheel declares Ledger's narrower macOS 26 arm64 target. Every other payload byte,
license, notice and executable mode is preserved. RECORD and dist-info names
change as required by repacking. Its existing deterministic output hash is unchanged.

The local Playwright artifact is a modified distribution, not an upstream
release. Do not upload it to a package index under the upstream project name. Streamlit is verified byte-for-byte against its official
wheel and is never unpacked or repacked by this workflow. Both projects use
Apache-2.0; the accompanying license remains available alongside the builder.

UI and full-development readiness require the installed wheels' exact
`direct_url.json` archive hashes. The official Streamlit artifact therefore retains
an explicit provenance check as well as version and dependency checks. A missing
or substituted provenance record triggers the documented no-index wheel install.

The dependency audit scans public Streamlit normally. Its only expected public-index
skips are the unpublished local Ledger version and local Playwright derivative; it separately audits the exact
upstream `playwright==1.62.0` parent without skips. No advisory waiver or new skip is
introduced. See [development](../../docs/agent/development.md) for lock generation
and [delivery](../../docs/agent/delivery.md) for full synthetic UI and fresh-process proof.
