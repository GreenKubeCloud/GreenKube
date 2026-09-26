# tests/core/optimization/test_vpa_provider.py
"""Tests for the VPA recommendation source."""

from datetime import datetime, timezone

from greenkube.collectors.vpa_collector import VPARecommendation
from greenkube.core.config import Config
from greenkube.core.optimization.context import OptimizationContext
from greenkube.core.optimization.providers.vpa import VpaSource
from greenkube.models.metrics import CombinedMetric, RecommendationSource, RecommendationType


class FakeCollector:
    def __init__(self, recs):
        self._recs = recs

    async def collect(self):
        return list(self._recs)


def _metric(cpu_request=1000, memory_request=1024**3, total_cost=1.0, co2e=10.0):
    return CombinedMetric(
        pod_name="api-7d9f8b6c5-x2k4p",
        namespace="prod",
        owner_kind="Deployment",
        owner_name="api",
        cpu_request=cpu_request,
        memory_request=memory_request,
        total_cost=total_cost,
        co2e_grams=co2e,
        timestamp=datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc),
    )


def _vpa(cpu_target=300, memory_target=512 * 1024**2):
    return VPARecommendation(
        namespace="prod",
        vpa_name="api-vpa",
        target_kind="Deployment",
        target_name="api",
        container_name="api",
        cpu_target_millicores=cpu_target,
        memory_target_bytes=memory_target,
    )


def _context(metrics):
    return OptimizationContext(config=Config(), metrics=metrics, analysis_window_seconds=7 * 86400)


class TestVpaSource:
    async def test_maps_vpa_targets_to_recommendations(self):
        source = VpaSource(Config(), collector=FakeCollector([_vpa()]))
        recs = await source.collect(_context([_metric()]))

        by_type = {r.type: r for r in recs}
        assert set(by_type) == {RecommendationType.RIGHTSIZING_CPU, RecommendationType.RIGHTSIZING_MEMORY}

        cpu = by_type[RecommendationType.RIGHTSIZING_CPU]
        assert cpu.source == RecommendationSource.VPA
        assert cpu.source_ref == "prod/api-vpa"
        assert cpu.owner_kind == "Deployment"
        assert cpu.owner_name == "api"
        assert cpu.current_cpu_request_millicores == 1000
        assert cpu.recommended_cpu_request_millicores == 300
        # 70% reduction of the annualized cost
        annual_cost = 1.0 * (365 / 7)
        assert cpu.potential_savings_cost is not None
        assert abs(cpu.potential_savings_cost - annual_cost * 0.7) < 0.01

    async def test_skips_increases(self):
        source = VpaSource(Config(), collector=FakeCollector([_vpa(cpu_target=2000, memory_target=2 * 1024**3)]))
        recs = await source.collect(_context([_metric()]))
        assert recs == []

    async def test_aggregates_multiple_containers(self):
        vpas = [
            _vpa(cpu_target=200, memory_target=256 * 1024**2),
            VPARecommendation(
                namespace="prod",
                vpa_name="api-vpa",
                target_kind="Deployment",
                target_name="api",
                container_name="sidecar",
                cpu_target_millicores=100,
                memory_target_bytes=128 * 1024**2,
            ),
        ]
        source = VpaSource(Config(), collector=FakeCollector(vpas))
        recs = await source.collect(_context([_metric()]))

        cpu = next(r for r in recs if r.type == RecommendationType.RIGHTSIZING_CPU)
        mem = next(r for r in recs if r.type == RecommendationType.RIGHTSIZING_MEMORY)
        assert cpu.recommended_cpu_request_millicores == 300
        assert mem.recommended_memory_request_bytes == 384 * 1024**2

    async def test_skips_targets_without_metrics(self):
        vpa = _vpa()
        vpa.target_name = "unknown-workload"
        source = VpaSource(Config(), collector=FakeCollector([vpa]))
        recs = await source.collect(_context([_metric()]))
        assert recs == []

    async def test_collector_failure_is_isolated(self):
        class BrokenCollector:
            async def collect(self):
                raise RuntimeError("k8s down")

        source = VpaSource(Config(), collector=BrokenCollector())
        assert await source.collect(_context([_metric()])) == []
