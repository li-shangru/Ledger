"""Exact installed-environment comparison against ``requirements.lock``.

The lock is a local interpreter/platform freeze. This gate intentionally checks a
different contract from :mod:`ledger.maintenance.lock_check`: every non-editable
installed distribution must have the same canonical name and version as the lock,
with all missing, mismatched, and unexpected distributions reported together.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Protocol

from packaging.utils import canonicalize_name

from ledger.maintenance.lock_check import LOCK_PATH, _read_lock_pins

_AUDITED_RUNTIME_WHEELS = {
    "streamlit": "29607190a589fe1fa9bd7e154635d335d47dc09b4010c6cc273daa6e4430ee31",
    "playwright": "3c9c9e0ce4ece2ade7d611735327877a7a32234a2cc7b4ab5403768d66ade04a",
}


class _PackageMetadata(Protocol):
    def __getitem__(self, key: str) -> str: ...


class _Distribution(Protocol):
    @property
    def metadata(self) -> _PackageMetadata: ...

    @property
    def version(self) -> str: ...

    def read_text(self, filename: str) -> str | None: ...


@dataclass(frozen=True)
class EnvironmentDrift:
    """All exact differences between the lock and installed distributions."""

    missing: dict[str, str]
    mismatched: dict[str, tuple[str, str]]
    unexpected: dict[str, str]
    untrusted_local: dict[str, str] = field(default_factory=dict)
    adapter_error: str | None = None

    @property
    def ok(self) -> bool:
        return not (
            self.missing
            or self.mismatched
            or self.unexpected
            or self.untrusted_local
            or self.adapter_error
        )


def runtime_is_supported(version_info: tuple[int, ...] | None = None) -> bool:
    """Return whether the interpreter is on Ledger's supported Python 3.14 line."""

    selected = sys.version_info if version_info is None else version_info
    return (selected[0], selected[1]) == (3, 14)


def _canonical(name: str) -> str:
    return str(canonicalize_name(name))


def _is_editable(distribution: _Distribution) -> bool:
    direct_url = distribution.read_text("direct_url.json")
    if not direct_url:
        return False
    try:
        payload = json.loads(direct_url)
    except json.JSONDecodeError, TypeError:
        return False
    if not isinstance(payload, dict):
        return False
    directory_info = payload.get("dir_info")
    return isinstance(directory_info, dict) and directory_info.get("editable") is True


def installed_non_editable_distributions(
    distributions: Iterable[_Distribution] | None = None,
) -> dict[str, str]:
    """Return canonical installed names and versions, excluding editable projects."""

    selected = metadata.distributions() if distributions is None else distributions
    installed: dict[str, str] = {}
    for distribution in selected:
        if _is_editable(distribution):
            continue
        name = distribution.metadata["Name"]
        if not name:
            continue
        installed[_canonical(name)] = distribution.version
    return installed


def pinned_wheel_source_violations(
    distributions: Iterable[_Distribution],
) -> dict[str, str]:
    """Require the exact official Streamlit and private Playwright artifacts."""

    violations: dict[str, str] = {}
    for distribution in distributions:
        name = _canonical(distribution.metadata["Name"])
        expected = _AUDITED_RUNTIME_WHEELS.get(name)
        if expected is None:
            continue
        direct_url = distribution.read_text("direct_url.json")
        if not direct_url:
            violations[name] = "installed distribution has no direct wheel provenance"
            continue
        try:
            payload = json.loads(direct_url)
        except json.JSONDecodeError, TypeError:
            violations[name] = "installed distribution has invalid direct wheel provenance"
            continue
        archive = payload.get("archive_info") if isinstance(payload, dict) else None
        hashes = archive.get("hashes") if isinstance(archive, dict) else None
        actual = hashes.get("sha256") if isinstance(hashes, dict) else None
        if actual != expected:
            violations[name] = "installed archive hash is not the audited runtime wheel hash"
    return dict(sorted(violations.items()))


def compare_environment(
    *,
    lock_pins: Mapping[str, str],
    installed: Mapping[str, str],
) -> EnvironmentDrift:
    """Compare two name/version maps after PEP 503 name canonicalization."""

    canonical_lock = {_canonical(name): version for name, version in lock_pins.items()}
    canonical_installed = {_canonical(name): version for name, version in installed.items()}

    missing = {
        name: canonical_lock[name]
        for name in sorted(canonical_lock.keys() - canonical_installed.keys())
    }
    mismatched = {
        name: (canonical_lock[name], canonical_installed[name])
        for name in sorted(canonical_lock.keys() & canonical_installed.keys())
        if canonical_lock[name] != canonical_installed[name]
    }
    unexpected = {
        name: canonical_installed[name]
        for name in sorted(canonical_installed.keys() - canonical_lock.keys())
    }
    return EnvironmentDrift(missing, mismatched, unexpected)


