from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from argon2 import profiles
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from assetboard.app import create_app
from assetboard.branding import APP_NAME, ENV_PREFIX
from assetboard.clock import FixedClock
from assetboard.config import Settings
from assetboard.db.engine import create_database_engine
from assetboard.db.migrate import upgrade_database
from assetboard.services import auth as auth_service
from assetboard.services.auth import run_initial_setup


@pytest.fixture(autouse=True)
def isolate_application_data_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    monkeypatch.setenv(f"{ENV_PREFIX}_DATA_DIR", str(tmp_path / "assetboard-data"))
    for name in (
        "DATABASE_URL",
        "SECRET_KEY",
        "BASE_URL",
        "ENV",
        "LOG_LEVEL",
        "LOG_FORMAT",
        "TRUSTED_PROXIES",
        "DEFAULT_LOCALE",
        "DEFAULT_CURRENCY",
        "AUTO_MIGRATE",
        "SETUP_TOKEN",
        "ALLOW_REGISTRATION",
        "SESSION_IDLE_TIMEOUT_DAYS",
        "SESSION_ABSOLUTE_TIMEOUT_DAYS",
        "AUTH_ACCOUNT_FREE_FAILURES",
        "AUTH_IP_FREE_FAILURES",
        "AUTH_IP_WINDOW_MINUTES",
        "AUTH_THROTTLE_BASE_SECONDS",
        "AUTH_THROTTLE_MAX_SECONDS",
        "SETUP_THROTTLE_FREE_FAILURES",
        "REGISTRATION_IP_LIMIT",
        "REGISTRATION_WINDOW_MINUTES",
        "AUTH_MAX_CONCURRENT_HASHES",
    ):
        monkeypatch.delenv(f"{ENV_PREFIX}_{name}", raising=False)

    logger = logging.getLogger(APP_NAME.lower())
    for handler in tuple(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.propagate = True
    logger.setLevel(logging.NOTSET)
    yield
    for handler in tuple(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


@pytest.fixture(autouse=True)
def cheap_password_hashing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test-only seam: production hashing keeps argon2-cffi's default parameters."""
    monkeypatch.setattr(auth_service, "ARGON2_PARAMETERS", profiles.CHEAPEST)


@pytest.fixture(scope="session")
def database_engine(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Engine]:
    database_directory = tmp_path_factory.mktemp("assetboard-database")
    database_url = f"sqlite:///{database_directory / 'test.db'}"
    upgrade_database(
        Settings(_env_file=None, data_dir=database_directory, database_url=database_url)
    )
    engine = create_database_engine(database_url)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def db_session(database_engine: Engine, fixed_clock: FixedClock) -> Iterator[Session]:
    connection = database_engine.connect()
    transaction = connection.begin()
    session = Session(
        bind=connection,
        join_transaction_mode="create_savepoint",
        info={"clock": fixed_clock},
    )
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def fixed_clock() -> FixedClock:
    return FixedClock("2026-01-01T00:00:00+00:00")


ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "Correct Horse Battery!"
_CSRF_INPUT = re.compile(r'name="csrf_token" value="([^"]+)"')
_CSRF_META = re.compile(r'<meta name="csrf-token" content="([^"]+)"')


class WebHarness:
    """Builds apps on a fresh data directory and drives setup and sign-in over HTTP."""

    admin_email = ADMIN_EMAIL
    admin_password = ADMIN_PASSWORD

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir

    def settings(self, **overrides: object) -> Settings:
        values: dict[str, object] = {"data_dir": self.data_dir, "secret_key": "test-secret"}
        values.update(overrides)
        return Settings(_env_file=None, **values)  # type: ignore[arg-type]

    def app(self, **overrides: object) -> FastAPI:
        return create_app(self.settings(**overrides))

    def complete_setup(self, app: FastAPI, *, email: str = ADMIN_EMAIL) -> None:
        run_initial_setup(
            app.state.session_factory,
            email=email,
            display_name="Admin",
            password=ADMIN_PASSWORD,
            workspace_name="Home",
            settings=app.state.settings,
            clock=app.state.clock,
        )

    @staticmethod
    def csrf(html: str) -> str:
        match = _CSRF_INPUT.search(html) or _CSRF_META.search(html)
        assert match, "no CSRF token in the page"
        return match.group(1)

    def sign_in(
        self, client: TestClient, *, email: str = ADMIN_EMAIL, password: str = ADMIN_PASSWORD
    ) -> Response:
        token = self.csrf(client.get("/login").text)
        return client.post(
            "/login",
            data={"email": email, "password": password, "csrf_token": token},
            follow_redirects=False,
        )

    def page_token(self, client: TestClient, path: str = "/") -> str:
        return self.csrf(client.get(path).text)


@pytest.fixture
def web(tmp_path: Path) -> WebHarness:
    return WebHarness(tmp_path / "web-data")


@pytest.fixture
def signed_in(web: WebHarness) -> Iterator[TestClient]:
    """A client signed in as the instance administrator after first-run setup."""
    app = web.app()
    with TestClient(app) as client:
        web.complete_setup(app)
        response = web.sign_in(client)
        assert response.status_code == 303, response.text
        yield client


class CountingHasher:
    """Counts Argon2 hash/verify calls made through ``auth.password_hasher``."""

    def __init__(self) -> None:
        self.hashes = 0
        self.verifies = 0


@pytest.fixture
def argon2_calls(monkeypatch: pytest.MonkeyPatch) -> CountingHasher:
    counter = CountingHasher()
    real = auth_service.password_hasher

    class Wrapped:
        def __init__(self) -> None:
            self._inner = real()

        def hash(self, password: str) -> str:
            counter.hashes += 1
            return self._inner.hash(password)

        def verify(self, encoded: str, password: str) -> bool:
            counter.verifies += 1
            return self._inner.verify(encoded, password)

        def check_needs_rehash(self, encoded: str) -> bool:
            return self._inner.check_needs_rehash(encoded)

    monkeypatch.setattr(auth_service, "password_hasher", Wrapped)
    return counter
