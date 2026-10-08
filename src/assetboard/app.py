from __future__ import annotations

from collections.abc import Callable
from importlib.metadata import version
from typing import Any

from fastapi import FastAPI, Request
from starlette.responses import JSONResponse

from assetboard.branding import APP_NAME
from assetboard.config import Settings
from assetboard.logging import configure_logging
from assetboard.middleware import RequestIdMiddleware

DatabaseChecker = Callable[[], bool]


def create_app(
    settings: Settings | None = None,
    *,
    database_checker: DatabaseChecker | None = None,
) -> FastAPI:
    settings = settings or Settings()
    logger = configure_logging(settings.log_level, settings.log_format)
    settings.ensure_secret_key()
    checker = database_checker or (lambda: True)

    app = FastAPI(title=APP_NAME, version=version("assetboard"))
    app.state.settings = settings
    app.state.logger = logger
    app.state.database_checker = checker
    app.add_middleware(RequestIdMiddleware, logger=logger)

    @app.get("/healthz")
    async def healthz(request: Request) -> Any:
        return {"status": "ok", "request_id": request.state.request_id}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        if checker():
            return JSONResponse({"status": "ok", "database": "ready"})
        return JSONResponse(
            {"status": "unavailable", "database": "unavailable"},
            status_code=503,
        )

    return app
