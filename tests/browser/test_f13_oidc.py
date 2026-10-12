"""OIDC sign-in and RP-initiated logout in Chromium, against the fake provider (F1.3 part B).

The browser talks to a tiny HTTP front of ``FakeIdp`` on a second loopback port (the
authorization and end-session pages). The app's own calls to the provider (discovery, keys,
token) reach the same ``FakeIdp`` object through the injected transport: no network beyond
loopback, as in the other browser test.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from playwright.sync_api import expect, sync_playwright

from assetboard.app import create_app
from assetboard.config import Settings
from assetboard.services.auth import run_initial_setup
from assetboard.services.context import Context, Role, UserRef, WorkspaceRef
from assetboard.services.oidc import OidcClient
from assetboard.services.workspaces import create_invitation
from tests.oidc.fake_idp import CLIENT_ID, CLIENT_SECRET, FakeIdp, StepClock

pytestmark = pytest.mark.skipif(
    os.environ.get("ASSETBOARD_RUN_BROWSER_TESTS") != "1",
    reason="Set ASSETBOARD_RUN_BROWSER_TESTS=1 to run the Chromium tests",
)


def _listener() -> socket.socket:
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    return listener


@contextmanager
def _serve(app: FastAPI, listener: socket.socket) -> Iterator[None]:
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        threading.Event().wait(0.01)
    assert server.started
    try:
        yield
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        listener.close()


def _provider_front(idp: FakeIdp, logouts: list[dict[str, list[str]]]) -> FastAPI:
    front = FastAPI()

    @front.get("/auth")
    async def authorize(request: Request) -> RedirectResponse:
        # The provider's login page: the user is already signed in there and consents.
        params = idp.authorize(str(request.url))
        return RedirectResponse(f"{idp.redirect_uri}?{urlencode(params)}", status_code=302)

    @front.get("/logout")
    async def end_session(request: Request) -> RedirectResponse:
        query = parse_qs(urlsplit(str(request.url)).query)
        logouts.append(query)
        return RedirectResponse(query["post_logout_redirect_uri"][0], status_code=302)

    return front


def test_f13_oidc_sign_in_and_provider_logout(tmp_path: Path) -> None:
    app_listener, idp_listener = _listener(), _listener()
    base = f"http://127.0.0.1:{app_listener.getsockname()[1]}"
    issuer = f"http://127.0.0.1:{idp_listener.getsockname()[1]}"
    app = create_app(
        Settings(
            _env_file=None,
            data_dir=tmp_path,
            database_url=f"sqlite:///{tmp_path / 'browser.db'}",
            secret_key="browser-test-key",
            env="dev",  # http issuer and BASE_URL on loopback
            base_url=base,
            oidc_issuer=issuer,
            oidc_client_id=CLIENT_ID,
            oidc_client_secret=CLIENT_SECRET,
            oidc_provider_name="Fake IdP",
            oidc_auto_create=True,
            oidc_rp_logout=True,
        )
    )
    clock = StepClock(datetime.now(UTC).isoformat())
    idp = FakeIdp(clock, issuer=issuer, base=issuer, redirect_uri=f"{base}/auth/oidc/callback")
    idp.claims = {"email": "oidc@example.com", "email_verified": True, "name": "Olga OIDC"}
    app.state.oidc = OidcClient(app.state.settings, idp, clock)
    logouts: list[dict[str, list[str]]] = []

    with _serve(app, app_listener), _serve(_provider_front(idp, logouts), idp_listener):
        admin, workspace = run_initial_setup(
            app.state.session_factory,
            email="admin@example.com",
            display_name="Admin",
            password="Correct Horse Battery!",
            workspace_name="Home",
            settings=app.state.settings,
            clock=app.state.clock,
        )
        owner = Context(
            user=UserRef(admin.id, admin.email, admin.display_name, "en", True),
            workspace=WorkspaceRef(
                workspace.id,
                workspace.name,
                workspace.base_currency,
                workspace.locale,
                workspace.timezone,
                workspace.country_code,
            ),
            role=Role.OWNER,
            clock=app.state.clock,
        )
        token = create_invitation(
            app.state.session_factory, owner, role="viewer", email=None, expires_in_days=7
        ).token
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()

            # Static files loaded by /login and by an invite page carry no Referer (the page
            # URL can hold the invitation token), with the OIDC button on both pages. Each page
            # gets a fresh page and context (browser.new_page), so nothing comes from a cache.
            for path in ("/login", f"/invite/{token}"):
                visitor = browser.new_page()
                static_requests: list[tuple[str, str | None]] = []
                visitor.on(
                    "request",
                    lambda request, seen=static_requests: (
                        seen.append((request.url, request.all_headers().get("referer")))
                        if "/static/" in request.url
                        else None
                    ),
                )
                visitor.goto(f"{base}{path}", wait_until="networkidle")
                expect(visitor.get_by_role("link", name="Continue with Fake IdP")).to_be_visible()
                urls = {url.split("?")[0].removeprefix(base) for url, _referer in static_requests}
                assert urls >= {"/static/css/app.css", "/static/js/theme.js"}, (path, urls)
                assert all(referer is None for _url, referer in static_requests), (
                    path,
                    static_requests,
                )
                visitor.close()

            page = browser.new_page()
            callback_responses: list[int] = []
            page.on(
                "response",
                lambda response: (
                    callback_responses.append(response.status)
                    if "/auth/oidc/callback" in response.url
                    else None
                ),
            )

            page.goto(f"{base}/login", wait_until="networkidle")
            page.get_by_role("link", name="Continue with Fake IdP").click()
            page.wait_for_url(f"{base}/")
            # Auto-created with its own workspace, named after the person.
            expect(page.locator("[data-current-workspace]")).to_have_text("Olga OIDC")
            # The callback answered with a redirect, never a page.
            assert callback_responses == [303]
            assert "code=" not in page.url

            page.locator("details summary").click()
            page.get_by_role("button", name="Sign out", exact=True).click()
            # The interstitial continues to the provider, which sends the browser back.
            page.wait_for_url(f"{base}/login")
            assert len(logouts) == 1
            assert logouts[0]["client_id"] == [CLIENT_ID]
            assert logouts[0]["post_logout_redirect_uri"] == [f"{base}/login"]
            # Signed out here too.
            page.goto(f"{base}/", wait_until="networkidle")
            assert page.url.startswith(f"{base}/login")
            browser.close()
