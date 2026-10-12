from __future__ import annotations

import logging
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select, update

from assetboard.config import Settings
from assetboard.db.models.identity import (
    AuditEvent,
    AuthThrottle,
    ExternalIdentity,
    Invitation,
    Membership,
    SetupState,
    User,
    UserSession,
)
from assetboard.services.auth import (
    argon2_hash,
    authenticate_with_factory,
    change_password_with_factory,
    create_session_for,
    read_web_session,
    session_id_hash,
)
from assetboard.services.invitations import MAX_FAILED_EMAIL_ATTEMPTS, hash_token
from assetboard.services.oidc import OidcClient, OidcIdentity
from assetboard.services.oidc_accounts import (
    OidcSignIn,
    invitation_token,
    link_identity,
    record_oidc_failure,
    sign_in_with_oidc,
)
from assetboard.services.oidc_flow import PendingLink
from assetboard.services.workspaces import create_invitation
from tests.oidc.fake_idp import BASE_URL, CLIENT_ID, CLIENT_SECRET, ISSUER, FakeIdp, StepClock
from tests.tenant.workspace_db import WorkspaceDb, workspace_db

PASSWORD = "Local Account Password"


@pytest.fixture
def db(tmp_path: Path) -> Iterator[WorkspaceDb]:
    for database in workspace_db(tmp_path):
        with database.factory.begin() as session:
            # This database skipped first-run setup; account creation needs it closed.
            session.execute(update(SetupState).values(completed_at=database.clock.now()))
        yield database


def oidc_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "data_dir": tmp_path,
        "env": "prod",
        "base_url": BASE_URL,
        "oidc_issuer": ISSUER,
        "oidc_client_id": CLIENT_ID,
        "oidc_client_secret": CLIENT_SECRET,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def identity(**overrides: Any) -> OidcIdentity:
    values: dict[str, Any] = {
        "issuer": ISSUER,
        "subject": "sub-ola",
        "email": "ola@example.com",
        "email_verified": True,
        "display_name": "Ola",
        "groups": None,
    }
    values.update(overrides)
    return OidcIdentity(**values)


def sign_in(
    db: WorkspaceDb,
    settings: Settings,
    who: OidcIdentity,
    *,
    next_path: str = "/",
    existing_session_id: str | None = None,
) -> OidcSignIn:
    return sign_in_with_oidc(
        db.factory,
        who,
        next_path=next_path,
        settings=settings,
        clock=db.clock,
        host="203.0.113.7",
        existing_session_id=existing_session_id,
    )


def add_user(
    db: WorkspaceDb,
    email: str,
    *,
    password: str | None = PASSWORD,
    admin: bool = False,
    active: bool = True,
) -> UUID:
    with db.factory.begin() as session:
        user = User(
            email=email,
            display_name=email.split("@")[0],
            password_hash=argon2_hash(password) if password is not None else None,
            is_instance_admin=admin,
            is_active=active,
        )
        session.add(user)
        session.flush()
        return user.id


def link(db: WorkspaceDb, user_id: UUID, subject: str = "sub-ola") -> None:
    with db.factory.begin() as session:
        session.add(
            ExternalIdentity(
                user_id=user_id, provider=ISSUER, subject=subject, email_at_link="x@example.com"
            )
        )


def open_session(db: WorkspaceDb, settings: Settings, user_id: UUID) -> str:
    session_id, _ = create_session_for(
        db.factory, user_id, settings=settings, clock=db.clock, host=None, user_agent=None
    )
    return session_id


def sessions(db: WorkspaceDb, user_id: UUID) -> list[UserSession]:
    with db.factory() as session:
        return list(session.scalars(select(UserSession).where(UserSession.user_id == user_id)))


def identities(db: WorkspaceDb) -> list[tuple[UUID, str]]:
    with db.factory() as session:
        rows = session.execute(select(ExternalIdentity.user_id, ExternalIdentity.subject))
        return [(user_id, subject) for user_id, subject in rows]


