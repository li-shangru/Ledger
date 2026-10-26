"""Root-level core CLI commands: build, parsers, db-info, query, spending, health, patterns."""

from __future__ import annotations

import logging
from contextlib import ExitStack, closing
from decimal import Decimal
from pathlib import Path
from typing import Annotated

import typer
from rich.table import Table

from ledger import __version__
from ledger.commands.common import (
    console,
    resolve_db_path,
    setup_logging,
    validate_period,
)
from ledger.config import (
    find_cli_defaults_file,
    get_config_versions,
    load_app_config,
    load_cli_defaults,
    load_ledger_config,
)
from ledger.config.repository import SQLiteConfigRepository, load_runtime_app_config
from ledger.output import write_all
from ledger.parsers import get_all_parsers
from ledger.processing import compute_summary, process
from ledger.runtime import require_external_runtime_path
from ledger.storage.read_only import DatabaseChangedError, ReadOnlyDatabase
from ledger.utils import script_version
from ledger.utils.private_files import ensure_private_dir

# Output format tokens accepted by --formats. "none" is a valid no-op (write nothing);
# the rest map to writers in ledger.output.write_all.
KNOWN_FORMATS = ("csv", "parquet", "json", "none")


def validate_formats(formats: str) -> list[str]:
    """Parse and validate the --formats token list.

    Returns the normalized (lower-cased, stripped) tokens. Raises
    ``typer.BadParameter`` listing the valid formats if any token is unknown, so a
    typo like ``--formats bogus`` fails loudly instead of silently writing nothing.
    """
    tokens = [f.strip().lower() for f in (formats or "").split(",") if f.strip()]
    unknown = [token for token in tokens if token not in KNOWN_FORMATS]
    if unknown:
        raise typer.BadParameter(
            f"unknown format(s): {', '.join(unknown)}. Valid formats: {', '.join(KNOWN_FORMATS)}"
        )
    return tokens


def validate_date_option(value: str | None, name: str):
    """Validate an optional date option (``--start`` / ``--end``).

    Returns the parsed ``date`` (or ``None`` when not supplied). Raises
    ``typer.BadParameter`` on an unparseable date so an invalid value fails loudly
    instead of being silently dropped (which would return the full unfiltered set).
    """
    if value is None:
        return None
    from ledger.utils import parse_date

    parsed = parse_date(value)
    if parsed is None:
        raise typer.BadParameter(f"{name} must be a valid date (YYYY-MM-DD)")
    return parsed


