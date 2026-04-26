# src/greenkube/cli/start.py
"""
Start command for the GreenKube CLI.

This module contains the scheduler start command and the helper that
collects carbon intensity for all zones. It mirrors the previous logic
that lived in `cli.main` but is isolated for clarity and easier testing.
"""

import asyncio
import logging
import signal
import traceback
from typing import Optional, Set

import typer
from typing_extensions import Annotated

from ..collectors.electricity_maps_collector import ElectricityMapsCollector
from ..collectors.node_collector import NodeCollector
from ..core.config import get_config
from ..core.factory import get_node_repository, get_repository
from ..core.scheduler import Scheduler
from ..utils.mapping_translator import get_emaps_zone_from_cloud_zone
from .utils import write_combined_metrics_to_database

try:
    from ..api.metrics_endpoint import update_node_metrics
except Exception:  # pragma: no cover – optional dependency
    update_node_metrics = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

app = typer.Typer(name="start", help="Start the GreenKube data collection service.")


async def collect_carbon_intensity_for_all_zones() -> None:
    """
    Orchestrates the collection and saving of carbon intensity data.
    """
    logger.info("--- Starting hourly carbon intensity collection task ---")
    try:
        repository = get_repository()
        node_collector = NodeCollector()
        em_collector = ElectricityMapsCollector()
    except Exception as e:
        logger.error("Failed to initialize components for intensity collection: %s", e)
        return

    try:
        nodes_info = await node_collector.collect()
        if not nodes_info:
            logger.warning("No node zones discovered.")
            return

        # Collect unique (zone, region, provider) tuples for mapping.
        unique_zone_region_providers = {
            (node_info.zone, node_info.region, node_info.cloud_provider)
            for node_info in nodes_info.values()
            if node_info.zone or node_info.region
        }
        emaps_zones: Set[str] = set()
        for cz, region, provider in unique_zone_region_providers:
            emz = None
            # Try zone first
            if cz:
                emz = get_emaps_zone_from_cloud_zone(cz, provider=provider)
            # Fallback to region (mirrors NodeZoneMapper logic)
            if not emz and region:
                emz = get_emaps_zone_from_cloud_zone(region, provider=provider)
            if emz and emz != "unknown":
                emaps_zones.add(emz)
            else:
                logger.warning(
                    "Could not map cloud zone '%s' or region '%s' (provider: %s) to an Electricity Maps zone.",
                    cz,
                    region,
                    provider,
                )

        if not emaps_zones:
            logger.warning("No mappable Electricity Maps zones found based on node discovery.")
            return

        # Parallelize zone history collection
        async def process_zone(zone):
            try:
                history_data = await em_collector.collect(zone=zone)
                if history_data:
                    saved_count = await repository.save_history(history_data, zone=zone)
                    logger.info("Successfully saved %s new records for zone: %s", saved_count, zone)
                else:
                    logger.info("No new data to save for zone: %s", zone)
            except Exception as e:
                logger.error("Failed to process data for zone %s: %s", zone, e)

        await asyncio.gather(*(process_zone(zone) for zone in emaps_zones))

    except Exception as e:
        logger.error("Failed to collect node zones: %s", e)
    finally:
        await node_collector.close()
        await em_collector.close()

    logger.info("--- Finished carbon intensity collection task ---")


async def analyze_nodes() -> None:
    """
    Collects node information and updates the database.
    """
    logger.info("--- Starting node analysis task ---")
    try:
        node_collector = NodeCollector()
        node_repo = get_node_repository()
    except Exception as e:
        logger.error("Failed to initialize components for node analysis: %s", e)
        return

    try:
        nodes_info = await node_collector.collect()
        if not nodes_info:
            logger.warning("No nodes discovered during analysis.")
            return

        saved_count = await node_repo.save_nodes(list(nodes_info.values()))
        logger.info("Successfully updated %s nodes in the database.", saved_count)

        # Update Prometheus gauges for Grafana scraping
        if update_node_metrics is not None:
            try:
                update_node_metrics(list(nodes_info.values()))
            except Exception as e:
                logger.warning("Failed to update Prometheus node metrics: %s", e)

    except Exception as e:
        logger.error("Failed to analyze nodes: %s", e)
    finally:
        if "node_collector" in locals():
            await node_collector.close()

    logger.info("--- Finished node analysis task ---")


async def async_write_combined_metrics_to_database(last: Optional[str] = None):
    """Wrapper to make write_combined_metrics_to_database suitable for scheduler."""
    await write_combined_metrics_to_database(last=last)


async def scheduled_write_metrics():
    """Scheduled task wrapper for writing combined metrics (always uses last=None)."""
    await async_write_combined_metrics_to_database(last=None)


async def attribute_recommendation_savings() -> None:
    """Attribute prorated savings for all applied recommendations to the ledger."""
    logger.info("--- Starting savings attribution task ---")
    try:
        from ..core.config import get_config as _cfg
        from ..core.factory import get_recommendation_repository as _reco_repo
        from ..core.factory import get_savings_ledger_repository as _savings_repo
        from ..core.savings_attributor import SavingsAttributor

        cfg = _cfg()
        # Derive period_seconds from PROMETHEUS_QUERY_RANGE_STEP
        step = cfg.PROMETHEUS_QUERY_RANGE_STEP or "5m"
        unit = step[-1]
        value = int(step[:-1])
        period_map = {"s": 1, "m": 60, "h": 3600}
        period_seconds = value * period_map.get(unit, 60)

        reco_repo = _reco_repo()
        savings_repo = _savings_repo()
        applied = await reco_repo.get_applied_recommendations()

        cluster = cfg.CLUSTER_NAME or "default"
        attributor = SavingsAttributor(savings_repo=savings_repo, cluster_name=cluster)
        count = await attributor.attribute_period(applied, period_seconds=period_seconds)
        logger.info("Savings attribution complete: %d records written.", count)
    except Exception as e:
        logger.error("Savings attribution task failed: %s", e)
    logger.info("--- Finished savings attribution task ---")


