from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo


class Clock:
    """Abstract clock for application and domain time."""

    def now(self) -> datetime:
        """Return the current UTC time."""
        return datetime.now(UTC)

    def today(self, tz: str) -> date:
        """Return the current local date in the given time zone."""
        return self.now().astimezone(ZoneInfo(tz)).date()


class FixedClock(Clock):
    """Clock used for deterministic tests."""

    def __init__(self, value: str) -> None:
        self._value = datetime.fromisoformat(value)
        if self._value.tzinfo is None or self._value.utcoffset() is None:
            raise ValueError("FixedClock requires a timezone-aware datetime")

    def now(self) -> datetime:
        return self._value

    def today(self, tz: str) -> date:
        return self._value.astimezone(ZoneInfo(tz)).date()
