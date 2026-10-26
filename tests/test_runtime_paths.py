"""Public runtime isolation, using only independently created temporary paths."""

from pathlib import Path

import pytest

from ledger.runtime import (
    RuntimePathError,
    default_database_path,
    require_external_runtime_path,
    runtime_home,
    runtime_path,
)


@pytest.fixture
def isolated_runtime(tmp_path, monkeypatch):
    root = tmp_path.resolve() / "private-runtime"
    monkeypatch.setenv("LEDGER_HOME", str(root))
    monkeypatch.delenv("LEDGER_DB", raising=False)
    return root


def test_external_home_is_lazy_and_independent_of_working_directory(
    isolated_runtime, tmp_path, monkeypatch
):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    monkeypatch.chdir(checkout)
    assert runtime_home() == isolated_runtime
    assert runtime_path("import-runs") == isolated_runtime / "import-runs"
    assert default_database_path() == isolated_runtime / "ledger.db"
    assert not isolated_runtime.exists()
    assert sorted(p.name for p in checkout.iterdir()) == [".git"]


def test_home_is_resolved_per_call_and_requires_an_absolute_override(tmp_path, monkeypatch):
    for name in ("first", "second"):
        root = tmp_path.resolve() / name
        monkeypatch.setenv("LEDGER_HOME", str(root))
        assert runtime_home() == root
    monkeypatch.setenv("LEDGER_HOME", "relative-runtime")
    with pytest.raises(RuntimePathError, match="absolute"):
        runtime_home()


@pytest.mark.parametrize("kind", ["git-directory", "worktree-file", "source-archive"])
def test_repository_destinations_are_rejected_before_creation(tmp_path, kind):
    root = tmp_path.resolve() / "checkout"
    root.mkdir()
    if kind == "git-directory":
        (root / ".git").mkdir()
    elif kind == "worktree-file":
        (root / ".git").write_text("gitdir: /unread/example\n")
    else:
        (root / "pyproject.toml").write_text('[project]\nname="ledger"\n')
        package = root / "src" / "ledger"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
    target = root / "new" / "ledger.db"
    with pytest.raises(RuntimePathError, match="source_repository"):
        require_external_runtime_path(target)
    assert not target.parent.exists()


def test_repository_alias_and_relative_paths_cannot_bypass_boundary(tmp_path, monkeypatch):
    checkout = tmp_path.resolve() / "checkout"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(checkout, target_is_directory=True)
    for path in (alias / "ledger.db", checkout / "nested" / ".." / "ledger.db"):
        with pytest.raises(RuntimePathError, match="source_repository"):
            require_external_runtime_path(path)
    monkeypatch.chdir(checkout)
    with pytest.raises(RuntimePathError, match="source_repository"):
        require_external_runtime_path(Path("ledger.db"))
    assert not (checkout / "ledger.db").exists()


def test_runtime_children_cannot_escape_the_home(isolated_runtime):
    for child in ("../other", "/absolute", "a/../../other"):
        with pytest.raises(RuntimePathError):
            runtime_path(child)


def test_explicit_database_override_is_checked_and_does_not_create_files(
    isolated_runtime, tmp_path, monkeypatch
):
    separate = tmp_path.resolve() / "second-runtime" / "facts.db"
    monkeypatch.setenv("LEDGER_DB", str(separate))
    assert default_database_path() == separate
    assert not separate.parent.exists()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    monkeypatch.setenv("LEDGER_DB", str(repo / "ledger.db"))
    with pytest.raises(RuntimePathError, match="source_repository"):
        default_database_path()


def test_financial_database_and_artifact_writers_refuse_repository_paths(tmp_path):
    from ledger.proposals.database import init_proposal_database
    from ledger.storage.database import get_engine, init_database
    from ledger.utils.private_files import ensure_private_dir, write_private_text_atomic

    repo = tmp_path.resolve() / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    target = repo / "ledger.db"
    actions = [
        lambda: get_engine(target),
        lambda: init_database(target),
        lambda: init_proposal_database(repo / "proposals.db"),
        lambda: ensure_private_dir(repo / "exports"),
        lambda: write_private_text_atomic(repo / "report.txt", "fictional"),
    ]
    for action in actions:
        with pytest.raises((RuntimePathError, OSError), match="source_repository"):
            action()
        assert sorted(p.name for p in repo.iterdir()) == [".git"]


def test_read_only_database_rejects_repository_location_without_reading_it(tmp_path):
    from ledger.storage.read_only import ReadOnlyDatabase, ReadOnlyUnavailableError

    repo = tmp_path.resolve() / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    target = repo / "ledger.db"
    target.write_bytes(b"independently authored sentinel")
    before = (target.read_bytes(), target.stat().st_mtime_ns)
    with pytest.raises(ReadOnlyUnavailableError):
        ReadOnlyDatabase(target)
    assert (target.read_bytes(), target.stat().st_mtime_ns) == before
    assert sorted(p.name for p in repo.iterdir()) == [".git", "ledger.db"]


def test_ui_and_cli_use_the_same_external_default(isolated_runtime):
    from ledger.commands.common import resolve_db_path
    from ledger.ui.context import resolve_default_db

    assert resolve_db_path(None, must_exist=False) == isolated_runtime / "ledger.db"
    assert resolve_default_db() == isolated_runtime / "ledger.db"
    assert not isolated_runtime.exists()


