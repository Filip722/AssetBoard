"""The per-request ``Context`` and role guards (F1.2).

A ``Context`` is built for every request from the signed-in session and the workspace selected
in it. Membership and role are read from the database each time, so a role change or a removed
membership applies on the next request. Services take a ``Context`` and check roles themselves
with ``require_role``; routes never decide authorization alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.orm import Session

from assetboard.clock import Clock
from assetboard.db.models.identity import UserSession
from assetboard.db.repositories.membership import MembershipRepository
from assetboard.db.repositories.user_memberships import UserMembershipReader

if TYPE_CHECKING:
    from assetboard.services.auth import WebSession


class Role(StrEnum):
    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"


# Explicit ranks: each role may do everything the roles below it may do.
ROLE_RANK: dict[Role, int] = {Role.VIEWER: 1, Role.EDITOR: 2, Role.OWNER: 3}


@dataclass(frozen=True, slots=True)
class UserRef:
    id: UUID
    email: str
    display_name: str
    locale: str | None  # None: follows the browser (F1.5)
    is_instance_admin: bool


@dataclass(frozen=True, slots=True)
class WorkspaceRef:
    id: UUID
    name: str
    base_currency: str
    locale: str
    timezone: str
    country_code: str | None


@dataclass(frozen=True, slots=True)
class Context:
    """Who is acting, in which workspace, with which role. Detached from any ORM session."""

    user: UserRef
    workspace: WorkspaceRef
    role: Role
    clock: Clock


class PermissionDenied(Exception):
    """The current role is below the role an operation needs."""

    def __init__(self, required: Role, actual: Role) -> None:
        super().__init__(f"requires {required.value}, has {actual.value}")
        self.required = required
        self.actual = actual


class NoWorkspace(Exception):
    """The signed-in user is not a member of any workspace."""


class WorkspaceNotAvailable(Exception):
    """The requested workspace does not exist or the user is not a member of it."""


def has_role(ctx: Context, minimum: Role | str) -> bool:
    return ROLE_RANK[ctx.role] >= ROLE_RANK[Role(minimum)]


def require_role(ctx: Context, minimum: Role | str) -> None:
    """Raise ``PermissionDenied`` unless ``ctx.role`` is at least ``minimum``."""
    required = Role(minimum)
    if not has_role(ctx, required):
        raise PermissionDenied(required, ctx.role)


def require_role_now(session: Session, ctx: Context, minimum: Role | str) -> None:
    """Re-read the actor's membership in ``session``; raise unless it is at least ``minimum``.

    A ``Context`` is built at the start of a request and can be stale by the time a write runs
    (the actor may have been demoted or removed meanwhile). Writes call this inside their
    ``BEGIN IMMEDIATE`` transaction, after the cheap ``require_role`` check on the Context.
    A removed member counts as a viewer for the error.
    """
    required = Role(minimum)
    membership = MembershipRepository(session, ctx).for_user(ctx.user.id)
    actual = Role(membership.role) if membership is not None else Role.VIEWER
    if membership is None or ROLE_RANK[actual] < ROLE_RANK[required]:
        raise PermissionDenied(required, actual)


def build_context(session: Session, web_session: WebSession, *, clock: Clock) -> Context:
    """The Context for a signed-in request.

    Uses the workspace selected in the session when the user is still a member of it; otherwise
    the oldest membership (by ``created_at``, then ``id``). The fallback is not stored: only
    ``select_workspace`` writes the selection.
    """
    reader = UserMembershipReader(session, web_session.user_id)
    membership = None
    if web_session.current_workspace_id is not None:
        membership = reader.membership_in(web_session.current_workspace_id)
    if membership is None:
        memberships = reader.memberships()
        if not memberships:
            raise NoWorkspace
        membership = memberships[0]
    return Context(
        user=UserRef(
            id=web_session.user_id,
            email=web_session.email,
            display_name=web_session.display_name,
            locale=web_session.locale,
            is_instance_admin=web_session.is_instance_admin,
        ),
        workspace=WorkspaceRef(
            id=membership.workspace_id,
            name=membership.workspace_name,
            base_currency=membership.base_currency,
            locale=membership.workspace_locale,
            timezone=membership.timezone,
            country_code=membership.country_code,
        ),
        role=Role(membership.role),
        clock=clock,
    )


@dataclass(frozen=True, slots=True)
class WorkspaceChoice:
    id: UUID
    name: str
    role: Role


@dataclass(frozen=True, slots=True)
class WorkspaceNav:
    """What the page chrome shows about workspaces: the current one and the switcher's list."""

    current: WorkspaceRef
    role: Role
    choices: tuple[WorkspaceChoice, ...]

    @property
    def can_switch(self) -> bool:
        return len(self.choices) > 1


def workspace_nav(session: Session, ctx: Context) -> WorkspaceNav:
    """The user's own memberships (one query), for the switcher on workspace pages."""
    memberships = UserMembershipReader(session, ctx.user.id).memberships()
    choices = tuple(
        WorkspaceChoice(id=m.workspace_id, name=m.workspace_name, role=Role(m.role))
        for m in memberships
    )
    return WorkspaceNav(current=ctx.workspace, role=ctx.role, choices=choices)


def select_workspace(
    session: Session, *, session_hash: str, user_id: UUID, workspace_id: UUID
) -> None:
    """Store the workspace a session works in; refuses workspaces the user is not a member of."""
    if UserMembershipReader(session, user_id).membership_in(workspace_id) is None:
        raise WorkspaceNotAvailable
    row = session.get(UserSession, session_hash)
    if row is None or row.user_id != user_id:
        raise WorkspaceNotAvailable
    row.current_workspace_id = workspace_id
    session.flush()
