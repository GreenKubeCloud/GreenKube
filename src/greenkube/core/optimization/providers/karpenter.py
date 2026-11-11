# src/greenkube/core/optimization/providers/karpenter.py
"""Karpenter recommendation source (Phase 6) and NodePool policy advice (roadmap G5).

Reads Karpenter NodePools and NodeClaims and emits node-pool consolidation
recommendations when a multi-node pool is consistently under-utilized. The
connector is optional: it degrades gracefully when the Karpenter CRDs are not
installed.

The ``greenkube.nodepool`` source also emits **policy signals**, high-risk and
advisory only (no v1 recommendation, no pull request until NodePool actions
exist, roadmap H7):

* ``consolidationPolicy: WhenEmpty`` -> ``WhenEmptyOrUnderutilized``;
* ``consolidateAfter`` above :data:`EXCESSIVE_CONSOLIDATE_AFTER` (or ``Never``);
* on-demand only capacity on a pool whose observed workloads are all stateless -> allow spot;
* a single architecture (amd64) -> allow arm64 where every image is multi-arch.

Each carries the suggested NodePool patch in ``evidence.data.suggested_patch``.
"""

import logging
import math
import re
from datetime import timedelta
from typing import TYPE_CHECKING, Any, ClassVar, Dict, FrozenSet, List, Optional, Protocol, Tuple

from greenkube.collectors.karpenter_collector import KarpenterCollector, NodePoolInfo
from greenkube.core.optimization.providers.base import RecommendationSource
from greenkube.impact.projector import ImpactProjector, impact_fields
from greenkube.models.metrics import (
    Recommendation,
    RecommendationCapability,
    RecommendationType,
)
from greenkube.models.metrics import RecommendationSource as Source
from greenkube.signals.adapters import RecommendationSourceAdapter
from greenkube.signals.model import (
    AdvisoryProposal,
    AlgorithmInfo,
    Capability,
    SignalEvidence,
    SignalOrigin,
    SignalProvenance,
    SourceSignal,
    TargetRef,
)
from greenkube.signals.sdk import CollectContext, SourceBatch, SourceDeps, SourceTier

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
        #: NodePools read by the last ``collect()`` (policy signals).
        self.last_pools: List[NodePoolInfo] = []

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
        self.last_pools = []
        try:
            pools = await self._collector.collect()
        except Exception as exc:
            logger.warning("Karpenter collection failed: %s. Proceeding without NodePool recommendations.", exc)
            return []
        if not pools:
            return []
        self.last_pools = list(pools)

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


#: ``consolidateAfter`` above this delays consolidation enough to matter.
EXCESSIVE_CONSOLIDATE_AFTER = timedelta(hours=1)
#: ``consolidateAfter`` suggested instead.
SUGGESTED_CONSOLIDATE_AFTER = "5m"
CAPACITY_TYPE_KEY = "karpenter.sh/capacity-type"
ARCH_KEY = "kubernetes.io/arch"
#: Workload kinds that make a pool stateful (no spot suggestion).
_STATEFUL_KINDS = frozenset({"StatefulSet"})
_DURATION_PART = re.compile(r"(\d+(?:\.\d+)?)(h|m|s)")


def parse_duration(value: Optional[str]) -> Optional[timedelta]:
    """A Go duration (``90s``, ``1h30m``); ``Never`` is infinite (``timedelta.max``); None when unreadable."""
    if value is None:
        return None
    text = str(value).strip()
    if text == "Never":
        return timedelta.max
    parts = _DURATION_PART.findall(text)
    if not parts or "".join(f"{number}{unit}" for number, unit in parts) != text:
        return None
    scale = {"h": 3600, "m": 60, "s": 1}
    return timedelta(seconds=sum(float(number) * scale[unit] for number, unit in parts))


def _requirement(pool: NodePoolInfo, key: str) -> Optional[Dict[str, Any]]:
    for requirement in pool.requirements or []:
        if isinstance(requirement, dict) and requirement.get("key") == key:
            return requirement
    return None


