from __future__ import annotations

import inspect
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text

from assetboard.clock import FixedClock
from assetboard.db.models.identity import AuditEvent, Invitation, Membership
from assetboard.services import invitations, workspaces
from assetboard.services.context import Role
from assetboard.services.invitations import (
    MAX_FAILED_EMAIL_ATTEMPTS,
    AlreadyMember,
    EmailMismatch,
    InvitationInvalid,
    accept_invitation,
    check_for_sign_up,
    hash_token,
    preview_invitation,
)
from assetboard.services.workspaces import (
    DEFAULT_INVITATION_EXPIRY_DAYS,
    INVITATION_EXPIRY_DAYS,
    MAX_PENDING_INVITATIONS,
    InvalidInvitation,
    TooManyInvitations,
    create_invitation,
    list_invitations,
    revoke_invitation,
)
from tests.tenant.static_scan import BOOTSTRAP_ALLOWLIST
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


@pytest.fixture
def db(tmp_path: Path) -> Iterator[WorkspaceDb]:
    yield from workspace_db(tmp_path)


@pytest.fixture
def team(db: WorkspaceDb) -> WorkspaceDb:
    db.add_member("olga", "owner")
    db.add_member("ivy", "viewer")
    return db


def _invite(db: WorkspaceDb, role: str = "editor", email: str | None = None, days: int = 7) -> str:
    return create_invitation(
        db.factory, db.ctx("olga", "owner"), role=role, email=email, expires_in_days=days
    ).token


def _accept(db: WorkspaceDb, token: str, key: str, email: str | None = None) -> Any:
    if key not in db.users:
        db.add_member(key, "viewer", workspace_id=db.add_workspace(f"{key}-own"))
    return accept_invitation(
        db.factory,
        token,
        user_id=db.users[key],
        user_email=email or f"{key}@example.com",
        clock=db.clock,
    )


def _events(db: WorkspaceDb) -> list[tuple[str, dict[str, Any]]]:
    with db.factory() as session:
        rows = session.scalars(select(AuditEvent).order_by(AuditEvent.ts, AuditEvent.action))
        return [(event.action, event.data) for event in rows]


def _invitation(db: WorkspaceDb, token: str) -> Invitation:
    with db.factory() as session:
        return session.scalars(
            select(Invitation).where(Invitation.token_hash == hash_token(token))
        ).one()


# Creating


def test_constants_match_the_agreed_policy() -> None:
    assert INVITATION_EXPIRY_DAYS == (1, 7, 30)
    assert DEFAULT_INVITATION_EXPIRY_DAYS == 7
    assert MAX_PENDING_INVITATIONS == 50
    assert MAX_FAILED_EMAIL_ATTEMPTS == 5


def test_create_invitation_stores_only_the_token_hash(team: WorkspaceDb) -> None:
    created = create_invitation(
        team.factory, team.ctx("olga", "owner"), role="editor", email=None, expires_in_days=7
    )
    assert invitations.TOKEN_PATTERN.fullmatch(created.token)
    assert created.expires_at == team.clock.now() + timedelta(days=7)
    with team.factory() as session:
        dump = "\n".join(str(row) for row in session.execute(text("SELECT * FROM invitations")))
        events = "\n".join(str(row) for row in session.execute(text("SELECT * FROM audit_events")))
    assert created.token not in dump and created.token not in events
    assert hash_token(created.token) in dump and hash_token(created.token) not in events
    assert _events(team) == [
        ("invitation.created", {"role": "editor", "email_locked": False, "expires_in_days": 7})
    ]
    assert [i.id for i in list_invitations(team.factory, team.ctx("olga", "owner"))] == [created.id]


@pytest.mark.parametrize(
    ("kwargs", "field", "code"),
    [
        ({"role": "owner", "email": None}, "email", "owner_requires_email"),
        ({"role": "owner", "email": "  "}, "email", "owner_requires_email"),
        ({"role": "editor", "email": "not-an-email"}, "email", "invalid_email"),
        (
            {"role": "editor", "email": None, "expires_in_days": 2},
            "expires_in_days",
            "invalid_expiry",
        ),
    ],
)
def test_create_invitation_validates_input(
    team: WorkspaceDb, kwargs: dict[str, Any], field: str, code: str
) -> None:
    arguments: dict[str, Any] = {"expires_in_days": 7, **kwargs}
    with pytest.raises(InvalidInvitation) as invalid:
        create_invitation(team.factory, team.ctx("olga", "owner"), **arguments)
    assert (invalid.value.field, invalid.value.code) == (field, code)
    assert _events(team) == []


def test_owner_invitation_with_email_lock_is_allowed_and_normalized(team: WorkspaceDb) -> None:
    created = create_invitation(
        team.factory,
        team.ctx("olga", "owner"),
        role=Role.OWNER,
        email="  New.Owner@Example.COM ",
        expires_in_days=1,
    )
    assert created.email == "new.owner@example.com"


def test_pending_invitations_are_capped(team: WorkspaceDb) -> None:
    for _ in range(MAX_PENDING_INVITATIONS):
        _invite(team)
    with pytest.raises(TooManyInvitations):
        _invite(team)
    # Revoking one frees a slot.
    first = list_invitations(team.factory, team.ctx("olga", "owner"))[0]
    revoke_invitation(team.factory, team.ctx("olga", "owner"), invitation_id=first.id)
    _invite(team)


def test_revoke_audits_and_invalidates(team: WorkspaceDb) -> None:
    token = _invite(team)
    invitation = _invitation(team, token)
    revoke_invitation(team.factory, team.ctx("olga", "owner"), invitation_id=invitation.id)
    assert ("invitation.revoked", {"reason": "owner"}) in _events(team)
    with pytest.raises(InvitationInvalid):
        _accept(team, token, "newbie")


