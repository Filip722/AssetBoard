"""Instance administration of user accounts (F1.5 part B).

* ``GET /admin/users``: every account, with sign-in methods as labels (never subjects).
* ``GET /admin/users/{id}/{action}`` and ``POST``: one confirm page per action, the action from
  ``ADMIN_ACTIONS`` (activate, deactivate, promote, demote).
* ``GET /admin/users/new`` and ``POST``: create an account with a generated temporary password,
  shown once in the POST response (no redirect, no cookie, no flash, never logged or audited).
  Both answer 404 under ``OIDC_ONLY``.

Only active instance administrators get these pages (403 otherwise); every POST is confirmed
by the acting administrator through ``web.confirm.run_confirmed``, the one confirmation path.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from assetboard.db.models.identity import User
from assetboard.domain.identity import (
    InvalidDisplayName,
    InvalidEmail,
    normalize_display_name,
    normalize_email,
)
from assetboard.i18n import gettext
from assetboard.services.admin_users import (
    ADMIN_ACTIONS,
    AdminAction,
    NotInstanceAdmin,
    UserRow,
    apply_admin_action,
    generate_temporary_password,
    get_user,
    list_users,
    record_user_created,
)
from assetboard.services.auth import WebSession, argon2_hash, create_user_with_temporary_password
from assetboard.services.profile import ConfirmMethod
from assetboard.web.confirm import (
    BUSY,
    CONFIRM_FAILED,
    CONFIRM_STALE,
    confirm_view,
    run_confirmed,
)
from assetboard.web.errors import BUSY_RETRY_AFTER
from assetboard.web.rendering import render_template
from assetboard.web.security import client_host, require_csrf, web_session

router = APIRouter(dependencies=[Depends(require_csrf)])

FormStr = Annotated[str, Form()]


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


ACTION_TITLES: dict[str, str] = {
    "activate": _("Activate account"),
    "deactivate": _("Deactivate account"),
    "promote": _("Make instance administrator"),
    "demote": _("Remove instance administrator"),
}
ACTION_EXPLANATIONS: dict[str, str] = {
    "activate": _("%(user)s can sign in again."),
    "deactivate": _(
        "%(user)s can no longer sign in, and every session of this account ends now: "
        "password, identity provider and proxy sign-ins alike."
    ),
    "promote": _("%(user)s can manage every account on this instance."),
    "demote": _("%(user)s can no longer manage accounts on this instance."),
}
ACTION_RESULTS: dict[str, str] = {
    "done": _("Done."),
    "unchanged": _("Nothing to change: the account is already in that state."),
    "self": _("You cannot deactivate your own account or remove your own administrator role."),
    "last_admin": _("This is the last active administrator, so this would leave none."),
}
# With OIDC_ADMIN_GROUP set, the provider's group decides the role again at each OIDC sign-in
# (``oidc_accounts._sync_admin``), so promote and demote never claim a durable result.
GROUP_DECIDES = _(
    "The identity provider decides this at each sign-in; to change it, change the group there."
)
ROLE_ACTIONS = frozenset({"promote", "demote"})
CREATE_ERRORS = {
    "email": _("Enter a valid email address."),
    "email_taken": _("An account with this email address already exists."),
    "display_name": _(
        "Enter a display name of 1 to 120 characters, without control or invisible characters."
    ),
}


def _state(request: Request) -> Any:
    return request.app.state


def _t(request: Request, message: str, **values: str) -> str:
    return (
        gettext(message, request.state.locale) % values
        if values
        else gettext(message, request.state.locale)
    )


def _require_admin(request: Request) -> WebSession:
    current = web_session(request)
    if current is None:  # the gate redirects first; this guards direct use
        raise HTTPException(status_code=404)
    if not current.is_instance_admin:
        raise NotInstanceAdmin
    return current


def _group_decides(request: Request, action: str) -> bool:
    return action in ROLE_ACTIONS and _state(request).settings.oidc_admin_group is not None


def _creation_allowed(request: Request) -> None:
    if _state(request).settings.oidc_only:
        raise HTTPException(status_code=404)


def _action(name: str) -> AdminAction:
    if name not in ADMIN_ACTIONS:
        raise HTTPException(status_code=404)
    return name  # type: ignore[return-value]


async def _users_page(
    request: Request,
    current: WebSession,
    *,
    notice: str | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    state = _state(request)
    users = await run_in_threadpool(
        lambda: list_users(state.session_factory, actor_id=current.user_id)
    )
    return render_template(
        request,
        "admin/users.html",
        context={
            "users": users,
            "can_create": not state.settings.oidc_only,
            "group_decides_admin": state.settings.oidc_admin_group is not None,
            "notice": notice,
            "error": error,
        },
        status_code=status_code,
    )


def _refusal(status: str) -> tuple[str, int]:
    if status == "busy":
        return BUSY, 503
    if status == "stale":
        return CONFIRM_STALE, 403
    return CONFIRM_FAILED, 401


@router.get("/admin/users", name="admin_users")
async def users(request: Request) -> Response:
    return await _users_page(request, _require_admin(request))


# One confirm page per action


async def _target(request: Request, current: WebSession, user_id: str) -> UserRow:
    state = _state(request)
    try:
        target_id = UUID(user_id)
    except ValueError:
        raise HTTPException(status_code=404) from None
    row = await run_in_threadpool(
        lambda: get_user(state.session_factory, actor_id=current.user_id, user_id=target_id)
    )
    if row is None:
        raise HTTPException(status_code=404)
    return row


async def _confirm_page(
    request: Request,
    current: WebSession,
    action: AdminAction,
    target: UserRow,
    *,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    response = render_template(
        request,
        "admin/confirm.html",
        context={
            "action": action,
            "title": _t(request, ACTION_TITLES[action]),
            "explanation": _t(request, ACTION_EXPLANATIONS[action], user=target.email),
            "group_notice": _t(request, GROUP_DECIDES) if _group_decides(request, action) else None,
            "target": target,
            "confirm": await confirm_view(request, current),
            "error": error,
        },
        status_code=status_code,
    )
    if status_code == 503:
        response.headers["Retry-After"] = BUSY_RETRY_AFTER
    return response


@router.get("/admin/users/{user_id}/{action}", name="admin_user_action")
async def action_form(request: Request, user_id: str, action: str) -> Response:
    current = _require_admin(request)
    chosen = _action(action)
    return await _confirm_page(request, current, chosen, await _target(request, current, user_id))


@router.post("/admin/users/{user_id}/{action}")
async def action_submit(
    request: Request, user_id: str, action: str, confirm_password: FormStr = ""
) -> Response:
    current = _require_admin(request)
    chosen = _action(action)
    target = await _target(request, current, user_id)
    state = _state(request)

    def apply(session: Session, actor: User, _method: ConfirmMethod) -> str:
        return apply_admin_action(
            session,
            actor,
            target.id,
            chosen,
            clock=state.clock,
            host=client_host(request),
            user_agent=request.headers.get("user-agent"),
        )

    outcome = await run_confirmed(
        request, current, purpose=f"admin_{chosen}", password=confirm_password, action=apply
    )
    if outcome.status != "confirmed" or outcome.value is None:
        message, status = _refusal(outcome.status)
        return await _confirm_page(
            request, current, chosen, target, error=_t(request, message), status_code=status
        )
    if outcome.value == "not_found":
        raise HTTPException(status_code=404)
    message = _t(request, ACTION_RESULTS[outcome.value])
    if outcome.value == "done" and _group_decides(request, chosen):
        message = f"{message} {_t(request, GROUP_DECIDES)}"
    if outcome.value in {"self", "last_admin"}:
        return await _confirm_page(request, current, chosen, target, error=message, status_code=409)
    return await _users_page(request, current, notice=message)


# Create an account with a temporary password


async def _new_user_page(
    request: Request,
    current: WebSession,
    *,
    values: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    response = render_template(
        request,
        "admin/user_new.html",
        context={
            "values": values or {"email": "", "display_name": ""},
            "errors": {key: _t(request, message) for key, message in (errors or {}).items()},
            "confirm": await confirm_view(request, current),
            "error": error,
        },
        status_code=status_code,
    )
    if status_code == 503:
        response.headers["Retry-After"] = BUSY_RETRY_AFTER
    return response


@router.get("/admin/users/new", name="admin_user_new")
async def new_user_form(request: Request) -> Response:
    current = _require_admin(request)
    _creation_allowed(request)
    return await _new_user_page(request, current)


@router.post("/admin/users/new")
async def new_user_submit(
    request: Request,
    email: FormStr = "",
    display_name: FormStr = "",
    confirm_password: FormStr = "",
) -> Response:
    current = _require_admin(request)
    _creation_allowed(request)
    state = _state(request)
    values = {"email": email[:400], "display_name": display_name[:400]}
    errors: dict[str, str] = {}
    try:
        clean_email = normalize_email(email)
    except InvalidEmail:
        errors["email"] = CREATE_ERRORS["email"]
        clean_email = ""
    try:
        clean_name = normalize_display_name(display_name)
    except InvalidDisplayName:
        errors["display_name"] = CREATE_ERRORS["display_name"]
        clean_name = ""
    if errors:
        return await _new_user_page(request, current, values=values, errors=errors, status_code=400)

    # Generated and hashed outside the write transaction; shown once below, kept nowhere.
    password = generate_temporary_password(clean_email, clean_name)
    # Through the hash gate like every other hash (a full gate is the usual 503).
    password_hash = await state.hash_gate.run(lambda: argon2_hash(password))

    def create(session: Session, actor: User, _method: ConfirmMethod) -> str:
        if not actor.is_active or not actor.is_instance_admin:
            raise NotInstanceAdmin
        if session.scalar(select(User.id).where(User.email == clean_email)) is not None:
            return "email_taken"
        try:
            with session.begin_nested():
                user = create_user_with_temporary_password(
                    session,
                    email=clean_email,
                    display_name=clean_name,
                    password=password,
                    password_hash=password_hash,
                    settings=state.settings,
                )
                session.flush()
        except IntegrityError:
            return "email_taken"
        record_user_created(
            session,
            actor,
            user,
            clock=state.clock,
            host=client_host(request),
            user_agent=request.headers.get("user-agent"),
        )
        return "created"

    outcome = await run_confirmed(
        request, current, purpose="admin_create_user", password=confirm_password, action=create
    )
    if outcome.status != "confirmed" or outcome.value is None:
        message, status = _refusal(outcome.status)
        return await _new_user_page(
            request, current, values=values, error=_t(request, message), status_code=status
        )
    if outcome.value == "email_taken":
        return await _new_user_page(
            request,
            current,
            values=values,
            errors={"email": CREATE_ERRORS["email_taken"]},
            status_code=409,
        )
    # The only place the temporary password ever appears. Every page is Cache-Control:
    # no-store (security headers); it is set here as well so this never depends on that.
    response = render_template(
        request,
        "admin/user_created.html",
        context={"email": clean_email, "display_name": clean_name, "password": password},
    )
    response.headers["Cache-Control"] = "no-store"
    return response
