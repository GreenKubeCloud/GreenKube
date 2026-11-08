"""OpenCost exact-window allocation keyed by (namespace, pod) (roadmap A4, BUG-04, BUG-41)."""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
import respx
from httpx import Response

from greenkube.collectors.opencost_collector import OpenCostCollector, opencost_step_share

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "opencost_allocation_namespace_pod.json").read_text())
URL = "http://opencost:9003/allocation/compute"
START = datetime(2026, 10, 1, 10, 0, tzinfo=timezone.utc)
END = datetime(2026, 10, 1, 10, 5, tzinfo=timezone.utc)


async def _collect_range(**kwargs):
    async def resolve(client):
        return URL

    with patch.object(OpenCostCollector, "_resolve_url", side_effect=resolve):
        collector = OpenCostCollector()
        try:
            return await collector.collect_range(START, END, **kwargs)
        finally:
            await collector.close()


@respx.mock
async def test_collect_range_queries_the_exact_window_by_namespace_and_pod():
    route = respx.get(URL).mock(return_value=Response(200, json=FIXTURE))

    await _collect_range()

    params = dict(route.calls.last.request.url.params)
    assert params == {
        "window": f"{int(START.timestamp())},{int(END.timestamp())}",
        "aggregate": "namespace,pod",
        "accumulate": "true",
        "includeIdle": "false",
    }


@respx.mock
async def test_same_named_pods_in_two_namespaces_get_distinct_costs():
    respx.get(URL).mock(return_value=Response(200, json=FIXTURE))

    metrics = await _collect_range()

    by_key = {(metric.namespace, metric.pod_name): metric for metric in metrics}
    assert set(by_key) == {("cache", "redis-0"), ("queue", "redis-0")}
    assert by_key[("cache", "redis-0")].total_cost == pytest.approx(0.00237)
    assert by_key[("queue", "redis-0")].total_cost == pytest.approx(0.00024)


@respx.mock
async def test_cost_components_include_adjustments_and_sum_to_total():
    respx.get(URL).mock(return_value=Response(200, json=FIXTURE))

    metric = next(m for m in await _collect_range() if m.namespace == "cache")

    assert metric.cpu_cost == pytest.approx(0.0011)
    assert metric.ram_cost == pytest.approx(0.0009)
    assert metric.storage_cost == pytest.approx(0.0003)
    assert metric.network_cost == pytest.approx(0.00002)
    assert metric.lb_cost == pytest.approx(0.00005)
    assert metric.gpu_cost == 0
    components = metric.cpu_cost + metric.ram_cost + metric.storage_cost + metric.network_cost + metric.lb_cost
    assert components == pytest.approx(metric.total_cost)
    assert metric.window_start == START
    assert metric.window_end == END
    assert metric.minutes == 5
    assert metric.source == "opencost"


@respx.mock
async def test_unallocated_and_idle_entries_are_not_attributed_to_pods():
    respx.get(URL).mock(return_value=Response(200, json=FIXTURE))

    metrics = await _collect_range()

    assert all(not metric.pod_name.startswith("__") for metric in metrics)


@respx.mock
async def test_opencost_errors_return_no_cost_rows():
    respx.get(URL).mock(return_value=Response(500, text="boom"))

    assert await _collect_range() == []


@respx.mock
async def test_five_minute_window_cost_equals_the_allocation():
    """One sample in a 5-minute window carries the window's whole allocation (±1%)."""
    respx.get(URL).mock(return_value=Response(200, json=FIXTURE))

    metric = next(m for m in await _collect_range() if m.namespace == "cache")

    share = opencost_step_share(metric, step_seconds=300, window_seconds=300)
    assert metric.total_cost * share == pytest.approx(0.00237, rel=0.01)


def test_step_share_follows_running_minutes_not_the_window_length():
    """BUG-41: a pod that ran 10 minutes of a 1-hour window is not under-costed per step."""
    from greenkube.models.metrics import CostMetric

    new_pod = CostMetric(pod_name="p", namespace="n", cpu_cost=0.0, ram_cost=0.0, total_cost=1.2, minutes=10)
    old_pod = CostMetric(pod_name="q", namespace="n", cpu_cost=0.0, ram_cost=0.0, total_cost=1.2, minutes=60)
    unknown = CostMetric(pod_name="r", namespace="n", cpu_cost=0.0, ram_cost=0.0, total_cost=1.2)

    assert opencost_step_share(new_pod, step_seconds=300, window_seconds=3600) == pytest.approx(0.5)
    assert opencost_step_share(old_pod, step_seconds=300, window_seconds=3600) == pytest.approx(1 / 12)
    assert opencost_step_share(unknown, step_seconds=300, window_seconds=3600) == pytest.approx(1 / 12)
    assert opencost_step_share(new_pod, step_seconds=900, window_seconds=3600) == 1.0
