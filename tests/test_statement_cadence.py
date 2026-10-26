"""Statement cadence and gap classification over fictional covered-month series.

The independently authored histories exercise monthly and bimonthly modes,
near ties, sparse history, a trailing silence, interior gaps and explicit closure.
No account labels, calendar histories or measurements come from an owner ledger.
"""

from __future__ import annotations

from datetime import date

import pytest

from ledger.analysis.statement_cadence import (
    DECIDING_SIGNALS,
    DELIVERY_GRACE_DAYS,
    DORMANCY_MULTIPLIER,
    ENTRY_DECIDING_SIGNALS,
    GAP_CLASSIFICATIONS,
    MIN_CADENCE_OBSERVATIONS,
    NO_ENTRY_SIGNALS,
    classify_gap,
    estimate_cadence,
    gap_evaluation_date,
    month_deltas,
    month_index,
)

# --- independently authored covered-month series -----------------------------

GAPLESS_MONTHLY = tuple(f"2041-{month:02d}" for month in range(1, 13))
# Deltas: 1, 2, 1, 2, 1, 2, 1, 3. Monthly narrowly wins four versus three.
IRREGULAR_MONTHLY = (
    "2040-01",
    "2040-02",
    "2040-04",
    "2040-05",
    "2040-07",
    "2040-08",
    "2040-10",
    "2040-11",
    "2041-02",
)
# Deltas: 2, 1, 2, 2, 1, 2, 2. Bimonthly wins by five of seven intervals.
IRREGULAR_BIMONTHLY = (
    "2040-01",
    "2040-03",
    "2040-04",
    "2040-06",
    "2040-08",
    "2040-09",
    "2040-11",
    "2041-01",
)
SHORT_BANK_HISTORY = ("2041-05", "2041-06", "2041-07")
SHORT_CARD_HISTORY = ("2041-06", "2041-07")


def test_the_fictional_series_preserve_their_intended_deltas() -> None:
    """Keep each cadence oracle tied to the intentionally authored intervals."""
    assert len(GAPLESS_MONTHLY) == 12
    assert month_deltas(GAPLESS_MONTHLY) == (1,) * 11
    assert month_deltas(IRREGULAR_MONTHLY) == (1, 2, 1, 2, 1, 2, 1, 3)
    assert month_deltas(IRREGULAR_BIMONTHLY) == (2, 1, 2, 2, 1, 2, 2)


# --- estimate_cadence -------------------------------------------------------


def test_a_gapless_twelve_month_sequence_is_monthly_at_full_confidence() -> None:
    cadence = estimate_cadence(GAPLESS_MONTHLY)

    assert cadence.months == 1
    assert cadence.confidence == pytest.approx(1.0)
    assert cadence.observations == 12
    assert cadence.basis == "modal_month_delta"


def test_the_bimonthly_series_resolves_to_a_two_month_cadence_not_a_monthly_one() -> None:
    """The estimator MUST tolerate a modal gap of 2, or this account is
    permanently overdue for statements that were never due."""
    cadence = estimate_cadence(IRREGULAR_BIMONTHLY)

    assert cadence.months == 2, "a 2-month cadence must not become a monthly obligation"
    assert cadence.modal_intervals == 5
    assert cadence.intervals == 7
    assert cadence.confidence == pytest.approx(5 / 7)


def test_the_near_tie_series_resolves_to_the_modal_delta_which_is_one_not_two() -> None:
    """A minority of longer gaps must not override the modal one-month interval."""
    cadence = estimate_cadence(IRREGULAR_MONTHLY)

    assert cadence.months == 1
    assert cadence.modal_intervals == 4
    assert cadence.intervals == 8
    assert cadence.confidence == pytest.approx(4 / 8)


def test_a_tie_resolves_to_the_longer_interval() -> None:
    """A tie means the account is irregular, and the two errors are not equal.

    Choosing the shorter interval manufactures the false `overdue` entries §2.2
    measured; choosing the longer one delays a true one by a single period. The
    tie-break is explicit so it cannot ride on `Counter.most_common`'s insertion
    order, which depends on which delta happened to appear first.
    """
    assert estimate_cadence(("2040-01", "2040-02", "2040-04", "2040-05", "2040-07")).months == 2


