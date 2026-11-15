"""Build a real HistoricalRangeProcessor wired to fakes for collection tests."""

from __future__ import annotations

from datetime import datetime
from typing import Iterable
from unittest.mock import AsyncMock, MagicMock

from greenkube.core.calculator import CarbonCalculator
from greenkube.core.config import Config
from greenkube.core.embodied_service import EmbodiedEmissionsService
from greenkube.core.historical_range_processor import HistoricalRangeProcessor
from greenkube.core.metric_assembler import MetricAssembler
from greenkube.energy.estimator import BasicEstimator
from greenkube.models.metrics import CostMetric, PodMetric
from greenkube.models.node import NodeInfo


class FakeOpenCost:
    def __init__(self, pods: Iterable[tuple[str, str]] = (("ns", "p"),)) -> None:
        self.pods = list(pods)
        self.calls: list[tuple[datetime, datetime]] = []

    async def collect_range(self, start, end):
        self.calls.append((start, end))
        # The cost of a window identifies the window: minute-of-hour / 100.
        return [
            CostMetric(pod_name=pod, namespace=namespace, cpu_cost=0, ram_cost=0, total_cost=start.minute / 100.0)
            for namespace, pod in self.pods
        ]


def build_range_processor(
    cfg: Config,
    combined_repository,
    prometheus,
    opencost,
    *,
    pods: Iterable[tuple[str, str, str]] = (("ns", "p", "app"),),
    nodes: dict[str, NodeInfo] | None = None,
    instance_types: dict[str, str] | None = None,
    node_snapshots: Iterable[NodeInfo] = (),
    zone_mapper=None,
    pod_snapshot_repository=None,
    pod_list: list[PodMetric] | None = None,
) -> HistoricalRangeProcessor:
    ci_repo = MagicMock()
    ci_repo.get_for_zone_at_time = AsyncMock(return_value=100.0)
    calculator = CarbonCalculator(ci_repo, config=cfg)
    estimator = BasicEstimator(cfg)
    node_repo = MagicMock()
    node_repo.get_latest_snapshots_before = AsyncMock(return_value=list(node_snapshots))
    node_repo.get_snapshots = AsyncMock(return_value=[])
    node_repo.save_nodes = AsyncMock(return_value=0)
    if nodes is None:
        nodes = {"n1": NodeInfo(name="n1", instance_type="m5.large", cloud_provider="aws", cpu_capacity_cores=2)}
    node_collector = MagicMock()
    node_collector.collect_instance_types = AsyncMock(
        return_value=instance_types
        if instance_types is not None
        else {name: info.instance_type for name, info in nodes.items() if info.instance_type}
    )
    node_collector.collect = AsyncMock(return_value=nodes)
    pod_collector = MagicMock()
    pod_collector.collect = AsyncMock(
        return_value=pod_list
        if pod_list is not None
        else [
            PodMetric(pod_name=pod, namespace=namespace, container_name=container, cpu_request=1000, memory_request=0)
            for namespace, pod, container in pods
        ]
    )
    if zone_mapper is None:
        zone_mapper = MagicMock()
        zone_mapper.map_nodes = AsyncMock(return_value={})
    embodied_repo = MagicMock()
    embodied_repo.get_profile = AsyncMock(return_value={"gwp_manufacture": 100, "lifespan_hours": 35040})
    embodied = EmbodiedEmissionsService(MagicMock(), embodied_repo, node_repo, calculator, estimator, cfg)
    assembler = MetricAssembler(calculator, estimator, ci_repo, MagicMock(), zone_mapper, embodied, cfg)
    return HistoricalRangeProcessor(
        prometheus,
        opencost,
        node_collector,
        pod_collector,
        ci_repo,
        combined_repository,
        node_repo,
        calculator,
        estimator,
        assembler,
        zone_mapper,
        cfg,
        pod_snapshot_repository=pod_snapshot_repository,
    )
