from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta
from threading import Event, Thread
from uuid import uuid4

import pytest
from argon2 import extract_parameters, profiles
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from assetboard.clock import FixedClock
from assetboard.config import Settings
from assetboard.db.base import Base
from assetboard.db.engine import create_database_engine
from assetboard.db.migrate import upgrade_database
from assetboard.db.models.identity import (
    AuditEvent,
    AuthThrottle,
    Membership,
    SetupState,
    User,
    UserSession,
    Workspace,
)
from assetboard.domain.identity import InvalidDisplayName, InvalidEmail, normalize_display_name
from assetboard.services import auth
from assetboard.services.auth import (
    EmailAlreadyRegistered,
    InvalidAccountDetails,
    LoginOutcome,
    PasswordChangeOutcome,
    PasswordPolicyError,
    RegistrationDisabled,
    SetupAlreadyCompleted,
    UnknownUser,
    authenticate_user,
    change_password,
    client_ip_prefix,
    common_passwords,
    create_admin_cli,
    create_initial_admin,
    create_user_session,
    derive_subkey,
    get_valid_session,
    hash_password,
    normalize_client_host,
    normalize_email,
    prune_auth_throttles,
    register_user,
    reset_user_password,
    revoke_other_sessions,
    revoke_session,
    session_id_hash,
    setup_token_at_startup,
    touch_session,
    validate_password,
    verify_setup_token,
)

PASSWORD = "Correct Horse Battery!"
BASE_TIME = datetime.fromisoformat("2026-01-01T00:00:00+00:00")


def at(**delta: float) -> FixedClock:
    return FixedClock((BASE_TIME + timedelta(**delta)).isoformat())


@pytest.fixture
def auth_settings() -> Settings:
    return Settings(_env_file=None, secret_key="test-master-key")


@pytest.fixture
def registration_settings() -> Settings:
    return Settings(_env_file=None, allow_registration=True)


def _create_user(
    db_session: Session,
    settings: Settings,
    clock: FixedClock,
    *,
    email: str = "test@example.com",
    name: str = "Test User",
    password: str = PASSWORD,
) -> User:
    user, _workspace = create_admin_cli(
        db_session,
        email=email,
        display_name=name,
        password=password,
        workspace_name="Home",
        settings=settings,
        clock=clock,
    )
    return user


def _login(
    db_session: Session,
    settings: Settings,
    clock: FixedClock,
    *,
    email: str = "test@example.com",
    password: str = "wrong password",
    host: str | None = "192.0.2.10",
    existing_session_id: str | None = None,
) -> LoginOutcome:
    return authenticate_user(
        db_session,
        email=email,
        password=password,
        settings=settings,
        clock=clock,
        host=host,
        existing_session_id=existing_session_id,
    )


def _register(
    db_session: Session,
    settings: Settings,
    clock: FixedClock,
    *,
    email: str = "reg@example.com",
    password: str = "Registrant Strong Password",
    host: str = "192.0.2.95",
) -> auth.RegistrationOutcome:
    auth._claim_setup(db_session, clock)  # pyright: ignore[reportPrivateUsage]
    return register_user(
        db_session,
        email=email,
        display_name="Registrant",
        password=password,
        workspace_name="My budget",
        settings=settings,
        clock=clock,
        host=host,
    )


def _throttle(db_session: Session, scope: str, key: str) -> AuthThrottle | None:
    return db_session.scalar(
        select(AuthThrottle).where(
            AuthThrottle.key_type == scope,
            AuthThrottle.key_hash == hashlib.sha256(key.encode()).hexdigest(),
        )
    )


# Emails


def test_email_is_normalized_and_validated() -> None:
    assert normalize_email("  ALICE.Example@Example.COM ") == "alice.example@example.com"
    for invalid in ("", "a", "not an email", "alice@localhost", "@example.com", "x" * 321):
        with pytest.raises(InvalidEmail) as error:
            normalize_email(invalid)
        assert error.value.code == "invalid_email"


def test_normalized_email_has_database_uniqueness(
    db_session: Session, auth_settings: Settings, fixed_clock: FixedClock
) -> None:
    _create_user(db_session, auth_settings, fixed_clock)
    db_session.add(User(email=" TEST@example.com", display_name="Other", password_hash=None))
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_user_model_normalizes_email_on_assignment() -> None:
    user = User(email="  ALICE@EXAMPLE.COM ", display_name="Alice")
    assert user.email == "alice@example.com"


# Password policy and hashing


@pytest.mark.parametrize(
    ("password", "email", "display_name", "code"),
    [
        ("short", "alice@example.com", "Alice", "password_too_short"),
        ("123456789", "alice@example.com", "Alice", "password_too_short"),
        ("x" * 1025, "alice@example.com", "Alice", "password_too_long"),
        ("QWERTYUIOP", "alice@example.com", "Alice", "password_too_common"),
        ("BasketBall", "alice@example.com", "Alice", "password_too_common"),
        ("a very long name", "bob@example.com", "A Very Long Name", "password_matches_identity"),
        (
            "VeryLongLocalPart",
            "verylonglocalpart@example.com",
            "Other",
            "password_matches_identity",
        ),
    ],
)
def test_password_policy_rejects_short_common_or_identity_passwords(
    password: str, email: str, display_name: str, code: str
) -> None:
    with pytest.raises(PasswordPolicyError) as error:
        validate_password(password, email, display_name)
    assert error.value.code == code


@pytest.mark.parametrize("password", ["abcdefghij", "no composition rules apply", "x" * 1024])
def test_password_policy_accepts_ten_characters_without_composition_rules(password: str) -> None:
    validate_password(password, "alice@example.com", "Alice")


def test_password_hash_is_argon2id_and_common_list_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert hash_password(PASSWORD, "alice@example.com", "Alice").startswith("$argon2id$")
    monkeypatch.setattr(auth, "ARGON2_PARAMETERS", None)
    production = hash_password(PASSWORD, "alice@example.com", "Alice")
    assert production.startswith("$argon2id$")
    assert extract_parameters(production) == profiles.get_default_parameters()
    assert len(common_passwords()) == 10000
    assert "password" in common_passwords()


def test_production_hasher_uses_library_defaults_and_rehashes_cheap_hashes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    encoded = hash_password(PASSWORD, "alice@example.com", "Alice")
    assert extract_parameters(encoded) == profiles.CHEAPEST  # autouse test seam
    assert auth.verify_password(encoded, PASSWORD)
    assert not auth.verify_password(encoded, "incorrect password")
    assert not auth.verify_password("not-a-hash", PASSWORD)
    assert not auth.password_hasher().check_needs_rehash(encoded)
    monkeypatch.setattr(auth, "ARGON2_PARAMETERS", None)
    assert profiles.get_default_parameters() == profiles.RFC_9106_LOW_MEMORY
    assert extract_parameters(auth.password_hasher().hash(PASSWORD)) == (
        profiles.get_default_parameters()
    )
    assert auth.password_hasher().check_needs_rehash(encoded)


