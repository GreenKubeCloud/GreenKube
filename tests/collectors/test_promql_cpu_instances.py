"""CPU of consecutive container instances is added, duplicates of one instance are not (COL-11).

cAdvisor's ``name`` label is the runtime container id: equal for duplicate
cgroup paths and scrape jobs of one container, different for consecutive
instances (restart, crash loop, pod recreated under the same name), whose
rates each cover their own part of the window.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from greenkube.collectors.promql import PromQLCatalog
from tests.core.fake_prometheus import FakePrometheus  # pyrefly: ignore[missing-import]

T0 = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)
BASE = {"namespace": "shop", "pod": "web-0", "container": "app", "node": "n1"}
METRIC = "container_cpu_usage_seconds_total"


async def _cores(prom: FakePrometheus, query: str) -> dict[str, float]:
    result = await prom.collect_range(T0, T0, step="300s", query=query)
    return {r["metric"].get("container", ""): float(r["values"][0][1]) for r in result}


@pytest.mark.parametrize("query_name", ["cpu_usage_by_level", "container_cpu_usage"])
async def test_consecutive_instances_are_added(query_name):
    prom = FakePrometheus()
    prom.add(METRIC, {**BASE, "name": "instance-a", "id": "/kubepods/a"}, 0.3)
    prom.add(METRIC, {**BASE, "name": "instance-b", "id": "/kubepods/b"}, 0.2)
    query = getattr(PromQLCatalog(step_seconds=300), query_name)()

    assert await _cores(prom, query) == {"app": pytest.approx(0.5)}


async def test_duplicate_cgroup_paths_of_one_instance_count_once():
    prom = FakePrometheus()
    prom.add(METRIC, {**BASE, "name": "instance-a", "id": "/kubepods/a"}, 0.3)
    prom.add(METRIC, {**BASE, "name": "instance-a", "id": "/system.slice/a"}, 0.25)

    assert await _cores(prom, PromQLCatalog(step_seconds=300).cpu_usage_by_level()) == {"app": pytest.approx(0.3)}


async def test_a_kubelet_resource_endpoint_duplicate_does_not_double():
    prom = FakePrometheus()
    prom.add(METRIC, {**BASE, "name": "instance-a", "id": "/kubepods/a", "job": "cadvisor"}, 0.3)
    # /metrics/resource: no name or id label.
    prom.add(METRIC, {**BASE, "job": "resource"}, 0.29)

    assert await _cores(prom, PromQLCatalog(step_seconds=300).cpu_usage_by_level()) == {"app": pytest.approx(0.3)}


async def test_resource_endpoint_alone_and_pod_cgroups_are_kept():
    prom = FakePrometheus()
    prom.add(METRIC, {**BASE, "job": "resource"}, 0.29)
    prom.add(METRIC, {"namespace": "shop", "pod": "dockerd-0", "container": "", "node": "n1", "id": "/kubepods/p"}, 0.4)

    assert await _cores(prom, PromQLCatalog(step_seconds=300).cpu_usage_by_level()) == {
        "app": pytest.approx(0.29),
        "": pytest.approx(0.4),
    }
