"""The month summary (F2.4): reads the month's data and runs ``domain.summary.summarize_month``.

Every member may read it. The month's non-deleted transactions, its planned lines and the
workspace's categories (in the user's order, archived included) are read in one session and
summed in Python, in integer minor units of the workspace's base currency.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from assetboard.db.repositories import (
    BudgetLineRepository,
    CategoryRepository,
    CurrentWorkspaceRepository,
    TransactionRepository,
)
from assetboard.domain.money import Money
from assetboard.domain.months import YearMonth
from assetboard.domain.summary import (
    Entry,
    MonthSummary,
    PlanLine,
    SortOrder,
    SummaryCategory,
    summarize_month,
)
from assetboard.services.budget import PlanCategory
from assetboard.services.context import Context, Role, require_role


@dataclass(frozen=True, slots=True)
class SummaryData:
    summary: MonthSummary
    categories: dict[UUID, PlanCategory]  # every category of the workspace, by id


def month_summary(
    factory: sessionmaker[Session], ctx: Context, month: YearMonth, *, sort: SortOrder = "actual"
) -> SummaryData:
    """The summary of ``month`` (planned, actual, difference, totals, KPIs) and the
    categories it names."""
    require_role(ctx, Role.VIEWER)
    with factory() as session:
        currency = CurrentWorkspaceRepository(session, ctx).get().base_currency
        rows = CategoryRepository(session, ctx).ordered()
        categories = [
            SummaryCategory(
                id=c.id, kind=c.kind, treatment=c.treatment, archived=c.archived_at is not None
            )
            for c in rows
        ]
        names = {
            c.id: PlanCategory(
                id=c.id,
                name=c.name,
                kind=c.kind,
                treatment=c.treatment,
                color=c.color,
                archived=c.archived_at is not None,
            )
            for c in rows
        }
        entries = [
            Entry(row.category_id, Money(row.amount_minor, row.currency))
            for row in TransactionRepository(session, ctx).for_month(month)
        ]
        lines = [
            PlanLine(line.category_id, Money(line.planned_minor, line.currency))
            for line in BudgetLineRepository(session, ctx).for_month(month)
        ]
    summary = summarize_month(entries, lines, categories, currency=currency, sort=sort)
    return SummaryData(summary=summary, categories=names)
