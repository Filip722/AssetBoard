"""The signed-in user's own memberships, across workspaces.

This reader is scoped by user, not by workspace: it is what the Context and the workspace switcher
need before a workspace is known. It returns only rows whose ``user_id`` is the reader's user.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from assetboard.db.models.identity import Membership, Workspace


@dataclass(frozen=True, slots=True)
class MembershipView:
    membership_id: UUID
    workspace_id: UUID
    workspace_name: str
    base_currency: str
    workspace_locale: str
    timezone: str
    country_code: str | None
    role: str
    created_at: datetime
    require_two_factor: bool = False


class UserMembershipReader:
    def __init__(self, session: Session, user_id: UUID) -> None:
        self._session = session
        self._user_id = user_id

    def _select(self) -> Select[Membership, Workspace]:
        return (
            select(Membership, Workspace)
            .join(Workspace, Workspace.id == Membership.workspace_id)
            .where(Membership.user_id == self._user_id)
        )

    def memberships(self) -> list[MembershipView]:
        """All of the user's memberships, oldest first (``created_at``, then ``id``)."""
        statement = self._select().order_by(Membership.created_at, Membership.id)
        return [_view(m, w) for m, w in self._session.execute(statement)]

    def membership_in(self, workspace_id: UUID) -> MembershipView | None:
        statement = self._select().where(Membership.workspace_id == workspace_id)
        row = self._session.execute(statement).one_or_none()
        return _view(row[0], row[1]) if row else None

    def authorization_in(self, workspace_id: UUID) -> tuple[str, bool] | None:
        row = self._session.execute(
            select(Membership.role, Workspace.require_two_factor)
            .join(Workspace, Workspace.id == Membership.workspace_id)
            .where(Membership.user_id == self._user_id, Membership.workspace_id == workspace_id)
        ).one_or_none()
        return (row[0], row[1]) if row is not None else None


def _view(membership: Membership, workspace: Workspace) -> MembershipView:
    return MembershipView(
        membership_id=membership.id,
        workspace_id=workspace.id,
        workspace_name=workspace.name,
        base_currency=workspace.base_currency,
        workspace_locale=workspace.locale,
        timezone=workspace.timezone,
        country_code=workspace.country_code,
        role=membership.role,
        created_at=membership.created_at,
        require_two_factor=workspace.require_two_factor,
    )
