"""CLI coverage for SQLite runtime config commands."""

import contextlib
import sqlite3
import stat
from pathlib import Path

from typer.testing import CliRunner

from ledger.cli import app
from ledger.commands.adjustments import adjustments_app
from ledger.commands.build import register_build_commands
from ledger.commands.config import config_app, rules_app
from ledger.commands.import_pdf import import_pdf_app
from ledger.config.repository import SQLiteConfigRepository
from ledger.config.schemas import CategoriesConfig, CategoryRule, MerchantRule, MerchantRulesConfig
from ledger.imports.pdf.files import write_run_state
from ledger.imports.pdf.readiness import import_owned_fingerprint, import_owned_match
from ledger.storage.database import init_database


def write_configs(path: Path) -> None:
    path.mkdir()
    (path / "merchants.toml").write_text(
        """
version = "cli-test"

[[rule]]
merchant_norm = "ACME"
category = "Groceries"
patterns = ["ACME"]
""",
        encoding="utf-8",
    )
    (path / "categories.toml").write_text(
        """
version = "cli-test"
default_category = "Other"

[[category]]
category_norm = "Groceries"
merchants = ["ACME"]
""",
        encoding="utf-8",
    )


def test_config_import_validate_export_commands(tmp_path: Path):
    runner = CliRunner()
    source = tmp_path / "configs"
    dest = tmp_path / "exported"
    db_path = tmp_path / "ledger.db"
    write_configs(source)

    # Seeding a database with no runtime config destroys nothing, so it applies
    # directly; replacing an existing config is dry-run by default (see the
    # seventh-pass audit tests).
    result = runner.invoke(
        app,
        ["config", "import", "--db", str(db_path), "--source", str(source)],
    )
    assert result.exit_code == 0, result.output
    assert "Imported runtime config" in result.output

    result = runner.invoke(app, ["config", "validate", "--db", str(db_path)])
    assert result.exit_code == 0, result.output
    assert "Issues" in result.output

    result = runner.invoke(
        app,
        ["config", "export", "--db", str(db_path), "--dest", str(dest)],
    )
    assert result.exit_code == 0, result.output
    assert (dest / "merchants.toml").exists()
    assert (dest / "categories.toml").exists()


def test_cli_command_modules_are_registered():
    runner = CliRunner()

    for args, expected in [
        (["--help"], "Personal finance ledger"),
        (["import", "pdf", "--help"], "Stage, inspect, finalize"),
        (["rules", "--help"], "Validate and apply"),
        (["adjustments", "--help"], "true-cost"),
    ]:
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.output
        assert expected in result.output

    assert config_app.registered_commands
    assert rules_app.registered_commands
    assert adjustments_app.registered_commands
    assert import_pdf_app.registered_commands
    assert callable(register_build_commands)


