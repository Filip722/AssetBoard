"""Local accounts, server-side sessions, throttling and first-run setup.

Flows that record throttle counters or audit events on failure (login, registration, setup
token) return an outcome instead of raising, so the caller commits those writes. Errors carry a
stable ``code`` that the web layer maps to a translated message; the English text is for logs
and the CLI only.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from importlib import resources
from typing import Literal
from uuid import UUID, uuid4

from argon2 import Parameters, PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from assetboard.clock import Clock
from assetboard.config import Settings
from assetboard.db.models.identity import (
    AuditEvent,
    AuthThrottle,
    Membership,
    SetupState,
    User,
    UserSession,
    Workspace,
)
from assetboard.domain.identity import MAX_EMAIL_LENGTH, InvalidEmail, normalize_email
from assetboard.services.workspace_options import WorkspaceOptions, validate_workspace_options

ActorType = Literal["user", "cli", "system"]
ThrottleScope = Literal["account", "ip", "setup", "register"]

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 1024
MAX_NAME_LENGTH = 120
SESSION_TOUCH_INTERVAL = timedelta(minutes=5)
THROTTLE_IDLE_RESET = timedelta(hours=24)

# Test-only seam: an autouse fixture swaps in cheap parameters; production never sets it.
ARGON2_PARAMETERS: Parameters | None = None
_cached_dummy_hashes: dict[str, str] = {}
_process_setup_token: str | None = None


class AuthError(ValueError):
    code = "auth_error"


class PasswordPolicyError(AuthError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class InvalidAccountDetails(AuthError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SetupAlreadyCompleted(AuthError):
    """Raised by the single deterministic loser of concurrent setup attempts."""

    code = "setup_completed"

    def __init__(self) -> None:
        super().__init__("Initial setup has already completed")


class EmailAlreadyRegistered(AuthError):
    code = "email_taken"

    def __init__(self) -> None:
        super().__init__("A user with that email address already exists")


class UnknownUser(AuthError):
    code = "unknown_user"

    def __init__(self) -> None:
        super().__init__("No user has that email address")


class RegistrationDisabled(AuthError):
    code = "registration_disabled"

    def __init__(self) -> None:
        super().__init__("Registration is disabled")


@dataclass(frozen=True, slots=True)
class LoginOutcome:
    """A failed login never says why: unknown email, wrong password and throttling look alike."""

    user: User | None = None
    session_id: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.user is not None and self.session_id is not None


PasswordChangeStatus = Literal["changed", "invalid_current", "invalid_new", "throttled"]


@dataclass(frozen=True, slots=True)
class PasswordChangeOutcome:
    status: PasswordChangeStatus
    revoked_sessions: int = 0
    error: AuthError | None = None


RegistrationStatus = Literal["created", "invalid", "rejected", "throttled"]


@dataclass(frozen=True, slots=True)
class RegistrationOutcome:
    """``rejected`` means the email is already registered.

    It differs from ``created``, so registration discloses whether an address has an account.
    This is an accepted risk: registration is off by default and limited per client address.
    """

    status: RegistrationStatus
    user: User | None = None
    workspace: Workspace | None = None
    error: AuthError | None = None


# Passwords


@lru_cache(maxsize=1)
def common_passwords() -> frozenset[str]:
    path = resources.files("assetboard").joinpath("data", "common-passwords.txt")
    return frozenset(line.casefold() for line in path.read_text(encoding="utf-8").splitlines())


def validate_password(password: str, email: str, display_name: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            "password_too_short", f"Password must contain at least {MIN_PASSWORD_LENGTH} characters"
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise PasswordPolicyError(
            "password_too_long", f"Password must contain at most {MAX_PASSWORD_LENGTH} characters"
        )
    folded = password.casefold()
    if folded in common_passwords():
        raise PasswordPolicyError(
            "password_too_common", "Choose a password that is not commonly used"
        )
    local_part = normalize_email(email).split("@", maxsplit=1)[0].casefold()
    if folded == local_part or folded == display_name.strip().casefold():
        raise PasswordPolicyError(
            "password_matches_identity",
            "Password must differ from your email name and display name",
        )


def password_hasher() -> PasswordHasher:
    """Production uses argon2-cffi defaults; tests patch ``ARGON2_PARAMETERS`` only."""
    if ARGON2_PARAMETERS is None:
        return PasswordHasher()
    return PasswordHasher.from_parameters(ARGON2_PARAMETERS)


def hash_password(password: str, email: str, display_name: str) -> str:
    validate_password(password, email, display_name)
    encoded = password_hasher().hash(password)
    if not encoded.startswith("$argon2id$"):
        raise RuntimeError("PasswordHasher did not produce an Argon2id hash")
    return encoded


def _dummy_hash() -> str:
    key = repr(ARGON2_PARAMETERS)
    if key not in _cached_dummy_hashes:
        # Never a valid credential for any account; never logged.
        _cached_dummy_hashes[key] = password_hasher().hash(secrets.token_urlsafe(32))
    return _cached_dummy_hashes[key]


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return password_hasher().verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# Keys, client addresses and audit


def session_id_hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def derive_subkey(secret_key: str, purpose: str) -> bytes:
    """Derive a purpose-specific key with HMAC extract/expand-style separation."""
    if not purpose or len(purpose) > 128:
        raise ValueError("Key purpose must contain 1 to 128 characters")
    prk = hmac.new(b"assetboard-subkey-v1", secret_key.encode("utf-8"), hashlib.sha256).digest()
    return hmac.new(prk, f"assetboard:{purpose}:v1".encode() + b"\x01", hashlib.sha256).digest()


def normalize_client_host(host: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse one ``request.client.host`` value; forwarding chains are never parsed here."""
    if host is None or "," in host:
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        # ::ffff:a.b.c.d from dual-stack sockets is the IPv4 client a.b.c.d.
        return address.ipv4_mapped
    return address


