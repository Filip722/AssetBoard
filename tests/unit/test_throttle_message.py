"""The throttled message: minutes rounded up, plural forms, and the Retry-After value."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from assetboard.web.throttle import retry_after, throttled_message, wait_seconds

NOW = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0.5, "Too many attempts. Try again in 1 minute."),
        (60, "Too many attempts. Try again in 1 minute."),
        (61, "Too many attempts. Try again in 2 minutes."),
        (900, "Too many attempts. Try again in 15 minutes."),
    ],
)
def test_minutes_are_rounded_up(seconds: float, expected: str) -> None:
    assert throttled_message(NOW + timedelta(seconds=seconds), NOW, "en") == expected


@pytest.mark.parametrize("until", [None, NOW, NOW - timedelta(seconds=5)])
def test_an_unknown_or_past_wait_says_later(until: datetime | None) -> None:
    assert throttled_message(until, NOW, "en") == "Too many attempts. Try again later."
    assert throttled_message(until, NOW, "pl") == "Zbyt wiele prób. Spróbuj ponownie później."
    assert retry_after(until, NOW) is None


@pytest.mark.parametrize(
    ("minutes", "word"),
    [(1, "minutę"), (2, "minuty"), (4, "minuty"), (5, "minut"), (12, "minut"), (22, "minuty")],
)
def test_polish_uses_its_three_plural_forms(minutes: int, word: str) -> None:
    message = throttled_message(NOW + timedelta(minutes=minutes), NOW, "pl")
    assert message == f"Zbyt wiele prób. Spróbuj ponownie za {minutes} {word}."


def test_retry_after_is_whole_seconds_rounded_up() -> None:
    assert retry_after(NOW + timedelta(seconds=59.2), NOW) == "60"
    assert wait_seconds(NOW + timedelta(seconds=150), NOW) == 150
