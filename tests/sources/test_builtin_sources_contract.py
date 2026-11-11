"""The three ported built-in sources honour the Source SDK contract (roadmap E5).

* native analyzers through the analyzer adapter (``greenkube.rightsizing_cpu`` and every other analyzer);
* ``vpa``, the VPA connector through the v1 adapter;
* ``greenkube.nodepool``, the NodePool heuristic over Karpenter objects.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.collectors.karpenter_collector import NodePoolInfo
from greenkube.collectors.vpa_collector import VPARecommendation
from greenkube.core.config import Config
from greenkube.core.optimization.analyzers import (
    DEFAULT_ANALYZERS,
    CpuRightsizingAnalyzer,
    MemoryRightsizingAnalyzer,
)
from greenkube.core.optimization.context import OptimizationContext
from greenkube.core.optimization.providers.karpenter import KarpenterSource, NodePoolSignalSource
from greenkube.core.optimization.providers.vpa import VpaSignalSource, VpaSource
from greenkube.models.metrics import CombinedMetric
from greenkube.models.node import NodeInfo
from greenkube.models.workload_usage import UsageSnapshot, UsageStats, WorkloadUsageSummary
from greenkube.signals.adapters import analyzer_source_class
from greenkube.signals.model import Capability, SignalOrigin
from greenkube.signals.sdk import SourceSettings, SourceTier
from greenkube.signals.testing import check_source_contract
from tests.sources.contract import SourceContract, collect_context, deps  # pyrefly: ignore[missing-import]

MIB = 1024 * 1024


def _stats(avg: float, p95: float, p99: float, peak: float) -> UsageStats:
    return UsageStats(sample_count=4032, avg=avg, p50=avg, p90=p95, p95=p95, p99=p99, max=peak)


def _usage_context() -> OptimizationContext:
    """A two-week, mostly idle Deployment container: CPU and memory decreases."""
    last = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)

    def summary(container: str) -> WorkloadUsageSummary:
        return WorkloadUsageSummary(
            namespace="shop",
            target_kind="Deployment",
            target_name="api",
            container_name=container,
            first_hour=last - timedelta(hours=14 * 24 - 1),
            last_hour=last,
            hours_observed=14 * 24,
            cpu=_stats(50, 80, 90, 120),
            memory=_stats(100 * MIB, 120 * MIB, 130 * MIB, 140 * MIB),
            cpu_request=1000,
            memory_request=1024 * MIB,
        )

    return OptimizationContext(
        config=Config(),
        usage=UsageSnapshot(summaries={("shop", "Deployment", "api"): {"web": summary("web"), "": summary("")}}),
        window_start=last - timedelta(days=14),
        window_end=last,
    )


class TestNativeCpuRightsizingContract(SourceContract):
    def make_source(self):
        cls = analyzer_source_class(CpuRightsizingAnalyzer)
        return cls(SourceSettings(), deps())

    def make_context(self):
        return collect_context(_usage_context())


class TestNativeMemoryRightsizingContract(SourceContract):
    def make_source(self):
        cls = analyzer_source_class(MemoryRightsizingAnalyzer)
        return cls(SourceSettings(), deps())

    def make_context(self):
        return collect_context(_usage_context())


@pytest.mark.parametrize("analyzer", DEFAULT_ANALYZERS, ids=lambda cls: cls.__name__)
async def test_every_native_analyzer_honours_the_contract(analyzer):
    cls = analyzer_source_class(analyzer)
    assert cls.id.startswith("greenkube.")
    assert cls.tier == SourceTier.NATIVE
    assert cls.legacy_source == "greenkube"
    await check_source_contract(
        cls(SourceSettings(), deps()),
        collect_context(_usage_context()),
        collect_context(OptimizationContext(config=Config())),
        builtin=True,
        expect_signals=False,
    )


class _FakeVpaCollector:
    def __init__(self, recs):
        self._recs = recs

    async def collect(self):
        return list(self._recs)


def _vpa_context() -> OptimizationContext:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    metric = CombinedMetric(
        pod_name="api-7d9f8b6c5-x2k4p",
        namespace="prod",
        owner_kind="Deployment",
        owner_name="api",
        container_name="api",
        cpu_request=1000,
        memory_request=1024**3,
        total_cost=1.0,
        co2e_grams=10.0,
        timestamp=now - timedelta(hours=1),
    )
    return OptimizationContext(config=Config(), metrics=[metric], analysis_window_seconds=7 * 86400)


class TestVpaContract(SourceContract):
    def make_source(self):
        vpa = VPARecommendation(
            namespace="prod",
            vpa_name="api-vpa",
            target_kind="Deployment",
            target_name="api",
            container_name="api",
            cpu_target_millicores=300,
            memory_target_bytes=512 * MIB,
        )
        connector = VpaSource(Config(), collector=_FakeVpaCollector([vpa]))
        return VpaSignalSource(SourceSettings(), deps(), connector=connector)

    def make_context(self):
        return collect_context(_vpa_context())

    async def test_signals_carry_the_vpa_object_and_algorithm(self):
        batch = await self.make_source().collect(self.make_context())

        assert {signal.kind for signal in batch.signals} == {"RIGHTSIZING_CPU", "RIGHTSIZING_MEMORY"}
        for signal in batch.signals:
            assert signal.source_id == "vpa"
            assert signal.capability == Capability.CONTAINER_REQUESTS
            assert signal.algorithm.name == "vpa-recommender"
            assert signal.source_ref == "prod/api-vpa"
            assert signal.raw["vpa"] == "prod/api-vpa"
            assert signal.provenance.origin == SignalOrigin.ADAPTER
            assert signal.target.key() == "contract-cluster/apps.Deployment/prod/api/api"


def _nodepool_context() -> OptimizationContext:
    now = datetime.now(timezone.utc)
    metrics = [
        CombinedMetric(
            pod_name=f"pod-{node}", namespace="default", node=node, cpu_request=200, total_cost=1.0, timestamp=now
        )
        for node in ("n1", "n2")
    ]
    nodes = [NodeInfo(name=node, cpu_capacity_cores=4.0, cloud_provider="aws") for node in ("n1", "n2")]
    return OptimizationContext(
        config=Config(),
        metrics=metrics,
        node_infos=nodes,
        analysis_window_seconds=86400,
        window_start=now - timedelta(days=1),
        window_end=now,
    )


class _FakeKarpenterCollector:
    async def collect(self):
        return [NodePoolInfo(name="default", node_names=["n1", "n2"], consolidation_policy="WhenUnderutilized")]


class TestNodePoolContract(SourceContract):
    def make_source(self):
        connector = KarpenterSource(Config(), collector=_FakeKarpenterCollector())
        return NodePoolSignalSource(SourceSettings(), deps(), connector=connector)

    def make_context(self):
        return collect_context(_nodepool_context())

    async def test_is_a_native_heuristic_with_a_compatible_v1_source(self):
        source = self.make_source()
        batch = await source.collect(self.make_context())

        assert type(source).id == "greenkube.nodepool"
        assert type(source).tier == SourceTier.NATIVE
        [signal] = batch.signals
        assert signal.provenance.origin == SignalOrigin.NATIVE
        assert signal.target.kind == "NodePool" and signal.target.name == "default"
        assert signal.target.key() == "contract-cluster/karpenter.sh.NodePool/_/default"
        # v1 consumers keep seeing the historical source name and capability.
        [rec] = batch.recommendations
        assert rec.source == "karpenter"
        assert rec.capability is not None and rec.capability.value == "node_pool"
