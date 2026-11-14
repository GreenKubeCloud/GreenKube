"""The controller's first recommendation scan runs on collected data (E1).

Regression for the startup race where the scan poll fired while the initial
collection was still running: that early scan covered the interval, so the
post-collection cycle did not scan and real recommendations only appeared one
``RECOMMENDATION_SCAN_INTERVAL`` later.

These tests drive the real ``_async_start`` wiring with the real
``Scheduler``, the real ``RecommendationScanService`` and a SQLite run queue.
"""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, List, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

from greenkube.cli import start as start_module
from greenkube.core.config import Config, config
from greenkube.core.db import DatabaseManager
from greenkube.core.optimization.engine import ScanResult
from greenkube.core.optimization.scan_service import RecommendationScanService
from greenkube.models.optimization_run import (
    OptimizationRunCompletion,
    OptimizationRunStatus,
    OptimizationRunTrigger,
)
from greenkube.storage.sqlite.optimization_run_repository import SQLiteOptimizationRunRepository

#: Scaled-down scan poll (production: 15s); the scheduler's floor is 1s.
POLL = "1s"
#: Initial collection outlasts one scan poll, as on any real cluster.
COLLECTION_SECONDS = 1.4
#: Observation window after the first scan: covers at least one more poll.
SETTLE_SECONDS = 1.6


class _Elector:
    """Lease holder from the first renewal."""

    is_leader = True
    # LeaderGate hands each gated job a fence built from the Lease generation (A14).
    fencing_token = 1

    async def try_acquire_or_renew(self) -> bool:
        return True

    async def run(self, stop_event: asyncio.Event) -> None:
        await stop_event.wait()


@pytest.fixture
async def run_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_TYPE", "sqlite")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "controller.sqlite"))
    config.reload()
    manager = DatabaseManager(Config())
    await manager.connect()
    try:
        yield manager
    finally:
        await manager.close()


def _install_controller(monkeypatch, runs: SQLiteOptimizationRunRepository, *, leader_election: bool, state, scans):
    class Engine:
        def __init__(self, run_repository):
            self.run_repository = run_repository

        async def run_scan(self, combined_repo, node_repo, reco_repo, namespace=None, run=None):
            assert run is not None and run.id is not None
            scans.append({"trigger": run.trigger, "data_collected": state["collected"]})
            done = await self.run_repository.finish(
                run.id,
                OptimizationRunCompletion(status=OptimizationRunStatus.SUCCEEDED, completed_at=run.started_at),
            )
            return ScanResult(status=OptimizationRunStatus.SUCCEEDED, run=done)

    def build_scan_service(*, should_continue=None, identity=None):
        return RecommendationScanService(
            runs,
            combined_repo=AsyncMock(),
            node_repo=AsyncMock(),
            reco_repo=AsyncMock(),
            interval=timedelta(hours=1),
            identity="controller-0",
            engine_factory=lambda repo: cast(Any, Engine(repo)),
            should_continue=should_continue,
        )

    async def slow_initial_collection(last=None):
        await asyncio.sleep(COLLECTION_SECONDS)
        state["collected"] = True

    db_manager = MagicMock()
    db_manager.connect = AsyncMock()
    processor = MagicMock()
    processor.close = AsyncMock()
    discovery = SimpleNamespace(run_once=AsyncMock(), interval_seconds=300.0)

    monkeypatch.setattr(
        start_module,
        "get_config",
        lambda: SimpleNamespace(
            LOG_LEVEL="INFO",
            LOG_FORMAT="console",
            CLUSTER_NAME="test-cluster",
            DB_TYPE="sqlite",
            PROMETHEUS_QUERY_RANGE_STEP="5m",
            NODE_ANALYSIS_INTERVAL="1h",
            RECOMMENDATION_LIFECYCLE_INTERVAL="5m",
        ),
    )
    monkeypatch.setattr("greenkube.core.db.get_db_manager", lambda: db_manager)
    monkeypatch.setattr("greenkube.automation.plan_runtime.initialize_gitops_provider_configs", AsyncMock())
    monkeypatch.setattr(
        "greenkube.gitops.discovery.runtime.create_gitops_discovery_runtime", AsyncMock(return_value=discovery)
    )
    monkeypatch.setattr(start_module, "_create_controller_plan_runtime", AsyncMock(return_value=None))
    monkeypatch.setattr(start_module, "_create_session_purge", lambda _db_manager: None)
    monkeypatch.setattr(
        start_module, "_create_controller_leader_election", lambda cfg: _Elector() if leader_election else None
    )
    monkeypatch.setattr(start_module, "get_data_processor", lambda: processor)
    for name in (
        "collect_carbon_intensity_for_all_zones",
        "analyze_nodes",
        "attribute_recommendation_savings",
        "run_recommendation_lifecycle",
        "compress_metrics",
        "refresh_dashboard_summary",
    ):
        monkeypatch.setattr(start_module, name, AsyncMock())
    monkeypatch.setattr(start_module, "async_write_combined_metrics_to_database", slow_initial_collection)
    monkeypatch.setattr("greenkube.core.optimization.scan_service.RECOMMENDATION_SCAN_POLL_INTERVAL", POLL)
    monkeypatch.setattr(
        "greenkube.core.optimization.scan_service.create_recommendation_scan_service", build_scan_service
    )
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda *args: None)


