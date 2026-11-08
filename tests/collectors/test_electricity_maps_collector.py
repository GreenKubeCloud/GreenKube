# tests/collectors/test_electricity_maps_collector.py

from unittest.mock import patch

import httpx
import pytest
import respx
from httpx import Response

from greenkube.collectors.electricity_maps_collector import ElectricityMapsCollector

# A sample successful API response for mocking
MOCK_API_RESPONSE = {
    "zone": "FR",
    "history": [
        {"carbonIntensity": 23, "datetime": "2025-08-16T16:00:00.000Z"},
        {"carbonIntensity": 27, "datetime": "2025-08-16T17:00:00.000Z"},
    ],
}


@pytest.mark.asyncio
@respx.mock
@patch("greenkube.collectors.electricity_maps_collector.config")
async def test_collect_success(mock_config):
    """
    Tests that the collector correctly calls the history API and returns the data.
    """
    # Arrange
    # Simulate the presence of the API token
    mock_config.ELECTRICITY_MAPS_TOKEN = "test-token"

    respx.get("https://api.electricitymaps.com/v3/carbon-intensity/history?zone=FR").mock(
        return_value=Response(200, json=MOCK_API_RESPONSE)
    )

    collector = ElectricityMapsCollector()

    # Act
    result = await collector.collect(zone="FR")

    # Assert
    assert result == MOCK_API_RESPONSE["history"]


@pytest.mark.asyncio
@respx.mock
@patch("greenkube.collectors.electricity_maps_collector.config")
async def test_collect_api_error_fallback(mock_config):
    """
    An API error returns no record: defaults are applied at read time, never persisted (A8).
    """
    # Arrange
    mock_config.ELECTRICITY_MAPS_TOKEN = "test-token"
    respx.get("https://api.electricitymaps.com/v3/carbon-intensity/history?zone=FR").mock(
        side_effect=httpx.HTTPError("API Error")
    )

    collector = ElectricityMapsCollector()

    # Act
    # collect is async
    result = await collector.collect(zone="FR")

    # Assert
    assert result == []


@pytest.mark.asyncio
@patch("greenkube.collectors.electricity_maps_collector.config")
async def test_collect_no_token_fallback(mock_config):
    """
    Without a token no record is returned (A8): the zone default is computed at read time.
    """
    # Arrange
    mock_config.ELECTRICITY_MAPS_TOKEN = None

    collector = ElectricityMapsCollector()

    # Act
    result = await collector.collect(zone="FR")

    # Assert
    assert result == []


@pytest.mark.asyncio
@patch("greenkube.collectors.electricity_maps_collector.config")
async def test_collect_unknown_zone(mock_config):
    """
    Tests that the collector returns an empty list for an unknown zone without a token.
    """
    # Arrange
    mock_config.ELECTRICITY_MAPS_TOKEN = None

    collector = ElectricityMapsCollector()

    # Act
    result = await collector.collect(zone="UNKNOWN_ZONE")

    # Assert
    assert result == []


@pytest.mark.asyncio
@respx.mock
@patch("greenkube.collectors.electricity_maps_collector.config")
async def test_collect_timeout_handling(mock_config):
    """
    Tests that the collector requests are made properly (timeout handled by client factory).
    """
    mock_config.ELECTRICITY_MAPS_TOKEN = "test-token"

    route = respx.get("https://api.electricitymaps.com/v3/carbon-intensity/history?zone=FR").mock(
        return_value=Response(200, json=MOCK_API_RESPONSE)
    )

    collector = ElectricityMapsCollector()
    await collector.collect(zone="FR")

    # Verify get was called with correct headers
    assert route.called
    request = route.calls.last.request
    assert request.headers["auth-token"] == "test-token"
