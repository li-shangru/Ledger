"""Direct coverage for the import merchant-review predicate + grouping (F24).

needs_merchant_review / build_merchant_review are monkeypatched to [] in every
workflow test, so the classification (which rows land in the manual-review queue) and
grouping (dedup, counts, sort) ran untested. These exercise the real functions.
"""

from datetime import date, datetime
from decimal import Decimal

from ledger.core.models import Transaction
from ledger.imports.pdf.review import build_merchant_review, needs_merchant_review
from ledger.processing import ProcessingResult


def _txn(
    txn_id: str,
    *,
    merchant_raw: str = "",
    merchant: str = "",
    category: str = "Other",
    mconf: str = "high",
    cconf: str = "high",
    amount: str = "-10.00",
) -> Transaction:
    return Transaction(
        txn_id=txn_id,
        txn_fingerprint=f"fp{txn_id}",
        posting_date=date(2026, 1, 5),
        institution="harbor",
        account="acct",
        account_type="visa",
        flow="expense",
        cashflow_type="expense",
        include_in_cashflow=True,
        currency="CAD",
        amount_raw=abs(Decimal(amount)),
        amount_signed=Decimal(amount),
        description=merchant_raw or "desc",
        merchant=merchant,
        category=category,
        merchant_confidence=mconf,
        category_confidence=cconf,
        merchant_raw=merchant_raw,
        category_raw="",
        source_file="x.csv",
        source_path="x.csv",
        source_period="2026-01",
        source_row=1,
        imported_at=datetime(2026, 1, 1),
        parser_version="t",
    )


def test_needs_merchant_review_predicate():
    # Fully high-confidence, non-Other -> no review.
    assert not needs_merchant_review(_txn("a", category="Dining", mconf="high", cconf="high"))
    # Any low confidence -> review.
    assert needs_merchant_review(_txn("b", category="Dining", mconf="low", cconf="high"))
    assert needs_merchant_review(_txn("c", category="Dining", mconf="high", cconf="low"))
    # Category "Other" even at high confidence -> review.
    assert needs_merchant_review(_txn("d", category="Other", mconf="high", cconf="high"))


def test_build_merchant_review_groups_counts_and_sorts():
    result = ProcessingResult(
        transactions=[
            # excluded: fully high-confidence, real category
            _txn("hi", merchant_raw="COSTCO WHOLESALE", category="Groceries"),
            # two low-confidence rows for the same raw merchant -> one group, count 2
            _txn("s1", merchant_raw="STARBUCKS #1", category="Dining", mconf="low"),
            _txn("s2", merchant_raw="STARBUCKS #1", category="Dining", mconf="low"),
            # one Other-category row -> its own group, count 1
            _txn("u1", merchant_raw="UNKNOWN SHOP", category="Other"),
        ],
        transfers=[],
        exceptions=[],
    )

    groups = build_merchant_review(result)

    assert [g["key"] for g in groups] == ["STARBUCKS #1", "UNKNOWN SHOP"]  # sorted by -count, key
    assert "COSTCO WHOLESALE" not in {g["key"] for g in groups}  # high-confidence excluded

    starbucks = groups[0]
    assert starbucks["count"] == 2
    assert sorted(starbucks["txn_ids"]) == ["s1", "s2"]
    assert starbucks["total_abs_amount"] == Decimal("20.00")


def test_build_merchant_review_dedupes_identical_examples():
    """Identical example dicts (same date/amount/description/source) collapse to one."""
    result = ProcessingResult(
        transactions=[
            _txn("e1", merchant_raw="DUP MERCHANT", category="Other"),
            _txn("e2", merchant_raw="DUP MERCHANT", category="Other"),
        ],
        transfers=[],
        exceptions=[],
    )
    groups = build_merchant_review(result)
    assert len(groups) == 1
    assert groups[0]["count"] == 2
    # Both rows share identical example content -> deduped to a single example.
    assert len(groups[0]["examples"]) == 1
