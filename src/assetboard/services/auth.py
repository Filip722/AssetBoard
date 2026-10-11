"""Local accounts, server-side sessions, throttling and first-run setup.

Flows that record throttle counters or audit events on failure (login, password change,
registration, setup token) return an outcome instead of raising, so the caller commits those
writes. Errors carry a stable ``code`` that the web layer maps to a translated message; the
English text is for logs and the CLI only.

Login and password change reserve their throttle attempt *before* the password is verified.
With a session factory, the reservation is its own short ``BEGIN IMMEDIATE`` transaction
(ADR-0007), so parallel requests cannot all pass the throttle check before any failure is
recorded; slow hashing then runs outside any transaction. The single-session functions run the
same steps inside the caller's transaction.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import logging
import secrets
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
from importlib import resources
from typing import Literal, Protocol
from uuid import UUID, uuid4

import anyio.from_thread
import anyio.to_thread
from argon2 import Parameters, PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from assetboard.clock import Clock
from assetboard.config import Settings
from assetboard.db.engine import is_database_locked, run_immediate
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
from assetboard.services.hash_gate import HashGate, HashGateBusy
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
_dummy_hash_lock = threading.Lock()

Verifier = Callable[[str, str], bool]
Hasher = Callable[[str], str]


class TransactionRunner(Protocol):
    """Runs one unit of work in a transaction and returns its result."""

    def __call__[T](self, operation: Callable[[Session], T], /) -> T: ...

    def read[T](self, operation: Callable[[Session], T], /) -> T:
        """Run a read-only step; it must not write."""
        ...


class SameSession:
    """Runs every step in the caller's session and transaction (CLI, tests)."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def __call__[T](self, operation: Callable[[Session], T], /) -> T:
        return operation(self._session)

    def read[T](self, operation: Callable[[Session], T], /) -> T:
        return operation(self._session)


class ImmediateTransactions:
    """Runs every write step in its own committed ``BEGIN IMMEDIATE`` transaction (web).

    Read-only steps use an ordinary deferred transaction, so they never take the write lock.
    """

    def __init__(self, factory: sessionmaker[Session]) -> None:
        self._factory = factory

    def __call__[T](self, operation: Callable[[Session], T], /) -> T:
        return run_immediate(self._factory, operation)

    def read[T](self, operation: Callable[[Session], T], /) -> T:
        with self._factory() as session:
            return operation(session)


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


def argon2_hash(password: str) -> str:
    """Hash without policy checks; callers validate first."""
    encoded = password_hasher().hash(password)
    if not encoded.startswith("$argon2id$"):
        raise RuntimeError("PasswordHasher did not produce an Argon2id hash")
    return encoded


def hash_password(password: str, email: str, display_name: str) -> str:
    validate_password(password, email, display_name)
    return argon2_hash(password)


def _dummy_hash() -> str:
    """The hash verified for unknown users; computed once per process, never recomputed.

    ``prepare_dummy_hash`` runs at startup, so requests normally only read the cache. The lock
    makes concurrent cold callers (CLI, tests) compute it exactly once.
    """
    key = repr(ARGON2_PARAMETERS)
    cached = _cached_dummy_hashes.get(key)
    if cached is not None:
        return cached
    with _dummy_hash_lock:
        if key not in _cached_dummy_hashes:
            # Never a valid credential for any account; never logged.
            _cached_dummy_hashes[key] = password_hasher().hash(secrets.token_urlsafe(32))
        return _cached_dummy_hashes[key]


def prepare_dummy_hash() -> None:
    """Compute the dummy hash at startup so no request pays for it."""
    _dummy_hash()


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


def _reset_account(session: Session, account_key: str, now: datetime) -> None:
    """Successful authentication: clear the account counter and start a new window.

    The new ``window_started_at`` makes any reservation still in flight from before the reset
    a no-op when it is released.
    """
    row = _find_throttle(session, "account", account_key)
    if row is not None:
        row.failure_count = 0
        row.blocked_until = None
        row.window_started_at = now
        row.updated_at = now


