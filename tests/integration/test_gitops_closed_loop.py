"""Reliability checks for the composed recommendation/GitOps loop.

The closed loop is deliberately best effort at its boundaries: a failed
startup refresh or ledger write must not take down the API or lose the
ability to serve health traffic.  These tests exercise those recovery
contracts without requiring a Kubernetes or Git provider.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

from greenkube.api.app import lifespan
from greenkube.core.savings_attributor import SavingsAttributor
from greenkube.models.metrics import RecommendationRecord, RecommendationStatus, RecommendationType


def _recommendation(status=RecommendationStatus.APPLIED) -> RecommendationRecord:
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
    )


@pytest.mark.asyncio
async def test_startup_refresh_failure_does_not_block_shutdown_or_health_state():
    """A failed background refresh is isolated and resources still close."""
    db = MagicMock()
    db.connect = AsyncMock()
    db.close = AsyncMock()
    app = FastAPI()

    with (
        patch("greenkube.core.db.get_db_manager", return_value=db),
        patch(
            "greenkube.api.app.run_startup_recommendation_scan",
            new=AsyncMock(side_effect=RuntimeError("transient provider outage")),
        ),
        patch("greenkube.core.k8s_client.close_k8s_client", new=AsyncMock()),
    ):
        async with lifespan(app):
            assert app.state.ready is True
            with pytest.raises(RuntimeError, match="transient provider outage"):
                await app.state._startup_tasks[0]

    assert app.state.ready is False
    db.connect.assert_awaited_once()
    db.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_savings_ledger_failure_is_recoverable():
    """A ledger outage returns zero and leaves the collection loop alive."""
    repo = MagicMock()
    repo.save_records = AsyncMock(side_effect=ConnectionError("ledger unavailable"))
    attributor = SavingsAttributor(repo, cluster_name="prod")
    end = datetime.now(timezone.utc)

    result = await attributor.attribute_period(
        [_recommendation()],
        period_start=end - timedelta(minutes=5),
        period_end=end,
    )

    assert result == 0
    repo.save_records.assert_awaited_once()


def test_rollback_review_stops_future_savings_attribution():
    """Rollback transitions stop attribution while preserving the source record."""
    repo = MagicMock()
    attributor = SavingsAttributor(repo, cluster_name="prod")
    end = datetime.now(timezone.utc)

    records = attributor._compute_period_records(
        [_recommendation(RecommendationStatus.ROLLBACK_REVIEW)],
        period_start=end - timedelta(minutes=5),
        period_end=end,
    )

    assert records == []
