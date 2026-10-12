"""Pure ASGI middlewares that decide whether a request may reach the routes.

Order (outermost first, see ADR-0012): SecurityHeaders → Locale → RequestId →
OriginCheck → AccessGate → routes (CSRF dependency). The access gate answers before any route,
so for example a POST without a CSRF token gets the gate's 503 (schema not current) or 303
(setup open) rather than a CSRF 403. Neither middleware reads a request body.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote, urlsplit

from sqlalchemy.exc import OperationalError
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from assetboard.branding import APP_NAME, CLI_NAME
from assetboard.db.engine import is_database_locked, run_immediate
from assetboard.i18n import gettext
from assetboard.middleware import route_path
from assetboard.services.auth import (
    WebSession,
    read_web_session,
    revoke_session,
    setup_is_open,
    touch_session_best_effort,
)
from assetboard.services.proxy_auth import proxy_identity_owner
from assetboard.web.errors import error_response, is_htmx_fragment_request
from assetboard.web.security import (
    ProxyRequest,
    client_host,
    proxy_request,
    proxy_start_url,
    session_cookie_name,
)

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
ALWAYS_OPEN = frozenset({"/healthz", "/readyz"})
SIGNED_OUT_PATHS = frozenset(
    {"/login", "/register", "/setup", "/login/link", "/login/local", "/auth/proxy"}
)
# Invitation links work signed out: the invitee may still need to create an account. OIDC
# sign-in (start, callback and its failure pages) happens before there is a session.
SIGNED_OUT_PREFIXES = ("/invite/", "/auth/oidc/", "/auth/proxy/")
SAFE_NAVIGATION = frozenset({"GET", "HEAD"})


def _always_open(path: str) -> bool:
    return path in ALWAYS_OPEN or path.startswith("/static/")


def _redirect(request: Request, path: str) -> Response:
    location = f"{request.scope.get('root_path', '').rstrip('/')}{path}"
    if is_htmx_fragment_request(request):
        # A plain 303 would be followed inside the XHR; ask htmx to navigate instead.
        return Response(status_code=204, headers={"HX-Redirect": location})
    return Response(status_code=303, headers={"Location": location})


# Origin check


def _normalized_origin(value: str) -> str | None:
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    default_port = 443 if parts.scheme == "https" else 80
    return f"{parts.scheme}://{parts.hostname.lower()}:{port or default_port}"


def origin_allowed(headers: Headers, scheme: str, base_url: str) -> bool:
    """Same-origin check for state-changing requests.

    ``Sec-Fetch-Site: cross-site`` is always refused. Otherwise an ``Origin`` header, when sent,
    must equal the ``BASE_URL`` origin or the origin the request was addressed to (scheme +
    ``Host``), so LAN addresses and non-default ports work. "same-site" alone is not enough:
    a sibling subdomain is refused. Requests without ``Origin`` still need a CSRF token.
    """
    if headers.get("sec-fetch-site", "").strip().lower() == "cross-site":
        return False
    origin = headers.get("origin")
    if origin is None:
        return True
    normalized = _normalized_origin(origin)
    if normalized is None:  # includes the opaque "null" origin
        return False
    allowed = {_normalized_origin(base_url)}
    host = headers.get("host")
    if host:
        allowed.add(_normalized_origin(f"{scheme}://{host}"))
    return normalized in allowed


class OriginCheckMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("method", "GET") in UNSAFE_METHODS:
            settings = scope["app"].state.settings
            headers = Headers(scope=scope)
            if not origin_allowed(headers, scope.get("scheme", "http"), settings.base_url):
                request = Request(scope)
                locale = request.state.locale
                response = error_response(
                    request,
                    403,
                    gettext("Access denied", locale),
                    gettext("This request came from another site and was blocked.", locale),
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


# Access gate


def _schema_response(request: Request, schema_state: str | None) -> Response:
    locale = request.state.locale
    if schema_state == "behind":
        title = gettext("Database upgrade required", locale)
        message = gettext(
            "The database needs an upgrade. Ask the administrator to run %(command)s, then "
            "restart the application.",
            locale,
        ) % {"command": f"{CLI_NAME} migrate"}
    elif schema_state == "ahead":
        title = gettext("Newer database", locale)
        message = gettext(
            "This database was written by a newer version of %(app)s. Install that version "
            "or a later one, then restart the application.",
            locale,
        ) % {"app": APP_NAME}
    else:
        title = gettext("Starting up", locale)
        message = gettext("The application is starting. Try again in a moment.", locale)
    return error_response(request, 503, title, message, headers={"Retry-After": "30"})


async def _setup_open(app: Any) -> bool:
    state = app.state
    if getattr(state, "setup_closed", False):
        return False  # setup never reopens, so only "closed" is cached

    def check() -> bool:
        with state.session_factory() as session:
            return setup_is_open(session)

    is_open = await run_in_threadpool(check)
    if not is_open:
        state.setup_closed = True
    return is_open


def is_page_navigation(request: Request) -> bool:
    """A browser loading a page: ``Sec-Fetch-Dest: document`` or ``Sec-Fetch-Mode: navigate``.

    Clients that send no Fetch Metadata count when they accept ``text/html``.
    """
    headers = request.headers
    dest = headers.get("sec-fetch-dest")
    mode = headers.get("sec-fetch-mode")
    if dest is not None or mode is not None:
        return (dest or "").strip().lower() == "document" or (
            mode or ""
        ).strip().lower() == "navigate"
    return "text/html" in headers.get("accept", "").lower()


async def _load_session(request: Request, proxy: ProxyRequest | None) -> WebSession | None:
    """The signed-in session, if any. Proxy sessions are bound to the proxy (ADR-0015).

    A ``proxy`` session counts only while the trusted proxy presents the subject linked to the
    session's user. Without that (proxy auth off, an untrusted peer, missing, repeated or
    invalid headers) the request is signed out and the session stays. A different subject (or
    one no longer linked) ends the session on GET and HEAD; other methods only count as signed
    out. Password and OIDC sessions are never affected by proxy headers.
    """
    state = request.app.state
    session_id = request.cookies.get(session_cookie_name(state.settings))
    if not session_id:
        return None
    subject = proxy.subject if proxy is not None else None

    def read() -> tuple[WebSession | None, bool]:
        """The session, and whether it is a proxy session now presented by someone else."""
        with state.session_factory() as session:
            current = read_web_session(
                session, session_id, settings=state.settings, clock=state.clock
            )
            if current is None or current.auth_method != "proxy":
                return current, False
            if subject is None:
                return None, False
            if proxy_identity_owner(session, subject) == current.user_id:
                return current, False
            return None, True

    current, other_subject = await run_in_threadpool(read)
    if other_subject and request.method in SAFE_NAVIGATION:
        # Best effort: the request is already signed out, and the next navigation retries
        # (``/auth/proxy`` also replaces the old session when it signs the new user in).
        try:
            await run_in_threadpool(
                run_immediate,
                state.session_factory,
                lambda session: revoke_session(
                    session,
                    session_id,
                    clock=state.clock,
                    host=client_host(request),
                    user_agent=request.headers.get("user-agent"),
                ),
            )
        except OperationalError as exc:
            if not is_database_locked(exc):
                raise
            state.logger.warning("proxy session not ended yet: the database is busy")
        else:
            state.logger.info("proxy session ended: the proxy presents a different user")
    if current is not None and current.needs_touch:
        # Best effort with a short lock wait: a busy database never fails the request.
        await run_in_threadpool(
            touch_session_best_effort, state.touch_factory, current.session_hash, state.clock
        )
    return current


class AccessGateMiddleware:
    """Schema state, then first-run setup, then sign-in."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = route_path(scope)
        if _always_open(path):
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        app = scope["app"]
        schema_state = getattr(app.state, "schema_state", None)
        if schema_state != "current":
            await _schema_response(request, schema_state)(scope, receive, send)
            return
        if await _setup_open(app):
            if path != "/setup":
                await _redirect(request, "/setup")(scope, receive, send)
                return
            await self.app(scope, receive, send)
            return
        proxy = proxy_request(request)
        current = await _load_session(request, proxy)
        scope.setdefault("state", {})["web_session"] = current
        if (
            current is None
            and path not in SIGNED_OUT_PATHS
            and not path.startswith(SIGNED_OUT_PREFIXES)
        ):
            if (
                proxy is not None
                and proxy.subject is not None
                and request.method in SAFE_NAVIGATION
                and (is_htmx_fragment_request(request) or is_page_navigation(request))
            ):
                # The trusted proxy vouches for someone: sign in through /auth/proxy. Its
                # redirect back goes through /auth/proxy/continue, so a browser that drops the
                # session cookie ends on a page instead of looping. Other requests (monitors,
                # scripts, images) get the plain /login redirect and write nothing.
                if is_htmx_fragment_request(request):
                    target = proxy_start_url()
                else:
                    original = path + (f"?{request.url.query}" if request.url.query else "")
                    target = proxy_start_url(original)
                await _redirect(request, target)(scope, receive, send)
                return
            target = "/login"
            # ``next`` only for full-page GET navigations: an HTMX fragment's path is not a page
            # to return to, so htmx is sent to plain /login.
            if scope.get("method") == "GET" and not is_htmx_fragment_request(request):
                original = path + (f"?{request.url.query}" if request.url.query else "")
                target += "?next=" + quote(original, safe="")
            await _redirect(request, target)(scope, receive, send)
            return
        await self.app(scope, receive, send)
