from unittest.mock import AsyncMock, MagicMock

import pytest
import respx
from httpx import Response

from greenkube.collectors.boavizta_collector import BoaviztaCollector
from greenkube.core.config import config


@pytest.fixture
def collector():
    return BoaviztaCollector()


@pytest.mark.asyncio
async def test_get_server_impact_cloud_instance(collector):
    provider = "aws"
    instance_type = "m5.large"

    mock_response = {"impacts": {"gwp": {"manufacture": 1500.0, "unit": "kgCO2eq"}}}

    with respx.mock(base_url=config.BOAVIZTA_API_URL) as respx_mock:
        respx_mock.get("/v1/cloud/instance").mock(return_value=Response(200, json=mock_response))

        result = await collector.get_server_impact(provider=provider, instance_type=instance_type, verbose=True)

        assert result is not None
        assert result.impacts.gwp.manufacture == 1500.0


@pytest.mark.asyncio
async def test_get_server_impact_archetype(collector):
    archetype = "dell_r740"

    mock_response = {"impacts": {"gwp": {"manufacture": 2000.0, "unit": "kgCO2eq"}}}

    with respx.mock(base_url=config.BOAVIZTA_API_URL) as respx_mock:
        respx_mock.get("/v1/server/").mock(return_value=Response(200, json=mock_response))

        result = await collector.get_server_impact(model=archetype, verbose=True)

        assert result is not None
        assert result.impacts.gwp.manufacture == 2000.0


@pytest.mark.asyncio
async def test_get_server_impact_error(collector):
    provider = "aws"
    instance_type = "unknown"

    with respx.mock(base_url=config.BOAVIZTA_API_URL) as respx_mock:
        respx_mock.get("/v1/cloud/instance").mock(return_value=Response(404, json={"detail": "Not found"}))

        result = await collector.get_server_impact(provider=provider, instance_type=instance_type)

        assert result is None


@pytest.mark.asyncio
async def test_collect_returns_empty_and_missing_parameters_skip_lookup(collector):
    assert await collector.collect() == []
    assert await collector.get_server_impact() is None


@pytest.mark.asyncio
async def test_cloud_instance_unexpected_error_returns_none(collector):
    client = MagicMock()
    client.get = AsyncMock(side_effect=ValueError("invalid json"))
    collector._get_client = AsyncMock(return_value=client)

    assert await collector.get_server_impact(provider="aws", instance_type="m5.large") is None


@pytest.mark.asyncio
async def test_archetype_http_and_unexpected_errors_return_none(collector):
    with respx.mock(base_url=config.BOAVIZTA_API_URL) as respx_mock:
        respx_mock.get("/v1/server/").mock(return_value=Response(500, json={"detail": "boom"}))
        assert await collector.get_server_impact(model="dell_r740") is None

    client = MagicMock()
    client.get = AsyncMock(side_effect=ValueError("bad response"))
    collector._get_client = AsyncMock(return_value=client)
    assert await collector.get_server_impact(model="dell_r740") is None


@pytest.mark.asyncio
async def test_client_reuse_and_close(collector):
    first_client = await collector._get_client()
    second_client = await collector._get_client()
    assert second_client is first_client

    await collector.close()
    assert collector._client is None

    closed_client = MagicMock()
    closed_client.is_closed = True
    closed_client.aclose = AsyncMock()
    collector._client = closed_client
    await collector.close()
    closed_client.aclose.assert_not_awaited()
