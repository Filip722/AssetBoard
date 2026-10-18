"""Ledger models (M2)."""

from __future__ import annotations

import datetime as dt
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Date,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from assetboard.db.base import (
    Base,
    IdMixin,
    SoftDeleteMixin,
    TimestampMixin,
    WorkspaceScopedMixin,
)
from assetboard.db.types import UtcDateTime
from assetboard.domain.categories import MAX_CATEGORY_NAME_LENGTH, MAX_NAME_KEY_LENGTH
from assetboard.domain.transactions import (
    MAX_AMOUNT_MINOR,
    MAX_DESCRIPTION_KEY_LENGTH,
    MAX_DESCRIPTION_LENGTH,
)


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

    ``plan_started_at`` (F2.3) is set by the month's first planned-value write (a saved value, a
    copy or an automatic copy) and never cleared, so a row is also created then. The automatic
    copy of the previous month's plan runs only while it is unset: a plan emptied on purpose is
    not filled again on the next visit.
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
    plan_started_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)


class Transaction(IdMixin, TimestampMixin, SoftDeleteMixin, WorkspaceScopedMixin, Base):
    """An expense or income entry (F2.2).

    Amounts are positive minor units in the workspace's base currency; ``direction`` says which
    way the money went and always equals the category's kind. ``(workspace_id, category_id)``
    references ``categories(workspace_id, id)``, so a transaction can never point at another
    workspace's category. Rows are soft-deleted (``deleted_at``) and never removed.
    ``description_key`` is the description's case-insensitive key for autocomplete
    (``domain.transactions.description_key``). The reserved columns of the spec
    (``scheduled_occurrence_id``, ``import_batch_id``, ``account_id``) arrive with the features
    that own their tables (M3, M4).
    """

    __tablename__ = "transactions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "category_id"],
            ["categories.workspace_id", "categories.id"],
            name="fk_transactions_workspace_category",
        ),
        CheckConstraint("direction IN ('expense', 'income')", name="direction_valid"),
        CheckConstraint(
            f"amount_minor > 0 AND amount_minor <= {MAX_AMOUNT_MINOR}", name="amount_valid"
        ),
        CheckConstraint("length(currency) = 3", name="currency_length"),
        CheckConstraint(
            "source IN ('manual', 'recurring', 'recurring_auto', 'import')", name="source_valid"
        ),
        CheckConstraint("date >= '1900-01-01' AND date <= '2199-12-31'", name="date_in_range"),
        Index("ix_transactions_workspace_date", "workspace_id", "date"),
        Index("ix_transactions_workspace_category_date", "workspace_id", "category_id", "date"),
        Index(
            "ix_transactions_workspace_direction_description",
            "workspace_id",
            "direction",
            "description_key",
        ),
    )

    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    date: Mapped[dt.date] = mapped_column(Date(), nullable=False)
    # Money(amount_minor, currency); no ORM composite, whose history breaks partial updates.
    amount_minor: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    category_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    description: Mapped[str] = mapped_column(String(MAX_DESCRIPTION_LENGTH), nullable=False)
    description_key: Mapped[str] = mapped_column(String(MAX_DESCRIPTION_KEY_LENGTH), nullable=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class BudgetLine(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    """A planned value: the amount planned for one category in one month (F2.3).

    ``month`` is the month's first day (the repository takes a ``YearMonth``). The amount is
    positive minor units in the workspace's base currency; a zero plan has no row (lines are
    removed, not soft-deleted). ``(workspace_id, category_id)`` references
    ``categories(workspace_id, id)``, so a line never points at another workspace's category.
    """

    __tablename__ = "budget_lines"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "month", "category_id", name="uq_budget_lines_workspace_month_category"
        ),
        ForeignKeyConstraint(
            ["workspace_id", "category_id"],
            ["categories.workspace_id", "categories.id"],
            name="fk_budget_lines_workspace_category",
        ),
        CheckConstraint(
            f"planned_minor > 0 AND planned_minor <= {MAX_AMOUNT_MINOR}", name="planned_valid"
        ),
        CheckConstraint("length(currency) = 3", name="currency_length"),
        CheckConstraint("month >= '1900-01-01' AND month <= '2199-12-01'", name="month_in_range"),
        Index("ix_budget_lines_workspace_category", "workspace_id", "category_id"),
    )

    month: Mapped[date] = mapped_column(Date(), nullable=False)
    category_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    # Money(planned_minor, currency); no ORM composite, as for transactions.
    planned_minor: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
