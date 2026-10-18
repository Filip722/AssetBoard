"""The sign-in throttle looks the same for a known and an unknown email (ADR-0012).

The same request sequence is sent for a known and an unknown email: attacker guesses, sign-ins
with the owner's (current) password, and the owner's password changes and confirmations, right
or wrong. Everything an attacker can observe through sign-in must be identical.
"""

from __future__ import annotations

import hashlib
import itertools
from datetime import datetime, timedelta
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.orm import Session

from assetboard.clock import FixedClock
from assetboard.config import Settings
from assetboard.db.models.identity import AuthThrottle
from assetboard.services import auth
from assetboard.services.auth import (
    LoginOutcome,
    authenticate_user,
    change_password,
    create_admin_cli,
)
from tests.unit.test_auth import BASE_TIME, PASSWORD

_examples = itertools.count()


def at(seconds: float) -> FixedClock:
    return FixedClock((BASE_TIME + timedelta(seconds=seconds)).isoformat())


def _login(session: Session, config: Settings, t: float, email: str, password: str, host: str):
    return authenticate_user(
        session, email=email, password=password, settings=config, clock=at(t), host=host
    )


def _seen_by_attacker(
    session: Session, config: Settings, email: str, now: datetime
) -> tuple[Any, ...]:
    """The sign-in bucket as the next attempt reads it: the count still in its window (and
    when that window started), and an active block. An expired window counts as zero, as an
    idle row does, whatever is stored."""
    row = session.scalar(
        select(AuthThrottle).where(
            AuthThrottle.key_type == "account",
            AuthThrottle.key_hash == hashlib.sha256(email.encode()).hexdigest(),
        )
    )
    window = timedelta(minutes=config.auth_ip_window_minutes)
    count = auth._current_count(row, now, window)  # pyright: ignore[reportPrivateUsage]
    started = row.window_started_at if row is not None and count else None
    blocked = row.blocked_until if row and row.blocked_until and row.blocked_until > now else None
    return count, started, blocked


@pytest.fixture
def config() -> Settings:
    return Settings(_env_file=None, secret_key="test-master-key")


OWNER_HOST = "203.0.113.9"
# One step: which request, and how many seconds after the previous step.
#   attacker            a guess against the email
#   owner_sign_in       the owner's correct password; sent to the unknown email too, as the
#                       same request from someone who has an account elsewhere
#   owner_change_ok / owner_change_typo / owner_confirm_ok / owner_confirm_typo
#                       password change or confirmation by the signed-in owner (no analogue
#                       for an email without an account)
ACTIONS = [
    "attacker",
    "owner_sign_in",
    "owner_change_ok",
    "owner_change_typo",
    "owner_confirm_ok",
    "owner_confirm_typo",
]
STEPS = st.lists(
    st.tuples(st.sampled_from(ACTIONS), st.integers(min_value=0, max_value=1500)),
    min_size=1,
    max_size=24,
)


def _run(
    db_session: Session, config: Settings, steps: list[tuple[str, int]]
) -> tuple[list[Any], list[Any]]:
    """Run the same request sequence against a known and an unknown email.

    Returns, per email, everything an attacker can observe: the outcome of each of their
    attempts (status, message and wait follow from it) and the sign-in bucket as the next
    attempt reads it, after every step.
    """
    n = next(_examples)
    known, unknown = f"known-{n}@example.com", f"unknown-{n}@example.com"
    # Each side's attacker comes from its own address, which sees the same sequence.
    net = f"10.{n // 250}.{n % 250}"
    attackers = {known: f"{net}.1", unknown: f"{net}.2"}
    owner, _workspace = create_admin_cli(
        db_session,
        email=known,
        display_name="Owner",
        password=PASSWORD,
        workspace_name="Home",
        settings=config,
        clock=at(0),
    )
    current = PASSWORD
    changes = itertools.count(1)
    seen: dict[str, list[Any]] = {known: [], unknown: []}
    t = 0.0
    for action, gap in steps:
        t += gap
        if action == "attacker":
            for email in (known, unknown):
                outcome = _login(db_session, config, t, email, "guess", attackers[email])
                assert not outcome.succeeded
                seen[email].append(outcome)
        elif action == "owner_sign_in":
            for email in (known, unknown):
                _login(db_session, config, t, email, current, OWNER_HOST)
        elif action.startswith("owner_change"):
            ok = action.endswith("_ok")
            new_password = f"Another Strong Pass {next(changes)}"
            outcome = change_password(
                db_session,
                owner,
                current if ok else "typo",
                new_password,
                settings=config,
                clock=at(t),
            )
            if outcome.status == "changed":
                current = new_password
        else:
            _confirm(db_session, config, owner.id, current if action.endswith("_ok") else "typo", t)
        now = at(t).now()
        for email in (known, unknown):
            seen[email].append(_seen_by_attacker(db_session, config, email, now))
    return seen[known], seen[unknown]


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(steps=STEPS)
def test_any_sequence_looks_the_same_for_known_and_unknown_emails(
    db_session: Session, config: Settings, steps: list[tuple[str, int]]
) -> None:
    known, unknown = _run(db_session, config, steps)
    assert known == unknown


