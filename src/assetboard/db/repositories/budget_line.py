"""Budget lines (planned values) of the current workspace (F2.3)."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import exists, select

from assetboard.db.models.ledger import BudgetLine
from assetboard.db.repositories.base import ScopedRepository
from assetboard.domain.months import YearMonth


@dataclass(frozen=True, slots=True)
class LineUsage:
    references: int  # lines of the category (any month)
    months: frozenset[YearMonth]  # the months they plan


class BudgetLineRepository(ScopedRepository[BudgetLine]):
    """A zero plan has no row: lines are removed with the generic ``delete``, changed with
    ``update``. Months are passed as ``YearMonth`` and stored as their first day."""

    model = BudgetLine

    def create(
        self, *, month: YearMonth, category_id: UUID, planned_minor: int, currency: str
    ) -> BudgetLine:
        return self.add(
            BudgetLine(
                month=month.first_day,
                category_id=category_id,
                planned_minor=planned_minor,
                currency=currency,
            )
        )

    def for_month(self, month: YearMonth) -> list[BudgetLine]:
        """The month's lines (ordered by id only for determinism; callers order by category)."""
        statement = (
            self._scoped_select().where(BudgetLine.month == month.first_day).order_by(BudgetLine.id)
        )
        return list(self._session.scalars(statement))

    def line(self, month: YearMonth, category_id: UUID) -> BudgetLine | None:
        statement = self._scoped_select().where(
            BudgetLine.month == month.first_day, BudgetLine.category_id == category_id
        )
        return self._session.scalars(statement).one_or_none()

    def usage_by_category(self) -> dict[UUID, LineUsage]:
        """Per category: how many lines point at it, and their months."""
        statement = select(BudgetLine.category_id, BudgetLine.month).where(
            BudgetLine.workspace_id == self._workspace_id
        )
        found: dict[UUID, list[YearMonth]] = {}
        for category_id, month in self._session.execute(statement):
            found.setdefault(category_id, []).append(YearMonth.from_date(month))
        return {
            category_id: LineUsage(references=len(months), months=frozenset(months))
            for category_id, months in found.items()
        }

    def any_exists(self) -> bool:
        """Whether the workspace has any line (in any month)."""
        statement = select(exists().where(BudgetLine.workspace_id == self._workspace_id))
        return bool(self._session.execute(statement).scalar_one())
