from __future__ import annotations

import re

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
MAX_EMAIL_LENGTH = 320


class InvalidEmail(ValueError):
    code = "invalid_email"

    def __init__(self) -> None:
        super().__init__("Enter a valid email address")


def normalize_email(email: str) -> str:
    normalized = email.strip().lower()
    if len(normalized) > MAX_EMAIL_LENGTH or not _EMAIL_RE.fullmatch(normalized):
        raise InvalidEmail
    return normalized
