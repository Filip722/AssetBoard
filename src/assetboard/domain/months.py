"""Calendar months (F2.5): the ``YearMonth`` value type. Pure: no I/O, no clock.

A month is addressed in URLs as ``YYYY-MM`` (``/m/2026-10``) and stored as its first day.
Months outside ``MIN_MONTH``..``MAX_MONTH`` are invalid everywhere, so a URL such as
``/m/0001-01`` is a 404 rather than a page with nonsensical neighbours.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date
from functools import total_ordering

# ASCII digits only: Python's \d also matches other scripts' digits.
_STRICT = re.compile(r"([0-9]{4})-([0-9]{2})")
# What a person may type into the picker's text fallback (Firefox and Safari on desktop show
# <input type="month"> as a text box): 2026-10, 2026/10, 10.2026, 10/2026, 1.2026.
_YEAR_FIRST = re.compile(r"([0-9]{4})[-/.]([0-9]{1,2})")
_MONTH_FIRST = re.compile(r"([0-9]{1,2})[-/.]([0-9]{4})")


class InvalidMonth(ValueError):
    """Not a ``YYYY-MM`` month, or outside the supported range."""


@total_ordering
@dataclass(frozen=True, slots=True)
class YearMonth:
    year: int
    month: int

    def __post_init__(self) -> None:
        if not 1 <= self.month <= 12:
            raise InvalidMonth(f"month {self.month} is not 1..12")
        if not (MIN_YEAR, 1) <= (self.year, self.month) <= (MAX_YEAR, 12):
            raise InvalidMonth(f"{self.year:04d}-{self.month:02d} is out of range")

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, YearMonth):
            return NotImplemented
        return (self.year, self.month) < (other.year, other.month)

    def __str__(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"

    @classmethod
    def parse(cls, value: str) -> YearMonth:
        """Strict ``YYYY-MM`` (the URL form)."""
        match = _STRICT.fullmatch(value)
        if match is None:
            raise InvalidMonth(f"not YYYY-MM: {value!r}")
        return cls(int(match.group(1)), int(match.group(2)))

    @classmethod
    def parse_loose(cls, value: str) -> YearMonth:
        """``YYYY-MM`` and the forms people type: ``2026/10``, ``10.2026``, ``10/2026``."""
        text = value.strip()
        if match := _YEAR_FIRST.fullmatch(text):
            return cls(int(match.group(1)), int(match.group(2)))
        if match := _MONTH_FIRST.fullmatch(text):
            return cls(int(match.group(2)), int(match.group(1)))
        raise InvalidMonth(f"not a month: {value!r}")

    @classmethod
    def from_date(cls, day: date) -> YearMonth:
        return cls(day.year, day.month)

    @property
    def first_day(self) -> date:
        return date(self.year, self.month, 1)

    @property
    def last_day(self) -> date:
        return date(self.year, self.month, calendar.monthrange(self.year, self.month)[1])

    @property
    def days(self) -> int:
        return calendar.monthrange(self.year, self.month)[1]

    def contains(self, day: date) -> bool:
        return (day.year, day.month) == (self.year, self.month)

    def prev(self) -> YearMonth | None:
        """The month before, or ``None`` at ``MIN_MONTH``."""
        year, month = (self.year - 1, 12) if self.month == 1 else (self.year, self.month - 1)
        return None if (year, month) < (MIN_YEAR, 1) else YearMonth(year, month)

    def next(self) -> YearMonth | None:
        """The month after, or ``None`` at ``MAX_MONTH``."""
        year, month = (self.year + 1, 1) if self.month == 12 else (self.year, self.month + 1)
        return None if (year, month) > (MAX_YEAR, 12) else YearMonth(year, month)


MIN_YEAR = 1900
MAX_YEAR = 2199
MIN_MONTH = YearMonth(MIN_YEAR, 1)
MAX_MONTH = YearMonth(MAX_YEAR, 12)
