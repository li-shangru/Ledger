"""Independently authored fictional extension and artifact-boundary regressions."""

import base64
import csv
import hashlib
import importlib.metadata as metadata
import io
import json
import sys
from pathlib import Path

import pytest

from ledger.adapters import registry
from ledger.adapters.contracts import AdapterError


@pytest.fixture
def adapter_runtime(tmp_path, monkeypatch):
    runtime = tmp_path / "private-runtime"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("LEDGER_HOME", str(runtime))
    monkeypatch.setenv("LEDGER_ADAPTER_CONFIG", str(runtime / "adapters.json"))
    registry._CACHE.clear()
    registry._IMPORTED.clear()
    registry._MODULES.clear()
    registry._SOURCE_FINDER.sources.clear()
    for name in list(sys.modules):
        if name == "invented_adapter" or name.startswith("invented_adapter."):
            monkeypatch.delitem(sys.modules, name)
    yield runtime
    for name in list(sys.modules):
        if name == "invented_adapter" or name.startswith("invented_adapter."):
            sys.modules.pop(name, None)


def write_record(root: Path) -> None:
    """Write a normal wheel installation receipt for invented fixture bytes."""
    record = root / "invented_package-1.0.0.dist-info" / "RECORD"
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path == record or "__pycache__" in path.parts:
            continue
        content = path.read_bytes()
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")
        writer.writerow((path.relative_to(root).as_posix(), "sha256=" + digest, len(content)))
    writer.writerow((record.relative_to(root).as_posix(), "", ""))
    record.write_text(output.getvalue())


