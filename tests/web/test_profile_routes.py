"""The profile page, sensitive-action confirmation and linked identities (F1.5 part A)."""

from __future__ import annotations

import html
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from assetboard.cli import serve_app
from assetboard.db.models.identity import AuditEvent, ExternalIdentity, User, UserSession
from assetboard.i18n.locale import SUPPORTED_LOCALES
from assetboard.services.auth import argon2_hash
from assetboard.services.external_identity import PROXY_PROVIDER
from assetboard.services.oidc import OidcClient
from assetboard.web import confirm
from assetboard.web.account_routes import LANGUAGE_NAMES
from assetboard.web.security import session_cookie_name
from tests.oidc.fake_idp import BASE_URL, CLIENT_ID, CLIENT_SECRET, ISSUER, FakeIdp, StepClock

if TYPE_CHECKING:
    from conftest import WebHarness

PROXY = "172.30.0.2"
OUTSIDE = "198.51.100.20"
NAVIGATION = {"Accept": "text/html", "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate"}
OLA_PROXY = {"Remote-User": "ola", "Remote-Email": "ola@example.com"}
LOCAL_PASSWORD = "Local Account Password"
OLD_ISSUER = "https://old-idp.example.test"


def settings(**overrides: object) -> dict[str, object]:
    return {
        "env": "prod",
        "base_url": BASE_URL,
        "oidc_issuer": ISSUER,
        "oidc_client_id": CLIENT_ID,
        "oidc_client_secret": CLIENT_SECRET,
        "oidc_provider_name": "Authentik",
        "oidc_auto_create": True,
        "proxy_auth_enabled": True,
        "proxy_auth_user_header": "Remote-User",
        "proxy_auth_email_header": "Remote-Email",
        "proxy_auth_auto_create": True,
        "trusted_proxies": PROXY,
        "registration_ip_limit": 50,
        **overrides,
    }


@dataclass
class Harness:
    app: FastAPI
    web: WebHarness
    idp: FakeIdp
    clients: list[TestClient] = field(default_factory=list[TestClient])

    def client(self, peer: str = PROXY) -> TestClient:
        client = TestClient(
            serve_app(self.app, self.app.state.settings),
            base_url=BASE_URL,
            client=(peer, 40000),
            headers=NAVIGATION,
        )
        self.clients.append(client)
        return client

    def db(self) -> Any:
        return self.app.state.session_factory()

    @property
    def cookie(self) -> str:
        return session_cookie_name(self.app.state.settings)

    def token(self, client: TestClient, path: str = "/account", **kwargs: Any) -> str:
        return self.web.csrf(client.get(path, **kwargs).text)

    def identities(self) -> list[tuple[str, str, str]]:
        """(email, provider, subject) of every linked identity."""
        with self.db() as session:
            rows = session.execute(
                select(User.email, ExternalIdentity.provider, ExternalIdentity.subject).join(
                    ExternalIdentity, ExternalIdentity.user_id == User.id
                )
            )
            return sorted((email, provider, subject) for email, provider, subject in rows)

    def sessions(self) -> list[tuple[str, str]]:
        with self.db() as session:
            rows = session.execute(
                select(User.email, UserSession.auth_method).join(
                    UserSession, UserSession.user_id == User.id
                )
            )
            return sorted((email, method) for email, method in rows)

    def audits(self, action: str) -> list[dict[str, Any]]:
        with self.db() as session:
            rows = session.scalars(
                select(AuditEvent).where(AuditEvent.action == action).order_by(AuditEvent.ts)
            ).all()
            return [dict(row.data) for row in rows]

    def user(self, email: str) -> User:
        with self.db() as session:
            return session.scalars(select(User).where(User.email == email)).one()

    def oidc_sign_in(self, client: TestClient) -> Response:
        start = client.get("/auth/oidc/start", follow_redirects=False)
        params = self.idp.authorize(start.headers["location"])
        return client.get("/auth/oidc/callback", params=params, follow_redirects=False)

    def proxy_sign_in(self, client: TestClient, headers: dict[str, str]) -> None:
        # /account works without a workspace (accounts created here have none).
        response = client.get("/account", headers=headers, follow_redirects=False)
        for _ in range(4):
            if response.status_code != 303:
                break
            response = client.get(
                response.headers["location"], headers=headers, follow_redirects=False
            )
        assert response.status_code == 200, response.text

    def age_sessions(self, minutes: int) -> None:
        with self.app.state.session_factory.begin() as session:
            session.execute(
                update(UserSession).values(
                    created_at=self.app.state.clock.now() - timedelta(minutes=minutes)
                )
            )