def client_ip_prefix(host: str | None, *, for_throttle: bool = False) -> str | None:
    """Stored prefixes are IPv4 /24 and IPv6 /48; throttle keys use IPv4 /32 and IPv6 /64."""
    address = normalize_client_host(host)
    if address is None:
        return None
    if for_throttle:
        prefix = 32 if address.version == 4 else 64
    else:
        prefix = 24 if address.version == 4 else 48
    return str(ipaddress.ip_network(f"{address}/{prefix}", strict=False))


def _key_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ip_key(host: str | None) -> str:
    return client_ip_prefix(host, for_throttle=True) or "unknown"


def _audit(
    session: Session,
    clock: Clock,
    *,
    action: str,
    entity_type: str,
    entity_id: str | None,
    actor_type: ActorType,
    actor_user_id: UUID | None = None,
    workspace_id: UUID | None = None,
    host: str | None = None,
    user_agent: str | None = None,
    data: dict[str, str | int | bool] | None = None,
) -> None:
    session.add(
        AuditEvent(
            id=uuid4(),
            workspace_id=workspace_id,
            actor_user_id=actor_user_id,
            actor_type=actor_type,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            data=data or {},
            ts=clock.now(),
            ip_prefix=client_ip_prefix(host),
            user_agent=user_agent[:200] if user_agent else None,
        )
    )


# Throttling


def _find_throttle(session: Session, scope: ThrottleScope, key: str) -> AuthThrottle | None:
    """Read-only lookup; checking a block never creates or changes a row."""
    return session.scalar(
        select(AuthThrottle).where(
            AuthThrottle.key_type == scope, AuthThrottle.key_hash == _key_hash(key)
        )
    )


def _current_count(row: AuthThrottle | None, now: datetime, window: timedelta | None) -> int:
    """Counter value as read: rows idle for 24 h or past their window count as zero."""
    if row is None or now - row.updated_at >= THROTTLE_IDLE_RESET:
        return 0
    if window is not None and now >= row.window_started_at + window:
        return 0
    return row.failure_count


