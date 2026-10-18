"""Races around invitations, made deterministic with the services' checkpoint seams."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select, update

from assetboard.config import Settings
from assetboard.db.models.identity import (
    AuditEvent,
    ExternalIdentity,
    Invitation,
    SetupState,
    User,
)
from assetboard.db.repositories import InvitationRepository, NotFound
from assetboard.services import invitations, workspaces
from assetboard.services.auth import RegistrationOutcome, register_with_invitation
from assetboard.services.external_identity import SignInIdentity
from assetboard.services.invitations import (
    MAX_FAILED_EMAIL_ATTEMPTS,
    EmailMismatch,
    InvitationInvalid,
    accept_invitation,
)
from assetboard.services.oidc_accounts import SignInOutcome, sign_in_external
from assetboard.services.workspaces import (
    MAX_PENDING_INVITATIONS,
    TooManyInvitations,
    create_invitation,
    list_invitations,
    revoke_invitation,
)
from tests.tenant.serialize import serialize_at_checkpoint
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


def _unlocked_token(db: WorkspaceDb, role: str) -> str:
    """An invitation without an email lock, as created before every invitation needed one.

    Such links stay valid until they expire, so two different people can still race on one;
    a locked link would refuse the second person before the race.
    """
    token, token_hash = invitations.new_token()
    with db.factory() as session, session.begin():
        InvitationRepository(session, db.ctx("olga", "owner")).create(
            token_hash=token_hash,
            role=role,
            email=None,
            expires_at=db.clock.now() + timedelta(days=7),
            created_by_user_id=db.users["olga"],
        )
    return token


@pytest.fixture
def db(tmp_path: Path) -> Iterator[WorkspaceDb]:
    yield from workspace_db(tmp_path)


def test_concurrent_accepts_create_one_membership(
    db: WorkspaceDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    db.add_member("olga", "owner")
    for key in ("amy", "ben"):
        db.add_member(key, "owner", workspace_id=db.add_workspace(f"{key}-own"))
    token = _unlocked_token(db, "editor")

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


# Races on an email-locked link (every new invitation is locked)


def _locked_token(db: WorkspaceDb, email: str) -> str:
    return create_invitation(
        db.factory, db.ctx("olga", "owner"), role="editor", email=email, expires_in_days=7
    ).token


def _accept_as(db: WorkspaceDb, token: str, key: str) -> object:
    return accept_invitation(
        db.factory,
        token,
        user_id=db.users[key],
        user_email=f"{key}@example.com",
        clock=db.clock,
    )


def _invitation_row(db: WorkspaceDb) -> Invitation:
    with db.factory() as session:
        return session.scalars(select(Invitation)).one()


def _revocations(db: WorkspaceDb) -> int:
    with db.factory() as session:
        return len(
            session.scalars(
                select(AuditEvent.id).where(AuditEvent.action == "invitation.revoked")
            ).all()
        )


@pytest.mark.parametrize("first", ["right", "wrong"])
def test_a_matching_and_a_wrong_email_racing_on_a_locked_link(
    db: WorkspaceDb, monkeypatch: pytest.MonkeyPatch, first: str
) -> None:
    db.add_member("olga", "owner")
    for key in ("amy", "ben"):
        db.add_member(key, "owner", workspace_id=db.add_workspace(f"{key}-own"))
    token = _locked_token(db, "amy@example.com")
    right, wrong = (lambda: _accept_as(db, token, "amy")), (lambda: _accept_as(db, token, "ben"))
    calls = (right, wrong) if first == "right" else (wrong, right)
    outcomes = serialize_at_checkpoint(db.engine, monkeypatch, invitations, *calls)
    right_outcome = outcomes["first" if first == "right" else "second"]
    wrong_outcome = outcomes["second" if first == "right" else "first"]
    assert right_outcome is None
    invitation = _invitation_row(db)
    assert invitation.used_by_user_id == db.users["amy"]
    assert db.roles() == {"olga": "owner", "amy": "editor"}  # nothing for ben
    if first == "right":
        # The link was used before ben's attempt read it: invalid, and not counted.
        assert isinstance(wrong_outcome, InvitationInvalid)
        assert invitation.failed_email_attempts == 0
    else:
        assert isinstance(wrong_outcome, EmailMismatch) and not wrong_outcome.revoked
        assert invitation.failed_email_attempts == 1
    assert _revocations(db) == 0


def test_two_wrong_emails_racing_at_the_limit_revoke_once(
    db: WorkspaceDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    db.add_member("olga", "owner")
    for key in ("ben", "cid", "dan"):
        db.add_member(key, "owner", workspace_id=db.add_workspace(f"{key}-own"))
    token = _locked_token(db, "amy@example.com")
    for _ in range(MAX_FAILED_EMAIL_ATTEMPTS - 1):
        with pytest.raises(EmailMismatch):
            _accept_as(db, token, "dan")
    assert _invitation_row(db).failed_email_attempts == MAX_FAILED_EMAIL_ATTEMPTS - 1
    outcomes = serialize_at_checkpoint(
        db.engine,
        monkeypatch,
        invitations,
        lambda: _accept_as(db, token, "ben"),
        lambda: _accept_as(db, token, "cid"),
    )
    first, second = outcomes["first"], outcomes["second"]
    assert isinstance(first, EmailMismatch) and first.revoked
    assert isinstance(second, InvitationInvalid)  # already revoked: not counted again
    invitation = _invitation_row(db)
    assert invitation.failed_email_attempts == MAX_FAILED_EMAIL_ATTEMPTS
    assert invitation.revoked_at is not None and invitation.used_at is None
    assert _revocations(db) == 1


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
    token = _unlocked_token(db, "viewer")
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
    token = _unlocked_token(db, "viewer")
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
    token = _unlocked_token(db, "viewer")
    results: dict[str, SignInOutcome] = {}

    def sign_up(name: str) -> None:
        who = SignInIdentity(
            issuer="https://idp.example.test",
            subject=name,
            email=f"{name}@example.com",
            email_verified=True,
            display_name=name,
            groups=None,
        )
        results[name] = sign_in_external(
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
        db.factory,
        db.ctx("olga", "owner"),
        role="editor",
        email="amy@example.com",
        expires_in_days=7,
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
        create_invitation(
            db.factory, owner, role="viewer", email="x@example.com", expires_in_days=7
        )

    def create() -> object:
        return create_invitation(
            db.factory, owner, role="viewer", email="x@example.com", expires_in_days=7
        )

    outcomes = serialize_at_checkpoint(db.engine, monkeypatch, workspaces, create, create)
    assert outcomes["first"] is None
    assert isinstance(outcomes["second"], TooManyInvitations)
    assert len(list_invitations(db.factory, owner)) == MAX_PENDING_INVITATIONS


def test_concurrent_sign_ups_with_the_locked_email_create_one_user(
    db: WorkspaceDb, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path, registration_ip_limit=50)
    _close_setup(db)
    db.add_member("olga", "owner")
    token = _locked_token(db, "newbie@example.com")
    results: dict[str, RegistrationOutcome] = {}

    def sign_up(name: str) -> None:
        results[name] = _sign_up(db, settings, token, "newbie@example.com")

    outcomes = serialize_at_checkpoint(
        db.engine, monkeypatch, invitations, lambda: sign_up("first"), lambda: sign_up("second")
    )
    assert outcomes == {"first": None, "second": None}
    assert results["first"].status == "created"
    # The second waits for the first's write to commit; it then finds the link used.
    assert results["second"].status == "invitation_invalid"
    assert _users(db) == {"olga@example.com", "newbie@example.com"}
    assert db.roles() == {"olga": "owner", "newbie": "editor"}
    assert _invitation_row(db).failed_email_attempts == 0
