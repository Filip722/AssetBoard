"""Transaction entry rules (F2.2): day numbers and dates, amounts, descriptions."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from assetboard.domain.money import Money
from assetboard.domain.months import YearMonth
from assetboard.domain.transactions import (
    MAX_AMOUNT_MINOR,
    MAX_DESCRIPTION_LENGTH,
    EntryError,
    amount_minor,
    clean_description,
    description_key,
    parse_entry_date,
)

OCTOBER, NOVEMBER = YearMonth(2026, 10), YearMonth(2026, 11)


@pytest.mark.parametrize(
    ("text", "month", "expected"),
    [
        ("7", OCTOBER, date(2026, 10, 7)),
        ("07", OCTOBER, date(2026, 10, 7)),
        (" 1 ", OCTOBER, date(2026, 10, 1)),
        ("31", OCTOBER, date(2026, 10, 31)),
        ("30", NOVEMBER, date(2026, 11, 30)),
        ("29", YearMonth(2028, 2), date(2028, 2, 29)),  # leap year
        ("29", YearMonth(2000, 2), date(2000, 2, 29)),  # divisible by 400
        ("28", YearMonth(2026, 2), date(2026, 2, 28)),
        ("2026-10-07", NOVEMBER, date(2026, 10, 7)),  # a full date may be in another month
        ("2026-1-5", OCTOBER, date(2026, 1, 5)),
        ("07.10.2026", OCTOBER, date(2026, 10, 7)),
        ("7.1.2027", OCTOBER, date(2027, 1, 7)),
        ("1900-01-01", OCTOBER, date(1900, 1, 1)),
        ("31.12.2199", OCTOBER, date(2199, 12, 31)),
    ],
)
def test_parse_entry_date_accepts_day_numbers_and_full_dates(
    text: str, month: YearMonth, expected: date
) -> None:
    assert parse_entry_date(text, month) == expected


@pytest.mark.parametrize(
    ("text", "month", "code"),
    [
        ("31", NOVEMBER, "day_not_in_month"),
        ("29", YearMonth(2026, 2), "day_not_in_month"),
        ("29", YearMonth(1900, 2), "day_not_in_month"),  # divisible by 100, not 400
        ("30", YearMonth(2028, 2), "day_not_in_month"),
        ("2026-02-30", OCTOBER, "day_not_in_month"),
        ("31.04.2026", OCTOBER, "day_not_in_month"),
        ("0", OCTOBER, "invalid_date"),
        ("32", OCTOBER, "invalid_date"),
        ("-1", OCTOBER, "invalid_date"),
        ("123", OCTOBER, "invalid_date"),
        ("", OCTOBER, "invalid_date"),
        ("seven", OCTOBER, "invalid_date"),
        ("٣", OCTOBER, "invalid_date"),  # Arabic-Indic digit
        ("７", OCTOBER, "invalid_date"),  # full-width digit
        ("2026-13-01", OCTOBER, "invalid_date"),
        ("2026-00-10", OCTOBER, "invalid_date"),
        ("07/10/2026", OCTOBER, "invalid_date"),  # ambiguous order: refused
        ("2026-10-07T00:00", OCTOBER, "invalid_date"),
        ("1899-12-31", OCTOBER, "date_out_of_range"),
        ("01.01.2200", OCTOBER, "date_out_of_range"),
    ],
)
def test_parse_entry_date_rejects_invalid_input_with_a_code(
    text: str, month: YearMonth, code: str
) -> None:
    with pytest.raises(EntryError) as caught:
        parse_entry_date(text, month)
    assert caught.value.code == code


months = st.builds(YearMonth, st.integers(1900, 2199), st.integers(1, 12))


@given(month=months, data=st.data())
def test_every_day_of_a_month_round_trips_and_later_days_fail(
    month: YearMonth, data: st.DataObject
) -> None:
    day = data.draw(st.integers(1, month.days))
    assert parse_entry_date(str(day), month) == date(month.year, month.month, day)
    assert parse_entry_date(f"{day:02d}", month) == date(month.year, month.month, day)
    beyond = data.draw(st.integers(month.days + 1, 31)) if month.days < 31 else None
    if beyond is not None:
        with pytest.raises(EntryError) as caught:
            parse_entry_date(str(beyond), month)
        assert caught.value.code == "day_not_in_month"


@given(day=st.dates(date(1900, 1, 1), date(2199, 12, 31)), month=months)
def test_full_dates_parse_to_themselves_in_any_viewed_month(day: date, month: YearMonth) -> None:
    assert parse_entry_date(day.isoformat(), month) == day
    assert parse_entry_date(day.strftime("%d.%m.%Y"), month) == day


# Amounts


@pytest.mark.parametrize(
    ("amount", "currency", "expected"),
    [
        (Decimal("12.50"), "EUR", 1250),
        (Decimal("12.5"), "EUR", 1250),
        (Decimal("1234.56"), "PLN", 123456),
        (Decimal("0.01"), "EUR", 1),
        (Decimal("12.500"), "EUR", 1250),  # trailing zeros are not extra precision
        (Decimal("5"), "JPY", 5),
        (Decimal("1.234"), "KWD", 1234),
        (Decimal("1E+2"), "EUR", 10000),
        (Decimal("9999999999999.99"), "EUR", MAX_AMOUNT_MINOR),
    ],
)
def test_amount_minor_converts_exactly(amount: Decimal, currency: str, expected: int) -> None:
    assert amount_minor(amount, currency) == expected


@pytest.mark.parametrize(
    ("amount", "currency", "code"),
    [
        (Decimal("0"), "EUR", "amount_not_positive"),
        (Decimal("0.00"), "EUR", "amount_not_positive"),
        (Decimal("-5"), "EUR", "amount_not_positive"),
        (Decimal("12.345"), "EUR", "too_many_decimals"),
        (Decimal("0.001"), "EUR", "too_many_decimals"),
        (Decimal("1.5"), "JPY", "too_many_decimals"),
        (Decimal("12.50000000000000000000000000000001"), "EUR", "too_many_decimals"),
        (Decimal("10000000000000"), "EUR", "amount_too_large"),
        (Decimal("1E+60"), "EUR", "amount_too_large"),
        (Decimal("NaN"), "EUR", "invalid_amount"),
        (Decimal("Infinity"), "EUR", "invalid_amount"),
    ],
)
def test_amount_minor_refuses_instead_of_rounding(
    amount: Decimal, currency: str, code: str
) -> None:
    with pytest.raises(EntryError) as caught:
        amount_minor(amount, currency)
    assert caught.value.code == code


def test_amount_minor_refuses_floats() -> None:
    with pytest.raises(EntryError):
        amount_minor(12.5, "EUR")  # type: ignore[arg-type]


@given(
    minor=st.integers(1, MAX_AMOUNT_MINOR),
    currency=st.sampled_from(["EUR", "PLN", "JPY", "KWD", "CLF"]),
)
def test_amount_minor_round_trips_money(minor: int, currency: str) -> None:
    assert amount_minor(Money(minor, currency).to_decimal(), currency) == minor


# Descriptions


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("  Lidl   weekly  shop ", "Lidl weekly shop"),
        ("", ""),
        ("   ", ""),
        ("Café 👨‍👩‍👧", "Café 👨‍👩‍👧"),
        ("x" * MAX_DESCRIPTION_LENGTH, "x" * MAX_DESCRIPTION_LENGTH),
    ],
)
def test_clean_description_trims_and_collapses(value: str, expected: str) -> None:
    assert clean_description(value) == expected


@pytest.mark.parametrize(
    ("value", "code"),
    [
        ("a\x00b", "invalid_description"),
        ("a‮b", "invalid_description"),
        ("x" * (MAX_DESCRIPTION_LENGTH + 1), "description_too_long"),
        ("ﷺ" * 20, "description_too_long"),  # the key would not fit its column
    ],
)
def test_clean_description_refuses_bad_text(value: str, code: str) -> None:
    with pytest.raises(EntryError) as caught:
        clean_description(value)
    assert caught.value.code == code


def test_description_key_folds_unicode_case() -> None:
    assert description_key("ŻABKA  Express") == description_key("żabka express") == "żabka express"
    assert description_key("Straße") == "strasse"