def _is_blocked(row: AuthThrottle | None, now: datetime) -> bool:
    return row is not None and row.blocked_until is not None and row.blocked_until > now


def _get_or_create_throttle(
    session: Session, scope: ThrottleScope, key: str, now: datetime
) -> AuthThrottle:
    row = _find_throttle(session, scope, key)
    if row is not None:
        return row
    row = AuthThrottle(
        id=uuid4(),
        key_type=scope,
        key_hash=_key_hash(key),
        failure_count=0,
        window_started_at=now,
        updated_at=now,
    )
    try:
        with session.begin_nested():
            session.add(row)
    except IntegrityError:
        # A concurrent request created the same key first.
        existing = _find_throttle(session, scope, key)
        if existing is None:
            raise
        return existing
    return row


def _count_attempt(
    session: Session,
    scope: ThrottleScope,
    key: str,
    *,
    now: datetime,
    window: timedelta | None,
    free: int | None,
    settings: Settings,
) -> AuthThrottle:
    """Record one attempt; above ``free`` attempts, back off exponentially up to the cap."""
    row = _get_or_create_throttle(session, scope, key, now)
    if _current_count(row, now, window) == 0:
        row.failure_count = 0
        row.window_started_at = now
        if not _is_blocked(row, now):
            row.blocked_until = None
    row.failure_count += 1
    row.updated_at = now
    if free is not None and row.failure_count > free:
        exponent = min(row.failure_count - free - 1, 32)
        seconds = min(
            settings.auth_throttle_base_seconds * (2**exponent),
            settings.auth_throttle_max_seconds,
        )
        row.blocked_until = now + timedelta(seconds=seconds)
    return row


def _count_account_failure(
    session: Session, account_key: str, now: datetime, settings: Settings
) -> None:
    _count_attempt(
        session,
        "account",
        account_key,
        now=now,
        window=None,
        free=settings.auth_account_free_failures,
        settings=settings,
    )


def _reset_account(session: Session, account_key: str, now: datetime) -> None:
    row = _find_throttle(session, "account", account_key)
    if row is not None and (row.failure_count or row.blocked_until is not None):
        row.failure_count = 0
        row.blocked_until = None
        row.updated_at = now


def prune_auth_throttles(session: Session, clock: Clock) -> int:
    """Delete idle throttle rows older than 24 hours that no longer block anything."""
    now = clock.now()
    deleted = session.scalars(
        delete(AuthThrottle)
        .where(
            AuthThrottle.updated_at < now - THROTTLE_IDLE_RESET,
            or_(AuthThrottle.blocked_until.is_(None), AuthThrottle.blocked_until <= now),
        )
        .returning(AuthThrottle.id)
        .execution_options(synchronize_session=False)
    ).all()
    return len(deleted)


# Setup token


def setup_token_for_process(
    settings: Settings, *, user_exists: bool, logger: logging.Logger
) -> str | None:
    """Return the setup token only while setup is open; configured secrets are never logged."""
    global _process_setup_token
    if user_exists:
        return None
    if settings.setup_token is not None:
        return settings.setup_token.get_secret_value()
    if _process_setup_token is None:
        _process_setup_token = secrets.token_urlsafe(32)
        logger.warning("Generated first-run setup token: %s", _process_setup_token)
    return _process_setup_token


def verify_setup_token(
    session: Session,
    provided: str,
    expected: str,
    *,
    clock: Clock,
    settings: Settings,
    host: str | None,
) -> bool:
    """Check a setup token; the caller must commit after ``False`` to keep the counter."""
    now = clock.now()
    key = _ip_key(host)
    if _is_blocked(_find_throttle(session, "setup", key), now):
        return False
    if secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        return True
    _count_attempt(
        session,
        "setup",
        key,
        now=now,
        window=timedelta(minutes=settings.auth_ip_window_minutes),
        free=settings.setup_throttle_free_failures,
        settings=settings,
    )
    prune_auth_throttles(session, clock)
    return False


