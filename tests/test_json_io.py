"""Import-boundary contracts for the canonical JSON utility leaf."""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_json_io_import_does_not_load_pdf_import_modules() -> None:
    program = textwrap.dedent(
        f"""
        import sys

        sys.path.insert(0, {str(REPO_ROOT / "src")!r})
        import ledger.utils.json_io  # noqa: F401

        loaded = sorted(
            name
            for name in sys.modules
            if name == "ledger.imports.pdf" or name.startswith("ledger.imports.pdf.")
        )
        if loaded:
            raise SystemExit(f"canonical JSON utility loaded PDF modules: {{loaded}}")
        """
    )

    result = subprocess.run(
        [sys.executable, "-c", program],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr or result.stdout


def test_pdf_json_utils_are_exact_compatibility_reexports() -> None:
    import ledger.utils.json_io as canonical
    from ledger.imports.pdf import json_utils as compatibility

    assert compatibility.utc_now is canonical.utc_now
    assert compatibility.decimal_json is canonical.decimal_json
    assert compatibility.write_json_atomic is canonical.write_json_atomic