def events(db: WorkspaceDb, action: str) -> list[dict[str, Any]]:
    with db.factory() as session:
        rows = session.scalars(
            select(AuditEvent).where(AuditEvent.action == action).order_by(AuditEvent.ts)
        )
        return [event.data for event in rows]


def user(db: WorkspaceDb, user_id: UUID) -> User:
    with db.factory() as session:
        found = session.get(User, user_id)
        assert found is not None
        return found


def pending_for(outcome: OidcSignIn) -> PendingLink:
    assert outcome.status == "confirm_link" and outcome.pending_link is not None
    return outcome.pending_link


def do_link(
    db: WorkspaceDb, settings: Settings, pending: PendingLink, password: str = PASSWORD
) -> Any:
    return link_identity(
        db.factory, pending, password, settings=settings, clock=db.clock, host="203.0.113.7"
    )


# Linked identities


def test_a_linked_identity_signs_in_with_a_new_oidc_session(
    db: WorkspaceDb, tmp_path: Path
) -> None:
    settings = oidc_settings(tmp_path)
    user_id = add_user(db, "ola@example.com", password=None)
    link(db, user_id)
    existing = open_session(db, settings, user_id)
    outcome = sign_in(db, settings, identity(), existing_session_id=existing)
    assert (outcome.status, outcome.user_id) == ("signed_in", user_id)
    assert outcome.session_id and outcome.session_id != existing
    rows = sessions(db, user_id)
    # The session the browser already had is replaced (rotation).
    assert [row.id_hash for row in rows] == [session_id_hash(outcome.session_id)]
    assert rows[0].auth_method == "oidc"
    assert user(db, user_id).last_login_at == db.clock.now()
    assert events(db, "auth.login_success") == [{"method": "oidc"}]


def test_a_linked_identity_matches_by_issuer_and_subject_not_email(
    db: WorkspaceDb, tmp_path: Path
) -> None:
    settings = oidc_settings(tmp_path)
    user_id = add_user(db, "ola@example.com", password=None)
    link(db, user_id)
    # The provider's email changed: the identity still maps to the same user.
    assert sign_in(db, settings, identity(email="new@example.com")).user_id == user_id
    # The same subject at another issuer is a different identity.
    other = sign_in(db, settings, identity(issuer="https://other.test", email="z@example.com"))
    assert other.status == "no_account"


def test_an_inactive_user_is_refused(db: WorkspaceDb, tmp_path: Path) -> None:
    settings = oidc_settings(tmp_path)
    user_id = add_user(db, "ola@example.com", password=None, active=False)
    link(db, user_id)
    assert sign_in(db, settings, identity()).status == "inactive"
    assert sessions(db, user_id) == []
    assert events(db, "auth.oidc_failure") == [{"reason": "inactive"}]


# Linking by email


@pytest.mark.parametrize(
    ("flag", "verified", "status"),
    [
        (True, True, "confirm_link"),
        (True, False, "email_unverified"),
        (False, True, "account_exists"),
        (False, False, "email_unverified"),
    ],
)
def test_link_by_email_needs_the_flag_and_a_verified_email(
    db: WorkspaceDb, tmp_path: Path, flag: bool, verified: bool, status: str
) -> None:
    settings = oidc_settings(tmp_path, oidc_link_by_email=flag, oidc_auto_create=True)
    user_id = add_user(db, "ola@example.com")
    outcome = sign_in(db, settings, identity(email_verified=verified))
    assert outcome.status == status
    assert identities(db) == [] and sessions(db, user_id) == []
    if status == "confirm_link":
        assert outcome.pending_link == PendingLink(
            user_id, ISSUER, "sub-ola", "ola@example.com", None, "/"
        )
    else:
        assert events(db, "auth.oidc_failure") == [{"reason": status}]


def test_an_account_without_a_password_is_never_linked_by_email(
    db: WorkspaceDb, tmp_path: Path
) -> None:
    settings = oidc_settings(tmp_path, oidc_link_by_email=True)
    add_user(db, "ola@example.com", password=None)
    assert sign_in(db, settings, identity()).status == "account_exists"
    assert identities(db) == []


