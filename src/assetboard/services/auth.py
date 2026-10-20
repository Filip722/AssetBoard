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
from argon2 import Parameters, PasswordHasher, Type
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
    SetupState,
    User,
    UserSession,
    Workspace,
)
from assetboard.db.repositories import create_owned_workspace
from assetboard.domain.identity import (
    MAX_EMAIL_LENGTH,
    InvalidDisplayName,
    InvalidEmail,
    normalize_display_name,
    normalize_email,
)
from assetboard.services import categories, invitations
from assetboard.services.hash_gate import HashGate, HashGateBusy
from assetboard.services.workspace_options import WorkspaceOptions, validate_workspace_options

ActorType = Literal["user", "cli", "system"]
# ``account``: sign-in attempts per normalized email (whether or not an account has it).
# ``confirm``: password re-checks of a known user (change, confirm, link), per user id.
ThrottleScope = Literal["account", "confirm", "ip", "setup", "register"]
AuthMethod = Literal["password", "oidc", "proxy"]

MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 1024
MAX_NAME_LENGTH = 120
SESSION_TOUCH_INTERVAL = timedelta(minutes=5)
THROTTLE_IDLE_RESET = timedelta(hours=24)

# Test-only seam: an autouse fixture swaps in cheap parameters; production never sets it.
# The production Argon2id parameters, pinned (argon2-cffi's RFC 9106 low-memory profile as of
# 25.1): a library upgrade must not change them silently, because hashes made with other
# parameters would then verify in a different time than the dummy hash. Changing them is a
# deliberate migration (ADR-0012): stored hashes are rehashed at their next sign-in.
PRODUCTION_ARGON2_PARAMETERS = Parameters(
    type=Type.ID,
    version=19,
    salt_len=16,
    hash_len=32,
    time_cost=3,
    memory_cost=65536,
    parallelism=4,
)
# Test-only override; production leaves it ``None``.
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
class Throttled:
    """An attempt refused, unverified, because a throttle bucket blocks until ``until``."""

    until: datetime


@dataclass(frozen=True, slots=True)
class LoginOutcome:
    """A failed login never says whether the account exists.

    Unknown email and wrong password look alike. ``throttled_until`` is set only when the
    attempt was refused by a blocked bucket. The sign-in (``account``) bucket is keyed by the
    normalized email, counted the same way whether or not an account has it, decays with time
    only and is never reset by the owner's activity, so it looks the same for every email
    (ADR-0012, amendment of 2026-10-17).
    """

    user: User | None = None
    session_id: str | None = None
    throttled_until: datetime | None = None

    @property
    def succeeded(self) -> bool:
        return self.user is not None and self.session_id is not None


PasswordChangeStatus = Literal[
    "changed", "invalid_current", "invalid_new", "throttled", "no_password"
]


@dataclass(frozen=True, slots=True)
class PasswordChangeOutcome:
    status: PasswordChangeStatus
    revoked_sessions: int = 0
    error: AuthError | None = None
    throttled_until: datetime | None = None


RegistrationStatus = Literal[
    "created", "invalid", "rejected", "throttled", "invitation_invalid", "email_mismatch"
]


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
    # Invitation sign-up: the workspace joined, and whether a wrong email revoked the link.
    joined_workspace_id: UUID | None = None
    invitation_revoked: bool = False


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


def argon2_parameters() -> Parameters:
    """The parameters in effect: the pinned production ones unless a test overrides them."""
    return ARGON2_PARAMETERS or PRODUCTION_ARGON2_PARAMETERS


def password_hasher() -> PasswordHasher:
    """The one hasher for passwords and the dummy hash, with the pinned parameters."""
    return PasswordHasher.from_parameters(argon2_parameters())


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
    key = repr(argon2_parameters())
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


def blocked_until(
    session: Session, keys: Sequence[tuple[ThrottleScope, str]], now: datetime
) -> datetime | None:
    """The latest time any of these buckets blocks until, or ``None`` if none is blocked.

    Read-only, like ``_find_throttle``. The longest wait is the one that matters.
    """
    until: datetime | None = None
    for scope, key in keys:
        row = _find_throttle(session, scope, key)
        if row is not None and row.blocked_until is not None and row.blocked_until > now:
            until = row.blocked_until if until is None else max(until, row.blocked_until)
    return until


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


def _reset_confirm(session: Session, user_id: UUID, now: datetime) -> None:
    """A correct password re-check: clear the user's ``confirm`` counter, start a new window.

    Only the ``confirm`` bucket is ever reset. It is reachable only by a signed-in user or an
    email the identity provider proved, so its state discloses nothing. The sign-in
    (``account``) bucket is never reset by anything an account's owner does: it decays with
    time only, so it cannot tell an attacker whether an account exists (ADR-0012).

    The new ``window_started_at`` makes any reservation still in flight from before the reset
    a no-op when it is released.
    """
    row = _find_throttle(session, "confirm", _confirm_key(user_id))
    if row is not None:
        row.failure_count = 0
        row.blocked_until = None
        row.window_started_at = now
        row.updated_at = now


