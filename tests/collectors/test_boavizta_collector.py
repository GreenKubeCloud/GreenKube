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
