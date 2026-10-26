"""Format-independent safety gate for ``scripts/rebuild-db``.

``scripts/rebuild-db`` builds a fresh SQLite DB from a CSV archive and only
promotes it over the live ``ledger.db`` if the build is trustworthy. Two things
make a rebuild unsafe to promote:

  1. It produced zero rows (nothing was parsed).
  2. Any row failed to parse (a *partial* loss — some transactions silently
     dropped).

The count of parse exceptions is persisted into ``import_runs.errors`` on every
build regardless of the output FORMATS (see ``DatabaseWriter.write_result``), so
this check reads that column rather than the CSV-only ``exceptions_all.csv``
artifact. That keeps the gate correct even under ``LEDGER_FORMATS=parquet``.

The check FAILS CLOSED: if the ``import_runs`` table / ``errors`` column is
missing, or any query errors, the rebuilt DB is treated as UNSAFE (refuse),
never silently safe.

Usable as a thin CLI so the shell script can invoke it without an inline
heredoc::

    python -m ledger.maintenance.rebuild_guard "$TMP_DB"

exits non-zero and prints the refusal reason when unsafe.
"""

from __future__ import annotations

import contextlib
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RebuildSafety:
    """Result of the pre-promote safety check for a rebuilt DB.

    ``ok`` is True only when the DB has at least one transaction/transfer AND
    recorded zero parse exceptions. ``message`` is a user-facing line suitable
    for printing (success or the specific refusal reason).
    """

    ok: bool
    message: str
    transactions: int
    transfers: int
    exceptions: int


def check_rebuild_safe(db_path: Path | str) -> RebuildSafety:
    """Decide whether a freshly-rebuilt DB is safe to promote.

    Safe requires both:
      (a) ``transactions + transfers > 0``, and
      (b) ``SUM(import_runs.errors) == 0``.

    Fails closed: a missing ``import_runs`` table/``errors`` column or any query
    error yields an UNSAFE result rather than trusting the DB.
    """
    db_path = Path(db_path)

    try:
        with contextlib.closing(sqlite3.connect(db_path)) as conn:
            transactions = int(conn.execute("select count(*) from transactions").fetchone()[0])
            transfers = int(conn.execute("select count(*) from transfers").fetchone()[0])
            exceptions = int(
                conn.execute("select coalesce(sum(errors), 0) from import_runs").fetchone()[0]
            )
    except (sqlite3.Error, TypeError, ValueError) as exc:
        # Fail closed: an unreadable schema (missing import_runs / errors column,
        # corrupt DB, ...) must NOT be promoted.
        return RebuildSafety(
            ok=False,
            message=(f"Could not verify rebuilt database ({exc}); refusing to replace database."),
            transactions=0,
            transfers=0,
            exceptions=0,
        )

    if transactions + transfers == 0:
        return RebuildSafety(
            ok=False,
            message="Build produced zero transactions/transfers; refusing to replace database.",
            transactions=transactions,
            transfers=transfers,
            exceptions=exceptions,
        )

    if exceptions:
        return RebuildSafety(
            ok=False,
            message=f"Build produced {exceptions} exceptions; refusing to replace database.",
            transactions=transactions,
            transfers=transfers,
            exceptions=exceptions,
        )

    return RebuildSafety(
        ok=True,
        message=f"Verified rebuilt database: {transactions} transactions, {transfers} transfers.",
        transactions=transactions,
        transfers=transfers,
        exceptions=exceptions,
    )


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m ledger.maintenance.rebuild_guard <db_path>", file=sys.stderr)
        return 2

    result = check_rebuild_safe(args[0])
    if not result.ok:
        print(result.message, file=sys.stderr)
        return 1
    print(result.message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