async def _run_controller_until_first_scan_settles(scans: List[dict]) -> None:
    loop = asyncio.get_running_loop()
    controller = asyncio.create_task(start_module._async_start(last=None))
    try:
        deadline = loop.time() + 15
        while not scans and loop.time() < deadline and not controller.done():
            await asyncio.sleep(0.05)
        await asyncio.sleep(SETTLE_SECONDS)
    finally:
        controller.cancel()
        await asyncio.gather(controller, return_exceptions=True)


@pytest.mark.parametrize("leader_election", [False, True], ids=["inline", "leader"])
async def test_first_scan_runs_once_after_initial_collection(run_db, monkeypatch, leader_election):
    state = {"collected": False}
    scans: List[dict] = []
    runs = SQLiteOptimizationRunRepository(run_db)
    _install_controller(monkeypatch, runs, leader_election=leader_election, state=state, scans=scans)

    await _run_controller_until_first_scan_settles(scans)

    assert scans == [{"trigger": OptimizationRunTrigger.SCHEDULED, "data_collected": True}]
    stored = await runs.list_runs(limit=10)
    assert [run.status for run in stored] == [OptimizationRunStatus.SUCCEEDED]


async def test_restart_within_the_interval_still_scans_the_fresh_collection(run_db, monkeypatch):
    """A controller restart rescans after collection even if a scan ran minutes ago."""
    state = {"collected": False}
    scans: List[dict] = []
    runs = SQLiteOptimizationRunRepository(run_db)
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    previous = await runs.enqueue_if_due(requested_at=now - timedelta(minutes=5), due_before=now - timedelta(hours=2))
    assert previous is not None and previous.id is not None
    claimed = await runs.claim_next(claimed_by="previous-pod", started_at=now - timedelta(minutes=5))
    assert claimed is not None
    await runs.finish(
        previous.id,
        OptimizationRunCompletion(status=OptimizationRunStatus.SUCCEEDED, completed_at=now - timedelta(minutes=4)),
    )
    _install_controller(monkeypatch, runs, leader_election=False, state=state, scans=scans)

    await _run_controller_until_first_scan_settles(scans)

    assert scans == [{"trigger": OptimizationRunTrigger.SCHEDULED, "data_collected": True}]


# -- Shutdown: the Lease is handed over only after all leader work has stopped ------
# Releasing lets a standby acquire within one retry period, so leader loops and the
# deferred initial work must no longer be running or unwinding at that point.


class _ReleasingElector:
    """Leader from the first renewal; records what was still running at release."""

    is_leader = True
    fencing_token = 1

    def __init__(self, state: dict) -> None:
        self.state = state
        self.releases: List[dict] = []

    async def try_acquire_or_renew(self) -> bool:
        return True

    async def run(self, stop_event: asyncio.Event) -> bool:
        await stop_event.wait()
        return True

    async def release(self, timeout_seconds: float = 5.0) -> bool:
        running = sorted(
            task.get_name()
            for task in asyncio.all_tasks()
            if not task.done() and (task.get_name().startswith("loop-") or task.get_name() == "controller-initial-work")
        )
        self.releases.append({"running": running, "initial_work_unwound": self.state["unwound"]})
        return True


async def test_lease_is_released_once_leader_loops_and_initial_work_have_stopped(run_db, monkeypatch):
    state = {"collected": False, "started": False, "unwound": False}
    runs = SQLiteOptimizationRunRepository(run_db)
    _install_controller(monkeypatch, runs, leader_election=True, state=state, scans=[])
    elector = _ReleasingElector(state)
    monkeypatch.setattr(start_module, "_create_controller_leader_election", lambda cfg: elector)

    async def endless_initial_collection(last=None):
        state["started"] = True
        try:
            await asyncio.sleep(3600)
        finally:
            # Leader work that unwinds with awaits after cancellation.
            await asyncio.sleep(0.2)
            state["unwound"] = True

    monkeypatch.setattr(start_module, "async_write_combined_metrics_to_database", endless_initial_collection)

    loop = asyncio.get_running_loop()
    controller = asyncio.create_task(start_module._async_start(last=None))
    try:
        deadline = loop.time() + 15
        while not state["started"] and loop.time() < deadline and not controller.done():
            await asyncio.sleep(0.05)
        assert state["started"], "the deferred initial work never started"
    finally:
        controller.cancel()
        await asyncio.gather(controller, return_exceptions=True)

    assert elector.releases == [{"running": [], "initial_work_unwound": True}]
