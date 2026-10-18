"""The current workspace's own row.

``Workspace`` is the tenant itself and has no ``workspace_id``, so it is not a
``ScopedRepository``. This repository can only ever read or change ``ctx.workspace.id``, and
only the fields in ``UPDATABLE_FIELDS``.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from assetboard.db.models.identity import Workspace
from assetboard.db.repositories.base import NotFound, WorkspaceScope

UPDATABLE_FIELDS = frozenset(
    {"name", "base_currency", "country_code", "timezone", "locale", "auto_copy_plan"}
)


class CurrentWorkspaceRepository:
    def __init__(self, session: Session, ctx: WorkspaceScope) -> None:
        self._session = session
        self._workspace_id: UUID = ctx.workspace.id

    def get(self) -> Workspace:
        statement = select(Workspace).where(Workspace.id == self._workspace_id)
        workspace = self._session.scalars(statement).one_or_none()
        if workspace is None:
            raise NotFound
        return workspace

    def lock(self, *, exclusive: bool) -> None:
        """Lock the current workspace's row until the transaction ends.

        ``FOR SHARE`` (``exclusive=False``) or ``FOR UPDATE`` on PostgreSQL. SQLite has no row
        locks; there every write already runs under ``BEGIN IMMEDIATE``, which serialises
        writers, and SQLAlchemy leaves the clause out. Used by ``services.months``: writes to a
        month take the shared lock before checking that it is open, closing and reopening take
        the exclusive one, so a close can never interleave with a write (docs/architecture.md).
        """
        statement = select(Workspace.id).where(Workspace.id == self._workspace_id)
        statement = statement.with_for_update(read=not exclusive)
        if self._session.execute(statement).one_or_none() is None:
            raise NotFound

    def update(self, **values: object) -> Workspace:
        """Change allowed fields of the current workspace; returns it."""
        invalid = set(values) - UPDATABLE_FIELDS
        if invalid:
            raise ValueError(f"cannot update {', '.join(sorted(invalid))}")
        workspace = self.get()
        for name, value in values.items():
            setattr(workspace, name, value)
        self._session.flush()
        return workspace
