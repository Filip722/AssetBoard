from __future__ import annotations

import io
import logging

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import Response

from assetboard.app import create_app
from assetboard.config import Settings
from assetboard.logging import configure_logging
from assetboard.middleware import RequestIdMiddleware


def test_request_id_is_validated_and_returned() -> None:
    app = create_app(Settings(_env_file=None, secret_key="test-secret"))

    with TestClient(app) as client:
        valid = client.get("/healthz", headers={"x-request-id": "trace-abc_123"})
        invalid = client.get("/healthz", headers={"x-request-id": "x" * 129})

    assert valid.headers["x-request-id"] == "trace-abc_123"
    assert valid.json()["request_id"] == "trace-abc_123"
    generated_id = invalid.headers["x-request-id"]
    assert len(generated_id) == 36
    assert invalid.json()["request_id"] == generated_id


def test_request_log_has_safe_path_status_duration_and_request_id() -> None:
    app = create_app(Settings(_env_file=None, secret_key="test-secret"))
    logger: logging.Logger = app.state.logger
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(logging.Formatter("%(request_id)s %(message)s"))
    logger.addHandler(handler)

    try:
        with TestClient(app) as client:
            response = client.get(
                "/readyz?token=do-not-log",
                headers={"x-request-id": "safe-trace"},
            )
            client.get("/healthz")
    finally:
        logger.removeHandler(handler)
        handler.close()

    assert response.status_code == 200
    # Startup also logs (the first-run setup token); only request lines matter here.
    lines = [line for line in output.getvalue().splitlines() if "request_complete" in line]
    assert len(lines) == 1
    assert lines[0].startswith("safe-trace request_complete method=GET path=/readyz ")
    assert "status_code=200" in lines[0]
    assert "duration_ms=" in lines[0]
    assert "do-not-log" not in lines[0]


def test_healthz_suppresses_success_and_warns_on_failure() -> None:
    app = FastAPI()

    @app.get("/healthz")
    async def healthz(unavailable: bool = False) -> Response:
        return Response(status_code=503 if unavailable else 200)

    logger = configure_logging("INFO", "text")
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(logging.Formatter("%(levelname)s %(request_id)s %(message)s"))
    logger.addHandler(handler)

    try:
        with TestClient(RequestIdMiddleware(app, logger)) as client:
            healthy = client.get("/healthz", headers={"x-request-id": "healthy-id"})
            unhealthy = client.get(
                "/healthz?unavailable=true",
                headers={"x-request-id": "unhealthy-id"},
            )
    finally:
        logger.removeHandler(handler)
        handler.close()

    assert healthy.status_code == 200
    assert unhealthy.status_code == 503
    lines = output.getvalue().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("WARNING unhealthy-id request_complete method=GET path=/healthz ")
    assert "status_code=503" in lines[0]


def test_unhandled_exception_logs_traceback_and_returns_request_id() -> None:
    app = create_app(Settings(_env_file=None, secret_key="test-secret"))

    @app.get("/route-that-raises")
    async def route_that_raises() -> None:
        raise RuntimeError("synthetic route failure")

    logger: logging.Logger = app.state.logger
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(logging.Formatter("%(request_id)s %(message)s"))
    logger.addHandler(handler)

    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(
                "/route-that-raises",
                headers={"x-request-id": "failure-trace"},
            )
    finally:
        logger.removeHandler(handler)
        handler.close()

    assert response.status_code == 500
    assert response.headers["x-request-id"] == "failure-trace"
    assert "failure-trace request_complete method=GET path=/route-that-raises " in output.getvalue()
    assert "status_code=500" in output.getvalue()
    assert "Traceback (most recent call last)" in output.getvalue()
    assert "synthetic route failure" in output.getvalue()