def build(
    src: Annotated[
        Path | None, typer.Option("--src", help="Source directory with CSV exports")
    ] = None,
    out_dir: Annotated[Path | None, typer.Option("--out-dir", help="Output directory")] = None,
    ledger_config: Annotated[
        Path | None, typer.Option("--ledger-config", help="Ledger config TOML")
    ] = None,
    merchant_rules: Annotated[
        Path | None, typer.Option("--merchant-rules", help="Merchant rules TOML")
    ] = None,
    categories: Annotated[Path | None, typer.Option("--categories", help="Categories TOML")] = None,
    db_path: Annotated[
        Path | None, typer.Option("--db", help="SQLite database path (enables database storage)")
    ] = None,
    year: Annotated[str | None, typer.Option("--year", help="Filter by year (YYYY)")] = None,
    month: Annotated[str | None, typer.Option("--month", help="Filter by month (YYYY-MM)")] = None,
    quarter: Annotated[
        str | None, typer.Option("--quarter", help="Filter by quarter (YYYY-QN)")
    ] = None,
    formats: Annotated[
        str | None, typer.Option("--formats", help="Output formats (csv,parquet,json)")
    ] = None,
    include_raw: Annotated[
        bool, typer.Option("--include-raw", help="Include raw data for debugging")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Parse only, don't write files")
    ] = False,
    log_level: Annotated[str | None, typer.Option("--log-level", help="Log level")] = None,
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="CLI defaults config file (overrides default locations)"),
    ] = None,
) -> None:
    """
    Build master ledger from CSV exports.

    Parses bank/credit card CSVs, normalizes merchants and categories,
    deduplicates, and outputs to multiple formats.

    When a database path is provided by --db or CLI defaults, automatically runs all analyses:
    - Account detection and balance calculation
    - Recurring pattern detection (subscriptions, bills, income)
    - Spending trend aggregation
    - Financial health scoring

    Arguments can be provided via CLI flags or loaded from a defaults config file.
    CLI flags override defaults. Use --config or the private runtime defaults.toml.
    """
    # Load defaults from config file
    defaults = load_cli_defaults(config_file)
    defaults_file = config_file or find_cli_defaults_file()

    # Merge CLI args with defaults (CLI args take precedence)
    src = Path(src) if src else (Path(defaults.src) if defaults.src else None)
    out_dir = Path(out_dir) if out_dir else (Path(defaults.out_dir) if defaults.out_dir else None)
    ledger_config = (
        Path(ledger_config)
        if ledger_config
        else (Path(defaults.ledger_config) if defaults.ledger_config else None)
    )
    merchant_rules = (
        Path(merchant_rules)
        if merchant_rules
        else (Path(defaults.merchant_rules) if defaults.merchant_rules else None)
    )
    categories = (
        Path(categories)
        if categories
        else (Path(defaults.categories) if defaults.categories else None)
    )
    db_path = Path(db_path) if db_path else (Path(defaults.db) if defaults.db else None)
    year = year or defaults.year
    month = month or defaults.month
    quarter = quarter or defaults.quarter
    formats = formats or defaults.formats
    log_level = log_level or defaults.log_level

    setup_logging(log_level)
    logging.getLogger(__name__)

    # Check required arguments
    if not src:
        console.print("[red]Error:[/red] --src is required (or set in config)")
        raise typer.Exit(1)
    if not out_dir:
        console.print("[red]Error:[/red] --out-dir is required (or set in config)")
        raise typer.Exit(1)
    if not ledger_config:
        console.print("[red]Error:[/red] --ledger-config is required (or set in config)")
        raise typer.Exit(1)
    if not merchant_rules:
        console.print("[red]Error:[/red] --merchant-rules is required (or set in config)")
        raise typer.Exit(1)
    if not categories:
        console.print("[red]Error:[/red] --categories is required (or set in config)")
        raise typer.Exit(1)

    # Validate paths
    # Reject every financial destination before seeding or mutating a database.
    # Validate lexical paths first so checkout-contained aliases cannot hide.
    src = require_external_runtime_path(src).resolve()
    out_dir = require_external_runtime_path(out_dir).resolve()
    if db_path is not None:
        db_path = require_external_runtime_path(db_path).resolve()

    if not src.exists():
        console.print(f"[red]Error:[/red] Source directory not found: {src}")
        raise typer.Exit(1)

    for path, name in [
        (ledger_config, "ledger-config"),
        (merchant_rules, "merchant-rules"),
        (categories, "categories"),
    ]:
        path = path.expanduser().resolve()
        if not path.exists():
            console.print(f"[red]Error:[/red] {name} not found: {path}")
            raise typer.Exit(1)

    # Validate period filters
    year = validate_period(year, r"20\d{2}", "--year")
    month = validate_period(month, r"20\d{2}-\d{2}", "--month")
    quarter = validate_period(quarter, r"20\d{2}-Q[1-4]", "--quarter")

    # Infer year from month/quarter if not specified
    if month and not year:
        year = month[:4]
    if quarter and not year:
        year = quarter[:4]

    # Parse and validate formats (unknown tokens fail loudly instead of writing nothing)
    format_list = validate_formats(formats)

    console.print(f"\n[bold]Ledger[/bold] v{__version__}")
    if defaults_file:
        console.print(f"Config: {defaults_file}")
    console.print(f"Source: {src}")
    console.print(f"Output: {out_dir}")

    # Retain one guarded identity across every dry-run config and preflight read.
    # Unsafe existing targets refuse before configuration can open a writable DB.
    with ExitStack() as read_scope:
        dry_run_database = (
            read_scope.enter_context(closing(ReadOnlyDatabase(db_path)))
            if dry_run and db_path is not None and db_path.exists()
            else None
        )
        # Load configuration. When writing to SQLite, seed/read merchant and
        # category config through the runtime config tables before processing.
        with console.status("Loading configuration..."):
            ledger_config = ledger_config.expanduser().resolve()
            merchant_rules = merchant_rules.expanduser().resolve()
            categories = categories.expanduser().resolve()
            # Dry runs never initialize or seed a target, including an existing DB
            # without runtime configuration. Read its configuration only when present.
            dry_run_repository = (
                SQLiteConfigRepository(db_path, read_only_database=dry_run_database)
                if db_path is not None and dry_run_database is not None
                else None
            )
            if dry_run_repository is not None and dry_run_repository.has_runtime_config():
                config = dry_run_repository.load_app_config(load_ledger_config(ledger_config))
                validation = dry_run_repository.validate()
                if not validation.ok:
                    for issue in validation.issues:
                        console.print(f"[red]Config issue:[/red] {issue}")
                    raise typer.Exit(1)
            elif db_path and not dry_run:
                db_path = db_path.expanduser().resolve()
                # Owner-only like every other managed-path creator: a bare mkdir
                # inherited the umask and could mint a world-readable data directory.
                ensure_private_dir(db_path.parent)
                ledger = load_ledger_config(ledger_config)
                config = load_runtime_app_config(
                    db_path,
                    ledger,
                    merchant_rules,
                    categories,
                )
                validation = SQLiteConfigRepository(db_path).validate()
                if not validation.ok:
                    for issue in validation.issues:
                        console.print(f"[red]Config issue:[/red] {issue}")
                    raise typer.Exit(1)
            else:
                config = load_app_config(
                    ledger_config,
                    merchant_rules,
                    categories,
                )
            config_versions = get_config_versions(
                ledger_config,
                merchant_rules,
                categories,
            )

        console.print(f"Merchants: {len(config.merchants.rules)} rules")
        console.print(f"Categories: {len(config.categories.categories)} categories")

        # Process files
        console.print("\n[bold]Processing...[/bold]")

        result = process(
            config=config,
            source_root=src,
            year=year,
            month=month,
            quarter=quarter,
            include_raw=include_raw,
            defer_identity_check_to_writer=db_path is not None,
        )

        if db_path is not None:
            from ledger.storage.database import session_scope
            from ledger.storage.writer import DatabaseWriter

            # Validate the explicitly selected target before summaries or exports.
            # The writer repeats this check in its write transaction; this preflight
            # neither creates a missing target nor permits a dry run to write to it.
            if dry_run_database is not None:
                with dry_run_database.session_scope(db_path) as session:
                    DatabaseWriter.validate_identity(result, session)
            elif dry_run:
                if db_path.exists():
                    raise DatabaseChangedError
                DatabaseWriter.validate_identity(result)
            elif db_path.exists():
                # Normal builds already use writable sessions for this explicit
                # target. Preserve their support for valid WAL-mode databases.
                with session_scope(db_path) as session:
                    DatabaseWriter.validate_identity(result, session)
            else:
                DatabaseWriter.validate_identity(result)

    # Compute summary
    filters = {"year": year, "month": month, "quarter": quarter}
    summary = compute_summary(
        result=result,
        config=config,
        source_root=src,
        filters=filters,
        config_versions=config_versions,
        parser_version=script_version(),
    )

    # Display results table
    table = Table(title="Processing Results")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right")

    table.add_row("Files scanned", str(result.total_scanned))
    table.add_row("Transactions", str(len(result.transactions)))
    table.add_row("Transfers", str(len(result.transfers)))
    table.add_row("Exceptions", str(len(result.exceptions)))
    table.add_row("Duplicates removed", str(result.duplicates_removed))
    table.add_row("Collisions", str(result.collisions))
    table.add_row("", "")
    table.add_row("Income", f"${summary.cashflow_income:,.2f}")
    table.add_row("Expenses", f"${summary.cashflow_expense:,.2f}")
    table.add_row("Net", f"${summary.cashflow_net:,.2f}")

    console.print(table)

    if dry_run:
        console.print("\n[yellow]Dry run - no files written[/yellow]")
        return

    # Write to database (if enabled)
    if db_path:
        from ledger.storage.writer import DatabaseWriter

        db_path = db_path.expanduser().resolve()
        console.print(f"\n[bold]Writing to database:[/bold] {db_path}")

        with console.status("Writing to database..."):
            writer = DatabaseWriter(db_path)
            writer.write_result(result, src, script_version())

        stats = writer.get_stats()
        console.print(f"  • Total transactions in DB: {stats['total_transactions']}")
        console.print(f"  • Total transfers in DB: {stats['total_transfers']}")
        console.print(f"  • Date range: {stats['earliest_date']} to {stats['latest_date']}")

        # Run all analyses automatically
        console.print("\n[bold]Running analysis...[/bold]")
        from ledger.analysis import AnalysisRunner

        analysis = AnalysisRunner(db_path)

        # 1. Account detection and balance calculation
        with console.status("Detecting accounts and computing balances..."):
            account_stats = analysis.run_account_analysis()
        console.print(f"  • Detected {account_stats['accounts_detected']} accounts")
        console.print(f"  • Computed {account_stats['balances_computed']} daily balances")

        # 2. Pattern detection
        with console.status("Detecting recurring patterns..."):
            pattern_stats = analysis.run_pattern_detection()
        console.print(f"  • Detected {pattern_stats['patterns_detected']} recurring patterns")
        console.print(
            f"  • Matched {pattern_stats['transactions_matched']} transactions to patterns"
        )

        # 3. Trend aggregation
        with console.status("Aggregating spending trends..."):
            trend_stats = analysis.run_trend_aggregation(days=30, months=12)
        console.print(f"  • Computed {trend_stats['daily_trends_computed']} daily trends")
        console.print(f"  • Computed {trend_stats['monthly_trends_computed']} monthly trends")

        # 4. Health scoring
        with console.status("Computing financial health scores..."):
            health_stats = analysis.run_health_scoring(days=30)
        console.print(f"  • Computed {health_stats['scores_computed']} health scores")

        from ledger.reporting import ReportSnapshotService

        with console.status("Refreshing report snapshot..."):
            snapshot = ReportSnapshotService(db_path).create_snapshot(source="cli_build")
        console.print(f"  • Created report snapshot #{snapshot['id']}")

    # Write outputs
    label = year or "all"
    if month:
        label = month
    if quarter:
        label = quarter

    with console.status("Writing output files..."):
        outputs = write_all(
            output_dir=out_dir,
            transactions=result.transactions,
            transfers=result.transfers,
            exceptions=result.exceptions,
            summary=summary,
            label=label,
            formats=format_list,
        )

    console.print(f"\n[green]✓[/green] Wrote {len(outputs)} files to {out_dir}")

    for _name, path in outputs.items():
        console.print(f"  • {path.name}")


