"""The month page's transaction tables (F2.2 part B): view models and messages.

Shared by ``web.month_routes`` (the page) and ``web.transaction_routes`` (the writes, which
answer HTMX requests with one re-rendered table section). Everything here is read-only; the
writes and their authorization live in ``services.transactions``.

Each table (expense, income) is a ``SectionView``. Its ``SectionState`` carries what a request
adds to the stored rows: the values and messages of the entry row or of the row being edited, a
notice, and which element the browser focuses after the swap (``data-autofocus``):
``entry:<field>``, ``edit:<field>`` or ``row:<id>`` (the row's Edit button).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from uuid import UUID

from fastapi import Request
from sqlalchemy.orm import Session, sessionmaker

from assetboard.domain.months import YearMonth
from assetboard.domain.transactions import MAX_DESCRIPTION_LENGTH, TransactionDirection
from assetboard.i18n import gettext
from assetboard.i18n.dates import format_day
from assetboard.i18n.numbers import format_amount_input, format_money
from assetboard.services.categories import list_categories
from assetboard.services.context import Context, Role, has_role
from assetboard.services.months import MonthView
from assetboard.services.transactions import TransactionView, list_month


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


DIRECTIONS = (TransactionDirection.EXPENSE.value, TransactionDirection.INCOME.value)
FIELDS = ("day", "amount", "description", "category")
# A day as typed into the entry row (``?day=`` keeps it after a save without JavaScript): the
# forms ``domain.transactions.parse_entry_date`` reads, ASCII digits only.
ENTRY_DAY = re.compile(
    r"[0-9]{1,2}"  # a day of the viewed month
    r"|[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}"  # 2026-10-07
    r"|[0-9]{1,2}\.[0-9]{1,2}\.[0-9]{4}"  # 07.10.2026
)
FALLBACK_ERROR = _("The change could not be made. Reload the page and try again.")

# One message per error code of ``services.transactions.InvalidTransaction`` (part A).
FIELD_ERRORS: dict[str, str] = {
    "invalid_date": _(
        "Enter a day of the month (1-31) or a date such as 2026-10-07 or 07.10.2026."
    ),
    "day_not_in_month": _("That month has no such day."),
    "date_out_of_range": _("Enter a date between the years 1900 and 2199."),
    "invalid_amount": _("Enter a valid amount."),
    "amount_not_positive": _("The amount must be greater than zero."),
    "amount_too_large": _("The amount is too large."),
    "too_many_decimals": _("Too many decimal places for %(currency)s."),
    "invalid_description": _("The description contains characters that are not allowed."),
    "description_too_long": _("Use at most %(max)d characters."),
    "category_required": _("Choose a category."),
    "invalid_category": _("Choose one of the listed categories."),
    "category_kind_mismatch": "",  # by direction, see KIND_MISMATCH
    "category_archived": _("This category is archived. Choose another one."),
    "invalid_direction": FALLBACK_ERROR,
}
KIND_MISMATCH = {
    TransactionDirection.EXPENSE.value: _("Choose an expense category."),
    TransactionDirection.INCOME.value: _("Choose an income category."),
}
MONTH_CLOSED_ELSEWHERE = _("%(month)s is closed. Choose a date in an open month.")
NOT_SAVED = _("The entry was not saved. Check the marked fields.")
HEADINGS = {
    TransactionDirection.EXPENSE.value: _("Expenses"),
    TransactionDirection.INCOME.value: _("Income"),
}
EMPTY = {
    TransactionDirection.EXPENSE.value: _("No expenses in this month."),
    TransactionDirection.INCOME.value: _("No income in this month."),
}
NO_CATEGORIES = {
    TransactionDirection.EXPENSE.value: _("Add an expense category first."),
    TransactionDirection.INCOME.value: _("Add an income category first."),
}


def field_messages(
    request: Request, errors: Mapping[str, str], *, direction: str, currency: str
) -> dict[str, str]:
    """Translated messages for ``InvalidTransaction.errors`` (field -> code)."""
    locale = request.state.locale
    messages: dict[str, str] = {}
    for name, code in errors.items():
        if code == "category_kind_mismatch":
            text = gettext(KIND_MISMATCH.get(direction, FALLBACK_ERROR), locale)
        elif code == "too_many_decimals":
            text = gettext(FIELD_ERRORS[code], locale) % {"currency": currency}
        elif code == "description_too_long":
            text = gettext(FIELD_ERRORS[code], locale) % {"max": MAX_DESCRIPTION_LENGTH}
        else:
            text = gettext(FIELD_ERRORS.get(code) or FALLBACK_ERROR, locale)
        messages[name] = text
    return messages


def first_invalid(errors: Mapping[str, str]) -> str:
    """The first invalid field in tab order (where the focus goes after a refused save)."""
    return next((name for name in FIELDS if name in errors), "day")


@dataclass(frozen=True, slots=True)
class SectionState:
    entry_values: Mapping[str, str] = field(default_factory=dict[str, str])
    entry_errors: Mapping[str, str] = field(default_factory=dict[str, str])
    editing: UUID | None = None
    edit_values: Mapping[str, str] = field(default_factory=dict[str, str])
    edit_errors: Mapping[str, str] = field(default_factory=dict[str, str])
    notice: str | None = None
    notice_href: str | None = None
    error: str | None = None
    focus: str | None = None


@dataclass(frozen=True, slots=True)
class RowView:
    t: TransactionView
    full_date: str
    amount: str


@dataclass(frozen=True, slots=True)
class SectionView:
    direction: str
    heading: str
    empty: str
    no_categories: str
    rows: tuple[RowView, ...]
    categories: tuple[tuple[str, str], ...]  # (id, name): active categories of this kind
    edit_categories: tuple[tuple[str, str], ...]  # plus the edited row's archived category
    state: SectionState


@dataclass(frozen=True, slots=True)
class LedgerView:
    month: YearMonth
    can_write: bool  # editor or owner, and the month is open
    sections: tuple[SectionView, ...]
    max_description: int = MAX_DESCRIPTION_LENGTH

    def section(self, direction: str) -> SectionView:
        return next(s for s in self.sections if s.direction == direction)


def edit_values(row: TransactionView, month: YearMonth, locale: str) -> dict[str, str]:
    """The edit row's initial values: a day number within ``month``, else the full date."""
    day = str(row.date.day) if row.month == month else row.date.isoformat()
    return {
        "day": day,
        "amount": format_amount_input(row.amount, locale),
        "description": row.description,
        "category_id": str(row.category_id),
    }


