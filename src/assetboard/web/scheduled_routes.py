from __future__ import annotations

from datetime import date
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from assetboard.db.repositories import NotFound
from assetboard.domain.money import Money
from assetboard.domain.recurrence import Frequency, MonthDay, RecurrenceRule, WeekendPolicy
from assetboard.domain.scheduled import AmountMode, ItemAction
from assetboard.domain.transactions import TransactionDirection, amount_minor
from assetboard.i18n import gettext
from assetboard.i18n.numbers import format_amount_input, parse_amount
from assetboard.services import scheduled
from assetboard.services.categories import list_categories
from assetboard.services.context import Context, Role, has_role, require_role
from assetboard.web.context import CurrentContext
from assetboard.web.rendering import render_template
from assetboard.web.security import require_csrf

router = APIRouter(dependencies=[Depends(require_csrf)])


def _id(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise NotFound from None


async def _form_data(request: Request) -> dict[str, str]:
    form = await request.form()
    values = {key: value for key, value in form.items() if isinstance(value, str)}
    values["weekdays"] = ",".join(
        value for value in form.getlist("weekday") if isinstance(value, str)
    )
    return values


def _rule(values: dict[str, str]) -> RecurrenceRule:
    choice = values.get("month_day", "1")
    freq = Frequency(values.get("freq", "monthly"))
    return RecurrenceRule(
        start_date=date.fromisoformat(values.get("start_date", "")),
        end_date=date.fromisoformat(values["end_date"]) if values.get("end_date") else None,
        max_count=int(values["max_count"]) if values.get("max_count") else None,
        freq=freq,
        interval=int(values.get("interval", "1")),
        weekdays=tuple(int(day) for day in values.get("weekdays", "").split(",") if day),
        month_day=int(choice) if choice.lstrip("-").isdigit() else MonthDay(choice),
        month=int(values.get("month", "1")),
        weekend_policy=WeekendPolicy(values.get("weekend_policy", "none")),
    )


def _input(values: dict[str, str], ctx: Context, locale: str) -> scheduled.ScheduledInput:
    return scheduled.ScheduledInput(
        name=values.get("name", ""),
        direction=TransactionDirection(values.get("direction", "expense")),
        category_id=UUID(values.get("category_id", "")),
        amount=Money(
            amount_minor(
                parse_amount(values.get("amount", ""), locale), ctx.workspace.base_currency
            ),
            ctx.workspace.base_currency,
        ),
        rule=_rule(values),
        amount_mode=AmountMode(values.get("amount_mode", "fixed")),
        notes=values.get("notes", ""),
    )


async def _form(
    request: Request,
    ctx: Context,
    values: dict[str, str],
    *,
    item_id: str | None = None,
    error: bool = False,
) -> Response:
    require_role(ctx, Role.EDITOR)
    categories = await run_in_threadpool(list_categories, request.app.state.session_factory, ctx)
    return render_template(
        request,
        "scheduled/form.html",
        context={
            "values": values,
            "item_id": item_id,
            "categories": categories.expense + categories.income,
            "error": error,
            "days": list(range(1, 32)),
            "months": list(range(1, 13)),
            "weekdays": [
                ("0", gettext("Monday", request.state.locale)),
                ("1", gettext("Tuesday", request.state.locale)),
                ("2", gettext("Wednesday", request.state.locale)),
                ("3", gettext("Thursday", request.state.locale)),
                ("4", gettext("Friday", request.state.locale)),
                ("5", gettext("Saturday", request.state.locale)),
                ("6", gettext("Sunday", request.state.locale)),
            ],
        },
        status_code=400 if error else 200,
    )


@router.get("/recurring", name="scheduled_items")
async def items(request: Request, ctx: CurrentContext) -> Response:
    rows = await run_in_threadpool(scheduled.list_items, request.app.state.session_factory, ctx)
    return render_template(
        request,
        "scheduled/list.html",
        context={
            "items": rows,
            "can_edit": has_role(ctx, Role.EDITOR),
        },
    )


@router.get("/recurring/new", name="scheduled_new")
async def new(request: Request, ctx: CurrentContext) -> Response:
    return await _form(
        request,
        ctx,
        {
            "start_date": ctx.clock.today(ctx.workspace.timezone).isoformat(),
            "interval": "1",
            "freq": "monthly",
            "month_day": "1",
            "month": "1",
            "weekend_policy": "none",
            "amount_mode": "fixed",
            "direction": "expense",
        },
    )


@router.get("/recurring/{item_id}/edit", name="scheduled_edit")
async def edit(request: Request, ctx: CurrentContext, item_id: str) -> Response:
    require_role(ctx, Role.EDITOR)
    item = await run_in_threadpool(
        scheduled.get_item, request.app.state.session_factory, ctx, _id(item_id)
    )
    rule = item.rule
    return await _form(
        request,
        ctx,
        {
            "name": item.name,
            "direction": item.direction,
            "category_id": str(item.category_id),
            "amount": format_amount_input(item.amount, request.state.locale),
            "notes": item.notes,
            "amount_mode": item.amount_mode,
            "start_date": rule.start_date.isoformat(),
            "end_date": rule.end_date.isoformat() if rule.end_date else "",
            "max_count": str(rule.max_count) if rule.max_count else "",
            "freq": rule.freq,
            "interval": str(rule.interval),
            "weekdays": ",".join(map(str, rule.weekdays)),
            "month_day": str(rule.month_day),
            "month": str(rule.month),
            "weekend_policy": rule.weekend_policy,
        },
        item_id=item_id,
    )


@router.post("/recurring/preview", name="scheduled_preview")
async def preview(request: Request, ctx: CurrentContext) -> Response:
    values = await _form_data(request)
    try:
        dates = await run_in_threadpool(
            scheduled.preview, request.app.state.session_factory, ctx, _rule(values)
        )
    except ValueError:
        return render_template(
            request, "scheduled/preview.html", context={"dates": [], "invalid": True}
        )
    return render_template(
        request, "scheduled/preview.html", context={"dates": dates, "invalid": False}
    )


async def _save(request: Request, ctx: Context, item_id: str | None) -> Response:
    require_role(ctx, Role.EDITOR)
    values = await _form_data(request)
    try:
        data = _input(values, ctx, request.state.locale)
        await run_in_threadpool(
            lambda: scheduled.save_item(
                request.app.state.session_factory,
                ctx,
                data,
                request.app.state.settings,
                item_id=_id(item_id) if item_id else None,
                include_past=values.get("include_past") == "1",
            )
        )
    except ValueError:
        return await _form(request, ctx, values, item_id=item_id, error=True)
    return RedirectResponse(request.url_for("scheduled_items").path, status_code=303)


@router.post("/recurring", name="scheduled_create")
async def create(request: Request, ctx: CurrentContext) -> Response:
    return await _save(request, ctx, None)


@router.post("/recurring/{item_id}/edit", name="scheduled_update")
async def update(request: Request, ctx: CurrentContext, item_id: str) -> Response:
    return await _save(request, ctx, item_id)


@router.post("/recurring/{item_id}/{action}", name="scheduled_action")
async def action(request: Request, ctx: CurrentContext, item_id: str, action: str) -> Response:
    try:
        operation = ItemAction(action)
    except ValueError:
        raise NotFound from None
    try:
        await run_in_threadpool(
            scheduled.change_state,
            request.app.state.session_factory,
            ctx,
            _id(item_id),
            operation,
            request.app.state.settings,
        )
    except ValueError:
        return RedirectResponse(request.url_for("scheduled_items").path, status_code=303)
    return RedirectResponse(request.url_for("scheduled_items").path, status_code=303)
