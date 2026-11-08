# src/greenkube/core/optimization/providers/karpenter.py
"""Karpenter recommendation source (Phase 6).

Reads Karpenter NodePools and NodeClaims and emits node-pool consolidation
recommendations when a multi-node pool is consistently under-utilized. The
connector is optional: it degrades gracefully when the Karpenter CRDs are not
installed.
"""

import logging
import math
from typing import TYPE_CHECKING, Dict, List, Optional, Protocol, Tuple

from greenkube.collectors.karpenter_collector import KarpenterCollector, NodePoolInfo
from greenkube.core.optimization.providers.base import RecommendationSource
from greenkube.impact.projector import ImpactProjector, impact_fields
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

            label = f"NodePool '{pool.name}'"
            savings_fields: dict = {}
            removable, removed_nodes = _removable_nodes(pool, utilization, context)
            if removed_nodes:
                # Node-hours removed × node price; the consolidation is the action
                # itself, so the Impact Model uses a realization factor of 1.
                delta = ImpactProjector.for_context(context).node_removal(removed_nodes)
                savings_fields = impact_fields(delta)
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
                        "Karpenter can consolidate workloads onto fewer, larger nodes"
                        + (f"; about {removable} node(s) could be removed." if removable else ".")
                    ),
                    priority="medium",
                    **savings_fields,
                )
            )
        return recs


def _pool_cpu_utilization(pool: NodePoolInfo, context: "OptimizationContext") -> Optional[float]:
    """Estimates time-weighted CPU request utilization across a pool's nodes."""
    return _pool_request_utilization(pool, context, "cpu_request", "cpu_allocatable_millicores")


def _pool_memory_utilization(pool: NodePoolInfo, context: "OptimizationContext") -> Optional[float]:
    """Estimates time-weighted memory request utilization across a pool's nodes."""
    return _pool_request_utilization(pool, context, "memory_request", "memory_allocatable_bytes")


def _pool_request_utilization(
    pool: NodePoolInfo,
    context: "OptimizationContext",
    request_field: str,
    capacity_field: str,
) -> Optional[float]:
    """Time-weighted request utilization of one resource across a pool's nodes.

    Reads the per-node average requested capacity of the usage snapshot (roadmap
    E3): the sum of the requests of the pods on the node, averaged over the
    collection steps the node was observed.
    """
    if not pool.node_names:
        return None

    capacities: Dict[str, float] = {}
    for node in context.node_infos or []:
        name = getattr(node, "name", None)
        capacity = getattr(node, capacity_field, None)
        if name and capacity:
            capacities[name] = float(capacity)

    node_usage = context.usage_snapshot().nodes
    attribute = "cpu_request_avg" if request_field == "cpu_request" else "memory_request_avg"
    pool_nodes = [name for name, capacity in capacities.items() if name in pool.node_names and capacity > 0]
    if not any(name in node_usage for name in pool_nodes):
        return None
    ratios = []
    for node_name in pool_nodes:
        usage = node_usage.get(node_name)
        requested = float(getattr(usage, attribute, 0.0) or 0.0) if usage is not None else 0.0
        ratios.append(min(requested / capacities[node_name], 1.0))
    return sum(ratios) / len(ratios)


def _removable_nodes(
    pool: NodePoolInfo,
    utilization: float,
    context: "OptimizationContext",
) -> Tuple[int, List[str]]:
    """Nodes a consolidation could remove, choosing the cheapest ones (conservative).

    After consolidation the pool still needs ``ceil(node_count × utilization)``
    nodes (at least one) to host its current requests, where utilization is
    the larger of the CPU and memory request utilization.
    """
    memory_utilization = _pool_memory_utilization(pool, context)
    if memory_utilization is not None:
        utilization = max(utilization, memory_utilization)
    keep = max(1, math.ceil(pool.node_count * max(utilization, 0.0)))
    removable = max(pool.node_count - keep, 0)
    if removable == 0:
        return 0, []
    projector = ImpactProjector.for_context(context)
    priced = sorted(
        (price, name)
        for name in pool.node_names
        if (price := projector.node_hourly_price(name)) is not None and price > 0
    )
    return removable, [name for _price, name in priced[:removable]]
