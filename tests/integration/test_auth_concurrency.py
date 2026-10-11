"""Concurrency of the auth services on the real engine configuration (file SQLite, WAL,
explicit BEGIN / opt-in BEGIN IMMEDIATE per ADR-0007), driven by threads."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import NamedTuple

import pytest
from argon2 import Parameters, PasswordHasher, extract_parameters, profiles
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from assetboard.clock import FixedClock
from assetboard.config import Settings
from assetboard.db.engine import create_database_engine
from assetboard.db.migrate import upgrade_database
from assetboard.db.models.identity import AuthThrottle, User, Workspace
from assetboard.services import auth
from assetboard.services.auth import (
    LoginOutcome,
    SetupAlreadyCompleted,
    authenticate_user_async,
    authenticate_with_factory,
    change_password_with_factory,
    run_initial_setup,
    setup_is_open,
)
from assetboard.services.hash_gate import HashGate, HashGateBusy

PASSWORD = "Correct Horse Battery!"
CLOCK = FixedClock("2026-01-01T00:00:00+00:00")


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None)


class Database(NamedTuple):
    engine: Engine
    factory: sessionmaker[Session]


Connect = Callable[..., Database]


@pytest.fixture
def database(tmp_path: Path) -> Iterator[Connect]:
    """A migrated file database; yields a connector whose engines are disposed afterwards."""
    url = f"sqlite:///{tmp_path / 'auth.db'}"
    upgrade_database(Settings(_env_file=None, data_dir=tmp_path, database_url=url))
    engines: list[Engine] = []

    def connect(*, busy_timeout_ms: int = 5000) -> Database:
        engine = create_database_engine(url, busy_timeout_ms=busy_timeout_ms)
        engines.append(engine)
        factory = sessionmaker(engine, info={"clock": CLOCK}, expire_on_commit=False)
        return Database(engine, factory)

    try:
        yield connect
    finally:
        for engine in engines:
            engine.dispose()


def _record_statements(engine: Engine) -> list[str]:
    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def record(_conn: object, _cursor: object, statement: str, *_args: object) -> None:
        statements.append(statement)

    return statements


def _setup_admin(
    factory: sessionmaker[Session], settings: Settings, email: str = "admin@example.com"
) -> User:
    user, _workspace = run_initial_setup(
        factory,
        email=email,
        display_name="Admin",
        password=PASSWORD,
        workspace_name="Home",
        settings=settings,
        clock=CLOCK,
    )
    return user


def _throttle(factory: sessionmaker[Session], scope: str, key: str) -> AuthThrottle | None:
    with factory() as session:
        return session.scalar(
            select(AuthThrottle).where(
                AuthThrottle.key_type == scope,
                AuthThrottle.key_hash == hashlib.sha256(key.encode()).hexdigest(),
            )
        )


class Overlap:
    """Holds every verification open until all requests have either verified or been refused.

    This forces the worst case: all requests are in flight at the same time, so any request
    that got past the throttle is counted while the others are still deciding. Each request
    runs through ``call``, which records its arrival whether or not it reached ``verify``.
    """

    def __init__(self, total: int) -> None:
        self.total = total
        self.arrived = 0
        self.verifications = 0
        self.condition = threading.Condition()
        self.local = threading.local()

    def call[T](self, request: Callable[[], T]) -> T:
        self.local.verified = False
        try:
            return request()
        finally:
            if not self.local.verified:  # refused by the throttle
                with self.condition:
                    self.arrived += 1
                    self.condition.notify_all()

    def verify(self, _password_hash: str, _password: str) -> bool:
        self.local.verified = True
        with self.condition:
            self.verifications += 1
            self.arrived += 1
            self.condition.notify_all()
            if not self.condition.wait_for(lambda: self.arrived >= self.total, timeout=20):
                raise TimeoutError("not all requests arrived")
        return False


def _run_threads(count: int, target: Callable[[int], None]) -> None:
    errors: list[BaseException] = []
    start = threading.Barrier(count)

    def wrapped(index: int) -> None:
        try:
            start.wait(timeout=20)
            target(index)
        except BaseException as exc:  # noqa: BLE001 - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=wrapped, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
        assert not thread.is_alive()
    assert errors == []


class HashRecorder:
    """Every Argon2 hash/verify as ``(kind, inside_begin_immediate)``.

    ``hash_delay`` slows each recorded hash so concurrent callers truly overlap.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []  # list.append is atomic across threads
        self.hash_delay = 0.0
        self.state = threading.local()

    def count(self, kind: str) -> int:
        return [call_kind for call_kind, _ in self.calls].count(kind)

    def inside_immediate(self) -> list[tuple[str, bool]]:
        return [call for call in self.calls if call[1]]


