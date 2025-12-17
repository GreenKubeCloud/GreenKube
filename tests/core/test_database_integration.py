# tests/core/test_database_integration.py

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from greenkube.core.db import db_manager
from greenkube.models.metrics import CombinedMetric
from greenkube.storage.elasticsearch_repository import ElasticsearchCarbonIntensityRepository
from greenkube.storage.sqlite_repository import SQLiteCarbonIntensityRepository


# Fixture to initialize and clean up the SQLite database
@pytest.fixture
async def sqlite_repo():
    await db_manager.setup_sqlite(db_path=":memory:")
    repo = SQLiteCarbonIntensityRepository(db_manager)
    yield repo
    await db_manager.close()


# Fixture to initialize and clean up the Elasticsearch database
@pytest.fixture
def elasticsearch_repo():
    repo = MagicMock(spec=ElasticsearchCarbonIntensityRepository)
    repo.write_combined_metrics = AsyncMock(return_value=2)
    # read_combined_metrics needs to be awaitable
    repo.read_combined_metrics = AsyncMock()
    repo.read_combined_metrics.return_value = [
        CombinedMetric(
            pod_name="pod-1",
            namespace="ns-1",
            total_cost=1.0,
            co2e_grams=10.0,
            joules=100.0,
            timestamp=datetime.now(timezone.utc),
        ),
        CombinedMetric(
            pod_name="pod-2",
            namespace="ns-2",
            total_cost=2.0,
            co2e_grams=20.0,
            joules=200.0,
            timestamp=datetime.now(timezone.utc),
        ),
    ]
    yield repo


@pytest.mark.asyncio
async def test_write_and_read_combined_metrics_sqlite(sqlite_repo):
    """
    Tests writing and reading of combined metrics to and from the SQLite database.
    """
    repo = sqlite_repo
    timestamp = datetime.now(timezone.utc)
    metrics = [
        CombinedMetric(
            pod_name="pod-1",
            namespace="ns-1",
            total_cost=1.0,
            co2e_grams=10.0,
            joules=100.0,
            timestamp=timestamp,
        ),
        CombinedMetric(
            pod_name="pod-2",
            namespace="ns-2",
            total_cost=2.0,
            co2e_grams=20.0,
            joules=200.0,
            timestamp=timestamp,
        ),
    ]

    await repo.write_combined_metrics(metrics)
    read_metrics = await repo.read_combined_metrics(
        start_time=timestamp - timedelta(minutes=1),
        end_time=timestamp + timedelta(minutes=1),
    )

    assert len(read_metrics) == 2
    assert read_metrics[0].pod_name == "pod-1"
    assert read_metrics[1].pod_name == "pod-2"


@pytest.mark.asyncio
async def test_write_and_read_combined_metrics_elasticsearch(elasticsearch_repo):
    """
    Tests writing and reading of combined metrics to and from the Elasticsearch database.
    """
    repo = elasticsearch_repo
    timestamp = datetime.now(timezone.utc)
    metrics = [
        CombinedMetric(
            pod_name="pod-1",
            namespace="ns-1",
            total_cost=1.0,
            co2e_grams=10.0,
            joules=100.0,
            timestamp=timestamp,
        ),
        CombinedMetric(
            pod_name="pod-2",
            namespace="ns-2",
            total_cost=2.0,
            co2e_grams=20.0,
            joules=200.0,
            timestamp=timestamp,
        ),
    ]

    await repo.write_combined_metrics(metrics)
    read_metrics = await repo.read_combined_metrics(
        start_time=timestamp - timedelta(minutes=1),
        end_time=timestamp + timedelta(minutes=1),
    )

    assert len(read_metrics) == 2
    assert read_metrics[0].pod_name == "pod-1"
    assert read_metrics[1].pod_name == "pod-2"
