"""Sign-in, sessions and cookies over HTTP."""

from __future__ import annotations

import hashlib
import io
import logging
import re
import time
from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from sqlalchemy.orm import sessionmaker

from assetboard.db.engine import create_database_engine, immediate_transaction
from assetboard.db.models.identity import AuthThrottle, User, UserSession
from assetboard.services.auth import session_id_hash
from assetboard.services.hash_gate import HashGateBusy
from assetboard.web.security import safe_next

if TYPE_CHECKING:
    from conftest import CountingHasher, WebHarness

LOGIN_FAILED = "The email or password is not correct, or there were too many attempts."


def _login(client: TestClient, web: WebHarness, email: str, password: str, **extra: str):
    data = {"csrf_token": web.csrf(client.get("/login").text), "email": email, "password": password}
    data.update(extra)
    return client.post("/login", data=data, follow_redirects=False)


def _strip_tokens(html: str) -> str:
    return re.sub(r'value="[A-Za-z0-9_\-]{40,}"', 'value="TOKEN"', html)


def _throttle(app: object, scope: str, key: str) -> AuthThrottle | None:
    with app.state.session_factory() as session:  # type: ignore[attr-defined]
        return session.scalar(
            select(AuthThrottle).where(
                AuthThrottle.key_type == scope,
                AuthThrottle.key_hash == hashlib.sha256(key.encode()).hexdigest(),
            )
        )


@pytest.mark.parametrize("https", [False, True])
def test_session_cookie_attributes(web: WebHarness, https: bool) -> None:
    base_url = "https://assets.example" if https else "http://localhost:8000"
    app = web.app(base_url=base_url)
    with TestClient(app, base_url="https://testserver" if https else "http://testserver") as client:
        web.complete_setup(app)
        response = _login(client, web, web.admin_email, web.admin_password)
    assert response.status_code == 303
    cookie = next(c for c in response.headers.get_list("set-cookie") if "_session=" in c)
    name = "__Host-assetboard_session" if https else "assetboard_session"
    assert cookie.startswith(f"{name}=")
    assert "HttpOnly" in cookie and "Path=/" in cookie and "SameSite=lax" in cookie
    assert "Domain" not in cookie
    assert ("Secure" in cookie) is https
    max_age = int(re.search(r"Max-Age=(\d+)", cookie).group(1))  # type: ignore[union-attr]
    absolute = int(timedelta(days=app.state.settings.session_absolute_timeout_days).total_seconds())
    assert absolute - 5 <= max_age <= absolute


def test_login_rotates_the_session(web: WebHarness) -> None:
    app = web.app()
    with TestClient(app) as client:
        web.complete_setup(app)
        first = _login(client, web, web.admin_email, web.admin_password)
        old = first.cookies["assetboard_session"]
        second = _login(client, web, web.admin_email, web.admin_password)
        new = second.cookies["assetboard_session"]
    assert old != new
    with app.state.session_factory() as session:
        assert session.get(UserSession, session_id_hash(old)) is None
        assert session.get(UserSession, session_id_hash(new)) is not None


def test_login_failures_are_identical(web: WebHarness) -> None:
    app = web.app()
    with TestClient(app, client=("192.0.2.30", 1000)) as client:
        web.complete_setup(app)
        unknown = _login(client, web, "nobody@example.com", "wrong password")
        wrong = _login(client, web, web.admin_email, "wrong password")
        for _ in range(app.state.settings.auth_account_free_failures):
            _login(client, web, web.admin_email, "wrong password")
        throttled = _login(client, web, web.admin_email, web.admin_password)
    assert unknown.status_code == wrong.status_code == throttled.status_code == 401
    assert all("www-authenticate" not in r.headers for r in (unknown, wrong, throttled))
    assert _strip_tokens(unknown.text).replace("nobody@example.com", "X") == _strip_tokens(
        wrong.text
    ).replace(web.admin_email, "X")
    assert _strip_tokens(throttled.text) == _strip_tokens(wrong.text)
    assert LOGIN_FAILED in wrong.text


@pytest.mark.parametrize(
    "candidate",
    [
        "//evil.example",
        "/\\evil.example",
        "https://evil.example/",
        "javascript:alert(1)",
        "/%2F%2Fevil.example",
        "/ok\r\nLocation: //evil",
        "%2F%2Fevil.example",
        "",
        "relative/path",
    ],
)
def test_next_rejects_open_redirects(web: WebHarness, candidate: str) -> None:
    assert safe_next(candidate) == "/"
    app = web.app()
    with TestClient(app) as client:
        web.complete_setup(app)
        response = _login(client, web, web.admin_email, web.admin_password, next=candidate)
    assert response.headers["location"] == "/"


