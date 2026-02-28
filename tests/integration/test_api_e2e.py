# tests/integration/test_api_e2e.py
"""
End-to-end API integration tests.

These tests exercise the full FastAPI → Router → Repository → SQLite chain
using an in-memory SQLite database with real repository implementations,
validating that the API contracts are not broken at the integration boundary.

See: TEST-001 in the issue plan.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from greenkube.api.app import create_app
from greenkube.api.dependencies import (
    get_carbon_repository,
    get_node_repository,
    get_recommendation_repository,
)
from greenkube.core.db import db_manager
from greenkube.models.metrics import CombinedMetric
from greenkube.storage.sqlite_node_repository import SQLiteNodeRepository
from greenkube.storage.sqlite_recommendation_repository import SQLiteRecommendationRepository
from greenkube.storage.sqlite_repository import SQLiteCarbonIntensityRepository


@pytest.fixture
async def sqlite_repos():
    """Set up in-memory SQLite and yield real repository instances."""
    await db_manager.setup_sqlite(db_path=":memory:")
    carbon_repo = SQLiteCarbonIntensityRepository(db_manager)
    node_repo = SQLiteNodeRepository(db_manager)
    reco_repo = SQLiteRecommendationRepository(db_manager)
    yield carbon_repo, node_repo, reco_repo
    await db_manager.close()


@pytest.fixture
def e2e_client(sqlite_repos):
    """Create a TestClient wired to real SQLite repositories."""
    carbon_repo, node_repo, reco_repo = sqlite_repos

    with patch("greenkube.api.routers.recommendations.HPACollector") as mock_hpa:
        instance = AsyncMock()
        instance.collect = AsyncMock(return_value=set())
        mock_hpa.return_value = instance

        app = create_app()
        app.dependency_overrides[get_carbon_repository] = lambda: carbon_repo
        app.dependency_overrides[get_node_repository] = lambda: node_repo
        app.dependency_overrides[get_recommendation_repository] = lambda: reco_repo
        with TestClient(app) as c:
            yield c, carbon_repo
        app.dependency_overrides.clear()


def _make_metric(pod: str, namespace: str, co2: float, cost: float, ts: datetime) -> CombinedMetric:
    return CombinedMetric(
        pod_name=pod,
        namespace=namespace,
        total_cost=cost,
        co2e_grams=co2,
        joules=1000.0,
        timestamp=ts,
        pue=1.2,
        grid_intensity=50.0,
    )


# ── Tests ──────────────────────────────────────────────────────────────


class TestMetricsE2E:
    """Full round-trip: write metrics to SQLite, then read them via the API."""

    @pytest.mark.asyncio
    async def test_metrics_empty(self, e2e_client):
        client, _ = e2e_client
        resp = client.get("/api/v1/metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    @pytest.mark.asyncio
    async def test_metrics_round_trip(self, e2e_client):
        client, repo = e2e_client
        now = datetime.now(timezone.utc)
        metrics = [
            _make_metric("pod-a", "ns-1", 5.0, 0.01, now),
            _make_metric("pod-b", "ns-2", 3.0, 0.02, now),
        ]
        await repo.write_combined_metrics(metrics)

        resp = client.get("/api/v1/metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        names = {m["pod_name"] for m in data["items"]}
        assert names == {"pod-a", "pod-b"}

    @pytest.mark.asyncio
    async def test_metrics_namespace_filter(self, e2e_client):
        client, repo = e2e_client
        now = datetime.now(timezone.utc)
        await repo.write_combined_metrics(
            [
                _make_metric("pod-a", "ns-1", 5.0, 0.01, now),
                _make_metric("pod-b", "ns-2", 3.0, 0.02, now),
            ]
        )

        resp = client.get("/api/v1/metrics?namespace=ns-1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["namespace"] == "ns-1"

    @pytest.mark.asyncio
    async def test_metrics_pagination(self, e2e_client):
        client, repo = e2e_client
        now = datetime.now(timezone.utc)
        metrics = [_make_metric(f"pod-{i}", "ns", 1.0, 0.001, now) for i in range(5)]
        await repo.write_combined_metrics(metrics)

        resp = client.get("/api/v1/metrics?offset=2&limit=2")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert data["offset"] == 2
        assert data["limit"] == 2
        assert len(data["items"]) == 2


class TestSummaryE2E:
    """Summary endpoint with real data."""

    @pytest.mark.asyncio
    async def test_summary_aggregates(self, e2e_client):
        client, repo = e2e_client
        now = datetime.now(timezone.utc)
        await repo.write_combined_metrics(
            [
                _make_metric("pod-a", "ns-1", 5.0, 0.01, now),
                _make_metric("pod-b", "ns-2", 3.0, 0.02, now),
            ]
        )

        resp = client.get("/api/v1/metrics/summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_co2e_grams"] == pytest.approx(8.0)
        assert data["total_cost"] == pytest.approx(0.03)
        assert data["pod_count"] == 2
        assert data["namespace_count"] == 2


class TestHealthE2E:
    """Health endpoint always works."""

    def test_health(self, e2e_client):
        client, _ = e2e_client
        resp = client.get("/api/v1/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
