"""
Generic CSV parsing and explicitly enabled local extensions.

New parsers can be added by:
1. Create a new module in this package
2. Subclass BaseParser
3. Decorate with @register_parser

The registry will auto-detect the appropriate parser.
"""

from ledger.parsers.base import BaseParser, ParseContext
from ledger.parsers.registry import (
    create_parser,
    detect_format,
    get_all_parsers,
    get_parser,
    parse_file,
    register_parser,
)

__all__ = [
    "BaseParser",
    "ParseContext",
    "create_parser",
    "detect_format",
    "get_all_parsers",
    "get_parser",
    "parse_file",
    "register_parser",
]
