"""Instance administration of user accounts (F1.5 part B)."""

from __future__ import annotations

import io
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from assetboard.db.models.identity import (
    AuditEvent,
    ExternalIdentity,
    Membership,
    User,
    Workspace,
)
from assetboard.services.external_identity import PROXY_PROVIDER
from assetboard.services.hash_gate import HashGateBusy
from assetboard.web import confirm
from tests.oidc.fake_idp import ISSUER
from tests.web.test_profile_routes import (
    LOCAL_PASSWORD,
    OLA_PROXY,
    Harness,
    add_identity,
    add_user,
    admin,
    continue_url,
    harness,
)

if TYPE_CHECKING:
    from conftest import WebHarness

OLA = "ola@example.com"


@pytest.fixture
def h(web: WebHarness) -> Iterator[Harness]:
    with harness(web, oidc_link_by_email=True) as value:
        yield value


@contextmanager
def captured_log(h: Harness) -> Iterator[io.StringIO]:
    logger: logging.Logger = h.app.state.logger
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    logger.addHandler(handler)
    try:
        yield output
    finally:
        logger.removeHandler(handler)


def user_id(h: Harness, email: str) -> Any:
    return h.user(email).id


def add_member(h: Harness, email: str, role: str) -> Any:
    member_id = add_user(h, email)
    with h.app.state.session_factory.begin() as session:
        workspace = session.scalars(select(Workspace)).one()
        session.add(Membership(workspace_id=workspace.id, user_id=member_id, role=role))
    return member_id


def act(
    h: Harness, client: TestClient, target: Any, action: str, password: str | None = None
) -> Any:
    page = client.get(f"/admin/users/{target}/{action}")
    return client.post(
        f"/admin/users/{target}/{action}",
        data={
            "csrf_token": h.web.csrf(page.text),
            "confirm_password": h.web.admin_password if password is None else password,
        },
    )


def users_snapshot(h: Harness) -> list[tuple[Any, ...]]:
    with h.db() as session:
        rows = session.execute(
            select(User.email, User.is_active, User.is_instance_admin, User.password_hash)
        )
        return sorted(tuple(row) for row in rows)


# Authorization


@pytest.mark.parametrize("role", ["owner", "editor", "viewer"])
def test_admin_pages_need_an_instance_administrator(h: Harness, role: str) -> None:
    add_member(h, "member@example.com", role)
    target = user_id(h, h.web.admin_email)
    client = h.client()
    h.web.sign_in(client, email="member@example.com", password=LOCAL_PASSWORD)
    token = h.token(client, "/account")
    assert "data-admin-link" not in client.get("/account").text
    for path in ("/admin/users", "/admin/users/new", f"/admin/users/{target}/demote"):
        assert client.get(path).status_code == 403, path
    for path in ("/admin/users/new", f"/admin/users/{target}/demote"):
        response = client.post(path, data={"csrf_token": token, "confirm_password": LOCAL_PASSWORD})
        assert response.status_code == 403, path
    assert h.user(h.web.admin_email).is_instance_admin


def test_signed_out_visitors_are_sent_to_sign_in(h: Harness) -> None:
    response = h.client().get("/admin/users", follow_redirects=False)
    assert response.headers["location"] == "/login?next=%2Fadmin%2Fusers"


def test_users_list_shows_sign_in_methods_never_subjects(h: Harness) -> None:
    add_user(h, OLA)
    add_identity(h, OLA, ISSUER, "secret-subject-1234")
    add_identity(h, OLA, PROXY_PROVIDER, "secret-proxy-user")
    with h.app.state.session_factory.begin() as session:
        for identity in session.scalars(select(ExternalIdentity)):
            identity.email_at_link = "provider-only@example.net"
    client = admin(h)
    assert "data-admin-link" in client.get("/account").text
    page = client.get("/admin/users").text
    row = page.split(f'data-user="{OLA}"')[1].split("</tr>")[0]
    assert "Password" in row and "Identity provider" in row and "Proxy" in row
    for secret in ("secret-subject-1234", "secret-proxy-user", "provider-only@example.net"):
        assert secret not in page