# Sessions


def test_session_secret_is_random_and_only_hash_is_persisted(
    db_session: Session, auth_settings: Settings, fixed_clock: FixedClock
) -> None:
    user = _create_user(db_session, auth_settings, fixed_clock)
    clear_id = create_user_session(
        db_session,
        user,
        settings=auth_settings,
        clock=fixed_clock,
        host="192.0.2.45",
        user_agent="U" * 250,
    )
    other_id = create_user_session(
        db_session, user, settings=auth_settings, clock=fixed_clock, host=None
    )
    row = db_session.get(UserSession, session_id_hash(clear_id))
    assert row is not None
    assert clear_id != other_id
    assert len(clear_id) >= 43
    assert row.id_hash == hashlib.sha256(clear_id.encode()).hexdigest()
    assert db_session.scalar(select(UserSession).where(UserSession.id_hash == clear_id)) is None
    assert row.ip_prefix == "192.0.2.0/24"
    assert row.user_agent is not None and len(row.user_agent) == 200
    assert row.expires_at == fixed_clock.now() + timedelta(days=60)


@pytest.mark.parametrize(
    ("idle_days", "absolute_days", "touches", "check_at", "valid"),
    [
        (14, 60, [], timedelta(days=14) - timedelta(seconds=1), True),
        (14, 60, [], timedelta(days=14), False),
        (14, 60, [timedelta(days=13)], timedelta(days=26), True),
        (14, 60, [timedelta(days=d) for d in range(10, 60, 10)], timedelta(days=60), False),
        (1, 2, [], timedelta(days=1), False),
        (1, 2, [timedelta(hours=23)], timedelta(days=2), False),
    ],
)
def test_idle_and_absolute_expiry_use_injected_clock_and_settings(
    db_session: Session,
    fixed_clock: FixedClock,
    idle_days: int,
    absolute_days: int,
    touches: list[timedelta],
    check_at: timedelta,
    valid: bool,
) -> None:
    settings = Settings(
        _env_file=None,
        session_idle_timeout_days=idle_days,
        session_absolute_timeout_days=absolute_days,
    )
    user = _create_user(db_session, settings, fixed_clock)
    session_id = create_user_session(
        db_session, user, settings=settings, clock=fixed_clock, host=None
    )
    for touch in touches:
        touch_clock = at(seconds=touch.total_seconds())
        row = get_valid_session(db_session, session_id, settings=settings, clock=touch_clock)
        assert row is not None
        touch_session(row, touch_clock)
    result = get_valid_session(
        db_session, session_id, settings=settings, clock=at(seconds=check_at.total_seconds())
    )
    assert (result is not None) is valid
    if not valid:
        db_session.flush()
        assert db_session.get(UserSession, session_id_hash(session_id)) is None


def test_session_of_deactivated_user_is_invalid(
    db_session: Session, auth_settings: Settings, fixed_clock: FixedClock
) -> None:
    user = _create_user(db_session, auth_settings, fixed_clock)
    session_id = create_user_session(
        db_session, user, settings=auth_settings, clock=fixed_clock, host=None
    )
    user.is_active = False
    assert (
        get_valid_session(db_session, session_id, settings=auth_settings, clock=fixed_clock) is None
    )


def test_session_activity_is_written_no_more_than_every_five_minutes(
    db_session: Session, auth_settings: Settings, fixed_clock: FixedClock
) -> None:
    user = _create_user(db_session, auth_settings, fixed_clock)
    clear_id = create_user_session(
        db_session, user, settings=auth_settings, clock=fixed_clock, host=None
    )
    row = db_session.get(UserSession, session_id_hash(clear_id))
    assert row is not None
    assert touch_session(row, at(minutes=4, seconds=59)) is False
    assert row.last_seen_at == fixed_clock.now()
    assert touch_session(row, at(minutes=5)) is True
    assert row.last_seen_at == at(minutes=5).now()
    assert touch_session(row, at(minutes=9)) is False


# Login


