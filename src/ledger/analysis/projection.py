"""Expected recurring flows over an explicit balance window, disclosed line by line.

The caller supplies patterns, anchor, target and evaluation dates. This module
opens no database and reads no clock. Every pattern either produces line items
or has a counted disposition explaining why it did not.

Four stored-field properties matter:

* ``next_expected_date`` follows the detector's last accepted occurrence. It
  can be retrospective, and an older statement anchor can require walking the
  cadence backwards as well as forwards. Otherwise an accepted occurrence can
  fall between the absolute balance and the projected flows without appearing
  in either term of the reconciliation.
* ``expected_amount`` is an unsigned magnitude. Direction comes from the
  pattern type; a transfer has no unique direction and is explicitly omitted.
* ``missed_count`` floors whole overdue periods. A zero count therefore does
  not prove punctuality. Dates supply the overdue inference, while a separate
  pattern count preserves the signal when an advanced anchor hides old lines.
* ``expected_amount`` is a historical mean, not a claim about the latest charge.
  For example, a fictional bill averaging 840.00 can have a latest charge of
  900.00. A fixed-amount classification does not make that mean current.

Every emitted amount is quantized once to cents. The projected balance is the
anchor plus the sum of those emitted amounts, rather than a separately rounded
sum of raw values. Truncating the list would break that identity, so a window
that exceeds the line-item bound is refused.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from ledger.analysis.pattern_detector import (
    RECURRING_FREQUENCIES,
    next_occurrence,
    previous_occurrence,
)
from ledger.utils.money import to_cents

__all__ = [
    "DEFAULT_MAX_LINE_ITEMS",
    "DIRECTION_BY_PATTERN_TYPE",
    "DISPOSITIONS",
    "OMISSION_REASONS",
    "PROJECTION_HORIZON_DAYS",
    "REASON_INACTIVE",
    "REASON_NO_OCCURRENCE",
    "REASON_TRANSFER",
    "REASON_UNCLASSIFIED_TYPE",
    "REASON_UNSUPPORTED_FREQUENCY",
    "PatternDisposition",
    "Projection",
    "ProjectionLine",
    "ProjectionTooBroadError",
    "RecurringInput",
    "align_to_window",
    "build_projection",
    "validate_window",
]

# The ONLY three pattern types that carry a direction, and the sign each one
# carries. PUBLIC, and published on the wire, because a caller cannot otherwise
# tell an omitted flow from an absent one.
#
# A transfer's direction depends on which account is being projected. Its type
# alone cannot determine a sign, so no guessed direction is published here.
DIRECTION_BY_PATTERN_TYPE = {"income": 1, "bill": -1, "subscription": -1}

REASON_INACTIVE = "inactive"
REASON_TRANSFER = "transfer_direction_ambiguous"
REASON_UNCLASSIFIED_TYPE = "unclassified_pattern_type"
REASON_UNSUPPORTED_FREQUENCY = "unsupported_frequency"
REASON_NO_OCCURRENCE = "no_occurrence_in_window"
# Every way a pattern can contribute nothing. Published as one tuple so the wire
# vocabulary and the counters cannot fork: a sixth reason added to the classifier
# without being added here would go out as a number nothing explains.
OMISSION_REASONS = (
    REASON_INACTIVE,
    REASON_TRANSFER,
    REASON_UNCLASSIFIED_TYPE,
    REASON_UNSUPPORTED_FREQUENCY,
    REASON_NO_OCCURRENCE,
)
DISPOSITION_PROJECTED = "projected"
DISPOSITIONS = (DISPOSITION_PROJECTED, *OMISSION_REASONS)

# How far past the requested as-of date a projection may reach.
#
# A bound is unavoidable: the caller names the target date, a weekly pattern
# yields a line item every seven days, and the list is never truncated. A year
# plus a day is where it stops -- past that the roll-forward count on every line
# item is larger than the number of occurrences the detector actually observed,
# which is a statement about the arithmetic rather than about the account.
#
# The horizon is measured against the caller-supplied evaluation date. Anchors
# can be older by different amounts, so anchor-relative bounds would give
# different request limits for otherwise identical accounts.
PROJECTION_HORIZON_DAYS = 366
# Per account, and a refusal rather than a cut (see the module docstring). The
# horizon above already bounds one pattern at roughly 65 weekly occurrences, so
# this is a tripwire for an account carrying dozens of patterns, not a page size.
DEFAULT_MAX_LINE_ITEMS = 500


class ProjectionTooBroadError(ValueError):
    """More line items than the caller asked to be served in one answer."""


@dataclass(frozen=True)
class RecurringInput:
    """One detected pattern, exactly as ``recurring_patterns`` stores it.

    ``expected_amount`` is the stored magnitude, sign and all: this module
    ``abs()``es it rather than trusting it, because three other layers do the
    same and a sign that survived here would silently invert a direction that
    ``pattern_type`` had already decided.
    """

    pattern_id: str
    merchant: str
    category: str
    pattern_type: str
    frequency: str
    expected_day_of_period: int
    expected_amount: Decimal
    next_expected_date: date
    # The detector's own earliest ACCEPTED occurrence for this pattern. It floors
    # the backward half of the window: an anchor older than the pattern must not
    # manufacture flows from before anything was ever observed.
    first_occurrence: date
    missed_count: int
    confidence: float
    is_active: bool


@dataclass(frozen=True)
class ProjectionLine:
    """One expected flow, with everything that was derived to place it.

    ``amount`` is SIGNED and cent-quantized: positive is money in. ``next_expected
    _date`` is the stored value and ``projected_date`` is where this occurrence
    actually falls, so the two together plus ``periods_from_next_expected`` let a
    caller reproduce the derivation instead of taking it on trust.
    """

    pattern_id: str
    merchant: str
    category: str
    pattern_type: str
    frequency: str
    amount: Decimal
    projected_date: date
    next_expected_date: date
    # SIGNED whole periods between the stored date and this occurrence. It counts
    # OCCURRENCES, not staleness: the fourth occurrence of a perfectly current
    # pattern is +3 and zero periods late. Negative means this occurrence PRECEDES
    # the stored date, i.e. the detector had already accepted one for it.
    periods_from_next_expected: int
    # Whether the PATTERN's stored date has passed as of the query. A property of
    # the pattern, identical on each of its lines, and the field to sort by when
    # hunting stale patterns -- `periods_from_next_expected` is not that field.
    next_expected_is_retrospective: bool
    # `projected_date < as_of` AND this occurrence is not one the detector already
    # accepted. NOT read from `missed_count`, which floors to zero for anything
    # less than one full period late. It is an inference from the pattern's own
    # accepted occurrences, never an assertion that nothing arrived.
    is_overdue: bool
    missed_count: int
    confidence: float


@dataclass(frozen=True)
class PatternDisposition:
    """What became of every pattern considered, by count.

    ``total`` is the population, so ``projected`` plus the omissions equals the
    number of patterns attributed to the account. A caller reading zero line
    items can therefore tell "nothing recurs here" from "four things recur here
    and all four were omitted", which are the same empty list.
    """

    projected: int = 0
    inactive: int = 0
    transfer_direction_ambiguous: int = 0
    unclassified_pattern_type: int = 0
    unsupported_frequency: int = 0
    no_occurrence_in_window: int = 0

    @property
    def total(self) -> int:
        return (
            self.projected
            + self.inactive
            + self.transfer_direction_ambiguous
            + self.unclassified_pattern_type
            + self.unsupported_frequency
            + self.no_occurrence_in_window
        )


@dataclass(frozen=True)
class Projection:
    """The line items over one window, and their exact sum."""

    lines: tuple[ProjectionLine, ...]
    net: Decimal
    disposition: PatternDisposition
    # Projectable patterns whose STORED next_expected_date has passed, counted
    # whether or not any of their occurrences landed inside the window.
    #
    # A per-line `is_overdue` cannot carry this. Occurrences at or before the
    # anchor are outside the window by construction, so a genuinely late pattern
    # reports `is_overdue` false on every line the moment the anchor advances
    # past its stored date -- the signal would degrade exactly as the ledger
    # became more current. Keep the pattern-level signal even when no old line
    # remains inside the window.
    patterns_past_due: int = 0


def validate_window(*, as_of: date, target_date: date) -> None:
    """Refuse a request the line-item list cannot be finite for.

    Raises:
        ValueError: when the target date precedes the as-of date, or reaches
            more than :data:`PROJECTION_HORIZON_DAYS` past it.
    """
    if target_date < as_of:
        raise ValueError("target_date precedes as_of")
    if (target_date - as_of).days > PROJECTION_HORIZON_DAYS:
        raise ValueError("target_date is beyond the published projection horizon")


def align_to_window(
    next_expected: date,
    *,
    frequency: str,
    expected_day: int,
    not_before: date,
) -> tuple[date, int]:
    """Find the pattern's first occurrence at or after ``not_before``.

    Returns that occurrence and how many whole periods it sits from the STORED
    ``next_expected_date``, SIGNED: positive when the stored date had to be
    advanced, zero when the stored date is itself the first one in scope, and
    NEGATIVE when the window opens before the stored date and the cadence had to
    be walked backwards to reach it.

    The backward leg is not an embellishment. ``next_expected_date`` is stored one
    period after the last occurrence the detector ACCEPTED, so a window that opens
    at a statement anchor older than that date contains occurrences the stored
    date sits after. Advancing only -- the obvious reading of "roll forward" --
    leaves every one of them in no term at all: the absolute balance series stops
    at the anchor date and the flows would start at the stored date. The
    reconciliation identity cannot detect a line item dropped from both sides.

    Stepping is delegated to ``pattern_detector.next_occurrence`` and its exact
    inverse ``previous_occurrence``, the same functions that produced the stored
    date, so a month-end biller clamps here exactly as it clamped there. Both
    clamp per period against the pattern's OWN ``expected_day``, so a 31st biller
    that clamped to February's 28th returns to the 31st in March rather than
    drifting, in either direction.

    Raises:
        ValueError: for a frequency ``next_occurrence`` does not recognize. That
            function falls through to ``last + 30 days``, which is a guess; a
            guess inside a figure that reconciles to the cent cannot be told
            apart from an observation.
    """
    if frequency not in RECURRING_FREQUENCIES:
        raise ValueError(f"unsupported recurring frequency: {frequency!r}")
    occurrence = next_expected
    periods = 0
    while occurrence < not_before:
        occurrence = next_occurrence(occurrence, frequency, expected_day)
        periods += 1
    while True:
        earlier = previous_occurrence(occurrence, frequency, expected_day)
        if earlier < not_before:
            return occurrence, periods
        occurrence, periods = earlier, periods - 1


def _omission_reason(pattern: RecurringInput) -> str | None:
    """Why this pattern cannot be projected, or None if it can be.

    Ordered by what the caller would do about it: a lapsed pattern needs no
    action, an ambiguous direction needs a transfer surface, an unknown type or
    cadence needs the detector to learn something.
    """
    if not pattern.is_active:
        return REASON_INACTIVE
    if pattern.pattern_type == "transfer":
        return REASON_TRANSFER
    if pattern.pattern_type not in DIRECTION_BY_PATTERN_TYPE:
        return REASON_UNCLASSIFIED_TYPE
    if pattern.frequency not in RECURRING_FREQUENCIES:
        return REASON_UNSUPPORTED_FREQUENCY
    return None


def _occurrences(
    pattern: RecurringInput,
    *,
    not_before: date,
    through: date,
) -> list[tuple[date, int]]:
    """Every expected occurrence in ``[not_before, through]``, with its period offset.

    The lower bound is raised to the pattern's own ``first_occurrence``: an anchor
    older than the pattern would otherwise manufacture flows from before anything
    was ever observed, which is inventing history rather than projecting it.
    """
    occurrence, periods = align_to_window(
        pattern.next_expected_date,
        frequency=pattern.frequency,
        expected_day=pattern.expected_day_of_period,
        not_before=max(not_before, pattern.first_occurrence),
    )
    found: list[tuple[date, int]] = []
    while occurrence <= through:
        found.append((occurrence, periods))
        occurrence = next_occurrence(occurrence, pattern.frequency, pattern.expected_day_of_period)
        periods += 1
    return found


def build_projection(
    patterns: Iterable[RecurringInput],
    *,
    anchor_date: date,
    target_date: date,
    as_of: date,
    max_line_items: int = DEFAULT_MAX_LINE_ITEMS,
) -> Projection:
    """The expected flows between an anchor and a target date, and their sum.

    The window is ``(anchor_date, target_date]`` -- open at the anchor and closed
    at the target. Open at the anchor because the absolute balance series is
    daily and each row already contains that day's postings, so a flow projected
    ON the anchor date would be counted twice; closed at the target because the
    question is whether an account covers something on a named day, which
    includes what lands that day.

    That window is why an elapsed-but-unimported flow is a line item rather than
    a discrepancy (D6): an anchor may predate the evaluation date, and expected
    occurrences between them are outside the anchored balance.

    Raises:
        ValueError: for a window :func:`validate_window` refuses, or an ``as_of``
            before the anchor -- which would make the roll-forward and the
            overdue test disagree about which way time runs.
        ProjectionTooBroadError: when the window yields more than ``max_line_items``.
    """
    validate_window(as_of=as_of, target_date=target_date)
    if as_of < anchor_date:
        raise ValueError("as_of precedes the anchor date")

    lines: list[ProjectionLine] = []
    counts = dict.fromkeys(DISPOSITIONS, 0)
    past_due = 0
    for pattern in patterns:
        reason = _omission_reason(pattern)
        if reason is not None:
            counts[reason] += 1
            continue
        retrospective = pattern.next_expected_date < as_of
        # Counted BEFORE the window is applied, so a pattern that is late and has
        # no late occurrence left inside the window is still reported as late.
        past_due += int(retrospective)
        occurrences = _occurrences(
            pattern,
            not_before=anchor_date + timedelta(days=1),
            through=target_date,
        )
        if not occurrences:
            counts[REASON_NO_OCCURRENCE] += 1
            continue
        # BEFORE the flows are materialized, so the cap bounds the list rather
        # than reporting on one already built -- the same rule the query layer's
        # scan caps follow.
        if len(lines) + len(occurrences) > max_line_items:
            raise ProjectionTooBroadError(f"more than {max_line_items} projected flows")
        counts[DISPOSITION_PROJECTED] += 1
        # Quantized ONCE, here, and never re-rounded: the sum below is over the
        # exact values the wire carries.
        magnitude = to_cents(abs(pattern.expected_amount))
        amount = magnitude * DIRECTION_BY_PATTERN_TYPE[pattern.pattern_type]
        for projected_date, periods in occurrences:
            lines.append(
                ProjectionLine(
                    pattern_id=pattern.pattern_id,
                    merchant=pattern.merchant,
                    category=pattern.category,
                    pattern_type=pattern.pattern_type,
                    frequency=pattern.frequency,
                    amount=amount,
                    projected_date=projected_date,
                    next_expected_date=pattern.next_expected_date,
                    periods_from_next_expected=periods,
                    next_expected_is_retrospective=retrospective,
                    # A negative offset names an occurrence the detector already
                    # accepted into this pattern, so calling it a missed payment
                    # would invert the fact it was derived from.
                    is_overdue=projected_date < as_of and periods >= 0,
                    missed_count=pattern.missed_count,
                    confidence=pattern.confidence,
                )
            )

    ordered = _ordered(lines)
    return Projection(
        lines=ordered,
        net=sum((line.amount for line in ordered), start=Decimal("0.00")),
        disposition=PatternDisposition(**counts),
        patterns_past_due=past_due,
    )


def _ordered(lines: Sequence[ProjectionLine]) -> tuple[ProjectionLine, ...]:
    """Chronological, then by merchant, then by the pattern's own identifier.

    ``pattern_id`` closes the ordering rather than reaching the caller: it is a
    hash of the pattern's full-precision running average and is reminted on every
    analysis refresh, so it is stable within one answer and meaningless outside
    it. Two patterns for the same merchant on the same day are otherwise served
    in whatever order the rows arrived.
    """
    return tuple(
        sorted(lines, key=lambda line: (line.projected_date, line.merchant, line.pattern_id))
    )
