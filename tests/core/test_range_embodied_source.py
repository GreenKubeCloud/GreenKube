"""Collected rows carry where their embodied emissions came from (NRG-01 acceptance b, c)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from greenkube.core.config import Config
from greenkube.core.node_zone_mapper import NodeZoneMapper
from greenkube.models.node import NodeInfo
from tests.core.fake_prometheus import FakePrometheus  # pyrefly: ignore[missing-import]
from tests.core.range_harness import FakeOpenCost, build_range_processor  # pyrefly: ignore[missing-import]

STEP = 300
BASE = datetime(2026, 11, 7, 11, 0, tzinfo=timezone.utc)
LIFESPAN_HOURS = 4 * 8760


async def _run(tmp_path, node: NodeInfo):
    cfg = Config(
        DB_TYPE="sqlite",
        DB_PATH=str(tmp_path / "range.db"),
        PROMETHEUS_QUERY_RANGE_STEP="5m",
        KUBERNETES_METRICS_API_ENABLED=False,
        DEFAULT_ZONE="unknown",
    )
    prom = FakePrometheus()
    labels = {"namespace": "ns", "pod": "p", "node": node.name, "container": "app"}
    prom.add("container_cpu_usage_seconds_total", labels, 0.1)
    prom.add("container_memory_working_set_bytes", labels, 1024**2)
    processor = build_range_processor(
        cfg, MagicMock(), prom, FakeOpenCost(), nodes={node.name: node}, zone_mapper=NodeZoneMapper(MagicMock(), cfg)
    )
    rows = await processor.run_range(BASE, BASE + timedelta(seconds=STEP))
    return [row for row in rows if not row.container_name], processor.last_idle_metrics, cfg


async def test_on_prem_node_gets_the_per_vcpu_fallback_flagged(tmp_path):
    node = NodeInfo(name="m", cloud_provider="unknown", instance_type="cpu-96", cpu_capacity_cores=96)
    pods, (idle,), cfg = await _run(tmp_path, node)
    expected = 96 * cfg.DEFAULT_EMBODIED_KG_PER_VCPU * 1000 / LIFESPAN_HOURS * STEP / 3600
    assert sum(row.embodied_co2e_grams or 0 for row in pods) + idle.embodied_co2e_grams == pytest.approx(expected)
    assert {row.embodied_source for row in pods} == {"fallback_per_vcpu"}
    assert idle.embodied_source == "fallback_per_vcpu"
    assert all(any(r.startswith("Embodied emissions estimated") for r in row.estimation_reasons) for row in pods)


async def test_annotated_on_prem_node_is_configured(tmp_path):
    node = NodeInfo(
        name="m", cloud_provider="unknown", instance_type="cpu-8", cpu_capacity_cores=8, embodied_emissions_kg=1500
    )
    pods, (idle,), _ = await _run(tmp_path, node)
    expected = 1500 * 1000 / LIFESPAN_HOURS * STEP / 3600
    assert sum(row.embodied_co2e_grams or 0 for row in pods) + idle.embodied_co2e_grams == pytest.approx(expected)
    assert {row.embodied_source for row in pods} == {"configured"}
    assert not any(any(r.startswith("Embodied emissions estimated") for r in row.estimation_reasons) for row in pods)
