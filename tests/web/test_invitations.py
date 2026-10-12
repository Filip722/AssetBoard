"""Invitation links over HTTP (F1.2 part B2)."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError

from assetboard.db.models.identity import Invitation, Membership, User
from assetboard.services.invitations import MAX_FAILED_EMAIL_ATTEMPTS, AlreadyMember, hash_token
from assetboard.web import invite_routes
from tests.conftest import ADMIN_PASSWORD, OwnerApp, WebHarness

TOKEN_IN_LINK = re.compile(r'value="[^"]*/invite/([A-Za-z0-9_-]{43})"')
INVALID = "This invitation link is not valid."
MISMATCH = "This invitation was sent to a different email address."


def create_link(owner: OwnerApp, **form: str) -> str:
    data = {"role": "editor", "email": "", "expires_in_days": "7"} | form
    response = owner.post("/workspace/invitations", data, token_from="/workspace/members")
    assert response.status_code == 200, response.text
    match = TOKEN_IN_LINK.search(response.text)
    assert match, "no invitation link in the page"
    return match.group(1)


def anonymous(owner: OwnerApp) -> TestClient:
    return TestClient(owner.app)


def register(client: TestClient, web: WebHarness, token: str, **form: str) -> Response:
    csrf = web.csrf(client.get("/login").text)  # the anonymous token; invalid invites have no form
    data = {
        "csrf_token": csrf,
        "email": "newbie@example.com",
        "display_name": "Newbie",
        "password": ADMIN_PASSWORD,
    } | form
    return client.post(f"/invite/{token}/register", data=data, follow_redirects=False)


def _alert(html: str) -> str:
    match = re.search(r"<p[^>]*data-invite-error>(.*?)</p>", html, re.DOTALL)
    assert match is not None
    return match.group(1)


# Owners create, list and revoke


def test_create_invitation_shows_link_once_and_stores_only_the_hash(owner: OwnerApp) -> None:
    token = create_link(owner)
    members = owner.client.get("/workspace/members").text
    assert token not in members
    assert members.count("data-invitation=") == 1
    with owner.db() as session:
        invitation = session.scalars(select(Invitation)).one()
        assert invitation.token_hash == hash_token(token)


def test_owner_invitation_requires_an_email_lock(owner: OwnerApp) -> None:
    page = owner.client.get("/workspace/members").text
    assert "Owner invitations must have one" in page
    response = owner.post(
        "/workspace/invitations",
        {"role": "owner", "email": "", "expires_in_days": "7"},
        token_from="/workspace/members",
    )
    assert response.status_code == 400
    assert "Owner invitations must be locked to an email address." in response.text
    assert create_link(owner, role="owner", email="next.owner@example.com")


@pytest.mark.parametrize(
    ("form", "message"),
    [
        ({"expires_in_days": "2"}, "Choose how long the link stays valid."),
        ({"expires_in_days": "²"}, "Choose how long the link stays valid."),
        ({"expires_in_days": "9" * 5000}, "Choose how long the link stays valid."),
        ({"expires_in_days": "-7"}, "Choose how long the link stays valid."),
        ({"role": "admin"}, "Choose a role."),
        ({"email": "nope"}, "Enter a valid email address."),
    ],
)
def test_invitation_form_errors(owner: OwnerApp, form: dict[str, str], message: str) -> None:
    data = {"role": "editor", "email": "", "expires_in_days": "7"} | form
    response = owner.post("/workspace/invitations", data, token_from="/workspace/members")
    assert response.status_code == 400
    assert message in response.text


@pytest.mark.parametrize("role", ["editor", "viewer"])
def test_invitations_require_owner(owner: OwnerApp, role: str) -> None:
    token = owner.token("/workspace/members")
    owner.set_admin_role(role)
    for path in ("/workspace/invitations", f"/workspace/invitations/{uuid4()}/revoke"):
        response = owner.client.post(
            path, data={"csrf_token": token, "role": "viewer", "expires_in_days": "7"}
        )
        assert response.status_code == 403, path


def test_revoke_invitation_and_foreign_invitations_are_404(owner: OwnerApp) -> None:
    token = create_link(owner)
    with owner.db() as session:
        invitation_id = session.scalars(select(Invitation.id)).one()
    response = owner.post(
        f"/workspace/invitations/{invitation_id}/revoke", token_from="/workspace/members"
    )
    assert (response.status_code, response.headers["location"]) == (303, "/workspace/members")
    assert "data-invitation=" not in owner.client.get("/workspace/members").text
    for target in (invitation_id, uuid4(), "malformed"):
        response = owner.post(f"/workspace/invitations/{target}/revoke")
        assert response.status_code == 404
    assert INVALID in anonymous(owner).get(f"/invite/{token}").text


# The invitee


def test_signed_out_invite_page_offers_sign_in_and_registration(owner: OwnerApp) -> None:
    token = create_link(owner, email="locked.person@example.com")
    with anonymous(owner) as client:
        page = client.get(f"/invite/{token}")
    assert page.status_code == 200
    assert "You are invited to join Home as Editor." in page.text
    assert f'href="/login?next=%2Finvite%2F{token}"' in page.text
    assert f'action="/invite/{token}/register"' in page.text
    assert "locked.person@example.com" not in page.text  # never displayed or pre-filled
    assert 'name="email" type="email" value=""' in page.text


def test_sign_in_returns_to_the_invitation(owner: OwnerApp) -> None:
    token = create_link(owner)
    bea = owner.add_user("bea")
    with anonymous(owner) as client:
        login = client.get(f"/login?next=%2Finvite%2F{token}")
        csrf = owner.web.csrf(login.text)
        response = client.post(
            "/login",
            data={
                "csrf_token": csrf,
                "email": "bea@example.com",
                "password": ADMIN_PASSWORD,
                "next": f"/invite/{token}",
            },
            follow_redirects=False,
        )
        assert response.headers["location"] == f"/invite/{token}"
        page = client.get(f"/invite/{token}")
        assert "Accept invitation" in page.text
        accepted = client.post(
            f"/invite/{token}/accept",
            data={"csrf_token": owner.web.csrf(page.text)},
            follow_redirects=False,
        )
        assert (accepted.status_code, accepted.headers["location"]) == (303, "/")
        assert "data-current-workspace>Home<" in client.get("/").text
    assert owner.roles()["bea"] == "editor"
    with owner.db() as session:
        assert session.scalars(select(Invitation.used_by_user_id)).one() == bea


def test_accept_needs_sign_in(owner: OwnerApp) -> None:
    token = create_link(owner)
    with anonymous(owner) as client:
        csrf = owner.web.csrf(client.get(f"/invite/{token}").text)
        response = client.post(
            f"/invite/{token}/accept", data={"csrf_token": csrf}, follow_redirects=False
        )
    assert response.status_code == 303
    assert response.headers["location"] == f"/login?next=%2Finvite%2F{token}"


def test_already_member_is_told_and_the_invitation_stays_unused(owner: OwnerApp) -> None:
    token = create_link(owner, role="viewer")
    page = owner.client.get(f"/invite/{token}")
    response = owner.client.post(
        f"/invite/{token}/accept", data={"csrf_token": owner.web.csrf(page.text)}
    )
    assert response.status_code == 200
    assert "You are already a member of this workspace." in response.text
    assert owner.roles() == {"admin": "owner"}
    with owner.db() as session:
        assert session.scalars(select(Invitation.used_at)).one() is None


def test_signed_in_email_mismatch_is_specific_and_counted(owner: OwnerApp) -> None:
    token = create_link(owner, email="someone.else@example.com")
    owner.add_user("bea")
    with anonymous(owner) as client:
        assert owner.web.sign_in(client, email="bea@example.com").status_code == 303
        page = client.get(f"/invite/{token}")
        response = client.post(
            f"/invite/{token}/accept", data={"csrf_token": owner.web.csrf(page.text)}
        )
    assert response.status_code == 403
    assert MISMATCH in response.text
    assert "someone.else@example.com" not in response.text
    with owner.db() as session:
        assert session.scalars(select(Invitation.failed_email_attempts)).one() == 1


@pytest.fixture
def roomy(web: WebHarness) -> Iterator[OwnerApp]:
    """Like ``owner``, with a registration limit high enough for repeated sign-up attempts."""
    app = web.app(registration_ip_limit=50)
    with TestClient(app) as client:
        web.complete_setup(app)
        assert web.sign_in(client).status_code == 303
        yield OwnerApp(app, client, web)


def test_register_through_invitation_while_registration_is_disabled(roomy: OwnerApp) -> None:
    assert roomy.app.state.settings.allow_registration is False
    token = create_link(roomy, role="viewer")
    with anonymous(roomy) as client:
        response = register(client, roomy.web, token)
        assert (response.status_code, response.headers["location"]) == (303, "/")
        assert "data-current-workspace>Home<" in client.get("/").text
    assert roomy.roles()["newbie"] == "viewer"
    with roomy.db() as session:
        user_id = session.scalars(select(User.id).where(User.email == "newbie@example.com")).one()
        memberships = session.scalars(select(Membership).where(Membership.user_id == user_id))
        assert [m.workspace_id for m in memberships] == [roomy.home_id]  # no own workspace
    with anonymous(roomy) as client:
        assert INVALID in client.get(f"/invite/{token}").text  # single use


def test_registration_page_stays_disabled(roomy: OwnerApp) -> None:
    with anonymous(roomy) as client:
        assert client.get("/register").status_code == 404


def test_register_email_mismatch_is_counted_then_revokes(roomy: OwnerApp) -> None:
    token = create_link(roomy, email="right@example.com")
    with anonymous(roomy) as client:
        for attempt in range(1, MAX_FAILED_EMAIL_ATTEMPTS + 1):
            response = register(client, roomy.web, token)
            if attempt < MAX_FAILED_EMAIL_ATTEMPTS:
                assert response.status_code == 403
                assert MISMATCH in response.text and "right@example.com" not in response.text
            else:
                assert response.status_code == 404
                assert "revoked after too many attempts" in response.text
        response = register(client, roomy.web, token, email="right@example.com")
        assert response.status_code == 404 and INVALID in response.text
    with roomy.db() as session:
        assert (
            session.scalars(select(User.email).where(User.email != "admin@example.com")).all() == []
        )


def test_register_with_an_existing_email_is_rejected(roomy: OwnerApp) -> None:
    token = create_link(roomy)
    roomy.add_user("bea")
    with anonymous(roomy) as client:
        response = register(client, roomy.web, token, email="bea@example.com")
    assert response.status_code == 400
    assert "This email address cannot be registered." in response.text
    with roomy.db() as session:
        assert session.scalars(select(Invitation.used_at)).one() is None


def test_invite_registration_is_throttled(web: WebHarness) -> None:
    app = web.app(registration_ip_limit=1)
    with TestClient(app) as client:
        web.complete_setup(app)
        assert web.sign_in(client).status_code == 303
        owner = OwnerApp(app, client, web)
        token = create_link(owner)
        with anonymous(owner) as visitor:
            assert register(visitor, web, token, password="short").status_code == 400
            throttled = register(visitor, web, token)
        assert throttled.status_code == 429
        assert "Too many registration attempts" in throttled.text


@pytest.mark.parametrize("state", ["expired", "revoked", "used", "unknown", "malformed"])
def test_invalid_invitations_look_the_same(roomy: OwnerApp, state: str) -> None:
    token = create_link(roomy)
    if state == "expired":
        with roomy.db() as session, session.begin():
            past = roomy.app.state.clock.now() - timedelta(seconds=1)
            session.execute(update(Invitation).values(expires_at=past))
    elif state == "revoked":
        with roomy.db() as session:
            invitation_id = session.scalars(select(Invitation.id)).one()
        roomy.post(
            f"/workspace/invitations/{invitation_id}/revoke", token_from="/workspace/members"
        )
    elif state == "used":
        with anonymous(roomy) as first:
            assert register(first, roomy.web, token).status_code == 303
    elif state == "unknown":
        token = "Q" * 43
    else:
        token = "short"
    reference: Any = None
    with anonymous(roomy) as client:
        for response in (
            client.get(f"/invite/{token}"),
            register(client, roomy.web, token, email="late@example.com")
            if state != "malformed"
            else client.get(f"/invite/{token}"),
        ):
            assert response.status_code == 404
            assert INVALID in _alert(response.text)
            if reference is None:
                reference = _alert(response.text)
            assert _alert(response.text) == reference


def test_invite_pages_keep_referrers_same_origin_and_no_store(roomy: OwnerApp) -> None:
    token = create_link(roomy)
    with anonymous(roomy) as client:
        responses = [
            client.get(f"/invite/{token}"),
            client.get("/invite/" + "Z" * 43),
            register(client, roomy.web, token, password="short"),
        ]
    for response in responses:
        assert response.headers["referrer-policy"] == "same-origin"
        assert response.headers["cache-control"] == "no-store"
        assert '<meta name="referrer" content="same-origin">' in response.text


def test_sign_in_and_registration_pages_keep_referrers_same_origin(web: WebHarness) -> None:
    # Their "next" can carry an invitation token.
    app = web.app(allow_registration=True)
    with TestClient(app) as client:
        web.complete_setup(app)
        pages = [
            client.get("/login"),
            client.get("/login?next=%2Finvite%2F" + "Z" * 43),
            client.get("/register"),
        ]
        csrf = web.csrf(pages[0].text)
        failed_login = client.post(
            "/login", data={"csrf_token": csrf, "email": "x@example.com", "password": "nope"}
        )
        pages.append(failed_login)
        other = client.get("/setup")  # a 404 page once setup is closed
    for response in pages:
        assert response.headers["referrer-policy"] == "same-origin", response.request.url
        assert '<meta name="referrer" content="same-origin">' in response.text
    assert other.headers["referrer-policy"] == "strict-origin-when-cross-origin"


def _locked() -> OperationalError:
    return OperationalError("BEGIN IMMEDIATE", {}, sqlite3.OperationalError("database is locked"))


@pytest.mark.parametrize("path", ["accept", "register"])
def test_database_lock_is_503_and_leaves_the_invitation_usable(
    roomy: OwnerApp, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    token = create_link(roomy)

    def locked(*_args: object, **_kwargs: object) -> object:
        raise _locked()

    if path == "accept":
        monkeypatch.setattr(invite_routes, "accept_invitation", locked)
        page = roomy.client.get(f"/invite/{token}")
        response = roomy.client.post(
            f"/invite/{token}/accept", data={"csrf_token": roomy.web.csrf(page.text)}
        )
    else:

        async def locked_async(*_args: object, **_kwargs: object) -> object:
            raise _locked()

        monkeypatch.setattr(invite_routes, "register_with_invitation_async", locked_async)
        with anonymous(roomy) as client:
            response = register(client, roomy.web, token)
    assert response.status_code == 503
    assert response.headers["retry-after"] == "5"
    assert "The server is busy. Try again in a few seconds." in response.text
    assert INVALID not in response.text
    monkeypatch.undo()
    with anonymous(roomy) as client:
        assert "You are invited to join Home" in client.get(f"/invite/{token}").text


def test_already_member_page_renders_without_a_preview(
    owner: OwnerApp, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = create_link(owner)
    page = owner.client.get(f"/invite/{token}")

    def already(*_args: object, **_kwargs: object) -> object:
        raise AlreadyMember

    async def no_preview(*_args: object) -> None:
        return None

    monkeypatch.setattr(invite_routes, "accept_invitation", already)
    monkeypatch.setattr(invite_routes, "_preview", no_preview)
    response = owner.client.post(
        f"/invite/{token}/accept", data={"csrf_token": owner.web.csrf(page.text)}
    )
    assert response.status_code == 200
    assert "You are already a member of this workspace." in response.text
    assert "data-invite-summary" not in response.text


@pytest.mark.parametrize("locale", ["en", "pl"])
def test_invite_page_renders_in_english_and_polish(roomy: OwnerApp, locale: str) -> None:
    token = create_link(roomy)
    with anonymous(roomy) as client:
        page = client.get(f"/invite/{token}", headers={"Accept-Language": locale}).text
    if locale == "pl":
        assert "Zaproszenie" in page and "Edytor" in page
    else:
        assert "You are invited to join Home as Editor." in page


def test_pages_with_post_forms_never_use_no_referrer(roomy: OwnerApp) -> None:
    """Under "no-referrer" browsers send ``Origin: null`` on POSTs; the Origin check refuses it."""
    token = create_link(roomy)
    pages = [roomy.client.get(path) for path in ("/", "/workspace/members", "/account/password")]
    pages.append(roomy.client.get(f"/invite/{token}"))
    with anonymous(roomy) as client:
        pages += [client.get("/login"), client.get(f"/invite/{token}")]
    for page in pages:
        assert 'method="post"' in page.text, page.request.url
        assert page.headers["referrer-policy"] != "no-referrer", page.request.url
        assert 'content="no-referrer"' not in page.text, page.request.url


def test_invite_forms_pass_the_origin_check_with_a_browser_origin(roomy: OwnerApp) -> None:
    token = create_link(roomy)
    origin = {"Origin": roomy.app.state.settings.base_url.rstrip("/")}
    with anonymous(roomy) as client:
        csrf = roomy.web.csrf(client.get("/login").text)
        response = client.post(
            f"/invite/{token}/register",
            data={
                "csrf_token": csrf,
                "email": "newbie@example.com",
                "display_name": "Newbie",
                "password": ADMIN_PASSWORD,
            },
            headers=origin,
            follow_redirects=False,
        )
        assert response.status_code == 303
        null_origin = client.post(
            f"/invite/{token}/accept", data={"csrf_token": csrf}, headers={"Origin": "null"}
        )
        assert null_origin.status_code == 403  # what "no-referrer" pages would trigger
