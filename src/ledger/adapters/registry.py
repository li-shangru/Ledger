"""Explicit, artifact-pinned loading of installed local import extensions.

Discovery does not import plugins. Only an explicit enable operation loads a
selected distribution; ordinary imports require its saved private allowlist pin.
Read-only consumers use declarative metadata and never call ``load_adapters``.
"""

from __future__ import annotations

import base64
import configparser
import csv
import hashlib
import importlib.machinery
import importlib.metadata as metadata
import io
import json
import os
import re
import stat
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from ledger.adapters.contracts import (
    API_VERSION,
    ENTRY_POINT_GROUP,
    INVESTMENT_ASSET_CATEGORY_PATTERN,
    MAX_INVESTMENT_ASSET_CATEGORIES,
    MAX_INVESTMENT_ASSET_CATEGORY_LENGTH,
    AdapterDefinition,
    AdapterError,
    BalanceSpec,
    CompatibilityPolicy,
    CsvFormat,
    InvestmentCapability,
    InvestmentFormat,
    PdfFormat,
)
from ledger.runtime import require_external_runtime_path, runtime_path
from ledger.utils.private_files import pin_existing_directory

_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")
_VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+!-]{0,95}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_CACHE: dict[tuple[str, str, str, str], AdapterDefinition] = {}
_IMPORTED: dict[tuple[str, str], str] = {}
_CONFIG_LIMIT = 256 * 1024
_FILE_LIMIT = 16 * 1024 * 1024
_ARTIFACT_LIMIT = 128 * 1024 * 1024
_MODULES: dict[str, object] = {}


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def configuration_path(path: Path | None = None) -> Path:
    if path is None:
        selected = os.environ.get("LEDGER_ADAPTER_CONFIG")
        path = Path(selected) if selected else runtime_path("import-adapters.json")
    return require_external_runtime_path(path)


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AdapterError("adapter_unsafe_configuration")
        result[key] = value
    return result


def _token(value: object) -> bool:
    return type(value) is str and _TOKEN.fullmatch(value) is not None


def _text(value: object) -> bool:
    return type(value) is str and 0 < len(value) <= 512 and value.isprintable()


def _mapping(value: object, keys: set[str], *, optional: set[str] | None = None) -> None:
    if type(value) is not dict or not keys <= set(value) <= keys | (optional or set()):
        raise ValueError


def _strings(value: object, *, tokens: bool = False, minimum: int = 0) -> None:
    if type(value) is not list or not minimum <= len(value) <= 128:
        raise ValueError
    if not all((_token if tokens else _text)(item) for item in value):
        raise ValueError
    if len(set(value)) != len(value):
        raise ValueError


def _patterns(value: Any) -> None:
    from ledger.config.schemas import _check_redos

    _strings(value)
    for pattern in value:
        _check_redos(pattern)
        re.compile(pattern, re.IGNORECASE)


