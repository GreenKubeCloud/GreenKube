"""
This module provides the command-line interface (CLI) for GreenKube,
powered by the Typer library.

It orchestrates the collection, processing, and reporting of FinGreenOps data.
"""
import typer
from typing_extensions import Annotated
import time

# --- GreenKube Core Imports ---
from .core.scheduler import Scheduler
from .core.config import config

# --- GreenKube Collector Imports ---
from .collectors.electricity_maps_collector import ElectricityMapsCollector
from .collectors.node_collector import NodeCollector
from .collectors.kepler_collector import KeplerCollector
from .collectors.opencost_collector import OpenCostCollector

# --- GreenKube Storage Imports ---
from .storage.base_repository import CarbonIntensityRepository
from .storage.sqlite_repository import SQLiteCarbonIntensityRepository
from .storage.elasticsearch_repository import ElasticsearchCarbonIntensityRepository

# --- GreenKube Reporting and Processing Imports ---
from .core.calculator import CarbonCalculator
from .core.processor import DataProcessor
from .reporters.console_reporter import ConsoleReporter
# --- Use the correct translator function name ---
from .utils.mapping_translator import get_emaps_zone_from_cloud_zone
# ----------------------------------------------


app = typer.Typer(
    name="greenkube",
    help="Measure, understand, and reduce the carbon footprint of your Kubernetes infrastructure.",
    add_completion=False
)

def get_repository() -> CarbonIntensityRepository:
    """
    Factory function to get the appropriate repository based on config.
    """
    if config.DB_TYPE == "elasticsearch":
        typer.echo("Using Elasticsearch repository.")
        return ElasticsearchCarbonIntensityRepository()
    elif config.DB_TYPE == "sqlite":
        typer.echo("Using SQLite repository.")
        # --- Need to pass the connection for SQLite ---
        from .core.db import db_manager # Import db_manager locally for SQLite
        return SQLiteCarbonIntensityRepository(db_manager.get_connection())
        # ---------------------------------------------
    else:
        # Handle postgres or other cases if needed
        raise NotImplementedError(f"Repository for DB_TYPE '{config.DB_TYPE}' not implemented.")


def collect_carbon_intensity_for_all_zones():
    """
    Orchestrates the collection and saving of carbon intensity data.
    """
    typer.echo("--- Starting hourly carbon intensity collection task ---")

    # --- Initialize necessary components ---
    node_collector = NodeCollector()
    em_collector = ElectricityMapsCollector()
    # Use the factory to get the correct repository
    try:
        repository = get_repository()
    except Exception as e:
        typer.secho(f"ERROR: Failed to initialize repository: {e}", fg=typer.colors.RED)
        return # Stop if repository fails

    # --- Mapping logic (unchanged) ---
    try:
        nodes_zones_map = node_collector.collect() # Renamed variable for clarity
        if not nodes_zones_map:
            typer.secho("Warning: No node zones discovered.", fg=typer.colors.YELLOW)
            # Decide if we should try a default zone or stop
            # For now, let's stop if no nodes are found.
            return
    except Exception as e:
         typer.secho(f"ERROR: Failed to collect node zones: {e}", fg=typer.colors.RED)
         return # Stop if node collection fails

    # Extract unique cloud zones from the values of the map
    unique_cloud_zones = set(nodes_zones_map.values())
    emaps_zones = set()
    for cz in unique_cloud_zones:
        emz = get_emaps_zone_from_cloud_zone(cz)
        if emz and emz != "unknown": # Check for None and "unknown"
             emaps_zones.add(emz)
        else:
            typer.secho(f"Warning: Could not map cloud zone '{cz}' to an Electricity Maps zone.", fg=typer.colors.YELLOW)


    if not emaps_zones:
        typer.secho("Warning: No mappable Electricity Maps zones found based on node discovery.", fg=typer.colors.YELLOW)
        # Optionally, fallback to config.DEFAULT_ZONE if desired
        # emaps_zones = {config.DEFAULT_ZONE}
        # typer.echo(f"Falling back to default zone: {config.DEFAULT_ZONE}")
        return # Stop for now if no zones mapped


    # --- Collection and saving logic (unchanged) ---
    for zone in emaps_zones:
        try:
            history_data = em_collector.collect(zone=zone)
            if history_data:
                saved_count = repository.save_history(history_data, zone=zone)
                typer.echo(f"Successfully saved {saved_count} new records for zone: {zone}")
            else:
                typer.echo(f"No new data to save for zone: {zone}")
        except Exception as e:
            typer.secho(f"Failed to process data for zone {zone}: {e}", fg=typer.colors.RED)

    typer.echo("--- Finished carbon intensity collection task ---")


