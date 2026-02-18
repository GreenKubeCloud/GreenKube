# tests/core/test_recommender_v2.py
"""
Comprehensive tests for the enhanced recommendation engine.
Tests cover all 9 recommendation types using TDD methodology.
"""

from datetime import datetime, timedelta, timezone
from typing import List
from unittest.mock import MagicMock

import pytest

from greenkube.core.recommender import Recommender
from greenkube.models.metrics import CombinedMetric, RecommendationType

# ---------------------------------------------------------------------------
# Helpers to build test metrics
# ---------------------------------------------------------------------------


def _ts(hour: int = 12, day: int = 1, month: int = 1, year: int = 2026) -> datetime:
    """Create a UTC timestamp for testing."""
    return datetime(year, month, day, hour, 0, 0, tzinfo=timezone.utc)


def _make_metric(
    pod_name: str = "test-pod",
    namespace: str = "default",
    cpu_request: int = 1000,
    memory_request: int = 512 * 1024 * 1024,
    cpu_usage_millicores: int = 500,
    memory_usage_bytes: int = 256 * 1024 * 1024,
    joules: float = 50000.0,
    total_cost: float = 0.10,
    co2e_grams: float = 5.0,
    timestamp: datetime = None,
    duration_seconds: int = 300,
    node: str = "node-1",
    grid_intensity: float = 100.0,
    emaps_zone: str = "FR",
    owner_kind: str = None,
    owner_name: str = None,
) -> CombinedMetric:
    """Create a CombinedMetric for testing."""
    return CombinedMetric(
        pod_name=pod_name,
        namespace=namespace,
        cpu_request=cpu_request,
        memory_request=memory_request,
        cpu_usage_millicores=cpu_usage_millicores,
        memory_usage_bytes=memory_usage_bytes,
        joules=joules,
        total_cost=total_cost,
        co2e_grams=co2e_grams,
        timestamp=timestamp or _ts(),
        duration_seconds=duration_seconds,
        node=node,
        grid_intensity=grid_intensity,
        emaps_zone=emaps_zone,
        owner_kind=owner_kind,
        owner_name=owner_name,
    )


def _make_timeseries(
    pod_name: str = "spiky-pod",
    namespace: str = "default",
    cpu_request: int = 2000,
    memory_request: int = 1024 * 1024 * 1024,
    usages: list = None,
    memory_usages: list = None,
    start_hour: int = 0,
    interval_minutes: int = 5,
    node: str = "node-1",
    grid_intensity: float = 100.0,
    total_cost: float = 0.01,
    co2e_grams: float = 1.0,
    joules: float = 5000.0,
    owner_kind: str = None,
    owner_name: str = None,
) -> List[CombinedMetric]:
    """Create a time-series of CombinedMetric objects for pattern analysis."""
    if usages is None:
        usages = [500] * 24
    if memory_usages is None:
        memory_usages = [256 * 1024 * 1024] * len(usages)

    metrics = []
    base = _ts(hour=start_hour)
    for i, cpu_usage in enumerate(usages):
        ts = base + timedelta(minutes=i * interval_minutes)
        mem_usage = memory_usages[i] if i < len(memory_usages) else memory_usages[-1]
        metrics.append(
            _make_metric(
                pod_name=pod_name,
                namespace=namespace,
                cpu_request=cpu_request,
                memory_request=memory_request,
                cpu_usage_millicores=cpu_usage,
                memory_usage_bytes=mem_usage,
                timestamp=ts,
                duration_seconds=interval_minutes * 60,
                node=node,
                grid_intensity=grid_intensity,
                total_cost=total_cost,
                co2e_grams=co2e_grams,
                joules=joules,
                owner_kind=owner_kind,
                owner_name=owner_name,
            )
        )
    return metrics


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def recommender():
    """Recommender with default config thresholds."""
    return Recommender()


# ---------------------------------------------------------------------------
# Test: ZOMBIE_POD
# ---------------------------------------------------------------------------


