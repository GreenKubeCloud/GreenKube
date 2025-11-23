# tests/collectors/test_electricity_maps_collector.py

from unittest.mock import MagicMock, patch

import requests

from greenkube.collectors.electricity_maps_collector import ElectricityMapsCollector

# A sample successful API response for mocking
MOCK_API_RESPONSE = {
    "zone": "FR",
    "history": [
        {"carbonIntensity": 23, "datetime": "2025-08-16T16:00:00.000Z"},
        {"carbonIntensity": 27, "datetime": "2025-08-16T17:00:00.000Z"},
    ],
}


@patch("requests.get")
@patch("greenkube.collectors.electricity_maps_collector.config")
def test_collect_success(mock_config, mock_get):
    """
    Tests that the collector correctly calls the API and returns the data.
    """
    # Arrange
    # Simulate the presence of the API token
    mock_config.ELECTRICITY_MAPS_TOKEN = "test-token"

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = MOCK_API_RESPONSE
    mock_get.return_value = mock_response

    collector = ElectricityMapsCollector()

    # Act
    result = collector.collect(zone="FR")

    # Assert
    mock_get.assert_called_once_with(
        "https://api.electricitymaps.com/v3/carbon-intensity/history?zone=FR",
        headers={"auth-token": "test-token"},
    )
    assert result == MOCK_API_RESPONSE["history"]


@patch("requests.get")
@patch("greenkube.collectors.electricity_maps_collector.config")
def test_collect_api_error_fallback(mock_config, mock_get):
    """
    Tests that the collector returns the default value in case of an API error.
    """
    # Arrange
    mock_config.ELECTRICITY_MAPS_TOKEN = "test-token"
    mock_get.side_effect = requests.exceptions.RequestException("API Error")

    collector = ElectricityMapsCollector()

    # Act
    result = collector.collect(zone="FR")

    # Assert
    assert len(result) == 1
    assert result[0]["zone"] == "FR"
    assert result[0]["carbonIntensity"] == 26  # Default for FR
    assert result[0]["isEstimated"] is True


@patch("greenkube.collectors.electricity_maps_collector.config")
def test_collect_no_token_fallback(mock_config):
    """
    Tests that the collector returns the default value if no token is configured.
    """
    # Arrange
    mock_config.ELECTRICITY_MAPS_TOKEN = None

    collector = ElectricityMapsCollector()

    # Act
    result = collector.collect(zone="FR")

    # Assert
    assert len(result) == 1
    assert result[0]["zone"] == "FR"
    assert result[0]["carbonIntensity"] == 26  # Default for FR
    assert result[0]["isEstimated"] is True


@patch("greenkube.collectors.electricity_maps_collector.config")
def test_collect_unknown_zone(mock_config):
    """
    Tests that the collector returns an empty list for an unknown zone without a token.
    """
    # Arrange
    mock_config.ELECTRICITY_MAPS_TOKEN = None

    collector = ElectricityMapsCollector()

    # Act
    result = collector.collect(zone="UNKNOWN_ZONE")

    # Assert
    assert result == []
