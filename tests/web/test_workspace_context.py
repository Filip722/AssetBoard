"""The per-request Context over HTTP: selection, fallback, roles, 403 and 404 pages (F1.2)."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from assetboard.db.models.identity import Membership, User, UserSession, Workspace
from assetboard.db.repositories import MembershipRepository
from assetboard.services.auth import session_id_hash
from assetboard.services.context import Role, require_role, select_workspace
from assetboard.web.context import CurrentContext
from assetboard.web.security import session_cookie_name
from tests.conftest import WebHarness

NOT_FOUND_MESSAGE = "The page you requested could not be found."
NO_WORKSPACE_MESSAGE = (
    "Your account is not a member of any workspace. Ask a workspace owner to invite you."
)


def add_probe_routes(app: FastAPI) -> None:
    """Test-only routes that exercise the Context dependency, the role guard and a repository."""

    async def whoami(ctx: CurrentContext) -> dict[str, str]:
        return {"workspace": ctx.workspace.name, "role": ctx.role.value}

    async def owners_only(ctx: CurrentContext) -> dict[str, str]:
        require_role(ctx, Role.OWNER)
        return {"ok": "yes"}

    def member(member_id: UUID, ctx: CurrentContext) -> dict[str, str]:
        with app.state.session_factory() as session:
            row = MembershipRepository(session, ctx).get_or_raise(member_id)
            return {"role": row.role}

    app.add_api_route("/probe/context", whoami)
    app.add_api_route("/probe/owners", owners_only)
    app.add_api_route("/probe/members/{member_id}", member)


@dataclass
class Signed:
    app: FastAPI
    client: TestClient
    web: WebHarness

    def db(self) -> Session:
        return self.app.state.session_factory()

    @property
    def session_hash(self) -> str:
        cookie = self.client.cookies[session_cookie_name(self.app.state.settings)]
        return session_id_hash(cookie)

    def admin(self, session: Session) -> User:
        return session.scalars(select(User).where(User.email == self.web.admin_email)).one()

    def add_workspace(self, name: str, *, member: User | None = None, role: str = "viewer") -> UUID:
        with self.db() as session, session.begin():
            workspace = Workspace(name=name, base_currency="PLN", locale="pl", timezone="UTC")
            session.add(workspace)
            session.flush()
            if member is not None:
                session.add(Membership(workspace_id=workspace.id, user_id=member.id, role=role))
            return workspace.id


@pytest.fixture
def signed(web: WebHarness) -> Iterator[Signed]:
    app = web.app()
    add_probe_routes(app)
    with TestClient(app) as client:
        web.complete_setup(app)
        assert web.sign_in(client).status_code == 303
        yield Signed(app, client, web)


def test_context_uses_the_workspace_selected_in_the_session(signed: Signed) -> None:
    assert signed.client.get("/probe/context").json() == {"workspace": "Home", "role": "owner"}
    with signed.db() as session:
        admin = signed.admin(session)
    second = signed.add_workspace("Second", member=admin, role="viewer")
    with signed.db() as session, session.begin():
        select_workspace(
            session, session_hash=signed.session_hash, user_id=admin.id, workspace_id=second
        )
    assert signed.client.get("/probe/context").json() == {"workspace": "Second", "role": "viewer"}


def test_context_falls_back_when_the_selected_workspace_is_not_a_membership(
    signed: Signed,
) -> None:
    with signed.db() as session:
        admin = signed.admin(session)
    second = signed.add_workspace("Second", member=admin, role="editor")
    with signed.db() as session, session.begin():
        select_workspace(
            session, session_hash=signed.session_hash, user_id=admin.id, workspace_id=second
        )
        session.execute(delete(Membership).where(Membership.workspace_id == second))
    assert signed.client.get("/probe/context").json() == {"workspace": "Home", "role": "owner"}
    with signed.db() as session:
        row = session.get(UserSession, signed.session_hash)
        assert row is not None and row.current_workspace_id == second  # fallback not stored


def test_home_page_uses_the_workspace_currency(signed: Signed) -> None:
    # The instance default (DEFAULT_CURRENCY) stays EUR; only the workspace currency changes.
    assert signed.app.state.settings.default_currency == "EUR"
    assert "€0.00" in signed.client.get("/").text
    with signed.db() as session, session.begin():
        session.execute(update(Workspace).values(base_currency="JPY"))
    page = signed.client.get("/")
    assert page.status_code == 200
    assert "¥0" in page.text and "€" not in page.text


def test_role_change_applies_on_the_next_request(signed: Signed) -> None:
    assert signed.client.get("/probe/owners").status_code == 200
    with signed.db() as session, session.begin():
        session.execute(update(Membership).values(role="viewer"))
    denied = signed.client.get("/probe/owners")
    assert denied.status_code == 403
    assert "You do not have permission to view this page." in denied.text
    toast = signed.client.get("/probe/owners", headers={"HX-Request": "true"})
    assert toast.status_code == 403
    assert toast.headers["HX-Retarget"] == "#toast-container"
    assert signed.client.get("/probe/context").json()["role"] == "viewer"


def test_user_without_memberships_sees_no_workspace_page(signed: Signed) -> None:
    with signed.db() as session, session.begin():
        session.execute(delete(Membership))
    for path in ("/", "/probe/context"):
        page = signed.client.get(path)
        assert page.status_code == 403
        assert NO_WORKSPACE_MESSAGE in page.text
    # Account pages and sign-out do not need a workspace.
    account = signed.client.get("/account/password")
    assert account.status_code == 200
    token = signed.web.csrf(account.text)
    logout = signed.client.post("/logout", data={"csrf_token": token}, follow_redirects=False)
    assert logout.status_code == 303
    assert signed.client.get("/", follow_redirects=False).headers["location"].startswith("/login")


def test_no_workspace_page_is_translated(signed: Signed) -> None:
    with signed.db() as session, session.begin():
        session.execute(delete(Membership))
    page = signed.client.get("/", headers={"Accept-Language": "pl"})
    assert page.status_code == 403
    assert "Brak obszaru roboczego" in page.text
    assert (
        "Twoje konto nie należy do żadnego obszaru roboczego. Poproś właściciela obszaru "
        "roboczego o zaproszenie." in page.text
    )
    assert NO_WORKSPACE_MESSAGE not in page.text


def test_rows_of_another_workspace_are_404_without_a_workspace_hint(signed: Signed) -> None:
    stranger_id = uuid4()
    with signed.db() as session, session.begin():
        stranger = User(id=stranger_id, email="stranger@example.com", display_name="Stranger")
        session.add(stranger)
    foreign = signed.add_workspace("Secret Workspace")
    with signed.db() as session, session.begin():
        row = Membership(workspace_id=foreign, user_id=stranger_id, role="owner")
        session.add(row)
        session.flush()
        foreign_member = row.id
        own_member = session.scalars(
            select(Membership.id).where(Membership.workspace_id != foreign)
        ).one()

    assert signed.client.get(f"/probe/members/{own_member}").json() == {"role": "owner"}
    responses: list[Any] = [
        signed.client.get(f"/probe/members/{foreign_member}"),
        signed.client.get(f"/probe/members/{uuid4()}"),
    ]
    for response in responses:
        assert response.status_code == 404
        assert NOT_FOUND_MESSAGE in response.text
        assert "Secret Workspace" not in response.text
        assert str(foreign) not in response.text and foreign.hex not in response.text
    # A foreign row and a row that never existed get the very same error panel.
    foreign_panel, missing_panel = (_alert_panel(response.text) for response in responses)
    assert foreign_panel == missing_panel


def _alert_panel(html: str) -> str:
    match = re.search(r'<section[^>]*role="alert">.*?</section>', html, re.DOTALL)
    assert match is not None
    return match.group(0)
