from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from assetboard.config import Settings
from assetboard.services.external_identity import SignInIdentity
from assetboard.services.oidc import (
    ALLOWED_ALGORITHMS,
    AuthorizationRequest,
    OidcClient,
    OidcError,
    create_s256_code_challenge,
    email_verified_claim,
)
from tests.oidc.fake_idp import (
    BASE_URL,
    CLIENT_ID,
    CLIENT_SECRET,
    ISSUER,
    REDIRECT_URI,
    FakeIdp,
    StepClock,
    ec_key,
    hmac_key,
    other_rsa_key,
)


@pytest.fixture
def clock() -> StepClock:
    return StepClock("2026-05-01T10:00:00+00:00")


@pytest.fixture
def idp(clock: StepClock) -> FakeIdp:
    return FakeIdp(clock)


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "data_dir": tmp_path,
        "env": "prod",
        "base_url": BASE_URL,
        "oidc_issuer": ISSUER,
        "oidc_client_id": CLIENT_ID,
        "oidc_client_secret": CLIENT_SECRET,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def make_client(tmp_path: Path, idp: FakeIdp, clock: StepClock, **overrides: Any) -> OidcClient:
    return OidcClient(make_settings(tmp_path, **overrides), idp, clock)


def sign_in(client: OidcClient, idp: FakeIdp, **callback: str) -> SignInIdentity:
    request = client.begin()
    params = idp.authorize(request.url, **callback)
    return complete(client, request, params)


def complete(
    client: OidcClient, request: AuthorizationRequest, params: dict[str, str]
) -> SignInIdentity:
    return client.complete(
        params, state=request.state, nonce=request.nonce, code_verifier=request.code_verifier
    )


def reason(call: Any) -> str:
    with pytest.raises(OidcError) as error:
        call()
    return error.value.reason


# Authorization request


def test_begin_uses_pkce_s256_state_nonce_and_the_base_url_redirect(
    tmp_path: Path, idp: FakeIdp, clock: StepClock
) -> None:
    client = make_client(tmp_path, idp, clock)
    request = client.begin()
    query = {k: v[0] for k, v in parse_qs(urlsplit(request.url).query).items()}
    assert query["code_challenge"] == create_s256_code_challenge(request.code_verifier)
    assert query["code_challenge_method"] == "S256"
    assert query["state"] == request.state and query["nonce"] == request.nonce
    assert query["redirect_uri"] == REDIRECT_URI
    assert query["scope"] == "openid email profile"
    assert len(request.state) >= 43 and len(request.nonce) >= 43
    assert 43 <= len(request.code_verifier) <= 128
    assert request.state != client.begin().state


def test_happy_path_returns_the_identity(tmp_path: Path, idp: FakeIdp, clock: StepClock) -> None:
    identity = sign_in(make_client(tmp_path, idp, clock), idp)
    assert identity == SignInIdentity(
        issuer=ISSUER,
        subject="user-123",
        email="ola@example.com",
        email_verified=True,
        display_name="Ola Nordmann",
        groups=None,
    )
    # No userinfo call when the ID token has everything.
    assert ("GET", f"{ISSUER}/userinfo") not in idp.requests


def test_token_request_sends_the_code_verifier_and_basic_client_auth(
    tmp_path: Path, idp: FakeIdp, clock: StepClock
) -> None:
    client = make_client(tmp_path, idp, clock)
    request = client.begin()
    params = idp.authorize(request.url)
    wrong = AuthorizationRequest(request.url, request.state, request.nonce, "x" * 64)
    assert reason(lambda: complete(client, wrong, params)) == "token_error"
    sent = idp.token_requests[-1]
    assert sent["data"]["code_verifier"] == "x" * 64
    assert sent["headers"]["Authorization"].startswith("Basic ")
    assert "client_secret" not in sent["data"]
    assert sent["data"]["redirect_uri"] == REDIRECT_URI


def test_client_secret_post_and_public_clients(
    tmp_path: Path, idp: FakeIdp, clock: StepClock
) -> None:
    idp.auth_methods = ["client_secret_post"]
    sign_in(make_client(tmp_path, idp, clock), idp)
    assert idp.token_requests[-1]["data"]["client_secret"] == CLIENT_SECRET

    public = FakeIdp(clock, client_secret=None)
    sign_in(make_client(tmp_path, public, clock, oidc_client_secret=None), public)
    sent = public.token_requests[-1]
    assert "Authorization" not in sent["headers"] and "client_secret" not in sent["data"]


