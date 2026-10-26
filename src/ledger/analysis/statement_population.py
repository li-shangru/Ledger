"""The population every statement-cadence claim quantifies over, gathered once.

WHY THIS MODULE EXISTS. Two surfaces publish a universal over the same set of
accounts. ``ledger.coverage``'s ``gaps`` block classifies each one `overdue`,
`dormant` or `unknown`; the dashboard's health basis says "every non-dormant
account with imported statement coverage has a statement for {period} or later"
and cites the coverage tool's rule as its authority. Both were assembling that
population themselves -- the same slug/period gather, the same ``min()``
institution tie-break, the same ``lifecycle_account_ids`` fallback, the same
malformed-period drop -- with nothing binding the two copies together. Two
independent implementations of one published rule is how a dashboard and an MCP
tool come to classify the same account differently after a future edit to one of
them.

WHAT IS **NOT** IN THIS POPULATION. ``statement_coverage`` is written by exactly
one producer, the PDF transaction-statement import (``imports/pdf/
post_processing.py``), so it holds bank and card accounts only. Investment
accounts keep their own ``investment_accounts`` / ``investment_statements``
tables and their own ``status`` column, and none of it is read here. The
sentence a caller builds must therefore name the covered population explicitly. It also means a closure recorded on an investment account is invisible to
this rule: ``account_lifecycle`` is the only closure signal read.

The verdict itself is not here. It lives in ``statement_cadence``, which opens no
database, reads no clock and knows no account names.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy.orm import Session

from ledger.core.account_identity import lifecycle_account_ids
from ledger.storage.database import (
    AccountDB,
    AccountLifecycleDB,
    StatementCoverageDB,
)

__all__ = [
    "CoveragePopulation",
    "gather_coverage_population",
    "lifecycle_status",
]


@dataclass(frozen=True)
class CoveragePopulation:
    """Every account ``statement_coverage`` records, and what decides its verdict.

    ``periods`` maps a slug to its SCOREABLE covered periods, sorted ascending.
    ``period`` is unconstrained TEXT, so a malformed value is dropped from the
    cadence rather than allowed to raise: one bad row must not take down an
    answer about every healthy account. A slug whose only rows are malformed
    therefore has no entry here at all.
    """

    periods: Mapping[str, tuple[str, ...]]
    # Every slug the table holds, INCLUDING one whose rows are all malformed and
    # which therefore has no `periods` entry. `ledger.coverage` discloses the
    # difference as `unscoreable_accounts`; keeping both here is what lets it.
    all_slugs: frozenset[str]
    # One institution per slug, chosen by SORT rather than by whichever row the
    # database returned first. Conflicting institution spellings are anomalous, but
    # the choice feeds an account-id DERIVATION, and
    # an identity that depends on unordered row arrival is the class of defect
    # this release exists to remove.
    institutions: Mapping[str, str]
    stored_ids: Mapping[str, str]
    lifecycle: Mapping[str, str]

    def slugs(self) -> tuple[str, ...]:
        """Every scoreable slug, ordered by the STORED slug.

        Ordered by the slug and never by an account reference: the reference is
        an HMAC of a salt no cursor binds, so ordering on it would make the
        served order depend on which salt was minted.
        """
        return tuple(sorted(self.periods))

    def status(self, slug: str) -> str | None:
        """The operator's recorded status for one slug, under any of its ids."""
        return lifecycle_status(
            self.lifecycle, self.stored_ids, slug, self.institutions.get(slug, "")
        )


def lifecycle_status(
    lifecycle: Mapping[str, str],
    stored_ids: Mapping[str, str],
    slug: str,
    institution: str,
) -> str | None:
    """The operator's recorded status for one coverage slug, under any of its ids.

    Every candidate is tried, canonical first, because the id a slug resolves to
    is not stable across the account's lifetime -- see `lifecycle_account_ids`.
    `ledger accounts` writes under the canonical id and clears the others, so
    more than one match is only reachable on a hand-edited table; the canonical
    one wins there, which is the same row the CLI would have rewritten.
    """
    for account_id in lifecycle_account_ids(stored_ids, slug, institution):
        status = lifecycle.get(account_id)
        if status is not None:
            return status
    return None


def gather_coverage_population(session: Session) -> CoveragePopulation:
    """Read the whole of ``statement_coverage`` plus what classifies it.

    The WHOLE table, never a requested window. Cadence is an account-level
    property estimated from that account's complete covered history, and a caller
    who asked about three months would otherwise be told every account's cadence
    is unclaimable.

    The lifecycle join goes through ``account_id``, which is the key of neither
    table involved: ``statement_coverage`` is keyed by ``account_slug`` and
    ``account_lifecycle`` by ``account_id``. ``lifecycle_account_ids`` supplies
    every id a slug's record could be keyed by rather than a single answer,
    because a single answer is not stable: the stored ``accounts.account_id``
    disappears when ``save_accounts(replace_all=True)`` drops an account whose
    last transaction aged out -- the normal end state of a CLOSED account, and the
    case that table exists for -- and a coverage-only slug's institution is blank
    until account detection runs.

    Callers must have an initialized schema. ``account_lifecycle`` ships in
    1.4.0, so a database from before it has no such table; a caller that may see
    one checks first and says so rather than migrating on a read path.
    """
    from ledger.analysis.statement_cadence import is_scoreable_period

    periods: dict[str, set[str]] = {}
    spellings: dict[str, set[str]] = {}
    seen: set[str] = set()
    for slug_value, period_value, institution_value in session.query(
        StatementCoverageDB.account_slug,
        StatementCoverageDB.period,
        StatementCoverageDB.institution,
    ).all():
        slug = str(slug_value)
        seen.add(slug)
        if institution_value:
            spellings.setdefault(slug, set()).add(str(institution_value))
        if is_scoreable_period(str(period_value)):
            periods.setdefault(slug, set()).add(str(period_value))

    return CoveragePopulation(
        periods={slug: tuple(sorted(values)) for slug, values in periods.items()},
        all_slugs=frozenset(seen),
        institutions={slug: min(values) for slug, values in spellings.items()},
        stored_ids={
            str(number): str(account_id)
            for number, account_id in session.query(
                AccountDB.account_number, AccountDB.account_id
            ).all()
        },
        lifecycle={
            str(account_id): str(status)
            for account_id, status in session.query(
                AccountLifecycleDB.account_id, AccountLifecycleDB.status
            ).all()
        },
    )
