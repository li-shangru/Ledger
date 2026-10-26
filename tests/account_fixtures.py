"""Seed the accounts dimension for tests that write derived account rows.

``account_balances`` and ``recurring_patterns`` reference ``accounts.account_id``.
Production never writes either without first detecting the account, but many test
fixtures used to insert them straight into an empty database. This helper gives
those fixtures the parent row, keyed exactly as production keys it.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from ledger.core.account_identity import account_identity
from ledger.storage.database import AccountDB, session_scope

_CREDIT_KEYWORDS = ("visa", "mastercard", "credit")
_DEPOSIT_KEYWORDS = ("chequing", "checking", "savings", "deposit")


def looks_like_a_credit_card(account_type: str) -> bool:
    """Mirror AccountDetector._is_credit_card's keyword rule for a type string."""
    lowered = account_type.lower()
    if any(keyword in lowered for keyword in _DEPOSIT_KEYWORDS):
        return False
    return any(keyword in lowered for keyword in _CREDIT_KEYWORDS)


def seed_account(
    db_path: Path,
    institution: str,
    account_slug: str,
    *,
    account_type: str = "checking",
    account_subtype: str | None = None,
    is_credit_card: bool | None = None,
    detected_credit_limit: Decimal | None = None,
    first_transaction_date: date = date(2024, 1, 1),
    last_transaction_date: date = date(2026, 12, 31),
) -> str:
    """Insert (or leave in place) the accounts row for one institution/slug pair.

    Returns the account id, so a caller can use it as the foreign key value
    without deriving it a second time.

    ``account_subtype`` defaults to ``account_type`` but is a separate argument:
    collapsing the two forces callers that assert on a display label to smuggle
    display-cased values into ``account_type``, which production never emits.

    ``is_credit_card`` defaults to whatever the type string implies, so a fixture
    seeding a "credit_card" account cannot silently describe it as an asset --
    that flag is what the debt-ratio and emergency-fund metrics key on.
    """
    account_id = account_identity(institution, account_slug)
    now = datetime(2026, 1, 1, 12, 0)
    with session_scope(db_path) as session:
        if session.get(AccountDB, account_id) is not None:
            return account_id
        session.add(
            AccountDB(
                account_id=account_id,
                institution=institution,
                account_number=account_slug,
                account_type=account_type,
                account_subtype=account_type if account_subtype is None else account_subtype,
                first_transaction_date=first_transaction_date,
                last_transaction_date=last_transaction_date,
                is_active=True,
                transaction_count=1,
                avg_monthly_transactions=Decimal("1.00"),
                is_credit_card=(
                    looks_like_a_credit_card(account_type)
                    if is_credit_card is None
                    else is_credit_card
                ),
                detected_credit_limit=detected_credit_limit,
                created_at=now,
                updated_at=now,
            )
        )
    return account_id


def seed_accounts_for_existing_rows(db_path: Path) -> list[str]:
    """Create an accounts row for every (institution, account) already present.

    For fixtures that build transactions or transfers first and then run analysis
    or reporting over them. Mirrors what account detection would produce, without
    requiring the fixture to enumerate its own accounts.

    The account type is taken from the transactions themselves -- the most common
    value per account, as the detector does -- rather than defaulted. Defaulting
    it would make the dimension contradict the rows it was derived from, and a
    credit card seeded as a chequing account is exactly the kind of fixture that
    lets a debt metric pass for the wrong reason.
    """
    from sqlalchemy import text

    with session_scope(db_path) as session:
        rows = session.execute(
            text(
                "SELECT institution, account, account_type, COUNT(*) AS n FROM transactions "
                "GROUP BY institution, account, account_type "
                "UNION ALL "
                "SELECT institution, account, '', 0 FROM transfers "
                "GROUP BY institution, account"
            )
        ).all()

    best: dict[tuple[str, str], tuple[int, str]] = {}
    for institution, account_slug, account_type, count in rows:
        if not institution or not account_slug:
            continue
        key = (str(institution), str(account_slug))
        current = best.get(key, (-1, ""))
        if int(count) > current[0]:
            best[key] = (int(count), str(account_type or ""))

    seeded: list[str] = []
    for (institution, account_slug), (_count, account_type) in sorted(best.items()):
        seeded.append(
            seed_account(
                db_path,
                institution,
                account_slug,
                account_type=account_type or "checking",
            )
        )
    return seeded


__all__ = ["seed_account", "seed_accounts_for_existing_rows"]
