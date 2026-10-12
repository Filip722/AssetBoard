"""An in-process OIDC provider for tests: no sockets, no network.

It implements ``assetboard.services.oidc.HttpTransport``, so the client talks to it exactly as
it would talk to a real provider over HTTP. ``authorize`` plays the browser and the provider's
login page: it checks the authorization request and returns the callback parameters.

Every part a test may want to break is a plain attribute: the claims, the signing key and
algorithm, the userinfo document, the discovery document.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from joserfc import jwt
from joserfc.jwk import ECKey, KeySet, OctKey, RSAKey

from assetboard.clock import FixedClock
from assetboard.services.oidc import HttpResponse

ISSUER = "https://idp.example.test/realms/home"
CLIENT_ID = "assetboard"
CLIENT_SECRET = "fake-client-secret-value"
BASE_URL = "https://money.example.test"
REDIRECT_URI = f"{BASE_URL}/auth/oidc/callback"


class StepClock(FixedClock):
    """A fixed clock a test can move forward (caches and expiry use the injected clock)."""

    def advance(self, delta: timedelta) -> None:
        self._value += delta


def _json(status: int, document: object, content_type: str = "application/json") -> HttpResponse:
    return HttpResponse(status, json.dumps(document).encode(), content_type)


@dataclass
class _Grant:
    redirect_uri: str
    challenge: str
    nonce: str | None
    subject: str


@dataclass
class FakeIdp:
    clock: FixedClock
    issuer: str = ISSUER
    # Where the endpoints live (the issuer unless a test breaks it) and the client's callback.
    base: str = ISSUER
    redirect_uri: str = REDIRECT_URI
    client_id: str = CLIENT_ID
    client_secret: str | None = CLIENT_SECRET
    key: RSAKey = field(
        default_factory=lambda: RSAKey.generate_key(2048, parameters={"kid": "k1"}, private=True)
    )
    algorithm: str = "RS256"
    subject: str = "user-123"
    claims: dict[str, Any] = field(
        default_factory=lambda: {
            "email": "ola@example.com",
            "email_verified": True,
            "name": "Ola Nordmann",
        }
    )
    # Overrides applied to the signed ID token after the standard claims (None removes one).
    id_token_overrides: dict[str, Any] = field(default_factory=dict[str, Any])
    userinfo: dict[str, Any] | None = None
    discovery_overrides: dict[str, Any] = field(default_factory=dict[str, Any])
    auth_methods: list[str] = field(default_factory=lambda: ["client_secret_basic"])
    sign_with: Any = None  # a different key, to forge a signature
    requests: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    token_requests: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    discovery_status: int = 200
    published_keys: list[Any] | None = None
    _grants: dict[str, _Grant] = field(default_factory=dict[str, _Grant])

    # Documents

    def discovery(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "issuer": self.issuer,
            "authorization_endpoint": f"{self.base}/auth",
            "token_endpoint": f"{self.base}/token",
            "jwks_uri": f"{self.base}/certs",
            "userinfo_endpoint": f"{self.base}/userinfo",
            "end_session_endpoint": f"{self.base}/logout",
            "token_endpoint_auth_methods_supported": self.auth_methods,
            "code_challenge_methods_supported": ["S256"],
            "id_token_signing_alg_values_supported": ["RS256"],
        }
        document.update(self.discovery_overrides)
        return {name: value for name, value in document.items() if value is not None}

    def jwks(self) -> dict[str, Any]:
        keys: list[Any] = self.published_keys if self.published_keys is not None else [self.key]
        return dict(KeySet(keys).as_dict(private=False))

    # Transport

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        data: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        path = urlsplit(url)._replace(query="").geturl()
        self.requests.append((method, path))
        if path == f"{self.base.rstrip('/')}/.well-known/openid-configuration":
            return _json(self.discovery_status, self.discovery())
        if path == f"{self.base}/certs":
            return _json(200, self.jwks())
        if path == f"{self.base}/token" and method == "POST":
            return self._token(headers, dict(data or {}))
        if path == f"{self.base}/userinfo":
            if headers.get("Authorization") != "Bearer fake-access-token":
                return _json(401, {"error": "invalid_token"})
            return _json(200, self.userinfo if self.userinfo is not None else {})
        return _json(404, {"error": "not_found"})

    # The browser at the provider

    def authorize(self, url: str, *, state: str | None = None, **extra: str) -> dict[str, str]:
        """Check the authorization request and return the callback query parameters."""
        query = {name: values[0] for name, values in parse_qs(urlsplit(url).query).items()}
        assert url.startswith(f"{self.base}/auth?")
        assert query["client_id"] == self.client_id
        assert query["response_type"] == "code"
        assert query["redirect_uri"] == self.redirect_uri
        assert query["code_challenge_method"] == "S256"
        assert "openid" in query["scope"].split()
        code = secrets.token_urlsafe(16)
        self._grants[code] = _Grant(
            query["redirect_uri"], query["code_challenge"], query.get("nonce"), self.subject
        )
        params = {"code": code, "state": state if state is not None else query["state"]}
        params.update(extra)
        return params

    def _client_ok(self, headers: Mapping[str, str], data: dict[str, Any]) -> bool:
        if data.get("client_id") not in (None, self.client_id):
            return False
        if self.client_secret is None:
            return "client_secret" not in data and "Authorization" not in headers
        basic = headers.get("Authorization", "")
        if basic.startswith("Basic "):
            decoded = base64.b64decode(basic.removeprefix("Basic ")).decode()
            client, _, secret = decoded.partition(":")
            return unquote(client) == self.client_id and unquote(secret) == self.client_secret
        return data.get("client_secret") == self.client_secret

    def _token(self, headers: Mapping[str, str], data: dict[str, Any]) -> HttpResponse:
        self.token_requests.append({"headers": dict(headers), "data": data})
        if not self._client_ok(headers, data):
            return _json(401, {"error": "invalid_client"})
        grant = self._grants.pop(str(data.get("code")), None)  # single use
        if grant is None or data.get("grant_type") != "authorization_code":
            return _json(400, {"error": "invalid_grant"})
        if data.get("redirect_uri") != grant.redirect_uri:
            return _json(400, {"error": "invalid_grant"})
        verifier = str(data.get("code_verifier", ""))
        digest = hashlib.sha256(verifier.encode()).digest()
        if base64.urlsafe_b64encode(digest).rstrip(b"=").decode() != grant.challenge:
            return _json(400, {"error": "invalid_grant", "error_description": "PKCE failed"})
        return _json(
            200,
            {
                "access_token": "fake-access-token",
                "token_type": "Bearer",
                "id_token": self.id_token(grant.nonce, grant.subject),
            },
        )

    def id_token(self, nonce: str | None, subject: str | None = None) -> str:
        now = int(self.clock.now().timestamp())
        claims: dict[str, Any] = {
            "iss": self.issuer,
            "sub": subject or self.subject,
            "aud": self.client_id,
            "iat": now,
            "exp": now + 300,
            "nonce": nonce,
            **self.claims,
        }
        for name, value in self.id_token_overrides.items():
            if value is None:
                claims.pop(name, None)
            else:
                claims[name] = value
        key = self.sign_with if self.sign_with is not None else self.key
        header: dict[str, Any] = {"alg": self.algorithm}
        kid = getattr(key, "kid", None)
        if kid:
            header["kid"] = kid
        if self.algorithm == "none":
            return _unsigned(header, claims)
        return jwt.encode(header, claims, key, algorithms=[self.algorithm])


def _unsigned(header: dict[str, Any], claims: dict[str, Any]) -> str:
    def part(value: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    return f"{part(header)}.{part(claims)}."


def other_rsa_key(kid: str = "k1") -> RSAKey:
    return RSAKey.generate_key(2048, parameters={"kid": kid}, private=True)


def ec_key(kid: str = "e1") -> ECKey:
    return ECKey.generate_key("P-256", parameters={"kid": kid}, private=True)


def hmac_key(kid: str = "k1") -> OctKey:
    return OctKey.import_key(secrets.token_bytes(32), parameters={"kid": kid})
