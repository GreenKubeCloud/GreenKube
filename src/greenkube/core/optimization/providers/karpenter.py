# src/greenkube/core/optimization/providers/karpenter.py
"""Karpenter recommendation source (Phase 6).

Reads Karpenter NodePools and NodeClaims and emits node-pool consolidation
recommendations when a multi-node pool is consistently under-utilized. The
connector is optional: it degrades gracefully when the Karpenter CRDs are not
installed.
"""

import logging
from typing import TYPE_CHECKING, Dict, List, Optional, Protocol

from greenkube.collectors.karpenter_collector import KarpenterCollector, NodePoolInfo
from greenkube.core.optimization.providers.base import RecommendationSource
from greenkube.models.metrics import (
    Recommendation,
    RecommendationCapability,
    RecommendationType,
)
from greenkube.models.metrics import RecommendationSource as Source

if TYPE_CHECKING:
    from greenkube.core.config import Config
    from greenkube.core.optimization.context import OptimizationContext

logger = logging.getLogger(__name__)


class KarpenterCollectorLike(Protocol):
    """Minimal collector contract, so tests can inject fakes."""

    async def collect(self) -> List[NodePoolInfo]: ...


class KarpenterSource(RecommendationSource):
    """Node-pool consolidation recommendations backed by Karpenter."""

    name = "karpenter"
    priority = 2

    def __init__(self, config: "Config", collector: Optional[KarpenterCollectorLike] = None):
        self.config = config
        self._collector = collector if collector is not None else KarpenterCollector()
        self._available: Optional[bool] = None

    async def is_available(self) -> bool:
        """Checks for the Karpenter CRD once and caches the result.

        When the collector is a test fake (no ``_resolve_version`` helper) the
        source is considered available.
        """
        if self._available is not None:
            return self._available

        resolve = getattr(self._collector, "_resolve_version", None)
        if resolve is None:
            self._available = True
            return True

        try:
            from greenkube.core.k8s_client import get_custom_objects_api

            api = await get_custom_objects_api()
            self._available = bool(api is not None and await resolve(api))
        except Exception as exc:
            logger.debug("Karpenter availability check failed: %s", exc)
            self._available = False
        return self._available

    async def collect(self, context: "OptimizationContext") -> List[Recommendation]:
        try:
            pools = await self._collector.collect()
        except Exception as exc:
            logger.warning("Karpenter collection failed: %s. Proceeding without NodePool recommendations.", exc)
            return []
        if not pools:
            return []

        threshold = float(getattr(self.config, "NODE_UTILIZATION_THRESHOLD", 0.2))
        recs: List[Recommendation] = []
        for pool in pools:
            utilization = _pool_cpu_utilization(pool, context)
            if pool.node_count < 2 or utilization is None or utilization >= threshold:
                continue

            annual_cost = _pool_annual_cost(pool, context)
            label = f"NodePool '{pool.name}'"
            recs.append(
                Recommendation(
                    pod_name=None,
                    namespace=None,
                    type=RecommendationType.OVERPROVISIONED_NODE,
                    capability=RecommendationCapability.NODE_POOL,
                    scope="node",
                    source=Source.KARPENTER,
                    source_ref=pool.name,
                    target_node=f"nodepool/{pool.name}",
                    description=(
                        f"{label} has {pool.node_count} nodes with an average CPU request "
                        f"utilization of {utilization:.0%}. Consider consolidation."
                    ),
                    reason=(
                        f"Karpenter NodePool '{pool.name}' is below the "
                        f"{threshold:.0%} utilization threshold across {pool.node_count} claimed nodes. "
                        "Karpenter can consolidate workloads onto fewer, larger nodes."
                    ),
                    priority="medium",
                    potential_savings_cost=annual_cost * (1 - utilization) if annual_cost else None,
                )
            )
        return recs


def _pool_cpu_utilization(pool: NodePoolInfo, context: "OptimizationContext") -> Optional[float]:
    """Estimates average CPU request utilization across a pool's nodes."""
    if not pool.node_names:
        return None

    capacities: Dict[str, float] = {}
    for node in context.node_infos or []:
        name = getattr(node, "name", None)
        capacity = getattr(node, "cpu_allocatable_millicores", None)
        if name and capacity:
            capacities[name] = float(capacity)

    requests: Dict[str, float] = {}
    for metric in context.metrics:
        node_name = metric.node
        if node_name and node_name in pool.node_names:
            requests[node_name] = requests.get(node_name, 0.0) + float(metric.cpu_request or 0)

    ratios = []
    for node_name, capacity in capacities.items():
        if node_name in pool.node_names and capacity > 0:
            ratios.append(min(requests.get(node_name, 0.0) / capacity, 1.0))
    if not ratios:
        return None
    return sum(ratios) / len(ratios)


def _pool_annual_cost(pool: NodePoolInfo, context: "OptimizationContext") -> Optional[float]:
    """Annualizes the cost observed on a pool's nodes, when metrics are available."""
    window = context.analysis_window_seconds
    if not window or window <= 0:
        return None
    total = sum(m.total_cost for m in context.metrics if m.node in pool.node_names)
    if total <= 0:
        return None
    return total / window * (365.25 * 24 * 3600)
