"""transactions

Revision ID: 0011_f22
Revises: 0010_f25
Create Date: 2026-10-14 18:00:00.000000

``transactions`` (F2.2): expense and income entries in positive minor units. The composite
foreign key ``(workspace_id, category_id) -> categories(workspace_id, id)`` keeps every
transaction in its category's workspace. Rows are soft-deleted (``deleted_at``). The spec's
reserved columns (``scheduled_occurrence_id``, ``import_batch_id``, ``account_id``) are added by
the milestones that create their tables (M3, M4).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.db.types import UtcDateTime

revision = "0011_f22"
down_revision = "0010_f25"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "transactions",
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("date", sa.Date(), nullable=False),
        sa.Column("amount_minor", sa.BigInteger(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("category_id", sa.Uuid(), nullable=False),
        sa.Column("description", sa.String(length=200), nullable=False),
        sa.Column("description_key", sa.String(length=255), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("deleted_at", UtcDateTime(), nullable=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "direction IN ('expense', 'income')", name=op.f("ck_transactions_direction_valid")
        ),
        sa.CheckConstraint(
            "amount_minor > 0 AND amount_minor <= 999999999999999",
            name=op.f("ck_transactions_amount_valid"),
        ),
        sa.CheckConstraint("length(currency) = 3", name=op.f("ck_transactions_currency_length")),
        sa.CheckConstraint(
            "source IN ('manual', 'recurring', 'recurring_auto', 'import')",
            name=op.f("ck_transactions_source_valid"),
        ),
        sa.CheckConstraint(
            "date >= '1900-01-01' AND date <= '2199-12-31'",
            name=op.f("ck_transactions_date_in_range"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_transactions_created_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "category_id"],
            ["categories.workspace_id", "categories.id"],
            name="fk_transactions_workspace_category",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_transactions_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_transactions")),
    )
    with op.batch_alter_table("transactions", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_transactions_workspace_id"), ["workspace_id"], unique=False
        )
        batch_op.create_index(
            "ix_transactions_workspace_date", ["workspace_id", "date"], unique=False
        )
        batch_op.create_index(
            "ix_transactions_workspace_category_date",
            ["workspace_id", "category_id", "date"],
            unique=False,
        )
        batch_op.create_index(
            "ix_transactions_workspace_direction_description",
            ["workspace_id", "direction", "description_key"],
            unique=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("transactions", schema=None) as batch_op:
        batch_op.drop_index("ix_transactions_workspace_direction_description")
        batch_op.drop_index("ix_transactions_workspace_category_date")
        batch_op.drop_index("ix_transactions_workspace_date")
        batch_op.drop_index(batch_op.f("ix_transactions_workspace_id"))

    op.drop_table("transactions")
