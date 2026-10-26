"""The one function that decides what identifies a banking account.

An account is identified by its institution and its slug. The slug is the CSV
stem: ``TransactionDB.account``, ``AccountDB.account_number`` and
``AccountStatementCreditLimitDB.account_slug`` are all that same value space.

Every producer of an ``account_id`` must call this. Deriving the id inline
invites the failure this module exists to prevent: ``repair/statements.py``
used a fixed institution in its hash, which could attach another issuer's
balances to a fabricated account identity.
"""

from __future__ import annotations

from collections.abc import Mapping

from ledger.utils.hashing import stable_id


def account_identity(institution: str, account_slug: str) -> str:
    """Return the stable account id for one institution/slug pair.

    Raises:
        ValueError: if either part is blank. A blank part would collapse
            distinct accounts onto one id.
    """
    if not institution or not institution.strip():
        raise ValueError("account identity requires a non-blank institution")
    if not account_slug or not account_slug.strip():
        raise ValueError("account identity requires a non-blank account slug")
    return stable_id(institution, account_slug)


# The institution component used when an account has none. Coverage rows land at
# PDF finalize, and `imports/pdf/post_processing.py` writes `institution = ''`
# for any statement type the identity map does not know -- a state a newly
# onboarded institution reaches routinely. Such an account can still raise a
# coverage-gap alert, so it must still have an id its closure can be recorded
# under. Spelled with parentheses so it cannot be confused with, or collide
# with, a real institution name: `stable_id` lowercases and collapses whitespace
# but keeps punctuation, so `(unknown-institution)` is unreachable from any value
# a parser assigns.
_UNKNOWN_INSTITUTION = "(unknown-institution)"


def lifecycle_account_ids(
    stored_ids: Mapping[str, str], account_slug: str, institution: str
) -> tuple[str, ...]:
    """Every ``account_id`` one slug's lifecycle row could be keyed by, canonical first.

    ``account_lifecycle`` is operator-authored and keyed by ``account_id``, while
    the alert it silences is keyed by ``statement_coverage.account_slug``. The
    two are joined by a derivation, and the derivation's answer is not stable:

    * the stored ``accounts.account_id`` disappears when
      ``save_accounts(replace_all=True)`` drops an account whose last transaction
      aged out -- the normal end state of a closed account, and the very case the
      table exists for;
    * a coverage-only slug's institution is ``''`` until account detection runs,
      and then becomes a real value.

    Keying a write on whichever id happened to resolve at the time therefore
    produces a record that silently stops applying, and a later write under the
    other id leaves two contradictory rows for one account. So the canonical key
    (element 0) is the one that does NOT depend on a row that can vanish, and the
    remaining elements are the other places a row may already have been written.
    Writers move a record onto element 0; readers try every element in order.

    Returns an empty tuple only for a blank slug, which names no account at all.
    """
    slug = account_slug.strip()
    if not slug:
        return ()
    if institution.strip():
        candidates = [account_identity(institution, account_slug)]
        # Kept as a candidate even when the institution is known, because the
        # row may have been written while it was not.
        candidates.append(account_identity(_UNKNOWN_INSTITUTION, account_slug))
    else:
        candidates = [account_identity(_UNKNOWN_INSTITUTION, account_slug)]
    stored = stored_ids.get(account_slug)
    if stored is not None and stored not in candidates:
        candidates.append(stored)
    return tuple(candidates)


def institution_from_statement_type(statement_type: str) -> str:
    """Resolve explicit inert format metadata without executing an adapter."""
    from ledger.adapters.registry import metadata_for_statement_type

    metadata = metadata_for_statement_type(statement_type)
    if metadata is None or not metadata.get("institution"):
        raise ValueError("statement_institution_unavailable")
    return str(metadata["institution"])


# `resolve_account_id` used to live here, returning ONE id -- the stored
# `accounts.account_id` if present, else the derivation. It was removed rather
# than left beside its replacement: its answer changes the moment the `accounts`
# row disappears, which is the exact transition `account_lifecycle` exists for,
# so a record written under it silently stopped applying and a later write under
# the other id left two contradictory rows. Both of its callers now use
# `lifecycle_account_ids`, and leaving the single-answer version available would
# invite the same defect back.
__all__ = [
    "account_identity",
    "institution_from_statement_type",
    "lifecycle_account_ids",
]