@contextmanager
def harness(web: WebHarness, **overrides: object) -> Iterator[Harness]:
    app = web.app(**settings(**overrides))
    clock = StepClock(datetime.now(UTC).isoformat())
    idp = FakeIdp(clock)
    app.state.oidc = OidcClient(app.state.settings, idp, clock)
    with TestClient(serve_app(app, app.state.settings), base_url=BASE_URL) as first:
        web.complete_setup(app)
        h = Harness(app, web, idp, [first])
        yield h
        for client in h.clients[1:]:
            client.close()


@pytest.fixture
def h(web: WebHarness) -> Iterator[Harness]:
    with harness(web) as value:
        yield value


def admin(h: Harness) -> TestClient:
    client = h.client()
    assert h.web.sign_in(client).status_code == 303
    return client


def add_identity(h: Harness, email: str, provider: str, subject: str) -> Any:
    with h.app.state.session_factory.begin() as session:
        user = session.scalars(select(User).where(User.email == email)).one()
        identity = ExternalIdentity(user_id=user.id, provider=provider, subject=subject)
        session.add(identity)
        session.flush()
        return identity.id


def add_user(h: Harness, email: str, *, password: str | None = LOCAL_PASSWORD) -> Any:
    with h.app.state.session_factory.begin() as session:
        user = User(
            email=email,
            display_name=email.split("@")[0],
            password_hash=argon2_hash(password) if password else None,
        )
        session.add(user)
        session.flush()
        return user.id


def continue_url(page: Response) -> str:
    match = re.search(r'href="([^"]+)" rel="noreferrer" data-provider-link', page.text)
    assert match, page.text
    return html.unescape(match.group(1))


# Profile settings


def test_profile_page_and_nav_link(h: Harness) -> None:
    client = admin(h)
    home = client.get("/")
    assert 'href="/account" data-profile-link' in home.text
    page = client.get("/account")
    assert page.status_code == 200 and "data-profile-form" in page.text
    assert 'value="browser" selected' in page.text  # new accounts follow the browser


@pytest.mark.parametrize(
    ("form", "field_error"),
    [
        ({"display_name": "  ", "locale": "en", "theme": "dark"}, "display_name"),
        ({"display_name": "x" * 121, "locale": "en", "theme": "dark"}, "display_name"),
        ({"display_name": "Ola", "locale": "de", "theme": "dark"}, "locale"),
        ({"display_name": "Ola", "locale": "pl-PL", "theme": "dark"}, "locale"),
        ({"display_name": "Ola", "locale": "en", "theme": "neon"}, "theme"),
    ],
)
def test_invalid_profile_values_are_field_errors(
    h: Harness, form: dict[str, str], field_error: str
) -> None:
    client = admin(h)
    before = h.user(h.web.admin_email)
    response = client.post(
        "/account/profile", data={**form, "csrf_token": h.token(client)}, follow_redirects=False
    )
    assert response.status_code == 400
    assert f'id="{field_error}-error"' in response.text
    after = h.user(h.web.admin_email)
    assert (after.display_name, after.locale, after.theme) == (
        before.display_name,
        before.locale,
        before.theme,
    )


@pytest.mark.parametrize(
    ("value", "stored"), [("pl", "pl"), ("en", "en"), ("browser", None), ("", None)]
)
def test_profile_saves_name_language_and_theme(h: Harness, value: str, stored: str | None) -> None:
    client = admin(h)
    response = client.post(
        "/account/profile",
        data={
            "display_name": "  Ola   Admin ",
            "locale": value,
            "theme": "dark",
            "csrf_token": h.token(client),
        },
        follow_redirects=False,
    )
    assert response.status_code == 303 and response.headers["location"] == "/account"
    saved = h.user(h.web.admin_email)
    assert (saved.display_name, saved.locale, saved.theme) == ("Ola Admin", stored, "dark")


def save_locale(h: Harness, client: TestClient, locale: str) -> None:
    client.post(
        "/account/profile",
        data={
            "display_name": "Admin",
            "locale": locale,
            "theme": "system",
            "csrf_token": h.token(client),
        },
    )


