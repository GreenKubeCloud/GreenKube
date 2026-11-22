"""The OpenCost health check tests the allocation call itself (COST-01 step 6, COST-06 step 2)."""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from greenkube.core.health import ServiceStatus, check_opencost


def _client(allocation_status: int, seen: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/healthz"):
            return httpx.Response(200, text="ok")
        return httpx.Response(allocation_status, json={"code": allocation_status, "data": [{}]})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _check(monkeypatch, allocation_status: int, token: str | None = None):
    from greenkube.core.config import config

    monkeypatch.setenv("OPENCOST_API_URL", "http://opencost:9003")
    if token:
        monkeypatch.setenv("OPENCOST_BEARER_TOKEN", token)
    else:
        monkeypatch.delenv("OPENCOST_BEARER_TOKEN", raising=False)
    config.reload()
    seen: list[httpx.Request] = []
    with patch("greenkube.core.health.get_async_http_client", return_value=_client(allocation_status, seen)):
        result = await check_opencost(config)
    return result, seen


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 503])
async def test_a_failing_allocation_call_is_not_healthy_although_healthz_answers(monkeypatch, status):
    result, _ = await _check(monkeypatch, status)
    assert result.status != ServiceStatus.HEALTHY
    assert str(status) in (result.message or "")


@pytest.mark.asyncio
async def test_a_working_allocation_call_is_healthy_and_carries_the_credentials(monkeypatch):
    result, seen = await _check(monkeypatch, 200, token="abc")
    assert result.status == ServiceStatus.HEALTHY
    allocation = [request for request in seen if request.url.path.endswith("/allocation/compute")]
    assert allocation and allocation[0].headers.get("Authorization") == "Bearer abc"


@pytest.mark.asyncio
async def test_health_check_uses_the_given_config_and_the_cluster_filter(monkeypatch):
    """Review 5: the probe reads the cfg it is given (TLS, cluster id), not the global config."""
    from greenkube.core.config import Config

    cfg = Config(OPENCOST_API_URL="http://opencost:9003", OPENCOST_VERIFY_CERTS=False, OPENCOST_CLUSTER_ID="a")
    seen: list[httpx.Request] = []
    calls: list[dict] = []

    def factory(**kwargs):
        calls.append(kwargs)
        return _client(200, seen)

    with patch("greenkube.core.health.get_async_http_client", side_effect=factory):
        result = await check_opencost(cfg)
    assert result.status == ServiceStatus.HEALTHY
    assert calls and calls[0].get("verify") is False
    allocation = [request for request in seen if request.url.path.endswith("/allocation/compute")]
    assert allocation[0].url.params.get("filter") == 'cluster:"a"'