def _confirm_key(user_id: UUID) -> str:
    return str(user_id)


def _confirm_slot(user_id: UUID, settings: Settings) -> ThrottleSlot:
    return ThrottleSlot("confirm", _confirm_key(user_id), None, settings.auth_account_free_failures)


def _account_slot(account_key: str, settings: Settings) -> ThrottleSlot:
    """Sign-in attempts for one normalized email: a fixed window, like the address bucket.

    The window is the address bucket's (``auth_ip_window_minutes``, 60 minutes by default):
    after it the count starts again, and nothing else resets it. Every attempt counts,
    successful ones included, so its state never depends on whether a password was right.
    """
    return ThrottleSlot(
        "account",
        account_key,
        timedelta(minutes=settings.auth_ip_window_minutes),
        settings.auth_account_free_failures,
    )


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
    """The user lookup value and the throttle key, normalized once.

    A valid address is normalized and used for both. Anything else cannot match an account
    (stored emails are normalized and valid); it is truncated and keyed as ``invalid:``.
    """
    try:
        normalized = normalize_email(email)
    except InvalidEmail:
        lookup = email.strip().lower()[:MAX_EMAIL_LENGTH]
        return lookup, f"invalid:{lookup}"
    return normalized, normalized


_NO_USER = UUID(int=0)  # matches no account


@dataclass(frozen=True, slots=True)
class _LoginCandidate:
    reserved: tuple[ReservedAttempt, ...]
    user_id: UUID | None
    password_hash: str | None
    is_instance_admin: bool = False


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
    require_instance_admin: bool = False,
    second_factor: str = "",
) -> LoginOutcome:
    """``require_instance_admin`` (break-glass ``/login/local``): a correct password of anyone
    else fails exactly like a wrong one: same response, same counting, no rehash."""
    if len(password) > MAX_PASSWORD_LENGTH or len(email) > MAX_EMAIL_LENGTH * 2:
        # Cannot be a valid credential; refuse before any database work or hashing.
        return LoginOutcome()
    lookup, account_key = _login_keys(email)
    slots = (
        _account_slot(account_key, settings),
        ThrottleSlot(
            "ip",
            _ip_key(host),
            timedelta(minutes=settings.auth_ip_window_minutes),
            settings.auth_ip_free_failures,
        ),
    )

    keys: list[tuple[ThrottleScope, str]] = [(slot.scope, slot.key) for slot in slots]

    def reserve(session: Session) -> _LoginCandidate | Throttled:
        # Blocked attempts are answered from read-only lookups and write nothing.
        now = clock.now()
        reserved = reserve_attempts(session, slots, now, settings)
        if reserved is None:
            return Throttled(blocked_until(session, keys, now) or now)
        user = session.scalar(select(User).where(User.email == lookup))
        return _LoginCandidate(
            reserved,
            user.id if user else None,
            user.password_hash if user else None,
            bool(user and user.is_instance_admin),
        )

    def blocked(session: Session) -> datetime | None:
        return blocked_until(session, keys, clock.now())

    # A deferred read first, so a flood of blocked requests never takes the write lock;
    # ``reserve`` checks again under the lock. Neither looks at the user: a blocked attempt
    # is answered the same way whether or not the account exists.
    until = run.read(blocked)
    if until is not None:
        return LoginOutcome(throttled_until=until)
    candidate = run(reserve)
    if isinstance(candidate, Throttled):
        return LoginOutcome(throttled_until=candidate.until)
    rehashed: str | None = None
    verified: bool | None = None
    try:
        check = verify or verify_password
        verified = check(candidate.password_hash or _dummy_hash(), password)
        # Decided right after verification and hashed before ``finish``: no hashing ever
        # runs inside the write transaction.
        if (
            verified
            and candidate.password_hash is not None
            and (candidate.is_instance_admin or not require_instance_admin)
            and password_hasher().check_needs_rehash(candidate.password_hash)
        ):
            try:
                rehashed = (hasher or argon2_hash)(password)
            except HashGateBusy:
                # Best effort: the login still succeeds; the next login retries the rehash.
                rehashed = None
    except BaseException:
        # Before a verify outcome exists (``HashGateBusy`` from a saturated gate, any error),
        # nothing account-specific has happened: take the whole attempt back. After it (an
        # error while rehashing, which only a correct password reaches), the account attempt
        # stays counted, as it would on success.
        taken_back = [item for item in candidate.reserved if verified is None or item.scope == "ip"]
        run(lambda session: release_attempts(session, taken_back))
        raise

    def finish(session: Session) -> LoginOutcome:
        now = clock.now()
        # The same query whether or not the account exists (never the identity map).
        user = session.scalar(select(User).where(User.id == (candidate.user_id or _NO_USER)))
        not_admin = require_instance_admin and not (user is not None and user.is_instance_admin)
        succeeded = (
            verified
            and user is not None
            and user.is_active
            and user.password_hash is not None
            and user.password_hash == candidate.password_hash
            and not not_admin
        )
        factor_verified = False
        if succeeded and user is not None:
            from assetboard.services.totp import verify_sign_in

            succeeded, factor_verified = verify_sign_in(
                session, user.id, code=second_factor, settings=settings, now=now
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
                # Internal only: the response is the same as for a wrong password.
                data={"reason": "not_instance_admin"} if verified and not_admin else None,
            )
            return LoginOutcome()
        # Every attempt counts in the account bucket, this successful one too: taking it back
        # (or resetting the bucket) would make the outcome of a verify visible to others
        # probing the same email, and only a known email can succeed. The address bucket,
        # keyed by the requester, takes back this request's attempt as before.
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
            session,
            user,
            settings=settings,
            clock=clock,
            host=host,
            user_agent=user_agent,
            second_factor_verified=factor_verified,
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
    second_factor: str = "",
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
        second_factor=second_factor,
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
    require_instance_admin: bool = False,
    second_factor: str = "",
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
        require_instance_admin=require_instance_admin,
        second_factor=second_factor,
    )


