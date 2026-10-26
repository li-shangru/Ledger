"""Compatibility exports for the canonical JSON utility leaf."""

from ledger.utils.json_io import decimal_json, utc_now, write_json_atomic

__all__ = ["utc_now", "decimal_json", "write_json_atomic"]