def test_login_rotates_session_rehashes_and_audits_success(
    db_session: Session,
    auth_settings: Settings,
    fixed_clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _create_user(db_session, auth_settings, fixed_clock)
    old_id = create_user_session(
        db_session, user, settings=auth_settings, clock=fixed_clock, host=None
    )
    old_hash = user.password_hash
    real_hasher = auth.password_hasher()

    class RehashingHasher:
        def verify(self, encoded: str, password: str) -> bool:
            return real_hasher.verify(encoded, password)

        def check_needs_rehash(self, _encoded: str) -> bool:
            return True

        def hash(self, password: str) -> str:
            return real_hasher.hash(password)

    monkeypatch.setattr(auth, "password_hasher", lambda: RehashingHasher())
    outcome = _login(
        db_session,
        auth_settings,
        fixed_clock,
        email=" TEST@example.com ",
        password=PASSWORD,
        existing_session_id=old_id,
    )
    assert outcome.succeeded and outcome.user is user and outcome.session_id is not None
    assert outcome.session_id != old_id
    assert db_session.get(UserSession, session_id_hash(old_id)) is None
    assert db_session.get(UserSession, session_id_hash(outcome.session_id)) is not None
    assert user.last_login_at == fixed_clock.now()
    assert user.password_hash != old_hash
    assert user.password_hash is not None and user.password_hash.startswith("$argon2id$")
    actions = db_session.scalars(select(AuditEvent.action)).all()
    assert actions.count("auth.login_success") == 1


def test_unknown_email_and_wrong_password_are_indistinguishable(
    db_session: Session, auth_settings: Settings, fixed_clock: FixedClock
) -> None:
    _create_user(db_session, auth_settings, fixed_clock)
    outcomes: list[LoginOutcome] = []
    for email in ("test@example.com", "unknown@example.com"):
        for _ in range(auth_settings.auth_account_free_failures + 2):
            outcomes.append(_login(db_session, auth_settings, fixed_clock, email=email))
    assert outcomes == [LoginOutcome()] * len(outcomes)
    known = _throttle(db_session, "account", "test@example.com")
    unknown = _throttle(db_session, "account", "unknown@example.com")
    assert known is not None and unknown is not None
    assert (known.failure_count, known.blocked_until) == (
        unknown.failure_count,
        unknown.blocked_until,
    )
    assert known.blocked_until is not None
    # Stored keys are SHA-256 hashes, never the raw identifier or address.
    stored = " ".join(db_session.scalars(select(AuthThrottle.key_hash)).all())
    assert "example.com" not in stored and "192.0.2" not in stored


def test_unknown_user_and_user_without_password_go_through_dummy_verify(
    db_session: Session,
    auth_settings: Settings,
    fixed_clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _create_user(db_session, auth_settings, fixed_clock)
    calls: list[str] = []
    real_verify = auth.verify_password

    def recording_verify(encoded: str, password: str) -> bool:
        calls.append(encoded)
        return real_verify(encoded, password)

    monkeypatch.setattr(auth, "verify_password", recording_verify)
    _login(db_session, auth_settings, fixed_clock, email="unknown@example.com")
    _login(db_session, auth_settings, fixed_clock, email=user.email)
    user.password_hash = None
    _login(db_session, auth_settings, fixed_clock, email=user.email, password=PASSWORD)
    # One Argon2 verification per attempt, whether or not the account exists.
    assert len(calls) == 3
    assert all(encoded.startswith("$argon2id$") for encoded in calls)
    assert calls[0] == calls[2] != calls[1]


def test_account_backoff_is_exponential_capped_and_configurable(
    db_session: Session, auth_settings: Settings
) -> None:
    delays: list[int | None] = []
    elapsed = 0.0
    for _ in range(11):
        clock = at(seconds=elapsed)
        _login(db_session, auth_settings, clock, email="absent@example.com", host=None)
        row = _throttle(db_session, "account", "absent@example.com")
        assert row is not None
        blocked = row.blocked_until
        delays.append(None if blocked is None else int((blocked - clock.now()).total_seconds()))
        elapsed += (delays[-1] or 0) + 1
    assert delays == [None] * 5 + [60, 120, 240, 480, 900, 900]

    custom = Settings(
        _env_file=None,
        auth_account_free_failures=2,
        auth_throttle_base_seconds=10,
        auth_throttle_max_seconds=30,
    )
    custom_delays: list[int | None] = []
    elapsed = 0.0
    for _ in range(5):
        clock = at(seconds=elapsed)
        _login(db_session, custom, clock, email="custom@example.com", host=None)
        row = _throttle(db_session, "account", "custom@example.com")
        assert row is not None
        blocked = row.blocked_until
        custom_delays.append(
            None if blocked is None else int((blocked - clock.now()).total_seconds())
        )
        elapsed += (custom_delays[-1] or 0) + 1
    assert custom_delays == [None, None, 10, 20, 30]


def test_blocked_account_rejects_correct_password_until_backoff_ends(
    db_session: Session, auth_settings: Settings, fixed_clock: FixedClock
) -> None:
    _create_user(db_session, auth_settings, fixed_clock)
    for _ in range(auth_settings.auth_account_free_failures + 1):
        _login(db_session, auth_settings, fixed_clock)
    row = _throttle(db_session, "account", "test@example.com")
    assert row is not None and row.failure_count == 6
    assert not _login(db_session, auth_settings, at(seconds=59), password=PASSWORD).succeeded
    assert row.failure_count == 6  # Blocked attempts are not counted again.
    assert _login(db_session, auth_settings, at(seconds=60), password=PASSWORD).succeeded


def test_ip_threshold_allows_twenty_distinct_accounts_before_backoff(
    db_session: Session, auth_settings: Settings, fixed_clock: FixedClock
) -> None:
    for index in range(20):
        _login(db_session, auth_settings, fixed_clock, email=f"absent-{index}@example.com")
    ip_row = _throttle(db_session, "ip", "192.0.2.10/32")
    assert ip_row is not None and ip_row.failure_count == 20 and ip_row.blocked_until is None
    _login(db_session, auth_settings, fixed_clock, email="absent-20@example.com")
    assert ip_row.blocked_until == fixed_clock.now() + timedelta(seconds=60)
    # Another address is unaffected.
    _login(db_session, auth_settings, fixed_clock, email="absent-21@example.com", host="192.0.2.11")
    other = _throttle(db_session, "ip", "192.0.2.11/32")
    assert other is not None and other.failure_count == 1


def test_login_success_resets_only_the_account_counter(
    db_session: Session, auth_settings: Settings, fixed_clock: FixedClock
) -> None:
    _create_user(db_session, auth_settings, fixed_clock)
    for _ in range(3):
        _login(db_session, auth_settings, fixed_clock)
    assert _login(db_session, auth_settings, fixed_clock, password=PASSWORD).succeeded
    account = _throttle(db_session, "account", "test@example.com")
    ip_row = _throttle(db_session, "ip", "192.0.2.10/32")
    assert account is not None and account.failure_count == 0 and account.blocked_until is None
    assert ip_row is not None and ip_row.failure_count == 3


def test_ip_counter_decays_only_with_its_time_window(
    db_session: Session, auth_settings: Settings, fixed_clock: FixedClock
) -> None:
    for index in range(3):
        _login(db_session, auth_settings, fixed_clock, email=f"a{index}@example.com")
    ip_row = _throttle(db_session, "ip", "192.0.2.10/32")
    assert ip_row is not None and ip_row.failure_count == 3
    _login(db_session, auth_settings, at(minutes=59), email="b@example.com")
    assert ip_row.failure_count == 4
    _login(db_session, auth_settings, at(minutes=60), email="c@example.com")
    assert ip_row.failure_count == 1
    assert ip_row.window_started_at == at(minutes=60).now()


def test_throttle_pruning_removes_idle_rows_older_than_24_hours(
    db_session: Session, fixed_clock: FixedClock
) -> None:
    def row(key: str, updated: FixedClock, blocked_until: datetime | None = None) -> AuthThrottle:
        return AuthThrottle(
            id=uuid4(),
            key_type="account",
            key_hash=key * 64,
            failure_count=1,
            window_started_at=updated.now(),
            blocked_until=blocked_until,
            updated_at=updated.now(),
        )

    db_session.add_all(
        [
            row("1", at(hours=-24, seconds=-1)),
            row("2", at(hours=-24, seconds=1)),
            row("3", at(hours=-25), blocked_until=at(minutes=5).now()),
        ]
    )
    assert prune_auth_throttles(db_session, fixed_clock) == 1
    assert set(db_session.scalars(select(AuthThrottle.key_hash)).all()) == {"2" * 64, "3" * 64}


def test_login_failures_prune_old_throttle_rows_opportunistically(
    db_session: Session, auth_settings: Settings, fixed_clock: FixedClock
) -> None:
    _login(db_session, auth_settings, fixed_clock, email="stale@example.com", host="192.0.2.1")
    assert _throttle(db_session, "account", "stale@example.com") is not None
    _login(db_session, auth_settings, at(hours=25), email="fresh@example.com", host="192.0.2.2")
    assert _throttle(db_session, "account", "stale@example.com") is None
    assert _throttle(db_session, "account", "fresh@example.com") is not None


@pytest.mark.parametrize(
    ("host", "normal", "throttle"),
    [
        ("192.0.2.91", "192.0.2.0/24", "192.0.2.91/32"),
        ("2001:db8:abcd:12::9", "2001:db8:abcd::/48", "2001:db8:abcd:12::/64"),
        (None, None, None),
        ("not-an-ip", None, None),
    ],
)
def test_ip_prefix_and_throttle_prefixes(
    host: str | None, normal: str | None, throttle: str | None
) -> None:
    assert client_ip_prefix(host) == normal
    assert client_ip_prefix(host, for_throttle=True) == throttle


def test_client_address_uses_one_host_value_and_never_parses_forwarded_chains() -> None:
    assert str(normalize_client_host("192.0.2.91")) == "192.0.2.91"
    assert normalize_client_host("192.0.2.91, 198.51.100.2") is None
    assert normalize_client_host(None) is None


# Setup token


def test_setup_token_comparison_by_behaviour(db_session: Session, fixed_clock: FixedClock) -> None:
    settings = Settings(_env_file=None, setup_token="operator-generated-token-128bits")
    assert settings.setup_token is not None
    expected = settings.setup_token.get_secret_value()

    def check(provided: str) -> bool:
        return verify_setup_token(
            db_session, provided, expected, clock=fixed_clock, settings=settings, host="192.0.2.92"
        )

    assert check(expected)
    assert not check("operator-generated-token-128bitX")  # same length, wrong
    assert not check("operator-generated-token-128bit")  # shorter
    assert not check(expected + "-longer")  # longer
    assert not check("operator-generated-token-128bitś")  # non-ASCII must not crash
    assert not check("")
    row = _throttle(db_session, "setup", "192.0.2.92/32")
    assert row is not None and row.failure_count == 5


def test_setup_token_attempts_are_throttled_per_ip(
    db_session: Session, fixed_clock: FixedClock
) -> None:
    settings = Settings(_env_file=None, setup_token="operator-generated-token-128bits")
    expected = "operator-generated-token-128bits"

    def check(provided: str, clock: FixedClock, host: str = "192.0.2.92") -> bool:
        return verify_setup_token(
            db_session, provided, expected, clock=clock, settings=settings, host=host
        )

    for _ in range(settings.setup_throttle_free_failures + 1):
        assert not check("wrong", fixed_clock)
    assert not check(expected, at(seconds=59))  # blocked even with the right token
    assert check(expected, fixed_clock, host="192.0.2.93")  # other address unaffected
    assert check(expected, at(seconds=60))


def test_startup_setup_token_is_generated_and_logged_once_only_while_setup_is_open(
    auth_settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    logger = logging.getLogger("test.setup.token")
    caplog.set_level(logging.WARNING, logger="test.setup.token")
    assert setup_token_at_startup(auth_settings, setup_open=False, logger=logger) is None
    assert not caplog.records
    generated = setup_token_at_startup(auth_settings, setup_open=True, logger=logger)
    assert generated is not None and len(generated) >= 43  # 32 random bytes, >= 128 bits
    warnings = [r for r in caplog.records if "Generated first-run setup token" in r.message]
    assert len(warnings) == 1 and warnings[0].levelno == logging.WARNING
    assert generated in warnings[0].getMessage()
    # Each process start gets its own token.
    assert setup_token_at_startup(auth_settings, setup_open=True, logger=logger) != generated


def test_configured_setup_token_is_never_logged(caplog: pytest.LogCaptureFixture) -> None:
    logger = logging.getLogger("test.setup.configured")
    caplog.set_level(logging.DEBUG)
    configured = Settings(_env_file=None, setup_token="configured-secret-token-long-enough")
    assert (
        setup_token_at_startup(configured, setup_open=True, logger=logger)
        == "configured-secret-token-long-enough"
    )
    assert setup_token_at_startup(configured, setup_open=False, logger=logger) is None
    assert "configured-secret-token-long-enough" not in caplog.text


def test_configured_setup_token_is_secret_and_minimum_length() -> None:
    configured = Settings(_env_file=None, setup_token="secret-value-that-is-long-enough")
    assert "secret-value-that-is-long-enough" not in repr(configured)
    assert "secret-value-that-is-long-enough" not in configured.model_dump_json()
    with pytest.raises(ValueError, match="SETUP_TOKEN"):
        Settings(_env_file=None, setup_token="too-short")


def test_derived_subkeys_are_purpose_separated_and_not_the_raw_key() -> None:
    assert derive_subkey("secret", "csrf") != derive_subkey("secret", "session")
    assert derive_subkey("secret", "csrf") == derive_subkey("secret", "csrf")
    assert derive_subkey("secret", "csrf") != derive_subkey("other", "csrf")
    assert b"secret" not in derive_subkey("secret", "csrf")
    with pytest.raises(ValueError):
        derive_subkey("secret", "")


# First-run setup


def test_initial_admin_creates_user_workspace_and_owner_membership(
    db_session: Session, auth_settings: Settings, fixed_clock: FixedClock
) -> None:
    user, workspace = create_initial_admin(
        db_session,
        email=" ADMIN@EXAMPLE.COM ",
        display_name="Admin",
        password="Unique Admin Password",
        workspace_name="Household",
        base_currency="pln",
        country_code="pl",
        timezone="Europe/Warsaw",
        workspace_locale="pl",
        settings=auth_settings,
        clock=fixed_clock,
        host="192.0.2.93",
        user_agent="Agent/1",
    )
    assert user.email == "admin@example.com" and user.is_instance_admin
    assert (workspace.name, workspace.base_currency, workspace.country_code) == (
        "Household",
        "PLN",
        "PL",
    )
    assert (workspace.timezone, workspace.locale) == ("Europe/Warsaw", "pl")
    membership = db_session.scalar(select(Membership).where(Membership.user_id == user.id))
    assert membership is not None
    assert (membership.role, membership.workspace_id) == ("owner", workspace.id)
    state = db_session.get(SetupState, 1)
    assert state is not None and state.completed_at == fixed_clock.now()
    with pytest.raises(SetupAlreadyCompleted):
        create_initial_admin(
            db_session,
            email="second@example.com",
            display_name="Second",
            password="Other Strong Password",
            workspace_name="Other",
            settings=auth_settings,
            clock=fixed_clock,
        )


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"password": "short"}, PasswordPolicyError),
        ({"email": "not-an-email"}, InvalidEmail),
        ({"display_name": "  "}, InvalidAccountDetails),
        ({"base_currency": "XYZ"}, InvalidAccountDetails),
        ({"timezone": "Mars/Base"}, InvalidAccountDetails),
        ({"country_code": "QQ"}, InvalidAccountDetails),
        ({"workspace_locale": "de"}, InvalidAccountDetails),
    ],
)
def test_initial_admin_failure_leaves_no_partial_records_and_setup_open(
    database_engine,
    auth_settings: Settings,
    fixed_clock: FixedClock,
    overrides: dict[str, str],
    error: type[Exception],
) -> None:
    values = {
        "email": "admin@example.com",
        "display_name": "Admin",
        "password": "Unique Admin Password",
        "workspace_name": "Home",
        "base_currency": "EUR",
        "country_code": None,
        "timezone": "UTC",
        "workspace_locale": "en",
    } | overrides
    connection = database_engine.connect()
    transaction = connection.begin()
    try:
        session = Session(bind=connection, info={"clock": fixed_clock})
        with pytest.raises(error), session.begin_nested():
            create_initial_admin(
                session,
                settings=auth_settings,
                clock=fixed_clock,
                **values,  # type: ignore[arg-type]
            )
        assert session.scalars(select(User)).all() == []
        assert session.scalars(select(Workspace)).all() == []
        assert session.scalars(select(Membership)).all() == []
        state = session.get(SetupState, 1)
        assert state is not None and state.completed_at is None
        session.close()
    finally:
        transaction.rollback()
        connection.close()


