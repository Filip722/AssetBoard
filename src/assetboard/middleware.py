from __future__ import annotations

import logging
import re
import uuid
from time import perf_counter

from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from assetboard.i18n.locale import resolve_locale

_REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
SECURITY_HEADERS = {
    "content-security-policy": (
        "default-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'; "
        "object-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "font-src 'self'; connect-src 'self'"
    ),
    "x-content-type-options": "nosniff",
    "referrer-policy": "strict-origin-when-cross-origin",
    "x-frame-options": "DENY",
}
_SECURITY_HEADER_NAMES = frozenset(name.encode("ascii") for name in SECURITY_HEADERS)
# Pages whose URL can carry a secret send no Referer to other sites (ADR-0013): invitation pages
# (``/invite/<token>``), and sign-in and registration, whose ``next`` may be ``/invite/<token>``.
# "same-origin", not "no-referrer": under "no-referrer" browsers send ``Origin: null`` on form
# POSTs, which the Origin check (rightly) refuses.
SAME_ORIGIN_REFERRER_PREFIXES = ("/invite/",)
SAME_ORIGIN_REFERRER_PATHS = frozenset({"/login", "/register"})
_INVITE_TOKEN = re.compile(r"(/invite/)[^/?#]+")


def redact_path(path: str) -> str:
    """The request path as logged: invitation tokens are replaced with ``[redacted]``."""
    return _INVITE_TOKEN.sub(r"\1[redacted]", path)


def route_path(scope: Scope) -> str:
    """The request path without the ASGI ``root_path`` (for matching app routes)."""
    path: str = scope.get("path", "")
    root_path: str = scope.get("root_path", "")
    if root_path and path.startswith(root_path):
        path = path[len(root_path) :] or "/"
    return path


class RequestIdMiddleware:
    def __init__(self, app: ASGIApp, logger: logging.Logger) -> None:
        self.app = app
        self.logger = logger

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = self._request_id(scope)
        scope.setdefault("state", {})["request_id"] = request_id
        started_at = perf_counter()
        status_code = 500
        logged = False

        def log_request() -> None:
            nonlocal logged
            if logged:
                return
            logged = True
            if scope.get("path") == "/healthz":
                if status_code == 200:
                    return
                log_level = logging.WARNING
            else:
                log_level = logging.INFO
            duration_ms = (perf_counter() - started_at) * 1000
            # Only the path is logged, never the query string (it may carry ``next=/invite/…``).
            logged_path = redact_path(scope.get("path", ""))
            self.logger.log(
                log_level,
                "request_complete method=%s path=%s status_code=%d duration_ms=%.3f",
                scope.get("method", ""),
                logged_path,
                status_code,
                duration_ms,
                extra={
                    "request_id": request_id,
                    "method": scope.get("method", ""),
                    "path": logged_path,
                    "status_code": status_code,
                    "duration_ms": duration_ms,
                },
            )

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != b"x-request-id"
                ]
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message = {**message, "headers": headers}

            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                log_request()

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception:
            log_request()
            raise

    @staticmethod
    def _request_id(scope: Scope) -> str:
        for name, value in scope.get("headers", []):
            if name.lower() == b"x-request-id":
                try:
                    candidate = value.decode("ascii")
                except UnicodeDecodeError:
                    break
                if _REQUEST_ID_PATTERN.fullmatch(candidate):
                    return candidate
                break
        return str(uuid.uuid4())


class LocaleMiddleware:
    def __init__(self, app: ASGIApp, default_locale: str) -> None:
        self.app = app
        self.default_locale = default_locale

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            accept_language = headers.get(b"accept-language", b"").decode("latin-1")
            state = scope.setdefault("state", {})
            state["locale"] = resolve_locale(
                user_locale=state.get("user_locale"),
                workspace_locale=state.get("workspace_locale"),
                accept_language=accept_language,
                default_locale=self.default_locale,
            )
        await self.app(scope, receive, send)


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_security_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() not in _SECURITY_HEADER_NAMES
                ]
                response_headers.extend(
                    (key.encode("ascii"), value.encode("ascii"))
                    for key, value in SECURITY_HEADERS.items()
                )
                path = route_path(scope)
                if path in SAME_ORIGIN_REFERRER_PATHS or path.startswith(
                    SAME_ORIGIN_REFERRER_PREFIXES
                ):
                    response_headers = [
                        (name, b"same-origin" if name == b"referrer-policy" else value)
                        for name, value in response_headers
                    ]
                if not scope.get("path", "").startswith("/static/"):
                    response_headers = [
                        (name, value)
                        for name, value in response_headers
                        if name.lower() != b"cache-control"
                    ]
                    response_headers.append((b"cache-control", b"no-store"))
                    vary = next(
                        (value for name, value in response_headers if name.lower() == b"vary"),
                        b"",
                    )
                    vary_values = [value.strip() for value in vary.split(b",") if value.strip()]
                    if b"accept-language" not in {value.lower() for value in vary_values}:
                        vary_values.append(b"Accept-Language")
                    response_headers = [
                        (name, value) for name, value in response_headers if name.lower() != b"vary"
                    ]
                    response_headers.append((b"vary", b", ".join(vary_values)))
                message = {**message, "headers": response_headers}
            await send(message)

        await self.app(scope, receive, send_with_security_headers)


def add_security_headers(response: Response) -> None:
    headers = response.headers
    for name, value in SECURITY_HEADERS.items():
        headers[name] = value
