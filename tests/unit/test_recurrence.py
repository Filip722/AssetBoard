from dataclasses import replace
from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st

from assetboard.domain.recurrence import (
    Frequency,
    MonthDay,
    RecurrenceRule,
    WeekendPolicy,
    occurrences,
)


def test_monthly_clamps_and_last_day() -> None:
    rule = RecurrenceRule(date(2024, 1, 1), month_day=31)
    expected = [date(2024, 1, 31), date(2024, 2, 29), date(2024, 3, 31), date(2024, 4, 30)]
    assert occurrences(rule, date(2024, 1, 1), date(2024, 4, 30), set()) == expected
    assert (
        occurrences(replace(rule, month_day=-1), date(2024, 1, 1), date(2024, 4, 30), set())
        == expected
    )


def test_yearly_february_29_and_interval() -> None:
    rule = RecurrenceRule(date(2023, 1, 1), Frequency.YEARLY, month=2, month_day=29)
    assert occurrences(rule, date(2023, 1, 1), date(2025, 12, 31), set()) == [
        date(2023, 2, 28),
        date(2024, 2, 29),
        date(2025, 2, 28),
    ]
    assert occurrences(replace(rule, interval=2), date(2023, 1, 1), date(2025, 12, 31), set()) == [
        date(2023, 2, 28),
        date(2025, 2, 28),
    ]


def test_weekly_anchor_start_end_count_and_inclusive_query() -> None:
    rule = RecurrenceRule(
        date(2026, 3, 4), Frequency.WEEKLY, interval=2, weekdays=(0, 4), max_count=4
    )
    expected = [date(2026, 3, 6), date(2026, 3, 16), date(2026, 3, 20), date(2026, 3, 30)]
    assert occurrences(rule, date(2026, 3, 1), date(2026, 5, 1), set()) == expected
    assert occurrences(rule, date(2026, 3, 20), date(2026, 3, 30), set()) == expected[2:]
    assert (
        occurrences(
            replace(rule, end_date=date(2026, 3, 20)), date(2026, 3, 1), date(2026, 5, 1), set()
        )
        == expected[:3]
    )


@pytest.mark.parametrize(
    ("choice", "expected"),
    [
        (MonthDay.FIRST_WORKING, date(2026, 5, 4)),
        (MonthDay.LAST_WORKING, date(2026, 5, 28)),
    ],
)
def test_working_days_use_holidays(choice, expected) -> None:
    rule = RecurrenceRule(date(2026, 5, 1), month_day=choice)
    assert occurrences(
        rule, date(2026, 5, 1), date(2026, 5, 31), {date(2026, 5, 1), date(2026, 5, 29)}
    ) == [expected]


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        (WeekendPolicy.NONE, date(2026, 1, 1)),
        (WeekendPolicy.PREVIOUS, date(2025, 12, 31)),
        (WeekendPolicy.NEXT, date(2026, 1, 5)),
    ],
)
def test_shift_across_year_and_holiday_chain(policy, expected) -> None:
    rule = RecurrenceRule(date(2026, 1, 1), month_day=1, max_count=1, weekend_policy=policy)
    assert occurrences(
        rule, date(2025, 12, 31), date(2026, 1, 5), {date(2026, 1, 1), date(2026, 1, 2)}
    ) == [expected]


def test_shifted_duplicates_merge_but_count_nominal_dates() -> None:
    rule = RecurrenceRule(
        date(2026, 3, 6),
        Frequency.WEEKLY,
        weekdays=(4, 5, 6),
        max_count=3,
        weekend_policy=WeekendPolicy.NEXT,
    )
    assert occurrences(rule, date(2026, 3, 1), date(2026, 4, 1), set()) == [
        date(2026, 3, 6),
        date(2026, 3, 9),
    ]


@pytest.mark.parametrize(
    "changes",
    [
        {"interval": 0},
        {"max_count": 0},
        {"month_day": 0},
        {"end_date": date(2025, 1, 1)},
        {"freq": Frequency.WEEKLY},
        {"freq": Frequency.WEEKLY, "weekdays": (7,)},
        {"freq": Frequency.YEARLY, "month": 13},
        {"freq": Frequency.YEARLY, "month_day": MonthDay.FIRST_WORKING},
    ],
)
def test_invalid_rules_are_refused(changes) -> None:
    with pytest.raises(ValueError):
        RecurrenceRule(date(2026, 1, 1), **changes)


@given(day=st.integers(1, 31), interval=st.integers(1, 12), count=st.integers(1, 30))
def test_monthly_results_are_deterministic_ordered_unique_bounded(day, interval, count) -> None:
    rule = RecurrenceRule(date(2024, 1, 1), month_day=day, interval=interval, max_count=count)
    first, last = date(2024, 1, 1), date(2030, 12, 31)
    actual = occurrences(rule, first, last, set())
    assert actual == occurrences(rule, first, last, set())
    assert actual == sorted(set(actual)) and len(actual) <= count
    assert all(first <= day <= last for day in actual)
    assert occurrences(rule, first, last, set(), limit=6) == actual[:6]