def fictional_distribution(tmp_path, monkeypatch, *, body=None, root=None):
    root = root or tmp_path / "installed"
    root.mkdir()
    package = root / "invented_adapter"
    package.mkdir()
    source = package / "__init__.py"
    source.write_text(
        "from ledger.adapters.contracts import API_VERSION, AdapterDefinition, AdapterError\n"
        "def create():\n    "
        + (body or 'return AdapterDefinition("fictional-kit", API_VERSION, "1")')
        + "\n"
    )
    dist_info = root / "invented_package-1.0.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: invented-package\nVersion: 1.0.0\n"
    )
    (dist_info / "WHEEL").write_text(
        "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    )
    (dist_info / "entry_points.txt").write_text(
        "[ledger.import_adapters.v1]\nexample = invented_adapter:create\n"
    )
    write_record(root)
    monkeypatch.syspath_prepend(str(root))
    distribution = metadata.PathDistribution(dist_info)
    monkeypatch.setattr(registry.metadata, "distribution", lambda _: distribution)
    return distribution, source


def test_discovery_never_loads_unenabled_code(adapter_runtime, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Unenabled package discovery/import executed")

    monkeypatch.setattr(registry.metadata, "distribution", forbidden)
    assert registry.load_adapters() == ()
    assert registry.configured_metadata() == ()
    assert list(adapter_runtime.iterdir()) == []


def test_enable_pins_bytes_and_same_version_edit_refuses(adapter_runtime, tmp_path, monkeypatch):
    _, source = fictional_distribution(tmp_path, monkeypatch)
    path = adapter_runtime / "adapters.json"
    record = registry.enable_distribution("invented-package", "example", path=path)
    assert len(record["sha256"]) == 64
    assert path.stat().st_mode & 0o777 == 0o600
    assert registry.load_adapters()[0].definition.adapter_id == "fictional-kit"
    source.write_text('raise RuntimeError("private canary")\n')
    with pytest.raises(AdapterError, match=r"^adapter_artifact_changed$"):
        registry.load_adapters()


def test_read_only_metadata_does_not_load_extension(adapter_runtime, tmp_path, monkeypatch):
    fictional_distribution(tmp_path, monkeypatch)
    path = adapter_runtime / "adapters.json"
    registry.enable_distribution("invented-package", "example", path=path)
    registry.disable_distribution("invented-package", "example", path=path)
    monkeypatch.setattr(
        registry.metadata, "distribution", lambda _: pytest.fail("Package inspected")
    )
    assert registry.configured_metadata()[0]["adapter_id"] == "fictional-kit"
    assert registry.load_adapters() == ()


@pytest.mark.parametrize(
    "omitted_fields",
    [
        ("investment_asset_categories",),
        ("reimbursement_merchants",),
        ("investment_asset_categories", "reimbursement_merchants"),
    ],
)
def test_old_optional_policy_metadata_loads_without_receipt_rewrite(
    adapter_runtime, tmp_path, monkeypatch, omitted_fields
):
    fictional_distribution(tmp_path, monkeypatch)
    path = adapter_runtime / "adapters.json"
    registry.enable_distribution("invented-package", "example", path=path)
    config = json.loads(path.read_text())
    for field in omitted_fields:
        config["adapters"][0]["metadata"]["policy"].pop(field)
    path.write_text(json.dumps(config))
    before = path.read_bytes()

    assert registry.load_adapters()[0].definition.adapter_id == "fictional-kit"
    registry.enable_distribution("invented-package", "example", path=path)
    assert path.read_bytes() == before
    registry.disable_distribution("invented-package", "example", path=path)
    registry.enable_distribution("invented-package", "example", path=path)
    saved = json.loads(path.read_text())
    assert saved["adapters"][0]["enabled"] is True
    assert not set(omitted_fields) & saved["adapters"][0]["metadata"]["policy"].keys()


def test_incompatible_api_refuses_without_saving(adapter_runtime, tmp_path, monkeypatch):
    fictional_distribution(
        tmp_path, monkeypatch, body='return AdapterDefinition("fictional-kit", 999, "1")'
    )
    with pytest.raises(AdapterError, match=r"^adapter_incompatible$"):
        registry.enable_distribution(
            "invented-package", "example", path=adapter_runtime / "adapters.json"
        )
    assert not (adapter_runtime / "adapters.json").exists()


@pytest.mark.parametrize("exception", ["RuntimeError", "SystemExit", "KeyboardInterrupt"])
def test_extension_failure_does_not_disclose_exception(
    adapter_runtime, tmp_path, monkeypatch, exception
):
    fictional_distribution(
        tmp_path,
        monkeypatch,
        body=f'raise {exception}("CANARY_PROVIDER /private/account/file 123.45")',
    )
    with pytest.raises(AdapterError, match=r"^adapter_failed$"):
        registry.enable_distribution(
            "invented-package", "example", path=adapter_runtime / "adapters.json"
        )


def test_forged_adapter_error_does_not_escape(adapter_runtime, tmp_path, monkeypatch):
    fictional_distribution(
        tmp_path,
        monkeypatch,
        body='error = AdapterError(); error.args = ("PRIVATE_CANARY",); raise error',
    )
    with pytest.raises(AdapterError, match=r"^adapter_failed$"):
        registry.enable_distribution(
            "invented-package", "example", path=adapter_runtime / "adapters.json"
        )


def test_duplicate_json_keys_refuse(adapter_runtime):
    path = adapter_runtime / "adapters.json"
    path.write_text('{"schema_version":1,"adapters":[],"adapters":[]}')
    path.chmod(0o600)
    with pytest.raises(AdapterError, match=r"^adapter_unsafe_configuration$"):
        registry.read_configuration()


def test_binding_detects_disabled_package(adapter_runtime, tmp_path, monkeypatch):
    fictional_distribution(tmp_path, monkeypatch)
    path = adapter_runtime / "adapters.json"
    registry.enable_distribution("invented-package", "example", path=path)
    binding = registry.snapshot_bindings()
    registry.verify_bindings(binding)
    registry.disable_distribution("invented-package", "example", path=path)
    with pytest.raises(AdapterError, match=r"^import_binding_changed$"):
        registry.verify_bindings(binding)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda m: m.update(api_version=True),
        lambda m: m.update(layout_revision=3),
        lambda m: m.update(csv_formats="PRIVATE_CANARY"),
        lambda m: m.update(policy={}),
        lambda m: m["policy"].update(credit_institutions="fictional"),
        lambda m: m["policy"].update(payment_tokens={"fictional": [False]}),
        lambda m: m["policy"].update(account_product_tokens=[["one"]]),
        lambda m: m["policy"].update(classification_rules={"fictional": {"unknown": []}}),
        lambda m: m["policy"].update(merchant_prefix_patterns=["[PRIVATE_CANARY"]),
        lambda m: m.update(unrecognized=True),
    ],
)
def test_malformed_saved_metadata_refuses_inertly(adapter_runtime, tmp_path, monkeypatch, mutation):
    fictional_distribution(tmp_path, monkeypatch)
    path = adapter_runtime / "adapters.json"
    registry.enable_distribution("invented-package", "example", path=path)
    config = json.loads(path.read_text())
    mutation(config["adapters"][0]["metadata"])
    path.write_text(json.dumps(config))
    monkeypatch.setattr(
        registry.metadata, "distribution", lambda _: pytest.fail("Package inspected")
    )
    with pytest.raises(AdapterError, match=r"^adapter_unsafe_configuration$"):
        registry.read_configuration()


