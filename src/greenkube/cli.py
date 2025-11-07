# src/greenkube/cli.py
"""
This module provides the command-line interface (CLI) for GreenKube,
powered by the Typer library.

It orchestrates the collection, processing, and reporting of FinGreenOps data.
"""

import logging
import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional

import typer
from typer.core import TyperGroup
from typing_extensions import Annotated

# --- GreenKube Collector Imports ---
from .collectors.electricity_maps_collector import ElectricityMapsCollector
from .collectors.node_collector import NodeCollector
from .collectors.opencost_collector import OpenCostCollector
from .collectors.pod_collector import PodCollector
from .collectors.prometheus_collector import PrometheusCollector
from .core.calculator import CarbonCalculator
from .core.config import config
from .core.processor import DataProcessor
from .core.recommender import Recommender

# --- GreenKube Core Imports ---
from .core.scheduler import Scheduler
from .energy.estimator import BasicEstimator

# --- GreenKube Reporting and Processing Imports ---
from .reporters.console_reporter import ConsoleReporter

# --- GreenKube Storage Imports ---
from .storage.base_repository import CarbonIntensityRepository
from .storage.elasticsearch_repository import ElasticsearchCarbonIntensityRepository
from .storage.sqlite_repository import SQLiteCarbonIntensityRepository
from .utils.mapping_translator import get_emaps_zone_from_cloud_zone

