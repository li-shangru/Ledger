"""Projection contracts exercised with independently authored small schedules.

The shared scenario combines a weekly bill, quarterly subscription and monthly
income. Separate cases choose their own calendars for cadence stepping, start
bounds and rounding. No scenario represents a household or employment history.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from ledger.analysis.projection import (
    DIRECTION_BY_PATTERN_TYPE,
    DISPOSITIONS,
    OMISSION_REASONS,
    PROJECTION_HORIZON_DAYS,
    PatternDisposition,
    ProjectionTooBroadError,
    RecurringInput,
    align_to_window,
    build_projection,
    validate_window,
)


def _pattern(
    merchant: str,
    *,
    pattern_type: str,
    frequency: str,
    expected_day: int,
    amount: str,
    next_expected: date,
    is_active: bool = True,
    missed_count: int = 0,
    confidence: float = 0.85,
    category: str = "Synthetic expense",
    first_occurrence: date = date(2046, 1, 1),
) -> RecurringInput:
    return RecurringInput(
        pattern_id=f"pattern:{merchant}",
        merchant=merchant,
        category=category,
        pattern_type=pattern_type,
        frequency=frequency,
        expected_day_of_period=expected_day,
        expected_amount=Decimal(amount),
        next_expected_date=next_expected,
        first_occurrence=first_occurrence,
        missed_count=missed_count,
        confidence=confidence,
        is_active=is_active,
    )


ANCHOR = date(2047, 5, 18)
AS_OF = date(2047, 6, 4)
TARGET = date(2047, 6, 30)

# One accepted bill occurrence lies inside the window. Its stored next date is
# five days before AS_OF, less than the seven-day cadence, with missed_count=0.
BILL = _pattern(
    "SYNTHETIC BILL",
    pattern_type="bill",
    frequency="weekly",
    expected_day=3,
    amount="17.35",
    next_expected=date(2047, 5, 30),
    first_occurrence=date(2047, 5, 23),
)
SUBSCRIPTION = _pattern(
    "SYNTHETIC SUBSCRIPTION",
    pattern_type="subscription",
    frequency="quarterly",
    expected_day=11,
    amount="8.40",
    next_expected=date(2047, 6, 11),
    first_occurrence=date(2047, 3, 11),
)
INCOME = _pattern(
    "SYNTHETIC INCOME",
    pattern_type="income",
    frequency="monthly",
    expected_day=21,
    amount="123.45",
    next_expected=date(2047, 6, 21),
    first_occurrence=date(2047, 4, 21),
    category="Synthetic income",
)
INACTIVE_INCOME = _pattern(
    "SYNTHETIC INACTIVE INCOME",
    pattern_type="income",
    frequency="annual",
    expected_day=17,
    amount="77.00",
    next_expected=date(2044, 12, 17),
    first_occurrence=date(2043, 12, 17),
    is_active=False,
    missed_count=2,
    category="Synthetic income",
)
TRANSFER = _pattern(
    "SYNTHETIC TRANSFER",
    pattern_type="transfer",
    frequency="monthly",
    expected_day=9,
    amount="31.20",
    next_expected=date(2047, 6, 9),
)
UNCLASSIFIED = _pattern(
    "SYNTHETIC UNCLASSIFIED",
    pattern_type="reimbursement",
    frequency="quarterly",
    expected_day=16,
    amount="6.25",
    next_expected=date(2047, 6, 16),
)
ODD_CADENCE = _pattern(
    "SYNTHETIC ODD CADENCE",
    pattern_type="bill",
    frequency="every-ten-days",
    expected_day=2,
    amount="4.75",
    next_expected=date(2047, 6, 2),
)


def _future_bill() -> RecurringInput:
    return _pattern(
        "SYNTHETIC FUTURE BILL",
        pattern_type="bill",
        frequency="annual",
        expected_day=7,
        amount="29.95",
        next_expected=date(2048, 1, 7),
        first_occurrence=date(2046, 1, 7),
    )


def _plan(patterns, *, target_date=TARGET, anchor_date=ANCHOR, as_of=AS_OF, **kwargs):
    return build_projection(
        patterns, anchor_date=anchor_date, target_date=target_date, as_of=as_of, **kwargs
    )


def test_a_pattern_four_days_past_on_a_seven_day_period_rolls_forward_one() -> None:
    rolled, periods = align_to_window(
        date(2053, 2, 2), frequency="weekly", expected_day=6, not_before=date(2053, 2, 6)
    )

    assert periods == 1
    assert rolled == date(2053, 2, 9)
    assert rolled - date(2053, 2, 6) == timedelta(days=3)


def test_a_pattern_already_in_the_future_rolls_forward_zero() -> None:
    stored = date(2050, 10, 23)
    rolled, periods = align_to_window(
        stored, frequency="monthly", expected_day=23, not_before=date(2050, 10, 20)
    )

    assert (rolled, periods) == (stored, 0)


def test_a_pattern_due_exactly_on_the_boundary_rolls_forward_zero() -> None:
    boundary = date(2050, 2, 14)

    assert align_to_window(
        boundary, frequency="quarterly", expected_day=14, not_before=boundary
    ) == (boundary, 0)


def test_rolling_forward_many_periods_reports_every_one_of_them() -> None:
    rolled, periods = align_to_window(
        date(2044, 12, 17), frequency="annual", expected_day=17, not_before=AS_OF
    )

    assert periods == 3
    assert rolled == date(2047, 12, 17)


def test_a_month_end_pattern_does_not_drift_earlier_each_period() -> None:
    """February clamps to 28, March returns to 31, and April clamps to 30."""
    rolled, periods = align_to_window(
        date(2051, 1, 31), frequency="monthly", expected_day=31, not_before=date(2051, 4, 1)
    )

    assert (rolled, periods) == (date(2051, 4, 30), 3)


def test_an_unsupported_frequency_is_refused_rather_than_guessed_at_thirty_days() -> None:
    with pytest.raises(ValueError):
        align_to_window(
            date(2054, 9, 3),
            frequency="every-ten-days",
            expected_day=3,
            not_before=date(2054, 9, 14),
        )


def test_direction_comes_from_pattern_type_and_never_from_the_amount() -> None:
    plan = _plan([BILL, INCOME, SUBSCRIPTION], target_date=date(2047, 6, 11))
    by_merchant = {line.merchant: line for line in plan.lines}

    assert by_merchant["SYNTHETIC BILL"].amount == Decimal("-17.35")
    assert by_merchant["SYNTHETIC INCOME"].amount == Decimal("123.45")
    assert by_merchant["SYNTHETIC SUBSCRIPTION"].amount == Decimal("-8.40")
    assert DIRECTION_BY_PATTERN_TYPE == {"income": 1, "bill": -1, "subscription": -1}


def test_a_stored_magnitude_that_carries_a_sign_does_not_flip_a_direction() -> None:
    signed = _pattern(
        "SYNTHETIC SIGNED INCOME",
        pattern_type="income",
        frequency="monthly",
        expected_day=21,
        amount="-123.45",
        next_expected=date(2047, 6, 21),
    )
    plan = _plan([signed], target_date=date(2047, 6, 21))

    assert len(plan.lines) == 2
    assert {line.amount for line in plan.lines} == {Decimal("123.45")}


def test_a_transfer_typed_pattern_is_excluded_with_a_stated_reason() -> None:
    plan = _plan([TRANSFER])

    assert plan.lines == ()
    assert plan.disposition.transfer_direction_ambiguous == 1
    assert "transfer_direction_ambiguous" in OMISSION_REASONS


def test_a_pattern_type_with_no_published_direction_is_excluded_not_guessed() -> None:
    plan = _plan([UNCLASSIFIED])

    assert plan.lines == ()
    assert plan.disposition.unclassified_pattern_type == 1


def test_the_window_opens_after_the_anchor_date_not_on_it() -> None:
    """The anchor includes its own day, so only the next quarter appears."""
    on_the_anchor = _pattern(
        "SYNTHETIC ANCHOR SUBSCRIPTION",
        pattern_type="subscription",
        frequency="quarterly",
        expected_day=18,
        amount="12.60",
        next_expected=ANCHOR,
    )
    (line,) = _plan([on_the_anchor], target_date=date(2047, 9, 1)).lines

    assert line.projected_date == date(2047, 8, 18)
    assert line.periods_from_next_expected == 1


def test_an_elapsed_but_unimported_flow_is_a_disclosed_line_item() -> None:
    """The stored May 30 bill is five days late and remains inside the window."""
    plan = _plan([BILL], target_date=AS_OF)

    assert [(line.projected_date, line.periods_from_next_expected) for line in plan.lines] == [
        (date(2047, 5, 23), -1),
        (date(2047, 5, 30), 0),
    ]
    assert plan.lines[0].is_overdue is False
    assert plan.lines[1].is_overdue is True


def test_multiple_occurrences_in_one_window_are_each_disclosed_and_counted() -> None:
    plan = _plan([BILL])

    assert [(line.projected_date, line.periods_from_next_expected) for line in plan.lines] == [
        (date(2047, 5, 23), -1),
        (date(2047, 5, 30), 0),
        (date(2047, 6, 6), 1),
        (date(2047, 6, 13), 2),
        (date(2047, 6, 20), 3),
        (date(2047, 6, 27), 4),
    ]
    assert plan.disposition.projected == 1
    assert plan.net == Decimal("-104.10")


def test_an_occurrence_between_the_anchor_and_the_stored_date_is_inside_the_window() -> None:
    """Monthly income requires one backward calendar step, then its stored date."""
    plan = _plan([INCOME], target_date=date(2047, 6, 21))

    assert [(line.projected_date, line.periods_from_next_expected) for line in plan.lines] == [
        (date(2047, 5, 21), -1),
        (date(2047, 6, 21), 0),
    ]
    assert plan.net == Decimal("123.45") * 2
    assert plan.net == Decimal("246.90")


def test_the_window_never_steps_back_past_the_pattern_s_first_occurrence() -> None:
    started_late = _pattern(
        "SYNTHETIC NEW SUBSCRIPTION",
        pattern_type="subscription",
        frequency="quarterly",
        expected_day=24,
        amount="6.80",
        next_expected=date(2050, 10, 24),
        first_occurrence=date(2050, 4, 24),
    )
    plan = _plan(
        [started_late],
        target_date=date(2050, 10, 24),
        anchor_date=date(2050, 1, 1),
        as_of=date(2050, 10, 24),
    )

    assert [line.projected_date for line in plan.lines] == [
        date(2050, 4, 24),
        date(2050, 7, 24),
        date(2050, 10, 24),
    ]
    assert plan.net == Decimal("-20.40")


def test_an_occurrence_the_detector_already_accepted_is_not_reported_overdue() -> None:
    plan = _plan([INCOME], target_date=date(2047, 6, 21))
    already_seen = plan.lines[0]

    assert already_seen.periods_from_next_expected == -1
    assert already_seen.projected_date == date(2047, 5, 21)
    assert already_seen.projected_date < AS_OF
    assert already_seen.is_overdue is False


def test_a_pattern_whose_next_occurrence_is_past_the_target_is_counted_not_dropped() -> None:
    plan = _plan([_future_bill()], target_date=date(2047, 6, 11))

    assert plan.lines == ()
    assert plan.disposition.no_occurrence_in_window == 1
    assert plan.net == Decimal("0.00")


def test_an_inactive_pattern_is_not_projected_and_is_counted() -> None:
    plan = _plan([INACTIVE_INCOME])

    assert plan.lines == ()
    assert plan.disposition.inactive == 1


def test_the_lines_are_ordered_by_the_date_they_are_projected_for() -> None:
    plan = _plan([SUBSCRIPTION, BILL, INCOME])

    assert [line.projected_date for line in plan.lines] == [
        date(2047, 5, 21),
        date(2047, 5, 23),
        date(2047, 5, 30),
        date(2047, 6, 6),
        date(2047, 6, 11),
        date(2047, 6, 13),
        date(2047, 6, 20),
        date(2047, 6, 21),
        date(2047, 6, 27),
    ]


def test_overdue_is_computed_here_and_never_read_from_missed_count() -> None:
    """Five days late floors to zero missed weekly periods."""
    plan = _plan([BILL])
    by_offset = {line.periods_from_next_expected: line for line in plan.lines}

    assert by_offset[0].is_overdue is True
    assert by_offset[0].missed_count == 0
    assert by_offset[1].is_overdue is False


def test_overdue_is_measured_against_the_supplied_as_of_and_no_clock() -> None:
    on_the_day = _plan([BILL], as_of=date(2047, 5, 30))
    later = _plan([BILL], as_of=AS_OF)

    assert on_the_day.lines[1].is_overdue is False
    assert later.lines[1].is_overdue is True


def test_a_past_due_pattern_is_counted_even_when_the_anchor_moved_past_it() -> None:
    """Advancing the anchor hides old lines without clearing the pattern's lateness."""
    stale_anchor = _plan([BILL])
    fresh_anchor = _plan([BILL], anchor_date=date(2047, 5, 31))

    assert any(line.is_overdue for line in stale_anchor.lines)
    assert not any(line.is_overdue for line in fresh_anchor.lines)
    assert stale_anchor.patterns_past_due == fresh_anchor.patterns_past_due == 1