def test_current_directory_defaults_are_not_loaded(isolated_runtime, tmp_path, monkeypatch):
    from ledger.config.loader import load_cli_defaults

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".ledgerrc").write_text('[defaults]\ndb="/unread/legacy.db"\n')
    monkeypatch.chdir(checkout)
    assert load_cli_defaults().db is None
    assert default_database_path() == isolated_runtime / "ledger.db"
    isolated_runtime.mkdir()
    chosen = tmp_path.resolve() / "custom" / "chosen.db"
    (isolated_runtime / "defaults.toml").write_text(f'[defaults]\ndb="{chosen}"\n')
    assert default_database_path() == chosen
    assert not chosen.exists()


def test_packaged_seeds_win_over_working_directory(tmp_path, monkeypatch):
    from ledger.config.loader import load_categories, load_merchant_rules
    from ledger.runtime import seed_config_path

    monkeypatch.chdir(tmp_path)
    merchants = load_merchant_rules(seed_config_path("merchants.toml"))
    categories = load_categories(seed_config_path("categories.toml"))
    assert len(merchants.rules) == 8
    assert len(categories.categories) == 15
    assert not (tmp_path / "configs").exists()


def test_cli_creates_external_database_without_source_outputs(
    isolated_runtime, tmp_path, monkeypatch
):
    from typer.testing import CliRunner

    from ledger.cli import app

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    monkeypatch.chdir(checkout)
    runner = CliRunner()
    result = runner.invoke(app, ["config", "import"])
    assert result.exit_code == 0, result.output
    assert (isolated_runtime / "ledger.db").is_file()
    assert sorted(p.name for p in checkout.iterdir()) == [".git"]
    rejected = runner.invoke(app, ["config", "import", "--db", str(checkout / "blocked.db")])
    assert rejected.exit_code != 0
    assert not (checkout / "blocked.db").exists()


def test_audit_default_and_override_stay_external(isolated_runtime, tmp_path, monkeypatch):
    from ledger.evidence.service import default_audit_runs_dir

    assert default_audit_runs_dir() == isolated_runtime / "audit-runs"
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    monkeypatch.setenv("LEDGER_AUDIT_RUNS_DIR", str(checkout / "audit"))
    with pytest.raises(RuntimePathError):
        default_audit_runs_dir()
    assert not (checkout / "audit").exists()


@pytest.mark.parametrize("destination", ["out_dir", "out_alias", "db_alias"])
@pytest.mark.parametrize("existing", [False, True])
def test_build_refuses_source_paths_before_mutating_external_state(
    tmp_path, monkeypatch, temp_csv_dir, destination, existing
):
    from typer.testing import CliRunner

    from ledger.cli import app
    from ledger.runtime import seed_config_path

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    external = tmp_path / "external"
    external.mkdir()
    alias = checkout / "alias"
    alias.symlink_to(external, target_is_directory=True)
    output = (
        checkout / "reports"
        if destination == "out_dir"
        else (alias / "reports" if destination == "out_alias" else external / "reports")
    )
    database = alias / "ledger.db" if destination == "db_alias" else external / "ledger.db"
    runner = CliRunner()
    if existing:
        seeded = runner.invoke(app, ["config", "import", "--db", str(external / "ledger.db")])
        assert seeded.exit_code == 0, seeded.output
        from ledger.storage.database import clear_engine_cache

        clear_engine_cache()
    before = {p.name: p.read_bytes() for p in external.iterdir() if p.is_file()}
    arguments = [
        "build",
        "--src",
        str(temp_csv_dir),
        "--out-dir",
        str(output),
        "--db",
        str(database),
        "--formats",
        "csv",
    ]
    for flag, name in (
        ("--ledger-config", "ledger"),
        ("--merchant-rules", "merchants"),
        ("--categories", "categories"),
    ):
        arguments.extend([flag, str(seed_config_path(name + ".toml"))])
    result = CliRunner().invoke(app, arguments)
    assert result.exit_code != 0
    assert "runtime_in_source_repository" in str(result.exception)
    assert {p.name: p.read_bytes() for p in external.iterdir() if p.is_file()} == before
    assert not (external / "reports").exists()
    assert not (checkout / "reports").exists()


@pytest.mark.parametrize(
    "invalid", ["manifest", "manifest-direct", "csv-root", "processed-dir", "pdf-dir"]
)
def test_converter_preflights_all_runtime_paths_before_processing(tmp_path, monkeypatch, invalid):
    import sys

    from ledger.imports.pdf import statement_pdf_to_csv as converter

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    (checkout / ".git").mkdir()
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    pdf = inbox / "fictional.pdf"
    pdf.write_bytes(b"%PDF-synthetic-preflight")
    paths = {
        "pdf-dir": inbox,
        "csv-root": tmp_path / "csv",
        "processed-dir": tmp_path / "processed",
        "manifest": tmp_path / "manifest.json",
    }
    alias = checkout / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)
    if invalid == "manifest-direct":
        paths["manifest"] = checkout / "manifest.json"
    else:
        paths[invalid] = alias / paths[invalid].name
    calls = []

    def extract(path):
        calls.append(path)
        return "FICTIONAL PATH PREFLIGHT SENTINEL"

    monkeypatch.setattr(converter, "run_pdftotext", extract)
    monkeypatch.setattr(
        sys,
        "argv",
        ["converter", *[arg for key, path in paths.items() for arg in ("--" + key, str(path))]],
    )
    with pytest.raises((RuntimePathError, OSError), match="source_repository"):
        converter.main()
    assert calls == []
    assert pdf.read_bytes() == b"%PDF-synthetic-preflight"
    assert not (tmp_path / "csv").exists()
    assert not (tmp_path / "processed").exists()
    assert not (tmp_path / "manifest.json").exists()