def parsers() -> None:
    """List available parsers."""
    table = Table(title="Registered Parsers")
    table.add_column("Format", style="cyan")
    table.add_column("Institution")
    table.add_column("Required Headers")

    for name, cls in sorted(get_all_parsers().items()):
        headers = ", ".join(sorted(cls.REQUIRED_HEADERS)[:5])
        if len(cls.REQUIRED_HEADERS) > 5:
            headers += f" (+{len(cls.REQUIRED_HEADERS) - 5} more)"
        table.add_row(name, cls.INSTITUTION, headers)

    console.print(table)


def db_info(
    db_path: Annotated[Path | None, typer.Option("--db", help="Database path")] = None,
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="CLI defaults config file (overrides default locations)"),
    ] = None,
) -> None:
    """Show database statistics."""
    from ledger.storage.writer import DatabaseWriter

    db_path = resolve_db_path(db_path, config_file)

    writer = DatabaseWriter(db_path)
    stats = writer.get_stats()

    table = Table(title=f"Database: {db_path.name}")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right")

    table.add_row("Total Transactions", str(stats["total_transactions"]))
    table.add_row("Total Transfers", str(stats["total_transfers"]))
    table.add_row("Import Runs", str(stats["import_runs"]))
    if stats["earliest_date"] and stats["latest_date"]:
        table.add_row("Date Range", f"{stats['earliest_date']} to {stats['latest_date']}")
    else:
        table.add_row("Date Range", "No data")

    console.print(table)


