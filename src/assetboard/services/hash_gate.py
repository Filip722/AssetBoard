"""Bounded concurrency for password hashing.

Argon2id with the library defaults needs about 64 MiB and noticeable CPU per call. The gate
runs at most ``max_concurrent`` hashes at once, each in a worker thread so the event loop (and
``/healthz``) stays responsive. At most ``max_waiting`` callers wait for a slot (16 waiting in
total by default, not per slot); a full queue or a long wait fails fast with ``HashGateBusy``
so the web layer can answer 503 with ``Retry-After``.

Hashing threads come from a dedicated ``CapacityLimiter``, not anyio's shared default limiter
(40 tokens). Auth flows run their database steps in default-limiter threads and park there while
their hash waits; with a dedicated limiter those parked threads can never starve the hashing
itself, and the small waiter cap bounds how many default tokens they can hold, leaving the rest
for sync routes.

Loop binding: the semaphore binds to the event loop on first use and the limiter is created
lazily inside it, so one gate serves exactly one event loop (the app's). Create a new gate (or
app) for every loop, e.g. per ``asyncio.run`` in tests.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import anyio
import anyio.to_thread

MAX_WAITERS = 16
WAIT_TIMEOUT_SECONDS = 10.0
RETRY_AFTER_SECONDS = 5


class HashGateBusy(Exception):
    code = "auth_busy"

    def __init__(self) -> None:
        super().__init__("Too many sign-in requests are being processed; try again shortly")


class HashGate:
    def __init__(
        self,
        max_concurrent: int,
        *,
        max_waiting: int = MAX_WAITERS,
        wait_timeout: float = WAIT_TIMEOUT_SECONDS,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        self._max_concurrent = max_concurrent
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._limiter: anyio.CapacityLimiter | None = None
        self._max_waiting = max_waiting
        self._wait_timeout = wait_timeout
        self._waiting = 0

    @property
    def waiting(self) -> int:
        return self._waiting

    def _thread_limiter(self) -> anyio.CapacityLimiter:
        # Created on first use, inside the running event loop.
        if self._limiter is None:
            self._limiter = anyio.CapacityLimiter(self._max_concurrent)
        return self._limiter

    async def run[T](self, operation: Callable[[], T]) -> T:
        """Run ``operation`` in a dedicated hashing thread once a slot is free."""
        if self._semaphore.locked() and self._waiting >= self._max_waiting:
            raise HashGateBusy
        self._waiting += 1
        try:
            async with asyncio.timeout(self._wait_timeout):
                await self._semaphore.acquire()
        except TimeoutError as exc:
            raise HashGateBusy from exc
        finally:
            self._waiting -= 1
        try:
            return await anyio.to_thread.run_sync(operation, limiter=self._thread_limiter())
        finally:
            self._semaphore.release()
