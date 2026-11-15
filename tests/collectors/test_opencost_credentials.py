"""Allocation requests carry the configured OpenCost credentials (COST-06)."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from greenkube.collectors.opencost_collector import OpenCostCollector

START = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
SMALL_POD = {
    "properties": {"namespace": "ns", "pod": "p", "node": "n1"},
    "window": {"start": "2026-11-14T12:00:00Z", "end": "2026-11-14T12:05:00Z"},
    "minutes": 5,
    "cpuCoreHours": 0.00083,
    "cpuCoreRequestAverage": 0.01,
    "ramByteHours": 0,
    "cpuCost": 0.00003,
    "ramCost": 0,
    "totalCost": 0.00003,
}
NODE = {"properties": {"node": "n1"}, "cpuCoreHours": 48, "cpuCost": 1.5, "ramByteHours": 1e12, "ramCost": 0.5}


def _collector(monkeypatch, *, token=None, username=None, password=None):
    monkeypatch.setattr("greenkube.collectors.opencost_collector.config.OPENCOST_BEARER_TOKEN", token)
    monkeypatch.setattr("greenkube.collectors.opencost_collector.config.OPENCOST_USERNAME", username)
    monkeypatch.setattr("greenkube.collectors.opencost_collector.config.OPENCOST_PASSWORD", password)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.params.get("aggregate") == "node":
            return httpx.Response(200, json={"code": 200, "data": [{"n1": NODE}]})
        return httpx.Response(200, json={"code": 200, "data": [{"ns/p": SMALL_POD}]})

    collector = OpenCostCollector()
    collector._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    collector._resolved_url = "http://opencost:9003/allocation/compute"
    return collector, seen


async def _exercise(collector: OpenCostCollector) -> None:
    try:
        await collector.collect_range(START, START + timedelta(minutes=5))
        await collector.collect_pv_costs(["pv-1"])
        await collector.collect_lb_costs(["ns/lb"])
    finally:
        await collector.close()


@pytest.mark.asyncio
async def test_every_request_carries_the_bearer_token(monkeypatch):
    collector, seen = _collector(monkeypatch, token="abc")
    await _exercise(collector)
    assert len(seen) == 4  # allocation, node prices, PV, LB
    assert {request.headers.get("Authorization") for request in seen} == {"Bearer abc"}


@pytest.mark.asyncio
async def test_every_request_carries_basic_credentials(monkeypatch):
    collector, seen = _collector(monkeypatch, username="user", password="secret")
    await _exercise(collector)
    expected = "Basic " + base64.b64encode(b"user:secret").decode()
    assert len(seen) == 4
    assert {request.headers.get("Authorization") for request in seen} == {expected}


@pytest.mark.asyncio
async def test_no_credentials_no_authorization_header(monkeypatch):
    collector, seen = _collector(monkeypatch)
    await _exercise(collector)
    assert seen and all("Authorization" not in request.headers for request in seen)