def _confirm(session: Session, config: Settings, user_id: Any, password: str, t: float) -> None:
    """A password confirmation (profile, admin, sign out others, link) in this session."""
    auth._confirm_password_flow(  # pyright: ignore[reportPrivateUsage]
        auth.SameSession(session),
        user_id=user_id,
        password=password,
        purpose="test",
        settings=config,
        clock=at(t),
        host="198.51.100.20",
        user_agent=None,
        verify=None,
        on_success=lambda _session, _user: True,
    )


# Build the counter, wait out the block, the owner signs in, try again.
REVIEW_SCENARIO = [("attacker", 0)] * 6 + [("owner_sign_in", 61), ("attacker", 1)]


@pytest.mark.parametrize("regression", ["reset_on_success", "release_on_success"])
def test_the_check_catches_a_success_that_changes_the_bucket(
    db_session: Session, config: Settings, monkeypatch: pytest.MonkeyPatch, regression: str
) -> None:
    """Mutation check: if a successful sign-in reset its email's bucket, or took its own
    attempt back, the model above would see the difference."""
    real = auth.create_user_session

    def create_and_regress(session: Session, user: Any, **kwargs: Any) -> str:
        row = session.scalar(
            select(AuthThrottle).where(
                AuthThrottle.key_type == "account",
                AuthThrottle.key_hash == hashlib.sha256(user.email.encode()).hexdigest(),
            )
        )
        assert row is not None
        if regression == "reset_on_success":
            row.failure_count, row.blocked_until = 0, None
        else:
            row.failure_count -= 1
        return real(session, user, **kwargs)

    monkeypatch.setattr(auth, "create_user_session", create_and_regress)
    known, unknown = _run(db_session, config, REVIEW_SCENARIO)
    assert known != unknown


def test_the_owner_signing_in_between_blocks_changes_nothing_for_the_attacker(
    db_session: Session, config: Settings
) -> None:
    """The reviewer's scenario. Before the fix the owner's sign-in reset the bucket and the
    attacker's last attempt got the ordinary message for a known email but a block for an
    unknown one."""
    known, unknown = _run(db_session, config, REVIEW_SCENARIO)
    assert known == unknown
    # The owner's sign-in counted (the 7th attempt, after the first block ended), so the
    # attacker's next attempt is throttled, for twice the base wait, on both emails.
    base = config.auth_throttle_base_seconds
    last_attempt = [item for item in known if isinstance(item, LoginOutcome)][-1]
    assert last_attempt == LoginOutcome(
        throttled_until=BASE_TIME + timedelta(seconds=base + 1 + 2 * base)
    )


def test_password_change_and_confirmation_failures_leave_the_sign_in_bucket_alone(
    db_session: Session, config: Settings
) -> None:
    owner, _workspace = create_admin_cli(
        db_session,
        email="owner@example.com",
        display_name="Owner",
        password=PASSWORD,
        workspace_name="Home",
        settings=config,
        clock=at(0),
    )
    for _ in range(config.auth_account_free_failures + 3):
        change_password(
            db_session, owner, "typo", "Another Strong Pass", settings=config, clock=at(1)
        )
        _confirm(db_session, config, owner.id, "typo", 1)
    assert _seen_by_attacker(db_session, config, "owner@example.com", at(2).now()) == (
        0,
        None,
        None,
    )
    assert _login(db_session, config, 2, "owner@example.com", PASSWORD, "192.0.2.5").succeeded