def gate_verifier(gate: HashGate) -> Verifier:
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
    require_instance_admin: bool = False,
    second_factor: str = "",
) -> LoginOutcome:
    """Web entry point: database work in a worker thread, hashing through ``gate``.

    Raises ``HashGateBusy`` (after releasing the reserved attempt) when hashing is saturated.
    """
    verify, hasher = gate_verifier(gate), _gate_hasher(gate)
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
            require_instance_admin=require_instance_admin,
            second_factor=second_factor,
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
    auth_method: AuthMethod = "password",
    second_factor_verified: bool = False,
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
            auth_method=auth_method,
            second_factor_verified=second_factor_verified,
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

    def reserve(session: Session) -> Candidate | Throttled:
        user = session.get(User, user_id)
        if user is None:
            raise UnknownUser
        slot = _confirm_slot(user_id, settings)
        now = clock.now()
        reserved = reserve_attempts(session, (slot,), now, settings)
        if reserved is None:
            return Throttled(blocked_until(session, [(slot.scope, slot.key)], now) or now)
        return Candidate(reserved, user.password_hash, user.email, user.display_name)

    def blocked(session: Session) -> Throttled | bool | None:
        user = session.get(User, user_id)
        if user is None:
            raise UnknownUser
        if user.password_hash is None:
            return None
        slot = _confirm_slot(user_id, settings)
        until = blocked_until(session, [(slot.scope, slot.key)], clock.now())
        return Throttled(until) if until is not None else False

    # Deferred read first; ``reserve`` checks again under the write lock.
    is_blocked = run.read(blocked)
    if is_blocked is None:
        # A user without a password (OIDC, F1.3) has nothing to change: no attempt is counted.
        # Setting a first password is the CLI's ``reset-password`` (F1.5 adds a page).
        return PasswordChangeOutcome(status="no_password")
    if isinstance(is_blocked, Throttled):
        return PasswordChangeOutcome(status="throttled", throttled_until=is_blocked.until)
    candidate = run(reserve)
    if isinstance(candidate, Throttled):
        return PasswordChangeOutcome(status="throttled", throttled_until=candidate.until)

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
        if new_password == current_password:
            # Never "changed" to itself; this matters most for a temporary password (F1.5).
            raise PasswordPolicyError(
                "password_unchanged", "The new password must differ from the current one"
            )
        validate_password(new_password, candidate.email, candidate.display_name)
    except PasswordPolicyError as exc:
        run(lambda session: _reset_confirm(session, user_id, clock.now()))
        return PasswordChangeOutcome(status="invalid_new", error=exc)
    try:
        new_hash = (hasher or argon2_hash)(new_password)
    except BaseException:
        run(release)
        raise

    def apply(session: Session) -> PasswordChangeOutcome:
        _reset_confirm(session, user_id, clock.now())
        user = session.get(User, user_id)
        if user is None or user.password_hash != candidate.password_hash:
            # Changed concurrently (another device or the CLI); do not overwrite it.
            return PasswordChangeOutcome(status="invalid_current")
        user.password_hash = new_hash
        user.must_change_password = False  # a temporary password is replaced (F1.5)
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

    Wrong current passwords count against the user's ``confirm`` throttle (not sign-in's).
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
    verify, hasher = gate_verifier(gate), _gate_hasher(gate)
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


def _clean_display_name(value: str) -> str:
    try:
        return normalize_display_name(value)
    except InvalidDisplayName as exc:
        raise InvalidAccountDetails(exc.code, str(exc)) from exc


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


class WorkspaceCreationFailed(RuntimeError):
    """Creating a new account's workspace or its categories failed on a constraint.

    Not an ``IntegrityError``, so it is never mistaken for a duplicate email or identity.
    """


