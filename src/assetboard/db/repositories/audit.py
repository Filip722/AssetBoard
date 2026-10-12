from __future__ import annotations

from assetboard.db.models.identity import AuditEvent
from assetboard.db.repositories.base import ScopedRepository


class AuditEventRepository(ScopedRepository[AuditEvent]):
    """The current workspace's audit events. Append-only: no update, no delete.

    Instance-level events (``workspace_id IS NULL``) never match the scope; reading them is the
    instance administrator's audit view (F1.5).
    """

    model = AuditEvent
    unsupported = frozenset({"update", "delete"})
