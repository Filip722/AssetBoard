"""Workspace switcher, workspace settings and members pages (F1.2 part B1).

Every state-changing route is protected by the router-level ``require_csrf`` dependency.
Authorization happens in ``services.workspaces`` (owners only); routes only map outcomes to
pages. Successful writes redirect (303) back to the page they came from.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from assetboard.db.engine import run_immediate
from assetboard.i18n import gettext
from assetboard.services.context import Role, WorkspaceNotAvailable, select_workspace
from assetboard.services.workspace_options import (
    country_names,
    currency_codes,
    locale_codes,
    timezone_names,
)
from assetboard.services.workspaces import (
    MAX_WORKSPACE_NAME_LENGTH,
    InvalidWorkspaceSettings,
    WorkspaceError,
    WorkspaceSettings,
    change_role,
    list_members,
    remove_member,
    update_settings,
    workspace_settings,
)
from assetboard.web.context import CurrentContext
from assetboard.web.rendering import render_template
from assetboard.web.security import require_csrf, web_session

router = APIRouter(dependencies=[Depends(require_csrf)])

FormStr = Annotated[str, Form()]
LOCALE_NAMES = {"en": "English", "pl": "Polski"}


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


SETTINGS_ERRORS = {
    "invalid_workspace_name": _("Enter a workspace name of 1 to 120 characters."),
    "invalid_base_currency": _("Choose a supported currency."),
    "invalid_country_code": _("Choose a country from the list."),
    "invalid_timezone": _("Choose a time zone from the list."),
    "invalid_locale": _("Choose a supported language."),
}
MEMBER_ERRORS = {
    "last_owner": _("A workspace needs at least one owner. Make another member an owner first."),
    "self_removal": _("You cannot remove yourself from the workspace."),
}
MEMBER_ERROR_FALLBACK = _("The change could not be made. Reload the page and try again.")
ROLE_LABELS = {
    Role.OWNER: _("Owner"),
    Role.EDITOR: _("Editor"),
    Role.VIEWER: _("Viewer"),
}


def _t(request: Request, message: str) -> str:
    return gettext(message, request.state.locale)


def _parse_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise WorkspaceNotAvailable from None


# Switcher


@router.post("/workspace/switch", name="workspace_switch")
async def switch(request: Request, workspace_id: FormStr = "") -> Response:
    signed_in = web_session(request)
    if signed_in is None:  # the gate redirects first; this guards direct use
        raise WorkspaceNotAvailable
    target = _parse_uuid(workspace_id)
    state = request.app.state
    await run_in_threadpool(
        run_immediate,
        state.session_factory,
        lambda session: select_workspace(
            session,
            session_hash=signed_in.session_hash,
            user_id=signed_in.user_id,
            workspace_id=target,
        ),
    )
    return RedirectResponse(request.url_for("home").path, status_code=303)


# Settings


def _settings_page(
    request: Request,
    values: WorkspaceSettings,
    *,
    errors: dict[str, str] | None = None,
    notice: str | None = None,
    status_code: int = 200,
) -> Response:
    return render_template(
        request,
        "workspace/settings.html",
        context={
            "values": values,
            "errors": {field: _t(request, message) for field, message in (errors or {}).items()},
            "notice": notice,
            "currency_options": [(code, code) for code in currency_codes()],
            "country_options": list(country_names(request.state.locale).items()),
            "timezone_options": [(zone, zone) for zone in timezone_names()],
            "locales": [(code, LOCALE_NAMES.get(code, code)) for code in locale_codes()],
            "max_name_length": MAX_WORKSPACE_NAME_LENGTH,
        },
        status_code=status_code,
    )


@router.get("/workspace/settings", name="workspace_settings")
async def settings_form(request: Request, ctx: CurrentContext, saved: str = "") -> Response:
    values = await run_in_threadpool(workspace_settings, request.app.state.session_factory, ctx)
    notice = _t(request, _("Workspace settings saved.")) if saved else None
    return _settings_page(request, values, notice=notice)


@router.post("/workspace/settings")
async def settings_submit(
    request: Request,
    ctx: CurrentContext,
    name: FormStr = "",
    base_currency: FormStr = "",
    country_code: FormStr = "",
    timezone: FormStr = "",
    locale: FormStr = "",
) -> Response:
    submitted = WorkspaceSettings(
        name=name,
        base_currency=base_currency,
        country_code=country_code or None,
        timezone=timezone,
        locale=locale,
    )
    try:
        await run_in_threadpool(
            lambda: update_settings(
                request.app.state.session_factory,
                ctx,
                name=name,
                base_currency=base_currency,
                country_code=country_code or None,
                timezone=timezone,
                locale=locale,
            )
        )
    except InvalidWorkspaceSettings as exc:
        return _settings_page(
            request,
            submitted,
            errors={exc.field: SETTINGS_ERRORS[exc.code]},
            status_code=400,
        )
    return RedirectResponse(
        request.url_for("workspace_settings").path + "?saved=1", status_code=303
    )


# Members


def _members_page(
    request: Request,
    ctx: CurrentContext,
    *,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    members = list_members(request.app.state.session_factory, ctx)
    return render_template(
        request,
        "workspace/members.html",
        context={
            "members": members,
            "current_user_id": ctx.user.id,
            "roles": [(role.value, _t(request, ROLE_LABELS[role])) for role in Role],
            "role_labels": {role.value: _t(request, label) for role, label in ROLE_LABELS.items()},
            "error": _t(request, error) if error else None,
        },
        status_code=status_code,
    )


@router.get("/workspace/members", name="workspace_members")
async def members_page(request: Request, ctx: CurrentContext) -> Response:
    return await run_in_threadpool(_members_page, request, ctx)


def _members_redirect(request: Request) -> Response:
    return RedirectResponse(request.url_for("workspace_members").path, status_code=303)


async def _member_error(request: Request, ctx: CurrentContext, exc: WorkspaceError) -> Response:
    message = MEMBER_ERRORS.get(exc.code, MEMBER_ERROR_FALLBACK)
    return await run_in_threadpool(
        lambda: _members_page(request, ctx, error=message, status_code=409)
    )


@router.post("/workspace/members/{membership_id}/role", name="workspace_member_role")
async def member_role(
    request: Request, ctx: CurrentContext, membership_id: str, role: FormStr = ""
) -> Response:
    target = _parse_uuid(membership_id)
    try:
        new_role = Role(role)
    except ValueError:
        return await run_in_threadpool(
            lambda: _members_page(request, ctx, error=_("Choose a role."), status_code=400)
        )
    try:
        await run_in_threadpool(
            lambda: change_role(
                request.app.state.session_factory, ctx, membership_id=target, role=new_role
            )
        )
    except WorkspaceError as exc:
        return await _member_error(request, ctx, exc)
    return _members_redirect(request)


@router.post("/workspace/members/{membership_id}/remove", name="workspace_member_remove")
async def member_remove(request: Request, ctx: CurrentContext, membership_id: str) -> Response:
    target = _parse_uuid(membership_id)
    try:
        await run_in_threadpool(
            lambda: remove_member(request.app.state.session_factory, ctx, membership_id=target)
        )
    except WorkspaceError as exc:
        return await _member_error(request, ctx, exc)
    return _members_redirect(request)
