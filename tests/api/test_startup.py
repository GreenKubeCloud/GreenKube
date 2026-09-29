# tests/api/test_startup.py
"""Tests for src/greenkube/api/startup.py — startup recommendation scan."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from greenkube.api.startup import run_startup_recommendation_scan
from greenkube.models.metrics import RecommendationRecord, RecommendationType


def _make_reco_record(pod_name="nginx", namespace="default"):
    return RecommendationRecord(
        pod_name=pod_name,
        namespace=namespace,
        type=RecommendationType.RIGHTSIZING_CPU,
        description="Reduce CPU request",
        reason="Low utilisation",
        priority="medium",
        potential_savings_cost=0.50,
        potential_savings_co2e_grams=5.0,
        current_cpu_request_millicores=500,
        recommended_cpu_request_millicores=100,
    )


@pytest.fixture
def mock_repos():
    """Patch all three factory functions to return mock repositories."""
    combined_repo = AsyncMock()
    node_repo = AsyncMock()
    reco_repo = AsyncMock()
    with (
        patch("greenkube.api.startup.get_combined_metrics_repository", return_value=combined_repo),
        patch("greenkube.api.startup.get_node_repository", return_value=node_repo),
        patch("greenkube.api.startup.get_recommendation_repository", return_value=reco_repo),
    ):
        yield combined_repo, node_repo, reco_repo


@pytest.fixture
def mock_engine():
    """Patch the optimization engine used by the startup scan."""
    engine = MagicMock()
    engine.refresh = AsyncMock(return_value=[])
    with patch("greenkube.api.startup.OptimizationEngine", return_value=engine) as cls:
        yield cls, engine


@pytest.fixture(autouse=True)
def mock_update_metrics():
    """Suppress Prometheus gauge writes in all startup tests."""
    with patch("greenkube.api.startup.update_recommendation_metrics") as m:
        yield m


@pytest.mark.asyncio
class TestRunStartupRecommendationScan:
    """Unit tests for run_startup_recommendation_scan."""

    async def test_refreshes_engine_with_repositories(self, mock_repos, mock_engine):
        """Should build the engine and refresh recommendations from repositories."""
        combined_repo, node_repo, reco_repo = mock_repos
        _, engine = mock_engine

        await run_startup_recommendation_scan()

        engine.refresh.assert_awaited_once_with(combined_repo, node_repo, reco_repo, namespace=None)

    async def test_updates_metrics_with_generated_recommendations(self, mock_repos, mock_engine, mock_update_metrics):
        """Should publish generated recommendations to Prometheus gauges."""
        reco = _make_reco_record()
        _, engine = mock_engine
        engine.refresh = AsyncMock(return_value=[reco])

        await run_startup_recommendation_scan()

        mock_update_metrics.assert_called_once_with([reco])

    async def test_updates_metrics_when_no_recommendations(self, mock_repos, mock_engine, mock_update_metrics):
        """An empty result is still published so gauges are reset."""
        await run_startup_recommendation_scan()

        mock_update_metrics.assert_called_once_with([])

    async def test_top_level_exception_is_swallowed(self, mock_repos, mock_engine):
        """Any unexpected top-level error must not propagate (API startup must succeed)."""
        _, engine = mock_engine
        engine.refresh = AsyncMock(side_effect=Exception("catastrophic DB error"))

        await run_startup_recommendation_scan()  # must not raise


def test_heartbeat_route_is_registered_and_public():
    from fastapi.testclient import TestClient

    from greenkube.api.app import create_app

    client = TestClient(create_app())
    response = client.get("/api/v1/health/heartbeat")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_factory_dependencies_share_composed_services():
    from greenkube.api.dependencies import get_automation_service, get_optimization_engine
    from greenkube.core.factory import (
        get_automation_service as factory_automation,
    )
    from greenkube.core.factory import (
        get_optimization_engine as factory_engine,
    )

    assert get_automation_service.__name__ == "get_automation_service"
    assert get_optimization_engine.__name__ == "get_optimization_engine"
    assert factory_automation.cache_info().maxsize == 1
    assert factory_engine.cache_info().maxsize == 1
