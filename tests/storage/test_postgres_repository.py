from datetime import datetime
from unittest.mock import MagicMock

import pytest

from greenkube.models.metrics import CombinedMetric
from greenkube.storage.postgres_repository import PostgresCarbonIntensityRepository


@pytest.fixture
def mock_cursor():
    cursor = MagicMock()
    cursor.fetchone.return_value = None
    cursor.fetchall.return_value = []
    return cursor


@pytest.fixture
def mock_connection(mock_cursor):
    connection = MagicMock()
    connection.cursor.return_value = mock_cursor
    return connection


@pytest.fixture
def repository(mock_connection):
    return PostgresCarbonIntensityRepository(mock_connection)


def test_get_for_zone_at_time(repository, mock_cursor):
    mock_cursor.fetchone.return_value = {
        "zone": "FR",
        "carbon_intensity": 50,
        "datetime": datetime(2023, 1, 1, 12, 0, 0),
    }

    result = repository.get_for_zone_at_time("FR", datetime(2023, 1, 1, 12, 0, 0))

    assert result["zone"] == "FR"
    assert result["carbon_intensity"] == 50
    mock_cursor.execute.assert_called_once()


def test_save_history(repository, mock_cursor, mock_connection):
    history_data = [
        {
            "zone": "FR",
            "carbon_intensity": 50,
            "datetime": datetime(2023, 1, 1, 12, 0, 0),
            "updated_at": datetime.now(),
            "created_at": datetime.now(),
            "emission_factor_type": "estimated",
            "is_estimated": True,
            "estimation_method": "default",
        }
    ]

    repository.save_history(history_data)

    mock_cursor.executemany.assert_called_once()
    mock_connection.commit.assert_called_once()


def test_write_combined_metrics(repository, mock_cursor, mock_connection):
    metric = CombinedMetric(
        pod_name="test-pod",
        namespace="default",
        total_cost=0.1,
        co2e_grams=10.0,
        pue=1.2,
        grid_intensity=50.0,
        joules=1000.0,
        cpu_request=100,
        memory_request=1024,
        period="5m",
        timestamp=datetime(2023, 1, 1, 12, 0, 0),
        duration_seconds=300,
        grid_intensity_timestamp=datetime(2023, 1, 1, 12, 0, 0),
        node_instance_type="t3.medium",
        node_zone="eu-west-1a",
        emaps_zone="FR",
    )

    repository.write_combined_metrics([metric])

    mock_cursor.executemany.assert_called_once()
    mock_connection.commit.assert_called_once()


def test_read_combined_metrics(repository, mock_cursor):
    mock_cursor.fetchall.return_value = [
        {
            "pod_name": "test-pod",
            "namespace": "default",
            "total_cost": 0.1,
            "co2e_grams": 10.0,
            "pue": 1.2,
            "grid_intensity": 50.0,
            "joules": 1000.0,
            "cpu_request": 100,
            "memory_request": 1024,
            "period": "5m",
            "timestamp": datetime(2023, 1, 1, 12, 0, 0),
            "duration_seconds": 300,
            "grid_intensity_timestamp": datetime(2023, 1, 1, 12, 0, 0),
            "node_instance_type": "t3.medium",
            "node_zone": "eu-west-1a",
            "emaps_zone": "FR",
        }
    ]

    metrics = repository.read_combined_metrics(datetime(2023, 1, 1, 0, 0, 0), datetime(2023, 1, 2, 0, 0, 0))

    assert len(metrics) == 1
    assert metrics[0].pod_name == "test-pod"
    mock_cursor.execute.assert_called_once()
