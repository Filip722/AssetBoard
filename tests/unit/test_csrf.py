from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from assetboard.services.auth import derive_subkey
from assetboard.services.csrf import (
    ANONYMOUS_TOKEN_LIFETIME,
    anonymous_token,
    mask,
    new_anonymous_secret,
    session_token,
    unmask,
    verify_anonymous_token,
    verify_session_token,
)

KEY = derive_subkey("instance-secret", "csrf")
OTHER_KEY = derive_subkey("instance-secret", "other-purpose")
NOW = datetime.fromisoformat("2026-01-01T12:00:00+00:00")
SESSION_HASH = "a" * 64


def test_session_token_is_bound_to_key_and_session() -> None:
    token = session_token(KEY, SESSION_HASH)
    assert verify_session_token(KEY, SESSION_HASH, token)
    assert not verify_session_token(KEY, "b" * 64, token)
    assert not verify_session_token(OTHER_KEY, SESSION_HASH, token)
    assert not verify_session_token(derive_subkey("rotated", "csrf"), SESSION_HASH, token)
    for bad in (None, "", token[:-1], token + "x", "ż" * 10):
        assert not verify_session_token(KEY, SESSION_HASH, bad)


def test_session_token_does_not_contain_the_key_or_session_hash() -> None:
    token = session_token(KEY, SESSION_HASH)
    assert SESSION_HASH not in token
    assert token.isascii() and "=" not in token


def test_anonymous_token_round_trip_and_binding() -> None:
    secret = new_anonymous_secret()
    assert len(secret) >= 43
    token = anonymous_token(KEY, secret, NOW)
    assert verify_anonymous_token(KEY, secret, token, NOW)
    assert not verify_anonymous_token(KEY, new_anonymous_secret(), token, NOW)
    assert not verify_anonymous_token(OTHER_KEY, secret, token, NOW)
    assert not verify_anonymous_token(KEY, None, token, NOW)
    assert not verify_anonymous_token(KEY, secret, None, NOW)


@pytest.mark.parametrize(
    ("age", "valid"),
    [
        (timedelta(0), True),
        (ANONYMOUS_TOKEN_LIFETIME - timedelta(seconds=1), True),
        (ANONYMOUS_TOKEN_LIFETIME, False),
        (ANONYMOUS_TOKEN_LIFETIME + timedelta(days=1), False),
        (-timedelta(seconds=60), True),  # small clock skew tolerated
        (-timedelta(minutes=5), False),  # issued in the future
    ],
)
def test_anonymous_token_lifetime_is_twelve_hours(age: timedelta, valid: bool) -> None:
    assert timedelta(hours=12) == ANONYMOUS_TOKEN_LIFETIME
    secret = new_anonymous_secret()
    token = anonymous_token(KEY, secret, NOW)
    assert verify_anonymous_token(KEY, secret, token, NOW + age) is valid


def test_anonymous_token_issued_at_cannot_be_tampered_with() -> None:
    secret = new_anonymous_secret()
    timestamp, mac = anonymous_token(KEY, secret, NOW - timedelta(days=2)).split(".")
    fresh_timestamp = str(int(NOW.timestamp()))
    assert not verify_anonymous_token(KEY, secret, f"{fresh_timestamp}.{mac}", NOW)
    assert not verify_anonymous_token(KEY, secret, f"{timestamp}.{mac}", NOW)


@pytest.mark.parametrize(
    "token",
    ["", ".", "abc", "123", "123.", ".mac", "-1.mac", "1e5.mac", "١٢٣.mac", "9" * 20 + ".mac"],
)
def test_malformed_anonymous_tokens_are_rejected(token: str) -> None:
    assert not verify_anonymous_token(KEY, new_anonymous_secret(), token, NOW)


def test_mask_round_trip_differs_every_time() -> None:
    token = session_token(KEY, SESSION_HASH)
    first, second = mask(token), mask(token)
    assert first != second and token not in first
    assert unmask(first) == unmask(second) == token
    anonymous = anonymous_token(KEY, new_anonymous_secret(), NOW)
    assert unmask(mask(anonymous)) == anonymous


@pytest.mark.parametrize(
    "masked",
    [None, "", "!!!", "a", "ab" * 300, "zażółć", "AAAA"],
)
def test_unmask_rejects_malformed_input(masked: str | None) -> None:
    result = unmask(masked)
    assert result is None or not verify_session_token(KEY, SESSION_HASH, result)


def test_tampered_mask_does_not_verify() -> None:
    token = session_token(KEY, SESSION_HASH)
    masked = mask(token)
    flipped = masked[:-3] + ("A" if masked[-3] != "A" else "B") + masked[-2:]
    assert not verify_session_token(KEY, SESSION_HASH, unmask(flipped))
