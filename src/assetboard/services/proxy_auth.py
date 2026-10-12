"""Reverse-proxy header authentication: peer trust and identity headers (F1.4, ADR-0015).

Pure functions: the web layer passes the TCP peer recorded by ``PeerMiddleware`` and the raw
ASGI headers. The identity they produce goes through the shared account mapping
(``oidc_accounts.sign_in_external``) under the proxy's ``MappingPolicy``.

What is trusted, and only from a peer inside ``TRUSTED_PROXIES``:

- ``PROXY_AUTH_USER_HEADER``: the subject, stored as ``ExternalIdentity(provider="proxy")``.
- ``PROXY_AUTH_EMAIL_HEADER``: the email, treated as verified (the proxy vouches for it). A
  missing or malformed value counts as an unverified email.
- ``PROXY_AUTH_NAME_HEADER`` (optional): the display name of a new account, nothing else.

Groups are never read. A header that appears more than once is refused; an empty value counts
as absent.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from ipaddress import IPv4Network, IPv6Address, IPv6Network
from typing import Literal

from assetboard.config import Settings
from assetboard.domain.identity import InvalidEmail, normalize_email
from assetboard.services.auth import normalize_client_host
from assetboard.services.external_identity import PROXY_PROVIDER, SignInIdentity

MAX_SUBJECT_LENGTH = 255
MAX_DISPLAY_NAME_LENGTH = 120

ProxyHeaderStatus = Literal["absent", "duplicate", "invalid", "ok"]


@dataclass(frozen=True, slots=True)
class ProxyHeaders:
    """The identity headers of one request. ``header`` names the one that was refused."""

    status: ProxyHeaderStatus
    identity: SignInIdentity | None = None
    header: str | None = None


def peer_is_trusted(peer: str | None, networks: Sequence[IPv4Network | IPv6Network]) -> bool:
    """Whether the TCP peer is inside ``TRUSTED_PROXIES``.

    ``::ffff:a.b.c.d`` (dual-stack sockets) is compared as the IPv4 address ``a.b.c.d``. No
    peer, a Unix socket path or anything else that is not an IP address is never trusted, and
    neither is a peer with an IPv6 zone (``fe80::1%eth0``): the same link-local address can be a
    different host on each interface.
    """
    address = normalize_client_host(peer)
    if address is None:
        return False
    if isinstance(address, IPv6Address) and address.scope_id is not None:
        return False
    return any(address.version == network.version and address in network for network in networks)


def _values(headers: Iterable[tuple[bytes, bytes]], name: str) -> list[bytes]:
    wanted = name.encode("ascii")
    return [value.strip() for key, value in headers if key.lower() == wanted and value.strip()]


# Control and format characters, line and paragraph separators, private-use and unassigned
# code points: none belongs in a user name, email or display name.
_REJECTED_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp", "Co", "Cn"})


def _text(value: bytes) -> str | None:
    """UTF-8 without characters from ``_REJECTED_CATEGORIES``; None otherwise."""
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        return None
    if any(unicodedata.category(character) in _REJECTED_CATEGORIES for character in text):
        return None
    return text


def _display_name(name: str | None, subject: str, email: str | None) -> str:
    """Always 1 to 120 printable characters, so account creation cannot reject it."""
    candidates = [name, subject, email.split("@", 1)[0] if email else None]
    for candidate in candidates:
        if candidate:
            cleaned = " ".join(candidate.split())[:MAX_DISPLAY_NAME_LENGTH].strip()
            if cleaned:
                return cleaned
    return "User"


def read_identity_headers(
    headers: Iterable[tuple[bytes, bytes]], settings: Settings
) -> ProxyHeaders:
    """Parse the configured identity headers. Call only for a trusted peer."""
    user_header = settings.proxy_auth_user_header
    email_header = settings.proxy_auth_email_header
    if user_header is None or email_header is None:
        return ProxyHeaders("absent")
    raw = list(headers)
    found: dict[str, list[bytes]] = {}
    for name in (user_header, email_header, settings.proxy_auth_name_header):
        if name is None:
            continue
        found[name] = _values(raw, name)
        if len(found[name]) > 1:
            return ProxyHeaders("duplicate", header=name)
    if not found[user_header]:
        return ProxyHeaders("absent")

    subject = _text(found[user_header][0])
    if subject is None or len(subject) > MAX_SUBJECT_LENGTH:
        return ProxyHeaders("invalid", header=user_header)

    email: str | None = None
    if found[email_header]:
        email_text = _text(found[email_header][0])
        if email_text is not None:
            try:
                email = normalize_email(email_text)
            except InvalidEmail:
                email = None

    name: str | None = None
    name_header = settings.proxy_auth_name_header
    if name_header is not None and found[name_header]:
        name = _text(found[name_header][0])

    return ProxyHeaders(
        "ok",
        SignInIdentity(
            issuer=PROXY_PROVIDER,
            subject=subject,
            email=email,
            email_verified=email is not None,
            display_name=_display_name(name, subject, email),
            groups=None,
        ),
    )


def identity_header_names(headers: Iterable[tuple[bytes, bytes]], settings: Settings) -> list[str]:
    """The configured identity headers present with a value (for the spoofing warning)."""
    raw = list(headers)
    names = (
        settings.proxy_auth_user_header,
        settings.proxy_auth_email_header,
        settings.proxy_auth_name_header,
    )
    return [name for name in names if name is not None and _values(raw, name)]
