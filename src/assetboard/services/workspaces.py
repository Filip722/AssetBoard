"""Workspace settings and members (F1.2 part B1), and creating a workspace (pre-release A).

Settings, members and invitations are for owners only. ``create_own_workspace`` is for any
signed-in account while ``instance_settings.allow_workspace_creation`` is on (instance
administrators always): it needs no Context, since the user may have no workspace yet.

A ``Context`` is built at the start of a request and can be stale by the time a write runs (the
actor may have been demoted or removed meanwhile). So every write runs in one
``BEGIN IMMEDIATE`` transaction that re-reads the actor's membership, requires ``owner`` there,
and checks the last-owner rule in the same transaction. Concurrent writes queue at BEGIN and
each sees the previous one's result.

**Base currency.** Stored amounts are in the base currency and are never converted, so the
currency is locked once any amount exists (soft-deleted rows included). Each feature that stores
amounts adds a check to ``BASE_CURRENCY_LOCKS``: F2.2 transactions (soft-deleted ones included)
and F2.3 budget lines (the lines that exist now: a removed planned value holds no amount).
The settings write takes the exclusive workspace lock first, so on PostgreSQL a currency change
and a concurrent first transaction (which takes the shared lock) cannot both pass.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from assetboard.clock import Clock
from assetboard.db.engine import run_immediate
from assetboard.db.models.identity import User
from assetboard.db.repositories import (
    AuditEventRepository,
    CurrentWorkspaceRepository,
    InvitationRepository,
    MembershipRepository,
    WorkspaceMember,
    create_owned_workspace,
)
from assetboard.domain.identity import InvalidEmail, normalize_email
from assetboard.services import categories
from assetboard.services.budget import workspace_has_budget_lines
from assetboard.services.context import (
    Context,
    PermissionDenied,
    Role,
    require_role,
    select_workspace,
)
from assetboard.services.instance_settings import read_settings
from assetboard.services.invitations import new_token
from assetboard.services.scheduled import workspace_has_scheduled_amounts
from assetboard.services.transactions import workspace_has_transactions
from assetboard.services.workspace_options import WorkspaceOptionError, validate_workspace_options

MAX_WORKSPACE_NAME_LENGTH = 120
INVITATION_EXPIRY_DAYS = (1, 7, 30)
DEFAULT_INVITATION_EXPIRY_DAYS = 7
MAX_PENDING_INVITATIONS = 50

# Checks that lock the base currency: each returns True while amounts exist in the workspace.
BASE_CURRENCY_LOCKS: list[Callable[[Session, Context], bool]] = [
    workspace_has_transactions,
    workspace_has_budget_lines,
    workspace_has_scheduled_amounts,
]


class WorkspaceError(Exception):
    code = "invalid"


class LastOwnerError(WorkspaceError):
    """The change would leave the workspace without an owner."""

    code = "last_owner"


class SelfRemovalError(WorkspaceError):
    """Owners cannot remove themselves (leaving a workspace is a later feature)."""

    code = "self_removal"


class InvalidWorkspaceSettings(WorkspaceError):
    def __init__(self, field: str, code: str) -> None:
        super().__init__(code)
        self.field = field
        self.code = code


class WorkspaceCreationDisabled(WorkspaceError):
    """Self-service workspace creation is off for this account (or the account is inactive)."""

    code = "creation_disabled"


class InvalidInvitation(WorkspaceError):
    def __init__(self, field: str, code: str) -> None:
        super().__init__(code)
        self.field = field
        self.code = code


class TooManyInvitations(WorkspaceError):
    """The workspace already has ``MAX_PENDING_INVITATIONS`` pending invitations."""

    code = "too_many_invitations"


@dataclass(frozen=True, slots=True)
class CreatedInvitation:
    """Returned once: the token is not stored and cannot be shown again."""

    id: UUID
    token: str
    role: str
    email: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class PendingInvitation:
    id: UUID
    role: str
    email: str | None  # None only for invitations created before the lock became mandatory
    expires_at: datetime
    created_at: datetime


@dataclass(frozen=True, slots=True)
class WorkspaceSettings:
    name: str
    base_currency: str
    country_code: str | None
    timezone: str
    locale: str
    # F2.3: copy the previous month's plan when a month is first opened.
    auto_copy_plan: bool = False
    # True once amounts are stored in the base currency (it can no longer change).
    base_currency_locked: bool = False


def _checkpoint(_name: str) -> None:
    """Test seam: called inside write transactions after the actor's role was re-read."""


