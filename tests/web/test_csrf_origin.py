"""CSRF tokens (forms and HTMX) and the Origin check."""

from __future__ import annotations

import re
from datetime import timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from assetboard.middleware import SECURITY_HEADERS
from assetboard.services import csrf
from assetboard.web.security import require_csrf

if TYPE_CHECKING:
    from conftest import WebHarness

TEMPLATES = Path(__file__).parents[2] / "src/assetboard/web/templates"
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def _api_routes(routes: list[object]) -> list[APIRoute]:
    """All API routes, including those of included routers (FastAPI wraps them lazily)."""
    found: list[APIRoute] = []
    for route in routes:
        if isinstance(route, APIRoute):
            found.append(route)
        elif hasattr(route, "original_router"):
            found.extend(_api_routes(route.original_router.routes))  # type: ignore[attr-defined]
        elif hasattr(route, "routes"):
            found.extend(_api_routes(route.routes))  # type: ignore[attr-defined]
    return found


def _unsafe_routes(app: object) -> list[tuple[str, str]]:
    routes = _api_routes(app.routes)  # type: ignore[attr-defined]
    return [
        (method, route.path)
        for route in routes
        for method in sorted((route.methods or set()) & UNSAFE)
    ]


def test_every_state_changing_route_requires_a_valid_csrf_token(
    signed_in: TestClient, web: WebHarness
) -> None:
    routes = _unsafe_routes(signed_in.app)
    assert {path for _, path in routes} >= {
        "/setup",
        "/login",
        "/logout",
        "/register",
        "/account/password",
        "/account/logout-others",
        "/workspace/switch",
        "/workspace/settings",
        "/workspace/members/{membership_id}/role",
        "/workspace/members/{membership_id}/remove",
        "/workspace/invitations",
        "/workspace/invitations/{invitation_id}/revoke",
        "/invite/{token}/accept",
        "/invite/{token}/register",
    }
    other_session_token = csrf.mask(csrf.session_token(signed_in.app.state.csrf_key, "f" * 64))  # type: ignore[attr-defined]
    for method, path in routes:
        for token in (None, "not-a-token", other_session_token):
            data = {} if token is None else {"csrf_token": token}
            response = signed_in.request(method, path, data=data, follow_redirects=False)
            assert response.status_code == 403, (method, path, token, response.status_code)
            assert "This form has expired" in response.text
    assert signed_in.get("/").status_code == 200  # still signed in


def test_every_state_changing_route_declares_the_csrf_dependency(signed_in: TestClient) -> None:
    """Structural twin of the route walk: a route without ``require_csrf`` fails here by name."""
    missing = [
        f"{method} {route.path}"
        for route in _api_routes(signed_in.app.routes)  # type: ignore[attr-defined]
        for method in sorted((route.methods or set()) & UNSAFE)
        if not any(dependency.dependency is require_csrf for dependency in route.dependencies)
    ]
    assert missing == []


def test_signed_out_forms_require_the_anonymous_token_and_its_cookie(web: WebHarness) -> None:
    app = web.app(allow_registration=True)
    with TestClient(app) as client:
        web.complete_setup(app)
        expected = {
            "/login",
            "/register",
            "/setup",
            "/invite/{token}/accept",
            "/invite/{token}/register",
        }
        signed_out_routes = {path for _, path in _unsafe_routes(app)} & expected
        assert signed_out_routes == expected
        with TestClient(app) as other_browser:
            foreign_token = web.csrf(other_browser.get("/login").text)
        for path in sorted(signed_out_routes):
            client.cookies.clear()
            cases = {
                "no cookie, no token": {},
                "no cookie, another browser's token": {"csrf_token": foreign_token},
            }
            for case, data in cases.items():
                response = client.post(path, data=data, follow_redirects=False)
                assert response.status_code == 403, (path, case)
            web.csrf(client.get("/login").text)  # now this browser has its own cookie
            for case, data in {
                "cookie, no token": {},
                "cookie, wrong token": {"csrf_token": "wrong-token"},
                "cookie, another browser's token": {"csrf_token": foreign_token},
            }.items():
                response = client.post(path, data=data, follow_redirects=False)
                assert response.status_code == 403, (path, case)
                assert "This form has expired" in response.text


def test_htmx_requests_send_the_meta_tag_token_in_a_header(
    signed_in: TestClient, web: WebHarness
) -> None:
    page = signed_in.get("/account/password")
    meta = re.search(r'<meta name="csrf-token" content="([^"]+)"', page.text)
    assert meta is not None
    meta = meta.group(1)
    ok = signed_in.post(
        "/account/logout-others", headers={"HX-Request": "true", "X-CSRF-Token": meta}
    )
    assert ok.status_code == 200
    # The header wins over the form field: a bad header fails even with a good field.
    bad = signed_in.post(
        "/account/logout-others",
        data={"csrf_token": web.csrf(page.text)},
        headers={"HX-Request": "true", "X-CSRF-Token": "forged"},
    )
    assert bad.status_code == 403
    assert bad.headers["hx-retarget"] == "#toast-container"
    assert '<div data-toast role="alert"' in bad.text


