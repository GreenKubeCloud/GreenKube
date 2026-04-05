# src/greenkube/cli/main.py
"""
This module is the main entry point for the GreenKube CLI.

It aggregates all commands from the submodules (report, recommend, etc.)
"""

import logging
import os

import typer

from ..core.config import get_config
from . import demo, recommend, report, start

# --- Setup Logger ---
# force=True ensures basicConfig wins even if logging.warning() was already
# called (e.g. from Config.__init__) which may have pre-installed a lastResort
# handler at WARNING level before this point.
logging.basicConfig(
    level=get_config().LOG_LEVEL.upper(),
    format="%(asctime)s - %(levelname)s - %(message)s",
    force=True,
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
    no_color: bool = typer.Option(
        False,
        "--no-color",
        help="Disable colors and Rich formatting. Useful for CI/CD pipelines and log parsers.",
        is_eager=True,
    ),
):
    """
    GreenKube CLI main entry point.
    """
    if no_color or os.environ.get("NO_COLOR"):
        os.environ["NO_COLOR"] = "1"


# Register command sub-apps
app.add_typer(report.app, name="report")
app.add_typer(recommend.app, name="recommend")
app.add_typer(start.app, name="start")
app.add_typer(demo.app, name="demo")


if __name__ == "__main__":
    app()
