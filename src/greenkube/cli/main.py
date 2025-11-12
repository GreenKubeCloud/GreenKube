# src/greenkube/cli/main.py
"""
This module is the main entry point for the GreenKube CLI.

It aggregates all commands from the submodules (report, recommend, etc.)
and defines the `start` command for the scheduler.
"""

import logging
import time
import traceback

import typer

from ..collectors.electricity_maps_collector import ElectricityMapsCollector
from ..collectors.node_collector import NodeCollector
from ..core.config import config
from ..core.factory import get_repository
from ..core.scheduler import Scheduler
from ..utils.mapping_translator import get_emaps_zone_from_cloud_zone
from . import recommend, report

# --- Setup Logger ---
logging.basicConfig(
    level=config.LOG_LEVEL.upper(),
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


app = typer.Typer(
    name="greenkube",
    help="Measure, understand, and reduce the carbon footprint of your Kubernetes infrastructure.",
    add_completion=False,
)

# Register command sub-apps
app.add_typer(report.app, name="report")
app.add_typer(recommend.app, name="recommend")


def collect_carbon_intensity_for_all_zones():
    """
    Orchestrates the collection and saving of carbon intensity data.
    """
    logger.info("--- Starting hourly carbon intensity collection task ---")
    try:
        repository = get_repository()
        node_collector = NodeCollector()
        em_collector = ElectricityMapsCollector()
    except Exception as e:
        logger.error(f"Failed to initialize components for intensity collection: {e}")
        return

    try:
        nodes_zones_map = node_collector.collect()  # Renamed variable
        if not nodes_zones_map:
            logger.warning("No node zones discovered.")
            return
    except Exception as e:
        logger.error(f"Failed to collect node zones: {e}")
        return  # Stop if node collection fails

    # Extract unique cloud zones from the values of the map
    unique_cloud_zones = set(nodes_zones_map.values())
    emaps_zones = set()
    for cz in unique_cloud_zones:
        emz = get_emaps_zone_from_cloud_zone(cz)
        if emz and emz != "unknown":  # Check for None and "unknown"
            emaps_zones.add(emz)
        else:
            logger.warning(f"Could not map cloud zone '{cz}' to an Electricity Maps zone.")

    if not emaps_zones:
        logger.warning("No mappable Electricity Maps zones found based on node discovery.")
        return  # Stop for now if no zones mapped

    # --- Collection and saving logic (unchanged) ---
    for zone in emaps_zones:
        try:
            history_data = em_collector.collect(zone=zone)
            if history_data:
                saved_count = repository.save_history(history_data, zone=zone)
                logger.info(f"Successfully saved {saved_count} new records for zone: {zone}")
            else:
                logger.info(f"No new data to save for zone: {zone}")
        except Exception as e:
            logger.error(f"Failed to process data for zone {zone}: {e}")

    logger.info("--- Finished carbon intensity collection task ---")


@app.command()
def start():
    """
    Initialize the database and start the GreenKube data collection service.
    """
    logger.info("🚀 Initializing GreenKube...")
    try:
        # For SQLite, initialize the DB schema if needed
        if config.DB_TYPE == "sqlite":
            from ..core.db import db_manager

            db_manager.setup_sqlite()  # Ensure schema exists
            logger.info("✅ SQLite Database connection successful and schema is ready.")

        scheduler = Scheduler()
        scheduler.add_job(collect_carbon_intensity_for_all_zones, interval_hours=1)

        logger.info("📈 Starting scheduler...")
        logger.info("\nGreenKube is running. Press CTRL+C to exit.")

        logger.info("Running initial data collection for all zones...")
        collect_carbon_intensity_for_all_zones()
        logger.info("Initial collection complete.")

        while True:
            scheduler.run_pending()
            time.sleep(60)

    except KeyboardInterrupt:
        logger.info("\n🛑 Shutting down GreenKube service.")
        raise typer.Exit()
    except Exception as e:
        logger.error(f"❌ An unexpected error occurred during startup: {e}")
        logger.error("Startup failed: %s", traceback.format_exc())
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