def query(
    db_path: Annotated[Path | None, typer.Option("--db", help="Database path")] = None,
    start_date: Annotated[
        str | None, typer.Option("--start", help="Start date (YYYY-MM-DD)")
    ] = None,
    end_date: Annotated[str | None, typer.Option("--end", help="End date (YYYY-MM-DD)")] = None,
    category: Annotated[str | None, typer.Option("--category", help="Filter by category")] = None,
    merchant: Annotated[str | None, typer.Option("--merchant", help="Filter by merchant")] = None,
    search: Annotated[str | None, typer.Option("--search", help="Search description")] = None,
    limit: Annotated[int, typer.Option("--limit", help="Max results")] = 50,
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="CLI defaults config file (overrides default locations)"),
    ] = None,
) -> None:
    """Query transactions from database."""
    from ledger.query import LedgerQueryService

    db_path = resolve_db_path(db_path, config_file)

    service = LedgerQueryService(db_path)

    # Parse and validate dates: an invalid date fails loudly instead of being
    # silently dropped (which would return the full unfiltered dataset).
    start = validate_date_option(start_date, "--start")
    end = validate_date_option(end_date, "--end")

    # Query
    transactions = service.get_transactions(
        start_date=start,
        end_date=end,
        categories=[category] if category else None,
        merchants=[merchant] if merchant else None,
        description_search=search,
        limit=limit,
    )

    # Display
    table = Table(title=f"Transactions ({len(transactions)} results)")
    table.add_column("Date", style="cyan")
    table.add_column("Merchant")
    table.add_column("Category")
    table.add_column("Amount", justify="right")

    for txn in transactions:
        table.add_row(
            str(txn.posting_date),
            txn.merchant or txn.description[:30],
            txn.category,
            f"${txn.amount_signed:,.2f}",
        )

    console.print(table)