@dataclass(frozen=True, slots=True)
class ThrottleSlot:
    scope: ThrottleScope
    key: str
    window: timedelta | None
    free: int


@dataclass(frozen=True, slots=True)
class ReservedAttempt:
    """One counted attempt, identified so the same request can take it back."""

    scope: ThrottleScope
    key: str
    window_started_at: datetime
    block_set: datetime | None


def reserve_attempts(
    session: Session, slots: Sequence[ThrottleSlot], now: datetime, settings: Settings
) -> tuple[ReservedAttempt, ...] | None:
    """Count one attempt in every slot, or return ``None`` (writing nothing) if any is blocked."""
    if any(_is_blocked(_find_throttle(session, slot.scope, slot.key), now) for slot in slots):
        return None
    reserved: list[ReservedAttempt] = []
    for slot in slots:
        row = _count_attempt(
            session,
            slot.scope,
            slot.key,
            now=now,
            window=slot.window,
            free=slot.free,
            settings=settings,
        )
        block_set = row.blocked_until if row.failure_count > slot.free else None
        reserved.append(ReservedAttempt(slot.scope, slot.key, row.window_started_at, block_set))
    return tuple(reserved)


def release_attempts(session: Session, reserved: Sequence[ReservedAttempt]) -> None:
    """Take back this request's own attempts.

    Only within the same window (a reset or a new window since then makes it a no-op), never
    below zero, and a block is cleared only if this reservation set it.
    """
    for attempt in reserved:
        row = _find_throttle(session, attempt.scope, attempt.key)
        if row is None or row.window_started_at != attempt.window_started_at:
            continue
        row.failure_count = max(0, row.failure_count - 1)
        if attempt.block_set is not None and row.blocked_until == attempt.block_set:
            row.blocked_until = None


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


def setup_is_open(session: Session) -> bool:
    """The single definition of "first-run setup is open": ``setup_state.completed_at`` is null.

    Every path that creates a user claims setup in the same transaction (web setup and
    ``create-admin``), and registration refuses while setup is open.
    """
    row = session.execute(select(SetupState.completed_at).where(SetupState.id == 1)).one_or_none()
    if row is None:
        raise RuntimeError("setup_state row is missing; the database is not migrated")
    return row[0] is None


def setup_token_at_startup(
    settings: Settings, *, setup_open: bool, logger: logging.Logger
) -> str | None:
    """Called once per process start: the token for this process, or ``None`` if setup is closed.

    A configured ``SETUP_TOKEN`` is returned and never logged. Otherwise a 256-bit token is
    generated and logged once at WARNING, only while setup is open.
    """
    if not setup_open:
        return None
    if settings.setup_token is not None:
        return settings.setup_token.get_secret_value()
    token = secrets.token_urlsafe(32)
    logger.warning("Generated first-run setup token: %s", token)
    return token


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


def _login_keys(email: str) -> tuple[str, str]:
    lookup = email.strip().lower()[:MAX_EMAIL_LENGTH]
    try:
        return lookup, normalize_email(lookup)
    except InvalidEmail:
        return lookup, f"invalid:{lookup}"


@dataclass(frozen=True, slots=True)
class _LoginCandidate:
    reserved: tuple[ReservedAttempt, ...]
    user_id: UUID | None
    password_hash: str | None


