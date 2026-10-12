"""Races around invitations, made deterministic with the services' checkpoint seams."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select, update

from assetboard.config import Settings
from assetboard.db.models.identity import ExternalIdentity, Invitation, SetupState, User
from assetboard.db.repositories import NotFound
from assetboard.services import invitations, workspaces
from assetboard.services.auth import RegistrationOutcome, register_with_invitation
from assetboard.services.invitations import InvitationInvalid, accept_invitation
from assetboard.services.oidc import OidcIdentity
from assetboard.services.oidc_accounts import OidcSignIn, sign_in_with_oidc
from assetboard.services.workspaces import (
    MAX_PENDING_INVITATIONS,
    TooManyInvitations,
    create_invitation,
    list_invitations,
    revoke_invitation,
)
from tests.tenant.serialize import serialize_at_checkpoint
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


@pytest.fixture
def db(tmp_path: Path) -> Iterator[WorkspaceDb]:
    yield from workspace_db(tmp_path)


def test_concurrent_accepts_create_one_membership(
    db: WorkspaceDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    db.add_member("olga", "owner")
    for key in ("amy", "ben"):
        db.add_member(key, "owner", workspace_id=db.add_workspace(f"{key}-own"))
    token = create_invitation(
        db.factory, db.ctx("olga", "owner"), role="editor", email=None, expires_in_days=7
    ).token

    def accept(key: str) -> object:
        return accept_invitation(
            db.factory,
            token,
            user_id=db.users[key],
            user_email=f"{key}@example.com",
            clock=db.clock,
        )

    outcomes = serialize_at_checkpoint(
        db.engine, monkeypatch, invitations, lambda: accept("amy"), lambda: accept("ben")
    )
    assert outcomes["first"] is None
    assert isinstance(outcomes["second"], InvitationInvalid)
    assert db.roles() == {"olga": "owner", "amy": "editor"}


# Two sign-ups on one link


def _sign_up(db: WorkspaceDb, settings: Settings, token: str, email: str) -> RegistrationOutcome:
    return register_with_invitation(
        db.factory,
        token=token,
        email=email,
        display_name=email.split("@")[0].title(),
        password="Correct Horse Battery!",
        settings=settings,
        clock=db.clock,
        host="192.0.2.10",
    )


def _close_setup(db: WorkspaceDb) -> None:
    """Sign-up is refused while first-run setup is open; this database skipped setup."""
    with db.factory.begin() as session:
        session.execute(update(SetupState).values(completed_at=db.clock.now()))


def _users(db: WorkspaceDb) -> set[str]:
    with db.factory() as session:
        return set(session.scalars(select(User.email)))


def test_concurrent_sign_ups_on_one_link_create_one_user(
    db: WorkspaceDb, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path, registration_ip_limit=50)
    _close_setup(db)
    db.add_member("olga", "owner")
    token = create_invitation(
        db.factory, db.ctx("olga", "owner"), role="viewer", email=None, expires_in_days=7
    ).token
    results: dict[str, RegistrationOutcome] = {}

    def sign_up(name: str) -> None:
        results[name] = _sign_up(db, settings, token, f"{name}@example.com")

    outcomes = serialize_at_checkpoint(
        db.engine, monkeypatch, invitations, lambda: sign_up("first"), lambda: sign_up("second")
    )
    assert outcomes == {"first": None, "second": None}
    assert results["first"].status == "created"
    assert results["second"].status == "invitation_invalid"
    assert _users(db) == {"olga@example.com", "first@example.com"}  # no orphan user
    assert db.roles() == {"olga": "owner", "first": "viewer"}


def test_sign_up_rolls_back_the_user_when_the_link_was_used_meanwhile(
    db: WorkspaceDb, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The pre-check passed, then another sign-up used the link before this one's transaction."""
    settings = Settings(_env_file=None, data_dir=tmp_path, registration_ip_limit=50)
    _close_setup(db)
    db.add_member("olga", "owner")
    token = create_invitation(
        db.factory, db.ctx("olga", "owner"), role="viewer", email=None, expires_in_days=7
    ).token
    assert _sign_up(db, settings, token, "first@example.com").status == "created"
    monkeypatch.setattr(invitations, "check_for_sign_up", lambda *_args, **_kwargs: None)
    outcome = _sign_up(db, settings, token, "second@example.com")
    assert outcome.status == "invitation_invalid"
    assert _users(db) == {"olga@example.com", "first@example.com"}