def _require_owner_now(session: Session, ctx: Context) -> None:
    """Re-read the actor's membership inside the write transaction; it must still be owner."""
    actor = MembershipRepository(session, ctx).for_user(ctx.user.id)
    actual = Role(actor.role) if actor is not None else Role.VIEWER
    if actor is None or actual is not Role.OWNER:
        raise PermissionDenied(Role.OWNER, actual)


def _base_currency_locked(session: Session, ctx: Context) -> bool:
    return any(check(session, ctx) for check in BASE_CURRENCY_LOCKS)


class BaseCurrencyLocked(InvalidWorkspaceSettings):
    """Amounts exist in the current base currency, so it cannot change."""

    def __init__(self) -> None:
        super().__init__("base_currency", "base_currency_locked")


def workspace_settings(factory: sessionmaker[Session], ctx: Context) -> WorkspaceSettings:
    require_role(ctx, Role.OWNER)
    with factory() as session:
        workspace = CurrentWorkspaceRepository(session, ctx).get()
        return WorkspaceSettings(
            name=workspace.name,
            base_currency=workspace.base_currency,
            country_code=workspace.country_code,
            timezone=workspace.timezone,
            locale=workspace.locale,
            auto_copy_plan=workspace.auto_copy_plan,
            base_currency_locked=_base_currency_locked(session, ctx),
        )


def update_settings(
    factory: sessionmaker[Session],
    ctx: Context,
    *,
    name: str,
    base_currency: str,
    country_code: str | None,
    timezone: str,
    locale: str,
    auto_copy_plan: bool,
) -> WorkspaceSettings:
    """Change the workspace's settings; the audit event lists changed fields, not values."""
    require_role(ctx, Role.OWNER)
    clean_name = name.strip()
    if not clean_name or len(clean_name) > MAX_WORKSPACE_NAME_LENGTH:
        raise InvalidWorkspaceSettings("name", "invalid_workspace_name")
    try:
        options = validate_workspace_options(
            base_currency=base_currency,
            country_code=country_code,
            timezone=timezone,
            locale=locale,
        )
    except WorkspaceOptionError as exc:
        raise InvalidWorkspaceSettings(exc.field, f"invalid_{exc.field}") from exc
    wanted = WorkspaceSettings(
        name=clean_name,
        base_currency=options.base_currency,
        country_code=options.country_code,
        timezone=options.timezone,
        locale=options.locale,
        auto_copy_plan=auto_copy_plan,
    )

    def write(session: Session) -> WorkspaceSettings:
        _require_owner_now(session, ctx)
        _checkpoint("update_settings")
        repository = CurrentWorkspaceRepository(session, ctx)
        repository.lock(exclusive=True)  # before any read: orders against amount writes
        workspace = repository.get()
        changes = {
            field: value
            for field, value in (
                ("name", wanted.name),
                ("base_currency", wanted.base_currency),
                ("country_code", wanted.country_code),
                ("timezone", wanted.timezone),
                ("locale", wanted.locale),
                ("auto_copy_plan", wanted.auto_copy_plan),
            )
            if getattr(workspace, field) != value
        }
        if "base_currency" in changes and _base_currency_locked(session, ctx):
            raise BaseCurrencyLocked
        if changes:
            repository.update(**changes)
            AuditEventRepository(session, ctx).record(
                actor_user_id=ctx.user.id,
                action="workspace.settings_updated",
                entity_type="workspace",
                entity_id=str(ctx.workspace.id),
                ts=ctx.clock.now(),
                data={"fields": ",".join(sorted(changes))},
            )
        return replace(wanted, base_currency_locked=_base_currency_locked(session, ctx))

    return run_immediate(factory, write)


