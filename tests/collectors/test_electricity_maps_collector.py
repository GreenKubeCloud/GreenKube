# tests/collectors/test_electricity_maps_collector.py

import requests
from unittest.mock import patch, MagicMock
from src.greenkube.collectors.electricity_maps_collector import ElectricityMapsCollector

# A sample successful API response for mocking
MOCK_API_RESPONSE = {
  "zone": "FR",
  "history": [
    {"carbonIntensity": 23, "datetime": "2025-08-16T16:00:00.000Z"},
    {"carbonIntensity": 27, "datetime": "2025-08-16T17:00:00.000Z"}
  ]
}

@patch('requests.get')
@patch('src.greenkube.collectors.electricity_maps_collector.config')
def test_collect_success(mock_config, mock_get):
    """
    Teste que le collecteur appelle correctement l'API et retourne les données.
    """
    # Arrange
    # On simule la présence du token API
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
        headers={"auth-token": "test-token"}
    )
    assert result == MOCK_API_RESPONSE["history"]

@patch('requests.get')
@patch('src.greenkube.collectors.electricity_maps_collector.config')
def test_collect_api_error(mock_config, mock_get):
    """
    Teste que le collecteur retourne une liste vide en cas d'erreur API.
    """
    # Arrange
    mock_config.ELECTRICITY_MAPS_TOKEN = "test-token"
    mock_get.side_effect = requests.exceptions.RequestException("API Error")
    
    collector = ElectricityMapsCollector()

    # Act
    result = collector.collect(zone="FR")

    # Assert
    assert result == []

