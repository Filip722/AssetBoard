"""Workspace switcher, workspace settings and members pages (F1.2 part B1).

Every state-changing route is protected by the router-level ``require_csrf`` dependency.
Authorization happens in ``services.workspaces`` (owners only); routes only map outcomes to
pages. Successful writes redirect (303) back to the page they came from.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Form, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from assetboard.db.engine import run_immediate
from assetboard.domain.identity import MAX_EMAIL_LENGTH
from assetboard.i18n import gettext
from assetboard.services.context import Role, WorkspaceNotAvailable, select_workspace
from assetboard.services.workspace_options import (
    country_names,
    currency_codes,
    locale_codes,
    timezone_names,
)
from assetboard.services.workspaces import (
    DEFAULT_INVITATION_EXPIRY_DAYS,
    INVITATION_EXPIRY_DAYS,
    MAX_PENDING_INVITATIONS,
    MAX_WORKSPACE_NAME_LENGTH,
    CreatedInvitation,
    InvalidInvitation,
    InvalidWorkspaceSettings,
    TooManyInvitations,
    WorkspaceError,
    WorkspaceSettings,
    change_role,
    create_invitation,
    list_invitations,
    list_members,
    remove_member,
    revoke_invitation,
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
    "base_currency_locked": _("The base currency cannot change: amounts are already stored in it."),
    "invalid_country_code": _("Choose a country from the list."),
    "invalid_timezone": _("Choose a time zone from the list."),
    "invalid_locale": _("Choose a supported language."),
}
MEMBER_ERRORS = {
    "last_owner": _("A workspace needs at least one owner. Make another member an owner first."),
    "self_removal": _("You cannot remove yourself from the workspace."),
}
INVITATION_ERRORS = {
    "invalid_email": _("Enter a valid email address."),
    "email_required": _("Enter the email address of the person you invite."),
    "invalid_expiry": _("Choose how long the link stays valid."),
    "invalid_role": _("Choose a role."),
    "too_many_invitations": _(
        "This workspace has too many pending invitations. Revoke some before creating more."
    ),
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
        current = await run_in_threadpool(
            workspace_settings, request.app.state.session_factory, ctx
        )
        if current.base_currency_locked:
            # The locked field shows the stored currency, whatever was submitted.
            submitted = replace(
                submitted, base_currency=current.base_currency, base_currency_locked=True
            )
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
    created: CreatedInvitation | None = None,
    invite_values: dict[str, str] | None = None,
    invite_errors: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    factory = request.app.state.session_factory
    members = list_members(factory, ctx)
    invitations = list_invitations(factory, ctx)
    base_url = request.app.state.settings.base_url.rstrip("/")
    return render_template(
        request,
        "workspace/members.html",
        context={
            "members": members,
            "invitations": invitations,
            "current_user_id": ctx.user.id,
            "roles": [(role.value, _t(request, ROLE_LABELS[role])) for role in Role],
            "role_labels": {role.value: _t(request, label) for role, label in ROLE_LABELS.items()},
            "error": _t(request, error) if error else None,
            "created": created,
            # Shown once, in this response only: the token is not stored.
            "created_link": f"{base_url}/invite/{created.token}" if created else None,
            "expiry_choices": INVITATION_EXPIRY_DAYS,
            "invite_values": {
                "role": Role.VIEWER.value,
                "email": "",
                "expires_in_days": str(DEFAULT_INVITATION_EXPIRY_DAYS),
            }
            | (invite_values or {}),
            "invite_errors": {
                field: _t(request, message) for field, message in (invite_errors or {}).items()
            },
            "max_pending": MAX_PENDING_INVITATIONS,
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


# Invitations


@router.post("/workspace/invitations", name="workspace_invitations")
async def invitation_create(
    request: Request,
    ctx: CurrentContext,
    role: FormStr = "",
    email: FormStr = "",
    expires_in_days: FormStr = "",
) -> Response:
    values = {"role": role, "email": email, "expires_in_days": expires_in_days}
    factory = request.app.state.session_factory
    field, code, status_code = "", "", 400
    created: CreatedInvitation | None = None
    if len(email) > MAX_EMAIL_LENGTH:
        field, code = "email", "invalid_email"
    elif role not in {r.value for r in Role}:
        field, code = "role", "invalid_role"
    else:
        try:
            days = int(expires_in_days)
        except ValueError:  # includes "²" and strings longer than int()'s digit limit
            days = 0
        try:
            created = await run_in_threadpool(
                lambda: create_invitation(
                    factory, ctx, role=role, email=email, expires_in_days=days
                )
            )
        except InvalidInvitation as exc:
            field, code = exc.field, exc.code
        except TooManyInvitations:
            field, code, status_code = "role", "too_many_invitations", 409
    if created is not None:
        shown = created
        return await run_in_threadpool(lambda: _members_page(request, ctx, created=shown))
    errors = {field: INVITATION_ERRORS[code]}
    return await run_in_threadpool(
        lambda: _members_page(
            request, ctx, invite_values=values, invite_errors=errors, status_code=status_code
        )
    )


@router.post("/workspace/invitations/{invitation_id}/revoke", name="workspace_invitation_revoke")
async def invitation_revoke(request: Request, ctx: CurrentContext, invitation_id: str) -> Response:
    target = _parse_uuid(invitation_id)
    await run_in_threadpool(
        lambda: revoke_invitation(request.app.state.session_factory, ctx, invitation_id=target)
    )
    return _members_redirect(request)
