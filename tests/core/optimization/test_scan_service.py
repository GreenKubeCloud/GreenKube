"""Scheduled, leader-gated recommendation scans (E1, BUG-21).

Acceptance: two API replicas plus one controller produce exactly one scan per
interval, and API processes never scan.
"""

import asyncio
import importlib
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, List, Optional, cast
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI

from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.core.optimization.engine import ScanResult
from greenkube.core.optimization.scan_service import RecommendationScanService
from greenkube.models.optimization_run import (
    OptimizationRun,
    OptimizationRunCompletion,
    OptimizationRunStatus,
    OptimizationRunTrigger,
)
from greenkube.storage.sqlite.optimization_run_repository import SQLiteOptimizationRunRepository

T0 = datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc)
INTERVAL = timedelta(hours=1)
POLL = timedelta(seconds=15)


class Clock:
    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class CountingEngine:
    """Stands in for OptimizationEngine: records scans and completes the claimed run."""

    scans: List[tuple] = []

    def __init__(self, run_repository, clock: Clock, fail: bool = False):
        self.run_repository = run_repository
        self.clock = clock
        self.fail = fail

    async def run_scan(self, combined_repo, node_repo, reco_repo, namespace=None, run=None):
        assert run is not None and run.id is not None
        CountingEngine.scans.append((self.clock(), namespace, run.trigger))
        if self.fail:
            await self.run_repository.finish(
                run.id,
                OptimizationRunCompletion(status=OptimizationRunStatus.FAILED, completed_at=self.clock(), error="X"),
            )
            raise RuntimeError("scan failed")
        finished = await self.run_repository.finish(
            run.id,
            OptimizationRunCompletion(status=OptimizationRunStatus.SUCCEEDED, completed_at=self.clock()),
        )
        return ScanResult(status=OptimizationRunStatus.SUCCEEDED, run=finished)


@pytest.fixture(autouse=True)
def reset_scans():
    CountingEngine.scans = []
    yield
    CountingEngine.scans = []


@pytest.fixture
async def shared_sqlite(tmp_path, monkeypatch):
    """One database file shared by several 'processes' (one manager each)."""
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "shared.sqlite"))
    config.reload()
    managers: List[DatabaseManager] = []

    async def open_process() -> DatabaseManager:
        manager = DatabaseManager(Config())
        await manager.connect()
        managers.append(manager)
        return manager

    try:
        yield open_process
    finally:
        for manager in managers:
            await manager.close()


async def _stored(repo: SQLiteOptimizationRunRepository, run_id: Optional[int]) -> OptimizationRun:
    assert run_id is not None
    run = await repo.get(run_id)
    assert run is not None
    return run


def _controller(manager, clock, identity="controller-0", fail=False, **kwargs) -> RecommendationScanService:
    run_repo = SQLiteOptimizationRunRepository(manager)
    return RecommendationScanService(
        run_repo,
        combined_repo=AsyncMock(),
        node_repo=AsyncMock(),
        reco_repo=AsyncMock(),
        interval=INTERVAL,
        identity=identity,
        engine_factory=lambda repo: cast(Any, CountingEngine(repo, clock, fail=fail)),
        clock=clock,
        **kwargs,
    )


def _patch_api_lifespan(monkeypatch):
    """Let the real API lifespan start with a fake DB and no plan runtime."""
    api_app = importlib.import_module("greenkube.api.app")
    database = importlib.import_module("greenkube.core.db")
    k8s_client = importlib.import_module("greenkube.core.k8s_client")

    class _NoopManager:
        async def connect(self):
            return None

        async def close(self):
            return None

    async def _noop():
        return None

    async def _no_plan_service(**kwargs):
        raise RuntimeError("no providers")

    monkeypatch.setattr(database, "get_db_manager", lambda: _NoopManager())
    monkeypatch.setattr(api_app, "get_config", lambda: SimpleNamespace(AUTOMATION_ENABLED=False))
    monkeypatch.setattr(api_app, "build_plan_api_service", _no_plan_service)
    monkeypatch.setattr(k8s_client, "close_k8s_client", _noop)
    return api_app


async def test_two_api_replicas_and_one_controller_scan_exactly_once_per_interval(shared_sqlite, monkeypatch):
    clock = Clock(T0)
    controller = _controller(await shared_sqlite(), clock)
    api_runs = [SQLiteOptimizationRunRepository(await shared_sqlite()) for _ in range(2)]
    run_scan = AsyncMock(side_effect=AssertionError("API processes must never scan"))
    monkeypatch.setattr("greenkube.core.optimization.engine.OptimizationEngine.run_scan", run_scan)

    api_app = _patch_api_lifespan(monkeypatch)
    async with api_app.lifespan(FastAPI()), api_app.lifespan(FastAPI()):
        # Three hours of controller polling while both API replicas serve traffic.
        while clock() < T0 + 3 * INTERVAL:
            await controller.tick()
            clock.advance(POLL)
        await asyncio.sleep(0)

    run_scan.assert_not_awaited()
    assert [scan[0] for scan in CountingEngine.scans] == [T0, T0 + INTERVAL, T0 + 2 * INTERVAL]
    assert all(scan[2] == OptimizationRunTrigger.SCHEDULED for scan in CountingEngine.scans)
    runs = await api_runs[0].list_runs(limit=10)
    assert [run.status for run in runs] == [OptimizationRunStatus.SUCCEEDED] * 3


