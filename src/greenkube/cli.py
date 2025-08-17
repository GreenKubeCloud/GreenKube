# src/greenkube/cli.py
"""
This module provides the command-line interface (CLI) for GreenKube,
powered by the Typer library.

It orchestrates the collection, processing, and reporting of FinGreenOps data.
"""
import typer
from typing_extensions import Annotated
import time

from .collectors.kepler_collector import KeplerCollector
from .collectors.opencost_collector import OpenCostCollector
from .core.calculator import CarbonCalculator
from .core.processor import DataProcessor
from .reporters.console_reporter import ConsoleReporter
# By importing from the db module, we trigger the global DatabaseManager instance,
# which connects to the database and initializes the schema upon script startup.
from .core.db import get_db_connection

app = typer.Typer(
    name="greenkube",
    help="Measure, understand, and reduce the carbon footprint of your Kubernetes infrastructure.",
    add_completion=False
)

@app.command()
def start():
    """
    Initialize the database and start the GreenKube service.

    This command ensures the database is connected and the schema is correctly
    set up. It will be the future home for the scheduled data collectors.
    """
    typer.echo("🚀 Initializing GreenKube...")
    try:
        # The import above has already triggered the database setup.
        # We can get the connection here to confirm it was successful.
        conn = get_db_connection()
        if conn:
            typer.secho("✅ Database connection successful and schema is ready.", fg=typer.colors.GREEN)
        else:
            # This case should ideally be caught by an exception during initialization
            typer.secho("❌ Failed to establish database connection.", fg=typer.colors.RED)
            raise typer.Exit(code=1)

        typer.echo("📈 Starting collectors... (Scheduler to be implemented in a future step)")
        # --- FUTURE IMPLEMENTATION ---
        # scheduler = Scheduler()
        # scheduler.add_job(kepler_collector.collect, 'interval', minutes=5)
        # scheduler.add_job(opencost_collector.collect, 'interval', hours=1)
        # scheduler.add_job(electricity_maps_collector.collect, 'interval', hours=1)
        # scheduler.start()
        # ---------------------------

        typer.echo("\nGreenKube is running. Press CTRL+C to exit.")
        # This loop simulates a running service and keeps the script alive.
        while True:
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

    # --- 1. Initialization of Components ---
    # In a real-world scenario, these components would be configured with
    # API endpoints, authentication, etc. For now, they use mocked data.
    kepler_collector = KeplerCollector()
    opencost_collector = OpenCostCollector()
    carbon_calculator = CarbonCalculator()
    
    processor = DataProcessor(
        energy_collector=kepler_collector,
        cost_collector=opencost_collector,
        calculator=carbon_calculator
    )
    
    console_reporter = ConsoleReporter()

    # --- 2. Run the Data Processing Pipeline ---
    print("INFO: Running the data processing pipeline...")
    combined_data = processor.run()

    # --- 3. Filter Data if a Namespace is Specified ---
    if namespace:
        print(f"INFO: Filtering results for namespace: {namespace}...")
        combined_data = [item for item in combined_data if item.namespace == namespace]

    # --- 4. Report the Final Data ---
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