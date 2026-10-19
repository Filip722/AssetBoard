"""The month summary block (F2.3 part B, F2.4): view models and messages.

The block is ``#month-summary`` (``months/_summary.html``): KPI cards, planned-vs-actual bars
and one row per category in the summary's groups (expenses, saved / invested, income, withdrawn
from savings, and the plan-only "Transfers (not counted)" while such lines exist), each with its
planned value (F2.3: editable inline for editors and owners while the month is open), actual and
difference (F2.4), and a totals row per group.

Shared by ``web.month_routes`` (the page), ``web.transaction_routes`` (every transaction write
sends the block out of band) and ``web.plan_routes`` (the planned-value writes). Everything here
is read-only; writes and their authorization live in ``services.budget``.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from fastapi import Request
from sqlalchemy.orm import Session, sessionmaker

from assetboard.domain.categories import Treatment
from assetboard.domain.money import Money
from assetboard.domain.summary import SortOrder
from assetboard.i18n import gettext
from assetboard.i18n.dates import format_month_after_from
from assetboard.i18n.numbers import format_amount_input, format_money, format_rate_tenths
from assetboard.services.budget import CopyResult, PlanCategory, get_plan
from assetboard.services.context import Context, Role, has_role
from assetboard.services.months import MonthView
from assetboard.services.summary import month_summary
from assetboard.web.security import summary_sort_cookie_name


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
    editable: bool  # the planned cell is an input (writers; plannable or with a line)
    value: str  # the input's value (locale decimal sign, no grouping); "" without a line
    saved_value: str  # the stored value in the same form (Esc restores it)
    display: str  # the formatted planned amount for read-only cells (0.00 without a line)
    error: str | None
    focus: bool
    actual: str = ""  # "" for the plan-only transfer group
    difference: str = ""
    tone: str = ""  # "favourable", "unfavourable" or "even" (for the difference)


@dataclass(frozen=True, slots=True)
class TotalsView:
    planned: str
    actual: str
    difference: str
    tone: str


@dataclass(frozen=True, slots=True)
class PlanGroupView:
    key: str
    heading: str
    rows: tuple[PlanRowView, ...]
    totals: TotalsView | None = None  # None for the plan-only transfer group


@dataclass(frozen=True, slots=True)
class KpiView:
    earned: str
    spent: str
    saved: str
    net: str
    net_tone: str
    savings_rate: str | None  # None: nothing earned ("—")


@dataclass(frozen=True, slots=True)
class BarView:
    key: str  # "expense" or "income"
    planned: str
    actual: str
    planned_width: int  # 0-100, of max(planned, actual)
    actual_width: int
    over: bool  # actual above planned


@dataclass(frozen=True, slots=True)
class SummaryView:
    month: str
    can_write: bool  # editor or owner, and the month is open
    groups: tuple[PlanGroupView, ...]  # only groups with rows
    banner_source: str | None  # "Copy plan from <month>" is offered (month after "from")
    copied: CopiedNotice | None
    kpis: KpiView | None = None
    bars: tuple[BarView, ...] = ()
    sort: str = "actual"
    zero_input: str = "0"  # the placeholder of an empty planned cell ("0.00", "0,00")
    filter_id: str = ""  # the category the transaction tables are filtered to (aria-current)

    def row(self, category_id: UUID) -> PlanRowView | None:
        wanted = str(category_id)
        return next(
            (r for g in self.groups for r in g.rows if r.category_id == wanted),
            None,
        )

    def group_of(self, category_id: UUID) -> PlanGroupView | None:
        wanted = str(category_id)
        return next((g for g in self.groups if any(r.category_id == wanted for r in g.rows)), None)


def _tone(amount: Money) -> str:
    if amount.amount_minor > 0:
        return "favourable"
    return "unfavourable" if amount.amount_minor < 0 else "even"


def _signed(amount: Money, locale: str) -> str:
    """A difference with its sign: ``+€400.00``, ``-€20.00``, ``€0.00``."""
    text = format_money(amount, locale)
    return f"+{text}" if amount.amount_minor > 0 else text


def _width(part: int, whole: int) -> int:
    return part * 100 // whole if whole > 0 else 0


def _bar(key: str, planned: Money, actual: Money, locale: str) -> BarView:
    scale = max(planned.amount_minor, actual.amount_minor)
    return BarView(
        key=key,
        planned=format_money(planned, locale),
        actual=format_money(actual, locale),
        planned_width=_width(planned.amount_minor, scale),
        actual_width=_width(actual.amount_minor, scale),
        over=actual.amount_minor > planned.amount_minor,
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
    sort: SortOrder | None = None,
    filter_id: UUID | None = None,
) -> SummaryView:
    """The ``#month-summary`` block of ``view.month`` (blocking: run in a thread).

    The rows, totals and KPIs come from ``services.summary.month_summary`` (sorted as the
    browser's ``summary_sort`` cookie says); the plan (``services.budget.get_plan``) adds the
    plan-only transfer group, which categories take new planned values, and the copy banner.
    """
    locale = request.state.locale
    sort = sort or summary_sort(request)
    plan = get_plan(factory, ctx, view.month)
    data = month_summary(factory, ctx, view.month, sort=sort)
    summary = data.summary
    can_write = has_role(ctx, Role.EDITOR) and not view.closed
    first = focus_first and can_write

    def row_view(category: PlanCategory, planned: Money | None, **extra: str) -> PlanRowView:
        nonlocal first
        has_line = planned is not None and planned.amount_minor > 0
        plannable = not category.archived and category.treatment != Treatment.TRANSFER
        editable = can_write and (plannable or has_line)
        saved = format_amount_input(planned, locale) if planned is not None and has_line else ""
        value, error, focus = saved, None, False
        if cell is not None and cell.category_id == category.id and editable:
            value, error, focus = cell.value, cell.error, cell.focus
        if first and editable:
            focus, first = True, False
        shown = planned if planned is not None else Money(0, summary.currency)
        return PlanRowView(
            category_id=str(category.id),
            name=category.name,
            color=category.color,
            archived=category.archived,
            editable=editable,
            value=value,
            saved_value=saved,
            display=format_money(shown, locale),
            error=error,
            focus=focus,
            **extra,
        )

    groups: list[PlanGroupView] = []
    for block in summary.blocks:
        rows = tuple(
            row_view(
                data.categories[r.category_id],
                r.planned,
                actual=format_money(r.actual, locale),
                difference=_signed(r.difference, locale),
                tone=_tone(r.difference),
            )
            for r in block.rows
        )
        if not rows:
            continue
        totals = TotalsView(
            planned=format_money(block.planned, locale),
            actual=format_money(block.actual, locale),
            difference=_signed(block.difference, locale),
            tone=_tone(block.difference),
        )
        heading = gettext(GROUP_HEADINGS[block.key], locale)
        groups.append(PlanGroupView(block.key, heading, rows, totals))
    transfers = plan.group("transfer")
    if transfers is not None and transfers.rows:
        rows = tuple(row_view(r.category, r.planned) for r in transfers.rows)
        groups.append(PlanGroupView("transfer", gettext(GROUP_HEADINGS["transfer"], locale), rows))
    kpis = summary.kpis
    rate = kpis.savings_rate_tenths
    kpi_view = KpiView(
        earned=format_money(kpis.earned, locale),
        spent=format_money(kpis.spent, locale),
        saved=format_money(kpis.saved, locale),
        net=format_money(kpis.net, locale),
        net_tone=_tone(kpis.net),
        savings_rate=format_rate_tenths(rate, locale) if rate is not None else None,
    )
    expense, income = summary.block("expense"), summary.block("income")
    bars = (
        _bar("expense", expense.planned, expense.actual, locale),
        _bar("income", income.planned, income.actual, locale),
    )
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
        kpis=kpi_view,
        bars=bars,
        sort=sort,
        zero_input=format_amount_input(Money(0, summary.currency), locale),
        filter_id=str(filter_id) if filter_id is not None else "",
    )


def summary_sort(request: Request) -> SortOrder:
    """The browser's chosen order of the summary rows (cookie ``summary_sort``; owner, P4)."""
    value = request.cookies.get(summary_sort_cookie_name(request.app.state.settings), "")
    return "manual" if value == "manual" else "actual"


def summary_context(summary: SummaryView) -> dict[str, object]:
    """The ``#month-summary`` block's context (``months/_summary.html``)."""
    return {"summary": summary}


def plan_error(request: Request, code: str, currency: str) -> str:
    """The translated message for an ``InvalidPlannedValue`` code."""
    text = gettext(PLAN_ERRORS.get(code, PLAN_ERRORS["invalid_amount"]), request.state.locale)
    return text % {"currency": currency} if code == "too_many_decimals" else text