def test_linking_needs_the_password_and_revokes_other_sessions(
    db: WorkspaceDb, tmp_path: Path
) -> None:
    settings = oidc_settings(tmp_path, oidc_link_by_email=True)
    user_id = add_user(db, "ola@example.com")
    other_device = open_session(db, settings, user_id)
    pending = pending_for(sign_in(db, settings, identity()))

    result = do_link(db, settings, pending)
    assert (result.status, result.user_id, result.revoked_sessions) == ("linked", user_id, 1)
    assert identities(db) == [(user_id, "sub-ola")]
    rows = sessions(db, user_id)
    assert [row.id_hash for row in rows] == [session_id_hash(result.session_id)]
    assert session_id_hash(other_device) not in {row.id_hash for row in rows}
    assert rows[0].auth_method == "oidc"
    assert events(db, "auth.oidc_linked") == [{"other_sessions_revoked": 1}]
    # From now on the identity signs in directly.
    assert sign_in(db, settings, identity()).status == "signed_in"


def test_a_wrong_password_is_counted_like_a_failed_login(db: WorkspaceDb, tmp_path: Path) -> None:
    settings = oidc_settings(tmp_path, oidc_link_by_email=True)
    user_id = add_user(db, "ola@example.com")
    pending = pending_for(sign_in(db, settings, identity()))
    # Five free failures, then the sixth starts the backoff (as for login).
    for _ in range(settings.auth_account_free_failures + 1):
        assert do_link(db, settings, pending, "wrong password!").status == "failed"
    assert identities(db) == []
    assert len(events(db, "auth.password_confirm_failure")) == 6
    assert events(db, "auth.password_confirm_failure")[0] == {"purpose": "oidc_link"}
    # The account bucket is the login one: the right password is now throttled too.
    assert do_link(db, settings, pending).status == "failed"
    with db.factory() as session:
        counts = {row.key_type: row.failure_count for row in session.scalars(select(AuthThrottle))}
    assert counts["account"] == 6
    assert identities(db) == [] and sessions(db, user_id) == []


def test_pre_hijacked_account_is_not_linked(db: WorkspaceDb, tmp_path: Path) -> None:
    """An attacker registered the victim's email locally before the victim ever signed in.

    With linking by email on, the victim's verified OIDC identity must not be bound to the
    attacker's account: the victim does not know the attacker's password, so the link never
    happens, and the attacker's session is not upgraded to an OIDC identity.
    """
    settings = oidc_settings(tmp_path, oidc_link_by_email=True, oidc_auto_create=True)
    attacker_account = add_user(db, "ola@example.com", password="Attacker Chosen Password")
    attacker_session = open_session(db, settings, attacker_account)

    outcome = sign_in(db, settings, identity())
    assert outcome.status == "confirm_link"  # never signed in on the email alone
    assert outcome.session_id is None
    assert do_link(db, settings, pending_for(outcome), "victim's own password").status == "failed"
    assert identities(db) == []
    assert [row.id_hash for row in sessions(db, attacker_account)] == [
        session_id_hash(attacker_session)
    ]
    # Auto-create does not take the email over either.
    with db.factory() as session:
        assert session.scalars(select(User.id).where(User.email == "ola@example.com")).all() == [
            attacker_account
        ]


def test_pre_hijacking_through_an_unverified_provider_email_is_refused(
    db: WorkspaceDb, tmp_path: Path
) -> None:
    # The attacker controls an IdP account that claims the victim's email without verifying it.
    settings = oidc_settings(tmp_path, oidc_auto_create=True, oidc_link_by_email=True)
    victim = add_user(db, "ola@example.com")
    attacker = identity(subject="attacker", email_verified=False)
    assert sign_in(db, settings, attacker).status == "email_unverified"
    # And no account is created for an unverified email that is still free.
    free = identity(subject="attacker", email="later-victim@example.com", email_verified=False)
    assert sign_in(db, settings, free).status == "email_unverified"
    assert identities(db) == [] and sessions(db, victim) == []


