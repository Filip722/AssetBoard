"""Month states of the current workspace (F2.5)."""

from __future__ import annotations

from assetboard.db.models.ledger import MonthState
from assetboard.db.repositories.base import ScopedRepository
from assetboard.domain.months import YearMonth


class MonthStateRepository(ScopedRepository[MonthState]):
    """Rows are never deleted: reopening a month clears its closed fields (generic ``update``).

    Months are passed as ``YearMonth`` and stored as their first day, so no other day can be
    stored.
    """

    model = MonthState
    unsupported = frozenset({"delete"})

    def for_month(self, month: YearMonth) -> MonthState | None:
        statement = self._scoped_select().where(MonthState.month == month.first_day)
        return self._session.scalars(statement).one_or_none()

    def create(self, month: YearMonth) -> MonthState:
        """An open row for ``month`` (the caller then closes it)."""
        return self.add(MonthState(month=month.first_day))

    def closed_months(self) -> frozenset[YearMonth]:
        """Every month of the workspace that is closed now."""
        statement = self._scoped_select().where(MonthState.closed_at.is_not(None))
        return frozenset(YearMonth.from_date(row.month) for row in self._session.scalars(statement))