def test_next_accepts_local_paths(web: WebHarness) -> None:
    assert safe_next("/account/password?tab=1") == "/account/password?tab=1"
    app = web.app()
    with TestClient(app) as client:
        web.complete_setup(app)
        redirect = client.get("/account/password", follow_redirects=False).headers["location"]
        assert redirect == "/login?next=%2Faccount%2Fpassword"
        page = client.get(redirect)
        assert 'name="next" value="/account/password"' in page.text
        response = _login(
            client, web, web.admin_email, web.admin_password, next="/account/password"
        )
    assert response.headers["location"] == "/account/password"


def test_logout_clears_the_cookie_and_the_session_row(
    signed_in: TestClient, web: WebHarness
) -> None:
    session_id = signed_in.cookies["assetboard_session"]
    token = web.page_token(signed_in)
    response = signed_in.post("/logout", data={"csrf_token": token}, follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/login")
    cookie = next(c for c in response.headers.get_list("set-cookie") if "_session=" in c)
    assert "Max-Age=0" in cookie
    app = signed_in.app
    with app.state.session_factory() as session:  # type: ignore[attr-defined]
        assert session.get(UserSession, session_id_hash(session_id)) is None
    assert signed_in.get("/", follow_redirects=False).status_code == 303


def test_logout_other_devices_keeps_the_current_session(web: WebHarness) -> None:
    app = web.app()
    with TestClient(app) as phone, TestClient(app) as laptop:
        web.complete_setup(app)
        web.sign_in(phone)
        web.sign_in(laptop)
        token = web.page_token(laptop, "/account/password")
        response = laptop.post(
            "/account/logout-others",
            data={"confirm_password": web.admin_password},  # a sensitive action since F1.5
            headers={"HX-Request": "true", "X-CSRF-Token": token},
        )
        assert response.status_code == 200 and "Other sessions signed out: 1." in response.text
        assert laptop.get("/").status_code == 200
        assert phone.get("/", follow_redirects=False).status_code == 303


@pytest.mark.parametrize("case", ["idle", "absolute", "tampered", "deactivated"])
def test_invalid_session_cookies_are_signed_out(signed_in: TestClient, case: str) -> None:
    app = signed_in.app
    session_id = signed_in.cookies["assetboard_session"]
    id_hash = session_id_hash(session_id)
    with app.state.session_factory.begin() as session:  # type: ignore[attr-defined]
        row = session.get(UserSession, id_hash)
        assert row is not None
        now = app.state.clock.now()  # type: ignore[attr-defined]
        if case == "idle":  # absolute expiry still in the future
            row.last_seen_at = now - timedelta(days=15)
            assert row.expires_at > now
        elif case == "absolute":  # recently active, but past the absolute lifetime
            row.expires_at = now - timedelta(seconds=1)
            row.last_seen_at = now
        elif case == "deactivated":
            session.execute(update(User).values(is_active=False))
    if case == "tampered":
        signed_in.cookies.set("assetboard_session", session_id[:-2] + "xx")
    response = signed_in.get("/", follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/login?next=%2F")


def test_session_activity_write_is_best_effort_when_the_database_is_locked(
    signed_in: TestClient,
) -> None:
    app = signed_in.app
    id_hash = session_id_hash(signed_in.cookies["assetboard_session"])
    stale = app.state.clock.now() - timedelta(minutes=10)  # type: ignore[attr-defined]
    with app.state.session_factory.begin() as session:  # type: ignore[attr-defined]
        row = session.get(UserSession, id_hash)
        assert row is not None
        row.last_seen_at = stale
    locker = create_database_engine(app.state.settings.database_url)  # type: ignore[attr-defined]
    try:
        with immediate_transaction(sessionmaker(locker, info={"clock": app.state.clock})):  # type: ignore[attr-defined]
            started = time.monotonic()
            response = signed_in.get("/")
            elapsed = time.monotonic() - started
    finally:
        locker.dispose()
    assert response.status_code == 200
    assert elapsed < 3  # the touch waits ~250 ms, never the 5 s request lock timeout
    with app.state.session_factory() as session:  # type: ignore[attr-defined]
        row = session.get(UserSession, id_hash)
        assert row is not None and abs((row.last_seen_at - stale).total_seconds()) < 1


def test_throttle_uses_the_client_address_and_ignores_forwarded_headers(web: WebHarness) -> None:
    app = web.app()
    with TestClient(app, client=("192.0.2.7", 4000)) as client:
        web.complete_setup(app)
        data = {"csrf_token": web.csrf(client.get("/login").text), "email": "x@example.com"}
        client.post(
            "/login",
            data=data | {"password": "wrong password"},
            headers={"X-Forwarded-For": "203.0.113.9", "Forwarded": "for=203.0.113.9"},
        )
    assert _throttle(app, "ip", "192.0.2.7/32") is not None
    assert _throttle(app, "ip", "203.0.113.9/32") is None


def test_missing_client_address_uses_the_shared_bucket_and_warns_once(web: WebHarness) -> None:
    app = web.app()
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    app.state.logger.addHandler(handler)
    try:
        with TestClient(app) as client:  # TestClient's peer is "testclient", not an IP
            web.complete_setup(app)
            for _ in range(2):
                _login(client, web, "x@example.com", "wrong password")
    finally:
        app.state.logger.removeHandler(handler)
    unknown = _throttle(app, "ip", "unknown")
    assert unknown is not None and unknown.failure_count == 2
    assert output.getvalue().count("client address unavailable") == 1


def test_auth_pages_are_not_cached_and_have_autocomplete(
    signed_in: TestClient, web: WebHarness
) -> None:
    password = signed_in.get("/account/password")
    signed_in.post("/logout", data={"csrf_token": web.csrf(password.text)})
    login = signed_in.get("/login")
    for page in (password, login):
        assert page.headers["cache-control"] == "no-store"
    assert 'autocomplete="username"' in login.text
    assert 'autocomplete="current-password"' in login.text
    assert 'autocomplete="current-password"' in password.text
    assert password.text.count('autocomplete="new-password"') == 2


def test_oversized_login_is_refused_without_hashing_or_counting(
    web: WebHarness, argon2_calls: CountingHasher
) -> None:
    app = web.app()
    with TestClient(app, client=("192.0.2.31", 1000)) as client:
        web.complete_setup(app)
        before = (argon2_calls.hashes, argon2_calls.verifies)
        response = _login(client, web, web.admin_email, "p" * 1025)
        long_email = _login(client, web, "e" * 400 + "@example.com", "password")
    assert response.status_code == long_email.status_code == 401
    assert LOGIN_FAILED in response.text
    assert (argon2_calls.hashes, argon2_calls.verifies) == before
    assert _throttle(app, "ip", "192.0.2.31/32") is None


class _BusyGate:
    async def run(self, _operation: object) -> object:
        raise HashGateBusy


def test_busy_hashing_returns_503_with_retry_after_on_every_auth_route(web: WebHarness) -> None:
    app = web.app(allow_registration=True)
    with TestClient(app, client=("192.0.2.40", 1000)) as client:
        setup_form = {
            "csrf_token": web.csrf(client.get("/setup").text),
            "setup_token": app.state.setup_token,
            "email": web.admin_email,
            "display_name": "Admin",
            "password": web.admin_password,
        }
        real_gate = app.state.hash_gate
        app.state.hash_gate = _BusyGate()
        setup = client.post("/setup", data=setup_form)
        app.state.hash_gate = real_gate
        web.complete_setup(app)
        app.state.hash_gate = _BusyGate()
        login = _login(client, web, web.admin_email, web.admin_password)
        register = client.post(
            "/register",
            data={
                "csrf_token": web.csrf(client.get("/register").text),
                "email": "new@example.com",
                "display_name": "New",
                "password": "Another Strong Password",
            },
        )
        app.state.hash_gate = real_gate
        web.sign_in(client)
        app.state.hash_gate = _BusyGate()
        change = client.post(
            "/account/password",
            data={
                "csrf_token": web.page_token(client, "/account/password"),
                "current_password": web.admin_password,
                "new_password": "Another Strong Password",
                "confirm_password": "Another Strong Password",
            },
        )
        app.state.hash_gate = real_gate
    for response in (setup, login, register, change):
        assert response.status_code == 503, response.text
        assert response.headers["retry-after"] == "5"
        assert "The server is busy" in response.text
    # Each request released what it reserved.
    account = _throttle(app, "account", web.admin_email)
    assert account is None or account.failure_count == 0
    register_row = _throttle(app, "register", "192.0.2.40/32")
    assert register_row is not None and register_row.failure_count == 0