# Login and sessions


def authenticate_user(
    session: Session,
    *,
    email: str,
    password: str,
    settings: Settings,
    clock: Clock,
    host: str | None,
    user_agent: str | None = None,
    existing_session_id: str | None = None,
) -> LoginOutcome:
    """Authenticate with email + password; the caller commits whatever the outcome."""
    now = clock.now()
    lookup = email.strip().lower()[:MAX_EMAIL_LENGTH]
    try:
        account_key = normalize_email(lookup)
    except InvalidEmail:
        account_key = f"invalid:{lookup}"
    ip_key = _ip_key(host)
    # Blocked attempts are answered from read-only lookups and write nothing.
    if _is_blocked(_find_throttle(session, "ip", ip_key), now) or _is_blocked(
        _find_throttle(session, "account", account_key), now
    ):
        return LoginOutcome()

    user = session.scalar(select(User).where(User.email == lookup))
    password_hash = user.password_hash if user is not None else None
    verified = verify_password(password_hash or _dummy_hash(), password)
    if user is None or password_hash is None or not user.is_active or not verified:
        _count_account_failure(session, account_key, now, settings)
        _count_attempt(
            session,
            "ip",
            ip_key,
            now=now,
            window=timedelta(minutes=settings.auth_ip_window_minutes),
            free=settings.auth_ip_free_failures,
            settings=settings,
        )
        prune_auth_throttles(session, clock)
        _audit(
            session,
            clock,
            action="auth.login_failure",
            entity_type="user",
            entity_id=str(user.id) if user else None,
            actor_type="user",
            actor_user_id=user.id if user else None,
            host=host,
            user_agent=user_agent,
        )
        return LoginOutcome()

    # Only the account counter resets; the per-IP counter decays with its time window.
    _reset_account(session, account_key, now)
    hasher = password_hasher()
    if hasher.check_needs_rehash(password_hash):
        user.password_hash = hasher.hash(password)
    user.last_login_at = now
    if existing_session_id:
        session.execute(
            delete(UserSession).where(UserSession.id_hash == session_id_hash(existing_session_id))
        )
    new_session_id = create_user_session(
        session, user, settings=settings, clock=clock, host=host, user_agent=user_agent
    )
    _audit(
        session,
        clock,
        action="auth.login_success",
        entity_type="user",
        entity_id=str(user.id),
        actor_type="user",
        actor_user_id=user.id,
        host=host,
        user_agent=user_agent,
    )
    return LoginOutcome(user=user, session_id=new_session_id)


def create_user_session(
    session: Session,
    user: User,
    *,
    settings: Settings,
    clock: Clock,
    host: str | None,
    user_agent: str | None = None,
) -> str:
    session_id = secrets.token_urlsafe(32)
    now = clock.now()
    session.add(
        UserSession(
            id_hash=session_id_hash(session_id),
            user_id=user.id,
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(days=settings.session_absolute_timeout_days),
            user_agent=user_agent[:200] if user_agent else None,
            ip_prefix=client_ip_prefix(host),
        )
    )
    session.flush()
    return session_id


def get_valid_session(
    session: Session,
    session_id: str,
    *,
    settings: Settings,
    clock: Clock,
) -> UserSession | None:
    session_row = session.get(UserSession, session_id_hash(session_id))
    if session_row is None:
        return None
    now = clock.now()
    idle_expiry = session_row.last_seen_at + timedelta(days=settings.session_idle_timeout_days)
    user = session.get(User, session_row.user_id)
    if session_row.expires_at <= now or idle_expiry <= now or user is None or not user.is_active:
        session.delete(session_row)
        return None
    return session_row


