# tests/api/conftest.py
"""
Shared fixtures for API tests.
Uses FastAPI's TestClient with dependency overrides to inject mock repositories.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from greenkube.api.app import create_app
from greenkube.api.dependencies import (
    get_carbon_repository,
    get_node_repository,
)
from greenkube.models.metrics import CombinedMetric
from greenkube.models.node import NodeInfo


@pytest.fixture
def mock_carbon_repo():
    """Returns a mock CarbonIntensityRepository."""
    repo = AsyncMock()
    repo.read_combined_metrics = AsyncMock(return_value=[])
    repo.write_combined_metrics = AsyncMock(return_value=0)
    return repo


@pytest.fixture
def mock_node_repo():
    """Returns a mock NodeRepository."""
    repo = AsyncMock()
    repo.get_latest_snapshots_before = AsyncMock(return_value=[])
    repo.get_snapshots = AsyncMock(return_value=[])
    return repo


@pytest.fixture
def client(mock_carbon_repo, mock_node_repo):
    """Creates a TestClient with dependency overrides for all repositories."""
    app = create_app()
    app.dependency_overrides[get_carbon_repository] = lambda: mock_carbon_repo
    app.dependency_overrides[get_node_repository] = lambda: mock_node_repo
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