def _allowed_values(pool: NodePoolInfo, key: str, default: Tuple[str, ...]) -> Optional[Tuple[str, ...]]:
    """Values a requirement allows (``In``), the Karpenter default when absent; None when not a plain list."""
    requirement = _requirement(pool, key)
    if requirement is None:
        return default
    if requirement.get("operator") != "In":
        return None
    return tuple(sorted(str(value) for value in requirement.get("values") or []))


def _pool_workload_kinds(pool: NodePoolInfo, context: "OptimizationContext") -> Optional[set]:
    """Owner kinds observed on the pool's nodes in this scan; None when nothing was observed."""
    nodes = set(pool.node_names)
    kinds = {str(metric.owner_kind) for metric in (context.metrics or []) if metric.node in nodes and metric.owner_kind}
    return kinds or None


def _requirements_patch(key: str, values: List[str]) -> Dict[str, Any]:
    return {"spec": {"template": {"spec": {"requirements": [{"key": key, "operator": "In", "values": values}]}}}}


def nodepool_policy_findings(pool: NodePoolInfo, context: "OptimizationContext") -> List[Dict[str, Any]]:
    """Policy advice for one NodePool: ``kind``, ``text``, ``patch`` and ``facts`` per finding."""
    findings: List[Dict[str, Any]] = []
    if pool.consolidation_policy == "WhenEmpty":
        findings.append(
            {
                "kind": "NODEPOOL_CONSOLIDATION_POLICY",
                "text": (
                    f"NodePool {pool.name} only removes empty nodes (consolidationPolicy: WhenEmpty). "
                    "WhenEmptyOrUnderutilized also consolidates under-utilized nodes, so request cuts free nodes."
                ),
                "patch": {"spec": {"disruption": {"consolidationPolicy": "WhenEmptyOrUnderutilized"}}},
                "facts": {"consolidation_policy": pool.consolidation_policy},
            }
        )
    delay = parse_duration(pool.consolidate_after)
    if delay is not None and delay > EXCESSIVE_CONSOLIDATE_AFTER:
        findings.append(
            {
                "kind": "NODEPOOL_CONSOLIDATE_AFTER",
                "text": (
                    f"NodePool {pool.name} waits {pool.consolidate_after} before consolidating "
                    f"(consolidateAfter); {SUGGESTED_CONSOLIDATE_AFTER} lets freed capacity become removed nodes."
                ),
                "patch": {"spec": {"disruption": {"consolidateAfter": SUGGESTED_CONSOLIDATE_AFTER}}},
                "facts": {"consolidate_after": pool.consolidate_after},
            }
        )
    capacity = _allowed_values(pool, CAPACITY_TYPE_KEY, ("on-demand",))
    kinds = _pool_workload_kinds(pool, context)
    if capacity == ("on-demand",) and kinds is not None and not (kinds & _STATEFUL_KINDS):
        findings.append(
            {
                "kind": "NODEPOOL_CAPACITY_TYPE",
                "text": (
                    f"NodePool {pool.name} runs on-demand capacity only while its observed workloads are "
                    "stateless; allowing spot capacity cuts node prices. Spot nodes can be reclaimed at short "
                    "notice: check that the workloads tolerate interruptions."
                ),
                "patch": _requirements_patch(CAPACITY_TYPE_KEY, ["on-demand", "spot"]),
                "facts": {"capacity_types": ["on-demand"], "workload_kinds": sorted(kinds)},
            }
        )
    arch = _allowed_values(pool, ARCH_KEY, ("amd64",))
    if arch == ("amd64",):
        findings.append(
            {
                "kind": "NODEPOOL_ARCHITECTURE",
                "text": (
                    f"NodePool {pool.name} only allows amd64 nodes. Allowing arm64 usually lowers node prices and "
                    "energy use, but only if every image running on the pool is multi-arch, which GreenKube "
                    "cannot verify."
                ),
                "patch": _requirements_patch(ARCH_KEY, ["amd64", "arm64"]),
                "facts": {"architectures": ["amd64"], "requirement_set": _requirement(pool, ARCH_KEY) is not None},
            }
        )
    return findings