def test_a_code_is_single_use(tmp_path: Path, idp: FakeIdp, clock: StepClock) -> None:
    client = make_client(tmp_path, idp, clock)
    request = client.begin()
    params = idp.authorize(request.url)
    complete(client, request, params)
    assert reason(lambda: complete(client, request, params)) == "token_error"


# Callback parameters


def test_state_must_match(tmp_path: Path, idp: FakeIdp, clock: StepClock) -> None:
    client = make_client(tmp_path, idp, clock)
    request = client.begin()
    # A second tab started another flow: its cookie no longer matches this callback.
    other = client.begin()
    params = idp.authorize(request.url)
    assert reason(lambda: complete(client, other, params)) == "state_mismatch"
    assert reason(lambda: complete(client, request, {**params, "state": ""})) == "state_mismatch"
    assert idp.token_requests == []


def test_iss_parameter_is_checked_when_present_or_advertised(
    tmp_path: Path, idp: FakeIdp, clock: StepClock
) -> None:
    client = make_client(tmp_path, idp, clock)
    assert reason(lambda: sign_in(client, idp, iss="https://evil.test")) == "iss_mismatch"
    assert sign_in(client, idp, iss=ISSUER).subject == "user-123"

    idp.discovery_overrides["authorization_response_iss_parameter_supported"] = True
    strict = make_client(tmp_path, idp, clock)
    assert reason(lambda: sign_in(strict, idp)) == "iss_mismatch"


def test_provider_errors_get_fixed_reasons_and_only_the_log_has_the_description(
    tmp_path: Path, idp: FakeIdp, clock: StepClock, caplog: pytest.LogCaptureFixture
) -> None:
    client = make_client(tmp_path, idp, clock)
    request = client.begin()
    state = request.state
    with caplog.at_level(logging.WARNING):
        denied = reason(
            lambda: complete(
                client,
                request,
                {"state": state, "error": "access_denied", "error_description": "<b>nope</b>"},
            )
        )
        other = reason(lambda: complete(client, request, {"state": state, "error": "<script>"}))
    assert (denied, other) == ("access_denied", "idp_error")
    assert "<b>nope</b>" in caplog.text
    assert reason(lambda: complete(client, request, {"state": state})) == "missing_code"


# Discovery


def test_discovery_is_lazy_cached_and_failures_are_cached_briefly(
    tmp_path: Path, idp: FakeIdp, clock: StepClock
) -> None:
    client = make_client(tmp_path, idp, clock)
    assert idp.requests == []  # nothing at construction
    client.begin()
    client.begin()
    discovery = ("GET", f"{ISSUER}/.well-known/openid-configuration")
    assert idp.requests.count(discovery) == 1

    failing = FakeIdp(clock, discovery_status=503)
    client = make_client(tmp_path, failing, clock)
    assert reason(client.begin) == "discovery_failed"
    assert reason(client.begin) == "discovery_failed"
    assert failing.requests.count(discovery) == 1  # negative cache
    failing.discovery_status = 200
    clock.advance(timedelta(seconds=31))
    client.begin()
    assert failing.requests.count(discovery) == 2


@pytest.mark.parametrize(
    "override",
    [
        {"issuer": "https://idp.example.test/realms/other"},
        {"issuer": ISSUER + "/"},
        {"token_endpoint": "http://idp.example.test/token"},
        {"authorization_endpoint": None},
        {"jwks_uri": "ftp://idp.example.test/certs"},
        {"code_challenge_methods_supported": ["plain"]},
    ],
)
def test_discovery_documents_are_validated(
    tmp_path: Path, idp: FakeIdp, clock: StepClock, override: dict[str, Any]
) -> None:
    idp.discovery_overrides = override
    assert reason(make_client(tmp_path, idp, clock).begin) == "discovery_invalid"


