# src/greenkube/cli.py
"""
This module provides the command-line interface (CLI) for GreenKube,
powered by the Typer library.

It orchestrates the collection, processing, and reporting of FinGreenOps data.
"""
import typer
from typing_extensions import Annotated
import time

# --- GreenKube Core Imports ---
from .core.db import get_db_connection
from .core.config import config
from .core.scheduler import Scheduler

# --- GreenKube Collector Imports ---
from .collectors.electricity_maps_collector import ElectricityMapsCollector
from .collectors.node_collector import NodeCollector # Import the new collector
from .collectors.kepler_collector import KeplerCollector
from .collectors.opencost_collector import OpenCostCollector

# --- GreenKube Reporting and Processing Imports ---
from .core.calculator import CarbonCalculator
from .core.processor import DataProcessor
from .reporters.console_reporter import ConsoleReporter


app = typer.Typer(
    name="greenkube",
    help="Measure, understand, and reduce the carbon footprint of your Kubernetes infrastructure.",
    add_completion=False
)

def collect_carbon_intensity_for_all_zones():
    """
    Orchestrates the collection of carbon intensity data for all discovered zones.
    """
    typer.echo("--- Starting hourly carbon intensity collection task ---")
    node_collector = NodeCollector()
    unique_zones = node_collector.get_zones()

    if not unique_zones:
        typer.secho("Warning: No node zones discovered. Skipping carbon intensity collection.", fg=typer.colors.YELLOW)
        return

    for zone in unique_zones:
        try:
            em_collector = ElectricityMapsCollector(zone=zone)
            em_collector.collect()
        except Exception as e:
            typer.secho(f"Failed to collect data for zone {zone}: {e}", fg=typer.colors.RED)
    typer.echo("--- Finished carbon intensity collection task ---")


@app.command()
def start():
    """
    Initialize the database and start the GreenKube data collection service.
    """
    typer.echo("🚀 Initializing GreenKube...")
    try:
        # Database connection is triggered by the initial import
        conn = get_db_connection()
        if conn:
            typer.secho("✅ Database connection successful and schema is ready.", fg=typer.colors.GREEN)
        else:
            typer.secho("❌ Failed to establish database connection.", fg=typer.colors.RED)
            raise typer.Exit(code=1)

        # --- Initialize and Configure Scheduler ---
        scheduler = Scheduler()
        # Schedule the master collection function to run every hour
        scheduler.add_job(collect_carbon_intensity_for_all_zones, interval_hours=1)
        
        typer.echo("📈 Starting scheduler...")
        typer.echo("\nGreenKube is running. Press CTRL+C to exit.")
        
        # --- Run Initial Collection Immediately on Startup ---
        typer.echo("Running initial data collection for all zones...")
        collect_carbon_intensity_for_all_zones()
        typer.echo("Initial collection complete.")

        # --- Main Service Loop ---
        while True:
            scheduler.run_pending()
            time.sleep(1)

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
    Displays a summary carbon footprint and cost report for all namespaces.
    """
    print("INFO: Initializing GreenKube FinGreenOps reporting tool...")
    kepler_collector = KeplerCollector()
    opencost_collector = OpenCostCollector()
    carbon_calculator = CarbonCalculator()
    processor = DataProcessor(
        energy_collector=kepler_collector,
        cost_collector=opencost_collector,
        calculator=carbon_calculator
    )
    console_reporter = ConsoleReporter()
    print("INFO: Running the data processing pipeline...")
    combined_data = processor.run()
    if namespace:
        print(f"INFO: Filtering results for namespace: {namespace}...")
        combined_data = [item for item in combined_data if item.namespace == namespace]
    if not combined_data:
        print(f"WARN: No data found for namespace '{namespace}'. Please check if the namespace is correct and has active workloads.")
        raise typer.Exit(code=1)
    print("INFO: Calling the reporter...")
    console_reporter.report(data=combined_data)


@app.command()
def export(
    format: str = typer.Option("csv", help="The output format (e.g., 'csv', 'json')."),
    output: str = typer.Option("report.csv", help="The path to the output file.")
):
    """
    Export raw data in a specified format (e.g., CSV, JSON).
    
    (This is a placeholder for a future implementation)
    """
    typer.echo(f"Placeholder: Exporting data in {format} format to {output}")
    # In a real implementation, you would run the processor and then have
    # different reporters (e.g., CsvReporter, JsonReporter) to handle the export.


if __name__ == "__main__":
    app()