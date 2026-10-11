from __future__ import annotations

import asyncio
import io
import logging
import re
from importlib.metadata import version
from pathlib import Path

import pytest
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi import HTTPException
from fastapi.testclient import TestClient

from assetboard.app import create_app
from assetboard.clock import Clock
from assetboard.config import Settings
from assetboard.db.migrate import alembic_config
from assetboard.services.auth import derive_subkey, run_initial_setup


def test_app_factory_ensures_secret_key_after_setup() -> None:
    settings = Settings(_env_file=None)
    assert settings.secret_key is None

    app = create_app(settings, database_checker=lambda: True)

    assert settings.secret_key
    assert (settings.data_dir / "secret_key").read_text(encoding="utf-8") == settings.secret_key
    assert app.state.settings is settings
    assert app.version == version("assetboard")


def test_app_runs_migrations_during_lifespan_not_factory(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path, secret_key="test-secret")
    app = create_app(settings)
    engine = app.state.database_engine

    with engine.connect() as connection:
        tables = (
            connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'")
            .scalars()
            .all()
        )
    assert "alembic_version" not in tables

    with TestClient(app), engine.connect() as connection:
        version_rows = (
            connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalars().all()
        )
        current_heads = MigrationContext.configure(connection).get_current_heads()

    assert current_heads == tuple(ScriptDirectory.from_config(alembic_config(settings)).get_heads())
    assert version_rows == list(current_heads)


def test_health_endpoints_report_process_and_database() -> None:
    app = create_app(
        Settings(_env_file=None, secret_key="test-secret"),
        database_checker=lambda: True,
    )

    with TestClient(app) as client:
        health = client.get("/healthz")
        ready = client.get("/readyz")

    assert health.status_code == 200
    assert health.json() == {"status": "ok", "request_id": health.headers["x-request-id"]}
    assert ready.status_code == 200
    assert ready.json() == {"status": "ok", "database": "ready"}


def test_readyz_reports_database_failure() -> None:
    app = create_app(
        Settings(_env_file=None, secret_key="test-secret"),
        database_checker=lambda: False,
    )

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "database": "unavailable"}


def test_readyz_checks_the_real_database(tmp_path: Path) -> None:
    app = create_app(Settings(_env_file=None, data_dir=tmp_path, secret_key="test-secret"))

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ready"}


def test_readyz_runs_synchronous_checker_in_threadpool() -> None:
    def check_not_on_event_loop() -> bool:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return True
        return False

    app = create_app(
        Settings(_env_file=None, secret_key="test-secret", auto_migrate=False),
        database_checker=check_not_on_event_loop,
    )

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 200


def test_root_renders_translatable_shell_and_local_assets() -> None:
    app = create_app(
        Settings(_env_file=None, secret_key="test-secret", auto_migrate=False),
    )

    with TestClient(app) as client:
        response = client.get("/", headers={"accept-language": "pl-PL, en;q=0.8"})
        htmx = client.get("/static/vendor/htmx.min.js")
        alpine = client.get("/static/vendor/alpinejs-csp.min.js")
        charts = client.get("/static/vendor/echarts.min.js")

    assert response.status_code == 200
    assert '<html lang="pl" class="h-full">' in response.text
    assert "Podsumowanie miesiąca" in response.text
    assert '"allowEval":false' in response.text
    assert '"allowScriptTags":false' in response.text
    assert '"includeIndicatorStyles":false' in response.text
    assert '"code":"[45]..","swap":true,"error":true' in response.text
    assert "</main>" in response.text
    assert response.text.index("</main>") < response.text.index('id="toast-container"')
    assert response.text.index("</main>") < response.text.index('<dialog id="confirm-dialog"')
    asset_urls = re.findall(r'(?:src|href)="([^"]+)"', response.text)
    local_assets = [url for url in asset_urls if url.startswith("/static/")]
    assert len(local_assets) == 5
    assert all(url.startswith("/static/") for url in local_assets)
    assert all(url.endswith(f"?v={version('assetboard')}") for url in local_assets)
    assert 'href="/"' in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "accept-language" in response.headers["vary"].lower()
    assert "unsafe-inline" not in response.headers["content-security-policy"]
    assert "unsafe-eval" not in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert all(asset.status_code == 200 for asset in (htmx, alpine, charts))
    assert "no-store" not in htmx.headers.get("cache-control", "")


