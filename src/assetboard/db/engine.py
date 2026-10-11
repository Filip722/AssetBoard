from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

SQLITE_BUSY_TIMEOUT_MS = 5000
# Execution option read by the SQLite "begin" hook; other dialects ignore it.
BEGIN_IMMEDIATE_OPTION = "assetboard_begin_immediate"


def create_database_engine(
    database_url: str, *, busy_timeout_ms: int = SQLITE_BUSY_TIMEOUT_MS
) -> Engine:
    url = make_url(database_url)
    connect_args: dict[str, object] = {}
    if url.get_backend_name() == "sqlite":
        database = url.database
        if database and database != ":memory:" and not database.startswith("file:"):
            Path(database).expanduser().parent.mkdir(parents=True, exist_ok=True)
        connect_args["check_same_thread"] = False

    engine = create_engine(url, connect_args=connect_args, pool_pre_ping=True)
    if url.get_backend_name() == "sqlite":
        _configure_sqlite_pragmas(engine, busy_timeout_ms)
    return engine


def _configure_sqlite_pragmas(engine: Engine, busy_timeout_ms: int) -> None:
    @event.listens_for(engine, "connect")
    def set_pragmas(connection: object, record: object) -> None:
        connection.isolation_level = None  # type: ignore[attr-defined]
        cursor = connection.cursor()  # type: ignore[attr-defined]
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()

    @event.listens_for(engine, "begin")
    def begin_transaction(connection: object) -> None:
        options = connection.get_execution_options()  # type: ignore[attr-defined]
        statement = "BEGIN IMMEDIATE" if options.get(BEGIN_IMMEDIATE_OPTION) else "BEGIN"
        connection.exec_driver_sql(statement)  # type: ignore[attr-defined]


@contextmanager
def immediate_transaction(factory: sessionmaker[Session]) -> Iterator[Session]:
    """One short write transaction that takes SQLite's write lock at BEGIN (ADR-0007).

    Concurrent writers queue at BEGIN for the busy timeout instead of failing when a read
    transaction tries to upgrade. Savepoints (``begin_nested``) work inside it.
    """
    with factory() as session, session.begin():
        session.connection(execution_options={BEGIN_IMMEDIATE_OPTION: True})
        yield session


def run_immediate[T](factory: sessionmaker[Session], operation: Callable[[Session], T]) -> T:
    with immediate_transaction(factory) as session:
        return operation(session)


def is_database_locked(error: OperationalError) -> bool:
    """SQLite gave up waiting for the write lock (busy timeout exceeded)."""
    return "database is locked" in str(error.orig)


def database_is_ready(engine: Engine) -> bool:
    try:
        with engine.connect() as connection:
            return connection.execute(text("SELECT 1")).scalar_one() == 1
    except SQLAlchemyError:
        return False
