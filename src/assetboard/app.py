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

from assetboard.branding import APP_NAME, CLI_NAME
from assetboard.clock import Clock
from assetboard.config import Settings
from assetboard.db.engine import create_database_engine, database_is_ready
from assetboard.db.migrate import current_revisions, schema_state, upgrade_database
from assetboard.db.repositories import NotFound
from assetboard.i18n import gettext
from assetboard.logging import configure_logging
from assetboard.middleware import (
    LocaleMiddleware,
    RequestIdMiddleware,
    SecurityHeadersMiddleware,
    add_security_headers,
)
from assetboard.services.admin_users import NotInstanceAdmin
from assetboard.services.auth import (
    derive_subkey,
    prepare_dummy_hash,
    setup_is_open,
    setup_token_at_startup,
)
from assetboard.services.context import NoWorkspace, PermissionDenied, WorkspaceNotAvailable
from assetboard.services.hash_gate import HashGate, HashGateBusy
from assetboard.services.instance_settings import workspace_creation_allowed
from assetboard.services.months import MonthClosed
from assetboard.services.oidc import OidcClient, UrllibTransport
from assetboard.services.oidc_flow import ConsumedStates
from assetboard.services.proxy_auth import RecentKeys
from assetboard.web.account_routes import router as account_router
from assetboard.web.admin_routes import router as admin_router
from assetboard.web.audit_routes import router as audit_router
from assetboard.web.auth_routes import router as auth_router
from assetboard.web.category_routes import router as category_router
from assetboard.web.errors import BUSY_RETRY_AFTER, add_error_response_headers, error_response
from assetboard.web.errors import is_htmx_fragment_request as _is_htmx_fragment_request
from assetboard.web.gate import AccessGateMiddleware, OriginCheckMiddleware
from assetboard.web.invite_routes import router as invite_router
from assetboard.web.month_routes import MONTH_CLOSED_MESSAGE, MONTH_CLOSED_TITLE
from assetboard.web.month_routes import router as month_router
from assetboard.web.oidc_routes import router as oidc_router
from assetboard.web.plan_routes import router as plan_router
from assetboard.web.proxy_routes import router as proxy_router
from assetboard.web.rendering import render_template
from assetboard.web.routes import router as web_router
from assetboard.web.security import CsrfFailed, web_session
from assetboard.web.transaction_routes import router as transaction_router
from assetboard.web.workspace_routes import router as workspace_router

# Short lock wait for the best-effort session activity write; a busy database skips it.
TOUCH_BUSY_TIMEOUT_MS = 250
SCHEMA_READINESS = {"behind": "schema_outdated", "ahead": "schema_newer"}

DatabaseChecker = Callable[[], bool]


