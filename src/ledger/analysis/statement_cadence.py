"""Estimate statement cadence and classify trailing gaps for one account.

Pure functions consume covered months, an evaluation date and an explicit
lifecycle status. They open no database and do not infer closure from activity.

A monthly assumption would flag every interior gap on an account with a slower
or irregular cadence. The modal month delta estimates that account's interval;
confidence reports the fraction of matching intervals. Only trailing silence is
classified, and the delivery grace allows a newly closed period time to arrive.

``accounts.is_active`` is unsuitable as a closure signal: its transaction-age
threshold decays during the same silence being measured. The account can remain
open while receiving infrequent statements. Only the operator's explicit
``account_lifecycle`` record supplies closure.

``overdue`` means the imported history's cadence expected a statement by now.
It does not claim that an institution issued one or that source coverage is
complete.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from itertools import pairwise

__all__ = [
    "CADENCE_BASES",
    "DECIDING_SIGNALS",
    "DELIVERY_GRACE_DAYS",
    "DORMANCY_MULTIPLIER",
    "ENTRY_DECIDING_SIGNALS",
    "GAP_CLASSIFICATIONS",
    "GAP_DORMANT",
    "MIN_CADENCE_OBSERVATIONS",
    "NO_ENTRY_SIGNALS",
    "Cadence",
    "GapAssessment",
    "classify_gap",
    "estimate_cadence",
    "gap_evaluation_date",
    "is_scoreable_period",
    "month_deltas",
    "month_index",
    "period_at",
]

_PERIOD_PATTERN = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])$")

# PUBLIC, and emitted on the wire as schema consts by `ledger.coverage` (see
# `mcp/contracts.py`). These two numbers decide every verdict this module gives,
# so a caller holding `{cadence_observations: 3, classification: "unknown"}`
# cannot tell whether one more statement or thirty would change the answer unless
# it can read them. Held privately they were also outside the contract digest,
# which meant a one-character edit here reclassified accounts with the published
# contract unchanged.

# Four covered periods provide three intervals. Less history does not establish
# a cadence: one monthly gap alone cannot justify a monthly overdue alert.
MIN_CADENCE_OBSERVATIONS = 4
# How many cadence steps of silence stop being "a statement is late" and start
# being "this account has gone quiet". Relative to the account's OWN cadence, so
# seven elapsed months is dormancy for a monthly account and a single late
# statement for a two-month one. A fixed month count cannot express that.
DORMANCY_MULTIPLIER = 3
# Days after a period closes before its statement can be called missing.
#
# A just-closed monthly period needs delivery time before it can be missing.
# Grace is counted in calendar days; this module has no holiday calendar.
DELIVERY_GRACE_DAYS = 5

CADENCE_BASIS_MODAL = "modal_month_delta"
CADENCE_BASIS_INSUFFICIENT = "insufficient_history"
CADENCE_BASES = (CADENCE_BASIS_MODAL, CADENCE_BASIS_INSUFFICIENT)

GAP_OVERDUE = "overdue"
GAP_DORMANT = "dormant"
GAP_UNKNOWN = "unknown"
GAP_CLASSIFICATIONS = (GAP_OVERDUE, GAP_DORMANT, GAP_UNKNOWN)

SIGNAL_OPERATOR_CLOSURE = "operator_recorded_closure"
SIGNAL_CADENCE_UNKNOWN = "cadence_unknown_insufficient_history"
SIGNAL_BEYOND_DORMANCY_MULTIPLE = "elapsed_beyond_cadence_dormancy_multiple"
SIGNAL_BEYOND_ONE_CADENCE = "elapsed_beyond_one_cadence"
SIGNAL_WITHIN_CADENCE = "within_cadence"
SIGNAL_WITHIN_DELIVERY_GRACE = "within_delivery_grace"
# The COMPLETE vocabulary. Published so a test can audit every answer this module
# can give -- including the D8 property that no signal names `is_active` -- from
# the vocabulary rather than from a reading of the branches that produce it.
DECIDING_SIGNALS = (
    SIGNAL_OPERATOR_CLOSURE,
    SIGNAL_CADENCE_UNKNOWN,
    SIGNAL_BEYOND_DORMANCY_MULTIPLE,
    SIGNAL_BEYOND_ONE_CADENCE,
    SIGNAL_WITHIN_CADENCE,
    SIGNAL_WITHIN_DELIVERY_GRACE,
)
# The two signals that decide there is NO entry. Published as their own tuple
# because the wire vocabulary is `DECIDING_SIGNALS` minus these, and a consumer
# deriving that subtraction from a hand-written list in another file is how the
# two drift: a signal added here but not subtracted there makes every
# `ledger.coverage` answer fail its own schema validation.
NO_ENTRY_SIGNALS = (SIGNAL_WITHIN_CADENCE, SIGNAL_WITHIN_DELIVERY_GRACE)
ENTRY_DECIDING_SIGNALS = tuple(
    signal for signal in DECIDING_SIGNALS if signal not in NO_ENTRY_SIGNALS
)


def gap_evaluation_date() -> date:
    """The reference date the gap arithmetic measures elapsed months against.

    A named seam, not an input: making it a tool argument would put a caller in
    charge of what "overdue" means, and every tool input must also be bound into
    `_cursor_fingerprint`. `ledger.coverage` echoes the value on the wire as
    `gaps_evaluated_on`, so the answer discloses the clock it read.

    ONE seam for every caller. `ledger accounts list` reached for `date.today()`
    directly, which meant a test patching the query layer's clock bought the CLI
    nothing and the two surfaces could disagree about the same account on the
    same day. Callers import it inside the function body so that patching THIS
    attribute reaches all of them.

    The LOCAL calendar date, deliberately. Stored timestamps are naive UTC, but
    "has this month's statement arrived" is a question about the operator's
    calendar, and the two differ only within hours of a month boundary.
    """
    return date.today()


def month_index(period: str) -> int:
    """Months since year zero, so a delta is one subtraction.

    Raises:
        ValueError: for anything that is not ``YYYY-MM``. ``statement_coverage``
            stores the period as TEXT with no constraint, and scoring a
            malformed one would produce a cadence rather than a complaint.
    """
    if not _PERIOD_PATTERN.match(period):
        raise ValueError(f"statement period must be YYYY-MM, got {period!r}")
    year, month = period.split("-")
    return int(year) * 12 + (int(month) - 1)


def period_at(index: int) -> str:
    """The inverse of :func:`month_index`."""
    year, month = divmod(index, 12)
    return f"{year:04d}-{month + 1:02d}"


def is_scoreable_period(value: str) -> bool:
    """Whether :func:`month_index` can score this stored period.

    ``statement_coverage.period`` is unconstrained TEXT, so every caller has to
    decide what to do with a value that is not ``YYYY-MM``. They all make the
    same choice -- drop it from the cadence rather than let one malformed row
    take down an answer about eight healthy accounts -- and they make it through
    THIS predicate, so the definition of a scoreable period cannot fork between
    the query layer and the CLI.
    """
    return bool(_PERIOD_PATTERN.match(value))


def month_deltas(periods: Sequence[str]) -> tuple[int, ...]:
    """Gaps in months between consecutive covered periods.

    Sorted and de-duplicated first. SQL supplies these ordered and distinct, but
    a duplicate would contribute a zero-month delta, and a zero cadence makes
    every subsequent gap infinite.
    """
    ordered = sorted({month_index(period) for period in periods})
    return tuple(second - first for first, second in pairwise(ordered))


@dataclass(frozen=True)
class Cadence:
    """How often one account produces a statement, and how sure that is.

    ``months`` is ``None`` when the history is too short to establish cadence.
    An unknown cadence is a valid result, not a guessed monthly obligation.
    """

    months: int | None
    # Fraction of observed intervals matching the mode; zero when unknown.
    # A near tie remains visible through confidence rather than being concealed
    # by the selected interval.
    confidence: float
    observations: int
    intervals: int
    modal_intervals: int
    basis: str


@dataclass(frozen=True)
class GapAssessment:
    """One account's statement-gap verdict, and the fact that decided it.

    ``classification`` is ``None`` when there is no gap. ``deciding_signal`` is
    always populated, including then, so an entry can always be traced back to
    the single fact that produced it rather than to "the classifier said so".
    """

    classification: str | None
    deciding_signal: str
    # Months from the last covered period to the current month. NEGATIVE when the
    # newest covered period is in the future, which clock skew or a mis-stamped
    # statement can produce and which is never a gap.
    elapsed_months: int
    # The periods this account's own cadence expected and that were not imported,
    # stepped BY that cadence: a two-month account is not missing the months
    # between its statements. Empty whenever no statement can honestly be named:
    # an unknown cadence, or an operator-recorded closure.
    missing_periods: tuple[str, ...]


def estimate_cadence(periods: Sequence[str]) -> Cadence:
    """The modal month-delta over an account's covered periods.

    Use the mode rather than a rounded mean: a mean can describe an interval
    that never occurred, and rounding can change after one additional import.
    The mode is a delta the account actually produces.

    Ties resolve to the LONGER interval. A tie means the account is genuinely
    irregular, and the two errors are not symmetric -- the shorter value
    manufactures the false ``overdue`` entries this estimator exists to remove,
    while the longer one delays a true one by a single period. Made explicit so
    it cannot ride on ``Counter.most_common``'s insertion order, which would make
    the answer depend on which delta happened to occur first.

    Raises:
        ValueError: for a malformed period (see :func:`month_index`).
    """
    distinct = sorted({month_index(period) for period in periods})
    observations = len(distinct)
    deltas = tuple(second - first for first, second in pairwise(distinct))
    if observations < MIN_CADENCE_OBSERVATIONS:
        return Cadence(
            months=None,
            confidence=0.0,
            observations=observations,
            intervals=len(deltas),
            modal_intervals=0,
            basis=CADENCE_BASIS_INSUFFICIENT,
        )
    counts = Counter(deltas)
    best = max(counts.values())
    months = max(delta for delta, count in counts.items() if count == best)
    return Cadence(
        months=months,
        confidence=best / len(deltas),
        observations=observations,
        intervals=len(deltas),
        modal_intervals=best,
        basis=CADENCE_BASIS_MODAL,
    )


def classify_gap(
    *,
    last_covered: str,
    cadence: Cadence,
    today: date,
    lifecycle_status: str | None,
) -> GapAssessment:
    """Classify the trailing silence on one account.

    The TRAILING gap only. Interior gaps contribute to the cadence estimate;
    reporting those same gaps as missing statements can misclassify an irregular
    account. Ask whether the next statement expected by its cadence is present.

    Precedence, in order, each branch naming its own deciding signal:

    1. an operator-recorded closure -> ``dormant``. The operator's own record
       outranks every derivation, including an unknown cadence: an account closed
       before it accumulated four statements is the ordinary case;
    2. an unclaimable cadence -> ``unknown``. No statement is named missing;
    3. silence past ``DORMANCY_MULTIPLIER`` cadences -> ``dormant``;
    4. silence past one cadence -> ``overdue``;
    5. otherwise no gap.

    A recorded status of ``active`` decides nothing. It records that the operator
    checked, not that statements have stopped being expected, so it falls through
    to the same derivation an unrecorded account gets. Only ``closed`` silences.

    ``accounts.is_active`` is not a parameter and must never become one (D8).
    """
    last_index = month_index(last_covered)
    current_index = month_index(f"{today.year:04d}-{today.month:02d}")
    elapsed = current_index - last_index
    # The newest period a statement can be called missing FOR, exclusive. A
    # period closes at the end of its month, and `DELIVERY_GRACE_DAYS` more have
    # to pass before its absence means anything -- so on the first of a month the
    # month just ended is still in transit, and only the one before it counts.
    #
    # The reported `elapsed_months` deliberately stays the raw month arithmetic.
    # It is a measurement of the silence, not a verdict about it, and a caller
    # reconciling it against `last_covered_period` and `gaps_evaluated_on` must
    # get the number those two imply.
    settled_index = current_index - (0 if today.day > DELIVERY_GRACE_DAYS else 1)
    settled_elapsed = settled_index - last_index

    if lifecycle_status == "closed":
        return GapAssessment(
            classification=GAP_DORMANT,
            deciding_signal=SIGNAL_OPERATOR_CLOSURE,
            elapsed_months=elapsed,
            missing_periods=(),
        )
    if cadence.months is None:
        return GapAssessment(
            classification=GAP_UNKNOWN,
            deciding_signal=SIGNAL_CADENCE_UNKNOWN,
            elapsed_months=elapsed,
            missing_periods=(),
        )

    step = cadence.months
    missing = tuple(period_at(index) for index in range(last_index + step, settled_index, step))
    if settled_elapsed > step * DORMANCY_MULTIPLIER:
        return GapAssessment(
            classification=GAP_DORMANT,
            deciding_signal=SIGNAL_BEYOND_DORMANCY_MULTIPLE,
            elapsed_months=elapsed,
            missing_periods=missing,
        )
    if settled_elapsed > step:
        return GapAssessment(
            classification=GAP_OVERDUE,
            deciding_signal=SIGNAL_BEYOND_ONE_CADENCE,
            elapsed_months=elapsed,
            missing_periods=missing,
        )
    # Two different facts, told apart because a caller reading `elapsed_months: 2`
    # beside `cadence_months: 1` and no entry would otherwise have to guess which
    # one the classifier applied.
    return GapAssessment(
        classification=None,
        deciding_signal=(SIGNAL_WITHIN_DELIVERY_GRACE if elapsed > step else SIGNAL_WITHIN_CADENCE),
        elapsed_months=elapsed,
        missing_periods=(),
    )
