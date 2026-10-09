from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, localcontext

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from assetboard.domain.money import Money, minor_unit_for


@given(
    left=st.integers(min_value=-(10**18), max_value=10**18),
    right=st.integers(min_value=-(10**18), max_value=10**18),
)
@settings(max_examples=100)
def test_money_addition_and_subtraction_use_integer_minor_units(left: int, right: int) -> None:
    left_money = Money(left, "EUR")
    right_money = Money(right, "EUR")

    assert (left_money + right_money).amount_minor == left + right
    assert (left_money - right_money).amount_minor == left - right


@given(
    amount=st.decimals(
        min_value=Decimal("-99999999999999999999.99999"),
        max_value=Decimal("99999999999999999999.99999"),
        places=5,
        allow_nan=False,
        allow_infinity=False,
    )
)
@settings(max_examples=100)
def test_money_decimal_conversion_rounds_half_up(amount: Decimal) -> None:
    expected = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    money = Money.from_decimal(amount, "EUR")

    assert money.to_decimal() == expected
    assert money.amount_minor == int(expected * 100)


def test_money_half_up_rounds_ties_away_from_zero() -> None:
    assert Money.from_decimal(Decimal("1.005"), "EUR").amount_minor == 101
    assert Money.from_decimal(Decimal("-1.005"), "EUR").amount_minor == -101


def test_money_supports_explicit_currency_scale_and_validates_currency() -> None:
    assert Money.from_decimal(Decimal("1.234"), "KWD", minor_unit=3).amount_minor == 1234
    assert Money(100, "eur").currency == "EUR"
    assert minor_unit_for("JPY") == 0
    assert Money.from_decimal(Decimal("1.5"), "JPY").amount_minor == 2

    with pytest.raises(ValueError, match="currency"):
        Money(100, "EURO")

    with pytest.raises(ValueError, match="unsupported currency"):
        minor_unit_for("ZZZ")
    with pytest.raises(ValueError, match="unsupported currency"):
        Money(100, "ZZZ")

    with pytest.raises(TypeError, match="currency must be a string"):
        Money(100, None)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("amount", "currency"),
    [
        (Decimal("123.45"), "EUR"),
        (Decimal("-123"), "JPY"),
        (Decimal("1.234"), "KWD"),
    ],
)
def test_from_decimal_to_decimal_round_trip_for_currency_minor_units(
    amount: Decimal,
    currency: str,
) -> None:
    money = Money.from_decimal(amount, currency)

    assert money.to_decimal() == amount


def test_money_rounding_uses_local_decimal_precision() -> None:
    with localcontext() as context:
        context.prec = 2
        money = Money.from_decimal(Decimal("12345.678"), "EUR")

    assert money.amount_minor == 1_234_568


def test_money_rejects_floats_and_mixed_currency_arithmetic() -> None:
    with pytest.raises(TypeError, match="Decimal"):
        Money.from_decimal(float("0.1"), "EUR")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="different currencies"):
        _ = Money(100, "EUR") + Money(100, "USD")
