"""Transactions on the month page (F2.2 part B): add, edit, delete, undo and autocomplete.

Authorization, validation and the closed-month guard happen in ``services.transactions``;
routes map outcomes to pages. Every state-changing route is covered by the router-level
``require_csrf`` dependency (HTMX sends the token in the ``X-CSRF-Token`` header).

HTMX requests get one table section (``#ledger-expense`` or ``#ledger-income``) back, with the
month summary (``#month-summary``), the screen-reader status line (``#ledger-status``) and, after
a deletion, the undo toast out of band. Opening and cancelling an edit swap a single row. Plain
form posts redirect (303) to the month page, which re-renders the tables with the form's values
and messages when a save is refused, so everything works without JavaScript.

A write refused because the viewed month was closed meanwhile reloads the page with the closed
month message (303, or ``HX-Redirect`` for HTMX): the page in the browser is stale. A full date
in another, closed month is a message on the day field instead, keeping what was typed.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Annotated, Any
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from assetboard.db.repositories import NotFound
from assetboard.domain.months import YearMonth
from assetboard.i18n import gettext
from assetboard.i18n.dates import format_month
from assetboard.services.categories import get_category
from assetboard.services.context import Context
from assetboard.services.months import MonthClosed, get_month
from assetboard.services.transactions import (
    InvalidTransaction,
    TransactionView,
    UndoExpired,
    create_transaction,
    delete_transaction,
    get_transaction,
    suggest_descriptions,
    undo_delete,
    update_transaction,
)
from assetboard.web.context import CurrentContext
from assetboard.web.errors import error_response, is_htmx_fragment_request
from assetboard.web.ledger import (
    DIRECTIONS,
    ENTRY_DAY,
    FALLBACK_ERROR,
    MONTH_CLOSED_ELSEWHERE,
    NOT_SAVED,
    SectionState,
    build_ledger,
    edit_values,
    field_messages,
    first_invalid,
    parse_filter,
)
from assetboard.web.month_routes import _month_path, _parse, month_context
from assetboard.web.plan import build_summary, summary_context
from assetboard.web.rendering import render_template
from assetboard.web.security import require_csrf

router = APIRouter(dependencies=[Depends(require_csrf)])

FormStr = Annotated[str, Form()]
MAX_PREFIX_LENGTH = 255
_LIST_ID = re.compile(r"[a-z0-9-]{1,64}")


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


STATUS = {
    "added": _("Entry added."),
    "saved": _("Entry saved."),
    "deleted": _("Entry deleted."),
    "restored": _("Entry restored."),
}
MOVED = _("Entry saved in %(month)s.")
OUTSIDE_FILTER = _("Entry saved. Its category is not shown while this filter is on.")
UNDO_TOO_LATE_TITLE = _("Too late to undo")
UNDO_TOO_LATE = _("The deletion can no longer be undone: the entry stays deleted.")
NOT_SAVED_TITLE = _("Not saved")


def _t(request: Request, message: str) -> str:
    return gettext(message, request.state.locale)


def _transaction_id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise NotFound from None


def _category_id(value: str) -> UUID | None:
    """The submitted category; ``None`` when none was chosen. A malformed id is refused."""
    if not value:
        return None
    try:
        return UUID(value)
    except ValueError:
        raise InvalidTransaction({"category": "invalid_category"}) from None


def _values(day: str, amount: str, description: str, category_id: str) -> dict[str, str]:
    return {"day": day, "amount": amount, "description": description, "category_id": category_id}


@dataclass(frozen=True, slots=True)
class _FilterRef:
    category_id: UUID
    kind: str


def _resolve_filter(request: Request, ctx: Context, value: str) -> _FilterRef | None:
    """The filter as a category of this workspace (blocking); anything else is no filter."""
    category_id = parse_filter(value)
    if category_id is None:
        return None
    try:
        category = get_category(
            request.app.state.session_factory, ctx, category_id, usage_sources=()
        )
    except NotFound:
        return None
    return _FilterRef(category.id, category.kind)


async def _use_filter(request: Request, ctx: Context, value: str) -> None:
    """Validate and remember the category filter the request's page shows (F2.4: the
    ``filter`` form field or query value), so the answer stays filtered. Only the validated id
    is used afterwards (in answers and in redirects)."""
    raw = value or request.query_params.get("filter", "")
    request.state.ledger_filter = await run_in_threadpool(_resolve_filter, request, ctx, raw)


def _filter(request: Request, direction: str | None = None) -> UUID | None:
    """The validated filter; ``None`` without one or when it is another kind's (a table of
    ``direction`` is never filtered by a category of the other kind)."""
    ref: _FilterRef | None = getattr(request.state, "ledger_filter", None)
    if ref is None or (direction is not None and ref.kind != direction):
        return None
    return ref.category_id


def _path(request: Request, month: YearMonth, query: str = "", direction: str | None = None) -> str:
    """The month page, filtered as the request's page was."""
    chosen = _filter(request, direction)
    if chosen is not None:
        query = f"{query}&category={chosen}" if query else f"category={chosen}"
    return _month_path(request, month, query)


