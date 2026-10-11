"""The access gate: schema state, first-run setup and sign-in (middleware order included)."""

from __future__ import annotations

import io
import logging
import sqlite3
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient

from assetboard.db.migrate import upgrade_database
from assetboard.services.auth import create_admin_cli as create_admin_service

if TYPE_CHECKING:
    from conftest import WebHarness

ALWAYS_OPEN = ["/healthz", "/readyz", "/static/css/app.css", "/static/js/app.js"]


def test_open_gate_redirects_everything_to_setup_except_exemptions(web: WebHarness) -> None:
    with TestClient(web.app()) as client:
        for path in ALWAYS_OPEN:
            assert client.get(path).status_code == 200, path
        assert client.get("/setup").status_code == 200
        for path in ["/", "/login", "/register", "/account/password", "/nope"]:
            response = client.get(path, follow_redirects=False)
            assert (response.status_code, response.headers["location"]) == (303, "/setup"), path
        htmx = client.get("/", headers={"HX-Request": "true"}, follow_redirects=False)
        assert (htmx.status_code, htmx.headers["hx-redirect"]) == (204, "/setup")


def test_closed_gate_lets_signed_out_visitors_reach_only_the_public_pages(
    web: WebHarness,
) -> None:
    app = web.app(allow_registration=True)
    with TestClient(app) as client:
        web.complete_setup(app)
        for path in ALWAYS_OPEN:
            assert client.get(path).status_code == 200, path
        assert client.get("/login").status_code == 200
        assert client.get("/register").status_code == 200
        assert client.get("/setup").status_code == 404
        response = client.get("/account/password?tab=1", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login?next=%2Faccount%2Fpassword%3Ftab%3D1"


def test_next_is_added_only_for_get_navigations(web: WebHarness) -> None:
    app = web.app()
    with TestClient(app) as client:
        web.complete_setup(app)
        post = client.post("/account/logout-others", follow_redirects=False)
        assert (post.status_code, post.headers["location"]) == (303, "/login")


def test_signed_out_htmx_fragment_is_sent_to_plain_login(web: WebHarness) -> None:
    app = web.app()
    with TestClient(app) as client:
        web.complete_setup(app)
        response = client.get(
            "/account/password?tab=1", headers={"HX-Request": "true"}, follow_redirects=False
        )
        assert response.status_code == 204
        assert response.headers["hx-redirect"] == "/login"
        assert "location" not in response.headers
        # A boosted navigation is a full page: it keeps ``next``.
        boosted = client.get(
            "/account/password",
            headers={"HX-Request": "true", "HX-Boosted": "true"},
            follow_redirects=False,
        )
        assert boosted.headers["location"] == "/login?next=%2Faccount%2Fpassword"


def test_registration_page_is_404_when_disabled(web: WebHarness) -> None:
    app = web.app()
    with TestClient(app) as client:
        web.complete_setup(app)
        assert client.get("/register").status_code == 404


@pytest.mark.parametrize("closed_by", ["web", "cli"])
def test_setup_is_404_once_closed(web: WebHarness, closed_by: str) -> None:
    app = web.app()
    with TestClient(app) as client:
        assert client.get("/setup").status_code == 200
        if closed_by == "web":
            web.complete_setup(app)
        else:
            with app.state.session_factory.begin() as session:
                create_admin_service(
                    session,
                    email="cli@example.com",
                    display_name="CLI",
                    password=web.admin_password,
                    settings=app.state.settings,
                    clock=app.state.clock,
                )
        assert client.get("/setup").status_code == 404
        assert client.post("/setup", data={}).status_code in {403, 404}


def test_gate_caches_only_the_closed_state(
    web: WebHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from assetboard.web import gate

    calls: list[bool] = []
    real = gate.setup_is_open

    def counting(session: object) -> bool:
        result = real(session)  # type: ignore[arg-type]
        calls.append(result)
        return result

    monkeypatch.setattr(gate, "setup_is_open", counting)
    app = web.app()
    with TestClient(app) as client:
        client.get("/", follow_redirects=False)
        client.get("/", follow_redirects=False)
        assert calls == [True, True]  # "open" is never cached
        web.complete_setup(app)
        client.get("/", follow_redirects=False)
        client.get("/", follow_redirects=False)
        assert calls == [True, True, False]  # "closed" is cached


def _capture(app: object) -> tuple[logging.Logger, io.StringIO, logging.Handler]:
    logger: logging.Logger = app.state.logger  # type: ignore[attr-defined]
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    logger.addHandler(handler)
    return logger, output, handler


def test_schema_behind_with_auto_migrate_off(web: WebHarness) -> None:
    app = web.app(auto_migrate=False)
    logger, output, handler = _capture(app)
    try:
        with TestClient(app) as client:
            assert app.state.schema_state == "behind"
            assert app.state.setup_token is None
            assert client.get("/healthz").status_code == 200
            ready = client.get("/readyz")
            assert (ready.status_code, ready.json()) == (
                503,
                {"status": "unavailable", "database": "schema_outdated"},
            )
            assert client.get("/static/css/app.css").status_code == 200
            page = client.get("/")
            assert page.status_code == 503
            assert "assetboard migrate" in page.text and "restart" in page.text
            assert page.headers["retry-after"] == "30"
            htmx = client.get("/", headers={"HX-Request": "true"})
            assert htmx.status_code == 503 and htmx.headers["hx-retarget"] == "#toast-container"
            assert "assetboard migrate" in htmx.text
    finally:
        logger.removeHandler(handler)
    assert "setup token" not in output.getvalue()
    assert "AUTO_MIGRATE is off" in output.getvalue()


def test_schema_newer_than_code(web: WebHarness) -> None:
    settings = web.settings()
    upgrade_database(settings)
    database = settings.data_dir / "assetboard.db"
    connection = sqlite3.connect(database)
    connection.execute("UPDATE alembic_version SET version_num = '9999_future'")
    connection.commit()
    connection.close()

    app = web.app()  # AUTO_MIGRATE is on, but a newer database is never "upgraded"
    with TestClient(app) as client:
        assert app.state.schema_state == "ahead"
        assert app.state.setup_token is None
        ready = client.get("/readyz")
        assert (ready.status_code, ready.json()["database"]) == (503, "schema_newer")
        page = client.get("/login")
        assert page.status_code == 503
        assert "newer version" in page.text
    connection = sqlite3.connect(database)
    assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
        "9999_future",
    )
    connection.close()


def test_gate_answers_before_the_csrf_dependency(web: WebHarness) -> None:
    """Middleware order: schema 503 and setup 303 come before a CSRF 403."""
    behind = web.app(auto_migrate=False)
    with TestClient(behind) as client:
        assert client.post("/login", data={}).status_code == 503
    open_setup = web.app()
    with TestClient(open_setup) as client:
        response = client.post("/login", data={}, follow_redirects=False)
        assert (response.status_code, response.headers["location"]) == (303, "/setup")
        # Signed out after setup: /login is public, so the CSRF dependency answers.
        web.complete_setup(open_setup)
        assert client.post("/login", data={}).status_code == 403