def test_http_errors_are_localized_and_htmx_aware() -> None:
    app = create_app(Settings(_env_file=None, secret_key="test-secret", auto_migrate=False))

    @app.get("/_test/forbidden")
    async def forbidden() -> None:
        raise HTTPException(status_code=403)

    @app.get("/_test/error")
    async def unexpected_error() -> None:
        raise RuntimeError("Synthetic failure")

    @app.get("/_test/teapot")
    async def teapot() -> None:
        raise HTTPException(status_code=418, detail="Teapot", headers={"X-Teapot": "retained"})

    with TestClient(app, raise_server_exceptions=False) as client:
        forbidden_response = client.get("/_test/forbidden", headers={"accept-language": "pl"})
        partial_response = client.get(
            "/missing",
            headers={"HX-Request": "true", "accept-language": "pl"},
        )
        boosted = client.get(
            "/missing",
            headers={"HX-Request": "true", "HX-Boosted": "true", "accept-language": "pl"},
        )
        history_restore = client.get(
            "/missing",
            headers={
                "HX-Request": "true",
                "HX-History-Restore-Request": "true",
                "accept-language": "pl",
            },
        )
        server_error = client.get(
            "/_test/error",
            headers={"accept-language": "pl", "x-request-id": "f0-4-error"},
        )
        partial_server_error = client.get(
            "/_test/error",
            headers={"HX-Request": "true", "accept-language": "pl"},
        )
        other_http_error = client.get("/_test/teapot")

    assert forbidden_response.status_code == 403
    assert "Brak dostępu" in forbidden_response.text
    assert "Nie masz uprawnień" in forbidden_response.text
    assert "<!doctype html>" in forbidden_response.text.lower()
    assert partial_response.status_code == 404
    assert '<div data-toast role="alert"' in partial_response.text
    assert "Nie znaleziono strony" in partial_response.text
    assert "<!doctype html>" not in partial_response.text.lower()
    assert partial_response.headers["hx-retarget"] == "#toast-container"
    assert partial_response.headers["hx-reswap"] == "beforeend"
    assert "hx-request" in partial_response.headers["vary"].lower()
    assert "accept-language" in partial_response.headers["vary"].lower()
    assert partial_response.headers["cache-control"] == "no-store"
    assert "<!doctype html>" in boosted.text.lower()
    assert "<!doctype html>" in history_restore.text.lower()
    assert server_error.status_code == 500
    assert "Coś poszło nie tak" in server_error.text
    assert server_error.headers["x-request-id"] == "f0-4-error"
    assert server_error.headers["cache-control"] == "no-store"
    assert "accept-language" in server_error.headers["vary"].lower()
    assert "<!doctype html>" in server_error.text.lower()
    assert partial_server_error.status_code == 500
    assert "Wystąpił nieoczekiwany błąd" in partial_server_error.text
    assert '<div data-toast role="alert"' in partial_server_error.text
    assert partial_server_error.headers["hx-retarget"] == "#toast-container"
    assert "<!doctype html>" not in partial_server_error.text.lower()
    assert other_http_error.status_code == 418
    assert other_http_error.json() == {"detail": "Teapot"}
    assert other_http_error.headers["x-teapot"] == "retained"
    assert server_error.headers["content-security-policy"]


def test_500_uses_plain_text_fallback_when_template_rendering_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import assetboard.app as app_module

    def fail_rendering(*args: object, **kwargs: object) -> None:
        raise RuntimeError("Synthetic template failure")

    monkeypatch.setattr(app_module, "render_template", fail_rendering)
    app = create_app(Settings(_env_file=None, secret_key="test-secret", auto_migrate=False))

    @app.get("/_test/failing-render")
    async def failing_render() -> None:
        raise RuntimeError("Synthetic request failure")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/_test/failing-render", headers={"x-request-id": "fallback-id"})

    assert response.status_code == 500
    assert response.text == "An unexpected error occurred."
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["x-request-id"] == "fallback-id"
    assert response.headers["cache-control"] == "no-store"


