"""Setup, sign-in, registration and account security pages.

Every state-changing route is protected by the router-level ``require_csrf`` dependency.
Field lengths are checked here, before any hashing or database work.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool
from starlette.responses import RedirectResponse, Response

from assetboard.db.engine import is_database_locked, run_immediate
from assetboard.db.models.identity import User
from assetboard.domain.identity import MAX_EMAIL_LENGTH, InvalidEmail
from assetboard.i18n import gettext
from assetboard.services.auth import (
    MAX_NAME_LENGTH,
    MAX_PASSWORD_LENGTH,
    AuthError,
    RegistrationDisabled,
    SetupAlreadyCompleted,
    WebSession,
    authenticate_user_async,
    change_password_async,
    create_session_for,
    register_user_async,
    revoke_other_sessions,
    revoke_session,
    run_initial_setup_async,
    session_expiry,
    setup_is_open,
    user_has_password,
    verify_setup_token,
)
from assetboard.services.profile import ConfirmMethod
from assetboard.services.workspace_options import (
    country_names,
    currency_codes,
    locale_codes,
    timezone_names,
)
from assetboard.web.confirm import confirm_view, refusal, run_confirmed
from assetboard.web.errors import error_response, is_htmx_fragment_request
from assetboard.web.rendering import render_template
from assetboard.web.security import (
    clear_csrf_cookie,
    clear_session_cookie,
    client_host,
    oidc_start_url,
    proxy_request,
    proxy_start_url,
    require_csrf,
    safe_next,
    session_cookie_name,
    set_session_cookie,
    web_session,
)
from assetboard.web.throttle import retry_after, throttled_message

router = APIRouter(dependencies=[Depends(require_csrf)])

MAX_SETUP_TOKEN_LENGTH = 256
FormStr = Annotated[str, Form()]


def _(message: str) -> str:
    """Marks a message for extraction; it is translated when rendered."""
    return message


ERROR_MESSAGES = {
    "invalid_email": _("Enter a valid email address."),
    "password_too_short": _("Use at least 8 characters for the password."),
    "password_too_long": _("Use at most 1,024 characters for the password."),
    "password_too_common": _("This password is too common. Choose another one."),
    "password_matches_identity": _(
        "The password must differ from your email name and your display name."
    ),
    "invalid_display_name": _(
        "Enter a display name of 1 to 120 characters, without control or invisible characters."
    ),
    "invalid_workspace_name": _("Enter a workspace name of 1 to 120 characters."),
    "invalid_workspace_settings": _(
        "Choose a supported currency, country, time zone and language."
    ),
    "too_long": _("This value is too long."),
    "setup_token": _("The setup token is not correct."),
    "password_mismatch": _("The new passwords do not match."),
    "password_unchanged": _("Choose a new password that differs from the current one."),
    "current_password_invalid": _("The current password is not correct."),
    "invalid": _("Check the details and try again."),
}
ERROR_FIELDS = {
    "invalid_email": "email",
    "password_too_short": "password",
    "password_too_long": "password",
    "password_too_common": "password",
    "password_matches_identity": "password",
    "invalid_display_name": "display_name",
    "invalid_workspace_name": "workspace_name",
    "invalid_workspace_settings": "base_currency",
}
LOGIN_FAILED = _("The email or password is not correct.")
SETUP_RACE = _("Setup was already completed, or is being completed. Reload to continue.")
LOCALE_NAMES = {"en": "English", "pl": "Polski"}


def _t(request: Request, message: str) -> str:
    return gettext(message, request.state.locale)


def too_long(limits: dict[str, tuple[str, int]]) -> str | None:
    for field, (value, limit) in limits.items():
        if len(value) > limit:
            return field
    return None


def _state(request: Request) -> Any:
    return request.app.state


def _user_agent(request: Request) -> str | None:
    return request.headers.get("user-agent")


def signed_in_redirect(
    request: Request, target: str, session_id: str, expires_at: datetime
) -> Response:
    state = _state(request)
    response = RedirectResponse(target, status_code=303)
    set_session_cookie(
        response, state.settings, session_id, expires_at=expires_at, now=state.clock.now()
    )
    # The anonymous CSRF secret belongs to the signed-out page; rotate it away.
    clear_csrf_cookie(response, state.settings)
    return response


def _require_session(request: Request) -> WebSession:
    current = web_session(request)
    if current is None:  # the gate redirects first; this guards direct use
        raise HTTPException(status_code=404)
    return current


# First-run setup


def _setup_race(request: Request) -> Response:
    return error_response(
        request, 409, _t(request, _("Setup in progress")), _t(request, SETUP_RACE)
    )


async def _setup_is_open(request: Request) -> bool:
    state = _state(request)

    def check() -> bool:
        with state.session_factory() as session:
            return setup_is_open(session)

    return await run_in_threadpool(check)


def _setup_page(
    request: Request,
    *,
    values: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    settings = _state(request).settings
    defaults = {
        "email": "",
        "display_name": "",
        "workspace_name": "",
        "base_currency": settings.default_currency,
        "country_code": "",
        "timezone": "UTC",
        "workspace_locale": request.state.locale,
    }
    return render_template(
        request,
        "auth/setup.html",
        context={
            "values": defaults | (values or {}),
            "errors": {field: _t(request, message) for field, message in (errors or {}).items()},
            "currency_options": [(code, code) for code in currency_codes()],
            "country_options": list(country_names(request.state.locale).items()),
            "timezone_options": [(zone, zone) for zone in timezone_names()],
            "locales": [(code, LOCALE_NAMES.get(code, code)) for code in locale_codes()],
        },
        status_code=status_code,
    )


@router.get("/setup", name="setup")
async def setup_form(request: Request) -> Response:
    if not await _setup_is_open(request):
        raise HTTPException(status_code=404)
    return _setup_page(request)


@router.post("/setup")
async def setup_submit(
    request: Request,
    setup_token: FormStr = "",
    email: FormStr = "",
    display_name: FormStr = "",
    password: FormStr = "",
    workspace_name: FormStr = "",
    base_currency: FormStr = "",
    country_code: FormStr = "",
    timezone: FormStr = "",
    workspace_locale: FormStr = "",
) -> Response:
    state = _state(request)
    if not await _setup_is_open(request):
        raise HTTPException(status_code=404)
    values = {
        "email": email,
        "display_name": display_name,
        "workspace_name": workspace_name,
        "base_currency": base_currency,
        "country_code": country_code,
        "timezone": timezone,
        "workspace_locale": workspace_locale,
    }
    long_field = too_long(
        {
            "setup_token": (setup_token, MAX_SETUP_TOKEN_LENGTH),
            "email": (email, MAX_EMAIL_LENGTH),
            "display_name": (display_name, MAX_NAME_LENGTH),
            "workspace_name": (workspace_name, MAX_NAME_LENGTH),
            "password": (password, MAX_PASSWORD_LENGTH),
        }
    )
    if long_field:
        return _setup_page(
            request,
            values=values,
            errors={long_field: ERROR_MESSAGES["too_long"]},
            status_code=400,
        )
    expected = state.setup_token
    host = client_host(request)
    try:
        token_ok = expected is not None and await run_in_threadpool(
            run_immediate,
            state.session_factory,
            lambda session: verify_setup_token(
                session,
                setup_token,
                expected,
                clock=state.clock,
                settings=state.settings,
                host=host,
            ),
        )
    except OperationalError as exc:
        if not is_database_locked(exc):
            raise
        # Another setup holds the write lock: it is being completed right now.
        return _setup_race(request)
    if not token_ok:
        return _setup_page(
            request,
            values=values,
            errors={"setup_token": ERROR_MESSAGES["setup_token"]},
            status_code=400,
        )
    try:
        user, _workspace = await run_initial_setup_async(
            state.session_factory,
            state.hash_gate,
            email=email,
            display_name=display_name,
            password=password,
            workspace_name=workspace_name,
            settings=state.settings,
            clock=state.clock,
            base_currency=base_currency or None,
            country_code=country_code or None,
            timezone=timezone or None,
            workspace_locale=workspace_locale or None,
            # The suggested categories are named in the language the page is shown in.
            seed_locale=request.state.locale,
            host=host,
            user_agent=_user_agent(request),
        )
    except SetupAlreadyCompleted:
        return _setup_race(request)
    except (AuthError, InvalidEmail) as exc:
        code = getattr(exc, "code", "")
        return _setup_page(
            request,
            values=values,
            errors={
                ERROR_FIELDS.get(code, "email"): ERROR_MESSAGES.get(code, ERROR_MESSAGES["invalid"])
            },
            status_code=400,
        )
    session_id, expires_at = await run_in_threadpool(
        lambda: create_session_for(
            state.session_factory,
            user.id,
            settings=state.settings,
            clock=state.clock,
            host=host,
            user_agent=_user_agent(request),
        )
    )
    state.setup_token = None
    return signed_in_redirect(request, "/", session_id, expires_at)


# Sign in and out


def _login_error(request: Request, *, error: bool, throttled_until: datetime | None) -> str | None:
    """Throttled only when a bucket refused the attempt; never a hint that an account exists."""
    if throttled_until is not None:
        now = _state(request).clock.now()
        return throttled_message(throttled_until, now, request.state.locale)
    return _t(request, LOGIN_FAILED) if error else None


def _login_page(
    request: Request,
    *,
    email: str = "",
    next_path: str = "",
    error: bool = False,
    throttled_until: datetime | None = None,
    status: int = 200,
    break_glass: bool = False,
) -> Response:
    settings = _state(request).settings
    # Break-glass shows only the password form; OIDC_ONLY shows only the provider's button.
    oidc = settings.oidc_enabled and not break_glass
    proxy = proxy_request(request)
    proxy_user = proxy.subject if proxy is not None and not break_glass else None
    return render_template(
        request,
        "auth/login.html",
        context={
            "email": email,
            "next_path": safe_next(next_path, default=""),
            "error": _login_error(request, error=error, throttled_until=throttled_until),
            "registration_enabled": settings.allow_registration and not break_glass,
            "password_login": break_glass or not settings.oidc_only,
            "form_action": "login_local" if break_glass else "login",
            "oidc_provider": settings.oidc_provider_name if oidc else None,
            "oidc_start_url": oidc_start_url(safe_next(next_path, default="")) if oidc else None,
            # Shown only when the trusted proxy presents a user right now (ADR-0015).
            "proxy_user": proxy_user,
            "proxy_start_url": proxy_start_url(safe_next(next_path, default=""))
            if proxy_user
            else None,
        },
        status_code=status,
    )


@router.get("/login", name="login")
async def login_form(request: Request, next: str = "") -> Response:  # noqa: A002
    if web_session(request) is not None:
        return RedirectResponse(safe_next(next), status_code=303)
    return _login_page(request, next_path=next)


async def _password_sign_in(
    request: Request, email: str, password: str, next_path: str, *, break_glass: bool
) -> Response:
    state = _state(request)
    if len(email) > MAX_EMAIL_LENGTH or len(password) > MAX_PASSWORD_LENGTH:
        # Cannot be valid; same response as any failed login, without hashing or queries.
        return _login_page(
            request,
            email=email[:MAX_EMAIL_LENGTH],
            next_path=next_path,
            error=True,
            status=401,
            break_glass=break_glass,
        )
    existing = request.cookies.get(session_cookie_name(state.settings))
    outcome = await authenticate_user_async(
        state.session_factory,
        state.hash_gate,
        email=email,
        password=password,
        settings=state.settings,
        clock=state.clock,
        host=client_host(request),
        user_agent=_user_agent(request),
        existing_session_id=existing,
        require_instance_admin=break_glass,
    )
    if not outcome.succeeded or outcome.session_id is None:
        # Throttled attempts keep the 401 that log-based tools count (ADR-0012), plus a
        # Retry-After; the page says how long to wait.
        response = _login_page(
            request,
            email=email,
            next_path=next_path,
            error=True,
            throttled_until=outcome.throttled_until,
            status=401,
            break_glass=break_glass,
        )
        wait = retry_after(outcome.throttled_until, state.clock.now())
        if wait is not None:
            response.headers["Retry-After"] = wait
        return response
    session_id = outcome.session_id
    expires_at = await run_in_threadpool(session_expiry, state.session_factory, session_id)
    if expires_at is None:
        raise RuntimeError("Session disappeared right after login")
    return signed_in_redirect(request, safe_next(next_path), session_id, expires_at)


@router.post("/login")
async def login_submit(
    request: Request,
    email: FormStr = "",
    password: FormStr = "",
    next: FormStr = "",  # noqa: A002
) -> Response:
    if _state(request).settings.oidc_only:
        raise HTTPException(status_code=404)
    return await _password_sign_in(request, email, password, next, break_glass=False)


# Break-glass password sign-in for instance administrators under OIDC_ONLY (ADR-0014). Not
# linked from any page; same throttle, dummy verify and generic error as /login.


def _require_break_glass(request: Request) -> None:
    settings = _state(request).settings
    if not (settings.oidc_only and settings.allow_local_admin_login):
        raise HTTPException(status_code=404)


@router.get("/login/local", name="login_local")
async def login_local_form(request: Request, next: str = "") -> Response:  # noqa: A002
    _require_break_glass(request)
    if web_session(request) is not None:
        return RedirectResponse(safe_next(next), status_code=303)
    return _login_page(request, next_path=next, break_glass=True)


@router.post("/login/local")
async def login_local_submit(
    request: Request,
    email: FormStr = "",
    password: FormStr = "",
    next: FormStr = "",  # noqa: A002
) -> Response:
    _require_break_glass(request)
    return await _password_sign_in(request, email, password, next, break_glass=True)


@router.post("/logout", name="logout")
async def logout(request: Request) -> Response:
    state = _state(request)
    session_id = request.cookies.get(session_cookie_name(state.settings))
    current = web_session(request)
    # RP-initiated logout (ADR-0014): only for sessions that started at the provider.
    provider_logout = (
        state.oidc is not None
        and state.settings.oidc_rp_logout
        and current is not None
        and current.auth_method == "oidc"
    )
    if session_id:
        await run_in_threadpool(
            run_immediate,
            state.session_factory,
            lambda session: revoke_session(
                session,
                session_id,
                clock=state.clock,
                host=client_host(request),
                user_agent=_user_agent(request),
            ),
        )
    # Computed only after the local session is gone: a slow or failing provider never keeps
    # anyone signed in here.
    provider_logout_url: str | None = None
    if provider_logout and state.oidc is not None:
        provider_logout_url = await run_in_threadpool(state.oidc.end_session_url)
    if provider_logout_url is not None:
        # A page, not a redirect: Chromium applies CSP form-action to redirects after a form
        # POST, and the provider is another origin. The local session is already gone.
        response = render_template(
            request,
            "auth/oidc_logout.html",
            context={
                "provider": state.settings.oidc_provider_name,
                "logout_url": provider_logout_url,
            },
        )
    else:
        response = RedirectResponse("/login", status_code=303)
    clear_session_cookie(response, state.settings)
    return response


# Registration


def _registration_enabled(request: Request) -> None:
    if not _state(request).settings.allow_registration:
        raise HTTPException(status_code=404)


def _register_page(
    request: Request,
    *,
    values: dict[str, str] | None = None,
    errors: dict[str, str] | None = None,
    notice: str | None = None,
    status_code: int = 200,
) -> Response:
    return render_template(
        request,
        "auth/register.html",
        context={
            "values": {"email": "", "display_name": "", "workspace_name": ""} | (values or {}),
            "errors": {field: _t(request, message) for field, message in (errors or {}).items()},
            "notice": _t(request, notice) if notice else None,
        },
        status_code=status_code,
    )


@router.get("/register", name="register")
async def register_form(request: Request) -> Response:
    _registration_enabled(request)
    if web_session(request) is not None:
        return RedirectResponse("/", status_code=303)
    return _register_page(request)


@router.post("/register")
async def register_submit(
    request: Request,
    email: FormStr = "",
    display_name: FormStr = "",
    password: FormStr = "",
    workspace_name: FormStr = "",
) -> Response:
    _registration_enabled(request)
    state = _state(request)
    values = {"email": email, "display_name": display_name, "workspace_name": workspace_name}
    long_field = too_long(
        {
            "email": (email, MAX_EMAIL_LENGTH),
            "display_name": (display_name, MAX_NAME_LENGTH),
            "workspace_name": (workspace_name, MAX_NAME_LENGTH),
            "password": (password, MAX_PASSWORD_LENGTH),
        }
    )
    if long_field:
        return _register_page(
            request, values=values, errors={long_field: ERROR_MESSAGES["too_long"]}, status_code=400
        )
    host = client_host(request)
    try:
        outcome = await register_user_async(
            state.session_factory,
            state.hash_gate,
            email=email,
            display_name=display_name,
            password=password,
            workspace_name=workspace_name,
            settings=state.settings,
            clock=state.clock,
            host=host,
            user_agent=_user_agent(request),
            workspace_locale=request.state.locale,
            seed_locale=request.state.locale,
        )
    except RegistrationDisabled as exc:
        raise HTTPException(status_code=404) from exc
    if outcome.status == "throttled":
        return _register_page(
            request,
            values=values,
            notice=_("Too many registration attempts from your network. Try again later."),
            status_code=429,
        )
    if outcome.status == "rejected":
        return _register_page(
            request,
            values=values,
            errors={"email": _("This email address cannot be registered.")},
            status_code=400,
        )
    if outcome.status == "invalid" or outcome.user is None:
        code = outcome.error.code if outcome.error else ""
        return _register_page(
            request,
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
            user_agent=_user_agent(request),
        )
    )
    return signed_in_redirect(request, "/", session_id, expires_at)


# Account security


NO_PASSWORD = _(
    "This account has no password: it signs in with your identity provider. An administrator "
    "can set one with the reset-password command."
)


async def _password_page(
    request: Request,
    *,
    errors: dict[str, str] | None = None,
    translated_errors: dict[str, str] | None = None,
    notice: str | None = None,
    status_code: int = 200,
) -> Response:
    """``errors`` holds message keys (translated here); ``translated_errors`` and ``notice`` are
    already in the request's language (for example a message with the throttle wait)."""
    current = _require_session(request)
    has_password = await run_in_threadpool(
        user_has_password, _state(request).session_factory, current.user_id
    )
    return render_template(
        request,
        "auth/password.html",
        context={
            "errors": {
                **{field: _t(request, message) for field, message in (errors or {}).items()},
                **(translated_errors or {}),
            },
            "notice": notice,
            "no_password": None if has_password else _t(request, NO_PASSWORD),
            "confirm": await confirm_view(request, current),
        },
        status_code=status_code,
    )


