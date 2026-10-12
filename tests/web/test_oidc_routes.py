"""OIDC sign-in pages (F1.3 part B) driven end to end through the fake provider."""

from __future__ import annotations

import io
import logging
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from assetboard.db.models.identity import (
    AuditEvent,
    AuthThrottle,
    ExternalIdentity,
    Membership,
    User,
    UserSession,
    Workspace,
)
from assetboard.services import auth as auth_service
from assetboard.services import csrf
from assetboard.services.auth import argon2_hash, session_id_hash
from assetboard.services.context import Context, Role, UserRef, WorkspaceRef
from assetboard.services.hash_gate import HashGateBusy
from assetboard.services.oidc import OidcClient
from assetboard.services.oidc_flow import FlowState, PendingLink
from assetboard.services.workspaces import create_invitation
from assetboard.web import oidc_routes
from assetboard.web.oidc_routes import FAILURES
from tests.oidc.fake_idp import BASE_URL, CLIENT_ID, CLIENT_SECRET, ISSUER, FakeIdp, StepClock

if TYPE_CHECKING:
    from conftest import WebHarness

LOCAL_PASSWORD = "Local Account Password"


@dataclass
class Oidc:
    app: FastAPI
    client: TestClient
    idp: FakeIdp
    web: WebHarness

    def start(self, next_path: str = "") -> Response:
        query = f"?next={quote(next_path, safe='')}" if next_path else ""
        return self.client.get(f"/auth/oidc/start{query}", follow_redirects=False)

    def sign_in(self, next_path: str = "", **callback: str) -> Response:
        start = self.start(next_path)
        assert start.status_code == 303, start.text
        params = self.idp.authorize(start.headers["location"], **callback)
        return self.client.get("/auth/oidc/callback", params=params, follow_redirects=False)

    def db(self) -> Any:
        return self.app.state.session_factory()


def oidc_settings(**overrides: object) -> dict[str, object]:
    return {
        "env": "prod",
        "base_url": BASE_URL,
        "oidc_issuer": ISSUER,
        "oidc_client_id": CLIENT_ID,
        "oidc_client_secret": CLIENT_SECRET,
        "oidc_provider_name": "Authentik",
        "registration_ip_limit": 50,
        **overrides,
    }


@contextmanager
def oidc_app(web: WebHarness, **overrides: object) -> Iterator[Oidc]:
    app = web.app(**oidc_settings(**overrides))
    clock = StepClock(datetime.now(UTC).isoformat())
    idp = FakeIdp(clock)
    app.state.oidc = OidcClient(app.state.settings, idp, clock)
    with TestClient(app, base_url=BASE_URL) as client:
        web.complete_setup(app)
        yield Oidc(app, client, idp, web)


@pytest.fixture
def oidc(web: WebHarness) -> Iterator[Oidc]:
    with oidc_app(web, oidc_auto_create=True) as harness:
        yield harness


def set_cookies(response: Response) -> list[str]:
    return response.headers.get_list("set-cookie")


def cookie_named(response: Response, name: str) -> str:
    found = [header for header in set_cookies(response) if header.startswith(f"{name}=")]
    assert found, (name, set_cookies(response))
    return found[0]


def flow_cookie_deleted(response: Response) -> bool:
    header = cookie_named(response, "__Host-assetboard_oidc_flow")
    return "Max-Age=0" in header or "max-age=0" in header.lower()


def add_user(
    o: Oidc, email: str, *, password: str | None = LOCAL_PASSWORD, admin: bool = False
) -> Any:
    with o.app.state.session_factory.begin() as session:
        user = User(
            email=email,
            display_name=email.split("@")[0],
            password_hash=argon2_hash(password) if password else None,
            is_instance_admin=admin,
        )
        session.add(user)
        session.flush()
        return user.id


def invite(o: Oidc, role: str = "editor", email: str | None = None) -> str:
    with o.db() as session:
        admin = session.scalars(select(User).where(User.email == o.web.admin_email)).one()
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
            clock=o.app.state.clock,
        )
    return create_invitation(
        o.app.state.session_factory, ctx, role=role, email=email, expires_in_days=7
    ).token


def audit_reasons(o: Oidc) -> list[str]:
    with o.db() as session:
        rows = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "auth.oidc_failure")
        ).all()
        return [str(row.data.get("reason")) for row in rows]


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