def test_concurrent_setup_has_exactly_one_success_and_deterministic_loser(
    tmp_path, auth_settings: Settings, fixed_clock: FixedClock
) -> None:
    database_url = f"sqlite:///{tmp_path / 'concurrent-setup.db'}"
    upgrade_database(
        auth_settings.model_copy(update={"data_dir": tmp_path, "database_url": database_url})
    )
    engine = create_database_engine(database_url)
    factory = sessionmaker(engine, class_=Session, info={"clock": fixed_clock})
    claimed = Event()
    release_winner = Event()
    outcomes: list[str] = []

    def attempt(email: str) -> None:
        with factory.begin() as session:
            create_initial_admin(
                session,
                email=email,
                display_name="Admin",
                password="Admin Strong Password",
                workspace_name="Home",
                settings=auth_settings,
                clock=fixed_clock,
                host="192.0.2.101",
            )
            if email == "winner@example.com":
                claimed.set()
                assert release_winner.wait(timeout=10)

    def winner() -> None:
        attempt("winner@example.com")
        outcomes.append("success")

    def loser() -> None:
        assert claimed.wait(timeout=10)
        try:
            attempt("loser@example.com")
        except SetupAlreadyCompleted as exc:
            outcomes.append(f"loser:{exc.code}")
        else:
            outcomes.append("unexpected-success")

    threads = [Thread(target=winner), Thread(target=loser)]
    try:
        for thread in threads:
            thread.start()
        assert claimed.wait(timeout=10)
        release_winner.set()
        for thread in threads:
            thread.join(timeout=10)
            assert not thread.is_alive()
        assert sorted(outcomes) == ["loser:setup_completed", "success"]
        with factory() as session:
            assert session.scalars(select(User.email)).all() == ["winner@example.com"]
            assert len(session.scalars(select(Workspace)).all()) == 1
    finally:
        release_winner.set()
        engine.dispose()