def _login_flow(
    run: TransactionRunner,
    *,
    email: str,
    password: str,
    settings: Settings,
    clock: Clock,
    host: str | None,
    user_agent: str | None,
    existing_session_id: str | None,
    verify: Verifier | None,
    hasher: Hasher | None,
) -> LoginOutcome:
    lookup, account_key = _login_keys(email)
    slots = (
        ThrottleSlot("account", account_key, None, settings.auth_account_free_failures),
        ThrottleSlot(
            "ip",
            _ip_key(host),
            timedelta(minutes=settings.auth_ip_window_minutes),
            settings.auth_ip_free_failures,
        ),
    )

    def reserve(session: Session) -> _LoginCandidate | None:
        # Blocked attempts are answered from read-only lookups and write nothing.
        reserved = reserve_attempts(session, slots, clock.now(), settings)
        if reserved is None:
            return None
        user = session.scalar(select(User).where(User.email == lookup))
        return _LoginCandidate(
            reserved, user.id if user else None, user.password_hash if user else None
        )

    def blocked(session: Session) -> bool:
        now = clock.now()
        return any(_is_blocked(_find_throttle(session, s.scope, s.key), now) for s in slots)

    # A deferred read first, so a flood of blocked requests never takes the write lock;
    # ``reserve`` checks again under the lock.
    if run.read(blocked):
        return LoginOutcome()
    candidate = run(reserve)
    if candidate is None:
        return LoginOutcome()
    rehashed: str | None = None
    try:
        check = verify or verify_password
        verified = check(candidate.password_hash or _dummy_hash(), password)
        # Decided right after verification and hashed before ``finish``: no hashing ever
        # runs inside the write transaction.
        if (
            verified
            and candidate.password_hash is not None
            and password_hasher().check_needs_rehash(candidate.password_hash)
        ):
            try:
                rehashed = (hasher or argon2_hash)(password)
            except HashGateBusy:
                # Best effort: the login still succeeds; the next login retries the rehash.
                rehashed = None
    except BaseException:
        run(lambda session: release_attempts(session, candidate.reserved))
        raise

    def finish(session: Session) -> LoginOutcome:
        now = clock.now()
        user = session.get(User, candidate.user_id) if candidate.user_id else None
        succeeded = (
            verified
            and user is not None
            and user.is_active
            and user.password_hash is not None
            and user.password_hash == candidate.password_hash
        )
        if not succeeded or user is None or user.password_hash is None:
            # The reservation stays: it is this failure's count.
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
        # The account counter resets; the IP counter only takes back this request's attempt
        # and otherwise decays with its time window.
        _reset_account(session, account_key, now)
        release_attempts(session, [item for item in candidate.reserved if item.scope == "ip"])
        if rehashed is not None:
            # Safe: ``succeeded`` above means the stored hash is still the verified one.
            user.password_hash = rehashed
        user.last_login_at = now
        if existing_session_id:
            session.execute(
                delete(UserSession).where(
                    UserSession.id_hash == session_id_hash(existing_session_id)
                )
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

    return run(finish)


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
    """Authenticate inside the caller's transaction; the caller commits whatever the outcome."""
    return _login_flow(
        SameSession(session),
        email=email,
        password=password,
        settings=settings,
        clock=clock,
        host=host,
        user_agent=user_agent,
        existing_session_id=existing_session_id,
        verify=None,
        hasher=None,
    )


def authenticate_with_factory(
    factory: sessionmaker[Session],
    *,
    email: str,
    password: str,
    settings: Settings,
    clock: Clock,
    host: str | None,
    user_agent: str | None = None,
    existing_session_id: str | None = None,
    verify: Verifier | None = None,
    hasher: Hasher | None = None,
) -> LoginOutcome:
    """Authenticate with the reservation committed before ``verify`` runs (blocking)."""
    return _login_flow(
        ImmediateTransactions(factory),
        email=email,
        password=password,
        settings=settings,
        clock=clock,
        host=host,
        user_agent=user_agent,
        existing_session_id=existing_session_id,
        verify=verify,
        hasher=hasher,
    )


def _gate_verifier(gate: HashGate) -> Verifier:
    """From a worker thread, run each verification through the event loop's hash gate."""

    def verify(password_hash: str, password: str) -> bool:
        return anyio.from_thread.run(gate.run, lambda: verify_password(password_hash, password))

    return verify


def _gate_hasher(gate: HashGate) -> Hasher:
    def hash_(password: str) -> str:
        return anyio.from_thread.run(gate.run, lambda: argon2_hash(password))

    return hash_


async def authenticate_user_async(
    factory: sessionmaker[Session],
    gate: HashGate,
    *,
    email: str,
    password: str,
    settings: Settings,
    clock: Clock,
    host: str | None,
    user_agent: str | None = None,
    existing_session_id: str | None = None,
) -> LoginOutcome:
    """Web entry point: database work in a worker thread, hashing through ``gate``.

    Raises ``HashGateBusy`` (after releasing the reserved attempt) when hashing is saturated.
    """
    verify, hasher = _gate_verifier(gate), _gate_hasher(gate)
    return await anyio.to_thread.run_sync(
        lambda: authenticate_with_factory(
            factory,
            email=email,
            password=password,
            settings=settings,
            clock=clock,
            host=host,
            user_agent=user_agent,
            existing_session_id=existing_session_id,
            verify=verify,
            hasher=hasher,
        )
    )


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


def _change_password_flow(
    run: TransactionRunner,
    *,
    user_id: UUID,
    current_password: str,
    new_password: str,
    settings: Settings,
    clock: Clock,
    current_session_id: str | None,
    host: str | None,
    user_agent: str | None,
    verify: Verifier | None,
    hasher: Hasher | None,
) -> PasswordChangeOutcome:
    @dataclass(frozen=True, slots=True)
    class Candidate:
        reserved: tuple[ReservedAttempt, ...]
        password_hash: str | None
        email: str
        display_name: str

    def reserve(session: Session) -> Candidate | None:
        user = session.get(User, user_id)
        if user is None:
            raise UnknownUser
        slot = ThrottleSlot("account", user.email, None, settings.auth_account_free_failures)
        reserved = reserve_attempts(session, (slot,), clock.now(), settings)
        if reserved is None:
            return None
        return Candidate(reserved, user.password_hash, user.email, user.display_name)

    def blocked(session: Session) -> bool:
        user = session.get(User, user_id)
        if user is None:
            raise UnknownUser
        return _is_blocked(_find_throttle(session, "account", user.email), clock.now())

    # Deferred read first; ``reserve`` checks again under the write lock.
    if run.read(blocked):
        return PasswordChangeOutcome(status="throttled")
    candidate = run(reserve)
    if candidate is None:
        return PasswordChangeOutcome(status="throttled")

    def release(session: Session) -> None:
        release_attempts(session, candidate.reserved)

    try:
        check = verify or verify_password
        verified = candidate.password_hash is not None and check(
            candidate.password_hash, current_password
        )
    except BaseException:
        run(release)
        raise
    if not verified:
        # The reservation stays: it is this failure's count.
        run(lambda session: prune_auth_throttles(session, clock))
        return PasswordChangeOutcome(status="invalid_current")

    try:
        validate_password(new_password, candidate.email, candidate.display_name)
    except PasswordPolicyError as exc:
        run(lambda session: _reset_account(session, candidate.email, clock.now()))
        return PasswordChangeOutcome(status="invalid_new", error=exc)
    try:
        new_hash = (hasher or argon2_hash)(new_password)
    except BaseException:
        run(release)
        raise

    def apply(session: Session) -> PasswordChangeOutcome:
        _reset_account(session, candidate.email, clock.now())
        user = session.get(User, user_id)
        if user is None or user.password_hash != candidate.password_hash:
            # Changed concurrently (another device or the CLI); do not overwrite it.
            return PasswordChangeOutcome(status="invalid_current")
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

    return run(apply)


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
    return _change_password_flow(
        SameSession(session),
        user_id=user.id,
        current_password=current_password,
        new_password=new_password,
        settings=settings,
        clock=clock,
        current_session_id=current_session_id,
        host=host,
        user_agent=user_agent,
        verify=None,
        hasher=None,
    )


def change_password_with_factory(
    factory: sessionmaker[Session],
    *,
    user_id: UUID,
    current_password: str,
    new_password: str,
    settings: Settings,
    clock: Clock,
    current_session_id: str | None = None,
    host: str | None = None,
    user_agent: str | None = None,
    verify: Verifier | None = None,
    hasher: Hasher | None = None,
) -> PasswordChangeOutcome:
    return _change_password_flow(
        ImmediateTransactions(factory),
        user_id=user_id,
        current_password=current_password,
        new_password=new_password,
        settings=settings,
        clock=clock,
        current_session_id=current_session_id,
        host=host,
        user_agent=user_agent,
        verify=verify,
        hasher=hasher,
    )


async def change_password_async(
    factory: sessionmaker[Session],
    gate: HashGate,
    *,
    user_id: UUID,
    current_password: str,
    new_password: str,
    settings: Settings,
    clock: Clock,
    current_session_id: str | None = None,
    host: str | None = None,
    user_agent: str | None = None,
) -> PasswordChangeOutcome:
    verify, hasher = _gate_verifier(gate), _gate_hasher(gate)
    return await anyio.to_thread.run_sync(
        lambda: change_password_with_factory(
            factory,
            user_id=user_id,
            current_password=current_password,
            new_password=new_password,
            settings=settings,
            clock=clock,
            current_session_id=current_session_id,
            host=host,
            user_agent=user_agent,
            verify=verify,
            hasher=hasher,
        )
    )


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
    password_hash: str | None = None,
) -> tuple[User, Workspace | None]:
    """Create a user and, when ``options`` is given, a workspace they own.

    ``password_hash`` lets the caller hash outside the transaction; the policy is checked here
    either way.
    """
    clean_display_name = _clean_name(
        display_name, "invalid_display_name", "Display name must contain 1 to 120 characters"
    )
    normalized_email = normalize_email(email)
    validate_password(password, normalized_email, clean_display_name)
    password_hash = password_hash or argon2_hash(password)
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
    password_hash: str | None = None,
) -> tuple[User, Workspace]:
    """First-run setup: admin + workspace + owner membership in the caller's transaction."""
    options = _workspace_options(
        settings,
        base_currency=base_currency,
        country_code=country_code,
        timezone=timezone,
        workspace_locale=workspace_locale,
    )
    if not _claim_setup(session, clock):
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
        password_hash=password_hash,
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


