"""Month pages (F2.5): ``/m/YYYY-MM`` with navigation, the month picker, closing and reopening.

The page is the shell that F2.2-F2.4 fill (transactions, planned values, summary). Authorization
happens in ``services.months``; plain form posts redirect (303) back to the month, so the page
works without JavaScript. Every state-changing route is covered by the router-level
``require_csrf`` dependency.
"""

from __future__ import annotations

from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Query, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from assetboard.db.repositories import NotFound
from assetboard.domain.months import InvalidMonth, YearMonth
from assetboard.i18n import gettext
from assetboard.i18n.dates import format_day, format_month, format_month_heading
from assetboard.services.months import (
    MonthError,
    close_month,
    current_month,
    get_month,
    reopen_month,
)
from assetboard.web.context import CurrentContext
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
}
NOTICES = {
    "closed": _("Month closed."),
    "reopened": _("Month reopened."),
    "already_closed": _("This month was already closed."),
    "already_open": _("This month is already open."),
}


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


@router.get("/m/{month}", name="month")
async def month_page(
    request: Request, ctx: CurrentContext, month: str, notice: str = "", error: str = ""
) -> Response:
    target = _parse(month)
    view = await run_in_threadpool(get_month, request.app.state.session_factory, ctx, target)
    locale = request.state.locale
    closed_on = None
    if view.closed_at is not None:
        local = view.closed_at.astimezone(ZoneInfo(ctx.workspace.timezone)).date()
        closed_on = format_day(local, locale)
    context: dict[str, object] = {
        "view": view,
        "month_heading": format_month_heading(view.month, locale),  # heading and title
        "month_name": format_month(view.month, locale),  # in sentences (confirmations)
        "prev_name": format_month(view.prev, locale) if view.prev else None,
        "next_name": format_month(view.next, locale) if view.next else None,
        "closed_on": closed_on,
        "notice": _t(request, NOTICES[notice]) if notice in NOTICES else None,
        "error": _t(request, ERRORS[error]) if error in ERRORS else None,
    }
    return render_template(request, "months/page.html", context=context)


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