@pytest.mark.parametrize(
    ("saved", "accept", "expected"),
    [
        ("browser", "pl", "pl"),  # NULL follows Accept-Language
        ("browser", "en", "en"),
        ("pl", "en", "pl"),  # the saved language beats the browser
        ("en", "pl", "en"),
    ],
)
def test_language_precedence(h: Harness, saved: str, accept: str, expected: str) -> None:
    client = admin(h)
    save_locale(h, client, saved)
    page = client.get("/account", headers={"Accept-Language": accept})
    assert f'<html lang="{expected}"' in page.text
    assert ("Profil" in page.text and "Twoje dane" in page.text) == (expected == "pl")


def test_the_app_sets_no_language_cookie(h: Harness) -> None:
    # Precedence starts with a per-device cookie only if the app sets one; it sets none, so a
    # stray "locale" cookie changes nothing.
    client = admin(h)
    save_locale(h, client, "pl")
    client.cookies.set("locale", "en")
    response = client.get("/account", headers={"Accept-Language": "en"})
    assert '<html lang="pl"' in response.text
    assert not any("locale" in cookie for cookie in response.headers.get_list("set-cookie"))


@pytest.mark.parametrize("theme", ["system", "light", "dark"])
def test_saved_theme_is_the_server_rendered_default(h: Harness, theme: str) -> None:
    client = admin(h)
    client.post(
        "/account/profile",
        data={
            "display_name": "Admin",
            "locale": "browser",
            "theme": theme,
            "csrf_token": h.token(client),
        },
    )
    page = client.get("/account").text
    tag = re.search(r"<html [^>]*>", page)
    assert tag is not None
    assert ("dark" in tag.group(0).split('class="')[1].split('"')[0]) == (theme == "dark")
    assert (f'data-theme="{theme}"' in tag.group(0)) == (theme != "system")
    assert "<script>" not in page  # no inline script (CSP)


# The one confirmation entry point


def sensitive_posts(h: Harness, client: TestClient) -> dict[str, tuple[str, dict[str, str]]]:
    # An earlier issuer's identity: a current-issuer one would refuse link_oidc before any
    # confirmation (one identity per provider).
    oidc_id = add_identity(h, h.web.admin_email, OLD_ISSUER, "admin-oidc")
    return {
        "link_oidc": ("/account/identities/oidc", {}),
        "link_proxy": ("/account/identities/proxy", OLA_PROXY),
        "unlink": (f"/account/identities/{oidc_id}/unlink", {}),
        "logout_others": ("/account/logout-others", {}),
    }


@pytest.mark.parametrize("action", ["link_oidc", "link_proxy", "unlink", "logout_others"])
@pytest.mark.parametrize("password", ["", "wrong password"])
def test_every_sensitive_post_needs_confirmation(h: Harness, action: str, password: str) -> None:
    client = admin(h)
    other_device = h.client()
    h.web.sign_in(other_device)
    path, headers = sensitive_posts(h, client)[action]
    before = (h.identities(), h.sessions())
    response = client.post(
        path,
        data={"csrf_token": h.token(client), "confirm_password": password},
        headers=headers,
        follow_redirects=False,
    )
    assert response.status_code == 401
    assert "data-provider-link" not in response.text  # no continuation to the provider
    assert not any("oidc_flow" in cookie for cookie in response.headers.get_list("set-cookie"))
    assert (h.identities(), h.sessions()) == before
    assert h.audits("auth.password_confirm_failure")  # the primitive audits failures


def test_a_stale_external_session_is_refused_without_an_audit_row(h: Harness) -> None:
    client = h.client()
    h.proxy_sign_in(client, OLA_PROXY)  # passwordless account created through the proxy
    h.age_sessions(11)
    audits_before = len(h.audits("auth.password_confirm_failure"))
    page = client.get("/account", headers=OLA_PROXY)
    assert "data-confirm-stale" in page.text and "sign in again" in page.text.lower()
    response = client.post(
        "/account/logout-others", data={"csrf_token": h.web.csrf(page.text)}, headers=OLA_PROXY
    )
    assert response.status_code == 403
    assert "Sign in again to confirm this change" in response.text
    assert len(h.audits("auth.password_confirm_failure")) == audits_before


