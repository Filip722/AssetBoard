"""A migrated file database with synthetic workspaces and members, for service-level tests."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from assetboard.clock import FixedClock
from assetboard.config import Settings
from assetboard.db.engine import create_database_engine
from assetboard.db.migrate import upgrade_database
from assetboard.db.models.identity import Membership, User, Workspace
from assetboard.services.context import Context, Role, UserRef, WorkspaceRef


@dataclass
class WorkspaceDb:
    engine: Engine
    factory: sessionmaker[Session]
    clock: FixedClock
    workspace_id: UUID
    users: dict[str, UUID] = field(default_factory=dict[str, UUID])
    memberships: dict[str, UUID] = field(default_factory=dict[str, UUID])

    def add_workspace(self, name: str) -> UUID:
        with self.factory.begin() as session:
            workspace = Workspace(name=name, base_currency="EUR", locale="en", timezone="UTC")
            session.add(workspace)
            session.flush()
            return workspace.id

    def add_member(self, key: str, role: str, workspace_id: UUID | None = None) -> UUID:
        with self.factory.begin() as session:
            user_id = self.users.get(key)
            if user_id is None:
                user = User(email=f"{key}@example.com", display_name=key.title())
                session.add(user)
                session.flush()
                user_id = self.users[key] = user.id
            membership = Membership(
                workspace_id=workspace_id or self.workspace_id, user_id=user_id, role=role
            )
            session.add(membership)
            session.flush()
            if workspace_id is None:
                self.memberships[key] = membership.id
            return membership.id

    def ctx(self, key: str, role: Role | str, workspace_id: UUID | None = None) -> Context:
        """A Context as a request would have built it (the role may be stale on purpose)."""
        return Context(
            user=UserRef(
                id=self.users[key],
                email=f"{key}@example.com",
                display_name=key.title(),
                locale="en",
                is_instance_admin=False,
            ),
            workspace=WorkspaceRef(
                id=workspace_id or self.workspace_id,
                name="Home",
                base_currency="EUR",
                locale="en",
                timezone="UTC",
                country_code=None,
            ),
            role=Role(role),
            clock=self.clock,
        )

    def roles(self, workspace_id: UUID | None = None) -> dict[str, str]:
        with self.factory() as session:
            rows = session.execute(
                select(User.email, Membership.role)
                .join(User, User.id == Membership.user_id)
                .where(Membership.workspace_id == (workspace_id or self.workspace_id))
            )
            return {email.split("@")[0]: role for email, role in rows}


def workspace_db(tmp_path: Path) -> Iterator[WorkspaceDb]:
    database_url = f"sqlite:///{tmp_path / 'workspaces.db'}"
    upgrade_database(Settings(_env_file=None, data_dir=tmp_path, database_url=database_url))
    engine = create_database_engine(database_url)
    clock = FixedClock("2026-03-01T12:00:00+00:00")
    factory = sessionmaker(engine, class_=Session, info={"clock": clock}, expire_on_commit=False)
    with factory.begin() as session:
        workspace = Workspace(name="Home", base_currency="EUR", locale="en", timezone="UTC")
        session.add(workspace)
        session.flush()
        workspace_id = workspace.id
    try:
        yield WorkspaceDb(engine, factory, clock, workspace_id)
    finally:
        engine.dispose()