def touch_session(session_row: UserSession, clock: Clock) -> bool:
    """Record activity at most every five minutes to avoid a write per request."""
    now = clock.now()
    if now - session_row.last_seen_at < SESSION_TOUCH_INTERVAL:
        return False
    session_row.last_seen_at = now
    return True


def revoke_session(
    session: Session,
    session_id: str,
    *,
    clock: Clock,
    host: str | None = None,
    user_agent: str | None = None,
) -> None:
    session_row = session.get(UserSession, session_id_hash(session_id))
    if session_row is None:
        return
    user_id = session_row.user_id
    session.delete(session_row)
    _audit(
        session,
        clock,
        action="auth.logout",
        entity_type="user",
        entity_id=str(user_id),
        actor_type="user",
        actor_user_id=user_id,
        host=host,
        user_agent=user_agent,
    )


def _delete_sessions_except(session: Session, user_id: UUID, keep_hash: str | None) -> int:
    rows = session.scalars(select(UserSession).where(UserSession.user_id == user_id)).all()
    revoked = 0
    for row in rows:
        if row.id_hash != keep_hash:
            session.delete(row)
            revoked += 1
    return revoked


def revoke_other_sessions(
    session: Session,
    user_id: UUID,
    current_session_id: str,
    *,
    clock: Clock,
    host: str | None = None,
    user_agent: str | None = None,
) -> int:
    revoked = _delete_sessions_except(session, user_id, session_id_hash(current_session_id))
    _audit(
        session,
        clock,
        action="auth.logout",
        entity_type="user",
        entity_id=str(user_id),
        actor_type="user",
        actor_user_id=user_id,
        host=host,
        user_agent=user_agent,
        data={"scope": "other_devices", "revoked_count": revoked},
    )
    return revoked


def change_password(
    session: Session,
    user: User,
    current_password: str,
    new_password: str,
    *,
    settings: Settings,
    clock: Clock,
    current_session_id: str | None = None,
    host: str | None = None,
    user_agent: str | None = None,
) -> PasswordChangeOutcome:
    """Change the password and revoke every other session; the caller commits any outcome.

    Wrong current passwords count against the same per-account throttle as login.
    """
    now = clock.now()
    account_key = user.email
    if _is_blocked(_find_throttle(session, "account", account_key), now):
        return PasswordChangeOutcome(status="throttled")
    if user.password_hash is None or not verify_password(user.password_hash, current_password):
        _count_account_failure(session, account_key, now, settings)
        prune_auth_throttles(session, clock)
        return PasswordChangeOutcome(status="invalid_current")
    _reset_account(session, account_key, now)
    try:
        new_hash = hash_password(new_password, user.email, user.display_name)
    except PasswordPolicyError as exc:
        return PasswordChangeOutcome(status="invalid_new", error=exc)
    user.password_hash = new_hash
    keep_hash = session_id_hash(current_session_id) if current_session_id else None
    revoked = _delete_sessions_except(session, user.id, keep_hash)
    _audit(
        session,
        clock,
        action="auth.password_change",
        entity_type="user",
        entity_id=str(user.id),
        actor_type="user",
        actor_user_id=user.id,
        host=host,
        user_agent=user_agent,
        data={"other_sessions_revoked": revoked},
    )
    return PasswordChangeOutcome(status="changed", revoked_sessions=revoked)


# Account creation


def _claim_setup(session: Session, clock: Clock) -> bool:
    """Atomically mark first-run setup complete; only one transaction can win."""
    result = session.execute(
        update(SetupState)
        .where(SetupState.id == 1, SetupState.completed_at.is_(None))
        .values(completed_at=clock.now())
        .returning(SetupState.id)
    )
    return result.scalar_one_or_none() is not None


def _clean_name(value: str, code: str, message: str) -> str:
    cleaned = value.strip()
    if not cleaned or len(cleaned) > MAX_NAME_LENGTH:
        raise InvalidAccountDetails(code, message)
    return cleaned


