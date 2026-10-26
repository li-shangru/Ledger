"""Shared Streamlit context and global dashboard controls."""

from __future__ import annotations

import inspect
import json
import logging
import os
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import streamlit as st

from ledger.config.repository import SQLiteConfigRepository, seed_config_database
from ledger.config.schemas import CategoriesConfig, CategoryRule
from ledger.import_review import pending_review_count
from ledger.imports.pdf.readiness import FINALIZABLE_STATUSES
from ledger.query import LedgerQueryService
from ledger.runtime import resolve_runtime_path, seed_config_path
from ledger.storage.migrations import invalidate_db_caches, run_migrations
from ledger.storage.snapshots import (
    DatabaseTargetLockError,
    bound_database_read_connection,
    database_target_lock,
    recover_database_journal,
)
from ledger.ui.cache import cache_database_read
from ledger.ui.navigation import consume_brief_navigation

logger = logging.getLogger(__name__)

PERIOD_PRESETS = [
    "This Month",
    "Last Month",
    "This Quarter",
    "Last Quarter",
    "This Year",
    "Last Year",
    "Year to Date",
    "Last 30 Days",
    "Last 90 Days",
    "All Time",
    "Custom Range",
]


@dataclass(frozen=True)
class AppContext:
    db_path: Path
    service: LedgerQueryService
    category_config: CategoriesConfig
    stats: dict
    earliest: date
    latest: date
    start_date: date
    end_date: date
    period_label: str
    review_source: dict
    review_db: Path
    pending_reviews: int


@st.cache_resource
def get_query_service(db_path: Path) -> LedgerQueryService:
    return LedgerQueryService(db_path)


def _query_service_supports_period_quality(service: object) -> bool:
    try:
        params = inspect.signature(getattr(service, "get_duplicate_candidates")).parameters  # noqa: B009
    except AttributeError, TypeError, ValueError:
        return False
    return {"start_date", "end_date"}.issubset(params)


def get_compatible_query_service(db_path: Path) -> LedgerQueryService:
    service = get_query_service(db_path)
    if not _query_service_supports_period_quality(service):
        get_query_service.clear()
        service = get_query_service(db_path)
    return service


@cache_database_read
def get_category_config(
    db_path: Path, merchants_path: Path, categories_path: Path
) -> CategoriesConfig:
    from ledger.imports.pdf.readiness import live_mutation_boundary

    repo = SQLiteConfigRepository(db_path, prepare_schema=False)
    if not repo.has_runtime_config():
        with live_mutation_boundary(db_path, operation="ui_config_seed"):
            # Re-check inside the shared exclusion boundary: another process may have
            # seeded after the observational check but before this lock was acquired.
            guarded_repo = SQLiteConfigRepository(db_path, prepare_schema=False)
            if not guarded_repo.has_runtime_config():
                seed_config_database(db_path, merchants_path, categories_path)
    return repo.load_categories()


def choose_default_page(pending_reviews: int, *, has_transactions: bool = True) -> str:
    if not has_transactions and pending_reviews == 0:
        return "import-center"
    return "review" if pending_reviews > 0 else "overview"


def resolve_default_db() -> Path:
    from ledger.runtime import default_database_path

    return default_database_path().resolve()


