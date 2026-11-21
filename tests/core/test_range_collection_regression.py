"""Regression tests ported from the audit scenario ``sim_range.py``.

They drive the real :class:`HistoricalRangeProcessor` with the real SQLite
repository, calculator, estimator and assembler, and a fake Prometheus that
applies PromQL label matchers to raw cAdvisor-like series.

* BUG-01 / A1: the pod cgroup series (``container=""``) and the pause
  container (``container="POD"``) must not be summed with container series.
* BUG-03 / A2: consecutive scheduled windows are each computed exactly once,
  with half-open ``[start, end)`` semantics and one row per pod and step.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.core.config import Config
from greenkube.core.db import DatabaseManager
from greenkube.storage.sqlite.repository import SQLiteCombinedMetricsRepository
from tests.core.fake_prometheus import FakePrometheus  # pyrefly: ignore[missing-import]
from tests.core.range_harness import FakeOpenCost, build_range_processor  # pyrefly: ignore[missing-import]

STEP = 300
BASE = datetime(2026, 11, 7, 10, 0, tzinfo=timezone.utc)
CPU_PER_CONTAINER = 0.2
MEMORY_PER_CONTAINER = 100 * 1024**2
# m5.large: 2 vCPU, AWS CCF coefficients 0.74 W / 3.5 W per vCPU.
# util = 0.2 / 2 -> node CPU power 1.48 + 0.1 * (7.0 - 1.48) = 2.032 W -> 609.6 J per 5-minute step.
NODE_CPU_JOULES = 609.6
# Reservation-aware allocation (B2): the pod reserves 1 of 2 cores, so it gets
# half the idle power (0.74 W) plus all the dynamic power (0.552 W), plus the
# memory power of its 100 MiB working set (0.392 W/GiB, B5). The other half of
# the idle power goes to the node's __idle__ row.
MEMORY_WATTS = 100 / 1024 * 0.392
GOLDEN_JOULES = (0.74 + 0.552 + MEMORY_WATTS) * STEP
IDLE_JOULES = 0.74 * STEP


def _cadvisor(prom: FakePrometheus, *, include_pod_cgroup: bool) -> None:
    labels = {"namespace": "ns", "pod": "p", "node": "n1"}
    prom.add("container_cpu_usage_seconds_total", {**labels, "container": "app"}, CPU_PER_CONTAINER)
    prom.add("container_memory_working_set_bytes", {**labels, "container": "app"}, MEMORY_PER_CONTAINER)
    if include_pod_cgroup:
        # cAdvisor pod-level cgroup (no container label) and the pause container.
        prom.add("container_cpu_usage_seconds_total", labels, CPU_PER_CONTAINER)
        prom.add("container_cpu_usage_seconds_total", {**labels, "container": "POD"}, 0.001)
        prom.add("container_memory_working_set_bytes", labels, MEMORY_PER_CONTAINER)
        prom.add("container_memory_working_set_bytes", {**labels, "container": "POD"}, 1024**2)


@pytest.fixture
async def harness(tmp_path):
    cfg = Config(
        DB_TYPE="sqlite",
        DB_PATH=str(tmp_path / "range.db"),
        PROMETHEUS_QUERY_RANGE_STEP="5m",
        KUBERNETES_METRICS_API_ENABLED=False,
        CLOUD_PROVIDER="aws",
    )
    dbm = DatabaseManager(cfg)
    await dbm.connect()
    repo = SQLiteCombinedMetricsRepository(dbm)

    def build(prom: FakePrometheus, opencost: FakeOpenCost):
        return build_range_processor(cfg, repo, prom, opencost)

    try:
        yield build, repo
    finally:
        await dbm.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("include_pod_cgroup", [False, True], ids=["containers-only", "with-pod-cgroup"])
async def test_range_usage_counts_each_container_once(harness, include_pod_cgroup):
    """BUG-01: CPU stays at 200m (not 400m) and energy at the golden 609.6 J."""
    build, _repo = harness
    prom = FakePrometheus()
    _cadvisor(prom, include_pod_cgroup=include_pod_cgroup)
    processor = build(prom, FakeOpenCost())

    rows = await processor.run_range(BASE, BASE + timedelta(seconds=STEP))

    pods = [row for row in rows if not row.container_name]
    containers = [row for row in rows if row.container_name]
    assert len(pods) == 1
    assert pods[0].cpu_usage_millicores == 200
    assert pods[0].memory_usage_bytes == MEMORY_PER_CONTAINER
    assert pods[0].joules == pytest.approx(GOLDEN_JOULES, rel=1e-6)
    (idle,) = processor.last_idle_metrics
    assert idle.joules == pytest.approx(IDLE_JOULES, rel=1e-6)
    # Conservation: pod rows + __idle__ = node power (CPU profile + memory).
    assert pods[0].joules + idle.joules == pytest.approx(NODE_CPU_JOULES + MEMORY_WATTS * STEP, rel=1e-6)
    assert [(c.container_name, c.cpu_usage_millicores) for c in containers] == [("app", 200)]
    # The node root cgroup query (id="/", NRG-12) is node-level by design and not a pod usage query.
    cpu_queries = [q for q, *_ in prom.queries if "container_cpu_usage_seconds_total" in q and 'id="/"' not in q]
    # Containers and pod cgroups are read together (COL-09); the per-pod choice keeps the value at 200m.
    assert cpu_queries and all(
        'container!="POD"' in q and ('pod!=""' in q or 'container!=""' in q) for q in cpu_queries
    )


@pytest.mark.asyncio
async def test_consecutive_windows_are_each_computed_once_with_half_open_bounds(harness):
    """BUG-03: no window is skipped, no sample is shared by two windows, cost is per window."""
    build, repo = harness
    prom = FakePrometheus()
    _cadvisor(prom, include_pod_cgroup=True)
    opencost = FakeOpenCost()
    processor = build(prom, opencost)

    for k in range(6):
        start = BASE + timedelta(seconds=STEP * k)
        end = start + timedelta(seconds=STEP)
        rows = await processor.run_range(start, end)
        pod_rows = [row for row in rows if not row.container_name]
        assert [row.timestamp for row in pod_rows] == [start], f"window {start:%H:%M}"
        await repo.write_combined_metrics(rows)

    assert len(opencost.calls) == 6
    stored = await repo.read_combined_metrics(BASE - timedelta(hours=1), BASE + timedelta(hours=1))
    pod_rows = sorted((row for row in stored if not row.container_name), key=lambda row: row.timestamp)
    assert [row.timestamp for row in pod_rows] == [BASE + timedelta(seconds=STEP * k) for k in range(6)]
    assert [row.total_cost for row in pod_rows] == pytest.approx([k * 5 / 100.0 for k in range(6)])
    assert all(row.cpu_usage_millicores == 200 for row in pod_rows)
    assert all(row.duration_seconds == STEP for row in pod_rows)


@pytest.mark.asyncio
async def test_multi_step_range_yields_one_row_per_step_with_its_own_usage(harness):
    """A backfill chunk keeps per-step usage instead of smearing one value over the chunk."""
    build, _repo = harness
    prom = FakePrometheus()
    labels = {"namespace": "ns", "pod": "p", "node": "n1", "container": "app"}
    # Usage measured at evaluation time t describes the step that ends at t.
    prom.add(
        "container_cpu_usage_seconds_total",
        labels,
        lambda ts: 0.1 * (1 + int((ts - BASE.timestamp()) // STEP)),
    )
    processor = build(prom, FakeOpenCost())

    rows = await processor.run_range(BASE, BASE + timedelta(seconds=3 * STEP))

    pod_rows = sorted((row for row in rows if not row.container_name), key=lambda row: row.timestamp)
    assert [row.timestamp for row in pod_rows] == [BASE + timedelta(seconds=STEP * k) for k in range(3)]
    # Row [10:00, 10:05) is evaluated at 10:05 (k=1) and so on.
    assert [row.cpu_usage_millicores for row in pod_rows] == [200, 300, 400]