def _validate_metadata(info: Any) -> None:
    """Validate inert JSON completely before historical consumers use any field."""
    _mapping(
        info,
        {
            "adapter_id",
            "api_version",
            "layout_revision",
            "csv_formats",
            "pdf_formats",
            "investment_formats",
            "policy",
        },
    )
    if (
        not _token(info["adapter_id"])
        or not _token(info["layout_revision"])
        or type(info["api_version"]) is not int
        or info["api_version"] != API_VERSION
    ):
        raise ValueError
    for kind in ("csv", "pdf", "investment"):
        formats = info[kind + "_formats"]
        if type(formats) is not list or len(formats) > 64:
            raise ValueError
        identifiers: set[str] = set()
        for fmt in formats:
            fields = {"format_id", "institution"}
            if kind == "pdf":
                fields |= {"headers", "balance_spec", "archive_kind"}
            elif kind == "investment":
                fields |= {"source_layout", "capabilities", "default", "evidence_only_aliases"}
            _mapping(fmt, fields)
            if not _token(fmt["format_id"]) or not _token(fmt["institution"]):
                raise ValueError
            if fmt["format_id"] in identifiers:
                raise AdapterError("adapter_duplicate")
            identifiers.add(fmt["format_id"])
            if kind == "pdf":
                _strings(fmt["headers"], minimum=1)
                if not _token(fmt["archive_kind"]):
                    raise ValueError
                spec = fmt["balance_spec"]
                if spec is not None:
                    _mapping(
                        spec,
                        {
                            "opening_key",
                            "closing_key",
                            "date_columns",
                            "amount_column",
                            "printed_sign",
                            "row_sign",
                            "movement_backed",
                        },
                    )
                    if not _token(spec["opening_key"]) or not _token(spec["closing_key"]):
                        raise ValueError
                    _strings(spec["date_columns"], minimum=1)
                    if (
                        not set(spec["date_columns"]) <= set(fmt["headers"])
                        or not _text(spec["amount_column"])
                        or spec["amount_column"] not in fmt["headers"]
                        or type(spec["movement_backed"]) is not bool
                    ):
                        raise ValueError
                    if any(
                        type(spec[key]) is not int or spec[key] not in (-1, 1)
                        for key in ("printed_sign", "row_sign")
                    ):
                        raise ValueError
            elif kind == "investment":
                if (
                    type(fmt["source_layout"]) is not str
                    or fmt["source_layout"] not in {"account_folders", "flat"}
                    or type(fmt["default"]) is not bool
                ):
                    raise ValueError
                _strings(fmt["evidence_only_aliases"])
                _strings(fmt["capabilities"], tokens=True)
                capabilities = set(fmt["capabilities"])
                if not capabilities <= {str(c) for c in InvestmentCapability}:
                    raise ValueError
                required = {
                    str(c)
                    for c in (
                        InvestmentCapability.COMPLETE_STATEMENT,
                        InvestmentCapability.POSITIONS,
                        InvestmentCapability.BALANCES,
                    )
                }
                if not required <= capabilities:
                    raise AdapterError("adapter_unsupported_capability")
    policy = info["policy"]
    _mapping(
        policy,
        {
            "account_aliases",
            "credit_institutions",
            "credit_account_tokens",
            "payment_tokens",
            "account_product_tokens",
            "classification_rules",
            "bill_tokens",
            "merchant_prefix_patterns",
        },
        optional={"investment_asset_categories", "reimbursement_merchants"},
    )
    reimbursement_merchants = policy.get("reimbursement_merchants", [])
    _strings(reimbursement_merchants)
    if any(
        not merchant.strip() or len(merchant.strip().upper()) > 512
        for merchant in reimbursement_merchants
    ):
        raise ValueError
    asset_categories = policy.get("investment_asset_categories", [])
    _strings(asset_categories)
    if len(asset_categories) > MAX_INVESTMENT_ASSET_CATEGORIES or any(
        len(category) > MAX_INVESTMENT_ASSET_CATEGORY_LENGTH
        or re.fullmatch(INVESTMENT_ASSET_CATEGORY_PATTERN, category) is None
        for category in asset_categories
    ):
        raise ValueError
    aliases = policy["account_aliases"]
    if (
        type(aliases) is not dict
        or len(aliases) > 128
        or any(not _text(key) or not _text(value) for key, value in aliases.items())
    ):
        raise ValueError
    _strings(policy["credit_institutions"], tokens=True)
    _strings(policy["credit_account_tokens"])
    _strings(policy["bill_tokens"])
    _patterns(policy["merchant_prefix_patterns"])
    pairs = policy["account_product_tokens"]
    if type(pairs) is not list or len(pairs) > 128:
        raise ValueError
    for pair in pairs:
        if type(pair) is not list or len(pair) != 2 or not _text(pair[0]) or not _token(pair[1]):
            raise ValueError
    if len({pair[0] for pair in pairs}) != len(pairs):
        raise ValueError
    for key in ("payment_tokens", "classification_rules"):
        values = policy[key]
        if type(values) is not dict or len(values) > 128 or not all(_token(k) for k in values):
            raise ValueError
        for value in values.values():
            if key == "payment_tokens":
                _strings(value, minimum=1)
            else:
                allowed = {
                    "transfer_patterns",
                    "etransfer_patterns",
                    "cc_payment_patterns",
                    "fee_patterns",
                    "interest_patterns",
                    "income_patterns",
                    "payment_patterns",
                    "reward_patterns",
                }
                if type(value) is not dict or not set(value) <= allowed:
                    raise ValueError
                for patterns in value.values():
                    _patterns(patterns)


def _validate_configuration(value: Any) -> None:
    _mapping(value, {"schema_version", "adapters"})
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise ValueError
    records = value["adapters"]
    if type(records) is not list or len(records) > 32:
        raise ValueError
    identities: set[tuple[str, str]] = set()
    adapter_ids: set[str] = set()
    formats: set[tuple[str, str]] = set()
    for record in records:
        _mapping(
            record, {"distribution", "version", "entry_point", "sha256", "metadata", "enabled"}
        )
        if type(record["enabled"]) is not bool:
            raise ValueError
        if not _token(record["distribution"]) or not _token(record["entry_point"]):
            raise ValueError
        if type(record["version"]) is not str or not _VERSION.fullmatch(record["version"]):
            raise ValueError
        if type(record["sha256"]) is not str or not _DIGEST.fullmatch(record["sha256"]):
            raise ValueError
        identity = (_distribution_name(record["distribution"]), record["entry_point"])
        if identity in identities:
            raise AdapterError("adapter_duplicate")
        identities.add(identity)
        info = record["metadata"]
        _validate_metadata(info)
        if info["adapter_id"] in adapter_ids:
            raise AdapterError("adapter_duplicate")
        adapter_ids.add(info["adapter_id"])
        # Disabled records still own their historical identifiers. Reusing one
        # would make historical interpretation depend on registration order.
        for kind in ("csv", "pdf", "investment"):
            for fmt in info[kind + "_formats"]:
                identity = (kind, fmt["format_id"])
                if identity in formats:
                    raise AdapterError("adapter_duplicate")
                formats.add(identity)
    _merged_policy(records)


