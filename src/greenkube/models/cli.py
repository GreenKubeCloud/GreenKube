# src/greenkube/models/cli.py
"""
Data models for GreenKube CLI command options using Typer.
This allows for clean dependency injection of parameters.
"""

from pathlib import Path
from typing import Optional

import typer
from typing_extensions import Annotated


class ReportOptions:
    """Dependency-injectable model for report options."""

    def __init__(
        self,
        aggregate: Annotated[
            bool,
            typer.Option(
                "--aggregate",
                help="Aggregate data by namespace, pod, and period.",
            ),
        ] = False,
    ):
        self.aggregate = aggregate


class FilterOptions:
    """Dependency-injectable model for filter options."""

    def __init__(
        self,
        namespace: Annotated[
            Optional[str],
            typer.Option(help="Filter by a specific namespace."),
        ] = None,
        last: Annotated[
            Optional[str],
            typer.Option(
                "--last",
                help="Time range to report (e.g., '3h', '7d', '2w', '1m', '1y').",
            ),
        ] = None,
    ):
        self.namespace = namespace
        self.last = last


class GroupingOptions:
    """Dependency-injectable model for time grouping options."""

    def __init__(
        self,
        hourly: Annotated[bool, typer.Option("--hourly", help="Group data by hour.")] = False,
        daily: Annotated[bool, typer.Option("--daily", help="Group data by day.")] = False,
        weekly: Annotated[bool, typer.Option("--weekly", help="Group data by week.")] = False,
        monthly: Annotated[bool, typer.Option("--monthly", help="Group data by month.")] = False,
        yearly: Annotated[bool, typer.Option("--yearly", help="Group data by year.")] = False,
    ):
        self.hourly = hourly
        self.daily = daily
        self.weekly = weekly
        self.monthly = monthly
        self.yearly = yearly
        self._validate()

    def _validate(self):
        """Ensures only one grouping option is selected."""
        groupings = [
            self.hourly,
            self.daily,
            self.weekly,
            self.monthly,
            self.yearly,
        ]
        if sum(groupings) > 1:
            raise typer.BadParameter(
                "Only one time grouping flag (--hourly, --daily, --weekly, --monthly, --yearly) can be used at a time."
            )

    @property
    def is_enabled(self) -> bool:
        """Checks if any grouping option is active."""
        return any(
            [
                self.hourly,
                self.daily,
                self.weekly,
                self.monthly,
                self.yearly,
            ]
        )


class OutputOptions:
    """Dependency-injectable model for output/export options."""

    def __init__(
        self,
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
    ):
        self.output_format = output_format
        self.output_path = output_path
        self._validate()

    def _validate(self):
        """Validates the output format."""
        if self.output_format and self.output_format.lower() not in [
            "csv",
            "json",
        ]:
            raise typer.BadParameter(f"Invalid output format '{self.output_format}'. Must be 'csv' or 'json'.")

    @property
    def is_enabled(self) -> bool:
        """Checks if file output is enabled."""
        return self.output_format is not None

    @property
    def format(self) -> str:
        """Returns the validated, lower-cased format."""
        return self.output_format.lower() if self.output_format else "csv"