def anonymous_csrf(client: TestClient, app: FastAPI) -> str:
    """A valid signed-out token, for pages that have no form under OIDC_ONLY."""
    secret = csrf.new_anonymous_secret()
    client.cookies.set("__Host-assetboard_csrf", secret)
    raw = csrf.anonymous_token(app.state.csrf_key, secret, app.state.clock.now())
    return csrf.mask(raw)


def _locked() -> OperationalError:
    return OperationalError("BEGIN IMMEDIATE", {}, sqlite3.OperationalError("database is locked"))


# OIDC off


def test_every_oidc_route_is_404_while_oidc_is_off(web: WebHarness) -> None:
    app = web.app()
    with TestClient(app) as client:
        web.complete_setup(app)
        token = web.csrf(client.get("/login").text)
        responses = [
            client.get("/auth/oidc/start", follow_redirects=False),
            client.get("/auth/oidc/callback?code=x&state=y", follow_redirects=False),
            client.get("/auth/oidc/failed/no_account"),
            client.get("/login/link"),
            client.post("/login/link", data={"csrf_token": token, "password": "x"}),
            client.get("/login/local"),
            client.post("/login/local", data={"csrf_token": token, "email": "a", "password": "b"}),
        ]
        login = client.get("/login").text
    assert [response.status_code for response in responses] == [404] * len(responses)
    assert "/auth/oidc/start" not in login and "data-oidc-sign-in" not in login


# Start and callback


def test_start_seals_the_flow_cookie_and_redirects_to_the_provider(oidc: Oidc) -> None:
    response = oidc.start("/workspace/members")
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(f"{ISSUER}/auth?")
    query = parse_qs(urlsplit(location).query)
    assert query["code_challenge_method"] == ["S256"]
    cookie = cookie_named(response, "__Host-assetboard_oidc_flow")
    flow = FlowState.unseal(
        oidc.app.state.oidc_flow_key,
        cookie.split(";", 1)[0].split("=", 1)[1],
        now=oidc.app.state.clock.now(),
    )
    assert flow is not None and flow.next_path == "/workspace/members"
    assert flow.state == query["state"][0] and flow.nonce == query["nonce"][0]


@pytest.mark.parametrize(
    ("settings", "name", "secure"),
    [
        ({}, "__Host-assetboard_oidc_flow", True),
        (
            {"env": "dev", "base_url": "http://localhost:8000"},
            "assetboard_oidc_flow",
            False,
        ),
    ],
)
def test_flow_cookie_attributes(
    web: WebHarness, settings: dict[str, object], name: str, secure: bool
) -> None:
    with oidc_app(web, **settings) as o:
        response = o.start()
    header = cookie_named(response, name)
    attributes = {part.strip().split("=", 1)[0].lower() for part in header.split(";")[1:]}
    assert "httponly" in attributes and "path" in attributes and "max-age" in attributes
    assert ("secure" in attributes) is secure
    assert "domain" not in attributes
    assert "samesite=lax" in header.lower() and "path=/;" in header.lower() + ";"
    assert "max-age=600" in header.lower()


def test_happy_path_signs_in_with_a_clean_redirect_and_no_flow_cookie(oidc: Oidc) -> None:
    response = oidc.sign_in("/workspace/members")
    assert response.status_code == 303
    assert response.headers["location"] == "/workspace/members"
    assert flow_cookie_deleted(response)
    session_cookie = cookie_named(response, "__Host-assetboard_session")
    session_id = session_cookie.split(";", 1)[0].split("=", 1)[1]
    with oidc.db() as session:
        row = session.get(UserSession, session_id_hash(session_id))
        assert row is not None and row.auth_method == "oidc"
    assert oidc.client.get("/", follow_redirects=False).status_code == 200


