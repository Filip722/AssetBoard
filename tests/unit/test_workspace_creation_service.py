"""Self-service workspace creation and the instance setting behind it (pre-release A)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import delete, select, update

from assetboard.config import Settings
from assetboard.db.models.identity import (
    AuditEvent,
    InstanceSettings,
    Membership,
    User,
    UserSession,
    Workspace,
)
from assetboard.db.models.ledger import Category
from assetboard.domain.categories import SEED
from assetboard.i18n import gettext
from assetboard.services import workspaces
from assetboard.services.admin_users import NotInstanceAdmin
from assetboard.services.auth import create_session_for, session_id_hash
from assetboard.services.instance_settings import (
    InstanceSettingsView,
    get_settings,
    update_settings,
    workspace_creation_allowed,
)
from assetboard.services.workspaces import (
    InvalidWorkspaceSettings,
    WorkspaceCreationDisabled,
    create_own_workspace,
)
from tests.tenant.serialize import serialize_at_checkpoint
from tests.tenant.workspace_db import WorkspaceDb, workspace_db


@pytest.fixture
def db(tmp_path: Path) -> Iterator[WorkspaceDb]:
    yield from workspace_db(tmp_path)


def _user(db: WorkspaceDb, key: str, *, admin: bool = False, active: bool = True) -> UUID:
    with db.factory.begin() as session:
        user = User(
            email=f"{key}@example.com",
            display_name=key.title(),
            is_instance_admin=admin,
            is_active=active,
        )
        session.add(user)
        session.flush()
        db.users[key] = user.id
        return user.id


def _session_hash(db: WorkspaceDb, user_id: UUID, tmp_path: Path) -> str:
    session_id, _expires = create_session_for(
        db.factory,
        user_id,
        settings=Settings(_env_file=None, data_dir=tmp_path),
        clock=db.clock,
        host=None,
        user_agent=None,
    )
    return session_id_hash(session_id)


def _create(db: WorkspaceDb, user_id: UUID, session_hash: str, **overrides: Any) -> UUID:
    values: dict[str, Any] = {
        "name": "Side project",
        "base_currency": "PLN",
        "country_code": "PL",
        "timezone": "Europe/Warsaw",
        "locale": "pl",
        "seed_locale": "en",
    } | overrides
    return create_own_workspace(
        db.factory, user_id=user_id, session_hash=session_hash, clock=db.clock, **values
    )


def _set_allowed(db: WorkspaceDb, allowed: bool) -> None:
    with db.factory.begin() as session:
        session.execute(update(InstanceSettings).values(allow_workspace_creation=allowed))


def _workspace_count(db: WorkspaceDb) -> int:
    with db.factory() as session:
        return len(session.scalars(select(Workspace.id)).all())


def _events(db: WorkspaceDb, workspace_id: UUID | None) -> list[tuple[str, UUID | None, Any]]:
    scope = (
        AuditEvent.workspace_id.is_(None)
        if workspace_id is None
        else AuditEvent.workspace_id == workspace_id
    )
    with db.factory() as session:
        rows = session.scalars(select(AuditEvent).where(scope).order_by(AuditEvent.action))
        return [(e.action, e.actor_user_id, e.data) for e in rows]


# Creating


def test_any_signed_in_user_creates_a_workspace_they_own(db: WorkspaceDb, tmp_path: Path) -> None:
    ola = _user(db, "ola")
    session_hash = _session_hash(db, ola, tmp_path)
    workspace_id = _create(db, ola, session_hash)
    with db.factory() as session:
        workspace = session.get(Workspace, workspace_id)
        assert workspace is not None
        assert (
            workspace.name,
            workspace.base_currency,
            workspace.country_code,
            workspace.timezone,
            workspace.locale,
        ) == ("Side project", "PLN", "PL", "Europe/Warsaw", "pl")
        roles = session.scalars(
            select(Membership.role).where(Membership.workspace_id == workspace_id)
        ).all()
        current = session.get(UserSession, session_hash)
        assert current is not None and current.current_workspace_id == workspace_id
    assert roles == ["owner"]
    assert db.roles(workspace_id) == {"ola": "owner"}


def test_the_new_workspace_is_seeded_and_audited_in_its_own_log(
    db: WorkspaceDb, tmp_path: Path
) -> None:
    ola = _user(db, "ola")
    workspace_id = _create(db, ola, _session_hash(db, ola, tmp_path), seed_locale="pl")
    with db.factory() as session:
        names = session.scalars(
            select(Category.name).where(Category.workspace_id == workspace_id)
        ).all()
    assert len(names) == len(SEED) and gettext("Food", "pl") in names
    assert _events(db, workspace_id) == [
        ("category.seeded", ola, {"count": len(SEED), "automatic": True}),
        ("workspace.created", ola, {}),
    ]
    assert _events(db, db.workspace_id) == []  # nothing in any other workspace
    assert _events(db, None) == []  # and nothing instance-level (owner decision P5)


def test_a_blank_name_becomes_the_display_name(db: WorkspaceDb, tmp_path: Path) -> None:
    ola = _user(db, "ola")
    workspace_id = _create(db, ola, _session_hash(db, ola, tmp_path), name="   ")
    with db.factory() as session:
        workspace = session.get(Workspace, workspace_id)
        assert workspace is not None and workspace.name == "Ola"


@pytest.mark.parametrize(
    ("overrides", "field", "code"),
    [
        ({"name": "x" * 121}, "name", "invalid_workspace_name"),
        ({"base_currency": "XXX"}, "base_currency", "invalid_base_currency"),
        ({"country_code": "ZZ"}, "country_code", "invalid_country_code"),
        ({"timezone": "Mars/Base"}, "timezone", "invalid_timezone"),
        ({"locale": "xx"}, "locale", "invalid_locale"),
    ],
)
def test_invalid_values_create_nothing(
    db: WorkspaceDb, tmp_path: Path, overrides: dict[str, str], field: str, code: str
) -> None:
    ola = _user(db, "ola")
    before = _workspace_count(db)
    with pytest.raises(InvalidWorkspaceSettings) as invalid:
        _create(db, ola, _session_hash(db, ola, tmp_path), **overrides)
    assert (invalid.value.field, invalid.value.code) == (field, code)
    assert _workspace_count(db) == before


def test_creation_is_refused_when_the_setting_is_off(db: WorkspaceDb, tmp_path: Path) -> None:
    ola = _user(db, "ola")
    _set_allowed(db, False)
    before = _workspace_count(db)
    with pytest.raises(WorkspaceCreationDisabled):
        _create(db, ola, _session_hash(db, ola, tmp_path))
    assert _workspace_count(db) == before


def test_instance_administrators_can_always_create(db: WorkspaceDb, tmp_path: Path) -> None:
    ada = _user(db, "ada", admin=True)
    _set_allowed(db, False)
    workspace_id = _create(db, ada, _session_hash(db, ada, tmp_path))
    assert db.roles(workspace_id) == {"ada": "owner"}


def test_an_inactive_account_cannot_create(db: WorkspaceDb, tmp_path: Path) -> None:
    ola = _user(db, "ola")
    session_hash = _session_hash(db, ola, tmp_path)
    with db.factory.begin() as session:
        session.execute(update(User).where(User.id == ola).values(is_active=False))
    with pytest.raises(WorkspaceCreationDisabled):
        _create(db, ola, session_hash)


def test_the_setting_is_read_inside_the_write_transaction(
    db: WorkspaceDb, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An administrator turns creation off while a request is between its page check and its
    write: the write waits for the change and is refused."""
    ola, ada = _user(db, "ola"), _user(db, "ada", admin=True)
    session_hash = _session_hash(db, ola, tmp_path)
    second_seen: list[bool] = []

    def turn_off() -> None:
        with db.factory.begin() as session:
            admin = session.get(User, ada)
            assert admin is not None
            update_settings(
                session,
                admin,
                allow_workspace_creation=False,
                clock=db.clock,
                host=None,
                user_agent=None,
            )
            workspaces._checkpoint("turn_off")  # pyright: ignore[reportPrivateUsage]

    def create() -> None:
        second_seen.append(True)
        _create(db, ola, session_hash)

    # The admin's change holds the write lock at the checkpoint; the creation waits at BEGIN.
    with db.factory.begin() as session:
        session.execute(update(InstanceSettings).values(allow_workspace_creation=True))
    outcomes = serialize_at_checkpoint(db.engine, monkeypatch, workspaces, turn_off, create)
    assert outcomes["first"] is None and second_seen
    assert isinstance(outcomes["second"], WorkspaceCreationDisabled)