def nodepool_policy_signals(
    pools: List[NodePoolInfo], context: "OptimizationContext", ctx: CollectContext, fetched_at: Any
) -> List[SourceSignal]:
    """High-risk advisory signals of the NodePool policy findings (roadmap G5)."""
    algorithm = AlgorithmInfo(
        name="greenkube.nodepool.policy",
        params={
            "excessive_consolidate_after_seconds": int(EXCESSIVE_CONSOLIDATE_AFTER.total_seconds()),
            "suggested_consolidate_after": SUGGESTED_CONSOLIDATE_AFTER,
        },
        window=ctx.window,
    )
    signals: List[SourceSignal] = []
    for pool in pools:
        for finding in nodepool_policy_findings(pool, context):
            signals.append(
                SourceSignal(
                    source_id=NodePoolSignalSource.id,
                    source_ref=pool.name,
                    target=TargetRef.for_kind(ctx.cluster_uid, "NodePool", pool.name),
                    capability=Capability.NODE_POOL,
                    kind=finding["kind"],
                    proposal=AdvisoryProposal(text=finding["text"]),
                    algorithm=algorithm,
                    evidence=SignalEvidence(
                        summary=finding["text"],
                        details=(
                            "High risk: a NodePool change affects every workload on the pool. Advisory only: "
                            "review it and apply it in Git yourself; GreenKube does not open pull requests for "
                            "NodePools yet."
                        ),
                        data={
                            "risk_class": "high",
                            "advisory": True,
                            "suggested_patch": finding["patch"],
                            "facts": finding["facts"],
                            "nodes": len(pool.node_names),
                        },
                    ),
                    observed_window=ctx.window,
                    provenance=SignalProvenance(origin=SignalOrigin.NATIVE),
                    raw={"nodepool": pool.name},
                    fetched_at=fetched_at,
                )
            )
    return signals


class NodePoolSignalSource(RecommendationSourceAdapter):
    """GreenKube's NodePool consolidation heuristic over Karpenter objects (roadmap E5, G5).

    A GreenKube heuristic that reads Karpenter NodePools and NodeClaims, not
    Karpenter's own advice, hence the native id ``greenkube.nodepool``. Its v1
    recommendations keep ``source: karpenter`` for compatibility.
    """

    id: ClassVar[str] = "greenkube.nodepool"
    display_name: ClassVar[str] = "GreenKube NodePool consolidation"
    tier: ClassVar[SourceTier] = SourceTier.NATIVE
    capabilities: ClassVar[FrozenSet[Capability]] = frozenset({Capability.NODE_POOL})
    legacy_source: ClassVar[Optional[str]] = Source.KARPENTER.value
    algorithm_name: ClassVar[Optional[str]] = "greenkube.nodepool"
    signal_origin: ClassVar[SignalOrigin] = SignalOrigin.NATIVE
    description: ClassVar[str] = (
        "Under-utilized Karpenter NodePools that could consolidate onto fewer nodes, and NodePool policy advice "
        "(consolidation, capacity type, architecture)."
    )

    def build_connector(self, deps: SourceDeps) -> RecommendationSource:
        return KarpenterSource(deps.config)

    def raw_payload(self, rec: Recommendation) -> Optional[Dict[str, Any]]:
        return {"nodepool": rec.source_ref}

    async def collect(self, ctx: CollectContext) -> SourceBatch:
        batch = await super().collect(ctx)
        pools = list(getattr(self.connector, "last_pools", None) or [])
        if pools and ctx.optimization is not None and ctx.namespace is None:
            batch.signals.extend(nodepool_policy_signals(pools, ctx.optimization, ctx, self.deps.clock()))
        return batch
