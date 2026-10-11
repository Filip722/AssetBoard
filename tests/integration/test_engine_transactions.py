from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from assetboard.clock import FixedClock
from assetboard.db.engine import (
    create_database_engine,
    immediate_transaction,
    is_database_locked,
    run_immediate,
)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    engine = create_database_engine(f"sqlite:///{tmp_path / 'tx.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE items (name TEXT PRIMARY KEY)")
    try:
        yield engine
    finally:
        engine.dispose()


def _factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, info={"clock": FixedClock("2026-01-01T00:00:00+00:00")})


def _record_statements(engine: Engine) -> list[str]:
    statements: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def record(_conn: object, _cursor: object, statement: str, *_args: object) -> None:
        statements.append(statement)

    return statements


def test_default_transactions_emit_deferred_begin(engine: Engine) -> None:
    statements = _record_statements(engine)
    with _factory(engine).begin() as session:
        session.execute(text("SELECT 1"))
    assert "BEGIN" in statements
    assert "BEGIN IMMEDIATE" not in statements


def test_immediate_transaction_emits_begin_immediate_and_supports_savepoints(
    engine: Engine,
) -> None:
    statements = _record_statements(engine)
    with immediate_transaction(_factory(engine)) as session:
        session.execute(text("INSERT INTO items VALUES ('kept')"))
        with pytest.raises(RuntimeError), session.begin_nested():
            session.execute(text("INSERT INTO items VALUES ('rolled back')"))
            raise RuntimeError("roll back the savepoint only")
    assert statements[0] == "BEGIN IMMEDIATE"
    assert any(statement.startswith("SAVEPOINT") for statement in statements)
    with engine.connect() as connection:
        assert connection.exec_driver_sql("SELECT name FROM items").scalars().all() == ["kept"]
    # The option does not leak into later default transactions on the pooled connection.
    statements.clear()
    with _factory(engine).begin() as session:
        session.execute(text("SELECT 1"))
    assert "BEGIN IMMEDIATE" not in statements


def test_immediate_transaction_takes_the_write_lock_at_begin(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'lock.db'}"
    holder = create_database_engine(url)
    waiter = create_database_engine(url, busy_timeout_ms=50)
    try:
        with immediate_transaction(_factory(holder)):
            # A deferred reader still works (WAL) ...
            with _factory(waiter)() as reader:
                assert reader.execute(text("SELECT 1")).scalar_one() == 1
            # ... but a second immediate transaction cannot start.
            with pytest.raises(OperationalError) as locked:
                run_immediate(_factory(waiter), lambda session: None)
        assert is_database_locked(locked.value)
        assert run_immediate(_factory(waiter), lambda session: "free again") == "free again"
    finally:
        holder.dispose()
        waiter.dispose()