@dataclass(frozen=True, slots=True)
class WorkspaceSeed:
    """Every new workspace gets the suggested categories (pre-release A): their language and
    the actor recorded on ``category.seeded`` (``user`` is the new account itself)."""

    locale: str
    actor_type: ActorType
    now: datetime


def _create_user_records(
    session: Session,
    *,
    email: str,
    display_name: str,
    password: str | None,
    workspace_name: str | None,
    options: WorkspaceOptions | None,
    settings: Settings,
    is_admin: bool,
    password_hash: str | None = None,
    seed: WorkspaceSeed | None = None,
) -> tuple[User, Workspace | None]:
    """Create a user and, when ``options`` is given, a workspace they own, with its categories.

    ``password_hash`` lets the caller hash outside the transaction; the policy is checked here
    either way. ``password=None`` creates a user without a password (OIDC, F1.3). A workspace
    needs ``seed``: it is created with the suggested categories in ``seed.locale``.
    """
    if options is not None and seed is None:
        raise RuntimeError("A new workspace needs its category seed")
    clean_display_name = _clean_display_name(display_name)
    normalized_email = normalize_email(email)
    if password is not None:
        validate_password(password, normalized_email, clean_display_name)
        password_hash = password_hash or argon2_hash(password)
    else:
        password_hash = None
    user = User(
        email=normalized_email,
        display_name=clean_display_name,
        password_hash=password_hash,
        # No language until the user picks one in the profile: the browser's applies (F1.5).
        locale=None,
        is_active=True,
        is_instance_admin=is_admin,
    )
    session.add(user)
    workspace_name_clean = (
        _clean_name(
            workspace_name or clean_display_name,
            "invalid_workspace_name",
            "Workspace name must contain 1 to 120 characters",
        )
        if options is not None
        else None
    )
    session.flush()
    if options is None or seed is None or workspace_name_clean is None:
        return user, None
    try:
        workspace, scope = create_owned_workspace(
            session,
            name=workspace_name_clean,
            base_currency=options.base_currency,
            country_code=options.country_code,
            timezone=options.timezone,
            locale=options.locale,
            owner_user_id=user.id,
        )
        categories.seed_into(
            session,
            scope,
            locale=seed.locale,
            actor_user_id=user.id if seed.actor_type == "user" else None,
            actor_type=seed.actor_type,
            now=seed.now,
            automatic=True,
        )
        session.flush()
    except IntegrityError as exc:
        # Callers read an IntegrityError around this call as "the email (or identity) is
        # taken"; only the user insert above can mean that. Anything else is a bug.
        raise WorkspaceCreationFailed from exc
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
    seed_locale: str | None = None,
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
        seed=WorkspaceSeed(seed_locale or options.locale, "system", clock.now()),
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
    seed_locale: str | None = None,
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
    clean_display_name = _clean_display_name(display_name)
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
                seed_locale=seed_locale,
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
    seed_locale: str | None = None,
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
            seed_locale=seed_locale,
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
    seed_locale: str | None = None,
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
        seed=WorkspaceSeed(seed_locale or options.locale, "cli", clock.now())
        if options is not None
        else None,
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


def _registration_blocked(session: Session, key: str, settings: Settings, clock: Clock) -> bool:
    if setup_is_open(session):
        raise RegistrationDisabled
    row = _find_throttle(session, "register", key)
    window = timedelta(minutes=settings.registration_window_minutes)
    return _current_count(row, clock.now(), window) >= settings.registration_ip_limit


def _reserve_registration(
    session: Session, key: str, settings: Settings, clock: Clock
) -> tuple[ReservedAttempt, ...] | None:
    if _registration_blocked(session, key, settings, clock):
        return None
    window = timedelta(minutes=settings.registration_window_minutes)
    row = _count_attempt(
        session, "register", key, now=clock.now(), window=window, free=None, settings=settings
    )
    prune_auth_throttles(session, clock)
    return (ReservedAttempt("register", key, row.window_started_at, None),)


def reserve_registration_attempt(
    factory: sessionmaker[Session], *, host: str | None, settings: Settings, clock: Clock
) -> bool:
    """Count one account creation against the per-address registration limit.

    For account creation outside ``register_user`` (OIDC, F1.3). False when the limit is
    reached; raises ``RegistrationDisabled`` while first-run setup is open. The attempt stays
    counted whatever happens next, as for registration.
    """
    run = ImmediateTransactions(factory)
    key = _ip_key(host)
    if run.read(lambda session: _registration_blocked(session, key, settings, clock)):
        return False
    return run(lambda session: _reserve_registration(session, key, settings, clock)) is not None


@dataclass(frozen=True, slots=True)
class _RegistrationInput:
    email: str
    display_name: str
    options: WorkspaceOptions | None