def test_three_observations_claim_nothing() -> None:
    cadence = estimate_cadence(SHORT_BANK_HISTORY)

    assert cadence.months is None
    assert cadence.observations == 3
    assert cadence.basis == "insufficient_history"
    assert cadence.confidence == pytest.approx(0.0)


def test_two_observations_claim_nothing() -> None:
    assert estimate_cadence(SHORT_CARD_HISTORY).months is None


def test_one_observation_claims_nothing() -> None:
    assert estimate_cadence(("2041-07",)).months is None


def test_no_observations_claim_nothing() -> None:
    assert estimate_cadence(()).months is None


def test_the_minimum_is_four_observations_and_it_is_a_boundary() -> None:
    assert MIN_CADENCE_OBSERVATIONS == 4
    assert estimate_cadence(("2040-01", "2040-02", "2040-03")).months is None
    assert estimate_cadence(("2040-01", "2040-02", "2040-03", "2040-04")).months == 1


def test_duplicate_and_unsorted_periods_do_not_invent_a_zero_delta() -> None:
    """SQL supplies these sorted and distinct; the estimator does not rely on it.

    A duplicate would otherwise contribute a 0-month delta, and a 0 cadence makes
    every gap infinite.
    """
    cadence = estimate_cadence(("2040-04", "2040-01", "2040-02", "2040-02", "2040-03"))

    assert cadence.months == 1
    assert cadence.observations == 4


def test_a_malformed_period_is_refused_rather_than_silently_scored() -> None:
    with pytest.raises(ValueError):
        estimate_cadence(("2040-01", "2040-1", "2040-03", "2040-04"))


# --- classify_gap -----------------------------------------------------------


def test_an_account_within_its_own_cadence_has_no_gap() -> None:
    """The current month has not ended, so its statement cannot be overdue."""
    assessment = classify_gap(
        last_covered="2041-07",
        cadence=estimate_cadence(GAPLESS_MONTHLY),
        today=date(2041, 8, 10),
        lifecycle_status=None,
    )

    assert assessment.classification is None
    assert assessment.deciding_signal == "within_cadence"
    assert assessment.elapsed_months == 1
    assert assessment.missing_periods == ()


def test_overdue_fires_one_full_cadence_past_the_last_covered_period() -> None:
    assessment = classify_gap(
        last_covered="2041-07",
        cadence=estimate_cadence(GAPLESS_MONTHLY),
        today=date(2041, 9, 10),
        lifecycle_status=None,
    )

    assert assessment.classification == "overdue"
    assert assessment.deciding_signal == "elapsed_beyond_one_cadence"
    assert assessment.elapsed_months == 2
    assert assessment.missing_periods == ("2041-08",)


# --- the delivery grace at the period boundary -------------------------------


def test_a_statement_is_not_overdue_the_day_after_its_period_closed() -> None:
    """Without a grace window a monthly account reads `overdue` on the first
    day of each month, before any institution could plausibly have delivered the statement -- which
    is the false-positive class this whole feature exists to remove.

    `elapsed_months` still reports the raw arithmetic (2), because it is a
    measurement rather than a verdict; the SIGNAL is what says grace decided it.
    """
    assessment = classify_gap(
        last_covered="2041-07",
        cadence=estimate_cadence(GAPLESS_MONTHLY),
        today=date(2041, 9, 1),
        lifecycle_status=None,
    )

    assert assessment.classification is None
    assert assessment.deciding_signal == "within_delivery_grace"
    assert assessment.elapsed_months == 2
    assert assessment.missing_periods == ()


def test_the_grace_window_is_a_published_number_of_days_and_is_a_boundary() -> None:
    """A caller cannot reproduce a verdict it cannot count days against."""
    assert DELIVERY_GRACE_DAYS == 5

    def _signal(day: int) -> str:
        return classify_gap(
            last_covered="2041-07",
            cadence=estimate_cadence(GAPLESS_MONTHLY),
            today=date(2041, 9, day),
            lifecycle_status=None,
        ).deciding_signal

    assert _signal(DELIVERY_GRACE_DAYS) == "within_delivery_grace"
    assert _signal(DELIVERY_GRACE_DAYS + 1) == "elapsed_beyond_one_cadence"


