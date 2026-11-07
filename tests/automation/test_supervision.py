"""Supervised in-process automation loops (roadmap D9, BUG-51)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from greenkube.api.routers import config as config_router
from greenkube.automation.supervision import LoopHealth, SupervisedLoop, backoff_delay, error_code

_NOW = datetime(2026, 11, 7, 12, tzinfo=timezone.utc)


def test_backoff_is_exponential_jittered_and_capped():
    upper = [backoff_delay(failures, initial=1, maximum=30, jitter=lambda low, high: high) for failures in range(7)]
    lower = [backoff_delay(failures, initial=1, maximum=30, jitter=lambda low, high: low) for failures in range(7)]

    assert upper == [0.0, 1, 2, 4, 8, 16, 30]
    assert lower == [0.0, 0.5, 1, 2, 4, 8, 15]


def test_error_code_prefers_a_stable_code_and_never_the_message():
    class Coded(Exception):
        code = SimpleNamespace(value="provider_not_ready")

    assert error_code(Coded("token=secret")) == "provider_not_ready"
    assert error_code(ConnectionError("password=hunter2")) == "ConnectionError"


def test_loop_health_readiness_follows_the_heartbeat_age():
    now = [_NOW]
    health = LoopHealth(name="loop", stale_after=timedelta(minutes=5), clock=lambda: now[0])
    assert not health.ready()

    health.state = "running"
    health.beat()
    assert health.ready()
    now[0] = _NOW + timedelta(minutes=6)
    assert not health.ready()
    snapshot = health.snapshot()
    assert snapshot["ready"] is False
    assert snapshot["heartbeat_age_seconds"] == 360.0

    health.beat()
    health.state = "stopped"
    assert not health.ready()


@pytest.mark.asyncio
async def test_failed_and_exited_loops_are_restarted_with_backoff():
    runs = 0
    delays: list[float] = []
    failures: list[str] = []
    finished = asyncio.Event()

    async def factory(heartbeat):
        nonlocal runs
        runs += 1
        heartbeat()
        if runs == 1:
            raise ConnectionError("database restarting")
        if runs == 2:
            return None
        finished.set()
        await asyncio.Event().wait()

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    supervisor = SupervisedLoop(
        "test-loop",
        factory,
        initial_backoff=2,
        max_backoff=60,
        jitter=lambda low, high: high,
        sleep=record_sleep,
        on_failure=failures.append,
    )
    supervisor.start()
    await asyncio.wait_for(finished.wait(), timeout=1)

    assert runs == 3
    assert delays == [2, 4]
    assert failures == ["ConnectionError", "loop_exited"]
    assert supervisor.health.restarts == 2
    assert supervisor.health.last_error_code == "loop_exited"
    assert supervisor.health.state == "running"
    assert supervisor.health.ready()

    await supervisor.stop()
    assert supervisor.health.state == "stopped"
    assert not supervisor.health.ready()


class _CrashOnceSupervisor(SupervisedLoop):
    crashed = False

    async def _supervise(self) -> None:
        if not self.crashed:
            self.crashed = True
            raise RuntimeError("supervisor bug")
        await super()._supervise()


@pytest.mark.asyncio
async def test_done_callback_restarts_a_supervisor_that_died():
    running = asyncio.Event()

    async def factory(heartbeat):
        heartbeat()
        running.set()
        await asyncio.Event().wait()

    supervisor = _CrashOnceSupervisor("crashy", factory, initial_backoff=0.01, max_backoff=0.02)
    supervisor.start()

    await asyncio.wait_for(running.wait(), timeout=1)

    assert supervisor.crashed
    assert supervisor.health.restarts == 1
    assert supervisor.health.last_error_code == "RuntimeError"
    assert supervisor.health.state == "running"
    await supervisor.stop()
    assert supervisor.health.state == "stopped"


def test_readiness_endpoint_degrades_when_a_supervised_loop_is_wedged():
    app = FastAPI()
    app.include_router(config_router.router, prefix="/api/v1")
    now = [_NOW]
    health = LoopHealth(name="inline-plan-worker", stale_after=timedelta(minutes=20), clock=lambda: now[0])
    health.state = "running"
    health.beat()
    app.state.automation_loop_supervisor = SimpleNamespace(health=health)
    client = TestClient(app)

    assert client.get("/api/v1/health").status_code == 200
    now[0] = _NOW + timedelta(minutes=21)
    degraded = client.get("/api/v1/health")
    assert degraded.status_code == 503
    assert degraded.json()["status"] == "degraded"

    app.state.automation_loop_supervisor = None
    assert client.get("/api/v1/health").status_code == 200
