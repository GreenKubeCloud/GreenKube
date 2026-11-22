"""B.4: the range collection sends fewer Prometheus range queries per chunk.

The network, disk, ephemeral-storage and PVC dimensions were removed, so
their queries are no longer sent. A recording Prometheus fake counts the
range queries sent for each collection chunk.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from greenkube.core.config import Config
from greenkube.models.metrics import CostMetric, PodMetric
from tests.core.fake_prometheus import FakePrometheus  # pyrefly: ignore[missing-import]
from tests.core.range_harness import build_range_processor  # pyrefly: ignore[missing-import]

STEP = 300
STEPS = 24  # two one-hour chunks
CHUNK_SECONDS = 3600
BASE = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)

#: Range queries per chunk before B.4 (network receive and transmit, disk read
#: and write, ephemeral usage) and after.
QUERIES_PER_CHUNK_BEFORE = 16
QUERIES_PER_CHUNK_AFTER = 11

REMOVED_METRICS = (
    "container_network_receive_bytes_total",
    "container_network_transmit_bytes_total",
    "container_fs_reads_bytes_total",
    "container_fs_writes_bytes_total",
    "container_fs_usage_bytes",
    "kube_persistentvolumeclaim",
    "kubelet_volume_stats",
)


class _OpenCost:
    async def collect_range(self, start, end):
        return [CostMetric(pod_name="app", namespace="ns", cpu_cost=0, ram_cost=0, total_cost=1.0, minutes=60)]


def _prometheus() -> FakePrometheus:
    prom = FakePrometheus()
    prom.add("kube_pod_info", {"namespace": "ns", "pod": "app", "node": "n1"}, 1.0)
    prom.add("kube_pod_status_phase", {"namespace": "ns", "pod": "app", "phase": "Running"}, 1.0)
    labels = {"namespace": "ns", "pod": "app", "node": "n1", "container": "app"}
    prom.add("container_cpu_usage_seconds_total", labels, 0.2)
    prom.add("container_memory_working_set_bytes", labels, 100 * 1024**2)
    return prom


@pytest.mark.asyncio
async def test_range_queries_per_chunk_drop_after_dimension_removal(tmp_path, monkeypatch):
    monkeypatch.setattr("greenkube.core.historical_range_processor._CHUNK_SECONDS", CHUNK_SECONDS)
    cfg = Config(
        DB_TYPE="sqlite",
        DB_PATH=str(tmp_path / "range.db"),
        PROMETHEUS_QUERY_RANGE_STEP="5m",
        KUBERNETES_METRICS_API_ENABLED=False,
        CLOUD_PROVIDER="aws",
    )
    pods = [PodMetric(pod_name="app", namespace="ns", container_name="app", cpu_request=100, memory_request=0)]
    prom = _prometheus()
    processor = build_range_processor(cfg, MagicMock(), prom, _OpenCost(), pod_list=pods)

    rows = await processor.run_range(BASE, BASE + timedelta(seconds=STEPS * STEP))

    assert rows
    # A chunk's queries share its end; some start one step early to read the first sample's rate.
    by_chunk = Counter(end for _query, _start, end, _step in prom.queries)
    per_chunk = list(by_chunk.values())
    print("range queries per chunk:", per_chunk)
    assert len(per_chunk) == 2
    assert per_chunk == [QUERIES_PER_CHUNK_AFTER] * 2
    assert QUERIES_PER_CHUNK_AFTER < QUERIES_PER_CHUNK_BEFORE
    sent = " ".join(query for query, *_ in prom.queries)
    assert not [name for name in REMOVED_METRICS if name in sent]