def _spending_view_rows(
    by: str,
    data: dict[str, Decimal],
    period_total: Decimal,
) -> tuple[list[tuple[str, str, str]], tuple[str, str, str]]:
    """Build the (rows, footer) for the spending breakdown table.

    Percentages are relative to the TRUE period total. The category view spans the
    whole spend universe, so its footer is the real ``TOTAL`` (100%). The merchant
    view shows only the top-N merchants, so its footer is an honest
    ``TOP 20 SUBTOTAL`` — a share of the period, not a fake 100% that implies the
    listed rows are all the spending there was.
    """
    rows: list[tuple[str, str, str]] = []
    for name, amount in sorted(data.items(), key=lambda x: x[1], reverse=True):
        pct = (amount / period_total * 100) if period_total > 0 else Decimal(0)
        rows.append((name, f"${amount:,.2f}", f"{pct:.1f}%"))

    shown_total = sum(data.values(), Decimal(0))
    footer_pct = (shown_total / period_total * 100) if period_total > 0 else Decimal(0)
    label = "TOTAL" if by == "category" else "TOP 20 SUBTOTAL"
    footer = (label, f"${shown_total:,.2f}", f"{footer_pct:.1f}%")
    return rows, footer


def spending(
    db_path: Annotated[Path | None, typer.Option("--db", help="Database path")] = None,
    start_date: Annotated[
        str | None, typer.Option("--start", help="Start date (YYYY-MM-DD)")
    ] = None,
    end_date: Annotated[str | None, typer.Option("--end", help="End date (YYYY-MM-DD)")] = None,
    by: Annotated[str, typer.Option("--by", help="Group by: category or merchant")] = "category",
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="CLI defaults config file (overrides default locations)"),
    ] = None,
) -> None:
    """Show spending breakdown."""
    from ledger.query import LedgerQueryService
    from ledger.utils import parse_date

    db_path = resolve_db_path(db_path, config_file)

    service = LedgerQueryService(db_path)

    if not start_date or not end_date:
        console.print("[red]Error:[/red] --start and --end are required")
        raise typer.Exit(1)

    start = parse_date(start_date)
    end = parse_date(end_date)
    if start is None or end is None:
        console.print("[red]Error:[/red] --start and --end must be valid dates")
        raise typer.Exit(1)

    if by == "category":
        data = service.get_spending_by_category(start, end)
        # Category view spans the whole spend universe, so the shown rows ARE the
        # period total.
        period_total = sum(data.values(), Decimal(0))
    elif by == "merchant":
        data = service.get_spending_by_merchant(start, end, top_n=20)
        # Merchant view shows only the top 20; percentages and the footer are taken
        # against the TRUE period spend (the full category universe) so the long
        # tail is not silently dropped from the denominator.
        period_total = sum(service.get_spending_by_category(start, end).values(), Decimal(0))
    else:
        console.print("[red]Error:[/red] --by must be 'category' or 'merchant'")
        raise typer.Exit(1)

    # Display
    table = Table(title=f"Spending by {by.title()} ({start} to {end})")
    table.add_column(by.title(), style="cyan")
    table.add_column("Total", justify="right")
    table.add_column("%", justify="right")

    rows, footer = _spending_view_rows(by, data, period_total)
    for name, amount_str, pct_str in rows:
        table.add_row(name, amount_str, pct_str)

    table.add_row("", "", "")
    table.add_row(footer[0], footer[1], footer[2], style="bold")

    console.print(table)


