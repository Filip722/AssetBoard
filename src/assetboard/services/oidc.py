"""OIDC relying party: discovery, authorization request, code exchange, ID token checks.

F1.3, ADR-0014. The provider is reached through ``HttpTransport``: ``UrllibTransport`` (stdlib,
so ``SSL_CERT_FILE`` works for private CAs) in production, an in-process fake IdP in tests.

Authlib builds the authorization URL and the token request body and computes the PKCE (S256)
challenge. joserfc verifies signatures. The algorithm allowlist and every claim check are ours:
``iss``, ``aud``, ``azp``, ``exp``, ``iat``, ``nonce``, ``sub``, and ``sub`` again on userinfo.

Errors carry a stable ``reason`` for the audit log and the web layer. Tokens and codes are never
logged; the provider's ``error_description`` is logged (truncated) but never shown.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import re
import secrets
import ssl
import threading
import unicodedata
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol, cast
from urllib.parse import parse_qsl, urlencode, urlsplit

from authlib.oauth2 import rfc7636
from authlib.oauth2.rfc6749 import parameters as oauth2_parameters
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet

from assetboard.branding import APP_NAME
from assetboard.clock import Clock
from assetboard.config import Settings
from assetboard.domain.identity import InvalidEmail, normalize_email

logger = logging.getLogger(f"{APP_NAME.lower()}.oidc")

# Authlib has no type hints; these are the only parts of it we call.
_authlib_parameters: Any = oauth2_parameters
_authlib_pkce: Any = rfc7636
prepare_grant_uri = cast(Callable[..., str], _authlib_parameters.prepare_grant_uri)
prepare_token_request = cast(Callable[..., str], _authlib_parameters.prepare_token_request)
create_s256_code_challenge = cast(Callable[[str], str], _authlib_pkce.create_s256_code_challenge)

HTTP_TIMEOUT_SECONDS = 10
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_ID_TOKEN_LENGTH = 16 * 1024
DISCOVERY_TTL = timedelta(hours=1)
DISCOVERY_FAILURE_TTL = timedelta(seconds=30)
JWKS_TTL = timedelta(hours=1)
JWKS_REFRESH_INTERVAL = timedelta(seconds=60)
CLOCK_SKEW = timedelta(seconds=60)
ID_TOKEN_MAX_AGE = timedelta(minutes=10)
MAX_SUBJECT_LENGTH = 255
# Asymmetric only: "none" and HMAC (which a public JWKS key could forge) are never accepted.
ALLOWED_ALGORITHMS = (
    "RS256",
    "RS384",
    "RS512",
    "PS256",
    "PS384",
    "PS512",
    "ES256",
    "ES384",
    "ES512",
    "EdDSA",
)
_KEY_TYPES = {"RS": "RSA", "PS": "RSA", "ES": "EC", "Ed": "OKP"}
_ERROR_CODE = re.compile(r"[a-z_]{1,64}")


class OidcError(Exception):
    """A sign-in that cannot continue. ``reason`` is a stable code; nothing secret is in it."""

    def __init__(self, reason: str, detail: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        # Safe to log: may echo provider text (an error code), never with control characters.
        self.detail = log_safe(detail) if detail else None


# Transport


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes
    content_type: str


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        data: Mapping[str, str] | None = None,
    ) -> HttpResponse: ...


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None  # the 3xx is returned as an error response and refused below


class UrllibTransport:
    """Stdlib HTTP client: 10 s timeouts, no redirects, responses capped at 1 MiB.

    The default SSL context uses the system trust store and honours ``SSL_CERT_FILE``.
    """

    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(
            _NoRedirects(), urllib.request.HTTPSHandler(context=ssl.create_default_context())
        )

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        data: Mapping[str, str] | None = None,
    ) -> HttpResponse:
        body = urlencode(data).encode("ascii") if data is not None else None
        request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
        if body is not None:
            request.add_header("Content-Type", "application/x-www-form-urlencoded")
        request.add_header("User-Agent", APP_NAME)
        try:
            with self._opener.open(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                return HttpResponse(
                    response.status,
                    response.read(MAX_RESPONSE_BYTES + 1),
                    response.headers.get_content_type(),
                )
        except urllib.error.HTTPError as error:
            content = error.read(MAX_RESPONSE_BYTES + 1)
            return HttpResponse(error.code, content, error.headers.get_content_type())
        except (OSError, ValueError) as error:
            raise OidcError("provider_unreachable", type(error).__name__) from error


# Provider metadata


@dataclass(frozen=True, slots=True)
class ProviderMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str
    userinfo_endpoint: str | None
    end_session_endpoint: str | None
    token_endpoint_auth_methods: tuple[str, ...]
    iss_parameter_supported: bool


@dataclass(frozen=True, slots=True)
class AuthorizationRequest:
    """Where to send the browser, and what the flow cookie must remember."""

    url: str
    state: str
    nonce: str
    code_verifier: str


@dataclass(frozen=True, slots=True)
class OidcIdentity:
    """What a validated sign-in tells us. ``groups`` is None when the claim is unusable."""

    issuer: str
    subject: str
    email: str | None
    email_verified: bool
    display_name: str
    groups: tuple[str, ...] | None


def email_verified_claim(value: object) -> bool:
    """Strict: only boolean true or the string "true" count."""
    return value is True or value == "true"


def _json_object(response: HttpResponse, reason: str) -> dict[str, Any]:
    if len(response.body) > MAX_RESPONSE_BYTES:
        raise OidcError(reason, "response too large")
    try:
        value = json.loads(response.body)
    except (ValueError, UnicodeDecodeError) as error:
        raise OidcError(reason, "invalid JSON") from error
    if not isinstance(value, dict):
        raise OidcError(reason, "not a JSON object")
    return cast(dict[str, Any], value)


class OidcClient:
    """One configured provider. Thread-safe; caches discovery and keys in memory."""

    def __init__(self, settings: Settings, transport: HttpTransport, clock: Clock) -> None:
        if settings.oidc_issuer is None or settings.oidc_client_id is None:
            raise ValueError("OIDC is not configured")
        self._settings = settings
        self._issuer = settings.oidc_issuer
        self._client_id = settings.oidc_client_id
        self._transport = transport
        self._clock = clock
        self._lock = threading.Lock()
        self._metadata: ProviderMetadata | None = None
        self._metadata_expires: datetime | None = None
        self._failure_until: datetime | None = None
        self._keys: KeySet | None = None
        self._keys_expires: datetime | None = None
        self._keys_fetched: datetime | None = None

    @property
    def issuer(self) -> str:
        return self._issuer

    # Discovery

    def _check_url(self, value: object, name: str, *, required: bool = True) -> str | None:
        if value is None and not required:
            return None
        if not isinstance(value, str):
            raise OidcError("discovery_invalid", f"{name} missing")
        try:
            parts = urlsplit(value)
        except ValueError as error:
            raise OidcError("discovery_invalid", f"{name} invalid") from error
        schemes = {"https", "http"} if self._settings.env == "dev" else {"https"}
        if parts.scheme not in schemes or not parts.hostname or parts.fragment:
            raise OidcError("discovery_invalid", f"{name} must be an https URL")
        return value

    def metadata(self) -> ProviderMetadata:
        now = self._clock.now()
        with self._lock:
            if self._metadata and self._metadata_expires and now < self._metadata_expires:
                return self._metadata
            if self._failure_until and now < self._failure_until:
                raise OidcError("discovery_failed", "recent failure cached")
        try:
            metadata = self._discover()
        except OidcError:
            with self._lock:
                self._failure_until = now + DISCOVERY_FAILURE_TTL
            raise
        with self._lock:
            self._metadata = metadata
            self._metadata_expires = now + DISCOVERY_TTL
            self._failure_until = None
        return metadata

    def _discover(self) -> ProviderMetadata:
        url = self._issuer.rstrip("/") + "/.well-known/openid-configuration"
        response = self._transport.request("GET", url, headers={"Accept": "application/json"})
        if response.status != 200:
            raise OidcError("discovery_failed", f"HTTP {response.status}")
        document = _json_object(response, "discovery_invalid")
        # Exact match (mix-up protection): the document must describe the configured issuer.
        if document.get("issuer") != self._issuer:
            raise OidcError("discovery_invalid", "issuer mismatch")
        methods = document.get("code_challenge_methods_supported")
        if isinstance(methods, list) and "S256" not in methods:
            raise OidcError("discovery_invalid", "S256 PKCE not supported")
        auth_methods = document.get("token_endpoint_auth_methods_supported")
        return ProviderMetadata(
            issuer=self._issuer,
            authorization_endpoint=cast(
                str, self._check_url(document.get("authorization_endpoint"), "authorization")
            ),
            token_endpoint=cast(str, self._check_url(document.get("token_endpoint"), "token")),
            jwks_uri=cast(str, self._check_url(document.get("jwks_uri"), "jwks_uri")),
            userinfo_endpoint=self._check_url(
                document.get("userinfo_endpoint"), "userinfo", required=False
            ),
            end_session_endpoint=self._check_url(
                document.get("end_session_endpoint"), "end_session", required=False
            ),
            token_endpoint_auth_methods=tuple(
                method for method in cast(list[object], auth_methods) if isinstance(method, str)
            )
            if isinstance(auth_methods, list)
            else ("client_secret_basic",),
            iss_parameter_supported=document.get("authorization_response_iss_parameter_supported")
            is True,
        )

    # Keys

    def _fetch_keys(self, jwks_uri: str) -> KeySet:
        response = self._transport.request("GET", jwks_uri, headers={"Accept": "application/json"})
        if response.status != 200:
            raise OidcError("jwks_failed", f"HTTP {response.status}")
        document = _json_object(response, "jwks_failed")
        keys = document.get("keys")
        if not isinstance(keys, list):
            raise OidcError("jwks_failed", "no keys")
        usable: list[object] = [
            key
            for key in cast(list[object], keys)
            if isinstance(key, dict)
            and cast(dict[str, Any], key).get("kty") in {"RSA", "EC", "OKP"}
            and cast(dict[str, Any], key).get("use", "sig") == "sig"
        ]
        try:
            return KeySet.import_key_set({"keys": cast(Any, usable)})
        except (JoseError, ValueError, TypeError) as error:
            raise OidcError("jwks_failed", "invalid key set") from error

    def _key_set(self, jwks_uri: str, *, refresh: bool) -> KeySet:
        now = self._clock.now()
        with self._lock:
            fresh = self._keys is not None and self._keys_expires and now < self._keys_expires
            recently = self._keys_fetched and now - self._keys_fetched < JWKS_REFRESH_INTERVAL
            if self._keys is not None and fresh and (not refresh or recently):
                return self._keys
        keys = self._fetch_keys(jwks_uri)
        with self._lock:
            self._keys = keys
            self._keys_expires = now + JWKS_TTL
            self._keys_fetched = now
        return keys

    @staticmethod
    def _pick_key(keys: KeySet, header: Mapping[str, Any]) -> Any:
        algorithm = header.get("alg")
        if algorithm not in ALLOWED_ALGORITHMS:
            raise OidcError("id_token_invalid", "algorithm not allowed")
        key_type = _KEY_TYPES[str(algorithm)[:2]]
        kid = header.get("kid")
        candidates = [
            key for key in keys.keys if key.key_type == key_type and (kid is None or key.kid == kid)
        ]
        if len(candidates) != 1:
            return None
        return candidates[0]

    # Flow

    def begin(self) -> AuthorizationRequest:
        """A fresh authorization request: state, nonce and a PKCE verifier (S256)."""
        metadata = self.metadata()
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(48)
        url = prepare_grant_uri(
            metadata.authorization_endpoint,
            client_id=self._client_id,
            response_type="code",
            redirect_uri=self._settings.oidc_redirect_uri,
            scope=self._settings.oidc_scopes,
            state=state,
            nonce=nonce,
            code_challenge=create_s256_code_challenge(code_verifier),
            code_challenge_method="S256",
        )
        return AuthorizationRequest(url, state, nonce, code_verifier)

    def complete(
        self,
        params: Mapping[str, str],
        *,
        state: str,
        nonce: str,
        code_verifier: str,
    ) -> OidcIdentity:
        """Check the callback against the flow cookie's values, then exchange and validate."""
        metadata = self.metadata()
        returned_state = params.get("state", "")
        if not hmac.compare_digest(returned_state.encode(), state.encode()):
            raise OidcError("state_mismatch")
        # RFC 9207: when the provider says who answered, it must be our issuer.
        if "iss" in params:
            if params["iss"] != self._issuer:
                raise OidcError("iss_mismatch")
        elif metadata.iss_parameter_supported:
            raise OidcError("iss_mismatch", "iss parameter missing")
        if "error" in params:
            code = params["error"] if _ERROR_CODE.fullmatch(params["error"]) else "invalid"
            # Logged, never shown: the description is provider-controlled text.
            logger.warning(
                "OIDC provider returned an error: %s (%r)",
                code,
                log_safe(params.get("error_description", "")),
            )
            raise OidcError("access_denied" if code == "access_denied" else "idp_error", code)
        code = params.get("code")
        if not code or len(code) > 2048:
            raise OidcError("missing_code")
        tokens = self._exchange(metadata, code, code_verifier)
        claims = self._validate_id_token(metadata, tokens["id_token"], nonce)
        return self._identity(metadata, claims, tokens.get("access_token"))

    def _exchange(self, metadata: ProviderMetadata, code: str, verifier: str) -> dict[str, str]:
        secret = self._settings.oidc_client_secret
        headers = {"Accept": "application/json"}
        extra: dict[str, str] = {"client_id": self._client_id, "code_verifier": verifier}
        if secret is not None:
            if "client_secret_basic" in metadata.token_endpoint_auth_methods:
                # RFC 6749 §2.3.1: both parts are form-encoded before Basic encoding.
                pair = f"{_form(self._client_id)}:{_form(secret.get_secret_value())}"
                basic = base64.b64encode(pair.encode("utf-8")).decode("ascii")
                headers["Authorization"] = f"Basic {basic}"
            else:
                extra["client_secret"] = secret.get_secret_value()
        body = prepare_token_request(
            "authorization_code",
            code=code,
            redirect_uri=self._settings.oidc_redirect_uri,
            **extra,
        )
        data = dict(_parse_form(body))
        response = self._transport.request(
            "POST", metadata.token_endpoint, headers=headers, data=data
        )
        document = _json_object(response, "token_error")
        if response.status != 200:
            error = document.get("error")
            code_name = error if isinstance(error, str) and _ERROR_CODE.fullmatch(error) else None
            logger.warning("OIDC token request failed: HTTP %s %s", response.status, code_name)
            raise OidcError("token_error", code_name)
        id_token = document.get("id_token")
        if not isinstance(id_token, str) or not id_token or len(id_token) > MAX_ID_TOKEN_LENGTH:
            raise OidcError("token_error", "no id_token")
        tokens = {"id_token": id_token}
        access_token = document.get("access_token")
        if isinstance(access_token, str) and access_token:
            tokens["access_token"] = access_token
        return tokens

    def _decode(self, metadata: ProviderMetadata, id_token: str) -> dict[str, Any]:
        for refresh in (False, True):
            keys = self._key_set(metadata.jwks_uri, refresh=refresh)
            chosen: list[Any] = []

            def resolve(guest: Any, keys: KeySet = keys, chosen: list[Any] = chosen) -> Any:
                key = self._pick_key(keys, guest.headers())
                if key is None:
                    raise OidcError("unknown_key")
                chosen.append(key)
                return key

            try:
                token = jwt.decode(id_token, resolve, algorithms=list(ALLOWED_ALGORITHMS))
            except OidcError as error:
                if error.reason == "unknown_key" and not refresh:
                    continue  # the provider may have rotated its keys: fetch them once more
                raise OidcError("id_token_invalid", error.reason) from error
            except (JoseError, ValueError, TypeError) as error:
                raise OidcError("id_token_invalid", type(error).__name__) from error
            return dict(token.claims)
        raise OidcError("id_token_invalid", "unknown key")  # pragma: no cover - loop returns

    def _validate_id_token(
        self, metadata: ProviderMetadata, id_token: str, nonce: str
    ) -> dict[str, Any]:
        claims = self._decode(metadata, id_token)
        now = self._clock.now().timestamp()
        skew = CLOCK_SKEW.total_seconds()

        def fail(detail: str) -> OidcError:
            return OidcError("id_token_invalid", detail)

        if claims.get("iss") != self._issuer:
            raise fail("iss")
        audience = claims.get("aud")
        audiences = (
            [audience]
            if isinstance(audience, str)
            else cast(list[object], audience)
            if isinstance(audience, list)
            else []
        )
        if self._client_id not in audiences:
            raise fail("aud")
        azp = claims.get("azp")
        if (len(audiences) > 1 or azp is not None) and azp != self._client_id:
            raise fail("azp")
        exp, iat = claims.get("exp"), claims.get("iat")
        if not _is_number(exp) or cast(float, exp) <= now - skew:
            raise fail("exp")
        if not _is_number(iat):
            raise fail("iat")
        issued = cast(float, iat)
        if issued > now + skew or issued < now - ID_TOKEN_MAX_AGE.total_seconds() - skew:
            raise fail("iat")
        returned_nonce = claims.get("nonce")
        if not isinstance(returned_nonce, str) or not hmac.compare_digest(
            returned_nonce.encode(), nonce.encode()
        ):
            raise OidcError("nonce_mismatch")
        subject = claims.get("sub")
        if not isinstance(subject, str) or not subject or len(subject) > MAX_SUBJECT_LENGTH:
            raise fail("sub")
        return claims

    def _userinfo(
        self, metadata: ProviderMetadata, access_token: str, subject: str
    ) -> dict[str, Any]:
        assert metadata.userinfo_endpoint is not None
        response = self._transport.request(
            "GET",
            metadata.userinfo_endpoint,
            headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"},
        )
        if response.status != 200:
            raise OidcError("userinfo_failed", f"HTTP {response.status}")
        if response.content_type == "application/jwt":
            raise OidcError("userinfo_failed", "signed userinfo is not supported")
        document = _json_object(response, "userinfo_failed")
        # OIDC Core §5.3.2: userinfo claims are only usable when sub matches the ID token.
        if document.get("sub") != subject:
            raise OidcError("userinfo_sub_mismatch")
        return document

    def _identity(
        self, metadata: ProviderMetadata, claims: dict[str, Any], access_token: str | None
    ) -> OidcIdentity:
        subject = cast(str, claims["sub"])
        groups_claim = self._settings.oidc_groups_claim
        wants_groups = self._settings.oidc_admin_group is not None
        missing = (
            "email" not in claims
            or "email_verified" not in claims
            or (wants_groups and groups_claim not in claims)
        )
        userinfo: dict[str, Any] = {}
        if missing and metadata.userinfo_endpoint and access_token:
            userinfo = self._userinfo(metadata, access_token, subject)

        # Email and its verification always come from the same document.
        source = claims if "email" in claims else userinfo
        email: str | None = None
        raw_email = source.get("email")
        if isinstance(raw_email, str):
            try:
                email = normalize_email(raw_email)
            except InvalidEmail:
                email = None
        verified = email is not None and email_verified_claim(source.get("email_verified"))

        groups: tuple[str, ...] | None = None
        if wants_groups:
            group_source = claims if groups_claim in claims else userinfo
            raw_groups = group_source.get(groups_claim)
            if isinstance(raw_groups, list) and all(
                isinstance(group, str) for group in cast(list[object], raw_groups)
            ):
                groups = tuple(cast(list[str], raw_groups))
            else:
                state = "absent" if groups_claim not in group_source else "not a list of strings"
                logger.warning(
                    "OIDC groups claim %r is %s; instance admin status is unchanged",
                    groups_claim,
                    state,
                )

        return OidcIdentity(
            issuer=self._issuer,
            subject=subject,
            email=email,
            email_verified=verified,
            display_name=_display_name(claims, userinfo, email),
            groups=groups,
        )

    def end_session_url(self) -> str | None:
        """RP-initiated logout target, or None if the provider has none (or is unreachable)."""
        try:
            metadata = self.metadata()
        except OidcError:
            return None
        if metadata.end_session_endpoint is None:
            return None
        return _add_params(
            metadata.end_session_endpoint,
            {
                "client_id": self._client_id,
                "post_logout_redirect_uri": f"{self._settings.base_url}/login",
            },
        )


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _form(value: str) -> str:
    return urlencode({"": value})[1:]


