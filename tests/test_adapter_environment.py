"""Inert installed-adapter allowances cannot weaken the exact core environment gate."""

import base64
import csv
import hashlib
import importlib.metadata
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ledger.adapters import registry
from ledger.adapters.contracts import API_VERSION, AdapterDefinition
from ledger.maintenance import environment_check


def _receipt(root: Path, dist_info: Path) -> None:
    output = io.StringIO()
    rows = csv.writer(output)
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.name == "RECORD":
            continue
        content = path.read_bytes()
        hashed = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")
        rows.writerow((path.relative_to(root).as_posix(), "sha256=" + hashed, len(content)))
    rows.writerow(((dist_info / "RECORD").relative_to(root).as_posix(), "", ""))
    (dist_info / "RECORD").write_text(output.getvalue())


@pytest.fixture
def installed_adapter(tmp_path, monkeypatch):
    root = tmp_path / "site-packages"
    source = root / "delivery_fictional_adapter" / "__init__.py"
    source.parent.mkdir(parents=True)
    source.write_text('raise RuntimeError("delivery verification executed private adapter code")\n')
    dist_info = root / "delivery_fictional_adapter-1.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: delivery-fictional-adapter\nVersion: 1.0\n"
    )
    (dist_info / "WHEEL").write_text("Wheel-Version: 1.0\nTag: py3-none-any\n")
    (dist_info / "entry_points.txt").write_text(
        "[ledger.import_adapters.v1]\nfictional = delivery_fictional_adapter:create\n"
    )
    _receipt(root, dist_info)
    distribution = importlib.metadata.PathDistribution(dist_info)
    record = {
        "distribution": "delivery-fictional-adapter",
        "version": "1.0",
        "entry_point": "fictional",
        "sha256": registry.artifact_digest(distribution),
        "metadata": registry._definition_metadata(AdapterDefinition("fictional", API_VERSION, "1")),
        "enabled": True,
    }
    runtime = tmp_path / "private-pins"
    runtime.mkdir(mode=0o700)
    config = runtime / "allowed.json"
    config.write_text(json.dumps({"schema_version": 1, "adapters": [record]}))
    config.chmod(0o600)
    core = SimpleNamespace(
        metadata={"Name": "locked-package"}, version="2.0", read_text=lambda _: None
    )
    monkeypatch.setattr(environment_check, "_read_lock_pins", lambda _: {"locked-package": "2.0"})
    monkeypatch.syspath_prepend(str(root))
    yield config, record, distribution, core, source
    assert "delivery_fictional_adapter" not in sys.modules


def test_explicit_pinned_adapter_is_allowed_without_executing_it(installed_adapter):
    path, _, adapter, core, _ = installed_adapter
    drift = environment_check.check_environment([core, adapter], allowed_adapter_config=path)
    assert drift.ok, environment_check.format_environment_report(drift)


def test_parsing_configuration_and_installed_entrypoints_never_grant_allowance(
    installed_adapter, monkeypatch
):
    path, _, adapter, core, _ = installed_adapter
    monkeypatch.setenv("LEDGER_ADAPTER_CONFIG", str(path))
    monkeypatch.setattr(registry, "read_configuration", lambda *_: pytest.fail("Default consulted"))
    drift = environment_check.check_environment([core, adapter])
    assert drift.unexpected == {"delivery-fictional-adapter": "1.0"}
    assert not drift.ok


@pytest.mark.parametrize(
    "failure",
    [
        "missing-config",
        "missing-package",
        "changed-code",
        "changed-version",
        "disabled",
        "wrong-entry",
        "core-collision",
        "duplicate",
    ],
)
def test_invalid_adapter_allowance_fails_closed(installed_adapter, failure):
    path, record, adapter, core, source = installed_adapter
    selected = [core, adapter]
    if failure == "missing-config":
        path.unlink()
    elif failure == "missing-package":
        selected = [core]
    elif failure == "changed-code":
        source.write_text("# altered at the same version\n")
    elif failure == "changed-version":
        record["version"] = "2.0"
    elif failure == "disabled":
        record["enabled"] = False
    elif failure == "wrong-entry":
        record["entry_point"] = "unknown"
    elif failure == "core-collision":
        record["distribution"] = "locked-package"
    elif failure == "duplicate":
        selected.append(adapter)
    if failure in {"changed-version", "disabled", "wrong-entry", "core-collision"}:
        path.write_text(json.dumps({"schema_version": 1, "adapters": [record]}))
    drift = environment_check.check_environment(selected, allowed_adapter_config=path)
    assert not drift.ok
    assert drift.adapter_error or drift.unexpected


def test_adapter_allowance_preserves_core_pins_and_refuses_unlisted_dependencies(installed_adapter):
    path, _, adapter, core, _ = installed_adapter
    core.version = "3.0"
    extra = SimpleNamespace(
        metadata={"Name": "unlisted-private-dependency"}, version="1.0", read_text=lambda _: None
    )
    drift = environment_check.check_environment([core, adapter, extra], allowed_adapter_config=path)
    assert drift.mismatched == {"locked-package": ("2.0", "3.0")}
    assert drift.unexpected == {"unlisted-private-dependency": "1.0"}
    assert not drift.ok


def test_adapter_allowance_preserves_audited_runtime_wheel_provenance(installed_adapter):
    path, _, adapter, core, _ = installed_adapter
    unaudited = SimpleNamespace(
        metadata={"Name": "streamlit"}, version="1.66.1", read_text=lambda _: None
    )
    drift = environment_check.check_environment(
        [core, adapter, unaudited], allowed_adapter_config=path
    )
    assert drift.untrusted_local == {
        "streamlit": "installed distribution has no direct wheel provenance"
    }
    assert not drift.ok


def test_unknown_editable_package_cannot_escape_the_environment_allowlist(installed_adapter):
    path, _, adapter, core, _ = installed_adapter
    extra = SimpleNamespace(
        metadata={"Name": "unlisted-private-dependency"},
        version="1.0",
        read_text=lambda _: '{"dir_info":{"editable":true}}',
    )
    drift = environment_check.check_environment([core, adapter, extra], allowed_adapter_config=path)
    assert drift.untrusted_local == {
        "unlisted-private-dependency": "only the canonical Ledger project may be editable"
    }
    assert not drift.ok
