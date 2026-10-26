"""Current documentation must not leak real absolute home-directory paths.

Scan all shipped Markdown documentation, including release records. Candidate
source and artifacts have separate distribution-wide privacy checks.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Matches a real home-directory path like /Users/example/... or /home/alice/...
# (but not a bare "/Users/" or "/home/" with no account segment).
_HOME_PATH_RE = re.compile(r"/(?:Users|home)/[^/\s`]+/")

_SCANNED_DOCS = (
    REPO_ROOT / "README.md",
    REPO_ROOT / "AGENTS.md",
    *sorted((REPO_ROOT / "docs").rglob("*.md")),
)


def test_docs_carry_no_real_home_paths():
    offenders: list[str] = []
    for doc in _SCANNED_DOCS:
        assert doc.exists(), f"expected tracked doc to exist: {doc}"
        text = doc.read_text(encoding="utf-8")
        for match in _HOME_PATH_RE.finditer(text):
            offenders.append(f"{doc.relative_to(REPO_ROOT)}: {match.group(0)!r}")
    assert not offenders, "real home path(s) leaked in tracked docs:\n" + "\n".join(offenders)
