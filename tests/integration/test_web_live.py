"""Behaviour against a real uvicorn server in a background thread."""

from __future__ import annotations

import re
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import event, select
from sqlalchemy.orm import Session, sessionmaker

from assetboard.app import create_app
from assetboard.cli import proxy_header_options
from assetboard.config import Settings
from assetboard.db.engine import create_database_engine
from assetboard.db.models.identity import User
from assetboard.services import auth
from tests.conftest import live_server

SETUP_RACE = "Setup was already completed, or is being completed. Reload to continue."
PASSWORD = "Correct Horse Battery!"


# Forwarded headers come only from TRUSTED_PROXIES


async def echo_client(scope: dict[str, Any], receive: Any, send: Any) -> None:
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            else:
                await send({"type": "lifespan.shutdown.complete"})
                return
    host = scope["client"][0] if scope.get("client") else "none"
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": host.encode()})


@pytest.mark.parametrize(
    ("trusted", "expected"),
    [("", "127.0.0.1"), ("127.0.0.1/32", "203.0.113.9")],
)
def test_forwarded_headers_are_honoured_only_from_trusted_proxies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trusted: str, expected: str
) -> None:
    # Uvicorn's own fallback (env FORWARDED_ALLOW_IPS, else 127.0.0.1) must not apply.
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
    settings = Settings(_env_file=None, data_dir=tmp_path, trusted_proxies=trusted)
    with live_server(echo_client, **proxy_header_options(settings)) as base_url:
        response = httpx.get(base_url, headers={"X-Forwarded-For": "203.0.113.9"})
    assert response.text == expected


# Concurrent first-run setup over HTTP


def _setup_form(client: httpx.Client, base_url: str, token: str, email: str) -> dict[str, str]:
    page = client.get(f"{base_url}/setup")
    csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert csrf is not None
    return {
        "csrf_token": csrf.group(1),
        "setup_token": token,
        "email": email,
        "display_name": "Admin",
        "password": PASSWORD,
    }


@pytest.mark.parametrize("variant", ["waits_for_lock", "lock_timeout"])
def test_concurrent_setup_posts_get_one_success_and_one_409(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variant: str
) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path, secret_key="test-secret")
    app = create_app(settings)
    if variant == "lock_timeout":
        engine = create_database_engine(settings.database_url or "", busy_timeout_ms=50)
        app.state.session_factory = sessionmaker(
            engine, class_=Session, info={"clock": app.state.clock}, expire_on_commit=False
        )
    winner_claimed, release_winner, loser_at_begin = (threading.Event() for _ in range(3))
    real_claim = auth._claim_setup  # pyright: ignore[reportPrivateUsage]

    def claim(session: Session, clock: Any) -> bool:
        won = real_claim(session, clock)
        if not winner_claimed.is_set():
            winner_claimed.set()
            assert release_winner.wait(timeout=20)
        return won

    monkeypatch.setattr(auth, "_claim_setup", claim)
    bind = app.state.session_factory.kw["bind"]

    @event.listens_for(bind, "before_cursor_execute")
    def loser_begins(_conn: object, _cursor: object, statement: str, *_args: object) -> None:
        if statement == "BEGIN IMMEDIATE" and winner_claimed.is_set():
            loser_at_begin.set()

    results: dict[str, httpx.Response] = {}
    with live_server(app) as base_url:
        token = app.state.setup_token
        with httpx.Client() as first, httpx.Client() as second:
            winner_form = _setup_form(first, base_url, token, "winner@example.com")
            loser_form = _setup_form(second, base_url, token, "loser@example.com")

            def post(name: str, client: httpx.Client, form: dict[str, str]) -> None:
                results[name] = client.post(f"{base_url}/setup", data=form)

            winner = threading.Thread(target=post, args=("winner", first, winner_form))
            loser = threading.Thread(target=post, args=("loser", second, loser_form))
            winner.start()
            assert winner_claimed.wait(timeout=20)
            loser.start()
            # The loser is now waiting for the winner's write lock.
            assert loser_at_begin.wait(timeout=20)
            if variant == "lock_timeout":
                loser.join(timeout=20)  # it gives up while the winner still holds the lock
                assert "loser" in results
            release_winner.set()
            winner.join(timeout=20)
            loser.join(timeout=20)
        if variant == "lock_timeout":
            bind.dispose()

    assert results["winner"].status_code == 303
    assert results["loser"].status_code == 409
    assert SETUP_RACE in results["loser"].text
    check = create_database_engine(settings.database_url or "")
    try:
        with sessionmaker(check)() as session:
            assert session.scalars(select(User.email)).all() == ["winner@example.com"]
    finally:
        check.dispose()