def list_members(factory: sessionmaker[Session], ctx: Context) -> list[WorkspaceMember]:
    require_role(ctx, Role.OWNER)
    with factory() as session:
        return MembershipRepository(session, ctx).members()


def change_role(
    factory: sessionmaker[Session], ctx: Context, *, membership_id: UUID, role: Role | str
) -> None:
    """Set a member's role. The last owner cannot be demoted, not even by themselves."""
    require_role(ctx, Role.OWNER)
    new_role = Role(role)

    def write(session: Session) -> None:
        _require_owner_now(session, ctx)
        _checkpoint("change_role")
        memberships = MembershipRepository(session, ctx)
        target = memberships.get_or_raise(membership_id)
        old_role = Role(target.role)
        if old_role is new_role:
            return
        if old_role is Role.OWNER and memberships.count_owners() <= 1:
            raise LastOwnerError
        memberships.update(target.id, role=new_role.value)
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="member.role_changed",
            entity_type="membership",
            entity_id=str(target.id),
            ts=ctx.clock.now(),
            data={
                "user_id": str(target.user_id),
                "from": old_role.value,
                "to": new_role.value,
            },
        )

    run_immediate(factory, write)


def remove_member(factory: sessionmaker[Session], ctx: Context, *, membership_id: UUID) -> None:
    """Remove a member. Owners cannot remove themselves, and the last owner stays."""
    require_role(ctx, Role.OWNER)

    def write(session: Session) -> None:
        _require_owner_now(session, ctx)
        _checkpoint("remove_member")
        memberships = MembershipRepository(session, ctx)
        target = memberships.get_or_raise(membership_id)
        if target.user_id == ctx.user.id:
            raise SelfRemovalError
        if Role(target.role) is Role.OWNER and memberships.count_owners() <= 1:
            raise LastOwnerError
        removed_role = target.role
        memberships.delete(target)
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="member.removed",
            entity_type="membership",
            entity_id=str(target.id),
            ts=ctx.clock.now(),
            data={"user_id": str(target.user_id), "role": removed_role},
        )

    run_immediate(factory, write)


# Invitations (owners); the invitee's side is services/invitations.py


def list_invitations(factory: sessionmaker[Session], ctx: Context) -> list[PendingInvitation]:
    require_role(ctx, Role.OWNER)
    with factory() as session:
        return [
            PendingInvitation(
                id=invitation.id,
                role=invitation.role,
                email=invitation.email,
                expires_at=invitation.expires_at,
                created_at=invitation.created_at,
            )
            for invitation in InvitationRepository(session, ctx).list_pending(ctx.clock.now())
        ]


def create_invitation(
    factory: sessionmaker[Session],
    ctx: Context,
    *,
    role: Role | str,
    email: str,
    expires_in_days: int,
) -> CreatedInvitation:
    """Create a single-use invitation link, locked to an email address (every role).

    Invitations created before the lock became mandatory may have no email; they stay usable
    until they expire, are used or are revoked (``services.invitations`` accepts them). No
    account is looked up here, so creating an invitation reveals nothing about which addresses
    are registered.
    """
    require_role(ctx, Role.OWNER)
    invited_role = Role(role)
    if expires_in_days not in INVITATION_EXPIRY_DAYS:
        raise InvalidInvitation("expires_in_days", "invalid_expiry")
    if not email.strip():
        raise InvalidInvitation("email", "email_required")
    try:
        locked_email = normalize_email(email)
    except InvalidEmail as exc:
        raise InvalidInvitation("email", "invalid_email") from exc
    token, token_hash = new_token()
    now = ctx.clock.now()
    expires_at = now + timedelta(days=expires_in_days)

    def write(session: Session) -> CreatedInvitation:
        _require_owner_now(session, ctx)
        _checkpoint("create_invitation")
        invitations = InvitationRepository(session, ctx)
        if invitations.count_pending(now) >= MAX_PENDING_INVITATIONS:
            raise TooManyInvitations
        invitation = invitations.create(
            token_hash=token_hash,
            role=invited_role.value,
            email=locked_email,
            expires_at=expires_at,
            created_by_user_id=ctx.user.id,
        )
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="invitation.created",
            entity_type="invitation",
            entity_id=str(invitation.id),
            ts=now,
            data={
                "role": invited_role.value,
                "email_locked": True,  # always, since pre-release A
                "expires_in_days": expires_in_days,
            },
        )
        return CreatedInvitation(
            invitation.id, token, invited_role.value, locked_email, invitation.expires_at
        )

    return run_immediate(factory, write)


