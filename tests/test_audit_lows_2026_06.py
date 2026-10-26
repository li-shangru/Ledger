"""Tests for the four 2026-06-21 audit-low fixes.

Fix 1 (freeze-check): verified by bash -n in CI — no Python test needed.
Fix 2 (DB perms):   freshly init'd DB is mode 0o600 on POSIX;
                    data subdirs created by DataCleanupService are mode 0o700.
Fix 3 (shadowing):  no public method name appears on more than one query module.
Fix 4 (_base_run_state): pure refactor — covered by existing test_pdf_import_safety tests.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

# =============================================================================
# Fix 2 — DB file chmod 0o600 + data-dir chmod 0o700
# =============================================================================


@pytest.mark.skipif(os.name != "posix", reason="chmod semantics are POSIX-only")
class TestDbInitPermissions:
    """init_database must leave the DB file owner-readable/writable only (0o600)."""

    def test_init_database_mode_is_0o600(self, tmp_path: Path) -> None:
        from ledger.storage.database import init_database

        db_path = tmp_path / "fresh.db"
        init_database(db_path)

        mode = stat.S_IMODE(db_path.stat().st_mode)
        assert mode == 0o600, (
            f"Expected DB file mode 0o600, got 0o{mode:o}. "
            "A group/world-readable DB leaks financial data on multi-user hosts."
        )

    def test_clone_database_copy_mode_is_0o600(self, tmp_path: Path) -> None:
        from ledger.imports.pdf.files import clone_database
        from ledger.storage.database import init_database

        source = tmp_path / "source.db"
        init_database(source)
        target = tmp_path / "clone.db"
        clone_database(source, target)

        mode = stat.S_IMODE(target.stat().st_mode)
        assert mode == 0o600, f"Cloned DB must be 0o600; got 0o{mode:o}."

    def test_clone_database_fresh_mode_is_0o600(self, tmp_path: Path) -> None:
        """clone_database with no source (fresh init path) must also be 0o600."""
        from ledger.imports.pdf.files import clone_database

        target = tmp_path / "fresh-clone.db"
        clone_database(None, target)

        mode = stat.S_IMODE(target.stat().st_mode)
        assert mode == 0o600, f"Fresh-clone DB must be 0o600; got 0o{mode:o}."

    def test_db_is_still_readable_and_writable_after_chmod(self, tmp_path: Path) -> None:
        """chmod 0o600 keeps owner rw — the test process (= owner) must still use the DB."""
        from ledger.storage.database import ConfigMetadataDB, init_database, session_scope

        db_path = tmp_path / "rw_check.db"
        init_database(db_path)

        # Must be able to write and read back without PermissionError
        with session_scope(db_path) as session:
            session.add(ConfigMetadataDB(key="test_key", value="test_value"))

        with session_scope(db_path) as session:
            row = session.query(ConfigMetadataDB).filter_by(key="test_key").first()
            assert row is not None and row.value == "test_value"


@pytest.mark.skipif(os.name != "posix", reason="chmod semantics are POSIX-only")
class TestDataDirPermissions:
    """DataCleanupService._ensure_directories must create data subdirs mode 0o700."""

    def test_ensure_directories_mode_is_0o700(self, tmp_path: Path) -> None:
        from ledger.maintenance.cleanup import DataCleanupService
        from ledger.storage.database import init_database

        db_path = tmp_path / "ledger.db"
        init_database(db_path)

        svc = DataCleanupService(db_path=db_path, data_dir=tmp_path)
        # Call the private method directly so we don't need a full backup setup.
        svc._ensure_directories()

        for name in ["inbox", "import-runs", "processed", "db-backups"]:
            d = tmp_path / name
            assert d.exists(), f"Expected directory {name!r} to be created"
            mode = stat.S_IMODE(d.stat().st_mode)
            assert mode == 0o700, f"Data subdir {name!r} must be mode 0o700; got 0o{mode:o}."


# =============================================================================
# Fix 3 — query-facade shadowing-invariant test
# =============================================================================


class TestQueryFacadeShadowingInvariant:
    """No public method name may appear on more than one of the 8 query modules.

    If a name were duplicated, multiple-inheritance resolution would depend on
    MRO order and callers could silently hit the wrong implementation.
    """

    def test_no_public_name_on_more_than_one_module(self) -> None:
        from ledger.query.accounts import AccountQueries
        from ledger.query.aggregates import AggregateQueries
        from ledger.query.exports import ExportQueries
        from ledger.query.insights import InsightQueries
        from ledger.query.quality import QualityQueries
        from ledger.query.reports import ReportQueries
        from ledger.query.transactions import TransactionQueries
        from ledger.query.transfers import TransferQueries

        module_classes = [
            ("transactions", TransactionQueries),
            ("aggregates", AggregateQueries),
            ("accounts", AccountQueries),
            ("insights", InsightQueries),
            ("quality", QualityQueries),
            ("transfers", TransferQueries),
            ("exports", ExportQueries),
            ("reports", ReportQueries),
        ]

        # Collect public callable names per module (skip dunder + private)
        name_to_modules: dict[str, list[str]] = {}
        for module_name, cls in module_classes:
            for attr in dir(cls):
                if attr.startswith("_"):
                    continue
                if not callable(getattr(cls, attr, None)):
                    continue
                name_to_modules.setdefault(attr, []).append(module_name)

        duplicates = {
            name: modules for name, modules in name_to_modules.items() if len(modules) > 1
        }

        assert not duplicates, (
            "The following public method names appear on more than one query module "
            "(multiple-inheritance resolution would be MRO-dependent):\n"
            + "\n".join(
                f"  {name!r}: {', '.join(modules)}" for name, modules in sorted(duplicates.items())
            )
        )