def test_record_digest_required_before_initial_enable(adapter_runtime, tmp_path, monkeypatch):
    _, source = fictional_distribution(tmp_path, monkeypatch)
    source.write_text('raise RuntimeError("PRIVATE_CANARY")\n')
    with pytest.raises(AdapterError, match=r"^adapter_artifact_changed$"):
        registry.enable_distribution(
            "invented-package", "example", path=adapter_runtime / "adapters.json"
        )
    assert "invented_adapter" not in sys.modules


def test_shadowed_module_refuses_before_execution(adapter_runtime, tmp_path, monkeypatch):
    fictional_distribution(tmp_path, monkeypatch)
    shadow = tmp_path / "shadow"
    shadow.mkdir()
    (shadow / "invented_adapter.py").write_text('raise RuntimeError("PRIVATE_CANARY")\n')
    monkeypatch.syspath_prepend(str(shadow))
    with pytest.raises(AdapterError, match=r"^adapter_artifact_changed$"):
        registry.enable_distribution(
            "invented-package", "example", path=adapter_runtime / "adapters.json"
        )
    assert "invented_adapter" not in sys.modules


def test_configuration_is_read_through_one_pinned_descriptor(adapter_runtime, monkeypatch):
    path = adapter_runtime / "adapters.json"
    path.write_text('{"schema_version": 1, "adapters": []}')
    path.chmod(0o600)
    from ledger.utils.private_files import PinnedPrivateDirectory

    monkeypatch.setattr(
        PinnedPrivateDirectory,
        "read_private_text",
        lambda *a, **k: pytest.fail("Configuration reopened"),
    )
    assert registry.read_configuration() == {"schema_version": 1, "adapters": []}


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "permissions", "oversized"])
def test_unsafe_configuration_files_refuse(adapter_runtime, tmp_path, kind):
    path = adapter_runtime / "adapters.json"
    target = tmp_path / "source.json"
    target.write_text('{"schema_version":1,"adapters":[]}')
    target.chmod(0o600)
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "hardlink":
        path.hardlink_to(target)
    else:
        path.write_bytes(target.read_bytes() if kind == "permissions" else b" " * (256 * 1024 + 1))
        path.chmod(0o644 if kind == "permissions" else 0o600)
    with pytest.raises(AdapterError, match=r"^adapter_unsafe_configuration$"):
        registry.read_configuration()


def test_config_replaced_while_descriptor_is_read_refuses(adapter_runtime, monkeypatch):
    path = adapter_runtime / "adapters.json"
    path.write_text('{"schema_version":1,"adapters":[]}')
    path.chmod(0o600)
    replacement = adapter_runtime / "replacement.json"
    replacement.write_bytes(path.read_bytes())
    replacement.chmod(0o600)
    read = registry.os.read
    swapped = False

    def swap(descriptor, size):
        nonlocal swapped
        content = read(descriptor, size)
        if not swapped:
            swapped = True
            replacement.replace(path)
        return content

    monkeypatch.setattr(registry.os, "read", swap)
    with pytest.raises(AdapterError, match=r"^adapter_unsafe_configuration$"):
        registry.read_configuration()


