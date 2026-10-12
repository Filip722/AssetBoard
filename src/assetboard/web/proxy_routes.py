"""Sign-in through the reverse proxy's identity headers (F1.4, ADR-0015).

* ``GET /auth/proxy?next=…``: the gate sends signed-out page loads here when the trusted proxy
  presents a user; ``/login`` and invitation pages link here ("Continue as <user>"). It maps the
  identity with the shared account rules and answers with a 303: to ``/auth/proxy/continue``
  (new session), ``next`` (a proxy session for the same user already exists), ``/login/link``
  (password confirmation) or ``/auth/proxy/failed/<reason>``.
* ``GET /auth/proxy/continue?next=…``: the cookie check. With the new session it goes on to
  ``next``; a browser that did not keep the cookie (for example an ``https`` ``BASE_URL``
  opened over ``http``) gets a page instead of another round through the gate.
* An untrusted peer, missing headers and repeated or invalid headers all get the same page, so
  the response tells nothing about which check failed.

Every route answers 404 while proxy authentication is off. The pages have no forms and send no
Referer, since ``next`` can be ``/invite/<token>``.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.exc import OperationalError
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from assetboard.db.engine import is_database_locked
from assetboard.i18n import gettext
from assetboard.services.oidc_accounts import (
    SignInOutcome,
    proxy_policy,
    record_sign_in_failure,
    sign_in_external,
)
from assetboard.services.proxy_auth import hashed
from assetboard.web.errors import BUSY_RETRY_AFTER
from assetboard.web.invite_routes import BUSY, EMAIL_MISMATCH, EMAIL_MISMATCH_REVOKED, INVALID
from assetboard.web.oidc_routes import finish_external_sign_in
from assetboard.web.rendering import render_template
from assetboard.web.security import (
    client_host,
    oidc_next,
    proxy_request,
    session_cookie_name,
    web_session,
)


def _enabled(request: Request) -> None:
    if not request.app.state.settings.proxy_auth_enabled:
        raise HTTPException(status_code=404)


router = APIRouter(dependencies=[Depends(_enabled)])


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


# Failure pages: reason -> (status, message). "unavailable" is the one answer for an untrusted
# peer, missing headers and repeated or invalid headers.
FAILURES: dict[str, tuple[int, str]] = {
    "unavailable": (
        403,
        _(
            "Sign-in through your proxy is not available for this request. Try again; if it "
            "keeps failing, contact your administrator."
        ),
    ),
    "busy": (503, BUSY),
    "no_account": (
        403,
        _("There is no account for your sign-in. Ask an administrator for an invitation."),
    ),
    "account_exists": (
        409,
        _(
            "An account with this email address already exists. Sign in with your password, "
            "then link it from your profile."
        ),
    ),
    "email_unverified": (
        403,
        _(
            "Your proxy did not send a usable email address, so no account can be created or "
            "linked. Contact your administrator."
        ),
    ),
    "inactive": (403, _("This account is deactivated. Contact your administrator.")),
    "invitation_invalid": (404, INVALID),
    "email_mismatch": (403, EMAIL_MISMATCH),
    "email_mismatch_revoked": (403, EMAIL_MISMATCH_REVOKED),
    "throttled": (
        429,
        _("Too many accounts were created from your network. Try again later."),
    ),
    "cookie": (
        400,
        _(
            "Your browser did not keep the sign-in. Open this site at its configured address "
            "(%(base_url)s), allow cookies for it, and try again."
        ),
    ),
}


def _state(request: Request) -> Any:
    return request.app.state


def _user_agent(request: Request) -> str | None:
    return request.headers.get("user-agent")


def _failed(reason: str) -> Response:
    return RedirectResponse(
        f"/auth/proxy/failed/{reason if reason in FAILURES else 'unavailable'}", status_code=303
    )


def _continue_url(target: str) -> str:
    return "/auth/proxy/continue?next=" + quote(oidc_next(target), safe="")


async def _audit(request: Request, reason: str, key: str) -> None:
    """Best effort, and at most once per key in 10 minutes (then logged only)."""
    state = _state(request)
    if not state.proxy_audits.first(key, now=state.clock.now()):
        return
    try:
        await run_in_threadpool(
            lambda: record_sign_in_failure(
                state.session_factory,
                proxy_policy(state.settings).failure_action,
                reason,
                clock=state.clock,
                host=client_host(request),
                user_agent=_user_agent(request),
            )
        )
    except OperationalError as exc:
        if not is_database_locked(exc):
            raise


def _log(request: Request, reason: str) -> None:
    _state(request).logger.warning("proxy sign-in failed: reason=%s", reason)


@router.get("/auth/proxy", name="proxy_sign_in")
async def proxy_sign_in(request: Request, next: str = "") -> Response:  # noqa: A002
    state = _state(request)
    settings = state.settings
    next_path = oidc_next(next)
    proxy = proxy_request(request)
    if proxy is None or proxy.headers is None or proxy.headers.identity is None:
        if proxy is not None and proxy.headers is not None:
            status = proxy.headers.status
            if status in {"duplicate", "invalid"}:
                # The trusted proxy is misconfigured (the gate already logged a WARNING).
                await _audit(request, f"headers_{status}", f"headers:{status}")
        return _failed("unavailable")
    identity = proxy.headers.identity

    current = web_session(request)
    if current is not None and current.auth_method == "proxy":
        # The gate honours a proxy session only for the subject linked to its user, so this is
        # the same person: nothing to rotate.
        return RedirectResponse(next_path, status_code=303)

    subject_key = hashed(identity.subject)

    def audit_filter(reason: str) -> bool:
        first = state.proxy_audits.first(f"{subject_key}:{reason}", now=state.clock.now())
        if not first:
            _log(request, reason)
        return first

    try:
        outcome: SignInOutcome = await run_in_threadpool(
            lambda: sign_in_external(
                state.session_factory,
                identity,
                next_path=next_path,
                settings=settings,
                clock=state.clock,
                host=client_host(request),
                user_agent=_user_agent(request),
                # Any other session this browser had (password, OIDC, another proxy user's) is
                # replaced: a new session id every time.
                existing_session_id=request.cookies.get(session_cookie_name(settings)),
                audit_filter=audit_filter,
            )
        )
    except OperationalError as exc:
        if not is_database_locked(exc):
            raise
        _log(request, "database_locked")
        return _failed("busy")

    async def report(reason: str) -> None:
        _log(request, reason)
        await _audit(request, reason, f"{subject_key}:{reason}")

    return await finish_external_sign_in(
        request,
        outcome,
        next_path,
        failed=_failed,
        report=report,
        landing=_continue_url,
    )


@router.get("/auth/proxy/continue", name="proxy_continue")
async def proxy_continue(request: Request, next: str = "") -> Response:  # noqa: A002
    current = web_session(request)
    if current is not None and current.auth_method == "proxy":
        return RedirectResponse(oidc_next(next), status_code=303)
    # The session cookie set a moment ago did not come back: stop here, never loop.
    _log(request, "cookie_not_kept")
    return _failure_page(request, "cookie")


@router.get("/auth/proxy/failed/{reason}", name="proxy_failed")
async def proxy_failed(request: Request, reason: str) -> Response:
    if reason not in FAILURES or reason == "cookie":
        raise HTTPException(status_code=404)
    return _failure_page(request, reason)


def _failure_page(request: Request, reason: str) -> Response:
    status, message = FAILURES[reason]
    text = gettext(message, request.state.locale) % {"base_url": _state(request).settings.base_url}
    response = render_template(
        request, "auth/proxy_failed.html", context={"message": text}, status_code=status
    )
    if status == 503:
        response.headers["Retry-After"] = BUSY_RETRY_AFTER
    return response
