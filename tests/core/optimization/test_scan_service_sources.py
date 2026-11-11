"""Scan service retention of source runs and signals (roadmap E5)."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock

from greenkube.models.optimization_run import OptimizationRunStatus
from tests.core.optimization import test_scan_service as base  # pyrefly: ignore[missing-import]

#: Reuse the scan service suite's database fixture.
shared_sqlite = base.shared_sqlite


async def test_scheduling_prunes_source_history_past_retention(shared_sqlite):
    clock = base.Clock(base.T0)
    manager = await shared_sqlite()
    signals = AsyncMock()
    signals.prune = AsyncMock(return_value=3)
    controller = base._controller(
        manager, clock, signal_repository=signals, source_history_retention=timedelta(days=30)
    )

    await controller.tick()

    signals.prune.assert_awaited_once_with(base.T0 - timedelta(days=30))


async def test_source_history_pruning_failure_does_not_block_the_scan(shared_sqlite):
    clock = base.Clock(base.T0)
    manager = await shared_sqlite()
    signals = AsyncMock()
    signals.prune = AsyncMock(side_effect=ConnectionError("down"))
    controller = base._controller(manager, clock, signal_repository=signals)

    executed = await controller.tick()

    assert [run.status for run in executed] == [OptimizationRunStatus.SUCCEEDED]