@dataclass(frozen=True, slots=True)
class InvitationSignUp:
    """Sign-up through an invitation link: no own workspace; joins the invitation's instead."""

    factory: sessionmaker[Session]
    token: str
    clock: Clock

    def check(self, email: str) -> None:
        invitations.check_for_sign_up(self.factory, self.token, email=email, clock=self.clock)

    def consume(self, session: Session, user_id: UUID, email: str) -> UUID:
        return invitations.consume_for_new_user(
            session, self.token, user_id=user_id, email=email, now=self.clock.now()
        )


def _register_flow(
    run: TransactionRunner,
    *,
    email: str,
    display_name: str,
    password: str,
    workspace_name: str,
    settings: Settings,
    clock: Clock,
    host: str | None,
    user_agent: str | None,
    base_currency: str | None,
    country_code: str | None,
    timezone: str | None,
    workspace_locale: str | None,
    seed_locale: str | None,
    hasher: Hasher | None,
    invitation: InvitationSignUp | None = None,
) -> RegistrationOutcome:
    # A valid invitation allows sign-up even when open registration is off (ADR-0013).
    if invitation is None and not settings.allow_registration:
        raise RegistrationDisabled
    key = _ip_key(host)

    def throttled(session: Session) -> bool:
        return _registration_blocked(session, key, settings, clock)

    def reserve(session: Session) -> tuple[ReservedAttempt, ...] | None:
        return _reserve_registration(session, key, settings, clock)

    # A deferred read first; ``reserve`` checks again under the write lock.
    if run.read(throttled):
        return RegistrationOutcome(status="throttled")
    reserved = run(reserve)
    if reserved is None:
        return RegistrationOutcome(status="throttled")

    try:
        normalized_email = normalize_email(email)
        clean_display_name = _clean_display_name(display_name)
        validate_password(password, normalized_email, clean_display_name)
        options: WorkspaceOptions | None = None
        if invitation is None:
            _clean_name(
                workspace_name or clean_display_name,
                "invalid_workspace_name",
                "Workspace name must contain 1 to 120 characters",
            )
            options = _workspace_options(
                settings,
                base_currency=base_currency,
                country_code=country_code,
                timezone=timezone,
                workspace_locale=workspace_locale,
            )
        details = _RegistrationInput(normalized_email, clean_display_name, options)
    except (AuthError, InvalidEmail) as exc:
        # The attempt stays counted; invalid input costs no hashing.
        error = exc if isinstance(exc, AuthError) else InvalidAccountDetails(exc.code, str(exc))
        return RegistrationOutcome(status="invalid", error=error)

    if invitation is not None:
        # Before hashing: the invitation is pending and its email lock matches (a mismatch is
        # counted and may revoke it).
        try:
            invitation.check(details.email)
        except invitations.EmailMismatch as exc:
            return RegistrationOutcome(status="email_mismatch", invitation_revoked=exc.revoked)
        except invitations.InvitationInvalid:
            return RegistrationOutcome(status="invitation_invalid")

    try:
        # Hashed before the duplicate check, so an existing email takes the same time.
        password_hash = (hasher or argon2_hash)(password)
    except BaseException:
        run(lambda session: release_attempts(session, reserved))
        raise

    def create(session: Session) -> RegistrationOutcome:
        if session.scalar(select(User.id).where(User.email == details.email)) is not None:
            return RegistrationOutcome(status="rejected")
        try:
            with session.begin_nested():
                user, workspace = _create_user_records(
                    session,
                    email=details.email,
                    display_name=details.display_name,
                    password=password,
                    workspace_name=workspace_name,
                    options=details.options,
                    settings=settings,
                    is_admin=False,
                    password_hash=password_hash,
                    seed=WorkspaceSeed(seed_locale or details.options.locale, "user", clock.now())
                    if details.options is not None
                    else None,
                )
        except IntegrityError:
            # Defence in depth: a concurrent registration of the same email won the insert.
            return RegistrationOutcome(status="rejected")
        joined: UUID | None = None
        if invitation is not None:
            # Raises InvitationInvalid (rolling the user back) if the link was used meanwhile.
            joined = invitation.consume(session, user.id, details.email)
        elif workspace is None:
            raise RuntimeError("Registration must create a workspace")
        _audit(
            session,
            clock,
            action="auth.register",
            entity_type="user",
            entity_id=str(user.id),
            actor_type="user",
            actor_user_id=user.id,
            workspace_id=workspace.id if workspace is not None else None,
            host=host,
            user_agent=user_agent,
            data={"via": "invitation"} if invitation is not None else None,
        )
        session.flush()
        return RegistrationOutcome(
            status="created", user=user, workspace=workspace, joined_workspace_id=joined
        )

    try:
        return run(create)
    except invitations.InvitationInvalid:
        return RegistrationOutcome(status="invitation_invalid")


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
    seed_locale: str | None = None,
) -> RegistrationOutcome:
    """Open registration; every attempt counts against its own per-IP window.

    Registration never touches the login throttle, so it cannot be used to clear it. The
    outcome discloses whether an email is already registered (see ``RegistrationOutcome``).
    The caller commits whatever the outcome.
    """
    return _register_flow(
        SameSession(session),
        email=email,
        display_name=display_name,
        password=password,
        workspace_name=workspace_name,
        settings=settings,
        clock=clock,
        host=host,
        user_agent=user_agent,
        base_currency=base_currency,
        country_code=country_code,
        timezone=timezone,
        workspace_locale=workspace_locale,
        seed_locale=seed_locale,
        hasher=None,
    )


