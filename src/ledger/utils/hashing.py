"""
Hashing utilities for generating stable identifiers.

Uses SHA-256 (not SHA-1) for better collision resistance.
"""

import hashlib
import logging
import re
from collections.abc import Iterable
from pathlib import Path

logger = logging.getLogger(__name__)

# Sentinel stored in parser_version when provenance could not be determined. Rows
# carrying it cannot be checked for drift, so `ledger db doctor` reports them.
UNKNOWN_VERSION = "unknown"

# Whitespace normalization
WHITESPACE = re.compile(r"\s+")

_SCRIPT_VERSION_FILES = (
    "core/__init__.py",
    "core/enums.py",
    "core/models.py",
    "finance_policy.py",
    "imports/pdf/statement_pdf_to_csv/__init__.py",
    "imports/pdf/statement_pdf_to_csv/__main__.py",
    "imports/pdf/statement_pdf_to_csv/_common.py",
    "imports/pdf/statement_pdf_to_csv/_dispatch.py",
    # The import layer that decides which parsed rows are written and how existing
    # rows are replaced. It changes what lands in `transactions` just as directly as
    # a parser does, so leaving it out let stored provenance claim a version whose
    # behaviour had in fact changed.
    "imports/pdf/processing.py",
    "parsers/__init__.py",
    "parsers/base.py",
    "parsers/canonical.py",
    "adapters/contracts.py",
    "adapters/registry.py",
    "adapters/csv_profile.py",
    "adapters/provenance.py",
    "adapters/mapped_export.py",
    "parsers/registry.py",
    "processing/__init__.py",
    "processing/identity.py",
    "processing/pipeline.py",
    "utils/__init__.py",
    "utils/dates.py",
    "utils/fuzzy.py",
    "utils/hashing.py",
    "utils/money.py",
    "utils/text.py",
)


def normalize_text(text: str) -> str:
    """Normalize text for consistent hashing."""
    return WHITESPACE.sub(" ", (text or "").strip().lower())


def sha256_hex(data: str) -> str:
    """Compute SHA-256 hash of string."""
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def stable_id(*parts: str) -> str:
    """
    Generate a stable identifier from parts.

    Each part is normalized via ``normalize_text`` (lowercased, whitespace
    collapsed, stripped), the normalized parts are joined with |, and the
    result is hashed. The result is a 40-character hex string (truncated
    SHA-256).

    Because inputs are lowercased before hashing, case in the input does not
    affect the result -- e.g. "MCDONALDS" and "mcdonalds" produce the same id.

    Example:
        >>> # "MCDONALDS" is normalized to "mcdonalds" before hashing
        >>> stable_id("example-bank", "chequing", "2025-01-15", "-100.00", "MCDONALDS")
        "a1b2c3..."
    """
    normalized = [normalize_text(p or "") for p in parts]
    joined = "|".join(normalized)
    return sha256_hex(joined)[:40]


def fingerprint(*parts: str) -> str:
    """
    Generate a content fingerprint (shorter hash for collision detection).

    Different from stable_id in that it's meant to detect
    when two transactions with the same ID have different content.
    """
    normalized = [normalize_text(p or "") for p in parts]
    joined = "|".join(normalized)
    return sha256_hex(joined)[:32]


def currency_identity_suffix(currency: str | None) -> tuple[str, ...]:
    """Return the optional identity part that discriminates non-CAD rows.

    CAD and legacy blank currency values contribute no part at all so their existing
    transaction IDs and fingerprints remain byte-for-byte stable.
    """
    normalized = (currency or "").strip().upper()
    if not normalized or normalized == "CAD":
        return ()
    return (f"currency:{normalized}",)


def file_hash(path: Path) -> str:
    """
    Compute hash of a file's contents.

    Used for parser version tracking.
    """
    try:
        content = path.read_bytes()
        return hashlib.sha256(content).hexdigest()[:40]
    except Exception:
        return "unknown"


def _hash_version_files(package_root: Path, relative_paths: Iterable[str | Path]) -> str:
    """Hash a complete, location-independent set of provenance inputs.

    Returns ``UNKNOWN_VERSION`` when the set cannot be hashed completely. Each such
    exit is logged: an un-loggable degradation here is invisible, and every stored
    row would then claim a provenance that means nothing.
    """
    normalized_paths: list[str] = []
    try:
        for raw_path in relative_paths:
            relative_path = Path(raw_path)
            if relative_path.is_absolute() or ".." in relative_path.parts:
                logger.warning("Provenance hash skipped: unsafe relative path %r", str(raw_path))
                return UNKNOWN_VERSION
            normalized_paths.append(relative_path.as_posix())
        if not normalized_paths:
            logger.warning("Provenance hash skipped: no version files declared")
            return UNKNOWN_VERSION

        digest = hashlib.sha256()
        for relative_name in sorted(normalized_paths):
            source_path = package_root / relative_name
            if not source_path.is_file():
                logger.warning("Provenance hash skipped: missing version file %s", relative_name)
                return UNKNOWN_VERSION
            path_bytes = relative_name.encode("utf-8")
            source_bytes = source_path.read_bytes()
            if not source_bytes:
                logger.warning("Provenance hash skipped: empty version file %s", relative_name)
                return UNKNOWN_VERSION
            digest.update(len(path_bytes).to_bytes(8, "big"))
            digest.update(path_bytes)
            digest.update(len(source_bytes).to_bytes(8, "big"))
            digest.update(source_bytes)
        return digest.hexdigest()[:40]
    except Exception:
        logger.warning("Provenance hash failed", exc_info=True)
        return UNKNOWN_VERSION


def script_version() -> str:
    """Return a complete parser/extractor/pipeline provenance hash."""
    try:
        import ledger

        if ledger.__file__ is None:
            logger.warning("Provenance hash skipped: ledger package has no __file__")
            return UNKNOWN_VERSION
        return _hash_version_files(Path(ledger.__file__).resolve().parent, _SCRIPT_VERSION_FILES)
    except Exception:
        logger.warning("Provenance hash failed to locate the ledger package", exc_info=True)
        return UNKNOWN_VERSION
