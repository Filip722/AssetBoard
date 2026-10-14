"""Locale-aware month and date texts from Babel's CLDR data (F2.5).

Month names come from CLDR, not from the gettext catalog: every locale Babel knows gets its own
names and grammar (Polish uses the nominative "październik 2026" for a month on its own).
"""

from __future__ import annotations

from datetime import date

from babel import Locale
from babel.core import UnknownLocaleError
from babel.dates import format_date, format_skeleton

from assetboard.domain.months import YearMonth

_FALLBACK_LOCALE = "en"


def _locale(locale: str) -> str:
    try:
        return str(Locale.parse(locale.replace("-", "_")))
    except (UnknownLocaleError, ValueError):
        return _FALLBACK_LOCALE


def format_month(month: YearMonth, locale: str) -> str:
    """The month and year as CLDR writes them in running text: "October 2026",
    "październik 2026"."""
    return format_skeleton("yMMMM", month.first_day, locale=_locale(locale))


def format_month_heading(month: YearMonth, locale: str) -> str:
    """``format_month`` with a capital first letter, for a heading or page title only:
    "Październik 2026"."""
    text = format_month(month, locale)
    return text[:1].upper() + text[1:]


def format_day(day: date, locale: str) -> str:
    """A date in the locale's medium format: "Oct 14, 2026", "14 paź 2026"."""
    return format_date(day, format="medium", locale=_locale(locale))
