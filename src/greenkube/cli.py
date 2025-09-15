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
from .core.scheduler import Scheduler

# --- GreenKube Collector Imports ---
from .collectors.electricity_maps_collector import ElectricityMapsCollector
from .collectors.node_collector import NodeCollector
from .collectors.kepler_collector import KeplerCollector
from .collectors.opencost_collector import OpenCostCollector

# --- GreenKube Storage Imports (NOUVEAU) ---
from .storage.sqlite_repository import SQLiteCarbonIntensityRepository

# --- GreenKube Reporting and Processing Imports ---
from .core.calculator import CarbonCalculator
from .core.processor import DataProcessor
from .reporters.console_reporter import ConsoleReporter
from .utils.mapping_translator import get_emaps_zone_from_cloud_zone


app = typer.Typer(
    name="greenkube",
    help="Measure, understand, and reduce the carbon footprint of your Kubernetes infrastructure.",
    add_completion=False
)

def collect_carbon_intensity_for_all_zones():
    """
    Orchestre la collecte et la sauvegarde des données d'intensité carbone.
    """
    typer.echo("--- Starting hourly carbon intensity collection task ---")
    
    # --- Initialisation des composants nécessaires ---
    node_collector = NodeCollector()
    em_collector = ElectricityMapsCollector()
    # On instancie le repository qui va gérer la sauvegarde
    repository = SQLiteCarbonIntensityRepository()

    # --- Logique de traduction (inchangée) ---
    cloud_zones = node_collector.collect()
    if not cloud_zones:
        typer.secho("Warning: No node zones discovered.", fg=typer.colors.YELLOW)
        return

    emaps_zones = {get_emaps_zone_from_cloud_zone(cz) for cz in cloud_zones if get_emaps_zone_from_cloud_zone(cz) != "unknown"}
    if not emaps_zones:
        typer.secho("Warning: Could not map any cloud zone.", fg=typer.colors.YELLOW)
        return

    # --- Nouvelle logique : Collecter PUIS Sauvegarder ---
    for zone in emaps_zones:
        try:
            # 1. Le collecteur récupère les données
            history_data = em_collector.collect(zone=zone)
            if history_data:
                # 2. Le repository sauvegarde les données
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
    # ... (Cette fonction n'a pas besoin de changer)
    typer.echo("🚀 Initializing GreenKube...")
    try:
        get_db_connection()
        typer.secho("✅ Database connection successful and schema is ready.", fg=typer.colors.GREEN)
        
        scheduler = Scheduler()
        scheduler.add_job(collect_carbon_intensity_for_all_zones, interval_hours=1)
        
        typer.echo("📈 Starting scheduler...")
        typer.echo("\nGreenKube is running. Press CTRL+C to exit.")
        
        typer.echo("Running initial data collection for all zones...")
        collect_carbon_intensity_for_all_zones()
        typer.echo("Initial collection complete.")

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
    Affiche un rapport combiné des coûts et de l'empreinte carbone.
    """
    print("INFO: Initializing GreenKube FinGreenOps reporting tool...")
    
    # --- INJECTION DE DÉPENDANCES ---
    # 1. On crée le repository
    sqlite_repo = SQLiteCarbonIntensityRepository()
    
    # 2. On injecte le repository dans le calculateur
    carbon_calculator = CarbonCalculator(repository=sqlite_repo)

    # 3. On injecte le calculateur dans le processeur
    kepler_collector = KeplerCollector()
    opencost_collector = OpenCostCollector()
    processor = DataProcessor(
        energy_collector=kepler_collector,
        cost_collector=opencost_collector,
        calculator=carbon_calculator
    )
    
    console_reporter = ConsoleReporter()
    
    # --- Le reste de la logique est inchangé ---
    print("INFO: Running the data processing pipeline...")
    combined_data = processor.run()
    if namespace:
        print(f"INFO: Filtering results for namespace: {namespace}...")
        combined_data = [item for item in combined_data if item.namespace == namespace]
    if not combined_data:
        print(f"WARN: No data found for namespace '{namespace}'.")
        raise typer.Exit(code=1)
    
    print("INFO: Calling the reporter...")
    console_reporter.report(data=combined_data)

# ... (La commande export ne change pas)
@app.command()
def export(
    format: str = typer.Option("csv", help="The output format (e.g., 'csv', 'json')."),
    output: str = typer.Option("report.csv", help="The path to the output file.")
):
    typer.echo(f"Placeholder: Exporting data in {format} format to {output}")

if __name__ == "__main__":
    app()