# Accepting


def test_accept_creates_the_membership_and_uses_the_invitation(team: WorkspaceDb) -> None:
    token = _invite(team, role="editor")
    preview = preview_invitation(team.factory, token, clock=team.clock)
    assert (preview.workspace_name, preview.role, preview.email_locked) == ("Home", "editor", False)
    joined = _accept(team, token, "newbie")
    assert joined == team.workspace_id
    assert team.roles()["newbie"] == "editor"
    invitation = _invitation(team, token)
    assert invitation.used_at == team.clock.now()
    assert invitation.used_by_user_id == team.users["newbie"]
    assert (
        "invitation.accepted",
        {"user_id": str(team.users["newbie"]), "role": "editor"},
    ) in _events(team)
    with pytest.raises(InvitationInvalid):  # single use
        _accept(team, token, "other")


def test_accept_builds_its_scope_from_the_invitation_row_only() -> None:
    # No parameter can name a workspace: it comes from the row found by the token hash.
    parameters = set(inspect.signature(accept_invitation).parameters)
    assert parameters == {"factory", "token", "user_id", "user_email", "clock"}
    # And the membership is written through repositories, with no new allowlist entry.
    assert set(BOOTSTRAP_ALLOWLIST) == {
        ("assetboard.services.auth", "_create_user_records"),
        ("assetboard.services.auth", "_audit"),
    }


def test_accept_joins_the_invitations_workspace_not_the_users_current_one(
    team: WorkspaceDb,
) -> None:
    other = team.add_workspace("Other")
    team.add_member("otto", "owner", workspace_id=other)
    token = create_invitation(
        team.factory,
        team.ctx("otto", "owner", workspace_id=other),
        role="viewer",
        email=None,
        expires_in_days=7,
    ).token
    assert _accept(team, token, "ivy") == other
    assert team.roles(other)["ivy"] == "viewer"
    assert team.roles()["ivy"] == "viewer"  # unchanged in Home


def test_already_member_is_told_and_the_invitation_stays_unused(team: WorkspaceDb) -> None:
    token = _invite(team, role="owner", email="ivy@example.com")
    with pytest.raises(AlreadyMember):
        _accept(team, token, "ivy")
    assert team.roles()["ivy"] == "viewer"
    assert _invitation(team, token).used_at is None


@pytest.mark.parametrize("state", ["expired", "revoked", "used", "unknown", "malformed"])
def test_invalid_invitations_look_the_same(team: WorkspaceDb, state: str) -> None:
    token = _invite(team, days=1)
    if state == "expired":
        team.clock = FixedClock((team.clock.now() + timedelta(days=1)).isoformat())
    elif state == "revoked":
        invitation = _invitation(team, token)
        revoke_invitation(team.factory, team.ctx("olga", "owner"), invitation_id=invitation.id)
    elif state == "used":
        _accept(team, token, "first")
    elif state == "unknown":
        token = "A" * 43
    else:
        token = token[:-1] + "!"
    for call in (
        lambda: preview_invitation(team.factory, token, clock=team.clock),
        lambda: _accept(team, token, "late"),
        lambda: check_for_sign_up(team.factory, token, email="x@example.com", clock=team.clock),
    ):
        with pytest.raises(InvitationInvalid):
            call()


# Email lock


def test_email_mismatch_is_counted_and_revokes_after_five(team: WorkspaceDb) -> None:
    token = _invite(team, email="right@example.com")
    for attempt in range(1, MAX_FAILED_EMAIL_ATTEMPTS + 1):
        with pytest.raises(EmailMismatch) as mismatch:
            _accept(team, token, "wrong")
        assert mismatch.value.revoked is (attempt == MAX_FAILED_EMAIL_ATTEMPTS)
        assert _invitation(team, token).failed_email_attempts == attempt
    invitation = _invitation(team, token)
    assert invitation.revoked_at is not None and invitation.used_at is None
    actions = [action for action, _data in _events(team)]
    assert actions.count("invitation.email_mismatch") == MAX_FAILED_EMAIL_ATTEMPTS
    assert ("invitation.revoked", {"reason": "failed_email_attempts"}) in _events(team)
    with pytest.raises(InvitationInvalid):
        _accept(team, token, "right")
    assert "wrong" not in team.roles()


def test_sign_up_check_counts_mismatches_without_a_user(team: WorkspaceDb) -> None:
    token = _invite(team, email="right@example.com")
    check_for_sign_up(team.factory, token, email="right@example.com", clock=team.clock)
    with pytest.raises(EmailMismatch):
        check_for_sign_up(team.factory, token, email="wrong@example.com", clock=team.clock)
    assert _invitation(team, token).failed_email_attempts == 1
    with team.factory() as session:
        event = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "invitation.email_mismatch")
        ).one()
        assert (event.actor_type, event.actor_user_id) == ("system", None)


def test_matching_email_is_accepted(team: WorkspaceDb) -> None:
    token = _invite(team, email="newbie@example.com")
    _accept(team, token, "newbie")
    assert team.roles()["newbie"] == "editor"


def test_no_membership_is_created_without_a_valid_invitation(team: WorkspaceDb) -> None:
    with team.factory() as session:
        before = session.scalars(select(Membership.id)).all()
    with pytest.raises(InvitationInvalid):
        _accept(team, "B" * 43, "nobody")
    with team.factory() as session:
        after = session.scalars(select(Membership.id)).all()
    assert len(after) == len(before) + 1  # only nobody's own workspace from the helper


def test_owner_only_functions_live_in_services_workspaces() -> None:
    # The owner matrix test in test_workspaces_service.py introspects that module.
    for name in ("create_invitation", "list_invitations", "revoke_invitation"):
        assert getattr(workspaces, name).__module__ == workspaces.__name__
