# src/greenkube/reporters/console_reporter.py
"""
A reporter that displays the final data in a formatted table in the console.
"""

import logging
from typing import List

from rich.console import Console
from rich.table import Table

from ..models.metrics import CombinedMetric, Recommendation, RecommendationType
from .base_reporter import BaseReporter

logger = logging.getLogger(__name__)


class ConsoleReporter(BaseReporter):
    """
    Renders FinGreenOps data to the console using the 'rich' library.
    """

    def __init__(self):
        self.console = Console()

    def report(
        self,
        data: List[CombinedMetric],
        group_by: str = "namespace",
        sort_by: str = "cost",
        recommendations: List[Recommendation] | None = None,
    ):
        """
        Displays the combined metrics in a rich, detailed table.
        If recommendations are provided, displays them in a second table.
        """
        if not data and not recommendations:
            self.console.print("No data to report.", style="yellow")
            return

        if not data:
            self.console.print("No data to report.", style="yellow")
        else:
            table = Table(
                title="GreenKube FinGreenOps Report",
                header_style="bold magenta",
                show_lines=True,
            )
            # If any item has a period, include a Period column
            has_period = any(getattr(item, "period", None) for item in data)
            table.add_column("Pod Name", style="cyan")
            table.add_column("Namespace", style="cyan")
            if has_period:
                table.add_column("Period", style="magenta")
            table.add_column("Total Cost ($)", style="green", justify="right")
            table.add_column("CO2e (g)", style="red", justify="right")
            table.add_column("Energy (Joules)", style="yellow", justify="right")
            table.add_column("CPU Req (m)", style="blue", justify="right")
            table.add_column("Mem Req (Mi)", style="blue", justify="right")
            table.add_column("Grid Intensity (g/kWh)", style="dim", justify="right")
            table.add_column("PUE", style="dim", justify="right")

            # Sort by CO2e descending
            sorted_data = sorted(data, key=lambda item: item.co2e_grams, reverse=True)

            for item in sorted_data:
                mem_mib = item.memory_request / (1024 * 1024) if item.memory_request else 0.0
                period = getattr(item, "period", None)

                row = [
                    item.pod_name,
                    item.namespace,
                ]
                if has_period:
                    row.append(period or "")
                row.extend(
                    [
                        f"{item.total_cost:.4f}",
                        f"{item.co2e_grams:.2f}",
                        f"{item.joules:.0f}",
                        f"{item.cpu_request}",
                        f"{mem_mib:.1f}",
                        f"{item.grid_intensity:.2f}",
                        f"{item.pue:.2f}",
                    ]
                )

                table.add_row(*row)

            self.console.print(table)

        if recommendations is not None:
            self.report_recommendations(recommendations)

    def report_recommendations(self, recommendations: List[Recommendation]):
        """
        Displays optimization recommendations in a separate table.
        """
        if not recommendations:
            self.console.print("\n✅ All systems look optimized! No recommendations to display.", style="green")
            return

        table = Table(
            title="GreenKube Optimization Recommendations",
            header_style="bold magenta",
            show_lines=True,
        )
        table.add_column("Type", style="bold")
        table.add_column("Namespace", style="cyan")
        table.add_column("Pod Name", style="cyan")
        table.add_column("Recommendation", style="white")

        # Sort by type, namespace, pod
        sorted_recs = sorted(
            recommendations,
            key=lambda r: (
                r.type.value if hasattr(r.type, "value") else str(r.type),
                r.namespace,
                r.pod_name,
            ),
        )

        for rec in sorted_recs:
            style = "white"
            type_str = rec.type.value if hasattr(rec.type, "value") else str(rec.type)
            if rec.type == RecommendationType.ZOMBIE_POD:
                style = "bold yellow"
                type_str = f"🧟 {type_str}"
            elif rec.type == RecommendationType.RIGHTSIZING_CPU:
                style = "bold cyan"
                type_str = f"📉 {type_str}"

            table.add_row(f"[{style}]{type_str}[/]", rec.namespace, rec.pod_name, rec.description)

        self.console.print(table)