class TestZombiePod:
    """Tests for zombie pod detection."""

    def test_detects_zombie_pod(self, recommender):
        """A pod with cost but near-zero energy should be flagged."""
        metrics = [
            _make_metric(
                pod_name="zombie-pod",
                total_cost=0.05,
                joules=100.0,
                co2e_grams=0.1,
                cpu_usage_millicores=0,
            )
        ]
        recs = recommender.generate_recommendations(metrics)
        zombie_recs = [r for r in recs if r.type == RecommendationType.ZOMBIE_POD]
        assert len(zombie_recs) == 1
        assert zombie_recs[0].pod_name == "zombie-pod"
        assert zombie_recs[0].priority == "high"
        assert zombie_recs[0].potential_savings_cost is not None
        assert zombie_recs[0].potential_savings_cost > 0

    def test_no_zombie_for_active_pod(self, recommender):
        """An active pod should not be flagged as zombie."""
        metrics = [
            _make_metric(
                pod_name="active-pod",
                total_cost=0.05,
                joules=50000.0,
                cpu_usage_millicores=500,
            )
        ]
        recs = recommender.generate_recommendations(metrics)
        zombie_recs = [r for r in recs if r.type == RecommendationType.ZOMBIE_POD]
        assert len(zombie_recs) == 0

    def test_no_zombie_for_free_pod(self, recommender):
        """A pod with no cost should not be flagged even if energy is low."""
        metrics = [
            _make_metric(
                pod_name="free-pod",
                total_cost=0.0,
                joules=10.0,
            )
        ]
        recs = recommender.generate_recommendations(metrics)
        zombie_recs = [r for r in recs if r.type == RecommendationType.ZOMBIE_POD]
        assert len(zombie_recs) == 0

    def test_zombie_includes_savings(self, recommender):
        """Zombie recommendation should estimate savings."""
        metrics = [
            _make_metric(
                pod_name="zombie-pod",
                total_cost=1.50,
                joules=50.0,
                co2e_grams=0.5,
            )
        ]
        recs = recommender.generate_recommendations(metrics)
        zombie_recs = [r for r in recs if r.type == RecommendationType.ZOMBIE_POD]
        assert len(zombie_recs) == 1
        assert zombie_recs[0].potential_savings_cost == pytest.approx(1.50, abs=0.01)
        assert zombie_recs[0].potential_savings_co2e_grams == pytest.approx(0.5, abs=0.01)


# ---------------------------------------------------------------------------
# Test: RIGHTSIZING_CPU
# ---------------------------------------------------------------------------


class TestRightsizingCPU:
    """Tests for CPU rightsizing recommendations."""

    def test_detects_oversized_cpu(self, recommender):
        """Pod using 10% of CPU request should get rightsizing rec."""
        metrics = _make_timeseries(
            pod_name="oversized-cpu",
            cpu_request=2000,
            usages=[200] * 48,  # Consistently low usage
        )
        recs = recommender.generate_recommendations(metrics)
        cpu_recs = [r for r in recs if r.type == RecommendationType.RIGHTSIZING_CPU]
        assert len(cpu_recs) == 1
        assert cpu_recs[0].pod_name == "oversized-cpu"
        assert cpu_recs[0].current_cpu_request_millicores == 2000
        assert cpu_recs[0].recommended_cpu_request_millicores is not None
        assert cpu_recs[0].recommended_cpu_request_millicores < 2000

    def test_no_rightsizing_for_well_used_cpu(self, recommender):
        """Pod using 80% of CPU request should NOT get rightsizing rec."""
        metrics = _make_timeseries(
            pod_name="well-used",
            cpu_request=1000,
            usages=[800] * 48,
        )
        recs = recommender.generate_recommendations(metrics)
        cpu_recs = [r for r in recs if r.type == RecommendationType.RIGHTSIZING_CPU]
        assert len(cpu_recs) == 0

    def test_no_rightsizing_without_cpu_request(self, recommender):
        """Pod with no CPU request should not get rightsizing recommendation."""
        metrics = _make_timeseries(
            pod_name="no-request",
            cpu_request=0,
            usages=[100] * 48,
        )
        recs = recommender.generate_recommendations(metrics)
        cpu_recs = [r for r in recs if r.type == RecommendationType.RIGHTSIZING_CPU]
        assert len(cpu_recs) == 0

    def test_recommended_value_has_headroom(self, recommender):
        """Recommended CPU should include headroom over P95 usage."""
        # 40 samples at 100, 8 samples at 500 => P95 is 500
        usages = [100] * 40 + [500] * 8
        metrics = _make_timeseries(
            pod_name="oversized",
            cpu_request=5000,
            usages=usages,
        )
        recs = recommender.generate_recommendations(metrics)
        cpu_recs = [r for r in recs if r.type == RecommendationType.RIGHTSIZING_CPU]
        assert len(cpu_recs) == 1
        # P95 = 500, headroom 1.2x = 600
        assert cpu_recs[0].recommended_cpu_request_millicores >= 500
        assert cpu_recs[0].recommended_cpu_request_millicores <= 700

    def test_no_rightsizing_when_no_usage_data(self, recommender):
        """Metrics without cpu_usage_millicores should not generate CPU rightsizing."""
        metrics = [
            _make_metric(
                pod_name="no-usage-data",
                cpu_request=1000,
                cpu_usage_millicores=None,
            )
        ]
        recs = recommender.generate_recommendations(metrics)
        cpu_recs = [r for r in recs if r.type == RecommendationType.RIGHTSIZING_CPU]
        assert len(cpu_recs) == 0


