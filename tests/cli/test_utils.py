from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import typer

from greenkube.cli import utils
from greenkube.models.metrics import CombinedMetric


def _metric(namespace: str = "prod") -> CombinedMetric:
    return CombinedMetric(
        pod_name="api-pod",
        namespace=namespace,
        total_cost=1.0,
        co2e_grams=2.0,
        joules=100.0,
        timestamp=datetime(2026, 4, 30, 12, 0, tzinfo=timezone.utc),
    )


def test_parse_last_duration_wraps_invalid_values():
    with pytest.raises(typer.BadParameter):
        utils.parse_last_duration("soon")


@pytest.mark.parametrize(
    ("step", "expected_delta"),
    [
        ("30s", timedelta(seconds=30)),
        ("5m", timedelta(minutes=5)),
        ("1h", timedelta(hours=1)),
    ],
)
def test_get_normalized_window_aligns_to_configured_step(step, expected_delta):
    fixed_now = datetime(2026, 4, 30, 12, 7, 43, tzinfo=timezone.utc)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now if tz is None else fixed_now.astimezone(tz)

    with patch("greenkube.cli.utils.get_config", return_value=SimpleNamespace(PROMETHEUS_QUERY_RANGE_STEP=step)):
        with patch("greenkube.cli.utils.datetime", FixedDateTime):
            start, end = utils.get_normalized_window()

    assert end <= fixed_now
    assert end.second % max(int(expected_delta.total_seconds()), 1) == 0 if step.endswith("s") else True
    assert end - start == expected_delta


def test_get_normalized_window_rejects_unsupported_step():
    with patch("greenkube.cli.utils.get_config", return_value=SimpleNamespace(PROMETHEUS_QUERY_RANGE_STEP="1d")):
        with pytest.raises(ValueError, match="Unsupported PROMETHEUS_QUERY_RANGE_STEP"):
            utils.get_normalized_window()


@pytest.mark.asyncio
async def test_write_combined_metrics_to_database_saves_and_updates_gauges():
    metric = _metric()
    repo = MagicMock()
    repo.write_combined_metrics = AsyncMock(return_value=1)
    processor = MagicMock()
    processor.run_range = AsyncMock(return_value=[metric])
    processor.close = AsyncMock()
    update_cluster_metrics = MagicMock()

    with patch("greenkube.cli.utils.get_combined_metrics_repository", return_value=repo):
        with patch("greenkube.cli.utils.get_processor", return_value=processor):
            with patch("greenkube.cli.utils.parse_last_duration", return_value=timedelta(hours=1)):
                with patch("greenkube.cli.utils.update_cluster_metrics", update_cluster_metrics):
                    await utils.write_combined_metrics_to_database(last="1h")

    processor.run_range.assert_awaited_once()
    repo.write_combined_metrics.assert_awaited_once_with([metric])
    update_cluster_metrics.assert_called_once_with([metric])
    processor.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_write_combined_metrics_to_database_handles_empty_results():
    repo = MagicMock()
    repo.write_combined_metrics = AsyncMock()
    processor = MagicMock()
    processor.run_range = AsyncMock(return_value=[])
    processor.close = AsyncMock()

    with patch("greenkube.cli.utils.get_combined_metrics_repository", return_value=repo):
        with patch("greenkube.cli.utils.get_processor", return_value=processor):
            with patch(
                "greenkube.cli.utils.get_normalized_window",
                return_value=(datetime.now(timezone.utc), datetime.now(timezone.utc)),
            ):
                await utils.write_combined_metrics_to_database()

    repo.write_combined_metrics.assert_not_awaited()
    processor.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_write_combined_metrics_to_database_handles_initialization_failure():
    with patch("greenkube.cli.utils.get_combined_metrics_repository", side_effect=RuntimeError("missing db")):
        await utils.write_combined_metrics_to_database()


@pytest.mark.asyncio
async def test_write_combined_metrics_to_database_closes_processor_after_processing_error():
    repo = MagicMock()
    processor = MagicMock()
    processor.run_range = AsyncMock(side_effect=RuntimeError("collector failed"))
    processor.close = AsyncMock()

    with patch("greenkube.cli.utils.get_combined_metrics_repository", return_value=repo):
        with patch("greenkube.cli.utils.get_processor", return_value=processor):
            with patch(
                "greenkube.cli.utils.get_normalized_window",
                return_value=(datetime.now(timezone.utc), datetime.now(timezone.utc)),
            ):
                await utils.write_combined_metrics_to_database()

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
                "greenkube.cli.utils.get_normalized_window",
                return_value=(datetime.now(timezone.utc), datetime.now(timezone.utc)),
            ):
                with patch("greenkube.cli.utils.update_cluster_metrics", side_effect=RuntimeError("gauge failed")):
                    await utils.write_combined_metrics_to_database()

    repo.write_combined_metrics.assert_awaited_once_with([metric])
    processor.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_read_combined_metrics_from_database_filters_namespace():
    repo = MagicMock()
    repo.read_combined_metrics = AsyncMock(return_value=[_metric("prod"), _metric("dev")])
    start = datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc)
    end = datetime(2026, 4, 30, 11, 0, tzinfo=timezone.utc)

    with patch("greenkube.cli.utils.get_combined_metrics_repository", return_value=repo):
        data = await utils.read_combined_metrics_from_database(start, end, namespace="prod")

    assert [item.namespace for item in data] == ["prod"]
    repo.read_combined_metrics.assert_awaited_once_with(start_time=start, end_time=end)


@pytest.mark.asyncio
async def test_read_combined_metrics_from_database_returns_empty_on_error():
    with patch("greenkube.cli.utils.get_combined_metrics_repository", side_effect=RuntimeError("db down")):
        data = await utils.read_combined_metrics_from_database(
            datetime(2026, 4, 30, 10, 0, tzinfo=timezone.utc),
            datetime(2026, 4, 30, 11, 0, tzinfo=timezone.utc),
        )

    assert data == []
