"""Atomic owner-only JSON serialization helpers."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from ledger.utils.private_files import write_private_text_atomic

# Retain ``ledger.utils.json_io.os.replace`` as the established test/patch target.
# ``private_files`` imports the same stdlib module singleton, so patching this
# attribute still intercepts the atomic publication call.
_OS_REPLACE_PATCH_TARGET = os.replace


def utc_now() -> str:
    """Return the current UTC time as a compact ISO-8601 string (no microseconds)."""
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def decimal_json(value: Any) -> Any:
    """JSON ``default`` hook: serialise Decimal, Path, datetime, and date."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return value


def json_safe(value: Any) -> Any:
    """Recursively convert a value into something ``json.dumps`` accepts.

    ``Decimal`` becomes ``float`` (not ``str``, unlike :func:`decimal_json`): this is
    the convention for values bound for a SQLAlchemy ``JSON`` column or a reporting
    payload, whose consumers read them as numbers. On-disk manifests use
    ``decimal_json`` instead, which keeps exactness by emitting a string.

    Returns a new structure; the input is never mutated. Tuples become lists, and
    non-string mapping keys are stringified.
    """
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [json_safe(item) for item in value]
    if isinstance(value, set | frozenset):
        return [json_safe(item) for item in sorted(value, key=repr)]
    return value


def dumps_json_column(value: Any) -> str:
    """``json_serializer`` for the SQLAlchemy engine.

    Every ``JSON`` column in the schema is one stray ``Decimal``/``date`` away from a
    hard ``TypeError`` on flush -- which is exactly how a single over-budget category
    took down insight refresh, PDF import finalize, and rules apply. Normalising here
    means no column can hit that, regardless of what a caller passes.
    """
    return json.dumps(json_safe(value))


def write_json_atomic(path: Path, payload: Any) -> None:
    """Write *payload* as owner-only JSON via a same-directory atomic replace.

    A crash or concurrent reader will see either the old file or the new file,
    never a half-written/truncated intermediate.  The temp is written in the same
    directory so the final os.replace is guaranteed to be on a single filesystem
    (cross-device renames are not atomic on POSIX).

    The shared private-file primitive creates unique ``0600`` temporaries, fsyncs
    their contents, and keeps managed destination directories at ``0700``.
    """
    write_private_text_atomic(
        path,
        json.dumps(payload, indent=2, default=decimal_json),
        encoding="utf-8",
    )
