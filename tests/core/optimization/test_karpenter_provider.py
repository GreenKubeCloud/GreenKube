# tests/core/optimization/test_karpenter_provider.py
"""Tests for the Karpenter NodePool consolidation source."""

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.collectors.karpenter_collector import NodePoolInfo
from greenkube.core.config import Config
from greenkube.core.optimization.context import OptimizationContext
from greenkube.core.optimization.providers.karpenter import KarpenterSource
from greenkube.models.metrics import CombinedMetric, RecommendationCapability, RecommendationType
from greenkube.models.node import NodeInfo


class FakeCollector:
    def __init__(self, pools):
        self.pools = pools

    async def collect(self):
        return self.pools


def _context(metrics, node_infos) -> OptimizationContext:
    return OptimizationContext(
        config=Config(),
        metrics=metrics,
        node_infos=node_infos,
        analysis_window_seconds=86400,
        window_start=datetime.now(timezone.utc),
        window_end=datetime.now(timezone.utc),
    )


def _metric(
    node: str, cpu_request: int, cost: float = 1.0, timestamp: datetime | None = None, pod_name: str | None = None
) -> CombinedMetric:
    return CombinedMetric(
        pod_name=pod_name or f"pod-{node}",
        namespace="default",
        node=node,
        cpu_request=cpu_request,
        total_cost=cost,
        timestamp=timestamp or datetime.now(timezone.utc),
    )


def _node(name: str, capacity: int) -> NodeInfo:
    return NodeInfo(name=name, cpu_capacity_cores=capacity / 1000, cloud_provider="aws")


class TestKarpenterSource:
    @pytest.mark.asyncio
    async def test_emits_consolidation_for_underutilized_pool(self):
        pool = NodePoolInfo(name="default", node_names=["n1", "n2"], consolidation_policy="WhenUnderutilized")
        source = KarpenterSource(Config(), collector=FakeCollector([pool]))
        context = _context(
            [_metric("n1", 200), _metric("n2", 200)],
            [_node("n1", 4000), _node("n2", 4000)],
        )

        recs = await source.collect(context)

        assert len(recs) == 1
        rec = recs[0]
        assert rec.type == RecommendationType.OVERPROVISIONED_NODE
        assert rec.capability == RecommendationCapability.NODE_POOL
        assert rec.source.value == "karpenter"
        assert rec.source_ref == "default"
        assert rec.target_node == "nodepool/default"
        assert rec.potential_savings_cost is not None

    @pytest.mark.asyncio
    async def test_skips_single_node_pool(self):
        pool = NodePoolInfo(name="small", node_names=["n1"])
        source = KarpenterSource(Config(), collector=FakeCollector([pool]))
        context = _context([_metric("n1", 100)], [_node("n1", 4000)])
        assert await source.collect(context) == []

    @pytest.mark.asyncio
    async def test_skips_utilized_pool(self):
        pool = NodePoolInfo(name="busy", node_names=["n1", "n2"])
        source = KarpenterSource(Config(), collector=FakeCollector([pool]))
        context = _context(
            [_metric("n1", 3500), _metric("n2", 3500)],
            [_node("n1", 4000), _node("n2", 4000)],
        )
        assert await source.collect(context) == []

    @pytest.mark.asyncio
    async def test_no_pools_returns_empty(self):
        source = KarpenterSource(Config(), collector=FakeCollector([]))
        assert await source.collect(_context([], [])) == []

    @pytest.mark.asyncio
    async def test_fake_collector_is_available(self):
        source = KarpenterSource(Config(), collector=FakeCollector([]))
        assert await source.is_available() is True

    @pytest.mark.asyncio
    async def test_missing_node_metrics_returns_empty(self):
        pool = NodePoolInfo(name="unknown", node_names=["n1", "n2"])
        source = KarpenterSource(Config(), collector=FakeCollector([pool]))
        assert await source.collect(_context([], [])) == []

    @pytest.mark.asyncio
    async def test_aggregates_multi_replica_points_per_timestamp(self):
        pool = NodePoolInfo(name="temporal", node_names=["n1", "n2"])
        source = KarpenterSource(Config(), collector=FakeCollector([pool]))
        start = datetime.now(timezone.utc) - timedelta(hours=1)
        metrics = [
            _metric("n1", 200, timestamp=start, pod_name="api-a"),
            _metric("n1", 200, timestamp=start, pod_name="api-b"),
            _metric("n2", 200, timestamp=start, pod_name="api-c"),
            _metric("n2", 200, timestamp=start, pod_name="api-d"),
            _metric("n1", 800, timestamp=start + timedelta(minutes=5), pod_name="api-a"),
            _metric("n2", 800, timestamp=start + timedelta(minutes=5), pod_name="api-c"),
        ]

        recs = await source.collect(_context(metrics, [_node("n1", 4000), _node("n2", 4000)]))

        assert len(recs) == 1