@router.get("/account/password", name="account_password")
async def password_form(request: Request) -> Response:
    _require_session(request)
    return await _password_page(request)


@router.post("/account/password")
async def password_submit(
    request: Request,
    current_password: FormStr = "",
    new_password: FormStr = "",
    confirm_password: FormStr = "",
) -> Response:
    current = _require_session(request)
    state = _state(request)
    long_field = too_long(
        {
            "current_password": (current_password, MAX_PASSWORD_LENGTH),
            "new_password": (new_password, MAX_PASSWORD_LENGTH),
            "confirm_password": (confirm_password, MAX_PASSWORD_LENGTH),
        }
    )
    if long_field:
        return await _password_page(
            request, errors={long_field: ERROR_MESSAGES["too_long"]}, status_code=400
        )
    if new_password != confirm_password:
        return await _password_page(
            request,
            errors={"confirm_password": ERROR_MESSAGES["password_mismatch"]},
            status_code=400,
        )
    session_id = request.cookies.get(session_cookie_name(state.settings))
    outcome = await change_password_async(
        state.session_factory,
        state.hash_gate,
        user_id=current.user_id,
        current_password=current_password,
        new_password=new_password,
        settings=state.settings,
        clock=state.clock,
        current_session_id=session_id,
        host=client_host(request),
        user_agent=_user_agent(request),
    )
    if outcome.status == "changed":
        notice = _t(request, _("Your password was changed. Other devices were signed out."))
        return await _password_page(request, notice=notice)
    if outcome.status == "no_password":
        return await _password_page(request, status_code=400)
    if outcome.status == "throttled":
        now = state.clock.now()
        response = await _password_page(
            request,
            translated_errors={
                "current_password": throttled_message(
                    outcome.throttled_until, now, request.state.locale
                )
            },
            status_code=429,
        )
        wait = retry_after(outcome.throttled_until, now)
        if wait is not None:
            response.headers["Retry-After"] = wait
        return response
    if outcome.status == "invalid_new":
        code = outcome.error.code if outcome.error else ""
        return await _password_page(
            request,
            errors={"new_password": ERROR_MESSAGES.get(code, ERROR_MESSAGES["invalid"])},
            status_code=400,
        )
    return await _password_page(
        request,
        errors={"current_password": ERROR_MESSAGES["current_password_invalid"]},
        status_code=400,
    )


