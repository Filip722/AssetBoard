"""Categories page (F2.1): list, inline edit, reorder, archive, restore, delete and seed.

Authorization happens in ``services.categories`` (viewers read, editors and owners write);
routes map outcomes to pages. Every state-changing route is covered by the router-level
``require_csrf`` dependency. HTMX requests get the ``#categories-body`` fragment back (or one
row for inline edit and cancel); plain form posts redirect (303) to the page, so the page works
without JavaScript.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from assetboard.db.repositories import NotFound
from assetboard.domain.categories import MAX_CATEGORY_NAME_LENGTH, Kind, Treatment
from assetboard.i18n import gettext
from assetboard.services.categories import (
    CategoryError,
    CategoryList,
    CategoryView,
    archive_category,
    create_category,
    delete_category,
    get_category,
    list_categories,
    move_category,
    restore_category,
    seed_categories,
    update_category,
)
from assetboard.services.context import Context, Role, has_role
from assetboard.web.context import CurrentContext
from assetboard.web.errors import is_htmx_fragment_request
from assetboard.web.rendering import render_template
from assetboard.web.security import require_csrf

router = APIRouter(dependencies=[Depends(require_csrf)])

FormStr = Annotated[str, Form()]
DEFAULT_COLOR = "#64748b"


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


KIND_LABELS = {Kind.EXPENSE.value: _("Expense"), Kind.INCOME.value: _("Income")}
TREATMENT_LABELS = {
    Treatment.REGULAR.value: _("Regular"),
    Treatment.SAVINGS.value: _("Savings"),
    Treatment.TRANSFER.value: _("Transfer"),
}
ERRORS = {
    "invalid_name": _("Enter a name of 1 to 80 characters, without control characters."),
    "duplicate_name": _("Another active category of this kind already has this name."),
    "invalid_kind": _("Choose expense or income."),
    "invalid_treatment": _("Choose a treatment."),
    "invalid_color": _("Choose a colour."),
    "kind_in_use": _("The kind cannot change: this category is in use."),
    "archived": _("Restore the category before changing it."),
    "already_seeded": _("The suggested categories can only be added to an empty list."),
    "invalid_direction": _("The change could not be made. Reload the page and try again."),
}
FALLBACK_ERROR = _("The change could not be made. Reload the page and try again.")
NOTICES = {
    "created": _("Category added."),
    "saved": _("Category saved."),
    "archived": _("Category archived."),
    "archived_in_use": _("Archived because it is in use."),
    "restored": _("Category restored."),
    "deleted": _("Category deleted."),
    "seeded": _("Suggested categories added."),
}


def _t(request: Request, message: str) -> str:
    return gettext(message, request.state.locale)


def _parse_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise NotFound from None


def _labels(request: Request) -> dict[str, object]:
    return {
        "kind_labels": {code: _t(request, label) for code, label in KIND_LABELS.items()},
        "treatment_labels": {code: _t(request, label) for code, label in TREATMENT_LABELS.items()},
    }


def _values(category: CategoryView | None = None) -> dict[str, str]:
    if category is None:
        return {
            "name": "",
            "kind": Kind.EXPENSE.value,
            "treatment": Treatment.REGULAR.value,
            "color": DEFAULT_COLOR,
        }
    return {
        "name": category.name,
        "kind": category.kind,
        "treatment": category.treatment,
        "color": category.color,
    }


def _body_context(
    request: Request,
    ctx: Context,
    categories: CategoryList,
    *,
    notice: str | None = None,
    error: str | None = None,
    create_values: dict[str, str] | None = None,
    create_errors: dict[str, str] | None = None,
    editing: UUID | None = None,
    edit_values: dict[str, str] | None = None,
    edit_errors: dict[str, str] | None = None,
) -> dict[str, object]:
    return {
        "categories": categories,
        "can_edit": has_role(ctx, Role.EDITOR),
        "notice": _t(request, NOTICES[notice]) if notice in NOTICES else None,
        "error": _t(request, error) if error else None,
        "create_values": create_values or _values(),
        "create_errors": {f: _t(request, m) for f, m in (create_errors or {}).items()},
        "editing": editing,
        "edit_values": edit_values,
        "edit_errors": {f: _t(request, m) for f, m in (edit_errors or {}).items()},
        "max_name_length": MAX_CATEGORY_NAME_LENGTH,
        **_labels(request),
    }


def _render(request: Request, ctx: Context, status_code: int = 200, **kwargs: Any) -> Response:
    """The whole page, or only ``#categories-body`` for an HTMX request."""
    categories = list_categories(request.app.state.session_factory, ctx)
    context = _body_context(request, ctx, categories, **kwargs)
    template = (
        "categories/_body.html" if is_htmx_fragment_request(request) else "categories/list.html"
    )
    response = render_template(request, template, context=context, status_code=status_code)
    response.headers["Vary"] = "HX-Request"
    return response