def register_with_factory(
    factory: sessionmaker[Session],
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
    seed_locale: str | None = None,
    hasher: Hasher | None = None,
) -> RegistrationOutcome:
    return _register_flow(
        ImmediateTransactions(factory),
        email=email,
        display_name=display_name,
        password=password,
        workspace_name=workspace_name,
        settings=settings,
        clock=clock,
        host=host,
        user_agent=user_agent,
        base_currency=base_currency,
        country_code=country_code,
        timezone=timezone,
        workspace_locale=workspace_locale,
        seed_locale=seed_locale,
        hasher=hasher,
    )


async def register_user_async(
    factory: sessionmaker[Session],
    gate: HashGate,
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
    seed_locale: str | None = None,
) -> RegistrationOutcome:
    """Web entry point; raises ``HashGateBusy`` after releasing the counted attempt."""
    hasher = _gate_hasher(gate)
    return await anyio.to_thread.run_sync(
        lambda: register_with_factory(
            factory,
            email=email,
            display_name=display_name,
            password=password,
            workspace_name=workspace_name,
            settings=settings,
            clock=clock,
            host=host,
            user_agent=user_agent,
            base_currency=base_currency,
            country_code=country_code,
            timezone=timezone,
            workspace_locale=workspace_locale,
            seed_locale=seed_locale,
            hasher=hasher,
        )
    )


def register_with_invitation(
    factory: sessionmaker[Session],
    *,
    token: str,
    email: str,
    display_name: str,
    password: str,
    settings: Settings,
    clock: Clock,
    host: str | None,
    user_agent: str | None = None,
    hasher: Hasher | None = None,
) -> RegistrationOutcome:
    """Invitation sign-up (allowed even when open registration is off); same throttle and gate."""
    return _register_flow(
        ImmediateTransactions(factory),
        email=email,
        display_name=display_name,
        password=password,
        workspace_name="",
        settings=settings,
        clock=clock,
        host=host,
        user_agent=user_agent,
        base_currency=None,
        country_code=None,
        timezone=None,
        workspace_locale=None,
        seed_locale=None,  # an invitation sign-up never creates a workspace
        hasher=hasher,
        invitation=InvitationSignUp(factory, token, clock),
    )


async def register_with_invitation_async(
    factory: sessionmaker[Session],
    gate: HashGate,
    *,
    token: str,
    email: str,
    display_name: str,
    password: str,
    settings: Settings,
    clock: Clock,
    host: str | None,
    user_agent: str | None = None,
) -> RegistrationOutcome:
    """Web entry point; raises ``HashGateBusy`` after releasing the counted attempt."""
    hasher = _gate_hasher(gate)
    return await anyio.to_thread.run_sync(
        lambda: register_with_invitation(
            factory,
            token=token,
            email=email,
            display_name=display_name,
            password=password,
            settings=settings,
            clock=clock,
            host=host,
            user_agent=user_agent,
            hasher=hasher,
        )
    )


# Building blocks for other sign-in methods (OIDC, F1.3)


def record_auth_event(
    session: Session,
    clock: Clock,
    *,
    action: str,
    entity_id: str | None,
    actor_user_id: UUID | None = None,
    host: str | None = None,
    user_agent: str | None = None,
    data: dict[str, str | int | bool] | None = None,
    entity_type: str = "user",
) -> None:
    """An instance-level event (``workspace_id`` NULL), about a user unless ``entity_type``
    says otherwise (``instance`` for instance settings)."""
    _audit(
        session,
        clock,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        actor_type="user" if actor_user_id else "system",
        actor_user_id=actor_user_id,
        host=host,
        user_agent=user_agent,
        data=data,
    )


def default_workspace_options(settings: Settings) -> WorkspaceOptions:
    """The instance defaults, for a workspace created without a form."""
    return _workspace_options(
        settings, base_currency=None, country_code=None, timezone=None, workspace_locale=None
    )


def create_user_with_temporary_password(
    session: Session,
    *,
    email: str,
    display_name: str,
    password: str,
    password_hash: str,
    settings: Settings,
) -> User:
    """An account an administrator creates (F1.5): no workspace, ``must_change_password`` set.

    The password is hashed by the caller, outside the write transaction; the policy and the
    display-name rule are checked here like for any other account.
    """
    user, _workspace = _create_user_records(
        session,
        email=email,
        display_name=display_name,
        password=password,
        workspace_name=None,
        options=None,
        settings=settings,
        is_admin=False,
        password_hash=password_hash,
    )
    user.must_change_password = True
    return user


