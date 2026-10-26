"""Local budgets and savings goals."""

from __future__ import annotations

import calendar
import csv
import os
import secrets
import sqlite3
import stat
import statistics
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, suppress
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, TextIO

from sqlalchemy import create_engine, func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ledger.core.results import RunResult
from ledger.finance_policy import spend_sql_filters
from ledger.query import LedgerQueryService
from ledger.query.base import SessionScopeFactory
from ledger.storage.database import (
    AccountBalanceDB,
    AccountDB,
    Base,
    SavingsGoalDB,
    TransactionDB,
    session_scope,
)
from ledger.storage.migrations import (
    MIGRATIONS,
    REQUIRED_TABLES,
    ensure_schema,
    invalidate_db_caches,
)
from ledger.storage.operations import backup_database, record_db_operation
from ledger.storage.operations import utc_now_dt as utc_now
from ledger.storage.snapshots import (
    DatabaseTargetLockError,
    DatabaseWriteCommittedError,
    audited_database_write_connection,
    bound_database_read_connection,
    database_target_lock,
    database_write_outcome,
)
from ledger.utils import year_month
from ledger.utils.money import quantize_cents, to_cents
from ledger.utils.private_files import (
    PinnedPrivateDirectory,
    PinnedPrivateFile,
    pin_existing_directory,
    pin_private_directory,
)
from ledger.utils.text import neutralize_csv_cell


def _coerce_amount(value: Any) -> Decimal:
    """NaN-safe coercion of an interactive editor amount cell to a Decimal.

    A blank (``None`` / ``""``), a float NaN (what an empty Streamlit NumberColumn
    cell becomes), or a non-numeric string must NOT crash the save path or
    propagate a ``Decimal('NaN')`` — they all coerce to ``Decimal('0')`` so the
    existing ``_budget_errors`` / ``_goal_errors`` validation can flag them
    normally (a zero amount is a perfectly valid value for validation to inspect).
    """
    if value is None:
        return Decimal("0")
    if isinstance(value, float):
        # NaN (and inf) cannot become valid money; an empty NumberColumn cell
        # arrives here as float('nan').
        if value != value or value in (float("inf"), float("-inf")):
            return Decimal("0")
        return Decimal(str(value))
    text = str(value).strip()
    if not text:
        return Decimal("0")
    try:
        amount = Decimal(text)
    except InvalidOperation:
        return Decimal("0")
    # A Decimal parsed from a string like "nan"/"inf" is finite-checkable here.
    if not amount.is_finite():
        return Decimal("0")
    return amount


_MAX_STORED_BUDGET_AMOUNT = Decimal("9999999999.99")


def _parse_budget_csv_amount(value: Any) -> Decimal:
    """Parse one destructive-import amount without editor-style zero coercion."""
    text = str(value if value is not None else "").strip()
    if not text or "e" in text.lower():
        raise ValueError("invalid budget CSV amount")
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("invalid budget CSV amount") from exc
    if not amount.is_finite() or abs(amount) > _MAX_STORED_BUDGET_AMOUNT:
        raise ValueError("invalid budget CSV amount")
    try:
        cents = quantize_cents(amount)
    except InvalidOperation as exc:
        raise ValueError("invalid budget CSV amount") from exc
    if cents != amount:
        raise ValueError("invalid budget CSV amount")
    return cents


def _budget_csv_row_has_unsafe_semantic_cell(row: dict[str, Any]) -> bool:
    """Return whether literal text cannot be emitted safely and reversibly as CSV."""
    for key in ("month", "category", "notes"):
        value = row.get(key)
        if isinstance(value, str) and (
            value.startswith("'") or neutralize_csv_cell(value) != value
        ):
            return True
    return False


def _database_artifact_paths(db_path: Path) -> tuple[Path, ...]:
    database = db_path.expanduser().absolute()
    return (
        database,
        Path(f"{database}-wal"),
        Path(f"{database}-shm"),
        Path(f"{database}-journal"),
        database.parent / f".{database.name}.rebuild.lock",
        database.parent / ".ledger-sqlite-alias.lock",
    )


def _normalized_budget_output(dest: Path) -> Path:
    """Resolve one traversal-free output path exactly once for publication."""
    expanded = Path(dest).expanduser()
    if ".." in expanded.parts:
        raise ValueError("Budget CSV destination must not contain traversal")
    lexical = expanded if expanded.is_absolute() else Path.cwd() / expanded
    if any(
        part == ".ledger-sqlite-alias.lock" or part.startswith(".ledger-sqlite-alias-")
        for part in lexical.parts
    ):
        raise ValueError("Budget CSV destination uses a reserved SQLite alias name")
    current = Path(lexical.anchor)
    for part in lexical.parts[1:]:
        current /= part
        if current.is_symlink():
            raise OSError("Budget CSV destination contains a symlink")
    output = Path(os.path.abspath(os.fspath(lexical)))
    if not output.name or output.name in {".", ".."}:
        raise ValueError("Budget CSV destination must name one file")
    if output.name == ".ledger-sqlite-alias.lock" or output.name.startswith(
        ".ledger-sqlite-alias-"
    ):
        raise ValueError("Budget CSV destination uses a reserved SQLite alias name")
    return output


class _BudgetCSVPublication:
    """Track whether one absent-name CSV install crossed its durable barrier."""

    def __init__(self, output: Path) -> None:
        self.output = output
        self.committed = False


