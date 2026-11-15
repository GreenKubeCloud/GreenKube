from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import typer

from greenkube.cli import utils
from greenkube.models.metrics import CombinedMetric
from greenkube.storage.base_collection_watermark_repository import CollectionWatermark


def _metric(namespace: str = "prod") -> CombinedMetric:
    return CombinedMetric(
        pod_name="api-pod",
        namespace=namespace,
        total_cost=1.0,
        co2e_grams=2.0,
        joules=100.0,
        timestamp=datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc),
    )


class _MemoryWatermarks:
    def __init__(self, last_complete_end: datetime | None = None) -> None:
        self.value = last_complete_end

    async def get(self, cluster_uid, source):
        if self.value is None:
            return None
        return CollectionWatermark(cluster_uid, source, self.value, self.value)

    async def advance(self, cluster_uid, source, last_complete_end):
        self.value = max(self.value, last_complete_end) if self.value else last_complete_end
        return CollectionWatermark(cluster_uid, source, self.value, self.value)

    async def list_all(self, cluster_uid=None):
        return []


@pytest.fixture(autouse=True)
def _no_window_records():
    """These tests use in-memory doubles; the collection window record has its own tests (COL-04)."""
    with patch("greenkube.cli.utils.get_collection_window_repository", return_value=None):
        yield


def _recent_watermark() -> datetime:
    # One pending 5-minute window: watermark two steps behind now.
    now = datetime.now(timezone.utc)
    aligned = datetime.fromtimestamp(int(now.timestamp()) // 300 * 300, tz=timezone.utc)
    return datetime.fromtimestamp(aligned.timestamp() - 600, tz=timezone.utc)


def test_parse_last_duration_wraps_invalid_values():
    with pytest.raises(typer.BadParameter):
        utils.parse_last_duration("soon")


@pytest.mark.asyncio
async def test_write_combined_metrics_to_database_saves_and_updates_gauges():
    metric = _metric()
    repo = MagicMock()
    repo.write_combined_metrics = AsyncMock(return_value=1)
    processor = MagicMock()
    processor.run_range = AsyncMock(return_value=[metric])
    processor.close = AsyncMock()
    update_cluster_metrics = MagicMock()
    watermarks = _MemoryWatermarks()

    with patch("greenkube.cli.utils.get_combined_metrics_repository", return_value=repo):
        with patch("greenkube.cli.utils.get_processor", return_value=processor):
            with patch("greenkube.cli.utils.get_collection_watermark_repository", return_value=watermarks):
                with patch("greenkube.cli.utils.update_cluster_metrics", update_cluster_metrics):
                    report = await utils.write_combined_metrics_to_database(last="10min")

    assert report is not None and report.succeeded
    assert processor.run_range.await_count == 1
    repo.write_combined_metrics.assert_awaited_once_with([metric], replace_containers=True)
    update_cluster_metrics.assert_called_once_with([metric])
    assert watermarks.value is not None
    processor.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_write_combined_metrics_to_database_handles_empty_results():
    repo = MagicMock()
    repo.write_combined_metrics = AsyncMock()
    processor = MagicMock()
    processor.run_range = AsyncMock(return_value=[])
    processor.close = AsyncMock()
    watermarks = _MemoryWatermarks(_recent_watermark())
    before = watermarks.value

    with patch("greenkube.cli.utils.get_combined_metrics_repository", return_value=repo):
        with patch("greenkube.cli.utils.get_processor", return_value=processor):
            with patch("greenkube.cli.utils.get_collection_watermark_repository", return_value=watermarks):
                report = await utils.write_combined_metrics_to_database()

    repo.write_combined_metrics.assert_not_awaited()
    # An answered-but-empty window is complete: the watermark still moves forward.
    assert report is not None and report.succeeded
    assert watermarks.value is not None and before is not None and watermarks.value > before
    processor.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_write_combined_metrics_to_database_handles_initialization_failure():
    with patch("greenkube.cli.utils.get_combined_metrics_repository", side_effect=RuntimeError("missing db")):
        assert await utils.write_combined_metrics_to_database() is None


@pytest.mark.asyncio
async def test_write_combined_metrics_to_database_keeps_watermark_after_processing_error():
    repo = MagicMock()
    processor = MagicMock()
    processor.run_range = AsyncMock(side_effect=RuntimeError("collector failed"))
    processor.close = AsyncMock()
    watermarks = _MemoryWatermarks(_recent_watermark())
    before = watermarks.value

    with patch("greenkube.cli.utils.get_combined_metrics_repository", return_value=repo):
        with patch("greenkube.cli.utils.get_processor", return_value=processor):
            with patch("greenkube.cli.utils.get_collection_watermark_repository", return_value=watermarks):
                report = await utils.write_combined_metrics_to_database()

    assert report is not None and not report.succeeded
    assert watermarks.value == before
    processor.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_write_combined_metrics_to_database_ignores_gauge_update_failure():
    metric = _metric()
    repo = MagicMock()
    repo.write_combined_metrics = AsyncMock(return_value=1)
    processor = MagicMock()
    processor.run_range = AsyncMock(return_value=[metric])
    processor.close = AsyncMock()

    with patch("greenkube.cli.utils.get_combined_metrics_repository", return_value=repo):
        with patch("greenkube.cli.utils.get_processor", return_value=processor):
            with patch(
                "greenkube.cli.utils.get_collection_watermark_repository",
                return_value=_MemoryWatermarks(_recent_watermark()),
            ):
                with patch("greenkube.cli.utils.update_cluster_metrics", side_effect=RuntimeError("gauge failed")):
                    await utils.write_combined_metrics_to_database()

    repo.write_combined_metrics.assert_awaited_once_with([metric], replace_containers=True)
    processor.close.assert_awaited_once()


def test_watermark_repository_follows_database_type():
    from greenkube.storage.sqlite.collection_watermark_repository import SQLiteCollectionWatermarkRepository

    assert isinstance(utils.get_collection_watermark_repository(), SQLiteCollectionWatermarkRepository)