# ---------------------------------------------------------------------------
# Test: RIGHTSIZING_MEMORY
# ---------------------------------------------------------------------------


class TestRightsizingMemory:
    """Tests for memory rightsizing recommendations."""

    def test_detects_oversized_memory(self, recommender):
        """Pod using 10% of memory request should get rightsizing rec."""
        mem_req = 1024 * 1024 * 1024  # 1 GiB
        mem_usage = 100 * 1024 * 1024  # 100 MiB
        metrics = _make_timeseries(
            pod_name="oversized-mem",
            memory_request=mem_req,
            memory_usages=[mem_usage] * 48,
            usages=[500] * 48,  # CPU is fine
        )
        recs = recommender.generate_recommendations(metrics)
        mem_recs = [r for r in recs if r.type == RecommendationType.RIGHTSIZING_MEMORY]
        assert len(mem_recs) == 1
        assert mem_recs[0].pod_name == "oversized-mem"
        assert mem_recs[0].current_memory_request_bytes == mem_req
        assert mem_recs[0].recommended_memory_request_bytes < mem_req

    def test_no_rightsizing_for_well_used_memory(self, recommender):
        """Pod using 80% of memory request should NOT get rightsizing rec."""
        mem_req = 512 * 1024 * 1024
        mem_usage = 400 * 1024 * 1024
        metrics = _make_timeseries(
            pod_name="well-used-mem",
            memory_request=mem_req,
            memory_usages=[mem_usage] * 48,
            usages=[500] * 48,
        )
        recs = recommender.generate_recommendations(metrics)
        mem_recs = [r for r in recs if r.type == RecommendationType.RIGHTSIZING_MEMORY]
        assert len(mem_recs) == 0


# ---------------------------------------------------------------------------
# Test: AUTOSCALING_CANDIDATE
# ---------------------------------------------------------------------------


