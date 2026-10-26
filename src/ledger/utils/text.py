"""
Text normalization utilities.
"""

import re
from collections.abc import Mapping
from typing import Any

WHITESPACE = re.compile(r"\s+")
MONTH_FOLDER = re.compile(r"^20\d{2}-(?:0[1-9]|1[0-2])$")
# Money-like tokens: an optional sign/currency symbol, digits with optional grouping,
# and exactly two decimals (e.g. -4250.00, $1,234.56). Targeted at amounts so plain
# integers (row numbers, counts) in diagnostic text are left intact.
_MONEY_TOKEN = re.compile(r"[-+]?\$?\d[\d,]*\.\d{2}\b")


def redact_money(text: str) -> str:
    """Replace exact money amounts with ``<redacted>``.

    Used to keep amounts (financial PII under this project's threat model) out of
    exception reasons, manifests, and other diagnostic artifacts while preserving
    the surrounding text (violation type, field names, source coordinates).
    """
    return _MONEY_TOKEN.sub("<redacted>", text)


_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@")


def neutralize_csv_cell(value):
    """Defuse a CSV formula-injection payload in a string cell.

    A merchant/description captured from a crafted or corrupt statement PDF could
    begin with ``= + - @`` (or a control char); Excel/Sheets/LibreOffice would then
    execute it as a formula (data exfiltration via HYPERLINK/WEBSERVICE, etc.) when
    the exported CSV is opened. Prefix a single quote so the cell is treated as text.

    Non-strings are returned unchanged, and a plain negative number (e.g. ``-50.00``)
    is left intact so amount columns are not mangled.
    """
    if not isinstance(value, str) or not value:
        return value
    first = value[0]
    if first in ("\t", "\r", "\n"):
        return "'" + value
    if first in _CSV_FORMULA_PREFIXES:
        try:
            float(value.replace(",", ""))
            return value  # a numeric string, not a formula
        except ValueError:
            return "'" + value
    return value


def neutralize_csv_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Return a CSV-safe copy of a mapping by neutralizing every string cell."""
    return {key: neutralize_csv_cell(value) for key, value in row.items()}


def normalize_whitespace(text: str | None) -> str:
    """Normalize whitespace in text."""
    if text is None:
        return ""
    return WHITESPACE.sub(" ", text.strip())


def is_month_folder(name: str) -> bool:
    """Check if folder name is YYYY-MM format."""
    return bool(MONTH_FOLDER.fullmatch(name))


def clean_account_name(name: str, aliases: dict[str, str] | None = None) -> str:
    """
    Clean and normalize account name.

    Applies aliases if provided.
    """
    cleaned = name.strip().lower()
    if aliases:
        cleaned = aliases.get(cleaned, cleaned)
    return cleaned
