"""Invitation tokens: preview, accept, and the checks used by invitation sign-up (F1.2 B2).

Owners create and revoke invitations in ``services.workspaces``. This module is the invitee's
side, where no Context exists yet: an invitation is found by the SHA-256 of its token, and a
``WorkspaceScope`` is built from that row (never from request input). The membership and the
audit events are then written through the workspace-scoped repositories. See ADR-0013.

The email lock is a convenience, not proof of ownership: AssetBoard does not verify email
addresses. A wrong email is counted per invitation, and the invitation is revoked after
``MAX_FAILED_EMAIL_ATTEMPTS``.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from assetboard.clock import Clock
from assetboard.db.engine import run_immediate
from assetboard.db.models.identity import Workspace
from assetboard.db.repositories import (
    AuditEventRepository,
    CurrentWorkspaceRepository,
    InvitationRepository,
    InvitationTokenReader,
    MembershipRepository,
)

TOKEN_BYTES = 32
# ``secrets.token_urlsafe(32)``: 43 characters of the URL-safe base64 alphabet.
TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{43}")
MAX_FAILED_EMAIL_ATTEMPTS = 5


def new_token() -> tuple[str, str]:
    """A fresh invitation token (256 bits) and the hash that is stored."""
    token = secrets.token_urlsafe(TOKEN_BYTES)
    return token, hash_token(token)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class InvitationError(Exception):
    pass


class InvitationInvalid(InvitationError):
    """Unknown, malformed, expired, used or revoked: all look the same to the invitee."""


class AlreadyMember(InvitationError):
    """The user already belongs to the workspace; the invitation stays unused."""


class EmailMismatch(InvitationError):
    """The invitation is locked to another email address (which is never shown)."""

    def __init__(self, *, revoked: bool) -> None:
        super().__init__("email mismatch")
        self.revoked = revoked


@dataclass(frozen=True, slots=True)
class _WorkspaceId:
    id: UUID


@dataclass(frozen=True, slots=True)
class InvitationScope:
    """A ``WorkspaceScope`` built from the invitation row found by its token hash."""

    workspace: _WorkspaceId


@dataclass(frozen=True, slots=True)
class InvitationPreview:
    workspace_name: str
    role: str
    email_locked: bool


@dataclass(frozen=True, slots=True)
class _Found:
    invitation_id: UUID
    scope: InvitationScope
    role: str
    email: str | None


def _find_pending(session: Session, token: str, now: datetime) -> _Found:
    if not TOKEN_PATTERN.fullmatch(token):
        raise InvitationInvalid
    location = InvitationTokenReader(session).locate(hash_token(token))
    if location is None:
        raise InvitationInvalid
    scope = InvitationScope(_WorkspaceId(location.workspace_id))
    invitation = InvitationRepository(session, scope).get(location.invitation_id)
    if (
        invitation is None
        or invitation.used_at is not None
        or invitation.revoked_at is not None
        or invitation.expires_at <= now
    ):
        raise InvitationInvalid
    return _Found(invitation.id, scope, invitation.role, invitation.email)


def preview_invitation(
    factory: sessionmaker[Session], token: str, *, clock: Clock
) -> InvitationPreview:
    """What the invite page shows: workspace name, role, whether an email lock applies."""
    with factory() as session:
        found = _find_pending(session, token, clock.now())
        workspace: Workspace = CurrentWorkspaceRepository(session, found.scope).get()
        return InvitationPreview(workspace.name, found.role, found.email is not None)


MismatchActor = Literal["user", "system"]


def _record_mismatch(
    session: Session, found: _Found, *, actor_user_id: UUID | None, now: datetime
) -> bool:
    revoked = InvitationRepository(session, found.scope).record_failed_email(
        found.invitation_id, now=now, limit=MAX_FAILED_EMAIL_ATTEMPTS
    )
    audit = AuditEventRepository(session, found.scope)
    actor_type: MismatchActor = "user" if actor_user_id else "system"
    audit.record(
        actor_user_id=actor_user_id,
        actor_type=actor_type,
        action="invitation.email_mismatch",
        entity_type="invitation",
        entity_id=str(found.invitation_id),
        ts=now,
    )
    if revoked:
        audit.record(
            actor_user_id=actor_user_id,
            actor_type=actor_type,
            action="invitation.revoked",
            entity_type="invitation",
            entity_id=str(found.invitation_id),
            ts=now,
            data={"reason": "failed_email_attempts"},
        )
    return revoked


def _consume(session: Session, found: _Found, *, user_id: UUID, now: datetime) -> None:
    MembershipRepository(session, found.scope).add_member(user_id=user_id, role=found.role)
    InvitationRepository(session, found.scope).mark_used(
        found.invitation_id, user_id=user_id, now=now
    )
    AuditEventRepository(session, found.scope).record(
        actor_user_id=user_id,
        action="invitation.accepted",
        entity_type="invitation",
        entity_id=str(found.invitation_id),
        ts=now,
        data={"user_id": str(user_id), "role": found.role},
    )


def _checkpoint(_name: str) -> None:
    """Test seam: called inside the accept transaction after the invitation was validated."""


def accept_invitation(
    factory: sessionmaker[Session],
    token: str,
    *,
    user_id: UUID,
    user_email: str,
    clock: Clock,
) -> UUID:
    """Join the invitation's workspace; returns its id. Single use, checked under the lock."""
    now = clock.now()

    def write(session: Session) -> UUID | EmailMismatch:
        found = _find_pending(session, token, now)
        _checkpoint("accept")
        if MembershipRepository(session, found.scope).for_user(user_id) is not None:
            raise AlreadyMember
        if found.email is not None and found.email != user_email:
            # Committed (the count must survive), then raised outside the transaction.
            return EmailMismatch(
                revoked=_record_mismatch(session, found, actor_user_id=user_id, now=now)
            )
        _consume(session, found, user_id=user_id, now=now)
        return found.scope.workspace.id

    outcome = run_immediate(factory, write)
    if isinstance(outcome, EmailMismatch):
        raise outcome
    return outcome


# Invitation sign-up (called from services.auth's registration flow)


def check_for_sign_up(
    factory: sessionmaker[Session], token: str, *, email: str, clock: Clock
) -> None:
    """Before hashing a password: the invitation is pending and its email lock matches.

    A mismatch is counted (and may revoke the invitation) before ``EmailMismatch`` is raised.
    """
    now = clock.now()
    with factory() as session:
        found = _find_pending(session, token, now)
    if found.email is None or found.email == email:
        return

    def write(session: Session) -> EmailMismatch:
        current = _find_pending(session, token, now)
        return EmailMismatch(
            revoked=_record_mismatch(session, current, actor_user_id=None, now=now)
        )

    raise run_immediate(factory, write)


def consume_for_new_user(
    session: Session, token: str, *, user_id: UUID, email: str, now: datetime
) -> UUID:
    """Inside the sign-up transaction: re-check the invitation, then join and mark it used."""
    found = _find_pending(session, token, now)
    _checkpoint("sign_up")
    if found.email is not None and found.email != email:
        raise InvitationInvalid  # the pre-check counted it; the lock changed meanwhile
    _consume(session, found, user_id=user_id, now=now)
    return found.scope.workspace.id