class TestAutoscalingCandidate:
    """Tests for autoscaling recommendation."""

    def test_detects_spiky_workload(self, recommender):
        """Pod with high usage variance should be flagged for autoscaling."""
        # Create a spiky pattern: low for most, then high spikes
        usages = [100] * 40 + [1800, 1900, 1800, 1900, 100, 100, 100, 100]
        metrics = _make_timeseries(
            pod_name="spiky-pod",
            cpu_request=2000,
            usages=usages,
        )
        recs = recommender.generate_recommendations(metrics)
        auto_recs = [r for r in recs if r.type == RecommendationType.AUTOSCALING_CANDIDATE]
        assert len(auto_recs) == 1
        assert auto_recs[0].pod_name == "spiky-pod"

    def test_no_autoscaling_for_steady_workload(self, recommender):
        """Pod with consistent usage should NOT be flagged for autoscaling."""
        usages = [800] * 48  # Steady
        metrics = _make_timeseries(
            pod_name="steady-pod",
            cpu_request=1000,
            usages=usages,
        )
        recs = recommender.generate_recommendations(metrics)
        auto_recs = [r for r in recs if r.type == RecommendationType.AUTOSCALING_CANDIDATE]
        assert len(auto_recs) == 0

    def test_no_autoscaling_for_single_metric(self, recommender):
        """With only one data point, no pattern detection possible."""
        metrics = [
            _make_metric(
                pod_name="single-point",
                cpu_request=2000,
                cpu_usage_millicores=500,
            )
        ]
        recs = recommender.generate_recommendations(metrics)
        auto_recs = [r for r in recs if r.type == RecommendationType.AUTOSCALING_CANDIDATE]
        assert len(auto_recs) == 0


# ---------------------------------------------------------------------------
# Test: OFF_PEAK_SCALING
# ---------------------------------------------------------------------------


class TestOffPeakScaling:
    """Tests for off-peak scaling recommendations."""

    def test_detects_business_hours_pattern(self, recommender):
        """Pod active 9-17 and idle overnight should get off-peak rec."""
        # Simulate 24 hours, 1 metric per hour
        usages = []
        for h in range(24):
            if 9 <= h < 17:
                usages.append(800)  # Active during business hours
            else:
                usages.append(5)  # Near-idle overnight
        metrics = _make_timeseries(
            pod_name="business-app",
            cpu_request=1000,
            usages=usages,
            interval_minutes=60,  # 1 per hour
            start_hour=0,
        )
        recs = recommender.generate_recommendations(metrics)
        offpeak_recs = [r for r in recs if r.type == RecommendationType.OFF_PEAK_SCALING]
        assert len(offpeak_recs) == 1
        assert offpeak_recs[0].pod_name == "business-app"
        assert offpeak_recs[0].cron_schedule is not None

    def test_no_offpeak_for_always_active(self, recommender):
        """Pod that is always active should NOT get off-peak rec."""
        usages = [700 + (i % 100) for i in range(24)]
        metrics = _make_timeseries(
            pod_name="always-active",
            cpu_request=1000,
            usages=usages,
            interval_minutes=60,
            start_hour=0,
        )
        recs = recommender.generate_recommendations(metrics)
        offpeak_recs = [r for r in recs if r.type == RecommendationType.OFF_PEAK_SCALING]
        assert len(offpeak_recs) == 0

    def test_no_offpeak_for_short_idle(self, recommender):
        """Pod idle for only 2 hours should NOT trigger (min is 4)."""
        usages = [800] * 22 + [5, 5]  # Only 2 hours idle
        metrics = _make_timeseries(
            pod_name="short-idle",
            cpu_request=1000,
            usages=usages,
            interval_minutes=60,
            start_hour=0,
        )
        recs = recommender.generate_recommendations(metrics)
        offpeak_recs = [r for r in recs if r.type == RecommendationType.OFF_PEAK_SCALING]
        assert len(offpeak_recs) == 0


# ---------------------------------------------------------------------------
# Test: IDLE_NAMESPACE
# ---------------------------------------------------------------------------


class TestIdleNamespace:
    """Tests for idle namespace detection."""

    def test_detects_idle_namespace(self, recommender):
        """Namespace with tiny energy and cost should be flagged."""
        metrics = [
            _make_metric(pod_name="pod-a", namespace="idle-ns", joules=100, total_cost=0.05, co2e_grams=0.01),
            _make_metric(pod_name="pod-b", namespace="idle-ns", joules=200, total_cost=0.03, co2e_grams=0.01),
            _make_metric(pod_name="active-pod", namespace="active-ns", joules=500000, total_cost=5.0, co2e_grams=50.0),
        ]
        recs = recommender.generate_recommendations(metrics)
        idle_recs = [r for r in recs if r.type == RecommendationType.IDLE_NAMESPACE]
        assert len(idle_recs) == 1
        assert idle_recs[0].namespace == "idle-ns"

    def test_no_idle_for_active_namespace(self, recommender):
        """Active namespace should not be flagged."""
        metrics = [
            _make_metric(pod_name="pod-a", namespace="active-ns", joules=500000, total_cost=5.0),
        ]
        recs = recommender.generate_recommendations(metrics)
        idle_recs = [r for r in recs if r.type == RecommendationType.IDLE_NAMESPACE]
        assert len(idle_recs) == 0


