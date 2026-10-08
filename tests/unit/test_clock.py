from __future__ import annotations

import pytest

from assetboard.clock import FixedClock


def test_fixed_clock_returns_aware_time() -> None:
    clock = FixedClock("2026-10-08T12:34:56+00:00")

    assert clock.now().isoformat() == "2026-10-08T12:34:56+00:00"
    assert clock.today("Europe/Warsaw").isoformat() == "2026-10-08"


def test_fixed_clock_rejects_naive_time() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        FixedClock("2026-10-08T12:34:56")


def test_fixed_clock_today_crosses_local_date_boundary() -> None:
    clock = FixedClock("2026-10-08T22:30:00+00:00")

    assert clock.today("Europe/Warsaw").isoformat() == "2026-10-09"
