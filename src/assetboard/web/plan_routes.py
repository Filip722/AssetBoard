"""Planned values on the month page (F2.3 part B): save one planned cell, copy the plan.

Authorization, validation and the closed-month guard happen in ``services.budget``; routes map
outcomes to pages. Every state-changing route is covered by the router-level ``require_csrf``
dependency (HTMX sends the token in the ``X-CSRF-Token`` header).

HTMX requests get one planned row back (with the copy banner and the screen-reader status line
out of band), or the whole ``#month-summary`` after a copy (its result also goes to the status
line). Plain form posts redirect (303) to
the month page, which re-renders a refused value with its message, so everything works without
JavaScript. A write refused because the month was closed meanwhile reloads the page with the
closed-month message (303, or ``HX-Redirect`` for HTMX): the page in the browser is stale. So
does a new value for a category that was archived or turned into a transfer meanwhile (its row
has left the plan): ``?error=plan_category_archived`` / ``plan_category_transfer``.
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from assetboard.db.repositories import NotFound
from assetboard.domain.months import YearMonth
from assetboard.domain.summary import SortOrder
from assetboard.i18n import gettext
from assetboard.services.budget import (
    CopyResult,
    InvalidPlannedValue,
    copy_previous_plan,
    set_planned,
)
from assetboard.services.context import Context
from assetboard.services.months import MonthClosed, get_month
from assetboard.web.context import CurrentContext
from assetboard.web.errors import is_htmx_fragment_request
from assetboard.web.month_routes import _month_path, _parse, month_context
from assetboard.web.plan import PLAN_STATUS, CellState, build_summary, plan_error, summary_context
from assetboard.web.rendering import render_template
from assetboard.web.security import (
    require_csrf,
    set_preference_cookie,
    summary_sort_cookie_name,
)

router = APIRouter(dependencies=[Depends(require_csrf)])

FormStr = Annotated[str, Form()]


def _category_id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise NotFound from None


def _redirect(request: Request, month: YearMonth, query: str, *, status_code: int) -> Response:
    """A 303 to the month page; for HTMX, ``HX-Redirect`` (the page itself is stale)."""
    location = _month_path(request, month, query)
    if is_htmx_fragment_request(request):
        response = Response(status_code=status_code)
        response.headers["HX-Redirect"] = location
        return response
    return RedirectResponse(location, status_code=303)


def _row(
    request: Request,
    ctx: Context,
    month: YearMonth,
    category_id: UUID,
    *,
    cell: CellState | None = None,
    status: str = "",
    status_code: int = 200,
) -> Response:
    """One planned row with the banner and the status line out of band (blocking)."""
    factory = request.app.state.session_factory
    summary = build_summary(factory, ctx, request, get_month(factory, ctx, month), cell=cell)
    context: dict[str, object] = {
        **summary_context(summary),
        "row_view": summary.row(category_id),
        "group_view": summary.group_of(category_id),
        "status": status,
    }
    response = render_template(
        request, "months/_plan_row.html", context=context, status_code=status_code
    )
    response.headers["Vary"] = "HX-Request"
    return response


@router.post("/m/{month}/plan/copy", name="plan_copy")
async def plan_copy(request: Request, ctx: CurrentContext, month: str) -> Response:
    target = _parse(month)
    factory = request.app.state.session_factory
    try:
        copied = await run_in_threadpool(copy_previous_plan, factory, ctx, target)
    except MonthClosed:
        return _redirect(request, target, "error=month_closed", status_code=409)
    if not is_htmx_fragment_request(request):
        query = ""
        if copied is not None:
            query = urlencode({"plan_copied": copied.copied, "plan_skipped": copied.skipped})
        return RedirectResponse(
            _month_path(request, target, query) + "#month-summary", status_code=303
        )
    return await run_in_threadpool(_summary, request, ctx, target, copied)


def _summary(
    request: Request,
    ctx: Context,
    month: YearMonth,
    copied: CopyResult | None,
    *,
    focus_first: bool = True,
    sort: SortOrder | None = None,
    alone: bool = False,
) -> Response:
    """The summary block; after a copy also its result on the status line (``alone``: only the
    block, so a re-sort leaves the status line as it was)."""
    factory = request.app.state.session_factory
    summary = build_summary(
        factory, ctx, request, get_month(factory, ctx, month), copied=copied,
        focus_first=focus_first, sort=sort,
    )  # fmt: skip
    template = "months/_summary.html" if alone else "months/_plan_copy.html"
    response = render_template(request, template, context=summary_context(summary))
    response.headers["Vary"] = "HX-Request"
    return response


@router.post("/m/{month}/summary/sort", name="summary_sort")
async def summary_sort_set(
    request: Request, ctx: CurrentContext, month: str, sort: FormStr = ""
) -> Response:
    """Choose the summary's row order (F2.4): largest actual first, or the user's category
    order. Kept in this browser (cookie; owner decision P4), so every later render, including
    the out-of-band summary after a transaction write, uses it."""
    target = _parse(month)
    chosen: SortOrder = "manual" if sort == "manual" else "actual"
    settings = request.app.state.settings
    if is_htmx_fragment_request(request):
        # The cookie arrives with this response; this render uses the chosen order directly.
        response = await run_in_threadpool(
            lambda: _summary(request, ctx, target, None, focus_first=False, sort=chosen, alone=True)
        )
    else:
        path = _month_path(request, target) + "#month-summary"
        response = RedirectResponse(path, status_code=303)
    set_preference_cookie(response, settings, summary_sort_cookie_name(settings), chosen)
    return response


@router.post("/m/{month}/plan/{category_id}", name="plan_set")
async def plan_set(
    request: Request, ctx: CurrentContext, month: str, category_id: str, amount: FormStr = ""
) -> Response:
    target = _parse(month)
    category = _category_id(category_id)
    try:
        saved = await run_in_threadpool(
            lambda: set_planned(
                request.app.state.session_factory,
                ctx,
                month=target,
                category_id=category,
                amount=amount,
                locale=request.state.locale,
            )
        )
    except NotFound:
        return _redirect(request, target, "error=plan_category_gone", status_code=404)
    except MonthClosed:
        return _redirect(request, target, "error=month_closed", status_code=409)
    except InvalidPlannedValue as exc:
        if exc.code in ("category_archived", "category_transfer"):
            # The category left the plan meanwhile (archived or turned into a transfer in
            # another tab): its row is gone, so the page is stale. Reload it with the message.
            return _redirect(request, target, f"error=plan_{exc.code}", status_code=409)
        message = plan_error(request, exc.code, exc.currency or ctx.workspace.base_currency)
        if is_htmx_fragment_request(request):
            # No focus: the user may already be typing in the next cell.
            cell = CellState(category, amount, message)
            return await run_in_threadpool(
                lambda: _row(request, ctx, target, category, cell=cell, status_code=400)
            )
        cell = CellState(category, amount, message, focus=True)
        context = await run_in_threadpool(
            lambda: month_context(request, ctx, target, plan_cell=cell)
        )
        return render_template(request, "months/page.html", context=context, status_code=400)
    if not is_htmx_fragment_request(request):
        notice = "plan_removed" if saved.outcome == "removed" else "plan_saved"
        path = _month_path(request, target, f"notice={notice}")
        return RedirectResponse(f"{path}#plan-{category}", status_code=303)
    status = gettext(PLAN_STATUS[saved.outcome], request.state.locale)
    return await run_in_threadpool(lambda: _row(request, ctx, target, category, status=status))
