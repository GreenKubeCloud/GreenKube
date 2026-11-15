# tests/core/test_native_analyzers.py
"""
Comprehensive tests for the native recommendation analyzers.
Tests cover all 11 recommendation types using TDD methodology.
"""

from datetime import datetime, timedelta, timezone
from typing import List
from unittest.mock import MagicMock

import pytest

from greenkube.collectors.deletion_candidates import NamespaceInfo
from greenkube.collectors.lb_collector import OrphanedLoadBalancer
from greenkube.collectors.pv_collector import OrphanedPV
from greenkube.models.metrics import CombinedMetric, RecommendationType

from .optimization.helpers import NativeRecommender  # pyrefly: ignore[missing-import]

# ---------------------------------------------------------------------------
# Helpers to build test metrics
# ---------------------------------------------------------------------------


#: Deletion candidates need evidence and a first-seen time older than DELETION_MIN_UNUSED_DAYS.
_EVIDENCE = ["selector app=x matches no pod in namespace legacy"]


def _unused_since(days: float = 8.0) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def _lb(**kwargs) -> OrphanedLoadBalancer:
    kwargs.setdefault("evidence", list(_EVIDENCE))
    kwargs.setdefault("first_seen_at", _unused_since())
    return OrphanedLoadBalancer(**kwargs)


def _orphan_pv(**kwargs) -> OrphanedPV:
    kwargs.setdefault("evidence", ["phase Released: its claim default/gone-claim was deleted"])
    kwargs.setdefault("first_seen_at", _unused_since())
    return OrphanedPV(**kwargs)


def _namespaces(*names: str) -> dict:
    created = datetime.now(timezone.utc) - timedelta(days=90)
    return {name: NamespaceInfo(name=name, created_at=created) for name in names}


def _ts(hour: int = 12, day: int = 1, month: int = 1, year: int = 2026) -> datetime:
    """Create a UTC timestamp for testing."""
    return datetime(year, month, day, hour, 0, 0, tzinfo=timezone.utc)


