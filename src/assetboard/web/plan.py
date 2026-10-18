"""The month summary block with its planned values (F2.3 part B): view models and messages.

The block is ``#month-summary`` (``months/_summary.html``). F2.3 fills it with the plan: one row
per category in F2.4's groups (expenses, saved / invested, income, withdrawn from savings, and
"Transfers (not counted)" only while such lines exist), each with its planned value, editable
inline for editors and owners while the month is open. F2.4 adds the actual and difference
columns, totals, KPIs and bars to the same rows.

Shared by ``web.month_routes`` (the page), ``web.transaction_routes`` (every transaction write
sends the block out of band) and ``web.plan_routes`` (the planned-value writes). Everything here
is read-only; writes and their authorization live in ``services.budget``.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from fastapi import Request
from sqlalchemy.orm import Session, sessionmaker

from assetboard.i18n import gettext
from assetboard.i18n.dates import format_month_after_from
from assetboard.i18n.numbers import format_amount_input, format_money
from assetboard.services.budget import CopyResult, get_plan
from assetboard.services.context import Context, Role, has_role
from assetboard.services.months import MonthView


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


GROUP_HEADINGS = {
    "expense": _("Expenses"),
    "saved": _("Saved / invested"),
    "income": _("Income"),
    "withdrawn": _("Withdrawn from savings"),
    "transfer": _("Transfers (not counted)"),
}
# One message per ``services.budget.InvalidPlannedValue`` code.
PLAN_ERRORS = {
    "invalid_amount": _("Enter a valid amount."),
    "amount_negative": _("A planned value cannot be negative."),
    "amount_too_large": _("The amount is too large."),
    "too_many_decimals": _("Too many decimal places for %(currency)s."),
    "category_archived": _("This category is archived: it takes no new planned values."),
    "category_transfer": _("Transfers are not counted in the plan: this category takes none."),
}
PLAN_STATUS = {
    "created": _("Planned value saved."),
    "updated": _("Planned value saved."),
    "unchanged": _("Planned value saved."),
    "removed": _("Planned value removed."),
}


@dataclass(frozen=True, slots=True)
class CellState:
    """What a request adds to one planned cell: the typed value and its message."""

    category_id: UUID
    value: str
    error: str | None = None
    focus: bool = False


@dataclass(frozen=True, slots=True)
class CopiedNotice:
    count: int
    skipped: int
    source: str  # the source month after "from"
    automatic: bool


@dataclass(frozen=True, slots=True)
class PlanRowView:
    category_id: str
    name: str
    color: str
    archived: bool
    value: str  # the input's value (locale decimal sign, no grouping); "" without a line
    saved_value: str  # the stored value in the same form (Esc restores it)
    display: str  # the formatted amount for read-only cells; "" without a line
    error: str | None
    focus: bool


@dataclass(frozen=True, slots=True)
class PlanGroupView:
    key: str
    heading: str
    rows: tuple[PlanRowView, ...]


@dataclass(frozen=True, slots=True)
class SummaryView:
    month: str
    can_write: bool  # editor or owner, and the month is open
    groups: tuple[PlanGroupView, ...]  # only groups with rows
    banner_source: str | None  # "Copy plan from <month>" is offered (month after "from")
    copied: CopiedNotice | None

    def row(self, category_id: UUID) -> PlanRowView | None:
        wanted = str(category_id)
        return next(
            (r for g in self.groups for r in g.rows if r.category_id == wanted),
            None,
        )


def build_summary(
    factory: sessionmaker[Session],
    ctx: Context,
    request: Request,
    view: MonthView,
    *,
    cell: CellState | None = None,
    copied: CopyResult | None = None,
    automatic: bool = False,
    focus_first: bool = False,
) -> SummaryView:
    """The ``#month-summary`` block of ``view.month`` (blocking: run in a thread)."""
    locale = request.state.locale
    plan = get_plan(factory, ctx, view.month)
    can_write = has_role(ctx, Role.EDITOR) and not view.closed
    groups: list[PlanGroupView] = []
    first = focus_first and can_write
    for group in plan.groups:
        rows: list[PlanRowView] = []
        for row in group.rows:
            saved = format_amount_input(row.planned, locale) if row.planned else ""
            value = saved
            display = format_money(row.planned, locale) if row.planned else ""
            error, focus = None, False
            if cell is not None and cell.category_id == row.category.id and can_write:
                value, error, focus = cell.value, cell.error, cell.focus
            if first:
                focus, first = True, False
            rows.append(
                PlanRowView(
                    category_id=str(row.category.id),
                    name=row.category.name,
                    color=row.category.color,
                    archived=row.category.archived,
                    value=value,
                    saved_value=saved,
                    display=display,
                    error=error,
                    focus=focus,
                )
            )
        if rows:
            heading = gettext(GROUP_HEADINGS[group.key], locale)
            groups.append(PlanGroupView(group.key, heading, tuple(rows)))
    banner = None
    if can_write and not plan.has_lines and plan.copyable_from_previous and plan.previous:
        banner = format_month_after_from(plan.previous, locale)
    notice = None
    if copied is not None and can_write:
        notice = CopiedNotice(
            count=copied.copied,
            skipped=copied.skipped,
            source=format_month_after_from(copied.source, locale),
            automatic=automatic,
        )
    return SummaryView(
        month=str(view.month),
        can_write=can_write,
        groups=tuple(groups),
        banner_source=banner,
        copied=notice,
    )


def summary_context(summary: SummaryView) -> dict[str, object]:
    """The ``#month-summary`` block's context (``months/_summary.html``)."""
    return {"summary": summary}


def plan_error(request: Request, code: str, currency: str) -> str:
    """The translated message for an ``InvalidPlannedValue`` code."""
    text = gettext(PLAN_ERRORS.get(code, PLAN_ERRORS["invalid_amount"]), request.state.locale)
    return text % {"currency": currency} if code == "too_many_decimals" else text
