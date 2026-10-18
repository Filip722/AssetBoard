"""Proxy sign-in over HTTP (F1.4 part B, ADR-0015).

Every client runs through ``serve_app`` (``PeerMiddleware``) with an explicit TCP peer:
``PROXY`` is the trusted proxy, ``OUTSIDE`` anyone else.
"""

from __future__ import annotations

import io
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import delete, select, update
from sqlalchemy.exc import OperationalError

from assetboard.cli import serve_app
from assetboard.db.models.identity import (
    AuditEvent,
    ExternalIdentity,
    Membership,
    User,
    UserSession,
    Workspace,
)
from assetboard.domain.categories import SEED
from assetboard.i18n import gettext
from assetboard.services.auth import argon2_hash, session_id_hash
from assetboard.services.context import Context, Role, UserRef, WorkspaceRef
from assetboard.services.external_identity import PROXY_PROVIDER
from assetboard.services.workspaces import create_invitation
from assetboard.web import gate
from assetboard.web.security import session_cookie_name
from tests.conftest import owned_workspace_category_names

if TYPE_CHECKING:
    from conftest import WebHarness

PROXY = "172.30.0.2"
OUTSIDE = "198.51.100.20"
OLA = {"Remote-User": "ola", "Remote-Email": "ola@example.com", "Remote-Name": "Ola Nowak"}
BOB = {"Remote-User": "bob", "Remote-Email": "bob@example.com"}
LOCAL_PASSWORD = "Local Account Password"
# What a browser sends when it loads a page; only such requests are sent to /auth/proxy.
NAVIGATION = {
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
}


def proxy_settings(**overrides: object) -> dict[str, object]:
    return {
        "proxy_auth_enabled": True,
        "proxy_auth_user_header": "Remote-User",
        "proxy_auth_email_header": "Remote-Email",
        "proxy_auth_name_header": "Remote-Name",
        "proxy_auth_auto_create": True,
        "trusted_proxies": PROXY,
        "registration_ip_limit": 50,
        **overrides,
    }


@dataclass
class Proxy:
    app: FastAPI
    web: WebHarness
    clients: list[TestClient]

    def client(self, peer: str = PROXY, *, navigate: bool = True, **options: Any) -> TestClient:
        """Another browser behind ``peer`` (the lifespan already ran for the first one)."""
        client = TestClient(
            serve_app(self.app, self.app.state.settings),
            client=(peer, 40000),
            headers=NAVIGATION if navigate else None,
            **options,
        )
        self.clients.append(client)
        return client

    def db(self) -> Any:
        return self.app.state.session_factory()

    @property
    def cookie(self) -> str:
        return session_cookie_name(self.app.state.settings)

    def sessions(self) -> list[tuple[str, str]]:
        """(email, auth_method) of every session."""
        with self.db() as session:
            rows = session.execute(
                select(User.email, UserSession.auth_method).join(
                    UserSession, UserSession.user_id == User.id
                )
            )
            return sorted((email, method) for email, method in rows)

    def audits(self, action: str = "auth.proxy_failure") -> list[str]:
        with self.db() as session:
            rows = session.scalars(select(AuditEvent).where(AuditEvent.action == action)).all()
            return [str(row.data.get("reason")) for row in rows]


@contextmanager
def proxy_app(web: WebHarness, **overrides: object) -> Iterator[Proxy]:
    app = web.app(**proxy_settings(**overrides))
    with TestClient(serve_app(app, app.state.settings), client=(PROXY, 40000)) as first:
        web.complete_setup(app)
        harness = Proxy(app, web, [first])
        yield harness
        for client in harness.clients[1:]:
            client.close()


@pytest.fixture
def proxy(web: WebHarness) -> Iterator[Proxy]:
    with proxy_app(web) as harness:
        yield harness


@contextmanager
def captured_log(app: FastAPI) -> Iterator[io.StringIO]:
    logger: logging.Logger = app.state.logger
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    logger.addHandler(handler)
    try:
        yield output
    finally:
        logger.removeHandler(handler)


