"""What other features tell the categories service about a category's use (F2.1 seam).

A usage source returns, per category id, a ``CategoryUsage``. Sources are listed statically in
``services.categories.USAGE_SOURCES`` (no import-time registration, so the result never depends
on which modules happen to be imported).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy.orm import Session

from assetboard.domain.months import YearMonth
from assetboard.services.context import Context


@dataclass(frozen=True, slots=True)
class CategoryUsage:
    references: int = 0  # every row that points at the category (blocks delete and kind change)
    active_transactions: int = 0  # non-deleted transactions (shown when the treatment changes)
    # Months whose totals depend on the category's treatment (non-deleted rows only).
    active_months: frozenset[YearMonth] = field(default_factory=frozenset[YearMonth])

    def __add__(self, other: CategoryUsage) -> CategoryUsage:
        return CategoryUsage(
            references=self.references + other.references,
            active_transactions=self.active_transactions + other.active_transactions,
            active_months=self.active_months | other.active_months,
        )


UsageSource = Callable[[Session, Context], Mapping[UUID, CategoryUsage]]
