"""Tests for runtime rule application."""

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from ledger.config.repository import SQLiteConfigRepository
from ledger.config.schemas import CategoriesConfig, CategoryRule, MerchantRule, MerchantRulesConfig
from ledger.rules import RuleApplicationService
from ledger.storage.database import RuleApplicationRunDB, TransactionDB, session_scope


def _temporal_fields(posting_date: date) -> dict:
    return {
        "day_of_week": posting_date.weekday(),
        "day_of_month": posting_date.day,
        "week_of_year": posting_date.isocalendar()[1],
        "is_weekend": posting_date.weekday() >= 5,
        "is_month_start": posting_date.day <= 3,
        "is_month_end": posting_date.day >= 28,
    }


def _insert_healthy_planet(db_path: Path) -> None:
    posted = date(2026, 5, 1)
    with session_scope(db_path) as session:
        session.add(
            TransactionDB(
                txn_id="hp001",
                posting_date=posted,
                transaction_date=posted,
                institution="test-card",
                account="123456789",
                account_type="credit",
                flow="expense",
                cashflow_type="expense",
                include_in_cashflow=True,
                currency="CAD",
                amount_raw=Decimal("42.15"),
                amount_signed=Decimal("-42.15"),
                description="HEALTHY PLANET STORE 10",
                merchant="HEALTHY PLANET",
                category="Health & Wellness",
                merchant_confidence="rule",
                category_confidence="rule",
                exclude_from_spending=False,
                merchant_raw="HEALTHY PLANET STORE 10",
                category_raw="",
                source_file="statement.csv",
                source_path="/private/source/statement.csv",
                source_period="2026-05",
                source_row=1,
                imported_at=datetime.now(UTC).replace(tzinfo=None),
                parser_version="test",
                txn_fingerprint="fingerprint",
                **_temporal_fields(posted),
            )
        )


def _seed_acme_rule(db_path: Path) -> None:
    """Seed an ACME->Groceries merchant rule and categories."""
    repo = SQLiteConfigRepository(db_path)
    categories = CategoriesConfig(
        version="test",
        categories=[
            CategoryRule(name="Groceries", merchants=[]),
            CategoryRule(name="Dining", merchants=[]),
        ],
    )
    repo.replace_from_configs(
        MerchantRulesConfig(
            version="test",
            rules=[
                MerchantRule(
                    merchant_norm="ACME MARKET",
                    category="Groceries",
                    patterns=["ACME MARKET"],
                )
            ],
        ),
        categories,
    )


def _insert_acme_txn(db_path: Path, txn_id: str = "acme001") -> None:
    posted = date(2026, 5, 10)
    with session_scope(db_path) as session:
        session.add(
            TransactionDB(
                txn_id=txn_id,
                posting_date=posted,
                transaction_date=posted,
                institution="test-card",
                account="987654321",
                account_type="credit",
                flow="expense",
                cashflow_type="expense",
                include_in_cashflow=True,
                currency="CAD",
                amount_raw=Decimal("15.00"),
                amount_signed=Decimal("-15.00"),
                description="ACME MARKET DOWNTOWN",
                merchant="ACME MARKET",
                category="Groceries",
                merchant_confidence="rule",
                category_confidence="rule",
                exclude_from_spending=False,
                merchant_raw="ACME MARKET DOWNTOWN",
                category_raw="",
                source_file="statement.csv",
                source_path="/private/source/statement.csv",
                source_period="2026-05",
                source_row=2,
                imported_at=datetime.now(UTC).replace(tzinfo=None),
                parser_version="test",
                txn_fingerprint=f"fp-{txn_id}",
                **_temporal_fields(posted),
            )
        )


def test_apply_all_skips_manual_category_override(tmp_path: Path):
    """apply_all must NOT overwrite a manually-set category (M2 fix)."""
    db_path = tmp_path / "ledger.db"
    _seed_acme_rule(db_path)
    _insert_acme_txn(db_path)

    # Manually override this transaction's category to "Dining"
    from ledger.rules.categorization import CategorizationService

    ok = CategorizationService(db_path).set_transaction_category("acme001", "Dining")
    assert ok

    # Confirm the manual override is in place
    with session_scope(db_path) as session:
        row = session.get(TransactionDB, "acme001")
        assert row.category == "Dining"
        assert row.category_confidence == "manual"

    # Now run apply_all — the Groceries rule matches "ACME MARKET" descriptions,
    # but the manual flag must prevent it from overwriting "Dining".
    result = RuleApplicationService(db_path).apply_all(
        source="test",
        refresh_analytics=False,
    )

    assert result.ok
    with session_scope(db_path) as session:
        row = session.get(TransactionDB, "acme001")
        assert row.category == "Dining", (
            f"Manual category was overwritten to {row.category!r}; expected 'Dining'"
        )
        assert row.category_confidence == "manual", (
            f"category_confidence changed to {row.category_confidence!r}; expected 'manual'"
        )


