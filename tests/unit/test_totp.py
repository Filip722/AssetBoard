from datetime import UTC, datetime

import pytest

from assetboard.domain.totp import matching_step, recovery_hash

RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


@pytest.mark.parametrize("timestamp", [29, 59, 60, 89])
def test_rfc6238_sha1_vector_and_adjacent_window(timestamp: int) -> None:
    assert matching_step(RFC_SECRET, "287082", datetime.fromtimestamp(timestamp, UTC), -1) == 1


def test_expiry_replay_and_invalid_codes() -> None:
    assert matching_step(RFC_SECRET, "287082", datetime.fromtimestamp(90, UTC), -1) is None
    assert matching_step(RFC_SECRET, "287082", datetime.fromtimestamp(59, UTC), 1) is None
    for code in ("", "28708", "2870820", "２８７０８２"):
        assert matching_step(RFC_SECRET, code, datetime.fromtimestamp(59, UTC), -1) is None


def test_recovery_hash_normalizes_format_and_is_sha256() -> None:
    assert recovery_hash("AB-CD ") == recovery_hash("abcd")
    assert (
        recovery_hash("abcd") == "88d4266fd4e6338d13b845fcf289579d209c897823b9217da3e161936f031589"
    )