def test_the_callback_rotates_an_existing_session(oidc: Oidc) -> None:
    # The flow starts signed out; the browser has a session by the time the callback arrives
    # (another tab signed in meanwhile). The callback ends it and issues a new id.
    start = oidc.start()
    assert oidc.web.sign_in(oidc.client).status_code == 303
    old = oidc.client.cookies.get("__Host-assetboard_session")
    assert old
    params = oidc.idp.authorize(start.headers["location"])
    response = oidc.client.get("/auth/oidc/callback", params=params, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"] == "/"
    new = cookie_named(response, "__Host-assetboard_session").split(";")[0].split("=", 1)[1]
    assert new != old
    with oidc.db() as session:
        assert session.get(UserSession, session_id_hash(old)) is None
        assert session.get(UserSession, session_id_hash(new)) is not None


def test_start_sends_a_signed_in_visitor_to_next(oidc: Oidc) -> None:
    assert oidc.web.sign_in(oidc.client).status_code == 303
    response = oidc.start("/workspace/members")
    assert response.headers["location"] == "/workspace/members"


def test_callback_without_the_flow_cookie_is_refused(oidc: Oidc) -> None:
    """Login CSRF: a callback URL the victim's browser did not start."""
    start = oidc.start()
    params = oidc.idp.authorize(start.headers["location"])
    with (
        TestClient(oidc.app, base_url=BASE_URL) as victim,
        captured_log(oidc.app) as output,
    ):
        response = victim.get("/auth/oidc/callback", params=params, follow_redirects=False)
        assert (response.status_code, response.headers["location"]) == (
            303,
            "/auth/oidc/failed/start_over",
        )
        assert victim.get("/", follow_redirects=False).status_code == 303  # not signed in
    # Decided before the code exchange: logged, not audited.
    assert "reason=flow_missing" in output.getvalue()
    assert audit_reasons(oidc) == []
    assert oidc.idp.token_requests == []


def test_callback_with_a_wrong_state_is_refused(oidc: Oidc) -> None:
    start = oidc.start()
    params = oidc.idp.authorize(start.headers["location"], state="forged-state")
    with captured_log(oidc.app) as output:
        response = oidc.client.get("/auth/oidc/callback", params=params, follow_redirects=False)
    assert response.headers["location"] == "/auth/oidc/failed/start_over"
    assert flow_cookie_deleted(response)
    assert "reason=state_mismatch" in output.getvalue()
    assert audit_reasons(oidc) == []


def test_a_provider_error_callback_is_logged_not_audited(oidc: Oidc) -> None:
    with captured_log(oidc.app) as output:
        response = oidc.sign_in(error="access_denied", error_description="no")
    assert response.headers["location"] == "/auth/oidc/failed/cancelled"
    assert "reason=access_denied" in output.getvalue()
    assert audit_reasons(oidc) == []


def test_a_junk_code_is_logged_not_audited(oidc: Oidc) -> None:
    start = oidc.start()
    params = {**oidc.idp.authorize(start.headers["location"]), "code": "junk"}
    with captured_log(oidc.app) as output:
        response = oidc.client.get("/auth/oidc/callback", params=params, follow_redirects=False)
    assert response.headers["location"] == "/auth/oidc/failed/failed"
    assert "reason=token_error" in output.getvalue()
    assert audit_reasons(oidc) == []
    assert len(oidc.idp.token_requests) == 1


def test_failures_after_the_provider_accepted_the_code_are_audited(oidc: Oidc) -> None:
    oidc.idp.id_token_overrides = {"aud": "someone-else"}
    response = oidc.sign_in()
    assert response.headers["location"] == "/auth/oidc/failed/failed"
    assert audit_reasons(oidc) == ["id_token_invalid"]


def test_a_replayed_callback_is_refused_before_any_provider_call(oidc: Oidc) -> None:
    start = oidc.start()
    flow_cookie = cookie_named(start, "__Host-assetboard_oidc_flow").split(";")[0].split("=", 1)[1]
    params = oidc.idp.authorize(start.headers["location"])
    first = oidc.client.get("/auth/oidc/callback", params=params, follow_redirects=False)
    assert first.headers["location"] == "/"
    assert len(oidc.idp.token_requests) == 1
    # The same cookie and the same callback URL again (for example captured and replayed).
    with TestClient(oidc.app, base_url=BASE_URL) as replay, captured_log(oidc.app) as output:
        replay.cookies.set("__Host-assetboard_oidc_flow", flow_cookie)
        again = replay.get("/auth/oidc/callback", params=params, follow_redirects=False)
        assert again.headers["location"] == "/auth/oidc/failed/start_over"
        assert replay.get("/", follow_redirects=False).status_code == 303  # not signed in
    assert len(oidc.idp.token_requests) == 1  # no second code exchange
    assert "reason=flow_replayed" in output.getvalue()
    assert audit_reasons(oidc) == []


def test_a_second_tab_replaces_the_flow_and_the_first_tab_starts_over(oidc: Oidc) -> None:
    first = oidc.start()
    oidc.start()  # the second tab overwrites the cookie
    params = oidc.idp.authorize(first.headers["location"])
    response = oidc.client.get("/auth/oidc/callback", params=params, follow_redirects=False)
    assert response.headers["location"] == "/auth/oidc/failed/start_over"
    page = oidc.client.get(response.headers["location"])
    assert "another sign-in was started in a different tab" in page.text


@pytest.mark.parametrize(
    ("bad_next", "expected"),
    [
        ("//evil.example/x", "/"),
        ("/\\evil.example", "/"),
        ("https://evil.example/", "/"),
        ("/\tjavascript:alert(1)", "/"),
        ("/" + "a" * 600, "/"),
        ("/ok/page", "/ok/page"),
    ],
)
def test_the_callback_validates_next_from_the_cookie_again(
    oidc: Oidc, bad_next: str, expected: str
) -> None:
    client: OidcClient = oidc.app.state.oidc
    request = client.begin()
    params = oidc.idp.authorize(request.url)
    forged = FlowState(request.state, request.nonce, request.code_verifier, bad_next)
    oidc.client.cookies.set(
        "__Host-assetboard_oidc_flow",
        forged.seal(oidc.app.state.oidc_flow_key, now=oidc.app.state.clock.now()),
    )
    response = oidc.client.get("/auth/oidc/callback", params=params, follow_redirects=False)
    assert response.headers["location"] == expected


@pytest.mark.parametrize("next_path", ["/café", "/" + "x" * 513, "//evil.example"])
def test_start_caps_next(oidc: Oidc, next_path: str) -> None:
    response = oidc.start(next_path)
    cookie = cookie_named(response, "__Host-assetboard_oidc_flow")
    flow = FlowState.unseal(
        oidc.app.state.oidc_flow_key,
        cookie.split(";", 1)[0].split("=", 1)[1],
        now=oidc.app.state.clock.now(),
    )
    assert flow is not None and flow.next_path == "/"


@pytest.mark.parametrize(
    ("scenario", "page"),
    [
        ("no_account", "no_account"),
        ("unverified", "email_unverified"),
        ("cancelled", "cancelled"),
        ("bad_token", "failed"),
        ("locked", "busy"),
        ("domain", "domain_not_allowed"),
    ],
)
def test_every_callback_outcome_is_a_303_to_a_clean_url(
    web: WebHarness, monkeypatch: pytest.MonkeyPatch, scenario: str, page: str
) -> None:
    overrides: dict[str, object] = {"oidc_auto_create": scenario != "no_account"}
    if scenario == "domain":
        overrides["oidc_allowed_domains"] = "family.example"
    with oidc_app(web, **overrides) as o:
        callback: dict[str, str] = {}
        if scenario == "unverified":
            o.idp.claims["email_verified"] = "false"
        elif scenario == "cancelled":
            callback = {"error": "access_denied", "error_description": "user said no\nFAKE LOG"}
        elif scenario == "bad_token":
            o.idp.id_token_overrides = {"aud": "someone-else"}
        elif scenario == "locked":

            def locked(*_args: object, **_kwargs: object) -> object:
                raise _locked()

            monkeypatch.setattr(oidc_routes, "sign_in_with_oidc", locked)
        response = o.sign_in("/workspace/members", **callback)
        assert response.status_code == 303
        location = response.headers["location"]
        assert location == f"/auth/oidc/failed/{page}" and "?" not in location
        assert flow_cookie_deleted(response)
        assert not any("assetboard_session" in c for c in set_cookies(response))
        shown = o.client.get(location)
        assert shown.status_code == FAILURES[page][0]
        if page == "busy":
            assert shown.headers["retry-after"] == "5"


def test_an_unreachable_provider_at_start_is_a_failure_page(web: WebHarness) -> None:
    with oidc_app(web) as o:
        o.idp.discovery_status = 503
        response = o.start()
        assert response.headers["location"] == "/auth/oidc/failed/unavailable"
        page = o.client.get(response.headers["location"])
    assert page.status_code == 503 and page.headers["retry-after"] == "5"
    assert "Authentik cannot be reached right now" in page.text


def test_an_oversized_pending_link_is_a_failure_page_not_a_500(
    web: WebHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def too_large(*_args: object, **_kwargs: object) -> str:
        raise ValueError("payload too large for a cookie")

    monkeypatch.setattr(PendingLink, "seal", too_large)
    with oidc_app(web, oidc_link_by_email=True) as o:
        o.idp.claims["email"] = o.web.admin_email
        with captured_log(o.app) as output:
            response = o.sign_in()
    assert response.headers["location"] == "/auth/oidc/failed/failed"
    assert "reason=pending_link_too_large" in output.getvalue()
    # The reason only: nothing from the payload.
    assert o.web.admin_email not in output.getvalue() and "user-123" not in output.getvalue()


# Failure pages


@pytest.mark.parametrize("reason", sorted(FAILURES))
def test_failure_pages_send_no_referrer_and_have_no_forms(oidc: Oidc, reason: str) -> None:
    response = oidc.client.get(f"/auth/oidc/failed/{reason}")
    assert response.status_code == FAILURES[reason][0]
    assert response.headers["referrer-policy"] == "no-referrer"
    assert '<meta name="referrer" content="no-referrer">' in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "<form" not in response.text
    assert "%(provider)s" not in response.text
    assert ("retry-after" in response.headers) is (FAILURES[reason][0] == 503)


def test_no_failure_message_mentions_the_profile_yet() -> None:
    # Linking from the profile arrives with F1.5, which restores that sentence.
    assert all("profile" not in message.lower() for _status, message in FAILURES.values())


def test_unknown_failure_reasons_are_404(oidc: Oidc) -> None:
    assert oidc.client.get("/auth/oidc/failed/whatever").status_code == 404


def test_start_and_callback_responses_send_no_referrer(oidc: Oidc) -> None:
    for response in (oidc.start(), oidc.sign_in()):
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["cache-control"] == "no-store"


def test_oidc_paths_are_signed_out_paths_but_setup_comes_first(web: WebHarness) -> None:
    app = web.app(**oidc_settings())
    with TestClient(app, base_url=BASE_URL) as client:
        before_setup = client.get("/auth/oidc/start", follow_redirects=False)
        assert before_setup.headers["location"] == "/setup"
        web.complete_setup(app)
        assert client.get("/auth/oidc/failed/no_account").status_code == 403  # no /login redirect
        assert client.get("/login/link").status_code == 400  # expired page, signed out


# Linking by email


def link_cookie(response: Response) -> str:
    return cookie_named(response, "__Host-assetboard_oidc_link")


@contextmanager
def start_link(web: WebHarness, **overrides: object) -> Iterator[Oidc]:
    """An app where the provider's verified email matches the administrator's account."""
    with oidc_app(web, oidc_link_by_email=True, **overrides) as o:
        o.idp.claims["email"] = o.web.admin_email
        response = o.sign_in("/workspace/members")
        assert response.headers["location"] == "/login/link"
        assert flow_cookie_deleted(response)
        link_cookie(response)
        yield o


def test_linking_confirms_the_password_on_a_same_origin_page(web: WebHarness) -> None:
    with start_link(web) as o:
        page = o.client.get("/login/link")
        assert page.status_code == 200
        assert page.headers["referrer-policy"] == "same-origin"
        assert o.web.admin_email in page.text and "Authentik" in page.text
        token = o.web.csrf(page.text)
        wrong = o.client.post("/login/link", data={"csrf_token": token, "password": "nope"})
        assert wrong.status_code == 401
        assert "The password is not correct" in wrong.text
        right = o.client.post(
            "/login/link",
            data={"csrf_token": o.web.csrf(wrong.text), "password": o.web.admin_password},
            follow_redirects=False,
        )
        assert (right.status_code, right.headers["location"]) == (303, "/workspace/members")
        assert "max-age=0" in link_cookie(right).lower()
        with o.db() as session:
            assert session.scalars(select(ExternalIdentity.subject)).all() == ["user-123"]


def test_linking_after_the_flag_was_turned_off_fails_generically(web: WebHarness) -> None:
    with start_link(web) as o:
        page = o.client.get("/login/link")
        o.app.state.settings.oidc_link_by_email = False
        response = o.client.post(
            "/login/link",
            data={"csrf_token": o.web.csrf(page.text), "password": o.web.admin_password},
        )
        assert response.status_code == 401
        assert "The password is not correct, or there were too many attempts" in response.text
        with o.db() as session:
            assert session.scalars(select(ExternalIdentity)).all() == []


def test_link_page_without_a_pending_link_asks_to_start_over(oidc: Oidc) -> None:
    page = oidc.client.get("/login/link")
    assert page.status_code == 400
    assert "This confirmation expired" in page.text and "<form" not in page.text
    token = oidc.web.csrf(oidc.client.get("/login").text)
    posted = oidc.client.post("/login/link", data={"csrf_token": token, "password": "x"})
    assert posted.status_code == 400


def test_link_confirmation_handles_a_locked_database(
    web: WebHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    with start_link(web) as o:
        page = o.client.get("/login/link")

        async def locked(*_args: object, **_kwargs: object) -> object:
            raise _locked()

        monkeypatch.setattr(oidc_routes, "link_identity_async", locked)
        response = o.client.post(
            "/login/link", data={"csrf_token": o.web.csrf(page.text), "password": "x"}
        )
        assert response.status_code == 503 and response.headers["retry-after"] == "5"
        assert "The server is busy. Try again in a few seconds." in response.text


def test_link_confirmation_handles_busy_hashing(web: WebHarness) -> None:
    with start_link(web) as o:
        page = o.client.get("/login/link")

        class BusyGate:
            async def run(self, *_args: object, **_kwargs: object) -> object:
                raise HashGateBusy

        o.app.state.hash_gate = BusyGate()
        response = o.client.post(
            "/login/link",
            data={"csrf_token": o.web.csrf(page.text), "password": o.web.admin_password},
        )
        assert response.status_code == 503 and response.headers["retry-after"] == "5"


# Invitations


def test_invite_page_offers_the_provider_with_next(oidc: Oidc) -> None:
    token = invite(oidc)
    with TestClient(oidc.app, base_url=BASE_URL) as visitor:
        page = visitor.get(f"/invite/{token}").text
    expected = "/auth/oidc/start?next=" + quote(f"/invite/{token}", safe="")
    assert f'href="{expected}"' in page
    assert "Continue with Authentik" in page
    assert "Create account and join" in page  # local sign-up still offered


def test_invitation_sign_up_through_the_provider_joins_the_workspace(web: WebHarness) -> None:
    with oidc_app(web) as o:  # auto-create off: the invitation is enough
        token = invite(o, role="viewer")
        response = o.sign_in(f"/invite/{token}")
        assert (response.status_code, response.headers["location"]) == (303, "/")
        session_id = cookie_named(response, "__Host-assetboard_session").split(";")[0]
        with o.db() as session:
            row = session.get(UserSession, session_id_hash(session_id.split("=", 1)[1]))
            workspace = session.scalars(select(Workspace)).one()
            assert row is not None and row.current_workspace_id == workspace.id
            roles = session.scalars(select(Membership.role)).all()
            assert sorted(roles) == ["owner", "viewer"]


# OIDC_ONLY and break-glass


def test_login_page_offers_the_provider_and_carries_next(oidc: Oidc) -> None:
    page = oidc.client.get("/login?next=%2Fworkspace%2Fmembers").text
    assert 'href="/auth/oidc/start?next=%2Fworkspace%2Fmembers"' in page
    assert "Continue with Authentik" in page and 'name="password"' in page
    assert "/login/local" not in page


def test_oidc_only_hides_password_sign_in_and_local_sign_up(web: WebHarness) -> None:
    with oidc_app(web, oidc_only=True) as o:
        login = o.client.get("/login")
        assert "Continue with Authentik" in login.text
        assert 'name="password"' not in login.text
        token = invite(o)
        csrf_token = anonymous_csrf(o.client, o.app)
        invite_page = o.client.get(f"/invite/{token}").text
        assert "Continue with Authentik" in invite_page
        assert "Create account and join" not in invite_page
        posts = [
            o.client.post(
                "/login",
                data={
                    "csrf_token": csrf_token,
                    "email": o.web.admin_email,
                    "password": o.web.admin_password,
                },
            ),
            o.client.post(
                f"/invite/{token}/register",
                data={
                    "csrf_token": csrf_token,
                    "email": "n@example.com",
                    "display_name": "N",
                    "password": "Another Strong Password",
                },
            ),
            o.client.get("/register"),
            o.client.get("/login/local"),  # break-glass not enabled
        ]
        assert [p.status_code for p in posts] == [404, 404, 404, 404]


def _break_glass(o: Oidc, email: str, password: str) -> Response:
    page = o.client.get("/login/local")
    return o.client.post(
        "/login/local",
        data={"csrf_token": o.web.csrf(page.text), "email": email, "password": password},
        follow_redirects=False,
    )


def _without_tokens(html: str) -> str:
    return re.sub(r'value="[^"]{20,}"', 'value=""', html).replace("x@example.com", "")


def test_break_glass_is_for_instance_admins_only(
    web: WebHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    with oidc_app(web, oidc_only=True, allow_local_admin_login=True) as o:
        add_user(o, "x@example.com")
        verifies: list[str] = []
        real_verify = auth_service.verify_password

        def counting(stored: str, given: str) -> bool:
            verifies.append(given)
            return real_verify(stored, given)

        monkeypatch.setattr(auth_service, "verify_password", counting)
        o.app.state.hash_gate = _InlineGate()
        right_non_admin = _break_glass(o, "x@example.com", LOCAL_PASSWORD)
        wrong_non_admin = _break_glass(o, "x@example.com", "Wrong Password Here")
        assert right_non_admin.status_code == wrong_non_admin.status_code == 401
        assert _without_tokens(right_non_admin.text) == _without_tokens(wrong_non_admin.text)
        assert len(verifies) == 2  # one verify each: the same path
        with o.db() as session:
            failures = session.scalars(
                select(AuditEvent)
                .where(AuditEvent.action == "auth.login_failure")
                .order_by(AuditEvent.ts)
            ).all()
            account = session.scalars(
                select(AuthThrottle).where(AuthThrottle.key_type == "account")
            ).all()
        assert [event.data for event in failures] == [{"reason": "not_instance_admin"}, {}]
        assert [row.failure_count for row in account] == [2]  # both counted
        admin = _break_glass(o, o.web.admin_email, o.web.admin_password)
        assert admin.status_code == 303
        assert "/login/local" not in o.client.get("/login", follow_redirects=False).text


class _InlineGate:
    """Runs hashing inline so a monkeypatched verify is visible (the real gate uses threads)."""

    async def run(self, operation: Any) -> Any:
        return operation()


# RP-initiated logout


def _logout(o: Oidc) -> Response:
    token = o.web.csrf(o.client.get("/account/password").text)
    return o.client.post("/logout", data={"csrf_token": token}, follow_redirects=False)


def test_rp_logout_shows_the_interstitial_for_oidc_sessions(web: WebHarness) -> None:
    with oidc_app(web, oidc_auto_create=True, oidc_rp_logout=True) as o:
        signed_in = o.sign_in()
        session_id = cookie_named(signed_in, "__Host-assetboard_session").split(";")[0]
        response = _logout(o)
        assert response.status_code == 200
        assert '<meta name="referrer" content="no-referrer">' in response.text
        refresh = re.search(r'<meta http-equiv="refresh" content="0; url=([^"]+)">', response.text)
        assert refresh is not None
        target = refresh.group(1).replace("&amp;", "&")
        assert target.startswith(f"{ISSUER}/logout?")
        query = parse_qs(urlsplit(target).query)
        assert query == {
            "client_id": [CLIENT_ID],
            "post_logout_redirect_uri": [f"{BASE_URL}/login"],
        }
        assert "data-provider-logout" in response.text
        assert "max-age=0" in cookie_named(response, "__Host-assetboard_session").lower()
        with o.db() as session:
            hashed = session_id_hash(session_id.split("=", 1)[1])
            assert session.get(UserSession, hashed) is None  # ended first


@pytest.mark.parametrize("case", ["password_session", "flag_off", "no_end_session"])
def test_logout_is_normal_otherwise(web: WebHarness, case: str) -> None:
    with oidc_app(web, oidc_auto_create=True, oidc_rp_logout=case != "flag_off") as o:
        if case == "no_end_session":
            o.idp.discovery_overrides = {"end_session_endpoint": None}
        if case == "password_session":
            assert o.web.sign_in(o.client).status_code == 303
        else:
            o.sign_in()
        response = _logout(o)
        assert (response.status_code, response.headers["location"]) == (303, "/login")


def test_account_page_says_an_oidc_only_account_has_no_password(oidc: Oidc) -> None:
    oidc.sign_in()
    page = oidc.client.get("/account/password")
    assert "This account has no password" in page.text
    assert 'name="current_password"' not in page.text
    response = oidc.client.post(
        "/account/password",
        data={
            "csrf_token": oidc.web.csrf(page.text),
            "current_password": "",
            "new_password": "Brand New Password",
            "confirm_password": "Brand New Password",
        },
    )
    assert response.status_code == 400
    assert "This account has no password" in response.text
    assert "The current password is not correct" not in response.text
