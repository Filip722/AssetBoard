"""selected workspace per session

Revision ID: 0003_f12
Revises: 0002_f11
Create Date: 2026-10-12 10:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_f12"
down_revision = "0002_f11"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("user_sessions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("current_workspace_id", sa.Uuid(), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_user_sessions_current_workspace_id"),
            ["current_workspace_id"],
            unique=False,
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_user_sessions_current_workspace_id_workspaces"),
            "workspaces",
            ["current_workspace_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("user_sessions", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_user_sessions_current_workspace_id_workspaces"), type_="foreignkey"
        )
        batch_op.drop_index(batch_op.f("ix_user_sessions_current_workspace_id"))
        batch_op.drop_column("current_workspace_id")