def test_static_and_home_urls_include_root_path() -> None:
    app = create_app(Settings(_env_file=None, secret_key="test-secret", auto_migrate=False))

    with TestClient(app, root_path="/x") as client:
        response = client.get("/")
        missing = client.get("/missing")

    assert response.status_code == 200
    assert 'href="/x/"' in response.text
    assert 'href="/x/static/css/app.css?v=' in response.text
    assert missing.status_code == 404
    assert 'href="/x/"' in missing.text


def _capture_app_log(app: object) -> tuple[logging.Logger, io.StringIO, logging.Handler]:
    logger: logging.Logger = app.state.logger  # type: ignore[attr-defined]
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setLevel(logging.WARNING)
    logger.addHandler(handler)
    return logger, output, handler


def test_setup_token_is_generated_and_logged_once_at_startup_while_setup_is_open(
    tmp_path: Path,
) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path, secret_key="test-secret")
    app = create_app(settings)
    logger, output, handler = _capture_app_log(app)
    try:
        assert app.state.setup_token is None
        with TestClient(app):
            # Before any request has been made.
            token = app.state.setup_token
            assert token is not None and len(token) >= 43
            assert output.getvalue().count("Generated first-run setup token") == 1
            assert token in output.getvalue()
    finally:
        logger.removeHandler(handler)

    restarted = create_app(Settings(_env_file=None, data_dir=tmp_path, secret_key="test-secret"))
    logger, output, handler = _capture_app_log(restarted)
    try:
        with TestClient(restarted):
            assert restarted.state.setup_token not in (None, token)
            assert output.getvalue().count("Generated first-run setup token") == 1
            run_initial_setup(
                restarted.state.session_factory,
                email="admin@example.com",
                display_name="Admin",
                password="Correct Horse Battery!",
                workspace_name="Home",
                settings=settings,
                clock=Clock(),
            )
    finally:
        logger.removeHandler(handler)

    closed = create_app(Settings(_env_file=None, data_dir=tmp_path, secret_key="test-secret"))
    logger, output, handler = _capture_app_log(closed)
    try:
        with TestClient(closed):
            assert closed.state.setup_token is None
            assert "setup token" not in output.getvalue()
    finally:
        logger.removeHandler(handler)


def test_configured_setup_token_is_used_at_startup_and_never_logged(tmp_path: Path) -> None:
    configured = "operator-configured-setup-token"
    app = create_app(
        Settings(_env_file=None, data_dir=tmp_path, secret_key="s", setup_token=configured)
    )
    logger, output, handler = _capture_app_log(app)
    logger.setLevel(logging.DEBUG)
    handler.setLevel(logging.DEBUG)
    try:
        with TestClient(app):
            assert app.state.setup_token == configured
    finally:
        logger.removeHandler(handler)
    assert configured not in output.getvalue()


def test_csrf_key_is_a_subkey_of_the_persisted_secret_and_survives_restart(
    tmp_path: Path,
) -> None:
    def csrf_key() -> bytes:
        app = create_app(Settings(_env_file=None, data_dir=tmp_path), database_checker=lambda: True)
        app.state.database_engine.dispose()
        return app.state.csrf_key

    first = csrf_key()
    secret = (tmp_path / "secret_key").read_text(encoding="utf-8")
    assert first == derive_subkey(secret, "csrf")
    assert secret.encode() not in first
    assert csrf_key() == first  # restart: same key file, same subkey

    (tmp_path / "secret_key").unlink()
    assert csrf_key() != first  # lost key file: outstanding tokens stop verifying

    explicit = create_app(
        Settings(_env_file=None, data_dir=tmp_path, secret_key="operator-secret"),
        database_checker=lambda: True,
    )
    explicit.state.database_engine.dispose()
    assert explicit.state.csrf_key == derive_subkey("operator-secret", "csrf")


def test_dummy_hash_is_computed_once_at_startup_before_any_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from assetboard.services import auth

    monkeypatch.setattr(auth, "_cached_dummy_hashes", {})
    app = create_app(Settings(_env_file=None, data_dir=tmp_path, secret_key="test-secret"))
    with TestClient(app):
        cached = dict(auth._cached_dummy_hashes)  # pyright: ignore[reportPrivateUsage]
        assert len(cached) == 1
        assert next(iter(cached.values())).startswith("$argon2id$")
        auth.prepare_dummy_hash()  # later callers reuse it
        assert auth._cached_dummy_hashes == cached  # pyright: ignore[reportPrivateUsage]