async def test_api_requests_from_both_replicas_are_deduplicated_and_run_once(shared_sqlite):
    clock = Clock(T0)
    controller = _controller(await shared_sqlite(), clock)
    api_a = SQLiteOptimizationRunRepository(await shared_sqlite())
    api_b = SQLiteOptimizationRunRepository(await shared_sqlite())

    await controller.tick()  # scheduled scan at T0
    clock.advance(timedelta(minutes=10))
    requests = await asyncio.gather(
        *(
            repo.enqueue(namespace=None, trigger=OptimizationRunTrigger.REFRESH, requested_at=clock())
            for repo in (api_a, api_b, api_a, api_b)
        )
    )
    assert len({run.id for run, _ in requests}) == 1
    assert sum(created for _, created in requests) == 1

    await controller.tick()
    clock.advance(POLL)
    await controller.tick()

    assert [(scan[0], scan[2]) for scan in CountingEngine.scans] == [
        (T0, OptimizationRunTrigger.SCHEDULED),
        (T0 + timedelta(minutes=10), OptimizationRunTrigger.REFRESH),
    ]
    finished = await _stored(api_b, requests[0][0].id)
    assert finished.status == OptimizationRunStatus.SUCCEEDED


async def test_two_controllers_without_leader_election_still_scan_once_per_interval(shared_sqlite):
    clock = Clock(T0)
    controllers = [
        _controller(await shared_sqlite(), clock, identity="controller-0"),
        _controller(await shared_sqlite(), clock, identity="controller-1"),
    ]

    while clock() < T0 + 2 * INTERVAL:
        await asyncio.gather(*(controller.tick() for controller in controllers))
        clock.advance(POLL)

    assert [scan[0] for scan in CountingEngine.scans] == [T0, T0 + INTERVAL]


async def test_standby_controller_does_not_claim_runs_after_losing_the_lease(shared_sqlite):
    clock = Clock(T0)
    leader = {"value": False}
    controller = _controller(await shared_sqlite(), clock, should_continue=lambda: leader["value"])
    api = SQLiteOptimizationRunRepository(await shared_sqlite())
    run, _ = await api.enqueue(namespace="prod", trigger=OptimizationRunTrigger.MANUAL, requested_at=T0)

    assert await controller.tick() == []
    assert (await _stored(api, run.id)).status == OptimizationRunStatus.QUEUED

    leader["value"] = True
    executed = await controller.tick()
    assert {item.namespace for item in executed} == {None, "prod"}


async def test_failed_scan_is_recorded_and_does_not_reschedule_before_the_interval(shared_sqlite):
    clock = Clock(T0)
    controller = _controller(await shared_sqlite(), clock, fail=True)

    executed = await controller.tick()
    clock.advance(timedelta(minutes=30))
    await controller.tick()

    assert [run.status for run in executed] == [OptimizationRunStatus.FAILED]
    assert len(CountingEngine.scans) == 1


async def test_abandoned_running_run_is_failed_and_scheduling_resumes(shared_sqlite):
    clock = Clock(T0)
    manager = await shared_sqlite()
    runs = SQLiteOptimizationRunRepository(manager)
    await runs.enqueue_if_due(requested_at=T0, due_before=T0 - INTERVAL)
    crashed = await runs.claim_next(claimed_by="dead-controller", started_at=T0)
    assert crashed is not None
    controller = _controller(manager, clock)

    clock.advance(timedelta(minutes=20))
    assert await controller.tick() == []
    clock.advance(timedelta(minutes=50))
    executed = await controller.tick()

    stored = await _stored(runs, crashed.id)
    assert stored.status == OptimizationRunStatus.FAILED
    assert (stored.error or "").startswith("abandoned")
    assert [run.trigger for run in executed] == [OptimizationRunTrigger.SCHEDULED]


async def test_tick_bounds_runs_per_cycle(shared_sqlite):
    clock = Clock(T0)
    manager = await shared_sqlite()
    runs = SQLiteOptimizationRunRepository(manager)
    for namespace in ("a", "b", "c", "d"):
        await runs.enqueue(namespace=namespace, trigger=OptimizationRunTrigger.MANUAL, requested_at=T0)
    controller = _controller(manager, clock, max_runs_per_tick=2)

    first = await controller.tick()
    second = await controller.tick()

    assert len(first) == 2 and len(second) == 2


def test_interval_must_be_positive():
    with pytest.raises(ValueError):
        RecommendationScanService(
            AsyncMock(),
            combined_repo=AsyncMock(),
            node_repo=AsyncMock(),
            reco_repo=AsyncMock(),
            interval=timedelta(0),
            identity="c",
        )