def _redirect(request: Request, month: YearMonth, query: str, *, status_code: int) -> Response:
    """A 303 to the month page; for HTMX, ``HX-Redirect`` (the page itself is stale)."""
    location = _path(request, month, query)
    if is_htmx_fragment_request(request):
        response = Response(status_code=status_code)
        response.headers["HX-Redirect"] = location
        return response
    return RedirectResponse(location, status_code=303)


def _section(
    request: Request,
    ctx: Context,
    month: YearMonth,
    direction: str,
    state: SectionState,
    *,
    status: str | None = None,
    toast_undo: str | None = None,
    toast_seconds: int = 0,
    oob_section: bool = False,
    status_code: int = 200,
) -> Response:
    """One table section with the out-of-band summary, status line and toast (blocking)."""
    factory = request.app.state.session_factory
    view = get_month(factory, ctx, month)
    ledger = build_ledger(
        factory, ctx, request, view, {direction: state}, _filter(request, direction)
    )
    filter_id = ledger.filter.category_id if ledger.filter else None
    context: dict[str, object] = {
        "ledger": ledger,
        "section_view": ledger.section(direction),
        **summary_context(build_summary(factory, ctx, request, view, filter_id=filter_id)),
        "status": _t(request, STATUS[status]) if status else "",
        "toast_message": _t(request, STATUS["deleted"]) if toast_undo else None,
        "toast_undo": toast_undo,
        "toast_seconds": toast_seconds,
        "oob_section": oob_section,
    }
    response = render_template(
        request, "months/_section.html", context=context, status_code=status_code
    )
    response.headers["Vary"] = "HX-Request"
    return response


def _page(
    request: Request,
    ctx: Context,
    month: YearMonth,
    direction: str,
    state: SectionState,
    status_code: int,
) -> Response:
    """The whole month page with a refused form (no JavaScript; blocking)."""
    context = month_context(
        request, ctx, month, states={direction: state}, filter_id=_filter(request, direction)
    )
    return render_template(request, "months/page.html", context=context, status_code=status_code)


def _refused(
    request: Request,
    ctx: Context,
    month: YearMonth,
    direction: str,
    state: SectionState,
    status_code: int,
) -> Response:
    if is_htmx_fragment_request(request):
        return _section(request, ctx, month, direction, state, status_code=status_code)
    return _page(request, ctx, month, direction, state, status_code)


def _closed_elsewhere(request: Request, exc: MonthClosed) -> dict[str, str]:
    month = format_month(exc.month, request.state.locale)
    return {"day": _t(request, MONTH_CLOSED_ELSEWHERE) % {"month": month}}


def _hidden(request: Request, saved: TransactionView, month: YearMonth) -> bool:
    """Whether a saved entry of this month is not shown because of the category filter."""
    chosen = _filter(request, saved.direction)
    return saved.month == month and chosen is not None and saved.category_id != chosen


