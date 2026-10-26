import csv
import sqlite3
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from ledger.config.repository import SQLiteConfigRepository
from ledger.config.schemas import CategoriesConfig, CategoryRule, MerchantRule, MerchantRulesConfig
from ledger.core import Transaction
from ledger.imports.pdf.statement_pdf_to_csv import ExtractedCsv
from ledger.optimize.categories import (
    CategoryOptimizationOptions,
    CategoryOptimizer,
    CategoryRecommendation,
    analyze_merchants,
    category_rows,
    merge_merchant_rules,
    optimize_taxonomy,
)
from ledger.processing import ProcessingResult
from ledger.repair.statements import (
    StatementRepairOptions,
    StatementRepairService,
    build_move_plan,
    carry_forward_overrides,
    validate_move_plan,
)
from ledger.rules import RuleApplicationService
from ledger.storage.database import (
    DBOperationAuditDB,
    TransactionDB,
    init_database,
    session_scope,
)
from ledger.storage.writer import DatabaseWriter


def _override_txn(
    txn_id: str,
    *,
    source_row: int,
    source_path: str = "2025-01/statement.csv",
    institution: str = "harbor",
    account: str = "harbor-chequing",
    posting_date: date = date(2025, 1, 15),
    currency: str = "CAD",
    amount: Decimal = Decimal("-25.00"),
    description: str | None = None,
    merchant: str = "PARSED MERCHANT",
    category: str = "Parsed Category",
) -> Transaction:
    return Transaction(
        txn_id=txn_id,
        txn_fingerprint=f"fp-{txn_id}",
        posting_date=posting_date,
        institution=institution,
        account=account,
        account_type="Chequing",
        flow="expense",
        cashflow_type="expense",
        include_in_cashflow=True,
        currency=currency,
        amount_raw=abs(amount),
        amount_signed=amount,
        description=description or f"DESCRIPTION {txn_id}",
        merchant=merchant,
        category=category,
        exclude_from_spending=False,
        source_file="statement.csv",
        source_path=source_path,
        source_period="2025-01",
        source_row=source_row,
        imported_at=datetime.now(UTC).replace(tzinfo=None),
        parser_version="test",
    )


def _write_override_rows(db_path: Path, transactions: list[Transaction]) -> None:
    DatabaseWriter(db_path).write_result(
        ProcessingResult(
            transactions=transactions,
            transfers=[],
            exceptions=[],
            total_scanned=len(transactions),
        ),
        db_path.parent,
        "test",
    )


def test_statement_repair_inventory_csv_neutralizes_every_string_cell(tmp_path: Path) -> None:
    from ledger.repair.statements import write_csv as write_repair_csv

    path = tmp_path / "statement_inventory.csv"
    write_repair_csv(
        path,
        [
            {
                "account": "=ACCOUNT",
                "source_file": "+SOURCE.pdf",
                "reason": "\treason",
                "amount": "-50.00",
            }
        ],
    )

    with path.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))

    assert row["account"].startswith("'=")
    assert row["source_file"].startswith("'+")
    assert row["reason"].startswith("'\t")
    assert row["amount"] == "-50.00"


def test_category_optimization_artifact_csv_neutralizes_every_string_cell(
    tmp_path: Path,
) -> None:
    from ledger.optimize.categories import write_csv as write_optimization_csv

    path = tmp_path / "merchant_recommendations.csv"
    write_optimization_csv(
        path,
        [
            {
                "merchant": "@MERCHANT",
                "reason": "\rreason",
                "new_value": "-formula",
                "amount": "+50.00",
            }
        ],
    )

    with path.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))

    assert row["merchant"].startswith("'@")
    assert row["reason"].startswith("'\r")
    assert row["new_value"].startswith("'-")
    assert row["amount"] == "+50.00"


def test_statement_repair_changed_ids_preserve_manual_and_rule_overrides(tmp_path: Path) -> None:
    live_db = tmp_path / "live.db"
    candidate_db = tmp_path / "candidate.db"
    init_database(live_db)
    init_database(candidate_db)

    _write_override_rows(
        live_db,
        [
            _override_txn(
                "old-manual",
                source_row=1,
                source_path="2025-01/./statement.csv",
                currency="cad",
            ),
            _override_txn("old-rule", source_row=2),
        ],
    )
    with session_scope(live_db) as session:
        manual = session.get(TransactionDB, "old-manual")
        assert manual is not None
        manual.merchant = "MANUAL MERCHANT"
        manual.merchant_confidence = "manual"
        manual.category = "Manual Category"
        manual.category_confidence = "manual"
        manual.exclude_from_spending = True

        rule = session.get(TransactionDB, "old-rule")
        assert rule is not None
        rule.merchant = "RULE MERCHANT"
        rule.merchant_confidence = "rule"
        rule.category = "Rule Category"
        rule.category_confidence = "rule"

    _write_override_rows(
        candidate_db,
        [
            _override_txn(
                "new-manual",
                source_row=1,
                account="renamed-account",
                description="DESCRIPTION CHANGED",
                merchant="NEW PARSED MERCHANT",
                category="New Parsed Category",
            ),
            _override_txn(
                "new-rule",
                source_row=2,
                account="renamed-account",
                description="RULE DESCRIPTION CHANGED",
            ),
        ],
    )

    assert carry_forward_overrides(live_db, candidate_db) == 2

    with session_scope(candidate_db) as session:
        manual = session.get(TransactionDB, "new-manual")
        assert manual is not None
        assert (manual.merchant, manual.merchant_confidence) == ("MANUAL MERCHANT", "manual")
        assert (manual.category, manual.category_confidence) == ("Manual Category", "manual")
        assert manual.exclude_from_spending is True

        rule = session.get(TransactionDB, "new-rule")
        assert rule is not None
        assert (rule.merchant, rule.merchant_confidence) == ("RULE MERCHANT", "rule")
        assert (rule.category, rule.category_confidence) == ("Rule Category", "rule")


def test_statement_repair_manual_mismatch_rolls_back_all_overrides(tmp_path: Path) -> None:
    live_db = tmp_path / "live.db"
    candidate_db = tmp_path / "candidate.db"
    init_database(live_db)
    init_database(candidate_db)

    _write_override_rows(
        live_db,
        [
            _override_txn("old-safe", source_row=1),
            _override_txn("old-mismatch", source_row=2),
        ],
    )
    with session_scope(live_db) as session:
        for txn_id, category in (
            ("old-safe", "Safe Manual Category"),
            ("old-mismatch", "Mismatch Manual Category"),
        ):
            row = session.get(TransactionDB, txn_id)
            assert row is not None
            row.category = category
            row.category_confidence = "manual"

    _write_override_rows(
        candidate_db,
        [
            _override_txn("new-safe", source_row=1),
            _override_txn("new-mismatch", source_row=2, amount=Decimal("-26.00")),
        ],
    )

    with pytest.raises(RuntimeError, match="MANUAL"):
        carry_forward_overrides(live_db, candidate_db)

    with session_scope(candidate_db) as session:
        safe = session.get(TransactionDB, "new-safe")
        mismatch = session.get(TransactionDB, "new-mismatch")
        assert safe is not None and mismatch is not None
        assert (safe.category, safe.category_confidence) == ("Parsed Category", "low")
        assert (mismatch.category, mismatch.category_confidence) == ("Parsed Category", "low")