def _parse_form(body: str) -> list[tuple[str, str]]:
    return parse_qsl(body, keep_blank_values=True, strict_parsing=True)


def _add_params(url: str, params: Mapping[str, str]) -> str:
    separator = "&" if urlsplit(url).query else "?"
    return f"{url}{separator}{urlencode(params)}"


def clean_text(value: str, limit: int) -> str:
    """Drop control and format characters (Unicode Cc, Cf) and collapse whitespace."""
    kept = "".join(
        " " if character.isspace() else character
        for character in value
        if unicodedata.category(character) not in {"Cc", "Cf"} or character.isspace()
    )
    return " ".join(kept.split())[:limit].strip()


def log_safe(value: str, limit: int = 200) -> str:
    """Provider text for a log line: never a newline or other control character."""
    return clean_text(value, limit)


def _display_name(claims: Mapping[str, Any], userinfo: Mapping[str, Any], email: str | None) -> str:
    """Always 1 to 120 printable characters, so account creation cannot reject it."""
    candidates: list[object] = [
        source.get(name) for source in (claims, userinfo) for name in ("name", "preferred_username")
    ]
    if email:
        candidates.append(email.split("@", 1)[0])
    for value in candidates:
        if isinstance(value, str):
            cleaned = clean_text(value, 120)
            if cleaned:
                return cleaned
    return "User"
