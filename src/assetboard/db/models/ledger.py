"""Ledger models (M2)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import CheckConstraint, Index, Integer, String, UniqueConstraint, text
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
