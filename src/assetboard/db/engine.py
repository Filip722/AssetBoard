from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import SQLAlchemyError

SQLITE_BUSY_TIMEOUT_MS = 5000


def create_database_engine(database_url: str) -> Engine:
    url = make_url(database_url)
    connect_args: dict[str, object] = {}
    if url.get_backend_name() == "sqlite":
        database = url.database
        if database and database != ":memory:" and not database.startswith("file:"):
            Path(database).expanduser().parent.mkdir(parents=True, exist_ok=True)
        connect_args["check_same_thread"] = False

    engine = create_engine(url, connect_args=connect_args, pool_pre_ping=True)
    if url.get_backend_name() == "sqlite":
        _configure_sqlite_pragmas(engine)
    return engine


def _configure_sqlite_pragmas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def set_pragmas(connection: object, record: object) -> None:
        connection.isolation_level = None  # type: ignore[attr-defined]
        cursor = connection.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()

    @event.listens_for(engine, "begin")
    def begin_transaction(connection: object) -> None:
        connection.exec_driver_sql("BEGIN")  # type: ignore[attr-defined]


def database_is_ready(engine: Engine) -> bool:
    try:
        with engine.connect() as connection:
            return connection.execute(text("SELECT 1")).scalar_one() == 1
    except SQLAlchemyError:
        return False
