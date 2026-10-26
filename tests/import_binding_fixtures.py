"""Real interpretation receipts for hand-authored synthetic import states."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ledger.adapters.provenance import bind_import_inputs, file_binding
from ledger.imports.pdf.paths import Paths


def bound_import_state(state: dict[str, Any], paths: Paths) -> dict[str, Any]:
    """Bind fixture inputs once, before a test introduces its intended failure.

    This uses the same source, configuration and core/adapter hashing as staging.
    It does not replace verification, renew pins after mutations, or touch the
    target database. Missing optional configuration is explicitly bound as absent.
    """
    csv_root = Path(state["csv_root"])
    manifest = Path(state.get("extraction_manifest", csv_root.parent / "extraction-manifest.json"))
    if not manifest.exists():
        manifest.write_text(json.dumps(state.get("pdfs", [])), encoding="utf-8")
    artifacts = {str(manifest): file_binding(manifest)}
    for item in state.get("pdfs", []):
        if item.get("original_pdf"):
            path = Path(item["original_pdf"])
            artifacts[str(path)] = file_binding(path)
    return state | {
        "extraction_manifest": str(manifest),
        "import_binding": bind_import_inputs(
            csv_root=csv_root,
            source_files=[Path(path) for path in state.get("approved_csv_paths", [])],
            account_slugs=state.get("account_slugs", {}),
            config_paths=[paths.ledger_config, paths.merchant_rules, paths.categories],
            artifacts=artifacts,
        ),
    }