@pytest.mark.parametrize(
    "kind", ["symlink", "hardlink", "missing_hash", "duplicate_record", "missing_record"]
)
def test_unsafe_installed_artifacts_refuse(adapter_runtime, tmp_path, monkeypatch, kind):
    distribution, source = fictional_distribution(tmp_path, monkeypatch)
    root = source.parent.parent
    record = root / "invented_package-1.0.0.dist-info" / "RECORD"
    if kind in {"symlink", "hardlink"}:
        target = tmp_path / "source.py"
        source.rename(target)
        source.symlink_to(target) if kind == "symlink" else source.hardlink_to(target)
    elif kind == "missing_hash":
        lines = record.read_text().splitlines()
        lines[0] = lines[0].split(",")[0] + ",,"
        record.write_text("\n".join(lines) + "\n")
    elif kind == "duplicate_record":
        record.write_text(record.read_text() + record.read_text().splitlines()[0] + "\n")
    else:
        record.unlink()
    with pytest.raises(AdapterError, match=r"^adapter_artifact_changed$"):
        registry.artifact_digest(distribution)
    assert "invented_adapter" not in sys.modules


def test_artifact_replacement_during_read_refuses(adapter_runtime, tmp_path, monkeypatch):
    distribution, source = fictional_distribution(tmp_path, monkeypatch)
    source_inode = source.stat().st_ino
    read = registry.os.read
    replacement = tmp_path / "replacement.py"
    replacement.write_bytes(source.read_bytes())

    def swap(descriptor, size):
        content = read(descriptor, size)
        if registry.os.fstat(descriptor).st_ino == source_inode and replacement.exists():
            replacement.replace(source)
        return content

    monkeypatch.setattr(registry.os, "read", swap)
    with pytest.raises(AdapterError, match=r"^adapter_artifact_changed$"):
        registry.artifact_digest(distribution)


def test_duplicate_installed_distributions_refuse(adapter_runtime, tmp_path, monkeypatch):
    _, source = fictional_distribution(tmp_path, monkeypatch)
    other = source.parent.parent / "invented_package-2.0.0.dist-info"
    other.mkdir()
    (other / "METADATA").write_text("Name: invented-package\nVersion: 2.0.0\n")
    with pytest.raises(AdapterError, match=r"^adapter_unavailable$"):
        registry.enable_distribution(
            "invented-package", "example", path=adapter_runtime / "adapters.json"
        )
    assert "invented_adapter" not in sys.modules


def test_duplicate_historical_format_refuses_without_execution(
    adapter_runtime, tmp_path, monkeypatch
):
    from copy import deepcopy

    fictional_distribution(tmp_path, monkeypatch)
    path = adapter_runtime / "adapters.json"
    registry.enable_distribution("invented-package", "example", path=path)
    config = json.loads(path.read_text())
    first = config["adapters"][0]
    first["metadata"]["csv_formats"] = [{"format_id": "invented", "institution": "fictional"}]
    second = deepcopy(first)
    second["distribution"] = "other-package"
    second["metadata"]["adapter_id"] = "other-kit"
    second["enabled"] = False
    config["adapters"].append(second)
    path.write_text(json.dumps(config))
    monkeypatch.setattr(
        registry.metadata, "distribution", lambda _: pytest.fail("Package inspected")
    )
    with pytest.raises(AdapterError, match=r"^adapter_unsafe_configuration$"):
        registry.load_adapters()


