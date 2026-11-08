"""Reliability checks for the composed recommendation/GitOps loop.

The closed loop is deliberately best effort at its boundaries: API
replicas never scan, and a failed ledger write must not take down the API
or lose the ability to serve health traffic.  These tests exercise those recovery
contracts without requiring a Kubernetes or Git provider.
"""

import asyncio
import importlib
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

from greenkube.api.app import lifespan
from greenkube.core.savings_attributor import SavingsAttributor
from greenkube.models.metrics import RecommendationRecord, RecommendationStatus, RecommendationType


def _recommendation(status=RecommendationStatus.APPLIED, applied_at=None) -> RecommendationRecord:
    return RecommendationRecord(
        id=41,
        pod_name="api",
        namespace="production",
        type=RecommendationType.RIGHTSIZING_CPU,
        description="Reduce CPU request",
        reason="Sustained low utilization",
        priority="medium",
        potential_savings_cost=12.0,
        potential_savings_co2e_grams=120.0,
        cost_saved=12.0,
        carbon_saved_co2e_grams=120.0,
        status=status,
        applied_at=applied_at,
    )


@pytest.mark.asyncio
async def test_api_lifespan_never_scans_and_still_closes_resources():
    """API replicas never run recommendation scans (E1); resources still close."""
    db = MagicMock()
    db.connect = AsyncMock()
    db.close = AsyncMock()
    app = FastAPI()
    scan_entry_points = {
        name: AsyncMock(side_effect=AssertionError(f"the API must not call {name}"))
        for name in ("run_scan", "refresh", "collect", "tick")
    }

    with (
        patch("greenkube.core.db.get_db_manager", return_value=db),
        patch("greenkube.core.optimization.engine.OptimizationEngine.run_scan", new=scan_entry_points["run_scan"]),
        patch("greenkube.core.optimization.engine.OptimizationEngine.refresh", new=scan_entry_points["refresh"]),
        patch("greenkube.core.optimization.engine.OptimizationEngine.collect", new=scan_entry_points["collect"]),
        patch("greenkube.core.optimization.scan_service.RecommendationScanService.tick", new=scan_entry_points["tick"]),
        patch("greenkube.core.k8s_client.close_k8s_client", new=AsyncMock()),
    ):
        async with lifespan(app):
            assert app.state.ready is True
            # Give every startup task time to reach its first await point and beyond.
            if app.state._startup_tasks:
                await asyncio.wait(app.state._startup_tasks, timeout=0.2)
            for _ in range(10):
                await asyncio.sleep(0)

    for entry_point in scan_entry_points.values():
        entry_point.assert_not_awaited()
    assert not hasattr(importlib.import_module("greenkube.api.startup"), "run_startup_recommendation_scan")
    assert app.state.ready is False
    db.connect.assert_awaited_once()
    db.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_savings_ledger_failure_is_recoverable():
    """A ledger outage writes nothing, keeps the watermark and leaves the collection loop alive."""
    repo = MagicMock()
    repo.get_accrual_watermark = AsyncMock(return_value=None)
    repo.get_accrual_floors = AsyncMock(return_value={})
    repo.append_accruals = AsyncMock(side_effect=ConnectionError("ledger unavailable"))
    attributor = SavingsAttributor(repo, cluster_name="prod")
    now = datetime.now(timezone.utc)

    result = await attributor.accrue([_recommendation(applied_at=now - timedelta(hours=3))], now=now)

    assert result.written == 0 and result.errors == 1
    assert result.watermark is None
    repo.append_accruals.assert_awaited_once()


@pytest.mark.asyncio
async def test_rollback_review_stops_future_savings_attribution():
    """Rollback transitions stop attribution while preserving the source record."""
    repo = MagicMock()
    repo.get_accrual_watermark = AsyncMock(return_value=None)
    repo.get_accrual_floors = AsyncMock(return_value={})
    repo.append_accruals = AsyncMock(return_value=0)
    attributor = SavingsAttributor(repo, cluster_name="prod")
    now = datetime.now(timezone.utc)

    await attributor.accrue(
        [_recommendation(RecommendationStatus.ROLLBACK_REVIEW, applied_at=now - timedelta(hours=3))], now=now
    )

    call = repo.append_accruals.await_args
    assert call is not None
    records = call.args[0]
    assert records == []
