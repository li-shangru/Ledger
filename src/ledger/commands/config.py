"""CLI commands for config."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from ledger.commands.common import (
    console,
    print_config_validation,
    resolve_db_path,
)
from ledger.config.loader import load_categories, load_merchant_rules
from ledger.config.repository import SQLiteConfigRepository
from ledger.imports.pdf.readiness import (
    LiveMutationDuringOpenImportRefused,
    live_mutation_boundary,
)
from ledger.runtime import resolve_runtime_path, seed_config_path
from ledger.utils.private_files import ensure_private_dir

config_app = typer.Typer(help="Import, export, and validate runtime config.")
rules_app = typer.Typer(help="Validate and apply runtime merchant/category rules.")


@config_app.command("import")
def config_import(
    db_path: Annotated[Path | None, typer.Option("--db", help="SQLite database path")] = None,
    source: Annotated[
        Path | None,
        typer.Option("--source", help="Directory containing merchants.toml and categories.toml"),
    ] = None,
    execute: Annotated[
        bool,
        typer.Option("--execute", help="Actually replace the runtime config"),
    ] = False,
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="CLI defaults config file (overrides default locations)"),
    ] = None,
) -> None:
    """Replace the SQLite runtime config from TOML seeds. Defaults to a dry run.

    This is a destructive REPLACE, not a merge: every merchant rule, pattern, and
    category in the database is deleted and rebuilt from the TOML files. The tracked
    seeds drift from the runtime config as rules are edited in the app, so importing
    them can silently discard a large amount of accumulated work -- and any category
    a transaction still references disappears with it.

    The dry run reports exactly what would be lost. --execute routes through
    RuntimeConfigMutationService, which takes a pre-mutation backup and restores it
    automatically if the replacement or the subsequent rules pass fails.
    """
    from ledger.rules import RuntimeConfigMutationService

    # This command seeds a brand-new database as well as replacing an existing one.
    db_path = resolve_db_path(db_path, config_file, must_exist=False)
    source = (source or seed_config_path("merchants.toml").parent).expanduser().resolve()

    for name in ["merchants.toml", "categories.toml"]:
        if not (source / name).exists():
            console.print(f"[red]Error:[/red] Missing {source / name}")
            raise typer.Exit(1)

    # Owner-only like every other managed-path creator: a bare mkdir
    # inherited the umask and could mint a world-readable data directory.
    ensure_private_dir(db_path.parent)
    repo = SQLiteConfigRepository(db_path, prepare_schema=False)
    incoming_merchants = load_merchant_rules(source / "merchants.toml")
    incoming_categories = load_categories(source / "categories.toml")

    # Seeding a database that has no runtime config yet destroys nothing, so it needs
    # neither a dry run nor a backup. Only a REPLACE of an existing config does.
    seeding_fresh = not db_path.exists() or not repo.has_runtime_config()
    if not seeding_fresh:
        _print_config_import_diff(repo, incoming_merchants, incoming_categories)

        if not execute:
            console.print(
                "\n[yellow]Dry run[/yellow] — no changes were made. "
                "Re-run with [bold]--execute[/bold] to apply "
                "(a pre-mutation backup is taken automatically)."
            )
            return

    if seeding_fresh:
        try:
            with live_mutation_boundary(db_path, operation="runtime_config"):
                writable_repo = SQLiteConfigRepository(db_path)
                print_config_validation(writable_repo.import_from_toml(source))
        except LiveMutationDuringOpenImportRefused as exc:
            console.print(f"[red]Refused:[/red] {exc}")
            raise typer.Exit(1) from exc
        console.print(f"\n[green]✓[/green] Imported runtime config into {db_path}")
        return

    try:
        result = RuntimeConfigMutationService(db_path).replace_and_apply(
            incoming_merchants,
            incoming_categories,
            source="cli_config_import",
        )
    except LiveMutationDuringOpenImportRefused as exc:
        console.print(f"[red]Refused:[/red] {exc}")
        raise typer.Exit(1) from exc
    if not result.ok:
        for issue in result.validation_issues:
            console.print(f"[red]Config issue:[/red] {issue}")
        console.print("[red]Aborting[/red] — the previous runtime config was restored.")
        raise typer.Exit(1)

    print_config_validation(repo.validate())
    console.print(f"\n[green]✓[/green] Imported runtime config into {db_path}")
    console.print(f"  • Transactions recategorized: {result.transactions_updated:,}")


def _print_config_import_diff(
    repo: SQLiteConfigRepository,
    incoming_merchants,
    incoming_categories,
) -> None:
    """Report what a replace would remove, before it happens."""
    current_merchants = repo.load_merchant_rules()
    current_categories = repo.load_categories()

    current_rule_names = {rule.merchant_norm.upper().strip() for rule in current_merchants.rules}
    incoming_rule_names = {rule.merchant_norm.upper().strip() for rule in incoming_merchants.rules}
    current_patterns = sum(len(rule.patterns) for rule in current_merchants.rules)
    incoming_patterns = sum(len(rule.patterns) for rule in incoming_merchants.rules)
    current_category_names = {category.name for category in current_categories.categories}
    incoming_category_names = {category.name for category in incoming_categories.categories}
    current_rule_exclusions = {
        rule.merchant_norm.upper().strip(): bool(rule.exclude_from_spending)
        for rule in current_merchants.rules
    }
    incoming_rule_exclusions = {
        rule.merchant_norm.upper().strip(): bool(rule.exclude_from_spending)
        for rule in incoming_merchants.rules
    }
    current_category_exclusions = {
        category.name: bool(category.exclude_from_spending)
        for category in current_categories.categories
    }
    incoming_category_exclusions = {
        category.name: bool(category.exclude_from_spending)
        for category in incoming_categories.categories
    }
    retained_rule_flips = sum(
        current_rule_exclusions[name] != incoming_rule_exclusions[name]
        for name in current_rule_names & incoming_rule_names
    )
    retained_category_flips = sum(
        current_category_exclusions[name] != incoming_category_exclusions[name]
        for name in current_category_names & incoming_category_names
    )

    removed_rules = sorted(current_rule_names - incoming_rule_names)
    removed_categories = sorted(current_category_names - incoming_category_names)

    console.print(f"Replacing the runtime config in {repo.db_path} with {'':s}the TOML seeds:")
    console.print(
        f"  • Merchant rules:    {len(current_merchants.rules):,} -> "
        f"{len(incoming_merchants.rules):,}"
    )
    console.print(f"  • Merchant patterns: {current_patterns:,} -> {incoming_patterns:,}")
    console.print(
        f"  • Categories:        {len(current_categories.categories):,} -> "
        f"{len(incoming_category_names):,}"
    )
    console.print(
        "  • Merchant-rule exclusions: "
        f"{sum(current_rule_exclusions.values()):,} -> "
        f"{sum(incoming_rule_exclusions.values()):,} "
        f"({retained_rule_flips:,} retained rule(s) flip)"
    )
    console.print(
        "  • Category exclusions: "
        f"{sum(current_category_exclusions.values()):,} -> "
        f"{sum(incoming_category_exclusions.values()):,} "
        f"({retained_category_flips:,} retained category(ies) flip)"
    )

    if removed_rules:
        console.print(f"\n[yellow]{len(removed_rules)} merchant rule(s) would be deleted:[/yellow]")
        for name in removed_rules[:10]:
            console.print(f"    - {name}")
        if len(removed_rules) > 10:
            console.print(f"    - … and {len(removed_rules) - 10} more")

    if removed_categories:
        console.print(
            f"\n[yellow]{len(removed_categories)} categor(ies) would be deleted:[/yellow]"
        )
        for name in removed_categories[:10]:
            console.print(f"    - {name}")
        if len(removed_categories) > 10:
            console.print(f"    - … and {len(removed_categories) - 10} more")

        orphaned = _orphaned_transaction_counts(repo.db_path, removed_categories)
        if orphaned:
            total = sum(orphaned.values())
            console.print(
                f"\n[red]{total:,} transaction(s) would be left referencing a category "
                "that no longer exists:[/red]"
            )
            for name, count in sorted(orphaned.items(), key=lambda item: -item[1])[:10]:
                console.print(f"    - {name}: {count:,}")


def _orphaned_transaction_counts(db_path: Path, removed: list[str]) -> dict[str, int]:
    import sqlite3
    from contextlib import closing

    if not removed:
        return {}
    placeholders = ", ".join("?" for _ in removed)
    with closing(sqlite3.connect(db_path)) as conn:
        rows = conn.execute(
            f"SELECT category, COUNT(*) FROM transactions WHERE category IN ({placeholders})"
            " GROUP BY category",
            removed,
        ).fetchall()
    return {str(row[0]): int(row[1]) for row in rows}


@config_app.command("export")
def config_export(
    db_path: Annotated[Path | None, typer.Option("--db", help="SQLite database path")] = None,
    dest: Annotated[Path | None, typer.Option("--dest", help="Export directory")] = None,
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="CLI defaults config file (overrides default locations)"),
    ] = None,
) -> None:
    """Export SQLite runtime config to TOML files."""
    dest = resolve_runtime_path(dest, "exported")
    db_path = resolve_db_path(db_path, config_file)
    dest = dest.expanduser().resolve()
    SQLiteConfigRepository(db_path).export_to_directory(dest)
    console.print(f"[green]✓[/green] Exported runtime config to {dest}")


@config_app.command("validate")
def config_validate(
    db_path: Annotated[Path | None, typer.Option("--db", help="SQLite database path")] = None,
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="CLI defaults config file (overrides default locations)"),
    ] = None,
) -> None:
    """Validate SQLite runtime merchant/category config."""
    db_path = resolve_db_path(db_path, config_file)
    result = SQLiteConfigRepository(db_path).validate()
    print_config_validation(result)
    if not result.ok:
        raise typer.Exit(1)


@config_app.command("ensure-account-salt")
def config_ensure_account_salt(
    db_path: Annotated[Path | None, typer.Option("--db", help="SQLite database path")] = None,
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="CLI defaults config file (overrides default locations)"),
    ] = None,
) -> None:
    """Seed the per-database salt behind every opaque account reference.

    The salt keys the HMAC behind the ``acct-…`` references used by exports and by
    read-only consumers. It used to be minted only as a side effect of running an
    export — a WRITE — so a read-only consumer could never obtain one and had to
    fall back to raw account slugs. This verb mints it deliberately, ahead of time.

    Idempotent: an existing salt is reported and left alone, because rotating it
    would silently change every reference already handed out. The salt itself is
    a local secret and is never printed.
    """
    from ledger.storage.database import init_database
    from ledger.storage.operations import ensure_account_ref_salt, read_account_ref_salt

    db_path = resolve_db_path(db_path, config_file)
    # ensure_account_ref_salt() creates config_metadata when it is missing, but the
    # read has to happen BEFORE the mint to report which of the two actually occurred.
    init_database(db_path)
    existing = read_account_ref_salt(db_path)
    ensure_account_ref_salt(db_path)

    if existing is None:
        console.print(f"[green]✓[/green] Minted an account-reference salt in {db_path}")
    else:
        console.print(f"[green]✓[/green] Account-reference salt already present in {db_path}")
    console.print("  • The salt is a local secret — it is never printed or exported.")


@rules_app.command("validate")
def rules_validate(
    db_path: Annotated[Path | None, typer.Option("--db", help="SQLite database path")] = None,
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="CLI defaults config file (overrides default locations)"),
    ] = None,
) -> None:
    """Validate runtime rules before applying them to historical rows."""
    db_path = resolve_db_path(db_path, config_file)
    result = SQLiteConfigRepository(db_path).validate()
    print_config_validation(result)
    if not result.ok:
        raise typer.Exit(1)


@rules_app.command("apply")
def rules_apply(
    db_path: Annotated[Path | None, typer.Option("--db", help="SQLite database path")] = None,
    execute: Annotated[
        bool,
        typer.Option(
            "--execute",
            help="Apply to the live DB (off by default; takes an automatic pre-mutation backup)",
        ),
    ] = False,
    config_file: Annotated[
        Path | None,
        typer.Option("--config", help="CLI defaults config file (overrides default locations)"),
    ] = None,
) -> None:
    """Apply runtime merchant profiles/categories to existing transactions.

    This rewrites the merchant and category of EVERY transaction row, so — like the
    other destructive commands (``data clean``, ``optimize categories``) — it is gated
    behind ``--execute`` and takes an automatic pre-mutation backup first.
    """
    from ledger.rules import RuleApplicationService
    from ledger.storage.operations import backup_database

    db_path = resolve_db_path(db_path, config_file)

    if not execute:
        console.print(
            "[yellow]Dry run[/yellow] — `rules apply` rewrites the merchant and category "
            "of EVERY transaction row. No changes were made."
        )
        console.print(
            "Re-run with [bold]--execute[/bold] to apply "
            "(a pre-mutation backup is taken automatically)."
        )
        return

    try:
        with live_mutation_boundary(db_path, operation="runtime_rules"):
            backup = backup_database(db_path, reason="pre-rules-apply")
            backup_path = backup.artifacts.get("backup")
            if not backup.ok:
                for err in backup.errors:
                    console.print(f"[red]Backup failed:[/red] {err}")
                console.print("[red]Aborting[/red] — refusing to mutate without a backup.")
                raise typer.Exit(1)
            console.print(f"[dim]Pre-mutation backup:[/dim] {backup_path}")

            with console.status("Applying runtime rules and refreshing analytics..."):
                result = RuleApplicationService(db_path).apply_all(source="cli")
    except LiveMutationDuringOpenImportRefused as exc:
        console.print(f"[red]Refused:[/red] {exc}")
        raise typer.Exit(1) from exc

    if not result.ok:
        for issue in result.validation_issues:
            console.print(f"[red]Rule issue:[/red] {issue}")
        console.print(f"[yellow]Restore the backup to roll back:[/yellow] {backup_path}")
        raise typer.Exit(1)

    console.print("[green]✓[/green] Applied runtime rules")
    console.print(f"  • Transactions scanned: {result.transactions_scanned:,}")
    console.print(f"  • Transactions updated: {result.transactions_updated:,}")
    console.print(f"  • Merchant profiles changed: {result.changed_merchants:,}")
    console.print(f"  • Categories changed: {result.changed_categories:,}")
    console.print(f"  • Analytics refreshed: {result.analytics_refreshed}")
