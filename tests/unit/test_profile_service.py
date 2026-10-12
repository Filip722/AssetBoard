"""Profile services: the confirmation entry point, usable sign-in methods, unlink (F1.5)."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select, update

from assetboard.config import Settings
from assetboard.db.engine import run_immediate
from assetboard.db.models.identity import (
    AuditEvent,
    ExternalIdentity,
    SetupState,
    User,
    UserSession,
)
from assetboard.services import profile
from assetboard.services.auth import argon2_hash, create_user_session, session_id_hash
from assetboard.services.external_identity import PROXY_PROVIDER
from assetboard.services.profile import (
    confirm_sensitive,
    confirmation_method,
    unlink_identity,
    update_profile,
)
from tests.tenant.serialize import serialize_at_checkpoint
from tests.tenant.workspace_db import WorkspaceDb, workspace_db

ISSUER = "https://idp.example.test/realms/home"
OLD_ISSUER = "https://old-idp.example.test"
PASSWORD = "Local Account Password"


@pytest.fixture
def db(tmp_path: Path) -> Iterator[WorkspaceDb]:
    for database in workspace_db(tmp_path):
        with database.factory.begin() as session:
            session.execute(update(SetupState).values(completed_at=database.clock.now()))
        yield database


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "data_dir": tmp_path,
        "env": "prod",
        "base_url": "https://money.example.test",
        "oidc_issuer": ISSUER,
        "oidc_client_id": "assetboard",
        "proxy_auth_enabled": True,
        "proxy_auth_user_header": "Remote-User",
        "proxy_auth_email_header": "Remote-Email",
        "trusted_proxies": "172.30.0.2",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def add_user(db: WorkspaceDb, *, password: bool = True, admin: bool = False) -> UUID:
    with db.factory.begin() as session:
        user = User(
            email="ola@example.com",
            display_name="Ola",
            password_hash=argon2_hash(PASSWORD) if password else None,
            is_instance_admin=admin,
        )
        session.add(user)
        session.flush()
        return user.id


def add_identity(db: WorkspaceDb, user_id: UUID, provider: str, subject: str) -> UUID:
    with db.factory.begin() as session:
        identity = ExternalIdentity(user_id=user_id, provider=provider, subject=subject)
        session.add(identity)
        session.flush()
        return identity.id


def add_session(
    db: WorkspaceDb, settings: Settings, user_id: UUID, method: str, minutes_ago: int = 0
) -> str:
    with db.factory.begin() as session:
        user = session.get(User, user_id)
        assert user is not None
        session_id = create_user_session(
            session,
            user,
            settings=settings,
            clock=db.clock,
            host=None,
            user_agent=None,
            auth_method=method,  # type: ignore[arg-type]
        )
        session.execute(
            update(UserSession)
            .where(UserSession.id_hash == session_id_hash(session_id))
            .values(created_at=db.clock.now() - timedelta(minutes=minutes_ago))
        )
    return session_id_hash(session_id)


def audits(db: WorkspaceDb, action: str) -> list[dict[str, Any]]:
    with db.factory() as session:
        rows = session.scalars(select(AuditEvent).where(AuditEvent.action == action)).all()
        return [dict(row.data) for row in rows]


# Which confirmation applies


@pytest.mark.parametrize(
    ("password", "method", "minutes_ago", "expected"),
    [
        (True, "oidc", 0, "password"),  # a password always wins
        (True, "proxy", 0, "password"),
        (False, "oidc", 0, "fresh_session"),
        (False, "proxy", 10, "fresh_session"),  # exactly 10 minutes: still fresh
        (False, "proxy", 11, "none"),
        (False, "password", 0, "none"),  # only an external sign-in counts
    ],
)
def test_confirmation_method(
    db: WorkspaceDb,
    tmp_path: Path,
    password: bool,
    method: str,
    minutes_ago: int,
    expected: str,
) -> None:
    settings = make_settings(tmp_path)
    user_id = add_user(db, password=password)
    session_hash = add_session(db, settings, user_id, method, minutes_ago)
    with db.factory() as session:
        assert (
            confirmation_method(
                session, user_id=user_id, session_hash=session_hash, now=db.clock.now()
            )
            == expected
        )


def test_another_users_session_never_confirms(db: WorkspaceDb, tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    user_id = add_user(db, password=False)
    with db.factory.begin() as session:
        other = User(email="x@example.com", display_name="X")
        session.add(other)
        session.flush()
        other_id = other.id
    session_hash = add_session(db, settings, other_id, "oidc")
    with db.factory() as session:
        method = confirmation_method(
            session, user_id=user_id, session_hash=session_hash, now=db.clock.now()
        )
    assert method == "none"


def confirm(
    db: WorkspaceDb, settings: Settings, user_id: UUID, session_hash: str, password: str
) -> Any:
    return confirm_sensitive(
        db.factory,
        user_id=user_id,
        session_hash=session_hash,
        password=password,
        purpose="test",
        settings=settings,
        clock=db.clock,
        on_success=lambda _session, _user, method: method,
    )


def test_the_password_branch_is_the_existing_primitive(db: WorkspaceDb, tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    user_id = add_user(db)
    session_hash = add_session(db, settings, user_id, "oidc")
    assert confirm(db, settings, user_id, session_hash, "wrong").status == "failed"
    assert audits(db, "auth.password_confirm_failure") == [{"purpose": "test"}]
    outcome = confirm(db, settings, user_id, session_hash, PASSWORD)
    assert (outcome.status, outcome.value) == ("confirmed", "password")


def test_a_stale_session_writes_nothing(db: WorkspaceDb, tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    user_id = add_user(db, password=False)
    session_hash = add_session(db, settings, user_id, "proxy", minutes_ago=30)
    calls: list[str] = []
    outcome = confirm_sensitive(
        db.factory,
        user_id=user_id,
        session_hash=session_hash,
        password="anything",
        purpose="test",
        settings=settings,
        clock=db.clock,
        on_success=lambda _s, _u, method: calls.append(method),
    )
    assert outcome.status == "stale" and calls == []
    with db.factory() as session:
        assert session.scalars(select(AuditEvent)).all() == []


def test_a_fresh_session_confirms_and_reports_how(db: WorkspaceDb, tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    user_id = add_user(db, password=False)
    session_hash = add_session(db, settings, user_id, "oidc", minutes_ago=3)
    outcome = confirm(db, settings, user_id, session_hash, "")
    assert (outcome.status, outcome.value) == ("confirmed", "fresh_session")


# Profile settings


@pytest.mark.parametrize(
    ("locale", "stored", "error"),
    [("pl", "pl", None), ("browser", None, None), ("", None, None), ("de", None, "locale")],
)
def test_update_profile_locale(
    db: WorkspaceDb, locale: str, stored: str | None, error: str | None
) -> None:
    user_id = add_user(db)
    result = update_profile(
        db.factory, user_id=user_id, display_name="Ola", locale=locale, theme="system"
    )
    assert result == error
    with db.factory() as session:
        user = session.get(User, user_id)
        assert user is not None and user.locale == (stored if error is None else None)


# Unlink: never the last usable sign-in method


@pytest.mark.parametrize(
    ("overrides", "password", "admin", "others", "allowed"),
    [
        ({}, True, False, [], True),  # the password remains
        ({}, False, False, [], False),  # nothing would remain
        ({}, False, False, [(PROXY_PROVIDER, "ola")], True),  # the proxy remains
        ({"proxy_auth_enabled": False}, False, False, [(PROXY_PROVIDER, "ola")], False),
        ({}, False, False, [(OLD_ISSUER, "s")], False),  # an old issuer does not count
        ({"oidc_only": True}, True, False, [], False),  # password sign-in is off
        ({"oidc_only": True}, True, True, [], False),  # admin, but no break-glass
        ({"oidc_only": True, "allow_local_admin_login": True}, True, True, [], True),
        ({"oidc_only": True, "allow_local_admin_login": True}, True, False, [], False),
    ],
)
def test_unlink_keeps_a_usable_sign_in_method(
    db: WorkspaceDb,
    tmp_path: Path,
    overrides: dict[str, Any],
    password: bool,
    admin: bool,
    others: list[tuple[str, str]],
    allowed: bool,
) -> None:
    settings = make_settings(tmp_path, **overrides)
    user_id = add_user(db, password=password, admin=admin)
    target = add_identity(db, user_id, ISSUER, "s1")
    for provider, subject in others:
        add_identity(db, user_id, provider, subject)

    def remove(session: Any) -> str:
        user = session.get(User, user_id)
        return unlink_identity(
            session,
            user,
            target,
            current_session_hash="",
            confirmed="password",
            settings=settings,
            clock=db.clock,
            host=None,
            user_agent=None,
        )

    status = run_immediate(db.factory, remove)
    assert status == ("unlinked" if allowed else "last_method")
    with db.factory() as session:
        remaining = session.scalars(select(ExternalIdentity.id)).all()
    assert (target in remaining) == (not allowed)
    if not allowed:
        assert audits(db, "auth.oidc_unlinked") == []  # a refusal writes nothing


def test_two_unlinks_at_once_keep_one_method(
    db: WorkspaceDb, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(tmp_path)
    user_id = add_user(db, password=False)
    first_id = add_identity(db, user_id, ISSUER, "s1")
    second_id = add_identity(db, user_id, PROXY_PROVIDER, "ola")
    results: dict[str, str] = {}

    def remove(name: str, identity_id: UUID) -> None:
        def run(session: Any) -> str:
            user = session.get(User, user_id)
            return unlink_identity(
                session,
                user,
                identity_id,
                current_session_hash="",
                confirmed="fresh_session",
                settings=settings,
                clock=db.clock,
                host=None,
                user_agent=None,
            )

        results[name] = run_immediate(db.factory, run)

    outcomes = serialize_at_checkpoint(
        db.engine,
        monkeypatch,
        profile,
        lambda: remove("first", first_id),
        lambda: remove("second", second_id),
    )
    assert outcomes == {"first": None, "second": None}
    assert sorted(results.values()) == ["last_method", "unlinked"]
    with db.factory() as session:
        assert len(session.scalars(select(ExternalIdentity.id)).all()) == 1


def test_one_identity_per_provider(db: WorkspaceDb, tmp_path: Path) -> None:
    user_id = add_user(db)
    add_identity(db, user_id, ISSUER, "first")
    add_identity(db, user_id, OLD_ISSUER, "old")  # an earlier issuer never counts

    def link(provider: str, subject: str) -> str:
        def run(session: Any) -> str:
            user = session.get(User, user_id)
            return profile.link_identity_to_user(
                session,
                user,
                provider=provider,
                subject=subject,
                email=None,
                confirmed="password",
                clock=db.clock,
                host=None,
                user_agent=None,
            )

        return run_immediate(db.factory, run)

    assert link(ISSUER, "first") == "already_linked"  # checked before the per-provider rule
    assert link(ISSUER, "second") == "one_per_provider"
    assert link(PROXY_PROVIDER, "ola") == "linked"
    assert link(PROXY_PROVIDER, "other") == "one_per_provider"
    assert audits(db, "auth.oidc_linked") == []
    assert audits(db, "auth.proxy_linked") == [{"via": "profile", "confirmed": "password"}]
