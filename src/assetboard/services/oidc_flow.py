"""Signed, short-lived cookie payloads for the OIDC flow (F1.3, ADR-0014).

Pure functions: no I/O, and the time is passed in. The flow cookie carries state, nonce, the
PKCE verifier and ``next`` from the start of a sign-in to its callback. The pending-link cookie
carries a verified identity to the password confirmation page. Neither is stored server-side.

A payload is ``base64url(JSON).HMAC``, with a purpose-specific key from ``derive_subkey`` and a
purpose string in the MAC, so one kind can never be replayed as the other. ``iat`` and ``exp``
are inside the signed payload and checked here; the cookie's own Max-Age is only a hint.
Values are signed, not encrypted: the browser that started the flow may read them.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID

FLOW_LIFETIME = timedelta(minutes=10)
LINK_LIFETIME = timedelta(minutes=10)
CLOCK_SKEW = timedelta(minutes=1)
MAX_COOKIE_LENGTH = 3800
FLOW_PURPOSE = "oidc-flow-v1"
LINK_PURPOSE = "oidc-link-v1"

JsonValue = str | int | bool | None


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(key: bytes, purpose: str, body: str) -> str:
    message = purpose.encode("ascii") + b"\x00" + body.encode("ascii")
    return _b64(hmac.new(key, message, hashlib.sha256).digest())


def seal(
    key: bytes,
    purpose: str,
    payload: dict[str, JsonValue],
    *,
    now: datetime,
    lifetime: timedelta,
) -> str:
    issued = int(now.timestamp())
    data = {**payload, "iat": issued, "exp": issued + int(lifetime.total_seconds())}
    # UTF-8, not \\uXXXX escapes: a non-ASCII subject then costs at most 4 bytes per character,
    # which keeps the worst case (255-character subject, 512-character issuer, 320-character
    # email, 512-character next) under MAX_COOKIE_LENGTH.
    text = json.dumps(data, separators=(",", ":"), sort_keys=True, ensure_ascii=False)
    body = _b64(text.encode("utf-8"))
    value = f"{body}.{_mac(key, purpose, body)}"
    if len(value) > MAX_COOKIE_LENGTH:
        raise ValueError("payload too large for a cookie")
    return value


def unseal(
    key: bytes, purpose: str, value: str | None, *, now: datetime, lifetime: timedelta
) -> dict[str, Any] | None:
    """The payload, or None if missing, malformed, forged, expired or issued in the future."""
    if not value or len(value) > MAX_COOKIE_LENGTH or not value.isascii():
        return None
    body, separator, mac = value.partition(".")
    if not separator or not hmac.compare_digest(mac.encode(), _mac(key, purpose, body).encode()):
        return None
    try:
        data = json.loads(_unb64(body))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    payload = cast(dict[str, Any], data)
    issued, expires = payload.get("iat"), payload.get("exp")
    if type(issued) is not int or type(expires) is not int:
        return None
    current = now.timestamp()
    if issued > current + CLOCK_SKEW.total_seconds() or expires <= current:
        return None
    if expires - issued > lifetime.total_seconds():
        return None
    return payload


def _text(payload: dict[str, Any], name: str) -> str | None:
    value = payload.get(name)
    return value if isinstance(value, str) else None


@dataclass(frozen=True, slots=True)
class FlowState:
    state: str
    nonce: str
    code_verifier: str
    next_path: str

    def seal(self, key: bytes, *, now: datetime) -> str:
        return seal(
            key,
            FLOW_PURPOSE,
            {
                "state": self.state,
                "nonce": self.nonce,
                "verifier": self.code_verifier,
                "next": self.next_path,
            },
            now=now,
            lifetime=FLOW_LIFETIME,
        )

    @classmethod
    def unseal(cls, key: bytes, value: str | None, *, now: datetime) -> FlowState | None:
        payload = unseal(key, FLOW_PURPOSE, value, now=now, lifetime=FLOW_LIFETIME)
        if payload is None:
            return None
        state, nonce = _text(payload, "state"), _text(payload, "nonce")
        verifier, next_path = _text(payload, "verifier"), _text(payload, "next")
        if not state or not nonce or not verifier or next_path is None:
            return None
        return cls(state, nonce, verifier, next_path)


@dataclass(frozen=True, slots=True)
class PendingLink:
    """A verified OIDC identity waiting for the local account's password.

    ``admin`` is the group mapping's decision at sign-in (None: leave unchanged).
    """

    user_id: UUID
    issuer: str
    subject: str
    email: str
    admin: bool | None
    next_path: str

    def seal(self, key: bytes, *, now: datetime) -> str:
        return seal(
            key,
            LINK_PURPOSE,
            {
                "user": str(self.user_id),
                "iss": self.issuer,
                "sub": self.subject,
                "email": self.email,
                "admin": self.admin,
                "next": self.next_path,
            },
            now=now,
            lifetime=LINK_LIFETIME,
        )

    @classmethod
    def unseal(cls, key: bytes, value: str | None, *, now: datetime) -> PendingLink | None:
        payload = unseal(key, LINK_PURPOSE, value, now=now, lifetime=LINK_LIFETIME)
        if payload is None:
            return None
        user, issuer, subject = _text(payload, "user"), _text(payload, "iss"), _text(payload, "sub")
        email, next_path = _text(payload, "email"), _text(payload, "next")
        admin = payload.get("admin")
        if not user or not issuer or not subject or not email or next_path is None:
            return None
        if admin is not None and not isinstance(admin, bool):
            return None
        try:
            user_id = UUID(user)
        except ValueError:
            return None
        return cls(user_id, issuer, subject, email, admin, next_path)


MAX_CONSUMED_STATES = 10_000


class ConsumedStates:
    """Flow states already used by a callback, so a replayed callback is refused.

    Checked right after the flow cookie is unsealed, before any provider call or database write.
    Only the SHA-256 of each state is kept, for ``FLOW_LIFETIME`` (after which the cookie itself
    has expired). Bounded: past ``max_entries`` the oldest entries are evicted, so a flood of
    callbacks could let an old state be replayed early; the provider's single-use code still
    refuses it. In memory, per process: a restart forgets it (ADR-0014; one worker, ADR-0012).
    """

    def __init__(self, *, max_entries: int = MAX_CONSUMED_STATES) -> None:
        self._max_entries = max_entries
        self._seen: OrderedDict[str, datetime] = OrderedDict()
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return len(self._seen)

    def consume(self, state: str, *, now: datetime) -> bool:
        """True the first time ``state`` is seen; False for a replay."""
        key = hashlib.sha256(state.encode("utf-8")).hexdigest()
        with self._lock:
            while self._seen:
                oldest, expires = next(iter(self._seen.items()))
                if expires > now:
                    break
                del self._seen[oldest]
            if key in self._seen:
                return False
            self._seen[key] = now + FLOW_LIFETIME
            while len(self._seen) > self._max_entries:
                self._seen.popitem(last=False)
            return True

    def __contains__(self, state: object) -> bool:
        if not isinstance(state, str):
            return False
        return hashlib.sha256(state.encode("utf-8")).hexdigest() in self._seen
