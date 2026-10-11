"""identity and local authentication

Revision ID: 0002_f11
Revises: 0001_f03
Create Date: 2026-10-10 22:30:04.105286
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from assetboard.db.types import UtcDateTime

revision = "0002_f11"
down_revision = "0001_f03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "auth_throttles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key_type", sa.String(length=16), nullable=False),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("window_started_at", UtcDateTime(), nullable=False),
        sa.Column("blocked_until", UtcDateTime(), nullable=True),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.CheckConstraint(
            "length(key_hash) = 64", name=op.f("ck_auth_throttles_key_hash_sha256_length")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_throttles")),
        sa.UniqueConstraint("key_type", "key_hash", name="uq_auth_throttles_type_hash"),
    )
    with op.batch_alter_table("auth_throttles", schema=None) as batch_op:
        batch_op.create_index("ix_auth_throttles_updated_at", ["updated_at"], unique=False)

    op.create_table(
        "setup_state",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("completed_at", UtcDateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_setup_state")),
    )
    op.bulk_insert(
        sa.table(
            "setup_state", sa.column("id", sa.Integer()), sa.column("completed_at", UtcDateTime())
        ),
        [{"id": 1, "completed_at": None}],
    )
    op.create_table(
        "users",
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("password_hash", sa.String(length=512), nullable=True),
        sa.Column("locale", sa.String(length=8), nullable=False),
        sa.Column("theme", sa.String(length=16), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("is_instance_admin", sa.Boolean(), nullable=False),
        sa.Column("last_login_at", UtcDateTime(), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.CheckConstraint("email = lower(email)", name=op.f("ck_users_email_lowercase")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name="uq_users_email_normalized"),
    )
    op.create_table(
        "workspaces",
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("base_currency", sa.String(length=3), nullable=False),
        sa.Column("locale", sa.String(length=8), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("country_code", sa.String(length=2), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspaces")),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=True),
        sa.Column("actor_user_id", sa.Uuid(), nullable=True),
        sa.Column("actor_type", sa.String(length=16), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.String(length=64), nullable=True),
        sa.Column("data", sa.JSON(), nullable=False),
        sa.Column("ts", UtcDateTime(), nullable=False),
        sa.Column("ip_prefix", sa.String(length=64), nullable=True),
        sa.Column("user_agent", sa.String(length=200), nullable=True),
        sa.ForeignKeyConstraint(
            ["actor_user_id"],
            ["users.id"],
            name=op.f("fk_audit_events_actor_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_audit_events_workspace_id_workspaces"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_events")),
    )
    with op.batch_alter_table("audit_events", schema=None) as batch_op:
        batch_op.create_index("ix_audit_events_actor_ts", ["actor_user_id", "ts"], unique=False)
        batch_op.create_index(
            batch_op.f("ix_audit_events_actor_user_id"), ["actor_user_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_audit_events_workspace_id"), ["workspace_id"], unique=False
        )
        batch_op.create_index("ix_audit_events_workspace_ts", ["workspace_id", "ts"], unique=False)

    op.create_table(
        "memberships",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("updated_at", UtcDateTime(), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "role IN ('owner', 'editor', 'viewer')", name=op.f("ck_memberships_role_valid")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_memberships_user_id_users")
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name=op.f("fk_memberships_workspace_id_workspaces")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_memberships")),
        sa.UniqueConstraint("workspace_id", "user_id", name="uq_memberships_workspace_user"),
    )
    with op.batch_alter_table("memberships", schema=None) as batch_op:
        batch_op.create_index(
            "ix_memberships_user_workspace", ["user_id", "workspace_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_memberships_workspace_id"), ["workspace_id"], unique=False
        )

    op.create_table(
        "user_sessions",
        sa.Column("id_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", UtcDateTime(), nullable=False),
        sa.Column("last_seen_at", UtcDateTime(), nullable=False),
        sa.Column("expires_at", UtcDateTime(), nullable=False),
        sa.Column("user_agent", sa.String(length=200), nullable=True),
        sa.Column("ip_prefix", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "length(id_hash) = 64", name=op.f("ck_user_sessions_id_hash_sha256_length")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_sessions_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id_hash", name=op.f("pk_user_sessions")),
    )
    with op.batch_alter_table("user_sessions", schema=None) as batch_op:
        batch_op.create_index(
            "ix_user_sessions_user_expires", ["user_id", "expires_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("user_sessions", schema=None) as batch_op:
        batch_op.drop_index("ix_user_sessions_user_expires")

    op.drop_table("user_sessions")
    with op.batch_alter_table("memberships", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_memberships_workspace_id"))
        batch_op.drop_index("ix_memberships_user_workspace")

    op.drop_table("memberships")
    with op.batch_alter_table("audit_events", schema=None) as batch_op:
        batch_op.drop_index("ix_audit_events_workspace_ts")
        batch_op.drop_index(batch_op.f("ix_audit_events_workspace_id"))
        batch_op.drop_index(batch_op.f("ix_audit_events_actor_user_id"))
        batch_op.drop_index("ix_audit_events_actor_ts")

    op.drop_table("audit_events")
    op.drop_table("workspaces")
    op.drop_table("users")
    op.drop_table("setup_state")
    with op.batch_alter_table("auth_throttles", schema=None) as batch_op:
        batch_op.drop_index("ix_auth_throttles_updated_at")

    op.drop_table("auth_throttles")