def check_environment(
    distributions: Iterable[_Distribution] | None = None,
    *,
    allowed_adapter_config: Path | None = None,
) -> EnvironmentDrift:
    """Check the core lock plus explicitly selected, inertly verified adapter pins.

    No configuration default or installed entry-point discovery grants an allowance.
    Adapter dependencies must already belong to the core lock or independently be
    explicitly pinned adapters; allowing an adapter never allows arbitrary extras.
    """

    from ledger.adapters.contracts import AdapterError

    selected = list(metadata.distributions() if distributions is None else distributions)
    pins = _read_lock_pins(LOCK_PATH)
    adapter_error = None
    if allowed_adapter_config is not None:
        try:
            config = validated_adapter_configuration(allowed_adapter_config, selected)
            pins = {
                **pins,
                **{
                    record["distribution"]: record["version"]
                    for record in config["adapters"]
                    if record["enabled"]
                },
            }
        except AdapterError as exc:
            adapter_error = str(exc)
    drift = compare_environment(
        lock_pins=pins,
        installed=installed_non_editable_distributions(selected),
    )
    untrusted = pinned_wheel_source_violations(selected)
    for distribution in selected:
        name = _canonical(distribution.metadata["Name"])
        if _is_editable(distribution) and name != "ledger":
            untrusted[name] = "only the canonical Ledger project may be editable"
    return EnvironmentDrift(
        drift.missing,
        drift.mismatched,
        drift.unexpected,
        dict(sorted(untrusted.items())),
        adapter_error,
    )


def validated_adapter_configuration(
    path: Path,
    distributions: Iterable[_Distribution] | None = None,
) -> dict:
    """Validate one explicit private pin file without importing any adapter code."""

    from ledger.adapters import registry
    from ledger.adapters.contracts import AdapterError

    # Registry reads normally treat absence as no enabled adapters. An explicit
    # delivery allowance is an exact input, so an absent file must fail instead.
    if not path.is_file():
        raise AdapterError("adapter_unsafe_configuration")
    config = registry.read_configuration(path)
    selected = list(metadata.distributions() if distributions is None else distributions)
    core = {_canonical(name) for name in _read_lock_pins(LOCK_PATH)} | {"ledger"}
    for record in config["adapters"]:
        if not record["enabled"]:
            continue
        name = _canonical(record["distribution"])
        if name in core:
            raise AdapterError("adapter_incompatible")
        candidates = [d for d in selected if _canonical(d.metadata["Name"]) == name]
        if len(candidates) != 1:
            raise AdapterError("adapter_unavailable")
        distribution = candidates[0]
        if _is_editable(distribution) or distribution.version != record["version"]:
            raise AdapterError("adapter_incompatible")
        # All helpers below inspect installed bytes/metadata only. In particular,
        # never call enable_distribution, load_adapters, or EntryPoint.load here.
        actual, admitted = registry._artifact_inventory(distribution)  # type: ignore[arg-type]
        if actual != record["sha256"]:
            raise AdapterError("adapter_artifact_changed")
        entries = [
            entry
            for entry in registry._verified_entries(admitted)
            if entry.name == record["entry_point"]
        ]
        if len(entries) != 1:
            raise AdapterError("adapter_unavailable")
        registry._entry_origin(entries[0], admitted)
    if registry.read_configuration(path) != config:
        raise AdapterError("adapter_artifact_changed")
    return config


def format_environment_report(drift: EnvironmentDrift) -> str:
    """Render every environment difference in a stable, operator-readable report."""

    if drift.ok:
        return (
            "environment-check: installed non-editable distributions exactly match "
            "requirements.lock and any explicitly selected verified adapter pins."
        )

    lines = ["environment/lock drift:"]
    if drift.missing:
        lines.append("  missing from installed environment:")
        lines.extend(f"    {name}=={version}" for name, version in drift.missing.items())
    if drift.mismatched:
        lines.append("  version mismatches:")
        lines.extend(
            f"    {name}: lock=={locked}, installed=={installed}"
            for name, (locked, installed) in drift.mismatched.items()
        )
    if drift.unexpected:
        lines.append("  unexpected in installed environment:")
        lines.extend(f"    {name}=={version}" for name, version in drift.unexpected.items())
    if drift.untrusted_local:
        lines.append("  untrusted installed distributions:")
        lines.extend(f"    {name}: {reason}" for name, reason in drift.untrusted_local.items())
    if drift.adapter_error:
        lines.append(f"  explicit adapter allowance failed: {drift.adapter_error}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allowed-adapter-config",
        type=Path,
        default=os.environ.get("LEDGER_ALLOWED_ADAPTER_CONFIG"),
        help="Explicit private pin file; independent of the parsing configuration.",
    )
    args = parser.parse_args()
    if not runtime_is_supported():
        current = ".".join(str(part) for part in sys.version_info[:3])
        print(f"environment-check: Python 3.14 is required; found {current}.")
        return 1
    drift = check_environment(allowed_adapter_config=args.allowed_adapter_config)
    print(format_environment_report(drift))
    return 0 if drift.ok else 1


if __name__ == "__main__":
    sys.exit(main())
