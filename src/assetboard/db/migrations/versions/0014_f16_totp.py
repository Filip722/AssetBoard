"""F1.6 TOTP two-factor authentication

Revision ID: 0014_f16
Revises: 0013_instance_settings
Create Date: 2026-10-20 21:58:50.270033
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.db.types import UtcDateTime

revision = "0014_f16"
down_revision = "0013_instance_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "totp_factors",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("encrypted_secret", sa.String(length=256), nullable=False),
        sa.Column("enrollment_session_hash", sa.String(length=64), nullable=True),
        sa.Column("enrollment_expires_at", UtcDateTime(), nullable=True),
        sa.Column("enabled_at", UtcDateTime(), nullable=True),
        sa.Column("last_step", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_totp_factors_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_totp_factors")),
    )
    op.create_table(
        "totp_recovery_codes",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_totp_recovery_codes_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "code_hash", name=op.f("pk_totp_recovery_codes")),
    )
    with op.batch_alter_table("user_sessions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "second_factor_verified", sa.Boolean(), server_default=sa.false(), nullable=False
            )
        )

    with op.batch_alter_table("workspaces", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("require_two_factor", sa.Boolean(), server_default=sa.false(), nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table("workspaces", schema=None) as batch_op:
        batch_op.drop_column("require_two_factor")

    with op.batch_alter_table("user_sessions", schema=None) as batch_op:
        batch_op.drop_column("second_factor_verified")

    op.drop_table("totp_recovery_codes")
    op.drop_table("totp_factors")