def _make_metric(
    pod_name: str = "test-pod",
    namespace: str = "default",
    cpu_request: int = 1000,
    memory_request: int = 512 * 1024 * 1024,
    cpu_usage_millicores: int | None = 500,
    cpu_usage_max_millicores: int | None = None,
    memory_usage_bytes: int = 256 * 1024 * 1024,
    memory_usage_max_bytes: int | None = None,
    joules: float = 50000.0,
    total_cost: float = 0.10,
    co2e_grams: float = 5.0,
    timestamp: datetime | None = None,
    duration_seconds: int = 300,
    node: str = "node-1",
    grid_intensity: float = 100.0,
    emaps_zone: str = "FR",
    owner_kind: str | None = None,
    owner_name: str | None = None,
    container_name: str | None = None,
) -> CombinedMetric:
    """Create a CombinedMetric for testing."""
    return CombinedMetric(
        restart_count=0,
        pod_name=pod_name,
        container_name=container_name,
        namespace=namespace,
        cpu_request=cpu_request,
        memory_request=memory_request,
        cpu_usage_millicores=cpu_usage_millicores,
        cpu_usage_max_millicores=cpu_usage_max_millicores,
        memory_usage_bytes=memory_usage_bytes,
        memory_usage_max_bytes=memory_usage_max_bytes,
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
    usages: list | None = None,
    memory_usages: list | None = None,
    start_hour: int = 0,
    interval_minutes: int = 5,
    node: str = "node-1",
    grid_intensity: float = 100.0,
    total_cost: float = 0.01,
    co2e_grams: float = 1.0,
    joules: float = 5000.0,
    owner_kind: str | None = None,
    owner_name: str | None = None,
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
    """Native analyzer test helper with default config thresholds."""
    return NativeRecommender()


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
        """Deleting a zombie releases its observed allocated cost and its allocated CO2e share."""
        window = 7 * 24 * 3600
        metrics = [
            _make_metric(
                pod_name="zombie-pod",
                total_cost=1.50,
                joules=50.0,
                co2e_grams=0.5,
            )
        ]
        recs = recommender.generate_recommendations(metrics, analysis_window_seconds=window)
        zombie_recs = [r for r in recs if r.type == RecommendationType.ZOMBIE_POD]
        assert len(zombie_recs) == 1
        rec = zombie_recs[0]
        assert rec.potential_savings_cost == pytest.approx(1.50 * 365 / 7)
        assert rec.impact is not None
        assert rec.impact["kind"] == "workload_deletion"
        assert rec.impact["realization_factor"] == 1.0
        # CO2e is the reservation-allocated idle and embodied share plus the
        # (small) dynamic share, not the usage-based row value.
        breakdown = rec.impact["co2e_breakdown_annual_g"]
        assert breakdown["idle_operational"] > 0
        assert breakdown["embodied"] > 0
        assert rec.potential_savings_co2e_grams == pytest.approx(breakdown["total"])


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

    def test_recommended_value_accounts_for_observed_maximum(self, recommender):
        """A low average with a high observed max should not recommend idle-sized CPU."""
        metrics = [
            _make_metric(
                pod_name="bursty-worker",
                cpu_request=1000,
                cpu_usage_millicores=10,
                cpu_usage_max_millicores=500,
            )
        ]

        recs = recommender.generate_recommendations(metrics)
        cpu_recs = [r for r in recs if r.type == RecommendationType.RIGHTSIZING_CPU]

        assert len(cpu_recs) == 1
        assert cpu_recs[0].recommended_cpu_request_millicores is not None
        assert cpu_recs[0].recommended_cpu_request_millicores > 250
        assert cpu_recs[0].recommended_cpu_request_millicores < 1000

    def test_peaks_above_the_request_recommend_a_cpu_increase(self, recommender):
        """Per-step peaks above the request are a reliability problem (E9): recommend an increase."""
        metrics = [
            _make_metric(
                pod_name="bursty-api",
                container_name="api",
                cpu_request=500,
                cpu_usage_millicores=25,
                cpu_usage_max_millicores=819,
                total_cost=1.0,
                co2e_grams=10.0,
            )
        ]

        recs = recommender.generate_recommendations(metrics)
        cpu_recs = [r for r in recs if r.type == RecommendationType.RIGHTSIZING_CPU]

        assert len(cpu_recs) == 1
        rec = cpu_recs[0]
        assert rec.current_cpu_request_millicores == 500
        assert rec.recommended_cpu_request_millicores is not None
        assert rec.recommended_cpu_request_millicores > 819
        # An increase costs money: the Impact Model projects a negative saving.
        assert rec.potential_savings_cost is not None and rec.potential_savings_cost < 0

    def test_pod_level_usage_never_yields_a_cpu_increase(self, recommender):
        """Usage known only for the whole pod cannot size one container (USER BUG 2)."""
        metrics = [
            _make_metric(pod_name="bursty-api", cpu_request=500, cpu_usage_millicores=25, cpu_usage_max_millicores=819)
        ]

        recs = recommender.generate_recommendations(metrics)

        assert [r for r in recs if r.type == RecommendationType.RIGHTSIZING_CPU] == []

    def test_no_cpu_rightsizing_when_minimum_floor_removes_savings(self, recommender):
        """Minimum CPU floors must not turn an equal request into a savings recommendation."""
        metrics = [
            _make_metric(
                pod_name="tiny-worker",
                cpu_request=recommender.min_cpu_millicores,
                cpu_usage_millicores=1,
                total_cost=1.0,
                co2e_grams=10.0,
            )
        ]

        recs = recommender.generate_recommendations(metrics)
        cpu_recs = [r for r in recs if r.type == RecommendationType.RIGHTSIZING_CPU]

        assert cpu_recs == []

    def test_cpu_rightsizing_uses_latest_request_not_historical_maximum(self, recommender):
        """A workload already reduced during the lookback should not be judged against its old request."""
        metrics = [
            _make_metric(
                pod_name="recently-rightsized",
                cpu_request=500,
                cpu_usage_millicores=70,
                timestamp=_ts(hour=1),
            ),
            _make_metric(
                pod_name="recently-rightsized",
                cpu_request=100,
                cpu_usage_millicores=70,
                timestamp=_ts(hour=2),
            ),
        ]

        recs = recommender.generate_recommendations(metrics)
        cpu_recs = [r for r in recs if r.type == RecommendationType.RIGHTSIZING_CPU]

        assert cpu_recs == []

    def test_cpu_savings_use_floored_recommended_value(self, recommender):
        """Savings estimates should match the final recommendation shown to users."""
        metrics = [
            _make_metric(
                pod_name="floor-aware-worker",
                cpu_request=100,
                cpu_usage_millicores=1,
                total_cost=100.0,
                co2e_grams=100.0,
            )
        ]

        recs = recommender.generate_recommendations(metrics)
        cpu_rec = next(r for r in recs if r.type == RecommendationType.RIGHTSIZING_CPU)

        assert cpu_rec.recommended_cpu_request_millicores == recommender.min_cpu_millicores
        # The released reservation is current - floored target, observed for one
        # 5-minute sample and annualized over that span. It is priced from the
        # row's observed total (100 per 5 minutes), apportioned to CPU and RAM
        # (0.1 core, 0.5 GiB reserved) with the OpenCost default rate weights.
        released_core_hours = (100 - recommender.min_cpu_millicores) / 1000 * 8760
        assert cpu_rec.impact["capacity_released"]["cpu_core_hours"] == pytest.approx(released_core_hours)
        cpu_share = 0.1 * 0.031611 / (0.1 * 0.031611 + 0.5 * 0.004237)
        released_ratio = (100 - recommender.min_cpu_millicores) / 100
        assert cpu_rec.potential_savings_cost == pytest.approx(100.0 * 12 * 8760 * cpu_share * released_ratio)
        assert cpu_rec.impact["cost_breakdown_annual"]["ram"] == 0.0

    def test_deployment_pods_are_grouped_as_one_workload(self, recommender):
        """Pod restarts for the same Deployment should produce one stable workload recommendation."""
        metrics = [
            _make_metric(
                pod_name="api-7f9c5d9f6d-a1b2c",
                cpu_request=2000,
                cpu_usage_millicores=100,
                owner_kind="Deployment",
                owner_name="api",
            ),
            _make_metric(
                pod_name="api-7f9c5d9f6d-d4e5f",
                cpu_request=2000,
                cpu_usage_millicores=120,
                owner_kind="Deployment",
                owner_name="api",
            ),
        ]

        recs = recommender.generate_recommendations(metrics)
        cpu_recs = [r for r in recs if r.type == RecommendationType.RIGHTSIZING_CPU]

        assert len(cpu_recs) == 1
        assert cpu_recs[0].scope == "workload"
        assert cpu_recs[0].pod_name == "api"
        assert "Deployment 'api'" in cpu_recs[0].description

    def test_deployment_like_pods_without_owner_metadata_are_grouped(self, recommender):
        """Historical rows missing owner fields should still target the stable Deployment."""
        metrics = [
            _make_metric(
                pod_name="api-7f9c5d9f6d-a1b2c",
                cpu_request=2000,
                cpu_usage_millicores=100,
            ),
            _make_metric(
                pod_name="api-6f89d4fc7c-d4e5f",
                cpu_request=2000,
                cpu_usage_millicores=120,
            ),
        ]

        recs = recommender.generate_recommendations(metrics)
        cpu_recs = [r for r in recs if r.type == RecommendationType.RIGHTSIZING_CPU]

        assert len(cpu_recs) == 1
        assert cpu_recs[0].scope == "workload"
        assert cpu_recs[0].pod_name == "api"
        assert "Deployment 'api'" in cpu_recs[0].description


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

    def test_recommended_value_accounts_for_observed_maximum(self, recommender):
        """A low average with a high observed max should not recommend idle-sized memory."""
        mib = 1024 * 1024
        metrics = [
            _make_metric(
                pod_name="bursty-cache",
                memory_request=1000 * mib,
                memory_usage_bytes=10 * mib,
                memory_usage_max_bytes=500 * mib,
                cpu_usage_millicores=500,
            )
        ]

        recs = recommender.generate_recommendations(metrics)
        mem_recs = [r for r in recs if r.type == RecommendationType.RIGHTSIZING_MEMORY]

        assert len(mem_recs) == 1
        assert mem_recs[0].recommended_memory_request_bytes is not None
        assert mem_recs[0].recommended_memory_request_bytes > 250 * mib
        assert mem_recs[0].recommended_memory_request_bytes < 1000 * mib

    def test_peaks_above_the_request_recommend_a_memory_increase(self, recommender):
        """A working-set peak above the request is a reliability problem (E9): recommend an increase."""
        mib = 1024 * 1024
        metrics = [
            _make_metric(
                pod_name="bursty-cache",
                container_name="cache",
                memory_request=500 * mib,
                memory_usage_bytes=25 * mib,
                memory_usage_max_bytes=819 * mib,
                cpu_usage_millicores=500,
                total_cost=1.0,
                co2e_grams=10.0,
            )
        ]

        recs = recommender.generate_recommendations(metrics)
        mem_recs = [r for r in recs if r.type == RecommendationType.RIGHTSIZING_MEMORY]

        assert len(mem_recs) == 1
        rec = mem_recs[0]
        assert rec.recommended_memory_request_bytes is not None
        assert rec.recommended_memory_request_bytes >= 819 * mib * 1.1
        assert rec.priority == "high"
        assert rec.potential_savings_cost is not None and rec.potential_savings_cost < 0

    def test_pod_level_usage_never_yields_a_memory_increase(self, recommender):
        """Usage known only for the whole pod cannot size one container (USER BUG 2)."""
        mib = 1024 * 1024
        metrics = [
            _make_metric(
                pod_name="bursty-cache",
                memory_request=500 * mib,
                memory_usage_bytes=25 * mib,
                memory_usage_max_bytes=819 * mib,
            )
        ]

        recs = recommender.generate_recommendations(metrics)

        assert [r for r in recs if r.type == RecommendationType.RIGHTSIZING_MEMORY] == []

    def test_memory_rightsizing_uses_latest_request_not_historical_maximum(self, recommender):
        """A workload already reduced during the lookback should use its current memory request."""
        mib = 1024 * 1024
        metrics = [
            _make_metric(
                pod_name="recently-rightsized-cache",
                memory_request=500 * mib,
                memory_usage_bytes=70 * mib,
                cpu_usage_millicores=500,
                timestamp=_ts(hour=1),
            ),
            _make_metric(
                pod_name="recently-rightsized-cache",
                memory_request=100 * mib,
                memory_usage_bytes=70 * mib,
                cpu_usage_millicores=500,
                timestamp=_ts(hour=2),
            ),
        ]

        recs = recommender.generate_recommendations(metrics)
        mem_recs = [r for r in recs if r.type == RecommendationType.RIGHTSIZING_MEMORY]

        assert mem_recs == []

    def test_memory_savings_use_floored_recommended_value(self, recommender):
        """Memory savings estimates should match the final floored recommendation."""
        mib = 1024 * 1024
        metrics = [
            _make_metric(
                pod_name="floor-aware-cache",
                memory_request=32 * mib,
                memory_usage_bytes=1 * mib,
                cpu_usage_millicores=500,
                total_cost=100.0,
                co2e_grams=100.0,
            )
        ]

        recs = recommender.generate_recommendations(metrics)
        mem_rec = next(r for r in recs if r.type == RecommendationType.RIGHTSIZING_MEMORY)

        assert mem_rec.recommended_memory_request_bytes == recommender.min_memory_bytes
        released_gib_hours = (32 * mib - recommender.min_memory_bytes) / 1024**3 * 8760
        assert mem_rec.impact["capacity_released"]["mem_gib_hours"] == pytest.approx(released_gib_hours)
        # RAM share of the observed total (1 core and 32 MiB reserved).
        ram_share = (32 / 1024 * 0.004237) / (1.0 * 0.031611 + 32 / 1024 * 0.004237)
        released_ratio = (32 * mib - recommender.min_memory_bytes) / (32 * mib)
        assert mem_rec.potential_savings_cost == pytest.approx(100.0 * 12 * 8760 * ram_share * released_ratio)
        assert mem_rec.impact["cost_breakdown_annual"]["cpu"] == 0.0
        # Memory power is allocated by memory reservation (roadmap B5): a memory
        # cut releases memory power only, never idle CPU power or embodied.
        co2e = mem_rec.impact["co2e_breakdown_annual_g"]
        assert co2e["memory_operational"] > 0
        assert co2e["idle_operational"] == co2e["embodied"] == 0.0
        assert mem_rec.potential_savings_co2e_grams == pytest.approx(co2e["memory_operational"])

    def test_memory_savings_are_annualized_from_analysis_window(self, recommender):
        """Potential memory savings should be annual projections, not only lookback-window totals."""
        mib = 1024 * 1024
        analysis_window_seconds = 7 * 24 * 60 * 60
        metrics = [
            _make_metric(
                pod_name="otel-collector-opentelemetry-collector",
                namespace="otel",
                memory_request=256 * mib,
                memory_usage_bytes=40 * mib,
                memory_usage_max_bytes=40 * mib,
                cpu_usage_millicores=500,
                total_cost=0.001,
                co2e_grams=1.0,
                timestamp=_ts(day=1, hour=1),
                duration_seconds=300,
            ),
            _make_metric(
                pod_name="otel-collector-opentelemetry-collector",
                namespace="otel",
                memory_request=256 * mib,
                memory_usage_bytes=40 * mib,
                memory_usage_max_bytes=40 * mib,
                cpu_usage_millicores=500,
                total_cost=0.001,
                co2e_grams=1.0,
                timestamp=_ts(day=1, hour=2),
                duration_seconds=300,
            ),
        ]

        recs = recommender.generate_recommendations(metrics, analysis_window_seconds=analysis_window_seconds)
        mem_rec = next(r for r in recs if r.type == RecommendationType.RIGHTSIZING_MEMORY)
        released_gib = (mem_rec.current_memory_request_bytes - mem_rec.recommended_memory_request_bytes) / 1024**3
        annualization_factor = (365 * 24 * 60 * 60) / analysis_window_seconds
        observed_hours = 2 * 300 / 3600

        expected_gib_hours = released_gib * observed_hours * annualization_factor
        assert mem_rec.impact["capacity_released"]["mem_gib_hours"] == pytest.approx(expected_gib_hours)
        # The RAM share of the observed total (1 core and 0.25 GiB reserved),
        # times the released fraction of the request, annualized.
        ram_share = (0.25 * 0.004237) / (1.0 * 0.031611 + 0.25 * 0.004237)
        released_ratio = released_gib / 0.25
        assert mem_rec.potential_savings_cost == pytest.approx(
            2 * 0.001 * ram_share * released_ratio * annualization_factor
        )
        # Memory power released by the cut (B5), annualized like the cost.
        co2e = mem_rec.impact["co2e_breakdown_annual_g"]
        assert mem_rec.potential_savings_co2e_grams == pytest.approx(co2e["memory_operational"])
        assert co2e["memory_operational"] > 0


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
            owner_kind="Deployment",
            owner_name="business-app",
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
        recs = recommender.generate_recommendations(metrics, namespace_info=_namespaces("idle-ns", "active-ns"))
        idle_recs = [r for r in recs if r.type == RecommendationType.IDLE_NAMESPACE]
        assert len(idle_recs) == 1
        assert idle_recs[0].namespace == "idle-ns"
        assert "appears idle" in idle_recs[0].description
        assert "Evidence that it is unused" in idle_recs[0].reason

    def test_no_idle_namespace_when_namespaces_are_unreadable(self, recommender):
        """Fail closed: without namespace metadata (opt-outs, age) nothing is recommended."""
        metrics = [_make_metric(pod_name="pod-a", namespace="idle-ns", joules=100, total_cost=0.05)]
        recs = recommender.generate_recommendations(metrics)
        assert [r for r in recs if r.type == RecommendationType.IDLE_NAMESPACE] == []

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

    def test_multi_pod_utilization_is_summed_not_averaged(self, recommender):
        """Multiple pods on the same node must be summed per timestamp, not averaged.

        Regression test: 3 pods each using ~160-244m on a 4-core node
        yields ~565m total = ~14% utilization, which is below the 20% threshold.
        Previously, the per-pod average (188m / 4000m = 4.7%) was used instead,
        reporting 0% utilization in the recommendation reason.
        """
        node_infos = [MagicMock(name="node-3a7e1d", cpu_capacity_cores=4.0)]
        node_infos[0].name = "node-3a7e1d"
        # Simulate k8s metrics: 3 pods on the same node at the same timestamp
        t = _ts()
        metrics = [
            _make_metric(pod_name="pod-a", node="node-3a7e1d", cpu_usage_millicores=160, timestamp=t),
            _make_metric(pod_name="pod-b", node="node-3a7e1d", cpu_usage_millicores=161, timestamp=t),
            _make_metric(pod_name="pod-c", node="node-3a7e1d", cpu_usage_millicores=244, timestamp=t),
        ]
        recs = recommender.generate_recommendations(metrics, node_infos=node_infos)
        node_recs = [r for r in recs if r.type == RecommendationType.OVERPROVISIONED_NODE]
        # Total = 565m / 4000m = ~14% — still below threshold, recommendation is expected
        assert len(node_recs) == 1
        # The reported utilization must NOT be 0% — it must reflect the summed total (~14%)
        # Use a word-boundary check: "(0%)" is what the old bug produced
        assert "(0%)" not in node_recs[0].reason
        assert "14%" in node_recs[0].reason

    def test_no_overprovisioned_when_memory_is_high(self, recommender):
        """Node with low CPU but high memory usage must NOT be flagged as overprovisioned."""
        node_infos = [MagicMock()]
        node_infos[0].name = "mem-heavy-node"
        node_infos[0].cpu_capacity_cores = 16.0
        node_infos[0].memory_capacity_bytes = 64 * 1024**3  # 64 GiB
        # Low CPU: 300m / 16000m = ~2%
        # High memory: 54 GiB / 64 GiB = ~84%
        metrics = [
            _make_metric(
                pod_name="mem-pod",
                node="mem-heavy-node",
                cpu_usage_millicores=300,
                memory_usage_bytes=54 * 1024**3,
            ),
        ]
        recs = recommender.generate_recommendations(metrics, node_infos=node_infos)
        node_recs = [r for r in recs if r.type == RecommendationType.OVERPROVISIONED_NODE]
        assert len(node_recs) == 0, "Node with high memory usage must not be flagged as overprovisioned"

    def test_overprovisioned_when_both_cpu_and_memory_are_low(self, recommender):
        """Node with low CPU AND low memory must be flagged, with both metrics in the description."""
        node_infos = [MagicMock()]
        node_infos[0].name = "idle-node"
        node_infos[0].cpu_capacity_cores = 16.0
        node_infos[0].memory_capacity_bytes = 64 * 1024**3  # 64 GiB
        # Low CPU: 200m / 16000m = ~1%
        # Low memory: 2 GiB / 64 GiB = ~3%
        metrics = [
            _make_metric(
                pod_name="tiny-pod",
                node="idle-node",
                cpu_usage_millicores=200,
                memory_usage_bytes=2 * 1024**3,
            ),
        ]
        recs = recommender.generate_recommendations(metrics, node_infos=node_infos)
        node_recs = [r for r in recs if r.type == RecommendationType.OVERPROVISIONED_NODE]
        assert len(node_recs) == 1
        assert node_recs[0].target_node == "idle-node"
        assert "memory" in node_recs[0].description.lower(), "Description must mention memory utilization"


# ---------------------------------------------------------------------------
# Test: UNDERUTILIZED_NODE
# ---------------------------------------------------------------------------


class TestUnderutilizedNode:
    """UNDERUTILIZED_NODE is no longer emitted (E9): node advice comes from the overprovisioned check."""

    def test_lonely_low_usage_node_is_reported_once_as_overprovisioned(self, recommender):
        """A node with one idle pod gets the overprovisioned advice, never the duplicate type."""
        node_infos = [MagicMock(name="lonely-node", cpu_capacity_cores=8.0)]
        node_infos[0].name = "lonely-node"
        metrics = [
            _make_metric(pod_name="solo-pod", node="lonely-node", cpu_usage_millicores=100),
        ]
        recs = recommender.generate_recommendations(metrics, node_infos=node_infos)
        assert [r for r in recs if r.type == RecommendationType.UNDERUTILIZED_NODE] == []
        node_recs = [r for r in recs if r.type == RecommendationType.OVERPROVISIONED_NODE]
        assert [r.target_node for r in node_recs] == ["lonely-node"]

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
            # pod_name and namespace may be None for node/namespace-level recs
            if rec.scope == "pod":
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


# ---------------------------------------------------------------------------
# Test: Missing usage data edge cases
# ---------------------------------------------------------------------------


class TestMissingUsageData:
    """Tests for graceful handling of missing usage data in rightsizing recommendations."""

    def test_no_memory_rightsizing_when_memory_usage_bytes_is_none(self, recommender):
        """Pods with memory_usage_bytes=None in all metrics should not trigger memory rightsizing."""
        metrics = _make_timeseries(
            pod_name="no-mem-data",
            memory_request=1024 * 1024 * 1024,  # 1 GiB request
            # memory_usages defaults to 256 MiB — override with None via direct construction
        )
        # Override all metrics so memory_usage_bytes is None
        for m in metrics:
            object.__setattr__(m, "memory_usage_bytes", None)

        recs = recommender.generate_recommendations(metrics)
        mem_recs = [r for r in recs if r.type == RecommendationType.RIGHTSIZING_MEMORY]
        assert len(mem_recs) == 0, "Should not recommend memory rightsizing with no usage data"

    def test_no_cpu_rightsizing_when_all_usage_none(self, recommender):
        """Pods with cpu_usage_millicores=None in all metrics should not trigger CPU rightsizing."""
        metrics = _make_timeseries(
            pod_name="no-cpu-data",
            cpu_request=2000,
        )
        for m in metrics:
            object.__setattr__(m, "cpu_usage_millicores", None)

        recs = recommender.generate_recommendations(metrics)
        cpu_recs = [r for r in recs if r.type == RecommendationType.RIGHTSIZING_CPU]
        assert len(cpu_recs) == 0, "Should not recommend CPU rightsizing with no usage data"

    def test_memory_rightsizing_uses_available_data_when_some_none(self, recommender):
        """Partially missing memory usage data should still produce a recommendation based on available points."""
        mem_req = 1024 * 1024 * 1024  # 1 GiB
        low_usage = 100 * 1024 * 1024  # 100 MiB
        # Half the metrics have data, half have None
        metrics = _make_timeseries(
            pod_name="partial-mem",
            memory_request=mem_req,
            memory_usages=[low_usage] * 48,
            usages=[500] * 48,
        )
        # Set half to None
        for m in metrics[24:]:
            object.__setattr__(m, "memory_usage_bytes", None)

        recs = recommender.generate_recommendations(metrics)
        mem_recs = [r for r in recs if r.type == RecommendationType.RIGHTSIZING_MEMORY]
        # Should still trigger — 24 valid data points are enough
        assert len(mem_recs) == 1


# ---------------------------------------------------------------------------
# Test: System namespace exclusion
# ---------------------------------------------------------------------------


class TestSystemNamespaceExclusion:
    """Tests for system namespace exclusion in idle namespace detection."""

    def test_kube_system_excluded_from_idle_detection_by_default(self, recommender):
        """kube-system namespace must not be flagged as idle when recommend_system_namespaces=False (default)."""
        assert not recommender.recommend_system_namespaces, "Default config must exclude system namespaces"

        metrics = [
            _make_metric(
                pod_name="coredns",
                namespace="kube-system",
                joules=50,  # Very low — would normally trigger idle
                total_cost=0.01,
                co2e_grams=0.01,
            )
        ]
        recs = recommender.generate_recommendations(metrics)
        idle_recs = [r for r in recs if r.type == RecommendationType.IDLE_NAMESPACE]
        assert len(idle_recs) == 0, "kube-system should be excluded from idle namespace detection"

    def test_all_system_namespaces_excluded_by_default(self, recommender):
        """All well-known system namespaces must not be flagged as idle by default."""
        system_namespaces = [
            "kube-system",
            "kube-public",
            "kube-node-lease",
            "coredns",
            "istio-system",
            "kubernetes-dashboard",
        ]
        metrics = [
            _make_metric(
                pod_name=f"pod-{ns}",
                namespace=ns,
                joules=50,
                total_cost=0.01,
            )
            for ns in system_namespaces
        ]
        recs = recommender.generate_recommendations(metrics)
        idle_recs = [r for r in recs if r.type == RecommendationType.IDLE_NAMESPACE]
        flagged_ns = {r.namespace for r in idle_recs}
        overlap = flagged_ns & set(system_namespaces)
        assert len(overlap) == 0, f"System namespaces should be excluded, but got: {overlap}"

    def test_custom_namespace_still_flagged_as_idle(self, recommender):
        """Non-system namespaces are still flagged as idle even when system exclusion is active."""
        metrics = [
            _make_metric(
                pod_name="pod-a",
                namespace="my-custom-ns",
                joules=50,
                total_cost=0.01,
            )
        ]
        recs = recommender.generate_recommendations(metrics, namespace_info=_namespaces("my-custom-ns"))
        idle_recs = [r for r in recs if r.type == RecommendationType.IDLE_NAMESPACE]
        assert len(idle_recs) == 1
        assert idle_recs[0].namespace == "my-custom-ns"

    def test_system_namespace_never_decommissioned_even_when_flag_enabled(self):
        """RECOMMEND_SYSTEM_NAMESPACES never makes a system namespace a deletion candidate."""
        from greenkube.core.config import Config

        cfg = Config()
        cfg.RECOMMEND_SYSTEM_NAMESPACES = True
        recommender_with_sys = NativeRecommender(config=cfg)

        metrics = [
            _make_metric(
                pod_name="coredns",
                namespace="kube-system",
                joules=50,
                total_cost=0.05,
            )
        ]
        recs = recommender_with_sys.generate_recommendations(metrics, namespace_info=_namespaces("kube-system"))
        idle_recs = [r for r in recs if r.type == RecommendationType.IDLE_NAMESPACE]
        assert idle_recs == []


# ---------------------------------------------------------------------------
# Test: Node overprovisioning - memory capacity edge cases
# ---------------------------------------------------------------------------


class TestNodeMemoryCapacityEdgeCases:
    """Tests for node overprovisioning when memory capacity data is missing or ambiguous."""

    def test_no_overprovisioned_when_only_memory_capacity_missing(self, recommender):
        """When memory_capacity_bytes is not set on node_infos, overprovisioning check falls back to CPU only."""
        node_infos = [MagicMock()]
        node_infos[0].name = "cpu-only-node"
        node_infos[0].cpu_capacity_cores = 16.0
        # memory_capacity_bytes is an auto-created MagicMock attribute (not int/float)
        # → the code's isinstance(..., (int, float)) check should exclude it

        metrics = [
            # Very low CPU: should trigger overprovisioning (no memory data to save it)
            _make_metric(pod_name="pod-a", node="cpu-only-node", cpu_usage_millicores=50),
        ]
        recs = recommender.generate_recommendations(metrics, node_infos=node_infos)
        node_recs = [r for r in recs if r.type == RecommendationType.OVERPROVISIONED_NODE]
        # Without memory capacity, the check is CPU-only → should still flag if CPU is very low
        assert len(node_recs) == 1

    def test_overprovisioned_description_omits_memory_when_no_capacity(self, recommender):
        """When memory capacity is unknown, the description must NOT mention memory utilization."""
        node_infos = [MagicMock()]
        node_infos[0].name = "no-mem-cap-node"
        node_infos[0].cpu_capacity_cores = 16.0
        # MagicMock auto-creates memory_capacity_bytes as a Mock — not int → excluded

        metrics = [
            _make_metric(pod_name="pod-a", node="no-mem-cap-node", cpu_usage_millicores=50),
        ]
        recs = recommender.generate_recommendations(metrics, node_infos=node_infos)
        node_recs = [r for r in recs if r.type == RecommendationType.OVERPROVISIONED_NODE]
        assert len(node_recs) == 1
        # The description should NOT mention GiB memory figures since capacity is unknown
        assert "GiB" not in node_recs[0].description

    def test_overprovisioned_description_includes_memory_when_both_low(self, recommender):
        """When both CPU and memory are low and memory capacity is known, description mentions memory."""
        node_infos = [MagicMock()]
        node_infos[0].name = "low-both-node"
        node_infos[0].cpu_capacity_cores = 16.0
        node_infos[0].memory_capacity_bytes = 64 * 1024**3  # 64 GiB

        metrics = [
            _make_metric(
                pod_name="pod-a",
                node="low-both-node",
                cpu_usage_millicores=100,
                memory_usage_bytes=1 * 1024**3,  # 1 GiB / 64 GiB = ~1.6%
            ),
        ]
        recs = recommender.generate_recommendations(metrics, node_infos=node_infos)
        node_recs = [r for r in recs if r.type == RecommendationType.OVERPROVISIONED_NODE]
        assert len(node_recs) == 1
        assert "memory" in node_recs[0].description.lower()


# ---------------------------------------------------------------------------
# Test: Underutilized node reason field
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Test: ORPHANED_PERSISTENT_VOLUME
# ---------------------------------------------------------------------------


class TestOrphanedPersistentVolume:
    """Tests for orphaned PersistentVolume detection."""

    def _pv_recs(self, recommender, volumes):
        metrics = [_make_metric(pod_name="unrelated-pod")]
        return [
            r
            for r in recommender.generate_recommendations(metrics, persistent_volumes=volumes)
            if r.type == RecommendationType.ORPHANED_PERSISTENT_VOLUME
        ]

    def test_detects_released_volume(self, recommender):
        """A released PV with a deleted claim should be flagged with estimated cost savings."""
        volumes = [
            _orphan_pv(
                name="pvc-dead",
                phase="Released",
                capacity_bytes=100 * 1024**3,
                claim_namespace="default",
                claim_name="gone-claim",
                reclaim_policy="Retain",
            )
        ]
        recs = self._pv_recs(recommender, volumes)
        assert len(recs) == 1
        rec = recs[0]
        assert rec.pod_name == "pvc-dead"
        assert rec.namespace is None
        assert rec.scope == "cluster"
        assert rec.priority == "medium"
        assert "100.0GiB" in rec.description
        assert "appears unused" in rec.description
        assert "Released" in rec.reason and "Evidence that it is unused" in rec.reason
        # 100 GiB at $0.10/GiB-month → $120/year in annualized cost savings
        assert rec.potential_savings_cost == pytest.approx(100 * 0.10 * 12)
        assert "120.00" in rec.description
        # CO2e savings are not projected: energy estimation only covers CPU usage today
        assert rec.potential_savings_co2e_grams is None

    def test_no_cost_savings_when_capacity_unknown(self, recommender):
        """A PV with unknown capacity should not report fabricated cost savings."""
        volumes = [
            _orphan_pv(
                name="pvc-no-cap",
                phase="Released",
                capacity_bytes=0,
                claim_namespace="default",
                claim_name="gone-claim",
            )
        ]
        recs = self._pv_recs(recommender, volumes)
        assert len(recs) == 1
        assert recs[0].potential_savings_cost is None

    def test_cost_savings_use_configured_storage_price(self):
        """The storage price per GiB-month must be configurable."""
        from greenkube.core.config import Config

        cfg = Config()
        cfg.STORAGE_COST_PER_GIB_MONTH = 0.25
        custom_recommender = NativeRecommender(config=cfg)

        volumes = [
            _orphan_pv(
                name="pvc-pricey",
                phase="Released",
                capacity_bytes=10 * 1024**3,
                claim_namespace="default",
                claim_name="gone-claim",
            )
        ]
        metrics = [_make_metric(pod_name="unrelated-pod")]
        recs = [
            r
            for r in custom_recommender.generate_recommendations(metrics, persistent_volumes=volumes)
            if r.type == RecommendationType.ORPHANED_PERSISTENT_VOLUME
        ]
        # 10 GiB at $0.25/GiB-month → $30/year
        assert len(recs) == 1
        assert recs[0].potential_savings_cost == pytest.approx(10 * 0.25 * 12)

    def test_prefers_real_opencost_cost_when_available(self, recommender):
        """A real OpenCost annual cost must take precedence over the capacity estimate."""
        volumes = [
            _orphan_pv(
                name="pvc-real",
                phase="Released",
                capacity_bytes=100 * 1024**3,
                claim_namespace="default",
                claim_name="gone-claim",
                annual_cost=87.5,
            )
        ]
        recs = self._pv_recs(recommender, volumes)
        assert len(recs) == 1
        assert recs[0].potential_savings_cost == pytest.approx(87.5)
        assert "OpenCost-reported cost savings: $87.50/year" in recs[0].description
        assert "$0.10/GiB-month" not in recs[0].description

    def test_reason_lists_the_collector_evidence(self, recommender):
        """The recommendation shows the evidence gathered by the collector and the unused duration."""
        volumes = [
            _orphan_pv(
                name="pvc-stale",
                phase="Released",
                capacity_bytes=50 * 1024**3,
                claim_namespace="prod",
                claim_name="deleted-pvc",
                evidence=["no PVC named prod/deleted-pvc exists"],
            )
        ]
        recs = self._pv_recs(recommender, volumes)
        assert len(recs) == 1
        assert "no PVC named prod/deleted-pvc exists" in recs[0].reason
        assert "unused continuously since" in recs[0].reason

    def test_multiple_volumes_produce_separate_recommendations(self, recommender):
        """Each orphaned PV should produce its own recommendation."""
        volumes = [
            _orphan_pv(name="pvc-a", phase="Released", capacity_bytes=10 * 1024**3),
            _orphan_pv(name="pvc-b", phase="Released", capacity_bytes=20 * 1024**3),
        ]
        recs = self._pv_recs(recommender, volumes)
        assert len(recs) == 2
        assert {r.pod_name for r in recs} == {"pvc-a", "pvc-b"}

    def test_no_recommendations_without_volumes(self, recommender):
        """No PV input should produce no PV recommendations."""
        assert self._pv_recs(recommender, None) == []
        assert self._pv_recs(recommender, []) == []

    def test_accepts_mock_objects(self, recommender):
        """The analyzer should tolerate duck-typed descriptors (e.g. mocks)."""
        mock_pv = MagicMock()
        mock_pv.name = "pvc-mocked"
        mock_pv.phase = "Released"
        mock_pv.capacity_bytes = 30 * 1024**3
        mock_pv.claim_namespace = "default"
        mock_pv.claim_name = "gone"
        mock_pv.reclaim_policy = "Retain"
        mock_pv.evidence = ["phase Released"]
        mock_pv.first_seen_at = _unused_since()
        recs = self._pv_recs(recommender, [mock_pv])
        assert len(recs) == 1
        assert recs[0].pod_name == "pvc-mocked"


# ---------------------------------------------------------------------------
# Test: ORPHANED_LOAD_BALANCER
# ---------------------------------------------------------------------------


class TestOrphanedLoadBalancer:
    """Tests for orphaned LoadBalancer detection."""

    def _lb_recs(self, recommender, load_balancers):
        metrics = [_make_metric(pod_name="unrelated-pod")]
        return [
            r
            for r in recommender.generate_recommendations(metrics, load_balancers=load_balancers)
            if r.type == RecommendationType.ORPHANED_LOAD_BALANCER
        ]

    def test_detects_orphaned_load_balancer(self, recommender):
        """A LoadBalancer Service with no endpoints should be flagged with estimated cost savings."""
        services = [
            _lb(
                name="dead-lb",
                namespace="legacy",
                endpoint_count=0,
                external_ip="1.2.3.4",
            )
        ]
        recs = self._lb_recs(recommender, services)
        assert len(recs) == 1
        rec = recs[0]
        assert rec.pod_name == "dead-lb"
        assert rec.namespace == "legacy"
        assert rec.scope == "cluster"
        assert rec.priority == "medium"
        assert "appears unused" in rec.description
        assert "1.2.3.4" in rec.description
        assert "selector app=x matches no pod" in rec.reason
        assert 'greenkube.io/ignore="true"' in rec.reason
        # Flat estimate at $18.00/month → $216/year
        assert rec.potential_savings_cost == pytest.approx(216.0)
        assert "216.00" in rec.description
        # CO2e savings are not projected: energy estimation only covers CPU usage today
        assert rec.potential_savings_co2e_grams is None

    def test_no_recommendations_without_load_balancers(self, recommender):
        """No LoadBalancer input should produce no LoadBalancer recommendations."""
        assert self._lb_recs(recommender, None) == []
        assert self._lb_recs(recommender, []) == []

    def test_prefers_real_opencost_cost_when_available(self, recommender):
        """A real OpenCost annual cost must take precedence over the flat estimate."""
        services = [
            _lb(
                name="lb-real",
                namespace="default",
                endpoint_count=0,
                annual_cost=97.3,
            )
        ]
        recs = self._lb_recs(recommender, services)
        assert len(recs) == 1
        assert recs[0].potential_savings_cost == pytest.approx(97.3)
        assert "OpenCost-reported cost savings: $97.30/year" in recs[0].description
        assert "$18.00/month" not in recs[0].description

    def test_cost_savings_use_configured_lb_price(self):
        """The LoadBalancer price per month must be configurable."""
        from greenkube.core.config import Config

        cfg = Config()
        cfg.LOAD_BALANCER_COST_PER_MONTH = 25.0
        custom_recommender = NativeRecommender(config=cfg)

        services = [_lb(name="lb-pricey", namespace="default", endpoint_count=0, external_ip="203.0.113.7")]
        metrics = [_make_metric(pod_name="unrelated-pod")]
        recs = [
            r
            for r in custom_recommender.generate_recommendations(metrics, load_balancers=services)
            if r.type == RecommendationType.ORPHANED_LOAD_BALANCER
        ]
        # $25/month → $300/year
        assert len(recs) == 1
        assert recs[0].potential_savings_cost == pytest.approx(300.0)

    def test_multiple_services_produce_separate_recommendations(self, recommender):
        """Each orphaned LoadBalancer should produce its own recommendation."""
        services = [
            _lb(name="lb-a", namespace="default", endpoint_count=0),
            _lb(name="lb-b", namespace="default", endpoint_count=0),
        ]
        recs = self._lb_recs(recommender, services)
        assert len(recs) == 2
        assert {r.pod_name for r in recs} == {"lb-a", "lb-b"}

    def test_accepts_mock_objects(self, recommender):
        """The analyzer should tolerate duck-typed descriptors (e.g. mocks)."""
        mock_lb = MagicMock()
        mock_lb.name = "lb-mocked"
        mock_lb.namespace = "default"
        mock_lb.endpoint_count = 0
        mock_lb.external_ip = ""
        mock_lb.ports = "80/TCP"
        mock_lb.evidence = list(_EVIDENCE)
        mock_lb.first_seen_at = _unused_since()
        recs = self._lb_recs(recommender, [mock_lb])
        assert len(recs) == 1
        assert recs[0].pod_name == "lb-mocked"


@pytest.fixture(autouse=True)
def _no_decrease_coverage_gate(monkeypatch):
    """These fixtures span hours, not weeks: disable the one-week coverage gate for
    request decreases (E9), which tests/core/optimization/test_two_way_rightsizing.py covers."""
    monkeypatch.setenv("RECOMMENDATION_MIN_DECREASE_COVERAGE_DAYS", "0")
    # Likewise for the observed-idle span of zombie and idle-namespace deletions, covered by
    # tests/core/optimization/test_deletion_safety.py (persisted LB/PV ages still apply).
    monkeypatch.setenv("DELETION_MIN_UNUSED_DAYS", "0")
