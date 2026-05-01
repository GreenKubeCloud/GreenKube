# tests/api/conftest.py
"""
Shared fixtures for API tests.
Uses FastAPI's TestClient with dependency overrides to inject mock repositories.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from greenkube.api.app import create_app
from greenkube.api.dependencies import (
    get_carbon_repository,
    get_combined_metrics_repository,
    get_node_repository,
    get_recommendation_repository,
)
from greenkube.models.metrics import CombinedMetric
from greenkube.models.node import NodeInfo


@pytest.fixture(autouse=True)
def mock_hpa_collector():
    """Mock HPA collector to avoid K8s API calls in tests."""
    with patch("greenkube.api.routers.recommendations.HPACollector") as mock_cls:
        instance = AsyncMock()
        instance.collect = AsyncMock(return_value=set())
        mock_cls.return_value = instance
        yield mock_cls


@pytest.fixture(autouse=True)
def mock_k8s_namespaces():
    """Mock Kubernetes namespace listing to avoid real K8s API calls in tests.

    Returns None by default so the namespace filter is bypassed unless the
    individual test explicitly overrides this mock.
    """
    with patch(
        "greenkube.api.routers.recommendations._get_active_k8s_namespaces",
        new=AsyncMock(return_value=None),
    ):
        yield


@pytest.fixture
def mock_carbon_repo():
    """Returns a mock CarbonIntensityRepository."""
    repo = AsyncMock()
    return repo


@pytest.fixture
def mock_combined_metrics_repo():
    """Returns a mock CombinedMetricsRepository."""
    repo = AsyncMock()
    repo.read_combined_metrics = AsyncMock(return_value=[])
    repo.write_combined_metrics = AsyncMock(return_value=0)

    # aggregate_summary and aggregate_timeseries default to the base class
    # Python-side implementation which delegates to read_combined_metrics.
    # We reproduce that delegation here so tests that mock read_combined_metrics
    # automatically get correct aggregate results without additional setup.
    from greenkube.storage.base_repository import CombinedMetricsRepository as _Base

    async def _aggregate_summary(start_time, end_time, namespace=None):
        return await _Base.aggregate_summary(repo, start_time, end_time, namespace=namespace)

    async def _aggregate_timeseries(start_time, end_time, granularity="hour", namespace=None):
        return await _Base.aggregate_timeseries(
            repo, start_time, end_time, granularity=granularity, namespace=namespace
        )

    repo.aggregate_summary = _aggregate_summary
    repo.aggregate_timeseries = _aggregate_timeseries

    # Wire up read_combined_metrics_smart and read_hourly_metrics
    # so that tests mocking read_combined_metrics get correct behavior.
    async def _read_combined_metrics_smart(start_time, end_time, namespace=None):
        return await _Base.read_combined_metrics_smart(repo, start_time, end_time, namespace=namespace)

    async def _read_hourly_metrics(start_time, end_time, namespace=None):
        return await _Base.read_hourly_metrics(repo, start_time, end_time, namespace=namespace)

    async def _list_namespaces():
        return await _Base.list_namespaces(repo)

    repo.read_combined_metrics_smart = _read_combined_metrics_smart
    repo.read_hourly_metrics = _read_hourly_metrics
    repo.list_namespaces = _list_namespaces
    return repo


@pytest.fixture
def mock_node_repo():
    """Returns a mock NodeRepository."""
    repo = AsyncMock()
    repo.get_latest_snapshots_before = AsyncMock(return_value=[])
    repo.get_snapshots = AsyncMock(return_value=[])
    return repo


@pytest.fixture
def mock_reco_repo():
    """Returns a mock RecommendationRepository."""
    repo = AsyncMock()
    repo.save_recommendations = AsyncMock(return_value=0)
    repo.upsert_recommendations = AsyncMock(return_value=0)
    repo.reconcile_active_recommendations = AsyncMock(return_value=0)
    repo.get_recommendations = AsyncMock(return_value=[])
    repo.get_active_recommendations = AsyncMock(return_value=[])
    repo.get_ignored_recommendations = AsyncMock(return_value=[])
    repo.get_applied_recommendations = AsyncMock(return_value=[])
    repo.get_savings_summary = AsyncMock(return_value=None)
    return repo


@pytest.fixture
def client(mock_carbon_repo, mock_combined_metrics_repo, mock_node_repo, mock_reco_repo):
    """Creates a TestClient with dependency overrides for all repositories."""
    app = create_app()
    app.dependency_overrides[get_carbon_repository] = lambda: mock_carbon_repo
    app.dependency_overrides[get_combined_metrics_repository] = lambda: mock_combined_metrics_repo
    app.dependency_overrides[get_node_repository] = lambda: mock_node_repo
    app.dependency_overrides[get_recommendation_repository] = lambda: mock_reco_repo
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def sample_combined_metrics():
    """Returns a list of sample CombinedMetric objects for testing."""
    ts = datetime(2026, 2, 8, 12, 0, 0, tzinfo=timezone.utc)
    return [
        CombinedMetric(
            pod_name="nginx-abc123",
            namespace="default",
            total_cost=0.0042,
            co2e_grams=1.5,
            pue=1.2,
            grid_intensity=50.0,
            joules=5000.0,
            cpu_request=250,
            memory_request=256 * 1024 * 1024,
            timestamp=ts,
            duration_seconds=300,
            node="node-1",
            node_instance_type="m5.large",
            node_zone="eu-west-1a",
            emaps_zone="IE",
            is_estimated=False,
            estimation_reasons=[],
            embodied_co2e_grams=0.05,
        ),
        CombinedMetric(
            pod_name="api-server-xyz",
            namespace="production",
            total_cost=0.0128,
            co2e_grams=4.2,
            pue=1.3,
            grid_intensity=120.0,
            joules=15000.0,
            cpu_request=500,
            memory_request=512 * 1024 * 1024,
            timestamp=ts,
            duration_seconds=300,
            node="node-2",
            node_instance_type="c5.xlarge",
            node_zone="us-east-1b",
            emaps_zone="US-MIDA-PJM",
            is_estimated=True,
            estimation_reasons=["Fallback zone used"],
            embodied_co2e_grams=0.12,
        ),
    ]


@pytest.fixture
def sample_node_infos():
    """Returns a list of sample NodeInfo objects for testing."""
    return [
        NodeInfo(
            name="node-1",
            instance_type="m5.large",
            zone="eu-west-1a",
            region="eu-west-1",
            cloud_provider="aws",
            architecture="amd64",
            cpu_capacity_cores=2.0,
            memory_capacity_bytes=8 * 1024 * 1024 * 1024,
            timestamp=datetime(2026, 2, 8, 11, 0, 0, tzinfo=timezone.utc),
        ),
        NodeInfo(
            name="node-2",
            instance_type="c5.xlarge",
            zone="us-east-1b",
            region="us-east-1",
            cloud_provider="aws",
            architecture="amd64",
            cpu_capacity_cores=4.0,
            memory_capacity_bytes=16 * 1024 * 1024 * 1024,
            timestamp=datetime(2026, 2, 8, 11, 0, 0, tzinfo=timezone.utc),
        ),
    ]
