# src/greenkube/cli/report.py
"""
Implements the consolidated `report` command for the GreenKube CLI.
"""

import asyncio
import logging
import sys
import traceback
from pathlib import Path
from typing import List, Optional

import typer
from typing_extensions import Annotated

from ..core.aggregator import aggregate_metrics
from ..core.factory import get_repository
from ..exporters.csv_exporter import CSVExporter
from ..exporters.json_exporter import JSONExporter
from ..models.cli import FilterOptions, GroupingOptions, OutputOptions, ReportOptions
from ..models.metrics import CombinedMetric
from ..reporters.console_reporter import ConsoleReporter
from .utils import get_report_time_range, write_combined_metrics_to_database

logger = logging.getLogger(__name__)

app = typer.Typer(help="Generate and export FinGreenOps reports.", add_completion=False)


async def handle_export(
    data: List[CombinedMetric],
    output_options: OutputOptions,
):
    """Handles writing the report data to a file."""
    output_format = output_options.format
    output_path = output_options.output_path

    if output_format == "csv":
        exporter = CSVExporter()
    elif output_format == "json":
        exporter = JSONExporter()
    else:
        # This should be caught by Typer's Choice, but as a safeguard:
        logger.error(f"Invalid output format '{output_format}'.")
        raise typer.Exit(code=1)

    if not output_path:
        output_path = Path.cwd() / "data" / exporter.DEFAULT_FILENAME
    else:
        output_path = Path(output_path)

    # Ensure parent directory exists
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.error(f"Failed to create output directory {output_path.parent}: {e}")
        raise typer.Exit(code=1)

    try:
        rows = [item.model_dump(mode="json") for item in data]
    except Exception as e:
        logger.error(f"Failed to serialize report data for export: {e}")
        logger.error(traceback.format_exc())
        # Re-raise as a TyperExit to stop execution gracefully
        raise typer.Exit(code=1)

    try:
        written_path = await exporter.export(rows, str(output_path))
        logger.info(f"Successfully exported report to {written_path}")
        print(f"Report exported to: {written_path}", file=sys.stderr)

    except Exception as e:
        logger.error(f"Failed to export report to {output_path}: {e}")
        logger.error(traceback.format_exc())
        raise typer.Exit(code=1)


@app.callback(invoke_without_command=True)
def report(
    ctx: typer.Context,
    namespace: Annotated[Optional[str], typer.Option(help="Filter by a specific namespace.")] = None,
    last: Annotated[
        Optional[str],
        typer.Option("--last", help="Time range to report (e.g., '10min', '2h', '7d', '3w', '1m' for month, '1y')."),
    ] = None,
    hourly: Annotated[bool, typer.Option("--hourly", help="Group data by hour.")] = False,
    daily: Annotated[bool, typer.Option("--daily", help="Group data by day.")] = False,
    weekly: Annotated[bool, typer.Option("--weekly", help="Group data by week.")] = False,
    monthly: Annotated[bool, typer.Option("--monthly", help="Group data by month.")] = False,
    yearly: Annotated[bool, typer.Option("--yearly", help="Group data by year.")] = False,
    output_format: Annotated[
        Optional[str],
        typer.Option(
            "--output",
            help="Output format (csv/json). If set, writes to a file instead of the console.",
            case_sensitive=False,
        ),
    ] = None,
    output_path: Annotated[
        Optional[Path],
        typer.Option(
            "--output-path",
            help="Specify output file path. Default: './data/greenkube-report.<format>'",
            exists=False,
            dir_okay=False,
            writable=True,
        ),
    ] = None,
    aggregate: Annotated[
        bool,
        typer.Option(
            "--aggregate",
            help="Aggregate data by namespace, pod, and period.",
        ),
    ] = False,
    update_data: Annotated[
        bool,
        typer.Option(
            "--update-data",
            help="Update the database with the latest metrics before generating the report.",
        ),
    ] = False,
):
    """
    Generate and export FinGreenOps reports.

    Displays a report in the console by default.
    Use --output (csv/json) to export to a file.
    """
    # This function is the default command for the 'report' app.
    if ctx.invoked_subcommand is not None:
        return

    logger.info("Initializing GreenKube FinGreenOps reporting tool...")

    # Build model objects from raw CLI params so we retain validation logic
    filters = FilterOptions(namespace=namespace, last=last)
    grouping = GroupingOptions(hourly=hourly, daily=daily, weekly=weekly, monthly=monthly, yearly=yearly)
    output = OutputOptions(output_format=output_format, output_path=output_path)
    report_options = ReportOptions(aggregate=aggregate)

    async def _report_async():
        combined_data: List[CombinedMetric] = []
        try:
            # If requested, update the database with the latest metrics first.
            if update_data:
                await write_combined_metrics_to_database(last=last)

            repository = get_repository()

            start, end = get_report_time_range(filters.last)

            combined_data = await repository.read_combined_metrics(start_time=start, end_time=end)

            if filters.namespace:
                combined_data = [item for item in combined_data if item.namespace == filters.namespace]

            if report_options.aggregate:
                combined_data = aggregate_metrics(
                    combined_data,
                    hourly=grouping.hourly,
                    daily=grouping.daily,
                    weekly=grouping.weekly,
                    monthly=grouping.monthly,
                    yearly=grouping.yearly,
                )

            if not combined_data:
                logger.warning("No combined data was found in the database for the given time range.")
                if not output.is_enabled:
                    ConsoleReporter().report(data=[])
                else:
                    await handle_export(data=[], output_options=output)
                return  # Exit gracefully

            # Handle Output
            if output.is_enabled:
                await handle_export(
                    data=combined_data,
                    output_options=output,
                )
            else:
                # Default to console output
                console_reporter = ConsoleReporter()
                console_reporter.report(data=combined_data)

        except typer.Exit:
            raise
        except Exception as e:
            logger.error(f"An error occurred during report generation: {e}")
            logger.error("Report generation failed: %s", traceback.format_exc())
            raise typer.Exit(code=1)

    try:
        asyncio.run(_report_async())
    except typer.Exit:
        raise
    except Exception as e:
        logger.error(f"An unexpected error occurred: {e}")
        raise typer.Exit(code=1)
