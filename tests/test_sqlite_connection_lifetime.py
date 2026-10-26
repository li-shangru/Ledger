from __future__ import annotations

import ast
import gc
import warnings
from pathlib import Path

from ledger.investments.workflow import _validate_staging_database
from ledger.maintenance.rebuild_guard import check_rebuild_safe
from ledger.repair.statements import (
    carry_forward_import_review,
    carry_forward_investments,
    carry_forward_operation_audit,
)
from ledger.storage.database import init_database
from ledger.storage.migrations import run_migrations


def _is_direct_sqlite_connect(expression: ast.expr) -> bool:
    return (
        isinstance(expression, ast.Call)
        and isinstance(expression.func, ast.Attribute)
        and isinstance(expression.func.value, ast.Name)
        and expression.func.value.id == "sqlite3"
        and expression.func.attr == "connect"
    )


def test_all_sqlite_contexts_also_close_connections() -> None:
    """A Connection context commits or rolls back, but does not close the handle."""
    repo_root = Path(__file__).parents[1]
    unsafe: list[str] = []

    for source_root in (repo_root / "src" / "ledger", repo_root / "tests"):
        for path in sorted(source_root.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.With):
                    continue
                for item in node.items:
                    if _is_direct_sqlite_connect(item.context_expr):
                        unsafe.append(f"{path.relative_to(repo_root)}:{node.lineno}")

    assert unsafe == [], (
        "sqlite3.Connection.__exit__ does not close the connection; wrap every direct "
        f"context in contextlib.closing (and a nested transaction context for writes): {unsafe}"
    )


def test_repair_and_investment_lifetimes_make_resource_warnings_fail(tmp_path: Path) -> None:
    live_db = tmp_path / "live.db"
    candidate_db = tmp_path / "candidate.db"
    init_database(live_db)
    init_database(candidate_db)
    run_migrations(candidate_db)
    gc.collect()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        assert carry_forward_import_review(live_db, candidate_db) == 0
        assert carry_forward_operation_audit(live_db, candidate_db) == 0
        assert all(
            count == 0 for count in carry_forward_investments(live_db, candidate_db).values()
        )
        _validate_staging_database(candidate_db, expected_statements=0)
        assert check_rebuild_safe(candidate_db).ok is False
        gc.collect()

    resource_warnings = [str(item.message) for item in caught if item.category is ResourceWarning]
    assert resource_warnings == []
