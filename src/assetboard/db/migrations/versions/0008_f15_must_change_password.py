"""temporary password flag

Revision ID: 0008_f15
Revises: 0007_f15
Create Date: 2026-10-13 14:00:00.000000

``users.must_change_password`` marks accounts an administrator created with a temporary
password (F1.5): until the password is changed, their password sessions reach only the
password page. Existing accounts get ``false``.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_f15"
down_revision = "0007_f15"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "must_change_password", sa.Boolean(), server_default=sa.false(), nullable=False
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("users", schema=None) as batch_op:
        batch_op.drop_column("must_change_password")
