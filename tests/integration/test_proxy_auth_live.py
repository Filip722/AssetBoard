"""Proxy sign-in through a real uvicorn server, wired exactly as ``serve`` runs it.

The server listens on 127.0.0.1. Clients connect from other loopback addresses (Linux routes
all of 127.0.0.0/8 to loopback): 127.0.0.2 is the trusted proxy, 127.0.0.3 is not.
"""

from __future__ import annotations

import socket
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from assetboard.app import create_app
from assetboard.cli import serve_app, server_options
from assetboard.config import Settings
from assetboard.db.models.identity import User, UserSession
from assetboard.services.auth import run_initial_setup
from tests.conftest import live_server

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


def _browser(source: str) -> httpx.Client:
    # A browser loading pages: only navigations are sent to /auth/proxy.
    return httpx.Client(
        transport=httpx.HTTPTransport(local_address=source),
        follow_redirects=True,
        timeout=10,
        headers={"Accept": "text/html", "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate"},
    )


def test_identity_headers_count_only_from_the_trusted_tcp_peer(tmp_path: Path) -> None:
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

    def sessions() -> list[tuple[str, str | None]]:
        with app.state.session_factory() as session:
            rows = session.execute(
                select(UserSession.auth_method, UserSession.ip_prefix).order_by(
                    UserSession.created_at
                )
            )
            return [(method, prefix) for method, prefix in rows]

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

        # An excluded peer: the headers are never read, also when it claims to be forwarded by
        # the trusted proxy.
        for extra in ({}, {"X-Forwarded-For": TRUSTED_PEER, "X-Forwarded-Proto": "https"}):
            with _browser(EXCLUDED_PEER) as outsider:
                page = outsider.get(f"{base_url}/workspace/members", headers=IDENTITY | extra)
            assert page.url.path == "/login" and "Continue as" not in page.text
        assert sessions() == []

        # The trusted proxy without the header: signed out.
        with _browser(TRUSTED_PEER) as proxy:
            assert proxy.get(f"{base_url}/").url.path == "/login"

        # The trusted proxy with the header: through /auth/proxy and the cookie check, back to
        # the page, signed in as a new account.
        with _browser(TRUSTED_PEER) as proxy:
            page = proxy.get(f"{base_url}/workspace/members", headers=IDENTITY)
            assert page.status_code == 200 and page.url.path == "/workspace/members"
            hops = [response.url.path for response in page.history]
            assert hops == ["/workspace/members", "/auth/proxy", "/auth/proxy/continue"]
        assert sessions() == [("proxy", "127.0.0.0/24")]

        # Forwarded headers from the proxy still set the client address (stored as its
        # prefix), while trust stays with the TCP peer.
        with _browser(TRUSTED_PEER) as proxy:
            page = proxy.get(f"{base_url}/", headers=IDENTITY | {"X-Forwarded-For": "198.51.100.7"})
            assert page.status_code == 200
        assert sessions()[-1] == ("proxy", "198.51.100.0/24")

    with app.state.session_factory() as session:
        emails = session.scalars(select(User.email).order_by(User.email)).all()
    assert emails == ["admin@example.com", "ola@example.com"]
