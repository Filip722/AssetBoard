from __future__ import annotations

from decimal import Decimal

import pytest
from babel.numbers import format_decimal
from hypothesis import given
from hypothesis import strategies as st

from assetboard.domain.money import Money
from assetboard.i18n.locale import resolve_locale
from assetboard.i18n.numbers import format_money, parse_amount


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
