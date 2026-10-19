"""Transaction search (F2.6): the filter, the sort orders and the page window. Pure: no I/O.

``TransactionFilter`` is the query object the repository turns into SQL; the service builds it
from the user's input (dates, locale-formatted amounts) and validates it. Amount bounds are
minor units in the workspace's base currency; ``text_key`` is compared with the stored
``description_key`` ("contains", case-insensitive through ``domain.text.text_key``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from uuid import UUID

from assetboard.domain.text import text_key

PAGE_SIZE = 50
MAX_SEARCH_TEXT_LENGTH = 200


class SortOrder(StrEnum):
    """Orders of the list; ties are broken by entry order (``created_at``, then id)."""

    DATE_DESC = "-date"
    DATE_ASC = "date"
    AMOUNT_DESC = "-amount"
    AMOUNT_ASC = "amount"


DEFAULT_SORT = SortOrder.DATE_DESC


def parse_sort(value: str) -> SortOrder:
    """The sort order named by ``value``; anything else is the default (newest first)."""
    try:
        return SortOrder(value)
    except ValueError:
        return DEFAULT_SORT


@dataclass(frozen=True, slots=True)
class TransactionFilter:
    """Which non-deleted transactions match. Every bound is inclusive; ``None`` means open."""

    date_from: date | None = None
    date_to: date | None = None
    direction: str | None = None
    category_ids: frozenset[UUID] = frozenset()
    text_key: str = ""
    amount_min_minor: int | None = None
    amount_max_minor: int | None = None

    def __post_init__(self) -> None:
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from is after date_to")
        if (
            self.amount_min_minor is not None
            and self.amount_max_minor is not None
            and self.amount_min_minor > self.amount_max_minor
        ):
            raise ValueError("amount_min_minor is above amount_max_minor")


def search_key(text: str) -> str:
    """The key a search text is matched with (the same folding as ``description_key``)."""
    return text_key(text)


@dataclass(frozen=True, slots=True)
class PageWindow:
    """One page of ``total`` results: 1-based ``number``, clamped to the existing pages."""

    number: int
    size: int
    total: int

    @property
    def pages(self) -> int:
        return max(1, -(-self.total // self.size))

    @property
    def offset(self) -> int:
        return (self.number - 1) * self.size

    @property
    def first_item(self) -> int:
        """1-based position of the page's first row (0 when there are no rows)."""
        return self.offset + 1 if self.total else 0

    @property
    def last_item(self) -> int:
        return min(self.offset + self.size, self.total)

    @property
    def has_previous(self) -> bool:
        return self.number > 1

    @property
    def has_next(self) -> bool:
        return self.number < self.pages


def page_window(requested: int, total: int, size: int = PAGE_SIZE) -> PageWindow:
    """The page to show for a requested page number: below 1 is 1, past the end is the last."""
    if size < 1 or total < 0:
        raise ValueError("size must be positive and total not negative")
    pages = max(1, -(-total // size))
    return PageWindow(number=min(max(requested, 1), pages), size=size, total=total)