def test_http_endpoints_are_allowed_only_in_dev(
    tmp_path: Path, idp: FakeIdp, clock: StepClock
) -> None:
    idp.discovery_overrides = {"userinfo_endpoint": "http://idp.example.test/userinfo"}
    assert reason(make_client(tmp_path, idp, clock).begin) == "discovery_invalid"
    make_client(tmp_path, idp, clock, env="dev").begin()


# ID token


@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("bad_signature", "id_token_invalid"),
        ("alg_none", "id_token_invalid"),
        ("hs256", "id_token_invalid"),
        ("wrong_iss", "id_token_invalid"),
        ("wrong_aud", "id_token_invalid"),
        ("multiple_aud_without_azp", "id_token_invalid"),
        ("wrong_azp", "id_token_invalid"),
        ("expired", "id_token_invalid"),
        ("issued_in_future", "id_token_invalid"),
        ("issued_long_ago", "id_token_invalid"),
        ("no_iat", "id_token_invalid"),
        ("no_sub", "id_token_invalid"),
        ("wrong_nonce", "nonce_mismatch"),
        ("no_nonce", "nonce_mismatch"),
    ],
)
def test_id_token_checks(
    tmp_path: Path, idp: FakeIdp, clock: StepClock, case: str, expected: str
) -> None:
    now = int(clock.now().timestamp())
    match case:
        case "bad_signature":
            idp.sign_with = other_rsa_key()
        case "alg_none":
            idp.algorithm = "none"
        case "hs256":
            idp.algorithm, idp.sign_with = "HS256", hmac_key()
        case "wrong_iss":
            idp.id_token_overrides = {"iss": "https://evil.test"}
        case "wrong_aud":
            idp.id_token_overrides = {"aud": "someone-else"}
        case "multiple_aud_without_azp":
            idp.id_token_overrides = {"aud": [CLIENT_ID, "someone-else"]}
        case "wrong_azp":
            idp.id_token_overrides = {"azp": "someone-else"}
        case "expired":
            idp.id_token_overrides = {"exp": now - 61}
        case "issued_in_future":
            idp.id_token_overrides = {"iat": now + 120}
        case "issued_long_ago":
            idp.id_token_overrides = {"iat": now - 3600}
        case "no_iat":
            idp.id_token_overrides = {"iat": None}
        case "no_sub":
            idp.id_token_overrides = {"sub": ""}
        case "wrong_nonce":
            idp.id_token_overrides = {"nonce": "not-the-nonce"}
        case "no_nonce":
            idp.id_token_overrides = {"nonce": None}
        case _:
            raise AssertionError(case)
    assert reason(lambda: sign_in(make_client(tmp_path, idp, clock), idp)) == expected


def test_azp_with_multiple_audiences_is_accepted(
    tmp_path: Path, idp: FakeIdp, clock: StepClock
) -> None:
    idp.id_token_overrides = {"aud": [CLIENT_ID, "someone-else"], "azp": CLIENT_ID}
    assert sign_in(make_client(tmp_path, idp, clock), idp).subject == "user-123"


def test_only_asymmetric_algorithms_are_allowed() -> None:
    assert "none" not in ALLOWED_ALGORITHMS
    assert not any(algorithm.startswith("HS") for algorithm in ALLOWED_ALGORITHMS)


def test_ec_keys_and_tokens_without_kid_work(
    tmp_path: Path, idp: FakeIdp, clock: StepClock
) -> None:
    key = ec_key()
    idp.key, idp.algorithm = key, "ES256"  # type: ignore[assignment]
    assert sign_in(make_client(tmp_path, idp, clock), idp).subject == "user-123"


def test_unknown_kid_refetches_the_keys_once(
    tmp_path: Path, idp: FakeIdp, clock: StepClock
) -> None:
    client = make_client(tmp_path, idp, clock)
    sign_in(client, idp)
    certs = ("GET", f"{ISSUER}/certs")
    assert idp.requests.count(certs) == 1
    # The provider rotates its key.
    idp.key = other_rsa_key("k2")
    clock.advance(timedelta(seconds=61))
    sign_in(client, idp)
    assert idp.requests.count(certs) == 2
    # A token signed with a key the provider never publishes: one refetch, then refused.
    idp.sign_with = other_rsa_key("k3")
    clock.advance(timedelta(seconds=61))
    assert reason(lambda: sign_in(client, idp)) == "id_token_invalid"
    assert idp.requests.count(certs) == 3


