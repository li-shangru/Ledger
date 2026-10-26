# Third-party notices

Ledger-authored source is licensed under the [MIT License](../LICENSE), copyright
2026 Shangru Li. Third-party packages retain their own licenses. Installing a
dependency does not relicense it under Ledger's MIT terms.

The declared dependencies and optional extras are in
[pyproject.toml](../pyproject.toml). [requirements.lock](../requirements.lock) records
the canonical macOS 26 arm64 development environment. Consult each installed
distribution's license and notice files for its terms; this document is not a
complete license inventory of every platform's resolved dependency graph.

## Verified runtime wheels

The source tree includes a reproducible builder and manifest, rather than bundled
runtime wheels. Its [manifest](../vendor/downstream-wheels/manifest.toml) records the
upstream artifacts and permitted changes.

| Project | Use | License and treatment |
| --- | --- | --- |
| Streamlit 1.66.1 | Local dashboard | Apache-2.0; the builder preserves the official wheel byte for byte. |
| Playwright 1.62.0 | Development and UI verification | Apache-2.0; the local `1.62.0+ledger.1` derivative changes version, dependency-range, and platform-tag metadata. It preserves upstream payload, licenses, notices, and executable modes. |

An [Apache License 2.0 copy](../vendor/downstream-wheels/LICENSE-APACHE-2.0) accompanies
the builder. The Playwright derivative is not an upstream release. Its platform
tag targets macOS 26 arm64, and it must not be represented as a portable wheel or
uploaded as an official upstream artifact. Preserve upstream license and notice
files if redistributing dependency artifacts.

Poppler's `pdftotext` is an external PDF extraction prerequisite. Ledger does not
bundle that executable; obtain it through your platform and retain the terms of
that distribution. Browser binaries installed for UI verification are separate
dependencies with their own license notices.