def test_linking_is_refused_when_things_changed_meanwhile(db: WorkspaceDb, tmp_path: Path) -> None:
    settings = oidc_settings(tmp_path, oidc_link_by_email=True)
    user_id = add_user(db, "ola@example.com")
    pending = pending_for(sign_in(db, settings, identity()))
    assert do_link(db, oidc_settings(tmp_path), pending).status == "disabled"
    assert do_link(db, settings, replace(pending, email="other@example.com")).status == "failed"
    other = add_user(db, "other@example.com", password=None)
    link(db, other, subject="sub-ola")
    assert do_link(db, settings, pending).status == "failed"
    assert sessions(db, user_id) == []


# Creating accounts


def test_auto_create_makes_a_passwordless_user_with_an_own_workspace(
    db: WorkspaceDb, tmp_path: Path
) -> None:
    settings = oidc_settings(tmp_path, oidc_auto_create=True)
    outcome = sign_in(db, settings, identity())
    assert outcome.status == "signed_in" and outcome.created
    assert outcome.user_id is not None
    created = user(db, outcome.user_id)
    assert (created.email, created.display_name, created.password_hash) == (
        "ola@example.com",
        "Ola",
        None,
    )
    assert not created.is_instance_admin
    with db.factory() as session:
        roles = session.scalars(
            select(Membership.role).where(Membership.user_id == outcome.user_id)
        ).all()
    assert roles == ["owner"]
    assert identities(db) == [(outcome.user_id, "sub-ola")]
    assert events(db, "auth.register") == [{"via": "oidc"}]


@pytest.mark.parametrize(
    ("who", "status"),
    [
        (identity(email_verified=False), "email_unverified"),
        (identity(email=None, email_verified=False), "email_unverified"),
    ],
)
def test_auto_create_needs_a_verified_email(
    db: WorkspaceDb, tmp_path: Path, who: OidcIdentity, status: str
) -> None:
    settings = oidc_settings(tmp_path, oidc_auto_create=True)
    assert sign_in(db, settings, who).status == status
    assert identities(db) == []


def test_without_auto_create_an_unknown_identity_has_no_account(
    db: WorkspaceDb, tmp_path: Path
) -> None:
    assert sign_in(db, oidc_settings(tmp_path), identity()).status == "no_account"
    assert events(db, "auth.oidc_failure") == [{"reason": "no_account"}]


def test_account_creation_uses_the_registration_throttle(db: WorkspaceDb, tmp_path: Path) -> None:
    settings = oidc_settings(tmp_path, oidc_auto_create=True, registration_ip_limit=2)
    for number in range(2):
        who = identity(subject=f"s{number}", email=f"u{number}@example.com")
        assert sign_in(db, settings, who).status == "signed_in"
    assert sign_in(db, settings, identity(subject="s3", email="u3@example.com")).status == (
        "throttled"
    )


# Invitations


def invite(db: WorkspaceDb, email: str | None = None, role: str = "editor") -> str:
    if "olga" not in db.users:
        db.add_member("olga", "owner")
    return create_invitation(
        db.factory, db.ctx("olga", "owner"), role=role, email=email, expires_in_days=7
    ).token


def invitation(db: WorkspaceDb, token: str) -> Invitation:
    with db.factory() as session:
        return session.scalars(
            select(Invitation).where(Invitation.token_hash == hash_token(token))
        ).one()


def test_invitation_sign_up_works_without_auto_create(db: WorkspaceDb, tmp_path: Path) -> None:
    settings = oidc_settings(tmp_path)
    token = invite(db)
    outcome = sign_in(db, settings, identity(), next_path=f"/invite/{token}")
    assert outcome.status == "signed_in" and outcome.joined_workspace_id == db.workspace_id
    assert db.roles()["ola"] == "editor"
    assert invitation(db, token).used_by_user_id == outcome.user_id
    with db.factory() as session:
        memberships = session.scalars(
            select(Membership).where(Membership.user_id == outcome.user_id)
        ).all()
    assert len(memberships) == 1  # no own workspace
    assert events(db, "auth.register") == [{"via": "oidc_invitation"}]


