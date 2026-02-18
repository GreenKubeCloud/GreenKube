# tests/core/test_recommender.py

from datetime import datetime
from unittest.mock import patch

import pytest

from greenkube.core.recommender import Recommender
from greenkube.models.metrics import CombinedMetric, RecommendationType


# Test data fixture
@pytest.fixture
def mock_combined_metrics():
    """Creates a list of CombinedMetric for tests."""
    timestamp = datetime.now()

    # 1. "Zombie" pod: high cost, no usage
    metric_zombie = CombinedMetric(
        timestamp=timestamp,
        pod_name="zombie-pod",
        namespace="default",
        total_cost=0.5,
        joules=10,
        cpu_request=500,
        memory_request=1073741824,
    )

    # 2. Underutilized pod (CPU rightsizing)
    metric_rightsizing = CombinedMetric(
        timestamp=timestamp,
        pod_name="oversized-pod",
        namespace="prod",
        total_cost=1.2,
        joules=50000,
        cpu_request=1000,
        memory_request=2147483648,
        # We simulate that 50000 Joules equals 50 millicores avg usage
        # (This logic will be in the Recommender, here we simulate the state)
    )

    # 3. Healthy pod: good usage
    metric_healthy = CombinedMetric(
        timestamp=timestamp,
        pod_name="healthy-pod",
        namespace="prod",
        total_cost=1.0,
        joules=800000,
        cpu_request=1000,
        memory_request=2147483648,
    )

    # 4. Pod with no "request" (cannot be rightsized)
    metric_no_request = CombinedMetric(
        timestamp=timestamp,
        pod_name="no-request-pod",
        namespace="dev",
        total_cost=0.1,
        joules=5000,
        cpu_request=0,
        memory_request=0,
    )

    return [metric_zombie, metric_rightsizing, metric_healthy, metric_no_request]


@pytest.fixture
def recommender():
    """Fixture for a Recommender instance with default config."""
    return Recommender()


def test_generate_zombie_recommendations(recommender, mock_combined_metrics):
    """Tests the correct identification of zombie pods."""
    recs = recommender.generate_zombie_recommendations(mock_combined_metrics)

    assert len(recs) == 1
    rec = recs[0]

    assert rec.pod_name == "zombie-pod"
    assert rec.namespace == "default"
    assert rec.type == RecommendationType.ZOMBIE_POD
    assert "cost" in rec.description
    assert "0.5" in rec.description
    assert "Joules" in rec.description


def test_generate_rightsizing_recommendations(recommender, mock_combined_metrics):
    """Tests the correct identification of pods for rightsizing."""

    # Test Logic: The Recommender will need an estimate of CPU usage
    # from Joules. For TDD, we'll MOCK this conversion function.

    # Hypothesis for this test:
    # 800000 Joules (healthy) -> 80% usage
    # 50000 Joules (oversized) -> 5% usage
    # 10 Joules (zombie) -> 0% usage

    # We simulate this conversion (it will be in the Recommender)
    # In this test, we'll patch a private method `_estimate_cpu_usage_percent`

    def mock_estimate_usage(metric, all_metrics):
        if metric.pod_name == "oversized-pod":
            return 0.05  # 5% usage
        if metric.pod_name == "healthy-pod":
            return 0.80  # 80% usage
        if metric.pod_name == "zombie-pod":
            return 0.00
        if metric.pod_name == "no-request-pod":
            return 0.10  # 10% usage (but no request)
        return 0

    with patch.object(Recommender, "_estimate_cpu_usage_percent_legacy", side_effect=mock_estimate_usage):
        recs = recommender.generate_rightsizing_recommendations(mock_combined_metrics)

        assert len(recs) == 1
        rec = recs[0]

        assert rec.pod_name == "oversized-pod"
        assert rec.namespace == "prod"
        assert rec.type == RecommendationType.RIGHTSIZING_CPU
        assert "is only using 5.0%" in rec.description
        assert "of its requested 1000m" in rec.description


def test_no_recommendations_for_healthy_or_no_request_pods(recommender, mock_combined_metrics):
    """Checks that no recommendations are generated for healthy or no-request pods."""

    # Take only the "healthy" and "no-request" pods
    healthy_metrics = [m for m in mock_combined_metrics if m.pod_name in ["healthy-pod", "no-request-pod"]]

    def mock_estimate_usage(metric, all_metrics):
        if metric.pod_name == "healthy-pod":
            return 0.80  # 80% usage
        if metric.pod_name == "no-request-pod":
            return 0.10
        return 0

    with patch.object(Recommender, "_estimate_cpu_usage_percent_legacy", side_effect=mock_estimate_usage):
        rightsizing_recs = recommender.generate_rightsizing_recommendations(healthy_metrics)
        zombie_recs = recommender.generate_zombie_recommendations(healthy_metrics)

        assert len(rightsizing_recs) == 0
        assert len(zombie_recs) == 0


def test_recommender_handles_empty_list(recommender):
    """Tests that the recommender does not crash with an empty list."""
    recs_zombie = recommender.generate_zombie_recommendations([])
    recs_rightsizing = recommender.generate_rightsizing_recommendations([])

    assert recs_zombie == []
    assert recs_rightsizing == []