# ---------------------------------------------------------------------------
# Test: CARBON_AWARE_SCHEDULING
# ---------------------------------------------------------------------------


class TestCarbonAwareScheduling:
    """Tests for carbon-aware scheduling recommendations."""

    def test_detects_high_carbon_workload(self, recommender):
        """Pod running during high-carbon periods should be flagged."""
        # Some pods run during high intensity, others during low
        metrics = [
            _make_metric(
                pod_name="batch-job-1", namespace="batch", grid_intensity=300.0, emaps_zone="DE", timestamp=_ts(hour=14)
            ),
            _make_metric(
                pod_name="batch-job-1", namespace="batch", grid_intensity=280.0, emaps_zone="DE", timestamp=_ts(hour=15)
            ),
            _make_metric(
                pod_name="web-app", namespace="prod", grid_intensity=100.0, emaps_zone="DE", timestamp=_ts(hour=3)
            ),
            _make_metric(
                pod_name="web-app", namespace="prod", grid_intensity=90.0, emaps_zone="DE", timestamp=_ts(hour=4)
            ),
        ]
        recs = recommender.generate_recommendations(metrics)
        carbon_recs = [r for r in recs if r.type == RecommendationType.CARBON_AWARE_SCHEDULING]
        # batch-job runs at high intensity (290 avg) vs zone avg (192.5) -> 290/192.5 = 1.51x > 1.5x
        assert len(carbon_recs) >= 1
        assert any(r.pod_name == "batch-job-1" for r in carbon_recs)

    def test_no_carbon_aware_for_low_intensity(self, recommender):
        """Pod running during low-carbon period should NOT be flagged."""
        metrics = [
            _make_metric(pod_name="green-job", grid_intensity=50.0, emaps_zone="FR"),
            _make_metric(pod_name="green-job", grid_intensity=60.0, emaps_zone="FR"),
        ]
        recs = recommender.generate_recommendations(metrics)
        carbon_recs = [r for r in recs if r.type == RecommendationType.CARBON_AWARE_SCHEDULING]
        assert len(carbon_recs) == 0


# ---------------------------------------------------------------------------
# Test: OVERPROVISIONED_NODE
# ---------------------------------------------------------------------------


class TestOverprovisionedNode:
    """Tests for overprovisioned node detection."""

    def test_detects_overprovisioned_node(self, recommender):
        """Node with very low total pod usage should be flagged."""
        node_infos = [MagicMock(name="big-node", cpu_capacity_cores=16.0)]
        node_infos[0].name = "big-node"
        metrics = [
            _make_metric(pod_name="tiny-pod-1", node="big-node", cpu_usage_millicores=100),
            _make_metric(pod_name="tiny-pod-2", node="big-node", cpu_usage_millicores=200),
        ]
        recs = recommender.generate_recommendations(metrics, node_infos=node_infos)
        node_recs = [r for r in recs if r.type == RecommendationType.OVERPROVISIONED_NODE]
        assert len(node_recs) == 1
        assert node_recs[0].target_node == "big-node"

    def test_no_overprovisioned_for_utilized_node(self, recommender):
        """Node with good utilization should NOT be flagged."""
        node_infos = [MagicMock(name="busy-node", cpu_capacity_cores=4.0)]
        node_infos[0].name = "busy-node"
        metrics = [
            _make_metric(pod_name="pod-1", node="busy-node", cpu_usage_millicores=1500),
            _make_metric(pod_name="pod-2", node="busy-node", cpu_usage_millicores=1500),
        ]
        recs = recommender.generate_recommendations(metrics, node_infos=node_infos)
        node_recs = [r for r in recs if r.type == RecommendationType.OVERPROVISIONED_NODE]
        assert len(node_recs) == 0


