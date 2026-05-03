from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import typer

from greenkube.cli import start as start_module
from greenkube.models.node import NodeInfo


@pytest.mark.asyncio
async def test_collect_carbon_intensity_for_all_zones_saves_mapped_zones():
    repository = MagicMock()
    repository.save_history = AsyncMock(return_value=2)

    node_collector = MagicMock()
    node_collector.collect = AsyncMock(
        return_value={
            "node-a": NodeInfo(name="node-a", zone="eu-west-3a", region="eu-west-3", cloud_provider="aws"),
            "node-b": NodeInfo(name="node-b", zone=None, region="eu-west-3", cloud_provider="aws"),
        }
    )
    node_collector.close = AsyncMock()

    em_collector = MagicMock()
    em_collector.collect = AsyncMock(return_value=[{"datetime": "2026-04-30T12:00:00Z", "carbonIntensity": 50}])
    em_collector.close = AsyncMock()

    with patch("greenkube.cli.start.get_repository", return_value=repository):
        with patch("greenkube.cli.start.NodeCollector", return_value=node_collector):
            with patch("greenkube.cli.start.ElectricityMapsCollector", return_value=em_collector):
                with patch("greenkube.cli.start.get_emaps_zone_from_cloud_zone", return_value="FR"):
                    await start_module.collect_carbon_intensity_for_all_zones()

    repository.save_history.assert_awaited_once()
    assert repository.save_history.await_args.kwargs == {"zone": "FR"}
    node_collector.close.assert_awaited_once()
    em_collector.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_collect_carbon_intensity_for_all_zones_handles_initialization_failure():
    with patch("greenkube.cli.start.get_repository", side_effect=RuntimeError("db missing")):
        await start_module.collect_carbon_intensity_for_all_zones()


@pytest.mark.asyncio
async def test_collect_carbon_intensity_for_all_zones_handles_no_nodes():
    node_collector = MagicMock()
    node_collector.collect = AsyncMock(return_value={})
    node_collector.close = AsyncMock()
    em_collector = MagicMock()
    em_collector.close = AsyncMock()

    with patch("greenkube.cli.start.get_repository", return_value=MagicMock()):
        with patch("greenkube.cli.start.NodeCollector", return_value=node_collector):
            with patch("greenkube.cli.start.ElectricityMapsCollector", return_value=em_collector):
                await start_module.collect_carbon_intensity_for_all_zones()

    node_collector.close.assert_awaited_once()
    em_collector.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_collect_carbon_intensity_for_all_zones_handles_unmapped_zones():
    repository = MagicMock()
    repository.save_history = AsyncMock()
    node_collector = MagicMock()
    node_collector.collect = AsyncMock(
        return_value={"node-a": NodeInfo(name="node-a", zone="moon-1", region="moon", cloud_provider="test")}
    )
    node_collector.close = AsyncMock()
    em_collector = MagicMock()
    em_collector.close = AsyncMock()

    with patch("greenkube.cli.start.get_repository", return_value=repository):
        with patch("greenkube.cli.start.NodeCollector", return_value=node_collector):
            with patch("greenkube.cli.start.ElectricityMapsCollector", return_value=em_collector):
                with patch("greenkube.cli.start.get_emaps_zone_from_cloud_zone", return_value="unknown"):
                    await start_module.collect_carbon_intensity_for_all_zones()

    repository.save_history.assert_not_awaited()


@pytest.mark.asyncio
async def test_collect_carbon_intensity_for_all_zones_keeps_going_when_zone_fails():
    repository = MagicMock()
    repository.save_history = AsyncMock(side_effect=RuntimeError("insert failed"))
    node_collector = MagicMock()
    node_collector.collect = AsyncMock(
        return_value={"node-a": NodeInfo(name="node-a", zone="eu-west-3a", region="eu-west-3", cloud_provider="aws")}
    )
    node_collector.close = AsyncMock()
    em_collector = MagicMock()
    em_collector.collect = AsyncMock(return_value=[{"datetime": "2026-04-30T12:00:00Z", "carbonIntensity": 50}])
    em_collector.close = AsyncMock()

    with patch("greenkube.cli.start.get_repository", return_value=repository):
        with patch("greenkube.cli.start.NodeCollector", return_value=node_collector):
            with patch("greenkube.cli.start.ElectricityMapsCollector", return_value=em_collector):
                with patch("greenkube.cli.start.get_emaps_zone_from_cloud_zone", return_value="FR"):
                    await start_module.collect_carbon_intensity_for_all_zones()

    repository.save_history.assert_awaited_once()
    node_collector.close.assert_awaited_once()
    em_collector.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_analyze_nodes_handles_initialization_failure():
    with patch("greenkube.cli.start.NodeCollector", side_effect=RuntimeError("k8s missing")):
        await start_module.analyze_nodes()