# Registration


def test_registration_is_disabled_by_default(
    db_session: Session, auth_settings: Settings, fixed_clock: FixedClock
) -> None:
    with pytest.raises(RegistrationDisabled):
        _register(db_session, auth_settings, fixed_clock)
    assert db_session.scalars(select(AuthThrottle)).all() == []


def test_registration_creates_owner_workspace_and_one_workspace_audit_event(
    db_session: Session, registration_settings: Settings, fixed_clock: FixedClock
) -> None:
    outcome = _register(db_session, registration_settings, fixed_clock)
    assert outcome.status == "created" and outcome.user and outcome.workspace
    assert not outcome.user.is_instance_admin
    membership = db_session.scalar(select(Membership).where(Membership.user_id == outcome.user.id))
    assert membership is not None and membership.role == "owner"
    events = db_session.scalars(select(AuditEvent)).all()
    assert [(e.action, e.workspace_id) for e in events] == [("auth.register", outcome.workspace.id)]
    assert events[0].data == {}


def test_registration_rejects_existing_email_without_creating_a_user(
    db_session: Session, registration_settings: Settings, fixed_clock: FixedClock
) -> None:
    _create_user(db_session, registration_settings, fixed_clock, email="taken@example.com")
    outcome = _register(db_session, registration_settings, fixed_clock, email=" TAKEN@example.com")
    assert outcome == auth.RegistrationOutcome(status="rejected")
    assert len(db_session.scalars(select(User)).all()) == 1


def test_registration_reports_invalid_input_with_codes(
    db_session: Session, registration_settings: Settings, fixed_clock: FixedClock
) -> None:
    weak = _register(db_session, registration_settings, fixed_clock, password="qwertyuiop")
    bad_email = _register(db_session, registration_settings, fixed_clock, email="nope")
    assert weak.status == "invalid" and weak.error and weak.error.code == "password_too_common"
    assert bad_email.status == "invalid"
    assert bad_email.error and bad_email.error.code == "invalid_email"


def test_registration_counts_every_attempt_per_ip_and_throttles_the_sixth(
    db_session: Session, registration_settings: Settings, fixed_clock: FixedClock
) -> None:
    statuses = [
        _register(db_session, registration_settings, fixed_clock, email=f"r{i}@example.com").status
        for i in range(3)
    ]
    statuses.append(_register(db_session, registration_settings, fixed_clock, email="bad").status)
    statuses.append(
        _register(db_session, registration_settings, fixed_clock, email="r0@example.com").status
    )
    statuses.append(
        _register(db_session, registration_settings, fixed_clock, email="r9@example.com").status
    )
    assert statuses == ["created"] * 3 + ["invalid", "rejected", "throttled"]
    assert db_session.scalar(select(User).where(User.email == "r9@example.com")) is None
    other_ip = _register(
        db_session, registration_settings, fixed_clock, email="r9@example.com", host="192.0.2.96"
    )
    assert other_ip.status == "created"
    assert (
        _register(db_session, registration_settings, at(minutes=60), email="r8@example.com").status
        == "created"
    )


def test_registration_success_leaves_login_ip_counter_unchanged(
    db_session: Session, registration_settings: Settings, fixed_clock: FixedClock
) -> None:
    for index in range(4):
        _login(
            db_session,
            registration_settings,
            fixed_clock,
            email=f"x{index}@example.com",
            host="192.0.2.95",
        )
    ip_row = _throttle(db_session, "ip", "192.0.2.95/32")
    assert ip_row is not None
    before = (ip_row.failure_count, ip_row.blocked_until, ip_row.window_started_at)
    assert _register(db_session, registration_settings, fixed_clock).status == "created"
    assert (ip_row.failure_count, ip_row.blocked_until, ip_row.window_started_at) == before


# Password change, logout and CLI


def test_change_password_invalidates_other_sessions_but_keeps_current(
    db_session: Session, auth_settings: Settings, fixed_clock: FixedClock
) -> None:
    user = _create_user(db_session, auth_settings, fixed_clock)
    current = create_user_session(
        db_session, user, settings=auth_settings, clock=fixed_clock, host=None
    )
    other = create_user_session(
        db_session, user, settings=auth_settings, clock=fixed_clock, host=None
    )
    wrong = change_password(
        db_session,
        user,
        "wrong current",
        "An even Stronger Password",
        settings=auth_settings,
        clock=fixed_clock,
        current_session_id=current,
    )
    assert wrong == PasswordChangeOutcome(status="invalid_current")
    weak = change_password(
        db_session,
        user,
        PASSWORD,
        "qwertyuiop",
        settings=auth_settings,
        clock=fixed_clock,
        current_session_id=current,
    )
    assert weak.status == "invalid_new" and weak.error and weak.error.code == "password_too_common"
    assert db_session.get(UserSession, session_id_hash(other)) is not None
    changed = change_password(
        db_session,
        user,
        PASSWORD,
        "An even Stronger Password",
        settings=auth_settings,
        clock=fixed_clock,
        current_session_id=current,
    )
    assert changed == PasswordChangeOutcome(status="changed", revoked_sessions=1)
    assert db_session.get(UserSession, session_id_hash(current)) is not None
    assert db_session.get(UserSession, session_id_hash(other)) is None
    assert user.password_hash and auth.verify_password(
        user.password_hash, "An even Stronger Password"
    )
    event = db_session.scalar(select(AuditEvent).where(AuditEvent.action == "auth.password_change"))
    assert event is not None and event.data == {"other_sessions_revoked": 1}