def create_passwordless_user(
    session: Session,
    *,
    email: str,
    display_name: str,
    own_workspace: bool,
    settings: Settings,
    is_admin: bool,
    clock: Clock,
    seed_locale: str | None = None,
) -> tuple[User, Workspace | None]:
    """A user who signs in elsewhere (OIDC, proxy): no password, optionally an own workspace.

    The own workspace gets the suggested categories named in ``seed_locale`` (the language of
    the sign-in request), else in the workspace's default locale.
    """
    options = default_workspace_options(settings) if own_workspace else None
    return _create_user_records(
        session,
        email=email,
        display_name=display_name,
        password=None,
        workspace_name=None,
        options=options,
        settings=settings,
        is_admin=is_admin,
        seed=WorkspaceSeed(seed_locale or options.locale, "user", clock.now())
        if options is not None
        else None,
    )


def user_has_password(factory: sessionmaker[Session], user_id: UUID) -> bool:
    """False for accounts that sign in elsewhere (OIDC) and never had a password."""
    with factory() as session:
        user = session.get(User, user_id)
        return user is not None and user.password_hash is not None


def revoke_all_sessions(session: Session, user_id: UUID) -> int:
    return _delete_sessions_except(session, user_id, None)


@dataclass(frozen=True, slots=True)
class _ConfirmCandidate:
    reserved: tuple[ReservedAttempt, ...]
    email: str
    password_hash: str | None


def _confirm_password_flow[T](
    run: TransactionRunner,
    *,
    user_id: UUID,
    password: str,
    purpose: str,
    settings: Settings,
    clock: Clock,
    host: str | None,
    user_agent: str | None,
    verify: Verifier | None,
    on_success: Callable[[Session, User], T],
    second_factor: str | None = None,
) -> T | Throttled | None:
    if len(password) > MAX_PASSWORD_LENGTH:
        return None

    # The user's own ``confirm`` bucket (never the sign-in bucket of their email) and the
    # client address.
    slots = (
        _confirm_slot(user_id, settings),
        ThrottleSlot(
            "ip",
            _ip_key(host),
            timedelta(minutes=settings.auth_ip_window_minutes),
            settings.auth_ip_free_failures,
        ),
    )
    keys: list[tuple[ThrottleScope, str]] = [(slot.scope, slot.key) for slot in slots]

    def blocked(session: Session) -> Throttled | bool:
        user = session.get(User, user_id)
        if user is None:
            return True
        until = blocked_until(session, keys, clock.now())
        return Throttled(until) if until is not None else False

    def reserve(session: Session) -> _ConfirmCandidate | Throttled | None:
        user = session.get(User, user_id)
        if user is None:
            return None
        now = clock.now()
        reserved = reserve_attempts(session, slots, now, settings)
        if reserved is None:
            return Throttled(blocked_until(session, keys, now) or now)
        return _ConfirmCandidate(reserved, user.email, user.password_hash)

    # As for login: a deferred read first, then the reservation under the write lock. The
    # account is already known here (a signed-in user, or one the identity provider named),
    # so saying that it is throttled discloses nothing.
    is_blocked = run.read(blocked)
    if isinstance(is_blocked, Throttled):
        return is_blocked
    if is_blocked:
        return None
    candidate = run(reserve)
    if candidate is None or isinstance(candidate, Throttled):
        return candidate
    try:
        # A user without a password costs the same dummy verify and fails like a wrong one.
        verified = (verify or verify_password)(candidate.password_hash or _dummy_hash(), password)
    except BaseException:
        run(lambda session: release_attempts(session, candidate.reserved))
        raise

    def finish(session: Session) -> T | None:
        user = session.get(User, user_id)
        factor_ok = True
        if (
            verified
            and user is not None
            and user.is_active
            and user.password_hash is not None
            and user.password_hash == candidate.password_hash
            and second_factor is not None
        ):
            from assetboard.services.totp import verify_sign_in

            factor_ok, _ = verify_sign_in(
                session, user_id, code=second_factor, settings=settings, now=clock.now()
            )
        if (
            not factor_ok
            or not verified
            or user is None
            or not user.is_active
            or user.password_hash is None
            or user.password_hash != candidate.password_hash
        ):
            # The reservation stays: it is this failure's count.
            prune_auth_throttles(session, clock)
            _audit(
                session,
                clock,
                action="auth.password_confirm_failure",
                entity_type="user",
                entity_id=str(user_id),
                actor_type="user",
                actor_user_id=user.id if user else None,
                host=host,
                user_agent=user_agent,
                data={"purpose": purpose},
            )
            return None
        _reset_confirm(session, user_id, clock.now())
        release_attempts(session, [item for item in candidate.reserved if item.scope == "ip"])
        return on_success(session, user)

    return run(finish)


