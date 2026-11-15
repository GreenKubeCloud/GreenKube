"""WP-01.1: history is built from the Kubernetes state at sample time (COL-01, RECON-05).

The range processor runs after the window it collects, so the node or the pod
of a historical sample may already be gone from the Kubernetes API.  Its
attributes must then come from the stored snapshots, not from defaults.
"""

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


def _config(tmp_path) -> Config:
    return Config(
        DB_TYPE="sqlite",
        DB_PATH=str(tmp_path / "range.db"),
        PROMETHEUS_QUERY_RANGE_STEP="5m",
        KUBERNETES_METRICS_API_ENABLED=False,
        DEFAULT_ZONE="unknown",
        DEFAULT_PUE=1.3,
    )


def _prometheus(node: str = "gone-node") -> FakePrometheus:
    prom = FakePrometheus()
    labels = {"namespace": "ns", "pod": "p", "node": node, "container": "app"}
    prom.add("container_cpu_usage_seconds_total", labels, 0.2)
    prom.add("container_memory_working_set_bytes", labels, 100 * 1024**2)
    return prom


def _snapshot(timestamp: datetime) -> NodeInfo:
    return NodeInfo(
        name="gone-node",
        instance_type="m5.large",
        zone="eu-west-3a",
        region="eu-west-3",
        cloud_provider="aws",
        cpu_capacity_cores=2,
        memory_capacity_bytes=8 * 1024**3,
        timestamp=timestamp,
    )


def _build(cfg: Config, *, snapshots, prom: FakePrometheus):
    return build_range_processor(
        cfg,
        MagicMock(),
        prom,
        FakeOpenCost(),
        nodes={},
        instance_types={},
        node_snapshots=snapshots,
        zone_mapper=NodeZoneMapper(MagicMock(), cfg),
    )


@pytest.mark.asyncio
async def test_node_absent_from_api_takes_zone_pue_and_embodied_from_snapshot(tmp_path):
    """RECON-05 b / COL-01 c: snapshot valid 10:00-12:00, no current node."""
    cfg = _config(tmp_path)
    processor = _build(cfg, snapshots=[_snapshot(BASE - timedelta(hours=1))], prom=_prometheus())

    rows = await processor.run_range(BASE, BASE + timedelta(seconds=STEP))

    pods = [row for row in rows if not row.container_name]
    assert len(pods) == 1
    row = pods[0]
    assert row.emaps_zone == "FR"
    assert row.pue == cfg.get_pue_for_provider("aws")
    assert row.pue != 1.3
    assert row.node_instance_type == "m5.large"
    assert row.node_zone == "eu-west-3a"
    assert row.embodied_co2e_grams > 0
    assert row.node_source == "snapshot"
    assert not any("zone map" in reason for reason in row.estimation_reasons)
    idle = processor.last_idle_metrics
    assert idle and idle[0].emaps_zone == "FR"


@pytest.mark.asyncio
async def test_node_seen_only_after_the_sample_uses_the_earliest_later_snapshot(tmp_path):
    """RECON-05 fix: the earliest snapshot after the sample time when none is valid at it."""
    cfg = _config(tmp_path)
    processor = _build(cfg, snapshots=[], prom=_prometheus())
    later = _snapshot(BASE + timedelta(seconds=120))
    processor.node_repository.get_snapshots.return_value = [(later.timestamp, later)]

    rows = await processor.run_range(BASE, BASE + timedelta(seconds=STEP))

    row = next(row for row in rows if not row.container_name)
    assert row.emaps_zone == "FR"
    assert row.node_instance_type == "m5.large"
    assert row.node_source == "snapshot"


@pytest.mark.asyncio
async def test_node_without_any_snapshot_keeps_fallback_and_is_flagged(tmp_path):
    """RECON-05 c: no snapshot at all -> fallback values, flagged, node_source unknown."""
    cfg = _config(tmp_path)
    processor = _build(cfg, snapshots=[], prom=_prometheus())

    rows = await processor.run_range(BASE, BASE + timedelta(seconds=STEP))

    row = next(row for row in rows if not row.container_name)
    assert row.emaps_zone == "unknown"
    assert row.pue == 1.3
    assert row.is_estimated
    assert row.node_source == "unknown"


@pytest.mark.asyncio
async def test_current_node_is_labelled_current_api(tmp_path):
    """A node present in the API and without a snapshot keeps node_source current_api."""
    cfg = _config(tmp_path)
    current = _snapshot(BASE)
    current.timestamp = None
    processor = build_range_processor(
        cfg,
        MagicMock(),
        _prometheus(),
        FakeOpenCost(),
        nodes={"gone-node": current},
        zone_mapper=NodeZoneMapper(MagicMock(), cfg),
    )

    rows = await processor.run_range(BASE, BASE + timedelta(seconds=STEP))

    row = next(row for row in rows if not row.container_name)
    assert row.emaps_zone == "FR"
    assert row.node_source == "current_api"