# Userinfo and claims


def test_userinfo_fills_missing_claims_only_when_sub_matches(
    tmp_path: Path, idp: FakeIdp, clock: StepClock
) -> None:
    idp.claims = {}
    idp.userinfo = {"sub": "user-123", "email": "Ola@Example.com", "email_verified": "true"}
    identity = sign_in(make_client(tmp_path, idp, clock), idp)
    assert (identity.email, identity.email_verified) == ("ola@example.com", True)
    assert identity.display_name == "ola"

    idp.userinfo = {"sub": "someone-else", "email": "boss@example.com", "email_verified": True}
    assert reason(lambda: sign_in(make_client(tmp_path, idp, clock), idp)) == (
        "userinfo_sub_mismatch"
    )


def test_email_and_its_verification_come_from_the_same_document(
    tmp_path: Path, idp: FakeIdp, clock: StepClock
) -> None:
    # The ID token has the email but not email_verified; userinfo's verification belongs to a
    # different document, so it is not combined with the ID token's email.
    idp.claims = {"email": "ola@example.com"}
    idp.userinfo = {"sub": "user-123", "email": "other@example.com", "email_verified": True}
    identity = sign_in(make_client(tmp_path, idp, clock), idp)
    assert (identity.email, identity.email_verified) == ("ola@example.com", False)


@pytest.mark.parametrize(
    ("value", "verified"),
    [
        (True, True),
        ("true", True),
        (False, False),
        ("false", False),
        ("True", False),
        ("yes", False),
        (1, False),
        ("1", False),
        (None, False),
        ([], False),
    ],
)
def test_email_verified_is_parsed_strictly(value: object, verified: bool) -> None:
    assert email_verified_claim(value) is verified


@pytest.mark.parametrize(("value", "verified"), [(True, True), ("true", True), ("1", False)])
def test_email_verified_in_a_sign_in(
    tmp_path: Path, idp: FakeIdp, clock: StepClock, value: object, verified: bool
) -> None:
    idp.claims["email_verified"] = value
    assert sign_in(make_client(tmp_path, idp, clock), idp).email_verified is verified


def test_missing_email_verified_is_false(tmp_path: Path, idp: FakeIdp, clock: StepClock) -> None:
    del idp.claims["email_verified"]
    idp.userinfo = {"sub": "user-123"}
    assert sign_in(make_client(tmp_path, idp, clock), idp).email_verified is False


@pytest.mark.parametrize(
    ("claims", "groups", "warned"),
    [
        ({"groups": ["family", "money-admins"]}, ("family", "money-admins"), False),
        ({"groups": []}, (), False),
        ({}, None, True),
        ({"groups": "money-admins"}, None, True),
        ({"groups": ["ok", 3]}, None, True),
    ],
)
def test_groups_claim(
    tmp_path: Path,
    idp: FakeIdp,
    clock: StepClock,
    caplog: pytest.LogCaptureFixture,
    claims: dict[str, Any],
    groups: tuple[str, ...] | None,
    warned: bool,
) -> None:
    idp.claims.update(claims)
    idp.userinfo = {"sub": "user-123"}
    client = make_client(tmp_path, idp, clock, oidc_admin_group="money-admins")
    with caplog.at_level(logging.WARNING):
        identity = sign_in(client, idp)
    assert identity.groups == groups
    assert ("groups claim" in caplog.text) is warned


def test_groups_are_ignored_without_an_admin_group(
    tmp_path: Path, idp: FakeIdp, clock: StepClock
) -> None:
    idp.claims["groups"] = ["money-admins"]
    assert sign_in(make_client(tmp_path, idp, clock), idp).groups is None


def test_invalid_email_claim_is_no_email(tmp_path: Path, idp: FakeIdp, clock: StepClock) -> None:
    idp.claims["email"] = "not an email"
    identity = sign_in(make_client(tmp_path, idp, clock), idp)
    assert (identity.email, identity.email_verified) == (None, False)


