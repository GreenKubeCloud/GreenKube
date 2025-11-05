import logging
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional

import typer
from typing_extensions import Annotated

from ..reporters.console_reporter import ConsoleReporter

logger = logging.getLogger(__name__)

app = typer.Typer(name="report", help="Generate FinOps and GreenOps reports.")


@app.command("now")
def report_now(
    namespace: Annotated[
        Optional[str],
        typer.Option(help="Display a detailed report for a specific namespace."),
    ] = None,
):
    """Get a report for the current state."""
    try:
        # reuse get_processor factory from top-level module for consistency
        from ..cli import get_processor

        processor = get_processor()
        console_reporter = ConsoleReporter()

        combined_data = processor.run()
        if not combined_data:
            raise typer.Exit(code=0)
        if namespace:
            combined_data = [c for c in combined_data if c.namespace == namespace]
            if not combined_data:
                raise typer.Exit(code=0)
        console_reporter.report(data=combined_data)
    except typer.Exit:
        raise
    except Exception as e:
        logger.error(f"Error during report_now: {e}")
        logger.error(traceback.format_exc())
        raise typer.Exit(code=1)


@app.command("range")
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
    """Generate a report over a time range."""
    if monthly and yearly:
        raise typer.BadParameter("--monthly and --yearly are mutually exclusive")

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
        from ..cli import get_processor

        processor = get_processor()
        console = ConsoleReporter()
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
            console.report([])
            return
        console.report(combined)
    except typer.Exit:
        raise
    except Exception as e:
        logger.error(f"Error during report_range: {e}")
        logger.error(traceback.format_exc())
        raise typer.Exit(code=1)
