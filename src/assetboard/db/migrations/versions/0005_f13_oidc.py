"""oidc identities and session auth method

Revision ID: 0005_f13
Revises: 0004_f12
Create Date: 2026-10-12 18:00:00.000000

``users.password_hash`` is already nullable (0002_f11), so OIDC-only users need no change there.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.db.types import UtcDateTime

revision = "0005_f13"
down_revision = "0004_f12"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "external_identities",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=512), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("email_at_link", sa.String(length=320), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_external_identities_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_external_identities")),
        sa.UniqueConstraint("provider", "subject", name="uq_external_identities_provider_subject"),
    )
    with op.batch_alter_table("external_identities", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_external_identities_user_id"), ["user_id"], unique=False
        )

    with op.batch_alter_table("user_sessions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "auth_method", sa.String(length=16), server_default="password", nullable=False
            )
        )
        batch_op.create_check_constraint(
            op.f("ck_user_sessions_auth_method_valid"), "auth_method IN ('password', 'oidc')"
        )


def downgrade() -> None:
    with op.batch_alter_table("user_sessions", schema=None) as batch_op:
        batch_op.drop_constraint(op.f("ck_user_sessions_auth_method_valid"), type_="check")
        batch_op.drop_column("auth_method")

    with op.batch_alter_table("external_identities", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_external_identities_user_id"))

    op.drop_table("external_identities")
