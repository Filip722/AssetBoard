from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, validates

from assetboard.db.base import (
    AppendOnly,
    Base,
    IdMixin,
    TimestampMixin,
    WorkspaceOwned,
    WorkspaceScopedMixin,
)
from assetboard.db.types import UtcDateTime
from assetboard.domain.identity import normalize_email


class User(IdMixin, TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("email", name="uq_users_email_normalized"),
        CheckConstraint("email = lower(email)", name="email_lowercase"),
    )

    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String(512), nullable=True)
    locale: Mapped[str] = mapped_column(String(8), nullable=False, default="en")
    theme: Mapped[str] = mapped_column(String(16), nullable=False, default="system")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_instance_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_login_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)

    @validates("email")
    def _normalize_email(self, _key: str, value: str) -> str:
        return normalize_email(value)


class Workspace(IdMixin, TimestampMixin, Base):
    __tablename__ = "workspaces"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    base_currency: Mapped[str] = mapped_column(String(3), nullable=False, default="EUR")
    locale: Mapped[str] = mapped_column(String(8), nullable=False, default="en")
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True)


class Membership(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    __tablename__ = "memberships"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", name="uq_memberships_workspace_user"),
        CheckConstraint("role IN ('owner', 'editor', 'viewer')", name="role_valid"),
        Index("ix_memberships_user_workspace", "user_id", "workspace_id"),
    )

    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)


class Invitation(IdMixin, TimestampMixin, WorkspaceScopedMixin, Base):
    """An invitation link. Only the SHA-256 of its token is stored; it can be used once."""

    __tablename__ = "invitations"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_invitations_token_hash"),
        CheckConstraint("length(token_hash) = 64", name="token_hash_sha256_length"),
        CheckConstraint("role IN ('owner', 'editor', 'viewer')", name="role_valid"),
        CheckConstraint("email IS NULL OR email = lower(email)", name="email_lowercase"),
        CheckConstraint("role != 'owner' OR email IS NOT NULL", name="owner_requires_email"),
        CheckConstraint("failed_email_attempts >= 0", name="failed_email_attempts_valid"),
        Index("ix_invitations_workspace_created", "workspace_id", "created_at"),
    )

    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    used_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    failed_email_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class UserSession(Base):
    __tablename__ = "user_sessions"
    __table_args__ = (
        Index("ix_user_sessions_user_expires", "user_id", "expires_at"),
        CheckConstraint("length(id_hash) = 64", name="id_hash_sha256_length"),
    )

    id_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    user_agent: Mapped[str | None] = mapped_column(String(200), nullable=True)
    ip_prefix: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # The workspace chosen with the switcher; NULL means "the oldest membership" (F1.2).
    current_workspace_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspaces.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )


class AuditEvent(AppendOnly, WorkspaceOwned, Base):
    """Append-only (enforced at flush).

    ``workspace_id`` is NULL for instance-level (auth and admin) events.
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_workspace_ts", "workspace_id", "ts"),
        Index("ix_audit_events_actor_ts", "actor_user_id", "ts"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    workspace_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("workspaces.id"),
        nullable=True,
        index=True,
        active_history=True,
    )
    actor_user_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    actor_type: Mapped[str] = mapped_column(String(16), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    ts: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    ip_prefix: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(200), nullable=True)


class AuthThrottle(Base):
    __tablename__ = "auth_throttles"
    __table_args__ = (
        UniqueConstraint("key_type", "key_hash", name="uq_auth_throttles_type_hash"),
        Index("ix_auth_throttles_updated_at", "updated_at"),
        CheckConstraint("length(key_hash) = 64", name="key_hash_sha256_length"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    key_type: Mapped[str] = mapped_column(String(16), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    window_started_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)
    blocked_until: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UtcDateTime(), nullable=False)


class SetupState(Base):
    """Singleton row serializing first-run setup across concurrent requests."""

    __tablename__ = "setup_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    completed_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
