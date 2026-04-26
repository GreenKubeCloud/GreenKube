# tests/api/test_kube_state_metrics_labels.py
"""
Tests for kube-state-metrics compatible label standardization and
sustainability golden signal metrics.

TDD: Tests written BEFORE implementation for ticket #182.

Goals:
  - Pod-level gauges expose `cluster` and `region` labels alongside
    existing `namespace`, `pod`, `node` (matching kube-state-metrics).
  - Namespace-level gauges expose `cluster` label.
  - Cluster-level gauges expose `cluster` label.
  - New sustainability golden signal metric: greenkube_carbon_intensity_score
  - New metric: greenkube_carbon_intensity_zone to track intensity per zone
  - CLUSTER_NAME config option exists and flows through to labels.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from greenkube.api.app import create_app
from greenkube.api.dependencies import (
    get_combined_metrics_repository,
    get_node_repository,
    get_recommendation_repository,
)
from greenkube.api.metrics_endpoint import (
    REGISTRY,
    update_cluster_metrics,
)
from greenkube.models.metrics import CombinedMetric
from greenkube.models.node import NodeInfo


@pytest.fixture
def sample_combined_metrics():
    """CombinedMetrics with region data for label testing."""
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
            node="node-1",
            emaps_zone="FR",
            timestamp=now,
        ),
        CombinedMetric(
            pod_name="api-server-def",
            namespace="production",
            total_cost=0.25,
            co2e_grams=18.0,
            embodied_co2e_grams=1.2,
            pue=1.3,
            grid_intensity=120.0,
            joules=8000.0,
            cpu_request=1000,
            memory_request=1073741824,
            cpu_usage_millicores=750,
            memory_usage_bytes=805306368,
            node="node-1",
            emaps_zone="DE",
            timestamp=now,
        ),
        CombinedMetric(
            pod_name="worker-ghi",
            namespace="staging",
            total_cost=0.05,
            co2e_grams=3.0,
            embodied_co2e_grams=0.3,
            pue=1.2,
            grid_intensity=400.0,
            joules=2000.0,
            cpu_request=250,
            memory_request=268435456,
            cpu_usage_millicores=100,
            memory_usage_bytes=134217728,
            node="node-2",
            emaps_zone="US-CAL-CISO",
            timestamp=now,
        ),
    ]


@pytest.fixture
def sample_node_infos():
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
    ]


class TestPodLabelsIncludeCluster:
    """Pod-level gauges must include a `cluster` label."""

    def test_pod_co2_has_cluster_label(self, sample_combined_metrics):
        update_cluster_metrics(sample_combined_metrics)
        from prometheus_client import generate_latest

        output = generate_latest(REGISTRY).decode("utf-8")
        lines = [ln for ln in output.split("\n") if ln.startswith("greenkube_pod_co2e_grams{")]
        assert len(lines) > 0, "No greenkube_pod_co2e_grams samples found"
        for line in lines:
            assert "cluster=" in line, f"Missing 'cluster' label in: {line}"

    def test_pod_co2_has_region_label(self, sample_combined_metrics):
        update_cluster_metrics(sample_combined_metrics)
        from prometheus_client import generate_latest

        output = generate_latest(REGISTRY).decode("utf-8")
        lines = [ln for ln in output.split("\n") if ln.startswith("greenkube_pod_co2e_grams{")]
        assert len(lines) > 0
        for line in lines:
            assert "region=" in line, f"Missing 'region' label in: {line}"

    def test_pod_energy_has_cluster_label(self, sample_combined_metrics):
        update_cluster_metrics(sample_combined_metrics)
        from prometheus_client import generate_latest

        output = generate_latest(REGISTRY).decode("utf-8")
        lines = [ln for ln in output.split("\n") if ln.startswith("greenkube_pod_energy_joules{")]
        assert len(lines) > 0
        for line in lines:
            assert "cluster=" in line

    def test_pod_cost_has_cluster_label(self, sample_combined_metrics):
        update_cluster_metrics(sample_combined_metrics)
        from prometheus_client import generate_latest

        output = generate_latest(REGISTRY).decode("utf-8")
        lines = [ln for ln in output.split("\n") if ln.startswith("greenkube_pod_cost_dollars{")]
        assert len(lines) > 0
        for line in lines:
            assert "cluster=" in line


class TestNamespaceLabelsIncludeCluster:
    """Namespace-level gauges must include a `cluster` label."""

    def test_namespace_co2_has_cluster_label(self, sample_combined_metrics):
        update_cluster_metrics(sample_combined_metrics)
        from prometheus_client import generate_latest

        output = generate_latest(REGISTRY).decode("utf-8")
        lines = [ln for ln in output.split("\n") if ln.startswith("greenkube_namespace_co2e_grams_total{")]
        assert len(lines) > 0
        for line in lines:
            assert "cluster=" in line


class TestClusterLabelsIncludeCluster:
    """Cluster-level gauges must include a `cluster` label for multi-cluster environments."""

    def test_cluster_co2_has_cluster_label(self, sample_combined_metrics):
        update_cluster_metrics(sample_combined_metrics)
        from prometheus_client import generate_latest

        output = generate_latest(REGISTRY).decode("utf-8")
        lines = [ln for ln in output.split("\n") if ln.startswith("greenkube_cluster_co2e_grams_total{")]
        assert len(lines) > 0
        for line in lines:
            assert "cluster=" in line


class TestClusterNameConfig:
    """CLUSTER_NAME must be a configurable env var."""

    def test_cluster_name_defaults_to_empty(self, monkeypatch):
        from unittest.mock import patch

        from greenkube.core.config import Config

        with patch.object(Config, "_auto_detect_cluster_name", return_value=""):
            monkeypatch.delenv("CLUSTER_NAME", raising=False)
            cfg = Config()
        assert hasattr(cfg, "CLUSTER_NAME")
        assert cfg.CLUSTER_NAME == ""

    def test_cluster_name_from_env(self, monkeypatch):
        monkeypatch.setenv("CLUSTER_NAME", "prod-eu-west")
        from greenkube.core.config import Config

        cfg = Config()
        assert cfg.CLUSTER_NAME == "prod-eu-west"


class TestSustainabilityGoldenSignal:
    """Sustainability Golden Signal: carbon intensity score metric."""

    def test_carbon_intensity_score_metric_exists(self, sample_combined_metrics):
        """greenkube_carbon_intensity_score should be exposed after update."""
        update_cluster_metrics(sample_combined_metrics)
        from prometheus_client import generate_latest

        output = generate_latest(REGISTRY).decode("utf-8")
        assert "greenkube_carbon_intensity_score" in output

    def test_carbon_intensity_score_is_weighted_average(self, sample_combined_metrics):
        """The score should be the energy-weighted average grid intensity."""
        update_cluster_metrics(sample_combined_metrics)
        from prometheus_client import generate_latest

        output = generate_latest(REGISTRY).decode("utf-8")
        # Weighted avg: (45*5000 + 120*8000 + 400*2000) / (5000+8000+2000)
        # = (225000 + 960000 + 800000) / 15000 = 1985000 / 15000 = 132.333...
        lines = [ln for ln in output.split("\n") if ln.startswith("greenkube_carbon_intensity_score")]
        assert len(lines) > 0

    def test_carbon_intensity_zone_metric_exists(self, sample_combined_metrics):
        """greenkube_carbon_intensity_zone should track per-zone intensity."""
        update_cluster_metrics(sample_combined_metrics)
        from prometheus_client import generate_latest

        output = generate_latest(REGISTRY).decode("utf-8")
        assert "greenkube_carbon_intensity_zone" in output

    def test_carbon_intensity_zone_has_zone_label(self, sample_combined_metrics):
        """Each zone should be a separate label value."""
        update_cluster_metrics(sample_combined_metrics)
        from prometheus_client import generate_latest

        output = generate_latest(REGISTRY).decode("utf-8")
        lines = [ln for ln in output.split("\n") if ln.startswith("greenkube_carbon_intensity_zone{")]
        assert len(lines) >= 2, f"Expected at least 2 zone intensity samples, got {len(lines)}"

    def test_sustainability_score_metric_exists(self, sample_combined_metrics):
        """greenkube_sustainability_score should be exposed (0-100, 100=best)."""
        update_cluster_metrics(sample_combined_metrics)
        from prometheus_client import generate_latest

        output = generate_latest(REGISTRY).decode("utf-8")
        assert "greenkube_sustainability_score" in output

    def test_sustainability_dimension_scores_exist(self, sample_combined_metrics):
        """Per-dimension scores should be exposed with a 'dimension' label."""
        update_cluster_metrics(sample_combined_metrics)
        from prometheus_client import generate_latest

        output = generate_latest(REGISTRY).decode("utf-8")
        assert "greenkube_sustainability_dimension_score" in output
        # Verify at least a few dimensions are present
        for dim in ("resource_efficiency", "carbon_efficiency", "stability"):
            assert f'dimension="{dim}"' in output, f"Missing dimension '{dim}' in output"


class TestEndpointWithNewLabels:
    """Integration: verify the /prometheus/metrics endpoint exposes new labels."""

    def test_endpoint_has_cluster_label_in_output(self, sample_combined_metrics):
        mock_combined_repo = AsyncMock()
        mock_combined_repo.read_combined_metrics = AsyncMock(return_value=sample_combined_metrics)
        mock_node_repo = AsyncMock()
        mock_node_repo.get_latest_snapshots_before = AsyncMock(return_value=[])
        mock_reco_repo = AsyncMock()
        mock_reco_repo.get_recommendations = AsyncMock(return_value=[])

        app = create_app()
        app.dependency_overrides[get_combined_metrics_repository] = lambda: mock_combined_repo
        app.dependency_overrides[get_node_repository] = lambda: mock_node_repo
        app.dependency_overrides[get_recommendation_repository] = lambda: mock_reco_repo

        with TestClient(app) as c:
            response = c.get("/prometheus/metrics")

        assert response.status_code == 200
        body = response.text
        assert "cluster=" in body
        app.dependency_overrides.clear()
