"""Request-level security helpers: cookies, client address, redirects and CSRF.

CSRF checks run as a router-level dependency (``require_csrf``), never in middleware, so form
bodies are parsed once by FastAPI and never inside ``BaseHTTPMiddleware``.
"""

from __future__ import annotations

import ipaddress
from datetime import datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, unquote, urlsplit

from starlette.requests import Request
from starlette.responses import Response

from assetboard.branding import CLI_NAME
from assetboard.services import csrf
from assetboard.services.auth import WebSession

if TYPE_CHECKING:
    from assetboard.config import Settings

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
CSRF_FIELD = "csrf_token"
CSRF_HEADER = "x-csrf-token"
MAX_NEXT_LENGTH = 2048


class CsrfFailed(Exception):
    """The request's CSRF token was missing, malformed, expired or for another session."""


def is_https(settings: Settings) -> bool:
    return settings.base_url.lower().startswith("https://")


def _cookie_name(settings: Settings, purpose: str) -> str:
    # ``__Host-`` requires Secure, Path=/ and no Domain; browsers enforce it on https only.
    prefix = "__Host-" if is_https(settings) else ""
    return f"{prefix}{CLI_NAME}_{purpose}"


def session_cookie_name(settings: Settings) -> str:
    return _cookie_name(settings, "session")


def csrf_cookie_name(settings: Settings) -> str:
    return _cookie_name(settings, "csrf")


def _cookie_options(settings: Settings) -> dict[str, Any]:
    return {"path": "/", "secure": is_https(settings), "httponly": True, "samesite": "lax"}


def set_session_cookie(
    response: Response,
    settings: Settings,
    session_id: str,
    *,
    expires_at: datetime,
    now: datetime,
) -> None:
    """Max-Age follows the absolute expiry; the idle timeout is enforced on the server."""
    max_age = max(0, int((expires_at - now).total_seconds()))
    response.set_cookie(
        session_cookie_name(settings), session_id, max_age=max_age, **_cookie_options(settings)
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(session_cookie_name(settings), **_cookie_options(settings))


def clear_csrf_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(csrf_cookie_name(settings), **_cookie_options(settings))


# OIDC (F1.3, ADR-0014): the flow cookie (start -> callback) and the pending-link cookie
# (callback -> password confirmation). Both hold signed payloads that expire after 10 minutes.
OIDC_COOKIE_SECONDS = 600


def oidc_flow_cookie_name(settings: Settings) -> str:
    return _cookie_name(settings, "oidc_flow")


def oidc_link_cookie_name(settings: Settings) -> str:
    return _cookie_name(settings, "oidc_link")


def set_short_cookie(response: Response, settings: Settings, name: str, value: str) -> None:
    response.set_cookie(name, value, max_age=OIDC_COOKIE_SECONDS, **_cookie_options(settings))


def clear_cookie(response: Response, settings: Settings, name: str) -> None:
    response.delete_cookie(name, **_cookie_options(settings))


def client_host(request: Request) -> str | None:
    """The TCP peer as uvicorn reports it; forwarded headers are never read here.

    Uvicorn rewrites ``request.client`` from forwarded headers only for ``TRUSTED_PROXIES``.
    A missing or non-IP client shares the ``unknown`` throttle bucket; warn once per process.
    """
    host = request.client.host if request.client else None
    try:
        if host is not None:
            ipaddress.ip_address(host)
            return host
    except ValueError:
        pass
    state = request.app.state
    if not getattr(state, "unknown_client_warned", False):
        state.unknown_client_warned = True
        state.logger.warning(
            "client address unavailable or not an IP address; all such clients share one "
            "throttle bucket. Configure the reverse proxy and ASSETBOARD_TRUSTED_PROXIES."
        )
    return host


def safe_next(value: str | None, default: str = "/", *, max_length: int = MAX_NEXT_LENGTH) -> str:
    """Accept only a local absolute path (``/…``); anything else falls back to ``default``."""
    if not value or len(value) > max_length:
        return default
    decoded = unquote(value)
    for candidate in (value, decoded):
        if not candidate.startswith("/") or candidate.startswith("//"):
            return default
        if "\\" in candidate or any(ord(char) < 32 or ord(char) == 127 for char in candidate):
            return default
        parts = urlsplit(candidate)
        if parts.scheme or parts.netloc:
            return default
    return value


# Short enough that the OIDC flow and pending-link cookies always fit (their payloads also hold
# the issuer, subject and email). An invitation path is 51 characters.
MAX_OIDC_NEXT_LENGTH = 512


def oidc_next(value: str | None) -> str:
    """``safe_next`` for OIDC: a shorter cap and ASCII only, so the signed cookies always fit."""
    candidate = safe_next(value, max_length=MAX_OIDC_NEXT_LENGTH)
    return candidate if candidate.isascii() else "/"


def oidc_start_url(next_path: str = "") -> str:
    """The link behind "Continue with <provider>" (a link, never a form: ADR-0014)."""
    target = oidc_next(next_path)
    if target == "/":
        return "/auth/oidc/start"
    return "/auth/oidc/start?next=" + quote(target, safe="")


def web_session(request: Request) -> WebSession | None:
    return getattr(request.state, "web_session", None)


def _anonymous_secret(request: Request) -> str:
    """The anonymous CSRF cookie value, creating one for this response if needed."""
    existing = getattr(request.state, "new_csrf_secret", None)
    if existing:
        return existing
    cookie = request.cookies.get(csrf_cookie_name(request.app.state.settings))
    if cookie and 20 <= len(cookie) <= 128:
        return cookie
    secret = csrf.new_anonymous_secret()
    request.state.new_csrf_secret = secret
    return secret


def csrf_token(request: Request) -> str:
    """Template helper: the masked CSRF token for this request (fresh bytes every render)."""
    key: bytes = request.app.state.csrf_key
    signed_in = web_session(request)
    if signed_in is not None:
        raw = csrf.session_token(key, signed_in.session_hash)
    else:
        raw = csrf.anonymous_token(key, _anonymous_secret(request), request.app.state.clock.now())
    return csrf.mask(raw)


def apply_new_csrf_cookie(request: Request, response: Response) -> None:
    secret = getattr(request.state, "new_csrf_secret", None)
    if secret:
        settings = request.app.state.settings
        response.set_cookie(csrf_cookie_name(settings), secret, **_cookie_options(settings))


async def require_csrf(request: Request) -> None:
    """Router-level dependency for every state-changing route.

    The ``X-CSRF-Token`` header (HTMX) takes precedence over the ``csrf_token`` form field.
    """
    if request.method in SAFE_METHODS:
        return
    provided = request.headers.get(CSRF_HEADER)
    if provided is None:
        form = await request.form()
        field = form.get(CSRF_FIELD)
        provided = field if isinstance(field, str) else None
    token = csrf.unmask(provided)
    key: bytes = request.app.state.csrf_key
    signed_in = web_session(request)
    if signed_in is not None:
        valid = csrf.verify_session_token(key, signed_in.session_hash, token)
    else:
        cookie = request.cookies.get(csrf_cookie_name(request.app.state.settings))
        valid = csrf.verify_anonymous_token(key, cookie, token, request.app.state.clock.now())
    if not valid:
        raise CsrfFailed
