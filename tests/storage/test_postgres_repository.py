from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from greenkube.models.metrics import CombinedMetric
from greenkube.storage.postgres_repository import PostgresCarbonIntensityRepository


@pytest.fixture
def connection_mock():
    conn = AsyncMock()
    # Ensure fetchrow and fetch return awaitables (AsyncMock does this by default)
    conn.fetchrow.return_value = None
    conn.fetch.return_value = []
    conn.executemany.return_value = None
    return conn


@pytest.fixture
def mock_db_manager(connection_mock):
    manager = MagicMock()

    @asynccontextmanager
    async def conn_scope():
        yield connection_mock

    manager.connection_scope = conn_scope
    return manager


@pytest.fixture
def repository(mock_db_manager):
    return PostgresCarbonIntensityRepository(mock_db_manager)


@pytest.mark.asyncio
async def test_get_for_zone_at_time_success(repository, connection_mock):
    # Setup
    zone = "TEST"
    timestamp = datetime(2023, 1, 1, 12, 0, tzinfo=timezone.utc)

    # Mock return value for fetchrow as a DICT
    connection_mock.fetchrow.return_value = {"carbon_intensity": 50.0}

    # Execute
    result = await repository.get_for_zone_at_time(zone, timestamp)

    # Verify
    assert result == 50.0
    connection_mock.fetchrow.assert_called_once()


@pytest.mark.asyncio
async def test_get_for_zone_at_time_none(repository, connection_mock):
    connection_mock.fetchrow.return_value = None

    # Execute
    result = await repository.get_for_zone_at_time("FR", datetime.now(timezone.utc))

    # Verify
    assert result is None
    connection_mock.fetchrow.assert_called_once()


@pytest.mark.asyncio
async def test_save_history_success(repository, connection_mock):
    # Setup
    data = [
        {
            "carbonIntensity": 50.0,
            "datetime": "2023-01-01T12:00:00Z",
            "updatedAt": "2023-01-01T12:00:00Z",
            "createdAt": "2023-01-01T12:00:00Z",
            "emissionFactorType": "test",
            "isEstimated": False,
            "estimationMethod": None,
        }
    ]
    zone = "TEST"

    # Execute
    count = await repository.save_history(data, zone)

    # Verify
    assert count == 1
    connection_mock.executemany.assert_called_once()


@pytest.mark.asyncio
async def test_save_history_updates_existing_record(repository, connection_mock):
    data = [{"datetime": "2023-01-01T12:00:00Z", "carbonIntensity": 55.0}]
    await repository.save_history(data, "TEST")
    connection_mock.executemany.assert_called_once()


@pytest.mark.asyncio
async def test_write_combined_metrics_success(repository, connection_mock):
    metrics = [
        CombinedMetric(
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
            is_estimated=False,
            estimation_reasons=[],
        )
    ]

    await repository.write_combined_metrics(metrics)
    connection_mock.executemany.assert_called_once()


@pytest.mark.asyncio
async def test_read_combined_metrics_success(repository, connection_mock):
    start = datetime(2023, 1, 1, 0, 0, tzinfo=timezone.utc)
    end = datetime(2023, 1, 1, 23, 59, tzinfo=timezone.utc)

    db_row = {
        "pod_name": "pod1",
        "namespace": "default",
        "total_cost": 0.1,
        "co2e_grams": 10.5,
        "pue": 1.2,
        "grid_intensity": 50.0,
        "joules": 1000.0,
        "cpu_request": 100.0,
        "memory_request": 1024.0,
        "period": "5m",
        "timestamp": datetime(2023, 1, 1, 12, 0, tzinfo=timezone.utc),
        "duration_seconds": 300.0,
        "grid_intensity_timestamp": datetime(2023, 1, 1, 12, 0, tzinfo=timezone.utc),
        "node_instance_type": "t3.medium",
        "node_zone": "eu-west-1a",
        "emaps_zone": "FR",
        "is_estimated": True,
        "estimation_reasons": '["default_profile"]',  # JSON string from DB
    }

    connection_mock.fetch.return_value = [db_row]

    metrics = await repository.read_combined_metrics(start, end)

    assert len(metrics) == 1
    assert metrics[0].pod_name == "pod1"
    assert metrics[0].estimation_reasons == ["default_profile"]
