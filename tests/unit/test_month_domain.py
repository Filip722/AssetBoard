"""``YearMonth`` (F2.5): parsing, bounds, neighbours and day ranges."""

from __future__ import annotations

from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st

from assetboard.domain.months import MAX_MONTH, MIN_MONTH, InvalidMonth, YearMonth

months = st.builds(YearMonth, st.integers(1900, 2199), st.integers(1, 12))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-10", YearMonth(2026, 10)),
        ("2026-01", YearMonth(2026, 1)),
        ("1900-01", MIN_MONTH),
        ("2199-12", MAX_MONTH),
    ],
)
def test_parse_accepts_yyyy_mm(text: str, expected: YearMonth) -> None:
    assert YearMonth.parse(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "2026-13",
        "2026-00",
        "2026-1",
        "26-10",
        "2026-10-01",
        " 2026-10",
        "2026-10 ",
        "2026/10",
        "10.2026",
        "1899-12",
        "2200-01",
        "0000-01",
        "２０２６-10",  # full-width digits: \d would match them
        "٢٠٢٦-10",  # Arabic-Indic digits
    ],
)
def test_parse_rejects_everything_else(text: str) -> None:
    with pytest.raises(InvalidMonth):
        YearMonth.parse(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-10", YearMonth(2026, 10)),
        (" 2026-10 ", YearMonth(2026, 10)),
        ("2026/3", YearMonth(2026, 3)),
        ("2026.03", YearMonth(2026, 3)),
        ("10.2026", YearMonth(2026, 10)),
        ("3.2026", YearMonth(2026, 3)),
        ("03/2026", YearMonth(2026, 3)),
        ("3-2026", YearMonth(2026, 3)),
    ],
)
def test_parse_loose_accepts_what_people_type(text: str, expected: YearMonth) -> None:
    assert YearMonth.parse_loose(text) == expected


@pytest.mark.parametrize(
    "text", ["", "october", "2026", "13.2026", "2026-13", "1.1.2026", "2200-01", "١٠.٢٠٢٦"]
)
def test_parse_loose_rejects_garbage_and_out_of_range(text: str) -> None:
    with pytest.raises(InvalidMonth):
        YearMonth.parse_loose(text)


@pytest.mark.parametrize(("year", "month"), [(2026, 0), (2026, 13), (1899, 12), (2200, 1)])
def test_constructor_validates(year: int, month: int) -> None:
    with pytest.raises(InvalidMonth):
        YearMonth(year, month)


@pytest.mark.parametrize(
    ("month", "prev", "next_"),
    [
        (YearMonth(2026, 10), YearMonth(2026, 9), YearMonth(2026, 11)),
        (YearMonth(2026, 1), YearMonth(2025, 12), YearMonth(2026, 2)),
        (YearMonth(2026, 12), YearMonth(2026, 11), YearMonth(2027, 1)),
        (MIN_MONTH, None, YearMonth(1900, 2)),
        (MAX_MONTH, YearMonth(2199, 11), None),
    ],
)
def test_neighbours_cross_years_and_stop_at_the_bounds(
    month: YearMonth, prev: YearMonth | None, next_: YearMonth | None
) -> None:
    assert (month.prev(), month.next()) == (prev, next_)


@pytest.mark.parametrize(
    ("month", "last", "days"),
    [
        (YearMonth(2026, 1), date(2026, 1, 31), 31),
        (YearMonth(2026, 2), date(2026, 2, 28), 28),
        (YearMonth(2028, 2), date(2028, 2, 29), 29),
        (YearMonth(2100, 2), date(2100, 2, 28), 28),  # not a leap year
        (YearMonth(2000, 2), date(2000, 2, 29), 29),
        (YearMonth(2026, 11), date(2026, 11, 30), 30),
    ],
)
def test_last_day_and_length(month: YearMonth, last: date, days: int) -> None:
    assert (month.first_day.day, month.last_day, month.days) == (1, last, days)


def test_text_ordering_and_contains() -> None:
    october = YearMonth(2026, 10)
    assert str(YearMonth(1987, 3)) == "1987-03"
    assert YearMonth(2025, 12) < october < YearMonth(2026, 11)
    assert october.contains(date(2026, 10, 31)) and not october.contains(date(2026, 11, 1))
    assert YearMonth.from_date(date(2026, 10, 14)) == october


@given(months)
def test_text_round_trips(month: YearMonth) -> None:
    assert YearMonth.parse(str(month)) == month
    assert YearMonth.parse_loose(str(month)) == month


@given(months)
def test_prev_and_next_are_inverse(month: YearMonth) -> None:
    after, before = month.next(), month.prev()
    assert after is None or after.prev() == month
    assert before is None or before.next() == month


@given(months, months)
def test_ordering_matches_first_days(a: YearMonth, b: YearMonth) -> None:
    assert (a < b) == (a.first_day < b.first_day)
    assert (a == b) == (a.first_day == b.first_day)


@given(st.dates(date(1900, 1, 1), date(2199, 12, 31)))
def test_every_day_is_in_its_month(day: date) -> None:
    month = YearMonth.from_date(day)
    assert month.contains(day) and month.contains(month.first_day)
    assert month.contains(month.last_day)
    assert month.first_day <= day <= month.last_day
