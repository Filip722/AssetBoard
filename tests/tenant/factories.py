"""One row factory per workspace-owned model, for the tenant-isolation test.

A factory builds an unsaved row for the requested workspace (creating any non-workspace rows it
needs, such as users). ``update_values`` is a valid change for the cross-tenant update check.
Adding a workspace-owned model without an entry here fails
``test_discovery_finds_every_workspace_owned_model_with_repository_and_factory``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from assetboard.db.models.identity import AuditEvent, Invitation, Membership, User
from assetboard.services.invitations import new_token


@dataclass(frozen=True)
class TenantCase:
    factory: Callable[[Session, UUID], Any]
    update_values: Mapping[str, object] = field(default_factory=dict[str, object])
    # Operations the repository must refuse; asserted against the repository's own declaration.
    unsupported: frozenset[str] = frozenset()


def _user(session: Session) -> User:
    user = User(email=f"member-{uuid4().hex[:12]}@example.com", display_name="Member")
    session.add(user)
    session.flush()
    return user


def make_membership(session: Session, workspace_id: UUID) -> Membership:
    return Membership(workspace_id=workspace_id, user_id=_user(session).id, role="viewer")


def make_audit_event(session: Session, workspace_id: UUID) -> AuditEvent:
    return AuditEvent(
        id=uuid4(),
        workspace_id=workspace_id,
        actor_type="user",
        action="test.created",
        entity_type="test",
        entity_id=uuid4().hex,
        data={"synthetic": True},
        ts=session.info["clock"].now(),
    )


def make_invitation(session: Session, workspace_id: UUID) -> Invitation:
    _token, token_hash = new_token()
    now = session.info["clock"].now()
    return Invitation(
        workspace_id=workspace_id,
        token_hash=token_hash,
        role="viewer",
        email=None,
        expires_at=now + timedelta(days=7),
        failed_email_attempts=0,
        created_by_user_id=_user(session).id,
    )


TENANT_CASES: dict[type[Any], TenantCase] = {
    Membership: TenantCase(make_membership, update_values={"role": "editor"}),
    AuditEvent: TenantCase(make_audit_event, unsupported=frozenset({"update", "delete"})),
    # Invitations change only through named transitions (revoke, mark_used, ...).
    Invitation: TenantCase(make_invitation, unsupported=frozenset({"update", "delete"})),
}
