"""CLI commands for recomputing derived analysis tables."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from ledger.commands.common import console, resolve_db_path

analysis_app = typer.Typer(help="Recompute derived analysis tables from stored transactions.")


@analysis_app.command("refresh")
def analysis_refresh(
    db_path: Annotated[Path | None, typer.Option("--db", help="SQLite database path")] = None,
    days: Annotated[
        int, typer.Option("--days", help="Trailing days of daily trends and health scores")
    ] = 30,
    months: Annotated[int, typer.Option("--months", help="Trailing months of trends")] = 12,
    skip_adjustments: Annotated[
        bool,
        typer.Option("--skip-adjustments", help="Do not re-detect true-cost offsets"),
    ] = False,
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="CLI defaults config file (overrides default locations)"),
    ] = None,
) -> None:
    """Recompute accounts, patterns, trends, health scores, insights, and quality checks.

    Derived tables are written once and then served as current forever, so a code fix
    does not reach data that was computed by the superseded version. `reports refresh`
    only rebuilds the reporting snapshot from whatever the derived tables already hold;
    this rebuilds the derived tables themselves.

    Reads transactions and rewrites derived tables only — source rows are untouched.
    """
    from ledger.analysis.runner import AnalysisRunner
    from ledger.imports.pdf.readiness import (
        LiveMutationDuringOpenImportRefused,
        live_mutation_boundary,
    )

    db_path = resolve_db_path(db_path, config_file)

    try:
        with (
            live_mutation_boundary(db_path, operation="analysis_refresh"),
            console.status("Recomputing derived analysis..."),
        ):
            results = AnalysisRunner(db_path).run_all(
                source="cli_analysis_refresh",
                days=days,
                months=months,
                detect_adjustments=not skip_adjustments,
            )
    except LiveMutationDuringOpenImportRefused as exc:
        console.print(f"[red]Refused:[/red] {exc}")
        raise typer.Exit(1) from exc

    console.print("[green]✓[/green] Recomputed derived analysis")
    console.print(f"  • Accounts detected: {results.accounts['accounts_detected']:,}")
    console.print(f"  • Balances computed: {results.accounts['balances_computed']:,}")
    console.print(f"  • Recurring patterns: {results.patterns['patterns_detected']:,}")
    console.print(
        f"  • Trends: {results.trends['daily_trends_computed']:,} daily, "
        f"{results.trends['monthly_trends_computed']:,} monthly"
    )
    console.print(f"  • Health scores: {results.health['scores_computed']:,}")
    if not skip_adjustments:
        console.print(
            f"  • Adjustments: {results.adjustments['offsets']:,} offsets, "
            f"{results.adjustments['treatments']:,} treatments"
        )
    counts = results.insights.counts
    console.print(
        f"  • Insight findings: {counts.get('generated', 0):,} generated "
        f"({counts.get('created', 0):,} new, {counts.get('resolved', 0):,} auto-resolved)"
    )
    console.print(
        f"  • Quality checks: {len(results.quality):,} "
        f"({sum(issue.issue_count for issue in results.quality):,} issue(s))"
    )
