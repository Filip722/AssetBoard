"""Option lists for the workspace fields (first-run setup, "Create workspace", settings)."""

from __future__ import annotations

from assetboard.services.workspace_options import (
    country_names,
    currency_codes,
    locale_codes,
    timezone_names,
)

LOCALE_NAMES = {"en": "English", "pl": "Polski"}


def workspace_form_options(locale: str) -> dict[str, list[tuple[str, str]]]:
    """Currencies, countries (named in ``locale``), time zones and languages."""
    return {
        "currency_options": [(code, code) for code in currency_codes()],
        "country_options": list(country_names(locale).items()),
        "timezone_options": [(zone, zone) for zone in timezone_names()],
        "locales": [(code, LOCALE_NAMES.get(code, code)) for code in locale_codes()],
    }
