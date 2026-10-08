from __future__ import annotations

from importlib.metadata import version

from fastapi.testclient import TestClient

from assetboard.app import create_app
from assetboard.config import Settings


def test_app_factory_ensures_secret_key_after_setup() -> None:
    settings = Settings(_env_file=None)
    assert settings.secret_key is None

    app = create_app(settings, database_checker=lambda: True)

    assert settings.secret_key
    assert (settings.data_dir / "secret_key").read_text(encoding="utf-8") == settings.secret_key
    assert app.state.settings is settings
    assert app.version == version("assetboard")


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