def test_a_missing_settings_row_reads_as_the_defaults(db: WorkspaceDb, tmp_path: Path) -> None:
    with db.factory.begin() as session:
        session.execute(delete(InstanceSettings))
    assert get_settings(db.factory) == InstanceSettingsView(allow_workspace_creation=True)
    ola = _user(db, "ola")
    assert _create(db, ola, _session_hash(db, ola, tmp_path))


# The instance setting


def test_the_setting_is_on_by_default(db: WorkspaceDb) -> None:
    assert get_settings(db.factory).allow_workspace_creation is True


@pytest.mark.parametrize(("allowed", "admin", "expected"), [
    (True, False, True), (False, False, False), (False, True, True), (True, True, True),
])  # fmt: skip
def test_workspace_creation_allowed(
    db: WorkspaceDb, allowed: bool, admin: bool, expected: bool
) -> None:
    _set_allowed(db, allowed)
    with db.factory() as session:
        assert workspace_creation_allowed(session, is_instance_admin=admin) is expected


def test_an_administrator_changes_the_setting_and_it_is_audited(db: WorkspaceDb) -> None:
    ada = _user(db, "ada", admin=True)
    with db.factory.begin() as session:
        admin = session.get(User, ada)
        assert admin is not None
        status = update_settings(
            session, admin, allow_workspace_creation=False, clock=db.clock,
            host="192.0.2.1", user_agent="test",
        )  # fmt: skip
    assert status == "done"
    with db.factory() as session:
        row = session.get(InstanceSettings, 1)
        assert row is not None
        assert (row.allow_workspace_creation, row.updated_by_user_id) == (False, ada)
        assert row.updated_at == db.clock.now()
        event = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "instance.settings_updated")
        ).one()
        assert (event.workspace_id, event.actor_user_id, event.entity_type) == (
            None,
            ada,
            "instance",
        )
        assert event.data == {"allow_workspace_creation": False}
    with db.factory.begin() as session:
        admin = session.get(User, ada)
        assert admin is not None
        again = update_settings(
            session, admin, allow_workspace_creation=False, clock=db.clock,
            host=None, user_agent=None,
        )  # fmt: skip
    assert again == "unchanged"
    assert len([e for e in _events(db, None) if e[0] == "instance.settings_updated"]) == 1


@pytest.mark.parametrize(("admin", "active"), [(False, True), (True, False)])
def test_only_active_administrators_change_the_setting(
    db: WorkspaceDb, admin: bool, active: bool
) -> None:
    user_id = _user(db, "someone", admin=admin, active=active)
    with db.factory.begin() as session, pytest.raises(NotInstanceAdmin):
        actor = session.get(User, user_id)
        assert actor is not None
        update_settings(
            session, actor, allow_workspace_creation=False, clock=db.clock,
            host=None, user_agent=None,
        )  # fmt: skip
    assert get_settings(db.factory).allow_workspace_creation is True