def test_logout_and_logout_other_devices_are_audited(
    db_session: Session, auth_settings: Settings, fixed_clock: FixedClock
) -> None:
    user = _create_user(db_session, auth_settings, fixed_clock)
    current = create_user_session(
        db_session, user, settings=auth_settings, clock=fixed_clock, host=None
    )
    other = create_user_session(
        db_session, user, settings=auth_settings, clock=fixed_clock, host=None
    )
    assert revoke_other_sessions(db_session, user.id, current, clock=fixed_clock) == 1
    assert db_session.get(UserSession, session_id_hash(current)) is not None
    assert db_session.get(UserSession, session_id_hash(other)) is None
    revoke_session(db_session, current, clock=fixed_clock)
    db_session.flush()
    assert db_session.get(UserSession, session_id_hash(current)) is None
    events = db_session.scalars(select(AuditEvent).where(AuditEvent.action == "auth.logout")).all()
    assert len(events) == 2
    assert all(e.actor_user_id == user.id and e.entity_id == str(user.id) for e in events)


def test_cli_create_admin_is_break_glass_and_never_modifies_existing_users(
    db_session: Session, auth_settings: Settings, fixed_clock: FixedClock
) -> None:
    first = _create_user(db_session, auth_settings, fixed_clock)
    first_hash = first.password_hash
    with pytest.raises(EmailAlreadyRegistered):
        create_admin_cli(
            db_session,
            email=" TEST@EXAMPLE.COM",
            display_name="Takeover",
            password="Takeover Strong Password",
            settings=auth_settings,
            clock=fixed_clock,
        )
    assert first.password_hash == first_hash and first.display_name == "Test User"

    second, workspace = create_admin_cli(
        db_session,
        email="second@example.com",
        display_name="Second Admin",
        password="Second Admin Password",
        settings=auth_settings,
        clock=fixed_clock,
        create_workspace=False,
    )
    assert workspace is None and second.is_instance_admin
    assert db_session.scalars(select(Membership).where(Membership.user_id == second.id)).all() == []
    events = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == "cli.create_admin")
    ).all()
    assert [
        (e.actor_type, e.actor_user_id, e.entity_id, e.workspace_id, e.data) for e in events
    ] == [
        ("cli", None, str(first.id), None, {}),
        ("cli", None, str(second.id), None, {}),
    ]


def test_cli_create_admin_on_empty_instance_closes_web_setup(
    db_session: Session, auth_settings: Settings, fixed_clock: FixedClock
) -> None:
    _create_user(db_session, auth_settings, fixed_clock)
    with pytest.raises(SetupAlreadyCompleted):
        create_initial_admin(
            db_session,
            email="web@example.com",
            display_name="Web",
            password="Web Strong Password",
            workspace_name="Web",
            settings=auth_settings,
            clock=fixed_clock,
        )


def test_reset_password_sets_password_revokes_all_sessions_and_audits(
    db_session: Session, auth_settings: Settings, fixed_clock: FixedClock
) -> None:
    user = _create_user(db_session, auth_settings, fixed_clock)
    for _ in range(2):
        create_user_session(db_session, user, settings=auth_settings, clock=fixed_clock, host=None)
    reset_user_password(
        db_session,
        email=" Test@Example.com",
        password="Brand New Strong Password",
        settings=auth_settings,
        clock=fixed_clock,
    )
    assert user.password_hash and auth.verify_password(
        user.password_hash, "Brand New Strong Password"
    )
    assert db_session.scalars(select(UserSession)).all() == []
    event = db_session.scalar(select(AuditEvent).where(AuditEvent.action == "cli.reset_password"))
    assert event is not None
    assert (event.actor_type, event.actor_user_id, event.data) == (
        "cli",
        None,
        {"sessions_revoked": 2},
    )
    with pytest.raises(UnknownUser):
        reset_user_password(
            db_session,
            email="nobody@example.com",
            password="Brand New Strong Password",
            settings=auth_settings,
            clock=fixed_clock,
        )


def test_audit_action_set_and_failed_login_never_stores_attempted_identifier(
    db_session: Session, auth_settings: Settings, fixed_clock: FixedClock
) -> None:
    user, _workspace = create_initial_admin(
        db_session,
        email="audit@example.com",
        display_name="Audit User",
        password="Audit User Strong Password",
        workspace_name="Audit Home",
        settings=auth_settings,
        clock=fixed_clock,
        host="198.51.100.77",
        user_agent="A" * 250,
    )
    authenticate_user(
        db_session,
        email="not-present@example.com",
        password="incorrect password",
        settings=auth_settings,
        clock=fixed_clock,
        host="2001:db8:abcd:12::9",
        user_agent="B" * 250,
    )
    _login(db_session, auth_settings, fixed_clock, email=user.email)
    outcome = _login(
        db_session,
        auth_settings,
        fixed_clock,
        email=user.email,
        password="Audit User Strong Password",
    )
    assert outcome.session_id is not None
    assert (
        change_password(
            db_session,
            user,
            "Audit User Strong Password",
            "Audit User New Strong Password",
            settings=auth_settings,
            clock=fixed_clock,
            current_session_id=outcome.session_id,
        ).status
        == "changed"
    )
    revoke_session(db_session, outcome.session_id, clock=fixed_clock)
    cli_user, _ = create_admin_cli(
        db_session,
        email="second-cli@example.com",
        display_name="CLI User",
        password="CLI User Strong Password",
        workspace_name="CLI Workspace",
        settings=auth_settings,
        clock=fixed_clock,
    )
    reset_user_password(
        db_session,
        email=cli_user.email,
        password="CLI User Reset Strong Password",
        settings=auth_settings,
        clock=fixed_clock,
    )
    actions = set(db_session.scalars(select(AuditEvent.action)).all())
    assert actions == {
        "setup.create_admin",
        "auth.login_failure",
        "auth.login_success",
        "auth.password_change",
        "auth.logout",
        "cli.create_admin",
        "cli.reset_password",
    }
    failures = db_session.scalars(
        select(AuditEvent).where(AuditEvent.action == "auth.login_failure")
    ).all()
    unknown = next(e for e in failures if e.actor_user_id is None)
    known = next(e for e in failures if e.actor_user_id is not None)
    assert (unknown.entity_id, unknown.data) == (None, {})
    assert unknown.ip_prefix == "2001:db8:abcd::/48"
    assert unknown.user_agent is not None and len(unknown.user_agent) == 200
    assert known.actor_user_id == user.id and known.data == {}
    every_value = " ".join(
        f"{e.entity_id} {e.data} {e.ip_prefix} {e.user_agent}"
        for e in db_session.scalars(select(AuditEvent)).all()
    )
    assert "not-present" not in every_value
    assert "Password" not in every_value