def test_a_fresh_external_session_confirms_for_ten_minutes(h: Harness) -> None:
    client = h.client()
    h.proxy_sign_in(client, OLA_PROXY)
    page = client.get("/account", headers=OLA_PROXY)
    assert (
        "data-confirm-fresh" in page.text
        and 'name="confirm_password" type="password"' not in page.text
    )
    h.age_sessions(9)  # created 9 minutes ago: still fresh (created_at, not last activity)
    response = client.post(
        "/account/logout-others", data={"csrf_token": h.web.csrf(page.text)}, headers=OLA_PROXY
    )
    assert response.status_code == 200 and "Other sessions signed out: 0." in response.text


def test_a_password_account_never_uses_the_fresh_session(h: Harness) -> None:
    # Ola has a password and signs in through the proxy: the password is still required.
    add_user(h, "ola@example.com")
    add_identity(h, "ola@example.com", PROXY_PROVIDER, "ola")
    client = h.client()
    h.proxy_sign_in(client, OLA_PROXY)
    page = client.get("/account", headers=OLA_PROXY)
    assert 'name="confirm_password" type="password"' in page.text
    response = client.post(
        "/account/logout-others", data={"csrf_token": h.web.csrf(page.text)}, headers=OLA_PROXY
    )
    assert response.status_code == 401


# Linking OIDC from the profile


def test_link_oidc_from_the_profile(h: Harness) -> None:
    client = admin(h)
    session_cookie = client.cookies[h.cookie]
    logins_before = len(h.audits("auth.login_success"))
    page = client.post(
        "/account/identities/oidc",
        data={"csrf_token": h.token(client), "confirm_password": h.web.admin_password},
    )
    assert page.status_code == 200
    assert '<meta http-equiv="refresh"' in page.text
    # The continuation to the provider sends no Referer (the page's own policy).
    assert '<meta name="referrer" content="no-referrer">' in page.text
    params = h.idp.authorize(continue_url(page))
    done = client.get("/auth/oidc/callback", params=params, follow_redirects=False)
    assert done.status_code == 303 and done.headers["location"] == "/account?link=linked"
    assert h.identities() == [(h.web.admin_email, ISSUER, "user-123")]
    assert h.audits("auth.oidc_linked") == [{"via": "profile", "confirmed": "password"}]
    # Linking is not a sign-in: no login event, no new session cookie.
    assert len(h.audits("auth.login_success")) == logins_before
    assert not any(h.cookie in cookie for cookie in done.headers.get_list("set-cookie"))
    assert client.cookies[h.cookie] == session_cookie
    shown = client.get(done.headers["location"])
    assert "The sign-in method is linked to your account." in shown.text


def start_link(h: Harness, client: TestClient) -> dict[str, str]:
    page = client.post(
        "/account/identities/oidc",
        data={"csrf_token": h.token(client), "confirm_password": h.web.admin_password},
    )
    return h.idp.authorize(continue_url(page))


def test_link_callback_needs_the_session_that_confirmed(h: Harness) -> None:
    client = admin(h)
    params = start_link(h, client)
    # The confirming session ends before the provider comes back.
    client.post("/logout", data={"csrf_token": h.token(client, "/")})
    h.web.sign_in(client)
    done = client.get("/auth/oidc/callback", params=params, follow_redirects=False)
    assert done.headers["location"] == "/account?link=failed"
    assert h.identities() == []
    shown = client.get("/account?link=failed").text
    assert (
        "Linking failed." in shown and "Sign-in" not in shown.split("data-profile-error")[1][:200]
    )


def test_link_callback_in_another_session_of_the_same_user_links_nothing(h: Harness) -> None:
    confirming = admin(h)
    params = start_link(h, confirming)
    flow_cookie = next(
        (name, value) for name, value in confirming.cookies.items() if "oidc_flow" in name
    )
    # The same user's other device receives the callback (with the flow cookie copied over):
    # it is not the session that confirmed, which still exists.
    other = admin(h)
    other.cookies.set(*flow_cookie)
    done = other.get("/auth/oidc/callback", params=params, follow_redirects=False)
    assert done.headers["location"] == "/account?link=failed"
    assert h.identities() == []
    assert len(h.sessions()) == 2


