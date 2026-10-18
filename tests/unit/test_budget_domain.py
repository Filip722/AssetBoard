"""Planned-value amounts (F2.3): zero removes, negatives and extra decimals are refused."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from assetboard.domain.budget import MAX_AMOUNT_MINOR, EntryError, planned_minor
from assetboard.domain.money import Money
from assetboard.i18n.numbers import format_amount_input, parse_amount


@pytest.mark.parametrize(
    ("amount", "currency", "minor"),
    [
        (Decimal("550"), "EUR", 55000),
        (Decimal("5350.00"), "EUR", 535000),
        (Decimal("0.01"), "EUR", 1),
        (Decimal("1500"), "JPY", 1500),
        (Decimal("0"), "EUR", 0),  # remove the line
        (Decimal("0.000"), "EUR", 0),
        (Decimal("-0"), "EUR", 0),
        (Decimal("9999999999999.99"), "EUR", MAX_AMOUNT_MINOR),
    ],
)
def test_planned_minor_converts_exactly(amount: Decimal, currency: str, minor: int) -> None:
    assert planned_minor(amount, currency) == minor


@pytest.mark.parametrize(
    ("amount", "currency", "code"),
    [
        (Decimal("-0.01"), "EUR", "amount_negative"),
        (Decimal("-5"), "EUR", "amount_negative"),
        (Decimal("12.345"), "EUR", "too_many_decimals"),
        (Decimal("1.5"), "JPY", "too_many_decimals"),
        (Decimal("10000000000000"), "EUR", "amount_too_large"),
        (Decimal("NaN"), "EUR", "invalid_amount"),
        (Decimal("Infinity"), "EUR", "invalid_amount"),
    ],
)
def test_planned_minor_refuses_instead_of_rounding(
    amount: Decimal, currency: str, code: str
) -> None:
    with pytest.raises(EntryError) as caught:
        planned_minor(amount, currency)
    assert caught.value.code == code


@given(
    minor=st.integers(min_value=0, max_value=MAX_AMOUNT_MINOR),
    currency=st.sampled_from(["EUR", "PLN", "JPY", "KWD"]),
    locale=st.sampled_from(["en", "pl"]),
)
def test_a_formatted_planned_value_reads_back_unchanged(
    minor: int, currency: str, locale: str
) -> None:
    text = format_amount_input(Money(minor, currency), locale)
    assert planned_minor(parse_amount(text, locale), currency) == minor