class RecordingHasher:
    def __init__(self, inner: PasswordHasher, recorder: HashRecorder) -> None:
        self._inner, self._recorder = inner, recorder

    def _record(self, kind: str) -> None:
        in_immediate = bool(getattr(self._recorder.state, "in_immediate", False))
        self._recorder.calls.append((kind, in_immediate))

    def hash(self, password: str) -> str:
        self._record("hash")
        time.sleep(self._recorder.hash_delay)
        return self._inner.hash(password)

    def verify(self, encoded: str, password: str) -> bool:
        self._record("verify")
        return self._inner.verify(encoded, password)

    def check_needs_rehash(self, encoded: str) -> bool:
        return self._inner.check_needs_rehash(encoded)


@pytest.fixture
def hashes(monkeypatch: pytest.MonkeyPatch) -> HashRecorder:
    recorder = HashRecorder()
    real_hasher, real_run_immediate = auth.password_hasher, auth.run_immediate

    def run_immediate[T](factory: sessionmaker[Session], operation: Callable[[Session], T]) -> T:
        recorder.state.in_immediate = True
        try:
            return real_run_immediate(factory, operation)
        finally:
            recorder.state.in_immediate = False

    monkeypatch.setattr(auth, "password_hasher", lambda: RecordingHasher(real_hasher(), recorder))
    monkeypatch.setattr(auth, "run_immediate", run_immediate)
    return recorder


def _stronger_parameters() -> Parameters:
    return dataclasses.replace(profiles.CHEAPEST, time_cost=profiles.CHEAPEST.time_cost + 1)


def _stored_hash(factory: sessionmaker[Session], user_id: object) -> str:
    with factory() as session:
        stored = session.get(User, user_id)
        assert stored is not None and stored.password_hash is not None
        return stored.password_hash


# Burst limits


def test_parallel_login_burst_verifies_at_most_free_plus_one_per_account(
    database: Connect, settings: Settings
) -> None:
    factory = database().factory
    _setup_admin(factory, settings)
    overlap = Overlap(total=20)
    outcomes: list[LoginOutcome] = []

    def attempt(index: int) -> None:
        outcomes.append(
            overlap.call(
                lambda: authenticate_with_factory(
                    factory,
                    email="admin@example.com",
                    password="wrong password",
                    settings=settings,
                    clock=CLOCK,
                    host=f"192.0.2.{index + 1}",  # distinct addresses: only the account bucket
                    verify=overlap.verify,
                )
            )
        )

    _run_threads(20, attempt)
    # Reservations are serialised, so exactly free + 1 requests get to verify a password.
    assert overlap.verifications == settings.auth_account_free_failures + 1
    assert outcomes == [LoginOutcome()] * 20
    account = _throttle(factory, "account", "admin@example.com")
    assert account is not None
    assert account.failure_count == overlap.verifications
    assert account.blocked_until is not None


def test_parallel_login_burst_verifies_at_most_free_plus_one_per_ip(
    database: Connect, settings: Settings
) -> None:
    factory = database().factory
    overlap = Overlap(total=30)
    outcomes: list[LoginOutcome] = []

    def attempt(index: int) -> None:
        outcomes.append(
            overlap.call(
                lambda: authenticate_with_factory(
                    factory,
                    email=f"spray-{index}@example.com",
                    password="wrong password",
                    settings=settings,
                    clock=CLOCK,
                    host="198.51.100.7",
                    verify=overlap.verify,
                )
            )
        )

    _run_threads(30, attempt)
    assert overlap.verifications == settings.auth_ip_free_failures + 1
    assert outcomes == [LoginOutcome()] * 30
    ip_row = _throttle(factory, "ip", "198.51.100.7/32")
    assert ip_row is not None and ip_row.failure_count == overlap.verifications


