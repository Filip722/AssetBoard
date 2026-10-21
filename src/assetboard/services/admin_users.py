"""Instance administration of user accounts (F1.5 part B).

Every mutation runs inside the acting administrator's confirmation (``web.confirm.
run_confirmed``), in that same ``BEGIN IMMEDIATE`` transaction, and checks there that the actor
is still an active instance administrator. Self-protection (no deactivating or demoting
yourself) and last-administrator protection are decided in that transaction too, so two
administrators acting on each other at the same moment cannot remove every administrator.

Audit actions (instance-level, ``workspace_id`` NULL, ``entity_id`` = the target user) and the
only ``data`` keys each one carries:

* ``admin.user_created``: ``temporary_password`` (always ``true``; never the password itself)
* ``admin.user_activated``: none
* ``admin.user_deactivated``: ``sessions_revoked`` (how many sessions ended)
* ``admin.admin_granted``: none
* ``admin.admin_revoked``: none
* ``admin.totp_reset``: ``sessions_revoked`` (how many sessions ended)
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from assetboard.clock import Clock
from assetboard.db.models.identity import ExternalIdentity, User
from assetboard.db.models.totp import TotpFactor
from assetboard.db.repositories.totp import TotpRepository
from assetboard.services.auth import (
    PasswordPolicyError,
    record_auth_event,
    revoke_all_sessions,
    validate_password,
)
from assetboard.services.external_identity import PROXY_PROVIDER

# The allowlist the audit view (part C) renders ``data`` through; nothing else is shown.
AUDIT_DATA_KEYS: dict[str, tuple[str, ...]] = {
    "admin.user_created": ("temporary_password",),
    "admin.user_activated": (),
    "admin.user_deactivated": ("sessions_revoked",),
    "admin.admin_granted": (),
    "admin.admin_revoked": (),
    "admin.totp_reset": ("sessions_revoked",),
}

AdminAction = Literal["activate", "deactivate", "promote", "demote", "reset_totp"]
ADMIN_ACTIONS: tuple[AdminAction, ...] = (
    "activate",
    "deactivate",
    "promote",
    "demote",
    "reset_totp",
)
ActionStatus = Literal["done", "unchanged", "self", "self_totp_reset", "last_admin", "not_found"]
TEMPORARY_PASSWORD_BYTES = 12  # token_urlsafe(12): 16 characters, 96 bits
TEMPORARY_PASSWORD_ATTEMPTS = 20


class NotInstanceAdmin(Exception):
    """The acting user is not an active instance administrator."""


def _checkpoint(_name: str) -> None:
    """Test seam: called inside the action transaction after the actor was checked."""


def require_instance_admin(session: Session, actor_id: UUID) -> User:
    actor = session.get(User, actor_id)
    if actor is None or not actor.is_active or not actor.is_instance_admin:
        raise NotInstanceAdmin
    return actor


@dataclass(frozen=True, slots=True)
class UserRow:
    """What the users list shows: sign-in methods as labels, never subjects."""

    id: UUID
    email: str
    display_name: str
    is_active: bool
    is_instance_admin: bool
    must_change_password: bool
    last_login_at: datetime | None
    created_at: datetime
    methods: tuple[Literal["password", "oidc", "proxy"], ...]
    has_two_factor: bool


def _row(session: Session, user: User) -> UserRow:
    providers = set(
        session.scalars(
            select(ExternalIdentity.provider).where(ExternalIdentity.user_id == user.id)
        ).all()
    )
    methods: list[Literal["password", "oidc", "proxy"]] = []
    if user.password_hash is not None:
        methods.append("password")
    if providers - {PROXY_PROVIDER}:
        methods.append("oidc")
    if PROXY_PROVIDER in providers:
        methods.append("proxy")
    factor = session.get(TotpFactor, user.id)
    return UserRow(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        is_active=user.is_active,
        is_instance_admin=user.is_instance_admin,
        must_change_password=user.must_change_password,
        last_login_at=user.last_login_at,
        created_at=user.created_at,
        methods=tuple(methods),
        has_two_factor=factor is not None and factor.enabled_at is not None,
    )


def list_users(factory: sessionmaker[Session], *, actor_id: UUID) -> list[UserRow]:
    with factory() as session:
        require_instance_admin(session, actor_id)
        users = session.scalars(select(User).order_by(User.email)).all()
        return [_row(session, user) for user in users]


def get_user(factory: sessionmaker[Session], *, actor_id: UUID, user_id: UUID) -> UserRow | None:
    with factory() as session:
        require_instance_admin(session, actor_id)
        user = session.get(User, user_id)
        return _row(session, user) if user is not None else None


def _other_active_admins(session: Session, user_id: UUID) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(User)
            .where(User.is_instance_admin.is_(True), User.is_active.is_(True), User.id != user_id)
        )
        or 0
    )


def _protection(
    session: Session, actor: User, target: User, action: AdminAction
) -> ActionStatus | None:
    """Self and last-administrator protection, decided in the action's transaction.

    With the actor checked first, the actor is itself another active administrator, so the
    last-administrator rule cannot trigger through ``apply_admin_action``; it stays as a second
    line of defence (and is tested on its own).
    """
    if target.id == actor.id and action in {"deactivate", "demote"}:
        return "self"
    removes_admin = target.is_instance_admin and target.is_active
    if (
        action in {"deactivate", "demote"}
        and removes_admin
        and not _other_active_admins(session, target.id)
    ):
        return "last_admin"
    return None


def apply_admin_action(
    session: Session,
    actor: User,
    target_id: UUID,
    action: AdminAction,
    *,
    clock: Clock,
    host: str | None,
    user_agent: str | None,
) -> ActionStatus:
    """One administrator action, inside the confirming transaction (``actor`` confirmed)."""
    if not actor.is_active or not actor.is_instance_admin:
        raise NotInstanceAdmin
    target = session.get(User, target_id)
    if target is None:
        return "not_found"
    _checkpoint("admin_action")
    refusal = _protection(session, actor, target, action)
    if refusal is not None:
        return refusal
    data: dict[str, str | int | bool] = {}
    if action == "reset_totp":
        if target.id == actor.id:
            return "self_totp_reset"
        repository = TotpRepository(session, target.id)
        factor = repository.factor(lock=True)
        if factor is None or factor.enabled_at is None:
            return "unchanged"
        repository.clear()
        data["sessions_revoked"] = revoke_all_sessions(session, target.id)
        audit = "admin.totp_reset"
    elif action == "activate":
        if target.is_active:
            return "unchanged"
        target.is_active = True
        audit = "admin.user_activated"
    elif action == "deactivate":
        if not target.is_active:
            return "unchanged"
        target.is_active = False
        # Every session ends, password, OIDC and proxy alike; sign-in already refuses
        # inactive accounts on every path.
        data["sessions_revoked"] = revoke_all_sessions(session, target.id)
        audit = "admin.user_deactivated"
    elif action == "promote":
        if target.is_instance_admin:
            return "unchanged"
        target.is_instance_admin = True
        audit = "admin.admin_granted"
    else:
        if not target.is_instance_admin:
            return "unchanged"
        target.is_instance_admin = False
        audit = "admin.admin_revoked"
    record_auth_event(
        session,
        clock,
        action=audit,
        entity_id=str(target.id),
        actor_user_id=actor.id,
        host=host,
        user_agent=user_agent,
        data=data,
    )
    return "done"


def generate_temporary_password(email: str, display_name: str) -> str:
    """16 random URL-safe characters that pass the password policy (shown once, never kept).

    A random 16-character string practically always passes; after ``TEMPORARY_PASSWORD_ATTEMPTS``
    failures something else is wrong, so this raises instead of looping.
    """
    for _attempt in range(TEMPORARY_PASSWORD_ATTEMPTS):
        password = secrets.token_urlsafe(TEMPORARY_PASSWORD_BYTES)
        try:
            validate_password(password, email, display_name)
        except PasswordPolicyError:
            continue
        return password
    raise RuntimeError("could not generate a temporary password that passes the policy")


def record_user_created(
    session: Session,
    actor: User,
    user: User,
    *,
    clock: Clock,
    host: str | None,
    user_agent: str | None,
) -> None:
    record_auth_event(
        session,
        clock,
        action="admin.user_created",
        entity_id=str(user.id),
        actor_user_id=actor.id,
        host=host,
        user_agent=user_agent,
        data={"temporary_password": True},
    )
