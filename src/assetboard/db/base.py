from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import ForeignKey, MetaData, Uuid, event
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from assetboard.clock import Clock
from assetboard.db.types import UtcDateTime

CONSTRAINT_NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=CONSTRAINT_NAMING_CONVENTION)


class IdMixin:
    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)


class SoftDeleteMixin:
    deleted_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


class WorkspaceScopedMixin:
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspaces.id"),
        nullable=False,
        index=True,
    )


@event.listens_for(Session, "before_flush")
def set_timestamps_from_clock(
    session: Session,
    _flush_context: object,
    _instances: object,
) -> None:
    new_objects = [obj for obj in session.new if isinstance(obj, TimestampMixin)]
    dirty_objects = [
        obj
        for obj in session.dirty
        if isinstance(obj, TimestampMixin) and session.is_modified(obj, include_collections=False)
    ]
    if not new_objects and not dirty_objects:
        return

    clock = session.info.get("clock")
    if not isinstance(clock, Clock):
        raise RuntimeError("A Clock must be injected in Session.info['clock'] to write timestamps")
    now = clock.now().astimezone(UTC)

    for obj in new_objects:
        if obj.created_at is None:
            obj.created_at = now
        if obj.updated_at is None:
            obj.updated_at = now
    for obj in dirty_objects:
        obj.updated_at = now