def build_ledger(
    factory: sessionmaker[Session],
    ctx: Context,
    request: Request,
    view: MonthView,
    states: Mapping[str, SectionState] | None = None,
) -> LedgerView:
    """Both tables of ``view.month`` with the requests' states (blocking: run in a thread)."""
    locale = request.state.locale
    transactions = list_month(factory, ctx, view.month)
    categories = list_categories(factory, ctx, usage_sources=())
    can_write = has_role(ctx, Role.EDITOR) and not view.closed
    sections: list[SectionView] = []
    for direction in DIRECTIONS:
        state = (states or {}).get(direction, SectionState())
        stored = transactions.expense if direction == "expense" else transactions.income
        rows = tuple(
            RowView(t, format_day(t.date, locale), format_money(t.amount, locale)) for t in stored
        )
        active = categories.expense if direction == "expense" else categories.income
        options = tuple((str(c.id), c.name) for c in active)
        edit_options = options
        ids = {r.t.id for r in rows}
        if not can_write:  # read-only: no entry row, no edit row, nothing to focus
            state = SectionState(
                notice=state.notice, notice_href=state.notice_href, error=state.error
            )
        elif state.editing is not None and state.editing not in ids:
            focus = None if (state.focus or "").startswith("edit:") else state.focus
            state = replace(state, editing=None, edit_values={}, edit_errors={}, focus=focus)
        elif state.editing is not None:
            edited = next(r.t for r in rows if r.t.id == state.editing)
            if edited.category_archived:
                archived = gettext("%(name)s (archived)", locale) % {"name": edited.category_name}
                edit_options = (*options, (str(edited.category_id), archived))
        sections.append(
            SectionView(
                direction=direction,
                heading=gettext(HEADINGS[direction], locale),
                empty=gettext(EMPTY[direction], locale),
                no_categories=gettext(NO_CATEGORIES[direction], locale),
                rows=rows,
                categories=options,
                edit_categories=edit_options,
                state=state,
            )
        )
    return LedgerView(month=view.month, can_write=can_write, sections=tuple(sections))


def summary_context(ledger: LedgerView) -> dict[str, object]:
    """The ``#month-summary`` block's context. Empty until F2.4 builds the month summary.

    Every transaction write sends the block out of band (``months/_summary.html``), so F2.4's
    totals refresh without a reload once this returns them.
    """
    del ledger
    return {}