def health(
    db_path: Annotated[Path | None, typer.Option("--db", help="Database path")] = None,
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="CLI defaults config file (overrides default locations)"),
    ] = None,
    days: Annotated[int, typer.Option("--days", help="Number of recent days to show")] = 7,
) -> None:
    """Show financial health scores."""
    from ledger.planning.readiness import health_basis
    from ledger.query import LedgerQueryService

    db_path = resolve_db_path(db_path, config_file)

    service = LedgerQueryService(db_path)
    scores = service.get_health_scores(limit=days)

    if not scores:
        console.print("[yellow]No health scores found. Run analysis first:[/yellow]")
        console.print("  [cyan]ledger build[/cyan]\n")
        raise typer.Exit(0)

    # THE FOURTH SURFACE. The Overview caption, the Planning Readiness tile and
    # the Budgets/Goals Emergency Fund tile all disclose this score's basis; this
    # command rendered the same frozen number bare, under a header that read
    # "Last N Days" over rows that could be weeks old. `--days` is a ROW LIMIT,
    # not a window, so the header now names the days actually shown and the
    # basis sentence -- the identical string the dashboard renders -- says how
    # the newest of them relates to the newest imported day.
    basis = health_basis(db_path)
    span = (
        f"{scores[-1]['score_date']} to {scores[0]['score_date']}"
        if len(scores) > 1
        else f"{scores[0]['score_date']}"
    )
    console.print(
        f"\n[bold cyan]📊 Financial Health Scores ({len(scores)} scored day(s): "
        f"{span})[/bold cyan]\n"
    )
    console.print(f"[dim]{basis['statement']}[/dim]\n")

    # Show latest score prominently
    latest = scores[0]
    score = latest["overall_score"]
    color = "green" if score >= 70 else "yellow" if score >= 50 else "red"

    console.print(
        f"[bold]Latest Score ({latest['score_date']}):[/bold] [{color}]{score}/100[/{color}]"
    )
    console.print(
        f"  Trend: {latest['score_trend_7d'].title()} ({latest['score_change_7d']:+d} points)"
    )
    console.print(f"  Cashflow Health: {latest['cashflow_health']}/100")
    console.print(f"  Spending Consistency: {latest['spending_consistency']}/100")
    console.print(f"  Savings Rate: {latest['savings_rate']:.1f}%\n")

    if latest["risk_factors"]:
        console.print("[bold red]⚠️  Risk Factors:[/bold red]")
        for risk in latest["risk_factors"]:
            console.print(f"  • {risk}")
        console.print()

    # Show recent history
    if len(scores) > 1:
        console.print("[bold]Recent History:[/bold]")
        from rich.table import Table

        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("Date")
        table.add_column("Score", justify="right")
        table.add_column("Trend", justify="center")
        table.add_column("Cashflow", justify="right")
        table.add_column("Consistency", justify="right")
        table.add_column("Savings", justify="right")

        for s in scores[1:]:
            trend_emoji = (
                "📈"
                if s["score_trend_7d"] == "improving"
                else "📉"
                if s["score_trend_7d"] == "declining"
                else "➡️"
            )
            table.add_row(
                str(s["score_date"]),
                str(s["overall_score"]),
                trend_emoji,
                str(s["cashflow_health"]),
                str(s["spending_consistency"]),
                f"{s['savings_rate']:.1f}%",
            )

        console.print(table)
        console.print()


