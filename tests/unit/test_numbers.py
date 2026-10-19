from __future__ import annotations

from decimal import Decimal

import pytest
from babel.numbers import format_decimal
from hypothesis import given
from hypothesis import strategies as st

from assetboard.domain.money import Money
from assetboard.i18n.locale import resolve_locale
from assetboard.i18n.numbers import format_amount_input, format_money, parse_amount


@pytest.mark.parametrize("locale", ["en", "pl", "de"])
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("12,5", Decimal("12.5")),
        ("12,50", Decimal("12.50")),
        ("1 234,56", Decimal("1234.56")),
        ("12.50", Decimal("12.50")),
    ],
)
def test_parse_amount_accepts_spec_examples_for_each_locale(
    text: str, expected: Decimal, locale: str
) -> None:
    assert parse_amount(text, locale) == expected


@pytest.mark.parametrize(
    ("text", "locale", "expected"),
    [
        ("1.234,56", "de-DE", Decimal("1234.56")),
        ("1,234.56", "en-US", Decimal("1234.56")),
        ("1,234", "en", Decimal("1234")),
        ("1.234", "de-DE", Decimal("1234")),
        ("12,500", "pl", Decimal("12.500")),
        ("-3", "en", Decimal("-3")),
        ("+3", "pl", Decimal("3")),
        ("−3", "pl", Decimal("-3")),
    ],
)
def test_parse_amount_uses_locale_separator_convention(
    text: str, locale: str, expected: Decimal
) -> None:
    assert parse_amount(text, locale) == expected


@pytest.mark.parametrize("text", ["", " ", "abc", "12x", "1,23,4", "NaN", "Infinity", "1" * 65])
def test_parse_amount_rejects_invalid_or_non_finite_input(text: str) -> None:
    with pytest.raises(ValueError, match="valid amount"):
        parse_amount(text, "en")


@pytest.mark.parametrize("locale", ["en", "pl", "de"])
@given(
    amount=st.decimals(
        min_value=Decimal("-9999999.99"),
        max_value=Decimal("9999999.99"),
        places=2,
    )
)
def test_parse_amount_round_trips_babel_formatting(locale: str, amount: Decimal) -> None:
    formatted = format_decimal(amount, locale=locale, decimal_quantization=False)

    assert parse_amount(formatted, locale) == amount


@pytest.mark.parametrize(
    ("currency", "amount", "locale", "expected"),
    [
        ("EUR", 123456, "en", "€1,234.56"),
        ("EUR", 123456, "pl", "1 234,56 €"),
        ("JPY", 1234, "en", "¥1,234"),
    ],
)
def test_format_money_uses_locale_and_currency_digits(
    currency: str,
    amount: int,
    locale: str,
    expected: str,
) -> None:
    assert format_money(Money(amount, currency), locale) == expected


def test_resolve_locale_obeys_user_workspace_header_fallback_order() -> None:
    assert (
        resolve_locale(
            user_locale="pl-PL",
            workspace_locale="en",
            accept_language="en-US",
        )
        == "pl"
    )
    assert (
        resolve_locale(
            workspace_locale="pl",
            accept_language="en-US",
        )
        == "pl"
    )
    assert resolve_locale(accept_language="fr-FR,pl-PL;q=0.8,en;q=0.7") == "pl"
    assert resolve_locale(accept_language="en;q=0,pl;q=0.4") == "pl"
    assert resolve_locale(accept_language="fr-FR") == "en"


def test_resolve_locale_skips_unsupported_preferences() -> None:
    assert resolve_locale(user_locale="fr", workspace_locale="pl") == "pl"
    assert resolve_locale(accept_language="pl;q=NaN", default_locale="pl") == "pl"


def test_locale_aware_amount_helpers_reject_unknown_locale() -> None:
    with pytest.raises(ValueError, match="Unsupported locale"):
        parse_amount("10", "not-a-locale")


@pytest.mark.parametrize(
    ("text", "locale", "expected"),
    [
        ("1 234,56", "pl", Decimal("1234.56")),
        ("1 234,56", "pl", Decimal("1234.56")),
        ("12,5", "pl", Decimal("12.5")),
        ("12,5", "en", Decimal("12.5")),
        ("0,001", "pl", Decimal("0.001")),  # pl: the comma is the decimal separator
        ("0,5", "en", Decimal("0.5")),
        ("0.001", "en", Decimal("0.001")),
        ("1,234.56", "en", Decimal("1234.56")),
        ("1,234.56", "pl", Decimal("1234.56")),
        ("1.234,56", "pl", Decimal("1234.56")),
        ("1,234,567", "en", Decimal("1234567")),
        ("1 234 567", "en", Decimal("1234567")),
        ("123,456", "en", Decimal("123456")),
    ],
)
def test_parse_amount_groups_and_decimals_table(text: str, locale: str, expected: Decimal) -> None:
    assert parse_amount(text, locale) == expected


@pytest.mark.parametrize("locale", ["en", "pl"])
@pytest.mark.parametrize(
    "text",
    [
        "1,23,456",  # groups after the first must have 3 digits
        "1.23.456",
        "1 23 456",
        "1 2345",
        "1234 567",  # the first group has at most 3 digits
        "0 001",  # a leading zero group is not a thousands group
        "01 234",
        "0,001.5",
        "0.001,5",
        "1,2345.6",
    ],
)
def test_parse_amount_rejects_malformed_grouping(text: str, locale: str) -> None:
    with pytest.raises(ValueError, match="valid amount|prawidłową kwotę"):
        parse_amount(text, locale)


@pytest.mark.parametrize("text", ["0,001", "01,234", "0,000"])
def test_parse_amount_rejects_a_leading_zero_group_in_en(text: str) -> None:
    """In ``en`` the comma groups thousands: "0,001" is neither 1 nor 0.001."""
    with pytest.raises(ValueError, match="valid amount"):
        parse_amount(text, "en")


@pytest.mark.parametrize(
    ("currency", "amount", "locale", "expected"),
    [
        ("EUR", 123456, "en", "1234.56"),
        ("EUR", 123456, "pl", "1234,56"),
        ("EUR", 5, "pl", "0,05"),
        ("EUR", 1250, "de", "12,50"),
        ("JPY", 1234, "en", "1234"),
        ("KWD", 1234567, "pl", "1234,567"),
    ],
)
def test_format_amount_input_writes_the_locale_decimal_without_grouping(
    currency: str, amount: int, locale: str, expected: str
) -> None:
    assert format_amount_input(Money(amount, currency), locale) == expected


@pytest.mark.parametrize("locale", ["en", "pl", "de"])
@pytest.mark.parametrize("currency", ["EUR", "JPY", "KWD"])
@given(amount=st.integers(min_value=1, max_value=10**15 - 1))
def test_format_amount_input_round_trips_through_parse_amount(
    locale: str, currency: str, amount: int
) -> None:
    money = Money(amount, currency)
    assert parse_amount(format_amount_input(money, locale), locale) == money.to_decimal()


@pytest.mark.parametrize(
    ("tenths", "locale", "text"),
    [
        (-433, "en", "-43.3%"),
        (-433, "pl", "-43,3%"),
        (1000, "en", "100.0%"),
        (0, "en", "0.0%"),
        (1, "en", "0.1%"),
        (123, "en", "12.3%"),
        (-1, "en", "-0.1%"),
        (12345, "en", "1,234.5%"),
    ],
)
def test_rates_in_tenths_of_a_percent_show_one_decimal(tenths: int, locale: str, text: str) -> None:
    from assetboard.i18n.numbers import format_rate_tenths

    assert format_rate_tenths(tenths, locale) == text