def test_workspace_table_scope_allowlist_for_f12() -> None:
    expected_global = {
        "auth_throttles": "global authentication controls keyed by account/IP",
        "external_identities": "OIDC identities belong to global users",
        "setup_state": "singleton instance setup state",
        "user_sessions": "sessions belong to global users, independent of workspace selection",
        "users": "global identities can belong to multiple workspaces",
        "workspaces": "tenant root defines rather than belongs to a workspace",
    }
    mapped = Base.metadata.tables
    for table, model_table in mapped.items():
        if table == "audit_events":
            assert model_table.c.workspace_id.nullable
        elif table in {"memberships", "invitations"}:
            assert model_table.c.workspace_id.nullable is False
        else:
            assert "workspace_id" not in model_table.c
            assert table in expected_global
    assert set(mapped) == set(expected_global) | {"audit_events", "memberships", "invitations"}


def test_change_password_wrong_attempts_use_the_account_throttle(
    db_session: Session, auth_settings: Settings, fixed_clock: FixedClock
) -> None:
    user = _create_user(db_session, auth_settings, fixed_clock)

    def attempt(current: str, clock: FixedClock) -> PasswordChangeOutcome:
        return change_password(
            db_session,
            user,
            current,
            "Another Strong Password",
            settings=auth_settings,
            clock=clock,
        )

    statuses = [attempt("wrong current", fixed_clock).status for _ in range(6)]
    assert statuses == ["invalid_current"] * 6
    assert attempt(PASSWORD, fixed_clock).status == "throttled"
    assert auth.verify_password(user.password_hash or "", PASSWORD)
    # The same bucket blocks login, and the block ends with the backoff.
    assert not _login(db_session, auth_settings, fixed_clock, password=PASSWORD).succeeded
    assert attempt(PASSWORD, at(seconds=60)).status == "changed"


@pytest.mark.parametrize(
    ("host", "normal", "throttle"),
    [
        ("::ffff:192.0.2.7", "192.0.2.0/24", "192.0.2.7/32"),
        ("::ffff:198.51.100.9", "198.51.100.0/24", "198.51.100.9/32"),
    ],
)
def test_ipv4_mapped_ipv6_addresses_are_unwrapped(host: str, normal: str, throttle: str) -> None:
    assert client_ip_prefix(host) == normal
    assert client_ip_prefix(host, for_throttle=True) == throttle


def test_ipv4_mapped_clients_get_separate_throttle_buckets(
    db_session: Session, auth_settings: Settings, fixed_clock: FixedClock
) -> None:
    _login(db_session, auth_settings, fixed_clock, email="a@example.com", host="::ffff:192.0.2.7")
    _login(db_session, auth_settings, fixed_clock, email="b@example.com", host="::ffff:192.0.2.8")
    first = _throttle(db_session, "ip", "192.0.2.7/32")
    second = _throttle(db_session, "ip", "192.0.2.8/32")
    assert first is not None and second is not None and first.id != second.id
    assert (first.failure_count, second.failure_count) == (1, 1)
    # Same bucket whether the client arrives over IPv4 or as an IPv4-mapped IPv6 address.
    _login(db_session, auth_settings, fixed_clock, email="c@example.com", host="192.0.2.7")
    assert first.failure_count == 2


def test_blocked_login_attempts_with_distinct_emails_add_no_rows(
    db_session: Session, auth_settings: Settings, fixed_clock: FixedClock
) -> None:
    for index in range(auth_settings.auth_ip_free_failures + 1):
        _login(db_session, auth_settings, fixed_clock, email=f"spray-{index}@example.com")
    ip_row = _throttle(db_session, "ip", "192.0.2.10/32")
    assert ip_row is not None and ip_row.blocked_until is not None
    db_session.flush()
    rows_before = db_session.scalars(select(AuthThrottle)).all()
    snapshot = [(r.key_type, r.key_hash, r.failure_count, r.updated_at) for r in rows_before]
    audits_before = len(db_session.scalars(select(AuditEvent)).all())
    for index in range(10):
        outcome = _login(
            db_session, auth_settings, fixed_clock, email=f"blocked-{index}@example.com"
        )
        assert outcome == LoginOutcome()
    db_session.flush()
    rows_after = db_session.scalars(select(AuthThrottle)).all()
    assert [(r.key_type, r.key_hash, r.failure_count, r.updated_at) for r in rows_after] == snapshot
    assert len(db_session.scalars(select(AuditEvent)).all()) == audits_before


@pytest.mark.parametrize("pruning", [True, False])
def test_account_counter_idle_for_24_hours_counts_as_zero(
    db_session: Session,
    auth_settings: Settings,
    fixed_clock: FixedClock,
    monkeypatch: pytest.MonkeyPatch,
    pruning: bool,
) -> None:
    if not pruning:
        monkeypatch.setattr(auth, "prune_auth_throttles", lambda _session, _clock: 0)
    for _ in range(auth_settings.auth_account_free_failures):
        _login(db_session, auth_settings, fixed_clock, email="idle@example.com")
    almost = at(hours=24, seconds=-1)
    _login(db_session, auth_settings, almost, email="idle@example.com")
    row = _throttle(db_session, "account", "idle@example.com")
    assert row is not None and row.failure_count == 6 and row.blocked_until is not None

    later = at(hours=48)
    if pruning:
        assert prune_auth_throttles(db_session, later) >= 1
        db_session.flush()
        assert _throttle(db_session, "account", "idle@example.com") is None
    else:
        assert auth.prune_auth_throttles(db_session, later) == 0
    _login(db_session, auth_settings, later, email="idle@example.com")
    row = _throttle(db_session, "account", "idle@example.com")
    assert row is not None
    assert (row.failure_count, row.blocked_until) == (1, None)


def test_users_email_must_be_stored_lowercase(
    db_session: Session, auth_settings: Settings, fixed_clock: FixedClock
) -> None:
    user = _create_user(db_session, auth_settings, fixed_clock)
    db_session.flush()
    with pytest.raises(IntegrityError, match="ck_users_email_lowercase"), db_session.begin_nested():
        db_session.execute(
            text("UPDATE users SET email = 'Mixed@Example.com' WHERE id = :id"),
            {"id": user.id.hex},
        )


