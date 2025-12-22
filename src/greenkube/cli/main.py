# src/greenkube/cli/main.py
"""
This module is the main entry point for the GreenKube CLI.

It aggregates all commands from the submodules (report, recommend, etc.)
"""

import logging

import typer

from ..core.config import config
from . import recommend, report, start

# --- Setup Logger ---
logging.basicConfig(
    level=config.LOG_LEVEL.upper(),
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


app = typer.Typer(
    name="greenkube",
    help="Measure, understand, and reduce the carbon footprint of your Kubernetes infrastructure.",
    add_completion=False,
)


def version_callback(value: bool):
    """
    Prints the version of GreenKube.
    """
    if value:
        from .. import __version__

        typer.echo(f"GreenKube version: {__version__}")
        raise typer.Exit()


@app.command()
def version():
    """
    Show the version of GreenKube.
    """
    from .. import __version__

    typer.echo(f"GreenKube version: {__version__}")


@app.callback()
def main(
    version: bool = typer.Option(
        None,
        "--version",
        callback=version_callback,
        is_eager=True,
        help="Show the version and exit.",
    ),
):
    """
    GreenKube CLI main entry point.
    """
    pass


# Register command sub-apps
app.add_typer(report.app, name="report")
app.add_typer(recommend.app, name="recommend")
app.add_typer(start.app, name="start")


if __name__ == "__main__":
    app()