def test_grace_never_suppresses_a_statement_that_was_already_late() -> None:
    """The window forgives the just-closed period only. An account two cadences
    behind on the first of the month is still overdue, and the period it is
    missing is still named."""
    assessment = classify_gap(
        last_covered="2041-06",
        cadence=estimate_cadence(GAPLESS_MONTHLY),
        today=date(2041, 9, 1),
        lifecycle_status=None,
    )

    assert assessment.classification == "overdue"
    assert assessment.deciding_signal == "elapsed_beyond_one_cadence"
    assert assessment.missing_periods == ("2041-07",), (
        "2041-08 closed one day ago and is inside the grace window"
    )


def test_within_cadence_and_within_grace_are_different_facts() -> None:
    """An account with nothing outstanding at all must not be reported as one
    whose statement is merely being waited on."""
    assessment = classify_gap(
        last_covered="2041-08",
        cadence=estimate_cadence(GAPLESS_MONTHLY),
        today=date(2041, 9, 1),
        lifecycle_status=None,
    )

    assert assessment.classification is None
    assert assessment.deciding_signal == "within_cadence"


def test_neither_no_gap_signal_can_appear_on_an_entry() -> None:
    """Both decide that there is NO entry, so publishing either in the entry
    vocabulary would advertise a value no answer can carry."""
    assert set(NO_ENTRY_SIGNALS) == {"within_cadence", "within_delivery_grace"}
    assert set(ENTRY_DECIDING_SIGNALS) == set(DECIDING_SIGNALS) - set(NO_ENTRY_SIGNALS)
    assert set(NO_ENTRY_SIGNALS) <= set(DECIDING_SIGNALS)


# --- the seam the CLI and the query layer must share -------------------------


def test_the_evaluation_date_seam_is_this_modules_and_is_the_local_calendar() -> None:
    """One seam, or the CLI and `ledger.coverage` classify against two clocks --
    and a test that patches one of them proves nothing about the other."""
    assert gap_evaluation_date() == date.today()


# --- retrospective replay over fictional irregular histories -------------------


def _months_reading_overdue(periods: tuple[str, ...]) -> tuple[str, ...]:
    """Replay an account's own history, classifying at the 15th of each month.

    The 15th so the delivery grace is never what decides an answer: this measures
    the cadence rule, not the boundary window.
    """
    covered = sorted(periods)
    flagged: list[str] = []
    cadence = estimate_cadence(covered)
    first, last = month_index(covered[0]), month_index(covered[-1])
    for index in range(first + 1, last + 1):
        year, month = divmod(index, 12)
        seen = [period for period in covered if month_index(period) <= index]
        assessment = classify_gap(
            last_covered=seen[-1],
            cadence=cadence,
            today=date(year, month + 1, 15),
            lifecycle_status=None,
        )
        if assessment.classification == "overdue":
            flagged.append(f"{year:04d}-{month + 1:02d}")
    return tuple(flagged)


def test_replaying_each_laggards_history_shows_what_the_cadence_rule_removed() -> None:
    """Interior gaps are not all overdue: classify only the trailing silence.

    The monthly series has one three-month interval; its final missing month
    is overdue. Every bimonthly interval is short enough to remain within cadence.
    """
    uncovered_monthly = {
        f"{divmod(index, 12)[0]:04d}-{divmod(index, 12)[1] + 1:02d}"
        for index in range(month_index(IRREGULAR_MONTHLY[0]), month_index(IRREGULAR_MONTHLY[-1]))
    } - set(IRREGULAR_MONTHLY)

    assert len(uncovered_monthly) == 5
    assert _months_reading_overdue(IRREGULAR_MONTHLY) == ("2041-01",)
    assert _months_reading_overdue(IRREGULAR_BIMONTHLY) == ()


