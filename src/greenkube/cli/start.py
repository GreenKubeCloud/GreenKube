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
    except Exception as e:
        logger.error("Failed to collect node zones: %s", e)
        return

    unique_zone_providers = {
        (node_info.zone, node_info.cloud_provider) for node_info in nodes_info.values() if node_info.zone
    }
    emaps_zones: Set[str] = set()
    for cz, provider in unique_zone_providers:
        emz = get_emaps_zone_from_cloud_zone(cz, provider=provider)
        if emz and emz != "unknown":
            emaps_zones.add(emz)
        else:
            logger.warning("Could not map cloud zone '%s' (provider: %s) to an Electricity Maps zone.", cz, provider)

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

    logger.info("--- Finished carbon intensity collection task ---")
    if "node_collector" in locals():
        await node_collector.close()
    if "em_collector" in locals():
        await em_collector.close()


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


async def _async_start(last: Optional[str]):
    cfg = get_config()
    logging.basicConfig(
        level=cfg.LOG_LEVEL.upper(),
        format="%(asctime)s - %(levelname)s - %(message)s",
    )
    logger.info("🚀 Initializing GreenKube (Async)...")

    # For SQLite, establish the connection and initialize the DB schema
    if cfg.DB_TYPE == "sqlite":
        from ..core.db import get_db_manager

        await get_db_manager().connect()
        logger.info("✅ SQLite Database connection successful and schema is ready.")

    scheduler = Scheduler()
    scheduler.add_job(collect_carbon_intensity_for_all_zones, interval_hours=1)

    scheduler.add_job_from_string(scheduled_write_metrics, cfg.PROMETHEUS_QUERY_RANGE_STEP)
    scheduler.add_job_from_string(analyze_nodes, cfg.NODE_ANALYSIS_INTERVAL)

    logger.info("📈 Starting scheduler...")
    logger.info("\nGreenKube is running. Press CTRL+C to exit.")

    # Initial Run
    logger.info("Running initial data collection for all zones...")
    await collect_carbon_intensity_for_all_zones()
    await analyze_nodes()
    # pass 'last' only to the initial run
    await async_write_combined_metrics_to_database(last=last)
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