# --- Setup Logger ---
logging.basicConfig(level=config.LOG_LEVEL.upper(), format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


class HelpOnUnknown(TyperGroup):
    """Custom Click Group that prints dynamic help when an unknown command is used."""

    def resolve_command(self, ctx, args):
        try:
            return super().resolve_command(ctx, args)
        except Exception:
            # Print dynamic help and re-raise to keep Click behavior
            try:
                help_cmd()
            except Exception:
                pass
            raise


app = typer.Typer(
    name="greenkube",
    help="Measure, understand, and reduce the carbon footprint of your Kubernetes infrastructure.",
    add_completion=False,
    cls=HelpOnUnknown,
)

# Register sub-apps under different names to avoid shadowing top-level commands
from .commands import recommend as recommend_module  # noqa: E402
from .commands import report as report_module  # noqa: E402

# Register sub-apps under alternative names
app.add_typer(report_module.app, name="reports")
app.add_typer(recommend_module.app, name="recommendations")

# The top-level report command is implemented in the sub-apps. We avoid
# keeping lightweight wrappers here that duplicate behavior and cause
# redefinition lint errors.


def get_repository() -> CarbonIntensityRepository:
    """
    Factory function to get the appropriate repository based on config.
    """
    import os

    # Allow tests to override DB_TYPE via environment variables (monkeypatch).
    db_type = os.getenv("DB_TYPE", config.DB_TYPE)

    if db_type == "elasticsearch":
        logger.info("Using Elasticsearch repository.")
        try:
            from .storage import elasticsearch_repository as es_mod

            es_mod.setup_connection()
        except Exception as e:
            logger.error(f"Failed to setup Elasticsearch connection: {e}")
        return ElasticsearchCarbonIntensityRepository()
    elif db_type == "sqlite":
        logger.info("Using SQLite repository.")
        from .core.db import db_manager

        return SQLiteCarbonIntensityRepository(db_manager.get_connection())
    else:
        raise NotImplementedError(f"Repository for DB_TYPE '{config.DB_TYPE}' not implemented.")


def get_processor() -> DataProcessor:
    """
    Factory function to instantiate and return a fully configured DataProcessor.
    """
    logger.info("Initializing data collectors and processor...")
    try:
        # 1. Get the repository
        repository = get_repository()

        # 2. Instantiate all collectors
        prometheus_collector = PrometheusCollector(settings=config)
        opencost_collector = OpenCostCollector()
        node_collector = NodeCollector()
        pod_collector = PodCollector()

        # 3. Instantiate the calculator and estimator
        carbon_calculator = CarbonCalculator(repository=repository)
        estimator = BasicEstimator(settings=config)

        # 4. Instantiate and return the processor
        processor = DataProcessor(
            prometheus_collector=prometheus_collector,
            opencost_collector=opencost_collector,
            node_collector=node_collector,
            pod_collector=pod_collector,
            repository=repository,
            calculator=carbon_calculator,
            estimator=estimator,
        )
        return processor
    except Exception as e:
        logger.error(f"An error occurred during processor initialization: {e}")
        logger.error("Processor initialization failed: %s", traceback.format_exc())
        raise typer.Exit(code=1)


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

    # ... (rest of the function is unchanged) ...
    try:
        nodes_zones_map = node_collector.collect()  # Renamed variable for clarity
        if not nodes_zones_map:
            logger.warning("No node zones discovered.")
            # Decide if we should try a default zone or stop
            # For now, let's stop if no nodes are found.
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
        # Optionally, fallback to config.DEFAULT_ZONE if desired
        # emaps_zones = {config.DEFAULT_ZONE}
        # logger.info(f"Falling back to default zone: {config.DEFAULT_ZONE}")
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
            # --- Import db_manager locally for SQLite ---
            from .core.db import db_manager

            # -----------------------------------------
            db_manager.setup_sqlite()  # Ensure schema exists
            logger.info("✅ SQLite Database connection successful and schema is ready.")
        # Add checks or initial setup for Elasticsearch if necessary in the future

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


@app.command()
def report(
    namespace: Annotated[
        Optional[str],
        typer.Option(help="Display a detailed report for a specific namespace."),
    ] = None,
    today: Annotated[bool, typer.Option(help="Report from midnight UTC to now")] = False,
    days: Annotated[int, typer.Option(help="Number of days to include (integer)")] = 0,
    hours: Annotated[int, typer.Option(help="Number of hours to include (integer)")] = 0,
    minutes: Annotated[int, typer.Option(help="Number of minutes to include (integer)")] = 0,
    weeks: Annotated[int, typer.Option(help="Number of weeks to include (integer)")] = 0,
    monthly: Annotated[bool, typer.Option(help="Aggregate results by month (UTC)")] = False,
    yearly: Annotated[bool, typer.Option(help="Aggregate results by year (UTC)")] = False,
    format: Annotated[str, typer.Option(help="Output format when --output is provided (csv|json)")] = "csv",
    output: Annotated[
        Optional[str],
        typer.Option(help="Path to output file (CSV or JSON) when provided"),
    ] = None,
):
    """
    Displays a combined report of costs and carbon footprint.

    This command now supports the same range-related flags as `report-range`.
    If any range flag is supplied, it delegates to `report_range` to run
    the range-based flow. Without range flags it behaves like the old `report`.
    """
    # If any range-related option is provided, delegate to report_range
    if any((today, days, hours, minutes, weeks, monthly, yearly, output)):
        # Call the existing report_range implementation to handle ranged reports.
        # Typer will handle rendering and exits, so just forward the values.
        return report_range(
            namespace=namespace,
            today=today,
            days=days,
            hours=hours,
            minutes=minutes,
            weeks=weeks,
            monthly=monthly,
            yearly=yearly,
            format=format,
            output=output,
        )

    logger.info("Initializing GreenKube FinGreenOps reporting tool...")
    try:
        processor = get_processor()
        console_reporter = ConsoleReporter()

        logger.info("Running the data processing pipeline...")
        combined_data = processor.run()

        if not combined_data:
            logger.warning("No combined data was generated by the processor.")
            raise typer.Exit(code=0)

        if namespace:
            logger.info(f"Filtering results for namespace: {namespace}...")
            original_count = len(combined_data)
            combined_data = [item for item in combined_data if item.namespace == namespace]
            if not combined_data:
                logger.warning(
                    f"No data found for namespace '{namespace}' after processing {original_count} total items."
                )
                raise typer.Exit(code=0)

        # Call console reporter to keep console output behavior
        console_reporter.report(data=combined_data)

        # If user provided an output path or format, delegate rendering to reporters/exporters
        if output:
            # Delegate to existing export command implementation
            return export(format=format, output=output)

        # Default behavior: write CSV to project-root data/greenkube-report.csv
        import os

        from .exporters.csv_exporter import CSVExporter

        data_dir = os.path.abspath(os.path.join(os.getcwd(), "data"))
        os.makedirs(data_dir, exist_ok=True)
        out_path = os.path.join(data_dir, CSVExporter.DEFAULT_FILENAME)

        # Convert CombinedMetric objects to dicts if necessary
        try:
            rows = [r.__dict__ if hasattr(r, "__dict__") else dict(r) for r in combined_data]
        except Exception:
            rows = list(combined_data)

        exporter = CSVExporter()
        written = exporter.export(rows, out_path)
        logger.info(f"Exported default CSV report to {written}")

    except typer.Exit:
        raise
    except Exception as e:
        logger.error(f"An error occurred during report generation: {e}")
        logger.error("Report generation failed: %s", traceback.format_exc())
        raise typer.Exit(code=1)


@app.command()
def report_range(
    namespace: Annotated[str, typer.Option(help="Namespace filter")] = None,
    today: Annotated[bool, typer.Option(help="Report from midnight UTC to now")] = False,
    days: Annotated[int, typer.Option(help="Number of days to include (integer)")] = 0,
    hours: Annotated[int, typer.Option(help="Number of hours to include (integer)")] = 0,
    minutes: Annotated[int, typer.Option(help="Number of minutes to include (integer)")] = 0,
    weeks: Annotated[int, typer.Option(help="Number of weeks to include (integer)")] = 0,
    monthly: Annotated[bool, typer.Option(help="Aggregate results by month (UTC)")] = False,
    yearly: Annotated[bool, typer.Option(help="Aggregate results by year (UTC)")] = False,
    format: Annotated[str, typer.Option(help="Output format when --output is provided (csv|json)")] = "csv",
    output: Annotated[
        Optional[str],
        typer.Option(help="Path to output file (CSV or JSON) when provided"),
    ] = None,
):
    """Produce a simple range report. Use --today to report from UTC midnight."""
    # Validate exclusive flags
    if monthly and yearly:
        raise typer.BadParameter("--monthly and --yearly are mutually exclusive")
    # Build start/end datetimes
    end = datetime.now(timezone.utc)
    if today:
        start = end.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        if monthly and not any((days, hours, minutes, weeks)):
            start = end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        elif yearly and not any((days, hours, minutes, weeks)):
            start = end.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        else:
            delta = timedelta(days=days, hours=hours, minutes=minutes, weeks=weeks)
            if delta.total_seconds() <= 0:
                raise typer.BadParameter("Please provide a positive time range via --days/--hours/... or use --today")
            start = end - delta

    try:
        processor = get_processor()
        console = ConsoleReporter()
        # Delegate range processing to DataProcessor
        combined = processor.run_range(
            start=start,
            end=end,
            step=None,
            namespace=namespace,
            monthly=monthly,
            yearly=yearly,
            output=output,
            fmt=format,
        )

        if not combined:
            logger.warning("No combined data was generated for the requested range.")
            # Let the reporter handle empty result sets
            console.report([])
            return

        # If an output was requested, export to the requested format/path instead of printing to console
        if output:
            # Determine requested format: if output is 'csv' or 'json', treat it as format shortcut
            desired_fmt = format
            out_path = output
            if isinstance(output, str) and output.lower() in ("csv", "json"):
                desired_fmt = output.lower()
                import os

                from .exporters.csv_exporter import CSVExporter as _CSV
                from .exporters.json_exporter import JSONExporter as _JSON

                exporter_cls = _CSV if desired_fmt == "csv" else _JSON
                data_dir = os.path.abspath(os.path.join(os.getcwd(), "data"))
                os.makedirs(data_dir, exist_ok=True)
                out_path = os.path.join(data_dir, exporter_cls.DEFAULT_FILENAME)

            # Convert combined items to plain dicts where possible
            try:
                rows = [r.__dict__ if hasattr(r, "__dict__") else dict(r) for r in combined]
            except Exception:
                rows = list(combined)

            # Choose exporter and write
            if desired_fmt == "csv":
                from .exporters.csv_exporter import CSVExporter

                exporter = CSVExporter()
            else:
                from .exporters.json_exporter import JSONExporter

                exporter = JSONExporter()

            written = exporter.export(rows, out_path)
            logger.info(f"Exported report to {written}")
            return

        console.report(combined)

    except typer.Exit:
        raise
    except Exception as e:
        logger.error(f"An error occurred during ranged report generation: {e}")
        logger.error("Ranged report generation failed: %s", traceback.format_exc())
        raise typer.Exit(code=1)


@app.command(name="help")
def help_cmd():
    """Show available commands and dynamic usage information.

    This prints the same help text as `greenkube --help` by delegating to
    Click/Typer's help formatter using a synthetic context.
    """
    # Deterministic, test-friendly listing of command names and help
    print("GreenKube available commands:")
    try:
        # Typer exposes a mapping of registered_commands in some versions
        cmds = getattr(app, "registered_commands", None)
        if cmds:
            for name in sorted(cmds.keys()):
                cmd = cmds[name]
                help_text = getattr(cmd, "help", "") or ""
                print(f" - {name}: {help_text}")
        else:
            # Fallback: print top-level Typer command names
            for sub in getattr(app, "commands", []):
                print(f" - {sub}")
    except Exception:
        pass
    print("\nUse `greenkube <command> --help` for details on a command.")


@app.command()
def recommend(
    namespace: Annotated[str, typer.Option(help="Display recommendations for a specific namespace.")] = None,
):
    """
    Analyzes data and provides optimization recommendations.
    """
    logger.info("Initializing GreenKube Recommender...")

    try:
        # 1. Get the processed data
        processor = get_processor()
        logger.info("Running the data processing pipeline...")
        combined_data = processor.run()

        if not combined_data:
            logger.warning("No combined data was generated by the processor. Cannot generate recommendations.")
            raise typer.Exit(code=0)

        # 2. Filter by namespace if provided
        if namespace:
            logger.info(f"Filtering results for namespace: {namespace}...")
            combined_data = [item for item in combined_data if item.namespace == namespace]
            if not combined_data:
                logger.warning(f"No data found for namespace '{namespace}'.")
                raise typer.Exit(code=0)

        # 3. Instantiate Recommender and Reporter
        recommender = Recommender()  # Uses default thresholds
        console_reporter = ConsoleReporter()

        logger.info("Generating recommendations...")

        # 4. Generate recommendations
        zombie_recs = recommender.generate_zombie_recommendations(combined_data)
        rightsizing_recs = recommender.generate_rightsizing_recommendations(combined_data)

        all_recs = zombie_recs + rightsizing_recs

        if not all_recs:
            logger.info("\n✅ All systems look optimized! No recommendations to display.")
            return
        # 5. Report recommendations
        logger.info(f"Found {len(all_recs)} recommendations.")
        # Call the reporter's recommendation-specific method
        # ConsoleReporter exposes `report_recommendations` for this purpose.
        # Prefer calling `report` with a 'recommendations' kwarg when present
        if hasattr(console_reporter, "report"):
            try:
                console_reporter.report(data=combined_data, recommendations=all_recs)
            except Exception:
                # if report exists but fails, try fallback
                if hasattr(console_reporter, "report_recommendations"):
                    try:
                        console_reporter.report_recommendations(all_recs)
                    except Exception:
                        pass
        elif hasattr(console_reporter, "report_recommendations"):
            console_reporter.report_recommendations(all_recs)

    except typer.Exit:
        raise
    except Exception as e:
        logger.error(f"An error occurred during recommendation generation: {e}")
        logger.error("Recommendation generation failed: %s", traceback.format_exc())
        raise typer.Exit(code=1)


@app.command()
def export(
    format: str = typer.Option("csv", help="The output format (e.g., 'csv', 'json')."),
    output: Optional[str] = typer.Option(None, help="The path to the output file."),
):
    """Exports the combined report data to a file using the CSV/JSON exporters.

    Default output directory is `data/` at the project root. Default filenames are
    provided by each exporter (greenkube-report.csv / greenkube-report.json).
    """
    fmt = (format or "csv").lower()
    try:
        if fmt == "csv":
            from .exporters.csv_exporter import CSVExporter

            exporter_cls = CSVExporter
        elif fmt == "json":
            from .exporters.json_exporter import JSONExporter

            exporter_cls = JSONExporter
        else:
            raise typer.BadParameter(f"Unsupported export format: {format}")

        processor = get_processor()
        combined_data = processor.run()
        # If there is no data, still create an empty export file so callers
        # and tests get a consistent artifact. This avoids requiring live
        # external services just to exercise the exporter CLI path.
        if not combined_data:
            logger.warning("No data to export. Creating an empty export file.")
            # Prepare an empty rows list and still call exporter so the file
            # is created and the path logged.
            rows = []
            import os

            data_dir = os.path.abspath(os.path.join(os.getcwd(), "data"))
            os.makedirs(data_dir, exist_ok=True)

            exporter = exporter_cls()
            out_path = output if output and output.strip() else os.path.join(data_dir, exporter_cls.DEFAULT_FILENAME)
            written = exporter.export(rows, out_path)
            logger.info(f"Exported report to {written}")
            raise typer.Exit(code=0)

        # Prepare rows (convert objects to dicts when possible)
        try:
            rows = [r.__dict__ if hasattr(r, "__dict__") else dict(r) for r in combined_data]
        except Exception:
            rows = list(combined_data)

        import os

        data_dir = os.path.abspath(os.path.join(os.getcwd(), "data"))
        os.makedirs(data_dir, exist_ok=True)

        exporter = exporter_cls()
        out_path = output if output and output.strip() else os.path.join(data_dir, exporter_cls.DEFAULT_FILENAME)
        written = exporter.export(rows, out_path)
        logger.info(f"Exported report to {written}")

    except typer.Exit:
        raise
    except Exception as e:
        logger.error(f"Failed to export report: {e}")
        logger.error("Export failed: %s", traceback.format_exc())
        raise typer.Exit(code=1)


if __name__ == "__main__":
    # Run Typer app but intercept unknown commands to show our dynamic help
    try:
        app()
    except Exception as e:
        # If Click/Typer raised due to unknown command, show dynamic help
        try:
            from click import NoSuchCommand

            if isinstance(e, NoSuchCommand):
                help_cmd()
                raise
        except Exception:
            # Fallback to printing help and re-raising
            help_cmd()
            raise
