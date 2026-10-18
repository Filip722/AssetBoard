"""Transaction entry rules (F2.2): dates, amounts and descriptions. Pure: no I/O, no clock.

A transaction is entered on a month page, so its date may be a bare day number interpreted
within the viewed month (``7``), or a full date (``2026-10-07``, ``07.10.2026``) that may lie in
another month. Amounts are entered in the locale's format, parsed by ``i18n.numbers`` into a
``Decimal`` and stored as positive integer minor units (never rounded: an amount with more
fraction digits than the currency has is refused).
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal
from enum import StrEnum

from assetboard.domain.money import minor_unit_for
from assetboard.domain.months import InvalidMonth, YearMonth
from assetboard.domain.text import collapse_whitespace, has_rejected_characters, text_key

MAX_DESCRIPTION_LENGTH = 200
# The key can be longer than the description (NFKC/casefold expand a few characters); a
# description whose key does not fit is refused, as for category names.
MAX_DESCRIPTION_KEY_LENGTH = 255
# 15 digits of minor units: far above any household amount, and sums of many such amounts
# still fit a signed 64-bit integer.
MAX_AMOUNT_MINOR = 10**15 - 1
UNDO_WINDOW = timedelta(seconds=10)

# ASCII digits only: Python's \d also matches other scripts' digits.
_DAY = re.compile(r"([0-9]{1,2})")
_ISO = re.compile(r"([0-9]{4})-([0-9]{1,2})-([0-9]{1,2})")
_DOTTED = re.compile(r"([0-9]{1,2})\.([0-9]{1,2})\.([0-9]{4})")


class TransactionDirection(StrEnum):
    """Which table a transaction belongs to; must equal its category's kind."""

    EXPENSE = "expense"
    INCOME = "income"


class Source(StrEnum):
    """How a transaction was created. F2.2 writes ``manual``; M3 and M4 add the others."""

    MANUAL = "manual"
    RECURRING = "recurring"
    RECURRING_AUTO = "recurring_auto"
    IMPORT = "import"


class EntryError(ValueError):
    """Invalid user input; ``code`` names the problem for the form."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _date(year: int, month: int, day: int) -> date:
    if not 1 <= month <= 12 or not 1 <= day <= 31:
        raise EntryError("invalid_date")
    try:
        target = YearMonth(year, month)
    except InvalidMonth:
        raise EntryError("date_out_of_range") from None
    if day > target.days:
        raise EntryError("day_not_in_month")
    return date(year, month, day)


def parse_entry_date(text: str, month: YearMonth) -> date:
    """A day number (1-31) within ``month``, or a full ``YYYY-MM-DD`` / ``DD.MM.YYYY`` date.

    Error codes: ``invalid_date`` (not a date), ``day_not_in_month`` (31 in November, 29
    February in a common year), ``date_out_of_range`` (outside 1900-01..2199-12).
    """
    value = text.strip()
    if match := _DAY.fullmatch(value):
        return _date(month.year, month.month, int(match.group(1)))
    if match := _ISO.fullmatch(value):
        return _date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    if match := _DOTTED.fullmatch(value):
        return _date(int(match.group(3)), int(match.group(2)), int(match.group(1)))
    raise EntryError("invalid_date")


def amount_minor(amount: Decimal, currency: str) -> int:
    """Positive minor units of ``amount`` in ``currency``; refuses instead of rounding.

    Error codes: ``amount_not_positive``, ``amount_too_large``, ``too_many_decimals``.
    """
    if type(amount) is not Decimal or not amount.is_finite():
        raise EntryError("invalid_amount")
    if amount <= 0:
        raise EntryError("amount_not_positive")
    minor_unit = minor_unit_for(currency)
    if amount > Decimal(MAX_AMOUNT_MINOR).scaleb(-minor_unit):
        raise EntryError("amount_too_large")
    # Exact integer arithmetic on the digits (Decimal operations round to the context precision).
    sign, digits, exponent = amount.as_tuple()
    assert isinstance(exponent, int) and sign == 0  # finite and positive, checked above
    trailing = list(digits)
    while exponent < 0 and len(trailing) > 1 and trailing[-1] == 0:
        trailing.pop()
        exponent += 1
    if exponent < -minor_unit:
        raise EntryError("too_many_decimals")
    return int("".join(map(str, trailing))) * 10 ** (exponent + minor_unit)


def clean_description(value: str) -> str:
    """Trimmed, whitespace collapsed; may be empty. Refuses control/invisible characters.

    Error codes: ``invalid_description``, ``description_too_long``.
    """
    if has_rejected_characters(value.strip()):
        raise EntryError("invalid_description")
    cleaned = collapse_whitespace(value)
    if len(cleaned) > MAX_DESCRIPTION_LENGTH or len(text_key(cleaned)) > MAX_DESCRIPTION_KEY_LENGTH:
        raise EntryError("description_too_long")
    return cleaned


def description_key(description: str) -> str:
    """The case-insensitive key used for autocomplete (``Żabka`` matches ``ż``)."""
    return text_key(description)