def test_tokens_are_masked_differently_in_every_response(
    signed_in: TestClient, web: WebHarness
) -> None:
    first = web.csrf(signed_in.get("/account/password").text)
    second = web.csrf(signed_in.get("/account/password").text)
    assert first != second
    for token in (first, second):
        response = signed_in.post(
            "/account/logout-others", headers={"HX-Request": "true", "X-CSRF-Token": token}
        )
        assert response.status_code == 200
    with TestClient(signed_in.app) as signed_out:
        a, b = (web.csrf(signed_out.get("/login").text) for _ in range(2))
    assert a != b and csrf.unmask(a) is not None


def test_anonymous_tokens_expire_and_are_bound_to_their_cookie(web: WebHarness) -> None:
    app = web.app()
    with TestClient(app) as client:
        web.complete_setup(app)
        client.get("/login")
        secret = client.cookies["assetboard_csrf"]
        old = csrf.mask(
            csrf.anonymous_token(
                app.state.csrf_key, secret, app.state.clock.now() - timedelta(hours=12, seconds=1)
            )
        )
        login = {"email": web.admin_email, "password": web.admin_password}
        assert client.post("/login", data=login | {"csrf_token": old}).status_code == 403
        foreign = csrf.mask(
            csrf.anonymous_token(
                app.state.csrf_key, "another-browser-secret-123", app.state.clock.now()
            )
        )
        assert client.post("/login", data=login | {"csrf_token": foreign}).status_code == 403
        fresh = web.csrf(client.get("/login").text)
        response = client.post("/login", data=login | {"csrf_token": fresh}, follow_redirects=False)
        assert response.status_code == 303


class _ScriptFinder(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.inline: list[str] = []
        self.handlers: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        names = {name for name, _ in attrs}
        if tag == "script" and "src" not in names:
            self.inline.append(self.get_starttag_text() or tag)
        self.handlers.extend(name for name in names if name.startswith("on"))


def test_templates_and_pages_have_no_inline_scripts(signed_in: TestClient) -> None:
    for template in TEMPLATES.rglob("*.html"):
        finder = _ScriptFinder()
        finder.feed(template.read_text(encoding="utf-8"))
        assert finder.inline == [] and finder.handlers == [], template
    pages = [signed_in.get(path) for path in ("/", "/account/password")]
    with TestClient(signed_in.app) as signed_out:
        pages.append(signed_out.get("/login"))
    for page in pages:
        finder = _ScriptFinder()
        finder.feed(page.text)
        assert finder.inline == [] and finder.handlers == []
        assert (
            page.headers["content-security-policy"] == SECURITY_HEADERS["content-security-policy"]
        )


ORIGIN_CASES = [
    # (headers, base_url, client base_url, allowed)
    ({"Sec-Fetch-Site": "cross-site"}, "http://localhost:8000", "http://testserver", False),
    ({"Sec-Fetch-Site": "same-origin"}, "http://localhost:8000", "http://testserver", True),
    ({"Origin": "http://localhost:8000"}, "http://localhost:8000", "http://testserver", True),
    ({"Origin": "http://testserver"}, "http://localhost:8000", "http://testserver", True),
    (  # LAN access by IP and port while BASE_URL names the host
        {"Origin": "http://192.168.1.10:8000"},
        "http://assets.lan:8000",
        "http://192.168.1.10:8000",
        True,
    ),
    (
        {"Origin": "https://app.example:8443"},
        "https://app.example:8443",
        "https://app.example:8443",
        True,
    ),
    ({"Origin": "https://app.example:8443"}, "https://app.example", "https://testserver", False),
    ({"Origin": "https://evil.example"}, "http://localhost:8000", "http://testserver", False),
    ({"Origin": "null"}, "http://localhost:8000", "http://testserver", False),
    (
        {"Origin": "https://other.assets.example", "Sec-Fetch-Site": "same-site"},
        "https://www.assets.example",
        "https://www.assets.example",
        False,
    ),
    (
        {"Origin": "http://localhost:8000", "Sec-Fetch-Site": "cross-site"},
        "http://localhost:8000",
        "http://testserver",
        False,
    ),
]


@pytest.mark.parametrize(("headers", "base_url", "client_url", "allowed"), ORIGIN_CASES)
def test_origin_check(
    web: WebHarness, headers: dict[str, str], base_url: str, client_url: str, allowed: bool
) -> None:
    app = web.app(base_url=base_url)
    with TestClient(app, base_url=client_url) as client:
        web.complete_setup(app)
        token = web.csrf(client.get("/login").text)
        response = client.post(
            "/login",
            data={"csrf_token": token, "email": web.admin_email, "password": web.admin_password},
            headers=headers,
            follow_redirects=False,
        )
    if allowed:
        assert response.status_code == 303
    else:
        assert response.status_code == 403
        assert "This request came from another site and was blocked." in response.text