def test_linking_an_identity_already_linked_to_this_user(h: Harness) -> None:
    client = admin(h)
    params = start_link(h, client)
    add_identity(h, h.web.admin_email, ISSUER, "user-123")  # linked meanwhile
    done = client.get("/auth/oidc/callback", params=params, follow_redirects=False)
    assert done.headers["location"] == "/account?link=already_linked"
    assert "already linked to your account" in client.get(done.headers["location"]).text


def test_linking_an_identity_of_someone_else_is_a_generic_failure(h: Harness) -> None:
    add_user(h, "other@example.com")
    add_identity(h, "other@example.com", ISSUER, "user-123")
    client = admin(h)
    params = start_link(h, client)
    done = client.get("/auth/oidc/callback", params=params, follow_redirects=False)
    assert done.headers["location"] == "/account?link=failed"  # same as any other failure
    assert h.identities() == [("other@example.com", ISSUER, "user-123")]
    assert h.audits("auth.oidc_linked") == []


@pytest.mark.parametrize("code", ["evil", "<script>", "linked%0A"])
def test_unknown_link_codes_show_nothing(h: Harness, code: str) -> None:
    page = admin(h).get(f"/account?link={code}")
    assert "data-profile-error" not in page.text and "data-profile-notice" not in page.text


# Linking the proxy from the profile


def test_link_proxy_takes_the_subject_from_the_trusted_headers(h: Harness) -> None:
    client = admin(h)
    page = client.get("/account", headers=OLA_PROXY)
    assert "data-link-proxy" in page.text and "signs you in as ola" in page.text
    response = client.post(
        "/account/identities/proxy",
        data={
            "csrf_token": h.web.csrf(page.text),
            "confirm_password": h.web.admin_password,
            "subject": "forged",  # ignored: never a form field
        },
        headers=OLA_PROXY,
    )
    assert response.status_code == 200
    assert h.identities() == [(h.web.admin_email, PROXY_PROVIDER, "ola")]
    assert h.audits("auth.proxy_linked") == [{"via": "profile", "confirmed": "password"}]
    # The password session stays (proxy headers never touch it).
    assert h.sessions() == [(h.web.admin_email, "password")]


def test_a_proxy_subject_linked_to_someone_else_shows_nothing(h: Harness) -> None:
    add_user(h, "ola@example.com")
    add_identity(h, "ola@example.com", PROXY_PROVIDER, "ola")
    client = admin(h)
    page = client.get("/account", headers=OLA_PROXY)
    assert "data-link-proxy" not in page.text and "ola" not in page.text.split("data-identities")[1]
    response = client.post(
        "/account/identities/proxy",
        data={"csrf_token": h.web.csrf(page.text), "confirm_password": h.web.admin_password},
        headers=OLA_PROXY,
    )
    assert response.status_code == 409 and "Linking failed." in response.text
    assert h.identities() == [("ola@example.com", PROXY_PROVIDER, "ola")]


def test_link_proxy_from_an_untrusted_peer_is_a_generic_failure(h: Harness) -> None:
    client = h.client(OUTSIDE)
    h.web.sign_in(client)
    response = client.post(
        "/account/identities/proxy",
        data={"csrf_token": h.token(client), "confirm_password": h.web.admin_password},
        headers=OLA_PROXY,
    )
    assert response.status_code == 400 and "Linking failed." in response.text
    assert h.identities() == []


# Unlinking


def unlink(
    h: Harness, client: TestClient, identity_id: Any, password: str, **kwargs: Any
) -> Response:
    return client.post(
        f"/account/identities/{identity_id}/unlink",
        data={"csrf_token": h.token(client, **kwargs), "confirm_password": password},
        **kwargs,
    )


def test_unlink_oidc_revokes_other_oidc_sessions_only(h: Harness) -> None:
    client = admin(h)
    identity_id = add_identity(h, h.web.admin_email, ISSUER, "user-123")
    with h.app.state.session_factory.begin() as session:
        user = session.scalars(select(User).where(User.email == h.web.admin_email)).one()
    elsewhere = h.client()
    h.web.sign_in(elsewhere)
    with h.app.state.session_factory.begin() as session:
        hashes = session.scalars(
            select(UserSession.id_hash).where(UserSession.user_id == user.id)
        ).all()
        session.execute(
            update(UserSession).where(UserSession.id_hash == hashes[-1]).values(auth_method="oidc")
        )
    response = unlink(h, client, identity_id, h.web.admin_password)
    assert response.status_code == 200 and "was removed" in response.text
    assert h.identities() == []
    assert h.sessions() == [(h.web.admin_email, "password")]
    assert h.audits("auth.oidc_unlinked") == [
        {"via": "profile", "confirmed": "password", "other_sessions_revoked": 1}
    ]