def revoke_invitation(factory: sessionmaker[Session], ctx: Context, *, invitation_id: UUID) -> None:
    require_role(ctx, Role.OWNER)

    def write(session: Session) -> None:
        _require_owner_now(session, ctx)
        _checkpoint("revoke_invitation")
        now = ctx.clock.now()
        invitation = InvitationRepository(session, ctx).revoke(invitation_id, now)
        AuditEventRepository(session, ctx).record(
            actor_user_id=ctx.user.id,
            action="invitation.revoked",
            entity_type="invitation",
            entity_id=str(invitation.id),
            ts=now,
            data={"reason": "owner"},
        )

    run_immediate(factory, write)


# Creating a workspace (pre-release A)


def _workspace_name(name: str) -> str | None:
    """The cleaned name; ``None`` when blank (the account's display name is used then)."""
    clean = name.strip()
    if not clean:
        return None
    if len(clean) > MAX_WORKSPACE_NAME_LENGTH:
        raise InvalidWorkspaceSettings("name", "invalid_workspace_name")
    return clean


def create_own_workspace(
    factory: sessionmaker[Session],
    *,
    user_id: UUID,
    session_hash: str,
    name: str,
    base_currency: str,
    country_code: str | None,
    timezone: str,
    locale: str,
    seed_locale: str,
    clock: Clock,
) -> UUID:
    """Create a workspace the user owns, with the suggested categories, and switch to it.

    One ``BEGIN IMMEDIATE`` transaction re-reads the account (active?) and the instance setting
    (instance administrators may always create), creates the workspace and the owner
    membership, seeds the categories in ``seed_locale``, records ``workspace.created`` in the
    new workspace and selects it in the session ``session_hash``. A blank name becomes the
    account's display name, as at first-run setup.
    """
    clean_name = _workspace_name(name)
    try:
        options = validate_workspace_options(
            base_currency=base_currency,
            country_code=country_code,
            timezone=timezone,
            locale=locale,
        )
    except WorkspaceOptionError as exc:
        raise InvalidWorkspaceSettings(exc.field, f"invalid_{exc.field}") from exc

    def write(session: Session) -> UUID:
        user = session.get(User, user_id)
        if user is None or not user.is_active:
            raise WorkspaceCreationDisabled
        allowed = read_settings(session, lock=True).allow_workspace_creation
        if not (allowed or user.is_instance_admin):
            raise WorkspaceCreationDisabled
        _checkpoint("create_own_workspace")
        now = clock.now()
        workspace, scope = create_owned_workspace(
            session,
            name=clean_name or user.display_name,
            base_currency=options.base_currency,
            country_code=options.country_code,
            timezone=options.timezone,
            locale=options.locale,
            owner_user_id=user.id,
        )
        categories.seed_into(
            session,
            scope,
            locale=seed_locale,
            actor_user_id=user.id,
            actor_type="user",
            now=now,
            automatic=True,
        )
        AuditEventRepository(session, scope).record(
            actor_user_id=user.id,
            action="workspace.created",
            entity_type="workspace",
            entity_id=str(workspace.id),
            ts=now,
        )
        select_workspace(
            session, session_hash=session_hash, user_id=user.id, workspace_id=workspace.id
        )
        return workspace.id

    return run_immediate(factory, write)
