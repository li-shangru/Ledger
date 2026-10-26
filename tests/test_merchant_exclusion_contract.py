"""Unit G — a resolved merchant DISPLAY NAME is not statement text.

``MerchantRulesConfig.find_merchant`` used to accept every argument as raw statement
text and iterate RULES in the outer loop.  Two defects followed:

1. Rule ordering beat text priority: a broad rule with a low ``sort_order`` won even
   when it matched a lower-priority text and a specific rule matched the raw
   description.
2. Both bulk consumers (``rules/service.py`` and ``import_review.py``) passed the
   ALREADY-RESOLVED display merchant alongside the raw text, so a freshly typed
   merchant name was pattern-matched against the ruleset and silently inherited an
   unrelated rule's ``exclude_from_spending``.

The contract these tests pin: positional ``texts`` are raw statement text matched by
PATTERN in priority order; ``display_name`` is a resolved label matched only by
IDENTITY against ``merchant_norm``, and only when no raw text matched.

The safety invariant is one-directional: a row a rule genuinely excludes must stay
excluded; only an exclusion inherited through pattern-matching a display name goes
away.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from ledger.config.repository import SQLiteConfigRepository
from ledger.config.schemas import (
    CategoriesConfig,
    CategoryRule,
    MerchantRule,
    MerchantRulesConfig,
)
from ledger.core import Transaction
from ledger.import_review import (
    apply_review_decisions,
    get_review_rows,
    save_review_rows,
    seed_review_candidates,
)
from ledger.processing import ProcessingResult
from ledger.rules import RuleApplicationService
from ledger.storage.database import TransactionDB, session_scope
from ledger.storage.writer import DatabaseWriter

# --- shared fixture config -------------------------------------------------
# A BROAD excluding rule ("LUMEN" -> Investing, exclude) and a SPECIFIC
# non-excluding rule ("LM FEES" -> Fees).  The specific rule deliberately sorts
# AFTER the broad one so rule ordering and text priority disagree.

BROAD_EXCLUDING = MerchantRule(
    merchant_norm="LUMEN",
    category="Investing",
    patterns=[r"LUMEN"],
    exclude_from_spending=True,
)
SPECIFIC_SPENDING = MerchantRule(
    merchant_norm="LM FEES",
    category="Fees",
    patterns=[r"LM\s+FEE"],
    exclude_from_spending=False,
)

MERCHANTS = MerchantRulesConfig(version="test", rules=[BROAD_EXCLUDING, SPECIFIC_SPENDING])

# Neither category excludes, so the merchant RULE flag is the only source of
# exclusion in these tests — an inherited flag cannot hide behind a category.
CATEGORIES = CategoriesConfig(
    version="test",
    default_category="Other",
    categories=[
        CategoryRule(name="Investing", merchants=["LUMEN"], exclude_from_spending=False),
        CategoryRule(name="Fees", merchants=["LM FEES"], exclude_from_spending=False),
    ],
)


# ===========================================================================
# 1. find_merchant contract
# ===========================================================================


def test_earlier_text_beats_earlier_rule():
    """Text priority outranks rule ordering.

    The broad rule sorts first and matches the SECOND text; the specific rule sorts
    last and matches the FIRST.  The first text must win — otherwise a low
    ``sort_order`` silently overrides the most authoritative statement field.
    """
    merchant, category, exclude = MERCHANTS.find_merchant(
        "LM FEE 0725",  # matches SPECIFIC_SPENDING only
        "LUMEN INVEST",  # matches BROAD_EXCLUDING only
    )

    assert (merchant, category, exclude) == ("LM FEES", "Fees", False)


def test_display_name_is_never_pattern_matched():
    """A typed display name that merely CONTAINS another rule's pattern must not
    match it — that is the exclusion leak (a 'LUMEN FEES' label inheriting
    exclude_from_spending from the broad LUMEN rule)."""
    assert MERCHANTS.find_merchant(
        "SETTLEMENT 0725",  # raw text matches nothing
        display_name="LUMEN FEES",
    ) == ("", "Other", False)


def test_display_name_matches_a_configured_merchant_by_identity():
    """Naming an EXISTING merchant still resolves to that merchant's own rule, so a
    row the config genuinely excludes keeps its exclusion."""
    assert MERCHANTS.find_merchant("SETTLEMENT 0725", display_name="LUMEN") == (
        "LUMEN",
        "Investing",
        True,
    )
    # Identity is case- and whitespace-insensitive, not byte-exact.
    assert MERCHANTS.find_merchant("SETTLEMENT 0725", display_name="  lumen ") == (
        "LUMEN",
        "Investing",
        True,
    )


def test_raw_text_match_wins_over_display_name():
    """Raw statement text is evidence; a display name is a label. Evidence wins."""
    assert MERCHANTS.find_merchant("LM FEE 0725", display_name="LUMEN") == (
        "LM FEES",
        "Fees",
        False,
    )


def test_empty_and_absent_display_name_are_inert():
    assert MERCHANTS.find_merchant("SETTLEMENT", display_name="") == ("", "Other", False)
    assert MERCHANTS.find_merchant("SETTLEMENT") == ("", "Other", False)


def test_single_raw_text_still_pattern_matches():
    """Unchanged contract for the parser/preview callers that pass one raw text."""
    assert MERCHANTS.find_merchant("LUMEN TRADE INC") == (
        "LUMEN",
        "Investing",
        True,
    )


# ===========================================================================
# 2. rules-apply (src/ledger/rules/service.py)
# ===========================================================================


def _txn(
    txn_id: str,
    *,
    description: str,
    merchant_raw: str,
    merchant: str,
    category: str = "Other",
    exclude: bool = False,
) -> Transaction:
    return Transaction(
        txn_id=txn_id,
        txn_fingerprint=f"fp_{txn_id}",
        posting_date=date(2025, 4, 10),
        transaction_date=date(2025, 4, 10),
        institution="harbor",
        account="harbor-chequing",
        flow="expense",
        cashflow_type="expense",
        include_in_cashflow=True,
        currency="CAD",
        amount_raw=Decimal("40.00"),
        amount_signed=Decimal("-40.00"),
        description=description,
        merchant=merchant,
        category=category,
        exclude_from_spending=exclude,
        merchant_raw=merchant_raw,
        category_raw="",
        source_file="test.csv",
        source_path="/test/test.csv",
        source_period="2025-04",
        source_row=1,
        imported_at=date(2025, 4, 15),
        parser_version="test",
    )


def _seed(db_path: Path, txns: list[Transaction]) -> None:
    DatabaseWriter(db_path).write_result(
        ProcessingResult(
            transactions=txns,
            transfers=[],
            exceptions=[],
            total_scanned=len(txns),
            duplicates_removed=0,
            collisions=0,
        ),
        Path("/test"),
        "test",
    )
    SQLiteConfigRepository(db_path).replace_from_configs(MERCHANTS, CATEGORIES)


def _row(db_path: Path, txn_id: str) -> dict:
    with session_scope(db_path) as session:
        row = session.query(TransactionDB).filter_by(txn_id=txn_id).one()
        return {
            "merchant": row.merchant,
            "category": row.category,
            "exclude": bool(row.exclude_from_spending),
        }


def test_rules_apply_does_not_inherit_exclusion_from_the_display_name(tmp_path: Path):
    """The stored display name 'LUMEN FEES' pattern-matches the broad
    excluding rule, while the RAW text matches only the non-excluding fee rule.
    Rules-apply must classify from the raw text and leave the row spendable."""
    db_path = tmp_path / "ledger.db"
    _seed(
        db_path,
        [
            _txn(
                "g_txn_leak",
                description="LM FEE 0725",
                merchant_raw="LM FEE 0725",
                merchant="LUMEN FEES",
            )
        ],
    )

    RuleApplicationService(db_path).apply_all(source="test", refresh_analytics=False)

    assert _row(db_path, "g_txn_leak") == {
        "merchant": "LM FEES",
        "category": "Fees",
        "exclude": False,
    }


def test_rules_apply_keeps_a_genuinely_excluded_row_excluded(tmp_path: Path):
    """Safety direction: raw text matching the excluding rule still excludes."""
    db_path = tmp_path / "ledger.db"
    _seed(
        db_path,
        [
            _txn(
                "g_txn_keep",
                description="LUMEN INVEST CONTRIB",
                merchant_raw="LUMEN INVEST",
                merchant="",
            )
        ],
    )

    RuleApplicationService(db_path).apply_all(source="test", refresh_analytics=False)

    assert _row(db_path, "g_txn_keep") == {
        "merchant": "LUMEN",
        "category": "Investing",
        "exclude": True,
    }


def test_rules_apply_still_resolves_a_row_by_its_configured_merchant_name(tmp_path: Path):
    """A row whose raw text matches nothing but whose merchant IS a configured
    merchant keeps resolving through that merchant's own rule (identity), so the
    steady state of a previously-applied ruleset is idempotent."""
    db_path = tmp_path / "ledger.db"
    _seed(
        db_path,
        [
            _txn(
                "g_txn_identity",
                description="SETTLEMENT 0725",
                merchant_raw="SETTLEMENT 0725",
                merchant="LUMEN",
            )
        ],
    )

    RuleApplicationService(db_path).apply_all(source="test", refresh_analytics=False)

    assert _row(db_path, "g_txn_identity") == {
        "merchant": "LUMEN",
        "category": "Investing",
        "exclude": True,
    }


# ===========================================================================
# 3. review-apply (src/ledger/import_review.py)
# ===========================================================================


def _resolve_into(db_path: Path, txn_id: str, merchant: str, category: str) -> int:
    seed_review_candidates(
        db_path,
        [
            {
                "key": f"merchant:{txn_id}",
                "merchant": merchant,
                "current_category": "Other",
                "txn_ids": [txn_id],
                "examples": [{"description": merchant}],
                "count": 1,
                "total_abs_amount": Decimal("40.00"),
            }
        ],
    )
    rows = get_review_rows(db_path)
    rows[0]["status"] = "resolved"
    rows[0]["merchant"] = merchant
    rows[0]["category"] = category
    save_review_rows(db_path, rows)
    return apply_review_decisions(db_path)


def test_review_resolve_does_not_inherit_exclusion_from_a_typed_merchant_name(tmp_path: Path):
    """THE LEAK. The raw text matches no rule; the user types the new merchant name
    'LUMEN FEES' and picks the spending category 'Fees'.  Pattern-matching
    that label against the ruleset used to inherit the broad LUMEN rule's
    exclude_from_spending and silently drop the row out of spending."""
    db_path = tmp_path / "ledger.db"
    _seed(
        db_path,
        [
            _txn(
                "g_rev_leak",
                description="SETTLEMENT 0725",
                merchant_raw="SETTLEMENT 0725",
                merchant="SETTLEMENT",
            )
        ],
    )

    assert _resolve_into(db_path, "g_rev_leak", "LUMEN FEES", "Fees") == 1

    assert _row(db_path, "g_rev_leak")["exclude"] is False, (
        "resolving into a spending category inherited an unrelated rule's exclusion "
        "by pattern-matching the typed display name"
    )


def test_review_resolve_into_a_configured_excluded_merchant_stays_excluded(tmp_path: Path):
    """Safety direction: naming the excluded merchant EXACTLY keeps the exclusion,
    even though the chosen category does not exclude."""
    db_path = tmp_path / "ledger.db"
    _seed(
        db_path,
        [
            _txn(
                "g_rev_identity",
                description="SETTLEMENT 0725",
                merchant_raw="SETTLEMENT 0725",
                merchant="SETTLEMENT",
            )
        ],
    )

    assert _resolve_into(db_path, "g_rev_identity", "LUMEN", "Fees") == 1

    assert _row(db_path, "g_rev_identity")["exclude"] is True


def test_review_resolve_keeps_exclusion_the_raw_text_earns(tmp_path: Path):
    """Safety direction (F16 parity): the raw text pattern-matches the excluding
    rule, so a resolution to an unrelated label and a spending category still
    excludes."""
    db_path = tmp_path / "ledger.db"
    _seed(
        db_path,
        [
            _txn(
                "g_rev_rawtext",
                description="LUMEN INVEST CONTRIB",
                merchant_raw="LUMEN INVEST",
                merchant="LM",
            )
        ],
    )

    assert _resolve_into(db_path, "g_rev_rawtext", "Money to my broker", "Fees") == 1

    assert _row(db_path, "g_rev_rawtext")["exclude"] is True


@pytest.mark.parametrize("typed_merchant", ["LUMEN FEES", "LUMEN"])
def test_review_resolve_always_writes_the_typed_merchant_and_category(
    tmp_path: Path, typed_merchant: str
):
    """Whatever the exclusion outcome, the user's explicit merchant/category choice
    is what lands on the row — the exclusion fix must not rewrite their decision."""
    db_path = tmp_path / "ledger.db"
    _seed(
        db_path,
        [
            _txn(
                "g_rev_choice",
                description="SETTLEMENT 0725",
                merchant_raw="SETTLEMENT 0725",
                merchant="SETTLEMENT",
            )
        ],
    )

    assert _resolve_into(db_path, "g_rev_choice", typed_merchant, "Fees") == 1

    row = _row(db_path, "g_rev_choice")
    assert row["merchant"] == typed_merchant
    assert row["category"] == "Fees"
