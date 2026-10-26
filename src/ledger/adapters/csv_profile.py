"""Explicit, format-only CSV mappings; no account or statement-state inference.

Inputs use UTF-8 (an initial UTF-8 BOM is accepted), RFC-style double quotes and
an explicit delimiter. ``header_row`` and ``source_row`` count logical CSV records
from one; quoted multiline values remain one record. Dates use an explicit numeric
four-digit-year format. Amounts are ungrouped ASCII decimals with at most two
fractional digits; currency symbols, exponents and implicit rounding are refused.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import io
import json
import os
import re
import secrets
import stat
from dataclasses import asdict, dataclass, fields
from datetime import date, datetime
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

from ledger.runtime import RuntimePathError, require_external_runtime_path
from ledger.utils.private_files import PinnedPrivateDirectory, pin_existing_directory

MAX_CSV_BYTES = 8 * 1024 * 1024
MAX_CSV_ROWS = 100_000
MAX_COLUMNS = 128
MAX_FIELD_CHARS = 4096
MAX_PROFILE_BYTES = 64 * 1024
MAX_HEADER_ROW = 100
DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%Y%m%d",
    "%m/%d/%Y",
    "%d/%m/%Y",
    "%m-%d-%Y",
    "%d-%m-%Y",
    "%d.%m.%Y",
)
PREVIEW_NOTICE = (
    "Unreconciled transaction export. No balances, statement coverage, or account "
    "lifecycle have been inferred."
)
_TOKEN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z")
_CURRENCY = re.compile(r"[A-Z]{3}\Z")
_AMOUNT = re.compile(r"[+-]?[0-9]{1,18}(?:\.[0-9]{1,2})?\Z")
_CODES = frozenset(
    {
        "profile_invalid",
        "profile_invalid_json",
        "profile_unsafe_path",
        "profile_io",
        "profile_exists",
        "profile_too_large",
        "csv_too_large",
        "csv_too_many_rows",
        "csv_encoding",
        "csv_malformed",
        "csv_headers",
        "csv_header_drift",
        "csv_empty",
        "csv_row_width",
        "csv_field_limit",
        "csv_date",
        "csv_description",
        "csv_amount",
        "csv_debit_credit",
        "csv_currency",
    }
)


class CsvProfileError(ValueError):
    """A bounded diagnostic that never interpolates private input or paths."""

    def __init__(self, code: str) -> None:
        self.code = code if code in _CODES else "profile_invalid"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True, init=False)
class CsvProfile:
    """An immutable exact layout contract; unknown fields are refused."""

    profile_id: str
    delimiter: str
    header_row: int
    headers: tuple[str, ...]
    date_column: str
    date_format: str
    description_column: str
    schema_version: int = 1
    amount_column: str | None = None
    sign_convention: str | None = None
    debit_column: str | None = None
    credit_column: str | None = None
    currency: str | None = None
    currency_column: str | None = None
    reference_column: str | None = None

    def __init__(self, **values: Any) -> None:
        required = {
            "profile_id",
            "delimiter",
            "header_row",
            "headers",
            "date_column",
            "date_format",
            "description_column",
        }
        names = {field.name for field in fields(type(self))}
        if set(values) - names or required - set(values):
            raise CsvProfileError("profile_invalid")
        normalized: dict[str, Any] = {field.name: field.default for field in fields(type(self))}
        normalized.update(values)
        _validate_profile(normalized)
        normalized["headers"] = tuple(normalized["headers"])
        for name, value in normalized.items():
            object.__setattr__(self, name, value)


@dataclass(frozen=True, slots=True)
class MappedRow:
    source_row: int
    transaction_date: date
    description: str
    amount: Decimal
    currency: str
    reference: str


def _headers_valid(headers: Any) -> bool:
    return (
        isinstance(headers, (tuple, list))
        and 1 <= len(headers) <= MAX_COLUMNS
        and all(
            type(value) is str
            and value.strip()
            and len(value) <= MAX_FIELD_CHARS
            and not any(ord(char) < 32 or ord(char) == 127 for char in value)
            for value in headers
        )
        and len(set(headers)) == len(headers)
    )


def _validate_profile(values: dict[str, Any]) -> None:
    def invalid() -> None:
        raise CsvProfileError("profile_invalid")

    if type(values["schema_version"]) is not int or values["schema_version"] != 1:
        invalid()
    if type(values["profile_id"]) is not str or not _TOKEN.fullmatch(values["profile_id"]):
        invalid()
    if values["delimiter"] not in (",", ";", "\t", "|"):
        invalid()
    if type(values["header_row"]) is not int or not 1 <= values["header_row"] <= MAX_HEADER_ROW:
        invalid()
    if not _headers_valid(values["headers"]):
        invalid()
    if values["date_format"] not in DATE_FORMATS:
        invalid()
    selected = []
    for name in (
        "date_column",
        "description_column",
        "amount_column",
        "debit_column",
        "credit_column",
        "currency_column",
        "reference_column",
    ):
        value = values[name]
        if value is None and name not in {"date_column", "description_column"}:
            continue
        if type(value) is not str or value not in values["headers"]:
            invalid()
        selected.append(value)
    if len(set(selected)) != len(selected):
        invalid()
    amount_mode = values["amount_column"] is not None
    if amount_mode:
        if values["debit_column"] is not None or values["credit_column"] is not None:
            invalid()
        if values["sign_convention"] not in ("inflow_positive", "outflow_positive"):
            invalid()
    elif (
        values["debit_column"] is None
        or values["credit_column"] is None
        or values["sign_convention"] is not None
    ):
        invalid()
    if (values["currency"] is None) == (values["currency_column"] is None):
        invalid()
    if values["currency"] is not None and (
        type(values["currency"]) is not str or not _CURRENCY.fullmatch(values["currency"])
    ):
        invalid()


def _canonical_json(profile: CsvProfile) -> bytes:
    if type(profile) is not CsvProfile:
        raise CsvProfileError("profile_invalid")
    return json.dumps(
        asdict(profile), sort_keys=True, ensure_ascii=True, separators=(",", ":")
    ).encode("utf-8")


def profile_digest(profile: CsvProfile) -> str:
    """Hash every format field, including explicit nulls, in canonical JSON."""
    return hashlib.sha256(_canonical_json(profile)).hexdigest()


def _check_quote_grammar(text: str, delimiter: str) -> None:
    # csv.reader(strict=True) still accepts a quote inside an unquoted field.
    # Reject that ambiguous grammar instead of quietly retaining the quote.
    state = "start"
    for char in text:
        if state == "quoted":
            if char == '"':
                state = "closed"
        elif state == "closed":
            if char == '"':
                state = "quoted"
            elif char == delimiter or char in "\r\n":
                state = "start"
            else:
                raise CsvProfileError("csv_malformed")
        elif char == '"':
            if state != "start":
                raise CsvProfileError("csv_malformed")
            state = "quoted"
        elif char == delimiter or char in "\r\n":
            state = "start"
        else:
            state = "plain"
    if state == "quoted":
        raise CsvProfileError("csv_malformed")


def _csv_records(content: bytes, delimiter: str) -> list[list[str]]:
    if type(content) is not bytes:
        raise CsvProfileError("csv_encoding")
    if len(content) > MAX_CSV_BYTES:
        raise CsvProfileError("csv_too_large")
    if delimiter not in (",", ";", "\t", "|"):
        raise CsvProfileError("profile_invalid")
    try:
        text = content.decode("utf-8-sig")
    except UnicodeError:
        raise CsvProfileError("csv_encoding") from None
    if "\x00" in text:
        raise CsvProfileError("csv_malformed")
    _check_quote_grammar(text, delimiter)
    records: list[list[str]] = []
    try:
        for row in csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True):
            if len(records) >= MAX_CSV_ROWS:
                raise CsvProfileError("csv_too_many_rows")
            if len(row) > MAX_COLUMNS or any(len(value) > MAX_FIELD_CHARS for value in row):
                raise CsvProfileError("csv_field_limit")
            records.append(row)
    except csv.Error:
        raise CsvProfileError("csv_malformed") from None
    return records


def inspect_csv_headers(content: bytes, *, delimiter: str, header_row: int) -> tuple[str, ...]:
    """Return the exact explicitly selected header; never detect or normalize it."""
    if type(header_row) is not int or not 1 <= header_row <= MAX_HEADER_ROW:
        raise CsvProfileError("profile_invalid")
    records = _csv_records(content, delimiter)
    if len(records) < header_row or not _headers_valid(records[header_row - 1]):
        raise CsvProfileError("csv_headers")
    return tuple(records[header_row - 1])


def _decimal(value: str) -> Decimal:
    if not _AMOUNT.fullmatch(value):
        raise CsvProfileError("csv_amount")
    # Explicit precision avoids process-global Decimal context changing an amount.
    with localcontext() as context:
        context.prec = 40
        amount = Decimal(value).quantize(Decimal("0.01"))
    if not amount.is_finite():
        raise CsvProfileError("csv_amount")
    return amount


def parse_csv_bytes(content: bytes, profile: CsvProfile) -> tuple[MappedRow, ...]:
    """Validate the entire export or fail; no partial rows escape on an error."""
    if type(profile) is not CsvProfile:
        raise CsvProfileError("profile_invalid")
    records = _csv_records(content, profile.delimiter)
    if len(records) < profile.header_row:
        raise CsvProfileError("csv_headers")
    headers = records[profile.header_row - 1]
    if not _headers_valid(headers):
        raise CsvProfileError("csv_headers")
    if tuple(headers) != profile.headers:
        raise CsvProfileError("csv_header_drift")
    if len(records) == profile.header_row:
        raise CsvProfileError("csv_empty")
    mapped = []
    for row_number, cells in enumerate(records[profile.header_row :], start=profile.header_row + 1):
        if len(cells) != len(headers):
            raise CsvProfileError("csv_row_width")
        row = dict(zip(headers, cells, strict=True))
        try:
            parsed_date = datetime.strptime(row[profile.date_column], profile.date_format).date()
        except ValueError:
            raise CsvProfileError("csv_date") from None
        if parsed_date.strftime(profile.date_format) != row[profile.date_column]:
            raise CsvProfileError("csv_date")
        description = row[profile.description_column]
        if not description.strip():
            raise CsvProfileError("csv_description")
        if profile.amount_column is not None:
            amount = _decimal(row[profile.amount_column])
            if profile.sign_convention == "outflow_positive":
                amount = amount.copy_negate()
        else:
            assert profile.debit_column is not None and profile.credit_column is not None
            debit_text, credit_text = row[profile.debit_column], row[profile.credit_column]
            if not debit_text and not credit_text:
                raise CsvProfileError("csv_amount")
            debit = _decimal(debit_text) if debit_text else Decimal("0.00")
            credit = _decimal(credit_text) if credit_text else Decimal("0.00")
            if debit < 0 or credit < 0 or (debit != 0 and credit != 0):
                raise CsvProfileError("csv_debit_credit")
            amount = debit.copy_negate() if debit else credit
        if profile.currency is not None:
            currency = profile.currency
        else:
            assert profile.currency_column is not None
            currency = row[profile.currency_column]
        if not _CURRENCY.fullmatch(currency):
            raise CsvProfileError("csv_currency")
        mapped.append(
            MappedRow(
                source_row=row_number,
                transaction_date=parsed_date,
                description=description,
                amount=amount,
                currency=currency,
                reference=row[profile.reference_column] if profile.reference_column else "",
            )
        )
    return tuple(mapped)


def _profile_path(path: Path) -> Path:
    try:
        path = Path(path).expanduser()
        if ".." in path.parts:
            raise CsvProfileError("profile_unsafe_path")
        path = require_external_runtime_path(path)
        for component in (path, *path.parents):
            if component.is_symlink():
                raise CsvProfileError("profile_unsafe_path")
        if path.name in {"", ".", ".."}:
            raise CsvProfileError("profile_unsafe_path")
        return path
    except RuntimePathError, OSError, TypeError, ValueError:
        raise CsvProfileError("profile_unsafe_path") from None


def _check_parent(parent: PinnedPrivateDirectory) -> None:
    parent.require_owner_only()
    if os.fstat(parent.descriptor).st_uid != os.geteuid():
        raise CsvProfileError("profile_unsafe_path")


def _check_file(descriptor: int) -> None:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
    ):
        raise CsvProfileError("profile_unsafe_path")
    if metadata.st_size > MAX_PROFILE_BYTES:
        raise CsvProfileError("profile_too_large")


def _json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise CsvProfileError("profile_invalid_json")
        result[key] = value
    return result


def load_profile(path: Path) -> CsvProfile:
    """Load one bounded private profile, refusing links and permission drift."""
    path = _profile_path(path)
    try:
        with pin_existing_directory(path.parent) as parent:
            _check_parent(parent)
            with parent.pin_private_file(path.name) as pinned:
                _check_file(pinned.descriptor)
                content = bytearray()
                while len(content) <= MAX_PROFILE_BYTES:
                    chunk = os.read(
                        pinned.descriptor, min(8192, MAX_PROFILE_BYTES + 1 - len(content))
                    )
                    if not chunk:
                        break
                    content.extend(chunk)
                if len(content) > MAX_PROFILE_BYTES:
                    raise CsvProfileError("profile_too_large")
                _check_file(pinned.descriptor)
                _check_parent(parent)
    except CsvProfileError:
        raise
    except OSError:
        raise CsvProfileError("profile_unsafe_path") from None
    try:
        values = json.loads(content, object_pairs_hook=_json_object)
        if type(values) is not dict:
            raise CsvProfileError("profile_invalid_json")
        return CsvProfile(**values)
    except CsvProfileError:
        raise
    except ValueError, TypeError, UnicodeError, RecursionError:
        raise CsvProfileError("profile_invalid_json") from None


def save_profile(path: Path, profile: CsvProfile) -> None:
    """Atomically publish a new private revision; the parent must already be 0700.

    A completed temporary file is linked into an absent destination, then its
    temporary name is removed. Existing files are never overwritten or chmodded.
    """
    path = _profile_path(path)
    content = _canonical_json(profile) + b"\n"
    if len(content) > MAX_PROFILE_BYTES:
        raise CsvProfileError("profile_too_large")
    try:
        with pin_existing_directory(path.parent) as parent:
            _check_parent(parent)
            temporary = f".csv-profile-{secrets.token_hex(16)}.tmp"
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent.descriptor,
            )
            try:
                os.fchmod(descriptor, 0o600)
                with os.fdopen(os.dup(descriptor), "wb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                _check_file(descriptor)
                _check_parent(parent)
                metadata = os.stat(temporary, dir_fd=parent.descriptor, follow_symlinks=False)
                pinned = os.fstat(descriptor)
                if (metadata.st_dev, metadata.st_ino) != (pinned.st_dev, pinned.st_ino):
                    raise CsvProfileError("profile_unsafe_path")
                os.link(
                    temporary,
                    path.name,
                    src_dir_fd=parent.descriptor,
                    dst_dir_fd=parent.descriptor,
                    follow_symlinks=False,
                )
                os.unlink(temporary, dir_fd=parent.descriptor)
                _check_file(descriptor)
                _check_parent(parent)
                os.fsync(parent.descriptor)
            finally:
                os.close(descriptor)
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=parent.descriptor)
    except CsvProfileError:
        raise
    except FileExistsError:
        raise CsvProfileError("profile_exists") from None
    except OSError:
        raise CsvProfileError("profile_unsafe_path") from None