@pytest.mark.asyncio
async def test_analyze_nodes_ignores_node_metric_update_failure():
    node_collector = MagicMock()
    node_collector.collect = AsyncMock(return_value={"node-a": NodeInfo(name="node-a", zone="eu-west-3a")})
    node_collector.close = AsyncMock()
    node_repo = MagicMock()
    node_repo.save_nodes = AsyncMock(return_value=1)

    with patch("greenkube.cli.start.NodeCollector", return_value=node_collector):
        with patch("greenkube.cli.start.get_node_repository", return_value=node_repo):
            with patch("greenkube.cli.start.update_node_metrics", side_effect=RuntimeError("gauge failed")):
                await start_module.analyze_nodes()

    node_repo.save_nodes.assert_awaited_once()
    node_collector.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_scheduled_write_metrics_delegates_to_async_writer():
    with patch("greenkube.cli.start.async_write_combined_metrics_to_database", new_callable=AsyncMock) as writer:
        await start_module.scheduled_write_metrics()

    writer.assert_awaited_once_with(last=None)


@pytest.mark.asyncio
async def test_async_write_combined_metrics_to_database_delegates_last_value():
    with patch("greenkube.cli.start.write_combined_metrics_to_database", new_callable=AsyncMock) as writer:
        await start_module.async_write_combined_metrics_to_database(last="1h")

    writer.assert_awaited_once_with(last="1h")


@pytest.mark.asyncio
async def test_attribute_recommendation_savings_success():
    reco_repo = MagicMock()
    reco_repo.get_applied_recommendations = AsyncMock(return_value=["rec"])
    savings_repo = MagicMock()
    attributor = MagicMock()
    attributor.attribute_period = AsyncMock(return_value=1)

    with patch(
        "greenkube.core.config.get_config",
        return_value=SimpleNamespace(PROMETHEUS_QUERY_RANGE_STEP="5m", CLUSTER_NAME="cluster-a"),
    ):
        with patch("greenkube.core.factory.get_recommendation_repository", return_value=reco_repo):
            with patch("greenkube.core.factory.get_savings_ledger_repository", return_value=savings_repo):
                with patch("greenkube.core.savings_attributor.SavingsAttributor", return_value=attributor):
                    await start_module.attribute_recommendation_savings()

    attributor.attribute_period.assert_awaited_once_with(["rec"], period_seconds=300)


@pytest.mark.asyncio
async def test_attribute_recommendation_savings_handles_error():
    with patch("greenkube.core.factory.get_recommendation_repository", side_effect=RuntimeError("repo missing")):
        await start_module.attribute_recommendation_savings()


@pytest.mark.asyncio
async def test_compress_metrics_success_with_savings_compression():
    compressor = MagicMock()
    compressor.run = AsyncMock(return_value={"hours_compressed": 2, "raw_rows_pruned": 3, "hourly_rows_pruned": 4})
    compressor.refresh_namespace_cache = AsyncMock()
    savings_repo = MagicMock()
    savings_repo.compress_to_hourly = AsyncMock(return_value=5)
    savings_repo.prune_raw = AsyncMock()

    with patch("greenkube.core.metrics_compressor.MetricsCompressor", return_value=compressor):
        with patch("greenkube.core.db.get_db_manager", return_value=MagicMock()):
            with patch("greenkube.core.factory.get_savings_ledger_repository", return_value=savings_repo):
                with patch(
                    "greenkube.core.config.get_config",
                    return_value=SimpleNamespace(METRICS_COMPRESSION_AGE_HOURS=24, METRICS_RAW_RETENTION_DAYS=30),
                ):
                    await start_module.compress_metrics()

    compressor.run.assert_awaited_once()
    compressor.refresh_namespace_cache.assert_awaited_once()
    savings_repo.compress_to_hourly.assert_awaited_once_with(cutoff_hours=24)
    savings_repo.prune_raw.assert_awaited_once_with(retention_days=30)


@pytest.mark.asyncio
async def test_compress_metrics_handles_errors():
    with patch("greenkube.core.metrics_compressor.MetricsCompressor", side_effect=RuntimeError("compressor failed")):
        await start_module.compress_metrics()