def _moved(request: Request, saved: TransactionView, month: YearMonth) -> dict[str, Any]:
    """The notice for an entry whose date lies in another month, linking that month, or that
    the filter hides (F2.4)."""
    if _hidden(request, saved, month):
        return {"notice": _t(request, OUTSIDE_FILTER)}
    if saved.month == month:
        return {}
    text = _t(request, MOVED) % {"month": format_month(saved.month, request.state.locale)}
    return {"notice": text, "notice_href": _month_path(request, saved.month)}


@router.post("/m/{month}/transactions", name="transaction_create")
async def transaction_create(
    request: Request,
    ctx: CurrentContext,
    month: str,
    direction: FormStr = "",
    day: FormStr = "",
    amount: FormStr = "",
    description: FormStr = "",
    category_id: FormStr = "",
    filter: FormStr = "",
) -> Response:
    target = _parse(month)
    await _use_filter(request, ctx, filter)
    if direction not in DIRECTIONS:
        return error_response(
            request, 400, _t(request, NOT_SAVED_TITLE), _t(request, FALLBACK_ERROR)
        )
    values = _values(day, amount, description, category_id)
    currency = ctx.workspace.base_currency
    try:
        saved = await run_in_threadpool(
            lambda: create_transaction(
                request.app.state.session_factory,
                ctx,
                month=target,
                direction=direction,
                day=day,
                amount=amount,
                description=description,
                category_id=_category_id(category_id),
                locale=request.state.locale,
            )
        )
    except InvalidTransaction as exc:
        errors = field_messages(request, exc.errors, direction=direction, currency=currency)
        state = SectionState(
            entry_values=values,
            entry_errors=errors,
            error=_t(request, NOT_SAVED),
            focus=f"entry:{first_invalid(errors)}",
        )
        return await run_in_threadpool(_refused, request, ctx, target, direction, state, 400)
    except MonthClosed as exc:
        if exc.month == target:
            return _redirect(request, target, "error=month_closed", status_code=409)
        errors = _closed_elsewhere(request, exc)
        state = SectionState(
            entry_values=values,
            entry_errors=errors,
            error=_t(request, NOT_SAVED),
            focus="entry:day",
        )
        return await run_in_threadpool(_refused, request, ctx, target, direction, state, 409)
    # The next entry row keeps the day (owner, 2026-10-18): as a day number when the entry is
    # in the viewed month, else as typed; app.js selects it so typing replaces it.
    kept_day = str(saved.date.day) if saved.month == target else day.strip()
    if not ENTRY_DAY.fullmatch(kept_day):
        kept_day = ""
    if not is_htmx_fragment_request(request):
        query = "notice=transaction_added"
        if _hidden(request, saved, target):
            query = "notice=outside_filter"
        if saved.month != target:
            query = f"saved_in={saved.month}"
        if kept_day:
            query += "&" + urlencode({"entry": direction, "day": kept_day})
        path = _path(request, target, query, direction)
        return RedirectResponse(f"{path}#ledger-{direction}", status_code=303)
    state = SectionState(
        entry_values={"day": kept_day}, focus="entry:day", **_moved(request, saved, target)
    )
    return await run_in_threadpool(
        lambda: _section(request, ctx, target, direction, state, status="added")
    )


@router.get("/m/{month}/transactions/{transaction_id}/edit", name="transaction_edit")
async def transaction_edit(
    request: Request, ctx: CurrentContext, month: str, transaction_id: str, filter: str = ""
) -> Response:
    target = _parse(month)
    row_id = _transaction_id(transaction_id)
    await _use_filter(request, ctx, filter)
    if not is_htmx_fragment_request(request):
        path = _path(request, target, f"edit={row_id}")
        return RedirectResponse(f"{path}#tx-{row_id}", status_code=303)
    return await run_in_threadpool(_row, request, ctx, target, row_id, True)


@router.get("/m/{month}/transactions/{transaction_id}/row", name="transaction_row")
async def transaction_row(
    request: Request, ctx: CurrentContext, month: str, transaction_id: str, filter: str = ""
) -> Response:
    target = _parse(month)
    row_id = _transaction_id(transaction_id)
    await _use_filter(request, ctx, filter)
    if not is_htmx_fragment_request(request):
        return RedirectResponse(_path(request, target), status_code=303)
    return await run_in_threadpool(_row, request, ctx, target, row_id, False)