def _done(request: Request, ctx: Context, notice: str) -> Response:
    if is_htmx_fragment_request(request):
        return _render(request, ctx, notice=notice)
    return RedirectResponse(
        request.url_for("categories").path + f"?notice={notice}", status_code=303
    )


def _failed(request: Request, ctx: Context, exc: CategoryError, status_code: int = 409) -> Response:
    return _render(
        request, ctx, status_code=status_code, error=ERRORS.get(exc.code, FALLBACK_ERROR)
    )


@router.get("/categories", name="categories")
async def categories_page(
    request: Request, ctx: CurrentContext, notice: str = "", edit: str = ""
) -> Response:
    editing: UUID | None = None
    values: dict[str, str] | None = None
    if edit and has_role(ctx, Role.EDITOR):
        editing = _parse_uuid(edit)
        category = await run_in_threadpool(
            get_category, request.app.state.session_factory, ctx, editing
        )
        if category.archived:  # archived categories are restored, not edited
            editing = None
        else:
            values = _values(category)
    return await run_in_threadpool(
        lambda: _render(request, ctx, notice=notice or None, editing=editing, edit_values=values)
    )


@router.post("/categories/seed", name="categories_seed")
async def categories_seed(request: Request, ctx: CurrentContext) -> Response:
    locale = request.state.locale
    try:
        await run_in_threadpool(
            lambda: seed_categories(request.app.state.session_factory, ctx, locale=locale)
        )
    except CategoryError as exc:
        return await run_in_threadpool(_failed, request, ctx, exc)
    return await run_in_threadpool(_done, request, ctx, "seeded")


@router.post("/categories", name="categories_create")
async def category_create(
    request: Request,
    ctx: CurrentContext,
    name: FormStr = "",
    kind: FormStr = "",
    treatment: FormStr = "",
    color: FormStr = "",
) -> Response:
    values = {"name": name, "kind": kind, "treatment": treatment, "color": color}
    factory = request.app.state.session_factory
    try:
        await run_in_threadpool(
            lambda: create_category(
                factory, ctx, name=name, kind=kind, treatment=treatment, color=color
            )
        )
    except CategoryError as exc:
        errors = {exc.field or "name": ERRORS.get(exc.code, FALLBACK_ERROR)}
        return await run_in_threadpool(
            lambda: _render(
                request, ctx, status_code=400, create_values=values, create_errors=errors
            )
        )
    return await run_in_threadpool(_done, request, ctx, "created")


def _row(request: Request, ctx: Context, category: CategoryView, *, editing: bool) -> Response:
    categories = list_categories(request.app.state.session_factory, ctx)
    siblings = [c.id for c in categories.expense + categories.income if c.kind == category.kind]
    position = siblings.index(category.id) if category.id in siblings else -1
    context: dict[str, object] = {
        "category": category,
        "first": position == 0,
        "last": position == len(siblings) - 1,
        "can_edit": has_role(ctx, Role.EDITOR),
        "editing": category.id if editing else None,
        "edit_values": _values(category),
        "edit_errors": {},
        "max_name_length": MAX_CATEGORY_NAME_LENGTH,
        **_labels(request),
    }
    return render_template(request, "categories/_row.html", context=context)