def _workspace_options(
    settings: Settings,
    *,
    base_currency: str | None,
    country_code: str | None,
    timezone: str | None,
    workspace_locale: str | None,
) -> WorkspaceOptions:
    try:
        return validate_workspace_options(
            base_currency=base_currency or settings.default_currency,
            country_code=country_code,
            timezone=timezone or "UTC",
            locale=workspace_locale or settings.default_locale,
        )
    except ValueError as exc:
        raise InvalidAccountDetails("invalid_workspace_settings", str(exc)) from exc


def _create_user_records(
    session: Session,
    *,
    email: str,
    display_name: str,
    password: str,
    workspace_name: str | None,
    options: WorkspaceOptions | None,
    settings: Settings,
    is_admin: bool,
) -> tuple[User, Workspace | None]:
    """Create a user and, when ``options`` is given, a workspace they own."""
    clean_display_name = _clean_name(
        display_name, "invalid_display_name", "Display name must contain 1 to 120 characters"
    )
    normalized_email = normalize_email(email)
    password_hash = hash_password(password, normalized_email, clean_display_name)
    user = User(
        email=normalized_email,
        display_name=clean_display_name,
        password_hash=password_hash,
        locale=options.locale if options else settings.default_locale,
        is_active=True,
        is_instance_admin=is_admin,
    )
    session.add(user)
    workspace = None
    if options is not None:
        workspace = Workspace(
            name=_clean_name(
                workspace_name or clean_display_name,
                "invalid_workspace_name",
                "Workspace name must contain 1 to 120 characters",
            ),
            base_currency=options.base_currency,
            country_code=options.country_code,
            timezone=options.timezone,
            locale=options.locale,
        )
        session.add(workspace)
    session.flush()
    if workspace is not None:
        session.add(Membership(workspace_id=workspace.id, user_id=user.id, role="owner"))
        session.flush()
    return user, workspace


def create_initial_admin(
    session: Session,
    *,
    email: str,
    display_name: str,
    password: str,
    workspace_name: str,
    settings: Settings,
    clock: Clock,
    base_currency: str | None = None,
    country_code: str | None = None,
    timezone: str | None = None,
    workspace_locale: str | None = None,
    host: str | None = None,
    user_agent: str | None = None,
) -> tuple[User, Workspace]:
    """First-run setup: admin + workspace + owner membership in the caller's transaction."""
    options = _workspace_options(
        settings,
        base_currency=base_currency,
        country_code=country_code,
        timezone=timezone,
        workspace_locale=workspace_locale,
    )
    if not _claim_setup(session, clock) or session.scalar(select(User.id).limit(1)) is not None:
        raise SetupAlreadyCompleted
    user, workspace = _create_user_records(
        session,
        email=email,
        display_name=display_name,
        password=password,
        workspace_name=workspace_name,
        options=options,
        settings=settings,
        is_admin=True,
    )
    if workspace is None:
        raise RuntimeError("Initial setup must create a workspace")
    _audit(
        session,
        clock,
        action="setup.create_admin",
        entity_type="user",
        entity_id=str(user.id),
        actor_type="system",
        workspace_id=workspace.id,
        host=host,
        user_agent=user_agent,
    )
    session.flush()
    return user, workspace


def create_admin_cli(
    session: Session,
    *,
    email: str,
    display_name: str,
    password: str,
    settings: Settings,
    clock: Clock,
    workspace_name: str | None = None,
    create_workspace: bool = True,
    base_currency: str | None = None,
    country_code: str | None = None,
    timezone: str | None = None,
    workspace_locale: str | None = None,
) -> tuple[User, Workspace | None]:
    """Break-glass admin creation; works with or without existing users, never modifies one."""
    normalized_email = normalize_email(email)
    if session.scalar(select(User.id).where(User.email == normalized_email)) is not None:
        raise EmailAlreadyRegistered
    options = (
        _workspace_options(
            settings,
            base_currency=base_currency,
            country_code=country_code,
            timezone=timezone,
            workspace_locale=workspace_locale,
        )
        if create_workspace
        else None
    )
    # Close the web setup gate too, so /setup cannot race the CLI on an empty instance.
    _claim_setup(session, clock)
    user, workspace = _create_user_records(
        session,
        email=normalized_email,
        display_name=display_name,
        password=password,
        workspace_name=workspace_name,
        options=options,
        settings=settings,
        is_admin=True,
    )
    _audit(
        session,
        clock,
        action="cli.create_admin",
        entity_type="user",
        entity_id=str(user.id),
        actor_type="cli",
    )
    session.flush()
    return user, workspace


