"""A multi-cluster OpenCost never merges same-named pods of different clusters (COST-10)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from greenkube.collectors.opencost_collector import (
    OPENCOST_AMBIGUOUS_CLUSTER,
    OpenCostCollector,
    OpenCostUnavailableError,
)

START = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)


def _allocation(cluster: str, cost: float) -> dict:
    return {
        "properties": {"cluster": cluster, "namespace": "monitoring", "pod": "prometheus-k8s-0", "node": "node-1"},
        "window": {"start": "2026-11-14T12:00:00Z", "end": "2026-11-14T13:00:00Z"},
        "minutes": 60,
        "cpuCost": cost,
        "ramCost": 0,
        "totalCost": cost,
    }


def _collector(monkeypatch, cluster_id, clusters):
    monkeypatch.setattr("greenkube.collectors.opencost_collector.config.OPENCOST_CLUSTER_ID", cluster_id)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        wanted = request.url.params.get("filter")
        data = {
            f"{cluster}/monitoring/prometheus-k8s-0": _allocation(cluster, cost)
            for cluster, cost in clusters.items()
            if wanted is None or wanted == f'cluster:"{cluster}"'
        }
        return httpx.Response(200, json={"code": 200, "data": [data]})

    collector = OpenCostCollector()
    collector._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    collector._resolved_url = "http://opencost:9003/allocation/compute"
    return collector, seen


@pytest.mark.asyncio
async def test_configured_cluster_id_filters_the_allocation_query(monkeypatch):
    collector, seen = _collector(monkeypatch, "a", {"a": 0.01, "b": 0.02})
    try:
        (metric,) = await collector.collect_range(START, START + timedelta(hours=1))
    finally:
        await collector.close()
    assert metric.total_cost == pytest.approx(0.01)
    assert seen[0].url.params["filter"] == 'cluster:"a"'
    assert seen[0].url.params["aggregate"] == "cluster,namespace,pod"


@pytest.mark.asyncio
async def test_several_clusters_without_an_id_are_not_merged(monkeypatch):
    collector, _ = _collector(monkeypatch, None, {"a": 0.01, "b": 0.02})
    try:
        with pytest.raises(OpenCostUnavailableError):
            await collector.collect_range(START, START + timedelta(hours=1))
    finally:
        await collector.close()
    assert OPENCOST_AMBIGUOUS_CLUSTER._value.get() == 1


@pytest.mark.asyncio
async def test_single_cluster_without_an_id_is_unchanged(monkeypatch):
    collector, seen = _collector(monkeypatch, None, {"default-cluster": 0.01})
    try:
        (metric,) = await collector.collect_range(START, START + timedelta(hours=1))
    finally:
        await collector.close()
    assert metric.total_cost == pytest.approx(0.01)
    assert "filter" not in seen[0].url.params
    assert OPENCOST_AMBIGUOUS_CLUSTER._value.get() == 0
