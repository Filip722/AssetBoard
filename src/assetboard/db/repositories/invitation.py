"""Invitation links (F1.2 part B2).

``InvitationRepository`` is the workspace-scoped side (owners create, list and revoke; the
accept flow marks rows used). Generic ``update`` and ``delete`` are unsupported: an invitation
changes only through the named transitions below, and revoking keeps the row for the audit log.

``InvitationTokenReader`` is scoped by token: the invitee has no Context yet, so it finds an
invitation by the SHA-256 of its token and returns only where it lives. Callers then build a
``WorkspaceScope`` from that row and use ``InvitationRepository``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from assetboard.db.models.identity import Invitation
from assetboard.db.repositories.base import NotFound, ScopedRepository


def _pending(now: datetime) -> tuple[ColumnElement[bool], ...]:
    return (
        Invitation.used_at.is_(None),
        Invitation.revoked_at.is_(None),
        Invitation.expires_at > now,
    )


class InvitationRepository(ScopedRepository[Invitation]):
    model = Invitation
    unsupported = frozenset({"update", "delete"})

    def create(
        self,
        *,
        token_hash: str,
        role: str,
        email: str | None,
        expires_at: datetime,
        created_by_user_id: UUID,
    ) -> Invitation:
        return self.add(
            Invitation(
                token_hash=token_hash,
                role=role,
                email=email,
                expires_at=expires_at,
                created_by_user_id=created_by_user_id,
                failed_email_attempts=0,
            )
        )

    def list_pending(self, now: datetime) -> list[Invitation]:
        statement = (
            self._scoped_select()
            .where(*_pending(now))
            .order_by(Invitation.created_at, Invitation.id)
        )
        return list(self._session.scalars(statement))

    def count_pending(self, now: datetime) -> int:
        statement = (
            select(func.count())
            .select_from(Invitation)
            .where(Invitation.workspace_id == self._workspace_id, *_pending(now))
        )
        return self._session.execute(statement).scalar_one()

    def revoke(self, invitation_id: UUID, now: datetime) -> Invitation:
        """Revoke a pending invitation; used, revoked or expired ones are not found."""
        statement = self._scoped_select().where(
            Invitation.id == invitation_id,
            *_pending(now),
        )
        invitation = self._session.scalars(statement).one_or_none()
        if invitation is None:
            raise NotFound
        invitation.revoked_at = now
        self._session.flush()
        return invitation

    def mark_used(self, invitation_id: UUID, *, user_id: UUID, now: datetime) -> Invitation:
        invitation = self.get_or_raise(invitation_id)
        invitation.used_at = now
        invitation.used_by_user_id = user_id
        self._session.flush()
        return invitation

    def record_failed_email(self, invitation_id: UUID, *, now: datetime, limit: int) -> bool:
        """Count a wrong-email attempt; revokes the invitation at ``limit``. True if revoked."""
        invitation = self.get_or_raise(invitation_id)
        invitation.failed_email_attempts += 1
        revoked = invitation.failed_email_attempts >= limit and invitation.revoked_at is None
        if revoked:
            invitation.revoked_at = now
        self._session.flush()
        return revoked


@dataclass(frozen=True, slots=True)
class InvitationLocation:
    invitation_id: UUID
    workspace_id: UUID


class InvitationTokenReader:
    def __init__(self, session: Session) -> None:
        self._session = session

    def locate(self, token_hash: str) -> InvitationLocation | None:
        """Where the invitation with this token hash lives, whatever its state."""
        statement = select(Invitation.id, Invitation.workspace_id).where(
            Invitation.token_hash == token_hash
        )
        row = self._session.execute(statement).one_or_none()
        return InvitationLocation(row[0], row[1]) if row else None