def register_user(
    session: Session,
    *,
    email: str,
    display_name: str,
    password: str,
    workspace_name: str,
    settings: Settings,
    clock: Clock,
    host: str | None,
    user_agent: str | None = None,
    base_currency: str | None = None,
    country_code: str | None = None,
    timezone: str | None = None,
    workspace_locale: str | None = None,
) -> RegistrationOutcome:
    """Open registration; every attempt counts against its own per-IP window.

    Registration never touches the login throttle, so it cannot be used to clear it. The
    outcome discloses whether an email is already registered (see ``RegistrationOutcome``).
    The caller commits whatever the outcome.
    """
    if not settings.allow_registration:
        raise RegistrationDisabled
    now = clock.now()
    key = _ip_key(host)
    window = timedelta(minutes=settings.registration_window_minutes)
    row = _find_throttle(session, "register", key)
    if _current_count(row, now, window) >= settings.registration_ip_limit:
        return RegistrationOutcome(status="throttled")
    _count_attempt(session, "register", key, now=now, window=window, free=None, settings=settings)
    prune_auth_throttles(session, clock)

    try:
        normalized_email = normalize_email(email)
        clean_display_name = _clean_name(
            display_name, "invalid_display_name", "Display name must contain 1 to 120 characters"
        )
        validate_password(password, normalized_email, clean_display_name)
        options = _workspace_options(
            settings,
            base_currency=base_currency,
            country_code=country_code,
            timezone=timezone,
            workspace_locale=workspace_locale,
        )
    except (AuthError, InvalidEmail) as exc:
        error = exc if isinstance(exc, AuthError) else InvalidAccountDetails(exc.code, str(exc))
        return RegistrationOutcome(status="invalid", error=error)

    if session.scalar(select(User.id).where(User.email == normalized_email)) is not None:
        # Spend the same hashing cost as a real registration before rejecting.
        password_hasher().hash(password)
        return RegistrationOutcome(status="rejected")

    user, workspace = _create_user_records(
        session,
        email=normalized_email,
        display_name=clean_display_name,
        password=password,
        workspace_name=workspace_name,
        options=options,
        settings=settings,
        is_admin=False,
    )
    if workspace is None:
        raise RuntimeError("Registration must create a workspace")
    _audit(
        session,
        clock,
        action="auth.register",
        entity_type="user",
        entity_id=str(user.id),
        actor_type="user",
        actor_user_id=user.id,
        workspace_id=workspace.id,
        host=host,
        user_agent=user_agent,
    )
    session.flush()
    return RegistrationOutcome(status="created", user=user, workspace=workspace)


def reset_user_password(
    session: Session,
    *,
    email: str,
    password: str,
    settings: Settings,
    clock: Clock,
) -> User:
    """CLI password reset: sets a new password and revokes all of the user's sessions."""
    normalized = normalize_email(email)
    user = session.scalar(select(User).where(User.email == normalized))
    if user is None:
        raise UnknownUser
    user.password_hash = hash_password(password, user.email, user.display_name)
    revoked = _delete_sessions_except(session, user.id, None)
    _audit(
        session,
        clock,
        action="cli.reset_password",
        entity_type="user",
        entity_id=str(user.id),
        actor_type="cli",
        data={"sessions_revoked": revoked},
    )
    session.flush()
    return user
