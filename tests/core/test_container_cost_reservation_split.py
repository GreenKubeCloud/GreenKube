"""The pod cost is split between its containers by reservation, not by usage (COST-02, IMP-02)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from greenkube.core.metric_assembler import MetricAssembler
from greenkube.core.prometheus_resource_mapper import PodResourceMaps
from greenkube.impact.recompute import split_to_containers
from greenkube.models.metrics import CombinedMetric, PodMetric

MIB = 1024**2
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)


def _pod_metrics() -> list[PodMetric]:
    return [
        PodMetric(pod_name="api-0", namespace="shop", container_name="app", cpu_request=1000, memory_request=512 * MIB),
        PodMetric(pod_name="api-0", namespace="shop", container_name="proxy", cpu_request=100, memory_request=64 * MIB),
    ]


def _maps() -> PodResourceMaps:
    return PodResourceMaps(
        container_cpu_usage_map={("shop", "api-0", "app"): 50, ("shop", "api-0", "proxy"): 50},
        container_memory_usage_map={("shop", "api-0", "app"): 100 * MIB, ("shop", "api-0", "proxy"): 100 * MIB},
    )


def _pod(cost_source: str = "opencost") -> CombinedMetric:
    return CombinedMetric(
        pod_name="api-0",
        namespace="shop",
        node="n1",
        timestamp=NOW,
        total_cost=1.5,
        cost_cpu=1.0,
        cost_ram=0.4,
        cost_storage=0.1,
        co2e_grams=10.0,
        joules=1000.0,
        cpu_request=1100,
        memory_request=576 * MIB,
        cost_source=cost_source,
        usage_source="prometheus",
    )


def test_cpu_and_ram_cost_follow_the_reservation_of_each_container():
    """(a) app (1000m request, 50m usage) gets 0.909 of the CPU cost, proxy (100m, 50m) 0.091."""
    rows = {
        row.container_name: row for row in MetricAssembler.assemble_container_metrics([_pod()], _pod_metrics(), _maps())
    }
    assert rows["app"].cost_cpu == pytest.approx(1.0 * 1000 / 1100, rel=0.01)
    assert rows["proxy"].cost_cpu == pytest.approx(1.0 * 100 / 1100, rel=0.01)
    # RAM: max(request, usage) = 512 MiB and 100 MiB.
    assert rows["app"].cost_ram == pytest.approx(0.4 * 512 / 612, rel=0.01)
    assert rows["proxy"].cost_ram == pytest.approx(0.4 * 100 / 612, rel=0.01)
    for field in ("total_cost", "cost_cpu", "cost_ram", "cost_storage"):
        assert sum(getattr(row, field) for row in rows.values()) == pytest.approx(getattr(_pod(), field))


def test_recompute_splits_container_rows_like_collection():
    """(e) recompute re-splits a modeled pod row onto the same container rows as collection."""
    # A modeled pod row carries CPU and RAM cost only.
    pod = _pod("pricing_catalog").model_copy(update={"total_cost": 1.4, "cost_storage": None})
    collected = MetricAssembler.assemble_container_metrics([pod], _pod_metrics(), _maps())
    recomputed = split_to_containers(pod, collected)
    for before, after in zip(collected, recomputed, strict=True):
        assert after.cost_cpu == pytest.approx(before.cost_cpu)
        assert after.cost_ram == pytest.approx(before.cost_ram)
        assert after.total_cost == pytest.approx(before.total_cost)


def test_equal_split_only_when_neither_request_nor_usage_is_known():
    metrics = [
        PodMetric(pod_name="api-0", namespace="shop", container_name="a", cpu_request=0, memory_request=0),
        PodMetric(pod_name="api-0", namespace="shop", container_name="b", cpu_request=0, memory_request=0),
    ]
    maps = PodResourceMaps(
        container_cpu_usage_map={("shop", "api-0", "a"): 0, ("shop", "api-0", "b"): 0},
        container_memory_usage_map={("shop", "api-0", "a"): 0, ("shop", "api-0", "b"): 0},
    )
    rows = MetricAssembler.assemble_container_metrics([_pod()], metrics, maps)
    assert [row.cost_cpu for row in rows] == pytest.approx([0.5, 0.5])