def _row(request: Request, ctx: Context, month: YearMonth, row_id: UUID, editing: bool) -> Response:
    """One row of the viewed month, as an edit row or a display row (blocking)."""
    factory = request.app.state.session_factory
    row = get_transaction(factory, ctx, row_id)
    if row.month != month:
        raise NotFound
    if editing:
        values = edit_values(row, month, request.state.locale)
        state = SectionState(editing=row.id, edit_values=values, focus="edit:day")
    else:
        state = SectionState(focus=f"row:{row.id}")
    view = get_month(factory, ctx, month)
    states = {row.direction: state}
    ledger = build_ledger(factory, ctx, request, view, states, _filter(request, row.direction))
    section = ledger.section(row.direction)
    row_view = next((r for r in section.rows if r.t.id == row.id), None)
    if row_view is None:  # a row outside the filter: shown unfiltered (no error)
        ledger = build_ledger(factory, ctx, request, view, states)
        section = ledger.section(row.direction)
        row_view = next(r for r in section.rows if r.t.id == row.id)
    context: dict[str, object] = {
        "ledger": ledger,
        "section_view": section,
        "row_view": row_view,
    }
    response = render_template(request, "months/_row.html", context=context)
    response.headers["Vary"] = "HX-Request"
    return response


@router.post("/m/{month}/transactions/{transaction_id}", name="transaction_update")
async def transaction_update(
    request: Request,
    ctx: CurrentContext,
    month: str,
    transaction_id: str,
    day: FormStr = "",
    amount: FormStr = "",
    description: FormStr = "",
    category_id: FormStr = "",
    filter: FormStr = "",
) -> Response:
    target = _parse(month)
    row_id = _transaction_id(transaction_id)
    await _use_filter(request, ctx, filter)
    values = _values(day, amount, description, category_id)
    factory = request.app.state.session_factory
    try:
        saved = await run_in_threadpool(
            lambda: update_transaction(
                factory,
                ctx,
                row_id,
                month=target,
                day=day,
                amount=amount,
                description=description,
                category_id=_category_id(category_id),
                locale=request.state.locale,
            )
        )
    except NotFound:
        return _redirect(request, target, "error=transaction_gone", status_code=404)
    except (InvalidTransaction, MonthClosed) as exc:
        if isinstance(exc, MonthClosed) and exc.month == target:
            return _redirect(request, target, "error=month_closed", status_code=409)
        try:
            stored = await run_in_threadpool(get_transaction, factory, ctx, row_id)
        except NotFound:
            return _redirect(request, target, "error=transaction_gone", status_code=404)
        if isinstance(exc, MonthClosed):
            errors, status_code = _closed_elsewhere(request, exc), 409
        else:
            currency = stored.amount.currency
            errors = field_messages(
                request, exc.errors, direction=stored.direction, currency=currency
            )
            status_code = 400
        state = SectionState(
            editing=row_id,
            edit_values=values,
            edit_errors=errors,
            error=_t(request, NOT_SAVED),
            focus=f"edit:{first_invalid(errors)}",
        )
        return await run_in_threadpool(
            _refused, request, ctx, target, stored.direction, state, status_code
        )
    if not is_htmx_fragment_request(request):
        query = "notice=transaction_saved" if saved.month == target else f"saved_in={saved.month}"
        if _hidden(request, saved, target):
            query = "notice=outside_filter"
        path = _path(request, target, query, saved.direction)
        shown = saved.month == target and not _hidden(request, saved, target)
        anchor = f"tx-{saved.id}" if shown else f"ledger-{saved.direction}"
        return RedirectResponse(f"{path}#{anchor}", status_code=303)
    shown = saved.month == target and not _hidden(request, saved, target)
    focus = f"row:{saved.id}" if shown else "entry:day"
    state = SectionState(focus=focus, **_moved(request, saved, target))
    return await run_in_threadpool(
        lambda: _section(request, ctx, target, saved.direction, state, status="saved")
    )