def test_config_import_execute_refuses_while_target_has_a_promotable_run(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    source = tmp_path / "configs"
    db_path = tmp_path / "data" / "ledger.db"
    write_configs(source)

    seeded = runner.invoke(
        app,
        ["config", "import", "--db", str(db_path), "--source", str(source)],
    )
    assert seeded.exit_code == 0, seeded.output

    run_dir = tmp_path / "custom-runs" / "open-run"
    run_dir.mkdir(parents=True)
    write_run_state(
        run_dir,
        {
            "version": 1,
            "status": "needs_review",
            "target_db_path": str(db_path),
            "staging_db_path": str(run_dir / "staging.db"),
        },
    )
    before = import_owned_fingerprint(db_path)

    result = runner.invoke(
        app,
        [
            "config",
            "import",
            "--db",
            str(db_path),
            "--source",
            str(source),
            "--execute",
        ],
    )

    assert result.exit_code == 1, result.output
    normalized = " ".join(result.output.split())
    assert "import run is open" in normalized
    assert "finalize" in normalized and "cancel" in normalized
    assert import_owned_match(before, import_owned_fingerprint(db_path))


def test_fresh_config_seed_refuses_while_target_has_a_promotable_run(
    tmp_path: Path,
) -> None:
    source = tmp_path / "configs"
    db_path = tmp_path / "data" / "ledger.db"
    write_configs(source)
    db_path.parent.mkdir()
    init_database(db_path)
    run_dir = tmp_path / "custom-runs" / "open-run"
    run_dir.mkdir(parents=True)
    write_run_state(
        run_dir,
        {
            "version": 1,
            "status": "needs_review",
            "target_db_path": str(db_path),
            "staging_db_path": str(run_dir / "staging.db"),
        },
    )

    result = CliRunner().invoke(
        app,
        ["config", "import", "--db", str(db_path), "--source", str(source)],
    )

    assert result.exit_code == 1, result.output
    assert "import run is open" in " ".join(result.output.split())
    assert SQLiteConfigRepository(db_path).has_runtime_config() is False


def test_config_import_precheck_does_not_prepare_a_pre_migration_open_target(
    tmp_path: Path,
) -> None:
    source = tmp_path / "configs"
    db_path = tmp_path / "data" / "ledger.db"
    write_configs(source)
    db_path.parent.mkdir()
    with contextlib.closing(sqlite3.connect(db_path)) as connection, connection:
        connection.execute("create table sentinel (value text)")
    db_path.chmod(0o644)
    before = db_path.read_bytes()
    run_dir = tmp_path / "custom-runs" / "open-run"
    run_dir.mkdir(parents=True, mode=0o700)
    write_run_state(
        run_dir,
        {
            "version": 1,
            "status": "needs_review",
            "target_db_path": str(db_path),
            "staging_db_path": str(run_dir / "staging.db"),
        },
    )

    result = CliRunner().invoke(
        app,
        ["config", "import", "--db", str(db_path), "--source", str(source)],
    )

    assert result.exit_code == 1, result.output
    assert "import run is open" in " ".join(result.output.split())
    assert db_path.read_bytes() == before
    assert stat.S_IMODE(db_path.stat().st_mode) == 0o644


def test_rules_apply_execute_refuses_before_backup_while_run_can_promote(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    source = tmp_path / "configs"
    db_path = tmp_path / "data" / "ledger.db"
    write_configs(source)
    seeded = runner.invoke(
        app,
        ["config", "import", "--db", str(db_path), "--source", str(source)],
    )
    assert seeded.exit_code == 0, seeded.output
    run_dir = tmp_path / "custom-runs" / "open-run"
    run_dir.mkdir(parents=True)
    write_run_state(
        run_dir,
        {
            "version": 1,
            "status": "needs_review",
            "target_db_path": str(db_path),
            "staging_db_path": str(run_dir / "staging.db"),
        },
    )

    result = runner.invoke(
        app,
        ["rules", "apply", "--db", str(db_path), "--execute"],
    )

    assert result.exit_code == 1, result.output
    assert "import run is open" in " ".join(result.output.split())
    assert not list(db_path.parent.glob("*.backup-*"))


def test_config_import_dry_run_reports_merchant_and_category_exclusion_flips(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ledger.db"
    SQLiteConfigRepository(db_path).replace_from_configs(
        MerchantRulesConfig(
            version="current",
            rules=[
                MerchantRule(
                    merchant_norm="ACME",
                    patterns=["ACME"],
                    category="Groceries",
                    exclude_from_spending=True,
                )
            ],
        ),
        CategoriesConfig(
            version="current",
            categories=[
                CategoryRule(name="Groceries", exclude_from_spending=True),
            ],
        ),
    )
    source = tmp_path / "configs"
    write_configs(source)

    result = CliRunner().invoke(
        app,
        ["config", "import", "--db", str(db_path), "--source", str(source)],
    )

    normalized = " ".join(result.output.split())
    assert result.exit_code == 0, result.output
    assert "Merchant-rule exclusions: 1 -> 0 (1 retained rule(s) flip)" in normalized
    assert "Category exclusions: 1 -> 0 (1 retained category(ies) flip)" in normalized