def _publish_budget_csv_no_clobber(
    output_parent: PinnedPrivateDirectory,
    output_name: str,
    write: Callable[[TextIO], None],
    publication: _BudgetCSVPublication,
) -> None:
    """Install one owner-only CSV by descriptor without replacing any entry."""
    output_parent.validate()
    try:
        os.stat(output_name, dir_fd=output_parent.descriptor, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise FileExistsError("Budget CSV destination must not exist")

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    candidate_name = ""
    descriptor = -1
    installed_identity: tuple[int, int] | None = None
    try:
        for _ in range(100):
            candidate_name = f".{output_name}.{secrets.token_hex(12)}.tmp"
            try:
                descriptor = os.open(
                    candidate_name,
                    flags,
                    0o600,
                    dir_fd=output_parent.descriptor,
                )
            except FileExistsError:
                continue
            break
        else:
            raise FileExistsError("Budget CSV temporary candidate could not be allocated")

        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            descriptor = -1
            write(handle)
            handle.flush()
            os.fchmod(handle.fileno(), 0o600)
            os.fsync(handle.fileno())
        candidate = os.stat(
            candidate_name,
            dir_fd=output_parent.descriptor,
            follow_symlinks=False,
        )
        if not stat.S_ISREG(candidate.st_mode) or stat.S_IMODE(candidate.st_mode) != 0o600:
            raise OSError("Budget CSV candidate is not an owner-only regular file")
        installed_identity = (candidate.st_dev, candidate.st_ino)
        output_parent.validate()
        os.link(
            candidate_name,
            output_name,
            src_dir_fd=output_parent.descriptor,
            dst_dir_fd=output_parent.descriptor,
            follow_symlinks=False,
        )
        installed = os.stat(
            output_name,
            dir_fd=output_parent.descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(installed.st_mode)
            or stat.S_IMODE(installed.st_mode) != 0o600
            or (installed.st_dev, installed.st_ino) != installed_identity
        ):
            raise OSError("Budget CSV installed identity changed")
        os.fsync(output_parent.descriptor)
        publication.committed = True
        os.unlink(candidate_name, dir_fd=output_parent.descriptor)
        candidate_name = ""
        os.fsync(output_parent.descriptor)
        output_parent.validate()
    except BaseException:
        if installed_identity is not None and not publication.committed:
            try:
                installed = os.stat(
                    output_name,
                    dir_fd=output_parent.descriptor,
                    follow_symlinks=False,
                )
                if (installed.st_dev, installed.st_ino) == installed_identity:
                    os.unlink(output_name, dir_fd=output_parent.descriptor)
                    os.fsync(output_parent.descriptor)
            except BaseException:
                pass
        raise
    finally:
        if descriptor >= 0:
            with suppress(OSError):
                os.close(descriptor)
        if candidate_name:
            try:
                os.unlink(candidate_name, dir_fd=output_parent.descriptor)
                os.fsync(output_parent.descriptor)
            except OSError:
                pass


def _normalized_budget_output_collides_with_database(output: Path, db_path: Path) -> bool:
    """Fail closed when one normalized CSV output names or aliases a SQLite artifact."""
    try:
        output_identity = None
        if output.exists():
            metadata = output.stat()
            output_identity = (metadata.st_dev, metadata.st_ino)
        for artifact in _database_artifact_paths(db_path):
            if output == artifact or output == artifact.resolve(strict=False):
                return True
            if output_identity is not None and artifact.exists():
                metadata = artifact.stat()
                if output_identity == (metadata.st_dev, metadata.st_ino):
                    return True
    except OSError:
        return True
    return False


def _budget_output_collides_with_database(dest: Path, db_path: Path) -> bool:
    """Normalize one destination and fail closed on SQLite artifact collisions."""
    try:
        output = _normalized_budget_output(dest)
    except OSError, ValueError:
        return True
    return _normalized_budget_output_collides_with_database(output, db_path)


def _pinned_budget_output_collides_with_database(
    output_parent: PinnedPrivateDirectory,
    output_name: str,
    pinned_database: PinnedPrivateFile,
) -> bool:
    """Compare an output basename with managed DB names and inodes under both pins."""
    try:
        output_parent.validate()
        pinned_database.validate()
        database_parent = pinned_database.parent
        database_parent.require_owner_only()
        if output_name == ".ledger-sqlite-alias.lock" or output_name.startswith(
            ".ledger-sqlite-alias-"
        ):
            return True
        database_names = {
            pinned_database.path.name,
            f"{pinned_database.path.name}-wal",
            f"{pinned_database.path.name}-shm",
            f"{pinned_database.path.name}-journal",
            f".{pinned_database.path.name}.rebuild.lock",
            ".ledger-sqlite-alias.lock",
        }
        if output_parent.identity == database_parent.identity and output_name in database_names:
            return True
        try:
            output_metadata = os.stat(
                output_name,
                dir_fd=output_parent.descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            output_identity: tuple[int, int] | None = None
        else:
            if not stat.S_ISREG(output_metadata.st_mode):
                return True
            output_identity = (output_metadata.st_dev, output_metadata.st_ino)
        if output_identity is not None:
            for name in database_names:
                try:
                    artifact = os.stat(
                        name,
                        dir_fd=database_parent.descriptor,
                        follow_symlinks=False,
                    )
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(artifact.st_mode):
                    return True
                if output_identity == (artifact.st_dev, artifact.st_ino):
                    return True
        output_parent.validate()
        pinned_database.validate()
        database_parent.require_owner_only()
    except OSError:
        return True
    return False


def _require_safe_pinned_budget_output(
    output_parent: PinnedPrivateDirectory,
    output_name: str,
    pinned_database: PinnedPrivateFile,
) -> None:
    """Revalidate both namespaces at the final descriptor-relative publish seam."""
    if _pinned_budget_output_collides_with_database(
        output_parent,
        output_name,
        pinned_database,
    ):
        raise OSError("Budget CSV publication target changed or conflicts with the database")


def _is_calendar_month(value: str) -> bool:
    try:
        date.fromisoformat(f"{value}-01")
    except ValueError:
        return False
    return True


def _budget_import_schema_issues(connection: Any) -> dict[str, int]:
    """Return read-only drift counts for the exact live database snapshot."""
    existing_tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type = 'table'"
        ).fetchall()
    }
    missing_tables = REQUIRED_TABLES - existing_tables
    column_mismatches = 0
    for table_name in sorted(REQUIRED_TABLES & existing_tables):
        expected_columns = set(Base.metadata.tables[table_name].columns.keys())
        quoted_table = table_name.replace('"', '""')
        live_columns = connection.execute(f'PRAGMA table_info("{quoted_table}")').fetchall()
        actual_columns = {str(row[1]) for row in live_columns}
        incompatible_extra = any(
            str(row[1]) not in expected_columns and bool(row[3]) and row[4] is None
            for row in live_columns
        )
        if expected_columns - actual_columns or incompatible_extra:
            column_mismatches += 1

    expected_migrations = {migration.version for migration in MIGRATIONS}
    applied_migrations: set[str] = set()
    if "schema_migrations" in existing_tables:
        migration_columns = {
            str(row[1])
            for row in connection.execute('PRAGMA table_info("schema_migrations")').fetchall()
        }
        if "version" in migration_columns:
            applied_migrations = {
                str(row[0])
                for row in connection.execute("SELECT version FROM schema_migrations").fetchall()
            }
    return {
        "missing_tables": len(missing_tables),
        "column_mismatches": column_mismatches,
        "migration_set_mismatch": int(applied_migrations != expected_migrations),
    }


