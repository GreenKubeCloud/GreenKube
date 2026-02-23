# tests/core/test_recommender_hpa.py
"""
Tests for HPA-aware autoscaling recommendations.
TDD: Tests written before implementation.
"""

from datetime import datetime, timedelta, timezone
from typing import Set, Tuple

import pytest

from greenkube.core.recommender import Recommender
from greenkube.models.metrics import CombinedMetric, RecommendationType


def _ts(hour: int = 12) -> datetime:
    return datetime(2026, 1, 1, hour, 0, 0, tzinfo=timezone.utc)


def _make_spiky_timeseries(
    pod_name: str = "spiky-pod",
    namespace: str = "default",
    owner_kind: str = "Deployment",
    owner_name: str = "spiky-deploy",
):
    """Creates a time series with spiky CPU patterns that triggers autoscaling recommendation."""
    # High variance: alternating low and high usage
    usages = [100, 100, 100, 3000, 100, 100, 100, 3000, 100, 100, 100, 3000]
    metrics = []
    base = _ts(0)
    for i, cpu in enumerate(usages):
        metrics.append(
            CombinedMetric(
                pod_name=pod_name,
                namespace=namespace,
                cpu_request=2000,
                memory_request=512 * 1024 * 1024,
                cpu_usage_millicores=cpu,
                memory_usage_bytes=256 * 1024 * 1024,
                joules=50000.0,
                total_cost=0.10,
                co2e_grams=5.0,
                timestamp=base + timedelta(minutes=i * 5),
                duration_seconds=300,
                owner_kind=owner_kind,
                owner_name=owner_name,
            )
        )
    return metrics


@pytest.fixture
def recommender():
    return Recommender()


class TestHPAAwareAutoscaling:
    """Tests for autoscaling recommendations with HPA detection."""

    def test_autoscaling_recommendation_without_hpa(self, recommender):
        """A spiky pod WITHOUT an HPA should get an autoscaling recommendation."""
        metrics = _make_spiky_timeseries()
        recs = recommender.generate_recommendations(metrics)
        autoscale_recs = [r for r in recs if r.type == RecommendationType.AUTOSCALING_CANDIDATE]
        assert len(autoscale_recs) >= 1

    def test_autoscaling_recommendation_skipped_with_hpa(self, recommender):
        """A spiky pod WITH an existing HPA should NOT get an autoscaling recommendation."""
        metrics = _make_spiky_timeseries(
            pod_name="hpa-pod",
            namespace="default",
            owner_kind="Deployment",
            owner_name="hpa-deploy",
        )
        hpa_targets: Set[Tuple[str, str, str]] = {
            ("default", "Deployment", "hpa-deploy"),
        }
        recs = recommender.generate_recommendations(metrics, hpa_targets=hpa_targets)
        autoscale_recs = [r for r in recs if r.type == RecommendationType.AUTOSCALING_CANDIDATE]
        assert len(autoscale_recs) == 0

    def test_autoscaling_recommendation_different_namespace_not_filtered(self, recommender):
        """An HPA in a different namespace should NOT filter recommendations."""
        metrics = _make_spiky_timeseries(
            pod_name="spiky-pod",
            namespace="team-a",
            owner_kind="Deployment",
            owner_name="spiky-deploy",
        )
        hpa_targets: Set[Tuple[str, str, str]] = {
            ("team-b", "Deployment", "spiky-deploy"),
        }
        recs = recommender.generate_recommendations(metrics, hpa_targets=hpa_targets)
        autoscale_recs = [r for r in recs if r.type == RecommendationType.AUTOSCALING_CANDIDATE]
        assert len(autoscale_recs) >= 1

    def test_autoscaling_recommendation_no_owner_metadata(self, recommender):
        """A spiky pod without owner metadata should still get recommendation even with HPA targets."""
        metrics = _make_spiky_timeseries(
            pod_name="orphan-pod",
            owner_kind=None,
            owner_name=None,
        )
        hpa_targets: Set[Tuple[str, str, str]] = {
            ("default", "Deployment", "some-deploy"),
        }
        recs = recommender.generate_recommendations(metrics, hpa_targets=hpa_targets)
        autoscale_recs = [r for r in recs if r.type == RecommendationType.AUTOSCALING_CANDIDATE]
        assert len(autoscale_recs) >= 1

    def test_empty_hpa_targets_does_not_filter(self, recommender):
        """An empty HPA targets set should not filter any recommendations."""
        metrics = _make_spiky_timeseries()
        recs = recommender.generate_recommendations(metrics, hpa_targets=set())
        autoscale_recs = [r for r in recs if r.type == RecommendationType.AUTOSCALING_CANDIDATE]
        assert len(autoscale_recs) >= 1

    def test_none_hpa_targets_does_not_filter(self, recommender):
        """None hpa_targets (default) should not filter any recommendations."""
        metrics = _make_spiky_timeseries()
        recs = recommender.generate_recommendations(metrics, hpa_targets=None)
        autoscale_recs = [r for r in recs if r.type == RecommendationType.AUTOSCALING_CANDIDATE]
        assert len(autoscale_recs) >= 1