# ---------------------------------------------------------------------------
# Test: UNDERUTILIZED_NODE
# ---------------------------------------------------------------------------


class TestUnderutilizedNode:
    """Tests for underutilized node detection (few pods + low usage)."""

    def test_detects_underutilized_node(self, recommender):
        """Node with 1 pod and low usage should be flagged."""
        node_infos = [MagicMock(name="lonely-node", cpu_capacity_cores=8.0)]
        node_infos[0].name = "lonely-node"
        metrics = [
            _make_metric(pod_name="solo-pod", node="lonely-node", cpu_usage_millicores=100),
        ]
        recs = recommender.generate_recommendations(metrics, node_infos=node_infos)
        node_recs = [r for r in recs if r.type == RecommendationType.UNDERUTILIZED_NODE]
        assert len(node_recs) == 1
        assert node_recs[0].target_node == "lonely-node"

    def test_no_underutilized_for_busy_node(self, recommender):
        """Node with many pods should NOT be flagged."""
        node_infos = [MagicMock(name="busy-node", cpu_capacity_cores=8.0)]
        node_infos[0].name = "busy-node"
        metrics = [_make_metric(pod_name=f"pod-{i}", node="busy-node", cpu_usage_millicores=500) for i in range(5)]
        recs = recommender.generate_recommendations(metrics, node_infos=node_infos)
        node_recs = [r for r in recs if r.type == RecommendationType.UNDERUTILIZED_NODE]
        assert len(node_recs) == 0


# ---------------------------------------------------------------------------
# Test: Empty and edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Tests for edge cases and robustness."""

    def test_empty_metrics(self, recommender):
        """Empty metrics should return empty recommendations."""
        recs = recommender.generate_recommendations([])
        assert recs == []

    def test_all_types_can_coexist(self, recommender):
        """Multiple recommendation types should be generated from mixed data."""
        node_infos = [MagicMock(name="big-node", cpu_capacity_cores=32.0)]
        node_infos[0].name = "big-node"

        # Zombie pod
        zombie = _make_metric(
            pod_name="zombie", total_cost=0.5, joules=50, co2e_grams=0.1, cpu_usage_millicores=0, node="big-node"
        )

        # Oversized CPU pod (time-series of low usage)
        oversized_ts = _make_timeseries(
            pod_name="oversized",
            cpu_request=4000,
            usages=[200] * 24,
            node="big-node",
        )

        all_metrics = [zombie] + oversized_ts
        recs = recommender.generate_recommendations(all_metrics, node_infos=node_infos)
        types_found = {r.type for r in recs}
        assert RecommendationType.ZOMBIE_POD in types_found
        assert RecommendationType.RIGHTSIZING_CPU in types_found

    def test_recommendations_have_required_fields(self, recommender):
        """All recommendations should have non-empty required fields."""
        metrics = [
            _make_metric(
                pod_name="zombie",
                total_cost=0.5,
                joules=50.0,
                co2e_grams=0.1,
            )
        ]
        recs = recommender.generate_recommendations(metrics)
        for rec in recs:
            assert rec.pod_name
            assert rec.namespace
            assert rec.type
            assert rec.description
            assert rec.priority in ("high", "medium", "low")

    def test_deduplication(self, recommender):
        """Same pod should not get duplicate recommendations of the same type."""
        metrics = _make_timeseries(
            pod_name="dup-pod",
            cpu_request=4000,
            usages=[200] * 48,
        )
        recs = recommender.generate_recommendations(metrics)
        cpu_recs = [r for r in recs if r.type == RecommendationType.RIGHTSIZING_CPU]
        pod_names = [r.pod_name for r in cpu_recs]
        assert len(pod_names) == len(set(pod_names)), "Duplicate recommendations found"