@router.post("/m/{month}/transactions/{transaction_id}/delete", name="transaction_delete")
async def transaction_delete(
    request: Request, ctx: CurrentContext, month: str, transaction_id: str, filter: FormStr = ""
) -> Response:
    target = _parse(month)
    await _use_filter(request, ctx, filter)
    row_id = _transaction_id(transaction_id)
    factory = request.app.state.session_factory
    try:
        row = await run_in_threadpool(get_transaction, factory, ctx, row_id)
        deleted = await run_in_threadpool(delete_transaction, factory, ctx, row_id)
    except NotFound:
        return _redirect(request, target, "error=transaction_gone", status_code=404)
    except MonthClosed:
        return _redirect(request, target, "error=month_closed", status_code=409)
    if not is_htmx_fragment_request(request):
        path = _path(request, target, f"deleted={row_id}", row.direction)
        return RedirectResponse(f"{path}#ledger-{row.direction}", status_code=303)
    undo = request.url_for("transaction_undo", month=str(target), transaction_id=str(row_id))
    kept = _filter(request, row.direction)
    undo_path = undo.path + (f"?filter={kept}" if kept else "")
    # The toast counts down the time the server still allows, not a fixed ten seconds.
    left = (deleted.undo_until - ctx.clock.now()).total_seconds()
    seconds = max(0, math.ceil(left))
    state = SectionState(focus="entry:day")
    return await run_in_threadpool(
        lambda: _section(
            request,
            ctx,
            target,
            row.direction,
            state,
            status="deleted",
            toast_undo=undo_path,
            toast_seconds=seconds,
        )
    )


@router.post("/m/{month}/transactions/{transaction_id}/undo", name="transaction_undo")
async def transaction_undo(
    request: Request, ctx: CurrentContext, month: str, transaction_id: str, filter: FormStr = ""
) -> Response:
    target = _parse(month)
    await _use_filter(request, ctx, filter)
    row_id = _transaction_id(transaction_id)
    try:
        restored = await run_in_threadpool(
            undo_delete, request.app.state.session_factory, ctx, row_id
        )
    except NotFound:
        return _redirect(request, target, "error=transaction_gone", status_code=404)
    except MonthClosed:
        return _redirect(request, target, "error=month_closed", status_code=409)
    except UndoExpired:
        if is_htmx_fragment_request(request):
            return error_response(
                request, 409, _t(request, UNDO_TOO_LATE_TITLE), _t(request, UNDO_TOO_LATE)
            )
        return RedirectResponse(_path(request, target, "error=undo_expired"), status_code=303)
    if not is_htmx_fragment_request(request):
        path = _path(request, target, "notice=transaction_restored", restored.direction)
        return RedirectResponse(f"{path}#tx-{restored.id}", status_code=303)
    # The toast (the request's target) is replaced by nothing; the section comes out of band.
    focus = f"row:{restored.id}" if restored.month == target else "entry:day"
    state = SectionState(focus=focus)
    return await run_in_threadpool(
        lambda: _section(
            request, ctx, target, restored.direction, state, status="restored", oob_section=True
        )
    )


@router.get("/transactions/suggestions", name="transaction_suggestions")
async def transaction_suggestions(
    request: Request, ctx: CurrentContext, direction: str = "", description: str = ""
) -> Response:
    """Earlier descriptions starting with ``description``, as ``role="option"`` items."""
    if direction not in DIRECTIONS:
        return Response(status_code=400)
    if len(description) > MAX_PREFIX_LENGTH:  # longer than any stored description
        description = ""
    suggestions = await run_in_threadpool(
        lambda: suggest_descriptions(
            request.app.state.session_factory, ctx, direction=direction, prefix=description
        )
    )
    target = request.headers.get("hx-target", "")
    list_id = target if _LIST_ID.fullmatch(target) else "suggestions"
    context: dict[str, object] = {"suggestions": suggestions, "list_id": list_id}
    response = render_template(request, "months/_suggestions.html", context=context)
    response.headers["Cache-Control"] = "no-store"
    return response
