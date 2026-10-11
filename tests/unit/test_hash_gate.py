from __future__ import annotations

import asyncio
import re
import threading
from pathlib import Path

import anyio.to_thread
import httpx
import pytest

from assetboard.app import create_app
from assetboard.clock import Clock
from assetboard.config import Settings
from assetboard.services import auth
from assetboard.services.hash_gate import HashGate, HashGateBusy


class Blocker:
    """A stand-in for a slow hash that runs until released, tracking concurrency."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.lock = threading.Lock()
        self.running = 0
        self.peak = 0
        self.threads: set[int] = set()

    def __call__(self) -> str:
        with self.lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
            self.threads.add(threading.get_ident())
        try:
            if not self.release.wait(timeout=10):
                raise TimeoutError("blocker was never released")
            return "hashed"
        finally:
            with self.lock:
                self.running -= 1


async def _until(predicate: object, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():  # type: ignore[operator]
            await asyncio.sleep(0.005)


def test_gate_runs_at_most_the_cap_in_worker_threads() -> None:
    async def scenario() -> tuple[list[str], Blocker, int]:
        gate = HashGate(2)
        blocker = Blocker()
        tasks = [asyncio.create_task(gate.run(blocker)) for _ in range(6)]
        await _until(lambda: blocker.running == 2 and gate.waiting == 4)
        blocker.release.set()
        results = await asyncio.gather(*tasks)
        return results, blocker, threading.get_ident()

    results, blocker, loop_thread = asyncio.run(scenario())
    assert results == ["hashed"] * 6
    assert blocker.peak == 2
    assert loop_thread not in blocker.threads


def test_gate_rejects_when_the_queue_is_full() -> None:
    async def scenario() -> None:
        gate = HashGate(1, max_waiting=1)
        blocker = Blocker()
        running = asyncio.create_task(gate.run(blocker))
        queued = asyncio.create_task(gate.run(blocker))
        await _until(lambda: blocker.running == 1 and gate.waiting == 1)
        with pytest.raises(HashGateBusy) as busy:
            await gate.run(blocker)
        assert busy.value.code == "auth_busy"
        blocker.release.set()
        assert await asyncio.gather(running, queued) == ["hashed", "hashed"]

    asyncio.run(scenario())


def test_gate_gives_up_after_the_wait_timeout_and_frees_the_queue_slot() -> None:
    async def scenario() -> None:
        gate = HashGate(1, max_waiting=1, wait_timeout=0.05)
        blocker = Blocker()
        running = asyncio.create_task(gate.run(blocker))
        await _until(lambda: blocker.running == 1)
        with pytest.raises(HashGateBusy):
            await gate.run(blocker)
        assert gate.waiting == 0
        blocker.release.set()
        assert await running == "hashed"
        # The slot is usable again.
        assert await gate.run(lambda: "next") == "next"

    asyncio.run(scenario())


def test_gate_releases_its_slot_when_the_operation_fails() -> None:
    def explode() -> str:
        raise RuntimeError("hash failed")

    async def scenario() -> None:
        gate = HashGate(1, max_waiting=0)
        with pytest.raises(RuntimeError):
            await gate.run(explode)
        assert await gate.run(lambda: "ok") == "ok"

    asyncio.run(scenario())


def test_healthz_stays_responsive_while_hashing_is_saturated(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None, data_dir=tmp_path, secret_key="test-secret", auth_max_concurrent_hashes=2
    )
    app = create_app(settings, database_checker=lambda: True)
    gate: HashGate = app.state.hash_gate

    async def scenario() -> None:
        blocker = Blocker()
        hashes = [asyncio.create_task(gate.run(blocker)) for _ in range(2 + 16)]
        await _until(lambda: blocker.running == 2 and gate.waiting == 16)
        with pytest.raises(HashGateBusy):
            await gate.run(blocker)  # queue is full: fail fast
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            async with asyncio.timeout(2):
                health = await client.get("/healthz")
                ready = await client.get("/readyz")
        assert (health.status_code, ready.status_code) == (200, 200)
        assert blocker.running == 2
        blocker.release.set()
        assert await asyncio.gather(*hashes) == ["hashed"] * 18

    async def with_lifespan() -> None:
        async with app.router.lifespan_context(app):  # decides the schema state, as at startup
            await scenario()

    asyncio.run(with_lifespan())


def test_saturated_logins_starve_neither_sync_routes_nor_queued_requests(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """40 real logins at AUTH_MAX_CONCURRENT_HASHES=8: 8 hash, 16 wait, 16 are told to retry.

    Flow threads parked behind the gate hold default-limiter tokens; the dedicated hashing
    limiter and the 16-waiter cap leave enough of the 40 default tokens for sync routes.
    """
    settings = Settings(
        _env_file=None, data_dir=tmp_path, secret_key="test-secret", auth_max_concurrent_hashes=8
    )
    app = create_app(settings)
    app.add_api_route("/sync-probe", lambda: {"ok": True})  # a plain def route: runs in a thread
    blocker = Blocker()
    real_verify = auth.verify_password

    def slow_verify(password_hash: str, password: str) -> bool:
        blocker()
        return real_verify(password_hash, password)

    gate: HashGate = app.state.hash_gate

    async def login(index: int) -> object:
        try:
            return await auth.authenticate_user_async(
                app.state.session_factory,
                gate,
                email=f"user-{index}@example.com",
                password="Correct Horse Battery!",
                settings=settings,
                clock=Clock(),
                host=f"192.0.2.{index + 1}",
            )
        except HashGateBusy as exc:
            return exc

    async def sign_in(client: httpx.AsyncClient) -> None:
        await anyio.to_thread.run_sync(
            lambda: auth.run_initial_setup(
                app.state.session_factory,
                email="admin@example.com",
                display_name="Admin",
                password="Correct Horse Battery!",
                workspace_name="Home",
                settings=settings,
                clock=Clock(),
            )
        )
        token = re.search(r'name="csrf_token" value="([^"]+)"', (await client.get("/login")).text)
        assert token is not None
        signed = await client.post(
            "/login",
            data={
                "email": "admin@example.com",
                "password": "Correct Horse Battery!",
                "csrf_token": token.group(1),
            },
        )
        assert signed.status_code == 303

    async def scenario() -> list[object]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            await sign_in(client)  # the probe route needs a signed-in session
            monkeypatch.setattr(auth, "verify_password", slow_verify)
            logins = [asyncio.create_task(login(index)) for index in range(40)]
            busy = lambda: sum(  # noqa: E731
                1 for task in logins if task.done() and isinstance(task.result(), HashGateBusy)
            )
            await _until(lambda: blocker.running == 8 and gate.waiting == 16 and busy() == 16, 15)
            async with asyncio.timeout(3):
                responses = [
                    await client.get("/healthz"),
                    await client.get("/sync-probe"),
                    await client.get("/readyz"),
                ]
        assert [response.status_code for response in responses] == [200, 200, 200]
        blocker.release.set()
        async with asyncio.timeout(20):  # every queued request still completes
            return await asyncio.gather(*logins)

    async def with_lifespan() -> list[object]:
        async with app.router.lifespan_context(app):  # migrations, schema state, dummy hash
            return await scenario()

    results = asyncio.run(with_lifespan())
    assert sum(isinstance(result, HashGateBusy) for result in results) == 16
    assert sum(isinstance(result, auth.LoginOutcome) for result in results) == 24
    assert blocker.peak == 8
