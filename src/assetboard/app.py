from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from importlib.metadata import version
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse, PlainTextResponse, Response

from assetboard.branding import APP_NAME
from assetboard.clock import Clock
from assetboard.config import Settings
from assetboard.db.engine import create_database_engine, database_is_ready
from assetboard.db.migrate import database_is_at_head, upgrade_database
from assetboard.i18n import gettext
from assetboard.logging import configure_logging
from assetboard.middleware import (
    LocaleMiddleware,
    RequestIdMiddleware,
    SecurityHeadersMiddleware,
    add_security_headers,
)
from assetboard.services.auth import (
    derive_subkey,
    prepare_dummy_hash,
    setup_is_open,
    setup_token_at_startup,
)
from assetboard.services.hash_gate import HashGate
from assetboard.web.rendering import render_template
from assetboard.web.routes import router as web_router

DatabaseChecker = Callable[[], bool]


def create_app(
    settings: Settings | None = None,
    *,
    database_checker: DatabaseChecker | None = None,
) -> FastAPI:
    settings = settings or Settings()
    logger = configure_logging(settings.log_level, settings.log_format)
    settings.ensure_data_dir_writable()
    # SECRET_KEY, or the key generated once and persisted under DATA_DIR. Only purpose-specific
    # subkeys leave this function; a restart reads the same file, so tokens stay valid.
    secret_key = settings.ensure_secret_key()
    database_url = settings.database_url
    if database_url is None:
        raise ValueError("DATABASE_URL was not configured")
    engine = create_database_engine(database_url)
    session_factory = sessionmaker(
        engine, class_=Session, info={"clock": Clock()}, expire_on_commit=False
    )
    checker = database_checker or (lambda: database_is_ready(engine))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            if settings.auto_migrate:
                upgrade_database(settings)
            if settings.auto_migrate or database_is_at_head(settings):
                with session_factory() as session:
                    setup_open = setup_is_open(session)
                app.state.setup_token = setup_token_at_startup(
                    settings, setup_open=setup_open, logger=logger
                )
            # Unknown-user logins verify against this; compute it once, before any request.
            await run_in_threadpool(prepare_dummy_hash)
            yield
        finally:
            engine.dispose()

    app = FastAPI(title=APP_NAME, version=version("assetboard"), lifespan=lifespan)
    app.state.settings = settings
    app.state.app_name = APP_NAME
    app.state.logger = logger
    app.state.database_engine = engine
    app.state.session_factory = session_factory
    app.state.csrf_key = derive_subkey(secret_key, "csrf")
    app.state.hash_gate = HashGate(settings.auth_max_concurrent_hashes)
    app.state.setup_token = None
    app.state.database_checker = checker
    app.add_middleware(RequestIdMiddleware, logger=logger)
    app.add_middleware(LocaleMiddleware, default_locale=settings.default_locale)
    app.add_middleware(SecurityHeadersMiddleware)
    static_directory = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_directory), name="static")
    app.include_router(web_router)

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_error(request: Request, exc: StarletteHTTPException) -> Any:
        if exc.status_code not in {403, 404}:
            return await http_exception_handler(request, exc)
        locale = getattr(request.state, "locale", settings.default_locale)
        if exc.status_code == 403:
            title = gettext("Access denied", locale)
            message = gettext("You do not have permission to view this page.", locale)
        else:
            title = gettext("Page not found", locale)
            message = gettext("The page you requested could not be found.", locale)
        is_fragment = _is_htmx_fragment_request(request)
        template_name = "errors/fragment.html" if is_fragment else "errors/page.html"
        response = render_template(
            request,
            template_name,
            context={
                "app_name": APP_NAME,
                "title": title,
                "message": message,
                "status_code": exc.status_code,
            },
            status_code=exc.status_code,
        )
        if exc.headers:
            response.headers.update(exc.headers)
        _add_error_response_headers(response, is_fragment=is_fragment)
        return response

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> Any:
        request_id = getattr(request.state, "request_id", "unknown")
        logger.error(
            "unhandled_request_error request_id=%s exception_type=%s",
            request_id,
            type(exc).__name__,
            exc_info=True,
        )
        locale = getattr(request.state, "locale", settings.default_locale)
        title = gettext("Something went wrong", locale)
        message = gettext("An unexpected error occurred. Please try again.", locale)
        is_fragment = _is_htmx_fragment_request(request)
        try:
            template_name = "errors/fragment.html" if is_fragment else "errors/page.html"
            response = render_template(
                request,
                template_name,
                context={
                    "app_name": APP_NAME,
                    "title": title,
                    "message": message,
                    "status_code": 500,
                },
                status_code=500,
            )
        except Exception:
            response = PlainTextResponse(
                "An unexpected error occurred.",
                status_code=500,
                headers={"x-request-id": request_id},
            )
        _add_error_response_headers(response, is_fragment=is_fragment)
        response.headers["x-request-id"] = request_id
        add_security_headers(response)
        return response

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


def _is_htmx_fragment_request(request: Request) -> bool:
    headers = request.headers
    return (
        headers.get("hx-request", "").lower() == "true"
        and headers.get("hx-boosted", "").lower() != "true"
        and headers.get("hx-history-restore-request", "").lower() != "true"
    )


def _add_error_response_headers(response: Response, *, is_fragment: bool) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Vary"] = "Accept-Language, HX-Request"
    if is_fragment:
        response.headers["HX-Retarget"] = "#toast-container"
        response.headers["HX-Reswap"] = "beforeend"