def test_valid_structured_historical_metadata_is_inert(adapter_runtime, tmp_path, monkeypatch):
    fictional_distribution(tmp_path, monkeypatch)
    path = adapter_runtime / "adapters.json"
    registry.enable_distribution("invented-package", "example", path=path)
    config = json.loads(path.read_text())
    info = config["adapters"][0]["metadata"]
    info["pdf_formats"] = [
        {
            "format_id": "fictional-deposit",
            "institution": "fictional",
            "headers": ["date", "amount"],
            "archive_kind": "deposit",
            "balance_spec": {
                "opening_key": "opening",
                "closing_key": "closing",
                "date_columns": ["date"],
                "amount_column": "amount",
                "printed_sign": 1,
                "row_sign": -1,
                "movement_backed": False,
            },
        }
    ]
    info["investment_formats"] = [
        {
            "format_id": "fictional-investments",
            "institution": "fictional",
            "source_layout": "flat",
            "capabilities": ["complete_statement", "position_snapshots", "statement_balances"],
            "default": True,
            "evidence_only_aliases": ["Fictional savings"],
        }
    ]
    info["policy"].update(
        payment_tokens={"fictional": ["fictional payment"]},
        account_product_tokens=[["fictional savings", "savings"]],
        classification_rules={"fictional": {"fee_patterns": [r"^Fictional fee$"]}},
    )
    config["adapters"][0]["enabled"] = False
    path.write_text(json.dumps(config))
    monkeypatch.setattr(
        registry.metadata, "distribution", lambda _: pytest.fail("Package inspected")
    )
    assert registry.metadata_for_statement_type("fictional-deposit")["archive_kind"] == "deposit"
    assert registry.compatibility_policy()["payment_tokens"] == {"fictional": ["fictional payment"]}
    assert registry.load_adapters() == ()
    info["pdf_formats"][0]["balance_spec"]["row_sign"] = True
    path.write_text(json.dumps(config))
    with pytest.raises(AdapterError, match=r"^adapter_unsafe_configuration$"):
        registry.configured_metadata()


def test_boolean_binding_schema_is_not_integer_version(adapter_runtime):
    binding = registry.snapshot_bindings()
    binding["schema_version"] = True
    with pytest.raises(AdapterError, match=r"^import_binding_changed$"):
        registry.verify_bindings(binding)


def test_untrusted_bytecode_cache_cannot_replace_verified_source(
    adapter_runtime, tmp_path, monkeypatch
):
    import importlib.util
    import marshal
    import struct

    _, source = fictional_distribution(tmp_path, monkeypatch)
    code = compile('raise RuntimeError("PRIVATE_CANARY")', str(source), "exec")
    info = source.stat()
    payload = (
        importlib.util.MAGIC_NUMBER
        + struct.pack("<III", 0, int(info.st_mtime), info.st_size)
        + marshal.dumps(code)
    )
    cache = Path(importlib.util.cache_from_source(str(source)))
    cache.parent.mkdir()
    cache.write_bytes(payload)
    record = registry.enable_distribution(
        "invented-package", "example", path=adapter_runtime / "adapters.json"
    )
    assert record["metadata"]["adapter_id"] == "fictional-kit"


def test_lazy_import_cannot_execute_unrecorded_package_source(
    adapter_runtime, tmp_path, monkeypatch
):
    _, source = fictional_distribution(tmp_path, monkeypatch)
    registry.enable_distribution(
        "invented-package", "example", path=adapter_runtime / "adapters.json"
    )
    (source.parent / "unrecorded.py").write_text('raise RuntimeError("PRIVATE_CANARY")')
    import importlib

    with pytest.raises(AdapterError, match=r"^adapter_artifact_changed$"):
        importlib.import_module("invented_adapter.unrecorded")
    assert "invented_adapter.unrecorded" not in sys.modules


def test_same_pinned_package_can_be_reenabled(adapter_runtime, tmp_path, monkeypatch):
    fictional_distribution(tmp_path, monkeypatch)
    path = adapter_runtime / "adapters.json"
    before = registry.enable_distribution("invented-package", "example", path=path)
    registry.disable_distribution("invented-package", "example", path=path)
    assert registry.enable_distribution("invented-package", "example", path=path) == before
    assert registry.load_adapters()[0].definition.adapter_id == "fictional-kit"


