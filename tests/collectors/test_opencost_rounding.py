"""OpenCost rounds costs to 5 decimals: a small pod's per-step cost reads 0 (live Minikube e2e).

Fixtures are allocations returned by OpenCost on a Minikube node for a
one-minute window (``aggregate=namespace,pod``) and for the node over 24 hours
(``aggregate=node``). OpenCost's default pricing is 0.031611 per core-hour and
0.004237 per GiB-hour.

A rounded zero is not a missing price: the cost is rebuilt from the
allocation's resource-hours at OpenCost's own node unit prices when OpenCost's
figures agree with it within their rounding, and stored as
``opencost_derived`` (trusted). A zero that rounding cannot explain, a NaN or
a missing total stays untrusted.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from greenkube.collectors.opencost_collector import (
    NodeUnitPrices,
    OpenCostCollector,
    _allocation_to_cost_metric,
    node_unit_prices,
)
from greenkube.core.metric_assembler import cost_fields
from greenkube.core.optimization.verifier import cost_is_trusted, window_rates
from greenkube.models.metrics import CombinedMetric
from greenkube.models.provenance import is_cost_trusted
from greenkube.verify.model import TimeWindow
from greenkube.verify.series import hourly_buckets, quantity_series

GIB = 1024**3
CPU_PRICE = 0.031611
RAM_PRICE_PER_BYTE = 0.004237 / GIB
NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)

#: ``aggregate=node`` over the 24 hours before the window (live Minikube).
NODE_ALLOCATION = {
    "properties": {"cluster": "default-cluster", "node": "minikube"},
    "minutes": 1440,
    "cpuCoreHours": 78.25158,
    "ramByteHours": 152248341845.33337,
    "cpuCost": 2.47361,
    "ramCost": 0.60077,
    "totalCost": 3.07438,
}
UNMOUNTED_NODE_ALLOCATION = {
    "properties": {"cluster": "default-cluster", "container": "__unmounted__"},
    "minutes": 1440,
    "cpuCoreHours": 0,
    "ramByteHours": 0,
    "cpuCost": 0,
    "ramCost": 0,
    "totalCost": 0,
}


def _pod(name: str, cpu_request: float, ram_byte_hours: float, cpu_core_hours: float, cpu, ram, total, **extra):
    item = {
        "name": f"greenkube-e2e-apps/{name}",
        "properties": {
            "cluster": "default-cluster",
            "node": "minikube",
            "namespace": "greenkube-e2e-apps",
            "pod": name,
        },
        "window": {"start": "2026-11-14T11:59:00Z", "end": "2026-11-14T12:00:00Z"},
        "minutes": 1,
        "cpuCoreHours": cpu_core_hours,
        "cpuCoreRequestAverage": cpu_request,
        "cpuCoreUsageAverage": 0,
        "ramByteHours": ram_byte_hours,
        "ramByteRequestAverage": ram_byte_hours * 60,
        "cpuCost": cpu,
        "ramCost": ram,
        "pvCost": 0,
        "networkCost": 0,
        "loadBalancerCost": 0,
        "gpuCost": 0,
        "totalCost": total,
    }
    item.update(extra)
    return item


def small_pod(**extra):
    """5m CPU / 8 MiB for one minute: true cost 3.2e-6, reported 0 (live ``prod-lb-backend``)."""
    return _pod("prod-lb-backend-79f7795b6d-r8ptc", 0.005, 139810.13333, 8e-05, 0, 0, 0, **extra)


def tiny_pod(**extra):
    """10m CPU / 16 MiB for one minute: true cost 6.4e-6, reported 1e-5 (live ``oversized-worker``)."""
    return _pod("oversized-worker-c7bb6894f-ggxgn", 0.01, 279620.26667, 0.00017, 1e-05, 0, 1e-05, **extra)


def large_pod(**extra):
    """2 cores / 4 GiB for one minute: true cost 1.1e-3; a reported 0 is a missing price."""
    return _pod("big-api-0", 2.0, 4 * GIB / 60, 0.03333, 0, 0, 0, **extra)


def true_cost(cpu_request: float, ram_byte_hours: float, minutes: float = 1.0) -> float:
    return cpu_request * minutes / 60 * CPU_PRICE + ram_byte_hours * RAM_PRICE_PER_BYTE


RATES = node_unit_prices({"minikube": NODE_ALLOCATION, "": UNMOUNTED_NODE_ALLOCATION})


def _metric(item, rates=RATES):
    metric = _allocation_to_cost_metric(item["name"], item, NOW, node_prices=rates)
    assert metric is not None
    return metric


def _row(item, rates=RATES, cpu_millicores: int = 5, memory_bytes: int = 8 * 1024**2):
    return {
        **cost_fields(_metric(item, rates), 1.0, 0.0),
        "cpu_request": cpu_millicores,
        "memory_request": memory_bytes,
    }


def test_node_unit_prices_are_opencost_prices() -> None:
    prices = RATES["minikube"]
    assert prices.cpu_core_hour == pytest.approx(CPU_PRICE, rel=1e-4)
    assert prices.ram_byte_hour == pytest.approx(RAM_PRICE_PER_BYTE, rel=1e-4)
    # The unmounted-volume entry has no node and no cost: no prices.
    assert set(RATES) == {"minikube"}


def test_a_small_pod_rounded_to_zero_is_trusted_with_its_derived_cost() -> None:
    row = _row(small_pod())
    assert row["cost_source"] == "opencost_derived"
    assert row["total_cost"] == pytest.approx(true_cost(0.005, 139810.13333), rel=0.01)
    assert row["total_cost"] > 0
    assert row["cost_cpu"] + row["cost_ram"] == pytest.approx(row["total_cost"])
    assert is_cost_trusted(row)
    assert cost_is_trusted(CombinedMetric.model_validate({"pod_name": "p", "namespace": "n", **_row(small_pod())}))


@pytest.mark.parametrize(
    "values",
    [
        {"totalCost": float("nan")},
        {"totalCost": "NaN"},
        {"cpuCost": float("nan")},
        {"ramCost": float("inf")},
        {"totalCost": None, "cpuCost": None},
        {"totalCost": -1e-05},
    ],
)
def test_a_nan_missing_or_negative_cost_stays_untrusted_even_with_node_prices(values) -> None:
    row = _row(small_pod(**values))
    assert row["cost_source"] != "opencost_derived"
    assert not is_cost_trusted(row)


def test_a_large_pod_with_a_zero_cost_stays_untrusted() -> None:
    row = _row(large_pod(), cpu_millicores=2000, memory_bytes=4 * GIB)
    assert row["cost_source"] == "none"
    assert not is_cost_trusted(row)


def test_a_rounded_zero_without_node_prices_stays_untrusted() -> None:
    assert _row(small_pod(), rates={})["cost_source"] == "none"
    unknown_node = small_pod(properties={"namespace": "greenkube-e2e-apps", "pod": "prod-lb-backend-79f7795b6d-r8ptc"})
    assert _row(unknown_node)["cost_source"] == "none"


def test_a_zero_from_a_node_without_a_price_stays_untrusted() -> None:
    unpriced = node_unit_prices({"minikube": {**NODE_ALLOCATION, "cpuCost": 0, "ramCost": 0, "totalCost": 0}})
    assert _row(small_pod(), rates=unpriced)["cost_source"] == "none"


def test_a_cost_above_the_precision_floor_keeps_opencost_figures() -> None:
    item = large_pod(cpuCost=0.00105, ramCost=0.00028, totalCost=0.00133)
    metric = _metric(item)
    assert metric.source == "opencost"
    assert metric.total_cost == pytest.approx(0.00133)


def test_rounding_neither_creates_nor_erases_a_saving() -> None:
    """10m -> 5m CPU halves the CPU cost. OpenCost reads 1e-5 -> 0 (a 100% "saving"); derived costs keep the ratio."""
    before = _metric(tiny_pod())
    after = _metric(small_pod(ramByteHours=279620.26667, ramByteRequestAverage=279620.26667 * 60))
    assert before.source == after.source == "opencost_derived"
    true_before = true_cost(0.01, 279620.26667)
    true_after = true_cost(0.005, 279620.26667)
    measured = (before.total_cost - after.total_cost) / before.total_cost
    expected = (true_before - true_after) / true_before
    assert measured == pytest.approx(expected, abs=0.01)
    assert before.total_cost == pytest.approx(true_before, rel=0.01)


def _derived_rows(window: TimeWindow, step: timedelta) -> list[CombinedMetric]:
    fields = cost_fields(_metric(small_pod()), 1.0, 0.0)
    rows = []
    moment = window.start
    while moment < window.end:
        rows.append(
            CombinedMetric(
                pod_name="prod-lb-backend-79f7795b6d-r8ptc",
                namespace="greenkube-e2e-apps",
                timestamp=moment,
                duration_seconds=int(step.total_seconds()),
                cpu_request=5,
                memory_request=8 * 1024**2,
                usage_source="prometheus",
                **fields,
            )
        )
        moment += step
    return rows


def test_verifier_counts_small_pod_samples_as_trusted() -> None:
    window = TimeWindow(start=NOW - timedelta(hours=1), end=NOW)
    rows = _derived_rows(window, timedelta(minutes=1))
    rates = window_rates(rows, 60)
    assert rates.cost_steps == pytest.approx(60)
    assert rates.cost_per_hour == pytest.approx(true_cost(0.005, 139810.13333) * 60, rel=0.01)


def test_change_measurement_counts_small_pod_samples_as_trusted() -> None:
    window = TimeWindow(start=NOW - timedelta(hours=2), end=NOW)
    rows = _derived_rows(window, timedelta(minutes=1))
    series = quantity_series(hourly_buckets(rows, window, 60), "allocated_cost")
    assert len(series) == 2
    for value in series.values():
        assert value == pytest.approx(true_cost(0.005, 139810.13333) * 60, rel=0.01)


def _client(responses):
    client = AsyncMock()
    calls = []

    async def get(url, params=None):
        calls.append(dict(params or {}))
        response = MagicMock()
        response.raise_for_status = MagicMock()
        response.json.return_value = {"data": [responses[params.get("aggregate")]]}
        return response

    client.get.side_effect = get
    client.is_closed = False
    return client, calls


@pytest.mark.asyncio
async def test_collect_range_queries_node_prices_once_per_hour_and_derives_small_pods() -> None:
    responses = {
        "cluster,namespace,pod": {"greenkube-e2e-apps/prod-lb-backend": small_pod()},
        "node": {"minikube": NODE_ALLOCATION},
    }
    client, calls = _client(responses)
    with patch("greenkube.collectors.opencost_collector.get_async_http_client", return_value=client):
        collector = OpenCostCollector()
        with patch.object(collector, "_resolve_url", AsyncMock(return_value="http://opencost/allocation/compute")):
            first = await collector.collect_range(NOW - timedelta(minutes=1), NOW)
            second = await collector.collect_range(NOW, NOW + timedelta(minutes=1))

    assert [metric.source for metric in first + second] == ["opencost_derived", "opencost_derived"]
    assert first[0].total_cost == pytest.approx(true_cost(0.005, 139810.13333), rel=0.01)
    node_calls = [params for params in calls if params.get("aggregate") == "node"]
    assert len(node_calls) == 1
    end = int(NOW.timestamp())
    assert node_calls[0]["window"] == f"{end - 86400},{end}"
    assert node_calls[0]["accumulate"] == "true"


@pytest.mark.asyncio
async def test_collect_skips_the_node_query_when_no_cost_is_below_precision() -> None:
    responses = {"cluster,namespace,pod": {"prod/big": large_pod(cpuCost=0.00105, ramCost=0.00028, totalCost=0.00133)}}
    client, calls = _client(responses)
    with patch("greenkube.collectors.opencost_collector.get_async_http_client", return_value=client):
        collector = OpenCostCollector()
        with patch.object(collector, "_resolve_url", AsyncMock(return_value="http://opencost/allocation/compute")):
            metrics = await collector.collect_range(NOW - timedelta(minutes=1), NOW)
    assert [metric.source for metric in metrics] == ["opencost"]
    assert all(params.get("aggregate") != "node" for params in calls)


@pytest.mark.asyncio
async def test_a_failed_node_price_query_fails_closed() -> None:
    client = AsyncMock()
    client.is_closed = False

    async def get(url, params=None):
        response = MagicMock()
        if (params or {}).get("aggregate") == "node":
            response.json.return_value = {"code": 500}
        else:
            response.json.return_value = {"data": [{"greenkube-e2e-apps/prod-lb-backend": small_pod()}]}
        response.raise_for_status = MagicMock()
        return response

    client.get.side_effect = get
    with patch("greenkube.collectors.opencost_collector.get_async_http_client", return_value=client):
        collector = OpenCostCollector()
        with patch.object(collector, "_resolve_url", AsyncMock(return_value="http://opencost/allocation/compute")):
            metrics = await collector.collect_range(NOW - timedelta(minutes=1), NOW)
    assert [metric.source for metric in metrics] == ["none"]


def test_node_unit_prices_need_a_precise_node_cost() -> None:
    fresh = {**NODE_ALLOCATION, "cpuCoreHours": 0.01, "cpuCost": 0.00032, "ramByteHours": 1e6, "ramCost": 0.0}
    prices = node_unit_prices({"minikube": fresh})
    assert prices.get("minikube", NodeUnitPrices(None, None)).cpu_core_hour is None


@pytest.mark.asyncio
async def test_node_prices_are_re_read_when_a_node_had_no_price_yet() -> None:
    """A fresh cluster: the first node query has no precise price; the next window re-reads it."""
    fresh = {**NODE_ALLOCATION, "cpuCoreHours": 0.01, "cpuCost": 0.00032, "ramByteHours": 1e6, "ramCost": 0.0}
    answers = [{"minikube": fresh}, {"minikube": NODE_ALLOCATION}]
    client = AsyncMock()
    client.is_closed = False

    async def get(url, params=None):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        if (params or {}).get("aggregate") == "node":
            response.json.return_value = {"data": [answers.pop(0)]}
        else:
            response.json.return_value = {"data": [{"greenkube-e2e-apps/prod-lb-backend": small_pod()}]}
        return response

    client.get.side_effect = get
    with patch("greenkube.collectors.opencost_collector.get_async_http_client", return_value=client):
        collector = OpenCostCollector()
        with patch.object(collector, "_resolve_url", AsyncMock(return_value="http://opencost/allocation/compute")):
            # Both windows end in the same hour of collection time.
            first = await collector.collect_range(NOW, NOW + timedelta(minutes=1))
            second = await collector.collect_range(NOW + timedelta(minutes=1), NOW + timedelta(minutes=2))
    assert [metric.source for metric in first + second] == ["none", "opencost_derived"]
    assert answers == []


def _node_answers_client(answers: list[dict]):
    client = AsyncMock()
    client.is_closed = False

    async def get(url, params=None):
        response = MagicMock()
        response.raise_for_status = MagicMock()
        if (params or {}).get("aggregate") == "node":
            response.json.return_value = {"data": [answers.pop(0)]}
        else:
            response.json.return_value = {"data": [{"greenkube-e2e-apps/prod-lb-backend": small_pod()}]}
        return response

    client.get.side_effect = get
    return client


@pytest.mark.asyncio
async def test_a_node_without_a_ram_price_yet_is_re_read_within_the_hour() -> None:
    """A fresh OpenCost: the node's CPU cost is precise but its RAM cost is not yet (e2e, 2026-11-22).

    The partial price is not kept for the hour: every small pod would stay
    untrusted until the next hour. It is reused for a few minutes, then re-read.
    """
    partial = {**NODE_ALLOCATION, "ramCost": 0.00061}
    answers = [{"minikube": partial}, {"minikube": NODE_ALLOCATION}]
    client = _node_answers_client(answers)
    with patch("greenkube.collectors.opencost_collector.get_async_http_client", return_value=client):
        collector = OpenCostCollector()
        with patch.object(collector, "_resolve_url", AsyncMock(return_value="http://opencost/allocation/compute")):
            first = await collector.collect_range(NOW, NOW + timedelta(minutes=1))
            soon = await collector.collect_range(NOW + timedelta(minutes=1), NOW + timedelta(minutes=2))
            later = await collector.collect_range(NOW + timedelta(minutes=6), NOW + timedelta(minutes=7))
    assert node_unit_prices({"minikube": partial})["minikube"].ram_byte_hour is None
    assert [metric.source for metric in first + soon + later] == ["none", "none", "opencost_derived"]
    assert answers == []


@pytest.mark.asyncio
async def test_complete_node_prices_are_still_read_once_per_hour() -> None:
    answers = [{"minikube": NODE_ALLOCATION}]
    client = _node_answers_client(answers)
    with patch("greenkube.collectors.opencost_collector.get_async_http_client", return_value=client):
        collector = OpenCostCollector()
        with patch.object(collector, "_resolve_url", AsyncMock(return_value="http://opencost/allocation/compute")):
            for minute in (0, 6, 12):
                metrics = await collector.collect_range(
                    NOW + timedelta(minutes=minute), NOW + timedelta(minutes=minute + 1)
                )
                assert [metric.source for metric in metrics] == ["opencost_derived"]
    assert answers == []