def _read_budget_import_schema_issues(pinned: PinnedPrivateFile) -> dict[str, int]:
    """Inspect schema through the exact pinned logical database after backup."""
    with bound_database_read_connection(
        pinned,
        label="Budget import schema source",
        require_main_owner_only=True,
        require_parent_owner_only=True,
    ) as schema_connection:
        return _budget_import_schema_issues(schema_connection)


def _month_range(start_date: date, end_date: date) -> list[str]:
    months = []
    current = start_date.replace(day=1)
    end = end_date.replace(day=1)
    while current <= end:
        months.append(year_month(current))
        if current == end:
            break
        if current.month == 12:
            current = date(current.year + 1, 1, 1)
        else:
            current = date(current.year, current.month + 1, 1)
    return months


def _month_overlap_fraction(month_start: date, start_date: date, end_date: date) -> Decimal:
    """Fraction of the calendar month beginning at ``month_start`` that lies inside
    the inclusive report window ``[start_date, end_date]``.

    A whole month inside the window yields exactly ``Decimal('1')``; a half-covered
    month yields ~``Decimal('0.5')``; a month entirely outside yields ``Decimal('0')``.
    ``month_start`` must be the first day of its month.
    """
    days_in_month = calendar.monthrange(month_start.year, month_start.month)[1]
    month_end = date(month_start.year, month_start.month, days_in_month)
    overlap_start = max(month_start, start_date)
    overlap_end = min(month_end, end_date)
    if overlap_end < overlap_start:
        return Decimal("0")
    overlap_days = (overlap_end - overlap_start).days + 1
    return Decimal(overlap_days) / Decimal(days_in_month)


def _prorated_month_count(start_date: date, end_date: date) -> Decimal:
    """Effective month count of [start_date, end_date] for a per-month ('*') budget.

    Each calendar month contributes the fraction of its days inside the range: a whole
    month contributes 1, a partial month contributes (days in range)/(days in month).
    Counting whole calendar months touched (the old multiplier) over-stated recurring
    budgets for non-month-aligned windows — e.g. a 90-day range that clips four calendar
    months scored a 4x budget against ~3 months of actual spend. A full single month
    yields exactly Decimal('1').
    """
    if end_date < start_date:
        return Decimal("0")
    total = Decimal("0")
    cursor = start_date.replace(day=1)
    last_month = end_date.replace(day=1)
    while cursor <= last_month:
        total += _month_overlap_fraction(cursor, start_date, end_date)
        if cursor == last_month:
            break
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)
    return total


