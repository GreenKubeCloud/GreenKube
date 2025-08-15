# src/greenkube/cli.py
"""
This module provides the command-line interface (CLI) for GreenKube,
powered by the Typer library.

It orchestrates the collection, processing, and reporting of FinGreenOps data.
"""
import typer
from typing_extensions import Annotated

# Import the core components of the application
from .collectors.kepler_collector import KeplerCollector
from .collectors.opencost_collector import OpenCostCollector
from .core.calculator import CarbonCalculator
from .core.processor import DataProcessor
from .reporters.console_reporter import ConsoleReporter

# Create a Typer application instance
app = typer.Typer(
    name="greenkube",
    help="Measure, understand, and reduce the carbon footprint of your Kubernetes infrastructure.",
    add_completion=False
)

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
    
    # These values would typically come from a configuration file or cloud provider metadata
    carbon_calculator = CarbonCalculator(pue=1.5, grid_intensity_gco2e_per_kwh=50.0)
    
    # The DataProcessor orchestrates the entire workflow
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
        
    print("INFO: Generating report...")
    console_reporter.report(data=combined_data)
        
    print("INFO: Generating report...")
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