@pytest.mark.parametrize("path", ["/admin/users/{id}/explode", "/admin/users/not-a-uuid/activate"])
def test_unknown_actions_and_users_are_404(h: Harness, path: str) -> None:
    client = admin(h)
    target = user_id(h, h.web.admin_email)
    assert client.get(path.replace("{id}", str(target))).status_code == 404
    missing = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/admin/users/{missing}/activate").status_code == 404


# Every admin mutation goes through the one confirmation path


def test_a_throttled_admin_confirmation_says_how_long_to_wait(h: Harness) -> None:
    add_user(h, OLA)
    target = user_id(h, OLA)
    client = admin(h)
    free = h.app.state.settings.auth_account_free_failures
    wrong = [act(h, client, target, "deactivate", "wrong password") for _ in range(free + 1)]
    right = act(h, client, target, "deactivate")
    assert all(r.status_code == 401 for r in [*wrong, right])
    assert "The password is not correct." in wrong[-1].text
    assert "Too many attempts" not in wrong[-1].text
    assert "Too many attempts. Try again in 1 minute." in right.text
    assert right.headers["retry-after"] == "60"
    with h.app.state.session_factory() as session:
        user = session.get(User, target)
        assert user is not None and user.is_active


@pytest.mark.parametrize("action", ["create", "activate", "deactivate", "promote", "demote"])
@pytest.mark.parametrize("password", ["", "wrong password"])
def test_every_admin_mutation_needs_confirmation(h: Harness, action: str, password: str) -> None:
    add_user(h, OLA)
    target = user_id(h, OLA)
    with h.app.state.session_factory.begin() as session:
        user = session.get(User, target)
        assert user is not None
        user.is_active = action != "activate"
        user.is_instance_admin = action == "demote"
    client = admin(h)
    before = users_snapshot(h)
    if action == "create":
        token = h.token(client, "/admin/users/new")
        response = client.post(
            "/admin/users/new",
            data={
                "csrf_token": token,
                "email": "new@example.com",
                "display_name": "New",
                "confirm_password": password,
            },
        )
        assert "data-temporary-password" not in response.text
    else:
        response = act(h, client, target, action, password)
    assert response.status_code == 401
    assert users_snapshot(h) == before
    assert h.audits("auth.password_confirm_failure")
    assert not any(h.audits(f"admin.{name}") for name in ("user_created", "user_activated"))


