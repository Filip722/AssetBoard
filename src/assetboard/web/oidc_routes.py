"""OIDC sign-in pages (F1.3, ADR-0014): start, callback, failure pages, link confirmation.

* ``GET /auth/oidc/start`` is a link, not a form. It seals the flow cookie and redirects to the
  provider.
* ``GET /auth/oidc/callback`` always deletes the flow cookie and always answers with a 303 to a
  URL without a query: ``next``, the link confirmation, or ``/auth/oidc/failed/<reason>``. No page
  is ever rendered at the callback URL, which carries the authorization code.
* ``/auth/oidc/*`` pages send ``Referrer-Policy: no-referrer`` and contain no forms (under
  ``no-referrer`` browsers send ``Origin: null`` on POSTs, which the Origin check refuses). The
  password confirmation form is ``/login/link``, with ``same-origin`` like ``/login``.

Every ``/auth/oidc/*`` route answers 404 while OIDC is not configured; ``/login/link`` while
neither OIDC nor proxy authentication is.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from assetboard.db.engine import is_database_locked, run_immediate
from assetboard.db.models.identity import User, UserSession
from assetboard.i18n import gettext
from assetboard.services.auth import MAX_PASSWORD_LENGTH, session_id_hash
from assetboard.services.context import select_workspace
from assetboard.services.oidc import OidcClient, OidcError, log_safe
from assetboard.services.oidc_accounts import (
    SignInOutcome,
    link_identity_async,
    record_oidc_failure,
    sign_in_external,
)
from assetboard.services.oidc_flow import FlowState, PendingLink
from assetboard.services.profile import link_identity_to_user
from assetboard.web.auth_routes import signed_in_redirect
from assetboard.web.errors import BUSY_RETRY_AFTER
from assetboard.web.invite_routes import (
    BUSY,
    EMAIL_MISMATCH,
    EMAIL_MISMATCH_REVOKED,
    INVALID,
)
from assetboard.web.rendering import render_template
from assetboard.web.security import (
    clear_cookie,
    client_host,
    oidc_flow_cookie_name,
    oidc_link_cookie_name,
    oidc_next,
    require_csrf,
    session_cookie_name,
    set_short_cookie,
    web_session,
)
from assetboard.web.throttle import retry_after, throttled_message

router = APIRouter(dependencies=[Depends(require_csrf)])

FormStr = Annotated[str, Form()]


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


# Failure pages: reason -> (status, message). ``%(provider)s`` is the configured button label.
FAILURES: dict[str, tuple[int, str]] = {
    "start_over": (
        400,
        _("This sign-in expired, or another sign-in was started in a different tab. Start over."),
    ),
    "cancelled": (403, _("Sign-in with %(provider)s was cancelled.")),
    "failed": (
        400,
        _(
            "Sign-in with %(provider)s did not work. Try again; if it keeps failing, contact "
            "your administrator."
        ),
    ),
    "unavailable": (
        503,
        _("%(provider)s cannot be reached right now. Try again in a few minutes."),
    ),
    "busy": (503, BUSY),
    "no_account": (
        403,
        _(
            "There is no account for your %(provider)s sign-in. Ask an administrator for an "
            "invitation."
        ),
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
            "%(provider)s did not confirm that your email address is verified. Verify it "
            "there, then try again."
        ),
    ),
    "domain_not_allowed": (403, _("Accounts with your email domain cannot sign in here.")),
    "inactive": (403, _("This account is deactivated. Contact your administrator.")),
    "invitation_invalid": (404, INVALID),
    "email_mismatch": (403, EMAIL_MISMATCH),
    "email_mismatch_revoked": (403, EMAIL_MISMATCH_REVOKED),
    "throttled": (
        429,
        _("Too many accounts were created from your network. Try again later."),
    ),
}
# Protocol errors from ``OidcClient.complete`` -> failure page.
_PROTOCOL_PAGES = {
    "state_mismatch": "start_over",
    "access_denied": "cancelled",
    "provider_unreachable": "unavailable",
    "discovery_failed": "unavailable",
}
LINK_FAILED = _("The password is not correct. Try again, or start over.")
LINK_EXPIRED = _("This confirmation expired. Start over to sign in.")


def _t(request: Request, message: str) -> str:
    provider = request.app.state.settings.oidc_provider_name
    return gettext(message, request.state.locale) % {"provider": provider}


def _state(request: Request) -> Any:
    return request.app.state


def _client(request: Request) -> OidcClient:
    client: OidcClient | None = _state(request).oidc
    if client is None:
        raise HTTPException(status_code=404)
    return client


def _user_agent(request: Request) -> str | None:
    return request.headers.get("user-agent")


def _failed(request: Request, reason: str) -> Response:
    """Every failure leaves through a 303 to a URL without a query; the flow cookie is gone."""
    response = RedirectResponse(f"/auth/oidc/failed/{reason}", status_code=303)
    settings = _state(request).settings
    clear_cookie(response, settings, oidc_flow_cookie_name(settings))
    return response


async def _audit(request: Request, reason: str, *, via: str | None = None) -> None:
    """Best effort: a busy database must not turn a refused sign-in into an error."""
    state = _state(request)
    try:
        await run_in_threadpool(
            lambda: record_oidc_failure(
                state.session_factory,
                reason,
                clock=state.clock,
                host=client_host(request),
                user_agent=_user_agent(request),
                via=via,
            )
        )
    except OperationalError as exc:
        if not is_database_locked(exc):
            raise


def _log(request: Request, reason: str, detail: str | None = None) -> None:
    _state(request).logger.warning(
        "OIDC sign-in failed: reason=%s detail=%r", reason, log_safe(detail or "")
    )


# Start


@router.get("/auth/oidc/start", name="oidc_start")
async def oidc_start(request: Request, next: str = "") -> Response:  # noqa: A002
    client = _client(request)
    state = _state(request)
    next_path = oidc_next(next)
    if web_session(request) is not None:
        return RedirectResponse(next_path, status_code=303)
    try:
        authorization = await run_in_threadpool(client.begin)
    except OidcError as error:
        _log(request, error.reason, error.detail)
        return _failed(request, _PROTOCOL_PAGES.get(error.reason, "failed"))
    flow = FlowState(
        authorization.state, authorization.nonce, authorization.code_verifier, next_path
    )
    response = RedirectResponse(authorization.url, status_code=303)
    set_short_cookie(
        response,
        state.settings,
        oidc_flow_cookie_name(state.settings),
        flow.seal(state.oidc_flow_key, now=state.clock.now()),
    )
    return response


# Callback


@router.get("/auth/oidc/callback", name="oidc_callback")
async def oidc_callback(request: Request) -> Response:
    client = _client(request)
    state = _state(request)
    settings = state.settings
    flow = FlowState.unseal(
        state.oidc_flow_key,
        request.cookies.get(oidc_flow_cookie_name(settings)),
        now=state.clock.now(),
    )
    if flow is None:
        # No flow from this browser (expired, or a callback URL someone else started). Anyone
        # can cause this with a crafted URL, so it is logged, never audited.
        _log(request, "flow_missing")
        return _failed(request, "start_over")
    # Single use, decided before any provider call or database write.
    if not state.oidc_consumed_states.consume(flow.state, now=state.clock.now()):
        _log(request, "flow_replayed")
        return _failed(request, "start_over")
    params = dict(request.query_params.items())
    if flow.link_user is not None:
        # Linking from the profile (F1.5): never a sign-in, so no login event and no rotation.
        return await _finish_link(request, client, flow, params)
    try:
        identity = await run_in_threadpool(
            lambda: client.complete(
                params, state=flow.state, nonce=flow.nonce, code_verifier=flow.code_verifier
            )
        )
    except OidcError as error:
        _log(request, error.reason, error.detail)
        if error.auditable:
            # Only failures after the provider accepted the code are audited; earlier ones
            # (state, iss, provider errors, token endpoint errors) come from URLs anyone can
            # craft.
            await _audit(request, error.reason)
        return _failed(request, _PROTOCOL_PAGES.get(error.reason, "failed"))

    # Validated again: the cookie is ours, but the rule must hold for every redirect target.
    next_path = oidc_next(flow.next_path)
    existing = request.cookies.get(session_cookie_name(settings))
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
                existing_session_id=existing,
                locale=request.state.locale,
            )
        )
    except OperationalError as exc:
        if not is_database_locked(exc):
            raise
        _log(request, "database_locked")
        return _failed(request, "busy")

    response = await finish_external_sign_in(
        request,
        outcome,
        next_path,
        failed=lambda reason: RedirectResponse(
            f"/auth/oidc/failed/{reason if reason in FAILURES else 'failed'}", status_code=303
        ),
        report=lambda reason: _report(request, reason),
    )
    # Every outcome deletes the flow cookie (``_failed`` does the same for earlier failures).
    clear_cookie(response, settings, oidc_flow_cookie_name(settings))
    return response


async def _report(request: Request, reason: str) -> None:
    _log(request, reason)
    await _audit(request, reason)


async def finish_external_sign_in(
    request: Request,
    outcome: SignInOutcome,
    next_path: str,
    *,
    failed: Callable[[str], Response],
    report: Callable[[str], Awaitable[None]],
    landing: Callable[[str], str] = lambda target: target,
) -> Response:
    """The response to a mapped OIDC or proxy sign-in (both answer with a 303).

    ``failed(reason)`` builds the failure redirect (the service already audited the refusal),
    ``report`` logs and audits an unexpected problem, and ``landing(target)`` is where a new
    session is sent first (the proxy adds a cookie check, ADR-0015).
    """
    state = _state(request)
    settings = state.settings
    if outcome.status == "confirm_link" and outcome.pending_link is not None:
        try:
            sealed = outcome.pending_link.seal(state.oidc_link_key, now=state.clock.now())
        except ValueError:
            # Cannot happen with the caps on next, issuer, subject and email; never a 500.
            await report("pending_link_too_large")
            return failed("failed")
        response = RedirectResponse("/login/link", status_code=303)
        set_short_cookie(response, settings, oidc_link_cookie_name(settings), sealed)
        return response

    if outcome.status != "signed_in" or outcome.session_id is None:
        # The service already audited the refusal with its reason.
        reason = outcome.status
        if reason == "email_mismatch" and outcome.invitation_revoked:
            reason = "email_mismatch_revoked"
        return failed(reason)

    target = next_path
    session_id = outcome.session_id
    if outcome.joined_workspace_id is not None and outcome.user_id is not None:
        user_id, workspace_id = outcome.user_id, outcome.joined_workspace_id
        # Invitation sign-up: start in the workspace just joined, as a local sign-up does.
        await run_in_threadpool(
            run_immediate,
            state.session_factory,
            lambda session: select_workspace(
                session,
                session_hash=session_id_hash(session_id),
                user_id=user_id,
                workspace_id=workspace_id,
            ),
        )
        target = "/"
    if outcome.expires_at is None:
        raise RuntimeError("external sign-in returned a session without an expiry")
    return signed_in_redirect(request, landing(target), session_id, outcome.expires_at)


@router.get("/auth/oidc/failed/{reason}", name="oidc_failed")
async def oidc_failed(request: Request, reason: str) -> Response:
    _client(request)
    if reason not in FAILURES:
        raise HTTPException(status_code=404)
    status, message = FAILURES[reason]
    response = render_template(
        request,
        "auth/oidc_failed.html",
        context={"message": _t(request, message)},
        status_code=status,
    )
    if status == 503:
        response.headers["Retry-After"] = BUSY_RETRY_AFTER
    return response


# Linking from the profile (F1.5)

# Outcome codes for ``/account?link=…``: an allowlist, never free text.
LINK_CODES = frozenset(
    {"linked", "already_linked", "one_per_provider", "failed", "cancelled", "unavailable", "busy"}
)


def _link_result(request: Request, code: str) -> Response:
    response = RedirectResponse(f"/account?link={code}", status_code=303)
    settings = _state(request).settings
    clear_cookie(response, settings, oidc_flow_cookie_name(settings))
    return response


async def _finish_link(
    request: Request, client: OidcClient, flow: FlowState, params: dict[str, str]
) -> Response:
    state = _state(request)
    current = web_session(request)
    if (
        current is None
        or current.user_id != flow.link_user
        or current.session_hash != flow.link_session
    ):
        # Another browser session (or none) than the one that confirmed: link nothing.
        _log(request, "link_session_mismatch")
        return _link_result(request, "failed")
    try:
        identity = await run_in_threadpool(
            lambda: client.complete(
                params, state=flow.state, nonce=flow.nonce, code_verifier=flow.code_verifier
            )
        )
    except OidcError as error:
        _log(request, error.reason, error.detail)
        if error.auditable:
            await _audit(request, error.reason, via="profile")
        code = {
            "access_denied": "cancelled",
            "provider_unreachable": "unavailable",
            "discovery_failed": "unavailable",
        }.get(error.reason, "failed")
        return _link_result(request, code)
    user_id, session_hash = flow.link_user, flow.link_session
    confirmed = "fresh_session" if flow.link_confirmed == "fresh_session" else "password"

    def link(session: Session) -> str:
        # Checked again under the write lock: the confirming session must still exist.
        row = session.get(UserSession, session_hash)
        user = session.get(User, user_id)
        if row is None or row.user_id != user_id or user is None:
            return "failed"
        return link_identity_to_user(
            session,
            user,
            provider=identity.issuer,
            subject=identity.subject,
            email=identity.email if identity.email_verified else None,
            confirmed=confirmed,
            clock=state.clock,
            host=client_host(request),
            user_agent=_user_agent(request),
        )

    try:
        status = await run_in_threadpool(run_immediate, state.session_factory, link)
    except OperationalError as exc:
        if not is_database_locked(exc):
            raise
        return _link_result(request, "busy")
    # "refused" (linked to someone else) is the generic failure: no oracle.
    known = {"linked", "already_linked", "one_per_provider"}
    return _link_result(request, status if status in known else "failed")


# Password confirmation before linking (``/login/link``, same-origin like ``/login``). Shared
# by OIDC and proxy sign-in, so it works when either is on; the pending link says which.


def _linking_available(request: Request) -> None:
    settings = _state(request).settings
    if not (settings.oidc_enabled or settings.proxy_auth_enabled):
        raise HTTPException(status_code=404)


def _pending(request: Request) -> PendingLink | None:
    state = _state(request)
    return PendingLink.unseal(
        state.oidc_link_key,
        request.cookies.get(oidc_link_cookie_name(state.settings)),
        now=state.clock.now(),
    )


def _link_page(
    request: Request,
    pending: PendingLink | None,
    *,
    error: str | None = None,
    message: str | None = None,
    status_code: int = 200,
) -> Response:
    """``error`` is a message key; ``message`` is already translated."""
    return render_template(
        request,
        "auth/link.html",
        context={
            "pending": pending,
            "error": message or (_t(request, error) if error else None),
            "expired": _t(request, LINK_EXPIRED) if pending is None else None,
            # The sign-in page offers every method that is on.
            "start_url": "/login",
        },
        status_code=status_code if pending is not None else 400,
    )


@router.get("/login/link", name="oidc_link")
async def link_form(request: Request) -> Response:
    _linking_available(request)
    return _link_page(request, _pending(request))


@router.post("/login/link")
async def link_submit(request: Request, password: FormStr = "") -> Response:
    _linking_available(request)
    state = _state(request)
    pending = _pending(request)
    if pending is None:
        return _link_page(request, None)
    if len(password) > MAX_PASSWORD_LENGTH:
        return _link_page(request, pending, error=LINK_FAILED, status_code=401)
    try:
        outcome = await link_identity_async(
            state.session_factory,
            state.hash_gate,
            pending,
            password,
            settings=state.settings,
            clock=state.clock,
            host=client_host(request),
            user_agent=_user_agent(request),
            existing_session_id=request.cookies.get(session_cookie_name(state.settings)),
        )
    except OperationalError as exc:
        if not is_database_locked(exc):
            raise
        response = _link_page(request, pending, error=BUSY, status_code=503)
        response.headers["Retry-After"] = BUSY_RETRY_AFTER
        return response
    if outcome.status == "throttled":
        # The account is already named by this page, so the wait can be shown; still 401.
        now = state.clock.now()
        response = _link_page(
            request,
            pending,
            message=throttled_message(outcome.throttled_until, now, request.state.locale),
            status_code=401,
        )
        wait = retry_after(outcome.throttled_until, now)
        if wait is not None:
            response.headers["Retry-After"] = wait
        return response
    if outcome.status != "linked" or outcome.session_id is None or outcome.expires_at is None:
        # Wrong password, linking turned off meanwhile: one message for both.
        return _link_page(request, pending, error=LINK_FAILED, status_code=401)
    response = signed_in_redirect(
        request, oidc_next(pending.next_path), outcome.session_id, outcome.expires_at
    )
    clear_cookie(response, state.settings, oidc_link_cookie_name(state.settings))
    return response
