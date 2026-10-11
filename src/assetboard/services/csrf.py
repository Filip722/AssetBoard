"""CSRF token primitives: pure functions, no I/O and no clock access.

The key is always a purpose-specific subkey (``derive_subkey(secret, "csrf")``), never the raw
``SECRET_KEY``.

* Signed-in forms use a synchronizer token bound to the session: HMAC over the stored session
  hash, so it is valid exactly as long as the session.
* Signed-out forms (login, setup, registration) use a signed double-submit token: the browser
  holds a random cookie secret, and the form token is ``<issued_at>.<HMAC(cookie, issued_at)>``.
  It expires ``ANONYMOUS_TOKEN_LIFETIME`` after it was issued.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

ANONYMOUS_TOKEN_LIFETIME = timedelta(hours=12)
# Tolerated clock difference for tokens that appear to be issued slightly in the future.
CLOCK_SKEW = timedelta(minutes=1)
_SESSION_DOMAIN = b"assetboard-csrf-session-v1"
_ANONYMOUS_DOMAIN = b"assetboard-csrf-anonymous-v1"


def _mac(key: bytes, *parts: bytes) -> str:
    digest = hmac.new(key, b"\x00".join(parts), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def session_token(key: bytes, session_id_hash: str) -> str:
    return _mac(key, _SESSION_DOMAIN, session_id_hash.encode("utf-8"))


def verify_session_token(key: bytes, session_id_hash: str, token: str | None) -> bool:
    if not token:
        return False
    return _equal(token, session_token(key, session_id_hash))


def new_anonymous_secret() -> str:
    """Random value for the anonymous CSRF cookie (256 bits)."""
    return secrets.token_urlsafe(32)


def anonymous_token(key: bytes, cookie_secret: str, issued_at: datetime) -> str:
    timestamp = str(int(issued_at.timestamp()))
    mac = _mac(key, _ANONYMOUS_DOMAIN, cookie_secret.encode("utf-8"), timestamp.encode("ascii"))
    return f"{timestamp}.{mac}"


def verify_anonymous_token(
    key: bytes,
    cookie_secret: str | None,
    token: str | None,
    now: datetime,
    *,
    lifetime: timedelta = ANONYMOUS_TOKEN_LIFETIME,
) -> bool:
    if not cookie_secret or not token:
        return False
    timestamp, separator, mac = token.partition(".")
    if not separator or not timestamp.isascii() or not timestamp.isdigit() or len(timestamp) > 12:
        return False
    expected = _mac(
        key, _ANONYMOUS_DOMAIN, cookie_secret.encode("utf-8"), timestamp.encode("ascii")
    )
    if not _equal(mac, expected):
        return False
    issued_at = datetime.fromtimestamp(int(timestamp), tz=now.tzinfo)
    return now - lifetime < issued_at <= now + CLOCK_SKEW


def mask(token: str) -> str:
    """Render ``token`` with a fresh one-time pad, so its bytes differ in every response.

    Defeats BREACH-style compression oracles: the secret never appears verbatim in the page.
    """
    raw = token.encode("ascii")
    pad = secrets.token_bytes(len(raw))
    masked = bytes(byte ^ key for byte, key in zip(raw, pad, strict=True))
    return base64.urlsafe_b64encode(pad + masked).rstrip(b"=").decode("ascii")


def unmask(masked: str | None) -> str | None:
    """Reverse ``mask``; ``None`` for anything malformed."""
    if not masked or not masked.isascii() or len(masked) > 512:
        return None
    try:
        data = base64.urlsafe_b64decode(masked + "=" * (-len(masked) % 4))
    except ValueError:
        return None
    if not data or len(data) % 2:
        return None
    half = len(data) // 2
    raw = bytes(byte ^ key for byte, key in zip(data[half:], data[:half], strict=True))
    try:
        return raw.decode("ascii")
    except UnicodeDecodeError:
        return None
