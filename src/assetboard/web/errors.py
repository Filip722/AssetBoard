"""Error responses as full pages or, for HTMX requests, as toasts.

HTMX does not swap 4xx/5xx responses by default. ``base.html`` configures ``responseHandling``
to swap them (marked as errors), and these responses retarget the swap into
``#toast-container`` with ``HX-Retarget``/``HX-Reswap``, so a failed HTMX request shows a toast
instead of replacing the page fragment.
"""

from __future__ import annotations

from collections.abc import Mapping

from starlette.requests import Request
from starlette.responses import Response

from assetboard.branding import APP_NAME
from assetboard.services.hash_gate import RETRY_AFTER_SECONDS
from assetboard.web.rendering import render_template

# Retry-After for every "server busy" 503 (hashing limit, locked database).
BUSY_RETRY_AFTER = str(RETRY_AFTER_SECONDS)


def is_htmx_fragment_request(request: Request) -> bool:
    headers = request.headers
    return (
        headers.get("hx-request", "").lower() == "true"
        and headers.get("hx-boosted", "").lower() != "true"
        and headers.get("hx-history-restore-request", "").lower() != "true"
    )


def add_error_response_headers(response: Response, *, is_fragment: bool) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Vary"] = "Accept-Language, HX-Request"
    if is_fragment:
        response.headers["HX-Retarget"] = "#toast-container"
        response.headers["HX-Reswap"] = "beforeend"


def error_response(
    request: Request,
    status_code: int,
    title: str,
    message: str,
    *,
    headers: Mapping[str, str] | None = None,
) -> Response:
    is_fragment = is_htmx_fragment_request(request)
    response = render_template(
        request,
        "errors/fragment.html" if is_fragment else "errors/page.html",
        context={
            "app_name": APP_NAME,
            "title": title,
            "message": message,
            "status_code": status_code,
        },
        status_code=status_code,
    )
    if headers:
        response.headers.update(headers)
    add_error_response_headers(response, is_fragment=is_fragment)
    return response
