"""Month pages (F2.5): ``/m/YYYY-MM`` with navigation, the month picker, closing and reopening.

The page is the shell that F2.2-F2.4 fill: F2.2 adds the transaction tables
(``web.ledger``; the writes are in ``web.transaction_routes``), F2.3 planned values and F2.4
the summary. F2.3 fills the summary block with the planned values (``web.plan``; the writes are
in ``web.plan_routes``) and runs the automatic plan copy when an editor opens a month page
(``services.budget.auto_copy_on_open``: a GET that writes at most once per month, by design).
Authorization happens in ``services.months``; plain form posts redirect (303)
back to the month, so the page works without JavaScript. Every state-changing route is covered
by the router-level ``require_csrf`` dependency.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import replace
from typing import Annotated
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from assetboard.db.repositories import NotFound
from assetboard.domain.months import InvalidMonth, YearMonth
from assetboard.i18n import gettext
from assetboard.i18n.dates import format_day, format_month, format_month_heading
from assetboard.services.budget import CopyResult, auto_copy_on_open
from assetboard.services.context import Context
from assetboard.services.months import (
    MonthError,
    close_month,
    current_month,
    get_month,
    reopen_month,
)
from assetboard.services.transactions import get_transaction, pending_undo
from assetboard.web.context import CurrentContext
from assetboard.web.ledger import (
    DIRECTIONS,
    ENTRY_DAY,
    SectionState,
    build_ledger,
    edit_values,
    parse_filter,
)
from assetboard.web.plan import PLAN_ERRORS, CellState, build_summary, summary_context
from assetboard.web.rendering import render_template
from assetboard.web.security import require_csrf

router = APIRouter(dependencies=[Depends(require_csrf)])


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


# Shown wherever a write is refused because its month is closed (F2.2/F2.3 reuse it; the
# application-wide ``MonthClosed`` handler shows it too).
MONTH_CLOSED_TITLE = _("Month closed")
MONTH_CLOSED_MESSAGE = _(
    "This month is closed. Its entries and planned values cannot change until an owner reopens it."
)
ERRORS = {
    "invalid_month": _("Enter a month as YYYY-MM, for example 2026-10."),
    "month_in_future": _("Future months cannot be closed; they stay open for planning."),
    "month_closed": MONTH_CLOSED_MESSAGE,
    "transaction_gone": _("This entry no longer exists. It may have been deleted elsewhere."),
    "undo_expired": _("Too late to undo: the entry stays deleted."),
    "plan_category_gone": _("This category no longer exists. It may have been deleted elsewhere."),
    "plan_category_archived": PLAN_ERRORS["category_archived"],
    "plan_category_transfer": PLAN_ERRORS["category_transfer"],
}
NOTICES = {
    "closed": _("Month closed."),
    "reopened": _("Month reopened."),
    "already_closed": _("This month was already closed."),
    "already_open": _("This month is already open."),
    "transaction_added": _("Entry added."),
    "transaction_saved": _("Entry saved."),
    "transaction_restored": _("Entry restored."),
    "outside_filter": _("Entry saved. Its category is not shown while this filter is on."),
    "plan_saved": _("Planned value saved."),
    "plan_removed": _("Planned value removed."),
}
SAVED_IN = _("Entry saved in %(month)s.")


def _t(request: Request, message: str) -> str:
    return gettext(message, request.state.locale)


def _parse(value: str) -> YearMonth:
    """The URL month; anything else (bad format, out of range) is a plain 404."""
    try:
        return YearMonth.parse(value)
    except InvalidMonth:
        raise NotFound from None


def _month_path(request: Request, month: YearMonth, query: str = "") -> str:
    path = request.url_for("month", month=str(month)).path
    return f"{path}?{query}" if query else path


@router.get("/m", name="month_current")
async def month_current(
    request: Request,
    ctx: CurrentContext,
    month: str | None = None,
    from_: Annotated[str, Query(alias="from")] = "",
) -> Response:
    """Today's month, or the month chosen in the picker (``?month=...&from=YYYY-MM``)."""
    if month is None:
        return RedirectResponse(_month_path(request, current_month(ctx)), status_code=303)
    try:
        target = YearMonth.parse_loose(month)
    except InvalidMonth:
        try:
            back = YearMonth.parse(from_)
        except InvalidMonth:
            back = current_month(ctx)
        return RedirectResponse(_month_path(request, back, "error=invalid_month"), status_code=303)
    return RedirectResponse(_month_path(request, target), status_code=303)


def month_context(
    request: Request,
    ctx: Context,
    target: YearMonth,
    *,
    notice: str = "",
    error: str = "",
    states: Mapping[str, SectionState] | None = None,
    deleted: UUID | None = None,
    saved_in: YearMonth | None = None,
    plan_cell: CellState | None = None,
    copied: CopyResult | None = None,
    automatic: bool = False,
    filter_id: UUID | None = None,
) -> dict[str, object]:
    """The month page's context (blocking: run in a thread). ``states`` carry the transaction
    tables' form values and messages; ``deleted`` offers Undo (pages without JavaScript) while
    that row of this month can still be restored; ``plan_cell`` is a refused planned value and
    ``copied`` a plan copy to report."""
    factory = request.app.state.session_factory
    view = get_month(factory, ctx, target)
    locale = request.state.locale
    closed_on = None
    if view.closed_at is not None:
        local = view.closed_at.astimezone(ZoneInfo(ctx.workspace.timezone)).date()
        closed_on = format_day(local, locale)
    ledger = build_ledger(factory, ctx, request, view, states, filter_id)
    pending = None
    if deleted is not None and ledger.can_write:
        pending = pending_undo(factory, ctx, deleted)
        if pending is not None and pending.month != target:
            pending = None
    notice_text = _t(request, NOTICES[notice]) if notice in NOTICES else None
    saved_in_href = None
    if saved_in is not None and saved_in != target:
        notice_text = _t(request, SAVED_IN) % {"month": format_month(saved_in, locale)}
        saved_in_href = _month_path(request, saved_in)
    return {
        "view": view,
        "month_heading": format_month_heading(view.month, locale),  # heading and title
        "month_name": format_month(view.month, locale),  # in sentences (confirmations)
        "prev_name": format_month(view.prev, locale) if view.prev else None,
        "next_name": format_month(view.next, locale) if view.next else None,
        "closed_on": closed_on,
        "notice": notice_text,
        "notice_href": saved_in_href,
        "error": _t(request, ERRORS[error]) if error in ERRORS else None,
        "ledger": ledger,
        **summary_context(
            build_summary(
                factory,
                ctx,
                request,
                view,
                cell=plan_cell,
                copied=copied,
                automatic=automatic,
                filter_id=ledger.filter.category_id if ledger.filter else None,
            )
        ),
        "deleted": pending.id if pending is not None else None,
    }