def test_apply_all_still_updates_non_manual_rows(tmp_path: Path):
    """Regression: apply_all must still recategorize rows that are NOT manual."""
    db_path = tmp_path / "ledger.db"
    repo = SQLiteConfigRepository(db_path)
    categories = CategoriesConfig(
        version="test",
        categories=[
            CategoryRule(name="Groceries", merchants=[]),
            CategoryRule(name="Dining", merchants=[]),
        ],
    )
    repo.replace_from_configs(
        MerchantRulesConfig(
            version="test",
            rules=[
                MerchantRule(
                    merchant_norm="ACME MARKET",
                    category="Dining",  # changed from initial Groceries below
                    patterns=["ACME MARKET"],
                )
            ],
        ),
        categories,
    )
    # Insert with stale category "Groceries" and NON-manual confidence
    _insert_acme_txn(db_path)  # has category="Groceries", category_confidence="rule"

    result = RuleApplicationService(db_path).apply_all(
        source="test",
        refresh_analytics=False,
    )

    assert result.ok
    with session_scope(db_path) as session:
        row = session.get(TransactionDB, "acme001")
        assert row.category == "Dining", (
            f"Non-manual row was NOT updated; still has {row.category!r}"
        )
        assert row.category_confidence == "rule"


def test_merchant_profile_category_change_updates_mapping_and_transactions(tmp_path: Path):
    db_path = tmp_path / "ledger.db"
    repo = SQLiteConfigRepository(db_path)
    categories = CategoriesConfig(
        version="test",
        categories=[
            CategoryRule(name="Groceries", merchants=[]),
            CategoryRule(name="Health & Wellness", merchants=[]),
        ],
    )
    repo.replace_from_configs(
        MerchantRulesConfig(
            version="test",
            rules=[
                MerchantRule(
                    merchant_norm="HEALTHY PLANET",
                    category="Health & Wellness",
                    patterns=["HEALTHY PLANET"],
                )
            ],
        ),
        categories,
    )
    _insert_healthy_planet(db_path)

    repo.replace_from_configs(
        MerchantRulesConfig(
            version="test",
            rules=[
                MerchantRule(
                    merchant_norm="HEALTHY PLANET",
                    category="Groceries",
                    patterns=["HEALTHY PLANET"],
                )
            ],
        ),
        categories,
    )

    assert repo.load_categories().build_merchant_map()["HEALTHY PLANET"] == (
        "Groceries",
        False,
    )

    result = RuleApplicationService(db_path).apply_all(
        source="test",
        refresh_analytics=False,
    )

    assert result.ok
    assert result.transactions_updated == 1
    with session_scope(db_path) as session:
        txn = session.get(TransactionDB, "hp001")
        assert txn.category == "Groceries"
        assert txn.merchant_confidence == "rule"
        assert txn.category_confidence == "rule"
        audit = session.query(RuleApplicationRunDB).one()
        assert audit.transactions_updated == 1
        assert audit.details["changed_merchants"] == ["HEALTHY PLANET"]


def test_enrich_applies_rule_category_exclusion(tmp_path: Path, sample_ledger_config):
    """Import-time _enrich must apply the resolved category's exclude_from_spending
    even when the category comes from the merchant RULE (not the merchant->category
    map), matching RuleApplicationService (rules/service.py:103). Otherwise the
    persisted exclude flag diverges and — being MANUAL/RULE — does not self-heal."""
    from ledger.config.schemas import (
        AppConfig,
        CategoriesConfig,
        CategoryRule,
        MerchantRule,
        MerchantRulesConfig,
    )
    from ledger.parsers.base import BaseParser, ParseContext

    merchants = MerchantRulesConfig(
        version="test",
        rules=[
            MerchantRule(
                merchant_norm="GYMSHARE",
                category="Reimbursement",
                patterns=[r"(?i)\bGYMSHARE\b"],
            )
        ],
    )
    # "Reimbursement" is excluded from spending, but GYMSHARE is not listed under any
    # category's merchants, so merchant_to_category has no entry -> _enrich takes the
    # found_cat (rule category) branch, which historically dropped the exclusion.
    categories = CategoriesConfig(
        version="test",
        default_category="Other",
        categories=[
            CategoryRule(
                name="Reimbursement", merchants=["SOMEONE_ELSE"], exclude_from_spending=True
            ),
            CategoryRule(name="Other", merchants=["PLACEHOLDER"]),
        ],
    )
    config = AppConfig(ledger=sample_ledger_config, merchants=merchants, categories=categories)

    class _StubParser(BaseParser):
        FORMAT_NAME = "t"
        INSTITUTION = "t"
        REQUIRED_HEADERS: set[str] = set()

        def parse_row(self, row, row_num):
            return None

    source_file = tmp_path / "file.csv"
    source_file.write_text("x")
    ctx = ParseContext(config=config, source_root=tmp_path, file_path=source_file)
    parser = _StubParser(ctx)
    builder = parser._builder(2)
    parser._enrich(builder, "GYMSHARE", "", "GYMSHARE MONTHLY")

    data = builder.to_dict()
    assert data["category"] == "Reimbursement"
    assert data["exclude_from_spending"] is True
