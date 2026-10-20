"""F3.1 scheduled items and occurrences

Revision ID: 0014_f31
Revises: 0013_instance_settings
Create Date: 2026-10-20 22:40:30.790699
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.db.types import UtcDateTime

revision = "0014_f31"
down_revision = "0013_instance_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scheduled_items",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=False),
        sa.Column("amount_mode", sa.String(length=16), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=True),
        sa.Column("max_count", sa.Integer(), nullable=True),
        sa.Column("freq", sa.String(length=16), nullable=False),
        sa.Column("interval", sa.Integer(), nullable=False),
        sa.Column("weekdays", sa.JSON(), nullable=False),
        sa.Column("month_day", sa.String(length=24), nullable=False),
        sa.Column("month", sa.Integer(), nullable=False),
        sa.Column("weekend_policy", sa.String(length=24), nullable=False),
        sa.Column("notes", sa.String(length=200), nullable=False),
        sa.Column("paused_at", UtcDateTime(), nullable=True),
        sa.Column("archived_at", UtcDateTime(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "amount_mode IN ('fixed', 'estimate')",
            name=op.f("ck_scheduled_items_amount_mode_valid"),
        ),
        sa.CheckConstraint(
            "direction IN ('expense', 'income')", name=op.f("ck_scheduled_items_direction_valid")
        ),
        sa.CheckConstraint(
            "freq IN ('weekly', 'monthly', 'yearly')", name=op.f("ck_scheduled_items_freq_valid")
        ),
        sa.CheckConstraint(
            "weekend_policy IN ('none', 'previous_working_day', 'next_working_day')",
            name=op.f("ck_scheduled_items_weekend_policy_valid"),
        ),
        sa.CheckConstraint(
            "amount_minor > 0 AND amount_minor <= 999999999999999",
            name=op.f("ck_scheduled_items_amount_valid"),
        ),
        sa.CheckConstraint(
            "end_date IS NULL OR end_date >= start_date",
            name=op.f("ck_scheduled_items_end_date_valid"),
        ),
        sa.CheckConstraint("interval >= 1", name=op.f("ck_scheduled_items_interval_valid")),
        sa.CheckConstraint(
            "max_count IS NULL OR max_count >= 1", name=op.f("ck_scheduled_items_max_count_valid")
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "category_id"],
            ["categories.workspace_id", "categories.id"],
            name="fk_scheduled_items_workspace_category",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_scheduled_items_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scheduled_items")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_scheduled_items_workspace_id_id"),
    )
    with op.batch_alter_table("scheduled_items", schema=None) as batch_op:
        batch_op.create_index(
            "ix_scheduled_items_workspace_category", ["workspace_id", "category_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_scheduled_items_workspace_id"), ["workspace_id"], unique=False
        )

    op.create_table(
        "scheduled_occurrences",
        sa.Column("scheduled_item_id", sa.Uuid(), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("expected_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("amount_state", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("label", sa.String(length=200), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "amount_state IN ('estimated', 'confirmed')",
            name=op.f("ck_scheduled_occurrences_amount_state_valid"),
        ),
        sa.CheckConstraint(
            "direction IN ('expense', 'income')",
            name=op.f("ck_scheduled_occurrences_direction_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'posted', 'skipped')",
            name=op.f("ck_scheduled_occurrences_status_valid"),
        ),
        sa.CheckConstraint(
            "expected_minor > 0 AND expected_minor <= 999999999999999",
            name=op.f("ck_scheduled_occurrences_amount_valid"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "category_id"],
            ["categories.workspace_id", "categories.id"],
            name="fk_scheduled_occurrences_workspace_category",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "scheduled_item_id"],
            ["scheduled_items.workspace_id", "scheduled_items.id"],
            name="fk_scheduled_occurrences_workspace_item",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_scheduled_occurrences_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scheduled_occurrences")),
        sa.UniqueConstraint(
            "scheduled_item_id", "due_date", name="uq_scheduled_occurrences_item_due"
        ),
    )
    with op.batch_alter_table("scheduled_occurrences", schema=None) as batch_op:
        batch_op.create_index(
            "ix_scheduled_occurrences_workspace_category",
            ["workspace_id", "category_id"],
            unique=False,
        )
        batch_op.create_index(
            "ix_scheduled_occurrences_workspace_due", ["workspace_id", "due_date"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_scheduled_occurrences_workspace_id"), ["workspace_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("scheduled_occurrences", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_scheduled_occurrences_workspace_id"))
        batch_op.drop_index("ix_scheduled_occurrences_workspace_due")
        batch_op.drop_index("ix_scheduled_occurrences_workspace_category")

    op.drop_table("scheduled_occurrences")
    with op.batch_alter_table("scheduled_items", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_scheduled_items_workspace_id"))
        batch_op.drop_index("ix_scheduled_items_workspace_category")

    op.drop_table("scheduled_items")
