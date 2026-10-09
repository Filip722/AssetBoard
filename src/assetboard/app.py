from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from importlib.metadata import version
from typing import Any

from fastapi import FastAPI, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

from assetboard.branding import APP_NAME
from assetboard.config import Settings
from assetboard.db.engine import create_database_engine, database_is_ready
from assetboard.db.migrate import upgrade_database
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
    database_url = settings.database_url
    if database_url is None:
        raise ValueError("DATABASE_URL was not configured")
    engine = create_database_engine(database_url)
    checker = database_checker or (lambda: database_is_ready(engine))

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            if settings.auto_migrate:
                upgrade_database(settings)
            yield
        finally:
            engine.dispose()

    app = FastAPI(title=APP_NAME, version=version("assetboard"), lifespan=lifespan)
    app.state.settings = settings
    app.state.logger = logger
    app.state.database_engine = engine
    app.state.database_checker = checker
    app.add_middleware(RequestIdMiddleware, logger=logger)

    @app.get("/healthz")
    async def healthz(request: Request) -> Any:
        return {"status": "ok", "request_id": request.state.request_id}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        if await run_in_threadpool(checker):
            return JSONResponse({"status": "ok", "database": "ready"})
        return JSONResponse(
            {"status": "unavailable", "database": "unavailable"},
            status_code=503,
        )

    return app
