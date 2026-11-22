"""Cost keying, cost components and provenance in the production run_range path.

Roadmap A4 (BUG-04: OpenCost cost map keyed by pod name only), A11 (per-dimension
provenance; container rows not blanket-estimated, BUG-29) and A8 (intensity
provenance on stored rows).
"""

from datetime import datetime, timedelta, timezone
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from greenkube.core.calculator import CarbonCalculator
from greenkube.core.config import Config
from greenkube.core.historical_range_processor import HistoricalRangeProcessor
from greenkube.core.metric_assembler import MetricAssembler
from greenkube.energy.estimator import BasicEstimator
from greenkube.models.metrics import CostMetric, PodMetric
from greenkube.models.node import NodeInfo, NodeZoneContext
from greenkube.models.provenance import is_cost_trusted, is_usage_trusted
from greenkube.storage.base_repository import CarbonIntensityRepository, IntensityPoint

START = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
END = START + timedelta(minutes=5)
PODS = (("cache", "redis-0", 0.4), ("queue", "redis-0", 0.1))


class FreshIntensity(CarbonIntensityRepository):
    async def get_for_zone_at_time(self, zone: str, timestamp: str) -> float | None:
        return None

    async def get_intensity_point(self, zone: str, timestamp: str) -> Optional[IntensityPoint]:
        return IntensityPoint(value=50.0, observed_at=START, is_estimated=False)

    async def save_history(self, history_data: list, zone: str) -> int:
        return 0


def _series(query: str) -> list[dict]:
    # The sample evaluated at END describes the half-open window [START, END) (A2).
    ts = END.timestamp()
    if "container_cpu_usage_seconds_total" in query:
        return [
            {
                "metric": {"namespace": ns, "pod": pod, "container": "redis", "node": "node-1"},
                "values": [[ts, str(cores)]],
            }
            for ns, pod, cores in PODS
        ]
    if "container_memory_working_set_bytes" in query:
        return [
            {
                "metric": {"namespace": ns, "pod": pod, "container": "redis", "node": "node-1"},
                "values": [[ts, str(64 * 1024**2)]],
            }
            for ns, pod, _ in PODS
        ]
    return []


def _processor(cost_metrics: list[CostMetric]) -> HistoricalRangeProcessor:
    config = Config()
    prometheus = MagicMock()
    prometheus.collect_range = AsyncMock(side_effect=lambda **kwargs: _series(kwargs["query"]))
    opencost = MagicMock()
    opencost.collect_range = AsyncMock(return_value=cost_metrics)
    node_info = NodeInfo(
        name="node-1",
        zone="eu-west-3a",
        region="eu-west-3",
        cloud_provider="aws",
        instance_type="m5.large",
        architecture="amd64",
        node_pool=None,
    )
    nodes = MagicMock()
    nodes.collect_instance_types = AsyncMock(return_value={"node-1": "m5.large"})
    nodes.collect = AsyncMock(return_value={"node-1": node_info})
    pods = MagicMock()
    pods.collect = AsyncMock(
        return_value=[
            PodMetric(
                pod_name=pod,
                namespace=ns,
                container_name="redis",
                cpu_request=500,
                memory_request=128 * 1024**2,
                cpu_limit=1000,
                memory_limit=256 * 1024**2,
            )
            for ns, pod, _ in PODS
        ]
    )
    stored = MagicMock()
    stored.read_combined_metrics = AsyncMock(return_value=[])
    node_repository = MagicMock()
    node_repository.get_latest_snapshots_before = AsyncMock(return_value=[])
    node_repository.get_snapshots = AsyncMock(return_value=[])
    intensity = FreshIntensity()
    estimator = BasicEstimator(config)
    embodied = MagicMock()
    embodied.prepare_embodied_data = AsyncMock(return_value={})
    embodied.calculate_pod_embodied = MagicMock(return_value=0.0)
    embodied.is_embodied_fallback = MagicMock(return_value=False)
    calculator = CarbonCalculator(intensity, config=config)
    assembler = MetricAssembler(
        calculator=calculator,
        estimator=estimator,
        repository=intensity,
        electricity_provider=MagicMock(),
        zone_mapper=MagicMock(),
        embodied_service=embodied,
        config=config,
    )
    zone_mapper = MagicMock()
    zone_mapper.map_nodes = AsyncMock(return_value={"node-1": NodeZoneContext(node="node-1", emaps_zone="FR")})
    metrics_server = MagicMock()
    metrics_server.collect = AsyncMock(return_value=[])
    return HistoricalRangeProcessor(
        prometheus_collector=prometheus,
        opencost_collector=opencost,
        node_collector=nodes,
        pod_collector=pods,
        repository=intensity,
        combined_metrics_repository=stored,
        node_repository=node_repository,
        calculator=calculator,
        estimator=estimator,
        assembler=assembler,
        zone_mapper=zone_mapper,
        config=config,
        metrics_server_collector=metrics_server,
    )


def _cost(namespace: str, total: float, cpu: float, ram: float) -> CostMetric:
    return CostMetric(
        pod_name="redis-0",
        namespace=namespace,
        cpu_cost=cpu,
        ram_cost=ram,
        storage_cost=total - cpu - ram,
        total_cost=total,
        window_start=START,
        window_end=END,
        minutes=5,
    )


@pytest.mark.asyncio
async def test_same_named_pods_get_their_own_namespace_cost_and_components():
    processor = _processor([_cost("cache", 0.03, 0.02, 0.005), _cost("queue", 0.006, 0.004, 0.002)])
    collect_range = processor.opencost_collector.collect_range

    rows = await processor.run_range(START, END)

    pods = {row.namespace: row for row in rows if row.container_name is None}
    assert set(pods) == {"cache", "queue"}
    assert pods["cache"].total_cost == pytest.approx(0.03)
    assert pods["queue"].total_cost == pytest.approx(0.006)
    assert pods["cache"].cost_cpu == pytest.approx(0.02)
    assert pods["cache"].cost_ram == pytest.approx(0.005)
    assert pods["cache"].cost_storage == pytest.approx(0.005)
    assert isinstance(collect_range, AsyncMock)
    collect_range.assert_awaited_once_with(start=START, end=END)


@pytest.mark.asyncio
async def test_pod_and_container_rows_carry_provenance():
    processor = _processor([_cost("cache", 0.03, 0.02, 0.005)])

    rows = await processor.run_range(START, END)

    cache_rows = [row for row in rows if row.namespace == "cache"]
    assert {row.container_name for row in cache_rows} == {None, "redis"}
    for row in cache_rows:
        assert row.cost_source == "opencost"
        assert row.usage_source == "prometheus"
        assert row.power_source == "instance_profile"
        assert row.intensity_source == "consolidated"
        assert is_cost_trusted(row) and is_usage_trusted(row)
    container = next(row for row in cache_rows if row.container_name)
    assert container.cpu_limit == 1000
    assert container.memory_limit == 256 * 1024**2
    assert container.emaps_zone == "FR"
    assert container.is_estimated == next(row for row in cache_rows if row.container_name is None).is_estimated

    # Without an OpenCost cost, the Impact Model prices the pod's reservation at
    # the node's list price (B2) and labels the tier; it never claims "opencost".
    queue_pod = next(row for row in rows if row.namespace == "queue" and row.container_name is None)
    assert queue_pod.cost_source == "pricing_catalog"
    assert queue_pod.total_cost > 0
    assert queue_pod.total_cost == pytest.approx((queue_pod.cost_cpu or 0) + (queue_pod.cost_ram or 0))
    assert any(reason.startswith("No observed cost for pod") for reason in queue_pod.estimation_reasons)