def test_the_missing_periods_step_by_the_accounts_own_cadence() -> None:
    """A 2-month account is not missing the months between its statements.

    Naming 2041-06 for a bimonthly series would be exactly the false positive the
    cadence estimate exists to remove.
    """
    assessment = classify_gap(
        last_covered="2041-05",
        cadence=estimate_cadence(IRREGULAR_BIMONTHLY),
        today=date(2041, 8, 10),
        lifecycle_status=None,
    )

    assert assessment.classification == "overdue"
    assert assessment.missing_periods == ("2041-07",)


def test_a_gap_past_the_dormancy_multiple_is_dormant() -> None:
    assessment = classify_gap(
        last_covered="2041-01",
        cadence=estimate_cadence(GAPLESS_MONTHLY),
        today=date(2041, 8, 10),
        lifecycle_status=None,
    )

    assert DORMANCY_MULTIPLIER == 3
    assert assessment.elapsed_months == 7
    assert assessment.classification == "dormant"
    assert assessment.deciding_signal == "elapsed_beyond_cadence_dormancy_multiple"


def test_the_dormancy_multiple_is_relative_to_the_accounts_own_cadence() -> None:
    """Six elapsed months is dormant for a monthly account and overdue for a
    two-month one -- 6 > 3x1 but not > 3x2. A fixed month threshold cannot
    express that, and it is the whole reason the cadence is estimated."""
    monthly = classify_gap(
        last_covered="2041-02",
        cadence=estimate_cadence(GAPLESS_MONTHLY),
        today=date(2041, 8, 10),
        lifecycle_status=None,
    )
    two_monthly = classify_gap(
        last_covered="2041-02",
        cadence=estimate_cadence(IRREGULAR_BIMONTHLY),
        today=date(2041, 8, 10),
        lifecycle_status=None,
    )

    assert monthly.elapsed_months == two_monthly.elapsed_months == 6
    assert monthly.classification == "dormant"
    assert two_monthly.classification == "overdue"


def test_an_operator_recorded_closure_is_dormant_and_says_so() -> None:
    assessment = classify_gap(
        last_covered="2041-07",
        cadence=estimate_cadence(GAPLESS_MONTHLY),
        today=date(2041, 12, 1),
        lifecycle_status="closed",
    )

    assert assessment.classification == "dormant"
    assert assessment.deciding_signal == "operator_recorded_closure"
    assert assessment.missing_periods == (), (
        "a closed account is not missing statements; naming some contradicts the closure"
    )


def test_a_recorded_active_status_never_suppresses_a_gap() -> None:
    """`reopen` records that the account is open. It must not become a mute."""
    assessment = classify_gap(
        last_covered="2041-01",
        cadence=estimate_cadence(GAPLESS_MONTHLY),
        today=date(2041, 8, 10),
        lifecycle_status="active",
    )

    assert assessment.classification == "dormant"
    assert assessment.deciding_signal == "elapsed_beyond_cadence_dormancy_multiple"


def test_an_unknown_cadence_claims_no_gap_however_long_the_silence() -> None:
    assessment = classify_gap(
        last_covered="2041-05",
        cadence=estimate_cadence(SHORT_CARD_HISTORY),
        today=date(2048, 1, 1),
        lifecycle_status=None,
    )

    assert assessment.classification == "unknown"
    assert assessment.deciding_signal == "cadence_unknown_insufficient_history"
    assert assessment.missing_periods == ()


def test_closure_outranks_an_unknown_cadence() -> None:
    """The operator's own record is the strongest signal there is, and an account
    closed before it built four statements is the ordinary case."""
    assessment = classify_gap(
        last_covered="2041-07",
        cadence=estimate_cadence(SHORT_CARD_HISTORY),
        today=date(2041, 12, 1),
        lifecycle_status="closed",
    )

    assert assessment.classification == "dormant"
    assert assessment.deciding_signal == "operator_recorded_closure"