def test_statement_repair_manual_does_not_fallback_onto_unchanged_non_durable_row(
    tmp_path: Path,
) -> None:
    live_db = tmp_path / "live.db"
    candidate_db = tmp_path / "candidate.db"
    init_database(live_db)
    init_database(candidate_db)

    _write_override_rows(
        live_db,
        [
            _override_txn(
                "old-manual",
                source_row=1,
                description="MANUAL SOURCE ORIGINAL",
            ),
            _override_txn(
                "unchanged-neighbor",
                source_row=1,
                description="NEIGHBOR ORIGINAL",
                category="Neighbor Original",
            ),
        ],
    )
    with session_scope(live_db) as session:
        manual = session.get(TransactionDB, "old-manual")
        assert manual is not None
        manual.category = "Manual Category"
        manual.category_confidence = "manual"

    _write_override_rows(
        candidate_db,
        [
            _override_txn(
                "unchanged-neighbor",
                source_row=1,
                description="NEIGHBOR REPARSED",
                category="Neighbor Candidate",
            )
        ],
    )

    with pytest.raises(RuntimeError, match="MANUAL"):
        carry_forward_overrides(live_db, candidate_db)

    with session_scope(candidate_db) as session:
        neighbor = session.get(TransactionDB, "unchanged-neighbor")
        assert neighbor is not None
        assert (neighbor.description, neighbor.category, neighbor.category_confidence) == (
            "NEIGHBOR REPARSED",
            "Neighbor Candidate",
            "low",
        )


def test_move_plan_rejects_conflicting_processed_target(tmp_path: Path):
    source = tmp_path / "source.pdf"
    target = tmp_path / "processed.pdf"
    source.write_text("one", encoding="utf-8")
    target.write_text("two", encoding="utf-8")
    result = ExtractedCsv(
        pdf=source,
        csv_path=None,
        processed_pdf=target,
        statement_type="harbor_deposit",
        transaction_count=0,
    )

    plan = build_move_plan([result], tmp_path)

    try:
        validate_move_plan(plan)
    except RuntimeError as exc:
        assert "different hash" in str(exc)
    else:
        raise AssertionError("Expected conflicting target to be rejected")


def test_optimizer_taxonomy_maps_existing_categories():
    categories = CategoriesConfig(
        categories=[
            CategoryRule(name="Housing", merchants=[]),
            CategoryRule(name="Software Tools", merchants=[]),
            CategoryRule(name="Transfers (Excluded)", merchants=[], exclude_from_spending=True),
        ]
    )

    recommendations, optimized = optimize_taxonomy(categories)

    assert (
        CategoryRecommendation(
            old_name="Housing",
            new_name="Housing",
            parent="Home & Fixed Costs",
            old_exclude_from_spending=False,
            new_exclude_from_spending=False,
        )
        in recommendations
    )
    by_name = {category.name: category for category in optimized.categories}
    assert by_name["Home & Fixed Costs"].parent is None
    assert by_name["Software Tools"].parent == "Digital & Subscriptions"
    assert by_name["Transfers"].exclude_from_spending is True


def test_optimize_taxonomy_preserves_spend_semantics_no_silent_flip():
    # F10: --execute-safe must never silently change what counts as spending.
    # The user COUNTS Investing/Fees/Taxes/Cash Withdrawals (exclude_from_spending=False)
    # and EXCLUDES Transfers. Optimization is taxonomy-only: it must preserve every
    # existing category's flag verbatim -- Investing in particular must NOT flip to
    # excluded just because it appears in EXCLUDED_CATEGORIES.
    categories = CategoriesConfig(
        categories=[
            CategoryRule(name="Investing", merchants=[], exclude_from_spending=False),
            CategoryRule(name="Fees", merchants=[], exclude_from_spending=False),
            CategoryRule(name="Taxes", merchants=[], exclude_from_spending=False),
            CategoryRule(name="Cash Withdrawals", merchants=[], exclude_from_spending=False),
            CategoryRule(name="Transfers (Excluded)", merchants=[], exclude_from_spending=True),
        ]
    )

    _recommendations, optimized = optimize_taxonomy(categories)

    by_name = {category.name: category for category in optimized.categories}
    assert by_name["Investing"].exclude_from_spending is False
    assert by_name["Fees"].exclude_from_spending is False
    assert by_name["Taxes"].exclude_from_spending is False
    assert by_name["Cash Withdrawals"].exclude_from_spending is False
    # A user-excluded category stays excluded (preserved from its own flag).
    assert by_name["Transfers"].exclude_from_spending is True


def test_category_recommendations_surface_exclusion_and_show_no_flip():
    categories = CategoriesConfig(
        categories=[
            CategoryRule(name="Investing", merchants=[], exclude_from_spending=False),
            CategoryRule(name="Groceries", merchants=[], exclude_from_spending=False),
            CategoryRule(name="Transfers (Excluded)", merchants=[], exclude_from_spending=True),
        ]
    )

    recommendations, _optimized = optimize_taxonomy(categories)

    # Invariant: optimization never flips an existing category's exclusion.
    for rec in recommendations:
        assert rec.new_exclude_from_spending == rec.old_exclude_from_spending

    rows = category_rows(recommendations)
    for row in rows:
        assert "old_exclude_from_spending" in row
        assert "new_exclude_from_spending" in row
        # No silent spend-semantics change under the preserve policy.
        assert row["spend_semantics_change"] == ""


def test_category_rows_flags_a_real_include_to_exclude_change():
    # The surfacing is not vacuous: an actual flip is reported explicitly so the
    # user can see it before applying.
    flip = CategoryRecommendation(
        old_name="Investing",
        new_name="Investing",
        parent="Financial & Excluded",
        old_exclude_from_spending=False,
        new_exclude_from_spending=True,
    )
    reverse = CategoryRecommendation(
        old_name="Fees",
        new_name="Fees",
        parent="Financial & Excluded",
        old_exclude_from_spending=True,
        new_exclude_from_spending=False,
    )

    rows = category_rows([flip, reverse])

    assert rows[0]["spend_semantics_change"] == "include->exclude"
    assert rows[1]["spend_semantics_change"] == "exclude->include"


def test_merge_merchant_rules_preserves_merchant_exclusion_no_silent_flip():
    # F10 (merchant path): --execute-safe merges merchant rules too. A merchant the
    # user COUNTS must not be flipped to excluded just because its category name is in
    # EXCLUDED_CATEGORIES (e.g. Investing). Exclusion is resolved at apply-time from the
    # preserved category + merchant flags, so the merge must carry the rule's own flag
    # verbatim in both directions.
    merchants = MerchantRulesConfig(
        rules=[
            MerchantRule(
                merchant_norm="MERCHANT_237",
                category="Investing",
                patterns=[r"\bMERCHANT_237\b"],
                exclude_from_spending=False,
            ),
            MerchantRule(
                merchant_norm="MERCHANT_219",
                category="Transfers",
                patterns=[r"\bMERCHANT_219\b"],
                exclude_from_spending=True,
            ),
        ]
    )

    merged = merge_merchant_rules(
        merchants, auto_rules=[], category_map={"Investing": "Investing", "Transfers": "Transfers"}
    )

    by_norm = {rule.merchant_norm: rule for rule in merged.rules}
    # Counted Investing merchant is NOT flipped to excluded.
    assert by_norm["MERCHANT_237"].exclude_from_spending is False
    # Explicitly-excluded merchant stays excluded.
    assert by_norm["MERCHANT_219"].exclude_from_spending is True


