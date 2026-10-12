"""The identity an external sign-in hands over (OIDC in F1.3, reverse-proxy headers in F1.4).

Kept apart from the OIDC client and the account mapping, so either can use it without
importing the other.
"""

from __future__ import annotations

from dataclasses import dataclass

# ``ExternalIdentity.provider`` for reverse-proxy identities. OIDC providers are stored by their
# issuer URL, which always has a scheme, so the two never collide.
PROXY_PROVIDER = "proxy"


@dataclass(frozen=True, slots=True)
class SignInIdentity:
    """What a validated sign-in tells us. ``groups`` is None when the claim is unusable.

    ``issuer`` is the provider: the OIDC issuer URL, or ``PROXY_PROVIDER``.
    """

    issuer: str
    subject: str
    email: str | None
    email_verified: bool
    display_name: str
    groups: tuple[str, ...] | None
