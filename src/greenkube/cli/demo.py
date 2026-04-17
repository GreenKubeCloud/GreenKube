# src/greenkube/cli/demo.py
"""
Demo command for the GreenKube CLI.

Launches GreenKube in demo mode with pre-populated sample data,
allowing users to explore the dashboard and API without a live
Kubernetes cluster.
"""

import asyncio
import logging
import traceback

import typer
from typing_extensions import Annotated

logger = logging.getLogger(__name__)

app = typer.Typer(
    name="demo",
    help="Launch GreenKube in demo mode with realistic sample data.",
    add_completion=False,
)


@app.callback(invoke_without_command=True)
def demo(
    ctx: typer.Context,
    port: Annotated[
        int,
        typer.Option("--port", "-p", help="Port for the API server."),
    ] = 8000,
    days: Annotated[
        int,
        typer.Option("--days", "-d", help="Number of days of sample data to generate."),
    ] = 30,
    no_browser: Annotated[
        bool,
        typer.Option("--no-browser", help="Do not open the browser automatically."),
    ] = False,
) -> None:
    """Launch GreenKube demo mode with pre-populated sample data.

    This command creates a temporary SQLite database with realistic Kubernetes
    metrics (carbon emissions, costs, resource usage, recommendations), starts
    the API server, and opens the dashboard in your browser.

    No Kubernetes cluster required — perfect for evaluating GreenKube.

    Examples:

        greenkube demo

        greenkube demo --port 9000 --days 14

        greenkube demo --no-browser
    """
    if ctx.invoked_subcommand is not None:
        return

    logging.basicConfig(
        level="INFO",
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

    try:
        from greenkube.demo.runner import run_demo

        asyncio.run(run_demo(port=port, days=days, no_browser=no_browser))
    except KeyboardInterrupt:
        pass
    except Exception as e:
        logger.error("❌ Demo failed: %s", e)
        logger.error(traceback.format_exc())
        raise typer.Exit(code=1)
