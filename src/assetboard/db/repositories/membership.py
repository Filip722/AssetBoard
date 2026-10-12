from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select

from assetboard.db.models.identity import Membership, User
from assetboard.db.repositories.base import ScopedRepository


@dataclass(frozen=True, slots=True)
class WorkspaceMember:
    membership_id: UUID
    user_id: UUID
    display_name: str
    email: str
    role: str
    joined_at: datetime


class MembershipRepository(ScopedRepository[Membership]):
    """Members of the current workspace."""

    model = Membership

    def members(self) -> list[WorkspaceMember]:
        """The workspace's members with their names, oldest membership first."""
        statement = (
            self._scoped_select()
            .add_columns(User.display_name, User.email)
            .join(User, User.id == Membership.user_id)
            .order_by(Membership.created_at, Membership.id)
        )
        return [
            WorkspaceMember(
                membership_id=membership.id,
                user_id=membership.user_id,
                display_name=display_name,
                email=email,
                role=membership.role,
                joined_at=membership.created_at,
            )
            for membership, display_name, email in self._session.execute(statement)
        ]

    def for_user(self, user_id: UUID) -> Membership | None:
        """The user's membership in this workspace, if any."""
        statement = self._scoped_select().where(Membership.user_id == user_id)
        return self._session.scalars(statement).one_or_none()

    def add_member(self, *, user_id: UUID, role: str) -> Membership:
        """Add a user to this workspace (invitation acceptance)."""
        return self.add(Membership(user_id=user_id, role=role))

    def count_owners(self) -> int:
        statement = (
            select(func.count())
            .select_from(Membership)
            .where(Membership.workspace_id == self._workspace_id, Membership.role == "owner")
        )
        return self._session.execute(statement).scalar_one()