@router.post("/account/logout-others", name="logout_others")
async def logout_others(request: Request, confirm_password: FormStr = "") -> Response:
    current = _require_session(request)
    state = _state(request)
    session_id = request.cookies.get(session_cookie_name(state.settings)) or ""

    def revoke(session: Session, user: User, _method: ConfirmMethod) -> int:
        return revoke_other_sessions(
            session,
            user.id,
            session_id,
            clock=state.clock,
            host=client_host(request),
            user_agent=_user_agent(request),
        )

    # Sensitive (F1.5): confirmed like every profile action.
    outcome = await run_confirmed(
        request, current, purpose="logout_others", password=confirm_password, action=revoke
    )
    fragment = is_htmx_fragment_request(request)
    if outcome.status != "confirmed" or outcome.value is None:
        refused = refusal(request, outcome)
        if fragment:
            response = render_template(
                request,
                "auth/notice_fragment.html",
                context={"notice": refused.message, "failed": True},
                status_code=refused.status_code,
            )
        else:
            response = await _password_page(
                request,
                translated_errors={"confirm": refused.message},
                status_code=refused.status_code,
            )
        if refused.retry_after is not None:
            response.headers["Retry-After"] = refused.retry_after
        return response
    notice = gettext("Other sessions signed out: %(count)d.", request.state.locale) % {
        "count": outcome.value
    }
    if fragment:
        return render_template(request, "auth/notice_fragment.html", context={"notice": notice})
    return await _password_page(request, notice=notice)
