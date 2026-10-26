"""
Parser registry with plugin pattern.

Parsers register themselves using the @register_parser decorator.
The registry auto-detects the appropriate parser for a CSV file.
"""

import io
from collections.abc import Iterator
from pathlib import Path

from ledger.adapters.contracts import AdapterError
from ledger.adapters.registry import load_adapters
from ledger.config import AppConfig
from ledger.core import ExceptionRecord, Transaction
from ledger.parsers.base import BaseParser, ParseContext

# Global registry
_PARSERS: dict[str, type[BaseParser]] = {}


def register_parser(cls: type[BaseParser]) -> type[BaseParser]:
    """
    Decorator to register a parser class.

    Usage:
        @register_parser
        class ExampleParser(BaseParser):
            FORMAT_NAME = "example"
            ...
    """
    if not cls.FORMAT_NAME:
        raise ValueError(f"Parser {cls.__name__} must define FORMAT_NAME")
    existing = _PARSERS.get(cls.FORMAT_NAME)
    if existing is not None and existing is not cls:
        # Fail loudly on a FORMAT_NAME collision rather than silently shadowing the
        # previously-registered parser (which could change detection results).
        raise ValueError(
            f"Parser FORMAT_NAME collision: {cls.__name__} and {existing.__name__} "
            f"both register as '{cls.FORMAT_NAME}'"
        )
    _PARSERS[cls.FORMAT_NAME] = cls
    return cls


def get_parser(format_name: str) -> type[BaseParser] | None:
    """Get parser class by format name."""
    return get_all_parsers().get(format_name)


def get_all_parsers() -> dict[str, type[BaseParser]]:
    """Get all registered parsers."""
    from ledger.parsers.canonical import CanonicalParser

    parsers: dict[str, type[BaseParser]] = {CanonicalParser.FORMAT_NAME: CanonicalParser}
    for name, parser in _PARSERS.items():
        if name in parsers and parsers[name] is not parser:
            raise AdapterError("adapter_duplicate")
        parsers[name] = parser
    for loaded in load_adapters():
        for fmt in loaded.definition.csv_formats:
            if fmt.format_id in parsers and parsers[fmt.format_id] is not fmt.parser:
                raise AdapterError("adapter_duplicate")
            parsers[fmt.format_id] = fmt.parser
    return parsers


def detect_format(headers: list[str]) -> str | None:
    """
    Detect file format from headers.

    Returns format name or None if unknown.
    """
    header_set = {h.strip() for h in headers}

    if len(header_set) != len(headers) or not all(header_set):
        raise AdapterError("adapter_ambiguous")
    try:
        matches = [
            name for name, parser in get_all_parsers().items() if parser.can_parse(header_set)
        ]
    except AdapterError:
        raise
    except Exception:
        raise AdapterError() from None
    if len(matches) > 1:
        raise AdapterError("adapter_ambiguous")
    return matches[0] if matches else None


def create_parser(
    format_name: str,
    config: AppConfig,
    source_root: Path,
    file_path: Path,
    include_raw: bool = False,
    account_slug: str | None = None,
) -> BaseParser | None:
    """
    Create a parser instance for the given format.

    Returns None if format not found.
    """
    parser_cls = get_parser(format_name)
    if not parser_cls:
        return None

    ctx = ParseContext(
        config=config,
        source_root=source_root,
        file_path=file_path,
        include_raw=include_raw,
        account_slug=account_slug,
    )

    try:
        return parser_cls(ctx)
    except Exception:
        raise AdapterError() from None


def parse_file(
    config: AppConfig,
    source_root: Path,
    file_path: Path,
    include_raw: bool = False,
    account_slug: str | None = None,
) -> Iterator[Transaction | ExceptionRecord]:
    """
    Parse a CSV file, auto-detecting the format.

    Yields Transaction or ExceptionRecord objects.
    """
    import csv

    # One immutable read feeds recognition and parsing; do not reopen a changed file.
    content = file_path.read_bytes().decode("utf-8-sig")
    headers = next(csv.reader(io.StringIO(content, newline="")), [])
    try:
        format_name = detect_format(headers)
    except AdapterError as exc:
        yield ExceptionRecord(
            source_file=file_path.name,
            source_path=str(file_path.relative_to(source_root)),
            source_row=1,
            reason=exc.code,
        )
        return
    if not format_name:
        yield ExceptionRecord(
            source_file=file_path.name,
            source_path=str(file_path.relative_to(source_root)),
            source_row=1,
            reason="unknown_format",
            # Headers are normally column names, but a misidentified file may put a
            # data row here (merchant/amount). Honour the same include_raw privacy
            # gate the per-row path uses (BaseParser._exception).
            raw_data={"headers": headers} if include_raw else {},
        )
        return

    # Create parser and parse
    try:
        parser = create_parser(
            format_name,
            config,
            source_root,
            file_path,
            include_raw,
            account_slug=account_slug,
        )
    except AdapterError as exc:
        yield ExceptionRecord(
            source_file=file_path.name,
            source_path=str(file_path.relative_to(source_root)),
            source_row=1,
            reason=exc.code,
        )
        return
    if not parser:
        yield ExceptionRecord(
            source_file=file_path.name,
            source_path=str(file_path.relative_to(source_root)),
            source_row=1,
            reason=f"no_parser_for_{format_name}",
        )
        return

    # Parse file
    with io.StringIO(content, newline="") as f:
        dict_reader = csv.DictReader(f)
        # detect_format() strips whitespace from header cells before matching, so a CSV
        # with padded headers (" Account Type ,...") is still recognized. Mirror that
        # canonicalization for row access — otherwise DictReader keeps the raw padded
        # keys and every parser row.get("Account Type")/row.get("Amount") returns None,
        # failing the whole file. Accessing .fieldnames consumes the header row once;
        # reassigning it leaves data rows keyed by the stripped names.
        if dict_reader.fieldnames is not None:
            dict_reader.fieldnames = [name.strip() for name in dict_reader.fieldnames]
        try:
            for parsed in parser.parse(dict_reader):
                if not isinstance(parsed, (Transaction, ExceptionRecord)):
                    raise AdapterError("adapter_invalid_contract")
                yield parsed
        except Exception:
            yield ExceptionRecord(
                source_file=file_path.name,
                source_path=str(file_path.relative_to(source_root)),
                source_row=0,
                reason="adapter_failed",
            )
