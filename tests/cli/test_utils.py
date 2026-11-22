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

    async def advance(self, cluster_uid, source, last_complete_end, *, connection=None):
        self.value = max(self.value, last_complete_end) if self.value else last_complete_end
        return CollectionWatermark(cluster_uid, source, self.value, self.value)

    async def list_all(self, cluster_uid=None):
        return []


class _MemoryMetrics:
    """Metrics repository double: records the rows of every window and commits its watermark (STO-03)."""

    def __init__(self) -> None:
        self.windows: list[list[CombinedMetric]] = []

    async def stored_window_profile(self, start, end):
        return {}

    async def write_window(self, rows, idle_rows, *, window, run_id, on_commit=None, force=False):
        if rows:
            self.windows.append(list(rows))
        if on_commit is not None:
            await on_commit(None)
        return len(rows)


@pytest.fixture(autouse=True)
def _no_window_records():
    """These tests use in-memory doubles; the collection window record has its own tests (COL-04)."""
    with (
        patch("greenkube.cli.utils.get_collection_window_repository", return_value=None),
        patch("greenkube.cli.utils.resolve_collection_cluster_uid", AsyncMock(return_value="test-uid")),
    ):
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
async def test_write_combined_metrics_to_database_saves_rows():
    metric = _metric()
    repo = _MemoryMetrics()
    processor = MagicMock()
    processor.run_range = AsyncMock(return_value=[metric])
    processor.close = AsyncMock()
    watermarks = _MemoryWatermarks()

    with patch("greenkube.cli.utils.get_combined_metrics_repository", return_value=repo):
        with patch("greenkube.cli.utils.get_processor", return_value=processor):
            with patch("greenkube.cli.utils.get_collection_watermark_repository", return_value=watermarks):
                report = await utils.write_combined_metrics_to_database(last="10min")

    assert report is not None and report.succeeded
    assert processor.run_range.await_count == 1
    assert repo.windows == [[metric]]
    assert watermarks.value is not None
    processor.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_write_combined_metrics_to_database_handles_empty_results():
    repo = _MemoryMetrics()
    processor = MagicMock()
    processor.run_range = AsyncMock(return_value=[])
    processor.close = AsyncMock()
    watermarks = _MemoryWatermarks(_recent_watermark())
    before = watermarks.value

    with patch("greenkube.cli.utils.get_combined_metrics_repository", return_value=repo):
        with patch("greenkube.cli.utils.get_processor", return_value=processor):
            with patch("greenkube.cli.utils.get_collection_watermark_repository", return_value=watermarks):
                report = await utils.write_combined_metrics_to_database()

    assert repo.windows == []
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


def test_watermark_repository_follows_database_type():
    from greenkube.storage.sqlite.collection_watermark_repository import SQLiteCollectionWatermarkRepository

    assert isinstance(utils.get_collection_watermark_repository(), SQLiteCollectionWatermarkRepository)


@pytest.mark.asyncio
async def test_collection_does_not_start_on_a_database_of_another_cluster():
    """STO-09: a cluster UID mismatch stops before any processor or write."""
    from greenkube.core.cluster_identity import ClusterIdentityMismatch

    processor_factory = MagicMock()
    with patch(
        "greenkube.cli.utils.resolve_collection_cluster_uid",
        AsyncMock(side_effect=ClusterIdentityMismatch("This database belongs to cluster a")),
    ):
        with patch("greenkube.cli.utils.get_processor", processor_factory):
            assert await utils.write_combined_metrics_to_database() is None
    processor_factory.assert_not_called()
