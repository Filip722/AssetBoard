"""Instance settings an administrator changes in the UI (pre-release A).

One row, ``instance_settings.id = 1`` (migration 0013); a missing row reads as the defaults.

* ``allow_workspace_creation`` (default on): any signed-in user may create a workspace and
  becomes its owner. Instance administrators always may. It covers only that self-service
  creation: open registration and OIDC or proxy auto-create follow their own settings.

Changes run inside the acting administrator's confirmation (``web.confirm.run_confirmed``) and
are recorded as the instance-level event ``instance.settings_updated`` with the new value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from sqlalchemy.orm import Session, sessionmaker

from assetboard.clock import Clock
from assetboard.db.models.identity import InstanceSettings, User
from assetboard.services.admin_users import NotInstanceAdmin
from assetboard.services.auth import record_auth_event

ROW_ID = 1
DEFAULT_ALLOW_WORKSPACE_CREATION = True


@dataclass(frozen=True, slots=True)
class InstanceSettingsView:
    allow_workspace_creation: bool = DEFAULT_ALLOW_WORKSPACE_CREATION


def read_settings(session: Session, *, lock: bool = False) -> InstanceSettingsView:
    """The current settings. ``lock`` takes a shared row lock (PostgreSQL) for a write that
    depends on them; SQLite serialises writers at ``BEGIN IMMEDIATE`` instead."""
    row = session.get(InstanceSettings, ROW_ID, with_for_update={"read": True} if lock else None)
    if row is None:
        return InstanceSettingsView()
    return InstanceSettingsView(allow_workspace_creation=row.allow_workspace_creation)


def get_settings(factory: sessionmaker[Session]) -> InstanceSettingsView:
    with factory() as session:
        return read_settings(session)


def workspace_creation_allowed(session: Session, *, is_instance_admin: bool) -> bool:
    """Whether this account may create a workspace now."""
    return is_instance_admin or read_settings(session).allow_workspace_creation


UpdateStatus = Literal["done", "unchanged"]


def update_settings(
    session: Session,
    actor: User,
    *,
    allow_workspace_creation: bool,
    clock: Clock,
    host: str | None,
    user_agent: str | None,
) -> UpdateStatus:
    """Change the settings, inside the confirming transaction (``actor`` confirmed).

    Migration 0013 creates the row, so it always exists; the insert below only covers a
    database where it was deleted by hand. Two administrators inserting it at once could then
    collide on PostgreSQL (a primary-key error, nothing lost); that race is theoretical.
    """
    if not actor.is_active or not actor.is_instance_admin:
        raise NotInstanceAdmin
    row = session.get(InstanceSettings, ROW_ID, with_for_update=True)
    if row is None:
        row = InstanceSettings(id=ROW_ID, allow_workspace_creation=True)
        session.add(row)
    if row.allow_workspace_creation is allow_workspace_creation:
        return "unchanged"
    row.allow_workspace_creation = allow_workspace_creation
    row.updated_at = clock.now()
    row.updated_by_user_id = actor.id
    record_auth_event(
        session,
        clock,
        action="instance.settings_updated",
        entity_type="instance",
        entity_id=None,
        actor_user_id=actor.id,
        host=host,
        user_agent=user_agent,
        data={"allow_workspace_creation": allow_workspace_creation},
    )
    session.flush()
    return "done"
