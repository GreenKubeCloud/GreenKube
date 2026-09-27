# src/greenkube/core/optimization/analyzers/nodes.py
"""Node-level analysis: overprovisioned and underutilized nodes."""

from collections import defaultdict
from typing import TYPE_CHECKING, Dict, List, Optional

from greenkube.core.optimization.analyzers.base import Analyzer
from greenkube.models.metrics import Recommendation, RecommendationType

if TYPE_CHECKING:
    from greenkube.core.optimization.context import OptimizationContext


class NodesAnalyzer(Analyzer):
    """Analyzes node-level utilization patterns."""

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

        # Group pod CPU and memory usage per node per timestamp, so we can sum across pods
        node_usage_by_ts: Dict[str, Dict] = defaultdict(lambda: defaultdict(float))
        node_mem_usage_by_ts: Dict[str, Dict] = defaultdict(lambda: defaultdict(float))
        node_pods: Dict[str, set] = defaultdict(set)

        for m in context.metrics:
            if m.node:
                ts_key = m.timestamp if m.timestamp is not None else 0
                if m.cpu_usage_millicores is not None:
                    node_usage_by_ts[m.node][ts_key] += m.cpu_usage_millicores
                if m.memory_usage_bytes is not None:
                    node_mem_usage_by_ts[m.node][ts_key] += m.memory_usage_bytes
                node_pods[m.node].add(m.pod_name)

        for node_name, capacity_cores in node_capacity.items():
            if capacity_cores <= 0:
                continue

            ts_totals = list(node_usage_by_ts.get(node_name, {}).values())
            if not ts_totals:
                continue

            capacity_millicores = capacity_cores * 1000
            avg_total_usage = sum(ts_totals) / len(ts_totals)
            cpu_utilization = avg_total_usage / capacity_millicores
            unique_pods = len(node_pods.get(node_name, set()))

            # Compute memory utilization if capacity is available
            mem_capacity = node_mem_capacity.get(node_name)
            mem_utilization: Optional[float] = None
            avg_mem_usage: float = 0.0
            if mem_capacity:
                mem_ts_totals = list(node_mem_usage_by_ts.get(node_name, {}).values())
                if mem_ts_totals:
                    avg_mem_usage = sum(mem_ts_totals) / len(mem_ts_totals)
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

            # UNDERUTILIZED_NODE: few pods + low utilization
            if unique_pods < 3 and cpu_utilization < 0.15:
                recs.append(
                    Recommendation(
                        type=RecommendationType.UNDERUTILIZED_NODE,
                        scope="node",
                        description=(
                            f"Node '{node_name}' has only {unique_pods} pod(s) and "
                            f"{cpu_utilization:.0%} CPU utilization. Consider draining and removing."
                        ),
                        reason=(
                            f"Node has {unique_pods} pods (< 3) and {cpu_utilization:.0%} CPU utilization (< 15%)."
                        ),
                        priority="low",
                        target_node=node_name,
                    )
                )

        return recs