def test_a_pattern_with_no_occurrence_in_the_window_still_counts_as_past_due() -> None:
    plan = _plan(
        [BILL],
        target_date=date(2047, 6, 1),
        anchor_date=date(2047, 6, 1),
        as_of=date(2047, 6, 1),
    )

    assert plan.lines == ()
    assert plan.disposition.no_occurrence_in_window == 1
    assert plan.patterns_past_due == 1


def test_a_pattern_whose_stored_date_is_still_ahead_is_not_counted_past_due() -> None:
    plan = _plan([INCOME])

    assert plan.lines
    assert plan.patterns_past_due == 0
    assert all(line.next_expected_is_retrospective is False for line in plan.lines)


def test_a_far_future_occurrence_of_a_current_pattern_is_not_labelled_stale() -> None:
    current = _plan([INCOME], target_date=date(2047, 9, 30))
    retrospective = _plan([BILL], target_date=date(2047, 9, 30))

    assert max(line.periods_from_next_expected for line in current.lines) == 3
    assert {line.next_expected_is_retrospective for line in current.lines} == {False}
    assert {line.next_expected_is_retrospective for line in retrospective.lines} == {True}


def test_the_sum_of_the_line_items_is_the_projected_delta_to_the_cent() -> None:
    plan = _plan([BILL, SUBSCRIPTION, INCOME])

    assert len(plan.lines) == 9
    assert plan.net == sum((line.amount for line in plan.lines), start=Decimal("0.00"))
    assert all(line.amount == line.amount.quantize(Decimal("0.01")) for line in plan.lines)
    assert plan.net == plan.net.quantize(Decimal("0.01"))
    assert plan.net == Decimal("-17.35") * 6 - Decimal("8.40") + Decimal("123.45") * 2
    assert plan.net == Decimal("134.40")


