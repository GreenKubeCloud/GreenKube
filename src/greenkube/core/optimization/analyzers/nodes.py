"""Node-level analysis: overprovisioned nodes.

``UNDERUTILIZED_NODE`` (fewer than 3 pods and under 15% CPU) is no longer
emitted (roadmap E9): it duplicated this analysis with weaker logic. Node
consolidation advice belongs to the node-pool analysis; the enum value stays
for stored recommendations.
"""

from typing import TYPE_CHECKING, Dict, List, Optional

from greenkube.core.optimization.analyzers.base import Analyzer
from greenkube.models.metrics import Recommendation, RecommendationType

if TYPE_CHECKING:
    from greenkube.core.optimization.context import OptimizationContext


class NodesAnalyzer(Analyzer):
    """Analyzes node-level utilization from the per-node usage summaries."""

    capability = "node_optimization"

    def analyze(self, context: "OptimizationContext") -> List[Recommendation]:
        recs: List[Recommendation] = []
        cfg = self.config
        node_infos = context.node_infos
        if not node_infos:
            return recs

        node_capacity: Dict[str, float] = {}
        node_mem_capacity: Dict[str, int] = {}
        for ni in node_infos:
            if hasattr(ni, "name") and hasattr(ni, "cpu_capacity_cores"):
                node_capacity[ni.name] = ni.cpu_capacity_cores or 0
            if hasattr(ni, "name") and hasattr(ni, "memory_capacity_bytes"):
                mem_cap = ni.memory_capacity_bytes
                if isinstance(mem_cap, (int, float)) and mem_cap > 0:
                    node_mem_capacity[ni.name] = int(mem_cap)

        node_usage = context.usage_snapshot().nodes

        for node_name, capacity_cores in node_capacity.items():
            if capacity_cores <= 0:
                continue

            usage = node_usage.get(node_name)
            if usage is None or usage.hours_observed <= 0:
                continue

            capacity_millicores = capacity_cores * 1000
            # Average concurrent usage of the pods on the node over its observed steps.
            avg_total_usage = usage.cpu_usage_avg
            cpu_utilization = avg_total_usage / capacity_millicores

            mem_capacity = node_mem_capacity.get(node_name)
            mem_utilization: Optional[float] = None
            avg_mem_usage: float = 0.0
            if mem_capacity:
                avg_mem_usage = usage.memory_usage_avg
                mem_utilization = avg_mem_usage / mem_capacity

            # OVERPROVISIONED_NODE: both CPU and memory (when available) must be low
            cpu_is_low = cpu_utilization < cfg.NODE_UTILIZATION_THRESHOLD
            mem_is_low = mem_utilization is None or mem_utilization < cfg.NODE_UTILIZATION_THRESHOLD

            if cpu_is_low and mem_is_low:
                mem_detail = (
                    f", memory {mem_utilization:.0%} ({avg_mem_usage / (1024**3):.1f} GiB / "
                    f"{(mem_capacity or 0) / (1024**3):.1f} GiB)"
                    if mem_utilization is not None and mem_capacity is not None
                    else ""
                )
                recs.append(
                    Recommendation(
                        type=RecommendationType.OVERPROVISIONED_NODE,
                        scope="node",
                        description=(
                            f"Node '{node_name}' has {cpu_utilization:.0%} average CPU utilization "
                            f"({avg_total_usage:.0f}m / {capacity_millicores:.0f}m){mem_detail}. "
                            f"Consider consolidating workloads or downsizing."
                        ),
                        reason=(
                            f"Node CPU utilization ({cpu_utilization:.0%}) is below "
                            f"threshold ({cfg.NODE_UTILIZATION_THRESHOLD:.0%})."
                        ),
                        priority="medium",
                        target_node=node_name,
                    )
                )

        return recs
