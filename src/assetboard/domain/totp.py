from __future__ import annotations

import hashlib
import hmac
import re
from datetime import datetime

import pyotp

TOTP_INTERVAL_SECONDS = 30
TOTP_WINDOW_STEPS = 1
RECOVERY_CODE_COUNT = 10


def matching_step(secret: str, code: str, now: datetime, last_step: int) -> int | None:
    if re.fullmatch(r"[0-9]{6}", code) is None:
        return None
    current = int(now.timestamp()) // TOTP_INTERVAL_SECONDS
    totp = pyotp.TOTP(secret, interval=TOTP_INTERVAL_SECONDS)
    for step in range(current + TOTP_WINDOW_STEPS, current - TOTP_WINDOW_STEPS - 1, -1):
        if step > last_step and hmac.compare_digest(totp.at(step * TOTP_INTERVAL_SECONDS), code):
            return step
    return None


def recovery_hash(code: str) -> str:
    return hashlib.sha256(code.strip().replace("-", "").lower().encode()).hexdigest()
