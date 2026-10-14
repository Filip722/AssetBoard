"""month states

Revision ID: 0010_f25
Revises: 0009_f21
Create Date: 2026-10-14 14:57:32.203667

``month_states`` (F2.5): one row per (workspace, month) once a month has been closed. ``month``
is the month's first day. Reopening clears ``closed_at`` and ``closed_by_user_id``; rows are
never deleted. ``closed_by_user_id`` is set to NULL when the account is deleted.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.db.types import UtcDateTime

revision = "0010_f25"
down_revision = "0009_f21"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "month_states",
        sa.Column("month", sa.Date(), nullable=False),
        sa.Column("closed_at", UtcDateTime(), nullable=True),
        sa.Column("closed_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["closed_by_user_id"],
            ["users.id"],
            name=op.f("fk_month_states_closed_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_month_states_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_month_states")),
        sa.UniqueConstraint("workspace_id", "month", name="uq_month_states_workspace_month"),
    )
    with op.batch_alter_table("month_states", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_month_states_workspace_id"), ["workspace_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("month_states", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_month_states_workspace_id"))

    op.drop_table("month_states")
