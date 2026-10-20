from __future__ import annotations

import base64
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

import pyotp
from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session, sessionmaker

from assetboard.clock import Clock
from assetboard.config import Settings
from assetboard.db.engine import run_immediate
from assetboard.db.models.identity import User
from assetboard.db.repositories.totp import TotpRepository
from assetboard.db.repositories.user_memberships import UserMembershipReader
from assetboard.domain.totp import RECOVERY_CODE_COUNT, matching_step, recovery_hash
from assetboard.services.auth import (
    Throttled,
    ThrottleSlot,
    blocked_until,
    derive_subkey,
    record_auth_event,
    reserve_attempts,
)
from assetboard.services.context import Role, WorkspaceChoice

ENROLLMENT_LIFETIME = timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class TotpState:
    enabled: bool
    secret: str | None = None
    workspaces: tuple[WorkspaceChoice, ...] = ()


def _cipher(settings: Settings) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(derive_subkey(settings.ensure_secret_key(), "totp")))


def state(
    factory: sessionmaker[Session],
    *,
    user_id: UUID,
    session_hash: str,
    settings: Settings,
    clock: Clock,
) -> TotpState:
    with factory() as session:
        repository = TotpRepository(session, user_id)
        workspaces = tuple(
            WorkspaceChoice(id=m.workspace_id, name=m.workspace_name, role=Role(m.role))
            for m in UserMembershipReader(session, user_id).memberships()
        )
        factor = repository.factor()
        if factor is None:
            return TotpState(False, workspaces=workspaces)
        if factor.enabled_at is not None:
            return TotpState(True, workspaces=workspaces)
        if (
            factor.enrollment_session_hash == session_hash
            and factor.enrollment_expires_at is not None
            and factor.enrollment_expires_at > clock.now()
        ):
            try:
                secret = _cipher(settings).decrypt(factor.encrypted_secret).decode()
            except InvalidToken:
                secret = None
            return TotpState(False, secret, workspaces)
        return TotpState(False, workspaces=workspaces)


def begin_enrollment(
    session: Session,
    user: User,
    *,
    session_hash: str,
    settings: Settings,
    clock: Clock,
) -> bool:
    repository = TotpRepository(session, user.id)
    factor = repository.factor(lock=True)
    if user.password_hash is None or not repository.owns_session(session_hash):
        return False
    if factor is not None and factor.enabled_at is not None:
        return False
    secret = (
        _cipher(settings)
        .encrypt_at_time(pyotp.random_base32().encode(), int(clock.now().timestamp()))
        .decode()
    )
    if factor is None:
        factor = repository.add_factor(encrypted_secret=secret)
    else:
        factor.encrypted_secret = secret
    factor.enrollment_session_hash = session_hash
    factor.enrollment_expires_at = clock.now() + ENROLLMENT_LIFETIME
    factor.last_step = -1
    return True


def complete_enrollment(
    factory: sessionmaker[Session],
    *,
    user_id: UUID,
    session_hash: str,
    code: str,
    settings: Settings,
    clock: Clock,
) -> list[str] | Throttled | None:
    def complete(session: Session) -> list[str] | Throttled | None:
        repository = TotpRepository(session, user_id)
        factor = repository.factor(lock=True)
        if (
            factor is None
            or factor.enabled_at is not None
            or factor.enrollment_session_hash != session_hash
            or factor.enrollment_expires_at is None
            or factor.enrollment_expires_at <= clock.now()
            or not repository.owns_session(session_hash)
        ):
            return None
        slot = ThrottleSlot(
            "account",
            f"totp:{user_id}",
            timedelta(minutes=settings.auth_ip_window_minutes),
            settings.auth_account_free_failures,
        )
        if reserve_attempts(session, (slot,), clock.now(), settings) is None:
            return Throttled(
                blocked_until(session, [(slot.scope, slot.key)], clock.now()) or clock.now()
            )
        try:
            secret = _cipher(settings).decrypt(factor.encrypted_secret).decode()
        except InvalidToken:
            return None
        step = matching_step(secret, code, clock.now(), factor.last_step)
        if step is None:
            return None
        factor.enabled_at = clock.now()
        factor.enrollment_session_hash = None
        factor.enrollment_expires_at = None
        factor.last_step = step
        codes = [secrets.token_hex(16) for _ in range(RECOVERY_CODE_COUNT)]
        repository.add_recovery_codes([recovery_hash(value) for value in codes])
        repository.update_password_sessions(session_hash, verified=True)
        record_auth_event(
            session,
            clock,
            action="auth.totp_enabled",
            entity_id=str(user_id),
            actor_user_id=user_id,
        )
        return codes

    return run_immediate(factory, complete)


def verify_sign_in(
    session: Session,
    user_id: UUID,
    *,
    code: str,
    settings: Settings,
    now: datetime,
) -> tuple[bool, bool]:
    repository = TotpRepository(session, user_id)
    factor = repository.factor(lock=True)
    if factor is None or factor.enabled_at is None:
        return True, False
    if len(code) > 64:
        return False, False
    try:
        secret = _cipher(settings).decrypt(factor.encrypted_secret).decode()
        step = matching_step(secret, code.strip(), now, factor.last_step)
    except InvalidToken:
        step = None
    if step is not None:
        factor.last_step = step
        return True, True
    recovered = repository.consume_recovery_code(recovery_hash(code))
    return recovered, recovered


def disable(
    session: Session,
    user: User,
    *,
    session_hash: str,
    clock: Clock,
) -> bool:
    repository = TotpRepository(session, user.id)
    if user.password_hash is None or not repository.owns_session(session_hash):
        return False
    repository.factor(lock=True)
    repository.clear()
    repository.update_password_sessions(session_hash, verified=False)
    record_auth_event(
        session, clock, action="auth.totp_disabled", entity_id=str(user.id), actor_user_id=user.id
    )
    return True
