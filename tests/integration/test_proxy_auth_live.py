"""Proxy identity headers through a real uvicorn server, wired exactly as ``serve`` runs it.

The server listens on 127.0.0.1. Clients connect from other loopback addresses (Linux routes
all of 127.0.0.0/8 to loopback): 127.0.0.2 is the trusted proxy, 127.0.0.3 is not. Sign-in
routes arrive in F1.4 part B, so a test-only route runs the part A pieces in order: the recorded
peer (``proxy_peer_trusted``), the headers (``read_identity_headers``) and the shared account
mapping (``sign_in_external``).
"""

from __future__ import annotations

import http.client
import json
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi import Request
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool

from assetboard.app import create_app
from assetboard.cli import serve_app, server_options
from assetboard.config import Settings
from assetboard.db.models.identity import UserSession
from assetboard.services.auth import run_initial_setup
from assetboard.services.oidc_accounts import sign_in_external
from assetboard.services.proxy_auth import read_identity_headers
from assetboard.web import gate
from assetboard.web.security import client_host, proxy_peer_trusted
from tests.conftest import live_server

PROBE = "/test/proxy-sign-in"
TRUSTED_PEER = "127.0.0.2"
EXCLUDED_PEER = "127.0.0.3"
IDENTITY = {"Remote-User": "ola", "Remote-Email": "ola@example.com"}


def _loopback_alias_works() -> bool:
    try:
        with socket.socket() as probe:
            probe.bind((EXCLUDED_PEER, 0))
    except OSError:
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _loopback_alias_works(), reason="needs 127.0.0.0/8 on loopback (Linux)"
)


def _probe(request: Request) -> dict[str, Any]:
    state = request.app.state
    result: dict[str, Any] = {
        "trusted": proxy_peer_trusted(request),
        "client": client_host(request),
        "scheme": request.url.scheme,
    }
    if not result["trusted"]:
        return result
    parsed = read_identity_headers(request.scope["headers"], state.settings)
    result["headers"] = parsed.status
    return result | ({"identity": parsed.identity} if parsed.identity else {})


def _get(base_url: str, source: str, headers: dict[str, str]) -> dict[str, Any]:
    parts = urlsplit(base_url)
    connection = http.client.HTTPConnection(
        parts.hostname or "", parts.port, timeout=10, source_address=(source, 0)
    )
    try:
        connection.request("GET", PROBE, headers=headers)
        response = connection.getresponse()
        assert response.status == 200, response.status
        return json.loads(response.read())
    finally:
        connection.close()


def test_identity_headers_count_only_from_the_trusted_tcp_peer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        _env_file=None,
        data_dir=tmp_path,
        secret_key="live-proxy-test",
        trusted_proxies=f"{TRUSTED_PEER}/32",
        proxy_auth_enabled=True,
        proxy_auth_user_header="Remote-User",
        proxy_auth_email_header="Remote-Email",
        proxy_auth_auto_create=True,
    )
    app = create_app(settings)
    monkeypatch.setattr(gate, "SIGNED_OUT_PATHS", gate.SIGNED_OUT_PATHS | {PROBE})

    @app.get(PROBE)
    async def probe(request: Request) -> dict[str, Any]:  # pyright: ignore[reportUnusedFunction]
        result = _probe(request)
        identity = result.pop("identity", None)
        if identity is not None:
            outcome = await run_in_threadpool(
                lambda: sign_in_external(
                    app.state.session_factory,
                    identity,
                    next_path="/",
                    settings=settings,
                    clock=app.state.clock,
                    host=client_host(request),
                )
            )
            result["status"] = outcome.status
        return result

    with live_server(serve_app(app, settings), **server_options(settings)) as base_url:
        run_initial_setup(
            app.state.session_factory,
            email="admin@example.com",
            display_name="Admin",
            password="Correct Horse Battery!",
            workspace_name="Home",
            settings=settings,
            clock=app.state.clock,
        )

        # The trusted proxy: the headers sign in (an account is created).
        signed_in = _get(base_url, TRUSTED_PEER, IDENTITY)
        assert signed_in == {
            "trusted": True,
            "client": TRUSTED_PEER,
            "scheme": "http",
            "headers": "ok",
            "status": "signed_in",
        }

        # Forwarded headers from the proxy still set the client address and scheme, while
        # trust stays with the TCP peer.
        forwarded = _get(
            base_url,
            TRUSTED_PEER,
            IDENTITY | {"X-Forwarded-For": "198.51.100.7", "X-Forwarded-Proto": "https"},
        )
        assert forwarded["trusted"] and forwarded["status"] == "signed_in"
        assert (forwarded["client"], forwarded["scheme"]) == ("198.51.100.7", "https")

        # The trusted proxy without the header: nothing to sign in with.
        assert _get(base_url, TRUSTED_PEER, {})["headers"] == "absent"

        # An excluded peer: the headers are never read.
        assert _get(base_url, EXCLUDED_PEER, IDENTITY) == {
            "trusted": False,
            "client": EXCLUDED_PEER,
            "scheme": "http",
        }

        # An excluded peer claiming to be forwarded by the trusted proxy changes nothing:
        # neither trust nor its own client address.
        spoofed = _get(
            base_url,
            EXCLUDED_PEER,
            IDENTITY | {"X-Forwarded-For": TRUSTED_PEER, "X-Forwarded-Proto": "https"},
        )
        assert spoofed == {"trusted": False, "client": EXCLUDED_PEER, "scheme": "http"}

    with app.state.session_factory() as session:
        methods = session.scalars(select(UserSession.auth_method)).all()
    assert sorted(methods) == ["proxy", "proxy"]  # the two trusted sign-ins only