def test_private_aliases_are_inert_and_conflicts_refuse(adapter_runtime, tmp_path, monkeypatch):
    from copy import deepcopy

    fictional_distribution(tmp_path, monkeypatch)
    path = adapter_runtime / "adapters.json"
    registry.enable_distribution("invented-package", "example", path=path)
    config = json.loads(path.read_text())
    first = config["adapters"][0]
    first["metadata"]["policy"]["account_aliases"] = {"fictional-typo": "fictional-canonical"}
    first["enabled"] = False
    path.write_text(json.dumps(config))
    monkeypatch.setattr(
        registry.metadata, "distribution", lambda _: pytest.fail("Package inspected")
    )
    assert registry.compatibility_policy()["account_aliases"] == {
        "fictional-typo": "fictional-canonical"
    }
    second = deepcopy(first)
    second["distribution"] = "other-package"
    second["metadata"]["adapter_id"] = "other-kit"
    second["metadata"]["policy"]["account_aliases"]["fictional-typo"] = "different-canonical"
    config["adapters"].append(second)
    path.write_text(json.dumps(config))
    with pytest.raises(AdapterError, match=r"^adapter_unsafe_configuration$"):
        registry.compatibility_policy()


def test_declaration_rejects_non_string_policy_keys_without_coercion():
    from ledger.adapters.contracts import API_VERSION, AdapterDefinition, CompatibilityPolicy

    definition = AdapterDefinition(
        "fictional-kit",
        API_VERSION,
        "1",
        policy=CompatibilityPolicy(payment_tokens={123: ("fictional",)}),
    )
    with pytest.raises(AdapterError, match=r"^adapter_invalid_contract$"):
        registry._definition_metadata(definition)


def test_recorded_lazy_import_keeps_cached_adapter_usable(adapter_runtime, tmp_path, monkeypatch):
    import importlib

    _, source = fictional_distribution(tmp_path, monkeypatch)
    (source.parent / "helper.py").write_text('VALUE = "fictional"\n')
    write_record(source.parent.parent)
    registry.enable_distribution(
        "invented-package", "example", path=adapter_runtime / "adapters.json"
    )
    assert importlib.import_module("invented_adapter.helper").VALUE == "fictional"
    assert registry.load_adapters()[0].definition.adapter_id == "fictional-kit"


def test_installed_code_in_checkout_venv_uses_external_configuration(
    adapter_runtime, tmp_path, monkeypatch
):
    checkout = tmp_path / "fictional-checkout"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    installed = checkout / ".venv" / "lib" / "python3.14" / "site-packages"
    installed.parent.mkdir(parents=True)
    distribution, source = fictional_distribution(tmp_path, monkeypatch, root=installed)
    # A real PathDistribution and import path exercise the installed-code boundary.
    path = adapter_runtime / "adapters.json"
    assert len(registry.artifact_digest(distribution)) == 64
    registry.enable_distribution("invented-package", "example", path=path)
    assert registry.load_adapters()[0].definition.adapter_id == "fictional-kit"
    assert Path(sys.modules["invented_adapter"].__file__) == source
    assert checkout in source.parents
    with pytest.raises(AdapterError, match=r"^adapter_unsafe_configuration$"):
        registry.read_configuration(checkout / "adapters.json")


def test_linked_code_directory_is_rejected(adapter_runtime, tmp_path, monkeypatch):
    distribution, source = fictional_distribution(tmp_path, monkeypatch)
    linked = tmp_path / "linked-package"
    source.parent.rename(linked)
    source.parent.symlink_to(linked, target_is_directory=True)
    with pytest.raises(AdapterError, match=r"^adapter_artifact_changed$"):
        registry.artifact_digest(distribution)


def test_code_parent_replaced_during_pinned_read_is_rejected(
    adapter_runtime, tmp_path, monkeypatch
):
    distribution, source = fictional_distribution(tmp_path, monkeypatch)
    original = source.read_bytes()
    inode = source.stat().st_ino
    moved = tmp_path / "moved-package"
    read = registry.os.read

    def replace_parent(descriptor, size):
        content = read(descriptor, size)
        if registry.os.fstat(descriptor).st_ino == inode and not moved.exists():
            source.parent.rename(moved)
            source.parent.mkdir()
            source.write_bytes(original)
        return content

    monkeypatch.setattr(registry.os, "read", replace_parent)
    with pytest.raises(AdapterError, match=r"^adapter_artifact_changed$"):
        registry.artifact_digest(distribution)
