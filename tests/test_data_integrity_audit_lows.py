"""Tests for four data-integrity fixes from the 2026-06 audit.

Fix 1 (BEHAVIOR CHANGE): 'reviewed_as_is' suppresses the review prompt only — it must
  NOT set merchant_confidence/category_confidence to 'manual'. Confidence must stay
  auto-derived so future rule passes can update the row.  The review queue is already
  gated by the ``status`` field: reviewed_as_is rows have a distinct status and are
  excluded by pending_review_count / the UI active_rows filter without needing any
  extra column.
  'resolved' (user actually overrode merchant/category) must still set 'manual' (unchanged).

Fix 2: replace_statement_rows must delete transaction_treatments/transaction_offsets for
  the txn_ids being removed, and AdjustmentDetectionService.validate() must flag any
  pre-existing orphaned rows.

Fix 3: replace_statement_rows must delete NULL-source_path transfers that belong to the
  statement being replaced (matched by institution+account+txn_id overlap), but must NOT
  delete NULL-source_path transfers from unrelated statements.

Fix 4: _period_excluded_income_total must filter include_in_cashflow=True,
  cashflow_type='income', amount_signed>0 so it composes correctly with gross_income
  regardless of classifier errors.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from ledger.adjustments import AdjustmentDetectionService
from ledger.import_review import (
    apply_review_decisions,
    get_review_rows,
    pending_review_count,
    save_review_rows,
    seed_review_candidates,
)
from ledger.storage.database import (
    TransactionDB,
    TransactionOffsetDB,
    TransactionTreatmentDB,
    TransferDB,
    init_database,
    session_scope,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _temporal(posting_date: date) -> dict:
    return {
        "day_of_week": posting_date.weekday(),
        "day_of_month": posting_date.day,
        "week_of_year": posting_date.isocalendar()[1],
        "is_weekend": posting_date.weekday() >= 5,
        "is_month_start": posting_date.day <= 3,
        "is_month_end": posting_date.day >= 28,
    }


def _insert_txn(
    db_path: Path,
    txn_id: str,
    *,
    amount: str = "-50.00",
    merchant: str = "MERCHANT",
    category: str = "Other",
    cashflow_type: str = "expense",
    include_in_cashflow: bool = True,
    source_path: str = "2026-01/harbor-chequing.csv",
    institution: str = "harbor",
    account: str = "chequing-1234",
    merchant_confidence: str = "low",
    category_confidence: str = "low",
    description: str | None = None,
) -> None:
    posted = date(2026, 1, 15)
    value = Decimal(amount)
    with session_scope(db_path) as session:
        session.merge(
            TransactionDB(
                txn_id=txn_id,
                posting_date=posted,
                transaction_date=posted,
                institution=institution,
                account=account,
                account_type="Chequing",
                flow="income" if value > 0 else "expense",
                cashflow_type=cashflow_type,
                include_in_cashflow=include_in_cashflow,
                currency="CAD",
                amount_raw=abs(value),
                amount_signed=value,
                description=description or merchant,
                merchant=merchant,
                category=category,
                merchant_confidence=merchant_confidence,
                category_confidence=category_confidence,
                exclude_from_spending=False,
                merchant_raw=merchant,
                category_raw="",
                source_file="statement.csv",
                source_path=source_path,
                source_period="2026-01",
                source_row=1,
                imported_at=_now(),
                parser_version="test",
                txn_fingerprint=f"fp-{txn_id}",
                **_temporal(posted),
            )
        )


def _seed_review(
    db_path: Path,
    key: str,
    txn_ids: list[str],
    status: str = "pending",
) -> None:
    seed_review_candidates(
        db_path,
        [
            {
                "key": key,
                "merchant": "MERCHANT",
                "current_category": "Other",
                "txn_ids": txn_ids,
                "examples": [{"description": "MERCHANT"}],
                "count": len(txn_ids),
                "total_abs_amount": Decimal("50.00"),
            }
        ],
    )
    if status != "pending":
        rows = get_review_rows(db_path)
        for row in rows:
            if row["review_key"] == key:
                row["status"] = status
        save_review_rows(db_path, rows)


def _seed_runtime_categories(db_path: Path, *category_names: str) -> None:
    """Seed the authoritative category choices a valid review source carries."""
    from ledger.config.repository import SQLiteConfigRepository
    from ledger.config.schemas import CategoriesConfig, CategoryRule, MerchantRulesConfig

    SQLiteConfigRepository(db_path).replace_from_configs(
        MerchantRulesConfig(version="test"),
        CategoriesConfig(
            version="test",
            default_category="Other",
            categories=[
                CategoryRule(name="Other"),
                *(CategoryRule(name=name) for name in category_names),
            ],
        ),
    )


# =============================================================================
# Fix 1 — reviewed_as_is suppresses prompt only, does NOT freeze confidence
# =============================================================================


class TestReviewedAsIsSuppressesPromptOnly:
    """reviewed_as_is must NOT set merchant_confidence/category_confidence to 'manual'.
    The row's confidence stays auto (low/rule/etc.) so future rule passes can update it.
    The review queue is gated by status: reviewed_as_is rows have status='reviewed_as_is'
    which is excluded from pending_review_count and the UI active_rows filter — no extra
    column is required.
    """

    def test_reviewed_as_is_does_not_set_manual_confidence(self, tmp_path: Path) -> None:
        """After apply_review_decisions with reviewed_as_is, confidence must NOT be 'manual'."""
        db_path = tmp_path / "ledger.db"
        init_database(db_path)
        _insert_txn(db_path, "txn-asis-1", merchant_confidence="low", category_confidence="low")

        _seed_review(db_path, "merchant:MERCHANT", ["txn-asis-1"], status="reviewed_as_is")
        apply_review_decisions(db_path)

        with closing(sqlite3.connect(db_path)) as conn:
            row = conn.execute(
                "select merchant_confidence, category_confidence from transactions where txn_id = 'txn-asis-1'"
            ).fetchone()

        merchant_conf, category_conf = row
        assert merchant_conf != "manual", (
            f"reviewed_as_is must NOT freeze merchant_confidence to 'manual'; got {merchant_conf!r}"
        )
        assert category_conf != "manual", (
            f"reviewed_as_is must NOT freeze category_confidence to 'manual'; got {category_conf!r}"
        )

    def test_reviewed_as_is_row_no_longer_appears_in_active_review_queue(
        self, tmp_path: Path
    ) -> None:
        """A reviewed_as_is row must be excluded from the queue by its status value alone.

        pending_review_count filters on status='pending' (or resolved-incomplete); a row
        with status='reviewed_as_is' is therefore never counted — no extra column needed.
        This test verifies: seed as pending → apply → still 0 pending.
        """
        db_path = tmp_path / "ledger.db"
        init_database(db_path)
        _insert_txn(db_path, "txn-asis-2")

        _seed_review(db_path, "merchant:MERCHANT2", ["txn-asis-2"], status="reviewed_as_is")
        # Before apply: the row has status='reviewed_as_is' which already excludes it.
        assert pending_review_count(db_path) == 0, (
            "A reviewed_as_is row must not contribute to pending_review_count before apply"
        )

        apply_review_decisions(db_path)

        # After apply: still excluded — status='reviewed_as_is' is preserved.
        assert pending_review_count(db_path) == 0, (
            "pending_review_count must remain 0 after apply_review_decisions for reviewed_as_is"
        )

    def test_future_rule_pass_can_update_reviewed_as_is_row(self, tmp_path: Path) -> None:
        """A row that was reviewed_as_is must still be updatable by apply_all (confidence != manual)."""
        from ledger.config.repository import SQLiteConfigRepository
        from ledger.config.schemas import MerchantRule, MerchantRulesConfig
        from ledger.rules.service import RuleApplicationService

        db_path = tmp_path / "ledger.db"
        init_database(db_path)
        _insert_txn(db_path, "txn-asis-3", merchant="SYNTHETIC ACORN", category="Other")

        _seed_review(db_path, "merchant:SYNTHETIC ACORN", ["txn-asis-3"], status="reviewed_as_is")
        apply_review_decisions(db_path)

        # Verify not frozen first
        with closing(sqlite3.connect(db_path)) as conn:
            row = conn.execute(
                "select merchant_confidence, category_confidence from transactions where txn_id = 'txn-asis-3'"
            ).fetchone()
        assert row[0] != "manual" and row[1] != "manual", (
            "Pre-condition: must not be manual after reviewed_as_is"
        )

        # Now apply a rule that updates SYNTHETIC ACORN → Dining
        repo = SQLiteConfigRepository(db_path)
        rules_config = MerchantRulesConfig(
            version="test",
            rules=[
                MerchantRule(
                    merchant_norm="SYNTHETIC ACORN",
                    category="Dining",
                    patterns=[r"(?i)SYNTHETIC\s+ACORN"],
                )
            ],
        )
        from ledger.config.schemas import CategoriesConfig, CategoryRule

        cats_config = CategoriesConfig(
            version="test",
            default_category="Other",
            categories=[CategoryRule(name="Dining", merchants=["SYNTHETIC ACORN"])],
        )
        repo.replace_from_configs(rules_config, cats_config)
        svc = RuleApplicationService(db_path)
        svc.apply_all(refresh_analytics=False)

        with closing(sqlite3.connect(db_path)) as conn:
            updated = conn.execute(
                "select category, category_confidence from transactions where txn_id = 'txn-asis-3'"
            ).fetchone()
        assert updated[0] == "Dining", (
            f"Future rule pass must be able to update a reviewed_as_is row; category stayed {updated[0]!r}"
        )

    def test_resolved_decision_still_sets_manual_confidence(self, tmp_path: Path) -> None:
        """The 'resolved' path must still set manual confidence (unchanged behavior)."""
        db_path = tmp_path / "ledger.db"
        init_database(db_path)
        _insert_txn(db_path, "txn-resolved-1")
        _seed_runtime_categories(db_path, "Dining")

        _seed_review(db_path, "merchant:RESOLVE_ME", ["txn-resolved-1"])
        rows = get_review_rows(db_path)
        rows[0]["status"] = "resolved"
        rows[0]["merchant"] = "RESOLVE_ME"
        rows[0]["category"] = "Dining"
        save_review_rows(db_path, rows)
        apply_review_decisions(db_path)

        with closing(sqlite3.connect(db_path)) as conn:
            row = conn.execute(
                "select merchant, category, merchant_confidence, category_confidence "
                "from transactions where txn_id = 'txn-resolved-1'"
            ).fetchone()
        merchant, category, merchant_conf, category_conf = row
        assert merchant == "RESOLVE_ME"
        assert category == "Dining"
        assert merchant_conf == "manual", "resolved must still set manual confidence"
        assert category_conf == "manual", "resolved must still set manual confidence"

    def test_resolved_row_cannot_be_updated_by_future_rule_pass(self, tmp_path: Path) -> None:
        """'resolved' rows must have manual confidence and therefore be skipped by apply_all."""
        from ledger.config.repository import SQLiteConfigRepository
        from ledger.config.schemas import (
            CategoriesConfig,
            CategoryRule,
            MerchantRule,
            MerchantRulesConfig,
        )
        from ledger.rules.service import RuleApplicationService

        db_path = tmp_path / "ledger.db"
        init_database(db_path)
        _insert_txn(db_path, "txn-resolved-2", merchant="FIXED_MERCHANT", category="Other")
        _seed_runtime_categories(db_path, "Shopping")

        _seed_review(db_path, "merchant:FIXED_MERCHANT", ["txn-resolved-2"])
        rows = get_review_rows(db_path)
        rows[0]["status"] = "resolved"
        rows[0]["merchant"] = "FIXED_MERCHANT"
        rows[0]["category"] = "Shopping"
        save_review_rows(db_path, rows)
        apply_review_decisions(db_path)

        # Apply a contradictory rule
        repo = SQLiteConfigRepository(db_path)
        rules_config = MerchantRulesConfig(
            version="test",
            rules=[
                MerchantRule(
                    merchant_norm="FIXED_MERCHANT",
                    category="Electronics",
                    patterns=[r"(?i)FIXED_MERCHANT"],
                )
            ],
        )
        cats_config = CategoriesConfig(
            version="test",
            default_category="Other",
            categories=[
                CategoryRule(name="Shopping", merchants=["FIXED_MERCHANT"]),
                CategoryRule(name="Electronics", merchants=[]),
            ],
        )
        repo.replace_from_configs(rules_config, cats_config)
        svc = RuleApplicationService(db_path)
        svc.apply_all(refresh_analytics=False)

        with closing(sqlite3.connect(db_path)) as conn:
            row = conn.execute(
                "select category from transactions where txn_id = 'txn-resolved-2'"
            ).fetchone()
        assert row[0] == "Shopping", (
            f"Resolved (manual) row must not be updated by rule pass; category changed to {row[0]!r}"
        )

    def test_reviewed_as_is_status_excludes_row_without_extra_column(self, tmp_path: Path) -> None:
        """import_review must NOT have a 'suppressed' column — status alone gates the queue.

        The reviewed_as_is status value is sufficient: pending_review_count filters on
        status='pending' (or resolved-incomplete), so reviewed_as_is rows are inherently
        excluded. This test confirms the column is absent and the exclusion still holds.
        """
        from ledger.storage.migrations import run_migrations

        db_path = tmp_path / "ledger.db"
        init_database(db_path)
        run_migrations(db_path)

        with closing(sqlite3.connect(db_path)) as conn:
            cols = {row[1] for row in conn.execute("PRAGMA table_info(import_review)").fetchall()}
        assert "suppressed" not in cols, (
            "import_review must NOT have a 'suppressed' column — it is dead schema"
        )

        # Seed a reviewed_as_is row and confirm it does not count as pending
        _insert_txn(db_path, "txn-no-suppressed-col")
        _seed_review(
            db_path, "merchant:NO_SUPP", ["txn-no-suppressed-col"], status="reviewed_as_is"
        )
        assert pending_review_count(db_path) == 0, (
            "reviewed_as_is row must be excluded from pending_review_count by status alone"
        )


# =============================================================================
# Fix 2 — orphan treatments/offsets after txn_id-changing re-import
# =============================================================================


class TestOrphanTreatmentsOffsets:
    """replace_statement_rows must clean up treatments/offsets for removed txn_ids.
    validate() must flag any pre-existing orphans it finds."""

    def _make_treatment(self, db_path: Path, txn_id: str) -> None:
        with session_scope(db_path) as session:
            session.add(
                TransactionTreatmentDB(
                    txn_id=txn_id,
                    treatment_type="reward_rebate",
                    status="auto_applied",
                    confidence=Decimal("0.95"),
                    netting_policy="exclude_from_income",
                    target_category=None,
                    evidence={},
                    detected_at=_now(),
                    source="deterministic_v1",
                )
            )

    def _make_offset(self, db_path: Path, credit_id: str, debit_id: str) -> None:
        with session_scope(db_path) as session:
            session.add(
                TransactionOffsetDB(
                    credit_txn_id=credit_id,
                    debit_txn_id=debit_id,
                    offset_amount=Decimal("50.00"),
                    confidence=Decimal("0.95"),
                    status="auto_applied",
                    evidence={},
                    detected_at=_now(),
                    source="deterministic_v1",
                )
            )

    def test_replace_statement_rows_deletes_treatments_for_removed_txn_ids(
        self, tmp_path: Path
    ) -> None:
        """When replace_statement_rows removes a txn, its treatment must be deleted too."""
        from ledger.imports.pdf.processing import replace_statement_rows
        from ledger.processing import ProcessingResult

        db_path = tmp_path / "ledger.db"
        init_database(db_path)

        # Insert a transaction and its treatment
        _insert_txn(db_path, "old-txn-1", source_path="2026-01/harbor-chequing.csv")
        self._make_treatment(db_path, "old-txn-1")

        # Re-import the same source_path but with a different txn_id (simulates txn_id change)
        result = ProcessingResult(total_scanned=1)
        # new txn with new id but same source_path

        # replace_statement_rows: old-txn-1 has source_path "2026-01/harbor-chequing.csv"
        # we call replace with a result that no longer includes "old-txn-1"
        # → old-txn-1 must be deleted from transactions AND its treatment must be deleted
        result = ProcessingResult(total_scanned=1)
        result.transactions = []  # empty → old-txn-1 will be removed by replace
        result.transfers = []

        # Manually construct a minimal stub ProcessingResult that replace_statement_rows accepts.
        # We need source_path on the result to know which paths to scope.
        # Since result.transactions is empty, the source_paths set will be empty → early return.
        # Instead we manually set source_path on the transactions we're "importing".
        class MinimalTxn:
            source_path = "2026-01/harbor-chequing.csv"
            txn_id = "new-txn-1"

        result.transactions = [MinimalTxn()]  # type: ignore[list-item]
        result.transfers = []

        replace_statement_rows(db_path, result)

        with session_scope(db_path) as session:
            remaining = session.query(TransactionTreatmentDB).filter_by(txn_id="old-txn-1").count()
        assert remaining == 0, (
            f"replace_statement_rows must delete treatments for removed txn_ids; {remaining} remain"
        )

    def test_replace_statement_rows_deletes_offsets_for_removed_txn_ids(
        self, tmp_path: Path
    ) -> None:
        """When replace_statement_rows removes a txn, offsets referencing it must be deleted."""
        from ledger.imports.pdf.processing import replace_statement_rows
        from ledger.processing import ProcessingResult

        db_path = tmp_path / "ledger.db"
        init_database(db_path)

        _insert_txn(db_path, "old-credit-1", source_path="2026-01/harbor-chequing.csv")
        _insert_txn(
            db_path, "old-debit-1", source_path="2026-01/harbor-chequing.csv", amount="-50.00"
        )
        self._make_offset(db_path, "old-credit-1", "old-debit-1")

        class MinimalTxn:
            source_path = "2026-01/harbor-chequing.csv"
            txn_id = "new-txn-2"

        result = ProcessingResult(total_scanned=1)
        result.transactions = [MinimalTxn()]  # type: ignore[list-item]
        result.transfers = []

        replace_statement_rows(db_path, result)

        with session_scope(db_path) as session:
            # Offsets referencing either old-credit-1 or old-debit-1 must be gone
            remaining = (
                session.query(TransactionOffsetDB)
                .filter(
                    (TransactionOffsetDB.credit_txn_id == "old-credit-1")
                    | (TransactionOffsetDB.debit_txn_id == "old-debit-1")
                )
                .count()
            )
        assert remaining == 0, (
            f"replace_statement_rows must delete offsets referencing removed txn_ids; {remaining} remain"
        )

    def test_validate_flags_orphaned_treatment(self, tmp_path: Path) -> None:
        """validate() must flag a treatment whose txn_id has no matching transaction."""
        db_path = tmp_path / "ledger.db"
        init_database(db_path)

        # Insert treatment for a txn that doesn't exist
        self._make_treatment(db_path, "ghost-txn-1")

        svc = AdjustmentDetectionService(db_path)
        result = svc.validate()

        assert result.status == "failed", "validate() must fail when orphaned treatments exist"
        assert any(
            "orphan" in e.lower()
            or "ghost" in e.lower()
            or "no matching" in e.lower()
            or "treatment" in e.lower()
            for e in result.errors
        ), f"validate() must report orphaned treatment; errors={result.errors}"

    def test_validate_flags_orphaned_offset(self, tmp_path: Path) -> None:
        """validate() must flag an offset whose txn_ids have no matching transactions."""
        db_path = tmp_path / "ledger.db"
        init_database(db_path)

        # Insert offset with both txn_ids not in transactions table
        self._make_offset(db_path, "ghost-credit", "ghost-debit")

        svc = AdjustmentDetectionService(db_path)
        result = svc.validate()

        assert result.status == "failed", "validate() must fail when orphaned offsets exist"
        assert any("orphan" in e.lower() or "offset" in e.lower() for e in result.errors), (
            f"validate() must report orphaned offsets; errors={result.errors}"
        )

    def test_validate_passes_when_no_orphans(self, tmp_path: Path) -> None:
        """validate() must succeed when all treatments/offsets have matching transactions."""
        db_path = tmp_path / "ledger.db"
        init_database(db_path)

        _insert_txn(db_path, "real-credit", amount="100.00")
        _insert_txn(db_path, "real-debit", amount="-100.00")
        self._make_treatment(db_path, "real-credit")
        self._make_offset(db_path, "real-credit", "real-debit")

        svc = AdjustmentDetectionService(db_path)
        result = svc.validate()

        assert result.status == "success", (
            f"validate() must pass when no orphans; errors={result.errors}"
        )


# =============================================================================
# Fix 3 — NULL-source_path transfers deleted on replace
# =============================================================================


def _insert_transfer(
    db_path: Path,
    txn_id: str,
    *,
    source_path: str | None,
    institution: str = "harbor",
    account: str = "chequing-1234",
) -> None:
    posted = date(2026, 1, 15)
    with session_scope(db_path) as session:
        session.merge(
            TransferDB(
                txn_id=txn_id,
                posting_date=posted,
                institution=institution,
                account=account,
                account_type="Chequing",
                flow="debit",
                amount_raw=Decimal("500.00"),
                description="Transfer",
                source_file="statement.csv",
                source_path=source_path,
                imported_at=_now(),
            )
        )


class TestNullSourcePathTransferDeletion:
    """replace_statement_rows must also delete legacy NULL-source_path transfers that
    belong to the statement being replaced, without touching transfers from other statements."""

    def test_null_source_path_transfer_deleted_when_txn_id_in_replaced_set(
        self, tmp_path: Path
    ) -> None:
        """A NULL-source_path transfer whose txn_id IS in the set of txn_ids being replaced
        must be deleted on re-import (no duplicate)."""
        from ledger.imports.pdf.processing import replace_statement_rows
        from ledger.processing import ProcessingResult

        db_path = tmp_path / "ledger.db"
        init_database(db_path)

        # Insert a transfer with NULL source_path (legacy pre-migration row)
        _insert_transfer(
            db_path,
            "legacy-transfer-1",
            source_path=None,
            institution="harbor",
            account="chequing-1234",
        )

        # The re-import covers the same institution+account, and produces a new txn_id
        # for the same statement period. legacy-transfer-1 is in the "old" set.
        class MinimalTxn:
            source_path = "2026-01/harbor-chequing.csv"
            txn_id = "new-transfer-1"
            institution = "harbor"
            account = "chequing-1234"
            posting_date = date(2026, 1, 15)  # same period as the legacy row → in range

        result = ProcessingResult(total_scanned=1)
        result.transactions = []
        result.transfers = [MinimalTxn()]  # type: ignore[list-item]

        # We need the old transfer to be in scope. Since source_path is NULL, the existing
        # filter (source_path.in_(source_paths)) misses it. The fix must also scope on
        # institution+account+NULL source_path for the txn_ids being replaced.
        # The predicate: DELETE FROM transfers WHERE
        #   source_path IN (current source_paths)
        #   OR (source_path IS NULL AND institution = ? AND account = ? AND txn_id NOT IN new_ids)
        replace_statement_rows(db_path, result)

        with session_scope(db_path) as session:
            remaining = session.query(TransferDB).filter_by(txn_id="legacy-transfer-1").count()
        assert remaining == 0, (
            "NULL-source_path transfer belonging to the replaced statement must be deleted on re-import"
        )

    def test_null_source_path_transfer_from_different_statement_not_touched(
        self, tmp_path: Path
    ) -> None:
        """A NULL-source_path transfer from a DIFFERENT institution/account must NOT be deleted."""
        from ledger.imports.pdf.processing import replace_statement_rows
        from ledger.processing import ProcessingResult

        db_path = tmp_path / "ledger.db"
        init_database(db_path)

        # This transfer belongs to a different account
        _insert_transfer(
            db_path,
            "other-transfer-1",
            source_path=None,
            institution="harbor",
            account="savings-9999",
        )

        class MinimalTxn:
            source_path = "2026-01/harbor-chequing.csv"
            txn_id = "new-transfer-2"
            institution = "harbor"
            account = "chequing-1234"
            posting_date = date(2026, 1, 15)

        result = ProcessingResult(total_scanned=1)
        result.transactions = []
        result.transfers = [MinimalTxn()]  # type: ignore[list-item]

        replace_statement_rows(db_path, result)

        with session_scope(db_path) as session:
            remaining = session.query(TransferDB).filter_by(txn_id="other-transfer-1").count()
        assert remaining == 1, (
            "NULL-source_path transfer from a DIFFERENT statement must NOT be deleted"
        )

    def test_historical_null_source_path_transfer_outside_statement_period_survives(
        self, tmp_path: Path
    ) -> None:
        """The data-loss regression: in a DB where EVERY transfer has NULL source_path,
        importing one new statement for an account must NOT delete that account's
        transfers from EARLIER periods (outside the new statement's date coverage)."""
        from ledger.imports.pdf.processing import replace_statement_rows
        from ledger.processing import ProcessingResult

        db_path = tmp_path / "ledger.db"
        init_database(db_path)

        # Two legacy NULL-source_path transfers on the SAME account: one old (2025-06),
        # one in the new statement's period (2026-01). _insert_transfer posts at 2026-01-15,
        # so override the old one's date directly.
        _insert_transfer(db_path, "hist-2025", source_path=None, account="chequing-1234")
        with session_scope(db_path) as session:
            session.query(TransferDB).filter_by(txn_id="hist-2025").update(
                {"posting_date": date(2025, 6, 15)}
            )
        _insert_transfer(db_path, "stale-2026", source_path=None, account="chequing-1234")

        class MinimalTxn:
            source_path = "2026-01/harbor-chequing.csv"
            txn_id = "new-2026"
            institution = "harbor"
            account = "chequing-1234"
            posting_date = date(2026, 1, 15)

        result = ProcessingResult(total_scanned=1)
        result.transactions = []
        result.transfers = [MinimalTxn()]  # type: ignore[list-item]

        replace_statement_rows(db_path, result)

        with session_scope(db_path) as session:
            ids = {r.txn_id for r in session.query(TransferDB).all()}
        assert "hist-2025" in ids, (
            "historical transfer outside the new statement period was wrongly deleted (data loss)"
        )
        assert "stale-2026" not in ids, (
            "an in-period stale legacy duplicate should still be replaced"
        )


# =============================================================================
# Fix 4 — _period_excluded_income_total mirrors gross_income filters
# =============================================================================


class TestExcludedIncomeTotalFilters:
    """_period_excluded_income_total must filter on include_in_cashflow, cashflow_type='income',
    and amount_signed > 0, mirroring the gross_income query."""

    def test_excluded_income_ignores_row_excluded_from_cashflow(self, tmp_path: Path) -> None:
        """A reward credit with include_in_cashflow=False must not reduce adjusted_income."""
        db_path = tmp_path / "ledger.db"
        init_database(db_path)

        # Real income
        _insert_txn(
            db_path,
            "payroll-1",
            amount="1000.00",
            merchant="PAYROLL",
            category="Income",
            cashflow_type="income",
            description="Cashback reward",  # would match reward pattern
            include_in_cashflow=False,  # excluded from cashflow — must NOT be touched
        )
        # A separate payroll that IS included
        _insert_txn(
            db_path,
            "payroll-2",
            amount="2000.00",
            merchant="EMPLOYER",
            category="Income",
            cashflow_type="income",
            include_in_cashflow=True,
        )

        svc = AdjustmentDetectionService(db_path)
        svc.detect(execute=True)
        summary = svc.true_cost_summary(date(2026, 1, 1), date(2026, 1, 31))

        # gross_income only includes payroll-2 (payroll-1 has include_in_cashflow=False)
        assert summary.gross_income == Decimal("2000.00"), (
            f"gross_income must exclude include_in_cashflow=False; got {summary.gross_income}"
        )
        # adjusted_income must not be reduced by the excluded row's reward treatment
        assert summary.adjusted_income == Decimal("2000.00"), (
            f"adjusted_income must ignore rows excluded from cashflow; got {summary.adjusted_income}"
        )

    def test_excluded_income_ignores_non_income_cashflow_type(self, tmp_path: Path) -> None:
        """A row with cashflow_type != 'income' must not reduce adjusted_income even if
        the reward pattern matches and netting_policy is exclude_from_income."""
        db_path = tmp_path / "ledger.db"
        init_database(db_path)

        # Real income
        _insert_txn(
            db_path,
            "payroll-3",
            amount="3000.00",
            merchant="EMPLOYER",
            category="Income",
            cashflow_type="income",
            include_in_cashflow=True,
        )
        # Reward credit mis-classified as expense (cashflow_type wrong)
        _insert_txn(
            db_path,
            "reward-expense",
            amount="50.00",
            merchant="CEDAR",
            category="Utilities",
            cashflow_type="expense",  # wrong type
            description="Cashback reward",
            include_in_cashflow=True,
        )

        svc = AdjustmentDetectionService(db_path)
        svc.detect(execute=True)
        summary = svc.true_cost_summary(date(2026, 1, 1), date(2026, 1, 31))

        # gross_income: only payroll-3 (cashflow_type='income')
        assert summary.gross_income == Decimal("3000.00")
        # adjusted_income must not deduct the reward treatment for reward-expense
        # (it is cashflow_type=expense, not income, so _period_excluded_income_total
        #  must not include it)
        assert summary.adjusted_income == Decimal("3000.00"), (
            f"_period_excluded_income_total must filter cashflow_type='income'; "
            f"got adjusted_income={summary.adjusted_income}"
        )

    def test_excluded_income_ignores_negative_amount(self, tmp_path: Path) -> None:
        """A treatment with netting_policy=exclude_from_income on a negative amount_signed row
        must not be summed (amount_signed > 0 filter required)."""
        db_path = tmp_path / "ledger.db"
        init_database(db_path)

        _insert_txn(
            db_path,
            "payroll-4",
            amount="5000.00",
            merchant="EMPLOYER",
            category="Income",
            cashflow_type="income",
        )
        # Negative row mistakenly tagged with exclude_from_income treatment
        _insert_txn(
            db_path,
            "expense-1",
            amount="-100.00",
            merchant="STORE",
            category="Shopping",
            cashflow_type="income",  # mis-classified
            description="Cashback reward",
            include_in_cashflow=True,
        )

        svc = AdjustmentDetectionService(db_path)
        svc.detect(execute=True)
        summary = svc.true_cost_summary(date(2026, 1, 1), date(2026, 1, 31))

        # adjusted_income must not be artificially deflated by the negative amount
        assert summary.adjusted_income >= Decimal("4900.00"), (
            f"adjusted_income must not be reduced by negative amount_signed rows "
            f"in _period_excluded_income_total; got {summary.adjusted_income}"
        )

    def test_normal_reward_exclusion_still_works(self, tmp_path: Path) -> None:
        """The normal exclude_from_income case (reward credit, include_in_cashflow=True,
        cashflow_type=income, amount_signed > 0) must still reduce adjusted_income."""
        db_path = tmp_path / "ledger.db"
        init_database(db_path)

        _insert_txn(
            db_path,
            "payroll-5",
            amount="1000.00",
            merchant="EMPLOYER",
            category="Income",
            cashflow_type="income",
        )
        _insert_txn(
            db_path,
            "reward-1",
            amount="25.00",
            merchant="CEDAR",
            category="Utilities",
            cashflow_type="income",
            description="Cashback reward",
            include_in_cashflow=True,
        )

        svc = AdjustmentDetectionService(db_path)
        svc.detect(execute=True)
        summary = svc.true_cost_summary(date(2026, 1, 1), date(2026, 1, 31))

        assert summary.gross_income == Decimal("1025.00")
        assert summary.adjusted_income == Decimal("1000.00"), (
            f"Normal reward exclusion must still reduce adjusted_income; got {summary.adjusted_income}"
        )
