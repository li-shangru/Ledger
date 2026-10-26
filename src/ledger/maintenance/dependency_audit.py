"""Audit only canonical public lock pins and reviewed upstream derivative parents.

Never enumerate installed packages for a network audit: private adapters are
validated locally by environment_check and must not disclose their identities to
a package registry or vulnerability service.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from packaging.utils import canonicalize_name

from ledger.maintenance.lock_check import LOCK_PATH, _read_lock_pins

LOCAL_DERIVATIVE_VERSIONS = {"playwright": "1.62.0+ledger.1"}
UPSTREAM_PARENT_VERSIONS = {
    "playwright": "1.62.0",
}
REPO_ROOT = Path(__file__).resolve().parents[3]
UPSTREAM_REQUIREMENTS = (
    REPO_ROOT / "vendor" / "downstream-wheels" / "upstream-audit-requirements.txt"
)


class DependencyAuditError(RuntimeError):
    """The vulnerability evidence was incomplete or reported a vulnerability."""


def _dependency_rows(payload: object) -> list[Mapping[str, object]]:
    if not isinstance(payload, dict):
        raise DependencyAuditError("audit payload is not an object")
    dependencies = payload.get("dependencies")
    if not isinstance(dependencies, list):
        raise DependencyAuditError("audit payload has no dependency list")
    rows: list[Mapping[str, object]] = []
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            raise DependencyAuditError("audit dependency row is not an object")
        rows.append(dependency)
    return rows


def validate_audit_payload(
    payload: object,
    *,
    expected_skips: frozenset[str],
    expected_versions: Mapping[str, str] | None = None,
) -> None:
    """Require clean findings plus the exact intended skip/version evidence."""

    rows = _dependency_rows(payload)
    skipped: set[str] = set()
    seen: set[str] = set()
    versions: dict[str, str] = {}
    vulnerable: set[str] = set()
    for row in rows:
        raw_name = row.get("name")
        if not isinstance(raw_name, str):
            raise DependencyAuditError("audit dependency has no name")
        name = str(canonicalize_name(raw_name))
        if name in seen:
            raise DependencyAuditError(f"audit dependency is duplicated: {name}")
        seen.add(name)
        if "skip_reason" in row:
            skipped.add(name)
            continue
        raw_version = row.get("version")
        vulnerabilities = row.get("vulns")
        if not isinstance(raw_version, str) or not isinstance(vulnerabilities, list):
            raise DependencyAuditError(f"audit dependency evidence is incomplete: {name}")
        versions[name] = raw_version
        if vulnerabilities:
            vulnerable.add(name)

    if skipped != expected_skips:
        raise DependencyAuditError(
            f"unexpected audit skips: expected={sorted(expected_skips)}, actual={sorted(skipped)}"
        )
    if vulnerable:
        raise DependencyAuditError(f"vulnerabilities reported for: {sorted(vulnerable)}")
    if expected_versions is not None:
        canonical_expected = {
            str(canonicalize_name(name)): version for name, version in expected_versions.items()
        }
        if versions != canonical_expected:
            raise DependencyAuditError(
                f"dependency evidence mismatch: expected={canonical_expected}, actual={versions}"
            )


def _run_pip_audit(arguments: Sequence[str]) -> object:
    completed = subprocess.run(
        [sys.executable, "-m", "pip_audit", *arguments],
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )
    if completed.returncode != 0:
        raise DependencyAuditError("pip-audit reported a vulnerability or collection failure")
    try:
        return json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise DependencyAuditError("pip-audit returned invalid JSON") from exc


def main() -> int:
    """Audit the exact public lock closure without consulting installed identities."""

    try:
        versions = public_audit_versions()
        with tempfile.TemporaryDirectory(prefix="ledger-public-dependency-audit-") as directory:
            requirements = Path(directory) / "requirements.txt"
            requirements.write_text(
                "".join(f"{name}=={version}\n" for name, version in sorted(versions.items())),
                encoding="utf-8",
            )
            requirements.chmod(0o600)
            payload = _run_pip_audit(
                [
                    "--requirement",
                    str(requirements),
                    "--no-deps",
                    "--disable-pip",
                    "--strict",
                    "--format",
                    "json",
                    "--progress-spinner",
                    "off",
                ]
            )
        validate_audit_payload(
            payload,
            expected_skips=frozenset(),
            expected_versions=versions,
        )
    except DependencyAuditError as exc:
        print(f"dependency-audit: failed: {exc}", file=sys.stderr)
        return 1

    print(
        "dependency-audit: exact public core lock and upstream derivative parents clean; "
        "private installed package identities were not submitted."
    )
    return 0


def public_audit_versions() -> dict[str, str]:
    """Keep every public lock pin and replace only the reviewed local derivatives."""

    versions = _read_lock_pins(LOCK_PATH)
    if not versions or _read_lock_pins(UPSTREAM_REQUIREMENTS) != UPSTREAM_PARENT_VERSIONS:
        raise DependencyAuditError("canonical public audit inputs changed")
    if set(LOCAL_DERIVATIVE_VERSIONS) != set(UPSTREAM_PARENT_VERSIONS) or any(
        versions.get(name) != version for name, version in LOCAL_DERIVATIVE_VERSIONS.items()
    ):
        raise DependencyAuditError("local derivative lock pins changed without upstream review")
    return {**versions, **UPSTREAM_PARENT_VERSIONS}


if __name__ == "__main__":
    raise SystemExit(main())