def test_unlinking_the_proxy_ends_the_proxy_session_through_the_binding(h: Harness) -> None:
    add_user(h, "ola@example.com")
    identity_id = add_identity(h, "ola@example.com", PROXY_PROVIDER, "ola")
    proxy_browser = h.client()
    h.proxy_sign_in(proxy_browser, OLA_PROXY)
    assert ("ola@example.com", "proxy") in h.sessions()
    password_browser = h.client(OUTSIDE)
    h.web.sign_in(password_browser, email="ola@example.com", password=LOCAL_PASSWORD)
    response = unlink(h, password_browser, identity_id, LOCAL_PASSWORD)
    assert response.status_code == 200
    assert h.audits("auth.proxy_unlinked") == [{"via": "profile", "confirmed": "password"}]
    # The next page load through the proxy no longer matches: the proxy session ends.
    proxy_browser.get("/", headers=OLA_PROXY, follow_redirects=False)
    assert ("ola@example.com", "proxy") not in h.sessions()


def test_the_last_sign_in_method_cannot_be_unlinked(h: Harness) -> None:
    client = h.client()
    assert h.oidc_sign_in(client).status_code == 303  # passwordless account through OIDC
    with h.db() as session:
        identity_id = session.scalars(select(ExternalIdentity.id)).one()
    response = unlink(h, client, identity_id, "")
    assert response.status_code == 409 and "last way to sign in" in response.text
    assert len(h.identities()) == 1
    assert h.audits("auth.oidc_unlinked") == []


def test_unlinking_someone_elses_identity_is_404(h: Harness) -> None:
    add_user(h, "other@example.com")
    identity_id = add_identity(h, "other@example.com", ISSUER, "x")
    client = admin(h)
    assert unlink(h, client, identity_id, h.web.admin_password).status_code == 404
    assert unlink(h, client, "not-a-uuid", h.web.admin_password).status_code == 404
    assert len(h.identities()) == 1


# The restored account_exists sentence


@pytest.mark.parametrize(
    "path", ["/auth/oidc/failed/account_exists", "/auth/proxy/failed/account_exists"]
)
@pytest.mark.parametrize(
    ("language", "text"),
    [
        ("en", "Sign in with your password, then link it from your profile."),
        ("pl", "powiąż je w swoim profilu"),
    ],
)
def test_account_exists_says_to_link_from_the_profile(
    h: Harness, path: str, language: str, text: str
) -> None:
    page = h.client().get(path, headers={"Accept-Language": language})
    assert page.status_code == 409 and text in page.text


def test_new_accounts_follow_the_browser_language(h: Harness) -> None:
    # Setup (the administrator), proxy auto-create and OIDC auto-create all store NULL.
    h.proxy_sign_in(h.client(), OLA_PROXY)
    h.idp.subject, h.idp.claims["email"] = "oidc-1", "oidc@example.com"
    assert h.oidc_sign_in(h.client()).status_code == 303
    with h.db() as session:
        locales = session.execute(select(User.email, User.locale).order_by(User.email)).all()
    assert [tuple(row) for row in locales] == [
        (h.web.admin_email, None),
        ("oidc@example.com", None),
        ("ola@example.com", None),
    ]


# One identity per provider


def test_link_oidc_is_refused_up_front_when_the_issuer_is_already_linked(h: Harness) -> None:
    client = admin(h)
    add_identity(h, h.web.admin_email, ISSUER, "admin-oidc")
    page = client.get("/account")
    assert "data-link-oidc" not in page.text
    response = client.post(
        "/account/identities/oidc",
        data={"csrf_token": h.web.csrf(page.text), "confirm_password": "not even checked"},
    )
    assert response.status_code == 409
    assert "already linked to Authentik" in response.text
    assert "data-provider-link" not in response.text
    assert not any("oidc_flow" in c for c in response.headers.get_list("set-cookie"))
    assert h.audits("auth.password_confirm_failure") == []  # refused before confirming


