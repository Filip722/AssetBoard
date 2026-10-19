"""instance settings

Revision ID: 0013_instance_settings
Revises: 0012_f23
Create Date: 2026-10-18 12:00:00.000000

``instance_settings`` (pre-release A): one row (``id = 1``) of settings an instance
administrator changes in the UI. ``allow_workspace_creation`` (default true) lets any signed-in
user create a workspace.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.db.types import UtcDateTime

revision = "0013_instance_settings"
down_revision = "0012_f23"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "instance_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "allow_workspace_creation", sa.Boolean(), server_default=sa.true(), nullable=False
        ),
        sa.Column("updated_at", UtcDateTime(), nullable=True),
        sa.Column("updated_by_user_id", sa.Uuid(), nullable=True),
        sa.CheckConstraint("id = 1", name=op.f("ck_instance_settings_singleton")),
        sa.ForeignKeyConstraint(
            ["updated_by_user_id"],
            ["users.id"],
            name=op.f("fk_instance_settings_updated_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_instance_settings")),
    )
    op.bulk_insert(
        sa.table(
            "instance_settings",
            sa.column("id", sa.Integer()),
            sa.column("allow_workspace_creation", sa.Boolean()),
        ),
        [{"id": 1, "allow_workspace_creation": True}],
    )


def downgrade() -> None:
    op.drop_table("instance_settings")