@pytest.mark.parametrize("verified", [True, False])
def test_invitation_email_lock_matches_the_provider_email(
    db: WorkspaceDb, tmp_path: Path, verified: bool
) -> None:
    settings = oidc_settings(tmp_path)
    token = invite(db, email="ola@example.com", role="owner")
    outcome = sign_in(db, settings, identity(email_verified=verified), next_path=f"/invite/{token}")
    assert outcome.status == ("signed_in" if verified else "email_unverified")
    # An unverified email is refused before the lock is compared: nothing is counted.
    assert invitation(db, token).failed_email_attempts == 0


def test_invitation_email_mismatch_is_counted_and_revokes_after_five(
    db: WorkspaceDb, tmp_path: Path
) -> None:
    settings = oidc_settings(tmp_path)
    token = invite(db, email="right@example.com")
    for attempt in range(1, MAX_FAILED_EMAIL_ATTEMPTS + 1):
        outcome = sign_in(
            db,
            settings,
            identity(subject=f"s{attempt}", email=f"wrong{attempt}@example.com"),
            next_path=f"/invite/{token}",
        )
        assert outcome.status == "email_mismatch"
        assert outcome.invitation_revoked is (attempt == MAX_FAILED_EMAIL_ATTEMPTS)
    assert invitation(db, token).revoked_at is not None
    right = identity(subject="right", email="right@example.com")
    assert sign_in(db, settings, right, next_path=f"/invite/{token}").status == (
        "invitation_invalid"
    )
    assert identities(db) == []


def test_invitation_for_an_existing_local_email_says_the_account_exists(
    db: WorkspaceDb, tmp_path: Path
) -> None:
    settings = oidc_settings(tmp_path)
    add_user(db, "ola@example.com")
    token = invite(db)
    outcome = sign_in(db, settings, identity(), next_path=f"/invite/{token}")
    assert outcome.status == "account_exists"
    assert invitation(db, token).used_at is None


def test_an_invalid_invitation_creates_nothing(db: WorkspaceDb, tmp_path: Path) -> None:
    settings = oidc_settings(tmp_path)
    outcome = sign_in(db, settings, identity(), next_path="/invite/" + "A" * 43)
    assert outcome.status == "invitation_invalid"
    assert identities(db) == []


@pytest.mark.parametrize(
    ("next_path", "token"),
    [
        ("/invite/" + "a" * 43, "a" * 43),
        ("/invite/" + "a" * 42, None),
        ("/invite/" + "a" * 43 + "/accept", None),
        ("/", None),
        ("/x/invite/" + "a" * 43, None),
    ],
)
def test_invitation_token_from_next(next_path: str, token: str | None) -> None:
    assert invitation_token(next_path) == token


# Domain allow-list


@pytest.mark.parametrize(
    ("who", "allowed"),
    [
        (identity(), True),
        (identity(email="ola@EXAMPLE.com"), True),
        (identity(email="ola@evil.test"), False),
        (identity(email="ola@sub.example.com"), False),
        (identity(email_verified=False), False),
        (identity(email=None, email_verified=False), False),
    ],
)
def test_allowed_domains(db: WorkspaceDb, tmp_path: Path, who: OidcIdentity, allowed: bool) -> None:
    settings = oidc_settings(tmp_path, oidc_auto_create=True, oidc_allowed_domains="example.com")
    normalized = replace(who, email=who.email.lower() if who.email else None)
    outcome = sign_in(db, settings, normalized)
    assert (outcome.status == "signed_in") is allowed
    if not allowed:
        assert outcome.status == "domain_not_allowed"


def test_allowed_domains_are_checked_for_linked_identities_too(
    db: WorkspaceDb, tmp_path: Path
) -> None:
    settings = oidc_settings(tmp_path, oidc_allowed_domains="example.com")
    user_id = add_user(db, "ola@example.com", password=None)
    link(db, user_id)
    assert sign_in(db, settings, identity(email="ola@moved.test")).status == "domain_not_allowed"


# Admin group