def sign_in(client: TestClient, headers: dict[str, str], path: str = "/") -> Response:
    """Follow the gate's redirects through /auth/proxy and the cookie check."""
    response = client.get(path, headers=headers, follow_redirects=False)
    for _ in range(4):
        if response.status_code not in {302, 303}:
            break
        response = client.get(response.headers["location"], headers=headers, follow_redirects=False)
    return response


def add_user(
    p: Proxy, email: str, *, password: str | None = LOCAL_PASSWORD, active: bool = True
) -> Any:
    with p.app.state.session_factory.begin() as session:
        user = User(
            email=email,
            display_name=email.split("@")[0],
            password_hash=argon2_hash(password) if password else None,
            is_active=active,
        )
        session.add(user)
        session.flush()
        return user.id


def link(p: Proxy, user_id: Any, subject: str) -> None:
    with p.app.state.session_factory.begin() as session:
        session.add(ExternalIdentity(user_id=user_id, provider=PROXY_PROVIDER, subject=subject))


def invite(p: Proxy) -> str:
    with p.db() as session:
        admin = session.scalars(select(User).where(User.email == p.web.admin_email)).one()
        workspace = session.scalars(select(Workspace)).one()
        ctx = Context(
            user=UserRef(admin.id, admin.email, admin.display_name, "en", True),
            workspace=WorkspaceRef(
                workspace.id,
                workspace.name,
                workspace.base_currency,
                workspace.locale,
                workspace.timezone,
                workspace.country_code,
            ),
            role=Role.OWNER,
            clock=p.app.state.clock,
        )
    return create_invitation(
        p.app.state.session_factory, ctx, role="editor", email="ola@example.com", expires_in_days=7
    ).token


# Off by default, and only the trusted peer counts


def test_every_proxy_route_is_404_while_proxy_auth_is_off(web: WebHarness) -> None:
    app = web.app(trusted_proxies=PROXY)
    with TestClient(serve_app(app, app.state.settings), client=(PROXY, 1)) as client:
        web.complete_setup(app)
        for path in (
            "/auth/proxy",
            "/auth/proxy?next=%2F",
            "/auth/proxy/continue",
            "/auth/proxy/failed/unavailable",
            "/auth/proxy/failed/no_account",
        ):
            assert client.get(path, headers=OLA, follow_redirects=False).status_code == 404
        # The headers mean nothing at all.
        page = client.get("/", headers=OLA, follow_redirects=False)
        assert page.headers["location"] == "/login?next=%2F"
        assert "Continue as" not in client.get("/login", headers=OLA).text


def test_spoofed_headers_from_an_untrusted_peer_are_ignored_and_warned_once(
    proxy: Proxy,
) -> None:
    outside = proxy.client(OUTSIDE)
    with captured_log(proxy.app) as log:
        for path in ("/", "/workspace/members", "/auth/proxy"):
            response = outside.get(path, headers=OLA, follow_redirects=False)
            assert response.status_code == 303
            assert response.headers["location"].startswith(("/login", "/auth/proxy/failed/"))
        # Forwarded headers naming the trusted proxy change nothing either.
        forged = outside.get("/", headers={**OLA, "X-Forwarded-For": PROXY}, follow_redirects=False)
        assert forged.headers["location"] == "/login?next=%2F"
    assert proxy.sessions() == []
    lines = [line for line in log.getvalue().splitlines() if "untrusted peer" in line]
    assert len(lines) == 1  # once per peer prefix in 10 minutes
    assert "remote-user" in lines[0] and "198.51.100.20/32" in lines[0]
    assert "ola@example.com" not in log.getvalue() and "Ola Nowak" not in log.getvalue()
    # Anyone can send these headers: never an audit row.
    assert proxy.audits() == []


def test_the_trusted_peer_without_identity_headers_is_signed_out(proxy: Proxy) -> None:
    client = proxy.client()
    with captured_log(proxy.app) as log:
        response = client.get("/", follow_redirects=False)
    assert response.headers["location"] == "/login?next=%2F"
    assert "WARNING" not in log.getvalue() and "proxy" not in log.getvalue()


# Signing in