async def compress_metrics() -> None:
    """Compress old raw metrics into hourly aggregates and prune stale data."""
    logger.info("--- Starting metrics compression task ---")
    try:
        from ..core.db import get_db_manager
        from ..core.metrics_compressor import MetricsCompressor

        compressor = MetricsCompressor(get_db_manager())
        stats = await compressor.run()
        # Also refresh the namespace cache
        await compressor.refresh_namespace_cache()
        logger.info(
            "Compression stats: %d hours compressed, %d raw pruned, %d hourly pruned",
            stats["hours_compressed"],
            stats["raw_rows_pruned"],
            stats["hourly_rows_pruned"],
        )
        # Compress savings ledger too
        try:
            from ..core.config import get_config as _cfg
            from ..core.factory import get_savings_ledger_repository as _savings_repo

            savings_repo = _savings_repo()
            cfg_c = _cfg()
            compressed = await savings_repo.compress_to_hourly(cutoff_hours=cfg_c.METRICS_COMPRESSION_AGE_HOURS)
            await savings_repo.prune_raw(retention_days=cfg_c.METRICS_RAW_RETENTION_DAYS)
            logger.info("Savings ledger compression: %d hourly rows upserted.", compressed)
        except Exception as e_s:
            logger.error("Savings ledger compression failed: %s", e_s)
    except Exception as e:
        logger.error("Metrics compression failed: %s", e)
    logger.info("--- Finished metrics compression task ---")


async def refresh_dashboard_summary() -> None:
    """Refresh the pre-computed dashboard summary table."""
    logger.info("--- Starting dashboard summary refresh task ---")
    try:
        from ..core.factory import (
            get_combined_metrics_repository,
            get_summary_repository,
            get_timeseries_cache_repository,
        )
        from ..core.summary_refresher import SummaryRefresher

        refresher = SummaryRefresher(
            metrics_repo=get_combined_metrics_repository(),
            summary_repo=get_summary_repository(),
            timeseries_cache_repo=get_timeseries_cache_repository(),
        )
        count = await refresher.run()
        logger.info("Dashboard summary refresh complete: %d rows upserted.", count)
    except Exception as e:
        logger.error("Dashboard summary refresh failed: %s", e)
    logger.info("--- Finished dashboard summary refresh task ---")


async def _async_start(last: Optional[str]):
    cfg = get_config()
    logging.basicConfig(
        level=cfg.LOG_LEVEL.upper(),
        format="%(asctime)s - %(levelname)s - %(message)s",
        force=True,
    )
    logger.info("🚀 Initializing GreenKube (Async)...")

    # Establish the database connection and run schema migrations eagerly.
    # This must happen before any scheduled task runs to guarantee that all
    # tables exist.  Previously this was only done for SQLite; PostgreSQL
    # relied on lazy initialization which caused race conditions where tasks
    # would query tables before setup_postgres() had created them.
    from ..core.db import get_db_manager

    await get_db_manager().connect()
    logger.info("✅ Database connection successful and schema is ready (%s).", cfg.DB_TYPE)

    scheduler = Scheduler()
    scheduler.add_job(collect_carbon_intensity_for_all_zones, interval_hours=1, skip_initial=True)

    scheduler.add_job_from_string(scheduled_write_metrics, cfg.PROMETHEUS_QUERY_RANGE_STEP, skip_initial=True)
    scheduler.add_job_from_string(analyze_nodes, cfg.NODE_ANALYSIS_INTERVAL, skip_initial=True)
    scheduler.add_job_from_string(attribute_recommendation_savings, cfg.PROMETHEUS_QUERY_RANGE_STEP, skip_initial=True)
    # Compress old raw metrics into hourly aggregates every hour
    scheduler.add_job(compress_metrics, interval_hours=1)
    # Refresh pre-computed dashboard summary every hour (after compression)
    scheduler.add_job(refresh_dashboard_summary, interval_hours=1)

    logger.info("📈 Starting scheduler...")
    logger.info("\nGreenKube is running. Press CTRL+C to exit.")

    # Initial Run
    logger.info("Running initial data collection for all zones...")
    await collect_carbon_intensity_for_all_zones()
    await analyze_nodes()
    # pass 'last' only to the initial run
    await async_write_combined_metrics_to_database(last=last)
    await attribute_recommendation_savings()
    await compress_metrics()
    await refresh_dashboard_summary()
    logger.info("Initial collection complete.")

    stop_event = asyncio.Event()

    def handle_sig():
        logger.info("\n🛑 Received shutdown signal...")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_sig)

    await stop_event.wait()
    await scheduler.stop()
    logger.info("🛑 Shutting down GreenKube service gracefully.")


@app.callback(invoke_without_command=True)
def start(
    ctx: typer.Context,
    last: Annotated[
        Optional[str],
        typer.Option("--last", help="Time range to collect (e.g., '10min', '2h', '7d', '3w', '1m' for month)."),
    ] = None,
) -> None:
    """
    Initialize the database (if needed) and start the scheduler loop.
    """
    if ctx.invoked_subcommand is not None:
        return

    try:
        asyncio.run(_async_start(last))
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.error("❌ An unexpected error occurred during startup: %s", e)
        logger.error("Startup failed: %s", traceback.format_exc())
        raise typer.Exit(code=1)
