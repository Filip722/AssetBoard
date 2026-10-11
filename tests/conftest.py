from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from argon2 import profiles
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from assetboard.branding import APP_NAME, ENV_PREFIX
from assetboard.clock import FixedClock
from assetboard.config import Settings
from assetboard.db.engine import create_database_engine
from assetboard.db.migrate import upgrade_database
from assetboard.services import auth as auth_service


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