def test_a_page_load_signs_in_through_the_proxy(proxy: Proxy) -> None:
    client = proxy.client()
    first = client.get("/workspace/members?x=1", headers=OLA, follow_redirects=False)
    assert first.status_code == 303
    assert first.headers["location"] == "/auth/proxy?next=" + quote(
        "/workspace/members?x=1", safe=""
    )
    signed_in = client.get(first.headers["location"], headers=OLA, follow_redirects=False)
    assert signed_in.status_code == 303
    assert signed_in.headers["location"].startswith("/auth/proxy/continue?next=")
    assert proxy.cookie in signed_in.headers.get("set-cookie", "")
    onward = client.get(signed_in.headers["location"], headers=OLA, follow_redirects=False)
    assert onward.headers["location"] == "/workspace/members?x=1"
    assert client.get("/", headers=OLA).status_code == 200
    assert proxy.sessions() == [("ola@example.com", "proxy")]
    with proxy.db() as session:
        user = session.scalars(select(User).where(User.email == "ola@example.com")).one()
    assert user.display_name == "Ola Nowak"
    assert proxy.audits("auth.login_success")


@pytest.mark.parametrize(
    "path", ["/auth/proxy", "/auth/proxy/continue", "/auth/proxy/failed/unavailable"]
)
def test_proxy_pages_send_no_referrer(proxy: Proxy, path: str) -> None:
    response = proxy.client().get(path, headers=OLA, follow_redirects=False)
    assert response.headers["referrer-policy"] == "no-referrer"