def confirm_password_with_factory[T](
    factory: sessionmaker[Session],
    *,
    user_id: UUID,
    password: str,
    purpose: str,
    settings: Settings,
    clock: Clock,
    on_success: Callable[[Session, User], T],
    host: str | None = None,
    user_agent: str | None = None,
    verify: Verifier | None = None,
    second_factor: str | None = None,
) -> T | Throttled | None:
    """Re-check a known user's password, then run ``on_success`` in the same transaction.

    Used to link an OIDC identity to an account that has a password (F1.3) and for F1.5's
    sensitive profile actions. It uses the user's own ``confirm`` bucket (shared with password
    change, never the sign-in bucket of the email) and login's address bucket and dummy
    verify. A refused attempt returns ``Throttled``; every other failure is the same ``None``:
    wrong password, no password, inactive user, or a password changed meanwhile.
    """
    return _confirm_password_flow(
        ImmediateTransactions(factory),
        user_id=user_id,
        password=password,
        purpose=purpose,
        settings=settings,
        clock=clock,
        host=host,
        user_agent=user_agent,
        verify=verify,
        on_success=on_success,
        second_factor=second_factor,
    )


async def confirm_password_async[T](
    factory: sessionmaker[Session],
    gate: HashGate,
    *,
    user_id: UUID,
    password: str,
    purpose: str,
    settings: Settings,
    clock: Clock,
    on_success: Callable[[Session, User], T],
    host: str | None = None,
    user_agent: str | None = None,
    second_factor: str | None = None,
) -> T | Throttled | None:
    """Web entry point: verifies through the hashing limit (``HashGateBusy`` when saturated)."""
    verify = gate_verifier(gate)
    return await anyio.to_thread.run_sync(
        lambda: confirm_password_with_factory(
            factory,
            user_id=user_id,
            password=password,
            purpose=purpose,
            settings=settings,
            clock=clock,
            on_success=on_success,
            host=host,
            user_agent=user_agent,
            verify=verify,
            second_factor=second_factor,
        )
    )


# Web sessions


@dataclass(frozen=True, slots=True)
class WebSession:
    """What a request needs to know about its signed-in user (detached from the ORM)."""

    session_hash: str
    user_id: UUID
    email: str
    display_name: str
    locale: str | None
    is_instance_admin: bool
    expires_at: datetime
    needs_touch: bool
    current_workspace_id: UUID | None = None
    auth_method: str = "password"
    theme: str = "system"
    must_change_password: bool = False
    second_factor_verified: bool = False


def read_web_session(
    session: Session, session_id: str, *, settings: Settings, clock: Clock
) -> WebSession | None:
    """Validate a session cookie without writing (idle and absolute expiry, active user)."""
    if not session_id or len(session_id) > 128:
        return None
    row = session.get(UserSession, session_id_hash(session_id))
    if row is None:
        return None
    now = clock.now()
    idle_expiry = row.last_seen_at + timedelta(days=settings.session_idle_timeout_days)
    user = session.get(User, row.user_id)
    if row.expires_at <= now or idle_expiry <= now or user is None or not user.is_active:
        return None
    return WebSession(
        session_hash=row.id_hash,
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        locale=user.locale,
        is_instance_admin=user.is_instance_admin,
        expires_at=row.expires_at,
        needs_touch=now - row.last_seen_at >= SESSION_TOUCH_INTERVAL,
        current_workspace_id=row.current_workspace_id,
        auth_method=row.auth_method,
        theme=user.theme,
        must_change_password=user.must_change_password,
        second_factor_verified=row.second_factor_verified,
    )


def touch_session_best_effort(
    factory: sessionmaker[Session], session_hash: str, clock: Clock
) -> bool:
    """Record activity; never fails the request (a locked database just skips it)."""
    try:
        with factory.begin() as session:
            row = session.get(UserSession, session_hash)
            if row is not None:
                touch_session(row, clock)
    except OperationalError:
        return False
    return True


def create_session_for(
    factory: sessionmaker[Session],
    user_id: UUID,
    *,
    settings: Settings,
    clock: Clock,
    host: str | None,
    user_agent: str | None,
) -> tuple[str, datetime]:
    """Sign a user in after setup or registration; returns the session id and its expiry."""

    def create(session: Session) -> tuple[str, datetime]:
        user = session.get(User, user_id)
        if user is None:
            raise UnknownUser
        session_id = create_user_session(
            session, user, settings=settings, clock=clock, host=host, user_agent=user_agent
        )
        row = session.get(UserSession, session_id_hash(session_id))
        if row is None:
            raise RuntimeError("Session was not created")
        return session_id, row.expires_at

    return run_immediate(factory, create)


def session_expiry(factory: sessionmaker[Session], session_id: str) -> datetime | None:
    with factory() as session:
        row = session.get(UserSession, session_id_hash(session_id))
        return row.expires_at if row else None


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
