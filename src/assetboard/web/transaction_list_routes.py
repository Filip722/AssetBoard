"""The transaction list (F2.6): ``/transactions`` with filters, sorting, pages, CSV export and
bulk recategorize.

Everything is a plain form, so the page works without JavaScript: the filters are a GET form
(the URL holds them, so a result can be bookmarked or shared), the export is a GET of the same
filters plus the CSV options, and a bulk change is a POST to a preview page that asks for
confirmation, then a POST that applies it and redirects (303) back to the list. The selected
rows are per-row checkboxes joined to the bulk form with the ``form`` attribute.

Validation, authorization (viewers read and export; editors and owners change categories) and
the closed-month guard are in ``services.transaction_search``. The state-changing routes are
covered by the router-level ``require_csrf`` dependency; the export changes nothing (owner
decision 2026-10-19: not audited), so it is a GET.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator, Mapping
from datetime import UTC
from tempfile import SpooledTemporaryFile
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import ImmutableMultiDict
from starlette.responses import RedirectResponse, Response, StreamingResponse
from starlette.types import Receive, Scope, Send

from assetboard import branding
from assetboard.domain.ledger_csv import (
    FORMULA_GUARD_DEFAULT,
    CsvOptionError,
    CsvOptions,
)
from assetboard.domain.transaction_search import (
    DEFAULT_SORT,
    MAX_SEARCH_TEXT_LENGTH,
    SortOrder,
    TransactionFilter,
    parse_sort,
)
from assetboard.i18n import gettext, ngettext
from assetboard.i18n.dates import format_day, format_month
from assetboard.i18n.numbers import format_money
from assetboard.services import transaction_search as search_service
from assetboard.services.context import Context, Role, has_role
from assetboard.services.transaction_search import (
    MAX_SELECTED_IDS,
    CategoryOption,
    FilterInput,
    InvalidFilter,
    InvalidTarget,
    MonthsClosed,
    RecategorizeError,
    RecategorizePlan,
    SearchResult,
    Selection,
    category_options,
    export_transactions_csv,
    parse_filter,
    preview_recategorize,
    recategorize_transactions,
    search_transactions,
)
from assetboard.web.context import CurrentContext
from assetboard.web.rendering import render_template, scoped_url
from assetboard.web.security import require_csrf

router = APIRouter(dependencies=[Depends(require_csrf)])


class SpoolResponse(StreamingResponse):
    """Streams a spooled export file and closes it however the response ends: after the last
    chunk, or when sending fails (the client went away mid-download)."""

    def __init__(self, spool: SpooledTemporaryFile[bytes], **kwargs: Any) -> None:
        def chunks() -> Iterator[bytes]:
            while data := spool.read(EXPORT_CHUNK_BYTES):
                yield data

        super().__init__(chunks(), **kwargs)
        self.spool = spool

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.spool.close()


# The export is written to a spooled file first (one consistent read), then streamed.
EXPORT_SPOOL_BYTES = 8 * 1024 * 1024
EXPORT_CHUNK_BYTES = 64 * 1024
# Query and form field names of the filter (short, readable URLs).
PARAMS = {
    "date_from": "from",
    "date_to": "to",
    "direction": "direction",
    "categories": "category",
    "text": "q",
    "amount_min": "min",
    "amount_max": "max",
}


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


FILTER_ERRORS = {
    "invalid_date": _("Enter a date such as 2026-10-07 or 07.10.2026."),
    "day_not_in_month": _("That month has no such day."),
    "date_out_of_range": _("Enter a date between the years 1900 and 2199."),
    "date_range_reversed": _("The end date is before the start date."),
    "invalid_direction": _("Choose expenses, income or both."),
    "invalid_category": _("Choose one of the listed categories."),
    "text_too_long": _("Use at most %(max)d characters."),
    "invalid_amount": _("Enter a valid amount."),
    "amount_negative": _("The amount cannot be negative."),
    "amount_too_large": _("The amount is too large."),
    "too_many_decimals": _("Too many decimal places for %(currency)s."),
    "amount_range_reversed": _("The largest amount is below the smallest."),
}
OPTION_ERRORS = {
    "invalid_delimiter": _("Choose one of the listed options."),
    "invalid_decimal": _("Choose one of the listed options."),
    "decimal_comma_needs_semicolon": _(
        "A decimal comma needs the semicolon delimiter, so that amounts stay one column."
    ),
}
BULK_ERRORS = {
    "nothing_selected": _("Select at least one entry."),
    "too_many_rows": _(
        "Too many entries for one change (at most %(max)d). Narrow the filters first."
    ),
    "too_many_ticked": _(
        "Too many ticked entries (at most %(max)d at a time). Tick fewer, or change all "
        "matching entries."
    ),
    "invalid_target": _("Choose an active category."),
    "kind_mismatch": _(
        "Choose a category of the same kind as the entries. To change expenses and income, "
        "filter by one of them first."
    ),
    "selection_changed": _(
        "The entries changed after you reviewed the change. Nothing was changed; review it again."
    ),
    "months_closed": _(
        "Nothing was changed: some entries are in closed months (%(months)s). An owner can "
        "reopen them."
    ),
}
FILTER_INVALID = _("Correct the marked filters to see the entries.")
SORT_LABELS = {
    SortOrder.DATE_DESC: _("Newest first"),
    SortOrder.DATE_ASC: _("Oldest first"),
    SortOrder.AMOUNT_DESC: _("Largest amount first"),
    SortOrder.AMOUNT_ASC: _("Smallest amount first"),
}
DIRECTION_LABELS = {"expense": _("Expense"), "income": _("Income")}


# Query parameters and form data alike.
_Params = ImmutableMultiDict[str, Any]


def _t(request: Request, message: str) -> str:
    return gettext(message, request.state.locale)


def _text(params: _Params, name: str, limit: int = 64) -> str:
    value = params.get(name, "")
    return value[:limit] if isinstance(value, str) else ""


def _filter_input(params: _Params) -> FilterInput:
    # The text keeps one character over the limit, so a too long text is reported, not cut.
    return FilterInput(
        date_from=_text(params, PARAMS["date_from"]),
        date_to=_text(params, PARAMS["date_to"]),
        direction=_text(params, PARAMS["direction"]),
        categories=tuple(
            value[:64]
            for value in params.getlist(PARAMS["categories"])[:500]
            if isinstance(value, str)
        ),  # fmt: skip
        text=_text(params, PARAMS["text"], MAX_SEARCH_TEXT_LENGTH + 1),
        amount_min=_text(params, PARAMS["amount_min"]),
        amount_max=_text(params, PARAMS["amount_max"]),
    )


def _page_number(params: _Params) -> int:
    value = _text(params, "page", 9)
    return int(value) if value.isascii() and value.isdigit() else 1


def _state_pairs(raw: FilterInput, sort: SortOrder, page: int = 1) -> list[tuple[str, str]]:
    """The filter, sort and page as (name, value) pairs: for URLs and hidden form fields."""
    pairs: list[tuple[str, str]] = []
    for field, name in PARAMS.items():
        value = getattr(raw, field)
        if field == "categories":
            pairs += [(name, item) for item in value if item]
        elif value:
            pairs.append((name, value))
    if sort is not DEFAULT_SORT:
        pairs.append(("sort", sort.value))
    if page > 1:
        pairs.append(("page", str(page)))
    return pairs


def _list_url(request: Request, pairs: list[tuple[str, str]]) -> str:
    path = scoped_url(request, "transactions")
    return f"{path}?{urlencode(pairs)}" if pairs else path


def _filter_messages(request: Request, ctx: Context, errors: Mapping[str, str]) -> dict[str, str]:
    messages: dict[str, str] = {}
    for field, code in errors.items():
        text = _t(request, FILTER_ERRORS.get(code, FILTER_ERRORS["invalid_amount"]))
        if code == "text_too_long":
            text %= {"max": MAX_SEARCH_TEXT_LENGTH}
        elif code == "too_many_decimals":
            text %= {"currency": ctx.workspace.base_currency}
        messages[field] = text
    return messages


def _bulk_message(request: Request, error: RecategorizeError) -> str:
    text = _t(request, BULK_ERRORS[error.code])
    if isinstance(error, MonthsClosed):
        months = ", ".join(format_month(m, request.state.locale) for m in error.months)
        return text % {"months": months}
    if error.code == "too_many_rows":
        return text % {"max": search_service.MAX_RECATEGORIZE_ROWS}
    if error.code == "too_many_ticked":
        return text % {"max": MAX_SELECTED_IDS}
    return text


def _grouped(options: tuple[CategoryOption, ...]) -> dict[str, list[CategoryOption]]:
    return {
        "expense": [c for c in options if c.kind == "expense" and not c.archived],
        "income": [c for c in options if c.kind == "income" and not c.archived],
        "archived": [c for c in options if c.archived],
    }


def _row_view(request: Request, row: Any) -> dict[str, Any]:
    locale = request.state.locale
    return {
        "t": row,
        "amount": format_money(row.amount, locale),
        "full_date": format_day(row.date, locale),
        "month_href": scoped_url(request, "month", month=str(row.month)),
        "direction": _t(request, DIRECTION_LABELS[row.direction]),
    }


async def _list_page(
    request: Request,
    ctx: Context,
    raw: FilterInput,
    sort: SortOrder,
    page: int,
    *,
    notice: str = "",
    bulk_error: str = "",
    bulk_values: Mapping[str, Any] | None = None,
    export_values: Mapping[str, Any] | None = None,
    export_error: str = "",
    status_code: int = 200,
) -> Response:
    factory = request.app.state.session_factory
    errors: dict[str, str] = {}
    where: TransactionFilter | None = None
    try:
        where = parse_filter(raw, locale=request.state.locale, currency=ctx.workspace.base_currency)
    except InvalidFilter as exc:
        errors = _filter_messages(request, ctx, exc.errors)
    result: SearchResult | None = None
    if where is not None:
        result = await run_in_threadpool(
            search_transactions, factory, ctx, where, sort=sort, page=page
        )
        options = result.categories
    else:
        options = await run_in_threadpool(category_options, factory, ctx)
    pairs = _state_pairs(raw, sort)
    window = result.window if result is not None else None
    pages: dict[str, str | None] = {"previous": None, "next": None}
    if window is not None:
        if window.has_previous:
            pages["previous"] = _list_url(request, _state_pairs(raw, sort, window.number - 1))
        if window.has_next:
            pages["next"] = _list_url(request, _state_pairs(raw, sort, window.number + 1))
    current = _state_pairs(raw, sort, window.number if window else 1)
    context: dict[str, Any] = {
        "raw": raw,
        "selected_categories": set(raw.categories),
        "errors": errors,
        "filter_invalid": _t(request, FILTER_INVALID) if errors else "",
        "result": result,
        "rows": [_row_view(request, row) for row in result.rows] if result else [],
        "window": window,
        "pages": pages,
        "sort": sort,
        "sort_options": [(s.value, _t(request, label)) for s, label in SORT_LABELS.items()],
        "categories": _grouped(options),
        "can_edit": has_role(ctx, Role.EDITOR),
        "state_fields": current,
        "filter_fields": pairs,
        "list_url": _list_url(request, current),
        "notice": notice,
        "bulk_error": bulk_error,
        "bulk_values": bulk_values or {},
        "export_values": export_values or {"guard": FORMULA_GUARD_DEFAULT},
        "export_error": export_error,
        "max_text": MAX_SEARCH_TEXT_LENGTH,
        # Read at request time: the cap is a module constant tests may lower.
        "too_many_for_bulk": (
            _t(request, BULK_ERRORS["too_many_rows"])
            % {"max": search_service.MAX_RECATEGORIZE_ROWS}
            if window is not None and window.total > search_service.MAX_RECATEGORIZE_ROWS
            else ""
        ),
    }
    return render_template(
        request, "transactions/list.html", context=context, status_code=status_code
    )


@router.get("/transactions", name="transactions")
async def transactions_page(request: Request, ctx: CurrentContext) -> Response:
    params = request.query_params
    raw, sort, page = _filter_input(params), parse_sort(_text(params, "sort")), _page_number(params)
    notice = ""
    done = _text(params, "changed", 9)
    if done.isascii() and done.isdigit():
        count = int(done)
        notice = ngettext(
            "Category changed for %(num)d entry.",
            "Category changed for %(num)d entries.",
            count,
            request.state.locale,
        ) % {"num": count}
    return await _list_page(request, ctx, raw, sort, page, notice=notice)


# Export


@router.get("/transactions/export.csv", name="transactions_export")
async def transactions_export(request: Request, ctx: CurrentContext) -> Response:
    params = request.query_params
    raw, sort, page = _filter_input(params), parse_sort(_text(params, "sort")), _page_number(params)
    values = {
        "delimiter": _text(params, "delimiter") or "comma",
        "decimal": _text(params, "decimal") or "dot",
        "bom": _text(params, "bom") == "1",
        "guard": _text(params, "guard") == "1",
    }
    try:
        options = CsvOptions(
            delimiter=values["delimiter"],
            decimal=values["decimal"],
            bom=values["bom"],
            formula_guard=values["guard"],
        )
        where = parse_filter(raw, locale=request.state.locale, currency=ctx.workspace.base_currency)
    except (CsvOptionError, InvalidFilter) as exc:
        export_error = (
            _t(request, OPTION_ERRORS[exc.code]) if isinstance(exc, CsvOptionError) else ""
        )
        return await _list_page(
            request, ctx, raw, sort, page, export_values=values, export_error=export_error,
            status_code=400,
        )  # fmt: skip
    spool = SpooledTemporaryFile(max_size=EXPORT_SPOOL_BYTES)  # noqa: SIM115 - closed below
    try:
        await run_in_threadpool(
            export_transactions_csv, request.app.state.session_factory, ctx, where, options, spool
        )
    except BaseException:
        spool.close()
        raise
    spool.seek(0)
    stamp = ctx.clock.now().astimezone(UTC).strftime("%Y%m%d-%H%M%S")
    filename = f"{branding.CLI_NAME}-transactions-{stamp}Z.csv"
    return SpoolResponse(
        spool,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )


# Bulk recategorize


class _BulkInputError(Exception):
    def __init__(self, error: RecategorizeError | None = None) -> None:
        super().__init__()
        self.error = error


def _selection(form: _Params, raw: FilterInput, request: Request, ctx: Context) -> Selection:
    scope = _text(form, "scope")
    if scope == "filtered":
        where = parse_filter(  # InvalidFilter: shown on the list
            raw, locale=request.state.locale, currency=ctx.workspace.base_currency
        )
        return Selection(where=where)
    ids: set[UUID] = set()
    for value in form.getlist("ids")[:1000]:
        with contextlib.suppress(ValueError, TypeError, AttributeError):
            ids.add(UUID(value))
    return Selection(ids=frozenset(ids))


def _target(form: _Params) -> UUID:
    try:
        return UUID(_text(form, "category_id"))
    except ValueError:
        raise _BulkInputError(InvalidTarget()) from None


def _bulk_values(form: _Params) -> dict[str, Any]:
    return {
        "category_id": _text(form, "category_id"),
        "ids": {value for value in form.getlist("ids")[:1000] if isinstance(value, str)},
    }


async def _bulk_refused(
    request: Request,
    ctx: Context,
    form: _Params,
    raw: FilterInput,
    sort: SortOrder,
    page: int,
    error: RecategorizeError | None,
) -> Response:
    message = _bulk_message(request, error) if error is not None else ""
    status = (
        409 if error is not None and error.code in {"months_closed", "selection_changed"} else 400
    )
    return await _list_page(
        request, ctx, raw, sort, page, bulk_error=message, bulk_values=_bulk_values(form),
        status_code=status,
    )  # fmt: skip


@router.post("/transactions/recategorize", name="transactions_recategorize_preview")
async def transactions_recategorize_preview(request: Request, ctx: CurrentContext) -> Response:
    form = await request.form()
    raw, sort, page = _filter_input(form), parse_sort(_text(form, "sort")), _page_number(form)
    try:
        target = _target(form)
        selection = _selection(form, raw, request, ctx)
        plan = await run_in_threadpool(
            preview_recategorize, request.app.state.session_factory, ctx, selection, target
        )
    except InvalidFilter:
        return await _bulk_refused(request, ctx, form, raw, sort, page, None)
    except _BulkInputError as exc:
        return await _bulk_refused(request, ctx, form, raw, sort, page, exc.error)
    except RecategorizeError as exc:
        return await _bulk_refused(request, ctx, form, raw, sort, page, exc)
    return _confirm_page(request, raw, sort, page, selection, plan)


def _confirm_page(
    request: Request,
    raw: FilterInput,
    sort: SortOrder,
    page: int,
    selection: Selection,
    plan: RecategorizePlan,
) -> Response:
    locale = request.state.locale
    pairs = _state_pairs(raw, sort, page)
    hidden = [*pairs, ("category_id", str(plan.target.id)), ("fingerprint", plan.fingerprint)]
    if selection.ids is not None:
        hidden.append(("scope", "selected"))
        hidden += [("ids", str(row_id)) for row_id in sorted(selection.ids)]
    else:
        hidden.append(("scope", "filtered"))
    context = {
        "plan": plan,
        "months": ", ".join(format_month(m, locale) for m in plan.months),
        "from_categories": ", ".join(plan.from_categories),
        "unchanged": plan.selected - plan.changing,
        "hidden_fields": hidden,
        "back_url": _list_url(request, pairs),
        "target_kind": _t(request, DIRECTION_LABELS[plan.target.kind]),
    }
    return render_template(request, "transactions/recategorize.html", context=context)


@router.post("/transactions/recategorize/confirm", name="transactions_recategorize")
async def transactions_recategorize(request: Request, ctx: CurrentContext) -> Response:
    form = await request.form()
    raw, sort, page = _filter_input(form), parse_sort(_text(form, "sort")), _page_number(form)
    try:
        target = _target(form)
        selection = _selection(form, raw, request, ctx)
        plan = await run_in_threadpool(
            lambda: recategorize_transactions(
                request.app.state.session_factory, ctx, selection, target,
                expected=_text(form, "fingerprint"),
            )
        )  # fmt: skip
    except InvalidFilter:
        return await _bulk_refused(request, ctx, form, raw, sort, page, None)
    except _BulkInputError as exc:
        return await _bulk_refused(request, ctx, form, raw, sort, page, exc.error)
    except RecategorizeError as exc:
        return await _bulk_refused(request, ctx, form, raw, sort, page, exc)
    pairs = [*_state_pairs(raw, sort, page), ("changed", str(plan.changing))]
    return RedirectResponse(_list_url(request, pairs), status_code=303)