def test_head_requests_are_sent_to_proxy_sign_in(proxy: Proxy) -> None:
    client = proxy.client()
    response = client.head("/", headers=OLA, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/auth/proxy"
    # Pin: /auth/proxy is GET only, so HEAD there signs nobody in.
    assert client.head("/auth/proxy", headers=OLA).status_code == 405 and proxy.sessions() == []


def audit_count(p: Proxy) -> int:
    with p.db() as session:
        return len(session.scalars(select(AuditEvent.id)).all())


@pytest.mark.parametrize(
    "headers",
    [
        {"Accept": "*/*"},  # curl, monitors
        {"Accept": "application/json"},
        {"Accept": "text/html", "Sec-Fetch-Dest": "image", "Sec-Fetch-Mode": "no-cors"},
        {"Accept": "text/html", "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors"},
    ],
    ids=["any", "json", "image", "fetch"],
)
def test_requests_that_are_not_page_loads_get_login_and_write_nothing(
    proxy: Proxy, headers: dict[str, str]
) -> None:
    client = proxy.client(navigate=False)
    audits = audit_count(proxy)
    for method in ("GET", "HEAD"):
        response = client.request(method, "/", headers={**OLA, **headers}, follow_redirects=False)
        assert response.status_code == 303
        # ``next`` only for GET, as before.
        assert response.headers["location"] == ("/login?next=%2F" if method == "GET" else "/login")
    assert proxy.sessions() == [] and audit_count(proxy) == audits
    with proxy.db() as session:
        assert session.scalars(select(User).where(User.email == "ola@example.com")).first() is None


@pytest.mark.parametrize(
    "headers",
    [
        {"Accept": "text/html"},  # no Fetch Metadata (older browsers)
        {"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "iframe"},
        {"Sec-Fetch-Dest": "document"},
    ],
    ids=["accept-html", "mode-navigate", "dest-document"],
)
def test_page_navigations_still_sign_in(proxy: Proxy, headers: dict[str, str]) -> None:
    client = proxy.client(navigate=False)
    response = client.get("/", headers={**OLA, **headers}, follow_redirects=False)
    assert response.headers["location"] == "/auth/proxy"
    assert sign_in(proxy.client(), OLA).status_code == 200
    assert proxy.sessions() == [("ola@example.com", "proxy")]


def test_htmx_requests_get_hx_redirect(proxy: Proxy) -> None:
    response = proxy.client().get(
        "/workspace/members", headers={**OLA, "HX-Request": "true"}, follow_redirects=False
    )
    assert response.status_code == 204
    assert response.headers["hx-redirect"] == "/auth/proxy"


def test_a_browser_that_drops_the_cookie_gets_a_page_not_a_loop(web: WebHarness) -> None:
    # BASE_URL is https, the browser uses http: the Secure __Host- cookie never comes back.
    with proxy_app(web, base_url="https://money.example.test", env="prod") as p:
        client = p.client()
        response = client.get("/", headers=OLA, follow_redirects=False)
        hops = []
        while response.status_code in {302, 303} and len(hops) < 10:
            hops.append(response.headers["location"].split("?")[0])
            response = client.get(response.headers["location"], headers=OLA, follow_redirects=False)
        assert hops == ["/auth/proxy", "/auth/proxy/continue"]  # one round, then a page
        assert response.status_code == 400
        assert "did not keep the sign-in" in response.text
        assert "https://money.example.test" in response.text
        assert "?" not in "".join(hops)  # no query was needed to stop the loop


def test_a_valid_proxy_session_for_the_same_user_is_not_rotated(proxy: Proxy) -> None:
    client = proxy.client()
    sign_in(client, OLA)
    before = client.cookies.get(proxy.cookie)
    again = client.get(
        "/auth/proxy?next=%2Fworkspace%2Fmembers", headers=OLA, follow_redirects=False
    )
    assert again.headers["location"] == "/workspace/members"
    assert proxy.cookie not in again.headers.get("set-cookie", "")
    assert client.cookies.get(proxy.cookie) == before
    assert proxy.sessions() == [("ola@example.com", "proxy")]


def test_a_password_session_is_rotated_by_an_explicit_proxy_sign_in(proxy: Proxy) -> None:
    client = proxy.client()
    assert proxy.web.sign_in(client).status_code == 303
    assert proxy.sessions() == [(proxy.web.admin_email, "password")]
    response = client.get("/auth/proxy", headers=OLA, follow_redirects=False)
    assert response.headers["location"].startswith("/auth/proxy/continue")
    assert proxy.sessions() == [("ola@example.com", "proxy")]


# Proxy sessions are bound to the proxy


def test_another_proxy_user_ends_the_session_and_signs_in_again(proxy: Proxy) -> None:
    client = proxy.client()
    sign_in(client, OLA)
    response = client.get("/", headers=BOB, follow_redirects=False)
    assert response.headers["location"] == "/auth/proxy"
    assert proxy.sessions() == []  # ended on GET
    assert proxy.audits("auth.logout")
    sign_in(client, BOB)
    assert proxy.sessions() == [("bob@example.com", "proxy")]


def test_a_busy_database_does_not_fail_the_revoke(
    proxy: Proxy, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = proxy.client()
    sign_in(client, OLA)

    def locked(*_args: object, **_kwargs: object) -> None:
        raise OperationalError(
            "BEGIN IMMEDIATE", {}, sqlite3.OperationalError("database is locked")
        )

    with monkeypatch.context() as m, captured_log(proxy.app) as log:
        m.setattr(gate, "revoke_session", locked)
        response = client.get("/", headers=BOB, follow_redirects=False)
    # Signed out for this request all the same; the session ends on the next try.
    assert response.status_code == 303 and response.headers["location"] == "/auth/proxy"
    assert proxy.sessions() == [("ola@example.com", "proxy")]
    assert "database is busy" in log.getvalue()
    assert client.get("/", headers=BOB, follow_redirects=False).headers["location"] == (
        "/auth/proxy"
    )
    assert proxy.sessions() == []


def test_another_proxy_user_on_a_post_is_signed_out_without_revoking(proxy: Proxy) -> None:
    client = proxy.client()
    sign_in(client, OLA)
    response = client.post("/workspace/switch", headers=BOB, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/login"
    assert proxy.sessions() == [("ola@example.com", "proxy")]


@pytest.mark.parametrize(
    ("peer", "headers"),
    [
        (OUTSIDE, OLA),  # untrusted peer
        (PROXY, {}),  # no headers
        (PROXY, {"Remote-Email": "ola@example.com"}),  # no user header
    ],
)
def test_missing_trust_or_headers_count_as_signed_out_without_revoking(
    proxy: Proxy, peer: str, headers: dict[str, str]
) -> None:
    client = proxy.client()
    sign_in(client, OLA)
    other = proxy.client(peer, cookies={proxy.cookie: client.cookies[proxy.cookie]})
    response = other.get("/", headers=headers, follow_redirects=False)
    assert response.headers["location"] == "/login?next=%2F"
    assert proxy.sessions() == [("ola@example.com", "proxy")]
    assert client.get("/", headers=OLA).status_code == 200  # still valid through the proxy


@pytest.mark.parametrize(
    "raw",
    [
        [("remote-user", "ola"), ("Remote-User", "admin")],
        [("remote-user", "ola\x07")],
    ],
    ids=["duplicate", "invalid"],
)
def test_duplicate_or_invalid_headers_count_as_signed_out_without_revoking(
    proxy: Proxy, raw: list[tuple[str, str]]
) -> None:
    client = proxy.client()
    sign_in(client, OLA)
    with captured_log(proxy.app) as log:
        for _ in range(2):
            response = client.get(
                "/",
                headers=[(name, value) for name, value in raw],  # type: ignore[arg-type]
                follow_redirects=False,
            )
            assert response.headers["location"] == "/login?next=%2F"
    assert proxy.sessions() == [("ola@example.com", "proxy")]
    assert log.getvalue().count("the trusted proxy sent") == 1


def test_a_session_whose_proxy_identity_was_deleted_ends(proxy: Proxy) -> None:
    client = proxy.client()
    sign_in(client, OLA)
    with proxy.app.state.session_factory.begin() as session:
        session.execute(delete(ExternalIdentity))
    response = client.get("/", headers=OLA, follow_redirects=False)
    assert response.headers["location"] == "/auth/proxy"
    assert proxy.sessions() == []
    # The email now belongs to an unlinked account (link-by-email is off).
    assert sign_in(client, OLA).status_code == 409


@pytest.mark.parametrize("method", ["password", "oidc"])
def test_password_and_oidc_sessions_ignore_proxy_headers(proxy: Proxy, method: str) -> None:
    client = proxy.client()
    proxy.web.sign_in(client)
    with proxy.app.state.session_factory.begin() as session:
        session.execute(update(UserSession).values(auth_method=method))
    for peer in (PROXY, OUTSIDE):
        browser = proxy.client(peer, cookies={proxy.cookie: client.cookies[proxy.cookie]})
        for headers in (OLA, BOB, {}):
            assert browser.get("/", headers=headers, follow_redirects=False).status_code == 200
    assert proxy.sessions() == [(proxy.web.admin_email, method)]


def test_proxy_sessions_count_as_signed_out_once_proxy_auth_is_off(web: WebHarness) -> None:
    with proxy_app(web) as p:
        client = p.client()
        sign_in(client, OLA)
        cookie = client.cookies[p.cookie]
    app = web.app(trusted_proxies=PROXY)  # proxy auth off, same database
    with TestClient(serve_app(app, app.state.settings), client=(PROXY, 1)) as browser:
        browser.cookies.set(session_cookie_name(app.state.settings), cookie)
        response = browser.get("/", headers=OLA, follow_redirects=False)
        assert response.headers["location"] == "/login?next=%2F"
        with app.state.session_factory() as session:
            methods = session.scalars(select(UserSession.auth_method)).all()
        assert methods == ["proxy"]  # the row stays


# Failures: one generic page for trust and header problems, audited at most once


@pytest.mark.parametrize(
    ("peer", "headers"),
    [
        (OUTSIDE, OLA),
        (PROXY, {}),
        (PROXY, [("remote-user", "ola"), ("remote-user", "admin")]),
        (PROXY, [("remote-user", "ola\x07")]),
    ],
    ids=["untrusted", "absent", "duplicate", "invalid"],
)
def test_one_generic_failure_page_without_a_reason_oracle(
    proxy: Proxy, peer: str, headers: Any
) -> None:
    client = proxy.client(peer)
    response = client.get("/auth/proxy?next=%2F", headers=headers, follow_redirects=False)
    assert response.headers["location"] == "/auth/proxy/failed/unavailable"
    page = client.get(response.headers["location"])
    assert page.status_code == 403
    assert "not available for this request" in page.text
    assert proxy.sessions() == []


def test_header_problems_from_the_trusted_proxy_are_audited_once_per_reason(
    proxy: Proxy,
) -> None:
    client = proxy.client()
    for _ in range(3):
        client.get("/auth/proxy", headers=[("remote-user", "a"), ("remote-user", "b")])
    client.get("/auth/proxy", headers=[("remote-user", "bad\x07")])
    assert sorted(proxy.audits()) == ["headers_duplicate", "headers_invalid"]


def test_refusals_are_audited_once_per_subject_and_reason(web: WebHarness) -> None:
    with proxy_app(web, proxy_auth_auto_create=False) as p:
        client = p.client()
        with captured_log(p.app) as log:
            for _ in range(3):
                response = client.get("/auth/proxy", headers=OLA, follow_redirects=False)
                assert response.headers["location"] == "/auth/proxy/failed/no_account"
            client.get("/auth/proxy", headers=BOB, follow_redirects=False)
        assert p.audits() == ["no_account", "no_account"]  # ola once, bob once
        assert log.getvalue().count("reason=no_account") == 2  # ola's repeats, logged only
        page = client.get("/auth/proxy/failed/no_account")
        assert page.status_code == 403 and "no account for your sign-in" in page.text


def test_a_deactivated_proxy_user_is_refused(proxy: Proxy) -> None:
    user_id = add_user(proxy, "ola@example.com", password=None, active=False)
    link(proxy, user_id, "ola")
    client = proxy.client()
    for _ in range(2):
        response = client.get("/auth/proxy", headers=OLA, follow_redirects=False)
        assert response.headers["location"] == "/auth/proxy/failed/inactive"
    assert proxy.audits() == ["inactive"]


@pytest.mark.parametrize("reason", ["cookie", "nope", "failed"])
def test_unknown_failure_pages_are_404(proxy: Proxy, reason: str) -> None:
    assert proxy.client().get(f"/auth/proxy/failed/{reason}").status_code == 404


# Pages


def test_login_offers_continue_as_the_proxy_user_escaped_and_not_cached(proxy: Proxy) -> None:
    client = proxy.client()
    page = client.get("/login?next=%2Fworkspace%2Fmembers", headers=OLA)
    assert page.status_code == 200
    assert page.headers["cache-control"] == "no-store"
    assert "Continue as ola" in page.text
    assert 'href="/auth/proxy?next=%2Fworkspace%2Fmembers"' in page.text
    hostile = client.get("/login", headers={"Remote-User": "<b>x</b>"})
    assert "Continue as &lt;b&gt;x&lt;/b&gt;" in hostile.text and "<b>x</b>" not in hostile.text
    # Not offered when the proxy presents nobody, or to an untrusted peer.
    for other, headers in ((client, {}), (proxy.client(OUTSIDE), OLA)):
        page = other.get("/login", headers=headers)
        assert "data-proxy-sign-in" not in page.text
        assert page.headers["cache-control"] == "no-store"


def test_invite_pages_offer_continue_as_and_join_the_workspace(web: WebHarness) -> None:
    with proxy_app(web, proxy_auth_auto_create=False) as p:
        token = invite(p)
        client = p.client()
        page = client.get(f"/invite/{token}", headers=OLA)
        assert page.headers["cache-control"] == "no-store"
        expected = "/auth/proxy?next=" + quote(f"/invite/{token}", safe="")
        assert f'href="{expected}"' in page.text and "Continue as ola" in page.text
        outside = p.client(OUTSIDE).get(f"/invite/{token}", headers=OLA)
        assert "data-proxy-sign-in" not in outside.text
        assert outside.headers["cache-control"] == "no-store"
        # The invite page is signed out: the link signs in and joins.
        assert sign_in(client, OLA, expected).status_code == 200
        with p.db() as session:
            ola = session.scalars(select(User).where(User.email == "ola@example.com")).one()
            roles = session.scalars(select(Membership.role).where(Membership.user_id == ola.id))
            assert list(roles) == ["editor"]


def test_logout_lands_on_login_without_signing_in_again(proxy: Proxy) -> None:
    client = proxy.client()
    sign_in(client, OLA)
    token = proxy.web.csrf(client.get("/", headers=OLA).text)
    response = client.post(
        "/logout", data={"csrf_token": token}, headers=OLA, follow_redirects=False
    )
    assert response.headers["location"] == "/login"
    assert proxy.sessions() == []
    login = client.get("/login", headers=OLA, follow_redirects=False)
    assert login.status_code == 200 and "Continue as ola" in login.text
    assert proxy.sessions() == []
    # Any other page signs in again while the proxy presents the user.
    assert client.get("/", headers=OLA, follow_redirects=False).headers["location"] == (
        "/auth/proxy"
    )


def test_csrf_and_origin_checks_apply_to_proxy_sessions(proxy: Proxy) -> None:
    client = proxy.client()
    sign_in(client, OLA)
    assert client.post("/logout", headers=OLA, follow_redirects=False).status_code == 403
    token = proxy.web.csrf(client.get("/", headers=OLA).text)
    foreign = client.post(
        "/logout",
        data={"csrf_token": token},
        headers={**OLA, "Origin": "https://evil.example"},
        follow_redirects=False,
    )
    assert foreign.status_code == 403
    assert proxy.sessions() == [("ola@example.com", "proxy")]


def test_link_by_email_works_with_proxy_auth_alone(web: WebHarness) -> None:
    with proxy_app(web, proxy_auth_link_by_email=True) as p:
        user_id = add_user(p, "ola@example.com")
        client = p.client()
        response = client.get(
            "/auth/proxy?next=%2Fworkspace%2Fmembers", headers=OLA, follow_redirects=False
        )
        assert response.headers["location"] == "/login/link"
        page = client.get("/login/link", headers=OLA)
        assert page.status_code == 200
        assert "link it to this sign-in method" in page.text
        token = p.web.csrf(page.text)
        done = client.post(
            "/login/link",
            data={"password": LOCAL_PASSWORD, "csrf_token": token},
            headers=OLA,
            follow_redirects=False,
        )
        assert done.status_code == 303
        assert done.headers["location"] == "/workspace/members"
        assert p.sessions() == [("ola@example.com", "proxy")]
        with p.db() as session:
            linked = session.scalars(select(ExternalIdentity.user_id)).all()
        assert linked == [user_id]
        assert p.audits("auth.proxy_linked")


def test_link_page_is_404_without_oidc_and_proxy_auth(web: WebHarness) -> None:
    app = web.app()
    with TestClient(app) as client:
        web.complete_setup(app)
        assert client.get("/login/link").status_code == 404


def test_proxy_sign_in_works_under_oidc_only(web: WebHarness) -> None:
    with proxy_app(
        web,
        env="dev",
        base_url="http://localhost:8000",
        oidc_issuer="http://idp.example.test",
        oidc_client_id="assetboard",
        oidc_only=True,
    ) as p:
        client = p.client()
        assert sign_in(client, OLA).status_code == 200
        assert p.sessions() == [("ola@example.com", "proxy")]
        login = p.client(OUTSIDE).get("/login")
        assert "<form" not in login.text or 'name="password"' not in login.text


def test_session_ids_in_rows_are_hashes(proxy: Proxy) -> None:
    client = proxy.client()
    sign_in(client, OLA)
    with proxy.db() as session:
        hashes = session.scalars(select(UserSession.id_hash)).all()
    assert hashes == [session_id_hash(client.cookies[proxy.cookie])]


def test_auto_create_seeds_the_new_workspace_in_the_request_language(proxy: Proxy) -> None:
    client = proxy.client()
    headers = OLA | {"accept-language": "pl"}
    first = client.get("/", headers=headers, follow_redirects=False)
    signed_in = client.get(first.headers["location"], headers=headers, follow_redirects=False)
    assert signed_in.status_code == 303
    names = owned_workspace_category_names(proxy.app.state.session_factory, "ola@example.com")
    assert len(names) == len(SEED) and gettext("Food", "pl") in names