def read_configuration(path: Path | None = None) -> dict[str, Any]:
    """Read inert private metadata once through its validated pinned descriptor."""
    try:
        selected = configuration_path(path)
        try:
            with pin_existing_directory(selected.parent) as parent:
                parent.require_owner_only()
                with parent.pin_private_file(selected.name) as pinned:
                    before = os.fstat(pinned.descriptor)
                    if before.st_nlink != 1 or before.st_size > _CONFIG_LIMIT:
                        raise ValueError
                    content = _read_descriptor(pinned.descriptor, _CONFIG_LIMIT)
                    pinned.validate()
                    after = os.fstat(pinned.descriptor)
                    if (
                        after.st_nlink != 1
                        or after.st_mode != before.st_mode
                        or after.st_ctime_ns != before.st_ctime_ns
                        or len(content) != before.st_size
                    ):
                        raise ValueError
            value = json.loads(content, object_pairs_hook=_no_duplicate_keys)
        except FileNotFoundError:
            return {"schema_version": 1, "adapters": []}
        _validate_configuration(value)
        return value
    except Exception:
        raise AdapterError("adapter_unsafe_configuration") from None


def _read_descriptor(descriptor: int, limit: int) -> bytes:
    chunks = []
    remaining = limit + 1
    while remaining:
        chunk = os.read(descriptor, min(remaining, 64 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    if not remaining:
        raise ValueError
    return b"".join(chunks)


def _distribution_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def _distribution(name: str) -> metadata.Distribution:
    try:
        distribution = metadata.distribution(name)
        if (
            _distribution_name(distribution.metadata["Name"]) != _distribution_name(name)
            or type(distribution.version) is not str
            or not _VERSION.fullmatch(distribution.version)
        ):
            raise AdapterError("adapter_incompatible")
        candidates = {
            (
                str(candidate.locate_file("")),
                candidate.version,
                tuple(
                    str(f) for f in (candidate.files or ()) if str(f).endswith(".dist-info/RECORD")
                ),
            )
            for candidate in metadata.distributions(name=name)
        }
        if len(candidates) != 1:
            raise AdapterError("adapter_unavailable")
        direct = distribution.read_text("direct_url.json")
        if direct and json.loads(direct).get("dir_info", {}).get("editable"):
            raise AdapterError("adapter_incompatible")
        return distribution
    except AdapterError:
        raise
    except Exception:
        raise AdapterError("adapter_unavailable") from None


@contextmanager
def _artifact_directory(path: Path) -> Iterator[tuple[int, Callable[[], None]]]:
    """Pin read-only code directories, including virtualenvs within checkouts.

    Financial runtime helpers deliberately refuse source-tree paths. Installed
    package code has a different location contract, but retains descriptor-relative
    no-follow traversal and validation of every ancestor binding.
    """
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    opened: list[tuple[int, str, tuple[int, int]]] = []

    def validate() -> None:
        for index, (descriptor, name, expected) in enumerate(opened):
            parent = opened[index - 1][0] if index else None
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            pinned = os.fstat(descriptor)
            if (
                not stat.S_ISDIR(current.st_mode)
                or not stat.S_ISDIR(pinned.st_mode)
                or (current.st_dev, current.st_ino) != expected
                or (pinned.st_dev, pinned.st_ino) != expected
            ):
                raise ValueError

    try:
        for name in (path.anchor, *path.parts[1:]):
            parent = opened[-1][0] if opened else None
            descriptor = os.open(name, flags, dir_fd=parent)
            info = os.fstat(descriptor)
            opened.append((descriptor, name, (info.st_dev, info.st_ino)))
            if not stat.S_ISDIR(info.st_mode):
                raise ValueError
        validate()
        yield opened[-1][0], validate
        validate()
    finally:
        for descriptor, _, _ in reversed(opened):
            os.close(descriptor)


def _read_artifact(path: Path, *, limit: int = _FILE_LIMIT) -> bytes:
    """Read a regular artifact through one no-follow fd in a pinned directory."""
    with _artifact_directory(path.parent) as (parent_descriptor, validate_parent):
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
        try:
            before = os.fstat(descriptor)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
                raise ValueError
            content = _read_descriptor(descriptor, limit)
            after = os.fstat(descriptor)
            lexical = os.stat(path.name, dir_fd=parent_descriptor, follow_symlinks=False)

            def identity(info):
                return (
                    info.st_dev,
                    info.st_ino,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                    info.st_mode,
                    info.st_nlink,
                )

            if (
                identity(before) != identity(after)
                or identity(after) != identity(lexical)
                or len(content) != before.st_size
            ):
                raise ValueError
            validate_parent()
            return content
        finally:
            os.close(descriptor)


def _artifact_inventory(distribution: metadata.Distribution) -> tuple[str, dict[Path, str]]:
    try:
        root = Path(str(distribution.locate_file("")))
        files = distribution.files
        if not root.is_absolute() or not files or len(files) > 4096:
            raise ValueError
        names = [str(item) for item in files]
        if len(set(names)) != len(names):
            raise ValueError
        record_names = [
            name
            for name in names
            if len(Path(name).parts) == 2
            and Path(name).parent.name.endswith(".dist-info")
            and Path(name).name == "RECORD"
        ]
        if len(record_names) != 1:
            raise ValueError
        record_name = record_names[0]
        receipt = _read_artifact(root / record_name, limit=_CONFIG_LIMIT)
        rows = list(csv.reader(io.StringIO(receipt.decode("utf-8")), strict=True))
        if len(rows) != len(names) or any(len(row) != 3 for row in rows):
            raise ValueError
        if {row[0] for row in rows} != set(names):
            raise ValueError
        dist_info = Path(record_name).parent
        if not {str(dist_info / name) for name in ("METADATA", "WHEEL", "entry_points.txt")} <= set(
            names
        ):
            raise ValueError
        digest = hashlib.sha256()
        admitted: dict[Path, str] = {}
        total = 0
        for name, expected, size in sorted(rows):
            relative = Path(name)
            if (
                not name
                or relative.is_absolute()
                or ".." in relative.parts
                or relative.as_posix() != name
                or "\\" in name
                or any(ord(char) < 32 for char in name)
            ):
                raise ValueError
            path = root / relative
            if Path(str(distribution.locate_file(relative))) != path:
                raise ValueError
            if "__pycache__" in relative.parts or name.endswith(".pyc"):
                continue
            content = _read_artifact(path)
            total += len(content)
            if total > _ARTIFACT_LIMIT:
                raise ValueError
            # Location-dependent installer receipts are not part of the stable
            # digest. Every code/resource/metadata member must match RECORD.
            receipt_only = relative.parent == dist_info and relative.name in {
                "RECORD",
                "INSTALLER",
                "REQUESTED",
                "direct_url.json",
            }
            if not receipt_only:
                actual = (
                    base64.urlsafe_b64encode(hashlib.sha256(content).digest()).decode().rstrip("=")
                )
                if (
                    expected != "sha256=" + actual
                    or not size.isascii()
                    or not size.isdecimal()
                    or int(size) != len(content)
                ):
                    raise ValueError
                admitted[path] = hashlib.sha256(content).hexdigest()
                digest.update(name.encode() + b"\0" + hashlib.sha256(content).digest())
            elif name == record_name and (expected or size):
                raise ValueError
        if not admitted or _read_artifact(root / record_name, limit=_CONFIG_LIMIT) != receipt:
            raise ValueError
        return digest.hexdigest(), admitted
    except Exception:
        raise AdapterError("adapter_artifact_changed") from None


def artifact_digest(distribution: metadata.Distribution) -> str:
    """Verify RECORD hashes and hash installed artifact bytes without importing."""
    return _artifact_inventory(distribution)[0]


class _EntryPointParser(configparser.ConfigParser):
    def optionxform(self, optionstr: str) -> str:
        return optionstr


def _verified_entries(admitted: dict[Path, str]) -> list[metadata.EntryPoint]:
    try:
        paths = [
            path
            for path in admitted
            if path.name == "entry_points.txt" and path.parent.name.endswith(".dist-info")
        ]
        if len(paths) != 1:
            raise ValueError
        content = _read_artifact(paths[0], limit=_CONFIG_LIMIT)
        if hashlib.sha256(content).hexdigest() != admitted[paths[0]]:
            raise ValueError
        parser = _EntryPointParser(interpolation=None, strict=True)
        parser.read_string(content.decode("utf-8"))
        if parser.defaults() or ENTRY_POINT_GROUP not in parser:
            raise ValueError
        return [
            metadata.EntryPoint(name=name, value=value, group=ENTRY_POINT_GROUP)
            for name, value in parser[ENTRY_POINT_GROUP].items()
        ]
    except Exception:
        raise AdapterError("adapter_unavailable") from None


def _entry_origin(entry: metadata.EntryPoint, admitted: dict[Path, str]) -> str:
    """Reject shadowed or already foreign-loaded entry modules before execution.

    This is an origin check for trusted Python code, not a sandbox. Dependencies
    and Python's process-wide import machinery remain part of that trust boundary.
    """
    try:
        if type(entry.value) is not str or not re.fullmatch(
            r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*:[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*",
            entry.value,
        ):
            raise ValueError
        module_name = entry.value.split(":", 1)[0]
        search = None
        for index in range(1, len(module_name.split(".")) + 1):
            name = ".".join(module_name.split(".")[:index])
            spec = importlib.machinery.PathFinder.find_spec(name, search)
            if (
                spec is None
                or not spec.origin
                or Path(spec.origin) not in admitted
                or type(spec.loader) is not importlib.machinery.SourceFileLoader
            ):
                raise ValueError
            if name in sys.modules and (
                not _IMPORTED or _MODULES.get(name) is not sys.modules[name]
            ):
                raise ValueError
            search = spec.submodule_search_locations
            if index < len(module_name.split(".")) and not search:
                raise ValueError
        top_level = module_name.split(".")[0]
        for name, module in tuple(sys.modules.items()):
            if name == top_level or name.startswith(top_level + "."):
                if not _IMPORTED or _MODULES.get(name) is not module:
                    raise ValueError
        return top_level
    except Exception:
        raise AdapterError("adapter_artifact_changed") from None


class _VerifiedSourceLoader(importlib.machinery.SourceFileLoader):
    """Compile pinned source bytes, never a timestamp-trusted bytecode cache."""

    def __init__(self, name: str, path: str, expected: str):
        super().__init__(name, path)
        self.expected = expected

    def exec_module(self, module):
        super().exec_module(module)
        _MODULES[module.__name__] = module

    def get_code(self, fullname: str):
        try:
            content = _read_artifact(Path(self.path))
            if hashlib.sha256(content).hexdigest() != self.expected:
                raise ValueError
            return self.source_to_code(content, self.path)
        except BaseException:
            raise AdapterError("adapter_artifact_changed") from None


class _VerifiedImportFinder:
    """Keep eager and lazy imports within a registered package on verified source.

    Other dependencies retain ordinary Python loading. Enabled adapters are trusted
    code and can change process state; this finder is not a security sandbox.
    """

    def __init__(self):
        self.sources: dict[str, dict[Path, str]] = {}

    def find_spec(self, fullname: str, path=None, target=None):
        admitted = self.sources.get(fullname.split(".")[0])
        if admitted is None:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if (
            spec is None
            or not spec.origin
            or Path(spec.origin) not in admitted
            or type(spec.loader) is not importlib.machinery.SourceFileLoader
        ):
            raise AdapterError("adapter_artifact_changed")
        spec.loader = _VerifiedSourceLoader(fullname, spec.origin, admitted[Path(spec.origin)])
        return spec


_SOURCE_FINDER = _VerifiedImportFinder()


def _loaded_origins(top_level: str, admitted: dict[Path, str]) -> None:
    for name, module in tuple(sys.modules.items()):
        if name != top_level and not name.startswith(top_level + "."):
            continue
        spec = getattr(module, "__spec__", None)
        if (
            spec is None
            or not spec.origin
            or Path(spec.origin) not in admitted
            or getattr(module, "__file__", None) != spec.origin
            or type(spec.loader) is not _VerifiedSourceLoader
        ):
            raise AdapterError("adapter_artifact_changed")
        if name in _MODULES and _MODULES[name] is not module:
            raise AdapterError("adapter_artifact_changed")
        _MODULES[name] = module


def _declaration(value: Any, depth: int = 0) -> Any:
    """Copy only plain declarative values, without coercion or user object hooks."""
    if depth > 10:
        raise ValueError
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) in (list, tuple):
        if len(value) > 128:
            raise ValueError
        return [_declaration(item, depth + 1) for item in value]
    if type(value) is dict:
        if len(value) > 128 or any(type(key) is not str for key in value):
            raise ValueError
        return {key: _declaration(item, depth + 1) for key, item in value.items()}
    raise ValueError


def _dataclass_declaration(value: BalanceSpec | CompatibilityPolicy) -> dict[str, Any]:
    return {field.name: _declaration(getattr(value, field.name)) for field in fields(value)}


def _definition_metadata(definition: AdapterDefinition) -> dict[str, Any]:
    try:
        if (
            type(definition) is not AdapterDefinition
            or type(definition.api_version) is not int
            or definition.api_version != API_VERSION
        ):
            raise AdapterError("adapter_incompatible")
        if type(definition.policy) is not CompatibilityPolicy:
            raise ValueError
        for formats, cls in (
            (definition.csv_formats, CsvFormat),
            (definition.pdf_formats, PdfFormat),
            (definition.investment_formats, InvestmentFormat),
        ):
            if (
                type(formats) is not tuple
                or len(formats) > 64
                or any(type(fmt) is not cls for fmt in formats)
            ):
                raise ValueError
        from ledger.parsers.base import BaseParser

        for fmt in definition.csv_formats:
            if not isinstance(fmt.parser, type) or not issubclass(fmt.parser, BaseParser):
                raise ValueError
            if fmt.format_id != fmt.parser.FORMAT_NAME or fmt.institution != fmt.parser.INSTITUTION:
                raise ValueError
        for pdf in definition.pdf_formats:
            if type(pdf.headers) is not tuple or any(
                not callable(fn) for fn in (pdf.recognizes, pdf.parse, pdf.account_slug)
            ):
                raise ValueError
            if pdf.balance_spec is not None and type(pdf.balance_spec) is not BalanceSpec:
                raise ValueError
        for investment in definition.investment_formats:
            if (
                type(investment.capabilities) is not frozenset
                or any(type(c) is not InvestmentCapability for c in investment.capabilities)
                or type(investment.evidence_only_aliases) is not tuple
                or any(
                    not callable(fn)
                    for fn in (
                        investment.recognizes_text,
                        investment.parse,
                        investment.normalize_alias,
                        investment.period_from_text,
                        investment.matches_account,
                    )
                )
            ):
                raise ValueError
        info = {
            "adapter_id": definition.adapter_id,
            "api_version": definition.api_version,
            "layout_revision": definition.layout_revision,
            "csv_formats": [
                {"format_id": f.format_id, "institution": f.institution}
                for f in definition.csv_formats
            ],
            "pdf_formats": [
                {
                    "format_id": f.format_id,
                    "institution": f.institution,
                    "headers": list(f.headers),
                    "balance_spec": _dataclass_declaration(f.balance_spec)
                    if f.balance_spec
                    else None,
                    "archive_kind": f.archive_kind,
                }
                for f in definition.pdf_formats
            ],
            "investment_formats": [
                {
                    "format_id": f.format_id,
                    "institution": f.institution,
                    "source_layout": f.source_layout,
                    "capabilities": sorted(str(c) for c in f.capabilities),
                    "evidence_only_aliases": list(f.evidence_only_aliases),
                    "default": f.default,
                }
                for f in definition.investment_formats
            ],
            "policy": _dataclass_declaration(definition.policy),
        }
        _validate_metadata(info)
        return info
    except AdapterError as exc:
        code = (
            exc.code
            if type(exc) is AdapterError and type(exc.code) is str
            else "adapter_invalid_contract"
        )
        raise AdapterError(code) from None
    except BaseException:
        raise AdapterError("adapter_invalid_contract") from None


def _metadata_with_policy_defaults(info: dict[str, Any]) -> dict[str, Any]:
    """Compare old inert receipts without rewriting them or widening their meaning."""
    return {
        **info,
        "policy": {
            **info["policy"],
            "investment_asset_categories": info["policy"].get("investment_asset_categories", []),
            "reimbursement_merchants": info["policy"].get("reimbursement_merchants", []),
        },
    }


def _load_record(record: dict[str, Any]) -> AdapterDefinition:
    distribution = _distribution(record["distribution"])
    actual_digest, admitted = _artifact_inventory(distribution)
    if distribution.version != record["version"] or actual_digest != record["sha256"]:
        raise AdapterError("adapter_artifact_changed")
    identity = (_distribution_name(record["distribution"]), record["entry_point"])
    key = (*identity, record["version"], record["sha256"])
    if identity in _IMPORTED and _IMPORTED[identity] != actual_digest:
        raise AdapterError("adapter_artifact_changed")
    entries = [
        entry for entry in _verified_entries(admitted) if entry.name == record["entry_point"]
    ]
    if len(entries) != 1:
        raise AdapterError("adapter_unavailable")
    top_level = _entry_origin(entries[0], admitted)
    existing = _SOURCE_FINDER.sources.get(top_level)
    if existing is not None and existing != admitted:
        raise AdapterError("adapter_artifact_changed")
    _SOURCE_FINDER.sources[top_level] = admitted
    if _SOURCE_FINDER not in sys.meta_path:
        sys.meta_path.insert(0, _SOURCE_FINDER)
    if key not in _CACHE:
        try:
            definition = entries[0].load()()
        except BaseException:
            # Even a plugin-raised AdapterError may have forged args/__str__.
            raise AdapterError() from None
        _definition_metadata(definition)
    else:
        definition = _CACHE[key]
    if artifact_digest(distribution) != actual_digest:
        raise AdapterError("adapter_artifact_changed")
    _loaded_origins(top_level, admitted)
    if "metadata" in record and _canonical(
        _metadata_with_policy_defaults(_definition_metadata(definition))
    ) != _canonical(_metadata_with_policy_defaults(record["metadata"])):
        raise AdapterError("adapter_artifact_changed")
    _IMPORTED[identity] = actual_digest
    _CACHE[key] = definition
    return definition


def enable_distribution(name: str, entry_point: str, *, path: Path) -> dict[str, Any]:
    """Explicit local trust action. Installation is separate and never automatic."""
    if not _token(name) or not _token(entry_point):
        raise AdapterError("adapter_invalid_contract")
    try:
        selected = configuration_path(path)
    except Exception:
        raise AdapterError("adapter_unsafe_configuration") from None
    config = read_configuration(selected)
    distribution = _distribution(name)
    record: dict[str, Any] = {
        "distribution": _distribution_name(name),
        "version": distribution.version,
        "entry_point": entry_point,
        "sha256": artifact_digest(distribution),
        "enabled": True,
    }
    record["metadata"] = _definition_metadata(_load_record(record))
    identity = (record["distribution"], entry_point)
    for old in config["adapters"]:
        if (_distribution_name(old["distribution"]), old["entry_point"]) == identity:
            if {
                **old,
                "enabled": True,
                "metadata": _metadata_with_policy_defaults(old["metadata"]),
            } == record:
                if not old["enabled"]:
                    old["enabled"] = True
                    _write_configuration(selected, config)
                return record
            # A different artifact requires an explicit new config revision. Never
            # silently replace a pin while an existing reviewed import may use it.
            raise AdapterError("adapter_artifact_changed")
        if old["metadata"]["adapter_id"] == record["metadata"]["adapter_id"]:
            raise AdapterError("adapter_duplicate")
    config["adapters"].append(record)
    try:
        _validate_configuration(config)
    except AdapterError:
        raise
    except Exception:
        raise AdapterError("adapter_invalid_contract") from None
    _write_configuration(selected, config)
    return record


def _write_configuration(path: Path, config: dict[str, Any]) -> None:
    try:
        content = json.dumps(config, indent=2) + "\n"
        if len(content.encode()) > _CONFIG_LIMIT:
            raise ValueError
        with pin_existing_directory(path.parent) as parent:
            parent.require_owner_only()
            with parent.atomic_text(path.name) as handle:
                handle.write(content)
            parent.validate()
    except Exception:
        raise AdapterError("adapter_unsafe_configuration") from None


def disable_distribution(name: str, entry_point: str, *, path: Path) -> None:
    """Disable execution while retaining inert historical interpretation metadata."""
    if not _token(name) or not _token(entry_point):
        raise AdapterError("adapter_invalid_contract")
    try:
        selected = configuration_path(path)
    except Exception:
        raise AdapterError("adapter_unsafe_configuration") from None
    config = read_configuration(selected)
    matches = [
        r
        for r in config["adapters"]
        if (_distribution_name(r["distribution"]), r["entry_point"])
        == (_distribution_name(name), entry_point)
    ]
    if len(matches) != 1:
        raise AdapterError("adapter_not_enabled")
    matches[0]["enabled"] = False
    _write_configuration(selected, config)


@dataclass(frozen=True)
class LoadedAdapter:
    definition: AdapterDefinition
    binding: dict[str, Any]


def load_adapters() -> tuple[LoadedAdapter, ...]:
    config = read_configuration()
    result: list[LoadedAdapter] = []
    formats: set[tuple[str, str]] = set()
    for record in config["adapters"]:
        if not record["enabled"]:
            continue
        definition = _load_record(record)
        for kind, entries in (
            ("csv", definition.csv_formats),
            ("pdf", definition.pdf_formats),
            ("investment", definition.investment_formats),
        ):
            for entry in entries:
                identity = (kind, entry.format_id)
                if identity in formats:
                    raise AdapterError("adapter_duplicate")
                formats.add(identity)
        result.append(LoadedAdapter(definition, json.loads(json.dumps(record))))
    return tuple(result)


def configured_metadata() -> tuple[dict[str, Any], ...]:
    """Declarative historical policies only: never imports or executes a package."""
    return tuple(record["metadata"] for record in read_configuration()["adapters"])


def metadata_for_statement_type(statement_type: str) -> dict[str, Any] | None:
    matches = [
        item
        for metadata in configured_metadata()
        for item in metadata.get("pdf_formats", [])
        if item.get("format_id") == statement_type
    ]
    if len(matches) > 1:
        raise AdapterError("adapter_duplicate")
    return matches[0] if matches else None


def _merged_policy(records: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "account_aliases": {},
        "credit_institutions": [],
        "credit_account_tokens": [],
        "payment_tokens": {},
        "account_product_tokens": [],
        "classification_rules": {},
        "bill_tokens": [],
        "merchant_prefix_patterns": [],
        "investment_asset_categories": [],
        "reimbursement_merchants": [],
    }
    products: dict[str, str] = {}
    for record in records:
        policy = record["metadata"]["policy"]
        for alias, canonical in policy["account_aliases"].items():
            if alias in result["account_aliases"] and result["account_aliases"][alias] != canonical:
                raise AdapterError("adapter_duplicate")
            result["account_aliases"][alias] = canonical
        for token, product in policy["account_product_tokens"]:
            if token in products and products[token] != product:
                raise AdapterError("adapter_duplicate")
            products[token] = product
        result["credit_institutions"].extend(policy["credit_institutions"])
        result["credit_account_tokens"].extend(policy["credit_account_tokens"])
        result["account_product_tokens"].extend(policy.get("account_product_tokens", []))
        result["bill_tokens"].extend(policy.get("bill_tokens", []))
        result["merchant_prefix_patterns"].extend(policy.get("merchant_prefix_patterns", []))
        result["investment_asset_categories"].extend(policy.get("investment_asset_categories", []))
        result["reimbursement_merchants"].extend(policy.get("reimbursement_merchants", []))
        for institution, rules in policy.get("classification_rules", {}).items():
            if (
                institution in result["classification_rules"]
                and result["classification_rules"][institution] != rules
            ):
                raise AdapterError("adapter_duplicate")
            result["classification_rules"][institution] = rules
        for institution, tokens in policy.get("payment_tokens", {}).items():
            if (
                institution in result["payment_tokens"]
                and result["payment_tokens"][institution] != tokens
            ):
                raise AdapterError("adapter_duplicate")
            result["payment_tokens"][institution] = tokens
    result["investment_asset_categories"] = sorted(set(result["investment_asset_categories"]))
    if len(result["investment_asset_categories"]) > 128:
        raise ValueError
    result["reimbursement_merchants"] = sorted(
        {merchant.strip().upper() for merchant in result["reimbursement_merchants"]}
    )
    if len(result["reimbursement_merchants"]) > 128:
        raise ValueError
    return result


def compatibility_policy() -> dict[str, Any]:
    return _merged_policy(read_configuration()["adapters"])


@dataclass(frozen=True)
class CompatibilityPolicySnapshot:
    """Immutable interpretation inputs for one operation; no extension execution."""

    digest: str
    credit_institutions: frozenset[str]
    credit_account_tokens: tuple[str, ...]
    payment_tokens: tuple[tuple[str, tuple[str, ...]], ...]
    investment_asset_categories: tuple[str, ...] = ()
    reimbursement_merchants: frozenset[str] = frozenset()


def compatibility_policy_snapshot() -> CompatibilityPolicySnapshot:
    """Read one inert policy and bind its complete semantics to an opaque digest.

    Historical consumers retain this snapshot for their whole operation. The digest
    includes the complete merged policy, so a cursor cannot silently continue after
    external interpretation metadata changes while the database stays unchanged.
    """
    policy = compatibility_policy()
    return CompatibilityPolicySnapshot(
        digest=hashlib.sha256(_canonical(policy)).hexdigest(),
        credit_institutions=frozenset(policy["credit_institutions"]),
        credit_account_tokens=tuple(token.lower() for token in policy["credit_account_tokens"]),
        payment_tokens=tuple(
            (key, tuple(tokens)) for key, tokens in sorted(policy["payment_tokens"].items())
        ),
        investment_asset_categories=tuple(policy["investment_asset_categories"]),
        reimbursement_merchants=frozenset(policy["reimbursement_merchants"]),
    )


def get_pdf_format(format_id: str) -> PdfFormat:
    matches = [
        f
        for loaded in load_adapters()
        for f in loaded.definition.pdf_formats
        if f.format_id == format_id
    ]
    if len(matches) != 1:
        raise AdapterError("adapter_unknown_format" if not matches else "adapter_ambiguous")
    return matches[0]


def get_investment_format(institution: str | None = None) -> InvestmentFormat:
    formats = [f for loaded in load_adapters() for f in loaded.definition.investment_formats]
    matches = (
        [f for f in formats if (f.institution == institution or f.format_id == institution)]
        if institution
        else [f for f in formats if f.default]
    )
    if len(matches) != 1:
        raise AdapterError("adapter_unknown_format" if not matches else "adapter_ambiguous")
    return matches[0]


def snapshot_bindings() -> dict[str, Any]:
    """Capture an immutable interpretation closure separately from financial IDs."""
    from ledger.adapters.provenance import core_digest

    records = read_configuration()["adapters"]
    load_adapters()
    if _canonical(records) != _canonical(read_configuration()["adapters"]):
        raise AdapterError("import_binding_changed")
    return {"schema_version": 1, "core_sha256": core_digest(), "adapters": records}


def verify_bindings(binding: object, *, execute: bool = False) -> None:
    from ledger.adapters.provenance import core_digest

    if not isinstance(binding, dict) or set(binding) != {
        "schema_version",
        "core_sha256",
        "adapters",
    }:
        raise AdapterError("import_binding_missing")
    if (
        type(binding["schema_version"]) is not int
        or binding["schema_version"] != 1
        or type(binding["core_sha256"]) is not str
        or binding["core_sha256"] != core_digest()
    ):
        raise AdapterError("import_binding_changed")
    config = read_configuration()
    try:
        _validate_configuration({"schema_version": 1, "adapters": binding["adapters"]})
    except Exception:
        raise AdapterError("import_binding_changed") from None
    if _canonical(config["adapters"]) != _canonical(binding["adapters"]):
        raise AdapterError("import_binding_changed")
    for record in config["adapters"]:
        if not record["enabled"]:
            continue
        distribution = _distribution(record["distribution"])
        if (
            distribution.version != record["version"]
            or artifact_digest(distribution) != record["sha256"]
        ):
            raise AdapterError("adapter_artifact_changed")
    if execute:
        load_adapters()
