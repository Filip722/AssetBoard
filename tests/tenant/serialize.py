"""Deterministic two-writer races on a real SQLite database (see the concurrency tests)."""

from __future__ import annotations

import threading
from collections.abc import Callable
from types import ModuleType

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine


def serialize_at_checkpoint(
    engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    module: ModuleType,
    first: Callable[[], object],
    second: Callable[[], object],
) -> dict[str, BaseException | None]:
    """Run ``first`` until ``module._checkpoint``; start ``second`` and see it reach
    ``BEGIN IMMEDIATE`` (where SQLite makes it wait); then release ``first``.

    Returns each call's exception, or ``None`` if it succeeded.
    """
    first_inside, release_first, second_at_begin = (threading.Event() for _ in range(3))

    def checkpoint(_name: str) -> None:
        if not first_inside.is_set():
            first_inside.set()
            assert release_first.wait(timeout=20)

    monkeypatch.setattr(module, "_checkpoint", checkpoint)

    @event.listens_for(engine, "before_cursor_execute")
    def watch_begin(_conn: object, _cursor: object, statement: str, *_args: object) -> None:
        if statement == "BEGIN IMMEDIATE" and first_inside.is_set():
            second_at_begin.set()

    outcomes: dict[str, BaseException | None] = {}

    def run(name: str, call: Callable[[], object]) -> None:
        try:
            call()
            outcomes[name] = None
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            outcomes[name] = exc

    threads = {
        "first": threading.Thread(target=run, args=("first", first)),
        "second": threading.Thread(target=run, args=("second", second)),
    }
    try:
        threads["first"].start()
        assert first_inside.wait(timeout=20), "first writer never reached the checkpoint"
        threads["second"].start()
        assert second_at_begin.wait(timeout=20), "second writer never reached BEGIN IMMEDIATE"
        assert threads["second"].is_alive()  # waiting for the write lock
        release_first.set()
        for thread in threads.values():
            thread.join(timeout=20)
            assert not thread.is_alive()
    finally:
        release_first.set()
        event.remove(engine, "before_cursor_execute", watch_begin)
    return outcomes