def create_app(
    settings: Settings | None = None,
    *,
    database_checker: DatabaseChecker | None = None,
) -> FastAPI:
    settings = settings or Settings()
    logger = configure_logging(settings.log_level, settings.log_format)
    settings.ensure_data_dir_writable()
    if settings.proxy_auth_enabled:
        # Accepted by validation (at most /24 or /64), but every address in it can sign in as
        # anyone through the identity headers (ADR-0015).
        for network in settings.wide_trusted_proxies():
            logger.warning(
                "TRUSTED_PROXIES entry %s covers more than one address; with proxy "
                "authentication every host in it can sign in as anyone. Prefer the proxy's own "
                "address.",
                network,
            )
    # SECRET_KEY, or the key generated once and persisted under DATA_DIR. Only purpose-specific
    # subkeys leave this function; a restart reads the same file, so tokens stay valid.
    secret_key = settings.ensure_secret_key()
    database_url = settings.database_url
    if database_url is None:
        raise ValueError("DATABASE_URL was not configured")
    clock = Clock()
    engine = create_database_engine(database_url)
    touch_engine = create_database_engine(database_url, busy_timeout_ms=TOUCH_BUSY_TIMEOUT_MS)
    session_factory = sessionmaker(
        engine, class_=Session, info={"clock": clock}, expire_on_commit=False
    )
    touch_factory = sessionmaker(
        touch_engine, class_=Session, info={"clock": clock}, expire_on_commit=False
    )
    checker = database_checker or (lambda: database_is_ready(engine))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        try:
            # Decided once per process start; a restart picks up an upgraded database.
            state = schema_state(settings)
            if state == "behind" and settings.auto_migrate:
                before = current_revisions(settings)
                upgrade_database(settings)
                state = "current"
                # One line, so an operator can see that the upgrade happened (and from where).
                logger.info(
                    "database migrations applied at startup: %s -> %s",
                    ",".join(before) or "empty",
                    ",".join(current_revisions(settings)),
                )
            app.state.schema_state = state
            if state == "current":
                with session_factory() as session:
                    setup_open = setup_is_open(session)
                app.state.setup_token = setup_token_at_startup(
                    settings, setup_open=setup_open, logger=logger
                )
            elif state == "behind":
                logger.warning(
                    "database schema is behind this version and AUTO_MIGRATE is off; "
                    "run '%s migrate', then restart",
                    CLI_NAME,
                )
            else:
                logger.error("database schema is newer than this version of %s", APP_NAME)
            # Unknown-user logins verify against this; compute it once, before any request.
            await run_in_threadpool(prepare_dummy_hash)
            yield
        finally:
            engine.dispose()
            touch_engine.dispose()

    app = FastAPI(title=APP_NAME, version=version("assetboard"), lifespan=lifespan)
    app.state.settings = settings
    app.state.app_name = APP_NAME
    app.state.logger = logger
    app.state.database_engine = engine
    app.state.clock = clock
    app.state.session_factory = session_factory
    app.state.touch_factory = touch_factory
    app.state.schema_state = None  # decided in the lifespan
    app.state.csrf_key = derive_subkey(secret_key, "csrf")
    app.state.hash_gate = HashGate(settings.auth_max_concurrent_hashes)
    # OIDC (ADR-0014): None when not configured; tests replace the transport with a fake.
    app.state.oidc = (
        OidcClient(settings, UrllibTransport(), clock) if settings.oidc_enabled else None
    )
    app.state.oidc_flow_key = derive_subkey(secret_key, "oidc-flow")
    app.state.oidc_link_key = derive_subkey(secret_key, "oidc-link")
    app.state.oidc_consumed_states = ConsumedStates()
    # Proxy authentication (ADR-0015): repeated warnings and audited refusals are limited.
    app.state.proxy_spoof_warnings = RecentKeys()
    app.state.proxy_audits = RecentKeys()
    app.state.setup_token = None
    app.state.database_checker = checker
    # Added innermost first; see ADR-0012 for the resulting order.
    app.add_middleware(AccessGateMiddleware)
    app.add_middleware(OriginCheckMiddleware)
    app.add_middleware(RequestIdMiddleware, logger=logger)
    app.add_middleware(LocaleMiddleware, default_locale=settings.default_locale)
    app.add_middleware(SecurityHeadersMiddleware)
    static_directory = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_directory), name="static")
    app.include_router(web_router)
    app.include_router(auth_router)
    app.include_router(account_router)
    app.include_router(admin_router)
    app.include_router(audit_router)
    app.include_router(workspace_router)
    app.include_router(category_router)
    app.include_router(month_router)
    app.include_router(plan_router)
    app.include_router(transaction_router)
    app.include_router(invite_router)
    app.include_router(oidc_router)
    app.include_router(proxy_router)

    @app.exception_handler(CsrfFailed)
    async def handle_csrf_failure(request: Request, _exc: CsrfFailed) -> Response:
        locale = request.state.locale
        return error_response(
            request,
            403,
            gettext("Form expired", locale),
            gettext("This form has expired. Reload the page and try again.", locale),
        )

    @app.exception_handler(HashGateBusy)
    async def handle_busy(request: Request, _exc: HashGateBusy) -> Response:
        locale = request.state.locale
        return error_response(
            request,
            503,
            gettext("Busy", locale),
            gettext("The server is busy. Try again in a few seconds.", locale),
            headers={"Retry-After": BUSY_RETRY_AFTER},
        )

    @app.exception_handler(NoWorkspace)
    async def handle_no_workspace(request: Request, _exc: NoWorkspace) -> Response:
        locale = request.state.locale
        title = gettext("No workspace", locale)
        message = gettext(
            "Your account is not a member of any workspace. Ask a workspace owner to invite you.",
            locale,
        )
        if _is_htmx_fragment_request(request):
            return error_response(request, 403, title, message)
        # The page offers "Create workspace" when this account may create one (pre-release A).
        signed_in = web_session(request)

        def allowed() -> bool:
            if signed_in is None:
                return False
            with app.state.session_factory() as session:
                return workspace_creation_allowed(
                    session, is_instance_admin=signed_in.is_instance_admin
                )

        response = render_template(
            request,
            "errors/no_workspace.html",
            context={
                "app_name": APP_NAME,
                "title": title,
                "message": message,
                "status_code": 403,
                "can_create_workspace": await run_in_threadpool(allowed),
            },
            status_code=403,
        )
        add_error_response_headers(response, is_fragment=False)
        return response

    @app.exception_handler(PermissionDenied)
    @app.exception_handler(NotInstanceAdmin)
    async def handle_permission_denied(request: Request, _exc: Exception) -> Response:
        locale = request.state.locale
        return error_response(
            request,
            403,
            gettext("Access denied", locale),
            gettext("You do not have permission to view this page.", locale),
        )

    # Safety net (F2.5): a write refused because its month is closed, when the route did not
    # handle it itself.
    @app.exception_handler(MonthClosed)
    async def handle_month_closed(request: Request, _exc: MonthClosed) -> Response:
        locale = request.state.locale
        return error_response(
            request,
            409,
            gettext(MONTH_CLOSED_TITLE, locale),
            gettext(MONTH_CLOSED_MESSAGE, locale),
        )

    # A row of another workspace looks exactly like a missing one: same 404, no workspace hint.
    @app.exception_handler(NotFound)
    @app.exception_handler(WorkspaceNotAvailable)
    async def handle_not_found(request: Request, _exc: Exception) -> Response:
        locale = request.state.locale
        return error_response(
            request,
            404,
            gettext("Page not found", locale),
            gettext("The page you requested could not be found.", locale),
        )

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
        return error_response(request, exc.status_code, title, message, headers=exc.headers)

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
        add_error_response_headers(response, is_fragment=is_fragment)
        response.headers["x-request-id"] = request_id
        add_security_headers(response)
        return response

    @app.get("/healthz")
    async def healthz(request: Request) -> Any:
        return {"status": "ok", "request_id": request.state.request_id}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        state = app.state.schema_state
        if state != "current":
            database = SCHEMA_READINESS.get(state, "starting")
            return JSONResponse({"status": "unavailable", "database": database}, status_code=503)
        if await run_in_threadpool(checker):
            return JSONResponse({"status": "ok", "database": "ready"})
        return JSONResponse(
            {"status": "unavailable", "database": "unavailable"},
            status_code=503,
        )

    return app