def test_registration_is_refused_while_setup_is_open(
    db_session: Session, registration_settings: Settings, fixed_clock: FixedClock
) -> None:
    assert auth.setup_is_open(db_session)
    with pytest.raises(RegistrationDisabled):
        register_user(
            db_session,
            email="early@example.com",
            display_name="Early",
            password="Early Strong Password",
            workspace_name="Early",
            settings=registration_settings,
            clock=fixed_clock,
            host="192.0.2.5",
        )
    assert db_session.scalars(select(User)).all() == []


def _account_slot(settings: Settings, key: str = "test@example.com") -> auth.ThrottleSlot:
    return auth.ThrottleSlot("account", key, None, settings.auth_account_free_failures)


def test_release_takes_back_only_this_requests_attempt(
    db_session: Session, auth_settings: Settings, fixed_clock: FixedClock
) -> None:
    slot = _account_slot(auth_settings)
    first = auth.reserve_attempts(db_session, [slot], fixed_clock.now(), auth_settings)
    second = auth.reserve_attempts(db_session, [slot], fixed_clock.now(), auth_settings)
    assert first and second
    auth.release_attempts(db_session, second)
    row = _throttle(db_session, "account", "test@example.com")
    assert row is not None and row.failure_count == 1


def test_release_is_a_no_op_after_a_reset_or_a_new_window(
    db_session: Session, auth_settings: Settings, fixed_clock: FixedClock
) -> None:
    slot = _account_slot(auth_settings)
    reserved = auth.reserve_attempts(db_session, [slot], fixed_clock.now(), auth_settings)
    assert reserved
    # A successful login elsewhere resets the counter and starts a new window ...
    auth._reset_account(db_session, "test@example.com", at(seconds=1).now())  # pyright: ignore[reportPrivateUsage]
    # ... and a new failure is counted in it.
    auth.reserve_attempts(db_session, [slot], at(seconds=2).now(), auth_settings)
    auth.release_attempts(db_session, reserved)
    row = _throttle(db_session, "account", "test@example.com")
    assert row is not None and row.failure_count == 1  # the later failure is not erased

    ip_slot = auth.ThrottleSlot("ip", "192.0.2.1/32", timedelta(minutes=60), 20)
    old = auth.reserve_attempts(db_session, [ip_slot], fixed_clock.now(), auth_settings)
    auth.reserve_attempts(db_session, [ip_slot], at(minutes=61).now(), auth_settings)
    assert old
    auth.release_attempts(db_session, old)
    ip_row = _throttle(db_session, "ip", "192.0.2.1/32")
    assert ip_row is not None and ip_row.failure_count == 1


def test_release_never_goes_below_zero(
    db_session: Session, auth_settings: Settings, fixed_clock: FixedClock
) -> None:
    slot = _account_slot(auth_settings)
    reserved = auth.reserve_attempts(db_session, [slot], fixed_clock.now(), auth_settings)
    assert reserved
    row = _throttle(db_session, "account", "test@example.com")
    assert row is not None
    row.failure_count = 0  # same window, counter already at zero
    auth.release_attempts(db_session, reserved)
    auth.release_attempts(db_session, reserved)
    assert row.failure_count == 0


def test_release_clears_only_a_block_its_own_reservation_set(
    db_session: Session, auth_settings: Settings, fixed_clock: FixedClock
) -> None:
    slot = _account_slot(auth_settings)
    for _ in range(auth_settings.auth_account_free_failures):
        auth.reserve_attempts(db_session, [slot], fixed_clock.now(), auth_settings)
    sixth = auth.reserve_attempts(db_session, [slot], fixed_clock.now(), auth_settings)
    assert sixth and sixth[0].block_set is not None
    row = _throttle(db_session, "account", "test@example.com")
    assert row is not None and row.blocked_until == sixth[0].block_set
    # Another request's later block must survive this release.
    row.blocked_until = sixth[0].block_set + timedelta(seconds=30)
    auth.release_attempts(db_session, sixth)
    assert row.failure_count == 5 and row.blocked_until is not None
    # Its own block is cleared.
    seventh = auth.reserve_attempts(db_session, [slot], at(minutes=2).now(), auth_settings)
    assert seventh and seventh[0].block_set is not None
    auth.release_attempts(db_session, seventh)
    assert row.blocked_until is None


def test_successful_login_clears_the_ip_block_its_own_reservation_set(
    db_session: Session, fixed_clock: FixedClock
) -> None:
    settings = Settings(_env_file=None, auth_ip_free_failures=1)
    _create_user(db_session, settings, fixed_clock)
    _login(db_session, settings, fixed_clock, email="someone-else@example.com")
    ip_row = _throttle(db_session, "ip", "192.0.2.10/32")
    assert ip_row is not None and (ip_row.failure_count, ip_row.blocked_until) == (1, None)
    # The success reserves attempt 2 (> 1 free, so it sets a block) and then takes it back.
    assert _login(db_session, settings, fixed_clock, password=PASSWORD).succeeded
    assert (ip_row.failure_count, ip_row.blocked_until) == (1, None)


def test_setup_is_open_is_the_single_definition_for_web_cli_and_registration(
    db_session: Session, registration_settings: Settings, fixed_clock: FixedClock
) -> None:
    assert auth.setup_is_open(db_session)
    with pytest.raises(RegistrationDisabled):
        register_user(
            db_session,
            email="r@example.com",
            display_name="R",
            password="Registrant Strong Pw",
            workspace_name="R",
            settings=registration_settings,
            clock=fixed_clock,
            host=None,
        )
    _create_user(db_session, registration_settings, fixed_clock)  # CLI path
    assert not auth.setup_is_open(db_session)
    with pytest.raises(SetupAlreadyCompleted):
        create_initial_admin(
            db_session,
            email="web@example.com",
            display_name="Web",
            password=PASSWORD,
            workspace_name="Web",
            settings=registration_settings,
            clock=fixed_clock,
        )


@pytest.mark.parametrize(
    "name", ["Ola\x07", "Ola\u202eadmin", "Ola\u200b", "Ola\nAdmin", "\u2028", " ", "x" * 121]
)
def test_display_names_refuse_control_and_format_characters(name: str) -> None:
    with pytest.raises(InvalidDisplayName):
        normalize_display_name(name)


def test_display_names_are_trimmed_and_inner_whitespace_collapsed() -> None:
    assert normalize_display_name("  Ola   Nowak ") == "Ola Nowak"
    assert normalize_display_name("Żaneta Łęcka") == "Żaneta Łęcka"


def test_change_password_refuses_the_current_password_as_the_new_one(
    db_session: Session, auth_settings: Settings, fixed_clock: FixedClock
) -> None:
    user = _create_user(db_session, auth_settings, fixed_clock)
    old_hash = user.password_hash
    unchanged = change_password(
        db_session, user, PASSWORD, PASSWORD, settings=auth_settings, clock=fixed_clock
    )
    assert unchanged.status == "invalid_new"
    assert unchanged.error is not None and unchanged.error.code == "password_unchanged"
    assert user.password_hash == old_hash