@pytest.mark.asyncio
async def test_refresh_dashboard_summary_success():
    summary_repo = MagicMock()
    summary_repo.get_rows = AsyncMock(return_value=["row"])
    refresher = MagicMock()
    refresher.run = AsyncMock(return_value=1)
    update_metrics = MagicMock()

    with patch("greenkube.core.factory.get_summary_repository", return_value=summary_repo):
        with patch("greenkube.core.factory.get_combined_metrics_repository", return_value=MagicMock()):
            with patch("greenkube.core.factory.get_timeseries_cache_repository", return_value=MagicMock()):
                with patch("greenkube.core.summary_refresher.SummaryRefresher", return_value=refresher):
                    with patch("greenkube.api.metrics_endpoint.update_dashboard_summary_metrics", update_metrics):
                        await start_module.refresh_dashboard_summary()

    refresher.run.assert_awaited_once()
    summary_repo.get_rows.assert_awaited_once_with(namespace=None)
    update_metrics.assert_called_once_with(["row"], reset=True)


@pytest.mark.asyncio
async def test_refresh_dashboard_summary_handles_errors():
    with patch("greenkube.core.factory.get_summary_repository", side_effect=RuntimeError("repo missing")):
        await start_module.refresh_dashboard_summary()


@pytest.mark.asyncio
async def test_async_start_bootstraps_scheduler_and_initial_tasks():
    db_manager = MagicMock()
    db_manager.connect = AsyncMock()
    scheduler = MagicMock()
    scheduler.add_job = MagicMock()
    scheduler.add_job_from_string = MagicMock()
    scheduler.stop = AsyncMock()
    stop_event = MagicMock()
    stop_event.wait = AsyncMock(return_value=None)
    loop = MagicMock()

    with patch(
        "greenkube.cli.start.get_config",
        return_value=SimpleNamespace(
            LOG_LEVEL="INFO",
            DB_TYPE="sqlite",
            PROMETHEUS_QUERY_RANGE_STEP="5m",
            NODE_ANALYSIS_INTERVAL="1h",
        ),
    ):
        with patch("greenkube.core.db.get_db_manager", return_value=db_manager):
            with patch("greenkube.cli.start.Scheduler", return_value=scheduler):
                with patch(
                    "greenkube.cli.start.collect_carbon_intensity_for_all_zones", new_callable=AsyncMock
                ) as carbon:
                    with patch("greenkube.cli.start.analyze_nodes", new_callable=AsyncMock) as analyze:
                        with patch(
                            "greenkube.cli.start.async_write_combined_metrics_to_database", new_callable=AsyncMock
                        ) as write_metrics:
                            with patch(
                                "greenkube.cli.start.attribute_recommendation_savings", new_callable=AsyncMock
                            ) as attribute:
                                with patch("greenkube.cli.start.compress_metrics", new_callable=AsyncMock) as compress:
                                    with patch(
                                        "greenkube.cli.start.refresh_dashboard_summary", new_callable=AsyncMock
                                    ) as refresh:
                                        with patch("greenkube.cli.start.asyncio.Event", return_value=stop_event):
                                            with patch(
                                                "greenkube.cli.start.asyncio.get_running_loop", return_value=loop
                                            ):
                                                await start_module._async_start(last="1h")

    db_manager.connect.assert_awaited_once()
    assert scheduler.add_job.call_count == 3
    assert scheduler.add_job_from_string.call_count == 3
    carbon.assert_awaited_once()
    analyze.assert_awaited_once()
    write_metrics.assert_awaited_once_with(last="1h")
    attribute.assert_awaited_once()
    compress.assert_awaited_once()
    refresh.assert_awaited_once()
    stop_event.wait.assert_awaited_once()
    scheduler.stop.assert_awaited_once()


def test_start_returns_when_subcommand_invoked():
    ctx = MagicMock()
    ctx.invoked_subcommand = "child"

    with patch("greenkube.cli.start.asyncio.run") as run:
        start_module.start(ctx)

    run.assert_not_called()


def test_start_ignores_keyboard_interrupt():
    ctx = MagicMock()
    ctx.invoked_subcommand = None

    with patch("greenkube.cli.start._async_start", return_value="startup-coroutine"):
        with patch("greenkube.cli.start.asyncio.run", side_effect=KeyboardInterrupt):
            start_module.start(ctx)


def test_start_raises_typer_exit_on_unexpected_error():
    ctx = MagicMock()
    ctx.invoked_subcommand = None

    with patch("greenkube.cli.start._async_start", return_value="startup-coroutine"):
        with patch("greenkube.cli.start.asyncio.run", side_effect=RuntimeError("boom")):
            with pytest.raises(typer.Exit) as exc_info:
                start_module.start(ctx)

    assert exc_info.value.exit_code == 1
