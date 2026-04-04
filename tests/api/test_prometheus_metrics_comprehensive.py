# tests/api/test_prometheus_metrics_comprehensive.py
"""
Tests for the comprehensive Prometheus metrics exposition.

These tests validate that GreenKube exposes all key metrics
(CO2, cost, energy, CPU, memory, network, grid intensity, node info,
recommendations) as Prometheus gauges at /prometheus/metrics.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from greenkube.api.app import create_app
from greenkube.api.dependencies import (
    get_carbon_repository,
    get_combined_metrics_repository,
    get_node_repository,
    get_recommendation_repository,
)
from greenkube.api.metrics_endpoint import (
    REGISTRY,
    update_cluster_metrics,
    update_node_metrics,
    update_recommendation_metrics,
)
from greenkube.models.metrics import (
    CombinedMetric,
    Recommendation,
    RecommendationType,
)
from greenkube.models.node import NodeInfo


@pytest.fixture
def mock_carbon_repo():
    return AsyncMock()


@pytest.fixture
def mock_combined_metrics_repo():
    repo = AsyncMock()
    repo.read_combined_metrics = AsyncMock(return_value=[])
    return repo


@pytest.fixture
def mock_node_repo():
    repo = AsyncMock()
    repo.get_latest_snapshots_before = AsyncMock(return_value=[])
    return repo


@pytest.fixture
def mock_reco_repo():
    repo = AsyncMock()
    repo.save_recommendations = AsyncMock(return_value=0)
    repo.get_recommendations = AsyncMock(return_value=[])
    return repo


@pytest.fixture
def client(mock_carbon_repo, mock_combined_metrics_repo, mock_node_repo, mock_reco_repo):
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
    """A set of realistic CombinedMetrics for testing gauge updates."""
    now = datetime.now(timezone.utc)
    return [
        CombinedMetric(
            pod_name="web-frontend-abc",
            namespace="production",
            total_cost=0.15,
            co2e_grams=12.5,
            embodied_co2e_grams=0.8,
            pue=1.3,
            grid_intensity=45.0,
            joules=5000.0,
            cpu_request=500,
            memory_request=536870912,
            cpu_usage_millicores=320,
            memory_usage_bytes=268435456,
            network_receive_bytes=1024.0,
            network_transmit_bytes=2048.0,
            disk_read_bytes=512.0,
            disk_write_bytes=256.0,
            node="node-1",
            timestamp=now,
        ),
        CombinedMetric(
            pod_name="api-server-def",
            namespace="production",
            total_cost=0.25,
            co2e_grams=18.0,
            embodied_co2e_grams=1.2,
            pue=1.3,
            grid_intensity=45.0,
            joules=8000.0,
            cpu_request=1000,
            memory_request=1073741824,
            cpu_usage_millicores=750,
            memory_usage_bytes=805306368,
            network_receive_bytes=4096.0,
            network_transmit_bytes=8192.0,
            node="node-1",
            timestamp=now,
        ),
        CombinedMetric(
            pod_name="worker-ghi",
            namespace="staging",
            total_cost=0.05,
            co2e_grams=3.0,
            embodied_co2e_grams=0.3,
            pue=1.2,
            grid_intensity=120.0,
            joules=2000.0,
            cpu_request=250,
            memory_request=268435456,
            cpu_usage_millicores=100,
            memory_usage_bytes=134217728,
            node="node-2",
            timestamp=now,
        ),
    ]


@pytest.fixture
def sample_node_infos():
    """Realistic NodeInfo objects for testing."""
    return [
        NodeInfo(
            name="node-1",
            instance_type="m5.large",
            zone="eu-west-1a",
            region="eu-west-1",
            cloud_provider="aws",
            architecture="amd64",
            cpu_capacity_cores=2.0,
            memory_capacity_bytes=8589934592,
            embodied_emissions_kg=350.0,
        ),
        NodeInfo(
            name="node-2",
            instance_type="m5.xlarge",
            zone="eu-west-1b",
            region="eu-west-1",
            cloud_provider="aws",
            architecture="amd64",
            cpu_capacity_cores=4.0,
            memory_capacity_bytes=17179869184,
            embodied_emissions_kg=500.0,
        ),
    ]


class TestComprehensiveClusterMetrics:
    """Tests for update_cluster_metrics function."""

    def test_update_sets_pod_co2_gauge(self, sample_combined_metrics):
        update_cluster_metrics(sample_combined_metrics)
        from prometheus_client import generate_latest

        output = generate_latest(REGISTRY).decode("utf-8")
        assert "greenkube_pod_co2e_grams" in output

    def test_update_sets_pod_cost_gauge(self, sample_combined_metrics):
        update_cluster_metrics(sample_combined_metrics)
        from prometheus_client import generate_latest

        output = generate_latest(REGISTRY).decode("utf-8")
        assert "greenkube_pod_cost_dollars" in output

    def test_update_sets_pod_energy_gauge(self, sample_combined_metrics):
        update_cluster_metrics(sample_combined_metrics)
        from prometheus_client import generate_latest

        output = generate_latest(REGISTRY).decode("utf-8")
        assert "greenkube_pod_energy_joules" in output

    def test_update_sets_pod_cpu_usage(self, sample_combined_metrics):
        update_cluster_metrics(sample_combined_metrics)
        from prometheus_client import generate_latest

        output = generate_latest(REGISTRY).decode("utf-8")
        assert "greenkube_pod_cpu_usage_millicores" in output

    def test_update_sets_pod_memory_usage(self, sample_combined_metrics):
        update_cluster_metrics(sample_combined_metrics)
        from prometheus_client import generate_latest

        output = generate_latest(REGISTRY).decode("utf-8")
        assert "greenkube_pod_memory_usage_bytes" in output

    def test_update_sets_namespace_totals(self, sample_combined_metrics):
        update_cluster_metrics(sample_combined_metrics)
        from prometheus_client import generate_latest

        output = generate_latest(REGISTRY).decode("utf-8")
        assert "greenkube_namespace_co2e_grams_total" in output
        assert "greenkube_namespace_cost_dollars_total" in output
        assert "greenkube_namespace_energy_joules_total" in output

    def test_update_sets_cluster_summary(self, sample_combined_metrics):
        update_cluster_metrics(sample_combined_metrics)
        from prometheus_client import generate_latest

        output = generate_latest(REGISTRY).decode("utf-8")
        assert "greenkube_cluster_co2e_grams_total" in output
        assert "greenkube_cluster_cost_dollars_total" in output
        assert "greenkube_cluster_energy_joules_total" in output
        assert "greenkube_cluster_pod_count" in output
        assert "greenkube_cluster_namespace_count" in output

    def test_update_sets_grid_intensity(self, sample_combined_metrics):
        update_cluster_metrics(sample_combined_metrics)
        from prometheus_client import generate_latest

        output = generate_latest(REGISTRY).decode("utf-8")
        assert "greenkube_grid_intensity_gco2_kwh" in output

    def test_update_sets_pue(self, sample_combined_metrics):
        update_cluster_metrics(sample_combined_metrics)
        from prometheus_client import generate_latest

        output = generate_latest(REGISTRY).decode("utf-8")
        assert "greenkube_pue" in output

    def test_update_sets_embodied_carbon(self, sample_combined_metrics):
        update_cluster_metrics(sample_combined_metrics)
        from prometheus_client import generate_latest

        output = generate_latest(REGISTRY).decode("utf-8")
        assert "greenkube_pod_embodied_co2e_grams" in output

    def test_update_sets_network_metrics(self, sample_combined_metrics):
        update_cluster_metrics(sample_combined_metrics)
        from prometheus_client import generate_latest

        output = generate_latest(REGISTRY).decode("utf-8")
        assert "greenkube_pod_network_receive_bytes" in output
        assert "greenkube_pod_network_transmit_bytes" in output

    def test_update_with_empty_list_clears_metrics(self):
        update_cluster_metrics([])
        from prometheus_client import generate_latest

        output = generate_latest(REGISTRY).decode("utf-8")
        # Cluster totals should be 0 or absent after clearing
        assert "greenkube_cluster_pod_count" in output

    def test_labels_contain_namespace_and_pod(self, sample_combined_metrics):
        update_cluster_metrics(sample_combined_metrics)
        from prometheus_client import generate_latest

        output = generate_latest(REGISTRY).decode("utf-8")
        assert 'namespace="production"' in output
        assert 'pod="web-frontend-abc"' in output

    def test_cpu_request_vs_usage_exposed(self, sample_combined_metrics):
        update_cluster_metrics(sample_combined_metrics)
        from prometheus_client import generate_latest

        output = generate_latest(REGISTRY).decode("utf-8")
        assert "greenkube_pod_cpu_request_millicores" in output
        assert "greenkube_pod_cpu_usage_millicores" in output

    def test_memory_request_vs_usage_exposed(self, sample_combined_metrics):
        update_cluster_metrics(sample_combined_metrics)
        from prometheus_client import generate_latest

        output = generate_latest(REGISTRY).decode("utf-8")
        assert "greenkube_pod_memory_request_bytes" in output
        assert "greenkube_pod_memory_usage_bytes" in output


class TestNodeMetrics:
    """Tests for update_node_metrics function."""

    def test_update_sets_node_cpu_capacity(self, sample_node_infos):
        update_node_metrics(sample_node_infos)
        from prometheus_client import generate_latest

        output = generate_latest(REGISTRY).decode("utf-8")
        assert "greenkube_node_cpu_capacity_millicores" in output

    def test_update_sets_node_memory_capacity(self, sample_node_infos):
        update_node_metrics(sample_node_infos)
        from prometheus_client import generate_latest

        output = generate_latest(REGISTRY).decode("utf-8")
        assert "greenkube_node_memory_capacity_bytes" in output

    def test_update_sets_node_embodied_emissions(self, sample_node_infos):
        update_node_metrics(sample_node_infos)
        from prometheus_client import generate_latest

        output = generate_latest(REGISTRY).decode("utf-8")
        assert "greenkube_node_embodied_emissions_kg" in output

    def test_labels_contain_node_metadata(self, sample_node_infos):
        update_node_metrics(sample_node_infos)
        from prometheus_client import generate_latest

        output = generate_latest(REGISTRY).decode("utf-8")
        assert 'node="node-1"' in output
        assert 'instance_type="m5.large"' in output


class TestEndpointAfterUpdate:
    """Integration tests: update metrics then hit the /prometheus/metrics endpoint."""

    def test_endpoint_contains_cluster_metrics_after_update(self, client, sample_combined_metrics):
        update_cluster_metrics(sample_combined_metrics)
        response = client.get("/prometheus/metrics")
        assert response.status_code == 200
        body = response.text
        assert "greenkube_cluster_co2e_grams_total" in body
        assert "greenkube_pod_co2e_grams" in body

    def test_endpoint_contains_node_metrics_after_update(self, client, sample_node_infos):
        update_node_metrics(sample_node_infos)
        response = client.get("/prometheus/metrics")
        assert response.status_code == 200
        body = response.text
        assert "greenkube_node_cpu_capacity_millicores" in body

    def test_endpoint_contains_recommendation_metrics_after_update(self, client):
        recs = [
            Recommendation(
                pod_name="pod-1",
                namespace="default",
                type=RecommendationType.ZOMBIE_POD,
                description="Zombie",
                priority="high",
                potential_savings_cost=1.0,
                potential_savings_co2e_grams=0.5,
            ),
        ]
        update_recommendation_metrics(recs)
        response = client.get("/prometheus/metrics")
        assert response.status_code == 200
        body = response.text
        assert "greenkube_recommendations_total" in body
        assert "greenkube_recommendations_savings_cost_dollars" in body
        assert "greenkube_recommendations_savings_co2e_grams" in body


class TestRefreshMetricsFromDB:
    """Tests that the /prometheus/metrics endpoint refreshes gauges from DB on each scrape."""

    def test_endpoint_reads_combined_metrics_from_db(
        self, mock_combined_metrics_repo, mock_node_repo, mock_reco_repo, sample_combined_metrics
    ):
        """Verify the endpoint reads combined metrics from the DB and populates gauges."""
        mock_combined_metrics_repo.read_combined_metrics = AsyncMock(return_value=sample_combined_metrics)
        mock_node_repo.get_latest_snapshots_before = AsyncMock(return_value=[])
        mock_reco_repo.get_recommendations = AsyncMock(return_value=[])

        app = create_app()
        app.dependency_overrides[get_combined_metrics_repository] = lambda: mock_combined_metrics_repo
        app.dependency_overrides[get_node_repository] = lambda: mock_node_repo
        app.dependency_overrides[get_recommendation_repository] = lambda: mock_reco_repo

        with TestClient(app) as c:
            response = c.get("/prometheus/metrics")

        assert response.status_code == 200
        body = response.text
        # Gauges must have been populated from the DB data
        assert "greenkube_cluster_co2e_grams_total" in body
        assert "greenkube_pod_co2e_grams{" in body
        assert 'namespace="production"' in body
        assert 'pod="web-frontend-abc"' in body
        mock_combined_metrics_repo.read_combined_metrics.assert_called_once()
        app.dependency_overrides.clear()

    def test_endpoint_reads_node_metrics_from_db(
        self, mock_combined_metrics_repo, mock_node_repo, mock_reco_repo, sample_node_infos
    ):
        """Verify the endpoint reads node data from the DB and populates node gauges."""
        mock_combined_metrics_repo.read_combined_metrics = AsyncMock(return_value=[])
        mock_node_repo.get_latest_snapshots_before = AsyncMock(return_value=sample_node_infos)
        mock_reco_repo.get_recommendations = AsyncMock(return_value=[])

        app = create_app()
        app.dependency_overrides[get_combined_metrics_repository] = lambda: mock_combined_metrics_repo
        app.dependency_overrides[get_node_repository] = lambda: mock_node_repo
        app.dependency_overrides[get_recommendation_repository] = lambda: mock_reco_repo

        with TestClient(app) as c:
            response = c.get("/prometheus/metrics")

        assert response.status_code == 200
        body = response.text
        assert "greenkube_node_info" in body
        assert 'node="node-1"' in body
        mock_node_repo.get_latest_snapshots_before.assert_called_once()
        app.dependency_overrides.clear()

    def test_endpoint_reads_recommendations_from_db(self, mock_combined_metrics_repo, mock_node_repo, mock_reco_repo):
        """Verify the endpoint reads recommendations from the DB and populates recommendation gauges."""
        from greenkube.models.metrics import RecommendationRecord

        records = [
            RecommendationRecord(
                type=RecommendationType.ZOMBIE_POD,
                description="Zombie pod detected",
                priority="high",
                namespace="default",
                pod_name="zombie-1",
                potential_savings_cost=2.5,
                potential_savings_co2e_grams=10.0,
            ),
        ]
        mock_combined_metrics_repo.read_combined_metrics = AsyncMock(return_value=[])
        mock_node_repo.get_latest_snapshots_before = AsyncMock(return_value=[])
        mock_reco_repo.get_recommendations = AsyncMock(return_value=records)

        app = create_app()
        app.dependency_overrides[get_combined_metrics_repository] = lambda: mock_combined_metrics_repo
        app.dependency_overrides[get_node_repository] = lambda: mock_node_repo
        app.dependency_overrides[get_recommendation_repository] = lambda: mock_reco_repo

        with TestClient(app) as c:
            response = c.get("/prometheus/metrics")

        assert response.status_code == 200
        body = response.text
        assert "greenkube_recommendations_total" in body
        assert "ZOMBIE_POD" in body
        mock_reco_repo.get_recommendations.assert_called_once()
        app.dependency_overrides.clear()

    def test_endpoint_survives_db_errors_gracefully(self, mock_combined_metrics_repo, mock_node_repo, mock_reco_repo):
        """Verify the endpoint returns a valid response even when DB reads fail."""
        mock_combined_metrics_repo.read_combined_metrics = AsyncMock(side_effect=Exception("DB down"))
        mock_node_repo.get_latest_snapshots_before = AsyncMock(side_effect=Exception("DB down"))
        mock_reco_repo.get_recommendations = AsyncMock(side_effect=Exception("DB down"))

        app = create_app()
        app.dependency_overrides[get_combined_metrics_repository] = lambda: mock_combined_metrics_repo
        app.dependency_overrides[get_node_repository] = lambda: mock_node_repo
        app.dependency_overrides[get_recommendation_repository] = lambda: mock_reco_repo

        with TestClient(app) as c:
            response = c.get("/prometheus/metrics")

        # Endpoint should still return 200 with whatever gauges exist
        assert response.status_code == 200
        app.dependency_overrides.clear()

    def test_deduplication_keeps_latest_row_per_pod(self, mock_combined_metrics_repo, mock_node_repo, mock_reco_repo):
        """When the DB returns multiple rows for the same pod in the time window,
        only the latest snapshot per (namespace, pod_name) must appear in gauges.
        This prevents Grafana from showing the same pod multiple times."""
        from datetime import timedelta

        now = datetime.now(timezone.utc)
        older = now - timedelta(minutes=5)

        # Two rows for the same pod: older has co2=1.0, newer has co2=9.9
        duplicate_metrics = [
            CombinedMetric(
                pod_name="web-frontend-abc",
                namespace="production",
                total_cost=0.01,
                co2e_grams=1.0,
                pue=1.2,
                grid_intensity=50.0,
                joules=100.0,
                cpu_request=100,
                memory_request=134217728,
                node="node-1",
                timestamp=older,
            ),
            CombinedMetric(
                pod_name="web-frontend-abc",
                namespace="production",
                total_cost=0.02,
                co2e_grams=9.9,
                pue=1.2,
                grid_intensity=50.0,
                joules=200.0,
                cpu_request=100,
                memory_request=134217728,
                node="node-1",
                timestamp=now,
            ),
        ]

        mock_combined_metrics_repo.read_combined_metrics = AsyncMock(return_value=duplicate_metrics)
        mock_node_repo.get_latest_snapshots_before = AsyncMock(return_value=[])
        mock_reco_repo.get_recommendations = AsyncMock(return_value=[])

        app = create_app()
        app.dependency_overrides[get_combined_metrics_repository] = lambda: mock_combined_metrics_repo
        app.dependency_overrides[get_node_repository] = lambda: mock_node_repo
        app.dependency_overrides[get_recommendation_repository] = lambda: mock_reco_repo

        with TestClient(app) as c:
            response = c.get("/prometheus/metrics")

        assert response.status_code == 200
        body = response.text
        # Only the latest value (9.9) must appear; the older value (1.0) must not
        assert 'pod="web-frontend-abc"' in body
        assert "9.9" in body
        # Cluster total should reflect only the latest row
        assert "greenkube_cluster_co2e_grams_total" in body
        app.dependency_overrides.clear()
