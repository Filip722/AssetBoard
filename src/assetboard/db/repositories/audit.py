from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from assetboard.db.models.identity import AuditEvent
from assetboard.db.repositories.base import ScopedRepository


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