def test_a_sub_cent_magnitude_is_quantized_per_flow_and_never_at_the_sum() -> None:
    """Seven rounded flows total -23.38; rounding their raw sum gives -23.34."""
    pattern = _pattern(
        "SYNTHETIC FRACTIONAL BILL",
        pattern_type="bill",
        frequency="biweekly",
        expected_day=1,
        amount="3.335",
        next_expected=date(2052, 9, 3),
    )
    plan = _plan(
        [pattern],
        anchor_date=date(2052, 9, 2),
        as_of=date(2052, 9, 3),
        target_date=date(2052, 11, 26),
    )

    assert [line.projected_date for line in plan.lines] == [
        date(2052, 9, 3),
        date(2052, 9, 17),
        date(2052, 10, 1),
        date(2052, 10, 15),
        date(2052, 10, 29),
        date(2052, 11, 12),
        date(2052, 11, 26),
    ]
    assert {line.amount for line in plan.lines} == {Decimal("-3.34")}
    assert plan.net == Decimal("-23.38")
    assert plan.net == sum((line.amount for line in plan.lines), start=Decimal("0.00"))
    assert (Decimal("-3.335") * 7).quantize(Decimal("0.01")) == Decimal("-23.34")


def test_the_cent_quantization_rounds_half_to_even_as_the_wire_states() -> None:
    """The first three magnitudes round down; the last rounds up to an even cent."""
    cases = {"2.345": "-2.34", "11.225": "-11.22", "0.045": "-0.04", "2.355": "-2.36"}

    for magnitude, expected in cases.items():
        pattern = _pattern(
            "SYNTHETIC HALF CENT",
            pattern_type="bill",
            frequency="quarterly",
            expected_day=11,
            amount=magnitude,
            next_expected=date(2047, 6, 11),
        )
        (line,) = _plan([pattern]).lines

        assert line.amount == Decimal(expected), magnitude


