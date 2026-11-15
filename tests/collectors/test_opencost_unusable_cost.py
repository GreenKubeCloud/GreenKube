"""OpenCost allocations that carry no usable cost are not trusted (COST-05, RECON-11)."""

from datetime import datetime, timedelta, timezone

import pytest

from greenkube.collectors.opencost_collector import _allocation_to_cost_metric
from greenkube.core.config import Config
from greenkube.core.observability import OPENCOST_UNREBUILT_ALLOCATIONS
from greenkube.models.provenance import CostSource, is_cost_trusted
from tests.core.fake_prometheus import FakePrometheus  # pyrefly: ignore[missing-import]
from tests.core.range_harness import build_range_processor  # pyrefly: ignore[missing-import]

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)


def _zero_minutes(**extra):
    """Recorded live item after a gap in OpenCost's data: minutes 0, start = end, request set, cost 0."""
    item = {
        "name": "shop/web-0",
        "properties": {"cluster": "default-cluster", "node": "minikube", "namespace": "shop", "pod": "web-0"},
        "window": {"start": "2026-11-14T11:59:00Z", "end": "2026-11-14T11:59:00Z"},
        "start": "2026-11-14T11:59:00Z",
        "end": "2026-11-14T11:59:00Z",
        "minutes": 0,
        "cpuCoreHours": 0,
        "cpuCoreRequestAverage": 0.1,
        "cpuCoreUsageAverage": 0,
        "ramByteHours": 0,
        "ramByteRequestAverage": 0,
        "cpuCost": 0,
        "ramCost": 0,
        "totalCost": 0,
    }
    item.update(extra)
    return item


def test_zero_minute_allocation_with_a_request_carries_no_cost():
    metric = _allocation_to_cost_metric("shop/web-0", _zero_minutes(), NOW)
    assert metric is not None
    assert metric.source == CostSource.NONE.value


def test_zero_minute_allocation_without_request_but_with_usage_carries_no_cost():
    item = _zero_minutes(cpuCoreRequestAverage=0, cpuCoreUsageAverage=0.006)
    metric = _allocation_to_cost_metric("shop/web-0", item, NOW)
    assert metric is not None
    assert metric.source == CostSource.NONE.value


def test_sub_precision_allocation_without_node_price_is_not_trusted():
    """(c) cpuCost 0.00003 and no node price: the rounded figure is not trusted."""
    before = OPENCOST_UNREBUILT_ALLOCATIONS._value.get()
    item = _zero_minutes(minutes=1, cpuCoreHours=0.00167, cpuCost=0.00003, totalCost=0.00003)
    item["window"] = {"start": "2026-11-14T11:59:00Z", "end": "2026-11-14T12:00:00Z"}
    metric = _allocation_to_cost_metric("shop/web-0", item, NOW, node_prices={})
    assert metric is not None
    assert metric.source == CostSource.NONE.value
    assert OPENCOST_UNREBUILT_ALLOCATIONS._value.get() == before + 1


def test_allocation_above_the_precision_floor_keeps_its_opencost_cost():
    item = _zero_minutes(minutes=60, cpuCoreHours=0.1, cpuCost=0.0031611, totalCost=0.0031611)
    item["window"] = {"start": "2026-11-14T11:00:00Z", "end": "2026-11-14T12:00:00Z"}
    metric = _allocation_to_cost_metric("shop/web-0", item, NOW, node_prices={})
    assert metric is not None
    assert metric.source == CostSource.OPENCOST.value
    assert metric.total_cost == pytest.approx(0.0031611)


@pytest.mark.parametrize(
    "row",
    [
        {"cost_source": "opencost", "total_cost": 0, "cpu_usage_millicores": 6},
        {"cost_source": "opencost_derived", "total_cost": 0, "memory_usage_bytes": 1024},
    ],
)
def test_a_zero_opencost_cost_for_a_row_with_usage_is_not_trusted(row):
    """(d) is_cost_trusted rejects a zero OpenCost-family total for any row with usage or request."""
    assert not is_cost_trusted(row)


def test_a_zero_cost_for_an_idle_row_without_request_or_usage_stays_trusted():
    assert is_cost_trusted({"cost_source": "opencost", "total_cost": 0, "cpu_usage_millicores": 0})


@pytest.mark.asyncio
async def test_run_range_gives_a_zero_minute_allocation_the_modeled_reservation_cost():
    """(b) The stored row is not opencost and its cost is the modeled reservation cost, never 0."""
    start = datetime(2026, 11, 14, 11, 55, tzinfo=timezone.utc)
    metric = _allocation_to_cost_metric("shop/web-0", _zero_minutes(), start)

    class _OpenCost:
        last_status = "ok"

        async def collect_range(self, start, end):
            return [metric]

    class _Stored:
        async def read_combined_metrics(self, *args, **kwargs):
            return []

    prom = FakePrometheus()
    prom.add(
        "container_cpu_usage_seconds_total",
        {"namespace": "shop", "pod": "web-0", "container": "app", "node": "n1"},
        0.05,
    )
    cfg = Config(PROMETHEUS_QUERY_RANGE_STEP="5m", KUBERNETES_METRICS_API_ENABLED=False, CLOUD_PROVIDER="aws")
    processor = build_range_processor(cfg, _Stored(), prom, _OpenCost(), pods=[("shop", "web-0", "app")])
    rows = await processor.run_range(start, start + timedelta(minutes=5))
    (row,) = [row for row in rows if row.container_name is None]
    assert row.cost_source not in {CostSource.OPENCOST.value, CostSource.OPENCOST_DERIVED.value}
    assert row.total_cost and row.total_cost > 0
