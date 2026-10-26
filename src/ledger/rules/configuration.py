"""Atomic runtime configuration replacement and historical rule application."""

from __future__ import annotations

from pathlib import Path

from ledger.config.repository import SQLiteConfigRepository, _validate_configs_in_memory
from ledger.config.schemas import CategoriesConfig, MerchantRulesConfig
from ledger.imports.pdf.readiness import live_mutation_boundary
from ledger.rules.service import RuleApplicationResult, RuleApplicationService
from ledger.storage.migrations import invalidate_db_caches
from ledger.storage.operations import backup_database
from ledger.storage.snapshots import restore_database


def _failure_result(
    *,
    status: str,
    source: str,
    issues: list[str],
) -> RuleApplicationResult:
    return RuleApplicationResult(
        status=status,
        source=source,
        transactions_scanned=0,
        transactions_updated=0,
        changed_merchants=0,
        changed_categories=0,
        analytics_refreshed=False,
        validation_issues=issues,
        details={"validation_issues": issues},
    )


class RuntimeConfigMutationService:
    """Replace runtime config and apply it as one recoverable workflow."""

    def __init__(self, db_path: Path):
        self.db_path = db_path

    def replace_and_apply(
        self,
        merchants: MerchantRulesConfig,
        categories: CategoriesConfig,
        *,
        source: str = "manual",
        refresh_analytics: bool = True,
    ) -> RuleApplicationResult:
        """Validate, back up, replace, validate, and apply; restore on failure."""
        candidate_issues = _validate_configs_in_memory(merchants, categories)
        if candidate_issues:
            return _failure_result(
                status="validation_failed",
                source=source,
                issues=candidate_issues,
            )

        with live_mutation_boundary(self.db_path, operation="runtime_config"):
            backup = backup_database(self.db_path, reason="pre-runtime-config")
            backup_artifact = backup.artifacts.get("backup")
            if not backup.ok or backup_artifact is None:
                issues = list(backup.errors) or ["Runtime configuration backup did not complete."]
                return _failure_result(status="backup_failed", source=source, issues=issues)
            backup_path = Path(str(backup_artifact))

            repo = SQLiteConfigRepository(self.db_path)
            replaced = False
            try:
                repo.replace_from_configs(merchants, categories)
                replaced = True

                persisted = repo.validate()
                if not persisted.ok:
                    result = _failure_result(
                        status="validation_failed",
                        source=source,
                        issues=persisted.issues,
                    )
                    self._restore(backup_path)
                    return result

                result = RuleApplicationService(self.db_path).apply_all(
                    source=source,
                    refresh_analytics=refresh_analytics,
                )
                if not result.ok:
                    self._restore(backup_path)
                return result
            except Exception:
                if replaced:
                    self._restore(backup_path)
                raise

    def _restore(self, backup_path: Path) -> None:
        restore_database(backup_path, self.db_path)
        invalidate_db_caches()
