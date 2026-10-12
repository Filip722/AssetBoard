from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta
from uuid import uuid4

import pytest

from assetboard.services.auth import derive_subkey
from assetboard.services.oidc_flow import (
    FLOW_LIFETIME,
    FLOW_PURPOSE,
    LINK_PURPOSE,
    FlowState,
    PendingLink,
    seal,
    unseal,
)

KEY = derive_subkey("test-secret", "oidc-flow")
NOW = datetime.fromisoformat("2026-05-01T10:00:00+00:00")
FLOW = FlowState("state-value", "nonce-value", "v" * 64, "/invite/" + "a" * 43)


def test_flow_state_round_trips() -> None:
    assert FlowState.unseal(KEY, FLOW.seal(KEY, now=NOW), now=NOW) == FLOW


def test_pending_link_round_trips() -> None:
    pending = PendingLink(uuid4(), "https://idp.test", "sub", "ola@example.com", None, "/")
    assert PendingLink.unseal(KEY, pending.seal(KEY, now=NOW), now=NOW) == pending


@pytest.mark.parametrize(
    ("offset", "valid"),
    [
        (timedelta(0), True),
        (FLOW_LIFETIME - timedelta(seconds=1), True),
        (FLOW_LIFETIME, False),  # exp is exclusive
        (timedelta(hours=1), False),
        (-timedelta(seconds=59), True),  # small clock skew
        (-timedelta(minutes=5), False),  # issued in the future
    ],
)
def test_iat_and_exp_are_checked_server_side(offset: timedelta, valid: bool) -> None:
    value = FLOW.seal(KEY, now=NOW)
    assert (FlowState.unseal(KEY, value, now=NOW + offset) is not None) is valid


def test_tampering_another_key_or_purpose_is_refused() -> None:
    value = FLOW.seal(KEY, now=NOW)
    body, _, mac = value.partition(".")
    payload = json.loads(base64.urlsafe_b64decode(body + "=="))
    payload["next"] = "/somewhere-else"
    forged = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    assert FlowState.unseal(KEY, f"{forged}.{mac}", now=NOW) is None
    assert FlowState.unseal(derive_subkey("other-secret", "oidc-flow"), value, now=NOW) is None
    # A flow cookie can never be used as a pending link, and the other way round.
    assert unseal(KEY, LINK_PURPOSE, value, now=NOW, lifetime=FLOW_LIFETIME) is None


def test_a_longer_lifetime_than_allowed_is_refused() -> None:
    value = seal(KEY, FLOW_PURPOSE, {"state": "s"}, now=NOW, lifetime=timedelta(days=1))
    assert unseal(KEY, FLOW_PURPOSE, value, now=NOW, lifetime=FLOW_LIFETIME) is None


@pytest.mark.parametrize("value", [None, "", "abc", "abc.def", "ü.x", "a" * 5000])
def test_malformed_values_are_refused(value: str | None) -> None:
    assert FlowState.unseal(KEY, value, now=NOW) is None


def test_missing_fields_are_refused() -> None:
    value = seal(KEY, FLOW_PURPOSE, {"state": "s", "nonce": "n"}, now=NOW, lifetime=FLOW_LIFETIME)
    assert FlowState.unseal(KEY, value, now=NOW) is None
    link = seal(
        KEY,
        LINK_PURPOSE,
        {"user": "not-a-uuid", "iss": "i", "sub": "s", "email": "e", "admin": None, "next": "/"},
        now=NOW,
        lifetime=FLOW_LIFETIME,
    )
    assert PendingLink.unseal(KEY, link, now=NOW) is None


def test_oversized_payloads_cannot_be_sealed() -> None:
    with pytest.raises(ValueError):
        seal(KEY, FLOW_PURPOSE, {"next": "x" * 4000}, now=NOW, lifetime=FLOW_LIFETIME)