def test_end_session_url(tmp_path: Path, idp: FakeIdp, clock: StepClock) -> None:
    url = make_client(tmp_path, idp, clock).end_session_url()
    assert url is not None and url.startswith(f"{ISSUER}/logout?")
    query = parse_qs(urlsplit(url).query)
    assert query == {"client_id": [CLIENT_ID], "post_logout_redirect_uri": [f"{BASE_URL}/login"]}
    idp.discovery_overrides = {"end_session_endpoint": None}
    assert make_client(tmp_path, idp, clock).end_session_url() is None


@pytest.mark.parametrize(
    ("claims", "expected"),
    [
        ({"name": "Ola\nNordmann"}, "Ola Nordmann"),
        ({"name": "\u202eanmdroN alO"}, "anmdroN alO"),  # RTL override removed
        ({"name": "Ola\u200b  \t Nordmann "}, "Ola Nordmann"),
        ({"name": "", "preferred_username": "ola"}, "ola"),
        ({"name": "\u200b\x00", "preferred_username": "\u202e"}, "ola"),  # email local part
        ({"name": "x" * 300}, "x" * 120),
    ],
)
def test_display_names_are_cleaned(
    tmp_path: Path, idp: FakeIdp, clock: StepClock, claims: dict[str, Any], expected: str
) -> None:
    idp.claims.update(claims)
    assert sign_in(make_client(tmp_path, idp, clock), idp).display_name == expected


def test_display_name_falls_back_to_user(tmp_path: Path, idp: FakeIdp, clock: StepClock) -> None:
    idp.claims = {"name": "\u202e"}
    idp.userinfo = {"sub": "user-123"}
    assert sign_in(make_client(tmp_path, idp, clock), idp).display_name == "User"


def test_provider_text_cannot_forge_log_lines(
    tmp_path: Path, idp: FakeIdp, clock: StepClock, caplog: pytest.LogCaptureFixture
) -> None:
    client = make_client(tmp_path, idp, clock)
    request = client.begin()
    forged = "nope\n2026-05-01 INFO request_complete path=/admin status_code=200\r\u2028x"
    with caplog.at_level(logging.WARNING), pytest.raises(OidcError) as error:
        complete(
            client,
            request,
            {"state": request.state, "error": "access_denied", "error_description": forged},
        )
    messages = [record.getMessage() for record in caplog.records]
    assert messages and all("\n" not in m and "\r" not in m for m in messages)
    assert all("\u2028" not in m for m in messages)
    assert "request_complete" in messages[0]  # kept, but inside the one line
    assert error.value.detail == "access_denied"
    assert OidcError("x", "a\nb\x1b").detail == "a b"


def test_the_urllib_transport_honours_proxy_variables_and_never_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import urllib.request

    from assetboard.services.oidc import UrllibTransport

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    transport = UrllibTransport()
    opener: Any = transport._opener  # pyright: ignore[reportPrivateUsage]
    handlers: list[object] = list(opener.handlers)
    proxies: list[Any] = [h for h in handlers if isinstance(h, urllib.request.ProxyHandler)]
    assert proxies and proxies[0].proxies.get("https") == "http://proxy.internal:3128"
    redirects = [h for h in handlers if isinstance(h, urllib.request.HTTPRedirectHandler)]
    assert len(redirects) == 1 and type(redirects[0]).__name__ == "_NoRedirects"


def test_errors_before_the_provider_accepted_the_code_are_not_auditable(
    tmp_path: Path, idp: FakeIdp, clock: StepClock
) -> None:
    client = make_client(tmp_path, idp, clock)
    request = client.begin()
    params = idp.authorize(request.url)
    cases = [
        {**params, "state": "forged"},
        {**params, "iss": "https://evil.test"},
        {"state": request.state, "error": "access_denied"},
        {"state": request.state},
        {**params, "code": "junk"},  # the token endpoint answers invalid_grant
    ]
    for case in cases:
        with pytest.raises(OidcError) as error:
            complete(client, request, case)
        assert not error.value.auditable, case
    assert len(idp.token_requests) == 1  # only the junk code reached the provider
    assert idp.token_requests[0]["data"]["code"] == "junk"
    idp.id_token_overrides = {"aud": "someone-else"}
    with pytest.raises(OidcError) as late:
        complete(client, request, params)
    assert late.value.reason == "id_token_invalid" and late.value.auditable
