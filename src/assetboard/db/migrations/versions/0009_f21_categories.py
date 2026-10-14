"""categories

Revision ID: 0009_f21
Revises: 0008_f15
Create Date: 2026-10-14 10:39:29.393145

``categories`` (F2.1). Names are unique per (workspace, kind) among non-archived rows through a
partial unique index on the Python-computed ``name_key`` (SQLite's ``lower()`` folds ASCII
only). ``UNIQUE(workspace_id, id)`` is the target of later composite foreign keys.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.db.types import UtcDateTime

revision = "0009_f21"
down_revision = "0008_f15"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "categories",
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("name_key", sa.String(length=255), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("treatment", sa.String(length=16), nullable=False),
        sa.Column("color", sa.String(length=7), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("archived_at", UtcDateTime(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint("kind IN ('expense', 'income')", name=op.f("ck_categories_kind_valid")),
        sa.CheckConstraint(
            "treatment IN ('regular', 'savings', 'transfer')",
            name=op.f("ck_categories_treatment_valid"),
        ),
        sa.CheckConstraint("length(color) = 7", name=op.f("ck_categories_color_length")),
        sa.CheckConstraint("length(name) > 0", name=op.f("ck_categories_name_not_empty")),
        sa.CheckConstraint("sort_order >= 0", name=op.f("ck_categories_sort_order_valid")),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_categories_workspace_id_workspaces")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_categories")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_categories_workspace_id_id"),
    )
    with op.batch_alter_table("categories", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_categories_workspace_id"), ["workspace_id"], unique=False
        )
        batch_op.create_index(
            "ix_categories_workspace_kind_order",
            ["workspace_id", "kind", "sort_order"],
            unique=False,
        )
        batch_op.create_index(
            "uq_categories_active_name",
            ["workspace_id", "kind", "name_key"],
            unique=True,
            sqlite_where=sa.text("archived_at IS NULL"),
            postgresql_where=sa.text("archived_at IS NULL"),
        )


def downgrade() -> None:
    with op.batch_alter_table("categories", schema=None) as batch_op:
        batch_op.drop_index(
            "uq_categories_active_name",
            sqlite_where=sa.text("archived_at IS NULL"),
            postgresql_where=sa.text("archived_at IS NULL"),
        )
        batch_op.drop_index("ix_categories_workspace_kind_order")
        batch_op.drop_index(batch_op.f("ix_categories_workspace_id"))

    op.drop_table("categories")
