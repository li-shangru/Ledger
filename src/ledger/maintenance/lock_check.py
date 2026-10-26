"""Consistency gate: requirements.lock must cover and satisfy pyproject constraints.

`requirements.lock` is a full-environment freeze (`pip freeze`). Whenever a pinned
version drifts outside the bounded range that `pyproject.toml` declares for the same
package, a reproducible install from the lock silently diverges from what pyproject
promises (e.g. `rich==15.0.0` under a `rich>=13.0,<15` constraint). This module parses
both files and reports every applicable missing dependency or lock pin that violates
its pyproject specifier so the `lock-consistency` freeze-check gate can fail loudly.

Runnable as a module: ``python -m ledger.maintenance.lock_check`` exits non-zero and
prints the offending pins on any violation.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

REPO_ROOT = Path(__file__).resolve().parents[3]
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
LOCK_PATH = REPO_ROOT / "requirements.lock"


def _read_pyproject_deps(path: Path = PYPROJECT_PATH) -> list[str]:
    """Return every declared requirement string: [project].dependencies AND every
    [project.optional-dependencies] extra.

    The extras are included so an out-of-range pin for an upper-bounded extras package
    (e.g. streamlit<2, plotly<7) is range-checked too -- a lock pin can violate an
    extras constraint just as easily as a core one, and the freeze gate must catch it.
    """
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    project = data.get("project", {})
    deps: list[str] = list(project.get("dependencies", []))
    for extra_deps in project.get("optional-dependencies", {}).values():
        deps.extend(extra_deps)
    return deps


def _read_lock_pins(path: Path = LOCK_PATH) -> dict[str, str]:
    """Parse ``name==version`` lines from requirements.lock into {canonical_name: version}."""
    pins: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "==" not in line:
            continue
        name, _, version = line.partition("==")
        # Drop any environment markers / extras trailing the version (e.g. "; python_version").
        version = version.split(";", 1)[0].strip()
        name = name.strip()
        if not name or not version:
            continue
        pins[_canonical(name)] = version
    return pins


def _canonical(name: str) -> str:
    """Normalize a distribution name for case/separator-insensitive comparison."""
    return str(canonicalize_name(name))


def check_lock_against_pyproject(
    pyproject_deps: list[str] | None = None,
    lock_pins: dict[str, str] | None = None,
) -> list[dict[str, str]]:
    """Return the lock pins that violate their pyproject dependency specifier.

    When ``pyproject_deps`` / ``lock_pins`` are None the repo's real files are read.
    Each violation is ``{"name", "pin", "specifier"}``. Applicable pyproject
    dependencies without a matching lock pin are reported with ``pin="<missing>"``.
    """
    if pyproject_deps is None:
        pyproject_deps = _read_pyproject_deps()
    if lock_pins is None:
        lock_pins = _read_lock_pins()

    canonical_pins = {_canonical(name): version for name, version in lock_pins.items()}

    violations: list[dict[str, str]] = []
    for dep in pyproject_deps:
        try:
            req = Requirement(dep)
        except Exception:
            # A malformed dep string should not crash the gate.
            continue
        if req.marker is not None and not req.marker.evaluate():
            continue
        pin = canonical_pins.get(_canonical(req.name))
        if pin is None:
            violations.append(
                {
                    "name": req.name,
                    "pin": "<missing>",
                    "specifier": str(req.specifier),
                }
            )
            continue
        if not req.specifier:
            continue
        try:
            pinned = Version(pin)
        except InvalidVersion:
            continue
        # prereleases=True so a pinned rc/beta is still range-checked.
        if not req.specifier.contains(pinned, prereleases=True):
            violations.append(
                {
                    "name": req.name,
                    "pin": pin,
                    "specifier": str(req.specifier),
                }
            )
    return violations


def main() -> int:
    violations = check_lock_against_pyproject()
    if violations:
        print("lock/pyproject drift: declared dependencies are missing or out of range:")
        for v in violations:
            if v["pin"] == "<missing>":
                print(
                    f"  {v['name']} is missing from requirements.lock ({v['specifier'] or 'any'})"
                )
            else:
                print(f"  {v['name']}=={v['pin']} violates '{v['specifier']}'")
        print(
            "Regenerate the lock with every applicable declaration at a version inside "
            "the pyproject range (or change the declaration intentionally)."
        )
        return 1
    print("lock-consistency: all lock pins satisfy pyproject constraints.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