def _optional_uuid(value: str) -> UUID | None:
    try:
        return UUID(value) if value else None
    except ValueError:
        return None


_COUNT = re.compile(r"[0-9]{1,6}")  # ASCII digits only: str.isdigit() also accepts "²"


def _copied(month: YearMonth, copied: str, skipped: str) -> CopyResult | None:
    """The copy reported after a plain form post (``?plan_copied=N&plan_skipped=M``); anything
    malformed is ignored."""
    source = month.prev()
    skipped = skipped or "0"
    if source is None or not _COUNT.fullmatch(copied) or not _COUNT.fullmatch(skipped):
        return None
    return CopyResult(source=source, copied=int(copied), skipped=int(skipped), kept=0)


def _auto_copy(request: Request, ctx: Context, month: YearMonth) -> CopyResult | None:
    """The automatic plan copy (F2.3) for a full page view (blocking). HEAD never gets here
    (405); the check keeps a future HEAD route from writing."""
    if request.method != "GET":
        return None
    return auto_copy_on_open(request.app.state.session_factory, ctx, month)


def _optional_month(value: str) -> YearMonth | None:
    try:
        return YearMonth.parse(value) if value else None
    except InvalidMonth:
        return None


@router.get("/m/{month}", name="month")
async def month_page(
    request: Request,
    ctx: CurrentContext,
    month: str,
    notice: str = "",
    error: str = "",
    edit: str = "",
    deleted: str = "",
    saved_in: str = "",
    entry: str = "",
    day: str = "",
    plan_copied: str = "",
    plan_skipped: str = "",
    category: str = "",
) -> Response:
    """The month page. For browsers without JavaScript: ``?edit=<id>`` opens a row for
    editing, ``?deleted=<id>`` offers Undo, ``?entry=<direction>&day=<day>`` prefills that
    table's entry row with the day just used. ``?saved_in=YYYY-MM`` links an entry saved to
    another month. ``?plan_copied=N&plan_skipped=M`` reports a plan copy. ``?category=<id>``
    shows that category's entries only (F2.4; an unknown id shows everything). Opening the
    page may copy the previous month's plan (workspace setting,
    ``services.budget.auto_copy_on_open``)."""
    target = _parse(month)
    states: dict[str, SectionState] = {}
    editing = _optional_uuid(edit)
    if editing is not None:
        states = await run_in_threadpool(edit_states, request, ctx, target, editing)
    if entry in DIRECTIONS and ENTRY_DAY.fullmatch(day):
        kept = SectionState(entry_values={"day": day}, focus="entry:day")
        if entry in states:
            kept = replace(states[entry], entry_values=kept.entry_values)
        states[entry] = kept
    copied, automatic = _copied(target, plan_copied, plan_skipped), False
    if copied is None:
        copied = await run_in_threadpool(_auto_copy, request, ctx, target)
        automatic = copied is not None
    context = await run_in_threadpool(
        lambda: month_context(
            request,
            ctx,
            target,
            notice=notice,
            error=error,
            states=states,
            deleted=_optional_uuid(deleted),
            saved_in=_optional_month(saved_in),
            copied=copied,
            automatic=automatic,
            filter_id=parse_filter(category),
        )
    )
    return render_template(request, "months/page.html", context=context)


def edit_states(
    request: Request, ctx: Context, month: YearMonth, transaction_id: UUID
) -> dict[str, SectionState]:
    """The section state that opens ``transaction_id`` for editing (none if it is gone)."""
    try:
        row = get_transaction(request.app.state.session_factory, ctx, transaction_id)
    except NotFound:
        return {}
    values = edit_values(row, month, request.state.locale)
    return {row.direction: SectionState(editing=row.id, edit_values=values, focus="edit:day")}


@router.post("/m/{month}/close", name="month_close")
async def month_close(request: Request, ctx: CurrentContext, month: str) -> Response:
    target = _parse(month)
    try:
        changed = await run_in_threadpool(
            close_month, request.app.state.session_factory, ctx, target
        )
    except MonthError as exc:
        return RedirectResponse(_month_path(request, target, f"error={exc.code}"), status_code=303)
    notice = "closed" if changed else "already_closed"  # a no-op says so instead
    return RedirectResponse(_month_path(request, target, f"notice={notice}"), status_code=303)


@router.post("/m/{month}/reopen", name="month_reopen")
async def month_reopen(request: Request, ctx: CurrentContext, month: str) -> Response:
    target = _parse(month)
    changed = await run_in_threadpool(reopen_month, request.app.state.session_factory, ctx, target)
    notice = "reopened" if changed else "already_open"  # a no-op says so instead
    return RedirectResponse(_month_path(request, target, f"notice={notice}"), status_code=303)
