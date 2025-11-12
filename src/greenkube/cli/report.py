# src/greenkube/cli/report.py
"""
Implements the consolidated `report` command for the GreenKube CLI.
"""

import logging
import re
import sys
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

import typer
from typing_extensions import Annotated

from ..core.factory import get_processor
from ..exporters.csv_exporter import CSVExporter
from ..exporters.json_exporter import JSONExporter
from ..models.metrics import CombinedMetric
from ..reporters.console_reporter import ConsoleReporter

logger = logging.getLogger(__name__)

app = typer.Typer(help="Generate and export FinGreenOps reports.", add_completion=False)


def parse_last_duration(last: str) -> timedelta:
    """Parses a duration string (e.g., '3h', '7d', '2w') into a timedelta."""
    match = re.match(r"^(\d+)([hdwmy])$", last.lower())
    if not match:
        logger.error(f"Invalid format for --last: '{last}'. Use format like '7d', '3h', '2w', '1m', '1y'.")
        raise typer.BadParameter(f"Invalid format for --last: '{last}'. Use format like '7d', '3h', '2w', '1m', '1y'.")

    value, unit = int(match.group(1)), match.group(2)
    if unit == "h":
        return timedelta(hours=value)
    if unit == "d":
        return timedelta(days=value)
    if unit == "w":
        return timedelta(weeks=value)
    if unit == "m":
        # Approximate month as 30 days
        return timedelta(days=value * 30)
    if unit == "y":
        # Approximate year as 365 days
        return timedelta(days=value * 365)
    # This line is unreachable due to regex
    return timedelta()


def handle_export(
    data: List[CombinedMetric],
    output_format: str,
    output_path: Optional[Path],
):
    """Handles writing the report data to a file."""
    if not output_path:
        default_filename = CSVExporter.DEFAULT_FILENAME if output_format == "csv" else JSONExporter.DEFAULT_FILENAME
        output_path = Path.cwd() / "data" / default_filename
    else:
        output_path = Path(output_path)

    # Ensure parent directory exists
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.error(f"Failed to create output directory {output_path.parent}: {e}")
        raise typer.Exit(code=1)

    try:
        # Convert Pydantic models to dicts for export
        rows = [item.model_dump() for item in data]
    except AttributeError:  # Fallback for older Pydantic or other objects
        rows = [item.__dict__ if hasattr(item, "__dict__") else dict(item) for item in data]

    try:
        if output_format == "csv":
            exporter = CSVExporter()
        elif output_format == "json":
            exporter = JSONExporter()
        else:
            # This should be caught by Typer's Choice, but as a safeguard:
            logger.error(f"Invalid output format '{output_format}'.")
            raise typer.Exit(code=1)

        written_path = exporter.export(rows, str(output_path))
        logger.info(f"Successfully exported report to {written_path}")
        print(f"Report exported to: {written_path}", file=sys.stderr)

    except Exception as e:
        logger.error(f"Failed to export report to {output_path}: {e}")
        logger.error(traceback.format_exc())
        raise typer.Exit(code=1)


@app.callback(invoke_without_command=True)
def report(
    ctx: typer.Context,
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
    # Time Period Grouping
    hourly: Annotated[bool, typer.Option("--hourly", help="Group data by hour.")] = False,
    daily: Annotated[bool, typer.Option("--daily", help="Group data by day.")] = False,
    monthly: Annotated[bool, typer.Option("--monthly", help="Group data by month.")] = False,
    yearly: Annotated[bool, typer.Option("--yearly", help="Group data by year.")] = False,
    # Time Range Selection
    last: Annotated[
        Optional[str],
        typer.Option(
            "--last",
            help="Time range to report (e.g., '3h', '7d', '2w', '1m', '1y').",
        ),
    ] = None,
    # Filters
    namespace: Annotated[
        Optional[str],
        typer.Option(help="Filter by a specific namespace."),
    ] = None,
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

    # Validate output format
    if output_format and output_format.lower() not in ["csv", "json"]:
        raise typer.BadParameter(f"Invalid output format '{output_format}'. Must be 'csv' or 'json'.")

    # Validate mutually exclusive time grouping
    time_groupings = [hourly, daily, monthly, yearly]
    if sum(time_groupings) > 1:
        raise typer.BadParameter(
            "Only one time grouping flag (--hourly, --daily, --monthly, --yearly) can be used at a time."
        )

    # Determine if this is a "now" report or a "range" report
    is_range_report = any([last, hourly, daily, monthly, yearly])

    combined_data: List[CombinedMetric] = []

    try:
        processor = get_processor()

        if is_range_report:
            logger.info("Running ranged data processing pipeline...")
            end = datetime.now(timezone.utc)
            start = end - timedelta(days=1)  # Default to last 24h if a grouping is set without --last

            if last:
                start = end - parse_last_duration(last)

            combined_data = processor.run_range(
                start=start,
                end=end,
                step=None,  # Processor will calculate step
                namespace=namespace,
                monthly=monthly,
                yearly=yearly,
                # Pass daily/hourly to run_range if it supports it,
                # otherwise, we'd do aggregation here.
                # The current run_range seems to only support monthly/yearly grouping.
                # We will pass them anyway for future proofing.
                # daily=daily,
                # hourly=hourly,
            )
        else:
            logger.info("Running 'now' data processing pipeline...")
            combined_data = processor.run()
            if namespace:
                combined_data = [item for item in combined_data if item.namespace == namespace]

        if not combined_data:
            logger.warning("No combined data was generated by the processor.")
            if not output_format:
                ConsoleReporter().report(data=[])
            else:
                handle_export(data=[], output_format=output_format.lower(), output_path=output_path)
            raise typer.Exit(code=0)

        # Handle Output
        if output_format:
            handle_export(
                data=combined_data,
                output_format=output_format.lower(),
                output_path=output_path,
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
