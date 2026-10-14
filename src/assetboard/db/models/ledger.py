"""Ledger models (M2)."""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from assetboard.db.base import Base, IdMixin, TimestampMixin, WorkspaceScopedMixin
from assetboard.db.types import UtcDateTime
from assetboard.domain.categories import MAX_CATEGORY_NAME_LENGTH, MAX_NAME_KEY_LENGTH


class Category(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    """A transaction category (F2.1).

    ``name_key`` is the name's case-insensitive identity (``domain.categories.name_key``): names
    are unique per (workspace, kind) among non-archived categories. SQLite's ``lower()`` folds
    ASCII only, so the key is computed in Python and indexed as a plain column.
    ``UNIQUE(workspace_id, id)`` is the target of composite foreign keys from transactions and
    budget lines (docs/architecture.md).
    """

    __tablename__ = "categories"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_categories_workspace_id_id"),
        CheckConstraint("kind IN ('expense', 'income')", name="kind_valid"),
        CheckConstraint("treatment IN ('regular', 'savings', 'transfer')", name="treatment_valid"),
        CheckConstraint("length(color) = 7", name="color_length"),
        CheckConstraint("sort_order >= 0", name="sort_order_valid"),
        CheckConstraint("length(name) > 0", name="name_not_empty"),
        Index(
            "uq_categories_active_name",
            "workspace_id",
            "kind",
            "name_key",
            unique=True,
            sqlite_where=text("archived_at IS NULL"),
            postgresql_where=text("archived_at IS NULL"),
        ),
        Index("ix_categories_workspace_kind_order", "workspace_id", "kind", "sort_order"),
    )

    name: Mapped[str] = mapped_column(String(MAX_CATEGORY_NAME_LENGTH), nullable=False)
    name_key: Mapped[str] = mapped_column(String(MAX_NAME_KEY_LENGTH), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    treatment: Mapped[str] = mapped_column(String(16), nullable=False)
    color: Mapped[str] = mapped_column(String(7), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)
    archived_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


class MonthState(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    """Per-month state of a workspace (F2.5): whether the month is closed.

    ``month`` is the month's first day (as for budget lines). A row is created the first time a
    month is closed; reopening clears ``closed_at`` and ``closed_by_user_id`` and keeps the row.
    A month without a row is open. ``closed_by_user_id`` becomes NULL if the account is deleted,
    while the month stays closed.
    """

    __tablename__ = "month_states"
    __table_args__ = (
        UniqueConstraint("workspace_id", "month", name="uq_month_states_workspace_month"),
    )

    month: Mapped[date] = mapped_column(Date(), nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    closed_by_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