def run_initial_setup(
    factory: sessionmaker[Session],
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
    hasher: Hasher | None = None,
) -> tuple[User, Workspace]:
    """First-run setup in its own ``BEGIN IMMEDIATE`` transaction, hashing beforehand.

    Exactly one concurrent caller wins; every loser gets ``SetupAlreadyCompleted``, including
    one that gave up waiting for SQLite's write lock ("database is locked").
    """
    with factory() as session:
        if not setup_is_open(session):
            raise SetupAlreadyCompleted
    clean_display_name = _clean_name(
        display_name, "invalid_display_name", "Display name must contain 1 to 120 characters"
    )
    normalized_email = normalize_email(email)
    validate_password(password, normalized_email, clean_display_name)
    _workspace_options(
        settings,
        base_currency=base_currency,
        country_code=country_code,
        timezone=timezone,
        workspace_locale=workspace_locale,
    )
    password_hash = (hasher or argon2_hash)(password)
    try:
        return run_immediate(
            factory,
            lambda session: create_initial_admin(
                session,
                email=normalized_email,
                display_name=clean_display_name,
                password=password,
                workspace_name=workspace_name,
                settings=settings,
                clock=clock,
                base_currency=base_currency,
                country_code=country_code,
                timezone=timezone,
                workspace_locale=workspace_locale,
                host=host,
                user_agent=user_agent,
                password_hash=password_hash,
            ),
        )
    except OperationalError as exc:
        if is_database_locked(exc):
            raise SetupAlreadyCompleted from exc
        raise


async def run_initial_setup_async(
    factory: sessionmaker[Session],
    gate: HashGate,
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
    hasher = _gate_hasher(gate)
    return await anyio.to_thread.run_sync(
        lambda: run_initial_setup(
            factory,
            email=email,
            display_name=display_name,
            password=password,
            workspace_name=workspace_name,
            settings=settings,
            clock=clock,
            base_currency=base_currency,
            country_code=country_code,
            timezone=timezone,
            workspace_locale=workspace_locale,
            host=host,
            user_agent=user_agent,
            hasher=hasher,
        )
    )


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
    if not settings.allow_registration or setup_is_open(session):
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