@pytest.mark.parametrize(
    ("groups", "start", "end"),
    [
        (("money-admins",), False, True),
        (("family",), True, False),
        ((), True, False),  # present and empty is authoritative
        (None, True, True),  # absent or malformed: unchanged
        (None, False, False),
    ],
)
def test_admin_group_grants_and_revokes(
    db: WorkspaceDb,
    tmp_path: Path,
    groups: tuple[str, ...] | None,
    start: bool,
    end: bool,
) -> None:
    settings = oidc_settings(tmp_path, oidc_admin_group="money-admins")
    add_user(db, "keeper@example.com", admin=True)  # never the last admin here
    user_id = add_user(db, "ola@example.com", password=None, admin=start)
    link(db, user_id)
    assert sign_in(db, settings, identity(groups=groups)).status == "signed_in"
    assert user(db, user_id).is_instance_admin is end
    changes = events(db, "auth.instance_admin_granted") + events(db, "auth.instance_admin_revoked")
    assert changes == ([{"source": "oidc_group"}] if start != end else [])


def test_the_last_active_admin_is_never_demoted(
    db: WorkspaceDb, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = oidc_settings(tmp_path, oidc_admin_group="money-admins")
    add_user(db, "former@example.com", admin=True, active=False)
    user_id = add_user(db, "ola@example.com", password=None, admin=True)
    link(db, user_id)
    with caplog.at_level(logging.WARNING):
        assert sign_in(db, settings, identity(groups=())).status == "signed_in"
    assert user(db, user_id).is_instance_admin
    assert "last active instance administrator" in caplog.text


def test_admin_group_applies_to_new_and_linked_accounts(db: WorkspaceDb, tmp_path: Path) -> None:
    settings = oidc_settings(
        tmp_path, oidc_admin_group="money-admins", oidc_auto_create=True, oidc_link_by_email=True
    )
    created = sign_in(db, settings, identity(groups=("money-admins",)))
    assert created.user_id is not None and user(db, created.user_id).is_instance_admin
    local = add_user(db, "local@example.com")
    pending = pending_for(
        sign_in(db, settings, identity(subject="s2", email="local@example.com", groups=("x",)))
    )
    assert pending.admin is False
    assert do_link(db, settings, pending).status == "linked"
    assert not user(db, local).is_instance_admin


# Passwords on OIDC-only accounts


@pytest.mark.parametrize("password", ["", " ", "x", "Local Account Password", "a" * 1024])
def test_an_oidc_only_user_cannot_sign_in_with_any_password(
    db: WorkspaceDb, tmp_path: Path, password: str
) -> None:
    settings = oidc_settings(tmp_path, oidc_auto_create=True)
    created = sign_in(db, settings, identity())
    verifies: list[str] = []

    def verify(stored: str, given: str) -> bool:
        verifies.append(stored)
        return False

    outcome = authenticate_with_factory(
        db.factory,
        email="ola@example.com",
        password=password,
        settings=settings,
        clock=db.clock,
        host="203.0.113.8",
        verify=verify,
    )
    assert not outcome.succeeded
    # Same path as an unknown user: one (dummy) verify, never a NULL hash.
    assert len(verifies) == 1 and verifies[0].startswith("$argon2")
    assert created.user_id is not None
    assert user(db, created.user_id).password_hash is None


def test_an_oidc_only_user_has_no_password_to_change(db: WorkspaceDb, tmp_path: Path) -> None:
    settings = oidc_settings(tmp_path, oidc_auto_create=True)
    created = sign_in(db, settings, identity())
    assert created.user_id is not None
    outcome = change_password_with_factory(
        db.factory,
        user_id=created.user_id,
        current_password="",
        new_password="Brand New Password",
        settings=settings,
        clock=db.clock,
    )
    assert outcome.status == "no_password"
    assert user(db, created.user_id).password_hash is None
    with db.factory() as session:
        counted = session.scalars(
            select(AuthThrottle).where(AuthThrottle.key_type == "account")
        ).all()
    assert counted == []  # nothing counted against the account


# Audit


def test_failures_are_audited_with_a_reason_code_only(db: WorkspaceDb, tmp_path: Path) -> None:
    record_oidc_failure(
        db.factory, "state_mismatch", clock=db.clock, host="203.0.113.9", user_agent="UA"
    )
    with db.factory() as session:
        event = session.scalars(select(AuditEvent)).one()
    assert (event.action, event.data, event.actor_type) == (
        "auth.oidc_failure",
        {"reason": "state_mismatch"},
        "system",
    )
    assert event.workspace_id is None and event.ip_prefix == "203.0.113.0/24"


# End to end with the fake provider


def test_end_to_end_with_the_fake_provider(db: WorkspaceDb, tmp_path: Path) -> None:
    clock = StepClock(db.clock.now().isoformat())
    idp = FakeIdp(clock)
    settings = oidc_settings(tmp_path, oidc_auto_create=True)
    client = OidcClient(settings, idp, clock)
    request = client.begin()
    params = idp.authorize(request.url)
    who = client.complete(
        params, state=request.state, nonce=request.nonce, code_verifier=request.code_verifier
    )
    outcome = sign_in(db, settings, who)
    assert outcome.status == "signed_in"
    with db.factory() as session:
        assert session.scalars(select(ExternalIdentity.subject)).all() == ["user-123"]


def test_the_web_session_reports_how_it_was_started(db: WorkspaceDb, tmp_path: Path) -> None:
    settings = oidc_settings(tmp_path, oidc_auto_create=True)
    outcome = sign_in(db, settings, identity())
    assert outcome.session_id is not None and outcome.user_id is not None
    password_session = open_session(db, settings, outcome.user_id)
    with db.factory() as session:
        oidc = read_web_session(session, outcome.session_id, settings=settings, clock=db.clock)
        local = read_web_session(session, password_session, settings=settings, clock=db.clock)
    assert oidc is not None and oidc.auth_method == "oidc"
    assert local is not None and local.auth_method == "password"


@pytest.mark.parametrize(
    "flags",
    [
        {},
        {"oidc_auto_create": True},
        {"oidc_link_by_email": True},
        {"oidc_auto_create": True, "oidc_link_by_email": True},
    ],
)
@pytest.mark.parametrize("with_invitation", [False, True])
def test_an_unverified_email_reveals_nothing_about_local_accounts(
    db: WorkspaceDb, tmp_path: Path, flags: dict[str, bool], with_invitation: bool
) -> None:
    """Same answer whether or not the claimed address is registered here."""
    settings = oidc_settings(tmp_path, **flags)
    add_user(db, "taken@example.com")
    next_path = f"/invite/{invite(db)}" if with_invitation else "/"
    answers = {
        sign_in(
            db,
            settings,
            identity(subject=email, email=email, email_verified=False),
            next_path=next_path,
        ).status
        for email in ("taken@example.com", "free@example.com")
    }
    assert answers == {"email_unverified"}
    assert identities(db) == []


def test_a_linked_identity_does_not_need_a_verified_email(db: WorkspaceDb, tmp_path: Path) -> None:
    settings = oidc_settings(tmp_path)
    user_id = add_user(db, "ola@example.com", password=None)
    link(db, user_id)
    assert sign_in(db, settings, identity(email_verified=False)).status == "signed_in"


@pytest.mark.parametrize(
    "name",
    ["Ola\nadmin@example.com 200 OK", "\u202eOla", "\u200b\u200b", "", "   ", "\x00\x1b[31m"],
)
def test_awkward_provider_names_never_break_account_creation(
    db: WorkspaceDb, tmp_path: Path, name: str
) -> None:
    clock = StepClock(db.clock.now().isoformat())
    idp = FakeIdp(clock)
    idp.claims["name"] = name
    settings = oidc_settings(tmp_path, oidc_auto_create=True)
    client = OidcClient(settings, idp, clock)
    request = client.begin()
    who = client.complete(
        idp.authorize(request.url),
        state=request.state,
        nonce=request.nonce,
        code_verifier=request.code_verifier,
    )
    outcome = sign_in(db, settings, who)
    assert outcome.status == "signed_in" and outcome.user_id is not None
    stored = user(db, outcome.user_id).display_name
    assert stored and stored == stored.strip()
    assert all(character.isprintable() for character in stored)
