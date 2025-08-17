# src/greenkube/reporters/console_reporter.py
"""
A reporter that displays the final data in a formatted table in the console.
"""
from typing import List
from rich.console import Console
from rich.table import Table

from .base_reporter import BaseReporter
from ..models.metrics import CombinedMetric

class ConsoleReporter(BaseReporter):
    """
    Renders FinGreenOps data to the console using the 'rich' library.
    """
    def report(self, data: List[CombinedMetric]):
        """
        Displays the combined metrics in a rich, detailed table.
        """
        print("INFO: Generating report...")
        console = Console()
        table = Table(show_header=True, header_style="bold magenta", title="GreenKube FinGreenOps Report")
        
        table.add_column("Namespace", style="dim")
        table.add_column("Pod Name")
        table.add_column("Total Cost ($)", justify="right")
        table.add_column("CO2e (grams)", justify="right")
        table.add_column("PUE", justify="right")
        table.add_column("Grid Intensity (gCO2e/kWh)", justify="right")

        for item in data:
            table.add_row(
                item.namespace,
                item.pod_name,
                f"{item.total_cost:.4f}",
                f"{item.co2e_grams:.4f}",
                f"{item.pue:.2f}",
                f"{item.grid_intensity:.1f}"
            )

        console.print(table)

