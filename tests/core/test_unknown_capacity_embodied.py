"""A node of unknown capacity gets the flat embodied default, not 1 vCPU of the per-vCPU fallback (NRG-01)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from greenkube.core.config import Config
from greenkube.core.node_zone_mapper import NodeZoneMapper
from greenkube.impact.projector import ImpactProjector
from greenkube.impact.recompute import MetricRecomputer
from greenkube.models.node import NodeInfo
from tests.core.fake_prometheus import FakePrometheus  # pyrefly: ignore[missing-import]
from tests.core.range_harness import FakeOpenCost, build_range_processor  # pyrefly: ignore[missing-import]

STEP = 300
BASE = datetime(2026, 11, 7, 11, 0, tzinfo=timezone.utc)


def _unknown_node() -> NodeInfo:
    return NodeInfo(name="u1", cloud_provider="unknown")


async def test_collection_uses_the_flat_default(tmp_path):
    cfg = Config(
        DB_TYPE="sqlite",
        DB_PATH=str(tmp_path / "range.db"),
        PROMETHEUS_QUERY_RANGE_STEP="5m",
        KUBERNETES_METRICS_API_ENABLED=False,
        DEFAULT_ZONE="unknown",
    )
    prom = FakePrometheus()
    labels = {"namespace": "ns", "pod": "p", "node": "u1", "container": "app"}
    prom.add("container_cpu_usage_seconds_total", labels, 0.1)
    prom.add("container_memory_working_set_bytes", labels, 1024**2)
    node = _unknown_node()
    processor = build_range_processor(
        cfg, MagicMock(), prom, FakeOpenCost(), nodes={"u1": node}, zone_mapper=NodeZoneMapper(MagicMock(), cfg)
    )
    rows = await processor.run_range(BASE, BASE + timedelta(seconds=STEP))
    assert {row.embodied_source for row in rows if not row.container_name} == {"fallback_default"}


def test_recompute_uses_the_flat_default():
    recomputer = MetricRecomputer(Config(), MagicMock(), MagicMock())
    spec = recomputer._spec("u1", _unknown_node(), [], 0.0)
    assert spec.embodied_source == "fallback_default"


def test_projector_uses_the_flat_default():
    projector = ImpactProjector(Config(), node_infos=[_unknown_node()])
    assert projector.pool.nodes["u1"].embodied_source == "fallback_default"