def test_every_pattern_lands_in_exactly_one_disposition() -> None:
    patterns = [
        BILL,
        SUBSCRIPTION,
        INCOME,
        _future_bill(),
        INACTIVE_INCOME,
        TRANSFER,
        UNCLASSIFIED,
        ODD_CADENCE,
    ]
    plan = _plan(patterns, target_date=date(2047, 6, 11))

    assert plan.disposition == PatternDisposition(
        projected=3,
        inactive=1,
        transfer_direction_ambiguous=1,
        unclassified_pattern_type=1,
        unsupported_frequency=1,
        no_occurrence_in_window=1,
    )
    assert plan.disposition.total == len(patterns)
    assert sorted(DISPOSITIONS) == sorted(("projected", *OMISSION_REASONS))


def test_the_published_horizon_bounds_the_request_and_is_a_boundary() -> None:
    assert PROJECTION_HORIZON_DAYS == 366
    validate_window(as_of=AS_OF, target_date=AS_OF + timedelta(days=PROJECTION_HORIZON_DAYS))
    with pytest.raises(ValueError):
        validate_window(
            as_of=AS_OF, target_date=AS_OF + timedelta(days=PROJECTION_HORIZON_DAYS + 1)
        )
    with pytest.raises(ValueError):
        _plan([BILL], target_date=AS_OF + timedelta(days=PROJECTION_HORIZON_DAYS + 1))


def test_an_as_of_before_the_anchor_is_refused_rather_than_projected_backwards() -> None:
    with pytest.raises(ValueError):
        _plan([BILL], as_of=ANCHOR - timedelta(days=1))


def test_more_line_items_than_the_cap_refuses_rather_than_truncating() -> None:
    with pytest.raises(ProjectionTooBroadError):
        _plan([BILL], max_line_items=4)


def test_a_target_date_equal_to_the_anchor_leaves_an_empty_half_open_window() -> None:
    plan = _plan([BILL], target_date=ANCHOR, as_of=ANCHOR)

    assert plan.lines == ()
    assert plan.net == Decimal("0.00")
    assert plan.disposition.no_occurrence_in_window == 1


def test_a_target_before_the_anchor_cannot_be_requested_at_all() -> None:
    with pytest.raises(ValueError):
        _plan([BILL], target_date=ANCHOR - timedelta(days=1), as_of=ANCHOR - timedelta(days=1))
