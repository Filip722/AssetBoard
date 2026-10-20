from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from assetboard.db.base import Base
from assetboard.db.types import UtcDateTime


class TotpFactor(Base):
    __tablename__ = "totp_factors"

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    encrypted_secret: Mapped[str] = mapped_column(String(256), nullable=False)
    enrollment_session_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    enrollment_expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    enabled_at: Mapped[datetime | None] = mapped_column(UtcDateTime(), nullable=True)
    last_step: Mapped[int] = mapped_column(Integer(), nullable=False, default=-1)


class TotpRecoveryCode(Base):
    __tablename__ = "totp_recovery_codes"

    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    code_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
