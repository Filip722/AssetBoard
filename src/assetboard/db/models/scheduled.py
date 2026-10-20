from __future__ import annotations

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from assetboard.db.base import Base, IdMixin, TimestampMixin, WorkspaceScopedMixin
from assetboard.db.types import UtcDateTime
from assetboard.domain.transactions import MAX_AMOUNT_MINOR


class ScheduledItem(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    __tablename__ = "scheduled_items"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_scheduled_items_workspace_id_id"),
        ForeignKeyConstraint(
            ["workspace_id", "category_id"],
            ["categories.workspace_id", "categories.id"],
            name="fk_scheduled_items_workspace_category",
        ),
        CheckConstraint("direction IN ('expense', 'income')", name="direction_valid"),
        CheckConstraint("amount_mode IN ('fixed', 'estimate')", name="amount_mode_valid"),
        CheckConstraint(
            f"amount_minor > 0 AND amount_minor <= {MAX_AMOUNT_MINOR}", name="amount_valid"
        ),
        CheckConstraint("freq IN ('weekly', 'monthly', 'yearly')", name="freq_valid"),
        CheckConstraint("interval >= 1", name="interval_valid"),
        CheckConstraint("max_count IS NULL OR max_count >= 1", name="max_count_valid"),
        CheckConstraint("end_date IS NULL OR end_date >= start_date", name="end_date_valid"),
        CheckConstraint(
            "weekend_policy IN ('none', 'previous_working_day', 'next_working_day')",
            name="weekend_policy_valid",
        ),
        Index("ix_scheduled_items_workspace_category", "workspace_id", "category_id"),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    category_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    amount_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    amount_minor: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    start_date: Mapped[date] = mapped_column(Date(), nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date(), nullable=True)
    max_count: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    freq: Mapped[str] = mapped_column(String(16), nullable=False)
    interval: Mapped[int] = mapped_column(Integer(), nullable=False)
    weekdays: Mapped[list[int]] = mapped_column(JSON(), nullable=False)
    month_day: Mapped[str] = mapped_column(String(24), nullable=False)
    month: Mapped[int] = mapped_column(Integer(), nullable=False)
    weekend_policy: Mapped[str] = mapped_column(String(24), nullable=False)
    notes: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    paused_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


class ScheduledOccurrence(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    __tablename__ = "scheduled_occurrences"
    __table_args__ = (
        UniqueConstraint("scheduled_item_id", "due_date", name="uq_scheduled_occurrences_item_due"),
        ForeignKeyConstraint(
            ["workspace_id", "scheduled_item_id"],
            ["scheduled_items.workspace_id", "scheduled_items.id"],
            name="fk_scheduled_occurrences_workspace_item",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "category_id"],
            ["categories.workspace_id", "categories.id"],
            name="fk_scheduled_occurrences_workspace_category",
        ),
        CheckConstraint("direction IN ('expense', 'income')", name="direction_valid"),
        CheckConstraint("amount_state IN ('estimated', 'confirmed')", name="amount_state_valid"),
        CheckConstraint("status IN ('pending', 'posted', 'skipped')", name="status_valid"),
        CheckConstraint(
            f"expected_minor > 0 AND expected_minor <= {MAX_AMOUNT_MINOR}", name="amount_valid"
        ),
        Index("ix_scheduled_occurrences_workspace_due", "workspace_id", "due_date"),
        Index("ix_scheduled_occurrences_workspace_category", "workspace_id", "category_id"),
    )

    scheduled_item_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    category_id: Mapped[UUID] = mapped_column(Uuid(), nullable=False)
    due_date: Mapped[date] = mapped_column(Date(), nullable=False)
    expected_minor: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    amount_state: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    label: Mapped[str] = mapped_column(String(200), nullable=False)