@router.get("/categories/{category_id}/edit", name="category_edit")
async def category_edit(request: Request, ctx: CurrentContext, category_id: str) -> Response:
    target = _parse_uuid(category_id)
    if not is_htmx_fragment_request(request):
        path = request.url_for("categories").path
        return RedirectResponse(f"{path}?edit={target}", status_code=303)
    category = await run_in_threadpool(get_category, request.app.state.session_factory, ctx, target)
    editing = has_role(ctx, Role.EDITOR) and not category.archived
    return await run_in_threadpool(lambda: _row(request, ctx, category, editing=editing))


@router.get("/categories/{category_id}/row", name="category_row")
async def category_row(request: Request, ctx: CurrentContext, category_id: str) -> Response:
    target = _parse_uuid(category_id)
    if not is_htmx_fragment_request(request):
        return RedirectResponse(request.url_for("categories").path, status_code=303)
    category = await run_in_threadpool(get_category, request.app.state.session_factory, ctx, target)
    return await run_in_threadpool(lambda: _row(request, ctx, category, editing=False))


@router.post("/categories/{category_id}", name="category_update")
async def category_update(
    request: Request,
    ctx: CurrentContext,
    category_id: str,
    name: FormStr = "",
    kind: FormStr = "",
    treatment: FormStr = "",
    color: FormStr = "",
) -> Response:
    target = _parse_uuid(category_id)
    values = {"name": name, "kind": kind, "treatment": treatment, "color": color}
    factory = request.app.state.session_factory
    try:
        await run_in_threadpool(
            lambda: update_category(
                factory, ctx, target, name=name, kind=kind, treatment=treatment, color=color
            )
        )
    except CategoryError as exc:
        if not exc.field:
            return await run_in_threadpool(_failed, request, ctx, exc)
        errors = {exc.field: ERRORS.get(exc.code, FALLBACK_ERROR)}
        return await run_in_threadpool(
            lambda: _render(
                request,
                ctx,
                status_code=400,
                editing=target,
                edit_values=values,
                edit_errors=errors,
            )
        )
    return await run_in_threadpool(_done, request, ctx, "saved")


@router.post("/categories/{category_id}/move", name="category_move")
async def category_move(
    request: Request, ctx: CurrentContext, category_id: str, direction: FormStr = ""
) -> Response:
    target = _parse_uuid(category_id)
    factory = request.app.state.session_factory
    try:
        await run_in_threadpool(lambda: move_category(factory, ctx, target, direction))
    except CategoryError as exc:
        status_code = 400 if exc.code == "invalid_direction" else 409
        return await run_in_threadpool(_failed, request, ctx, exc, status_code)
    if is_htmx_fragment_request(request):
        return await run_in_threadpool(lambda: _render(request, ctx))
    return RedirectResponse(request.url_for("categories").path, status_code=303)


@router.post("/categories/{category_id}/archive", name="category_archive")
async def category_archive(request: Request, ctx: CurrentContext, category_id: str) -> Response:
    target = _parse_uuid(category_id)
    await run_in_threadpool(archive_category, request.app.state.session_factory, ctx, target)
    return await run_in_threadpool(_done, request, ctx, "archived")


@router.post("/categories/{category_id}/restore", name="category_restore")
async def category_restore(request: Request, ctx: CurrentContext, category_id: str) -> Response:
    target = _parse_uuid(category_id)
    try:
        await run_in_threadpool(restore_category, request.app.state.session_factory, ctx, target)
    except CategoryError as exc:
        return await run_in_threadpool(_failed, request, ctx, exc)
    return await run_in_threadpool(_done, request, ctx, "restored")


@router.post("/categories/{category_id}/delete", name="category_delete")
async def category_delete(request: Request, ctx: CurrentContext, category_id: str) -> Response:
    target = _parse_uuid(category_id)
    outcome = await run_in_threadpool(
        lambda: delete_category(request.app.state.session_factory, ctx, target)
    )
    notice = "deleted" if outcome == "deleted" else "archived_in_use"
    return await run_in_threadpool(_done, request, ctx, notice)
