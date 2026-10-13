from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.orm import Session

from assetboard.db.models.identity import AuditEvent
from assetboard.db.repositories.base import ScopedRepository


@dataclass(frozen=True, slots=True)
class AuditCursor:
    """Keyset position: the last row of the previous page (rows are newest first)."""

    ts: datetime
    id: UUID


@dataclass(frozen=True, slots=True)
class AuditFilter:
    actions: Collection[str] | None = None  # None: every action
    since: datetime | None = None  # inclusive
    until: datetime | None = None  # exclusive
    before: AuditCursor | None = None


def _filtered(statement: Select[AuditEvent], where: AuditFilter, limit: int) -> Select[AuditEvent]:
    if where.actions is not None:
        statement = statement.where(AuditEvent.action.in_(list(where.actions)))
    if where.since is not None:
        statement = statement.where(AuditEvent.ts >= where.since)
    if where.until is not None:
        statement = statement.where(AuditEvent.ts < where.until)
    if where.before is not None:
        statement = statement.where(
            or_(
                AuditEvent.ts < where.before.ts,
                and_(AuditEvent.ts == where.before.ts, AuditEvent.id < where.before.id),
            )
        )
    return statement.order_by(AuditEvent.ts.desc(), AuditEvent.id.desc()).limit(limit)


class AuditEventRepository(ScopedRepository[AuditEvent]):
    """The current workspace's audit events. Append-only: no update, no delete.

    Instance-level events (``workspace_id IS NULL``) never match the scope; reading them is the
    instance administrator's audit view (F1.5).
    """

    model = AuditEvent
    unsupported = frozenset({"update", "delete"})

    def record(
        self,
        *,
        actor_user_id: UUID | None,
        action: str,
        entity_type: str,
        entity_id: str | None,
        ts: datetime,
        data: Mapping[str, str | int | bool] | None = None,
        actor_type: Literal["user", "system"] = "user",
    ) -> AuditEvent:
        """Append an action to this workspace's audit log."""
        return self.add(
            AuditEvent(
                id=uuid4(),
                actor_user_id=actor_user_id,
                actor_type=actor_type,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                data=dict(data or {}),
                ts=ts,
            )
        )

    def page(self, where: AuditFilter, *, limit: int) -> list[AuditEvent]:
        """This workspace's events, newest first (keyset on ``ts``, ``id``)."""
        return list(self._session.scalars(_filtered(self._scoped_select(), where, limit)))


class InstanceAuditReader:
    """Instance-level audit events (``workspace_id IS NULL``): auth, admin and sign-in failures.

    Not a ``ScopedRepository``: these rows belong to no workspace. The ``workspace_id IS NULL``
    predicate is part of every query, so no workspace's events can be read through it. Callers
    check that the reader is an instance administrator (``services.audit_view``).
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def page(
        self, where: AuditFilter, *, limit: int, entity_id: str | None = None
    ) -> list[AuditEvent]:
        statement = select(AuditEvent).where(AuditEvent.workspace_id.is_(None))
        if entity_id is not None:
            statement = statement.where(AuditEvent.entity_id == entity_id)
        return list(self._session.scalars(_filtered(statement, where, limit)))
