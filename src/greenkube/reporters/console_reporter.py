# src/greenkube/reporters/console_reporter.py
"""
A reporter that displays the final data in a formatted table in the console.
"""
from typing import List, Optional
import typer
from rich.console import Console
from rich.table import Table
import logging

from .base_reporter import BaseReporter
from ..models.metrics import CombinedMetric, Recommendation, RecommendationType

logger = logging.getLogger(__name__)


class ConsoleReporter(BaseReporter):
    """
    Renders FinGreenOps data to the console using the 'rich' library.
    """
    def __init__(self):
        self.console = Console()

    def report(self, data: List[CombinedMetric]):
        """
        Displays the combined metrics in a rich, detailed table.
        Shows CPU and Memory requests in the report.
        """
        if not data:
            self.console.print("No data to report.", style="yellow")
            return

        table = Table(title="GreenKube FinGreenOps Report", header_style="bold magenta", show_lines=True)
        table.add_column("Pod Name", style="cyan")
        table.add_column("Namespace", style="cyan")
        table.add_column("Total Cost ($)", style="green", justify="right")
        table.add_column("CO2e (g)", style="red", justify="right")
        table.add_column("Energy (Joules)", style="yellow", justify="right")
        table.add_column("CPU Req (m)", style="blue", justify="right")
        table.add_column("Mem Req (Mi)", style="blue", justify="right")
        table.add_column("Grid Intensity (g/kWh)", style="dim", justify="right")
        table.add_column("PUE", style="dim", justify="right")

        # Aggregate by namespace+pod to avoid duplicate entries
        aggregated = {}
        for item in data:
            key = (item.namespace, item.pod_name)
            if key not in aggregated:
                aggregated[key] = {
                    "cost": 0.0,
                    "co2e": 0.0,
                    "joules": 0.0,
                    "cpu_req": item.cpu_request,
                    "mem_req": item.memory_request,
                    "intensity": item.grid_intensity,
                    "pue": item.pue,
                }
            aggregated[key]["cost"] += item.total_cost
            aggregated[key]["co2e"] += item.co2e_grams
            aggregated[key]["joules"] += item.joules

        # Sort by CO2e descending
        sorted_keys = sorted(aggregated.keys(), key=lambda k: aggregated[k]["co2e"], reverse=True)

        for ns, pod in sorted_keys:
            item = aggregated[(ns, pod)]
            mem_mib = item["mem_req"] / (1024 * 1024) if item["mem_req"] else 0.0

            table.add_row(
                pod,
                ns,
                f"{item['cost']:.4f}",
                f"{item['co2e']:.2f}",
                f"{item['joules']:.0f}",
                f"{item['cpu_req']}",
                f"{mem_mib:.1f}",
                f"{item['intensity']:.2f}",
                f"{item['pue']:.2f}",
            )

        self.console.print(table)

    def report_recommendations(self, recommendations: List[Recommendation]):
        """
        Displays optimization recommendations in a separate table.
        """
        if not recommendations:
            self.console.print("No recommendations to display.", style="green")
            return

        table = Table(title="GreenKube Optimization Recommendations", header_style="bold magenta", show_lines=True)
        table.add_column("Type", style="bold")
        table.add_column("Namespace", style="cyan")
        table.add_column("Pod Name", style="cyan")
        table.add_column("Recommendation", style="white")

        # Sort by type, namespace, pod
        sorted_recs = sorted(recommendations, key=lambda r: (r.type.value if hasattr(r.type, 'value') else str(r.type), r.namespace, r.pod_name))

        for rec in sorted_recs:
            style = "white"
            type_str = rec.type.value if hasattr(rec.type, 'value') else str(rec.type)
            if rec.type == RecommendationType.ZOMBIE_POD:
                style = "bold yellow"
                type_str = f"🧟 {type_str}"
            elif rec.type == RecommendationType.RIGHTSIZING_CPU:
                style = "bold cyan"
                type_str = f"📉 {type_str}"

            table.add_row(f"[{style}]{type_str}[/]", rec.namespace, rec.pod_name, rec.description)

        self.console.print(table)

