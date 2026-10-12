"""Invitation links: preview, accept (signed in) and sign-up (signed out). See ADR-0013.

``/invite/<token>`` is reachable signed out. The token is in the URL path, so the request log
redacts it (``assetboard.middleware.redact_path``), uvicorn's access log is off, and these pages
send ``Referrer-Policy: same-origin`` and ``Cache-Control: no-store``. Every POST is protected by
the router-level ``require_csrf`` dependency.
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.exc import OperationalError
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from assetboard.db.engine import is_database_locked, run_immediate
from assetboard.domain.identity import MAX_EMAIL_LENGTH
from assetboard.i18n import gettext
from assetboard.services.auth import (
    MAX_NAME_LENGTH,
    MAX_PASSWORD_LENGTH,
    create_session_for,
    register_with_invitation_async,
)
from assetboard.services.context import select_workspace
from assetboard.services.invitations import (
    AlreadyMember,
    EmailMismatch,
    InvitationInvalid,
    InvitationPreview,
    accept_invitation,
    preview_invitation,
)
from assetboard.web.auth_routes import (
    ERROR_FIELDS,
    ERROR_MESSAGES,
    signed_in_redirect,
    too_long,
)
from assetboard.web.rendering import render_template
from assetboard.web.security import client_host, oidc_start_url, require_csrf, web_session

router = APIRouter(dependencies=[Depends(require_csrf)])

FormStr = Annotated[str, Form()]


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


INVALID = _(
    "This invitation link is not valid. It may have expired, been used or been revoked. Ask the "
    "workspace owner for a new link."
)
EMAIL_MISMATCH = _("This invitation was sent to a different email address.")
EMAIL_MISMATCH_REVOKED = _(
    "This invitation was sent to a different email address. It has now been revoked after too "
    "many attempts; ask the workspace owner for a new link."
)
ALREADY_MEMBER = _("You are already a member of this workspace.")
BUSY = _("The server is busy. Try again in a few seconds.")
ROLE_NAMES = {"owner": _("Owner"), "editor": _("Editor"), "viewer": _("Viewer")}


def _t(request: Request, message: str) -> str:
    return gettext(message, request.state.locale)


def _invite_page(
    request: Request,
    token: str,
    preview: InvitationPreview | None,
    *,
    error: str | None = None,
    notice: str | None = None,
    values: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    signed_in = web_session(request) is not None
    settings = request.app.state.settings
    return render_template(
        request,
        "invite/show.html",
        context={
            "token": token,
            "preview": preview,
            "role_name": _t(request, ROLE_NAMES[preview.role]) if preview else "",
            "signed_in": signed_in,
            "login_url": "/login?next=" + quote(f"/invite/{token}", safe=""),
            "local_sign_up": not settings.oidc_only,
            "oidc_provider": settings.oidc_provider_name if settings.oidc_enabled else None,
            "oidc_start_url": oidc_start_url(f"/invite/{token}"),
            "error": _t(request, error) if error else None,
            "notice": _t(request, notice) if notice else None,
            "values": values or {"email": "", "display_name": ""},
            "errors": {field: _t(request, message) for field, message in (errors or {}).items()},
        },
        status_code=status_code,
    )


async def _preview(request: Request, token: str) -> InvitationPreview | None:
    state = request.app.state
    try:
        return await run_in_threadpool(
            preview_invitation, state.session_factory, token, clock=state.clock
        )
    except InvitationInvalid:
        return None


def _busy(request: Request, token: str) -> Response:
    """The database stayed locked: the invitation is unchanged, so the visitor can retry."""
    response = _invite_page(request, token, None, error=BUSY, status_code=503)
    response.headers["Retry-After"] = "5"
    return response


async def _invalid(request: Request, token: str, message: str = INVALID) -> Response:
    return _invite_page(request, token, None, error=message, status_code=404)


@router.get("/invite/{token}", name="invite")
async def invite_page(request: Request, token: str) -> Response:
    preview = await _preview(request, token)
    if preview is None:
        return await _invalid(request, token)
    return _invite_page(request, token, preview)


@router.post("/invite/{token}/accept", name="invite_accept")
async def invite_accept(request: Request, token: str) -> Response:
    signed_in = web_session(request)
    if signed_in is None:
        return RedirectResponse("/login?next=" + quote(f"/invite/{token}", safe=""), 303)
    state = request.app.state
    try:
        workspace_id = await run_in_threadpool(
            lambda: accept_invitation(
                state.session_factory,
                token,
                user_id=signed_in.user_id,
                user_email=signed_in.email,
                clock=state.clock,
            )
        )
    except InvitationInvalid:
        return await _invalid(request, token)
    except AlreadyMember:
        return _invite_page(request, token, await _preview(request, token), notice=ALREADY_MEMBER)
    except EmailMismatch as exc:
        if exc.revoked:
            return await _invalid(request, token, EMAIL_MISMATCH_REVOKED)
        preview = await _preview(request, token)
        return _invite_page(request, token, preview, error=EMAIL_MISMATCH, status_code=403)
    except OperationalError as exc:
        if not is_database_locked(exc):
            raise
        return _busy(request, token)
    await run_in_threadpool(
        run_immediate,
        state.session_factory,
        lambda session: select_workspace(
            session,
            session_hash=signed_in.session_hash,
            user_id=signed_in.user_id,
            workspace_id=workspace_id,
        ),
    )
    return RedirectResponse(request.url_for("home").path, status_code=303)


@router.post("/invite/{token}/register", name="invite_register")
async def invite_register(
    request: Request,
    token: str,
    email: FormStr = "",
    display_name: FormStr = "",
    password: FormStr = "",
) -> Response:
    state = request.app.state
    if state.settings.oidc_only:
        raise HTTPException(status_code=404)  # sign-up goes through the provider (ADR-0014)
    if web_session(request) is not None:
        return RedirectResponse(request.url_for("invite", token=token).path, status_code=303)
    values = {"email": email, "display_name": display_name}
    preview = await _preview(request, token)
    if preview is None:
        return await _invalid(request, token)
    long_field = too_long(
        {
            "email": (email, MAX_EMAIL_LENGTH),
            "display_name": (display_name, MAX_NAME_LENGTH),
            "password": (password, MAX_PASSWORD_LENGTH),
        }
    )
    if long_field:
        return _invite_page(
            request,
            token,
            preview,
            values=values,
            errors={long_field: ERROR_MESSAGES["too_long"]},
            status_code=400,
        )
    host = client_host(request)
    try:
        outcome = await register_with_invitation_async(
            state.session_factory,
            state.hash_gate,
            token=token,
            email=email,
            display_name=display_name,
            password=password,
            settings=state.settings,
            clock=state.clock,
            host=host,
            user_agent=request.headers.get("user-agent"),
        )
    except OperationalError as exc:
        if not is_database_locked(exc):
            raise
        return _busy(request, token)
    if outcome.status == "invitation_invalid":
        return await _invalid(request, token)
    if outcome.status == "email_mismatch":
        if outcome.invitation_revoked:
            return await _invalid(request, token, EMAIL_MISMATCH_REVOKED)
        return _invite_page(
            request,
            token,
            preview,
            values=values,
            errors={"email": EMAIL_MISMATCH},
            status_code=403,
        )
    if outcome.status == "throttled":
        return _invite_page(
            request,
            token,
            preview,
            values=values,
            error=_("Too many registration attempts from your network. Try again later."),
            status_code=429,
        )
    if outcome.status == "rejected":
        return _invite_page(
            request,
            token,
            preview,
            values=values,
            errors={"email": _("This email address cannot be registered.")},
            status_code=400,
        )
    if outcome.status == "invalid" or outcome.user is None:
        code = outcome.error.code if outcome.error else ""
        return _invite_page(
            request,
            token,
            preview,
            values=values,
            errors={
                ERROR_FIELDS.get(code, "email"): ERROR_MESSAGES.get(code, ERROR_MESSAGES["invalid"])
            },
            status_code=400,
        )
    user_id = outcome.user.id
    session_id, expires_at = await run_in_threadpool(
        lambda: create_session_for(
            state.session_factory,
            user_id,
            settings=state.settings,
            clock=state.clock,
            host=host,
            user_agent=request.headers.get("user-agent"),
        )
    )
    return signed_in_redirect(request, request.url_for("home").path, session_id, expires_at)
