"""invitations

Revision ID: 0004_f12
Revises: 0003_f12
Create Date: 2026-10-12 12:07:23.833725
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.db.types import UtcDateTime

revision = "0004_f12"
down_revision = "0003_f12"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "invitations",
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("expires_at", UtcDateTime(), nullable=False),
        sa.Column("used_at", UtcDateTime(), nullable=True),
        sa.Column("used_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("revoked_at", UtcDateTime(), nullable=True),
        sa.Column("failed_email_attempts", sa.Integer(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "role != 'owner' OR email IS NOT NULL", name=op.f("ck_invitations_owner_requires_email")
        ),
        sa.CheckConstraint(
            "role IN ('owner', 'editor', 'viewer')", name=op.f("ck_invitations_role_valid")
        ),
        sa.CheckConstraint(
            "email IS NULL OR email = lower(email)", name=op.f("ck_invitations_email_lowercase")
        ),
        sa.CheckConstraint(
            "failed_email_attempts >= 0", name=op.f("ck_invitations_failed_email_attempts_valid")
        ),
        sa.CheckConstraint(
            "length(token_hash) = 64", name=op.f("ck_invitations_token_hash_sha256_length")
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_invitations_created_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["used_by_user_id"],
            ["users.id"],
            name=op.f("fk_invitations_used_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_invitations_workspace_id_workspaces")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invitations")),
        sa.UniqueConstraint("token_hash", name="uq_invitations_token_hash"),
    )
    with op.batch_alter_table("invitations", schema=None) as batch_op:
        batch_op.create_index(
            "ix_invitations_workspace_created", ["workspace_id", "created_at"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_invitations_workspace_id"), ["workspace_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("invitations", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_invitations_workspace_id"))
        batch_op.drop_index("ix_invitations_workspace_created")

    op.drop_table("invitations")
