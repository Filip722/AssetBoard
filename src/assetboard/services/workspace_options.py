"""Choices and validation for workspace settings, built only from existing dependencies."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import cast
from zoneinfo import available_timezones

from babel import Locale

from assetboard.domain.money import CURRENCY_MINOR_UNITS, minor_unit_for
from assetboard.i18n.locale import SUPPORTED_LOCALES, normalize_locale

# CLDR territory codes that are not countries (unknown region, EU/eurozone, UN, pseudo-locales).
_NON_COUNTRY_TERRITORIES = frozenset({"EU", "EZ", "QO", "UN", "XA", "XB", "ZZ"})


@dataclass(frozen=True, slots=True)
class WorkspaceOptions:
    base_currency: str
    country_code: str | None
    timezone: str
    locale: str


class WorkspaceOptionError(ValueError):
    """A workspace option is not supported; ``field`` names it."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


def currency_codes() -> tuple[str, ...]:
    return tuple(sorted(CURRENCY_MINOR_UNITS))


@lru_cache(maxsize=1)
def timezone_names() -> tuple[str, ...]:
    return tuple(sorted(available_timezones()))


def country_names(display_locale: str) -> dict[str, str]:
    territories = cast(dict[str, str], Locale.parse(normalize_locale(display_locale)).territories)
    return {
        code: name
        for code, name in sorted(territories.items(), key=lambda item: item[1])
        if len(code) == 2 and code.isalpha() and code not in _NON_COUNTRY_TERRITORIES
    }


def locale_codes() -> tuple[str, ...]:
    return tuple(sorted(SUPPORTED_LOCALES))


def validate_workspace_options(
    *,
    base_currency: str,
    country_code: str | None,
    timezone: str,
    locale: str,
) -> WorkspaceOptions:
    currency = base_currency.strip().upper()
    try:
        minor_unit_for(currency)
    except ValueError as exc:
        raise WorkspaceOptionError("base_currency", "Unsupported currency") from exc
    country = (country_code or "").strip().upper() or None
    if country is not None and country not in country_names("en"):
        raise WorkspaceOptionError("country_code", "Unsupported country code")
    zone = timezone.strip()
    if zone not in timezone_names():
        raise WorkspaceOptionError("timezone", "Unsupported time zone")
    try:
        language = normalize_locale(locale.strip())
    except ValueError as exc:
        raise WorkspaceOptionError("locale", "Unsupported locale") from exc
    return WorkspaceOptions(
        base_currency=currency,
        country_code=country,
        timezone=zone,
        locale=language,
    )