def patterns(
    db_path: Annotated[Path | None, typer.Option("--db", help="Database path")] = None,
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="CLI defaults config file (overrides default locations)"),
    ] = None,
    pattern_type: Annotated[
        str | None,
        typer.Option("--type", help="Filter by type (subscription, bill, income, transfer)"),
    ] = None,
    active_only: Annotated[
        bool,
        typer.Option(
            "--active-only/--all",
            help="Show only active patterns (--all shows active and inactive)",
        ),
    ] = True,
) -> None:
    """Show detected recurring patterns."""
    from ledger.query import LedgerQueryService

    db_path = resolve_db_path(db_path, config_file)

    service = LedgerQueryService(db_path)
    patterns_list = service.get_recurring_patterns(
        pattern_type=pattern_type,
        is_active=True if active_only else None,
        limit=100,
    )

    if not patterns_list:
        console.print("[yellow]No recurring patterns found. Run analysis first:[/yellow]")
        console.print("  [cyan]ledger build[/cyan]\n")
        raise typer.Exit(0)

    console.print(f"\n[bold cyan]🔄 Recurring Patterns ({len(patterns_list)} found)[/bold cyan]\n")

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Merchant")
    table.add_column("Category")
    table.add_column("Type")
    table.add_column("Frequency")
    table.add_column("Amount", justify="right")
    table.add_column("Confidence", justify="right")
    table.add_column("Next Expected")

    for p in patterns_list:
        table.add_row(
            p["merchant"],
            p["category"],
            p["pattern_type"].title(),
            p["frequency"].title(),
            f"${p['expected_amount']:.2f}",
            f"{p['confidence']:.0%}",
            str(p["next_expected_date"]),
        )

    console.print(table)

    # Summary stats. Costs only, normalized to a monthly equivalent: filtering on
    # frequency == "monthly" dropped every other cadence (a biweekly bill counted as $0).
    from ledger.analysis.pattern_detector import RECURRING_COST_TYPES, monthly_equivalent

    total_monthly = sum(
        (
            monthly_equivalent(p["expected_amount"], p["frequency"])
            for p in patterns_list
            if p["pattern_type"] in RECURRING_COST_TYPES
        ),
        Decimal("0"),
    )
    subscription_count = sum(1 for p in patterns_list if p["pattern_type"] == "subscription")

    console.print("\n[bold]Summary:[/bold]")
    console.print(f"  Estimated monthly recurring cost: ${total_monthly:.2f}")
    console.print(f"  Active subscriptions: {subscription_count}\n")


def version() -> None:
    """Show version information."""
    console.print(f"Ledger v{__version__}")


def register_build_commands(app: typer.Typer) -> None:
    app.command()(build)
    app.command()(parsers)
    app.command()(db_info)
    app.command()(query)
    app.command()(spending)
    app.command()(health)
    app.command()(patterns)
    app.command()(version)
