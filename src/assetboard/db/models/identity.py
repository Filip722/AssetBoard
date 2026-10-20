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
    false,
    true,
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
    # The language the user chose in the profile; NULL follows the browser (F1.5).
    locale: Mapped[str | None] = mapped_column(String(8), nullable=True, default=None)
    theme: Mapped[str] = mapped_column(String(16), nullable=False, default="system")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_instance_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Set for an account created by an administrator with a temporary password (F1.5): its
    # password sessions can only change the password until it does.
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    last_login_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)

    @validates("email")
    def _normalize_email(self, _key: str, value: str) -> str:
        return normalize_email(value)


class ExternalIdentity(IdMixin, TimestampMixin, Base):
    """An identity at an OIDC provider, linked to a user (F1.3, ADR-0014).

    ``provider`` is the issuer URL, so subjects of different issuers never collide.
    ``email_at_link`` records the provider's email when the identity was linked; the user's own
    email does not follow later changes at the provider.
    """

    __tablename__ = "external_identities"
    __table_args__ = (
        UniqueConstraint("provider", "subject", name="uq_external_identities_provider_subject"),
    )

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(512), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    email_at_link: Mapped[str | None] = mapped_column(String(320), nullable=True)


class Workspace(IdMixin, TimestampMixin, Base):
    __tablename__ = "workspaces"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    require_two_factor: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=False, server_default=false()
    )
    base_currency: Mapped[str] = mapped_column(String(3), nullable=False, default="EUR")
    locale: Mapped[str] = mapped_column(String(8), nullable=False, default="en")
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    # F2.3: copy the previous month's plan when a month is first opened (off by default).
    auto_copy_plan: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=False, server_default=false()
    )


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
    # The email lock. Required for every new invitation (pre-release A, checked in
    # services.workspaces); NULL only on invitations created before, which stay usable by any
    # account until they expire.
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
        CheckConstraint("auth_method IN ('password', 'oidc', 'proxy')", name="auth_method_valid"),
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
    # How the session was started ("password", "oidc" or "proxy", F1.4); RP-initiated logout
    # applies to "oidc" sessions only.
    auth_method: Mapped[str] = mapped_column(
        String(16), nullable=False, default="password", server_default="password"
    )
    second_factor_verified: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=False, server_default=false()
    )
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


class InstanceSettings(Base):
    """Instance-wide settings an administrator changes in the UI (pre-release A).

    A single row (``id = 1``), created by migration 0013; a missing row reads as the defaults.
    """

    __tablename__ = "instance_settings"
    __table_args__ = (CheckConstraint("id = 1", name="singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # Any signed-in user may create a workspace (instance administrators always may).
    allow_workspace_creation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true()
    )
    updated_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    updated_by_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
