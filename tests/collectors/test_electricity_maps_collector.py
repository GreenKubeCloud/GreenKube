# tests/collectors/test_electricity_maps_collector.py

import pytest
import requests
from unittest.mock import patch, MagicMock
from src.greenkube.collectors.electricity_maps_collector import ElectricityMapsCollector
from src.greenkube.core.db import DatabaseManager

# A sample successful API response for mocking
MOCK_API_RESPONSE = {
  "zone": "FR",
  "history": [
    {
      "carbonIntensity": 23,
      "datetime": "2025-08-16T16:00:00.000Z",
      "updatedAt": "2025-08-16T18:43:16.594Z",
      "createdAt": "2025-08-13T16:43:55.718Z",
      "emissionFactorType": "lifecycle",
      "isEstimated": False,
      "estimationMethod": None
    },
    {
      "carbonIntensity": 27,
      "datetime": "2025-08-16T17:00:00.000Z",
      "updatedAt": "2025-08-16T19:38:32.161Z",
      "createdAt": "2025-08-13T17:43:43.588Z",
      "emissionFactorType": "lifecycle",
      "isEstimated": False,
      "estimationMethod": None
    }
  ]
}

@patch('requests.get')
def test_collect_success(mock_get, test_db_connection):
    """
    Tests the happy path: the collector successfully fetches data from the API
    and saves it to the database.
    """
    # Arrange
    # Configure the mock to return a successful response
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = MOCK_API_RESPONSE
    mock_get.return_value = mock_response

    # Initialize the schema in our in-memory test database
    db_manager = DatabaseManager()
    db_manager.connection = test_db_connection
    db_manager.init_schema()

    collector = ElectricityMapsCollector(zone="FR")
    collector.db_connection = test_db_connection

    # Act
    collector.collect()

    # Assert
    # Verify that requests.get was called correctly
    mock_get.assert_called_once()
    # Verify that the data was inserted into the database
    cursor = test_db_connection.cursor()
    cursor.execute("SELECT * FROM carbon_intensity")
    results = cursor.fetchall()
    assert len(results) == 2
    assert results[0][3] == "2025-08-16T16:00:00.000Z" # Check datetime of first entry
    assert results[1][2] == 27 # Check carbon intensity of second entry

@patch('requests.get')
def test_collect_is_idempotent(mock_get, test_db_connection):
    """
    Tests that collecting the same data twice does not create duplicate entries
    in the database, thanks to the UNIQUE constraint.
    """
    # Arrange
    mock_response = MagicMock()
    mock_response.json.return_value = MOCK_API_RESPONSE
    mock_get.return_value = mock_response

    db_manager = DatabaseManager()
    db_manager.connection = test_db_connection
    db_manager.init_schema()

    collector = ElectricityMapsCollector(zone="FR")
    collector.db_connection = test_db_connection

    # Act
    # Call collect twice with the same mock data
    collector.collect()
    collector.collect()

    # Assert
    # The API should have been called twice
    assert mock_get.call_count == 2
    # But the database should still only contain 2 unique records
    cursor = test_db_connection.cursor()
    cursor.execute("SELECT COUNT(*) FROM carbon_intensity")
    count = cursor.fetchone()[0]
    assert count == 2

@patch('requests.get')
def test_collect_handles_api_error(mock_get, test_db_connection):
    """
    Tests that the collector handles an API error gracefully and does not
    insert any data into the database.
    """
    # Arrange
    # Configure the mock to simulate an HTTP error
    mock_response = MagicMock()
    mock_response.raise_for_status.side_effect = requests.exceptions.HTTPError("404 Not Found")
    mock_get.return_value = mock_response

    db_manager = DatabaseManager()
    db_manager.connection = test_db_connection
    db_manager.init_schema()

    collector = ElectricityMapsCollector(zone="FR")
    collector.db_connection = test_db_connection

    # Act
    collector.collect()

    # Assert
    # Verify no data was inserted into the database
    cursor = test_db_connection.cursor()
    cursor.execute("SELECT COUNT(*) FROM carbon_intensity")
    count = cursor.fetchone()[0]
    assert count == 0