def get_period_dates(period_type: str, reference_date: date | None = None) -> tuple[date, date]:
    today = reference_date or date.today()
    first_this_month = today.replace(day=1)

    if period_type == "this_month":
        return first_this_month, today
    if period_type == "last_month":
        last_month_end = first_this_month - timedelta(days=1)
        return last_month_end.replace(day=1), last_month_end
    if period_type == "this_quarter":
        quarter_month = ((today.month - 1) // 3) * 3 + 1
        return date(today.year, quarter_month, 1), today
    if period_type == "last_quarter":
        quarter_month = ((today.month - 1) // 3) * 3 + 1
        this_quarter_start = date(today.year, quarter_month, 1)
        last_quarter_end = this_quarter_start - timedelta(days=1)
        last_quarter_month = ((last_quarter_end.month - 1) // 3) * 3 + 1
        return date(last_quarter_end.year, last_quarter_month, 1), last_quarter_end
    if period_type == "this_year":
        return date(today.year, 1, 1), today
    if period_type == "last_year":
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    if period_type == "ytd":
        return date(today.year, 1, 1), today
    if period_type == "last_30d":
        return today - timedelta(days=30), today
    if period_type == "last_90d":
        return today - timedelta(days=90), today
    return first_this_month, today


def clamp_period(start: date, end: date, earliest: date, latest: date) -> tuple[date, date]:
    clamped_start = max(start, earliest)
    clamped_end = min(end, latest)
    if clamped_start > clamped_end:
        return earliest, latest
    return clamped_start, clamped_end


def resolve_preset_period(
    period_type: str,
    earliest: date,
    latest: date,
    today: date | None = None,
) -> tuple[date, date]:
    """Resolve a relative preset (this_month/this_quarter/...) to a concrete window
    within the available data.

    A current-period preset ("This Month"/"This Quarter"/"This Year") is computed
    relative to *today*. When today is PAST the newest transaction (e.g. it's July but
    the latest imported statement is in June), that window falls entirely after the
    data, and ``clamp_period`` would return the whole date range — so "This Quarter"
    silently shows 18 months. Detect that case and re-anchor the preset to ``latest``
    so it tracks the most recent real month/quarter instead. Presets that already land
    inside the data (Last Month/Quarter, This Year, etc.) are unaffected.
    """
    today = today or date.today()
    start, end = get_period_dates(period_type, today)
    if start > latest:
        start, end = get_period_dates(period_type, latest)
    return clamp_period(start, end, earliest, latest)


def format_period_label(start_date: date, end_date: date) -> str:
    if start_date.year == end_date.year:
        if start_date.month == end_date.month:
            return start_date.strftime("%B %Y")
        return f"{start_date.strftime('%b')} - {end_date.strftime('%b %Y')}"
    return f"{start_date.strftime('%b %Y')} - {end_date.strftime('%b %Y')}"


def get_active_import_run(
    runs_dir: Path | None = None,
    *,
    finalizable_only: bool = False,
) -> dict | None:
    runs_dir = resolve_runtime_path(runs_dir, "import-runs")
    latest = runs_dir / "latest-run.txt"
    if not latest.exists():
        return None

    # Normalize the path read from latest-run.txt (expanduser), matching the handling
    # in imports/pdf/files.py, so a "~"-relative run path resolves consistently.
    run_dir = Path(latest.read_text(encoding="utf-8").strip()).expanduser()
    run_path = run_dir / "run.json"
    if not run_path.exists():
        return None

    try:
        run_state = json.loads(run_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError, OSError:
        return None
    if finalizable_only and str(run_state.get("status", "")) not in FINALIZABLE_STATUSES:
        # A cancelled/finalized/failed run's staging DB deliberately survives
        # cleanup, and latest-run.txt keeps pointing at it. Review routing must
        # skip it: the save path refuses exactly these statuses, so offering the
        # run as the review target strands every decision behind a save-time
        # error while the banner promises a finalize that can never happen. The
        # Import Center keeps the default (it labels terminal runs explicitly).
        return None
    staging_db = Path(run_state.get("staging_db_path", ""))
    if not staging_db.exists():
        return None

    return {
        "run_dir": run_dir,
        "runs_dir": runs_dir,
        "run_state": run_state,
        "staging_db": staging_db,
    }


def get_review_source(db_path: Path) -> dict:
    """Resolve which database the Review Queue reads AND writes.

    ``is_live_db`` is carried explicitly rather than left to be inferred from the
    label or the status string. With no active import run this silently retargets the
    live ledger, and Save runs ``apply_review_decisions`` against real transactions
    with no staging copy to discard — the page has to be able to say so out loud.
    """
    # Import paths are anchored beside the selected database. A custom
    # LEDGER_DB must route Review Queue to that database's staging run instead
    # of accidentally probing the repository's default data/import-runs.
    runs_dir = Path(db_path).expanduser().resolve().parent / "import-runs"
    active_run = get_active_import_run(runs_dir, finalizable_only=True)
    if active_run:
        run_state = active_run["run_state"]
        return {
            "label": active_run["run_dir"].name,
            "status": run_state.get("status", "unknown"),
            "db_path": active_run["staging_db"],
            "source_kind": "Active Import",
            "is_live_db": False,
            "run_dir": active_run["run_dir"],
            "runs_dir": active_run["runs_dir"],
        }

    return {
        "label": db_path.name,
        "status": "live_db",
        "db_path": db_path,
        "source_kind": "Current DB",
        "is_live_db": True,
    }


def load_review_category_config(review_db: Path) -> CategoriesConfig | None:
    """Categories as seen by the database review decisions are applied TO.

    ``apply_review_decisions`` loads its merchant/category config from the REVIEW
    database, so any picker built from the live config can offer a category the
    applier will not recognise: the row resolves, ``exclude_from_spending`` falls
    through to ``False``, and 'manual' confidence freezes that answer in place.

    Deliberately uncached — the review database's config can change under an open
    page (a rules edit, a re-stage), and a stale dropdown reintroduces exactly the
    divergence this exists to close. Returns ``None`` when that database carries no
    runtime config so the caller can say so rather than render an empty dropdown.
    """
    try:
        if not review_db.is_file():
            return None
        with bound_database_read_connection(
            review_db,
            label="Review Queue category-config source",
            require_main_owner_only=True,
            require_parent_owner_only=True,
        ) as connection:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "select name from sqlite_master where type = 'table' "
                    "and name in ('merchant_rules', 'category_config', "
                    "'category_merchants', 'config_metadata')"
                )
            }
            required_tables = {
                "merchant_rules",
                "category_config",
                "category_merchants",
                "config_metadata",
            }
            if tables != required_tables:
                return None
            merchant_count = int(
                connection.execute("select count(*) from merchant_rules").fetchone()[0]
            )
            category_count = int(
                connection.execute("select count(*) from category_config").fetchone()[0]
            )
            if merchant_count == 0 or category_count == 0:
                return None
            metadata = {
                str(row[0]): str(row[1])
                for row in connection.execute(
                    "select key, value from config_metadata "
                    "where key in ('categories_version', 'categories_default')"
                )
            }
            categories = []
            for name, parent, exclude_from_spending in connection.execute(
                "select name, parent, exclude_from_spending from category_config "
                "order by sort_order"
            ):
                merchants = [
                    str(row[0])
                    for row in connection.execute(
                        "select merchant from category_merchants "
                        "where category_name = ? order by sort_order",
                        (name,),
                    )
                ]
                categories.append(
                    CategoryRule(
                        name=str(name),
                        parent=str(parent) if parent is not None else None,
                        merchants=merchants,
                        exclude_from_spending=bool(exclude_from_spending),
                    )
                )
            return CategoriesConfig(
                version=metadata.get("categories_version", "sqlite"),
                default_category=metadata.get("categories_default", "Other"),
                categories=categories,
            )
    except Exception:
        logger.warning("could not load category config from the review database")
        return None


def render_period_controls(
    earliest: date,
    latest: date,
    *,
    requested_period: tuple[date, date] | None = None,
) -> tuple[date, date, str]:
    period_map = {
        "This Month": "this_month",
        "Last Month": "last_month",
        "This Quarter": "this_quarter",
        "Last Quarter": "last_quarter",
        "This Year": "this_year",
        "Last Year": "last_year",
        "Year to Date": "ytd",
        "Last 30 Days": "last_30d",
        "Last 90 Days": "last_90d",
    }

    default_preset = st.session_state.get("period_preset", "This Month")
    if default_preset not in PERIOD_PRESETS:
        default_preset = "This Month"
    if requested_period is not None:
        current_period: tuple[date | None, date | None]
        if default_preset == "All Time":
            current_period = (earliest, latest)
        elif default_preset == "Custom Range":
            current_period = (
                st.session_state.get("custom_start_date"),
                st.session_state.get("custom_end_date"),
            )
        else:
            current_period = resolve_preset_period(period_map[default_preset], earliest, latest)
        if current_period != requested_period:
            default_preset = "Custom Range"
            st.session_state["period_preset"] = default_preset
            st.session_state["custom_start_date"], st.session_state["custom_end_date"] = (
                requested_period
            )

    preset = st.sidebar.selectbox(
        "Period",
        PERIOD_PRESETS,
        index=PERIOD_PRESETS.index(default_preset),
        key="period_preset",
    )

    if preset == "All Time":
        start_date, end_date = earliest, latest
    elif preset == "Custom Range":
        start_default = st.session_state.get("custom_start_date", earliest)
        end_default = st.session_state.get("custom_end_date", latest)
        # Planning periods need not contain imported transactions. Keep a
        # rolling ten-year window available, including any older imported or
        # explicitly linked dates without changing their evidence period.
        today = date.today()
        minimum = min(earliest, start_default, end_default, date(today.year - 10, 1, 1))
        maximum = max(latest, start_default, end_default, date(today.year + 10, 12, 31))
        col1, col2 = st.sidebar.columns(2)
        with col1:
            start_date = st.date_input(
                "From",
                value=start_default,
                min_value=minimum,
                max_value=maximum,
                key="custom_start_date",
            )
        with col2:
            end_date = st.date_input(
                "To",
                value=end_default,
                min_value=minimum,
                max_value=maximum,
                key="custom_end_date",
            )
        if start_date > end_date:
            st.sidebar.warning("Start date is after end date; using the full range.")
            start_date, end_date = earliest, latest
    else:
        start_date, end_date = resolve_preset_period(period_map[preset], earliest, latest)

    return start_date, end_date, format_period_label(start_date, end_date)


def _render_database_recovery(db_path: Path) -> None:
    """Stop before ordinary dashboard SQLite access can recover a hot journal."""
    from ledger.imports.pdf.readiness import (
        LiveMutationDuringOpenImportRefused,
        live_mutation_boundary,
    )

    st.title("Restore database access")
    st.info(
        "A change may still be running or Ledger may have stopped during a write. "
        "Wait for any running operation, then recover database access to continue. "
        "Saved proposal decisions are preserved."
    )
    if st.button("Recover database access", key="ledger_storage_recover"):
        try:
            with live_mutation_boundary(db_path, operation="planning_storage_recovery"):
                recover_database_journal(db_path, label="Dashboard storage recovery")
        except LiveMutationDuringOpenImportRefused:
            st.error("An import can still promote. Finalize or cancel it before recovery.")
        except DatabaseTargetLockError:
            st.error("The database is busy. Wait for the other operation, then retry.")
        except Exception:
            st.error(
                "Database recovery could not complete. Keep the files intact for local repair."
            )
        else:
            invalidate_db_caches()
            st.session_state.pop("planning_preview", None)
            st.rerun()
    st.stop()


def build_app_context() -> AppContext:
    st.sidebar.title("Ledger")
    st.sidebar.caption("Local finance workspace")

    default_db = resolve_default_db()
    db_path_str = st.sidebar.text_input(
        "Database",
        value=str(default_db),
        help="Path to the SQLite ledger database.",
    )
    db_path = Path(db_path_str).expanduser().resolve()
    navigation = consume_brief_navigation(db_path)

    if not db_path.exists():
        st.title("Welcome to Ledger")
        st.info("Create a local ledger, then upload your first statements in Import Center.")
        if st.button("Create ledger", type="primary"):
            try:
                with database_target_lock(db_path):
                    # Explicit creation must also recreate a path whose previous
                    # database was moved away after the schema memo was filled.
                    run_migrations(db_path)
            except Exception as exc:
                st.error(f"Could not create the ledger: {exc}")
            else:
                st.rerun()
        st.stop()

    try:
        # Hold the publisher lock across the check and initialization: an app
        # writer must not leave a hot journal between them. Existing journal
        # state is handled only by the explicit guarded recovery action.
        with database_target_lock(db_path):
            needs_recovery = os.path.lexists(f"{db_path}-journal")
            if not needs_recovery:
                run_migrations(db_path)
        if needs_recovery:
            _render_database_recovery(db_path)
        service = get_compatible_query_service(db_path)
        merchants_path = seed_config_path("merchants.toml")
        categories_path = seed_config_path("categories.toml")
        if not merchants_path.exists() or not categories_path.exists():
            st.error("Bundled seed configuration is unavailable")
            st.stop()
        category_config = get_category_config(db_path, merchants_path, categories_path)
    except Exception as exc:
        st.error(f"Could not initialize Ledger dashboard: {exc}")
        st.stop()

    stats = service.get_stats()
    earliest, latest = service.get_date_range()
    if stats["total_transactions"] and (not earliest or not latest):
        st.error("No transaction date range is available.")
        st.stop()
    earliest = earliest or date.today()
    latest = latest or earliest

    st.sidebar.divider()
    st.sidebar.subheader("Date Range")
    start_date, end_date, period_label = render_period_controls(
        earliest,
        latest,
        requested_period=(navigation.start_date, navigation.end_date) if navigation else None,
    )
    st.sidebar.caption(f"Viewing {period_label}")

    review_source = get_review_source(db_path)
    review_db = review_source["db_path"]
    pending_reviews = pending_review_count(review_db)

    st.sidebar.divider()
    st.sidebar.subheader("Status")
    st.sidebar.metric("Pending Review", pending_reviews)
    st.sidebar.metric("Transactions", f"{stats['total_transactions']:,}")
    st.sidebar.caption(f"Data from {stats['earliest_date']} to {stats['latest_date']}")

    return AppContext(
        db_path=db_path,
        service=service,
        category_config=category_config,
        stats=stats,
        earliest=earliest,
        latest=latest,
        start_date=start_date,
        end_date=end_date,
        period_label=period_label,
        review_source=review_source,
        review_db=review_db,
        pending_reviews=pending_reviews,
    )


def review_examples_text(raw_examples: str) -> str:
    try:
        examples = json.loads(raw_examples or "[]")
    except json.JSONDecodeError:
        return ""

    labels = []
    for example in examples[:3]:
        labels.append(
            f"{example.get('posting_date', '')} {example.get('amount_signed', '')}: "
            f"{example.get('description', '')}"
        )
    return "\n".join(labels)


def review_amount(rows: list[dict]) -> Decimal:
    total = Decimal("0")
    for row in rows:
        total += Decimal(str(row.get("total_abs_amount") or "0"))
    return total