def test_an_earlier_issuers_identity_does_not_block_linking(h: Harness) -> None:
    client = admin(h)
    add_identity(h, h.web.admin_email, OLD_ISSUER, "old")
    params = start_link(h, client)
    done = client.get("/auth/oidc/callback", params=params, follow_redirects=False)
    assert done.headers["location"] == "/account?link=linked"


def test_the_callback_refuses_a_second_identity_of_the_issuer(h: Harness) -> None:
    client = admin(h)
    params = start_link(h, client)
    add_identity(h, h.web.admin_email, ISSUER, "another-subject")  # linked meanwhile
    done = client.get("/auth/oidc/callback", params=params, follow_redirects=False)
    assert done.headers["location"] == "/account?link=one_per_provider"
    assert "already linked to Authentik" in client.get(done.headers["location"]).text
    assert h.identities() == [(h.web.admin_email, ISSUER, "another-subject")]


def test_a_second_proxy_identity_is_refused(h: Harness) -> None:
    client = admin(h)
    add_identity(h, h.web.admin_email, PROXY_PROVIDER, "admin-proxy")
    page = client.get("/account", headers=OLA_PROXY)
    assert "data-link-proxy" not in page.text
    response = client.post(
        "/account/identities/proxy",
        data={"csrf_token": h.web.csrf(page.text), "confirm_password": h.web.admin_password},
        headers=OLA_PROXY,
    )
    assert response.status_code == 409 and "already linked to a proxy user" in response.text
    assert h.identities() == [(h.web.admin_email, PROXY_PROVIDER, "admin-proxy")]


# A locked database is handled once, in run_confirmed


def _locked(*_args: object, **_kwargs: object) -> None:
    raise OperationalError("BEGIN IMMEDIATE", {}, sqlite3.OperationalError("database is locked"))


def test_link_oidc_answers_503_when_the_database_is_busy(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = admin(h)
    token = h.token(client)
    with monkeypatch.context() as m:
        m.setattr(confirm, "confirm_sensitive", _locked)
        response = client.post(
            "/account/identities/oidc",
            data={"csrf_token": token, "confirm_password": h.web.admin_password},
        )
    assert response.status_code == 503 and response.headers["retry-after"] == "5"
    assert "The server is busy" in response.text and "data-provider-link" not in response.text


@pytest.mark.parametrize("htmx", [False, True])
def test_logout_others_answers_503_when_the_database_is_busy(
    h: Harness, monkeypatch: pytest.MonkeyPatch, htmx: bool
) -> None:
    client = admin(h)
    token = h.token(client, "/account/password")
    headers = {"HX-Request": "true"} if htmx else {}
    with monkeypatch.context() as m:
        m.setattr(confirm, "confirm_sensitive", _locked)
        response = client.post(
            "/account/logout-others",
            data={"csrf_token": token, "confirm_password": h.web.admin_password},
            headers=headers,
        )
    assert response.status_code == 503 and response.headers["retry-after"] == "5"
    assert "The server is busy" in response.text
    assert ("<html" in response.text) == (not htmx)


# Display names and the link flow's audit


@pytest.mark.parametrize("name", ["Ola\x07", "Ola\u202eadmin", "Ola\nAdmin", "\u2028"])
def test_profile_display_name_refuses_control_and_format_characters(h: Harness, name: str) -> None:
    client = admin(h)
    response = client.post(
        "/account/profile",
        data={
            "display_name": name,
            "locale": "en",
            "theme": "system",
            "csrf_token": h.token(client),
        },
    )
    assert response.status_code == 400 and 'id="display_name-error"' in response.text
    assert "without control or invisible characters" in response.text
    assert h.user(h.web.admin_email).display_name == "Admin"


def test_audited_link_flow_errors_say_via_profile(h: Harness) -> None:
    client = admin(h)
    params = start_link(h, client)
    h.idp.id_token_overrides = {"aud": "someone-else"}  # refused after the code was accepted
    done = client.get("/auth/oidc/callback", params=params, follow_redirects=False)
    assert done.headers["location"] == "/account?link=failed"
    assert h.audits("auth.oidc_failure") == [{"reason": "id_token_invalid", "via": "profile"}]


def test_every_supported_language_has_a_name() -> None:
    assert set(LANGUAGE_NAMES) == set(SUPPORTED_LOCALES)
