from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from greenkube.models.metrics import CombinedMetric
from greenkube.storage.postgres_repository import PostgresCarbonIntensityRepository


@pytest.fixture
def mock_db_manager():
    manager = MagicMock()
    # Mock connection_scope context manager
    connection = MagicMock()
    manager.connection_scope.return_value.__enter__.return_value = connection

    # Mock cursor context manager
    cursor_ctx = MagicMock()
    real_cursor = MagicMock()
    connection.cursor.return_value = cursor_ctx
    cursor_ctx.__enter__.return_value = real_cursor

    return manager, real_cursor


@pytest.fixture
def repository(mock_db_manager):
    manager, _ = mock_db_manager
    return PostgresCarbonIntensityRepository(manager)


def test_get_for_zone_at_time_success(repository, mock_db_manager):
    _, cursor = mock_db_manager
    # Setup
    zone = "FR"
    time = datetime(2023, 1, 1, 12, 0, tzinfo=timezone.utc)
    expected_result = {"zone": "FR", "carbon_intensity": 50, "datetime": time, "is_estimated": False}

    # Configure mock cursor
    cursor.fetchone.return_value = expected_result

    # Execute
    result = repository.get_for_zone_at_time(zone, time)

    # Verify
    assert result == expected_result
    cursor.execute.assert_called_once()
    args = cursor.execute.call_args[0]
    assert "SELECT * FROM carbon_intensity_history" in args[0]
    assert args[1] == (zone, time)


def test_get_for_zone_at_time_none(repository, mock_db_manager):
    _, cursor = mock_db_manager
    # Setup
    cursor.fetchone.return_value = None

    # Execute
    result = repository.get_for_zone_at_time("FR", datetime.now(timezone.utc))

    # Verify
    assert result is None


def test_save_history_success(repository, mock_db_manager):
    manager, cursor = mock_db_manager
    # Setup
    history_data = [
        {
            "zone": "FR",
            "carbon_intensity": 50,
            "datetime": datetime(2023, 1, 1, 12, 0, tzinfo=timezone.utc),
            "updated_at": datetime.now(timezone.utc),
            "created_at": datetime.now(timezone.utc),
            "emission_factor_type": "lifecycle",
            "is_estimated": False,
            "estimation_method": None,
        }
    ]

    # Execute
    repository.save_history(history_data)

    # Verify
    cursor.executemany.assert_called_once()
    conn = manager.connection_scope.return_value.__enter__.return_value
    conn.commit.assert_called_once()


def test_write_combined_metrics_success(repository, mock_db_manager):
    manager, cursor = mock_db_manager
    # Setup
    metric = CombinedMetric(
        pod_name="pod1",
        namespace="default",
        total_cost=0.1,
        co2e_grams=10.5,
        pue=1.2,
        grid_intensity=50.0,
        joules=1000.0,
        cpu_request=100,
        memory_request=1024,
        period="5m",
        timestamp=datetime(2023, 1, 1, 12, 0, tzinfo=timezone.utc),
        duration_seconds=300,
        grid_intensity_timestamp=datetime(2023, 1, 1, 12, 0, tzinfo=timezone.utc),
        node_instance_type="t3.medium",
        node_zone="eu-west-1a",
        emaps_zone="FR",
        is_estimated=True,
        estimation_reasons=["default_profile"],
    )
    metrics = [metric]

    # Execute
    repository.write_combined_metrics(metrics)

    # Verify
    cursor.executemany.assert_called_once()

    # Check that estimation_reasons was serialized to JSON
    call_args = cursor.executemany.call_args[0]
    inserted_data = call_args[1]
    assert len(inserted_data) == 1
    assert isinstance(inserted_data[0]["estimation_reasons"], str)
    assert '"default_profile"' in inserted_data[0]["estimation_reasons"]
    conn = manager.connection_scope.return_value.__enter__.return_value
    conn.commit.assert_called_once()


def test_read_combined_metrics_success(repository, mock_db_manager):
    _, cursor = mock_db_manager
    # Setup
    start_time = datetime(2023, 1, 1, 0, 0, tzinfo=timezone.utc)
    end_time = datetime(2023, 1, 1, 23, 59, tzinfo=timezone.utc)

    db_row = {
        "pod_name": "pod1",
        "namespace": "default",
        "total_cost": 0.1,
        "co2e_grams": 10.5,
        "pue": 1.2,
        "grid_intensity": 50.0,
        "joules": 1000.0,
        "cpu_request": 100,
        "memory_request": 1024,
        "period": "5m",
        "timestamp": datetime(2023, 1, 1, 12, 0, tzinfo=timezone.utc),
        "duration_seconds": 300,
        "grid_intensity_timestamp": datetime(2023, 1, 1, 12, 0, tzinfo=timezone.utc),
        "node_instance_type": "t3.medium",
        "node_zone": "eu-west-1a",
        "emaps_zone": "FR",
        "is_estimated": True,
        "estimation_reasons": '["default_profile"]',  # JSON string from DB
    }

    cursor.fetchall.return_value = [db_row]

    # Execute
    metrics = repository.read_combined_metrics(start_time, end_time)

    # Verify
    assert len(metrics) == 1
    metric = metrics[0]
    assert isinstance(metric, CombinedMetric)
    assert metric.pod_name == "pod1"
    assert metric.estimation_reasons == ["default_profile"]  # Should be deserialized
