"""PersistentVolume and LoadBalancer costs read from OpenCost's real allocation fields (COST-03)."""

from __future__ import annotations

import httpx
import pytest

from greenkube.collectors.lb_collector import OrphanedLoadBalancer, enrich_orphaned_lb_costs
from greenkube.collectors.opencost_collector import OpenCostCollector
from greenkube.collectors.pv_collector import OrphanedPV, enrich_orphaned_pv_costs

GIB = 1024**3
#: The released kind volume: 1 GiB for 5.18 h at OpenCost's default storage price.
PV_BYTES = 1 * GIB
PV_HOURS = 5.18
PV_COST = 0.001418


def _allocation(name: str, **fields) -> dict:
    base = {
        "name": name,
        "properties": {"cluster": "default-cluster", "namespace": name.split("/")[0]},
        "window": {"start": "2026-11-08T00:00:00Z", "end": "2026-11-15T00:00:00Z"},
        "minutes": 10080,
        "totalCost": 0,
        "pvs": None,
        "lbAllocations": None,
    }
    base.update(fields)
    return base


PAYLOAD = {
    "code": 200,
    "data": [
        {
            "__unmounted__": _allocation(
                "__unmounted__",
                pvs={
                    "cluster=default-cluster:name=gk-waste-csi-released": {
                        "byteHours": PV_BYTES * PV_HOURS,
                        "cost": PV_COST,
                        "providerID": "",
                        "adjustment": 0,
                    },
                    "cluster=default-cluster:name=free-pv": {
                        "byteHours": PV_BYTES * 24,
                        "cost": 0,
                        "providerID": "",
                        "adjustment": 0,
                    },
                },
            ),
            "shop": _allocation(
                "shop",
                lbAllocations={
                    "default-cluster/shop/web-lb": {
                        "service": "shop/web-lb",
                        "cost": 0.42,
                        "private": False,
                        "ip": "203.0.113.7",
                        "hours": 168,
                        "adjustment": 0,
                    }
                },
            ),
            "other": _allocation(
                "other",
                lbAllocations={
                    "default-cluster/other/web-lb": {
                        "service": "other/web-lb",
                        "cost": 9.99,
                        "private": False,
                        "ip": "203.0.113.8",
                        "hours": 168,
                        "adjustment": 0,
                    }
                },
            ),
        }
    ],
}


class _Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/allocation/compute"):
            return httpx.Response(200, json=PAYLOAD)
        return httpx.Response(404, text="not found")


def _collector(recorder: _Recorder) -> OpenCostCollector:
    collector = OpenCostCollector()
    collector._client = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    collector._resolved_url = "http://opencost:9003/allocation/compute"
    return collector


@pytest.mark.asyncio
async def test_pv_cost_is_read_from_the_pvs_map_of_the_allocation_endpoint():
    """(a), (f): the volume's cost over its hours comes from ``pvs``; only /allocation/compute is queried."""
    recorder = _Recorder()
    collector = _collector(recorder)
    try:
        costs = await collector.collect_pv_costs(["gk-waste-csi-released", "free-pv", "unknown-pv"])
    finally:
        await collector.close()
    assert costs["gk-waste-csi-released"] == pytest.approx(PV_COST, rel=0.01)
    assert costs["free-pv"] == 0.0
    assert "unknown-pv" not in costs
    assert {request.url.path for request in recorder.requests} == {"/allocation/compute"}
    params = recorder.requests[0].url.params
    assert params["aggregate"] == "namespace"
    assert params["accumulate"] == "true"
    assert "aggregateBy" not in params


@pytest.mark.asyncio
async def test_orphan_pv_is_annualized_by_the_hours_it_existed():
    """(a) 0.001418 over 5.18 h annualizes to about 2.40 per year; (c) a zero stays an observed zero."""
    recorder = _Recorder()
    collector = _collector(recorder)
    volumes = [
        OrphanedPV(name="gk-waste-csi-released", phase="Released", capacity_bytes=PV_BYTES),
        OrphanedPV(name="free-pv", phase="Released", capacity_bytes=PV_BYTES),
    ]
    try:
        await enrich_orphaned_pv_costs(volumes, opencost=collector)
    finally:
        await collector.close()
    assert volumes[0].annual_cost == pytest.approx(PV_COST / PV_HOURS * 8760, rel=0.01)
    assert volumes[0].annual_cost == pytest.approx(2.40, rel=0.01)
    assert volumes[1].annual_cost == 0.0


@pytest.mark.asyncio
async def test_lb_cost_is_read_from_lb_allocations_with_an_exact_namespace_match():
    """(b) shop/web-lb costs 0.42 over 7 days (21.9 per year); other/web-lb is never attached to it."""
    recorder = _Recorder()
    collector = _collector(recorder)
    services = [
        OrphanedLoadBalancer(name="web-lb", namespace="shop"),
        OrphanedLoadBalancer(name="web-lb", namespace="x"),
    ]
    try:
        costs = await collector.collect_lb_costs(["shop/web-lb", "x/web-lb"])
        await enrich_orphaned_lb_costs(services, opencost=collector)
    finally:
        await collector.close()
    assert costs == {"shop/web-lb": pytest.approx(0.42)}
    assert services[0].annual_cost == pytest.approx(21.9, rel=0.01)
    assert services[1].annual_cost is None
    assert {request.url.path for request in recorder.requests} == {"/allocation/compute"}