def test_parallel_wrong_current_passwords_verify_at_most_free_plus_one(
    database: Connect, settings: Settings
) -> None:
    factory = database().factory
    user = _setup_admin(factory, settings)
    overlap = Overlap(total=12)
    statuses: list[str] = []

    def attempt(_index: int) -> None:
        outcome = overlap.call(
            lambda: change_password_with_factory(
                factory,
                user_id=user.id,
                current_password="wrong current",
                new_password="Another Strong Password",
                settings=settings,
                clock=CLOCK,
                verify=overlap.verify,
            )
        )
        statuses.append(outcome.status)

    _run_threads(12, attempt)
    verified = overlap.verifications
    assert verified == settings.auth_account_free_failures + 1
    assert sorted(statuses) == sorted(
        ["invalid_current"] * verified + ["throttled"] * (12 - verified)
    )


# Blocked requests stay off the write lock


def test_blocked_requests_never_take_the_write_lock(database: Connect, settings: Settings) -> None:
    engine, factory = database()
    user = _setup_admin(factory, settings)
    for _ in range(settings.auth_account_free_failures + 1):
        authenticate_with_factory(
            factory,
            email="admin@example.com",
            password="wrong",
            settings=settings,
            clock=CLOCK,
            host="192.0.2.1",
        )
    statements = _record_statements(engine)

    login = authenticate_with_factory(
        factory,
        email="admin@example.com",
        password=PASSWORD,
        settings=settings,
        clock=CLOCK,
        host="192.0.2.2",
    )
    change = change_password_with_factory(
        factory,
        user_id=user.id,
        current_password=PASSWORD,
        new_password="Another Strong Password",
        settings=settings,
        clock=CLOCK,
    )

    assert login == LoginOutcome() and change.status == "throttled"
    assert "BEGIN" in statements
    assert "BEGIN IMMEDIATE" not in statements
    assert not any(
        s.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")) for s in statements
    )


# Releasing reservations


def test_login_releases_its_own_reservation_when_verification_fails(
    database: Connect, settings: Settings
) -> None:
    factory = database().factory
    _setup_admin(factory, settings)
    for _ in range(settings.auth_account_free_failures):
        authenticate_with_factory(
            factory,
            email="admin@example.com",
            password="wrong",
            settings=settings,
            clock=CLOCK,
            host="192.0.2.1",
        )

    def broken(_password_hash: str, _password: str) -> bool:
        raise RuntimeError("worker died")

    with pytest.raises(RuntimeError):
        authenticate_with_factory(
            factory,
            email="admin@example.com",
            password="wrong",
            settings=settings,
            clock=CLOCK,
            host="192.0.2.1",
            verify=broken,
        )
    account = _throttle(factory, "account", "admin@example.com")
    ip_row = _throttle(factory, "ip", "192.0.2.1/32")
    assert account is not None and ip_row is not None
    # The 6th reservation set a block; releasing it restored the previous state.
    assert (account.failure_count, account.blocked_until) == (5, None)
    assert ip_row.failure_count == 5


def test_change_password_releases_its_reservation_when_hashing_fails(
    database: Connect, settings: Settings
) -> None:
    factory = database().factory
    user = _setup_admin(factory, settings)

    def broken_hasher(_password: str) -> str:
        raise RuntimeError("worker died")

    with pytest.raises(RuntimeError):
        change_password_with_factory(
            factory,
            user_id=user.id,
            current_password=PASSWORD,
            new_password="Another Strong Password",
            settings=settings,
            clock=CLOCK,
            hasher=broken_hasher,
        )
    account = _throttle(factory, "account", "admin@example.com")
    assert account is not None and account.failure_count == 0
    with factory() as session:
        stored = session.get(User, user.id)
        assert stored is not None and auth.verify_password(stored.password_hash or "", PASSWORD)


def test_busy_hash_gate_releases_the_reservation(database: Connect, settings: Settings) -> None:
    factory = database().factory
    _setup_admin(factory, settings)

    async def scenario() -> None:
        gate = HashGate(1, max_waiting=0)
        hold = threading.Event()
        occupied = asyncio.create_task(gate.run(lambda: hold.wait(10)))
        await asyncio.sleep(0)
        try:
            with pytest.raises(HashGateBusy):
                await authenticate_user_async(
                    factory,
                    gate,
                    email="admin@example.com",
                    password=PASSWORD,
                    settings=settings,
                    clock=CLOCK,
                    host="192.0.2.9",
                )
        finally:
            hold.set()
            await occupied

    asyncio.run(scenario())
    account = _throttle(factory, "account", "admin@example.com")
    ip_row = _throttle(factory, "ip", "192.0.2.9/32")
    assert account is not None and account.failure_count == 0
    assert ip_row is not None and ip_row.failure_count == 0


