"""The ONE resolver every account-bearing surface mints its opaque ref through.

`account_ref` HMACs three components, and three call sites used to supply three
different third ones -- the parser's `statement_coverage.statement_type`, the
stored `accounts.account_type`, and the free-text `transactions.account_type`.
Eight accounts minted twenty-seven refs, no account carried the same ref on two
tools, and the server instructions asserted the opposite. Anything that emits an
account ref resolves it here instead of deriving one locally.
"""

from __future__ import annotations

import hmac
from collections.abc import Mapping

from sqlalchemy.orm import Session

from ledger.storage.database import AccountDB, InvestmentAccountDB
from ledger.storage.operations import account_ref

__all__ = [
    "BANKING_ACCOUNT_REF_DOMAIN",
    "account_id_ref_map",
    "account_ref_map",
    "banking_account_ref",
    "investment_account_ref_map",
    "reverse_account_ref_map",
    "resolve_account_ref",
    "unresolved_account_ref",
]

BANKING_ACCOUNT_REF_DOMAIN = "banking-account"


def banking_account_ref(
    salt: str,
    institution: str,
    account: str,
    _display_type: str | None = None,
) -> str:
    """Mint a stable banking ref from the canonical account identity.

    The fixed domain separates banking identities from investment aliases while
    keeping mutable display classifications out of the HMAC input. The optional
    display type is accepted only so callers migrating from ``account_ref`` can
    pass their existing value and prove it has no effect.
    """
    return account_ref(salt, institution, account, BANKING_ACCOUNT_REF_DOMAIN)


def account_id_ref_map(session: Session, salt: str | None) -> dict[str, str]:
    """Map private banking primary keys to their canonical opaque references.

    This is the adapter for tables whose foreign keys point at ``accounts``.
    The public HMAC inputs are the account row's institution, canonical account
    number, and a fixed banking domain. The primary key is only a lookup key and
    never crosses an agent boundary.
    """
    if not salt:
        return {}
    rows = session.query(
        AccountDB.account_id,
        AccountDB.account_number,
        AccountDB.institution,
    ).all()
    return {
        str(account_id): banking_account_ref(salt, str(institution), str(number))
        for account_id, number, institution in rows
    }


def reverse_account_ref_map(session: Session, salt: str | None) -> dict[str, str]:
    """Resolve an opaque banking reference to one private account primary key.

    A truncated HMAC collision is fail-closed: ambiguous references are omitted
    instead of selecting an arbitrary account.  Unresolved sentinels never enter
    this map because they have no account row to resolve to.
    """
    by_id = account_id_ref_map(session, salt)
    candidates: dict[str, list[str]] = {}
    for account_id, ref in by_id.items():
        candidates.setdefault(ref, []).append(account_id)
    return {ref: ids[0] for ref, ids in candidates.items() if len(ids) == 1}


def account_ref_map(session: Session, salt: str | None) -> dict[str, str]:
    """Map every account slug to the ONE opaque ref every surface emits.

    Keyed by ``accounts.account_number`` -- the same value space as
    ``transactions.account`` and ``statement_coverage.account_slug``. The third
    HMAC component is a fixed banking domain. Parser-supplied statement types,
    free-text transaction types, and the detector's mutable modal account type
    never participate, so reclassification cannot rename the account.

    The institution is read from the ``accounts`` row rather than joined on,
    because the slug already includes the institution and the other tables'
    institution column is parser-supplied and free to drift.

    Absent salt yields an empty map; callers withhold rather than degrade. A slug
    with no row here resolves to nothing, and the caller states that as a reason
    instead of falling back to a locally derived ref.
    """
    if not salt:
        return {}
    rows = session.query(AccountDB.account_number, AccountDB.institution).all()
    return {
        str(number): banking_account_ref(salt, str(institution), str(number))
        for number, institution in rows
    }


def investment_account_ref_map(
    session: Session,
    salt: str | None,
) -> dict[tuple[str, str, str], str]:
    """Map investment identity to the same opaque ``acct-`` namespace.

    Investment statements do not share banking's extractor-assigned account
    slug.  Their public identity is therefore the explicitly configured
    ``(institution, account_alias, account_type)`` tuple.  The source account id
    and transfer destination are deliberately absent: both are private import
    facts, not stable join keys.  Absent salt follows the banking resolver's
    fail-closed rule and returns no refs.
    """
    if not salt:
        return {}
    rows = session.query(
        InvestmentAccountDB.institution,
        InvestmentAccountDB.account_alias,
        InvestmentAccountDB.account_type,
    ).all()
    return {
        (str(institution), str(alias), str(kind)): account_ref(
            salt,
            str(institution),
            str(alias),
            str(kind),
        )
        for institution, alias, kind in rows
    }


def unresolved_account_ref(salt: str, account: str) -> str:
    """The sentinel for an account slug the ``accounts`` dimension has no row for.

    D3: an unjoinable row emits an opaque salted sentinel in a VISIBLY DISTINCT
    namespace plus a stated status -- never a resolved-namespace ``acct-`` ref,
    never the raw slug, and never a blank or null key.

    The blank key is the alternative worth naming, because it is the one that
    looks safe. Two different unjoinable accounts then produce byte-identical
    values, so anything that groups by ref merges two real accounts: the export's
    ``monthly_trend.csv`` groups on ``["month", "account_ref"]`` and summed their
    money into one row, and ``ledger.coverage`` emitted two identical entries for
    two different accounts' statements. This is a state production reaches, not a
    hypothetical -- ``persist_statement_coverage`` runs at finalize BEFORE account
    detection, and ``save_accounts`` never writes a row for an account with zero
    transactions, so a new account whose first statement had no activity leaves a
    permanently unjoinable coverage row.

    Derived here, once, so every surface that has to disclose the case discloses
    the SAME value for the same account. The salt is the account salt, so the
    sentinel reveals nothing about the slug, and it is as stable within a database
    as a resolved ref is. It is never joinable to one: the namespaces cannot
    collide, and the caller is told which it holds.
    """
    digest = hmac.new(
        salt.encode(),
        f"unresolved-account|{account}".encode(),
        "sha256",
    ).hexdigest()
    return f"unresolved-{digest[:10]}"


def resolve_account_ref(refs: Mapping[str, str], account: str, salt: str) -> tuple[str, str]:
    """The ref an account-bearing row emits, and the status that explains it.

    ``(ref, "resolved")`` when the slug joins to an ``accounts`` record, and
    ``(sentinel, "no_account_record")`` when it does not. Both branches return a
    string; the status is what a caller reads to know which namespace it holds.
    """
    resolved = refs.get(account)
    if resolved is not None:
        return resolved, "resolved"
    return unresolved_account_ref(salt, account), "no_account_record"