def test_a_period_in_the_future_never_reads_as_a_gap() -> None:
    """Clock skew, or a statement period stamped ahead. Neither is a gap."""
    assessment = classify_gap(
        last_covered="2041-12",
        cadence=estimate_cadence(GAPLESS_MONTHLY),
        today=date(2041, 8, 10),
        lifecycle_status=None,
    )

    assert assessment.classification is None
    assert assessment.elapsed_months == -4
    assert assessment.missing_periods == ()


# --- D8: is_active is never read, and the audit is on the published strings ---


def test_no_deciding_signal_can_ever_mention_is_active() -> None:
    """Derived from the published vocabulary, not from a reading of the branches.

    Every signal the classifier can emit is in `DECIDING_SIGNALS`, and a test
    below asserts the classifier only ever returns members of it. So this covers
    the whole surface rather than the cases someone remembered to write.
    """
    assert DECIDING_SIGNALS
    assert not [signal for signal in DECIDING_SIGNALS if "is_active" in signal]
    assert not [signal for signal in DECIDING_SIGNALS if "active" in signal.split("_")[:1]]


@pytest.mark.parametrize(
    ("last_covered", "periods", "today", "lifecycle"),
    [
        ("2041-07", GAPLESS_MONTHLY, date(2041, 8, 10), None),
        ("2041-07", GAPLESS_MONTHLY, date(2041, 9, 10), None),
        ("2041-01", GAPLESS_MONTHLY, date(2041, 8, 10), None),
        ("2041-05", IRREGULAR_BIMONTHLY, date(2041, 8, 10), None),
        ("2041-05", IRREGULAR_MONTHLY, date(2041, 8, 10), None),
        ("2041-07", SHORT_CARD_HISTORY, date(2041, 8, 10), None),
        ("2041-07", GAPLESS_MONTHLY, date(2041, 8, 10), "closed"),
        ("2041-07", GAPLESS_MONTHLY, date(2041, 8, 10), "active"),
    ],
)
def test_every_reachable_answer_stays_inside_the_published_vocabularies(
    last_covered: str,
    periods: tuple[str, ...],
    today: date,
    lifecycle: str | None,
) -> None:
    assessment = classify_gap(
        last_covered=last_covered,
        cadence=estimate_cadence(periods),
        today=today,
        lifecycle_status=lifecycle,
    )

    assert assessment.deciding_signal in DECIDING_SIGNALS
    assert assessment.classification is None or assessment.classification in GAP_CLASSIFICATIONS


def test_both_fictional_cadences_are_overdue_then_dormant_once_closed() -> None:
    """A three-month trailing silence is overdue for either cadence.

    Neither cadence is dormant on elapsed time alone. An explicit closure
    outranks that elapsed-time assessment.
    """
    today = date(2041, 8, 10)
    monthly = classify_gap(
        last_covered="2041-05",
        cadence=estimate_cadence(IRREGULAR_MONTHLY),
        today=today,
        lifecycle_status=None,
    )
    bimonthly = classify_gap(
        last_covered="2041-05",
        cadence=estimate_cadence(IRREGULAR_BIMONTHLY),
        today=today,
        lifecycle_status=None,
    )

    assert monthly.classification == "overdue"
    assert monthly.deciding_signal == "elapsed_beyond_one_cadence"
    assert monthly.missing_periods == ("2041-06", "2041-07")
    assert bimonthly.classification == "overdue"
    assert bimonthly.deciding_signal == "elapsed_beyond_one_cadence"
    assert bimonthly.missing_periods == ("2041-07",)

    for periods in (IRREGULAR_MONTHLY, IRREGULAR_BIMONTHLY):
        closed = classify_gap(
            last_covered="2041-05",
            cadence=estimate_cadence(periods),
            today=today,
            lifecycle_status="closed",
        )
        assert closed.classification == "dormant"
        assert closed.deciding_signal == "operator_recorded_closure"


def test_a_current_monthly_account_produces_no_gap() -> None:
    """The other half of "no false positives": the rule must be quiet on the
    accounts that are current, or its output is unreadable."""
    assessment = classify_gap(
        last_covered="2041-07",
        cadence=estimate_cadence(GAPLESS_MONTHLY),
        today=date(2041, 8, 10),
        lifecycle_status=None,
    )

    assert assessment.classification is None