def test_a_busy_database_answers_503(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    add_user(h, OLA)
    client = admin(h)

    def locked(*_args: object, **_kwargs: object) -> None:
        raise OperationalError(
            "BEGIN IMMEDIATE", {}, sqlite3.OperationalError("database is locked")
        )

    target = user_id(h, OLA)
    page = client.get(f"/admin/users/{target}/deactivate")
    with monkeypatch.context() as m:
        m.setattr(confirm, "confirm_sensitive", locked)
        response = client.post(
            f"/admin/users/{target}/deactivate",
            data={"csrf_token": h.web.csrf(page.text), "confirm_password": h.web.admin_password},
        )
    assert response.status_code == 503 and response.headers["retry-after"] == "5"
    assert h.user(OLA).is_active


# Deactivation


def test_deactivation_ends_every_session_and_every_sign_in(h: Harness) -> None:
    add_user(h, OLA)
    add_identity(h, OLA, ISSUER, "user-123")  # the fake provider's subject
    add_identity(h, OLA, PROXY_PROVIDER, "ola")
    h.web.sign_in(h.client(), email=OLA, password=LOCAL_PASSWORD)
    assert h.oidc_sign_in(h.client()).headers["location"] == "/"
    h.proxy_sign_in(h.client(), OLA_PROXY)
    assert sorted(m for e, m in h.sessions() if e == OLA) == ["oidc", "password", "proxy"]

    response = act(h, admin(h), user_id(h, OLA), "deactivate")
    assert response.status_code == 200 and "data-admin-notice" in response.text
    assert [e for e, _m in h.sessions() if e == OLA] == []
    assert h.audits("admin.user_deactivated") == [{"sessions_revoked": 3}]
    assert not h.user(OLA).is_active
    # No way back in: password, identity provider and proxy all refuse.
    assert h.web.sign_in(h.client(), email=OLA, password=LOCAL_PASSWORD).status_code == 401
    assert h.oidc_sign_in(h.client()).headers["location"] == "/auth/oidc/failed/inactive"
    proxy = h.client().get("/auth/proxy", headers=OLA_PROXY, follow_redirects=False)
    assert proxy.headers["location"] == "/auth/proxy/failed/inactive"


def test_deactivation_stops_an_in_flight_login_link(h: Harness) -> None:
    add_user(h, OLA)  # a password, nothing linked: OIDC sign-in asks for the password
    browser = h.client()
    assert h.oidc_sign_in(browser).headers["location"] == "/login/link"
    page = browser.get("/login/link")
    act(h, admin(h), user_id(h, OLA), "deactivate")
    response = browser.post(
        "/login/link",
        data={"csrf_token": h.web.csrf(page.text), "password": LOCAL_PASSWORD},
    )
    assert response.status_code == 401
    assert h.identities() == [] and [e for e, _m in h.sessions() if e == OLA] == []


def test_deactivation_stops_an_in_flight_profile_link(h: Harness) -> None:
    add_user(h, OLA)
    browser = h.client()
    h.web.sign_in(browser, email=OLA, password=LOCAL_PASSWORD)
    page = browser.post(
        "/account/identities/oidc",
        data={"csrf_token": h.token(browser), "confirm_password": LOCAL_PASSWORD},
    )
    params = h.idp.authorize(continue_url(page))
    act(h, admin(h), user_id(h, OLA), "deactivate")
    done = browser.get("/auth/oidc/callback", params=params, follow_redirects=False)
    assert done.headers["location"] == "/account?link=failed"
    assert h.identities() == []


# Activate, promote, demote; self and last-administrator protection


def test_activate_promote_and_demote_with_their_audit_data(h: Harness) -> None:
    add_user(h, OLA)
    target = user_id(h, OLA)
    client = admin(h)
    assert "Done." in act(h, client, target, "deactivate").text
    assert "Done." in act(h, client, target, "activate").text
    assert "already in that state" in act(h, client, target, "activate").text
    assert "Done." in act(h, client, target, "promote").text
    assert h.user(OLA).is_instance_admin
    assert "Done." in act(h, client, target, "demote").text
    assert h.audits("admin.user_activated") == [{}]
    assert h.audits("admin.admin_granted") == [{}]
    assert h.audits("admin.admin_revoked") == [{}]
    assert h.user(OLA).is_active and not h.user(OLA).is_instance_admin


@pytest.mark.parametrize("action", ["deactivate", "demote"])
def test_administrators_cannot_deactivate_or_demote_themselves(h: Harness, action: str) -> None:
    client = admin(h)
    response = act(h, client, user_id(h, h.web.admin_email), action)
    assert response.status_code == 409 and "your own account" in response.text
    me = h.user(h.web.admin_email)
    assert me.is_active and me.is_instance_admin


# Creating an account with a temporary password


def create(
    h: Harness, client: TestClient, email: str, name: str, password: str | None = None
) -> Any:
    token = h.token(client, "/admin/users/new")
    return client.post(
        "/admin/users/new",
        data={
            "csrf_token": token,
            "email": email,
            "display_name": name,
            "confirm_password": h.web.admin_password if password is None else password,
        },
    )


def temporary_password(response: Any) -> str:
    return response.text.split("data-temporary-password>")[1].split("</p>")[0].strip()


def test_create_shows_the_temporary_password_once_and_keeps_it_nowhere(h: Harness) -> None:
    client = admin(h)
    with captured_log(h) as log:
        response = create(h, client, "New@Example.com", "  New   Person ")
    assert response.status_code == 200
    password = temporary_password(response)
    assert len(password) >= 16
    # Shown in the POST response only: no redirect, no cookie, never cached.
    assert response.headers["cache-control"] == "no-store"
    assert not response.headers.get_list("set-cookie")
    assert password not in log.getvalue()
    created = h.user("new@example.com")
    assert created.display_name == "New Person" and created.must_change_password
    assert created.locale is None and not created.is_instance_admin
    with h.db() as session:
        assert (
            session.scalars(select(Membership).where(Membership.user_id == created.id)).all() == []
        )
        for event in session.scalars(select(AuditEvent)):
            assert password not in str(event.data) and password not in (event.user_agent or "")
    assert h.audits("admin.user_created") == [{"temporary_password": True}]
    assert password not in client.get("/admin/users").text


@pytest.mark.parametrize(
    ("email", "name", "field"),
    [
        ("not-an-email", "New", "email"),
        ("new@example.com", "New‮admin", "display_name"),
        ("new@example.com", "New‌", "display_name"),
        ("new@example.com", "   ", "display_name"),
    ],
)
def test_create_validates_email_and_display_name(
    h: Harness, email: str, name: str, field: str
) -> None:
    response = create(h, admin(h), email, name)
    assert response.status_code == 400 and f'id="{field}-error"' in response.text
    assert "data-temporary-password" not in response.text
    with h.db() as session:
        assert session.scalars(select(User).where(User.email == "new@example.com")).first() is None


def test_create_refuses_an_email_that_is_taken(h: Harness) -> None:
    add_user(h, OLA)
    response = create(h, admin(h), OLA, "Ola Again")
    assert response.status_code == 409 and "already exists" in response.text


def test_create_is_404_under_oidc_only(web: WebHarness) -> None:
    with harness(web, oidc_only=True, allow_local_admin_login=True) as h:
        client = h.client()
        token = h.web.csrf(client.get("/login/local").text)
        client.post(
            "/login/local",
            data={
                "email": h.web.admin_email,
                "password": h.web.admin_password,
                "csrf_token": token,
            },
        )
        assert client.get("/admin/users").status_code == 200
        assert "data-admin-create" not in client.get("/admin/users").text
        assert client.get("/admin/users/new").status_code == 404
        response = client.post(
            "/admin/users/new",
            data={"csrf_token": h.token(client), "email": "x@example.com", "display_name": "X"},
        )
        assert response.status_code == 404


# The temporary password must be changed first


def signed_in_with_temporary_password(h: Harness) -> tuple[TestClient, str]:
    password = temporary_password(create(h, admin(h), "new@example.com", "New Person"))
    client = h.client()
    assert h.web.sign_in(client, email="new@example.com", password=password).status_code == 303
    return client, password


def test_a_temporary_password_session_can_only_change_the_password(h: Harness) -> None:
    client, password = signed_in_with_temporary_password(h)
    for path in ("/", "/account", "/workspace/members"):
        response = client.get(path, follow_redirects=False)
        assert response.headers["location"] == "/account/password", path
    head = client.head("/", follow_redirects=False)
    assert head.headers["location"] == "/account/password"
    htmx = client.get("/account", headers={"HX-Request": "true"}, follow_redirects=False)
    assert htmx.status_code == 204 and htmx.headers["hx-redirect"] == "/account/password"
    page = client.get("/account/password")
    assert page.status_code == 200 and "data-temporary-password-notice" in page.text
    token = h.web.csrf(page.text)
    refused = client.post("/account/profile", data={"csrf_token": token, "display_name": "X"})
    assert refused.status_code == 403 and "temporary password" in refused.text
    assert client.get("/healthz").status_code == 200
    assert client.get("/static/css/app.css").status_code == 200

    changed = client.post(
        "/account/password",
        data={
            "csrf_token": token,
            "current_password": password,
            "new_password": "A Brand New Passphrase",
            "confirm_password": "A Brand New Passphrase",
        },
    )
    assert changed.status_code == 200
    assert not h.user("new@example.com").must_change_password
    # A new account without a workspace or invitation sees the "no workspace" page.
    home = client.get("/", follow_redirects=False)
    assert home.status_code == 403 and "not a member of any workspace" in home.text


def test_sign_out_works_with_a_temporary_password(h: Harness) -> None:
    client, _password = signed_in_with_temporary_password(h)
    token = h.web.csrf(client.get("/account/password").text)
    response = client.post("/logout", data={"csrf_token": token}, follow_redirects=False)
    assert response.headers["location"] == "/login"


def test_the_temporary_password_rule_applies_to_password_sessions_only(h: Harness) -> None:
    created = temporary_password(create(h, admin(h), OLA, "Ola"))
    assert created
    add_identity(h, OLA, PROXY_PROVIDER, "ola")
    client = h.client()
    h.proxy_sign_in(client, OLA_PROXY)  # a proxy session for the same account
    assert client.get("/account", headers=OLA_PROXY, follow_redirects=False).status_code == 200


def test_a_temporary_password_cannot_be_changed_to_itself(h: Harness) -> None:
    client, password = signed_in_with_temporary_password(h)
    page = client.get("/account/password")
    response = client.post(
        "/account/password",
        data={
            "csrf_token": h.web.csrf(page.text),
            "current_password": password,
            "new_password": password,
            "confirm_password": password,
        },
    )
    assert response.status_code == 400
    assert "differs from the current one" in response.text
    assert h.user("new@example.com").must_change_password
    assert client.get("/", follow_redirects=False).headers["location"] == "/account/password"


def test_the_temporary_password_is_hashed_through_the_hash_gate(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = admin(h)
    gate = h.app.state.hash_gate
    real_run = gate.run
    calls: list[str] = []

    async def counting(operation: Any) -> Any:
        calls.append("hash")
        return await real_run(operation)

    monkeypatch.setattr(gate, "run", counting)
    token = h.token(client, "/admin/users/new")
    calls.clear()  # the page load above verified nothing
    response = client.post(
        "/admin/users/new",
        data={
            "csrf_token": token,
            "email": "new@example.com",
            "display_name": "New",
            "confirm_password": h.web.admin_password,
        },
    )
    assert response.status_code == 200
    # One hash for the new account, one verification of the administrator's password.
    assert len(calls) == 2


def test_a_full_hash_gate_answers_503_and_creates_nothing(
    h: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = admin(h)
    token = h.token(client, "/admin/users/new")

    async def busy(_operation: Any) -> Any:
        raise HashGateBusy

    monkeypatch.setattr(h.app.state.hash_gate, "run", busy)
    response = client.post(
        "/admin/users/new",
        data={
            "csrf_token": token,
            "email": "new@example.com",
            "display_name": "New",
            "confirm_password": h.web.admin_password,
        },
    )
    assert response.status_code == 503 and response.headers["retry-after"] == "5"
    with h.db() as session:
        assert session.scalars(select(User).where(User.email == "new@example.com")).first() is None


# OIDC_ADMIN_GROUP: the provider decides the administrator role at each sign-in


@pytest.mark.parametrize("group", [None, "assetboard-admins"])
def test_promote_and_demote_say_when_the_provider_decides(
    web: WebHarness, group: str | None
) -> None:
    with harness(web, oidc_admin_group=group) as g:
        add_user(g, OLA)
        add_identity(g, OLA, ISSUER, "ola-subject")
        add_user(g, "local@example.com")
        client = admin(g)
        target = user_id(g, OLA)
        notice = "The identity provider decides this at each sign-in"
        for action in ("promote", "demote"):
            page = client.get(f"/admin/users/{target}/{action}")
            assert ("data-group-notice" in page.text) == (group is not None), action
            done = act(g, client, target, action)
            assert "Done." in done.text
            assert (notice in done.text) == (group is not None), action
        # Deactivation never carries the notice: it does not depend on the group.
        assert "data-group-notice" not in client.get(f"/admin/users/{target}/deactivate").text
        assert notice not in act(g, client, target, "deactivate").text
        listing = client.get("/admin/users").text
        ola_row = listing.split(f'data-user="{OLA}"')[1].split("</tr>")[0]
        local_row = listing.split('data-user="local@example.com"')[1].split("</tr>")[0]
        assert ("(from the identity provider)" in ola_row) == (group is not None)
        assert "(from the identity provider)" not in local_row  # no OIDC identity