def test_concurrent_oidc_sign_ups_on_one_link_create_one_user(
    db: WorkspaceDb, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        registration_ip_limit=50,
        base_url="https://money.example.test",
        oidc_issuer="https://idp.example.test",
        oidc_client_id="assetboard",
    )
    _close_setup(db)
    db.add_member("olga", "owner")
    token = create_invitation(
        db.factory, db.ctx("olga", "owner"), role="viewer", email=None, expires_in_days=7
    ).token
    results: dict[str, OidcSignIn] = {}

    def sign_up(name: str) -> None:
        who = OidcIdentity(
            issuer="https://idp.example.test",
            subject=name,
            email=f"{name}@example.com",
            email_verified=True,
            display_name=name,
            groups=None,
        )
        results[name] = sign_in_with_oidc(
            db.factory,
            who,
            next_path=f"/invite/{token}",
            settings=settings,
            clock=db.clock,
            host="192.0.2.10",
        )

    outcomes = serialize_at_checkpoint(
        db.engine, monkeypatch, invitations, lambda: sign_up("first"), lambda: sign_up("second")
    )
    assert outcomes == {"first": None, "second": None}
    assert results["first"].status == "signed_in"
    assert results["second"].status == "invitation_invalid"
    # The second user and identity were rolled back with the failed invitation use.
    assert _users(db) == {"olga@example.com", "first@example.com"}
    with db.factory() as session:
        assert session.scalars(select(ExternalIdentity.subject)).all() == ["first"]
    assert db.roles() == {"olga": "owner", "first": "viewer"}


# Revoke versus accept


@pytest.mark.parametrize("first", ["accept", "revoke"])
def test_revoke_and_accept_race_has_one_winner(
    db: WorkspaceDb, monkeypatch: pytest.MonkeyPatch, first: str
) -> None:
    db.add_member("olga", "owner")
    db.add_member("amy", "owner", workspace_id=db.add_workspace("amy-own"))
    created = create_invitation(
        db.factory, db.ctx("olga", "owner"), role="editor", email=None, expires_in_days=7
    )

    def accept() -> object:
        return accept_invitation(
            db.factory,
            created.token,
            user_id=db.users["amy"],
            user_email="amy@example.com",
            clock=db.clock,
        )

    def revoke() -> object:
        revoke_invitation(db.factory, db.ctx("olga", "owner"), invitation_id=created.id)
        return None

    if first == "accept":
        outcomes = serialize_at_checkpoint(db.engine, monkeypatch, invitations, accept, revoke)
        assert outcomes["first"] is None
        assert isinstance(outcomes["second"], NotFound)  # only pending invitations are revoked
        assert db.roles()["amy"] == "editor"
    else:
        outcomes = serialize_at_checkpoint(db.engine, monkeypatch, workspaces, revoke, accept)
        assert outcomes["first"] is None
        assert isinstance(outcomes["second"], InvitationInvalid)
        assert "amy" not in db.roles()
    with db.factory() as session:
        invitation = session.get(Invitation, created.id)
        assert invitation is not None
        assert (invitation.used_at is None) is (first == "revoke")
        assert (invitation.revoked_at is None) is (first == "accept")


# The pending cap


def test_concurrent_creates_at_the_cap_leave_fifty_pending(
    db: WorkspaceDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    db.add_member("olga", "owner")
    owner = db.ctx("olga", "owner")
    for _ in range(MAX_PENDING_INVITATIONS - 1):
        create_invitation(db.factory, owner, role="viewer", email=None, expires_in_days=7)

    def create() -> object:
        return create_invitation(db.factory, owner, role="viewer", email=None, expires_in_days=7)

    outcomes = serialize_at_checkpoint(db.engine, monkeypatch, workspaces, create, create)
    assert outcomes["first"] is None
    assert isinstance(outcomes["second"], TooManyInvitations)
    assert len(list_invitations(db.factory, owner)) == MAX_PENDING_INVITATIONS