class BudgetService:
    """Manage SQLite-backed local category budgets and goals."""

    def __init__(
        self,
        db_path: Path,
        *,
        session_scope_factory: SessionScopeFactory = session_scope,
        prepare_schema: bool = True,
    ):
        self.db_path = db_path
        self._session_scope_factory = session_scope_factory
        self._bound_default_reads = not prepare_schema and session_scope_factory is session_scope
        if prepare_schema:
            with database_target_lock(self.db_path):
                ensure_schema(self.db_path)

    def _open_session(self) -> AbstractContextManager[Session]:
        if self._bound_default_reads:
            return self._open_read_only_session()
        return self._session_scope_factory(self.db_path)

    @contextmanager
    def _open_read_only_session(self) -> Iterator[Session]:
        """Bind default read-safe methods to one exact main/WAL snapshot."""
        path = self.db_path.expanduser().absolute()
        if not path.is_file():
            raise FileNotFoundError(f"Database not found: {path}")
        with bound_database_read_connection(
            path,
            label="Budget read source",
            require_main_owner_only=False,
            require_parent_owner_only=False,
        ) as raw_connection:
            engine = create_engine(
                "sqlite://",
                creator=lambda: raw_connection,
                poolclass=StaticPool,
            )
            sqlalchemy_connection = engine.connect()
            session = Session(bind=sqlalchemy_connection)
            try:
                yield session
            finally:
                session.rollback()
                session.close()
                sqlalchemy_connection.close()
                engine.dispose()

    def _open_query_session(self, _path: Path) -> AbstractContextManager[Session]:
        """Let query modules share this service's exact read-session policy."""
        return self._open_session()

    @staticmethod
    @contextmanager
    def _open_pinned_read_session(
        pinned: PinnedPrivateFile,
        *,
        label: str,
    ) -> Iterator[Session]:
        """Expose one exact pinned SQLite snapshot through the ORM query surface."""
        with bound_database_read_connection(
            pinned,
            label=label,
            require_main_owner_only=True,
            require_parent_owner_only=True,
        ) as raw_connection:
            engine = create_engine(
                "sqlite://",
                creator=lambda: raw_connection,
                poolclass=StaticPool,
            )
            sqlalchemy_connection = engine.connect()
            session = Session(bind=sqlalchemy_connection)
            try:
                yield session
            finally:
                session.rollback()
                session.close()
                sqlalchemy_connection.close()
                engine.dispose()

    @contextmanager
    def _pin_mutable_target(self) -> Iterator[PinnedPrivateFile]:
        """Pin the exact existing target below the shared publication lock."""
        with self._pin_mutable_path(self.db_path) as pinned:
            yield pinned

    @staticmethod
    @contextmanager
    def _pin_mutable_path(path: Path) -> Iterator[PinnedPrivateFile]:
        """Pin one exact owner-only mutable file below its pinned parent."""
        with (
            pin_private_directory(path.parent) as parent,
            parent.pin_private_file(path.name, mutable=True) as pinned,
        ):
            yield pinned

    def list_budgets(self) -> list[dict[str, Any]]:
        with self._open_session() as session:
            return self._list_budgets_session(session)

    @staticmethod
    def _list_budgets_session(session: Session) -> list[dict[str, Any]]:
        rows = session.connection().exec_driver_sql(
            """
            SELECT
                id,
                typeof(budget_month), budget_month,
                typeof(category), category,
                typeof(amount), amount,
                typeof(notes), notes
            FROM budgets
            ORDER BY budget_month, category
            """
        )
        budgets: list[dict[str, Any]] = []
        try:
            for row in rows:
                month_type, month = str(row[1]), row[2]
                category_type, category = str(row[3]), row[4]
                amount_type, raw_amount = str(row[5]), row[6]
                notes_type, notes = str(row[7]), row[8]
                if (
                    month_type != "text"
                    or not isinstance(month, str)
                    or category_type != "text"
                    or not isinstance(category, str)
                    or notes_type != "text"
                    or not isinstance(notes, str)
                    or amount_type not in {"integer", "real", "text"}
                ):
                    raise ValueError("Stored budget definition is invalid")
                amount = Decimal(str(raw_amount))
                parsed = {
                    "id": row[0],
                    "month": month,
                    "category": category,
                    "amount": amount,
                    "notes": notes,
                }
                if BudgetService._budget_errors([parsed]):
                    raise ValueError("Stored budget definition is invalid")
                budgets.append(parsed)
        except (InvalidOperation, TypeError, OverflowError) as exc:
            raise ValueError("Stored budget definition is invalid") from exc
        return budgets

    def replace_budgets(self, rows: list[dict[str, Any]]) -> RunResult:
        with (
            database_write_outcome() as outcome,
            database_target_lock(self.db_path),
        ):
            result = self._replace_budgets_locked(rows)
            if result.ok:
                outcome.mark_committed()
            return result

    def _replace_budgets_locked(
        self,
        rows: list[dict[str, Any]],
    ) -> RunResult:
        with database_write_outcome() as outcome, self._pin_mutable_target() as pinned:
            result = self._replace_budgets_pinned(rows, pinned_target=pinned)
            if result.ok:
                outcome.mark_committed()
            return result

    def _replace_budgets_pinned(
        self,
        rows: list[dict[str, Any]],
        *,
        pinned_target: PinnedPrivateFile,
        backup_result: RunResult | None = None,
    ) -> RunResult:
        started_at = utc_now()
        parsed = [
            self._parse_budget_row(row) for row in rows if str(row.get("category", "")).strip()
        ]
        errors = self._budget_errors(parsed)
        if errors:
            return RunResult(
                status="failed",
                started_at=started_at,
                finished_at=utc_now(),
                errors=errors,
                counts={"rows": len(parsed)},
            )

        now = utc_now()
        result: RunResult | None = None
        with audited_database_write_connection(
            pinned_target,
            label="Budget replacement target",
        ) as connection:
            connection.execute("DELETE FROM budgets")
            connection.executemany(
                """
                INSERT INTO budgets (
                    budget_month, category, amount, notes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        row["month"],
                        row["category"],
                        str(row["amount"]),
                        row.get("notes", ""),
                        now.isoformat(sep=" "),
                        now.isoformat(sep=" "),
                    )
                    for row in parsed
                ],
            )
            result = RunResult(
                status="success",
                started_at=started_at,
                finished_at=utc_now(),
                counts={"budgets": len(parsed)},
                artifacts=dict(backup_result.artifacts) if backup_result is not None else {},
            )
            if backup_result is not None:
                backup_path = Path(backup_result.artifacts["backup"])
                record_db_operation(
                    self.db_path,
                    operation="backup",
                    result=backup_result,
                    artifact_path=backup_path,
                    connection=connection,
                )
            record_db_operation(
                self.db_path,
                operation="budget_replace",
                result=result,
                connection=connection,
            )
        assert result is not None
        return result

    def budget_variance(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        months = _month_range(start_date, end_date)
        # A per-month ('*') budget applies once per month of coverage; prorate partial
        # months instead of counting whole calendar months touched, so the budget tracks
        # the actual [start_date, end_date] spend window it is compared against.
        prorated_months = _prorated_month_count(start_date, end_date)
        with self._open_session() as session:

            @contextmanager
            def borrowed_session(_path: Path) -> Iterator[Session]:
                yield session

            spending = LedgerQueryService(
                self.db_path,
                session_scope_factory=borrowed_session,
            ).get_spending_by_category(start_date, end_date)
            budgets = self._list_budgets_session(session)
        budget_totals: dict[str, Decimal] = {}
        for row in budgets:
            month = row["month"] or "*"
            if month != "*" and month not in months:
                continue
            if month == "*":
                multiplier = prorated_months
            else:
                # A month-specific budget covers only its own month: prorate it by
                # how much of that month the report window actually spans, so a
                # partial-month window doesn't compare full-month budget to partial
                # actuals (which understated % used / over-budget).
                multiplier = _month_overlap_fraction(
                    date(int(month[:4]), int(month[5:7]), 1), start_date, end_date
                )
            category = row["category"]
            budget_totals[category] = (
                budget_totals.get(category, Decimal("0")) + Decimal(str(row["amount"])) * multiplier
            )

        rows = []
        for category in sorted(set(spending) | set(budget_totals)):
            # The planning result is a money-facing contract.  Round both sides
            # to storage/wire cents before deriving variance, status, and percent;
            # otherwise a prorated sub-cent remainder can produce contradictory
            # output such as equal displayed amounts with an ``over`` status.
            actual = to_cents(Decimal(str(spending.get(category, Decimal("0")))))
            budget = to_cents(budget_totals.get(category, Decimal("0")))
            is_defined = category in budget_totals
            variance = to_cents(budget - actual)
            pct_used = (
                (actual / budget * Decimal("100")).quantize(Decimal("0.01"))
                if is_defined and budget
                else None
            )
            rows.append(
                {
                    "category": category,
                    "budget": budget,
                    "actual": actual,
                    "variance": variance,
                    "pct_used": pct_used,
                    "status": (
                        "over"
                        if is_defined and actual > budget
                        else "ok"
                        if is_defined
                        else "unbudgeted"
                    ),
                }
            )
        return sorted(
            rows,
            key=lambda row: (row["status"] != "over", -abs(row["variance"])),
        )

    def list_goals(self) -> list[dict[str, Any]]:
        with self._open_session() as session:
            rows = (
                session.query(SavingsGoalDB)
                .order_by(SavingsGoalDB.status, SavingsGoalDB.name)
                .all()
            )
            account_ids = [str(row.account_id) for row in rows if row.account_id is not None]
            accounts = (
                {
                    str(account.account_id): account
                    for account in session.query(AccountDB)
                    .filter(AccountDB.account_id.in_(account_ids))
                    .all()
                }
                if account_ids
                else {}
            )
            balances = self._latest_account_balances(session, account_ids)
            goal_rows = []
            for row in rows:
                if row.account_id is None:
                    # Legacy / not-yet-linked goal: distinguish from a linked-but-empty goal
                    goal_rows.append(
                        {
                            "id": row.id,
                            "name": row.name,
                            "account_id": None,
                            "account": "Not linked",
                            "target_amount": Decimal(str(row.target_amount)),
                            "current_amount": None,
                            "current_balance_date": None,
                            "target_date": row.target_date,
                            "status": row.status,
                            "notes": row.notes,
                        }
                    )
                else:
                    account_id = str(row.account_id)
                    balance = balances.get(account_id, {})
                    current_amount = balance.get("amount")
                    goal_rows.append(
                        {
                            "id": row.id,
                            "name": row.name,
                            "account_id": account_id,
                            "account": self._account_label(accounts.get(account_id)) or account_id,
                            "target_amount": Decimal(str(row.target_amount)),
                            "current_amount": current_amount,
                            "current_balance_date": balance.get("date"),
                            "target_date": row.target_date,
                            "status": row.status,
                            "notes": row.notes,
                        }
                    )
            return goal_rows

    def list_goal_accounts(self) -> list[dict[str, Any]]:
        with self._open_session() as session:
            rows = (
                session.query(AccountDB)
                .order_by(AccountDB.institution, AccountDB.account_type, AccountDB.account_number)
                .all()
            )
            account_ids = [str(row.account_id) for row in rows]
            balances = self._latest_account_balances(session, account_ids)
            return [
                {
                    "account_id": row.account_id,
                    "label": self._account_label(row),
                    "is_active": row.is_active,
                    "current_amount": balances[str(row.account_id)]["amount"]
                    if str(row.account_id) in balances
                    else None,
                    "current_balance_date": balances.get(str(row.account_id), {}).get("date"),
                }
                for row in rows
            ]

    def replace_goals(self, rows: list[dict[str, Any]]) -> RunResult:
        with (
            database_write_outcome() as outcome,
            database_target_lock(self.db_path),
        ):
            result = self._replace_goals_locked(rows)
            if result.ok:
                outcome.mark_committed()
            return result

    def _replace_goals_locked(
        self,
        rows: list[dict[str, Any]],
    ) -> RunResult:
        with database_write_outcome() as outcome, self._pin_mutable_target() as pinned:
            result = self._replace_goals_pinned(rows, pinned_target=pinned)
            if result.ok:
                outcome.mark_committed()
            return result

    def _replace_goals_pinned(
        self,
        rows: list[dict[str, Any]],
        *,
        pinned_target: PinnedPrivateFile,
    ) -> RunResult:
        started_at = utc_now()
        now = utc_now()
        parsed = [self._parse_goal_row(row) for row in rows if str(row.get("name", "")).strip()]
        with audited_database_write_connection(
            pinned_target,
            label="Savings-goal replacement target",
        ) as connection:
            account_ids = {
                str(row[0]) for row in connection.execute("SELECT account_id FROM accounts")
            }
            errors = self._goal_errors(parsed, account_ids)
            if errors:
                return RunResult(
                    status="failed",
                    started_at=started_at,
                    finished_at=utc_now(),
                    errors=errors,
                    counts={"rows": len(parsed)},
                )
            connection.execute("DELETE FROM savings_goals")
            connection.executemany(
                """
                INSERT INTO savings_goals (
                    name, account_id, target_amount, target_date, status,
                    notes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        str(row["name"]).strip(),
                        str(row["account_id"]).strip(),
                        str(row.get("target_amount") or 0),
                        row["target_date"].isoformat() if row.get("target_date") else None,
                        str(row.get("status") or "active"),
                        str(row.get("notes") or ""),
                        now.isoformat(sep=" "),
                        now.isoformat(sep=" "),
                    )
                    for row in parsed
                ],
            )
            result = RunResult(
                status="success",
                started_at=started_at,
                finished_at=utc_now(),
                counts={"goals": len(parsed)},
            )
            record_db_operation(
                self.db_path,
                operation="goals_replace",
                result=result,
                connection=connection,
            )
        return result

    def import_csv(self, source: Path, *, execute: bool = False) -> RunResult:
        started_at = utc_now()
        rows: list[dict[str, Any]] = []
        try:
            with source.open(newline="", encoding="utf-8") as handle:
                reader = csv.DictReader(handle, strict=True)
                if reader.fieldnames != ["month", "category", "amount", "notes"]:
                    return RunResult(
                        status="failed",
                        started_at=started_at,
                        finished_at=utc_now(),
                        counts={"rows": 0},
                        errors=["Budget CSV header must be exactly: month,category,amount,notes"],
                        details={"execute": execute},
                    )
                for row in reader:
                    if None in row or any(row.get(field) is None for field in reader.fieldnames):
                        return RunResult(
                            status="failed",
                            started_at=started_at,
                            finished_at=utc_now(),
                            counts={"rows": len(rows)},
                            errors=["Budget CSV rows must contain exactly four columns."],
                            details={"execute": execute},
                        )
                    raw_row = {
                        "month": row.get("month") or "",
                        "category": row.get("category") or "",
                        "amount": row.get("amount") or "",
                        "notes": row.get("notes") or "",
                    }
                    if any(str(value).strip() for value in raw_row.values()):
                        rows.append(raw_row)
        except (OSError, UnicodeDecodeError, csv.Error) as exc:
            # A missing or non-UTF-8 budget CSV must fail as a clean RunResult, not a
            # raw traceback. Keep the reason generic (no file bytes).
            return RunResult(
                status="error",
                started_at=started_at,
                finished_at=utc_now(),
                counts={},
                errors=[f"Could not read budget CSV {source.name}: {type(exc).__name__}"],
                details={"execute": execute},
            )
        try:
            parsed = [self._parse_budget_csv_row(row) for row in rows]
        except ValueError:
            return RunResult(
                status="failed",
                started_at=started_at,
                finished_at=utc_now(),
                counts={"rows": len(rows)},
                errors=["Budget CSV contains an invalid amount; no changes were made."],
                details={"execute": execute},
            )
        if not parsed:
            return RunResult(
                status="failed",
                started_at=started_at,
                finished_at=utc_now(),
                counts={"rows": 0},
                errors=["Budget CSV produced zero budget rows; no changes were made."],
                details={"execute": execute},
            )
        errors = self._budget_errors(parsed)
        if errors:
            return RunResult(
                status="failed",
                started_at=started_at,
                finished_at=utc_now(),
                counts={"rows": len(parsed)},
                errors=errors,
                details={"execute": execute},
            )
        if not execute:
            return RunResult(
                status="success",
                started_at=started_at,
                finished_at=utc_now(),
                counts={"budgets": len(parsed)},
                warnings=["Dry run only; no budget rows were changed."],
                details={"execute": False},
            )
        backup: RunResult | None = None
        live_result: RunResult | None = None
        live_committed = False
        try:
            with database_target_lock(self.db_path), self._pin_mutable_target() as pinned:
                backup = backup_database(
                    self.db_path,
                    reason="pre-budget-import",
                    pinned_source=pinned,
                    record=False,
                )
                if not backup.ok:
                    return RunResult(
                        status="failed",
                        started_at=started_at,
                        finished_at=utc_now(),
                        counts={"rows": len(parsed)},
                        errors=["Pre-write budget backup failed; no changes were made."],
                        details={"execute": True},
                    )
                pinned.validate()
                schema_issues = _read_budget_import_schema_issues(pinned)
                pinned.validate()
                if any(schema_issues.values()):
                    return RunResult(
                        status="failed",
                        started_at=started_at,
                        finished_at=utc_now(),
                        counts={"rows": len(parsed)},
                        errors=[
                            "Budget import retained its backup but the live database schema "
                            "is not current; run `ledger db migrate` first, then retry."
                        ],
                        artifacts=dict(backup.artifacts),
                        details={
                            "execute": True,
                            "committed": False,
                            "schema_issues": schema_issues,
                        },
                    )
                live_result = self._replace_budgets_pinned(
                    parsed,
                    pinned_target=pinned,
                    backup_result=backup,
                )
                live_committed = True
                invalidate_db_caches()
                pinned.validate()
        except DatabaseWriteCommittedError:
            return RunResult(
                status="failed",
                started_at=started_at,
                finished_at=utc_now(),
                counts=live_result.counts if live_result is not None else {"rows": len(parsed)},
                errors=[
                    "Budget replacement commit succeeded, but final validation or cleanup "
                    "did not complete; inspect the retained backup before retrying."
                ],
                artifacts=dict(backup.artifacts) if backup is not None and backup.ok else {},
                details={"execute": True, "committed": True},
            )
        except BaseException as exc:
            if live_committed:
                return RunResult(
                    status="failed",
                    started_at=started_at,
                    finished_at=utc_now(),
                    counts=live_result.counts if live_result is not None else {},
                    errors=[
                        "Budget replacement commit succeeded, but final validation or cleanup "
                        "did not complete; inspect the retained backup before retrying."
                    ],
                    artifacts=(dict(backup.artifacts) if backup is not None and backup.ok else {}),
                    details={"execute": True, "committed": True},
                )
            if not isinstance(exc, Exception):
                raise
            return RunResult(
                status="failed",
                started_at=started_at,
                finished_at=utc_now(),
                counts={"rows": len(parsed)},
                errors=["Budget import could not be completed; no replacement was committed."],
                artifacts=dict(backup.artifacts) if backup is not None and backup.ok else {},
                details={"execute": True},
            )
        assert live_result is not None
        return RunResult(
            status=live_result.status,
            started_at=started_at,
            finished_at=live_result.finished_at,
            counts=live_result.counts,
            warnings=live_result.warnings,
            errors=live_result.errors,
            artifacts=live_result.artifacts,
            details={"execute": True, "backup": str(backup.artifacts.get("backup", ""))},
        )

    def suggest_budgets(self, start_date: date, end_date: date) -> list[dict[str, Any]]:
        """Median represented nonzero monthly gross spend; absent months stay absent."""
        if end_date < start_date:
            raise ValueError("Budget suggestion end date must not precede start date")
        with self._open_session() as session:
            return self._suggest_budgets_session(session, start_date, end_date)

    @staticmethod
    def _suggest_budgets_session(
        session: Session,
        start_date: date,
        end_date: date,
    ) -> list[dict[str, Any]]:
        totals = (
            session.query(
                func.strftime("%Y-%m", TransactionDB.posting_date).label("month"),
                TransactionDB.category,
                func.sum(func.abs(TransactionDB.amount_signed)).label("total"),
            )
            .filter(
                TransactionDB.posting_date >= start_date,
                TransactionDB.posting_date <= end_date,
                *spend_sql_filters(),
            )
            .group_by("month", TransactionDB.category)
            .order_by(TransactionDB.category, "month")
            .all()
        )
        by_category: dict[str, list[Decimal]] = {}
        for row in totals:
            by_category.setdefault(str(row.category), []).append(to_cents(row.total))
        return [
            {
                "month": "*",
                "category": category,
                "amount": to_cents(statistics.median(amounts)),
                "notes": (
                    f"Median of {len(amounts)} nonzero monthly gross spending total(s), "
                    f"{start_date.isoformat()} through {end_date.isoformat()}."
                ),
            }
            for category, amounts in sorted(by_category.items())
        ]

    def export_suggestions(self, dest: Path, start_date: date, end_date: date) -> RunResult:
        started_at = utc_now()
        try:
            output = _normalized_budget_output(dest)
        except OSError, ValueError:
            return self._output_collision_result(started_at)
        if _normalized_budget_output_collides_with_database(output, self.db_path):
            return self._output_collision_result(started_at)
        publication = _BudgetCSVPublication(output)
        rows: list[dict[str, Any]] = []
        try:
            with (
                database_target_lock(self.db_path),
                self._pin_mutable_target() as pinned_database,
                pin_existing_directory(output.parent) as output_parent,
            ):
                if _pinned_budget_output_collides_with_database(
                    output_parent,
                    output.name,
                    pinned_database,
                ):
                    return self._output_collision_result(started_at)
                with self._open_pinned_read_session(
                    pinned_database,
                    label="Budget suggestion source",
                ) as session:
                    rows = self._suggest_budgets_session(session, start_date, end_date)
                if not rows:
                    return RunResult(
                        status="failed",
                        started_at=started_at,
                        finished_at=utc_now(),
                        counts={"budgets": 0},
                        errors=["No qualifying spending was found; no budget CSV was created."],
                        details={
                            "actual_basis": "gross_spending",
                            "committed": False,
                            "start_date": start_date.isoformat(),
                            "end_date": end_date.isoformat(),
                        },
                    )
                suggestion_errors = self._budget_errors(rows)
                if suggestion_errors:
                    return RunResult(
                        status="failed",
                        started_at=started_at,
                        finished_at=utc_now(),
                        counts={"budgets": 0},
                        errors=[
                            "Suggested budgets fall outside the importable budget domain; "
                            "no budget CSV was created."
                        ],
                        details={
                            "actual_basis": "gross_spending",
                            "committed": False,
                            "start_date": start_date.isoformat(),
                            "end_date": end_date.isoformat(),
                            "issues": len(suggestion_errors),
                        },
                    )
                if any(_budget_csv_row_has_unsafe_semantic_cell(row) for row in rows):
                    return RunResult(
                        status="failed",
                        started_at=started_at,
                        finished_at=utc_now(),
                        counts={"budgets": 0},
                        errors=[
                            "Suggested budgets contain text that cannot be exported safely; "
                            "no budget CSV was created."
                        ],
                        details={
                            "actual_basis": "gross_spending",
                            "committed": False,
                            "start_date": start_date.isoformat(),
                            "end_date": end_date.isoformat(),
                        },
                    )
                _require_safe_pinned_budget_output(
                    output_parent,
                    output.name,
                    pinned_database,
                )

                def write_suggestions(handle: TextIO) -> None:
                    writer = csv.DictWriter(
                        handle, fieldnames=["month", "category", "amount", "notes"]
                    )
                    writer.writeheader()
                    writer.writerows(
                        {
                            **row,
                            "amount": f"{Decimal(str(row['amount'])):.2f}",
                        }
                        for row in rows
                    )

                _publish_budget_csv_no_clobber(
                    output_parent,
                    output.name,
                    write_suggestions,
                    publication,
                )
                _require_safe_pinned_budget_output(
                    output_parent,
                    output.name,
                    pinned_database,
                )
        except DatabaseTargetLockError:
            if publication.committed:
                return self._committed_output_failure(
                    started_at,
                    output,
                    counts={"budgets": len(rows)},
                    kind="suggestion",
                )
            return RunResult(
                status="failed",
                started_at=started_at,
                finished_at=utc_now(),
                counts={},
                errors=["Budget suggestions could not acquire the database target lock."],
                details={"committed": False},
            )
        except BaseException as exc:
            if publication.committed:
                return self._committed_output_failure(
                    started_at,
                    output,
                    counts={"budgets": len(rows)},
                    kind="suggestion",
                )
            if not isinstance(exc, Exception):
                raise
            return self._output_collision_result(started_at)
        return RunResult(
            status="success",
            started_at=started_at,
            finished_at=utc_now(),
            counts={"budgets": len(rows)},
            artifacts={"csv": output},
            details={
                "actual_basis": "gross_spending",
                "committed": True,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            },
        )

    def export_csv(self, dest: Path) -> RunResult:
        started_at = utc_now()
        try:
            output = _normalized_budget_output(dest)
        except OSError, ValueError:
            return self._output_collision_result(started_at)
        if _normalized_budget_output_collides_with_database(output, self.db_path):
            return self._output_collision_result(started_at)
        result: RunResult | None = None
        try:
            with database_target_lock(self.db_path):
                result = self._export_csv_locked(output)
        except DatabaseTargetLockError:
            if result is not None and result.details.get("committed") is True:
                return self._committed_output_failure(
                    started_at,
                    output,
                    counts=result.counts,
                    kind="export",
                )
            return RunResult(
                status="failed",
                started_at=started_at,
                finished_at=utc_now(),
                counts={},
                errors=["Budget export could not acquire the database target lock."],
                details={"committed": False},
            )
        except BaseException as exc:
            if result is not None and result.details.get("committed") is True:
                return self._committed_output_failure(
                    started_at,
                    output,
                    counts=result.counts,
                    kind="export",
                )
            if not isinstance(exc, Exception):
                raise
            return self._output_collision_result(started_at)
        assert result is not None
        return result

    @staticmethod
    def _output_collision_result(started_at: datetime) -> RunResult:
        return RunResult(
            status="failed",
            started_at=started_at,
            finished_at=utc_now(),
            counts={},
            errors=["Budget CSV destination is unsafe or already exists; it must not exist."],
            details={"committed": False},
        )

    @staticmethod
    def _committed_output_failure(
        started_at: datetime,
        output: Path,
        *,
        counts: dict[str, int],
        kind: str,
    ) -> RunResult:
        unfinished = (
            "final audit, validation, or cleanup"
            if kind == "export"
            else "final validation or cleanup"
        )
        return RunResult(
            status="failed",
            started_at=started_at,
            finished_at=utc_now(),
            counts=counts,
            errors=[
                f"Budget CSV publication succeeded, but {unfinished} did not complete; "
                "inspect the published artifact before retrying."
            ],
            artifacts={"csv": output},
            details={"committed": True},
        )

    def _export_csv_locked(self, dest: Path, *, started_at: datetime | None = None) -> RunResult:
        started_at = started_at or utc_now()
        output = _normalized_budget_output(dest)
        if _normalized_budget_output_collides_with_database(output, self.db_path):
            return self._output_collision_result(started_at)
        publication = _BudgetCSVPublication(output)
        rows: list[dict[str, Any]] = []
        try:
            ensure_schema(self.db_path)
            with (
                self._pin_mutable_target() as pinned_database,
                pin_existing_directory(output.parent) as output_parent,
            ):
                if _pinned_budget_output_collides_with_database(
                    output_parent,
                    output.name,
                    pinned_database,
                ):
                    return self._output_collision_result(started_at)
                with self._open_pinned_read_session(
                    pinned_database,
                    label="Budget export source",
                ) as session:
                    rows = self._list_budgets_session(session)
                if any(_budget_csv_row_has_unsafe_semantic_cell(row) for row in rows):
                    return RunResult(
                        status="failed",
                        started_at=started_at,
                        finished_at=utc_now(),
                        counts={"budgets": 0},
                        errors=[
                            "Stored budgets contain text that cannot be exported safely; "
                            "no budget CSV was created."
                        ],
                        details={"committed": False},
                    )
                _require_safe_pinned_budget_output(
                    output_parent,
                    output.name,
                    pinned_database,
                )

                def write_export(handle: TextIO) -> None:
                    writer = csv.DictWriter(
                        handle,
                        fieldnames=["month", "category", "amount", "notes"],
                    )
                    writer.writeheader()
                    writer.writerows(
                        {
                            "month": row["month"],
                            "category": row["category"],
                            "amount": row["amount"],
                            "notes": row["notes"],
                        }
                        for row in rows
                    )

                _publish_budget_csv_no_clobber(
                    output_parent,
                    output.name,
                    write_export,
                    publication,
                )
                _require_safe_pinned_budget_output(
                    output_parent,
                    output.name,
                    pinned_database,
                )
                result = RunResult(
                    status="success",
                    started_at=started_at,
                    finished_at=utc_now(),
                    counts={"budgets": len(rows)},
                    artifacts={"csv": output},
                    details={"committed": True},
                )
                with audited_database_write_connection(
                    pinned_database,
                    label="Budget export audit target",
                ) as connection:
                    _require_safe_pinned_budget_output(
                        output_parent,
                        output.name,
                        pinned_database,
                    )
                    record_db_operation(
                        self.db_path,
                        operation="budget_export",
                        result=result,
                        artifact_path=output,
                        connection=connection,
                    )
                _require_safe_pinned_budget_output(
                    output_parent,
                    output.name,
                    pinned_database,
                )
                return result
        except BaseException as exc:
            if publication.committed:
                return self._committed_output_failure(
                    started_at,
                    output,
                    counts={"budgets": len(rows)},
                    kind="export",
                )
            if not isinstance(exc, Exception):
                raise
            return self._output_collision_result(started_at)

    def validate(self) -> RunResult:
        started_at = utc_now()
        try:
            rows = self.list_budgets()
            errors = self._budget_errors([self._parse_budget_row(row) for row in rows])
        except FileNotFoundError:
            raise
        except (
            InvalidOperation,
            OSError,
            SQLAlchemyError,
            sqlite3.DatabaseError,
            TypeError,
            ValueError,
        ):
            return RunResult(
                status="failed",
                started_at=started_at,
                finished_at=utc_now(),
                counts={"budgets": 0, "issues": 1},
                errors=["Stored budget definitions are corrupt or incompatible."],
                details={"committed": False},
            )
        return RunResult(
            status="success" if not errors else "failed",
            started_at=started_at,
            finished_at=utc_now(),
            counts={"budgets": len(rows), "issues": len(errors)},
            errors=errors,
            details={"committed": False},
        )

    @staticmethod
    def _parse_budget_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "month": str(row.get("month") or row.get("budget_month") or "*").strip() or "*",
            "category": str(row.get("category") or "").strip(),
            "amount": _coerce_amount(row.get("amount")),
            "notes": str(row.get("notes") or ""),
        }

    @staticmethod
    def _parse_budget_csv_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "month": str(row.get("month") if row.get("month") is not None else ""),
            "category": str(row.get("category") if row.get("category") is not None else ""),
            "amount": _parse_budget_csv_amount(row.get("amount")),
            "notes": str(row.get("notes") if row.get("notes") is not None else ""),
        }

    @staticmethod
    def _parse_goal_row(row: dict[str, Any]) -> dict[str, Any]:
        target_date = row.get("target_date")
        if isinstance(target_date, datetime):
            target_date = target_date.date()
        elif isinstance(target_date, str) and target_date.strip():
            target_date = date.fromisoformat(target_date.strip())
        elif not isinstance(target_date, date):
            target_date = None
        return {
            "name": str(row.get("name") or "").strip(),
            "account_id": str(row.get("account_id") or row.get("account") or "").strip(),
            "target_amount": _coerce_amount(row.get("target_amount")),
            "target_date": target_date,
            "status": str(row.get("status") or "active"),
            "notes": str(row.get("notes") or ""),
        }

    @staticmethod
    def _budget_errors(rows: list[dict[str, Any]]) -> list[str]:
        errors = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            month = row["month"]
            category = row["category"]
            if month != "*" and not _is_calendar_month(month):
                errors.append(f"Budget month must be '*' or YYYY-MM: {month}")
            if not category:
                errors.append("Budget category is required.")
            elif len(category) > 100:
                errors.append("Budget category cannot exceed 100 characters.")
            elif category != category.strip():
                errors.append("Budget category cannot have leading or trailing whitespace.")
            amount = row["amount"]
            if not amount.is_finite():
                errors.append(f"Budget amount must be finite: {category}")
            elif amount < 0:
                errors.append(f"Budget amount cannot be negative: {category}")
            elif amount > _MAX_STORED_BUDGET_AMOUNT:
                errors.append(f"Budget amount exceeds the storage maximum: {category}")
            elif quantize_cents(amount) != amount:
                errors.append(f"Budget amount must use whole cents: {category}")
            key = (month, category)
            if key in seen:
                errors.append(f"Duplicate budget row: {month} {category}")
            seen.add(key)
        return errors

    @staticmethod
    def _goal_errors(rows: list[dict[str, Any]], account_ids: set[str]) -> list[str]:
        errors = []
        for row in rows:
            name = row["name"]
            account_id = row["account_id"]
            if not account_id:
                errors.append(f"Goal account is required: {name}")
            elif account_id not in account_ids:
                errors.append(f"Unknown goal account: {account_id}")
            if row["target_amount"] < 0:
                errors.append(f"Goal target cannot be negative: {name}")
        return errors

    @staticmethod
    def _account_label(account: AccountDB | None) -> str:
        if account is None:
            return ""
        number = str(account.account_number or "")
        suffix = number[-4:] if number else "----"
        subtype = account.account_subtype or account.account_type
        label = f"{account.institution} - {subtype} (...{suffix})"
        if not account.is_active:
            label = f"{label} - inactive"
        return label

    @staticmethod
    def _latest_account_balances(session: Any, account_ids: list[str]) -> dict[str, dict[str, Any]]:
        unique_ids = set(account_ids)
        if not unique_ids:
            return {}
        # Batch the latest-balance lookup into a single query instead of one query
        # per account (the old N+1). Pull every balance row for the requested
        # accounts ordered (account_id, balance_date) and keep the last seen per
        # account — which, given the ascending order, is the latest balance.
        rows = (
            session.query(AccountBalanceDB)
            .filter(AccountBalanceDB.account_id.in_(unique_ids))
            .filter(AccountBalanceDB.balance_basis == "absolute")
            .order_by(AccountBalanceDB.account_id, AccountBalanceDB.balance_date)
            .all()
        )
        balances: dict[str, dict[str, Any]] = {}
        for row in rows:
            balances[row.account_id] = {
                "amount": Decimal(str(row.balance)),
                "date": row.balance_date,
            }
        return balances