@app.command()
def start():
    """
    Initialize the database and start the GreenKube data collection service.
    """
    typer.echo("🚀 Initializing GreenKube...")
    try:
        # For SQLite, initialize the DB schema if needed
        if config.DB_TYPE == "sqlite":
            # --- Import db_manager locally for SQLite ---
            from .core.db import db_manager
            # -----------------------------------------
            db_manager.setup_sqlite() # Ensure schema exists
            typer.secho("✅ SQLite Database connection successful and schema is ready.", fg=typer.colors.GREEN)
        # Add checks or initial setup for Elasticsearch if necessary in the future

        scheduler = Scheduler()
        scheduler.add_job(collect_carbon_intensity_for_all_zones, interval_hours=1)

        typer.echo("📈 Starting scheduler...")
        typer.echo("\nGreenKube is running. Press CTRL+C to exit.")

        typer.echo("Running initial data collection for all zones...")
        collect_carbon_intensity_for_all_zones()
        typer.echo("Initial collection complete.")

        while True:
            scheduler.run_pending()
            time.sleep(60) # Check every minute instead of every second

    except KeyboardInterrupt:
        typer.echo("\n🛑 Shutting down GreenKube service.")
        raise typer.Exit()
    except Exception as e:
        typer.secho(f"❌ An unexpected error occurred during startup: {e}", fg=typer.colors.RED)
        raise typer.Exit(code=1)


@app.command()
def report(
    namespace: Annotated[str, typer.Option(
        help="Display a detailed report for a specific namespace."
    )] = None
):
    """
    Displays a combined report of costs and carbon footprint.
    """
    print("INFO: Initializing GreenKube FinGreenOps reporting tool...")

    try: # Add error handling for initialization
        # --- DEPENDENCY INJECTION (UPDATED) ---
        # 1. Get the repository using the factory
        repository = get_repository()

        # 2. Instantiate all collectors
        kepler_collector = KeplerCollector()
        opencost_collector = OpenCostCollector()
        node_collector = NodeCollector() # Instantiate NodeCollector

        # 3. Instantiate the calculator, injecting the repository
        carbon_calculator = CarbonCalculator(repository=repository)

        # 4. Instantiate the processor, injecting all dependencies
        processor = DataProcessor(
            kepler_collector=kepler_collector,     # Renamed argument
            opencost_collector=opencost_collector, # Renamed argument
            node_collector=node_collector,         # Add NodeCollector
            repository=repository,                 # Add Repository
            calculator=carbon_calculator           # Pass Calculator instance
        )

        console_reporter = ConsoleReporter()
        # --- END DEPENDENCY INJECTION ---

        print("INFO: Running the data processing pipeline...")
        combined_data = processor.run() # This now handles internal errors more gracefully

        if not combined_data:
             print("WARN: No combined data was generated by the processor.")
             # Decide if this is an error or just an empty report state
             raise typer.Exit(code=0) # Exit cleanly if no data


        if namespace:
            print(f"INFO: Filtering results for namespace: {namespace}...")
            original_count = len(combined_data)
            combined_data = [item for item in combined_data if item.namespace == namespace]
            if not combined_data:
                # Use typer.secho for warnings/errors
                typer.secho(f"WARN: No data found for namespace '{namespace}' after processing {original_count} total items.", fg=typer.colors.YELLOW)
                raise typer.Exit(code=0) # Exit cleanly, just no data for this namespace

        print("INFO: Calling the reporter...")
        console_reporter.report(data=combined_data)

    except Exception as e:
        # Catch errors during initialization or processing
        typer.secho(f"❌ An error occurred during report generation: {e}", fg=typer.colors.RED)
        # Optionally add more specific error handling based on exception types
        import traceback
        traceback.print_exc() # Print full traceback for debugging
        raise typer.Exit(code=1)


@app.command()
def export(
    format: str = typer.Option("csv", help="The output format (e.g., 'csv', 'json')."),
    output: str = typer.Option("report.csv", help="The path to the output file.")
):
    """ Exports the combined report data to a file. (Placeholder) """
    typer.echo(f"Placeholder: Exporting data in {format} format to {output}")
    # Implementation would be similar to 'report', but using a file exporter instead of ConsoleReporter

if __name__ == "__main__":
    app()