def test_async_login_through_the_gate_succeeds(database: Connect, settings: Settings) -> None:
    factory = database().factory
    _setup_admin(factory, settings)

    async def scenario() -> LoginOutcome:
        return await authenticate_user_async(
            factory,
            HashGate(2),
            email="admin@example.com",
            password=PASSWORD,
            settings=settings,
            clock=CLOCK,
            host="192.0.2.9",
        )

    assert asyncio.run(scenario()).succeeded


# Hashing never runs inside BEGIN IMMEDIATE; the dummy hash is computed once


def test_no_password_hash_runs_inside_an_immediate_transaction(
    database: Connect,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    hashes: HashRecorder,
) -> None:
    factory = database().factory
    monkeypatch.setattr(auth, "_cached_dummy_hashes", {})
    user = _setup_admin(factory, settings)
    assert (hashes.count("hash"), hashes.count("verify")) == (1, 0)  # the admin's password

    stronger = _stronger_parameters()  # the next successful login must rehash
    monkeypatch.setattr(auth, "ARGON2_PARAMETERS", stronger)

    unknown = authenticate_with_factory(
        factory,
        email="nobody@example.com",
        password=PASSWORD,
        settings=settings,
        clock=CLOCK,
        host="192.0.2.3",
    )
    assert not unknown.succeeded
    # One dummy hash for the new parameters, one verification against it.
    assert (hashes.count("hash"), hashes.count("verify")) == (2, 1)

    login = authenticate_with_factory(
        factory,
        email="admin@example.com",
        password=PASSWORD,
        settings=settings,
        clock=CLOCK,
        host="192.0.2.3",
    )
    assert login.succeeded
    # Exactly one more verification and exactly one rehash, and the rehash was stored.
    assert (hashes.count("hash"), hashes.count("verify")) == (3, 2)
    assert extract_parameters(_stored_hash(factory, user.id)) == stronger

    changed = change_password_with_factory(
        factory,
        user_id=user.id,
        current_password=PASSWORD,
        new_password="Another Strong Password",
        settings=settings,
        clock=CLOCK,
    )
    assert changed.status == "changed"
    assert (hashes.count("hash"), hashes.count("verify")) == (4, 3)
    assert hashes.inside_immediate() == []  # no hash or verify inside BEGIN IMMEDIATE