def test_optimizer_dry_run_does_not_mutate_runtime_config(tmp_path: Path):
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    repo = SQLiteConfigRepository(db_path)
    repo.replace_from_configs(
        MerchantRulesConfig(
            rules=[
                MerchantRule(
                    merchant_norm="EXISTING",
                    category="General Shopping",
                    patterns=[r"\bEXISTING\b"],
                )
            ]
        ),
        CategoriesConfig(
            categories=[
                CategoryRule(name="General Shopping", merchants=[]),
                CategoryRule(name="Shopping", merchants=[]),
            ]
        ),
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    with session_scope(db_path) as session:
        for index in range(3):
            session.add(
                TransactionDB(
                    txn_id=f"txn-{index}",
                    posting_date=date(2025, 1, index + 1),
                    transaction_date=date(2025, 1, index + 1),
                    institution="harbor",
                    account="harbor-spark-visa-1004",
                    account_type="SPARK Visa",
                    flow="purchase",
                    cashflow_type="expense",
                    include_in_cashflow=True,
                    currency="CAD",
                    amount_raw=Decimal("50.00"),
                    amount_signed=Decimal("-50.00"),
                    description="SAFE MERCHANT",
                    merchant="SAFE MERCHANT",
                    category="General Shopping",
                    merchant_confidence="low",
                    category_confidence="low",
                    exclude_from_spending=False,
                    merchant_raw="SAFE MERCHANT",
                    category_raw="",
                    source_file="x.csv",
                    source_path="x.csv",
                    source_period="2025-01",
                    source_row=index + 2,
                    imported_at=now,
                    parser_version="test",
                    txn_fingerprint=f"fp-{index}",
                    day_of_week=0,
                    day_of_month=index + 1,
                    week_of_year=1,
                    is_weekend=False,
                    is_month_start=True,
                    is_month_end=False,
                )
            )

    result = CategoryOptimizer().run(
        CategoryOptimizationOptions(
            db_path=db_path,
            runs_dir=tmp_path / "runs",
            execute_safe=False,
            require_repaired=False,
        )
    )

    assert result.ok
    assert repo.load_merchant_rules().rules[0].merchant_norm == "EXISTING"
    recommendations, auto_rules, _manual = analyze_merchants(db_path, repo.load_merchant_rules())
    assert any(row["merchant"] == "SAFE MERCHANT" for row in recommendations)
    assert any(row["subject"] == "SAFE MERCHANT" for row in auto_rules)


# ---------------------------------------------------------------------------
# Task 8: optimize categories --execute-safe must back up DB before mutating
# ---------------------------------------------------------------------------


def _build_optimizer_db(tmp_path: Path) -> tuple[Path, SQLiteConfigRepository]:
    """Set up a minimal DB suitable for running CategoryOptimizer with execute_safe=True."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    repo = SQLiteConfigRepository(db_path)
    repo.replace_from_configs(
        MerchantRulesConfig(
            rules=[
                MerchantRule(
                    merchant_norm="EXISTING",
                    category="General Shopping",
                    patterns=[r"\bEXISTING\b"],
                )
            ]
        ),
        CategoriesConfig(
            categories=[
                CategoryRule(name="General Shopping", merchants=[]),
                CategoryRule(name="Shopping", merchants=[]),
            ]
        ),
    )
    now = datetime.now(UTC).replace(tzinfo=None)
    with session_scope(db_path) as session:
        for index in range(3):
            session.add(
                TransactionDB(
                    txn_id=f"txn-exec-{index}",
                    posting_date=date(2025, 1, index + 1),
                    transaction_date=date(2025, 1, index + 1),
                    institution="harbor",
                    account="harbor-spark-visa-1004",
                    account_type="SPARK Visa",
                    flow="purchase",
                    cashflow_type="expense",
                    include_in_cashflow=True,
                    currency="CAD",
                    amount_raw=Decimal("50.00"),
                    amount_signed=Decimal("-50.00"),
                    description="SAFE MERCHANT",
                    merchant="SAFE MERCHANT",
                    category="General Shopping",
                    merchant_confidence="low",
                    category_confidence="low",
                    exclude_from_spending=False,
                    merchant_raw="SAFE MERCHANT",
                    category_raw="",
                    source_file="x.csv",
                    source_path="x.csv",
                    source_period="2025-01",
                    source_row=index + 2,
                    imported_at=now,
                    parser_version="test",
                    txn_fingerprint=f"fp-exec-{index}",
                    day_of_week=0,
                    day_of_month=index + 1,
                    week_of_year=1,
                    is_weekend=False,
                    is_month_start=True,
                    is_month_end=False,
                )
            )
    return db_path, repo


def test_execute_safe_creates_backup_before_mutating(tmp_path: Path) -> None:
    """optimize categories --execute-safe must call backup_database BEFORE it mutates
    runtime config (replace_from_configs / apply_all).  The backup artifact must exist
    in the configured backup dir when the run succeeds."""
    from ledger.storage.operations import backup_database as real_backup_database

    db_path, _repo = _build_optimizer_db(tmp_path)
    backup_dir = tmp_path / "db-backups"

    call_order: list[str] = []

    def spy_backup(*args: object, **kwargs: object):  # type: ignore[return]
        call_order.append("backup_database")
        return real_backup_database(*args, **kwargs)  # type: ignore[arg-type]

    real_replace = SQLiteConfigRepository.replace_from_configs

    def spy_replace(self: SQLiteConfigRepository, *args: object, **kwargs: object) -> None:
        call_order.append("replace_from_configs")
        real_replace(self, *args, **kwargs)  # type: ignore[arg-type]

    with (
        patch("ledger.optimize.categories.backup_database", side_effect=spy_backup) as mock_backup,
        patch.object(SQLiteConfigRepository, "replace_from_configs", spy_replace),
    ):
        result = CategoryOptimizer().run(
            CategoryOptimizationOptions(
                db_path=db_path,
                runs_dir=tmp_path / "runs",
                execute_safe=True,
                exported_config_dir=tmp_path / "exported",
                require_repaired=False,
                backup_dir=backup_dir,
            )
        )

    assert result.ok, f"Optimizer failed: {result.errors}"
    mock_backup.assert_called_once()
    # backup must have been called BEFORE replace_from_configs
    assert call_order.index("backup_database") < call_order.index("replace_from_configs"), (
        "backup_database must be called before replace_from_configs; got order: " + str(call_order)
    )
    # A real backup file must exist on disk
    backup_files = list(backup_dir.glob("*.db"))
    assert backup_files, "No backup file was created in the backup dir"


def test_execute_safe_aborts_if_backup_fails(tmp_path: Path) -> None:
    """If backup_database returns a failed RunResult, the optimizer must raise
    and must NOT call replace_from_configs or apply_all."""
    from ledger.core.results import RunResult

    db_path, _repo = _build_optimizer_db(tmp_path)

    failed_backup = RunResult(
        status="failed",
        started_at=datetime.now(UTC).replace(tzinfo=None),
        finished_at=datetime.now(UTC).replace(tzinfo=None),
        errors=["disk full"],
    )

    with (
        patch("ledger.optimize.categories.backup_database", return_value=failed_backup),
        patch.object(SQLiteConfigRepository, "replace_from_configs") as mock_replace,
        patch.object(RuleApplicationService, "apply_all") as mock_apply_all,
    ):
        result = CategoryOptimizer().run(
            CategoryOptimizationOptions(
                db_path=db_path,
                runs_dir=tmp_path / "runs",
                execute_safe=True,
                exported_config_dir=tmp_path / "exported",
                require_repaired=False,
                backup_dir=tmp_path / "db-backups",
            )
        )

    assert not result.ok
    assert any("backup" in e.lower() for e in result.errors), (
        "Expected a backup-related error message; got: " + str(result.errors)
    )
    mock_replace.assert_not_called()
    mock_apply_all.assert_not_called()


def test_execute_safe_holds_target_boundary_through_artifact_failure_and_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from ledger.imports.pdf import workflow
    from ledger.imports.pdf.paths import Paths
    from ledger.imports.pdf.readiness import import_owned_fingerprint, import_owned_match
    from ledger.optimize import categories as categories_module
    from ledger.storage.snapshots import DatabaseTargetLockError

    db_path, _repo = _build_optimizer_db(tmp_path)
    before = import_owned_fingerprint(db_path)
    pdf_dir = tmp_path / "inbox"
    pdf_dir.mkdir()
    (pdf_dir / "synthetic.pdf").write_bytes(b"%PDF-1.4 synthetic")
    import_runs = tmp_path / "import-runs"
    paths = Paths(
        pdf_dir=pdf_dir,
        processed_dir=tmp_path / "processed",
        db_path=db_path,
        base_db_path=None,
        backup_dir=tmp_path / "import-backups",
        ledger_config=Path("configs/ledger.toml"),
        merchant_rules=Path("configs/merchants.toml"),
        categories=Path("configs/categories.toml"),
        runs_dir=import_runs,
    )
    boundary_phases: set[str] = set()

    def assert_competing_stage_refused(phase: str) -> None:
        with pytest.raises(DatabaseTargetLockError):
            workflow.stage(SimpleNamespace(), paths)
        boundary_phases.add(phase)

    def fail_artifact(*_args: object, **_kwargs: object) -> None:
        if "artifact" not in boundary_phases:
            assert_competing_stage_refused("artifact")
        raise RuntimeError("synthetic artifact failure")

    real_restore = categories_module.restore_database

    def probe_restore(*args: object, **kwargs: object) -> None:
        assert_competing_stage_refused("restore")
        real_restore(*args, **kwargs)

    real_write_json = categories_module.write_json

    def probe_failure_summary(path: Path, payload: dict[str, object]) -> None:
        if payload.get("status") == "failed":
            assert_competing_stage_refused("failure_summary")
        real_write_json(path, payload)

    real_record = categories_module.record_db_operation

    def probe_failed_audit(*args: object, **kwargs: object) -> None:
        result = kwargs.get("result")
        if getattr(result, "status", None) == "failed":
            assert_competing_stage_refused("failed_audit")
        real_record(*args, **kwargs)

    monkeypatch.setattr(categories_module, "write_csv", fail_artifact)
    monkeypatch.setattr(categories_module, "restore_database", probe_restore)
    monkeypatch.setattr(categories_module, "write_json", probe_failure_summary)
    monkeypatch.setattr(categories_module, "record_db_operation", probe_failed_audit)

    result = CategoryOptimizer().run(
        CategoryOptimizationOptions(
            db_path=db_path,
            runs_dir=tmp_path / "optimization-runs",
            execute_safe=True,
            exported_config_dir=tmp_path / "exported",
            require_repaired=False,
            backup_dir=tmp_path / "db-backups",
        )
    )

    assert result.status == "failed"
    assert boundary_phases == {"artifact", "restore", "failure_summary", "failed_audit"}
    assert import_owned_match(before, import_owned_fingerprint(db_path))
    assert not import_runs.exists()


# ---------------------------------------------------------------------------
# Task 7: repair statements default is validate-only (requires --execute to promote)
# ---------------------------------------------------------------------------


def test_repair_statements_default_does_not_promote(tmp_path: Path) -> None:
    """The repair service with dry_run=True (CLI default: no --execute) must not call
    promote_candidate and must leave the live DB file untouched."""
    live_db = tmp_path / "ledger.db"
    live_db.write_bytes(b"sentinel")
    mtime_before = live_db.stat().st_mtime

    options = StatementRepairOptions(
        db_path=live_db,
        pdf_dirs=[],  # no PDFs — service will fail before promotion
        processed_dir=tmp_path / "processed",
        runs_dir=tmp_path / "runs",
        backup_dir=tmp_path / "backups",
        dry_run=True,  # validate-only; this is what the CLI must pass by default
    )

    with patch("ledger.repair.statements.promote_candidate") as mock_promote:
        StatementRepairService().run(options)

    # No PDFs means the run ends with status "failed" (no-PDFs error), which is fine —
    # the critical assertion is that promotion was never invoked.
    mock_promote.assert_not_called()
    assert live_db.stat().st_mtime == mtime_before, (
        "live DB must not be modified in validate-only mode"
    )
    assert live_db.read_bytes() == b"sentinel", "live DB content must be unchanged"


def test_repair_statements_cli_default_is_validate_only() -> None:
    """The CLI command must pass dry_run=True (validate-only) when --execute is not supplied.
    This test calls the CLI handler with no --execute flag and asserts that
    StatementRepairService.run receives dry_run=True.

    The repair command does a lazy import (`from ledger.repair import ...`) inside the
    function body, so we patch `ledger.repair.StatementRepairService` — the canonical
    module path that the lazy import resolves to.
    """
    import typer.testing

    from ledger.commands.repair import repair_app
    from ledger.repair.statements import StatementRepairResult

    runner = typer.testing.CliRunner()

    captured_options: list[StatementRepairOptions] = []

    class _FakeService:
        def run(self, options: StatementRepairOptions) -> StatementRepairResult:
            captured_options.append(options)
            return StatementRepairResult(
                "validated", options.runs_dir, options.runs_dir / "c.db", {}
            )

    with patch("ledger.repair.StatementRepairService", _FakeService):
        # repair_app is the sub-app; "statements" is already the single command on it.
        # Invoke directly without repeating the subcommand name.
        result = runner.invoke(repair_app, ["--db", "/dev/null"])

    assert captured_options, "StatementRepairService.run was not called"
    assert captured_options[0].dry_run is True, (
        "CLI default must be validate-only (dry_run=True); got dry_run="
        f"{captured_options[0].dry_run!r}. "
        "Did you forget to invert the --execute flag?"
    )
    # The fake service returns status="validated" (ok), so the validate-only hint must appear.
    assert "--execute" in result.output, (
        "validate-only run must print the '--execute' hint; got output:\n" + result.output
    )


# ---------------------------------------------------------------------------
# Defect 2: repair promote must NOT false-abort after backup_database write
# ---------------------------------------------------------------------------


def _make_minimal_repair_db(tmp_path: Path) -> Path:
    """Create a minimal initialized live DB for repair tests."""
    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    return db_path


def _repair_options(
    tmp_path: Path, pdf_dir: Path, *, dry_run: bool = False
) -> StatementRepairOptions:
    return StatementRepairOptions(
        db_path=tmp_path / "ledger.db",
        pdf_dirs=[pdf_dir],
        processed_dir=tmp_path / "processed",
        runs_dir=tmp_path / "runs",
        backup_dir=tmp_path / "backups",
        ledger_config=tmp_path / "ledger.toml",
        merchant_rules=tmp_path / "merchants.toml",
        categories=tmp_path / "categories.toml",
        dry_run=dry_run,
    )


def _apply_repair_mocks_pre_promote(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch all heavy steps that precede backup_database/promote_candidate.

    Keeps backup_database and promote_candidate real so we exercise the actual
    fingerprint-capture logic.

    We do NOT mock init_database or run_migrations — the candidate_db must be
    genuinely created on disk so that promote_candidate can copy it onto the live DB.
    """
    from pathlib import Path as _Path
    from types import SimpleNamespace as _Namespace

    import ledger.repair.statements as _svc
    from ledger.imports.pdf.statement_pdf_to_csv import ExtractedCsv as _ExtractedCsv
    from ledger.processing import ProcessingResult as _ProcessingResult

    # discover_pdfs: return a single dummy PDF path
    def fake_discover(pdf_dirs):
        return [_Path(pdf_dirs[0]) / "dummy.pdf"] if pdf_dirs else []

    monkeypatch.setattr(_svc, "discover_pdfs", fake_discover)

    # run_migrations: no-op (init_database must stay real so candidate_db is created)
    monkeypatch.setattr(_svc, "run_migrations", lambda _p: None)

    # copy_runtime_config: no-op
    monkeypatch.setattr(_svc, "copy_runtime_config", lambda *_a, **_k: None)

    # process_pdf extraction: return a valid ExtractedCsv stub
    def fake_process_pdf(pdf, csv_root, processed_dir, **_kwargs):
        csv_path = csv_root / "2025-01" / "dummy.csv"
        csv_path.parent.mkdir(parents=True, exist_ok=True)
        csv_path.write_text("Date,Description,Amount\n2025-01-01,TEST,-10.00\n", encoding="utf-8")
        return _ExtractedCsv(
            pdf=pdf,
            csv_path=csv_path,
            processed_pdf=processed_dir / "dummy.pdf",
            statement_type="harbor_deposit",
            transaction_count=1,
            validation_summary={
                "opening_balance": "110.00",
                "closing_balance": "100.00",
                "printed_credit_limit": "5000.00",
            },
            account_slug="harbor-chequing-1001",
        )

    monkeypatch.setattr(_svc.statement_pdf_to_csv, "process_pdf", fake_process_pdf)
    monkeypatch.setattr(
        _svc.statement_pdf_to_csv,
        "write_manifest",
        lambda path, _results, dry_run: path.write_text("[]", encoding="utf-8"),
    )

    # build_inventory / write_csv: no-ops
    monkeypatch.setattr(_svc, "build_inventory", lambda _results: [])
    monkeypatch.setattr(_svc, "write_csv", lambda _path, _rows: None)

    # load_runtime_config
    fake_config = _Namespace(
        ledger=_Namespace(general=_Namespace(account_aliases={})),
    )
    monkeypatch.setattr(_svc, "load_runtime_config", lambda _db, _opts: fake_config)

    # process / validate_processing_result / write_result_to_db
    monkeypatch.setattr(_svc, "process", lambda **_kw: _ProcessingResult(total_scanned=1))
    monkeypatch.setattr(_svc, "validate_processing_result", lambda *_a: None)
    monkeypatch.setattr(_svc, "write_result_to_db", lambda *_a: None)

    # refresh_repair_analytics
    monkeypatch.setattr(_svc, "refresh_repair_analytics", lambda *_a, **_k: {})

    # build_move_plan / write_json / validate_move_plan / record_processed_files
    monkeypatch.setattr(_svc, "build_move_plan", lambda _results, _dir: [])
    monkeypatch.setattr(_svc, "write_json", lambda _path, _data: None)
    monkeypatch.setattr(_svc, "validate_move_plan", lambda _plan: None)
    monkeypatch.setattr(_svc, "record_processed_files", lambda *_a: None)

    # StatementAuditService
    from unittest.mock import MagicMock as _Mock

    fake_audit = _Mock()
    fake_audit.run.return_value = _Mock(
        run_dir=_Path("/tmp/audit"),
        status="ok",
        summary={
            "counts": {
                "processed_file_hash_matches": 1,
            },
            "pdfs": 1,
        },
    )
    monkeypatch.setattr(_svc, "StatementAuditService", lambda: fake_audit)

    # validate_candidate_audit / validate_database
    monkeypatch.setattr(_svc, "validate_candidate_audit", lambda *_a, **_k: None)
    monkeypatch.setattr(
        _svc, "validate_database", lambda *_a, **_k: _Mock(status="success", errors=[])
    )

    # prepare_pdf_targets / remove_replaced_sources: no-ops
    monkeypatch.setattr(_svc, "prepare_pdf_targets", lambda _plan: None)
    monkeypatch.setattr(_svc, "remove_replaced_sources", lambda _plan: None)

    # record_db_operation (post-promote audit step): no-op
    monkeypatch.setattr(_svc, "record_db_operation", lambda *_a, **_k: None)


def test_repair_passes_extracted_account_slug_mapping_to_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ledger.repair.statements as repair_statements

    _make_minimal_repair_db(tmp_path)
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    (pdf_dir / "dummy.pdf").write_bytes(b"%PDF-1.4 fake")
    options = _repair_options(tmp_path, pdf_dir, dry_run=True)
    _apply_repair_mocks_pre_promote(monkeypatch)
    captured: dict[str, object] = {}

    def capture_process(**kwargs):
        captured.update(kwargs)
        return ProcessingResult(total_scanned=1)

    monkeypatch.setattr(repair_statements, "process", capture_process)

    result = StatementRepairService().run(options)

    assert result.status == "validated"
    assert captured["account_slugs"] == {"2025-01/dummy.csv": "harbor-chequing-1001"}


def test_repair_persists_printed_credit_limits_before_account_analysis(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    sample_app_config,
) -> None:
    import ledger.repair.statements as repair_statements
    from ledger.storage.operations import get_latest_statement_credit_limit

    real_write_manifest = repair_statements.statement_pdf_to_csv.write_manifest
    _make_minimal_repair_db(tmp_path)
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    (pdf_dir / "dummy.pdf").write_bytes(b"%PDF-1.4 fake")
    options = _repair_options(tmp_path, pdf_dir, dry_run=True)
    _apply_repair_mocks_pre_promote(monkeypatch)
    monkeypatch.setattr(
        repair_statements.statement_pdf_to_csv,
        "write_manifest",
        real_write_manifest,
    )
    aliased_general = sample_app_config.ledger.general.model_copy(
        update={"account_aliases": {"harbor-chequing-1001": "household-chequing"}}
    )
    aliased_ledger = sample_app_config.ledger.model_copy(update={"general": aliased_general})
    aliased_config = sample_app_config.model_copy(update={"ledger": aliased_ledger})
    monkeypatch.setattr(
        repair_statements,
        "load_runtime_config",
        lambda _db, _options: aliased_config,
    )
    observed: dict[str, Decimal | None] = {}

    def observe_limit_before_analytics(db_path, _config, _extractions):
        observed["aliased"] = get_latest_statement_credit_limit(
            db_path,
            "household-chequing",
        )
        observed["canonical"] = get_latest_statement_credit_limit(db_path, "harbor-chequing-1001")
        return {}

    monkeypatch.setattr(
        repair_statements, "refresh_repair_analytics", observe_limit_before_analytics
    )

    result = StatementRepairService().run(options)

    assert result.status == "validated"
    assert observed == {
        "aliased": Decimal("5000.00"),
        "canonical": None,
    }


def test_repair_execute_happy_path_no_false_abort(tmp_path: Path, monkeypatch) -> None:
    """StatementRepairService.run(dry_run=False) must SUCCEED without false-aborting
    when the live DB has not been concurrently modified by an external process.

    Pre-fix: backup_database writes a db_operation_audit row to the live DB, changing
    its sha256/mtime/counts.  The pre_promote_fingerprint is captured BEFORE backup,
    so promote_candidate's recheck sees a mismatch and raises RuntimeError on EVERY
    repair run — not just genuine concurrent writes.

    Post-fix: the fingerprint is captured AFTER backup_database (and prepare_pdf_targets),
    so the recheck only fires on a genuine external concurrent write.
    """
    from ledger.repair.statements import StatementRepairService

    _make_minimal_repair_db(tmp_path)
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    (pdf_dir / "dummy.pdf").write_bytes(b"%PDF-1.4 fake")

    options = _repair_options(tmp_path, pdf_dir, dry_run=False)

    _apply_repair_mocks_pre_promote(monkeypatch)

    # Run end-to-end with real backup_database and promote_candidate.
    result = StatementRepairService().run(options)

    assert result.status == "success", (
        f"repair --execute must succeed when no concurrent write occurred; "
        f"got status={result.status!r}. "
        "Pre-fix symptom: RuntimeError 'target DB changed during finalize' because "
        "backup_database wrote an audit row before fingerprint was captured."
    )


def test_repair_execute_genuine_concurrent_write_still_aborts(tmp_path: Path, monkeypatch) -> None:
    """A genuine concurrent write to the live DB AFTER the fingerprint is captured
    but BEFORE the atomic promote must still abort with the live DB intact.

    This verifies that the fix (moving the fingerprint capture after backup) does NOT
    disable the concurrent-write guard — it only prevents the self-inflicted false abort.

    The write is injected inside promote_candidate (wrapping the real function): it
    arrives after the fingerprint snapshot but before the atomic swap, so only the
    recheck inside promote_candidate can catch it.
    """
    import sqlite3 as _sqlite3
    from contextlib import closing as _closing
    from datetime import UTC as _UTC
    from datetime import datetime as _datetime

    import ledger.repair.statements as _svc
    from ledger.repair.statements import StatementRepairService
    from ledger.repair.statements import _promote_candidate_locked as real_promote_candidate_locked

    live_db = _make_minimal_repair_db(tmp_path)
    pdf_dir = tmp_path / "pdfs"
    pdf_dir.mkdir()
    (pdf_dir / "dummy.pdf").write_bytes(b"%PDF-1.4 fake")

    options = _repair_options(tmp_path, pdf_dir, dry_run=False)

    _apply_repair_mocks_pre_promote(monkeypatch)

    def _insert_txn(db_path: Path, txn_id: str) -> None:
        now = _datetime.now(_UTC).replace(microsecond=0).isoformat()
        with _closing(_sqlite3.connect(db_path)) as conn, conn:
            conn.execute(
                """
                insert into transactions (
                    txn_id, posting_date, institution, account, account_type, flow,
                    cashflow_type, include_in_cashflow, currency, amount_raw, amount_signed,
                    description, merchant, category, merchant_confidence, category_confidence,
                    exclude_from_spending, merchant_raw, category_raw, source_file, source_path,
                    source_period, source_row, imported_at, parser_version, txn_fingerprint,
                    day_of_week, day_of_month, week_of_year, is_weekend, is_month_start,
                    is_month_end
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    txn_id,
                    "2044-02-10",
                    "harbor",
                    "harbor-chequing",
                    "Chequing",
                    "expense",
                    "expense",
                    1,
                    "CAD",
                    "18.60",
                    "-18.60",
                    "test",
                    "TEST",
                    "Other",
                    "low",
                    "low",
                    0,
                    "TEST",
                    "",
                    "statement.csv",
                    "2044-02/statement.csv",
                    "2044-02",
                    1,
                    now,
                    "test",
                    f"fp-{txn_id}",
                    2,
                    10,
                    6,
                    0,
                    0,
                    0,
                ),
            )

    def concurrent_write_inside_promote(
        candidate: Path, target: Path, *, expected_fingerprint=None, expected_import_owned=None
    ) -> None:
        # Simulate a concurrent write that lands AFTER the fingerprint snapshot but
        # BEFORE the atomic swap — inside the promote call itself.
        _insert_txn(target, "txn-repair-genuine-concurrent")
        return real_promote_candidate_locked(
            candidate,
            target,
            expected_fingerprint=expected_fingerprint,
            expected_import_owned=expected_import_owned,
        )

    monkeypatch.setattr(_svc, "_promote_candidate_locked", concurrent_write_inside_promote)

    # StatementRepairService.run catches the RuntimeError internally and returns
    # status='failed', so we assert on the returned status (not on a re-raised exception).
    result = StatementRepairService().run(options)
    assert result.status == "failed", (
        f"expected status='failed' when concurrent write aborts the promote; got {result.status!r}"
    )
    error_msg = result.summary.get("error", "")
    assert "target DB changed during finalize" in error_msg, (
        f"expected abort error message in summary; got: {error_msg!r}"
    )

    # The key assertion: promote did NOT overwrite the live DB, so our injected transaction
    # must still be in the live DB.
    with _closing(_sqlite3.connect(live_db)) as conn:
        txn_count = conn.execute(
            "select count(*) from transactions where txn_id = 'txn-repair-genuine-concurrent'"
        ).fetchone()[0]
    assert txn_count == 1, (
        "genuine concurrent write was lost — promote_candidate overwrote the live DB "
        "despite the fingerprint mismatch (concurrent-write guard broken)"
    )


# ---------------------------------------------------------------------------
# Fix: execute_safe must restore backup + write FAILED audit row on mid-run failure
# ---------------------------------------------------------------------------


def test_execute_safe_restores_backup_on_mid_run_failure(tmp_path: Path) -> None:
    """If apply_all raises AFTER replace_from_configs has already mutated the config
    tables, execute_safe must:
      (a) restore the live DB from the pre-mutation backup (partial mutation rolled back —
          config tables are back to their pre-optimize state),
      (b) write a FAILED db_operation_audit row for 'category_optimize' carrying the error
          and the backup artifact path,
      (c) return a result with status='failed' (not raise).

    Pre-fix: the except block only appended errors and wrote the summary JSON — no
    restore and no audit row — leaving config tables mutated but transactions un-applied.
    The config tables would contain the full optimized taxonomy (many more categories)
    while the transactions still had their original categories.
    """
    from ledger.storage.database import CategoryConfigDB, MerchantRuleDB

    db_path, _repo = _build_optimizer_db(tmp_path)
    backup_dir = tmp_path / "db-backups"

    # Record the pre-optimize config table state.
    # _build_optimizer_db inserts 2 categories ("General Shopping", "Shopping")
    # and 1 merchant rule ("EXISTING"). After replace_from_configs, the taxonomy
    # expands to many more categories (ROOT_CATEGORIES + sub-categories). After
    # restore, it must be back to these original 2.
    with session_scope(db_path) as session:
        original_category_names = sorted(row.name for row in session.query(CategoryConfigDB).all())
        original_merchant_norms = sorted(
            row.merchant_norm for row in session.query(MerchantRuleDB).all()
        )

    assert len(original_category_names) == 2, (
        f"test setup: expected 2 categories; got {original_category_names}"
    )

    # Inject a failure in apply_all — AFTER replace_from_configs has already run.
    def boom_apply_all(self, **kwargs):  # type: ignore[misc]
        raise RuntimeError("simulated apply_all failure mid-optimize")

    from ledger.optimize import categories as categories_module

    real_backup = categories_module.backup_database

    def permissive_backup(*args: object, **kwargs: object):
        result = real_backup(*args, **kwargs)  # type: ignore[arg-type]
        Path(result.artifacts["backup"]).chmod(0o644)
        return result

    with (
        patch.object(RuleApplicationService, "apply_all", boom_apply_all),
        patch("ledger.optimize.categories.backup_database", side_effect=permissive_backup),
        patch("shutil.copy2", side_effect=AssertionError("raw DB restore is forbidden")),
    ):
        result = CategoryOptimizer().run(
            CategoryOptimizationOptions(
                db_path=db_path,
                runs_dir=tmp_path / "runs",
                execute_safe=True,
                exported_config_dir=tmp_path / "exported",
                require_repaired=False,
                backup_dir=backup_dir,
            )
        )

    # (c) must return failed, not raise
    assert result.status == "failed", f"Expected status='failed', got {result.status!r}"
    assert result.errors, "Expected at least one error string in result.errors"

    # (a) live DB must be restored — config tables must be back to pre-optimize state.
    # Pre-fix: replace_from_configs writes the full optimized taxonomy (many categories)
    # and the except block does NOT restore. Post-fix: the backup is copied back.
    with session_scope(db_path) as session:
        post_category_names = sorted(row.name for row in session.query(CategoryConfigDB).all())
        post_merchant_norms = sorted(
            row.merchant_norm for row in session.query(MerchantRuleDB).all()
        )

    assert post_category_names == original_category_names, (
        f"Config tables were NOT restored after mid-run failure.\n"
        f"  category_config before: {original_category_names}\n"
        f"  category_config after:  {post_category_names}\n"
        "Expected the backup to be restored, reverting the partial replace_from_configs write."
    )
    assert post_merchant_norms == original_merchant_norms, (
        f"merchant_rules were NOT restored after mid-run failure.\n"
        f"  before: {original_merchant_norms}\n"
        f"  after:  {post_merchant_norms}"
    )
    assert db_path.stat().st_mode & 0o777 == 0o600

    # (b) a FAILED audit row for 'category_optimize' must exist
    with session_scope(db_path) as session:
        audit_rows = (
            session.query(DBOperationAuditDB)
            .filter(
                DBOperationAuditDB.operation == "category_optimize",
                DBOperationAuditDB.status == "failed",
            )
            .all()
        )
        assert audit_rows, (
            "No FAILED db_operation_audit row for 'category_optimize' was written. "
            "The destructive attempt must be recorded even on failure."
        )
        row = audit_rows[0]
        row_errors = list(row.errors or [])
        row_artifact = row.artifact_path

    assert row_errors, "Audit row must carry the error list"
    assert any("apply_all" in e or "simulated" in e for e in row_errors), (
        f"Error text not found in audit row errors: {row_errors}"
    )
    # backup artifact path must be referenced in the audit row
    assert row_artifact is not None, "Audit row must reference the backup artifact path"
    assert backup_dir.name in row_artifact or "backups" in row_artifact, (
        f"Audit artifact_path should point to backup dir; got: {row_artifact!r}"
    )


def test_execute_safe_restore_replaces_committed_wal_state(tmp_path: Path) -> None:
    from ledger.storage.database import CategoryConfigDB

    db_path, _repo = _build_optimizer_db(tmp_path)
    with session_scope(db_path) as session:
        original_categories = sorted(row.name for row in session.query(CategoryConfigDB).all())

    keeper = sqlite3.connect(db_path)
    try:
        assert keeper.execute("pragma journal_mode=wal").fetchone()[0] == "wal"
        keeper.execute("pragma wal_autocheckpoint=0")
        keeper.execute("pragma wal_checkpoint(truncate)")

        def fail_after_replacement(self, **_kwargs):  # type: ignore[misc]
            assert db_path.with_name(db_path.name + "-wal").exists()
            raise RuntimeError("fail after WAL-backed config replacement")

        with patch.object(RuleApplicationService, "apply_all", fail_after_replacement):
            result = CategoryOptimizer().run(
                CategoryOptimizationOptions(
                    db_path=db_path,
                    runs_dir=tmp_path / "runs",
                    execute_safe=True,
                    exported_config_dir=tmp_path / "exported",
                    require_repaired=False,
                    backup_dir=tmp_path / "db-backups",
                )
            )
    finally:
        keeper.close()

    assert result.status == "failed"
    with session_scope(db_path) as session:
        restored_categories = sorted(row.name for row in session.query(CategoryConfigDB).all())
    assert restored_categories == original_categories


def test_execute_safe_happy_path_unchanged(tmp_path: Path) -> None:
    """A successful execute_safe run still applies the optimization and records a
    SUCCESS audit row for 'category_optimize'. Verifies the fix did not break the
    normal code path."""
    db_path, _repo = _build_optimizer_db(tmp_path)
    backup_dir = tmp_path / "db-backups"

    result = CategoryOptimizer().run(
        CategoryOptimizationOptions(
            db_path=db_path,
            runs_dir=tmp_path / "runs",
            execute_safe=True,
            exported_config_dir=tmp_path / "exported",
            require_repaired=False,
            backup_dir=backup_dir,
        )
    )

    assert result.ok, f"Expected successful optimize; errors: {result.errors}"

    # A SUCCESS audit row must exist for 'category_optimize'
    with session_scope(db_path) as session:
        audit_count = (
            session.query(DBOperationAuditDB)
            .filter(
                DBOperationAuditDB.operation == "category_optimize",
                DBOperationAuditDB.status == "success",
            )
            .count()
        )
    assert audit_count > 0, "No SUCCESS db_operation_audit row for 'category_optimize'"

    # A backup file must exist in the backup dir
    backup_files = list(backup_dir.glob("*.db"))
    assert backup_files, "No backup file found after successful execute_safe"


# ---------------------------------------------------------------------------
# H1: execute_safe must fail closed when apply_all returns a non-success status
# (defense-in-depth: apply_all EARLY-RETURNS validation_failed without raising)
# ---------------------------------------------------------------------------


def test_execute_safe_fails_closed_when_apply_all_returns_validation_failed(
    tmp_path: Path,
) -> None:
    """If apply_all returns a result whose status != 'success' (e.g. 'validation_failed')
    WITHOUT raising, execute_safe must treat it as a HARD failure:
      (a) restore the live DB from the pre-mutation backup (config tables reverted),
      (b) return status='failed' (NOT 'success'),
      (c) surface the validation issues in result.errors.

    Pre-fix: apply_result.status was never inspected; validation issues were only
    appended to warnings and the run reported 'success', leaving the live DB
    post-replace-but-rules-not-applied and the backup un-restored.
    """
    from ledger.rules.service import RuleApplicationResult
    from ledger.storage.database import CategoryConfigDB, MerchantRuleDB

    db_path, _repo = _build_optimizer_db(tmp_path)
    backup_dir = tmp_path / "db-backups"

    with session_scope(db_path) as session:
        original_category_names = sorted(row.name for row in session.query(CategoryConfigDB).all())
        original_merchant_norms = sorted(
            row.merchant_norm for row in session.query(MerchantRuleDB).all()
        )

    # apply_all returns (does NOT raise) a validation_failed result — mirrors the real
    # early-return in RuleApplicationService.apply_all when repo.validate() fails.
    def failed_apply_all(self, **kwargs):  # type: ignore[misc]
        return RuleApplicationResult(
            status="validation_failed",
            source="category_optimizer",
            transactions_scanned=0,
            transactions_updated=0,
            changed_merchants=0,
            changed_categories=0,
            analytics_refreshed=False,
            validation_issues=["Parent category 'X' must not list merchants."],
            details={},
        )

    with patch.object(RuleApplicationService, "apply_all", failed_apply_all):
        result = CategoryOptimizer().run(
            CategoryOptimizationOptions(
                db_path=db_path,
                runs_dir=tmp_path / "runs",
                execute_safe=True,
                exported_config_dir=tmp_path / "exported",
                require_repaired=False,
                backup_dir=backup_dir,
            )
        )

    # (b) must NOT report success
    assert result.status == "failed", f"Expected status='failed', got {result.status!r}"
    assert not result.ok

    # (c) the validation issue must surface as an error, not merely a warning
    assert any("must not list merchants" in e for e in result.errors), (
        f"Expected the validation issue in result.errors; got: {result.errors}"
    )

    # (a) config tables must be restored to their pre-optimize state
    with session_scope(db_path) as session:
        post_category_names = sorted(row.name for row in session.query(CategoryConfigDB).all())
        post_merchant_norms = sorted(
            row.merchant_norm for row in session.query(MerchantRuleDB).all()
        )
    assert post_category_names == original_category_names, (
        "Config tables were NOT restored after apply_all returned validation_failed.\n"
        f"  before: {original_category_names}\n  after:  {post_category_names}"
    )
    assert post_merchant_norms == original_merchant_norms

    # a FAILED audit row for 'category_optimize' must exist (not a success row)
    with session_scope(db_path) as session:
        failed_rows = (
            session.query(DBOperationAuditDB)
            .filter(
                DBOperationAuditDB.operation == "category_optimize",
                DBOperationAuditDB.status == "failed",
            )
            .count()
        )
        success_rows = (
            session.query(DBOperationAuditDB)
            .filter(
                DBOperationAuditDB.operation == "category_optimize",
                DBOperationAuditDB.status == "success",
            )
            .count()
        )
    assert failed_rows > 0, "Expected a FAILED audit row for 'category_optimize'"
    assert success_rows == 0, "A SUCCESS audit row must NOT be written on validation failure"


# ---------------------------------------------------------------------------
# H1: refuse an invalid taxonomy BEFORE the destructive replace — a merchant rule
# whose optimized target category becomes a PARENT must be rejected pre-replace
# ---------------------------------------------------------------------------


def test_execute_safe_refuses_merchant_rule_on_parent_before_replace(tmp_path: Path) -> None:
    """A merchant rule on a leaf category that the taxonomy remap turns into a PARENT
    must be refused BEFORE replace_from_configs commits (no destructive replace, no
    half-mutated DB). Mirrors the finding: a rule on 'Shopping' (leaf) whose optimized
    target 'Shopping & Lifestyle' becomes a parent (it gains 'General Shopping' as a
    child), leaving the parent owning a rule-derived merchant association.

    Assert: the run does NOT report success, the live DB config is UNCHANGED (the
    destructive replace never happened), and an error explains the parent/merchant
    conflict.
    """
    from ledger.storage.database import CategoryConfigDB, MerchantRuleDB

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    repo = SQLiteConfigRepository(db_path)
    # Pre-optimize this is VALID: 'Shopping' and 'General Shopping' are both leaves,
    # and the merchant rule points at the leaf 'Shopping'. After optimize_taxonomy,
    # 'Shopping' -> 'Shopping & Lifestyle' (a ROOT) and 'General Shopping' is reparented
    # under 'Shopping & Lifestyle', so the remapped rule now targets a PARENT.
    repo.replace_from_configs(
        MerchantRulesConfig(
            rules=[
                MerchantRule(
                    merchant_norm="CINEPLEXX",
                    category="Shopping",
                    patterns=[r"\bCINEPLEXX\b"],
                )
            ]
        ),
        CategoriesConfig(
            categories=[
                CategoryRule(name="Shopping", merchants=[]),
                CategoryRule(name="General Shopping", merchants=[]),
            ]
        ),
    )

    with session_scope(db_path) as session:
        original_category_names = sorted(row.name for row in session.query(CategoryConfigDB).all())
        original_merchant_norms = sorted(
            row.merchant_norm for row in session.query(MerchantRuleDB).all()
        )

    result = CategoryOptimizer().run(
        CategoryOptimizationOptions(
            db_path=db_path,
            runs_dir=tmp_path / "runs",
            execute_safe=True,
            exported_config_dir=tmp_path / "exported",
            require_repaired=False,
            backup_dir=tmp_path / "db-backups",
        )
    )

    # Must not report success
    assert result.status == "failed", f"Expected status='failed', got {result.status!r}"
    assert not result.ok

    # An error must explain the parent/merchant conflict
    assert any("parent" in e.lower() and "merchant" in e.lower() for e in result.errors), (
        f"Expected a parent/merchant conflict error; got: {result.errors}"
    )

    # Live DB config must be UNCHANGED — the destructive replace must NOT have run
    with session_scope(db_path) as session:
        post_category_names = sorted(row.name for row in session.query(CategoryConfigDB).all())
        post_merchant_norms = sorted(
            row.merchant_norm for row in session.query(MerchantRuleDB).all()
        )
    assert post_category_names == original_category_names, (
        "Config was mutated despite an invalid optimized taxonomy.\n"
        f"  before: {original_category_names}\n  after:  {post_category_names}"
    )
    assert post_merchant_norms == original_merchant_norms


def test_replace_from_configs_refuses_rule_on_parent_before_destructive_replace(
    tmp_path: Path,
) -> None:
    """ISOLATION for the H1 PRE-replace guard (config/repository.py `_validate_configs_in_memory`
    lines ~84-89): a merchant rule whose target category is a PARENT must be rejected by
    ``replace_from_configs`` itself — BEFORE it deletes any of the existing valid config —
    raising ConfigError.

    This test exercises the pre-replace guard in ISOLATION (not through CategoryOptimizer),
    so it FAILS if the pre-replace guard block alone is reverted, even though the H1 part-1
    POST-replace backstop in CategoryOptimizer is left intact. The end-state-only H1 test
    above still passes with only the backstop; this one does not.
    """
    from ledger.core.exceptions import ConfigError

    db_path = tmp_path / "ledger.db"
    init_database(db_path)
    repo = SQLiteConfigRepository(db_path)

    # Seed a KNOWN-GOOD runtime config so we can prove the destructive replace never ran.
    repo.replace_from_configs(
        MerchantRulesConfig(
            rules=[
                MerchantRule(
                    merchant_norm="GOODMART", category="Groceries", patterns=[r"\bGOODMART\b"]
                )
            ]
        ),
        CategoriesConfig(categories=[CategoryRule(name="Groceries", merchants=[])]),
    )
    from ledger.storage.database import CategoryConfigDB, MerchantRuleDB

    with session_scope(db_path) as session:
        seeded_categories = sorted(row.name for row in session.query(CategoryConfigDB).all())
        seeded_merchants = sorted(row.merchant_norm for row in session.query(MerchantRuleDB).all())

    # A config where 'Shopping' is a PARENT (it has 'General Shopping' as a child) AND a
    # merchant rule targets that parent directly — the exact invariant the pre-replace guard
    # rejects. CategoriesConfig alone accepts this (both categories exist); only the
    # rule->parent cross-check in _validate_configs_in_memory catches it.
    bad_merchants = MerchantRulesConfig(
        rules=[
            MerchantRule(
                merchant_norm="CINEPLEXX", category="Shopping", patterns=[r"\bCINEPLEXX\b"]
            )
        ]
    )
    bad_categories = CategoriesConfig(
        categories=[
            CategoryRule(name="Shopping", merchants=[]),
            CategoryRule(name="General Shopping", parent="Shopping", merchants=[]),
        ]
    )

    # Calling replace_from_configs DIRECTLY must raise ConfigError from the pre-replace
    # guard, BEFORE any destructive delete/commit. (If the guard block is removed, the
    # rule-on-parent config is accepted and the destructive replace runs — no raise.)
    with pytest.raises(ConfigError, match="parent"):
        repo.replace_from_configs(bad_merchants, bad_categories)

    # The seeded good config must be COMPLETELY intact — the destructive replace never ran.
    with session_scope(db_path) as session:
        post_categories = sorted(row.name for row in session.query(CategoryConfigDB).all())
        post_merchants = sorted(row.merchant_norm for row in session.query(MerchantRuleDB).all())
    assert post_categories == seeded_categories, (
        "replace_from_configs mutated the category config despite a rule-on-parent "
        f"violation; guard did not fire pre-replace. before={seeded_categories} "
        f"after={post_categories}"
    )
    assert post_merchants == seeded_merchants, (
        "replace_from_configs mutated the merchant rules despite a rule-on-parent violation."
    )


pytestmark = pytest.mark.usefixtures("fictional_adapter_metadata")