def test_busy_gate_skips_the_rehash_and_the_login_still_succeeds(
    database: Connect, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = database().factory
    user = _setup_admin(factory, settings)
    before = _stored_hash(factory, user.id)
    monkeypatch.setattr(auth, "ARGON2_PARAMETERS", _stronger_parameters())

    def busy_hasher(_password: str) -> str:
        raise HashGateBusy

    outcome = authenticate_with_factory(
        factory,
        email="admin@example.com",
        password=PASSWORD,
        settings=settings,
        clock=CLOCK,
        host="192.0.2.5",
        hasher=busy_hasher,
    )
    assert outcome.succeeded
    assert _stored_hash(factory, user.id) == before  # unchanged; the next login retries
    account = _throttle(factory, "account", "admin@example.com")
    assert account is not None and account.failure_count == 0


def test_login_is_refused_and_the_newer_hash_kept_when_the_password_changes_mid_login(
    database: Connect, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = database().factory
    user = _setup_admin(factory, settings)
    monkeypatch.setattr(auth, "ARGON2_PARAMETERS", _stronger_parameters())

    def verify_then_change_elsewhere(password_hash: str, password: str) -> bool:
        ok = auth.verify_password(password_hash, password)
        with factory.begin() as session:  # e.g. the CLI resets the password meanwhile
            stored = session.get(User, user.id)
            assert stored is not None
            stored.password_hash = auth.argon2_hash("Reset By Operator Pw")
        return ok

    outcome = authenticate_with_factory(
        factory,
        email="admin@example.com",
        password=PASSWORD,
        settings=settings,
        clock=CLOCK,
        host="192.0.2.4",
        verify=verify_then_change_elsewhere,
    )
    assert not outcome.succeeded
    assert auth.verify_password(_stored_hash(factory, user.id), "Reset By Operator Pw")


def test_concurrent_cold_callers_compute_the_dummy_hash_exactly_once(
    monkeypatch: pytest.MonkeyPatch, hashes: HashRecorder
) -> None:
    monkeypatch.setattr(auth, "_cached_dummy_hashes", {})
    hashes.hash_delay = 0.2  # long enough that every caller is inside before the first finishes
    results: list[str] = []

    _run_threads(12, lambda _index: results.append(auth._dummy_hash()))  # pyright: ignore[reportPrivateUsage]

    assert hashes.count("hash") == 1
    assert len(set(results)) == 1 and len(results) == 12


def test_parallel_cold_start_unknown_email_logins_compute_one_dummy_hash(
    database: Connect,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    hashes: HashRecorder,
) -> None:
    factory = database().factory
    monkeypatch.setattr(auth, "_cached_dummy_hashes", {})
    hashes.hash_delay = 0.2
    outcomes: list[LoginOutcome] = []

    def attempt(index: int) -> None:
        outcomes.append(
            authenticate_with_factory(
                factory,
                email=f"unknown-{index}@example.com",
                password=PASSWORD,
                settings=settings,
                clock=CLOCK,
                host=f"192.0.2.{index + 1}",
            )
        )

    _run_threads(12, attempt)
    assert outcomes == [LoginOutcome()] * 12
    assert (hashes.count("hash"), hashes.count("verify")) == (1, 12)


# Concurrent setup


@pytest.mark.parametrize("variant", ["waits_for_lock", "lock_timeout"])
def test_concurrent_setup_has_one_winner_and_a_setup_already_completed_loser(
    database: Connect,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
) -> None:
    winner_factory = database().factory
    loser_db = database(busy_timeout_ms=5000 if variant == "waits_for_lock" else 50)
    claimed, release_winner = threading.Event(), threading.Event()
    loser_at_begin, loser_done = threading.Event(), threading.Event()
    original_claim = auth._claim_setup  # pyright: ignore[reportPrivateUsage]

    def claim(session: Session, clock: FixedClock) -> bool:
        won = original_claim(session, clock)
        if threading.current_thread().name == "winner":
            claimed.set()
            assert release_winner.wait(timeout=20)
        return won

    @event.listens_for(loser_db.engine, "before_cursor_execute")
    def loser_begins(_conn: object, _cursor: object, statement: str, *_args: object) -> None:
        if statement == "BEGIN IMMEDIATE":
            loser_at_begin.set()

    monkeypatch.setattr(auth, "_claim_setup", claim)
    results: dict[str, object] = {}

    def run(name: str, factory: sessionmaker[Session]) -> None:
        try:
            results[name] = run_initial_setup(
                factory,
                email=f"{name}@example.com",
                display_name=name.title(),
                password=PASSWORD,
                workspace_name=name,
                settings=settings,
                clock=CLOCK,
            )
        except BaseException as exc:  # noqa: BLE001 - asserted below
            results[name] = exc
        finally:
            if name == "loser":
                loser_done.set()

    winner = threading.Thread(target=run, args=("winner", winner_factory), name="winner")
    loser = threading.Thread(target=run, args=("loser", loser_db.factory), name="loser")
    winner.start()
    assert claimed.wait(timeout=20)
    loser.start()
    # The loser has passed its read-only "setup open?" check (the winner has not committed)
    # and is now at BEGIN IMMEDIATE, behind the winner's write lock.
    assert loser_at_begin.wait(timeout=20)
    if variant == "lock_timeout":
        # The winner keeps the lock until the loser has given up waiting for it.
        assert loser_done.wait(timeout=20)
    else:
        assert not loser_done.is_set()
    release_winner.set()
    winner.join(timeout=20)
    loser.join(timeout=20)

    assert isinstance(results["winner"], tuple)
    loser_result = results["loser"]
    assert isinstance(loser_result, SetupAlreadyCompleted)
    assert loser_result.code == "setup_completed"
    if variant == "lock_timeout":
        assert isinstance(loser_result.__cause__, OperationalError)
    else:
        assert loser_result.__cause__ is None  # it got the lock and lost the claim
    with winner_factory() as session:
        assert session.scalars(select(User.email)).all() == ["winner@example.com"]
        assert len(session.scalars(select(Workspace)).all()) == 1
        assert not setup_is_open(session)
